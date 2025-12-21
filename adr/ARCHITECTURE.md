# django-ray — Architecture

**Version:** Draft v0.1
**Runtime:** Python 3.12 only

This document expands `requirements.md` into an implementable architecture, including component boundaries, interfaces, state machine, and operational flows.

---

## 1. System overview

`django-ray` integrates Django 6 Tasks with Ray by providing:

* **A Runner/Worker** that claims tasks from the configured Tasks backend and submits them to Ray.
* **A canonical metadata layer** in Postgres that tracks execution attempts, Ray metadata, and operational state.
* **Pluggable execution adapters** to support multiple Ray execution modes.

**Design stance:**

* **Postgres is the system of record** for task state.
* **Ray is the execution fabric**.
* Execution is **at-least-once**; idempotency is encouraged and supported.

---

## 2. Deployment topology

### 2.1 Components

1. **Django Web / API**

   * Defines and enqueues tasks.
   * Reads status/results.
   * Optionally exposes admin UI.

2. **Postgres**

   * Stores Django app data.
   * Stores task backend data (if DB-backed backend).
   * Stores django-ray execution attempt metadata.

3. **django-ray Worker**

   * Runs as one or more replicas.
   * Claims tasks via DB locking.
   * Submits tasks to Ray.
   * Reconciles statuses.

4. **Ray cluster**

   * Executes functions / jobs.
   * Exposes Job server / dashboard.

### 2.2 Operational mode matrix

| Mode    |         Local dev |        Staging |                 Production |
| ------- | ----------------: | -------------: | -------------------------: |
| Worker  |    management cmd | K8s Deployment |       K8s Deployment (HPA) |
| Ray     | local `ray start` | in-cluster Ray |     autoscaling Ray on K8s |
| Backend |                DB |             DB |                         DB |
| Runner  |     Ray Job (MVP) |        Ray Job | Ray Job / Ray Core (later) |

---

## 3. Data model

### 3.1 Canonical task record

Tasks are enqueued via Django Tasks; the backend stores canonical fields (name/import path, args/kwargs, scheduled run time, state, etc.). `django-ray` **must not fork** canonical state; it extends it with Ray-specific metadata.

### 3.2 django-ray models

#### 3.2.1 `RayTaskExecution`

**Purpose:** one row per execution attempt.

**Fields (required):**

* `id: UUID`
* `django_task_id: str` (or UUID depending on backend)
* `attempt: int` (1..n)
* `runner_type: str` (`ray_job`, `ray_core`)
* `state: str` (`SUBMITTED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELED`, `LOST`)

**Ray metadata (nullable):**

* `ray_job_id: str`
* `ray_submission_id: str`
* `ray_actor_id: str`
* `ray_address: str` (useful if multiple clusters)

**Timestamps:**

* `submitted_at`, `started_at`, `finished_at`
* `last_heartbeat_at`

**Outcome details:**

* `exit_code: int?`
* `error_class: str?`
* `error_message: str?`
* `traceback: str?`

**Output pointers:**

* `result_ref: str?` (DB inline JSON OR external pointer)
* `log_ref: str?` (Ray log pointer, dashboard URL, or blob pointer)

**Indexes:**

* `(django_task_id, attempt)` unique
* `state`
* `last_heartbeat_at`

#### 3.2.2 `RayWorkerLease` (optional, recommended)

**Purpose:** record presence and health of worker processes.

* `worker_id: UUID`
* `identity: str` (pod name/host)
* `started_at`, `last_seen_at`
* `version: str`

---

## 4. State machines

### 4.1 Canonical task states (backend)

* `QUEUED` → `RUNNING` → `SUCCEEDED` | `FAILED` | `CANCELED`

### 4.2 Execution attempt states (`RayTaskExecution`)

* `SUBMITTED` → `RUNNING` → `SUCCEEDED` | `FAILED` | `CANCELED`
* `LOST` if Ray cannot confirm state and the attempt exceeds a timeout window.

### 4.3 Transition rules

1. Claiming transitions canonical task `QUEUED → RUNNING` in a DB transaction.
2. Submission creates `RayTaskExecution` attempt with `SUBMITTED`.
3. On first Ray signal (or periodic poll), set attempt state to `RUNNING` and set `started_at`.
4. On completion:

   * success: attempt `SUCCEEDED`, task `SUCCEEDED`
   * failure: attempt `FAILED`, task `FAILED` (or `QUEUED` if retry policy schedules a retry)
5. Cancellation:

   * if not submitted: task becomes `CANCELED`, no attempt row required
   * if submitted: attempt `CANCELED` when confirmed; canonical task `CANCELED`

---

## 5. Core interfaces

`django-ray` is structured around three internal interfaces. Concrete implementations must be swappable.

### 5.1 `TaskBackendAdapter`

**Purpose:** normalize access to Django Tasks backend APIs.

Responsibilities:

* Fetch runnable tasks (filtered by queue, ETA)
* Atomically claim tasks using DB locks
* Update canonical task state
* Read/write task outcome

Key methods (conceptual):

* `list_runnable(limit, now, queues) -> list[TaskRecord]`
* `claim(task_ids, worker_id) -> list[TaskRecord]` (atomic)
* `mark_running(task_id, started_at)`
* `mark_succeeded(task_id, finished_at, result_ref)`
* `mark_failed(task_id, finished_at, error)`
* `mark_canceled(task_id, finished_at)`

### 5.2 `ExecutionAdapter`

**Purpose:** submit execution to Ray and reconcile status.

Two implementations:

* `RayJobExecutionAdapter` (MVP)
* `RayCoreExecutionAdapter` (later)

Key methods:

* `submit(task_record, attempt) -> SubmissionHandle`
* `poll(handle) -> ExecutionStatus` (running/succeeded/failed/canceled + metadata)
* `cancel(handle) -> CancelStatus`
* `fetch_logs(handle) -> log_ref?`

### 5.3 `ResultStore`

**Purpose:** store results with size-aware behavior.

Implementations:

* `DBResultStore` (default)
* `ExternalBlobResultStore` (S3/GCS/MinIO) (later)

Key methods:

* `put(task_id, attempt, result) -> result_ref`
* `get(result_ref) -> result`

---

## 6. Worker design

### 6.1 Process responsibilities

The worker is a long-running loop:

1. Heartbeat/lease update
2. Claim tasks
3. Submit to Ray
4. Track active handles
5. Reconcile states
6. Apply retry/cancel policies

### 6.2 Worker loop phases

#### Phase A — Claim

* Query runnable tasks
* Claim using `FOR UPDATE SKIP LOCKED`
* Set canonical tasks to `RUNNING`
* Create attempt row(s) as `SUBMITTED`

#### Phase B — Submit

* For each claimed task attempt:

  * call `execution_adapter.submit()`
  * persist Ray metadata in `RayTaskExecution`

#### Phase C — Reconcile

* For in-flight attempts:

  * poll Ray for status
  * update attempt state + canonical state
  * store result/error pointers

#### Phase D — Recovery

* Detect stuck attempts (no heartbeat / no Ray status / too old)
* Mark attempt `LOST`
* Apply retry policy if enabled

### 6.3 Concurrency

* Worker maintains a max `in_flight` count (global)
* Optional per-queue throttling
* Backoff/jitter on polling to avoid DB thundering herd

---

## 7. Serialization & execution entrypoint

### 7.1 Task identity

* Task callable is referenced by import path.

### 7.2 Argument encoding

* args/kwargs must be JSON-serializable.
* Provide a strict validation mode (default on) that fails enqueue if not serializable.

### 7.3 Execution entrypoint

A single entrypoint module invoked by Ray:

1. Set `DJANGO_SETTINGS_MODULE`
2. `django.setup()`
3. Import callable by path
4. Execute callable
5. Serialize result
6. Return structured outcome to the runner

**Outcome contract:**

* `ok: bool`
* `result: any?` (JSON)
* `error: {class, message, traceback}?`
* `metrics/log_refs?`

---

## 8. Retry policy

### 8.1 Policy configuration

Retry policy can be defined:

* per task (decorator option)
* per queue (settings default)

Parameters:

* `max_attempts`
* `backoff: fixed | exponential`
* `base_delay_seconds`, `max_delay_seconds`
* `retry_on: [ExceptionClassPaths]` allowlist
* `no_retry_on: [ExceptionClassPaths]` denylist

### 8.2 Retry behavior

* On failure:

  * persist attempt failure
  * if retry allowed, re-queue canonical task with ETA
  * increment attempt counter

---

## 9. Cancellation

### 9.1 Cancellation API

* Provide a callable API: `cancel(task_id)`
* Provide admin action: cancel selected tasks

### 9.2 Semantics

* If task not yet submitted: mark `CANCELED`.
* If submitted:

  * request Ray cancellation
  * mark canonical state `CANCELED` after confirmation or after timeout

---

## 10. Observability

### 10.1 Logs

* Worker emits structured logs with task_id, attempt, queue, runner_type, ray_job_id.
* Execution logs are linked via `log_ref`.

### 10.2 Metrics

Expose Prometheus metrics:

* queue depth (by queue)
* in-flight tasks
* success/failure counters
* latency histograms

### 10.3 Tracing (later)

* OpenTelemetry spans:

  * enqueue → claim → submit → run → finalize

---

## 11. Security

* Default redaction for fields named like: `password`, `token`, `secret`, `key`, `auth`.
* Configurable redaction hooks.
* Admin actions require explicit permissions.
* Ray endpoint must be reachable only from trusted networks.

---

# Implementation plan (initial)

> This section is a concise plan; a fuller breakdown belongs in `IMPLEMENTATION_PLAN.md`. If you want a separate file, copy this section into it.

## Phase 0 — Repo scaffolding (foundation)

### Deliverables

* `pyproject.toml` pinned to Python 3.12
* Linting/formatting (ruff, black)
* Type checking (pyright or mypy)
* Test runner (pytest)
* Basic docs skeleton

### Acceptance criteria

* `pip install -e .` works
* CI passes lint + typecheck + unit tests

---

## Phase 1 — MVP (DB claim + Ray Job runner)

### Deliverables

* Django app: `django_ray` with models `RayTaskExecution`
* Worker management command: `python manage.py django_ray_worker`
* DB claiming implementation (`SKIP LOCKED`)
* `RayJobExecutionAdapter`:

  * submit job
  * poll status
  * cancel job
  * capture logs pointer
* Result storage (DB small results)
* Admin integration

### Acceptance criteria

* A demo task enqueued via Django Tasks executes via Ray
* Task states visible in admin
* Failure captures traceback

---

## Phase 2 — Reliability

### Deliverables

* Heartbeats
* Stuck detection and `LOST` state
* Retry policies + backoff
* Cancellation improvements

### Acceptance criteria

* Worker crash mid-run recovers
* Stuck tasks are handled deterministically

---

## Phase 3 — Throughput mode (Ray Core)

### Deliverables

* `RayCoreExecutionAdapter`
* Actor pool option
* Per-queue concurrency

### Acceptance criteria

* Lower overhead than job runner in benchmark test

---

## Phase 4 — Ecosystem polish

### Deliverables

* Prometheus metrics endpoint
* OTel tracing hooks
* K8s examples
* Reference application

### Acceptance criteria

* Observability docs + sample dashboards
* Example deployment works end-to-end
