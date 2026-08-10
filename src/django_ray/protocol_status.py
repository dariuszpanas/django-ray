"""Bounded read-only reporting for the execution-protocol rollout boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.db.models import Case, CharField, Count, Exists, F, OuterRef, Q, Value, When
from django.db.models.functions import Length
from django.db.utils import ConnectionDoesNotExist
from django.utils import timezone

from django_ray.execution_protocol import (
    LEGACY_EXECUTION_METADATA_SCHEMA_VERSION,
    LEGACY_EXECUTION_PROTOCOL_VERSION,
    LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
    PROTOCOL_POLICY_SCHEMA_VERSION,
    WORKER_CAPABILITY_SCHEMA_VERSION,
)
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)
from django_ray.redaction import normalize_terminal_text, redact_text
from django_ray.runner.leasing import get_lease_duration

PROTOCOL_STATUS_SCHEMA = "django-ray.protocol-status"
PROTOCOL_STATUS_SCHEMA_VERSION = 1
PROTOCOL_STATUS_GROUP_LIMIT = 64
PROTOCOL_STATUS_OUTPUT_MAX_BYTES = 65_536

_OUTPUT_CONTENT_MAX_BYTES = PROTOCOL_STATUS_OUTPUT_MAX_BYTES - 1
_POLICY_KEY = 1
_LEGACY_TOKEN_KEY = 1
_MAX_POLICY_REVISION = (1 << 63) - 1
_SUPPORTED_DATABASE_VENDORS = frozenset({"postgresql", "sqlite"})
_NONTERMINAL_STATES = (
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.CANCELLING,
)


class ProtocolStatusError(RuntimeError):
    """The rollout report could not be produced safely."""


class ProtocolStatusBlockerCode(StrEnum):
    """Stable redaction-safe blocker vocabulary for the v1 rollout report."""

    ACTIVE_LEGACY_LEASES = "active_legacy_leases"
    ACTIVE_UPGRADED_LEASES = "active_upgraded_leases"
    LEGACY_METADATA_PROVENANCE_UNATTESTED = "legacy_metadata_provenance_unattested"
    LEGACY_PRODUCERS_UNATTESTED = "legacy_producers_unattested"
    LEGACY_READERS_UNATTESTED = "legacy_readers_unattested"
    NO_UPGRADED_READER_CAPACITY = "no_upgraded_reader_capacity"
    NON_V1_NONTERMINAL_WORK = "non_v1_nonterminal_work"
    POLICY_REVISION_EXHAUSTED = "policy_revision_exhausted"
    QUEUE_CAPACITY_UNATTESTED = "queue_capacity_unattested"
    RAY_TARGET_READINESS_UNATTESTED = "ray_target_readiness_unattested"
    REMOTE_WORK_RETIREMENT_UNATTESTED = "remote_work_retirement_unattested"
    UNSUPPORTED_NONTERMINAL_WORK = "unsupported_nonterminal_work"


@dataclass(frozen=True, slots=True)
class ProtocolPolicyStatus:
    """Validated singleton rollout-policy and token state."""

    schema_version: int
    active_write_protocol_version: int
    legacy_worker_admission_enabled: bool
    revision: int
    updated_at: datetime
    legacy_admission_token_present: bool


@dataclass(frozen=True, slots=True)
class ProtocolLeaseCounts:
    """Complete lease totals split by durable activity and heartbeat freshness."""

    total: int
    active: int
    heartbeat_live: int
    stale_active: int
    inactive: int
    active_legacy: int
    heartbeat_live_legacy: int
    stale_active_legacy: int
    active_explicit: int
    heartbeat_live_explicit: int
    stale_active_explicit: int


@dataclass(frozen=True, slots=True)
class ProtocolCapabilityGroup:
    """One aggregated heartbeat-live effective capability range."""

    kind: str
    minimum: int
    maximum: int
    heartbeat_live_leases: int


@dataclass(frozen=True, slots=True)
class ProtocolCapabilitySection:
    """Bounded capability groups with complete omitted accounting."""

    groups: tuple[ProtocolCapabilityGroup, ...]
    total_groups: int
    total_leases: int
    omitted_groups: int
    omitted_leases: int


@dataclass(frozen=True, slots=True)
class ProtocolWorkGroup:
    """One redacted aggregate of nonterminal durable work."""

    queue: str
    state: str
    execution_protocol_version: int
    count: int


@dataclass(frozen=True, slots=True)
class ProtocolWorkSection:
    """Bounded work groups with complete omitted accounting."""

    groups: tuple[ProtocolWorkGroup, ...]
    total_groups: int
    total_tasks: int
    omitted_groups: int
    omitted_tasks: int


@dataclass(frozen=True, slots=True)
class ProtocolStatusBlocker:
    """One fixed blocker code with an optional complete durable count."""

    code: ProtocolStatusBlockerCode
    scope: str
    count: int | None = None


@dataclass(frozen=True, slots=True)
class ProtocolStatusReport:
    """Immutable bounded execution-protocol status report."""

    schema: str
    schema_version: int
    observed_at: datetime
    lease_heartbeat_cutoff: datetime
    policy: ProtocolPolicyStatus
    leases: ProtocolLeaseCounts
    capabilities: ProtocolCapabilitySection
    nonterminal_work: ProtocolWorkSection
    unsupported_work: ProtocolWorkSection
    legacy_metadata_nonterminal_count: int
    non_v1_nonterminal_count: int
    no_upgraded_reader_nonterminal_count: int
    queue_capacity_attested: bool
    blockers: tuple[ProtocolStatusBlocker, ...]


def _safe_int(value: Any) -> int:
    return int(value or 0)


def _utc_datetime(value: datetime) -> datetime:
    if not timezone.is_aware(value):
        raise ProtocolStatusError("protocol status requires an aware observation time")
    return value.astimezone(UTC)


def _iso_datetime(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


def _safe_queue_name(value: object) -> str:
    if not isinstance(value, str):
        return "[INVALID]"
    return normalize_terminal_text(redact_text(value))


def _load_policy(*, using: str) -> ProtocolPolicyStatus:
    policy_rows = list(
        TaskExecutionProtocolPolicy.objects.using(using)
        .order_by("singleton_key")
        .values(
            "singleton_key",
            "schema_version",
            "active_write_protocol_version",
            "legacy_worker_admission_enabled",
            "revision",
            "updated_at",
        )[:2]
    )
    if len(policy_rows) != 1 or int(policy_rows[0]["singleton_key"]) != _POLICY_KEY:
        raise ProtocolStatusError("the execution-protocol policy singleton is unavailable")
    policy = policy_rows[0]
    schema_version = int(policy["schema_version"])
    active_write_protocol_version = int(policy["active_write_protocol_version"])
    revision = int(policy["revision"])
    if schema_version != PROTOCOL_POLICY_SCHEMA_VERSION:
        raise ProtocolStatusError("the execution-protocol policy schema is unsupported")
    if active_write_protocol_version != LEGACY_EXECUTION_PROTOCOL_VERSION:
        raise ProtocolStatusError("the execution-protocol active write version is unsupported")
    if revision < 1 or revision > _MAX_POLICY_REVISION:
        raise ProtocolStatusError("the execution-protocol policy revision is invalid")

    token_keys = tuple(
        LegacyWorkerAdmissionToken.objects.using(using)
        .order_by("singleton_key")
        .values_list("singleton_key", flat=True)[:2]
    )
    if token_keys not in ((), (_LEGACY_TOKEN_KEY,)):
        raise ProtocolStatusError("the legacy-admission token singleton is invalid")
    token_present = bool(token_keys)
    legacy_enabled = bool(policy["legacy_worker_admission_enabled"])
    if token_present != legacy_enabled:
        raise ProtocolStatusError("the execution-protocol policy and token are inconsistent")

    updated_at = policy["updated_at"]
    if not isinstance(updated_at, datetime):
        raise ProtocolStatusError("the execution-protocol policy timestamp is invalid")
    return ProtocolPolicyStatus(
        schema_version=schema_version,
        active_write_protocol_version=active_write_protocol_version,
        legacy_worker_admission_enabled=legacy_enabled,
        revision=revision,
        updated_at=_utc_datetime(updated_at),
        legacy_admission_token_present=token_present,
    )


def _validate_lease_shapes(*, using: str) -> None:
    valid_legacy = Q(
        capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
        django_ray_version__isnull=True,
        min_supported_execution_protocol_version__isnull=True,
        max_supported_execution_protocol_version__isnull=True,
    ) & (Q(is_active=False) | Q(legacy_admission_token_id=_LEGACY_TOKEN_KEY))
    valid_explicit = Q(
        capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
        legacy_admission_token__isnull=True,
        min_supported_execution_protocol_version__isnull=False,
        max_supported_execution_protocol_version__isnull=False,
        min_supported_execution_protocol_version__gte=1,
        max_supported_execution_protocol_version__gte=F("min_supported_execution_protocol_version"),
    )
    if TaskWorkerLease.objects.using(using).exclude(valid_legacy | valid_explicit).exists():
        raise ProtocolStatusError("a worker capability advertisement is invalid")


def _lease_counts(*, using: str, cutoff: datetime) -> ProtocolLeaseCounts:
    counts = TaskWorkerLease.objects.using(using).aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(is_active=True)),
        heartbeat_live=Count(
            "pk",
            filter=Q(is_active=True, last_heartbeat_at__gte=cutoff),
        ),
        stale_active=Count(
            "pk",
            filter=Q(is_active=True, last_heartbeat_at__lt=cutoff),
        ),
        inactive=Count("pk", filter=Q(is_active=False)),
        active_legacy=Count(
            "pk",
            filter=Q(
                is_active=True,
                capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
            ),
        ),
        heartbeat_live_legacy=Count(
            "pk",
            filter=Q(
                is_active=True,
                last_heartbeat_at__gte=cutoff,
                capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
            ),
        ),
        stale_active_legacy=Count(
            "pk",
            filter=Q(
                is_active=True,
                last_heartbeat_at__lt=cutoff,
                capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
            ),
        ),
        active_explicit=Count(
            "pk",
            filter=Q(
                is_active=True,
                capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
            ),
        ),
        heartbeat_live_explicit=Count(
            "pk",
            filter=Q(
                is_active=True,
                last_heartbeat_at__gte=cutoff,
                capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
            ),
        ),
        stale_active_explicit=Count(
            "pk",
            filter=Q(
                is_active=True,
                last_heartbeat_at__lt=cutoff,
                capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
            ),
        ),
    )
    return ProtocolLeaseCounts(**{key: _safe_int(value) for key, value in counts.items()})


def _capability_section(
    *,
    using: str,
    cutoff: datetime,
    policy: ProtocolPolicyStatus,
    leases: ProtocolLeaseCounts,
) -> ProtocolCapabilitySection:
    explicit = TaskWorkerLease.objects.using(using).filter(
        is_active=True,
        last_heartbeat_at__gte=cutoff,
        capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
        legacy_admission_token__isnull=True,
        min_supported_execution_protocol_version__isnull=False,
        max_supported_execution_protocol_version__isnull=False,
    )
    explicit_total_groups = (
        explicit.values(
            "min_supported_execution_protocol_version",
            "max_supported_execution_protocol_version",
        )
        .distinct()
        .count()
    )
    explicit_rows = list(
        explicit.values(
            "min_supported_execution_protocol_version",
            "max_supported_execution_protocol_version",
        )
        .annotate(heartbeat_live_leases=Count("pk"))
        .order_by(
            "min_supported_execution_protocol_version",
            "max_supported_execution_protocol_version",
        )[: PROTOCOL_STATUS_GROUP_LIMIT + 1]
    )
    groups: list[ProtocolCapabilityGroup] = []
    legacy_effective = policy.legacy_worker_admission_enabled and leases.heartbeat_live_legacy > 0
    if legacy_effective:
        groups.append(
            ProtocolCapabilityGroup(
                kind="legacy",
                minimum=LEGACY_EXECUTION_PROTOCOL_VERSION,
                maximum=LEGACY_EXECUTION_PROTOCOL_VERSION,
                heartbeat_live_leases=leases.heartbeat_live_legacy,
            )
        )
    groups.extend(
        ProtocolCapabilityGroup(
            kind="explicit",
            minimum=int(row["min_supported_execution_protocol_version"]),
            maximum=int(row["max_supported_execution_protocol_version"]),
            heartbeat_live_leases=int(row["heartbeat_live_leases"]),
        )
        for row in explicit_rows
    )
    groups.sort(key=lambda group: (group.kind, group.minimum, group.maximum))
    displayed = tuple(groups[:PROTOCOL_STATUS_GROUP_LIMIT])
    total_groups = explicit_total_groups + int(legacy_effective)
    total_leases = leases.heartbeat_live_explicit + (
        leases.heartbeat_live_legacy if legacy_effective else 0
    )
    displayed_leases = sum(group.heartbeat_live_leases for group in displayed)
    return ProtocolCapabilitySection(
        groups=displayed,
        total_groups=total_groups,
        total_leases=total_leases,
        omitted_groups=max(total_groups - len(displayed), 0),
        omitted_leases=max(total_leases - displayed_leases, 0),
    )


def _work_section(queryset: Any) -> ProtocolWorkSection:
    total_tasks = queryset.count()
    # SQLite does not enforce VARCHAR lengths. Bound queue text in SQL before
    # Python materializes it from an untrusted database row.
    grouped = (
        queryset.annotate(_queue_length=Length("queue_name"))
        .annotate(
            _bounded_queue_name=Case(
                When(_queue_length__lte=100, then=F("queue_name")),
                default=Value("[OVERSIZED]"),
                output_field=CharField(max_length=100),
            )
        )
        .values(
            "_bounded_queue_name",
            "state",
            "execution_protocol_version",
        )
    )
    total_groups = grouped.distinct().count()
    rows = list(
        grouped.annotate(count=Count("pk")).order_by(
            "_bounded_queue_name", "state", "execution_protocol_version"
        )[: PROTOCOL_STATUS_GROUP_LIMIT + 1]
    )
    groups = tuple(
        ProtocolWorkGroup(
            queue=_safe_queue_name(row["_bounded_queue_name"]),
            state=str(row["state"]),
            execution_protocol_version=int(row["execution_protocol_version"]),
            count=int(row["count"]),
        )
        for row in rows[:PROTOCOL_STATUS_GROUP_LIMIT]
    )
    displayed_tasks = sum(group.count for group in groups)
    return ProtocolWorkSection(
        groups=groups,
        total_groups=total_groups,
        total_tasks=total_tasks,
        omitted_groups=max(total_groups - len(groups), 0),
        omitted_tasks=max(total_tasks - displayed_tasks, 0),
    )


def _blockers(
    *,
    policy: ProtocolPolicyStatus,
    leases: ProtocolLeaseCounts,
    legacy_metadata_nonterminal_count: int,
    non_v1_nonterminal_count: int,
    no_upgraded_reader_nonterminal_count: int,
    unsupported_nonterminal_count: int,
) -> tuple[ProtocolStatusBlocker, ...]:
    blockers = [
        ProtocolStatusBlocker(
            ProtocolStatusBlockerCode.LEGACY_PRODUCERS_UNATTESTED,
            "legacy_close",
        ),
        ProtocolStatusBlocker(
            ProtocolStatusBlockerCode.LEGACY_READERS_UNATTESTED,
            "code_rollback",
        ),
        ProtocolStatusBlocker(
            ProtocolStatusBlockerCode.QUEUE_CAPACITY_UNATTESTED,
            "capacity",
        ),
        ProtocolStatusBlocker(
            ProtocolStatusBlockerCode.RAY_TARGET_READINESS_UNATTESTED,
            "capacity",
        ),
        ProtocolStatusBlocker(
            ProtocolStatusBlockerCode.REMOTE_WORK_RETIREMENT_UNATTESTED,
            "code_rollback",
        ),
    ]
    if leases.active_legacy:
        blockers.append(
            ProtocolStatusBlocker(
                ProtocolStatusBlockerCode.ACTIVE_LEGACY_LEASES,
                "legacy_close",
                leases.active_legacy,
            )
        )
    if leases.active_explicit:
        blockers.append(
            ProtocolStatusBlocker(
                ProtocolStatusBlockerCode.ACTIVE_UPGRADED_LEASES,
                "code_rollback",
                leases.active_explicit,
            )
        )
    if policy.revision >= _MAX_POLICY_REVISION:
        blockers.append(
            ProtocolStatusBlocker(
                ProtocolStatusBlockerCode.POLICY_REVISION_EXHAUSTED,
                "policy_transition",
                1,
            )
        )
    if legacy_metadata_nonterminal_count:
        blockers.append(
            ProtocolStatusBlocker(
                ProtocolStatusBlockerCode.LEGACY_METADATA_PROVENANCE_UNATTESTED,
                "historical_baseline",
                legacy_metadata_nonterminal_count,
            )
        )
    if non_v1_nonterminal_count:
        blockers.append(
            ProtocolStatusBlocker(
                ProtocolStatusBlockerCode.NON_V1_NONTERMINAL_WORK,
                "code_rollback",
                non_v1_nonterminal_count,
            )
        )
    if no_upgraded_reader_nonterminal_count:
        blockers.append(
            ProtocolStatusBlocker(
                ProtocolStatusBlockerCode.NO_UPGRADED_READER_CAPACITY,
                "reader_retirement",
                no_upgraded_reader_nonterminal_count,
            )
        )
    if unsupported_nonterminal_count:
        blockers.append(
            ProtocolStatusBlocker(
                ProtocolStatusBlockerCode.UNSUPPORTED_NONTERMINAL_WORK,
                "capacity",
                unsupported_nonterminal_count,
            )
        )
    return tuple(sorted(blockers, key=lambda blocker: (blocker.code.value, blocker.scope)))


def _capability_section_to_dict(section: ProtocolCapabilitySection) -> dict[str, Any]:
    return {
        "groups": [
            {
                "heartbeat_live_leases": group.heartbeat_live_leases,
                "kind": group.kind,
                "maximum": group.maximum,
                "minimum": group.minimum,
            }
            for group in section.groups
        ],
        "omitted_groups": section.omitted_groups,
        "omitted_leases": section.omitted_leases,
        "total_groups": section.total_groups,
        "total_leases": section.total_leases,
    }


def _work_section_to_dict(section: ProtocolWorkSection) -> dict[str, Any]:
    return {
        "groups": [
            {
                "count": group.count,
                "execution_protocol_version": group.execution_protocol_version,
                "queue": group.queue,
                "state": group.state,
            }
            for group in section.groups
        ],
        "omitted_groups": section.omitted_groups,
        "omitted_tasks": section.omitted_tasks,
        "total_groups": section.total_groups,
        "total_tasks": section.total_tasks,
    }


def protocol_status_to_dict(report: ProtocolStatusReport) -> dict[str, Any]:
    """Convert one already-bounded report into its stable JSON-ready shape."""
    return {
        "blockers": [
            {
                "code": blocker.code.value,
                "count": blocker.count,
                "scope": blocker.scope,
            }
            for blocker in report.blockers
        ],
        "capabilities": _capability_section_to_dict(report.capabilities),
        "lease_heartbeat_cutoff": _iso_datetime(report.lease_heartbeat_cutoff),
        "leases": {
            "active": report.leases.active,
            "active_explicit": report.leases.active_explicit,
            "active_legacy": report.leases.active_legacy,
            "heartbeat_live": report.leases.heartbeat_live,
            "heartbeat_live_explicit": report.leases.heartbeat_live_explicit,
            "heartbeat_live_legacy": report.leases.heartbeat_live_legacy,
            "inactive": report.leases.inactive,
            "stale_active": report.leases.stale_active,
            "stale_active_explicit": report.leases.stale_active_explicit,
            "stale_active_legacy": report.leases.stale_active_legacy,
            "total": report.leases.total,
        },
        "legacy_metadata_nonterminal_count": report.legacy_metadata_nonterminal_count,
        "non_v1_nonterminal_count": report.non_v1_nonterminal_count,
        "no_upgraded_reader_nonterminal_count": (report.no_upgraded_reader_nonterminal_count),
        "nonterminal_work": _work_section_to_dict(report.nonterminal_work),
        "observed_at": _iso_datetime(report.observed_at),
        "policy": {
            "active_write_protocol_version": report.policy.active_write_protocol_version,
            "legacy_admission_token_present": report.policy.legacy_admission_token_present,
            "legacy_worker_admission_enabled": report.policy.legacy_worker_admission_enabled,
            "revision": report.policy.revision,
            "schema_version": report.policy.schema_version,
            "updated_at": _iso_datetime(report.policy.updated_at),
        },
        "queue_capacity_attested": report.queue_capacity_attested,
        "schema": report.schema,
        "schema_version": report.schema_version,
        "unsupported_work": _work_section_to_dict(report.unsupported_work),
    }


def render_protocol_status_json(report: ProtocolStatusReport) -> str:
    """Render canonical compact JSON for one bounded report."""
    return json.dumps(
        protocol_status_to_dict(report),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _render_protocol_status_text_unchecked(report: ProtocolStatusReport) -> str:
    lines = [
        f"schema={report.schema} schema_version={report.schema_version}",
        f"observed_at={_iso_datetime(report.observed_at)}",
        f"lease_heartbeat_cutoff={_iso_datetime(report.lease_heartbeat_cutoff)}",
        (
            "policy "
            f"schema={report.policy.schema_version} "
            f"active_write_protocol={report.policy.active_write_protocol_version} "
            f"legacy_admission={str(report.policy.legacy_worker_admission_enabled).lower()} "
            f"token_present={str(report.policy.legacy_admission_token_present).lower()} "
            f"revision={report.policy.revision} "
            f"updated_at={_iso_datetime(report.policy.updated_at)}"
        ),
        (
            "leases "
            f"total={report.leases.total} active={report.leases.active} "
            f"heartbeat_live={report.leases.heartbeat_live} "
            f"stale_active={report.leases.stale_active} inactive={report.leases.inactive} "
            f"active_legacy={report.leases.active_legacy} "
            f"heartbeat_live_legacy={report.leases.heartbeat_live_legacy} "
            f"stale_active_legacy={report.leases.stale_active_legacy} "
            f"active_explicit={report.leases.active_explicit} "
            f"heartbeat_live_explicit={report.leases.heartbeat_live_explicit} "
            f"stale_active_explicit={report.leases.stale_active_explicit}"
        ),
        f"queue_capacity_attested={str(report.queue_capacity_attested).lower()}",
    ]
    for group in report.capabilities.groups:
        lines.append(
            "capability "
            f"kind={group.kind} minimum={group.minimum} maximum={group.maximum} "
            f"heartbeat_live_leases={group.heartbeat_live_leases}"
        )
    lines.append(
        "capabilities "
        f"total_groups={report.capabilities.total_groups} "
        f"total_leases={report.capabilities.total_leases} "
        f"omitted_groups={report.capabilities.omitted_groups} "
        f"omitted_leases={report.capabilities.omitted_leases}"
    )
    for section_name, section in (
        ("nonterminal", report.nonterminal_work),
        ("unsupported", report.unsupported_work),
    ):
        for group in section.groups:
            queue = json.dumps(group.queue, ensure_ascii=False)
            lines.append(
                f"{section_name}_work queue={queue} state={group.state} "
                f"protocol={group.execution_protocol_version} count={group.count}"
            )
        lines.append(
            f"{section_name}_work_totals total_groups={section.total_groups} "
            f"total_tasks={section.total_tasks} omitted_groups={section.omitted_groups} "
            f"omitted_tasks={section.omitted_tasks}"
        )
    lines.extend(
        (
            f"legacy_metadata_nonterminal_count={report.legacy_metadata_nonterminal_count}",
            f"non_v1_nonterminal_count={report.non_v1_nonterminal_count}",
            (f"no_upgraded_reader_nonterminal_count={report.no_upgraded_reader_nonterminal_count}"),
        )
    )
    for blocker in report.blockers:
        count = "unknown" if blocker.count is None else str(blocker.count)
        lines.append(f"blocker code={blocker.code.value} scope={blocker.scope} count={count}")
    return "\n".join(lines)


def render_protocol_status_text(report: ProtocolStatusReport) -> str:
    """Render the bounded inert human-readable view of one report."""
    rendered = _render_protocol_status_text_unchecked(report)
    if len(rendered.encode("utf-8")) > _OUTPUT_CONTENT_MAX_BYTES:
        raise ProtocolStatusError("the bounded protocol status text exceeded its output budget")
    return rendered


def _remove_last_capability(report: ProtocolStatusReport) -> ProtocolStatusReport:
    group = report.capabilities.groups[-1]
    section = replace(
        report.capabilities,
        groups=report.capabilities.groups[:-1],
        omitted_groups=report.capabilities.omitted_groups + 1,
        omitted_leases=report.capabilities.omitted_leases + group.heartbeat_live_leases,
    )
    return replace(report, capabilities=section)


def _remove_last_work_group(
    report: ProtocolStatusReport,
    *,
    section_name: str,
) -> ProtocolStatusReport:
    section = getattr(report, section_name)
    group = section.groups[-1]
    bounded = replace(
        section,
        groups=section.groups[:-1],
        omitted_groups=section.omitted_groups + 1,
        omitted_tasks=section.omitted_tasks + group.count,
    )
    return replace(report, **{section_name: bounded})


def _fit_output_budget(report: ProtocolStatusReport) -> ProtocolStatusReport:
    current = report
    while True:
        json_size = len(render_protocol_status_json(current).encode("utf-8"))
        text_size = len(_render_protocol_status_text_unchecked(current).encode("utf-8"))
        if max(json_size, text_size) <= _OUTPUT_CONTENT_MAX_BYTES:
            return current
        candidates: list[tuple[int, str]] = []
        if current.capabilities.groups:
            candidates.append(
                (
                    len(
                        json.dumps(
                            _capability_section_to_dict(current.capabilities)["groups"][-1],
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ),
                    "capabilities",
                )
            )
        for section_name in ("nonterminal_work", "unsupported_work"):
            section = getattr(current, section_name)
            if section.groups:
                candidates.append(
                    (
                        len(
                            json.dumps(
                                _work_section_to_dict(section)["groups"][-1],
                                ensure_ascii=False,
                            ).encode("utf-8")
                        ),
                        section_name,
                    )
                )
        if not candidates:
            raise ProtocolStatusError("the protocol status header exceeded its output budget")
        _size, selected = max(candidates, key=lambda item: (item[0], item[1]))
        if selected == "capabilities":
            current = _remove_last_capability(current)
        else:
            current = _remove_last_work_group(current, section_name=selected)


def _build_protocol_status_observation(
    *,
    using: str,
    observed: datetime,
    cutoff: datetime,
) -> ProtocolStatusReport:
    policy = _load_policy(using=using)
    _validate_lease_shapes(using=using)
    leases = _lease_counts(using=using, cutoff=cutoff)
    capabilities = _capability_section(
        using=using,
        cutoff=cutoff,
        policy=policy,
        leases=leases,
    )

    nonterminal = RayTaskExecution.objects.using(using).filter(state__in=_NONTERMINAL_STATES)
    nonterminal_counts = nonterminal.aggregate(
        legacy_metadata=Count(
            "pk",
            filter=Q(metadata_schema_version=LEGACY_EXECUTION_METADATA_SCHEMA_VERSION),
        ),
        non_v1=Count(
            "pk",
            filter=~Q(execution_protocol_version=LEGACY_EXECUTION_PROTOCOL_VERSION),
        ),
    )
    legacy_metadata_count = _safe_int(nonterminal_counts["legacy_metadata"])
    non_v1_count = _safe_int(nonterminal_counts["non_v1"])

    explicit_capacity = TaskWorkerLease.objects.using(using).filter(
        is_active=True,
        last_heartbeat_at__gte=cutoff,
        capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
        legacy_admission_token__isnull=True,
        min_supported_execution_protocol_version__lte=OuterRef("execution_protocol_version"),
        max_supported_execution_protocol_version__gte=OuterRef("execution_protocol_version"),
    )
    no_explicit_capacity = nonterminal.annotate(
        _has_explicit_protocol_capacity=Exists(explicit_capacity)
    ).filter(_has_explicit_protocol_capacity=False)
    no_upgraded_reader_count = no_explicit_capacity.count()
    unsupported = no_explicit_capacity
    if policy.legacy_worker_admission_enabled and leases.heartbeat_live_legacy > 0:
        unsupported = unsupported.exclude(
            execution_protocol_version=LEGACY_EXECUTION_PROTOCOL_VERSION
        )

    nonterminal_section = _work_section(nonterminal)
    unsupported_section = _work_section(unsupported)
    report = ProtocolStatusReport(
        schema=PROTOCOL_STATUS_SCHEMA,
        schema_version=PROTOCOL_STATUS_SCHEMA_VERSION,
        observed_at=observed,
        lease_heartbeat_cutoff=cutoff,
        policy=policy,
        leases=leases,
        capabilities=capabilities,
        nonterminal_work=nonterminal_section,
        unsupported_work=unsupported_section,
        legacy_metadata_nonterminal_count=legacy_metadata_count,
        non_v1_nonterminal_count=non_v1_count,
        no_upgraded_reader_nonterminal_count=no_upgraded_reader_count,
        queue_capacity_attested=False,
        blockers=_blockers(
            policy=policy,
            leases=leases,
            legacy_metadata_nonterminal_count=legacy_metadata_count,
            non_v1_nonterminal_count=non_v1_count,
            no_upgraded_reader_nonterminal_count=no_upgraded_reader_count,
            unsupported_nonterminal_count=unsupported_section.total_tasks,
        ),
    )
    return _fit_output_budget(report)


def build_protocol_status(
    *,
    using: str = DEFAULT_DB_ALIAS,
    observed_at: datetime | None = None,
) -> ProtocolStatusReport:
    """Build one consistent bounded read-only protocol report."""
    try:
        connection = connections[using]
        if connection.vendor not in _SUPPORTED_DATABASE_VENDORS:
            raise ProtocolStatusError(
                "execution-protocol status supports only SQLite and PostgreSQL"
            )
        if connection.in_atomic_block or not connection.get_autocommit():
            raise ProtocolStatusError(
                "protocol status must own its outermost read-only database transaction"
            )
        observed = _utc_datetime(observed_at or datetime.now(UTC))
        cutoff = observed - get_lease_duration()
        with transaction.atomic(using=using, durable=True):
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return _build_protocol_status_observation(
                using=using,
                observed=observed,
                cutoff=cutoff,
            )
    except ProtocolStatusError:
        raise
    except ConnectionDoesNotExist:
        raise ProtocolStatusError("the selected protocol status database is unavailable") from None
    except (DatabaseError, ImproperlyConfigured, KeyError, TypeError, ValueError):
        raise ProtocolStatusError("execution-protocol status is unavailable") from None
