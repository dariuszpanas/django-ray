"""One owned Core callback on an unchanged existing connection.

The parent has already committed preparation and dispatch. It retains that claim
and its connection ticket until this callback exits. Local cleanup/exit never
proves remote completion or permits repeating a dispatched generation.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event, Thread, get_ident
from typing import Any

from django_ray.execution_codec import ExecutionIdentity
from django_ray.runner.cohort_connection import CoreConnectionTicket
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner


class CohortCoreSubmissionError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort Core submission unavailable")


@dataclass(frozen=True, slots=True, eq=False)
class CohortCoreSubmissionTicket:
    identity: ExecutionIdentity
    request_digest: str
    contract_digest: str
    connection: CoreConnectionTicket = field(repr=False)


@dataclass(frozen=True, slots=True)
class CohortCoreSubmissionResult:
    ticket: CohortCoreSubmissionTicket
    stage: str
    accepted: bool
    uncertainty: str | None
    handle: RayCoreHandle | None = field(repr=False)
    local_cleanup_complete: bool


@dataclass
class _Operation:
    ticket: CohortCoreSubmissionTicket
    source: Any = field(repr=False)
    runner: RayCoreRunner = field(repr=False)
    wall: datetime
    mono: float
    deadline_wall: datetime
    deadline_mono: float
    completed: Event = field(default_factory=Event, repr=False)
    thread: Any = field(default=None, repr=False)
    handle: RayCoreHandle | None = field(default=None, repr=False)
    cleaned: bool = True
    accepted: bool = False
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
        raise CohortCoreSubmissionError
    return wall, float(mono)


def _outside_transaction():
    from django.db import connections

    if any(
        item.in_atomic_block or (item.connection is not None and item.autocommit is not True)
        for item in connections.all(initialized_only=True)
    ):
        raise CohortCoreSubmissionError


def _uncertain(operation, reason):
    if operation.uncertainty is None:
        operation.uncertainty = reason


def _alive(operation):
    if operation.thread is None:
        return False
    try:
        return operation.thread.is_alive() is not False
    except BaseException:
        return True


class CohortCoreSubmissionController:
    """One parent and one retained callback; poll never touches Ray or the DB."""

    def __init__(self):
        self._parent = get_ident()
        self._operation: _Operation | None = None

    def _owner(self):
        if get_ident() != self._parent:
            raise CohortCoreSubmissionError

    def _owned(self, ticket):
        self._owner()
        operation = self._operation
        if operation is None or operation.ticket is not ticket:
            raise CohortCoreSubmissionError
        return operation

    @property
    def busy(self):
        self._owner()
        # Even an exited callback keeps its slot until the parent consumes its
        # outcome and proves that local snapshot cleanup actually completed.
        return self._operation is not None

    def begin(self, runner, task_execution, *, prepared, connection_ticket, timeout_seconds=30.0):
        self._owner()
        _outside_transaction()
        from django_ray.target.cohort_contract import _digest

        if (
            type(runner) is not RayCoreRunner
            or type(connection_ticket) is not CoreConnectionTicket
            or type(connection_ticket.epoch) is not int
            or not 1 <= connection_ticket.epoch < 1 << 63
            or type(connection_ticket.deadline) not in {int, float}
            or not math.isfinite(connection_ticket.deadline)
            or connection_ticket.deadline < 0
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise CohortCoreSubmissionError
        try:
            _digest(connection_ticket.configuration_digest)
            source = runner._capture_cohort_submission(task_execution, prepared=prepared)
        except Exception:
            raise CohortCoreSubmissionError from None
        if self._operation is not None:
            operation = self._operation
            if operation.ticket.identity == prepared.identity:
                if (
                    operation.runner is not runner
                    or operation.ticket.connection is not connection_ticket
                    or operation.source != source
                ):
                    raise CohortCoreSubmissionError
                return operation.ticket
            return None
        if runner._pending_tasks.get(prepared.identity.task_execution_pk) is not None:
            raise CohortCoreSubmissionError
        try:
            wall, mono = _time()
            deadline_wall = wall + timedelta(seconds=timeout_seconds)
            deadline_mono = mono + timeout_seconds
            if not math.isfinite(deadline_mono):
                raise CohortCoreSubmissionError
        except Exception:
            raise CohortCoreSubmissionError from None
        ticket = CohortCoreSubmissionTicket(
            prepared.identity, prepared.request_digest, prepared.contract_digest, connection_ticket
        )
        operation = _Operation(
            ticket,
            source,
            runner,
            wall,
            mono,
            deadline_wall,
            deadline_mono,
        )
        self._operation = operation

        def run():
            last_wall, last_mono = wall, mono

            def fresh():
                nonlocal last_wall, last_mono
                current_wall, current_mono = _time()
                if (
                    operation.uncertainty is not None
                    or current_wall < last_wall
                    or current_mono < last_mono
                    or current_wall >= operation.deadline_wall
                    or current_mono >= operation.deadline_mono
                ):
                    raise CohortCoreSubmissionError
                last_wall, last_mono = current_wall, current_mono

            def retain(handle):
                # The runner invokes this immediately after .remote returns,
                # before any diagnostic RPC or post-submission deadline check.
                operation.handle = handle

            def cleanup(complete):
                operation.cleaned = complete is True

            try:
                fresh()
                returned = runner._submit_captured_cohort_submission(
                    source, check_pending=fresh, on_handle=retain, on_cleanup=cleanup
                )
                fresh()
                if (
                    type(returned) is not RayCoreHandle
                    or returned is not operation.handle
                    or returned.cohort_prepared is not prepared
                ):
                    raise CohortCoreSubmissionError
                operation.accepted = True
            except BaseException:
                _uncertain(operation, "submission_unconfirmed")
            finally:
                operation.completed.set()

        try:
            operation.thread = Thread(target=run, daemon=True, name="django-ray-cohort-core-submit")
            operation.thread.start()
        except BaseException:
            _uncertain(operation, "callback_start_failed")
            if not _alive(operation):
                operation.completed.set()
        return ticket

    def poll(self, ticket, *, connection_ticket):
        operation = self._owned(ticket)
        if operation.ticket.connection is not connection_ticket:
            _uncertain(operation, "connection_changed")
        try:
            wall, mono = _time()
            if wall < operation.wall or mono < operation.mono:
                raise CohortCoreSubmissionError
            operation.wall, operation.mono = wall, mono
            if wall >= operation.deadline_wall or mono >= operation.deadline_mono:
                _uncertain(operation, "submission_deadline")
        except BaseException:
            _uncertain(operation, "submission_clock_unavailable")
        exited = operation.completed.is_set() and not _alive(operation)
        return CohortCoreSubmissionResult(
            ticket,
            "uncertain" if operation.uncertainty else "finished" if exited else "submitting",
            exited and operation.cleaned and operation.accepted and operation.uncertainty is None,
            operation.uncertainty,
            # The callback can enrich its immutable handle after the first
            # retained ObjectRef. Expose only its final retained handle.
            operation.handle if exited else None,
            exited and operation.cleaned,
        )

    def abort(self, ticket):
        _uncertain(self._owned(ticket), "submission_aborted")

    def retire(self, ticket):
        operation = self._owned(ticket)
        if not operation.completed.is_set() or _alive(operation) or not operation.cleaned:
            return False
        self._operation = None
        return True
