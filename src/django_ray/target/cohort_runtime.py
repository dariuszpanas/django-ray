"""Private, pre-Django runtime checks for a validated current-cohort claim.

This module does not activate execution protocol 3. A caller must independently
validate the claim's identity and digest before calling these helpers. A guard
refusal is neither an ordinary application failure nor proof that an outer
invocation, sibling, or earlier remote submission had no effects.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Never

from packaging.version import InvalidVersion, Version

from django_ray.target.attestation import (
    RayNodeStateVersion,
    RayRuntimeVersion,
    build_ray_observation_boundary,
    ray_membership_digest,
)
from django_ray.target.cohort_contract import (
    CohortContractError,
    CohortExecutionContract,
    CohortLeafContract,
    CohortRuntimeMismatch,
    compare_cohort_runtime,
    decode_cohort_execution_contract,
    decode_cohort_leaf_contract,
    encode_cohort_execution_contract,
    encode_cohort_leaf_contract,
)

if TYPE_CHECKING:
    from types import ModuleType


class CohortRuntimeGuardReason(StrEnum):
    """Fixed diagnostics; never include dependency exceptions or credentials."""

    INVALID_CONTRACT = "invalid_contract"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    UNSUPPORTED_RAY_VERSION = "unsupported_ray_version"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    OBSERVATION_INCONSISTENT = "observation_inconsistent"
    MEMBERSHIP_CHANGED = "membership_changed"
    CLOCK_REGRESSION = "clock_regression"


class CohortRuntimeGuardError(RuntimeError):
    """Refuse this boundary without making a durable lifecycle decision."""

    def __init__(self, reason: CohortRuntimeGuardReason | CohortRuntimeMismatch) -> None:
        if type(reason) not in {CohortRuntimeGuardReason, CohortRuntimeMismatch}:
            raise TypeError("invalid cohort guard reason")
        self.reason = reason
        super().__init__(f"Cohort runtime guard refused: {reason.value}")


@dataclass(frozen=True, slots=True)
class CohortRuntimeObservation:
    """One local executor and its bounded, current membership observation."""

    django_ray_version: str
    runtime: RayRuntimeVersion
    cluster_session: str
    node_id: str
    membership_digest: str
    observed_at: datetime


def _reject(reason: CohortRuntimeGuardReason | CohortRuntimeMismatch) -> Never:
    raise CohortRuntimeGuardError(reason) from None


def _local_runtime(ray: ModuleType) -> tuple[str, RayRuntimeVersion]:
    """Read the actual interpreter; never trust environment version overrides."""
    from django_ray import __version__

    try:
        value = ray.__version__  # type: ignore[unresolved-attribute]
        if type(value) is not str:
            raise ValueError
        version = Version(value)
        if (
            str(version) != value
            or len(version.release) != 3
            or version.epoch != 0
            or version.is_prerelease
            or version.is_postrelease
            or version.is_devrelease
            or version.local is not None
        ):
            raise ValueError
        runtime = RayRuntimeVersion(
            ray_major=version.release[0],
            ray_minor=version.release[1],
            ray_patch=version.release[2],
            python_implementation=platform.python_implementation().strip().lower(),
            python_major=sys.version_info.major,
            python_minor=sys.version_info.minor,
            python_patch=sys.version_info.micro,
        )
    except (AttributeError, InvalidVersion, TypeError, ValueError):
        _reject(CohortRuntimeGuardReason.RUNTIME_UNAVAILABLE)
    return __version__, runtime


def _canonical_claim(
    contract: CohortExecutionContract | CohortLeafContract,
) -> CohortExecutionContract | CohortLeafContract:
    try:
        if type(contract) is CohortExecutionContract:
            return decode_cohort_execution_contract(encode_cohort_execution_contract(contract))
        if type(contract) is CohortLeafContract:
            return decode_cohort_leaf_contract(encode_cohort_leaf_contract(contract))
    except CohortContractError:
        pass
    _reject(CohortRuntimeGuardReason.INVALID_CONTRACT)


def verify_cohort_runtime(
    contract: CohortExecutionContract | CohortLeafContract,
    *,
    timeout_seconds: float = 5.0,
    max_nodes: int = 64,
) -> CohortRuntimeObservation:
    """Check actual package/tuple/session and membership before application entry.

    Claim-time attestation validity is checked by the canonical contract, not
    reinterpreted as an execution deadline. RuntimeEnv setup or long workflows
    may outlive that admission TTL. The current snapshot must still match the
    claim's exact schedulable node set. No per-node jobs are launched here.

    This function never imports Django, initializes Ray, reads application
    inputs, or changes database state. All refusals leave outcome disposition
    to an independently fenced owner.
    """
    claim = _canonical_claim(contract)
    try:
        import ray
    except ImportError:
        _reject(CohortRuntimeGuardReason.RUNTIME_UNAVAILABLE)

    package, runtime = _local_runtime(ray)
    # The session supplied here is the expectation only to classify local
    # package/interpreter mismatches before touching any Ray connection.
    try:
        reason = compare_cohort_runtime(
            claim,
            actual_django_ray_version=package,
            actual_runtime=runtime,
            actual_cluster_session=claim.target_expectation.cluster_session,
        )
    except CohortContractError:
        _reject(CohortRuntimeGuardReason.RUNTIME_UNAVAILABLE)
    if reason is not None:
        _reject(reason)

    from django_ray.target import probe

    if ray.__version__ != probe.RAY_TARGET_PROBE_RAY_VERSION:
        _reject(CohortRuntimeGuardReason.UNSUPPORTED_RAY_VERSION)
    try:
        if ray.is_initialized() is not True:
            _reject(CohortRuntimeGuardReason.RUNTIME_UNAVAILABLE)
        caller = probe._current_caller_observation(ray)
        snapshot = probe._current_resource_state_snapshot(
            ray, timeout_seconds=timeout_seconds, max_nodes=max_nodes
        )
    except CohortRuntimeGuardError:
        raise
    except Exception:
        _reject(CohortRuntimeGuardReason.OBSERVATION_UNAVAILABLE)
    if (
        caller.session_name != snapshot.session_name
        or caller.node_id not in snapshot.node_ids
        or caller.ray_version != ray.__version__
        or caller.python_implementation != runtime.python_implementation
        or caller.python_version
        != (runtime.python_major, runtime.python_minor, runtime.python_patch)
    ):
        _reject(CohortRuntimeGuardReason.OBSERVATION_INCONSISTENT)
    try:
        reason = compare_cohort_runtime(
            claim,
            actual_django_ray_version=package,
            actual_runtime=runtime,
            actual_cluster_session=caller.session_name,
        )
    except CohortContractError:
        _reject(CohortRuntimeGuardReason.OBSERVATION_INCONSISTENT)
    if reason is not None:
        _reject(reason)
    try:
        node_states = tuple(
            RayNodeStateVersion(node_id=node_id, node_state_version=version)
            for node_id, version in snapshot.node_state_versions
        )
        boundary = build_ray_observation_boundary(
            resource_state_version_before=snapshot.cluster_resource_state_version,
            resource_state_version_after=snapshot.cluster_resource_state_version,
            node_state_versions_before=node_states,
            node_state_versions_after=node_states,
        )
        membership_digest = ray_membership_digest(boundary)
    except Exception:
        _reject(CohortRuntimeGuardReason.OBSERVATION_INCONSISTENT)
    expected_membership = (
        claim.claim_attestation.membership_digest
        if type(claim) is CohortExecutionContract
        else claim.claim_membership_digest
    )
    if membership_digest != expected_membership:
        _reject(CohortRuntimeGuardReason.MEMBERSHIP_CHANGED)
    observed_at = datetime.now(UTC)
    if observed_at < claim.claimed_at:
        _reject(CohortRuntimeGuardReason.CLOCK_REGRESSION)
    return CohortRuntimeObservation(
        django_ray_version=package,
        runtime=runtime,
        cluster_session=caller.session_name,
        node_id=caller.node_id,
        membership_digest=membership_digest,
        observed_at=observed_at,
    )
