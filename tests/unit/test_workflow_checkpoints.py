"""Unit tests for the Ray-free durable step checkpoint state machine (#289)."""

from __future__ import annotations

import pytest

from django_ray.workflow.checkpoints import (
    CheckpointableNodeSpec,
    CheckpointConfigurationError,
    CheckpointFencingError,
    CheckpointIndeterminateSideEffectError,
    CheckpointNonIdempotentInvalidationError,
    CheckpointPlanMismatchError,
    DurableStepCheckpointEnvelope,
    NodeResumabilityClass,
    ResumeNodeDisposition,
    SideEffectDisposition,
    WorkflowCheckpointResumePlanner,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
PLAN_FP = "plan-fp-289"


def _cp(
    node_id: str,
    callable_path: str = "app.tasks.step",
    *,
    resumability: NodeResumabilityClass = NodeResumabilityClass.PURE_DETERMINISTIC,
    side_effect: SideEffectDisposition = SideEffectDisposition.NONE,
    input_digest: str = DIGEST_A,
    output_digest: str | None = DIGEST_B,
    attempt: int = 1,
    gen: int = 1,
    idempotency_key: str | None = None,
    plan_fp: str = PLAN_FP,
) -> DurableStepCheckpointEnvelope:
    return DurableStepCheckpointEnvelope(
        workflow_plan_fingerprint=plan_fp,
        node_id=node_id,
        callable_path=callable_path,
        resumability_class=resumability,
        side_effect_disposition=side_effect,
        input_digest_sha256=input_digest,
        output_digest_sha256=output_digest,
        output_reference="resfs://sha256/out?bytes=16" if output_digest else None,
        source_attempt_number=attempt,
        source_execution_generation=gen,
        idempotency_key=idempotency_key,
    )


def test_partial_resume_reuses_completed_prefix_and_invalidates_downstream_cone() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec("step-1", "app.tasks.s1"),
            CheckpointableNodeSpec("step-2", "app.tasks.s2", upstream_node_ids=("step-1",)),
            CheckpointableNodeSpec("step-3", "app.tasks.s3", upstream_node_ids=("step-2",)),
        ],
    )

    # step-1 and step-2 succeeded in attempt 1; step-3 was missing.
    plan = planner.build_resume_plan(
        target_attempt_number=2,
        target_execution_generation=2,
        checkpoints=[
            _cp("step-1", "app.tasks.s1"),
            _cp("step-2", "app.tasks.s2"),
        ],
    )
    assert plan.reused_node_ids == ("step-1", "step-2")
    assert plan.reexecuted_node_ids == ("step-3",)

    # If step-1 output is corrupted, both step-1 and downstream step-2/step-3 re-execute.
    cone_plan = planner.build_resume_plan(
        target_attempt_number=2,
        target_execution_generation=2,
        checkpoints=[
            _cp("step-1", "app.tasks.s1"),
            _cp("step-2", "app.tasks.s2"),
        ],
        corrupted_output_node_ids={"step-1"},
    )
    assert cone_plan.reused_node_ids == ()
    assert cone_plan.reexecuted_node_ids == ("step-1", "step-2", "step-3")
    assert cone_plan.decisions[1].disposition == ResumeNodeDisposition.UPSTREAM_INVALIDATED


def test_indeterminate_non_idempotent_side_effect_fails_closed() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec(
                "charge-card",
                "app.tasks.charge",
                resumability_class=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
            ),
        ],
    )
    with pytest.raises(
        CheckpointIndeterminateSideEffectError,
        match="IN_FLIGHT_INDETERMINATE non-idempotent side effect",
    ):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[
                _cp(
                    "charge-card",
                    "app.tasks.charge",
                    resumability=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.IN_FLIGHT_INDETERMINATE,
                    output_digest=None,
                )
            ],
        )


def test_indeterminate_idempotent_side_effect_with_matching_key_reexecutes() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec(
                "apply-k8s-manifest",
                "app.tasks.apply",
                resumability_class=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                idempotency_key="idem-cluster-1",
            ),
        ],
    )
    plan = planner.build_resume_plan(
        target_attempt_number=2,
        target_execution_generation=2,
        checkpoints=[
            _cp(
                "apply-k8s-manifest",
                "app.tasks.apply",
                resumability=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                side_effect=SideEffectDisposition.IN_FLIGHT_INDETERMINATE,
                output_digest=None,
                idempotency_key="idem-cluster-1",
            )
        ],
    )
    assert plan.decisions[0].disposition == ResumeNodeDisposition.REEXECUTE_IDEMPOTENT


def test_upstream_invalidation_reaching_committed_non_idempotent_node_fails_closed() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec(
                "prepare-session",
                "app.tasks.session",
                resumability_class=NodeResumabilityClass.EPHEMERAL_UNCHECKPOINTABLE,
            ),
            CheckpointableNodeSpec(
                "send-email",
                "app.tasks.email",
                resumability_class=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
                upstream_node_ids=("prepare-session",),
            ),
        ],
    )
    with pytest.raises(
        CheckpointNonIdempotentInvalidationError,
        match="Upstream invalidation reached committed non-idempotent node",
    ):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[
                _cp(
                    "send-email",
                    "app.tasks.email",
                    resumability=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.COMMITTED,
                )
            ],
        )


def test_fencing_and_plan_mismatch_refusals() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[CheckpointableNodeSpec("step-1", "app.tasks.s1")],
    )

    with pytest.raises(CheckpointPlanMismatchError):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[_cp("step-1", "app.tasks.s1", plan_fp="wrong-fp")],
        )

    with pytest.raises(CheckpointFencingError, match="non-prior authority"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[_cp("step-1", "app.tasks.s1", attempt=2, gen=2)],
        )

    with pytest.raises(CheckpointConfigurationError, match="cycle"):
        WorkflowCheckpointResumePlanner(
            workflow_plan_fingerprint=PLAN_FP,
            nodes=[
                CheckpointableNodeSpec("a", "app.a", upstream_node_ids=("b",)),
                CheckpointableNodeSpec("b", "app.b", upstream_node_ids=("a",)),
            ],
        )


def test_checkpoint_spec_envelope_and_forced_reexecute_edge_cases() -> None:
    with pytest.raises(CheckpointConfigurationError, match="node_id must be a non-empty"):
        CheckpointableNodeSpec("", "app.a").validated()
    with pytest.raises(CheckpointConfigurationError, match="callable_path must be a non-empty"):
        CheckpointableNodeSpec("a", "").validated()
    with pytest.raises(CheckpointConfigurationError, match="resumability_class"):
        CheckpointableNodeSpec("a", "app.a", resumability_class="bad").validated()  # type: ignore[arg-type]
    with pytest.raises(CheckpointConfigurationError, match="idempotency_key"):
        CheckpointableNodeSpec(
            "a",
            "app.a",
            resumability_class=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
        ).validated()

    with pytest.raises(CheckpointConfigurationError, match="workflow_plan_fingerprint"):
        _cp("a", "app.a", plan_fp="").validated()
    with pytest.raises(CheckpointConfigurationError, match="node_id is required"):
        _cp("", "app.a").validated()
    with pytest.raises(CheckpointConfigurationError, match="callable_path is required"):
        _cp("a", "").validated()
    with pytest.raises(CheckpointConfigurationError, match="input_digest_sha256"):
        _cp("a", "app.a", input_digest="bad").validated()
    with pytest.raises(CheckpointConfigurationError, match="output_digest_sha256"):
        _cp("a", "app.a", output_digest="bad").validated()
    with pytest.raises(CheckpointConfigurationError, match="positive ints"):
        _cp("a", "app.a", attempt=0).validated()

    with pytest.raises(CheckpointConfigurationError, match="workflow_plan_fingerprint"):
        WorkflowCheckpointResumePlanner(
            workflow_plan_fingerprint="",
            nodes=[CheckpointableNodeSpec("a", "app.a")],
        )
    with pytest.raises(CheckpointConfigurationError, match="nodes must be non-empty"):
        WorkflowCheckpointResumePlanner(workflow_plan_fingerprint=PLAN_FP, nodes=[])
    with pytest.raises(CheckpointConfigurationError, match="Duplicate node_id"):
        WorkflowCheckpointResumePlanner(
            workflow_plan_fingerprint=PLAN_FP,
            nodes=[
                CheckpointableNodeSpec("a", "app.a"),
                CheckpointableNodeSpec("a", "app.a"),
            ],
        )
    with pytest.raises(CheckpointConfigurationError, match="unknown upstream node"):
        WorkflowCheckpointResumePlanner(
            workflow_plan_fingerprint=PLAN_FP,
            nodes=[CheckpointableNodeSpec("a", "app.a", upstream_node_ids=("missing",))],
        )

    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec("a", "app.a"),
            CheckpointableNodeSpec(
                "b",
                "app.b",
                resumability_class=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                idempotency_key="idem-b",
                upstream_node_ids=("a",),
            ),
            CheckpointableNodeSpec(
                "c",
                "app.c",
                resumability_class=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
                upstream_node_ids=("b",),
            ),
        ],
    )
    with pytest.raises(CheckpointFencingError, match="target_attempt_number > 1"):
        planner.build_resume_plan(
            target_attempt_number=1,
            target_execution_generation=2,
            checkpoints=[],
        )
    with pytest.raises(CheckpointConfigurationError, match="unknown nodes"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[],
            force_reexecute_node_ids={"unknown"},
        )
    with pytest.raises(CheckpointPlanMismatchError, match="not in workflow plan"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[_cp("unknown", "app.x")],
        )
    with pytest.raises(CheckpointPlanMismatchError, match="callable_path"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[_cp("a", "app.wrong")],
        )
    with pytest.raises(CheckpointIndeterminateSideEffectError, match="mismatched idempotency_key"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[
                _cp(
                    "b",
                    "app.b",
                    resumability=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.IN_FLIGHT_INDETERMINATE,
                    idempotency_key="wrong-key",
                )
            ],
        )
    with pytest.raises(CheckpointNonIdempotentInvalidationError, match="Cannot force re-execution"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[
                _cp("a", "app.a"),
                _cp(
                    "b",
                    "app.b",
                    resumability=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.COMMITTED,
                    idempotency_key="idem-b",
                ),
                _cp(
                    "c",
                    "app.c",
                    resumability=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.COMMITTED,
                ),
            ],
            force_reexecute_node_ids={"c"},
        )
    with pytest.raises(CheckpointNonIdempotentInvalidationError, match="Corrupted output"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=2,
            checkpoints=[
                _cp("a", "app.a"),
                _cp(
                    "b",
                    "app.b",
                    resumability=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.COMMITTED,
                    idempotency_key="idem-b",
                ),
                _cp(
                    "c",
                    "app.c",
                    resumability=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.COMMITTED,
                ),
            ],
            corrupted_output_node_ids={"c"},
        )

    plan = planner.build_resume_plan(
        target_attempt_number=2,
        target_execution_generation=2,
        checkpoints=[_cp("a", "app.a")],
        force_reexecute_node_ids={"a"},
    )
    assert plan.decisions[0].disposition is ResumeNodeDisposition.FORCED_REEXECUTE
