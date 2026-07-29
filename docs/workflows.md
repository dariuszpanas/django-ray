# Ray-Native Workflows

django-ray workflows combine one durable Django task with low-overhead Ray-native
steps. The outer task is queued, retried, cancelled, and recorded in the database.
Internal workflow steps are submitted directly to Ray and exchange intermediate
values through Ray object references without creating a database row per step.

This model is intended for fan-out workloads where database-backed dispatch would
cost more than the individual units of work.

`WorkflowSignature` objects are reusable definition builders. They are not durable
execution plans, and a logically static `chain` or `group` is not automatically a
compiled graph. The maintained [workflow-plan contract](workflow-plans.md) separates
definitions, immutable plans, per-run invocations, logical work, physical actors, and
execution strategies while preserving this public API.

## Requirements

Ray Core is the lowest-latency production path. Ray Job mode also supports workflows:
its isolated driver connects back to Ray lazily before submitting leaves. Local
execution is available for sync workers and unit tests.

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head:10001",
    "RUNNER": "ray_core",  # Lowest submission overhead.
}
```

Start a worker normally, or select the cluster explicitly:

```bash
python manage.py django_ray_worker --cluster=ray://ray-head:10001
```

## Complete Dynamic Fan-Out

The following is one complete `myapp/workflows.py` module. Every callable is defined at
module scope so Ray workers can import it:

```python
from django.tasks import task

from django_ray.workflows import chain, map_step, step


def build_items(count: int) -> list[int]:
    return list(range(count))


def calculate(value: int) -> dict[str, int]:
    # Stand-in for one independently expensive API or compute operation.
    checksum = sum((value * number) % 97 for number in range(500_000))
    return {"value": value, "checksum": checksum}


def summarize(results: list[dict[str, int]]) -> dict[str, int]:
    return {
        "items": len(results),
        "checksum": sum(result["checksum"] for result in results),
    }


calculation = chain(
    step(build_items),
    map_step(calculate, ray_options={"num_cpus": 0.25}).with_limits(
        max_concurrency=16,
        max_items=10_000,
    ),
    step(summarize),
)


@task(queue_name="default")
def calculate_batch(count: int) -> dict[str, int]:
    return calculation.run(count)
```

Calling `calculate_batch.enqueue(20)` creates one durable `RayTaskExecution`. Once it
starts, `build_items`, every `calculate` call, and `summarize` are connected inside
Ray. After its input iterable resolves, the bounded `map_step` consumes that iterable
lazily, keeps at most 16 map items submitted at once, incrementally resolves them, and
returns results in input order.

For a Kubernetes sync, the same shape is typically `list_namespaces → map(sync one
namespace) → summarize`. Keep client creation or discovery outside the smallest inner
resource loop where possible, and batch resources when each API operation is shorter
than Ray submission overhead.

## Bound Dynamic Fan-Out

Call `with_limits()` on a map signature when its input cardinality is data-dependent:

```python
sync_namespaces = map_step(
    sync_namespace,
    ray_options={"num_cpus": 0.25},
).with_limits(
    max_concurrency=8,
    max_items=500,
    cancel_timeout_seconds=1.0,
)
```

`max_concurrency` is an admission window. At most that many map-item result references
are retained while Ray work is pending, and results are collected as individual items
finish. Fast items can therefore make room without waiting for an earlier slow item,
while the final list remains in input order. The window counts **map items**, not every
physical task in a nested signature. A mapped `group` with three branches and a window
of eight can have up to 24 branch tasks plus its bounded per-item collectors submitted.
A nested dynamic map needs its own `with_limits()` contract; an outer window cannot cap
the number of leaves expanded inside one map item.

Lazy admission begins only after `map_step` calls `executor.resolve()` for its input. If
an upstream Ray task returns a list or inventory, that task still produces the complete
value and Ray transfers and deserializes it into the workflow coordinator before the
first map item is admitted. Only an iterator that is already available locally is pulled
one item at a time by the admission loop. Remote or paged input materialization is
tracked in [GitHub issue #94](https://github.com/dariuszpanas/django-ray/issues/94).

For every admitted item, django-ray retains the terminal result reference and the
physical dependency references created by its nested `chain` or `group`. Those cleanup
references are released as soon as the item result is collected, so their peak remains
the admission window multiplied by the signature's fixed physical width.

`max_items` is an expansion safety limit. Sized inputs that exceed it fail before any
leaf is submitted. For a generator, detecting overflow requires reading item
`max_items + 1`; work completed before that discovery is not rolled back. Use idempotent
leaves when iteration, retries, or cancellation can overlap external side effects.

On the first leaf, iterator, or collection failure, django-ray stops reading new items,
requests cancellation for every retained physical reference belonging to the failed and
pending items, and waits up to `cancel_timeout_seconds` for them to become terminal. It
does not assume that cancelling a final task also cancels its Ray input dependencies.
The default deadline is one second. The original exception is always re-raised.
References still pending after the deadline are released rather than making cleanup wait
indefinitely; an uncooperative running leaf may consequently finish after the workflow
has failed. The deadline bounds only Ray cancellation and drain waiting. It does not
bound input deserialization or arbitrary user iterator cleanup such as a generator's
`close()` method.

Bounded maps use one aggregate `kind="map"` progress node with submitted, completed,
in-flight, and input-exhaustion counters. Their physical item nodes are intentionally
omitted from the live workflow graph. This keeps observability proportional to the
declared workflow rather than to a 10k- or 50k-item expansion. For the same reason,
`report_progress()` returns `False` inside those physical item leaves; the aggregate map
counters remain available.

Incremental collection bounds pending references, not total result bytes. The ordered
result list is still materialized in the workflow coordinator before it is placed back
in Ray, so coordinator memory remains proportional to total output size. A bounded
in-Ray reduction or aggregation path is tracked in
[GitHub issue #91](https://github.com/dariuszpanas/django-ray/issues/91).

### Opt-in Ray result buffer

When the next stage is a Ray step, a bounded map can keep its complete intermediate
list out of the outer workflow coordinator:

```python
sync_resources = chain(
    step(list_namespaces),
    map_step(sync_namespace, ray_options={"num_cpus": 0.25})
    .with_limits(max_concurrency=8, max_items=500)
    .with_result_buffer(
        max_serialized_bytes=64 * 1024 * 1024,
        actor_options={
            "num_cpus": 0.25,
            "memory": 128 * 1024 * 1024,
            "resources": {"workflow_result_buffer": 1},
            "scheduling_strategy": "SPREAD",
        },
    ),
    step(summarize_sync),
)
```

This is an explicit transport selection. `max_concurrency`, `max_items`, and
`max_serialized_bytes` must all be positive caller-declared limits. `actor_options`
must explicitly set `num_cpus > 0` and an integer `memory` request at least as large
as `max_serialized_bytes`. The only optional version 1 actor fields are a bounded
positive `resources` mapping and `scheduling_strategy="DEFAULT"` or `"SPREAD"`;
unknown fields are rejected instead of being forwarded to Ray.
Every requested custom resource must be advertised by an eligible cluster node. If
it is unavailable, the ready handshake intentionally remains pending while ordinary
progress flushing and cancellation stay live, and no map leaf effects begin.

The non-detached actor has fixed `max_restarts=0`, `max_task_retries=0`,
`max_concurrency=1`, and `max_pending_calls=2`. django-ray waits for its ready
acknowledgement through normal progress-flushing resolution before admitting leaf
side effects. The leaf admission window remains at `max_concurrency`, but the outer
coordinator uses `ray.wait(..., fetch_local=False)` to select a ready leaf without
decoding it. It then submits and completely resolves one small append acknowledgement
before issuing another actor call. The second pending-call slot only gives Ray room to
retire a completed prior call from the sender handle's bookkeeping before accepting its
successor. It does not permit concurrent protocol transitions, multiple unacknowledged
calls, or an extra retained result. Map payloads are never resolved by the coordinator.

The byte limit counts bytes actually retained by the version 1 `ray.cloudpickle`
codec using pickle protocol 5. An append serializes first, checks the prospective item
and byte totals, and mutates retained state only when both remain within their bounds.
This is not a limit on decoded Python heap size, transient serialization memory, Ray
wire bytes, or object-store bytes. The scheduler `memory` request accounts for actor
placement but is not a hard process-memory limit.

Finalization returns the ordered Python list and a small acknowledgement as two direct
Ray returns. It does not call `ray.put()` inside the actor and does not nest an
`ObjectRef` inside another return. The coordinator proves both returns are materialized,
resolves only the acknowledgement, kills the actor best effort, and passes the one
unresolved payload reference to the downstream Ray step. Append and finalization waits
continue flushing ordinary workflow progress.

If the buffered map is terminal, `run()` necessarily resolves that final reference and
the coordinator still uses O(total output) memory. A downstream step also materializes
the complete ordered list and therefore uses O(total output) consumer memory, while the
object store holds the final object. The actor itself transiently decodes the complete
list during finalization. Chunked/reducer transport and total-independent object-store
or consumer memory remain in [GitHub issue #91](https://github.com/dariuszpanas/django-ray/issues/91),
and production-shaped throughput and memory validation remain in
[GitHub issue #87](https://github.com/dariuszpanas/django-ray/issues/87).

A version 1 result-buffer map cannot be nested inside a dynamic map; plan
materialization rejects that topology before actor or leaf effects so actor
multiplication stays explicit. Local execution preserves input order, `max_items`, and
ordinary leaf failure behavior without creating an actor. It validates and fingerprints
the buffer selection but does not retain, cloudpickle, or measure results against
`max_serialized_bytes`; that byte limit is specifically the Ray actor's retained-byte
contract and is enforced only during Ray execution. Maps that do not call
`with_result_buffer()` retain their existing bounded or eager transport exactly.
Cleanup cancels pending or resolving leaf references plus their captured nested
dependencies, then discards the serialized bytes retained by the actor and kills it
best effort on failure or cancellation. Non-detached ownership also cleans the actor
when its owner dies, but the protocol makes no cleanup, recovery, or durability
guarantee after node loss.

### Opt-in ordered result fold

When a workflow needs one compact summary rather than the complete mapped list, call
`reduce()` on a bounded map:

```python
sync_resources = chain(
    step(list_namespaces),
    map_step(sync_namespace, ray_options={"num_cpus": 0.25})
    .with_limits(max_concurrency=8, max_items=500)
    .reduce(
        step(
            merge_sync_summary,
            runtime_env="kubernetes-sync",
        ),
        initial=empty_sync_summary,
        max_serialized_bytes=8 * 1024 * 1024,
        actor_options={
            "num_cpus": 0.25,
            "memory": 16 * 1024 * 1024,
            "scheduling_strategy": "SPREAD",
        },
    ),
    step(store_sync_summary),
)
```

`reduce()` changes the map result from `list[item]` to one accumulator. It requires
positive `max_concurrency`, `max_items`, and `max_serialized_bytes` limits, an explicit
`initial` value (including explicit `None` when appropriate), the same narrow
resource-accounted `actor_options` accepted by `with_result_buffer()`, and exactly one
`Step` reducer. The reducer contract is a strict input-order left fold:

```python
reducer(accumulator, item, *bound_args, **bound_kwargs) -> accumulator
```

The reducer must be synchronous and return a concrete non-generator value. Reducer Ray
task options are rejected because it runs inside the fold actor, not as a separate Ray
task. Its importable callable identity, Django bootstrap flag, bound-argument schema,
and resolved effective RuntimeEnv are applied to and fingerprinted with that actor.
Actor-internal calls do not create per-item progress nodes, and `report_progress()` or
durable workflow task context inside the reducer is unsupported in version 1. Reducers
should be pure: retries or failures do not provide exactly-once reducer side effects.

The initial accumulator is invocation data. Its value is cloudpickled and validated by
the actor before any mapper leaf is admitted, but it is never persisted in the effective
plan or included in the plan fingerprint. The plan records only the required initial
binding schema. Local execution also cloudpickle round-trips the initial value before
leaves so every run receives a fresh decoded accumulator, and it imports and validates
the reducer before mapper effects. A reducer supplied only by a RuntimeEnv is therefore
Ray-only. Local mode does not create an actor or enforce the Ray retained-byte limit.

Leaves may finish in any order, but the actor keeps its accumulator serialized and folds
only the next contiguous input index. Admission credits are replenished only when items
are incorporated. If the earliest item is slow, no more than
`min(max_items - 1, max_concurrency - 1)` later results can be retained, and no more than
`max_concurrency` mapper references remain admitted. This head-of-line backpressure is
intentional: the reducer need not be associative or commutative, so a parallel tree
reduction would change behavior.

The actor still executes protocol transitions serially with `max_concurrency=1`, and the
coordinator completely resolves each small acknowledgement before issuing the next
transition. `max_pending_calls=2` leaves one per-handle bookkeeping slot for Ray to retire
a completed prior call without rejecting its successor; it does not permit two
fold transitions to execute concurrently.

`max_serialized_bytes` covers the serialized accumulator plus serialized out-of-order
items retained by the actor. Initial, individual item, resulting accumulator, and
combined-state checks are transactional: state changes only after the complete
contiguous fold candidate succeeds. Overflow can still be completion-order-sensitive.
For example, an early large out-of-order result may fail the combined-state check before
the missing earlier item can reduce the accumulator. A schedule-independent byte limit
would require a different deferred/backpressure protocol. The limit does not hard-cap
temporary decoded actor heap, reducer working memory, serialization scratch space, Ray
wire bytes, or object-store bytes. Ray's scheduler `memory` request remains placement
accounting rather than process enforcement.

The coordinator waits for leaves with `ray.wait(..., fetch_local=False)`, resolves only
small protocol acknowledgements, and never decodes mapped items or the intermediate
accumulator. Finalization returns the accumulator and acknowledgement as two direct Ray
returns; one unresolved accumulator reference becomes the single dependency of a
downstream Ray step. A terminal fold necessarily resolves that one accumulator in
`run()`, so its final size still matters, but no O(total item) list is constructed.

Fold mode and `with_result_buffer()` are mutually exclusive. A version 1 fold cannot be
nested inside a dynamic map, and its mapper cannot itself contain a dynamic map; plan
materialization rejects both shapes before actor or leaf effects. Static `chain` and
`group` mapper bodies remain supported. Mapper, reducer, actor, byte-limit, and
cancellation failures stop admission, recursively cancel pending nested dependencies
without fetching their payloads into the coordinator, discard actor state, and preserve
the original exception. Non-detached ownership and fixed no-restart semantics match the
result-buffer protocol. List-preserving chunk or hierarchical transport remains in
[GitHub issue #91](https://github.com/dariuszpanas/django-ray/issues/91), and production
evidence remains in [GitHub issue #87](https://github.com/dariuszpanas/django-ray/issues/87).

Calling `map_step()` without `with_limits()` retains the original eager behavior for
compatibility. New dynamic workloads should normally choose an explicit window and an
input cap. Local execution preserves inputs, ordered outputs, limits, and failures but
runs leaves sequentially.

### Choose Concurrency and Batch Granularity

For a rate-limited Kubernetes or HTTP API, `max_concurrency` limits concurrent batches;
it does not enforce requests per second. Keep the API client's own token-bucket or
server-advertised retry policy enabled. Choose the mapped item deliberately:

- Map one namespace when discovery and reconciliation can share one client session and
  one failure boundary.
- Map one `(namespace, resource_kind)` batch when namespaces contain enough resources to
  leave cluster capacity idle.
- Batch several tiny resources into one item when a single API round trip is shorter
  than Ray submission overhead.

Start with a window no larger than the external client's connection pool, benchmark
throttling and retry rates, and increase it only while useful throughput improves. A
preceding step can construct batches; `map_step` passes each batch to one leaf and
returns one ordered result per batch.

## Chains and Groups

`chain` passes each result as the first argument to the next signature. Reusing the
module above:

```python
pipeline = chain(
    step(build_items),
    map_step(calculate),
    step(summarize),
)
```

`group` sends the same input to every child and returns an ordered result list:

```python
from django_ray.workflows import group


def minimum(values: list[int]) -> int:
    return min(values)


def maximum(values: list[int]) -> int:
    return max(values)


def total(values: list[int]) -> int:
    return sum(values)


inspect_values = chain(
    step(build_items),
    group(
        step(minimum),
        step(maximum),
        step(total),
    ),
)
```

Groups can contain chains, maps, or other groups.

## Django-Aware and Native Steps

Workflow steps are Ray-native by default and skip Django initialization. This is the
fast path for API clients, transformations, and compute that do not use Django models:

```python
step(calculate)
```

Set `django=True` when a step needs Django's app registry, ORM, settings-dependent
components, or another Django facility:

```python
def load_account_name(account_id: int) -> str:
    from myapp.models import Account

    return Account.objects.values_list("name", flat=True).get(pk=account_id)


load_account = step(load_account_name, django=True)
```

Django initialization is guarded by the app registry, so a reused Ray worker does not
initialize Django again for every step.

Use `ray_options` or `Step.with_options()` for Ray scheduling controls:

```python
gpu_calculation = step(
    calculate,
    ray_options={"num_gpus": 1, "max_retries": 2},
)
two_cpu_calculation = step(calculate).with_options(num_cpus=2)
```

Use a named RuntimeEnv profile when a leaf needs different dependencies:

```python
numpy_calculation = step(calculate, runtime_env="numpy-2-3")
```

Leaves otherwise inherit the outer durable task's environment. See
[Runtime Environments](runtime-environments.md).

## Application Progress

Long-running leaves can report progress without writing to Django directly:

```python
from django_ray.workflows import report_progress


def normalize_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized = []
    for index, row in enumerate(rows, start=1):
        normalized.append({key: value.strip() for key, value in row.items()})
        if index % 100 == 0:
            report_progress(
                index,
                len(rows),
                message="Normalizing rows",
                metrics={"last_row": index},
            )
    return normalized
```

`report_progress()` is a no-op that returns `False` during local workflow
execution. On Ray it updates the node through the in-memory coordinator and
returns `True`. Metrics are bounded operational metadata: use at most 32 scalar
string, number, boolean, or null values. Keys are capped at 64 UTF-8 bytes, strings at
256 UTF-8 bytes, and the normalized mapping at 4 KiB; sensitive or oversized values
are redacted or replaced before the event crosses Ray.

## Graph and Progress Schema

Every snapshot is a versioned graph suitable for a custom task-tracking UI:

```json
{
  "schema_version": 2,
  "workflow_id": "django-ray:42",
  "run_identity": {
    "schema_version": 1,
    "run_id": "2eb22ff3-5fd2-43a0-834c-d920737b584c",
    "task_execution_pk": 42,
    "attempt_number": 2,
    "execution_generation": 5
  },
  "revision": 12,
  "state": "RUNNING",
  "progress_percent": 50.0,
  "graph": {
    "nodes": [
      {
        "node_id": "0.1.m0",
        "kind": "task",
        "label": "sync_resource",
        "callable_path": "myapp.workflows.sync_resource",
        "dependencies": ["0.0"],
        "state": "RUNNING",
        "progress": {"current": 50, "total": 100, "percent": 50.0},
        "runtime_env": {"mode": "inherit", "hash": "..."},
        "execution": {
          "ray_task_id": "...",
          "ray_job_id": "...",
          "ray_node_id": "...",
          "ray_worker_id": "..."
        }
      }
    ],
    "edges": [{"source": "0.0", "target": "0.1.m0"}]
  }
}
```

Node IDs are stable for one workflow expansion. Dynamic map nodes appear after
their input iterable resolves, so clients should redraw when `revision` changes.
Revisions are monotonic only within one `run_identity.run_id` and restart when a
new invocation claims progress ownership. Clients must reset their stored graph
before applying a revision from a different run ID, attempt, or execution
generation. Database writes occur only when the coordinator revision changes and
the task is still `RUNNING` with that exact attempt, generation, and run ID. The
independent task-monitor heartbeat still proves that the owning worker is alive.

## Local Execution

Signatures run locally when Ray is not initialized. This makes workflow logic easy to
exercise in unit tests:

```python
result = calculation.run(4, use_ray=False)
assert result["items"] == 4
```

Set `use_ray=True` to fail instead of falling back when Ray is unavailable.

## Workflow Progress Policy

Ray workflow node reporting defaults to the configured
`WORKFLOW_PROGRESS_REPORTING_POLICY="full"`. A latency-sensitive or very large
workflow can opt out for one invocation:

```python
result = calculation.with_progress_reporting("disabled").run(4, use_ray=True)
```

Disabled reporting creates no workflow progress actor, sends no node or application
progress RPCs, and writes no `RayTaskExecution.progress_data`. Calls to
`report_progress()` return `False`, and the workflow submits the same Ray work. Inside
a durable task, it still claims and fences its exact run, pins the effective plan and
selected strategy, and preserves the outer task's state, result/error, retry,
cancellation, recovery, and monitor heartbeat behavior.

The bounded task summary exposes the effective policy. Authorized bounded workflow
progress readers return `availability="DISABLED"` with no fabricated summary or graph
for the current disabled run. The plan selection is the current-attempt signal, so
an active full-reporting run without schema v3 remains `NOT_REPORTED`, while a terminal
full-reporting run without schema v3 is `MISSING`. Historical attempts without an
archived schema-v3 summary remain `NOT_REPORTED`.
Local execution has no progress actor and is recorded as disabled regardless of the
configured Ray reporting default.

Only `"full"` and `"disabled"` are executable policies today. Sampled and terminal-only
collection remain later #79 work; changing
`WORKFLOW_PROGRESS_FLUSH_SECONDS` only throttles full-mode database snapshots and does
not remove producer or actor overhead.

Full reporting prepares every data event as canonical identity-bound JSON before the
Ray call. Event payloads are capped at 16 KiB, complete wire and decoded envelopes at
32 KiB, and dependency-edge batches at 32 edges. The actor revalidates the complete
task, attempt, generation, and workflow-run fence before mutation. Its node, edge,
recent-event, and retained-byte state uses the same V1 limits as durable workflow
progress, with bounded accepted, rejected, and truncated diagnostics. Descriptive
metadata is redacted before it crosses Ray.

These bounds close the individual producer-to-actor and retained-collector gap. They
do not bound the aggregate number or bytes of events already queued in Ray's actor
mailbox, coalesce repeated producer updates, provide producer backpressure, or provide
the bounded actor-to-preparation drain needed at the hard V1 ceilings. Those remain
separate #79 scale and default-activation work. They do not block #212 from proving a
default-off schema-v3 pilot under stricter admission limits. A bounded event or actor
snapshot is observational state, not a recovery or flow-control protocol.

## Durability Semantics

The outer Django task is the durability and retry boundary:

- Internal steps do not create individual Django tasks.
- In full reporting mode, an in-memory Ray coordinator collects node events. The outer
  task currently writes a complete progress snapshot to
  `RayTaskExecution.progress_data` at `WORKFLOW_PROGRESS_FLUSH_SECONDS` intervals.
  Individual producer envelopes and retained actor nodes, edges, events, and bytes are
  bounded. The aggregate Ray mailbox, snapshot-to-preparer drain, and transient
  snapshot materialization are not yet governed by the later admission/backpressure
  contract. Disabled mode bypasses the coordinator, codec, actor, and snapshot path
  while leaving the durable outer-task boundary intact.
- A separate nullable `workflow_progress_summary_json` field and schema-v3 codec are
  deployed reader-first. The field is fixed-shape and capped at 16 KiB of canonical
  UTF-8 JSON. Package-owned topology/detail tables and the internal storage writer can
  now publish a verified immutable topology, sparse normalized latest-state detail,
  and that summary pointer in one transaction. The standalone schema-v3 writer rejects
  topology/detail pointers; it is the only path for an intentional summary-only
  `DISABLED` or `OMITTED_BY_POLICY` record, which creates no empty detail storage.
  Current coordinators intentionally remain schema-v2 writers. Authorized public
  readers are implemented. Issue #212 owns a default-off initial producer that applies
  admission limits below the hard V1 ceilings, uses the existing compatibility
  preparer, and is proved through the bundled project and local KubeRay. General or
  default activation still requires #79's mailbox controls, #142's composite bounded
  preparation, aggregate spill capacity ownership, and an old-writer drain. Topology
  preparation already uses ADR-0005's spill-backed package path, while the compatibility
  result still carries complete observed membership for materialized initial detail.
- A workflow invocation atomically claims `workflow_run_id`. Retry, cancellation,
  timeout, LOST recovery, and a newer invocation prevent its old coordinator from
  writing again; rejected reporters drain later leaf events without persisting them.
- Before the first leaf submission, the workflow stores a bounded, secret-free
  effective-plan manifest, fingerprint, and strategy selection independently from
  progress. A retry must reproduce the pinned fingerprint and have retry-safe
  RuntimeEnv bindings, or it fails closed before submitting changed work.
- Progress includes node paths, callable labels, node states, completion counts,
  dependency edges, percent complete, explicit leaf progress, Ray execution IDs,
  runtime environment identity, and recent events.
- A leaf failure fails the outer task.
- Retrying the outer task reruns the workflow, including previously completed leaves.
- `TaskAttempt` archives terminal task diagnostics, not workflow graphs. If a run has
  already published a canonical terminal schema-v3 summary, the same bounded value is
  archived with its attempt before retry cleanup. Otherwise, lifecycle reconciliation
  derives a terminal envelope from the last accepted running summary under the row
  lock. The outer outcome and expiry are authoritative. Success marks every discovered
  node complete; interrupted outcomes retain the last accepted counters. Invalid
  summaries and legacy complete snapshots are not copied. This keeps retry history
  bounded;
  `progress_data`, the current summary, and `workflow_run_id` otherwise describe only
  the current attempt or its latest terminal invocation.
- Cancellation of a Ray Core outer task recursively cancels its child tasks through
  Ray's normal cancellation behavior.
- The final workflow result must satisfy the same result-serialization rules as any
  django-ray task.

ADR-0004 accepts the replacement storage contract: a dedicated always-bounded
task-row summary points to immutable topology manifests/pages and normalized
latest-state node-detail rows. Changed detail rows are batch-upserted before the
#81-fenced summary pointer advances; each accepted detail-changing publication expires
prior cursors, while static topology is not duplicated across repeated publications or
state transitions within one run and topology version.
Authorized readers use revision-bound pagination. The ADR defines exact V1 limits,
availability states, legacy v1/v2 reads, retention, and cleanup. Its first delivery now
implements the nullable current/per-attempt summary fields, strict schema-v3 codec,
monotonic exact-run writer primitive, bounded rolling reader, and lifecycle archival.
Its second delivery adds the run-scoped topology manifests/pages, normalized
latest-state rows, bounded staging and integrity verification, sparse atomic
publication, terminal expiry, and retention/orphan cleanup. Public detail services are
implemented, but the runtime producer still writes complete schema-v2 snapshots.
ADR-0005's production topology phase now externalizes exact node/edge identity,
duplicate, reference, and selection state into a private bounded SQLite workspace and
removes it before returning prepared evidence. The unchanged result still includes
complete `observed_node_ids` for the existing materialized detail API, so #142 must
complete the shared topology/detail lifetime before activation. See
[ADR-0004: Bounded Workflow Progress Storage](design/adr-0004-bounded-workflow-progress.md)
and
[ADR-0005: Bounded Workflow Progress Preparation](design/adr-0005-bounded-workflow-preparation.md).

Ray Core tasks already run inside an initialized Ray worker. Ray Job drivers
initialize their cluster connection lazily when a workflow first requests Ray, and
use the same durable context and progress graph protocol.

Future execution strategies must preserve this outer durability boundary. In
particular, Compiled Graph is a possible engine for a validated static actor region,
not a Django task type or a flag that makes data-dependent `map_step` expansion static.
See [Workflow Plans and Execution Strategies](workflow-plans.md).

Apply database migrations before starting upgraded workers. Migration
`0012_workflow_progress_summary` adds nullable summary fields, and migration
`0013_workflow_progress_detail_storage` adds dormant package-owned topology and detail
tables. Neither migration rewrites existing `progress_data`; older writers continue to
work and upgraded readers retain the 64 MiB schema-v1/v2 compatibility cap. Do not
enable unguarded schema-v3 production yet. Deploy the authorized public facade and
bounded ingress first, then use #212's default-off pilot and stricter admission profile
while old workflow writers drain. Complete #79's remaining mailbox controls, #142's
composite preparation, and aggregate spill ownership before enabling schema v3 by
default or admitting workloads near the hard V1 ceilings.

Existing rows start with `workflow_run_id = NULL` and a nullable plan identity so an
older writer can still insert during the rollout; the first upgraded, fully identified
workflow invocation claims a UUID and pins its plan. Prepared actors or graphs in later
strategies must drain when that fingerprint changes. Custom uses of
`durable_task_execution()` that omit the attempt or execution generation continue to
run their workflow but intentionally do not persist the plan or progress because their
writes cannot be fenced safely.

The drain statement is a contract for those later strategies. The current release
provides fingerprint comparison and stale-owner helper functions but has no resident
graph owner or prepared-graph cache to drain.

An identified context whose attempt or execution generation is stale is different: its
plan claim fails closed before local application code, progress actors, or Ray leaves
can run.

Use idempotent steps when retries can repeat external side effects. Durable stage
checkpoints remain a planned extension. Progress is observational rather than a
recovery log: after a cluster loss, the outer task retry remains the recovery boundary.

## Test Project Examples

The bundled test project exposes two experiments in its Swagger UI:

```text
POST /api/cluster/workflow-benchmark
GET  /api/cluster/workflow-benchmark/{task_id}

POST /api/cluster/complex-workflow
GET  /api/cluster/complex-workflow/{task_id}

GET  /api/cluster/workflows/{task_id}/graph
GET  /api/cluster/workflows/{task_id}/nodes/{node_id}
GET  /api/cluster/workflows/{task_id}/nodes/{node_id}?include_logs=true&tail=200
```

The complex example runs this nested shape:

```text
chain(
    build_config,
    group(
        chain(build_fast_items, map(fast_leaf), summarize_fast),
        chain(build_slow_items, map(slow_leaf), summarize_slow),
    ),
    summarize_workflow,
)
```

The GET endpoints return live progress while the outer Django task is running and
the timing/result tree after it completes.

## API Reference

| API | Behavior |
|---|---|
| `step(callable, *args, django=False, ray_options=None, runtime_env=None, **kwargs)` | Bind an importable callable as one workflow step |
| `chain(*signatures)` | Run signatures sequentially |
| `group(*signatures)` | Fan out the same input and gather ordered results |
| `map_step(callable_or_signature, ...)` | Fan out over the preceding iterable; callable keyword arguments remain leaf arguments |
| `map_signature.with_limits(max_concurrency=None, max_items=None, cancel_timeout_seconds=1.0)` | Add bounded admission, expansion, and failure-cleanup controls |
| `bounded_map.with_result_buffer(max_serialized_bytes=..., actor_options=...)` | Opt into a resource-accounted Ray actor that forwards one ordered payload reference without coordinator decoding |
| `report_progress(current, total, message=None, metrics=None)` | Report progress from a running leaf |
| `signature.run(*args, use_ray=None, **kwargs)` | Execute with Ray when initialized, otherwise locally |
| `signature.with_progress_reporting(policy).run(...)` | Execute one invocation with explicit `"full"` or `"disabled"` node reporting without reserving an application keyword |
