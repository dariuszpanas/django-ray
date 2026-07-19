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
2. Backend stores a `RayTaskExecution` row in `QUEUED` state, including Django's
   numeric priority. The selected RuntimeEnv profile is resolved into an immutable
   JSON snapshot.
3. Worker claims eligible rows by descending priority and FIFO creation time, then
   marks them `RUNNING`.
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

The command currently orchestrates the existing leasing, runner, reconciliation, and
cancellation helpers. The runner classes own mode-specific submission and polling, while
the command coordinates when to claim, reconcile, or hand off work. Broader extraction
of those orchestration paths into separate services remains future work.

This boundary is explicit for Ray Core tracking: `RayCoreRunner.pending_task_ids` returns
a stable task-ID snapshot and `clear_pending_tasks()` clears local tracking. The command
does not reach into the runner's private object-reference registry, so connection-loss
and shutdown paths can manage local state without coupling lifecycle code to runner
storage.

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
| `priority` | Django priority from `-100` to `100`; larger values are claimed sooner |
| `state` | `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `CANCELLING`, `LOST` |
| `attempt_number` | Current attempt counter |
| `args_json`, `kwargs_json` | Serialized arguments, or JSON `null` placeholders for external input |
| `input_reference` | Optional durable pointer to a versioned combined input envelope |
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
| `timeout_seconds` | Optional timeout from the selected backend's `OPTIONS["TIMEOUT_SECONDS"]` |
| `created_at`, `started_at`, `finished_at`, `last_heartbeat_at` | Lifecycle timestamps |

The worker evaluates `timeout_seconds` during periodic reconciliation, so timeout
enforcement is approximate. A timeout is terminal `FAILED` state (manual retry is
required). Ray Core cancellation uses the tracked object reference, Ray Job cancellation
uses the Job API, and synchronous execution can only be finalized after the worker
regains control.

### `TaskWorkerLease`

Worker coordination record used to detect dead/inactive workers.

| Field | Notes |
|---|---|
| `worker_id` | Primary key identifier for worker process |
| `hostname`, `pid` | Worker identity details |
| `queue_name` | Informational queue assignment |
| `started_at`, `last_heartbeat_at`, `stopped_at` | Lease timing |
| `is_active` | Active/inactive lease state |

### `TaskInputPayload`

Registry and cleanup tombstone for content-addressed external inputs. It records the
reference, backend, digest, byte size, envelope version, last-use time, cleanup state,
and cleanup error. Execution rows retain `input_reference` after cleanup for audit.
Row locks on the registry and referencing executions prevent cleanup from deleting a
payload while another enqueue is registering the same content.

### `TaskAttempt`

Each terminal transition records the one-based attempt number, state, result
references, and failure diagnostics in `TaskAttempt`. The current
`RayTaskExecution` row remains the source of truth for scheduling, while this
history makes retries auditable after the current row is reset for its next
attempt. Admin retries, the operational retry API, and automatic worker retries
all use the same row-locked lifecycle service and increment the attempt counter.

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
- Retries keep the persisted priority; due delayed/retry rows and immediate rows share
  one descending-priority, FIFO claim order.
- Queue names select workload boundaries and have no implicit scheduling precedence.
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

The loop below is pseudocode, not a public callable API:

```text
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
- Payload is URL-safe base64 JSON containing callable path, serialized args/kwargs,
  attempt number, and execution generation.
- Applies the same persisted RuntimeEnv snapshot to the submitted Ray Job.
- Carries durable task identity into the driver and initializes Ray lazily for
  nested workflows, giving Ray Job and Ray Core the same graph/progress protocol.
- The driver persists a structured completion envelope on `RayTaskExecution` before
  exiting. Reconciliation uses this durable channel for success/failure and treats
  missing or malformed envelopes as non-terminal; Ray stdout/stderr is diagnostic only.
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
  - `result_reference` (for oversized results)
  - `error`
  - `traceback`
  - `exception_type`

For Ray Job mode, this envelope is also written to the task's `completion_data`
field. It is the authoritative completion channel; logs may be unavailable or
contain arbitrary application output.

### Rolling upgrades

The completion envelope and `execution_generation` fields are part of the Ray
Job protocol. Drain Ray Job workers before deploying a version that introduces
or changes this protocol: let submitted jobs finish (or explicitly mark them
for retry), stop the old workers, apply database migrations, and then start the
new workers. Do not leave old and new workers reconciling the same in-flight
jobs, because an old driver may not write the envelope or generation metadata
required for the new worker to prove which execution produced a terminal state.

Durable input transport has a separate opt-in boundary. Apply its additive migration
and deploy the new code everywhere while `MAX_INLINE_INPUT_SIZE_BYTES` remains `None`.
Drain old Ray Job drivers before enabling spillover. Existing inline rows remain valid;
referenced Ray Jobs use transport version 2 and contain only `input_reference`. Before
rolling back, disable spillover and drain all tasks that already have a reference.

## Reliability Controls

- Unified retry policy with denylist support (short and fully-qualified exception names).
- Worker lease heartbeat + cross-worker orphan recovery.
- Task monitor heartbeats for active reconciliation paths.
- Throttled, batched Ray Core task-monitor heartbeat persistence.
- Per-workflow in-memory progress coordination with revision-based database snapshots.
- Versioned workflow graphs with stable node IDs, dependency edges, Ray execution
  identifiers, environment identity, and application-reported leaf progress.
- Stuck/timeout detection with loss handling and retry path.
- Startup settings validation fail-fast by default, with migration/bootstrap bypass controls.
- Result size enforcement with configurable oversized-result backends (`digest`, `filesystem`, `s3`, `gcs`).
- Backend result retrieval rehydrates `result_reference` payloads for retrievable backends.
- Versioned, content-addressed input envelopes with retrievable filesystem, S3, and GCS backends.

## Observability Surfaces

- Django admin for task/lease inspection and operations.
- Authenticated, polling-based live task state and workflow progress in the task admin.
- Versioned package services for task, queue, attempt, workflow, and bounded live-Ray data.
- Package-owned Prometheus rendering with explicit queue-label allowlists and fixed labels.
- Worker logs for claim/submit/reconcile/retry events.
- Structured workflow-leaf logs correlated by durable task, workflow node, and Ray IDs.
- Optional Ray State API lookup for live task attempts and bounded stdout/stderr tails.
- Optional authenticated HTTP adapters in the `testproject` example app.

## See Also

- [Configuration](configuration.md)
- [Worker Modes](worker-modes.md)
- [Ray-Native Workflows](workflows.md)
- [Runtime Environments](runtime-environments.md)
- [Retry & Error Handling](retry.md)
