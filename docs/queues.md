# Working with Queues

Queues separate workloads by latency, resource needs, or ownership. They affect which
task-manager process claims a durable task; Ray resource options affect where the
claimed work runs.

## Configure and Use Queues

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default", "urgent", "email", "batch"],
    },
}
```

Define tasks in `myapp/tasks.py`:

```python
from django.core.mail import send_mail
from django.tasks import task


@task(queue_name="email")
def send_email(to: str, subject: str, body: str) -> int:
    return send_mail(subject, body, None, [to])


@task(queue_name="batch")
def sum_values(values: list[int]) -> int:
    return sum(values)
```

The decorator supplies the normal queue. A caller may select another configured queue:

```python
from myapp.tasks import send_email

send_email.using(queue_name="urgent").enqueue(
    to="on-call@example.com",
    subject="Service alert",
    body="The error budget threshold was crossed.",
)
```

## Run Queue-Specific Workers

```bash
# One queue
python manage.py django_ray_worker --queue=email --local --concurrency=10

# Several queues
python manage.py django_ray_worker --queue=urgent,default --local --concurrency=20

# Every queue configured on any django-ray backend alias
python manage.py django_ray_worker --all-queues --local
```

`--all-queues` inspects every configured `RayTaskBackend` (including subclasses),
deduplicates their declared queue names, and ignores Celery, Immediate, and other
backend aliases. This makes it safe for a staged multi-backend migration where
`default` is not django-ray. It fails instead of guessing when no django-ray queues can
be enumerated; use `--queue` or `--queues` for an intentionally open-ended backend.
Ray Job mode can preserve a different durable target for each task. Ray Core modes bind
one process to one cluster, so `--all-queues` rejects aliases with different effective
`RAY_ADDRESS` values; run an explicitly selected queue set for each cluster instead.

In production, separate deployments can run the same image with different queue and
concurrency arguments. This is usually more predictable than one worker consuming
latency-sensitive and bulk queues together.

## Priority Semantics

Queues do not carry scheduling priority. Names such as `urgent`, `high-priority`,
`background`, and `batch` have no special claim meaning; they remain workload-isolation
boundaries selected by worker configuration.

Use Django's numeric priority for ordering work consumed by the same worker. Priorities
range from `-100` through `100`, and larger values run sooner:

```python
from myapp.tasks import send_email

send_email.using(queue_name="email", priority=80).enqueue(
    to="on-call@example.com",
    subject="Service alert",
    body="The error budget threshold was crossed.",
)
```

The default is `0`. Tasks at the same priority are FIFO by creation time, including
tasks selected from several queues. Once a delayed task becomes eligible, it joins the
same ordering; retries retain the priority stored at their original enqueue.

Priority cannot make an unselected queue visible to a worker. For strict resource or
latency isolation, dedicate workers to the relevant queue.

## Queue vs Ray Resources

A queue selects a task manager. It does not reserve CPUs, GPUs, memory, or a specific
Ray node. Use Ray scheduling options inside a workflow for that:

```python
from django_ray.workflows import step


def run_inference(features: list[float]) -> float:
    return sum(features)


gpu_inference = step(
    run_inference,
    ray_options={"num_gpus": 1},
)
```

A common deployment pattern is:

- `urgent`: dedicated low-concurrency task managers, warm RuntimeEnv;
- `default`: general application work;
- `batch`: separate task managers with high submission concurrency;
- workflow leaf `ray_options`: actual CPU/GPU requirements enforced by Ray.

## Monitor Queue Depth

The durable model works in a management command, view, or shell:

```python
from django.db.models import Count

from django_ray.models import RayTaskExecution, TaskState

depths = (
    RayTaskExecution.objects.filter(state=TaskState.QUEUED)
    .values("queue_name")
    .annotate(count=Count("id"))
    .order_by("queue_name")
)
for depth in depths:
    print(f"{depth['queue_name']}: {depth['count']}")
```

The package Prometheus renderer exposes allowlisted queue-depth metrics. The bundled
testproject mounts those metrics behind its bearer-authenticated HTTP adapter. Those HTTP
endpoints belong to the example project, not the reusable django-ray package.

## Choosing Queue Boundaries

Create a queue when it needs a different:

- latency objective or backlog policy;
- task-manager concurrency;
- RuntimeEnv backend alias;
- operational owner or deployment;
- maintenance/drain schedule.

Do not create a queue for every function. Each extra queue adds worker and routing
configuration without reducing Ray task overhead.

## See Also

- [Performance](performance.md) for concurrency and granularity
- [Worker Modes](worker-modes.md) for execution topology
- [Configuration](configuration.md) for backend aliases
