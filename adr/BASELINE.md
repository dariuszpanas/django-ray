# django-ray — Requirements Specification

**Version:** Draft v0.1
**Status:** Architecture & implementation requirements
**Runtime:** Python 3.12 only

---

## 1. Overview

`django-ray` is a Django extension that implements **Django 6.0 Tasks execution** using **Ray** as the scalable execution engine.

Django 6 introduces a standardized Tasks API for defining, enqueueing, and tracking background work, but **does not execute tasks itself**. Execution is explicitly delegated to external systems. `django-ray` fills this gap by acting as a **production-grade task runner** that submits and manages task execution on a Ray cluster.

The project is designed to be:

* Django-native (ORM, admin, management commands)
* Ray-powered (scalable, autoscaling-friendly, cloud-native)
* Operationally simple
* Extensible for future execution backends

---

## 2. Constraints & Assumptions

### 2.1 Language & Runtime

* **Python 3.12 is mandatory**.

  * This is required to satisfy both Django 6.x and modern Ray dependencies.
  * No other Python versions will be supported.

### 2.2 Framework & Platform

* Django >= 6.0 (Tasks framework required)
* Ray (minimum version pinned during implementation)
* PostgreSQL as the canonical metadata store
* Kubernetes is the primary deployment target (but not required for local dev)

### 2.3 Execution Guarantees

* **At-least-once execution** semantics
* Exactly-once semantics are explicitly out of scope
* Tasks must be designed to be **idempotent**

---

## 3. Goals & Non-Goals

### 3.1 Goals

1. Provide a **production-ready execution backend** for Django Tasks
2. Support scalable, autoscaling execution using Ray
3. Maintain Django as the **system of record** for task state
4. Provide strong observability (status, retries, logs, errors)
5. Support multiple execution strategies behind a stable interface
6. Enable incremental adoption (local → Ray → large-scale)

### 3.2 Non-Goals (Initial Versions)

* Full Celery feature parity (chains, chords, canvas primitives)
* Exactly-once delivery guarantees
* Ray-native durable queue (DB remains canonical)

---

## 4. High-Level Architecture

### 4.1 Logical Components

1. **Django Application (Producer)**

   * Defines tasks using Django 6 Tasks API
   * Enqueues tasks via configured backend

2. **Task Backend (Storage Layer)**

   * Stores task metadata, arguments, scheduling info, and results
   * Database-backed backend is required for MVP

3. **django-ray Runner / Worker (Control Plane)**

   * Long-running worker process
   * Claims tasks from backend
   * Submits execution to Ray
   * Reconciles execution state

4. **Ray Cluster (Execution Plane)**

   * Executes task code
   * Provides scalability and autoscaling

---

## 5. Execution Modes

### 5.1 Ray Job Submission Runner (Required for MVP)

* Uses Ray Job Submission API
* Submits each task as a Ray job

**Advantages:**

* Strong isolation
* Clear operational boundaries
* Easier dependency management

**Disadvantages:**

* Higher per-task overhead

### 5.2 Ray Core Runner (Planned)

* Uses Ray remote functions and/or actors

**Advantages:**

* Lower latency and overhead
* Higher throughput

**Disadvantages:**

* Requires identical runtime environment across Ray workers

**Requirement:** Both runners must produce identical Django-visible semantics.

---

## 6. Canonical Data Model

Django (Postgres) is the **source of truth** for task state.

### 6.1 RayTaskExecution Model

Tracks execution attempts and Ray-specific metadata.

**Fields (required):**

* id (UUID)
* django_task_id
* attempt (int)
* runner_type (ray_job | ray_core)
* state (SUBMITTED | RUNNING | SUCCEEDED | FAILED | CANCELED | LOST)
* ray_job_id (nullable)
* ray_actor_id (nullable)
* submitted_at
* started_at
* finished_at
* last_heartbeat_at
* error_class
* error_message
* traceback
* result_ref
* log_ref

**Indexes:**

* (django_task_id, attempt)
* state
* last_heartbeat_at

### 6.2 Worker Lease Model (Optional but Recommended)

Tracks active worker processes for safety and observability.

---

## 7. Task Lifecycle & State Machine

### 7.1 Canonical Task States

* QUEUED
* RUNNING
* SUCCEEDED
* FAILED
* CANCELED

### 7.2 Execution Attempt States

* SUBMITTED → RUNNING → SUCCEEDED | FAILED | CANCELED
* LOST (Ray job missing / worker failure)

### 7.3 Retry Semantics

* Each retry creates a **new execution attempt**
* Retry policies are configurable
* Backoff strategies must be supported

---

## 8. Claiming & Concurrency Control

### 8.1 Claiming Algorithm

* Query for runnable tasks:

  * status = QUEUED
  * eta <= now
* Use `SELECT ... FOR UPDATE SKIP LOCKED`
* Transition task to RUNNING atomically

### 8.2 Concurrency Limits

Must support:

* Global in-flight limit
* Per-queue concurrency limits
* Priority ordering (optional)

---

## 9. Task Payload & Serialization

### 9.1 Task Identity

* Tasks are identified by import path (module + callable name)

### 9.2 Arguments

* Arguments must be JSON-serializable
* ORM objects must be passed by ID, not instance

### 9.3 Execution Entrypoint

A unified execution entrypoint must:

1. Initialize Django settings
2. Import the task callable
3. Execute with provided args/kwargs
4. Capture result or exception

---

## 10. Result Handling

### 10.1 Default Behavior

* Small results stored directly in DB (size-limited)
* Large results stored externally (S3/GCS/MinIO)

### 10.2 Error Handling

* Capture exception class, message, traceback
* Persist for admin/UI visibility

---

## 11. Cancellation

### 11.1 Cancellation Semantics

* Best-effort cancellation
* Ray job or actor termination requested
* Final state must always be consistent in DB

---

## 12. Observability Requirements

### 12.1 Logging

* Worker lifecycle logs
* Submission metadata
* Execution result summaries

### 12.2 Metrics (Required)

* tasks_queued
* tasks_running
* tasks_succeeded_total
* tasks_failed_total
* task_latency_seconds
* ray_submission_failures

### 12.3 Tracing (Planned)

* OpenTelemetry-compatible spans

---

## 13. Configuration

### 13.1 Django Settings Namespace

`DJANGO_RAY`:

* RUNNER
* RAY_ADDRESS
* QUEUES
* CONCURRENCY
* RETRIES
* RESULTS
* HEARTBEAT_INTERVAL_SECONDS
* STUCK_TASK_TIMEOUT_SECONDS

---

## 14. Django Admin Integration

Admin UI must support:

* Task list & filters
* Execution attempts
* Retry & cancel actions
* Error and traceback visibility
* Ray metadata inspection

---

## 15. Security Requirements

* Sensitive arguments must be redacted from logs
* Admin actions require explicit permissions
* Ray endpoints must be secured at network level

---

## 16. Compatibility & CI Requirements

* Python 3.12 only
* Django 6.x compatibility matrix
* Ray version pinned and validated
* CI must include integration tests with a local Ray cluster

---

## 17. Development Phases

### Phase 0: Scaffolding

* Project structure
* CI pipeline
* Dependency pinning

### Phase 1: MVP

* DB-backed queue
* Ray Job runner
* Worker management command
* Admin UI

### Phase 2: Reliability

* Heartbeats
* Stuck task detection
* Retries & backoff

### Phase 3: Throughput Mode

* Ray Core runner
* Actor pools

### Phase 4: Ecosystem

* Metrics
* Tracing
* K8s examples

---

## 18. Open Design Questions

1. Custom DB backend vs Django-provided backend
2. Result storage defaults
3. Built-in scheduler vs external integration
4. Multi-tenant isolation strategies

---

## 19. Acceptance Criteria (MVP)

* Task enqueued via Django API executes on Ray
* Success and failure states visible in Django admin
* Worker crash does not corrupt task state
* Retry logic functions as configured
* System runs reliably on Python 3.12
