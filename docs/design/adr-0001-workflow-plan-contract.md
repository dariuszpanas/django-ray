# ADR-0001: Workflow Plans and Execution Strategies

- **Status:** Accepted
- **Date:** 2026-07-19
- **Decision owners:** django-ray maintainers
- **Related contract:** [Workflow Plans and Execution Strategies](../workflow-plans.md)

## Context

django-ray currently exposes lazy `WorkflowSignature` expressions through `step`,
`chain`, `group`, and `map_step`. The expression objects directly expand and submit
ordinary Ray tasks. One outer Django task supplies durable queueing, retry,
cancellation, result storage, and progress persistence; internal leaves are not durable
tasks.

That model is useful but does not distinguish a reusable definition from an immutable
execution plan, one durable run from a repeated invocation, logical work items from
physical actor replicas, or a prepared graph from the process that owns it. It also
allows definition objects to retain mutable dictionaries and resolves named per-step
RuntimeEnv profiles during submission.

Ray Compiled Graph is a beta execution API for repeated invocation of a statically
shaped actor graph. Its engine-specific ownership, capacity, result, and teardown rules
cannot safely define django-ray's task model. Adding a `compiled=True` branch to the
current recursive submitter would mix durable lifecycle semantics with a beta runtime
handle and make graph reuse, invalidation, cancellation, and fallback ambiguous.

## Decision

django-ray will introduce a strategy-neutral, versioned effective execution-plan
boundary between workflow definitions and executors.

1. `WorkflowSignature` remains the source-compatible public definition builder. It is
   not the durable plan or graph-cache key.
2. A materializer will convert a definition into a plan template and then a fully
   resolved effective plan before remote submission.
3. The effective plan is deeply immutable, canonical JSON with a secret-free SHA-256
   fingerprint. It contains logical and physical topology, callable/code identity,
   resolved RuntimeEnv identity, bounds, resources, transport, effects, lifecycle,
   owner, durability, and compatibility requirements. It contains no Ray runtime
   object.
4. Definition-time bound values and `run()` arguments are invocation bindings by
   default. Runtime Kubernetes inventory, request state, credentials, and secrets stay
   in the invocation envelope and do not define graph topology.
5. One `RayTaskExecution` remains the outer durability and recovery boundary. A run is
   scoped by task primary key, attempt number, execution generation, and run ID; a
   repeated plan execution adds an invocation ID.
6. Execution is delegated to a strategy with explicit validation, preparation,
   admission, execution, result consumption, cancellation, drain, and cleanup
   contracts. Local and dynamic Ray-task execution remain the compatibility baseline.
7. Strategy eligibility produces bounded structured rejection reasons before
   submission. Automatic fallback is permitted only before preparation or execution
   can create resources or application effects.
8. The first effective-plan identity is pinned to the task's attempt chain. A retry
   rematerializes and verifies that identity before submission; a mismatch fails closed
   rather than silently running changed code or environments.
9. Selected strategy, plan fingerprint, requested policy, and rejection summary are
   durable run metadata independent of optional node-level progress reporting.
10. A future Compiled Graph adapter is only one execution strategy for eligible static
    or fixed-width actor regions. It is not a Django task type, a new durable boundary,
    or an automatic property of `chain` and `group`.

The normative field separation, capability vocabulary, fingerprint/invalidation rules,
compatibility mapping, diagnostics, and testable implementation requirements are
maintained in the linked workflow-plan contract.

## Why Compiled Graph is a strategy, not a task type

A Django task answers control-plane questions: what is queued, who owns the claim, how
long it may run, whether it can retry, where its result is stored, and which terminal
state is authoritative. Compiled Graph answers execution-plane questions: how a fixed
actor graph is prepared, how inputs enter preallocated channels, how many invocations
may be in flight, who may call it, how results are consumed, and how its resources are
torn down.

Those questions have different lifetimes. One compiled graph instance may serve many
invocations, potentially across durable runs if a later ownership ADR permits that. One
durable run may also execute dynamic discovery outside a compiled region. Making
Compiled Graph a task type would force a false one-to-one relationship, leak beta
objects into persistence, and couple Django retry/cancellation semantics to one Ray
transport.

Keeping compilation behind the strategy boundary preserves the durable task contract,
allows an ordinary dynamic baseline and future engines to share the same effective
plan, and makes compatibility or platform rejection an execution decision rather than
a new application model.

## Consequences

### Positive

- Current callers retain the workflow builder and result semantics while implementation
  gains a stable validation and strategy seam.
- Static, dynamic, and fixed-width shapes can be classified honestly. Changing logical
  inventory can remain invocation data in a fixed-width physical plan.
- Canonical plan identity gives per-step RuntimeEnv changes, code revisions, resource
  layout, transport, and platform compatibility an explicit invalidation path.
- Strategy rejection and fallback become explainable and testable.
- The durable run identity can fence progress, owner callbacks, cancellation, results,
  and cleanup without treating progress as a recovery log.
- Beta Compiled Graph handles, one-time result behavior, and owner lifetime remain
  isolated inside an adapter.

### Costs and risks

- Plan materialization introduces validation and serialization work before submission.
- Conservative code and compatibility identities may reduce cache reuse, especially in
  development working directories.
- Retry pinning can reject a task after a deployment instead of silently using new
  workflow code. Operators need an explicit re-enqueue or future audited migration
  path.
- Current bound values must be lifted into invocation slots. The compatibility adapter
  must preserve argument ordering, bound-keyword precedence, and ordered collections.
- Secret-dependent RuntimeEnv reuse requires an external non-secret revision identity;
  otherwise resident strategies must reject the plan.
- A plan contract does not prove side-effect safety. Application controls and failure
  tests remain necessary.

## Alternatives considered

### Add `compiled=True` to `WorkflowSignature.run()`

Rejected. It mixes materialization, eligibility, owner selection, compilation, result
ownership, and fallback into the current recursive submitter. It also implies that the
same Python expression is already a canonical reusable plan.

### Introduce a separate compiled Django task type

Rejected. It couples a beta Ray execution optimization to durable task semantics and
cannot represent a dynamic outer workflow containing one compiled region or one graph
instance serving repeated invocations.

### Persist `WorkflowSignature` or Ray DAG objects

Rejected. Python objects can be mutable and process-local; Ray DAG, actor, object, and
compiled references are version- and process-specific. They are unsuitable as a
canonical database protocol or graph-cache identity.

### Fingerprint only callable paths and graph edges

Rejected. The same paths and edges can execute with different code, RuntimeEnv
contents, resources, actor layout, transport, buffers, failure policy, or platform
semantics. Reuse would be unsafe and difficult to diagnose.

### Put bound values and discovered inventory in the fingerprint

Rejected. It would expose or hash secret/request material, invalidate fixed-width plans
for every inventory change, and prevent reuse across compatible invocations. Binding
shape belongs to the plan; ordinary values belong to the invocation.

### Allow retry to adopt whatever definition is deployed

Rejected for the effective-plan path. It could run a changed per-step environment or
route into an incompatible resident graph while retaining the original durable task
identity. A changed definition requires a new task or an explicit future migration
policy.

## Compatibility and rollout

This ADR changes no executor and adds no persistence or public API. The current dynamic
Ray-task and local paths remain authoritative until implementation issues deliver the
plan materializer and strategy interface.

Implementation proceeds in separate changes:

1. snapshot and test the effective plan contract;
2. fence run-scoped progress and callbacks;
3. introduce the strategy interface with the current dynamic engine as default;
4. benchmark and implement static actors only if evidence supports them;
5. add a guarded Compiled Graph strategy only on validated platforms and ownership
   topologies, after issue #99 reviews an exact immutable deployment/image,
   shared-memory, and object-store capability row rather than inferring one from a
   generic host or container.

During rollout, legacy progress snapshots and direct `WorkflowSignature` execution need
an additive compatibility reader. No persisted beta reference is introduced.

## Validation required of later implementations

The implementation must satisfy the stable `PLAN-01` through `PLAN-14` requirements in
the [workflow-plan contract](../workflow-plans.md#acceptance-contract-for-plan-snapshot-implementation).
In particular, it must prove canonical equality and invalidation, deep immutability,
secret exclusion, per-step RuntimeEnv resolution, retry pinning, structured eligibility,
run-scoped observability, and result parity for existing workflow APIs.

## Upstream context

- [Ray Compiled Graph overview](https://docs.ray.io/en/latest/ray-core/compiled-graph/ray-compiled-graph.html)
- [Ray Compiled Graph limitations and troubleshooting](https://docs.ray.io/en/latest/ray-core/compiled-graph/troubleshooting.html)
