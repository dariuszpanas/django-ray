"""Fail-closed compatibility policy for Ray Compiled Graph.

This module deliberately contains no imports from :mod:`ray`.  A caller can make a
capability decision before importing or invoking Ray's beta Compiled Graph APIs.  The
decision is JSON-safe and may be included in an effective workflow plan; process-local
Ray objects must never be persisted with it.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import sysconfig
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from importlib import metadata
from pathlib import Path
from typing import Any

COMPILED_GRAPH_CAPABILITY_SCHEMA_VERSION = 2
COMPILED_GRAPH_POLICY_VERSION = 3
_MAX_IDENTITY_FIELD_CHARS = 1_024
_MAX_DECISION_TEXT_CHARS = 2_048
_MAX_REQUEST_DIMENSION_CHARS = 256


class CompiledGraphTopology(StrEnum):
    """The process topology that will own compilation and every invocation."""

    DIRECT_DRIVER = "direct-driver"
    NESTED_RAY_TASK = "nested-ray-task"
    RAY_JOB_DRIVER = "ray-job-driver"
    RAY_CLIENT_DRIVER = "ray-client-driver"


class CompiledGraphTransport(StrEnum):
    """The channel family requested by the effective workflow plan."""

    CPU_SHARED_MEMORY = "cpu-shared-memory"
    GPU_NCCL = "gpu-nccl"


class CompiledGraphSubmissionTransport(StrEnum):
    """How the compiler-owner process is reached from the submitting process."""

    DIRECT_RAY_CORE = "direct-ray-core"
    RAY_CLIENT = "ray-client"
    RAY_JOB = "ray-job"


class CompiledGraphReason(StrEnum):
    """Stable reason codes returned by the compatibility policy."""

    ELIGIBLE = "ELIGIBLE"
    CANDIDATE_REQUIRES_SMOKE = "CANDIDATE_REQUIRES_SMOKE"
    INCOMPLETE_CAPABILITY_CONTEXT = "INCOMPLETE_CAPABILITY_CONTEXT"
    INVALID_RUNTIME_IDENTITY = "INVALID_RUNTIME_IDENTITY"
    RAY_NOT_INSTALLED = "RAY_NOT_INSTALLED"
    UNSUPPORTED_RAY_VERSION = "UNSUPPORTED_RAY_VERSION"
    UNSUPPORTED_PYTHON = "UNSUPPORTED_PYTHON"
    UNSUPPORTED_OPERATING_SYSTEM = "UNSUPPORTED_OPERATING_SYSTEM"
    UNSUPPORTED_ARCHITECTURE = "UNSUPPORTED_ARCHITECTURE"
    UNSUPPORTED_TOPOLOGY = "UNSUPPORTED_TOPOLOGY"
    UNSUPPORTED_SUBMISSION_TRANSPORT = "UNSUPPORTED_SUBMISSION_TRANSPORT"
    UNSUPPORTED_TRANSPORT = "UNSUPPORTED_TRANSPORT"


@dataclass(frozen=True)
class CompiledGraphRuntimeIdentity:
    """Serializable runtime facts that participate in capability identity."""

    ray_version: str | None
    python_version: str
    operating_system: str
    architecture: str
    python_implementation: str | None = None
    python_abi: str | None = None
    dependency_profile: str | None = None
    platform_profile: str | None = None
    libc_profile: str | None = None
    container_profile: str | None = None
    deployment_profile: str | None = None
    shared_memory_profile: str | None = None
    object_store_profile: str | None = None

    def asdict(self) -> dict[str, str | None]:
        """Return a bounded, JSON-safe representation without importing Ray."""
        values = {
            "ray_version": self.ray_version,
            "python_version": self.python_version,
            "operating_system": self.operating_system,
            "architecture": self.architecture,
            "python_implementation": self.python_implementation,
            "python_abi": self.python_abi,
            "dependency_profile": self.dependency_profile,
            "platform_profile": self.platform_profile,
            "libc_profile": self.libc_profile,
            "container_profile": self.container_profile,
            "deployment_profile": self.deployment_profile,
            "shared_memory_profile": self.shared_memory_profile,
            "object_store_profile": self.object_store_profile,
        }
        return {
            name: _digest_safe_text(value, limit=_MAX_IDENTITY_FIELD_CHARS)
            for name, value in values.items()
        }


@dataclass(frozen=True)
class CompiledGraphCapabilityDecision:
    """Versioned, bounded result of evaluating one native capability request."""

    eligible: bool
    reason: CompiledGraphReason
    message: str
    topology: str
    submission_transport: str
    transport: str
    runtime: CompiledGraphRuntimeIdentity
    candidate: bool = False
    verified: bool = False
    capability_set: str | None = None
    schema_version: int = COMPILED_GRAPH_CAPABILITY_SCHEMA_VERSION
    policy_version: int = COMPILED_GRAPH_POLICY_VERSION

    @property
    def plan_rejection_code(self) -> str | None:
        """Map the detailed decision to the workflow-plan rejection vocabulary."""
        if self.eligible:
            return None
        if self.reason is CompiledGraphReason.UNSUPPORTED_TOPOLOGY:
            return "OWNER_LIFETIME_MISMATCH"
        if self.reason is CompiledGraphReason.UNSUPPORTED_SUBMISSION_TRANSPORT:
            return "OWNER_LIFETIME_MISMATCH"
        if self.reason is CompiledGraphReason.UNSUPPORTED_TRANSPORT:
            return "UNSUPPORTED_TRANSPORT"
        return "INCOMPATIBLE_PLATFORM"

    def asdict(self) -> dict[str, Any]:
        """Return the complete bounded, JSON-safe compatibility record."""
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "eligible": self.eligible,
            "candidate": self.candidate,
            "verified": self.verified,
            "reason": self.reason.value,
            "plan_rejection_code": self.plan_rejection_code,
            "message": _digest_safe_text(self.message, limit=_MAX_DECISION_TEXT_CHARS),
            "topology": _digest_safe_text(
                self.topology,
                limit=_MAX_REQUEST_DIMENSION_CHARS,
            ),
            "submission_transport": _digest_safe_text(
                self.submission_transport,
                limit=_MAX_REQUEST_DIMENSION_CHARS,
            ),
            "transport": _digest_safe_text(
                self.transport,
                limit=_MAX_REQUEST_DIMENSION_CHARS,
            ),
            "runtime": self.runtime.asdict(),
            "capability_set": self.capability_set,
            "beta": True,
        }


class CompiledGraphUnsupportedError(RuntimeError):
    """Raised before native Ray APIs when the requested combination is unsupported."""

    def __init__(self, decision: CompiledGraphCapabilityDecision) -> None:
        self.decision = decision
        super().__init__(f"{decision.reason.value}: {decision.message}")


@dataclass(frozen=True)
class _CandidateRuntime:
    ray_version: str
    python_minor: tuple[int, int]
    operating_system: str = "linux"
    architecture: str = "x86_64"


@dataclass(frozen=True)
class _CapabilityIdentity:
    """Every exact dimension that a reviewed native smoke is allowed to verify."""

    runtime: CompiledGraphRuntimeIdentity
    topology: CompiledGraphTopology
    submission_transport: CompiledGraphSubmissionTransport
    transport: CompiledGraphTransport


# Exact versions are intentional. Compiled Graph is beta, and a future patch release is
# not silently treated as equivalent until its subprocess canary has passed. 2.56.0 is
# retained because it is the package security floor and repository lock; 2.56.1 is the
# latest release reviewed when the original candidate set was established.
_CANDIDATE_RUNTIMES = frozenset(
    {
        _CandidateRuntime("2.56.0", (3, 12)),
        _CandidateRuntime("2.56.1", (3, 12)),
    }
)
_CANDIDATE_TOPOLOGIES = frozenset(
    {
        CompiledGraphTopology.DIRECT_DRIVER,
        CompiledGraphTopology.NESTED_RAY_TASK,
        CompiledGraphTopology.RAY_JOB_DRIVER,
    }
)
_CANDIDATE_OWNER_CONTEXTS = frozenset(
    {
        (
            CompiledGraphTopology.DIRECT_DRIVER,
            CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        ),
        (
            CompiledGraphTopology.NESTED_RAY_TASK,
            CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        ),
        (
            CompiledGraphTopology.RAY_JOB_DRIVER,
            CompiledGraphSubmissionTransport.RAY_JOB,
        ),
    }
)
# A capability enters this set only after a real subprocess-isolated native smoke has
# passed for its exact runtime, owner topology, submission transport, and channel
# transport. Unit tests and version-shape checks are not evidence. The initial policy
# intentionally ships closed; CI introduced with this policy gathers the evidence needed
# for a reviewed follow-up revision.
_VERIFIED_CAPABILITIES: frozenset[_CapabilityIdentity] = frozenset()
_VERSION_PREFIX = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?")
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[^\d].*)?$")
_IMMUTABLE_DEPLOYMENT_PROFILE = re.compile(r"^(?:[a-z0-9._/-]+@)?sha256:[0-9a-f]{64}$")
_PROFILE_DISTRIBUTIONS = (
    "ray",
    "numpy",
    "pyarrow",
    "cupy",
    "cupy-cuda11x",
    "cupy-cuda12x",
)
_CONTAINER_PROFILE_ENV = "DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE"
_DEPLOYMENT_PROFILE_ENV = "DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE"
_SHARED_MEMORY_PROFILE_ENV = "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE"
_OBJECT_STORE_PROFILE_ENV = "DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE"
_UNRESOLVED_PROFILE_VALUES = frozenset(
    {"default", "generic", "none", "unknown", "unavailable", "unresolved"}
)
_GENERIC_CONTAINER_PROFILES = frozenset({"container", "docker", "host"})


def detect_compiled_graph_runtime() -> CompiledGraphRuntimeIdentity:
    """Detect compatibility inputs without importing :mod:`ray`."""
    try:
        ray_version: str | None = metadata.version("ray")
    except metadata.PackageNotFoundError:
        ray_version = None
    return CompiledGraphRuntimeIdentity(
        ray_version=ray_version,
        python_version=platform.python_version(),
        operating_system=_normalize_operating_system(platform.system()),
        architecture=_normalize_architecture(platform.machine()),
        python_implementation=platform.python_implementation().strip().lower(),
        python_abi=str(sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag or ""),
        dependency_profile=_detect_dependency_profile(),
        platform_profile=platform.platform(),
        libc_profile=_detect_libc_profile(),
        container_profile=_detect_container_profile(),
        deployment_profile=_detect_deployment_profile(),
        shared_memory_profile=_detect_shared_memory_profile(),
        object_store_profile=_detect_object_store_profile(),
    )


def evaluate_compiled_graph_support(
    topology: CompiledGraphTopology | str,
    transport: CompiledGraphTransport | str = CompiledGraphTransport.CPU_SHARED_MEMORY,
    *,
    submission_transport: CompiledGraphSubmissionTransport | str | None = None,
    runtime: CompiledGraphRuntimeIdentity | None = None,
) -> CompiledGraphCapabilityDecision:
    """Return the policy decision that must precede any native compilation call."""
    identity = runtime or detect_compiled_graph_runtime()
    topology_value, parsed_topology = _parse_enum(CompiledGraphTopology, topology)
    transport_value, parsed_transport = _parse_enum(CompiledGraphTransport, transport)
    submission_value, parsed_submission = _parse_enum(
        CompiledGraphSubmissionTransport,
        submission_transport,
    )

    oversized_identity = _oversized_identity_fields(identity)
    if oversized_identity:
        return _reject(
            CompiledGraphReason.INVALID_RUNTIME_IDENTITY,
            (
                "Runtime identity fields exceed the bounded compatibility-record limit: "
                f"{', '.join(oversized_identity)}."
            ),
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    if identity.ray_version is None:
        return _reject(
            CompiledGraphReason.RAY_NOT_INSTALLED,
            "Ray is not installed; Compiled Graph cannot be selected.",
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    ray_version = _parse_version(identity.ray_version)
    python_version = _parse_version(identity.python_version)
    if ray_version is None or python_version is None:
        return _reject(
            CompiledGraphReason.INVALID_RUNTIME_IDENTITY,
            "Ray and Python versions must begin with numeric major.minor components.",
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    if identity.operating_system != "linux":
        return _reject(
            CompiledGraphReason.UNSUPPORTED_OPERATING_SYSTEM,
            (
                f"Compiled Graph policy v{COMPILED_GRAPH_POLICY_VERSION} permits only Linux; "
                f"detected {identity.operating_system or 'unknown'}."
            ),
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    if identity.architecture != "x86_64":
        return _reject(
            CompiledGraphReason.UNSUPPORTED_ARCHITECTURE,
            (
                f"Compiled Graph policy v{COMPILED_GRAPH_POLICY_VERSION} permits only x86_64; "
                f"detected {identity.architecture or 'unknown'}."
            ),
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    python_minor = python_version[:2]
    if python_minor != (3, 12):
        return _reject(
            CompiledGraphReason.UNSUPPORTED_PYTHON,
            (
                f"Python {identity.python_version} has no required Compiled Graph canary; "
                f"policy v{COMPILED_GRAPH_POLICY_VERSION} permits Python 3.12 only."
            ),
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    runtime_row = _CandidateRuntime(identity.ray_version, python_minor)
    if runtime_row not in _CANDIDATE_RUNTIMES:
        candidates = ", ".join(sorted(row.ray_version for row in _CANDIDATE_RUNTIMES))
        return _reject(
            CompiledGraphReason.UNSUPPORTED_RAY_VERSION,
            (
                f"Ray {identity.ray_version} is untested by Compiled Graph policy "
                f"v{COMPILED_GRAPH_POLICY_VERSION}; "
                f"candidate releases are {candidates}."
            ),
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    if parsed_topology not in _CANDIDATE_TOPOLOGIES:
        return _reject(
            CompiledGraphReason.UNSUPPORTED_TOPOLOGY,
            (
                f"Topology {topology_value!r} has no supported compiler-owner contract; "
                "direct compilation by a Ray Client driver and unknown compiler-owner "
                "topologies must use dynamic execution. A nested owner submitted through "
                "Ray Client is distinct from the candidate local nested-owner tuple and "
                "remains unverified."
            ),
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    if (parsed_topology, parsed_submission) not in _CANDIDATE_OWNER_CONTEXTS:
        return _reject(
            CompiledGraphReason.UNSUPPORTED_SUBMISSION_TRANSPORT,
            (
                f"Submission transport {submission_value!r} is not a candidate for "
                f"compiler owner {topology_value!r}. A local/direct Ray Core owner, a "
                "Ray Client-submitted owner, and a Ray Job owner require independent "
                "evidence and can never share a verified row."
            ),
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    if parsed_transport is not CompiledGraphTransport.CPU_SHARED_MEMORY:
        return _reject(
            CompiledGraphReason.UNSUPPORTED_TRANSPORT,
            (
                f"Transport {transport_value!r} has no production canary; policy "
                f"v{COMPILED_GRAPH_POLICY_VERSION} permits CPU shared-memory channels only."
            ),
            topology_value,
            submission_value,
            transport_value,
            identity,
        )

    missing_context = _missing_capability_context(identity)
    if missing_context:
        return CompiledGraphCapabilityDecision(
            eligible=False,
            candidate=True,
            verified=False,
            reason=CompiledGraphReason.INCOMPLETE_CAPABILITY_CONTEXT,
            message=(
                "Runtime matches a coarse Compiled Graph candidate, but exact capability "
                f"context is missing or invalid: {', '.join(missing_context)}. Dynamic "
                "execution remains available."
            ),
            topology=topology_value,
            submission_transport=submission_value,
            transport=transport_value,
            runtime=identity,
        )

    assert parsed_topology is not None
    assert parsed_submission is not None
    assert parsed_transport is not None
    capability = _CapabilityIdentity(
        runtime=identity,
        topology=parsed_topology,
        submission_transport=parsed_submission,
        transport=parsed_transport,
    )
    capability_set = _capability_set_identifier(capability)
    if capability not in _VERIFIED_CAPABILITIES:
        return CompiledGraphCapabilityDecision(
            eligible=False,
            candidate=True,
            verified=False,
            reason=CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE,
            message=(
                "Runtime matches a candidate Compiled Graph row, but no reviewed native "
                "subprocess smoke verifies this exact capability tuple. Dynamic execution "
                "remains available."
            ),
            topology=topology_value,
            submission_transport=submission_value,
            transport=transport_value,
            runtime=identity,
            capability_set=capability_set,
        )
    return CompiledGraphCapabilityDecision(
        eligible=True,
        candidate=True,
        verified=True,
        reason=CompiledGraphReason.ELIGIBLE,
        message=(
            "Runtime is eligible for an opt-in CPU Compiled Graph pilot; plan, owner, "
            "admission, lifecycle, and result checks still apply."
        ),
        topology=topology_value,
        submission_transport=submission_value,
        transport=transport_value,
        runtime=identity,
        capability_set=capability_set,
    )


def require_compiled_graph_support(
    topology: CompiledGraphTopology | str,
    transport: CompiledGraphTransport | str = CompiledGraphTransport.CPU_SHARED_MEMORY,
    *,
    submission_transport: CompiledGraphSubmissionTransport | str | None = None,
    runtime: CompiledGraphRuntimeIdentity | None = None,
) -> CompiledGraphCapabilityDecision:
    """Return an eligible decision or fail before a beta Ray API can be imported."""
    decision = evaluate_compiled_graph_support(
        topology,
        transport,
        submission_transport=submission_transport,
        runtime=runtime,
    )
    if not decision.eligible:
        raise CompiledGraphUnsupportedError(decision)
    return decision


def candidate_compiled_graph_runtime_rows() -> tuple[dict[str, Any], ...]:
    """Expose proposed runtime rows; these are not native support evidence."""
    return tuple(
        {
            "ray_version": row.ray_version,
            "python_minor": f"{row.python_minor[0]}.{row.python_minor[1]}",
            "operating_system": row.operating_system,
            "architecture": row.architecture,
        }
        for row in sorted(
            _CANDIDATE_RUNTIMES,
            key=lambda item: (_version_sort_key(item.ray_version), item.python_minor),
        )
    )


def verified_compiled_graph_capability_rows() -> tuple[dict[str, Any], ...]:
    """Expose only exact capability tuples backed by reviewed native smoke evidence."""
    return tuple(
        {
            **capability.runtime.asdict(),
            "topology": capability.topology.value,
            "submission_transport": capability.submission_transport.value,
            "transport": capability.transport.value,
        }
        for capability in sorted(
            _VERIFIED_CAPABILITIES,
            key=lambda item: (
                _version_sort_key(item.runtime.ray_version or ""),
                item.runtime.python_version,
                item.topology.value,
                item.submission_transport.value,
                item.transport.value,
            ),
        )
    )


def _reject(
    reason: CompiledGraphReason,
    message: str,
    topology: str,
    submission_transport: str,
    transport: str,
    runtime: CompiledGraphRuntimeIdentity,
) -> CompiledGraphCapabilityDecision:
    return CompiledGraphCapabilityDecision(
        eligible=False,
        reason=reason,
        message=message,
        topology=topology,
        submission_transport=submission_transport,
        transport=transport,
        runtime=runtime,
    )


def _parse_enum(
    enum_type: type[StrEnum], value: StrEnum | str | None
) -> tuple[str, StrEnum | None]:
    text = "" if value is None else str(value)
    if len(text) > _MAX_REQUEST_DIMENSION_CHARS:
        return (
            _digest_safe_text(text, limit=_MAX_REQUEST_DIMENSION_CHARS) or "",
            None,
        )
    try:
        return text, enum_type(text)
    except ValueError:
        return text, None


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_PREFIX.match(value)
    if match is None:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or 0),
    )


def _version_sort_key(value: str) -> tuple[int, int, int]:
    return _parse_version(value) or (sys.maxsize, sys.maxsize, sys.maxsize)


def _normalize_operating_system(value: str) -> str:
    return value.strip().lower()


def _normalize_architecture(value: str) -> str:
    normalized = value.strip().lower()
    return {"amd64": "x86_64", "x64": "x86_64"}.get(normalized, normalized)


def _missing_capability_context(runtime: CompiledGraphRuntimeIdentity) -> tuple[str, ...]:
    values = {
        "python_implementation": runtime.python_implementation,
        "python_abi": runtime.python_abi,
        "dependency_profile": runtime.dependency_profile,
        "platform_profile": runtime.platform_profile,
        "libc_profile": runtime.libc_profile,
        "container_profile": runtime.container_profile,
        "deployment_profile": runtime.deployment_profile,
        "shared_memory_profile": runtime.shared_memory_profile,
        "object_store_profile": runtime.object_store_profile,
    }
    missing = [name for name, value in values.items() if not value or not value.strip()]
    missing.extend(
        name for name, value in values.items() if value and _is_unresolved_profile(value)
    )
    if runtime.container_profile and (
        runtime.container_profile.strip().casefold() in _GENERIC_CONTAINER_PROFILES
    ):
        missing.append("container_profile_specificity")
    if runtime.deployment_profile and not _IMMUTABLE_DEPLOYMENT_PROFILE.fullmatch(
        runtime.deployment_profile.strip().casefold()
    ):
        missing.append("deployment_profile_immutable")
    if not _EXACT_VERSION.match(runtime.python_version):
        missing.append("python_version_patch")
    if runtime.dependency_profile and runtime.ray_version:
        dependencies = dict(
            entry.split("=", maxsplit=1)
            for entry in runtime.dependency_profile.split(";")
            if "=" in entry
        )
        if dependencies.get("ray") != runtime.ray_version:
            missing.append("dependency_profile_ray_version")
    return tuple(missing)


def _oversized_identity_fields(runtime: CompiledGraphRuntimeIdentity) -> tuple[str, ...]:
    values = {
        "ray_version": runtime.ray_version,
        "python_version": runtime.python_version,
        "operating_system": runtime.operating_system,
        "architecture": runtime.architecture,
        "python_implementation": runtime.python_implementation,
        "python_abi": runtime.python_abi,
        "dependency_profile": runtime.dependency_profile,
        "platform_profile": runtime.platform_profile,
        "libc_profile": runtime.libc_profile,
        "container_profile": runtime.container_profile,
        "deployment_profile": runtime.deployment_profile,
        "shared_memory_profile": runtime.shared_memory_profile,
        "object_store_profile": runtime.object_store_profile,
    }
    return tuple(
        name
        for name, value in values.items()
        if value is not None and len(value) > _MAX_IDENTITY_FIELD_CHARS
    )


def _digest_safe_text(value: str | None, *, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    encoded = value.encode("utf-8", errors="surrogatepass")
    return f"<oversized:{len(value)}:sha256:{sha256(encoded).hexdigest()}>"


def _capability_set_identifier(capability: _CapabilityIdentity) -> str:
    payload = {
        **capability.runtime.asdict(),
        "topology": capability.topology.value,
        "submission_transport": capability.submission_transport.value,
        "transport": capability.transport.value,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"ray-cgraph-policy-v{COMPILED_GRAPH_POLICY_VERSION}:{sha256(encoded).hexdigest()}"


def _detect_dependency_profile() -> str:
    versions: list[str] = []
    for distribution in _PROFILE_DISTRIBUTIONS:
        try:
            version = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            version = "absent"
        versions.append(f"{distribution}={version}")
    return ";".join(versions)


def _detect_libc_profile() -> str:
    name, version = platform.libc_ver()
    return f"{name or 'unknown'}-{version or 'unknown'}"


def _detect_container_profile() -> str:
    override = os.environ.get(_CONTAINER_PROFILE_ENV, "").strip()
    if override:
        return override
    if Path("/.dockerenv").exists():
        return "docker"
    if Path("/run/.containerenv").exists():
        return "container"
    return "host"


def _detect_deployment_profile() -> str:
    return os.environ.get(_DEPLOYMENT_PROFILE_ENV, "").strip() or "unresolved"


def _detect_shared_memory_profile() -> str:
    override = os.environ.get(_SHARED_MEMORY_PROFILE_ENV, "").strip()
    if override:
        return override
    return "unresolved"


def _detect_object_store_profile() -> str:
    return os.environ.get(_OBJECT_STORE_PROFILE_ENV, "").strip() or "unresolved"


def _is_unresolved_profile(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in _UNRESOLVED_PROFILE_VALUES:
        return True
    tokens = set(re.split(r"[^a-z0-9]+", normalized))
    return bool({"unknown", "unavailable", "unresolved"}.intersection(tokens))
