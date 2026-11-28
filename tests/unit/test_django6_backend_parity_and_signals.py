"""Unit tests for Django 6 Tasks parity, on_commit signals, RayTaskResult, and tracing."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.tasks import TaskResult, TaskResultStatus, task
from django.tasks.base import Task, TaskContext
from django.tasks.exceptions import InvalidTask
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.test.utils import CaptureQueriesContext

import django_ray
from django_ray.backends import (
    InputPayloadUnavailableError,
    RayTaskBackend,
    RayTaskResult,
    ResultUnavailableError,
    build_task_context,
    build_task_context_for_active_execution,
)
from django_ray.input_storage import InputPayloadError
from django_ray.lifecycle import (
    cancel_task,
    expire_queued_tasks,
    record_failure,
    record_lost,
    request_task_cancellation,
    start_task,
    succeed_task,
)
from django_ray.models import CancellationStatus, RayTaskExecution, TaskState
from django_ray.runtime.context import durable_task_execution
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.tracing import (
    SPAN_TASK_ENQUEUE,
    SPAN_TASK_REMOTE_EXECUTE,
    SPAN_TASK_TERMINAL_PERSIST,
    InMemoryTracer,
    TaskTracingCoordinator,
    TracingConfig,
    configure_task_tracing,
)


class CustomDomainTaskError(Exception):
    """Custom exception defined in a loaded module for exception_class_path parity."""


@task(takes_context=True)
def context_aware_sample_task(context: TaskContext, value: int) -> dict[str, Any]:
    return {"attempt": context.attempt, "task_id": context.task_result.id, "value": value}


def _bad_context_func(not_context: int) -> int:
    return not_context


def _sample_plain_func(x: int) -> int:
    return x * 2


def _make_backend(*, queues: list[str] | None = None) -> RayTaskBackend:
    return RayTaskBackend(
        "default",
        {
            "QUEUES": queues if queues is not None else ["default"],
            "OPTIONS": {"RAY_ADDRESS": "auto"},
        },
    )


class TestFailLoudRayTaskResult:
    """Fail-loud RayTaskResult contract for digest stubs and unavailable payloads (F-004)."""

    def test_public_exports_and_exception_hierarchy(self) -> None:
        assert django_ray.RayTaskResult is RayTaskResult
        assert django_ray.ResultUnavailableError is ResultUnavailableError
        assert issubclass(RayTaskResult, TaskResult)
        assert issubclass(ResultUnavailableError, ValueError)
        assert issubclass(InputPayloadUnavailableError, ResultUnavailableError)
        assert issubclass(InputPayloadUnavailableError, InputPayloadError)

    @pytest.mark.django_db
    def test_oversize_digest_stub_keeps_successful_status_and_fails_loud_on_return_value(
        self,
    ) -> None:
        digest = "a" * 64
        execution = RayTaskExecution.objects.create(
            task_id="oversize-stub-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="[1, 2]",
            kwargs_json="{}",
            result_data=None,
            result_reference=f"oversize://sha256/{digest}?bytes=2097152",
        )

        result = _make_backend().get_result(execution.task_id)

        assert isinstance(result, RayTaskResult)
        assert isinstance(result, TaskResult)
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.is_finished is True
        with pytest.raises(ResultUnavailableError) as exc_info:
            _ = result.return_value

        error = exc_info.value
        assert error.task_id == "oversize-stub-001"
        assert error.digest == digest
        assert error.size_bytes == 2097152
        assert error.reason == "digest_stub"
        assert digest in str(error)
        assert "2097152" in str(error)
        assert "RESULT_STORAGE_BACKEND" in str(error)

    @pytest.mark.django_db
    def test_genuine_none_return_value_still_returns_none(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="genuine-none-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="[]",
            kwargs_json="{}",
            result_data="null",
            result_reference=None,
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value is None

    @pytest.mark.django_db
    def test_refresh_and_arefresh_propagate_and_clear_unavailable_result_state(self) -> None:
        from testproject.tasks import add_numbers

        digest = "b" * 64
        backend = _make_backend()
        result = backend.enqueue(add_numbers.using(queue_name="default"), args=(1, 2), kwargs={})
        assert result.status == TaskResultStatus.READY

        RayTaskExecution.objects.filter(task_id=result.id).update(
            state=TaskState.SUCCEEDED,
            result_data=None,
            result_reference=f"oversize://sha256/{digest}?bytes=1500000",
        )
        result.refresh()
        assert result.status == TaskResultStatus.SUCCESSFUL
        with pytest.raises(ResultUnavailableError) as exc_info:
            _ = result.return_value
        assert exc_info.value.digest == digest

        RayTaskExecution.objects.filter(task_id=result.id).update(
            result_data="3",
            result_reference=None,
        )
        async_to_sync(result.arefresh)()
        assert result.return_value == 3

    @pytest.mark.django_db
    def test_unavailable_input_fails_loud_without_breaking_status_repr_or_shallow_copy(
        self,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="bad-input-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="{invalid-json",
            kwargs_json="{invalid-json",
            result_data="42",
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == 42
        assert "args=<unavailable>" in repr(result)
        shallow = copy.copy(result)
        assert shallow.task is result.task
        with pytest.raises(InputPayloadUnavailableError):
            _ = result.args
        with pytest.raises(InputPayloadUnavailableError):
            _ = result.kwargs


class TestDjango6TasksParity:
    """Django 6 Tasks validation, takes_context, attempts, CANCELLED errors, exception paths."""

    @pytest.mark.django_db
    def test_enqueue_calls_validate_task_and_rejects_unsupported_options_early(self) -> None:
        from testproject.tasks import add_numbers

        backend = _make_backend(queues=["default"])
        wrong_queue_task = Task(
            func=_sample_plain_func,
            priority=0,
            backend="default",
            queue_name="high-priority",
            run_after=None,
        )

        with pytest.raises(InvalidTask, match="Queue 'high-priority' is not valid"):
            backend.enqueue(wrong_queue_task, args=(1,), kwargs={})

        with pytest.raises(InvalidTask, match="django.tasks.base.Task"):
            backend.enqueue(
                SimpleNamespace(
                    module_path="testproject.tasks.add_numbers",
                    queue_name="default",
                    priority=0,
                    run_after=None,
                ),  # type: ignore[arg-type]
                args=(1, 2),
                kwargs={},
            )

        assert not RayTaskExecution.objects.exists()
        enqueued = backend.enqueue(add_numbers.using(queue_name="default"), args=(1, 2), kwargs={})
        assert enqueued.status == TaskResultStatus.READY

    @pytest.mark.django_db
    def test_takes_context_validation_projection_and_task_context_construction(self) -> None:
        backend = _make_backend()

        with pytest.raises(InvalidTask, match="first argument of 'context'"):
            Task(
                func=_bad_context_func,
                priority=0,
                backend="default",
                queue_name="default",
                run_after=None,
                takes_context=True,
            )

        enqueued = backend.enqueue(context_aware_sample_task, args=(7,), kwargs={})
        fetched = backend.get_result(enqueued.id)
        assert fetched.task.takes_context is True

        execution = RayTaskExecution.objects.get(task_id=enqueued.id)
        execution.state = TaskState.RUNNING
        execution.attempt_number = 2
        execution.claimed_by_worker = "worker-ctx"
        execution.started_at = datetime.now(UTC)
        execution.save(update_fields=["state", "attempt_number", "claimed_by_worker", "started_at"])

        ctx = backend.build_task_context(execution, task=context_aware_sample_task)
        assert isinstance(ctx, TaskContext)
        assert ctx.attempt == 2
        assert ctx.task_result.id == enqueued.id
        assert ctx.task_result.args == [7]
        assert context_aware_sample_task.call(ctx, 7) == {
            "attempt": 2,
            "task_id": enqueued.id,
            "value": 7,
        }

        with durable_task_execution(
            execution.pk,
            task_id=execution.task_id,
            attempt_number=3,
            execution_generation=4,
        ):
            active_ctx = build_task_context_for_active_execution(
                context_aware_sample_task,
                args=[9],
                kwargs={},
            )
            assert isinstance(active_ctx, TaskContext)
            assert active_ctx.attempt == 3
            assert active_ctx.task_result.id == enqueued.id
            assert context_aware_sample_task.call(active_ctx, 9) == {
                "attempt": 3,
                "task_id": enqueued.id,
                "value": 9,
            }

        # Direct helper works without an explicit backend instance too.
        standalone_ctx = build_task_context(execution)
        assert standalone_ctx.attempt == 2

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("state", "attempt_number", "worker", "started", "expected_attempts"),
        [
            (TaskState.QUEUED, 1, None, False, 0),
            (TaskState.RUNNING, 1, "worker-1", True, 1),
            (TaskState.QUEUED, 2, None, False, 1),
            (TaskState.RUNNING, 3, "worker-3", True, 3),
            (TaskState.SUCCEEDED, 3, "worker-3", True, 3),
            (TaskState.FAILED, 2, "worker-2", True, 2),
            (TaskState.EXPIRED, 1, None, False, 0),
            (TaskState.CANCELLED, 2, None, False, 1),
            (TaskState.CANCELLED, 2, "worker-2", True, 2),
        ],
    )
    def test_task_result_attempts_matches_execution_attempt_history(
        self,
        state: str,
        attempt_number: int,
        worker: str | None,
        started: bool,
        expected_attempts: int,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id=f"attempts-{state.lower()}-{attempt_number}-{started}",
            callable_path="testproject.tasks.add_numbers",
            state=state,
            attempt_number=attempt_number,
            claimed_by_worker=worker,
            started_at=datetime.now(UTC) if started else None,
            result_data="1" if state == TaskState.SUCCEEDED else None,
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.attempts == expected_attempts
        assert len(result.worker_ids) == expected_attempts
        if worker and expected_attempts > 0:
            assert result.worker_ids[-1] == worker

    @pytest.mark.django_db
    def test_cancelled_execution_preserves_errors_and_exception_class(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="cancelled-errors-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLED,
            cancellation_status=CancellationStatus.REQUESTED,
            cancellation_error="Cancelled by operator",
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.status == TaskResultStatus.FAILED
        assert len(result.errors) == 1
        assert result.errors[0].exception_class is asyncio.CancelledError
        assert "Cancelled by operator" in result.errors[0].traceback

    @pytest.mark.django_db
    def test_custom_loaded_exception_class_path_is_preserved_without_arbitrary_imports(
        self,
    ) -> None:
        custom_path = f"{CustomDomainTaskError.__module__}.{CustomDomainTaskError.__qualname__}"
        exec_custom = RayTaskExecution.objects.create(
            task_id="custom-exc-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            error_message="domain invariant failed",
            error_traceback=f"Traceback (most recent call last):\n{custom_path}: domain invariant failed",
        )
        exec_django = RayTaskExecution.objects.create(
            task_id="django-exc-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            error_message="invalid input",
            error_traceback="Traceback...\ndjango.core.exceptions.ValidationError: invalid input",
        )
        exec_empty_msg = RayTaskExecution.objects.create(
            task_id="empty-msg-exc-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            error_message="ValueError",
            error_traceback="Traceback (most recent call last):\nValueError",
        )
        exec_unloaded = RayTaskExecution.objects.create(
            task_id="unloaded-exc-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            error_message="boom",
            error_traceback="Traceback...\nunloaded_never_import_me.ExploitError: boom",
        )

        backend = _make_backend()
        res_custom = backend.get_result(exec_custom.task_id)
        res_django = backend.get_result(exec_django.task_id)
        res_empty = backend.get_result(exec_empty_msg.task_id)
        res_unloaded = backend.get_result(exec_unloaded.task_id)

        assert res_custom.errors[0].exception_class_path == custom_path
        assert res_custom.errors[0].exception_class is CustomDomainTaskError
        assert res_django.errors[0].exception_class_path == "django.core.exceptions.ValidationError"
        assert res_django.errors[0].exception_class is ValidationError
        assert res_empty.errors[0].exception_class_path == "builtins.ValueError"
        assert res_unloaded.errors[0].exception_class_path == "builtins.Exception"


class TestSignalsTracingAndEfficiency:
    """Django 6 on_commit signals (F-031/F-100), tracing wiring (F-042), efficiency (F-032)."""

    @pytest.mark.django_db(transaction=True)
    def test_task_enqueued_fires_only_on_commit_never_on_rollback(self) -> None:
        from testproject.tasks import add_numbers

        received: list[str] = []

        def on_enqueued(sender: Any, task_result: TaskResult, **kwargs: Any) -> None:
            del sender, kwargs
            received.append(task_result.id)

        task_enqueued.connect(on_enqueued)
        try:
            backend = _make_backend()
            with pytest.raises(RuntimeError, match="abort"), transaction.atomic():
                backend.enqueue(add_numbers.using(queue_name="default"), args=(1, 2), kwargs={})
                assert received == []
                raise RuntimeError("abort")
            assert received == []

            with transaction.atomic():
                committed = backend.enqueue(
                    add_numbers.using(queue_name="default"), args=(3, 4), kwargs={}
                )
                assert received == []
            assert received == [committed.id]
        finally:
            task_enqueued.disconnect(on_enqueued)

    @pytest.mark.django_db(transaction=True)
    def test_lifecycle_signals_emit_post_commit_with_receiver_isolation(self) -> None:
        started_ids: list[str] = []
        finished_events: list[tuple[str, TaskResultStatus]] = []

        def failing_receiver(sender: Any, task_result: TaskResult, **kwargs: Any) -> None:
            del sender, task_result, kwargs
            raise RuntimeError("broken receiver must not crash transitions")

        def ok_started(sender: Any, task_result: TaskResult, **kwargs: Any) -> None:
            del sender, kwargs
            started_ids.append(task_result.id)

        def ok_finished(sender: Any, task_result: TaskResult, **kwargs: Any) -> None:
            del sender, kwargs
            finished_events.append((task_result.id, task_result.status))

        task_started.connect(failing_receiver)
        task_started.connect(ok_started)
        task_finished.connect(failing_receiver)
        task_finished.connect(ok_finished)
        try:
            # 1) Start + Succeed
            t_ok = RayTaskExecution.objects.create(
                task_id="sig-ok-001",
                callable_path="testproject.tasks.add_numbers",
                state=TaskState.QUEUED,
            )
            assert start_task(t_ok, worker_id="worker-sig") is True
            assert started_ids == ["sig-ok-001"]
            assert succeed_task(t_ok, result_data="5", result_reference=None) is True
            assert finished_events[-1] == ("sig-ok-001", TaskResultStatus.SUCCESSFUL)

            # 2) Retryable failure must NOT emit task_finished; terminal failure MUST.
            t_fail = RayTaskExecution.objects.create(
                task_id="sig-fail-001",
                callable_path="testproject.tasks.add_numbers",
                state=TaskState.RUNNING,
                runtime_env_hash=normalize_runtime_env({}).digest,
            )
            before_count = len(finished_events)
            assert record_failure(t_fail, error_message="retry me", retry=True) is True
            assert len(finished_events) == before_count

            assert start_task(t_fail, worker_id="worker-sig") is True
            assert record_failure(t_fail, error_message="final boom", retry=False) is True
            assert finished_events[-1] == ("sig-fail-001", TaskResultStatus.FAILED)

            # 3) Cancel (CANCELLING -> CANCELLED), Queued cancel, Lost, Expired
            t_cancel = RayTaskExecution.objects.create(
                task_id="sig-cancel-001",
                callable_path="testproject.tasks.add_numbers",
                state=TaskState.CANCELLING,
            )
            assert cancel_task(t_cancel) is True
            assert finished_events[-1] == ("sig-cancel-001", TaskResultStatus.FAILED)

            t_qcancel = RayTaskExecution.objects.create(
                task_id="sig-qcancel-001",
                callable_path="testproject.tasks.add_numbers",
                state=TaskState.QUEUED,
            )
            before_qcancel = len(finished_events)
            assert request_task_cancellation(t_qcancel.pk).accepted is True
            assert len(finished_events) == before_qcancel + 1
            assert finished_events[-1][1] == TaskResultStatus.FAILED

            t_lost = RayTaskExecution.objects.create(
                task_id="sig-lost-001",
                callable_path="testproject.tasks.add_numbers",
                state=TaskState.RUNNING,
            )
            assert record_lost(t_lost, error_message="worker crashed") is True
            assert finished_events[-1] == ("sig-lost-001", TaskResultStatus.FAILED)

            now = datetime.now(UTC)
            t_exp = RayTaskExecution.objects.create(
                task_id="sig-exp-001",
                callable_path="testproject.tasks.add_numbers",
                state=TaskState.QUEUED,
                queue_name="default",
                queue_deadline_at=now - timedelta(seconds=1),
            )
            assert expire_queued_tasks(["default"], now=now) == (t_exp.pk,)
            assert finished_events[-1] == ("sig-exp-001", TaskResultStatus.FAILED)
        finally:
            task_started.disconnect(failing_receiver)
            task_started.disconnect(ok_started)
            task_finished.disconnect(failing_receiver)
            task_finished.disconnect(ok_finished)

    @pytest.mark.django_db(transaction=True)
    def test_enqueue_uses_in_memory_inputs_without_redownload_or_unnecessary_savepoint(
        self,
        settings,
        tmp_path,
        monkeypatch,
    ) -> None:
        from testproject.tasks import add_numbers

        backend = _make_backend()

        # 1) Inline enqueue outside an outer transaction emits zero SAVEPOINTs.
        with CaptureQueriesContext(connection) as queries:
            inline_result = backend.enqueue(
                add_numbers.using(queue_name="default"),
                args=(10, 20),
                kwargs={},
            )
        sql_statements = [q["sql"].upper() for q in queries.captured_queries]
        assert not any("SAVEPOINT" in sql for sql in sql_statements)
        assert inline_result.args == [10, 20]

        # 2) Externalized input enqueue NEVER calls load_task_input after uploading.
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        }
        monkeypatch.setattr(
            "django_ray.backends.load_task_input",
            lambda **_kwargs: pytest.fail("enqueue() must not re-download external inputs"),
        )
        big_arg = "x" * 2048
        external_result = backend.enqueue(
            add_numbers.using(queue_name="default"),
            args=(big_arg,),
            kwargs={"tag": "external"},
        )
        assert external_result.args == [big_arg]
        assert external_result.kwargs == {"tag": "external"}
        row = RayTaskExecution.objects.get(task_id=external_result.id)
        assert row.input_reference is not None

    @pytest.mark.django_db
    def test_get_result_defers_heavy_json_columns(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="defer-cols-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="[1, 2]",
            kwargs_json="{}",
            result_data="3",
            progress_data='{"heavy": true}',
            workflow_progress_summary_json='{"summary": true}',
            workflow_plan_json='{"plan": true}',
            workflow_plan_selection='{"selection": true}',
            completion_data='{"completion": true}',
        )

        with CaptureQueriesContext(connection) as queries:
            result = _make_backend().get_result(execution.task_id)

        assert result.return_value == 3
        select_sql = " ".join(
            q["sql"]
            for q in queries.captured_queries
            if q["sql"].lstrip().upper().startswith("SELECT")
        )
        for deferred_column in (
            "runtime_env_json",
            "progress_data",
            "workflow_progress_summary_json",
            "workflow_plan_json",
            "workflow_plan_selection",
            "completion_data",
        ):
            assert deferred_column not in select_sql

    @pytest.mark.django_db(transaction=True)
    def test_tracing_lifecycle_spans_wired_for_enqueued_started_finished(self) -> None:
        from testproject.tasks import add_numbers

        tracer = InMemoryTracer()
        coordinator = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=tracer)
        configure_task_tracing(coordinator)
        try:
            backend = _make_backend()
            result = backend.enqueue(
                add_numbers.using(queue_name="default"),
                args=(1, 2),
                kwargs={},
            )
            execution = RayTaskExecution.objects.get(task_id=result.id)
            assert start_task(execution, worker_id="worker-trace") is True
            assert succeed_task(execution, result_data="3", result_reference=None) is True

            span_names = [span.name for span in tracer.spans]
            assert span_names == [
                SPAN_TASK_ENQUEUE,
                SPAN_TASK_REMOTE_EXECUTE,
                SPAN_TASK_TERMINAL_PERSIST,
            ]
            assert all(span.ended for span in tracer.spans)
            assert [span.attributes["django_ray.task.id"] for span in tracer.spans] == [
                result.id,
                result.id,
                result.id,
            ]
        finally:
            configure_task_tracing(None)

    def test_entrypoint_invokes_takes_context_task_with_active_context(self) -> None:
        from django_ray.runtime.entrypoint import _invoke_task_callable

        with durable_task_execution(
            77,
            task_id="takes-ctx-live-001",
            attempt_number=2,
            execution_generation=3,
        ):
            outcome = _invoke_task_callable(context_aware_sample_task, [42], {})

        assert outcome == {
            "attempt": 2,
            "task_id": "takes-ctx-live-001",
            "value": 42,
        }

    @pytest.mark.django_db(transaction=True)
    def test_worker_claim_and_process_tasks_emits_post_commit_task_started(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from django_ray.management.commands.django_ray_worker import Command as WorkerCommand

        started_ids: list[str] = []

        def on_started(sender: object, task_result: object, **_kwargs: object) -> None:
            started_ids.append(str(getattr(task_result, "id", "")))

        task_started.connect(on_started)
        try:
            execution = RayTaskExecution.objects.create(
                task_id="worker-claim-signal-001",
                callable_path="testproject.tasks.add_numbers",
                queue_name="default",
                state=TaskState.QUEUED,
                args_json="[1, 2]",
                kwargs_json="{}",
            )
            worker = WorkerCommand()
            worker.worker_id = "worker-claim-signal-test"
            worker.execution_mode = "sync"
            worker.max_concurrency = 1
            worker.shutdown_requested = False
            worker.lease_ownership_lost = False
            worker._create_lease("default")
            monkeypatch.setattr(worker, "process_task", lambda _task: None)

            claimed = worker.claim_and_process_tasks(["default"], 1)
            assert claimed == 1
            assert started_ids == [execution.task_id]
        finally:
            task_started.disconnect(on_started)
