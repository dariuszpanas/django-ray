# ADR-0003: Compiled Invocation Lifecycle

- **Status:** Accepted as a Ray-free protocol contract
- **Date:** 2026-07-20
- **Decision owners:** django-ray maintainers
- **Related contracts:** [Workflow Plans and Execution Strategies](../workflow-plans.md),
  [ADR-0001](adr-0001-workflow-plan-contract.md),
  [ADR-0002](adr-0002-compiled-session-ownership.md)

## Context

ADR-0001 makes Compiled Graph an execution strategy rather than a Django task type.
ADR-0002 selects one outer Ray Core task as the first evidence owner and limits that
owner to serial invocations inside one durable run. Neither decision defines the
invocation state machine that an eventual adapter must follow.

That missing boundary matters even before a native adapter exists. A compiled result
is owned by the process that invoked the graph, must be consumed once, and cannot be
treated as an ordinary durable object reference. Preparation can reserve actors and
channels before an input is submitted. Cancellation, timeout, result consumption,
drain, and cleanup can then race with one another. Reusing a prepared graph also has a
different meaning from retrying a durable Django task.

If those rules live only in a future Ray adapter, beta API behavior would define
django-ray's durable semantics. Tests would need Ray to exercise basic lifecycle
decisions, and different owner implementations could disagree about fallback,
deadline, or output ownership.

## Decision

django-ray defines a versioned, deterministic Compiled Graph invocation lifecycle
protocol before implementing a native adapter. Protocol version 1 is represented in
effective plans at the exact path
`strategy_requirements.compiled_graph.lifecycle_protocol_version`.

The implementation boundary is a standard-library-only reducer. Given the same
initial state and the same ordered events, it produces the same next state, terminal
outcome, bounded diagnostics, and JSON-safe snapshot. It does not import Ray, inspect
process-global runtime state, read a clock, submit work, consume a native result, or
perform cleanup. A future adapter performs those effects and reports their observed
outcomes back as protocol events.

This decision establishes the lifecycle contract only. It does not enable a Compiled
Graph strategy, add a public compilation flag, create actors or channels, or promote
any native capability tuple. The compatibility policy still has no verified native
row.

## Identity scopes

Plan, session, run, and invocation identities answer different questions and must not
be substituted for one another.

| Scope | Required identity | What it fences |
|---|---|---|
| Effective plan | Plan fingerprint plus Compiled Graph compatibility and lifecycle protocol versions | The semantics and exact preparation contract that may share a graph identity |
| Execution session | Graph key, stable session identity, and owner epoch or equivalent adapter fence | Prepared actors, channels, admission, drain, and cleanup owned by one process |
| Durable run | `(task_execution_pk, attempt_number, execution_generation, run_id)` | The outer task execution allowed to publish current durable state |
| Invocation | Durable run identity plus a unique `invocation_id` | One admitted input, its output ownership, cancellation, outcome, and diagnostics |

For the ADR-0002 pilot, one session is bound to one durable run and may serve multiple
serial invocation IDs. The session is not bound to only the first invocation, and an
invocation ID is never a graph-cache key. Session validation, preparation, readiness,
idle-drain, quarantine, and teardown actions and events carry the exact four-field run
identity. Invocation admission, submission, callback, cancellation, result, drain, and
cleanup actions and events carry the complete five-field invocation identity. A
matching plan fingerprint or session ID alone is not enough to accept a callback or
output.

A future cross-run owner would need the owner-route and epoch protocol described in
ADR-0002. This lifecycle does not authorize that topology. It merely keeps session
identity separate so a later owner cannot accidentally turn plan equality into current
invocation authority.

## Reducer and adapter boundary

The version 1 Python boundary exposes `WorkflowInvocationIdentity`,
`CompiledGraphSessionState`, `CompiledGraphInvocationState`, versioned lifecycle
deadlines, events, actions, transitions, and rejections. `initial_session()` validates
the handle-free inputs and creates `NEW` state, `reduce_lifecycle(state, event)` is the
only transition function, and `lifecycle_snapshot()` emits the bounded durable
representation. A future
`CompiledGraphLifecycleAdapter` implements `dispatch(action) -> event` without becoming
the lifecycle authority.

The reducer owns semantic decisions:

- legal phase transitions and terminal immutability;
- full run and invocation identity matching;
- the preparation cutoff for strategy fallback and the submission cutoff for
  invocation re-execution;
- deadline and outer-control precedence;
- output-slot ownership and one-shot disposition;
- classified terminal outcome and cleanup completeness; and
- bounded, deterministic diagnostics and snapshots.

The future execution adapter owns effects. Its version 1 action set is exact:
`VALIDATE`, `PREPARE`, `ADMIT`, `SUBMIT`, `CONSUME_OUTPUT`, `RELEASE_CAPACITY`,
`CANCEL`, `DRAIN_INVOCATION`, `DRAIN_SESSION`, `CHECK_HEALTH`, and `TEARDOWN`.
In concrete terms, the adapter is responsible for:

- evaluating compatibility and preparing actors, channels, and a compiled handle;
- observing the outer cancellation state and absolute clock;
- submitting exactly one invocation input when the reducer permits it;
- waiting for and settling native outputs through one permitted disposition;
- requesting cancellation where the engine supports it;
- draining and tearing down engine resources; and
- translating bounded native observations into protocol events.

The reducer retains the pending action and at most the last completed bounded
deterministic token, not an event history. The adapter dispatches that exact action and
returns an event that echoes its token and identity. A duplicate, stale, or out-of-order
callback is rejected without retaining previous callbacks and cannot advance state.

The adapter must ask the reducer before each irreversible action and report the result
afterward. It must not mutate reducer state, infer a terminal outcome from an exception
alone, reset a deadline when a phase changes, or publish native handles in a snapshot.
An adapter exception is an observation to classify, drain, and clean up; it is not a
second state machine.

Tests may implement `CompiledGraphLifecycleAdapter` as a deterministic fake that records
actions and returns bounded events. That proves reducer/adapter ordering only; it is not
Ray, owner-topology, transport, or side-effect evidence.

## Version 1 capacity boundary

The reducer enforces the conservative ADR-0002 pilot values exactly:

```text
owner processes per durable run = 1
graph instances per session     = 1
concurrent callers per session  = 1
owner adapter concurrency       = 1
max in-flight invocations       = 1
max buffered invocation results = 1
owner-side queued invocations   = 0
```

`LifecycleCapacity` records the executable fields as `session_owners=1`, `callers=1`,
`maximum_in_flight=1`, `maximum_buffered_results=1`, `owner_concurrency=1`, and
`queue_capacity=0`. One lifecycle session itself owns the single graph instance.

Protocol version 1 rejects a lifecycle configuration that requests any other value.
These are semantic admission and ownership limits, not benchmark defaults. Increasing
one requires a new lifecycle protocol decision with revised ordering, deadline,
output-accounting, and fairness evidence.

## Lifecycle phases

Protocol version 1 keeps session state and invocation state independent. A terminal
invocation does not close a healthy session, and a cleaned-up session does not invent a
successful invocation.

Session state uses this exact version 1 vocabulary:

| Session state | Meaning |
|---|---|
| `NEW` | The handle-free initial state before validation |
| `VALIDATING` | Plan, compatibility, run identity, bounds, and deadlines are being validated before resource allocation |
| `VALIDATED` | Validation succeeded and preparation has not started |
| `PREPARING` | The adapter may allocate actors, channels, buffers, and the compiled handle; strategy fallback is already closed |
| `READY` | The exact graph instance is healthy enough to admit one serial invocation |
| `DRAINING` | Admission is stopped and session-owned reachable work is being settled |
| `QUARANTINED` | Health, submission, effects, or ownership cannot be proven safe enough for reuse |
| `REJECTED` | Validation rejected the selected strategy without preparing it |
| `TORN_DOWN` | Teardown completed and the session owns no reusable graph |

Invocation state uses a separate exact vocabulary:

| Invocation state | Meaning |
|---|---|
| `NONE` | The session has not admitted an invocation |
| `ADMITTING` | One complete five-field identity and declared output count are being admitted |
| `ADMITTED` | Capacity is held and submission has not started |
| `SUBMITTING` | Submission start permanently closed same-invocation replay |
| `RUNNING` | Submission succeeded and declared outputs may be consumed |
| `CONSUMING` | One declared output's one-shot opportunity is in progress |
| `CANCELLING` | The bounded cooperative cancellation action is in progress |
| `DRAINING` | Reachable invocation work and outputs are being settled without replay |
| `CHECKING_HEALTH` | Output ownership is terminal and graph reuse health is being checked |
| `TERMINAL` | Primary outcome, effect state, retry disposition, and every output's terminal ownership classification are fixed; this does not imply confirmed cleanup or graph reuse |

Preparation, invocation cleanup, and session teardown may be no-ops for an adapter, but
the adapter must still report their protocol transitions. A terminal business outcome
does not imply that cleanup succeeded. The final snapshot therefore keeps invocation
outcome, effect certainty, graph disposition, retry disposition, and cleanup status as
independent facts.

## Absolute deadlines and outer precedence

Every lifecycle budget is a distinct absolute deadline supplied by the outer durable
execution or derived once before lifecycle reduction. `LifecycleDeadlines` contains
exactly `outer`, `admission`, `submission`, `result`, `cancellation`, `drain`, and
`teardown`. Validation and preparation use the outer deadline rather than inventing
two additional budgets. No action receives a fresh relative budget when the reducer
enters a new state. An adapter may use monotonic time internally, but any timeout event
must name the already established deadline it observed.

The outer task's cancellation, timeout, worker-shutdown, and reconciliation fences are
authoritative. A strategy-local deadline can be shorter, but it cannot extend the
outer deadline. The effective deadline for a phase is the earliest applicable absolute
deadline. Entering drain or cleanup never resets the outer budget.

When events race, the reducer uses the ordered protocol facts and these precedence
rules:

1. an identity or owner-fence mismatch is stale and cannot mutate the invocation;
2. an outer cancellation or timeout stops admission and prevents later success from
   overriding that outer decision;
3. after submission, a late result is still settled exactly once even
   when cancellation or timeout owns the durable outcome; and
4. inability to prove whether submission or an external effect occurred produces an
   indeterminate outcome rather than a fallback or invented success.

The reducer does not compare wall clocks itself. The adapter emits a deadline event
only after observing the absolute deadline, which keeps replay deterministic and makes
fake-clock testing straightforward.

An action-scoped `DEADLINE_EXPIRED` callback carries the pending action token. Deadline
classification compares the action's stage deadline with `outer`: when the stage
deadline is earlier, its stage-specific timeout remains authoritative even if a
tokenless outer wake is observed later; when `outer` is earlier, the outcome is
`OUTER_DEADLINE`; and a tie also resolves to `OUTER_DEADLINE`. After the winning
deadline is applied, `OUTER_DEADLINE_EXPIRED` cannot replace that outcome and no late
work is dispatched.

## Preparation, fallback, and replay cutoffs

Eligibility rejection happens before preparation and may select the ordinary dynamic
strategy under an automatic policy. Issuing the preparation-start action atomically
and permanently disables automatic strategy fallback *before* the adapter may allocate
an actor, channel, buffer, or other engine resource. A preparation failure must drain
and clean up through the selected strategy. Successful cleanup does not rewind the
session or reopen dynamic fallback.

Issuing the submission-start action creates a second, stricter cutoff: the same
invocation can never be replayed or automatically re-executed. This remains true for
an idempotent application declaration. Idempotency can inform the outer durable retry
policy, but it does not make two executions of one invocation safe.

A submit timeout is ambiguous unless the adapter proves rejection before execution.
Ambiguity is treated as possibly submitted, records `SUBMIT_TIMEOUT` with
`MAY_HAVE_STARTED` and `APPLICATION_POLICY_REQUIRED`, and leads to output accounting,
drain, and health classification. The graph may be reusable if that independent health
check succeeds; otherwise it is quarantined or torn down. Even a proven
rejection-before-execution does not rewind or re-execute that invocation. It may only
inform whether application policy can authorize a *future durable retry* with a new
attempt, run ID, and invocation ID.

## One-shot output ownership

Every declared output slot belongs to exactly one invocation identity and one session
owner. Starting the native get/consume action spends that slot's one-shot retrieval
opportunity. A timeout or adapter error is not a polling result and does not permit a
second get. It forces bounded output accounting followed by drain, quarantine, or
teardown.

A declared slot starts as `DECLARED`. It becomes `NOT_CREATED` only when submission is
proven not to have started. A created output moves through `AVAILABLE`, `CONSUMING`, or
`RELEASE_PENDING` and reaches one truthful terminal ownership state:

- `CONSUMED` once into an ordinary result or stable result-storage reference;
- `ADAPTER_RELEASED` once under a proven native release contract;
- `LOST_WITH_OWNER` when post-submission owner loss makes its fate indeterminate;
- `CLEANUP_UNCONFIRMED` when teardown or cleanup fails before ownership release can be
  confirmed; or
- `UNAVAILABLE_AFTER_TEARDOWN` only after teardown proves it cannot be consumed.

Claiming a slot does not transfer its native handle to another process. The owner that
invoked the graph performs the native consume or release action, then reports the
disposition. Duplicate claims, a second consumption, an undeclared slot, and an event
for another invocation are protocol violations. A graph is reusable only when every
declared slot is terminal *and* the session receives a positive health classification.
`LOST_WITH_OWNER` is terminal for bounded accounting but permanently prevents graph
reuse. `CLEANUP_UNCONFIRMED` is also terminal accounting but never reuse-safe; it does
not pretend that cleanup succeeded. `UNAVAILABLE_AFTER_TEARDOWN` accounts for an output
only after confirmed teardown, when the graph is no longer reusable.

Protocol version 1 defines ownership and bounded slot accounting, not a native
multi-output or zero-copy transport claim. The first adapter may expose only one
ordinary output even though the reducer can retain a bounded declared slot model.
Issue [#116](https://github.com/dariuszpanas/django-ray/issues/116) must prove exact Ray
multi-output, forwarding, and zero-copy behavior before any additional transport is
made eligible. Until then, compiled references never cross the owner boundary or enter
durable state.

## Outcomes, cancellation, drain, and cleanup

The primary lifecycle outcome is a classified protocol value rather than an arbitrary
adapter exception. It can describe validation or preparation before an invocation
exists, or the later invocation path. Version 1 uses these exact values:

| Outcome | Required interpretation |
|---|---|
| `SUCCEEDED` | Every declared output was consumed; graph health and cleanup remain separate facts |
| `VALIDATION_REJECTED` | Validation rejected the strategy without starting preparation |
| `PREPARATION_FAILED` | Preparation failed after fallback had closed |
| `ADMISSION_TIMEOUT` | Capacity was not granted before the absolute admission deadline |
| `CAPACITY_REJECTED` | The bounded owner refused admission without starting submission |
| `SUBMIT_TIMEOUT` | Submission did not produce a conclusive response before its deadline |
| `GET_TIMEOUT` | The one-shot result action did not settle before its deadline; it cannot be tried again |
| `CANCELLED_PRE_SUBMISSION` | Outer cancellation won before submission started and effects are `NOT_STARTED` |
| `CANCELLED_AFTER_SUBMISSION` | Cancellation won after effects became `MAY_HAVE_STARTED` |
| `APPLICATION_ERROR` | Submitted application code reported an ordinary classified failure |
| `ACTOR_DIED` | A participant died and session health cannot remain positive |
| `CHANNEL_ERROR` | Input or output transport failed and ownership/effect state must be settled |
| `OWNER_LOST` | The compiler/invoker process was lost; pre-submit loss remains `NOT_STARTED`/`NOT_CREATED`/`AUTOMATIC_ALLOWED`, while post-submit loss is `MAY_HAVE_STARTED`/`LOST_WITH_OWNER`/`OPERATOR_RECONCILIATION_REQUIRED` |
| `OUTER_DEADLINE` | The outer durable deadline won, including ties with an inner deadline |
| `DRAIN_FAILURE` | The adapter explicitly reported that drain failed before its deadline; teardown is required |
| `DRAIN_TIMEOUT` | Reachable work or output ownership could not settle before the drain deadline |
| `TEARDOWN_FAILURE` | Resource release failed when no earlier primary outcome was already fixed; unresolved outputs become `CLEANUP_UNCONFIRMED` |

The exact version 1 vocabulary is fail-closed: an unknown outcome or event is rejected
rather than coerced.

Effect state is a separate exact fact: `NOT_STARTED` before submission and
`MAY_HAVE_STARTED` when submission starts. A timeout outcome therefore does not need to
pretend whether an external effect completed. Cleanup and teardown diagnostics are also
separate: they append bounded evidence but never replace the already selected primary
outcome.

Graph disposition and retry disposition are independent. Graph disposition is one of
`UNPREPARED`, `PREPARED`, `DRAIN_REQUIRED`, `REUSABLE`, `REBUILD_REQUIRED`, or
`TORN_DOWN`. Retry disposition is one of `AUTOMATIC_ALLOWED`,
`APPLICATION_POLICY_REQUIRED`, `OPERATOR_RECONCILIATION_REQUIRED`, or `PROHIBITED`.
Neither fact rewinds the current invocation or makes the reducer resubmit it.

| Retry disposition | Outer-layer meaning |
|---|---|
| `AUTOMATIC_ALLOWED` | Reducer evidence proves submission did not start; the outer durable retry policy may create a new attempt and invocation |
| `APPLICATION_POLICY_REQUIRED` | Effects may have started; only the declared application idempotency/retry policy can authorize a new durable attempt |
| `OPERATOR_RECONCILIATION_REQUIRED` | Owner-loss or equivalent ambiguity requires external reconciliation before a retry decision |
| `PROHIBITED` | The outcome, such as success or selected cancellation, must not be retried automatically |

Cancellation stops future admission. Before submission it can finish without an
application invocation. After submission it requests the adapter's bounded cooperative
cancellation, but the owner must still settle reachable outputs and drain before
cleanup. Teardown proves only that graph resources were released; it does not prove
that an external Kubernetes write was interrupted or rolled back.

Cleanup has its own bounded diagnostics. A cleanup failure does not rewrite a proven
application result, but it prevents a clean lifecycle classification and poisons the
per-run session against later admission. Any still-unresolved output becomes the
terminal, non-reuse-safe `CLEANUP_UNCONFIRMED` classification. `DRAIN_FAILURE` records
an adapter-reported failure and remains distinct from deadline-driven `DRAIN_TIMEOUT`.
Owner loss before submission is effect-free and may permit a new durable attempt, but
never strategy fallback after preparation. Owner loss after submission may have begun
is indeterminate and non-adoptable. A replacement owner cannot recover the process-local
handle or claim its one-shot output; outer reconciliation may start only a new durable
run after applying the retry disposition.

## Graph reuse is not durable retry

Graph reuse and retry are independent axes:

- **Within-run graph reuse** keeps one prepared session and uses a new invocation ID
  for each serial input under the same durable run identity.
- **Durable retry** increments the attempt and execution generation, creates a new run
  ID, rematerializes the pinned effective plan, and starts a new invocation lifecycle.

Plan fingerprint equality is required for retry pinning and graph compatibility, but
it does not authorize reuse of a previous session, invocation ID, output claim, or
terminal snapshot. The ADR-0002 pilot creates a new graph session for a retry even when
the fingerprint is unchanged. The reducer never resubmits an invocation by itself;
any retry decision belongs to the outer Django task lifecycle.

Within one session, an invocation that poisons graph state prevents later invocations
from being admitted. Reuse requires a positive session-health classification and every
declared output slot in a compatible terminal state: `NOT_CREATED`, `CONSUMED`, or
`ADAPTER_RELEASED`. `LOST_WITH_OWNER`, `CLEANUP_UNCONFIRMED`, and
`UNAVAILABLE_AFTER_TEARDOWN` complete bounded ownership accounting but cannot coexist
with graph reuse. A new invocation on a still-clean graph is reuse, not recovery of the
previous invocation. Metrics must report graph reuse and durable attempts separately.

## Bounded diagnostics and snapshots

Every reducer snapshot is explicitly versioned, JSON-safe, secret-free, count-bounded,
and byte-bounded. Version 1 admits at most 64 declared output slots, eight stable
cleanup diagnostic codes, and 16,384 canonical JSON bytes. It contains only stable
identity summaries, protocol version, exact capacity, session and invocation state,
current action token, fallback and retry disposition, absolute deadlines, output
states, primary outcome, effect state, graph disposition, cleanup diagnostics, and the
next bounded action sequence.

Lifecycle run and invocation IDs are limited to 128 characters, rejection messages to
256 characters, and action sequencing to the positive signed 63-bit range. Invalid or
exhausted values fail closed instead of expanding the snapshot.

Snapshots never contain invocation payloads, result values, credentials, tracebacks,
Ray task or actor handles, `ObjectRef`, `DAGNode`, `CompiledDAG`, `CompiledDAGRef`,
channel objects, exceptions, locks, or owner-process memory. Native log tails and full
errors remain adapter diagnostics under their existing independent bounds.

Cleanup diagnostics use only `CAPACITY_RELEASE_FAILED`, `CAPACITY_RELEASE_TIMEOUT`,
`CANCELLATION_FAILED`, `CANCELLATION_TIMEOUT`, `DRAIN_FAILED`, `DRAIN_TIMEOUT`,
`HEALTH_CHECK_FAILED`, `TEARDOWN_FAILED`, and `TEARDOWN_TIMEOUT`; they contain no
free-form adapter text. When the eight-code budget is exhausted, the last slot becomes
`DIAGNOSTICS_TRUNCATED` rather than growing the snapshot. Rejection text is
independently bounded. The state retains only the pending and last completed action
tokens rather than an event history. Progress reporting may copy a compact summary,
but the lifecycle snapshot is not a recovery log and cannot replace the durable outer
task state.

## Effective-plan identity

The exact lifecycle protocol version participates in effective-plan identity at:

```text
strategy_requirements.compiled_graph.lifecycle_protocol_version = 1
```

An unsupported value fails materialization before submission. Changing reducer
semantics, transition meaning, output ownership, deadline precedence, fallback cutoff,
or snapshot interpretation requires a new lifecycle protocol version and therefore a
different plan fingerprint. It does not necessarily require a new top-level
`plan_format_version` because the version 1 effective-plan schema already fingerprints
strategy-specific Compiled Graph settings.

The lifecycle version is independent of the Compiled Graph compatibility schema and
policy versions. Compatibility answers whether an exact native tuple may be attempted;
the lifecycle protocol answers how an accepted invocation must behave.

## Side-effect evidence boundary

This contract can classify declared effects, close fallback before preparation, and
forbid same-invocation replay after submission. It cannot prove that a Kubernetes
operation is idempotent, that cancellation interrupted an API request, or that a retry
will not duplicate an external mutation.

Issue [#115](https://github.com/dariuszpanas/django-ray/issues/115) owns that evidence:
idempotency keys, compare-and-set or resource-version behavior, partial-write tests,
cancellation windows, reconciliation, and operator guidance for Kubernetes syncs. A
compiled adapter must remain read-only or otherwise ineligible for those effects until
that issue's application-level controls are demonstrated.

Issue #116 independently owns native multi-output and zero-copy evidence. Neither child
issue blocks the Ray-free state machine, but both block claims about those application
and transport capabilities.

## Consequences

### Positive

- Lifecycle races can be replayed and tested without Ray or a live cluster.
- Every future adapter shares one fallback cutoff, identity fence, deadline policy,
  output-ownership rule, and bounded snapshot shape.
- Native beta objects remain isolated from persistence and durable Django semantics.
- Graph reuse can be measured without conflating it with durable retries.
- Kubernetes side-effect and multi-output claims remain explicit evidence gates.

### Costs and limitations

- A native adapter must translate every effect into protocol events and retain enough
  owner state to settle outputs during drain.
- Conservative indeterminate outcomes may require operator reconciliation rather than
  an automatic fallback.
- Absolute deadline plumbing must be supplied by the outer runner and owner adapter.
- The protocol alone provides no performance benefit and promotes no capability row.

## Alternatives considered

### Let the native adapter define lifecycle implicitly

Rejected. It would make durable behavior depend on beta exceptions and call ordering,
prevent Ray-free race tests, and allow owner implementations to disagree.

### Fall back whenever preparation or execution fails

Rejected. Preparation may allocate owner resources, so issuing its action closes
strategy fallback even if cleanup later succeeds. After submission, a second execution
could duplicate an application effect. An ambiguous submit is also unsafe and must be
treated as possibly submitted.

### Treat cleanup success as proof of cancellation

Rejected. Releasing actors and channels does not prove that submitted application code
or an external API mutation did not complete.

### Use the plan fingerprint as invocation identity

Rejected. Many invocations and durable attempts may share one plan. The fingerprint
cannot fence callbacks, output ownership, cancellation, or terminal writes.

### Persist native result references for another worker to consume

Rejected. Compiled results have owner-local, one-shot semantics. The owner must settle
them through the protocol and publish only an ordinary result or stable storage
reference.

## Implementation boundaries

1. Issue [#85](https://github.com/dariuszpanas/django-ray/issues/85) delivers the
   standard-library reducer, versioned plan identity, deterministic tests, and this
   decision record.
2. Issue [#70](https://github.com/dariuszpanas/django-ray/issues/70) introduces the
   strategy-neutral runtime hooks that a future adapter will call.
3. Issue [#71](https://github.com/dariuszpanas/django-ray/issues/71) proves static actor
   isolation before compilation.
4. Issue [#72](https://github.com/dariuszpanas/django-ray/issues/72) may integrate an
   opt-in native adapter only after compatibility, ownership, lifecycle, and benchmark
   gates pass.
5. Issues #115 and #116 supply side-effect and transport evidence without changing the
   Ray-free protocol boundary silently.

## Upstream references

- [Ray Compiled Graph overview](https://docs.ray.io/en/latest/ray-core/compiled-graph/ray-compiled-graph.html)
- [Compiled Graph limitations and troubleshooting](https://docs.ray.io/en/latest/ray-core/compiled-graph/troubleshooting.html)
- [`CompiledDAG.execute()` reference](https://docs.ray.io/en/latest/ray-core/compiled-graph/doc/ray.dag.compiled_dag_node.CompiledDAG.execute.html)
- [Compiled Graph quickstart and teardown](https://docs.ray.io/en/latest/ray-core/compiled-graph/quickstart.html)
