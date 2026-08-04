# Retry and Error Handling

django-ray does not provide exactly-once execution. Queued work can expire or be
cancelled before application code runs. Once execution begins, a task can complete its
side effect, lose its result before Django records success, and then run again. Task
code must therefore be idempotent.

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
| `EXPIRED` | The queued-wait deadline arrived before execution began |

A retry transitions a failed, lost, cancelled, or expired attempt back to `QUEUED` with `run_after` set to the
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

The Admin controls, operational retry endpoint, and worker retry path use the same
row-locked transition service, so a racing retry request is rejected rather than
applied twice. Success, permanent failure, timeout, LOST recovery, queue expiry,
cancellation, and Ray Job `STOPPED` reconciliation use that same terminal archival
boundary.

Manual Admin retry is state-based, not task-type-based. An authorized operator can use
**Retry task...** on a `FAILED`, `LOST`, or `EXPIRED` execution detail page, or select
multiple eligible rows in the execution list and choose **Retry selected tasks...**.
Both entry points open the same confirmation before calling the lifecycle service. It
shows bounded selected, eligible, skipped, and known-workflow counts but never renders
arguments, results, RuntimeEnv values, errors, or tracebacks. The signed confirmation
is bound to the operator's current Admin session, expires after 15 minutes, and covers
each row's state, attempt, execution generation, and exact workflow run/plan identity.
A changed selection fails closed, and at most 100 failed, lost, or expired rows can be
confirmed together. The row-locked transition rechecks the confirmed state as well as
the attempt and generation, closing the validation-to-lock race. The warning is
intentional: retry creates a new attempt and may repeat external effects.

The Admin does not retry a `SUCCEEDED` execution. A successful row is the authoritative
completed history for that invocation, including its result. If the same business work
must run again because external circumstances changed, enqueue a new task under the
application's authorization, idempotency, reconciliation, and audit policy. A new task
identity makes that new intent explicit and avoids rewriting a successful execution.
The detail page explains this next action instead of presenting a retry button.

Confirmation signatures use Django's `SECRET_KEY` and honor
`SECRET_KEY_FALLBACKS`. During key rotation, retain the previous key as a fallback for
at least the 15-minute confirmation window or expect open confirmations to fail closed.
The confirmation is an Admin-only browser ceremony, not an API token or a package
protocol. An application that exposes retry through an API must perform object/tenant
authorization and implement its own operator confirmation, idempotency, and audit
policy; never accept or forward the Admin confirmation token.

The testproject no longer exposes the arbitrary `POST /api/executions/reset` bulk
adapter. Its HTTP retry surface is only `POST /api/executions/{id}/retry`, fenced by the
attempt and execution generation observed during object authorization. The Admin's
signed, capped multi-row confirmation remains available to staff; removing the sample
bulk API does not remove that operator workflow.

Application APIs that need an explicit outcome should call
`django_ray.lifecycle.request_task_retry()` with the attempt number and execution
generation observed during object authorization. Its stable result distinguishes
accepted, missing, non-retryable, stale-attempt, stale-generation, and stale-workflow
requests without returning task arguments, output, or errors. The compatibility helper
`retry_task()` remains available when a model-or-`None` result is sufficient. Manual
retry archives the terminal attempt, increments both values, and clears only
attempt-local data.
The transactional reads are deliberately projected. One `select_for_update()` query
locks only durable state and workflow-identity fences. After those fences accept the
retry, one explicit read loads the RuntimeEnv, routing, queue-deadline,
result-reference, diagnostic, and workflow-summary fields needed to validate and
archive that attempt while the lock remains held. Rejected requests never transfer
those fields, and the transaction never performs an implicit deferred-field reload.
Task arguments, the durable input reference, application progress, workflow plan body
and selection, completion envelope, and unrelated cancellation diagnostic are not
transferred. They remain unchanged or are reset through named update fields. The model returned by the
compatibility `retry_task()` helper therefore keeps unrelated payload fields deferred;
accessing one can issue a normal query after the helper transaction block exits. An
application-owned outer `atomic()` block may still remain open at that point.
Prefer `request_task_retry()` when a bounded transition result is enough.
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
not discard that terminal channel. The lock projection contains only lifecycle
identity. An accepted queued cancellation reads the exact attempt-archive fields it
needs while retaining the row lock; rejected paths do not transfer them. The transaction
does not implicitly reload deferred execution fields. A running request checks
completion presence in SQL without transferring the completion envelope. Task inputs,
RuntimeEnv, progress, workflow plan, completion content, and unrelated cancellation
payloads remain outside these projections. Cancellation does not guarantee immediate interruption of
already-running synchronous Python code.

The testproject maps cancellation to a fixed bounded HTTP outcome: `202` only for
`ACCEPTED`, `404` for `NOT_FOUND`, and `409` for `ALREADY_REQUESTED`,
`ALREADY_TERMINAL`, `COMPLETION_PENDING`, `STALE_ATTEMPT`, `STALE_GENERATION`, and
`INVALID_STATE`. The response contains only `code`, `message`, `execution_id`, `state`,
`attempt_number`, `execution_generation`, `next_action`, and
`response_max_bytes=4096`, and the complete body is at most 4,096 bytes. It does not
refresh or serialize unrelated execution fields or return task payloads and diagnostics.

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
that succeeded in the previous attempt. django-ray 0.4 does not resume at the failed
node, and a node's progress state is not proof that its external side effect and output
were durably checkpointed.

Use:

- idempotent leaves for external changes;
- a stable operation key plus the external system's idempotency receipt where one is
  available;
- an application-owned transaction/outbox or reconciliation record when a local write
  coordinates an external request;
- separate Django tasks when a side-effecting or expensive stage needs its own durable
  retry, result, cancellation, and audit boundary;
- Ray `max_retries` options only when a leaf-level retry is safe.

An application checkpoint can help task code avoid repeated pure computation, but it
does not make django-ray skip the corresponding workflow node and cannot close the
crash window between an accepted external request and receipt persistence. Treat an
unknown external outcome as reconciliation work rather than automatically replaying or
skipping it. Workflow progress is for observation, not recovery. See
[Ray-Native Workflows](workflows.md#durability-semantics).
The 0.4 Admin retry confirmation therefore states that a workflow starts again at its
entry node. Successful nodes and output previews are not checkpoints, and django-ray
does not yet provide resume-from-failed-node behavior. Treat uncertain external
outcomes as reconciliation work before confirming a full retry. Durable selective
resume requires a separate checkpoint and effect-receipt protocol; it cannot be
inferred safely from the progress graph.

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

The Admin execution list provides filters plus bulk retry and cancellation actions.
Open one execution detail page for its diagnostics, retry guidance, and the
**Retry task...** button when eligible. Directly resetting model fields is an internal
operation and can leave stale Ray handles or result metadata if implemented
incompletely.

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
