# CLI Reference

## django_ray_lease_policy

Inspect the common lease duration without creating or modifying policy:

```bash
python manage.py django_ray_lease_policy --database default
```

Migration 0028 seeds 60 seconds with revision 1. JSON contains `schema_version` (1),
`scope` (`worker_lease_policy`), `status` (`matched` or `mismatch`),
`configured_seconds`, `lease_seconds` and `revision`. A read-only mismatch or
unavailable observation exits 2 with a fixed diagnostic. It is not zero capacity.

To change a policy, stop and drain all processes sharing the lease table, including
old workers, purgers and operational callers. Reconcile remote effects and active
lease state under the old agreed policy. Then use the observed revision and value:

```bash
python manage.py django_ray_lease_policy --database default \
  --set-seconds 120 --expected-seconds 60 --expected-revision 1 \
  --all-lease-users-stopped
```

The stopped-cohort flag is an operator assertion; the database cannot discover
old processes or prove that remote work stopped. The command also requires no
active leases (including stale ones) and no running/cancelling executions. It
never retires a lease or rewrites work. A stale expected value/revision refuses.
Lock contention is bounded and returns a fixed failure; there are no retries.
The shared policy lock follows existing worker ownership transactions, including
their already-fenced cancellation and storage operations. It does not move those
effects or widen their transaction scope. PostgreSQL shared locks permit peer
workers; an exclusive policy transition refuses contention immediately.
An explicit successful update exits 0 even when this process still has the old
setting; its JSON reports that mismatch. Change every process's configuration
before restart, keeping each heartbeat interval below the common duration.

Readers never bootstrap or repair missing policy. Old binaries do not honor these
guards, so mixed old/new writers remain unsupported. See the
[coordinated upgrade procedure](../deployment/upgrading-from-0.6.md#migration-and-execution-boundary).


## django_ray_doctor

Collect bounded read-only configuration and database observations on Linux:

```bash
python manage.py django_ray_doctor --database default --format json
```

This first slice returns exit 2 when available observations are clear because
remote readiness and inventory sections remain unavailable. See the
[doctor contract](doctor.md) for finite budgets, supported settings, process
cleanup, exit codes and the standalone entry point that also bounds startup.

## django_ray_worker

Before startup, the configured lease duration must match the database authority.
See [`django_ray_lease_policy`](#django_ray_lease_policy); startup refuses mismatches
before connecting to Ray or claiming work.

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

The command restores its own main-thread signal handlers after each local,
Ray Core or Ray Client initialization attempt, including failed startup,
reconnect attempts, and command-created runners that initialize again after a
connection loss. Ray initialization can replace `SIGTERM`; restoring the
worker's handler lets a later signal request shutdown without interrupting
handoff, lease release or disconnection already in progress. Repeated signals
preserve the first signal's exit status. Direct embedded initialization helpers
do not install the command's handlers unless it already owns them on the main
thread. Standalone runner initialization retains its existing signal behavior.
This does not protect against forced termination or establish a repair for a
native Ray shutdown crash.

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

Keep the equals sign in `--cursor=TOKEN`: a valid opaque token can start with
`-`, which the argument parser would otherwise mistake for another option.
Quoting a separate token does not remove this ambiguity. Programmatic callers
passing argument strings to `call_command` should likewise use one
`f"--cursor={token}"` argument; keyword callers can pass `cursor=token`.

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
command. Pre-rq2 binaries such as 0.4.0 understand neither `payload_kind` nor
`ray_job_request_reference`; their dry run misreports an aged active rq2 object
and `--delete` can remove it. Resume cleanup only from the current rq2 code.

Current request references carry the `rq2ref:v1:` discriminator. Typed request
cleanup accepts this format and historical native references. A native/wrapped
registry alias pair is retained as incomplete even when either row is a
tombstone or has a different payload kind. See the
[reference discriminator contract](input-storage.md#request-reference-discriminator)
before investigating or upgrading cleanup jobs.

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

## django_ray_runtime_env

Verify or explicitly rewrite one bounded range of retained RuntimeEnv snapshots:

```bash
python manage.py django_ray_runtime_env --mode dry-run --format json
python manage.py django_ray_runtime_env --mode rewrite --confirm --format json
python manage.py django_ray_runtime_env --mode rewrite --confirm --format json --cursor=TOKEN
python manage.py django_ray_runtime_env --mode check --format json
```

| Option | Default | Maximum or contract |
|---|---|---|
| `--database` | `default` | Plain SQLite file or PostgreSQL Django database alias; custom backends and unsupported connection options fail closed |
| `--mode` | `dry-run` | Every mode requires a valid selected active key; `dry-run` and `check` authenticate/inspect without writes; `rewrite` converts plaintext and rewraps old keys |
| `--confirm` | Off | Required only for `rewrite`; configuration must select encrypted/required writes and an active key |
| `--format` | `human` | `human` or versioned `json` |
| `--cursor=TOKEN` | Fresh pass | Same mode, database/table and key policy; changing `SECRET_KEY` invalidates tokens |
| `--max-rows` | 100 | 1–1,000 candidate rows; one additional numeric PK detects continuation |
| `--max-bytes` | 65,536 | 1–1,048,576 bytes per stored or replacement snapshot; identity fields have separate fixed limits |
| `--max-total-bytes` | 1,048,576 | 1–8,388,608 bytes of hydrated row text and generated replacement envelopes |
| `--max-seconds` | 10 | 0.1–60 seconds of cooperative elapsed budget |
| `--max-details` | 20 | 0–100 numeric PK/outcome details; omitted details are counted |

JSON schema `django-ray.runtime-env-maintenance`, version 1, reports fixed outcome
counters, `processed`, `processed_bytes`, bounded `details`, `details_omitted`,
`after`, `high_water`, `has_more`, `next_cursor`, `budget_exhausted`, `stop_code`,
`cleanup_confirmed`, `range_complete`, `limits`, `sql_statements` and `exit_code`.
It always sets `current_table_only=true`, `fresh_pass_required=true`,
`backup_obligations="not_verified"` and `key_retirement_authorized=false`.
No plaintext, ciphertext, task/profile/hash value, key ID/material or raw provider
error is printed. Command-owned SQL uses raw independent driver cursors, so enabling
Django `DEBUG` does not retain snapshot or compare-and-set parameters in Django's
query log. Administrators must still protect database/server-side query logging.

Outcomes distinguish `current_key`, `plaintext`, `old_key`, `rewritten`, `rewrapped`,
`missing_identity`, `invalid_snapshot`, `unavailable_key`, `row_too_large`, `changed`,
`missing`, `encryption_failed`, `commit_uncertain`, `database_unavailable` and
`interrupted`. A malformed row does not stop later rows in the finite page.
`range_complete` says only that traversal reached this pass's high-water mark;
it is not a consistent database snapshot or a clean-policy result.

- Exit **0**: the selected range completed and all observed rows are authenticated
  under the active key, either already or after confirmed maintenance.
- Exit **1**: an observed invalid/unsupported snapshot, missing identity/key,
  oversized row or encryption failure needs attention. Such rows remain unchanged.
- Exit **2**: continuation, budget exhaustion, pending dry-run/check work,
  concurrent changes, database failure, uncertain commit, interruption or unconfirmed
  cleanup leaves incomplete evidence. Observed row blockers take precedence and
  may return 1 while the report also retains continuation.

Keep all page reports: the last page does not erase earlier failures. A cursor is
an encrypted scan boundary, not mutation authority. The command binds PostgreSQL
schema/table identity and effective connection defaults, and SQLite file identity;
a restore, namespace change or key-policy change requires a fresh pass. Start a
fresh check after completing a cursor chain to revisit changes and later inserts.
An uncertain commit never advances past its affected row. Repeating from that
boundary authenticates any already committed replacement without rewriting it again.

Only `runtime_env_json` is updated under an exact compare-and-set. Task identity,
profile, plaintext hash, attempt/generation, state, owner, cancellation and history
are preserved. Oversized or corrupt SQLite identity fields are bounded before
Python hydration; SQLite's unenforced character declarations are not trusted.
Only one bounded snapshot is hydrated at a time. Registered inputs, results,
RuntimeEnv archives and restorable backups are not scanned or deleted.

The elapsed budget is **cooperative, not a hard wall-clock deadline**. Owned
PostgreSQL connections use a 3-second connect timeout, 1-second statement timeout
and 250-millisecond lock timeout. SQLite uses a 250-millisecond busy timeout and a
deadline-aware query progress callback. Trusted settings import, driver/DNS/OS
behavior, cryptography and final cleanup are outside a hard total-time guarantee.
The command closes only its own connection and does not change caller callbacks,
transactions or settings. See the [rotation and required-mode rollout](../runtime-environments.md#maintain-retained-snapshots-and-require-encryption)
before changing policy or removing keys.

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

