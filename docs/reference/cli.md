# CLI Reference

## django_ray_worker

The main worker command that processes tasks from the queue.

```bash
python manage.py django_ray_worker [options]
```

### Options

#### Queue Selection

| Option | Description |
|--------|-------------|
| `--queue=QUEUE` | Queue name to process (default: `default`) |
| `--queue=Q1,Q2` | Multiple queues (comma-separated) |
| `--queues Q1 Q2` | Multiple queues (space-separated alternative to `--queue`) |
| `--all-queues` | Process the declared queues from every configured django-ray backend alias |

The three queue-selection forms are mutually exclusive. `--all-queues` includes
`RayTaskBackend` subclasses, deduplicates queue names, and ignores queues owned only by
Celery or another backend. It fails when no django-ray queues can be enumerated instead
of silently using another backend's `default` queue. In Ray Core mode, it also rejects
aliases with different effective `RAY_ADDRESS` values because one process cannot honor
multiple cluster targets; Ray Job mode preserves the target stored on each task.
Ray Core and synchronous workers report and skip queues declared through a django-ray
backend with `OPTIONS["RAY_JOB_ONLY"] = True`. Explicit `--queue` or `--queues`
selection of one of those queues fails at startup, and the claim boundary repeats the
check for programmatic command use. Ray Job mode accepts them.

#### Execution Mode

| Option | Description |
|--------|-------------|
| `--sync` | Run tasks synchronously (no Ray) |
| `--local` | Use local Ray cluster |
| `--cluster=ADDRESS` | Connect to Ray cluster at ADDRESS |
| *(none)* | Use default from `DJANGO_RAY.RUNNER` (`ray_job` by default) |

The explicit execution-mode flags are mutually exclusive. Omit all three to derive the
mode from `DJANGO_RAY`; contradictory flags are rejected instead of using an implicit
precedence order.

The default `ray_job` mode requires a retrievable `INPUT_STORAGE_BACKEND`, even when
argument spillover is disabled. The worker validates that backend before creating a
lease or claiming work, so a missing or malformed configuration fails at startup.

#### Concurrency

| Option | Description |
|--------|-------------|
| `--concurrency=N` | Maximum concurrent tasks (default: `DJANGO_RAY["DEFAULT_CONCURRENCY"]`, which defaults to `10`) |

### Examples

```bash
# Process default queue with local Ray
python manage.py django_ray_worker --queue=default --local

# Process multiple queues
python manage.py django_ray_worker --queue=default,high-priority --local

# Process all queues with high concurrency
python manage.py django_ray_worker --all-queues --local --concurrency=50

# Connect to Ray cluster
python manage.py django_ray_worker --queue=default --cluster=ray://ray-head:10001

# Sync mode for testing
python manage.py django_ray_worker --queue=default --sync
```

### Signals

The worker explicitly handles these signals. Shutdown is a durable handoff:
new tasks are not claimed after the signal; synchronous work already running
is allowed to finish; Ray Job submissions are released for another worker to
reconcile; and in-flight Ray Core tasks receive a cancellation request and are
persisted as `CANCELLING` before the Ray connection closes.

| Signal | Behavior |
|--------|----------|
| `SIGTERM` | Graceful handoff; exit with `143` |
| `SIGINT` | Graceful handoff (Ctrl+C); exit with `130` |

### Environment Variables

The management command itself expects CLI flags and Django settings. The Docker entrypoint included
in this repository maps these environment variables into command-line options:

| Variable | Description |
|----------|-------------|
| `DJANGO_RAY_QUEUE` | Queue name passed as `--queue` |
| `DJANGO_RAY_QUEUES` | Comma-separated queue names passed as `--queue`; takes precedence over `DJANGO_RAY_QUEUE` |
| `DJANGO_RAY_CONCURRENCY` | Concurrency limit passed as `--concurrency` |
| `RAY_ADDRESS` | Ray cluster address used by sample settings and `worker-cluster` entrypoint mode |

### Exit Codes

| Code | Description |
|------|-------------|
| `0` | Normal shutdown |
| `1` | Error during startup |
| `130` | Interrupted (SIGINT) |
| `143` | Terminated (SIGTERM) |

## django_ray_protocol_status

Inspect the durable execution-protocol rollout state without changing policy, leases,
or task rows:

```bash
python manage.py django_ray_protocol_status
python manage.py django_ray_protocol_status --database=default --json
```

| Option | Description |
|--------|-------------|
| `--database=ALIAS` | Django database alias to inspect; default `default` |
| `--json` | Emit the canonical versioned JSON report instead of the text view |

Both formats describe the same `django-ray.protocol-status` report. It includes the
policy and admission-token relationship, active and heartbeat-stale task-manager lease
totals, aggregated protocol capability ranges, bounded nonterminal counts by queue,
state, and protocol, protocol-only unsupported-work counts, work lacking a
heartbeat-live explicit upgraded reader, and fixed rollout blocker codes. The service
owns one consistent read-only database snapshot; changing coordination operations still
recheck every durable precondition. Repeated groups are deterministic and capped at 64
entries with exact omitted group and task totals; the complete UTF-8 output is capped at
65,536 bytes.

The command emits no task IDs, worker IDs, hosts, callable paths, errors, package
versions, or payload data, and it performs no database mutation. Queue text is bounded
in the database before it is materialized, then normalized and redacted. Its
`queue_capacity_attested` field is always `false`: `TaskWorkerLease.queue_name` is
informational, and a protocol-compatible heartbeat does not prove that a worker serves
a particular queue or has a working Ray target. The database also cannot prove that
capability-unaware producer or reader processes have retired. Treat those fixed
limitations as operator evidence still required outside this report.

## django_ray_benchmark_polling

Compare fixed and adaptive claim polling against the configured PostgreSQL database:

```bash
python manage.py django_ray_benchmark_polling \
  --workers=4 --tasks=100 --idle-seconds=2 \
  --base-interval-seconds=0.1 --max-interval-seconds=0.5 \
  --seed=53 --json
```

The command starts the production worker claim loop on isolated temporary queues. It
separately measures idle query load and poll de-synchronization, spaced enqueue-to-claim
latency, and preloaded-burst claim throughput, then deletes its benchmark rows. Options
control worker count, task count per active phase, idle duration, enqueue interval, base
and maximum poll intervals, cross-worker overlap window, random seed, and
startup-barrier timeout.
`--json` records Django, Python, PostgreSQL, migration, timing, and seed metadata. It
refuses SQLite and other database engines because their locking behavior is not
representative.

Every run also emits additive schema-v1 `protocol_predicate_evidence`. The command
intercepts the production priority claim `SELECT` before it executes, proves that its
bounded comparison query has the same SQL shape, and compares it with a control that
omits only the execution-protocol range. Both variants select the same exact owned
rows at the package's active write protocol (currently protocol `1`) and use 12
deterministic, counterbalanced timing pairs. The report
contains fixed-vocabulary bounded `EXPLAIN ANALYZE` summaries, p50/p95 timings, and
signed production-minus-control deltas; it never includes SQL, queue names, task IDs,
or row IDs. Those timings are benchmark evidence rather than a pass/fail latency gate.
The command deletes only its exact temporary rows and capture lease, including on
failure.

## django_ray_purge_inputs

Report retained external task-input and Ray Job request payloads whose references are
all terminal and older than the selected retention window. The command name is retained
for compatibility:

```bash
python manage.py django_ray_purge_inputs --retention-days=30
```

The command is a dry run unless `--delete` is supplied. Deletion keeps the execution
references and marks the registry entry `PURGED` for audit:

```bash
python manage.py django_ray_purge_inputs --retention-days=30 --delete
```

| Option | Description |
|--------|-------------|
| `--retention-days=N` | Require registry use and every terminal finish to be at least `N` days old; default `30`, range `0..365000`; inherited from a continuation cursor when omitted |
| `--delete` | Delete eligible objects after a complete, bounded, row-locked reference check; required again when continuing a deletion pass |
| `--max-candidates=N` | Registry candidates per invocation; default `100`, range `1..1000` |
| `--max-attachments=N` | Execution references inspected per payload; default `100`, range `1..1000`; one extra row detects overflow |
| `--max-details=N` | Detailed fingerprint/outcome rows; default `20`, range `0..100`, also subject to the detail-byte budget |
| `--cursor=TOKEN` | Continue after the previous batch's registry primary key with the original fixed cutoff and mode |
| `--format=text\|json` | Human-readable text (default) or a versioned JSON report |

Each invocation fetches at most `max-candidates + 1` registry keys and, for each
selected key, at most `max-attachments + 1` execution rows. A payload with more
attachments is retained with the `incomplete` outcome, even if every inspected
row is terminal. No partial attachment history can authorize deletion. Raise the
attachment budget within the supported maximum and start a fresh pass to revisit
such rows; the command conservatively retains histories beyond the hard maximum.

The report is bounded to 16,384 UTF-8 bytes, including the fixed command-error
message. Details have an additional 8,192-byte budget; the summary counts omitted
details. JSON uses `schema_version: 1`, `mode`, `cutoff`, `retention_days`, `counts`,
`has_more`, `next_cursor`, `details`, `details_omitted`, and `limits`. Counts are
`processed`, `eligible`, `purged`, `retained`, `failed`, and `incomplete`.
`retained` means selected candidates not deleted by this invocation, including
dry-run candidates and rows that disappeared or changed before locking. Detail
outcomes are `eligible`, `purged`, `skipped`, `failed`, or `incomplete`.

Exit status `0` means the observed key range is exhausted with no storage failure
or attachment overflow. Status `2` means more candidates or incomplete attachment
evidence; status `1` means a storage failure or invalid invocation and takes
precedence over status `2`. The report is emitted before a storage/incomplete
error, so JSON consumers can still read the summary and continuation token.
Output-detail omission alone does not make the pass incomplete.

All counts and exit statuses describe **one invocation**. A cursor advances past
failed and attachment-overflow candidates too. A later page returning status 0
does not resolve errors or incomplete histories reported on earlier pages. Retain
every page's outcome and revisit unresolved candidates in a fresh pass.

Continue with the returned opaque token, preserving `--delete` for deletion:

```bash
python manage.py django_ray_purge_inputs --format=json --max-candidates=100
python manage.py django_ray_purge_inputs --format=json --cursor="$PURGE_CURSOR"
```

**Scheduling migration:** the formerly unbounded invocation now stops after 100
candidates by default. Update scheduled jobs to read JSON, retain each page's
outcomes, and continue with `next_cursor` while `has_more` is true, with the same
explicit deletion mode. Give the surrounding job its own finite page/time budget
and save the next token when that budget ends. Treat status 2 as a signal to inspect
continuation and incomplete counts, not an instruction to retry the identical
command indefinitely. Repeating without a cursor can continually revisit retained
leading rows and starve later candidates. Schedule recurring fresh passes as well
to revisit retained rows and concurrent earlier-key insertions. Alert on failures
and unresolved attachment overflow instead of declaring success from the final
page alone.

The token encrypts the storage-reference primary key and authenticates the cutoff,
retention days, and deletion mode using a domain-separated key derived from Django
`SECRET_KEY`. It is not an execution capability. A mode or explicit retention
conflict is rejected. Rotating `SECRET_KEY` invalidates old tokens, even when the
old value remains in `SECRET_KEY_FALLBACKS`; start a fresh pass. Never edit or
decode a token into an operator log.

The cursor advances past retained and incomplete rows as well as deleted rows,
and remains usable if its boundary row disappears. It is a traversal position,
not a database snapshot: concurrent insertion of an earlier key or mutation of
older rows requires a fresh pass. `has_more: false` never certifies a complete
global inventory under concurrent writers. Interruption rolls back the current
database transaction; earlier tombstones remain. Restart a fresh pass after an
interruption or failed deletion, rechecking storage outcomes before assuming an
external delete was rolled back with the database.

These limits bound returned rows, materialized data, and reporting. Registry keys
must match their bounded SQL projections before any deletion or cursor creation.
The database checks raw execution state against the terminal vocabulary; references use bounded
SQL substrings whose completeness is checked against the raw columns. Oversized or
NUL-truncated references and nonterminal or malformed states retain the object, even
on SQLite where declared text lengths are not enforced. Locked execution values have
no deferred model fields that could reload the original text. These limits do not
bound rows scanned inside the database, query/lock waits, connection time, backend
deletion latency, or total elapsed time. This command has no hard time deadline.

The registry's payload kind selects the exact execution reference column. Unknown,
wrong-kind, or dual-column references remain active for operator review. Storage
failures are recorded as a bounded exception class in
`TaskInputPayload.cleanup_error` and make the command exit with an error. Dry-run,
success, and failure details use only a 16-character SHA-256 reference fingerprint; full
storage references and provider exception messages are not printed. See
[Durable Input Storage](input-storage.md#retries-and-retention) before scheduling cleanup.
Before enabling rq2 writes, upgrade or disable every older scheduled/manual copy of this
command: released binaries understand neither `payload_kind` nor
`ray_job_request_reference`; their dry run misreports an aged active rq2 object and
`--delete` can remove it. Resume cleanup only from the exact final rq2 code.

## django_ray_cleanup_workflow_progress

Preview expired normalized detail, stale unpublished topology storage, and empty
inactive run rows:

```bash
python manage.py django_ray_cleanup_workflow_progress
```

The command is a dry run unless `--delete` is supplied. The deletion pass uses the
expiry already recorded with each terminal run; it does not accept a command-line
retention override and does not rewrite task or attempt summaries.

```bash
python manage.py django_ray_cleanup_workflow_progress --delete
```

| Option | Description |
|--------|-------------|
| `--batch-size=N` | Check at most `N` expired runs, pending manifests, orphan pages, and empty runs per class; default `100`, range `1`-`1000` |
| `--delete` | Delete candidates that remain eligible after row locking |

An expired run is protected while its complete attempt, generation, and run identity
is still the active task identity. Independently, only unpublished `PENDING` manifests
and unreferenced pages at least one hour old are orphan-cleanup candidates. Current
manifests and every referenced page remain protected. Repeated passes are idempotent;
schedule bounded passes until the report reaches zero eligible items.
Empty unpublished runs are eligible only when they are inactive and have no manifest,
page, or detail dependency.

Each item is rechecked under task, run, then manifest/page locks. One item failure does
not stop later candidates. Never-failed candidates run before retries, so one permanent
oldest failure cannot starve newer eligible work when the batch is small. The command
records only a bounded, message-redacted `cleanup_error` diagnostic on the retained run
or pending manifest and exits nonzero after finishing the pass.

## django_ray_audit_workflow_progress

Run a read-only, whole-run integrity check for one exact workflow identity:

```bash
python manage.py django_ray_audit_workflow_progress \
  --task-execution-pk=42 \
  --attempt-number=1 \
  --execution-generation=7 \
  --run-id=3f78c15c-a3ae-4d8c-8196-c952adb581cc
```

The command locks the task and exact run in publication order, verifies the current
topology, and streams at most 25,001 normalized detail rows in bounded batches. It
recomputes row count, byte, state, truncation, and event aggregates and fully validates
each row's canonical payload, digest, stable node key, and publication epochs. It never
repairs or deletes data. Any mismatch exits nonzero; successful output is deterministic
and suitable for periodic monitoring.

When the identity still owns the task row, the audit also binds the topology and detail
revisions, manifest pointer, and retained aggregates to the canonical task summary. For
an older retained run after task reuse, it verifies exact run-local evidence because the
task row now belongs to another attempt; it does not substitute that newer summary.

| Option | Description |
|--------|-------------|
| `--task-execution-pk=N` | Required `RayTaskExecution` primary key |
| `--attempt-number=N` | Required one-based attempt number |
| `--execution-generation=N` | Required execution fencing generation |
| `--run-id=UUID` | Required workflow run identifier |
| `--database=ALIAS` | Django database alias; default `default` |

