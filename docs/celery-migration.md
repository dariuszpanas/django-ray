# Migrating from Celery

django-ray is a backend for Django's Tasks framework. It stores durable task state in
the Django database and uses Ray for execution. It is **not** a Celery worker, broker
consumer, message-protocol adapter, result-backend adapter, or drop-in replacement.

Do not point django-ray at a Celery broker or assume that changing a decorator will
preserve delivery semantics. Existing Celery messages, task IDs, result records, beat
entries, revokes, and Canvas state cannot be imported or consumed by django-ray. A
safe migration classifies each workload by the behavior it relies on, runs both
systems during coexistence, and removes Celery only after its producers and state have
been drained.

## Decide whether this guide fits

The best first django-ray cohort is made of coarse, idempotent tasks that:

- already run on Python 3.12 through 3.14 and Django 6.0 or newer;
- accept and return JSON-compatible values;
- need an earliest-run time rather than a recurring schedule or expiry;
- can use exception-driven, deployment-wide retry policy;
- do not require broker acknowledgements, dead-letter exchanges, remote control, or
  independently durable Canvas children; and
- benefit from Ray execution, Ray-native fan-out, or RuntimeEnv isolation.

Keep a workload on Celery, at least initially, when its correctness depends on
Celery-specific broker delivery, beat, rich routing, rate limiting, worker pools,
signals, arbitrary serializers, or per-child Canvas lifecycle.

See [Compatibility and Version Policy](compatibility.md), [Getting Started](getting-started.md),
and [Performance](performance.md) before provisioning a migration environment.

## Runtime prerequisites

django-ray's current supported boundary is:

| Component | Migration requirement |
|---|---|
| Python | 3.12, 3.13, or 3.14 |
| Django | 6.0 or a newer compatible release, including Django's Tasks framework |
| Database | PostgreSQL for production; SQLite is suitable for a local walkthrough |
| Task manager | At least one `python manage.py django_ray_worker ...` process for each selected queue |
| Ray | Ray 2.53.0 or a newer compatible release; align Ray and Python versions across task managers and Ray nodes |
| Production platform | Linux is recommended; Kubernetes deployments should use the documented KubeRay boundary |

Install the package, add `django_ray` to `INSTALLED_APPS`, configure at least one
`TASKS` backend, configure `DJANGO_RAY`, and apply the package migrations before
starting task managers. The complete setup is in [Getting Started](getting-started.md);
execution topology is in [Worker Modes](worker-modes.md), and production deployment is
in [Kubernetes Deployment](deployment/kubernetes.md).

Celery and django-ray may use the same Django application during coexistence, but they
remain separate delivery systems:

- Celery producers continue to publish Celery messages to the broker.
- django-ray producers create `RayTaskExecution` rows through Django's task API.
- Celery workers cannot execute a django-ray row.
- django-ray task managers cannot consume a Celery message.

Keep both sets of workers, scheduler services, monitoring, and result storage running
until the corresponding workloads have moved and drained.

## Compatibility matrix

The classifications mean:

- **Direct**: a small syntax change covers the common case; still test the application
  side effect and result contract.
- **Partial**: django-ray covers the broad need with materially different limits or
  operations.
- **Semantic rewrite**: redesign the policy or durability boundary before moving.
- **Unsupported / no equivalent**: retain Celery or provide another application or
  platform service.

### Definition, dispatch, and scheduling

| Celery pattern | Classification | django-ray migration |
|---|---|---|
| [`@shared_task`](https://docs.celeryq.dev/en/stable/userguide/tasks.html) on a plain function | **Direct** | Replace it with Django's `@task` on a module-level function. Celery decorator options, bound `self`, and custom bases do not carry over. See [Defining Tasks](tasks.md#a-complete-task-module). |
| [`.delay(*args, **kwargs)`](https://docs.celeryq.dev/en/stable/userguide/calling.html) | **Direct** for a basic call | Use `.enqueue(*args, **kwargs)`. The returned Django `TaskResult` is an enqueue-time snapshot, not Celery's `AsyncResult`. See [Enqueueing](tasks.md#enqueueing) and [Reading Current Status](tasks.md#reading-current-status). |
| [`.apply_async(...)`](https://docs.celeryq.dev/en/stable/userguide/calling.html) | **Partial** | Use `.using(queue_name=..., priority=..., run_after=..., backend=...).enqueue(...)` for the supported subset. There is no single-call equivalent for `countdown`, `expires`, `task_id`, serializers, callbacks, errbacks, publish retry, exchange, or routing key. See [Tasks](tasks.md) and [Queues](queues.md). |
| [Queue and priority options](https://docs.celeryq.dev/en/stable/userguide/routing.html) | **Partial** | Named queues select which task-manager process may claim a database row. Numeric priorities range from `-100` through `100`; larger values are claimed first among eligible rows, with FIFO ties. Queues are not exchanges or routing keys. See [Priority Semantics](queues.md#priority-semantics). |
| [`eta` and `countdown`](https://docs.celeryq.dev/en/stable/userguide/calling.html) | **Partial** | Compute an aware `datetime` and pass it as `run_after`. It is the earliest eligibility time, not an exact deadline. There is no countdown keyword; compute the timestamp in application code. See [Deferred enqueue](#deferred-enqueue). |
| [`expires`](https://docs.celeryq.dev/en/stable/userguide/calling.html) | **Unsupported / no equivalent** | django-ray does not expire a queued task automatically. Implement an application deadline check inside an idempotent task, cancel through an authorized operational surface, or keep the workload on Celery. The supported scheduling and timeout controls are in [Defining Tasks](tasks.md). |
| Celery's Django [`delay_on_commit()`](https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html) | **Partial** | Wrap `.enqueue()` in Django's `transaction.on_commit()`. django-ray has no `delay_on_commit()` shortcut, and the callback cannot return a task result before commit. See [Transaction-safe enqueue](#transaction-safe-enqueue). |
| [Backend, queue, and task routing rules](https://docs.celeryq.dev/en/stable/userguide/routing.html) | **Semantic rewrite** | Select a configured Django Tasks backend alias and queue explicitly. A backend alias may select a timeout or RuntimeEnv profile; it does not recreate Celery routers, exchanges, headers, or wildcard routes. See [Django Tasks Configuration](configuration.md#django-tasks-configuration). |

### Reliability, retries, and results

| Celery pattern | Classification | django-ray migration |
|---|---|---|
| [`self.retry()` and `autoretry_for`](https://docs.celeryq.dev/en/stable/userguide/tasks.html) | **Semantic rewrite** | django-ray retries an uncaught exception according to `MAX_TASK_ATTEMPTS`, `RETRY_BACKOFF_SECONDS`, and `RETRY_EXCEPTION_DENYLIST`. There is no bound task object, in-task reschedule call, exception allowlist, or per-call retry options. Model permanent exception types explicitly and test the deployment-wide policy. See [Retry and Error Handling](retry.md). |
| [Acknowledgement, redelivery, and worker-loss policy](https://docs.celeryq.dev/en/stable/userguide/tasks.html) | **Semantic rewrite** | django-ray uses database claims, worker leases, persisted Ray handles, and recovery rather than broker ACK/reject/requeue. It provides at-least-once execution, so a side effect may happen before a lost success record causes another attempt. Use application idempotency keys. See [Delivery Semantics](architecture.md#delivery-semantics). |
| [Idempotent task behavior](https://docs.celeryq.dev/en/stable/userguide/tasks.html) | **Partial** | Preserve or add an application idempotency key at the system that owns each side effect, then retest the different failure windows. A final django-ray success does not prove an earlier attempt produced no effects. See [Delivery Semantics](architecture.md#delivery-semantics) and [Retry and Error Handling](retry.md). |
| [Dead-letter exchange or rejected-message workflow](https://docs.celeryq.dev/en/stable/userguide/configuration.html) | **Unsupported / no equivalent** | There is no broker reject or DLX path. Query durable failed/lost attempts, alert through application observability, and implement an authorized replay policy, or retain Celery for the workload. See [Durable States](retry.md#durable-states). |
| [Soft and hard time limits](https://docs.celeryq.dev/en/stable/userguide/workers.html) | **Partial** | A backend alias may set one positive `TIMEOUT_SECONDS`. Enforcement is periodic and approximate; Ray stop is best effort, sync execution cannot be interrupted, and a timeout is a permanent failure unless an operator retries it. There is no Celery soft-limit exception. See [Per-task timeouts](tasks.md#per-task-timeouts). |
| [Celery result backends and `AsyncResult`](https://docs.celeryq.dev/en/stable/reference/celery.result.html) | **Partial** | Django `TaskResult` exposes current task status and JSON-compatible results after refresh. django-ray stores state in its models and may externalize oversized JSON. It does not implement Celery result graphs, task-state events, `get()` semantics, or existing result IDs. See [Reading Current Status](tasks.md#reading-current-status) and [Result Storage](reference/result-storage.md). |
| Revoke or terminate through [Celery remote control](https://docs.celeryq.dev/en/stable/userguide/workers.html) | **Partial** | django-ray can cancel queued work and request best-effort cancellation of running Ray work through the Django admin or an authorized application adapter. This is durable task state, not a broadcast revoke list, and it has no terminate-by-signal equivalent. See [API Reference](reference/api.md) and [Retry and Error Handling](retry.md). |
| [Arbitrary task IDs](https://docs.celeryq.dev/en/stable/userguide/calling.html) or importing Celery result records | **Unsupported / no equivalent** | django-ray assigns a new task ID when `.enqueue()` creates a row. Do not copy Celery task IDs, messages, or result records into django-ray tables. Preserve old Celery records under their existing retention and audit policy. See [Enqueueing](tasks.md#enqueueing). |

### Workflows, operations, and platform behavior

| Celery pattern | Classification | django-ray migration |
|---|---|---|
| [Canvas chains, groups, and chords](https://docs.celeryq.dev/en/stable/userguide/canvas.html) | **Semantic rewrite** | django-ray `chain`, `group`, and `map_step` can express related compute shapes, but the whole workflow is one durable outer task. Leaves have no independent Django row, task ID, schedule, queue, retry, result, or cancellation boundary. See [Workflow durability](#canvas-is-not-a-durability-mapping) and [Durability Semantics](workflows.md#durability-semantics). |
| [Celery beat and periodic schedules](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html) | **Unsupported / no equivalent** | `run_after` schedules one enqueue once. Keep Celery beat for remaining periodic workloads or use another scheduler that calls `.enqueue()` after your authorization and overlap policy run. Do not remove beat while any schedule still targets Celery. See [Deferred enqueue](#deferred-enqueue). |
| [Exchanges, routing keys, topic/fanout, and broadcast routing](https://docs.celeryq.dev/en/stable/userguide/routing.html) | **Unsupported / no equivalent** | django-ray queues are database-backed workload partitions consumed by named task managers. They do not implement broker exchange or binding semantics. See [Working with Queues](queues.md). |
| [Task rate limits](https://docs.celeryq.dev/en/stable/userguide/workers.html) | **Unsupported / no equivalent** | Task-manager concurrency and Ray resource requests bound concurrency, not requests per second. Keep an application/client token bucket or external service quota, or retain Celery when Celery's rate-limit behavior is required. See [Performance](performance.md). |
| [Signals and custom `Task` bases](https://docs.celeryq.dev/en/stable/userguide/signals.html) | **Unsupported / no equivalent** | Move behavior into explicit callable wrappers, domain services, Django signals where appropriate, and observability adapters. django-ray does not invoke Celery lifecycle signals or custom task hooks. See [Defining Tasks](tasks.md). |
| Worker pool selection and [prefetch behavior](https://docs.celeryq.dev/en/stable/userguide/workers.html) | **Semantic rewrite** | Size django-ray task-manager concurrency and Ray resources for the workload. There is no broker prefetch multiplier or Celery pool selection to copy. See [Worker Modes](worker-modes.md) and [Performance](performance.md). |
| [JSON, pickle, YAML, or custom serializers](https://docs.celeryq.dev/en/stable/userguide/calling.html) | **Unsupported / no equivalent** beyond JSON | Durable django-ray task arguments and final results must be JSON-compatible. Pass stable IDs or application-owned object-storage references for other data. Do not replace this boundary with pickle. See [Arguments and Results](tasks.md#arguments-and-results). |
| [Celery events, inspect/control, and Flower](https://docs.celeryq.dev/en/stable/userguide/monitoring.html) | **Semantic rewrite** | Use durable task/attempt rows, Django admin, versioned observability services, bounded Prometheus metrics, and optional Ray live state/logs. There is no Celery event stream, worker broadcast control, or Flower protocol. See [Observability Services](observability.md). |
| [Queue- or header-based tenant routing](https://docs.celeryq.dev/en/stable/userguide/routing.html) | **Semantic rewrite** | Authenticate before enqueue, authorize every task/read operation in the application, and use separate Ray clusters for mutually untrusted tenants. Queue names and RuntimeEnv profiles organize work; they are not security boundaries. See [Security Boundary](observability.md#security-boundary) and [Runtime Environments](runtime-environments.md). |

## Copyable migration recipes

### Basic task and enqueue

A simple Celery task:

```python
# myapp/celery_tasks.py
from celery import shared_task


@shared_task
def add_numbers(left: int, right: int) -> int:
    return left + right
```

becomes a Django task:

```python
# myapp/tasks.py
from django.tasks import task


@task(queue_name="default")
def add_numbers(left: int, right: int) -> int:
    return left + right
```

Enqueue it with:

```python
from myapp.tasks import add_numbers

enqueued = add_numbers.enqueue(20, 22)
task_id = enqueued.id
```

This recipe covers only plain function dispatch. Revisit the matrix for every Celery
decorator option and every `.apply_async()` option the original call used. The
django-ray task and enqueue contracts are documented in [Defining Tasks](tasks.md).

### Transaction-safe enqueue

Enqueue only after the outer database transaction commits. This prevents a task from
running before the row it needs is visible.

```python
# myapp/models.py
from django.db import models


class Report(models.Model):
    title = models.CharField(max_length=200)
```

```python
# myapp/tasks.py
from django.tasks import task

from myapp.models import Report


@task(queue_name="default")
def index_report(report_id: int) -> dict[str, int | str]:
    report = Report.objects.only("title").get(pk=report_id)
    return {"report_id": report.pk, "title": report.title}
```

```python
# myapp/services.py
from functools import partial

from django.db import transaction

from myapp.models import Report
from myapp.tasks import index_report


def create_report_and_enqueue_index(title: str) -> int:
    with transaction.atomic():
        report = Report.objects.create(title=title)
        transaction.on_commit(partial(index_report.enqueue, report.pk))
    return report.pk
```

The callback is discarded if the transaction rolls back. Because `.enqueue()` runs
later, `create_report_and_enqueue_index()` returns the domain object's ID, not a
`TaskResult`. If the caller must durably know the eventual task ID, model an
application-owned outbox/correlation record instead of pretending a pre-commit task
ID already exists.

This is the same transaction boundary highlighted in Celery's
[Django integration guide](https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html);
the django-ray side uses Django's normal [enqueue API](tasks.md#enqueueing).

### Deferred enqueue

Celery `countdown=600` translates to an aware earliest-run timestamp:

```python
# myapp/services.py
from datetime import timedelta

from django.utils import timezone

from myapp.tasks import index_report


def enqueue_index_in_ten_minutes(report_id: int):
    return index_report.using(
        run_after=timezone.now() + timedelta(minutes=10),
    ).enqueue(report_id)
```

With `USE_TZ=True`, Django rejects a naive `run_after`. The timestamp makes the row
eligible; queue depth, worker capacity, and Ray capacity can make actual start later.
There is no `expires` equivalent, so put a business deadline in task data and check it
inside the task when stale work must become a safe no-op.

django-ray advertises Django's deferred-task capability and persists `run_after`; see
[Defining Tasks](tasks.md) and [Retry and Error Handling](retry.md).

### Queue, priority, and backend selection

Configure backend aliases for operationally meaningful differences:

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default", "maintenance"],
        "OPTIONS": {"RAY_ADDRESS": "auto"},
    },
    "quick": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["maintenance"],
        "OPTIONS": {
            "RAY_ADDRESS": "auto",
            "TIMEOUT_SECONDS": 30,
        },
    },
}

DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "RUNNER": "ray_core",
}
```

Select the complete dispatch policy before enqueue:

```python
from datetime import timedelta

from django.utils import timezone

from myapp.tasks import index_report

enqueued = index_report.using(
    backend="quick",
    queue_name="maintenance",
    priority=40,
    run_after=timezone.now() + timedelta(minutes=5),
).enqueue(123)
```

The queue must be listed on the selected alias and consumed by a task manager. Priority
orders eligible rows observed by that task manager; it is not an exchange-wide
guarantee. Backend aliases, queue behavior, and timeout limits are documented in
[Configuration](configuration.md#django-tasks-configuration),
[Queues](queues.md), and [Per-task timeouts](tasks.md#per-task-timeouts).

### Refresh a Django `TaskResult`

`enqueue()` returns a snapshot. Refresh that object before reading worker updates:

```python
from django.tasks import TaskResultStatus

from myapp.tasks import add_numbers

result = add_numbers.enqueue(20, 22)

# Later, for example in a polling service or management command:
result.refresh()

if result.status == TaskResultStatus.SUCCESSFUL:
    total = result.return_value
elif result.status == TaskResultStatus.FAILED:
    errors = result.errors
```

`refresh()` performs a database-backed result read through the configured backend. It
does not subscribe to updates or block until completion. If only the task ID remains,
`add_numbers.get_result(task_id)` returns a fresh matching result. See
[Reading Current Status](tasks.md#reading-current-status).

### Configure retrievable oversized results

The default `digest` backend records metadata for an oversized result but cannot
restore its value. Select filesystem, S3, or GCS storage before migrating a Celery
workload whose consumers need large results.

For S3:

```bash
python -m pip install "django-ray[s3]"
```

```python
# settings.py
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head.example:10001",
    "MAX_RESULT_SIZE_BYTES": 1024 * 1024,
    "RESULT_STORAGE_BACKEND": "s3",
    "RESULT_STORAGE_S3_BUCKET": "django-ray-results",
    "RESULT_STORAGE_S3_PREFIX": "production/results",
    "RESULT_STORAGE_S3_REGION": "us-west-2",
}
```

The task manager and every process that refreshes results need compatible storage
configuration and credentials. A successful result larger than the threshold is
stored externally; `TaskResult.refresh()` or `get_result()` rehydrates it when storage
is reachable. A storage write failure falls back to digest metadata, while a later
read failure leaves the task successful but its return value unavailable.

For a single host, or for a deployment with an explicitly shared volume, the
filesystem backend is also retrievable. See [Result Storage Reference](reference/result-storage.md)
for filesystem, S3, GCS, reference formats, credentials, and failure behavior.

## Canvas is not a durability mapping

Celery Canvas composes signatures that normally become distinct task messages and
results. django-ray workflows compose Ray work inside one durable outer
`RayTaskExecution`.

The shapes may look similar:

| Celery shape | Possible django-ray compute shape | Durability warning |
|---|---|---|
| `chain(a.s(), b.s())` | `chain(step(a), step(b))` | Both leaves share one outer retry and result. |
| `group(a.s(), b.s())` | `group(step(a), step(b))` | Leaves have no separate task IDs, queues, or result rows. |
| `chord(group(...), callback.s())` | `chain(group(...), step(callback))` | This is not a Celery chord protocol or independently durable callback. |
| Dynamic group/map | `map_step(...)` with explicit admission limits | The complete map belongs to one outer attempt; choose bounded fan-out deliberately. |

For every Canvas workflow, ask which boundary must survive a process or cluster
failure:

- If the complete workflow may retry as one idempotent unit, a django-ray workflow may
  fit.
- If each stage or child needs a durable ID, schedule, queue, retry budget, audit row,
  result, or cancellation, do not translate it into leaves. Keep Celery or redesign the
  stages as independently durable application operations.
- If a successful child must never run again after another child fails, django-ray's
  current outer-task retry is not equivalent. Add application-owned checkpoints or
  retain the Celery design.
- If the callback depends on Celery chord result-backend behavior, treat it as a new
  design, not a syntax conversion.

The authoritative django-ray boundary is in
[Ray-Native Workflows](workflows.md#durability-semantics). Celery's distinct primitives
and result requirements are documented in the official
[Canvas guide](https://docs.celeryq.dev/en/stable/userguide/canvas.html).

## Tenant and authorization boundary

Treat task routing and task authorization as separate decisions:

- Authenticate and authorize the producer before calling `.enqueue()`.
- Map request data to an allowlisted task, backend alias, queue, priority, and
  application-owned resource ID. Do not accept arbitrary callable paths, RuntimeEnv
  definitions, Ray addresses, or backend aliases from an untrusted request.
- Authorize cancel, retry, result, attempt-history, workflow, and live-log access
  against the application object that owns the task. General observability helpers are
  authorization-neutral, while bounded workflow reads require an authorizer on every
  call.
- Protect the database, Django admin, result storage, metrics route, Ray dashboard,
  and Ray State API independently. Redaction is defense in depth, not access control.
- Use separate Ray clusters, credentials, and appropriate application/data-plane
  isolation for mutually untrusted tenants. A queue or RuntimeEnv profile is an
  operational organization mechanism, not a sandbox.

The package boundary and an authorized adapter example are in
[Observability Services](observability.md#security-boundary) and
[API Reference](reference/api.md). RuntimeEnv storage and trust limitations are in
[Runtime Environments](runtime-environments.md).

## Inventory semantics, not task count

Before changing code, inventory every workload and every producer. A useful worksheet
has one row per behaviorally distinct use, even when several rows call the same Celery
task:

| Field | Questions to answer |
|---|---|
| Workload and owners | Who owns the callable, producer, worker, and incident response? |
| Producers | Web requests, model signals, management commands, scripts, webhooks, other tasks, beat, or external publishers? |
| Calling options | `.delay()`, `.apply_async()`, ETA/countdown, expiry, custom ID, serializer, callbacks, errbacks? |
| Delivery | Early/late ACK, reject/requeue, worker-loss redelivery, DLX, broker visibility timeout? |
| Retry | `self.retry()`, `autoretry_for`, per-task limits, jitter, manual replay, poison-message handling? |
| Routing and capacity | Queue, exchange, routing key, priority, rate limit, pool, prefetch, autoscale? |
| Results | Who calls `get()`, follows parents/children, reads progress, or retains result records? |
| Workflow | Chain, group, chord, map, linked callback, independently operated children? |
| Schedule | Beat entry, database scheduler, cron, one-off ETA, overlap lock, revoke or expiry state? |
| Data | JSON-compatible arguments/results, large payload references, secrets, model instances, pickle/custom serializer? |
| Safety | Idempotency key, transaction boundary, external-system deduplication, acceptable duplicate effects? |
| Security | Who may enqueue, cancel, retry, view arguments/results/logs, or select a tenant's execution target? |
| Drain proof | Which producer, broker, worker, scheduler, revoke, and result observations prove this workload is empty? |

Record the Celery version, broker transport, result backend, and worker pool as well;
their behavior changes the meaning of several options. Do not classify a workload from
the decorator alone.

## Phased coexistence plan

### 1. Establish the baseline

For each inventory row, capture representative task duration, queue delay, retry and
duplicate rate, failure modes, result consumers, and operational controls. Add a
workload-specific idempotency test before changing delivery.

### 2. Satisfy django-ray prerequisites

Upgrade the application/runtime where needed, deploy PostgreSQL-backed package
migrations, configure task backends and RuntimeEnv profiles, deploy Ray, and run
queue-specific task managers. Prove a small task through the same deployment boundary
the migrated workload will use.

### 3. Run both systems side by side

Keep Celery workers, broker, beat, monitoring, and result backend intact. Add distinct
django-ray task definitions and switch only identified producers. Do not make Celery
and Django `TaskResult` objects look interchangeable; store the delivery system and
task ID together in any application-owned tracking record.

Use a reversible application feature flag or producer routing decision when a cohort
needs gradual rollout. Rollback sends **new** work back to Celery; it does not convert
already-enqueued django-ray rows into Celery messages.

### 4. Move simple JSON-only idempotent tasks first

Start with coarse tasks that have no Canvas, beat, broker-control, custom serializer,
or specialized ACK requirements. Pass stable database/object-store identifiers rather
than model instances and validate result consumers against refreshed Django
`TaskResult` objects.

### 5. Translate policies explicitly

For each cohort, make and test separate decisions for:

- automatic versus manual retry and permanent exception types;
- idempotency keys and transaction-safe enqueue;
- queue and numeric priority;
- task-manager and Ray concurrency;
- timeout and application network deadlines;
- one-shot `run_after` scheduling;
- inline versus external result storage; and
- enqueue, cancel, retry, result, admin, metrics, and live-log authorization.

Do not accept "same as Celery" as a policy value.

### 6. Redesign each Canvas workflow

Choose one outer django-ray task only when repeating the complete workflow is safe.
Keep Celery or create application-owned durable stages when child-level retry,
scheduling, audit, cancellation, or results are requirements. Test failure after a
subset of children has already produced effects.

### 7. Retain unmatched Celery services

It is valid to keep Celery for beat, strict broker delivery, rich routing, rate-limited
tasks, or independently durable Canvas while django-ray handles Ray-oriented
workloads. A partial migration is safer than an invented compatibility layer.

### 8. Stop producers, then drain Celery

Drain in this order for each retired cohort:

1. Stop or switch every producer, including old deployments, scripts, webhooks,
   management commands, signals, other Celery tasks, and external publishers.
2. Disable or retarget every matching beat/database-scheduler entry. Confirm that a
   standby beat instance cannot resume it.
3. Let active tasks finish and inspect Celery's active, reserved, scheduled/ETA, and
   revoked state on every worker. Separately account for `RETRY` results/events and
   retry countdowns that appear as scheduled work. Celery documents these views in its
   [monitoring guide](https://docs.celeryq.dev/en/stable/userguide/monitoring.html).
4. Inspect broker-ready and dead-letter queues with broker-native tooling. Worker
   `inspect` output alone does not prove that an offline worker or broker queue is
   empty.
5. Account for Canvas callbacks/chords and application retry/outbox tables that can
   publish more work.
6. Keep the Celery result backend available until every result consumer and retention
   obligation has ended. Export audit records that must outlive the backend.
7. Preserve required revoke state until no matching message can arrive.
8. Stop the cohort's Celery workers. Observe through at least the longest producer,
   retry, ETA, and schedule window before deleting broker queues, beat state, or result
   data.

Never use `celery purge` as migration proof: it destroys queued messages and does not
prove that producers, schedulers, retries, or callbacks have stopped. Celery's
[monitoring documentation](https://docs.celeryq.dev/en/stable/userguide/monitoring.html)
explicitly describes purge as irreversible.

## Workload decision checklist

Classify one concrete producer-to-side-effect path at a time:

1. Are all arguments and the final result JSON-compatible?
2. Can large inputs/results use an application-owned URI or configured retrievable
   django-ray storage?
3. Is the side effect idempotent when an attempt repeats after an ambiguous failure?
4. Can retry be expressed as an uncaught exception plus deployment-wide attempt,
   backoff, and denylist settings?
5. Is one approximate hard timeout sufficient, with application-level I/O deadlines?
6. Does the task need only an earliest eligibility time, not recurrence or expiry?
7. Are a named task-manager queue, numeric priority, concurrency, and Ray resources
   sufficient without exchanges, routing keys, rate limits, or prefetch controls?
8. Can result consumers use a refreshed Django `TaskResult` instead of Celery
   `AsyncResult`, events, or a result graph?
9. If this is a workflow, may the complete outer task retry and repeat successful
   leaves?
10. Can Django admin, package observability, Prometheus, and authorized Ray live data
    replace Flower/inspect/control for this workload?
11. Is authorization enforced before enqueue and on every operational read/write,
    without treating a queue or RuntimeEnv as tenant isolation?
12. Is there a measurable producer-stop and Celery-drain proof?

If every answer is yes, the workload is a strong migration candidate. A "no" in
questions 3, 4, 8, or 9 usually requires a semantic redesign. A "no" because of
broker ACK/DLX, beat, remote control, arbitrary serializers, or strict rate limiting
usually means retaining Celery or choosing a separate service.

## Explicit non-goals

This migration path deliberately does not add or recommend:

- a Celery broker or task-protocol consumer;
- a `.delay()` or `.apply_async()` compatibility facade;
- automatic import of messages, task IDs, results, beat entries, or revokes;
- automatic Canvas translation;
- broker ACK/reject/requeue or dead-letter emulation;
- `self.retry()`, `expires`, Flower, inspect/control, or Celery event emulation;
- Celery signals, custom task bases, pools, prefetch, or arbitrary serializers; or
- queue names or RuntimeEnv profiles as tenant authorization boundaries.

Prefer explicit coexistence and a workload-specific rewrite over an API facade that
hides different reliability semantics.
