# ADR-0007: Durable Step Checkpoints and Fail-Closed Resume Planning

## Tracking

| Field | Value |
| --- | --- |
| Decision status | Proposed |
| Implementation status | Partial |
| Owners | `django-ray` maintainers |
| Proposed | 2026-11-01 |
| Accepted | Not accepted |
| Decision review | PR `#694` |
| Delivery tracking | `#289` |
| Supersedes | None |
| Superseded by | None |
| Last verified | 2026-11-01 (`src/django_ray/workflow/checkpoints.py`) |

## Context and scope

Currently, retrying a failed or interrupted workflow task in `django-ray` re-executes the entire workflow plan from the root under a new `attempt_number` and `execution_generation`. For long-running multi-step workflows, re-executing already completed upstream nodes wastes cluster compute and poses a safety risk when upstream steps perform external side effects.

However, naïve automatic resume from partial step outputs is unsafe unless the runtime explicitly models:
1. **Node resumability class** (`PURE_DETERMINISTIC`, `IDEMPOTENT_SIDE_EFFECT`, `NON_IDEMPOTENT_SIDE_EFFECT`, `EPHEMERAL_UNCHECKPOINTABLE`).
2. **Side-effect disposition** (`NONE`, `NOT_STARTED`, `IN_FLIGHT_INDETERMINATE`, `COMMITTED`, `ROLLED_BACK`).
3. **Transitive dependency-cone invalidation** when an upstream step is missing, corrupted, ephemeral, or forced to re-run.
4. **Attempt/generation fencing** so stale or future attempts cannot poison resume planning.

## Decision

We adopt a fail-closed, Ray-free reference state machine (`src/django_ray/workflow/checkpoints.py`) as the authoritative contract before introducing database schema migrations or public resume APIs:

1. **Pinned Plan Fingerprint Verification**: All candidate step checkpoints must match the pinned `workflow_plan_fingerprint` of the resuming `RayTaskExecution` and carry `(source_attempt_number, source_execution_generation)` strictly prior to the resuming attempt.
2. **Explicit Side-Effect Indeterminacy Refusal**: Any node recorded in `SideEffectDisposition.IN_FLIGHT_INDETERMINATE` (where the worker lost lease or crashed after starting a step before recording completion) fails closed with `CheckpointIndeterminateSideEffectError` unless the node is `PURE_DETERMINISTIC` or `IDEMPOTENT_SIDE_EFFECT` with a non-empty `idempotency_key`.
3. **Transitive Dependency-Cone Invalidation**: Any node whose checkpoint is absent, digest-mismatched, `EPHEMERAL_UNCHECKPOINTABLE`, or explicitly listed in `force_reexecute_node_ids` invalidates all reachable downstream nodes in the DAG (`ResumeNodeDisposition.UPSTREAM_INVALIDATED`).
4. **Non-Idempotent Downstream Protection**: If transitive invalidation reaches a downstream node that already has a `COMMITTED` checkpoint with `NodeResumabilityClass.NON_IDEMPOTENT_SIDE_EFFECT`, automatic resume fails closed with `CheckpointNonIdempotentInvalidationError` rather than silently re-executing a committed non-idempotent side effect.

## Alternatives considered

- **Implicitly checkpointing and reusing all completed step outputs**: Rejected because ephemeral handles cannot survive worker restarts and non-idempotent or indeterminate side effects can cause duplicate external mutations.
- **Adding a database table before validating the state machine**: Rejected per `#289`; proving the fail-closed state machine in a Ray-free module avoids premature schema lock-in.

## Consequences and rollout

- Provides an executable, deterministic state machine (`WorkflowCheckpointResumePlanner`) verified by unit tests without importing Ray or requiring a database migration.
- Subsequent schema/API work under `#289` can bind durable checkpoint storage directly to `DurableStepCheckpointEnvelope` and `WorkflowResumePlan`.

## Implementation and evidence

| Requirement | State | Source or delivery issue | Validation evidence |
| --- | --- | --- | --- |
| Ray-free checkpoint envelope, side-effect classification, and fail-closed resume planner | Implemented | `src/django_ray/workflow/checkpoints.py`, `#289` | `tests/unit/test_workflow_checkpoints.py` |
| Durable PostgreSQL checkpoint table and public resume API | Not started | `#289` | Deferred until post-ADR storage integration |

## Revisit and supersession

Revisit when wiring durable step checkpoint persistence into `RayTaskExecution` retry/resume orchestration.

## Change history

| Date | Change | Evidence |
| --- | --- | --- |
| 2026-11-01 | Proposed ADR-0007 and implemented Ray-free reference state machine | PR `#694`, `tests/unit/test_workflow_checkpoints.py` |
