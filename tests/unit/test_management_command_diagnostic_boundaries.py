"""Adversarial tests for package-owned command rendering boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError, OutputWrapper

import django_ray.management.commands.django_ray_audit_workflow_progress as audit_command
import django_ray.management.commands.django_ray_benchmark_polling as polling_benchmark
import django_ray.management.commands.django_ray_worker as worker_command
from django_ray.management.commands.django_ray_purge_inputs import Command as PurgeInputsCommand
from django_ray.management.commands.django_ray_worker import Command as WorkerCommand
from django_ray.models import RayTaskExecution, TaskState
from django_ray.redaction import REDACTED
from django_ray.result_storage import ResultStorageError
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runner.retry import RetryDecision
from django_ray.workflow.progress.storage import WorkflowProgressStorageError


def _output_command() -> tuple[WorkerCommand, StringIO]:
    command = WorkerCommand()
    stream = StringIO()
    command.stdout = OutputWrapper(stream)
    return command, stream


def test_audit_command_redacts_storage_exception_before_command_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_audit(*_args: Any, **_kwargs: Any) -> None:
        raise WorkflowProgressStorageError("pass\x1b[31mword=do-not-expose")

    monkeypatch.setattr(audit_command, "audit_workflow_progress_detail_storage", fail_audit)

    with pytest.raises(CommandError) as exc_info:
        call_command(
            "django_ray_audit_workflow_progress",
            task_execution_pk=1,
            attempt_number=1,
            execution_generation=1,
            run_id="00000000-0000-0000-0000-000000000001",
        )

    message = str(exc_info.value)
    assert message == f"Workflow progress detail audit failed: {REDACTED}"
    assert exc_info.value.__suppress_context__ is True
    assert "do-not-expose" not in message
    assert "\x1b" not in message


def test_worker_persists_raw_failure_but_renders_only_safe_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, stream = _output_command()
    task = SimpleNamespace(pk=7, error_message=None)
    raw = "pass\x1b[31mword=durable-private-diagnostic"
    persisted: list[str] = []
    monkeypatch.setattr(
        worker_command,
        "should_retry",
        lambda *_args: RetryDecision(should_retry=False, reason="No retry configured"),
    )
    monkeypatch.setattr(
        worker_command,
        "record_failure",
        lambda *_args, **kwargs: persisted.append(kwargs["error_message"]) or True,
    )

    assert command._handle_task_failure(task, error_message=raw) is True

    assert persisted == [raw]
    assert REDACTED in stream.getvalue()
    assert "durable-private-diagnostic" not in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_worker_redacts_provider_controlled_retry_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, stream = _output_command()
    task = SimpleNamespace(pk=8, error_message=None)
    reason = "Denied Remote\x1b[31m pass\x00word=reason-private"
    monkeypatch.setattr(
        worker_command,
        "should_retry",
        lambda *_args: RetryDecision(should_retry=False, reason=reason),
    )
    monkeypatch.setattr(worker_command, "record_failure", lambda *_args, **_kwargs: True)

    assert command._handle_task_failure(task, error_message="ordinary failure") is True

    assert REDACTED in stream.getvalue()
    assert "reason-private" not in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_worker_redacts_callable_path_before_display(
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"provider-capability"]}
    command, stream = _output_command()
    task = SimpleNamespace(
        pk=9,
        callable_path="tenant.tasks.pass\x1b[31mword=provider-capability",
    )
    monkeypatch.setattr(command, "_update_lease_heartbeat", lambda: False)

    command.process_task(task)

    output = stream.getvalue()
    assert REDACTED in output
    assert "provider-capability" not in output
    assert "\x1b" not in output


@pytest.mark.django_db
def test_worker_success_status_does_not_project_application_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, stream = _output_command()
    command._create_lease("default")
    task = RayTaskExecution.objects.create(
        task_id="fixed-success-output",
        callable_path="tests.fake",
        state=TaskState.RUNNING,
        claimed_by_worker=command.worker_id,
        args_json="[]",
        kwargs_json="{}",
    )
    marker = "password=do-not-expose"
    stored: list[object] = []
    monkeypatch.setattr(
        worker_command,
        "runtime_env_for_execution",
        lambda _task: SimpleNamespace(profile="default", digest="digest"),
    )
    monkeypatch.setattr(
        "django_ray.workflow.plans.runtime_env_plan_identity",
        lambda *_args, **_kwargs: SimpleNamespace(as_transport_dict=dict),
    )
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.execute_task",
        lambda **_kwargs: f'{{"success": true, "result": "{marker}"}}',
    )
    monkeypatch.setattr(
        command,
        "_store_and_succeed_task",
        lambda _task, value, **_kwargs: stored.append(value) or True,
    )

    command.execute_task_sync(task)

    assert stored == [marker]
    assert stream.getvalue().strip() == f"Task {task.pk} succeeded"
    assert marker not in stream.getvalue()


@pytest.mark.django_db
def test_worker_result_storage_fallback_survives_broken_exception_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, stream = _output_command()
    task = RayTaskExecution.objects.create(
        task_id="diagnostic-result-storage-fallback",
        callable_path="tests.fake",
        state=TaskState.RUNNING,
        args_json="[]",
        kwargs_json="{}",
    )
    calls = 0

    class BrokenStorageError(ResultStorageError):
        def __str__(self) -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("secondary password=do-not-expose")

    class FailingStorage:
        def store(self, *, serialized_result: str) -> str:
            del serialized_result
            raise BrokenStorageError()

    monkeypatch.setattr(
        worker_command,
        "get_settings",
        lambda: {"MAX_RESULT_SIZE_BYTES": 1, "RESULT_STORAGE_BACKEND": "digest"},
    )
    monkeypatch.setattr(
        "django_ray.result_storage.get_result_storage_backend",
        lambda _settings: FailingStorage(),
    )

    assert command._store_and_succeed_task(task, {"large": "value"}) is True

    task.refresh_from_db()
    output = stream.getvalue()
    assert task.state == TaskState.SUCCEEDED
    assert task.result_data is None
    assert task.result_reference is not None
    assert "BrokenStorageError: exception message unavailable" in output
    assert "falling back to digest-only result_reference" in output
    assert "secondary password" not in output
    assert calls == 1


def test_worker_cancellation_lifecycle_survives_broken_exception_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, stream = _output_command()
    handle = SubmissionHandle(
        ray_job_id="raysubmit_pass\x1b[31mword=provider-capability",
        ray_address="ray://cluster:10001",
        submitted_at=datetime.now(UTC),
    )
    calls = 0

    class BrokenCancellationError(RuntimeError):
        def __str__(self) -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("secondary password=do-not-expose")

    def fail_cancellation(*_args: Any, **_kwargs: Any) -> None:
        raise BrokenCancellationError()

    monkeypatch.setattr(worker_command, "request_remote_cancellation", fail_cancellation)

    outcome = command._cancel_untracked_submission(
        object(),
        handle,
        backend_name="test Ray Job",
    )

    assert outcome.message == (
        "Cancellation request raised BrokenCancellationError: exception message unavailable"
    )
    assert "BrokenCancellationError: exception message unavailable" in stream.getvalue()
    assert "provider-capability" not in stream.getvalue()
    assert "secondary password" not in stream.getvalue()
    assert calls == 1


@pytest.mark.django_db
def test_worker_lease_failure_is_safe_before_structured_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, _stream = _output_command()
    command.lease_identity = WorkerLeaseIdentity(
        worker_id=command.worker_id,
        hostname="diagnostic-host",
        pid=123,
        started_at=datetime.now(UTC),
    )
    calls = 0

    class BrokenError(RuntimeError):
        def __str__(self) -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("secondary password=do-not-expose")

    error = BrokenError()
    logged: list[tuple[str, dict[str, object]]] = []
    command.logger = SimpleNamespace(
        error=lambda message, **kwargs: logged.append((message, kwargs))
    )
    monkeypatch.setattr(
        command,
        "_lock_authoritative_leases",
        lambda **_kwargs: (_ for _ in ()).throw(error),
    )

    assert command._update_lease_heartbeat() is False

    assert calls == 0
    assert logged[0][0] == "worker lease heartbeat failed"
    exc_info = logged[0][1]["exc_info"]
    assert isinstance(exc_info, tuple)
    assert exc_info[1] is error
    assert command.shutdown_requested is True


def test_polling_benchmark_retains_schema_text_and_sanitizes_worker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenWorker:
        def __init__(self) -> None:
            raise RuntimeError("api\x1b[31m_key=do-not-expose")

    monkeypatch.setattr(polling_benchmark, "WorkerCommand", BrokenWorker)
    monkeypatch.setattr(polling_benchmark, "close_old_connections", lambda: None)
    command = polling_benchmark.Command()

    group = command._start_workers(
        phase="latency",
        policy_name="adaptive",
        workers=1,
        queue_name="ordinary-queue",
        on_claim=lambda _task: None,
        base_interval=0.01,
        max_interval=0.05,
        jitter_ratio=0.2,
        barrier_timeout=1.0,
        seed=53,
    )
    group.threads[0].join(timeout=1.0)

    assert group.metrics.errors == [f"worker 0: {REDACTED}"]
    with pytest.raises(
        CommandError,
        match=r"adaptive benchmark worker failed: worker 0: \[REDACTED\]",
    ):
        command._raise_worker_error(group, "adaptive")


def test_polling_benchmark_matches_across_exception_type_and_message(
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"BoundaryCanaryError: provider marker"]}
    error_type = type("BoundaryCanaryError", (RuntimeError,), {})

    class BrokenWorker:
        def __init__(self) -> None:
            raise error_type("provider marker")

    monkeypatch.setattr(polling_benchmark, "WorkerCommand", BrokenWorker)
    monkeypatch.setattr(polling_benchmark, "close_old_connections", lambda: None)
    command = polling_benchmark.Command()

    group = command._start_workers(
        phase="latency",
        policy_name="adaptive",
        workers=1,
        queue_name="ordinary-queue",
        on_claim=lambda _task: None,
        base_interval=0.01,
        max_interval=0.05,
        jitter_ratio=0.2,
        barrier_timeout=1.0,
        seed=53,
    )
    group.threads[0].join(timeout=1.0)

    assert group.metrics.errors == [f"worker 0: {REDACTED}"]
    assert "BoundaryCanaryError" not in group.metrics.errors[0]
    assert "provider marker" not in group.metrics.errors[0]


def test_purge_input_error_inventory_never_materializes_provider_message() -> None:
    calls = 0

    class ProviderDeleteError(RuntimeError):
        def __str__(self) -> str:
            nonlocal calls
            calls += 1
            return "password=do-not-expose"

    rendered = PurgeInputsCommand._format_cleanup_error(ProviderDeleteError())

    assert rendered == "ProviderDeleteError"
    assert calls == 0
