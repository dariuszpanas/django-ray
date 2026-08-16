"""SQLite and PostgreSQL contracts for dormant Ray target coordination."""

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
from django.db.models import F
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext

import django_ray.target.coordination as coordination
from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
)
from django_ray.target.attestation import (
    RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
    RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetAttestationRejection,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    decode_ray_cluster_attestation,
    decode_ray_target_expectation,
    encode_ray_cluster_attestation,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)
from django_ray.target.coordination import (
    InvalidRayTargetArgumentError,
    NestedRayTargetTransactionError,
    RayJobTargetPersistenceUnsupportedError,
    RayTargetAttestationRecord,
    RayTargetAttestationRegressionError,
    RayTargetAttestationRejectedError,
    RayTargetAttestationRevisionConflictError,
    RayTargetAttestationRevisionExhaustedError,
    RayTargetNotFoundError,
    RayTargetPersistenceRaceError,
    RayTargetPolicyChange,
    RayTargetPolicyRevisionConflictError,
    RayTargetPolicyRevisionExhaustedError,
    RayTargetPolicyStateError,
    RayTargetRegistrationConflictError,
    RayTargetRetirementReservedError,
    UnsupportedRayTargetDatabaseError,
    record_ray_target_attestation,
    register_ray_target,
    transition_ray_target_desired_state,
)

pytestmark = pytest.mark.django_db(transaction=True)

NOW = datetime(2026, 8, 15, 20, 0, 0, 123456, tzinfo=UTC)
NODE_ID = "a" * 56


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
    *,
    target_key: str = "primary.ray",
    policy_revision: int = 1,
    runner_family: RayRunnerFamily = RayRunnerFamily.RAY_CORE,
    cluster_session: str = "session_2026-08-15_12-00-00_123456_1",
    runtime: RayRuntimeVersion | None = None,
) -> RayTargetExpectation:
    return RayTargetExpectation(
        target_key=target_key,
        runner_family=runner_family,
        cluster_session=cluster_session,
        policy_revision=policy_revision,
        runtime=runtime or _runtime(),
    )


def _attestation(
    expectation: RayTargetExpectation,
    *,
    observed_at: datetime = NOW - timedelta(seconds=1),
    expires_at: datetime = NOW + timedelta(seconds=59),
):
    boundary = build_ray_observation_boundary(
        resource_state_version_before=10,
        resource_state_version_after=12,
        node_state_versions_before=(RayNodeStateVersion(node_id=NODE_ID, node_state_version=20),),
        node_state_versions_after=(RayNodeStateVersion(node_id=NODE_ID, node_state_version=21),),
    )
    nodes = (
        build_ray_node_observation(
            node_id=NODE_ID,
            cluster_session=expectation.cluster_session,
            runtime=expectation.runtime,
        ),
    )
    return build_ray_cluster_attestation(
        expectation=expectation,
        boundary=boundary,
        nodes=nodes,
        observed_at=observed_at,
        expires_at=expires_at,
    )


def _register() -> RayTargetPolicyChange:
    return register_ray_target(_expectation())


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
            except Exception as error:  # return exact typed race outcome to the parent thread
                return error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        return [future.result(timeout=20) for future in futures]


def test_registration_persists_exact_identity_and_initial_draining_revision() -> None:
    expectation = _expectation()

    result = register_ray_target(expectation)

    assert result == RayTargetPolicyChange(
        target_key=expectation.target_key,
        desired_state=RayTargetDesiredState.DRAINING,
        changed=True,
        previous_revision=0,
        revision=1,
        expectation=expectation,
    )
    target = RayTarget.objects.get(pk=expectation.target_key)
    assert target.runner_family == RayRunnerFamily.RAY_CORE.value
    assert target.cluster_session == expectation.cluster_session
    assert (
        target.ray_major,
        target.ray_minor,
        target.ray_patch,
        target.python_implementation,
        target.python_major,
        target.python_minor,
        target.python_patch,
    ) == (2, 56, 0, "cpython", 3, 12, 12)

    policy = RayTargetPolicyRevision.objects.get(target=target, revision=1)
    assert policy.desired_state == RayTargetDesiredState.DRAINING
    assert policy.expectation_schema_version == RAY_TARGET_EXPECTATION_SCHEMA_VERSION
    assert policy.expectation_json == encode_ray_target_expectation(expectation)
    assert decode_ray_target_expectation(policy.expectation_json) == expectation
    assert policy.expectation_digest == ray_target_expectation_digest(expectation)


def test_exact_duplicate_registration_is_idempotent_but_drift_conflicts() -> None:
    expectation = _expectation()
    register_ray_target(expectation)

    duplicate = register_ray_target(expectation)

    assert duplicate.changed is False
    assert duplicate.previous_revision == duplicate.revision == 1
    assert RayTarget.objects.count() == 1
    assert RayTargetPolicyRevision.objects.count() == 1

    with pytest.raises(RayTargetRegistrationConflictError) as error:
        register_ray_target(replace(expectation, runtime=_runtime(python_patch=13)))
    assert str(error.value) == "Ray target registration conflicts with durable state"
    assert expectation.target_key not in str(error.value)
    assert RayTargetPolicyRevision.objects.count() == 1


@pytest.mark.parametrize(
    "expectation,error_type",
    [
        (_expectation(policy_revision=2), InvalidRayTargetArgumentError),
        (
            _expectation(runner_family=RayRunnerFamily.RAY_JOB),
            RayJobTargetPersistenceUnsupportedError,
        ),
        (_expectation(target_key="UPPERCASE"), InvalidRayTargetArgumentError),
    ],
)
def test_registration_fails_closed_before_persisting_unsupported_intent(
    expectation: RayTargetExpectation,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        register_ray_target(expectation)

    assert not RayTarget.objects.exists()
    assert not RayTargetPolicyRevision.objects.exists()


def test_active_draining_transitions_are_cas_append_only_and_idempotent() -> None:
    _register()

    activated = transition_ray_target_desired_state(
        "primary.ray",
        RayTargetDesiredState.ACTIVE,
        expected_policy_revision=1,
    )
    unchanged = transition_ray_target_desired_state(
        "primary.ray",
        "active",
        expected_policy_revision=2,
    )
    drained = transition_ray_target_desired_state(
        "primary.ray",
        RayTargetDesiredState.DRAINING,
        expected_policy_revision=2,
    )

    assert (activated.changed, activated.previous_revision, activated.revision) == (True, 1, 2)
    assert activated.expectation.policy_revision == 2
    assert (unchanged.changed, unchanged.previous_revision, unchanged.revision) == (False, 2, 2)
    assert (drained.changed, drained.previous_revision, drained.revision) == (True, 2, 3)
    assert list(
        RayTargetPolicyRevision.objects.order_by("revision").values_list(
            "revision", "desired_state"
        )
    ) == [
        (1, RayTargetDesiredState.DRAINING),
        (2, RayTargetDesiredState.ACTIVE),
        (3, RayTargetDesiredState.DRAINING),
    ]
    assert [
        decode_ray_target_expectation(payload).policy_revision
        for payload in RayTargetPolicyRevision.objects.order_by("revision").values_list(
            "expectation_json", flat=True
        )
    ] == [1, 2, 3]


def test_revision_counters_fail_closed_at_the_signed_bigint_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _register()
    monkeypatch.setattr(coordination, "_MAX_REVISION", 1)

    with pytest.raises(RayTargetPolicyRevisionExhaustedError):
        transition_ray_target_desired_state(
            "primary.ray",
            "active",
            expected_policy_revision=1,
        )

    attestation = _attestation(registration.expectation)
    record_ray_target_attestation(
        "primary.ray",
        attestation,
        expected_policy_revision=1,
        expected_attestation_revision=0,
        now=NOW,
    )
    with pytest.raises(RayTargetAttestationRevisionExhaustedError):
        record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            now=NOW,
        )

    assert RayTargetPolicyRevision.objects.count() == 1
    assert RayTargetAttestationRevision.objects.count() == 1


def test_stale_policy_transition_and_retirement_are_fixed_redacted_refusals() -> None:
    _register()
    transition_ray_target_desired_state(
        "primary.ray",
        "active",
        expected_policy_revision=1,
    )

    with pytest.raises(RayTargetPolicyRevisionConflictError) as stale:
        transition_ray_target_desired_state(
            "primary.ray",
            "draining",
            expected_policy_revision=1,
        )
    assert (stale.value.expected_revision, stale.value.actual_revision) == (1, 2)
    assert str(stale.value) == "Ray target policy revision changed"

    with pytest.raises(RayTargetRetirementReservedError) as retired:
        transition_ray_target_desired_state(
            "primary.ray",
            "retired",
            expected_policy_revision=2,
        )
    assert str(retired.value) == (
        "Ray target retirement is not available in this coordination service"
    )
    assert "primary.ray" not in str(stale.value) + str(retired.value)
    assert RayTargetPolicyRevision.objects.count() == 2


def test_arbitrary_string_subclasses_cannot_smuggle_a_desired_state() -> None:
    class StringSubclass(str):
        pass

    _register()

    with pytest.raises(InvalidRayTargetArgumentError):
        transition_ray_target_desired_state(
            "primary.ray",
            StringSubclass("active"),
            expected_policy_revision=1,
        )

    assert RayTargetPolicyRevision.objects.count() == 1


def test_raw_retired_policy_blocks_every_coordination_mutation() -> None:
    registration = _register()
    retired_expectation = replace(registration.expectation, policy_revision=2)
    target = RayTarget.objects.get(pk="primary.ray")
    RayTargetPolicyRevision.objects.create(
        target=target,
        revision=2,
        desired_state=RayTargetDesiredState.RETIRED,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json=encode_ray_target_expectation(retired_expectation),
        expectation_digest=ray_target_expectation_digest(retired_expectation),
    )

    with pytest.raises(RayTargetRetirementReservedError):
        transition_ray_target_desired_state(
            "primary.ray",
            "active",
            expected_policy_revision=2,
        )
    with pytest.raises(RayTargetRetirementReservedError):
        record_ray_target_attestation(
            "primary.ray",
            _attestation(retired_expectation),
            expected_policy_revision=2,
            expected_attestation_revision=0,
            now=NOW,
        )

    assert RayTargetPolicyRevision.objects.count() == 2
    assert not RayTargetAttestationRevision.objects.exists()


def test_attestation_append_persists_canonical_proof_and_per_policy_cas() -> None:
    registration = _register()
    attestation = _attestation(registration.expectation)

    first = record_ray_target_attestation(
        "primary.ray",
        attestation,
        expected_policy_revision=1,
        expected_attestation_revision=0,
        now=NOW,
    )
    second = record_ray_target_attestation(
        "primary.ray",
        attestation,
        expected_policy_revision=1,
        expected_attestation_revision=1,
        now=NOW,
    )

    assert first == RayTargetAttestationRecord(
        target_key="primary.ray",
        policy_revision=1,
        previous_revision=0,
        revision=1,
        attestation=attestation,
        recorded_at=NOW,
    )
    assert (second.previous_revision, second.revision) == (1, 2)
    rows = list(RayTargetAttestationRevision.objects.order_by("revision"))
    assert [row.revision for row in rows] == [1, 2]
    for row in rows:
        assert row.attestation_schema_version == RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION
        assert row.attestation_json == encode_ray_cluster_attestation(attestation)
        assert decode_ray_cluster_attestation(row.attestation_json) == attestation
        assert row.expectation_digest == attestation.expectation_digest
        assert row.membership_digest == attestation.membership_digest
        assert row.attestation_digest == attestation.attestation_digest
        assert row.observed_at == attestation.observed_at
        assert row.expires_at == attestation.expires_at
        assert row.recorded_at == NOW


def test_attestation_observation_and_recording_times_cannot_regress() -> None:
    registration = _register()
    retained = _attestation(
        registration.expectation,
        observed_at=NOW - timedelta(seconds=10),
        expires_at=NOW + timedelta(seconds=60),
    )
    record_ray_target_attestation(
        "primary.ray",
        retained,
        expected_policy_revision=1,
        expected_attestation_revision=0,
        now=NOW,
    )

    older_observation = _attestation(
        registration.expectation,
        observed_at=NOW - timedelta(seconds=11),
        expires_at=NOW + timedelta(seconds=60),
    )
    with pytest.raises(RayTargetAttestationRegressionError) as observed_regression:
        record_ray_target_attestation(
            "primary.ray",
            older_observation,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            now=NOW,
        )

    newer_observation = _attestation(
        registration.expectation,
        observed_at=NOW - timedelta(seconds=9),
        expires_at=NOW + timedelta(seconds=60),
    )
    with pytest.raises(RayTargetAttestationRegressionError) as recorded_regression:
        record_ray_target_attestation(
            "primary.ray",
            newer_observation,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            now=NOW - timedelta(seconds=1),
        )

    assert str(observed_regression.value) == "Ray target attestation time regressed"
    assert str(recorded_regression.value) == "Ray target attestation time regressed"
    assert RayTargetAttestationRevision.objects.count() == 1


def test_attestation_rejects_stale_cas_mismatch_and_expiry_without_append() -> None:
    registration = _register()
    attestation = _attestation(registration.expectation)

    with pytest.raises(RayTargetPolicyRevisionConflictError):
        record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=2,
            expected_attestation_revision=0,
            now=NOW,
        )
    with pytest.raises(RayTargetAttestationRevisionConflictError) as stale:
        record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            now=NOW,
        )
    assert (stale.value.expected_revision, stale.value.actual_revision) == (1, 0)
    assert str(stale.value) == "Ray target attestation revision changed"

    expired = _attestation(
        registration.expectation,
        observed_at=NOW - timedelta(seconds=60),
        expires_at=NOW,
    )
    with pytest.raises(RayTargetAttestationRejectedError) as rejection:
        record_ray_target_attestation(
            "primary.ray",
            expired,
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=NOW,
        )
    assert rejection.value.classification is RayTargetAttestationRejection.EXPIRED
    assert str(rejection.value) == "Ray target attestation does not verify the current policy"
    assert not RayTargetAttestationRevision.objects.exists()


def test_old_policy_attestation_cannot_cross_a_desired_state_revision() -> None:
    registration = _register()
    old_attestation = _attestation(registration.expectation)
    activated = transition_ray_target_desired_state(
        "primary.ray",
        "active",
        expected_policy_revision=1,
    )

    with pytest.raises(RayTargetAttestationRejectedError) as rejection:
        record_ray_target_attestation(
            "primary.ray",
            old_attestation,
            expected_policy_revision=activated.revision,
            expected_attestation_revision=0,
            now=NOW,
        )

    assert rejection.value.classification is RayTargetAttestationRejection.POLICY_REVISION_MISMATCH
    assert not RayTargetAttestationRevision.objects.exists()


def test_target_key_and_attestation_identity_are_compared_separately() -> None:
    registration = _register()
    foreign_expectation = replace(registration.expectation, target_key="foreign.ray")

    with pytest.raises(RayTargetAttestationRejectedError) as rejection:
        record_ray_target_attestation(
            "primary.ray",
            _attestation(foreign_expectation),
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=NOW,
        )

    assert rejection.value.classification is RayTargetAttestationRejection.TARGET_KEY_MISMATCH
    assert not RayTargetAttestationRevision.objects.exists()


def test_recorded_at_accepts_validity_start_and_rejects_expiry_boundary() -> None:
    registration = _register()
    observed_at = NOW
    expires_at = NOW + timedelta(seconds=60)
    attestation = _attestation(
        registration.expectation,
        observed_at=observed_at,
        expires_at=expires_at,
    )

    first = record_ray_target_attestation(
        "primary.ray",
        attestation,
        expected_policy_revision=1,
        expected_attestation_revision=0,
        now=observed_at,
    )
    second = record_ray_target_attestation(
        "primary.ray",
        attestation,
        expected_policy_revision=1,
        expected_attestation_revision=1,
        now=expires_at - timedelta(microseconds=1),
    )
    with pytest.raises(RayTargetAttestationRejectedError) as rejection:
        record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=2,
            now=expires_at,
        )

    assert first.recorded_at == observed_at
    assert second.recorded_at == expires_at - timedelta(microseconds=1)
    assert rejection.value.classification is RayTargetAttestationRejection.EXPIRED
    assert RayTargetAttestationRevision.objects.count() == 2


def test_now_requires_an_exact_datetime_with_utc_offset() -> None:
    registration = _register()
    attestation = _attestation(registration.expectation)
    invalid_values: tuple[object, ...] = (
        NOW.replace(tzinfo=None),
        NOW.astimezone(timezone(timedelta(hours=1))),
        NOW.isoformat(),
    )

    for value in invalid_values:
        with pytest.raises(InvalidRayTargetArgumentError):
            record_ray_target_attestation(
                "primary.ray",
                attestation,
                expected_policy_revision=1,
                expected_attestation_revision=0,
                now=value,  # type: ignore[arg-type]
            )

    named_zero_offset = NOW.astimezone(timezone(timedelta(0), "named-zero"))
    recorded = record_ray_target_attestation(
        "primary.ray",
        attestation,
        expected_policy_revision=1,
        expected_attestation_revision=0,
        now=named_zero_offset,
    )
    assert recorded.recorded_at == NOW
    assert recorded.recorded_at.tzinfo is UTC


@pytest.mark.parametrize("failure_call", [1, 2])
def test_hostile_timezone_errors_are_mapped_without_details(failure_call: int) -> None:
    class HostileTimezone(tzinfo):
        calls = 0

        def utcoffset(self, value: datetime | None) -> timedelta | None:
            self.calls += 1
            if self.calls == failure_call:
                raise RuntimeError("timezone-secret")
            return timedelta(0)

        def dst(self, value: datetime | None) -> timedelta | None:
            return None

    registration = _register()
    attestation = _attestation(registration.expectation)
    hostile_now = NOW.replace(tzinfo=HostileTimezone())

    with pytest.raises(InvalidRayTargetArgumentError) as error:
        record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=hostile_now,
        )

    assert str(error.value) == "Ray target coordination received an invalid argument"
    assert "timezone-secret" not in str(error.value)
    assert not RayTargetAttestationRevision.objects.exists()


def test_attestation_is_canonicalized_before_the_durable_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _register()
    attestation = _attestation(registration.expectation)
    original_encode = coordination.encode_ray_cluster_attestation
    observed_atomic_states: list[bool] = []

    def tracked_encode(value):
        observed_atomic_states.append(connection.in_atomic_block)
        return original_encode(value)

    monkeypatch.setattr(coordination, "encode_ray_cluster_attestation", tracked_encode)

    record_ray_target_attestation(
        "primary.ray",
        attestation,
        expected_policy_revision=1,
        expected_attestation_revision=0,
        now=NOW,
    )

    assert observed_atomic_states == [False]


def test_record_api_exposes_no_probe_or_ray_effect_seam() -> None:
    parameters = signature(record_ray_target_attestation).parameters

    assert set(parameters) == {
        "target_key",
        "attestation",
        "expected_policy_revision",
        "expected_attestation_revision",
        "now",
        "using",
    }
    assert "probe_ray_target" not in vars(coordination)
    assert "ray" not in vars(coordination)


def test_mutations_require_supported_database_and_the_outermost_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation = _expectation()
    original_vendor = connections["default"].vendor
    monkeypatch.setattr(connections["default"], "vendor", "mysql")
    with pytest.raises(UnsupportedRayTargetDatabaseError):
        register_ray_target(expectation)
    monkeypatch.setattr(connections["default"], "vendor", original_vendor)

    with transaction.atomic():
        with pytest.raises(NestedRayTargetTransactionError):
            register_ray_target(expectation)
    assert not RayTarget.objects.exists()

    registration = _register()
    attestation = _attestation(registration.expectation)
    with transaction.atomic():
        with pytest.raises(NestedRayTargetTransactionError):
            transition_ray_target_desired_state(
                "primary.ray",
                "active",
                expected_policy_revision=1,
            )
        with pytest.raises(NestedRayTargetTransactionError):
            record_ray_target_attestation(
                "primary.ray",
                attestation,
                expected_policy_revision=1,
                expected_attestation_revision=0,
                now=NOW,
            )
    assert RayTargetPolicyRevision.objects.count() == 1
    assert not RayTargetAttestationRevision.objects.exists()


@pytest.mark.parametrize("using", ["", "missing-target-database"])
def test_invalid_database_aliases_are_fixed_refusals(using: str) -> None:
    with pytest.raises(UnsupportedRayTargetDatabaseError) as error:
        register_ray_target(_expectation(), using=using)

    assert str(error.value) == "Ray target coordination supports only SQLite and PostgreSQL"
    if using:
        assert using not in str(error.value)
    assert not RayTarget.objects.exists()


def test_public_canonical_arguments_require_exact_valid_attestation_types() -> None:
    with pytest.raises(InvalidRayTargetArgumentError):
        register_ray_target("not-an-expectation")  # type: ignore[arg-type]

    expectation = _expectation()
    with pytest.raises(InvalidRayTargetArgumentError):
        record_ray_target_attestation(
            "primary.ray",
            object(),  # type: ignore[arg-type]
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=NOW,
        )

    invalid_attestation = replace(
        _attestation(expectation),
        membership_digest="sha256:" + "0" * 64,
    )
    with pytest.raises(InvalidRayTargetArgumentError):
        record_ray_target_attestation(
            "primary.ray",
            invalid_attestation,
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=NOW,
        )

    assert not RayTarget.objects.exists()
    assert not RayTargetAttestationRevision.objects.exists()


def test_hostile_attestation_timestamps_are_fixed_refusals() -> None:
    class HostileTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta | None:
            raise RuntimeError("attestation-timezone-secret")

        def dst(self, value: datetime | None) -> timedelta | None:
            return None

    valid = _attestation(_expectation())
    hostile_timestamp = NOW.replace(tzinfo=HostileTimezone())

    for attestation in (
        replace(valid, observed_at=hostile_timestamp),
        replace(valid, expires_at=hostile_timestamp),
    ):
        with pytest.raises(InvalidRayTargetArgumentError) as error:
            record_ray_target_attestation(
                "primary.ray",
                attestation,
                expected_policy_revision=1,
                expected_attestation_revision=0,
                now=NOW,
            )
        assert str(error.value) == "Ray target coordination received an invalid argument"
        assert "attestation-timezone-secret" not in str(error.value)

    assert not RayTarget.objects.exists()
    assert not RayTargetAttestationRevision.objects.exists()


def test_unexpected_expectation_codec_errors_are_fixed_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poison = "expectation-codec-secret"

    def fail_encode(_value: object) -> str:
        raise RuntimeError(poison)

    monkeypatch.setattr(coordination, "encode_ray_target_expectation", fail_encode)

    with pytest.raises(InvalidRayTargetArgumentError) as error:
        register_ray_target(_expectation())

    assert str(error.value) == "Ray target coordination received an invalid argument"
    assert poison not in str(error.value)
    assert not RayTarget.objects.exists()


def test_invalid_keys_revisions_and_missing_targets_fail_without_echo() -> None:
    for operation in (
        lambda: transition_ray_target_desired_state(
            "private target",
            "active",
            expected_policy_revision=1,
        ),
        lambda: transition_ray_target_desired_state(
            "private.target",
            "active",
            expected_policy_revision=True,
        ),
        lambda: transition_ray_target_desired_state(
            "private.target",
            "paused",
            expected_policy_revision=1,
        ),
        lambda: record_ray_target_attestation(
            "private.target",
            _attestation(_expectation(target_key="private.target")),
            expected_policy_revision=1,
            expected_attestation_revision=-1,
            now=NOW,
        ),
    ):
        with pytest.raises(InvalidRayTargetArgumentError) as error:
            operation()
        assert "private" not in str(error.value)

    with pytest.raises(RayTargetNotFoundError) as missing:
        transition_ray_target_desired_state(
            "private.target",
            "active",
            expected_policy_revision=1,
        )
    assert "private.target" not in str(missing.value)


def test_policy_and_attestation_revision_gaps_fail_closed_after_delete() -> None:
    _register()
    transition_ray_target_desired_state(
        "primary.ray",
        "active",
        expected_policy_revision=1,
    )
    RayTargetPolicyRevision.objects.get(target_id="primary.ray", revision=1).delete()

    with pytest.raises(RayTargetPolicyStateError):
        transition_ray_target_desired_state(
            "primary.ray",
            "draining",
            expected_policy_revision=2,
        )

    RayTargetPolicyRevision.objects.filter(target_id="primary.ray").delete()
    with pytest.raises(RayTargetPolicyStateError):
        transition_ray_target_desired_state(
            "primary.ray",
            "draining",
            expected_policy_revision=2,
        )

    secondary_expectation = _expectation(
        target_key="secondary.ray",
        cluster_session="session_2026-08-15_12-00-00_123456_2",
    )
    registration = register_ray_target(secondary_expectation)
    attestation = _attestation(registration.expectation)
    for expected_revision in (0, 1):
        record_ray_target_attestation(
            "secondary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=expected_revision,
            now=NOW,
        )
    RayTargetAttestationRevision.objects.get(
        policy__target_id="secondary.ray",
        revision=1,
    ).delete()

    with pytest.raises(RayTargetPolicyStateError):
        record_ray_target_attestation(
            "secondary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=2,
            now=NOW,
        )


def test_raw_noncanonical_and_denormalized_retained_rows_fail_closed() -> None:
    registration = _register()
    target = RayTarget.objects.get(pk="primary.ray")
    policy = RayTargetPolicyRevision.objects.get(target=target, revision=1)
    policy.delete()
    canonical_expectation = encode_ray_target_expectation(registration.expectation)
    RayTargetPolicyRevision.objects.create(
        target=target,
        revision=1,
        desired_state=RayTargetDesiredState.DRAINING,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json=canonical_expectation + " ",
        expectation_digest=ray_target_expectation_digest(registration.expectation),
    )

    with pytest.raises(RayTargetPolicyStateError):
        register_ray_target(registration.expectation)

    RayTargetPolicyRevision.objects.get(target=target, revision=1).delete()
    RayTargetPolicyRevision.objects.create(
        target=target,
        revision=1,
        desired_state=RayTargetDesiredState.DRAINING,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json=canonical_expectation,
        expectation_digest="sha256:" + "f" * 64,
    )

    with pytest.raises(RayTargetPolicyStateError):
        register_ray_target(registration.expectation)

    RayTargetPolicyRevision.objects.get(target=target, revision=1).delete()
    valid_policy = RayTargetPolicyRevision.objects.create(
        target=target,
        revision=1,
        desired_state=RayTargetDesiredState.DRAINING,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json=canonical_expectation,
        expectation_digest=ray_target_expectation_digest(registration.expectation),
    )
    attestation = _attestation(registration.expectation)
    record_ray_target_attestation(
        "primary.ray",
        attestation,
        expected_policy_revision=1,
        expected_attestation_revision=0,
        now=NOW,
    )
    RayTargetAttestationRevision.objects.get(policy=valid_policy, revision=1).delete()
    canonical_attestation = encode_ray_cluster_attestation(attestation)
    RayTargetAttestationRevision.objects.create(
        policy=valid_policy,
        revision=1,
        attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        attestation_json=canonical_attestation + " ",
        expectation_digest=attestation.expectation_digest,
        membership_digest=attestation.membership_digest,
        attestation_digest=attestation.attestation_digest,
        observed_at=attestation.observed_at,
        expires_at=attestation.expires_at,
        recorded_at=NOW,
    )

    with pytest.raises(RayTargetPolicyStateError):
        record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            now=NOW,
        )

    RayTargetAttestationRevision.objects.get(policy=valid_policy, revision=1).delete()
    RayTargetAttestationRevision.objects.create(
        policy=valid_policy,
        revision=1,
        attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        attestation_json=canonical_attestation,
        expectation_digest=attestation.expectation_digest,
        membership_digest="sha256:" + "f" * 64,
        attestation_digest=attestation.attestation_digest,
        observed_at=attestation.observed_at,
        expires_at=attestation.expires_at,
        recorded_at=NOW,
    )

    with pytest.raises(RayTargetPolicyStateError):
        record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            now=NOW,
        )


@pytest.mark.skipif(connection.vendor != "sqlite", reason="requires SQLite query semantics")
def test_sqlite_transition_takes_the_target_writer_fence_before_reading_policy() -> None:
    _register()

    with CaptureQueriesContext(connection) as queries:
        transition_ray_target_desired_state(
            "primary.ray",
            "active",
            expected_policy_revision=1,
        )

    statements = [query["sql"] for query in queries.captured_queries]
    begin_index = next(index for index, sql in enumerate(statements) if sql == "BEGIN")
    first_statement = statements[begin_index + 1]
    assert first_statement.startswith('UPDATE "django_ray_raytarget"')
    assert '"target_key" = "django_ray_raytarget"."target_key"' in first_statement


@pytest.mark.skipif(connection.vendor != "sqlite", reason="requires SQLite trigger semantics")
def test_sqlite_immutability_trigger_permits_the_exact_writer_fence_noop() -> None:
    registration = _register()

    updated = RayTarget.objects.filter(pk="primary.ray").update(target_key=F("target_key"))

    assert updated == 1
    assert RayTarget.objects.get(pk="primary.ray").target_key == registration.target_key


@pytest.mark.postgresql
def test_postgresql_target_lock_serializes_policy_revision_cas() -> None:
    _require_postgresql()
    _register()

    results = _run_concurrently(
        *(
            lambda: transition_ray_target_desired_state(
                "primary.ray",
                "active",
                expected_policy_revision=1,
            )
            for _index in range(2)
        )
    )

    assert sum(isinstance(result, RayTargetPolicyChange) for result in results) == 1
    conflicts = [
        result for result in results if isinstance(result, RayTargetPolicyRevisionConflictError)
    ]
    assert len(conflicts) == 1
    assert (conflicts[0].expected_revision, conflicts[0].actual_revision) == (1, 2)
    assert list(
        RayTargetPolicyRevision.objects.order_by("revision").values_list(
            "revision", "desired_state"
        )
    ) == [(1, RayTargetDesiredState.DRAINING), (2, RayTargetDesiredState.ACTIVE)]


@pytest.mark.postgresql
def test_postgresql_target_lock_serializes_attestation_revision_cas() -> None:
    _require_postgresql()
    registration = _register()
    attestation = _attestation(registration.expectation)

    def record() -> RayTargetAttestationRecord:
        return record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=NOW,
        )

    results = _run_concurrently(record, record)

    assert sum(isinstance(result, RayTargetAttestationRecord) for result in results) == 1
    conflicts = [
        result
        for result in results
        if isinstance(result, RayTargetAttestationRevisionConflictError)
    ]
    assert len(conflicts) == 1
    assert (conflicts[0].expected_revision, conflicts[0].actual_revision) == (0, 1)
    assert list(RayTargetAttestationRevision.objects.values_list("revision", flat=True)) == [1]


@pytest.mark.postgresql
def test_postgresql_absent_registration_has_one_fixed_race_loser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    expectation = _expectation()
    both_saw_absence = Barrier(2)
    original = coordination._locked_or_absent_target

    def coordinated_absence(*, target_key: str, using: str, vendor: str):
        target = original(target_key=target_key, using=using, vendor=vendor)
        if target is None:
            both_saw_absence.wait(timeout=10)
        return target

    monkeypatch.setattr(coordination, "_locked_or_absent_target", coordinated_absence)

    results = _run_concurrently(
        lambda: register_ray_target(expectation),
        lambda: register_ray_target(expectation),
    )

    assert sum(isinstance(result, RayTargetPolicyChange) for result in results) == 1
    losers = [result for result in results if isinstance(result, RayTargetPersistenceRaceError)]
    assert len(losers) == 1
    assert str(losers[0]) == "Ray target persistence could not serialize the mutation"
    assert RayTarget.objects.count() == 1
    assert RayTargetPolicyRevision.objects.count() == 1


def test_unexpected_database_integrity_failure_is_mapped_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation = _expectation(target_key="private.target")
    poison = "database-secret"
    original_create = QuerySet.create

    def fail_create(self, **kwargs: Any):
        if self.model is RayTarget:
            raise IntegrityError(poison)
        return original_create(self, **kwargs)

    monkeypatch.setattr(QuerySet, "create", fail_create)

    with pytest.raises(RayTargetPersistenceRaceError) as error:
        register_ray_target(expectation)
    assert str(error.value) == "Ray target persistence could not serialize the mutation"
    assert poison not in str(error.value)


def test_append_database_failures_are_mapped_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _register()
    attestation = _attestation(registration.expectation)
    poison = "append-database-secret"
    original_create = QuerySet.create

    def fail_append(self, **kwargs: Any):
        if self.model in {RayTargetPolicyRevision, RayTargetAttestationRevision}:
            raise IntegrityError(poison)
        return original_create(self, **kwargs)

    monkeypatch.setattr(QuerySet, "create", fail_append)

    for operation in (
        lambda: transition_ray_target_desired_state(
            "primary.ray",
            "active",
            expected_policy_revision=1,
        ),
        lambda: record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=NOW,
        ),
    ):
        with pytest.raises(RayTargetPersistenceRaceError) as error:
            operation()
        assert str(error.value) == "Ray target persistence could not serialize the mutation"
        assert poison not in str(error.value)

    assert RayTargetPolicyRevision.objects.count() == 1
    assert not RayTargetAttestationRevision.objects.exists()


def test_connection_open_failures_are_mapped_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation = _expectation()
    attestation = _attestation(expectation)
    poison = "connection-endpoint-user-path-secret"

    def fail_get_autocommit() -> bool:
        raise OperationalError(poison)

    monkeypatch.setattr(connections["default"], "get_autocommit", fail_get_autocommit)

    for operation in (
        lambda: register_ray_target(expectation),
        lambda: transition_ray_target_desired_state(
            "primary.ray",
            "active",
            expected_policy_revision=1,
        ),
        lambda: record_ray_target_attestation(
            "primary.ray",
            attestation,
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=NOW,
        ),
    ):
        with pytest.raises(RayTargetPersistenceRaceError) as error:
            operation()
        assert str(error.value) == "Ray target persistence could not serialize the mutation"
        assert poison not in str(error.value)

    assert not RayTarget.objects.exists()
