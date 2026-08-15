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
| `metadata_schema_version`, `execution_protocol_version` | Immutable integer metadata and execution-contract epochs selected by the package-owned producer |
| `created_with_django_ray_version` | Nullable diagnostic package version that created the task chain; historical writers remain unknown |
| `managed_with_django_ray_version`, `executor_django_ray_version` | Nullable attempt-scoped diagnostic manager and executor package versions |
| `args_json`, `kwargs_json` | Serialized arguments, or JSON `null` placeholders for external input |
| `input_reference` | Optional durable pointer to a versioned combined input envelope |
| `result_data` | Inline JSON result when under size limit |
| `result_reference` | Pointer used when result exceeds `MAX_RESULT_SIZE_BYTES` (`digest`, `filesystem`, `s3`, `gcs`) |
| `progress_data` | Current schema-v1/v2 compatibility snapshot of retained actor state; actor-side rejection/truncation and fixed-shape, secret-free cost diagnostics remain in the envelope |
| `workflow_progress_summary_json` | Nullable canonical schema-v3 summary, capped at 16 KiB encoded; may hold lifecycle-authored evidence, one accepted terminal-only summary, or an accepted default-off terminal-pilot publication |
| `workflow_run_id` | Current workflow run allowed to update either progress representation |
| `workflow_run_namespace` | Nullable opaque 63-bit namespace reserved under a database uniqueness constraint when the row first allocates a fresh workflow run; legacy rows remain null until then |
| `workflow_run_sequence` | Internal non-resetting 59-bit fresh-allocation counter combined injectively with the row namespace in each new workflow UUIDv8 |
| `runtime_env_profile` | Optional name selected by the enqueueing backend |
| `runtime_env_json` | Canonical immutable plaintext mapping or strict versioned AES-256-GCM envelope used by execution and retries; the write format is opt-in while readers always support both |
| `runtime_env_hash` | Unkeyed SHA-256 identity of canonical plaintext used for integrity checks and cache correlation; it remains visible in encrypted mode and leaks equality |
| `error_message`, `error_traceback` | Original protected failure evidence; ordinary readers expose terminal-inert, pattern-redacted, surface-bounded projections, while the separately authorized sensitive Admin reader skips only pattern redaction |
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

The generated UUID is only a candidate; the lease primary key and retained in-flight
task claims are the allocation authority. Startup inserts a new row before initializing
Ray or printing the worker identity, then proves that no `RUNNING` or `CANCELLING` row
still names that ID. This second fence matters after the supported Admin cleanup has
deleted an inactive lease whose orphaned work still awaits reconciliation. SQLite and
PostgreSQL primary-key violations, or an in-flight owner reservation, are retried with
a fresh candidate a bounded number of times. Any other integrity or database error
fails startup closed. Existing active or inactive rows are never adopted during initial
allocation.

After acquisition, heartbeats, queue expiry, task claims, and graceful release use the
immutable `(worker_id, hostname, pid, started_at)` snapshot. Renewal also requires the
exact row to remain active and inside its lease duration. Expired, inactive, deleted,
or replaced ownership is irrevocably lost; the old process cannot reactivate or
recreate it. The worker holds that live-row fence across each bounded expiry and claim
transaction, including a write fence on SQLite where row locking is unavailable.

Recovery transactions lock every involved lease in worker-ID order before the durable
execution. They prove the adopter's complete immutable identity is active and fresh,
reject a live source owner, mark a stale source inactive, and transfer task ownership.
Timeout, LOST, and cancellation recovery keep those locks through the corresponding
archive and any bounded state-changing remote stop, so competing workers cannot issue
the same recovery effect for one execution identity. Lease cleanup follows the same
order and skips recovery rows that are already locked on databases that support it.
The supported Admin bulk deactivation and inactive-lease deletion actions also lock
their selected rows in worker-ID order before mutation. Generic Admin deletion is
disabled so it cannot bypass the inactive-only guard or the lock protocol. Both
controlled actions require the model's change permission; view-only operators cannot
deactivate or delete leases.

Sync and Ray Core terminal writes use the command's captured worker ID rather than a
freshly loaded task owner, while Ray Core monitor heartbeats include that owner too.
Ray Job adoption occurs before reconciliation, and every result-storage, failure,
stop, and monitor-heartbeat mutation revalidates the exact live adopter lease after
its read-only status or log RPC. If a previous process resumes, its expired heartbeat
fails closed and stale in-memory tracking is retired without touching the adopted
task. This protects coordination from accidental identifier collisions and stale
owner resumption. Public cancellation remains durable best-effort intent: neither the
lease protocol nor a remote cancellation response is an exactly-once guarantee.
Ray Core cancellation waits at most five seconds for graceful `ray.cancel` and
then records an indeterminate outcome while retiring only the exact tracked
`ObjectRef`. The cancellation RPC may still finish in its daemon thread, but that
late return cannot remove or target a replacement attempt. This bounds the worker's
lease and execution locks without claiming that the remote task has stopped. One
process-wide cancellation slot prevents a wedged Ray Client from accumulating daemon
threads across runner reconnects; later exact handles are retired with an indeterminate
outcome until the in-flight RPC returns.
Application administrators and database writers remain trusted not to forge
worker-managed state.

Graceful handoff is an ownership mutation, not an exception to the protocol. Before
requeueing a claimed-but-unsubmitted task, cancelling a Ray Core handle, or releasing
a Ray Job for another monitor, shutdown revalidates the complete live lease and holds
the lease-to-execution lock order through the effect and durable update. A process
whose lease expired while paused or before a signal arrived leaves task rows untouched
for a live owner to recover.

Shutdown handoff, lease release, and Ray disconnection are attempted independently, so
a handoff or release database failure retains a failure exit while later cleanup still
runs. A release database error remains distinguishable from an ownership-fence miss.
Drain old task managers before deploying this protocol: older code can still overwrite
an existing lease row and therefore must not run in a mixed-version worker fleet.

### `TaskInputPayload`

Typed registry and cleanup tombstone for content-addressed external payloads. It records
whether a reference contains a task-input envelope or a Ray Job execution request, plus
the backend, digest, byte size, envelope version, last-use time, cleanup state, and
cleanup error. Execution rows use separate `input_reference` and
`ray_job_request_reference` links so retry and retention cannot confuse the callable's
durable arguments with the latest submitted Ray Job request. An automatic retry retains
the prior job ID, address, and request reference as one audit/reconciliation tuple until
the fresh claim clears all three; an explicit retry clears that tuple immediately. Row
locks on the registry and both referencing columns prevent cleanup from deleting a
payload while another writer is registering the same content. A kind mismatch or a
reference present in both columns is ambiguous and remains retained. Every writer that
attaches or reactivates either kind must use the same registry-then-execution lock order;
the rq2 request writer and its definite pre-submission release path use that ordering so
cleanup cannot delete an object between registration and attachment.

Migration `0021` adds this typed registry and request-reference link as dormant schema.
Existing and released writers omit the new kind and receive the database default
`task_input`; no Ray Job changes transport merely because the migration is applied.

### `TaskAttempt`

Each terminal transition records the one-based attempt number, execution protocol,
attempt-scoped manager and executor provenance, state, result references, and failure
diagnostics in `TaskAttempt`. The current
`RayTaskExecution` row remains the source of truth for scheduling, while this
history makes retries auditable after the current row is reset for its next
attempt. Creator provenance remains on the task chain rather than being copied into
each attempt. Admin retries, the operational retry API, and automatic worker retries
all use the same row-locked lifecycle service, increment the attempt counter, and clear
the current manager/executor fields after archiving them so the replacement attempt
cannot inherit diagnostic ownership it has not earned. When
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
Fresh workflow-run allocation, exact run reclaim, and plan pins do so as well, including
idempotent plan verification.
The exact current-run fence after RuntimeEnv preparation refreshes activity before any
workflow leaves are submitted.
A fresh allocator never accepts a caller-selected run ID. Under the task row lock it
reserves an opaque 63-bit namespace through a database uniqueness constraint, advances a
non-resetting 59-bit row sequence, and injectively encodes both values in a UUIDv8. A
namespace collision is retried only when the database names that exact constraint;
unrelated integrity failures propagate, while bounded collision or sequence exhaustion
fails closed without candidate or task data. The allocator then clears superseded
progress projections and makes the new identity current. Reclaim is a separate operation:
it must present the exact current task, attempt, execution generation, and run ID, does
not allocate a namespace or advance the sequence, preserves normalized run storage, and
retains the existing restart behavior of clearing current snapshots before a new
coordinator republishes them.
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
stops the adopted job. A submission identity returned by the Jobs API is untrusted and
never becomes a control capability: if it differs from the deterministic durable
reservation, the runner emits a fixed acceptance-uncertain result without retaining,
logging, comparing through attacker-defined hooks, or stopping that returned value.
Reconciliation remains pinned to the exact job ID, address, and request reference that
were durably bound before submission. Discovery or cleanup of any differently named
remote job requires separately authenticated evidence; the raw API return is not enough.
Ray Job success, failure, stop, missing-envelope, and timeout
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

`django-ray` does not provide exactly-once execution. Queued work can expire or be
cancelled before application code begins. Retryable work that does begin can execute
more than once when a worker, Ray worker, Ray head, network connection, or process dies
after user code has performed side effects but before `django-ray` records the
successful result.

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
- New task managers build the same canonical flat versioned execution request used by
  Ray Core directly from the durable input JSON or opaque input reference. They enforce
  its one fixed UTF-8 ceiling, store the exact canonical bytes in the configured
  retrievable input backend, register and bind that request to the exact durable
  execution, and only then open the Ray client or upload RuntimeEnv artifacts. The
  submitted command contains only a bounded locator:
  - `python -m django_ray.runtime.entrypoint --request-ref-b64 <...>`
- Control-only runner construction and drain reconciliation do not require request
  storage. The Ray Job task-manager command validates storage before creating its lease,
  and every direct/public submission validates it again before opening a Ray client.
- The public submission path reserves the exact durable rq2 tuple under a row lock. A
  concurrent caller never submits or releases that tuple and receives a fixed uncertain
  result until the reservation owner either crosses the Ray request boundary or releases
  a definitely unsubmitted failure.
- The request carries the callable, input transport, complete task identity, execution
  protocol, and bounded RuntimeEnv identity. Rq2 metadata carries only fixed protocol
  markers, an opaque coordination digest, the execution protocol, the request digest and
  byte count, and hashes of the persisted request reference and exact locator token. It
  does not disclose the
  public task ID, callable, argument values, raw RuntimeEnv identity, or storage
  credentials. A replacement manager validates that bounded metadata against the exact
  persisted ID and reference; it does not retrieve request bytes from JobInfo or infer
  effects from the command.
- An rq2 driver first validates the bounded canonical locator and its metadata binding,
  retrieves the content-addressed request through the locator's non-secret allowlisted
  storage coordinates, and validates its declared size, SHA-256, canonical schema,
  identity, protocol, and inner input transport before Django setup, input hydration,
  application callable import, or invocation. Credentials remain ambient workload
  identity or environment and never enter the locator. Any rq2 marker opts the whole
  submission into this check and cannot fall back to rq1 or the legacy adapter.
- Applies the same persisted RuntimeEnv snapshot to the submitted Ray Job.
- Carries durable task identity into the driver and initializes Ray lazily for
  nested workflows, giving Ray Job and Ray Core the same graph/progress protocol.
- The driver persists a structured completion envelope on `RayTaskExecution` before
  exiting. Reconciliation uses this durable channel for success/failure and treats
  missing or malformed envelopes as initially non-terminal, then applies bounded
  recovery without duplicating a still-active Ray Job; Ray stdout/stderr is diagnostic
  only.
- Workers selected for the execution's queue can adopt orphaned persisted Ray Job
  handles from inactive workers and continue reconciliation instead of immediately
  retrying duplicate work. Queue selection is checked before orphan reconciliation,
  timeout recovery, cancellation takeover, or Ray status/stop I/O.
- A strict request rejection has one fixed driver exit classification, but an exit code
  cannot prove how far the driver progressed or whether application effects occurred.
  Every strict terminal driver with a verified binding but no exact completion envelope
  therefore waits for the publication grace period and receives one generic fixed
  non-retryable outcome. An unverifiable binding instead follows the exact-stop,
  `LOST`, no-auto-retry quarantine. Reconciliation never fetches logs for authority or
  automatically replays either class of work.
- Released unversioned protocol-v1 payloads and the earlier strict rq1 inline transport
  remain explicit drain adapters. New submissions are rq2 only. Because all three carry
  execution protocol `1`, operators must retire older task-manager claimers and close
  legacy admission before treating a deployment as reference-only; already submitted
  legacy/rq1 jobs can still drain under upgraded reconciliation.

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
`0022_ray_target_persistence` before starting upgraded workers:

```bash
python manage.py migrate django_ray
```

This command applies every unapplied `django_ray` migration through the current leaf.
Migration `0022` adds only dormant target intent and verified-attestation history;
current workers do not consume it for capacity, claims, or routing.

Migrations `0007` and `0008` add priority with a neutral default and enforce its
`-100` through `100` range. Migration `0008` is intentionally non-atomic:
PostgreSQL adds the constraint as `NOT VALID` before validating it, while other
databases add the check directly.

Migration `0009` adds the durable input registry and nullable task reference. Deploy
the new code everywhere while `MAX_INLINE_INPUT_SIZE_BYTES` remains `None`, then drain
old Ray Job drivers before enabling spillover. Existing inline rows remain valid;
referenced Ray Jobs use transport version 2 and contain only `input_reference`. Before
rolling back, disable spillover and drain every task that already has a reference.

Migration `0021` adds the separately typed Ray Job request reference used by rq2. It is
additive and dormant under 0.4.0 writers, but an rq2 task manager requires a retrievable
`INPUT_STORAGE_BACKEND` even when argument spillover remains disabled. Configure the
same backend namespace and ambient credentials for task managers and Ray Job drivers,
deploy the final rq2 reader, stop every 0.4.0 and intermediate rq1 task-manager claimer,
upgrade or disable every older scheduled/manual input-purge command, and close the
existing legacy-admission latch before resuming new Ray Job claims. Keep the old
namespace available until every retained request reference and purge tombstone expires.
Do not activate protocol `2`; rq2 is an outer carrier for protocol `1`.

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

Migration `0017` adds only the `django_ray.view_sensitive_task_data` permission to
the `RayTaskExecution` model state. It does not add a database column, rewrite task
rows, or grant the permission to any user or group. Apply it before delegating access
to the separate pattern-unredacted, terminal-inert Admin views. Before reversing it,
revoke every user and group grant explicitly. Django's permission synchronization
creates missing custom permissions but does not delete stale permission rows during a
schema rollback.

Migration `0018` adds the nullable workflow-run namespace and the non-null allocation
sequence. Its persistent database default of zero lets pre-`0018` enqueue writers omit
the sequence during a schema-first rollout or code-only rollback. Existing and
old-writer rows therefore enter the compatibility state with a null namespace and
sequence zero; their active run IDs remain unchanged and exactly reclaimable. Drain
older workflow coordinators and workers before starting 0.4 code so only the new
allocator can create fresh workflow ownership under the strengthened guarantee. For a
code rollback, stop new coordinators and drain active workflows first, retain migration
`0018`, and then start the old code; reversing `0018` separately drops the allocation
metadata and requires a stopped-writer maintenance window.

Migration `0019` establishes the schema-first execution-protocol boundary. It records
protocol `1` on every existing execution and archived attempt, classifies existing and
old-writer execution rows as metadata schema `0`, and leaves their package provenance
null. Package-owned 0.5 producers explicitly write metadata schema `1`, protocol `1`,
and their creator package version; an old writer that relies on database defaults
continues to produce schema-`0` rows with unknown creator provenance. Existing and
pre-capability worker leases use capability schema `0`, a null protocol range, and the
singleton legacy admission token; upgraded workers advertise capability schema `1`
and the explicit supported range `1` through `1`. Database fences keep execution,
attempt, and lease
capability identity immutable. While protocol `1` and legacy admission remain open,
the ownership fence is deliberately dormant so existing recovery behavior is unchanged.
For a future protocol, or after legacy admission closes, a new ownership transition is
rejected unless the active lease advertises a range containing the execution's protocol.
Package-owned producers always insert executions as `QUEUED`; the ownership fence is an
update boundary and does not validate unsupported direct insertion of an already-active
row. Custom SQLite migrations that rebuild an execution, attempt, or lease table must
reinstall the protocol triggers, and the current-schema migration test verifies that the
latest leaf still retains them.

The protocol-`1` backfill is supported only when the database comes from the exact
published 0.4.0 baseline at migration `0018`. A database whose nonterminal rows were
written directly by pre-0.4 code does not contain enough evidence to prove that those
rows implement protocol `1`; drain or cancel them, or complete an application-specific
audit, before applying `0019`. A migration number alone is not evidence that every
writer followed the 0.4.0 execution contract.

The seeded singleton policy has schema `1`, active write protocol `1`, legacy admission
open, and revision `1`. This is a deliberately dormant rollout boundary: `0019` does
not introduce a protocol-`2` writer, change the execution wire format, close legacy
admission, or expose policy mutation through Admin. Admin displays the policy and the
bounded lease capability as read-only operational evidence. The integer execution
protocol is the normative compatibility decision; `django-ray` package versions are
diagnostic provenance only and must not be used to infer admission or routing.

After a compatible claim or ownership adoption, the execution records the package
version of the manager for that current attempt. Terminal archival copies the exact
execution protocol plus the current manager/executor provenance into `TaskAttempt`
under the lifecycle row lock. Retry preserves the task-chain metadata, creator, and
protocol while resetting current manager/executor provenance for the replacement
attempt. Executor provenance is accepted only from a strictly validated versioned
completion and is written under that same lock before archival. It is never inferred
from the manager. A legacy or unversioned executor therefore remains null.

Routine execution reads annotate whether at least one heartbeat-live worker lease can
read the row's integer protocol. One observation freezes a single heartbeat cutoff and
accepts only a valid rollout policy plus valid explicit ranges, or the policy-controlled
legacy protocol-`1` capability while legacy admission is open. The annotation remains a
protocol-reader signal, not a routing or readiness claim: worker queue text is
informational, and `queue_capacity_attested` is always false. Ray/Python compatibility,
cluster identity, Ray connectivity, and free concurrency remain separate deployment
evidence. The bounded testproject status/list/detail projections and the Admin detail
and live summary expose this signal with creator, manager, and executor provenance.
Those HTTP and live-Admin reads guard each provenance value at 128 UTF-8 bytes in SQL
and apply configured presentation redaction. Historical or unreported provenance stays
null; an oversized stored value becomes null or the Admin's fixed unavailable display
instead of being transferred as an unbounded value.

The private protocol-coordination primitive is implementation infrastructure for the
later supported operator adapter; it is not itself an adopter-facing mutation API. A
changing call supplies the exact policy revision its caller reviewed, bounded to the
database positive-bigint range; an exhausted revision refuses mutation explicitly.
Closing also carries a caller assertion intended for the later operator adapter that
every capability-unaware enqueue producer has been retired because task-worker leases
cannot discover old web or API processes. Every active capability-schema-`0` lease blocks
closure, including a lease whose heartbeat appears stale; retirement remains an
explicit lifecycle operation rather than an inference made by activation.

PostgreSQL coordination calls first take one transaction-scoped advisory mutex.
Closure then locks existing token-linked legacy lease rows in worker-ID order, followed
by the singleton policy and admission token. This matches a heartbeat's lease-first
lock order and avoids a policy-to-lease deadlock. The policy lock serializes unaware
execution and active-lease inserts through the `0019` triggers, and the token lock also
serializes an unusual inactive historical lease insert. SQLite instead begins with an
exact no-op policy update so its database-wide writer fence is held before any rollout
state is read. Each transition must own the outermost database transaction, with
autocommit enabled before entry, so a caller cannot acquire locks before this ordering
begins. Both paths update the flag and revision, detach only inactive legacy lease
history, and delete the token in one transaction. A failure or stale expected
revision rolls back every change.

Reopening is also revision checked. It requires active write protocol `1` and no
`QUEUED`, `RUNNING`, or `CANCELLING` execution with another protocol, recreates the
singleton token before reopening the policy in the same transaction, and deliberately
does not relink inactive historical leases. A changing PostgreSQL reopen takes the
execution-table writer fence before the policy lock; an already-open idempotent check
locks only the policy so it cannot form a table-policy cycle with an admitted 0.4
worker. SQLite begins with its database writer fence.
Migration `0020` persists the resulting invariant by rejecting a non-protocol-`1`
nonterminal insert or terminal-to-nonterminal transition whenever legacy admission is
open. Installation fails closed if an already-open policy contains such work. It is a
prerequisite for a possible code-only rollback, not proof that upgraded producers,
task managers, RuntimeEnv
artifacts, or remote work have been retired safely. This service remains internal in
this slice: operators must not import or invoke it directly, and there is no mutable
Admin action, HTTP endpoint, public API export, or operator command yet.

An upgraded task manager now derives its claim capability from the exact schema-`1`,
token-free worker lease after that lease has been locked and proven live. Queued expiry
and claim selection apply the lease's inclusive protocol range before ordering and
limiting rows, so unsupported work cannot consume the bounded batch or be terminalized
by the wrong cohort. The authoritative lease-then-execution lock boundary binds the
snapshot protocol to the final row lock and refuses adoption or mutation when the
candidate lease does not support it. A protocol mismatch leaves the worker lease live
and the execution unchanged; it is not treated as lease loss. The database ownership
trigger remains the independent backstop for writers that do not use this application
path.

Ray Job reconciliation, stuck/timeout recovery, and cancellation processing capture
that same exact lease range in a short lease-first transaction before querying eligible
execution state. Production scans apply both the worker's selected queues and the
protocol range before iteration, so an out-of-queue or incompatible manager neither
contacts Ray nor changes the task's durable owner or source lease. Unsupported in-memory
Ray Job tracking is forgotten locally and may be recovered later by a compatible
cohort. The final remote or durable effect still passes through the authoritative
lease-then-execution lock, and candidate compatibility is checked before an inactive
source lease can be retired during takeover. Direct administrative calls that omit a
queue selection retain the all-queue recovery contract.

Package-owned lifecycle entry points also default to the package's supported execution-
protocol range and compare that range with the immutable protocol under the execution-
row lock. An unsupported cancellation or retry returns a distinct bounded status;
legacy retry returns no replacement row, terminal boolean transitions return false, and
queued expiry omits the row. Rejection happens before RuntimeEnv hydration, attempt
archival, cancellation effects, or state and provenance mutation.
Admin retry and cancellation report unsupported selections separately from stale or
otherwise ineligible work. Public cancellation and retry services always bind this
package build's supported range; application callers cannot widen it. Package-private
transition paths may receive an explicit range from an already-proven worker cohort,
but lifecycle code does not lock the rollout policy or a worker lease and therefore
does not change the lease-first ownership protocol.

Ray Core monitoring applies a second, driver-local boundary because its `ObjectRef`
handles cannot be transferred to another task manager. Before a task-specific
`ray.wait` or `ray.get`, the manager proves its exact live lease, compares the pending
handle with the durable owner, attempt, generation, state, and protocol, and retires
unsupported, missing, terminal, stale, or transferred handles locally. Only exact
compatible `RUNNING` or `CANCELLING` handles cross the Ray polling API. Unsupported
durable rows receive no heartbeat, result-storage call, retry, archive, or state change.
The monitor heartbeat holds the lease lock while updating only those exact compatible
rows with the same protocol predicate.

Ray polling itself occurs without database locks. A returned completion must then
re-enter the authoritative lease-then-execution boundary before cancellation,
result storage, success, failure, or retry. Disconnect and reconnect cleanup use the
same classification and authority check before routing a compatible handle through
loss handling. Retiring a local handle is not handoff or replay: another process cannot
recover its `ObjectRef`, so an unsupported or uncertain Ray Core row remains an explicit
drain or operator-decision boundary.

Completion consumption has its own private schema boundary. A flat versioned-v1
envelope preserves the legacy `success`, `result`, reference, and failure keys while
adding an exact completion schema, task primary/public identity, attempt, generation,
execution protocol, and bounded executor package provenance. Any reserved versioned
field opts the whole envelope into strict validation; a partial or mismatched envelope
cannot downgrade to the legacy adapter. The manager compares the complete identity and
protocol under the authoritative lease-and-task lock before it canonicalizes a result
reference, stores a result, changes lifecycle state, or records executor provenance.
Package Semantic Version remains diagnostic and never decides compatibility.

Unversioned 0.4 completions remain explicit protocol-v1 legacy envelopes. Their existing
success/failure behavior and bounded malformed-envelope recovery remain available during
a manager rolling handoff, but they cannot report executor provenance. A valid
versioned-v1 completion can also be consumed by an older permissive v1 manager because
the legacy outcome keys remain at the top level. In contrast, a versioned schema,
protocol, identity, or shape mismatch is uncertain: its result and reference are never
inspected or stored and it is never automatically retried. A still-active Ray Job is
quiesced by its exact durable identity and retained as `LOST`; a proven terminal
executor is recorded as a non-retryable failure.

Both adapters enforce a fixed whole-envelope byte, structure-depth, and node budget
before constructing the parsed tree. Exceeding that deterministic resource boundary is
non-retryable even for an unversioned envelope: the manager cannot safely scan beyond
the bound to prove its framing, and replaying the same completed work would create a
retry storm. Within that boundary the legacy adapter retains released-v1 JSON behavior,
including non-finite result numbers and long failure diagnostics.

Ray Core carries one canonical flat versioned-v1 execution request built from the
durable input JSON or opaque input reference rather than manager-hydrated application
values. The request includes the complete task identity, execution protocol, callable,
input transport, and bounded RuntimeEnv identity. Independent expected identity and
protocol primitives accompany the opaque request. The by-value Ray bootstrap compares
them and validates the bounded request before importing Django setup, input-storage, or
application-callable code. A malformed, unsupported, or mismatched request returns a
fixed non-retryable enriched completion without inspecting its application input or
invoking the callable. Released positional Ray Core calls remain a protocol-v1 legacy
adapter for managers that already shipped their older bootstrap by value.

If Ray returns no executor completion for a strict handle, the manager records a fixed
non-retryable transport failure without remote exception text or executor provenance.
The missing envelope cannot prove application quiescence or safe replay, so ordinary
automatic retry policy must not reinterpret that transport loss as a task failure.

Ray Job stores that canonical request in the retrievable input backend, binds the exact
reference to the execution, and passes only a bounded rq2 locator through the persisted
submission command. Independently bounded metadata carries an opaque coordination
digest, protocol, request content identity, request-reference hash, and exact canonical
locator-token hash. The driver validates the whole locator binding before storage I/O
and the retrieved canonical bytes before
Django setup, input hydration, or application callable import/invocation, then publishes
the same enriched completion.
The dedicated rejection exit classification is diagnostic only: without the exact
completion, reconciliation cannot prove the phase or absence of application effects.
Every strict terminal driver with a verified binding but missing that completion waits
for publication grace and then receives a fixed generic non-retryable outcome. An
unverifiable binding instead follows the exact-stop, `LOST`, no-auto-retry quarantine;
Ray Job logs are not authority for either case. Persisted strict job IDs plus bounded
identity and protocol metadata let a compatible replacement manager reconcile the same
job without rewriting its task identity or generation. Rq2 never falls back to the
earlier inline transports. Unversioned released payloads and rq1 remain protocol-v1 drain
inputs only; upgraded managers continue to reconcile them while their already submitted
jobs finish.

Strict outer Ray Core and Ray Job contexts now extend the same immutable task identity
and execution protocol through nested workflow steps, result-fold actors, and
distributed map, starmap, and scatter leaves. Each leaf receives one canonical bounded
request plus independent expected identity, protocol, boundary, callable, and RuntimeEnv
controls. Any partial strict controls reject without falling back to the released direct
call shape. Workflow and fold requests bind the exact run, node, and primary callable
path. A workflow-step request also binds the exact nullable output-preview callable path
and compares it with the independent leaf argument before either application callable
is imported; that wire field must be null for every other boundary. Distributed
requests bind an operation/index and the SHA-256 digest of the still-opaque pickled
callable. The transported RuntimeEnv plan identity retains its exact schema and checksum
and is descriptive of the selected environment, not live cluster attestation.

A workflow leaf validates before Django setup or callable import, then installs the
decoded strict context around invocation so deeper nesting remains fenced. A result-fold
actor validates before setup, initial serialization, or reducer import and reports a
typed rejection from its required ready acknowledgement before mapped leaves are
admitted; reducer calls run under the same context. A distributed leaf validates before
Django setup, django-ray's callable `pickle.loads`, or invocation. Ray has necessarily
already deserialized the remote bootstrap, request primitives, and ordinary Ray
arguments before any Python leaf body executes.

A typed nested-request rejection remains fixed and pickle-safe through bounded
`RayTaskError.cause` unwrapping. The outer completion records only its fixed classifier,
no remote traceback, and `retryable=false`; it is never automatically replayed because
sibling leaves may already have produced effects. Marker-free released direct calls
remain the protocol-v1 compatibility path, while an explicitly strict context cannot
downgrade. These boundaries still do not prove cross-version cloudpickle compatibility
or replace the separate exact Ray/Python and cluster-instance attestation required
before serialization and submission.

This completes the still-unreleased 0.5 explicit protocol-`1` worker contract. The
supported rolling boundary is released 0.4 legacy/schema-`0` workers versus one exact
final 0.5 candidate; intermediate development snapshots that advertised schema `1`
before this boundary landed are not a supported cohort and must be stopped and drained.
Protocol fields deliberately do not encode Git commits or package Semantic Versions.

The guarded local KubeRay gate validates that boundary with the real released and current
manager implementations rather than synthesizing their lease metadata. A manager built
from the pinned released `v0.4.0` tree acquires a capability-schema-`0` lease and submits a
slow protocol-`1` Ray Job through its released transport. After that manager stops, the exact
current candidate's explicit schema-`1`, `1..1` lease must adopt the same persisted job,
attempt, and generation without a second submission. A separately deferred protocol-`1`
row must also remain byte-for-byte queued across the replacement, then complete from that
same durable row through one current request-reference submission. A separate test-only
protocol-`2` row is first staged in a terminal state while legacy admission is open. After
the released manager and its exact schema-`0` lease are removed, the gate revision-checks
and closes legacy admission and moves that exact row to `QUEUED`; active write protocol
remains `1`. The row must remain unchanged and visible as unsupported to protocol status,
authenticated API projections, and fixed-label metrics, while a direct strict Ray Core
executor request rejects it before application invocation and leaves its unique marker
absent. Ray Core retains the specific unsupported-protocol classification without creating
a gate-only Ray Job transport. No production writer or live lease advertises protocol `2`
or `1..2`. Cleanup returns the exact fixture to a terminal state before reopening legacy
admission, deletes it after a consistent token exists at the next monotonic revision, and
restores current-manager scaling before passing evidence is emitted. The terminal staging
row and reserved release-manager hostname also let a later gate run identify and recover
only its exact interrupted residue; missing ownership, foreign residue, an orphan live lease,
or ambiguity fails closed. That recovery runs after the current application image identity is
pinned but before any live task submission, and repeats immediately before the handoff
certification.

`TaskWorkerLease.queue_name` remains informational and is not parsed as a durable queue
capability. Likewise, an execution-protocol-capable lease proves only task-manager
compatibility: it does not attest Ray connectivity, the Ray or Python version, or the
identity and membership of a target cluster. Those target-readiness and blue/green
drain guarantees require their separate compatibility boundary before a future status
surface can report end-to-end capacity.

The first target-attestation slice is deliberately Django-free and dormant. Its
canonical contract binds an operator target key and policy revision to the runner
family, one Ray cluster session, and an exact Ray/Python runtime tuple. A bounded Ray
2.56.0 adapter takes resource-state snapshots before and after one hard-affinity probe
on every live schedulable node. The cluster session and exact sorted node set must stay
unchanged across that interval, every node must report the expected tuple and its own
identity, and the resource-state and per-node counters must not regress. Those counters
advance on ordinary heartbeats, so the contract records both ends of the observation
boundary; it does not misrepresent them as a stable membership epoch.

This interval can detect a node set change visible at either snapshot, but cannot prove
that no transient join and leave happened wholly between them or after the final
snapshot. A later activation therefore still needs TTL renewal, fresh per-invocation
validation, and capability withdrawal. The probe slice adds none of those database,
enqueue, claim, adoption, routing, or drain effects. It supports Ray Core and Ray Client
observation only. Ray Job needs a separate authenticated pre-Django response channel;
logs, process exit, rq2 metadata, and a Ray-writable shared payload object are not
authoritative attestation results.

Migration `0022` adds the next dormant boundary as three separate append-history
records. One immutable target binds the bounded operator key to its runner family,
cluster session, and exact Ray/Python tuple. Immutable policy revisions bind that target
to canonical expectation JSON, its digest, a compare-and-set revision, and the desired
`active`, `draining`, or reserved `retired` vocabulary. Immutable attestation revisions
retain only canonical proofs that matched the exact policy, including the expectation,
membership, and full-attestation digests plus their observation window. The private
coordination service registers Ray Core targets at policy revision `1` in `draining`,
permits only revision-checked `draining`/`active` transitions, and does not expose
`retired`; that transition remains reserved for #368's reviewed doctor, drain, and
retirement adapter. Ray Job persistence remains blocked on its authenticated pre-Django
proof channel.

Attestation history is verified-only. A mismatch, unreachable probe, identity drift,
malformed response, not-yet-valid proof, or expired proof is a rejection or read-time
status, never a fabricated negative observation row. The current verified/expired
meaning is derived from the latest matching immutable proof and its bounded expiry
rather than stored as a mutable status that could outlive the evidence. Policy changes
append a new revision and cannot rewrite earlier policy or attestation history. The
coordinator owns that append discipline; database guards reject material updates and
invalid identity, digest, or byte-bound inserts. Exact no-op updates and deliberate
maintenance deletion remain possible.

This persistence slice still has no task or attempt target field, worker target
capability, lease relationship, enqueue selection, claim or adoption predicate,
reconciliation/cancellation/retry fence, routing change, status/Admin/operator surface,
renewal loop, or blue/green activation. In particular, applying `0022` alone cannot
advertise capacity or move work. Released code ignores the new tables, so a code-only
rollback retains `0022` and its audit history; reversing the schema is a separate
stopped-writer operation. The reverse migration refuses while retained target history
exists, so an operator must export or audit it and deliberately delete it before retrying
the destructive reversal.

The read-only protocol-status service exposes only the database facts this boundary can
support. One versioned immutable report aggregates policy/token consistency, active and
heartbeat-stale task-manager leases, explicit or policy-controlled protocol ranges, and
bounded nonterminal queue/state/protocol groups. It derives both protocol-only
unsupported work and the exact nonterminal count lacking a heartbeat-live explicit
upgraded reader, so legacy-reader retirement cannot be mistaken for capacity. The
builder owns one consistent read-only database snapshot and bounds queue text in SQL
before materialization. `queue_capacity_attested` remains false and fixed blocker codes
retain every unprovable external retirement requirement. Text and canonical JSON render
from the same report, cap repeated sections with exact omitted counts, stay within a
fixed UTF-8 byte budget, and disclose no task, worker, host, callable, error,
package-version, or payload identity. Building or rendering the report never locks for
mutation and never changes rollout state; every changing transition rechecks its own
durable preconditions.

A code-only rollback and a schema reversal are different operations. To return to exact
0.4.0 code, first keep the policy at protocol `1` with legacy admission open, verify
that nonterminal work is protocol `1`, stop upgraded task managers, and reconcile their
in-flight work; retain migration `0019` so old writers receive its legacy database
defaults and token, and retain `0020` so reopening cannot race incompatible work.
Reverse `0020` and then `0019` only in a separate stopped-writer maintenance window
after confirming no retained diagnostics require their fields. Reversal removes the
protocol and provenance columns, worker capability metadata, singleton policy and token,
and database fences; it is not required for a code rollback.

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
protocol. This release's explicit legacy-v1 adapter permits compatible 0.4 completions
to finish while upgraded task managers reconcile them, and the flat enriched-v1 shape
retains the old top-level outcome keys. A future incompatible request or completion
schema still requires a reader-first rollout and a compatible manager cohort until every
older in-flight Ray Job drains. Never retry an uncertain remote execution merely to
complete an upgrade; first prove its exact remote identity and quiescence.

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
- Backend result retrieval rehydrates authorized `result_reference` payloads only after
  provider/stat size, bounded raw-byte, SHA-256, and UTF-8 verification. New references
  are canonical; the legacy reader is limited to v0.2/v0.3 object-key encoding.
- Versioned, content-addressed input envelopes with retrievable filesystem, S3, and GCS backends;
  strict references are authorized before client construction, while historical reads and cleanup
  dispatch across configured retained namespaces by validated scheme. Input/result namespaces are
  disjoint because input retention may delete objects on its independent lifecycle.

## Observability Surfaces

- Django admin for task/lease inspection and operations.
- Redacted task and attempt details for ordinary operators, plus a separate GET-only,
  non-cacheable allowlist for users who hold both ordinary object view and
  `django_ray.view_sensitive_task_data` on the owning execution. Database byte-length
  guards keep oversized raw values out of the application process; RuntimeEnv,
  completion, workflow, and log payloads remain outside this surface. Protected failure
  fields retain original diagnostic evidence so control-split redaction patterns remain
  detectable. Both surfaces render terminal controls inert; only the privileged surface
  bypasses secret-pattern redaction.
- Authenticated, polling-based live task state and workflow progress in the task admin.
- Versioned package services for task, queue, attempt, workflow, bounded paginated
  workflow detail, indexed nodes, and bounded live-Ray data.
- Workflow node and edge identities are accepted only when they are already valid UTF-8,
  bounded, and unchanged by terminal normalization/redaction. Opaque identities are
  rejected rather than rewritten, so normalization cannot merge distinct nodes.
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
