"""Finite candidate pages for advisory worker maintenance scans.

This bounds candidate materialization, not database execution time or the
payloads subsequently needed by an authoritative lifecycle operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import Case, F, TextField, Value, When
from django.db.models.functions import Cast, Substr

MAINTENANCE_PAGE_SIZE = 32
MAINTENANCE_CONTINUATION_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class MaintenanceCandidate:
    pk: int
    attempt_number: int
    execution_generation: int
    execution_protocol_version: int
    owner: str | None
    owner_complete: bool
    identity_complete: bool = True

    @property
    def usable(self) -> bool:
        return (
            self.identity_complete
            and self.owner_complete
            and (self.owner is None or (type(self.owner) is str and len(self.owner) <= 255))
        )

    def filters(self) -> dict[str, Any]:
        return {
            "pk": self.pk,
            "attempt_number": self.attempt_number,
            "execution_generation": self.execution_generation,
            "execution_protocol_version": self.execution_protocol_version,
            "claimed_by_worker": self.owner,
        }


@dataclass(slots=True)
class MaintenanceScan:
    scope: tuple | None = None
    after: int | None = None
    high_water: int | None = None
    page_end: int | None = None
    page_has_more: bool = False
    pending: bool = False

    def page(self, queryset, *, scope: tuple) -> list[MaintenanceCandidate]:
        if self.scope != scope:
            self.scope = scope
            self.after = self.high_water = None
        if self.high_water is None:
            self.high_water = queryset.order_by("-pk").values_list("pk", flat=True).first()
        if self.high_water is None:
            self.pending = False
            return []
        window = queryset.filter(pk__lte=self.high_water)
        if self.after is not None:
            window = window.filter(pk__gt=self.after)
        # SQLite does not enforce varchar lengths. Never materialize an entire
        # corrupt owner, or use its truncated prefix as ownership authority.
        identity_fields = ("attempt_number", "execution_generation", "execution_protocol_version")
        for field in identity_fields:
            # Integer affinity is not a type guarantee on SQLite. A malformed
            # text cell must not turn this small projection into a payload read.
            window = window.annotate(
                **{f"_maintenance_{field}": Substr(Cast(field, TextField()), 1, 21)}
            )
        rows = list(
            window.order_by("pk")
            .annotate(_maintenance_owner=Substr("claimed_by_worker", 1, 256))
            .annotate(
                _maintenance_owner_complete=Case(
                    When(claimed_by_worker__isnull=True, then=Value(True)),
                    When(claimed_by_worker=F("_maintenance_owner"), then=Value(True)),
                    default=Value(False),
                )
            )
            .values_list(
                "pk",
                "_maintenance_attempt_number",
                "_maintenance_execution_generation",
                "_maintenance_execution_protocol_version",
                "_maintenance_owner",
                "_maintenance_owner_complete",
            )[: MAINTENANCE_PAGE_SIZE + 1]
        )
        self.page_has_more = len(rows) > MAINTENANCE_PAGE_SIZE
        selected = rows[:MAINTENANCE_PAGE_SIZE]
        self.page_end = selected[-1][0] if selected else None
        self.pending = bool(selected)
        if not selected:
            self.after = self.high_water = None
        candidates = []
        for pk, *values, owner, owner_complete in selected:
            valid = all(
                type(value) is str
                and value.isascii()
                and value.isdecimal()
                and len(value) <= 19
                and int(value) <= 2**63 - 1
                for value in values
            )
            numbers = [int(value) for value in values] if valid else [0, 0, 0]
            candidates.append(MaintenanceCandidate(pk, *numbers, owner, owner_complete, valid))
        return candidates

    def advance(self, candidate: MaintenanceCandidate) -> None:
        """Consider skipped/errors too, but never skip an unvisited suffix."""
        self.after = candidate.pk
        if candidate.pk == self.page_end and not self.page_has_more:
            self.after = self.high_water = None
            self.pending = False
