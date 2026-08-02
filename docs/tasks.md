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

## Coroutine Tasks

Django coroutine tasks use the same decorator and enqueue API. django-ray awaits them in
sync, Ray Core, and Ray Job execution modes:

```python
import asyncio

from django.tasks import task


@task(queue_name="default")
async def fetch_pair(left: int, right: int) -> dict[str, int]:
    await asyncio.sleep(0)  # Replace with an async client operation.
    return {"left": left, "right": right, "total": left + right}
```

Each coroutine invocation runs in a fresh event loop owned by that task. The loop is
closed before django-ray records success or failure, so event-loop state does not leak
between reused Ray workers. Synchronous callables keep the direct execution path and do
not pay event-loop startup cost.

The low-level synchronous `execute_task()` entrypoint must not be called from a thread
that already has a running event loop. Enqueue the task normally, or move that direct
test/debug call to a synchronous thread. django-ray does not nest or patch event loops.

Do not launch detached work with `asyncio.create_task()` and then return. Child tasks
must be awaited, normally with `asyncio.gather()` or `asyncio.TaskGroup`, before the
durable task finishes. A detached child has no independent Django task record, retry,
result, or cancellation boundary and cannot outlive the per-task loop reliably.

### Django ORM from a coroutine

Use Django's async ORM methods and async iteration inside coroutine tasks:

```python
from django.contrib.auth import get_user_model
from django.tasks import task


@task(queue_name="default")
async def user_email_async(user_id: int) -> dict[str, str]:
    user = await get_user_model().objects.aget(pk=user_id)
    return {"email": user.email}
```

Calling synchronous ORM methods such as `.get()` directly from an async task raises
Django's `SynchronousOnlyOperation`. When an operation has no async ORM equivalent,
move the complete synchronous unit behind `sync_to_async`; use `thread_sensitive=True`
for database work:

```python
from asgiref.sync import sync_to_async
from django.db import transaction


@sync_to_async(thread_sensitive=True)
def update_account(account_id: int) -> None:
    with transaction.atomic():
        # Keep the whole transaction inside this synchronous function.
        Account.objects.filter(pk=account_id).update(active=True)
```

`transaction.atomic()` is a synchronous context manager, so do not spread one database
transaction across `await` points.

The internal `django_ray.runtime.context.get_current_task_context()` API identifies the
durable django-ray execution and remains available across `await` points. It is not
Django's separate `@task(takes_context=True)` `TaskContext` feature; django-ray does not
currently reconstruct that standard task context for workers.

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

django-ray allocates this result ID as a UUIDv4 and commits it under a global database
uniqueness constraint. If a generated candidate already exists, enqueue recomputes
task-ID-bound storage metadata and retries a small bounded number of times before
failing without creating a claimable task. This collision handling prevents ambiguous
lookups; it is not application idempotency or enqueue deduplication. Every successful
call to `enqueue()` still creates a distinct task, so use a separate business operation
key when repeated submissions must collapse into one external effect.

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

## Backend-specific Ray Job routing

Set `RAY_ADDRESS` in a backend alias's `OPTIONS` when Ray Job tasks from that alias
must stay on a specific cluster:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
    },
    "analytics": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["analytics"],
        "OPTIONS": {"RAY_ADDRESS": "ray://analytics-head:10001"},
    },
}

result = build_report.using(
    backend="analytics",
    queue_name="analytics",
).enqueue(report_id=42)
```

The effective target is persisted when the task is enqueued and remains unchanged
through automatic or manual retry. An explicit `OPTIONS["RAY_ADDRESS"]` wins; when it
is omitted, django-ray snapshots `DJANGO_RAY["RAY_ADDRESS"]`. The selected address is
authoritative: Ray's ambient `RAY_API_SERVER_ADDRESS` and `RAY_ADDRESS` variables
cannot replace it during submission, status checks, cancellation, or log retrieval.

This per-task routing applies to Ray Job mode. A Ray Core task manager connects to one
cluster when it starts; use queue isolation and a dedicated `--cluster` worker for each
Ray Core cluster instead of assigning several cluster addresses to one worker.

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
Ray Core tasks are cancelled through their object reference, and Ray Job tasks are
stopped through the Ray Job API. Synchronous tasks cannot be interrupted while Python
is executing. This includes a coroutine running in sync worker mode: its per-task loop
owns the worker thread until the coroutine returns. Sync cancellation and timeout
handling therefore occur only when the worker regains control. Application code should
still use bounded client timeouts and cancellation-safe cleanup.

## Queue expiration

django-ray snapshots a 24-hour queued-wait budget by default. The budget begins at the
later of enqueue/requeue time and `run_after`; at the exact absolute deadline the worker
records terminal `EXPIRED` without submitting to Ray or automatically retrying. Configure
one backend alias with `OPTIONS["QUEUE_TIMEOUT_SECONDS"]` as an integer from `1` through
`2147483647`, or set it explicitly to `None` for intentionally durable work. Unlimited
queues require idempotent tasks, backlog alerts, and an operator drain or discard policy.
Django 6 has no per-call task expiration field, so this is a backend-alias policy rather
than a task argument.

Keep intentionally durable work on a visibly separate backend alias and queue instead of
disabling the safety default for unrelated tasks:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
    },
    "durable": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["durable"],
        "OPTIONS": {"QUEUE_TIMEOUT_SECONDS": None},
    },
}
```

Before applying migration `0016`, stop every old worker and pause every process that can
enqueue work. Old code cannot populate the new policy snapshot, so this upgrade requires
a bounded maintenance window rather than a mixed-version rolling writer deployment.
Preview the first 100 existing queued rows and their proposed deadlines:

```console
python manage.py shell -c "from datetime import timedelta; from django_ray.models import RayTaskExecution, TaskState; rows = RayTaskExecution.objects.filter(state=TaskState.QUEUED).order_by('created_at', 'pk').values_list('pk', 'created_at', 'run_after')[:100]; print([(pk, max(created_at, run_after) + timedelta(days=1) if run_after else created_at + timedelta(days=1)) for pk, created_at, run_after in rows])"
```

Their adopted deadline is one day after `max(created_at, run_after)`. All other existing
executions snapshot the 24-hour policy so any later retry also receives the safe default.
Operators who intentionally require the entire existing backlog to remain unlimited may
set the opt-out only on the migration process:

```console
DJANGO_RAY_EXISTING_QUEUED_UNLIMITED=1 python manage.py migrate django_ray
```

This one-time opt-out affects only rows already queued during migration; configure an
explicit unlimited backend alias for new durable work. Start upgraded workers only after
the migration and policy review, deploy upgraded code to every enqueue producer, and only
then resume enqueue traffic.

Reversing `0016` converts current and archived `EXPIRED` states to `FAILED` before it
drops the queue-policy fields while preserving migration `0015`'s task-ID uniqueness
constraint. Stop upgraded workers first and review every remaining `QUEUED` row before
rollback: older django-ray versions have no deadline fence and can submit that backlog as
soon as they start.

## Reading Current Status

The object returned by `enqueue()` is an enqueue-time snapshot. Fetch it again to see
worker updates:

```python
from django.tasks import TaskResultStatus

from myapp.tasks import send_email

enqueued = send_email.enqueue(
    to="user@example.com",
    subject="Hello",
    body="Your report is ready.",
)

# Later, for example in a polling service or management command.
enqueued.refresh()

if enqueued.status == TaskResultStatus.SUCCESSFUL:
    print(enqueued.return_value)
elif enqueued.status == TaskResultStatus.FAILED:
    print(enqueued.errors)
```

`TaskResult.refresh()` updates the snapshot through its configured backend. It does not
subscribe to changes or wait for completion. When only the ID remains, retrieve a fresh
matching snapshot from the task definition:

```python
current = send_email.get_result(enqueued.id)
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
- [Migrating from Celery](celery-migration.md) for compatibility classification and coexistence
