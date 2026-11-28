"""Regression tests for experimental workflow state-machine invariants (#829)."""

from __future__ import annotations

from typing import Any

import pytest

from django_ray.workflow import checkpoints, concurrency, map_transport, paged_input
from django_ray.workflow.checkpoints import (
    CheckpointableNodeSpec,
    CheckpointConfigurationError,
    CheckpointFencingError,
    CheckpointNonIdempotentInvalidationError,
    CheckpointPlanMismatchError,
    DurableStepCheckpointEnvelope,
    NodeResumabilityClass,
    ResumeNodeDisposition,
    SideEffectDisposition,
    WorkflowCheckpointResumePlanner,
)
from django_ray.workflow.concurrency import (
    ConcurrencyScopeSpec,
    ExternalServiceBudgetSpec,
    OverlapPolicy,
    RunOwnerIdentity,
    SharedServiceBudgetController,
    WorkflowConcurrencyConfigurationError,
    WorkflowConcurrencyCoordinator,
)
from django_ray.workflow.map_transport import (
    BoundedMapListAggregator,
    InMemoryChunkObjectStore,
    MapResultTransportError,
)
from django_ray.workflow.paged_input import (
    BoundedPagedMapInputStream,
    PageBatch,
    PagedMapInputConfig,
    PagedMapInputConfigurationError,
    PagedMapInputLimitError,
    PagedSourceThrottledError,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
PLAN_FP = "plan-fp-829"


def _cp(
    node_id: str,
    callable_path: str = "app.tasks.step",
    *,
    resumability: NodeResumabilityClass = NodeResumabilityClass.PURE_DETERMINISTIC,
    side_effect: SideEffectDisposition = SideEffectDisposition.NONE,
    input_digest: str = DIGEST_A,
    output_digest: str | None = DIGEST_B,
    output_reference: str | None = "resfs://sha256/out?bytes=16",
    attempt: int = 1,
    gen: int = 0,
    idempotency_key: str | None = None,
) -> DurableStepCheckpointEnvelope:
    return DurableStepCheckpointEnvelope(
        workflow_plan_fingerprint=PLAN_FP,
        node_id=node_id,
        callable_path=callable_path,
        resumability_class=resumability,
        side_effect_disposition=side_effect,
        input_digest_sha256=input_digest,
        output_digest_sha256=output_digest,
        output_reference=output_reference,
        source_attempt_number=attempt,
        source_execution_generation=gen,
        idempotency_key=idempotency_key,
    )


def _owner(pk: int, *, attempt: int = 1, gen: int = 0, priority: int = 0) -> RunOwnerIdentity:
    return RunOwnerIdentity(
        task_execution_pk=pk,
        attempt_number=attempt,
        execution_generation=gen,
        priority=priority,
    )


class _ListPagedSource:
    def __init__(self, batches: list[PageBatch]) -> None:
        self._batches = list(batches)
        self._index = 0
        self.closed = False

    def fetch_page(
        self,
        *,
        cursor: str | None,
        page_size: int,
        snapshot_token: str | None,
    ) -> PageBatch:
        batch = self._batches[self._index]
        self._index += 1
        return batch

    def close(self) -> None:
        self.closed = True


def test_experimental_workflow_modules_document_unwired_boundary() -> None:
    for mod in (checkpoints, concurrency, paged_input, map_transport):
        doc = (mod.__doc__ or "").lower()
        assert "experimental" in doc
        assert "not yet" in doc and "wired" in doc


# ---------------------------------------------------------------------------
# checkpoints.py invariants
# ---------------------------------------------------------------------------


def test_checkpoints_support_generation_zero_auto_retry_and_manual_retry() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec("step-1", "app.tasks.s1"),
            CheckpointableNodeSpec("step-2", "app.tasks.s2", upstream_node_ids=("step-1",)),
        ],
    )

    # Auto-retry keeps execution_generation=0 and increments attempt_number (1 -> 2).
    auto_plan = planner.build_resume_plan(
        target_attempt_number=2,
        target_execution_generation=0,
        checkpoints=[_cp("step-1", "app.tasks.s1", attempt=1, gen=0)],
    )
    assert auto_plan.reused_node_ids == ("step-1",)
    assert auto_plan.reexecuted_node_ids == ("step-2",)

    # Manual retry increments execution_generation (0 -> 1) and attempt_number (1 -> 2).
    manual_plan = planner.build_resume_plan(
        target_attempt_number=2,
        target_execution_generation=1,
        checkpoints=[_cp("step-1", "app.tasks.s1", attempt=1, gen=0)],
    )
    assert manual_plan.reused_node_ids == ("step-1",)
    assert manual_plan.reexecuted_node_ids == ("step-2",)

    # Manual retry after prior auto-retry: (gen=0, att=2) -> (gen=1, att=3).
    second_manual = planner.build_resume_plan(
        target_attempt_number=3,
        target_execution_generation=1,
        checkpoints=[_cp("step-1", "app.tasks.s1", attempt=2, gen=0)],
    )
    assert second_manual.reused_node_ids == ("step-1",)

    # Negative generation, bool generation, equal or future authority must fail closed.
    with pytest.raises(CheckpointConfigurationError, match="non-negative"):
        _cp("step-1", "app.tasks.s1", attempt=1, gen=-1).validated()
    with pytest.raises(CheckpointConfigurationError, match="non-negative"):
        _cp("step-1", "app.tasks.s1", attempt=1, gen=True).validated()  # type: ignore[arg-type]
    with pytest.raises(CheckpointFencingError, match="target_execution_generation >= 0"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=-1,
            checkpoints=[],
        )
    for bad_cp in (
        _cp("step-1", "app.tasks.s1", attempt=2, gen=0),  # equal (gen=0, att=2)
        _cp("step-1", "app.tasks.s1", attempt=3, gen=0),  # future attempt same gen
        _cp("step-1", "app.tasks.s1", attempt=1, gen=1),  # future gen
    ):
        with pytest.raises(CheckpointFencingError, match="non-prior authority"):
            planner.build_resume_plan(
                target_attempt_number=2,
                target_execution_generation=0,
                checkpoints=[bad_cp],
            )


def test_checkpoints_reject_resumability_class_and_idempotency_key_mismatch() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec(
                "apply",
                "app.tasks.apply",
                resumability_class=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                idempotency_key="idem-expected",
            ),
        ],
    )

    with pytest.raises(CheckpointPlanMismatchError, match="resumability_class"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[
                _cp(
                    "apply",
                    "app.tasks.apply",
                    resumability=NodeResumabilityClass.PURE_DETERMINISTIC,
                )
            ],
        )

    with pytest.raises(CheckpointPlanMismatchError, match="idempotency_key"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[
                _cp(
                    "apply",
                    "app.tasks.apply",
                    resumability=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.COMMITTED,
                    idempotency_key="idem-different",
                )
            ],
        )


def test_checkpoints_invalidate_on_input_digest_mismatch_and_missing_output_ref() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec("a", "app.tasks.a"),
            CheckpointableNodeSpec("b", "app.tasks.b", upstream_node_ids=("a",)),
        ],
    )

    # Input digest drift invalidates node and its downstream cone.
    plan = planner.build_resume_plan(
        target_attempt_number=2,
        target_execution_generation=0,
        checkpoints=[
            _cp("a", "app.tasks.a", input_digest=DIGEST_A),
            _cp("b", "app.tasks.b", input_digest=DIGEST_A),
        ],
        expected_input_digests={"a": DIGEST_C},
    )
    assert plan.reused_node_ids == ()
    assert plan.reexecuted_node_ids == ("a", "b")
    assert plan.decisions[0].invalidation_reason == "input_digest_mismatch"
    assert plan.decisions[1].disposition == ResumeNodeDisposition.UPSTREAM_INVALIDATED

    # Missing or blank output_reference must never be reused even when output_digest is set.
    for bad_ref in (None, "", "   "):
        ref_plan = planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[
                _cp("a", "app.tasks.a", output_digest=DIGEST_B, output_reference=bad_ref),
                _cp("b", "app.tasks.b"),
            ],
        )
        assert ref_plan.reused_node_ids == ()
        assert ref_plan.decisions[0].invalidation_reason == "corrupted_or_incomplete_output"

    # Unknown node or malformed digest in expected_input_digests fails configuration check.
    with pytest.raises(CheckpointConfigurationError, match="unknown nodes"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[],
            expected_input_digests={"missing": DIGEST_A},
        )
    with pytest.raises(CheckpointConfigurationError, match="sha256"):
        planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[],
            expected_input_digests={"a": "not-a-digest"},
        )

    # Committed non-idempotent node fails closed on input digest drift or missing output_reference.
    non_idem_planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec(
                "charge",
                "app.tasks.charge",
                resumability_class=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
            )
        ],
    )
    with pytest.raises(CheckpointNonIdempotentInvalidationError, match="Input digest changed"):
        non_idem_planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[
                _cp(
                    "charge",
                    "app.tasks.charge",
                    resumability=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.COMMITTED,
                    input_digest=DIGEST_A,
                )
            ],
            expected_input_digests={"charge": DIGEST_C},
        )
    with pytest.raises(CheckpointNonIdempotentInvalidationError, match="Corrupted output"):
        non_idem_planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[
                _cp(
                    "charge",
                    "app.tasks.charge",
                    resumability=NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT,
                    side_effect=SideEffectDisposition.COMMITTED,
                    output_reference=None,
                )
            ],
        )


def test_checkpoints_never_reuse_uncommitted_rolled_back_or_indeterminate_nodes() -> None:
    # Even when output_digest_sha256 and output_reference are present, non-committed
    # side effects or indeterminate checkpoints must never be reused.
    idem_planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[
            CheckpointableNodeSpec(
                "sync",
                "app.tasks.sync",
                resumability_class=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                idempotency_key="idem-1",
            )
        ],
    )
    indet_plan = idem_planner.build_resume_plan(
        target_attempt_number=2,
        target_execution_generation=0,
        checkpoints=[
            _cp(
                "sync",
                "app.tasks.sync",
                resumability=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                side_effect=SideEffectDisposition.IN_FLIGHT_INDETERMINATE,
                output_digest=DIGEST_B,
                output_reference="resfs://sha256/out?bytes=16",
                idempotency_key="idem-1",
            )
        ],
    )
    assert indet_plan.reused_node_ids == ()
    assert indet_plan.decisions[0].disposition == ResumeNodeDisposition.REEXECUTE_IDEMPOTENT

    for non_committed in (
        SideEffectDisposition.NOT_STARTED,
        SideEffectDisposition.ROLLED_BACK,
        SideEffectDisposition.NONE,
    ):
        plan = idem_planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[
                _cp(
                    "sync",
                    "app.tasks.sync",
                    resumability=NodeResumabilityClass.IDEMPOTENT_SIDE_EFFECT,
                    side_effect=non_committed,
                    idempotency_key="idem-1",
                )
            ],
        )
        assert plan.reused_node_ids == ()
        assert plan.decisions[0].disposition == ResumeNodeDisposition.EXECUTE_FRESH

    pure_planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint=PLAN_FP,
        nodes=[CheckpointableNodeSpec("pure", "app.tasks.pure")],
    )
    for bad_disp in (
        SideEffectDisposition.NOT_STARTED,
        SideEffectDisposition.ROLLED_BACK,
        SideEffectDisposition.IN_FLIGHT_INDETERMINATE,
    ):
        pure_plan = pure_planner.build_resume_plan(
            target_attempt_number=2,
            target_execution_generation=0,
            checkpoints=[_cp("pure", "app.tasks.pure", side_effect=bad_disp)],
        )
        assert pure_plan.reused_node_ids == ()
        assert pure_plan.decisions[0].disposition == ResumeNodeDisposition.EXECUTE_FRESH


# ---------------------------------------------------------------------------
# concurrency.py invariants
# ---------------------------------------------------------------------------


def test_concurrency_lease_expiry_promotes_queued_waiter_without_deadlock() -> None:
    coord = WorkflowConcurrencyCoordinator()
    scope = "tenant/deadlock-regression"
    coord.register_scope(
        ConcurrencyScopeSpec(
            scope_key=scope,
            overlap_policy=OverlapPolicy.QUEUE,
            max_concurrent_runs=1,
            max_queued_runs=4,
            lease_ttl_seconds=5.0,
        )
    )
    owner_a = _owner(1)
    owner_b = _owner(2)
    owner_c = _owner(3)

    adm_a = coord.request_admission(scope, owner_a, now_monotonic=0.0)
    assert adm_a.status == "admitted" and adm_a.lease is not None
    adm_b = coord.request_admission(scope, owner_b, now_monotonic=1.0)
    assert adm_b.status == "queued" and adm_b.queue_position == 1

    # Once owner_a's lease expires at t=5.0, owner_b polling request_admission must be promoted.
    adm_b_after_expiry = coord.request_admission(scope, owner_b, now_monotonic=5.0)
    assert adm_b_after_expiry.status == "admitted"
    assert adm_b_after_expiry.lease is not None
    assert adm_b_after_expiry.lease.owner == owner_b
    assert adm_b_after_expiry.lease.fencing_token > adm_a.lease.fencing_token
    assert len(coord.reclaimed_expired_leases) == 1

    # Also verify third-party arrival promotes the waiting head rather than deadlocking the scope.
    coord2 = WorkflowConcurrencyCoordinator()
    coord2.register_scope(
        ConcurrencyScopeSpec(
            scope_key=scope,
            overlap_policy=OverlapPolicy.QUEUE,
            max_concurrent_runs=1,
            max_queued_runs=4,
            lease_ttl_seconds=5.0,
        )
    )
    coord2.request_admission(scope, owner_a, now_monotonic=0.0)
    coord2.request_admission(scope, owner_b, now_monotonic=1.0)
    adm_c = coord2.request_admission(scope, owner_c, now_monotonic=6.0)
    assert adm_c.status == "queued" and adm_c.queue_position == 1
    # owner_b is now active with a fresh lease!
    adm_b_check = coord2.request_admission(scope, owner_b, now_monotonic=6.0)
    assert adm_b_check.status == "admitted" and adm_b_check.lease is not None


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), float("-inf"), -1.0, True, False, "5", None, 10**400],
)
def test_concurrency_throttle_feedback_rejects_non_finite_and_invalid_values(
    bad_value: Any,
) -> None:
    budget = SharedServiceBudgetController()
    budget.register_service(ExternalServiceBudgetSpec(service_key="ext-api"))

    with pytest.raises(WorkflowConcurrencyConfigurationError, match="retry_after_seconds"):
        budget.record_throttle_feedback(
            "ext-api",
            retry_after_seconds=bad_value,
            now_monotonic=1.0,
        )
    assert budget.throttle_events_recorded["ext-api"] == 0

    for bad_now in (float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(WorkflowConcurrencyConfigurationError, match="now_monotonic"):
            budget.record_throttle_feedback(
                "ext-api",
                retry_after_seconds=1.0,
                now_monotonic=bad_now,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# paged_input.py invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_retry_after",
    [float("nan"), float("inf"), float("-inf"), -0.1, True, False, "2", None, 10**400],
)
def test_paged_source_throttled_error_rejects_invalid_retry_after(bad_retry_after: Any) -> None:
    with pytest.raises(PagedMapInputConfigurationError, match="retry_after_seconds"):
        PagedSourceThrottledError(retry_after_seconds=bad_retry_after)


def test_paged_input_enforces_page_batch_byte_size_bounds_and_accounting() -> None:
    # Oversized declared PageBatch.byte_size fails closed even when items look tiny.
    oversized_source = _ListPagedSource([PageBatch(items=("a",), byte_size=200)])
    oversized_stream = BoundedPagedMapInputStream(
        oversized_source,
        PagedMapInputConfig(page_size=2, max_buffered_items=4, max_buffered_bytes=128),
    )
    with pytest.raises(PagedMapInputLimitError, match="batch byte_size"):
        oversized_stream.next_item()
    assert oversized_source.closed is True
    assert oversized_stream.diagnostics().aborted is True

    # Invalid negative/bool/float PageBatch.byte_size fails closed.
    for bad_size in (-1, True, 12.5):
        bad_source = _ListPagedSource([PageBatch(items=("a",), byte_size=bad_size)])  # type: ignore[arg-type]
        bad_stream = BoundedPagedMapInputStream(
            bad_source,
            PagedMapInputConfig(page_size=2, max_buffered_items=4, max_buffered_bytes=128),
        )
        with pytest.raises(PagedMapInputConfigurationError, match="PageBatch.byte_size"):
            bad_stream.next_item()
        assert bad_source.closed is True

    # Valid PageBatch.byte_size contributes to buffered/in-flight byte budget with zero drift.
    source = _ListPagedSource(
        [
            PageBatch(items=("x", "y"), next_cursor="p2", byte_size=100),
            PageBatch(items=("z",), next_cursor=None, byte_size=50),
        ]
    )
    stream = BoundedPagedMapInputStream(
        source,
        PagedMapInputConfig(page_size=2, max_buffered_items=4, max_buffered_bytes=128),
    )
    assert stream.prefetch_page() is True
    assert stream.total_outstanding_bytes == 100
    assert stream.diagnostics().peak_buffered_bytes == 100

    # Prefetching page 2 (50 B) while 100 B remain outstanding breaches 128 B cap.
    with pytest.raises(PagedMapInputLimitError, match="batch byte_size"):
        stream.prefetch_page()
    assert source.closed is True

    # Draining and acknowledging all items returns outstanding bytes cleanly to 0.
    clean_source = _ListPagedSource([PageBatch(items=("x", "y", "z"), byte_size=90)])
    clean_stream = BoundedPagedMapInputStream(
        clean_source,
        PagedMapInputConfig(page_size=3, max_buffered_items=3, max_buffered_bytes=128),
    )
    item0 = clean_stream.next_item()
    item1 = clean_stream.next_item()
    item2 = clean_stream.next_item()
    assert item0 is not None and item1 is not None and item2 is not None
    assert item0.byte_size + item1.byte_size + item2.byte_size == 90
    assert clean_stream.total_outstanding_bytes == 90
    assert clean_stream.diagnostics().buffered_bytes == 0
    clean_stream.acknowledge_item(item0.index)
    clean_stream.acknowledge_item(item1.index)
    clean_stream.acknowledge_item(item2.index)
    assert clean_stream.total_outstanding_bytes == 0

    # Empty page with declared byte_size tracks peak and enforces cap.
    empty_source = _ListPagedSource([PageBatch(items=(), byte_size=64)])
    empty_stream = BoundedPagedMapInputStream(
        empty_source,
        PagedMapInputConfig(page_size=2, max_buffered_items=2, max_buffered_bytes=128),
    )
    assert empty_stream.next_item() is None
    assert empty_stream.diagnostics().peak_buffered_bytes == 64


# ---------------------------------------------------------------------------
# map_transport.py invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_size", [True, False, -1, 1.5, float("nan"), "10"])
def test_map_transport_rejects_bool_and_invalid_byte_sizes(bad_size: Any) -> None:
    aggregator = BoundedMapListAggregator(2)
    with pytest.raises(MapResultTransportError, match="byte_size must be non-negative"):
        aggregator.record_leaf_completion(0, "val", byte_size=bad_size)

    store = InMemoryChunkObjectStore()
    with pytest.raises(MapResultTransportError, match="metadata must agree"):
        store.put_chunk(["val"], start_index=0, end_index=0, byte_size=bad_size)


def test_map_transport_rejects_bool_or_negative_chunk_indices() -> None:
    store = InMemoryChunkObjectStore()
    for start_idx, end_idx in ((True, True), (-1, -1), (False, 0)):
        with pytest.raises(MapResultTransportError, match="metadata must agree"):
            store.put_chunk(["val"], start_index=start_idx, end_index=end_idx, byte_size=1)  # type: ignore[arg-type]
