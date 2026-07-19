# Defining Tasks

django-ray implements a backend for Django 6's Tasks framework. Definition and
enqueueing use Django's public API; execution tracking and Ray integration are
provided by django-ray.

## A Complete Task Module

```python
# myapp/tasks.py
from django.tasks import task


@task(queue_name="default")
def send_email(to: str, subject: str, body: str) -> dict[str, str]:
    # Replace this print with your email provider call.
    print(f"To: {to}\nSubject: {subject}\n\n{body}")
    return {"sent_to": to, "subject": subject}


@task(queue_name="default")
def square(value: int) -> int:
    return value * value
```

The example is runnable as written. Comments explicitly mark the one integration point
an application would replace.

## Enqueueing

```python
from myapp.tasks import send_email

enqueued = send_email.enqueue(
    to="user@example.com",
    subject="Hello",
    body="Your report is ready.",
)
task_id = enqueued.id
```

Select another configured queue at enqueue time:

```python
enqueued = send_email.using(queue_name="email").enqueue(
    to="user@example.com",
    subject="Hello",
    body="Your report is ready.",
)
```

The `email` queue must appear in the selected backend's `TASKS[alias]["QUEUES"]`
configuration and a worker must consume it.

## Priority

Django priorities are whole numbers from `-100` through `100`. Larger values run
sooner; `0` is the default. Select a priority for one enqueue with Django's standard
`.using()` API:

```python
from myapp.tasks import send_email

enqueued = send_email.using(priority=80).enqueue(
    to="on-call@example.com",
    subject="Service alert",
    body="The error budget threshold was crossed.",
)
```

django-ray stores the selected priority with the execution. Eligible tasks with a
higher priority are claimed first, and tasks at the same priority remain FIFO by
creation time. Delayed tasks and retries retain their original priority. Values outside
the supported range, and non-whole-number values, are rejected by Django before enqueue.

## Per-task timeouts

Set `TIMEOUT_SECONDS` in a Ray backend's `OPTIONS` to apply a positive timeout to every
task that uses that backend. Use separate backend aliases when tasks need different
deadlines, then select the alias with Django's standard `.using(backend=...)` API:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"RAY_ADDRESS": "auto"},
    },
    "quick": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"RAY_ADDRESS": "auto", "TIMEOUT_SECONDS": 30},
    },
}

enqueued = send_email.using(backend="quick").enqueue(
    to="user@example.com",
    subject="Hello",
    body="Your report is ready.",
)
```

`TIMEOUT_SECONDS` must be a positive integer; invalid values fail during backend
initialization. The timeout is checked by the worker's periodic reconciliation loop,
so enforcement is approximate and can lag by one worker iteration. A timed-out task is
marked `FAILED` permanently; it does not automatically consume a retry attempt, but
operators can retry it through the admin or operational API after reviewing the cause.
Ray Core tasks are cancelled through their object reference, Ray Job tasks are stopped
through the Ray Job API, and synchronous tasks cannot be interrupted while Python is
executing; sync timeout handling occurs when the worker regains control.

## Reading Current Status

The object returned by `enqueue()` is an enqueue-time snapshot. Fetch it again to see
worker updates:

```python
from django.tasks import TaskResultStatus, task_backends

current = task_backends["default"].get_result(task_id)

if current.status == TaskResultStatus.SUCCESSFUL:
    print(current.return_value)
elif current.status == TaskResultStatus.FAILED:
    print(current.errors)
```

For operations, graph progress, attempts, and Ray identifiers, query
`RayTaskExecution` or use the observability helpers described in
[API and UI integration](reference/api.md).

## Arguments and Results

Arguments and return values must be JSON-serializable. Prefer strings, numbers,
booleans, `None`, lists, and dictionaries with string keys.

```python
# myapp/tasks.py
from typing import Any

from django.tasks import task


@task(queue_name="default")
def summarize(
    account_id: int,
    values: list[float],
    options: dict[str, Any],
) -> dict[str, float | int]:
    scale = float(options.get("scale", 1.0))
    scaled = [value * scale for value in values]
    return {
        "account_id": account_id,
        "count": len(scaled),
        "total": sum(scaled),
    }
```

Do not pass Django model instances, querysets, open files, functions, or custom class
instances. Pass stable identifiers and load database state inside the task:

```python
from django.contrib.auth import get_user_model
from django.tasks import task


@task(queue_name="default")
def user_email(user_id: int) -> dict[str, str]:
    user = get_user_model().objects.get(pk=user_id)
    return {"email": user.email}
```

Occasionally oversized JSON arguments can use django-ray's opt-in
[Durable Input Storage](reference/input-storage.md). For independently managed large
datasets, pass an application-owned S3, GCS, or database URI instead of copying the
dataset into the task envelope. Large results should use django-ray's configured
[Result Storage](reference/result-storage.md).

## Errors and Retries

An uncaught exception marks the attempt as failed and records its type, message, and
traceback. Retry behavior is controlled by `MAX_TASK_ATTEMPTS`,
`RETRY_BACKOFF_SECONDS`, and `RETRY_EXCEPTION_DENYLIST`.

```python
from django.tasks import task


class InvalidInvoice(ValueError):
    pass


@task(queue_name="default")
def calculate_invoice(subtotal: float, tax_rate: float) -> float:
    if subtotal < 0:
        raise InvalidInvoice("subtotal must not be negative")
    return round(subtotal * (1 + tax_rate), 2)
```

Add `"myapp.tasks.InvalidInvoice"` to `RETRY_EXCEPTION_DENYLIST` when repeating that
error can never succeed. Make tasks idempotent because a worker or cluster failure can
cause an attempt to be retried.

## Batch or Fan Out?

For small work, one batched Django task usually wins because each durable task requires
a database claim, submission, state transitions, and result write:

```python
from django.tasks import task


@task(queue_name="default")
def square_batch(values: list[int]) -> list[int]:
    return [value * value for value in values]
```

For independent, expensive items, submit Ray work inside one durable task. Functions
passed to `parallel_map()` must be module-level so Python can serialize them:

```python
# myapp/tasks.py
from django.tasks import task

from django_ray.runtime.distributed import parallel_map


def expensive_square(value: int) -> int:
    total = 0
    for number in range(500_000):
        total = (total + value * number) % 1_000_003
    return total


@task(queue_name="default")
def square_in_parallel(values: list[int]) -> list[int]:
    return parallel_map(
        expensive_square,
        values,
        num_cpus=0.25,
        max_concurrency=16,
    )
```

`parallel_map()` is convenient for one fan-out. Use
[Ray-native workflows](workflows.md) when work has multiple dependent stages, needs a
graph for a UI, or should report leaf progress without a database row per leaf.

Avoid enqueueing another durable Django task from every item solely to express a chain
or group. That maximizes database round trips and makes the outer task finish before
its children. Use separate Django tasks only when each child needs its own independent
retry, cancellation, audit record, or schedule.

## See Also

- [Performance](performance.md) for a practical granularity checklist
- [Queues](queues.md) for workload isolation
- [Retry and Error Handling](retry.md) for recovery behavior
- [Ray-Native Workflows](workflows.md) for dependent fan-out
