"""Cross-run workflow concurrency coordination and shared service budgets (#95).

This module provides strategy-neutral coordination for:
1. Overlapping workflow runs targeting the same logical ``scope_key``
   (``OverlapPolicy.ALLOW``, ``QUEUE``, ``REJECT``, ``COALESCE_LATEST``) with
   monotonic fencing tokens, priority/FIFO queuing, and TTL lease reclamation.
2. Shared external-service concurrency and token-bucket rate budgets across
   multiple concurrent workflow runs with per-run anti-starvation caps and
   ``Retry-After`` throttle cooldown feedback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

SCOPE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
MAX_CONCURRENT_RUNS_CEILING = 1024
MAX_QUEUED_RUNS_CEILING = 4096


class WorkflowConcurrencyError(RuntimeError):
    """Base error for cross-run workflow concurrency and service budgets."""


class WorkflowConcurrencyConfigurationError(WorkflowConcurrencyError, ValueError):
    """Raised when concurrency scope or service budget configuration is invalid."""


class WorkflowConcurrencyRejectedError(WorkflowConcurrencyError):
    """Raised when an overlapping run is rejected by policy or queue capacity."""


class StaleConcurrencyLeaseError(WorkflowConcurrencyError):
    """Raised when a superseded or expired lease attempts an authoritative action."""


class OverlapPolicy(StrEnum):
    """Cross-run overlap policy for a logical workflow concurrency scope."""

    ALLOW = "allow"
    QUEUE = "queue"
    REJECT = "reject"
    COALESCE_LATEST = "coalesce_latest"


@dataclass(frozen=True)
class RunOwnerIdentity:
    """Authoritative attempt/generation identity owning a concurrency lease or slot."""

    task_execution_pk: int
    attempt_number: int
    execution_generation: int
    priority: int = 0

    def validate(self) -> None:
        if (
            type(self.task_execution_pk) is not int
            or self.task_execution_pk <= 0
            or type(self.attempt_number) is not int
            or self.attempt_number <= 0
            or type(self.execution_generation) is not int
            or self.execution_generation < 0
            or type(self.priority) is not int
        ):
            raise WorkflowConcurrencyConfigurationError("Invalid RunOwnerIdentity fields")


def validate_scope_key(key: str, *, field_name: str = "scope_key") -> str:
    if not isinstance(key, str) or SCOPE_KEY_RE.match(key) is None:
        raise WorkflowConcurrencyConfigurationError(
            f"{field_name} must match {SCOPE_KEY_RE.pattern!r}"
        )
    return key


@dataclass(frozen=True)
class ConcurrencyScopeSpec:
    """Policy specification for a cross-run workflow concurrency scope."""

    scope_key: str
    overlap_policy: OverlapPolicy = OverlapPolicy.QUEUE
    max_concurrent_runs: int = 1
    max_queued_runs: int = 64
    lease_ttl_seconds: float = 300.0

    def validated(self) -> ConcurrencyScopeSpec:
        validate_scope_key(self.scope_key, field_name="scope_key")
        if not isinstance(self.overlap_policy, OverlapPolicy):
            raise WorkflowConcurrencyConfigurationError(
                "overlap_policy must be an OverlapPolicy member"
            )
        if (
            type(self.max_concurrent_runs) is not int
            or not 1 <= self.max_concurrent_runs <= MAX_CONCURRENT_RUNS_CEILING
        ):
            raise WorkflowConcurrencyConfigurationError(
                f"max_concurrent_runs must be between 1 and {MAX_CONCURRENT_RUNS_CEILING}"
            )
        if (
            type(self.max_queued_runs) is not int
            or not 0 <= self.max_queued_runs <= MAX_QUEUED_RUNS_CEILING
        ):
            raise WorkflowConcurrencyConfigurationError(
                f"max_queued_runs must be between 0 and {MAX_QUEUED_RUNS_CEILING}"
            )
        if (
            not isinstance(self.lease_ttl_seconds, int | float)
            or isinstance(self.lease_ttl_seconds, bool)
            or float(self.lease_ttl_seconds) <= 0.0
        ):
            raise WorkflowConcurrencyConfigurationError("lease_ttl_seconds must be > 0")
        return ConcurrencyScopeSpec(
            scope_key=self.scope_key,
            overlap_policy=self.overlap_policy,
            max_concurrent_runs=self.max_concurrent_runs,
            max_queued_runs=self.max_queued_runs,
            lease_ttl_seconds=float(self.lease_ttl_seconds),
        )


@dataclass(frozen=True)
class FencedConcurrencyLease:
    """Monotonically fenced lease granted to an admitted workflow run."""

    scope_key: str
    owner: RunOwnerIdentity
    fencing_token: int
    acquired_at_monotonic: float
    expires_at_monotonic: float


@dataclass(frozen=True)
class ConcurrencyAdmissionResult:
    """Outcome of requesting admission to a concurrency scope."""

    status: str  # "admitted" | "queued" | "coalesced"
    lease: FencedConcurrencyLease | None = None
    queue_position: int | None = None
    superseded_owner: RunOwnerIdentity | None = None


@dataclass
class _QueuedWaiter:
    owner: RunOwnerIdentity
    enqueue_seq: int
    enqueued_at: float


class WorkflowConcurrencyCoordinator:
    """Strategy-neutral coordinator for overlapping workflow runs."""

    def __init__(self) -> None:
        self._specs: dict[str, ConcurrencyScopeSpec] = {}
        self._fencing_counters: dict[str, int] = {}
        self._active_leases: dict[str, list[FencedConcurrencyLease]] = {}
        self._wait_queues: dict[str, list[_QueuedWaiter]] = {}
        self._enqueue_seq = 0
        self.superseded_owners: list[tuple[str, RunOwnerIdentity]] = []
        self.reclaimed_expired_leases: list[FencedConcurrencyLease] = []

    def register_scope(self, spec: ConcurrencyScopeSpec) -> ConcurrencyScopeSpec:
        validated = spec.validated()
        self._specs[validated.scope_key] = validated
        self._fencing_counters.setdefault(validated.scope_key, 0)
        self._active_leases.setdefault(validated.scope_key, [])
        self._wait_queues.setdefault(validated.scope_key, [])
        return validated

    def _reclaim_expired(self, scope_key: str, now: float) -> None:
        active = self._active_leases.get(scope_key, [])
        still_active: list[FencedConcurrencyLease] = []
        for lease in active:
            if lease.expires_at_monotonic <= now:
                self.reclaimed_expired_leases.append(lease)
            else:
                still_active.append(lease)
        self._active_leases[scope_key] = still_active

    def _issue_lease(
        self,
        spec: ConcurrencyScopeSpec,
        owner: RunOwnerIdentity,
        now: float,
    ) -> FencedConcurrencyLease:
        next_token = self._fencing_counters.get(spec.scope_key, 0) + 1
        self._fencing_counters[spec.scope_key] = next_token
        lease = FencedConcurrencyLease(
            scope_key=spec.scope_key,
            owner=owner,
            fencing_token=next_token,
            acquired_at_monotonic=now,
            expires_at_monotonic=now + spec.lease_ttl_seconds,
        )
        self._active_leases[spec.scope_key].append(lease)
        return lease

    def request_admission(
        self,
        scope_key: str,
        owner: RunOwnerIdentity,
        *,
        now_monotonic: float = 0.0,
    ) -> ConcurrencyAdmissionResult:
        """Request admission for ``owner`` under ``scope_key``."""
        owner.validate()
        spec = self._specs.get(scope_key)
        if spec is None:
            raise WorkflowConcurrencyConfigurationError(f"Unregistered scope_key: {scope_key!r}")

        self._reclaim_expired(scope_key, now_monotonic)
        active = self._active_leases[scope_key]

        # Idempotent re-admission check for an already active owner.
        for existing in active:
            if existing.owner == owner:
                return ConcurrencyAdmissionResult(status="admitted", lease=existing)

        if len(active) < spec.max_concurrent_runs and not self._wait_queues[scope_key]:
            lease = self._issue_lease(spec, owner, now_monotonic)
            return ConcurrencyAdmissionResult(status="admitted", lease=lease)

        if spec.overlap_policy == OverlapPolicy.REJECT:
            raise WorkflowConcurrencyRejectedError(
                f"Scope {scope_key!r} is at capacity ({len(active)}/{spec.max_concurrent_runs}) "
                "and uses OverlapPolicy.REJECT"
            )

        queue = self._wait_queues[scope_key]
        if spec.overlap_policy == OverlapPolicy.COALESCE_LATEST:
            superseded: RunOwnerIdentity | None = None
            if queue:
                superseded = queue[-1].owner
                self.superseded_owners.append((scope_key, superseded))
                queue.clear()
            self._enqueue_seq += 1
            queue.append(
                _QueuedWaiter(
                    owner=owner,
                    enqueue_seq=self._enqueue_seq,
                    enqueued_at=now_monotonic,
                )
            )
            return ConcurrencyAdmissionResult(
                status="coalesced" if superseded is not None else "queued",
                queue_position=1,
                superseded_owner=superseded,
            )

        # OverlapPolicy.QUEUE or OverlapPolicy.ALLOW when at max_concurrent_runs
        if len(queue) >= spec.max_queued_runs:
            raise WorkflowConcurrencyRejectedError(
                f"Scope {scope_key!r} wait queue is full ({spec.max_queued_runs})"
            )
        self._enqueue_seq += 1
        queue.append(
            _QueuedWaiter(
                owner=owner,
                enqueue_seq=self._enqueue_seq,
                enqueued_at=now_monotonic,
            )
        )
        # Sort by descending priority, then FIFO enqueue_seq.
        queue.sort(key=lambda w: (-w.owner.priority, w.enqueue_seq))
        pos = next(i for i, w in enumerate(queue, start=1) if w.owner == owner)
        return ConcurrencyAdmissionResult(status="queued", queue_position=pos)

    def validate_fencing_token(
        self,
        lease: FencedConcurrencyLease,
        *,
        now_monotonic: float | None = None,
    ) -> None:
        """Verify that ``lease`` is still the active, non-expired owner."""
        if now_monotonic is not None:
            self._reclaim_expired(lease.scope_key, now_monotonic)
        active = self._active_leases.get(lease.scope_key, [])
        for current in active:
            if current.fencing_token == lease.fencing_token and current.owner == lease.owner:
                return
        raise StaleConcurrencyLeaseError(
            f"Concurrency lease token={lease.fencing_token} for scope {lease.scope_key!r} "
            "is stale or expired"
        )

    def renew_lease(
        self,
        lease: FencedConcurrencyLease,
        *,
        now_monotonic: float,
    ) -> FencedConcurrencyLease:
        self.validate_fencing_token(lease, now_monotonic=now_monotonic)
        spec = self._specs[lease.scope_key]
        renewed = FencedConcurrencyLease(
            scope_key=lease.scope_key,
            owner=lease.owner,
            fencing_token=lease.fencing_token,
            acquired_at_monotonic=lease.acquired_at_monotonic,
            expires_at_monotonic=now_monotonic + spec.lease_ttl_seconds,
        )
        active = self._active_leases[lease.scope_key]
        for idx, current in enumerate(active):
            if current.fencing_token == lease.fencing_token:
                active[idx] = renewed
                break
        return renewed

    def release_and_advance(
        self,
        lease: FencedConcurrencyLease,
        *,
        now_monotonic: float = 0.0,
    ) -> list[FencedConcurrencyLease]:
        """Release ``lease`` and promote eligible queued runs in priority/FIFO order."""
        self._reclaim_expired(lease.scope_key, now_monotonic)
        active = self._active_leases.get(lease.scope_key, [])
        self._active_leases[lease.scope_key] = [
            c for c in active if c.fencing_token != lease.fencing_token
        ]
        return self.advance_queue(lease.scope_key, now_monotonic=now_monotonic)

    def advance_queue(
        self,
        scope_key: str,
        *,
        now_monotonic: float = 0.0,
    ) -> list[FencedConcurrencyLease]:
        spec = self._specs[scope_key]
        self._reclaim_expired(scope_key, now_monotonic)
        active = self._active_leases[scope_key]
        queue = self._wait_queues[scope_key]
        promoted: list[FencedConcurrencyLease] = []
        while len(active) < spec.max_concurrent_runs and queue:
            waiter = queue.pop(0)
            new_lease = self._issue_lease(spec, waiter.owner, now_monotonic)
            promoted.append(new_lease)
        return promoted


@dataclass(frozen=True)
class ExternalServiceBudgetSpec:
    """Shared concurrency and token-bucket rate budget for an external service."""

    service_key: str
    max_in_flight: int = 8
    max_in_flight_per_run: int = 4
    rate_tokens_per_second: float = 10.0
    burst_capacity: int = 10

    def validated(self) -> ExternalServiceBudgetSpec:
        validate_scope_key(self.service_key, field_name="service_key")
        if type(self.max_in_flight) is not int or not 1 <= self.max_in_flight <= 4096:
            raise WorkflowConcurrencyConfigurationError("max_in_flight must be between 1 and 4096")
        if (
            type(self.max_in_flight_per_run) is not int
            or not 1 <= self.max_in_flight_per_run <= self.max_in_flight
        ):
            raise WorkflowConcurrencyConfigurationError(
                "max_in_flight_per_run must be between 1 and max_in_flight"
            )
        if (
            not isinstance(self.rate_tokens_per_second, int | float)
            or isinstance(self.rate_tokens_per_second, bool)
            or float(self.rate_tokens_per_second) <= 0.0
        ):
            raise WorkflowConcurrencyConfigurationError("rate_tokens_per_second must be > 0")
        if type(self.burst_capacity) is not int or self.burst_capacity < 1:
            raise WorkflowConcurrencyConfigurationError("burst_capacity must be >= 1")
        return ExternalServiceBudgetSpec(
            service_key=self.service_key,
            max_in_flight=self.max_in_flight,
            max_in_flight_per_run=self.max_in_flight_per_run,
            rate_tokens_per_second=float(self.rate_tokens_per_second),
            burst_capacity=self.burst_capacity,
        )


@dataclass(frozen=True)
class ServiceBudgetGrant:
    """Permit for one external service call by a workflow run."""

    service_key: str
    owner: RunOwnerIdentity
    grant_id: int
    granted_at_monotonic: float


class SharedServiceBudgetController:
    """Coordinates shared external-service in-flight and rate budgets across runs."""

    def __init__(self) -> None:
        self._specs: dict[str, ExternalServiceBudgetSpec] = {}
        self._tokens: dict[str, float] = {}
        self._last_refill: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._in_flight_by_service: dict[str, dict[int, RunOwnerIdentity]] = {}
        self._grant_seq = 0
        self.throttle_events_recorded: dict[str, int] = {}

    def register_service(
        self,
        spec: ExternalServiceBudgetSpec,
        *,
        now_monotonic: float = 0.0,
    ) -> ExternalServiceBudgetSpec:
        validated = spec.validated()
        key = validated.service_key
        self._specs[key] = validated
        self._tokens[key] = float(validated.burst_capacity)
        self._last_refill[key] = now_monotonic
        self._cooldown_until[key] = 0.0
        self._in_flight_by_service[key] = {}
        self.throttle_events_recorded.setdefault(key, 0)
        return validated

    def _refill(self, service_key: str, now_monotonic: float) -> None:
        spec = self._specs[service_key]
        elapsed = max(0.0, now_monotonic - self._last_refill[service_key])
        if elapsed > 0.0:
            self._tokens[service_key] = min(
                float(spec.burst_capacity),
                self._tokens[service_key] + elapsed * spec.rate_tokens_per_second,
            )
            self._last_refill[service_key] = now_monotonic

    def try_acquire(
        self,
        service_key: str,
        owner: RunOwnerIdentity,
        *,
        now_monotonic: float = 0.0,
    ) -> tuple[ServiceBudgetGrant | None, str]:
        """Attempt to acquire an in-flight + rate token permit for ``owner``."""
        owner.validate()
        spec = self._specs.get(service_key)
        if spec is None:
            raise WorkflowConcurrencyConfigurationError(
                f"Unregistered service_key: {service_key!r}"
            )
        if now_monotonic < self._cooldown_until[service_key]:
            return None, "throttled_cooldown"

        self._refill(service_key, now_monotonic)
        active_grants = self._in_flight_by_service[service_key]
        if len(active_grants) >= spec.max_in_flight:
            return None, "service_max_in_flight"

        owner_in_flight = sum(1 for o in active_grants.values() if o == owner)
        if owner_in_flight >= spec.max_in_flight_per_run:
            return None, "per_run_fairness_cap"

        if self._tokens[service_key] < 1.0:
            return None, "rate_budget_exhausted"

        self._tokens[service_key] -= 1.0
        self._grant_seq += 1
        grant = ServiceBudgetGrant(
            service_key=service_key,
            owner=owner,
            grant_id=self._grant_seq,
            granted_at_monotonic=now_monotonic,
        )
        active_grants[grant.grant_id] = owner
        return grant, "granted"

    def release(self, grant: ServiceBudgetGrant) -> None:
        active = self._in_flight_by_service.get(grant.service_key)
        if active is not None:
            active.pop(grant.grant_id, None)

    def record_throttle_feedback(
        self,
        service_key: str,
        *,
        retry_after_seconds: float,
        now_monotonic: float = 0.0,
    ) -> float:
        """Apply a ``Retry-After`` cooldown window across all runs sharing ``service_key``."""
        if service_key not in self._specs:
            raise WorkflowConcurrencyConfigurationError(
                f"Unregistered service_key: {service_key!r}"
            )
        bounded_backoff = max(0.0, min(float(retry_after_seconds), 300.0))
        target = now_monotonic + bounded_backoff
        self._cooldown_until[service_key] = max(self._cooldown_until[service_key], target)
        self._tokens[service_key] = 0.0
        self._last_refill[service_key] = self._cooldown_until[service_key]
        self.throttle_events_recorded[service_key] += 1
        return self._cooldown_until[service_key]


__all__ = [
    "ConcurrencyAdmissionResult",
    "ConcurrencyScopeSpec",
    "ExternalServiceBudgetSpec",
    "FencedConcurrencyLease",
    "OverlapPolicy",
    "RunOwnerIdentity",
    "ServiceBudgetGrant",
    "SharedServiceBudgetController",
    "StaleConcurrencyLeaseError",
    "WorkflowConcurrencyConfigurationError",
    "WorkflowConcurrencyCoordinator",
    "WorkflowConcurrencyError",
    "WorkflowConcurrencyRejectedError",
    "validate_scope_key",
]
