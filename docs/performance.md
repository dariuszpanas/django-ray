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
)
```

Use fractional CPUs only when a leaf is mostly waiting or deliberately shares a core.
CPU-bound Python or native work should request realistic resources. An oversized
concurrency value can increase memory pressure, API throttling, and retries without
reducing wall-clock time.

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
