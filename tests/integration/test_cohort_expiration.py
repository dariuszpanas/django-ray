"""Declared queued expiry archives truthful attempts without executing task input."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from time import monotonic, sleep

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet

from django_ray import lifecycle, maintenance
from django_ray.execution_codec import ExecutionIdentity
from django_ray.models import (
    RayTaskCohortClaim,
    RayTaskExecution,
    RayTaskQuarantine,
    RayWorkerRetirement,
    TaskAttempt,
    TaskWorkerLease,
)
from django_ray.runner import cohort_expiration as expiration
from django_ray.runner.leasing import get_lease_duration
from django_ray.target.cohort_claim import CohortManagerRuntime
from django_ray.target.cohort_intent import CohortSelectionPolicy
from tests.integration.test_cohort_claim_storage import _lease
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import _apply, _result, _started
from tests.integration.test_cohort_selection import _alias, _clone
from tests.unit.test_cohort_claim import PYTHON

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


@pytest.fixture(autouse=True)
def isolated_expiration_controls(isolated_sqlite_ledger_maintenance):
    """Release audited fixture quarantine, then use stopped parent-first cleanup.

    Protected intent rows require the same isolated FK-disabled raw parent
    cleanup as the ledger fixture. Production lifecycle/SQL fences stay intact.
    """
    yield
    if connection.vendor == "sqlite":
        for task in RayTaskExecution.objects.filter(
            pk__in=RayTaskQuarantine.objects.values("task_execution_pk")
        ):
            latest = RayTaskQuarantine.objects.filter(task_execution_pk=task.pk).latest("revision")
            if latest.state == "QUARANTINED":
                maintenance.set_task_quarantine(
                    ExecutionIdentity(
                        task.pk, task.task_id, task.attempt_number, task.execution_generation
                    ),
                    quarantined=False,
                    expected_revision=latest.revision,
                    actor="isolated-test-teardown",
                    reason="release-fixture",
                    authorized=True,
                )
        with connection.constraint_checks_disabled(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM django_ray_raytaskexecution WHERE id IN (SELECT task_execution_pk FROM django_ray_raytaskquarantine)"
            )
        TaskWorkerLease.objects.filter(
            worker_id__in=RayWorkerRetirement.objects.values("worker_id")
        ).delete()


def _due(case, task=None, *, delta=0):
    task = case.task if task is None else task
    RayTaskExecution.objects.filter(pk=task.pk).update(
        queue_deadline_at=case.now + timedelta(seconds=delta),
        queue_timeout_seconds=60,
    )
    task.refresh_from_db()
    return task


def _expire(case, **kwargs):
    case.monkeypatch.setattr(expiration, "_clock", lambda: case.now)
    return expiration.expire_cohort_queued_tasks(
        case.owner,
        **(
            {
                "aliases": (_alias(case),),
                "manager_runtime": CohortManagerRuntime("0.5.0", PYTHON),
                "now": case.now,
            }
            | kwargs
        ),
    )


def test_due_current_cohort_is_expired_with_same_attempt_and_no_claim(case):
    task = _due(case)
    identity = (task.attempt_number, task.execution_generation)
    assert _expire(case) == (task.pk,)
    task.refresh_from_db()
    attempt = TaskAttempt.objects.get(execution=task)
    assert task.state == attempt.state == "EXPIRED"
    assert (task.attempt_number, task.execution_generation) == identity
    assert attempt.attempt_number == identity[0]
    assert task.finished_at == attempt.finished_at == case.now
    assert task.error_message == attempt.error_message == lifecycle.QUEUE_EXPIRED_ERROR
    assert attempt.execution_protocol_version == 3
    assert (
        task.managed_with_django_ray_version == attempt.managed_with_django_ray_version == "0.5.0"
    )
    assert not RayTaskCohortClaim.objects.exists()
    assert _expire(case) == ()


@pytest.mark.parametrize("mismatch", ["package", "alias", "configuration", "selection", "queue"])
def test_other_cohort_earlier_deadline_is_filtered_before_limit(case, mismatch):
    changes = {
        "package": {"package_version": "0.4.0"},
        "alias": {"backend_alias": "other"},
        "configuration": {"configuration_digest": "sha256:" + "d" * 64},
        "selection": {"selection_policy": CohortSelectionPolicy.JOBS_ONLY},
        "queue": {},
    }
    other = _clone(
        case,
        name="earlier",
        intent=replace(case.intent, **changes[mismatch]),
        queue="other" if mismatch == "queue" else "default",
    )
    _due(case, other, delta=-1)
    _due(case)
    assert _expire(case, limit=1) == (case.task.pk,)
    other.refresh_from_db()
    assert other.state == "QUEUED"
    assert not TaskAttempt.objects.filter(execution=other).exists()


@pytest.mark.parametrize("state", ["RUNNING", "CANCELLING", "SUCCEEDED", "FAILED"])
def test_only_queued_tasks_are_expired(case, state):
    value = _started(case)
    if state in {"SUCCEEDED", "FAILED"}:
        _apply(value, case, _result(value, success=state == "SUCCEEDED"))
    elif state == "CANCELLING":
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state=state)
    _due(case)
    assert _expire(case) == ()
    assert not TaskAttempt.objects.exists()


@pytest.mark.parametrize("mode", ["absent", "future", "paused", "quarantined"])
def test_only_actual_elapsed_deadline_can_expire_work(case, mode, monkeypatch):
    if mode != "absent":
        _due(case, delta=1)
    if mode == "paused":
        maintenance.replace_maintenance_policy(
            (),
            pause_enqueues=True,
            pause_claims=True,
            expected_revision=1,
            actor="test",
            reason="paused",
            authorized=True,
        )
    if mode == "quarantined":
        monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
        maintenance.set_task_quarantine(
            ExecutionIdentity(case.task.pk, case.task.task_id, 1, 0),
            quarantined=True,
            expected_revision=0,
            actor="test",
            reason="quarantine",
            authorized=True,
        )
    assert _expire(case) == ()
    assert RayTaskExecution.objects.get().state == "QUEUED"


@pytest.mark.parametrize("quarantined", [False, True])
def test_pauses_and_quarantine_allow_truthful_same_generation_expiry(
    case, quarantined, monkeypatch
):
    _due(case)
    maintenance.replace_maintenance_policy(
        (),
        pause_enqueues=True,
        pause_claims=True,
        expected_revision=1,
        actor="test",
        reason="paused",
        authorized=True,
    )
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    if quarantined:
        maintenance.set_task_quarantine(
            ExecutionIdentity(case.task.pk, case.task.task_id, 1, 0),
            quarantined=True,
            expected_revision=0,
            actor="test",
            reason="quarantine",
            authorized=True,
        )
    assert _expire(case) == (case.task.pk,)


def test_retiring_manager_cannot_take_unowned_queue_management(case, monkeypatch):
    _due(case)
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    maintenance.request_worker_retirement(
        case.owner,
        expected_revision=0,
        actor="test",
        reason="retire",
        authorized=True,
    )
    with pytest.raises(maintenance.MaintenanceAdmissionError):
        _expire(case)
    assert not TaskAttempt.objects.exists()


@pytest.mark.parametrize("mode", ["inactive", "expired", "future", "recreated", "package", "range"])
def test_only_live_exact_current_package_protocol_lease_can_expire(case, mode):
    _due(case)
    if mode == "inactive":
        TaskWorkerLease.objects.filter(pk=case.lease.pk).update(
            is_active=False, stopped_at=case.now
        )
    elif mode in {"expired", "future"}:
        heartbeat = (
            case.now - 2 * get_lease_duration()
            if mode == "expired"
            else case.now + timedelta(seconds=1)
        )
        TaskWorkerLease.objects.filter(pk=case.lease.pk).update(last_heartbeat_at=heartbeat)
    elif mode == "recreated":
        case.lease.delete()
        _lease(case.owner.worker_id, started=case.now)
    elif mode == "package":
        with pytest.raises(expiration.CohortExpirationError):
            _expire(case, manager_runtime=CohortManagerRuntime("0.5.1", PYTHON))
        return
    else:
        case.lease.delete()
        # Activation rejects an incompatible active incarnation at insertion.
        # The old identity also cannot expire work after its lease disappears.
        with pytest.raises(IntegrityError), transaction.atomic():
            TaskWorkerLease.objects.create(
                worker_id=case.owner.worker_id,
                hostname=case.owner.hostname,
                pid=case.owner.pid,
                started_at=case.owner.started_at,
                last_heartbeat_at=case.now,
                django_ray_version="0.5.0",
                capability_schema_version=1,
                legacy_admission_token=None,
                min_supported_execution_protocol_version=1,
                max_supported_execution_protocol_version=3,
            )
    with pytest.raises(RuntimeError):
        _expire(case)
    assert not TaskAttempt.objects.exists()


def test_expiration_never_loads_input_or_runtime_environment(case, monkeypatch):
    _due(case)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(
        callable_path="does.not.exist",
        args_json="not-json",
        runtime_env_json="not-json",
    )
    monkeypatch.setattr(
        "django_ray.input_storage.load_task_input", lambda *a, **k: pytest.fail("Input hydrated")
    )
    monkeypatch.setattr(
        lifecycle, "runtime_env_for_execution", lambda *a, **k: pytest.fail("RuntimeEnv loaded")
    )
    assert _expire(case) == (case.task.pk,)


@pytest.mark.parametrize("changed", ["deadline", "attempt", "generation", "state"])
def test_candidate_change_after_scan_is_not_expired(case, monkeypatch, changed):
    _due(case)
    original = QuerySet.select_for_update
    did_change = False

    def change(query, *args, **kwargs):
        nonlocal did_change
        if query.model is RayTaskExecution and not did_change:
            did_change = True
            RayTaskExecution.objects.filter(pk=case.task.pk).update(
                **{
                    "deadline": {"queue_deadline_at": case.now + timedelta(seconds=10)},
                    "attempt": {"attempt_number": 2},
                    "generation": {"execution_generation": 1},
                    "state": {"state": "CANCELLED"},
                }[changed]
            )
        return original(query, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", change)
    assert _expire(case) == ()
    assert not TaskAttempt.objects.exists()


@pytest.mark.parametrize("failure", ["archive", "clock", "lease-expiry"])
def test_archive_or_fresh_ownership_failure_rolls_back_entire_batch(case, monkeypatch, failure):
    _due(case)
    original = lifecycle._record_attempt

    def fail(current):
        original(current)
        if failure == "archive":
            raise RuntimeError("archive-failed")
        case.now += -timedelta(seconds=1) if failure == "clock" else 2 * get_lease_duration()

    monkeypatch.setattr(lifecycle, "_record_attempt", fail)
    with pytest.raises(RuntimeError):
        _expire(case)
    assert RayTaskExecution.objects.get().state == "QUEUED"
    assert not TaskAttempt.objects.exists()


@pytest.mark.parametrize("limit", [False, 0, 101, 1.0])
def test_finite_integer_limit_required(case, limit):
    with pytest.raises(expiration.CohortExpirationError):
        _expire(case, limit=limit)


def test_outer_transaction_is_refused(case):
    with transaction.atomic(), pytest.raises(expiration.CohortExpirationError):
        _expire(case)


@pytest.mark.postgresql
@pytest.mark.parametrize("ledger_database", ["postgresql"], indirect=True)
def test_postgresql_locked_earlier_task_is_skipped_while_next_deadline_expires(case):
    earlier = _due(case, _clone(case, name="locked-earlier"), delta=-1)
    _due(case)
    locked, release = Event(), Event()
    holder_pid = []

    def hold():
        connection.close()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '2s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    holder_pid.append(cursor.fetchone()[0])
                RayTaskExecution.objects.select_for_update().get(pk=earlier.pk)
                locked.set()
                if not release.wait(8):
                    raise TimeoutError("Expiry did not finish while earlier task stayed locked")
        finally:
            connection.close()

    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        original_timeout = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('statement_timeout', '2s', false), pg_backend_pid()")
        expiration_pid = cursor.fetchone()[1]
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = executor.submit(hold)
            try:
                assert locked.wait(5)
                assert holder_pid[0] != expiration_pid
                assert _expire(case, limit=2) == (case.task.pk,)
                assert not holder.done() and not release.is_set()
                earlier.refresh_from_db()
                assert earlier.state == "QUEUED"
                assert not TaskAttempt.objects.filter(execution=earlier).exists()
            finally:
                release.set()
                holder.result(timeout=3)
    finally:
        release.set()
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, false)", [original_timeout])


@pytest.mark.postgresql
@pytest.mark.parametrize("ledger_database", ["postgresql"], indirect=True)
def test_postgresql_waiting_lease_is_rechecked_with_fresh_clock_before_expiry(case, monkeypatch):
    _due(case)
    monkeypatch.setattr(expiration, "_clock", lambda: case.now)
    connected = Event()
    waiter_pid = []
    starting_time = case.now

    def expire_after_wait():
        connection.close()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '8s'")
                cursor.execute("SELECT pg_backend_pid()")
                waiter_pid.append(cursor.fetchone()[0])
            connected.set()
            try:
                expiration.expire_cohort_queued_tasks(
                    case.owner,
                    aliases=(_alias(case),),
                    manager_runtime=CohortManagerRuntime("0.5.0", PYTHON),
                    now=starting_time,
                )
            except expiration.CohortExpirationError:
                return "clock-refused"
            except Exception as error:
                from django_ray.target.cohort_claim_storage import (
                    CohortClaimStorageError,
                    CohortClaimStorageReason,
                )

                if (
                    isinstance(error, CohortClaimStorageError)
                    and error.reason is CohortClaimStorageReason.LEASE_UNAVAILABLE
                ):
                    return "lease-refused"
                raise
            return "unexpected-expiry"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            TaskWorkerLease.objects.select_for_update().get(pk=case.lease.pk)
            result = executor.submit(expire_after_wait)
            assert connected.wait(5)
            deadline = monotonic() + 5
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                        [waiter_pid[0]],
                    )
                    waiting = cursor.fetchone()[0]
                if waiting:
                    break
                if monotonic() >= deadline:
                    pytest.fail("Expiry did not wait for its exact manager lease")
                sleep(0.01)
            assert not result.done()
            case.now += 2 * get_lease_duration()
        assert result.result(timeout=3) == "lease-refused"
    assert RayTaskExecution.objects.get().state == "QUEUED"
    assert not TaskAttempt.objects.exists()
