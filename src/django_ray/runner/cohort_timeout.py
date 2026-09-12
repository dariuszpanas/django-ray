"""Request owned timeout cancellation without claiming the application stopped."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from django.db import connections, transaction

from django_ray.models import RayTaskCohortTimeout
from django_ray.runner.cohort_completion import _current, _digest, _prepared
from django_ray.runner.cohort_dispatch import PreparedCohortDispatch
from django_ray.runner.cohort_recovery import (
    RecoveredCohortJobCompletion,
    validate_recovered_cohort_job_completion,
)
from django_ray.target import cohort_claim_storage as claims
from django_ray.target.cohort_claim import (
    CohortClaimDisposition,
    CohortHoldBoundary,
    CohortHoldReason,
    CohortRunnerFamily,
)


class CohortTimeoutError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort timeout request refused")


@dataclass(frozen=True, slots=True)
class CohortTimeoutRequest[T]:
    dispatch: T
    requested: bool


def _clock():
    return datetime.now(UTC)


def request_cohort_timeout[T: (PreparedCohortDispatch, RecoveredCohortJobCompletion)](
    dispatch: T, *, now: datetime | None = None
) -> CohortTimeoutRequest[T]:
    """Snapshot elapsed timeout and hold the exact owned generation atomically.

    Existing operator cancellation is never reclassified. None/zero keeps the
    existing disabled-timeout behavior; equality to the deadline is not overdue.
    The remote request reservation remains separate, so no ACK/status is invented.
    Authentic late completion may resolve this HELD generation normally.
    """
    if any(
        connection.in_atomic_block or not connection.get_autocommit()
        for connection in connections.all()
    ):
        raise CohortTimeoutError
    if type(dispatch) is RecoveredCohortJobCompletion:
        validate_recovered_cohort_job_completion(dispatch)
    elif type(dispatch) is PreparedCohortDispatch:
        _prepared(dispatch)
    else:
        raise CohortTimeoutError
    if dispatch.claim.facts.binding.runner_family is CohortRunnerFamily.SYNC:
        raise CohortTimeoutError
    with transaction.atomic():
        observed = claims.capabilities._now(_clock() if now is None else now)
        current, record, observed = _current(dispatch, observed)
        refreshed = replace(dispatch, execution=current, claim=record)
        if (
            current.state != "RUNNING"
            or current.completion_data is not None
            or current.cancellation_status is not None
            or current.started_at is None
            or current.timeout_seconds in (None, 0)
        ):
            return CohortTimeoutRequest(refreshed, False)
        if (
            type(current.timeout_seconds) is not int
            or not 1 <= current.timeout_seconds <= 2147483647
        ):
            raise CohortTimeoutError
        started = claims.capabilities._now(current.started_at)
        try:
            deadline = started + timedelta(seconds=current.timeout_seconds)
        except OverflowError:
            raise CohortTimeoutError from None
        fresh = claims.capabilities._now(_clock())
        if fresh < observed:
            raise CohortTimeoutError
        if fresh <= deadline:
            return CohortTimeoutRequest(refreshed, False)
        if RayTaskCohortTimeout.objects.filter(claim_id=record.claim_id).exists():
            return CohortTimeoutRequest(refreshed, False)
        RayTaskCohortTimeout.objects.create(
            claim_id=record.claim_id,
            requested_at=fresh,
            started_at=started,
            timeout_seconds=current.timeout_seconds,
            deadline_at=deadline,
        )
        current.state = "CANCELLING"
        current.save(update_fields=["state"])
        if record.disposition is CohortClaimDisposition.OPEN:
            record = claims.hold_cohort_claim(
                record.owner,
                record.claim_id,
                expected_identity=record.facts.identity,
                expected_revision=record.revision,
                reason=CohortHoldReason.CANCELLATION_UNCERTAIN,
                boundary=CohortHoldBoundary.CONTROL,
                evidence_digest=_digest(
                    f"owned-timeout:{record.claim_id}:{started.isoformat()}:{current.timeout_seconds}:{fresh.isoformat()}"
                ),
                application_invoked=None,
                now=fresh,
            )
        finished = claims.capabilities._now(_clock())
        if finished < fresh:
            raise CohortTimeoutError
        claims._lease(record.owner, finished, using="default")
        return CohortTimeoutRequest(replace(dispatch, execution=current, claim=record), True)
