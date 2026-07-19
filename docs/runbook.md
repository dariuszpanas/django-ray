# Operator Runbook

This runbook is for operators running `django_ray_worker` in staging/production.

It focuses on:

- fast incident triage,
- safe manual recovery actions,
- expected metrics and alert signals.

## Scope

This covers the `django-ray` library runtime:

- `RayTaskExecution` task lifecycle rows,
- `TaskWorkerLease` worker heartbeat/coordination rows,
- `django_ray_worker` process behavior.

It does not cover custom business task logic internals.

## Quick Triage Checklist

1. Check worker process health (`django_ray_worker` logs, pod/process status).
2. Check task state distribution (`QUEUED`, `RUNNING`, `FAILED`, `LOST`, `CANCELLING`).
3. Check active worker leases and heartbeat freshness.
4. Check Ray connectivity from workers.
5. Check whether failures are retrying or already terminal.

## Primary Signals

From `/api/metrics` in the example project:

- `django_ray_tasks_total{state="..."}`
- `django_ray_tasks_queued`
- `django_ray_tasks_running`
- `django_ray_queue_depth{queue="..."}`

Expected behavior:

- steady state: `queued` remains near baseline while tasks complete.
- incident signal: `queued` rises while `running` stays near zero.
- incident signal: `running` grows and does not drain.
- incident signal: `failed`/`lost` rises quickly with the same callable path.

## Safety Model

Treat production tasks as at-least-once by default. `django-ray` can retry after app exceptions,
lost worker ownership, Ray connection loss, and unknown completion state. That protects throughput
and recovery, but it cannot prove that a crashed or disconnected attempt made no external side
effects before disappearing.

Before enabling automatic retries on side-effecting callables, confirm the task has an idempotency
key or operation table that makes duplicate execution harmless. For payments, emails, webhooks, or
external writes, prefer a deduplicated commit record over relying on task attempt counts.

## Useful Queries

```sql
-- Task counts by state
SELECT state, COUNT(*) AS count
FROM django_ray_raytaskexecution
GROUP BY state
ORDER BY state;
```

```sql
-- Long-running tasks (example threshold: 10 minutes)
SELECT id, task_id, callable_path, queue_name, claimed_by_worker, started_at
FROM django_ray_raytaskexecution
WHERE state = 'RUNNING'
  AND started_at < NOW() - INTERVAL '10 minutes'
ORDER BY started_at ASC;
```

```sql
-- Worker leases ordered by oldest heartbeat
SELECT worker_id, hostname, pid, queue_name, is_active, last_heartbeat_at
FROM django_ray_taskworkerlease
ORDER BY last_heartbeat_at ASC;
```

## Incident Playbooks

## 1) Queue Backlog Keeps Growing

Symptoms:

- `django_ray_tasks_queued` rising for several minutes.
- few or no new `RUNNING` tasks.

Checks:

1. Verify at least one worker is active and healthy.
2. Verify workers are polling the intended queue(s).
3. Verify Ray connectivity if running `--local` or `--cluster` modes.

Recovery:

1. Restart unhealthy worker processes/pods.
2. Confirm queue flags/settings match enqueue queue names.
3. If tasks are delayed by retries, inspect `run_after` timestamps before forcing retries.
4. Confirm `WORKER_POLL_INTERVAL_SECONDS` and
   `WORKER_POLL_MAX_INTERVAL_SECONDS`. An idle worker may take up to the configured
   maximum polling delay to observe newly enqueued work; this is not an end-to-end
   task-start bound. Sustained activity resets it to the base.

If database load is high while queues are empty, run the PostgreSQL polling benchmark
from the [performance guide](performance.md#benchmark-worker-polling) before tuning.
Increase the maximum gradually and compare idle queries per worker-second with p95
claim latency. Do not lengthen heartbeat, timeout, or cancellation settings to reduce
claim-query load; those schedules are independent safety controls.

## 2) Tasks Stuck In RUNNING

Symptoms:

- many `RUNNING` rows with stale `started_at`/`last_heartbeat_at`.

Checks:

1. Confirm owning workers (`claimed_by_worker`) still have active leases.
2. Confirm `STUCK_TASK_TIMEOUT_SECONDS` and `timeout_seconds` are appropriate.

Recovery:

1. Let normal recovery run first (`detect_stuck_tasks` handles orphaned ownership and
   Ray Job reconciliation can adopt persisted jobs from inactive workers).
2. If needed, requeue only clearly orphaned/failed tasks using admin retry actions.

Notes:

- A fresh `last_heartbeat_at` can mean either the owning worker is healthy or another
  worker is still actively monitoring/reconciling the task.
- `UNKNOWN` Ray Job states are intentionally allowed to age into stuck-task recovery
  once monitor heartbeats stop advancing.

Optional targeted SQL (use carefully):

```sql
-- Example: inspect orphan candidates first (do not update blindly)
SELECT id, task_id, callable_path, claimed_by_worker, started_at
FROM django_ray_raytaskexecution
WHERE state = 'RUNNING'
ORDER BY started_at ASC;
```

## 3) Retry Storm / Repeated Failures

Symptoms:

- fast growth in `FAILED` + `LOST`.
- same callable repeatedly retried.

Checks:

1. Identify dominant failing `callable_path`.
2. Inspect latest `error_message` and `error_traceback`.
3. Confirm denylist policy (`RETRY_EXCEPTION_DENYLIST`) for non-retryable failures.

Recovery:

1. Stop or scale down workers if failures are harmful/high-volume.
2. Fix configuration or task code root cause.
3. Requeue failed tasks in controlled batches after fix.

## 4) Ray Connection Loss

Symptoms:

- worker logs contain reconnect warnings/errors.
- tasks fail with Ray connection errors.

Checks:

1. Verify `RAY_ADDRESS` and network reachability.
2. Verify Ray head/dashboard/cluster health.
3. Verify worker mode (`--local`, `--cluster`, default runner mode).

Recovery:

1. Restore Ray cluster/network.
2. Restart workers after Ray is healthy.
3. Verify pending tasks move back to `QUEUED`/`RUNNING`.

## 5) Cancellation Stuck In CANCELLING

Symptoms:

- rows remain `CANCELLING` for too long.

Checks:

1. Confirm owning worker is still running.
2. Confirm cancellation processing runs in worker loop.

Recovery:

1. Restart worker if cancellation loop is stalled.
2. After restart, verify `CANCELLING -> CANCELLED` transitions complete.

## 6) Oversized Results

Symptoms:

- successful tasks with `result_data = NULL` and populated `result_reference`.

Interpretation:

- this is expected when result payload exceeds `MAX_RESULT_SIZE_BYTES`.
- reference format indicates backend:
  - `oversize://sha256/...` -> digest-only pointer (no external payload)
  - `resultfs://sha256/...` -> filesystem-backed payload reference
  - `s3://...` -> S3/object-storage payload reference
  - `gs://...` -> GCS payload reference

Recovery:

1. If this is unexpected, reduce result payload size in task design.
2. If retrieval is required, configure a retrievable backend:
   - `RESULT_STORAGE_BACKEND="filesystem"` with `RESULT_STORAGE_FILESYSTEM_PATH=<shared path>`
   - `RESULT_STORAGE_BACKEND="s3"` with bucket/config and working credentials
   - `RESULT_STORAGE_BACKEND="gcs"` with bucket/config and working credentials
3. `RayTaskBackend.get_result()` can rehydrate `resultfs://...`, `s3://...`, and `gs://...`
   references when the reader has matching storage configuration. `oversize://...`
   digest references remain metadata-only.

## Safe Manual Actions

Prefer Django admin actions for retries/cancellations before direct SQL updates.

If scripting is necessary, use Django shell and narrow filters:

```python
from django_ray.models import RayTaskExecution, TaskState

qs = RayTaskExecution.objects.filter(state=TaskState.FAILED, queue_name="default")[:100]
for task in qs:
    task.state = TaskState.QUEUED
    task.started_at = None
    task.finished_at = None
    task.error_message = None
    task.error_traceback = None
    task.claimed_by_worker = None
    task.ray_job_id = None
    task.save()
```

## Escalation Guidance

Escalate to development team when:

- repeated failures persist after configuration/network fixes,
- retry policy behavior differs across worker modes,
- state transitions violate expected lifecycle semantics,
- manual recovery requires broad database edits.

## Related Docs

- [Configuration](configuration.md)
- [Retry & Error Handling](retry.md)
- [Worker Modes](worker-modes.md)
- [Architecture](architecture.md)
