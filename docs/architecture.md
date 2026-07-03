# Architecture

This document describes the runtime architecture of `django-ray` and how work moves from Django to Ray and back.

## System Overview

`django-ray` integrates Django Tasks with Ray using a database-backed control plane:

- Django app code enqueues tasks through Django's Tasks API.
- `django-ray` persists execution metadata in the database.
- `django_ray_worker` claims work, runs it (sync/Ray Core/Ray Job), and reconciles status.
- Ray executes task callables in local or cluster compute environments.
- Ray-native workflow steps can fan out and chain within one durable task boundary.

## Request-To-Execution Flow

1. App code calls `.enqueue(...)` on a Django task.
2. Backend stores a `RayTaskExecution` row in `QUEUED` state. The selected
   RuntimeEnv profile is resolved into an immutable JSON snapshot.
3. Worker claims eligible rows and marks them `RUNNING`.
4. Worker submits task execution in the selected mode.
5. Worker reconciles completion and stores success/failure details.
6. Retry policy may requeue `FAILED` or `LOST` tasks until attempts are exhausted.

## Runtime Components

### Django Application Processes

- Enqueue tasks.
- Read task status/results via Django Tasks and admin/API views.

### Worker Process (`django_ray_worker`)

- Claims tasks from DB.
- Maintains worker lease heartbeats.
- Maintains task-monitor heartbeats for in-flight work it is actively reconciling.
- Submits and reconciles execution in sync/Ray Core/Ray Job modes.
- Applies retry policy and stuck-task/orphan recovery.

### Ray Runtime

- Executes submitted functions.
- Returns completion state and result/error payloads.
- Resolves workflow step dependencies through object references without a database
  round trip for each internal step.

### Database

- Canonical source of truth for task lifecycle state.
- Stores worker leases for cross-worker coordination.

## Data Model

### `RayTaskExecution`

Primary execution record for one task attempt chain.

| Field | Notes |
|---|---|
| `id` | `BigAutoField` primary key |
| `task_id` | Django task identifier |
| `callable_path` | Dotted import path for callable |
| `queue_name` | Queue used for claim/execution |
| `state` | `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `CANCELLING`, `LOST` |
| `attempt_number` | Current attempt counter |
| `args_json`, `kwargs_json` | Serialized call arguments |
| `result_data` | Inline JSON result when under size limit |
| `result_reference` | Pointer used when result exceeds `MAX_RESULT_SIZE_BYTES` (`digest`, `filesystem`, `s3`, `gcs`) |
| `progress_data` | Latest JSON progress snapshot for a Ray-native workflow |
| `runtime_env_profile` | Optional name selected by the enqueueing backend |
| `runtime_env_json` | Canonical immutable RuntimeEnv snapshot used by retries |
| `runtime_env_hash` | SHA-256 content identity used to correlate cache reuse |
| `error_message`, `error_traceback` | Failure metadata |
| `ray_job_id`, `ray_address` | Runner-specific execution handle metadata |
| `claimed_by_worker` | Worker lease owner that currently owns the task |
| `run_after` | Delayed/retry scheduling timestamp |
| `timeout_seconds` | Per-task timeout override |
| `created_at`, `started_at`, `finished_at`, `last_heartbeat_at` | Lifecycle timestamps |

### `TaskWorkerLease`

Worker coordination record used to detect dead/inactive workers.

| Field | Notes |
|---|---|
| `worker_id` | Primary key identifier for worker process |
| `hostname`, `pid` | Worker identity details |
| `queue_name` | Informational queue assignment |
| `started_at`, `last_heartbeat_at`, `stopped_at` | Lease timing |
| `is_active` | Active/inactive lease state |

## Task State Model

```text
QUEUED -> RUNNING -> SUCCEEDED
QUEUED -> CANCELLED
RUNNING -> CANCELLING -> CANCELLED
RUNNING -> FAILED
RUNNING -> LOST
FAILED/LOST -> QUEUED (if retry policy allows)
```

Notes:

- Retries increment `attempt_number` and set `run_after` backoff.
- Terminal failure happens after retry policy exhaustion.

## Delivery Semantics

`django-ray` provides at-least-once execution semantics for retryable work. A task can be
executed more than once when a worker, Ray worker, Ray head, network connection, or process dies
after user code has performed side effects but before `django-ray` records the successful result.

For side-effecting tasks, use an application-level idempotency key such as the Django task id, an
order id, or another operation id guarded by a unique constraint in the system being changed. Keep
external effects such as payments, email sends, webhooks, and third-party mutations idempotent or
split them into a deduplicated commit step. `SUCCEEDED` means the final observed outcome succeeded;
it does not prove that every earlier execution attempt had no side effects.

## Worker Loop

```python
while running:
    renew_worker_lease()
    claim_due_queued_tasks()
    submit_claimed_tasks()
    reconcile_in_flight_tasks()
    detect_stuck_and_orphaned_running_tasks()
    sleep(poll_interval)
```

## Execution Adapters

### Sync mode

- Executes callable in worker process.
- Useful for development/testing.

### Ray Core mode

- Uses Ray remote execution directly.
- Lower submission overhead.
- Applies the persisted RuntimeEnv snapshot to the outer remote task.
- Supports nested Ray-native workflows (`chain`, `group`, and `map_step`).

### Ray Job mode

- Uses Ray Job Submission API.
- Worker submits a payload transport command:
  - `python -m django_ray.runtime.entrypoint --payload-b64 <...>`
- Payload is URL-safe base64 JSON containing callable path and serialized args/kwargs.
- Applies the same persisted RuntimeEnv snapshot to the submitted Ray Job.
- Workers can adopt orphaned persisted Ray Job handles from inactive workers and continue reconciliation
  instead of immediately retrying duplicate work.

## Entrypoint Contract

`django_ray.runtime.entrypoint`:

- Bootstraps Django in Ray runtime.
- Decodes task payload.
- Imports callable and executes it.
- Returns JSON result envelope:
  - `success`
  - `result`
  - `error`
  - `traceback`
  - `exception_type`

## Reliability Controls

- Unified retry policy with denylist support (short and fully-qualified exception names).
- Worker lease heartbeat + cross-worker orphan recovery.
- Task monitor heartbeats for active reconciliation paths.
- Throttled, batched Ray Core task-monitor heartbeat persistence.
- Per-workflow in-memory progress coordination with bounded database snapshots.
- Stuck/timeout detection with loss handling and retry path.
- Startup settings validation fail-fast by default, with migration/bootstrap bypass controls.
- Result size enforcement with configurable oversized-result backends (`digest`, `filesystem`, `s3`, `gcs`).
- Backend result retrieval rehydrates `result_reference` payloads for retrievable backends.

## Observability Surfaces

- Django admin for task/lease inspection and operations.
- Worker logs for claim/submit/reconcile/retry events.
- Optional metrics/API surfaces in `testproject` example app.

## See Also

- [Configuration](configuration.md)
- [Worker Modes](worker-modes.md)
- [Ray-Native Workflows](workflows.md)
- [Runtime Environments](runtime-environments.md)
- [Retry & Error Handling](retry.md)
