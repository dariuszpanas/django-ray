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

This boundary is explicit for Ray Core tracking:
`RayCoreRunner.pending_task_handles` returns stable, immutable handles carrying the
task ID, attempt number, and execution generation, while `pending_task_ids` remains a
convenience snapshot for identity-insensitive diagnostics. Completion, heartbeat,
connection-loss, cancellation, timeout, and shutdown paths compare the full identity
before changing durable state. `cancel_pending()` cancels only the exact handle still
owned by the runner. Handles returned directly from Ray Core submission retain that
in-memory capability; a reconstructed `ray_core:<pk>` value cannot select whichever
submission currently occupies the same database row. `clear_pending_tasks()` clears
local tracking without exposing the private object-reference registry.

When the worker prepares a successful inline or external result, preparation and the
terminal state transition run while holding the execution row lock. This prevents
cancellation, retry, or replacement from winning between that worker-owned write and
reference publication. Ray Job completion envelopes may contain a reference prepared
remotely before reconciliation; the lock fences adoption of that reference, while
writer-owned staging and crash-retention cleanup remain separate follow-up work.

### Ray Runtime

- Executes submitted functions.
- Returns completion state and result/error payloads.
- Resolves workflow step dependencies through object references without a database
  round trip for each internal step.

### Workflow definitions, plans, and strategies

The public `WorkflowSignature` builders are reusable definitions, not persisted DAGs.
The architecture separates four layers:

1. a workflow definition built with `step`, `chain`, `group`, and `map_step`;
2. a versioned, immutable effective execution plan with invocation values removed;
3. one attempt- and generation-scoped durable run with one or more invocations; and
4. an execution strategy such as local execution, dynamic Ray tasks, static actors, or
   a future Compiled Graph adapter.

One `RayTaskExecution` remains the durability and recovery boundary. Logical plan nodes,
runtime map expansions, physical actors, and prepared graph instances do not create
independent Django task identities. Compiled Graph is therefore an execution strategy
for an eligible static actor region, not a new task type.

The effective plan is canonical and secret-free. Its fingerprint covers callable/code
identity, topology, physical layout, resolved RuntimeEnv identity, resources, bounds,
transport, lifecycle, and compatibility inputs. Current inventory, task arguments,
credentials, and other per-invocation values are bound separately. See
[Workflow Plans and Execution Strategies](workflow-plans.md) and
[ADR-0001](design/adr-0001-workflow-plan-contract.md). The first experimental
Compiled Graph session is owned by one Ray Core outer-task process for one durable run;
it does not survive a scheduled task. Current evidence is limited to the
`direct-ray-core` submission transport. The compatibility identity records submission
transport separately, so django-ray's production Ray Client-submitted path cannot
inherit that row and still needs a live-cluster lifetime probe. See
[ADR-0002](design/adr-0002-compiled-session-ownership.md).
That identity also fails closed unless it names a specific container, immutable
deployment/image digest, and explicit shared-memory and object-store profiles; a
generic host or container observation cannot authorize native compilation.

Compiled invocation lifecycle is a separate Ray-free boundary. The version 1 reducer
keeps session preparation/health/teardown state independent from each invocation's
admission/submission/output/outcome state. Session events carry the complete durable
run identity; invocation events add `invocation_id`. It issues one deterministic action
token at a time, applies distinct absolute deadlines capped by the outer task deadline,
closes strategy fallback before preparation, and forbids same-invocation replay when
submission starts.

The reducer also accounts for every one-shot output before graph reuse and keeps
primary outcome, effect certainty, graph health, future durable-retry disposition, and
cleanup diagnostics separate. Its bounded snapshot contains no Ray handles or result
values. The exact protocol version is a fingerprinted plan requirement at
`strategy_requirements.compiled_graph.lifecycle_protocol_version`. See
[ADR-0003](design/adr-0003-compiled-invocation-lifecycle.md). No native execution
adapter or verified Compiled Graph capability is introduced by this state machine.

Workflow progress storage has a separate strategy-neutral decision. ADR-0004 replaces
the current complete task-row graph design with an always-bounded summary plus
database-backed immutable topology pages and normalized latest-state node-detail rows.
Publication writes and verifies detail before conditionally advancing the summary
pointer through the exact #81 run fence. Static topology is run-scoped and is not
duplicated for each ADR-0003 invocation. Detail has exact availability, size,
retention, authorization, cursor, corruption, and cleanup contracts; periodic Admin
polling defers both durable payload fields and selects progress through one bounded
compatibility query. The nullable schema-v3 summary fields, strict codec, fenced writer
primitive, rolling reader, terminal attempt archival, topology/detail tables, atomic
storage writer, and retention cleanup are implemented. The standalone summary writer
rejects topology/detail pointers. The package-owned storage transaction alone may
promote a verified pending manifest, apply sparse latest-state changes, and advance
the summary pointer together. A summary-only `DISABLED` or `OMITTED_BY_POLICY` update
creates no topology or detail rows. The current workflow actor deliberately continues
to publish schema v2 during full-mode execution. When
`WORKFLOW_PROGRESS_SCHEMA_V3_PILOT` is explicitly enabled, the actor and one terminal
publication attempt use the narrower `schema-v3-pilot-v1` profile. The adapter
revalidates the pinned plan, complete snapshot, ingress evidence, and exact run fence,
then stages and atomically promotes topology, detail, and summary. Any rejected or
truncated ingress, invalid or over-limit evidence, preparation truncation, stale
ownership, or storage failure refuses publication without changing the application
result or removing schema-v2 compatibility evidence. The package default remains
disabled.

An invocation can instead select terminal-only reporting. Its versioned bounded plan
selection retains the effective policy and execution strategy, while no progress
actor, node or application-progress RPC, or legacy snapshot is created. Durable success
or failure makes one best-effort schema-v3 summary publication through the exact run
fence. The summary records pinned plan identity, declared counts, terminal outcome, and
bounded timestamps, but records zero discovered or executed nodes and
`OMITTED_BY_POLICY` detail. It creates no topology or detail row. Publication failure
is observational and never replaces the application result or error.

Disabled reporting uses the same actor-free execution path but creates no schema-v3
summary. Full remains the default, and authorized paginated services are implemented.
Terminal-only is not a substitute for the remaining full-mode live-ingestion,
composite-preparation, aggregate-spill, capacity, migration, and old-writer-drain
work. See
[ADR-0004](design/adr-0004-bounded-workflow-progress.md) and
[ADR-0005](design/adr-0005-bounded-workflow-preparation.md).

### Database

- Canonical source of truth for task lifecycle state.
- Stores worker leases for cross-worker coordination.

## Data Model

### `RayTaskExecution`

Primary execution record for one task attempt chain.

| Field | Notes |
|---|---|
| `id` | `BigAutoField` primary key |
| `task_id` | Globally unique Django task-result identifier; UUIDv4 candidates are committed under a database uniqueness constraint with bounded collision retry |
| `callable_path` | Dotted import path for callable |
| `queue_name` | Queue used for claim/execution |
| `priority` | Django priority from `-100` to `100`; larger values are claimed sooner |
| `state` | `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `CANCELLING`, `LOST`, `EXPIRED` |
| `attempt_number` | Current attempt counter |
| `args_json`, `kwargs_json` | Serialized arguments, or JSON `null` placeholders for external input |
| `input_reference` | Optional durable pointer to a versioned combined input envelope |
| `result_data` | Inline JSON result when under size limit |
| `result_reference` | Pointer used when result exceeds `MAX_RESULT_SIZE_BYTES` (`digest`, `filesystem`, `s3`, `gcs`) |
| `progress_data` | Current schema-v1/v2 compatibility snapshot of retained actor state; actor-side rejection/truncation and fixed-shape, secret-free cost diagnostics remain in the envelope |
| `workflow_progress_summary_json` | Nullable canonical schema-v3 summary, capped at 16 KiB encoded; may hold lifecycle-authored evidence, one accepted terminal-only summary, or an accepted default-off terminal-pilot publication |
| `workflow_run_id` | Current workflow run allowed to update either progress representation |
| `runtime_env_profile` | Optional name selected by the enqueueing backend |
| `runtime_env_json` | Canonical immutable plaintext mapping or strict versioned AES-256-GCM envelope used by execution and retries; the write format is opt-in while readers always support both |
| `runtime_env_hash` | Unkeyed SHA-256 identity of canonical plaintext used for integrity checks and cache correlation; it remains visible in encrypted mode and leaks equality |
| `error_message`, `error_traceback` | Failure metadata |
| `ray_target_address` | Immutable Ray Job routing target selected by the enqueueing backend |
| `ray_job_id`, `ray_address` | Runner-specific execution handle metadata |
| `claimed_by_worker` | Worker lease owner that currently owns the task |
| `run_after` | Delayed/retry scheduling timestamp |
| `timeout_seconds` | Optional timeout from the selected backend's `OPTIONS["TIMEOUT_SECONDS"]` |
| `queue_timeout_seconds`, `queue_deadline_at` | Snapshotted nullable queue policy and indexed absolute expiry deadline |
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
all use the same row-locked lifecycle service and increment the attempt counter. When
the current run has already published an accepted canonical terminal schema-v3
summary, `workflow_progress_summary_json` stores those exact bounded bytes on the
attempt. If timeout, loss, cancellation, or another lifecycle owner wins first, the
same row lock derives a terminal envelope from the last accepted running summary and
archives it before cleanup. Legacy complete graphs and malformed or noncanonical
summaries are never copied into attempt history.

Cancellation entry points likewise use one authorization-neutral, row-locked package
service. Queued work becomes terminal and is archived under that lock. Running work
moves to `CANCELLING`, after which a worker owns best-effort backend interruption and
terminal finalization. The caller supplies its observed attempt number and execution
generation, so an older API or admin request cannot control a replacement attempt.
Both values matter because automatic retries advance the attempt number without
replacing the execution generation.

Stuck-task recovery also revalidates the observed worker owner, backend handle, last
task-monitor heartbeat, and absence of a durable completion envelope under that row
lock. Accepted current-run workflow progress advances that same task-monitor heartbeat.
Workflow-run claims and plan pins do so as well, including idempotent plan verification.
The exact current-run fence after RuntimeEnv preparation refreshes activity before any
workflow leaves are submitted.
A concurrent heartbeat, Ray Job orphan adoption, workflow activity, or completion
publication therefore invalidates a stale `LOST` decision instead of retrying work that
has become live or has already completed. An exact locally tracked Ray Core handle also
bypasses generic heartbeat-based loss recovery; Ray Core polling, timeout, disconnect,
and shutdown paths own that capability. Ray Job submission derives a deterministic ID
from the durable task, attempt, and execution generation and reserves that exact ID and
cluster address before making the submission request. A response timeout therefore
retains one reconcilable identity instead of launching another attempt; definite
pre-request failures release the reservation, while post-request confirmation errors
retain the durable capability for reconciliation and cannot retry automatically.
Only a genuine execution-identity replacement receives an exact stop. Exact active
Ray Jobs likewise bypass generic loss recovery. If their status remains `UNKNOWN`
past the stuck-task timeout, reconciliation marks the fenced execution `LOST`,
requests an exact best-effort stop, and suppresses automatic retry because remote
quiescence is not proven. An expired malformed or invalid envelope while Ray still
reports `PENDING` or `RUNNING` follows the same exact-stop, no-auto-retry path; only
a terminal Ray state can enter normal failure/retry handling. A submitter that outlives
its worker lease distinguishes a same-identity ownership handoff from
an execution replacement: it drops only its local tracker after handoff and never
stops the adopted job. Ray Job success, failure, stop, missing-envelope, and timeout
decisions also revalidate the exact observed completion envelope under the task row
lock, so a concurrently published completion remains available to reconciliation.
The address-pinned Ray Job client applies a five-second HTTP request timeout to its
version check and lifecycle status, stop, and log calls. A timed-out status becomes
`UNKNOWN`; a timed-out stop becomes `INDETERMINATE`, allowing any held execution-row
lock to be released with a durable outcome. Ray Client, `auto`, and GCS address
discovery are prepared before acquiring that row lock; the exact already-prepared
stop capability is used only after the lock revalidates ownership and execution
identity.
Reconciliation consumes a valid durable envelope even while Ray briefly still reports
the wrapper process as running. Cancellation returns `COMPLETION_PENDING`, and timeout
recovery skips the row, when publication already won the lock.

If lifecycle owns a successful transition before the producer publishes terminal node
states, it preserves authoritative aggregate success while marking retained detail
`TRUNCATED` with `terminal_state_unreported`. The normalized rows remain last-observed
rather than being rewritten in an unbounded terminal update. Producer-authored complete
terminal detail remains `AVAILABLE`.

### Workflow progress detail storage

Migration `0013_workflow_progress_detail_storage` adds package-owned, run-scoped
tables without changing existing task or attempt columns:

- `WorkflowProgressRunStorage` binds the complete task, attempt, generation, and run
  identity to the current detail revision, exact bounded aggregates, persisted
  retention policy, expiry, and a bounded cleanup diagnostic.
- `WorkflowProgressTopologyManifest` and `WorkflowProgressTopologyPage` retain one
  immutable current topology plus at most one bounded pending candidate. Ordered link
  rows associate run-scoped content-addressed pages with a manifest.
- `WorkflowProgressNodeDetail` retains at most one bounded latest-state record per
  stable node key. Its last-updated revisions are evidence, not a historical snapshot
  filter.

Node-detail schema version 2 may retain one opt-in, strictly bounded and redacted
author projection of a successful leaf result. The projection is diagnostics only:
it is never a result reference, checkpoint, retry input, selective-resume marker, or
external-effect receipt. Schema-version-1 rows remain readable without mutation.

Candidate topology is normalized, redacted, digested, and bounded before persistence.
Staging does not hold the lifecycle task lock; it checks the exact run at both ends and
can leave at most one bounded orphan when ownership changes at the final boundary. The
publication transaction locks the exact task and run, verifies manifest/page metadata,
counts, ownership, sizes, and digests, then promotes topology, applies sparse detail
changes, and advances the summary pointer atomically. A stale fence, corrupt candidate,
or summary conflict rolls back the current-state mutation instead of exposing partial
detail.

Those durable-storage bounds are now paired with spill-backed production topology
preparation. [ADR-0005](design/adr-0005-bounded-workflow-preparation.md) uses a
package-owned, private SQLite workspace for exact one-shot node/edge duplicate and
reference validation, canonical selection, and cleanup before capability issuance.
Only retained topology and bounded batches enter Python during that phase. The public
prepared value still materializes the complete `observed_node_ids` compatibility set
needed by initial detail, so this is not yet an end-to-end O(retained) preparation
claim. #142 completes composite detail preparation under
[issue #132](https://github.com/dariuszpanas/django-ray/issues/132). #79 separately
owns sampled/coalesced reporting, live wire and cost attribution, aggregate
producer/mailbox admission, producer backpressure, bounded actor-to-preparer draining,
and large-fan-out slow-consumer evidence.
The current pilot avoids claiming those broader boundaries by using a fixed profile of
512 nodes, 2,048 edges, 2 MiB of topology, 1 MiB of detail, and 4 MiB combined.
Default or higher-scale schema-v3 activation must compose all of the remaining
boundaries.

Terminal detail expiry is derived from the canonical terminal timestamp and
`WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS`. Every accepted detail publication records
the selected retention days on the exact run. A lifecycle-authored canonical terminal
summary owns the exact expiry and can extend an earlier producer deadline. If that
summary is missing or corrupt, a terminal transition falls back to its completion
timestamp plus the run's persisted policy. The cleanup command deletes only due
inactive runs and old unpublished orphans; task-row and attempt summaries survive
detail deletion.

## Task State Model

```text
QUEUED -> RUNNING -> SUCCEEDED
QUEUED -> CANCELLED
QUEUED -> EXPIRED
RUNNING -> CANCELLING -> CANCELLED
RUNNING -> FAILED
RUNNING -> LOST
FAILED/LOST/CANCELLED/EXPIRED -> QUEUED (authorized retry)
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
  missing or malformed envelopes as initially non-terminal, then applies bounded
  recovery without duplicating a still-active Ray Job; Ray stdout/stderr is diagnostic
  only.
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

Apply the linear `django_ray` migration sequence through
`0016_raytaskexecution_queue_expiration` before starting upgraded workers:

```bash
python manage.py migrate django_ray
```

Migrations `0007` and `0008` add priority with a neutral default and enforce its
`-100` through `100` range. Migration `0008` is intentionally non-atomic:
PostgreSQL adds the constraint as `NOT VALID` before validating it, while other
databases add the check directly.

Migration `0009` adds the durable input registry and nullable task reference. Deploy
the new code everywhere while `MAX_INLINE_INPUT_SIZE_BYTES` remains `None`, then drain
old Ray Job drivers before enabling spillover. Existing inline rows remain valid;
referenced Ray Jobs use transport version 2 and contain only `input_reference`. Before
rolling back, disable spillover and drain every task that already has a reference.

Migrations `0010` and `0011` add nullable workflow-run and effective-plan identity,
selection, and pinned-attempt fields. They do not rewrite legacy progress, and older
writers can continue inserting rows during the rollout.

Migrations `0012` and `0013` implement the additive reader-first progress-storage
boundary: nullable schema-v3 summaries followed by package-owned topology and detail
tables. Existing rows and older writers continue using `progress_data`; `0013` does
not backfill or reinterpret legacy snapshots. Schema v2 remains the live compatibility
writer for full mode. Terminal-only can add one summary-only schema-v3 record without
enabling topology/detail production, changing the database schema, or reinterpreting
legacy rows. The full-detail schema-v3 producer remains disabled by default and may be
enabled only as the strict terminal pilot after authorized bounded readers and storage
are deployed; enabling it applies the smaller actor and publication profile and does
not make hard-V1-scale production supported. Reversing
`0013` discards normalized detail tables, while reversing `0012` drops the summary
columns. Export any retained schema-v3 data needed for audit before either rollback;
legacy progress remains unchanged.

Migration `0014` adds a nullable immutable Ray Job routing target without rewriting
existing rows. New enqueues snapshot either the explicit backend-alias address or the
global `DJANGO_RAY["RAY_ADDRESS"]` fallback. New task managers promote a legacy
row's non-`"auto"` Ray Job `ray_address` into that target under the existing claim or
retry lock before clearing stale handle metadata; Ray Core handle addresses are never
promoted. Legacy writers also used `"auto"` when no alias target was configured, so
that ambiguous value remains on the global fallback.

Pre-`0014` task managers do not read the new target. Drain and stop them before relying
on backend-specific Ray Job routing, and drain tasks that contain only the new target
before reversing `0014`; the reverse migration drops that routing column. Ray Core
workers still select one cluster at process startup and do not dynamically route by
backend alias.

Migration `0015` makes the public Django task-result ID globally unique. Its preflight
refuses to choose an owner or alter rows when a legacy database already contains a
duplicate identity; the bounded diagnostic identifies row groups by primary key rather
than rendering stored IDs. Resolve every duplicate explicitly while producers and task
managers remain stopped, rerun the migration, and only then start upgraded code. New
enqueue paths let the database arbitrate UUIDv4 candidates and retry only a proven
collision. Reversing `0015` removes the constraint and therefore removes this integrity
guarantee.

Treat `0015` as a maintenance-window migration on a large execution table. The
duplicate preflight and unique-index build inspect existing task IDs and may consume
temporary storage or block writes according to the database backend. Measure the
migration against a production-sized staging copy, confirm free database capacity and
backup/recovery procedures, and keep every enqueue producer and task manager stopped
until the constraint is present. This release deliberately prefers one portable,
fail-closed migration over claiming an unproven zero-downtime index rollout.

Migration `0016` adds the snapshotted queue-wait policy and indexed absolute deadline.
Before applying it, stop old task managers, pause all enqueue producers, and preview the
queued backlog using the documented [queue-expiration procedure](tasks.md#queue-expiration).
Existing queued rows receive a deadline one day after `max(created_at, run_after)` unless
the migration process explicitly sets `DJANGO_RAY_EXISTING_QUEUED_UNLIMITED=1`; other
existing executions retain the 24-hour policy for any later retry. Pre-`0016` writers
cannot populate a deliberate snapshot and pre-`0016` task managers do not enforce
deadlines, so upgrade every enqueue producer before resuming traffic, start upgraded
workers only after the migration, and do not operate a mixed fleet.
Reversal maps `EXPIRED` current and archived attempts to `FAILED` before dropping the
policy fields. Stop upgraded workers and review all remaining queued work first because
pre-`0016` code can submit it without a deadline fence. Reversing `0016` leaves the
`0015` task-ID uniqueness constraint in place.

RuntimeEnv encryption has no schema migration. Its rollout is nevertheless
reader-first: deploy the dual plaintext/encrypted reader everywhere while writes remain
plaintext, then distribute the complete key ring to every enqueue, retry, admin, and
task-manager process before enabling encrypted writes. An application-level rollback
to plaintext writes remains readable only while the dual-reader code and all historical
keys stay deployed. A binary downgrade to a release that does not understand encrypted
envelopes is unsafe after the first encrypted row. Key rotation adds a reader key before
making it active and retains every old key until no durable row needs it; this release
does not rewrite or rewrap historical rows.

The completion envelope and `execution_generation` fields are part of the Ray Job
protocol. Drain Ray Job workers before deploying a version that introduces or changes
this protocol: let submitted jobs finish and reconcile, or explicitly verify remote
quiescence before retrying them; then stop the old workers, apply database migrations,
and start the new workers. Do not run a mixed old/new task-manager fleet or leave old
and new workers reconciling the same in-flight jobs, because an old driver may not write
the envelope or generation metadata required for the new worker to prove which execution
produced a terminal state.

## Reliability Controls

- Unified retry policy with denylist support (short and fully-qualified exception names).
- Worker lease heartbeat + cross-worker orphan recovery.
- Task monitor heartbeats for active reconciliation paths.
- Throttled, batched Ray Core task-monitor heartbeat persistence.
- Per-workflow in-memory progress coordination emits revision-based schema-v2
  compatibility snapshots of retained actor state. Bounded schema-v3 summary/detail
  storage and authorized readers are present, with a default-off, stricter terminal
  publication pilot. Terminal-only reporting bypasses the actor and legacy writer,
  then attempts one fenced summary-only terminal publication. Default and higher-scale
  full-detail activation still wait for the remaining ingestion, preparation,
  capacity, migration, and old-writer-drain work.
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
- Versioned package services for task, queue, attempt, workflow, bounded paginated
  workflow detail, indexed nodes, and bounded live-Ray data.
- Explicit terminal-only API and Admin summaries that never advertise topology,
  node-detail, or execution-graph surfaces.
- Package-owned Prometheus rendering with explicit queue-label allowlists and fixed labels.
- Worker logs for claim/submit/reconcile/retry events.
- Structured workflow-leaf logs correlated by durable task, workflow node, and Ray IDs.
- Optional Ray State API lookup for live task attempts and bounded stdout/stderr tails.
- Optional authenticated HTTP adapters in the `testproject` example app.

## See Also

- [Configuration](configuration.md)
- [Worker Modes](worker-modes.md)
- [Ray-Native Workflows](workflows.md)
- [Workflow Plans and Execution Strategies](workflow-plans.md)
- [ADR-0001: Workflow Plans and Execution Strategies](design/adr-0001-workflow-plan-contract.md)
- [ADR-0002: Compiled Session Ownership and Reuse](design/adr-0002-compiled-session-ownership.md)
- [ADR-0003: Compiled Invocation Lifecycle](design/adr-0003-compiled-invocation-lifecycle.md)
- [ADR-0004: Bounded Workflow Progress Storage](design/adr-0004-bounded-workflow-progress.md)
- [ADR-0005: Bounded Workflow Progress Preparation](design/adr-0005-bounded-workflow-preparation.md)
- [Runtime Environments](runtime-environments.md)
- [Retry & Error Handling](retry.md)
