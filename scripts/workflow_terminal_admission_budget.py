"""Coordinator-only reservation model; not wired into production execution.

K charged reservations cover at most K logical metadata results and K collector
calls. Each channel is bounded by the charged byte budget B. This is not a bound
on physical Ray transport copies, process memory or lineage reconstruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

STATES = ("result", "ready", "submitted", "uncertain_result", "uncertain_submission")
COUNTERS = (
    "requested",
    "admitted",
    "denied",
    "released",
    "omitted",
    "submitted",
    "acknowledged",
    "actor_rejected",
    "uncertain",
    "invalid",
)


@dataclass(frozen=True, eq=False, slots=True)
class Reservation:
    """Identity is local object identity; copied or foreign tickets cannot act."""

    capacity: int


@dataclass(slots=True)
class _Flight:
    state: str = "result"
    wire: bytes | None = None


class TerminalAdmissionBudget:
    """Keep capacity charged until known omission or receiver acknowledgement."""

    def __init__(self, *, max_calls: int, max_bytes: int, counter_max: int = (1 << 63) - 1):
        if any(
            type(value) is not int or value <= 0 for value in (max_calls, max_bytes, counter_max)
        ):
            raise ValueError("Admission bounds must be positive integers")
        if max(max_calls, max_bytes) > counter_max:
            raise ValueError("Admission bounds exceed the diagnostic counter range")
        self.max_calls = max_calls
        self.max_bytes = max_bytes
        self._counter_max = counter_max
        self._flights: dict[Reservation, _Flight] = {}
        self._bytes = 0
        self._peak_calls = 0
        self._peak_bytes = 0
        self._counters = dict.fromkeys(COUNTERS, 0)
        self._saturated = False
        self._lock = RLock()

    def _count(self, key: str) -> None:
        value = self._counters[key] + 1
        self._saturated |= value > self._counter_max
        self._counters[key] = min(value, self._counter_max)

    def reserve(self, capacity: int) -> Reservation | None:
        with self._lock:
            self._count("requested")
            if (
                type(capacity) is not int
                or capacity <= 0
                or len(self._flights) >= self.max_calls
                or capacity > self.max_bytes - self._bytes
            ):
                self._count("denied")
                return None
            ticket = Reservation(capacity)
            self._flights[ticket] = _Flight()
            self._bytes += capacity
            self._peak_calls = max(self._peak_calls, len(self._flights))
            self._peak_bytes = max(self._peak_bytes, self._bytes)
            self._count("admitted")
            return ticket

    def _release(self, ticket: Reservation) -> None:
        del self._flights[ticket]
        self._bytes -= ticket.capacity
        self._count("released")

    def result(self, ticket: Reservation, wire: bytes | None) -> bool:
        with self._lock:
            flight = self._flights.get(ticket)
            if flight is None or flight.state not in {"result", "uncertain_result"}:
                self._count("invalid")
                return False
            if wire is None:
                self._count("omitted")
                self._release(ticket)
                return True
            if type(wire) is not bytes or len(wire) > ticket.capacity:
                self._count("invalid")
                # Do not retain an unbounded value or free an uncertain slot.
                flight.state = "uncertain_result"
                return False
            flight.wire = wire
            flight.state = "ready"
            return True

    def submit(self, ticket: Reservation) -> bytes | None:
        with self._lock:
            flight = self._flights.get(ticket)
            if flight is None or flight.state != "ready":
                self._count("invalid")
                return None
            flight.state = "submitted"
            self._count("submitted")
            return flight.wire

    def uncertain(self, ticket: Reservation) -> bool:
        with self._lock:
            flight = self._flights.get(ticket)
            if flight is None or flight.state == "ready":
                self._count("invalid")
                return False
            if flight.state.startswith("uncertain_"):
                return True
            flight.state = (
                "uncertain_result" if flight.state == "result" else "uncertain_submission"
            )
            self._count("uncertain")
            return True

    def acknowledge(self, ticket: Reservation, *, accepted: bool) -> bool:
        with self._lock:
            flight = self._flights.get(ticket)
            if (
                type(accepted) is not bool
                or flight is None
                or flight.state not in {"submitted", "uncertain_submission"}
            ):
                self._count("invalid")
                return False
            self._count("acknowledged" if accepted else "actor_rejected")
            self._release(ticket)
            return True

    def snapshot(self) -> dict:
        with self._lock:
            phases = dict.fromkeys(STATES, 0)
            for flight in self._flights.values():
                phases[flight.state] += 1
            return {
                "schema_version": 1,
                "saturated": self._saturated,
                "limits": {"calls": self.max_calls, "bytes": self.max_bytes},
                "active": {"calls": len(self._flights), "bytes": self._bytes, "phases": phases},
                "peak": {"calls": self._peak_calls, "bytes": self._peak_bytes},
                "counters": dict(self._counters),
            }
