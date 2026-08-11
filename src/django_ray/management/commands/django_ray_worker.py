"""Django management command for running the django-ray worker."""

from __future__ import annotations

import json
import random
import signal
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError, connection, transaction

from django_ray import __version__ as django_ray_version
from django_ray.conf.settings import get_settings
from django_ray.execution_codec import (
    DecodedExecutionCompletion,
    ExecutionCompletionDecodeError,
    ExecutionCompletionRejection,
    ExecutionCompletionSource,
    ExecutionIdentity,
    decode_execution_completion,
)
from django_ray.execution_protocol import (
    MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    SUPPORTED_EXECUTION_PROTOCOL_RANGE,
    WORKER_CAPABILITY_SCHEMA_VERSION,
    ExecutionProtocolRange,
    explicit_worker_protocol_range,
)
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
from django_ray.management.diagnostics import (
    render_console_diagnostic,
    render_console_resource_summary,
)
from django_ray.models import CancellationStatus, RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.ray_job_protocol import (
    RayJobRequestBindingError,
    RayJobRequestExpectation,
    RayJobRequestReferenceExpectation,
    is_rq2_ray_job_submission_id,
    is_strict_ray_job_submission_id,
    is_valid_strict_ray_job_submission_id,
    parse_ray_job_request_metadata,
    ray_job_metadata_has_strict_marker,
    validate_ray_job_request_expectation,
    validate_ray_job_request_reference_expectation,
)
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
    finalize_cancellation,
    prepare_remote_cancellation,
    request_remote_cancellation,
)
from django_ray.runner.leasing import (
    WorkerLeaseIdentity,
    generate_worker_id,
    get_heartbeat_interval,
    get_lease_duration,
    is_worker_id_primary_key_collision,
)
from django_ray.runner.polling import AdaptivePollingPolicy
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner
from django_ray.runner.reconciliation import (
    get_stuck_timeout,
    is_task_stuck,
    is_task_timed_out,
    mark_task_lost,
    mark_task_timed_out,
)
from django_ray.runner.retry import RetryDecision, should_retry
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    runtime_env_for_execution,
)

_WORKER_ID_ALLOCATION_ATTEMPTS = 3


class _WorkerIdReservedByTaskError(Exception):
    """Signal that a retained in-flight task still owns a worker ID."""


@dataclass(frozen=True, slots=True)
class _OwnedTask:
    """One execution held behind this process's exact live lease fence."""

    execution: RayTaskExecution
    adopted: bool
    supported_protocols: ExecutionProtocolRange


@dataclass(frozen=True, slots=True)
class _InspectedExecutionCompletion:
    """One completion decoded inside the authoritative task boundary."""

    decoded: DecodedExecutionCompletion | None
    prepared_result_reference: str | None
    rejection: ExecutionCompletionRejection | None
    requires_nonretryable_disposition: bool


@dataclass(frozen=True)
class _RayBackendQueueConfiguration:
    """Validated queue ownership needed before a worker may claim work."""

    aliases: tuple[str, ...]
    queues: tuple[str, ...]
    queues_by_alias: Mapping[str, tuple[str, ...]]
    ray_targets_by_alias: Mapping[str, str]


@dataclass(frozen=True)
class _RayJobQueueAffinity:
    """Raw opt-in queue reservations that do not import unrelated backends."""

    aliases_by_queue: Mapping[str, tuple[str, ...]]


class Command(BaseCommand):
    """Run a django-ray worker process."""

    help = "Run a django-ray worker that claims and executes tasks on Ray"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shutdown_requested = False
        self.lease_ownership_lost = False
        self.worker_id = generate_worker_id()
        self.logger = get_worker_logger(self.worker_id)
        self.active_tasks: dict[int, str] = {}  # task_pk -> ray_job_id (for Ray Job API mode)
        self.active_task_identities: dict[int, tuple[int, int]] = {}
        self.ray_core_runner: RayCoreRunner | None = None  # For local/cluster modes
        self.last_reconciliation = 0.0  # Last time we ran stuck task detection
        self.reconciliation_interval = 30.0  # Check for stuck tasks every 30 seconds
        self.timeout_check_interval = 30.0
        self.cancellation_interval = 30.0
        self.lease_cleanup_interval = 30.0
        self.lease: TaskWorkerLease | None = None  # Worker lease for coordination
        self.lease_identity: WorkerLeaseIdentity | None = None
        self.lease_queue_name: str = "default"  # Queue name retained for exact renewal
        self.last_task_processed = 0.0  # Last time we processed a task
        self.tasks_processed_count = 0  # Total tasks processed
        self.task_monitor_heartbeat_interval = 15.0
        self.last_task_monitor_heartbeat = 0.0
        self.completion_poll_interval = 0.1
        self.poll_base_interval = 0.1
        self.poll_max_interval = 0.1
        self.polling_policy = self._new_polling_policy()
        # A signal requests a graceful handoff.  Keep the signal number so the
        # command-line entrypoint can preserve the conventional 128+N status.
        self.shutdown_signal: int | None = None
        self.shutdown_exit_code: int | None = None
        self.verbosity = 1
        self.execution_mode = "sync"
        self.sync_mode = False
        self.local_mode = False
        self.cluster_address: str | None = None
        self._ray_backend_queue_configuration: _RayBackendQueueConfiguration | None = None
        self._ray_job_queue_affinity: _RayJobQueueAffinity | None = None

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
            help="Connect to a Ray cluster at the specified address (e.g., ray://localhost:10001)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the worker loop."""
        # Parse queue arguments - support multiple ways to specify queues
        queues = self._parse_queues(options)

        settings = get_settings()
        self.verbosity = int(options.get("verbosity", 1))
        concurrency = options.get("concurrency")
        if concurrency is not None and (
            type(concurrency) is not int or not 1 <= concurrency <= 1000
        ):
            raise CommandError("--concurrency must be an integer between 1 and 1000")
        self.sync_mode = options.get("sync", False)
        self.local_mode = options.get("local", False)
        self.cluster_address = options.get("cluster")

        # Determine execution mode without touching Ray. The database lease is
        # acquired first so a colliding identity can never initialize a driver
        # or begin claiming work.
        if self.sync_mode:
            self.execution_mode = "sync"
        elif self.local_mode:
            self.execution_mode = "local"
        elif self.cluster_address:
            self.execution_mode = "cluster"
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
        self.poll_base_interval = float(settings.get("WORKER_POLL_INTERVAL_SECONDS", 0.1))
        self.poll_max_interval = float(settings.get("WORKER_POLL_MAX_INTERVAL_SECONDS", 0.1))
        self.polling_policy = self._new_polling_policy()

        self._validate_execution_mode_configuration(settings)
        self.setup_signal_handlers()

        heartbeat_interval = get_heartbeat_interval().total_seconds()

        # Create worker lease for distributed coordination (use first queue for lease)
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
        finally:
            self.shutdown()

        # ``BaseCommand.run_from_argv`` does not use the return value from
        # ``handle``.  Raise only for the real CLI path so direct ``handle``
        # calls (and ``call_command`` users) retain normal Python semantics.
        if self.shutdown_exit_code is not None and getattr(
            self, "_called_from_command_line", False
        ):
            raise SystemExit(self.shutdown_exit_code)

    def _validate_execution_mode_configuration(self, settings: dict[str, Any]) -> None:
        """Fail before lease creation when Ray Job request storage is unusable."""
        if self.execution_mode != "ray":
            return

        from django.core.exceptions import ImproperlyConfigured

        from django_ray.conf.settings import validate_ray_job_request_storage_settings
        from django_ray.ray_job_request_storage import (
            RayJobRequestStorageError,
            validate_ray_job_request_storage_config,
        )

        try:
            validate_ray_job_request_storage_settings(settings)
            validate_ray_job_request_storage_config(settings)
        except (ImproperlyConfigured, RayJobRequestStorageError) as error:
            diagnostic = render_console_diagnostic(error)
            raise CommandError(
                f"Ray Job request storage configuration is invalid: {diagnostic}"
            ) from None

    def _initialize_ray_execution(self) -> None:
        """Initialize Ray only after this process owns its database lease."""
        if self.execution_mode == "local":
            try:
                self._init_local_ray()
                self.ray_core_runner = RayCoreRunner()
            except Exception as error:
                diagnostic = render_console_diagnostic(error)
                self.stdout.write(self.style.WARNING(f"Initial Ray init failed: {diagnostic}"))
                self.stdout.write("Will retry connection during operation...")
        elif self.execution_mode == "cluster":
            assert self.cluster_address is not None
            try:
                self._init_cluster_ray(self.cluster_address)
                self.ray_core_runner = RayCoreRunner()
            except Exception as error:
                diagnostic = render_console_diagnostic(error)
                self.stdout.write(
                    self.style.WARNING(f"Initial cluster connection failed: {diagnostic}")
                )
                self.stdout.write("Will retry connection during operation...")

    def _get_default_execution_mode(self, settings: dict[str, Any]) -> tuple[str, str | None]:
        """Resolve default worker mode from settings when no CLI mode flag is set.

        Returns:
            Tuple of (execution_mode, cluster_address).
        """
        runner = settings.get("RUNNER", "ray_job")
        if runner == "ray_core":
            ray_address = settings.get("RAY_ADDRESS")
            if ray_address and ray_address != "auto":
                return "cluster", str(ray_address)
            return "local", None

        # ray_job default
        return "ray", None

    @staticmethod
    def _requested_worker_family(options: Mapping[str, Any]) -> str:
        """Return the process-wide runner family selected by CLI flags/settings."""
        if options.get("sync"):
            return "sync"
        if options.get("local") or options.get("cluster"):
            return "ray_core"
        if get_settings().get("RUNNER", "ray_job") == "ray_core":
            return "ray_core"
        return "ray_job"

    def _active_worker_family(self) -> str:
        if self.execution_mode == "ray":
            return "ray_job"
        if self.execution_mode == "sync":
            return "sync"
        return "ray_core"

    def _load_ray_backend_queue_configuration(
        self,
    ) -> _RayBackendQueueConfiguration:
        """Resolve Ray backend queues and fail closed on ambiguous affinity policy."""
        if self._ray_backend_queue_configuration is not None:
            return self._ray_backend_queue_configuration

        from django.conf import settings as django_settings
        from django.tasks import DEFAULT_TASK_QUEUE_NAME
        from django.utils.module_loading import import_string

        from django_ray.backends import RayTaskBackend

        tasks_config = getattr(django_settings, "TASKS", {})
        if not isinstance(tasks_config, Mapping):
            raise CommandError("TASKS must be a mapping to resolve django-ray queue affinity")

        aliases: list[str] = []
        configured_queues: list[str] = []
        seen_queues: set[str] = set()
        queues_by_alias: dict[str, tuple[str, ...]] = {}
        ray_targets_by_alias: dict[str, str] = {}
        worker_settings = get_settings()

        for alias, backend_config in tasks_config.items():
            if not isinstance(alias, str) or not isinstance(backend_config, Mapping):
                raise CommandError("TASKS aliases must map names to backend settings")
            backend_path = backend_config.get("BACKEND")
            if not isinstance(backend_path, str):
                raise CommandError(f"TASKS backend {alias!r} must define BACKEND")
            try:
                backend_class = import_string(backend_path)
            except ImportError:
                raise CommandError(
                    f"Cannot import TASKS backend {alias!r} while resolving queue affinity"
                ) from None
            if not isinstance(backend_class, type):
                raise CommandError(f"TASKS backend {alias!r} does not resolve to a class")
            if not issubclass(backend_class, RayTaskBackend):
                continue

            options = backend_config.get("OPTIONS", {})
            if not isinstance(options, Mapping):
                raise CommandError(f"TASKS backend {alias!r} OPTIONS must be a mapping")
            ray_job_only = options.get("RAY_JOB_ONLY", False)
            if type(ray_job_only) is not bool:
                raise CommandError(
                    f"TASKS backend {alias!r} OPTIONS['RAY_JOB_ONLY'] must be a boolean"
                )
            ray_target = options.get("RAY_ADDRESS", worker_settings.get("RAY_ADDRESS", "auto"))
            if not isinstance(ray_target, str) or not ray_target.strip():
                raise CommandError(
                    f"TASKS backend {alias!r} RAY_ADDRESS must be a non-empty string"
                )

            raw_queues = backend_config.get("QUEUES", [DEFAULT_TASK_QUEUE_NAME])
            if isinstance(raw_queues, (str, bytes)) or not isinstance(
                raw_queues, Sequence | set | frozenset
            ):
                raise CommandError(
                    f"TASKS backend {alias!r} QUEUES must be a collection of queue names"
                )
            queue_values = list(raw_queues)
            for queue_name in queue_values:
                if not isinstance(queue_name, str) or not queue_name.strip():
                    raise CommandError(
                        f"TASKS backend {alias!r} QUEUES must contain non-empty strings"
                    )
            if isinstance(raw_queues, set | frozenset):
                queue_values.sort()

            aliases.append(alias)
            queues_by_alias[alias] = tuple(queue_values)
            ray_targets_by_alias[alias] = ray_target
            for queue_name in queue_values:
                if queue_name not in seen_queues:
                    configured_queues.append(queue_name)
                    seen_queues.add(queue_name)

        configuration = _RayBackendQueueConfiguration(
            aliases=tuple(aliases),
            queues=tuple(configured_queues),
            queues_by_alias=queues_by_alias,
            ray_targets_by_alias=ray_targets_by_alias,
        )
        self._ray_backend_queue_configuration = configuration
        return configuration

    def _load_ray_job_queue_affinity(self) -> _RayJobQueueAffinity:
        """Read only aliases that explicitly opt queues into Ray Job ownership.

        Explicit queue selection historically did not import every configured task
        backend. Keep that compatibility for mixed-backend projects while validating
        every opted-in backend before applying its fail-closed reservation.
        """
        if self._ray_job_queue_affinity is not None:
            return self._ray_job_queue_affinity

        from django.conf import settings as django_settings
        from django.tasks import DEFAULT_TASK_QUEUE_NAME
        from django.utils.module_loading import import_string

        from django_ray.backends import RayTaskBackend

        tasks_config = getattr(django_settings, "TASKS", {})
        if not isinstance(tasks_config, Mapping):
            raise CommandError("TASKS must be a mapping to resolve django-ray queue affinity")

        restricted_aliases: dict[str, list[str]] = {}
        for alias, backend_config in tasks_config.items():
            if not isinstance(backend_config, Mapping):
                continue
            options = backend_config.get("OPTIONS")
            if not isinstance(options, Mapping) or "RAY_JOB_ONLY" not in options:
                continue
            ray_job_only = options["RAY_JOB_ONLY"]
            if type(ray_job_only) is not bool:
                raise CommandError(
                    f"TASKS backend {alias!r} OPTIONS['RAY_JOB_ONLY'] must be a boolean"
                )
            if not ray_job_only:
                continue
            if not isinstance(alias, str):
                raise CommandError("RAY_JOB_ONLY TASKS aliases must be strings")
            backend_path = backend_config.get("BACKEND")
            if not isinstance(backend_path, str):
                raise CommandError(f"RAY_JOB_ONLY TASKS backend {alias!r} must define BACKEND")
            try:
                backend_class = import_string(backend_path)
            except ImportError:
                raise CommandError(
                    f"Cannot import TASKS backend {alias!r} while validating RAY_JOB_ONLY"
                ) from None
            if not isinstance(backend_class, type):
                raise CommandError(
                    f"RAY_JOB_ONLY TASKS backend {alias!r} does not resolve to a class"
                )
            if not issubclass(backend_class, RayTaskBackend):
                continue

            raw_queues = backend_config.get("QUEUES", [DEFAULT_TASK_QUEUE_NAME])
            if isinstance(raw_queues, (str, bytes)) or not isinstance(
                raw_queues, Sequence | set | frozenset
            ):
                raise CommandError(
                    f"RAY_JOB_ONLY TASKS backend {alias!r} QUEUES must be a collection "
                    "of queue names"
                )
            queue_values = list(raw_queues)
            if not queue_values:
                raise CommandError(
                    f"RAY_JOB_ONLY TASKS backend {alias!r} must declare at least one queue"
                )
            for queue_name in queue_values:
                if not isinstance(queue_name, str) or not queue_name.strip():
                    raise CommandError(
                        f"RAY_JOB_ONLY TASKS backend {alias!r} QUEUES must contain "
                        "non-empty strings"
                    )
                restricted_aliases.setdefault(queue_name, []).append(alias)

        affinity = _RayJobQueueAffinity(
            aliases_by_queue={
                queue: tuple(aliases) for queue, aliases in restricted_aliases.items()
            }
        )
        self._ray_job_queue_affinity = affinity
        return affinity

    def _enforce_queue_affinity(
        self,
        queues: Sequence[str],
        *,
        worker_family: str,
        allow_filter: bool,
    ) -> list[str]:
        """Keep Ray Job-only queues away from Ray Core and synchronous workers."""
        selected = list(queues)
        if not selected or any(
            not isinstance(queue, str) or not queue.strip() for queue in selected
        ):
            raise CommandError("Queue selection must contain at least one non-empty queue name")
        affinity = self._load_ray_job_queue_affinity()
        if worker_family == "ray_job":
            return selected

        blocked = [queue for queue in selected if queue in affinity.aliases_by_queue]
        if not blocked:
            return selected

        owner_aliases = sorted(
            {alias for queue in blocked for alias in affinity.aliases_by_queue[queue]}
        )
        mode_label = "Ray Core" if worker_family == "ray_core" else "synchronous"
        blocked_text = ", ".join(blocked)
        owners_text = ", ".join(owner_aliases)
        if allow_filter:
            allowed = [queue for queue in selected if queue not in blocked]
            self.stdout.write(
                self.style.NOTICE(
                    f"Skipping Ray Job-only queue(s) [{blocked_text}] declared by "
                    f"TASKS backend alias(es) [{owners_text}] for this {mode_label} worker"
                )
            )
            if allowed:
                return allowed
            raise CommandError(
                f"--all-queues found no queues compatible with this {mode_label} worker; "
                "start a Ray Job worker or configure at least one unrestricted queue"
            )

        raise CommandError(
            f"This {mode_label} worker cannot claim Ray Job-only queue(s) [{blocked_text}]. "
            f"TASKS backend alias(es) [{owners_text}] declare "
            "OPTIONS['RAY_JOB_ONLY']=True; start a Ray Job worker without "
            "--sync, --local, or --cluster, or select compatible queues"
        )

    def _parse_queues(self, options: dict[str, Any]) -> list[str]:
        """Parse queue arguments from command options.

        Supports multiple ways to specify queues:
        - --queue default (single queue)
        - --queue default,high-priority,low-priority (comma-separated)
        - --queues default high-priority low-priority (space-separated)
        - --all-queues (all queues configured on RayTaskBackend aliases)

        Args:
            options: Command options dictionary.

        Returns:
            List of queue names to process.
        """
        # Check for --all-queues flag first
        if options.get("all_queues"):
            configuration = self._load_ray_backend_queue_configuration()
            if not configuration.aliases:
                raise CommandError("--all-queues found no TASKS backend using RayTaskBackend")
            for alias in configuration.aliases:
                if not configuration.queues_by_alias[alias]:
                    raise CommandError(
                        f"TASKS backend {alias!r} has no enumerable QUEUES; "
                        "use --queue or --queues explicitly"
                    )

            worker_family = self._requested_worker_family(options)
            configured_queues = self._enforce_queue_affinity(
                configuration.queues,
                worker_family=worker_family,
                allow_filter=True,
            )
            eligible = set(configured_queues)
            ray_backend_targets = {
                alias: configuration.ray_targets_by_alias[alias]
                for alias in configuration.aliases
                if worker_family == "ray_core"
                and eligible.intersection(configuration.queues_by_alias[alias])
            }
            if len(set(ray_backend_targets.values())) > 1:
                aliases = ", ".join(ray_backend_targets)
                raise CommandError(
                    "--all-queues cannot combine django-ray backends with different "
                    f"RAY_ADDRESS values in Ray Core mode ({aliases}); use --queue or "
                    "--queues for one compatible target, or use Ray Job mode"
                )
            self.stdout.write(
                self.style.NOTICE(
                    "Processing all configured django-ray queues "
                    f"from {list(configuration.aliases)}: {configured_queues}"
                )
            )
            return configured_queues

        # Check for --queues (space-separated list)
        if options.get("queues") is not None:
            selected = options["queues"]
            return self._enforce_queue_affinity(
                selected,
                worker_family=self._requested_worker_family(options),
                allow_filter=False,
            )

        # Check for --queue (single or comma-separated)
        queue_arg = options.get("queue")
        if queue_arg is not None:
            if "," in queue_arg:
                selected = [q.strip() for q in queue_arg.split(",") if q.strip()]
            else:
                selected = [queue_arg]
            return self._enforce_queue_affinity(
                selected,
                worker_family=self._requested_worker_family(options),
                allow_filter=False,
            )

        # Default to "default" queue
        return self._enforce_queue_affinity(
            ["default"],
            worker_family=self._requested_worker_family(options),
            allow_filter=False,
        )

    def _init_local_ray(self) -> None:
        """Initialize a local Ray instance."""
        import os
        import sys

        import ray

        # Clear RAY_ADDRESS to ensure we start a fresh local instance
        if "RAY_ADDRESS" in os.environ:
            address = render_console_diagnostic(os.environ["RAY_ADDRESS"])
            self.stdout.write(self.style.WARNING(f"Clearing RAY_ADDRESS={address} for local mode"))
            del os.environ["RAY_ADDRESS"]

        # Disable Ray's uv runtime env hook - it causes issues on Windows
        # when Ray tries to spawn workers with 'uv run' which may not be in PATH
        if "RAY_RUNTIME_ENV_HOOK" in os.environ:
            del os.environ["RAY_RUNTIME_ENV_HOOK"]

        if not ray.is_initialized():
            self.stdout.write("Initializing local Ray instance...")
            ray.init(
                ignore_reinit_error=True,
                # Enable dashboard with task visibility
                dashboard_host="127.0.0.1",
                dashboard_port=8265,
                include_dashboard=True,
                # Use the current Python executable for workers
                runtime_env={"env_vars": {"PYTHONPATH": os.pathsep.join(sys.path)}},
                # Enable task/actor events for dashboard
                _system_config={
                    "enable_timeline": True,
                    "task_events_report_interval_ms": 100,
                },
            )
            self.stdout.write(self.style.SUCCESS("Ray initialized"))
            self.stdout.write(self.style.SUCCESS("  Dashboard: http://127.0.0.1:8265"))

    def _init_cluster_ray(self, address: str) -> None:
        """Connect to a remote Ray cluster.

        Args:
            address: Ray cluster address (e.g., 'ray://localhost:10001')

        Raises:
            Exception: If connection fails (caller should handle retry)
        """
        import ray

        # Shutdown any existing connection first
        if ray.is_initialized():
            ray.shutdown()

        rendered_address = render_console_diagnostic(address)
        self.stdout.write(f"Connecting to Ray cluster at {rendered_address}...")
        ray.init(
            address=address,
            ignore_reinit_error=True,
        )
        self.stdout.write(self.style.SUCCESS("Connected to Ray cluster"))
        # Show cluster resources
        resources = ray.cluster_resources()
        self.stdout.write(f"  Cluster resources: {render_console_resource_summary(resources)}")

    def _create_lease(self, queue: str) -> None:
        """Acquire a new worker lease without adopting an existing row.

        The lease tracks active workers and enables detection of
        crashed workers through heartbeat expiration. A generated ID collision
        is proven by the conflicting primary-key row and retried with a fresh
        identity. Other database failures abort startup before Ray or task
        claims begin.

        Args:
            queue: The queue this worker is processing.
        """
        import os
        import socket

        from django.utils import timezone

        self.lease_queue_name = queue
        if self.lease_identity is not None:
            if not self._recreate_lease():
                raise CommandError("Worker lease ownership was lost")
            return

        hostname = socket.gethostname()
        pid = os.getpid()
        for attempt in range(_WORKER_ID_ALLOCATION_ATTEMPTS):
            started_at = timezone.now()
            identity = WorkerLeaseIdentity(
                worker_id=self.worker_id,
                hostname=hostname,
                pid=pid,
                started_at=started_at,
            )
            try:
                with transaction.atomic():
                    lease = TaskWorkerLease.objects.create(
                        **identity.database_filters(),
                        queue_name=queue,
                        capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
                        django_ray_version=django_ray_version,
                        min_supported_execution_protocol_version=(
                            MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION
                        ),
                        max_supported_execution_protocol_version=(
                            MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION
                        ),
                        legacy_admission_token=None,
                        last_heartbeat_at=started_at,
                        is_active=True,
                        stopped_at=None,
                    )
                    # Inactive leases can be deleted through the supported
                    # Admin action while their orphaned tasks still await
                    # reconciliation.  A successful row insert therefore is
                    # not sufficient proof that the ID is unused: retain the
                    # task-side ownership fence until every in-flight row has
                    # left a state where workers coordinate by worker ID.
                    if RayTaskExecution.objects.filter(
                        claimed_by_worker=self.worker_id,
                        state__in=(TaskState.RUNNING, TaskState.CANCELLING),
                    ).exists():
                        raise _WorkerIdReservedByTaskError
            except _WorkerIdReservedByTaskError:
                if attempt + 1 < _WORKER_ID_ALLOCATION_ATTEMPTS:
                    self._set_worker_id(generate_worker_id())
                continue
            except IntegrityError as error:
                if not is_worker_id_primary_key_collision(error):
                    diagnostic = render_console_diagnostic(error)
                    raise CommandError(f"Could not create worker lease: {diagnostic}") from None
                if attempt + 1 < _WORKER_ID_ALLOCATION_ATTEMPTS:
                    self._set_worker_id(generate_worker_id())
                continue
            except Exception as error:
                diagnostic = render_console_diagnostic(error)
                raise CommandError(f"Could not create worker lease: {diagnostic}") from None

            self.lease = lease
            self.lease_identity = identity
            return

        # The backend exception includes the conflicting primary-key value on
        # supported databases. Keep the public startup diagnostic bounded and
        # avoid retaining that foreign identity in ``--traceback`` output.
        raise CommandError(
            "Could not allocate a unique worker lease after bounded retries"
        ) from None

    def setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self.handle_shutdown_signal)
        signal.signal(signal.SIGINT, self.handle_shutdown_signal)

    def _write_worker_output(self, message: str, *, minimum_verbosity: int = 1) -> None:
        """Write informational worker output when Django verbosity permits it."""
        if self.verbosity >= minimum_verbosity:
            self.stdout.write(message)

    def handle_shutdown_signal(self, signum: int, frame: FrameType | None) -> None:
        """Handle shutdown signals."""
        del frame
        if self.shutdown_requested:
            return
        self.stdout.write(self.style.WARNING(f"\nReceived signal {signum}, shutting down..."))
        self.shutdown_requested = True
        self.shutdown_signal = signum
        self.shutdown_exit_code = 128 + signum

    def run_loop(
        self,
        queues: Sequence[str],
        concurrency: int,
        heartbeat_interval: float,
    ) -> None:
        """Run the main worker loop.

        Args:
            queues: Sequence of queue names to process (not modified).
            concurrency: Maximum concurrent tasks.
            heartbeat_interval: Seconds between heartbeats.
        """
        now = time.monotonic()
        next_heartbeat = now
        next_completion_poll = now
        next_claim = now
        next_reconciliation = now
        next_timeout_check = now
        next_cancellation = now
        next_lease_cleanup = now

        while not self.shutdown_requested:
            current_time = time.monotonic()
            activity = False
            claim_due = current_time >= next_claim

            if current_time >= next_heartbeat:
                self.send_heartbeat()
                next_heartbeat = current_time + heartbeat_interval

            # Lease loss is a hard ownership boundary. Do not poll or mutate
            # task state once the heartbeat path has failed that fence.
            if self.shutdown_requested:
                break

            if (
                current_time >= next_completion_poll
                and self.execution_mode in ("local", "cluster")
                and self.ray_core_runner
            ):
                activity = bool(self.poll_ray_core_tasks()) or activity
                next_completion_poll = current_time + self.completion_poll_interval

            # A signal may arrive while heartbeat/polling is in progress.  Do
            # not claim another task once shutdown has begun.
            if self.shutdown_requested:
                break

            if claim_due:
                activity = bool(self.claim_and_process_tasks(queues, concurrency)) or activity

            # The pre-claim lease lock can discover that this process no longer
            # owns its identity. Stop before cancellation or reconciliation can
            # mistake a replacement worker's matching ID for local ownership.
            if self.shutdown_requested:
                break

            if current_time >= next_cancellation:
                activity = bool(self.process_cancellations(queues)) or activity
                next_cancellation = current_time + self.cancellation_interval
            if self.shutdown_requested:
                break

            if current_time >= next_reconciliation:
                activity = bool(self.reconcile_tasks(queues)) or activity
                self.last_reconciliation = current_time
                next_reconciliation = current_time + self.reconciliation_interval
            if self.shutdown_requested:
                break

            if current_time >= next_timeout_check:
                activity = bool(self.detect_stuck_tasks(queues)) or activity
                next_timeout_check = current_time + self.timeout_check_interval
            if self.shutdown_requested:
                break

            if current_time >= next_lease_cleanup:
                activity = bool(self.cleanup_expired_leases()) or activity
                next_lease_cleanup = current_time + self.lease_cleanup_interval

            if claim_due:
                next_claim = current_time + self.polling_policy.next_delay(activity=activity)
            elif activity:
                next_claim = min(
                    next_claim,
                    current_time + self.polling_policy.next_delay(activity=True),
                )

            deadlines = [
                next_heartbeat,
                next_claim,
                next_reconciliation,
                next_timeout_check,
                next_cancellation,
                next_lease_cleanup,
            ]
            if (
                self.execution_mode in ("local", "cluster")
                and self.ray_core_runner
                and getattr(self.ray_core_runner, "pending_count", 0) > 0
            ):
                deadlines.append(next_completion_poll)

            sleep_seconds = max(0.0, min(deadlines) - time.monotonic())
            time.sleep(sleep_seconds)

    def send_heartbeat(self) -> None:
        """Send worker heartbeat, update lease, and check Ray connection."""
        identity = self.lease_identity
        if identity is None:
            self._request_shutdown_for_lease_loss("worker lease was never acquired")
            return

        try:
            with transaction.atomic():
                updated = self._lock_authoritative_leases(
                    source_worker_id=self.worker_id,
                    allow_takeover=False,
                )
                if updated:
                    TaskWorkerLease.objects.filter(**identity.database_filters()).update(
                        queue_name=self.lease_queue_name
                    )
        except Exception as error:
            self._request_shutdown_for_lease_loss(
                "worker lease heartbeat failed",
                error=error,
            )
            return

        if updated:
            if self.lease is not None:
                self.lease.queue_name = self.lease_queue_name
        else:
            return

        if self.shutdown_requested:
            return

        # Check Ray connection health for local/cluster modes
        if self.execution_mode in ("local", "cluster"):
            self._check_ray_connection()

        # Periodic status output (every ~60 seconds based on 15s heartbeat)
        if hasattr(self, "_heartbeat_count"):
            self._heartbeat_count += 1
        else:
            self._heartbeat_count = 1

        if self._heartbeat_count % 4 == 0:  # Every 4th heartbeat (~60 seconds)
            ray_core_pending = self.ray_core_runner.pending_count if self.ray_core_runner else 0
            active = len(self.active_tasks) + ray_core_pending
            idle_time = (
                time.time() - self.last_task_processed if self.last_task_processed > 0 else 0
            )
            self.stdout.write(
                f"\n[Status] tasks_processed={self.tasks_processed_count}, "
                f"active={active}, idle={idle_time:.0f}s"
            )
        else:
            self.stdout.write(".", ending="")
        self.stdout.flush()

    def _recreate_lease(self) -> bool:
        """Renew only a still-live exact lease identity."""
        identity = self.lease_identity
        if identity is None:
            self._request_shutdown_for_lease_loss("worker lease was never acquired")
            return False

        try:
            with transaction.atomic():
                updated = self._lock_authoritative_leases(
                    source_worker_id=self.worker_id,
                    allow_takeover=False,
                )
                if updated:
                    TaskWorkerLease.objects.filter(**identity.database_filters()).update(
                        queue_name=self.lease_queue_name
                    )
            if not updated:
                return False
            if self.lease is not None:
                self.lease.queue_name = self.lease_queue_name
            self.stdout.write(self.style.SUCCESS(f"  Lease restored: {self.worker_id}"))
            return True
        except Exception as error:
            self._request_shutdown_for_lease_loss(
                "worker lease restoration failed",
                error=error,
            )
            return False

    def _request_shutdown_for_lease_loss(
        self,
        reason: str,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Fail closed when this process cannot prove lease ownership."""
        if not self.shutdown_requested:
            self.stdout.write(
                self.style.ERROR("\nWorker lease ownership lost; shutting down before claims")
            )
        if error is None:
            self.logger.error(reason)
        else:
            self.logger.error(
                reason,
                exc_info=(type(error), error, error.__traceback__),
            )
        self.lease_ownership_lost = True
        # These capabilities belong to the expired immutable lease identity.
        # Retire them immediately so no later reconciliation or shutdown path
        # can mistake a replacement process with the same worker ID for us.
        self.active_tasks.clear()
        self.active_task_identities.clear()
        self.shutdown_requested = True
        if self.shutdown_exit_code is None:
            self.shutdown_exit_code = 1

    def _update_lease_heartbeat(self) -> bool:
        """Update lease heartbeat without full heartbeat logic.

        This is called before each task execution to ensure the lease
        doesn't expire during long-running tasks.
        """
        identity = self.lease_identity
        if identity is None:
            self._request_shutdown_for_lease_loss("worker lease was never acquired")
            return False

        try:
            with transaction.atomic():
                updated = self._lock_authoritative_leases(
                    source_worker_id=self.worker_id,
                    allow_takeover=False,
                )
        except Exception as error:
            self._request_shutdown_for_lease_loss(
                "worker lease heartbeat failed",
                error=error,
            )
            return False
        if not updated:
            return False
        return True

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
        """Record that a running task is still being actively monitored."""
        heartbeat_time = now or datetime.now(UTC)
        filters: dict[str, Any] = {"pk": task.pk, "state": TaskState.RUNNING}
        if ray_job_id is not None:
            filters["ray_job_id"] = ray_job_id
        if expected_worker_id is not None:
            filters["claimed_by_worker"] = expected_worker_id
        if attempt_number is not None:
            filters["attempt_number"] = attempt_number
        if execution_generation is not None:
            filters["execution_generation"] = execution_generation
        updated = RayTaskExecution.objects.filter(**filters).update(
            last_heartbeat_at=heartbeat_time
        )
        if updated:
            task.last_heartbeat_at = heartbeat_time

    def _partition_ray_core_handles(
        self,
        runner: RayCoreRunner,
        handles: tuple[RayCoreHandle, ...],
        *,
        heartbeat_at: datetime | None = None,
    ) -> tuple[RayCoreHandle, ...] | None:
        """Authorize exact local handles and retire every ineligible capability.

        The live worker lease is locked before any execution heartbeat update.
        No Ray call occurs in this transaction. Unsupported, stale, missing,
        terminal, or transferred handles are forgotten locally without changing
        their durable rows.
        """
        if not handles:
            return ()

        eligible_handles: list[RayCoreHandle] = []
        with transaction.atomic():
            lease = self._lock_authoritative_leases(
                source_worker_id=self.worker_id,
                allow_takeover=False,
                renew_heartbeat=False,
            )
            if lease is None:
                return None
            supported_protocols = self._explicit_protocol_range(lease)
            if supported_protocols is None:
                return None

            rows_by_pk = {
                int(row.pk): row
                for row in RayTaskExecution.objects.filter(
                    pk__in=[handle.task_pk for handle in handles]
                ).only(
                    "pk",
                    "state",
                    "claimed_by_worker",
                    "attempt_number",
                    "execution_generation",
                    "execution_protocol_version",
                )
            }
            heartbeat_groups: dict[tuple[int, int, int], list[int]] = {}
            for handle in handles:
                row = rows_by_pk.get(handle.task_pk)
                if (
                    row is None
                    or str(row.claimed_by_worker or "") != self.worker_id
                    or row.state not in (TaskState.RUNNING, TaskState.CANCELLING)
                    or int(row.attempt_number) != handle.attempt_number
                    or int(row.execution_generation) != handle.execution_generation
                    or not supported_protocols.supports(int(row.execution_protocol_version))
                ):
                    continue
                eligible_handles.append(handle)
                if heartbeat_at is not None and row.state == TaskState.RUNNING:
                    identity = (
                        handle.attempt_number,
                        handle.execution_generation,
                        int(row.execution_protocol_version),
                    )
                    heartbeat_groups.setdefault(identity, []).append(handle.task_pk)

            for (
                attempt_number,
                execution_generation,
                execution_protocol_version,
            ), task_ids in heartbeat_groups.items():
                RayTaskExecution.objects.filter(
                    pk__in=task_ids,
                    state=TaskState.RUNNING,
                    claimed_by_worker=self.worker_id,
                    attempt_number=attempt_number,
                    execution_generation=execution_generation,
                    execution_protocol_version=execution_protocol_version,
                ).update(last_heartbeat_at=heartbeat_at)

        eligible_handle_ids = {id(handle) for handle in eligible_handles}
        retired = 0
        for handle in handles:
            if id(handle) not in eligible_handle_ids:
                retired += int(runner.retire_pending_handle(handle))
        if retired:
            self.stdout.write(
                self.style.NOTICE(f"\n  Retired {retired} stale or unsupported Ray Core handle(s)")
            )
        return tuple(eligible_handles)

    def _completion_envelope_grace_expired(
        self,
        task: RayTaskExecution,
        *,
        now: datetime,
    ) -> bool:
        """Return whether a terminal Ray Job has waited too long for its envelope."""
        last_activity = task.last_heartbeat_at or task.started_at or task.created_at
        last_activity_dt: datetime = last_activity  # type: ignore[assignment]
        return now - last_activity_dt > get_stuck_timeout()

    @staticmethod
    def _execution_completion_identity(task: RayTaskExecution) -> ExecutionIdentity:
        """Return the exact durable identity a completion must echo."""
        assert task.pk is not None
        return ExecutionIdentity(
            task_execution_pk=int(task.pk),
            task_id=str(task.task_id),
            attempt_number=int(task.attempt_number),
            execution_generation=int(task.execution_generation),
        )

    @classmethod
    def _inspect_execution_completion(
        cls,
        serialized: object,
        task: RayTaskExecution,
        *,
        supported_protocols: ExecutionProtocolRange,
    ) -> _InspectedExecutionCompletion:
        """Decode one exact completion before any result or reference side effect."""
        try:
            decoded = decode_execution_completion(
                serialized,
                expected_identity=cls._execution_completion_identity(task),
                expected_execution_protocol_version=int(task.execution_protocol_version),
                supported_protocols=supported_protocols,
            )
        except ExecutionCompletionDecodeError as error:
            return _InspectedExecutionCompletion(
                decoded=None,
                prepared_result_reference=None,
                rejection=error.classification,
                requires_nonretryable_disposition=(error.requires_nonretryable_disposition),
            )

        prepared_result_reference: str | None = None
        result_reference = decoded.completion.result_reference
        if decoded.completion.success and result_reference is not None:
            from django_ray.result_storage import (
                ResultStorageError,
                canonicalize_result_reference,
            )

            try:
                prepared_result_reference = canonicalize_result_reference(result_reference)
            except ResultStorageError:
                versioned = decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
                return _InspectedExecutionCompletion(
                    decoded=None,
                    prepared_result_reference=None,
                    rejection=(
                        ExecutionCompletionRejection.INVALID_VERSIONED
                        if versioned
                        else ExecutionCompletionRejection.MALFORMED_LEGACY
                    ),
                    requires_nonretryable_disposition=versioned,
                )

        return _InspectedExecutionCompletion(
            decoded=decoded,
            prepared_result_reference=prepared_result_reference,
            rejection=None,
            requires_nonretryable_disposition=False,
        )

    @staticmethod
    def _completion_rejection_policy(
        inspection: _InspectedExecutionCompletion,
    ) -> tuple[str, str, bool | None]:
        """Map one fixed codec rejection to bounded lifecycle policy."""
        assert inspection.rejection is not None
        code = inspection.rejection.value
        if inspection.requires_nonretryable_disposition:
            return (
                f"Execution completion rejected ({code})",
                "RayCompletionIncompatible",
                False,
            )
        return (
            f"Legacy execution completion rejected ({code})",
            "RayCompletionMalformed",
            None,
        )

    def _get_ray_cluster_resources_with_timeout(
        self, timeout_seconds: float
    ) -> dict[str, Any] | None:
        """Return Ray cluster resources or None when the check times out."""
        import queue
        import threading

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

    def _check_ray_connection(self) -> None:
        """Check if Ray connection is healthy and reconnect if needed."""
        import ray

        try:
            # Quick health check - try to get cluster resources with timeout
            if ray.is_initialized():
                resources = self._get_ray_cluster_resources_with_timeout(timeout_seconds=10)
                if resources is not None:
                    return  # Connection is healthy
                self.stdout.write(self.style.WARNING("\nRay health check timed out"))
        except Exception as e:
            diagnostic = render_console_diagnostic(e)
            self.stdout.write(self.style.WARNING(f"\nRay connection lost: {diagnostic}"))

        # Connection is broken or Ray is not initialized - try to reconnect
        self._reconnect_ray()

    def _reconnect_ray(self) -> None:
        """Attempt to reconnect to Ray cluster."""
        import ray

        self.stdout.write(self.style.WARNING("\nAttempting to reconnect to Ray..."))

        # First, shutdown existing connection if any
        try:
            if ray.is_initialized():
                ray.shutdown()
                self.stdout.write("  Shut down existing Ray connection")
        except Exception as e:
            self.stdout.write(f"  Error during shutdown: {render_console_diagnostic(e)}")

        # Wait a moment before reconnecting
        time.sleep(2)

        # Reconnect based on execution mode
        max_retries = 5
        retry_delay = 5  # seconds

        for attempt in range(1, max_retries + 1):
            try:
                if self.execution_mode == "local":
                    self._init_local_ray()
                elif self.execution_mode == "cluster" and self.cluster_address:
                    self._init_cluster_ray(self.cluster_address)

                # Verify connection
                if ray.is_initialized():
                    resources = ray.cluster_resources()
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"\n  Reconnected to Ray (attempt {attempt}/{max_retries})"
                        )
                    )
                    self.stdout.write(
                        f"  Cluster resources: {render_console_resource_summary(resources)}"
                    )

                    # Clear any stale Ray task references - they're invalid now
                    if self.ray_core_runner and self.ray_core_runner.pending_count > 0:
                        stale_count = self.ray_core_runner.pending_count
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Clearing {stale_count} stale Ray task references"
                            )
                        )
                        # Mark these tasks as LOST so they can be retried
                        self._mark_stale_ray_core_tasks_as_lost()

                    # Reinitialize the runner with the new connection
                    self.ray_core_runner = RayCoreRunner()

                    return  # Success!

            except Exception as e:
                diagnostic = render_console_diagnostic(e)
                self.stdout.write(
                    self.style.WARNING(
                        f"  Reconnection attempt {attempt}/{max_retries} failed: {diagnostic}"
                    )
                )
                if attempt < max_retries:
                    self.stdout.write(f"  Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)  # Exponential backoff, max 60s

        self.stdout.write(
            self.style.ERROR(
                f"\n  Failed to reconnect after {max_retries} attempts. "
                "Worker will continue but Ray tasks will fail."
            )
        )

    def _mark_stale_ray_core_tasks_as_lost(self) -> None:
        """Route stale Ray Core references through the normal retry/failure path."""
        if not self.ray_core_runner or self.ray_core_runner.pending_count == 0:
            return

        runner = self.ray_core_runner
        pending_handles = runner.pending_task_handles
        eligible_handles = self._partition_ray_core_handles(runner, pending_handles)
        if eligible_handles is None:
            return
        self._terminalize_lost_ray_core_handles(
            runner,
            eligible_handles,
            error_message="Ray connection lost - task state unknown",
        )

    def _terminalize_lost_ray_core_handles(
        self,
        runner: RayCoreRunner,
        handles: tuple[RayCoreHandle, ...],
        *,
        error_message: str,
    ) -> int:
        """Terminalize authorized lost handles under the central lease fence."""
        count = 0
        current_handles = {handle.task_pk: handle for handle in runner.pending_task_handles}
        for handle in handles:
            if current_handles.get(handle.task_pk) is not handle:
                continue
            try:
                task = RayTaskExecution.objects.get(pk=handle.task_pk)
            except RayTaskExecution.DoesNotExist:
                runner.retire_pending_handle(handle)
                continue

            if (
                task.state != TaskState.RUNNING
                or int(task.attempt_number) != handle.attempt_number
                or int(task.execution_generation) != handle.execution_generation
            ):
                runner.retire_pending_handle(handle)
                continue

            with self._authoritative_task_owner(
                task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is not None:
                    current = owned.execution
                    handled = self._handle_task_failure(
                        current,
                        error_message=error_message,
                        exception_type="RayConnectionError",
                        expected_claimed_by_worker=self.worker_id,
                        expected_attempt_number=handle.attempt_number,
                        expected_execution_generation=handle.execution_generation,
                        expected_completion_data=current.completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                    )
                    if handled:
                        count += 1

            runner.retire_pending_handle(handle)

        if count:
            self.stdout.write(
                self.style.WARNING(
                    f"  Routed {count} stale Ray Core task(s) through lifecycle handling"
                )
            )
        return count

    def _finalize_ray_core_cancellation(
        self,
        *,
        task_pk: int,
        attempt_number: int,
        execution_generation: int,
    ) -> bool:
        """Finalize one exact cancellation after a Ray Core completion."""
        task = (
            RayTaskExecution.objects.filter(
                pk=task_pk,
                state=TaskState.CANCELLING,
                claimed_by_worker=self.worker_id,
                attempt_number=attempt_number,
                execution_generation=execution_generation,
            )
            .only(
                "pk",
                "state",
                "claimed_by_worker",
                "ray_job_id",
                "ray_address",
                "attempt_number",
                "execution_generation",
                "execution_protocol_version",
                "started_at",
                "last_heartbeat_at",
                "completion_data",
            )
            .first()
        )
        if task is None:
            return False

        with self._authoritative_task_owner(
            task,
            expected_state=TaskState.CANCELLING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is None:
                return False
            current = owned.execution
            return cancel_task(
                current,
                expected_worker_id=self.worker_id,
                expected_attempt_number=attempt_number,
                expected_execution_generation=execution_generation,
                expected_completion_data=current.completion_data,
                require_completion_data_match=True,
                supported_protocols=owned.supported_protocols,
            )

    def claim_and_process_tasks(self, queues: Sequence[str], concurrency: int) -> int:
        """Claim and submit tasks for execution.

        Args:
            queues: Sequence of queue names to process (not modified).
            concurrency: Maximum concurrent tasks.
        """
        if self.shutdown_requested:
            return 0
        lease_identity = self.lease_identity
        if lease_identity is None:
            self._request_shutdown_for_lease_loss("worker lease was never acquired")
            return 0

        # Keep this guard at the durable claim boundary as well as CLI parsing.
        # Direct command-method users and future queue-selection paths must not be
        # able to hand a Ray Job-only workload to a process-wide Ray Core runner.
        queues = self._enforce_queue_affinity(
            queues,
            worker_family=self._active_worker_family(),
            allow_filter=False,
        )

        # Queue expiry remains independent from execution capacity, but both
        # expiry and claiming require the same exact active-lease fence below.
        ray_core_pending = self.ray_core_runner.pending_count if self.ray_core_runner else 0
        active_count = len(self.active_tasks) + ray_core_pending
        available_slots = concurrency - active_count

        # Claim tasks from any of the specified queues
        from django.db.models import Q

        with transaction.atomic():
            lease = self._lock_authoritative_leases(
                source_worker_id=self.worker_id,
                allow_takeover=False,
                renew_heartbeat=False,
            )
            if lease is None:
                return 0
            supported_protocols = self._explicit_protocol_range(lease)
            if supported_protocols is None:
                return 0

            sweep_now = datetime.now(UTC)
            expired = expire_queued_tasks(
                queues,
                now=sweep_now,
                limit=100,
                supported_protocols=supported_protocols,
            )
            if expired:
                self.stdout.write(
                    self.style.WARNING(f"  Expired {len(expired)} stale queued task(s)")
                )
            if available_slots <= 0:
                return len(expired)

            # Re-read the clock after attempt archival for the bounded expiry
            # batch. A deadline reached during that work must be excluded from
            # this claim even though the next sweep owns its transition.
            claim_now = datetime.now(UTC)

            # A single query keeps immediate and delayed/retried work in the same
            # priority order. Queue names only select workload-isolation boundaries.
            tasks = list(
                RayTaskExecution.objects.select_for_update(skip_locked=True)
                .filter(
                    state=TaskState.QUEUED,
                    queue_name__in=queues,
                    execution_protocol_version__gte=supported_protocols.minimum,
                    execution_protocol_version__lte=supported_protocols.maximum,
                )
                .filter(Q(run_after__isnull=True) | Q(run_after__lte=claim_now))
                .filter(Q(queue_deadline_at__isnull=True) | Q(queue_deadline_at__gt=claim_now))
                .order_by("-priority", "created_at", "pk")[:available_slots]
            )

            for task in tasks:
                task.state = TaskState.RUNNING
                task.started_at = claim_now
                task.last_heartbeat_at = claim_now
                task.claimed_by_worker = self.worker_id
                task.managed_with_django_ray_version = django_ray_version
                task.execution_generation = int(task.execution_generation) + 1
                task.completion_data = None
                task.progress_data = None
                task.workflow_progress_summary_json = None
                task.workflow_run_id = None
                task.workflow_plan_selection = None
                promote_legacy_ray_target(task)
                task.ray_job_id = None
                task.ray_job_request_reference = None
                task.ray_address = None
                task.save(
                    update_fields=[
                        "state",
                        "started_at",
                        "last_heartbeat_at",
                        "claimed_by_worker",
                        "managed_with_django_ray_version",
                        "execution_generation",
                        "completion_data",
                        "progress_data",
                        "workflow_progress_summary_json",
                        "workflow_run_id",
                        "workflow_plan_selection",
                        "ray_job_id",
                        "ray_job_request_reference",
                        "ray_target_address",
                        "ray_address",
                    ]
                )

        # Process each claimed task
        for task in tasks:
            if self.shutdown_requested:
                if not self.lease_ownership_lost:
                    self._handoff_unsubmitted_task(task)
                continue
            self.process_task(task)

        return len(expired) + len(tasks)

    def _handoff_unsubmitted_task(self, task: RayTaskExecution) -> None:
        """Return a just-claimed task to durable reconciliation on shutdown."""
        handed_off = False
        with self._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is not None:
                current = owned.execution
                current.state = TaskState.QUEUED
                current.started_at = None
                current.claimed_by_worker = None
                current.last_heartbeat_at = None
                current.ray_job_id = None
                current.ray_address = None
                current.save(
                    update_fields=[
                        "state",
                        "started_at",
                        "claimed_by_worker",
                        "last_heartbeat_at",
                        "ray_job_id",
                        "ray_address",
                    ]
                )
                handed_off = True
        if handed_off:
            self.stdout.write(
                self.style.NOTICE(f"  Task {task.pk} handed off before remote submission")
            )

    def process_task(self, task: RayTaskExecution) -> None:
        """Process a single task."""
        rendered_callable_path = render_console_diagnostic(task.callable_path)
        self.stdout.write(
            self.style.NOTICE(f"\nProcessing task {task.pk}: {rendered_callable_path}")
        )

        # Update heartbeat before task execution to prevent lease expiration
        # during long-running tasks
        if not self._update_lease_heartbeat():
            return

        # Track task processing
        self.last_task_processed = time.time()
        self.tasks_processed_count += 1

        try:
            runtime_env_for_execution(task)
        except RuntimeEnvSnapshotError as error:
            self._handle_task_failure(
                task,
                error_message=materialize_exception_message(error),
                exception_type=safe_exception_type_name(error),
                retryable=False,
                expected_claimed_by_worker=self.worker_id,
                expected_attempt_number=int(task.attempt_number),
                expected_execution_generation=int(task.execution_generation),
            )
            return

        if self.execution_mode == "sync":
            # Execute without Ray - purely synchronous
            self.execute_task_sync(task)
        elif self.execution_mode in ("local", "cluster"):
            # Submit to Ray cluster via @ray.remote (RayCoreRunner)
            # Tasks run on Ray workers, enabling distributed computing
            self.submit_task_to_ray_core(task)
        else:
            # Submit via Ray Job Submission API (process isolation)
            self.submit_task_to_ray(task)

    def execute_task_sync(self, task: RayTaskExecution) -> None:
        """Execute a task synchronously (without Ray)."""
        from django_ray.conf.settings import get_settings
        from django_ray.runtime.entrypoint import execute_task
        from django_ray.workflow_plans import runtime_env_plan_identity

        expected_attempt_number = int(task.attempt_number)
        expected_execution_generation = int(task.execution_generation)
        expected_worker_id = self.worker_id
        durable_task_id = getattr(task, "task_id", None)
        try:
            runtime_env = runtime_env_for_execution(task)
            plan_runtime_env_identity = runtime_env_plan_identity(
                runtime_env,
                trust_identity=get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {}),
            )
            result_json = execute_task(
                callable_path=task.callable_path,
                serialized_args=task.args_json,
                serialized_kwargs=task.kwargs_json,
                task_execution_pk=task.pk,
                task_id=str(durable_task_id) if durable_task_id is not None else None,
                attempt_number=expected_attempt_number,
                execution_generation=expected_execution_generation,
                runtime_env_profile=runtime_env.profile,
                runtime_env_hash=runtime_env.digest,
                runtime_env_plan_identity=plan_runtime_env_identity.as_transport_dict(),
                input_reference=getattr(task, "input_reference", None),
                ray_job_driver=False,
            )
            succeeded = False
            with self._authoritative_task_owner(
                task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is None:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"  Ignoring stale synchronous result for task {task.pk} "
                            f"attempt {expected_attempt_number}, "
                            f"generation {expected_execution_generation}"
                        )
                    )
                    return
                current = owned.execution
                inspection = self._inspect_execution_completion(
                    result_json,
                    current,
                    supported_protocols=owned.supported_protocols,
                )
                decoded = inspection.decoded
                if decoded is None:
                    error_message, exception_type, retryable = self._completion_rejection_policy(
                        inspection
                    )
                    self._handle_task_failure(
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
                    succeeded = self._store_and_succeed_task(
                        current,
                        completion.result,
                        prepared_result_reference=inspection.prepared_result_reference,
                        expected_claimed_by_worker=expected_worker_id,
                        expected_attempt_number=expected_attempt_number,
                        expected_execution_generation=expected_execution_generation,
                        expected_completion_data=current.completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                        executor_django_ray_version=(completion.executor_django_ray_version),
                    )
                else:
                    self._handle_task_failure(
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
                        executor_django_ray_version=(completion.executor_django_ray_version),
                    )

            if succeeded:
                self.stdout.write(self.style.SUCCESS(f"  Task {task.pk} succeeded"))

        except Exception as e:
            self._handle_task_failure(
                task,
                error_message=materialize_exception_message(e),
                exception_type=safe_exception_type_name(e),
                retryable=False if isinstance(e, RuntimeEnvSnapshotError) else None,
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
            )

    def _store_task_result(
        self,
        task: RayTaskExecution,
        result_value: Any,
    ) -> tuple[str, ...]:
        """Store task result inline or as a reference if result is too large.

        Args:
            task: The task to update with result fields.
            result_value: The Python value to serialize and store.

        Returns:
            Warning messages to emit only after the durable success commits.
        """
        from django_ray.result_storage import (
            DigestResultStorage,
            ResultStorageError,
            get_result_storage_backend,
        )

        settings = get_settings()
        max_result_size = int(settings.get("MAX_RESULT_SIZE_BYTES", 1024 * 1024))

        serialized_result = json.dumps(result_value)
        result_size_bytes = len(serialized_result.encode("utf-8"))

        if result_size_bytes <= max_result_size:
            task.result_data = serialized_result
            task.result_reference = None
            return ()

        task.result_data = None
        diagnostics: list[str] = []

        try:
            backend = get_result_storage_backend(settings)
            task.result_reference = backend.store(serialized_result=serialized_result)
        except ResultStorageError as e:
            # Preserve success semantics if external storage is unavailable.
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

    def _store_and_succeed_task(
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
        executor_django_ray_version: str | None = None,
    ) -> bool:
        """Store and publish one successful result under the execution row lock.

        External result references are content-addressed and may be shared, so
        deleting a reference after losing the terminal transition is unsafe.
        Holding the row lock across storage and the success transition prevents
        cancellation, retry, or replacement from winning in that window.
        """
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

        diagnostics: tuple[str, ...] = ()
        with transaction.atomic():
            current = RayTaskExecution.objects.select_for_update().filter(**filters).first()
            if current is None:
                return False
            if not supported_protocols.supports(int(current.execution_protocol_version)):
                return False

            if prepared_result_reference is None:
                diagnostics = self._store_task_result(current, result_value) or ()
            else:
                current.result_data = None
                current.result_reference = prepared_result_reference

            persisted = succeed_task(
                current,
                result_data=current.result_data,
                result_reference=current.result_reference,
                expected_ray_job_id=expected_ray_job_id,
                expected_claimed_by_worker=expected_claimed_by_worker,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=expected_completion_data,
                require_completion_data_match=require_completion_data_match,
                supported_protocols=supported_protocols,
                _executor_django_ray_version=executor_django_ray_version,
            )
            if persisted:
                task.__dict__.update(current.__dict__)

        for message in diagnostics:
            self.stdout.write(self.style.WARNING(render_console_diagnostic(message)))
        return persisted

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
        """Handle a failed task, potentially scheduling a retry.

        Args:
            task: The failed task.
            error_message: The error message.
            error_traceback: The full traceback (optional).
            exception_type: The exception class name (optional).
            retryable: Explicit executor decision for permanent input failures.
        """
        # Check if we should retry
        retry_decision = (
            RetryDecision(should_retry=False, reason="Executor marked failure non-retryable")
            if retryable is False
            else should_retry(task, exception_type)
        )

        try:
            handled = record_failure(
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
            handled = record_failure(
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
            self.stdout.write(
                self.style.WARNING(
                    f"  Task {task.pk} failed, scheduling retry "
                    f"at {retry_decision.next_attempt_at}: {rendered_error}"
                )
            )
        else:
            reason = retry_decision.reason or "No retry configured"
            rendered_reason = render_console_diagnostic(reason)
            self.stdout.write(
                self.style.ERROR(
                    f"  Task {task.pk} failed permanently ({rendered_reason}): {rendered_error}"
                )
            )
        return True

    @staticmethod
    def _persist_submission_tracking(
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
        expected_ray_job_id: str | None,
    ) -> bool:
        """Attach a backend handle only to the execution that submitted it."""
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
        heartbeat_at = datetime.now(UTC)
        updated = RayTaskExecution.objects.filter(**filters).update(
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

    @staticmethod
    def _release_submission_tracking(
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
    ) -> bool:
        """Release an exact reservation after a definite pre-request failure."""
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
        heartbeat_at = datetime.now(UTC)
        updated = RayTaskExecution.objects.filter(**filters).update(
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

    def _cancel_untracked_submission(
        self,
        runner: Any,
        handle: SubmissionHandle,
        *,
        backend_name: str,
        prepared: PreparedRemoteCancellation | None = None,
    ) -> CancellationOutcome:
        """Best-effort stop for a submission whose durable execution changed."""
        try:
            cancellation = request_remote_cancellation(runner, handle, prepared=prepared)
        except Exception as exc:
            cancellation = CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Cancellation request raised {materialize_exception_text(exc)}",
            )
        message = (
            f": {render_console_diagnostic(cancellation.message)}" if cancellation.message else ""
        )
        rendered_job_id = render_console_diagnostic(handle.ray_job_id)
        self.stdout.write(
            self.style.WARNING(
                f"  Discarded stale {backend_name} submission {rendered_job_id}; "
                f"cancellation {cancellation.status.value}{message}"
            )
        )
        return cancellation

    def _cancel_mismatched_submissions(
        self,
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
            self._cancel_untracked_submission(
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

    @staticmethod
    def _inspect_submission_tracking(
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
    ) -> tuple[str, str | None, str | None]:
        """Classify the durable identity after a submitter loses its CAS."""
        try:
            current = (
                RayTaskExecution.objects.filter(pk=task.pk)
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
    ) -> bool:
        """Close one ambiguous completion channel while its task lock is held."""
        completion_data = current.completion_data
        inspection = (
            self._inspect_execution_completion(
                completion_data,
                current,
                supported_protocols=supported_protocols,
            )
            if completion_data is not None
            else None
        )
        decoded = inspection.decoded if inspection is not None else None

        if decoded is not None and decoded.completion.success:
            completion = decoded.completion
            handled = self._store_and_succeed_task(
                current,
                completion.result,
                prepared_result_reference=inspection.prepared_result_reference,
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
                self.stdout.write(
                    self.style.WARNING(
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

        return self._handle_task_failure(
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
        """Resolve a failed post-request CAS without killing an adopted job."""
        disposition, _, inspection_detail = self._inspect_submission_tracking(
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        if disposition == "replaced":
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)
            self._cancel_untracked_submission(
                runner,
                handle,
                backend_name="replaced Ray Job",
            )
        elif disposition in {"transferred", "terminal"}:
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)

        retained = disposition in {"owned", "unknown"}
        action = "retaining exact tracking" if retained else "retiring local tracking"
        explanation = (
            f"; {render_console_diagnostic(inspection_detail)}" if inspection_detail else ""
        )
        rendered_detail = render_console_diagnostic(detail)
        self.stdout.write(
            self.style.WARNING(
                f"  Task {task.pk} could not confirm Ray Job tracking ({rendered_detail}); "
                f"{action}{explanation}"
            )
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
        """Fence a returned capability mismatch without cancelling an adopter."""
        handles = {
            (reserved_handle.ray_job_id, reserved_handle.ray_address): reserved_handle,
            (observed_handle.ray_job_id, observed_handle.ray_address): observed_handle,
        }
        prepared_cancellations = {
            identity: prepare_remote_cancellation(runner, handle)
            for identity, handle in handles.items()
        }

        # Refresh before locking so a completion published during submission is
        # captured and terminalized rather than invalidating the stale caller
        # snapshot. Exact live lease validation plus the execution lock makes
        # quiescence and closing the completion channel one decision.
        try:
            authoritative_snapshot = RayTaskExecution.objects.get(pk=task.pk)
        except RayTaskExecution.DoesNotExist:
            authoritative_snapshot = None
        if expected_worker_id == self.worker_id and authoritative_snapshot is not None:
            with self._authoritative_task_owner(
                authoritative_snapshot,
                expected_state=str(authoritative_snapshot.state),
                allow_takeover=False,
                require_completion_data_match=False,
            ) as owned:
                current = owned.execution if owned is not None else None
                if current is None:
                    pass
                elif (
                    str(current.ray_job_id or "") != reserved_handle.ray_job_id
                    or str(current.ray_address or "") != reserved_handle.ray_address
                    or current.attempt_number != expected_attempt_number
                    or current.execution_generation != expected_execution_generation
                ):
                    self._cancel_mismatched_submissions(
                        runner,
                        reserved_handle,
                        observed_handle,
                        prepared=prepared_cancellations,
                    )
                    task_pk = int(task.pk)
                    transaction.on_commit(
                        lambda: self._retire_active_ray_job_tracking(
                            task_pk,
                            ray_job_id=reserved_handle.ray_job_id,
                            identity=(
                                expected_attempt_number,
                                expected_execution_generation,
                            ),
                        )
                    )
                    return
                else:
                    cancellation = self._cancel_mismatched_submissions(
                        runner,
                        reserved_handle,
                        observed_handle,
                        prepared=prepared_cancellations,
                    )
                    handled = current.state != TaskState.RUNNING
                    if not handled:
                        handled = self._terminalize_mismatched_ray_job_submission(
                            current,
                            reserved_handle=reserved_handle,
                            expected_worker_id=expected_worker_id,
                            expected_attempt_number=expected_attempt_number,
                            expected_execution_generation=expected_execution_generation,
                            error_message=error_message,
                            exception_type=exception_type,
                            cancellation=cancellation,
                            supported_protocols=owned.supported_protocols,
                        )
                    if handled:
                        task_pk = int(task.pk)
                        transaction.on_commit(
                            lambda: self._retire_active_ray_job_tracking(
                                task_pk,
                                ray_job_id=reserved_handle.ray_job_id,
                                identity=(
                                    expected_attempt_number,
                                    expected_execution_generation,
                                ),
                            )
                        )
                        return

        # An invalid exact lease is a hard task-mutation boundary. The observed
        # handle is nevertheless absent from durable state, so no live owner
        # can discover it. Quiesce only that orphan capability; the reserved
        # durable handle remains exclusively for the replacement owner.
        if self.shutdown_requested:
            self._cancel_untracked_submission(
                runner,
                observed_handle,
                backend_name="orphaned mismatched Ray Job",
                prepared=prepared_cancellations[
                    (observed_handle.ray_job_id, observed_handle.ray_address)
                ],
            )
            return

        disposition, _, inspection_detail = self._inspect_submission_tracking(
            task,
            reserved_handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        if disposition in {"replaced", "terminal"}:
            self._cancel_mismatched_submissions(
                runner,
                reserved_handle,
                observed_handle,
                prepared=prepared_cancellations,
            )
        else:
            self._cancel_untracked_submission(
                runner,
                observed_handle,
                backend_name="mismatched Ray Job",
                prepared=prepared_cancellations[
                    (observed_handle.ray_job_id, observed_handle.ray_address)
                ],
            )

        if disposition in {"replaced", "transferred", "terminal"}:
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)

        retained = disposition in {"owned", "unknown"}
        action = "retaining exact tracking" if retained else "retiring local tracking"
        explanation = (
            f"; {render_console_diagnostic(inspection_detail)}" if inspection_detail else ""
        )
        self.stdout.write(
            self.style.WARNING(f"  Task {task.pk} Ray Job identity mismatch; {action}{explanation}")
        )

    def submit_task_to_ray_core(self, task: RayTaskExecution) -> None:
        """Submit a task to Ray via @ray.remote (RayCoreRunner).

        This submits tasks to Ray workers using Ray Core remote functions,
        providing lower latency than Ray Job API while still executing
        on the Ray cluster.

        Args:
            task: The task execution to submit.
        """
        import ray

        expected_worker_id = self.worker_id
        expected_attempt_number = int(task.attempt_number)
        expected_execution_generation = int(task.execution_generation)
        expected_ray_job_id = task.ray_job_id

        # Ensure Ray is connected and runner is available
        if not ray.is_initialized():
            self.stdout.write(self.style.WARNING("  Ray not initialized, attempting to connect..."))
            self._reconnect_ray()

            if not ray.is_initialized():
                self._handle_task_failure(
                    task,
                    error_message="Ray cluster not available",
                    exception_type="RayConnectionError",
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                )
                return

        # Ensure runner is initialized
        if self.ray_core_runner is None:
            self.ray_core_runner = RayCoreRunner()
        runner = self.ray_core_runner

        try:
            handle = runner.submit_durable(task_execution=task)
        except Exception as e:
            import traceback

            from django_ray.execution_codec import ExecutionRequestEncodeError
            from django_ray.workflow_plans import WorkflowPlanMismatchError

            self._handle_task_failure(
                task,
                error_message=(f"Failed to submit to Ray Core: {materialize_exception_message(e)}"),
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
            attached = self._persist_submission_tracking(
                task,
                handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_ray_job_id=expected_ray_job_id,
            )
        except Exception as exc:
            import traceback

            cancellation = self._cancel_untracked_submission(
                runner,
                handle,
                backend_name="Ray Core",
            )
            self._handle_task_failure(
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
            self._cancel_untracked_submission(
                runner,
                handle,
                backend_name="Ray Core",
            )
            return

        self.stdout.write(self.style.SUCCESS(f"  Task {task.pk} submitted to Ray Core (async)"))

    def poll_ray_core_tasks(self) -> int:
        """Poll for completed Ray Core tasks and update their status.

        Uses RayCoreRunner.poll_completed() for efficient batch polling.
        """
        if self.ray_core_runner is None or self.ray_core_runner.pending_count == 0:
            return 0

        runner = self.ray_core_runner
        monitored_handles = runner.pending_task_handles
        monitor_time = time.monotonic()
        heartbeat_due = (
            monitor_time - self.last_task_monitor_heartbeat >= self.task_monitor_heartbeat_interval
        )
        eligible_handles = self._partition_ray_core_handles(
            runner,
            monitored_handles,
            heartbeat_at=datetime.now(UTC) if heartbeat_due else None,
        )
        if eligible_handles is None:
            return 0
        retired_count = len(monitored_handles) - len(eligible_handles)
        if heartbeat_due:
            self.last_task_monitor_heartbeat = monitor_time

        if not eligible_handles:
            return retired_count

        import ray

        # Check connection only after unsupported local capabilities are retired.
        if not ray.is_initialized():
            self.stdout.write(self.style.WARNING("\nRay disconnected, retiring pending tasks..."))
            self._terminalize_lost_ray_core_handles(
                runner,
                eligible_handles,
                error_message="Ray connection lost",
            )
            return retired_count + len(eligible_handles)

        # Poll for completed tasks
        try:
            completed = runner.poll_completed(eligible_handles)
        except Exception as e:
            diagnostic = render_console_diagnostic(e)
            self.stdout.write(self.style.ERROR(f"\nError polling Ray Core tasks: {diagnostic}"))
            return retired_count

        for completion in completed:
            task_pk = completion.task_pk
            attempt_number = completion.attempt_number
            execution_generation = completion.execution_generation
            try:
                task = RayTaskExecution.objects.get(pk=task_pk)

                if (
                    task.attempt_number != attempt_number
                    or task.execution_generation != execution_generation
                ):
                    self.stdout.write(
                        self.style.NOTICE(
                            f"\n  Ignoring stale Ray Core result for task {task.pk} "
                            f"attempt {attempt_number}, generation {execution_generation}"
                        )
                    )
                    continue

                # Cancellation wins over a concurrently completed Ray result.
                if task.state in (TaskState.CANCELLED, TaskState.CANCELLING):
                    if task.state == TaskState.CANCELLING:
                        self._finalize_ray_core_cancellation(
                            task_pk=task_pk,
                            attempt_number=attempt_number,
                            execution_generation=execution_generation,
                        )
                    self.stdout.write(self.style.WARNING(f"\n  Task {task.pk} was cancelled"))
                    continue

                if task.state != TaskState.RUNNING:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"\n  Ignoring Ray Core result for task {task.pk} in state {task.state}"
                        )
                    )
                    continue

                completion_succeeded = False
                with self._authoritative_task_owner(
                    task,
                    expected_state=TaskState.RUNNING,
                    allow_takeover=False,
                    require_completion_data_match=True,
                ) as owned:
                    if owned is None:
                        cancelled = self._finalize_ray_core_cancellation(
                            task_pk=task_pk,
                            attempt_number=attempt_number,
                            execution_generation=execution_generation,
                        )
                        if cancelled:
                            self.stdout.write(
                                self.style.WARNING(f"\n  Task {task.pk} was cancelled")
                            )
                            continue
                        self.stdout.write(
                            self.style.NOTICE(
                                f"\n  Task {task.pk} changed before its Ray Core "
                                "result could be applied"
                            )
                        )
                        continue

                    current = owned.execution
                    inspection = self._inspect_execution_completion(
                        completion.result_json,
                        current,
                        supported_protocols=owned.supported_protocols,
                    )
                    decoded = inspection.decoded
                    if decoded is None:
                        error_message, exception_type, retryable = (
                            self._completion_rejection_policy(inspection)
                        )
                        persisted = self._handle_task_failure(
                            current,
                            error_message=error_message,
                            exception_type=exception_type,
                            retryable=retryable,
                            expected_claimed_by_worker=self.worker_id,
                            expected_attempt_number=attempt_number,
                            expected_execution_generation=execution_generation,
                            expected_completion_data=current.completion_data,
                            require_completion_data_match=True,
                            supported_protocols=owned.supported_protocols,
                        )
                    elif decoded.completion.success:
                        accepted_completion = decoded.completion
                        completion_succeeded = True
                        persisted = self._store_and_succeed_task(
                            current,
                            accepted_completion.result,
                            prepared_result_reference=(inspection.prepared_result_reference),
                            expected_claimed_by_worker=self.worker_id,
                            expected_attempt_number=attempt_number,
                            expected_execution_generation=execution_generation,
                            expected_completion_data=current.completion_data,
                            require_completion_data_match=True,
                            supported_protocols=owned.supported_protocols,
                            executor_django_ray_version=(
                                accepted_completion.executor_django_ray_version
                            ),
                        )
                    else:
                        accepted_completion = decoded.completion
                        persisted = self._handle_task_failure(
                            current,
                            error_message=accepted_completion.error or "Unknown error",
                            error_traceback=accepted_completion.traceback,
                            exception_type=accepted_completion.exception_type,
                            retryable=accepted_completion.retryable,
                            expected_claimed_by_worker=self.worker_id,
                            expected_attempt_number=attempt_number,
                            expected_execution_generation=execution_generation,
                            expected_completion_data=current.completion_data,
                            require_completion_data_match=True,
                            supported_protocols=owned.supported_protocols,
                            executor_django_ray_version=(
                                accepted_completion.executor_django_ray_version
                            ),
                        )

                if not persisted:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"\n  Task {task.pk} changed while its Ray Core "
                            "result was being applied"
                        )
                    )
                elif completion_succeeded:
                    self.stdout.write(self.style.SUCCESS(f"\n  Task {task.pk} completed"))

            except RayTaskExecution.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"\n  Task {task_pk} not found in database"))
            except Exception as e:
                diagnostic = render_console_diagnostic(e)
                self.stdout.write(
                    self.style.ERROR(f"\n  Error processing task {task_pk} result: {diagnostic}")
                )

        return retired_count + len(completed)

    def submit_task_to_ray(self, task: RayTaskExecution) -> None:
        """Submit a task to Ray for execution."""
        from django_ray.runner import (
            RayJobRequestPreparationError,
            RayJobSubmissionUncertainError,
        )
        from django_ray.runner.ray_job import RayJobRunner
        from django_ray.workflow_plans import WorkflowPlanMismatchError

        expected_worker_id = self.worker_id
        expected_attempt_number = int(task.attempt_number)
        expected_execution_generation = int(task.execution_generation)
        expected_ray_job_id = task.ray_job_id

        try:
            runner = RayJobRunner()
        except RayJobRequestPreparationError as exc:
            self._handle_task_failure(
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
            import traceback

            self._handle_task_failure(
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
            reserved = self._persist_submission_tracking(
                task,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_ray_job_id=expected_ray_job_id,
            )
        except RayJobRequestPreparationError as exc:
            self._handle_task_failure(
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
            import traceback

            self._handle_task_failure(
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

        # The exact identity is durable before the request. A timeout after Ray
        # accepts the request can therefore be reconciled without resubmitting.
        self.active_tasks[task.pk] = reserved_handle.ray_job_id
        self.active_task_identities[task.pk] = (
            expected_attempt_number,
            expected_execution_generation,
        )

        try:
            handle = runner.submit_durable(task_execution=task)
        except RayJobSubmissionUncertainError as exc:
            try:
                still_reserved = self._persist_submission_tracking(
                    task,
                    reserved_handle,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_ray_job_id=reserved_handle.ray_job_id,
                )
            except Exception as tracking_exc:
                self._handle_ray_job_confirmation_loss(
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
                self._handle_ray_job_confirmation_loss(
                    task,
                    runner,
                    reserved_handle,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    detail="the reservation confirmation lost its compare-and-swap",
                )
                return
            self.stdout.write(
                self.style.WARNING(
                    f"  Task {task.pk} submission acceptance is uncertain; "
                    f"retaining exact Ray job {exc.submission_id} for reconciliation"
                )
            )
            return
        except RayJobRequestPreparationError as exc:
            from django_ray.ray_job_request_storage import (
                release_ray_job_request_reservation,
            )

            released = release_ray_job_request_reservation(
                task,
                reserved_handle,
                expected_reference=task.ray_job_request_reference,
            )
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)
            if released:
                self._handle_task_failure(
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
            import traceback

            from django_ray.ray_job_request_storage import (
                release_ray_job_request_reservation,
            )

            released = release_ray_job_request_reservation(
                task,
                reserved_handle,
                expected_reference=task.ray_job_request_reference,
            )
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)
            if released:
                self._handle_task_failure(
                    task,
                    error_message=(
                        f"Failed to submit to Ray: {materialize_exception_message(exc)}"
                    ),
                    error_traceback=(
                        None if isinstance(exc, RuntimeEnvSnapshotError) else traceback.format_exc()
                    ),
                    exception_type=safe_exception_type_name(exc),
                    retryable=(
                        False
                        if isinstance(
                            exc,
                            (RuntimeEnvSnapshotError, WorkflowPlanMismatchError),
                        )
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
            self._handle_mismatched_ray_job_submission(
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
            still_reserved = self._persist_submission_tracking(
                task,
                handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_ray_job_id=reserved_handle.ray_job_id,
            )
        except Exception as exc:
            self._handle_ray_job_confirmation_loss(
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
            self._handle_ray_job_confirmation_loss(
                task,
                runner,
                handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail="the post-submit confirmation lost its compare-and-swap",
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"  Task {task.pk} submitted as Ray job {handle.ray_job_id}")
        )

    def _build_submission_handle(
        self,
        task: RayTaskExecution,
        ray_job_id: str,
    ) -> SubmissionHandle:
        """Build a submission handle from persisted task metadata."""
        return SubmissionHandle(
            ray_job_id=ray_job_id,
            ray_address=task.ray_address or "",
            submitted_at=task.started_at or datetime.now(UTC),
        )

    def _lock_authoritative_leases(
        self,
        *,
        source_worker_id: str | None,
        allow_takeover: bool,
        renew_heartbeat: bool = True,
        required_execution_protocol_version: int | None = None,
    ) -> TaskWorkerLease | None:
        """Lock involved leases and prove this process still owns its identity.

        The caller must already be inside ``transaction.atomic()`` and must
        acquire these lease locks before the execution row. PostgreSQL gets
        deterministic row locks; SQLite gets its database-wide write fence
        from the exact conditional heartbeat update.
        """
        identity = self.lease_identity
        if identity is None:
            self._request_shutdown_for_lease_loss("worker lease was never acquired")
            return None

        lease_worker_ids = sorted(
            worker_id
            for worker_id in {identity.worker_id, source_worker_id}
            if worker_id is not None
        )
        if connection.features.has_select_for_update:
            list(
                TaskWorkerLease.objects.select_for_update()
                .filter(worker_id__in=lease_worker_ids)
                .order_by("worker_id")
            )
        else:
            from django.db.models import F

            # SQLite has no row-level ``SELECT FOR UPDATE``. An exact no-op
            # write obtains its database-wide writer fence before freshness is
            # measured, without reviving an already-expired lease.
            TaskWorkerLease.objects.filter(**identity.database_filters()).update(
                last_heartbeat_at=F("last_heartbeat_at")
            )

        # Lock acquisition may itself take longer than the lease duration.
        # Freshness must therefore be measured only after every ordered lease
        # lock (or SQLite's write fence) is held.
        validation_time = datetime.now(UTC)
        cutoff = validation_time - get_lease_duration()
        lease_filters = {
            **identity.database_filters(),
            "is_active": True,
            "last_heartbeat_at__gte": cutoff,
        }
        if renew_heartbeat:
            owns_live_lease = TaskWorkerLease.objects.filter(**lease_filters).update(
                last_heartbeat_at=validation_time
            )
        else:
            owns_live_lease = TaskWorkerLease.objects.filter(**lease_filters).exists()

        if not owns_live_lease:
            self._request_shutdown_for_lease_loss(
                "worker lease expired, became inactive, or disappeared before task mutation"
            )
            return None
        if renew_heartbeat and self.lease is not None:
            self.lease.last_heartbeat_at = validation_time

        live_lease = TaskWorkerLease.objects.filter(**lease_filters).first()
        if live_lease is None:
            self._request_shutdown_for_lease_loss(
                "worker lease changed before its capabilities could be read"
            )
            return None

        if required_execution_protocol_version is not None:
            supported_protocols = self._explicit_protocol_range(live_lease)
            if supported_protocols is None or not supported_protocols.supports(
                required_execution_protocol_version
            ):
                # Protocol mismatch is an admission refusal, not lease loss.
                # Check it before retiring a stale source lease so an
                # incompatible manager cannot mutate another cohort's lease.
                return None

        if source_worker_id is None or source_worker_id == self.worker_id:
            return live_lease
        if not allow_takeover:
            return None

        if TaskWorkerLease.objects.filter(
            worker_id=source_worker_id,
            is_active=True,
            last_heartbeat_at__gte=cutoff,
        ).exists():
            return None

        TaskWorkerLease.objects.filter(
            worker_id=source_worker_id,
            is_active=True,
            last_heartbeat_at__lt=cutoff,
        ).update(
            is_active=False,
            stopped_at=validation_time,
        )
        # A boundary-value heartbeat or a concurrently recreated row is not
        # proof that the observed execution owner is stale.
        takeover_allowed = not TaskWorkerLease.objects.filter(
            worker_id=source_worker_id,
            is_active=True,
        ).exists()
        return live_lease if takeover_allowed else None

    @staticmethod
    def _explicit_protocol_range(lease: TaskWorkerLease) -> ExecutionProtocolRange | None:
        """Read the immutable protocol range from one locked explicit lease."""
        return explicit_worker_protocol_range(
            capability_schema_version=int(lease.capability_schema_version),
            legacy_admission_token_present=lease.legacy_admission_token_id is not None,
            minimum=lease.min_supported_execution_protocol_version,
            maximum=lease.max_supported_execution_protocol_version,
        )

    def _preliminary_takeover_protocol_range(self) -> ExecutionProtocolRange | None:
        """Read immutable candidate capability without locking a source lease.

        This advisory read avoids taking the candidate lease out of the final
        globally sorted lock order when a caller already owns a transaction.
        The combined lease lock revalidates identity, liveness, and protocol
        support before any source-lease or execution mutation.
        """
        identity = self.lease_identity
        if identity is None:
            self._request_shutdown_for_lease_loss("worker lease was never acquired")
            return None

        cutoff = datetime.now(UTC) - get_lease_duration()
        lease = (
            TaskWorkerLease.objects.filter(
                **identity.database_filters(),
                is_active=True,
                last_heartbeat_at__gte=cutoff,
            )
            .only(
                "capability_schema_version",
                "legacy_admission_token",
                "min_supported_execution_protocol_version",
                "max_supported_execution_protocol_version",
            )
            .first()
        )
        if lease is None:
            self._request_shutdown_for_lease_loss(
                "worker lease expired, became inactive, disappeared, or was replaced "
                "before takeover"
            )
            return None
        return self._explicit_protocol_range(lease)

    def _authoritative_protocol_range_for_scan(self) -> ExecutionProtocolRange | None:
        """Read this process's exact durable capability before a task scan."""
        with transaction.atomic():
            lease = self._lock_authoritative_leases(
                source_worker_id=self.worker_id,
                allow_takeover=False,
                renew_heartbeat=False,
            )
            if lease is None:
                return None
            return self._explicit_protocol_range(lease)

    @contextmanager
    def _authoritative_task_owner(
        self,
        snapshot: RayTaskExecution,
        *,
        expected_state: str,
        allow_takeover: bool,
        require_completion_data_match: bool = False,
    ) -> Iterator[_OwnedTask | None]:
        """Yield the exact current execution while lease and task locks are held.

        Callers that perform a remote state-changing effect must do so inside
        this context. The task lock then serializes that effect with ownership
        transfer and its corresponding durable terminal transition.
        """
        source_worker_id = str(snapshot.claimed_by_worker) if snapshot.claimed_by_worker else None
        required_protocol = int(snapshot.execution_protocol_version)

        if source_worker_id is not None and source_worker_id != self.worker_id:
            # Reject a mismatch without acquiring the source lease. The
            # capability fields are immutable; the final globally sorted lock
            # still rechecks the exact live identity and range for TOCTOU safety.
            preliminary_protocols = self._preliminary_takeover_protocol_range()
            if preliminary_protocols is None or not preliminary_protocols.supports(
                required_protocol
            ):
                yield None
                return

        with transaction.atomic():
            lease = self._lock_authoritative_leases(
                source_worker_id=source_worker_id,
                allow_takeover=allow_takeover,
                required_execution_protocol_version=required_protocol,
            )
            if lease is None:
                yield None
                return

            if source_worker_id != self.worker_id and not allow_takeover:
                yield None
                return

            task_filters: dict[str, Any] = {
                "pk": snapshot.pk,
                "state": expected_state,
                "ray_job_id": snapshot.ray_job_id,
                "ray_address": snapshot.ray_address,
                "attempt_number": snapshot.attempt_number,
                "execution_generation": snapshot.execution_generation,
                "execution_protocol_version": snapshot.execution_protocol_version,
                "started_at": snapshot.started_at,
                "last_heartbeat_at": snapshot.last_heartbeat_at,
            }
            if source_worker_id is None:
                task_filters["claimed_by_worker__isnull"] = True
            else:
                task_filters["claimed_by_worker"] = source_worker_id
            if require_completion_data_match:
                task_filters["completion_data"] = snapshot.completion_data

            current = RayTaskExecution.objects.select_for_update().filter(**task_filters).first()
            if current is None:
                yield None
                return

            supported_protocols = self._explicit_protocol_range(lease)
            if supported_protocols is None or not supported_protocols.supports(
                int(current.execution_protocol_version)
            ):
                yield None
                return

            adopted = source_worker_id != self.worker_id
            if adopted:
                current.claimed_by_worker = self.worker_id
                current.managed_with_django_ray_version = django_ray_version
                current.save(
                    update_fields=[
                        "claimed_by_worker",
                        "managed_with_django_ray_version",
                    ]
                )

            yield _OwnedTask(
                execution=current,
                adopted=adopted,
                supported_protocols=supported_protocols,
            )

    def _take_over_task_if_owner_stale(
        self,
        task: RayTaskExecution,
        *,
        now: datetime,
    ) -> RayTaskExecution | None:
        """Validate this lease and atomically fence a stale task owner."""
        del now  # The lock context captures one authoritative transaction time.
        current: RayTaskExecution | None = None
        with self._authoritative_task_owner(
            task,
            expected_state=str(task.state),
            allow_takeover=True,
            require_completion_data_match=True,
        ) as owned:
            if owned is not None:
                current = owned.execution

        if current is not None:
            task.__dict__.update(current.__dict__)
        return current

    def _adopt_orphaned_ray_job_task(self, task: RayTaskExecution, *, now: datetime) -> bool:
        """Fence a stale owner and adopt its Ray Job for reconciliation."""
        current = self._take_over_task_if_owner_stale(task, now=now)
        if current is None or current.state != TaskState.RUNNING:
            return False
        if task.ray_job_id:
            self.active_tasks[task.pk] = str(task.ray_job_id)
            self.active_task_identities[task.pk] = (
                int(task.attempt_number),
                int(task.execution_generation),
            )
        return True

    def _retire_active_ray_job_tracking(
        self,
        task_pk: int,
        *,
        ray_job_id: str,
        identity: tuple[int, int],
    ) -> None:
        """Forget active tracking only when the exact tracked identity still matches."""
        if self.active_tasks.get(task_pk) != ray_job_id:
            return
        tracked_identity = self.active_task_identities.get(task_pk)
        if tracked_identity is not None and tracked_identity != identity:
            return
        self.active_tasks.pop(task_pk, None)
        self.active_task_identities.pop(task_pk, None)

    def _reconcile_ray_job_task(
        self,
        task: RayTaskExecution,
        runner: Any,
        *,
        ray_job_id: str,
        completed_tasks: list[int],
        orphaned: bool,
        tracked_identity: tuple[int, int] | None = None,
    ) -> None:
        """Reconcile a single Ray Job task from either active or orphaned tracking."""
        from django_ray.runner.base import JobStatus

        task_identity = (int(task.attempt_number), int(task.execution_generation))
        expected_identity = tracked_identity or task_identity
        expected_worker_id = self.worker_id

        def complete_tracking() -> None:
            completed_tasks.append(task.pk)
            self._retire_active_ray_job_tracking(
                task.pk,
                ray_job_id=ray_job_id,
                identity=expected_identity,
            )

        # Active tracking may outlive a retry that reused the same Ray Job ID.
        # Reject that stale identity before making a status RPC.
        if task_identity != expected_identity:
            self._retire_active_ray_job_tracking(
                task.pk,
                ray_job_id=ray_job_id,
                identity=expected_identity,
            )
            return

        # Older or deliberately handed-off rows may be ownerless while this
        # process still has their exact durable Ray Job capability in memory.
        # Claim that row before the first reconciliation effect.
        if task.claimed_by_worker is None and not self._adopt_orphaned_ray_job_task(
            task,
            now=datetime.now(UTC),
        ):
            self._retire_active_ray_job_tracking(
                task.pk,
                ray_job_id=ray_job_id,
                identity=expected_identity,
            )
            return

        # A stale process may retain this Ray Job in memory after another
        # worker atomically adopts the durable execution. Retire local
        # tracking before any status, storage, or cancellation side effect.
        if str(task.claimed_by_worker or "") != expected_worker_id:
            self._retire_active_ray_job_tracking(
                task.pk,
                ray_job_id=ray_job_id,
                identity=expected_identity,
            )
            return

        # CANCELLING remains owned by process_cancellations(), which requests
        # status-aware remote interruption before finalizing the durable row.
        if task.state == TaskState.CANCELLING:
            return
        if task.state == TaskState.CANCELLED:
            complete_tracking()
            self.stdout.write(self.style.WARNING(f"\nTask {task.pk} was cancelled"))
            return
        if task.state != TaskState.RUNNING:
            complete_tracking()
            return

        expected_attempt_number, expected_execution_generation = expected_identity
        handle = self._build_submission_handle(task, ray_job_id)
        strict_ray_job = is_strict_ray_job_submission_id(ray_job_id)
        rq2_ray_job = is_rq2_ray_job_submission_id(ray_job_id)

        def consume_valid_completion(
            completion_data: str | None,
        ) -> tuple[bool, _InspectedExecutionCompletion | None]:
            """Apply one exact envelope or return its fixed rejection policy."""
            if completion_data is None:
                return False, None

            handled = False
            current: RayTaskExecution | None = None
            with self._authoritative_task_owner(
                task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is None:
                    return True, None
                current = owned.execution
                inspection = self._inspect_execution_completion(
                    completion_data,
                    current,
                    supported_protocols=owned.supported_protocols,
                )
                decoded = inspection.decoded
                if decoded is None:
                    return False, inspection
                accepted_completion = decoded.completion
                if accepted_completion.success:
                    handled = self._store_and_succeed_task(
                        current,
                        accepted_completion.result,
                        prepared_result_reference=inspection.prepared_result_reference,
                        expected_ray_job_id=ray_job_id,
                        expected_claimed_by_worker=expected_worker_id,
                        expected_attempt_number=expected_attempt_number,
                        expected_execution_generation=expected_execution_generation,
                        expected_completion_data=completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                        executor_django_ray_version=(
                            accepted_completion.executor_django_ray_version
                        ),
                    )
                else:
                    handled = self._handle_task_failure(
                        current,
                        error_message=accepted_completion.error or "Unknown error",
                        error_traceback=accepted_completion.traceback,
                        exception_type=accepted_completion.exception_type,
                        retryable=accepted_completion.retryable,
                        expected_ray_job_id=ray_job_id,
                        expected_claimed_by_worker=expected_worker_id,
                        expected_attempt_number=expected_attempt_number,
                        expected_execution_generation=expected_execution_generation,
                        expected_completion_data=completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                        executor_django_ray_version=(
                            accepted_completion.executor_django_ray_version
                        ),
                    )
                if handled is False:
                    return True, None

            assert current is not None
            task.__dict__.update(current.__dict__)
            if accepted_completion.success:
                self.stdout.write(self.style.SUCCESS(f"\nTask {task.pk} completed"))
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"\nTask {task.pk} returned failure envelope, handling via retry policy"
                    )
                )
            complete_tracking()
            return True, None

        # The entrypoint's valid durable envelope is authoritative. Consume it
        # before contacting Ray so a control-plane outage cannot strand a task
        # whose terminal result is already safely persisted.
        completion_consumed, completion_inspection = consume_valid_completion(task.completion_data)
        if completion_consumed:
            return

        job_info = runner.get_status(handle)
        now = datetime.now(UTC)

        # Refresh after the status RPC: the execution may have been retried or
        # claimed elsewhere while the RPC was in flight.
        try:
            task.refresh_from_db(
                fields=[
                    "state",
                    "completion_data",
                    "ray_job_id",
                    "ray_address",
                    "ray_job_request_reference",
                    "claimed_by_worker",
                    "attempt_number",
                    "execution_generation",
                    "execution_protocol_version",
                    "started_at",
                    "last_heartbeat_at",
                ]
            )
        except RayTaskExecution.DoesNotExist:
            complete_tracking()
            return

        # Never reconcile an old Ray Job against a replacement execution.
        if (
            str(task.ray_job_id or "") != ray_job_id
            or str(task.claimed_by_worker or "") != expected_worker_id
            or task.attempt_number != expected_attempt_number
            or task.execution_generation != expected_execution_generation
        ):
            self._retire_active_ray_job_tracking(
                task.pk,
                ray_job_id=ray_job_id,
                identity=expected_identity,
            )
            return

        if task.state != TaskState.RUNNING:
            complete_tracking()
            return

        def resolve_stale_untrusted_execution(
            *,
            expected_completion_data: str | None,
            error_message: str,
            log_detail: str,
            require_stale: bool = True,
            binding_validator: Any | None = None,
        ) -> bool:
            """Fence an untrusted execution, request its exact stop, and retain LOST."""
            timeout_recovery_owns_task = expected_completion_data is None and is_task_timed_out(
                task
            )
            if require_stale and (timeout_recovery_owns_task or not is_task_stuck(task)):
                return False

            prepared_cancellation = prepare_remote_cancellation(runner, handle)

            current: RayTaskExecution | None = None
            cancellation: CancellationOutcome | None = None
            # Revalidate the exact live worker lease after the status RPC and
            # hold lease+execution locks through LOST archival and the bounded
            # stop. A resumed stale process cannot act only because its old ID
            # remains on the task row.
            with self._authoritative_task_owner(
                task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is None:
                    return False
                current = owned.execution
                timeout_recovery_owns_task = expected_completion_data is None and is_task_timed_out(
                    current
                )
                if require_stale and (timeout_recovery_owns_task or not is_task_stuck(current)):
                    return False
                if binding_validator is not None and binding_validator(current) is None:
                    return False
                if not record_lost(
                    current,
                    error_message=error_message,
                    expected_completion_data=expected_completion_data,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    supported_protocols=owned.supported_protocols,
                ):
                    return False
                cancellation = request_remote_cancellation(
                    runner,
                    handle,
                    prepared=prepared_cancellation,
                )
                current.cancellation_status = CancellationStatus(cancellation.status.value)
                current.cancellation_error = cancellation.message
                current.save(update_fields=["cancellation_status", "cancellation_error"])

            assert current is not None
            assert cancellation is not None
            task.__dict__.update(current.__dict__)
            complete_tracking()
            self.stdout.write(
                self.style.ERROR(
                    f"\nTask {task.pk} {log_detail}; exact best-effort stop outcome was "
                    f"{cancellation.status.value} and automatic retry was suppressed"
                )
            )
            return True

        def handle_failure_authoritatively(
            *,
            error_message: str,
            exception_type: str,
            expected_completion_data: str | None,
            error_traceback: str | None = None,
            retryable: bool | None = None,
        ) -> bool:
            """Apply one Ray Job failure only behind the exact live lease fence."""
            current: RayTaskExecution | None = None
            handled = False
            with self._authoritative_task_owner(
                task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is None:
                    return False
                current = owned.execution
                handled = self._handle_task_failure(
                    current,
                    error_message=error_message,
                    error_traceback=error_traceback,
                    exception_type=exception_type,
                    retryable=retryable,
                    expected_ray_job_id=ray_job_id,
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=expected_completion_data,
                    require_completion_data_match=True,
                    supported_protocols=owned.supported_protocols,
                )

            if handled:
                assert current is not None
                task.__dict__.update(current.__dict__)
            return handled

        completion_data = task.completion_data

        strict_metadata_marker = ray_job_metadata_has_strict_marker(
            getattr(job_info, "metadata", None)
        )
        # The entrypoint's durable envelope is authoritative even while Ray
        # briefly continues to report PENDING/RUNNING during process teardown.
        # Consume it before monitor heartbeat or timeout logic can obscure it.
        completion_consumed, completion_inspection = consume_valid_completion(completion_data)
        if completion_consumed:
            return

        def strict_binding_rejection(candidate: RayTaskExecution) -> str | None:
            """Validate the status metadata against one current durable row."""
            if strict_metadata_marker and not strict_ray_job:
                return "unexpected_strict_metadata"
            if not strict_ray_job:
                return None
            if not is_valid_strict_ray_job_submission_id(ray_job_id):
                return "invalid_submission_id"
            if job_info.job_id != ray_job_id:
                return "job_id_mismatch"
            if job_info.status == JobStatus.UNKNOWN and job_info.metadata is None:
                return None
            try:
                expectation = parse_ray_job_request_metadata(
                    job_info.metadata,
                    required=True,
                )
                assert expectation is not None
                if rq2_ray_job:
                    if not isinstance(expectation, RayJobRequestReferenceExpectation):
                        return "invalid"
                    request_reference = candidate.ray_job_request_reference
                    if not request_reference:
                        return "missing"
                    from django_ray.ray_job_request_storage import (
                        RayJobRequestStorageError,
                        ray_job_request_reference_content_identity,
                    )

                    try:
                        request_digest, request_size_bytes = (
                            ray_job_request_reference_content_identity(request_reference)
                        )
                    except RayJobRequestStorageError as exc:
                        return exc.classification.value
                    validate_ray_job_request_reference_expectation(
                        expectation,
                        expected_identity=self._execution_completion_identity(candidate),
                        expected_execution_protocol_version=int(
                            candidate.execution_protocol_version
                        ),
                        expected_request_sha256=request_digest,
                        expected_request_size_bytes=request_size_bytes,
                        expected_submission_id=ray_job_id,
                        request_reference=request_reference,
                    )
                else:
                    if not isinstance(expectation, RayJobRequestExpectation):
                        return "invalid"
                    validate_ray_job_request_expectation(
                        expectation,
                        expected_identity=self._execution_completion_identity(candidate),
                        expected_execution_protocol_version=int(
                            candidate.execution_protocol_version
                        ),
                    )
            except RayJobRequestBindingError as exc:
                return exc.classification.value
            return None

        binding_rejection = strict_binding_rejection(task)
        if binding_rejection is not None:
            legacy_marker_conflict = not strict_ray_job
            resolve_stale_untrusted_execution(
                expected_completion_data=completion_data,
                error_message=(
                    f"Strict Ray Job request binding could not be verified ({binding_rejection})"
                ),
                log_detail=(
                    "had strict request metadata attached to a legacy submission ID"
                    if legacy_marker_conflict
                    else f"had an untrusted strict request binding ({binding_rejection})"
                ),
                require_stale=False,
                binding_validator=strict_binding_rejection,
            )
            return

        strict_terminal = strict_ray_job and job_info.status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.STOPPED,
        )
        if strict_terminal:
            if not self._completion_envelope_grace_expired(task, now=now):
                self.stdout.write(
                    self.style.NOTICE(
                        f"\nTask {task.pk} strict Ray job {job_info.status.value.lower()}; "
                        "waiting for an exact completion envelope"
                    )
                )
                return
            handled = handle_failure_authoritatively(
                error_message=("Strict Ray Job terminated without an exact completion envelope"),
                exception_type="RayJobCompletionMissing",
                expected_completion_data=completion_data,
                retryable=False,
            )
            if handled:
                complete_tracking()
                self.stdout.write(
                    self.style.ERROR(
                        f"\nTask {task.pk} strict Ray job exceeded the completion "
                        "envelope grace period without automatic retry"
                    )
                )
            return

        if completion_data is None and job_info.status in (
            JobStatus.PENDING,
            JobStatus.RUNNING,
        ):
            current: RayTaskExecution | None = None
            with self._authoritative_task_owner(
                task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is None:
                    return
                current = owned.execution
                self._mark_task_monitor_heartbeat(
                    current,
                    now=now,
                    ray_job_id=ray_job_id,
                    expected_worker_id=expected_worker_id,
                    attempt_number=expected_attempt_number,
                    execution_generation=expected_execution_generation,
                )
            task.__dict__.update(current.__dict__)
            return

        if completion_data is not None:
            assert completion_inspection is not None
            assert completion_inspection.decoded is None
            assert completion_inspection.rejection is not None
            completion_error = (
                "malformed"
                if completion_inspection.rejection is ExecutionCompletionRejection.MALFORMED_LEGACY
                else completion_inspection.rejection.value.replace("_", " ")
            )

            if completion_inspection.requires_nonretryable_disposition:
                error_message, exception_type, retryable = self._completion_rejection_policy(
                    completion_inspection
                )
                if job_info.status in (
                    JobStatus.UNKNOWN,
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                ):
                    resolve_stale_untrusted_execution(
                        expected_completion_data=completion_data,
                        error_message=error_message,
                        log_detail="published a non-retryable incompatible completion",
                        require_stale=False,
                    )
                    return
                handled = handle_failure_authoritatively(
                    error_message=error_message,
                    exception_type=exception_type,
                    expected_completion_data=completion_data,
                    retryable=retryable,
                )
                if handled:
                    complete_tracking()
                    self.stdout.write(
                        self.style.ERROR(
                            f"\nTask {task.pk} terminalized a non-retryable incompatible "
                            "completion without automatic retry"
                        )
                    )
                return

            if completion_inspection.rejection is not None:
                if job_info.status == JobStatus.UNKNOWN:
                    if resolve_stale_untrusted_execution(
                        expected_completion_data=completion_data,
                        error_message=(
                            "Ray Job status remained UNKNOWN past the stuck-task timeout"
                        ),
                        log_detail=("Ray job status remained unknown past the stuck-task timeout"),
                    ):
                        return
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {task.pk} has a {completion_error} completion envelope "
                            "while Ray job status is unknown; waiting for bounded recovery"
                        )
                    )
                    return
                if job_info.status in (JobStatus.PENDING, JobStatus.RUNNING):
                    if resolve_stale_untrusted_execution(
                        expected_completion_data=completion_data,
                        error_message=(
                            f"Ray Job produced a {completion_error} completion envelope "
                            f"while still {job_info.status.value}"
                        ),
                        log_detail=(
                            f"had a {completion_error} completion envelope while Ray "
                            f"still reported {job_info.status.value}"
                        ),
                    ):
                        return
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {task.pk} has a {completion_error} completion envelope "
                            f"while Ray still reports {job_info.status.value}; waiting for "
                            "bounded recovery"
                        )
                    )
                    return
                if self._completion_envelope_grace_expired(task, now=now):
                    handled = handle_failure_authoritatively(
                        error_message=f"Ray Job produced a {completion_error} completion envelope",
                        exception_type="RayCompletionMalformed",
                        expected_completion_data=completion_data,
                    )
                    if handled is False:
                        return
                    complete_tracking()
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {task.pk} exceeded the completion envelope grace period"
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {task.pk} has a {completion_error} completion envelope; "
                            "waiting for a valid update"
                        )
                    )
                return

            raise AssertionError("valid completion envelope was not consumed")

        if job_info.status == JobStatus.SUCCEEDED:
            # Ray Job logs are diagnostic only. Missing envelopes remain
            # non-terminal until the bounded grace period expires.
            if self._completion_envelope_grace_expired(task, now=now):
                handled = handle_failure_authoritatively(
                    error_message="Ray Job completed without a completion envelope",
                    exception_type="RayCompletionUnknown",
                    expected_completion_data=completion_data,
                )
                if handled is False:
                    return
                complete_tracking()
                self.stdout.write(
                    self.style.WARNING(
                        f"\nTask {task.pk} exceeded the completion envelope grace period"
                    )
                )
                return
            self.stdout.write(
                self.style.NOTICE(
                    f"\nTask {task.pk} Ray job succeeded; waiting for completion envelope"
                )
            )
            return

        if job_info.status == JobStatus.FAILED:
            logs = runner.get_logs(handle)
            handled = handle_failure_authoritatively(
                error_message=job_info.message or "Ray job failed",
                error_traceback=logs,
                exception_type="RayJobFailed",
                expected_completion_data=completion_data,
            )
            if handled is False:
                return
            complete_tracking()
            diagnostic = render_console_diagnostic(task.error_message or "Ray job failed")
            self.stdout.write(self.style.ERROR(f"\nTask {task.pk} failed: {diagnostic}"))
            return

        if job_info.status == JobStatus.STOPPED:
            current = None
            cancelled = False
            with self._authoritative_task_owner(
                task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is None:
                    return
                current = owned.execution
                cancelled = cancel_task(
                    current,
                    allowed_states=(TaskState.RUNNING,),
                    expected_worker_id=expected_worker_id,
                    expected_ray_job_id=ray_job_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=completion_data,
                    require_completion_data_match=True,
                )
                if not cancelled:
                    return
            assert current is not None
            task.__dict__.update(current.__dict__)
            complete_tracking()
            self.stdout.write(self.style.WARNING(f"\nTask {task.pk} was stopped"))
            return

        if job_info.status == JobStatus.UNKNOWN and resolve_stale_untrusted_execution(
            expected_completion_data=None,
            error_message="Ray Job status remained UNKNOWN past the stuck-task timeout",
            log_detail="Ray job status remained unknown past the stuck-task timeout",
        ):
            return

        scope = "orphaned " if orphaned else ""
        diagnostic = render_console_diagnostic(job_info.message or "no details")
        self.stdout.write(
            self.style.WARNING(f"\nTask {task.pk} {scope}Ray job status is unknown: {diagnostic}")
        )

    def _request_timeout_cancellation(self, task: RayTaskExecution) -> CancellationOutcome:
        """Stop a timed-out execution by its exact recorded backend identity."""
        return self._request_cancellation_for_task(task)

    def reconcile_tasks(self, queues: Sequence[str] | None = None) -> int:
        """Reconcile Ray Jobs, optionally fencing orphan adoption by queue.

        The production loop always supplies its validated queue selection. The
        default retains the all-queue scan for direct administrative callers and
        compatibility with existing command-method integrations.
        """
        if self.sync_mode or self.shutdown_requested:
            return 0

        supported_protocols = self._authoritative_protocol_range_for_scan()
        if supported_protocols is None:
            return 0

        from django_ray.runner.leasing import get_active_workers
        from django_ray.runner.ray_job import RayJobRunner

        runner: RayJobRunner | None = None
        completed_tasks: list[int] = []
        reconciled_task_ids: set[int] = set()
        active_task_ids_before = set(self.active_tasks)

        for task_pk, ray_job_id in list(self.active_tasks.items()):
            if self.shutdown_requested:
                break
            tracked_identity = self.active_task_identities.get(task_pk)
            try:
                if runner is None:
                    runner = RayJobRunner()
                task = RayTaskExecution.objects.filter(
                    execution_protocol_version__gte=supported_protocols.minimum,
                    execution_protocol_version__lte=supported_protocols.maximum,
                ).get(pk=task_pk)
                reconciled_task_ids.add(task.pk)
                self._reconcile_ray_job_task(
                    task,
                    runner,
                    ray_job_id=ray_job_id,
                    completed_tasks=completed_tasks,
                    orphaned=False,
                    tracked_identity=tracked_identity,
                )

            except RayTaskExecution.DoesNotExist:
                completed_tasks.append(task_pk)
                if tracked_identity is not None:
                    self._retire_active_ray_job_tracking(
                        task_pk,
                        ray_job_id=ray_job_id,
                        identity=tracked_identity,
                    )
                elif self.active_tasks.get(task_pk) == ray_job_id:
                    self.active_tasks.pop(task_pk, None)
            except Exception as e:
                diagnostic = render_console_diagnostic(e)
                self.stdout.write(
                    self.style.ERROR(f"\nError reconciling task {task_pk}: {diagnostic}")
                )

        if self.shutdown_requested:
            return len(completed_tasks)

        active_worker_ids = {str(lease.worker_id) for lease in get_active_workers()}
        orphaned_tasks = RayTaskExecution.objects.filter(
            state=TaskState.RUNNING,
            ray_job_id__startswith="raysubmit_",
            execution_protocol_version__gte=supported_protocols.minimum,
            execution_protocol_version__lte=supported_protocols.maximum,
        ).exclude(pk__in=reconciled_task_ids)
        if queues is not None:
            orphaned_tasks = orphaned_tasks.filter(queue_name__in=queues)

        for task in orphaned_tasks:
            if self.shutdown_requested:
                break
            task_worker_id = str(task.claimed_by_worker) if task.claimed_by_worker else None
            if task_worker_id == self.worker_id:
                continue
            if task_worker_id and task_worker_id in active_worker_ids:
                continue

            try:
                if runner is None:
                    runner = RayJobRunner()
                if not self._adopt_orphaned_ray_job_task(task, now=datetime.now(UTC)):
                    continue
                self.stdout.write(
                    self.style.NOTICE(
                        f"\nAdopted orphaned Ray job task {task.pk} for continued monitoring"
                    )
                )
                self._reconcile_ray_job_task(
                    task,
                    runner,
                    ray_job_id=str(task.ray_job_id or ""),
                    completed_tasks=completed_tasks,
                    orphaned=True,
                )
            except Exception as e:
                diagnostic = render_console_diagnostic(e)
                self.stdout.write(
                    self.style.ERROR(f"\nError reconciling orphaned task {task.pk}: {diagnostic}")
                )

        adopted_count = len(set(self.active_tasks) - active_task_ids_before)
        return len(completed_tasks) + adopted_count

    def detect_stuck_tasks(self, queues: Sequence[str] | None = None) -> int:
        """Detect and mark stuck tasks as LOST.

        This checks for tasks that have been RUNNING for too long without
        heartbeats, which indicates the worker processing them may have crashed.
        The production loop supplies its selected queues; omitting them retains
        the all-queue administrative contract.
        """
        from django_ray.runner.leasing import get_active_workers

        if self.shutdown_requested:
            return 0

        supported_protocols = self._authoritative_protocol_range_for_scan()
        if supported_protocols is None:
            return 0

        # Check all running tasks. For tasks owned by active workers, skip recovery
        # and let the owning worker manage its own in-flight work.
        running_tasks = RayTaskExecution.objects.filter(
            state=TaskState.RUNNING,
            execution_protocol_version__gte=supported_protocols.minimum,
            execution_protocol_version__lte=supported_protocols.maximum,
        )
        if queues is not None:
            running_tasks = running_tasks.filter(queue_name__in=queues)

        active_worker_ids = {str(lease.worker_id) for lease in get_active_workers()}
        ray_core_pending_handles = (
            {handle.task_pk: handle for handle in self.ray_core_runner.pending_task_handles}
            if self.ray_core_runner
            else {}
        )

        stuck_count = 0
        timeout_count = 0
        orphan_recovered_count = 0
        for task in running_tasks:
            task_worker_id = str(task.claimed_by_worker) if task.claimed_by_worker else None
            claimed_by_this_worker = task_worker_id == self.worker_id
            claimed_by_active_worker = bool(task_worker_id) and task_worker_id in active_worker_ids
            pending_ray_core_handle = ray_core_pending_handles.get(task.pk)
            pending_ray_core_identity_matches = bool(
                pending_ray_core_handle
                and task.attempt_number == pending_ray_core_handle.attempt_number
                and task.execution_generation == pending_ray_core_handle.execution_generation
            )
            active_ray_job_identity_matches = bool(
                self.active_tasks.get(task.pk) == str(task.ray_job_id or "")
                and self.active_task_identities.get(task.pk)
                == (int(task.attempt_number), int(task.execution_generation))
            )

            # Ray Job entrypoints publish their terminal envelope while the row
            # is still RUNNING. Reconciliation owns that channel; timeout or
            # generic LOST recovery must not overwrite a preexisting envelope.
            if task.completion_data is not None:
                continue

            # Leave tasks owned by healthy workers alone.
            if not claimed_by_this_worker and claimed_by_active_worker:
                continue

            # Check for timeout first (applies to all tasks)
            if is_task_timed_out(task):
                marked_timed_out = False
                adopted = False
                current: RayTaskExecution | None = None
                # Lease locks precede the execution lock, which remains held
                # through the bounded exact stop and terminal transition.
                with self._authoritative_task_owner(
                    task,
                    expected_state=TaskState.RUNNING,
                    allow_takeover=True,
                    require_completion_data_match=True,
                ) as owned:
                    if owned is None:
                        if self.shutdown_requested:
                            break
                        continue
                    current = owned.execution
                    if current.completion_data is not None or not is_task_timed_out(current):
                        continue
                    adopted = owned.adopted
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {current.pk} timed out after {current.timeout_seconds}s"
                        )
                    )
                    cancellation = self._request_timeout_cancellation(current)
                    marked_timed_out = mark_task_timed_out(
                        current,
                        cancellation_status=CancellationStatus(cancellation.status.value),
                        cancellation_error=cancellation.message,
                        expected_ray_job_id=(
                            str(current.ray_job_id) if current.ray_job_id else None
                        ),
                        expected_claimed_by_worker=self.worker_id,
                        expected_attempt_number=current.attempt_number,
                        expected_execution_generation=current.execution_generation,
                        expected_completion_data=None,
                        require_completion_data_match=True,
                    )
                if self.shutdown_requested:
                    break
                if marked_timed_out:
                    assert current is not None
                    task.__dict__.update(current.__dict__)
                    if task.pk in self.active_tasks:
                        del self.active_tasks[task.pk]
                        self.active_task_identities.pop(task.pk, None)
                    timeout_count += 1
                    if adopted:
                        orphan_recovered_count += 1
                else:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"\nTask {task.pk} completed while timeout cancellation was in flight"
                        )
                    )
                continue

            # Persisted Ray Job IDs are recoverable capabilities even when this
            # worker is not currently tracking them. Exact reconciliation owns
            # UNKNOWN/stale resolution and best-effort stop; generic LOST retry
            # must never launch a replacement while that job may still run.
            if str(task.ray_job_id or "").startswith("raysubmit_"):
                continue

            # An exact local Ray Core handle is stronger ownership evidence than
            # its persisted monitor timestamp. Polling owns completion and
            # disconnect recovery for that handle; generic LOST recovery must
            # not requeue the same PK while its ObjectRef remains live.
            if claimed_by_this_worker and (
                pending_ray_core_identity_matches or active_ray_job_identity_matches
            ):
                continue

            tracked_by_this_worker = claimed_by_this_worker and (
                pending_ray_core_identity_matches or active_ray_job_identity_matches
            )

            # Skip tasks we are actively monitoring while their monitor heartbeat is fresh.
            if tracked_by_this_worker and not is_task_stuck(task):
                continue

            # Check if task is stuck using the reconciliation logic
            if is_task_stuck(task):
                recovered_stuck = False
                adopted = False
                current = None
                retried: RayTaskExecution | None = None
                # Keep ownership transfer, LOST archival, and any retry in one
                # authoritative transaction so competing workers cannot both
                # create terminal history for the same execution identity.
                with self._authoritative_task_owner(
                    task,
                    expected_state=TaskState.RUNNING,
                    allow_takeover=True,
                    require_completion_data_match=True,
                ) as owned:
                    if owned is None:
                        if self.shutdown_requested:
                            break
                        continue
                    current = owned.execution
                    if current.completion_data is not None or not is_task_stuck(current):
                        continue
                    adopted = owned.adopted

                    if not adopted:
                        self.stdout.write(
                            self.style.WARNING(
                                f"\nTask {current.pk} appears stuck, marking as LOST"
                            )
                        )
                    else:
                        owner = task_worker_id or "unknown-worker"
                        self.stdout.write(
                            self.style.WARNING(
                                f"\nTask {current.pk} from inactive worker {owner} "
                                "appears stuck, marking as LOST"
                            )
                        )

                    if not mark_task_lost(current):
                        continue
                    recovered_stuck = True

                    retry_decision = should_retry(current, exception_type="TaskLost")
                    if retry_decision.should_retry:
                        try:
                            retried = retry_task(
                                current.pk,
                                allowed_states=(TaskState.LOST,),
                                next_attempt_at=retry_decision.next_attempt_at,
                                expected_attempt_number=current.attempt_number,
                                expected_execution_generation=current.execution_generation,
                            )
                        except RuntimeEnvSnapshotError as error:
                            diagnostic = render_console_diagnostic(error)
                            self.stdout.write(
                                self.style.ERROR(f"  Automatic retry blocked: {diagnostic}")
                            )

                if self.shutdown_requested:
                    break
                if not recovered_stuck:
                    continue
                if retried is not None:
                    task = retried
                    self.stdout.write(
                        self.style.NOTICE(
                            f"  Scheduling retry #{task.attempt_number} "
                            f"at {retry_decision.next_attempt_at}"
                        )
                    )
                elif current is not None:
                    task.__dict__.update(current.__dict__)

                stuck_count += 1
                if adopted:
                    orphan_recovered_count += 1

        if stuck_count > 0:
            self.stdout.write(self.style.WARNING(f"Detected {stuck_count} stuck task(s)"))
        if timeout_count > 0:
            self.stdout.write(self.style.WARNING(f"Detected {timeout_count} timed out task(s)"))
        if orphan_recovered_count > 0:
            self.stdout.write(
                self.style.NOTICE(
                    f"Recovered {orphan_recovered_count} task(s) from inactive or missing workers"
                )
            )

        return stuck_count + timeout_count

    def cleanup_expired_leases(self) -> int:
        """Clean up expired worker leases from other workers.

        This helps keep the TaskWorkerLease table clean by removing
        records from workers that have crashed without graceful shutdown.
        """
        from django_ray.runner.leasing import cleanup_expired_leases

        try:
            deleted_count = cleanup_expired_leases()
            if deleted_count > 0:
                self.stdout.write(
                    self.style.NOTICE(f"\nCleaned up {deleted_count} expired worker lease(s)")
                )
            return deleted_count
        except Exception:
            # Don't fail on lease cleanup errors
            self.logger.warning("Failed to cleanup expired leases", exc_info=True)
            return 0

    def _claim_orphaned_cancellation(
        self,
        task: RayTaskExecution,
        *,
        now: datetime,
    ) -> bool:
        """Fence a stale owner before taking over its cancellation."""
        current = self._take_over_task_if_owner_stale(task, now=now)
        return current is not None and current.state == TaskState.CANCELLING

    def _request_cancellation_for_task(self, task: RayTaskExecution) -> CancellationOutcome:
        """Best-effort cancellation using the backend recorded on the task."""
        ray_job_id = str(task.ray_job_id or "")
        handle = SubmissionHandle(
            ray_job_id=ray_job_id or f"ray_core:{task.pk}",
            ray_address=str(task.ray_address or ""),
            submitted_at=task.started_at or datetime.now(UTC),
        )

        if ray_job_id.startswith("raysubmit_"):
            try:
                from django_ray.runner.ray_job import RayJobRunner

                return request_remote_cancellation(RayJobRunner(), handle)
            except Exception as exc:
                return CancellationOutcome(
                    CancellationOutcomeStatus.INDETERMINATE,
                    f"Could not cancel Ray Job: {materialize_exception_message(exc)}",
                )

        if self.ray_core_runner is not None:
            pending_handle = self.ray_core_runner.get_pending_handle(
                task.pk,
                attempt_number=task.attempt_number,
                execution_generation=task.execution_generation,
            )
        else:
            pending_handle = None

        if pending_handle is not None:
            try:
                assert self.ray_core_runner is not None
                return self.ray_core_runner.cancel_pending_with_status(pending_handle)
            except Exception as exc:
                return CancellationOutcome(
                    CancellationOutcomeStatus.INDETERMINATE,
                    f"Could not cancel Ray Core task: {materialize_exception_message(exc)}",
                )

        if ray_job_id:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                "Exact Ray Core handle unavailable while recovering cancellation",
            )

        return CancellationOutcome(CancellationOutcomeStatus.NOT_APPLICABLE)

    def process_cancellations(self, queues: Sequence[str] | None = None) -> int:
        """Adopt cancellations in selected queues, or all queues when omitted."""
        if self.shutdown_requested:
            return 0
        supported_protocols = self._authoritative_protocol_range_for_scan()
        if supported_protocols is None:
            return 0
        cancelling_tasks = RayTaskExecution.objects.filter(
            state=TaskState.CANCELLING,
            execution_protocol_version__gte=supported_protocols.minimum,
            execution_protocol_version__lte=supported_protocols.maximum,
        )
        if queues is not None:
            cancelling_tasks = cancelling_tasks.filter(queue_name__in=queues)
        finalized_count = 0

        for task in cancelling_tasks:
            finalized = False
            current: RayTaskExecution | None = None
            # The exact live adopter lease and task row remain locked through
            # the bounded remote stop and terminal archive. A competing worker
            # can neither repeat the effect nor adopt between those operations.
            with self._authoritative_task_owner(
                task,
                expected_state=TaskState.CANCELLING,
                allow_takeover=True,
                require_completion_data_match=True,
            ) as owned:
                if owned is None:
                    if self.shutdown_requested:
                        break
                    continue
                current = owned.execution
                self.stdout.write(
                    self.style.WARNING(f"\nFinalizing cancellation for task {current.pk}")
                )
                cancellation = self._request_cancellation_for_task(current)
                finalized = finalize_cancellation(
                    current,
                    expected_worker_id=self.worker_id,
                    expected_attempt_number=current.attempt_number,
                    expected_execution_generation=current.execution_generation,
                    cancellation_status=CancellationStatus(cancellation.status.value),
                    cancellation_error=cancellation.message,
                )
            if self.shutdown_requested:
                break
            if finalized:
                assert current is not None
                task.__dict__.update(current.__dict__)
                # A stale completion callback cannot overwrite this row because
                # finalization was locked and conditional on exact ownership.
                self.active_tasks.pop(task.pk, None)
                self.active_task_identities.pop(task.pk, None)
                finalized_count += 1
                self.stdout.write(self.style.SUCCESS(f"  Task {task.pk} cancelled"))

        return finalized_count

    def _prepare_shutdown_handoff(self) -> None:
        """Make in-flight work safe for the next worker before disconnecting.

        Synchronous execution is allowed to finish because it is already
        running in this process.  Ray Job submissions are durable by ID, so
        they remain running and are released for another worker to reconcile.
        Ray Core ObjectRefs belong to this driver; request cancellation and
        persist ``CANCELLING`` so a subsequent worker can finalize the row.
        """
        # Once immutable lease ownership is lost, even a matching worker ID is
        # insufficient authority: a replacement process may already own it.
        # Leave durable task rows untouched for a live lease holder to recover.
        if self.execution_mode == "sync" or self.lease_ownership_lost:
            return

        # Ray Core work cannot be recovered after this driver's Ray connection
        # is closed.  Ask Ray to stop it, then persist the cancellation intent.
        if self.execution_mode in ("local", "cluster") and self.ray_core_runner:
            for pending_handle in self.ray_core_runner.pending_task_handles:
                if self.lease_ownership_lost:
                    break
                try:
                    task = RayTaskExecution.objects.get(pk=pending_handle.task_pk)
                except RayTaskExecution.DoesNotExist:
                    continue
                if (
                    task.state != TaskState.RUNNING
                    or task.attempt_number != pending_handle.attempt_number
                    or task.execution_generation != pending_handle.execution_generation
                ):
                    continue
                transitioned = False
                with self._authoritative_task_owner(
                    task,
                    expected_state=TaskState.RUNNING,
                    allow_takeover=False,
                    require_completion_data_match=True,
                ) as owned:
                    if owned is not None:
                        current = owned.execution
                        try:
                            outcome = self.ray_core_runner.cancel_pending_with_status(
                                pending_handle
                            )
                        except Exception as exc:
                            outcome = CancellationOutcome(
                                CancellationOutcomeStatus.INDETERMINATE,
                                "Ray Core shutdown cancellation failed: "
                                f"{materialize_exception_message(exc)}",
                            )
                        current.state = TaskState.CANCELLING
                        current.cancellation_status = CancellationStatus(outcome.status.value)
                        current.cancellation_error = outcome.message
                        current.save(
                            update_fields=[
                                "state",
                                "cancellation_status",
                                "cancellation_error",
                            ]
                        )
                        transitioned = True
                if transitioned:
                    self.stdout.write(
                        self.style.WARNING(f"  Task {task.pk} marked CANCELLING during shutdown")
                    )
        # Ray Jobs continue independently of this process.  Drop ownership so
        # another worker can adopt and reconcile their persisted job IDs.
        if self.execution_mode == "ray":
            for task_pk, ray_job_id in list(self.active_tasks.items()):
                if self.lease_ownership_lost:
                    break
                identity = self.active_task_identities.get(task_pk)
                handed_off = False
                if identity is not None:
                    try:
                        task = RayTaskExecution.objects.get(pk=task_pk)
                    except RayTaskExecution.DoesNotExist:
                        task = None
                    if task is not None and (
                        str(task.ray_job_id or "") == ray_job_id
                        and (task.attempt_number, task.execution_generation) == identity
                    ):
                        with self._authoritative_task_owner(
                            task,
                            expected_state=TaskState.RUNNING,
                            allow_takeover=False,
                            require_completion_data_match=True,
                        ) as owned:
                            if owned is not None:
                                current = owned.execution
                                current.claimed_by_worker = None
                                current.last_heartbeat_at = datetime.now(UTC)
                                current.save(
                                    update_fields=["claimed_by_worker", "last_heartbeat_at"]
                                )
                                handed_off = True
                if handed_off:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"  Ray Job task {task_pk} handed off for continued monitoring"
                        )
                    )
                self.active_tasks.pop(task_pk, None)
                self.active_task_identities.pop(task_pk, None)

    def shutdown(self) -> None:
        """Perform graceful shutdown."""
        cleanup_failed = False
        try:
            self._prepare_shutdown_handoff()
        except Exception:
            # A database outage during handoff must not skip the independently
            # useful lease-release and Ray-disconnect cleanup phases.
            self.logger.error("worker shutdown handoff failed", exc_info=True)
            self.stdout.write(
                self.style.ERROR("  Failed to prepare task handoff; continuing cleanup")
            )
            cleanup_failed = True
            if self.shutdown_exit_code is None:
                self.shutdown_exit_code = 1
        # Mark worker lease as inactive to signal we're gone
        if self.lease_identity is not None:
            try:
                from django_ray.runner.leasing import release_lease

                if release_lease(self.lease_identity):
                    self.stdout.write("  Lease released (marked inactive)")
                else:
                    cleanup_failed = True
                    if self.shutdown_exit_code is None:
                        self.shutdown_exit_code = 1
                    self.stdout.write("  Lease release skipped (ownership fence did not match)")
            except Exception:
                cleanup_failed = True
                if self.shutdown_exit_code is None:
                    self.shutdown_exit_code = 1
                self.logger.error("worker lease release failed", exc_info=True)
                self.stdout.write("  Failed to release lease; see worker logs")

        # Disconnect from Ray cluster
        if self.execution_mode in ("local", "cluster"):
            try:
                import ray

                if ray.is_initialized():
                    ray.shutdown()
                    self.stdout.write("  Ray connection closed")
            except Exception:
                cleanup_failed = True
                if self.shutdown_exit_code is None:
                    self.shutdown_exit_code = 1
                self.logger.error("worker Ray disconnect failed", exc_info=True)
                self.stdout.write("  Failed to close Ray connection; see worker logs")

        failed_without_signal = self.shutdown_exit_code is not None and self.shutdown_signal is None
        if cleanup_failed or failed_without_signal:
            self.stdout.write(
                self.style.WARNING(f"\nWorker {self.worker_id} shut down with errors")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"\nWorker {self.worker_id} shut down cleanly"))
