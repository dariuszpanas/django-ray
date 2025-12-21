# django-ray — Implementation Plan

**Version:** Draft v0.1
**Runtime:** Python 3.12 only

This plan expands `requirements.md` and `ARCHITECTURE.md` into actionable development phases with deliverables, file/module breakdowns, acceptance criteria, and test strategy.

---

## 0. Guiding principles

* **Ship a usable MVP early**: DB-backed canonical state + Ray Job runner.
* **Prefer explicit interfaces** over implicit coupling.
* **At-least-once semantics**: reliability features focus on recovery, reconciliation, and idempotency support.
* **Operational simplicity**: one worker process type, easy settings, safe defaults.
* **Python 3.12 only**: enforce in packaging, CI, and docs.

---

## 1. Repository structure (target)

```
repo/
  django_ray/
    __init__.py
    apps.py
    conf/
      __init__.py
      defaults.py
      settings.py
    models.py
    admin.py
    management/
      __init__.py
      commands/
        __init__.py
        django_ray_worker.py
    runtime/
      __init__.py
      entrypoint.py
      import_utils.py
      serialization.py
      redaction.py
    runner/
      __init__.py
      base.py
      ray_job.py
      ray_core.py
      reconciliation.py
      leasing.py
      retry.py
      cancellation.py
    results/
      __init__.py
      base.py
      db.py
      external.py
    metrics/
      __init__.py
      prometheus.py
    tests/
      __init__.py
      unit/
      integration/
  docs/
    requirements.md
    architecture.md
    implementation_plan.md
  pyproject.toml
  README.md
```

Notes:

* `runtime/entrypoint.py` is what Ray calls to execute a single Django Task.
* `runner/` is control plane logic.
* `results/` is result storage.

---

## 2. Phase 0 — Scaffolding & Baseline (Foundation)

### 2.1 Objectives

* Create installable package scaffold
* Enforce Python 3.12
* Establish CI and quality gates

### 2.2 Deliverables

**Packaging & tooling**

* `pyproject.toml`:

  * `requires-python = ">=3.12"`
  * dependencies pinned for Django >= 6.0 and Ray (min version TBD)
* Code quality:

  * Ruff config
  * Black config
  * Type checking (Pyright or mypy)
* Testing:

  * pytest + coverage

**Documentation**

* `README.md` (project pitch, quickstart, compatibility)
* `docs/requirements.md`, `docs/architecture.md`, `docs/implementation_plan.md`

### 2.3 Acceptance criteria

* `pip install -e .` works on Python 3.12
* CI runs:

  * ruff
  * black (check)
  * typecheck
  * pytest

### 2.4 Tests

* Unit tests for settings parsing and import utilities stubs

---

## 3. Phase 1 — MVP: DB claiming + Ray Job runner

### 3.1 Objectives

* Execute Django Tasks on Ray via Job Submission
* Track status and attempts in DB
* Provide admin visibility

### 3.2 Milestone 1.1 — Django app + models

**Deliverables**

* `django_ray/apps.py` with app config
* `django_ray/models.py`

  * `RayTaskExecution`
  * optional `RayWorkerLease`
* Initial migrations

**Acceptance criteria**

* migrations run cleanly
* models visible in admin (even if minimal)

**Tests**

* unit: model creation, basic query indexes

### 3.3 Milestone 1.2 — Settings and configuration

**Deliverables**

* `django_ray/conf/defaults.py` and `settings.py`
* Configuration parser/validator:

  * required keys (RUNNER, RAY_ADDRESS)
  * concurrency defaults
  * redaction defaults

**Acceptance criteria**

* misconfiguration raises clear `ImproperlyConfigured`

**Tests**

* unit: settings validation

### 3.4 Milestone 1.3 — Worker management command

**Deliverables**

* `management/commands/django_ray_worker.py`

  * loop with:

    * lease heartbeat
    * claim runnable tasks
    * submit
    * reconcile

**Acceptance criteria**

* worker starts and logs health
* worker exits cleanly on SIGTERM

**Tests**

* unit: loop control, graceful shutdown

### 3.5 Milestone 1.4 — Claiming implementation

**Deliverables**

* `runner/leasing.py` (optional)
* `runner/reconciliation.py`
* DB claiming using `SELECT ... FOR UPDATE SKIP LOCKED`
* Ensure atomic transition QUEUED → RUNNING

**Acceptance criteria**

* multiple worker processes do not double-claim
* tasks are claimed in batches

**Tests**

* integration: two workers, one queue, ensure no duplicates

### 3.6 Milestone 1.5 — Ray Job execution adapter

**Deliverables**

* `runner/base.py`: base adapter interface
* `runner/ray_job.py`:

  * submit job
  * poll status
  * cancel job
  * fetch logs reference

**Design notes**

* Implement a structured `SubmissionHandle` containing:

  * `ray_job_id`
  * `ray_address`
  * `submitted_at`

**Acceptance criteria**

* submitted Ray job runs execution entrypoint
* job id recorded in DB

**Tests**

* integration: local Ray + job server, submit and poll

### 3.7 Milestone 1.6 — Execution entrypoint

**Deliverables**

* `runtime/entrypoint.py`:

  * bootstrap Django (`DJANGO_SETTINGS_MODULE`, `django.setup()`)
  * import callable by path
  * deserialize args/kwargs
  * execute
  * return structured outcome
* `runtime/import_utils.py`
* `runtime/serialization.py`

**Acceptance criteria**

* demo task executes and returns JSON result
* non-serializable payload fails deterministically

**Tests**

* unit: import resolution
* unit: serialization roundtrip
* integration: end-to-end execute

### 3.8 Milestone 1.7 — Result store (DB)

**Deliverables**

* `results/base.py`
* `results/db.py` implementing size-limited DB results

**Acceptance criteria**

* small results stored and retrievable
* large results raise or store pointer (configurable; MVP can fail)

**Tests**

* unit: size thresholds

### 3.9 Milestone 1.8 — Admin UI

**Deliverables**

* `admin.py`:

  * Task execution list
  * filters by state
  * admin actions: retry/cancel (retry can be a placeholder in MVP)

**Acceptance criteria**

* ops can inspect failures and ray ids

**Tests**

* unit: admin registration

### 3.10 Phase 1 acceptance criteria (MVP)

* Enqueue task via Django Tasks → executed on Ray → marked succeeded
* Failures capture traceback and show in admin
* Multiple workers do not double-execute

---

## 4. Phase 2 — Reliability (heartbeats, stuck detection, retries, cancellation)

### 4.1 Milestone 2.1 — Heartbeats

**Deliverables**

* heartbeat update interval in worker
* `RayTaskExecution.last_heartbeat_at` updates

**Acceptance criteria**

* active tasks show heartbeats

### 4.2 Milestone 2.2 — Stuck detection and LOST state

**Deliverables**

* configurable `STUCK_TASK_TIMEOUT_SECONDS`
* reconciliation marks attempts LOST if:

  * Ray status is unavailable AND
  * no heartbeat beyond timeout

**Acceptance criteria**

* stuck tasks eventually resolve to LOST

### 4.3 Milestone 2.3 — Retry policy

**Deliverables**

* `runner/retry.py`:

  * max attempts
  * exponential backoff
  * allowlist/denylist exceptions
* Requeue behavior with ETA

**Acceptance criteria**

* failure triggers retry until max attempts

### 4.4 Milestone 2.4 — Cancellation improvements

**Deliverables**

* `runner/cancellation.py`:

  * best-effort cancellation
  * state reconciliation to CANCELED

**Acceptance criteria**

* cancel request stops job where possible
* final state consistent

### 4.5 Phase 2 tests

* integration: kill worker mid-flight, ensure recovery
* integration: simulate Ray unreachability

---

## 5. Phase 3 — Throughput mode (Ray Core runner)

### 5.1 Milestone 3.1 — Ray Core adapter

**Deliverables**

* `runner/ray_core.py` implementing remote execution
* optional actor pool for concurrency

**Acceptance criteria**

* same Django-visible semantics as job runner

### 5.2 Milestone 3.2 — Per-queue concurrency

**Deliverables**

* concurrency controller enforcing per-queue limits

**Acceptance criteria**

* queue limits respected under load

### 5.3 Phase 3 tests

* integration: high-throughput benchmark

---

## 6. Phase 4 — Ecosystem polish (metrics, tracing, external results, examples)

### 6.1 Milestone 4.1 — Metrics endpoint

**Deliverables**

* `metrics/prometheus.py`
* expose metrics via:

  * Django view (protected)
  * or separate process (documented)

**Acceptance criteria**

* scrapeable metrics with queue depth and latency

### 6.2 Milestone 4.2 — Tracing hooks

**Deliverables**

* OpenTelemetry spans around critical operations

### 6.3 Milestone 4.3 — External result store

**Deliverables**

* `results/external.py` supporting S3/GCS/MinIO

### 6.4 Milestone 4.4 — Deployment examples

**Deliverables**

* `examples/k8s/` manifests for:

  * django web
  * django-ray worker
  * ray cluster
  * postgres

---

## 7. CI / Release Checklist

* Python 3.12 only
* Matrix:

  * Django 6.0.x
  * pinned Ray min/max
* Unit tests + integration tests
* Build and publish:

  * version tagging
  * changelog
  * compatibility statement

---

## 8. Definition of Done (per milestone)

* Code formatted and linted
* Types validated (no major type errors)
* Tests added for new behavior
* Docs updated where behavior is user-facing
* Backward compatibility preserved within a minor series

---

## 9. Risk register (initial)

1. Ray Job Submission API behavior differences across versions
2. Packaging and environment parity between Django and Ray workers
3. DB claim contention at high scale
4. Result size growth / DB bloat

Mitigations are addressed in Phase 2+ (reconciliation, external storage, throttling).
