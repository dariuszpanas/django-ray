"""Responsive, single-callback preparation with parent-only database commit.

The parent captures immutable task/environment/claim scalars before starting the
fixed filesystem preparation function. The callback never receives an ORM model
or permission to submit work. A deadline discards readiness; it cannot kill a
thread or prove cleanup. The owner must hold or settle its claim before retiring
an abandoned ticket. Durable claim/revision checks remain authoritative.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from queue import Empty, Queue
from threading import Thread

from django.db import connections

from django_ray.execution_codec import ExecutionIdentity, is_valid_execution_identity
from django_ray.runner import cohort_dispatch as dispatch
from django_ray.runner.cohort_claims import ClaimedCohortTask
from django_ray.target.cohort_claim import CohortClaimFacts
from django_ray.target.cohort_claim_storage import CohortClaimRecord
from django_ray.target.cohort_transport import PreparedCohortExecution


class CohortPreparationReason(StrEnum):
    INVALID = "invalid"
    WRONG_OWNER = "wrong_owner"
    TRANSACTION = "transaction"
    BUSY = "busy"
    STALE = "stale"
    DEADLINE = "deadline"
    CLOCK_FAILED = "clock_failed"
    CLOCK_REGRESSION = "clock_regression"
    CAPTURE_FAILED = "capture_failed"
    START_FAILED = "start_failed"
    PREPARATION_FAILED = "preparation_failed"
    COMMIT_FAILED = "commit_failed"
    ABORTED = "aborted"


class CohortPreparationError(RuntimeError):
    def __init__(self, reason: CohortPreparationReason):
        self.reason = reason
        super().__init__(f"Current-cohort preparation refused: {reason.value}")


@dataclass(frozen=True, slots=True, eq=False)
class CohortPreparationTicket:
    epoch: int
    identity: ExecutionIdentity = field(repr=False)
    claim: CohortClaimRecord = field(repr=False)
    deadline_wall: datetime
    deadline_mono: float


@dataclass(frozen=True, slots=True)
class CohortPreparationSnapshot:
    ticket: CohortPreparationTicket
    stage: str
    reason: CohortPreparationReason | None
    local_cleanup_complete: bool


@dataclass(slots=True)
class _Operation:
    ticket: CohortPreparationTicket
    claimed: ClaimedCohortTask = field(repr=False)
    source: dispatch.CohortPreparationInput = field(repr=False)
    mailbox: Queue = field(default_factory=lambda: Queue(maxsize=1), repr=False)
    thread: Thread | None = field(default=None, repr=False)
    prepared: PreparedCohortExecution | None = field(default=None, repr=False)
    committed: dispatch.PreparedCohortDispatch | None = field(default=None, repr=False)
    stage: str = "preparing"
    reason: CohortPreparationReason | None = None
    commit_attempted: bool = False


def _reject(reason):
    raise CohortPreparationError(reason) from None


def _clock():
    return datetime.now(UTC), time.monotonic()


def _outside_transaction():
    try:
        active = any(
            connection.in_atomic_block
            or (connection.connection is not None and connection.autocommit is not True)
            for connection in connections.all(initialized_only=True)
        )
    except Exception:
        _reject(CohortPreparationReason.TRANSACTION)
    if active:
        _reject(CohortPreparationReason.TRANSACTION)


def _owned(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if threading.get_ident() != self._parent_thread:
            _reject(CohortPreparationReason.WRONG_OWNER)
        if self._accessing:
            _reject(CohortPreparationReason.BUSY)
        _outside_transaction()
        self._accessing = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self._accessing = False

    return guarded


def _alive(operation):
    if operation.thread is None:
        return False
    try:
        return operation.thread.is_alive() is not False
    except BaseException:
        # Unknown thread ownership cannot authorize replacement or retirement.
        return True


def _prepare(source, mailbox):
    # These are the only callback arguments: no controller, operation or ORM.
    try:
        prepared = dispatch.prepare_captured_cohort_execution(source)
        if type(prepared) is not PreparedCohortExecution:
            prepared = None
    except BaseException:
        prepared = None
    mailbox.put_nowait(prepared)


class CohortPreparationController:
    """At most 100 retained tickets and one running preparation callback.

    Polling reads local state and never waits, joins or prepares a manifest.
    Commit is attempted once, only on the parent after fresh result acceptance.
    Once the database commit returns, its new claim is retained and returned;
    elapsed time cannot hide a committed revision from the worker.
    """

    def __init__(self, *, max_pending=100):
        if type(max_pending) is not int or not 1 <= max_pending <= 100:
            _reject(CohortPreparationReason.INVALID)
        self._max_pending = max_pending
        self._parent_thread = threading.get_ident()
        self._operations: dict[int, _Operation] = {}
        self._by_identity: dict[ExecutionIdentity, CohortPreparationTicket] = {}
        self._active: _Operation | None = None
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None
        self._epoch = 0
        self._accessing = False

    def _current(self, ticket):
        operation = self._operations.get(id(ticket))
        if operation is None or operation.ticket is not ticket:
            _reject(CohortPreparationReason.STALE)
        return operation

    @staticmethod
    def _block(operation, reason):
        if operation.stage == "committed":
            return
        operation.stage = "blocked"
        if operation.reason is None:
            operation.reason = reason
        operation.prepared = None

    def _time(self, operation=None):
        reason = CohortPreparationReason.CLOCK_FAILED
        try:
            wall, mono = _clock()
            if (
                type(wall) is not datetime
                or wall.tzinfo is None
                or wall.utcoffset() != timedelta(0)
                or type(mono) not in (int, float)
                or not math.isfinite(mono)
                or mono < 0
            ):
                raise ValueError
            if (self._last_wall is not None and wall < self._last_wall) or (
                self._last_mono is not None and mono < self._last_mono
            ):
                reason = CohortPreparationReason.CLOCK_REGRESSION
                raise ValueError
        except BaseException:
            if operation is None:
                _reject(reason)
            self._block(operation, reason)
            return None
        self._last_wall, self._last_mono = wall, float(mono)
        if operation is not None and (
            wall >= operation.ticket.deadline_wall or mono >= operation.ticket.deadline_mono
        ):
            self._block(operation, CohortPreparationReason.DEADLINE)
        return wall, float(mono)

    @property
    @_owned
    def busy(self):
        return self._active is not None and _alive(self._active)

    @property
    @_owned
    def pending_count(self):
        return len(self._operations)

    @_owned
    def begin(self, claimed, *, transport=None, timeout_seconds=30):
        if (
            type(claimed) is not ClaimedCohortTask
            or type(claimed.claim) is not CohortClaimRecord
            or type(claimed.claim.facts) is not CohortClaimFacts
            or not is_valid_execution_identity(claimed.claim.facts.identity)
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            _reject(CohortPreparationReason.INVALID)
        identity = claimed.claim.facts.identity
        prior = self._by_identity.get(identity)
        if prior is not None:
            operation = self._current(prior)
            if prior.claim != claimed.claim or operation.source.transport != transport:
                _reject(CohortPreparationReason.STALE)
            return prior
        if (self._active is not None and _alive(self._active)) or len(
            self._operations
        ) >= self._max_pending:
            return None
        before = self._time()
        assert before is not None
        try:
            source = dispatch.capture_claimed_cohort_preparation(claimed, transport=transport)
            if type(source) is not dispatch.CohortPreparationInput or source.claim != claimed.claim:
                raise ValueError
            if self._epoch >= (1 << 63) - 1:
                raise ValueError
            deadline_wall = before[0] + timedelta(seconds=timeout_seconds)
            deadline_mono = before[1] + timeout_seconds
            if not math.isfinite(deadline_mono):
                raise ValueError
        except BaseException:
            _reject(CohortPreparationReason.CAPTURE_FAILED)
        self._epoch += 1
        ticket = CohortPreparationTicket(
            self._epoch, source.claim.facts.identity, source.claim, deadline_wall, deadline_mono
        )
        operation = _Operation(ticket, claimed, source)
        self._operations[id(ticket)] = operation
        self._by_identity[ticket.identity] = ticket
        self._active = operation
        self._time(operation)
        if operation.reason is not None:
            return ticket
        try:
            operation.thread = Thread(
                target=_prepare,
                args=(source, operation.mailbox),
                name="django-ray-cohort-prepare",
                daemon=True,
            )
            operation.thread.start()
        except BaseException:
            self._block(operation, CohortPreparationReason.START_FAILED)
        return ticket

    def _snapshot(self, operation):
        return CohortPreparationSnapshot(
            operation.ticket, operation.stage, operation.reason, not _alive(operation)
        )

    def _poll(self, operation):
        if operation.stage == "committed":
            return self._snapshot(operation)
        self._time(operation)
        if _alive(operation):
            return self._snapshot(operation)
        prepared = operation.prepared
        if operation.stage == "preparing":
            try:
                prepared = operation.mailbox.get_nowait()
            except Empty:
                prepared = None
        self._time(operation)
        if operation.reason is None:
            if type(prepared) is PreparedCohortExecution:
                operation.prepared = prepared
                operation.stage = "ready"
            else:
                self._block(operation, CohortPreparationReason.PREPARATION_FAILED)
        return CohortPreparationSnapshot(operation.ticket, operation.stage, operation.reason, True)

    @_owned
    def poll(self, ticket):
        return self._poll(self._current(ticket))

    @_owned
    def commit(self, ticket, *, now=None):
        operation = self._current(ticket)
        self._poll(operation)
        if operation.commit_attempted or operation.stage != "ready":
            _reject(operation.reason or CohortPreparationReason.BUSY)
        operation.commit_attempted = True
        try:
            result = dispatch.commit_claimed_cohort_preparation(
                operation.claimed, operation.source, operation.prepared, now=now
            )
        except BaseException:
            self._block(operation, CohortPreparationReason.COMMIT_FAILED)
            _reject(CohortPreparationReason.COMMIT_FAILED)
        operation.committed = result
        operation.stage = "committed"
        operation.prepared = None
        return result

    @_owned
    def abort(self, ticket):
        operation = self._current(ticket)
        self._block(operation, CohortPreparationReason.ABORTED)
        return self._snapshot(operation)

    @_owned
    def retire(self, ticket):
        operation = self._current(ticket)
        if _alive(operation):
            return False
        del self._operations[id(ticket)]
        del self._by_identity[ticket.identity]
        if self._active is operation:
            self._active = None
        return True
