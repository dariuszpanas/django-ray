# Operator Runbook

This runbook is for operators running `django_ray_worker` in staging/production.

It focuses on:

- fast incident triage,
- safe manual recovery actions,
- expected metrics and alert signals.

## Scope

This covers the `django-ray` library runtime:

- `RayTaskExecution` task lifecycle rows,
- `TaskInputPayload` durable-input registry and cleanup tombstones,
- normalized workflow-progress topology/detail retention,
- `TaskWorkerLease` worker heartbeat/coordination rows,
- `django_ray_worker` process behavior.

It does not cover custom business task logic internals.

## Quick Triage Checklist

1. Check worker process health (`django_ray_worker` logs, pod/process status).
2. Check task state distribution (`QUEUED`, `RUNNING`, `FAILED`, `LOST`, `EXPIRED`,
   `CANCELLING`).
3. Check active worker leases and heartbeat freshness.
4. Check Ray connectivity from workers.
5. Check whether failures are retrying or already terminal.
6. For input failures, verify the configured backend, object access, and registry state.
7. For workflow-progress cleanup failures, inspect the bounded `cleanup_error` code on
   the retained run or pending manifest without copying progress payloads into logs.

## Primary Signals

From `/api/metrics` in the example project:

- `django_ray_tasks_total{state="..."}`
- `django_ray_tasks_queued`
- `django_ray_tasks_running`
- `django_ray_queue_depth{queue="..."}`
- `django_ray_queue_wait_seconds_*`
- `django_ray_claim_latency_seconds_*`
- `django_ray_execution_duration_seconds_*`
- `django_ray_retries_recorded`
- `django_ray_failures_recorded`
- `django_ray_timeouts_recorded`
- `django_ray_tasks_expired`
- `django_ray_expirations_recorded`
- `django_ray_worker_leases{status="..."}`

The example endpoint requires its bearer token. Production deployments should use a
dedicated authenticated scrape identity or a network-restricted adapter. Queue series
appear only for the application's explicit allowlist.

Expected behavior:

- steady state: `queued` remains near baseline while tasks complete.
- incident signal: `queued` rises while `running` stays near zero.
- incident signal: `running` grows and does not drain.
- incident signal: `failed`/`lost` rises quickly with the same callable path.
- incident signal: `expired` rises, indicating queued work exceeded its snapshotted
  wait deadline before a worker could submit it.
- historical signal: `django_ray_expirations_recorded` rises even if an operator later
  retries an expired attempt; `django_ray_tasks_expired` is only the current-state gauge.
- incident signal: stale leases rise while claim latency and queue depth increase.

## Pattern-Unredacted Failure Diagnostics In Admin

Start on the ordinary execution or archived-attempt detail. It is safe to use for broad
operator access because arguments, results, errors, and tracebacks are redacted and
bounded. If redaction removes the evidence needed to diagnose a failure, ask a user who
has both the ordinary object-view permission and
`django_ray.view_sensitive_task_data` to open **Sensitive data**. Here,
"unredacted" means that configured name-pattern redaction is bypassed; it does not mean
raw bytes or an unbounded response.

The pattern-unredacted page is a deliberate incident-data boundary:

1. Confirm that the task belongs to the tenant or operational scope being investigated.
2. Open only the exact execution or archived attempt involved. Attempt pages reuse the
   original execution inputs but show the selected attempt's own outcome.
3. Treat the page as potentially secret-bearing. Do not paste it into tickets, chat, or
   logs without applying the application's incident-data policy.
4. Remove a temporary user/group grant after the investigation. Permission revocation
   takes effect on the next request; the response is GET-only and marked `no-store`.

RuntimeEnv snapshots, completion envelopes, workflow progress, and logs are excluded
even from this page. Use their separately bounded operational surfaces and deployment
authorization. A value larger than 64 KiB is reported by byte size but is not loaded or
rendered; inspect it through a deployment-owned database or result-storage procedure
with equivalent authorization. There is no built-in task-owner field in 0.4.0, so use
global groups sparingly or supply an object-permission backend for tenant-scoped grants.

## Safety Model

Treat started production work as replayable, not exactly once. Queued work can expire or
be cancelled before application code begins. `django-ray` can retry started work after
application exceptions, lost worker ownership, Ray connection loss, and unknown
completion state. That protects throughput and recovery, but it cannot prove that a
crashed or disconnected attempt made no external side effects before disappearing.

Before enabling automatic retries on side-effecting callables, confirm the task has an idempotency
key or operation table that makes duplicate execution harmless. For payments, emails, webhooks, or
external writes, prefer a deduplicated commit record over relying on task attempt counts.

## Execution Protocol Rollout Boundary

Migration `0019_execution_protocol_schema` creates a read-only protocol policy and
records bounded execution and worker capabilities. Its initial expected state is policy
schema `1`, active write protocol `1`, legacy worker admission enabled, and revision `1`.
This is an observation and fencing boundary, not activation of a new execution format:
only protocol `1` is supported, and upgraded workers advertise the range `1` through
`1`. The Task Execution Protocol Policy Admin does not permit adding, changing, or
deleting policy rows. Execution, archived-attempt, and worker-lease Admin pages likewise
show their protocol and provenance fields read-only.

Use the integer protocol fields for compatibility decisions. The displayed
`django_ray_version` and execution or attempt package versions are diagnostic provenance
only; a matching Semantic Version does not admit a worker, and a different Semantic
Version does not by itself reject one. A lease with no explicit range is a legacy,
policy-controlled lease. While legacy admission is open, only the singleton admission
token lets such a lease remain active for protocol-`1` work. The initial protocol-`1`
ownership fence remains behaviorally dormant for compatibility with existing recovery
paths; future-protocol and post-legacy ownership transitions require an explicitly
compatible active lease. Do not update the policy, token, protocol, or capability
columns directly.

For provenance review, treat `created_with_django_ray_version` as the immutable writer
of the task chain. `managed_with_django_ray_version` describes only the current attempt
and is replaced when a compatible task manager adopts ownership. An archived attempt
keeps the exact protocol plus its manager/executor values; a retry clears the current
manager/executor values before the next attempt can be claimed. Null is meaningful:
historical producers and legacy completion envelopes cannot be reconstructed, and a
manager package version must never be substituted for an unreported executor version.

Completion provenance is trusted only after the manager validates a versioned-v1
envelope's exact schema, protocol, task primary/public identity, attempt, and generation
under the authoritative lease-and-task lock. Any reserved versioned field requires the
complete strict envelope and prevents fallback to the legacy adapter. Only then may the
manager canonicalize a result reference, store a result, or archive the bounded executor
package version. Semantic package versions remain diagnostic and do not admit work.
Unversioned protocol-v1 completions remain supported for 0.4 handoff and keep executor
provenance null.

Package-owned producers insert `QUEUED` rows and acquire ownership through the fenced
update path. Directly inserting an already-`RUNNING` or `CANCELLING` row is unsupported
and bypasses that ownership-transition check.

Upgraded task managers read the supported protocol range from their exact locked and
fresh schema-`1` lease. The worker applies that range before the bounded queued-expiry
and claim limits, then rechecks it after locking an exact execution before adoption or
mutation. An unsupported row remains unchanged and does not cause the worker to give up
its lease. Reconciliation, stuck/timeout recovery, and cancellation processing refresh
the exact lease range before their global scans and filter active lookups plus
`RUNNING`/`CANCELLING` discovery before contacting Ray. Unsupported in-memory Ray Job
tracking is dropped locally without changing the task or its source lease; a compatible
cohort can recover it later. Every resulting effect still crosses the authoritative
lease-then-execution boundary.

Direct package lifecycle calls use the package-supported protocol range by default and
check the exact locked execution before cancellation, retry, expiry, success, failure,
loss, or final cancellation. Unsupported cancellation and retry requests return a
distinct fixed status; expiry and terminal transitions leave the row unchanged. The
check runs before RuntimeEnv hydration, attempt archival, or any remote cancellation
effect. Admin actions report unsupported selections separately. Do not attempt to
widen the public retry or cancellation services: they always bind the installed
package's supported range. A broader package-private range is valid only after the
calling worker cohort has already proved that capability.

Ray Core managers also prove the exact live lease and durable handle identity before a
task-specific `ray.wait` or `ray.get`. Unsupported, missing, terminal, stale, or
transferred handles are forgotten locally before that call; only exact compatible
`RUNNING` or `CANCELLING` rows receive monitor heartbeats. A completion rechecks the
authoritative lease-then-execution boundary before result storage or any lifecycle
effect, and disconnect/reconnect loss handling follows the same rule. An unsupported
durable row remains byte-for-byte unchanged.

Forgetting a Ray Core handle is not a recoverable handoff. The `ObjectRef` belongs to
the original driver, so another task manager cannot adopt it as it can a persisted Ray
Job ID. Keep the compatible driver alive while that work drains, or make an explicit
operator decision about the unchanged uncertain row. This boundary still does not
make the handle transferable. For new Ray Core submissions, the manager sends one
bounded canonical request assembled from durable JSON or an opaque input reference,
plus independent expected task and protocol primitives. The by-value bootstrap rejects
a malformed, unsupported, or mismatched request with a fixed non-retryable completion
before Django setup, input hydration, or application callable import/invocation.
Released positional submissions remain the protocol-v1 rolling-compatibility path.
If Ray returns no completion for a strict handle, the manager uses a fixed
non-retryable transport failure with no remote exception text or executor provenance;
inspect the remote execution before considering manual replay.

This check runs only after Ray deserializes the trusted bootstrap and plain request
string. Exact Ray/Python and cluster-instance attestation must still happen before Ray
Core serialization and submission.

New Ray Job submissions use the same canonical request, sourced from durable JSON or an
opaque input reference, and independently bind its exact identity, protocol, and digest
in bounded Ray Job metadata. The driver validates both before Django setup, input
hydration, or callable import/invocation. A compatible replacement manager validates
the exact persisted strict job ID plus bounded identity and protocol metadata; it does
not reconstruct the shell request or use the digest to infer execution effects.
Released unversioned payloads remain the protocol-v1 drain adapter, and a released
driver can still read the flat strict-v1 fields while upgraded code remains at active
protocol `1`.

Treat every strict terminal Ray Job with a verified binding but no exact completion as
non-retryable after the normal publication grace period. The dedicated rejection exit
code is diagnostic only: it cannot prove the driver stopped before imports or
application effects. The manager does not use Ray Job logs to establish compatibility,
phase, or replay safety. Malformed or missing strict metadata is uncertain: let the
exact stop/`LOST` boundary complete, inspect the remote job, and require an explicit
operator decision before replay. Inline request bytes still occupy the shell command
until the separate bounded reference-only transport work lands; enable input spillover
for large or sensitive arguments and do not treat the codec's resource budget as an
operating-system command length guarantee.

Strict Ray Core and Ray Job contexts carry their exact task identity and protocol into
nested workflow steps, result-fold reducers, and distributed map, starmap, and scatter
leaves. Those leaves validate a canonical boundary request and independent controls
before Django setup and application callable import or invocation; distributed leaves
also validate the callable-byte digest before django-ray calls `pickle.loads`. A partial
strict boundary never falls back to the released direct call. Result-fold admission
waits for the actor's validated ready acknowledgement before mapper effects begin.

A nested incompatibility is a fixed non-retryable outer failure, even when Ray wraps it
in a typed task-error cause. Do not manually replay it merely because the rejecting leaf
did not invoke its callable: sibling workflow or distributed leaves may already have
effects. A workflow output-preview path mismatch has the same disposition; the strict
request binds that optional second application import independently. Ray deserializes
each remote bootstrap and ordinary item arguments before the leaf body can validate, so
exact Ray/Python and cluster-instance attestation remains a separate pre-serialization
and pre-submission requirement.

Use only the exact final 0.5 candidate as the schema-`1`, protocol-`1` upgraded cohort.
Intermediate unreleased 0.5 snapshots that advertised the same protocol before nested
fencing was complete are not rollout-compatible; stop and drain them before the final
candidate. The supported handoff is from released 0.4 legacy/schema-`0` workers, not
between arbitrary development commits that happen to share a protocol number.

Do not interpret the lease's informational `queue_name` text as a durable per-queue
capability, or its protocol range as proof that Ray is ready. Ray/Python version and
cluster-instance attestation, normalized target capacity, and supported blue/green drain
status are separate rollout requirements.

Use the read-only protocol report before reviewing any later rollout transition:

```bash
python manage.py django_ray_protocol_status
python manage.py django_ray_protocol_status --database=default --json
```

The text and canonical JSON views share one versioned bounded observation. They report the
policy/token relationship, live and stale-active task-manager lease totals, aggregated
protocol ranges, nonterminal work grouped by queue/state/protocol, protocol-only work
with no heartbeat-live compatible lease, work with no heartbeat-live explicit upgraded
reader, and fixed closure or rollback blocker codes. The report owns one consistent
read-only database snapshot, is redaction-safe, and does not mutate any row. Queue text
is bounded in SQL before materialization. Repeated sections are capped with exact
omitted totals, and the complete output is bounded to 65,536 UTF-8 bytes.

This is evidence, not an activation command or end-to-end readiness result.
`queue_capacity_attested=false` is deliberate because lease queue text is
informational; the command cannot prove queue membership, available concurrency, Ray
connectivity, target identity, or Ray/Python compatibility. It also cannot discover
capability-unaware web, API, producer, or reader processes. Retirement of those
processes remains an explicit external assertion, and every changing coordination
operation rechecks its durable preconditions under the transactional fence.

The execution Admin detail/live summary and the bundled testproject task-status,
execution-list, and execution-detail examples expose the same point-in-time
`protocol_compatible_worker_available` signal. They also show
`execution_protocol_version`, creator/manager/executor package provenance, and
`queue_capacity_attested=false`. A false availability value means the read could not
safely attest a compatible heartbeat-live lease. That can mean no matching lease, an
invalid policy/token relationship, or any malformed lease advertisement; it does not
say which queue needs capacity or prove that Ray can accept work. A true value likewise
does not attest the lease's informational queue text, free worker slots, Ray/Python
compatibility, Ray connectivity, or cluster identity. Use the full status command and
external deployment evidence for rollout decisions. Provenance is diagnostic only;
null includes legitimate historical/unreported values and any stored value that fails
the fixed 128-byte public-read guard.

The package now contains a private coordination primitive for a later supported
operator adapter. It is not an operator API: do not import or call it directly, and do
not use SQL, model updates, Admin mutation, or manual token deletion as a substitute.
Keep legacy admission open in this slice. The future adapter must compare-and-set the
exact policy revision the operator reviewed within the database positive-bigint range
and collect an explicit assertion that every capability-unaware web, API, and other
enqueue producer has stopped. The database can serialize old inserts after that
assertion, but it cannot
discover those producer processes itself. Every active legacy lease blocks closure
even when its heartbeat is stale; a later lifecycle operation must durably retire it
rather than letting activation infer that it is dead.

The tested primitive serializes PostgreSQL coordination calls with a transaction-scoped
advisory mutex, then uses lease-first ordered locks followed by the policy and token for
closure; SQLite obtains a no-op policy write fence before reading blockers. Its
successful close preserves inactive lease rows with a null token, removes the singleton
token, closes policy, and increments revision atomically. A stale or malformed revision,
active legacy lease, inconsistent policy/token state, or database failure leaves the
prior state intact. A transition must own the outermost database transaction with
autocommit enabled before entry; a future adapter must not wrap it in another atomic
block, disable autocommit first, or acquire rollout locks beforehand.

Reopening requires the current revision, active write protocol `1`, and no nonterminal
work with another execution protocol. It recreates the token but does not reactivate or
relink an inactive legacy lease; an exact-0.4 task manager must acquire a new identity.
On PostgreSQL, a changing reopen fences execution-table writers before it locks and
evaluates the policy, while an already-open idempotent check avoids that table lock;
SQLite uses its database-wide writer fence. Migration `0020` then rejects
new non-protocol-`1` nonterminal inserts and terminal-to-nonterminal transitions for as
long as legacy admission remains open, so the rollback precondition cannot become stale
immediately after the transition. Applying `0020` fails closed if an already-open policy
already contains incompatible nonterminal work.
Reopening is not by itself a rollback-readiness result: stop and reconcile upgraded
task managers and verify the remaining artifacts and remote work separately.

The supported migration source is the exact published 0.4.0 baseline at migration
`0018`. Before applying `0019`, confirm every producer and task manager that may have
written retained nonterminal work was running 0.4.0. If any live row was written directly
by pre-0.4 code, drain or cancel it, or complete an application-specific audit; the new
columns cannot reconstruct its original execution contract. Do not treat an `0018`
migration record alone as proof of the writer version.

For a code-only rollback, keep both `0019` and `0020` applied, keep policy protocol `1`
and legacy admission open, verify all nonterminal work is protocol `1`, and stop and
reconcile upgraded task managers before starting exact 0.4.0 code. Reverse `0020` and
then `0019` only as a separate stopped-writer maintenance operation after accepting the
loss of their protocol, provenance, capability, policy, token, and fencing data.

## Useful Queries

```sql
-- Task counts by state
SELECT state, COUNT(*) AS count
FROM django_ray_raytaskexecution
GROUP BY state
ORDER BY state;
```

```sql
-- Queued work at or beyond its snapshotted deadline
SELECT id, task_id, callable_path, queue_name, queue_deadline_at
FROM django_ray_raytaskexecution
WHERE state = 'QUEUED'
  AND queue_deadline_at IS NOT NULL
  AND queue_deadline_at <= NOW()
ORDER BY queue_deadline_at ASC;
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
SELECT worker_id,
       hostname,
       pid,
       queue_name,
       is_active,
       capability_schema_version,
       min_supported_execution_protocol_version,
       max_supported_execution_protocol_version,
       django_ray_version,
       last_heartbeat_at
FROM django_ray_taskworkerlease
ORDER BY last_heartbeat_at ASC;
```

```sql
-- Singleton execution-protocol rollout policy (expected initial state: 1, 1, true, 1)
SELECT schema_version,
       active_write_protocol_version,
       legacy_worker_admission_enabled,
       revision,
       updated_at
FROM django_ray_taskexecutionprotocolpolicy
WHERE singleton_key = 1;
```

```sql
-- Nonterminal executions grouped by the normative protocol and metadata schema
SELECT execution_protocol_version, metadata_schema_version, state, COUNT(*) AS count
FROM django_ray_raytaskexecution
WHERE state IN ('QUEUED', 'RUNNING', 'CANCELLING')
GROUP BY execution_protocol_version, metadata_schema_version, state
ORDER BY execution_protocol_version, metadata_schema_version, state;
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
4. Check `django_ray_tasks_expired` and the oldest `queue_deadline_at`. `EXPIRED` is
   terminal and never automatically retries; correct worker capacity or routing before
   using the admin or API retry action, which assigns a fresh deadline.
5. Confirm `WORKER_POLL_INTERVAL_SECONDS` and
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
- An exact active Ray Job with `UNKNOWN` status is not generically requeued. Once its
  monitor heartbeat exceeds the stuck-task timeout, reconciliation marks it `LOST`,
  requests a stop for the exact persisted job ID, and suppresses automatic retry
  because the remote execution is not proven quiescent. Inspect
  `cancellation_status`/`cancellation_error` and Ray before retrying it manually.
- An unversioned malformed completion retains the bounded recovery and grace behavior
  used by legacy protocol-v1 jobs. While Ray still reports `PENDING` or `RUNNING`, an
  expired legacy envelope follows the exact-stop, `LOST`, no-auto-retry path; a terminal
  Ray status may use the configured failure/retry policy.
- A partial or mismatched versioned completion cannot fall back to legacy handling. Its
  result, result reference, and claimed error are ignored. An active or unknown exact Ray
  Job is stopped and retained as `LOST`; a terminal job becomes a fixed non-retryable
  failure. Inspect the bounded diagnostic and remote state before any manual retry.
- The strict driver uses a dedicated nonzero exit when it rejects a request before
  Django setup, but reconciliation treats that value only as diagnostic. Application or
  import code can terminate with the same value, so the exit alone cannot prove the
  execution phase or absence of effects.
- Every strict Ray Job that becomes terminal without its exact completion envelope
  waits for the normal publication grace period and then receives the same fixed generic
  failure without automatic retry or log inspection. Missing or mismatched strict
  metadata instead follows the exact-stop, `LOST`, no-auto-retry boundary.
- A completion that exceeds the fixed envelope byte, depth, or node budget is also
  rejected without automatic retry, including an unversioned legacy envelope. This is a
  deterministic resource boundary, not legacy malformed-envelope grace; inspect the
  remote execution before deciding whether replay is safe.
- Ray Job version, status, stop, and log HTTP requests used by lifecycle control are
  bounded to five seconds. A timeout can therefore leave `cancellation_status` as
  `INDETERMINATE`; verify the exact persisted Ray Job before manually retrying. Ray
  Client, `auto`, and GCS address discovery happens before the execution row lock, so
  slow discovery cannot block completion, adoption, or retry coordination.
- A worker warning that submission acceptance is uncertain refers to a deterministic
  job ID that was persisted before the request. Let reconciliation resolve that exact
  ID; do not manually start another attempt while its Ray state is unknown.
- A `RUNNING` row with non-null `completion_data` is between entrypoint publication
  and worker reconciliation. Cancellation reports `COMPLETION_PENDING`, and timeout
  recovery leaves it alone; let reconciliation consume the envelope even if Ray still
  reports the wrapper process as running.

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

## 4) Durable Input Retrieval or Cleanup Failure

Symptoms:

- task errors report a missing, malformed, unauthorized, or corrupt input reference;
- the task fails before application logs appear;
- `django_ray_purge_inputs` records `cleanup_error` or exits non-zero.

Checks:

1. Inspect `input_reference` and the matching `TaskInputPayload` state without copying
   the payload into tickets or logs.
2. Confirm every enqueueing and worker process uses the same input backend, filesystem
   root, bucket, prefix, and credentials.
3. Verify the object exists and its access policy has not changed.
4. If cleanup failed, inspect `cleanup_error` and fix storage access before rerunning.

Recovery:

1. Restore the exact content-addressed object or correct storage configuration.
2. Use a controlled manual retry only after retrieval succeeds. Validation failures do
   not auto-retry; storage retrieval failures may already follow normal retry policy.
3. Preview retention with `django_ray_purge_inputs --retention-days=30` before using
   `--delete`. Increase retention when historical manual retry or audit access is needed.

Do not edit `input_reference`, digest metadata, or JSON placeholders by hand. Successful
cleanup retains a `PURGED` tombstone and execution references for audit.

## 5) Workflow Progress Retention or Orphan Cleanup Failure

Symptoms:

- expired normalized detail continues to consume database storage;
- unpublished topology candidates or unreferenced pages remain for more than one hour;
- inactive run-storage shells remain after their last candidate or orphan page is gone;
- `django_ray_cleanup_workflow_progress --delete` exits nonzero and a retained run or
  pending manifest has `cleanup_error`.

Checks:

1. Preview one bounded pass with `django_ray_cleanup_workflow_progress`.
2. Confirm terminal detail has `detail_retention_days` and `detail_expires_at` recorded;
   cleanup does not invent or override the lifecycle-owned retention deadline.
3. Treat an exact active task identity as protected even if a malformed or manually
   altered expiry appears to be in the past.
4. Inspect the bounded cleanup code and exception type. Exception messages are
   intentionally redacted; use database and application logs under the deployment's
   normal secret-handling policy for deeper diagnosis.

Recovery:

1. Correct the database, lock-timeout, or integrity condition reported by operations.
2. Rerun the dry run, then run
   `django_ray_cleanup_workflow_progress --batch-size=100 --delete`.
3. Repeat bounded passes until zero eligible items remain. One failed item does not
   prevent later candidates in a pass, and clean candidates are processed before retry
   rows so a permanent oldest failure cannot starve later work. Any failure still makes
   that command invocation exit nonzero.

Cleanup removes expired run-owned detail or old unpublished orphans only. It does not
rewrite the task-row or `TaskAttempt` summary, task state, or terminal outcome. Do not
manually delete current manifests or referenced pages; their references are part of
the atomic publication and integrity contract.

Apply migration `0013_workflow_progress_detail_storage` before scheduling the command.
It is safe to establish the cleanup schedule while general/default schema-v3 runtime
publication remains disabled for the reader-first rollout, #79 live-ingestion bound,
and #142 composite preparation. The default-off strict terminal pilot may already
create admitted bounded rows, which the same retention contract covers. Start with
dry-run monitoring, then use bounded `--delete` passes at a cadence appropriate to
database growth and the configured retention window. Alert on a nonzero exit and on an
eligible count that does not fall across repeated successful passes.

A manifest, digest, aggregate, or node-key integrity error during publication is a
fail-closed storage fault. Do not manually promote the pending manifest or advance the
task summary pointer. Preserve the bounded diagnostic and relevant database logs,
stop the affected producer if it is repeatedly retrying, and investigate the exact run
identity before allowing a fresh publication.

Schedule `django_ray_audit_workflow_progress` for exact runs selected by operational
policy, and run it before attempting manual storage recovery. The audit is read-only:
it locks the task then exact run, verifies the current topology and every bounded
latest-state row, and exits nonzero without changing the corrupt evidence. Supply all
four identity fields from the summary or retained attempt record; do not substitute
only the current task primary key when auditing an older retained run. An audit reads
no more than the protocol limit plus one detail row and is intentionally separate from
the sparse publication path.

## 6) Ray Connection Loss

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

## 7) Cancellation Stuck In CANCELLING

Symptoms:

- rows remain `CANCELLING` for too long.

Checks:

1. Confirm owning worker is still running.
2. Confirm cancellation processing runs in worker loop.

Recovery:

1. Restart worker if cancellation loop is stalled.
2. After restart, verify `CANCELLING -> CANCELLED` transitions complete.

## 8) Oversized Results

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

Prefer Django admin actions for retries and cancellations. Both actions use package-owned,
row-locked lifecycle services; do not reproduce them with direct model or SQL updates.

If scripting is necessary, use Django shell, narrow authorized filters, and retain the
attempt number and execution generation observed during authorization:

```python
from django_ray.lifecycle import request_task_cancellation, retry_task
from django_ray.models import RayTaskExecution, TaskState

failed = RayTaskExecution.objects.filter(
    state=TaskState.FAILED,
    queue_name="default",
).only("pk", "attempt_number", "execution_generation")[:100]
for execution in failed:
    retry_task(
        execution.pk,
        expected_attempt_number=execution.attempt_number,
        expected_execution_generation=execution.execution_generation,
    )

# Apply the application's object/tenant authorization before selecting this row.
running = RayTaskExecution.objects.only(
    "pk",
    "attempt_number",
    "execution_generation",
).get(
    task_id="authorized-task-id",
)
outcome = request_task_cancellation(
    running.pk,
    expected_attempt_number=running.attempt_number,
    expected_execution_generation=running.execution_generation,
)
print(outcome.status, outcome.state)
```

Cancellation is a durable request, not a guarantee that already-running synchronous
Python code was interrupted. A queued task becomes `CANCELLED` immediately; running
work becomes `CANCELLING` until a worker makes its best-effort backend request and
finalizes the row. A stale attempt or generation, terminal task, duplicate request,
invalid state, or missing row returns a bounded no-op result rather than overwriting
newer work.

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
