from __future__ import annotations

import builtins
import platform
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import django_ray
from django_ray.target import probe
from django_ray.target.attestation import (
    RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS,
    RayRunnerFamily,
    RayRuntimeVersion,
    compare_ray_target_attestation,
    decode_ray_cluster_attestation,
    encode_ray_cluster_attestation,
)
from django_ray.target.cohort_probe import derive_cohort_target_key, observe_current_cohort_target

NODE = "1" * 56
OTHER_NODE = "2" * 56
SESSION = "session_first_discovery"


def local_runtime():
    return RayRuntimeVersion(
        2,
        58,
        0,
        platform.python_implementation().strip().lower(),
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )


def arguments(**changes):
    return {
        "target_key": None,
        "runner_family": RayRunnerFamily.RAY_CORE,
        "expected_django_ray_version": django_ray.__version__,
        "expected_runtime": local_runtime(),
        "policy_revision": 1,
        "ttl_seconds": 12,
        "timeout_seconds": 3.0,
        "max_nodes": 4,
    } | changes


def node_observation(node_id=NODE, *, session=SESSION):
    runtime = local_runtime()
    return {
        "node_id": node_id,
        "session_name": session,
        "ray_version": "2.58.0",
        "python_implementation": runtime.python_implementation,
        "python_version": (f"{runtime.python_major}.{runtime.python_minor}.{runtime.python_patch}"),
    }


def interval(*, session=SESSION):
    return {
        "ok": True,
        "coordinator": node_observation(session=session),
        "before": {
            "session_name": session,
            "cluster_resource_state_version": 1,
            "node_state_versions": [[NODE, 1], [OTHER_NODE, 2]],
        },
        "after": {
            "session_name": session,
            "cluster_resource_state_version": 20,
            "node_state_versions": [[NODE, 10], [OTHER_NODE, 21]],
        },
        "nodes": [node_observation(node, session=session) for node in (NODE, OTHER_NODE)],
    }


@pytest.fixture
def cluster(monkeypatch):
    ray = ModuleType("ray")
    ray.__version__ = "2.58.0"
    state = SimpleNamespace(
        session=SESSION,
        initialized=True,
        connection_calls=0,
        coordinator_calls=0,
        packet=interval(),
        max_nodes=None,
        owned_cleanup=None,
    )

    def initialized():
        state.connection_calls += 1
        return state.initialized

    def no_init(*args, **kwargs):
        pytest.fail("The cohort probe must not initialize Ray")

    def run_coordinator(_ray, *, deadline, max_nodes, owned_cleanup=False):
        assert _ray is ray
        state.coordinator_calls += 1
        state.max_nodes = max_nodes
        state.owned_cleanup = owned_cleanup
        # Keep the actual collector and remote-envelope validation in this path.
        return probe._decode_remote_interval(state.packet, max_nodes=max_nodes)

    ray.is_initialized = initialized
    ray.init = no_init
    ray.get_runtime_context = lambda: SimpleNamespace(
        get_node_id=lambda: NODE,
        get_session_name=lambda: state.session,
    )
    monkeypatch.setitem(sys.modules, "ray", ray)
    monkeypatch.setattr(probe, "_run_cluster_coordinator", run_coordinator)
    return ray, state


def assert_failure(error, reason):
    assert error.value.classification is reason
    assert str(error.value) == f"Ray target probe failed: {reason.value}"
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("owned_cleanup", [False, True])
def test_supervised_cleanup_policy_reaches_the_actual_collector(cluster, owned_cleanup):
    _ray, state = cluster
    proof = observe_current_cohort_target(**arguments(owned_cleanup=owned_cleanup))
    assert proof.expectation.cluster_session == SESSION
    assert state.owned_cleanup is owned_cleanup


@pytest.mark.parametrize("owned_cleanup", [None, 1, "true"])
def test_supervised_cleanup_policy_refuses_invalid_values_before_ray(cluster, owned_cleanup):
    _ray, state = cluster
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments(owned_cleanup=owned_cleanup))
    assert_failure(error, probe.RayTargetProbeFailure.INVALID_CONFIGURATION)
    assert state.connection_calls == 0


def test_session_key_derivation_has_a_fixed_versioned_vector_and_family_scope():
    key = derive_cohort_target_key(RayRunnerFamily.RAY_CORE, SESSION)
    assert key == (
        "cohort-v1-ray_core-7bdd78aa0a36baf1b6be70596fd860b2abe93781e37c034d63c10270a338108f"
    )
    assert key != derive_cohort_target_key(RayRunnerFamily.RAY_JOB, SESSION)
    assert key != derive_cohort_target_key(RayRunnerFamily.RAY_CORE, SESSION + "_replacement")


@pytest.mark.parametrize("session", [None, False, "", "secret://endpoint", "x" * 257])
def test_session_key_derivation_rejects_invalid_observations(session):
    with pytest.raises(ValueError):
        derive_cohort_target_key(RayRunnerFamily.RAY_CORE, session)


@pytest.mark.parametrize(
    "changes",
    [
        {"target_key": "caller-selected"},
        {"policy_revision": 2},
        {"expected_cluster_session": SESSION},
    ],
)
def test_discovery_mode_cannot_choose_a_key_or_impersonate_a_refresh(cluster, changes):
    _ray, state = cluster
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments(**changes))
    assert_failure(error, probe.RayTargetProbeFailure.INVALID_CONFIGURATION)
    assert (state.connection_calls, state.coordinator_calls) == (0, 0)


@pytest.mark.parametrize("family", list(RayRunnerFamily))
def test_first_proof_discovers_actual_session_and_all_nodes(cluster, family):
    _ray, state = cluster
    attestation = observe_current_cohort_target(**arguments(runner_family=family))
    assert attestation.expectation.cluster_session == SESSION
    assert attestation.expectation.runner_family is family
    assert attestation.expectation.policy_revision == 1
    assert attestation.expectation.target_key == derive_cohort_target_key(family, SESSION)
    assert attestation.expectation.runtime == local_runtime()
    assert tuple(node.node_id for node in attestation.nodes) == (NODE, OTHER_NODE)
    assert attestation.expires_at - attestation.observed_at == timedelta(seconds=12)
    assert (
        decode_ray_cluster_attestation(encode_ray_cluster_attestation(attestation)) == attestation
    )
    compare_ray_target_attestation(
        attestation.expectation, attestation, now=attestation.observed_at
    )
    assert (state.coordinator_calls, state.max_nodes) == (1, 4)


@pytest.mark.parametrize("family", list(RayRunnerFamily))
def test_refresh_requires_original_session_and_never_rediscovers_replacement(cluster, family):
    _ray, state = cluster
    kwargs = arguments(
        runner_family=family,
        target_key="current-cohort",
        expected_cluster_session=SESSION,
        policy_revision=7,
    )
    first = observe_current_cohort_target(**kwargs)
    assert first.expectation.cluster_session == SESSION
    state.session = "session_replaced"
    state.packet = interval(session=state.session)
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**kwargs)
    assert_failure(error, probe.RayTargetProbeFailure.SESSION_MISMATCH)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ray_major", 3),
        ("ray_minor", 57),
        ("ray_patch", 1),
        ("python_implementation", "pypy"),
        ("python_major", 4),
        ("python_minor", 99),
        ("python_patch", 999),
    ],
)
def test_wrong_expected_runtime_refuses_before_connection_or_collector(cluster, field, value):
    _ray, state = cluster
    kwargs = arguments(expected_runtime=replace(local_runtime(), **{field: value}))
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**kwargs)
    assert_failure(error, probe.RayTargetProbeFailure.RUNTIME_MISMATCH)
    assert (state.connection_calls, state.coordinator_calls) == (0, 0)


def test_wrong_expected_package_refuses_before_connection(cluster):
    _ray, state = cluster
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments(expected_django_ray_version="0.4.0"))
    assert_failure(error, probe.RayTargetProbeFailure.RUNTIME_MISMATCH)
    assert (state.connection_calls, state.coordinator_calls) == (0, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 121},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"max_nodes": True},
        {"max_nodes": 0},
        {"max_nodes": 257},
        {"ttl_seconds": True},
        {"ttl_seconds": 1.0},
        {"ttl_seconds": 0},
        {"ttl_seconds": RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS + 1},
        {"target_key": ""},
        {"target_key": "secret-canary" * 100},
        {"runner_family": "ray_core"},
        {"expected_runtime": None},
        {"policy_revision": True},
        {"policy_revision": -1},
        {"policy_revision": 2**63},
        {"expected_django_ray_version": None},
        {"expected_django_ray_version": "v0.5.0"},
        {"expected_django_ray_version": "secret-canary"},
        {"expected_django_ray_version": "1" * 129},
        {"expected_cluster_session": ""},
        {"expected_cluster_session": False},
        {"expected_cluster_session": "https://ray-head:8265"},
    ],
)
def test_invalid_configuration_refuses_before_ray_import(monkeypatch, changes):
    original = builtins.__import__

    def no_ray(name, *args, **kwargs):
        if name == "ray" or name.startswith("ray."):
            pytest.fail("Invalid configuration must not import Ray")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ray)
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments(**changes))
    assert_failure(error, probe.RayTargetProbeFailure.INVALID_CONFIGURATION)


@pytest.mark.parametrize("kind", ["ray_version", "package", "implementation"])
def test_malformed_actual_runtime_is_unavailable_not_a_mismatch(cluster, monkeypatch, kind):
    ray, state = cluster
    kwargs = arguments()
    if kind == "ray_version":
        ray.__version__ = "2.58.0+unreviewed"
    elif kind == "package":
        monkeypatch.setattr(django_ray, "__version__", "secret-canary")
    else:
        monkeypatch.setattr(platform, "python_implementation", lambda: "secret-canary/invalid")
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**kwargs)
    assert_failure(error, probe.RayTargetProbeFailure.PUBLIC_RUNTIME_UNAVAILABLE)
    assert (state.connection_calls, state.coordinator_calls) == (0, 0)


def test_future_matching_runtime_still_requires_exact_reviewed_adapter(cluster):
    ray, state = cluster
    ray.__version__ = "2.59.0"
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(
            **arguments(expected_runtime=replace(local_runtime(), ray_minor=59))
        )
    assert_failure(error, probe.RayTargetProbeFailure.UNSUPPORTED_RAY_VERSION)
    assert (state.connection_calls, state.coordinator_calls) == (0, 0)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("python_version", "3.99.0", probe.RayTargetProbeFailure.RUNTIME_MISMATCH),
        ("python_implementation", "pypy", probe.RayTargetProbeFailure.RUNTIME_MISMATCH),
        ("ray_version", "2.57.0", probe.RayTargetProbeFailure.UNSUPPORTED_RAY_VERSION),
        ("session_name", "session_wrong_node", probe.RayTargetProbeFailure.SESSION_MISMATCH),
        ("node_id", "3" * 56, probe.RayTargetProbeFailure.NODE_ID_MISMATCH),
    ],
)
def test_every_schedulable_node_must_pass_the_existing_collector(cluster, field, value, reason):
    _ray, state = cluster
    state.packet["nodes"][1][field] = value
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments())
    assert_failure(error, reason)


@pytest.mark.parametrize("change", ["leave", "join", "missing_observation", "regression"])
def test_interval_changes_or_missing_node_proof_cannot_create_attestation(cluster, change):
    _ray, state = cluster
    reason = probe.RayTargetProbeFailure.MEMBERSHIP_CHANGED
    if change == "leave":
        state.packet["after"]["node_state_versions"].pop()
    elif change == "join":
        state.packet["after"]["node_state_versions"].append(["3" * 56, 1])
    elif change == "missing_observation":
        state.packet["nodes"].pop()
        reason = probe.RayTargetProbeFailure.NODE_ID_MISMATCH
    else:
        state.packet["after"]["cluster_resource_state_version"] = 0
        reason = probe.RayTargetProbeFailure.REVISION_REGRESSION
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments())
    assert_failure(error, reason)


@pytest.mark.parametrize(
    "reason",
    [
        probe.RayTargetProbeFailure.NODE_PROBE_TIMEOUT,
        probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE,
        probe.RayTargetProbeFailure.RESOURCE_LIMIT,
    ],
)
def test_remote_collector_refusal_remains_a_fixed_failure(cluster, reason):
    _ray, state = cluster
    state.packet = {"ok": False, "classification": reason.value}
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments())
    assert_failure(error, reason)


def test_selected_node_bound_limits_the_actual_collector(cluster):
    _ray, state = cluster
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments(max_nodes=1))
    assert_failure(error, probe.RayTargetProbeFailure.RESOURCE_LIMIT)
    assert state.max_nodes == 1


def test_uninitialized_connection_does_not_start_ray(cluster):
    _ray, state = cluster
    state.initialized = False
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments())
    assert_failure(error, probe.RayTargetProbeFailure.RAY_NOT_INITIALIZED)
    assert state.coordinator_calls == 0


def test_unexpected_collector_exception_cannot_leak_dependency_secrets(cluster, monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError("Bearer secret-canary")

    monkeypatch.setattr(probe, "_collect_raw_cluster_observation", unavailable)
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments())
    assert_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)


def test_invalid_raw_collector_value_remains_a_fixed_build_failure(cluster, monkeypatch):
    monkeypatch.setattr(probe, "_collect_raw_cluster_observation", lambda **kwargs: None)
    with pytest.raises(probe.RayTargetProbeError) as error:
        observe_current_cohort_target(**arguments())
    assert_failure(error, probe.RayTargetProbeFailure.ATTESTATION_BUILD_FAILED)


def test_discovery_never_imports_django_or_initializes_ray(cluster, monkeypatch):
    original = builtins.__import__

    def no_django(name, *args, **kwargs):
        if name == "django" or name.startswith("django."):
            pytest.fail("Django must remain untouched before cohort proof")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_django)
    observe_current_cohort_target(**arguments())


def test_probe_import_is_django_and_ray_free_in_a_fresh_process():
    root = Path(__file__).resolve().parents[2]
    code = """
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name.split('.')[0] in {'django', 'ray'}:
        raise AssertionError('forbidden framework import')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
import django_ray.target.cohort_probe
assert 'django' not in sys.modules and 'ray' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root / "src")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
