# Observability Services

`django-ray` provides versioned, JSON-compatible service functions for durable task
state and a reusable Prometheus renderer. HTTP applications remain responsible for
authentication and authorization; the bundled testproject is an adapter, not the
public API contract.

## Versioned Service Schemas

The public helpers live in `django_ray.observability`:

```python
from django_ray.observability import (
    get_attempt_history,
    get_queue_depths,
    get_task_summary,
    get_workflow_node_snapshot,
    get_workflow_snapshot,
)
```

Every top-level response contains a stable schema name, `schema_version`, and a UTC
`generated_at` timestamp. Schema version `1` is the current contract. Additive fields
may appear within a version; removing fields or changing their meaning requires a new
version.

The task summary intentionally omits task arguments, result contents, tracebacks,
storage references, Ray addresses, and RuntimeEnv JSON. It includes durable identifiers,
queue and priority, lifecycle state, attempt/generation, timestamps, bounded redacted
errors, workflow run ID, and workflow revision.

```python
from django_ray.models import RayTaskExecution
from django_ray.observability import get_task_summary

execution = RayTaskExecution.objects.get(task_id=task_id)
payload = get_task_summary(execution)
```

`get_queue_depths()` groups observed queues into ready, delayed, queued, and running
counts. `get_attempt_history()` returns ordered archived attempts and includes the
current attempt only when it has not already been archived.

## Durable Workflow and Live Ray Data

`get_workflow_snapshot()` wraps the latest durable workflow progress while preserving
the workflow's own stored schema version, run identity, and revision. Its top-level
attempt, execution generation, and workflow run ID remain available while a new run has
claimed ownership but has not flushed its first snapshot. Legacy node-only progress
remains readable.

Workflow revisions are scoped to `workflow_run_id`, not to the durable task forever.
Polling clients must discard a cached graph when that ID, the attempt number, or the
execution generation changes before comparing revisions.

### Bounded progress compatibility

`get_workflow_progress()` is the rolling schema-v1/v2/v3 reader. For a saved
`RayTaskExecution`, it performs a fresh bounded database query instead of trusting a
possibly deferred or stale in-memory payload field:

1. A present schema-v3 summary has precedence and is returned only when its database
   byte length is at most 16 KiB.
2. Only when the summary column is SQL `NULL` may the reader select legacy
   `progress_data`, and only under the 64 MiB compatibility cap.
3. A present but oversized, malformed, noncanonical, unknown, or cross-run value
   produces a bounded diagnostic. It never falls back to stale legacy data.

Schema v3 stores complete internal run identity, strategy/plan identity, monotonic
publication revisions, aggregate counts, availability/completeness/truncation state,
retention, and bounded timestamps. The public helper removes the task database primary
key and internal manifest identifier. It contains no graph records, events, arbitrary
metrics or errors, credentials, paths, URIs, Ray identifiers, or handles.

The current workflow actor still publishes schema v2. Schema-v3 publication stays
disabled until topology/detail storage and its internal readers (#126), followed by the
authorized public detail facade (#127), have deployed and old writers have drained.
Until then, schema-v3 graph and node helpers report detail unavailable rather than
fabricating an empty workflow.

`get_workflow_node_snapshot()` always returns durable node data first. Live Ray state
and logs are opt-in:

```python
node = get_workflow_node_snapshot(
    execution,
    "0.1",
    include_live=True,
    include_logs=True,
    tail=200,
    max_log_bytes=64 * 1024,
)
```

If the Ray State API is unavailable, the response keeps the durable node and reports a
stable unavailable status. It does not turn a live-data outage into loss of durable task
visibility.

Ray logs are bounded independently by line count and UTF-8 byte size, then redacted.
The byte bound applies to each returned stream. Logs are live operational data, not a
durable audit store.

## Prometheus Metrics

`render_prometheus_metrics()` builds text exposition data from the durable database:

```python
from django_ray.metrics import render_prometheus_metrics

payload = render_prometheus_metrics(queue_names=("default", "ml"))
```

Queue labels are emitted only for the explicit allowlist. Omitting `queue_names` emits
no per-queue series, preventing arbitrary database values from creating unbounded label
cardinality. Task state and worker-lease status labels use fixed enums. Metrics never
label by task ID, callable path, worker ID, Ray ID, hostname, exception class, or error
text.

The renderer includes:

- `django_ray_tasks_total{state=...}`, queued, and running gauges;
- `django_ray_queue_depth{queue=...}` for explicitly allowed queues;
- count, sum, average, and maximum gauges for queue wait, claim latency, and execution
  duration;
- durable retry, failure, and timeout observations;
- `django_ray_worker_leases{status=...}` for healthy, stale, and inactive leases;
- an observability schema information metric.

Timing definitions are:

- queue wait: claim time minus original execution creation time;
- claim latency: claim time minus the latest eligibility time (`run_after` or creation);
- execution duration: finish time minus start time across archived attempts plus the
  current attempt when it has not already been archived.

These are database snapshot gauges, not process-local counters. Retry queue wait includes
the execution's earlier lifetime, while claim latency uses the latest persisted retry
eligibility. Timeout observations use django-ray's controlled timeout diagnostic prefix;
applications should not parse arbitrary error text into labels.

Rendering uses a fixed number of aggregate queries, but those aggregates still scan
retained task and attempt history. Use a conventional 30-60 second scrape interval,
apply database retention appropriate to the deployment, and measure query cost before
shortening that interval on a large history table.

## Mounting Metrics Safely

The package does not mount an unauthenticated URL. A Django application can adapt the
renderer to its own authorization policy:

```python
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse

from django_ray.metrics import render_prometheus_metrics


@staff_member_required
def django_ray_metrics(request):
    return HttpResponse(
        render_prometheus_metrics(queue_names=("default", "ml")),
        content_type="text/plain; version=0.0.4; charset=utf-8",
    )
```

For production Prometheus, prefer a dedicated authenticated scrape identity or a
network-restricted reverse-proxy route. Do not reuse a human admin session. The
testproject bearer-protected `/api/metrics` endpoint demonstrates an application adapter.

## Live Django Admin Updates

The package admin task detail page polls a staff-only, object-permission-checked endpoint
for durable task state and workflow progress. Polling uses ordinary same-origin GET
requests, pauses while the tab is hidden, and stops when the task reaches a terminal
state. Responses use `Cache-Control: no-store`.

The polling queryset defers both progress payload columns, then reuses one bounded
compatibility read for the task revision and workflow aggregate. For schema v3, the
panel maps only summary counts and availability; it does not load topology or normalized
detail. During the compatibility window, a legacy value below the explicit 64 MiB cap
may still be decoded, so high-frequency polling should be measured until all writers
have moved off complete snapshots.

The panel does not query Ray or retrieve logs automatically. Operators can explicitly
request live node data through an authorized application surface when needed. This
makes database state visible even during Ray outages.

## Security Boundary

The Python services are authorization-neutral. Every HTTP adapter must authenticate the
caller and enforce access to the referenced task. Redaction is defense in depth, not a
replacement for authorization or encryption. Custom patterns cannot guarantee removal
of arbitrary customer data, and application `print()` calls can still write sensitive
values to Ray logs.

Protect the database, admin, metrics route, Ray dashboard, State API, and storage
backends independently. Never expose live log access by default.

## See Also

- [Operator Runbook](runbook.md)
- [API Reference](reference/api.md)
- [Architecture](architecture.md)
- [Queues](queues.md)
