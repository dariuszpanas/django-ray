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
returns `True`. Metrics must be JSON-serializable.

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

## Durability Semantics

The outer Django task is the durability and retry boundary:

- Internal steps do not create individual Django tasks.
- While a workflow runs, an in-memory Ray coordinator collects node events. The
  outer task writes a bounded progress snapshot to `RayTaskExecution.progress_data`
  at `WORKFLOW_PROGRESS_FLUSH_SECONDS` intervals.
- A workflow invocation atomically claims `workflow_run_id`. Retry, cancellation,
  timeout, LOST recovery, and a newer invocation prevent its old coordinator from
  writing again; rejected reporters drain later leaf events without persisting them.
- Progress includes node paths, callable labels, node states, completion counts,
  dependency edges, percent complete, explicit leaf progress, Ray execution IDs,
  runtime environment identity, and recent events.
- A leaf failure fails the outer task.
- Retrying the outer task reruns the workflow, including previously completed leaves.
- `TaskAttempt` archives terminal task diagnostics, not workflow graphs. This keeps
  retry history bounded; `progress_data` and `workflow_run_id` describe only the
  current attempt or its latest terminal invocation.
- Cancellation of a Ray Core outer task recursively cancels its child tasks through
  Ray's normal cancellation behavior.
- The final workflow result must satisfy the same result-serialization rules as any
  django-ray task.

Ray Core tasks already run inside an initialized Ray worker. Ray Job drivers
initialize their cluster connection lazily when a workflow first requests Ray, and
use the same durable context and progress graph protocol.

Future execution strategies must preserve this outer durability boundary. In
particular, Compiled Graph is a possible engine for a validated static actor region,
not a Django task type or a flag that makes data-dependent `map_step` expansion static.
See [Workflow Plans and Execution Strategies](workflow-plans.md).

Apply the database migration before starting upgraded workers, and drain workflow
executions from older workers during a rolling deployment. Existing rows start with
`workflow_run_id = NULL`; the first upgraded, fully identified workflow invocation
claims a UUID. Custom uses of `durable_task_execution()` that omit the attempt or
execution generation continue to run their Ray workflow but intentionally do not
persist progress because their writes cannot be fenced safely.

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
| `report_progress(current, total, message=None, metrics=None)` | Report progress from a running leaf |
| `signature.run(*args, use_ray=None, **kwargs)` | Execute with Ray when initialized, otherwise locally |
