"""Read-only local time-travel replay debugger for persisted django-ray tasks."""

from __future__ import annotations

import asyncio
import json
import pdb
import sys
import time
import traceback
from datetime import datetime
from inspect import isawaitable, iscoroutine, iscoroutinefunction
from typing import Any, cast

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError, connections
from django.tasks import TaskContext, TaskResult, TaskResultStatus
from django.tasks.base import Task
from django.utils.connection import ConnectionDoesNotExist

from django_ray.decorators import get_ray_task_contract
from django_ray.input_storage import InputPayloadError, load_task_input
from django_ray.models import RayTaskExecution, TaskAttempt
from django_ray.redaction import (
    materialize_exception_message,
    normalize_terminal_text,
    redact_text,
    redact_value,
    safe_exception_type_name,
)
from django_ray.runtime.context import durable_task_execution
from django_ray.runtime.import_utils import resolve_task_callable
from django_ray.runtime.runtime_env import resolve_runtime_env_profile

REPLAY_REPORT_SCHEMA = "django-ray.replay"
REPLAY_REPORT_SCHEMA_VERSION = 1

_SENSITIVE_AUDIT_WARNING = (
    "WARNING: --show-sensitive enabled; unredacted task inputs, outputs, and errors "
    "may be displayed. Handle output carefully and do not copy secrets into logs or tickets."
)

_DEFERRED_EXECUTION_FIELDS = (
    "runtime_env_json",
    "progress_data",
    "workflow_progress_summary_json",
    "workflow_plan_json",
    "completion_data",
)


def _format_json_safe(value: Any, *, show_sensitive: bool) -> Any:
    if not show_sensitive:
        return redact_value(value)
    return value


def _format_text_safe(value: str | None, *, show_sensitive: bool) -> str | None:
    if value is None:
        return None
    if not show_sensitive:
        return redact_text(value)
    return value


def _sanitize_terminal(value: str | None) -> str:
    """Strip terminal escape/control sequences from operator-printed strings."""
    if not value:
        return ""
    return normalize_terminal_text(value)


def _check_runtime_env_drift(
    profile: str | None,
    recorded_hash: str | None,
) -> tuple[bool, str | None, str | None]:
    """Compare persisted runtime_env_hash against current local configuration."""
    if not recorded_hash:
        return False, None, None
    try:
        resolved = resolve_runtime_env_profile(profile)
    except ImproperlyConfigured:
        return True, None, "profile_unavailable"
    current_hash = resolved.digest
    if current_hash != recorded_hash:
        return True, current_hash, "hash_mismatch"
    return False, current_hash, None


def _build_replay_worker_ids(
    execution: RayTaskExecution,
    *,
    attempt_number: int,
) -> list[str]:
    """Build non-empty worker_ids so Django 6 ``TaskContext.attempt >= 1``."""
    effective_attempt = max(1, int(attempt_number))
    prior = [f"attempt-{idx}" for idx in range(1, effective_attempt)]
    final_worker = (
        str(execution.claimed_by_worker)
        if execution.claimed_by_worker
        else f"replay-attempt-{effective_attempt}"
    )
    return [*prior, final_worker]


def _build_replay_context(
    resolved_task: Task,
    execution: RayTaskExecution,
    args: list[Any],
    kwargs: dict[str, Any],
    *,
    attempt_number: int | None = None,
) -> TaskContext:
    """Build an in-memory read-only Django 6 ``TaskContext`` for local replay."""
    effective_attempt = (
        max(1, int(attempt_number))
        if attempt_number is not None
        else max(1, int(cast(Any, execution.attempt_number)))
    )
    snapshot = TaskResult(
        task=resolved_task,
        id=str(execution.task_id),
        status=TaskResultStatus.RUNNING,
        enqueued_at=cast(datetime | None, execution.created_at),
        started_at=cast(datetime | None, execution.started_at),
        finished_at=cast(datetime | None, execution.finished_at),
        last_attempted_at=cast(datetime | None, execution.started_at),
        args=list(args),
        kwargs=dict(kwargs),
        backend=resolved_task.backend,
        errors=[],
        worker_ids=_build_replay_worker_ids(execution, attempt_number=effective_attempt),
    )
    return TaskContext(task_result=snapshot)


def _ensure_no_running_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "django_ray_replay cannot execute an async task from a thread "
        "that already has a running event loop"
    )


class _ResumeSuspendedCoroutine:
    """Drive a coroutine whose first step already suspended under pdb."""

    __slots__ = ("_coro", "_first_yield")

    def __init__(self, coro: Any, first_yield: Any) -> None:
        self._coro = coro
        self._first_yield = first_yield

    def __await__(self) -> Any:
        yielded = self._first_yield
        while True:
            try:
                sent = yield yielded
            except BaseException as exc:
                try:
                    yielded = self._coro.throw(exc)
                except StopIteration as done:
                    return done.value
            else:
                try:
                    yielded = self._coro.send(sent)
                except StopIteration as done:
                    return done.value


async def _run_debugged_coroutine(coro: Any) -> Any:
    """Step into the coroutine body under pdb, then await any remaining suspensions."""
    try:
        first_yield = pdb.runcall(coro.send, None)
    except StopIteration as done:
        return done.value
    except BaseException:
        coro.close()
        raise
    return await _ResumeSuspendedCoroutine(coro, first_yield)


def _invoke_replayed_task(
    resolved_task: Task,
    execution: RayTaskExecution,
    args: list[Any],
    kwargs: dict[str, Any],
    *,
    use_pdb: bool,
    attempt_number: int | None = None,
) -> Any:
    """Invoke a resolved Django ``Task`` locally without touching database state."""
    effective_attempt = (
        max(1, int(attempt_number))
        if attempt_number is not None
        else max(1, int(cast(Any, execution.attempt_number)))
    )
    target = resolved_task.func
    call_args: tuple[Any, ...] = (
        (
            _build_replay_context(
                resolved_task,
                execution,
                args,
                kwargs,
                attempt_number=effective_attempt,
            ),
            *args,
        )
        if resolved_task.takes_context
        else tuple(args)
    )

    # Mark the replayed execution context as read_only_replay=True so tasks can
    # still inspect attempt_number and execution_generation while nested workflows
    # stay detached and never pin plans or mutate heartbeats on the persisted row.
    with durable_task_execution(
        int(cast(Any, execution.pk)),
        task_id=str(execution.task_id),
        execution_protocol_version=int(cast(Any, execution.execution_protocol_version)),
        attempt_number=effective_attempt,
        execution_generation=int(cast(Any, execution.execution_generation)),
        runtime_env_profile=cast(str | None, execution.runtime_env_profile),
        runtime_env_hash=str(execution.runtime_env_hash or ""),
        read_only_replay=True,
    ):
        if iscoroutinefunction(target):
            _ensure_no_running_event_loop()
            if use_pdb:

                async def _await_debugged_coro() -> Any:
                    coro = target(*call_args, **kwargs)
                    return await _run_debugged_coroutine(coro)

                return asyncio.run(_await_debugged_coro())
            return asyncio.run(target(*call_args, **kwargs))

        if use_pdb:
            outcome = pdb.runcall(target, *call_args, **kwargs)
        else:
            outcome = target(*call_args, **kwargs)

        if not isawaitable(outcome):
            return outcome

        try:
            _ensure_no_running_event_loop()
        except RuntimeError:
            if iscoroutine(outcome):
                outcome.close()
            raise
        if use_pdb and iscoroutine(outcome):
            return asyncio.run(_run_debugged_coroutine(outcome))
        return asyncio.run(outcome)


def render_replay_report_json(report: dict[str, Any]) -> str:
    """Serialize a replay report as canonical JSON."""
    return json.dumps(report, sort_keys=True, default=repr)


def render_replay_report_text(report: dict[str, Any]) -> str:
    """Render a concise human-readable summary of a local task replay."""
    task_info = report["task"]
    inputs_info = report["inputs"]
    replay_info = report["replay"]

    lines = [
        f"django-ray replay (read-only, mode={_sanitize_terminal(str(report['mode']))})",
        f"  Task ID:       {_sanitize_terminal(str(task_info['id']))}",
        f"  Callable:      {_sanitize_terminal(str(task_info['callable_path']))}",
        f"  Database:      {_sanitize_terminal(str(report['database']))}",
        (
            f"  Queue:         {_sanitize_terminal(str(task_info['queue_name']))} "
            f"(priority={task_info['priority']})"
        ),
        (
            f"  Attempt:       {task_info['attempt_number']} "
            f"(archived={task_info['attempt_archived']}, "
            f"state={_sanitize_terminal(str(task_info['original_state']))})"
        ),
        f"  Redacted:      {report['sensitive_redacted']}",
        f"  External Input:{' yes' if task_info['input_external'] else ' no'}",
    ]
    if task_info["runtime_env_profile"] or task_info["runtime_env_hash"]:
        profile_text = _sanitize_terminal(task_info["runtime_env_profile"]) or "-"
        hash_text = _sanitize_terminal(task_info["runtime_env_hash"]) or "-"
        lines.append(f"  RuntimeEnv:    profile={profile_text} hash={hash_text}")
    if task_info.get("runtime_env_drift"):
        current_hash = (
            _sanitize_terminal(task_info.get("current_runtime_env_hash")) or "unavailable"
        )
        lines.append(
            f"  RuntimeEnv Drift: WARNING local config hash ({current_hash}) "
            "differs from persisted execution snapshot"
        )
    if task_info["contract"] is not None:
        lines.append(
            "  Contract:      "
            + _sanitize_terminal(json.dumps(task_info["contract"], sort_keys=True, default=repr))
        )
    if task_info["original_error"]:
        lines.append(f"  Original Err:  {_sanitize_terminal(str(task_info['original_error']))}")
    lines.append(
        "  Args:          "
        + _sanitize_terminal(json.dumps(inputs_info["args"], sort_keys=True, default=repr))
    )
    lines.append(
        "  Kwargs:        "
        + _sanitize_terminal(json.dumps(inputs_info["kwargs"], sort_keys=True, default=repr))
    )
    lines.append(f"  Replay Status: {_sanitize_terminal(str(replay_info['status']))}")
    if replay_info["duration_ms"] is not None:
        lines.append(f"  Duration:      {replay_info['duration_ms']:.3f} ms")
    if replay_info["status"] == "succeeded":
        lines.append(
            "  Result:        "
            + _sanitize_terminal(json.dumps(replay_info["result"], sort_keys=True, default=repr))
        )
    elif replay_info["status"] == "failed":
        exc_type = _sanitize_terminal(str(replay_info["exception_type"]))
        err_text = _sanitize_terminal(str(replay_info["error"]))
        lines.append(f"  Exception:     {exc_type}: {err_text}")
        if replay_info["traceback"]:
            lines.append("  Traceback:")
            for tb_line in _sanitize_terminal(str(replay_info["traceback"])).splitlines():
                lines.append(f"    {tb_line}")
    return "\n".join(lines)


class Command(BaseCommand):
    """Replay a persisted task locally in-process without mutating database rows."""

    help = (
        "Replay a persisted django-ray task locally in-process without mutating "
        "database records or external storage artifacts."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "task_id",
            type=str,
            help="Durable Django task_id of the RayTaskExecution record to replay.",
        )
        parser.add_argument(
            "--attempt",
            type=int,
            default=None,
            help="Specific 1-based attempt number to inspect metadata for during replay.",
        )
        parser.add_argument(
            "--database",
            type=str,
            default="default",
            help="Django database alias to read the persisted execution from (default: default).",
        )
        parser.add_argument(
            "--format",
            choices=("text", "json"),
            default="text",
            help="Output format for the replay report: text or json (default: text).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit canonical JSON output (equivalent to --format=json).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Resolve the callable and inspect persisted inputs/metadata without "
                "invoking the task callable."
            ),
        )
        parser.add_argument(
            "--pdb",
            action="store_true",
            help="Invoke the resolved task callable under pdb.runcall for interactive debugging.",
        )
        parser.add_argument(
            "--show-sensitive",
            action="store_true",
            help=(
                "Display unredacted task inputs, outputs, and errors (emits an explicit "
                "audit warning to stderr)."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        task_id: str = str(options["task_id"]).strip()
        attempt_opt: int | None = options["attempt"]
        using: str = str(options["database"])
        fmt_opt: str | None = options["format"]
        as_json_flag: bool = bool(options["as_json"])
        dry_run: bool = bool(options["dry_run"])
        use_pdb: bool = bool(options["pdb"])
        show_sensitive: bool = bool(options["show_sensitive"])

        if not task_id:
            raise CommandError("task_id must be a non-empty string", returncode=2)
        if dry_run and use_pdb:
            raise CommandError("Cannot combine --dry-run with --pdb", returncode=2)
        if attempt_opt is not None and (type(attempt_opt) is not int or attempt_opt < 1):
            raise CommandError("--attempt must be a positive integer", returncode=2)

        output_json = as_json_flag or fmt_opt == "json"

        try:
            connections[using]
        except ConnectionDoesNotExist:
            raise CommandError(
                f"Unknown database alias '{_sanitize_terminal(using)}'",
                returncode=2,
            ) from None

        try:
            execution = (
                RayTaskExecution.objects.using(using)
                .defer(*_DEFERRED_EXECUTION_FIELDS)
                .get(task_id=task_id)
            )
        except RayTaskExecution.DoesNotExist:
            raise CommandError(
                f"No RayTaskExecution found for task_id '{_sanitize_terminal(redact_text(task_id))}'",
                returncode=2,
            ) from None
        except DatabaseError:
            raise CommandError(
                f"Database error reading task_id '{_sanitize_terminal(redact_text(task_id))}' "
                f"from '{_sanitize_terminal(using)}'",
                returncode=2,
            ) from None

        archived_attempt: TaskAttempt | None = None
        if attempt_opt is not None:
            archived_attempt = (
                TaskAttempt.objects.using(using)
                .filter(execution=execution, attempt_number=attempt_opt)
                .first()
            )
            if archived_attempt is None and int(execution.attempt_number) != attempt_opt:
                raise CommandError(
                    f"Attempt {attempt_opt} was not found for task "
                    f"'{_sanitize_terminal(redact_text(task_id))}'",
                    returncode=2,
                )

        try:
            resolved_task = resolve_task_callable(str(execution.callable_path))
        except (ImportError, AttributeError, TypeError, ValueError) as error:
            raise CommandError(
                f"Failed to resolve task callable "
                f"'{_sanitize_terminal(str(execution.callable_path))}': "
                f"{_sanitize_terminal(redact_text(error))}",
                returncode=2,
            ) from None

        try:
            hydrated_args, hydrated_kwargs = load_task_input(
                args_json=str(execution.args_json),
                kwargs_json=str(execution.kwargs_json),
                input_reference=(
                    str(execution.input_reference)
                    if execution.input_reference is not None
                    else None
                ),
            )
        except InputPayloadError as error:
            raise CommandError(
                f"Failed to load persisted task inputs: {_sanitize_terminal(redact_text(error))}",
                returncode=2,
            ) from None

        if show_sensitive:
            self.stderr.write(_SENSITIVE_AUDIT_WARNING)

        selected_attempt_number = (
            int(archived_attempt.attempt_number)
            if archived_attempt is not None
            else int(execution.attempt_number)
        )
        original_state = (
            str(archived_attempt.state) if archived_attempt is not None else str(execution.state)
        )
        raw_original_error = (
            archived_attempt.error_message
            if archived_attempt is not None
            else execution.error_message
        )
        contract = get_ray_task_contract(resolved_task)
        recorded_runtime_env_hash = str(execution.runtime_env_hash) or None
        runtime_env_drift, current_runtime_env_hash, runtime_env_drift_reason = (
            _check_runtime_env_drift(
                execution.runtime_env_profile,
                recorded_runtime_env_hash,
            )
        )
        if runtime_env_drift:
            self.stderr.write(
                "WARNING: Local RuntimeEnv configuration has drifted from persisted execution "
                f"snapshot (recorded={_sanitize_terminal(recorded_runtime_env_hash)}, "
                f"current={_sanitize_terminal(current_runtime_env_hash) or 'unavailable'}, "
                f"reason={runtime_env_drift_reason})."
            )

        mode = "dry_run" if dry_run else ("pdb" if use_pdb else "local")
        report: dict[str, Any] = {
            "database": using,
            "inputs": {
                "args": _format_json_safe(hydrated_args, show_sensitive=show_sensitive),
                "kwargs": _format_json_safe(hydrated_kwargs, show_sensitive=show_sensitive),
            },
            "mode": mode,
            "read_only": True,
            "replay": {
                "duration_ms": None,
                "status": "dry_run",
            },
            "schema": REPLAY_REPORT_SCHEMA,
            "schema_version": REPLAY_REPORT_SCHEMA_VERSION,
            "sensitive_redacted": not show_sensitive,
            "task": {
                "attempt_archived": archived_attempt is not None,
                "attempt_number": selected_attempt_number,
                "callable_path": str(execution.callable_path),
                "contract": contract.to_dict() if contract is not None else None,
                "current_runtime_env_hash": current_runtime_env_hash,
                "execution_generation": int(execution.execution_generation),
                "execution_pk": int(execution.pk),
                "id": str(execution.task_id),
                "input_external": execution.input_reference is not None,
                "original_error": _format_text_safe(
                    raw_original_error, show_sensitive=show_sensitive
                ),
                "original_state": original_state,
                "priority": int(execution.priority),
                "queue_name": str(execution.queue_name),
                "runtime_env_drift": runtime_env_drift,
                "runtime_env_hash": recorded_runtime_env_hash,
                "runtime_env_profile": execution.runtime_env_profile,
            },
        }

        if dry_run:
            self._emit_report(report, output_json=output_json)
            return

        started = time.perf_counter()
        try:
            return_value = _invoke_replayed_task(
                resolved_task,
                execution,
                hydrated_args,
                hydrated_kwargs,
                use_pdb=use_pdb,
                attempt_number=selected_attempt_number,
            )
        except (Exception, asyncio.CancelledError) as exc:
            if use_pdb and getattr(sys.stdin, "isatty", lambda: False)():
                pdb.post_mortem(exc.__traceback__)
            duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            exc_type = safe_exception_type_name(exc)
            raw_error = materialize_exception_message(exc)
            raw_tb = traceback.format_exc()
            report["replay"] = {
                "duration_ms": duration_ms,
                "error": _format_text_safe(raw_error, show_sensitive=show_sensitive),
                "exception_type": exc_type,
                "status": "failed",
                "traceback": _format_text_safe(raw_tb, show_sensitive=show_sensitive),
            }
            self._emit_report(report, output_json=output_json)
            raise CommandError(
                f"Replayed task '{_sanitize_terminal(str(execution.task_id))}' "
                f"failed with {_sanitize_terminal(exc_type)}",
                returncode=1,
            ) from None

        duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        report["replay"] = {
            "duration_ms": duration_ms,
            "result": _format_json_safe(return_value, show_sensitive=show_sensitive),
            "status": "succeeded",
        }
        self._emit_report(report, output_json=output_json)

    def _emit_report(self, report: dict[str, Any], *, output_json: bool) -> None:
        rendered = (
            render_replay_report_json(report) if output_json else render_replay_report_text(report)
        )
        self.stdout.write(rendered)
