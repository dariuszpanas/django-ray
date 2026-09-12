"""Exact operator decisions fence new ownership without falsifying owned work."""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import DatabaseError, close_old_connections, connection, transaction

from django_ray import lifecycle
from django_ray import maintenance as controls
from django_ray.execution_codec import ExecutionIdentity
from django_ray.models import (
    RayTaskCohortClaim,
    RayTaskExecution,
    RayTaskQuarantine,
    RayWorkerRetirement,
    TaskWorkerLease,
)
from django_ray.runner import cohort_dispatch as dispatch
from django_ray.runner.cohort_claims import ClaimedCohortTask
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.target.attestation import RayRunnerFamily
from tests.integration.test_cohort_cancellation import _apply as _apply_cancellation
from tests.integration.test_cohort_claim_storage import _claim, _lease, _ray_arguments, storage
from tests.integration.test_cohort_claim_storage import case as _cohort_case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_completion import _apply, _result, _started

cohort_case = _cohort_case
pytestmark = pytest.mark.django_db(transaction=True)
DIGEST = "sha256:" + "a" * 64
OPERATOR = {"actor": "test-operator", "reason": "planned-maintenance", "authorized": True}


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def selected_database(request):
    if connection.vendor != request.param:
        pytest.skip(f"Requires {request.param}")


def _identity(task):
    return ExecutionIdentity(task.pk, task.task_id, task.attempt_number, task.execution_generation)


def _quarantine(task, *, revision=0, quarantined=True, **changes):
    return controls.set_task_quarantine(
        _identity(task),
        expected_revision=revision,
        quarantined=quarantined,
        **(OPERATOR | changes),
    )


def _request(identity, *, revision=0, **changes):
    return controls.request_worker_retirement(
        identity,
        expected_revision=revision,
        **(OPERATOR | changes),
    )


def _complete(identity, **changes):
    return controls.complete_worker_retirement(
        identity,
        **(
            {
                **OPERATOR,
                "expected_revision": 1,
                "independently_confirmed_cleanup": True,
                "cleanup_evidence_digest": DIGEST,
                "cleanup_confirmed_at": controls._clock(),
            }
            | changes
        ),
    )


@pytest.fixture(autouse=True)
def isolated_controls(transactional_db, isolated_sqlite_ledger_maintenance):
    """Release fixture controls before ordinary SQLite parent-first maintenance."""
    yield
    if connection.vendor != "sqlite":
        return
    for pk in RayTaskQuarantine.objects.values_list("task_execution_pk", flat=True).distinct():
        latest = RayTaskQuarantine.objects.filter(task_execution_pk=pk).latest("revision")
        task = RayTaskExecution.objects.filter(pk=pk).first()
        if task is not None and latest.state == "QUARANTINED":
            _quarantine(task, revision=latest.revision, quarantined=False)
    RayTaskExecution.objects.filter(
        pk__in=RayTaskQuarantine.objects.values("task_execution_pk")
    ).exclude(pk__in=RayTaskCohortClaim.objects.values("binding_id")).delete()
    TaskWorkerLease.objects.filter(
        worker_id__in=RayWorkerRetirement.objects.values("worker_id")
    ).delete()


@pytest.fixture
def case_data(monkeypatch):
    now = datetime.now(UTC)
    value = SimpleNamespace(now=now)
    monkeypatch.setattr(controls, "_clock", lambda: value.now)
    value.task = RayTaskExecution.objects.create(
        task_id=str(uuid4()),
        callable_path="tests.tasks.add",
        created_at=now - timedelta(seconds=2),
    )
    value.lease = TaskWorkerLease.objects.create(
        worker_id=str(uuid4()),
        hostname="test-host",
        pid=100,
        started_at=now - timedelta(seconds=10),
        last_heartbeat_at=now,
        capability_schema_version=1,
        django_ray_version="0.5.0",
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    value.owner = WorkerLeaseIdentity(
        value.lease.worker_id,
        value.lease.hostname,
        value.lease.pid,
        value.lease.started_at,
    )
    return value


@pytest.fixture
def owned_data(cohort_case, monkeypatch):
    """Current producer intent plus one exact manager, before any claim."""
    case = cohort_case
    case.now = datetime.now(UTC)
    case.lease.last_heartbeat_at = case.now
    case.lease.save(update_fields=["last_heartbeat_at"])
    monkeypatch.setattr(controls, "_clock", lambda: case.now)
    return case


@pytest.fixture
def owned_case(selected_database, owned_data):
    return owned_data


@pytest.fixture
def postgres_owned_case(owned_data):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    return owned_data


def _complete_application(case, value, *, success=True):
    def complete(task, decoded, *, retry_admitted):
        assert decoded.completion.success is success
        if success:
            return lifecycle.succeed_task(
                task,
                result_data="5",
                result_reference=None,
                _allow_cancelling_completion=True,
            )
        assert not retry_admitted
        return lifecycle.record_failure(
            task,
            error_message=decoded.completion.error,
            retry=False,
            _allow_cancelling_completion=True,
        )

    return _apply(value, case, _result(value, success=success), callback=complete)


@pytest.fixture
def case(selected_database, case_data):
    return case_data


@pytest.fixture
def postgres_case(case_data):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    return case_data


def test_quarantine_dry_run_exact_identity_audit_and_idempotence(case):
    before = case.task.__dict__.copy()
    preview = _quarantine(case.task, dry_run=True)
    assert preview.changed and preview.dry_run and preview.revision == 1
    assert not RayTaskQuarantine.objects.exists()
    actual = _quarantine(case.task)
    assert actual.state == "QUARANTINED" and actual.revision == 1
    assert not _quarantine(case.task, revision=1).changed
    case.task.refresh_from_db()
    assert all(case.task.__dict__[key] == value for key, value in before.items() if key != "_state")
    released = _quarantine(case.task, revision=1, quarantined=False)
    assert released.state == "RELEASED" and released.revision == 2
    assert not _quarantine(case.task, revision=2, quarantined=False).changed
    _quarantine(case.task, revision=2)
    assert list(RayTaskQuarantine.objects.order_by("revision").values_list("state", flat=True)) == [
        "QUARANTINED",
        "RELEASED",
        "QUARANTINED",
    ]
    with pytest.raises(controls.MaintenanceAdmissionError, match="revision_changed"):
        _quarantine(case.task, revision=1, quarantined=False)


@pytest.mark.parametrize("change", ["attempt", "generation", "task_id"])
def test_quarantine_refuses_stale_execution_identity_without_writing(case, change):
    identity = _identity(case.task)
    changed = {
        "attempt": {"attempt_number": identity.attempt_number + 1},
        "generation": {"execution_generation": identity.execution_generation + 1},
        "task_id": {"task_id": "another-task"},
    }[change]
    with pytest.raises(controls.MaintenanceAdmissionError, match="identity_changed"):
        controls.set_task_quarantine(
            replace(identity, **changed), quarantined=True, expected_revision=0, **OPERATOR
        )
    assert not RayTaskQuarantine.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "RUNNING", "execution_generation": 1},
        {"attempt_number": 2},
        {"execution_generation": 1},
        {"task_id": "replacement"},
    ],
)
def test_queued_generation_zero_quarantine_fences_claim_and_identity_escape(case, changes):
    _quarantine(case.task)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=case.task.pk).update(**changes)
    case.task.refresh_from_db()
    assert case.task.state == "QUEUED" and case.task.execution_generation == 0


def test_quarantine_query_excludes_before_limit_and_release_restores_candidate(case):
    case.task.priority = 100
    case.task.save(update_fields=["priority"])
    other = RayTaskExecution.objects.create(task_id=str(uuid4()), callable_path="tests.tasks.add")
    _quarantine(case.task)
    query = (
        RayTaskExecution.objects.alias(
            blocked=controls.task_quarantine_blocked_expression(),
        )
        .filter(blocked=False)
        .order_by("-priority", "pk")
    )
    assert list(query.values_list("pk", flat=True)[:1]) == [other.pk]
    _quarantine(case.task, revision=1, quarantined=False)
    assert list(query.values_list("pk", flat=True)[:1]) == [case.task.pk]


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_owned_completion_and_cancellation_remain_allowed_but_retry_is_fenced(owned_case, terminal):
    case = owned_case
    if terminal == "CANCELLED":
        arguments = _ray_arguments(case, RayRunnerFamily.RAY_CORE, observed_at=case.now)
        record = _claim(case, **arguments)
        case.task.refresh_from_db()
        value = dispatch.prepare_claimed_cohort_dispatch(
            ClaimedCohortTask(case.task, record, None), now=case.now
        )
        value = dispatch.mark_cohort_dispatch_started(value, now=case.now)
    else:
        value = _started(case)
    original = value.claim.facts
    case.task.refresh_from_db()
    _quarantine(case.task)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLING")
    with transaction.atomic(), controls.maintenance_admission_barrier() as token:
        current = RayTaskExecution.objects.select_for_update().get(pk=case.task.pk)
        assert not controls.task_quarantine_retry_allowed(current, barrier=token)
    if terminal == "CANCELLED":

        def cancel(task):
            return lifecycle.cancel_task(task, expected_worker_id=case.owner.worker_id)

        assert _apply_cancellation(case, value, callback=cancel).applied
    else:
        assert _complete_application(case, value, success=terminal == "SUCCEEDED").applied
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="QUEUED", attempt_number=2)
    case.task.refresh_from_db()
    assert case.task.state == terminal and case.task.attempt_number == 1
    assert case.task.attempts.get().state == terminal
    record = storage._record(RayTaskCohortClaim.objects.get(pk=value.claim.claim_id))
    assert record.disposition == "RESOLVED" and record.facts == original
    refused = lifecycle.request_task_retry(case.task.pk, allowed_states=(terminal,))
    assert refused.status is lifecycle.TaskRetryRequestStatus.QUARANTINED
    _quarantine(case.task, revision=1, quarantined=False)
    with transaction.atomic(), controls.maintenance_admission_barrier() as token:
        current = RayTaskExecution.objects.select_for_update().get(pk=case.task.pk)
        assert controls.task_quarantine_retry_allowed(current, barrier=token)
    result = lifecycle.request_task_retry(case.task.pk, allowed_states=(terminal,))
    assert result.status is lifecycle.TaskRetryRequestStatus.ACCEPTED
    case.task.refresh_from_db()
    assert case.task.state == "QUEUED" and case.task.attempt_number == 2


def test_retirement_request_keeps_exact_lease_alive_and_dry_run_is_read_only(case):
    assert _request(case.owner, dry_run=True).dry_run
    assert not controls.worker_retirement_requested(case.owner)
    assert _request(case.owner).state == "REQUESTED"
    assert controls.worker_retirement_requested(case.owner)
    assert not _request(case.owner, revision=1).changed
    case.now += timedelta(seconds=1)
    TaskWorkerLease.objects.filter(**case.owner.database_filters()).update(
        last_heartbeat_at=case.now
    )
    case.lease.refresh_from_db()
    assert case.lease.is_active and case.lease.stopped_at is None
    with transaction.atomic(), controls.maintenance_admission_barrier() as token:
        TaskWorkerLease.objects.select_for_update().get(**case.owner.database_filters())
        with pytest.raises(controls.MaintenanceAdmissionError, match="retiring"):
            controls.check_worker_retirement_admission(case.owner, barrier=token)


@pytest.mark.parametrize("operation", ["new_claim", "adopt"])
def test_retirement_raw_sql_fences_new_claim_and_destination_adoption(owned_case, operation):
    case = owned_case
    if operation == "new_claim":
        _request(case.owner)
        with pytest.raises(controls.MaintenanceAdmissionError, match="retiring"):
            _claim(case)
        assert not RayTaskCohortClaim.objects.exists()
        destination = case.owner
    else:
        original = _claim(case)
        case.lease.is_active = False
        case.lease.stopped_at = case.now
        case.lease.save(update_fields=["is_active", "stopped_at"])
        lease, destination = _lease("retiring-destination")
        lease.last_heartbeat_at = case.now
        lease.save(update_fields=["last_heartbeat_at"])
        _request(destination)
        with pytest.raises(storage.CohortClaimStorageError, match="persistence_refused"):
            with transaction.atomic(), controls.maintenance_admission_barrier():
                storage.adopt_cohort_claim(
                    destination,
                    original.claim_id,
                    expected_identity=original.facts.identity,
                    expected_revision=original.revision,
                    expected_owner=case.owner,
                    now=case.now,
                )
        retained = storage._record(RayTaskCohortClaim.objects.get(pk=original.claim_id))
        assert retained == original
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=case.task.pk).update(
            state="RUNNING",
            execution_generation=1,
            claimed_by_worker=destination.worker_id,
        )


def test_retirement_does_not_change_owned_completion_and_requires_independent_cleanup(owned_case):
    case = owned_case
    value = _started(case)
    _request(case.owner)
    with pytest.raises(controls.MaintenanceAdmissionError, match="ownership_remains"):
        _complete(case.owner)
    assert _complete_application(case, value).applied
    assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"
    with pytest.raises(controls.MaintenanceAdmissionError, match="cleanup_unconfirmed"):
        _complete(case.owner, independently_confirmed_cleanup=False)
    preview = _complete(case.owner, dry_run=True)
    assert preview.state == "RETIRED" and preview.dry_run
    case.lease.refresh_from_db()
    assert case.lease.is_active and RayWorkerRetirement.objects.count() == 1
    assert _complete(case.owner).state == "RETIRED"
    case.lease.refresh_from_db()
    assert not case.lease.is_active and case.lease.stopped_at == case.now
    assert list(
        RayWorkerRetirement.objects.order_by("revision").values_list("state", flat=True)
    ) == ["REQUESTED", "RETIRED"]


@pytest.mark.parametrize("completed", [False, True])
def test_reused_worker_id_never_inherits_old_retirement(case, completed):
    _request(case.owner)
    if completed:
        _complete(case.owner)
    case.lease.delete()
    new_started = case.now + timedelta(seconds=1)
    case.now = new_started
    new = TaskWorkerLease.objects.create(
        worker_id=case.owner.worker_id,
        hostname=case.owner.hostname,
        pid=case.owner.pid,
        started_at=new_started,
        last_heartbeat_at=new_started,
        capability_schema_version=1,
        django_ray_version="0.5.0",
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    owner = replace(case.owner, started_at=new.started_at)
    assert controls.worker_retirement_requested(case.owner)
    assert not controls.worker_retirement_requested(owner)
    with pytest.raises(controls.MaintenanceAdmissionError, match="identity_changed"):
        _request(case.owner, revision=2)
    with transaction.atomic(), controls.maintenance_admission_barrier() as token:
        TaskWorkerLease.objects.select_for_update().get(**owner.database_filters())
        controls.check_worker_retirement_admission(owner, barrier=token)
    assert _request(owner).revision == 1


@pytest.mark.parametrize("kind", ["quarantine", "retirement"])
def test_audit_update_delete_cannot_remove_live_control(case, kind):
    if kind == "quarantine":
        _quarantine(case.task)
        query, parent = RayTaskQuarantine.objects.all(), case.task
    else:
        _request(case.owner)
        query, parent = RayWorkerRetirement.objects.all(), case.lease
    with pytest.raises(DatabaseError), transaction.atomic():
        query.update(reason="replacement")
    with pytest.raises(DatabaseError), transaction.atomic():
        query.delete()
    if kind == "quarantine":
        with pytest.raises(DatabaseError), transaction.atomic():
            parent.delete()


def test_requested_lease_retention_preserves_unverified_history(case):
    _request(case.owner)
    case.lease.delete()
    assert list(RayWorkerRetirement.objects.values_list("state", flat=True)) == ["REQUESTED"]
    for operation in (_request, _complete):
        with pytest.raises(controls.MaintenanceAdmissionError, match="identity_changed"):
            operation(case.owner)
    assert controls.worker_retirement_requested(case.owner)
    with transaction.atomic(), controls.maintenance_admission_barrier() as token:
        with pytest.raises(controls.MaintenanceAdmissionError, match="retiring"):
            controls.check_worker_retirement_admission(case.owner, barrier=token)


def test_released_controls_preserve_audit_without_preventing_parent_retention(case):
    _quarantine(case.task)
    _quarantine(case.task, revision=1, quarantined=False)
    _request(case.owner)
    _complete(case.owner)
    case.task.delete()
    case.lease.delete()
    assert RayTaskQuarantine.objects.count() == RayWorkerRetirement.objects.count() == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"authorized": False},
        {"authorized": 1},
        {"expected_revision": True},
        {"expected_revision": -1},
        {"dry_run": 1},
        {"actor": "with spaces"},
        {"reason": ""},
    ],
)
def test_control_validation_refuses_without_audit(case, changes):
    with pytest.raises(controls.MaintenanceAdmissionError):
        controls.set_task_quarantine(
            _identity(case.task),
            **(
                {
                    **OPERATOR,
                    "expected_revision": 0,
                    "quarantined": True,
                }
                | changes
            ),
        )
    assert not RayTaskQuarantine.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"independently_confirmed_cleanup": 1},
        {"cleanup_evidence_digest": "sha256:bad"},
        {"cleanup_confirmed_at": None},
    ],
)
def test_cleanup_evidence_shape_never_becomes_retirement(case, changes):
    _request(case.owner)
    with pytest.raises(controls.MaintenanceAdmissionError):
        _complete(case.owner, **changes)
    assert RayWorkerRetirement.objects.count() == 1


@pytest.mark.parametrize("offset", [-1, 1])
def test_cleanup_observation_must_follow_request_and_not_be_future(case, offset):
    _request(case.owner)
    with pytest.raises(controls.MaintenanceAdmissionError, match="clock_regression"):
        _complete(case.owner, cleanup_confirmed_at=case.now + timedelta(seconds=offset))
    assert RayWorkerRetirement.objects.count() == 1


def test_controls_and_authoritative_helpers_require_their_transaction_contract(case):
    with (
        transaction.atomic(),
        pytest.raises(controls.MaintenanceAdmissionError, match="transaction_open"),
    ):
        _quarantine(case.task)
    with pytest.raises(controls.MaintenanceAdmissionError, match="barrier_required"):
        controls.task_quarantine_retry_allowed(case.task)
    with pytest.raises(controls.MaintenanceAdmissionError, match="barrier_required"):
        controls.check_worker_retirement_admission(case.owner)
    connection.set_autocommit(False)
    try:
        with pytest.raises(controls.MaintenanceAdmissionError, match="transaction_open"):
            _request(case.owner)
    finally:
        connection.rollback()
        connection.set_autocommit(True)


def test_finalization_failure_rolls_back_audit_and_lease(case, monkeypatch):
    _request(case.owner)
    original = TaskWorkerLease.save

    def failed(self, *args, **kwargs):
        if kwargs.get("update_fields") == ("is_active", "stopped_at"):
            raise RuntimeError("owned finalization failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TaskWorkerLease, "save", failed)
    with pytest.raises(RuntimeError, match="owned finalization failed"):
        _complete(case.owner)
    assert RayWorkerRetirement.objects.count() == 1
    case.lease.refresh_from_db()
    assert case.lease.is_active
    monkeypatch.setattr(TaskWorkerLease, "save", original)


def test_controls_cannot_reverse_with_retained_audit(case):
    migration = importlib.import_module("django_ray.migrations.0032_maintenance_controls")
    _quarantine(case.task)
    # PostgreSQL LOCK TABLE requires this outer transaction as well.
    editor = connection.schema_editor(atomic=False)
    with pytest.raises(RuntimeError, match="retained history"), transaction.atomic():
        migration._remove(apps, editor)


@pytest.mark.parametrize("kind", ["quarantine", "retirement"])
def test_fresh_clock_after_parent_lock_cannot_regress_or_backdate_audit(case, kind, monkeypatch):
    values = iter((case.now, case.now - timedelta(microseconds=1)))
    with monkeypatch.context() as patch:
        patch.setattr(controls, "_clock", lambda: next(values))
        with pytest.raises(controls.MaintenanceAdmissionError, match="clock_regression"):
            if kind == "quarantine":
                _quarantine(case.task)
            else:
                _request(case.owner)
    assert not RayTaskQuarantine.objects.exists()
    assert not RayWorkerRetirement.objects.exists()


def test_retirement_completion_requires_exact_request_revision(case):
    with pytest.raises(controls.MaintenanceAdmissionError, match="revision_changed"):
        _complete(case.owner, expected_revision=0)
    _request(case.owner)
    with pytest.raises(controls.MaintenanceAdmissionError, match="revision_changed"):
        _complete(case.owner, expected_revision=0)
    _complete(case.owner)
    with pytest.raises(controls.MaintenanceAdmissionError, match="revision_changed"):
        _complete(case.owner, expected_revision=2)
    assert RayWorkerRetirement.objects.count() == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 2},
        {"state": "RELEASED"},
        {"task_id": "another-task"},
        {"attempt_number": 2},
        {"execution_generation": 1},
        {"actor": "has spaces"},
    ],
)
def test_raw_quarantine_requires_exact_current_identity_and_first_audit_shape(case, changes):
    values = {
        "task_execution_pk": case.task.pk,
        "task_id": case.task.task_id,
        "attempt_number": 1,
        "execution_generation": 0,
        "revision": 1,
        "state": "QUARANTINED",
        "actor": "operator",
        "reason": "maintenance",
        "created_at": case.now,
    } | changes
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskQuarantine.objects.create(**values)
    assert not RayTaskQuarantine.objects.exists()


def test_raw_audit_cannot_append_same_state_or_backdated_release(case):
    _quarantine(case.task)
    original = RayTaskQuarantine.objects.get()
    for state, created in (
        ("QUARANTINED", case.now),
        ("RELEASED", case.now - timedelta(seconds=1)),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            RayTaskQuarantine.objects.create(
                task_execution_pk=original.task_execution_pk,
                task_id=original.task_id,
                attempt_number=original.attempt_number,
                execution_generation=original.execution_generation,
                revision=2,
                state=state,
                created_at=created,
                actor="operator",
                reason="maintenance",
            )
    assert RayTaskQuarantine.objects.count() == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 2, "state": "RETIRED", "cleanup_evidence_digest": DIGEST},
        {"hostname": "another-host"},
        {"pid": 101},
        {"actor": "has spaces"},
        {"cleanup_evidence_digest": DIGEST},
    ],
)
def test_raw_retirement_requires_live_exact_incarnation_and_request_shape(case, changes):
    values = (
        case.owner.database_filters()
        | {
            "revision": 1,
            "state": "REQUESTED",
            "actor": "operator",
            "reason": "maintenance",
            "created_at": case.now,
        }
        | changes
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        RayWorkerRetirement.objects.create(**values)
    assert not RayWorkerRetirement.objects.exists()


def test_missing_parent_and_invalid_identities_cannot_append_controls(case):
    case.task.delete()
    with pytest.raises(controls.MaintenanceAdmissionError, match="identity_changed"):
        controls.set_task_quarantine(
            ExecutionIdentity(100000, "gone", 1, 0),
            quarantined=True,
            expected_revision=0,
            **OPERATOR,
        )
    for identity in (None, object(), replace(case.owner, pid=True)):
        with pytest.raises(controls.MaintenanceAdmissionError, match="invalid"):
            _request(identity)
    with pytest.raises(controls.MaintenanceAdmissionError, match="invalid"):
        controls.set_task_quarantine(None, quarantined=True, expected_revision=0, **OPERATOR)
    with pytest.raises(controls.MaintenanceAdmissionError, match="invalid"):
        controls.set_task_quarantine(
            ExecutionIdentity(100000, "gone", 1, 0), quarantined=1, expected_revision=0, **OPERATOR
        )


@pytest.mark.postgresql
def test_postgresql_retirement_cas_has_one_winner(postgres_case):
    case = postgres_case
    ready = Barrier(2)

    def request():
        close_old_connections()
        try:
            ready.wait(timeout=5)
            try:
                return _request(case.owner).state
            except controls.MaintenanceAdmissionError as error:
                return error.reason.value
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [
            "REQUESTED",
            "revision_changed",
        ]


def _assert_postgresql_waiting(pid):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                [pid],
            )
            if cursor.fetchone()[0]:
                return
        sleep(0.01)
    pytest.fail("contending maintenance operation never waited for the owned lock")


@pytest.mark.postgresql
def test_postgresql_claim_winner_makes_waiting_quarantine_identity_stale(
    postgres_owned_case, monkeypatch
):
    case = postgres_owned_case
    locked, release, attempting = Event(), Event(), Event()
    quarantine_pid = []
    original = storage._lock_task

    def locked_task(*args, **kwargs):
        task = original(*args, **kwargs)
        locked.set()
        assert release.wait(timeout=5)
        return task

    monkeypatch.setattr(storage, "_lock_task", locked_task)

    def claim():
        close_old_connections()
        try:
            return _claim(case)
        finally:
            close_old_connections()

    def quarantine():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout='8s'")
                cursor.execute("SELECT pg_backend_pid()")
                quarantine_pid.append(cursor.fetchone()[0])
                attempting.set()
            try:
                _quarantine(case.task)
            except controls.MaintenanceAdmissionError as error:
                return error.reason.value
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = pool.submit(claim)
        try:
            assert locked.wait(timeout=5)
            pending = pool.submit(quarantine)
            assert attempting.wait(timeout=5)
            _assert_postgresql_waiting(quarantine_pid[0])
        finally:
            release.set()
        record = claimed.result(timeout=10)
        assert pending.result(timeout=10) == "identity_changed"
    assert not RayTaskQuarantine.objects.exists()
    case.task.refresh_from_db()
    assert case.task.state == "RUNNING" and case.task.execution_generation == 1
    assert storage._record(RayTaskCohortClaim.objects.get()) == record


@pytest.mark.postgresql
def test_postgresql_quarantine_winner_refuses_waiting_current_claim_with_fresh_policy(
    postgres_owned_case, monkeypatch
):
    case = postgres_owned_case
    locked, release, attempting = Event(), Event(), Event()
    claimant_pid = []
    original = RayTaskQuarantine.save

    def retained_audit(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        locked.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(RayTaskQuarantine, "save", retained_audit)

    def quarantine():
        close_old_connections()
        try:
            return _quarantine(case.task)
        finally:
            close_old_connections()

    def claim():
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout='8s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    claimant_pid.append(cursor.fetchone()[0])
                    attempting.set()
                return _claim(case)
        except storage.CohortClaimStorageError as error:
            return error.reason.value
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        control = pool.submit(quarantine)
        try:
            assert locked.wait(timeout=5)
            pending = pool.submit(claim)
            assert attempting.wait(timeout=5)
            _assert_postgresql_waiting(claimant_pid[0])
        finally:
            release.set()
        assert control.result(timeout=10).state == "QUARANTINED"
        assert pending.result(timeout=10) == "execution_changed"
    case.task.refresh_from_db()
    assert case.task.state == "QUEUED" and case.task.execution_generation == 0
    assert not RayTaskCohortClaim.objects.exists()


def test_failed_control_audit_insert_is_fixed_and_rolls_back(case, monkeypatch):
    original = RayTaskQuarantine.save

    def failed(*_args, **_kwargs):
        raise DatabaseError("secret provider connection details")

    with monkeypatch.context() as patch:
        patch.setattr(RayTaskQuarantine, "save", failed)
        with pytest.raises(
            controls.MaintenanceAdmissionError, match="persistence_refused"
        ) as error:
            _quarantine(case.task)
        assert "secret" not in str(error.value)
    assert RayTaskQuarantine.save is original
    assert not RayTaskQuarantine.objects.exists()


@pytest.mark.parametrize("kind", ["quarantine", "retirement"])
@pytest.mark.parametrize("label", ["actor", "reason"])
@pytest.mark.parametrize("payload", ["actor\x00" + "x" * 1000, b"operator"])
def test_sqlite_raw_audit_labels_reject_nul_suffix_and_blob(case_data, kind, label, payload):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite storage-class and embedded-NUL behavior")
    if kind == "quarantine":
        fields = {
            "task_execution_pk": case_data.task.pk,
            "task_id": case_data.task.task_id,
            "attempt_number": 1,
            "execution_generation": 0,
            "revision": 1,
            "state": "QUARANTINED",
            "actor": "operator",
            "reason": "maintenance",
            "created_at": case_data.now,
        }
        model = RayTaskQuarantine
    else:
        fields = case_data.owner.database_filters() | {
            "revision": 1,
            "state": "REQUESTED",
            "actor": "operator",
            "reason": "maintenance",
            "created_at": case_data.now,
        }
        model = RayWorkerRetirement
    fields[label] = payload
    # Raw parameters retain the SQLite BLOB storage class that CharField would
    # otherwise stringify; the NUL case proves the hidden suffix cannot pass.
    quoted = ",".join(connection.ops.quote_name(field) for field in fields)
    placeholders = ",".join("%s" for _ in fields)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {connection.ops.quote_name(model._meta.db_table)} ({quoted}) VALUES ({placeholders})",
            list(fields.values()),
        )
    assert not model.objects.exists()


@pytest.mark.parametrize("digest", [DIGEST + "\x00" + "x" * 1000, DIGEST.encode()])
def test_sqlite_raw_retirement_receipt_digest_rejects_nul_suffix_and_blob(case_data, digest):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite storage-class and embedded-NUL behavior")
    _request(case_data.owner)
    fields = case_data.owner.database_filters() | {
        "revision": 2,
        "state": "RETIRED",
        "actor": "operator",
        "reason": "maintenance",
        "created_at": case_data.now,
        "cleanup_confirmed_at": case_data.now,
        "cleanup_evidence_digest": digest,
    }
    quoted = ",".join(connection.ops.quote_name(field) for field in fields)
    placeholders = ",".join("%s" for _ in fields)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO django_ray_rayworkerretirement ({quoted}) VALUES ({placeholders})",
            list(fields.values()),
        )
    assert RayWorkerRetirement.objects.count() == 1
