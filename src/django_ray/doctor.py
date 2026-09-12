"""Bounded database diagnostics, never admission, remote health or drain proof.

The doctor owns one read-only observation transaction. It reads scalar metadata
and aggregates only: no task imports, payload decoding, Ray calls or storage
checks. Recorded proof windows are not independent proof validation.
"""

from __future__ import annotations

import json
import pkgutil
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Case, CharField, Count, Exists, F, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Length
from django.utils.connection import ConnectionDoesNotExist

from django_ray import migrations, protocol_status
from django_ray.models import (
    RayCohortJobCleanup,
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayTargetProbeJobReceipt,
    RayTaskCohortClaim,
    RayTaskExecution,
    RayTaskQuarantine,
    RayWorkerRetirement,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)
from django_ray.redaction import normalize_terminal_text, redact_text
from django_ray.runner.leasing import get_lease_duration

DOCTOR_SCHEMA = "django-ray.doctor"
DOCTOR_SCHEMA_VERSION = 1
DOCTOR_GROUP_LIMIT = 64
DOCTOR_OUTPUT_MAX_BYTES = 65_536
_NONTERMINAL = ("QUEUED", "RUNNING", "CANCELLING")
_UNVERIFIED = (
    "runtime_qualified_queue_serviceability",
    "live_ray_connectivity_and_cluster_membership",
    "canonical_proof_and_endpoint_qualification",
    "remote_work_quiescence_and_cleanup",
    "runtime_env_artifact_and_encryption_key_readiness",
    "result_progress_storage_pressure_and_cleanup",
    "producer_reader_and_purger_retirement",
    "drain_completion_and_remote_retirement",
    "backup_restore_and_rollback_rehearsal",
)


class DoctorError(RuntimeError):
    """An observation cannot safely own its transaction or output budget."""


@dataclass(frozen=True, slots=True)
class DoctorReport:
    observed_at: datetime
    database: dict[str, Any]
    protocol: protocol_status.ProtocolStatusReport | None
    cohort: dict[str, Any] | None
    blockers: tuple[dict[str, Any], ...]


def _blocker(code: str, count: int | None = None) -> dict[str, Any]:
    return {"code": code, "count": count}


def _known_migrations() -> tuple[str, ...]:
    # Inventory this package's filenames without importing migration modules or
    # another installed application's code. Never echo unknown database names.
    return tuple(
        sorted(
            item.name
            for item in pkgutil.iter_modules(migrations.__path__)
            if re.fullmatch(r"[0-9]{4}_[a-z0-9_]+", item.name)
        )
    )


def _migration_status(connection) -> dict[str, Any]:
    known = _known_migrations()
    recorder = MigrationRecorder(connection)
    applied: set[str] = set()
    unknown = 0
    present = recorder.has_table()
    if present:
        rows = recorder.migration_qs.filter(app="django_ray")
        applied = set(rows.filter(name__in=known).values_list("name", flat=True).distinct())
        unknown = rows.exclude(name__in=known).count()
    missing = sorted(set(known) - applied)
    names = sorted(applied)
    return {
        "status": "unknown_applied"
        if unknown
        else "pending"
        if missing or not known
        else "current",
        "recorder_present": present,
        "known_count": len(known),
        "applied_count": len(applied),
        "unknown_applied_records": unknown,
        "missing_count": len(missing),
        "applied": names[:DOCTOR_GROUP_LIMIT],
        "omitted_applied": max(len(names) - DOCTOR_GROUP_LIMIT, 0),
        "missing": missing[:DOCTOR_GROUP_LIMIT],
        "omitted_missing": max(len(missing) - DOCTOR_GROUP_LIMIT, 0),
    }


def _counts(query, **filters: Q) -> dict[str, int]:
    return query.aggregate(
        total=Count("pk"), **{key: Count("pk", filter=value) for key, value in filters.items()}
    )


def _package_groups(*, using: str, cutoff: datetime, observed: datetime) -> dict[str, Any]:
    leases = TaskWorkerLease.objects.using(using).filter(
        is_active=True,
        started_at__lte=observed,
        last_heartbeat_at__gte=cutoff,
        last_heartbeat_at__lte=observed,
    )
    # SQLite VARCHAR does not impose a bound. Do not materialize oversized text.
    groups = (
        leases.annotate(_length=Length("django_ray_version"))
        .annotate(
            package=Case(
                When(_length__lte=128, then=F("django_ray_version")),
                When(django_ray_version__isnull=True, then=Value("[UNRECORDED]")),
                default=Value("[OVERSIZED]"),
                output_field=CharField(max_length=128),
            )
        )
        .values("package")
    )
    total_groups = groups.distinct().count()
    rows = list(groups.annotate(count=Count("pk")).order_by("package")[:DOCTOR_GROUP_LIMIT])
    for row in rows:
        row["package"] = normalize_terminal_text(redact_text(row["package"]))
    total = leases.count()
    return {
        "groups": rows,
        "total_groups": total_groups,
        "total_leases": total,
        "omitted_groups": total_groups - len(rows),
        "omitted_leases": total - sum(row["count"] for row in rows),
    }


def _cohort_observation(*, using: str, observed: datetime, cutoff: datetime) -> dict[str, Any]:
    policies = RayTargetPolicyRevision.objects.using(using)
    latest_policy = policies.filter(target_id=OuterRef("target_id")).order_by("-revision")
    current = policies.filter(pk=Subquery(latest_policy.values("pk")[:1]))
    attestations = (
        RayTargetAttestationRevision.objects.using(using)
        .filter(policy_id=OuterRef("pk"))
        .order_by("-revision")
    )
    current = current.annotate(
        proof_id=Subquery(attestations.values("pk")[:1]),
        proof_observed=Subquery(attestations.values("observed_at")[:1]),
        proof_expires=Subquery(attestations.values("expires_at")[:1]),
        proof_expectation=Subquery(attestations.values("expectation_digest")[:1]),
    )
    policy_counts = _counts(
        current,
        active=Q(desired_state="active"),
        draining=Q(desired_state="draining"),
        retired_records=Q(desired_state="retired"),
        proof_missing=Q(proof_id__isnull=True),
        proof_expired=Q(proof_expires__lte=observed),
        proof_future=Q(proof_observed__gt=observed),
        proof_expectation_mismatch=Q(proof_id__isnull=False)
        & ~Q(proof_expectation=F("expectation_digest")),
        proof_within_recorded_window=Q(
            proof_observed__lte=observed,
            proof_expires__gt=observed,
            proof_expectation=F("expectation_digest"),
        ),
    )
    policy_counts["targets_without_policy"] = (
        RayTarget.objects.using(using).filter(policy_revisions__isnull=True).count()
    )
    capabilities = RayWorkerTargetCapability.objects.using(using)
    exact_live = Q(
        lease__is_active=True,
        lease__started_at__lte=observed,
        lease__last_heartbeat_at__gte=cutoff,
        lease__last_heartbeat_at__lte=observed,
        lease_hostname=F("lease__hostname"),
        lease_pid=F("lease__pid"),
        lease_started_at=F("lease__started_at"),
    )
    capability_counts = _counts(
        capabilities,
        exact_heartbeat_live_lease=exact_live,
        stale_or_crossed_lease=~exact_live,
        proof_expired=Q(attestation__expires_at__lte=observed),
    )
    claims = RayTaskCohortClaim.objects.using(using)
    current_claim = Q(
        attempt_number=F("binding__execution__attempt_number"),
        execution_generation=F("binding__execution__execution_generation"),
        binding__execution__state__in=_NONTERMINAL,
    )
    exact_owner = TaskWorkerLease.objects.using(using).filter(
        worker_id=OuterRef("owner_lease_id"),
        hostname=OuterRef("owner_lease_hostname"),
        pid=OuterRef("owner_lease_pid"),
        started_at=OuterRef("owner_lease_started_at"),
        is_active=True,
        started_at__lte=observed,
        last_heartbeat_at__gte=cutoff,
        last_heartbeat_at__lte=observed,
    )
    claims = claims.annotate(owner_live=Exists(exact_owner))
    claim_counts = _counts(
        claims,
        open=Q(disposition="OPEN"),
        held=Q(disposition="HELD"),
        resolved=Q(disposition="RESOLVED"),
        unresolved=~Q(disposition="RESOLVED"),
        unresolved_outside_current_nonterminal=~current_claim & ~Q(disposition="RESOLVED"),
        current_unresolved=current_claim & ~Q(disposition="RESOLVED"),
        current_held=current_claim & Q(disposition="HELD"),
        current_unresolved_without_exact_live_owner=current_claim
        & ~Q(disposition="RESOLVED")
        & Q(owner_live=False),
    )
    probes = RayTargetProbeChallenge.objects.using(using)
    probe_counts = _counts(
        probes,
        pending=Q(consumed_at__isnull=True),
        consumed=Q(consumed_at__isnull=False),
        pending_expired=Q(consumed_at__isnull=True, expires_at__lte=observed),
        pending_without_exact_live_owner=Q(consumed_at__isnull=True) & ~exact_live,
    )
    receipts = _counts(
        RayTargetProbeJobReceipt.objects.using(using),
        pending_receipt=Q(received_at__isnull=True),
        received=Q(received_at__isnull=False),
    )
    work = RayTaskExecution.objects.using(using).filter(state__in=_NONTERMINAL)
    work_counts = _counts(
        work,
        queued=Q(state="QUEUED"),
        running=Q(state="RUNNING"),
        cancelling=Q(state="CANCELLING"),
        queued_deadline_elapsed=Q(state="QUEUED", queue_deadline_at__lte=observed),
        running_heartbeat_stale=Q(state="RUNNING")
        & (Q(last_heartbeat_at__lt=cutoff) | Q(last_heartbeat_at__isnull=True)),
    )
    return {
        "policies": policy_counts,
        "capabilities": capability_counts,
        "claims": claim_counts,
        "probes": probe_counts,
        "job_receipts": receipts,
        "work": work_counts,
        "manager_packages": _package_groups(using=using, cutoff=cutoff, observed=observed),
        "quarantine": _quarantine_observation(using=using, observed=observed),
        "worker_retirement": _retirement_observation(using=using, observed=observed, cutoff=cutoff),
        "job_cleanup": _cleanup_observation(using=using, observed=observed, cutoff=cutoff),
    }


def _quarantine_observation(*, using: str, observed: datetime) -> dict[str, int]:
    decisions = RayTaskQuarantine.objects.using(using)
    latest = decisions.filter(task_execution_pk=OuterRef("task_execution_pk")).order_by("-revision")
    current_task = RayTaskExecution.objects.using(using).filter(
        pk=OuterRef("task_execution_pk"),
        task_id=OuterRef("task_id"),
        attempt_number=OuterRef("attempt_number"),
        execution_generation=OuterRef("execution_generation"),
    )
    current = decisions.filter(pk=Subquery(latest.values("pk")[:1])).annotate(
        exact_task=Exists(current_task),
        nonterminal_task=Exists(current_task.filter(state__in=_NONTERMINAL)),
    )
    known_current = Q(exact_task=True, created_at__lte=observed)
    return _counts(
        current,
        current_quarantined=known_current & Q(state="QUARANTINED"),
        current_nonterminal_quarantined=known_current
        & Q(state="QUARANTINED", nonterminal_task=True),
        current_released=known_current & Q(state="RELEASED"),
        retained_without_current_identity=Q(exact_task=False),
        future_decisions=Q(created_at__gt=observed),
    )


def _retirement_observation(*, using: str, observed: datetime, cutoff: datetime) -> dict[str, int]:
    decisions = RayWorkerRetirement.objects.using(using)
    incarnation = {
        "worker_id": OuterRef("worker_id"),
        "hostname": OuterRef("hostname"),
        "pid": OuterRef("pid"),
        "started_at": OuterRef("started_at"),
    }
    latest = decisions.filter(**incarnation).order_by("-revision")
    exact_lease = TaskWorkerLease.objects.using(using).filter(**incarnation)
    current = decisions.filter(pk=Subquery(latest.values("pk")[:1])).annotate(
        exact_lease=Exists(exact_lease),
        live_lease=Exists(
            exact_lease.filter(
                is_active=True,
                started_at__lte=observed,
                last_heartbeat_at__gte=cutoff,
                last_heartbeat_at__lte=observed,
            )
        ),
        inactive_lease=Exists(
            exact_lease.filter(
                is_active=False, started_at__lte=observed, last_heartbeat_at__lte=observed
            )
        ),
    )
    requested = Q(state="REQUESTED", created_at__lte=observed)
    retired = Q(state="RETIRED", created_at__lte=observed, cleanup_confirmed_at__lte=observed)
    return _counts(
        current,
        requested=requested,
        requested_with_exact_live_lease=requested & Q(live_lease=True),
        requested_without_exact_live_lease=requested & Q(live_lease=False),
        retired_records=retired,
        retired_with_exact_inactive_lease=retired & Q(inactive_lease=True),
        retired_without_exact_inactive_lease=retired & Q(inactive_lease=False),
        retained_without_exact_lease=Q(exact_lease=False),
        future_decisions=Q(created_at__gt=observed) | Q(cleanup_confirmed_at__gt=observed),
    )


def _cleanup_observation(*, using: str, observed: datetime, cutoff: datetime) -> dict[str, int]:
    exact_owner = TaskWorkerLease.objects.using(using).filter(
        worker_id=OuterRef("owner_lease_id"),
        hostname=OuterRef("owner_lease_hostname"),
        pid=OuterRef("owner_lease_pid"),
        started_at=OuterRef("owner_lease_started_at"),
        is_active=True,
        started_at__lte=observed,
        last_heartbeat_at__gte=cutoff,
        last_heartbeat_at__lte=observed,
    )
    rows = RayCohortJobCleanup.objects.using(using).annotate(owner_live=Exists(exact_owner))
    pending = Q(state="OPEN")
    return _counts(
        rows,
        open=pending,
        closed_records=Q(state="CLOSED"),
        open_uninspectable=pending
        & (Q(expectation_digest__isnull=True) | Q(missing_expectation_reason__isnull=False)),
        open_without_exact_live_owner=pending & Q(owner_live=False),
        open_for_terminal_task=pending & ~Q(execution__state__in=_NONTERMINAL),
        open_for_prior_attempt_or_generation=pending
        & ~Q(
            claim__attempt_number=F("execution__attempt_number"),
            claim__execution_generation=F("execution__execution_generation"),
        ),
        future_records=Q(updated_at__gt=observed),
    )


def _observation_blockers(cohort: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    selected = (
        ("nonterminal_work", "work", "total"),
        ("current_unresolved_claims", "claims", "current_unresolved"),
        ("current_held_claims", "claims", "current_held"),
        (
            "unresolved_claims_outside_current_nonterminal",
            "claims",
            "unresolved_outside_current_nonterminal",
        ),
        ("current_quarantined_work", "quarantine", "current_quarantined"),
        ("quarantine_decision_future", "quarantine", "future_decisions"),
        ("worker_retirement_requested", "worker_retirement", "requested"),
        (
            "retirement_record_without_exact_inactive_lease",
            "worker_retirement",
            "retired_without_exact_inactive_lease",
        ),
        ("retirement_decision_future", "worker_retirement", "future_decisions"),
        ("open_jobs_cleanup", "job_cleanup", "open"),
        ("uninspectable_jobs_cleanup", "job_cleanup", "open_uninspectable"),
        ("jobs_cleanup_owner_unavailable", "job_cleanup", "open_without_exact_live_owner"),
        ("jobs_cleanup_record_future", "job_cleanup", "future_records"),
        (
            "current_claim_owner_unavailable",
            "claims",
            "current_unresolved_without_exact_live_owner",
        ),
        ("pending_target_probes", "probes", "pending"),
        ("target_proof_missing", "policies", "proof_missing"),
        ("target_proof_expired", "policies", "proof_expired"),
        ("target_proof_future", "policies", "proof_future"),
        ("target_proof_expectation_mismatch", "policies", "proof_expectation_mismatch"),
    )
    return tuple(
        _blocker(code, cohort[section][key])
        for code, section, key in selected
        if cohort[section][key]
    )


def doctor_to_dict(report: DoctorReport) -> dict[str, Any]:
    return {
        "schema": DOCTOR_SCHEMA,
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "observed_at": protocol_status._iso_datetime(report.observed_at),
        "scope": "selected_database",
        "database": report.database,
        "protocol": protocol_status.protocol_status_to_dict(report.protocol)
        if report.protocol
        else None,
        "cohort": report.cohort,
        "blockers": list(report.blockers),
        "unverified": list(_UNVERIFIED),
        "drain_status": "unverified",
        "upgrade_status": "unverified",
        "rollback_status": "unverified",
    }


def render_doctor_json(report: DoctorReport) -> str:
    rendered = json.dumps(
        doctor_to_dict(report), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if len(rendered.encode("utf-8")) >= DOCTOR_OUTPUT_MAX_BYTES:
        raise DoctorError("doctor report exceeded its output budget")
    return rendered


def _text(report: DoctorReport) -> str:
    encoded = doctor_to_dict(report)
    lines = [
        f"django-ray doctor schema_version={DOCTOR_SCHEMA_VERSION}",
        f"observed_at={encoded['observed_at']}",
        "Database: " + json.dumps(report.database, sort_keys=True, ensure_ascii=False),
        "Drain, upgrade and rollback: unverified",
    ]
    if report.protocol is not None:
        lines.extend(
            (
                "Protocol compatibility (not runtime-qualified serviceability):",
                protocol_status._render_protocol_status_text_unchecked(report.protocol),
            )
        )
    if report.cohort is not None:
        for name, values in report.cohort.items():
            lines.append(
                f"Recorded {name}: " + json.dumps(values, sort_keys=True, ensure_ascii=False)
            )
    lines.append("Database blockers: " + json.dumps(list(report.blockers), sort_keys=True))
    lines.extend("Unverified: " + name for name in _UNVERIFIED)
    return "\n".join(lines)


def render_doctor_text(report: DoctorReport) -> str:
    rendered = _text(report)
    if len(rendered.encode("utf-8")) >= DOCTOR_OUTPUT_MAX_BYTES:
        raise DoctorError("doctor report exceeded its output budget")
    return rendered


def _fit(report: DoctorReport) -> DoctorReport:
    # Preserve aggregate totals, fixed blockers and unverified checks when the
    # combined report needs more room than the standalone protocol report.
    while True:
        encoded = json.dumps(
            doctor_to_dict(report), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        if (
            max(len(encoded.encode("utf-8")), len(_text(report).encode("utf-8")))
            < DOCTOR_OUTPUT_MAX_BYTES
        ):
            return report
        protocol = report.protocol
        if protocol is not None and protocol.unsupported_work.groups:
            protocol = protocol_status._remove_last_work_group(
                protocol, section_name="unsupported_work"
            )
        elif protocol is not None and protocol.nonterminal_work.groups:
            protocol = protocol_status._remove_last_work_group(
                protocol, section_name="nonterminal_work"
            )
        elif protocol is not None and protocol.capabilities.groups:
            protocol = protocol_status._remove_last_capability(protocol)
        else:
            raise DoctorError("doctor report header exceeded its output budget")
        report = replace(report, protocol=protocol)


def build_doctor(
    *, using: str = DEFAULT_DB_ALIAS, observed_at: datetime | None = None
) -> DoctorReport:
    """Inspect durable state; connection/schema failures produce fixed reports.

    Refuse caller transactions. PostgreSQL uses a repeatable read-only snapshot;
    SQLite uses one outer read transaction. No observation grants serviceability
    or permission to upgrade, retire workers, replay work or discard artifacts.
    """
    try:
        observed = protocol_status._utc_datetime(
            datetime.now(UTC) if observed_at is None else observed_at
        )
    except (
        protocol_status.ProtocolStatusError,
        TypeError,
        AttributeError,
        ValueError,
        OverflowError,
    ):
        raise DoctorError("doctor requires an aware observation time") from None
    database: dict[str, Any] = {"connectivity": "unavailable", "migrations": None}
    failure = "database_unavailable"
    try:
        connection = connections[using]
        if connection.vendor not in {"sqlite", "postgresql"}:
            return DoctorReport(
                observed, database, None, None, (_blocker("database_vendor_unsupported"),)
            )
        if connection.in_atomic_block or not connection.get_autocommit():
            raise DoctorError("doctor must own its outermost read-only database transaction")
        with transaction.atomic(using=using, durable=True):
            with connection.cursor() as cursor:
                if connection.vendor == "postgresql":
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("SELECT 1")
                cursor.fetchone()
            database["connectivity"] = "reachable"
            failure = "migration_observation_unavailable"
            database["migrations"] = _migration_status(connection)
            if database["migrations"]["status"] != "current":
                return _fit(
                    DoctorReport(
                        observed, database, None, None, (_blocker("migrations_not_current"),)
                    )
                )
            failure = "database_observation_unavailable"
            cutoff = observed - get_lease_duration()
            protocol = protocol_status._build_protocol_status_observation(
                using=using, observed=observed, cutoff=cutoff
            )
            cohort = _cohort_observation(using=using, observed=observed, cutoff=cutoff)
            return _fit(
                DoctorReport(observed, database, protocol, cohort, _observation_blockers(cohort))
            )
    except (
        ConnectionDoesNotExist,
        DatabaseError,
        ImproperlyConfigured,
        protocol_status.ProtocolStatusError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        # Never serialize driver messages, connection strings or row contents.
        return DoctorReport(observed, database, None, None, (_blocker(failure),))
