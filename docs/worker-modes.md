# Worker Execution Modes

The task-manager process always claims durable work from Django's database. Its
execution mode determines how that work reaches Ray.

For how these transports compose with Data, Train, Tune, RLlib, Serve, and Compiled
Graph, see the [Ray Ecosystem Support and Install Matrix](ray-ecosystem.md).

## Comparison

| Mode | Command | Best fit | Relative startup cost |
|---|---|---|---|
| Sync | `--sync` | Tests and debugging | Lowest |
| Local Ray Core | `--local` | Development and one-machine execution | Low after Ray starts |
| Cluster Ray Core | `--cluster=ray://host:10001` | Bounded low-latency workflows with a connection-owned lifetime | Low |
| Ray Job | no mode flag with `RUNNER="ray_job"` | Isolated, coarse jobs | Highest |

If `RUNNER="ray_core"` and no mode flag is supplied, `RAY_ADDRESS="auto"` selects
local mode and a cluster address selects cluster mode.

Both synchronous and coroutine Django tasks are supported in every mode. A coroutine
gets one fresh event loop for its invocation; the task manager itself remains
synchronous and continues to own claims, heartbeats, retries, and reconciliation.

## Sync

```bash
python manage.py django_ray_worker --queue=default --sync
```

No running Ray cluster is required. Tasks execute in the task-manager process, one at
a time, which makes breakpoints and deterministic tests straightforward. Ray-native
workflow signatures use their local fallback, so sync mode does not demonstrate
parallel speedup or Ray failure behavior.

Coroutine tasks are awaited to completion on their per-task loop. While that loop is
running, the same sync worker cannot perform coordination work or interrupt the task.
Cancellation and timeout decisions are applied after control returns, matching the
existing limitation for long-running synchronous callables.

## Local Ray Core

```bash
python manage.py django_ray_worker --queue=default --local
```

The task manager starts a local Ray runtime, and the dashboard is normally available at
http://127.0.0.1:8265. Use this for development, workflow tests, and single-machine
parallelism.

Coroutine tasks run on the reused Ray worker but do not reuse an event loop from a
previous task. Await all child work before returning; detached `asyncio` tasks are not
durable Ray or Django tasks.

## Cluster Ray Core

```bash
python manage.py django_ray_worker \
  --queue=default \
  --cluster=ray://ray-head.example:10001
```

The task manager submits functions through Ray Client. Worker processes are reused,
workflow leaves exchange object references directly, and there is no separate Ray Job
driver per durable task. This is the lowest-overhead remote mode for bounded short
tasks, high throughput, and multi-stage workflows when the task-manager connection is
operated as part of the workload lifetime.

Ray Client is not an independent job lifecycle. An unexpected disconnect receives a
30-second reconnect grace period by default; Ray's
`RAY_CLIENT_RECONNECT_GRACE_PERIOD` environment variable can change it. If the task
manager cannot reconnect, Ray drops that client's references and documents the result
as terminating its in-flight workload. django-ray retains the outer database row and
can reconcile or retry it, but retry starts a new attempt: it does not resume completed
leaves, prove an external side effect did not happen, or roll anything back. Keep the
task-manager connection stable, bound the work, and make effects idempotent. Ray's own
[Ray Client guidance](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/ray-client.html)
recommends Ray Jobs for long-running work and documents architectural limitations for
Train and Tune over Ray Client. django-ray has not validated Train, Tune, RLlib, or
other component-owned lifecycles on this transport. Keep them off Ray Client unless
their own integration evidence establishes the complete lifecycle; use an
application-owned Ray Job or the component's normal operator instead.

## Ray Job

```bash
python manage.py django_ray_worker --queue=default
```

With `DJANGO_RAY["RUNNER"] = "ray_job"`, each durable task is submitted through Ray's
Job Submission API and gets an isolated driver process. Ray-native workflows are
supported: the driver connects back to its Ray cluster when it starts submitting
leaves.

Choose Ray Job when driver isolation, independent logs, a long or coarse job lifecycle,
or execution independent of the submitting task-manager connection is more valuable
than startup latency. An accepted Ray Job still does not survive every driver or
cluster failure; its application checkpoints and outer retry contract remain explicit.
Avoid it for thousands of tiny tasks.

Coroutine tasks use the same encoded entrypoint and completion envelope as synchronous
tasks. The isolated driver owns the per-task loop, and a Ray Job stop request terminates
that driver rather than preserving detached coroutine children.

## Distributed Utilities

`parallel_map()`, `parallel_starmap()`, and `scatter_gather()` work in local, cluster,
and Ray Job execution. Their callables must be serializable; module-level functions are
the reliable pattern:

```python
# myapp/tasks.py
from django.tasks import task

from django_ray.runtime.distributed import parallel_map


def process_one(item_id: int) -> dict[str, int | bool]:
    return {"id": item_id, "processed": True}


@task(queue_name="default")
def process_batch(item_ids: list[int]) -> list[dict[str, int | bool]]:
    return parallel_map(process_one, item_ids, max_concurrency=16)
```

Available helpers:

- `parallel_map(func, items)` applies one callable to every item.
- `parallel_starmap(func, argument_tuples)` unpacks positional arguments.
- `scatter_gather(calls)` executes heterogeneous call tuples.
- `get_num_workers()` reports the current Ray worker-node count.
- `get_ray_resources()` reports cluster resources.

Distributed helper remote wrappers are cached per process, so repeated calls do not
register a new Ray function definition. `max_concurrency` must be at least `1` when
provided, and resource requests (`num_cpus`/`num_gpus`) must be finite and non-negative.
`parallel_starmap()` requires argument tuples, while `scatter_gather()` requires
`(callable, args_tuple, kwargs_dict)` entries. Bounded calls use a sliding submission
window and still return results in input order.

For dependent stages and UI-visible graphs, prefer
[Ray-native workflows](workflows.md).

## Choose an Execution Model

| Main concern | Start with |
|---|---|
| deterministic tests or stepping through Python | Sync |
| development on one machine | Local Ray Core |
| bounded low-latency work while the task-manager connection remains stable | Cluster Ray Core |
| long/coarse work or a driver independent of the task-manager connection | Ray Job |

Ray Core is the performance default. Ray Job is deliberately more expensive: choose it
because isolation is worth the cost, not merely because it is the configuration
default.

## See Also

- [Performance](performance.md) for workload-size and batching guidance
- [Runtime Environments](runtime-environments.md) for environment startup cost
- [Kubernetes Deployment](deployment/kubernetes.md) for local evaluation and the production
  architecture checklist
