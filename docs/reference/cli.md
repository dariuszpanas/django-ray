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

## django_ray_maintenance

Inspect admission policy without changing it:

```bash
python manage.py django_ray_maintenance --json
```

The command supports deployment-wide and exact queue, protocol, and immutable-target
pause controls, exact worker retirement requests, and task-generation quarantine/release.
Changes require `--dry-run` or `--apply`, the exact
`--expected-revision`, `--actor`, `--reason`, and `--authorized` acknowledgment from
a trusted operator shell. Pausing admission does not cancel in-flight work or prove
that a deployment has drained. Entity selectors alone inspect their exact control history; the
command does not certify final worker cleanup. See [Maintenance controls](../operators/maintenance.md)
for the complete options, reviewed dry-run/apply sequence, and permission boundary.

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

## django_ray_doctor

Inspect database connectivity, migrations, protocol rollout state, and recorded
current-cohort metadata in one read-only observation:

```bash
python manage.py django_ray_doctor
python manage.py django_ray_doctor --database=default --json
```

The options are `--database=ALIAS` (default `default`) and `--json`. JSON uses the
independent `django-ray.doctor` schema at version 1 and embeds the existing protocol
report without changing that report's schema. Both formats are capped at 65,536 UTF-8
bytes, including the final newline. Repeated groups are deterministic, limited to 64,
and include omitted totals.

The command observes migration inventory before querying current application tables.
Missing or unknown applied migrations stop that part of the observation. With the
current schema, it reports current target-policy states, recorded proof windows,
capabilities and probe receipts, exact-owner lease freshness, and queued, running,
cancelling, held and orphaned claim totals. Stored timestamps and digests are metadata;
the command does not contact Ray or independently validate a canonical proof. A lease
dated in the future is not treated as heartbeat-live.

The fixed `quarantine`, `worker_retirement`, and `job_cleanup` summaries cover the
entire selected database, using the same observation transaction as the protocol and
claim totals. Quarantine counts use the latest decision and match the exact current
task ID, attempt and generation; released or purged history does not become current
quarantined work. Retirement counts use the latest decision for each exact worker
incarnation, distinguishing a live requested worker, an inactive retired record, and
retained history whose original lease is absent or replaced. Future decisions are
reported separately. Actor/reason text and individual identities are never printed.

OPEN Jobs cleanup remains visible after a terminal result or queued retry, including
uninspectable request references and unavailable exact owners. Closed records remain
historical counts. All unresolved cohort claims remain counted, including any outside
the task's current nonterminal generation. Nonzero unresolved, quarantine, retirement
request and cleanup counts produce fixed database blockers; zero counts never prove
drain, stopped writers, endpoint qualification or remote cleanup. A recorded RETIRED
decision does not certify that the rest of the deployment is drained.

Read `blockers` and `unverified` even when the command exits successfully: exit code 0
means it emitted a report. The report always leaves remote readiness, runtime-qualified
queue serviceability, remote cleanup, drain, upgrade and rollback unverified. It cannot
verify artifact or encryption-key availability, storage pressure, process retirement,
or a backup/restore rehearsal. It does not pause, drain, retire or quarantine work.

The observation reads scalar metadata and aggregates without decoding task payloads,
proof or receipt bodies, importing task callables, or reading external storage. Raw
endpoints, sessions, worker identities and consumption nonces are excluded from output.
PostgreSQL uses a repeatable-read, read-only transaction; SQLite uses one read snapshot.
Callers cannot embed the service in an existing transaction.

## django_ray_benchmark_polling

Compare fixed and adaptive claim polling against the configured PostgreSQL database:

```bash
python manage.py django_ray_benchmark_polling \
  --workers=4 --tasks=100 --idle-seconds=2 \
  --base-interval-seconds=0.1 --max-interval-seconds=0.5 \
  --seed=53 --json
```

The command runs current protocol-3 Sync claims on isolated temporary queues using real
producer intent and the manager's package/Python identity. It measures idle candidate
query load and poll de-synchronization, spaced enqueue-to-claim latency, and preloaded
burst claim throughput. Qualification and maintenance checks occur before priority and
`LIMIT`; actual claims retain the production ledger and skip-locked task transaction.
No application callable or Ray runtime runs. Options control worker count, task count,
idle duration, enqueue interval, poll intervals, overlap window, seed and startup timeout.
`--json` records Django, Python, PostgreSQL, migration, timing and seed metadata. SQLite
and other database engines are refused because their locking behavior is not representative.

Each run emits schema-v2 `protocol_predicate_evidence`. The actual pre-LIMIT candidate
`SELECT` is intercepted before execution and matched against the bounded comparison query.
The SELECT-only control omits just the root task's `protocol=3` equality; it keeps all
qualification predicates and never authorizes a claim. Both variants select the same
exact owned rows using 12 counterbalanced timing pairs. Fixed-vocabulary bounded plans,
p50/p95 timings and signed deltas contain no SQL, queue names, task IDs or row IDs. These
measurements have different semantics from historical schema-v1 range-query evidence.

After owned threads exit, cleanup resolves only retained claims independently verified
under their exact lease/task locks as unchanged, unprepared and undispatched. It records
verified non-invocation and a terminal state before deleting owned benchmark history.
Cleanup is outside measured claim timing. Ownership drift or unconfirmed thread exit
preserves evidence and fails the command; no general history or foreign rows are deleted.

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
| `--retention-days=N` | Require registry use and every terminal finish to be at least `N` days old; default `30` |
| `--delete` | Delete eligible objects after row-locking all references |

The registry's payload kind selects the exact execution reference column. Unknown,
wrong-kind, or dual-column references remain active for operator review. Storage
failures are recorded as a bounded exception class in
`TaskInputPayload.cleanup_error` and make the command exit with an error. Dry-run,
success, and failure output use only a 16-character SHA-256 reference fingerprint; full
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

