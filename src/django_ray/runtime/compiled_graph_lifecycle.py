"""Ray-free reference lifecycle for a future Compiled Graph adapter.

The reducer in this module owns no runtime handles and never reads a clock.  Callers
provide absolute timestamps on events and execute the returned actions through an
adapter that keeps beta/runtime-specific objects process-local.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from django_ray.runtime.context import WorkflowInvocationIdentity, WorkflowRunIdentity

COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION = 1
COMPILED_GRAPH_LIFECYCLE_SNAPSHOT_SCHEMA_VERSION = 1
COMPILED_GRAPH_LIFECYCLE_MAX_OUTPUTS = 64
COMPILED_GRAPH_LIFECYCLE_MAX_DIAGNOSTICS = 8
COMPILED_GRAPH_LIFECYCLE_MAX_SNAPSHOT_BYTES = 16_384

_MAX_ID_CHARS = 128
_MAX_REJECTION_MESSAGE_CHARS = 256
_MAX_ACTION_SEQUENCE = (1 << 63) - 1


class CompiledGraphSessionState(StrEnum):
    """Lifecycle of one process-local compiled graph session."""

    NEW = "NEW"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    PREPARING = "PREPARING"
    READY = "READY"
    DRAINING = "DRAINING"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"
    TORN_DOWN = "TORN_DOWN"


class CompiledGraphInvocationState(StrEnum):
    """Lifecycle of the single active V1 invocation owned by a session."""

    NONE = "NONE"
    ADMITTING = "ADMITTING"
    ADMITTED = "ADMITTED"
    SUBMITTING = "SUBMITTING"
    RUNNING = "RUNNING"
    CONSUMING = "CONSUMING"
    CANCELLING = "CANCELLING"
    DRAINING = "DRAINING"
    CHECKING_HEALTH = "CHECKING_HEALTH"
    TERMINAL = "TERMINAL"


class LifecycleActionKind(StrEnum):
    """Strategy-neutral operations dispatched by an adapter."""

    VALIDATE = "VALIDATE"
    PREPARE = "PREPARE"
    ADMIT = "ADMIT"
    SUBMIT = "SUBMIT"
    CONSUME_OUTPUT = "CONSUME_OUTPUT"
    RELEASE_CAPACITY = "RELEASE_CAPACITY"
    CANCEL = "CANCEL"
    DRAIN_INVOCATION = "DRAIN_INVOCATION"
    DRAIN_SESSION = "DRAIN_SESSION"
    CHECK_HEALTH = "CHECK_HEALTH"
    TEARDOWN = "TEARDOWN"


class LifecycleEventKind(StrEnum):
    """Commands and bounded observations accepted by the reducer."""

    VALIDATION_REQUESTED = "VALIDATION_REQUESTED"
    VALIDATED = "VALIDATED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    PREPARATION_REQUESTED = "PREPARATION_REQUESTED"
    PREPARED = "PREPARED"
    PREPARATION_FAILED = "PREPARATION_FAILED"
    ADMISSION_REQUESTED = "ADMISSION_REQUESTED"
    ADMITTED = "ADMITTED"
    CAPACITY_REJECTED = "CAPACITY_REJECTED"
    ADMISSION_TIMED_OUT = "ADMISSION_TIMED_OUT"
    SUBMISSION_REQUESTED = "SUBMISSION_REQUESTED"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_TIMED_OUT = "SUBMISSION_TIMED_OUT"
    CONSUMPTION_REQUESTED = "CONSUMPTION_REQUESTED"
    OUTPUT_CONSUMED = "OUTPUT_CONSUMED"
    RESULT_TIMED_OUT = "RESULT_TIMED_OUT"
    APPLICATION_ERROR = "APPLICATION_ERROR"
    ACTOR_DIED = "ACTOR_DIED"
    CHANNEL_ERROR = "CHANNEL_ERROR"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    CAPACITY_RELEASED = "CAPACITY_RELEASED"
    CAPACITY_RELEASE_FAILED = "CAPACITY_RELEASE_FAILED"
    CANCELLED = "CANCELLED"
    CANCELLATION_FAILED = "CANCELLATION_FAILED"
    DRAIN_REQUESTED = "DRAIN_REQUESTED"
    DRAINED = "DRAINED"
    DRAIN_FAILED = "DRAIN_FAILED"
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    TEARDOWN_REQUESTED = "TEARDOWN_REQUESTED"
    TORN_DOWN = "TORN_DOWN"
    TEARDOWN_FAILED = "TEARDOWN_FAILED"
    OWNER_LOST = "OWNER_LOST"
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"
    OUTER_DEADLINE_EXPIRED = "OUTER_DEADLINE_EXPIRED"


class LifecycleOutcome(StrEnum):
    """Stable primary classifications consumed by the durable outer task."""

    SUCCEEDED = "SUCCEEDED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    PREPARATION_FAILED = "PREPARATION_FAILED"
    ADMISSION_TIMEOUT = "ADMISSION_TIMEOUT"
    CAPACITY_REJECTED = "CAPACITY_REJECTED"
    SUBMIT_TIMEOUT = "SUBMIT_TIMEOUT"
    GET_TIMEOUT = "GET_TIMEOUT"
    CANCELLED_PRE_SUBMISSION = "CANCELLED_PRE_SUBMISSION"
    CANCELLED_AFTER_SUBMISSION = "CANCELLED_AFTER_SUBMISSION"
    APPLICATION_ERROR = "APPLICATION_ERROR"
    ACTOR_DIED = "ACTOR_DIED"
    CHANNEL_ERROR = "CHANNEL_ERROR"
    OWNER_LOST = "OWNER_LOST"
    OUTER_DEADLINE = "OUTER_DEADLINE"
    DRAIN_FAILURE = "DRAIN_FAILURE"
    DRAIN_TIMEOUT = "DRAIN_TIMEOUT"
    TEARDOWN_FAILURE = "TEARDOWN_FAILURE"


class LifecycleEffectState(StrEnum):
    """What the controller may safely claim about application effects."""

    NOT_STARTED = "NOT_STARTED"
    MAY_HAVE_STARTED = "MAY_HAVE_STARTED"


class LifecycleRetryDisposition(StrEnum):
    """Independent durable disposition for a later invocation attempt."""

    AUTOMATIC_ALLOWED = "AUTOMATIC_ALLOWED"
    APPLICATION_POLICY_REQUIRED = "APPLICATION_POLICY_REQUIRED"
    OPERATOR_RECONCILIATION_REQUIRED = "OPERATOR_RECONCILIATION_REQUIRED"
    PROHIBITED = "PROHIBITED"


class LifecycleGraphDisposition(StrEnum):
    """Whether the prepared graph may accept another invocation."""

    UNPREPARED = "UNPREPARED"
    PREPARED = "PREPARED"
    DRAIN_REQUIRED = "DRAIN_REQUIRED"
    REUSABLE = "REUSABLE"
    REBUILD_REQUIRED = "REBUILD_REQUIRED"
    TORN_DOWN = "TORN_DOWN"


class LifecycleOutputState(StrEnum):
    """One-shot ownership state for a declared output slot."""

    DECLARED = "DECLARED"
    NOT_CREATED = "NOT_CREATED"
    AVAILABLE = "AVAILABLE"
    CONSUMING = "CONSUMING"
    RELEASE_PENDING = "RELEASE_PENDING"
    CONSUMED = "CONSUMED"
    ADAPTER_RELEASED = "ADAPTER_RELEASED"
    LOST_WITH_OWNER = "LOST_WITH_OWNER"
    CLEANUP_UNCONFIRMED = "CLEANUP_UNCONFIRMED"
    UNAVAILABLE_AFTER_TEARDOWN = "UNAVAILABLE_AFTER_TEARDOWN"


class LifecycleCleanupCode(StrEnum):
    """Bounded diagnostics which never replace the primary outcome."""

    CAPACITY_RELEASE_FAILED = "CAPACITY_RELEASE_FAILED"
    CAPACITY_RELEASE_TIMEOUT = "CAPACITY_RELEASE_TIMEOUT"
    CANCELLATION_FAILED = "CANCELLATION_FAILED"
    CANCELLATION_TIMEOUT = "CANCELLATION_TIMEOUT"
    DRAIN_FAILED = "DRAIN_FAILED"
    DRAIN_TIMEOUT = "DRAIN_TIMEOUT"
    HEALTH_CHECK_FAILED = "HEALTH_CHECK_FAILED"
    TEARDOWN_FAILED = "TEARDOWN_FAILED"
    TEARDOWN_TIMEOUT = "TEARDOWN_TIMEOUT"
    DIAGNOSTICS_TRUNCATED = "DIAGNOSTICS_TRUNCATED"


class LifecycleRejectionCode(StrEnum):
    """Stable bounded reasons for refusing an event without mutation."""

    INVALID_STATE = "INVALID_STATE"
    INVALID_EVENT = "INVALID_EVENT"
    INVALID_IDENTITY = "INVALID_IDENTITY"
    STALE_SESSION_IDENTITY = "STALE_SESSION_IDENTITY"
    MISSING_INVOCATION_IDENTITY = "MISSING_INVOCATION_IDENTITY"
    STALE_INVOCATION_IDENTITY = "STALE_INVOCATION_IDENTITY"
    DUPLICATE_INVOCATION = "DUPLICATE_INVOCATION"
    INVALID_CLOCK = "INVALID_CLOCK"
    CLOCK_REGRESSION = "CLOCK_REGRESSION"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    ACTION_IN_PROGRESS = "ACTION_IN_PROGRESS"
    ACTION_TOKEN_REQUIRED = "ACTION_TOKEN_REQUIRED"
    UNEXPECTED_ACTION_TOKEN = "UNEXPECTED_ACTION_TOKEN"
    STALE_ACTION_TOKEN = "STALE_ACTION_TOKEN"
    DUPLICATE_ACTION = "DUPLICATE_ACTION"
    ACTION_SEQUENCE_EXHAUSTED = "ACTION_SEQUENCE_EXHAUSTED"
    DEADLINE_NOT_EXPIRED = "DEADLINE_NOT_EXPIRED"
    OUTPUT_CARDINALITY_INVALID = "OUTPUT_CARDINALITY_INVALID"
    OUTPUT_INDEX_INVALID = "OUTPUT_INDEX_INVALID"
    OUTPUT_ALREADY_CLAIMED = "OUTPUT_ALREADY_CLAIMED"
    ACTION_PAYLOAD_MISMATCH = "ACTION_PAYLOAD_MISMATCH"


_TERMINAL_OUTPUT_STATES = frozenset(
    {
        LifecycleOutputState.NOT_CREATED,
        LifecycleOutputState.CONSUMED,
        LifecycleOutputState.ADAPTER_RELEASED,
        LifecycleOutputState.LOST_WITH_OWNER,
        LifecycleOutputState.CLEANUP_UNCONFIRMED,
        LifecycleOutputState.UNAVAILABLE_AFTER_TEARDOWN,
    }
)
_REUSE_SAFE_OUTPUT_STATES = frozenset(
    {
        LifecycleOutputState.NOT_CREATED,
        LifecycleOutputState.CONSUMED,
        LifecycleOutputState.ADAPTER_RELEASED,
    }
)


@dataclass(frozen=True)
class LifecycleDeadlines:
    """Absolute phase deadlines; the outer deadline caps every phase."""

    outer: float
    admission: float
    submission: float
    result: float
    cancellation: float
    drain: float
    teardown: float

    def as_dict(self) -> dict[str, float]:
        """Return the JSON-safe absolute budget record."""
        return {
            "outer": self.outer,
            "admission": self.admission,
            "submission": self.submission,
            "result": self.result,
            "cancellation": self.cancellation,
            "drain": self.drain,
            "teardown": self.teardown,
        }


@dataclass(frozen=True)
class LifecycleCapacity:
    """Exact single-owner/single-active-invocation capacity supported by V1."""

    session_owners: int = 1
    callers: int = 1
    maximum_in_flight: int = 1
    maximum_buffered_results: int = 1
    owner_concurrency: int = 1
    queue_capacity: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return the fingerprintable V1 capacity record."""
        return {
            "session_owners": self.session_owners,
            "callers": self.callers,
            "maximum_in_flight": self.maximum_in_flight,
            "maximum_buffered_results": self.maximum_buffered_results,
            "owner_concurrency": self.owner_concurrency,
            "queue_capacity": self.queue_capacity,
        }


@dataclass(frozen=True)
class LifecycleOutputSlot:
    """Payload-free ownership record for one declared output."""

    index: int
    state: LifecycleOutputState


@dataclass(frozen=True)
class LifecycleAction:
    """One bounded adapter request produced by the reducer."""

    token: str
    kind: LifecycleActionKind
    session_identity: WorkflowRunIdentity
    deadline: float
    invocation_identity: WorkflowInvocationIdentity | None = None
    output_index: int | None = None
    declared_outputs: int | None = None
    protocol_version: int = COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION


@dataclass(frozen=True)
class LifecycleEvent:
    """One command or clock-injected adapter observation."""

    kind: LifecycleEventKind
    session_identity: WorkflowRunIdentity
    observed_at: float
    invocation_identity: WorkflowInvocationIdentity | None = None
    action_token: str | None = None
    declared_outputs: int | None = None
    output_index: int | None = None
    protocol_version: int = COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION


@dataclass(frozen=True)
class CompiledGraphLifecycleState:
    """Complete immutable state for the V1 single-invocation boundary."""

    session_identity: WorkflowRunIdentity
    deadlines: LifecycleDeadlines
    capacity: LifecycleCapacity
    session_state: CompiledGraphSessionState = CompiledGraphSessionState.NEW
    invocation_identity: WorkflowInvocationIdentity | None = None
    invocation_state: CompiledGraphInvocationState = CompiledGraphInvocationState.NONE
    effect_state: LifecycleEffectState = LifecycleEffectState.NOT_STARTED
    graph_disposition: LifecycleGraphDisposition = LifecycleGraphDisposition.UNPREPARED
    strategy_fallback_allowed: bool = True
    retry_disposition: LifecycleRetryDisposition = LifecycleRetryDisposition.AUTOMATIC_ALLOWED
    outputs: tuple[LifecycleOutputSlot, ...] = ()
    primary_outcome: LifecycleOutcome | None = None
    cleanup_diagnostics: tuple[LifecycleCleanupCode, ...] = ()
    pending_action: LifecycleAction | None = None
    last_completed_action_token: str | None = None
    next_action_sequence: int = 1
    last_observed_at: float | None = None
    protocol_version: int = COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION


@dataclass(frozen=True)
class LifecycleTransition:
    """Accepted reducer result and its ordered adapter actions."""

    state: CompiledGraphLifecycleState
    actions: tuple[LifecycleAction, ...] = ()


@dataclass(frozen=True)
class LifecycleRejection:
    """Rejected event; callers retain their previous state unchanged."""

    code: LifecycleRejectionCode
    message: str


class CompiledGraphLifecycleAdapter(Protocol):
    """Runtime-neutral boundary implemented by a future engine adapter."""

    def dispatch(self, action: LifecycleAction) -> LifecycleEvent:
        """Execute one action and return its bounded observation."""


_ACTION_CALLBACKS: dict[LifecycleActionKind, frozenset[LifecycleEventKind]] = {
    LifecycleActionKind.VALIDATE: frozenset(
        {
            LifecycleEventKind.VALIDATED,
            LifecycleEventKind.VALIDATION_REJECTED,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.PREPARE: frozenset(
        {
            LifecycleEventKind.PREPARED,
            LifecycleEventKind.PREPARATION_FAILED,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.ADMIT: frozenset(
        {
            LifecycleEventKind.ADMITTED,
            LifecycleEventKind.CAPACITY_REJECTED,
            LifecycleEventKind.ADMISSION_TIMED_OUT,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.SUBMIT: frozenset(
        {
            LifecycleEventKind.SUBMITTED,
            LifecycleEventKind.SUBMISSION_TIMED_OUT,
            LifecycleEventKind.APPLICATION_ERROR,
            LifecycleEventKind.ACTOR_DIED,
            LifecycleEventKind.CHANNEL_ERROR,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.CONSUME_OUTPUT: frozenset(
        {
            LifecycleEventKind.OUTPUT_CONSUMED,
            LifecycleEventKind.RESULT_TIMED_OUT,
            LifecycleEventKind.APPLICATION_ERROR,
            LifecycleEventKind.ACTOR_DIED,
            LifecycleEventKind.CHANNEL_ERROR,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.RELEASE_CAPACITY: frozenset(
        {
            LifecycleEventKind.CAPACITY_RELEASED,
            LifecycleEventKind.CAPACITY_RELEASE_FAILED,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.CANCEL: frozenset(
        {
            LifecycleEventKind.CANCELLED,
            LifecycleEventKind.CANCELLATION_FAILED,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.DRAIN_INVOCATION: frozenset(
        {
            LifecycleEventKind.DRAINED,
            LifecycleEventKind.DRAIN_FAILED,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.DRAIN_SESSION: frozenset(
        {
            LifecycleEventKind.DRAINED,
            LifecycleEventKind.DRAIN_FAILED,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.CHECK_HEALTH: frozenset(
        {
            LifecycleEventKind.HEALTHY,
            LifecycleEventKind.UNHEALTHY,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
    LifecycleActionKind.TEARDOWN: frozenset(
        {
            LifecycleEventKind.TORN_DOWN,
            LifecycleEventKind.TEARDOWN_FAILED,
            LifecycleEventKind.DEADLINE_EXPIRED,
        }
    ),
}

_CALLBACK_EVENTS = frozenset(
    event_kind for event_kinds in _ACTION_CALLBACKS.values() for event_kind in event_kinds
)
_EXPLICIT_TIMEOUT_EVENTS = frozenset(
    {
        LifecycleEventKind.ADMISSION_TIMED_OUT,
        LifecycleEventKind.SUBMISSION_TIMED_OUT,
        LifecycleEventKind.RESULT_TIMED_OUT,
        LifecycleEventKind.DEADLINE_EXPIRED,
    }
)


def initial_session(
    session_identity: WorkflowRunIdentity,
    deadlines: LifecycleDeadlines,
    *,
    capacity: LifecycleCapacity | None = None,
) -> CompiledGraphLifecycleState:
    """Create a validated, handle-free initial reducer state."""
    identity_error = _identity_error(session_identity)
    if identity_error is not None:
        raise ValueError(identity_error)
    _validate_deadlines(deadlines)
    selected_capacity = LifecycleCapacity() if capacity is None else capacity
    _validate_capacity(selected_capacity)
    return CompiledGraphLifecycleState(
        session_identity=session_identity,
        deadlines=deadlines,
        capacity=selected_capacity,
    )


def reduce_lifecycle(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition | LifecycleRejection:
    """Apply one event without I/O, clock reads, or runtime-handle access."""
    invariant_error = _state_invariant_error(state)
    if invariant_error is not None:
        return _reject(LifecycleRejectionCode.INVALID_STATE, invariant_error)
    validation = _validate_event(state, event)
    if validation is not None:
        return validation

    working_state = replace(state, last_observed_at=event.observed_at)
    if event.kind in _CALLBACK_EVENTS:
        result = _reduce_callback(working_state, event)
    else:
        result = _reduce_command(working_state, event)
    if isinstance(result, LifecycleRejection):
        return result

    next_state = result.state
    invariant_error = _state_invariant_error(next_state)
    if invariant_error is not None:
        return _reject(LifecycleRejectionCode.INVALID_STATE, invariant_error)
    return LifecycleTransition(state=next_state, actions=result.actions)


def lifecycle_snapshot(state: CompiledGraphLifecycleState) -> dict[str, object]:
    """Return a bounded, versioned, redacted, handle-free state snapshot."""
    invariant_error = _state_invariant_error(state)
    if invariant_error is not None:
        raise ValueError(invariant_error)
    pending = state.pending_action
    snapshot: dict[str, object] = {
        "schema_version": COMPILED_GRAPH_LIFECYCLE_SNAPSHOT_SCHEMA_VERSION,
        "protocol_version": state.protocol_version,
        "session_identity": state.session_identity.as_dict(),
        "session_state": state.session_state.value,
        "invocation_identity": (
            state.invocation_identity.as_dict() if state.invocation_identity is not None else None
        ),
        "invocation_state": state.invocation_state.value,
        "deadlines": state.deadlines.as_dict(),
        "capacity": state.capacity.as_dict(),
        "last_observed_at": state.last_observed_at,
        "strategy_fallback_allowed": state.strategy_fallback_allowed,
        "retry_disposition": state.retry_disposition.value,
        "effect_state": state.effect_state.value,
        "graph_disposition": state.graph_disposition.value,
        "primary_outcome": (
            state.primary_outcome.value if state.primary_outcome is not None else None
        ),
        "outputs": [
            {"index": output.index, "state": output.state.value} for output in state.outputs
        ],
        "cleanup_diagnostics": [item.value for item in state.cleanup_diagnostics],
        "pending_action": (
            {
                "token": pending.token,
                "kind": pending.kind.value,
                "deadline": pending.deadline,
                "output_index": pending.output_index,
                "declared_outputs": pending.declared_outputs,
            }
            if pending is not None
            else None
        ),
        "next_action_sequence": state.next_action_sequence,
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > COMPILED_GRAPH_LIFECYCLE_MAX_SNAPSHOT_BYTES:
        raise ValueError("lifecycle snapshot exceeds its bounded protocol size")
    return snapshot


def _reduce_command(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition | LifecycleRejection:
    if event.action_token is not None:
        return _reject(
            LifecycleRejectionCode.UNEXPECTED_ACTION_TOKEN,
            "command events must not carry an action token",
        )
    if event.kind is LifecycleEventKind.OUTER_DEADLINE_EXPIRED:
        return _outer_deadline_expired(state, event)
    if event.kind is LifecycleEventKind.CANCELLATION_REQUESTED:
        return _request_cancellation(state, event)
    if event.kind is LifecycleEventKind.OWNER_LOST:
        return _owner_lost(state, event)
    if state.pending_action is not None:
        return _reject(
            LifecycleRejectionCode.ACTION_IN_PROGRESS,
            "another adapter action is still pending",
        )

    if event.kind is LifecycleEventKind.VALIDATION_REQUESTED:
        if state.session_state is not CompiledGraphSessionState.NEW:
            return _invalid_transition(state, event)
        next_state = replace(state, session_state=CompiledGraphSessionState.VALIDATING)
        return _issue_action(next_state, LifecycleActionKind.VALIDATE)

    if event.kind is LifecycleEventKind.PREPARATION_REQUESTED:
        if state.session_state is not CompiledGraphSessionState.VALIDATED:
            return _invalid_transition(state, event)
        next_state = replace(
            state,
            session_state=CompiledGraphSessionState.PREPARING,
            strategy_fallback_allowed=False,
        )
        return _issue_action(next_state, LifecycleActionKind.PREPARE)

    if event.kind is LifecycleEventKind.ADMISSION_REQUESTED:
        return _request_admission(state, event)

    if event.kind is LifecycleEventKind.SUBMISSION_REQUESTED:
        if state.invocation_state is not CompiledGraphInvocationState.ADMITTED:
            return _invalid_transition(state, event)
        next_state = replace(
            state,
            invocation_state=CompiledGraphInvocationState.SUBMITTING,
            effect_state=LifecycleEffectState.MAY_HAVE_STARTED,
            retry_disposition=LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED,
        )
        return _issue_action(next_state, LifecycleActionKind.SUBMIT)

    if event.kind is LifecycleEventKind.CONSUMPTION_REQUESTED:
        return _request_consumption(state, event)

    if event.kind is LifecycleEventKind.DRAIN_REQUESTED:
        if state.session_state is not CompiledGraphSessionState.READY:
            return _invalid_transition(state, event)
        if state.invocation_identity is None:
            next_state = replace(state, session_state=CompiledGraphSessionState.DRAINING)
            return _issue_action(next_state, LifecycleActionKind.DRAIN_SESSION)
        if state.invocation_state is CompiledGraphInvocationState.TERMINAL:
            next_state = replace(
                state,
                session_state=CompiledGraphSessionState.DRAINING,
                graph_disposition=LifecycleGraphDisposition.DRAIN_REQUIRED,
            )
            return _issue_action(next_state, LifecycleActionKind.DRAIN_SESSION)
        return _begin_invocation_drain(state)

    assert event.kind is LifecycleEventKind.TEARDOWN_REQUESTED
    if state.session_state not in {
        CompiledGraphSessionState.READY,
        CompiledGraphSessionState.QUARANTINED,
    }:
        return _invalid_transition(state, event)
    return _begin_teardown(state)


def _outer_deadline_expired(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition | LifecycleRejection:
    if state.session_state in {
        CompiledGraphSessionState.REJECTED,
        CompiledGraphSessionState.TORN_DOWN,
    }:
        return _invalid_transition(state, event)
    if event.observed_at < state.deadlines.outer:
        return _reject(
            LifecycleRejectionCode.DEADLINE_NOT_EXPIRED,
            "the outer deadline has not expired",
        )
    if state.pending_action is not None:
        return _expire_action(state)
    if state.invocation_identity is None:
        terminal_state = (
            CompiledGraphSessionState.REJECTED
            if state.session_state
            in {
                CompiledGraphSessionState.NEW,
                CompiledGraphSessionState.VALIDATING,
                CompiledGraphSessionState.VALIDATED,
            }
            else CompiledGraphSessionState.QUARANTINED
        )
        return LifecycleTransition(
            replace(
                state,
                session_state=terminal_state,
                graph_disposition=(
                    LifecycleGraphDisposition.UNPREPARED
                    if terminal_state is CompiledGraphSessionState.REJECTED
                    else LifecycleGraphDisposition.REBUILD_REQUIRED
                ),
                primary_outcome=LifecycleOutcome.OUTER_DEADLINE,
                strategy_fallback_allowed=False,
                retry_disposition=LifecycleRetryDisposition.PROHIBITED,
            )
        )
    if state.invocation_state in {
        CompiledGraphInvocationState.ADMITTING,
        CompiledGraphInvocationState.ADMITTED,
    }:
        expired = replace(
            state,
            invocation_state=CompiledGraphInvocationState.CANCELLING,
            primary_outcome=LifecycleOutcome.OUTER_DEADLINE,
            retry_disposition=LifecycleRetryDisposition.PROHIBITED,
            outputs=_terminalize_outputs(
                state.outputs,
                LifecycleOutputState.NOT_CREATED,
            ),
        )
        return _issue_action(expired, LifecycleActionKind.RELEASE_CAPACITY)
    if state.invocation_state in {
        CompiledGraphInvocationState.SUBMITTING,
        CompiledGraphInvocationState.RUNNING,
        CompiledGraphInvocationState.CONSUMING,
        CompiledGraphInvocationState.CANCELLING,
        CompiledGraphInvocationState.DRAINING,
        CompiledGraphInvocationState.CHECKING_HEALTH,
    }:
        expired = _set_primary(state, LifecycleOutcome.OUTER_DEADLINE)
        expired = replace(
            expired,
            session_state=CompiledGraphSessionState.DRAINING,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            retry_disposition=LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED,
        )
        return _begin_invocation_drain(expired)
    assert state.invocation_state is CompiledGraphInvocationState.TERMINAL
    return LifecycleTransition(
        replace(
            state,
            session_state=CompiledGraphSessionState.QUARANTINED,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            retry_disposition=LifecycleRetryDisposition.PROHIBITED,
        )
    )


def _reduce_callback(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition | LifecycleRejection:
    pending = state.pending_action
    if pending is None:
        if event.action_token is None:
            return _reject(
                LifecycleRejectionCode.ACTION_TOKEN_REQUIRED,
                "adapter callbacks require an action token",
            )
        if event.action_token == state.last_completed_action_token:
            return _reject(
                LifecycleRejectionCode.DUPLICATE_ACTION,
                "the action callback was already applied",
            )
        return _reject(
            LifecycleRejectionCode.STALE_ACTION_TOKEN,
            "the action token is not pending",
        )
    if event.action_token is None:
        return _reject(
            LifecycleRejectionCode.ACTION_TOKEN_REQUIRED,
            "adapter callbacks require an action token",
        )
    if event.action_token != pending.token:
        if event.action_token == state.last_completed_action_token:
            code = LifecycleRejectionCode.DUPLICATE_ACTION
        else:
            code = LifecycleRejectionCode.STALE_ACTION_TOKEN
        return _reject(code, "the callback does not match the pending action")
    if event.kind not in _ACTION_CALLBACKS[pending.kind]:
        return _invalid_transition(state, event)

    if event.kind in _EXPLICIT_TIMEOUT_EVENTS and event.observed_at < pending.deadline:
        return _reject(
            LifecycleRejectionCode.DEADLINE_NOT_EXPIRED,
            "the action deadline has not expired",
        )
    if event.observed_at >= pending.deadline:
        return _expire_action(state)

    state = _complete_action(state)
    if event.kind is LifecycleEventKind.VALIDATED:
        return LifecycleTransition(
            replace(state, session_state=CompiledGraphSessionState.VALIDATED)
        )
    if event.kind is LifecycleEventKind.VALIDATION_REJECTED:
        return LifecycleTransition(
            replace(
                state,
                session_state=CompiledGraphSessionState.REJECTED,
                primary_outcome=LifecycleOutcome.VALIDATION_REJECTED,
            )
        )
    if event.kind is LifecycleEventKind.PREPARED:
        return LifecycleTransition(
            replace(
                state,
                session_state=CompiledGraphSessionState.READY,
                graph_disposition=LifecycleGraphDisposition.PREPARED,
            )
        )
    if event.kind is LifecycleEventKind.PREPARATION_FAILED:
        failed = replace(
            state,
            session_state=CompiledGraphSessionState.QUARANTINED,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            primary_outcome=LifecycleOutcome.PREPARATION_FAILED,
        )
        return _begin_teardown(failed)
    if event.kind is LifecycleEventKind.ADMITTED:
        return LifecycleTransition(
            replace(state, invocation_state=CompiledGraphInvocationState.ADMITTED)
        )
    if event.kind is LifecycleEventKind.CAPACITY_REJECTED:
        return LifecycleTransition(
            _finish_pre_submission(
                state,
                outcome=LifecycleOutcome.CAPACITY_REJECTED,
            )
        )
    if event.kind is LifecycleEventKind.SUBMITTED:
        return LifecycleTransition(
            replace(
                state,
                invocation_state=CompiledGraphInvocationState.RUNNING,
                outputs=tuple(
                    replace(output, state=LifecycleOutputState.AVAILABLE)
                    for output in state.outputs
                ),
            )
        )
    if event.kind is LifecycleEventKind.OUTPUT_CONSUMED:
        return _output_consumed(state, pending)
    if event.kind is LifecycleEventKind.APPLICATION_ERROR:
        if pending.kind is LifecycleActionKind.CONSUME_OUTPUT:
            state = _set_output(state, pending.output_index, LifecycleOutputState.CONSUMED)
        failed = _set_primary(state, LifecycleOutcome.APPLICATION_ERROR)
        failed = replace(
            failed,
            graph_disposition=LifecycleGraphDisposition.DRAIN_REQUIRED,
            retry_disposition=LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED,
        )
        return _begin_invocation_drain(failed)
    if event.kind in {LifecycleEventKind.ACTOR_DIED, LifecycleEventKind.CHANNEL_ERROR}:
        outcome = (
            LifecycleOutcome.ACTOR_DIED
            if event.kind is LifecycleEventKind.ACTOR_DIED
            else LifecycleOutcome.CHANNEL_ERROR
        )
        failed = _set_primary(state, outcome)
        failed = replace(
            failed,
            session_state=CompiledGraphSessionState.QUARANTINED,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            retry_disposition=LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED,
        )
        return _begin_teardown(failed)
    if event.kind is LifecycleEventKind.CAPACITY_RELEASED:
        return LifecycleTransition(
            replace(
                state,
                session_state=CompiledGraphSessionState.READY,
                invocation_state=CompiledGraphInvocationState.TERMINAL,
                graph_disposition=LifecycleGraphDisposition.PREPARED,
            )
        )
    if event.kind is LifecycleEventKind.CAPACITY_RELEASE_FAILED:
        failed = _append_diagnostic(state, LifecycleCleanupCode.CAPACITY_RELEASE_FAILED)
        failed = replace(
            failed,
            session_state=CompiledGraphSessionState.QUARANTINED,
            invocation_state=CompiledGraphInvocationState.TERMINAL,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
        )
        return _begin_teardown(failed)
    if event.kind in {LifecycleEventKind.CANCELLED, LifecycleEventKind.CANCELLATION_FAILED}:
        if event.kind is LifecycleEventKind.CANCELLATION_FAILED:
            state = _append_diagnostic(state, LifecycleCleanupCode.CANCELLATION_FAILED)
        return _begin_invocation_drain(state)
    if event.kind is LifecycleEventKind.DRAINED:
        return _drained(state, pending)
    if event.kind is LifecycleEventKind.DRAIN_FAILED:
        failed = _append_diagnostic(state, LifecycleCleanupCode.DRAIN_FAILED)
        failed = _set_primary(failed, LifecycleOutcome.DRAIN_FAILURE)
        failed = replace(
            failed,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
        )
        return _begin_teardown(failed)
    if event.kind is LifecycleEventKind.HEALTHY:
        return LifecycleTransition(
            replace(
                state,
                session_state=CompiledGraphSessionState.READY,
                invocation_state=CompiledGraphInvocationState.TERMINAL,
                graph_disposition=LifecycleGraphDisposition.REUSABLE,
            )
        )
    if event.kind is LifecycleEventKind.UNHEALTHY:
        failed = _append_diagnostic(state, LifecycleCleanupCode.HEALTH_CHECK_FAILED)
        failed = replace(
            failed,
            session_state=CompiledGraphSessionState.QUARANTINED,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
        )
        return _begin_teardown(failed)
    if event.kind is LifecycleEventKind.TORN_DOWN:
        return LifecycleTransition(
            replace(
                state,
                session_state=CompiledGraphSessionState.TORN_DOWN,
                invocation_state=(
                    CompiledGraphInvocationState.TERMINAL
                    if state.invocation_identity is not None
                    else CompiledGraphInvocationState.NONE
                ),
                graph_disposition=LifecycleGraphDisposition.TORN_DOWN,
                outputs=_terminalize_outputs(
                    state.outputs,
                    LifecycleOutputState.UNAVAILABLE_AFTER_TEARDOWN,
                ),
            )
        )
    assert event.kind is LifecycleEventKind.TEARDOWN_FAILED
    failed = _append_diagnostic(state, LifecycleCleanupCode.TEARDOWN_FAILED)
    failed = _set_primary(failed, LifecycleOutcome.TEARDOWN_FAILURE)
    return LifecycleTransition(
        replace(
            failed,
            session_state=CompiledGraphSessionState.QUARANTINED,
            invocation_state=(
                CompiledGraphInvocationState.TERMINAL
                if state.invocation_identity is not None
                else CompiledGraphInvocationState.NONE
            ),
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            outputs=_terminalize_outputs(
                failed.outputs,
                LifecycleOutputState.CLEANUP_UNCONFIRMED,
            ),
        )
    )


def _request_admission(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition | LifecycleRejection:
    if (
        state.session_state is not CompiledGraphSessionState.READY
        or state.graph_disposition
        not in {
            LifecycleGraphDisposition.PREPARED,
            LifecycleGraphDisposition.REUSABLE,
        }
        or state.invocation_state
        not in {
            CompiledGraphInvocationState.NONE,
            CompiledGraphInvocationState.TERMINAL,
        }
        or not _outputs_reuse_safe(state.outputs)
    ):
        return _invalid_transition(state, event)
    identity = event.invocation_identity
    assert identity is not None
    if state.invocation_identity == identity:
        return _reject(
            LifecycleRejectionCode.DUPLICATE_INVOCATION,
            "the invocation identity has already been used by this session",
        )
    output_count = event.declared_outputs
    if (
        isinstance(output_count, bool)
        or not isinstance(output_count, int)
        or not 1 <= output_count <= COMPILED_GRAPH_LIFECYCLE_MAX_OUTPUTS
    ):
        return _reject(
            LifecycleRejectionCode.OUTPUT_CARDINALITY_INVALID,
            f"declared_outputs must be between 1 and {COMPILED_GRAPH_LIFECYCLE_MAX_OUTPUTS}",
        )
    next_state = replace(
        state,
        invocation_identity=identity,
        invocation_state=CompiledGraphInvocationState.ADMITTING,
        effect_state=LifecycleEffectState.NOT_STARTED,
        retry_disposition=LifecycleRetryDisposition.AUTOMATIC_ALLOWED,
        outputs=tuple(
            LifecycleOutputSlot(index=index, state=LifecycleOutputState.DECLARED)
            for index in range(output_count)
        ),
        primary_outcome=None,
        cleanup_diagnostics=(),
    )
    return _issue_action(
        next_state,
        LifecycleActionKind.ADMIT,
        declared_outputs=output_count,
    )


def _request_consumption(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition | LifecycleRejection:
    if state.invocation_state is not CompiledGraphInvocationState.RUNNING:
        return _invalid_transition(state, event)
    index = event.output_index
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(state.outputs):
        return _reject(
            LifecycleRejectionCode.OUTPUT_INDEX_INVALID,
            "output_index does not identify a declared output",
        )
    if state.outputs[index].state is not LifecycleOutputState.AVAILABLE:
        return _reject(
            LifecycleRejectionCode.OUTPUT_ALREADY_CLAIMED,
            "a one-shot output may begin consumption only once",
        )
    next_state = _set_output(state, index, LifecycleOutputState.CONSUMING)
    next_state = replace(next_state, invocation_state=CompiledGraphInvocationState.CONSUMING)
    return _issue_action(
        next_state,
        LifecycleActionKind.CONSUME_OUTPUT,
        output_index=index,
    )


def _request_cancellation(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition | LifecycleRejection:
    if event.observed_at >= state.deadlines.outer:
        return _outer_deadline_expired(state, event)
    if state.invocation_state in {
        CompiledGraphInvocationState.ADMITTING,
        CompiledGraphInvocationState.ADMITTED,
    }:
        cancelled = replace(
            state,
            invocation_state=CompiledGraphInvocationState.CANCELLING,
            primary_outcome=LifecycleOutcome.CANCELLED_PRE_SUBMISSION,
            retry_disposition=LifecycleRetryDisposition.PROHIBITED,
            outputs=_terminalize_outputs(state.outputs, LifecycleOutputState.NOT_CREATED),
        )
        cancelled = _supersede_action(cancelled)
        return _issue_action(cancelled, LifecycleActionKind.RELEASE_CAPACITY)
    if state.invocation_state in {
        CompiledGraphInvocationState.SUBMITTING,
        CompiledGraphInvocationState.RUNNING,
        CompiledGraphInvocationState.CONSUMING,
    }:
        cancelled = replace(
            state,
            session_state=CompiledGraphSessionState.DRAINING,
            invocation_state=CompiledGraphInvocationState.CANCELLING,
            effect_state=LifecycleEffectState.MAY_HAVE_STARTED,
            graph_disposition=LifecycleGraphDisposition.DRAIN_REQUIRED,
            retry_disposition=LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED,
        )
        cancelled = _set_primary(cancelled, LifecycleOutcome.CANCELLED_AFTER_SUBMISSION)
        cancelled = _supersede_action(cancelled)
        return _issue_action(cancelled, LifecycleActionKind.CANCEL)
    return _invalid_transition(state, event)


def _owner_lost(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleTransition | LifecycleRejection:
    if event.observed_at >= state.deadlines.outer:
        return _outer_deadline_expired(state, event)
    if state.invocation_identity is None or state.invocation_state in {
        CompiledGraphInvocationState.NONE,
        CompiledGraphInvocationState.TERMINAL,
    }:
        return _invalid_transition(state, event)
    lost = _supersede_action(state)
    lost = _set_primary(lost, LifecycleOutcome.OWNER_LOST)
    before_submission = state.invocation_state in {
        CompiledGraphInvocationState.ADMITTING,
        CompiledGraphInvocationState.ADMITTED,
    }
    return LifecycleTransition(
        replace(
            lost,
            session_state=CompiledGraphSessionState.QUARANTINED,
            invocation_state=CompiledGraphInvocationState.TERMINAL,
            effect_state=(
                LifecycleEffectState.NOT_STARTED
                if before_submission
                else LifecycleEffectState.MAY_HAVE_STARTED
                if state.invocation_state
                in {
                    CompiledGraphInvocationState.SUBMITTING,
                    CompiledGraphInvocationState.RUNNING,
                    CompiledGraphInvocationState.CONSUMING,
                    CompiledGraphInvocationState.CANCELLING,
                    CompiledGraphInvocationState.DRAINING,
                    CompiledGraphInvocationState.CHECKING_HEALTH,
                }
                else state.effect_state
            ),
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            retry_disposition=(
                LifecycleRetryDisposition.AUTOMATIC_ALLOWED
                if before_submission
                else LifecycleRetryDisposition.OPERATOR_RECONCILIATION_REQUIRED
            ),
            outputs=_terminalize_outputs(
                state.outputs,
                LifecycleOutputState.NOT_CREATED
                if before_submission
                else LifecycleOutputState.LOST_WITH_OWNER,
            ),
        )
    )


def _output_consumed(
    state: CompiledGraphLifecycleState,
    action: LifecycleAction,
) -> LifecycleTransition | LifecycleRejection:
    state = _set_output(state, action.output_index, LifecycleOutputState.CONSUMED)
    if _outputs_terminal(state.outputs):
        state = _set_primary(state, LifecycleOutcome.SUCCEEDED)
        state = replace(
            state,
            session_state=CompiledGraphSessionState.DRAINING,
            invocation_state=CompiledGraphInvocationState.CHECKING_HEALTH,
            retry_disposition=LifecycleRetryDisposition.PROHIBITED,
        )
        return _issue_action(state, LifecycleActionKind.CHECK_HEALTH)
    return LifecycleTransition(
        replace(state, invocation_state=CompiledGraphInvocationState.RUNNING)
    )


def _drained(
    state: CompiledGraphLifecycleState,
    action: LifecycleAction,
) -> LifecycleTransition | LifecycleRejection:
    if action.kind is LifecycleActionKind.DRAIN_SESSION:
        return _begin_teardown(state)
    state = replace(
        state,
        outputs=_terminalize_outputs(state.outputs, LifecycleOutputState.ADAPTER_RELEASED),
    )
    if state.graph_disposition is LifecycleGraphDisposition.REBUILD_REQUIRED:
        return _begin_teardown(state)
    state = replace(state, invocation_state=CompiledGraphInvocationState.CHECKING_HEALTH)
    return _issue_action(state, LifecycleActionKind.CHECK_HEALTH)


def _begin_invocation_drain(
    state: CompiledGraphLifecycleState,
) -> LifecycleTransition | LifecycleRejection:
    assert state.invocation_identity is not None
    next_state = replace(
        state,
        session_state=CompiledGraphSessionState.DRAINING,
        invocation_state=CompiledGraphInvocationState.DRAINING,
    )
    return _issue_action(next_state, LifecycleActionKind.DRAIN_INVOCATION)


def _begin_teardown(
    state: CompiledGraphLifecycleState,
) -> LifecycleTransition | LifecycleRejection:
    next_state = replace(
        state,
        session_state=CompiledGraphSessionState.DRAINING,
        invocation_state=(
            CompiledGraphInvocationState.DRAINING
            if state.invocation_identity is not None
            and state.invocation_state is not CompiledGraphInvocationState.TERMINAL
            else state.invocation_state
        ),
        graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
    )
    return _issue_action(next_state, LifecycleActionKind.TEARDOWN)


def _finish_pre_submission(
    state: CompiledGraphLifecycleState,
    *,
    outcome: LifecycleOutcome,
) -> CompiledGraphLifecycleState:
    return replace(
        state,
        session_state=CompiledGraphSessionState.READY,
        invocation_state=CompiledGraphInvocationState.TERMINAL,
        graph_disposition=LifecycleGraphDisposition.PREPARED,
        effect_state=LifecycleEffectState.NOT_STARTED,
        retry_disposition=LifecycleRetryDisposition.AUTOMATIC_ALLOWED,
        outputs=_terminalize_outputs(state.outputs, LifecycleOutputState.NOT_CREATED),
        primary_outcome=outcome,
    )


def _expire_action(
    state: CompiledGraphLifecycleState,
) -> LifecycleTransition | LifecycleRejection:
    action = state.pending_action
    assert action is not None
    state = _complete_action(state)
    outcome = _deadline_outcome(state, action)

    if action.kind is LifecycleActionKind.VALIDATE:
        return LifecycleTransition(
            replace(
                state,
                session_state=CompiledGraphSessionState.REJECTED,
                primary_outcome=outcome,
            )
        )
    if action.kind is LifecycleActionKind.PREPARE:
        failed = replace(
            state,
            session_state=CompiledGraphSessionState.QUARANTINED,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            primary_outcome=outcome,
        )
        return _begin_teardown(failed)
    if action.kind is LifecycleActionKind.ADMIT:
        timed_out = replace(
            state,
            invocation_state=CompiledGraphInvocationState.CANCELLING,
            graph_disposition=LifecycleGraphDisposition.PREPARED,
            effect_state=LifecycleEffectState.NOT_STARTED,
            retry_disposition=LifecycleRetryDisposition.AUTOMATIC_ALLOWED,
            outputs=_terminalize_outputs(
                state.outputs,
                LifecycleOutputState.NOT_CREATED,
            ),
            primary_outcome=outcome,
        )
        return _issue_action(timed_out, LifecycleActionKind.RELEASE_CAPACITY)
    if action.kind is LifecycleActionKind.SUBMIT:
        failed = _set_primary(state, outcome)
        failed = replace(
            failed,
            graph_disposition=LifecycleGraphDisposition.DRAIN_REQUIRED,
            retry_disposition=LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED,
        )
        return _begin_invocation_drain(failed)
    if action.kind is LifecycleActionKind.CONSUME_OUTPUT:
        state = _set_output(state, action.output_index, LifecycleOutputState.RELEASE_PENDING)
        failed = _set_primary(state, outcome)
        failed = replace(
            failed,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            retry_disposition=LifecycleRetryDisposition.APPLICATION_POLICY_REQUIRED,
        )
        return _begin_invocation_drain(failed)
    if action.kind is LifecycleActionKind.RELEASE_CAPACITY:
        failed = _append_diagnostic(state, LifecycleCleanupCode.CAPACITY_RELEASE_TIMEOUT)
        failed = replace(
            failed,
            session_state=CompiledGraphSessionState.QUARANTINED,
            invocation_state=CompiledGraphInvocationState.TERMINAL,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
        )
        return _begin_teardown(failed)
    if action.kind is LifecycleActionKind.CANCEL:
        failed = _append_diagnostic(state, LifecycleCleanupCode.CANCELLATION_TIMEOUT)
        return _begin_invocation_drain(failed)
    if action.kind in {
        LifecycleActionKind.DRAIN_INVOCATION,
        LifecycleActionKind.DRAIN_SESSION,
    }:
        failed = _append_diagnostic(state, LifecycleCleanupCode.DRAIN_TIMEOUT)
        failed = _set_primary(failed, outcome)
        failed = replace(
            failed,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
        )
        return _begin_teardown(failed)
    if action.kind is LifecycleActionKind.CHECK_HEALTH:
        failed = _append_diagnostic(state, LifecycleCleanupCode.HEALTH_CHECK_FAILED)
        failed = replace(
            failed,
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
        )
        return _begin_teardown(failed)
    assert action.kind is LifecycleActionKind.TEARDOWN
    diagnostic = (
        LifecycleCleanupCode.TEARDOWN_TIMEOUT
        if outcome in {LifecycleOutcome.TEARDOWN_FAILURE, LifecycleOutcome.OUTER_DEADLINE}
        else LifecycleCleanupCode.TEARDOWN_FAILED
    )
    failed = _append_diagnostic(state, diagnostic)
    failed = _set_primary(failed, outcome)
    return LifecycleTransition(
        replace(
            failed,
            session_state=CompiledGraphSessionState.QUARANTINED,
            invocation_state=(
                CompiledGraphInvocationState.TERMINAL
                if state.invocation_identity is not None
                else CompiledGraphInvocationState.NONE
            ),
            graph_disposition=LifecycleGraphDisposition.REBUILD_REQUIRED,
            outputs=_terminalize_outputs(
                failed.outputs,
                LifecycleOutputState.CLEANUP_UNCONFIRMED,
            ),
        )
    )


def _deadline_outcome(
    state: CompiledGraphLifecycleState,
    action: LifecycleAction,
) -> LifecycleOutcome:
    stage_deadline = _stage_deadline(state.deadlines, action.kind)
    if state.deadlines.outer <= stage_deadline:
        return LifecycleOutcome.OUTER_DEADLINE
    if action.kind is LifecycleActionKind.ADMIT:
        return LifecycleOutcome.ADMISSION_TIMEOUT
    if action.kind is LifecycleActionKind.SUBMIT:
        return LifecycleOutcome.SUBMIT_TIMEOUT
    if action.kind is LifecycleActionKind.CONSUME_OUTPUT:
        return LifecycleOutcome.GET_TIMEOUT
    if action.kind in {
        LifecycleActionKind.DRAIN_INVOCATION,
        LifecycleActionKind.DRAIN_SESSION,
    }:
        return LifecycleOutcome.DRAIN_TIMEOUT
    if action.kind is LifecycleActionKind.TEARDOWN:
        return LifecycleOutcome.TEARDOWN_FAILURE
    return LifecycleOutcome.OUTER_DEADLINE


def _issue_action(
    state: CompiledGraphLifecycleState,
    kind: LifecycleActionKind,
    *,
    output_index: int | None = None,
    declared_outputs: int | None = None,
) -> LifecycleTransition | LifecycleRejection:
    assert state.pending_action is None
    if state.next_action_sequence > _MAX_ACTION_SEQUENCE:
        return _reject(
            LifecycleRejectionCode.ACTION_SEQUENCE_EXHAUSTED,
            "the bounded action sequence is exhausted",
        )
    token = (
        f"cg-lifecycle-v{COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION}:"
        f"{state.next_action_sequence}:{kind.value}"
    )
    invocation_identity = (
        state.invocation_identity
        if kind
        in {
            LifecycleActionKind.ADMIT,
            LifecycleActionKind.SUBMIT,
            LifecycleActionKind.CONSUME_OUTPUT,
            LifecycleActionKind.RELEASE_CAPACITY,
            LifecycleActionKind.CANCEL,
            LifecycleActionKind.DRAIN_INVOCATION,
            LifecycleActionKind.CHECK_HEALTH,
        }
        else None
    )
    action = LifecycleAction(
        token=token,
        kind=kind,
        session_identity=state.session_identity,
        invocation_identity=invocation_identity,
        deadline=min(state.deadlines.outer, _stage_deadline(state.deadlines, kind)),
        output_index=output_index,
        declared_outputs=declared_outputs,
    )
    next_state = replace(
        state,
        pending_action=action,
        next_action_sequence=state.next_action_sequence + 1,
    )
    if state.last_observed_at is not None and state.last_observed_at >= action.deadline:
        return _expire_action(next_state)
    return LifecycleTransition(state=next_state, actions=(action,))


def _complete_action(state: CompiledGraphLifecycleState) -> CompiledGraphLifecycleState:
    action = state.pending_action
    assert action is not None
    return replace(
        state,
        pending_action=None,
        last_completed_action_token=action.token,
    )


def _supersede_action(state: CompiledGraphLifecycleState) -> CompiledGraphLifecycleState:
    """Discard one pending action without treating its future callback as duplicate."""
    return replace(state, pending_action=None)


def _set_primary(
    state: CompiledGraphLifecycleState,
    outcome: LifecycleOutcome,
) -> CompiledGraphLifecycleState:
    if state.primary_outcome is not None:
        return state
    return replace(state, primary_outcome=outcome)


def _append_diagnostic(
    state: CompiledGraphLifecycleState,
    diagnostic: LifecycleCleanupCode,
) -> CompiledGraphLifecycleState:
    diagnostics = state.cleanup_diagnostics
    if diagnostic in diagnostics:
        return state
    if len(diagnostics) < COMPILED_GRAPH_LIFECYCLE_MAX_DIAGNOSTICS:
        return replace(state, cleanup_diagnostics=(*diagnostics, diagnostic))
    if diagnostics[-1] is LifecycleCleanupCode.DIAGNOSTICS_TRUNCATED:
        return state
    return replace(
        state,
        cleanup_diagnostics=(
            *diagnostics[: COMPILED_GRAPH_LIFECYCLE_MAX_DIAGNOSTICS - 1],
            LifecycleCleanupCode.DIAGNOSTICS_TRUNCATED,
        ),
    )


def _set_output(
    state: CompiledGraphLifecycleState,
    index: int | None,
    output_state: LifecycleOutputState,
) -> CompiledGraphLifecycleState:
    assert index is not None and 0 <= index < len(state.outputs)
    outputs = list(state.outputs)
    outputs[index] = replace(outputs[index], state=output_state)
    return replace(state, outputs=tuple(outputs))


def _terminalize_outputs(
    outputs: tuple[LifecycleOutputSlot, ...],
    output_state: LifecycleOutputState,
) -> tuple[LifecycleOutputSlot, ...]:
    return tuple(
        output if output.state in _TERMINAL_OUTPUT_STATES else replace(output, state=output_state)
        for output in outputs
    )


def _outputs_terminal(outputs: tuple[LifecycleOutputSlot, ...]) -> bool:
    return all(output.state in _TERMINAL_OUTPUT_STATES for output in outputs)


def _outputs_reuse_safe(outputs: tuple[LifecycleOutputSlot, ...]) -> bool:
    return all(output.state in _REUSE_SAFE_OUTPUT_STATES for output in outputs)


def _stage_deadline(
    deadlines: LifecycleDeadlines,
    kind: LifecycleActionKind,
) -> float:
    return {
        LifecycleActionKind.VALIDATE: deadlines.outer,
        LifecycleActionKind.PREPARE: deadlines.outer,
        LifecycleActionKind.ADMIT: deadlines.admission,
        LifecycleActionKind.SUBMIT: deadlines.submission,
        LifecycleActionKind.CONSUME_OUTPUT: deadlines.result,
        LifecycleActionKind.RELEASE_CAPACITY: deadlines.cancellation,
        LifecycleActionKind.CANCEL: deadlines.cancellation,
        LifecycleActionKind.DRAIN_INVOCATION: deadlines.drain,
        LifecycleActionKind.DRAIN_SESSION: deadlines.drain,
        LifecycleActionKind.CHECK_HEALTH: deadlines.drain,
        LifecycleActionKind.TEARDOWN: deadlines.teardown,
    }[kind]


def _validate_event(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleRejection | None:
    if not isinstance(event, LifecycleEvent) or not isinstance(event.kind, LifecycleEventKind):
        return _reject(LifecycleRejectionCode.INVALID_EVENT, "event kind is unsupported")
    if (
        isinstance(event.protocol_version, bool)
        or event.protocol_version != COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION
    ):
        return _reject(
            LifecycleRejectionCode.INVALID_EVENT,
            "event lifecycle protocol version is unsupported",
        )
    identity_error = _identity_error(event.session_identity)
    if identity_error is not None:
        return _reject(LifecycleRejectionCode.INVALID_IDENTITY, identity_error)
    if event.session_identity != state.session_identity:
        return _reject(
            LifecycleRejectionCode.STALE_SESSION_IDENTITY,
            "event run identity does not own this session",
        )
    if (
        isinstance(event.observed_at, bool)
        or not isinstance(event.observed_at, int | float)
        or not math.isfinite(event.observed_at)
        or event.observed_at < 0
    ):
        return _reject(
            LifecycleRejectionCode.INVALID_CLOCK,
            "observed_at must be a finite non-negative absolute timestamp",
        )
    if state.last_observed_at is not None and event.observed_at < state.last_observed_at:
        return _reject(
            LifecycleRejectionCode.CLOCK_REGRESSION,
            "observed_at cannot move backwards",
        )

    invocation = event.invocation_identity
    if invocation is not None:
        invocation_error = _invocation_identity_error(invocation)
        if invocation_error is not None:
            return _reject(LifecycleRejectionCode.INVALID_IDENTITY, invocation_error)
        if invocation.run_identity != state.session_identity:
            return _reject(
                LifecycleRejectionCode.STALE_SESSION_IDENTITY,
                "invocation identity belongs to another workflow run",
            )

    pending = state.pending_action
    expected_identity = pending.invocation_identity if pending is not None else None
    if event.kind is LifecycleEventKind.ADMISSION_REQUESTED:
        if event.output_index is not None:
            return _reject(
                LifecycleRejectionCode.ACTION_PAYLOAD_MISMATCH,
                "admission does not carry an output index",
            )
    elif event.kind is LifecycleEventKind.CONSUMPTION_REQUESTED:
        if event.declared_outputs is not None:
            return _reject(
                LifecycleRejectionCode.ACTION_PAYLOAD_MISMATCH,
                "consumption does not redeclare output cardinality",
            )
    elif pending is not None and pending.kind is LifecycleActionKind.CONSUME_OUTPUT:
        if event.output_index != pending.output_index or event.declared_outputs is not None:
            return _reject(
                LifecycleRejectionCode.ACTION_PAYLOAD_MISMATCH,
                "callback output index does not match its one-shot action",
            )
    elif event.output_index is not None or event.declared_outputs is not None:
        return _reject(
            LifecycleRejectionCode.ACTION_PAYLOAD_MISMATCH,
            "event carries fields that do not belong to its operation",
        )
    session_commands = {
        LifecycleEventKind.VALIDATION_REQUESTED,
        LifecycleEventKind.PREPARATION_REQUESTED,
        LifecycleEventKind.TEARDOWN_REQUESTED,
    }
    if event.kind in session_commands and invocation is not None:
        return _reject(
            LifecycleRejectionCode.INVALID_EVENT,
            "session lifecycle commands use only the four-field run identity",
        )
    if pending is not None and pending.invocation_identity is None and invocation is not None:
        return _reject(
            LifecycleRejectionCode.INVALID_EVENT,
            "session lifecycle callbacks use only the four-field run identity",
        )
    needs_invocation = event.kind in {
        LifecycleEventKind.ADMISSION_REQUESTED,
        LifecycleEventKind.SUBMISSION_REQUESTED,
        LifecycleEventKind.CONSUMPTION_REQUESTED,
        LifecycleEventKind.CANCELLATION_REQUESTED,
        LifecycleEventKind.OWNER_LOST,
    }
    if (
        event.kind is LifecycleEventKind.OUTER_DEADLINE_EXPIRED
        and state.invocation_identity is not None
        and state.invocation_state is not CompiledGraphInvocationState.TERMINAL
    ):
        needs_invocation = True
    if expected_identity is not None:
        needs_invocation = True
    if event.kind is LifecycleEventKind.DRAIN_REQUESTED:
        needs_invocation = (
            state.invocation_identity is not None
            and state.invocation_state is not CompiledGraphInvocationState.TERMINAL
        )
        if not needs_invocation and invocation is not None:
            return _reject(
                LifecycleRejectionCode.INVALID_EVENT,
                "idle session drain uses only the four-field run identity",
            )
    if needs_invocation and invocation is None:
        return _reject(
            LifecycleRejectionCode.MISSING_INVOCATION_IDENTITY,
            "event requires the complete five-field invocation identity",
        )
    if expected_identity is not None and invocation != expected_identity:
        return _reject(
            LifecycleRejectionCode.STALE_INVOCATION_IDENTITY,
            "callback invocation identity does not match its action",
        )
    if (
        invocation is not None
        and event.kind is not LifecycleEventKind.ADMISSION_REQUESTED
        and state.invocation_identity is not None
        and invocation != state.invocation_identity
    ):
        return _reject(
            LifecycleRejectionCode.STALE_INVOCATION_IDENTITY,
            "event invocation identity is stale",
        )
    return None


def _identity_error(identity: object) -> str | None:
    if not isinstance(identity, WorkflowRunIdentity):
        return "session identity must be a WorkflowRunIdentity"
    if (
        isinstance(identity.task_execution_pk, bool)
        or not isinstance(identity.task_execution_pk, int)
        or identity.task_execution_pk < 1
    ):
        return "task_execution_pk must be a positive integer"
    if (
        isinstance(identity.attempt_number, bool)
        or not isinstance(identity.attempt_number, int)
        or identity.attempt_number < 1
    ):
        return "attempt_number must be a positive integer"
    if (
        isinstance(identity.execution_generation, bool)
        or not isinstance(identity.execution_generation, int)
        or identity.execution_generation < 0
    ):
        return "execution_generation must be a non-negative integer"
    if (
        not isinstance(identity.run_id, str)
        or not identity.run_id
        or len(identity.run_id) > _MAX_ID_CHARS
    ):
        return f"run_id must contain 1 to {_MAX_ID_CHARS} characters"
    return None


def _invocation_identity_error(identity: object) -> str | None:
    if not isinstance(identity, WorkflowInvocationIdentity):
        return "invocation identity must be a WorkflowInvocationIdentity"
    run_error = _identity_error(identity.run_identity)
    if run_error is not None:
        return run_error
    if (
        not isinstance(identity.invocation_id, str)
        or not identity.invocation_id
        or len(identity.invocation_id) > _MAX_ID_CHARS
    ):
        return f"invocation_id must contain 1 to {_MAX_ID_CHARS} characters"
    return None


def _validate_deadlines(deadlines: object) -> None:
    if not isinstance(deadlines, LifecycleDeadlines):
        raise TypeError("deadlines must be a LifecycleDeadlines instance")
    for name, value in deadlines.as_dict().items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} deadline must be a finite non-negative timestamp")


def _validate_capacity(capacity: object) -> None:
    if not isinstance(capacity, LifecycleCapacity):
        raise TypeError("capacity must be a LifecycleCapacity instance")
    expected = {
        "session_owners": 1,
        "callers": 1,
        "maximum_in_flight": 1,
        "maximum_buffered_results": 1,
        "owner_concurrency": 1,
        "queue_capacity": 0,
    }
    values = capacity.as_dict()
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values.values()):
        raise ValueError("lifecycle capacity fields must be integers")
    if values != expected:
        raise ValueError("lifecycle protocol v1 requires exact single-owner capacity")


def _state_invariant_error(state: object) -> str | None:
    if not isinstance(state, CompiledGraphLifecycleState):
        return "state must be a CompiledGraphLifecycleState"
    if (
        isinstance(state.protocol_version, bool)
        or state.protocol_version != COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION
    ):
        return "state protocol version is unsupported"
    identity_error = _identity_error(state.session_identity)
    if identity_error is not None:
        return identity_error
    try:
        _validate_deadlines(state.deadlines)
        _validate_capacity(state.capacity)
    except (TypeError, ValueError) as error:
        return str(error)
    if not isinstance(state.session_state, CompiledGraphSessionState):
        return "session state is unsupported"
    if not isinstance(state.invocation_state, CompiledGraphInvocationState):
        return "invocation state is unsupported"
    if not isinstance(state.effect_state, LifecycleEffectState):
        return "effect state is unsupported"
    if not isinstance(state.graph_disposition, LifecycleGraphDisposition):
        return "graph disposition is unsupported"
    if not isinstance(state.retry_disposition, LifecycleRetryDisposition):
        return "retry disposition is unsupported"
    if not isinstance(state.strategy_fallback_allowed, bool):
        return "strategy fallback flag must be boolean"
    if state.primary_outcome is not None and not isinstance(
        state.primary_outcome, LifecycleOutcome
    ):
        return "primary outcome is unsupported"
    if not isinstance(state.outputs, tuple) or any(
        not isinstance(output, LifecycleOutputSlot) for output in state.outputs
    ):
        return "outputs must be an immutable tuple of output slots"
    if any(not isinstance(output.state, LifecycleOutputState) for output in state.outputs):
        return "output state is unsupported"
    if not isinstance(state.cleanup_diagnostics, tuple) or any(
        not isinstance(diagnostic, LifecycleCleanupCode) for diagnostic in state.cleanup_diagnostics
    ):
        return "cleanup diagnostics contain an unsupported code"
    if state.last_observed_at is not None and (
        isinstance(state.last_observed_at, bool)
        or not isinstance(state.last_observed_at, int | float)
        or not math.isfinite(state.last_observed_at)
        or state.last_observed_at < 0
    ):
        return "last observed timestamp is invalid"
    if (
        isinstance(state.next_action_sequence, bool)
        or not isinstance(state.next_action_sequence, int)
        or not 1 <= state.next_action_sequence <= _MAX_ACTION_SEQUENCE + 1
    ):
        return "next action sequence is outside its bounded range"
    if state.last_completed_action_token is not None and (
        not isinstance(state.last_completed_action_token, str)
        or not state.last_completed_action_token
        or len(state.last_completed_action_token) > _MAX_ID_CHARS
    ):
        return "last completed action token is invalid"
    if state.invocation_identity is None:
        if state.invocation_state is not CompiledGraphInvocationState.NONE:
            return "invocation state requires an invocation identity"
        if state.outputs:
            return "output slots require an invocation identity"
    else:
        invocation_error = _invocation_identity_error(state.invocation_identity)
        if invocation_error is not None:
            return invocation_error
        if state.invocation_identity.run_identity != state.session_identity:
            return "invocation identity belongs to another session"
        if state.invocation_state is CompiledGraphInvocationState.NONE:
            return "invocation identity requires an invocation state"
    if len(state.outputs) > COMPILED_GRAPH_LIFECYCLE_MAX_OUTPUTS:
        return "output cardinality exceeds the protocol bound"
    if any(
        isinstance(output.index, bool) or not isinstance(output.index, int) or output.index != index
        for index, output in enumerate(state.outputs)
    ):
        return "output slots must have dense ordered indexes"
    if len(state.cleanup_diagnostics) > COMPILED_GRAPH_LIFECYCLE_MAX_DIAGNOSTICS:
        return "cleanup diagnostics exceed the protocol bound"
    if state.graph_disposition is LifecycleGraphDisposition.REUSABLE:
        if state.session_state is not CompiledGraphSessionState.READY:
            return "a reusable graph must be ready"
        if state.invocation_identity is not None and not _outputs_reuse_safe(state.outputs):
            return "a reusable graph requires terminal output ownership"
    if any(output.state is LifecycleOutputState.NOT_CREATED for output in state.outputs):
        if state.effect_state is not LifecycleEffectState.NOT_STARTED:
            return "NOT_CREATED is legal only before submission may have started"
    if (
        any(
            output.state is LifecycleOutputState.UNAVAILABLE_AFTER_TEARDOWN
            for output in state.outputs
        )
        and state.graph_disposition is not LifecycleGraphDisposition.TORN_DOWN
    ):
        return "UNAVAILABLE_AFTER_TEARDOWN requires confirmed teardown"
    if any(output.state is LifecycleOutputState.LOST_WITH_OWNER for output in state.outputs):
        if (
            state.primary_outcome is not LifecycleOutcome.OWNER_LOST
            or state.graph_disposition is not LifecycleGraphDisposition.REBUILD_REQUIRED
            or state.retry_disposition
            is not LifecycleRetryDisposition.OPERATOR_RECONCILIATION_REQUIRED
        ):
            return "LOST_WITH_OWNER requires indeterminate operator reconciliation"
    if (
        state.session_state
        in {
            CompiledGraphSessionState.PREPARING,
            CompiledGraphSessionState.READY,
            CompiledGraphSessionState.DRAINING,
            CompiledGraphSessionState.QUARANTINED,
            CompiledGraphSessionState.TORN_DOWN,
        }
        and state.strategy_fallback_allowed
    ):
        return "strategy fallback cannot reopen after preparation starts"
    if (
        state.effect_state is LifecycleEffectState.MAY_HAVE_STARTED
        and state.retry_disposition is LifecycleRetryDisposition.AUTOMATIC_ALLOWED
    ):
        return "automatic invocation retry is forbidden after submission starts"
    if state.invocation_state is CompiledGraphInvocationState.TERMINAL and not _outputs_terminal(
        state.outputs
    ):
        return "terminal invocation phases require terminal output ownership"
    if (
        state.invocation_state is CompiledGraphInvocationState.CHECKING_HEALTH
        and not _outputs_reuse_safe(state.outputs)
    ):
        return "health classification requires reuse-safe output ownership"
    if state.pending_action is not None:
        if not isinstance(state.pending_action, LifecycleAction):
            return "pending action is unsupported"
        if not isinstance(state.pending_action.kind, LifecycleActionKind):
            return "pending action kind is unsupported"
        if (
            isinstance(state.pending_action.protocol_version, bool)
            or state.pending_action.protocol_version != COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION
        ):
            return "pending action protocol version is unsupported"
        if (
            not isinstance(state.pending_action.token, str)
            or not state.pending_action.token
            or len(state.pending_action.token) > _MAX_ID_CHARS
        ):
            return "pending action token is invalid"
        if (
            isinstance(state.pending_action.deadline, bool)
            or not isinstance(state.pending_action.deadline, int | float)
            or not math.isfinite(state.pending_action.deadline)
            or state.pending_action.deadline < 0
        ):
            return "pending action deadline is invalid"
        if state.pending_action.session_identity != state.session_identity:
            return "pending action belongs to another session"
        invocation_action_kinds = {
            LifecycleActionKind.ADMIT,
            LifecycleActionKind.SUBMIT,
            LifecycleActionKind.CONSUME_OUTPUT,
            LifecycleActionKind.RELEASE_CAPACITY,
            LifecycleActionKind.CANCEL,
            LifecycleActionKind.DRAIN_INVOCATION,
            LifecycleActionKind.CHECK_HEALTH,
        }
        if state.pending_action.kind in invocation_action_kinds:
            if state.pending_action.invocation_identity != state.invocation_identity:
                return "pending invocation action has a stale identity"
        elif state.pending_action.invocation_identity is not None:
            return "pending session action must not carry an invocation identity"
        if state.pending_action.kind is LifecycleActionKind.CONSUME_OUTPUT:
            if (
                isinstance(state.pending_action.output_index, bool)
                or not isinstance(state.pending_action.output_index, int)
                or not 0 <= state.pending_action.output_index < len(state.outputs)
            ):
                return "consume action output index is invalid"
        elif state.pending_action.output_index is not None:
            return "non-consume action carries an output index"
        if state.pending_action.kind is LifecycleActionKind.ADMIT:
            if state.pending_action.declared_outputs != len(state.outputs):
                return "admit action cardinality does not match output slots"
        elif state.pending_action.declared_outputs is not None:
            return "non-admit action carries output cardinality"
        if state.pending_action.token == state.last_completed_action_token:
            return "pending and completed action tokens cannot match"
        if state.pending_action.deadline > state.deadlines.outer:
            return "pending action exceeds the outer deadline"
        if state.pending_action.deadline != min(
            state.deadlines.outer,
            _stage_deadline(state.deadlines, state.pending_action.kind),
        ):
            return "pending action deadline does not match its absolute phase budget"

    pending_kind = state.pending_action.kind if state.pending_action is not None else None
    required_session_action = {
        CompiledGraphSessionState.VALIDATING: LifecycleActionKind.VALIDATE,
        CompiledGraphSessionState.PREPARING: LifecycleActionKind.PREPARE,
    }.get(state.session_state)
    if required_session_action is not None and pending_kind is not required_session_action:
        return "session phase does not match its pending action"
    if (
        state.session_state
        in {
            CompiledGraphSessionState.NEW,
            CompiledGraphSessionState.VALIDATED,
            CompiledGraphSessionState.REJECTED,
            CompiledGraphSessionState.QUARANTINED,
            CompiledGraphSessionState.TORN_DOWN,
        }
        and state.pending_action is not None
    ):
        return "terminal or idle session phase cannot retain an action"
    if state.session_state is CompiledGraphSessionState.DRAINING and state.pending_action is None:
        return "draining session requires a bounded pending action"
    if (
        state.session_state
        in {
            CompiledGraphSessionState.NEW,
            CompiledGraphSessionState.VALIDATING,
            CompiledGraphSessionState.VALIDATED,
            CompiledGraphSessionState.PREPARING,
            CompiledGraphSessionState.REJECTED,
        }
        and state.graph_disposition is not LifecycleGraphDisposition.UNPREPARED
    ):
        return "unprepared session phase has an incompatible graph disposition"
    if state.session_state is CompiledGraphSessionState.READY and state.graph_disposition not in {
        LifecycleGraphDisposition.PREPARED,
        LifecycleGraphDisposition.REUSABLE,
    }:
        return "ready session has an incompatible graph disposition"
    if (
        state.session_state is CompiledGraphSessionState.QUARANTINED
        and state.graph_disposition is not LifecycleGraphDisposition.REBUILD_REQUIRED
    ):
        return "quarantined session must require a graph rebuild"
    if state.session_state is CompiledGraphSessionState.TORN_DOWN and (
        state.graph_disposition is not LifecycleGraphDisposition.TORN_DOWN
    ):
        return "torn-down session must have a torn-down graph"

    allowed_invocation_actions = {
        CompiledGraphInvocationState.NONE: {
            None,
            LifecycleActionKind.VALIDATE,
            LifecycleActionKind.PREPARE,
            LifecycleActionKind.DRAIN_SESSION,
            LifecycleActionKind.TEARDOWN,
        },
        CompiledGraphInvocationState.ADMITTING: {LifecycleActionKind.ADMIT},
        CompiledGraphInvocationState.ADMITTED: {None},
        CompiledGraphInvocationState.SUBMITTING: {LifecycleActionKind.SUBMIT},
        CompiledGraphInvocationState.RUNNING: {None},
        CompiledGraphInvocationState.CONSUMING: {LifecycleActionKind.CONSUME_OUTPUT},
        CompiledGraphInvocationState.CANCELLING: {
            LifecycleActionKind.RELEASE_CAPACITY,
            LifecycleActionKind.CANCEL,
        },
        CompiledGraphInvocationState.DRAINING: {
            LifecycleActionKind.DRAIN_INVOCATION,
            LifecycleActionKind.TEARDOWN,
        },
        CompiledGraphInvocationState.CHECKING_HEALTH: {LifecycleActionKind.CHECK_HEALTH},
        CompiledGraphInvocationState.TERMINAL: {
            None,
            LifecycleActionKind.DRAIN_SESSION,
            LifecycleActionKind.TEARDOWN,
        },
    }[state.invocation_state]
    if pending_kind not in allowed_invocation_actions:
        return "invocation phase does not match its pending action"
    return None


def _invalid_transition(
    state: CompiledGraphLifecycleState,
    event: LifecycleEvent,
) -> LifecycleRejection:
    return _reject(
        LifecycleRejectionCode.INVALID_TRANSITION,
        f"{event.kind.value} is invalid for {state.session_state.value}/"
        f"{state.invocation_state.value}",
    )


def _reject(code: LifecycleRejectionCode, message: str) -> LifecycleRejection:
    return LifecycleRejection(
        code=code,
        message=message[:_MAX_REJECTION_MESSAGE_CHARS],
    )
