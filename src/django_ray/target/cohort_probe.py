"""Private full-cluster discovery for the coordinated current-cohort guard.

The first proof may discover an instance behind a configured endpoint. A later
proof can instead require an already bound instance. Neither form registers a
target, advertises capacity, or authenticates a result delivered over a Jobs
transport; that requires the exact pending challenge and database transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from django_ray.target import probe
from django_ray.target.attestation import (
    RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS,
    RayClusterAttestation,
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    encode_ray_target_expectation,
)
from django_ray.target.cohort_contract import _package_version
from django_ray.target.cohort_runtime import _local_runtime


def derive_cohort_target_key(runner_family: RayRunnerFamily, cluster_session: str) -> str:
    """Name a verified session under the immutable version-one derivation rule.

    Endpoints, aliases, package versions and runtime tuples cannot fork this
    identity to escape an existing target's drain. Core and Jobs have separate
    target policies, so this is deliberately not a cluster-wide drain key.
    This pure function does not authenticate the supplied session observation.
    """
    from django_ray.target.attestation import _cluster_session

    if type(runner_family) is not RayRunnerFamily:
        raise ValueError("Invalid cohort target identity")
    session = _cluster_session(cluster_session)
    canonical = json.dumps([runner_family.value, session], separators=(",", ":")).encode("ascii")
    digest = hashlib.sha256(b"django-ray/cohort-target-key/v1\x00" + canonical).hexdigest()
    return f"cohort-v1-{runner_family.value}-{digest}"


def observe_current_cohort_target(
    *,
    target_key: str | None,
    runner_family: RayRunnerFamily,
    expected_django_ray_version: str,
    expected_runtime: RayRuntimeVersion,
    expected_cluster_session: str | None = None,
    policy_revision: int = 1,
    ttl_seconds: int = 30,
    timeout_seconds: float = probe.RAY_TARGET_PROBE_DEFAULT_TIMEOUT_SECONDS,
    max_nodes: int = probe.RAY_TARGET_PROBE_DEFAULT_MAX_NODES,
) -> RayClusterAttestation:
    """Observe every schedulable node before a current-cohort target can qualify.

    The caller owns and initializes the connection, supplies trusted endpoint
    configuration and expected tuple, and validates an authenticated challenge
    on receipt. Generic nodes need only Ray: the existing bounded collector
    sends its observer by value. No application module or Django is imported.
    First discovery requires a null key/session and revision one; only the
    validated observation determines its final key. Refreshes require both
    the exact retained key and session and never relabel an existing proof.
    """
    try:
        if type(runner_family) is not RayRunnerFamily or type(expected_runtime) is not (
            RayRuntimeVersion
        ):
            raise ValueError
        if expected_cluster_session is None:
            if target_key is not None or policy_revision != 1:
                raise ValueError
        elif target_key is None:
            raise ValueError
        # Validate bounded expectation fields without inventing a stored session.
        validation_expectation = RayTargetExpectation(
            target_key if target_key is not None else "session-validation",
            runner_family,
            "session_validation" if expected_cluster_session is None else expected_cluster_session,
            policy_revision,
            expected_runtime,
        )
        encode_ray_target_expectation(validation_expectation)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= (
            RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS
        ):
            raise ValueError
        _package_version(expected_django_ray_version)
        probe._validate_probe_limits(timeout_seconds=timeout_seconds, max_nodes=max_nodes)
    except Exception:
        raise probe.RayTargetProbeError(probe.RayTargetProbeFailure.INVALID_CONFIGURATION) from None
    try:
        import ray

        package, runtime = _local_runtime(ray)
        _package_version(package)
        encode_ray_target_expectation(replace(validation_expectation, runtime=runtime))
    except Exception:
        raise probe.RayTargetProbeError(
            probe.RayTargetProbeFailure.PUBLIC_RUNTIME_UNAVAILABLE
        ) from None
    if package != expected_django_ray_version or runtime != expected_runtime:
        raise probe.RayTargetProbeError(probe.RayTargetProbeFailure.RUNTIME_MISMATCH) from None

    try:
        raw = probe._collect_raw_cluster_observation(
            timeout_seconds=timeout_seconds, max_nodes=max_nodes
        )
    except probe.RayTargetProbeError as error:
        raise probe.RayTargetProbeError(error.classification) from None
    except Exception:
        raise probe.RayTargetProbeError(
            probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE
        ) from None
    try:
        # The collector verifies every node against its coordinator and caller.
        # Bind that observed caller back to this trusted manager/driver.
        if (
            raw.caller.ray_version != ray.__version__
            or raw.caller.python_implementation != runtime.python_implementation
            or raw.caller.python_version
            != (runtime.python_major, runtime.python_minor, runtime.python_patch)
        ):
            raise probe.RayTargetProbeError(probe.RayTargetProbeFailure.RUNTIME_MISMATCH)
        if (
            expected_cluster_session is not None
            and raw.caller.session_name != expected_cluster_session
        ):
            raise probe.RayTargetProbeError(probe.RayTargetProbeFailure.SESSION_MISMATCH)
        expectation = RayTargetExpectation(
            target_key
            if target_key is not None
            else derive_cohort_target_key(runner_family, raw.caller.session_name),
            runner_family,
            raw.caller.session_name,
            policy_revision,
            runtime,
        )
        boundary = build_ray_observation_boundary(
            resource_state_version_before=raw.interval.before.cluster_resource_state_version,
            resource_state_version_after=raw.interval.after.cluster_resource_state_version,
            node_state_versions_before=tuple(
                RayNodeStateVersion(node_id, version)
                for node_id, version in raw.interval.before.node_state_versions
            ),
            node_state_versions_after=tuple(
                RayNodeStateVersion(node_id, version)
                for node_id, version in raw.interval.after.node_state_versions
            ),
        )
        observed_at = datetime.now(UTC)
        return build_ray_cluster_attestation(
            expectation=expectation,
            boundary=boundary,
            nodes=tuple(
                build_ray_node_observation(
                    node_id=node.node_id, cluster_session=node.session_name, runtime=runtime
                )
                for node in raw.interval.nodes
            ),
            observed_at=observed_at,
            expires_at=observed_at + timedelta(seconds=ttl_seconds),
        )
    except probe.RayTargetProbeError as error:
        raise probe.RayTargetProbeError(error.classification) from None
    except Exception:
        raise probe.RayTargetProbeError(
            probe.RayTargetProbeFailure.ATTESTATION_BUILD_FAILED
        ) from None
