"""Task execution dispatch, Ray init/reconnect, submission tracking, and result persistence."""

from __future__ import annotations

import copy
import inspect
import json
import os
import queue
import sys
import threading
import time as stdlib_time
import traceback
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from datetime import UTC
from threading import Event, Thread
from typing import Any, cast

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError
from django.db import connection, transaction

from django_ray.conf.settings import get_settings as default_get_settings
from django_ray.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_RANGE, ExecutionProtocolRange
from django_ray.lifecycle import (
    record_failure as default_record_failure,
)
from django_ray.lifecycle import (
    succeed_task as default_succeed_task,
)
from django_ray.management.diagnostics import (
    render_console_diagnostic,
    render_console_resource_summary,
)
from django_ray.models import RayTaskExecution, TaskState
from django_ray.redaction import (
    materialize_exception_message,
    materialize_exception_text,
    safe_exception_type_name,
)
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
    PreparedRemoteCancellation,
)
from django_ray.runner.cancellation import (
    prepare_remote_cancellation as default_prepare_remote_cancellation,
)
from django_ray.runner.cancellation import (
    request_remote_cancellation as default_request_remote_cancellation,
)
from django_ray.runner.leasing import get_heartbeat_interval as default_get_heartbeat_interval
from django_ray.runner.ray_core import RayCoreRunner as DefaultRayCoreRunner
from django_ray.runner.retry import RetryDecision, resolve_task_contract
from django_ray.runner.retry import should_retry as default_should_retry
from django_ray.runner.sync import SyncRunner
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
)
from django_ray.runtime.runtime_env import (
    runtime_env_for_execution as default_runtime_env_for_execution,
)
from django_ray.worker import (
    BUILTIN_DATETIME,
    RAY_RECONNECT_INITIAL_DELAY_SECONDS,
    RAY_RECONNECT_MAX_DELAY_SECONDS,
    SHUTDOWN_WAIT_SLICE_SECONDS,
    close_old_db_connections,
    command_dep,
    task_model,
)


def initialize_ray_execution(command: Any) -> None:
    """Initialize Ray only after this process owns its database lease."""
    if command.execution_mode == "local":
        try:
            command._init_local_ray()
            with command._preserve_shutdown_signal_handlers():
                command.ray_core_runner = command._create_ray_core_runner()
            command._reset_ray_reconnect_backoff()
        except Exception as error:
            command.ray_core_runner = None
            command._schedule_ray_reconnect_backoff()
            diagnostic = render_console_diagnostic(error)
            command.stdout.write(command.style.WARNING(f"Initial Ray init failed: {diagnostic}"))
            command.stdout.write("Will retry connection during operation...")
    elif command.execution_mode == "cluster":
        assert command.cluster_address is not None
        try:
            command._init_cluster_ray(command.cluster_address)
            with command._preserve_shutdown_signal_handlers():
                command.ray_core_runner = command._create_ray_core_runner()
            command._reset_ray_reconnect_backoff()
        except Exception as error:
            command.ray_core_runner = None
            command._schedule_ray_reconnect_backoff()
            diagnostic = render_console_diagnostic(error)
            command.stdout.write(
                command.style.WARNING(f"Initial cluster connection failed: {diagnostic}")
            )
            command.stdout.write("Will retry connection during operation...")


def create_ray_core_runner(command: Any) -> Any:
    """Retain the command's selected cluster if construction reconnects."""
    runner_cls = command_dep(command, "RayCoreRunner", DefaultRayCoreRunner)
    if command.execution_mode == "cluster":
        if command.cluster_address is None:
            raise CommandError("Cluster execution requires a selected Ray address")
        return runner_cls(address=command.cluster_address)
    return runner_cls()


def _ray_init_timeout_seconds(command: Any) -> float:
    """Bound ``ray.init()`` well within the worker lease TTL."""
    override = getattr(command, "_ray_init_timeout", None)
    if override is not None:
        return max(0.05, float(override))
    get_settings_fn = command_dep(command, "get_settings", default_get_settings)
    lease_seconds = float(
        command._lease_policy_seconds or get_settings_fn().get("WORKER_LEASE_SECONDS", 60)
    )
    return min(15.0, max(2.0, lease_seconds / 3.0))


def _ray_shutdown_timeout_seconds(command: Any) -> float:
    """Bound ``ray.shutdown()`` well within the worker lease TTL."""
    override = getattr(command, "_ray_shutdown_timeout", None)
    if override is not None:
        return max(0.05, float(override))
    get_settings_fn = command_dep(command, "get_settings", default_get_settings)
    lease_seconds = float(
        command._lease_policy_seconds or get_settings_fn().get("WORKER_LEASE_SECONDS", 60)
    )
    return min(5.0, max(1.0, lease_seconds / 4.0))


def _call_ray_with_timeout(
    operation_name: str,
    fn: Callable[[], Any],
    *,
    timeout_seconds: float,
) -> Any:
    """Run one blocking Ray control call in a daemon thread with a hard timeout."""
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _runner() -> None:
        try:
            result_queue.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - preserve KeyboardInterrupt/SystemExit
            result_queue.put(("error", exc))

    thread = threading.Thread(
        target=_runner,
        name=f"django-ray-{operation_name}",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        raise TimeoutError(f"Ray {operation_name} timed out after {timeout_seconds:g}s")

    status, payload = result_queue.get_nowait()
    if status == "error":
        raise payload
    return payload


def register_trusted_worker_ray_addresses(
    command: Any,
    settings: Mapping[str, Any] | None = None,
) -> None:
    """Register active worker ``--cluster`` / configured Ray addresses as trusted (R2-020)."""
    from django_ray.runner.ray_job import UntrustedRayAddressError, register_trusted_ray_address

    candidates: list[str] = []
    cluster_address = getattr(command, "cluster_address", None)
    if isinstance(cluster_address, str) and cluster_address.strip():
        candidates.append(cluster_address.strip())
    if settings is not None:
        configured = settings.get("RAY_ADDRESS")
        if isinstance(configured, str) and configured.strip():
            candidates.append(configured.strip())

    for candidate in candidates:
        try:
            register_trusted_ray_address(candidate)
        except (UntrustedRayAddressError, ImproperlyConfigured, ValueError) as error:
            if candidate == (cluster_address.strip() if isinstance(cluster_address, str) else None):
                diagnostic = render_console_diagnostic(error)
                raise CommandError(f"Invalid Ray cluster address: {diagnostic}") from None


def _callable_accepts_kwarg(fn: Any, kwarg_name: str) -> bool:
    """Return whether a callable accepts ``kwarg_name`` without raising ``TypeError``."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in signature.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == kwarg_name and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _apply_task_contract_for_dispatch(command: Any, task: Any) -> dict[str, Any]:
    """Resolve and apply per-task ``@ray_task`` contract metadata at dispatch (R2-005)."""
    contract = resolve_task_contract(task)
    if contract is None:
        return {}
    try:
        task._ray_task_contract = contract
    except Exception:
        pass

    if getattr(task, "timeout_seconds", None) is None and contract.timeout_seconds is not None:
        task.timeout_seconds = contract.timeout_seconds
        if isinstance(task, RayTaskExecution) and task.pk is not None:
            task_cls = task_model(command)
            task_cls.objects.filter(
                pk=task.pk,
                state=TaskState.RUNNING,
                claimed_by_worker=command.worker_id,
                attempt_number=int(task.attempt_number),
                execution_generation=int(task.execution_generation),
                timeout_seconds__isnull=True,
            ).update(timeout_seconds=contract.timeout_seconds)

    ray_options = contract.to_ray_options()
    if ray_options:
        try:
            task._ray_task_options = ray_options
        except Exception:
            pass
    return ray_options


def init_local_ray(command: Any) -> None:
    """Initialize a local Ray instance."""
    import ray

    if "RAY_ADDRESS" in os.environ:
        address = render_console_diagnostic(os.environ["RAY_ADDRESS"])
        command.stdout.write(
            command.style.WARNING(f"Clearing RAY_ADDRESS={address} for local mode")
        )
        del os.environ["RAY_ADDRESS"]

    if "RAY_RUNTIME_ENV_HOOK" in os.environ:
        del os.environ["RAY_RUNTIME_ENV_HOOK"]

    if not ray.is_initialized():
        command.stdout.write("Initializing local Ray instance...")
        with command._preserve_shutdown_signal_handlers():
            ray.init(
                ignore_reinit_error=True,
                dashboard_host="127.0.0.1",
                dashboard_port=8265,
                include_dashboard=True,
                runtime_env={"env_vars": {"PYTHONPATH": os.pathsep.join(sys.path)}},
                _system_config={
                    "enable_timeline": True,
                    "task_events_report_interval_ms": 100,
                },
            )
        command.stdout.write(command.style.SUCCESS("Ray initialized"))
        command.stdout.write(command.style.SUCCESS("  Dashboard: http://127.0.0.1:8265"))


def init_cluster_ray(command: Any, address: str) -> None:
    """Connect to a remote Ray cluster."""
    import ray

    from django_ray.runner.ray_job import UntrustedRayAddressError, register_trusted_ray_address

    try:
        register_trusted_ray_address(address)
    except (UntrustedRayAddressError, ImproperlyConfigured, ValueError) as error:
        diagnostic = render_console_diagnostic(error)
        raise CommandError(f"Invalid Ray cluster address: {diagnostic}") from None

    if ray.is_initialized():
        _call_ray_with_timeout(
            "shutdown",
            ray.shutdown,
            timeout_seconds=_ray_shutdown_timeout_seconds(command),
        )

    rendered_address = render_console_diagnostic(address)
    command.stdout.write(f"Connecting to Ray cluster at {rendered_address}...")
    init_timeout = _ray_init_timeout_seconds(command)
    with command._preserve_shutdown_signal_handlers():
        _call_ray_with_timeout(
            "init",
            lambda: ray.init(
                address=address,
                ignore_reinit_error=True,
            ),
            timeout_seconds=init_timeout,
        )
    command.stdout.write(command.style.SUCCESS("Connected to Ray cluster"))
    resources = command._get_ray_cluster_resources_with_timeout(
        timeout_seconds=command._ray_health_check_timeout_seconds()
    )
    if resources is None:
        raise TimeoutError("Ray cluster_resources timed out after connect")
    command.stdout.write(f"  Cluster resources: {render_console_resource_summary(resources)}")


def get_ray_cluster_resources_with_timeout(timeout_seconds: float) -> dict[str, Any] | None:
    """Return Ray cluster resources or None when the check times out."""
    import ray

    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _check_resources() -> None:
        try:
            result_queue.put(("ok", ray.cluster_resources()))
        except Exception as e:
            result_queue.put(("error", e))

    thread = threading.Thread(target=_check_resources, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        return None

    status, payload = result_queue.get_nowait()
    if status == "error":
        raise payload
    return payload


def is_ray_core_initialized() -> bool:
    """Return whether the process-wide Ray Core driver is initialized."""
    import ray

    return bool(ray.is_initialized())


def reset_ray_reconnect_backoff(command: Any) -> None:
    """Clear reconnect cooldown after a verified Ray Core connection."""
    command._ray_reconnect_backoff_until = 0.0
    command._ray_reconnect_delay = RAY_RECONNECT_INITIAL_DELAY_SECONDS


def schedule_ray_reconnect_backoff(command: Any) -> float:
    """Advance exponential cooldown before the next reconnect cycle."""
    time_mod = command_dep(command, "time", stdlib_time)
    delay = float(command._ray_reconnect_delay)
    command._ray_reconnect_backoff_until = time_mod.monotonic() + delay
    command._ray_reconnect_delay = min(delay * 2.0, RAY_RECONNECT_MAX_DELAY_SECONDS)
    return delay


def maybe_renew_lease_during_reconnect(
    command: Any,
    *,
    last_renewal_monotonic: float,
    elapsed_since_renewal: float,
    heartbeat_interval: float,
) -> tuple[float, float] | None:
    """Keep an owned worker lease fresh across multi-step Ray reconnect waits."""
    if command.lease_identity is None:
        return last_renewal_monotonic, elapsed_since_renewal
    time_mod = command_dep(command, "time", stdlib_time)
    wall_elapsed = max(0.0, time_mod.monotonic() - last_renewal_monotonic)
    if max(wall_elapsed, elapsed_since_renewal) < heartbeat_interval:
        return last_renewal_monotonic, elapsed_since_renewal
    if not command._update_lease_heartbeat() or command.shutdown_requested:
        return None
    return time_mod.monotonic(), 0.0


def wait_for_reconnect_delay(
    command: Any,
    delay: float,
    *,
    last_renewal_monotonic: float,
    elapsed_since_renewal: float,
    heartbeat_interval: float,
) -> tuple[float, float] | None:
    """Sleep in signal- and lease-heartbeat-safe slices during reconnect."""
    time_mod = command_dep(command, "time", stdlib_time)
    current_monotonic = last_renewal_monotonic
    current_elapsed = elapsed_since_renewal
    remaining = float(delay)
    while remaining > 0:
        if command.shutdown_requested or command._shutdown_event.is_set():
            return None
        renewal_state = command._maybe_renew_lease_during_reconnect(
            last_renewal_monotonic=current_monotonic,
            elapsed_since_renewal=current_elapsed,
            heartbeat_interval=heartbeat_interval,
        )
        if renewal_state is None:
            return None
        current_monotonic, current_elapsed = renewal_state
        step = min(remaining, SHUTDOWN_WAIT_SLICE_SECONDS)
        before = time_mod.monotonic()
        time_mod.sleep(step)
        actual = max(0.0, time_mod.monotonic() - before)
        advanced = max(actual, step)
        remaining -= advanced
        current_elapsed += advanced

    if command.shutdown_requested or command._shutdown_event.is_set():
        return None
    return command._maybe_renew_lease_during_reconnect(
        last_renewal_monotonic=current_monotonic,
        elapsed_since_renewal=current_elapsed,
        heartbeat_interval=heartbeat_interval,
    )


def ray_health_check_timeout_seconds(command: Any) -> float:
    """Bound Ray cluster resource checks well within the worker lease TTL."""
    get_settings_fn = command_dep(command, "get_settings", default_get_settings)
    lease_seconds = float(
        command._lease_policy_seconds or get_settings_fn().get("WORKER_LEASE_SECONDS", 60)
    )
    return min(15.0, max(5.0, lease_seconds / 3.0))


def check_ray_connection(command: Any) -> None:
    """Check if Ray connection is healthy and reconnect if needed."""
    import ray

    time_mod = command_dep(command, "time", stdlib_time)
    try:
        if ray.is_initialized():
            resources = command._get_ray_cluster_resources_with_timeout(
                timeout_seconds=command._ray_health_check_timeout_seconds()
            )
            if resources is not None:
                command._reset_ray_reconnect_backoff()
                return
            command.stdout.write(command.style.WARNING("\nRay health check timed out"))
        elif time_mod.monotonic() < command._ray_reconnect_backoff_until:
            return
    except Exception as e:
        diagnostic = render_console_diagnostic(e)
        command.stdout.write(command.style.WARNING(f"\nRay connection lost: {diagnostic}"))

    command._reconnect_ray()


def reconnect_ray(command: Any) -> None:
    """Attempt to reconnect to Ray cluster."""
    import ray

    time_mod = command_dep(command, "time", stdlib_time)
    get_hb_interval = command_dep(command, "get_heartbeat_interval", default_get_heartbeat_interval)

    reconnect_started = time_mod.monotonic()
    command.stdout.write(command.style.WARNING("\nAttempting to reconnect to Ray..."))

    try:
        if ray.is_initialized():
            _call_ray_with_timeout(
                "shutdown",
                ray.shutdown,
                timeout_seconds=_ray_shutdown_timeout_seconds(command),
            )
            command.stdout.write("  Shut down existing Ray connection")
    except Exception as e:
        command.stdout.write(f"  Error during shutdown: {render_console_diagnostic(e)}")

    if command.ray_core_runner and command.ray_core_runner.pending_count > 0:
        stale_count = command.ray_core_runner.pending_count
        command.stdout.write(
            command.style.WARNING(f"  Clearing {stale_count} stale Ray task references")
        )
        command._mark_stale_ray_core_tasks_as_lost()
    command.ray_core_runner = None
    command._last_partitioned_handles = ()

    heartbeat_interval = max(1.0, get_hb_interval().total_seconds())
    renewal_state = command._wait_for_reconnect_delay(
        1.0,
        last_renewal_monotonic=reconnect_started,
        elapsed_since_renewal=0.0,
        heartbeat_interval=heartbeat_interval,
    )
    if renewal_state is None:
        return
    last_renewal_monotonic, elapsed_since_renewal = renewal_state

    max_retries = 5
    retry_delay: float = 5.0
    max_retry_delay: float = 40.0

    for attempt in range(1, max_retries + 1):
        if command.shutdown_requested or command._shutdown_event.is_set():
            return
        renewal_state = command._maybe_renew_lease_during_reconnect(
            last_renewal_monotonic=last_renewal_monotonic,
            elapsed_since_renewal=elapsed_since_renewal,
            heartbeat_interval=heartbeat_interval,
        )
        if renewal_state is None:
            return
        last_renewal_monotonic, elapsed_since_renewal = renewal_state

        attempt_started = time_mod.monotonic()
        try:
            if command.execution_mode == "local":
                command._init_local_ray()
            elif command.execution_mode == "cluster" and command.cluster_address:
                command._init_cluster_ray(command.cluster_address)

            if ray.is_initialized():
                resources = command._get_ray_cluster_resources_with_timeout(
                    timeout_seconds=command._ray_health_check_timeout_seconds()
                )
                if resources is None:
                    raise TimeoutError("Ray cluster_resources timed out during reconnect")
                command.stdout.write(
                    command.style.SUCCESS(
                        f"\n  Reconnected to Ray (attempt {attempt}/{max_retries})"
                    )
                )
                command.stdout.write(
                    f"  Cluster resources: {render_console_resource_summary(resources)}"
                )

                with command._preserve_shutdown_signal_handlers():
                    command.ray_core_runner = command._create_ray_core_runner()
                command._reset_ray_reconnect_backoff()

                return

        except Exception as e:
            elapsed_since_renewal += max(0.0, time_mod.monotonic() - attempt_started)
            diagnostic = render_console_diagnostic(e)
            command.stdout.write(
                command.style.WARNING(
                    f"  Reconnection attempt {attempt}/{max_retries} failed: {diagnostic}"
                )
            )
            if attempt < max_retries:
                command.stdout.write(f"  Retrying in {retry_delay:g} seconds...")
                renewal_state = command._wait_for_reconnect_delay(
                    retry_delay,
                    last_renewal_monotonic=last_renewal_monotonic,
                    elapsed_since_renewal=elapsed_since_renewal,
                    heartbeat_interval=heartbeat_interval,
                )
                if renewal_state is None:
                    return
                last_renewal_monotonic, elapsed_since_renewal = renewal_state
                retry_delay = min(retry_delay * 2.0, max_retry_delay)

    command._schedule_ray_reconnect_backoff()
    command.stdout.write(
        command.style.ERROR(
            f"\n  Failed to reconnect after {max_retries} attempts. "
            "Worker will pause claiming new Ray Core tasks until reconnected."
        )
    )


def can_spawn_sync_heartbeat_thread(command: Any) -> bool:
    """Return whether a background heartbeat thread can safely open a DB connection."""
    if command.lease_identity is None:
        return False
    if connection.vendor == "sqlite":
        return bool(command._allow_sync_heartbeat_in_memory)
    return True


class _SyncHeartbeatTarget:
    """Thread-local lightweight task proxy to avoid cross-thread ORM mutation."""

    __slots__ = ("last_heartbeat_at", "pk")

    def __init__(self, pk: Any) -> None:
        self.pk = pk
        self.last_heartbeat_at: Any = None


@contextmanager
def sync_execution_heartbeat(command: Any, task: Any) -> Generator[None, None, None]:
    """Keep worker lease and task heartbeat alive during long --sync tasks."""
    if not isinstance(task, RayTaskExecution) or task.pk is None:
        yield
        return
    if not command._can_spawn_sync_heartbeat_thread():
        yield
        return

    get_settings_fn = command_dep(command, "get_settings", default_get_settings)
    get_hb_interval = command_dep(command, "get_heartbeat_interval", default_get_heartbeat_interval)

    stop_event = Event()
    lease_seconds = float(
        command._lease_policy_seconds or get_settings_fn().get("WORKER_LEASE_SECONDS", 60)
    )
    interval = max(
        0.25,
        min(
            get_hb_interval().total_seconds(),
            lease_seconds / 3.0,
        ),
    )
    task_pk = task.pk
    expected_worker_id = command.worker_id
    expected_attempt = int(task.attempt_number)
    expected_generation = int(task.execution_generation)
    latest_heartbeat: list[Any] = [None]

    def _beat_loop() -> None:
        proxy = _SyncHeartbeatTarget(task_pk)
        try:
            while not stop_event.wait(interval):
                if command.lease_ownership_lost:
                    return
                try:
                    close_old_db_connections(command)
                    if not command._update_lease_heartbeat():
                        if command.lease_ownership_lost:
                            return
                        continue
                    command._mark_task_monitor_heartbeat(
                        proxy,
                        expected_worker_id=expected_worker_id,
                        attempt_number=expected_attempt,
                        execution_generation=expected_generation,
                    )
                    if proxy.last_heartbeat_at is not None:
                        latest_heartbeat[0] = proxy.last_heartbeat_at
                except Exception:
                    command.logger.warning(
                        "Background --sync heartbeat failed; will retry",
                        exc_info=True,
                    )
        finally:
            close_old_db_connections(command)

    heartbeat_thread = Thread(
        target=_beat_loop,
        name=f"django-ray-sync-hb-{task_pk}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        yield
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=2.0)
        if latest_heartbeat[0] is not None:
            task.last_heartbeat_at = latest_heartbeat[0]


def prepare_result_outside_lock(
    command: Any,
    task: Any,
    result_value: Any,
    *,
    prepared_result_reference: str | None = None,
    expected_ray_job_id: str | None = None,
    expected_claimed_by_worker: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_completion_data: str | None = None,
    require_completion_data_match: bool = False,
    supported_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE,
) -> tuple[bool, bool, str | None, str | None, tuple[str, ...], Exception | None]:
    """Store oversized results outside any DB transaction/row lock."""
    if prepared_result_reference is not None:
        return True, True, None, prepared_result_reference, (), None

    get_settings_fn = command_dep(command, "get_settings", default_get_settings)
    task_cls = task_model(command)

    settings = get_settings_fn()
    max_result_size = int(settings.get("MAX_RESULT_SIZE_BYTES", 1024 * 1024))
    try:
        serialized_result = json.dumps(result_value)
    except Exception:
        return True, False, None, None, (), None
    if len(serialized_result.encode("utf-8")) <= max_result_size:
        return True, False, None, None, (), None

    if not command._preflight_own_lease_active():
        return False, False, None, None, (), None

    filters: dict[str, Any] = {"pk": task.pk, "state": TaskState.RUNNING}
    if expected_ray_job_id is not None:
        filters["ray_job_id"] = expected_ray_job_id
    if expected_claimed_by_worker is not None:
        filters["claimed_by_worker"] = expected_claimed_by_worker
    if expected_attempt_number is not None:
        filters["attempt_number"] = expected_attempt_number
    if expected_execution_generation is not None:
        filters["execution_generation"] = expected_execution_generation
    if require_completion_data_match:
        filters["completion_data"] = expected_completion_data

    protocol = (
        task_cls.objects.filter(**filters)
        .values_list("execution_protocol_version", flat=True)
        .first()
    )
    if protocol is None or not supported_protocols.supports(int(protocol)):
        return False, False, None, None, (), None

    staging = copy.copy(task)
    staging.result_data = None
    staging.result_reference = None
    try:
        diagnostics = command._store_task_result(staging, result_value) or ()
    except Exception as storage_error:
        return True, False, None, None, (), storage_error
    return (
        True,
        True,
        cast("str | None", staging.result_data),
        cast("str | None", staging.result_reference),
        tuple(diagnostics),
        None,
    )


def fenced_handle_task_failure(
    command: Any,
    task: Any,
    *args: Any,
    **kwargs: Any,
) -> bool:
    """Route failure transitions through a live lease fence whenever leased."""
    if command.lease_ownership_lost:
        return False
    if command.lease_identity is None or not isinstance(task, RayTaskExecution):
        return command._handle_task_failure(task, *args, **kwargs)
    with transaction.atomic():
        lease = command._lock_authoritative_leases(
            source_worker_id=command.worker_id,
            allow_takeover=False,
        )
        if lease is None:
            return False
        supported_protocols = command._explicit_protocol_range(lease)
        if supported_protocols is not None:
            kwargs.setdefault("supported_protocols", supported_protocols)
        return command._handle_task_failure(task, *args, **kwargs)


def fenced_persist_submission_tracking(
    command: Any,
    task: Any,
    handle: SubmissionHandle,
    *,
    expected_worker_id: str | None,
    expected_attempt_number: int,
    expected_execution_generation: int,
    expected_ray_job_id: str | None,
) -> bool:
    """Persist submission tracking only while this worker still holds its lease."""
    if command.lease_ownership_lost:
        return False
    if command.lease_identity is None or not isinstance(task, RayTaskExecution):
        return command._persist_submission_tracking(
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_ray_job_id=expected_ray_job_id,
        )
    with transaction.atomic():
        lease = command._lock_authoritative_leases(
            source_worker_id=command.worker_id,
            allow_takeover=False,
            renew_heartbeat=False,
        )
        if lease is None:
            return False
        return command._persist_submission_tracking(
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_ray_job_id=expected_ray_job_id,
        )


def process_task(command: Any, task: Any) -> None:
    """Process a single task."""
    time_mod = command_dep(command, "time", stdlib_time)
    runtime_env_fn = command_dep(
        command, "runtime_env_for_execution", default_runtime_env_for_execution
    )

    rendered_callable_path = render_console_diagnostic(task.callable_path)
    command.stdout.write(
        command.style.NOTICE(f"\nProcessing task {task.pk}: {rendered_callable_path}")
    )

    if command.lease_ownership_lost:
        return
    last_hb = float(getattr(command, "_last_successful_heartbeat_monotonic", 0.0))
    hb_interval = float(getattr(command, "_lease_heartbeat_interval_seconds", 0.0))
    if hb_interval <= 0.0:
        get_hb_interval = command_dep(
            command, "get_heartbeat_interval", default_get_heartbeat_interval
        )
        hb_interval = max(1.0, float(get_hb_interval().total_seconds()))
    heartbeat_due = last_hb <= 0.0 or (time_mod.monotonic() - last_hb) >= (hb_interval / 2.0)
    if heartbeat_due and not command._update_lease_heartbeat():
        # If heartbeat renewal failed transiently without losing lease ownership,
        # hand the already-RUNNING unsubmitted task back to QUEUED rather than
        # blackholing it until STUCK_TASK_TIMEOUT_SECONDS marks it LOST (R3-008).
        if not command.lease_ownership_lost:
            handoff = getattr(command, "_handoff_unsubmitted_task", None)
            if callable(handoff):
                close_old_db_connections(command)
                try:
                    handoff(task)
                except Exception:
                    command.logger.warning(
                        "Could not hand off unsubmitted task %s after transient lease heartbeat failure",
                        getattr(task, "pk", None),
                        exc_info=True,
                    )
        return

    command.last_task_processed = time_mod.time()
    command.tasks_processed_count += 1
    _apply_task_contract_for_dispatch(command, task)

    try:
        runtime_env_fn(task)
    except RuntimeEnvSnapshotError as error:
        command._fenced_handle_task_failure(
            task,
            error_message=materialize_exception_message(error),
            exception_type=safe_exception_type_name(error),
            retryable=False,
            expected_claimed_by_worker=command.worker_id,
            expected_attempt_number=int(task.attempt_number),
            expected_execution_generation=int(task.execution_generation),
        )
        return

    if command.execution_mode == "sync":
        command.execute_task_sync(task)
    elif command.execution_mode in ("local", "cluster"):
        command.submit_task_to_ray_core(task)
    else:
        command.submit_task_to_ray(task)


def execute_task_sync(command: Any, task: Any) -> None:
    """Execute a task synchronously via ``SyncRunner``."""
    runtime_env_fn = command_dep(
        command, "runtime_env_for_execution", default_runtime_env_for_execution
    )
    expected_attempt_number = int(task.attempt_number)
    expected_execution_generation = int(task.execution_generation)
    expected_worker_id = command.worker_id

    if getattr(command, "sync_runner", None) is None:
        command.sync_runner = SyncRunner(runtime_env_resolver=runtime_env_fn)
    runner = command.sync_runner

    try:
        with command._sync_execution_heartbeat(task):
            handle = runner.submit_durable(task)
        result_json = handle.result_json

        pre_inspection = command._inspect_execution_completion(
            result_json,
            task,
            supported_protocols=SUPPORTED_EXECUTION_PROTOCOL_RANGE,
        )
        pre_stored = False
        pre_result_data: str | None = None
        pre_result_reference: str | None = pre_inspection.prepared_result_reference
        pre_diagnostics: tuple[str, ...] = ()
        pre_storage_error: Exception | None = None
        if pre_inspection.decoded is not None and pre_inspection.decoded.completion.success:
            (
                eligible,
                pre_stored,
                pre_result_data,
                pre_result_reference,
                pre_diagnostics,
                pre_storage_error,
            ) = command._prepare_result_outside_lock(
                task,
                pre_inspection.decoded.completion.result,
                prepared_result_reference=pre_inspection.prepared_result_reference,
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=getattr(task, "completion_data", None),
                require_completion_data_match=True,
            )
            if not eligible:
                command.stdout.write(
                    command.style.NOTICE(
                        f"  Ignoring stale synchronous result for task {task.pk} "
                        f"attempt {expected_attempt_number}, "
                        f"generation {expected_execution_generation}"
                    )
                )
                return

        succeeded = False
        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is None:
                command.stdout.write(
                    command.style.NOTICE(
                        f"  Ignoring stale synchronous result for task {task.pk} "
                        f"attempt {expected_attempt_number}, "
                        f"generation {expected_execution_generation}"
                    )
                )
                return
            current = owned.execution
            inspection = command._inspect_execution_completion(
                result_json,
                current,
                supported_protocols=owned.supported_protocols,
            )
            decoded = inspection.decoded
            if decoded is None:
                error_message, exception_type, retryable = command._completion_rejection_policy(
                    inspection
                )
                command._handle_task_failure(
                    current,
                    error_message=error_message,
                    exception_type=exception_type,
                    retryable=retryable,
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=current.completion_data,
                    require_completion_data_match=True,
                    supported_protocols=owned.supported_protocols,
                )
                return

            completion = decoded.completion
            if completion.success:
                if pre_storage_error is not None:
                    command._handle_task_failure(
                        current,
                        error_message=(
                            "Failed to persist task result to external storage: "
                            f"{materialize_exception_message(pre_storage_error)}"
                        ),
                        exception_type="ResultStoragePersistenceError",
                        retryable=False,
                        expected_claimed_by_worker=expected_worker_id,
                        expected_attempt_number=expected_attempt_number,
                        expected_execution_generation=expected_execution_generation,
                        expected_completion_data=current.completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                        executor_django_ray_version=completion.executor_django_ray_version,
                    )
                    return
                succeeded = command._store_and_succeed_task(
                    current,
                    completion.result,
                    prepared_result_data=pre_result_data if pre_stored else None,
                    prepared_result_reference=(
                        pre_result_reference if pre_stored else inspection.prepared_result_reference
                    ),
                    _result_already_stored=pre_stored,
                    _storage_diagnostics=pre_diagnostics,
                    _already_locked=True,
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=current.completion_data,
                    require_completion_data_match=True,
                    supported_protocols=owned.supported_protocols,
                    executor_django_ray_version=completion.executor_django_ray_version,
                )
            else:
                command._handle_task_failure(
                    current,
                    error_message=completion.error or "Unknown error",
                    error_traceback=completion.traceback,
                    exception_type=completion.exception_type,
                    retryable=completion.retryable,
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=current.completion_data,
                    require_completion_data_match=True,
                    supported_protocols=owned.supported_protocols,
                    executor_django_ray_version=completion.executor_django_ray_version,
                )

        if succeeded:
            command.stdout.write(command.style.SUCCESS(f"  Task {task.pk} succeeded"))

    except Exception as e:
        command._fenced_handle_task_failure(
            task,
            error_message=materialize_exception_message(e),
            exception_type=safe_exception_type_name(e),
            retryable=False if isinstance(e, RuntimeEnvSnapshotError) else None,
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )


def store_task_result(command: Any, task: Any, result_value: Any) -> tuple[str, ...]:
    """Store task result inline or as a reference if result is too large."""
    from django_ray.result_storage import (
        DigestResultStorage,
        ResultStorageError,
        get_result_storage_backend,
    )

    get_settings_fn = command_dep(command, "get_settings", default_get_settings)
    settings = get_settings_fn()
    max_result_size = int(settings.get("MAX_RESULT_SIZE_BYTES", 1024 * 1024))

    serialized_result = json.dumps(result_value)
    result_size_bytes = len(serialized_result.encode("utf-8"))

    if result_size_bytes <= max_result_size:
        task.result_data = serialized_result
        task.result_reference = None
        return ()

    task.result_data = None
    diagnostics: list[str] = []
    configured_backend = str(settings.get("RESULT_STORAGE_BACKEND", "digest") or "digest")
    is_explicit_external = configured_backend.strip().lower() != "digest"

    try:
        backend = get_result_storage_backend(settings)
        task.result_reference = backend.store(serialized_result=serialized_result)
    except ResultStorageError as e:
        if is_explicit_external:
            raise
        task.result_reference = DigestResultStorage().store(serialized_result=serialized_result)
        diagnostics.append(
            f"  Result storage backend failed ({materialize_exception_text(e)}); "
            "falling back to digest-only result_reference"
        )

    diagnostics.append(
        f"  Task {task.pk} result is {result_size_bytes} bytes "
        f"(max={max_result_size}); stored result_reference"
    )
    return tuple(diagnostics)


def store_and_succeed_task(
    command: Any,
    task: Any,
    result_value: Any,
    *,
    prepared_result_data: str | None = None,
    prepared_result_reference: str | None = None,
    _result_already_stored: bool = False,
    _storage_diagnostics: tuple[str, ...] = (),
    _already_locked: bool = False,
    expected_ray_job_id: str | None = None,
    expected_claimed_by_worker: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_completion_data: str | None = None,
    require_completion_data_match: bool = False,
    supported_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE,
    executor_django_ray_version: str | None = None,
) -> bool:
    """Store and publish one successful result under the execution row lock.

    External result references are content-addressed and may be shared, so
    deleting a reference after losing the terminal transition is unsafe.
    Holding the row lock across storage and the success transition prevents
    cancellation, retry, or replacement from winning in that window.
    """
    task_cls = task_model(command)
    succeed_fn = command_dep(command, "succeed_task", default_succeed_task)

    filters: dict[str, Any] = {"pk": task.pk, "state": TaskState.RUNNING}
    if expected_ray_job_id is not None:
        filters["ray_job_id"] = expected_ray_job_id
    if expected_claimed_by_worker is not None:
        filters["claimed_by_worker"] = expected_claimed_by_worker
    if expected_attempt_number is not None:
        filters["attempt_number"] = expected_attempt_number
    if expected_execution_generation is not None:
        filters["execution_generation"] = expected_execution_generation
    if require_completion_data_match:
        filters["completion_data"] = expected_completion_data

    def _prepare_and_succeed(locked_row: Any) -> tuple[bool, tuple[str, ...]]:
        if not supported_protocols.supports(int(locked_row.execution_protocol_version)):
            return False, ()
        row_diagnostics: tuple[str, ...] = _storage_diagnostics
        if _result_already_stored:
            result_data: str | None = prepared_result_data
            result_reference: str | None = prepared_result_reference
        elif prepared_result_reference is None:
            row_diagnostics = command._store_task_result(locked_row, result_value) or ()
            result_data = locked_row.result_data
            result_reference = locked_row.result_reference
        else:
            locked_row.result_data = None
            locked_row.result_reference = prepared_result_reference
            result_data = None
            result_reference = prepared_result_reference

        ok = succeed_fn(
            locked_row,
            result_data=result_data,
            result_reference=result_reference,
            expected_ray_job_id=expected_ray_job_id,
            expected_claimed_by_worker=expected_claimed_by_worker,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_completion_data=expected_completion_data,
            require_completion_data_match=require_completion_data_match,
            supported_protocols=supported_protocols,
            _executor_django_ray_version=executor_django_ray_version,
        )
        return ok, row_diagnostics

    if _already_locked and connection.in_atomic_block:
        persisted, diagnostics = _prepare_and_succeed(task)
    else:
        with transaction.atomic():
            current = task_cls.objects.select_for_update().filter(**filters).first()
            if current is None:
                return False
            persisted, diagnostics = _prepare_and_succeed(current)
            if persisted:
                task.__dict__.update(current.__dict__)

    for message in diagnostics:
        command.stdout.write(command.style.WARNING(render_console_diagnostic(message)))
    return persisted


def handle_task_failure(
    command: Any,
    task: Any,
    error_message: str,
    error_traceback: str | None = None,
    exception_type: str | None = None,
    retryable: bool | None = None,
    *,
    expected_ray_job_id: str | None = None,
    expected_claimed_by_worker: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_completion_data: str | None = None,
    require_completion_data_match: bool = False,
    cancellation_status: str | None = None,
    cancellation_error: str | None = None,
    supported_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE,
    executor_django_ray_version: str | None = None,
) -> bool:
    """Handle a failed task, potentially scheduling a retry."""
    should_retry_fn = command_dep(command, "should_retry", default_should_retry)
    record_failure_fn = command_dep(command, "record_failure", default_record_failure)

    retry_decision = (
        RetryDecision(should_retry=False, reason="Executor marked failure non-retryable")
        if retryable is False
        else should_retry_fn(task, exception_type)
    )

    try:
        handled = record_failure_fn(
            task,
            error_message=error_message,
            error_traceback=error_traceback,
            retry=retry_decision.should_retry,
            next_attempt_at=retry_decision.next_attempt_at,
            expected_ray_job_id=expected_ray_job_id,
            expected_claimed_by_worker=expected_claimed_by_worker,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_completion_data=expected_completion_data,
            require_completion_data_match=require_completion_data_match,
            cancellation_status=cancellation_status,
            cancellation_error=cancellation_error,
            supported_protocols=supported_protocols,
            _executor_django_ray_version=executor_django_ray_version,
        )
    except RuntimeEnvSnapshotError as storage_error:
        retry_decision = RetryDecision(
            should_retry=False,
            reason="Persisted RuntimeEnv snapshot failed validation",
        )
        storage_message = materialize_exception_message(storage_error)
        error_message = f"{error_message}\nAutomatic retry blocked: {storage_message}"
        handled = record_failure_fn(
            task,
            error_message=error_message,
            error_traceback=error_traceback,
            retry=False,
            expected_ray_job_id=expected_ray_job_id,
            expected_claimed_by_worker=expected_claimed_by_worker,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_completion_data=expected_completion_data,
            require_completion_data_match=require_completion_data_match,
            cancellation_status=cancellation_status,
            cancellation_error=cancellation_error,
            supported_protocols=supported_protocols,
            _executor_django_ray_version=executor_django_ray_version,
        )
    if not handled:
        return False
    rendered_error = render_console_diagnostic(task.error_message or error_message)
    if retry_decision.should_retry:
        command.stdout.write(
            command.style.WARNING(
                f"  Task {task.pk} failed, scheduling retry "
                f"at {retry_decision.next_attempt_at}: {rendered_error}"
            )
        )
    else:
        reason = retry_decision.reason or "No retry configured"
        rendered_reason = render_console_diagnostic(reason)
        command.stdout.write(
            command.style.ERROR(
                f"  Task {task.pk} failed permanently ({rendered_reason}): {rendered_error}"
            )
        )
    return True


def persist_submission_tracking(
    target: Any,
    task: Any,
    handle: SubmissionHandle,
    *,
    expected_worker_id: str | None,
    expected_attempt_number: int,
    expected_execution_generation: int,
    expected_ray_job_id: str | None,
) -> bool:
    """Attach a backend handle only to the execution that submitted it."""
    dt = command_dep(target, "datetime", BUILTIN_DATETIME)
    task_cls = task_model(target)

    filters: dict[str, Any] = {
        "pk": task.pk,
        "state": TaskState.RUNNING,
        "attempt_number": expected_attempt_number,
        "execution_generation": expected_execution_generation,
    }
    if expected_worker_id is None:
        filters["claimed_by_worker__isnull"] = True
    else:
        filters["claimed_by_worker"] = expected_worker_id
    if expected_ray_job_id is None:
        filters["ray_job_id__isnull"] = True
    else:
        filters["ray_job_id"] = expected_ray_job_id
    heartbeat_at = dt.now(UTC)
    updated = task_cls.objects.filter(**filters).update(
        ray_job_id=handle.ray_job_id,
        ray_address=handle.ray_address,
        last_heartbeat_at=heartbeat_at,
    )
    if not updated:
        return False
    task.ray_job_id = handle.ray_job_id
    task.ray_address = handle.ray_address
    task.last_heartbeat_at = heartbeat_at
    return True


def release_submission_tracking(
    target: Any,
    task: Any,
    handle: SubmissionHandle,
    *,
    expected_worker_id: str | None,
    expected_attempt_number: int,
    expected_execution_generation: int,
) -> bool:
    """Release an exact reservation after a definite pre-request failure."""
    dt = command_dep(target, "datetime", BUILTIN_DATETIME)
    task_cls = task_model(target)

    filters: dict[str, Any] = {
        "pk": task.pk,
        "state": TaskState.RUNNING,
        "attempt_number": expected_attempt_number,
        "execution_generation": expected_execution_generation,
        "ray_job_id": handle.ray_job_id,
        "ray_address": handle.ray_address,
    }
    if expected_worker_id is None:
        filters["claimed_by_worker__isnull"] = True
    else:
        filters["claimed_by_worker"] = expected_worker_id
    heartbeat_at = dt.now(UTC)
    updated = task_cls.objects.filter(**filters).update(
        ray_job_id=None,
        ray_address=None,
        last_heartbeat_at=heartbeat_at,
    )
    if not updated:
        return False
    task.ray_job_id = None
    task.ray_address = None
    task.last_heartbeat_at = heartbeat_at
    return True


def cancel_untracked_submission(
    command: Any,
    runner: Any,
    handle: SubmissionHandle,
    *,
    backend_name: str,
    prepared: PreparedRemoteCancellation | None = None,
) -> CancellationOutcome:
    """Best-effort stop for a submission whose durable execution changed."""
    request_cancel_fn = command_dep(
        command, "request_remote_cancellation", default_request_remote_cancellation
    )
    try:
        cancellation = request_cancel_fn(runner, handle, prepared=prepared)
    except Exception as exc:
        cancellation = CancellationOutcome(
            CancellationOutcomeStatus.INDETERMINATE,
            f"Cancellation request raised {materialize_exception_text(exc)}",
        )
    message = f": {render_console_diagnostic(cancellation.message)}" if cancellation.message else ""
    rendered_job_id = render_console_diagnostic(handle.ray_job_id)
    command.stdout.write(
        command.style.WARNING(
            f"  Discarded stale {backend_name} submission {rendered_job_id}; "
            f"cancellation {cancellation.status.value}{message}"
        )
    )
    return cancellation


def cancel_mismatched_submissions(
    command: Any,
    runner: Any,
    reserved_handle: SubmissionHandle,
    observed_handle: SubmissionHandle,
    *,
    prepared: dict[tuple[str, str], PreparedRemoteCancellation] | None = None,
) -> CancellationOutcome:
    """Stop both capabilities, quiescing the undiscoverable duplicate first."""
    handles = {
        (observed_handle.ray_job_id, observed_handle.ray_address): observed_handle,
        (reserved_handle.ray_job_id, reserved_handle.ray_address): reserved_handle,
    }
    outcomes = [
        command._cancel_untracked_submission(
            runner,
            handle,
            backend_name="mismatched Ray Job",
            prepared=(prepared or {}).get((handle.ray_job_id, handle.ray_address)),
        )
        for handle in handles.values()
    ]
    statuses = {outcome.status for outcome in outcomes}
    quiescent_statuses = {
        CancellationOutcomeStatus.REQUESTED,
        CancellationOutcomeStatus.NOT_APPLICABLE,
    }
    if statuses <= quiescent_statuses:
        combined_status = (
            CancellationOutcomeStatus.REQUESTED
            if CancellationOutcomeStatus.REQUESTED in statuses
            else CancellationOutcomeStatus.NOT_APPLICABLE
        )
    elif statuses == {CancellationOutcomeStatus.FAILED}:
        combined_status = CancellationOutcomeStatus.FAILED
    else:
        combined_status = CancellationOutcomeStatus.INDETERMINATE
    messages = [outcome.message for outcome in outcomes if outcome.message]
    return CancellationOutcome(
        combined_status,
        "; ".join(messages) if messages else None,
    )


def inspect_submission_tracking(
    target: Any,
    task: Any,
    handle: SubmissionHandle,
    *,
    expected_worker_id: str | None,
    expected_attempt_number: int,
    expected_execution_generation: int,
) -> tuple[str, str | None, str | None]:
    """Classify the durable identity after a submitter loses its CAS."""
    task_cls = task_model(target)
    try:
        current = (
            task_cls.objects.filter(pk=task.pk)
            .values(
                "state",
                "claimed_by_worker",
                "ray_job_id",
                "ray_address",
                "attempt_number",
                "execution_generation",
                "completion_data",
            )
            .first()
        )
    except Exception as exc:
        return (
            "unknown",
            None,
            f"tracking inspection raised {materialize_exception_text(exc)}",
        )

    if current is None:
        return ("replaced", None, "the durable execution no longer exists")

    completion_data = current["completion_data"]
    same_identity = (
        str(current["ray_job_id"] or "") == handle.ray_job_id
        and str(current["ray_address"] or "") == handle.ray_address
        and int(current["attempt_number"]) == expected_attempt_number
        and int(current["execution_generation"]) == expected_execution_generation
    )
    if not same_identity:
        return ("replaced", completion_data, "the durable execution identity changed")
    current_worker_id = current["claimed_by_worker"]
    if current_worker_id not in (None, expected_worker_id):
        return (
            "transferred",
            completion_data,
            f"ownership moved to {current_worker_id}",
        )
    if current["state"] != TaskState.RUNNING:
        return (
            "terminal",
            completion_data,
            f"the durable execution is now {current['state']}",
        )

    if current_worker_id == expected_worker_id:
        return ("owned", completion_data, None)
    return (
        "transferred",
        completion_data,
        f"ownership moved to {current_worker_id or 'an unclaimed reconciler'}",
    )


def terminalize_mismatched_ray_job_submission(
    command: Any,
    current: Any,
    *,
    reserved_handle: SubmissionHandle,
    expected_worker_id: str | None,
    expected_attempt_number: int,
    expected_execution_generation: int,
    error_message: str,
    exception_type: str,
    cancellation: CancellationOutcome,
    supported_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE,
    _pre_stored: bool = False,
    _pre_result_data: str | None = None,
    _pre_result_reference: str | None = None,
    _pre_diagnostics: tuple[str, ...] = (),
    _pre_storage_error: Exception | None = None,
) -> bool:
    """Close one ambiguous completion channel while its task lock is held."""
    completion_data = current.completion_data
    inspection = (
        command._inspect_execution_completion(
            completion_data,
            current,
            supported_protocols=supported_protocols,
        )
        if completion_data is not None
        else None
    )
    decoded = inspection.decoded if inspection is not None else None

    if decoded is not None and decoded.completion.success:
        assert inspection is not None
        completion = decoded.completion
        if _pre_storage_error is not None:
            return command._handle_task_failure(
                current,
                error_message=(
                    "Failed to persist task result to external storage: "
                    f"{materialize_exception_message(_pre_storage_error)}"
                ),
                exception_type="ResultStoragePersistenceError",
                retryable=False,
                expected_ray_job_id=reserved_handle.ray_job_id,
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=completion_data,
                require_completion_data_match=True,
                cancellation_status=cancellation.status.value,
                cancellation_error=cancellation.message,
                supported_protocols=supported_protocols,
                executor_django_ray_version=completion.executor_django_ray_version,
            )
        handled = command._store_and_succeed_task(
            current,
            completion.result,
            prepared_result_data=_pre_result_data if _pre_stored else None,
            prepared_result_reference=(
                _pre_result_reference if _pre_stored else inspection.prepared_result_reference
            ),
            _result_already_stored=_pre_stored,
            _storage_diagnostics=_pre_diagnostics,
            _already_locked=True,
            expected_ray_job_id=reserved_handle.ray_job_id,
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_completion_data=completion_data,
            require_completion_data_match=True,
            supported_protocols=supported_protocols,
            executor_django_ray_version=completion.executor_django_ray_version,
        )
        if handled:
            command.stdout.write(
                command.style.WARNING(
                    f"  Task {current.pk} accepted its durable completion only after "
                    "quiescing both mismatched Ray Jobs"
                )
            )
        return handled

    mismatch_error = error_message
    error_traceback: str | None = None
    executor_django_ray_version: str | None = None
    if decoded is not None and not decoded.completion.success:
        completion = decoded.completion
        mismatch_error = f"{error_message}; completion reported: {completion.error}"
        error_traceback = completion.traceback
        executor_django_ray_version = completion.executor_django_ray_version
    elif completion_data is not None:
        assert inspection is not None
        assert inspection.rejection is not None
        mismatch_error = (
            f"{error_message}; completion envelope was rejected ({inspection.rejection.value})"
        )

    return command._handle_task_failure(
        current,
        error_message=mismatch_error,
        error_traceback=error_traceback,
        exception_type=exception_type,
        retryable=False,
        expected_ray_job_id=reserved_handle.ray_job_id,
        expected_claimed_by_worker=expected_worker_id,
        expected_attempt_number=expected_attempt_number,
        expected_execution_generation=expected_execution_generation,
        expected_completion_data=completion_data,
        require_completion_data_match=True,
        cancellation_status=cancellation.status.value,
        cancellation_error=cancellation.message,
        supported_protocols=supported_protocols,
        executor_django_ray_version=executor_django_ray_version,
    )


def handle_ray_job_confirmation_loss(
    command: Any,
    task: Any,
    runner: Any,
    handle: SubmissionHandle,
    *,
    expected_worker_id: str | None,
    expected_attempt_number: int,
    expected_execution_generation: int,
    detail: str,
) -> None:
    """Resolve a failed post-request CAS without killing an adopted job."""
    disposition, _, inspection_detail = command._inspect_submission_tracking(
        task,
        handle,
        expected_worker_id=expected_worker_id,
        expected_attempt_number=expected_attempt_number,
        expected_execution_generation=expected_execution_generation,
    )
    if disposition == "replaced":
        command.active_tasks.pop(task.pk, None)
        command.active_task_identities.pop(task.pk, None)
        command._cancel_untracked_submission(
            runner,
            handle,
            backend_name="replaced Ray Job",
        )
    elif disposition in {"transferred", "terminal"}:
        command.active_tasks.pop(task.pk, None)
        command.active_task_identities.pop(task.pk, None)

    retained = disposition in {"owned", "unknown"}
    action = "retaining exact tracking" if retained else "retiring local tracking"
    explanation = f"; {render_console_diagnostic(inspection_detail)}" if inspection_detail else ""
    rendered_detail = render_console_diagnostic(detail)
    command.stdout.write(
        command.style.WARNING(
            f"  Task {task.pk} could not confirm Ray Job tracking ({rendered_detail}); "
            f"{action}{explanation}"
        )
    )


def handle_mismatched_ray_job_submission(
    command: Any,
    task: Any,
    runner: Any,
    reserved_handle: SubmissionHandle,
    observed_handle: SubmissionHandle,
    *,
    expected_worker_id: str | None,
    expected_attempt_number: int,
    expected_execution_generation: int,
    error_message: str,
    exception_type: str,
) -> None:
    """Fence a returned capability mismatch without cancelling an adopter."""
    prepare_cancel_fn = command_dep(
        command, "prepare_remote_cancellation", default_prepare_remote_cancellation
    )
    task_cls = task_model(command)

    handles = {
        (reserved_handle.ray_job_id, reserved_handle.ray_address): reserved_handle,
        (observed_handle.ray_job_id, observed_handle.ray_address): observed_handle,
    }
    prepared_cancellations = {
        identity: prepare_cancel_fn(runner, handle) for identity, handle in handles.items()
    }

    try:
        authoritative_snapshot = task_cls.objects.get(pk=task.pk)
    except task_cls.DoesNotExist:
        authoritative_snapshot = None
    if expected_worker_id == command.worker_id and authoritative_snapshot is not None:
        # Phase 1: classify ownership and identity inside a short DB transaction (no RPCs).
        phase1_mode = "unowned"
        matched_is_running = False
        matched_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE
        with command._authoritative_task_owner(
            authoritative_snapshot,
            expected_state=str(authoritative_snapshot.state),
            allow_takeover=False,
            require_completion_data_match=False,
        ) as owned:
            current = owned.execution if owned is not None else None
            if current is not None and owned is not None:
                if (
                    str(current.ray_job_id or "") != reserved_handle.ray_job_id
                    or str(current.ray_address or "") != reserved_handle.ray_address
                    or current.attempt_number != expected_attempt_number
                    or current.execution_generation != expected_execution_generation
                ):
                    phase1_mode = "replaced"
                else:
                    phase1_mode = "matched"
                    matched_is_running = current.state == TaskState.RUNNING
                    matched_protocols = owned.supported_protocols

        # Phase 2: execute remote stop_job() RPCs strictly outside DB locks (R2-007).
        if phase1_mode == "replaced":
            command._cancel_mismatched_submissions(
                runner,
                reserved_handle,
                observed_handle,
                prepared=prepared_cancellations,
            )
            command._retire_active_ray_job_tracking(
                int(task.pk),
                ray_job_id=reserved_handle.ray_job_id,
                identity=(expected_attempt_number, expected_execution_generation),
            )
            return

        if phase1_mode == "matched":
            cancellation = command._cancel_mismatched_submissions(
                runner,
                reserved_handle,
                observed_handle,
                prepared=prepared_cancellations,
            )
            if not matched_is_running:
                command._retire_active_ray_job_tracking(
                    int(task.pk),
                    ray_job_id=reserved_handle.ray_job_id,
                    identity=(expected_attempt_number, expected_execution_generation),
                )
                return

            # Phase 3: pre-spill any oversized completion result outside DB locks,
            # then commit terminalization inside a short authoritative transaction.
            for _attempt in range(2):
                try:
                    latest_snapshot = task_cls.objects.get(pk=task.pk)
                except task_cls.DoesNotExist:
                    break
                pre_completion_data = latest_snapshot.completion_data
                pre_stored = False
                pre_result_data: str | None = None
                pre_result_reference: str | None = None
                pre_diagnostics: tuple[str, ...] = ()
                pre_storage_error: Exception | None = None
                if pre_completion_data is not None and latest_snapshot.state == TaskState.RUNNING:
                    pre_inspection = command._inspect_execution_completion(
                        pre_completion_data,
                        latest_snapshot,
                        supported_protocols=matched_protocols,
                    )
                    pre_result_reference = pre_inspection.prepared_result_reference
                    if (
                        pre_inspection.decoded is not None
                        and pre_inspection.decoded.completion.success
                    ):
                        (
                            eligible,
                            pre_stored,
                            pre_result_data,
                            pre_result_reference,
                            pre_diagnostics,
                            pre_storage_error,
                        ) = command._prepare_result_outside_lock(
                            latest_snapshot,
                            pre_inspection.decoded.completion.result,
                            prepared_result_reference=pre_inspection.prepared_result_reference,
                            expected_ray_job_id=reserved_handle.ray_job_id,
                            expected_claimed_by_worker=expected_worker_id,
                            expected_attempt_number=expected_attempt_number,
                            expected_execution_generation=expected_execution_generation,
                            expected_completion_data=pre_completion_data,
                            require_completion_data_match=True,
                            supported_protocols=matched_protocols,
                        )
                        if not eligible:
                            pre_stored = False

                retry_for_fresh_completion = False
                handled = False
                with command._authoritative_task_owner(
                    latest_snapshot,
                    expected_state=str(latest_snapshot.state),
                    allow_takeover=False,
                    require_completion_data_match=False,
                ) as owned:
                    current = owned.execution if owned is not None else None
                    if current is None or owned is None:
                        break
                    if (
                        str(current.ray_job_id or "") != reserved_handle.ray_job_id
                        or str(current.ray_address or "") != reserved_handle.ray_address
                        or current.attempt_number != expected_attempt_number
                        or current.execution_generation != expected_execution_generation
                    ):
                        handled = True
                    elif current.state != TaskState.RUNNING:
                        handled = True
                    elif current.completion_data != pre_completion_data and _attempt == 0:
                        retry_for_fresh_completion = True
                    else:
                        handled = command._terminalize_mismatched_ray_job_submission(
                            current,
                            reserved_handle=reserved_handle,
                            expected_worker_id=expected_worker_id,
                            expected_attempt_number=expected_attempt_number,
                            expected_execution_generation=expected_execution_generation,
                            error_message=error_message,
                            exception_type=exception_type,
                            cancellation=cancellation,
                            supported_protocols=owned.supported_protocols,
                            _pre_stored=pre_stored,
                            _pre_result_data=pre_result_data,
                            _pre_result_reference=pre_result_reference,
                            _pre_diagnostics=pre_diagnostics,
                            _pre_storage_error=pre_storage_error,
                        )
                    if handled:
                        task_pk = int(task.pk)
                        transaction.on_commit(
                            lambda task_pk=task_pk: command._retire_active_ray_job_tracking(
                                task_pk,
                                ray_job_id=reserved_handle.ray_job_id,
                                identity=(
                                    expected_attempt_number,
                                    expected_execution_generation,
                                ),
                            )
                        )
                        return
                if not retry_for_fresh_completion:
                    break

    if command.shutdown_requested:
        command._cancel_untracked_submission(
            runner,
            observed_handle,
            backend_name="orphaned mismatched Ray Job",
            prepared=prepared_cancellations[
                (observed_handle.ray_job_id, observed_handle.ray_address)
            ],
        )
        return

    disposition, _, inspection_detail = command._inspect_submission_tracking(
        task,
        reserved_handle,
        expected_worker_id=expected_worker_id,
        expected_attempt_number=expected_attempt_number,
        expected_execution_generation=expected_execution_generation,
    )
    if disposition in {"replaced", "terminal"}:
        command._cancel_mismatched_submissions(
            runner,
            reserved_handle,
            observed_handle,
            prepared=prepared_cancellations,
        )
    else:
        command._cancel_untracked_submission(
            runner,
            observed_handle,
            backend_name="mismatched Ray Job",
            prepared=prepared_cancellations[
                (observed_handle.ray_job_id, observed_handle.ray_address)
            ],
        )

    if disposition in {"replaced", "transferred", "terminal"}:
        command.active_tasks.pop(task.pk, None)
        command.active_task_identities.pop(task.pk, None)

    retained = disposition in {"owned", "unknown"}
    action = "retaining exact tracking" if retained else "retiring local tracking"
    explanation = f"; {render_console_diagnostic(inspection_detail)}" if inspection_detail else ""
    command.stdout.write(
        command.style.WARNING(f"  Task {task.pk} Ray Job identity mismatch; {action}{explanation}")
    )


def submit_task_to_ray_core(command: Any, task: Any) -> None:
    """Submit a task to Ray via @ray.remote (RayCoreRunner)."""
    import ray

    expected_worker_id = command.worker_id
    expected_attempt_number = int(task.attempt_number)
    expected_execution_generation = int(task.execution_generation)
    expected_ray_job_id = task.ray_job_id

    if not ray.is_initialized():
        command.stdout.write(
            command.style.WARNING("  Ray not initialized, attempting to connect...")
        )
        command._reconnect_ray()

        if command.lease_ownership_lost or command.shutdown_requested:
            return

        if not ray.is_initialized():
            command._fenced_handle_task_failure(
                task,
                error_message="Ray cluster not available",
                exception_type="RayConnectionError",
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
            )
            return

    if command.ray_core_runner is None:
        with command._preserve_shutdown_signal_handlers():
            command.ray_core_runner = command._create_ray_core_runner()
    runner = command.ray_core_runner

    ray_options = _apply_task_contract_for_dispatch(command, task)
    try:
        if ray_options and _callable_accepts_kwarg(runner.submit_durable, "ray_options"):
            handle = runner.submit_durable(task_execution=task, ray_options=ray_options)
        else:
            handle = runner.submit_durable(task_execution=task)
    except Exception as e:
        from django_ray.execution_codec import ExecutionRequestEncodeError
        from django_ray.workflow.plans import WorkflowPlanMismatchError

        command._fenced_handle_task_failure(
            task,
            error_message=f"Failed to submit to Ray Core: {materialize_exception_message(e)}",
            error_traceback=(
                None if isinstance(e, RuntimeEnvSnapshotError) else traceback.format_exc()
            ),
            exception_type=safe_exception_type_name(e),
            retryable=(
                False
                if isinstance(
                    e,
                    (
                        ExecutionRequestEncodeError,
                        RuntimeEnvSnapshotError,
                        WorkflowPlanMismatchError,
                    ),
                )
                else None
            ),
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        return

    try:
        attached = command._fenced_persist_submission_tracking(
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_ray_job_id=expected_ray_job_id,
        )
    except Exception as exc:
        cancellation = command._cancel_untracked_submission(
            runner,
            handle,
            backend_name="Ray Core",
        )
        command._fenced_handle_task_failure(
            task,
            error_message=(
                "Failed to persist Ray Core submission tracking: "
                f"{materialize_exception_message(exc)}"
            ),
            error_traceback=traceback.format_exc(),
            exception_type=safe_exception_type_name(exc),
            retryable=False,
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            cancellation_status=cancellation.status.value,
            cancellation_error=cancellation.message,
        )
        return

    if not attached:
        command._cancel_untracked_submission(
            runner,
            handle,
            backend_name="Ray Core",
        )
        return

    command._last_partitioned_handles = ()
    command.stdout.write(command.style.SUCCESS(f"  Task {task.pk} submitted to Ray Core (async)"))


def fenced_release_ray_job_request_reservation(
    command: Any,
    task: Any,
    reserved_handle: SubmissionHandle,
    *,
    expected_reference: str | None,
) -> bool:
    """Release a pre-submit Ray Job reservation only behind a live lease fence (R2-014)."""
    from django_ray.ray_job_request_storage import release_ray_job_request_reservation

    if command.lease_ownership_lost:
        return False
    if command.lease_identity is None or not isinstance(task, RayTaskExecution):
        return release_ray_job_request_reservation(
            task,
            reserved_handle,
            expected_reference=expected_reference,
        )
    with transaction.atomic():
        lease = command._lock_authoritative_leases(
            source_worker_id=command.worker_id,
            allow_takeover=False,
            renew_heartbeat=False,
        )
        if lease is None:
            return False
        return release_ray_job_request_reservation(
            task,
            reserved_handle,
            expected_reference=expected_reference,
        )


def submit_task_to_ray(command: Any, task: Any) -> None:
    """Submit a task to Ray via the Ray Job Submission API."""
    from django_ray.runner import (
        RayJobRequestPreparationError,
        RayJobSubmissionUncertainError,
    )
    from django_ray.runner.ray_job import RayJobRunner
    from django_ray.workflow.plans import WorkflowPlanMismatchError

    expected_worker_id = command.worker_id
    expected_attempt_number = int(task.attempt_number)
    expected_execution_generation = int(task.execution_generation)
    expected_ray_job_id = task.ray_job_id

    try:
        runner = RayJobRunner()
    except RayJobRequestPreparationError as exc:
        command._fenced_handle_task_failure(
            task,
            error_message=str(exc),
            error_traceback=None,
            exception_type=safe_exception_type_name(exc),
            retryable=(False if exc.requires_nonretryable_disposition else None),
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        return
    except Exception as e:
        command._fenced_handle_task_failure(
            task,
            error_message=f"Failed to submit to Ray: {materialize_exception_message(e)}",
            error_traceback=(
                None if isinstance(e, RuntimeEnvSnapshotError) else traceback.format_exc()
            ),
            exception_type=safe_exception_type_name(e),
            retryable=(
                False
                if isinstance(e, (RuntimeEnvSnapshotError, WorkflowPlanMismatchError))
                else None
            ),
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        return

    try:
        reserved_handle = runner.submission_handle(task)
        reserved = command._fenced_persist_submission_tracking(
            task,
            reserved_handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_ray_job_id=expected_ray_job_id,
        )
    except RayJobRequestPreparationError as exc:
        command._fenced_handle_task_failure(
            task,
            error_message=str(exc),
            error_traceback=None,
            exception_type=safe_exception_type_name(exc),
            retryable=(False if exc.requires_nonretryable_disposition else None),
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        return
    except Exception as exc:
        command._fenced_handle_task_failure(
            task,
            error_message=(
                "Failed to reserve Ray Job submission identity: "
                f"{materialize_exception_message(exc)}"
            ),
            error_traceback=(
                None if isinstance(exc, RuntimeEnvSnapshotError) else traceback.format_exc()
            ),
            exception_type=safe_exception_type_name(exc),
            retryable=(
                False
                if isinstance(exc, (RuntimeEnvSnapshotError, WorkflowPlanMismatchError))
                else None
            ),
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        return

    if not reserved:
        return

    command.active_tasks[task.pk] = reserved_handle.ray_job_id
    command.active_task_identities[task.pk] = (
        expected_attempt_number,
        expected_execution_generation,
    )

    ray_options = _apply_task_contract_for_dispatch(command, task)
    try:
        if ray_options and _callable_accepts_kwarg(runner.submit_durable, "ray_options"):
            handle = cast(Any, runner.submit_durable)(task_execution=task, ray_options=ray_options)
        else:
            handle = runner.submit_durable(task_execution=task)
    except RayJobSubmissionUncertainError as exc:
        try:
            still_reserved = command._fenced_persist_submission_tracking(
                task,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_ray_job_id=reserved_handle.ray_job_id,
            )
        except Exception as tracking_exc:
            command._handle_ray_job_confirmation_loss(
                task,
                runner,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail=materialize_exception_text(tracking_exc),
            )
            return
        if not still_reserved:
            command._handle_ray_job_confirmation_loss(
                task,
                runner,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail="the reservation confirmation lost its compare-and-swap",
            )
            return
        command.stdout.write(
            command.style.WARNING(
                f"  Task {task.pk} submission acceptance is uncertain; "
                f"retaining exact Ray job {exc.submission_id} for reconciliation"
            )
        )
        return
    except RayJobRequestPreparationError as exc:
        try:
            released = fenced_release_ray_job_request_reservation(
                command,
                task,
                reserved_handle,
                expected_reference=task.ray_job_request_reference,
            )
        except Exception as release_exc:
            command._handle_ray_job_confirmation_loss(
                task,
                runner,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail=materialize_exception_text(release_exc),
            )
            return
        if not released:
            command._handle_ray_job_confirmation_loss(
                task,
                runner,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail="the pre-submit reservation release did not match",
            )
            return
        command.active_tasks.pop(task.pk, None)
        command.active_task_identities.pop(task.pk, None)
        command._fenced_handle_task_failure(
            task,
            error_message=str(exc),
            error_traceback=None,
            exception_type=safe_exception_type_name(exc),
            retryable=(False if exc.requires_nonretryable_disposition else None),
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        return
    except Exception as exc:
        try:
            released = fenced_release_ray_job_request_reservation(
                command,
                task,
                reserved_handle,
                expected_reference=task.ray_job_request_reference,
            )
        except Exception as release_exc:
            command._handle_ray_job_confirmation_loss(
                task,
                runner,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail=materialize_exception_text(release_exc),
            )
            return
        if not released:
            command._handle_ray_job_confirmation_loss(
                task,
                runner,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail="the pre-submit reservation release did not match",
            )
            return
        command.active_tasks.pop(task.pk, None)
        command.active_task_identities.pop(task.pk, None)
        command._fenced_handle_task_failure(
            task,
            error_message=f"Failed to submit to Ray: {materialize_exception_message(exc)}",
            error_traceback=(
                None if isinstance(exc, RuntimeEnvSnapshotError) else traceback.format_exc()
            ),
            exception_type=safe_exception_type_name(exc),
            retryable=(
                False
                if isinstance(exc, (RuntimeEnvSnapshotError, WorkflowPlanMismatchError))
                else None
            ),
            expected_claimed_by_worker=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        return

    if (
        handle.ray_job_id != reserved_handle.ray_job_id
        or handle.ray_address != reserved_handle.ray_address
    ):
        command._handle_mismatched_ray_job_submission(
            task,
            runner,
            reserved_handle,
            handle,
            error_message=(
                "Ray Job runner returned a handle that differs from the "
                "durably reserved submission identity"
            ),
            exception_type="RayJobSubmissionIdentityError",
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        return

    try:
        still_reserved = command._fenced_persist_submission_tracking(
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_ray_job_id=reserved_handle.ray_job_id,
        )
    except Exception as exc:
        command._handle_ray_job_confirmation_loss(
            task,
            runner,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            detail=materialize_exception_text(exc),
        )
        return
    if not still_reserved:
        command._handle_ray_job_confirmation_loss(
            task,
            runner,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            detail="the post-submit confirmation lost its compare-and-swap",
        )
        return

    command.stdout.write(
        command.style.SUCCESS(f"  Task {task.pk} submitted as Ray job {handle.ray_job_id}")
    )


def build_submission_handle(command: Any, task: Any, ray_job_id: str) -> SubmissionHandle:
    """Build a submission handle from persisted task metadata."""
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    return SubmissionHandle(
        ray_job_id=ray_job_id,
        ray_address=task.ray_address or "",
        submitted_at=task.started_at or dt.now(UTC),
    )
