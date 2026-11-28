"""Strict query-budget and scaling-invariance regression tests.

Verifies constant-query and lock-free read budgets across Prometheus metrics,
TaskWorkerLeaseAdmin changelist rendering, protocol availability annotations,
external-input task enqueue, workflow run allocation, and coalesced workflow
progress collection reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.db import connection
from django.tasks import task
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from django_ray.admin import TaskWorkerLeaseAdmin
from django_ray.backends import RayTaskBackend
from django_ray.metrics import clear_metrics_cache, render_prometheus_metrics
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_status import annotate_execution_protocol_availability
from django_ray.result_storage import FilesystemResultStorage
from django_ray.runtime.context import DurableTaskContext
from django_ray.workflow.progress import reads as progress_reads
from django_ray.workflow.progress import runs as progress_runs
from tests.workflow_progress_storage_helpers import publish_initial_workflow


@task(queue_name="default")
def _query_budget_sample_task(payload: str, *, meta: dict[str, Any] | None = None) -> int:
    return len(payload) + (len(meta) if meta else 0)


def _make_backend() -> RayTaskBackend:
    return RayTaskBackend(
        "default",
        {
            "QUEUES": ["default"],
            "OPTIONS": {"RAY_ADDRESS": "auto"},
        },
    )


def _create_superuser(username: str) -> Any:
    user_model = get_user_model()
    return user_model.objects.create_superuser(
        username=username,
        email=f"{username}@example.com",
        password="test-password-123!",
    )


@pytest.mark.django_db
class TestPrometheusMetricsQueryBudget:
    """F-037: Prometheus scrape stays <= 4 queries cold and 0 queries on TTL cache hit."""

    def test_metrics_scrape_is_constant_and_zero_queries_on_cache_hit(
        self,
        settings: Any,
        django_assert_max_num_queries: Any,
    ) -> None:
        clear_metrics_cache()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "WORKER_LEASE_SECONDS": 60,
            "METRICS_CACHE_TTL_SECONDS": 60.0,
        }
        now = datetime.now(UTC)
        for idx in range(15):
            execution = RayTaskExecution.objects.create(
                task_id=f"qb-metric-exec-{idx}",
                callable_path=_query_budget_sample_task.module_path,
                queue_name="default" if idx % 2 == 0 else "ml",
                state=TaskState.FAILED if idx % 3 == 0 else TaskState.SUCCEEDED,
                error_message="Task timed out after 30s" if idx % 3 == 0 else None,
                attempt_number=2 if idx % 4 == 0 else 1,
                created_at=now - timedelta(seconds=30),
                started_at=now - timedelta(seconds=10),
                finished_at=now,
            )
            if idx % 4 == 0:
                TaskAttempt.objects.create(
                    execution=execution,
                    attempt_number=1,
                    state=TaskState.FAILED,
                    started_at=now - timedelta(seconds=25),
                    finished_at=now - timedelta(seconds=20),
                )

        TaskWorkerLease.objects.create(
            worker_id="qb-metric-lease-1",
            hostname="host-1",
            pid=1001,
            queue_name="default",
            capability_schema_version=1,
            legacy_admission_token=None,
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            is_active=True,
            last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=60),
            lease_policy_revision=1,
        )

        with django_assert_max_num_queries(4) as cold_capture:
            first = render_prometheus_metrics(queue_names=["default", "ml"])
        assert len(cold_capture.captured_queries) <= 4
        cold_sql = "\n".join(q["sql"].upper() for q in cold_capture.captured_queries)
        assert "NOT EXISTS" not in cold_sql
        assert " LIKE " not in cold_sql

        with django_assert_max_num_queries(0) as warm_capture:
            second = render_prometheus_metrics(queue_names=["default", "ml"])
        assert len(warm_capture.captured_queries) == 0
        assert second == first
        clear_metrics_cache()


@pytest.mark.django_db
class TestTaskWorkerLeaseAdminQueryBudget:
    """F-076: TaskWorkerLeaseAdmin changelist query count is strictly O(1) across 2 vs 20 rows."""

    def test_lease_admin_changelist_has_zero_n_plus_one_scaling(self) -> None:
        site = AdminSite(name="qb-lease-admin")
        site.register(TaskWorkerLease, TaskWorkerLeaseAdmin)
        lease_admin = site._registry[TaskWorkerLease]
        superuser = _create_superuser("qb-lease-superuser")
        now = datetime.now(UTC)

        def seed_leases(count: int) -> None:
            TaskWorkerLease.objects.all().delete()
            for idx in range(count):
                active = idx % 2 == 0
                stale = active and idx % 4 == 2
                hb = now - timedelta(seconds=120) if stale else now
                expires = hb + timedelta(seconds=60)
                TaskWorkerLease.objects.create(
                    worker_id=f"qb-lease-worker-{idx:03d}",
                    hostname=f"host-{idx % 3}",
                    pid=2000 + idx,
                    queue_name="default",
                    capability_schema_version=1,
                    legacy_admission_token=None,
                    min_supported_execution_protocol_version=1,
                    max_supported_execution_protocol_version=1,
                    is_active=active,
                    started_at=now - timedelta(minutes=5),
                    last_heartbeat_at=hb,
                    lease_expires_at=expires,
                    lease_policy_revision=1,
                    stopped_at=None if active else now - timedelta(minutes=1),
                )

        def render_changelist_query_count() -> int:
            request = RequestFactory().get("/admin/django_ray/taskworkerlease/?is_active=all")
            request.user = superuser
            with CaptureQueriesContext(connection) as ctx:
                response = lease_admin.changelist_view(request)
                if hasattr(response, "render"):
                    response.render()
            assert response.status_code == 200
            return len(ctx.captured_queries)

        seed_leases(2)
        queries_for_2 = render_changelist_query_count()

        seed_leases(20)
        queries_for_20 = render_changelist_query_count()

        assert queries_for_20 == queries_for_2
        assert queries_for_20 <= 8


@pytest.mark.django_db(transaction=True)
class TestProtocolAvailabilityQueryBudget:
    """F-076: annotate_execution_protocol_availability runs in 1 query with uncorrelated guards."""

    def test_protocol_availability_annotation_stays_single_query_for_2_vs_20_rows(self) -> None:
        observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        LegacyWorkerAdmissionToken.objects.all().delete()
        TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
            legacy_worker_admission_enabled=False,
            revision=2,
        )
        TaskWorkerLease.objects.create(
            worker_id="qb-proto-worker",
            hostname="proto-host",
            pid=3001,
            capability_schema_version=1,
            legacy_admission_token=None,
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=2,
            is_active=True,
            last_heartbeat_at=observed_at,
            lease_expires_at=observed_at + timedelta(seconds=60),
            lease_policy_revision=1,
        )

        def evaluate_for_count(count: int) -> tuple[int, str, list[tuple[str, bool | None]]]:
            RayTaskExecution.objects.all().delete()
            for idx in range(count):
                RayTaskExecution.objects.create(
                    task_id=f"qb-proto-row-{idx:03d}",
                    callable_path=_query_budget_sample_task.module_path,
                    execution_protocol_version=1 if idx % 2 == 0 else 9,
                    state=TaskState.QUEUED,
                )
            with CaptureQueriesContext(connection) as ctx:
                rows = list(
                    annotate_execution_protocol_availability(
                        RayTaskExecution.objects.order_by("task_id"),
                        observed_at=observed_at,
                    ).values_list("task_id", "protocol_compatible_worker_available")
                )
            return len(ctx.captured_queries), ctx.captured_queries[0]["sql"].upper(), rows

        q_count_2, sql_2, rows_2 = evaluate_for_count(2)
        q_count_20, sql_20, rows_20 = evaluate_for_count(20)

        assert q_count_2 == 1
        assert q_count_20 == 1
        assert "CASE WHEN NOT EXISTS" in sql_2
        assert sql_2 == sql_20
        assert rows_2 == [("qb-proto-row-000", True), ("qb-proto-row-001", False)]
        assert len(rows_20) == 20
        assert all(available is (idx % 2 == 0) for idx, (_tid, available) in enumerate(rows_20))


@pytest.mark.django_db(transaction=True)
class TestEnqueueSpilloverQueryAndIOBudget:
    """F-032: external input enqueue performs zero re-downloads and bounded SQL statements."""

    def test_enqueue_external_input_skips_redownload_and_stays_within_query_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        settings: Any,
        django_assert_max_num_queries: Any,
    ) -> None:
        input_dir = tmp_path / "inputs"
        input_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 32,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(input_dir),
        }

        def fail_on_load(*_args: Any, **_kwargs: Any) -> str:
            raise AssertionError("enqueue() must not re-download spilled external inputs")

        monkeypatch.setattr(FilesystemResultStorage, "load", fail_on_load)

        backend = _make_backend()
        large_payload = "z" * 256
        meta = {"source": "query-budget"}

        with django_assert_max_num_queries(10) as captured:
            result = backend.enqueue(
                _query_budget_sample_task,
                (large_payload,),
                {"meta": meta},
            )

        data_queries = [
            q["sql"]
            for q in captured.captured_queries
            if not q["sql"]
            .lstrip()
            .upper()
            .startswith(("BEGIN", "COMMIT", "SAVEPOINT", "RELEASE SAVEPOINT"))
        ]
        assert len(data_queries) <= 4
        assert result.args == [large_payload]
        assert result.kwargs == {"meta": meta}
        row = RayTaskExecution.objects.get(task_id=result.id)
        assert row.input_reference is not None

    def test_inline_enqueue_uses_minimal_sql_without_unnecessary_savepoints(
        self,
        settings: Any,
        django_assert_max_num_queries: Any,
    ) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto"}
        backend = _make_backend()

        with django_assert_max_num_queries(1) as captured:
            result = backend.enqueue(_query_budget_sample_task, ("ok",), {})

        assert len(captured.captured_queries) == 1
        assert "SAVEPOINT" not in captured.captured_queries[0]["sql"].upper()
        assert result.args == ["ok"]


@pytest.mark.django_db
class TestWorkflowRunAllocationAndProgressReadBudgets:
    """F-094 & F-040: single-update workflow run allocation and coalesced lock-free reads."""

    def test_allocate_workflow_run_uses_single_execution_update_when_namespace_exists(
        self,
        django_assert_max_num_queries: Any,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id=f"qb-alloc-{uuid.uuid4()}",
            callable_path=_query_budget_sample_task.module_path,
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
            workflow_run_namespace=123_456,
            workflow_run_sequence=1,
            workflow_run_id=None,
        )
        ctx = DurableTaskContext(
            task_pk=int(execution.pk),
            task_id=str(execution.task_id),
            attempt_number=1,
            execution_generation=1,
        )

        with django_assert_max_num_queries(4) as captured:
            allocated = progress_runs.allocate_workflow_run(ctx)

        assert allocated is not None
        update_queries = [
            q["sql"]
            for q in captured.captured_queries
            if q["sql"].lstrip().upper().startswith("UPDATE")
        ]
        assert len(update_queries) == 1
        assert len(captured.captured_queries) <= 4
        execution.refresh_from_db()
        assert execution.workflow_run_sequence == 2
        assert str(execution.workflow_run_id) == allocated.run_id

    def test_read_workflow_progress_collections_is_lock_free_and_within_coalesced_budget(
        self,
    ) -> None:
        published = publish_initial_workflow(12, case_id=837_001)
        execution = published.execution

        def allow_all(_execution: RayTaskExecution) -> bool:
            return True

        with CaptureQueriesContext(connection) as separate:
            progress_reads.get_workflow_progress_summary(execution, authorize=allow_all)
            progress_reads.list_workflow_topology_nodes(execution, authorize=allow_all)
            progress_reads.list_workflow_topology_edges(execution, authorize=allow_all)
            progress_reads.list_workflow_node_details(execution, authorize=allow_all)

        with CaptureQueriesContext(connection) as coalesced:
            bundle = progress_reads.read_workflow_progress_collections(
                execution,
                authorize=allow_all,
            )

        coalesced_sql = "\n".join(q["sql"].upper() for q in coalesced.captured_queries)
        assert "FOR UPDATE" not in coalesced_sql
        assert len(coalesced.captured_queries) < len(separate.captured_queries) - 2
        assert len(coalesced.captured_queries) <= 10
        assert len(bundle["topology_nodes"]["items"]) == 12
        assert len(bundle["node_details"]["items"]) == 12
