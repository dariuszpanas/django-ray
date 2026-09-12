from __future__ import annotations

import builtins
import platform
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from django_ray import __version__
from django_ray.execution_codec import ExecutionIdentity
from django_ray.target import cohort_runtime, probe
from django_ray.target.attestation import (
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    ray_target_expectation_digest,
)
from django_ray.target.cohort_contract import (
    CohortExecutionContract,
    CohortRuntimeMismatch,
    cohort_execution_contract_digest,
    cohort_leaf_contract_digest,
    derive_cohort_leaf_contract,
    encode_cohort_execution_contract,
    encode_cohort_leaf_contract,
)
from django_ray.target.cohort_probe import derive_cohort_target_key, observe_current_cohort_target
from django_ray.target.cohort_runtime import (
    CohortRuntimeGuardError,
    CohortRuntimeGuardReason,
    verify_cohort_runtime,
)

NODE = "1" * 56
OTHER_NODE = "2" * 56
SESSION = "session_cohort_runtime"
CLAIMED_AT = datetime(2020, 1, 1, tzinfo=UTC)


def _contract(*, runtime=None, family=RayRunnerFamily.RAY_CORE, package=__version__):
    runtime = runtime or RayRuntimeVersion(
        2,
        58,
        0,
        platform.python_implementation().strip().lower(),
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    expectation = RayTargetExpectation("cohort-runtime", family, SESSION, 1, runtime)
    nodes = (RayNodeStateVersion(NODE, 1),)
    boundary = build_ray_observation_boundary(
        resource_state_version_before=1,
        resource_state_version_after=2,
        node_state_versions_before=nodes,
        node_state_versions_after=nodes,
    )
    attestation = build_ray_cluster_attestation(
        expectation=expectation,
        boundary=boundary,
        nodes=(build_ray_node_observation(node_id=NODE, cluster_session=SESSION, runtime=runtime),),
        observed_at=CLAIMED_AT - timedelta(seconds=1),
        expires_at=CLAIMED_AT + timedelta(seconds=1),
    )
    return CohortExecutionContract(
        identity=ExecutionIdentity(1, "cohort-test-task", 1, 2),
        expected_django_ray_version=package,
        target_binding_id=1,
        cohort_evidence_id=1,
        cohort_evidence_digest="sha256:" + "a" * 64,
        claimed_at=CLAIMED_AT,
        target_expectation=expectation,
        target_expectation_digest=ray_target_expectation_digest(expectation),
        claim_attestation=attestation,
        claim_attestation_digest=attestation.attestation_digest,
    )


@pytest.fixture
def runtime(monkeypatch):
    ray = ModuleType("ray")
    ray.__version__ = "2.58.0"
    ray.is_initialized = lambda: True
    ray.get_runtime_context = lambda: SimpleNamespace(
        get_node_id=lambda: NODE, get_session_name=lambda: SESSION
    )
    monkeypatch.setitem(sys.modules, "ray", ray)
    state = SimpleNamespace(
        session=SESSION, nodes=((NODE, 100),), snapshot_calls=0, timeout=None, max_nodes=None
    )

    def snapshot(_ray, *, timeout_seconds, max_nodes):
        assert _ray is ray
        state.snapshot_calls += 1
        state.timeout = timeout_seconds
        state.max_nodes = max_nodes
        return probe._ResourceStateSnapshot(state.session, 999, state.nodes)

    monkeypatch.setattr(probe, "_current_resource_state_snapshot", snapshot)
    return ray, state


@pytest.mark.parametrize("family", list(RayRunnerFamily))
@pytest.mark.parametrize("leaf", [False, True])
def test_point_guard_uses_actual_runtime_and_current_membership(runtime, family, leaf):
    _ray, state = runtime
    claim = _contract(family=family)
    if leaf:
        claim = derive_cohort_leaf_contract(claim)
    observed = verify_cohort_runtime(claim, timeout_seconds=2.0, max_nodes=4)
    assert observed.django_ray_version == __version__
    assert observed.runtime == claim.target_expectation.runtime
    assert observed.cluster_session == SESSION
    assert observed.node_id == NODE
    assert state.snapshot_calls == 1
    assert (state.timeout, state.max_nodes) == (2.0, 4)
    # A valid admission TTL is not a deadline for RuntimeEnv setup or a leaf.
    assert observed.observed_at > CLAIMED_AT + timedelta(seconds=1)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("ray_major", 3, CohortRuntimeMismatch.RAY_VERSION_MISMATCH),
        ("ray_minor", 57, CohortRuntimeMismatch.RAY_VERSION_MISMATCH),
        ("ray_patch", 1, CohortRuntimeMismatch.RAY_VERSION_MISMATCH),
        ("python_implementation", "pypy", CohortRuntimeMismatch.PYTHON_IMPLEMENTATION_MISMATCH),
        ("python_major", 4, CohortRuntimeMismatch.PYTHON_VERSION_MISMATCH),
        ("python_minor", 99, CohortRuntimeMismatch.PYTHON_VERSION_MISMATCH),
        ("python_patch", 99, CohortRuntimeMismatch.PYTHON_VERSION_MISMATCH),
    ],
)
def test_exact_tuple_mismatch_precedes_any_connection_read(runtime, field, value, reason):
    _ray, state = runtime
    expected = replace(_contract().target_expectation.runtime, **{field: value})
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract(runtime=expected))
    assert error.value.reason is reason
    assert state.snapshot_calls == 0


def test_package_mismatch_precedes_connection_and_ignores_ambient_bypass(runtime, monkeypatch):
    _ray, state = runtime
    monkeypatch.setenv("RAY_IGNORE_VERSION_MISMATCH", "1")
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract(package="0.4.0"))
    assert error.value.reason is CohortRuntimeMismatch.PACKAGE_VERSION_MISMATCH
    assert state.snapshot_calls == 0


@pytest.mark.parametrize("value", [None, "2.58", "2.58.0+local", "2.58.0rc1", "2.58.0.post1"])
def test_noncanonical_actual_ray_version_cannot_reach_private_api(runtime, value):
    ray, state = runtime
    ray.__version__ = value
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract())
    assert error.value.reason is CohortRuntimeGuardReason.RUNTIME_UNAVAILABLE
    assert state.snapshot_calls == 0


def test_matching_future_ray_tuple_still_requires_reviewed_private_adapter(runtime):
    ray, state = runtime
    ray.__version__ = "2.59.0"
    expected = replace(_contract().target_expectation.runtime, ray_minor=59)
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract(runtime=expected))
    assert error.value.reason is CohortRuntimeGuardReason.UNSUPPORTED_RAY_VERSION
    assert state.snapshot_calls == 0


@pytest.mark.parametrize("source", ["package", "implementation"])
def test_malformed_actual_version_metadata_has_a_fixed_guard_failure(runtime, monkeypatch, source):
    import django_ray

    _ray, state = runtime
    claim = _contract()
    if source == "package":
        monkeypatch.setattr(django_ray, "__version__", "not-a-package-version")
    else:
        monkeypatch.setattr(platform, "python_implementation", lambda: "invalid/implementation")
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(claim)
    assert error.value.reason is CohortRuntimeGuardReason.RUNTIME_UNAVAILABLE
    assert state.snapshot_calls == 0


@pytest.mark.parametrize("leaf", [False, True])
def test_membership_change_blocks_outer_and_nested_boundaries(runtime, leaf):
    _ray, state = runtime
    state.nodes = ((NODE, 100), (OTHER_NODE, 1))
    claim = _contract()
    if leaf:
        claim = derive_cohort_leaf_contract(claim)
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(claim)
    assert error.value.reason is CohortRuntimeGuardReason.MEMBERSHIP_CHANGED


def test_replaced_cluster_is_not_accepted_at_same_configured_endpoint(runtime):
    ray, state = runtime
    state.session = "session_replacement"
    ray.get_runtime_context = lambda: SimpleNamespace(
        get_node_id=lambda: NODE, get_session_name=lambda: state.session
    )
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract())
    assert error.value.reason is CohortRuntimeMismatch.CLUSTER_SESSION_MISMATCH


@pytest.mark.parametrize(
    "change", ["session_race", "caller_absent", "duplicate_node", "negative_version"]
)
def test_incoherent_observations_remain_observation_failures(runtime, change):
    _ray, state = runtime
    if change == "session_race":
        state.session = "session_other"
    elif change == "caller_absent":
        state.nodes = ((OTHER_NODE, 1),)
    elif change == "duplicate_node":
        state.nodes = ((NODE, 1), (NODE, 2))
    else:
        state.nodes = ((NODE, -1),)
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract())
    assert error.value.reason is CohortRuntimeGuardReason.OBSERVATION_INCONSISTENT


def test_runtime_unavailability_is_not_compatibility_or_no_effects_proof(runtime):
    ray, _state = runtime
    ray.is_initialized = lambda: False
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract())
    assert error.value.reason is CohortRuntimeGuardReason.RUNTIME_UNAVAILABLE
    assert not hasattr(error.value, "no_application_effects")


def test_dependency_failure_diagnostics_do_not_echo_secrets(runtime, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("Bearer secret-from-dependency")

    monkeypatch.setattr(probe, "_current_resource_state_snapshot", unavailable)
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract())
    assert error.value.reason is CohortRuntimeGuardReason.OBSERVATION_UNAVAILABLE
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_point_check_cannot_enter_django_or_initialize_ray(runtime, monkeypatch):
    ray, _state = runtime
    original = builtins.__import__

    def deny_django(name, *args, **kwargs):
        if name == "django" or name.startswith("django."):
            raise AssertionError("Django must remain untouched")
        return original(name, *args, **kwargs)

    def deny_init(*args, **kwargs):
        raise AssertionError("guard cannot initialize Ray")

    ray.init = deny_init
    monkeypatch.setattr(builtins, "__import__", deny_django)
    verify_cohort_runtime(_contract())


def test_invalid_claim_is_rejected_before_ray_import(monkeypatch):
    original = builtins.__import__

    def deny_ray(name, *args, **kwargs):
        if name == "ray" or name.startswith("ray."):
            raise AssertionError("invalid claim cannot import Ray")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_ray)
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(replace(_contract(), cohort_evidence_digest="invalid"))
    assert error.value.reason is CohortRuntimeGuardReason.INVALID_CONTRACT


def test_backwards_clock_does_not_claim_a_passing_observation(runtime, monkeypatch):
    class BackwardsClock:
        @staticmethod
        def now(_tz):
            return CLAIMED_AT - timedelta(seconds=1)

    monkeypatch.setattr(cohort_runtime, "datetime", BackwardsClock)
    with pytest.raises(CohortRuntimeGuardError) as error:
        verify_cohort_runtime(_contract())
    assert error.value.reason is CohortRuntimeGuardReason.CLOCK_REGRESSION


@pytest.mark.real_ray
def test_native_discovery_and_worker_guards_keep_django_uninitialized(ray_cluster: Any) -> None:
    """Exercise the private helpers on Ray; this does not activate task claims."""
    _package, actual_runtime = cohort_runtime._local_runtime(ray_cluster)
    attestation = observe_current_cohort_target(
        target_key=None,
        runner_family=RayRunnerFamily.RAY_CORE,
        expected_django_ray_version=__version__,
        expected_runtime=actual_runtime,
        ttl_seconds=60,
        timeout_seconds=20,
        max_nodes=4,
    )
    assert attestation.expectation.cluster_session == (
        ray_cluster.get_runtime_context().get_session_name()
    )
    assert attestation.expectation.target_key == derive_cohort_target_key(
        RayRunnerFamily.RAY_CORE, attestation.expectation.cluster_session
    )
    claim = CohortExecutionContract(
        identity=ExecutionIdentity(1, "native-cohort-guard", 1, 1),
        expected_django_ray_version=__version__,
        target_binding_id=1,
        cohort_evidence_id=1,
        cohort_evidence_digest="sha256:" + "a" * 64,
        claimed_at=datetime.now(UTC),
        target_expectation=attestation.expectation,
        target_expectation_digest=ray_target_expectation_digest(attestation.expectation),
        claim_attestation=attestation,
        claim_attestation_digest=attestation.attestation_digest,
    )
    leaf = derive_cohort_leaf_contract(claim)
    incompatible = replace(claim, expected_django_ray_version="999.0.0")

    @ray_cluster.remote(num_cpus=0.1)
    def check_boundary(serialized, digest, leaf_boundary):
        import builtins
        import sys

        assert not any(name == "django" or name.startswith("django.") for name in sys.modules)
        original_import = builtins.__import__

        def deny_django(name, *args, **kwargs):
            if name == "django" or name.startswith("django."):
                raise AssertionError("native cohort boundary imported Django")
            return original_import(name, *args, **kwargs)

        builtins.__import__ = deny_django
        try:
            from django_ray.execution_codec import ExecutionIdentity
            from django_ray.target.cohort_contract import (
                decode_cohort_execution_contract,
                decode_cohort_leaf_contract,
            )
            from django_ray.target.cohort_runtime import (
                CohortRuntimeGuardError,
                verify_cohort_runtime,
            )

            decoder = (
                decode_cohort_leaf_contract if leaf_boundary else decode_cohort_execution_contract
            )
            decoded = decoder(
                serialized,
                expected_identity=ExecutionIdentity(1, "native-cohort-guard", 1, 1),
                expected_contract_digest=digest,
            )
            try:
                observed = verify_cohort_runtime(decoded, timeout_seconds=5, max_nodes=4)
            except CohortRuntimeGuardError as error:
                return {"rejected": error.reason.value}
            return {
                "package": observed.django_ray_version,
                "session": observed.cluster_session,
                "membership": observed.membership_digest,
            }
        finally:
            builtins.__import__ = original_import

    pending = [
        check_boundary.remote(
            encode_cohort_execution_contract(claim), cohort_execution_contract_digest(claim), False
        ),
        check_boundary.remote(
            encode_cohort_leaf_contract(leaf), cohort_leaf_contract_digest(leaf), True
        ),
        check_boundary.remote(
            encode_cohort_execution_contract(incompatible),
            cohort_execution_contract_digest(incompatible),
            False,
        ),
    ]
    try:
        outer_result, leaf_result, rejection = ray_cluster.get(pending, timeout=30)
        assert (
            outer_result
            == leaf_result
            == {
                "package": __version__,
                "session": attestation.expectation.cluster_session,
                "membership": attestation.membership_digest,
            }
        )
        assert rejection == {"rejected": "package_version_mismatch"}
        assert ray_cluster.is_initialized()
    finally:
        for reference in pending:
            ray_cluster.cancel(reference, force=True)
        ray_cluster.wait(pending, num_returns=len(pending), timeout=5)
