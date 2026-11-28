"""Queue parsing, lease fencing, and durable task claiming for django-ray workers."""

from __future__ import annotations

import os
import socket
import time as stdlib_time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction

from django_ray import __version__ as default_django_ray_version
from django_ray import lease_policy as default_lease_policy
from django_ray.conf.settings import get_settings as default_get_settings
from django_ray.execution_protocol import (
    MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    WORKER_CAPABILITY_SCHEMA_VERSION,
    ExecutionProtocolRange,
    explicit_worker_protocol_range,
)
from django_ray.lifecycle import (
    expire_queued_tasks as default_expire_queued_tasks,
)
from django_ray.lifecycle import (
    promote_legacy_ray_target as default_promote_legacy_ray_target,
)
from django_ray.management.diagnostics import render_console_diagnostic
from django_ray.models import TaskState
from django_ray.runner.leasing import (
    WorkerLeaseIdentity,
    expired_lease_q,
    is_worker_id_primary_key_collision,
    live_lease_freshness_q,
)
from django_ray.runner.leasing import (
    generate_worker_id as default_generate_worker_id,
)
from django_ray.worker import (
    BUILTIN_DATETIME,
    CLAIM_UPDATE_FIELDS,
    DB_BACKOFF_INITIAL_SECONDS,
    DB_BACKOFF_MAX_SECONDS,
    DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS,
    LEASE_QUEUE_NAME_MAX_LENGTH,
    WORKER_ID_ALLOCATION_ATTEMPTS,
    OwnedTask,
    RayBackendQueueConfiguration,
    RayJobQueueAffinity,
    WorkerIdReservedByTaskError,
    close_old_db_connections,
    command_dep,
    is_transient_db_error,
    lease_model,
    task_model,
)


def validate_execution_mode_configuration(command: Any, settings: dict[str, Any]) -> None:
    """Fail before lease creation when Ray Job request storage is unusable."""
    if command.execution_mode != "ray":
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


def get_default_execution_mode(settings: Mapping[str, Any]) -> tuple[str, str | None]:
    """Resolve default worker mode from settings when no CLI mode flag is set."""
    runner = settings.get("RUNNER", "ray_core")
    if runner == "ray_core":
        ray_address = settings.get("RAY_ADDRESS")
        if ray_address and ray_address != "auto":
            return "cluster", str(ray_address)
        return "local", None

    return "ray", None


def requested_worker_family(target: Any, options: Mapping[str, Any]) -> str:
    """Return the process-wide runner family selected by CLI flags/settings."""
    if options.get("sync"):
        return "sync"
    if options.get("local") or options.get("cluster"):
        return "ray_core"
    if options.get("ray_job"):
        return "ray_job"
    get_settings_fn = command_dep(target, "get_settings", default_get_settings)
    if get_settings_fn().get("RUNNER", "ray_core") == "ray_core":
        return "ray_core"
    return "ray_job"


def active_worker_family(command: Any) -> str:
    """Return the active runner family for an initialized worker command."""
    if command.execution_mode == "ray":
        return "ray_job"
    if command.execution_mode == "sync":
        return "sync"
    return "ray_core"


def load_ray_backend_queue_configuration(command: Any) -> RayBackendQueueConfiguration:
    """Resolve Ray backend queues and fail closed on ambiguous affinity policy."""
    if command._ray_backend_queue_configuration is not None:
        return command._ray_backend_queue_configuration

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
    worker_settings = command_dep(command, "get_settings", default_get_settings)()

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
            raise CommandError(f"TASKS backend {alias!r} OPTIONS['RAY_JOB_ONLY'] must be a boolean")
        ray_target = options.get("RAY_ADDRESS", worker_settings.get("RAY_ADDRESS", "auto"))
        if not isinstance(ray_target, str) or not ray_target.strip():
            raise CommandError(f"TASKS backend {alias!r} RAY_ADDRESS must be a non-empty string")

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
                raise CommandError(f"TASKS backend {alias!r} QUEUES must contain non-empty strings")
        if isinstance(raw_queues, set | frozenset):
            queue_values.sort()

        aliases.append(alias)
        queues_by_alias[alias] = tuple(queue_values)
        ray_targets_by_alias[alias] = ray_target
        for queue_name in queue_values:
            if queue_name not in seen_queues:
                configured_queues.append(queue_name)
                seen_queues.add(queue_name)

    configuration = RayBackendQueueConfiguration(
        aliases=tuple(aliases),
        queues=tuple(configured_queues),
        queues_by_alias=queues_by_alias,
        ray_targets_by_alias=ray_targets_by_alias,
    )
    command._ray_backend_queue_configuration = configuration
    return configuration


def load_ray_job_queue_affinity(command: Any) -> RayJobQueueAffinity:
    """Read only aliases that explicitly opt queues into Ray Job ownership."""
    if command._ray_job_queue_affinity is not None:
        return command._ray_job_queue_affinity

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
            raise CommandError(f"TASKS backend {alias!r} OPTIONS['RAY_JOB_ONLY'] must be a boolean")
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
            raise CommandError(f"RAY_JOB_ONLY TASKS backend {alias!r} does not resolve to a class")
        if not issubclass(backend_class, RayTaskBackend):
            continue

        raw_queues = backend_config.get("QUEUES", [DEFAULT_TASK_QUEUE_NAME])
        if isinstance(raw_queues, (str, bytes)) or not isinstance(
            raw_queues, Sequence | set | frozenset
        ):
            raise CommandError(
                f"RAY_JOB_ONLY TASKS backend {alias!r} QUEUES must be a collection of queue names"
            )
        queue_values = list(raw_queues)
        if not queue_values:
            raise CommandError(
                f"RAY_JOB_ONLY TASKS backend {alias!r} must declare at least one queue"
            )
        for queue_name in queue_values:
            if not isinstance(queue_name, str) or not queue_name.strip():
                raise CommandError(
                    f"RAY_JOB_ONLY TASKS backend {alias!r} QUEUES must contain non-empty strings"
                )
            restricted_aliases.setdefault(queue_name, []).append(alias)

    affinity = RayJobQueueAffinity(
        aliases_by_queue={queue: tuple(aliases) for queue, aliases in restricted_aliases.items()}
    )
    command._ray_job_queue_affinity = affinity
    return affinity


def enforce_queue_affinity(
    command: Any,
    queues: Sequence[str],
    *,
    worker_family: str,
    allow_filter: bool,
) -> list[str]:
    """Keep Ray Job-only queues away from Ray Core and synchronous workers."""
    selected = list(queues)
    if not selected or any(not isinstance(queue, str) or not queue.strip() for queue in selected):
        raise CommandError("Queue selection must contain at least one non-empty queue name")
    affinity = command._load_ray_job_queue_affinity()
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
        command.stdout.write(
            command.style.NOTICE(
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


def parse_queues(command: Any, options: dict[str, Any]) -> list[str]:
    """Parse queue arguments from command options."""
    if options.get("all_queues"):
        configuration = command._load_ray_backend_queue_configuration()
        if not configuration.aliases:
            raise CommandError("--all-queues found no TASKS backend using RayTaskBackend")
        for alias in configuration.aliases:
            if not configuration.queues_by_alias[alias]:
                raise CommandError(
                    f"TASKS backend {alias!r} has no enumerable QUEUES; "
                    "use --queue or --queues explicitly"
                )

        worker_family = command._requested_worker_family(options)
        configured_queues = command._enforce_queue_affinity(
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
        command.stdout.write(
            command.style.NOTICE(
                "Processing all configured django-ray queues "
                f"from {list(configuration.aliases)}: {configured_queues}"
            )
        )
        return configured_queues

    if options.get("queues") is not None:
        raw_selected = options["queues"]
        selected = [q.strip() if isinstance(q, str) else q for q in raw_selected]
        if selected and all(isinstance(q, str) and q for q in selected):
            selected = list(dict.fromkeys(selected))
        return command._enforce_queue_affinity(
            selected,
            worker_family=command._requested_worker_family(options),
            allow_filter=False,
        )

    queue_arg = options.get("queue")
    if queue_arg is not None:
        if "," in queue_arg:
            selected = list(dict.fromkeys(q.strip() for q in queue_arg.split(",") if q.strip()))
        else:
            selected = [queue_arg.strip()]
        return command._enforce_queue_affinity(
            selected,
            worker_family=command._requested_worker_family(options),
            allow_filter=False,
        )

    return command._enforce_queue_affinity(
        ["default"],
        worker_family=command._requested_worker_family(options),
        allow_filter=False,
    )


def create_lease(command: Any, queue: str) -> None:
    """Acquire a new worker lease without adopting an existing row."""
    from django.utils import timezone

    from django_ray.lease_policy import LeasePolicyError

    lp = command_dep(command, "lease_policy", default_lease_policy)
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    time_mod = command_dep(command, "time", stdlib_time)
    gen_worker_id = command_dep(command, "generate_worker_id", default_generate_worker_id)
    version = command_dep(command, "django_ray_version", default_django_ray_version)
    lease_cls = lease_model(command)
    task_cls = task_model(command)

    normalized_queue = queue[:LEASE_QUEUE_NAME_MAX_LENGTH]
    command.lease_queue_name = normalized_queue
    if command.lease_identity is not None:
        if not command._recreate_lease(queue_name=normalized_queue):
            raise CommandError("Worker lease ownership was lost")
        return

    hostname = socket.gethostname()
    pid = os.getpid()
    for attempt in range(WORKER_ID_ALLOCATION_ATTEMPTS):
        started_at = timezone.now()
        identity = WorkerLeaseIdentity(
            worker_id=command.worker_id,
            hostname=hostname,
            pid=pid,
            started_at=started_at,
        )
        try:
            with transaction.atomic():
                policy = lp.lock_lease_policy()
                lease = lease_cls.objects.create(
                    **identity.database_filters(),
                    queue_name=normalized_queue,
                    capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
                    django_ray_version=version,
                    min_supported_execution_protocol_version=(
                        MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION
                    ),
                    max_supported_execution_protocol_version=(
                        MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION
                    ),
                    legacy_admission_token=None,
                    last_heartbeat_at=started_at,
                    lease_expires_at=started_at + policy.duration,
                    lease_policy_revision=policy.revision,
                    is_active=True,
                    stopped_at=None,
                )
                if dt is BUILTIN_DATETIME:
                    lease_cls.objects.filter(**identity.database_filters()).update(
                        last_heartbeat_at=lp.db_clock_now(),
                        lease_expires_at=lp.db_clock_plus(policy.duration),
                    )
                if task_cls.objects.filter(
                    claimed_by_worker=command.worker_id,
                    state__in=(TaskState.RUNNING, TaskState.CANCELLING),
                ).exists():
                    raise WorkerIdReservedByTaskError
        except LeasePolicyError as error:
            raise CommandError(error.reason) from None
        except WorkerIdReservedByTaskError:
            if attempt + 1 < WORKER_ID_ALLOCATION_ATTEMPTS:
                command._set_worker_id(gen_worker_id())
            continue
        except IntegrityError as error:
            if not is_worker_id_primary_key_collision(error):
                diagnostic = render_console_diagnostic(error)
                raise CommandError(f"Could not create worker lease: {diagnostic}") from None
            if attempt + 1 < WORKER_ID_ALLOCATION_ATTEMPTS:
                command._set_worker_id(gen_worker_id())
            continue
        except Exception as error:
            diagnostic = render_console_diagnostic(error)
            raise CommandError(f"Could not create worker lease: {diagnostic}") from None

        command.lease = lease
        command.lease_identity = identity
        command._lease_policy_seconds = policy.seconds
        command._lease_policy_revision = policy.revision
        command._last_successful_heartbeat_monotonic = time_mod.monotonic()
        return

    raise CommandError("Could not allocate a unique worker lease after bounded retries") from None


def recreate_lease(command: Any, *, queue_name: str | None = None) -> bool:
    """Renew only a still-live exact lease identity."""
    identity = command.lease_identity
    if identity is None:
        command._request_shutdown_for_lease_loss("worker lease was never acquired")
        return False

    time_mod = command_dep(command, "time", stdlib_time)
    lease_cls = lease_model(command)

    try:
        with transaction.atomic():
            updated = command._lock_authoritative_leases(
                source_worker_id=command.worker_id,
                allow_takeover=False,
            )
            if not updated:
                return False
            if queue_name is not None and updated.queue_name != queue_name:
                lease_cls.objects.filter(**identity.database_filters()).update(
                    queue_name=queue_name
                )
                updated.queue_name = queue_name
                if command.lease is not None:
                    command.lease.queue_name = queue_name
        command._last_successful_heartbeat_monotonic = time_mod.monotonic()
        command.stdout.write(command.style.SUCCESS(f"  Lease restored: {command.worker_id}"))
        return True
    except Exception as error:
        command._request_shutdown_for_lease_loss(
            "worker lease restoration failed",
            error=error,
        )
        return False


def request_shutdown_for_lease_loss(
    command: Any,
    reason: str,
    *,
    error: BaseException | None = None,
) -> None:
    """Fail closed when this process cannot prove lease ownership."""
    if not command.shutdown_requested:
        command.stdout.write(
            command.style.ERROR("\nWorker lease ownership lost; shutting down before claims")
        )
    if error is None:
        command.logger.error(reason)
    else:
        command.logger.error(
            reason,
            exc_info=(type(error), error, error.__traceback__),
        )
    command.lease_ownership_lost = True
    command.active_tasks.clear()
    command.active_task_identities.clear()
    command.shutdown_requested = True
    if command.shutdown_exit_code is None or command.shutdown_exit_code == 0:
        command.shutdown_exit_code = 1
    command._shutdown_event.set()


def update_lease_heartbeat(command: Any) -> bool:
    """Update lease heartbeat with bounded transient DB retry within lease TTL."""
    identity = command.lease_identity
    if identity is None:
        command._request_shutdown_for_lease_loss("worker lease was never acquired")
        return False

    time_mod = command_dep(command, "time", stdlib_time)
    max_attempts = 3
    retry_delay = DB_BACKOFF_INITIAL_SECONDS
    for attempt in range(max_attempts):
        try:
            with transaction.atomic():
                updated = command._lock_authoritative_leases(
                    source_worker_id=command.worker_id,
                    allow_takeover=False,
                )
        except Exception as error:
            if is_transient_db_error(error) and command._within_transient_db_recovery_budget():
                close_old_db_connections(command)
                command.logger.warning(
                    "Transient database error during lease heartbeat; retrying within lease TTL",
                    exc_info=True,
                )
                if attempt + 1 < max_attempts:
                    time_mod.sleep(min(retry_delay, 1.0))
                    retry_delay = min(retry_delay * 2.0, DB_BACKOFF_MAX_SECONDS)
                    continue
                return False
            command._request_shutdown_for_lease_loss(
                "worker lease heartbeat failed",
                error=error,
            )
            return False
        if updated:
            command._last_successful_heartbeat_monotonic = time_mod.monotonic()
            command._db_backoff_delay = DB_BACKOFF_INITIAL_SECONDS
            command._heartbeat_retry_after = 0.0
            return True
        return False
    return False


def resolve_active_lease_policy(command: Any) -> Any:
    """Validate local lease duration against stamped policy without locking."""
    lp = command_dep(command, "lease_policy", default_lease_policy)
    configured = lp.configured_lease_seconds()
    if command._lease_policy_seconds is not None and command._lease_policy_revision is not None:
        if configured != command._lease_policy_seconds:
            raise lp.LeasePolicyError("lease_policy_mismatch")
        return lp.LeasePolicy(command._lease_policy_seconds, command._lease_policy_revision)
    policy = lp.read_lease_policy()
    command._lease_policy_seconds = policy.seconds
    command._lease_policy_revision = policy.revision
    return policy


def own_lease_freshness_q(
    duration: timedelta,
    *,
    validation_time: datetime | None = None,
) -> Any:
    """Build freshness filter using DB clock in production or mocked clock in tests."""
    from django.db.models import Q

    if validation_time is not None:
        cutoff = validation_time - duration
        return Q(last_heartbeat_at__gte=cutoff)
    return live_lease_freshness_q(duration)


def preflight_own_lease_active(command: Any, *, mark_lost: bool = True) -> bool:
    """Read-only non-locking lease check before external spill/cancel I/O."""
    if command.lease_ownership_lost:
        return False
    identity = command.lease_identity
    if identity is None:
        return mark_lost
    from django.db.models import Exists, OuterRef

    lp = command_dep(command, "lease_policy", default_lease_policy)
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    lease_cls = lease_model(command)

    try:
        policy = command._resolve_active_lease_policy()
    except lp.LeasePolicyError as error:
        if is_transient_db_error(error) and command._within_transient_db_recovery_budget():
            if mark_lost:
                raise
            return False
        if mark_lost:
            command._request_shutdown_for_lease_loss(error.reason)
        return False

    validation_time = None if dt is BUILTIN_DATETIME else dt.now(UTC)
    freshness_q = command._own_lease_freshness_q(
        policy.duration,
        validation_time=validation_time,
    )
    live = (
        lease_cls.objects.filter(
            freshness_q,
            **identity.database_filters(),
            is_active=True,
            stopped_at__isnull=True,
        )
        .alias(
            _lease_policy_valid=Exists(
                lp.policy_rows(matching=True, revision=OuterRef("lease_policy_revision"))
            )
        )
        .filter(_lease_policy_valid=True)
        .exists()
    )
    if not live:
        if mark_lost:
            command._request_shutdown_for_lease_loss(
                "worker lease expired, became inactive, or disappeared before result persistence"
            )
        return False
    return True


def explicit_protocol_range(lease: Any) -> ExecutionProtocolRange | None:
    """Read the immutable protocol range from one locked explicit lease."""
    return explicit_worker_protocol_range(
        capability_schema_version=int(lease.capability_schema_version),
        legacy_admission_token_present=lease.legacy_admission_token_id is not None,
        minimum=lease.min_supported_execution_protocol_version,
        maximum=lease.max_supported_execution_protocol_version,
    )


def lock_authoritative_leases(
    command: Any,
    *,
    source_worker_id: str | None,
    allow_takeover: bool,
    renew_heartbeat: bool = True,
    required_execution_protocol_version: int | None = None,
) -> Any:
    """Lock involved leases and prove this process still owns its identity."""
    identity = command.lease_identity
    if identity is None:
        command._request_shutdown_for_lease_loss("worker lease was never acquired")
        return None

    from django.db.models import Exists, F, OuterRef, Q

    lp = command_dep(command, "lease_policy", default_lease_policy)
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    lease_cls = lease_model(command)

    try:
        policy = command._resolve_active_lease_policy()
    except lp.LeasePolicyError as error:
        if not (is_transient_db_error(error) and command._within_transient_db_recovery_budget()):
            command._request_shutdown_for_lease_loss(error.reason)
        raise

    is_self_only = source_worker_id is None or source_worker_id == command.worker_id
    use_db_clock = dt is BUILTIN_DATETIME
    policy_exists = Exists(
        lp.policy_rows(matching=True, revision=OuterRef("lease_policy_revision"))
    )

    if is_self_only and connection.features.has_select_for_update:
        # Acquire the row lock BEFORE measuring freshness so waiting behind
        # another transaction cannot validate a lease against a pre-wait clock.
        locked_lease = (
            lease_cls.objects.select_for_update().filter(**identity.database_filters()).first()
        )
        if locked_lease is None:
            command._request_shutdown_for_lease_loss(
                "worker lease expired, became inactive, or disappeared before task mutation"
            )
            return None
        validation_time = None if use_db_clock else dt.now(UTC)
        freshness_q = command._own_lease_freshness_q(
            policy.duration,
            validation_time=validation_time,
        )
        live_lease = (
            lease_cls.objects.filter(
                freshness_q,
                **identity.database_filters(),
                is_active=True,
                stopped_at__isnull=True,
            )
            .alias(_lease_policy_valid=policy_exists)
            .filter(_lease_policy_valid=True)
            .first()
        )
        if live_lease is None:
            command._request_shutdown_for_lease_loss(
                "worker lease expired, became inactive, or disappeared before task mutation"
            )
            return None
        if renew_heartbeat:
            if use_db_clock:
                lease_cls.objects.filter(**identity.database_filters()).update(
                    last_heartbeat_at=lp.db_clock_now(),
                    lease_expires_at=lp.db_clock_plus(policy.duration),
                )
            else:
                assert validation_time is not None
                lease_cls.objects.filter(**identity.database_filters()).update(
                    last_heartbeat_at=validation_time,
                    lease_expires_at=validation_time + policy.duration,
                )
                if command.lease is not None:
                    command.lease.last_heartbeat_at = validation_time
                    command.lease.lease_expires_at = validation_time + policy.duration
        if required_execution_protocol_version is not None:
            supported_protocols = command._explicit_protocol_range(live_lease)
            if supported_protocols is None or not supported_protocols.supports(
                required_execution_protocol_version
            ):
                return None
        return live_lease

    lease_worker_ids = sorted(
        worker_id for worker_id in {identity.worker_id, source_worker_id} if worker_id is not None
    )
    if connection.features.has_select_for_update:
        list(
            lease_cls.objects.select_for_update()
            .filter(worker_id__in=lease_worker_ids)
            .order_by("worker_id")
        )
    else:
        fenced = lease_cls.objects.filter(**identity.database_filters()).update(
            last_heartbeat_at=F("last_heartbeat_at")
        )
        if not fenced:
            command._request_shutdown_for_lease_loss(
                "worker lease expired, became inactive, or disappeared before task mutation"
            )
            return None

    validation_time = None if use_db_clock else dt.now(UTC)
    freshness_q = command._own_lease_freshness_q(
        policy.duration,
        validation_time=validation_time,
    )
    own_queryset = (
        lease_cls.objects.filter(
            freshness_q,
            **identity.database_filters(),
            is_active=True,
            stopped_at__isnull=True,
        )
        .alias(_lease_policy_valid=policy_exists)
        .filter(_lease_policy_valid=True)
    )
    if renew_heartbeat:
        if use_db_clock:
            owns_live_lease = own_queryset.update(
                last_heartbeat_at=lp.db_clock_now(),
                lease_expires_at=lp.db_clock_plus(policy.duration),
            )
        else:
            assert validation_time is not None
            owns_live_lease = own_queryset.update(
                last_heartbeat_at=validation_time,
                lease_expires_at=validation_time + policy.duration,
            )
        if not owns_live_lease:
            command._request_shutdown_for_lease_loss(
                "worker lease expired, became inactive, or disappeared before task mutation"
            )
            return None
        if validation_time is not None and command.lease is not None:
            command.lease.last_heartbeat_at = validation_time
            command.lease.lease_expires_at = validation_time + policy.duration
        live_lease = own_queryset.first()
    else:
        live_lease = own_queryset.first()

    if live_lease is None:
        command._request_shutdown_for_lease_loss(
            "worker lease expired, became inactive, or disappeared before task mutation"
        )
        return None

    if required_execution_protocol_version is not None:
        supported_protocols = command._explicit_protocol_range(live_lease)
        if supported_protocols is None or not supported_protocols.supports(
            required_execution_protocol_version
        ):
            return None

    if is_self_only:
        return live_lease
    if not allow_takeover:
        return None

    assert source_worker_id is not None
    if use_db_clock:
        source_live_q = live_lease_freshness_q(policy.duration)
        source_expired_q = expired_lease_q(policy.duration)
        stopped_at_value: Any = lp.db_clock_now()
    else:
        assert validation_time is not None
        cutoff = validation_time - policy.duration
        source_live_q = Q(last_heartbeat_at__gte=cutoff)
        source_expired_q = Q(last_heartbeat_at__lt=cutoff)
        stopped_at_value = validation_time

    if lease_cls.objects.filter(
        source_live_q,
        worker_id=source_worker_id,
        is_active=True,
    ).exists():
        return None

    lease_cls.objects.filter(
        source_expired_q,
        worker_id=source_worker_id,
        is_active=True,
    ).update(
        is_active=False,
        stopped_at=stopped_at_value,
    )
    takeover_allowed = not lease_cls.objects.filter(
        worker_id=source_worker_id,
        is_active=True,
    ).exists()
    return live_lease if takeover_allowed else None


def preliminary_takeover_protocol_range(command: Any) -> ExecutionProtocolRange | None:
    """Read immutable candidate capability without locking a source lease."""
    identity = command.lease_identity
    if identity is None:
        command._request_shutdown_for_lease_loss("worker lease was never acquired")
        return None

    from django.db.models import Exists, OuterRef

    lp = command_dep(command, "lease_policy", default_lease_policy)
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    lease_cls = lease_model(command)

    try:
        policy = command._resolve_active_lease_policy()
    except lp.LeasePolicyError as error:
        command._request_shutdown_for_lease_loss(error.reason)
        return None
    validation_time = None if dt is BUILTIN_DATETIME else dt.now(UTC)
    freshness_q = command._own_lease_freshness_q(
        policy.duration,
        validation_time=validation_time,
    )
    lease = (
        lease_cls.objects.filter(
            freshness_q,
            **identity.database_filters(),
            is_active=True,
            stopped_at__isnull=True,
        )
        .alias(
            _lease_policy_valid=Exists(
                lp.policy_rows(matching=True, revision=OuterRef("lease_policy_revision"))
            )
        )
        .filter(_lease_policy_valid=True)
        .only(
            "capability_schema_version",
            "legacy_admission_token",
            "min_supported_execution_protocol_version",
            "max_supported_execution_protocol_version",
        )
        .first()
    )
    if lease is None:
        command._request_shutdown_for_lease_loss(
            "worker lease expired, became inactive, disappeared, or was replaced before takeover"
        )
        return None
    return command._explicit_protocol_range(lease)


def authoritative_protocol_range_for_scan(command: Any) -> ExecutionProtocolRange | None:
    """Read this process's exact durable capability before a task scan."""
    with transaction.atomic():
        lease = command._lock_authoritative_leases(
            source_worker_id=command.worker_id,
            allow_takeover=False,
            renew_heartbeat=False,
        )
        if lease is None:
            return None
        return command._explicit_protocol_range(lease)


@contextmanager
def authoritative_task_owner(
    command: Any,
    snapshot: Any,
    *,
    expected_state: str,
    allow_takeover: bool,
    require_completion_data_match: bool = False,
) -> Generator[OwnedTask | None, None, None]:
    """Yield the exact current execution while lease and task locks are held."""
    source_worker_id = str(snapshot.claimed_by_worker) if snapshot.claimed_by_worker else None
    required_protocol = int(snapshot.execution_protocol_version)
    task_cls = task_model(command)
    version = command_dep(command, "django_ray_version", default_django_ray_version)

    if source_worker_id is not None and source_worker_id != command.worker_id:
        preliminary_protocols = command._preliminary_takeover_protocol_range()
        if preliminary_protocols is None or not preliminary_protocols.supports(required_protocol):
            yield None
            return

    with transaction.atomic():
        lease = command._lock_authoritative_leases(
            source_worker_id=source_worker_id,
            allow_takeover=allow_takeover,
            required_execution_protocol_version=required_protocol,
        )
        if lease is None:
            yield None
            return

        if source_worker_id != command.worker_id and not allow_takeover:
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

        current = task_cls.objects.select_for_update().filter(**task_filters).first()
        if current is None:
            yield None
            return

        supported_protocols = command._explicit_protocol_range(lease)
        if supported_protocols is None or not supported_protocols.supports(
            int(current.execution_protocol_version)
        ):
            yield None
            return

        adopted = source_worker_id != command.worker_id
        if adopted:
            current.claimed_by_worker = command.worker_id
            current.managed_with_django_ray_version = version
            current.save(
                update_fields=[
                    "claimed_by_worker",
                    "managed_with_django_ray_version",
                ]
            )

        yield OwnedTask(
            execution=current,
            adopted=adopted,
            supported_protocols=supported_protocols,
        )


def take_over_task_if_owner_stale(
    command: Any,
    task: Any,
    *,
    now: datetime,
) -> Any:
    """Validate this lease and atomically fence a stale task owner."""
    del now
    current: Any = None
    with command._authoritative_task_owner(
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


def claim_and_process_tasks(command: Any, queues: Sequence[str], concurrency: int) -> int:
    """Claim and submit tasks for execution."""
    if command.shutdown_requested or getattr(command, "drain_mode", False):
        return 0
    lease_identity = command.lease_identity
    if lease_identity is None:
        command._request_shutdown_for_lease_loss("worker lease was never acquired")
        return 0

    queues = command._enforce_queue_affinity(
        queues,
        worker_family=command._active_worker_family(),
        allow_filter=False,
    )

    ray_core_pending = command.ray_core_runner.pending_count if command.ray_core_runner else 0
    active_count = len(command.active_tasks) + ray_core_pending
    available_slots = concurrency - active_count
    if (command.sync_mode or command.execution_mode == "sync") and available_slots > 1:
        available_slots = 1
    ray_core_claim_ready = (
        command.execution_mode not in ("local", "cluster") or command._is_ray_core_initialized()
    )

    from django.db.models import Q

    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    time_mod = command_dep(command, "time", stdlib_time)
    task_cls = task_model(command)
    expire_fn = command_dep(command, "expire_queued_tasks", default_expire_queued_tasks)
    promote_fn = command_dep(
        command, "promote_legacy_ray_target", default_promote_legacy_ray_target
    )
    version = command_dep(command, "django_ray_version", default_django_ray_version)

    last_expiry_sweep = float(getattr(command, "_last_expiry_sweep_monotonic", 0.0))
    expiry_sweep_interval = float(
        getattr(command, "expiry_sweep_interval", DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS)
    )
    sweep_monotonic = time_mod.monotonic()
    expiry_sweep_due = (
        last_expiry_sweep <= 0.0 or (sweep_monotonic - last_expiry_sweep) >= expiry_sweep_interval
    )

    last_hb = float(getattr(command, "_last_successful_heartbeat_monotonic", 0.0))
    hb_interval = float(getattr(command, "_lease_heartbeat_interval_seconds", 0.0))
    if hb_interval <= 0.0:
        from django_ray.runner.leasing import (
            get_heartbeat_interval as default_get_heartbeat_interval,
        )

        get_hb_interval = command_dep(
            command, "get_heartbeat_interval", default_get_heartbeat_interval
        )
        hb_interval = max(1.0, float(get_hb_interval().total_seconds()))
    heartbeat_due = last_hb <= 0.0 or (sweep_monotonic - last_hb) >= (hb_interval / 2.0)

    with transaction.atomic():
        lease = command._lock_authoritative_leases(
            source_worker_id=command.worker_id,
            allow_takeover=False,
            renew_heartbeat=heartbeat_due,
        )
        if lease is None:
            return 0
        supported_protocols = command._explicit_protocol_range(lease)
        if supported_protocols is None:
            return 0

        if expiry_sweep_due:
            sweep_now = dt.now(UTC)
            expired = expire_fn(
                queues,
                now=sweep_now,
                limit=100,
                supported_protocols=supported_protocols,
            )
            command._last_expiry_sweep_monotonic = 0.0 if len(expired) >= 100 else sweep_monotonic
        else:
            expired = ()
        if expired:
            command.stdout.write(
                command.style.WARNING(f"  Expired {len(expired)} stale queued task(s)")
            )
        if available_slots <= 0 or not ray_core_claim_ready:
            return len(expired)

        selection_now = dt.now(UTC)

        tasks = list(
            task_cls.objects.select_for_update(skip_locked=True)
            .filter(
                state=TaskState.QUEUED,
                queue_name__in=queues,
                execution_protocol_version__gte=supported_protocols.minimum,
                execution_protocol_version__lte=supported_protocols.maximum,
            )
            .filter(Q(run_after__isnull=True) | Q(run_after__lte=selection_now))
            .filter(Q(queue_deadline_at__isnull=True) | Q(queue_deadline_at__gt=selection_now))
            .order_by("-priority", "created_at", "pk")[:available_slots]
        )

        claim_now = selection_now
        if tasks:
            claim_now = dt.now(UTC)
            tasks = [
                task
                for task in tasks
                if task.queue_deadline_at is None or task.queue_deadline_at > claim_now
            ]

        for task in tasks:
            task.state = TaskState.RUNNING
            task.started_at = claim_now
            task.last_heartbeat_at = claim_now
            task.claimed_by_worker = command.worker_id
            task.managed_with_django_ray_version = version
            task.execution_generation = int(task.execution_generation) + 1
            task.error_message = None
            task.error_traceback = None
            task.cancellation_status = None
            task.cancellation_error = None
            task.completion_data = None
            task.progress_data = None
            task.workflow_progress_summary_json = None
            task.workflow_run_id = None
            task.workflow_plan_selection = None
            promote_fn(task)
            task.ray_job_id = None
            task.ray_job_request_reference = None
            task.ray_address = None
        if len(tasks) == 1:
            tasks[0].save(update_fields=list(CLAIM_UPDATE_FIELDS))
        elif tasks:
            task_cls.objects.bulk_update(tasks, list(CLAIM_UPDATE_FIELDS))
        if tasks:
            from django_ray.lifecycle import notify_task_started

            for task in tasks:
                notify_task_started(task)

    if heartbeat_due:
        command._last_successful_heartbeat_monotonic = time_mod.monotonic()
        command._db_backoff_delay = DB_BACKOFF_INITIAL_SECONDS
        command._heartbeat_retry_after = 0.0

    for index, task in enumerate(tasks):
        if command.shutdown_requested or (
            index > 0
            and command.execution_mode in ("local", "cluster")
            and not command._is_ray_core_initialized()
        ):
            if not command.lease_ownership_lost:
                command._handoff_unsubmitted_task(task)
            continue
        command.process_task(task)

    return len(expired) + len(tasks)


def handoff_unsubmitted_task(command: Any, task: Any) -> None:
    """Return a just-claimed task to durable reconciliation on shutdown."""
    handed_off = False
    with command._authoritative_task_owner(
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
            current.managed_with_django_ray_version = None
            current.last_heartbeat_at = None
            current.ray_job_id = None
            current.ray_address = None
            current.save(
                update_fields=[
                    "state",
                    "started_at",
                    "claimed_by_worker",
                    "managed_with_django_ray_version",
                    "last_heartbeat_at",
                    "ray_job_id",
                    "ray_address",
                ]
            )
            handed_off = True
    if handed_off:
        command.stdout.write(
            command.style.NOTICE(f"  Task {task.pk} handed off before remote submission")
        )
