"""Canonical, secret-free workflow execution-plan snapshots.

The public workflow builders intentionally remain convenient Python objects.  This
module is the immutable boundary between those definitions and an executor: it
normalizes topology and compatibility inputs, resolves per-step RuntimeEnv profiles,
and keeps the actual (potentially secret-bearing) RuntimeEnv payload in a separate
process-local execution binding.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import inspect
import json
import math
import os
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.compiled_graph import (
    CompiledGraphCapabilityDecision,
    CompiledGraphReason,
    CompiledGraphRuntimeIdentity,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    CompiledGraphTransport,
    detect_compiled_graph_runtime,
    evaluate_compiled_graph_support,
)
from django_ray.runtime.import_utils import import_callable
from django_ray.runtime.runtime_env import (
    ResolvedRuntimeEnv,
    _ray_runtime_env_default_excludes,
    normalize_runtime_env,
    resolve_runtime_env_profile,
)

if TYPE_CHECKING:
    from django_ray.runtime.context import DurableTaskContext
    from django_ray.workflows import WorkflowSignature


PLAN_FORMAT = "django-ray.workflow-plan"
PLAN_FORMAT_VERSION = 1
RUNTIME_ENV_PLAN_FORMAT = "django-ray.runtime-env-plan"
RUNTIME_ENV_PLAN_FORMAT_VERSION = 1
PLAN_SELECTION_FORMAT = "django-ray.workflow-plan-selection"
PLAN_SELECTION_FORMAT_VERSION = 1
PLAN_DOMAIN_SEPARATOR = b"django-ray.workflow-plan-v1\0"
RUNTIME_ENV_DOMAIN_SEPARATOR = b"django-ray.runtime-env-plan-v1\0"
RUNTIME_ENV_TRANSPORT_DOMAIN_SEPARATOR = b"django-ray.runtime-env-plan-transport-v1\0"
MAX_PLAN_BYTES = 64 * 1024
MAX_RUNTIME_ENV_IDENTITY_BYTES = 16 * 1024
MAX_PLAN_NODES = 64
MAX_JSON_DEPTH = 16
MAX_MAPPING_ITEMS = 256
MAX_SEQUENCE_ITEMS = 1024
MAX_STRING_CHARS = 2048
MAX_REJECTIONS = 32
MAX_RUNTIME_ENV_DIAGNOSTICS = 16
MAX_CODE_FILE_BYTES = 16 * 1024 * 1024
MAX_CODE_TREE_FILES = 2048
MAX_CODE_TREE_BYTES = 32 * 1024 * 1024
MAX_CODE_TREE_ENTRIES = 4096

_SECRET_KEY = re.compile(
    r"(?:^|[_-])(password|passwd|secret|token|credential|private[_-]?key|"
    r"api[_-]?key|access[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_RAY_GCS_PACKAGE_URI = re.compile(
    r"^gcs://_ray_pkg_(?P<digest>[0-9a-f]{16,64})(?P<extension>\.zip|\.tar\.gz)$"
)
_OCI_IMAGE_DIGEST = re.compile(
    r"^(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[0-9]+)?)"
    r"@sha256:(?P<digest>[0-9a-f]{64})$"
)
_RUNTIME_ENV_PATH = re.compile(r"^[A-Za-z0-9_.:*\[\]-]{1,512}$")
_TRUST_FIELDS = (
    "trust_domain",
    "credential_provider",
    "credential_profile",
    "credential_revision",
    "environment_revision",
    "scheduling_revision",
    "service_account_audience",
)
_RAY_TASK_OPTION_FIELDS = {
    "_generator_backpressure_num_objects",
    "_labels",
    "accelerator_type",
    "enable_task_events",
    "fallback_strategy",
    "label_selector",
    "max_retries",
    "memory",
    "name",
    "num_cpus",
    "num_gpus",
    "num_returns",
    "placement_group",
    "placement_group_bundle_index",
    "placement_group_capture_child_tasks",
    "resources",
    "retry_exceptions",
    "scheduling_strategy",
}


class WorkflowPlanValidationError(ValueError):
    """Raised before submission when a definition cannot be canonicalized safely."""


class WorkflowPlanMismatchError(RuntimeError):
    """Raised when a retry or owner presents a different effective plan."""


class _IdentityBudgetExceededError(Exception):
    """Signal that reusable identity is unavailable without blocking execution."""


@dataclass(frozen=True)
class PlanRejection:
    """One stable, bounded strategy-rejection diagnostic."""

    strategy: str
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "strategy": self.strategy,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class PlanEligibility:
    """Deterministic strategy eligibility for an effective plan."""

    eligible_strategies: tuple[str, ...]
    rejections: tuple[PlanRejection, ...]
    total_rejections: int

    def select(self, selected_strategy: str, *, requested_policy: str) -> PlanSelection:
        if selected_strategy not in self.eligible_strategies:
            raise WorkflowPlanValidationError(
                f"Execution strategy {selected_strategy!r} is not eligible for this workflow plan"
            )
        return PlanSelection(
            requested_policy=requested_policy,
            selected_strategy=selected_strategy,
            eligible_strategies=self.eligible_strategies,
            rejections=self.rejections,
            total_rejections=self.total_rejections,
        )


@dataclass(frozen=True)
class PlanSelection:
    """Bounded selection metadata stored independently from node progress."""

    requested_policy: str
    selected_strategy: str
    eligible_strategies: tuple[str, ...]
    rejections: tuple[PlanRejection, ...]
    total_rejections: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_selection_format": PLAN_SELECTION_FORMAT,
            "plan_selection_format_version": PLAN_SELECTION_FORMAT_VERSION,
            "requested_policy": self.requested_policy,
            "selected_strategy": self.selected_strategy,
            "eligible_strategies": list(self.eligible_strategies),
            "rejections": [rejection.as_dict() for rejection in self.rejections],
            "total_rejections": self.total_rejections,
            "rejections_truncated": self.total_rejections > len(self.rejections),
        }


def validate_plan_selection_manifest(value: Any) -> dict[str, Any]:
    """Validate the bounded durable selection protocol."""
    normalized = _normalize_json(value, path="plan_selection", depth=0)
    expected_fields = {
        "plan_selection_format",
        "plan_selection_format_version",
        "requested_policy",
        "selected_strategy",
        "eligible_strategies",
        "rejections",
        "total_rejections",
        "rejections_truncated",
    }
    if not isinstance(normalized, dict) or set(normalized) != expected_fields:
        raise WorkflowPlanValidationError("Workflow plan selection has an unsupported schema")
    if (
        normalized["plan_selection_format"] != PLAN_SELECTION_FORMAT
        or normalized["plan_selection_format_version"] != PLAN_SELECTION_FORMAT_VERSION
    ):
        raise WorkflowPlanValidationError(
            "Workflow plan selection has an unsupported format version"
        )
    for field in ("requested_policy", "selected_strategy"):
        if (
            not isinstance(normalized[field], str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", normalized[field]) is None
        ):
            raise WorkflowPlanValidationError(f"Workflow plan selection has an invalid {field}")
    eligible = normalized["eligible_strategies"]
    if (
        not isinstance(eligible, list)
        or not eligible
        or len(eligible) > 8
        or len(set(eligible)) != len(eligible)
        or any(
            not isinstance(strategy, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", strategy) is None
            for strategy in eligible
        )
        or normalized["selected_strategy"] not in eligible
    ):
        raise WorkflowPlanValidationError("Workflow plan selection has invalid eligible strategies")
    rejections = normalized["rejections"]
    if not isinstance(rejections, list) or len(rejections) > MAX_REJECTIONS:
        raise WorkflowPlanValidationError("Workflow plan selection has invalid rejections")
    for rejection in rejections:
        if not isinstance(rejection, dict) or set(rejection) != {
            "strategy",
            "code",
            "path",
            "message",
        }:
            raise WorkflowPlanValidationError("Workflow plan selection has invalid rejection data")
        if any(not isinstance(item, str) for item in rejection.values()):
            raise WorkflowPlanValidationError("Workflow plan selection rejection must use strings")
    total = normalized["total_rejections"]
    truncated = normalized["rejections_truncated"]
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < len(rejections)
        or not isinstance(truncated, bool)
        or truncated != (total > len(rejections))
    ):
        raise WorkflowPlanValidationError(
            "Workflow plan selection has inconsistent rejection metadata"
        )
    return normalized


@dataclass(frozen=True)
class EffectiveWorkflowPlan:
    """A deeply immutable canonical workflow plan and its identity."""

    manifest: Mapping[str, Any]
    canonical_json: str
    fingerprint: str
    eligibility: PlanEligibility

    def as_dict(self) -> dict[str, Any]:
        """Return a detached mutable JSON representation."""
        return _thaw_json(self.manifest)

    def summary(self) -> dict[str, Any]:
        """Return bounded, non-secret identity metadata for persistence and logs."""
        definition = self.manifest["definition"]
        topology = self.manifest["topology"]
        snapshot = self.manifest.get("snapshot", {})
        return {
            "plan_format": PLAN_FORMAT,
            "plan_format_version": PLAN_FORMAT_VERSION,
            "fingerprint": self.fingerprint,
            "definition_name": definition["name"],
            "definition_revision": definition["revision"],
            "topology_class": topology["class"],
            "node_count": snapshot.get("observed_node_count", len(self.manifest["nodes"])),
        }

    def cache_key(self, strategy: str) -> str:
        """Return the exact key a prepared-strategy cache must route by."""
        return f"{PLAN_FORMAT}:v{PLAN_FORMAT_VERSION}:{strategy}:{self.fingerprint}"

    @property
    def retry_safe(self) -> bool:
        """Return whether a later durable attempt can verify every environment binding."""
        retry_safety = self.manifest.get("retry_safety")
        return isinstance(retry_safety, Mapping) and retry_safety.get("retry_safe") is True

    @property
    def retry_unsafe_paths(self) -> tuple[str, ...]:
        """Return bounded, secret-free diagnostics for retry-unsafe bindings."""
        retry_safety = self.manifest.get("retry_safety")
        if not isinstance(retry_safety, Mapping):
            return ("retry_safety",)
        paths = retry_safety.get("retry_unsafe_paths")
        if not isinstance(paths, Sequence) or isinstance(paths, str | bytes | bytearray):
            return ("retry_safety",)
        normalized = tuple(path for path in paths if isinstance(path, str))
        return normalized or (() if self.retry_safe else ("retry_safety",))

    def assert_owner_fingerprint(self, owner_fingerprint: str) -> None:
        """Reject stale prepared owners before they admit an invocation."""
        if owner_fingerprint != self.fingerprint:
            raise WorkflowPlanMismatchError(
                "Prepared workflow owner is stale and must drain before accepting "
                f"plan {self.fingerprint}"
            )


@dataclass(frozen=True)
class RuntimeEnvPlanIdentity:
    """Secret-free RuntimeEnv projection used by plan identity and eligibility."""

    manifest: Mapping[str, Any]
    reusable: bool
    unresolved_paths: tuple[str, ...]
    retry_safe: bool
    retry_unsafe_paths: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return _thaw_json(self.manifest)

    def as_plan_dict(self) -> dict[str, Any]:
        """Return only semantic fields that participate in plan identity."""
        value = self.as_dict()
        value.pop("profile")
        value.pop("transport_digest")
        return value

    def as_transport_dict(self) -> dict[str, Any]:
        """Return the bounded versioned identity transported to a task worker."""
        return self.as_dict()


@dataclass(frozen=True)
class StepExecutionBinding:
    """Frozen process-local options resolved at the materialization boundary."""

    ray_options: Mapping[str, Any]
    runtime_env_profile: str | None
    runtime_env_serialized: str | None
    runtime_env_metadata: Mapping[str, Any]
    runtime_env_plan_digest: str | None
    runtime_env_trust_identity: Mapping[str, str]

    def ray_options_dict(self) -> dict[str, Any]:
        return _thaw_json(self.ray_options)


@dataclass(frozen=True)
class MaterializedWorkflowPlan:
    """An effective plan plus non-persistable per-step execution bindings."""

    plan: EffectiveWorkflowPlan
    step_bindings: Mapping[str, StepExecutionBinding]

    def binding_for_node(self, node_id: str) -> StepExecutionBinding | None:
        binding = self.step_bindings.get(node_id)
        if binding is not None:
            return binding
        template_id = re.sub(r"\.m\d+(?=\.|$)", ".m*", node_id)
        return self.step_bindings.get(template_id)

    def node_for_id(self, node_id: str) -> Mapping[str, Any] | None:
        template_id = re.sub(r"\.m\d+(?=\.|$)", ".m*", node_id)
        for node in self.plan.manifest["nodes"]:
            if node["id"] in {node_id, template_id}:
                return node
        return None


def prepare_materialized_plan_for_ray(
    materialized_plan: MaterializedWorkflowPlan,
) -> MaterializedWorkflowPlan:
    """Bind local code paths to content-addressed packages before leaf submission."""
    from django_ray.runtime.runtime_env import (
        prepare_runtime_env_for_ray_core,
        snapshot_local_runtime_env,
    )

    prepared_bindings: dict[str, StepExecutionBinding] = {}
    prepared_runtime_envs: dict[tuple[str, str, tuple[tuple[str, str], ...]], str] = {}
    for node_id, binding in materialized_plan.step_bindings.items():
        if binding.runtime_env_serialized is None:
            prepared_bindings[node_id] = binding
            continue
        cache_key = (
            binding.runtime_env_serialized,
            binding.runtime_env_plan_digest or "",
            tuple(sorted(binding.runtime_env_trust_identity.items())),
        )
        prepared_serialized = prepared_runtime_envs.get(cache_key)
        if prepared_serialized is None:
            original = normalize_runtime_env(
                json.loads(binding.runtime_env_serialized),
                profile=binding.runtime_env_profile,
                source=f"materialized workflow step {node_id} RuntimeEnv",
            )
            _assert_runtime_env_plan_digest(original, binding)
            with snapshot_local_runtime_env(original) as immutable_snapshot:
                _assert_runtime_env_plan_digest(immutable_snapshot, binding)
                prepared_spec = prepare_runtime_env_for_ray_core(immutable_snapshot)
            # Packaging reads mutable local paths. Verify the exact source snapshot
            # again before any workflow leaf is submitted; the execution binding
            # itself uses only the uploaded content-addressed URI from this point on.
            _assert_runtime_env_plan_digest(original, binding)
            prepared = normalize_runtime_env(
                prepared_spec,
                profile=binding.runtime_env_profile,
                source=f"prepared workflow step {node_id} RuntimeEnv",
            )
            prepared_serialized = prepared.serialized
            prepared_runtime_envs[cache_key] = prepared_serialized
        prepared_bindings[node_id] = StepExecutionBinding(
            ray_options=binding.ray_options,
            runtime_env_profile=binding.runtime_env_profile,
            runtime_env_serialized=prepared_serialized,
            runtime_env_metadata=binding.runtime_env_metadata,
            runtime_env_plan_digest=binding.runtime_env_plan_digest,
            runtime_env_trust_identity=binding.runtime_env_trust_identity,
        )
    return MaterializedWorkflowPlan(
        plan=materialized_plan.plan,
        step_bindings=MappingProxyType(prepared_bindings),
    )


def _assert_runtime_env_plan_digest(
    runtime_env: ResolvedRuntimeEnv,
    binding: StepExecutionBinding,
) -> None:
    expected = binding.runtime_env_plan_digest
    actual = runtime_env_plan_identity(
        runtime_env,
        trust_identity=binding.runtime_env_trust_identity,
    ).manifest["digest"]
    if expected != actual:
        raise WorkflowPlanMismatchError(
            "Workflow RuntimeEnv local content changed after plan materialization; "
            "enqueue the changed environment as a new task"
        )


@dataclass(frozen=True)
class WorkflowPlanBuildContext:
    """Explicit compatibility inputs for deterministic tests and deployments."""

    build_revision: str | None = None
    container_image_digest: str | None = None
    trust_identity: Mapping[str, str] | None = None
    compiled_graph_runtime: CompiledGraphRuntimeIdentity | None = None
    compiled_graph_topology: CompiledGraphTopology | str | None = None
    compiled_graph_submission_transport: CompiledGraphSubmissionTransport | str | None = None
    compiled_graph_settings: Mapping[str, Any] | None = None


def materialize_workflow_plan(
    signature: WorkflowSignature,
    *,
    invocation_args: Sequence[Any] = (),
    invocation_kwargs: Mapping[str, Any] | None = None,
    task_context: DurableTaskContext | None = None,
    build_context: WorkflowPlanBuildContext | None = None,
) -> MaterializedWorkflowPlan:
    """Resolve and fingerprint a workflow before any nested remote submission."""
    from django.conf import settings as django_settings

    from django_ray.conf.defaults import DEFAULTS
    from django_ray.conf.settings import get_settings

    config = get_settings() if django_settings.configured else dict(DEFAULTS)
    context = build_context or _default_build_context(config, task_context)
    trust_identity = _normalize_trust_identity(
        context.trust_identity
        if context.trust_identity is not None
        else config.get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
    )
    outer_identity = _outer_runtime_env_identity(task_context, trust_identity, config)
    builder = _PlanBuilder(
        outer_identity=outer_identity,
        trust_identity=trust_identity,
    )
    if context.build_revision is None:
        builder.unresolved_code_paths.append("definition.build_revision")
    terminals = builder.add(signature, "0", ())
    keyword_names = _normalized_keyword_names(
        invocation_kwargs or {},
        path="invocation_kwargs",
    )
    entry_ports = [f"arg:{index}" for index in range(len(invocation_args))]
    entry_ports.extend(f"kw:{name}" for name in keyword_names)
    overflow_reasons = _structural_overflow_reasons(builder, entry_ports, terminals)

    code_identities = [entry["code_identity"] for entry in builder.callable_entries]
    container_image_digest = _normalize_container_image_digest(context.container_image_digest)
    revision_payload = {
        "build_revision": context.build_revision,
        "container_image_digest": container_image_digest,
        "callables": code_identities,
    }
    definition_domain = b"django-ray.workflow-definition-v1\0"
    if overflow_reasons:
        definition_revision, _ = _streaming_json_digest_and_size(
            revision_payload,
            domain=definition_domain,
        )
    else:
        definition_revision = _domain_digest(
            definition_domain,
            _canonical_bytes(revision_payload),
        )
    first_callable = (
        builder.callable_entries[0]["import_path"] if builder.callable_entries else "anonymous"
    )
    compiled_graph_settings = _compiled_graph_settings(context.compiled_graph_settings)
    compiled_graph_runtime, deployment_digest_disagrees = _runtime_with_container_image_digest(
        context.compiled_graph_runtime or detect_compiled_graph_runtime(),
        container_image_digest,
    )
    compiled_graph_decision = evaluate_compiled_graph_support(
        context.compiled_graph_topology or "",
        CompiledGraphTransport(compiled_graph_settings["transport"]),
        submission_transport=context.compiled_graph_submission_transport,
        runtime=compiled_graph_runtime,
    )
    compiled_graph_compatibility = compiled_graph_decision.asdict()
    compiled_graph_compatibility.pop("message")
    compatibility = {
        "django_ray_plan_api": PLAN_FORMAT_VERSION,
        "django_ray": _django_ray_version(),
        "byteorder": sys.byteorder,
        "compiled_graph": compiled_graph_compatibility,
    }
    maximum_buffered_results = (
        None
        if any(bound is None for bound in builder.retained_result_bounds)
        else max((1, *builder.retained_result_bounds))
    )
    retry_safety = _effective_retry_safety(builder, outer_identity)
    manifest = {
        "plan_format": PLAN_FORMAT,
        "plan_format_version": PLAN_FORMAT_VERSION,
        "definition": {
            "name": f"workflow:{first_callable}",
            "revision": definition_revision,
            "build_revision": context.build_revision,
            "container_image_digest": container_image_digest,
        },
        "topology": {
            "class": "dynamic" if builder.has_dynamic_map else "static",
            "entry_ports": entry_ports,
            "result_ports": [f"node:{terminal}:result" for terminal in terminals],
        },
        "nodes": builder.nodes,
        "callables": builder.callable_entries,
        "edges": builder.edges,
        "physical_topology": {
            "node_model": ("ray_tasks_and_actors" if builder.result_buffer_actors else "ray_tasks"),
            "stages": [],
            "actors": builder.result_buffer_actors,
            "placement_relationships": builder.result_buffer_placements,
        },
        "capabilities": {
            "invocations": {"cardinality": "once"},
            "logical_items": {
                "cardinality": "input_bounded"
                if builder.map_limits and all(limit is not None for limit in builder.map_limits)
                else ("unbounded" if builder.has_dynamic_map else "fixed")
            },
            "admission": {
                "map_limits": builder.map_admission,
                "maximum_buffered_results": maximum_buffered_results,
            },
            "transport": {
                "kind": "ray_object_store",
                "serialization": "ray_cloudpickle",
                "ordering": "preserved",
                "zero_copy": False,
            },
            "results": {
                "cardinality": "one",
                "ownership": "outer_task",
                "retention": "until_resolved",
            },
            "effects": {"mode": "unknown"},
            "failure": {"fallback_after_submission": False},
            "cancellation": {"mode": "recursive_best_effort", "drain_required": True},
            "owner": {"kind": "outer_task", "lifetime": "durable_run", "sharing": "isolated"},
            "durability": {"boundary": "outer_task", "per_node_recovery": False},
        },
        "environments": {
            "outer": outer_identity.as_plan_dict(),
            "by_node": builder.environment_by_node,
        },
        "security": {"trust_identity": trust_identity},
        "retry_safety": retry_safety,
        "strategy_requirements": {"compiled_graph": compiled_graph_settings},
        "compatibility": compatibility,
    }
    if overflow_reasons:
        source_digest, observed_bytes = _streaming_json_digest_and_size(
            manifest,
            domain=PLAN_DOMAIN_SEPARATOR,
        )
        manifest = _overflow_manifest(
            manifest,
            builder=builder,
            reasons=overflow_reasons,
            source_digest=source_digest,
            observed_bytes=observed_bytes,
        )
        normalized = _normalize_json(manifest, path="$", depth=0)
        canonical = _canonical_json(normalized)
        canonical_bytes = canonical.encode("utf-8")
    else:
        normalized = _normalize_json(manifest, path="$", depth=0)
        canonical = _canonical_json(normalized)
        canonical_bytes = canonical.encode("utf-8")
        if len(canonical_bytes) > MAX_PLAN_BYTES:
            overflow_reasons = ("byte_limit",)
            manifest = _overflow_manifest(
                manifest,
                builder=builder,
                reasons=overflow_reasons,
                source_digest=_domain_digest(PLAN_DOMAIN_SEPARATOR, canonical_bytes),
                observed_bytes=len(canonical_bytes),
            )
            normalized = _normalize_json(manifest, path="$", depth=0)
            canonical = _canonical_json(normalized)
            canonical_bytes = canonical.encode("utf-8")
    if len(canonical_bytes) > MAX_PLAN_BYTES:
        raise WorkflowPlanValidationError(
            "Bounded workflow overflow snapshot exceeded its canonical byte limit"
        )
    fingerprint = f"sha256:{hashlib.sha256(PLAN_DOMAIN_SEPARATOR + canonical_bytes).hexdigest()}"
    eligibility = _build_eligibility(
        builder,
        outer_identity,
        compiled_graph_decision,
        deployment_digest_disagrees=deployment_digest_disagrees,
        snapshot_overflow_reasons=overflow_reasons,
    )
    plan = EffectiveWorkflowPlan(
        manifest=_freeze_json(normalized),
        canonical_json=canonical,
        fingerprint=fingerprint,
        eligibility=eligibility,
    )
    return MaterializedWorkflowPlan(
        plan=plan,
        step_bindings=MappingProxyType(dict(builder.bindings)),
    )


def _structural_overflow_reasons(
    builder: _PlanBuilder,
    entry_ports: Sequence[str],
    terminals: Sequence[str],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if len(builder.nodes) > MAX_PLAN_NODES:
        reasons.append("node_limit")
    if (
        len(entry_ports) > MAX_SEQUENCE_ITEMS
        or len(builder.edges) > MAX_SEQUENCE_ITEMS
        or len(builder.callable_entries) > MAX_SEQUENCE_ITEMS
        or len(builder.map_admission) > MAX_SEQUENCE_ITEMS
        or len(builder.environment_by_node) > MAX_MAPPING_ITEMS
    ):
        reasons.append("schema_limit")
    generated_identifiers = [
        *(str(node["id"]) for node in builder.nodes),
        *(str(edge["source"]) for edge in builder.edges),
        *(str(edge["target"]) for edge in builder.edges),
        *(f"node:{terminal}:result" for terminal in terminals),
    ]
    if any(len(identifier) > MAX_STRING_CHARS for identifier in generated_identifiers):
        reasons.append("identifier_limit")
    return tuple(reasons)


def _effective_retry_safety(
    builder: _PlanBuilder,
    outer_identity: RuntimeEnvPlanIdentity,
) -> dict[str, Any]:
    """Aggregate bounded RuntimeEnv retry diagnostics across the effective plan."""
    paths = [f"environments.outer.{path}" for path in outer_identity.retry_unsafe_paths]
    paths.extend(builder.retry_unsafe_env_paths)
    total = int(outer_identity.manifest["total_retry_unsafe_paths"])
    total += builder.total_retry_unsafe_env_paths
    safe_paths = sorted({_safe_runtime_env_diagnostic_path(path) for path in paths})
    retained_paths = safe_paths[:MAX_RUNTIME_ENV_DIAGNOSTICS]
    return {
        "retry_safe": total == 0,
        "retry_unsafe_paths": retained_paths,
        "total_retry_unsafe_paths": total,
        "retry_unsafe_paths_truncated": total > len(retained_paths),
    }


def _overflow_manifest(
    full_manifest: Mapping[str, Any],
    *,
    builder: _PlanBuilder,
    reasons: Sequence[str],
    source_digest: str,
    observed_bytes: int,
) -> dict[str, Any]:
    """Return a bounded identity-bearing sentinel for dynamic-only execution."""
    full_capabilities = full_manifest["capabilities"]
    return {
        "plan_format": PLAN_FORMAT,
        "plan_format_version": PLAN_FORMAT_VERSION,
        "definition": full_manifest["definition"],
        "topology": {
            "class": full_manifest["topology"]["class"],
            "entry_ports": [],
            "result_ports": [],
            "entry_port_count": len(full_manifest["topology"]["entry_ports"]),
            "result_port_count": len(full_manifest["topology"]["result_ports"]),
        },
        "nodes": [],
        "callables": [],
        "edges": [],
        "physical_topology": _overflow_physical_topology(full_manifest["physical_topology"]),
        "capabilities": {
            "invocations": full_capabilities["invocations"],
            "logical_items": full_capabilities["logical_items"],
            "admission": {
                "map_count": len(builder.map_admission),
                "all_maps_bounded": bool(builder.map_limits)
                and all(limit is not None for limit in builder.map_limits),
                "maximum_buffered_results": full_capabilities["admission"][
                    "maximum_buffered_results"
                ],
            },
            "transport": full_capabilities["transport"],
            "results": full_capabilities["results"],
            "effects": full_capabilities["effects"],
            "failure": full_capabilities["failure"],
            "cancellation": full_capabilities["cancellation"],
            "owner": full_capabilities["owner"],
            "durability": full_capabilities["durability"],
        },
        "environments": {
            "outer": full_manifest["environments"]["outer"],
            "by_node": {},
        },
        "security": full_manifest["security"],
        "retry_safety": full_manifest["retry_safety"],
        "strategy_requirements": full_manifest["strategy_requirements"],
        "compatibility": full_manifest["compatibility"],
        "snapshot": {
            "state": "overflow",
            "reasons": list(reasons),
            "source_digest": source_digest,
            "observed_node_count": len(builder.nodes),
            "observed_edge_count": len(builder.edges),
            "observed_callable_count": len(builder.callable_entries),
            "observed_canonical_bytes": observed_bytes,
        },
    }


def _overflow_physical_topology(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Compact physical details while the full source digest retains their identity."""
    stages = value["stages"]
    actors = value["actors"]
    relationships = value["placement_relationships"]
    if not stages and not actors and not relationships:
        # Preserve byte-for-byte compatibility for pre-buffer overflow plans.
        return value
    result_buffer_actors = sum(
        1
        for actor in actors
        if isinstance(actor, Mapping) and actor.get("kind") == "ordered_map_result_buffer"
    )
    return {
        "node_model": value["node_model"],
        "stages": [],
        "actors": [],
        "placement_relationships": [],
        "overflow_summary": {
            "stage_count": len(stages),
            "actor_count": len(actors),
            "result_buffer_actor_count": result_buffer_actors,
            "placement_relationship_count": len(relationships),
            "details_omitted": True,
        },
    }


def _streaming_json_digest_and_size(
    value: Any,
    *,
    domain: bytes,
) -> tuple[str, int]:
    """Digest a large secret-free JSON value without materializing its encoding."""
    digest = hashlib.sha256(domain)
    size = 0
    normalized = _normalize_overflow_json(value, path="$", depth=0)
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    try:
        for chunk in encoder.iterencode(normalized):
            encoded = chunk.encode("utf-8")
            digest.update(encoded)
            size += len(encoded)
    except (TypeError, ValueError, RecursionError) as error:
        raise WorkflowPlanValidationError(
            "Workflow overflow identity could not be canonicalized safely"
        ) from error
    return f"sha256:{digest.hexdigest()}", size


def _normalize_overflow_json(value: Any, *, path: str, depth: int) -> Any:
    """Apply canonical JSON semantics without detailed-snapshot collection caps."""
    if depth > MAX_JSON_DEPTH:
        raise WorkflowPlanValidationError(
            f"Workflow overflow identity exceeds maximum nesting depth {MAX_JSON_DEPTH}"
        )
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowPlanValidationError(
                f"Workflow overflow identity contains a non-finite number at {path}"
            )
        return _normalize_number(value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise WorkflowPlanValidationError(
                    f"Workflow overflow identity mapping key at {path} must be a string"
                )
            key = unicodedata.normalize("NFC", raw_key)
            if key in result:
                raise WorkflowPlanValidationError(
                    f"Workflow overflow identity has duplicate normalized key at {path}"
                )
            result[key] = _normalize_overflow_json(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
            )
        return result
    if isinstance(value, list | tuple):
        return [
            _normalize_overflow_json(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )
            for index, item in enumerate(value)
        ]
    raise WorkflowPlanValidationError(
        "Workflow overflow identity contains an unsupported value at " + path
    )


def runtime_env_plan_identity(
    runtime_env: ResolvedRuntimeEnv,
    *,
    trust_identity: Mapping[str, str] | None = None,
) -> RuntimeEnvPlanIdentity:
    """Build a bounded secret-free identity without hashing credential values."""
    trust = _normalize_trust_identity(trust_identity or {})
    projection, unresolved = _runtime_env_projection(runtime_env.spec, trust)
    retry_unsafe = unresolved - _retry_safe_runtime_env_paths(projection)
    identity_payload = {
        "plan_format": RUNTIME_ENV_PLAN_FORMAT,
        "plan_format_version": RUNTIME_ENV_PLAN_FORMAT_VERSION,
        "spec": projection,
        "trust_identity": trust,
    }
    canonical = _canonical_bytes(identity_payload)
    digest = _domain_digest(RUNTIME_ENV_DOMAIN_SEPARATOR, canonical)
    unresolved_paths = sorted(unresolved)
    safe_paths = sorted({_safe_runtime_env_diagnostic_path(path) for path in unresolved_paths})
    retained_paths = safe_paths[:MAX_RUNTIME_ENV_DIAGNOSTICS]
    retry_unsafe_paths = sorted(retry_unsafe)
    safe_retry_unsafe_paths = sorted(
        {_safe_runtime_env_diagnostic_path(path) for path in retry_unsafe_paths}
    )
    retained_retry_unsafe_paths = safe_retry_unsafe_paths[:MAX_RUNTIME_ENV_DIAGNOSTICS]
    manifest_without_transport_digest = {
        "plan_format": RUNTIME_ENV_PLAN_FORMAT,
        "plan_format_version": RUNTIME_ENV_PLAN_FORMAT_VERSION,
        "profile": runtime_env.profile,
        "digest": digest,
        "reusable": not unresolved,
        "unresolved_paths": retained_paths,
        "total_unresolved_paths": len(unresolved_paths),
        "unresolved_paths_truncated": len(unresolved_paths) > len(retained_paths),
        "retry_safe": not retry_unsafe,
        "retry_unsafe_paths": retained_retry_unsafe_paths,
        "total_retry_unsafe_paths": len(retry_unsafe_paths),
        "retry_unsafe_paths_truncated": (
            len(retry_unsafe_paths) > len(retained_retry_unsafe_paths)
        ),
        "trust_digest": _domain_digest(
            b"django-ray.workflow-plan-trust-v1\0",
            _canonical_bytes(trust),
        ),
    }
    transport_digest = _domain_digest(
        RUNTIME_ENV_TRANSPORT_DOMAIN_SEPARATOR,
        _canonical_bytes(manifest_without_transport_digest),
    )
    manifest = {
        **manifest_without_transport_digest,
        "transport_digest": transport_digest,
    }
    normalized = _normalize_json(manifest, path="runtime_env", depth=0)
    serialized = _canonical_bytes(normalized)
    if len(serialized) > MAX_RUNTIME_ENV_IDENTITY_BYTES:
        raise WorkflowPlanValidationError(
            "Secret-free RuntimeEnv plan identity exceeds its "
            f"{MAX_RUNTIME_ENV_IDENTITY_BYTES}-byte transport limit"
        )
    return RuntimeEnvPlanIdentity(
        manifest=_freeze_json(normalized),
        reusable=not unresolved,
        unresolved_paths=tuple(retained_paths),
        retry_safe=not retry_unsafe,
        retry_unsafe_paths=tuple(retained_retry_unsafe_paths),
    )


def _safe_runtime_env_diagnostic_path(path: str) -> str:
    if _RUNTIME_ENV_PATH.fullmatch(path) is not None:
        return path
    return "runtime_env.unsupported_field"


def _retry_safe_runtime_env_paths(projection: Mapping[str, Any]) -> set[str]:
    """Return unresolved paths that still carry a verifiable immutable identity."""
    result: set[str] = set()
    has_local_code_snapshot = False

    def collect(value: Any, path: str) -> None:
        nonlocal has_local_code_snapshot
        if isinstance(value, Mapping):
            if value.get("kind") in {"local_file_snapshot", "local_tree_snapshot"} and re.fullmatch(
                r"[0-9a-f]{64}", str(value.get("sha256", ""))
            ):
                result.add(path)
                if path == "spec.working_dir" or re.fullmatch(
                    r"spec\.py_modules\.\d+",
                    path,
                ):
                    has_local_code_snapshot = True
                return
            ray_package = value.get("identity") if value.get("kind") == "uri" else value
            if isinstance(ray_package, Mapping) and ray_package.get("kind") == "ray_gcs_package":
                digest = str(ray_package.get("package_digest", ""))
                extension = ray_package.get("package_extension")
                if re.fullmatch(r"[0-9a-f]{16,64}", digest) and extension in {"zip", "tar.gz"}:
                    result.add(path)
                    return
            for key, item in value.items():
                collect(item, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, f"{path}.{index}")

    collect(projection, "spec")
    if has_local_code_snapshot and "excludes" in projection:
        # The local content identity is computed after applying Ray's exclusion
        # semantics and verified again after snapshotting.  It therefore covers
        # the effect of these deliberately redacted patterns without making
        # excludes attached only to opaque code locations retry-safe.
        result.add("spec.excludes")
    return result


def runtime_env_plan_identity_from_transport(
    value: Mapping[str, Any],
    *,
    trust_identity: Mapping[str, str] | None = None,
) -> RuntimeEnvPlanIdentity:
    """Strictly reconstruct a transported identity at the worker boundary."""
    if not isinstance(value, Mapping):
        raise WorkflowPlanValidationError("Transported RuntimeEnv identity must be a mapping")
    normalized = _normalize_json(value, path="runtime_env", depth=0)
    expected_fields = {
        "plan_format",
        "plan_format_version",
        "profile",
        "digest",
        "reusable",
        "unresolved_paths",
        "total_unresolved_paths",
        "unresolved_paths_truncated",
        "retry_safe",
        "retry_unsafe_paths",
        "total_retry_unsafe_paths",
        "retry_unsafe_paths_truncated",
        "trust_digest",
        "transport_digest",
    }
    if set(normalized) != expected_fields:
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has an unsupported schema"
        )
    serialized = _canonical_bytes(normalized)
    if len(serialized) > MAX_RUNTIME_ENV_IDENTITY_BYTES:
        raise WorkflowPlanValidationError("Transported RuntimeEnv identity exceeds its byte limit")
    if (
        normalized["plan_format"] != RUNTIME_ENV_PLAN_FORMAT
        or normalized["plan_format_version"] != RUNTIME_ENV_PLAN_FORMAT_VERSION
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has an unsupported format version"
        )
    profile = normalized["profile"]
    if profile is not None and (
        not isinstance(profile, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", profile) is None
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has an invalid profile name"
        )
    for field in ("digest", "trust_digest", "transport_digest"):
        if (
            not isinstance(normalized[field], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", normalized[field]) is None
        ):
            raise WorkflowPlanValidationError(
                f"Transported RuntimeEnv identity has an invalid {field}"
            )
    paths = normalized["unresolved_paths"]
    if (
        not isinstance(paths, list)
        or len(paths) > MAX_RUNTIME_ENV_DIAGNOSTICS
        or any(
            not isinstance(path, str) or _RUNTIME_ENV_PATH.fullmatch(path) is None for path in paths
        )
        or paths != sorted(set(paths))
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has invalid unresolved paths"
        )
    total = normalized["total_unresolved_paths"]
    truncated = normalized["unresolved_paths_truncated"]
    reusable = normalized["reusable"]
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < len(paths)
        or not isinstance(truncated, bool)
        or truncated != (total > len(paths))
        or not isinstance(reusable, bool)
        or reusable != (total == 0)
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has inconsistent eligibility metadata"
        )
    retry_paths = normalized["retry_unsafe_paths"]
    if (
        not isinstance(retry_paths, list)
        or len(retry_paths) > MAX_RUNTIME_ENV_DIAGNOSTICS
        or any(
            not isinstance(path, str) or _RUNTIME_ENV_PATH.fullmatch(path) is None
            for path in retry_paths
        )
        or retry_paths != sorted(set(retry_paths))
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has invalid retry-unsafe paths"
        )
    retry_total = normalized["total_retry_unsafe_paths"]
    retry_truncated = normalized["retry_unsafe_paths_truncated"]
    retry_safe = normalized["retry_safe"]
    if (
        isinstance(retry_total, bool)
        or not isinstance(retry_total, int)
        or retry_total < len(retry_paths)
        or not isinstance(retry_truncated, bool)
        or retry_truncated != (retry_total > len(retry_paths))
        or not isinstance(retry_safe, bool)
        or retry_safe != (retry_total == 0)
        or retry_total > total
        or (reusable and not retry_safe)
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has inconsistent retry-safety metadata"
        )
    trust = _normalize_trust_identity(trust_identity or {})
    expected_trust_digest = _domain_digest(
        b"django-ray.workflow-plan-trust-v1\0",
        _canonical_bytes(trust),
    )
    if normalized["trust_digest"] != expected_trust_digest:
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity does not match the worker trust identity"
        )
    payload = dict(normalized)
    transported_digest = payload.pop("transport_digest")
    expected_transport_digest = _domain_digest(
        RUNTIME_ENV_TRANSPORT_DOMAIN_SEPARATOR,
        _canonical_bytes(payload),
    )
    if transported_digest != expected_transport_digest:
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity checksum does not match its payload"
        )
    return RuntimeEnvPlanIdentity(
        manifest=_freeze_json(normalized),
        reusable=reusable,
        unresolved_paths=tuple(paths),
        retry_safe=retry_safe,
        retry_unsafe_paths=tuple(retry_paths),
    )


def plan_requires_drain(
    prepared_fingerprint: str | None,
    replacement: EffectiveWorkflowPlan,
) -> bool:
    """Return whether prepared actors/graphs must drain before replacement."""
    return prepared_fingerprint is not None and prepared_fingerprint != replacement.fingerprint


class _PlanBuilder:
    def __init__(
        self,
        *,
        outer_identity: RuntimeEnvPlanIdentity,
        trust_identity: Mapping[str, str],
    ) -> None:
        self.outer_identity = outer_identity
        self.trust_identity = trust_identity
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, str]] = []
        self.bindings: dict[str, StepExecutionBinding] = {}
        self.environment_by_node: dict[str, Any] = {}
        self.map_limits: list[int | None] = []
        self.map_admission: list[dict[str, Any]] = []
        self.retained_result_bounds: list[int | None] = []
        self.result_buffer_actors: list[dict[str, Any]] = []
        self.result_buffer_placements: list[dict[str, Any]] = []
        self.has_dynamic_map = False
        self.unresolved_code_paths: list[str] = []
        self.unresolved_env_paths: list[str] = list(outer_identity.unresolved_paths)
        self.retry_unsafe_env_paths: list[str] = []
        self.total_retry_unsafe_env_paths = 0
        self.unresolved_option_paths: list[str] = []
        self.callable_identities: dict[str, dict[str, Any]] = {}
        self.module_identities: dict[str, dict[str, Any]] = {}
        self.callable_references: dict[str, str] = {}
        self.callable_entries: list[dict[str, Any]] = []
        self.runtime_env_identities: dict[
            tuple[str, str], tuple[ResolvedRuntimeEnv, RuntimeEnvPlanIdentity]
        ] = {}

    def add(
        self,
        signature: WorkflowSignature,
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Add a definition without consuming the Python recursion limit."""
        from django_ray.workflows import Chain, Group, Map, Step

        next_result_id = 1
        results: dict[int, tuple[str, ...]] = {}
        # Work entries are tuples whose first item is one of the bounded set of
        # tags below. Keeping the traversal state on the heap means a deeply
        # nested (but otherwise valid) definition produces a controlled plan
        # result instead of leaking RecursionError from the compiler boundary.
        work: list[tuple[Any, ...]] = [("visit", signature, node_id, dependencies, 0, 0)]
        while work:
            entry = work.pop()
            tag = entry[0]
            if tag == "visit":
                (
                    _,
                    current,
                    current_id,
                    current_dependencies,
                    result_id,
                    dynamic_map_depth,
                ) = entry
                if isinstance(current, Step):
                    results[result_id] = self._add_step(
                        current,
                        current_id,
                        current_dependencies,
                    )
                    continue
                if isinstance(current, Chain):
                    work.append(
                        (
                            "chain_next",
                            current,
                            current_id,
                            0,
                            current_dependencies,
                            result_id,
                            dynamic_map_depth,
                        )
                    )
                    continue
                if isinstance(current, Group):
                    child_result_ids = tuple(
                        range(next_result_id, next_result_id + len(current.signatures))
                    )
                    next_result_id += len(child_result_ids)
                    work.append(("group_finish", current_id, child_result_ids, result_id))
                    for index in reversed(range(len(current.signatures))):
                        work.append(
                            (
                                "visit",
                                current.signatures[index],
                                f"{current_id}.g{index}",
                                current_dependencies,
                                child_result_ids[index],
                                dynamic_map_depth,
                            )
                        )
                    continue
                if isinstance(current, Map):
                    if current.result_buffer is not None and dynamic_map_depth:
                        raise WorkflowPlanValidationError(
                            "Result-buffer maps cannot be nested inside a dynamic map in v1"
                        )
                    self._add_map_template(current, current_id, current_dependencies)
                    body_result_id = next_result_id
                    next_result_id += 1
                    work.append(
                        (
                            "map_finish",
                            current_id,
                            body_result_id,
                            result_id,
                            current.result_buffer is not None,
                        )
                    )
                    work.append(
                        (
                            "visit",
                            current.signature,
                            f"{current_id}.m*",
                            (current_id,),
                            body_result_id,
                            dynamic_map_depth + 1,
                        )
                    )
                    continue
                raise WorkflowPlanValidationError(
                    "Unsupported workflow signature type "
                    f"{type(current).__module__}.{type(current).__name__}"
                )
            if tag == "chain_next":
                (
                    _,
                    current,
                    current_id,
                    index,
                    current_dependencies,
                    result_id,
                    dynamic_map_depth,
                ) = entry
                if index == len(current.signatures):
                    results[result_id] = current_dependencies
                    continue
                child_result_id = next_result_id
                next_result_id += 1
                work.append(
                    (
                        "chain_resume",
                        current,
                        current_id,
                        index,
                        child_result_id,
                        result_id,
                        dynamic_map_depth,
                    )
                )
                work.append(
                    (
                        "visit",
                        current.signatures[index],
                        f"{current_id}.{index}",
                        current_dependencies,
                        child_result_id,
                        dynamic_map_depth,
                    )
                )
                continue
            if tag == "chain_resume":
                (
                    _,
                    current,
                    current_id,
                    index,
                    child_result_id,
                    result_id,
                    dynamic_map_depth,
                ) = entry
                work.append(
                    (
                        "chain_next",
                        current,
                        current_id,
                        index + 1,
                        results.pop(child_result_id),
                        result_id,
                        dynamic_map_depth,
                    )
                )
                continue
            if tag == "group_finish":
                _, current_id, child_result_ids, result_id = entry
                terminals = [
                    terminal
                    for child_result_id in child_result_ids
                    for terminal in results.pop(child_result_id)
                ]
                collect_id = f"{current_id}.collect"
                self.nodes.append(
                    {
                        "id": collect_id,
                        "operation": "ordered_collect",
                        "node_model": "task",
                        "inputs": [f"node:{terminal}:result" for terminal in terminals],
                        "outputs": ["result"],
                        "resources": {},
                        "scheduling": {},
                        "actor_layout": None,
                    }
                )
                self._add_edges(terminals, collect_id)
                self.retained_result_bounds.append(len(terminals))
                results[result_id] = (collect_id,)
                continue
            if tag == "map_finish":
                _, current_id, body_result_id, result_id, uses_result_buffer = entry
                body_terminals = results.pop(body_result_id)
                result_node_id = f"{current_id}.result"
                actor_id = f"{current_id}.result_buffer" if uses_result_buffer else None
                self.nodes.append(
                    {
                        "id": result_node_id,
                        "operation": (
                            "ordered_actor_finalize"
                            if uses_result_buffer
                            else "ordered_dynamic_collect"
                        ),
                        "node_model": "actor" if uses_result_buffer else "owner",
                        "inputs": [f"node:{terminal}:result" for terminal in body_terminals],
                        "outputs": ["result"],
                        "resources": {},
                        "scheduling": {},
                        "actor_layout": actor_id,
                    }
                )
                self._add_edges(body_terminals, result_node_id)
                results[result_id] = (result_node_id,)
                continue
            raise AssertionError(f"Unknown workflow-plan traversal entry {tag!r}")
        return results[0]

    def _add_map_template(
        self,
        signature: Any,
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> None:
        cancel_timeout_seconds = signature.cancel_timeout_seconds
        if isinstance(cancel_timeout_seconds, float):
            cancel_timeout_seconds = _normalize_number(cancel_timeout_seconds)
        result_buffer_contract = None
        result_buffer_actor_id = None
        if signature.result_buffer is not None:
            from django_ray.runtime.result_buffer import result_buffer_plan_contract

            if signature.max_items is None or signature.max_concurrency is None:
                raise WorkflowPlanValidationError(
                    "Result-buffer maps require positive max_items and max_concurrency"
                )
            result_buffer_contract = result_buffer_plan_contract(
                max_items=signature.max_items,
                max_concurrency=signature.max_concurrency,
                max_serialized_bytes=signature.result_buffer.max_serialized_bytes,
                actor_options=_thaw_json(signature.result_buffer.actor_options),
            )
            result_buffer_actor_id = f"{node_id}.result_buffer"
            actor_options = result_buffer_contract["actor"]
            self.result_buffer_actors.append(
                {
                    "id": result_buffer_actor_id,
                    "kind": "ordered_map_result_buffer",
                    "cardinality": "one_per_workflow_invocation",
                    "resources": {
                        "num_cpus": actor_options["num_cpus"],
                        "memory": actor_options["memory"],
                        "custom": actor_options["resources"],
                    },
                    "contract": result_buffer_contract,
                }
            )
            self.result_buffer_placements.append(
                {
                    "source": node_id,
                    "target": result_buffer_actor_id,
                    "relationship": "owns_non_detached_actor",
                    "placement": result_buffer_contract["placement"],
                }
            )
        self.has_dynamic_map = True
        self.map_limits.append(signature.max_items)
        self.retained_result_bounds.append(signature.max_items)
        self.map_admission.append(
            {
                "node_id": node_id,
                "maximum_items": signature.max_items,
                "maximum_in_flight": signature.max_concurrency,
                "cancel_timeout_seconds": cancel_timeout_seconds,
            }
        )
        map_node = {
            "id": node_id,
            "operation": "dynamic_map",
            "node_model": "task_template",
            "inputs": [f"node:{dependency}:result" for dependency in dependencies]
            or ["invocation:arg:0"],
            "outputs": ["result"],
            "bounds": {
                "maximum_items": signature.max_items,
                "maximum_in_flight": signature.max_concurrency,
                "cancel_timeout_seconds": cancel_timeout_seconds,
            },
            "actor_layout": result_buffer_actor_id,
        }
        self.nodes.append(map_node)
        self._add_edges(dependencies, node_id)

    def _add_step(
        self,
        signature: Any,
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> tuple[str, ...]:
        callable_identity = self.callable_identities.get(signature.callable_path)
        if callable_identity is None:
            callable_identity = _callable_code_identity(
                signature.callable_path,
                module_cache=self.module_identities,
            )
            self.callable_identities[signature.callable_path] = callable_identity
            callable_ref = f"callable:{len(self.callable_entries)}"
            self.callable_references[signature.callable_path] = callable_ref
            self.callable_entries.append(
                {
                    "id": callable_ref,
                    "import_path": signature.callable_path,
                    "kind": callable_identity["kind"],
                    "code_identity": callable_identity,
                }
            )
        else:
            callable_ref = self.callable_references[signature.callable_path]
        if not callable_identity["stable"]:
            self.unresolved_code_paths.append(f"callables.{callable_ref}.code_identity")
        ray_options, unresolved_options = _normalize_ray_options(
            signature.ray_options,
            node_id=node_id,
            trust_identity=self.trust_identity,
        )
        self.unresolved_option_paths.extend(unresolved_options)
        runtime_env, runtime_identity, mode = self._resolve_step_runtime_env(signature, node_id)
        plan_runtime_metadata = {
            "mode": mode,
            "hash": runtime_identity.manifest["digest"],
        }
        binding_runtime_metadata = {
            **plan_runtime_metadata,
            "profile": runtime_identity.manifest["profile"],
        }
        bound_keyword_names = _normalized_keyword_names(
            signature.bound_kwargs,
            path=f"nodes.{node_id}.bound_kwargs",
        )
        node = {
            "id": node_id,
            "operation": "call",
            "node_model": "task",
            "callable": {"ref": callable_ref},
            "inputs": [f"node:{dependency}:result" for dependency in dependencies]
            or ["invocation:root"],
            "binding_schema": {
                "bound_positional_count": len(signature.bound_args),
                "bound_keyword_names": bound_keyword_names,
                "keyword_precedence": "bound_over_invocation",
            },
            "outputs": ["result"],
            "bootstrap_django": bool(signature.bootstrap_django),
            "resources": _resource_options(ray_options),
            "scheduling": _scheduling_options(ray_options),
            "ray_options": ray_options,
            "environment": plan_runtime_metadata,
            "actor_layout": None,
        }
        self.nodes.append(node)
        self._add_edges(dependencies, node_id)
        self.environment_by_node[node_id] = dict(plan_runtime_metadata)
        if not runtime_identity.reusable:
            self.unresolved_env_paths.extend(
                f"environments.by_node.{node_id}.{path}"
                for path in runtime_identity.unresolved_paths
            )
        if mode == "override" and not runtime_identity.retry_safe:
            self.retry_unsafe_env_paths.extend(
                f"environments.by_node.{node_id}.{path}"
                for path in runtime_identity.retry_unsafe_paths
            )
            self.total_retry_unsafe_env_paths += int(
                runtime_identity.manifest["total_retry_unsafe_paths"]
            )
        self.bindings[node_id] = StepExecutionBinding(
            ray_options=signature.ray_options,
            runtime_env_profile=runtime_env.profile if runtime_env is not None else None,
            runtime_env_serialized=(runtime_env.serialized if runtime_env is not None else None),
            runtime_env_metadata=_freeze_json(binding_runtime_metadata),
            runtime_env_plan_digest=(
                str(runtime_identity.manifest["digest"]) if runtime_env is not None else None
            ),
            runtime_env_trust_identity=_freeze_json(self.trust_identity),
        )
        return (node_id,)

    def _resolve_step_runtime_env(
        self,
        signature: Any,
        node_id: str,
    ) -> tuple[ResolvedRuntimeEnv | None, RuntimeEnvPlanIdentity, str]:
        runtime_env_value = signature.runtime_env
        if runtime_env_value is None:
            return None, self.outer_identity, "inherit"
        if isinstance(runtime_env_value, str):
            cache_key = ("profile", runtime_env_value)
            cached = self.runtime_env_identities.get(cache_key)
            if cached is not None:
                return cached[0], cached[1], "override"
            resolved = resolve_runtime_env_profile(runtime_env_value)
        elif isinstance(runtime_env_value, Mapping):
            thawed = _thaw_json(runtime_env_value)
            serialized = json.dumps(
                thawed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            cache_key = ("inline", serialized)
            cached = self.runtime_env_identities.get(cache_key)
            if cached is not None:
                return cached[0], cached[1], "override"
            resolved = normalize_runtime_env(
                thawed,
                source=f"workflow step {node_id} RuntimeEnv",
            )
        else:
            raise WorkflowPlanValidationError(
                f"nodes.{node_id}.runtime_env must be a profile name or mapping"
            )
        identity = runtime_env_plan_identity(
            resolved,
            trust_identity=self.trust_identity,
        )
        self.runtime_env_identities[cache_key] = (resolved, identity)
        return resolved, identity, "override"

    def _add_edges(self, dependencies: Sequence[str], target: str) -> None:
        self.edges.extend(
            {"source": f"{dependency}:result", "target": f"{target}:input"}
            for dependency in dependencies
        )


def _build_eligibility(
    builder: _PlanBuilder,
    outer_identity: RuntimeEnvPlanIdentity,
    compiled_graph_decision: CompiledGraphCapabilityDecision,
    *,
    deployment_digest_disagrees: bool = False,
    snapshot_overflow_reasons: Sequence[str] = (),
) -> PlanEligibility:
    rejections: list[PlanRejection] = []
    if snapshot_overflow_reasons:
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "PLAN_SNAPSHOT_OVERFLOW",
                "snapshot",
                "effective plan exceeded bounded snapshot limits: "
                + ", ".join(snapshot_overflow_reasons),
            )
        )
    if builder.has_dynamic_map:
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "DYNAMIC_TOPOLOGY",
                "topology.class",
                "runtime map inputs create task instances",
            )
        )
    if any(bound is None for bound in builder.retained_result_bounds):
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "UNBOUNDED_ADMISSION",
                "capabilities.admission.maximum_buffered_results",
                "workflow result retention has no finite item bound",
            )
        )
    rejections.append(
        PlanRejection(
            "compiled_graph",
            "UNSUPPORTED_NODE_MODEL",
            "physical_topology.node_model",
            "current workflow leaves are Ray tasks, not dedicated actors",
        )
    )
    rejections.append(
        PlanRejection(
            "static_actors",
            "UNSUPPORTED_NODE_MODEL",
            "physical_topology.node_model",
            "no static actor layout is declared",
        )
    )
    unresolved_code_paths = (
        ["snapshot.omitted_nodes"]
        if snapshot_overflow_reasons and builder.unresolved_code_paths
        else sorted(set(builder.unresolved_code_paths))
    )
    for path in unresolved_code_paths:
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "UNRESOLVED_CODE_IDENTITY",
                path,
                "callable code has no stable content identity",
            )
        )
    unresolved_option_paths = (
        ["snapshot.omitted_nodes"]
        if snapshot_overflow_reasons and builder.unresolved_option_paths
        else sorted(set(builder.unresolved_option_paths))
    )
    for path in unresolved_option_paths:
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "UNRESOLVED_PLAN_OPTION",
                path,
                "Ray task option is valid for dynamic execution but has no reusable identity",
            )
        )
    unresolved_env = list(builder.unresolved_env_paths)
    if not outer_identity.reusable:
        unresolved_env.extend(
            f"environments.outer.{path}" for path in outer_identity.unresolved_paths
        )
    unresolved_env_paths = (
        ["snapshot.omitted_environments"]
        if snapshot_overflow_reasons and unresolved_env
        else sorted(set(unresolved_env))
    )
    for path in unresolved_env_paths:
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "UNRESOLVED_RUNTIME_ENV",
                path,
                "RuntimeEnv field has no non-secret reusable identity",
            )
        )
    compiled_rejection = _compiled_graph_plan_rejection(compiled_graph_decision)
    if compiled_rejection is not None:
        rejections.append(compiled_rejection)
    if deployment_digest_disagrees:
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "INCOMPATIBLE_PLATFORM",
                "compatibility.compiled_graph.runtime.deployment_profile",
                "container image digest disagrees with the detected deployment profile",
            )
        )
    all_ordered = sorted(rejections, key=lambda item: (item.strategy, item.code, item.path))
    ordered = tuple(all_ordered[:MAX_REJECTIONS])
    return PlanEligibility(
        eligible_strategies=("dynamic_tasks", "local"),
        rejections=ordered,
        total_rejections=len(all_ordered),
    )


def _compiled_graph_plan_rejection(
    decision: CompiledGraphCapabilityDecision,
) -> PlanRejection | None:
    code = decision.plan_rejection_code
    if code is None:
        return None
    reason_paths = {
        CompiledGraphReason.RAY_NOT_INSTALLED: "runtime.ray_version",
        CompiledGraphReason.UNSUPPORTED_RAY_VERSION: "runtime.ray_version",
        CompiledGraphReason.UNSUPPORTED_PYTHON: "runtime.python_version",
        CompiledGraphReason.UNSUPPORTED_OPERATING_SYSTEM: "runtime.operating_system",
        CompiledGraphReason.UNSUPPORTED_ARCHITECTURE: "runtime.architecture",
        CompiledGraphReason.UNSUPPORTED_TOPOLOGY: "topology",
        CompiledGraphReason.UNSUPPORTED_SUBMISSION_TRANSPORT: "submission_transport",
        CompiledGraphReason.UNSUPPORTED_TRANSPORT: "transport",
        CompiledGraphReason.INCOMPLETE_CAPABILITY_CONTEXT: "runtime",
        CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE: "capability_set",
        CompiledGraphReason.INVALID_RUNTIME_IDENTITY: "runtime",
    }
    path = reason_paths.get(decision.reason, "runtime")
    return PlanRejection(
        "compiled_graph",
        code,
        f"compatibility.compiled_graph.{path}",
        decision.message,
    )


def _normalize_container_image_digest(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value) is None:
        raise WorkflowPlanValidationError(
            "container_image_digest must be a sha256:<64 hexadecimal digits> identity"
        )
    return value.lower()


def _runtime_with_container_image_digest(
    runtime: CompiledGraphRuntimeIdentity,
    container_image_digest: str | None,
) -> tuple[CompiledGraphRuntimeIdentity, bool]:
    if container_image_digest is None:
        return runtime, False
    deployment_profile = (runtime.deployment_profile or "").strip().lower()
    if not deployment_profile or deployment_profile in {
        "default",
        "generic",
        "none",
        "unknown",
        "unavailable",
        "unresolved",
    }:
        return replace(runtime, deployment_profile=container_image_digest), False
    match = re.search(r"sha256:[0-9a-f]{64}$", deployment_profile)
    if match is not None and match.group(0) != container_image_digest:
        return runtime, True
    return runtime, False


def _default_build_context(
    config: Mapping[str, Any],
    task_context: DurableTaskContext | None,
) -> WorkflowPlanBuildContext:
    detected_revision, detected_image_digest = _detected_deployment_identity()
    revision = config.get("WORKFLOW_PLAN_CODE_REVISION")
    if revision is None:
        revision = detected_revision
    topology: CompiledGraphTopology | None = None
    submission_transport: CompiledGraphSubmissionTransport | str | None = None
    if task_context is not None and task_context.ray_job_driver:
        topology = CompiledGraphTopology.RAY_JOB_DRIVER
        submission_transport = CompiledGraphSubmissionTransport.RAY_JOB
    elif task_context is not None and task_context.compiled_graph_submission_transport:
        topology = CompiledGraphTopology.NESTED_RAY_TASK
        submission_transport = task_context.compiled_graph_submission_transport
    return WorkflowPlanBuildContext(
        build_revision=revision,
        container_image_digest=detected_image_digest,
        trust_identity=config.get("WORKFLOW_PLAN_TRUST_IDENTITY", {}),
        compiled_graph_runtime=detect_compiled_graph_runtime(),
        compiled_graph_topology=topology,
        compiled_graph_submission_transport=submission_transport,
    )


def _compiled_graph_settings(value: Mapping[str, Any] | None) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "settings_version": 1,
        "transport": CompiledGraphTransport.CPU_SHARED_MEMORY.value,
        "maximum_in_flight": 1,
        "maximum_buffered_results": 1,
        "buffer_bytes": None,
        "owner_concurrency": 1,
    }
    if value is None:
        return settings
    if not isinstance(value, Mapping):
        raise WorkflowPlanValidationError("compiled_graph_settings must be a mapping")
    unknown = set(value) - set(settings)
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise WorkflowPlanValidationError(
            f"compiled_graph_settings has unsupported fields: {fields}"
        )
    settings.update(value)
    normalized = _normalize_json(settings, path="compiled_graph_settings", depth=0)
    if normalized["settings_version"] != 1:
        raise WorkflowPlanValidationError("compiled_graph_settings.settings_version must be 1")
    if normalized["transport"] not in {
        CompiledGraphTransport.CPU_SHARED_MEMORY.value,
        CompiledGraphTransport.GPU_NCCL.value,
    }:
        raise WorkflowPlanValidationError(
            "compiled_graph_settings.transport must be cpu-shared-memory or gpu-nccl"
        )
    for key in ("maximum_in_flight", "maximum_buffered_results", "owner_concurrency"):
        item = normalized[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise WorkflowPlanValidationError(f"compiled_graph_settings.{key} must be positive")
    buffer_bytes = normalized["buffer_bytes"]
    if buffer_bytes is not None and (
        isinstance(buffer_bytes, bool) or not isinstance(buffer_bytes, int) or buffer_bytes < 1
    ):
        raise WorkflowPlanValidationError(
            "compiled_graph_settings.buffer_bytes must be None or a positive integer"
        )
    return normalized


def _detected_deployment_identity() -> tuple[str | None, str | None]:
    build_revision: str | None = None
    for name in (
        "DJANGO_RAY_BUILD_REVISION",
        "GIT_COMMIT",
        "SOURCE_VERSION",
        "K_REVISION",
    ):
        value = os.environ.get(name)
        if value and len(value) <= 256:
            build_revision = f"environment:{name}:{value}"
            break
    image = os.environ.get("DJANGO_RAY_IMAGE_DIGEST")
    if image:
        try:
            image_digest = _normalize_container_image_digest(image)
        except WorkflowPlanValidationError as error:
            raise WorkflowPlanValidationError(
                "DJANGO_RAY_IMAGE_DIGEST must be a sha256:<64 hexadecimal digits> identity"
            ) from error
    else:
        image_digest = None
    return build_revision, image_digest


def _outer_runtime_env_identity(
    task_context: DurableTaskContext | None,
    trust_identity: Mapping[str, str],
    config: Mapping[str, Any],
) -> RuntimeEnvPlanIdentity:
    context_identity = (
        getattr(task_context, "runtime_env_plan_identity", None)
        if task_context is not None
        else None
    )
    if isinstance(context_identity, Mapping):
        return runtime_env_plan_identity_from_transport(
            context_identity,
            trust_identity=trust_identity,
        )
    if task_context is not None and task_context.runtime_env_hash:
        empty_digest = hashlib.sha256(b"{}").hexdigest()
        if (
            task_context.runtime_env_hash == empty_digest
            and task_context.runtime_env_profile is None
        ):
            return runtime_env_plan_identity(
                normalize_runtime_env({}),
                trust_identity=trust_identity,
            )
        return runtime_env_plan_identity(
            normalize_runtime_env(
                {"legacy_outer_snapshot": True},
                profile=task_context.runtime_env_profile,
            ),
            trust_identity=trust_identity,
        )
    resolved = resolve_runtime_env_profile(config=config)
    return runtime_env_plan_identity(resolved, trust_identity=trust_identity)


def _runtime_env_projection(
    spec: Mapping[str, Any],
    trust_identity: Mapping[str, str],
) -> tuple[dict[str, Any], set[str]]:
    projection: dict[str, Any] = {}
    unresolved: set[str] = set()
    supported_fields = {
        "conda",
        "config",
        "container",
        "env_vars",
        "excludes",
        "image_uri",
        "pip",
        "py_executable",
        "py_modules",
        "uv",
        "worker_process_setup_hook",
        "working_dir",
    }
    unsupported_field_count = 0
    for key in sorted(spec):
        if key not in supported_fields:
            unsupported_field_count += 1
            unresolved.add("spec.unsupported_field")
            continue
        value = spec[key]
        path = f"spec.{key}"
        if _SECRET_KEY.search(str(key)):
            unresolved.add(path)
            projection[key] = {"value": "runtime_only"}
            continue
        if key == "env_vars":
            projection[key], paths = _safe_environment_variables(
                value,
                path=path,
                trust_identity=trust_identity,
            )
            unresolved.update(paths)
            continue
        if key == "working_dir":
            identity, paths = _code_location_identity(
                value,
                path=path,
                excludes=_runtime_env_code_excludes(spec, include_ray_defaults=True),
                include_root_name=False,
            )
            projection[key] = identity
            unresolved.update(paths)
            continue
        if key == "py_modules":
            identity, paths = _code_location_identity(
                value,
                path=path,
                excludes=_runtime_env_code_excludes(spec, include_ray_defaults=False),
                include_root_name=True,
            )
            projection[key] = identity
            unresolved.update(paths)
            continue
        if key == "image_uri":
            projection[key], paths = _safe_image_identity(value, path=path)
            unresolved.update(paths)
            continue
        if key in {"pip", "uv"}:
            safe_value, paths = _safe_dependency_identity(value, path=path)
            projection[key] = safe_value
            unresolved.update(paths)
            continue
        if key == "conda":
            safe_value, paths = _safe_conda_identity(
                value,
                path=path,
                trust_identity=trust_identity,
            )
            projection[key] = safe_value
            unresolved.update(paths)
            continue
        if key == "excludes":
            safe_value, paths = _safe_excludes_identity(
                value,
                path=path,
                trust_identity=trust_identity,
            )
            projection[key] = safe_value
            unresolved.update(paths)
            continue
        if key == "worker_process_setup_hook" and isinstance(value, str):
            if re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+",
                value,
            ):
                projection[key] = _normalize_identifier(value)
            else:
                projection[key] = {"value": "runtime_only"}
                unresolved.add(path)
            continue
        if key == "config":
            safe_value, paths = _safe_config_identity(value, path=path)
            projection[key] = safe_value
            unresolved.update(paths)
            continue
        if key in {"py_executable", "container"}:
            projection[key] = {"field_present": True, "value": "runtime_only"}
            unresolved.add(path)
            continue
        projection[key] = {"field_present": True, "value": "runtime_only"}
        unresolved.add(path)
    if unsupported_field_count:
        projection["unsupported_field_count"] = unsupported_field_count
    return projection, unresolved


def _safe_environment_variables(
    value: Any,
    *,
    path: str,
    trust_identity: Mapping[str, str],
) -> tuple[Any, set[str]]:
    """Classify variable names while keeping every value out of durable identity."""
    if not isinstance(value, Mapping) or len(value) > MAX_MAPPING_ITEMS:
        return {"values": "runtime_only"}, {path}
    credential_names: list[str] = []
    ordinary_names: list[str] = []
    invalid_names = 0
    unresolved: set[str] = set()
    for raw_name in sorted(value, key=str):
        name = str(raw_name)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,255}", name) is None:
            invalid_names += 1
            unresolved.add(f"{path}.names")
            continue
        if _SECRET_KEY.search(name):
            credential_names.append(name)
            if not (
                trust_identity.get("credential_provider")
                and trust_identity.get("credential_revision")
            ):
                unresolved.add(f"{path}.{name}.value")
        else:
            ordinary_names.append(name)
            if not trust_identity.get("environment_revision"):
                unresolved.add(f"{path}.{name}.value")
    result: dict[str, Any] = {
        "credential_names": credential_names,
        "ordinary_names": ordinary_names,
        "credential_values": "runtime_only",
        "ordinary_values": (
            "covered_by_environment_revision"
            if trust_identity.get("environment_revision")
            else "runtime_only"
        ),
    }
    if invalid_names:
        result["invalid_name_count"] = invalid_names
    return result, unresolved


def _safe_dependency_identity(value: Any, *, path: str) -> tuple[Any, set[str]]:
    if isinstance(value, str):
        return _safe_requirement(value, path=path)
    if isinstance(value, list | tuple):
        if len(value) > MAX_SEQUENCE_ITEMS:
            return [{"value": "runtime_only"}], {path}
        result: list[Any] = []
        unresolved: set[str] = set()
        for index, item in enumerate(value):
            safe, paths = _safe_requirement(item, path=f"{path}.{index}")
            result.append(safe)
            unresolved.update(paths)
        return result, unresolved
    if isinstance(value, Mapping):
        allowed = {"packages", "pip_check", "pip_version"}
        unknown = set(value) - allowed
        result: dict[str, Any] = {}
        unresolved = {f"{path}.{key}" for key in unknown}
        if "packages" in value:
            result["packages"], paths = _safe_dependency_identity(
                value["packages"], path=f"{path}.packages"
            )
            unresolved.update(paths)
        if "pip_check" in value and isinstance(value["pip_check"], bool):
            result["pip_check"] = value["pip_check"]
        elif "pip_check" in value:
            unresolved.add(f"{path}.pip_check")
        if "pip_version" in value:
            result["pip_version"], paths = _safe_requirement(
                value["pip_version"], path=f"{path}.pip_version"
            )
            unresolved.update(paths)
        if unknown:
            result["unsupported_fields"] = len(unknown)
        return result, unresolved
    return {"value": "runtime_only"}, {path}


def _safe_requirement(value: Any, *, path: str) -> tuple[Any, set[str]]:
    if not isinstance(value, str) or not value or len(value) > MAX_STRING_CHARS:
        return {"value": "runtime_only"}, {path}
    normalized = unicodedata.normalize("NFC", value)
    if "://" in normalized:
        return _safe_string_identity(normalized, path=path)
    if normalized.startswith(("-", "/", "\\", ".")) or re.match(r"^[A-Za-z]:[\\/]", normalized):
        return {"value": "runtime_only"}, {path}
    # Only structurally narrow package/version pins may influence identity.
    # Whitespace and installer flags can carry credentials or arbitrary config;
    # those strings remain execution-only even when they contain a hash-looking
    # token. Exact versions are still unresolved for reusable strategies because
    # an index can replace an artifact under the same version.
    if re.fullmatch(
        r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_.,-]+\])?(?:===?|=)[A-Za-z0-9_.+!-]+",
        normalized,
    ):
        return normalized, {path}
    return {"value": "runtime_only"}, {path}


def _safe_image_identity(value: Any, *, path: str) -> tuple[Any, set[str]]:
    if not isinstance(value, str) or len(value) > MAX_STRING_CHARS:
        return {"identity": "runtime_only"}, {path}
    normalized = unicodedata.normalize("NFC", value).lower()
    match = _OCI_IMAGE_DIGEST.fullmatch(normalized)
    if match is None:
        return {"identity": "runtime_only"}, {path}
    return {
        "kind": "oci_digest",
        "sha256": match.group("digest"),
    }, set()


def _safe_conda_identity(
    value: Any,
    *,
    path: str,
    trust_identity: Mapping[str, str],
) -> tuple[Any, set[str]]:
    if isinstance(value, str):
        return {"value": "runtime_only"}, {path}
    if not isinstance(value, Mapping) or len(value) > MAX_MAPPING_ITEMS:
        return {"value": "runtime_only"}, {path}
    result: dict[str, Any] = {}
    unresolved: set[str] = set()
    allowed = {"name", "channels", "dependencies", "variables"}
    unknown = set(value) - allowed
    if unknown:
        result["unsupported_fields"] = len(unknown)
        unresolved.update(f"{path}.{key}" for key in unknown)
    name = value.get("name")
    if name is not None:
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", name):
            result["name"] = name
        else:
            unresolved.add(f"{path}.name")
    channels = value.get("channels")
    if channels is not None:
        result["channels"], paths = _safe_channel_list(
            channels,
            path=f"{path}.channels",
            trust_identity=trust_identity,
        )
        unresolved.update(paths)
    dependencies = value.get("dependencies")
    if dependencies is not None:
        result["dependencies"], paths = _safe_conda_dependencies(
            dependencies, path=f"{path}.dependencies"
        )
        unresolved.update(paths)
    variables = value.get("variables")
    if variables is not None:
        result["variables"], paths = _safe_environment_variables(
            variables,
            path=f"{path}.variables",
            trust_identity=trust_identity,
        )
        unresolved.update(paths)
    return result, unresolved


def _safe_channel_list(
    value: Any,
    *,
    path: str,
    trust_identity: Mapping[str, str],
) -> tuple[Any, set[str]]:
    if not isinstance(value, list | tuple) or len(value) > MAX_SEQUENCE_ITEMS:
        return {"values": "runtime_only"}, {path}
    unresolved: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not 0 < len(item) <= MAX_STRING_CHARS:
            unresolved.add(f"{path}.{index}")
    environment_revision = trust_identity.get("environment_revision")
    if not environment_revision:
        unresolved.add(path)
    return {
        "channel_count": len(value),
        "values": ("covered_by_environment_revision" if environment_revision else "runtime_only"),
    }, unresolved


def _safe_conda_dependencies(value: Any, *, path: str) -> tuple[Any, set[str]]:
    if not isinstance(value, list | tuple) or len(value) > MAX_SEQUENCE_ITEMS:
        return [{"value": "runtime_only"}], {path}
    result: list[Any] = []
    unresolved: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}.{index}"
        if isinstance(item, Mapping) and set(item) == {"pip"}:
            safe, paths = _safe_dependency_identity(item["pip"], path=f"{item_path}.pip")
            result.append({"pip": safe})
        else:
            safe, paths = _safe_requirement(item, path=item_path)
            result.append(safe)
        unresolved.update(paths)
    return result, unresolved


def _safe_excludes_identity(
    value: Any,
    *,
    path: str,
    trust_identity: Mapping[str, str],
) -> tuple[Any, set[str]]:
    if not isinstance(value, list | tuple) or len(value) > MAX_SEQUENCE_ITEMS:
        return {"patterns": "runtime_only"}, {path}
    unresolved: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not 0 < len(item) <= MAX_STRING_CHARS:
            unresolved.add(f"{path}.{index}")
    environment_revision = trust_identity.get("environment_revision")
    if not environment_revision:
        unresolved.add(path)
    return {
        "pattern_count": len(value),
        "patterns": ("covered_by_environment_revision" if environment_revision else "runtime_only"),
    }, unresolved


def _safe_config_identity(value: Any, *, path: str) -> tuple[Any, set[str]]:
    if not isinstance(value, Mapping) or len(value) > MAX_MAPPING_ITEMS:
        return {"value": "runtime_only"}, {path}
    result: dict[str, Any] = {}
    unresolved: set[str] = set()
    supported_fields = {"eager_install", "log_files", "setup_timeout_seconds"}
    unknown_count = len(set(value) - supported_fields)
    for key_text in sorted(set(value) & supported_fields):
        item = value[key_text]
        if item is None or isinstance(item, bool | int):
            result[key_text] = item
        elif isinstance(item, float) and math.isfinite(item):
            result[key_text] = _normalize_number(item)
        else:
            result[key_text] = {"value": "runtime_only"}
            unresolved.add(f"{path}.{key_text}")
    if unknown_count:
        result["unsupported_field_count"] = unknown_count
        unresolved.add(f"{path}.unsupported_field")
    return result, unresolved


def _safe_string_identity(value: Any, *, path: str) -> tuple[Any, set[str]]:
    if not isinstance(value, str) or len(value) > MAX_STRING_CHARS:
        return {"value": "runtime_only"}, {path}
    normalized = unicodedata.normalize("NFC", value)
    if "://" not in normalized:
        return {"value": "runtime_only"}, {path}
    ray_package = _RAY_GCS_PACKAGE_URI.fullmatch(normalized)
    if ray_package is not None:
        return {
            "kind": "ray_gcs_package",
            "package_digest": ray_package.group("digest"),
            "package_extension": ray_package.group("extension").removeprefix("."),
            "identity_strength": "runtime_only",
        }, {path}
    # URI schemes, hosts, ports, paths, queries, fragments, and user info can
    # all carry signed or low-entropy credentials. Do not persist or hash any
    # of them. Without a verified content digest the environment is dynamic-only.
    return {"uri": "runtime_only"}, {path}


def _runtime_env_code_excludes(
    spec: Mapping[str, Any],
    *,
    include_ray_defaults: bool,
) -> list[str]:
    raw_excludes = spec.get("excludes") or []
    if (
        not isinstance(raw_excludes, list | tuple)
        or len(raw_excludes) > MAX_SEQUENCE_ITEMS
        or any(not isinstance(item, str) for item in raw_excludes)
    ):
        raise WorkflowPlanValidationError(
            "RuntimeEnv local code paths require a bounded list of string excludes"
        )
    result = list(raw_excludes)
    if include_ray_defaults:
        result = _ray_runtime_env_default_excludes() + result
    return result


def _code_location_identity(
    value: Any,
    *,
    path: str,
    excludes: list[str],
    include_root_name: bool,
) -> tuple[Any, set[str]]:
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.exists():
            try:
                identity = _local_path_identity(candidate, excludes=excludes)
            except _IdentityBudgetExceededError:
                return {
                    "kind": "local_code",
                    "identity": "runtime_only",
                    "reason": "identity_budget_exceeded",
                }, {path}
            if include_root_name:
                identity["import_root_name"] = candidate.resolve().name
            identity["execution_addressing"] = "ray_artifact_not_strongly_verified"
            return identity, {path}
        safe, unresolved = _safe_string_identity(value, path=path)
        return {"kind": "uri", "identity": safe}, unresolved
    if isinstance(value, list | tuple):
        identities = []
        unresolved: set[str] = set()
        for index, item in enumerate(value):
            identity, paths = _code_location_identity(
                item,
                path=f"{path}.{index}",
                excludes=excludes,
                include_root_name=include_root_name,
            )
            identities.append(identity)
            unresolved.update(paths)
        return identities, unresolved
    return {"identity": "unresolved"}, {path}


def _local_path_identity(path: Path, *, excludes: list[str]) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved.is_file():
        if resolved.stat().st_size > MAX_CODE_TREE_BYTES:
            raise _IdentityBudgetExceededError
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        return {"kind": "local_file_snapshot", "sha256": digest}
    if not resolved.is_dir():
        raise WorkflowPlanValidationError("RuntimeEnv code path does not exist")

    from ray._private.ray_constants import RAY_RUNTIME_ENV_IGNORE_GITIGNORE
    from ray._private.runtime_env.packaging import _dir_travel, _get_excludes

    include_gitignore = os.environ.get(RAY_RUNTIME_ENV_IGNORE_GITIGNORE, "0") != "1"
    records: list[tuple[str, str, str | None]] = []
    file_count = 0
    byte_count = 0
    entry_count = 0

    def record(candidate: Path) -> None:
        nonlocal byte_count, entry_count, file_count
        if candidate == resolved:
            return
        if candidate.is_symlink():
            raise WorkflowPlanValidationError("RuntimeEnv code trees cannot contain symlinks")
        entry_count += 1
        if entry_count > MAX_CODE_TREE_ENTRIES:
            raise _IdentityBudgetExceededError
        relative = candidate.relative_to(resolved).as_posix()
        if candidate.is_dir():
            records.append(("directory", relative, None))
            return
        if candidate.is_file():
            file_count += 1
            byte_count += candidate.stat().st_size
            if file_count > MAX_CODE_TREE_FILES or byte_count > MAX_CODE_TREE_BYTES:
                raise _IdentityBudgetExceededError
            content_digest = hashlib.sha256()
            with candidate.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    content_digest.update(chunk)
            records.append(("file", relative, content_digest.hexdigest()))
            return
        raise WorkflowPlanValidationError("RuntimeEnv code trees contain a special file")

    _dir_travel(
        resolved,
        [_get_excludes(resolved, excludes)],
        record,
        include_gitignore=include_gitignore,
    )
    digest = hashlib.sha256()
    for kind, relative, content_digest in sorted(records):
        digest.update(_canonical_bytes([kind, relative, content_digest]))
    return {
        "kind": "local_tree_snapshot",
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "byte_count": byte_count,
    }


def _normalize_ray_options(
    options: Mapping[str, Any],
    *,
    node_id: str,
    trust_identity: Mapping[str, str],
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(options, Mapping):
        raise WorkflowPlanValidationError(f"nodes.{node_id}.ray_options must be a mapping")
    unknown = set(options) - _RAY_TASK_OPTION_FIELDS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise WorkflowPlanValidationError(
            f"nodes.{node_id}.ray_options has unsupported fields: {fields}"
        )
    execution_options = _thaw_json(options)
    try:
        from ray._common.ray_option_utils import validate_task_options

        validate_task_options(execution_options, in_options=True)
    except ImportError:
        pass
    except (TypeError, ValueError) as error:
        raise WorkflowPlanValidationError(
            f"nodes.{node_id}.ray_options is incompatible with the installed Ray task API"
        ) from error

    projection = dict(execution_options)
    unresolved: list[str] = []
    retry_exceptions = execution_options.get("retry_exceptions")
    if isinstance(retry_exceptions, list | tuple):
        projection["retry_exceptions"] = {
            "exception_types": [
                f"{exception_type.__module__}.{exception_type.__qualname__}"
                for exception_type in retry_exceptions
            ]
        }
        unresolved.append(f"nodes.{node_id}.ray_options.retry_exceptions")
    scheduling = execution_options.get("scheduling_strategy")
    if scheduling is not None and not isinstance(scheduling, str):
        projection["scheduling_strategy"] = {
            "process_local_type": f"{type(scheduling).__module__}.{type(scheduling).__qualname__}"
        }
        unresolved.append(f"nodes.{node_id}.ray_options.scheduling_strategy")
    placement_group = execution_options.get("placement_group")
    if placement_group is not None and not isinstance(placement_group, str):
        projection["placement_group"] = {
            "process_local_type": (
                f"{type(placement_group).__module__}.{type(placement_group).__qualname__}"
            )
        }
        unresolved.append(f"nodes.{node_id}.ray_options.placement_group")

    normalized = _normalize_json(projection, path=f"nodes.{node_id}.ray_options", depth=0)
    for key in ("num_cpus", "num_gpus", "memory"):
        value = normalized.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int | float) or value < 0
        ):
            raise WorkflowPlanValidationError(
                f"nodes.{node_id}.ray_options.{key} must be a non-negative number"
            )
    resources = normalized.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            raise WorkflowPlanValidationError(
                f"nodes.{node_id}.ray_options.resources must be a mapping"
            )
        for key, value in resources.items():
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise WorkflowPlanValidationError(
                    f"nodes.{node_id}.ray_options.resources.{key} must be a non-negative number"
                )
    num_returns = normalized.get("num_returns", 1)
    if num_returns not in (None, 1):
        raise WorkflowPlanValidationError(
            f"nodes.{node_id}.ray_options.num_returns must be 1 for workflow steps"
        )
    label_selector = normalized.get("label_selector")
    if label_selector is not None and (
        not isinstance(label_selector, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in label_selector.items()
        )
    ):
        raise WorkflowPlanValidationError(
            f"nodes.{node_id}.ray_options.label_selector must map strings to strings"
        )
    labels = normalized.get("_labels")
    if labels is not None and (
        not isinstance(labels, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
        )
    ):
        raise WorkflowPlanValidationError(
            f"nodes.{node_id}.ray_options._labels must map strings to strings"
        )
    for key in ("enable_task_events", "placement_group_capture_child_tasks"):
        value = normalized.get(key)
        if value is not None and not isinstance(value, bool):
            raise WorkflowPlanValidationError(
                f"nodes.{node_id}.ray_options.{key} must be a boolean"
            )
    for key in (
        "max_retries",
        "_generator_backpressure_num_objects",
        "placement_group_bundle_index",
    ):
        value = normalized.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise WorkflowPlanValidationError(
                f"nodes.{node_id}.ray_options.{key} must be an integer"
            )
    if normalized.get("max_retries", -1) < -1:
        raise WorkflowPlanValidationError(
            f"nodes.{node_id}.ray_options.max_retries must be at least -1"
        )
    if normalized.get("_generator_backpressure_num_objects") == 0:
        raise WorkflowPlanValidationError(
            f"nodes.{node_id}.ray_options._generator_backpressure_num_objects cannot be 0"
        )
    normalized.pop("name", None)
    normalized.pop("_labels", None)
    semantic_defaults: dict[str, tuple[Any, ...]] = {
        "accelerator_type": (None,),
        "enable_task_events": (None, True),
        "fallback_strategy": (None,),
        "label_selector": (None,),
        "max_retries": (None, 3),
        "memory": (None,),
        "num_cpus": (1, 1.0),
        "num_gpus": (None,),
        "num_returns": (None, 1),
        "placement_group": (None, "default"),
        "placement_group_bundle_index": (None, -1),
        "placement_group_capture_child_tasks": (None, False),
        "resources": (None,),
        "retry_exceptions": (None, False),
        "scheduling_strategy": (None, "DEFAULT"),
        "_generator_backpressure_num_objects": (None,),
    }
    for key, defaults in semantic_defaults.items():
        if key in normalized and normalized[key] in defaults:
            normalized.pop(key)
    scheduling_revision = trust_identity.get("scheduling_revision")
    for key in ("label_selector", "fallback_strategy"):
        if key not in normalized:
            continue
        value = normalized[key]
        normalized[key] = {
            "entry_count": len(value) if isinstance(value, list | dict) else 1,
            "identity": (
                "covered_by_scheduling_revision" if scheduling_revision else "runtime_only"
            ),
        }
        if not scheduling_revision:
            unresolved.append(f"nodes.{node_id}.ray_options.{key}")
    scheduling_strategy = normalized.get("scheduling_strategy")
    if isinstance(scheduling_strategy, str) and scheduling_strategy not in {"DEFAULT", "SPREAD"}:
        normalized["scheduling_strategy"] = {
            "present": True,
            "identity": "runtime_only",
        }
        unresolved.append(f"nodes.{node_id}.ray_options.scheduling_strategy")
    placement_group = normalized.get("placement_group")
    if isinstance(placement_group, str) and placement_group != "default":
        normalized["placement_group"] = {
            "present": True,
            "identity": "runtime_only",
        }
        unresolved.append(f"nodes.{node_id}.ray_options.placement_group")
    return normalized, unresolved


def _resource_options(options: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: options[key]
        for key in ("num_cpus", "num_gpus", "memory", "accelerator_type")
        if key in options
    }
    if "resources" in options:
        result["custom"] = options["resources"]
    return result


def _scheduling_options(options: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: options[key]
        for key in (
            "scheduling_strategy",
            "label_selector",
            "fallback_strategy",
            "_labels",
        )
        if key in options
    }


def _source_digest(path: str, size: int) -> str:
    if size > MAX_CODE_FILE_BYTES:
        raise _IdentityBudgetExceededError
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _module_code_identity(module_name: str) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    origin = getattr(getattr(module, "__spec__", None), "origin", None)
    identity: dict[str, Any] = {
        "module": module_name,
        "stable": False,
        "origin": "unresolved",
    }
    distributions = _package_distributions().get(module_name.split(".", 1)[0], [])
    if distributions:
        packages = []
        for distribution in sorted(distributions):
            try:
                version = _distribution_version(distribution)
            except importlib.metadata.PackageNotFoundError:
                continue
            packages.append({"name": distribution, "version": version})
        if packages:
            identity["packages"] = packages
            identity["stable"] = True
    if isinstance(origin, str) and origin not in {"built-in", "frozen"}:
        path = Path(origin)
        if path.is_file():
            stat = path.stat()
            try:
                source_digest = _source_digest(str(path.resolve()), stat.st_size)
            except _IdentityBudgetExceededError:
                identity.update(
                    {
                        "stable": False,
                        "origin": "identity_budget_exceeded",
                    }
                )
            else:
                identity.update(
                    {
                        "stable": True,
                        "origin": "module_content",
                        "sha256": source_digest,
                    }
                )
    return identity


def _callable_code_identity(
    callable_path: str,
    *,
    module_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    owner_module_name = callable_path.rsplit(".", 1)[0]
    try:
        callable_obj = import_callable(callable_path)
    except Exception:  # noqa: BLE001 - submitter inspection cannot block worker-only code
        return {
            "module": owner_module_name,
            "resolved_qualname": callable_path.rsplit(".", 1)[-1],
            "kind": "unknown",
            "stable": False,
            "deployment": "worker_runtime_only",
            "import_owner": {
                "module": owner_module_name,
                "stable": False,
                "origin": "unavailable_on_submitter",
            },
            "resolved_target": {
                "module": owner_module_name,
                "stable": False,
                "origin": "unavailable_on_submitter",
            },
        }
    target_module_name = getattr(callable_obj, "__module__", owner_module_name)
    target_qualname = getattr(
        callable_obj,
        "__qualname__",
        getattr(callable_obj, "__name__", callable_path.rsplit(".", 1)[-1]),
    )
    if not isinstance(target_qualname, str):
        raise WorkflowPlanValidationError(
            f"Callable {callable_path!r} has no canonical resolved qualified name"
        )
    callable_kind_is_stable = bool(
        inspect.isfunction(callable_obj)
        or inspect.isbuiltin(callable_obj)
        or inspect.isclass(callable_obj)
    )
    owner_identity = module_cache.get(owner_module_name)
    if owner_identity is None:
        owner_identity = _module_code_identity(owner_module_name)
        module_cache[owner_module_name] = owner_identity
    target_identity = (
        owner_identity
        if target_module_name == owner_module_name
        else module_cache.get(target_module_name)
    )
    if target_identity is None:
        target_identity = _module_code_identity(target_module_name)
        module_cache[target_module_name] = target_identity
    kind = (
        "async"
        if inspect.iscoroutinefunction(callable_obj)
        else ("sync" if callable_kind_is_stable else "callable_object")
    )
    return {
        "module": target_module_name,
        "resolved_qualname": target_qualname,
        "kind": kind,
        "stable": bool(
            callable_kind_is_stable and owner_identity["stable"] and target_identity["stable"]
        ),
        "deployment": "module_and_target_identity",
        "callable_state": "module_defined" if callable_kind_is_stable else "runtime_only",
        "import_owner": owner_identity,
        "resolved_target": target_identity,
    }


@lru_cache(maxsize=1)
def _package_distributions() -> Mapping[str, list[str]]:
    return importlib.metadata.packages_distributions()


@lru_cache(maxsize=512)
def _distribution_version(distribution: str) -> str:
    return importlib.metadata.version(distribution)


def _normalize_trust_identity(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ImproperlyConfigured("django-ray: WORKFLOW_PLAN_TRUST_IDENTITY must be a mapping")
    unknown = set(value) - set(_TRUST_FIELDS)
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise ImproperlyConfigured(
            "django-ray: WORKFLOW_PLAN_TRUST_IDENTITY has unsupported fields: " + fields
        )
    result: dict[str, str] = {}
    for key in _TRUST_FIELDS:
        field = value.get(key)
        if field is None:
            continue
        if not isinstance(field, str) or not field or len(field) > 256:
            raise ImproperlyConfigured(
                f"django-ray: WORKFLOW_PLAN_TRUST_IDENTITY[{key!r}] must be a "
                "non-empty string of at most 256 characters"
            )
        result[key] = _normalize_identifier(field)
    return result


def _normalize_json(value: Any, *, path: str, depth: int) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise WorkflowPlanValidationError(f"{path} exceeds maximum nesting depth {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowPlanValidationError(f"{path} must not contain a non-finite number")
        return _normalize_number(value)
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise WorkflowPlanValidationError(
                f"{path} string exceeds maximum length {MAX_STRING_CHARS}"
            )
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        if len(value) > MAX_MAPPING_ITEMS:
            raise WorkflowPlanValidationError(
                f"{path} has more than {MAX_MAPPING_ITEMS} mapping entries"
            )
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise WorkflowPlanValidationError(f"{path} mapping keys must be strings")
            key = _normalize_identifier(raw_key)
            if key in normalized:
                raise WorkflowPlanValidationError(
                    f"{path} has duplicate keys after Unicode normalization: {key!r}"
                )
            normalized[key] = _normalize_json(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
            )
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        if len(value) > MAX_SEQUENCE_ITEMS:
            raise WorkflowPlanValidationError(
                f"{path} has more than {MAX_SEQUENCE_ITEMS} sequence entries"
            )
        return [
            _normalize_json(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise WorkflowPlanValidationError(
        f"{path} contains unsupported process-local value "
        f"{type(value).__module__}.{type(value).__name__}"
    )


def _normalize_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _normalize_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or len(normalized) > MAX_STRING_CHARS:
        raise WorkflowPlanValidationError("Plan identifiers must be non-empty and bounded")
    return normalized


def _normalized_keyword_names(value: Mapping[Any, Any], *, path: str) -> list[str]:
    normalized: set[str] = set()
    for key in value:
        if not isinstance(key, str):
            raise WorkflowPlanValidationError(f"{path} keys must be strings")
        name = _normalize_identifier(key)
        if name in normalized:
            raise WorkflowPlanValidationError(
                f"{path} has duplicate keys after Unicode normalization: {name!r}"
            )
        normalized.add(name)
    return sorted(normalized)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    normalized = _normalize_json(value, path="$", depth=0)
    return _canonical_json(normalized).encode("utf-8")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _domain_digest(domain: bytes, value: bytes) -> str:
    return f"sha256:{hashlib.sha256(domain + value).hexdigest()}"


def _django_ray_version() -> str:
    from django_ray import __version__

    return __version__


__all__ = [
    "EffectiveWorkflowPlan",
    "MaterializedWorkflowPlan",
    "MAX_RUNTIME_ENV_IDENTITY_BYTES",
    "PLAN_FORMAT",
    "PLAN_FORMAT_VERSION",
    "PLAN_SELECTION_FORMAT",
    "PLAN_SELECTION_FORMAT_VERSION",
    "PlanEligibility",
    "PlanRejection",
    "PlanSelection",
    "RuntimeEnvPlanIdentity",
    "StepExecutionBinding",
    "WorkflowPlanBuildContext",
    "WorkflowPlanMismatchError",
    "WorkflowPlanValidationError",
    "materialize_workflow_plan",
    "plan_requires_drain",
    "prepare_materialized_plan_for_ray",
    "runtime_env_plan_identity",
    "runtime_env_plan_identity_from_transport",
    "validate_plan_selection_manifest",
]
