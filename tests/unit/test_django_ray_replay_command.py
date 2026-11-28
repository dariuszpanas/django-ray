"""Unit tests for the read-only ``django_ray_replay`` local time-travel debugger."""

from __future__ import annotations

import asyncio
import json
import pdb
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.tasks import TaskContext, task

from django_ray.decorators import ray_task
from django_ray.input_storage import prepare_task_input
from django_ray.management.commands.django_ray_replay import Command
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.redaction import REDACTED


@ray_task(num_cpus=1, timeout_seconds=30, queue_name="default")
def _replay_add_task(left: int, right: int, *, api_key: str = "safe") -> dict[str, Any]:
    return {"sum": left + right, "api_key": api_key}


_DRY_RUN_INVOCATIONS: list[tuple[Any, ...]] = []


@ray_task(num_cpus=1, timeout_seconds=30, queue_name="default")
def _replay_dry_run_guard_task(left: int, right: int, *, api_key: str = "safe") -> int:
    _DRY_RUN_INVOCATIONS.append((left, right, api_key))
    raise AssertionError("Callable must not be invoked during --dry-run")


@task(queue_name="default")
def _replay_failing_task(secret_token: str) -> None:
    raise ValueError(f"boom with token={secret_token}")


@task(queue_name="default")
async def _replay_async_task(value: int) -> int:
    await asyncio.sleep(0)
    return value * 10


@task(queue_name="default")
def _replay_awaitable_wrapper_task(value: int) -> Any:
    async def _inner() -> int:
        await asyncio.sleep(0)
        return value + 100

    return _inner()


@task(queue_name="default", takes_context=True)
def _replay_context_task(context: TaskContext, label: str) -> dict[str, str]:
    return {"task_id": context.task_result.id, "label": label}


def _replay_workflow_double(value: int) -> int:
    return value * 2


@task(queue_name="default")
def _replay_nested_local_workflow_task(value: int) -> dict[str, Any]:
    from django_ray.runtime.context import get_current_task_context
    from django_ray.workflows import step

    ctx = get_current_task_context()
    computed = step(_replay_workflow_double).run(value, use_ray=False)
    return {
        "computed": computed,
        "read_only_replay": ctx.read_only_replay if ctx is not None else None,
        "attempt_number": ctx.attempt_number if ctx is not None else None,
        "execution_generation": ctx.execution_generation if ctx is not None else None,
    }


def _not_a_django_task(value: int) -> int:
    return value


def _snapshot_execution_state(execution_pk: int) -> dict[str, Any]:
    row = RayTaskExecution.objects.get(pk=execution_pk)
    attempts = list(
        TaskAttempt.objects.filter(execution_id=execution_pk)
        .order_by("attempt_number")
        .values(
            "attempt_number",
            "state",
            "error_message",
            "error_traceback",
            "result_data",
            "result_reference",
        )
    )
    return {
        "state": row.state,
        "attempt_number": row.attempt_number,
        "execution_generation": row.execution_generation,
        "args_json": row.args_json,
        "kwargs_json": row.kwargs_json,
        "input_reference": row.input_reference,
        "result_data": row.result_data,
        "result_reference": row.result_reference,
        "error_message": row.error_message,
        "error_traceback": row.error_traceback,
        "finished_at": row.finished_at,
        "attempts": attempts,
    }


class TestDjangoRayReplayCLI:
    """Verify CLI flag help completeness and argument validation."""

    def test_every_argument_has_explicit_help_text(self) -> None:
        command = Command()
        parser = command.create_parser("manage.py", "django_ray_replay")
        for action in parser._actions:
            if action.dest == "help":
                continue
            assert action.help, f"Argument {action.option_strings or action.dest} missing help="

    def test_conflicting_and_invalid_flags_fail_closed(self) -> None:
        with pytest.raises(CommandError, match="Cannot combine --dry-run with --pdb"):
            call_command("django_ray_replay", "task-1", "--dry-run", "--pdb")

        with pytest.raises(CommandError, match="--attempt must be a positive integer"):
            call_command("django_ray_replay", "task-1", "--attempt", "0")

        with pytest.raises(CommandError, match="task_id must be a non-empty string"):
            call_command("django_ray_replay", "   ")

        with pytest.raises(CommandError, match="Unknown database alias"):
            call_command("django_ray_replay", "task-1", "--database", "nonexistent_db")


@pytest.mark.django_db
class TestDjangoRayReplayExecution:
    """Verify read-only replay, external inputs, security boundary, redaction, and pdb."""

    def test_local_replay_succeeds_redacts_by_default_and_never_mutates_db(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        execution = RayTaskExecution.objects.create(
            task_id="replay-inline-1",
            callable_path=f"{__name__}._replay_add_task",
            queue_name="default",
            priority=5,
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=3,
            args_json=json.dumps([20, 22]),
            kwargs_json=json.dumps({"api_key": "super-secret-api-key"}),
            error_message="failed using password=hunter2",
        )
        before = _snapshot_execution_state(execution.pk)

        stdout = StringIO()
        stderr = StringIO()
        call_command(
            "django_ray_replay",
            execution.task_id,
            "--json",
            stdout=stdout,
            stderr=stderr,
        )

        assert stderr.getvalue() == ""
        payload = json.loads(stdout.getvalue())
        assert payload["schema"] == "django-ray.replay"
        assert payload["schema_version"] == 1
        assert payload["read_only"] is True
        assert payload["mode"] == "local"
        assert payload["sensitive_redacted"] is True
        assert payload["replay"]["status"] == "succeeded"
        assert payload["replay"]["duration_ms"] >= 0
        # Secret key and value are redacted by default.
        assert "super-secret-api-key" not in stdout.getvalue()
        assert "hunter2" not in stdout.getvalue()
        assert payload["replay"]["result"]["sum"] == 42
        assert payload["task"]["contract"]["num_cpus"] == 1.0
        assert payload["task"]["contract"]["timeout_seconds"] == 30
        assert _snapshot_execution_state(execution.pk) == before

    def test_show_sensitive_emits_stderr_audit_warning_and_reveals_values(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        execution = RayTaskExecution.objects.create(
            task_id="replay-sensitive-1",
            callable_path=f"{__name__}._replay_add_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps([1, 2]),
            kwargs_json=json.dumps({"api_key": "raw-secret-123"}),
        )

        stdout = StringIO()
        stderr = StringIO()
        call_command(
            "django_ray_replay",
            execution.task_id,
            "--format=json",
            "--show-sensitive",
            stdout=stdout,
            stderr=stderr,
        )

        assert "--show-sensitive" in stderr.getvalue()
        assert "WARNING" in stderr.getvalue()
        payload = json.loads(stdout.getvalue())
        assert payload["sensitive_redacted"] is False
        assert payload["inputs"]["kwargs"] == {"api_key": "raw-secret-123"}
        assert payload["replay"]["result"] == {"sum": 3, "api_key": "raw-secret-123"}

    def test_dry_run_does_not_invoke_callable_and_renders_text(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        _DRY_RUN_INVOCATIONS.clear()
        execution = RayTaskExecution.objects.create(
            task_id="replay-dry-run-1",
            callable_path=f"{__name__}._replay_dry_run_guard_task",
            queue_name="default",
            state=TaskState.FAILED,
            runtime_env_profile="project",
            runtime_env_hash="a" * 64,
            args_json=json.dumps([3, 4]),
            kwargs_json=json.dumps({"api_key": "hidden-key"}),
            error_message="transient upstream error",
        )
        before = _snapshot_execution_state(execution.pk)

        stdout = StringIO()
        call_command("django_ray_replay", execution.task_id, "--dry-run", stdout=stdout)

        output = stdout.getvalue()
        assert _DRY_RUN_INVOCATIONS == []
        assert "mode=dry_run" in output
        assert "Replay Status: dry_run" in output
        assert "RuntimeEnv:" in output
        assert "Contract:" in output
        assert "Original Err:" in output
        assert "hidden-key" not in output
        assert REDACTED in output
        assert _snapshot_execution_state(execution.pk) == before

    def test_replays_spilled_external_filesystem_input(
        self,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        input_dir = tmp_path / "inputs"
        input_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "ALLOWED_CALLABLE_MODULES": [__name__],
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(input_dir),
            "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
        }
        large_padding = "x" * 2048
        prepared = prepare_task_input([15, 27], {"api_key": large_padding})
        assert prepared.input_reference is not None

        execution = RayTaskExecution.objects.create(
            task_id="replay-external-1",
            callable_path=f"{__name__}._replay_add_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=prepared.args_json,
            kwargs_json=prepared.kwargs_json,
            input_reference=prepared.input_reference,
        )
        before = _snapshot_execution_state(execution.pk)
        files_before = sorted(p.name for p in input_dir.iterdir())

        stdout = StringIO()
        call_command("django_ray_replay", execution.task_id, "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload["task"]["input_external"] is True
        assert payload["replay"]["status"] == "succeeded"
        assert payload["replay"]["result"]["sum"] == 42
        assert _snapshot_execution_state(execution.pk) == before
        assert sorted(p.name for p in input_dir.iterdir()) == files_before

    def test_attempt_flag_loads_archived_attempt_and_rejects_unknown_attempt(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        execution = RayTaskExecution.objects.create(
            task_id="replay-attempt-1",
            callable_path=f"{__name__}._replay_add_task",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            attempt_number=2,
            args_json=json.dumps([5, 6]),
            kwargs_json=json.dumps({}),
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="attempt 1 failed",
        )
        before = _snapshot_execution_state(execution.pk)

        stdout = StringIO()
        call_command(
            "django_ray_replay",
            execution.task_id,
            "--attempt",
            "1",
            "--dry-run",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())
        assert payload["task"]["attempt_number"] == 1
        assert payload["task"]["attempt_archived"] is True
        assert payload["task"]["original_state"] == TaskState.FAILED
        assert payload["task"]["original_error"] == "attempt 1 failed"
        assert _snapshot_execution_state(execution.pk) == before

        with pytest.raises(CommandError, match="Attempt 9 was not found"):
            call_command("django_ray_replay", execution.task_id, "--attempt", "9", "--dry-run")

    def test_replays_async_and_takes_context_tasks_without_db_mutation(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        async_exec = RayTaskExecution.objects.create(
            task_id="replay-async-1",
            callable_path=f"{__name__}._replay_async_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps([7]),
            kwargs_json=json.dumps({}),
        )
        ctx_exec = RayTaskExecution.objects.create(
            task_id="replay-ctx-1",
            callable_path=f"{__name__}._replay_context_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps(["hello"]),
            kwargs_json=json.dumps({}),
        )
        before_async = _snapshot_execution_state(async_exec.pk)
        before_ctx = _snapshot_execution_state(ctx_exec.pk)

        out_async = StringIO()
        call_command("django_ray_replay", async_exec.task_id, "--json", stdout=out_async)
        assert json.loads(out_async.getvalue())["replay"]["result"] == 70
        assert _snapshot_execution_state(async_exec.pk) == before_async

        out_ctx = StringIO()
        call_command("django_ray_replay", ctx_exec.task_id, "--json", stdout=out_ctx)
        assert json.loads(out_ctx.getvalue())["replay"]["result"] == {
            "task_id": "replay-ctx-1",
            "label": "hello",
        }
        assert _snapshot_execution_state(ctx_exec.pk) == before_ctx

    def test_failing_replay_outputs_redacted_diagnostics_and_exits_nonzero(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        execution = RayTaskExecution.objects.create(
            task_id="replay-fail-1",
            callable_path=f"{__name__}._replay_failing_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps(["top-secret-xyz"]),
            kwargs_json=json.dumps({}),
        )
        before = _snapshot_execution_state(execution.pk)

        stdout = StringIO()
        with pytest.raises(CommandError, match="ValueError") as exc_info:
            call_command("django_ray_replay", execution.task_id, "--json", stdout=stdout)

        assert exc_info.value.returncode == 1
        output = stdout.getvalue()
        assert "top-secret-xyz" not in output
        payload = json.loads(output)
        assert payload["replay"]["status"] == "failed"
        assert payload["replay"]["exception_type"] == "ValueError"
        assert payload["replay"]["error"] == REDACTED
        assert _snapshot_execution_state(execution.pk) == before

    def test_pdb_flag_dispatches_via_pdb_runcall(
        self,
        settings: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        runcall_invocations: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

        def fake_runcall(func: Any, *args: Any, **kwargs: Any) -> Any:
            runcall_invocations.append((func, args, kwargs))
            return func(*args, **kwargs)

        monkeypatch.setattr(pdb, "runcall", fake_runcall)
        execution = RayTaskExecution.objects.create(
            task_id="replay-pdb-1",
            callable_path=f"{__name__}._replay_add_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps([9, 11]),
            kwargs_json=json.dumps({}),
        )

        stdout = StringIO()
        call_command("django_ray_replay", execution.task_id, "--pdb", "--json", stdout=stdout)

        assert len(runcall_invocations) == 1
        assert runcall_invocations[0][0] is _replay_add_task.func
        assert runcall_invocations[0][1] == (9, 11)
        payload = json.loads(stdout.getvalue())
        assert payload["mode"] == "pdb"
        assert payload["replay"]["status"] == "succeeded"
        assert payload["replay"]["result"]["sum"] == 20

    def test_enforces_resolve_task_callable_security_boundary(
        self,
        settings: Any,
    ) -> None:
        # 1. Non-Task plain function is rejected even when module is allowed.
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        plain_exec = RayTaskExecution.objects.create(
            task_id="replay-sec-plain",
            callable_path=f"{__name__}._not_a_django_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps([1]),
            kwargs_json=json.dumps({}),
        )
        with pytest.raises(CommandError, match="Failed to resolve task callable"):
            call_command("django_ray_replay", plain_exec.task_id, "--dry-run")

        # 2. Disallowed module is rejected before import.
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": ["testproject.tasks"]}
        disallowed_exec = RayTaskExecution.objects.create(
            task_id="replay-sec-blocked",
            callable_path=f"{__name__}._replay_add_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps([1, 2]),
            kwargs_json=json.dumps({}),
        )
        with pytest.raises(CommandError, match="ALLOWED_CALLABLE_MODULES"):
            call_command("django_ray_replay", disallowed_exec.task_id, "--dry-run")

        # 3. Missing task_id fails closed cleanly.
        with pytest.raises(CommandError, match="No RayTaskExecution found"):
            call_command("django_ray_replay", "does-not-exist", "--dry-run")

    def test_text_output_for_succeeded_and_failed_replays_and_corrupt_input(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        ok_exec = RayTaskExecution.objects.create(
            task_id="replay-text-ok",
            callable_path=f"{__name__}._replay_add_task",
            queue_name="default",
            state=TaskState.FAILED,
            attempt_number=2,
            args_json=json.dumps([4, 5]),
            kwargs_json=json.dumps({}),
        )
        out_ok = StringIO()
        call_command(
            "django_ray_replay",
            ok_exec.task_id,
            "--attempt",
            "2",
            "--format=text",
            stdout=out_ok,
        )
        assert "Replay Status: succeeded" in out_ok.getvalue()
        assert "Result:" in out_ok.getvalue()

        fail_exec = RayTaskExecution.objects.create(
            task_id="replay-text-fail",
            callable_path=f"{__name__}._replay_failing_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps(["secret-val"]),
            kwargs_json=json.dumps({}),
        )
        out_fail = StringIO()
        with pytest.raises(CommandError, match="ValueError"):
            call_command("django_ray_replay", fail_exec.task_id, "--format=text", stdout=out_fail)
        assert "Replay Status: failed" in out_fail.getvalue()
        assert "Traceback:" in out_fail.getvalue()
        assert "secret-val" not in out_fail.getvalue()

        corrupt_exec = RayTaskExecution.objects.create(
            task_id="replay-corrupt-input",
            callable_path=f"{__name__}._replay_add_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json="{not-valid-json",
            kwargs_json="{}",
        )
        with pytest.raises(CommandError, match="Failed to load persisted task inputs"):
            call_command("django_ray_replay", corrupt_exec.task_id, "--dry-run")

    def test_pdb_flag_with_async_task(
        self,
        settings: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        seen: list[Any] = []

        def fake_runcall(func: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(func)
            return func(*args, **kwargs)

        monkeypatch.setattr(pdb, "runcall", fake_runcall)
        async_exec = RayTaskExecution.objects.create(
            task_id="replay-pdb-async-1",
            callable_path=f"{__name__}._replay_async_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps([4]),
            kwargs_json=json.dumps({}),
        )
        stdout = StringIO()
        call_command("django_ray_replay", async_exec.task_id, "--pdb", "--json", stdout=stdout)
        assert len(seen) == 1
        assert getattr(seen[0], "__name__", None) == "send"
        assert json.loads(stdout.getvalue())["replay"]["result"] == 40

    def test_awaitable_wrapper_show_sensitive_failure_running_loop_and_db_error(
        self,
        settings: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from django.db import DatabaseError

        from django_ray.management.commands import django_ray_replay as replay_mod

        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        wrapper_exec = RayTaskExecution.objects.create(
            task_id="replay-awaitable-1",
            callable_path=f"{__name__}._replay_awaitable_wrapper_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps([7]),
            kwargs_json=json.dumps({}),
        )
        out = StringIO()
        call_command("django_ray_replay", wrapper_exec.task_id, "--json", stdout=out)
        assert json.loads(out.getvalue())["replay"]["result"] == 107

        # Running event loop rejects awaitable wrapper cleanly and closes coroutine.
        async def _run_inside_loop() -> None:
            replay_mod._invoke_replayed_task(
                _replay_awaitable_wrapper_task,
                wrapper_exec,
                [7],
                {},
                use_pdb=False,
            )

        with pytest.raises(RuntimeError, match="running event loop"):
            asyncio.run(_run_inside_loop())

        # --show-sensitive exposes unredacted original/replay error text with stderr warning.
        fail_exec = RayTaskExecution.objects.create(
            task_id="replay-sensitive-fail",
            callable_path=f"{__name__}._replay_failing_task",
            queue_name="default",
            state=TaskState.FAILED,
            args_json=json.dumps(["raw-token-999"]),
            kwargs_json=json.dumps({}),
            error_message="orig password=raw-pw",
        )
        out_sens = StringIO()
        err_sens = StringIO()
        with pytest.raises(CommandError, match="ValueError"):
            call_command(
                "django_ray_replay",
                fail_exec.task_id,
                "--json",
                "--show-sensitive",
                stdout=out_sens,
                stderr=err_sens,
            )
        assert "WARNING" in err_sens.getvalue()
        sens_payload = json.loads(out_sens.getvalue())
        assert "raw-token-999" in sens_payload["replay"]["error"]
        assert sens_payload["task"]["original_error"] == "orig password=raw-pw"

        # DatabaseError reading execution maps cleanly to CommandError(returncode=2).
        class _BrokenManager:
            def using(self, alias: str) -> Any:
                return self

            def defer(self, *fields: str) -> Any:
                return self

            def get(self, **kwargs: Any) -> Any:
                raise DatabaseError("connection lost")

        monkeypatch.setattr(replay_mod.RayTaskExecution, "objects", _BrokenManager())
        with pytest.raises(CommandError, match="Database error reading") as db_exc:
            call_command("django_ray_replay", "replay-sensitive-fail", "--dry-run")
        assert db_exc.value.returncode == 2

    def test_replay_nested_workflow_detached_for_failed_and_running_tasks(
        self,
        settings: Any,
    ) -> None:
        """Replaying a FAILED or RUNNING task with nested workflows must not pin or mutate DB."""
        from datetime import UTC, datetime

        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        initial_hb = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

        # 1. Replaying a FAILED task that executes a nested local workflow succeeds
        #    without hitting WorkflowPlanMismatchError from stale-row plan pinning.
        failed_exec = RayTaskExecution.objects.create(
            task_id="replay-nested-wf-failed",
            callable_path=f"{__name__}._replay_nested_local_workflow_task",
            queue_name="default",
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=4,
            args_json=json.dumps([21]),
            kwargs_json=json.dumps({}),
            last_heartbeat_at=initial_hb,
        )
        out_failed = StringIO()
        call_command("django_ray_replay", failed_exec.task_id, "--json", stdout=out_failed)
        failed_payload = json.loads(out_failed.getvalue())
        assert failed_payload["replay"]["status"] == "succeeded"
        assert failed_payload["replay"]["result"] == {
            "computed": 42,
            "read_only_replay": True,
            "attempt_number": 2,
            "execution_generation": 4,
        }
        failed_exec.refresh_from_db()
        assert failed_exec.workflow_plan_json is None
        assert failed_exec.workflow_plan_fingerprint is None
        assert failed_exec.workflow_plan_selection is None
        assert failed_exec.workflow_run_id is None
        assert failed_exec.last_heartbeat_at == initial_hb

        # 2. Replaying an active RUNNING task must NOT mutate workflow plan fields or heartbeat.
        running_exec = RayTaskExecution.objects.create(
            task_id="replay-nested-wf-running",
            callable_path=f"{__name__}._replay_nested_local_workflow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=2,
            args_json=json.dumps([15]),
            kwargs_json=json.dumps({}),
            last_heartbeat_at=initial_hb,
        )
        out_running = StringIO()
        call_command("django_ray_replay", running_exec.task_id, "--json", stdout=out_running)
        running_payload = json.loads(out_running.getvalue())
        assert running_payload["replay"]["status"] == "succeeded"
        assert running_payload["replay"]["result"]["computed"] == 30
        assert running_payload["replay"]["result"]["read_only_replay"] is True
        running_exec.refresh_from_db()
        assert running_exec.state == TaskState.RUNNING
        assert running_exec.workflow_plan_json is None
        assert running_exec.workflow_plan_fingerprint is None
        assert running_exec.workflow_plan_selection is None
        assert running_exec.workflow_run_id is None
        assert running_exec.last_heartbeat_at == initial_hb
