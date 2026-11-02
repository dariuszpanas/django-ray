"""Ray-free reference state machine for durable step checkpoints and resume planning (#289).

Implements the fail-closed resume planning contract defined in ADR-0007:
- Explicit node resumability classes (``PURE_DETERMINISTIC``,
  ``IDEMPOTENT_SIDE_EFFECT``, ``NON_IDEMPOTENT_SIDE_EFFECT``,
  ``EPHEMERAL_UNCHECKPOINTABLE``).
- Explicit side-effect dispositions (``NONE``, ``NOT_STARTED``,
  ``IN_FLIGHT_INDETERMINATE``, ``COMMITTED``, ``ROLLED_BACK``).
- Plan fingerprint and attempt/generation fencing validation.
- Transitive dependency-cone invalidation with protection against silently
  re-running committed non-idempotent side effects or indeterminate side effects.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class WorkflowCheckpointError(RuntimeError):
    """Base error for workflow step checkpoint validation and resume planning."""


class CheckpointConfigurationError(WorkflowCheckpointError, ValueError):
    """Raised when a checkpoint node descriptor or envelope is malformed."""


class CheckpointPlanMismatchError(WorkflowCheckpointError):
    """Raised when a checkpoint does not match the pinned workflow plan contract."""


class CheckpointFencingError(WorkflowCheckpointError):
    """Raised when a checkpoint carries a stale/future attempt or generation."""


class CheckpointIndeterminateSideEffectError(WorkflowCheckpointError):
    """Raised when a node has an indeterminate in-flight side effect that cannot safely resume."""


class CheckpointNonIdempotentInvalidationError(WorkflowCheckpointError):
    """Raised when upstream invalidation would force re-executing a committed non-idempotent node."""


class NodeResumabilityClass(StrEnum):
    """Classification of whether and how a workflow node can be resumed."""

    PURE_DETERMINISTIC = "pure_deterministic"
    IDEMPOTENT_SIDE_EFFECT = "idempotent_side_effect"
    NON_IDEMPOTENT_SIDE_EFFECT = "non_idempotent_side_effect"
    EPHEMERAL_UNCHECKPOINTABLE = "ephemeral_uncheckpointable"


class SideEffectDisposition(StrEnum):
    """Recorded state of external side effects for a workflow node attempt."""

    NONE = "none"
    NOT_STARTED = "not_started"
    IN_FLIGHT_INDETERMINATE = "in_flight_indeterminate"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class ResumeNodeDisposition(StrEnum):
    """Planned action for a workflow node during a resumed attempt."""

    REUSE_CHECKPOINT = "reuse_checkpoint"
    EXECUTE_FRESH = "execute_fresh"
    REEXECUTE_IDEMPOTENT = "reexecute_idempotent"
    UPSTREAM_INVALIDATED = "upstream_invalidated"
    FORCED_REEXECUTE = "forced_reexecute"


@dataclass(frozen=True)
class CheckpointableNodeSpec:
    """Static node metadata in a workflow DAG used for resume planning."""

    node_id: str
    callable_path: str
    resumability_class: NodeResumabilityClass = NodeResumabilityClass.PURE_DETERMINISTIC
    upstream_node_ids: tuple[str, ...] = ()
    idempotency_key: str | None = None

    def validated(self) -> CheckpointableNodeSpec:
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise CheckpointConfigurationError("node_id must be a non-empty string")
        if not isinstance(self.callable_path, str) or not self.callable_path.strip():
            raise CheckpointConfigurationError("callable_path must be a non-empty string")
        if not isinstance(self.resumability_class, NodeResumabilityClass):
            raise CheckpointConfigurationError("resumability_class must be a NodeResumabilityClass")
        if self.resumability_class == NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT and (
            not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip()
        ):
            raise CheckpointConfigurationError(
                f"Node {self.node_id!r} with IDEMPOTENT_SIDE_EFFECT requires a non-empty "
                "idempotency_key"
            )
        return self


@dataclass(frozen=True)
class DurableStepCheckpointEnvelope:
    """Verified metadata envelope for a persisted workflow step checkpoint."""

    workflow_plan_fingerprint: str
    node_id: str
    callable_path: str
    resumability_class: NodeResumabilityClass
    side_effect_disposition: SideEffectDisposition
    input_digest_sha256: str
    output_digest_sha256: str | None
    output_reference: str | None
    source_attempt_number: int
    source_execution_generation: int
    idempotency_key: str | None = None

    def validated(self) -> DurableStepCheckpointEnvelope:
        if (
            not isinstance(self.workflow_plan_fingerprint, str)
            or not self.workflow_plan_fingerprint
        ):
            raise CheckpointConfigurationError("workflow_plan_fingerprint is required")
        if not isinstance(self.node_id, str) or not self.node_id:
            raise CheckpointConfigurationError("node_id is required")
        if not isinstance(self.callable_path, str) or not self.callable_path:
            raise CheckpointConfigurationError("callable_path is required")
        if not isinstance(self.resumability_class, NodeResumabilityClass):
            raise CheckpointConfigurationError("Invalid resumability_class")
        if not isinstance(self.side_effect_disposition, SideEffectDisposition):
            raise CheckpointConfigurationError("Invalid side_effect_disposition")
        if (
            not isinstance(self.input_digest_sha256, str)
            or SHA256_DIGEST_RE.match(self.input_digest_sha256) is None
        ):
            raise CheckpointConfigurationError("input_digest_sha256 must be sha256:<64-hex>")
        if self.output_digest_sha256 is not None and (
            not isinstance(self.output_digest_sha256, str)
            or SHA256_DIGEST_RE.match(self.output_digest_sha256) is None
        ):
            raise CheckpointConfigurationError("output_digest_sha256 must be sha256:<64-hex>")
        if (
            type(self.source_attempt_number) is not int
            or self.source_attempt_number <= 0
            or type(self.source_execution_generation) is not int
            or self.source_execution_generation <= 0
        ):
            raise CheckpointConfigurationError(
                "source_attempt_number and source_execution_generation must be positive ints"
            )
        return self


@dataclass(frozen=True)
class NodeResumeDecision:
    """Per-node resume decision produced by ``WorkflowCheckpointResumePlanner``."""

    node_id: str
    disposition: ResumeNodeDisposition
    reused_checkpoint: DurableStepCheckpointEnvelope | None = None
    invalidation_reason: str | None = None


@dataclass(frozen=True)
class WorkflowResumePlan:
    """Complete, topologically ordered resume execution plan for a workflow attempt."""

    workflow_plan_fingerprint: str
    target_attempt_number: int
    target_execution_generation: int
    decisions: tuple[NodeResumeDecision, ...]
    reused_node_ids: tuple[str, ...]
    reexecuted_node_ids: tuple[str, ...]


class WorkflowCheckpointResumePlanner:
    """Fail-closed state machine for planning resumed workflow execution."""

    def __init__(
        self,
        *,
        workflow_plan_fingerprint: str,
        nodes: list[CheckpointableNodeSpec] | tuple[CheckpointableNodeSpec, ...],
    ) -> None:
        if not isinstance(workflow_plan_fingerprint, str) or not workflow_plan_fingerprint.strip():
            raise CheckpointConfigurationError("workflow_plan_fingerprint must be non-empty")
        if not nodes:
            raise CheckpointConfigurationError("nodes must be non-empty")

        self.workflow_plan_fingerprint = workflow_plan_fingerprint
        self._nodes_by_id: dict[str, CheckpointableNodeSpec] = {}
        for raw_node in nodes:
            node = raw_node.validated()
            if node.node_id in self._nodes_by_id:
                raise CheckpointConfigurationError(f"Duplicate node_id: {node.node_id!r}")
            self._nodes_by_id[node.node_id] = node

        self._topological_order = self._validate_dag_and_sort()

    def _validate_dag_and_sort(self) -> tuple[str, ...]:
        in_degree: dict[str, int] = dict.fromkeys(self._nodes_by_id, 0)
        children: dict[str, list[str]] = {nid: [] for nid in self._nodes_by_id}
        for nid, spec in self._nodes_by_id.items():
            for up in spec.upstream_node_ids:
                if up not in self._nodes_by_id:
                    raise CheckpointConfigurationError(
                        f"Node {nid!r} references unknown upstream node {up!r}"
                    )
                in_degree[nid] += 1
                children[up].append(nid)

        ready = deque(sorted(nid for nid, deg in in_degree.items() if deg == 0))
        order: list[str] = []
        while ready:
            current = ready.popleft()
            order.append(current)
            for child in sorted(children[current]):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    ready.append(child)

        if len(order) != len(self._nodes_by_id):
            raise CheckpointConfigurationError("Workflow checkpoint graph contains a cycle")
        return tuple(order)

    def build_resume_plan(
        self,
        *,
        target_attempt_number: int,
        target_execution_generation: int,
        checkpoints: list[DurableStepCheckpointEnvelope]
        | tuple[DurableStepCheckpointEnvelope, ...],
        corrupted_output_node_ids: set[str] | frozenset[str] = frozenset(),
        force_reexecute_node_ids: set[str] | frozenset[str] = frozenset(),
    ) -> WorkflowResumePlan:
        """Compute a fail-closed ``WorkflowResumePlan`` for a resuming attempt."""
        if (
            type(target_attempt_number) is not int
            or target_attempt_number <= 1
            or type(target_execution_generation) is not int
            or target_execution_generation <= 1
        ):
            raise CheckpointFencingError(
                "Resumed execution requires target_attempt_number > 1 and "
                "target_execution_generation > 1"
            )

        unknown_forced = set(force_reexecute_node_ids) - set(self._nodes_by_id)
        if unknown_forced:
            raise CheckpointConfigurationError(
                f"force_reexecute_node_ids contains unknown nodes: {sorted(unknown_forced)}"
            )

        cp_by_node: dict[str, DurableStepCheckpointEnvelope] = {}
        for raw_cp in checkpoints:
            cp = raw_cp.validated()
            if cp.workflow_plan_fingerprint != self.workflow_plan_fingerprint:
                raise CheckpointPlanMismatchError(
                    f"Checkpoint for node {cp.node_id!r} has plan fingerprint "
                    f"{cp.workflow_plan_fingerprint!r} != {self.workflow_plan_fingerprint!r}"
                )
            if cp.node_id not in self._nodes_by_id:
                raise CheckpointPlanMismatchError(
                    f"Checkpoint references node {cp.node_id!r} not in workflow plan"
                )
            node_spec = self._nodes_by_id[cp.node_id]
            if cp.callable_path != node_spec.callable_path:
                raise CheckpointPlanMismatchError(
                    f"Checkpoint callable_path {cp.callable_path!r} != {node_spec.callable_path!r}"
                )
            if (
                cp.source_attempt_number >= target_attempt_number
                or cp.source_execution_generation >= target_execution_generation
            ):
                raise CheckpointFencingError(
                    f"Checkpoint for node {cp.node_id!r} carries non-prior authority "
                    f"(attempt={cp.source_attempt_number}, gen={cp.source_execution_generation}) "
                    f"vs target (attempt={target_attempt_number}, gen={target_execution_generation})"
                )
            prev = cp_by_node.get(cp.node_id)
            if prev is None or (
                cp.source_execution_generation,
                cp.source_attempt_number,
            ) > (
                prev.source_execution_generation,
                prev.source_attempt_number,
            ):
                cp_by_node[cp.node_id] = cp

        invalidated_nodes: set[str] = set()
        decisions: list[NodeResumeDecision] = []
        reused_ids: list[str] = []
        reexecuted_ids: list[str] = []

        for node_id in self._topological_order:
            spec = self._nodes_by_id[node_id]
            cp = cp_by_node.get(node_id)
            upstream_invalidated = any(up in invalidated_nodes for up in spec.upstream_node_ids)

            # Check indeterminate side effects first (fail-closed regardless of upstream state).
            if (
                cp is not None
                and cp.side_effect_disposition == SideEffectDisposition.IN_FLIGHT_INDETERMINATE
            ):
                if spec.resumability_class == NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT:
                    raise CheckpointIndeterminateSideEffectError(
                        f"Node {node_id!r} has IN_FLIGHT_INDETERMINATE non-idempotent side effect; "
                        "automatic resume refused"
                    )
                if spec.resumability_class == NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT and (
                    not cp.idempotency_key or cp.idempotency_key != spec.idempotency_key
                ):
                    raise CheckpointIndeterminateSideEffectError(
                        f"Node {node_id!r} has IN_FLIGHT_INDETERMINATE side effect with mismatched "
                        "idempotency_key"
                    )

            if node_id in force_reexecute_node_ids:
                if (
                    cp is not None
                    and cp.side_effect_disposition == SideEffectDisposition.COMMITTED
                    and spec.resumability_class == NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT
                ):
                    raise CheckpointNonIdempotentInvalidationError(
                        f"Cannot force re-execution of committed non-idempotent node {node_id!r}"
                    )
                invalidated_nodes.add(node_id)
                reexecuted_ids.append(node_id)
                decisions.append(
                    NodeResumeDecision(
                        node_id=node_id,
                        disposition=ResumeNodeDisposition.FORCED_REEXECUTE,
                        invalidation_reason="operator_forced_reexecute",
                    )
                )
                continue

            if upstream_invalidated:
                if (
                    cp is not None
                    and cp.side_effect_disposition == SideEffectDisposition.COMMITTED
                    and spec.resumability_class == NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT
                ):
                    raise CheckpointNonIdempotentInvalidationError(
                        f"Upstream invalidation reached committed non-idempotent node {node_id!r}; "
                        "automatic re-execution refused"
                    )
                invalidated_nodes.add(node_id)
                reexecuted_ids.append(node_id)
                decisions.append(
                    NodeResumeDecision(
                        node_id=node_id,
                        disposition=ResumeNodeDisposition.UPSTREAM_INVALIDATED,
                        invalidation_reason="upstream_dependency_reexecuted",
                    )
                )
                continue

            if spec.resumability_class == NodeResumabilityClass.EPHEMERAL_UNCHECKPOINTABLE:
                invalidated_nodes.add(node_id)
                reexecuted_ids.append(node_id)
                decisions.append(
                    NodeResumeDecision(
                        node_id=node_id,
                        disposition=ResumeNodeDisposition.EXECUTE_FRESH,
                        invalidation_reason="ephemeral_uncheckpointable",
                    )
                )
                continue

            if cp is None:
                invalidated_nodes.add(node_id)
                reexecuted_ids.append(node_id)
                decisions.append(
                    NodeResumeDecision(
                        node_id=node_id,
                        disposition=ResumeNodeDisposition.EXECUTE_FRESH,
                        invalidation_reason="missing_checkpoint",
                    )
                )
                continue

            if node_id in corrupted_output_node_ids or cp.output_digest_sha256 is None:
                if (
                    cp.side_effect_disposition == SideEffectDisposition.IN_FLIGHT_INDETERMINATE
                    and spec.resumability_class == NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT
                ):
                    invalidated_nodes.add(node_id)
                    reexecuted_ids.append(node_id)
                    decisions.append(
                        NodeResumeDecision(
                            node_id=node_id,
                            disposition=ResumeNodeDisposition.REEXECUTE_IDEMPOTENT,
                            invalidation_reason="indeterminate_idempotent_retry",
                        )
                    )
                    continue
                if (
                    cp.side_effect_disposition == SideEffectDisposition.COMMITTED
                    and spec.resumability_class == NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT
                ):
                    raise CheckpointNonIdempotentInvalidationError(
                        f"Corrupted output for committed non-idempotent node {node_id!r} "
                        "cannot be automatically re-executed"
                    )
                invalidated_nodes.add(node_id)
                reexecuted_ids.append(node_id)
                decisions.append(
                    NodeResumeDecision(
                        node_id=node_id,
                        disposition=ResumeNodeDisposition.EXECUTE_FRESH,
                        invalidation_reason="corrupted_or_incomplete_output",
                    )
                )
                continue

            # Valid, complete checkpoint with intact upstream cone -> reuse!
            reused_ids.append(node_id)
            decisions.append(
                NodeResumeDecision(
                    node_id=node_id,
                    disposition=ResumeNodeDisposition.REUSE_CHECKPOINT,
                    reused_checkpoint=cp,
                )
            )

        return WorkflowResumePlan(
            workflow_plan_fingerprint=self.workflow_plan_fingerprint,
            target_attempt_number=target_attempt_number,
            target_execution_generation=target_execution_generation,
            decisions=tuple(decisions),
            reused_node_ids=tuple(reused_ids),
            reexecuted_node_ids=tuple(reexecuted_ids),
        )


__all__ = [
    "CheckpointConfigurationError",
    "CheckpointFencingError",
    "CheckpointIndeterminateSideEffectError",
    "CheckpointNonIdempotentInvalidationError",
    "CheckpointPlanMismatchError",
    "CheckpointableNodeSpec",
    "DurableStepCheckpointEnvelope",
    "NodeResumabilityClass",
    "NodeResumeDecision",
    "ResumeNodeDisposition",
    "SideEffectDisposition",
    "WorkflowCheckpointError",
    "WorkflowCheckpointResumePlanner",
    "WorkflowResumePlan",
]
