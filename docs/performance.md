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
django-ray writes compact graph snapshots only when the revision changes, bounded by
`WORKFLOW_PROGRESS_FLUSH_SECONDS`.

This is faster than making every leaf a Django task, but it changes semantics:

- a leaf does not have an independent durable retry record;
- failure fails the outer task;
- retrying the outer task can repeat successful leaves;
- progress is observational, not a recovery log.

Use idempotent leaves and external checkpoints when repeated side effects would be
unsafe.

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

The ordered result payload is still materialized in coordinator memory; a bounded
in-Ray aggregation path is tracked in
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
and has a zero cross-schedule graph-cache hit rate. The current probe covers a
local/direct submitter; the production Ray Client-submitted nested owner remains gated
on separate live-cluster lifetime evidence. See
[ADR-0002](design/adr-0002-compiled-session-ownership.md) for the process matrix,
resource budget, cancellation, and drain requirements.

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
