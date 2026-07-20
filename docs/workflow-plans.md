# Workflow Plans and Execution Strategies

This document defines the architecture contract between the public workflow builders,
durable Django task execution, and current or future Ray execution engines. It is a
design contract: the effective-plan materializer and strategy interface described here
are not implemented yet.

The governing decision is [ADR-0001](design/adr-0001-workflow-plan-contract.md).

## Stable invariants

- One workflow run remains inside one durable `RayTaskExecution` lifecycle. Internal
  workflow nodes do not become independent Django tasks or durable retry records.
- `WorkflowSignature` is a reusable definition builder, not a persisted execution plan.
- An effective execution plan is versioned, immutable, canonical, secret-free, and
  independent of any Ray beta object.
- Invocation values are bound after plan materialization. Changing Kubernetes
  inventory, request identifiers, credentials, or other runtime data does not by
  itself change topology identity.
- Logical work cardinality and physical worker cardinality are separate facts.
- Compiled Graph is an execution strategy for an eligible plan, not a Django task type
  or a promise that arbitrary workflow definitions can be compiled.
- Strategy validation and selection complete before remote side effects begin. A
  strategy may fall back only before submission; post-start fallback could duplicate
  effects and is prohibited.

## Vocabulary

| Term | Contract meaning |
|---|---|
| **Django task** | A callable enqueued through Django Tasks. django-ray stores one `RayTaskExecution` for its attempt chain and owns its queue, timeout, retry, cancellation, result, and terminal state. |
| **Workflow definition** | A reusable, lazy expression describing intended work. Today this is a tree of `WorkflowSignature` objects built with `step`, `chain`, `group`, and `map_step`. It can still contain mutable Python values and is not a durable IR. |
| **Plan template** | A validated, strategy-neutral description derived from a workflow definition before deployment-specific identities and invocation values are resolved. It names input slots instead of embedding their values. |
| **Effective execution plan** | A fully resolved, versioned, deeply immutable, canonical, secret-free IR produced before remote submission. It includes logical topology, physical requirements, resolved environment identities, bounds, lifecycle capabilities, and compatibility inputs. |
| **Durable workflow run** | One attempt-scoped execution of the outer Django task, identified by task primary key, attempt number, execution generation, and a run identifier. It is the recovery and durability boundary. |
| **Workflow invocation** | One binding of invocation inputs to an effective plan within a durable run. A run normally has one invocation today; a future owner may invoke the same fixed plan repeatedly. |
| **Logical work item** | One domain item, such as a Kubernetes resource. Work-item count may change without changing the number of plan nodes or actor replicas. |
| **Logical node** | A plan operation such as call, fixed branch, dynamic map, partition, or collect. Its stable plan node ID is not a Ray task or actor ID. |
| **Dependency** | A directed data or control edge between logical nodes or ports. Runtime object references are strategy-owned handles, not plan fields. |
| **Physical actor stage** | A fixed role implemented by one or more Ray actors. A stage may process many logical work items over time. |
| **Replica** | One physical actor instance assigned to a stage. Replica count is part of physical topology; it is not logical inventory count. |
| **Compiled region or kernel** | A statically shaped subset of an effective plan that a compiled strategy may prepare and invoke repeatedly. The plan marks the region without storing `CompiledDAG` objects. |
| **Graph instance** | Runtime resources prepared for one plan fingerprint and strategy compatibility key, such as actors, channels, and buffers. It has a finite lifetime and must be drained and cleaned up. |
| **Execution session or owner** | The process or service that prepares graph instances, admits invocations, owns their handles, drains results, and tears resources down. Ownership and cross-run reuse are separate design decisions. |
| **Execution strategy** | An engine that validates, prepares, admits, executes, consumes, cancels, drains, and cleans up a plan. Examples are local execution, dynamic Ray tasks, static actors, and a future Compiled Graph adapter. |
| **Progress revision** | A monotonic observability revision scoped to one durable workflow run. It is not a plan revision, code revision, attempt number, or invocation identifier. |

These terms deliberately avoid using *workflow* as a synonym for every task. A Django
task may contain no workflow, one workflow invocation, or eventually several repeated
invocations of one effective plan.

## Workload classification

The execution decision uses multiple axes rather than one broad task label:

| Workload shape | Logical cardinality | Physical topology | Invocation pattern | Baseline strategy | Static or compiled eligibility |
|---|---|---|---|---|---|
| Single callable | One item or one batch | One Ray task | Once | Dynamic Ray task | Not useful without repeated actor work |
| Static `chain` or `group` | Fixed plan nodes | Current implementation creates Ray tasks | Usually once | Dynamic Ray tasks | Potentially transformable only after actor, environment, owner, and lifecycle requirements validate |
| Data-dependent `map_step` | Determined after an input resolves | One Ray task per runtime item today | Once | Bounded dynamic Ray tasks | The expansion itself is dynamic and cannot be claimed as compiled static topology |
| Fixed-width worker pool | Variable item inventory | Fixed stages and replica counts | One or repeated batches | Static actors | Candidate when partitioning, bounds, callable kinds, and ownership validate |
| Repeated static actor kernel | Fixed nodes, ports, stages, and replicas | Preallocated actors and channels | Many compatible invocations | Static actor or compiled strategy | Primary Compiled Graph candidate when the platform and lifecycle adapter are supported |
| Long-lived online service | Requests are unbounded over service lifetime | Replicas scale or reconcile independently | Continuous | Ray Serve or an application service | Outside the finite `RayTaskExecution` lifecycle |

Every plan also records these independent dimensions:

- topology class: `static`, `dynamic`, or `fixed_width`;
- expected invocation cardinality: `once`, a declared bound, or `repeated`;
- logical work-item cardinality and its bound, if known;
- task versus actor node model and synchronous versus asynchronous callable kind;
- physical stage and replica count;
- fan-out, admission, in-flight, queue, result-buffer, and retained-reference bounds;
- resources, placement, RuntimeEnv identity, transport, side effects, failure policy,
  owner lifetime, and durability boundary.

### Kubernetes synchronization example

`discover namespaces -> map(sync namespace) -> summarize` is a dynamic workflow when
the map creates one node per discovered namespace or resource. Namespaces being stable
in practice does not make this topology static: the node count is still derived from
invocation data.

A different plan can use a fixed number of actor replicas and pass the current
namespace/resource inventory as invocation data. Each replica consumes a partition or
bounded stream. That is a `fixed_width` physical topology even though the logical item
count changes. It may become eligible for static actors or a compiled kernel if all
other capability checks pass. The inventory, resource bodies, resource versions, API
tokens, and credentials remain invocation data and are excluded from the plan
fingerprint.

This distinction prevents two unsafe claims:

1. stable production inventory is not proof of static graph topology; and
2. changing inventory does not force topology invalidation when fixed-width nodes are
   intentionally designed to accept it as data.

## Contract layers

### Definition and plan template

The public builders remain convenient Python objects. A materializer traverses a
definition and produces a plan template with stable operator IDs, input slots, callable
references, and declared capabilities. Definition-time values are not automatically
trusted as plan constants.

Current `Step.bound_args`, `Step.bound_kwargs`, and `WorkflowSignature.run()` arguments
are lifted into named invocation slots by the compatibility adapter. A future explicit
plan-constant API may embed a small canonical non-secret literal when changing that
literal intentionally changes plan identity. The materializer must not guess that an
arbitrary bound Python value is safe to persist or fingerprint.

### Effective execution plan

Before the first remote submission, the materializer resolves the template against the
deployment and produces an effective plan. At minimum it resolves:

- callable import paths, callable kinds, and an application definition/code revision;
- the outer RuntimeEnv identity and every per-step RuntimeEnv profile to canonical,
  secret-free specifications and content identities;
- Ray scheduling resources and placement requirements;
- physical actor stages and replica counts when present;
- transport, buffer, result-retention, admission, retry, cancellation, and owner
  requirements;
- Ray compatibility identity, operating platform capabilities, and strategy-specific
  settings that affect preparation or reuse.

Materialization deep-copies and normalizes every field. Mutating the source signature,
settings dictionaries, or profile definitions afterward cannot change the effective
plan.

### Invocation envelope

An invocation envelope binds values to the effective plan. It carries durable run and
invocation identity, input values or durable references, credentials supplied through
the approved runtime channel, deadlines, and idempotency context. The envelope is not
part of the plan fingerprint.

The input-slot schema, ordering, and binding rules *are* plan fields because changing
them changes how values are interpreted. The values bound to those slots are not.

## Version 1 effective-plan IR

The example below illustrates the normative field groups. It is not a public Python API
or a database schema:

```json
{
  "plan_format": "django-ray.workflow-plan",
  "plan_format_version": 1,
  "definition": {
    "name": "myapp.sync.fixed-width",
    "revision": "package:myapp@sha256:0123456789abcdef"
  },
  "topology": {
    "class": "fixed_width",
    "entry_ports": ["inventory", "sync-key"],
    "result_ports": ["summary"]
  },
  "nodes": [
    {
      "id": "partition",
      "operation": "call",
      "node_model": "actor",
      "callable": {
        "import_path": "myapp.workflows.partition_inventory",
        "kind": "sync"
      },
      "inputs": [{"port": "inventory", "source": "invocation:inventory"}],
      "outputs": ["partitions"],
      "stage": "partitioner"
    },
    {
      "id": "sync",
      "operation": "fixed_pool",
      "node_model": "actor",
      "callable": {
        "import_path": "myapp.workflows.sync_partition",
        "kind": "sync"
      },
      "inputs": [{"port": "items", "source": "node:partition:partitions"}],
      "outputs": ["results"],
      "stage": "sync-workers"
    }
  ],
  "edges": [
    {"source": "partition:partitions", "target": "sync:items", "transport": "object"}
  ],
  "physical_topology": {
    "stages": [
      {"id": "partitioner", "replicas": 1},
      {"id": "sync-workers", "replicas": 8}
    ]
  },
  "capabilities": {
    "invocations": {"cardinality": "repeated", "expected_count": 100},
    "logical_items": {"cardinality": "input_bounded", "maximum": 10000},
    "admission": {
      "maximum_in_flight": 8,
      "maximum_queued": 32,
      "maximum_buffered_results": 8
    },
    "effects": {"mode": "external_idempotent", "idempotency_slot": "sync-key"},
    "durability": {"boundary": "outer_task", "per_node_recovery": false},
    "owner": {"lifetime": "durable_run", "sharing": "isolated"}
  },
  "environments": {
    "outer": {"digest": "sha256:...", "profile": "worker"},
    "by_node": {"partition": "sha256:...", "sync": "sha256:..."}
  },
  "strategy_requirements": {
    "compiled": {
      "maximum_in_flight": 8,
      "transport": "auto",
      "buffer_bytes": 1048576
    }
  },
  "compatibility": {
    "django_ray_plan_api": 1,
    "ray": "2.56",
    "platform": "linux-x86_64",
    "capability_set": "ray-cgraph-2.56-linux-v1"
  }
}
```

The materialized representation must contain JSON values only. It must never contain a
callable object, `ObjectRef`, actor handle, `DAGNode`, `CompiledDAG`, `CompiledDAGRef`,
open client, file descriptor, coroutine, lock, or process-local identity.

### Required field groups

| Group | Required meaning |
|---|---|
| Format | Plan format name and exact supported format version. |
| Definition | Stable application name plus conservative code/definition revision. |
| Logical topology | Topology class, ordered nodes, ports, edges, input binding schema, callable path and kind, and any explicit plan constants. |
| Physical topology | Task/actor model, stages, replicas, actor roles, and placement relationships. Empty for plans that have no fixed physical layout. |
| Capabilities | Cardinality, bounds, resources, RuntimeEnv behavior, transport, effects, failure, lifecycle, owner, durability, and result retention. |
| Strategy requirements | Strategy-specific preparation settings expressed as stable values, without an engine-owned runtime object. |
| Compatibility | django-ray plan API, Ray compatibility range or version, platform/capability identity, and any required optional dependency identities. |

## Capability model

Capabilities are declarative constraints used for validation and selection. They do not
prove that application code is safe, deterministic, or idempotent.

| Capability | Minimum fields and semantics |
|---|---|
| Topology | `static`, `dynamic`, or `fixed_width`; identifies whether runtime data creates plan nodes. |
| Invocation | One, bounded, or repeated cardinality; expected count is a performance hint, while declared maxima are enforced bounds. |
| Logical items | Fixed, input-bounded, or unbounded/unknown cardinality, independently of physical stages. |
| Node model | Ray task or actor, sync or async callable kind, actor method/concurrency requirements, and Django bootstrap requirement. |
| Fan-out and admission | Static branch count or dynamic expansion, maximum pending/in-flight/queued work, maximum buffered results, and overflow behavior. Missing bounds mean ineligible for resident or compiled execution. |
| Resources and placement | Normalized CPU, GPU, custom resources, memory, accelerator, scheduling strategy, placement group/bundle relationship, stage, and replica count. |
| RuntimeEnv | Inherit or override mode plus resolved canonical digest for the outer run and each node/stage. A profile name alone is not identity. |
| Payload and transport | Input/output schemas, maximum byte sizes, transport/channel requirements, serialization, buffer size, copy/zero-copy policy, and whether references may be retained or forwarded. |
| Results | Result cardinality, ordering, maximum buffered/retained results, ownership, one-time-consumption requirements, and explicit discard/drain behavior. |
| Side effects | `none`, `read_only`, `idempotent`, `external_idempotent`, or `unknown`; idempotency-key slot and checkpoint contract where applicable. Unknown is the safe default. |
| Failure and retry | Outer-run retry policy, safe leaf retry declarations, application versus system failure handling, partial-result policy, and whether state must be quarantined. |
| Cancellation | Cooperative/forceful support, cancellation deadline, drain requirements, and the point after which fallback is forbidden. |
| Lifecycle and owner | Owner kind, sharing/isolation, per-invocation/per-run/resident lifetime, idle TTL, teardown, rolling-deployment drain, and cache budget. |
| Durability | Outer Django task remains the default boundary; per-node recovery/checkpoint support must be explicit and is currently false. |

Strategy implementations may add versioned rejection rules, but they must not reinterpret
the declared semantics. For example, an engine cannot silently treat unknown side
effects as idempotent or an unbounded map as fixed width.

## Plan fields versus invocation fields

| Effective-plan and fingerprint inputs | Per-invocation or observed data, excluded from the fingerprint |
|---|---|
| Plan format/version and definition name/revision | Durable task PK, Django task ID, attempt, generation, run ID, invocation ID |
| Logical node IDs, operations, ports, edges, and input-slot schema | Values bound to input slots and ordinary task arguments |
| Callable import path and sync/async kind | Current namespace names, Kubernetes objects, resource versions, or discovery results |
| Explicit, canonical, non-secret plan constants | Request IDs, timestamps, deadlines, tracing IDs, and idempotency-key values |
| Topology class, fan-out/admission bounds, stages, and replicas | Actor/task IDs, object references, graph handles, channel handles, and progress revisions |
| Normalized resources, placement, actor layout, and compile settings | Result values, progress events, logs, timing, cache hit/miss, and selected strategy outcome |
| Resolved secret-free RuntimeEnv identity and code/package/image identity | Credentials, tokens, passwords, certificates, kubeconfigs, and secret material |
| Transport, buffers, result ownership/retention, effects, retry/cancel, owner, and durability policy | Mutable external state read or changed by an invocation |
| Ray/platform capability identity when execution compatibility depends on it | Hostnames or node IDs unless an explicit placement identity is semantically required |

The selected strategy, policy, plan fingerprint, and rejection diagnostics are durable
run metadata, not node progress. They must remain available when node reporting is
disabled, but the selection result is not folded back into the strategy-neutral
semantic topology.

## Canonical serialization and fingerprint

The version 1 fingerprint is computed from the effective plan's normative fields:

1. Validate against the exact `plan_format_version` schema and reject unknown
   normative fields.
2. Normalize import paths, enum values, resource keys, platform identifiers, input
   slots, and user-visible identifiers. Identifiers are Unicode NFC.
3. Preserve order where order has semantics, such as chain nodes, argument slots,
   result ports, and replica ranks. Sort schema-declared sets before serialization.
4. Reject non-finite numbers, ambiguous numeric forms, duplicate object keys,
   unserializable values, and values normalized through `default=str`.
5. Serialize the normalized model as canonical UTF-8 JSON with lexicographically
   ordered object keys and no insignificant whitespace. Numeric normalization must be
   deterministic across supported Python versions.
6. Compute SHA-256 over the ASCII domain separator
   `django-ray.workflow-plan-v1\0` followed by the canonical bytes.
7. Render the identity as `sha256:<lowercase hexadecimal digest>`.

Two definitions that materialize to the same normative JSON must produce byte-for-byte
equal canonical serialization and the same fingerprint. Non-semantic annotations may
be stored beside the canonical plan, never inside the hashed payload.

### Invalidation inputs

A graph instance or prepared strategy state must be rejected and drained when any
fingerprinted field changes. Important examples are:

- plan format, topology, ports, dependencies, callable path or kind;
- conservative application code/definition revision;
- explicit plan constant or binding schema;
- normalized resource, placement, stage, replica, actor, or concurrency layout;
- resolved outer or per-node RuntimeEnv content identity;
- transport, channel, serialization, buffer, retained-result, or compilation setting;
- side-effect, retry, cancellation, durability, owner, lifetime, or admission contract;
- a Ray, django-ray, optional dependency, operating-system, architecture, accelerator,
  or strategy capability change that the compatibility policy says is relevant.

Changing only invocation values, run identity, timestamps, observations, or progress
does not invalidate the plan. A compatibility policy may deliberately declare a range
of Ray patch versions equivalent, but that decision must be explicit, versioned, and
tested; silently dropping Ray or platform identity is not acceptable.

### Code and deployment identity

Callable paths do not prove which code will run. Version 1 therefore requires a
conservative definition revision suitable for the deployment form:

| Deployment form | Acceptable identity | Reusable-strategy rejection |
|---|---|---|
| Installed wheel or package | Distribution name/version plus immutable artifact digest or build revision | Version alone when the same version can be rebuilt with different bytes |
| RuntimeEnv archive | Content-addressed URI and verified archive digest | Mutable branch, latest, or unsigned URI without a content identity |
| Container image | Registry manifest digest plus application build revision | Mutable image tag by itself |
| Development working directory | Deterministic content digest after documented excludes, together with dirty-state identity | Process path, modification time, or an unverified/dirty tree with no content snapshot |

The conservative revision may invalidate more often than perfect source-equivalence
analysis would require. That is preferable to routing an invocation into actors that
may contain incompatible code. Dynamic execution may remain available when a stable
reusable identity is unavailable, but its rejection diagnostic must explain why static
or compiled reuse is disabled.

### Secret handling

Raw secrets are never plan fields and are never hashed. Hashing a low-entropy password,
token, or namespace credential is not safe redaction.

When a secret changes process, actor, environment, or graph compatibility, the plan may
contain only a stable provider reference and non-secret version/revision supplied by an
approved secret system. The material value is injected after identity validation over a
trusted runtime channel. If no non-secret compatibility identity exists, the plan is
ineligible for reusable actors or compilation and receives a structured rejection.
Existing RuntimeEnv dictionaries that embed secret values cannot be persisted in an
effective plan merely because another surface redacts their display.

## Format compatibility and snapshot boundary

`plan_format_version` is an integer with fail-closed semantics:

- an executor must support the exact version before submission;
- unknown normative fields are rejected rather than ignored;
- changing fingerprint meaning, defaults, field interpretation, or required fields
  requires a new format version;
- non-semantic annotations may evolve outside the canonical payload;
- no executor may silently downgrade a plan or rewrite it after fingerprinting.

The effective plan is materialized and snapshotted before the first remote submission
of a durable workflow run. The bounded, redacted manifest, canonical fingerprint,
definition revision, and selection diagnostics must be persisted outside optional node
progress. The storage model is left to the implementation issue.

The first successful materialization pins the plan identity for the
`RayTaskExecution` attempt chain. A retry creates a new durable run identity and may
rebuild the definition in a new process, but its materialized fingerprint must match
the pinned identity before remote submission. A mismatch fails closed with an
actionable plan-revision error; it does not silently run new code under the old task.
Submitting intentionally changed work requires a new task or a future explicit,
audited migration policy.

This policy preserves the existing immutable outer RuntimeEnv intent and prevents a
retry or resident owner from silently switching per-step profiles after a deployment.
Queued work that has never materialized a plan continues to use the definition present
when its first run begins.

## Strategy eligibility and diagnostics

Every strategy returns a structured decision before preparation or submission:

```json
{
  "plan_fingerprint": "sha256:...",
  "requested_policy": "auto",
  "selected_strategy": "dynamic_tasks",
  "eligible_strategies": ["local", "dynamic_tasks"],
  "rejections": [
    {
      "strategy": "compiled_graph",
      "code": "DYNAMIC_TOPOLOGY",
      "path": "topology.class",
      "message": "map node sync expands from invocation data"
    }
  ]
}
```

Diagnostics are bounded, deterministic for the same plan and capability set, ordered by
strategy then code/path, and contain no input values or secret material. Each rejection
has a stable machine-readable code, relevant plan path or node ID, and concise operator
message. Multiple reasons are reported together where validation can continue safely.

The initial common rejection codes are:

| Code | Meaning |
|---|---|
| `UNSUPPORTED_PLAN_VERSION` | The engine cannot interpret this plan format. |
| `DYNAMIC_TOPOLOGY` | Runtime data creates nodes or edges that the strategy requires to be fixed. |
| `UNBOUNDED_ADMISSION` | Fan-out, queue, in-flight work, result buffers, or retained outputs lack a required bound. |
| `UNSUPPORTED_NODE_MODEL` | The plan uses tasks, actors, or callable kinds the strategy cannot execute. |
| `UNRESOLVED_CODE_IDENTITY` | The callable or deployment revision is process-local or otherwise not reusable safely. |
| `UNRESOLVED_RUNTIME_ENV` | A profile, environment, or secret-dependent identity is not canonical and stable. |
| `INCOMPATIBLE_PLATFORM` | Ray version, OS, architecture, accelerator, transport, or optional dependencies are unsupported. |
| `OWNER_LIFETIME_MISMATCH` | The required invocation/reuse lifetime has no valid owner in the selected worker mode. |
| `UNSUPPORTED_TRANSPORT` | Payload, channel, buffer, reference forwarding, or result-consumption requirements cannot be met. |
| `UNSAFE_EFFECT_POLICY` | Required retry/cancellation/reuse semantics conflict with declared or unknown side effects. |

An explicit strategy request fails pre-submission if rejected. `auto` may select another
eligible strategy and records all relevant rejections. Once preparation or execution
can have created actors, channels, reserved resources, or application side effects,
automatic fallback is forbidden; the strategy must use its own failure, cancellation,
drain, and cleanup contract.

### Eligibility summary

| Strategy | Eligible shapes | Important rejection conditions |
|---|---|---|
| Local | Current definition shapes supported by the compatibility adapter | Unsupported plan version or callable import/argument binding failure |
| Dynamic Ray tasks | Static groups/chains and bounded dynamic maps | Unsupported Ray options, unresolved environments, or noncanonical plan data |
| Static actors | Static or fixed-width physical topology | Data-dependent node expansion, no actor layout, unbounded admission, unsafe state sharing, or owner mismatch |
| Compiled Graph | Repeated static/fixed-width actor regions on a supported capability set | Task nodes, dynamic expansion, unsupported callable/transport/platform, no stable compiler owner, unbounded in-flight/results, or incompatible lifecycle |

Ray Compiled Graph is currently beta and optimizes repeated execution of a static graph.
Its current ownership, actor, capacity, result-consumption, and teardown limitations are
strategy capability checks, not new task semantics. See the upstream
[Compiled Graph overview](https://docs.ray.io/en/latest/ray-core/compiled-graph/ray-compiled-graph.html)
and [troubleshooting limitations](https://docs.ray.io/en/latest/ray-core/compiled-graph/troubleshooting.html).

## Run, invocation, and observability identity

A durable workflow run identity contains at least:

```text
(task_execution_pk, attempt_number, execution_generation, run_id)
```

Every invocation adds an `invocation_id` unique within that run. Every progress
revision, strategy callback, result, cancellation, terminal flush, owner request, and
cleanup action is scoped to this identity. The plan fingerprint and definition revision
describe *what* is intended; run and invocation identity describe *which execution* may
write current state.

The compatibility path for the version 1 progress schema is additive:

- current dynamic node IDs remain runtime expansion paths scoped to one invocation;
- future snapshots add versioned run, invocation, plan, and selected-strategy summary;
- a client resets rather than merges state when run or invocation identity changes;
- progress revisions restart only with a new run/invocation identity;
- node reporting may be disabled, but run identity, plan fingerprint, requested policy,
  selected strategy, and bounded rejection summary remain available through durable
  task observability;
- progress remains observational and is never a recovery log.

Attempt/generation write fencing is implemented separately; this document defines the
identity that the persistence and strategy layers consume.

## Current `WorkflowSignature` compatibility

The public API remains source compatible while the plan boundary is introduced:

| Current expression | Plan-template mapping |
|---|---|
| `step(callable, ...)` | One logical call node. The import path, Django bootstrap flag, normalized Ray options, resolved environment identity, and callable kind become plan fields. Bound values become invocation slots unless a future explicit constant API marks a safe canonical literal. |
| `chain(a, b, ...)` | Ordered nodes/regions with the preceding result ports connected to the next expression's first input. |
| `group(a, b, ...)` | Static branches that receive the same input bindings and produce an ordered collect result. |
| `map_step(x)` | One dynamic-map operator in the plan. Runtime expansion nodes and inventory values are invocation state and do not enter the plan fingerprint. |
| `signature.run(*args, **kwargs)` | Materialize/validate a plan, create one invocation envelope, choose a strategy, execute, and return the same concrete result. |
| `use_ray=False` | Select the local strategy for deterministic tests; it does not create a different task type. |

The adapter preserves current argument ordering, bound-keyword precedence, ordered
group/map results, local fallback, importability validation, RuntimeEnv override
behavior, and one outer task durability boundary. Current `Step` dataclasses are frozen
only at the attribute level; dictionaries and bound objects can still mutate. The
materialized plan, rather than the signature object, becomes the immutable boundary.

No current caller becomes compiled merely because its `chain` or `group` is logically
static. Current steps are Ray task nodes. Static actors, an owner, and all eligibility
constraints must first be introduced and validated.

## Acceptance contract for plan snapshot implementation

Issue #84 and later implementations can cite these stable requirements:

1. **PLAN-01 -- Pre-submit boundary:** materialization, canonical validation,
   fingerprinting, and strategy eligibility complete before any remote submission,
   actor creation, channel allocation, or application side effect.
2. **PLAN-02 -- Deep immutability:** the effective plan contains immutable normalized
   data; mutation of source signatures, bound dictionaries, settings, or RuntimeEnv
   profiles after materialization has no effect.
3. **PLAN-03 -- Canonical equality:** semantically equal definitions under the same
   compatibility inputs produce byte-equal canonical JSON and equal fingerprints across
   supported Python processes.
4. **PLAN-04 -- Conservative invalidation:** every topology, callable/revision,
   resource, placement, actor layout, resolved environment, transport, buffer, compile,
   lifecycle, owner, Ray, or platform input listed above changes identity unless a
   versioned compatibility rule explicitly proves equivalence.
5. **PLAN-05 -- Resolved environments:** named outer and per-step profiles are resolved
   at materialization; profile content changes cannot reuse the old fingerprint.
6. **PLAN-06 -- Secret-free identity:** canonical bytes, persisted manifests,
   diagnostics, logs, and fingerprints contain no secret material or digest of raw
   secret material. Unresolved secret compatibility rejects reusable strategies.
7. **PLAN-07 -- Invocation separation:** task arguments, lifted bound values,
   Kubernetes inventory, request IDs, credentials, run identity, and observed state are
   invocation fields. Changing only those values does not change the fingerprint.
8. **PLAN-08 -- Stable code identity:** package, image, archive, and development code
   use a documented conservative revision. A process-local or unverifiable identity is
   rejected for reusable strategies with `UNRESOLVED_CODE_IDENTITY`.
9. **PLAN-09 -- Retry pinning:** the first materialized fingerprint is pinned to the
   execution attempt chain. A retry mismatch fails before submission and cannot route
   to an old owner or silently adopt a new definition.
10. **PLAN-10 -- Structured diagnostics:** validation returns stable code, plan path or
    node, and redacted message for every safely discoverable rejection. Explicit and
    automatic selection behavior follows the pre-start fallback rule.
11. **PLAN-11 -- Durable summary:** run identity, plan version/fingerprint, definition
    revision, requested policy, selected strategy, and bounded rejection summary remain
    observable when node progress reporting is disabled.
12. **PLAN-12 -- API parity:** existing `step`, `chain`, `group`, `map_step`,
    `with_options`, `with_runtime_env`, `run`, and local execution tests retain their
    documented result and argument semantics on the default dynamic strategy.
13. **PLAN-13 -- Runtime-node scoping:** dynamic map expansion IDs, Ray IDs, graph
    handles, progress revisions, results, and timings are scoped to run/invocation state
    and never alter or enter canonical plan identity.
14. **PLAN-14 -- Bounded persistence:** any stored plan manifest and diagnostic summary
    has explicit size/depth/count limits, redaction, and versioning. Runtime engine
    objects and unbounded node graphs are never persisted as plan metadata.

These requirements do not choose a database model, resident-owner topology, or compiled
adapter. Those decisions remain in their focused issues.

## Non-goals

- Implement an effective-plan materializer, executor, owner, actor pool, or Compiled
  Graph integration in this documentation change.
- Add a database row for each logical work item or workflow node.
- Change the outer at-least-once durability and retry boundary.
- Infer that a side-effect declaration is true without application tests and controls.
- Make arbitrary Django tasks, dynamic maps, or process-local Python objects eligible
  for compilation.

## Related documentation

- [Architecture](architecture.md)
- [Ray-Native Workflows](workflows.md)
- [Performance](performance.md)
- [Runtime Environments](runtime-environments.md)
- [Retry and Error Handling](retry.md)
- [ADR-0001: Workflow plans and execution strategies](design/adr-0001-workflow-plan-contract.md)
