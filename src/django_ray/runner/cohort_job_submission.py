"""One owned Jobs callback with parent-only request attachment.

The callback owns its RuntimeEnv snapshot through preparation, parent admission,
submission and cleanup. Timeouts retain uncertainty and local ownership until
that callback actually exits. Neither its exit nor an SDK acknowledgement proves
remote task cleanup, application completion or permission to repeat a submission.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any

from django_ray.execution_codec import ExecutionIdentity
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.ray_job import RayJobRunner


class CohortJobSubmissionError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort Jobs submission unavailable")


@dataclass(frozen=True, slots=True)
class CohortJobSubmissionTicket:
    identity: ExecutionIdentity
    request_digest: str
    contract_digest: str
    submission_id: str
    jobs_endpoint: str


@dataclass(frozen=True, slots=True)
class CohortJobSubmissionResult:
    ticket: CohortJobSubmissionTicket
    stage: str
    accepted: bool
    uncertainty: str | None
    local_cleanup_complete: bool


@dataclass
class _Operation:
    ticket: CohortJobSubmissionTicket
    source: Any = field(repr=False)
    runner: RayJobRunner = field(repr=False)
    claim: Any = field(repr=False)
    began_wall: datetime
    began_mono: float
    deadline_wall: datetime
    deadline_mono: float
    parent_wall: datetime
    parent_mono: float
    wake: Event = field(default_factory=Event, repr=False)
    prepared: Event = field(default_factory=Event, repr=False)
    completed: Event = field(default_factory=Event, repr=False)
    thread: Any = field(default=None, repr=False)
    staged: Any = field(default=None, repr=False)
    authorized: bool = False
    accepted: bool = False
    cleaned: bool = False
    uncertainty: str | None = None


def _clock():
    return datetime.now(UTC), time.monotonic()


def _time():
    wall, mono = _clock()
    if (
        type(wall) is not datetime
        or wall.tzinfo is None
        or wall.utcoffset() != timedelta(0)
        or type(mono) not in {int, float}
        or not math.isfinite(mono)
        or mono < 0
    ):
        raise CohortJobSubmissionError
    return wall, float(mono)


def _outside_transaction():
    from django.db import connections

    if any(
        item.in_atomic_block or (item.connection is not None and item.autocommit is not True)
        for item in connections.all(initialized_only=True)
    ):
        raise CohortJobSubmissionError


def _uncertain(operation, reason):
    if operation.uncertainty is None:
        operation.uncertainty = reason
    operation.wake.set()


def _alive(operation):
    if operation.thread is None:
        return False
    try:
        alive = operation.thread.is_alive()
    except BaseException:
        return True
    return alive is not False


class CohortJobSubmissionController:
    """Bound pending identities and one callback; no polling network or retries."""

    def __init__(self, *, max_pending=100):
        if type(max_pending) is not int or not 1 <= max_pending <= 1000:
            raise CohortJobSubmissionError
        self._max_pending = max_pending
        self._operations = {}
        self._by_identity = {}
        self._active = None

    @property
    def busy(self):
        return self._active is not None and _alive(self._active)

    def _owned(self, ticket):
        operation = self._operations.get(id(ticket))
        if operation is None or operation.ticket is not ticket:
            raise CohortJobSubmissionError
        return operation

    def begin(self, task_execution, *, prepared, handle, claim, timeout_seconds=30.0):
        """Capture independent scalars and retain ownership before Thread.start."""
        _outside_transaction()
        from django_ray.target.cohort_claim import CohortClaimDisposition
        from django_ray.target.cohort_claim_storage import CohortClaimRecord

        if (
            type(claim) is not CohortClaimRecord
            or claim.disposition is not CohortClaimDisposition.OPEN
            or claim.dispatched_at is None
            or claim.facts.identity != prepared.identity
            or claim.prepared_request_digest != prepared.request_digest
        ):
            raise CohortJobSubmissionError
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise CohortJobSubmissionError
        runner = RayJobRunner()
        source = runner._capture_cohort_submission(task_execution, prepared=prepared, handle=handle)
        prior = self._by_identity.get(prepared.identity)
        if prior is not None:
            operation = self._owned(prior)
            if operation.source != source or operation.claim != claim:
                raise CohortJobSubmissionError
            return prior
        if self.busy or len(self._operations) >= self._max_pending:
            return None
        wall, mono = _time()
        ticket = CohortJobSubmissionTicket(
            prepared.identity,
            prepared.request_digest,
            prepared.contract_digest,
            source.submission_id,
            source.jobs_endpoint,
        )
        operation = _Operation(
            ticket,
            source,
            runner,
            claim,
            wall,
            mono,
            wall + timedelta(seconds=timeout_seconds),
            mono + timeout_seconds,
            wall,
            mono,
        )
        self._operations[id(ticket)] = operation
        self._by_identity[ticket.identity] = ticket
        self._active = operation

        def run():
            last_wall, last_mono = wall, mono

            def fresh():
                nonlocal last_wall, last_mono
                now_wall, now_mono = _time()
                if (
                    operation.uncertainty is not None
                    or now_wall < last_wall
                    or now_mono < last_mono
                    or now_wall >= operation.deadline_wall
                    or now_mono >= operation.deadline_mono
                ):
                    raise CohortJobSubmissionError
                last_wall, last_mono = now_wall, now_mono

            try:
                fresh()
                manager = runner._prepare_cohort_submission(source)
                staged = manager.__enter__()
                try:
                    fresh()
                    if staged.source is not source:
                        raise CohortJobSubmissionError
                    operation.staged = staged
                    operation.prepared.set()
                    while not operation.authorized:
                        operation.wake.wait(0.05)
                        fresh()
                    fresh()
                    returned = runner._submit_prepared_cohort_submission(
                        staged, check_pending=fresh
                    )
                    fresh()
                    if (
                        type(returned) is not SubmissionHandle
                        or returned.ray_job_id != ticket.submission_id
                        or returned.ray_address != ticket.jobs_endpoint
                    ):
                        raise CohortJobSubmissionError
                finally:
                    manager.__exit__(*sys.exc_info())
                    # Set only after the context's actual cleanup returns.
                    operation.cleaned = True
                fresh()
                operation.accepted = True
            except BaseException:
                _uncertain(operation, "submission_unconfirmed")
            finally:
                operation.completed.set()

        try:
            operation.thread = Thread(target=run, daemon=True, name="django-ray-cohort-submit")
            operation.thread.start()
        except BaseException:
            _uncertain(operation, "callback_start_failed")
            if not _alive(operation):
                operation.cleaned = True
                operation.completed.set()
        return ticket

    def _fresh_parent(self, operation):
        try:
            wall, mono = _time()
            if wall < operation.parent_wall or mono < operation.parent_mono:
                raise CohortJobSubmissionError
            operation.parent_wall, operation.parent_mono = wall, mono
            if wall >= operation.deadline_wall or mono >= operation.deadline_mono:
                _uncertain(operation, "submission_deadline")
        except BaseException:
            _uncertain(operation, "submission_clock_unavailable")

    def poll(self, ticket):
        """Read only local state; deadline and clock failure remain sticky."""
        operation = self._owned(ticket)
        self._fresh_parent(operation)
        exited = operation.completed.is_set() and not _alive(operation)
        if operation.uncertainty is not None:
            stage = "uncertain"
        elif exited:
            stage = "finished"
        elif operation.authorized:
            stage = "submitting"
        elif operation.prepared.is_set():
            stage = "prepared"
        else:
            stage = "preparing"
        return CohortJobSubmissionResult(
            ticket,
            stage,
            exited and operation.accepted and operation.uncertainty is None,
            operation.uncertainty,
            exited and operation.cleaned,
        )

    def authorize(self, ticket, *, task_execution):
        """Attach only this owned prepared request in the parent, then wake submit."""
        _outside_transaction()
        operation = self._owned(ticket)
        self._fresh_parent(operation)
        if operation.uncertainty is not None:
            return False
        if operation.authorized:
            return True
        if not operation.prepared.is_set() or not _alive(operation):
            return False
        try:
            operation.runner._attach_cohort_submission(
                operation.staged, task_execution=task_execution, expected_claim=operation.claim
            )
            self._fresh_parent(operation)
            if operation.uncertainty is not None:
                return False
            operation.authorized = True
            operation.wake.set()
            return True
        except BaseException:
            _uncertain(operation, "attachment_unconfirmed")
            return False

    def abort(self, ticket):
        """Withhold authorization; never promise to stop an in-flight submission."""
        _uncertain(self._owned(ticket), "submission_aborted")

    def retire(self, ticket):
        """Release local bookkeeping only after callback exit; no remote proof."""
        operation = self._owned(ticket)
        if _alive(operation) or not operation.completed.is_set() or not operation.cleaned:
            return False
        del self._operations[id(ticket)]
        del self._by_identity[ticket.identity]
        if self._active is operation:
            self._active = None
        return True
