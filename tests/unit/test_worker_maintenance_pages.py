"""Actual-model SQLite candidate bounds; no Ray or external services."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django_ray.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_RANGE as PROTOCOLS
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.maintenance import MaintenanceScan

pytestmark = pytest.mark.django_db


def rows(count, *, state=TaskState.RUNNING, queue="default", owner=None):
    return RayTaskExecution.objects.bulk_create(
        [
            RayTaskExecution(
                task_id=f"{state}-{queue}-{index}",
                callable_path="testproject.tasks.add_numbers",
                state=state,
                queue_name=queue,
                claimed_by_worker=owner,
                ray_job_id=f"raysubmit_{state}_{queue}_{index}",
            )
            for index in range(count)
        ]
    )


def test_frozen_cycle_revisits_old_rows_despite_continuous_inserts(monkeypatch):
    monkeypatch.setattr("django_ray.runner.maintenance.MAINTENANCE_PAGE_SIZE", 2)
    initial = rows(5)
    query = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    scan = MaintenanceScan()
    visited = []
    for index in range(3):
        page = scan.page(query, scope=("default",))
        visited.extend(row.pk for row in page)
        for candidate in page:
            scan.advance(candidate)
        RayTaskExecution.objects.create(
            task_id=f"insert-{index}",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
        )
    assert visited == [row.pk for row in initial]
    assert not scan.pending
    assert scan.page(query, scope=("default",))[0].pk == initial[0].pk


def test_projection_bounds_corrupt_sqlite_owner_and_avoids_all_payloads():
    created = rows(40)
    RayTaskExecution.objects.filter(pk=created[0].pk).update(claimed_by_worker="secret" * 10000)
    query = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    with CaptureQueriesContext(connection) as captured:
        page = MaintenanceScan().page(query, scope=("default",))
    assert len(page) == 32
    assert len(page[0].owner) == 256 and not page[0].owner_complete
    assert all(len(candidate.owner or "") <= 256 for candidate in page)
    assert len(captured) == 2
    projection = captured[-1]["sql"].split(" FROM ", 1)[0]
    for field in (
        "completion_data",
        "args_json",
        "kwargs_json",
        "error_message",
        "workflow_plan_selection",
        "workflow_progress_summary_json",
        "runtime_env_json",
    ):
        assert field not in projection
    assert "LIMIT 33" in captured[-1]["sql"]


def test_cursor_handles_deletion_state_flip_error_and_partial_page(monkeypatch):
    monkeypatch.setattr("django_ray.runner.maintenance.MAINTENANCE_PAGE_SIZE", 2)
    initial = rows(5)
    query = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    command = Command()
    page = command._maintenance_scan_page("orphan", query, ("default",), PROTOCOLS)
    command._maintenance_snapshot("orphan", query, page[0])
    # An interrupted turn did not consider page[1]. Deletion/state changes do
    # not shift an offset or remove its stable continuation position.
    initial[0].delete()
    RayTaskExecution.objects.filter(pk=initial[2].pk).update(state=TaskState.FAILED)
    page = command._maintenance_scan_page("orphan", query, ("default",), PROTOCOLS)
    assert [row.pk for row in page] == [initial[1].pk, initial[3].pk]
    query_type = type(query)
    original = query_type.first

    def broken_snapshot(selected):
        if selected.query.deferred_loading[0]:
            raise ValueError("one malformed snapshot")
        return original(selected)

    monkeypatch.setattr(query_type, "first", broken_snapshot)
    assert command._maintenance_snapshot("orphan", query, page[0]) is None
    monkeypatch.setattr(query_type, "first", original)
    assert command._maintenance_snapshot("orphan", query, page[1]).pk == initial[3].pk
    last = command._maintenance_scan_page("orphan", query, ("default",), PROTOCOLS)
    assert [row.pk for row in last] == [initial[4].pk]
    command._maintenance_snapshot("orphan", query, last[0])
    assert not command._maintenance_scans["orphan"].pending


def test_scope_reset_and_fresh_identity_filter():
    initial = rows(3)
    command = Command()
    query = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    first = command._maintenance_scan_page("running", query, ("default",), PROTOCOLS)
    RayTaskExecution.objects.filter(pk=initial[0].pk).update(
        attempt_number=2, execution_generation=1
    )
    assert command._maintenance_snapshot("running", query, first[0]) is None
    changed = command._maintenance_scan_page("running", query, None, PROTOCOLS)
    assert changed[0].pk == initial[0].pk and changed[0].attempt_number == 2


@pytest.mark.parametrize("kind", ["orphan", "running", "cancelling"])
def test_entrypoints_visit_tail_after_healthy_or_skipped_prefix(monkeypatch, kind):
    state = TaskState.CANCELLING if kind == "cancelling" else TaskState.RUNNING
    initial = rows(40, state=state)
    command = Command()
    monkeypatch.setattr(command, "_authoritative_protocol_range_for_scan", lambda: PROTOCOLS)
    monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", Mock())
    considered = []
    original = command._maintenance_snapshot

    def snapshot(*args):
        task = original(*args)
        if task is not None:
            considered.append(task.pk)
        return task

    monkeypatch.setattr(command, "_maintenance_snapshot", snapshot)
    monkeypatch.setattr(command, "_adopt_orphaned_ray_job_task", lambda *args, **kwargs: False)

    @contextmanager
    def unavailable(*args, **kwargs):
        yield None

    monkeypatch.setattr(command, "_authoritative_task_owner", unavailable)
    method = {
        "orphan": command.reconcile_tasks,
        "running": command.detect_stuck_tasks,
        "cancelling": command.process_cancellations,
    }[kind]
    assert method(("default",)) == 0
    assert len(considered) == 32
    assert method(("default",)) == 0
    assert considered == [row.pk for row in initial]


def test_owner_lookup_is_restricted_and_shutdown_leaves_suffix_unvisited(monkeypatch):
    initial = rows(40, owner="peer")
    command = Command()
    query = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    page = command._maintenance_scan_page("running", query, None, PROTOCOLS)
    with CaptureQueriesContext(connection) as captured:
        command._maintenance_active_workers(page)
    lease_queries = [
        row["sql"] for row in captured if f'FROM "{TaskWorkerLease._meta.db_table}"' in row["sql"]
    ]
    assert len(lease_queries) == 1 and " IN ('peer')" in lease_queries[0]
    command._maintenance_snapshot("running", query, page[0])
    command.shutdown_requested = True
    assert command.detect_stuck_tasks() == 0
    assert command._maintenance_scans["running"].after == initial[0].pk


def test_active_reconciliation_rpc_page_rotates_past_pending_prefix(monkeypatch):
    initial = rows(40, owner="local-worker")
    command = Command()
    command.worker_id = "local-worker"
    command.active_tasks = {row.pk: row.ray_job_id for row in initial}
    command.active_task_identities = {row.pk: (1, 0) for row in initial}
    monkeypatch.setattr(command, "_authoritative_protocol_range_for_scan", lambda: PROTOCOLS)
    monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", Mock())
    called = []
    monkeypatch.setattr(
        command, "_reconcile_ray_job_task", lambda task, *args, **kwargs: called.append(task.pk)
    )
    command.reconcile_tasks()
    assert len(called) == 32
    command.reconcile_tasks()
    assert called == [row.pk for row in initial]


def test_continuation_is_separate_from_business_activity(monkeypatch):
    rows(40)
    command = Command()
    query = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    page = command._maintenance_scan_page("running", query, None, PROTOCOLS)
    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.time.monotonic", lambda: 100.0
    )
    for candidate in page:
        command._maintenance_snapshot("running", query, candidate)
    assert command._maintenance_deadline("running", 30) == 100.1
    for candidate in command._maintenance_scan_page("running", query, None, PROTOCOLS):
        command._maintenance_snapshot("running", query, candidate)
    assert command._maintenance_deadline("running", 30) == 130.0


def test_corrupt_sqlite_numeric_cell_is_bounded_and_cannot_authorize_a_snapshot():
    task = rows(1)[0]
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE django_ray_raytaskexecution SET attempt_number=%s WHERE id=%s",
            ["invalid" * 10000, task.pk],
        )
    command = Command()
    query = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    page = command._maintenance_scan_page("running", query, None, PROTOCOLS)
    assert len(page) == 1 and not page[0].identity_complete
    assert command._maintenance_snapshot("running", query, page[0]) is None
    assert not command._maintenance_scans["running"].pending


def test_actual_loop_runs_heartbeat_and_claim_between_pending_cancellation_pages(monkeypatch):
    rows(40, state=TaskState.CANCELLING)
    command = Command()
    clock = [0.0]
    events = []
    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.time.monotonic", lambda: clock[0]
    )
    monkeypatch.setattr(
        command, "_wait_for_poll_deadline", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    monkeypatch.setattr(command, "send_heartbeat", lambda: events.append(("heartbeat", clock[0])))
    monkeypatch.setattr(
        command, "claim_and_process_tasks", lambda *args: events.append(("claim", clock[0])) or 0
    )
    monkeypatch.setattr(command, "_authoritative_protocol_range_for_scan", lambda: PROTOCOLS)
    monkeypatch.setattr(command, "reconcile_tasks", lambda *args: 0)
    monkeypatch.setattr(command, "detect_stuck_tasks", lambda *args: 0)
    monkeypatch.setattr(command, "cleanup_expired_leases", lambda: 0)

    @contextmanager
    def unavailable(*args, **kwargs):
        yield None

    monkeypatch.setattr(command, "_authoritative_task_owner", unavailable)
    original = command.process_cancellations
    considered = []
    original_snapshot = command._maintenance_snapshot

    def snapshot(*args):
        task = original_snapshot(*args)
        considered.append(task.pk)
        return task

    monkeypatch.setattr(command, "_maintenance_snapshot", snapshot)

    def cancellations(queues):
        events.append(("cancel", clock[0]))
        assert original(queues) == 0  # Pending work is not lifecycle activity.
        if len(considered) == 40:
            command.shutdown_requested = True
        return 0

    monkeypatch.setattr(command, "process_cancellations", cancellations)
    command.run_loop(queues=["default"], concurrency=1, heartbeat_interval=0.05)
    assert [event for event, _ in events] == [
        "heartbeat",
        "claim",
        "cancel",
        "heartbeat",
        "heartbeat",
        "claim",
        "cancel",
    ]
    assert [at for event, at in events if event == "cancel"] == pytest.approx([0.0, 0.1])
    assert len(considered) == len(set(considered)) == 40


def test_newly_eligible_row_behind_cursor_waits_for_next_finite_cycle(monkeypatch):
    monkeypatch.setattr("django_ray.runner.maintenance.MAINTENANCE_PAGE_SIZE", 2)
    initial = rows(5)
    query = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    scan = MaintenanceScan()
    page = scan.page(query, scope=("default",))
    for candidate in page:
        scan.advance(candidate)
    RayTaskExecution.objects.filter(pk=initial[0].pk).update(state=TaskState.CANCELLING)
    RayTaskExecution.objects.filter(pk=initial[0].pk).update(state=TaskState.RUNNING)
    remaining = []
    while scan.pending:
        page = scan.page(query, scope=("default",))
        remaining.extend(candidate.pk for candidate in page)
        for candidate in page:
            scan.advance(candidate)
    assert remaining == [row.pk for row in initial[2:]]
    assert scan.page(query, scope=("default",))[0].pk == initial[0].pk


def test_queue_and_protocol_scope_changes_reset_an_in_progress_cycle():
    from django_ray.execution_protocol import ExecutionProtocolRange

    defaults = rows(40)
    urgent = rows(1, queue="urgent")
    command = Command()
    base = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
    default_query = base.filter(queue_name="default")
    page = command._maintenance_scan_page("running", default_query, ["default"], PROTOCOLS)
    command._maintenance_snapshot("running", default_query, page[0])
    urgent_page = command._maintenance_scan_page(
        "running", base.filter(queue_name="urgent"), ["urgent"], PROTOCOLS
    )
    assert [candidate.pk for candidate in urgent_page] == [urgent[0].pk]
    changed = command._maintenance_scan_page(
        "running", default_query, ["default"], ExecutionProtocolRange(1, 2)
    )
    assert changed[0].pk == defaults[0].pk


@pytest.mark.parametrize("kind", ["orphan", "running"])
def test_live_peer_prefix_does_not_load_payload_rows_or_starve_tail(monkeypatch, kind):
    initial = rows(33, owner="healthy-peer")
    RayTaskExecution.objects.filter(pk=initial[-1].pk).update(claimed_by_worker=None)
    TaskWorkerLease.objects.create(
        worker_id="healthy-peer", hostname="peer", pid=1, queue_name="default"
    )
    command = Command()
    monkeypatch.setattr(command, "_authoritative_protocol_range_for_scan", lambda: PROTOCOLS)
    monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", Mock())
    monkeypatch.setattr(command, "_adopt_orphaned_ray_job_task", lambda *args, **kwargs: False)
    loaded = []
    original = command._maintenance_snapshot

    def snapshot(*args):
        task = original(*args)
        loaded.append(task.pk)
        return task

    monkeypatch.setattr(command, "_maintenance_snapshot", snapshot)
    operation = command.reconcile_tasks if kind == "orphan" else command.detect_stuck_tasks
    assert operation(["default"]) == 0
    assert loaded == []
    assert operation(["default"]) == 0
    assert loaded == [initial[-1].pk]


def test_running_page_looks_up_only_selected_exact_local_handles(monkeypatch):
    command = Command()
    initial = rows(40, owner=command.worker_id)
    RayTaskExecution.objects.all().update(ray_job_id=None)
    lookups = []
    untracked = []

    class Runner:
        @property
        def pending_task_handles(self):
            pytest.fail("maintenance must not copy every local pending handle")

        def get_pending_handle(self, task_pk, *, attempt_number, execution_generation):
            lookups.append((task_pk, attempt_number, execution_generation))
            if task_pk == initial[0].pk:
                return SimpleNamespace(
                    attempt_number=attempt_number, execution_generation=execution_generation
                )
            return None

    command.ray_core_runner = Runner()
    monkeypatch.setattr(command, "_authoritative_protocol_range_for_scan", lambda: PROTOCOLS)
    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.is_task_timed_out", lambda task: False
    )

    def is_stuck(task):
        untracked.append(task.pk)
        return False

    monkeypatch.setattr("django_ray.management.commands.django_ray_worker.is_task_stuck", is_stuck)
    assert command.detect_stuck_tasks(["default"]) == 0
    assert lookups == [
        (task.pk, task.attempt_number, task.execution_generation) for task in initial[:32]
    ]
    assert untracked == [task.pk for task in initial[1:32]]
    assert command.detect_stuck_tasks(["default"]) == 0
    assert len(lookups) == 40
    assert untracked == [task.pk for task in initial[1:]]
