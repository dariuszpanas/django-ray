"""Reserve one exact cancellation request before any remote operation.

The durable indeterminate marker prevents replay after a lost acknowledgment or
manager restart. An acknowledgment never resolves the claim; independent owned
completion or exact outer termination supplies that separate evidence.
"""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from django.db import connections, transaction

from django_ray.models import CancellationStatus, TaskState
from django_ray.runner.cohort_completion import _current, _prepared
from django_ray.runner.cohort_recovery import (
    RecoveredCohortJobCompletion,
    validate_recovered_cohort_job_completion,
)
from django_ray.target.cohort_claim import (
    CohortClaimDisposition,
    CohortHoldBoundary,
    CohortHoldReason,
    CohortRunnerFamily,
)

_UNCERTAIN = "Exact cohort cancellation request has no terminal confirmation"


@dataclass(frozen=True, slots=True)
class CohortCancellationReservation[T]:
    dispatch: T
    should_request: bool


@contextmanager
def _locked(dispatch, now):
    if any(
        connection.in_atomic_block or not connection.get_autocommit()
        for connection in connections.all()
    ):
        raise ValueError("Cohort cancellation requires its own transaction")
    if type(dispatch) is RecoveredCohortJobCompletion:
        validate_recovered_cohort_job_completion(dispatch)
    else:
        _prepared(dispatch)
    if dispatch.claim.facts.binding.runner_family is CohortRunnerFamily.SYNC:
        raise ValueError("Sync has no remote cancellation operation")
    with transaction.atomic():
        current, record, _observed = _current(dispatch, datetime.now(UTC) if now is None else now)
        yield current, record


def reserve_cohort_cancellation(dispatch, *, request=False, now=None):
    """Reserve at most once; ``request`` also asks an owned running task to stop.

    Existing cancellation metadata, including an unknown prior request, forbids
    another remote call. Missing terminal data is rechecked under the task lock.
    The returned value carries refreshed task fields without changing claim facts.
    """
    if type(request) is not bool:
        raise ValueError("Invalid cohort cancellation request")
    with _locked(dispatch, now) as (current, record):
        eligible = (
            current.completion_data is None
            and current.cancellation_status is None
            and (
                current.state == TaskState.CANCELLING
                or request
                and current.state == TaskState.RUNNING
            )
        )
        if eligible:
            current.state = TaskState.CANCELLING
            current.cancellation_status = CancellationStatus.INDETERMINATE
            current.cancellation_error = _UNCERTAIN
            current.save(update_fields=["state", "cancellation_status", "cancellation_error"])
        return CohortCancellationReservation(
            replace(dispatch, execution=current, claim=record), eligible
        )


def request_owned_cohort_cancellation(dispatch, *, now=None):
    """Request stopping owned work without claiming a remote call was issued."""
    with _locked(dispatch, now) as (current, record):
        if current.state == TaskState.RUNNING and current.completion_data is None:
            current.state = TaskState.CANCELLING
            current.save(update_fields=["state"])
        return replace(dispatch, execution=current, claim=record)


def acknowledge_cohort_cancellation(dispatch, *, requested, now=None):
    """Record only a strict request acknowledgment, never terminal proof."""
    if type(requested) is not bool:
        raise ValueError("Invalid cohort cancellation acknowledgment")
    with _locked(dispatch, now) as (current, record):
        if (
            requested
            and current.state == TaskState.CANCELLING
            and current.completion_data is None
            and current.cancellation_status == CancellationStatus.INDETERMINATE
            and current.cancellation_error == _UNCERTAIN
        ):
            current.cancellation_status = CancellationStatus.REQUESTED
            current.cancellation_error = None
            current.save(update_fields=["cancellation_status", "cancellation_error"])
        return replace(dispatch, execution=current, claim=record)


def release_unstarted_cohort_cancellation(reservation, *, now=None):
    """Clear only after the owned adapter explicitly returned no operation.

    A caller exception, timeout, failed callback or ambiguous response cannot
    call this seam. Returning None from begin is the source-owned observation
    that no callback/request began; the reservation object is not authentication.
    The task remains CANCELLING while waiting for bounded local capacity.
    """
    if (
        type(reservation) is not CohortCancellationReservation
        or reservation.should_request is not True
    ):
        raise ValueError("Cancellation reservation was not issued")
    with _locked(reservation.dispatch, now) as (current, record):
        if (
            current.state == TaskState.CANCELLING
            and current.completion_data is None
            and current.cancellation_status == CancellationStatus.INDETERMINATE
            and current.cancellation_error == _UNCERTAIN
        ):
            current.cancellation_status = None
            current.cancellation_error = None
            current.save(update_fields=["cancellation_status", "cancellation_error"])
        return replace(reservation.dispatch, execution=current, claim=record)


def hold_cohort_cancellation(dispatch, *, now=None):
    """Retain unknown outer cancellation without changing the task generation."""
    from django_ray.target.cohort_claim_storage import hold_cohort_claim

    observed = datetime.now(UTC) if now is None else now
    with _locked(dispatch, observed) as (current, record):
        if (
            current.state == TaskState.CANCELLING
            and current.completion_data is None
            and record.disposition is CohortClaimDisposition.OPEN
        ):
            record = hold_cohort_claim(
                record.owner,
                record.claim_id,
                expected_identity=record.facts.identity,
                expected_revision=record.revision,
                reason=CohortHoldReason.CANCELLATION_UNCERTAIN,
                boundary=CohortHoldBoundary.CONTROL,
                evidence_digest=record.prepared_request_digest,
                application_invoked=None,
                now=observed,
            )
        return replace(dispatch, execution=current, claim=record)
