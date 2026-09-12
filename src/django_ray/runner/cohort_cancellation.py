"""Apply an independently owned terminal cancellation without replaying work.

The caller must own the exact Core terminal observation or independently match
the physical Jobs details before accepting STOPPED. The evidence-kind argument
is a source boundary, not authentication. Local exit, stop ACK, expired proof,
or missing status is not accepted evidence and does not imply descendant cleanup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from django.db import connections, transaction

from django_ray.models import RayTaskCohortTimeout
from django_ray.runner.cohort_completion import (
    CohortCompletionApplication,
    _clock,
    _current,
    _digest,
    _expectations,
    _prepared,
)
from django_ray.runner.cohort_dispatch import PreparedCohortDispatch
from django_ray.runner.cohort_recovery import (
    RecoveredCohortJobCompletion,
    validate_recovered_cohort_job_completion,
)
from django_ray.target import cohort_claim_storage as storage
from django_ray.target.cohort_claim import CohortResolutionKind, CohortRunnerFamily


class CohortCancellationError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort cancellation refused")


def apply_cohort_cancellation[T: (PreparedCohortDispatch, RecoveredCohortJobCompletion)](
    dispatch: T,
    *,
    evidence_kind: str,
    apply_cancel: Callable[..., bool],
    apply_timeout: Callable[..., bool] | None = None,
    now: datetime | None = None,
) -> CohortCompletionApplication[T]:
    """Resolve exact CANCELLING ownership and callback together, or roll back.

    The callback receives the locked task and must return strict True after
    applying an existing fenced CANCELLED transition. An original timeout intent
    instead requires ``apply_timeout`` to persist FAILED without automatic retry.
    Its immutable snapshot survives mutable task deadline changes and adoption.
    Any durable completion,
    including an undecodable one, takes precedence over cancellation status.
    Existing HELD evidence remains immutable. No maintenance admission or new
    request, target, input, capability, or generation is constructed here.
    """
    if any(
        connection.in_atomic_block or not connection.get_autocommit()
        for connection in connections.all()
    ):
        raise CohortCancellationError
    if not callable(apply_cancel) or apply_timeout is not None and not callable(apply_timeout):
        raise CohortCancellationError
    if type(dispatch) is RecoveredCohortJobCompletion:
        validate_recovered_cohort_job_completion(dispatch)
    elif type(dispatch) is PreparedCohortDispatch:
        _prepared(dispatch)
    else:
        raise CohortCancellationError
    expected = {
        CohortRunnerFamily.RAY_CORE: "owned_core_terminal",
        CohortRunnerFamily.RAY_JOB: "exact_jobs_stopped",
    }.get(dispatch.claim.facts.binding.runner_family)
    if type(evidence_kind) is not str or expected is None or evidence_kind != expected:
        raise CohortCancellationError
    with transaction.atomic():
        observed = storage.capabilities._now(_clock() if now is None else now)
        current, record, observed = _current(dispatch, observed)
        retained = replace(dispatch, execution=current, claim=record)
        if current.completion_data is not None:
            return CohortCompletionApplication(retained, False, False)
        if current.state != "CANCELLING":
            raise CohortCancellationError
        timeout = (
            RayTaskCohortTimeout.objects.select_for_update()
            .filter(claim_id=record.claim_id)
            .first()
        )
        callback, expected_state = apply_cancel, "CANCELLED"
        evidence = "owned-cancellation:" + evidence_kind + ":" + _expectations(dispatch)[1]
        if timeout is not None:
            if apply_timeout is None or timeout.requested_at > observed:
                raise CohortCancellationError
            callback, expected_state = apply_timeout, "FAILED"
            evidence = (
                f"owned-timeout-cancellation:{evidence_kind}:{record.claim_id}:"
                f"{timeout.started_at.isoformat()}:{timeout.timeout_seconds}:"
                f"{timeout.requested_at.isoformat()}:{_expectations(dispatch)[1]}"
            )
        resolved = storage.resolve_cohort_claim(
            record.owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            kind=CohortResolutionKind.VERIFIED_CANCELLED,
            evidence_digest=_digest(evidence),
            now=observed,
        )
        if callback(current) is not True:
            raise CohortCancellationError
        current.refresh_from_db()
        if current.state != expected_state:
            raise CohortCancellationError
        return CohortCompletionApplication(
            replace(dispatch, execution=current, claim=resolved),
            True,
            False,
        )
