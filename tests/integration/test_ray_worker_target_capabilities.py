"""SQLite and PostgreSQL contracts for dormant worker target capabilities."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from inspect import signature
from threading import Barrier
from typing import Any

import pytest
from django.db import (
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext

import django_ray.target_capabilities as capabilities
from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.target_attestation import (
    RayClusterAttestation,
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetAttestationRejection,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    encode_ray_cluster_attestation,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)
from django_ray.target_capabilities import (
    InvalidRayWorkerTargetCapabilityArgumentError,
    NestedRayWorkerTargetCapabilityTransactionError,
    RayJobWorkerTargetCapabilityUnsupportedError,
    RayWorkerTargetCapabilityAdvertisementRegressionError,
    RayWorkerTargetCapabilityAttestationRevisionConflictError,
    RayWorkerTargetCapabilityAttestationStateError,
    RayWorkerTargetCapabilityChange,
    RayWorkerTargetCapabilityLeaseError,
    RayWorkerTargetCapabilityLimitError,
    RayWorkerTargetCapabilityPersistenceRaceError,
    RayWorkerTargetCapabilityPolicyRevisionConflictError,
    RayWorkerTargetCapabilityRevisionConflictError,
    RayWorkerTargetCapabilityRevisionExhaustedError,
    RayWorkerTargetCapabilityRuntimeMismatchError,
    RayWorkerTargetCapabilityStateError,
    RayWorkerTargetCapabilityTargetStateError,
    UnsupportedRayWorkerTargetCapabilityDatabaseError,
    advertise_ray_worker_target_capability,
    withdraw_all_ray_worker_target_capabilities,
    withdraw_ray_worker_target_capability,
)
from django_ray.target_coordination import (
    record_ray_target_attestation,
    register_ray_target,
    transition_ray_target_desired_state,
)

pytestmark = pytest.mark.django_db(transaction=True)

NOW = datetime(2026, 8, 15, 21, 0, 0, 123456, tzinfo=UTC)
ADVERTISED_AT = NOW + timedelta(seconds=5)
NODE_ID = "b" * 56


def _runtime(**changes: object) -> RayRuntimeVersion:
    values: dict[str, object] = {
        "ray_major": 2,
        "ray_minor": 56,
        "ray_patch": 0,
        "python_implementation": "cpython",
        "python_major": 3,
        "python_minor": 12,
        "python_patch": 12,
    }
    values.update(changes)
    return RayRuntimeVersion(**values)  # type: ignore[arg-type]


def _expectation(
    target_key: str = "target.primary",
    *,
    session_suffix: str = "1",
    policy_revision: int = 1,
    runner_family: RayRunnerFamily = RayRunnerFamily.RAY_CORE,
) -> RayTargetExpectation:
    return RayTargetExpectation(
        target_key=target_key,
        runner_family=runner_family,
        cluster_session=f"session_2026-08-15_13-00-00_123456_{session_suffix}",
        policy_revision=policy_revision,
        runtime=_runtime(),
    )


def _attestation(
    expectation: RayTargetExpectation,
    *,
    observed_at: datetime = NOW - timedelta(seconds=1),
    expires_at: datetime = NOW + timedelta(seconds=50),
) -> RayClusterAttestation:
    boundary = build_ray_observation_boundary(
        resource_state_version_before=10,
        resource_state_version_after=11,
        node_state_versions_before=(RayNodeStateVersion(node_id=NODE_ID, node_state_version=20),),
        node_state_versions_after=(RayNodeStateVersion(node_id=NODE_ID, node_state_version=21),),
    )
    node = build_ray_node_observation(
        node_id=NODE_ID,
        cluster_session=expectation.cluster_session,
        runtime=expectation.runtime,
    )
    return build_ray_cluster_attestation(
        expectation=expectation,
        boundary=boundary,
        nodes=(node,),
        observed_at=observed_at,
        expires_at=expires_at,
    )


def _target(
    target_key: str = "target.primary",
    *,
    session_suffix: str = "1",
    desired_state: RayTargetDesiredState = RayTargetDesiredState.DRAINING,
) -> tuple[RayTargetExpectation, RayTargetPolicyRevision, RayTargetAttestationRevision]:
    registration = register_ray_target(_expectation(target_key, session_suffix=session_suffix))
    expectation = registration.expectation
    if desired_state is RayTargetDesiredState.ACTIVE:
        expectation = transition_ray_target_desired_state(
            target_key,
            desired_state,
            expected_policy_revision=1,
        ).expectation
    proof = _attestation(expectation)
    record = record_ray_target_attestation(
        target_key,
        proof,
        expected_policy_revision=expectation.policy_revision,
        expected_attestation_revision=0,
        now=NOW,
    )
    policy = RayTargetPolicyRevision.objects.get(
        target_id=target_key,
        revision=expectation.policy_revision,
    )
    attestation = RayTargetAttestationRevision.objects.get(
        policy=policy,
        revision=record.revision,
    )
    return expectation, policy, attestation


def _lease(
    worker_id: str = "worker-primary",
    *,
    heartbeat_at: datetime = NOW + timedelta(seconds=4),
) -> tuple[TaskWorkerLease, WorkerLeaseIdentity]:
    lease = TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname="worker-host",
        pid=2101,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
        started_at=NOW - timedelta(minutes=1),
        last_heartbeat_at=heartbeat_at,
    )
    identity = WorkerLeaseIdentity(
        worker_id=worker_id,
        hostname=str(lease.hostname),
        pid=int(lease.pid),
        started_at=lease.started_at,
    )
    return lease, identity


def _advertise(
    identity: WorkerLeaseIdentity,
    expectation: RayTargetExpectation,
    *,
    expected_capability_revision: int = 0,
    expected_attestation_revision: int = 1,
    now: datetime = ADVERTISED_AT,
) -> RayWorkerTargetCapabilityChange:
    return advertise_ray_worker_target_capability(
        identity,
        expectation.target_key,
        expectation.runtime,
        manager_runner_family=expectation.runner_family,
        expected_policy_revision=expectation.policy_revision,
        expected_attestation_revision=expected_attestation_revision,
        expected_capability_revision=expected_capability_revision,
        now=now,
    )


def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def _run_concurrently(*operations):
    barrier = Barrier(len(operations))

    def invoke(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return operation()
            except Exception as error:
                return error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        return [future.result(timeout=30) for future in futures]


@pytest.mark.parametrize(
    "desired_state",
    [RayTargetDesiredState.ACTIVE, RayTargetDesiredState.DRAINING],
)
def test_advertisement_persists_the_exact_live_lease_target_and_runtime(
    desired_state: RayTargetDesiredState,
) -> None:
    expectation, policy, attestation = _target(desired_state=desired_state)
    lease, identity = _lease()
    original_heartbeat = lease.last_heartbeat_at

    result = _advertise(identity, expectation)

    assert result == RayWorkerTargetCapabilityChange(
        target_key=expectation.target_key,
        target_policy_revision=expectation.policy_revision,
        attestation_revision=1,
        manager_runner_family=RayRunnerFamily.RAY_CORE,
        manager_runtime=expectation.runtime,
        changed=True,
        previous_revision=0,
        revision=1,
        advertised_at=ADVERTISED_AT,
    )
    row = RayWorkerTargetCapability.objects.get()
    assert (row.lease_id, row.target_id, row.target_policy_id, row.attestation_id) == (
        lease.pk,
        expectation.target_key,
        policy.pk,
        attestation.pk,
    )
    assert (row.lease_hostname, row.lease_pid, row.lease_started_at) == (
        lease.hostname,
        lease.pid,
        lease.started_at,
    )
    lease.refresh_from_db()
    assert lease.last_heartbeat_at == original_heartbeat


def test_exact_create_and_renewal_replays_are_idempotent() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    created = _advertise(identity, expectation)
    create_retry = _advertise(identity, expectation)
    current_retry = _advertise(
        identity,
        expectation,
        expected_capability_revision=1,
    )

    renewed_at = ADVERTISED_AT + timedelta(seconds=1)
    renewed = _advertise(
        identity,
        expectation,
        expected_capability_revision=1,
        now=renewed_at,
    )
    renewal_retry = _advertise(
        identity,
        expectation,
        expected_capability_revision=1,
        now=renewed_at,
    )

    assert created.changed is True
    assert (create_retry.changed, create_retry.revision) == (False, 1)
    assert (current_retry.changed, current_retry.revision) == (False, 1)
    assert (renewed.changed, renewed.previous_revision, renewed.revision) == (True, 1, 2)
    assert (renewal_retry.changed, renewal_retry.revision) == (False, 2)
    row = RayWorkerTargetCapability.objects.get()
    assert (row.revision, row.advertised_at) == (2, renewed_at)


def test_changed_timestamp_with_stale_capability_revision_conflicts() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    _advertise(identity, expectation)
    _advertise(
        identity,
        expectation,
        expected_capability_revision=1,
        now=ADVERTISED_AT + timedelta(seconds=1),
    )

    with pytest.raises(RayWorkerTargetCapabilityRevisionConflictError) as stale:
        _advertise(
            identity,
            expectation,
            expected_capability_revision=1,
            now=ADVERTISED_AT + timedelta(seconds=2),
        )

    assert (stale.value.expected_revision, stale.value.actual_revision) == (1, 2)
    assert str(stale.value) == "Ray worker target capability revision changed"


def test_policy_and_attestation_revisions_are_exact_cas_inputs() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()

    with pytest.raises(RayWorkerTargetCapabilityPolicyRevisionConflictError) as policy:
        advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=expectation.policy_revision + 1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        )
    assert (policy.value.expected_revision, policy.value.actual_revision) == (2, 1)

    with pytest.raises(RayWorkerTargetCapabilityAttestationRevisionConflictError) as attestation:
        _advertise(identity, expectation, expected_attestation_revision=2)
    assert (attestation.value.expected_revision, attestation.value.actual_revision) == (2, 1)


def test_advertisement_requires_an_exact_fresh_explicit_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation, _policy, _attestation_row = _target()
    lease, identity = _lease()
    mismatched = replace(identity, hostname="other-host")

    with pytest.raises(RayWorkerTargetCapabilityLeaseError):
        _advertise(mismatched, expectation)

    lease.last_heartbeat_at = NOW - timedelta(minutes=2)
    lease.save(update_fields=["last_heartbeat_at"])
    with pytest.raises(RayWorkerTargetCapabilityLeaseError):
        _advertise(identity, expectation)

    poison = "private-settings-path"

    def fail_duration():
        raise RuntimeError(poison)

    lease.last_heartbeat_at = NOW + timedelta(seconds=4)
    lease.save(update_fields=["last_heartbeat_at"])
    monkeypatch.setattr(capabilities, "get_lease_duration", fail_duration)
    with pytest.raises(RayWorkerTargetCapabilityLeaseError) as fixed:
        _advertise(identity, expectation)
    assert poison not in str(fixed.value)
    assert not RayWorkerTargetCapability.objects.exists()


@pytest.mark.parametrize(
    ("poisoned_worker_id", "minimum", "maximum"),
    (
        ("worker-protocol-text-poison", "private-a", "private-z"),
        ("worker-protocol-fraction-poison", 1.5, 1.5),
        ("worker-protocol-maximum-text-poison", 1, "private-z"),
        ("worker-protocol-minimum-overflow", 32768, 32768),
        ("worker-protocol-maximum-overflow", 1, 32768),
    ),
)
def test_sqlite_raw_lease_protocol_poison_maps_to_fixed_refusal(
    poisoned_worker_id: str,
    minimum: object,
    maximum: object,
) -> None:
    if connection.vendor != "sqlite":
        pytest.skip("SQLite storage-class regression")

    expectation, policy, attestation_row = _target()
    lease, _identity = _lease()
    table = connection.ops.quote_name(TaskWorkerLease._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {table} WHERE worker_id = %s", [lease.pk])
        column_names = [column[0] for column in cursor.description]
        values = list(cursor.fetchone())
        positions = {name: index for index, name in enumerate(column_names)}
        values[positions["worker_id"]] = poisoned_worker_id
        values[positions["min_supported_execution_protocol_version"]] = minimum
        values[positions["max_supported_execution_protocol_version"]] = maximum
        columns = ", ".join(connection.ops.quote_name(name) for name in column_names)
        placeholders = ", ".join(["%s"] * len(values))
        cursor.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            values,
        )

    poisoned_lease = TaskWorkerLease.objects.get(pk=poisoned_worker_id)
    target = RayTarget.objects.get(pk=expectation.target_key)
    runtime = expectation.runtime
    with pytest.raises(IntegrityError), transaction.atomic():
        RayWorkerTargetCapability.objects.create(
            lease=poisoned_lease,
            lease_hostname=poisoned_lease.hostname,
            lease_pid=poisoned_lease.pid,
            lease_started_at=poisoned_lease.started_at,
            target=target,
            target_policy=policy,
            attestation=attestation_row,
            runner_family=expectation.runner_family.value,
            manager_ray_major=runtime.ray_major,
            manager_ray_minor=runtime.ray_minor,
            manager_ray_patch=runtime.ray_patch,
            manager_python_implementation=runtime.python_implementation,
            manager_python_major=runtime.python_major,
            manager_python_minor=runtime.python_minor,
            manager_python_patch=runtime.python_patch,
            revision=1,
            created_at=ADVERTISED_AT,
            advertised_at=ADVERTISED_AT,
        )

    poisoned_identity = WorkerLeaseIdentity(
        worker_id=poisoned_worker_id,
        hostname=str(lease.hostname),
        pid=int(lease.pid),
        started_at=lease.started_at,
    )
    with pytest.raises(RayWorkerTargetCapabilityLeaseError) as error:
        _advertise(poisoned_identity, expectation)
    assert "private" not in str(error.value)
    assert not RayWorkerTargetCapability.objects.exists()


def test_identity_validation_rejects_nul_and_hostile_timezone_without_leaking() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    for invalid in (
        replace(identity, worker_id="worker\x00private"),
        replace(identity, hostname="host\x00private"),
    ):
        with pytest.raises(InvalidRayWorkerTargetCapabilityArgumentError) as error:
            _advertise(invalid, expectation)
        assert "private" not in str(error.value)

    poison = "private-timezone-path"

    class PoisonTimezone(tzinfo):
        def utcoffset(self, _value: datetime | None) -> timedelta:
            raise RuntimeError(poison)

        def dst(self, _value: datetime | None) -> timedelta:
            return timedelta(0)

    hostile = replace(
        identity,
        started_at=datetime(2026, 8, 15, 20, 59, tzinfo=PoisonTimezone()),
    )
    with pytest.raises(InvalidRayWorkerTargetCapabilityArgumentError) as error:
        _advertise(hostile, expectation)
    assert poison not in str(error.value)

    class MissingOffsetTimezone(tzinfo):
        def utcoffset(self, _value: datetime | None) -> None:
            return None

        def dst(self, _value: datetime | None) -> timedelta:
            return timedelta(0)

    missing_offset = replace(
        identity,
        started_at=datetime(2026, 8, 15, 20, 59, tzinfo=MissingOffsetTimezone()),
    )
    with pytest.raises(InvalidRayWorkerTargetCapabilityArgumentError):
        _advertise(missing_offset, expectation)


def test_identity_normalizes_an_equivalent_nonzero_offset_before_database_use() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    offset_identity = replace(
        identity,
        started_at=identity.started_at.astimezone(timezone(timedelta(hours=5, minutes=30))),
    )

    change = _advertise(offset_identity, expectation)

    assert change.changed is True
    assert RayWorkerTargetCapability.objects.get().lease_started_at == identity.started_at


def test_identity_normalization_does_not_reuse_a_stateful_timezone() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()

    class StatefulTimezone(tzinfo):
        def __init__(self) -> None:
            self.calls = 0

        def utcoffset(self, _value: datetime | None) -> timedelta:
            self.calls += 1
            if self.calls > 2:
                raise RuntimeError("private-stateful-timezone")
            return timedelta(hours=1)

        def dst(self, _value: datetime | None) -> timedelta:
            return timedelta(0)

    stateful = StatefulTimezone()
    equivalent = identity.started_at + timedelta(hours=1)
    stateful_identity = replace(
        identity,
        started_at=datetime(
            equivalent.year,
            equivalent.month,
            equivalent.day,
            equivalent.hour,
            equivalent.minute,
            equivalent.second,
            equivalent.microsecond,
            tzinfo=stateful,
        ),
    )

    change = _advertise(stateful_identity, expectation)

    assert change.changed is True
    assert stateful.calls == 2


def test_now_validation_maps_hostile_timezone_without_leaking() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    poison = "private-now-timezone"

    class PoisonTimezone(tzinfo):
        def utcoffset(self, _value: datetime | None) -> timedelta:
            raise RuntimeError(poison)

        def dst(self, _value: datetime | None) -> timedelta:
            return timedelta(0)

    hostile_now = datetime(2026, 8, 15, 21, 0, tzinfo=PoisonTimezone())
    with pytest.raises(InvalidRayWorkerTargetCapabilityArgumentError) as error:
        _advertise(identity, expectation, now=hostile_now)
    assert poison not in str(error.value)


def test_runtime_mismatch_and_ray_job_are_fixed_refusals() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()

    with pytest.raises(RayWorkerTargetCapabilityRuntimeMismatchError):
        advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            _runtime(python_patch=13),
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        )
    with pytest.raises(RayJobWorkerTargetCapabilityUnsupportedError):
        advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_JOB,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        )


@pytest.mark.parametrize(
    "operation",
    [
        lambda identity, expectation: advertise_ray_worker_target_capability(
            identity,
            "Invalid Target",
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        ),
        lambda identity, expectation: advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=True,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        ),
        lambda identity, expectation: advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT.replace(tzinfo=None),
        ),
        lambda identity, expectation: advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT.astimezone(timezone(timedelta(hours=1))),
        ),
        lambda _identity, expectation: advertise_ray_worker_target_capability(
            object(),  # type: ignore[arg-type]
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        ),
        lambda identity, expectation: advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family="ray_core",  # type: ignore[arg-type]
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        ),
        lambda identity, expectation: advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            object(),  # type: ignore[arg-type]
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        ),
        lambda identity, expectation: advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            _runtime(python_implementation="Invalid"),
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        ),
    ],
)
def test_public_arguments_are_exact_and_canonical(operation) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()

    with pytest.raises(InvalidRayWorkerTargetCapabilityArgumentError):
        operation(identity, expectation)


def test_unsupported_database_vendor_is_a_fixed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    monkeypatch.setattr(connections["default"], "vendor", "mysql")

    with pytest.raises(UnsupportedRayWorkerTargetCapabilityDatabaseError):
        _advertise(identity, expectation)


def test_missing_target_policy_and_attestation_fail_closed() -> None:
    _lease_row, identity = _lease()
    expectation = _expectation()
    with pytest.raises(RayWorkerTargetCapabilityTargetStateError):
        _advertise(identity, expectation)

    registration = register_ray_target(expectation)
    with pytest.raises(RayWorkerTargetCapabilityAttestationStateError):
        _advertise(identity, registration.expectation)

    target = RayTarget.objects.get(pk=expectation.target_key)
    retained = RayTargetPolicyRevision.objects.get(target=target, revision=1)
    retained.delete()
    corrupt_expectation = replace(expectation, policy_revision=2)
    RayTargetPolicyRevision.objects.create(
        target=target,
        revision=2,
        desired_state=RayTargetDesiredState.DRAINING,
        expectation_schema_version=1,
        expectation_json=encode_ray_target_expectation(corrupt_expectation),
        expectation_digest=ray_target_expectation_digest(corrupt_expectation),
    )
    with pytest.raises(RayWorkerTargetCapabilityTargetStateError):
        advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=2,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        )


def test_retired_policy_is_not_an_advertisable_target() -> None:
    expectation, _policy, _attestation_row = _target()
    target = RayTarget.objects.get(pk=expectation.target_key)
    retired = replace(expectation, policy_revision=2)
    RayTargetPolicyRevision.objects.create(
        target=target,
        revision=2,
        desired_state=RayTargetDesiredState.RETIRED,
        expectation_schema_version=1,
        expectation_json=encode_ray_target_expectation(retired),
        expectation_digest=ray_target_expectation_digest(retired),
    )
    _lease_row, identity = _lease()

    with pytest.raises(RayWorkerTargetCapabilityTargetStateError):
        advertise_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_CORE,
            expected_policy_revision=2,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=ADVERTISED_AT,
        )


def test_retained_ray_job_policy_is_explicitly_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    retained_latest = capabilities._latest_policy

    def ray_job_latest(target: RayTarget, *, using: str):
        policy, retained, state = retained_latest(target, using=using)
        return policy, replace(retained, runner_family=RayRunnerFamily.RAY_JOB), state

    monkeypatch.setattr(capabilities, "_latest_policy", ray_job_latest)
    with pytest.raises(RayJobWorkerTargetCapabilityUnsupportedError):
        _advertise(identity, expectation)


def test_gapped_and_denormalized_attestation_history_fails_closed() -> None:
    expectation, policy, first = _target()
    proof = _attestation(
        expectation,
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=50),
    )
    record_ray_target_attestation(
        expectation.target_key,
        proof,
        expected_policy_revision=expectation.policy_revision,
        expected_attestation_revision=1,
        now=NOW + timedelta(seconds=1),
    )
    first.delete()
    _lease_row, identity = _lease()

    with pytest.raises(RayWorkerTargetCapabilityAttestationStateError):
        _advertise(identity, expectation, expected_attestation_revision=2)

    RayTargetAttestationRevision.objects.filter(policy=policy).delete()
    canonical = _attestation(expectation)
    RayTargetAttestationRevision.objects.create(
        policy=policy,
        revision=1,
        attestation_schema_version=1,
        attestation_json=encode_ray_cluster_attestation(canonical),
        expectation_digest=canonical.expectation_digest,
        membership_digest="sha256:" + "f" * 64,
        attestation_digest=canonical.attestation_digest,
        observed_at=canonical.observed_at,
        expires_at=canonical.expires_at,
        recorded_at=NOW,
    )
    with pytest.raises(RayWorkerTargetCapabilityAttestationStateError):
        _advertise(identity, expectation)


def test_expired_latest_attestation_fails_closed_with_fixed_classification() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease(heartbeat_at=NOW + timedelta(seconds=49))

    with pytest.raises(RayWorkerTargetCapabilityAttestationStateError) as error:
        _advertise(identity, expectation, now=NOW + timedelta(seconds=50))

    assert error.value.classification is RayTargetAttestationRejection.EXPIRED
    assert str(error.value) == (
        "Ray worker target capability attestation is unavailable or invalid"
    )


def test_ray_core_lease_can_advertise_only_one_target() -> None:
    primary, _policy, _attestation_row = _target()
    secondary, _policy2, _attestation2 = _target(
        "target.secondary",
        session_suffix="2",
    )
    _lease_row, identity = _lease()
    _advertise(identity, primary)

    with pytest.raises(RayWorkerTargetCapabilityLimitError):
        _advertise(identity, secondary)

    assert list(RayWorkerTargetCapability.objects.values_list("target_id", flat=True)) == [
        primary.target_key
    ]


def test_absent_capability_requires_expected_revision_zero() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()

    with pytest.raises(RayWorkerTargetCapabilityRevisionConflictError) as error:
        _advertise(identity, expectation, expected_capability_revision=1)
    assert (error.value.expected_revision, error.value.actual_revision) == (1, 0)


def test_renewal_rejects_time_regression_and_revision_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    _advertise(identity, expectation)

    with pytest.raises(RayWorkerTargetCapabilityAdvertisementRegressionError):
        _advertise(
            identity,
            expectation,
            expected_capability_revision=1,
            now=ADVERTISED_AT - timedelta(seconds=1),
        )

    monkeypatch.setattr(capabilities, "_MAX_REVISION", 1)
    with pytest.raises(RayWorkerTargetCapabilityRevisionExhaustedError):
        _advertise(
            identity,
            expectation,
            expected_capability_revision=1,
            now=ADVERTISED_AT + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "field,value",
    [("manager_python_implementation", "Invalid"), ("lease_hostname", "other-host")],
)
def test_corrupt_current_capability_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    expectation, _policy, _attestation_row = _target()
    lease, identity = _lease()
    _advertise(identity, expectation)
    current = RayWorkerTargetCapability.objects.get()
    setattr(current, field, value)
    monkeypatch.setattr(
        capabilities,
        "_locked_current_capability",
        lambda *_args, **_kwargs: current,
    )

    with pytest.raises(RayWorkerTargetCapabilityStateError):
        _advertise(
            identity,
            expectation,
            expected_capability_revision=1,
            now=ADVERTISED_AT + timedelta(seconds=1),
        )
    lease.refresh_from_db()
    assert lease.last_heartbeat_at == NOW + timedelta(seconds=4)


def test_exact_withdraw_is_cas_idempotent_and_allows_inactive_stale_lease() -> None:
    expectation, _policy, _attestation_row = _target()
    lease, identity = _lease()
    _advertise(identity, expectation)
    lease.is_active = False
    lease.stopped_at = NOW + timedelta(seconds=6)
    lease.last_heartbeat_at = NOW - timedelta(hours=1)
    lease.save(update_fields=["is_active", "stopped_at", "last_heartbeat_at"])

    with pytest.raises(RayWorkerTargetCapabilityRevisionConflictError):
        withdraw_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expected_capability_revision=2,
        )
    assert withdraw_ray_worker_target_capability(
        identity,
        expectation.target_key,
        expected_capability_revision=1,
    )
    assert not withdraw_ray_worker_target_capability(
        identity,
        expectation.target_key,
        expected_capability_revision=1,
    )


def test_withdraw_all_is_idempotent_and_exact_to_the_lease_incarnation() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    _advertise(identity, expectation)

    assert withdraw_all_ray_worker_target_capabilities(identity) == 1
    assert withdraw_all_ray_worker_target_capabilities(identity) == 0
    assert (
        withdraw_all_ray_worker_target_capabilities(
            replace(identity, started_at=identity.started_at + timedelta(seconds=1))
        )
        == 0
    )


def test_exact_withdraw_absence_is_fail_closed_and_idempotent() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()

    assert not withdraw_ray_worker_target_capability(
        replace(identity, hostname="other-host"),
        expectation.target_key,
        expected_capability_revision=1,
    )
    assert not withdraw_ray_worker_target_capability(
        identity,
        "target.missing",
        expected_capability_revision=1,
    )


@pytest.mark.parametrize("withdraw_all", [False, True])
def test_withdraw_database_failures_are_fixed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    withdraw_all: bool,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    _advertise(identity, expectation)
    poison = "private-withdraw-database-secret"

    def fail_delete(_self):
        raise OperationalError(poison)

    monkeypatch.setattr(QuerySet, "delete", fail_delete)

    def operation():
        if withdraw_all:
            return withdraw_all_ray_worker_target_capabilities(identity)
        return withdraw_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expected_capability_revision=1,
        )

    with pytest.raises(RayWorkerTargetCapabilityPersistenceRaceError) as error:
        operation()
    assert poison not in str(error.value)


def test_withdraw_delete_count_and_disappearing_capability_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    _advertise(identity, expectation)

    monkeypatch.setattr(QuerySet, "delete", lambda _self: (0, {}))
    with pytest.raises(RayWorkerTargetCapabilityStateError):
        withdraw_ray_worker_target_capability(
            identity,
            expectation.target_key,
            expected_capability_revision=1,
        )
    with pytest.raises(RayWorkerTargetCapabilityStateError):
        withdraw_all_ray_worker_target_capabilities(identity)

    monkeypatch.setattr(
        capabilities,
        "_locked_current_capability",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(RayWorkerTargetCapabilityStateError):
        withdraw_all_ray_worker_target_capabilities(identity)


def test_nested_transactions_and_invalid_database_aliases_are_fixed_refusals() -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()

    with transaction.atomic():
        with pytest.raises(NestedRayWorkerTargetCapabilityTransactionError):
            _advertise(identity, expectation)
        with pytest.raises(NestedRayWorkerTargetCapabilityTransactionError):
            withdraw_all_ray_worker_target_capabilities(identity)

    for using in ("", "private-missing-database"):
        with pytest.raises(UnsupportedRayWorkerTargetCapabilityDatabaseError) as error:
            advertise_ray_worker_target_capability(
                identity,
                expectation.target_key,
                expectation.runtime,
                manager_runner_family=RayRunnerFamily.RAY_CORE,
                expected_policy_revision=1,
                expected_attestation_revision=1,
                expected_capability_revision=0,
                now=ADVERTISED_AT,
                using=using,
            )
        if using:
            assert using not in str(error.value)


@pytest.mark.skipif(connection.vendor != "sqlite", reason="requires SQLite query semantics")
def test_sqlite_lock_order_is_exact_lease_then_target_then_capability() -> None:
    expectation, _policy, _attestation_row = _target()
    lease, identity = _lease()
    _advertise(identity, expectation)

    with CaptureQueriesContext(connection) as queries:
        _advertise(
            identity,
            expectation,
            expected_capability_revision=1,
            now=ADVERTISED_AT + timedelta(seconds=1),
        )

    statements = [query["sql"] for query in queries.captured_queries]
    begin = next(index for index, statement in enumerate(statements) if statement == "BEGIN")
    lease_lock = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith('UPDATE "django_ray_taskworkerlease"')
    )
    target_lock = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith('UPDATE "django_ray_raytarget"')
    )
    capability_locks = [
        index
        for index, statement in enumerate(statements)
        if statement.startswith('UPDATE "django_ray_rayworkertargetcapability"')
    ]
    assert lease_lock == begin + 1
    assert lease_lock < target_lock < capability_locks[0] < capability_locks[1]
    assert '"worker_id" = "django_ray_taskworkerlease"."worker_id"' in statements[lease_lock]
    lease.refresh_from_db()
    assert lease.last_heartbeat_at == NOW + timedelta(seconds=4)


@pytest.mark.skipif(connection.vendor != "sqlite", reason="requires SQLite lock helpers")
@pytest.mark.parametrize(
    ("locked_model", "anomaly"),
    [
        (TaskWorkerLease, "multiple"),
        (TaskWorkerLease, "disappeared"),
        (RayWorkerTargetCapability, "multiple"),
        (RayWorkerTargetCapability, "disappeared"),
    ],
)
def test_sqlite_lock_anomalies_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    locked_model: type[Any],
    anomaly: str,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    expected_revision = 0
    if locked_model is RayWorkerTargetCapability:
        _advertise(identity, expectation)
        expected_revision = 1
    original_update = QuerySet.update
    original_get = QuerySet.get

    def anomalous_update(self, **kwargs: Any):
        if self.model is locked_model and anomaly == "multiple":
            return 2
        return original_update(self, **kwargs)

    def anomalous_get(self, *args: Any, **kwargs: Any):
        if self.model is locked_model and anomaly == "disappeared":
            raise locked_model.DoesNotExist
        return original_get(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "update", anomalous_update)
    monkeypatch.setattr(QuerySet, "get", anomalous_get)
    with pytest.raises(RayWorkerTargetCapabilityStateError):
        _advertise(
            identity,
            expectation,
            expected_capability_revision=expected_revision,
            now=ADVERTISED_AT + timedelta(seconds=1),
        )


def test_material_capability_update_anomaly_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    _advertise(identity, expectation)
    original_update = QuerySet.update

    def lose_material_update(self, **kwargs: Any):
        if self.model is RayWorkerTargetCapability and "advertised_at" in kwargs:
            return 0
        return original_update(self, **kwargs)

    monkeypatch.setattr(QuerySet, "update", lose_material_update)
    with pytest.raises(RayWorkerTargetCapabilityStateError):
        _advertise(
            identity,
            expectation,
            expected_capability_revision=1,
            now=ADVERTISED_AT + timedelta(seconds=1),
        )


def test_postgresql_code_path_requests_row_locks_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    _advertise(identity, expectation)
    locked_models: list[type[Any]] = []
    original = QuerySet.select_for_update

    def track(self, *args: Any, **kwargs: Any):
        locked_models.append(self.model)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(capabilities, "_database_vendor", lambda *, using: "postgresql")
    monkeypatch.setattr(QuerySet, "select_for_update", track)

    _advertise(
        identity,
        expectation,
        expected_capability_revision=1,
        now=ADVERTISED_AT + timedelta(seconds=1),
    )

    assert locked_models[:3] == [TaskWorkerLease, RayTarget, RayWorkerTargetCapability]


@pytest.mark.postgresql
def test_postgresql_absent_advertisement_exact_replay_serializes() -> None:
    _require_postgresql()
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()

    results = _run_concurrently(
        lambda: _advertise(identity, expectation),
        lambda: _advertise(identity, expectation),
    )

    assert all(isinstance(result, RayWorkerTargetCapabilityChange) for result in results)
    assert sorted(result.changed for result in results) == [False, True]
    assert RayWorkerTargetCapability.objects.get().revision == 1


@pytest.mark.postgresql
def test_postgresql_lease_lock_serializes_competing_capability_renewals() -> None:
    _require_postgresql()
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    _advertise(identity, expectation)

    results = _run_concurrently(
        lambda: _advertise(
            identity,
            expectation,
            expected_capability_revision=1,
            now=ADVERTISED_AT + timedelta(seconds=1),
        ),
        lambda: _advertise(
            identity,
            expectation,
            expected_capability_revision=1,
            now=ADVERTISED_AT + timedelta(seconds=2),
        ),
    )

    assert sum(isinstance(result, RayWorkerTargetCapabilityChange) for result in results) == 1
    conflicts = [
        result
        for result in results
        if isinstance(result, RayWorkerTargetCapabilityRevisionConflictError)
    ]
    assert len(conflicts) == 1
    assert (conflicts[0].expected_revision, conflicts[0].actual_revision) == (1, 2)
    assert RayWorkerTargetCapability.objects.get().revision == 2


def test_database_failures_are_mapped_without_private_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation, _policy, _attestation_row = _target()
    _lease_row, identity = _lease()
    poison = "private-database-capability-secret"
    original = QuerySet.create

    def fail_create(self, **kwargs: Any):
        if self.model is RayWorkerTargetCapability:
            raise IntegrityError(poison)
        return original(self, **kwargs)

    monkeypatch.setattr(QuerySet, "create", fail_create)
    with pytest.raises(RayWorkerTargetCapabilityPersistenceRaceError) as error:
        _advertise(identity, expectation)
    assert poison not in str(error.value)
    assert not RayWorkerTargetCapability.objects.exists()

    def fail_autocommit() -> bool:
        raise OperationalError(poison)

    monkeypatch.setattr(QuerySet, "create", original)
    monkeypatch.setattr(connections["default"], "get_autocommit", fail_autocommit)
    with pytest.raises(RayWorkerTargetCapabilityPersistenceRaceError) as error:
        _advertise(identity, expectation)
    assert poison not in str(error.value)


def test_private_surface_has_no_probe_claim_or_task_selection_writer() -> None:
    assert set(signature(advertise_ray_worker_target_capability).parameters) == {
        "identity",
        "target_key",
        "actual_runtime",
        "manager_runner_family",
        "expected_policy_revision",
        "expected_attestation_revision",
        "expected_capability_revision",
        "now",
        "using",
    }
    assert set(signature(withdraw_ray_worker_target_capability).parameters) == {
        "identity",
        "target_key",
        "expected_capability_revision",
        "using",
    }
    assert set(signature(withdraw_all_ray_worker_target_capabilities).parameters) == {
        "identity",
        "using",
    }
    assert "ray" not in vars(capabilities)
    assert "RayTaskExecution" not in vars(capabilities)
    assert "probe_ray_target" not in vars(capabilities)
