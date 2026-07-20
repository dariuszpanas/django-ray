"""Executable contract tests for the Ray-free Compiled Graph lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

import django_ray.runtime.compiled_graph_lifecycle as lifecycle_module
from django_ray.runtime.compiled_graph_lifecycle import (
    COMPILED_GRAPH_LIFECYCLE_MAX_OUTPUTS,
    COMPILED_GRAPH_LIFECYCLE_MAX_SNAPSHOT_BYTES,
    COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION,
    CompiledGraphInvocationState,
    CompiledGraphLifecycleAdapter,
    CompiledGraphLifecycleState,
    CompiledGraphSessionState,
    LifecycleAction,
    LifecycleActionKind,
    LifecycleCapacity,
    LifecycleCleanupCode,
    LifecycleDeadlines,
    LifecycleEffectState,
    LifecycleEvent,
    LifecycleEventKind,
    LifecycleGraphDisposition,
    LifecycleOutcome,
    LifecycleOutputSlot,
    LifecycleOutputState,
    LifecycleRejection,
    LifecycleRejectionCode,
    LifecycleRetryDisposition,
    LifecycleTransition,
    initial_session,
    lifecycle_snapshot,
    reduce_lifecycle,
)
from django_ray.runtime.context import WorkflowInvocationIdentity, WorkflowRunIdentity

RUN_IDENTITY = WorkflowRunIdentity(
    task_execution_pk=17,
    attempt_number=2,
    execution_generation=5,
    run_id="00000000-0000-0000-0000-000000000201",
)
INVOCATION_IDENTITY = WorkflowInvocationIdentity(
    run_identity=RUN_IDENTITY,
    invocation_id="00000000-0000-0000-0000-000000000202",
)
OTHER_INVOCATION_IDENTITY = WorkflowInvocationIdentity(
    run_identity=RUN_IDENTITY,
    invocation_id="00000000-0000-0000-0000-000000000203",
)
DEADLINES = LifecycleDeadlines(
    outer=100.0,
    admission=30.0,
    submission=40.0,
    result=50.0,
    cancellation=60.0,
    drain=70.0,
    teardown=80.0,
)


def _transition(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition:
    result = reduce_lifecycle(state, event)
    assert isinstance(result, LifecycleTransition), result
    return result


def _command(
    state: CompiledGraphLifecycleState,
    kind: LifecycleEventKind,
    observed_at: float,
    *,
    invocation: WorkflowInvocationIdentity | None = None,
    declared_outputs: int | None = None,
    output_index: int | None = None,
) -> LifecycleTransition:
    return _transition(
        state,
        LifecycleEvent(
            kind=kind,
            session_identity=RUN_IDENTITY,
            invocation_identity=invocation,
            observed_at=observed_at,
            declared_outputs=declared_outputs,
            output_index=output_index,
        ),
    )


def _callback(
    state: CompiledGraphLifecycleState,
    kind: LifecycleEventKind,
    observed_at: float,
    *,
    token: str | None = None,
    invocation: WorkflowInvocationIdentity | None = None,
    output_index: int | None = None,
) -> LifecycleTransition:
    action = state.pending_action
    assert action is not None
    selected_output = output_index
    if action.kind is LifecycleActionKind.CONSUME_OUTPUT and selected_output is None:
        selected_output = action.output_index
    return _transition(
        state,
        LifecycleEvent(
            kind=kind,
            session_identity=RUN_IDENTITY,
            invocation_identity=(action.invocation_identity if invocation is None else invocation),
            observed_at=observed_at,
            action_token=action.token if token is None else token,
            output_index=selected_output,
        ),
    )


def _ready_session(*, deadlines: LifecycleDeadlines = DEADLINES) -> CompiledGraphLifecycleState:
    state = initial_session(RUN_IDENTITY, deadlines)
    state = _command(state, LifecycleEventKind.VALIDATION_REQUESTED, 1.0).state
    state = _callback(state, LifecycleEventKind.VALIDATED, 2.0).state
    state = _command(state, LifecycleEventKind.PREPARATION_REQUESTED, 3.0).state
    state = _callback(state, LifecycleEventKind.PREPARED, 4.0).state
    return state


def _admitted_session(
    *,
    outputs: int = 1,
    deadlines: LifecycleDeadlines = DEADLINES,
) -> CompiledGraphLifecycleState:
    state = _ready_session(deadlines=deadlines)
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=outputs,
    ).state
    return _callback(state, LifecycleEventKind.ADMITTED, 6.0).state


def _running_session(
    *,
    outputs: int = 1,
    deadlines: LifecycleDeadlines = DEADLINES,
) -> CompiledGraphLifecycleState:
    state = _admitted_session(outputs=outputs, deadlines=deadlines)
    state = _command(
        state,
        LifecycleEventKind.SUBMISSION_REQUESTED,
        7.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    return _callback(state, LifecycleEventKind.SUBMITTED, 8.0).state


def _assert_rejection(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
    code: LifecycleRejectionCode,
) -> LifecycleRejection:
    result = reduce_lifecycle(state, event)
    assert isinstance(result, LifecycleRejection)
    assert result.code is code
    assert len(result.code.value) <= 64
    assert len(result.message) <= 256
    return result


def test_success_consumes_each_output_once_before_health_and_reuse() -> None:
    state = _running_session(outputs=2)
    assert state.strategy_fallback_allowed is False
    assert state.retry_disposition is LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED
    assert state.effect_state is LifecycleEffectState.MAY_HAVE_STARTED

    first = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    )
    assert first.actions[0].kind is LifecycleActionKind.CONSUME_OUTPUT
    assert first.actions[0].deadline == DEADLINES.result
    state = _callback(first.state, LifecycleEventKind.OUTPUT_CONSUMED, 10.0).state
    assert state.outputs[0].state is LifecycleOutputState.CONSUMED
    assert state.graph_disposition is LifecycleGraphDisposition.PREPARED

    _assert_rejection(
        state,
        LifecycleEvent(
            kind=LifecycleEventKind.CONSUMPTION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=11.0,
            output_index=0,
        ),
        LifecycleRejectionCode.OUTPUT_ALREADY_CLAIMED,
    )

    state = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        12.0,
        invocation=INVOCATION_IDENTITY,
        output_index=1,
    ).state
    completed = _callback(state, LifecycleEventKind.OUTPUT_CONSUMED, 13.0)
    assert completed.actions[0].kind is LifecycleActionKind.CHECK_HEALTH
    assert completed.actions[0].invocation_identity == INVOCATION_IDENTITY
    assert completed.state.primary_outcome is LifecycleOutcome.SUCCEEDED
    assert completed.state.graph_disposition is LifecycleGraphDisposition.PREPARED

    state = _callback(completed.state, LifecycleEventKind.HEALTHY, 14.0).state
    assert state.session_state is CompiledGraphSessionState.READY
    assert state.invocation_state is CompiledGraphInvocationState.TERMINAL
    assert state.graph_disposition is LifecycleGraphDisposition.REUSABLE
    assert state.retry_disposition is LifecycleRetryDisposition.PROHIBITED


def test_action_tokens_are_deterministic_bounded_and_do_not_retain_history() -> None:
    state = initial_session(RUN_IDENTITY, DEADLINES)
    requested = _command(state, LifecycleEventKind.VALIDATION_REQUESTED, 1.0)
    action = requested.actions[0]
    assert action.token == "cg-lifecycle-v1:1:VALIDATE"
    assert action.deadline == DEADLINES.outer

    accepted = _callback(requested.state, LifecycleEventKind.VALIDATED, 2.0)
    duplicate_event = LifecycleEvent(
        kind=LifecycleEventKind.VALIDATED,
        session_identity=RUN_IDENTITY,
        observed_at=2.0,
        action_token=action.token,
    )
    _assert_rejection(
        accepted.state,
        duplicate_event,
        LifecycleRejectionCode.DUPLICATE_ACTION,
    )

    preparing = _command(
        accepted.state,
        LifecycleEventKind.PREPARATION_REQUESTED,
        3.0,
    )
    stale_event = replace(duplicate_event, observed_at=3.0, action_token="old-action")
    _assert_rejection(
        preparing.state,
        stale_event,
        LifecycleRejectionCode.STALE_ACTION_TOKEN,
    )
    snapshot = lifecycle_snapshot(preparing.state)
    assert "action_history" not in snapshot
    assert snapshot["next_action_sequence"] == 3


def test_callback_token_and_transition_failures_are_stable() -> None:
    state = initial_session(RUN_IDENTITY, DEADLINES)
    requested = _command(state, LifecycleEventKind.VALIDATION_REQUESTED, 1.0)

    _assert_rejection(
        requested.state,
        LifecycleEvent(
            kind=LifecycleEventKind.VALIDATED,
            session_identity=RUN_IDENTITY,
            observed_at=2.0,
        ),
        LifecycleRejectionCode.ACTION_TOKEN_REQUIRED,
    )
    _assert_rejection(
        requested.state,
        LifecycleEvent(
            kind=LifecycleEventKind.PREPARED,
            session_identity=RUN_IDENTITY,
            observed_at=2.0,
            action_token=requested.actions[0].token,
        ),
        LifecycleRejectionCode.INVALID_TRANSITION,
    )
    _assert_rejection(
        requested.state,
        LifecycleEvent(
            kind=LifecycleEventKind.DEADLINE_EXPIRED,
            session_identity=RUN_IDENTITY,
            observed_at=2.0,
            action_token=requested.actions[0].token,
        ),
        LifecycleRejectionCode.DEADLINE_NOT_EXPIRED,
    )

    validated = _callback(requested.state, LifecycleEventKind.VALIDATED, 2.0).state
    preparing = _command(
        validated,
        LifecycleEventKind.PREPARATION_REQUESTED,
        3.0,
    ).state
    _assert_rejection(
        preparing,
        LifecycleEvent(
            kind=LifecycleEventKind.VALIDATED,
            session_identity=RUN_IDENTITY,
            observed_at=3.0,
            action_token=requested.actions[0].token,
        ),
        LifecycleRejectionCode.DUPLICATE_ACTION,
    )


def test_preparation_is_the_permanent_strategy_fallback_cutoff() -> None:
    initial = initial_session(RUN_IDENTITY, DEADLINES)
    validating = _command(initial, LifecycleEventKind.VALIDATION_REQUESTED, 1.0).state
    rejected = _callback(validating, LifecycleEventKind.VALIDATION_REJECTED, 2.0).state
    assert rejected.strategy_fallback_allowed is True

    validated = _callback(validating, LifecycleEventKind.VALIDATED, 2.0).state
    preparing = _command(
        validated,
        LifecycleEventKind.PREPARATION_REQUESTED,
        3.0,
    ).state
    assert preparing.strategy_fallback_allowed is False
    failed = _callback(preparing, LifecycleEventKind.PREPARATION_FAILED, 4.0).state
    assert failed.pending_action is not None
    assert failed.pending_action.kind is LifecycleActionKind.TEARDOWN
    assert failed.strategy_fallback_allowed is False
    torn_down = _callback(failed, LifecycleEventKind.TORN_DOWN, 5.0).state
    assert torn_down.strategy_fallback_allowed is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_owners", 2),
        ("callers", 2),
        ("maximum_in_flight", 2),
        ("maximum_buffered_results", 2),
        ("owner_concurrency", 2),
        ("queue_capacity", 1),
    ],
)
def test_v1_capacity_rejects_every_alternative(field: str, value: int) -> None:
    capacity = replace(LifecycleCapacity(), **{field: value})

    with pytest.raises(ValueError, match="exact single-owner capacity"):
        initial_session(RUN_IDENTITY, DEADLINES, capacity=capacity)


@pytest.mark.parametrize(
    "identity",
    [
        replace(RUN_IDENTITY, task_execution_pk=0),
        replace(RUN_IDENTITY, attempt_number=0),
        replace(RUN_IDENTITY, execution_generation=-1),
        replace(RUN_IDENTITY, run_id=""),
        replace(RUN_IDENTITY, run_id="r" * 129),
    ],
)
def test_initial_session_rejects_incomplete_or_unbounded_identity(
    identity: WorkflowRunIdentity,
) -> None:
    with pytest.raises(ValueError):
        initial_session(identity, DEADLINES)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outer", -1.0),
        ("admission", float("inf")),
        ("submission", float("nan")),
        ("result", True),
    ],
)
def test_initial_session_rejects_invalid_absolute_deadlines(
    field: str,
    value: object,
) -> None:
    deadlines = replace(DEADLINES, **{field: value})

    with pytest.raises(ValueError, match="deadline must be"):
        initial_session(RUN_IDENTITY, deadlines)


def test_initial_session_rejects_wrong_deadline_and_capacity_records() -> None:
    with pytest.raises(TypeError, match="LifecycleDeadlines"):
        initial_session(RUN_IDENTITY, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="LifecycleCapacity"):
        initial_session(RUN_IDENTITY, DEADLINES, capacity=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fields must be integers"):
        initial_session(
            RUN_IDENTITY,
            DEADLINES,
            capacity=replace(LifecycleCapacity(), session_owners=True),
        )


def test_explicit_capacity_rejection_is_not_an_admission_timeout() -> None:
    state = _ready_session()
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    rejected = _callback(state, LifecycleEventKind.CAPACITY_REJECTED, 6.0)

    assert rejected.actions == ()
    assert rejected.state.primary_outcome is LifecycleOutcome.CAPACITY_REJECTED
    assert rejected.state.graph_disposition is LifecycleGraphDisposition.PREPARED
    assert rejected.state.retry_disposition is LifecycleRetryDisposition.AUTOMATIC_ALLOWED
    assert rejected.state.outputs[0].state is LifecycleOutputState.NOT_CREATED


def test_admission_timeout_releases_ambiguous_late_capacity_before_retry() -> None:
    state = _ready_session()
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    timed_out = _callback(state, LifecycleEventKind.ADMISSION_TIMED_OUT, 30.0)

    assert timed_out.state.primary_outcome is LifecycleOutcome.ADMISSION_TIMEOUT
    assert timed_out.actions[0].kind is LifecycleActionKind.RELEASE_CAPACITY
    assert timed_out.state.invocation_state is CompiledGraphInvocationState.CANCELLING
    assert timed_out.state.graph_disposition is LifecycleGraphDisposition.PREPARED

    released = _callback(timed_out.state, LifecycleEventKind.CAPACITY_RELEASED, 31.0).state
    assert released.session_state is CompiledGraphSessionState.READY
    assert released.graph_disposition is LifecycleGraphDisposition.PREPARED
    assert released.retry_disposition is LifecycleRetryDisposition.AUTOMATIC_ALLOWED


def test_capacity_release_failure_quarantines_and_tears_down() -> None:
    state = _ready_session()
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    releasing = _command(
        state,
        LifecycleEventKind.CANCELLATION_REQUESTED,
        6.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    failed = _callback(releasing, LifecycleEventKind.CAPACITY_RELEASE_FAILED, 7.0)

    assert failed.state.cleanup_diagnostics == (LifecycleCleanupCode.CAPACITY_RELEASE_FAILED,)
    assert failed.actions[0].kind is LifecycleActionKind.TEARDOWN
    assert failed.actions[0].invocation_identity is None


def test_capacity_release_timeout_is_bounded_cleanup_not_a_new_primary() -> None:
    state = _ready_session()
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    releasing = _command(
        state,
        LifecycleEventKind.CANCELLATION_REQUESTED,
        6.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    timed_out = _callback(releasing, LifecycleEventKind.DEADLINE_EXPIRED, 60.0)

    assert timed_out.state.primary_outcome is LifecycleOutcome.CANCELLED_PRE_SUBMISSION
    assert timed_out.state.cleanup_diagnostics == (LifecycleCleanupCode.CAPACITY_RELEASE_TIMEOUT,)
    assert timed_out.actions[0].kind is LifecycleActionKind.TEARDOWN


def test_pre_submission_cancellation_releases_capacity_and_rejects_late_admission() -> None:
    state = _ready_session()
    admitting = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    )
    cancelled = _command(
        admitting.state,
        LifecycleEventKind.CANCELLATION_REQUESTED,
        6.0,
        invocation=INVOCATION_IDENTITY,
    )
    assert cancelled.state.primary_outcome is LifecycleOutcome.CANCELLED_PRE_SUBMISSION
    assert cancelled.state.outputs[0].state is LifecycleOutputState.NOT_CREATED
    assert cancelled.state.retry_disposition is LifecycleRetryDisposition.PROHIBITED
    assert cancelled.actions[0].kind is LifecycleActionKind.RELEASE_CAPACITY

    late = LifecycleEvent(
        kind=LifecycleEventKind.ADMITTED,
        session_identity=RUN_IDENTITY,
        invocation_identity=INVOCATION_IDENTITY,
        observed_at=7.0,
        action_token=admitting.actions[0].token,
    )
    _assert_rejection(cancelled.state, late, LifecycleRejectionCode.STALE_ACTION_TOKEN)


def test_submit_timeout_can_reuse_healthy_graph_but_never_auto_retry_invocation() -> None:
    state = _admitted_session()
    state = _command(
        state,
        LifecycleEventKind.SUBMISSION_REQUESTED,
        7.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    timed_out = _callback(state, LifecycleEventKind.SUBMISSION_TIMED_OUT, 40.0)
    assert timed_out.state.primary_outcome is LifecycleOutcome.SUBMIT_TIMEOUT
    assert timed_out.state.effect_state is LifecycleEffectState.MAY_HAVE_STARTED
    assert timed_out.actions[0].kind is LifecycleActionKind.DRAIN_INVOCATION

    drained = _callback(timed_out.state, LifecycleEventKind.DRAINED, 41.0)
    assert drained.actions[0].kind is LifecycleActionKind.CHECK_HEALTH
    healthy = _callback(drained.state, LifecycleEventKind.HEALTHY, 42.0).state
    assert healthy.graph_disposition is LifecycleGraphDisposition.REUSABLE
    assert healthy.retry_disposition is LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED


def test_result_timeout_never_retries_one_shot_consumption() -> None:
    state = _running_session()
    consuming = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    )
    timed_out = _callback(consuming.state, LifecycleEventKind.RESULT_TIMED_OUT, 50.0)
    assert timed_out.state.primary_outcome is LifecycleOutcome.GET_TIMEOUT
    assert timed_out.state.outputs[0].state is LifecycleOutputState.RELEASE_PENDING
    assert timed_out.state.graph_disposition is LifecycleGraphDisposition.REBUILD_REQUIRED
    assert timed_out.actions[0].kind is LifecycleActionKind.DRAIN_INVOCATION

    drained = _callback(timed_out.state, LifecycleEventKind.DRAINED, 51.0)
    assert drained.state.outputs[0].state is LifecycleOutputState.ADAPTER_RELEASED
    assert drained.actions[0].kind is LifecycleActionKind.TEARDOWN
    torn_down = _callback(drained.state, LifecycleEventKind.TORN_DOWN, 52.0).state
    assert torn_down.session_state is CompiledGraphSessionState.TORN_DOWN
    assert torn_down.graph_disposition is LifecycleGraphDisposition.TORN_DOWN


def test_application_error_separates_graph_health_from_retry_safety() -> None:
    state = _running_session()
    state = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    ).state
    failed = _callback(state, LifecycleEventKind.APPLICATION_ERROR, 10.0)
    assert failed.state.primary_outcome is LifecycleOutcome.APPLICATION_ERROR
    assert failed.state.outputs[0].state is LifecycleOutputState.CONSUMED

    drained = _callback(failed.state, LifecycleEventKind.DRAINED, 11.0)
    healthy = _callback(drained.state, LifecycleEventKind.HEALTHY, 12.0).state
    assert healthy.graph_disposition is LifecycleGraphDisposition.REUSABLE
    assert healthy.retry_disposition is LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED


def test_unhealthy_graph_is_torn_down_after_outputs_are_accounted() -> None:
    state = _running_session()
    state = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    ).state
    checking = _callback(state, LifecycleEventKind.OUTPUT_CONSUMED, 10.0)
    unhealthy = _callback(checking.state, LifecycleEventKind.UNHEALTHY, 11.0)

    assert unhealthy.state.cleanup_diagnostics == (LifecycleCleanupCode.HEALTH_CHECK_FAILED,)
    assert unhealthy.actions[0].kind is LifecycleActionKind.TEARDOWN


def test_health_deadline_cannot_make_an_unconfirmed_graph_reusable() -> None:
    state = _running_session()
    state = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    ).state
    checking = _callback(state, LifecycleEventKind.OUTPUT_CONSUMED, 10.0)
    expired = _callback(checking.state, LifecycleEventKind.DEADLINE_EXPIRED, 70.0)

    assert expired.state.graph_disposition is LifecycleGraphDisposition.REBUILD_REQUIRED
    assert expired.state.cleanup_diagnostics == (LifecycleCleanupCode.HEALTH_CHECK_FAILED,)
    assert expired.actions[0].kind is LifecycleActionKind.TEARDOWN


@pytest.mark.parametrize(
    ("event_kind", "outcome"),
    [
        (LifecycleEventKind.ACTOR_DIED, LifecycleOutcome.ACTOR_DIED),
        (LifecycleEventKind.CHANNEL_ERROR, LifecycleOutcome.CHANNEL_ERROR),
    ],
)
def test_actor_and_channel_failures_require_teardown_before_outputs_are_unavailable(
    event_kind: LifecycleEventKind,
    outcome: LifecycleOutcome,
) -> None:
    state = _running_session()
    state = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    ).state
    failed = _callback(state, event_kind, 10.0)

    assert failed.state.primary_outcome is outcome
    assert failed.state.outputs[0].state is LifecycleOutputState.CONSUMING
    assert failed.state.graph_disposition is LifecycleGraphDisposition.REBUILD_REQUIRED
    assert failed.actions[0].kind is LifecycleActionKind.TEARDOWN

    torn_down = _callback(failed.state, LifecycleEventKind.TORN_DOWN, 11.0).state
    assert torn_down.outputs[0].state is LifecycleOutputState.UNAVAILABLE_AFTER_TEARDOWN


def test_owner_loss_is_indeterminate_permanent_output_loss_without_reuse() -> None:
    state = _running_session()
    lost = _command(
        state,
        LifecycleEventKind.OWNER_LOST,
        9.0,
        invocation=INVOCATION_IDENTITY,
    )

    assert lost.actions == ()
    assert lost.state.primary_outcome is LifecycleOutcome.OWNER_LOST
    assert lost.state.outputs[0].state is LifecycleOutputState.LOST_WITH_OWNER
    assert lost.state.graph_disposition is LifecycleGraphDisposition.REBUILD_REQUIRED
    assert (
        lost.state.retry_disposition is LifecycleRetryDisposition.OPERATOR_RECONCILIATION_REQUIRED
    )


def test_owner_loss_before_submission_is_retry_safe_and_never_claims_effects() -> None:
    state = _admitted_session()
    lost = _command(
        state,
        LifecycleEventKind.OWNER_LOST,
        7.0,
        invocation=INVOCATION_IDENTITY,
    ).state

    assert lost.primary_outcome is LifecycleOutcome.OWNER_LOST
    assert lost.effect_state is LifecycleEffectState.NOT_STARTED
    assert lost.outputs[0].state is LifecycleOutputState.NOT_CREATED
    assert lost.retry_disposition is LifecycleRetryDisposition.AUTOMATIC_ALLOWED
    assert lost.strategy_fallback_allowed is False


def test_outer_deadline_precedes_owner_loss_observed_at_the_boundary() -> None:
    state = _running_session()
    lost = _command(
        state,
        LifecycleEventKind.OWNER_LOST,
        DEADLINES.outer,
        invocation=INVOCATION_IDENTITY,
    ).state

    assert lost.primary_outcome is LifecycleOutcome.OUTER_DEADLINE
    assert all(output.state is not LifecycleOutputState.LOST_WITH_OWNER for output in lost.outputs)


def test_cleanup_failures_preserve_primary_cancellation_and_remain_bounded() -> None:
    state = _running_session()
    cancelling = _command(
        state,
        LifecycleEventKind.CANCELLATION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
    )
    assert cancelling.state.primary_outcome is LifecycleOutcome.CANCELLED_AFTER_SUBMISSION

    draining = _callback(cancelling.state, LifecycleEventKind.CANCELLATION_FAILED, 10.0)
    drain_timeout = _callback(draining.state, LifecycleEventKind.DEADLINE_EXPIRED, 70.0)
    assert drain_timeout.actions[0].kind is LifecycleActionKind.TEARDOWN
    failed_teardown = _callback(
        drain_timeout.state,
        LifecycleEventKind.TEARDOWN_FAILED,
        71.0,
    ).state

    assert failed_teardown.primary_outcome is LifecycleOutcome.CANCELLED_AFTER_SUBMISSION
    assert failed_teardown.cleanup_diagnostics == (
        LifecycleCleanupCode.CANCELLATION_FAILED,
        LifecycleCleanupCode.DRAIN_TIMEOUT,
        LifecycleCleanupCode.TEARDOWN_FAILED,
    )
    assert len(failed_teardown.cleanup_diagnostics) <= 8
    assert failed_teardown.outputs[0].state is LifecycleOutputState.CLEANUP_UNCONFIRMED
    assert failed_teardown.graph_disposition is LifecycleGraphDisposition.REBUILD_REQUIRED


def test_cancellation_timeout_still_drains_under_its_independent_budget() -> None:
    state = _running_session()
    cancelling = _command(
        state,
        LifecycleEventKind.CANCELLATION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    timed_out = _callback(cancelling, LifecycleEventKind.DEADLINE_EXPIRED, 60.0)

    assert timed_out.state.primary_outcome is LifecycleOutcome.CANCELLED_AFTER_SUBMISSION
    assert timed_out.state.cleanup_diagnostics == (LifecycleCleanupCode.CANCELLATION_TIMEOUT,)
    assert timed_out.actions[0].kind is LifecycleActionKind.DRAIN_INVOCATION
    assert timed_out.actions[0].deadline == DEADLINES.drain


def test_drain_failure_is_not_misclassified_as_a_timeout() -> None:
    state = _running_session()
    draining = _command(
        state,
        LifecycleEventKind.DRAIN_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    failed = _callback(draining, LifecycleEventKind.DRAIN_FAILED, 10.0)

    assert failed.state.primary_outcome is LifecycleOutcome.DRAIN_FAILURE
    assert failed.state.cleanup_diagnostics == (LifecycleCleanupCode.DRAIN_FAILED,)
    assert failed.actions[0].kind is LifecycleActionKind.TEARDOWN


def test_outer_deadline_is_tokenless_wins_over_cancel_and_dispatches_no_late_work() -> None:
    state = _running_session()
    expired = _command(
        state,
        LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
        100.0,
        invocation=INVOCATION_IDENTITY,
    )
    assert expired.actions == ()
    assert expired.state.primary_outcome is LifecycleOutcome.OUTER_DEADLINE
    assert expired.state.session_state is CompiledGraphSessionState.QUARANTINED
    assert expired.state.pending_action is None

    cancelled = _command(
        state,
        LifecycleEventKind.CANCELLATION_REQUESTED,
        100.0,
        invocation=INVOCATION_IDENTITY,
    )
    assert cancelled.actions == ()
    assert cancelled.state.primary_outcome is LifecycleOutcome.OUTER_DEADLINE


def test_outer_deadline_handles_pending_session_pre_admission_and_terminal_phases() -> None:
    initial = initial_session(RUN_IDENTITY, DEADLINES)
    validating = _command(initial, LifecycleEventKind.VALIDATION_REQUESTED, 1.0).state
    expired_validation = _command(
        validating,
        LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
        100.0,
    ).state
    assert expired_validation.primary_outcome is LifecycleOutcome.OUTER_DEADLINE
    assert expired_validation.session_state is CompiledGraphSessionState.REJECTED

    ready = _ready_session()
    expired_ready = _command(
        ready,
        LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
        100.0,
    ).state
    assert expired_ready.session_state is CompiledGraphSessionState.QUARANTINED

    admitted = _admitted_session()
    expired_admitted = _command(
        admitted,
        LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
        100.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    assert expired_admitted.primary_outcome is LifecycleOutcome.OUTER_DEADLINE
    assert expired_admitted.outputs[0].state is LifecycleOutputState.NOT_CREATED

    state = _ready_session()
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    terminal = _callback(state, LifecycleEventKind.CAPACITY_REJECTED, 6.0).state
    expired_terminal = _command(
        terminal,
        LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
        100.0,
    ).state
    assert expired_terminal.session_state is CompiledGraphSessionState.QUARANTINED


def test_deadline_expiry_before_validation_or_preparation_dispatches_no_action() -> None:
    initial = initial_session(RUN_IDENTITY, DEADLINES)
    expired = _command(initial, LifecycleEventKind.VALIDATION_REQUESTED, 100.0)
    assert expired.actions == ()
    assert expired.state.primary_outcome is LifecycleOutcome.OUTER_DEADLINE

    state = initial_session(RUN_IDENTITY, DEADLINES)
    state = _command(state, LifecycleEventKind.VALIDATION_REQUESTED, 1.0).state
    validated = _callback(state, LifecycleEventKind.VALIDATED, 2.0).state
    expired_preparation = _command(
        validated,
        LifecycleEventKind.PREPARATION_REQUESTED,
        100.0,
    )
    assert expired_preparation.actions == ()
    assert expired_preparation.state.primary_outcome is LifecycleOutcome.OUTER_DEADLINE
    assert expired_preparation.state.strategy_fallback_allowed is False


def test_late_outer_wake_cannot_rewrite_rejected_or_torn_down_session() -> None:
    state = initial_session(RUN_IDENTITY, DEADLINES)
    state = _command(state, LifecycleEventKind.VALIDATION_REQUESTED, 1.0).state
    rejected = _callback(state, LifecycleEventKind.VALIDATION_REJECTED, 2.0).state
    _assert_rejection(
        rejected,
        LifecycleEvent(
            kind=LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
            session_identity=RUN_IDENTITY,
            observed_at=100.0,
        ),
        LifecycleRejectionCode.INVALID_TRANSITION,
    )

    ready = _ready_session()
    tearing_down = _command(ready, LifecycleEventKind.TEARDOWN_REQUESTED, 5.0).state
    torn_down = _callback(tearing_down, LifecycleEventKind.TORN_DOWN, 6.0).state
    _assert_rejection(
        torn_down,
        LifecycleEvent(
            kind=LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
            session_identity=RUN_IDENTITY,
            observed_at=100.0,
        ),
        LifecycleRejectionCode.INVALID_TRANSITION,
    )
    _assert_rejection(
        torn_down,
        LifecycleEvent(
            kind=LifecycleEventKind.DRAIN_REQUESTED,
            session_identity=RUN_IDENTITY,
            observed_at=7.0,
        ),
        LifecycleRejectionCode.INVALID_TRANSITION,
    )
    _assert_rejection(
        torn_down,
        LifecycleEvent(
            kind=LifecycleEventKind.TEARDOWN_REQUESTED,
            session_identity=RUN_IDENTITY,
            observed_at=7.0,
        ),
        LifecycleRejectionCode.INVALID_TRANSITION,
    )


def test_absolute_action_deadlines_are_distinct_and_outer_capped() -> None:
    deadlines = replace(DEADLINES, outer=45.0, result=50.0, drain=70.0, teardown=80.0)
    state = _ready_session(deadlines=deadlines)
    admission = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    )
    assert admission.actions[0].deadline == deadlines.admission
    state = _callback(admission.state, LifecycleEventKind.ADMITTED, 6.0).state
    submission = _command(
        state,
        LifecycleEventKind.SUBMISSION_REQUESTED,
        7.0,
        invocation=INVOCATION_IDENTITY,
    )
    assert submission.actions[0].deadline == deadlines.submission
    state = _callback(submission.state, LifecycleEventKind.SUBMITTED, 8.0).state
    consumption = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    )
    assert consumption.actions[0].deadline == deadlines.outer
    expired = _callback(
        consumption.state,
        LifecycleEventKind.DEADLINE_EXPIRED,
        45.0,
    )
    assert expired.state.primary_outcome is LifecycleOutcome.OUTER_DEADLINE


def test_effective_deadline_classification_covers_stage_outer_and_tie_ordering() -> None:
    stage_first = replace(DEADLINES, outer=20.0, admission=10.0)
    state = _ready_session(deadlines=stage_first)
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    expired = _command(
        state,
        LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
        20.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    assert expired.primary_outcome is LifecycleOutcome.ADMISSION_TIMEOUT

    outer_first = replace(DEADLINES, outer=25.0, admission=30.0)
    state = _ready_session(deadlines=outer_first)
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    expired = _command(
        state,
        LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
        25.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    assert expired.primary_outcome is LifecycleOutcome.OUTER_DEADLINE

    tied = replace(DEADLINES, outer=30.0, admission=30.0)
    state = _ready_session(deadlines=tied)
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    expired = _command(
        state,
        LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
        30.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    assert expired.primary_outcome is LifecycleOutcome.OUTER_DEADLINE


def test_session_teardown_uses_only_run_identity_even_with_a_current_invocation() -> None:
    state = _running_session()
    requested = _command(state, LifecycleEventKind.TEARDOWN_REQUESTED, 9.0)
    assert requested.actions[0].kind is LifecycleActionKind.TEARDOWN
    assert requested.actions[0].invocation_identity is None
    torn_down = _callback(requested.state, LifecycleEventKind.TORN_DOWN, 10.0)
    assert torn_down.state.session_state is CompiledGraphSessionState.TORN_DOWN

    event_with_invocation = LifecycleEvent(
        kind=LifecycleEventKind.TEARDOWN_REQUESTED,
        session_identity=RUN_IDENTITY,
        invocation_identity=INVOCATION_IDENTITY,
        observed_at=9.0,
    )
    _assert_rejection(
        state,
        event_with_invocation,
        LifecycleRejectionCode.INVALID_EVENT,
    )


def test_idle_and_terminal_session_drain_use_session_identity_then_teardown() -> None:
    ready = _ready_session()
    draining = _command(ready, LifecycleEventKind.DRAIN_REQUESTED, 5.0)
    assert draining.actions[0].kind is LifecycleActionKind.DRAIN_SESSION
    assert draining.actions[0].invocation_identity is None
    teardown = _callback(draining.state, LifecycleEventKind.DRAINED, 6.0)
    assert teardown.actions[0].kind is LifecycleActionKind.TEARDOWN

    state = _ready_session()
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    terminal = _callback(state, LifecycleEventKind.CAPACITY_REJECTED, 6.0).state
    draining_terminal = _command(terminal, LifecycleEventKind.DRAIN_REQUESTED, 7.0)
    assert draining_terminal.actions[0].kind is LifecycleActionKind.DRAIN_SESSION
    assert draining_terminal.state.graph_disposition is LifecycleGraphDisposition.DRAIN_REQUIRED


def test_unprepared_sessions_reject_noop_drain_and_teardown_without_closing_fallback() -> None:
    initial = initial_session(RUN_IDENTITY, DEADLINES)
    validating = _command(initial, LifecycleEventKind.VALIDATION_REQUESTED, 1.0).state
    validated = _callback(validating, LifecycleEventKind.VALIDATED, 2.0).state

    rejected_validation = _command(
        initial_session(RUN_IDENTITY, DEADLINES),
        LifecycleEventKind.VALIDATION_REQUESTED,
        1.0,
    ).state
    rejected = _callback(
        rejected_validation,
        LifecycleEventKind.VALIDATION_REJECTED,
        2.0,
    ).state

    for state in (initial, validated, rejected):
        for kind in {
            LifecycleEventKind.DRAIN_REQUESTED,
            LifecycleEventKind.TEARDOWN_REQUESTED,
        }:
            result = reduce_lifecycle(
                state,
                LifecycleEvent(
                    kind=kind,
                    session_identity=RUN_IDENTITY,
                    observed_at=3.0,
                ),
            )
            assert isinstance(result, LifecycleRejection)
            assert result.code is LifecycleRejectionCode.INVALID_TRANSITION
        assert state.strategy_fallback_allowed is True


def test_teardown_failure_and_timeout_are_distinct_cleanup_observations() -> None:
    ready = _ready_session()
    tearing_down = _command(ready, LifecycleEventKind.TEARDOWN_REQUESTED, 5.0).state
    failed = _callback(tearing_down, LifecycleEventKind.TEARDOWN_FAILED, 6.0).state
    assert failed.primary_outcome is LifecycleOutcome.TEARDOWN_FAILURE
    assert failed.cleanup_diagnostics == (LifecycleCleanupCode.TEARDOWN_FAILED,)

    tearing_down = _command(ready, LifecycleEventKind.TEARDOWN_REQUESTED, 5.0).state
    timed_out = _callback(
        tearing_down,
        LifecycleEventKind.DEADLINE_EXPIRED,
        DEADLINES.teardown,
    ).state
    assert timed_out.primary_outcome is LifecycleOutcome.TEARDOWN_FAILURE
    assert timed_out.cleanup_diagnostics == (LifecycleCleanupCode.TEARDOWN_TIMEOUT,)


@pytest.mark.parametrize(
    "command_kind",
    [
        LifecycleEventKind.VALIDATION_REQUESTED,
        LifecycleEventKind.PREPARATION_REQUESTED,
        LifecycleEventKind.TEARDOWN_REQUESTED,
    ],
)
def test_session_commands_reject_an_extra_invocation_identity(
    command_kind: LifecycleEventKind,
) -> None:
    state = initial_session(RUN_IDENTITY, DEADLINES)
    _assert_rejection(
        state,
        LifecycleEvent(
            kind=command_kind,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=1.0,
        ),
        LifecycleRejectionCode.INVALID_EVENT,
    )


def test_session_callbacks_and_idle_drain_reject_extra_invocation_identity() -> None:
    state = initial_session(RUN_IDENTITY, DEADLINES)
    validating = _command(state, LifecycleEventKind.VALIDATION_REQUESTED, 1.0).state
    action = validating.pending_action
    assert action is not None
    _assert_rejection(
        validating,
        LifecycleEvent(
            kind=LifecycleEventKind.VALIDATED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=2.0,
            action_token=action.token,
        ),
        LifecycleRejectionCode.INVALID_EVENT,
    )

    ready = _ready_session()
    _assert_rejection(
        ready,
        LifecycleEvent(
            kind=LifecycleEventKind.DRAIN_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=5.0,
        ),
        LifecycleRejectionCode.INVALID_EVENT,
    )


def test_stale_identities_protocols_and_callback_payloads_fail_closed() -> None:
    state = _running_session()
    stale_run = replace(RUN_IDENTITY, run_id="stale-run")
    _assert_rejection(
        state,
        LifecycleEvent(
            kind=LifecycleEventKind.CONSUMPTION_REQUESTED,
            session_identity=stale_run,
            invocation_identity=replace(
                INVOCATION_IDENTITY,
                run_identity=stale_run,
            ),
            observed_at=9.0,
            output_index=0,
        ),
        LifecycleRejectionCode.STALE_SESSION_IDENTITY,
    )
    _assert_rejection(
        state,
        LifecycleEvent(
            kind=LifecycleEventKind.CONSUMPTION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=OTHER_INVOCATION_IDENTITY,
            observed_at=9.0,
            output_index=0,
        ),
        LifecycleRejectionCode.STALE_INVOCATION_IDENTITY,
    )
    _assert_rejection(
        state,
        LifecycleEvent(
            kind=LifecycleEventKind.CONSUMPTION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=9.0,
            output_index=0,
            protocol_version=2,
        ),
        LifecycleRejectionCode.INVALID_EVENT,
    )

    consuming = _command(
        state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    ).state
    action = consuming.pending_action
    assert action is not None
    _assert_rejection(
        consuming,
        LifecycleEvent(
            kind=LifecycleEventKind.OUTPUT_CONSUMED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=10.0,
            action_token=action.token,
            output_index=1,
        ),
        LifecycleRejectionCode.ACTION_PAYLOAD_MISMATCH,
    )


@pytest.mark.parametrize(
    ("event", "code"),
    [
        (
            LifecycleEvent(
                kind=LifecycleEventKind.VALIDATION_REQUESTED,
                session_identity=replace(RUN_IDENTITY, task_execution_pk=0),
                observed_at=1.0,
            ),
            LifecycleRejectionCode.INVALID_IDENTITY,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.VALIDATION_REQUESTED,
                session_identity=RUN_IDENTITY,
                observed_at=float("nan"),
            ),
            LifecycleRejectionCode.INVALID_CLOCK,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.VALIDATION_REQUESTED,
                session_identity=RUN_IDENTITY,
                observed_at=1.0,
                action_token="unexpected",
            ),
            LifecycleRejectionCode.UNEXPECTED_ACTION_TOKEN,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
                session_identity=RUN_IDENTITY,
                observed_at=1.0,
            ),
            LifecycleRejectionCode.DEADLINE_NOT_EXPIRED,
        ),
    ],
)
def test_invalid_event_envelopes_return_stable_codes(
    event: LifecycleEvent,
    code: LifecycleRejectionCode,
) -> None:
    _assert_rejection(initial_session(RUN_IDENTITY, DEADLINES), event, code)


def test_clock_regression_and_command_during_pending_action_are_rejected() -> None:
    state = initial_session(RUN_IDENTITY, DEADLINES)
    pending = _command(state, LifecycleEventKind.VALIDATION_REQUESTED, 2.0).state
    _assert_rejection(
        pending,
        LifecycleEvent(
            kind=LifecycleEventKind.PREPARATION_REQUESTED,
            session_identity=RUN_IDENTITY,
            observed_at=1.0,
        ),
        LifecycleRejectionCode.CLOCK_REGRESSION,
    )
    _assert_rejection(
        pending,
        LifecycleEvent(
            kind=LifecycleEventKind.PREPARATION_REQUESTED,
            session_identity=RUN_IDENTITY,
            observed_at=3.0,
        ),
        LifecycleRejectionCode.ACTION_IN_PROGRESS,
    )


@pytest.mark.parametrize("declared_outputs", [None, 0, 65, True])
def test_admission_rejects_invalid_output_cardinality(
    declared_outputs: int | None,
) -> None:
    state = _ready_session()
    _assert_rejection(
        state,
        LifecycleEvent(
            kind=LifecycleEventKind.ADMISSION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=5.0,
            declared_outputs=declared_outputs,
        ),
        LifecycleRejectionCode.OUTPUT_CARDINALITY_INVALID,
    )


def test_duplicate_invocation_and_invalid_output_index_are_rejected() -> None:
    state = _ready_session()
    state = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    state = _callback(state, LifecycleEventKind.CAPACITY_REJECTED, 6.0).state
    _assert_rejection(
        state,
        LifecycleEvent(
            kind=LifecycleEventKind.ADMISSION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=7.0,
            declared_outputs=1,
        ),
        LifecycleRejectionCode.DUPLICATE_INVOCATION,
    )

    running = _running_session()
    _assert_rejection(
        running,
        LifecycleEvent(
            kind=LifecycleEventKind.CONSUMPTION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=9.0,
            output_index=3,
        ),
        LifecycleRejectionCode.OUTPUT_INDEX_INVALID,
    )


@pytest.mark.parametrize("state_factory", [_admitted_session, _running_session])
def test_v1_rejects_a_second_in_flight_invocation(
    state_factory: Callable[[], CompiledGraphLifecycleState],
) -> None:
    state = state_factory()
    _assert_rejection(
        state,
        LifecycleEvent(
            kind=LifecycleEventKind.ADMISSION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=OTHER_INVOCATION_IDENTITY,
            observed_at=9.0,
            declared_outputs=1,
        ),
        LifecycleRejectionCode.INVALID_TRANSITION,
    )


def test_terminal_invocation_cannot_discard_unaccounted_outputs_on_new_admission() -> None:
    state = _running_session()
    forged = replace(
        state,
        invocation_state=CompiledGraphInvocationState.TERMINAL,
    )
    _assert_rejection(
        forged,
        LifecycleEvent(
            kind=LifecycleEventKind.ADMISSION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=OTHER_INVOCATION_IDENTITY,
            observed_at=9.0,
            declared_outputs=1,
        ),
        LifecycleRejectionCode.INVALID_STATE,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"session_state": "READY"},
        {"invocation_state": "RUNNING"},
        {"effect_state": "MAY_HAVE_STARTED"},
        {"graph_disposition": "REUSABLE"},
        {"retry_disposition": "AUTOMATIC_ALLOWED"},
        {"last_observed_at": float("nan")},
        {"outputs": [LifecycleOutputSlot(0, LifecycleOutputState.AVAILABLE)]},
        {"cleanup_diagnostics": ("DRAIN_FAILED",)},
    ],
)
def test_malformed_public_states_return_structured_rejections(
    mutation: dict[str, object],
) -> None:
    malformed = replace(initial_session(RUN_IDENTITY, DEADLINES), **mutation)
    event = LifecycleEvent(
        kind=LifecycleEventKind.VALIDATION_REQUESTED,
        session_identity=RUN_IDENTITY,
        observed_at=1.0,
    )

    _assert_rejection(malformed, event, LifecycleRejectionCode.INVALID_STATE)


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "session_state": CompiledGraphSessionState.PREPARING,
            "strategy_fallback_allowed": False,
        },
        {
            "session_state": CompiledGraphSessionState.TORN_DOWN,
            "strategy_fallback_allowed": False,
            "graph_disposition": LifecycleGraphDisposition.PREPARED,
        },
    ],
)
def test_cross_state_session_invariants_reject_incoherent_public_state(
    mutation: dict[str, object],
) -> None:
    malformed = replace(initial_session(RUN_IDENTITY, DEADLINES), **mutation)
    _assert_rejection(
        malformed,
        LifecycleEvent(
            kind=LifecycleEventKind.VALIDATION_REQUESTED,
            session_identity=RUN_IDENTITY,
            observed_at=1.0,
        ),
        LifecycleRejectionCode.INVALID_STATE,
    )


def test_cross_state_invocation_invariant_rejects_terminal_pending_submit() -> None:
    submitting = _admitted_session()
    submitting = _command(
        submitting,
        LifecycleEventKind.SUBMISSION_REQUESTED,
        7.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    malformed = replace(
        submitting,
        invocation_state=CompiledGraphInvocationState.TERMINAL,
    )

    _assert_rejection(
        malformed,
        LifecycleEvent(
            kind=LifecycleEventKind.OWNER_LOST,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=8.0,
        ),
        LifecycleRejectionCode.INVALID_STATE,
    )


def test_public_state_invariant_matrix_rejects_each_malformed_dimension() -> None:
    initial = initial_session(RUN_IDENTITY, DEADLINES)
    validating = _command(initial, LifecycleEventKind.VALIDATION_REQUESTED, 1.0).state
    ready = _ready_session()
    admitting = _command(
        ready,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=1,
    ).state
    admitted = _callback(admitting, LifecycleEventKind.ADMITTED, 6.0).state
    submitting = _command(
        admitted,
        LifecycleEventKind.SUBMISSION_REQUESTED,
        7.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    running = _callback(submitting, LifecycleEventKind.SUBMITTED, 8.0).state
    consuming = _command(
        running,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    ).state
    lost = _command(
        running,
        LifecycleEventKind.OWNER_LOST,
        9.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    health_state = _running_session()
    health_state = _command(
        health_state,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    ).state
    health_state = _callback(
        health_state,
        LifecycleEventKind.OUTPUT_CONSUMED,
        10.0,
    ).state
    tearing_down = _command(ready, LifecycleEventKind.TEARDOWN_REQUESTED, 5.0).state
    quarantined = _callback(
        tearing_down,
        LifecycleEventKind.TEARDOWN_FAILED,
        6.0,
    ).state
    tearing_down = _command(ready, LifecycleEventKind.TEARDOWN_REQUESTED, 5.0).state
    torn_down = _callback(tearing_down, LifecycleEventKind.TORN_DOWN, 6.0).state
    other_run = replace(RUN_IDENTITY, run_id="other-run")

    validating_action = validating.pending_action
    admitting_action = admitting.pending_action
    submitting_action = submitting.pending_action
    consuming_action = consuming.pending_action
    assert validating_action is not None
    assert admitting_action is not None
    assert submitting_action is not None
    assert consuming_action is not None

    malformed_states: list[tuple[str, object]] = [
        ("not-state", object()),
        ("session-identity-type", replace(initial, session_identity=object())),
        ("protocol", replace(initial, protocol_version=2)),
        (
            "session-identity",
            replace(initial, session_identity=replace(RUN_IDENTITY, task_execution_pk=0)),
        ),
        ("deadlines", replace(initial, deadlines=replace(DEADLINES, outer=-1.0))),
        (
            "capacity",
            replace(initial, capacity=replace(LifecycleCapacity(), callers=2)),
        ),
        ("fallback-type", replace(initial, strategy_fallback_allowed="yes")),
        ("fallback-reopened", replace(ready, strategy_fallback_allowed=True)),
        ("outcome-type", replace(initial, primary_outcome="SUCCEEDED")),
        (
            "output-state-type",
            replace(
                running,
                outputs=(replace(running.outputs[0], state="AVAILABLE"),),
            ),
        ),
        ("sequence", replace(initial, next_action_sequence=0)),
        ("completed-token", replace(initial, last_completed_action_token="")),
        (
            "invocation-state-without-identity",
            replace(initial, invocation_state=CompiledGraphInvocationState.RUNNING),
        ),
        (
            "outputs-without-identity",
            replace(
                initial,
                outputs=(LifecycleOutputSlot(0, LifecycleOutputState.DECLARED),),
            ),
        ),
        (
            "invalid-invocation-identity",
            replace(
                running,
                invocation_identity=replace(INVOCATION_IDENTITY, invocation_id=""),
            ),
        ),
        (
            "cross-run-invocation",
            replace(
                running,
                invocation_identity=replace(
                    INVOCATION_IDENTITY,
                    run_identity=other_run,
                ),
            ),
        ),
        (
            "identity-with-none-state",
            replace(running, invocation_state=CompiledGraphInvocationState.NONE),
        ),
        (
            "too-many-outputs",
            replace(
                running,
                outputs=tuple(
                    LifecycleOutputSlot(index, LifecycleOutputState.AVAILABLE)
                    for index in range(COMPILED_GRAPH_LIFECYCLE_MAX_OUTPUTS + 1)
                ),
            ),
        ),
        (
            "sparse-outputs",
            replace(
                running,
                outputs=(LifecycleOutputSlot(2, LifecycleOutputState.AVAILABLE),),
            ),
        ),
        (
            "too-many-diagnostics",
            replace(running, cleanup_diagnostics=tuple(LifecycleCleanupCode)[:9]),
        ),
        (
            "reusable-not-ready",
            replace(
                running,
                session_state=CompiledGraphSessionState.DRAINING,
                graph_disposition=LifecycleGraphDisposition.REUSABLE,
            ),
        ),
        (
            "reusable-incomplete",
            replace(running, graph_disposition=LifecycleGraphDisposition.REUSABLE),
        ),
        (
            "reusable-owner-loss",
            replace(
                lost,
                session_state=CompiledGraphSessionState.READY,
                graph_disposition=LifecycleGraphDisposition.REUSABLE,
            ),
        ),
        (
            "not-created-after-submit",
            replace(
                running,
                outputs=(LifecycleOutputSlot(0, LifecycleOutputState.NOT_CREATED),),
            ),
        ),
        (
            "unavailable-before-teardown",
            replace(
                running,
                outputs=(
                    LifecycleOutputSlot(
                        0,
                        LifecycleOutputState.UNAVAILABLE_AFTER_TEARDOWN,
                    ),
                ),
            ),
        ),
        (
            "lost-without-operator-reconciliation",
            replace(lost, retry_disposition=LifecycleRetryDisposition.PROHIBITED),
        ),
        (
            "automatic-after-effects",
            replace(running, retry_disposition=LifecycleRetryDisposition.AUTOMATIC_ALLOWED),
        ),
        ("pending-type", replace(validating, pending_action=object())),
        (
            "pending-kind",
            replace(validating, pending_action=replace(validating_action, kind="VALIDATE")),
        ),
        (
            "pending-protocol",
            replace(validating, pending_action=replace(validating_action, protocol_version=2)),
        ),
        (
            "pending-token",
            replace(validating, pending_action=replace(validating_action, token="")),
        ),
        (
            "pending-deadline",
            replace(
                validating,
                pending_action=replace(validating_action, deadline=float("nan")),
            ),
        ),
        (
            "pending-session",
            replace(
                validating,
                pending_action=replace(validating_action, session_identity=other_run),
            ),
        ),
        (
            "session-action-with-invocation",
            replace(
                validating,
                pending_action=replace(
                    validating_action,
                    invocation_identity=INVOCATION_IDENTITY,
                ),
            ),
        ),
        (
            "pending-invocation",
            replace(
                submitting,
                pending_action=replace(
                    submitting_action,
                    invocation_identity=OTHER_INVOCATION_IDENTITY,
                ),
            ),
        ),
        (
            "consume-index",
            replace(
                consuming,
                pending_action=replace(consuming_action, output_index=4),
            ),
        ),
        (
            "session-output-index",
            replace(
                validating,
                pending_action=replace(validating_action, output_index=0),
            ),
        ),
        (
            "admit-cardinality",
            replace(
                admitting,
                pending_action=replace(admitting_action, declared_outputs=2),
            ),
        ),
        (
            "session-cardinality",
            replace(
                validating,
                pending_action=replace(validating_action, declared_outputs=1),
            ),
        ),
        (
            "duplicate-pending-token",
            replace(validating, last_completed_action_token=validating_action.token),
        ),
        (
            "action-after-outer",
            replace(
                validating,
                pending_action=replace(validating_action, deadline=101.0),
            ),
        ),
        (
            "wrong-action-deadline",
            replace(
                validating,
                pending_action=replace(validating_action, deadline=99.0),
            ),
        ),
        (
            "idle-with-action",
            replace(validating, session_state=CompiledGraphSessionState.NEW),
        ),
        (
            "actionless-draining",
            replace(
                ready,
                session_state=CompiledGraphSessionState.DRAINING,
            ),
        ),
        (
            "health-with-unsafe-output",
            replace(
                health_state,
                outputs=(
                    LifecycleOutputSlot(
                        0,
                        LifecycleOutputState.CLEANUP_UNCONFIRMED,
                    ),
                ),
            ),
        ),
        (
            "unprepared-with-graph",
            replace(initial, graph_disposition=LifecycleGraphDisposition.PREPARED),
        ),
        (
            "ready-with-rebuild",
            replace(ready, graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED),
        ),
        (
            "quarantine-with-prepared",
            replace(quarantined, graph_disposition=LifecycleGraphDisposition.PREPARED),
        ),
        (
            "torn-with-prepared",
            replace(torn_down, graph_disposition=LifecycleGraphDisposition.PREPARED),
        ),
        (
            "session-phase-action",
            replace(
                validating,
                session_state=CompiledGraphSessionState.PREPARING,
                strategy_fallback_allowed=False,
            ),
        ),
        (
            "invocation-phase-action",
            replace(
                submitting,
                invocation_state=CompiledGraphInvocationState.TERMINAL,
                outputs=(
                    LifecycleOutputSlot(
                        0,
                        LifecycleOutputState.CLEANUP_UNCONFIRMED,
                    ),
                ),
            ),
        ),
    ]
    event = LifecycleEvent(
        kind=LifecycleEventKind.VALIDATION_REQUESTED,
        session_identity=RUN_IDENTITY,
        observed_at=10.0,
    )

    for label, malformed in malformed_states:
        result = reduce_lifecycle(malformed, event)  # type: ignore[arg-type]
        assert isinstance(result, LifecycleRejection), label
        assert result.code is LifecycleRejectionCode.INVALID_STATE, label


def test_every_finite_event_kind_returns_a_bounded_result_from_initial_state() -> None:
    state = initial_session(RUN_IDENTITY, DEADLINES)
    callbacks = {
        event_kind
        for event_kinds in {
            LifecycleActionKind.VALIDATE: {
                LifecycleEventKind.VALIDATED,
                LifecycleEventKind.VALIDATION_REJECTED,
                LifecycleEventKind.DEADLINE_EXPIRED,
            },
        }.values()
        for event_kind in event_kinds
    }
    for kind in LifecycleEventKind:
        invocation = (
            INVOCATION_IDENTITY
            if kind
            in {
                LifecycleEventKind.ADMISSION_REQUESTED,
                LifecycleEventKind.SUBMISSION_REQUESTED,
                LifecycleEventKind.CONSUMPTION_REQUESTED,
                LifecycleEventKind.CANCELLATION_REQUESTED,
                LifecycleEventKind.OWNER_LOST,
            }
            else None
        )
        result = reduce_lifecycle(
            state,
            LifecycleEvent(
                kind=kind,
                session_identity=RUN_IDENTITY,
                invocation_identity=invocation,
                observed_at=1.0,
                action_token="bounded-token" if kind in callbacks else None,
                declared_outputs=(1 if kind is LifecycleEventKind.ADMISSION_REQUESTED else None),
                output_index=(0 if kind is LifecycleEventKind.CONSUMPTION_REQUESTED else None),
            ),
        )
        assert isinstance(result, LifecycleTransition | LifecycleRejection)
        if isinstance(result, LifecycleRejection):
            assert isinstance(result.code, LifecycleRejectionCode)
            assert len(result.message) <= 256


def test_snapshot_is_versioned_bounded_redacted_and_handle_free() -> None:
    state = _ready_session()
    admitted = _command(
        state,
        LifecycleEventKind.ADMISSION_REQUESTED,
        5.0,
        invocation=INVOCATION_IDENTITY,
        declared_outputs=COMPILED_GRAPH_LIFECYCLE_MAX_OUTPUTS,
    ).state
    snapshot = lifecycle_snapshot(admitted)
    encoded = json.dumps(snapshot, sort_keys=True).encode()

    assert snapshot["schema_version"] == 1
    assert snapshot["protocol_version"] == COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION
    assert snapshot["capacity"] == LifecycleCapacity().as_dict()
    assert len(snapshot["outputs"]) == COMPILED_GRAPH_LIFECYCLE_MAX_OUTPUTS  # type: ignore[arg-type]
    assert len(encoded) <= COMPILED_GRAPH_LIFECYCLE_MAX_SNAPSHOT_BYTES
    assert "payload" not in encoded.decode().lower()
    assert "credential" not in encoded.decode().lower()
    assert "compileddag" not in encoded.decode().lower()
    assert "objectref" not in encoded.decode().lower()


def test_snapshot_rejects_invalid_state_and_enforces_serialized_bound(monkeypatch) -> None:
    invalid = replace(initial_session(RUN_IDENTITY, DEADLINES), session_state="NEW")
    with pytest.raises(ValueError, match="session state"):
        lifecycle_snapshot(invalid)

    monkeypatch.setattr(lifecycle_module, "COMPILED_GRAPH_LIFECYCLE_MAX_SNAPSHOT_BYTES", 1)
    with pytest.raises(ValueError, match="bounded protocol size"):
        lifecycle_snapshot(initial_session(RUN_IDENTITY, DEADLINES))


def test_event_validation_matrix_rejects_malformed_identity_and_operation_fields() -> None:
    initial = initial_session(RUN_IDENTITY, DEADLINES)
    other_run = replace(RUN_IDENTITY, run_id="other-run")
    invalid_events: list[tuple[object, LifecycleRejectionCode]] = [
        (object(), LifecycleRejectionCode.INVALID_EVENT),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.ADMISSION_REQUESTED,
                session_identity=RUN_IDENTITY,
                invocation_identity=object(),  # type: ignore[arg-type]
                observed_at=1.0,
                declared_outputs=1,
            ),
            LifecycleRejectionCode.INVALID_IDENTITY,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.ADMISSION_REQUESTED,
                session_identity=RUN_IDENTITY,
                invocation_identity=WorkflowInvocationIdentity(
                    replace(RUN_IDENTITY, task_execution_pk=0),
                    "invocation",
                ),
                observed_at=1.0,
                declared_outputs=1,
            ),
            LifecycleRejectionCode.INVALID_IDENTITY,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.ADMISSION_REQUESTED,
                session_identity=RUN_IDENTITY,
                invocation_identity=replace(INVOCATION_IDENTITY, invocation_id=""),
                observed_at=1.0,
                declared_outputs=1,
            ),
            LifecycleRejectionCode.INVALID_IDENTITY,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.ADMISSION_REQUESTED,
                session_identity=RUN_IDENTITY,
                invocation_identity=WorkflowInvocationIdentity(other_run, "invocation"),
                observed_at=1.0,
                declared_outputs=1,
            ),
            LifecycleRejectionCode.STALE_SESSION_IDENTITY,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.ADMISSION_REQUESTED,
                session_identity=RUN_IDENTITY,
                invocation_identity=INVOCATION_IDENTITY,
                observed_at=1.0,
                declared_outputs=1,
                output_index=0,
            ),
            LifecycleRejectionCode.ACTION_PAYLOAD_MISMATCH,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.CONSUMPTION_REQUESTED,
                session_identity=RUN_IDENTITY,
                invocation_identity=INVOCATION_IDENTITY,
                observed_at=1.0,
                declared_outputs=1,
                output_index=0,
            ),
            LifecycleRejectionCode.ACTION_PAYLOAD_MISMATCH,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.VALIDATION_REQUESTED,
                session_identity=RUN_IDENTITY,
                observed_at=1.0,
                declared_outputs=1,
            ),
            LifecycleRejectionCode.ACTION_PAYLOAD_MISMATCH,
        ),
        (
            LifecycleEvent(
                kind=LifecycleEventKind.ADMISSION_REQUESTED,
                session_identity=RUN_IDENTITY,
                observed_at=1.0,
                declared_outputs=1,
            ),
            LifecycleRejectionCode.MISSING_INVOCATION_IDENTITY,
        ),
    ]

    for event, code in invalid_events:
        result = reduce_lifecycle(initial, event)  # type: ignore[arg-type]
        assert isinstance(result, LifecycleRejection)
        assert result.code is code

    consuming = _running_session()
    consuming = _command(
        consuming,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
        output_index=0,
    ).state
    action = consuming.pending_action
    assert action is not None
    _assert_rejection(
        consuming,
        LifecycleEvent(
            kind=LifecycleEventKind.OUTPUT_CONSUMED,
            session_identity=RUN_IDENTITY,
            invocation_identity=OTHER_INVOCATION_IDENTITY,
            observed_at=10.0,
            action_token=action.token,
            output_index=0,
        ),
        LifecycleRejectionCode.STALE_INVOCATION_IDENTITY,
    )


def test_invalid_command_transitions_do_not_mutate_terminal_or_active_state() -> None:
    ready = _ready_session()
    _assert_rejection(
        ready,
        LifecycleEvent(
            kind=LifecycleEventKind.VALIDATION_REQUESTED,
            session_identity=RUN_IDENTITY,
            observed_at=5.0,
        ),
        LifecycleRejectionCode.INVALID_TRANSITION,
    )

    running = _running_session()
    for kind in {LifecycleEventKind.CANCELLATION_REQUESTED, LifecycleEventKind.OWNER_LOST}:
        terminal = replace(
            running,
            invocation_state=CompiledGraphInvocationState.TERMINAL,
            outputs=(LifecycleOutputSlot(0, LifecycleOutputState.CONSUMED),),
        )
        _assert_rejection(
            terminal,
            LifecycleEvent(
                kind=kind,
                session_identity=RUN_IDENTITY,
                invocation_identity=INVOCATION_IDENTITY,
                observed_at=9.0,
            ),
            LifecycleRejectionCode.INVALID_TRANSITION,
        )


def test_action_sequence_exhaustion_fails_closed() -> None:
    state = replace(_ready_session(), next_action_sequence=1 << 63)
    _assert_rejection(
        state,
        LifecycleEvent(
            kind=LifecycleEventKind.ADMISSION_REQUESTED,
            session_identity=RUN_IDENTITY,
            invocation_identity=INVOCATION_IDENTITY,
            observed_at=5.0,
            declared_outputs=1,
        ),
        LifecycleRejectionCode.ACTION_SEQUENCE_EXHAUSTED,
    )


def test_cleanup_diagnostic_deduplication_and_truncation_are_bounded() -> None:
    state = _running_session()
    cancelling = _command(
        state,
        LifecycleEventKind.CANCELLATION_REQUESTED,
        9.0,
        invocation=INVOCATION_IDENTITY,
    ).state
    duplicate = replace(
        cancelling,
        cleanup_diagnostics=(LifecycleCleanupCode.CANCELLATION_FAILED,),
    )
    draining = _callback(duplicate, LifecycleEventKind.CANCELLATION_FAILED, 10.0).state
    assert draining.cleanup_diagnostics == (LifecycleCleanupCode.CANCELLATION_FAILED,)

    full = tuple(
        code
        for code in LifecycleCleanupCode
        if code
        not in {
            LifecycleCleanupCode.CANCELLATION_FAILED,
            LifecycleCleanupCode.DIAGNOSTICS_TRUNCATED,
        }
    )[:8]
    truncated = _callback(
        replace(cancelling, cleanup_diagnostics=full),
        LifecycleEventKind.CANCELLATION_FAILED,
        10.0,
    ).state
    assert len(truncated.cleanup_diagnostics) == 8
    assert truncated.cleanup_diagnostics[-1] is LifecycleCleanupCode.DIAGNOSTICS_TRUNCATED

    already_truncated = (*full[:7], LifecycleCleanupCode.DIAGNOSTICS_TRUNCATED)
    unchanged = _callback(
        replace(cancelling, cleanup_diagnostics=already_truncated),
        LifecycleEventKind.CANCELLATION_FAILED,
        10.0,
    ).state
    assert unchanged.cleanup_diagnostics == already_truncated


class _FakeAdapter:
    def dispatch(self, action: LifecycleAction) -> LifecycleEvent:
        assert action.kind is LifecycleActionKind.VALIDATE
        return LifecycleEvent(
            kind=LifecycleEventKind.VALIDATED,
            session_identity=action.session_identity,
            observed_at=2.0,
            action_token=action.token,
        )


def _dispatch_once(
    adapter: CompiledGraphLifecycleAdapter,
    action: LifecycleAction,
) -> LifecycleEvent:
    return adapter.dispatch(action)


def test_fake_adapter_implements_strategy_neutral_single_dispatch_protocol() -> None:
    state = initial_session(RUN_IDENTITY, DEADLINES)
    requested = _command(state, LifecycleEventKind.VALIDATION_REQUESTED, 1.0)
    event = _dispatch_once(_FakeAdapter(), requested.actions[0])

    accepted = _transition(requested.state, event)

    assert accepted.state.session_state is CompiledGraphSessionState.VALIDATED


def test_lifecycle_import_and_reduction_work_when_ray_imports_are_prohibited() -> None:
    repository = Path(__file__).resolve().parents[2]
    script = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'ray' or name.startswith('ray.'):
        raise AssertionError(f'Ray import prohibited: {name}')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from django_ray.runtime.compiled_graph_lifecycle import (
    LifecycleDeadlines, LifecycleEvent, LifecycleEventKind,
    LifecycleTransition, initial_session, reduce_lifecycle,
)
from django_ray.runtime.context import WorkflowRunIdentity
identity = WorkflowRunIdentity(1, 1, 0, 'run')
deadlines = LifecycleDeadlines(10, 3, 4, 5, 6, 7, 8)
state = initial_session(identity, deadlines)
result = reduce_lifecycle(
    state,
    LifecycleEvent(LifecycleEventKind.VALIDATION_REQUESTED, identity, 1),
)
assert isinstance(result, LifecycleTransition)
assert result.actions[0].kind.value == 'VALIDATE'
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository / "src")

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
