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

Attempt numbers are one-based and increase for both automatic and manual retries;
manual retry never resets the counter or consumes a separate budget. Each terminal
attempt is copied to `TaskAttempt`, preserving its state, result reference, and
failure diagnostics while the current execution row is prepared for the next run.
When that run already owns an accepted canonical schema-v3 terminal progress summary,
the same at-most-16-KiB JSON value is archived on `TaskAttempt` before the current
summary is cleared. When lifecycle reconciliation wins first, it derives a terminal
envelope from the last accepted running summary under the same row lock. The envelope
records the authoritative outer outcome and detail expiry. Success marks every
discovered node complete; interrupted outcomes retain the last observed node counters.
Complete legacy graphs and topology/detail rows are never copied; malformed,
noncanonical, or missing summaries do not block the lifecycle transition.
Producer publications reserve one final summary revision so a conflicting terminal
report can be replaced by the authoritative row-locked task outcome.

The admin action, operational retry endpoint, and worker retry path use the same
row-locked transition service, so a racing retry request is rejected rather than
applied twice. Success, permanent failure, timeout, LOST recovery, cancellation, and
Ray Job `STOPPED` reconciliation use that same terminal archival boundary.

Application APIs should call `django_ray.lifecycle.retry_task()` with the attempt number
and execution generation observed during object authorization. Manual retry archives the
terminal attempt, increments both values, and clears only attempt-local data.
After the state and identity fences pass, the same row lock verifies the persisted
RuntimeEnv snapshot before any archival or reset. An identified missing, malformed,
unsupported, unknown-key, authentication-failed, noncanonical, or hash-mismatched
snapshot raises the redaction-safe
`RuntimeEnvSnapshotError`; application endpoints should map it to a fixed conflict
response, while bulk operations should skip that row and continue. Automatic retry
records the current failure terminally but does not create a replacement attempt when
the same preflight fails. Encryption mode changes do not rewrite a task's snapshot:
every retry of an encrypted row still needs the key named by its envelope, even when
new writes have returned to plaintext mode. Keep retired dedicated keys or Django
`SECRET_KEY_FALLBACKS` for at least as long as those rows remain retryable.
`django_ray.lifecycle.request_task_cancellation()` provides the matching
authorization-neutral cancellation service: it immediately archives queued work as
`CANCELLED`, or moves running work to `CANCELLING` for worker-owned, best-effort backend
interruption. Its stable result distinguishes accepted, duplicate, terminal, missing,
stale-attempt, stale-generation, completion-pending, and invalid-state requests. A
running row whose Ray Job entrypoint has already published its durable completion
returns `COMPLETION_PENDING` and remains owned by reconciliation. Cancellation does
not discard that terminal channel. It does not guarantee immediate interruption of
already-running synchronous Python code.

Tasks with durable external inputs keep the same immutable `input_reference` across
automatic and manual retries; a retry does not upload a replacement. Corrupt,
unauthorized, or unsupported input envelopes fail before user code and are marked
non-retryable. Retrieval/storage errors follow normal retry policy because an outage may
be transient. Restore a missing object or correct storage configuration before a manual
retry. Purged historical inputs cannot be retried unless the same content is restored
or reactivated; choose cleanup retention accordingly.

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
after no live owner or recoverable execution remains past the timeout. A Ray Job whose
status remains `UNKNOWN` receives an exact best-effort stop request and is left `LOST`
without automatic retry; verify the remote job is quiescent before using a manual
retry. The same rule applies to an expired malformed or invalid completion envelope
while Ray still reports `PENDING` or `RUNNING`; only terminal Ray states can use the
normal failure/retry policy.

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

Usually retryable when routed through the normal exception handler:

- application-level timeouts and temporary network failures;
- rate limits with a suitable backoff;
- transient database or object-store availability;
- Ray node loss.

Do not denylist broad classes such as every `TypeError` unless application behavior
makes that classification intentional.

The worker's configured per-task timeout is different: once the reconciliation loop
marks a running task timed out, it is a terminal `FAILED` state. Retry it explicitly
after investigating the timeout.

## See Also

- [Tasks](tasks.md) for task definitions and result lookup
- [Performance](performance.md) for choosing durable boundaries
- [Operator Runbook](runbook.md) for recovery procedures
