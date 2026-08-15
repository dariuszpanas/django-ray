"""Migration and persistence fences for protocol-2 target execution evidence."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import cast

import pytest
from django.db import (
    DatabaseError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetPolicyRevision,
    RayTargetRoute,
    RayTargetRouteRevision,
    RayTaskExecution,
    RayTaskTargetBinding,
    RayTaskTargetExecutionEvidence,
    RayTaskTargetExecutionOutcome,
    RayTaskTargetRouteSelection,
    RayWorkerTargetCapability,
    TaskExecutionProtocolPolicy,
    TaskWorkerLease,
)
from django_ray.protocol_coordination import close_legacy_worker_admission
from django_ray.target_execution_evidence import (
    RayTaskTargetExecutionEvidenceClaim,
    ray_task_target_execution_evidence_digest,
)

MIGRATE_FROM = [("django_ray", "0025_ray_worker_target_capabilities")]
MIGRATE_TO = [("django_ray", "0026_ray_task_target_execution_evidence")]
LATEST = MIGRATE_TO

_DIGEST_A = f"sha256:{'a' * 64}"
_DIGEST_B = f"sha256:{'b' * 64}"
_DIGEST_C = f"sha256:{'c' * 64}"
_DIGEST_D = f"sha256:{'d' * 64}"
_NODE_ID = "1" * 56


@dataclass(frozen=True)
class _Lineage:
    execution: RayTaskExecution
    target: RayTarget
    policy: RayTargetPolicyRevision
    attestation: RayTargetAttestationRevision
    route: RayTargetRoute
    route_revision: RayTargetRouteRevision
    binding: RayTaskTargetBinding
    selection: RayTaskTargetRouteSelection
    lease: TaskWorkerLease
    capability: RayWorkerTargetCapability
    claimed_at: datetime


def _create_lineage(
    *,
    suffix: str = "base",
    execution_protocol_version: int = 2,
    capability_precedes_lease: bool = False,
    identity_text: str | None = None,
) -> _Lineage:
    now = timezone.now().astimezone(UTC).replace(microsecond=123456)
    observed_at = now - timedelta(seconds=20)
    recorded_at = now - timedelta(seconds=19)
    lease_started_at = now - timedelta(seconds=10)
    advertised_at = now - timedelta(seconds=2)
    claimed_at = now - timedelta(seconds=1)
    if capability_precedes_lease:
        lease_started_at = now - timedelta(seconds=1, microseconds=500_000)
    protocol_policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
    if protocol_policy.legacy_worker_admission_enabled:
        close_legacy_worker_admission(
            expected_revision=protocol_policy.revision,
            legacy_producers_retired=True,
        )
    lease = TaskWorkerLease.objects.create(
        worker_id=identity_text or f"evidence-worker-{suffix}",
        hostname=identity_text or "worker.example",
        pid=1234,
        queue_name="default",
        capability_schema_version=1,
        django_ray_version="0.5.dev0",
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=2,
        legacy_admission_token=None,
        started_at=lease_started_at,
        last_heartbeat_at=claimed_at,
        is_active=True,
    )
    execution = RayTaskExecution.objects.create(
        task_id=identity_text or f"evidence-task-{suffix}",
        callable_path="tests.tasks.evidence",
        execution_protocol_version=execution_protocol_version,
        state="RUNNING",
        attempt_number=1,
        execution_generation=1,
        claimed_by_worker=lease.pk,
        started_at=claimed_at,
        finished_at=None,
    )
    target = RayTarget.objects.create(
        target_key=f"evidence-{suffix}",
        runner_family="ray_core",
        cluster_session=f"session_evidence_{suffix}",
        ray_major=2,
        ray_minor=56,
        ray_patch=0,
        python_implementation="cpython",
        python_major=3,
        python_minor=12,
        python_patch=12,
    )
    policy = RayTargetPolicyRevision.objects.create(
        target=target,
        revision=1,
        desired_state="active",
        expectation_schema_version=1,
        expectation_json='{"schema":"expectation"}',
        expectation_digest=_DIGEST_A,
        created_at=observed_at,
    )
    attestation = RayTargetAttestationRevision.objects.create(
        policy=policy,
        revision=1,
        attestation_schema_version=1,
        attestation_json='{"schema":"attestation"}',
        expectation_digest=_DIGEST_A,
        membership_digest=_DIGEST_B,
        attestation_digest=_DIGEST_C,
        observed_at=observed_at,
        expires_at=now + timedelta(seconds=60),
        recorded_at=recorded_at,
    )
    route = RayTargetRoute.objects.create(
        backend_alias=f"evidence-route-{suffix}",
        created_at=observed_at,
    )
    route_revision = RayTargetRouteRevision.objects.create(
        route=route,
        revision=1,
        target_policy=policy,
        created_at=observed_at,
    )
    binding = RayTaskTargetBinding.objects.create(
        execution=execution,
        target_policy=policy,
        created_at=observed_at,
    )
    selection = RayTaskTargetRouteSelection.objects.create(
        binding=binding,
        route_revision=route_revision,
        created_at=observed_at,
    )
    capability = RayWorkerTargetCapability.objects.create(
        lease=lease,
        lease_hostname=lease.hostname,
        lease_pid=lease.pid,
        lease_started_at=lease.started_at,
        target=target,
        target_policy=policy,
        attestation=attestation,
        runner_family=target.runner_family,
        manager_ray_major=target.ray_major,
        manager_ray_minor=target.ray_minor,
        manager_ray_patch=target.ray_patch,
        manager_python_implementation=target.python_implementation,
        manager_python_major=target.python_major,
        manager_python_minor=target.python_minor,
        manager_python_patch=target.python_patch,
        revision=1,
        created_at=advertised_at,
        advertised_at=advertised_at,
    )
    return _Lineage(
        execution=execution,
        target=target,
        policy=policy,
        attestation=attestation,
        route=route,
        route_revision=route_revision,
        binding=binding,
        selection=selection,
        lease=lease,
        capability=capability,
        claimed_at=claimed_at,
    )


def _claim(lineage: _Lineage, *, attempt: int = 1, generation: int = 1):
    return RayTaskTargetExecutionEvidenceClaim(
        execution_id=lineage.execution.pk,
        task_id=lineage.execution.task_id,
        attempt_number=attempt,
        execution_generation=generation,
        route_selection_id=lineage.selection.pk,
        route_backend_alias=lineage.route.pk,
        route_revision_id=lineage.route_revision.pk,
        route_revision=lineage.route_revision.revision,
        selected_target_policy_id=lineage.binding.target_policy_id,
        target_id=lineage.target.pk,
        target_policy_id=lineage.policy.pk,
        claim_attestation_id=lineage.attestation.pk,
        target_expectation_digest=lineage.policy.expectation_digest,
        claim_attestation_digest=lineage.attestation.attestation_digest,
        worker_target_capability_id=lineage.capability.pk,
        worker_target_capability_schema_version=lineage.capability.schema_version,
        worker_target_capability_revision=lineage.capability.revision,
        worker_target_capability_advertised_at=lineage.capability.advertised_at,
        worker_lease_id=lineage.lease.pk,
        worker_lease_hostname=lineage.lease.hostname,
        worker_lease_pid=lineage.lease.pid,
        worker_lease_started_at=lineage.lease.started_at,
        runner_family=lineage.capability.runner_family,
        manager_ray_major=lineage.capability.manager_ray_major,
        manager_ray_minor=lineage.capability.manager_ray_minor,
        manager_ray_patch=lineage.capability.manager_ray_patch,
        manager_python_implementation=lineage.capability.manager_python_implementation,
        manager_python_major=lineage.capability.manager_python_major,
        manager_python_minor=lineage.capability.manager_python_minor,
        manager_python_patch=lineage.capability.manager_python_patch,
        claimed_at=lineage.claimed_at,
    )


def _evidence_values(
    lineage: _Lineage,
    *,
    attempt: int = 1,
    generation: int = 1,
    evidence_digest: str | None = None,
) -> dict[str, object]:
    if evidence_digest is None:
        evidence_digest = ray_task_target_execution_evidence_digest(
            _claim(lineage, attempt=attempt, generation=generation)
        )
    return {
        "execution_id": lineage.execution.pk,
        "task_id": lineage.execution.task_id,
        "route_selection_id": lineage.selection.pk,
        "attempt_number": attempt,
        "execution_generation": generation,
        "target_id": lineage.target.pk,
        "target_policy_id": lineage.policy.pk,
        "claim_attestation_id": lineage.attestation.pk,
        "worker_target_capability_id": lineage.capability.pk,
        "worker_target_capability_schema_version": lineage.capability.schema_version,
        "worker_target_capability_revision": lineage.capability.revision,
        "worker_target_capability_advertised_at": lineage.capability.advertised_at,
        "worker_lease_id": lineage.lease.pk,
        "worker_lease_hostname": lineage.lease.hostname,
        "worker_lease_pid": lineage.lease.pid,
        "worker_lease_started_at": lineage.lease.started_at,
        "runner_family": lineage.capability.runner_family,
        "manager_ray_major": lineage.capability.manager_ray_major,
        "manager_ray_minor": lineage.capability.manager_ray_minor,
        "manager_ray_patch": lineage.capability.manager_ray_patch,
        "manager_python_implementation": lineage.capability.manager_python_implementation,
        "manager_python_major": lineage.capability.manager_python_major,
        "manager_python_minor": lineage.capability.manager_python_minor,
        "manager_python_patch": lineage.capability.manager_python_patch,
        "target_execution_evidence_digest": evidence_digest,
        "target_expectation_digest": lineage.policy.expectation_digest,
        "claim_attestation_digest": lineage.attestation.attestation_digest,
        "schema_version": 1,
        "claimed_at": lineage.claimed_at,
    }


def _create_evidence(
    lineage: _Lineage,
    *,
    attempt: int = 1,
    generation: int = 1,
    synchronize_execution: bool = True,
) -> RayTaskTargetExecutionEvidence:
    if synchronize_execution:
        RayTaskExecution.objects.filter(pk=lineage.execution.pk).update(
            state="RUNNING",
            attempt_number=attempt,
            execution_generation=generation,
            claimed_by_worker=lineage.lease.pk,
            started_at=lineage.claimed_at,
            finished_at=None,
        )
    return RayTaskTargetExecutionEvidence.objects.create(
        **_evidence_values(lineage, attempt=attempt, generation=generation)
    )


def _observation_values(lineage: _Lineage, evidence) -> dict[str, object]:
    observed_at = lineage.claimed_at + timedelta(microseconds=1)
    return {
        "evidence_id": evidence.pk,
        "status": "VERIFIED",
        "application_invoked": True,
        "compatibility_reason": None,
        "target_execution_evidence_digest": evidence.target_execution_evidence_digest,
        "target_expectation_digest": evidence.target_expectation_digest,
        "claim_attestation_digest": evidence.claim_attestation_digest,
        "observed_cluster_session": lineage.target.cluster_session,
        "observed_node_id": _NODE_ID,
        "observed_membership_digest": lineage.attestation.membership_digest,
        "observed_ray_major": lineage.target.ray_major,
        "observed_ray_minor": lineage.target.ray_minor,
        "observed_ray_patch": lineage.target.ray_patch,
        "observed_python_implementation": lineage.target.python_implementation,
        "observed_python_major": lineage.target.python_major,
        "observed_python_minor": lineage.target.python_minor,
        "observed_python_patch": lineage.target.python_patch,
        "observed_proof_digest": _DIGEST_D,
        "observed_at": observed_at,
        "recorded_at": observed_at,
    }


@pytest.mark.django_db(transaction=True)
def test_evidence_and_outcome_are_create_once_and_survive_ephemeral_cleanup() -> None:
    lineage = _create_lineage()
    evidence = _create_evidence(lineage)
    outcome = RayTaskTargetExecutionOutcome.objects.create(**_observation_values(lineage, evidence))

    assert evidence.pk > 0
    assert outcome.application_invoked is True
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskTargetExecutionEvidence.objects.filter(pk=evidence.pk).update(attempt_number=2)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskTargetExecutionOutcome.objects.filter(pk=evidence.pk).update(status="UNCERTAIN")
    with pytest.raises(DatabaseError), transaction.atomic():
        _create_evidence(lineage)

    for protected_parent in (
        lineage.execution,
        lineage.selection,
        lineage.binding,
        lineage.target,
        lineage.policy,
        lineage.attestation,
    ):
        with pytest.raises(ProtectedError):
            protected_parent.delete()

    worker_lease_id = lineage.lease.pk
    lineage.capability.delete()
    lineage.lease.delete()
    evidence.refresh_from_db()
    assert evidence.worker_lease_id == worker_lease_id
    with pytest.raises(ProtectedError):
        evidence.delete()
    outcome.delete()
    evidence.delete()
    assert not RayTaskTargetExecutionEvidence.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_identity_snapshots_preserve_existing_character_contract() -> None:
    identity_text = "é" * 252 + "e\u0301\n"
    assert len(identity_text) == 255
    lineage = _create_lineage(suffix="identity-text", identity_text=identity_text)

    evidence = _create_evidence(lineage)

    assert evidence.task_id == identity_text
    assert evidence.worker_lease_id == identity_text
    assert evidence.worker_lease_hostname == identity_text


def _assert_evidence_rejected(values: dict[str, object]) -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskTargetExecutionEvidence.objects.create(**values)


@pytest.mark.django_db(transaction=True)
def test_evidence_requires_exact_running_generation_owner_and_bound_selection() -> None:
    protocol_one = _create_lineage(
        suffix="protocol-one",
        execution_protocol_version=1,
    )
    _assert_evidence_rejected(_evidence_values(protocol_one))

    lineage = _create_lineage(suffix="execution-identity")
    _assert_evidence_rejected({**_evidence_values(lineage), "task_id": "wrong-task"})
    _assert_evidence_rejected(_evidence_values(lineage, attempt=2))
    _assert_evidence_rejected(_evidence_values(lineage, generation=2))

    RayTaskExecution.objects.filter(pk=lineage.execution.pk).update(
        claimed_by_worker=lineage.lease.pk,
        state="SUCCEEDED",
        finished_at=lineage.claimed_at,
    )
    _assert_evidence_rejected(_evidence_values(lineage))
    RayTaskExecution.objects.filter(pk=lineage.execution.pk).update(
        state="RUNNING",
        started_at=None,
        finished_at=None,
    )
    _assert_evidence_rejected(_evidence_values(lineage))

    other = _create_lineage(suffix="other-selection")
    wrong_selection = {
        **_evidence_values(other),
        "execution_id": lineage.execution.pk,
        "route_selection_id": other.selection.pk,
    }
    _assert_evidence_rejected(wrong_selection)
    wrong_target = {
        **_evidence_values(other),
        "execution_id": lineage.execution.pk,
        "route_selection_id": lineage.selection.pk,
    }
    _assert_evidence_rejected(wrong_target)


@pytest.mark.django_db(transaction=True)
def test_evidence_rejects_stale_heads_capability_and_inactive_lease() -> None:
    stale_policy = _create_lineage(suffix="stale-policy")
    RayTargetPolicyRevision.objects.create(
        target=stale_policy.target,
        revision=2,
        desired_state="active",
        expectation_schema_version=1,
        expectation_json='{"schema":"expectation-2"}',
        expectation_digest=_DIGEST_D,
        created_at=stale_policy.claimed_at,
    )
    _assert_evidence_rejected(_evidence_values(stale_policy))
    RayTargetPolicyRevision.objects.create(
        target=stale_policy.target,
        revision=4,
        desired_state="active",
        expectation_schema_version=1,
        expectation_json='{"schema":"expectation-4"}',
        expectation_digest=_DIGEST_D,
        created_at=stale_policy.claimed_at,
    )
    _assert_evidence_rejected(_evidence_values(stale_policy))

    stale_attestation = _create_lineage(suffix="stale-attestation")
    RayTargetAttestationRevision.objects.create(
        policy=stale_attestation.policy,
        revision=2,
        attestation_schema_version=1,
        attestation_json='{"schema":"attestation-2"}',
        expectation_digest=stale_attestation.policy.expectation_digest,
        membership_digest=_DIGEST_D,
        attestation_digest=_DIGEST_D,
        observed_at=cast(datetime, stale_attestation.attestation.observed_at)
        + timedelta(microseconds=1),
        expires_at=cast(datetime, stale_attestation.attestation.expires_at)
        + timedelta(microseconds=1),
        recorded_at=cast(datetime, stale_attestation.attestation.recorded_at)
        + timedelta(microseconds=1),
    )
    _assert_evidence_rejected(_evidence_values(stale_attestation))
    RayTargetAttestationRevision.objects.create(
        policy=stale_attestation.policy,
        revision=4,
        attestation_schema_version=1,
        attestation_json='{"schema":"attestation-4"}',
        expectation_digest=stale_attestation.policy.expectation_digest,
        membership_digest=_DIGEST_D,
        attestation_digest=_DIGEST_D,
        observed_at=stale_attestation.attestation.observed_at,
        expires_at=stale_attestation.attestation.expires_at,
        recorded_at=stale_attestation.attestation.recorded_at,
    )
    _assert_evidence_rejected(_evidence_values(stale_attestation))

    stale_capability = _create_lineage(suffix="stale-capability")
    RayWorkerTargetCapability.objects.filter(pk=stale_capability.capability.pk).update(
        revision=2,
        advertised_at=(
            cast(datetime, stale_capability.capability.advertised_at) + timedelta(microseconds=1)
        ),
    )
    _assert_evidence_rejected(_evidence_values(stale_capability))

    inactive_lease = _create_lineage(suffix="inactive-lease")
    TaskWorkerLease.objects.filter(pk=inactive_lease.lease.pk).update(
        is_active=False,
        stopped_at=inactive_lease.claimed_at,
    )
    _assert_evidence_rejected(_evidence_values(inactive_lease))


@pytest.mark.django_db(transaction=True)
def test_outcome_verdicts_require_exact_invocation_and_complete_proof() -> None:
    lineage = _create_lineage(suffix="verdicts")
    rejected_evidence = _create_evidence(lineage)
    rejected = _observation_values(lineage, rejected_evidence)
    rejected.update(
        status="COMPATIBILITY_REJECTED",
        application_invoked=False,
        compatibility_reason="ray_version_mismatch",
        observed_ray_patch=cast(int, lineage.target.ray_patch) + 1,
    )
    RayTaskTargetExecutionOutcome.objects.create(**rejected)

    uncertain_evidence = _create_evidence(lineage, generation=2)
    RayTaskTargetExecutionOutcome.objects.create(
        evidence=uncertain_evidence,
        status="UNCERTAIN",
        application_invoked=None,
        compatibility_reason=None,
        target_execution_evidence_digest=(uncertain_evidence.target_execution_evidence_digest),
        target_expectation_digest=uncertain_evidence.target_expectation_digest,
        claim_attestation_digest=uncertain_evidence.claim_attestation_digest,
        recorded_at=lineage.claimed_at,
    )

    invalid = _observation_values(lineage, _create_evidence(lineage, generation=3))
    invalid["observed_proof_digest"] = None
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskTargetExecutionOutcome.objects.create(**invalid)

    uncertain_with_proof = _observation_values(
        lineage,
        _create_evidence(lineage, generation=4),
    )
    uncertain_with_proof.update(status="UNCERTAIN", application_invoked=None)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskTargetExecutionOutcome.objects.create(**uncertain_with_proof)

    unsupported_reason = _observation_values(
        lineage,
        _create_evidence(lineage, generation=5),
    )
    unsupported_reason.update(
        status="COMPATIBILITY_REJECTED",
        application_invoked=False,
        compatibility_reason="not_yet_valid",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskTargetExecutionOutcome.objects.create(**unsupported_reason)

    backwards_observation = _observation_values(
        lineage,
        _create_evidence(lineage, generation=6),
    )
    backwards_observation.update(
        status="COMPATIBILITY_REJECTED",
        application_invoked=False,
        compatibility_reason="expired",
        observed_at=lineage.claimed_at - timedelta(microseconds=1),
        recorded_at=lineage.claimed_at,
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskTargetExecutionOutcome.objects.create(**backwards_observation)


def _stored_datetime(value: datetime) -> datetime:
    if connection.vendor == "sqlite":
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _raw_insert(model, values: dict[str, object]) -> None:
    quote = connection.ops.quote_name
    columns = ", ".join(quote(column) for column in values)
    placeholders = ", ".join(["%s"] * len(values))
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {quote(model._meta.db_table)} ({columns}) VALUES ({placeholders})",
            list(values.values()),
        )


def _close_owned_thread_connection() -> None:
    thread_connection = connections["default"]
    raw_connection = thread_connection.connection
    if raw_connection is None:
        return
    try:
        raw_connection.rollback()
    finally:
        raw_connection.close()
        thread_connection.connection = None


def _raw_evidence_values(
    lineage: _Lineage,
    *,
    evidence_digest: str | None = None,
) -> dict[str, object]:
    values = _evidence_values(lineage, evidence_digest=evidence_digest)
    for field in (
        "worker_target_capability_advertised_at",
        "worker_lease_started_at",
        "claimed_at",
    ):
        values[field] = _stored_datetime(values[field])  # type: ignore[arg-type]
    return {"target_execution_evidence_id": 1001, **values}


def _assert_raw_fences(*, sqlite_dynamic_types: bool) -> None:
    lineage = _create_lineage(suffix=f"raw-{connection.vendor}")
    valid = _raw_evidence_values(lineage)
    mutations: list[tuple[str, object]] = [
        ("task_id", "different-task"),
        ("attempt_number", 0),
        ("attempt_number", 2_147_483_648),
        ("execution_generation", 0),
        ("route_selection_id", lineage.selection.pk + 1000),
        ("target_policy_id", lineage.policy.pk + 1000),
        ("claim_attestation_id", lineage.attestation.pk + 1000),
        ("worker_target_capability_id", lineage.capability.pk + 1000),
        ("worker_target_capability_revision", 2),
        ("worker_lease_hostname", "other.example"),
        ("worker_lease_pid", 4321),
        ("worker_lease_pid", 2_147_483_648),
        ("runner_family", "ray_job"),
        ("manager_ray_patch", 1),
        ("manager_python_implementation", "CPython"),
        ("manager_python_implementation", "cpython\n"),
        ("target_execution_evidence_digest", "bad"),
        ("target_execution_evidence_digest", f"{_DIGEST_A}\n"),
        ("target_expectation_digest", _DIGEST_D),
        ("claim_attestation_digest", _DIGEST_D),
        ("schema_version", 2),
        (
            "claimed_at",
            _stored_datetime(cast(datetime, lineage.attestation.expires_at) + timedelta(seconds=1)),
        ),
    ]
    if sqlite_dynamic_types:
        mutations.extend(
            (
                ("attempt_number", 1.5),
                ("task_id", f"{lineage.execution.task_id}\x00suffix"),
                ("task_id", lineage.execution.task_id.encode()),
                ("execution_generation", b"1"),
                ("target_id", f"{lineage.target.pk}\x00suffix"),
                ("worker_lease_id", f"{lineage.lease.pk}\x00suffix"),
                ("worker_lease_hostname", b"worker.example"),
                ("worker_lease_hostname", "worker.example\x00suffix"),
                ("manager_ray_major", 2.5),
                ("manager_python_implementation", "cpython\x00suffix"),
                ("target_execution_evidence_digest", _DIGEST_A.encode()),
                ("claimed_at", "0000-01-01 00:00:00"),
                ("claimed_at", "2026-02-30 20:00:00"),
                ("claimed_at", "2026-08-15T20:00:00"),
                ("claimed_at", "2026-08-15 20:00:00.12345"),
                ("claimed_at", "now"),
                ("claimed_at", b"2026-08-15 20:00:00"),
            )
        )
    for field, value in mutations:
        with pytest.raises(DatabaseError), transaction.atomic():
            _raw_insert(RayTaskTargetExecutionEvidence, {**valid, field: value})
        assert not RayTaskTargetExecutionEvidence.objects.exists(), (field, value)

    invalid_chronology = _create_lineage(
        suffix=f"raw-chronology-{connection.vendor}",
        capability_precedes_lease=True,
    )
    invalid_chronology_values = {
        **_raw_evidence_values(invalid_chronology, evidence_digest=_DIGEST_D),
        "target_execution_evidence_id": 1002,
    }
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw_insert(RayTaskTargetExecutionEvidence, invalid_chronology_values)

    if sqlite_dynamic_types:
        poisoned_parent = _create_lineage(suffix="raw-poisoned-started-at")
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(RayTaskExecution._meta.db_table)} "
                "SET started_at = %s WHERE id = %s",
                ["0000-01-01 00:00:00", poisoned_parent.execution.pk],
            )
        poisoned_values = {
            **_raw_evidence_values(poisoned_parent),
            "target_execution_evidence_id": 1003,
        }
        with pytest.raises(DatabaseError), transaction.atomic():
            _raw_insert(RayTaskTargetExecutionEvidence, poisoned_values)

    _raw_insert(RayTaskTargetExecutionEvidence, valid)
    evidence = RayTaskTargetExecutionEvidence.objects.get(pk=1001)
    outcome = _observation_values(lineage, evidence)
    for field in ("observed_at", "recorded_at"):
        outcome[field] = _stored_datetime(outcome[field])  # type: ignore[arg-type]
    outcome_mutations: list[tuple[str, object]] = [
        ("status", "CLAIMED"),
        ("application_invoked", False),
        ("compatibility_reason", "runtime_mismatch"),
        ("target_execution_evidence_digest", _DIGEST_D),
        ("target_expectation_digest", _DIGEST_D),
        ("claim_attestation_digest", _DIGEST_D),
        ("observed_cluster_session", "session_wrong"),
        ("observed_cluster_session", f"{lineage.target.cluster_session}\n"),
        ("observed_node_id", f"{_NODE_ID}\n"),
        ("observed_membership_digest", _DIGEST_D),
        ("observed_membership_digest", f"{_DIGEST_B}\n"),
        ("observed_ray_patch", 1),
        ("observed_python_implementation", "cpython\n"),
        ("observed_proof_digest", None),
        ("observed_proof_digest", f"{_DIGEST_D}\n"),
        (
            "observed_at",
            _stored_datetime(cast(datetime, lineage.attestation.expires_at) + timedelta(seconds=1)),
        ),
        (
            "observed_at",
            _stored_datetime(lineage.claimed_at - timedelta(microseconds=1)),
        ),
    ]
    if sqlite_dynamic_types:
        outcome_mutations.extend(
            (
                ("application_invoked", 2),
                ("observed_cluster_session", "session_wrong\x00suffix"),
                ("observed_cluster_session", b"session_wrong"),
                ("observed_node_id", b"1" * 56),
                ("observed_node_id", f"{_NODE_ID}\x00suffix"),
                ("observed_ray_major", 2.5),
                ("observed_proof_digest", _DIGEST_D.encode()),
                ("observed_at", "2026-02-30 20:00:00"),
                ("observed_at", "2026-08-15T20:00:00"),
                ("observed_at", b"2026-08-15 20:00:00"),
                ("recorded_at", "now"),
            )
        )
    for field, value in outcome_mutations:
        try:
            with transaction.atomic():
                _raw_insert(RayTaskTargetExecutionOutcome, {**outcome, field: value})
        except DatabaseError:
            pass
        else:
            pytest.fail(f"raw outcome {field}={value!r} was accepted")
        assert not RayTaskTargetExecutionOutcome.objects.exists(), (field, value)

    _raw_insert(RayTaskTargetExecutionOutcome, outcome)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(RayTaskTargetExecutionOutcome._meta.db_table)} "
            "SET status = status WHERE evidence_id = %s",
            [evidence.pk],
        )


@pytest.mark.django_db(transaction=True)
def test_sqlite_raw_evidence_and_outcome_fences_match_orm_contract() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_raw_fences(sqlite_dynamic_types=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_raw_evidence_and_outcome_fences_match_orm_contract() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_raw_fences(sqlite_dynamic_types=False)


@pytest.mark.django_db(transaction=True)
def test_migration_is_additive_and_reverse_refuses_retained_evidence() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        legacy = old_execution.objects.create(
            task_id="evidence-old-writer",
            callable_path="tests.tasks.legacy",
            state="SUCCEEDED",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        assert not new_apps.get_model(
            "django_ray", "RayTaskTargetExecutionEvidence"
        ).objects.exists()
        assert old_execution.objects.filter(pk=legacy.pk).exists()

        lineage = _create_lineage(suffix="reverse")
        evidence = _create_evidence(lineage)
        with pytest.raises(RuntimeError, match="both tables to be empty"):
            MigrationExecutor(connection).migrate(MIGRATE_FROM)
        assert RayTaskTargetExecutionEvidence.objects.filter(pk=evidence.pk).exists()

        evidence.delete()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        with pytest.raises(LookupError):
            executor.loader.project_state(MIGRATE_FROM).apps.get_model(
                "django_ray", "RayTaskTargetExecutionEvidence"
            )
    finally:
        MigrationExecutor(connection).migrate(LATEST)


def _hold_evidence_writer(
    values: dict[str, object],
    writer_inserted: Event,
    release_writer: Event,
) -> None:
    close_old_connections()
    try:
        with transaction.atomic():
            _raw_insert(RayTaskTargetExecutionEvidence, values)
            writer_inserted.set()
            if not release_writer.wait(timeout=20):
                raise TimeoutError("test did not release the evidence writer")
    finally:
        _close_owned_thread_connection()


def _attempt_sqlite_reverse(reverse_started: Event) -> str:
    close_old_connections()
    try:
        thread_connection = connections["default"]
        with thread_connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout = 0")
        reverse_started.set()
        try:
            MigrationExecutor(thread_connection).migrate(MIGRATE_FROM)
        except OperationalError:
            return "writer-locked"
        except RuntimeError:
            return "history-refused"
        raise AssertionError("rollback unexpectedly removed target execution evidence")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
def test_sqlite_evidence_writer_cannot_race_partial_schema_reverse() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    lineage = _create_lineage(suffix="sqlite-writer-fence")
    values = {**_raw_evidence_values(lineage), "target_execution_evidence_id": 2001}
    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(
                _hold_evidence_writer,
                values,
                writer_inserted,
                release_writer,
            )
            assert writer_inserted.wait(timeout=20)
            reverse = pool.submit(_attempt_sqlite_reverse, reverse_started)
            assert reverse_started.wait(timeout=20)
            result = reverse.result(timeout=20)
            release_writer.set()
            writer.result(timeout=20)
        assert result in {"writer-locked", "history-refused"}
        assert RayTaskTargetExecutionEvidence.objects.filter(pk=2001).exists()
    finally:
        release_writer.set()
        MigrationExecutor(connection).migrate(LATEST)
        RayTaskTargetExecutionOutcome.objects.all().delete()
        RayTaskTargetExecutionEvidence.objects.filter(pk=2001).delete()


def _postgresql_backend_pid() -> int:
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _attempt_postgresql_reverse(reverse_started: Event, backend_pid: list[int]) -> str:
    close_old_connections()
    try:
        thread_connection = connections["default"]
        backend_pid.append(_postgresql_backend_pid())
        reverse_started.set()
        try:
            MigrationExecutor(thread_connection).migrate(MIGRATE_FROM)
        except RuntimeError:
            return "history-refused"
        raise AssertionError("rollback unexpectedly removed target execution evidence")
    finally:
        _close_owned_thread_connection()


def _wait_for_postgresql_lock(backend_pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                [backend_pid],
            )
            row = cursor.fetchone()
        if row is not None and row[0] == "Lock":
            return
        time.sleep(0.05)
    raise TimeoutError("rollback did not wait on the evidence writer lock")


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_evidence_writer_serializes_before_reverse_guard() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    lineage = _create_lineage(suffix="postgres-writer-fence")
    values = {**_raw_evidence_values(lineage), "target_execution_evidence_id": 2001}
    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    backend_pid: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(
                _hold_evidence_writer,
                values,
                writer_inserted,
                release_writer,
            )
            assert writer_inserted.wait(timeout=20)
            reverse = pool.submit(
                _attempt_postgresql_reverse,
                reverse_started,
                backend_pid,
            )
            assert reverse_started.wait(timeout=20)
            try:
                _wait_for_postgresql_lock(backend_pid[0])
            finally:
                release_writer.set()
            writer.result(timeout=20)
            assert reverse.result(timeout=20) == "history-refused"
        assert RayTaskTargetExecutionEvidence.objects.filter(pk=2001).exists()
    finally:
        release_writer.set()
        MigrationExecutor(connection).migrate(LATEST)
        RayTaskTargetExecutionOutcome.objects.all().delete()
        RayTaskTargetExecutionEvidence.objects.filter(pk=2001).delete()
