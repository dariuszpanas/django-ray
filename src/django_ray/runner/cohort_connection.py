"""Responsive ownership of one same-process Core connection epoch.

The worker acquires and heartbeats its lease before ``begin``. Its trusted
connect callback initializes only the selected default Ray context; it must not
claim tasks, write database state, or detach native work into another thread.
A successful callback is local connection readiness, never cohort qualification.
Its creator thread remains alive until verified disconnect: Linux parent-death
signals used by a locally started Ray cluster follow that thread's lifetime.

Only one callback may run. Deadlines limit result acceptance, not native call
duration: no thread is killed and no later connection replaces uncertain work.
Cleanup must verify the exact local disconnect. The outer owner must separately
gate probe/task quiescence and serialize all Ray context changes; neither thread
exit nor local disconnect proves remote cleanup, completion, or safe retry.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from queue import Empty, Queue
from threading import Thread

_MAX_EPOCH = (1 << 63) - 1


class CoreConnectionReason(StrEnum):
    INVALID = "invalid"
    WRONG_OWNER = "wrong_owner"
    BUSY = "busy"
    STALE = "stale"
    DEADLINE = "deadline"
    CLOCK_FAILED = "clock_failed"
    CLOCK_REGRESSION = "clock_regression"
    CONNECT_FAILED = "connect_failed"
    CLEANUP_FAILED = "cleanup_failed"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"


class CoreConnectionError(RuntimeError):
    def __init__(self, reason: CoreConnectionReason):
        self.reason = reason
        super().__init__(f"Core connection refused: {reason.value}")


class CoreConnectionPhase(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BLOCKED = "blocked"
    CLEANING = "cleaning"
    CLEANED = "cleaned"


@dataclass(frozen=True, slots=True, eq=False)
class CoreConnectionTicket:
    """Identity-bound local ticket; reconstructed equal values grant nothing."""

    epoch: int
    configuration_digest: str
    deadline: float


@dataclass(frozen=True, slots=True)
class CoreConnectionSnapshot:
    ticket: CoreConnectionTicket
    phase: CoreConnectionPhase
    reason: CoreConnectionReason | None
    callback_running: bool


@dataclass(slots=True)
class _Operation:
    ticket: CoreConnectionTicket
    phase: CoreConnectionPhase
    deadline: float
    thread: Thread | None = field(default=None, repr=False)
    results: Queue[bool] = field(default_factory=lambda: Queue(maxsize=1), repr=False)
    commands: Queue[Callable[[], bool] | None] = field(
        default_factory=lambda: Queue(maxsize=1), repr=False
    )
    callback_done: threading.Event = field(default_factory=threading.Event, repr=False)
    release_requested: bool = False
    reason: CoreConnectionReason | None = None


def _reject(reason: CoreConnectionReason):
    raise CoreConnectionError(reason) from None


def _owned(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        self._parent()
        if self._accessing:
            _reject(CoreConnectionReason.BUSY)
        self._accessing = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self._accessing = False

    return guarded


class CoreConnectionLifecycle:
    """Parent-only begin/poll/cleanup; polling never waits or runs callbacks.

    The parent may construct/use its existing RayCoreRunner after CONNECTED and
    independent qualification. Callbacks stay in the same process so the default
    Ray context remains available to that parent. Thread-local nondefault Client
    contexts and out-of-band init/shutdown calls are unsupported.
    """

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic):
        if not callable(monotonic):
            _reject(CoreConnectionReason.INVALID)
        self._owner = threading.get_ident()
        self._monotonic = monotonic
        self._last_time: float | None = None
        self._epoch = 0
        self._operation: _Operation | None = None
        self._accessing = False

    def _parent(self):
        if threading.get_ident() != self._owner:
            _reject(CoreConnectionReason.WRONG_OWNER)

    def _current(self, ticket):
        self._parent()
        if self._operation is None or self._operation.ticket is not ticket:
            _reject(CoreConnectionReason.STALE)
        return self._operation

    @staticmethod
    def _block(operation, reason):
        operation.phase = CoreConnectionPhase.BLOCKED
        if operation.reason is None:
            operation.reason = reason

    def _now(self, operation=None):
        reason = CoreConnectionReason.CLOCK_FAILED
        try:
            value = self._monotonic()
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError
            if self._last_time is not None and value < self._last_time:
                reason = CoreConnectionReason.CLOCK_REGRESSION
                raise ValueError
        except Exception:
            if operation is None:
                _reject(reason)
            self._block(operation, reason)
            return None
        self._last_time = float(value)
        return float(value)

    @staticmethod
    def _timeout(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 600:
            _reject(CoreConnectionReason.INVALID)
        return float(value)

    @staticmethod
    def _owner_running(operation):
        return operation.thread is not None and operation.thread.is_alive()

    @classmethod
    def _running(cls, operation):
        # An idle connection owner must stay alive without preventing the parent
        # from qualifying/using the context or requesting serialized cleanup.
        return cls._owner_running(operation) and (
            operation.release_requested or not operation.callback_done.is_set()
        )

    def _snapshot(self, operation):
        return CoreConnectionSnapshot(
            operation.ticket, operation.phase, operation.reason, self._running(operation)
        )

    @property
    @_owned
    def outstanding(self) -> CoreConnectionTicket | None:
        self._parent()
        return self._operation.ticket if self._operation is not None else None

    def _start(self, operation, callback, *, cleanup):
        # One retained owner and bounded command/result mailboxes per epoch.
        # Cleanup runs on the creator too; exiting it earlier can kill Ray's
        # GCS/raylet children even while the manager process remains healthy.
        def invoke(function, *, cleanup):
            try:
                result = function()
                succeeded = result is True if cleanup else result is None
            except BaseException:
                succeeded = False
            operation.results.put_nowait(succeeded)
            operation.callback_done.set()

        def execute():
            invoke(callback, cleanup=cleanup)
            while True:
                command = operation.commands.get()
                if command is None:
                    return
                invoke(command, cleanup=True)

        try:
            operation.callback_done.clear()
            operation.thread = Thread(
                target=execute, name="django-ray-core-connection", daemon=True
            )
            operation.thread.start()
        except Exception:
            self._block(
                operation,
                CoreConnectionReason.CLEANUP_FAILED
                if cleanup
                else CoreConnectionReason.CONNECT_FAILED,
            )

    @_owned
    def begin(
        self,
        configuration_digest: str,
        *,
        connect: Callable[[], None],
        timeout_seconds: float = 30,
    ) -> CoreConnectionTicket:
        self._parent()
        if self._operation is not None:
            _reject(CoreConnectionReason.BUSY)
        if (
            type(configuration_digest) is not str
            or len(configuration_digest) != 71
            or not configuration_digest.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in configuration_digest[7:])
            or not callable(connect)
            or self._epoch >= _MAX_EPOCH
        ):
            _reject(CoreConnectionReason.INVALID)
        timeout = self._timeout(timeout_seconds)
        now = self._now()
        assert now is not None
        if not math.isfinite(now + timeout):
            _reject(CoreConnectionReason.INVALID)
        self._epoch += 1
        ticket = CoreConnectionTicket(self._epoch, configuration_digest, now + timeout)
        operation = _Operation(ticket, CoreConnectionPhase.CONNECTING, ticket.deadline)
        self._operation = operation
        self._start(operation, connect, cleanup=False)
        return ticket

    @_owned
    def poll(self, ticket: CoreConnectionTicket) -> CoreConnectionSnapshot:
        operation = self._current(ticket)
        if operation.release_requested:
            # Exact disconnect was already accepted. Retain the ticket until
            # the creator actually exits; polling never joins that thread.
            if not self._owner_running(operation):
                return self._cleaned(operation)
            return self._snapshot(operation)
        now = self._now(operation)
        pending = operation.phase in (CoreConnectionPhase.CONNECTING, CoreConnectionPhase.CLEANING)
        if pending and now is not None and now >= operation.deadline:
            self._block(operation, CoreConnectionReason.DEADLINE)
        if not self._owner_running(operation):
            self._block(
                operation,
                CoreConnectionReason.CLEANUP_FAILED
                if operation.phase is CoreConnectionPhase.CLEANING
                else CoreConnectionReason.CONNECT_FAILED,
            )
            return self._snapshot(operation)
        if self._running(operation) or operation.phase not in (
            CoreConnectionPhase.CONNECTING,
            CoreConnectionPhase.CLEANING,
        ):
            return self._snapshot(operation)
        try:
            succeeded = operation.results.get_nowait()
        except Empty:
            succeeded = False
        # Recheck after observing callback completion and its bounded mailbox. A late
        # successful callback cannot become usable because the first clock was fresh.
        now = self._now(operation)
        if now is None or operation.phase is CoreConnectionPhase.BLOCKED:
            return self._snapshot(operation)
        if now >= operation.deadline:
            self._block(operation, CoreConnectionReason.DEADLINE)
        elif not succeeded:
            self._block(
                operation,
                CoreConnectionReason.CLEANUP_FAILED
                if operation.phase is CoreConnectionPhase.CLEANING
                else CoreConnectionReason.CONNECT_FAILED,
            )
        elif operation.phase is CoreConnectionPhase.CLEANING:
            return self._release(operation)
        else:
            operation.phase = CoreConnectionPhase.CONNECTED
        return self._snapshot(operation)

    @_owned
    def invalidate(self, ticket: CoreConnectionTicket) -> CoreConnectionSnapshot:
        """Lease loss/shutdown discards readiness while retaining exact ownership."""
        operation = self._current(ticket)
        self._block(operation, CoreConnectionReason.STALE)
        return self._snapshot(operation)

    @_owned
    def begin_cleanup(
        self,
        ticket: CoreConnectionTicket,
        *,
        cleanup: Callable[[], bool],
        timeout_seconds: float = 30,
    ) -> None:
        operation = self._current(ticket)
        if (
            self._running(operation)
            or operation.release_requested
            or operation.phase is CoreConnectionPhase.CLEANING
        ):
            _reject(CoreConnectionReason.BUSY)
        if not callable(cleanup):
            _reject(CoreConnectionReason.INVALID)
        timeout = self._timeout(timeout_seconds)
        now = self._now(operation)
        if now is not None:
            if not math.isfinite(now + timeout):
                _reject(CoreConnectionReason.INVALID)
            operation.deadline = now + timeout
            operation.phase = CoreConnectionPhase.CLEANING
        # A broken clock may prevent accepting success, but must not prevent
        # exact local cleanup. Such an attempt needs independent confirmation.
        operation.results = Queue(maxsize=1)
        if self._owner_running(operation):
            operation.callback_done.clear()
            operation.commands.put_nowait(cleanup)
        else:
            # A thread that failed to start (or unexpectedly exited) cannot
            # overlap cleanup; uncertainty about the context still needs proof.
            # Discard any command it died before consuming. A replacement
            # cleanup owner may run only this newly authorized callback.
            operation.commands = Queue(maxsize=1)
            self._start(operation, cleanup, cleanup=True)

    def _release(self, operation):
        operation.release_requested = True
        operation.phase = CoreConnectionPhase.CLEANING
        if self._owner_running(operation):
            operation.commands.put_nowait(None)
            return self._snapshot(operation)
        return self._cleaned(operation)

    def _cleaned(self, operation):
        operation.phase = CoreConnectionPhase.CLEANED
        snapshot = self._snapshot(operation)
        self._operation = None
        return snapshot

    @_owned
    def confirm_cleanup(
        self, ticket: CoreConnectionTicket, *, independently_confirmed: bool
    ) -> CoreConnectionSnapshot:
        """Accept trusted local disconnect evidence, including after a deadline.

        This flag acknowledges an independent exact-context check by the owner;
        never derive it from a caller request, thread exit, or Ray shutdown ACK.
        Remote probe/task quiescence remains a separate prerequisite owned by the
        caller. A bad clock cannot prevent retiring proven local ownership, but
        it still prevents beginning another connection until the clock recovers.
        Poll again after CLEANING until the retained creator has actually exited.
        """
        operation = self._current(ticket)
        if self._running(operation):
            _reject(CoreConnectionReason.BUSY)
        if independently_confirmed is not True:
            _reject(CoreConnectionReason.CLEANUP_UNCONFIRMED)
        return self._release(operation)
