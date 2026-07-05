# Worker Execution Modes

The task-manager process always claims durable work from Django's database. Its
execution mode determines how that work reaches Ray.

## Comparison

| Mode | Command | Best fit | Relative startup cost |
|---|---|---|---|
| Sync | `--sync` | Tests and debugging | Lowest |
| Local Ray Core | `--local` | Development and one-machine execution | Low after Ray starts |
| Cluster Ray Core | `--cluster=ray://host:10001` | Low-latency production workflows | Low |
| Ray Job | no mode flag with `RUNNER="ray_job"` | Isolated, coarse jobs | Highest |

If `RUNNER="ray_core"` and no mode flag is supplied, `RAY_ADDRESS="auto"` selects
local mode and a cluster address selects cluster mode.

## Sync

```bash
python manage.py django_ray_worker --queue=default --sync
```

No running Ray cluster is required. Tasks execute in the task-manager process, one at
a time, which makes breakpoints and deterministic tests straightforward. Ray-native
workflow signatures use their local fallback, so sync mode does not demonstrate
parallel speedup or Ray failure behavior.

## Local Ray Core

```bash
python manage.py django_ray_worker --queue=default --local
```

The task manager starts a local Ray runtime, and the dashboard is normally available at
http://127.0.0.1:8265. Use this for development, workflow tests, and single-machine
parallelism.

## Cluster Ray Core

```bash
python manage.py django_ray_worker \
  --queue=default \
  --cluster=ray://ray-head.example:10001
```

The task manager submits functions through Ray Client. Worker processes are reused,
workflow leaves exchange object references directly, and there is no separate Ray Job
driver per durable task. This is normally the best production mode for short tasks,
high throughput, and multi-stage workflows.

## Ray Job

```bash
python manage.py django_ray_worker --queue=default
```

With `DJANGO_RAY["RUNNER"] = "ray_job"`, each durable task is submitted through Ray's
Job Submission API and gets an isolated driver process. Ray-native workflows are
supported: the driver connects back to its Ray cluster when it starts submitting
leaves.

Choose Ray Job when driver isolation, independent logs, or coarse job lifecycle is
more valuable than startup latency. Avoid it for thousands of tiny tasks.

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

For dependent stages and UI-visible graphs, prefer
[Ray-native workflows](workflows.md).

## Choose an Execution Model

| Main concern | Start with |
|---|---|
| deterministic tests or stepping through Python | Sync |
| development on one machine | Local Ray Core |
| low-latency task and workflow submission | Cluster Ray Core |
| process isolation for long, coarse jobs | Ray Job |

Ray Core is the performance default. Ray Job is deliberately more expensive: choose it
because isolation is worth the cost, not merely because it is the configuration
default.

## See Also

- [Performance](performance.md) for workload-size and batching guidance
- [Runtime Environments](runtime-environments.md) for environment startup cost
- [Kubernetes Deployment](deployment/kubernetes.md) for production topology
