"""Wave 4 regression tests for core, DB, metrics, tracing, settings, purge, and replay fixes."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.db.models import ProtectedError
from django.tasks import TaskContext, TaskResultStatus, task
from django.tasks.signals import task_finished
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from django_ray.backends import (
    InputPayloadUnavailableError,
    RayTaskBackend,
    RayTaskResult,
    ResultUnavailableError,
    _build_signal_task_result,
)
from django_ray.conf.defaults import DEFAULTS
from django_ray.conf.validation import validate_settings
from django_ray.decorators import ray_task
from django_ray.doctor import probe
from django_ray.lifecycle import request_task_cancellation
from django_ray.management.commands.django_ray_purge_executions import (
    Command as PurgeExecutionsCommand,
)
from django_ray.management.purge_cursor import (
    ExecutionPurgeCursor,
    PurgeCursor,
    decode_execution_purge_cursor,
    decode_purge_cursor,
    encode_execution_purge_cursor,
    encode_purge_cursor,
)
from django_ray.metrics import (
    _collect_lease_health,
    clear_metrics_cache,
    render_prometheus_metrics,
)
from django_ray.models import (
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_status import annotate_execution_protocol_availability
from django_ray.result_storage import FilesystemResultStorage
from django_ray.runtime.context import get_current_task_context, get_current_task_execution_pk
from django_ray.runtime.runtime_env import resolve_runtime_env_profile
from django_ray.tracing import (
    SPAN_TASK_ENQUEUE,
    SPAN_TASK_REMOTE_EXECUTE,
    SPAN_TASK_TERMINAL_PERSIST,
    OpenTelemetryTracer,
    TaskTracingCoordinator,
    TracingConfig,
    _resolve_lifecycle_coordinator,
    clear_tracing_caches,
    configure_task_tracing,
    emit_task_lifecycle_span,
    get_task_tracing_coordinator,
)


@ray_task(
    queue_name="high-priority",
    priority=7,
    num_cpus=2,
    num_gpus=1,
    memory=1024 * 1024,
    max_attempts=5,
    retry_backoff_seconds=30,
    retry_jitter=False,
    timeout_seconds=600,
    runtime_env_profile="ml-inference",
)
def _contract_sample_task(payload: str) -> str:
    return payload


@ray_task(queue_name="default", timeout_seconds=120)
def _contract_timeout_only_task(value: int) -> int:
    return value + 1


@task(queue_name="default")
def _plain_w4_task(payload: str) -> str:
    return payload


@task(queue_name="default")
def _replay_durable_context_task(label: str) -> dict[str, Any]:
    ctx = get_current_task_context()
    return {
        "label": label,
        "pk": get_current_task_execution_pk(),
        "task_id": ctx.task_id if ctx is not None else None,
        "attempt_number": ctx.attempt_number if ctx is not None else None,
        "execution_generation": ctx.execution_generation if ctx is not None else None,
    }


@task(queue_name="default", takes_context=True)
def _replay_django_context_attempt_task(context: TaskContext, value: int) -> dict[str, Any]:
    return {"attempt": context.attempt, "value": value, "task_id": context.task_result.id}


@task(queue_name="default")
async def _replay_cancelled_async_task() -> str:
    raise asyncio.CancelledError("cancelled during local replay")


def _make_backend(
    *,
    queues: list[str] | None = None,
    options: dict[str, Any] | None = None,
) -> RayTaskBackend:
    return RayTaskBackend(
        "default",
        {
            "QUEUES": queues if queues is not None else ["default", "high-priority"],
            "OPTIONS": {"RAY_ADDRESS": "auto", **(options or {})},
        },
    )


@pytest.mark.django_db
class TestR2005RayTaskContractEnqueue:
    """R2-005: @ray_task contract fields apply cleanly during RayTaskBackend.enqueue()."""

    def test_enqueue_applies_contract_runtime_env_profile_and_timeout_over_backend_defaults(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RUNTIME_ENV_PROFILES": {
                "default-prof": {"env_vars": {"ROLE": "default"}},
                "ml-inference": {"pip": ["torch==2.5.0"], "env_vars": {"ROLE": "ml"}},
            },
        }
        backend = _make_backend(
            options={
                "RUNTIME_ENV_PROFILE": "default-prof",
                "TIMEOUT_SECONDS": 60,
            }
        )

        result = backend.enqueue(_contract_sample_task, args=("batch-1",), kwargs={})
        execution = RayTaskExecution.objects.get(task_id=result.id)

        assert execution.queue_name == "high-priority"
        assert execution.priority == 7
        assert execution.runtime_env_profile == "ml-inference"
        assert json.loads(execution.runtime_env_json)["pip"] == ["torch==2.5.0"]
        assert execution.timeout_seconds == 600

    def test_enqueue_contract_profile_cleanly_overrides_backend_inline_runtime_env(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RUNTIME_ENV_PROFILES": {
                "ml-inference": {"env_vars": {"PROFILE": "ml"}},
            },
        }
        backend = _make_backend(
            options={"RAY_RUNTIME_ENV": {"env_vars": {"INLINE": "backend"}}},
        )

        result = backend.enqueue(_contract_sample_task, args=("batch-2",), kwargs={})
        execution = RayTaskExecution.objects.get(task_id=result.id)

        assert execution.runtime_env_profile == "ml-inference"
        assert json.loads(execution.runtime_env_json) == {"env_vars": {"PROFILE": "ml"}}

    def test_enqueue_falls_back_to_backend_defaults_when_contract_omits_field(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RUNTIME_ENV_PROFILES": {
                "backend-prof": {"env_vars": {"SRC": "backend"}},
            },
        }
        backend = _make_backend(
            options={"RUNTIME_ENV_PROFILE": "backend-prof", "TIMEOUT_SECONDS": 45},
        )

        res_timeout_only = backend.enqueue(_contract_timeout_only_task, args=(10,), kwargs={})
        exec_timeout_only = RayTaskExecution.objects.get(task_id=res_timeout_only.id)
        assert exec_timeout_only.runtime_env_profile == "backend-prof"
        assert exec_timeout_only.timeout_seconds == 120

        res_plain = backend.enqueue(_plain_w4_task, args=("ok",), kwargs={})
        exec_plain = RayTaskExecution.objects.get(task_id=res_plain.id)
        assert exec_plain.runtime_env_profile == "backend-prof"
        assert exec_plain.timeout_seconds == 45


@pytest.mark.django_db(transaction=True)
class TestR2006PurgeExecutionsInvertedOrderAndToctou:
    """R2-006: DB rows delete first; post-commit orphan check prevents content-addressed TOCTOU."""

    def test_post_commit_toctou_new_live_row_sharing_blob_prevents_blob_deletion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        root = tmp_path / "results"
        root.mkdir()
        settings.DJANGO_RAY = {
            **getattr(settings, "DJANGO_RAY", {}),
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(root),
        }
        storage = FilesystemResultStorage(root)
        shared_ref = storage.store_payload(serialized_payload='{"value":"shared-toctou"}')
        old_ts = timezone.now() - timedelta(days=45)
        old_exec = RayTaskExecution.objects.create(
            task_id="purge-toctou-old",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            created_at=old_ts,
            finished_at=old_ts,
            result_reference=shared_ref,
        )

        original_partition = PurgeExecutionsCommand._partition_orphan_result_refs

        def inject_concurrent_live_row(
            candidate_refs: frozenset[str],
            *,
            using: str,
        ) -> tuple[tuple[str, ...], frozenset[str]]:
            assert not connection.in_atomic_block
            assert not RayTaskExecution.objects.filter(pk=old_exec.pk).exists()
            # Concurrent worker persists a brand-new live execution referencing the exact same blob.
            RayTaskExecution.objects.create(
                task_id="purge-toctou-live-new",
                callable_path="testproject.tasks.add_numbers",
                state=TaskState.SUCCEEDED,
                created_at=timezone.now(),
                finished_at=timezone.now(),
                result_reference=shared_ref,
            )
            return original_partition(candidate_refs, using=using)

        monkeypatch.setattr(
            PurgeExecutionsCommand,
            "_partition_orphan_result_refs",
            staticmethod(inject_concurrent_live_row),
        )

        stdout = StringIO()
        call_command(
            "django_ray_purge_executions",
            older_than_days=30,
            delete=True,
            format="json",
            stdout=stdout,
        )
        report = json.loads(stdout.getvalue())
        assert report["counts"]["executions_deleted"] == 1
        assert report["counts"]["results_deleted"] == 0
        assert report["counts"]["results_skipped_shared"] == 1
        assert storage.load(reference=shared_ref) == '{"value":"shared-toctou"}'

    def test_protected_error_rollback_deletes_zero_external_blobs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        import django_ray.result_storage as result_storage_module

        root = tmp_path / "results_prot"
        root.mkdir()
        settings.DJANGO_RAY = {
            **getattr(settings, "DJANGO_RAY", {}),
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(root),
        }
        storage = FilesystemResultStorage(root)
        ref = storage.store_payload(serialized_payload='{"value":"protected-blob"}')
        old_ts = timezone.now() - timedelta(days=45)
        exec_row = RayTaskExecution.objects.create(
            task_id="purge-protected-rollback",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            created_at=old_ts,
            finished_at=old_ts,
            result_reference=ref,
        )

        original_qs_delete = RayTaskExecution.objects.all().__class__.delete

        def fail_with_protected(qs: Any) -> Any:
            if qs.model is RayTaskExecution:
                raise ProtectedError("protected by concurrent binding", set())
            return original_qs_delete(qs)

        monkeypatch.setattr(RayTaskExecution.objects.all().__class__, "delete", fail_with_protected)
        monkeypatch.setattr(
            result_storage_module,
            "delete_result_reference",
            lambda *_a, **_kw: pytest.fail("must not delete blob when DB delete rolled back"),
            raising=False,
        )

        stdout = StringIO()
        with pytest.raises(CommandError):
            call_command(
                "django_ray_purge_executions",
                older_than_days=30,
                delete=True,
                format="json",
                stdout=stdout,
            )
        report = json.loads(stdout.getvalue())
        assert report["counts"]["executions_failed"] == 1
        assert report["counts"]["results_deleted"] == 0
        assert RayTaskExecution.objects.filter(pk=exec_row.pk).exists()
        assert storage.load(reference=ref) == '{"value":"protected-blob"}'


@pytest.mark.django_db(transaction=True)
class TestR2007EnqueueInputRegistrationOutsideAtomic:
    """R2-007(a): External input storage verification never executes inside a DB row lock."""

    def test_dedup_enqueue_verifies_storage_outside_atomic_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        input_dir = tmp_path / "inputs"
        input_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 32,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(input_dir),
        }
        backend = _make_backend()
        payload = "x" * 256

        first = backend.enqueue(_plain_w4_task, args=(payload,), kwargs={})
        row = RayTaskExecution.objects.get(task_id=first.id)
        assert row.input_reference is not None
        TaskInputPayload.objects.filter(pk=row.input_reference).update(
            state=InputPayloadState.PURGED
        )

        atomic_states_during_load: list[bool] = []
        original_load = FilesystemResultStorage.load

        def spy_load(self: FilesystemResultStorage, *, reference: str) -> str | None:
            atomic_states_during_load.append(connection.in_atomic_block)
            return original_load(self, reference=reference)

        monkeypatch.setattr(FilesystemResultStorage, "load", spy_load)

        second = backend.enqueue(_plain_w4_task, args=(payload,), kwargs={})
        assert second.id != first.id
        # Reactivating a PURGED tombstone verifies/restores external bytes inside
        # the TaskInputPayload row lock so concurrent purge serialization holds.
        assert atomic_states_during_load == [True]
        assert (
            TaskInputPayload.objects.get(pk=row.input_reference).state == InputPayloadState.ACTIVE
        )

    def test_enqueue_inside_caller_atomic_revalidates_reused_active_external_input(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        input_dir = tmp_path / "inputs_outer_tx"
        input_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 32,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(input_dir),
        }
        backend = _make_backend()
        payload = "y" * 256
        backend.enqueue(_plain_w4_task, args=(payload,), kwargs={})

        load_states: list[bool] = []
        original_load = FilesystemResultStorage.load

        def spy_load(self: FilesystemResultStorage, *, reference: str) -> str | None:
            load_states.append(connection.in_atomic_block)
            return original_load(self, reference=reference)

        monkeypatch.setattr(FilesystemResultStorage, "load", spy_load)
        with transaction.atomic():
            result = backend.enqueue(_plain_w4_task, args=(payload,), kwargs={})
        assert RayTaskExecution.objects.filter(task_id=result.id).exists()
        assert load_states == [True]


@pytest.mark.django_db(transaction=True)
class TestR2011QueuedCancelNotificationFidelity:
    """R2-011: QUEUED cancellation loads full snapshot fields for task_finished and tracing."""

    def test_queued_cancel_emits_intact_task_result_and_trace_span(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="cancel-queued-full-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="high-priority",
            priority=5,
            state=TaskState.QUEUED,
            args_json=json.dumps([10, 20]),
            kwargs_json=json.dumps({"scale": 2}),
        )

        received_results: list[RayTaskResult] = []

        def receiver(sender: Any, task_result: RayTaskResult, **kwargs: Any) -> None:
            del sender, kwargs
            received_results.append(task_result)

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        prev_coord = get_task_tracing_coordinator()
        configure_task_tracing(
            TaskTracingCoordinator(
                TracingConfig(enabled=True),
                tracer=OpenTelemetryTracer(tracer_provider=provider),
            )
        )
        task_finished.connect(receiver)
        try:
            outcome = request_task_cancellation(execution.pk)
        finally:
            task_finished.disconnect(receiver)
            configure_task_tracing(prev_coord)
            provider.shutdown()

        assert outcome.state == TaskState.CANCELLED
        assert len(received_results) == 1
        emitted = received_results[0]
        assert emitted.id == execution.task_id
        assert emitted.task.module_path == execution.callable_path
        assert emitted.task.queue_name == "high-priority"
        assert emitted.enqueued_at == execution.created_at
        assert emitted.args == [10, 20]
        assert emitted.kwargs == {"scale": 2}
        assert emitted.status == TaskResultStatus.FAILED

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == SPAN_TASK_TERMINAL_PERSIST
        assert spans[0].attributes is not None
        assert spans[0].attributes["django_ray.task.id"] == execution.task_id
        assert spans[0].attributes["django_ray.task.queue_name"] == "high-priority"

    def test_queued_cancel_tracing_only_omits_input_payload_columns(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="cancel-queued-trace-only-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="high-priority",
            priority=5,
            state=TaskState.QUEUED,
            args_json=json.dumps(["x" * 1024]),
            kwargs_json=json.dumps({"blob": "y" * 1024}),
        )

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        prev_coord = get_task_tracing_coordinator()
        configure_task_tracing(
            TaskTracingCoordinator(
                TracingConfig(enabled=True),
                tracer=OpenTelemetryTracer(tracer_provider=provider),
            )
        )
        try:
            with CaptureQueriesContext(connection) as queries:
                outcome = request_task_cancellation(execution.pk)
        finally:
            configure_task_tracing(prev_coord)
            provider.shutdown()

        assert outcome.state == TaskState.CANCELLED
        select_sqls = " ".join(
            q["sql"] for q in queries.captured_queries if q["sql"].upper().startswith("SELECT")
        )
        for payload_col in ("args_json", "kwargs_json", "input_reference", "cancellation_error"):
            assert f'"{payload_col}"' not in select_sqls
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes is not None
        assert spans[0].attributes["django_ray.task.id"] == execution.task_id
        assert spans[0].attributes["django_ray.task.queue_name"] == "high-priority"


@pytest.mark.django_db
class TestR2015SettingsMetricsAndTracing:
    """R2-015 & R2-028: Settings registration, 5s metrics cache + DB-clock, cached/linked tracing."""

    def test_all_seven_new_settings_have_exact_defaults_and_validation(self) -> None:
        assert DEFAULTS["METRICS_CACHE_TTL_SECONDS"] == 5.0
        assert DEFAULTS["TRACING_ENABLED"] is False
        assert DEFAULTS["TRACING_SAMPLE_RATE"] == 1.0
        assert DEFAULTS["TRACING_MAX_WORKFLOW_NODE_SPANS"] == 100
        assert DEFAULTS["ALLOWED_CALLABLE_MODULES"] is None
        assert DEFAULTS["ALLOWED_RAY_ADDRESSES"] is None
        assert DEFAULTS["WORKER_CANCELLATION_INTERVAL_SECONDS"] == 5.0

        validate_settings(
            {
                "RAY_ADDRESS": "auto",
                "METRICS_CACHE_TTL_SECONDS": 5.0,
                "TRACING_ENABLED": True,
                "TRACING_SAMPLE_RATE": 0.5,
                "TRACING_MAX_WORKFLOW_NODE_SPANS": 100,
                "ALLOWED_CALLABLE_MODULES": ["myapp.tasks", "other_app"],
                "ALLOWED_RAY_ADDRESSES": ["auto", "http://ray-head:8265", "ray://ray-head:10001"],
                "WORKER_CANCELLATION_INTERVAL_SECONDS": 5.0,
            }
        )

        invalid_cases: list[tuple[str, Any]] = [
            ("METRICS_CACHE_TTL_SECONDS", -1.0),
            ("METRICS_CACHE_TTL_SECONDS", True),
            ("METRICS_CACHE_TTL_SECONDS", 301.0),
            ("TRACING_ENABLED", "true"),
            ("TRACING_SAMPLE_RATE", 1.5),
            ("TRACING_SAMPLE_RATE", False),
            ("TRACING_MAX_WORKFLOW_NODE_SPANS", 257),
            ("TRACING_MAX_WORKFLOW_NODE_SPANS", -1),
            ("ALLOWED_CALLABLE_MODULES", "myapp.tasks"),
            ("ALLOWED_CALLABLE_MODULES", ["bad..module"]),
            ("ALLOWED_RAY_ADDRESSES", "http://ray-head:8265"),
            ("ALLOWED_RAY_ADDRESSES", ["file:///etc/passwd"]),
            ("WORKER_CANCELLATION_INTERVAL_SECONDS", 0.01),
            ("WORKER_CANCELLATION_INTERVAL_SECONDS", True),
        ]
        for key, bad_value in invalid_cases:
            with pytest.raises(ImproperlyConfigured, match=key):
                validate_settings({"RAY_ADDRESS": "auto", key: bad_value})

    def test_metrics_default_five_second_ttl_caches_and_uses_db_clock_for_leases(
        self,
        django_assert_max_num_queries: Any,
    ) -> None:
        clear_metrics_cache()
        try:
            now = timezone.now()
            TaskWorkerLease.objects.create(
                worker_id="w4-db-clock-healthy",
                hostname="host-1",
                pid=101,
                is_active=True,
                last_heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=60),
                lease_policy_revision=1,
            )
            with CaptureQueriesContext(connection) as live_ctx:
                counts = _collect_lease_health(observed_at=None)
            assert counts == {"healthy": 1, "stale": 0, "inactive": 0}
            lease_sql = live_ctx.captured_queries[0]["sql"].upper()
            assert "LEASE_EXPIRES_AT" in lease_sql
            assert any(
                token in lease_sql
                for token in ("CURRENT_TIMESTAMP", "STATEMENT_TIMESTAMP", "NOW()", "STRFTIME(")
            )

            with django_assert_max_num_queries(4) as cold:
                first = render_prometheus_metrics(queue_names=["default"])
            assert len(cold.captured_queries) <= 4

            with django_assert_max_num_queries(0) as warm:
                second = render_prometheus_metrics(queue_names=["default"])
            assert len(warm.captured_queries) == 0
            assert second == first
        finally:
            clear_metrics_cache()

    def test_tracing_caches_settings_coordinator_and_propagates_w3c_traceparent(self) -> None:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        prev_coord = get_task_tracing_coordinator()
        prev_django_conf = sys.modules.get("django.conf")
        clear_tracing_caches()
        try:
            configure_task_tracing(None)
            fake_conf = SimpleNamespace(
                settings=SimpleNamespace(
                    configured=True,
                    DJANGO_RAY={
                        "TRACING_ENABLED": True,
                        "TRACING_SAMPLE_RATE": 1.0,
                        "TRACING_MAX_WORKFLOW_NODE_SPANS": 64,
                    },
                )
            )
            sys.modules["django.conf"] = fake_conf  # type: ignore[assignment]
            first_coord = _resolve_lifecycle_coordinator()
            second_coord = _resolve_lifecycle_coordinator()
            assert first_coord is not None
            assert first_coord is second_coord

            # Changing sample_rate invalidates the cache cleanly.
            fake_conf.settings.DJANGO_RAY["TRACING_SAMPLE_RATE"] = 0.5
            third_coord = _resolve_lifecycle_coordinator()
            assert third_coord is not None
            assert third_coord is not first_coord

            # Real RayTaskExecution (no trace_context attr): enqueued -> started -> finished link!
            configure_task_tracing(
                TaskTracingCoordinator(
                    TracingConfig(enabled=True),
                    tracer=OpenTelemetryTracer(tracer_provider=provider),
                )
            )
            execution = RayTaskExecution.objects.create(
                task_id="trace-link-001",
                callable_path="testproject.tasks.add_numbers",
                queue_name="default",
                state=TaskState.QUEUED,
            )
            assert not hasattr(execution, "trace_context")

            emit_task_lifecycle_span("enqueued", execution)
            execution.state = TaskState.RUNNING
            emit_task_lifecycle_span("started", execution)
            execution.state = TaskState.SUCCEEDED
            emit_task_lifecycle_span("finished", execution)

            spans = exporter.get_finished_spans()
            assert [s.name for s in spans] == [
                SPAN_TASK_ENQUEUE,
                SPAN_TASK_REMOTE_EXECUTE,
                SPAN_TASK_TERMINAL_PERSIST,
            ]
            enq, started, finished = spans
            assert enq.context is not None
            assert started.context is not None and started.parent is not None
            assert finished.context is not None and finished.parent is not None
            assert started.context.trace_id == enq.context.trace_id
            assert started.parent.span_id == enq.context.span_id
            assert finished.context.trace_id == enq.context.trace_id
            assert finished.parent.span_id == enq.context.span_id
        finally:
            if prev_django_conf is None:
                sys.modules.pop("django.conf", None)
            else:
                sys.modules["django.conf"] = prev_django_conf
            configure_task_tracing(prev_coord)
            clear_tracing_caches()
            provider.shutdown()


@pytest.mark.django_db
class TestR2027R2028R2037BackendsAndProtocolStatus:
    """R2-027, R2-028, R2-037: Lazy external input poll, fail-loud signal refs, DB-clock status."""

    def test_get_result_and_refresh_lazily_load_external_inputs_only_on_access(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        input_dir = tmp_path / "lazy_inputs"
        input_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 32,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(input_dir),
        }
        backend = _make_backend()
        big_payload = "w" * 256
        enqueued = backend.enqueue(_plain_w4_task, args=(big_payload,), kwargs={})

        load_calls: list[str] = []
        original_load = FilesystemResultStorage.load

        def spy_load(self: FilesystemResultStorage, *, reference: str) -> str | None:
            load_calls.append(reference)
            return original_load(self, reference=reference)

        monkeypatch.setattr(FilesystemResultStorage, "load", spy_load)

        polled = backend.get_result(enqueued.id)
        assert polled.status == TaskResultStatus.READY
        assert "args=<external>" in repr(polled)
        assert load_calls == []

        polled.refresh()
        assert load_calls == []

        # First .args access triggers exactly one lazy download and caches both args and kwargs.
        assert polled.args == [big_payload]
        assert polled.kwargs == {}
        assert len(load_calls) == 1

    def test_signal_task_result_fails_loud_on_external_result_and_input_references(self) -> None:
        snapshot = {
            "task_id": "signal-ext-001",
            "callable_path": "testproject.tasks.add_numbers",
            "queue_name": "default",
            "state": TaskState.SUCCEEDED,
            "attempt_number": 1,
            "input_reference": "s3://private-bucket/inputs/abc.json?bytes=100",
            "result_reference": "s3://private-bucket/results/def.json?bytes=200",
        }
        result = _build_signal_task_result(snapshot)

        assert result.status == TaskResultStatus.SUCCESSFUL
        with pytest.raises(ResultUnavailableError) as res_err:
            _ = result.return_value
        assert res_err.value.reason == "external_result_deferred_in_signal"
        assert "private-bucket" not in str(res_err.value)

        with pytest.raises(InputPayloadUnavailableError) as in_err:
            _ = result.args
        assert in_err.value.reason == "external_input_deferred_in_signal"
        assert "private-bucket" not in str(in_err.value)

    def test_backend_check_install_hint_mentions_django_ray(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "ray", None)
        errors = _make_backend().check()
        assert len(errors) == 1
        assert "pip install django-ray" in str(errors[0].hint)

    def test_protocol_status_annotation_uses_db_clock_and_collapsed_subqueries(self) -> None:
        RayTaskExecution.objects.create(
            task_id="proto-db-clock-001",
            callable_path="testproject.tasks.add_numbers",
            execution_protocol_version=1,
            state=TaskState.QUEUED,
        )
        with CaptureQueriesContext(connection) as ctx:
            rows = list(
                annotate_execution_protocol_availability(
                    RayTaskExecution.objects.filter(task_id="proto-db-clock-001"),
                    observed_at=None,
                ).values_list("task_id", "protocol_compatible_worker_available")
            )
        assert len(ctx.captured_queries) == 1
        sql = ctx.captured_queries[0]["sql"].upper()
        assert "LEASE_EXPIRES_AT" in sql
        assert any(
            token in sql
            for token in ("CURRENT_TIMESTAMP", "STATEMENT_TIMESTAMP", "NOW()", "STRFTIME(")
        )
        assert sql.count("EXISTS") <= 7
        assert rows == [("proto-db-clock-001", False)]


@pytest.mark.django_db
class TestR2031R2032R2036DoctorReplayAndPurgeCursor:
    """R2-031, R2-032, R2-036: Doctor PG pool, replay durable context/drift/cancel, cursor DB."""

    def test_doctor_probe_accepts_and_strips_postgresql_pool_option(self) -> None:
        valid_source = SimpleNamespace(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "appdb",
                    "USER": "app",
                    "PASSWORD": "pw",
                    "HOST": "localhost",
                    "PORT": 5432,
                    "OPTIONS": {"sslmode": "require"},
                }
            }
        )
        cloned = probe.readonly_database(valid_source, "default", time.monotonic() + 10)
        assert "pool" not in cloned["OPTIONS"]
        assert cloned["OPTIONS"]["sslmode"] == "require"
        assert "default_transaction_read_only=on" in cloned["OPTIONS"]["options"]

        for unsupported_pool in (True, False, {"min_size": 2, "max_size": 8}, "enabled", 123):
            bad_source = SimpleNamespace(
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "appdb",
                        "OPTIONS": {"pool": unsupported_pool},
                    }
                }
            )
            with pytest.raises(probe.ProbeUnavailableError) as exc_info:
                probe.readonly_database(bad_source, "default", time.monotonic() + 10)
            assert exc_info.value.detail == "unsupported_postgresql_options"

    def test_replay_provides_durable_context_attempt_gte_1_cancels_cleanly_and_warns_drift(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "ALLOWED_CALLABLE_MODULES": [__name__],
            "RUNTIME_ENV_PROFILES": {"prod": {"env_vars": {"VER": "2"}}},
        }
        current_digest = resolve_runtime_env_profile("prod").digest
        stale_digest = "0" * 64
        assert stale_digest != current_digest

        durable_exec = RayTaskExecution.objects.create(
            task_id="replay-durable-ctx-001",
            callable_path=f"{__name__}._replay_durable_context_task",
            queue_name="default",
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=3,
            args_json=json.dumps(["hello"]),
            kwargs_json="{}",
            runtime_env_profile="prod",
            runtime_env_hash=stale_digest,
        )
        stdout = StringIO()
        stderr = StringIO()
        call_command(
            "django_ray_replay",
            durable_exec.task_id,
            "--json",
            stdout=stdout,
            stderr=stderr,
        )
        payload = json.loads(stdout.getvalue())
        assert payload["replay"]["status"] == "succeeded"
        assert payload["replay"]["result"] == {
            "label": "hello",
            "pk": durable_exec.pk,
            "task_id": durable_exec.task_id,
            "attempt_number": 2,
            "execution_generation": 3,
        }
        assert payload["task"]["runtime_env_drift"] is True
        assert payload["task"]["current_runtime_env_hash"] == current_digest
        assert "drifted" in stderr.getvalue().lower()

        ctx_exec = RayTaskExecution.objects.create(
            task_id="replay-attempt-ctx-001",
            callable_path=f"{__name__}._replay_django_context_attempt_task",
            queue_name="default",
            state=TaskState.FAILED,
            attempt_number=3,
            args_json=json.dumps([99]),
            kwargs_json="{}",
        )
        ctx_out = StringIO()
        call_command("django_ray_replay", ctx_exec.task_id, "--json", stdout=ctx_out)
        ctx_payload = json.loads(ctx_out.getvalue())
        assert ctx_payload["replay"]["result"]["attempt"] == 3

        cancel_exec = RayTaskExecution.objects.create(
            task_id="replay-async-cancel-001",
            callable_path=f"{__name__}._replay_cancelled_async_task",
            queue_name="default",
            state=TaskState.CANCELLED,
            args_json="[]",
            kwargs_json="{}",
        )
        cancel_out = StringIO()
        with pytest.raises(CommandError, match="CancelledError"):
            call_command("django_ray_replay", cancel_exec.task_id, "--json", stdout=cancel_out)
        cancel_payload = json.loads(cancel_out.getvalue())
        assert cancel_payload["replay"]["status"] == "failed"
        assert cancel_payload["replay"]["exception_type"] == "CancelledError"

        # Terminal escape sequences must be sanitized in text output.
        ansi_exec = RayTaskExecution.objects.create(
            task_id="replay-ansi-\x1b[31mred\x1b[0m",
            callable_path=f"{__name__}._replay_durable_context_task",
            queue_name="q\x1b[32mgreen\x1b[0m",
            state=TaskState.FAILED,
            args_json=json.dumps(["ok"]),
            kwargs_json="{}",
        )
        ansi_out = StringIO()
        call_command(
            "django_ray_replay",
            ansi_exec.task_id,
            "--dry-run",
            "--format=text",
            stdout=ansi_out,
        )
        assert "\x1b" not in ansi_out.getvalue()

    def test_execution_purge_cursor_binds_database_and_input_cursor_uses_ipc1_prefix(self) -> None:
        cutoff = datetime(2026, 10, 1, tzinfo=UTC)
        exec_cursor = ExecutionPurgeCursor(
            after_execution_pk=10,
            after_lease_id="w-1",
            cutoff=cutoff,
            older_than_days=30,
            delete=False,
            skip_leases=False,
            skip_results=False,
            database="replica",
        )
        token = encode_execution_purge_cursor(exec_cursor)
        assert decode_execution_purge_cursor(token).database == "replica"

        with pytest.raises(CommandError, match="conflicts"):
            call_command("django_ray_purge_executions", cursor=token, database="default")

        input_cursor = PurgeCursor("s3://bucket/key", cutoff, 30, False)
        input_token = encode_purge_cursor(input_cursor)
        assert decode_purge_cursor(input_token) == input_cursor
        assert decode_purge_cursor(f"ipc1.{input_token}") == input_cursor

    def test_ray_task_result_copy_pending_input_error_and_exception_path_edges(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import copy

        from django.tasks import TaskResultStatus

        from django_ray import backends as backends_mod
        from django_ray.input_storage import InputPayloadError

        # _resolve_exception_class_path edge cases: blank, >256 chars, non-identifier,
        # unknown module, missing attr, loaded custom exception, alias wrapper
        assert backends_mod._resolve_exception_class_path("   ") == "builtins.Exception"
        assert backends_mod._resolve_exception_class_path("x" * 300) == "builtins.Exception"
        assert (
            backends_mod._resolve_exception_class_path("not!valid.Err: msg") == "builtins.Exception"
        )
        assert (
            backends_mod._resolve_exception_class_path("no_such_mod_xyz.Err: msg")
            == "builtins.Exception"
        )
        assert (
            backends_mod._resolve_exception_class_path("django_ray.backends.NoSuchError: msg")
            == "builtins.Exception"
        )
        assert (
            backends_mod._builtin_exception_class_path(
                "django_ray.backends.ResultUnavailableError: gone"
            )
            == "django_ray.backends.ResultUnavailableError"
        )

        # RayTaskResult __copy__, _resolve_pending_input error redaction & InputPayloadUnavailableError
        secret_ref = "inputfs://sha256/" + ("a" * 64) + "?rel=aa/aa/secret.json&bytes=10"
        res = backends_mod.RayTaskResult(
            task=_replay_durable_context_task,
            id="copy-err-1",
            status=TaskResultStatus.READY,
            enqueued_at=None,
            started_at=None,
            finished_at=None,
            last_attempted_at=None,
            args=[],
            kwargs={},
            backend="default",
            errors=[],
            worker_ids=[],
        )
        object.__setattr__(res, "_pending_input_spec", ("copy-err-1", "[]", "{}", secret_ref))
        assert "<external>" in repr(res)
        cloned = copy.copy(res)
        assert cloned.id == "copy-err-1"

        monkeypatch.setattr(
            backends_mod,
            "load_task_input",
            lambda **_kw: (_ for _ in ()).throw(InputPayloadError(f"missing {secret_ref}")),
        )
        with pytest.raises(backends_mod.InputPayloadUnavailableError) as err_info:
            _ = res.args
        assert secret_ref not in str(err_info.value)
        assert "<unavailable>" in repr(res)

    def test_observability_plan_diagnostics_log_bounds_and_live_stream_edges(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cover observability.py plan diagnostics, binding, log bounds, and pinned log stream."""
        from django_ray import observability as obs

        execution = RayTaskExecution.objects.create(
            task_id="obs-edge-1",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.SUCCEEDED,
        )
        # Pre-supplied workflow_progress skips second read and picks summary_revision for v3
        summary = obs.get_task_summary(
            execution,
            workflow_progress={"schema_version": 3, "summary_revision": 7},
            include_workflow_plan_selection=False,
        )
        assert summary["workflow_revision"] == 7

        # get_workflow_plan_binding fails closed when plan is missing or incomplete
        with pytest.raises(obs.WorkflowObservabilityError, match="incomplete"):
            obs.get_workflow_plan_binding(execution)

        monkeypatch.setattr(
            obs,
            "_validated_workflow_plan_snapshot",
            lambda _ex: {"fingerprint": "sha256:" + ("a" * 64), "manifest": {}, "selection": None},
        )
        with pytest.raises(obs.WorkflowObservabilityError, match="incomplete"):
            obs.get_workflow_plan_binding(execution)

        # get_workflow_plan_diagnostics with mocked snapshots exercising every validation guard
        execution.workflow_plan_fingerprint = "sha256:" + ("a" * 64)
        execution.workflow_plan_json = "{}"
        execution.workflow_plan_selection = "{}"

        monkeypatch.setattr(obs, "_validated_workflow_plan_snapshot", lambda _ex: None)
        with pytest.raises(obs.WorkflowObservabilityError, match="incomplete"):
            obs.get_workflow_plan_diagnostics(execution)

        monkeypatch.setattr(
            obs,
            "_validated_workflow_plan_snapshot",
            lambda _ex: {"fingerprint": None, "manifest": {}, "selection": {}},
        )
        with pytest.raises(obs.WorkflowObservabilityError, match="incomplete"):
            obs.get_workflow_plan_diagnostics(execution)

        valid_manifest: dict[str, Any] = {
            "definition": {"name": "wf", "revision": "1"},
            "topology": {"class": "dag"},
            "nodes": [{"id": "n1"}],
            "retry_safety": {"retry_safe": True},
            "snapshot": {"observed_node_count": 3},
        }
        valid_selection: dict[str, Any] = {
            "requested_policy": "auto",
            "selected_strategy": "ray_core",
            "eligible_strategies": ["ray_core"],
            "rejections": [{"code": "NOT_SUPPORTED"}],
            "total_rejections": 2,
        }
        monkeypatch.setattr(obs, "effective_plan_selection_reporting_policy", lambda _s: "full")
        monkeypatch.setattr(
            obs,
            "_validated_workflow_plan_snapshot",
            lambda _ex: {
                "fingerprint": "sha256:" + ("b" * 64),
                "manifest": valid_manifest,
                "selection": valid_selection,
            },
        )
        diag = obs.get_workflow_plan_diagnostics(execution)
        assert diag["declared_node_count"] == 3
        assert diag["unretained_rejections"] == 1

        for bad_manifest, bad_selection, match_text in [
            (
                {**valid_manifest, "snapshot": {"observed_node_count": -1}},
                valid_selection,
                "node-count",
            ),
            (valid_manifest, {**valid_selection, "rejections": "bad"}, "rejection metadata"),
            (
                valid_manifest,
                {**valid_selection, "rejections": [{"code": "bad-lowercase"}]},
                "invalid rejection code",
            ),
            (
                valid_manifest,
                {**valid_selection, "total_rejections": 0},
                "inconsistent diagnostics",
            ),
        ]:
            monkeypatch.setattr(
                obs,
                "_validated_workflow_plan_snapshot",
                lambda _ex, m=bad_manifest, s=bad_selection: {
                    "fingerprint": "sha256:" + ("b" * 64),
                    "manifest": m,
                    "selection": s,
                },
            )
            with pytest.raises(obs.WorkflowObservabilityError, match=match_text):
                obs.get_workflow_plan_diagnostics(execution)

        # _attempt_to_dict, _validate_log_bounds, _bounded_log_text, _workflow_graph_from_progress
        assert obs._attempt_to_dict({"a": 1}) == {"a": 1}
        assert obs._attempt_to_dict(SimpleNamespace(x=2)) == {"x": 2}
        assert obs._attempt_to_dict(42) == {"record": "unsupported"}

        with pytest.raises(ValueError, match="tail"):
            obs._validate_log_bounds(tail=0, max_bytes=100)
        with pytest.raises(ValueError, match="max_bytes"):
            obs._validate_log_bounds(tail=10, max_bytes=obs.MAX_RAY_LOG_MAX_BYTES + 1)

        closed: list[bool] = []

        class _ClosableChunks:
            def __iter__(self) -> Any:
                yield "hello "
                yield "world!"

            def close(self) -> None:
                closed.append(True)

        text, truncated = obs._bounded_log_text(_ClosableChunks(), max_bytes=6)
        assert closed == [True]
        assert truncated is True
        assert text == "[TRUNCATED]"[:6]

        assert obs._workflow_graph_from_progress(execution, {"schema_version": 3}) is None
        with pytest.raises(obs.WorkflowObservabilityError, match="node and edge lists"):
            obs._workflow_graph_from_progress(execution, {"graph": {"nodes": "bad", "edges": []}})
        assert obs._workflow_graph_from_progress(execution, {"nodes": "bad"}) == {
            "nodes": [],
            "edges": [],
        }

        # _address_pinned_log_stream HTTP 302, 500, and 200 bytes/str streaming
        class _FakeResp:
            def __init__(self, status_code: int, chunks: list[Any], text: str = "err") -> None:
                self.status_code = status_code
                self._chunks = chunks
                self.text = text

            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

            def iter_content(self, chunk_size: Any = None) -> Any:
                del chunk_size
                return iter(self._chunks)

        class _FakeRayStateApiError(Exception):
            pass

        fake_state_api = SimpleNamespace(requests=SimpleNamespace(get=lambda *_a, **_kw: None))
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "ray.dashboard", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "ray.dashboard.modules", SimpleNamespace())
        monkeypatch.setitem(
            sys.modules,
            "ray.dashboard.modules.dashboard_sdk",
            SimpleNamespace(get_auth_headers_if_auth_enabled=lambda h: h),
        )
        monkeypatch.setitem(sys.modules, "ray.util", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "ray.util.state", SimpleNamespace(api=fake_state_api))
        monkeypatch.setitem(sys.modules, "ray.util.state.api", fake_state_api)
        monkeypatch.setitem(
            sys.modules,
            "ray.util.state.exception",
            SimpleNamespace(RayStateApiException=_FakeRayStateApiError),
        )

        fake_state_api.requests = None
        with pytest.raises(RuntimeError, match="ray\\[default\\]"):
            obs._address_pinned_log_stream(
                "http://127.0.0.1:8265", task_id="tid", tail=10, suffix="out", timeout=5
            )

        fake_state_api.requests = SimpleNamespace(get=lambda *_a, **_kw: _FakeResp(302, []))
        with pytest.raises(_FakeRayStateApiError, match="redirected"):
            list(
                obs._address_pinned_log_stream(
                    "http://127.0.0.1:8265", task_id="tid", tail=10, suffix="out", timeout=5
                )
            )

        fake_state_api.requests = SimpleNamespace(
            get=lambda *_a, **_kw: _FakeResp(500, [], text="backend error")
        )
        with pytest.raises(_FakeRayStateApiError, match="backend error"):
            list(
                obs._address_pinned_log_stream(
                    "http://127.0.0.1:8265", task_id="tid", tail=10, suffix="out", timeout=5
                )
            )

        fake_state_api.requests = SimpleNamespace(
            get=lambda *_a, **_kw: _FakeResp(200, [b"line1\n", "line2\n"])
        )
        assert list(
            obs._address_pinned_log_stream(
                "http://127.0.0.1:8265", task_id="tid", tail=10, suffix="out", timeout=5
            )
        ) == ["line1\n", "line2\n"]

    def test_lifecycle_start_task_hydration_and_result_reference_fence_edges(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cover lifecycle.start_task, deferred notification hydration, and PG advisory fence."""
        from django.db import connections

        from django_ray import lifecycle as lc
        from django_ray.execution_protocol import ExecutionProtocolRange

        # 1. start_task: success, stale attempt/state -> False, unsupported protocol -> False
        queued = RayTaskExecution.objects.create(
            task_id="lc-start-1",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            attempt_number=1,
            execution_generation=0,
            execution_protocol_version=1,
        )
        assert (
            lc.start_task(
                queued,
                worker_id="worker-a",
                expected_attempt_number=1,
                expected_execution_generation=0,
                supported_protocols=ExecutionProtocolRange(minimum=2, maximum=2),
            )
            is False
        )
        assert (
            lc.start_task(
                queued,
                worker_id="worker-a",
                expected_attempt_number=1,
                expected_execution_generation=0,
            )
            is True
        )
        assert queued.state == TaskState.RUNNING
        assert queued.claimed_by_worker == "worker-a"
        assert queued.execution_generation == 1
        # Second start on already-RUNNING row returns False
        assert lc.start_task(queued.pk, worker_id="worker-b") is False

        # 2. _schedule_lifecycle_notification hydrates scalar metadata on deferred rows post-commit
        deferred = RayTaskExecution.objects.only(
            "pk", "state", "attempt_number", "execution_generation"
        ).get(pk=queued.pk)
        emitted_events: list[tuple[str, Any]] = []
        monkeypatch.setattr(lc, "_is_lifecycle_tracing_enabled", lambda: True)
        monkeypatch.setattr(
            lc,
            "emit_task_lifecycle_span",
            lambda ev, obj: emitted_events.append((ev, obj.task_id)),
        )
        with transaction.atomic():
            lc.notify_task_started(deferred)
        # Inside pytest django_db (wrapped transaction), trigger on_commit hooks explicitly if queued
        for _sids, func, _robust in list(connection.run_on_commit):
            func()
        connection.run_on_commit.clear()
        assert any(ev == "started" and tid == "lc-start-1" for ev, tid in emitted_events)

        # 3. result_reference_fence: SQLite no-op + mocked PostgreSQL session & xact advisory locks
        executed_sql: list[str] = []

        class _FakeCursor:
            def __enter__(self) -> _FakeCursor:
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

            def execute(self, sql: str, params: Any = None) -> None:
                del params
                executed_sql.append(sql)

        class _FakePgConn:
            vendor = "postgresql"

            def __init__(self, in_atomic: bool) -> None:
                self.in_atomic_block = in_atomic

            def cursor(self) -> _FakeCursor:
                return _FakeCursor()

        with lc.result_reference_fence("resultfs://sha256/" + ("a" * 64)):
            pass

        orig_conn = connections["default"]
        try:
            connections["default"] = _FakePgConn(in_atomic=False)  # type: ignore[assignment]
            with lc.result_reference_fence("resultfs://sha256/" + ("b" * 64)):
                pass
            connections["default"] = _FakePgConn(in_atomic=True)  # type: ignore[assignment]
            with lc.result_reference_fence("resultfs://sha256/" + ("c" * 64)):
                pass
        finally:
            connections["default"] = orig_conn

        assert any("pg_advisory_lock" in s for s in executed_sql)
        assert any("pg_advisory_unlock" in s for s in executed_sql)
        assert any("pg_advisory_xact_lock" in s for s in executed_sql)

    def test_metrics_cache_eviction_and_replay_purge_worker_helper_edges(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cover metrics single-flight/eviction, replay CLI/coro edges, purge_inputs, and worker helpers."""
        import asyncio
        import io
        import pdb
        import sys
        import threading
        import time
        from types import SimpleNamespace

        from django.core.exceptions import ImproperlyConfigured
        from django.core.management import call_command
        from django.core.management.base import CommandError

        from django_ray import metrics as metrics_mod
        from django_ray import worker as worker_pkg
        from django_ray.conf import settings as conf_settings
        from django_ray.management.commands import django_ray_purge_inputs as purge_inputs_mod
        from django_ray.management.commands import django_ray_replay as replay_mod
        from django_ray.runtime.runtime_env import resolve_runtime_env_profile

        # 1. metrics.py: ImproperlyConfigured TTL fallback, single-flight follower, cache eviction.
        metrics_mod.clear_metrics_cache()
        monkeypatch.setattr(
            conf_settings,
            "get_settings",
            lambda: (_ for _ in ()).throw(ImproperlyConfigured("bad metrics config")),
        )
        assert metrics_mod._resolve_metrics_cache_ttl(None) == 5.0
        monkeypatch.undo()

        leader_entered = threading.Event()
        release_leader = threading.Event()
        render_calls = 0

        def _slow_render(*, allowed_queues: Any, observed_at: Any) -> str:
            nonlocal render_calls
            del allowed_queues, observed_at
            render_calls += 1
            leader_entered.set()
            assert release_leader.wait(timeout=5.0)
            return "# metrics-single-flight\n"

        monkeypatch.setattr(metrics_mod, "_render_prometheus_metrics_uncached", _slow_render)
        results: list[str] = []

        def _follower() -> None:
            results.append(metrics_mod.render_prometheus_metrics(cache_ttl_seconds=10.0))

        t_leader = threading.Thread(
            target=lambda: results.append(
                metrics_mod.render_prometheus_metrics(cache_ttl_seconds=10.0)
            )
        )
        t_leader.start()
        assert leader_entered.wait(timeout=5.0)
        t_follower = threading.Thread(target=_follower)
        t_follower.start()
        time.sleep(0.05)
        release_leader.set()
        t_leader.join(timeout=5.0)
        t_follower.join(timeout=5.0)
        assert render_calls == 1
        assert results == ["# metrics-single-flight\n", "# metrics-single-flight\n"]

        # Cache eviction: first with expired entries, then with all live entries (oldest evicted).
        monkeypatch.setattr(metrics_mod, "_METRICS_CACHE_MAX_ENTRIES", 2)
        metrics_mod.clear_metrics_cache()
        now_m = time.monotonic()
        metrics_mod._METRICS_CACHE[("expired", 1.0, "default")] = (now_m - 10.0, "old")
        metrics_mod._METRICS_CACHE[("live1", 1.0, "default")] = (now_m + 60.0, "live1")
        monkeypatch.setattr(
            metrics_mod,
            "_render_prometheus_metrics_uncached",
            lambda **_kw: "# fresh\n",
        )
        assert metrics_mod.render_prometheus_metrics(cache_ttl_seconds=30.0) == "# fresh\n"
        assert ("expired", 1.0, "default") not in metrics_mod._METRICS_CACHE
        # Now cache has 2 live entries; inserting a 3rd evicts the oldest live entry.
        metrics_mod._METRICS_CACHE[("oldest_live", 1.0, "default")] = (now_m + 1.0, "oldest")
        assert (
            metrics_mod.render_prometheus_metrics(queue_names=("q_evict",), cache_ttl_seconds=30.0)
            == "# fresh\n"
        )
        assert ("oldest_live", 1.0, "default") not in metrics_mod._METRICS_CACHE
        metrics_mod.clear_metrics_cache()
        monkeypatch.undo()

        # 2. django_ray_replay.py: matching hash, coroutine throw/pdb edges, CLI validation, post_mortem.
        default_hash = resolve_runtime_env_profile(None).digest
        assert replay_mod._check_runtime_env_drift(None, default_hash) == (
            False,
            default_hash,
            None,
        )

        async def _catching_coro() -> str:
            try:
                await asyncio.sleep(0)
            except ValueError:
                return "caught-thrown-value-error"
            return "not-thrown"

        c_throw = _catching_coro()
        first_y = c_throw.send(None)
        resumer = replay_mod._ResumeSuspendedCoroutine(c_throw, first_y)
        gen = resumer.__await__()
        next(gen)
        with pytest.raises(StopIteration) as stop_info:
            gen.throw(ValueError("injected"))
        assert stop_info.value.value == "caught-thrown-value-error"

        async def _immediate_coro() -> int:
            return 99

        monkeypatch.setattr(pdb, "runcall", lambda fn, *a, **kw: fn(*a, **kw))
        assert asyncio.run(replay_mod._run_debugged_coroutine(_immediate_coro())) == 99

        async def _crashing_coro() -> int:
            raise RuntimeError("coro-start-boom")

        with pytest.raises(RuntimeError, match="coro-start-boom"):
            asyncio.run(replay_mod._run_debugged_coroutine(_crashing_coro()))
        monkeypatch.undo()

        with pytest.raises(CommandError, match="non-empty string"):
            call_command("django_ray_replay", "   ")
        with pytest.raises(CommandError, match="Cannot combine --dry-run with --pdb"):
            call_command("django_ray_replay", "some-task", "--dry-run", "--pdb")
        with pytest.raises(CommandError, match="positive integer"):
            call_command("django_ray_replay", "some-task", "--attempt", "0")
        with pytest.raises(CommandError, match="Unknown database alias"):
            call_command("django_ray_replay", "some-task", "--database", "no_such_db")

        fail_exec = RayTaskExecution.objects.create(
            task_id="replay-pm-edge-1",
            callable_path="testproject.tasks.failing_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json="[]",
            kwargs_json="{}",
        )
        pm_called: list[bool] = []
        monkeypatch.setattr(pdb, "runcall", lambda fn, *a, **kw: fn(*a, **kw))
        monkeypatch.setattr(pdb, "post_mortem", lambda _tb: pm_called.append(True))
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        with pytest.raises(CommandError, match="failed with ValueError"):
            call_command("django_ray_replay", fail_exec.task_id, "--pdb", stdout=io.StringIO())
        assert pm_called == [True]
        monkeypatch.undo()

        # 3. django_ray_purge_inputs.py: validation errors, unregistered text output, inventory error.
        with pytest.raises(CommandError, match="non-empty database alias"):
            purge_inputs_mod.Command._validate_database("")
        with pytest.raises(CommandError, match="Unknown database alias"):
            purge_inputs_mod.Command._validate_database("missing_db_alias")
        with pytest.raises(CommandError, match="--max-batches"):
            call_command("django_ray_purge_inputs", "--max-batches", "0")
        with pytest.raises(CommandError, match="Invalid purge cursor"):
            call_command("django_ray_purge_inputs", "--cursor", "not-a-valid-cursor")

        fake_inv = SimpleNamespace(
            backend="filesystem",
            has_more=False,
            counts={
                "scanned": 2,
                "unregistered_eligible": 1,
                "unregistered_recent": 0,
                "purged": 0,
                "ambiguous": 0,
                "malformed_key": 0,
                "registered_missing": 0,
                "failed": 0,
            },
            as_dict=lambda: {"backend": "filesystem"},
        )
        monkeypatch.setattr(
            purge_inputs_mod,
            "discover_and_reap_unregistered_payloads",
            lambda **_kw: fake_inv,
        )
        out_text = io.StringIO()
        call_command("django_ray_purge_inputs", "--discover-unregistered", stdout=out_text)
        assert "Unregistered inventory (filesystem): scanned 2" in out_text.getvalue()

        monkeypatch.setattr(
            purge_inputs_mod,
            "discover_and_reap_unregistered_payloads",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("inv boom")),
        )
        out_err_text = io.StringIO()
        with pytest.raises(CommandError, match="inventory failed"):
            call_command("django_ray_purge_inputs", "--discover-unregistered", stdout=out_err_text)
        assert "inventory_failed" in out_err_text.getvalue()
        out_err_json = io.StringIO()
        with pytest.raises(CommandError, match="inventory failed"):
            call_command(
                "django_ray_purge_inputs",
                "--discover-unregistered",
                "--json",
                stdout=out_err_json,
            )
        assert '"inventory_error":"inventory_failed"' in out_err_json.getvalue()
        monkeypatch.undo()

        # 4. worker/__init__.py: command_dep fallbacks, close_old_db_connections error guards, empty queues.
        assert worker_pkg.command_dep(None, "anything", "fallback") == "fallback"
        ghost = type("Ghost", (), {"__module__": "nonexistent_mod_xyz"})()
        assert worker_pkg.command_dep(ghost, "anything", "fallback") == "fallback"
        assert worker_pkg.active_queue_task_counts(None, []) == (0, 0)

        monkeypatch.setattr(
            "django.db.connections.all",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("Database access not allowed")),
        )
        worker_pkg.close_old_db_connections(None)
        monkeypatch.setattr(
            "django.db.connections.all",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("other conn error")),
        )
        with pytest.raises(RuntimeError, match="other conn error"):
            worker_pkg.close_old_db_connections(None)
        monkeypatch.undo()

        fake_mod = SimpleNamespace(
            close_old_connections=lambda: (_ for _ in ()).throw(
                RuntimeError("Database access not allowed")
            )
        )
        monkeypatch.setitem(sys.modules, "fake_worker_cmd_mod", fake_mod)
        fake_cmd = type("FakeCmd", (), {"__module__": "fake_worker_cmd_mod"})()
        worker_pkg.close_old_db_connections(fake_cmd)
        fake_mod.close_old_connections = lambda: (_ for _ in ()).throw(RuntimeError("custom boom"))
        with pytest.raises(RuntimeError, match="custom boom"):
            worker_pkg.close_old_db_connections(fake_cmd)
