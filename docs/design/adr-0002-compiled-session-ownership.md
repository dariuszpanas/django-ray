# ADR-0002: Compiled Session Ownership and Reuse

- **Status:** Accepted for initial local/direct CPU-pilot evidence
- **Date:** 2026-07-19
- **Decision owners:** django-ray maintainers
- **Related contracts:** [Workflow Plans and Execution Strategies](../workflow-plans.md),
  [ADR-0001](adr-0001-workflow-plan-contract.md)

## Context

ADR-0001 keeps a Django task as the durable boundary and treats Ray Compiled Graph as
one possible strategy for an eligible actor region. It deliberately does not choose
who owns a prepared graph or how long that graph lives.

That distinction is material. Current Ray documentation says that only the process
that compiles a graph may invoke it. The current synchronous `CompiledDAG.execute()`
API is not thread-safe, graph capacity and buffered results are finite, and compiled
results cannot be transferred to another task or actor. Results must be consumed once,
and actors participating in a graph cannot participate in another graph until the
first graph is torn down. A reusable graph is therefore a process-owned session, not a
cluster-wide object reference or database payload.

django-ray has several processes that could appear to be an owner:

- the Django worker that claims and reconciles a durable task;
- the Ray worker process that runs a Ray Core outer task;
- a Ray Job entrypoint driver;
- a Ray Client driver that compiles and invokes the graph itself;
- a cluster-side outer-task worker submitted through Ray Client; or
- a future long-lived process or actor that accepts calls from many durable runs.

They have different lifetimes and failure modes. A scheduled Kubernetes sync also has
two different reuse questions: whether one schedule can invoke a namespace kernel many
times, and whether later schedules can find the same prepared graph. Stable namespace
configuration answers neither process-lifetime question by itself.

## Terms

This decision preserves ADR-0001's vocabulary and fixes the following relationships:

| Term | Ownership meaning |
|---|---|
| **Compiled region or kernel** | A statically shaped actor subset of an effective plan. It is descriptive and contains no Ray runtime handle. |
| **Graph instance** | Actors, channels, buffers, and one process-local compiled handle prepared for an exact graph identity. |
| **Execution session** | The lifetime in which one owner process prepares, admits, invokes, consumes, drains, and tears down graph instances. |
| **Workflow invocation** | One uniquely identified input binding admitted to a graph inside a durable run. |
| **Durable workflow run** | The outer task execution fenced by task primary key, attempt, execution generation, and the run UUID introduced by issue #81. |

A session may contain one graph instance and many invocations. For the first pilot, one
session belongs to exactly one durable run. A future cross-run session would be a new
operational service boundary rather than a longer-lived form of the same task.

## Decision

The initial CPU-pilot evidence supports **repeated, serial invocations inside one Ray
Core durable task submitted by a local or direct non-client driver**. This accepts the
cluster-side owner relationship, not django-ray's production submission transport.
The compatibility policy records submission transport as an exact capability
dimension; a local/direct row can never authorize a Ray Client-submitted owner.
Issue #99 must likewise review the specific container, immutable deployment/image,
shared-memory, and object-store profiles for a row. Missing context and generic
`host`, `container`, or `docker` labels cannot authorize this owner.

The process running the Ray Core outer task is the sole session owner. It prepares one
eligible fixed actor graph, invokes it repeatedly, consumes each result before the next
invocation, and explicitly tears it down before the outer task returns. The first
limits are:

```text
owner processes per durable run = 1
graph instances per session     = 1
concurrent callers per session  = 1
max in-flight invocations       = 1
max buffered results            = 1
owner-side queued invocations   = 0
```

This boundary is intentionally conservative. It satisfies Ray's same-process and
non-thread-safe invocation constraints, gives every beta result one consumer, and ties
all actors and buffers to the existing durable run. It is enough to test whether a
generic namespace kernel repeated within one schedule amortizes compilation.

django-ray's documented production Ray Core mode is different at the submission
boundary: the Django task manager connects through Ray Client (`ray://`) and holds the
outer task reference, while the outer task executes in a cluster-side worker. That
worker could still create, compile, invoke, consume, and tear down every beta handle in
one process. This is not the rejected topology where the Ray Client driver owns the
`CompiledDAG`. Ray documents that a Ray Client workload is terminated when its
connection remains lost beyond the reconnect grace period and that server-held
references are then dropped. The nested owner therefore remains a deferred candidate:
a live cluster must prove disconnect, reference-loss, cancellation, reconciliation,
and worker-shutdown behavior before production use is accepted.

This ADR does **not** enable a Compiled Graph strategy, implement a session service, or
make compilation a public flag. Issues #70, #71, #72, #84, #85, and #86 remain gates.
The ordinary bounded dynamic strategy remains authoritative until those gates and the
benchmark decision pass.

Cross-scheduled-run reuse is deferred. It requires a dedicated, long-lived owner with
leases, routing, admission, resource budgets, drain, and deployment operations that do
not exist in django-ray today. It must be justified separately by Stage B evidence; a
named or detached Ray actor alone is not that complete owner protocol.

## First pilot contract

The accepted owner follows these rules:

1. The outer Ray Core task materializes the effective plan and validates the exact
   compatibility tuple before creating actors or application effects.
2. The same outer-task process creates the dedicated actors, compiles the graph, calls
   `execute()`, and consumes every result.
3. Invocation is serialized. No thread, concurrent actor method, or second durable run
   may call the compiled handle.
4. Each invocation carries the complete durable run identity plus a unique
   `invocation_id`. Participants reject a different run identity.
5. `CompiledDAG`, `CompiledDAGRef`, actor handles, channels, and buffers stay in owner
   memory. Only ordinary serialized results and stable diagnostics cross the outer-task
   boundary.
6. The owner consumes or drains the current result before admitting another input.
7. The owner stops admission and explicitly tears down the graph and its dedicated
   actors on success, error, cancellation, timeout, or process shutdown.
8. Once preparation or invocation may have produced application effects, the same
   invocation never falls back automatically to another strategy.

The pilot is CPU-only, uses dedicated actors, and excludes Django ORM work until the
static actor issue proves connection and state isolation. It is limited to an exact
platform/Ray/Python/transport capability allowed by issue #86.

## Process-topology matrix

| django-ray mode or topology | Would-be owner | Decision status | Reason |
|---|---|---|---|
| Sync worker (`--sync`) | Django worker process | Rejected | No Ray execution boundary exists, and reserving graph actors inside the task-claiming process would mix worker concurrency, database connections, and graph lifetime. |
| Local Ray (`--local`) from a direct driver | Ray Core outer-task worker process | Selected initial evidence boundary | It proves the same-process nested-owner topology without claiming production acceleration. The exact platform still must pass #86. |
| Direct non-client Ray cluster connection | Ray Core outer-task worker process | Same topology; environment unproven | The owner can live for one durable run and return an ordinary result, but the current artifact does not establish a supported production transport. |
| Ray Client driver owns `CompiledDAG` | Django/Ray Client driver process | Rejected | Compilation and invocation would occur outside the cluster-side durable-task worker and bind the beta handle directly to the client lifetime. |
| Ray Client submits nested outer-task owner (`--cluster=ray://...`) | Cluster-side outer-task worker process | Deferred production candidate | Compilation and invocation could remain entirely in one remote worker, but losing the client connection beyond its reconnect grace period can terminate the workload and drop its outer reference. A dedicated live-cluster probe must establish disconnect, reference-lifetime, cancellation, reconciliation, and shutdown behavior. |
| Ray Job | Per-task entrypoint driver | Deferred candidate | The driver can own repeated calls within one job, but every durable task starts a new driver. It gives no cross-schedule reuse and its startup cost can dominate a short CPU kernel. |
| Dedicated cross-run owner | New resident owner process or proven actor topology | Deferred design | It could outlive scheduled tasks, but needs a new service protocol, capacity accounting, fencing, deployment drain, and independent operational ownership. |

The repository prototype proves only the local/direct-driver nested Ray Core topology.
Its inner process relationship matches the proposed production shape, but it does not
prove that a Ray Client submitter can disconnect, lose its outer reference, cancel, or
shut down without violating durable execution and teardown. Ray Client-submitted, Ray
Job, and cross-run ownership each need separate evidence.

## Kubernetes synchronization boundary

The first candidate shape is:

```text
one scheduled durable task
  -> discover or load configured namespaces
  -> prepare one fixed-width namespace kernel
  -> invoke the same kernel once per namespace or bounded namespace batch
  -> aggregate a bounded summary
  -> teardown
```

Namespace names, current resource inventory, resource versions, request context, and
credentials are invocation data. Adding or removing live Kubernetes resources does not
change the fixed physical width. Changing configured actor stages, replica counts,
code, environments, resources, transport, or compilation settings does change graph
identity.

This design can compile once per namespace loop and measure warm invocations within a
schedule. It does **not** reuse a graph across schedules. Every new scheduled durable
task creates a new outer Ray task, compiles a new graph instance, and tears it down. Its
cross-schedule graph-cache hit rate is therefore zero by definition. Results may be
described as within-run acceleration only; a one-shot schedule that invokes the graph
once is ineligible for an amortized-reuse claim.

A future cross-schedule owner would keep actors resident while no durable task is
running. That changes capacity, deployment, security, and failure semantics and must
not be inferred from namespaces being operationally stable.

## Graph identity, routing, and invalidation

Issue #84 implements the canonical effective-plan fingerprint. A session route must
use that fingerprint rather than reconstructing a weaker cache key. The versioned
graph identity covers, directly or through the effective plan:

- plan schema and canonical plan fingerprint;
- application definition and code revision;
- every resolved outer and per-stage RuntimeEnv content hash;
- Ray version and reviewed compatibility identity;
- operating system, architecture, and relevant accelerator/platform capabilities;
- logical compiled-region topology and physical actor/replica/resource layout;
- scheduling and placement requirements;
- transport, channel, serialization, buffer, and retained-result settings;
- compilation and lifecycle-adapter versions; and
- a canonical topology/configuration digest.

The resulting stable identifier is conceptually:

```text
graph_key = sha256("django-ray-compiled-session-v1\0" + canonical_identity_bytes)
```

The input bytes are secret-free. Credentials and raw secret values are never hashed or
persisted; a reusable plan that depends on changing secret material must provide a
non-secret revision identity or fail eligibility.

For the per-run pilot, routing is in-process and nothing looks up a previous owner. A
future cross-run protocol may persist only an opaque tuple such as
`(graph_key, owner_route, owner_epoch)`. The owner epoch is a fencing token. It prevents
an expired owner from publishing admission, result, or cleanup state after a
replacement acquires the route. Database rows may retain the plan fingerprint,
selected strategy, stable session identifier, and bounded timings; they never retain
Ray handles or compiled references.

A shared owner would acquire one expiring, heartbeat-backed lease for the graph key in
a transactional store. The lease key is unique and every successful replacement
increments `owner_epoch`; only that epoch may register a route or publish outcomes.
Lease conflict routes to the current healthy owner or returns bounded backpressure. It
never starts a duplicate instance optimistically. A Ray actor name or namespace may be
part of `owner_route`, but Ray naming is discovery rather than the fencing authority.

Any identity mismatch rejects reuse and creates a new instance only before execution.
Code or RuntimeEnv rollout, Ray upgrade, platform/transport change, actor-layout change,
configuration revision, owner epoch replacement, or failed compatibility probe makes
the old instance draining or invalid. An in-flight invocation stays attributed to its
original run and is never rerouted silently.

## Invocation identity and isolation

Every request envelope contains:

```text
(task_execution_pk, attempt_number, execution_generation, run_id, invocation_id)
```

The first four fields are the issue #81 `WorkflowRunIdentity`; `invocation_id` is unique
within the run. Admission, callbacks, logs, progress, result publication, cancellation,
discard, and cleanup carry all five fields. A stale run or invocation is rejected and
its late result is consumed and discarded without mutating the current durable task.

Actors are dedicated to the graph instance. Before each call they receive a complete
envelope rather than relying on process globals left by the previous call. A later
static-actor implementation must prove reset or isolation for task context, logging
context, inputs, outputs, mutable caches, credentials, database connections, and
application state. The CPU pilot rejects callables whose isolation cannot be shown.

## Owner request protocol

The per-run pilot uses in-process calls, but it preserves the request shape a future
owner would need. A request contains a protocol version, graph key, owner epoch, full
run and invocation identity, Django priority, admission deadline, and a bounded input
envelope or stable input reference. A response is one of:

- admitted with a stable invocation token;
- rejected before submission with a capacity, identity, compatibility, draining, or
  stale-owner reason;
- completed with one ordinary serialized result or stable result reference;
- failed with a classified application or graph error; or
- draining/indeterminate when clean interruption cannot be proven.

The owner API also needs bounded cancel, status, drain, and evict operations scoped by
graph key, owner epoch, and invocation identity. Status is observational: durable task
state remains in Django. Raw credentials, Ray handles, compiled references, object
references, and caller process state never appear in the routing record or response.

## Admission and resource budgets

`django_ray_worker --concurrency` bounds claimed outer rows, not graph actors, channel
buffers, or resident capacity. Every implementation must account for both layers:

```text
simultaneous sessions * (
    outer-owner resources
    + sum(actor replica resources)
    + declared channel and result-buffer bytes
) <= compiled-session deployment budget
```

The first pilot uses a zero-CPU coordination owner where feasible, fixed actor resource
requests from the effective plan, one in-flight call, one buffered result, and no hidden
owner queue. A caller that cannot be admitted waits only for a configured bounded
deadline and then receives a capacity outcome. It does not enqueue unbounded inputs.

Before product implementation, deployment configuration must add a session cap that is
independent of worker row concurrency. A Ray custom-resource token or an equivalent
cluster-scoped admission lease can enforce it. Preparation must prove that the owner
and every actor can be placed together; otherwise an outer task could retain resources
while waiting forever for its children. Autoscaling is not an admission guarantee.

For any later shared owner, the protocol must additionally define:

- maximum resident instances and total actor CPUs, GPUs, memory, and custom resources;
- maximum instances for one graph key and duplicate-owner prevention;
- maximum in-flight calls and buffered results per instance and globally;
- a finite priority queue with stable FIFO ordering within equal Django priorities;
- caller backpressure and an admission deadline;
- maximum idle TTL, eviction batch, cache bytes, and cleanup concurrency; and
- fairness so a hot low-priority graph cannot occupy all resident capacity.

## Failure, cancellation, and drain

Issue #85 owns the detailed Compiled Graph invocation state machine. This ownership
decision supplies these invariants:

- **Cancellation before admission:** reject without creating effects.
- **Cancellation after submission:** stop new admission, preserve the original
  invocation identity, and drain or quarantine according to #85. Teardown is not proof
  that an external Kubernetes side effect was interrupted.
- **Partial graph or actor failure:** poison the per-run session, reject later inputs,
  consume or discard all reachable outputs, and tear down. Do not fall back.
- **Owner loss:** all process-local graph handles are lost. The current run cannot be
  adopted by another owner; reconciliation applies the existing durable task outcome
  and a retry creates a new run UUID and graph instance.
- **Cluster restart or node loss:** invalidate the session. A retry rematerializes and
  verifies the pinned plan identity before preparing new resources.
- **Autoscaling:** active actor resources pin their placement. Scale-down benefit starts
  only after deterministic teardown; a moved or restarted actor invalidates the graph.
- **Orphan cleanup:** non-detached per-run actors fate-share with their owner where Ray
  can enforce it. Reconciliation still detects leaked named resources and records
  bounded cleanup failures without accepting stale callbacks.
- **Process shutdown:** stop admission, wait only for the configured drain deadline,
  consume/discard results as the lifecycle contract permits, explicitly tear down, and
  then release the outer task.

A rolling deployment first prevents old owners from admitting work. Accepted
invocations finish or reach the drain deadline, all results are consumed or discarded,
the graph is torn down, actors are released, and the old owner route is retired. Only
then may a new compatibility identity or owner epoch accept calls. A force-killed owner
cannot claim clean drain; its in-flight invocation remains failed or indeterminate
according to #85.

## Observability and benchmark interpretation

Bounded observability must distinguish session reuse from other warm caches. At minimum
later implementation exposes:

- compilation count and duration;
- within-run graph reuse count and invocation latency;
- cross-run cache hit and miss counts, even when both are respectively zero and one;
- resident instance and actor/resource gauges;
- in-flight, buffered-result, queue-depth, admission-wait, and capacity-rejection data;
- idle duration, TTL eviction, invalidation, drain, teardown, and orphan-cleanup data;
- owner loss and stale-invocation rejection; and
- the explicit reuse boundary (`within_run` or a future `cross_run`).

The metric families cover compilation outcomes and duration, cache lookup outcomes,
evictions by bounded reason, resident instances and reserved resources, in-flight and
buffered calls, queue depth and admission wait, capacity rejection, idle time, drain,
and teardown. Plan fingerprints, graph keys, run IDs, invocation IDs, namespace names,
and owner routes must not become metric labels; those high-cardinality identities
belong in bounded durable metadata and correlated logs.

Issue #69 must report cold compilation, warm invocation, cold and warm schedules,
cross-schedule hit rate, actor idle cost, and the selected reuse boundary separately.
RuntimeEnv or worker cache warmth does not make a per-run graph a cross-schedule hit.

## Raw-Ray topology prototype

`scripts/compiled_session_topology_probe.py` is a deliberately small, opt-in raw-Ray
artifact. A driver submits one outer task with retries disabled. That outer task creates
two dedicated actors, compiles their graph once, executes it serially several times,
consumes every result, tears down the graph, and returns only an ordinary JSON-safe
report. Each stage fences a synthetic issue #81 run identity and reports its process ID,
so the probe verifies one compiler/invoker process and reused actor processes.

The probe is not a django-ray strategy, benchmark, compatibility allowlist, Ray Job
test, or resident service. It must run only after issue #86 allows the exact native
capability. Native execution requires an explicit environment opt-in and has a bounded
outer-task timeout:

```powershell
$env:DJANGO_RAY_RUN_COMPILED_SESSION_TOPOLOGY_PROBE = "1"
uv run python scripts/compiled_session_topology_probe.py `
  --address local `
  --invocations 3
```

The artifact permits a direct non-client address for the same topology. It rejects a
`ray://` address or an already-active Ray Client session so its result cannot be
misread as production-transport evidence. That rejection does not reject the proposed
Ray Client-submitted nested owner; a dedicated live-cluster probe must test that
variant and produce its own exact capability row. A successful local probe demonstrates
the owner relationship only; benchmark and compatibility issues still decide whether
the topology is safe and useful.

## Consequences

### Positive

- The pilot matches current Ray process and thread-safety constraints without a new
  service or persistence protocol.
- One schedule can measure repeated namespace-kernel invocations while keeping
  durability, cancellation, and stale writes inside one run.
- Every beta handle and one-shot result has one in-process owner.
- Cross-schedule acceleration cannot be claimed accidentally from a per-run benchmark.
- A later resident owner has explicit identity, admission, fencing, and drain gates.

### Costs and limitations

- Every scheduled run compiles and reserves actors again.
- Serial invocation leaves pipelining and higher in-flight capacity for later evidence.
- Ray Job and the production Ray Client-submitted topology remain deferred; the latter
  requires a dedicated live-cluster lifetime probe.
- A short or I/O-bound Kubernetes sync may not amortize compile and actor reservation.
- Outer-task loss discards the session; retry prepares a new instance.

## Alternatives considered

### Reuse a graph from the Django worker

Rejected. The worker is not the process that executes the Ray Core outer task, worker
concurrency does not account for graph actors, and compiling in the Django/Ray Client
driver would bind the graph to that process. The deferred production candidate instead
submits a cluster-side outer task that owns all beta handles.

### Compile once in every scheduled Ray Job

Deferred rather than selected. It can support repeated calls inside one job but does
not survive the job and adds driver startup to every schedule.

### Store `CompiledDAG` or `CompiledDAGRef` in the database

Rejected. They are process-local beta objects with one-consumer result semantics. Only
canonical identity and opaque stable routing metadata may be durable.

### Use a named detached actor as the cross-run cache

Rejected as an incomplete design. Named/detached actors can outlive creators, but that
does not prove that the actor can safely own and invoke this Compiled Graph topology or
provide leases, duplicate-owner fencing, bounded queues, eviction, rolling drain, and
security isolation.

### Start with concurrent or async invocations

Rejected for the first CPU pilot. Serial execute/get makes capacity and result ownership
deterministic. Async admission can be evaluated after #85 defines the lifecycle and
benchmarks show that one in-flight call is the limiting factor.

## Implementation boundaries

This decision changes documentation and adds only the opt-in topology probe. Product
implementation remains split across the existing roadmap:

1. [Issue #84](https://github.com/dariuszpanas/django-ray/issues/84) provides the
   effective-plan snapshot and graph identity.
2. [Issue #70](https://github.com/dariuszpanas/django-ray/issues/70) provides
   strategy-neutral lifecycle hooks.
3. [Issue #71](https://github.com/dariuszpanas/django-ray/issues/71) proves dedicated
   static actors and cross-invocation isolation without compiling.
4. [Issue #85](https://github.com/dariuszpanas/django-ray/issues/85) defines compiled
   input/result/cancellation/drain semantics.
5. [Issue #86](https://github.com/dariuszpanas/django-ray/issues/86) supplies the exact
   compatibility allowlist and subprocess isolation.
6. Issues [#69](https://github.com/dariuszpanas/django-ray/issues/69),
   [#87](https://github.com/dariuszpanas/django-ray/issues/87), and
   [#88](https://github.com/dariuszpanas/django-ray/issues/88) supply feasibility
   evidence and the adoption decision.
7. [Issue #72](https://github.com/dariuszpanas/django-ray/issues/72) integrates an
   opt-in strategy only after those gates pass.

Focused implementation child issues should separate the per-run owner controller,
cluster-scoped admission budget, session observability, a live-cluster Ray
Client-submitted nested-owner probe, Ray Job topology validation, and any later
cross-schedule owner spike. This ADR does not file or implement them.

## Upstream references

- [Ray Compiled Graph overview](https://docs.ray.io/en/latest/ray-core/compiled-graph/ray-compiled-graph.html)
- [Compiled Graph limitations](https://docs.ray.io/en/latest/ray-core/compiled-graph/troubleshooting.html)
- [`CompiledDAG.execute()` thread-safety note](https://docs.ray.io/en/latest/ray-core/compiled-graph/doc/ray.dag.compiled_dag_node.CompiledDAG.execute.html)
- [Compiled Graph quickstart and teardown](https://docs.ray.io/en/latest/ray-core/compiled-graph/quickstart.html)
- [Ray actor fault tolerance](https://docs.ray.io/en/latest/ray-core/fault_tolerance/actors.html)
- [Ray actor termination](https://docs.ray.io/en/latest/ray-core/actors/terminating-actors.html)
- [Ray Client lifetime and disconnect behavior](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/ray-client.html)
- [Ray Jobs lifetime](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/index.html)
