# Ray-Native Workflows

django-ray workflows combine one durable Django task with low-overhead Ray-native
steps. The outer task is queued, retried, cancelled, and recorded in the database.
Internal workflow steps are submitted directly to Ray and exchange intermediate
values through Ray object references without creating a database row per step.

This model is intended for fan-out workloads where database-backed dispatch would
cost more than the individual units of work.

## Requirements

Run the outer task in Ray Core mode. Workflows also have a local fallback for sync
workers and tests, but Ray Job mode does not automatically provide nested Ray
submission.

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head:10001",
    "RUNNER": "ray_core",
}
```

Start a worker normally, or select the cluster explicitly:

```bash
python manage.py django_ray_worker --cluster=ray://ray-head:10001
```

## Dynamic Fan-Out

Define workflow callables at module scope so Ray workers can import them:

```python
from django.tasks import task

from django_ray.workflows import chain, map_step, step


def list_namespaces(cluster_name: str) -> list[str]:
    return load_kubernetes_client().list_namespaces(cluster_name)


def sync_namespace(namespace: str) -> dict:
    return sync_resources_for_namespace(namespace)


def summarize(results: list[dict]) -> dict:
    return {
        "namespaces": len(results),
        "resources": sum(result["resources"] for result in results),
    }


cluster_sync = chain(
    step(list_namespaces),
    map_step(sync_namespace, ray_options={"num_cpus": 0.25}),
    step(summarize),
)


@task(queue_name="sync")
def sync_cluster(cluster_name: str) -> dict:
    return cluster_sync.run(cluster_name)
```

Calling `sync_cluster.enqueue(...)` creates one durable `RayTaskExecution`. Once it
starts, `list_namespaces`, every `sync_namespace` call, and `summarize` are connected
inside Ray. `map_step` resolves the namespace list at the fan-out boundary, submits
one Ray task per item, and gathers the results in input order.

## Chains and Groups

`chain` passes each result as the first argument to the next signature:

```python
pipeline = chain(
    step(download_manifest),
    step(validate_manifest),
    step(apply_manifest),
)
```

`group` sends the same input to every child and returns an ordered result list:

```python
from django_ray.workflows import group

inspect_cluster = chain(
    step(load_cluster),
    group(
        step(inspect_workloads),
        step(inspect_networking),
        step(inspect_storage),
    ),
    step(build_report),
)
```

Groups can contain chains, maps, or other groups.

## Django-Aware and Native Steps

Workflow steps are Ray-native by default and skip Django initialization. This is the
fast path for API clients, transformations, and compute that do not use Django models:

```python
step(sync_namespace)
```

Set `django=True` when a step needs Django's app registry, ORM, settings-dependent
components, or another Django facility:

```python
step(load_cluster_from_database, django=True)
```

Django initialization is guarded by the app registry, so a reused Ray worker does not
initialize Django again for every step.

Use `ray_options` or `Step.with_options()` for Ray scheduling controls:

```python
step(run_inference, ray_options={"num_gpus": 1, "max_retries": 2})

step(transform).with_options(num_cpus=2)
```

Use a named RuntimeEnv profile when a leaf needs different dependencies:

```python
step(run_inference, runtime_env="numpy-2-3")
```

Leaves otherwise inherit the outer durable task's environment. See
[Runtime Environments](runtime-environments.md).

## Local Execution

Signatures run locally when Ray is not initialized. This makes workflow logic easy to
exercise in unit tests:

```python
result = cluster_sync.run("development", use_ray=False)
```

Set `use_ray=True` to fail instead of falling back when Ray is unavailable.

## Durability Semantics

The outer Django task is the durability and retry boundary:

- Internal steps do not create individual Django tasks.
- While a workflow runs, an in-memory Ray coordinator collects node events. The
  outer task writes a bounded progress snapshot to `RayTaskExecution.progress_data`
  at `WORKFLOW_PROGRESS_FLUSH_SECONDS` intervals.
- Progress includes node paths, callable labels, node states, completion counts,
  percent complete, and recent events.
- A leaf failure fails the outer task.
- Retrying the outer task reruns the workflow, including previously completed leaves.
- Cancellation of a Ray Core outer task recursively cancels its child tasks through
  Ray's normal cancellation behavior.
- The final workflow result must satisfy the same result-serialization rules as any
  django-ray task.

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
| `step(callable, *args, django=False, ray_options=None, **kwargs)` | Bind an importable callable as one workflow step |
| `chain(*signatures)` | Run signatures sequentially |
| `group(*signatures)` | Fan out the same input and gather ordered results |
| `map_step(callable_or_signature, ...)` | Fan out over the preceding iterable |
| `signature.run(*args, use_ray=None, **kwargs)` | Execute with Ray when initialized, otherwise locally |
