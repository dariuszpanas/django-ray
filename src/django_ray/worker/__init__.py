"""Cohesive worker domain services extracted from ``django_ray_worker``.

This package is a strict acyclic layer below ``django_ray_worker.py``:
- ``__init__.py`` holds shared value types, constants, and module-attribute
  dependency resolution so test monkeypatches targeting
  ``django_ray.management.commands.django_ray_worker`` keep working without
  introducing any circular import back to the management command.
- ``claimer.py`` owns queue parsing/affinity, lease lifecycle/fencing, and task
  claiming.
- ``dispatcher.py`` owns Ray driver initialization/reconnection, synchronous
  execution via ``SyncRunner``, Ray Core / Ray Job submission, and result or
  failure persistence.
- ``reconciler.py`` owns completion decoding, Ray Core polling, and Ray Job
  reconciliation.
- ``maintenance.py`` owns bounded maintenance scans, stuck/timeout recovery,
  cancellation finalization, and expired lease cleanup.
- ``shutdown.py`` owns graceful shutdown handoff, lease release, and Ray
  disconnect.
- ``loop.py`` owns signal handling, heartbeats, ``--drain`` termination, and
  the adaptive main polling loop.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import InterfaceError, OperationalError, close_old_connections
from django.db.models import Count

from django_ray import lease_policy
from django_ray.execution_codec import DecodedExecutionCompletion, ExecutionCompletionRejection
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.reconciliation import mark_task_lost

BUILTIN_DATETIME = datetime
DEFAULT_MARK_TASK_LOST = mark_task_lost

WORKER_ID_ALLOCATION_ATTEMPTS = 3
SHUTDOWN_WAIT_SLICE_SECONDS = 0.1
RAY_RECONNECT_INITIAL_DELAY_SECONDS = 5.0
RAY_RECONNECT_MAX_DELAY_SECONDS = 60.0
DB_BACKOFF_INITIAL_SECONDS = 0.5
DB_BACKOFF_MAX_SECONDS = 5.0
LEASE_QUEUE_NAME_MAX_LENGTH = 1024
DEFAULT_DRAIN_TIMEOUT_SECONDS = 300.0
MAX_DRAIN_TIMEOUT_SECONDS = 86_400.0
DEFAULT_CANCELLATION_INTERVAL_SECONDS = 5.0
DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS = 5.0

CLAIM_UPDATE_FIELDS: tuple[str, ...] = (
    "state",
    "started_at",
    "last_heartbeat_at",
    "claimed_by_worker",
    "managed_with_django_ray_version",
    "execution_generation",
    "error_message",
    "error_traceback",
    "cancellation_status",
    "cancellation_error",
    "completion_data",
    "progress_data",
    "workflow_progress_summary_json",
    "workflow_run_id",
    "workflow_plan_selection",
    "ray_job_id",
    "ray_job_request_reference",
    "ray_target_address",
    "ray_address",
)

DEFERRED_MAINTENANCE_SCAN_FIELDS: tuple[str, ...] = (
    "args_json",
    "kwargs_json",
    "runtime_env_json",
    "workflow_plan_json",
    "progress_data",
    "result_data",
    "error_traceback",
    "error_message",
    "cancellation_error",
    "workflow_plan_selection",
    "workflow_progress_summary_json",
)


class WorkerIdReservedByTaskError(Exception):
    """Signal that a retained in-flight task still owns a worker ID."""


@dataclass(frozen=True, slots=True)
class OwnedTask:
    """One execution held behind this process's exact live lease fence."""

    execution: Any
    adopted: bool
    supported_protocols: ExecutionProtocolRange


@dataclass(frozen=True, slots=True)
class InspectedExecutionCompletion:
    """One completion decoded inside the authoritative task boundary."""

    decoded: DecodedExecutionCompletion | None
    prepared_result_reference: str | None
    rejection: ExecutionCompletionRejection | None
    requires_nonretryable_disposition: bool


@dataclass(frozen=True)
class RayBackendQueueConfiguration:
    """Validated queue ownership needed before a worker may claim work."""

    aliases: tuple[str, ...]
    queues: tuple[str, ...]
    queues_by_alias: Mapping[str, tuple[str, ...]]
    ray_targets_by_alias: Mapping[str, str]


@dataclass(frozen=True)
class RayJobQueueAffinity:
    """Raw opt-in queue reservations that do not import unrelated backends."""

    aliases_by_queue: Mapping[str, tuple[str, ...]]


def command_dep(target: Any, name: str, default: Any) -> Any:
    """Resolve a module-level dependency from the command's defining module.

    Looking up attributes via ``sys.modules`` preserves compatibility with
    unit tests that monkeypatch globals on ``django_ray_worker`` while keeping
    the ``django_ray.worker`` package free of circular imports.
    """
    if target is None:
        return default
    module_name = target.__module__ if isinstance(target, type) else type(target).__module__
    module = sys.modules.get(module_name)
    if module is None:
        return default
    return getattr(module, name, default)


def task_model(command: Any) -> Any:
    """Return the active ``RayTaskExecution`` model class."""
    return command_dep(command, "RayTaskExecution", RayTaskExecution)


def lease_model(command: Any) -> Any:
    """Return the active ``TaskWorkerLease`` model class."""
    return command_dep(command, "TaskWorkerLease", TaskWorkerLease)


def is_transient_db_error(error: BaseException) -> bool:
    """Return whether a database or lease-policy error is transient."""
    if isinstance(error, (OperationalError, InterfaceError)):
        return True
    return isinstance(error, lease_policy.LeasePolicyError) and error.reason in {
        "lease_policy_busy",
        "lease_policy_unavailable",
    }


def close_old_db_connections(command: Any | None = None) -> None:
    """Close obsolete or unusable Django DB connections safely between loop cycles."""
    from django.db import connections

    close_fn = command_dep(command, "close_old_connections", close_old_connections)
    if close_fn is close_old_connections:
        try:
            for conn in connections.all(initialized_only=True):
                if conn.in_atomic_block:
                    continue
                conn.close_if_unusable_or_obsolete()
        except RuntimeError as error:
            if "Database access not allowed" in str(error):
                return
            raise
        return
    try:
        close_fn()
    except RuntimeError as error:
        if "Database access not allowed" in str(error):
            return
        raise


def in_flight_task_count(command: Any) -> int:
    """Return the number of tasks currently tracked in flight by this worker."""
    runner = getattr(command, "ray_core_runner", None)
    ray_core_pending = int(getattr(runner, "pending_count", 0) or 0) if runner is not None else 0
    active_tasks = getattr(command, "active_tasks", None)
    ray_job_active = len(active_tasks) if active_tasks is not None else 0
    return ray_job_active + ray_core_pending


def active_queue_task_counts(command: Any, queues: Sequence[str]) -> tuple[int, int]:
    """Return ``(queued_count, active_running_or_cancelling_count)`` for ``queues``."""
    queue_list = list(queues)
    if not queue_list:
        return 0, 0
    task_cls = task_model(command)
    rows = (
        task_cls.objects.filter(
            queue_name__in=queue_list,
            state__in=(TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING),
        )
        .values("state")
        .annotate(total=Count("pk"))
    )
    queued_count = 0
    active_count = 0
    for row in rows:
        state = row["state"]
        total = int(row["total"] or 0)
        if state == TaskState.QUEUED:
            queued_count += total
        else:
            active_count += total
    return queued_count, active_count


__all__ = [
    "BUILTIN_DATETIME",
    "CLAIM_UPDATE_FIELDS",
    "DB_BACKOFF_INITIAL_SECONDS",
    "DB_BACKOFF_MAX_SECONDS",
    "DEFAULT_CANCELLATION_INTERVAL_SECONDS",
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS",
    "DEFAULT_MARK_TASK_LOST",
    "DEFERRED_MAINTENANCE_SCAN_FIELDS",
    "InspectedExecutionCompletion",
    "LEASE_QUEUE_NAME_MAX_LENGTH",
    "MAX_DRAIN_TIMEOUT_SECONDS",
    "OwnedTask",
    "RAY_RECONNECT_INITIAL_DELAY_SECONDS",
    "RAY_RECONNECT_MAX_DELAY_SECONDS",
    "RayBackendQueueConfiguration",
    "RayJobQueueAffinity",
    "SHUTDOWN_WAIT_SLICE_SECONDS",
    "WORKER_ID_ALLOCATION_ATTEMPTS",
    "WorkerIdReservedByTaskError",
    "active_queue_task_counts",
    "close_old_db_connections",
    "command_dep",
    "in_flight_task_count",
    "is_transient_db_error",
    "lease_model",
    "task_model",
]
