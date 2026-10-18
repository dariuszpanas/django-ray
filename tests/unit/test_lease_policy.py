"""Common-policy refusal, transition and observation regressions without Ray."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_ray import lease_policy
from django_ray.lease_policy import LeasePolicy, LeasePolicyError, set_lease_policy
from django_ray.models import RayTaskExecution, TaskWorkerLease, TaskWorkerLeasePolicy
from django_ray.protocol_status import annotate_execution_protocol_availability
from django_ray.runner.leasing import get_active_worker_count, mark_expired_leases_inactive
from django_ray.worker_readiness import check_worker_lease_readiness

pytestmark = pytest.mark.django_db(transaction=True)


def lease(**changes):
    fields = {
        "worker_id": "owned-worker",
        "queue_name": "other-queue",
        "hostname": "owned-host",
        "pid": 123,
        "started_at": timezone.now() - timedelta(seconds=20),
        "last_heartbeat_at": timezone.now() - timedelta(seconds=10),
        "is_active": True,
    }
    fields.update(changes)
    return TaskWorkerLease.objects.create(**fields)


def change(**overrides):
    kwargs = {
        "seconds": 90,
        "expected_seconds": 60,
        "expected_revision": 1,
        "all_lease_users_stopped": True,
    }
    kwargs.update(overrides)
    return set_lease_policy(**kwargs)


def test_migration_seeds_documented_default_and_readers_never_write():
    with CaptureQueriesContext(connection) as captured:
        assert lease_policy.read_lease_policy() == LeasePolicy(60, 1)
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in captured)
    TaskWorkerLeasePolicy.objects.all().delete()
    with pytest.raises(LeasePolicyError, match="lease_policy_invalid"):
        lease_policy.read_lease_policy()
    assert not TaskWorkerLeasePolicy.objects.exists()


def test_shorter_policy_cannot_retire_another_queue_or_claim_no_workers(settings):
    row = lease()
    before = list(TaskWorkerLease.objects.values())
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKER_LEASE_SECONDS": 6,
        "WORKER_HEARTBEAT_SECONDS": 2,
    }
    with pytest.raises(LeasePolicyError, match="lease_policy_mismatch"):
        mark_expired_leases_inactive()
    with pytest.raises(LeasePolicyError, match="lease_policy_mismatch"):
        get_active_worker_count()
    observed = check_worker_lease_readiness(queue=row.queue_name, hostname=row.hostname)
    assert observed.status == "error"
    assert observed.reason == "lease_policy_mismatch"
    assert list(TaskWorkerLease.objects.values()) == before


def test_matching_policy_preserves_live_peer_and_retires_only_expired():
    live = lease()
    stale = lease(worker_id="stale", last_heartbeat_at=timezone.now() - timedelta(seconds=61))
    assert mark_expired_leases_inactive() == 1
    live.refresh_from_db()
    stale.refresh_from_db()
    assert live.is_active and not stale.is_active


@pytest.mark.parametrize("operation", ["heartbeat", "claim", "reclaim", "release"])
def test_mismatch_refuses_worker_mutation_seams_without_changing_rows(settings, operation):
    from django_ray.management.commands.django_ray_worker import Command
    from django_ray.runner.leasing import release_lease

    command = Command(stdout=io.StringIO(), stderr=io.StringIO())
    command.execution_mode = "sync"
    command._create_lease("default")
    old = lease(last_heartbeat_at=timezone.now() - timedelta(seconds=61))
    RayTaskExecution.objects.create(
        task_id="policy-queued", callable_path="fixture.task", state="QUEUED"
    )
    running = RayTaskExecution.objects.create(
        task_id="policy-running",
        callable_path="fixture.task",
        state="RUNNING",
        claimed_by_worker=old.worker_id,
        ray_job_id="raysubmit_owned_policy_fixture",
    )
    before_leases = list(TaskWorkerLease.objects.order_by("pk").values())
    before_tasks = list(RayTaskExecution.objects.order_by("pk").values())
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKER_LEASE_SECONDS": 6,
        "WORKER_HEARTBEAT_SECONDS": 2,
    }
    if operation == "heartbeat":
        assert command._update_lease_heartbeat() is False
    elif operation == "reclaim":
        assert command._adopt_orphaned_ray_job_task(running, now=timezone.now()) is False
    else:
        with pytest.raises(LeasePolicyError, match="lease_policy_mismatch"):
            if operation == "claim":
                command.claim_and_process_tasks(["default"], concurrency=1)
            else:
                assert command.lease_identity is not None
                release_lease(command.lease_identity)
    assert list(TaskWorkerLease.objects.order_by("pk").values()) == before_leases
    assert list(RayTaskExecution.objects.order_by("pk").values()) == before_tasks


def test_transition_changes_only_authority_and_restores_connection_budget():
    lease(is_active=False)
    queued = RayTaskExecution.objects.create(callable_path="tests.tasks.noop")
    before = list(TaskWorkerLease.objects.values())
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        timeout = cursor.fetchone()[0]
    assert change() == LeasePolicy(90, 2)
    assert list(TaskWorkerLease.objects.values()) == before
    assert RayTaskExecution.objects.filter(pk=queued.pk).exists()
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        assert cursor.fetchone()[0] == timeout


@pytest.mark.parametrize("age", [1, 600])
def test_transition_refuses_active_incarnations_even_when_stale(age):
    lease(last_heartbeat_at=timezone.now() - timedelta(seconds=age))
    with pytest.raises(LeasePolicyError, match="lease_policy_active_leases"):
        change()
    assert lease_policy.read_lease_policy() == LeasePolicy(60, 1)


@pytest.mark.parametrize("state", ["RUNNING", "CANCELLING"])
def test_transition_does_not_infer_remote_quiescence_from_absent_leases(state):
    RayTaskExecution.objects.create(callable_path="tests.tasks.noop", state=state)
    with pytest.raises(LeasePolicyError, match="lease_policy_active_work"):
        change()


@pytest.mark.parametrize(
    "overrides",
    [
        {"all_lease_users_stopped": False},
        {"all_lease_users_stopped": 1},
        {"seconds": True},
        {"seconds": 0},
        {"seconds": 86401},
        {"expected_revision": True},
        {"expected_seconds": None},
    ],
)
def test_transition_arguments_are_exact_and_refuse_before_mutation(overrides):
    with pytest.raises(LeasePolicyError, match="lease_policy_transition_invalid"):
        change(**overrides)
    assert lease_policy.read_lease_policy() == LeasePolicy(60, 1)


@pytest.mark.parametrize("overrides", [{"expected_revision": 2}, {"expected_seconds": 90}])
def test_compare_and_swap_rejects_stale_intent(overrides):
    with pytest.raises(LeasePolicyError, match="lease_policy_changed"):
        change(**overrides)
    assert lease_policy.read_lease_policy() == LeasePolicy(60, 1)


def test_transition_refuses_nested_transaction():
    with (
        transaction.atomic(),
        pytest.raises(LeasePolicyError, match="lease_policy_transaction_required"),
    ):
        change()


def test_observation_discards_result_when_authority_revision_changes():
    with pytest.raises(LeasePolicyError, match="lease_policy_changed"):
        with lease_policy.lease_policy_observation():
            TaskWorkerLeasePolicy.objects.update(revision=2)


def test_lazy_protocol_annotation_reports_unknown_instead_of_false(settings):
    row = RayTaskExecution.objects.create(callable_path="tests.tasks.noop")
    query = annotate_execution_protocol_availability(
        RayTaskExecution.objects.filter(pk=row.pk)
    ).values_list("protocol_compatible_worker_available", flat=True)
    # The QuerySet was constructed under the old matching authority.
    change()
    assert list(query) == [None]
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, "WORKER_LEASE_SECONDS": 90}
    fresh = annotate_execution_protocol_availability(
        RayTaskExecution.objects.filter(pk=row.pk)
    ).values_list("protocol_compatible_worker_available", flat=True)
    assert list(fresh) == [False]


def test_metric_policy_unavailable_does_not_emit_zero_health_counts(settings):
    from django_ray.metrics import render_prometheus_metrics

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKER_LEASE_SECONDS": 6,
        "WORKER_HEARTBEAT_SECONDS": 2,
    }
    rendered = render_prometheus_metrics()
    assert "django_ray_worker_lease_policy_available 0" in rendered
    assert not any(line.startswith("django_ray_worker_leases{") for line in rendered.splitlines())


def test_command_read_only_status_and_explicit_change():
    output = io.StringIO()
    call_command("django_ray_lease_policy", stdout=output)
    assert json.loads(output.getvalue())["status"] == "matched"
    output = io.StringIO()
    call_command(
        "django_ray_lease_policy",
        "--set-seconds",
        "90",
        "--expected-seconds",
        "60",
        "--expected-revision",
        "1",
        "--all-lease-users-stopped",
        stdout=output,
    )
    assert json.loads(output.getvalue())["revision"] == 2
    with pytest.raises(CommandError, match="lease_policy_mismatch"):
        call_command("django_ray_lease_policy", stdout=io.StringIO())


def test_database_errors_do_not_echo_private_detail(monkeypatch):
    def failure(**kwargs):
        raise OperationalError("private-database-host-secret")

    monkeypatch.setattr(lease_policy, "policy_rows", failure)
    with pytest.raises(LeasePolicyError) as captured:
        lease_policy.read_lease_policy()
    assert str(captured.value) == "lease_policy_unavailable"


def test_sqlite_guard_writes_before_reading_policy():
    with CaptureQueriesContext(connection) as captured:
        with lease_policy.lease_policy_guard() as policy:
            assert policy == LeasePolicy(60, 1)
    statements = [query["sql"].strip().upper() for query in captured]
    update = next(i for i, sql in enumerate(statements) if sql.startswith("UPDATE"))
    read = next(i for i, sql in enumerate(statements) if sql.startswith("SELECT"))
    assert update < read


@pytest.mark.parametrize("reason", ["private-database-host-secret", None, True])
def test_policy_diagnostics_reject_unknown_or_nontext_values(reason):
    with pytest.raises(ValueError, match="^Unsupported lease policy diagnostic$"):
        LeasePolicyError(reason)


@pytest.mark.parametrize("seconds", [True, None, "60", 0, 86401])
def test_configured_authority_refuses_invalid_bounds_before_sql(monkeypatch, seconds):
    monkeypatch.setattr(lease_policy, "get_settings", lambda: {"WORKER_LEASE_SECONDS": seconds})
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(LeasePolicyError, match="^lease_policy_invalid$"):
            lease_policy.configured_lease_seconds()
    assert not captured.captured_queries


def test_revision_predicate_never_accepts_a_different_authority():
    assert lease_policy.policy_rows(revision=1).exists()
    assert not lease_policy.policy_rows(revision=2).exists()


def test_guard_requires_transaction_and_never_bootstraps_missing_authority():
    with pytest.raises(LeasePolicyError, match="^lease_policy_transaction_required$"):
        lease_policy.lock_lease_policy()
    TaskWorkerLeasePolicy.objects.all().delete()
    with pytest.raises(LeasePolicyError, match="^lease_policy_invalid$"):
        with lease_policy.lease_policy_guard():
            pytest.fail("missing authority admitted a mutation")
    assert not TaskWorkerLeasePolicy.objects.exists()


def test_unsupported_backend_cannot_observe_or_lock_authority(monkeypatch):
    monkeypatch.setattr(connection, "vendor", "unsupported")
    with transaction.atomic(), CaptureQueriesContext(connection) as captured:
        for operation in (lease_policy.read_lease_policy, lease_policy.lock_lease_policy):
            with pytest.raises(LeasePolicyError, match="^lease_policy_unavailable$"):
                operation()
    assert not captured.captured_queries


def test_same_value_transition_preserves_revision_and_timestamp():
    before = list(TaskWorkerLeasePolicy.objects.values())
    assert change(seconds=60) == LeasePolicy(60, 1)
    assert list(TaskWorkerLeasePolicy.objects.values()) == before


def test_exhausted_revision_refuses_change_without_wrapping():
    TaskWorkerLeasePolicy.objects.update(revision=lease_policy.MAX_REVISION)
    before = list(TaskWorkerLeasePolicy.objects.values())
    with pytest.raises(LeasePolicyError, match="^lease_policy_revision_exhausted$"):
        change(expected_revision=lease_policy.MAX_REVISION)
    assert list(TaskWorkerLeasePolicy.objects.values()) == before


def test_transition_requires_heartbeat_to_fit_new_duration():
    before = list(TaskWorkerLeasePolicy.objects.values())
    with pytest.raises(LeasePolicyError, match="^lease_policy_invalid$"):
        change(seconds=15)
    assert list(TaskWorkerLeasePolicy.objects.values()) == before


@pytest.mark.parametrize("failure", ["busy", "stale_cas", "write_failed"])
def test_failed_transition_preserves_authority_and_connection_budget(monkeypatch, failure):
    from django.db.models.query import QuerySet

    original = QuerySet.update
    before = list(TaskWorkerLeasePolicy.objects.values())
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        timeout = cursor.fetchone()[0]

    def update(queryset, **changes):
        if queryset.model is TaskWorkerLeasePolicy:
            if failure == "busy" or (failure == "write_failed" and "updated_at" in changes):
                raise OperationalError("private-database-host-secret")
            if failure == "stale_cas" and "updated_at" in changes:
                return 0
        return original(queryset, **changes)

    monkeypatch.setattr(QuerySet, "update", update)
    expected = {
        "busy": "lease_policy_busy",
        "stale_cas": "lease_policy_changed",
        "write_failed": "lease_policy_unavailable",
    }[failure]
    with pytest.raises(LeasePolicyError, match=f"^{expected}$"):
        change()
    assert list(TaskWorkerLeasePolicy.objects.values()) == before
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        assert cursor.fetchone()[0] == timeout


@pytest.mark.parametrize("exclusive", [False, True])
@pytest.mark.parametrize("outcome", ["locked", "missing", "busy"])
def test_postgresql_guard_is_nowait_and_reads_only_after_lock(monkeypatch, exclusive, outcome):
    # This driver-boundary contract checks SQL and failure containment only.
    # Real shared/exclusive contention belongs to the hosted PostgreSQL tests.
    events = []

    class Cursor:
        def execute(self, sql, params):
            events.append((sql, params))
            if outcome == "busy":
                raise OperationalError("private-database-host-secret")

        def fetchone(self):
            return None if outcome == "missing" else (1,)

    @contextmanager
    def cursor():
        yield Cursor()

    def read(*, using, matching):
        events.append((using, matching))
        return LeasePolicy(60, 1)

    selected = SimpleNamespace(
        vendor="postgresql",
        in_atomic_block=True,
        cursor=cursor,
        ops=SimpleNamespace(quote_name=lambda value: f'"{value}"'),
    )
    monkeypatch.setattr(lease_policy, "connections", {"owned": selected})
    monkeypatch.setattr(lease_policy, "read_lease_policy", read)
    if outcome == "locked":
        assert lease_policy.lock_lease_policy(using="owned", exclusive=exclusive) == LeasePolicy(
            60, 1
        )
        assert events[1:] == [("owned", not exclusive)]
    else:
        expected = "lease_policy_invalid" if outcome == "missing" else "lease_policy_busy"
        with pytest.raises(LeasePolicyError, match=f"^{expected}$"):
            lease_policy.lock_lease_policy(using="owned", exclusive=exclusive)
        assert len(events) == 1
    lock = "UPDATE" if exclusive else "SHARE"
    assert events[0] == (
        f'SELECT singleton_key FROM "{TaskWorkerLeasePolicy._meta.db_table}" '
        f"WHERE singleton_key = %s FOR {lock} NOWAIT",
        [1],
    )


@pytest.mark.parametrize(
    "flags",
    [["--expected-seconds", "60"], ["--expected-revision", "1"], ["--all-lease-users-stopped"]],
)
def test_status_command_rejects_transition_flags_without_mutation(flags):
    before = list(TaskWorkerLeasePolicy.objects.values())
    output = io.StringIO()
    with pytest.raises(CommandError, match="^lease_policy_transition_invalid$") as raised:
        call_command("django_ray_lease_policy", *flags, stdout=output)
    assert raised.value.returncode == 2
    assert output.getvalue() == ""
    assert list(TaskWorkerLeasePolicy.objects.values()) == before


def test_status_command_unknown_database_is_fixed_and_private():
    output = io.StringIO()
    with pytest.raises(CommandError, match="^lease_policy_unavailable$") as raised:
        call_command("django_ray_lease_policy", "--database", "private-alias", stdout=output)
    assert raised.value.returncode == 2
    assert output.getvalue() == ""
