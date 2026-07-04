# Retry and Error Handling

django-ray provides at-least-once execution. A task can complete its side effect, lose
its result before Django records success, and then run again. Task code must therefore
be idempotent.

## Configure Retries

```python
# settings.py
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 60,
    "RETRY_EXCEPTION_DENYLIST": [
        "builtins.ValueError",
        "myapp.tasks.PermanentTaskError",
    ],
}
```

`MAX_TASK_ATTEMPTS` includes the first attempt. With a 60-second base, retry delays are
60 seconds before attempt two, 120 before attempt three, and 240 before attempt four.

The denylist accepts short built-in names or fully qualified exception paths. Use it
only for failures that cannot improve when repeated.

## A Complete Retry Example

```python
# myapp/tasks.py
from django.tasks import task


class PermanentTaskError(Exception):
    pass


class TemporaryServiceError(Exception):
    pass


@task(queue_name="default")
def reciprocal(value: float) -> float:
    if value == 0:
        raise PermanentTaskError("zero has no reciprocal")
    if value == 13:
        raise TemporaryServiceError("example transient failure")
    return 1 / value
```

With the configuration above, `PermanentTaskError` fails immediately.
`TemporaryServiceError` is retried until it succeeds or exhausts the attempt limit.
Uncaught exception type, message, and traceback are stored on `RayTaskExecution`.

## Durable States

| State | Meaning |
|---|---|
| `QUEUED` | Waiting for its schedule and a task-manager slot |
| `RUNNING` | Claimed and submitted or executing |
| `CANCELLING` | Cancellation requested for running Ray work |
| `CANCELLED` | Cancellation completed |
| `SUCCEEDED` | Result recorded successfully |
| `FAILED` | Permanent failure or retries exhausted |
| `LOST` | No live owner or reconcilable Ray execution was found |

A retry transitions a failed attempt back to `QUEUED` with `run_after` set to the
calculated backoff time.

## Make Side Effects Idempotent

Use an application-level operation key with a uniqueness constraint. The example below
contains both required files.

```python
# myapp/models.py
from django.db import models


class CompletedOperation(models.Model):
    key = models.CharField(max_length=255, unique=True)
    result = models.JSONField()
```

```python
# myapp/tasks.py
from django.db import transaction
from django.tasks import task

from myapp.models import CompletedOperation


@task(queue_name="default")
def record_total(operation_key: str, values: list[int]) -> dict[str, int]:
    with transaction.atomic():
        completed, created = CompletedOperation.objects.get_or_create(
            key=operation_key,
            defaults={"result": {"total": sum(values)}},
        )
    return {
        "total": int(completed.result["total"]),
        "created": int(created),
    }
```

For an external payment or webhook, pass the same operation key to the remote service
and rely on that service's idempotency guarantee. A local database record alone cannot
atomically cover a remote side effect.

## Workflow Retries

The outer Django task is the retry boundary for a Ray-native workflow. Internal leaves
do not have independent durable retry rows. Retrying the outer task may repeat leaves
that succeeded in the previous attempt.

Use:

- idempotent leaves for external changes;
- explicit application checkpoints for expensive completed stages;
- separate Django tasks when each child truly requires its own retry lifecycle;
- Ray `max_retries` options only when a leaf-level retry is safe.

Workflow progress is for observation, not recovery. See
[Ray-Native Workflows](workflows.md#durability-semantics).

## Lost and Stuck Work

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head.example:10001",
    "STUCK_TASK_TIMEOUT_SECONDS": 300,
    "WORKER_LEASE_SECONDS": 120,
    "WORKER_HEARTBEAT_SECONDS": 30,
    "TASK_MONITOR_HEARTBEAT_SECONDS": 15,
}
```

Worker leases detect dead task managers. Task-monitor heartbeats show that a live
manager is still reconciling active work. For persisted Ray Job handles, another
manager first tries to adopt or reconcile the existing job. Work is marked `LOST` only
after no live owner or recoverable execution remains past the timeout.

## Inspect Failures

```python
from django_ray.models import RayTaskExecution, TaskState

failed = RayTaskExecution.objects.filter(state=TaskState.FAILED).order_by("-finished_at")
for execution in failed:
    print(execution.task_id)
    print(execution.error_message)
    print(execution.error_traceback)
    print(execution.attempt_number)
```

The admin page `/admin/django_ray/raytaskexecution/` provides filters, tracebacks,
manual retry, and cancellation actions. Prefer the admin action for manual retries;
directly resetting model fields is an internal operation and can leave stale Ray
handles or result metadata if implemented incompletely.

## Denylist Guidance

Usually permanent:

- validation or schema errors;
- missing immutable input;
- permission denial that requires human intervention;
- unsupported file or protocol versions.

Usually retryable:

- timeouts and temporary network failures;
- rate limits with a suitable backoff;
- transient database or object-store availability;
- Ray node loss.

Do not denylist broad classes such as every `TypeError` unless application behavior
makes that classification intentional.

## See Also

- [Tasks](tasks.md) for task definitions and result lookup
- [Performance](performance.md) for choosing durable boundaries
- [Operator Runbook](runbook.md) for recovery procedures
