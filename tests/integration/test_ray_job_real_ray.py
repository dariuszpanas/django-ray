"""Real local-Ray integration tests for RayJobRunner and django_ray_worker Command.handle().

Exercises the default RayJobRunner (`submit`, `get_status`, `get_logs`,
`cancel_with_status`) and real `django_ray_worker.Command.handle()` execution
(without mocking `run_loop`, `_create_lease`, or runners) against a bounded
local Ray runtime.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.db import connections

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.ray_job_protocol import STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX
from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.runner.ray_job import RayJobRunner
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.runtime.serialization import serialize_args
from tests.local_ray import local_ray_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _pythonpath_for_subprocess() -> str:
    return os.pathsep.join(
        path
        for path in (
            str(PROJECT_ROOT / "src"),
            str(PROJECT_ROOT),
            os.environ.get("PYTHONPATH", ""),
        )
        if path
    )


def _prepare_migrated_sqlite_db(db_path: Path) -> Path:
    """Create and migrate an isolated SQLite database for Ray Job subprocesses."""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "testproject.settings",
        "DATABASE_ENGINE": "django.db.backends.sqlite3",
        "DATABASE_NAME": str(db_path),
        "PYTHONPATH": _pythonpath_for_subprocess(),
    }
    subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "testproject" / "manage.py"),
            "migrate",
            "--no-input",
            "--verbosity",
            "0",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with contextlib.closing(sqlite3.connect(db_path)) as sqlite_conn:
        sqlite_conn.execute("PRAGMA journal_mode=WAL")
        sqlite_conn.execute("PRAGMA busy_timeout=30000")
    return db_path


@contextlib.contextmanager
def _use_shared_migrated_sqlite_db(tmp_path: Path, filename: str) -> Generator[Path]:
    """Bind both the in-process Django connection and Ray Job subprocesses to one temp DB."""
    db_path = _prepare_migrated_sqlite_db(tmp_path / filename)
    conn = connections["default"]
    original_conn_name = conn.settings_dict["NAME"]
    original_conn_options = dict(conn.settings_dict.get("OPTIONS") or {})
    original_settings_name = django_settings.DATABASES["default"]["NAME"]
    original_settings_options = dict(django_settings.DATABASES["default"].get("OPTIONS") or {})
    # Keep an anchor handle open while swapping away from pytest-django's
    # shared-cache in-memory SQLite DB; closing the sole connection destroys
    # all in-memory tables for subsequent @pytest.mark.django_db tests.
    in_memory_anchor: sqlite3.Connection | None = None
    if isinstance(original_conn_name, str) and "mode=memory" in original_conn_name:
        in_memory_anchor = sqlite3.connect(original_conn_name, uri=True)
    merged_options = {**original_conn_options, "timeout": 30}
    # Update NAME before calling conn.close(): Django's sqlite3 DatabaseWrapper.close()
    # intentionally treats close() as a no-op while is_in_memory_db() is True, which
    # would otherwise leave conn.connection pinned to the in-memory DB until a later
    # close_old_connections() silently switched it to the empty file DB mid-test.
    conn.settings_dict["NAME"] = str(db_path)
    conn.settings_dict["OPTIONS"] = merged_options
    django_settings.DATABASES["default"]["NAME"] = str(db_path)
    django_settings.DATABASES["default"]["OPTIONS"] = merged_options
    conn.close()
    try:
        yield db_path
    finally:
        conn.close()
        conn.settings_dict["NAME"] = original_conn_name
        conn.settings_dict["OPTIONS"] = original_conn_options
        django_settings.DATABASES["default"]["NAME"] = original_settings_name
        django_settings.DATABASES["default"]["OPTIONS"] = original_settings_options
        if in_memory_anchor is not None:
            try:
                conn.ensure_connection()
            finally:
                in_memory_anchor.close()


def _resolve_local_dashboard_address(ray_module: Any) -> str:
    """Resolve the active local Ray dashboard HTTP URL without hardcoding port 8265."""
    address_info = getattr(
        getattr(getattr(ray_module, "_private", None), "worker", None),
        "global_worker",
        None,
    )
    node = getattr(address_info, "node", None)
    info = getattr(node, "address_info", None)
    webui_url = info.get("webui_url") if isinstance(info, dict) else None
    if not isinstance(webui_url, str) or not webui_url.strip():
        raise RuntimeError("Local Ray runtime did not report a dashboard webui_url")
    normalized = webui_url.strip().rstrip("/")
    if "://" not in normalized:
        normalized = f"http://{normalized}"
    return normalized


def _wait_for_jobs_api(address: str, *, timeout_seconds: float = 15.0) -> None:
    """Wait until the local Ray Jobs HTTP server accepts requests."""
    from ray.job_submission import JobSubmissionClient

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = JobSubmissionClient(address)
            client.list_jobs()
            return
        except Exception as exc:  # pragma: no cover - transient dashboard warmup
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Local Ray Jobs API at {address} did not become ready: {last_error}")


def _poll_job_until_terminal(
    runner: RayJobRunner,
    handle: SubmissionHandle,
    *,
    timeout_seconds: float = 45.0,
) -> JobStatus:
    deadline = time.monotonic() + timeout_seconds
    status = JobStatus.PENDING
    while time.monotonic() < deadline:
        status = runner.get_status(handle).status
        if status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED}:
            return status
        time.sleep(0.2)
    return status


class _BoundedShutdownWorkerCommand(Command):
    """Run real `Command.handle()` and stop cleanly once `stop_when()` is true.

    Inherits `handle()`, `_create_lease()`, `_initialize_ray_execution()`,
    `run_loop()`, claiming, polling, and `shutdown()` unmodified while
    providing a single-threaded bounded exit condition compatible with SQLite
    `:memory:` and PostgreSQL.
    """

    def __init__(
        self,
        *,
        stop_when: Callable[[], bool],
        deadline_seconds: float = 45.0,
    ) -> None:
        super().__init__(stdout=io.StringIO(), stderr=io.StringIO())
        self._stop_when = stop_when
        self._deadline = time.monotonic() + deadline_seconds

    def _wait_for_poll_deadline(self, timeout: float) -> None:
        if self._stop_when() or time.monotonic() >= self._deadline:
            self.shutdown_requested = True
            self._shutdown_event.set()
            return
        super()._wait_for_poll_deadline(min(timeout, 0.1))


def _run_handle_preserving_signals(command: Command, **options: Any) -> None:
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        command.handle(**options)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def _create_claimed_execution(
    *,
    task_id: str,
    callable_path: str,
    args: list[Any],
    kwargs: dict[str, Any],
    ray_target_address: str,
    sqlite_db_path: Path,
) -> tuple[RayTaskExecution, TaskWorkerLease]:
    command = Command(stdout=io.StringIO(), stderr=io.StringIO())
    command._create_lease("default")
    assert command.lease is not None
    runtime_env = normalize_runtime_env(
        {
            "env_vars": {
                "DJANGO_SETTINGS_MODULE": "testproject.settings",
                "DATABASE_ENGINE": "django.db.backends.sqlite3",
                "DATABASE_NAME": str(sqlite_db_path),
                "PYTHONPATH": _pythonpath_for_subprocess(),
            }
        }
    )
    execution = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path=callable_path,
        queue_name="default",
        state=TaskState.RUNNING,
        claimed_by_worker=command.worker_id,
        args_json=serialize_args(args),
        kwargs_json=serialize_args(kwargs),
        runtime_env_json=runtime_env.serialized,
        runtime_env_hash=runtime_env.digest,
        ray_target_address=ray_target_address,
        attempt_number=1,
        execution_generation=1,
    )
    return execution, command.lease


@pytest.mark.real_ray
@pytest.mark.django_db(transaction=True)
def test_ray_job_runner_submit_status_logs_and_cancel_on_real_ray(
    settings: Any,
    tmp_path: Path,
) -> None:
    """Exercise RayJobRunner.submit/get_status/get_logs/cancel_with_status on real local Ray."""
    with (
        _use_shared_migrated_sqlite_db(tmp_path, "ray_job_runner.sqlite3") as db_path,
        local_ray_runtime(include_dashboard=True, wrap_startup_error=True, isolated=True) as ray,
    ):
        dashboard_url = _resolve_local_dashboard_address(ray)
        _wait_for_jobs_api(dashboard_url)

        settings.DJANGO_RAY = {
            **getattr(settings, "DJANGO_RAY", {}),
            "RUNNER": "ray_job",
            "RAY_ADDRESS": dashboard_url,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "rq2-requests"),
        }

        runner = RayJobRunner()

        # 1. Submit a real task through RayJobRunner.submit() and verify completion + logs + DB persistence.
        execution, _lease = _create_claimed_execution(
            task_id="real-ray-job-runner-success",
            callable_path="testproject.tasks.add_numbers",
            args=[19, 23],
            kwargs={},
            ray_target_address=dashboard_url,
            sqlite_db_path=db_path,
        )
        handle = runner.submit(execution, "testproject.tasks.add_numbers", (19, 23), {})
        try:
            assert handle.ray_job_id.startswith(
                STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX
            )
            assert handle.ray_address == dashboard_url
            execution.refresh_from_db()
            assert execution.ray_job_id == handle.ray_job_id
            assert execution.ray_job_request_reference is not None

            status = _poll_job_until_terminal(runner, handle, timeout_seconds=45.0)
            logs = runner.get_logs(handle) or ""
            assert status == JobStatus.SUCCEEDED, logs
            assert "django-ray task completed successfully" in logs
            execution.refresh_from_db()
            assert execution.completion_data is not None
            assert json.loads(execution.completion_data)["result"] == 42
        finally:
            if runner.get_status(handle).status not in {
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.STOPPED,
            }:
                runner.cancel(handle)

        # 2. Submit a long-running task and verify RayJobRunner.cancel_with_status() stops it.
        slow_execution, _slow_lease = _create_claimed_execution(
            task_id="real-ray-job-runner-cancel",
            callable_path="testproject.tasks.slow_task",
            args=[30.0],
            kwargs={},
            ray_target_address=dashboard_url,
            sqlite_db_path=db_path,
        )
        slow_handle = runner.submit(slow_execution, "testproject.tasks.slow_task", (30.0,), {})
        try:
            outcome = runner.cancel_with_status(slow_handle)
            assert outcome.status == CancellationOutcomeStatus.REQUESTED
            final_status = _poll_job_until_terminal(runner, slow_handle, timeout_seconds=30.0)
            assert final_status == JobStatus.STOPPED
        finally:
            if runner.get_status(slow_handle).status not in {
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.STOPPED,
            }:
                runner.cancel(slow_handle)


@pytest.mark.real_ray
@pytest.mark.django_db(transaction=True)
def test_worker_command_handle_executes_task_end_to_end_with_real_ray_core(
    settings: Any,
) -> None:
    """Exercise real `Command.handle(--local)` claim -> Ray Core execute -> persist -> shutdown."""
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "RUNNER": "ray_core",
        "RAY_ADDRESS": "auto",
        "WORKER_POLL_INTERVAL_SECONDS": 0.05,
        "WORKER_POLL_MAX_INTERVAL_SECONDS": 0.1,
    }

    runtime_env = normalize_runtime_env(
        {
            "env_vars": {
                "DJANGO_SETTINGS_MODULE": "testproject.settings",
                "PYTHONPATH": _pythonpath_for_subprocess(),
            }
        }
    )
    execution = RayTaskExecution.objects.create(
        task_id="real-ray-worker-handle-core",
        callable_path="testproject.tasks.add_numbers",
        queue_name="default",
        state=TaskState.QUEUED,
        args_json=serialize_args([17, 25]),
        kwargs_json=serialize_args({}),
        runtime_env_json=runtime_env.serialized,
        runtime_env_hash=runtime_env.digest,
        attempt_number=1,
        execution_generation=1,
    )

    with local_ray_runtime(num_cpus=2, include_dashboard=False, isolated=True):
        command = _BoundedShutdownWorkerCommand(
            stop_when=lambda: RayTaskExecution.objects.filter(
                pk=execution.pk,
                state__in=(TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED),
            ).exists(),
            deadline_seconds=45.0,
        )
        _run_handle_preserving_signals(
            command,
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=1,
            sync=False,
            local=True,
            cluster=None,
            verbosity=1,
        )

    execution.refresh_from_db()
    assert execution.state == TaskState.SUCCEEDED, execution.error_message
    assert execution.result_data is not None
    assert json.loads(execution.result_data) == 42
    assert command.lease_identity is not None
    lease = TaskWorkerLease.objects.get(**command.lease_identity.database_filters())
    assert lease.is_active is False
    assert lease.stopped_at is not None


@pytest.mark.real_ray
@pytest.mark.django_db(transaction=True)
def test_worker_command_handle_submits_and_hands_off_real_ray_job(
    settings: Any,
    tmp_path: Path,
) -> None:
    """Exercise real `Command.handle(--ray-job)` claim -> rq2 submit -> completion reconcile -> SUCCEEDED."""
    with (
        _use_shared_migrated_sqlite_db(tmp_path, "worker_ray_job.sqlite3") as db_path,
        local_ray_runtime(include_dashboard=True, wrap_startup_error=True, isolated=True) as ray,
    ):
        dashboard_url = _resolve_local_dashboard_address(ray)
        _wait_for_jobs_api(dashboard_url)

        settings.DJANGO_RAY = {
            **getattr(settings, "DJANGO_RAY", {}),
            "RUNNER": "ray_job",
            "RAY_ADDRESS": dashboard_url,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "worker-rq2-requests"),
            "WORKER_POLL_INTERVAL_SECONDS": 0.05,
            "WORKER_POLL_MAX_INTERVAL_SECONDS": 0.1,
        }
        runtime_env = normalize_runtime_env(
            {
                "env_vars": {
                    "DJANGO_SETTINGS_MODULE": "testproject.settings",
                    "DATABASE_ENGINE": "django.db.backends.sqlite3",
                    "DATABASE_NAME": str(db_path),
                    "PYTHONPATH": _pythonpath_for_subprocess(),
                }
            }
        )
        execution = RayTaskExecution.objects.create(
            task_id="real-ray-worker-handle-ray-job",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json=serialize_args([11, 31]),
            kwargs_json=serialize_args({}),
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            ray_target_address=dashboard_url,
            attempt_number=1,
            execution_generation=1,
        )

        command = _BoundedShutdownWorkerCommand(
            stop_when=lambda: RayTaskExecution.objects.filter(
                pk=execution.pk,
                state__in=(TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED),
            ).exists(),
            deadline_seconds=60.0,
        )
        _run_handle_preserving_signals(
            command,
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=1,
            sync=False,
            local=False,
            ray_job=True,
            cluster=None,
            verbosity=1,
        )

        execution.refresh_from_db()
        assert execution.ray_job_id is not None
        assert execution.ray_job_id.startswith(
            STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX
        )
        assert execution.ray_job_request_reference is not None
        assert execution.completion_data is not None
        assert execution.state == TaskState.SUCCEEDED, execution.error_message
        assert execution.result_data is not None
        assert json.loads(execution.result_data) == 42
        assert command.lease_identity is not None
        lease = TaskWorkerLease.objects.get(**command.lease_identity.database_filters())
        assert lease.is_active is False
        assert lease.stopped_at is not None
