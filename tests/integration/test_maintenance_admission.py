"""Finite maintenance pauses serialize admission without rewriting ownership."""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import DatabaseError, close_old_connections, connection, transaction

from django_ray import maintenance as maintenance
from django_ray.models import (
    RayMaintenanceAudit,
    RayMaintenancePolicy,
    RayMaintenanceScope,
    RayTaskCohortClaim,
    RayTaskExecution,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.cohort_claim import CohortResolutionKind
from tests.integration.test_cohort_claim_storage import (
    _claim,
    _fresh_draining_ray_arguments,
    _hold,
    _mutate,
    _ray_arguments,
    _resolve_ray_claim_and_queue_next_attempt,
    storage,
)
from tests.integration.test_cohort_claim_storage import case as _cohort_case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)

pytestmark = pytest.mark.django_db(transaction=True)
cohort_case = _cohort_case


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgresql", id="postgresql", marks=pytest.mark.postgresql),
    ]
)
def selected_database(request):
    if connection.vendor != request.param:
        pytest.skip(f"Requires {request.param}")


def _change(scopes=(), **kwargs):
    values = {
        "pause_enqueues": False,
        "pause_claims": False,
        "expected_revision": maintenance.read_maintenance_policy().revision,
        "actor": "test-operator",
        "reason": "maintenance",
        "authorized": True,
    }
    values.update(kwargs)
    return maintenance.replace_maintenance_policy(scopes, **values)


def _task(queue="default", **kwargs):
    return RayTaskExecution.objects.create(
        task_id=str(uuid4()), callable_path="tests.tasks.add", queue_name=queue, **kwargs
    )


def _check(queue="default", protocol=1, **kwargs):
    with transaction.atomic(), maintenance.maintenance_admission_barrier() as token:
        return maintenance.check_maintenance_admission(
            queue, protocol, operation="claim", barrier=token, **kwargs
        )


def _postgresql_maintenance_refusal(error):
    cause = error.__cause__
    sqlstate = getattr(cause, "sqlstate", getattr(cause, "pgcode", None))
    return sqlstate == "P0001" and str(error).splitlines()[0] == "maintenance admission rejected"


@pytest.mark.usefixtures("selected_database")
def test_global_enqueue_pause_covers_unseen_queues_but_not_owned_completion():
    running = _task(state="RUNNING")
    queued = _task("already-queued")
    _change(pause_enqueues=True)
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="paused"):
        maintenance.check_maintenance_admission(
            "never-seen", 1, operation="enqueue", preflight=True
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        _task("never-seen")
    # A producer pause lets an admitted backlog run and complete.
    _check("already-queued")
    RayTaskExecution.objects.filter(pk=queued.pk).update(state="RUNNING", execution_generation=1)
    RayTaskExecution.objects.filter(pk=running.pk).update(state="CANCELLING")
    RayTaskExecution.objects.filter(pk=running.pk).update(state="CANCELLED")
    RayTaskExecution.objects.filter(pk=queued.pk).update(state="SUCCEEDED")
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=queued.pk).update(state="QUEUED")


@pytest.mark.usefixtures("selected_database")
def test_claim_pause_allows_enqueue_but_blocks_raw_generation():
    _change(pause_claims=True)
    task = _task()
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="paused"):
        _check()
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=task.pk).update(state="RUNNING", execution_generation=1)
    task.refresh_from_db()
    assert (task.state, task.execution_generation) == ("QUEUED", 0)


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize("queue", ["队列", " work queue ", "a b"])
def test_queue_scope_preserves_exact_spelling(queue):
    _change((maintenance.MaintenanceScope("queue", queue_name=queue, pause_enqueues=True),))
    with pytest.raises(DatabaseError), transaction.atomic():
        _task(queue)
    _task(queue + "x")
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="paused"):
        _check(queue)
    _check(queue + "x")


@pytest.mark.usefixtures("selected_database")
def test_protocol_scope_is_exact_and_pauses_queue_movement():
    task = _task("other")
    _change(
        (
            maintenance.MaintenanceScope("protocol", protocol_version=3),
            maintenance.MaintenanceScope("queue", queue_name="paused", pause_enqueues=True),
        )
    )
    _check(protocol=1)
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="paused"):
        _check(protocol=3)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=task.pk).update(queue_name="paused")


@pytest.mark.usefixtures("selected_database")
def test_revision_audit_dry_run_and_idempotence():
    before = maintenance.read_maintenance_policy()
    proposal = _change(pause_claims=True, dry_run=True)
    assert proposal.changed and proposal.dry_run and proposal.policy.revision == before.revision + 1
    assert maintenance.read_maintenance_policy() == before
    assert RayMaintenanceAudit.objects.count() == 1
    actual = _change(pause_claims=True)
    assert actual.policy == maintenance.read_maintenance_policy()
    assert RayMaintenanceAudit.objects.count() == 2
    assert not _change(pause_claims=True).changed
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="revision_changed"):
        _change(expected_revision=before.revision)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayMaintenancePolicy.objects.update(pause_claims=False)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayMaintenanceAudit.objects.filter(pk=actual.policy.revision).update(actor="rewritten")


@pytest.mark.usefixtures("selected_database")
def test_scope_is_immutable_and_missing_scope_fails_closed():
    _change((maintenance.MaintenanceScope("queue", queue_name="paused"),))
    with pytest.raises(DatabaseError), transaction.atomic():
        RayMaintenanceScope.objects.update(queue_name="other")
    # Privileged deletion/corruption cannot turn a retained pause into admission.
    RayMaintenanceScope.objects.all().delete()
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="unavailable"):
        _check("other")
    with pytest.raises(DatabaseError), transaction.atomic():
        _task("other")


@pytest.mark.usefixtures("selected_database")
def test_missing_policy_fails_closed_but_completion_survives():
    task = _task(state="RUNNING")
    RayMaintenancePolicy.objects.all().delete()
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="unavailable"):
        _check()
    with pytest.raises(DatabaseError), transaction.atomic():
        _task()
    RayTaskExecution.objects.filter(pk=task.pk).update(state="SUCCEEDED")


@pytest.mark.usefixtures("selected_database")
def test_scope_publication_rolls_back_audit_on_insert_failure(monkeypatch):
    before = maintenance.read_maintenance_policy()
    original = maintenance.RayMaintenanceScope.objects.get_queryset

    def broken():
        query = original()
        monkeypatch.setattr(
            type(query),
            "bulk_create",
            lambda *a, **k: (_ for _ in ()).throw(DatabaseError("injected")),
        )
        return query

    monkeypatch.setattr(maintenance.RayMaintenanceScope.objects, "get_queryset", broken)
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="persistence_refused"):
        _change((maintenance.MaintenanceScope("queue", queue_name="paused"),))
    assert maintenance.read_maintenance_policy() == before
    assert RayMaintenanceAudit.objects.count() == 1


@pytest.mark.usefixtures("selected_database")
def test_active_barrier_is_transaction_and_identity_bound():
    with (
        pytest.raises(maintenance.MaintenanceAdmissionError, match="transaction_required"),
        maintenance.maintenance_admission_barrier(),
    ):
        pass
    with transaction.atomic():
        maintenance.check_maintenance_admission("default", 1, operation="enqueue", preflight=True)
        with pytest.raises(maintenance.MaintenanceAdmissionError, match="barrier_required"):
            maintenance.check_maintenance_admission("default", 1, operation="enqueue")
        with maintenance.maintenance_admission_barrier() as token:
            maintenance.require_maintenance_admission_barrier(token)
            with pytest.raises(maintenance.MaintenanceAdmissionError, match="barrier_required"):
                maintenance.require_maintenance_admission_barrier(replace(token))
            with transaction.atomic():
                maintenance.require_maintenance_admission_barrier(token)
        with pytest.raises(maintenance.MaintenanceAdmissionError, match="barrier_required"):
            maintenance.require_maintenance_admission_barrier(token)
    with transaction.atomic(), maintenance.maintenance_admission_barrier():
        with pytest.raises(maintenance.MaintenanceAdmissionError, match="barrier_required"):
            maintenance.require_maintenance_admission_barrier(token)


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize(
    "changes",
    [
        {"authorized": False},
        {"authorized": 1},
        {"pause_claims": 1},
        {"dry_run": 1},
        {"expected_revision": True},
        {"expected_revision": 0},
        {"expected_revision": 1.5},
        {"actor": ""},
        {"actor": "credential\nvalue"},
        {"reason": ""},
    ],
)
def test_malformed_control_refuses_before_writes(changes):
    before = maintenance.read_maintenance_policy()
    with pytest.raises(maintenance.MaintenanceAdmissionError):
        _change(**changes)
    assert maintenance.read_maintenance_policy() == before


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize(
    "scopes",
    [
        [],
        (maintenance.MaintenanceScope("queue", queue_name=" "),),
        (maintenance.MaintenanceScope("queue", queue_name="a\x00b"),),
        (maintenance.MaintenanceScope("queue", queue_name="a" * 101),),
        (maintenance.MaintenanceScope("protocol", protocol_version=True),),
        (maintenance.MaintenanceScope("target", target_id="missing"),),
        (maintenance.MaintenanceScope("target", target_id="missing", pause_enqueues=True),),
        (maintenance.MaintenanceScope("queue", queue_name="x"),) * 2,
        tuple(maintenance.MaintenanceScope("queue", queue_name=str(i)) for i in range(65)),
    ],
)
def test_invalid_scope_sets_refuse_without_partial_revision(scopes):
    with pytest.raises(maintenance.MaintenanceAdmissionError):
        _change(scopes)
    assert RayMaintenanceAudit.objects.count() == 1


@pytest.mark.usefixtures("selected_database")
def test_policy_transition_requires_own_outer_transaction_and_fresh_clock(monkeypatch):
    with (
        transaction.atomic(),
        pytest.raises(maintenance.MaintenanceAdmissionError, match="transaction_open"),
    ):
        _change(pause_claims=True)
    before = maintenance.read_maintenance_policy()
    monkeypatch.setattr(
        maintenance, "_clock", lambda: before.updated_at - timedelta(microseconds=1)
    )
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="clock_regression"):
        _change(pause_claims=True)


@pytest.mark.usefixtures("selected_database")
def test_reverse_refuses_retained_history_in_outer_transaction():
    _change(pause_claims=True)
    migration = importlib.import_module("django_ray.migrations.0031_maintenance_admission")
    with transaction.atomic():
        with pytest.raises(RuntimeError, match="retained policy history"):
            migration._remove(apps, connection.schema_editor(atomic=False))
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="paused"):
        _check()


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
def test_target_pause_fences_resolved_history_on_draining_target(cohort_case, family, monkeypatch):
    original = _ray_arguments(cohort_case, RayRunnerFamily(family))
    first = _claim(cohort_case, **original)
    _resolve_ray_claim_and_queue_next_attempt(cohort_case, first)
    current, policy = _fresh_draining_ray_arguments(cohort_case, original)
    assert policy.desired_state == "draining"
    _change((maintenance.MaintenanceScope("target", target_id=policy.target_id),))
    before = RayTaskExecution.objects.values().get(pk=cohort_case.task.pk)
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="paused"):
        _claim(cohort_case, **current)
    with monkeypatch.context() as raw:
        raw.setattr(storage, "check_maintenance_admission", lambda *args, **kwargs: None)
        with pytest.raises(storage.CohortClaimStorageError, match="persistence_refused"):
            _claim(cohort_case, **current)
    assert RayTaskExecution.objects.values().get(pk=cohort_case.task.pk) == before
    assert RayTaskCohortClaim.objects.count() == 1
    _change()
    second = _claim(cohort_case, **current)
    assert (
        second.facts.binding == first.facts.binding
        and second.facts.identity.execution_generation == 2
    )


@pytest.mark.usefixtures("selected_database")
def test_pauses_preserve_held_identity_and_authentic_late_completion(cohort_case):
    first = _claim(cohort_case)
    prepared = _mutate(
        cohort_case, first, storage.prepare_cohort_claim, request_digest="sha256:" + "e" * 64
    )
    dispatched = _mutate(cohort_case, prepared, storage.mark_cohort_claim_dispatched)
    held = _hold(cohort_case, dispatched)
    initial_hold = RayTaskCohortClaim.objects.get(pk=held.claim_id).hold_reason
    _change(pause_enqueues=True, pause_claims=True)
    resolved = _mutate(
        cohort_case,
        held,
        storage.resolve_cohort_claim,
        kind=CohortResolutionKind.APPLICATION_COMPLETED,
        evidence_digest="sha256:" + "f" * 64,
    )
    RayTaskExecution.objects.filter(pk=cohort_case.task.pk).update(state="SUCCEEDED")
    assert resolved.facts == first.facts and resolved.disposition == "RESOLVED"
    assert RayTaskCohortClaim.objects.get(pk=held.claim_id).hold_reason == initial_hold


@pytest.mark.postgresql
def test_postgresql_shared_admission_finishes_before_exclusive_pause():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    revision = maintenance.read_maintenance_policy().revision
    began, finished = Event(), Event()

    def writer():
        close_old_connections()
        try:
            began.set()
            result = _change(pause_enqueues=True, expected_revision=revision)
            finished.set()
            return result
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic(), maintenance.maintenance_admission_barrier():
            future = executor.submit(writer)
            assert began.wait(5)
            assert not finished.wait(0.05)
            _task()
        assert future.result(timeout=10).changed
    with pytest.raises(DatabaseError), transaction.atomic():
        _task("later")
    assert RayTaskExecution.objects.count() == 1


@pytest.mark.postgresql
def test_postgresql_policy_revision_has_one_winner():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    revision = maintenance.read_maintenance_policy().revision
    start = Barrier(2)

    def change(value):
        close_old_connections()
        try:
            start.wait(timeout=5)
            try:
                _change(pause_enqueues=value, pause_claims=not value, expected_revision=revision)
                return "changed"
            except maintenance.MaintenanceAdmissionError as error:
                return error.reason.value
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(change, (False, True))) == ["changed", "revision_changed"]


@pytest.mark.usefixtures("selected_database")
def test_full_unicode_queue_scope_capacity_is_supported():
    # Astral characters use twelve bytes each in canonical escaped JSON; all
    # otherwise-valid 64 x 100-character queue names must still fit the bound.
    scopes = tuple(
        maintenance.MaintenanceScope("queue", queue_name="😀" * 97 + f"{i:03}") for i in range(64)
    )
    result = _change(scopes)
    assert len(result.policy.scopes) == 64
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="paused"):
        _check(scopes[-1].queue_name)


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize(
    "scope",
    [
        None,
        maintenance.MaintenanceScope("unknown"),
        maintenance.MaintenanceScope("queue", queue_name="x", pause_claims=None),
        maintenance.MaintenanceScope("queue", queue_name="x", pause_claims=False),
        maintenance.MaintenanceScope("queue", queue_name="x", protocol_version=3),
        maintenance.MaintenanceScope("protocol", protocol_version=3, queue_name="x"),
        maintenance.MaintenanceScope("queue", queue_name="\ud800"),
    ],
)
def test_malformed_scope_facts_refuse_before_database_publication(scope):
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="invalid"):
        _change((scope,))
    assert RayMaintenanceAudit.objects.count() == 1


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize(
    "changes",
    [
        {"preflight": 1},
        {"operation": "unknown"},
        {"protocol_version": True},
        {"queue_name": "\x00"},
        {"target_id": " target "},
    ],
)
def test_malformed_admission_observation_is_never_allowed(changes):
    values = {
        "queue_name": "default",
        "protocol_version": 3,
        "operation": "claim",
        "preflight": True,
    }
    values.update(changes)
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="invalid"):
        maintenance.check_maintenance_admission(**values)


@pytest.mark.usefixtures("selected_database")
def test_pure_snapshot_is_not_constructible_authority():
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="invalid"):
        maintenance.maintenance_admission_allowed(None, "default", 3, operation="claim")
    snapshot = maintenance.read_maintenance_policy()
    _change(pause_claims=True)
    assert maintenance.maintenance_admission_allowed(snapshot, "default", 3, operation="claim")
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="paused"):
        _check(protocol=3)


@pytest.mark.usefixtures("selected_database")
def test_barrier_cannot_outlive_rolled_back_acquisition_savepoint():
    # Exercise even explicitly-entered contexts: lexical token liveness alone
    # cannot retain a PG advisory lock that savepoint rollback has released.
    with transaction.atomic():
        inner = transaction.atomic()
        inner.__enter__()
        context = maintenance.maintenance_admission_barrier()
        token = context.__enter__()
        try:
            inner.__exit__(ValueError, ValueError("rollback"), None)
            with pytest.raises(maintenance.MaintenanceAdmissionError, match="barrier_required"):
                maintenance.require_maintenance_admission_barrier(token)
        finally:
            context.__exit__(None, None, None)


@pytest.mark.usefixtures("selected_database")
def test_malformed_clock_and_unsupported_database_refuse(monkeypatch):
    monkeypatch.setattr(maintenance, "_clock", lambda: None)
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="invalid"):
        _change(pause_claims=True)
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="invalid"):
        maintenance.read_maintenance_policy(using=1)
    monkeypatch.setattr(connection, "vendor", "unsupported")
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="unavailable"):
        maintenance.read_maintenance_policy()


@pytest.mark.usefixtures("selected_database")
def test_deleted_current_policy_cannot_reactivate_old_seed_revision():
    _change(pause_claims=True)
    seed = RayMaintenanceAudit.objects.get(pk=1)
    RayMaintenancePolicy.objects.all().delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        RayMaintenancePolicy.objects.create(
            singleton_key=1,
            schema_version=1,
            revision=1,
            pause_enqueues=False,
            pause_claims=False,
            updated_at=seed.created_at,
        )


@pytest.mark.usefixtures("selected_database")
def test_new_claim_rechecks_exact_selected_queue_under_lock(cohort_case, monkeypatch):
    original = storage._lock_task
    selected_queue = cohort_case.task.queue_name

    def changed(pk, **kwargs):
        RayTaskExecution.objects.filter(pk=pk).update(queue_name="other-worker-queue")
        return original(pk, **kwargs)

    monkeypatch.setattr(storage, "_lock_task", changed)
    with pytest.raises(storage.CohortClaimStorageError, match="execution_changed"):
        _claim(cohort_case, expected_queue_name=selected_queue)
    assert not RayTaskCohortClaim.objects.exists()


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize("bounds", [(1, 3), (3, 4)])
def test_new_claim_refuses_preexisting_broad_range_lease(cohort_case, bounds):
    original = TaskWorkerLease.objects.get(pk=cohort_case.owner.worker_id)
    lease = TaskWorkerLease.objects.create(
        worker_id="broad-range",
        hostname=original.hostname,
        pid=101,
        started_at=original.started_at,
        last_heartbeat_at=original.last_heartbeat_at,
        django_ray_version=original.django_ray_version,
        capability_schema_version=1,
        min_supported_execution_protocol_version=bounds[0],
        max_supported_execution_protocol_version=bounds[1],
        legacy_admission_token=None,
    )
    cohort_case.owner = WorkerLeaseIdentity(
        lease.worker_id, lease.hostname, lease.pid, lease.started_at
    )
    with pytest.raises(storage.CohortClaimStorageError, match="lease_unavailable"):
        _claim(cohort_case)
    assert not RayTaskCohortClaim.objects.exists()


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize(
    "changes",
    [
        {"skip_locked": 1},
        {"expected_queue_name": "\x00"},
        {"expected_queue_name": ""},
        {"expected_queue_name": False},
    ],
)
def test_new_claim_refuses_malformed_selection_controls(cohort_case, changes):
    with pytest.raises(storage.CohortClaimStorageError, match="invalid"):
        _claim(cohort_case, **changes)
    assert not RayTaskCohortClaim.objects.exists()


@pytest.mark.usefixtures("selected_database")
@pytest.mark.parametrize("kind", ["global", "queue", "protocol"])
def test_sql_claim_ledger_fences_survive_bypassed_python_check(cohort_case, kind, monkeypatch):
    scopes = ()
    if kind == "queue":
        scopes = (maintenance.MaintenanceScope("queue", queue_name=cohort_case.task.queue_name),)
    elif kind == "protocol":
        scopes = (maintenance.MaintenanceScope("protocol", protocol_version=3),)
    _change(scopes, pause_claims=kind == "global")
    monkeypatch.setattr(storage, "check_maintenance_admission", lambda *args, **kwargs: None)
    with pytest.raises(storage.CohortClaimStorageError, match="persistence_refused"):
        _claim(cohort_case)
    assert not RayTaskCohortClaim.objects.exists()
    cohort_case.task.refresh_from_db()
    assert cohort_case.task.state == "QUEUED" and cohort_case.task.execution_generation == 0


@pytest.mark.postgresql
def test_postgresql_raw_policy_writer_takes_barrier_before_row_lock():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    began = Event()
    process_id = []

    def raw_writer():
        close_old_connections()
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout='8s'")
                cursor.execute("SELECT pg_backend_pid()")
                process_id.append(cursor.fetchone()[0])
                began.set()
                try:
                    # Invalid unaudited edit; it must wait at the statement
                    # barrier before owning the policy tuple, then refuse.
                    cursor.execute("UPDATE django_ray_raymaintenancepolicy SET pause_claims=TRUE")
                except DatabaseError as error:
                    return (
                        "refused"
                        if _postgresql_maintenance_refusal(error)
                        else "unexpected-database-error"
                    )
            return "incorrectly-published"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic(), maintenance.maintenance_admission_barrier():
            future = executor.submit(raw_writer)
            assert began.wait(5)
            deadline = monotonic() + 5
            waiting = False
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted)",
                        [process_id[0]],
                    )
                    waiting = cursor.fetchone()[0]
                if waiting:
                    break
                sleep(0.01)
            assert waiting
            assert (
                RayMaintenancePolicy.objects.select_for_update(nowait=True).get(pk=1).revision == 1
            )
        assert future.result(timeout=10) == "refused"


@pytest.mark.postgresql
def test_postgresql_raw_insert_after_wait_observes_new_committed_pause():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    began = Event()
    process_id = []

    def enqueue():
        close_old_connections()
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout='8s'")
                cursor.execute("SELECT pg_backend_pid()")
                process_id.append(cursor.fetchone()[0])
                began.set()
                try:
                    _task("arrives-during-pause")
                except DatabaseError as error:
                    return (
                        "refused"
                        if _postgresql_maintenance_refusal(error)
                        else "unexpected-database-error"
                    )
            return "incorrectly-enqueued"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            maintenance._lock(using="default", exclusive=True)
            previous = maintenance.read_maintenance_policy()
            seed = RayMaintenanceAudit.objects.get(pk=previous.revision)
            now = previous.updated_at + timedelta(seconds=1)
            RayMaintenanceAudit.objects.create(
                revision=2,
                previous_revision=1,
                pause_enqueues=True,
                pause_claims=False,
                scope_count=0,
                scopes_json="[]",
                scopes_digest=seed.scopes_digest,
                actor="test-operator",
                reason="pause-before-insert",
                created_at=now,
            )
            RayMaintenancePolicy.objects.filter(pk=1).update(
                revision=2, pause_enqueues=True, updated_at=now
            )
            future = executor.submit(enqueue)
            assert began.wait(5)
            deadline = monotonic() + 5
            waiting = False
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted)",
                        [process_id[0]],
                    )
                    waiting = cursor.fetchone()[0]
                if waiting:
                    break
                sleep(0.01)
            assert waiting
        assert future.result(timeout=10) == "refused"
    assert not RayTaskExecution.objects.exists()
