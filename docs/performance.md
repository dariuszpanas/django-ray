# Performance Guide

django-ray has two layers with different costs:

1. A **durable Django task** is enqueued, claimed, retried, and recorded through the
   database.
2. A **Ray-native leaf** is submitted inside an active task and does not create its own
   database row.

Most performance decisions come down to choosing the correct durability boundary.

## Choose an Execution Model

| Workload | Prefer | Why |
|---|---|---|
| One short operation that does not benefit from parallelism | One batched Django task, or ordinary request code | Avoids fan-out overhead |
| Independent expensive items with one final result | `parallel_map()` inside one Django task | Small API and one durable boundary |
| Multiple dependent fan-out/fan-in stages | `chain()`, `group()`, and `map_step()` | Ray-native dependencies plus graph/progress metadata |
| Every item needs an independent retry, schedule, cancellation, or audit row | Separate Django tasks | Durability is worth the database traffic |
| Short, high-throughput cluster work | Ray Core cluster mode | Reuses workers and avoids a driver per task |
| Long job requiring driver isolation | Ray Job mode | Isolation is worth higher startup cost |
| Different Python packages on one trusted cluster | Stable RuntimeEnv profiles | Avoids rebuilding the Ray image for every application version |
| Large dependencies used by nearly every task | Base Ray image | Avoids node-local RuntimeEnv installation |

Queues organize durable tasks; they do not replace Ray scheduling. RuntimeEnv packages
code; it does not provide tenant security.

## Pick Useful Task Granularity

There is no universal item-count threshold. Measure these separately:

- database enqueue, claim, and result-write time;
- Ray submission and scheduling time;
- RuntimeEnv cold and warm setup time;
- useful work per leaf;
- serialization and object-transfer time.

If one leaf's useful work is comparable to its submission overhead, batch several
items into one leaf. For example, prefer 20 namespace tasks that each fetch several
resource types over hundreds of tiny resource tasks when every resource call takes
only a few milliseconds.

Increase granularity until:

- leaves run long enough to amortize submission;
- enough leaves remain to occupy available CPUs or I/O concurrency;
- retries do not repeat an unacceptably large batch.

The best batch size sits between those constraints.

## Keep Database Traffic at the Outer Boundary

A workflow creates one `RayTaskExecution` for the outer Django task. Internal leaves
exchange Ray object references and report events to an in-memory progress actor.
django-ray writes a complete graph snapshot when a changed revision is flushed.
`WORKFLOW_PROGRESS_FLUSH_SECONDS` limits write frequency, but it does not bound the
encoded snapshot, decoded object, task-row, or response size. Those costs currently
grow with the graph and its per-node metadata.

This is faster than making every leaf a Django task, but it changes semantics:

- a leaf does not have an independent durable retry record;
- failure fails the outer task;
- retrying the outer task can repeat successful leaves;
- progress is observational, not a recovery log.

Use idempotent leaves and external checkpoints when repeated side effects would be
unsafe.

## Benchmark Workflow Progress Persistence

Use the opt-in workflow-progress benchmark to compare the current complete-row
snapshot with bounded alternatives before changing the production persistence
contract. The command builds deterministic logical node/payload records and exercises
Django plus a disposable PostgreSQL database. It does not import Ray, start a Ray
runtime, execute native Compiled Graphs, or add any candidate representation to
production.

Install the PostgreSQL dependency set, activate that environment, and point
`tests.postgres_settings` at an isolated PostgreSQL database. Never use a production
database: the benchmark creates and removes transient representative rows and measures
database-wide effects. Prepare the schema, then run the fixed comparison locally:

```bash
export DJANGO_SETTINGS_MODULE=tests.postgres_settings
export DATABASE_NAME=django_ray
export DATABASE_USER=django_ray
export DATABASE_PASSWORD=django_ray
export DATABASE_HOST=127.0.0.1
export DATABASE_PORT=5432

python testproject/manage.py check
python testproject/manage.py migrate --noinput
mkdir -p artifacts
python testproject/manage.py django_ray_benchmark_workflow_progress \
  --nodes 1000 10000 50000 100000 \
  --change-rates 0.01 0.10 1.0 \
  --repetitions 5 \
  --warmups 1 \
  --seed 20260720 \
  --database-deployment local-postgresql \
  --output-json artifacts/workflow-progress-benchmark.json \
  --output-markdown artifacts/workflow-progress-benchmark.md
```

The `nodes` values are logical workflow-node counts. A change rate of `0.01` or
`0.10` models a sparse publication after 1% or 10% of nodes change; `1.0` models a
terminal publication in which every node has changed. Each case retains its first
measurement as `cold_sample`, then discards the requested warm-up measurements before
recording its five `warm_samples`. A fixed seed makes payload content and modeled
change distribution reproducible; this command does not construct dependency edges.

The benchmark-only representations have these meanings:

| Representation | What the benchmark models |
|---|---|
| `current_full_row` | The current behavior: re-encode and replace one complete graph snapshot in the task row. |
| `bounded_inline` | A fixed-size task-row summary plus count- and byte-bounded invocation detail retained inline. |
| `chunked_database` | A bounded task-row summary with bounded detail split into database chunks. |
| `normalized` | A bounded summary with node detail stored as independently addressable relational rows. |
| `append_delta` | A bounded summary plus revision deltas, with reads reconstructing detail from retained changes. |
| `external_chunk` | A bounded database summary and opaque pointer while encoded detail is modeled as externally stored chunks. |
| `live_only` | Bounded durable summary data with invocation detail available only from the live producer. |

These are comparison models, not supported storage modes. In particular, the
`external_chunk` case does not turn a benchmark artifact into a public storage
reference, and the `live_only` case does not promise that detail survives producer
loss.

The versioned JSON has these top-level fields: `schema_version`, `benchmark`,
`environment`, `configuration`, `profiles`, `candidates`, `cases`, and
`database_evidence`. Together they retain the configured matrix and seed, core
dependency and platform identity, PostgreSQL identity and schema state, payload
profiles, and candidate descriptions. Each `cases` entry identifies `profile`, `candidate`, `nodes`,
`change_rate`, `changed_nodes`, and a deterministic `workload_fingerprint`. Its
`write_amplification` records changed items, typed modeled storage units, estimated
database statements, and task, detail, external, and total encoded bytes. External
operations are not counted as database statements. `cold_sample` and `warm_samples`
retain snapshot and serialization measurements, encoded and decoded bytes,
parse/redaction work, summary/page/single-node read timings and response bytes, and
best-effort process memory evidence.

The same `workload_fingerprint` is intentionally shared by all seven candidates for a
given profile, node count, and change rate. It identifies a comparable workload, not a
unique case.

Payload profiles cover short and maximum-size metadata, multibyte UTF-8,
secret-bearing values, metric-cardinality limits, compressible content, and seeded
incompressible content. `database_evidence` holds the best-effort PostgreSQL temporary
table measurements, including `pg_column_size`, relation growth, and WAL evidence when
the server can provide them. Optional database evidence may be `unavailable` with an
explicit reason without failing the benchmark; an unavailable metric remains distinct
from a measured zero.

Treat the JSON file as the source of truth for analysis. It retains environment data,
the cold and individual warm samples, full-precision values, and unavailable-metric
information needed to recompute comparisons. The Markdown file is a concise, rounded
view derived from that JSON. Its first table pools heterogeneous cases into
candidate-wide medians across the complete matrix; the sparse-focus table is one
specific comparison. Use it to orient a decision and the GitHub Actions job
summary, not to replace the raw repetitions or compare runs with different
environments.

The environment also records the SHA-256 digest of the benchmark implementation.
Manual GitHub Actions runs record their exact `GITHUB_SHA`; local evidence reports the
source revision as unavailable instead of guessing. The configured matrix records
that the command touches only Django and its configured database, never Ray or
Kubernetes.

Python timings cover construction, serialization, parsing, redaction, and in-process
response shaping of bounded representative structures. They exclude ORM queries,
database and network round trips, and external-object operations. Write bytes and
database statement counts are analytical. The PostgreSQL probe inserts one bounded
representative document per candidate to capture JSONB row size, relation growth, and
WAL evidence; it does not execute seven production candidate schemas or establish
throughput. Real schema/query benchmarks remain required with the implementation.

The ADR-0004 decision uses a committed [Windows 11/PostgreSQL 17 summary](benchmarks/workflow-progress-storage-postgresql17-windows-2026-07-20.md)
and its [authoritative raw JSON](benchmarks/workflow-progress-storage-postgresql17-windows-2026-07-20.json).
That local run used a disposable Docker database; the command did not start Ray or
access Kubernetes. It therefore does not validate the image or package version in any
Kubernetes deployment.

For the modeled uniformly distributed sparse update, immutable 256-item detail pages
touched most pages even when only 1% of nodes changed. ADR-0004 therefore retains
immutable pages only for normally static topology and selects normalized latest-state
rows for mutable node detail. The benchmark compares storage shapes; it does not add
that schema or change production persistence.

To collect the reference Ubuntu/PostgreSQL 17 series, open **Actions**, select
**Workflow Progress Benchmark**, choose the branch or commit under test, and select
**Run workflow**. The manual job runs the same fixed command and uploads both files in
the `workflow-progress-benchmark-<run>-<attempt>` artifact, including any files that
exist when the benchmark step fails. It is intentionally absent from pull-request CI.

Do not turn its elapsed-time values into required pull-request thresholds. Shared
runner load, CPU scheduling, filesystem cache state, PostgreSQL checkpoints,
autovacuum, and fsync behavior add noise that correctness tests cannot normalize.
Compare candidates within one run, repeat material decisions on representative
infrastructure, and retain the raw artifacts with the ADR evidence.

PostgreSQL byte metrics answer different questions. `pg_column_size` describes a value
or row representation, not the table's complete disk cost. Relation growth can include
indexes, TOAST data, free space, and dead tuples, and it need not fall when rows are
deleted. WAL deltas vary with server settings, checkpoints, full-page writes,
compression, and concurrent activity. Measure on an otherwise idle disposable
database, record unavailable WAL evidence explicitly, and compare WAL or relation
deltas only between cases collected under the same server configuration.

## Benchmark Workflow Progress Preparation

ADR-0005's opt-in preparation benchmark measures the non-production SQLite spill
prototype without changing the runtime preparer. Each sparse or high-edge scenario
runs in a fresh subprocess, consumes topology and initial-detail generators once, and
removes its private workspace before the parent accepts the result. It does not start
Ray, write Django database rows, access Kubernetes, or activate schema-v3 production.

Run the required decision matrix on Linux from an otherwise idle checkout:

```bash
mkdir -p artifacts /tmp/django-ray-issue140-benchmark
python scripts/benchmark_workflow_progress_preparation.py \
  --required-scale \
  --timeout-seconds 7200 \
  --workspace-parent /tmp/django-ray-issue140-benchmark \
  --output artifacts/workflow-progress-preparation.json
```

`--required-scale` expands to 25,000, 100,000, and 250,000 observed nodes for both a
sparse chain and a deterministic high-edge graph. The latter crosses the retained
100,000-edge cap without constructing a quadratic graph. Smaller explicit `--nodes`
values are smoke checks only and cannot satisfy the ADR evidence gate.
The explicit two-hour per-case timeout accommodates the deliberately expensive exact
high-edge path; it is a watchdog, not a performance target.

The JSON records the exact source and implementation identity, environment and
filesystem characteristics, effective SQLite settings, configured cache/batch/item/
spill limits, observed and retained cardinalities, wall and CPU time, Python allocation
peak, explicitly scoped RSS evidence, spill high-water bytes, query plans, and child
plus parent-watchdog cleanup outcomes. A separate forced-termination control proves
that the parent removes a killed child's workspace. An unavailable memory metric is
reported as unavailable rather than as zero, and a cleanup failure fails the run.

The committed [WSL2 Linux summary](benchmarks/workflow-progress-preparation-sqlite-wsl2-linux-2026-07-21.md)
and [authoritative JSON](benchmarks/workflow-progress-preparation-sqlite-wsl2-linux-2026-07-21.json)
join the focused canonical and lifecycle tests to satisfy ADR-0005's issue-#140 evidence
gate. They are local contract evidence, not a production latency SLO. Compare resident
growth only after the retained node, edge, and detail caps are reached, and keep #79's
wire, mailbox, and producer allocations separate from this preparation-only
measurement.

## Control Fan-Out

Do not equate maximum submission count with maximum throughput.

```python
from django_ray.runtime.distributed import parallel_map


def process_one(item_id: int) -> int:
    return item_id * item_id


item_ids = list(range(100))
results = parallel_map(
    process_one,
    item_ids,
    num_cpus=0.25,
    max_concurrency=32,
)
```

For workflows, set per-leaf `ray_options`:

```python
from django_ray.workflows import map_step


def process_one(item_id: int) -> int:
    return item_id * item_id


fanout = map_step(
    process_one,
    ray_options={"num_cpus": 0.25},
).with_limits(
    max_concurrency=32,
    max_items=10_000,
)
```

The map window limits submitted map items and retained result references. Results are
collected incrementally but returned in input order. Nested groups can submit multiple
physical tasks per item, so include their fixed branch count when sizing the window.
Input admission starts only after `executor.resolve()`: an upstream Ray task still
produces its complete list, which is transferred and deserialized into coordinator
memory before fan-out begins. Only an iterator already available locally is pulled
lazily. Remote or paged input materialization is tracked in
[GitHub issue #94](https://github.com/dariuszpanas/django-ray/issues/94).

By default, the ordered result payload is still materialized in coordinator memory.
An explicitly bounded map can use
[`with_result_buffer()`](workflows.md#opt-in-ray-result-buffer) to retain versioned
serialized bytes in a resource-accounted Ray actor and forward one direct payload
reference to a downstream Ray step. The actor, object store, and final consumer still
have O(total output) boundaries. When only a compact summary is needed, use the explicit
[`reduce()` ordered fold](workflows.md#opt-in-ordered-result-fold). Its actor retains one
serialized accumulator and at most `max_concurrency - 1` out-of-order results, while
incorporation-based admission deliberately applies head-of-line backpressure to preserve
strict input order. Benchmark the reducer cost and accumulator size as well as map
throughput. List-preserving chunk or hierarchical transport remains tracked in
[GitHub issue #91](https://github.com/dariuszpanas/django-ray/issues/91). Calling
`map_step()` without `with_limits()` retains the legacy eager behavior.

Use fractional CPUs only when a leaf is mostly waiting or deliberately shares a core.
CPU-bound Python or native work should request realistic resources. An oversized
concurrency value can increase memory pressure, API throttling, and retries without
reducing wall-clock time.

For an API-limited workload, map batches rather than individual tiny requests and keep
the client's rate limiter enabled: a concurrency window bounds simultaneous batches,
not requests per second. For Kubernetes synchronization, compare namespace-sized batches
with `(namespace, resource_kind)` batches. Choose the smallest batch that still makes
Ray scheduling overhead minor, and set `max_items` to reject accidental discovery
explosions. See [Bound Dynamic Fan-Out](workflows.md#bound-dynamic-fan-out) for failure
and cleanup semantics.

Avoid passing large repeated values to every leaf. Put shared immutable data in Ray's
object store once, or load it in a preceding workflow step and pass references through
the graph.

## Runtime Environment Cost

RuntimeEnv caches are per Ray node. A four-node cold fan-out may prepare the same
environment on four nodes before useful work begins. Repeated tasks can then reuse each
node's cache.

For predictable latency:

- use a bounded set of named profiles;
- use immutable code archive URIs;
- pin production package versions;
- warm each node before directing latency-sensitive work to it;
- bake system libraries and large common Python dependencies into the Ray image;
- reserve RuntimeEnv for application code and genuinely variable dependencies.

Always compare cold and warm runs. A single first-run measurement mostly benchmarks
packaging and installation.

## Ray Core, Ray Job, and Compiled Graphs

Ray Core is django-ray's low-latency path. Ray Job adds an isolated driver and is a
better fit for coarse work.

django-ray workflow signatures currently build ordinary Ray task graphs. They do not
use Ray Compiled Graph. Compiled Graph is aimed at repeated execution of a fixed graph
from a long-lived Ray driver; django-ray's dynamic maps, durable outer-task lifecycle,
and per-run graph metadata have different requirements. It may become useful for a
future reusable fixed-DAG execution mode, but it is not a switch that removes
RuntimeEnv, database, or first-run costs.

The [workflow-plan contract](workflow-plans.md) defines how static, dynamic, and
fixed-width workloads are classified and which plan, invocation, ownership, and
eligibility data a future strategy must use.

The first experimental ownership boundary is deliberately limited to compiling once
and invoking repeatedly inside one Ray Core durable task. For a scheduled Kubernetes
sync, one generic fixed-width kernel may process several namespaces during that one
schedule. The graph is still rebuilt for the next schedule, so this is within-run reuse
and has a zero cross-schedule graph-cache hit rate. The current probe covers the
`direct-ray-core` submission transport; the production Ray Client-submitted nested
owner has a distinct compatibility identity and remains gated on separate live-cluster
lifetime evidence. See
[ADR-0002](design/adr-0002-compiled-session-ownership.md) for the process matrix,
resource budget, cancellation, and drain requirements.
Compatibility evidence must also pin the specific container and immutable
deployment/image identity and record explicit shared-memory and Ray object-store
configuration. Measurements from a generic host, `docker`, or unresolved environment
cannot be promoted as a native-capability row.

## Benchmark the Real Shape

The testproject provides:

```text
POST /api/cluster/workflow-benchmark?num_items=20&seconds_per_item=0.25
GET  /api/cluster/workflow-benchmark/{task_id}

POST /api/cluster/runtime-env/benchmark?profile=numpy-2-3&package=numpy&repeats=3
GET  /api/cluster/runtime-env/{task_id}
```

Run at least:

1. cold environment, cold workers;
2. warm environment with the same profile;
3. one large batch;
4. several leaf batch sizes;
5. Ray Core versus Ray Job when isolation is optional.

Record queue wait, environment setup, leaf runtime, and total duration separately.
Optimizing only the function body can miss the dominant cost.

## Benchmark Worker Polling

Run the polling benchmark against a disposable PostgreSQL database with no production
workers consuming the generated benchmark queue:

```bash
python manage.py django_ray_benchmark_polling \
  --workers=4 \
  --tasks=100 \
  --idle-seconds=2 \
  --enqueue-interval-seconds=0.05 \
  --base-interval-seconds=0.1 \
  --max-interval-seconds=0.5 \
  --seed=53 \
  --json
```

The command runs fixed-100-ms and adaptive policies sequentially through the production
worker claim loop. Both execute its real `SELECT ... FOR UPDATE SKIP LOCKED` query on
isolated queues. Independent phases report idle claim and total SQL queries per
worker-second, peak distinct-worker overlap and a sliding-window overlap ratio, spaced
enqueue-to-claim p50/p95, and preloaded-burst claim throughput. The enqueue timestamp is
captured before the task row is inserted, so latency includes enqueue database time.
Generated rows are validated for exact, unique ownership before deletion. The command
refuses non-PostgreSQL databases because SQLite cannot represent the multi-worker locking
behavior being measured.

For a scaling series, keep the database and task shape fixed and run `--workers=1`,
`4`, and `8`. Repeat each case at least five times after a warm-up run. Record PostgreSQL
version, host resources, connection-pool settings, database distance, worker count,
task count, enqueue interval, both polling intervals, schema version, and seed with every
result. Use the same seed when comparing policies; repeat runs still capture scheduler
and database variance.

The manually dispatched **Polling Benchmark** workflow runs a warm-up and five recorded
repetitions for each worker count. Launch it from GitHub Actions when polling behavior or
its operating environment changes, then download the `polling-benchmark-json` artifact
for exact environment metadata and individual measurements. Normal pull-request CI keeps
the PostgreSQL coordination and polling correctness tests but does not run this
time-based matrix. Performance varies with shared runner capacity, so the benchmark
checks claim integrity and finite metrics rather than imposing noisy latency or
throughput thresholds. Do not substitute SQLite or simulated timings.

The following values are medians from five repetitions after warm-up in
[GitHub Actions run 29703449242](https://github.com/dariuszpanas/django-ray/actions/runs/29703449242)
on 2026-07-19. The environment was PostgreSQL `server_version_num=170010`, Python
3.12.13, Django 6.0, and schema `0008_raytaskexecution_priority_constraint` on a shared
Azure Linux runner. Each policy used 100 tasks per phase, a 50 ms enqueue interval, a
2-second idle window, and a 25 ms overlap window.

| Policy/workers | Claim p50 (ms) | Claim p95 (ms) | Idle queries/worker/s | Idle overlap | Throughput (claims/s) |
|---|---:|---:|---:|---:|---:|
| Fixed 100 ms / 1 | 2620.0 | 4931.5 | 9.99 | 0% | 9.7 |
| Adaptive 100-500 ms / 1 | 2412.7 | 4283.8 | 3.50 | 0% | 10.7 |
| Fixed 100 ms / 4 | 56.9 | 99.2 | 9.49 | 100% | 38.5 |
| Adaptive 100-500 ms / 4 | 30.6 | 89.6 | 3.25 | 68.2% | 43.3 |
| Fixed 100 ms / 8 | 50.9 | 104.8 | 9.49 | 100% | 73.4 |
| Adaptive 100-500 ms / 8 | 15.7 | 51.9 | 3.19 | 93.0% | 85.1 |

The single-worker latency phase deliberately offers 20 tasks per second to a worker that
claims about 10 per second, so its growing queue produces multi-second latency. Treat that
row as saturation behavior, not idle wake-up latency. In this run, adaptive polling cut
idle claim queries by roughly two-thirds without reducing burst throughput. Shared-runner
values are evidence for comparison and tuning, not service-level objectives.
