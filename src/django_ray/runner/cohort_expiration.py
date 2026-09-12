"""Expire elapsed queued deadlines inside one declared current cohort.

No runtime qualification, task callable, input, or RuntimeEnv is consulted.
Admission pauses and quarantine do not turn work into expiry: only its actual
queue deadline does. Retirement prevents this new unowned queue management.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from django.db import connections, transaction
from django.db.models import Q

from django_ray import lifecycle
from django_ray.maintenance import (
    check_worker_retirement_admission,
    maintenance_admission_barrier,
)
from django_ray.models import RayTaskExecution, TaskState
from django_ray.runner.cohort_claims import CohortClaimAlias
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.target import capabilities
from django_ray.target.cohort_claim import CohortManagerRuntime, validate_cohort_python
from django_ray.target.cohort_claim_storage import _claim_lease
from django_ray.target.cohort_intent import CohortSelectionPolicy, _digest, _package_version, _text

_FIELDS = tuple(
    dict.fromkeys(
        (
            *lifecycle._LIFECYCLE_LOCK_FIELDS,
            *lifecycle._ATTEMPT_ARCHIVE_READ_FIELDS,
            "task_id",
            "queue_name",
            "queue_deadline_at",
            "created_with_django_ray_version",
        )
    )
)


class CohortExpirationError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort queue expiration refused")


def _clock():
    return datetime.now(UTC)


def _fresh(previous):
    current = capabilities._now(_clock())
    if current < previous:
        raise CohortExpirationError
    return current


def _declarations(aliases, manager_runtime, limit):
    if (
        type(manager_runtime) is not CohortManagerRuntime
        or type(limit) is not int
        or not 1 <= limit <= 100
        or isinstance(aliases, (str, bytes))
        or not isinstance(aliases, Sequence)
        or len(aliases) > 64
    ):
        raise CohortExpirationError
    _package_version(manager_runtime.package_version)
    validate_cohort_python(manager_runtime.python)
    identities = set()
    selected = Q(pk__in=[])
    for alias in aliases:
        if (
            type(alias) is not CohortClaimAlias
            or type(alias.selection_policy) is not CohortSelectionPolicy
            or type(alias.queues) is not tuple
            or not 1 <= len(alias.queues) <= 64
            or any(
                type(queue) is not str or not queue.strip() or len(queue) > 100 or "\x00" in queue
                for queue in alias.queues
            )
            or len(set(alias.queues)) != len(alias.queues)
        ):
            raise CohortExpirationError
        _text(alias.alias, limit=128)
        _digest(alias.declaration_digest)
        if alias.alias in identities:
            raise CohortExpirationError
        identities.add(alias.alias)
        selected |= Q(
            cohort_intent__schema_version=2,
            cohort_intent__package_version=manager_runtime.package_version,
            cohort_intent__backend_alias=alias.alias,
            cohort_intent__configuration_digest=alias.declaration_digest,
            cohort_intent__selection_policy=alias.selection_policy.value,
            queue_name__in=alias.queues,
        )
    return selected


def _live(identity, package, now):
    lease = _claim_lease(identity, now, using="default")
    if lease.django_ray_version != package:
        raise CohortExpirationError
    return lease


def expire_cohort_queued_tasks(
    identity: WorkerLeaseIdentity,
    *,
    aliases: Sequence[CohortClaimAlias],
    manager_runtime: CohortManagerRuntime,
    limit: int = 100,
    now: datetime | None = None,
) -> tuple[int, ...]:
    """Archive only elapsed exact queued identities; never create a new attempt.

    Each selected task is skipped if locked elsewhere or changed after the
    bounded scan. Any lease loss or clock regression rolls the whole batch back.
    The shared maintenance barrier precedes the lease and task locks solely for
    ordering with retirement; enqueue/claim pauses do not prevent truthful expiry.
    """
    if any(
        connection.in_atomic_block or not connection.get_autocommit()
        for connection in connections.all()
    ):
        raise CohortExpirationError
    identity = capabilities._identity(identity)
    selected = _declarations(aliases, manager_runtime, limit)
    now = capabilities._now(_clock() if now is None else now)
    with transaction.atomic(), maintenance_admission_barrier() as barrier:
        _live(identity, manager_runtime.package_version, now)
        now = _fresh(now)
        _live(identity, manager_runtime.package_version, now)
        check_worker_retirement_admission(identity, barrier=barrier)
        candidates = RayTaskExecution.objects.filter(
            selected,
            execution_protocol_version=3,
            created_with_django_ray_version=manager_runtime.package_version,
            state=TaskState.QUEUED,
            queue_deadline_at__isnull=False,
            queue_deadline_at__lte=now,
        )
        previews = tuple(
            candidates.order_by("queue_deadline_at", "pk").values(
                "pk",
                "task_id",
                "attempt_number",
                "execution_generation",
                "queue_deadline_at",
            )[:limit]
        )
        expired = []
        for preview in previews:
            current = (
                candidates.select_for_update(skip_locked=True, of=("self",))
                .only(*_FIELDS)
                .filter(**preview)
                .first()
            )
            now = _fresh(now)
            _live(identity, manager_runtime.package_version, now)
            if (
                current is None
                or current.queue_deadline_at is None
                or current.queue_deadline_at > now
            ):
                continue
            current.state = TaskState.EXPIRED
            current.finished_at = now
            current.error_message = lifecycle.QUEUE_EXPIRED_ERROR
            current.error_traceback = None
            current.managed_with_django_ray_version = manager_runtime.package_version
            lifecycle._record_attempt(current)
            current.save(
                update_fields=(
                    "state",
                    "finished_at",
                    "error_message",
                    "error_traceback",
                    "managed_with_django_ray_version",
                )
            )
            expired.append(current.pk)
        now = _fresh(now)
        _live(identity, manager_runtime.package_version, now)
        return tuple(expired)
