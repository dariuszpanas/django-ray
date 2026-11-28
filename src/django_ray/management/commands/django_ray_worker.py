"""Django management command for running the django-ray worker."""

from __future__ import annotations

import math
import random
import signal
import time
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from threading import Event, current_thread, main_thread
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import close_old_connections

from django_ray import __version__ as django_ray_version
from django_ray import lease_policy
from django_ray.conf.settings import get_settings
from django_ray.execution_codec import ExecutionIdentity, decode_execution_completion
from django_ray.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_RANGE, ExecutionProtocolRange
from django_ray.lifecycle import (
    cancel_task,
    expire_queued_tasks,
    promote_legacy_ray_target,
    record_failure,
    record_lost,
    retry_task,
    succeed_task,
)
from django_ray.logging import get_worker_logger
from django_ray.models import RayTaskExecution, TaskWorkerLease
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    PreparedRemoteCancellation,
    finalize_cancellation,
    prepare_remote_cancellation,
    request_remote_cancellation,
)
from django_ray.runner.leasing import (
    WorkerLeaseIdentity,
    generate_worker_id,
    get_heartbeat_interval,
)
from django_ray.runner.maintenance import MaintenanceCandidate, MaintenanceScan
from django_ray.runner.polling import AdaptivePollingPolicy
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner
from django_ray.runner.reconciliation import (
    get_stuck_timeout,
    is_task_stuck,
    is_task_timed_out,
    mark_task_lost,
    mark_task_timed_out,
)
from django_ray.runner.retry import should_retry
from django_ray.runner.sync import SyncRunner
from django_ray.runtime.runtime_env import runtime_env_for_execution
from django_ray.worker import (
    BUILTIN_DATETIME as _BUILTIN_DATETIME,
)
from django_ray.worker import (
    CLAIM_UPDATE_FIELDS as _CLAIM_UPDATE_FIELDS,
)
from django_ray.worker import (
    DB_BACKOFF_INITIAL_SECONDS as _DB_BACKOFF_INITIAL_SECONDS,
)
from django_ray.worker import (
    DB_BACKOFF_MAX_SECONDS as _DB_BACKOFF_MAX_SECONDS,
)
from django_ray.worker import (
    DEFAULT_CANCELLATION_INTERVAL_SECONDS as _DEFAULT_CANCELLATION_INTERVAL_SECONDS,
)
from django_ray.worker import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS as _DEFAULT_DRAIN_TIMEOUT_SECONDS,
)
from django_ray.worker import (
    DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS as _DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS,
)
from django_ray.worker import (
    DEFAULT_MARK_TASK_LOST as _DEFAULT_MARK_TASK_LOST,
)
from django_ray.worker import (
    DEFERRED_MAINTENANCE_SCAN_FIELDS as _DEFERRED_MAINTENANCE_SCAN_FIELDS_DEFAULT,
)
from django_ray.worker import (
    LEASE_QUEUE_NAME_MAX_LENGTH as _LEASE_QUEUE_NAME_MAX_LENGTH,
)
from django_ray.worker import (
    MAX_DRAIN_TIMEOUT_SECONDS as _MAX_DRAIN_TIMEOUT_SECONDS,
)
from django_ray.worker import (
    RAY_RECONNECT_INITIAL_DELAY_SECONDS as _RAY_RECONNECT_INITIAL_DELAY_SECONDS,
)
from django_ray.worker import (
    RAY_RECONNECT_MAX_DELAY_SECONDS as _RAY_RECONNECT_MAX_DELAY_SECONDS,
)
from django_ray.worker import (
    SHUTDOWN_WAIT_SLICE_SECONDS as _SHUTDOWN_WAIT_SLICE_SECONDS,
)
from django_ray.worker import (
    WORKER_ID_ALLOCATION_ATTEMPTS as _WORKER_ID_ALLOCATION_ATTEMPTS,
)
from django_ray.worker import (
    InspectedExecutionCompletion as _InspectedExecutionCompletion,
)
from django_ray.worker import (
    OwnedTask as _OwnedTask,
)
from django_ray.worker import (
    RayBackendQueueConfiguration as _RayBackendQueueConfiguration,
)
from django_ray.worker import (
    RayJobQueueAffinity as _RayJobQueueAffinity,
)
from django_ray.worker import (
    WorkerIdReservedByTaskError as _WorkerIdReservedByTaskError,
)
from django_ray.worker import (
    claimer,
    close_old_db_connections,
    dispatcher,
    is_transient_db_error,
    loop,
    maintenance,
    reconciler,
    shutdown,
)

# Compatibility facade: keep module-level names visible for tests that
# monkeypatch attributes on ``django_ray.management.commands.django_ray_worker``.
__all__ = [
    "Command",
    "RayCoreRunner",
    "RayTaskExecution",
    "TaskWorkerLease",
    "_BUILTIN_DATETIME",
    "_CLAIM_UPDATE_FIELDS",
    "_DB_BACKOFF_INITIAL_SECONDS",
    "_DB_BACKOFF_MAX_SECONDS",
    "_DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "_DEFAULT_MARK_TASK_LOST",
    "_InspectedExecutionCompletion",
    "_LEASE_QUEUE_NAME_MAX_LENGTH",
    "_MAX_DRAIN_TIMEOUT_SECONDS",
    "_OwnedTask",
    "_RAY_RECONNECT_INITIAL_DELAY_SECONDS",
    "_RAY_RECONNECT_MAX_DELAY_SECONDS",
    "_RayBackendQueueConfiguration",
    "_RayJobQueueAffinity",
    "_SHUTDOWN_WAIT_SLICE_SECONDS",
    "_WORKER_ID_ALLOCATION_ATTEMPTS",
    "_WorkerIdReservedByTaskError",
    "_close_old_db_connections",
    "_is_transient_db_error",
    "cancel_task",
    "close_old_connections",
    "current_thread",
    "datetime",
    "decode_execution_completion",
    "django_ray_version",
    "expire_queued_tasks",
    "finalize_cancellation",
    "generate_worker_id",
    "get_heartbeat_interval",
    "get_settings",
    "get_stuck_timeout",
    "is_task_stuck",
    "is_task_timed_out",
    "lease_policy",
    "main_thread",
    "mark_task_lost",
    "mark_task_timed_out",
    "prepare_remote_cancellation",
    "promote_legacy_ray_target",
    "record_failure",
    "record_lost",
    "request_remote_cancellation",
    "retry_task",
    "runtime_env_for_execution",
    "should_retry",
    "signal",
    "succeed_task",
    "time",
]

_is_transient_db_error = is_transient_db_error


def _close_old_db_connections() -> None:
    """Close obsolete or unusable Django DB connections safely between loop cycles."""
    close_old_db_connections(Command)


class Command(BaseCommand):
    """Run a django-ray worker process."""

    help = "Run a django-ray worker that claims and executes tasks on Ray"
    _DEFERRED_MAINTENANCE_SCAN_FIELDS: tuple[str, ...] = _DEFERRED_MAINTENANCE_SCAN_FIELDS_DEFAULT

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shutdown_requested = False
        self._shutdown_event = Event()
        self.lease_ownership_lost = False
        self.worker_id = generate_worker_id()
        self.logger = get_worker_logger(self.worker_id)
        self.active_tasks: dict[int, str] = {}
        self.active_task_identities: dict[int, tuple[int, int]] = {}
        self.ray_core_runner: RayCoreRunner | None = None
        self.sync_runner: SyncRunner | None = None
        self.last_reconciliation = 0.0
        self.reconciliation_interval = 30.0
        self.timeout_check_interval = 30.0
        self.cancellation_interval = _DEFAULT_CANCELLATION_INTERVAL_SECONDS
        self.lease_cleanup_interval = 30.0
        self.expiry_sweep_interval = _DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS
        self._last_expiry_sweep_monotonic = 0.0
        self._lease_heartbeat_interval_seconds = 15.0
        self.lease: TaskWorkerLease | None = None
        self.lease_identity: WorkerLeaseIdentity | None = None
        self.lease_queue_name: str = "default"
        self.last_task_processed = 0.0
        self.tasks_processed_count = 0
        self.task_monitor_heartbeat_interval = 15.0
        self.last_task_monitor_heartbeat = 0.0
        self.completion_poll_interval = 0.1
        self.ray_job_completion_poll_interval = 0.25
        self._ray_job_completion_cursor = 0
        self._ray_job_completion_cycle_end = 0
        self._maintenance_scans: dict[str, MaintenanceScan] = {}
        self._active_reconciliation_cursor = 0
        self._active_reconciliation_cycle_end = 0
        self._active_reconciliation_pending = False
        self.poll_base_interval = 0.1
        self.poll_max_interval = 1.0
        self.polling_policy = self._new_polling_policy()
        self.shutdown_signal: int | None = None
        self.shutdown_exit_code: int | None = None
        self.verbosity = 1
        self.execution_mode = "sync"
        self.sync_mode = False
        self.local_mode = False
        self.ray_job_mode = False
        self.cluster_address: str | None = None
        self.drain_mode = False
        self.drain_timeout_seconds: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS
        self.drain_timed_out = False
        self._ray_reconnect_backoff_until = 0.0
        self._ray_reconnect_delay = _RAY_RECONNECT_INITIAL_DELAY_SECONDS
        self._ray_backend_queue_configuration: _RayBackendQueueConfiguration | None = None
        self._ray_job_queue_affinity: _RayJobQueueAffinity | None = None
        self._lease_policy_seconds: int | None = None
        self._lease_policy_revision: int | None = None
        self._last_successful_heartbeat_monotonic: float = 0.0
        self._heartbeat_retry_after: float = 0.0
        self._db_backoff_delay: float = _DB_BACKOFF_INITIAL_SECONDS
        self._signal_logged: bool = False
        self._last_partitioned_handles: tuple[RayCoreHandle, ...] = ()
        self._last_ray_core_authority_check: float = 0.0
        self._prepared_cancellation_cache: dict[
            tuple[str, str], tuple[Any, PreparedRemoteCancellation]
        ] = {}
        self._allow_sync_heartbeat_in_memory: bool = False

    def _new_polling_policy(self) -> AdaptivePollingPolicy:
        """Build polling jitter from the current worker identity."""
        return AdaptivePollingPolicy(
            base_interval_seconds=self.poll_base_interval,
            max_interval_seconds=self.poll_max_interval,
            random_value=random.Random(self.worker_id).random,
        )

    def _set_worker_id(self, worker_id: str) -> None:
        """Replace an unacquired candidate and all identity-derived state."""
        if self.lease_identity is not None:
            raise RuntimeError("cannot replace an acquired worker lease identity")
        self.worker_id = worker_id
        self.logger = get_worker_logger(worker_id)
        self.polling_policy = self._new_polling_policy()

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        queue_selection = parser.add_mutually_exclusive_group()
        queue_selection.add_argument(
            "--queue",
            type=str,
            default=None,
            help="Queue name to process (default: default). Use comma-separated for multiple queues.",
        )
        queue_selection.add_argument(
            "--queues",
            type=str,
            nargs="+",
            default=None,
            help="Queue names to process (space-separated). Alternative to --queue.",
        )
        queue_selection.add_argument(
            "--all-queues",
            action="store_true",
            help="Process tasks from all queues configured on django-ray backends.",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=None,
            help="Maximum concurrent tasks (default: from settings)",
        )
        execution_mode = parser.add_mutually_exclusive_group()
        execution_mode.add_argument(
            "--sync",
            action="store_true",
            help="Run tasks synchronously (without Ray, for testing)",
        )
        execution_mode.add_argument(
            "--local",
            action="store_true",
            help="Run with local Ray instance (starts Ray automatically)",
        )
        execution_mode.add_argument(
            "--cluster",
            type=str,
            default=None,
            help=(
                "Connect to a Ray cluster at the specified address "
                "(e.g., auto or http://ray-head:8265)"
            ),
        )
        execution_mode.add_argument(
            "--ray-job",
            action="store_true",
            help="Submit tasks via the Ray Job Submission API (process isolation)",
        )
        parser.add_argument(
            "--drain",
            action="store_true",
            default=False,
            help=(
                "Drain mode for coordinated upgrades: stop claiming new tasks and passively "
                "wait for in-flight and DB-tracked queued/active work on the selected queues "
                "to reach zero (while existing workers finish it) up to "
                "--drain-timeout-seconds, then release the worker lease and exit."
            ),
        )
        parser.add_argument(
            "--drain-timeout-seconds",
            type=float,
            default=None,
            help=(
                "Maximum seconds to wait for in-flight and DB-tracked queued/active work to "
                "finish in --drain mode before exiting non-zero (default: 300)."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the worker loop."""
        queues = self._parse_queues(options)

        settings = get_settings()
        self.verbosity = int(options.get("verbosity", 1))
        concurrency = options.get("concurrency")
        if concurrency is not None and (
            type(concurrency) is not int or not 1 <= concurrency <= 1000
        ):
            raise CommandError("--concurrency must be an integer between 1 and 1000")

        raw_drain_timeout = options.get("drain_timeout_seconds")
        if raw_drain_timeout is not None:
            if (
                isinstance(raw_drain_timeout, bool)
                or not isinstance(raw_drain_timeout, (int, float))
                or not math.isfinite(float(raw_drain_timeout))
                or float(raw_drain_timeout) <= 0.0
                or float(raw_drain_timeout) > _MAX_DRAIN_TIMEOUT_SECONDS
            ):
                raise CommandError(
                    "--drain-timeout-seconds must be a finite number greater than 0 "
                    f"and at most {int(_MAX_DRAIN_TIMEOUT_SECONDS)}"
                )
            self.drain_timeout_seconds = float(raw_drain_timeout)
        else:
            self.drain_timeout_seconds = _DEFAULT_DRAIN_TIMEOUT_SECONDS
        self.drain_mode = bool(options.get("drain", False))
        self.drain_timed_out = False

        self.sync_mode = options.get("sync", False)
        self.local_mode = options.get("local", False)
        self.ray_job_mode = options.get("ray_job", False)
        self.cluster_address = options.get("cluster")

        if self.sync_mode:
            self.execution_mode = "sync"
        elif self.local_mode:
            self.execution_mode = "local"
        elif self.cluster_address:
            self.execution_mode = "cluster"
        elif self.ray_job_mode:
            self.execution_mode = "ray"
        else:
            default_mode, default_cluster_address = self._get_default_execution_mode(settings)
            self.execution_mode = default_mode
            if self.execution_mode == "cluster":
                assert default_cluster_address is not None, (
                    "_get_default_execution_mode() returns an address with cluster mode"
                )
                self.cluster_address = default_cluster_address

        if concurrency is None:
            concurrency = settings.get("DEFAULT_CONCURRENCY", 10)
        self.task_monitor_heartbeat_interval = float(
            settings.get("TASK_MONITOR_HEARTBEAT_SECONDS", 15)
        )
        raw_cancel_interval = settings.get(
            "WORKER_CANCELLATION_INTERVAL_SECONDS",
            _DEFAULT_CANCELLATION_INTERVAL_SECONDS,
        )
        if (
            isinstance(raw_cancel_interval, bool)
            or not isinstance(raw_cancel_interval, (int, float))
            or not math.isfinite(float(raw_cancel_interval))
            or float(raw_cancel_interval) <= 0.0
        ):
            raise CommandError(
                "WORKER_CANCELLATION_INTERVAL_SECONDS must be a finite number greater than 0"
            )
        self.cancellation_interval = float(raw_cancel_interval)
        self.poll_base_interval = float(settings.get("WORKER_POLL_INTERVAL_SECONDS", 0.1))
        self.poll_max_interval = float(settings.get("WORKER_POLL_MAX_INTERVAL_SECONDS", 1.0))
        self.polling_policy = self._new_polling_policy()

        dispatcher.register_trusted_worker_ray_addresses(self, settings)
        self._validate_execution_mode_configuration(settings)
        self.setup_signal_handlers()

        heartbeat_interval = get_heartbeat_interval().total_seconds()
        self._lease_heartbeat_interval_seconds = heartbeat_interval
        self._create_lease(queues[0] if len(queues) == 1 else ",".join(queues))

        try:
            self._write_worker_output(
                self.style.SUCCESS(f"Starting django-ray worker {self.worker_id}")
            )
            self._write_worker_output(f"  Queues: {', '.join(queues)}")
            self._write_worker_output(f"  Concurrency: {concurrency}")
            self._write_worker_output(f"  Mode: {self.execution_mode}")
            self._write_worker_output(
                f"  Polling: {self.poll_base_interval:g}s base, {self.poll_max_interval:g}s maximum"
            )
            if self.drain_mode:
                self._write_worker_output(
                    f"  Drain: enabled (timeout={self.drain_timeout_seconds:g}s)"
                )
            self._initialize_ray_execution()
            self.run_loop(
                queues=queues,
                concurrency=concurrency,
                heartbeat_interval=heartbeat_interval,
            )
        except KeyboardInterrupt:
            self.shutdown_signal = signal.SIGINT
            self.shutdown_exit_code = 128 + signal.SIGINT
            self.shutdown_requested = True
            self.stdout.write("\nShutdown requested via keyboard interrupt")
        except Exception:
            if self.shutdown_exit_code is None or self.shutdown_exit_code == 0:
                self.shutdown_exit_code = 1
            raise
        finally:
            self.shutdown()

        if self.shutdown_exit_code is not None and getattr(
            self, "_called_from_command_line", False
        ):
            raise SystemExit(self.shutdown_exit_code)

    def _write_worker_output(self, message: str, *, minimum_verbosity: int = 1) -> None:
        """Write informational worker output when Django verbosity permits it."""
        if self.verbosity >= minimum_verbosity:
            self.stdout.write(message)

    # ---- Claiming, queue affinity, and lease fencing delegates ----

    def _validate_execution_mode_configuration(self, settings: dict[str, Any]) -> None:
        claimer.validate_execution_mode_configuration(self, settings)

    def _get_default_execution_mode(self, settings: dict[str, Any]) -> tuple[str, str | None]:
        return claimer.get_default_execution_mode(settings)

    @classmethod
    def _requested_worker_family(cls, options: Mapping[str, Any]) -> str:
        return claimer.requested_worker_family(cls, options)

    def _active_worker_family(self) -> str:
        return claimer.active_worker_family(self)

    def _load_ray_backend_queue_configuration(self) -> _RayBackendQueueConfiguration:
        return claimer.load_ray_backend_queue_configuration(self)

    def _load_ray_job_queue_affinity(self) -> _RayJobQueueAffinity:
        return claimer.load_ray_job_queue_affinity(self)

    def _enforce_queue_affinity(
        self,
        queues: Sequence[str],
        *,
        worker_family: str,
        allow_filter: bool,
    ) -> list[str]:
        return claimer.enforce_queue_affinity(
            self,
            queues,
            worker_family=worker_family,
            allow_filter=allow_filter,
        )

    def _parse_queues(self, options: dict[str, Any]) -> list[str]:
        return claimer.parse_queues(self, options)

    def _create_lease(self, queue: str) -> None:
        claimer.create_lease(self, queue)

    def _recreate_lease(self, *, queue_name: str | None = None) -> bool:
        return claimer.recreate_lease(self, queue_name=queue_name)

    def _request_shutdown_for_lease_loss(
        self,
        reason: str,
        *,
        error: BaseException | None = None,
    ) -> None:
        claimer.request_shutdown_for_lease_loss(self, reason, error=error)

    def _update_lease_heartbeat(self) -> bool:
        return claimer.update_lease_heartbeat(self)

    def _resolve_active_lease_policy(self) -> lease_policy.LeasePolicy:
        return claimer.resolve_active_lease_policy(self)

    @staticmethod
    def _own_lease_freshness_q(
        duration: timedelta,
        *,
        validation_time: datetime | None = None,
    ) -> Any:
        return claimer.own_lease_freshness_q(duration, validation_time=validation_time)

    def _preflight_own_lease_active(self, *, mark_lost: bool = True) -> bool:
        return claimer.preflight_own_lease_active(self, mark_lost=mark_lost)

    @staticmethod
    def _explicit_protocol_range(lease: TaskWorkerLease) -> ExecutionProtocolRange | None:
        return claimer.explicit_protocol_range(lease)

    def _lock_authoritative_leases(
        self,
        *,
        source_worker_id: str | None,
        allow_takeover: bool,
        renew_heartbeat: bool = True,
        required_execution_protocol_version: int | None = None,
    ) -> TaskWorkerLease | None:
        return claimer.lock_authoritative_leases(
            self,
            source_worker_id=source_worker_id,
            allow_takeover=allow_takeover,
            renew_heartbeat=renew_heartbeat,
            required_execution_protocol_version=required_execution_protocol_version,
        )

    def _preliminary_takeover_protocol_range(self) -> ExecutionProtocolRange | None:
        return claimer.preliminary_takeover_protocol_range(self)

    def _authoritative_protocol_range_for_scan(self) -> ExecutionProtocolRange | None:
        return claimer.authoritative_protocol_range_for_scan(self)

    def _authoritative_task_owner(
        self,
        snapshot: RayTaskExecution,
        *,
        expected_state: str,
        allow_takeover: bool,
        require_completion_data_match: bool = False,
    ) -> AbstractContextManager[_OwnedTask | None]:
        return claimer.authoritative_task_owner(
            self,
            snapshot,
            expected_state=expected_state,
            allow_takeover=allow_takeover,
            require_completion_data_match=require_completion_data_match,
        )

    def _take_over_task_if_owner_stale(
        self,
        task: RayTaskExecution,
        *,
        now: datetime,
    ) -> RayTaskExecution | None:
        return claimer.take_over_task_if_owner_stale(self, task, now=now)

    def claim_and_process_tasks(self, queues: Sequence[str], concurrency: int) -> int:
        return claimer.claim_and_process_tasks(self, queues, concurrency)

    def _handoff_unsubmitted_task(self, task: RayTaskExecution) -> None:
        claimer.handoff_unsubmitted_task(self, task)

    # ---- Main loop, signals, and heartbeat delegates ----

    def setup_signal_handlers(self) -> None:
        loop.setup_signal_handlers(self)

    def _preserve_shutdown_signal_handlers(self) -> AbstractContextManager[None]:
        return loop.preserve_shutdown_signal_handlers(self)

    def handle_shutdown_signal(self, signum: int, frame: FrameType | None) -> None:
        loop.handle_shutdown_signal(self, signum, frame)

    def _log_received_shutdown_signal(self) -> None:
        loop.log_received_shutdown_signal(self)

    def _within_transient_db_recovery_budget(self) -> bool:
        return loop.within_transient_db_recovery_budget(self)

    def _wait_for_poll_deadline(self, timeout: float) -> None:
        loop.wait_for_poll_deadline(self, timeout)

    def run_loop(
        self,
        queues: Sequence[str],
        concurrency: int,
        heartbeat_interval: float,
    ) -> None:
        loop.run_loop(self, queues, concurrency, heartbeat_interval)

    def send_heartbeat(self) -> None:
        loop.send_heartbeat(self)

    # ---- Dispatch, Ray init/reconnect, and persistence delegates ----

    def _initialize_ray_execution(self) -> None:
        dispatcher.initialize_ray_execution(self)

    def _create_ray_core_runner(self) -> RayCoreRunner:
        return dispatcher.create_ray_core_runner(self)

    def _init_local_ray(self) -> None:
        dispatcher.init_local_ray(self)

    def _init_cluster_ray(self, address: str) -> None:
        dispatcher.init_cluster_ray(self, address)

    def _get_ray_cluster_resources_with_timeout(
        self, timeout_seconds: float
    ) -> dict[str, Any] | None:
        return dispatcher.get_ray_cluster_resources_with_timeout(timeout_seconds)

    def _is_ray_core_initialized(self) -> bool:
        return dispatcher.is_ray_core_initialized()

    def _reset_ray_reconnect_backoff(self) -> None:
        dispatcher.reset_ray_reconnect_backoff(self)

    def _schedule_ray_reconnect_backoff(self) -> float:
        return dispatcher.schedule_ray_reconnect_backoff(self)

    def _maybe_renew_lease_during_reconnect(
        self,
        *,
        last_renewal_monotonic: float,
        elapsed_since_renewal: float,
        heartbeat_interval: float,
    ) -> tuple[float, float] | None:
        return dispatcher.maybe_renew_lease_during_reconnect(
            self,
            last_renewal_monotonic=last_renewal_monotonic,
            elapsed_since_renewal=elapsed_since_renewal,
            heartbeat_interval=heartbeat_interval,
        )

    def _wait_for_reconnect_delay(
        self,
        delay: float,
        *,
        last_renewal_monotonic: float,
        elapsed_since_renewal: float,
        heartbeat_interval: float,
    ) -> tuple[float, float] | None:
        return dispatcher.wait_for_reconnect_delay(
            self,
            delay,
            last_renewal_monotonic=last_renewal_monotonic,
            elapsed_since_renewal=elapsed_since_renewal,
            heartbeat_interval=heartbeat_interval,
        )

    def _ray_health_check_timeout_seconds(self) -> float:
        return dispatcher.ray_health_check_timeout_seconds(self)

    def _check_ray_connection(self) -> None:
        dispatcher.check_ray_connection(self)

    def _reconnect_ray(self) -> None:
        dispatcher.reconnect_ray(self)

    def _can_spawn_sync_heartbeat_thread(self) -> bool:
        return dispatcher.can_spawn_sync_heartbeat_thread(self)

    def _sync_execution_heartbeat(self, task: RayTaskExecution) -> AbstractContextManager[None]:
        return dispatcher.sync_execution_heartbeat(self, task)

    def _prepare_result_outside_lock(
        self,
        task: RayTaskExecution,
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
        return dispatcher.prepare_result_outside_lock(
            self,
            task,
            result_value,
            prepared_result_reference=prepared_result_reference,
            expected_ray_job_id=expected_ray_job_id,
            expected_claimed_by_worker=expected_claimed_by_worker,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_completion_data=expected_completion_data,
            require_completion_data_match=require_completion_data_match,
            supported_protocols=supported_protocols,
        )

    def _fenced_handle_task_failure(
        self,
        task: RayTaskExecution,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        return dispatcher.fenced_handle_task_failure(self, task, *args, **kwargs)

    def _fenced_persist_submission_tracking(
        self,
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
        expected_ray_job_id: str | None,
    ) -> bool:
        return dispatcher.fenced_persist_submission_tracking(
            self,
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_ray_job_id=expected_ray_job_id,
        )

    def process_task(self, task: RayTaskExecution) -> None:
        dispatcher.process_task(self, task)

    def execute_task_sync(self, task: RayTaskExecution) -> None:
        dispatcher.execute_task_sync(self, task)

    def _store_task_result(
        self,
        task: RayTaskExecution,
        result_value: Any,
    ) -> tuple[str, ...]:
        return dispatcher.store_task_result(self, task, result_value)

    def _store_and_succeed_task(
        self,
        task: RayTaskExecution,
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
        return dispatcher.store_and_succeed_task(
            self,
            task,
            result_value,
            prepared_result_data=prepared_result_data,
            prepared_result_reference=prepared_result_reference,
            _result_already_stored=_result_already_stored,
            _storage_diagnostics=_storage_diagnostics,
            _already_locked=_already_locked,
            expected_ray_job_id=expected_ray_job_id,
            expected_claimed_by_worker=expected_claimed_by_worker,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_completion_data=expected_completion_data,
            require_completion_data_match=require_completion_data_match,
            supported_protocols=supported_protocols,
            executor_django_ray_version=executor_django_ray_version,
        )

    def _handle_task_failure(
        self,
        task: RayTaskExecution,
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
        return dispatcher.handle_task_failure(
            self,
            task,
            error_message,
            error_traceback=error_traceback,
            exception_type=exception_type,
            retryable=retryable,
            expected_ray_job_id=expected_ray_job_id,
            expected_claimed_by_worker=expected_claimed_by_worker,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_completion_data=expected_completion_data,
            require_completion_data_match=require_completion_data_match,
            cancellation_status=cancellation_status,
            cancellation_error=cancellation_error,
            supported_protocols=supported_protocols,
            executor_django_ray_version=executor_django_ray_version,
        )

    @classmethod
    def _persist_submission_tracking(
        cls,
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
        expected_ray_job_id: str | None,
    ) -> bool:
        return dispatcher.persist_submission_tracking(
            cls,
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            expected_ray_job_id=expected_ray_job_id,
        )

    @classmethod
    def _release_submission_tracking(
        cls,
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
    ) -> bool:
        return dispatcher.release_submission_tracking(
            cls,
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )

    def _cancel_untracked_submission(
        self,
        runner: Any,
        handle: SubmissionHandle,
        *,
        backend_name: str,
        prepared: PreparedRemoteCancellation | None = None,
    ) -> CancellationOutcome:
        return dispatcher.cancel_untracked_submission(
            self,
            runner,
            handle,
            backend_name=backend_name,
            prepared=prepared,
        )

    def _cancel_mismatched_submissions(
        self,
        runner: Any,
        reserved_handle: SubmissionHandle,
        observed_handle: SubmissionHandle,
        *,
        prepared: dict[tuple[str, str], PreparedRemoteCancellation] | None = None,
    ) -> CancellationOutcome:
        return dispatcher.cancel_mismatched_submissions(
            self,
            runner,
            reserved_handle,
            observed_handle,
            prepared=prepared,
        )

    @classmethod
    def _inspect_submission_tracking(
        cls,
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
    ) -> tuple[str, str | None, str | None]:
        return dispatcher.inspect_submission_tracking(
            cls,
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )

    def _terminalize_mismatched_ray_job_submission(
        self,
        current: RayTaskExecution,
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
        return dispatcher.terminalize_mismatched_ray_job_submission(
            self,
            current,
            reserved_handle=reserved_handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            error_message=error_message,
            exception_type=exception_type,
            cancellation=cancellation,
            supported_protocols=supported_protocols,
            _pre_stored=_pre_stored,
            _pre_result_data=_pre_result_data,
            _pre_result_reference=_pre_result_reference,
            _pre_diagnostics=_pre_diagnostics,
            _pre_storage_error=_pre_storage_error,
        )

    def _handle_ray_job_confirmation_loss(
        self,
        task: RayTaskExecution,
        runner: Any,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
        detail: str,
    ) -> None:
        dispatcher.handle_ray_job_confirmation_loss(
            self,
            task,
            runner,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            detail=detail,
        )

    def _handle_mismatched_ray_job_submission(
        self,
        task: RayTaskExecution,
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
        dispatcher.handle_mismatched_ray_job_submission(
            self,
            task,
            runner,
            reserved_handle,
            observed_handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
            error_message=error_message,
            exception_type=exception_type,
        )

    def submit_task_to_ray_core(self, task: RayTaskExecution) -> None:
        dispatcher.submit_task_to_ray_core(self, task)

    def submit_task_to_ray(self, task: RayTaskExecution) -> None:
        dispatcher.submit_task_to_ray(self, task)

    def _build_submission_handle(
        self,
        task: RayTaskExecution,
        ray_job_id: str,
    ) -> SubmissionHandle:
        return dispatcher.build_submission_handle(self, task, ray_job_id)

    # ---- Reconciliation and polling delegates ----

    def _mark_task_monitor_heartbeat(
        self,
        task: RayTaskExecution,
        *,
        now: datetime | None = None,
        ray_job_id: str | None = None,
        expected_worker_id: str | None = None,
        attempt_number: int | None = None,
        execution_generation: int | None = None,
    ) -> None:
        reconciler.mark_task_monitor_heartbeat(
            self,
            task,
            now=now,
            ray_job_id=ray_job_id,
            expected_worker_id=expected_worker_id,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
        )

    def _partition_ray_core_handles(
        self,
        runner: RayCoreRunner,
        handles: tuple[RayCoreHandle, ...],
        *,
        heartbeat_at: datetime | None = None,
    ) -> tuple[RayCoreHandle, ...] | None:
        return reconciler.partition_ray_core_handles(
            self,
            runner,
            handles,
            heartbeat_at=heartbeat_at,
        )

    def _completion_envelope_grace_expired(
        self,
        task: RayTaskExecution,
        *,
        now: datetime,
    ) -> bool:
        return reconciler.completion_envelope_grace_expired(self, task, now=now)

    @staticmethod
    def _execution_completion_identity(task: RayTaskExecution) -> ExecutionIdentity:
        return reconciler.execution_completion_identity(task)

    @classmethod
    def _inspect_execution_completion(
        cls,
        serialized: object,
        task: RayTaskExecution,
        *,
        supported_protocols: ExecutionProtocolRange,
    ) -> _InspectedExecutionCompletion:
        return reconciler.inspect_execution_completion(
            cls,
            serialized,
            task,
            supported_protocols=supported_protocols,
        )

    @staticmethod
    def _completion_rejection_policy(
        inspection: _InspectedExecutionCompletion,
    ) -> tuple[str, str, bool | None]:
        return reconciler.completion_rejection_policy(inspection)

    def _mark_stale_ray_core_tasks_as_lost(self) -> None:
        reconciler.mark_stale_ray_core_tasks_as_lost(self)

    def _terminalize_lost_ray_core_handles(
        self,
        runner: RayCoreRunner,
        handles: tuple[RayCoreHandle, ...],
        *,
        error_message: str,
    ) -> int:
        return reconciler.terminalize_lost_ray_core_handles(
            self,
            runner,
            handles,
            error_message=error_message,
        )

    def _finalize_ray_core_cancellation(
        self,
        *,
        task_pk: int,
        attempt_number: int,
        execution_generation: int,
    ) -> bool:
        return reconciler.finalize_ray_core_cancellation(
            self,
            task_pk=task_pk,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
        )

    def _apply_ray_core_completion(self, completion: Any) -> bool:
        return reconciler.apply_ray_core_completion(self, completion)

    def poll_ray_core_tasks(self) -> int:
        return reconciler.poll_ray_core_tasks(self)

    def _adopt_orphaned_ray_job_task(self, task: RayTaskExecution, *, now: datetime) -> bool:
        return reconciler.adopt_orphaned_ray_job_task(self, task, now=now)

    def _retire_active_ray_job_tracking(
        self,
        task_pk: int,
        *,
        ray_job_id: str,
        identity: tuple[int, int],
    ) -> None:
        reconciler.retire_active_ray_job_tracking(
            self,
            task_pk,
            ray_job_id=ray_job_id,
            identity=identity,
        )

    def _reconcile_ray_job_task(
        self,
        task: RayTaskExecution,
        runner: Any,
        *,
        ray_job_id: str,
        completed_tasks: list[int],
        orphaned: bool,
        tracked_identity: tuple[int, int] | None = None,
        completion_only: bool = False,
    ) -> None:
        reconciler.reconcile_ray_job_task(
            self,
            task,
            runner,
            ray_job_id=ray_job_id,
            completed_tasks=completed_tasks,
            orphaned=orphaned,
            tracked_identity=tracked_identity,
            completion_only=completion_only,
        )

    def poll_ray_job_completions(self) -> int:
        return reconciler.poll_ray_job_completions(self)

    def _active_reconciliation_page(self) -> list[int]:
        return reconciler.active_reconciliation_page(self)

    def reconcile_tasks(self, queues: Sequence[str] | None = None) -> int:
        return reconciler.reconcile_tasks(self, queues=queues)

    # ---- Maintenance, timeout, and cancellation delegates ----

    @classmethod
    def _defer_maintenance_scan_fields(cls, queryset: Any) -> Any:
        return maintenance.defer_maintenance_scan_fields(queryset)

    def _maintenance_scan_page(
        self,
        kind: str,
        queryset: Any,
        queues: Sequence[str] | None,
        supported_protocols: ExecutionProtocolRange,
    ) -> Any:
        return maintenance.maintenance_scan_page(self, kind, queryset, queues, supported_protocols)

    def _maintenance_deadline(self, kind: str, interval: float) -> float:
        return maintenance.maintenance_deadline(self, kind, interval)

    def _maintenance_active_workers(self, candidates: Any) -> set[str]:
        return maintenance.maintenance_active_workers(self, candidates)

    def _maintenance_snapshot(
        self,
        kind: str,
        queryset: Any,
        candidate: MaintenanceCandidate,
    ) -> Any:
        return maintenance.maintenance_snapshot(self, kind, queryset, candidate)

    def _request_timeout_cancellation(self, task: RayTaskExecution) -> CancellationOutcome:
        return maintenance.request_timeout_cancellation(self, task)

    def detect_stuck_tasks(self, queues: Sequence[str] | None = None) -> int:
        return maintenance.detect_stuck_tasks(self, queues=queues)

    def cleanup_expired_leases(self) -> int:
        return maintenance.cleanup_expired_leases(self)

    def _claim_orphaned_cancellation(self, task: RayTaskExecution, *, now: datetime) -> bool:
        return maintenance.claim_orphaned_cancellation(self, task, now=now)

    def _prepare_remote_cancellation_for_task(
        self,
        task: RayTaskExecution,
    ) -> tuple[str, str] | None:
        return maintenance.prepare_remote_cancellation_for_task(self, task)

    def _request_cancellation_for_task(self, task: RayTaskExecution) -> CancellationOutcome:
        return maintenance.request_cancellation_for_task(self, task)

    def process_cancellations(self, queues: Sequence[str] | None = None) -> int:
        return maintenance.process_cancellations(self, queues=queues)

    # ---- Shutdown delegates ----

    def _prepare_shutdown_handoff(self) -> None:
        shutdown.prepare_shutdown_handoff(self)

    def shutdown(self) -> None:
        shutdown.shutdown(self)
