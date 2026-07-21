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
| `--all-queues` | Process all configured queues |

#### Execution Mode

| Option | Description |
|--------|-------------|
| `--sync` | Run tasks synchronously (no Ray) |
| `--local` | Use local Ray cluster |
| `--cluster=ADDRESS` | Connect to Ray cluster at ADDRESS |
| *(none)* | Use default from `DJANGO_RAY.RUNNER` (`ray_job` by default) |

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

## django_ray_purge_inputs

Report retained external input payloads whose references are all terminal and older
than the selected retention window:

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

Storage failures are recorded in `TaskInputPayload.cleanup_error` and make the command
exit with an error. See [Durable Input Storage](input-storage.md#retries-and-retention)
before scheduling cleanup.

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

