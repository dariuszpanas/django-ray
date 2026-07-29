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
errors, workflow run ID, workflow revision, selected workflow strategy, and effective
workflow reporting policy.

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

The package-owned topology/detail storage, bounded integrity verifier, atomic writer,
retention cleanup, and authorized public read facade are present. The current workflow
actor still publishes schema v2 in full mode. A workflow using the disabled runtime
policy creates no actor and publishes no legacy snapshot. Its bounded version-2 plan
selection records the effective policy independently, so current-attempt authorized
readers return `DISABLED` without fabricating a schema-v3 summary or empty graph.
An active full-reporting run without schema v3 remains `NOT_REPORTED`; if that run
reaches a terminal task state without publishing schema v3, readers return `MISSING`
instead of presenting an empty graph. Historical attempts need an archived schema-v3
summary to retain these distinctions; otherwise they remain `NOT_REPORTED`.

Full-mode schema-v3 publication stays disabled until #79 bounds the remaining live
ingestion path, #142 completes ADR-0005's composite topology/detail preparation after
#141's spill-backed topology delivery, and old writers have drained. Until then,
schema-v3 graph and node helpers report detail unavailable rather than fabricating an
empty workflow.

Once activated, `AVAILABLE` and `TRUNCATED` summaries may reference the manifest and
detail revisions committed by the atomic storage writer. `DISABLED` and
`OMITTED_BY_POLICY` are summary-only states: they carry no manifest or detail pointer,
and callers must not interpret them as an empty completed graph.

### Authorized bounded detail reads

The package read facade requires an object authorizer on every call. There is no
allow-all default, and a cursor never substitutes for authorization:

```python
from django_ray.workflow_progress_reads import (
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
)


def can_view(execution):
    return request.user.has_perm("django_ray.view_raytaskexecution", execution)


summary = get_workflow_progress_summary(execution, authorize=can_view)
nodes = list_workflow_topology_nodes(execution, authorize=can_view, limit=100)
edges = list_workflow_topology_edges(execution, authorize=can_view, limit=100)
detail = list_workflow_node_details(
    execution,
    authorize=can_view,
    state="RUNNING",
    limit=100,
)
node = get_workflow_node_detail(execution, "namespace/apply", authorize=can_view)
```

Collection responses default to 100 records and never exceed 256 records, 512 KiB of
encoded response data, or 1 MiB of decoded records. Their opaque cursors are bound to
the complete run and applicable publication epochs, collection, filters, ordering,
applied limit, and cumulative returned count. A retired publication reports `EXPIRED`
with the cursor's original public identity and epoch metadata; it never advances the old
cursor into new rows. Final pages reconcile the cumulative count with bounded run-level
retention counters so missing child rows are not mistaken for complete results. Indexed
node lookup validates the current publication, reconciles bounded run counters and
truncation reasons, and looks up the stable node key without decoding the complete graph.
An absent key is `found: false`; distinguishing an unknown ID from an out-of-protocol
deletion requires paginated traversal or the periodic whole-run audit rather than making
the indexed endpoint scan retained detail.

Pass `attempt_number=` to select retained terminal detail after a retry has advanced the
current task row. The service still authorizes the owning `RayTaskExecution` on every
request, then validates the archived attempt's bounded summary and exact run epochs.

`AVAILABLE` and `TRUNCATED` return retained records while preserving their distinct
completeness values. `NOT_REPORTED`, `OMITTED_BY_POLICY`, `DISABLED`, and `EXPIRED`
return empty bounded envelopes with the exact availability. Missing or corrupt storage
raises a bounded `WorkflowProgressReadError`; readers never fall back to an older
revision or legacy graph.

When a lifecycle-owned successful transition wins before the workflow producer reports
terminal node states, the task summary is successful while detail is `TRUNCATED` with
`terminal_state_unreported`. The retained rows remain readable as the last accepted
node observations, so a state-filtered detail page can still contain `PENDING` or
`RUNNING` rows for that successful attempt. Aggregate task success is authoritative;
the rows are not rewritten or represented as complete terminal detail. Producer-authored
successful terminal detail remains `AVAILABLE` when it is otherwise complete.

The bundled testproject adapts these bounded functions under its bearer authentication
and an explicit callable-path object policy. It exposes bounded summary, topology-node,
topology-edge, node-detail-page, and indexed-node examples below
`/api/cluster/workflows/{task_id}`. The indexed read is
`/node-detail?node_id=...`; the query parameter round-trips bounded UTF-8 identifiers
such as `namespace/apply`. Applications must replace the sample callable allowlist with
their tenant or ownership policy. The old `/graph` endpoint remains a deprecated
schema-v1/v2 compatibility example and should not be used for new clients.

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

The testproject keeps its pre-existing `/nodes/{node_id}` adapter for this live snapshot
contract, including `include_logs` and `tail`. It is separate from the normalized
indexed `node-detail` route.

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

The polling queryset defers both progress payload columns. Its follow-up workflow read
uses a database byte guard for only the 16 KiB schema-v3 summary and explicitly disables
the legacy fallback. It never selects or parses `progress_data`, topology pages, or
normalized detail rows. Active legacy writers therefore appear as not yet reported in
the high-frequency panel. A terminal full-reporting run without schema v3 is explicitly
`MISSING`; compatibility tools may still opt into the separately capped schema-v1/v2
reader.

The change form links to bounded Admin topology and node-detail views. Those views repeat
the same per-object permission check on every page request and call the package read
facade; they are not fetched by the polling script.

By default, the same change form presents ordered immutable attempt history
contextually. The inline selects only attempt identity, state, timing, and a bounded
redacted error preview; it does not load complete tracebacks, results, result
references, or workflow summaries. Oversized error text is replaced by a fixed link
prompt next to the attempt-detail link rather than selecting or displaying an
arbitrary prefix.

Parent access alone does not reveal attempt history. Django renders the inline only
when the caller can view the execution and has global `view_taskattempt` or
`change_taskattempt` permission. The linked attempt detail repeats authorization with
the actual child object, so an object-permission backend may grant a direct detail
read without causing a permission query for every inline row.

`DJANGO_RAY["TASK_ATTEMPT_ADMIN_MODE"]` can select the default `inline`,
`standalone`, or `both` presentation. This is a navigation setting: attempt list and
detail routes remain registered and permission-checked in every mode.

The panel does not query Ray or retrieve logs automatically. Operators can explicitly
request live node data through an authorized application surface when needed. This
makes database state visible even during Ray outages.

## Security Boundary

General task and live-Ray helpers remain authorization-neutral. The bounded workflow
read facade instead requires an explicit object authorizer for every operation. Every
HTTP adapter must still authenticate its caller and express the deployment's tenant or
ownership rule through that authorizer. Redaction is defense in depth, not a replacement
for authorization or encryption. Custom patterns cannot guarantee removal of arbitrary
customer data, and application `print()` calls can still write sensitive values to Ray
logs.

Protect the database, admin, metrics route, Ray dashboard, State API, and storage
backends independently. Never expose live log access by default.

## See Also

- [Operator Runbook](runbook.md)
- [API Reference](reference/api.md)
- [Architecture](architecture.md)
- [Queues](queues.md)
