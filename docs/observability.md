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
actor still publishes schema v2 in full mode. Terminal-only and disabled workflows
create no actor and publish no legacy snapshot. Their bounded version-2 plan selection
records the effective policy independently.

On accepted durable success or failure, terminal-only mode attempts one revision-1
schema-v3 summary containing pinned plan identity, declared counts, terminal outcome,
and bounded timestamps. It reports zero discovered or executed nodes,
`OMITTED_BY_POLICY` detail, and no topology/detail pointers or rows. Summary
serialization or database attachment failure rolls back only the observability
attachment; the task result or application error remains authoritative. Disabled mode
does not attempt that summary, so current-attempt authorized readers return `DISABLED`
without fabricating an empty graph.

An active full-reporting run without schema v3 remains `NOT_REPORTED`; if that run
reaches a terminal task state without publishing schema v3, readers return `MISSING`.
Terminal-only uses explicit pending, missing, and available-summary presentations and
never advertises topology, node detail, or an execution graph. Historical attempts
need an archived schema-v3 summary to retain these distinctions; otherwise they remain
`NOT_REPORTED`.

General/default full-mode schema-v3 publication stays disabled until #79 bounds the
remaining live ingestion path, #142 completes ADR-0005's composite topology/detail
preparation after #141's spill-backed topology delivery, and old writers have drained.
The default-off `WORKFLOW_PROGRESS_SCHEMA_V3_PILOT` is the only current producer
exception: one admitted terminal snapshot may make bounded graph and node helpers
available for that run. Runs without an accepted pilot publication report detail
unavailable rather than fabricating an empty workflow.

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
the complete run, summary revision, applicable publication epochs, collection, filters,
ordering, applied limit, and cumulative returned count. A newer summary revision reports
the old cursor as `EXPIRED` with its original public identity and epoch metadata; it never
advances the old cursor into new rows. Final pages reconcile the cumulative count with
bounded run-level retention counters so missing child rows are not mistaken for complete
results. Indexed node lookup validates the current publication, reconciles bounded run
counters and truncation reasons, and looks up the stable node key without decoding the
complete graph.
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
their tenant or ownership policy. The pre-0.4.0 `/graph` complete-graph example was
removed. Existing schema-v1/v2 rows remain aggregate-readable through the summary
route, while topology and node detail require the bounded page routes.

The testproject's `GET /api/tasks/{task_id}` is a narrower status surface than the
package Python `TaskResult`. One unlocked projection returns status and durable attempt
identity without loading a callable, external input, result, or result backend. Its
nullable `args` and `kwargs` share a 16,384-byte inline-input guard, the whole response is
at most 65,536 bytes, and `input_omission_reason` is limited to
`external_input_not_loaded`, `stored_input_exceeds_status_limit`,
`malformed_inline_input`, and `encoded_response_limit`. Package `TaskResult` remains the
application-data interface for full arguments, keyword arguments, and successful return
values under the application's own trust boundary. The testproject's database-side byte
guard supports SQLite and PostgreSQL; other database semantics fail configuration
explicitly.

The workflow example pollers and `GET /api/cluster/runtime-env/{task_id}` likewise use
exact projections. Current inline result and error values are guarded at 16,384 bytes
each, external results are never loaded, and each complete response is at most 65,536
bytes. The fixed result omission reasons are `external_result_not_loaded`,
`stored_result_exceeds_poll_limit`, `malformed_inline_result`, and
`encoded_response_limit`; errors use `stored_error_exceeds_poll_limit` or
`encoded_response_limit`. Workflow progress is exposed only through a bounded aggregate
summary envelope. A published schema-v3 summary is preferred; supported older stored
progress may contribute sanitized aggregate counts, but never its complete graph.
Recovery history additionally guards each
of at most four archived attempt errors at 4,096 bytes and reports
`stored_error_exceeds_attempt_limit` or `encoded_response_limit` when one is omitted.
These polling byte guards have the same explicit SQLite/PostgreSQL support boundary.

The separate testproject `GET /api/executions` example has its own proven bounds: a
1-to-100 page size (50 by default), database-side 4,096-byte guards for inline result
and error values, and a 256 KiB encoded-response ceiling. Fixed omission and
truncation reasons distinguish a stored diagnostic that was not loaded
(`stored_value_exceeds_list_limit`) from a page that stopped at either the page-size
(`page_limit`) or response-size (`response_size_limit`) boundary. Its signed,
filter-bound `next_cursor` uses keyset continuation after the last complete returned
item, so concurrent inserts do not shift the next page. An included malformed, deep,
or conversion-failing result becomes the fixed `[REDACTED]` marker with a `null`
omission reason because the field was redacted rather than omitted. The list byte guard
is supported on the testproject's SQLite and PostgreSQL paths; other database LOB
semantics fail configuration explicitly rather than inheriting this bounded claim.
Each item also reports the durable execution protocol, nullable creator/manager/executor
package provenance, and a single-query heartbeat-live protocol compatibility
annotation. Provenance is guarded at 128 UTF-8 bytes in SQL and passes through the
configured presentation redaction policy. The fixed
`queue_capacity_attested=false` field prevents that annotation from being mistaken for
queue routing, free concurrency, Ray readiness, Ray/Python compatibility, or target
identity evidence.

The separate `GET /api/executions/{id}` exact operator lookup has its own bounded
contract. A single values projection selects only public response fields and applies
65,536-byte database guards to inline result and error values. Unreturned inputs,
tracebacks, RuntimeEnv data, workflow data, and completion envelopes do not cross the
database boundary. `stored_value_exceeds_detail_limit` identifies an oversized inline
diagnostic, while `external_result_not_loaded` identifies a result reference that the
route deliberately does not resolve. Malformed, truncated, too-deep, or
Unicode-conversion-failing inline result JSON becomes the fixed `[REDACTED]` marker;
this is redaction, so the omission reason remains `null`. Redaction can expand an
included valid value, so the renderer enforces a separate 256 KiB response ceiling: it
omits result before error with `response_size_limit`, then returns the fixed
`execution_detail_response_limit` `503` if bounded metadata still cannot fit. An
ordinary exception from either renderer attempt also fails closed to that
diagnostic-free response; process-control exceptions propagate. Responses disable
caching and MIME sniffing. This byte projection has the same explicit
SQLite/PostgreSQL support boundary as the list. Global bearer authentication remains
a testproject convenience; an application must impose its own tenant, ownership, or
object policy on the exact lookup.

The bounded task-status adapter exposes the same protocol fields and availability
semantics without loading results or external input. All three adapters freeze one
heartbeat cutoff per query and keep the compatibility test inside the execution SQL,
so list cardinality does not create worker-lease N+1 reads.

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

The pre-existing testproject `/nodes/{node_id}` live adapter is removed in 0.4.0. Its
HTTP replacement is the durable indexed `/node-detail?node_id=...` reader after the
same callable authorization as other workflow reads. Applications can still use
`get_workflow_node_snapshot()` behind a separately authorized, application-owned live
diagnostic surface; the sample callable allowlist is not a tenant or task-owner policy.

Ray logs are bounded independently by line count and UTF-8 byte size, then redacted.
The byte bound applies to each returned stream. Logs are live operational data, not a
durable audit store.

### Terminal-formatted failure diagnostics

Ray status messages and log tails may contain terminal formatting even when an
application expects plain text. Django-ray deliberately preserves the original failure
message, traceback, and cancellation evidence in the protected execution and attempt
fields. Normalizing those values before storage would be irreversible: a valid terminal
sequence can consume a character from a configured secret marker, preventing a later
reader from recognizing it, and privileged incident readers need the remaining printable
diagnostic. Internal completion envelopes use JSON framing, so control characters are
escaped while crossing that boundary.

Every package-owned reader removes bounded ANSI CSI and OSC/DCS/APC/PM/SOS control
strings, the remaining C0/C1 controls, unsafe Unicode format/bidi characters, and
invalid surrogate code points before text leaves the protected diagnostic boundary.
CRLF and lone carriage returns become one logical newline; tabs, newlines, ordinary
printable Unicode, emoji joiners/variation selectors, and private-use glyphs remain
intact. Matching-only projections additionally ignore Unicode default-ignorable
characters so harmless shaping cannot split a sensitive marker. Incomplete or malformed control
sequences lose their control introducer, so they cannot silently consume the printable
traceback which follows them.
Within every 7-bit or C1 control string, CAN and SUB cancel the hidden payload and
resume ordinary parsing; visible text after cancellation remains available to
fail-closed redaction matching.
The unsafe-format and default-ignorable tables are frozen from Unicode 16.0 rather than
read from the interpreter at runtime. The same code point therefore has the same display
and matching projection on supported Python 3.12, 3.13, and 3.14 processes even though
their bundled Unicode databases differ.
The streaming parser reads each input position once, performs bounded projection work,
and caps speculative control-string state, avoiding repeated suffix scans for malformed
unterminated input. Ordinary redaction fails closed before projecting a text value or
mapping key longer than 65,536 characters. Admin, graph, log, protocol, API, and
observability limits remain surface-specific rather than inheriting one universal HTTP
response bound.

Ordinary Admin, API, graph, logging, observability, and Django `TaskResult.errors`
projections use the shared terminal-normalization and pattern-redaction boundary before
their surface-specific display bounds. The permission-gated sensitive Admin view
normalizes and bounds the same allowlisted fields but deliberately skips pattern
redaction. Direct database consumers must treat the stored fields as raw sensitive
evidence and apply an equivalent authorized presentation boundary before rendering them.

Ordinary execution and attempt detail pages additionally use audited SQL projections.
SQLite measures the UTF-8 storage through a BLOB cast and PostgreSQL uses
`OCTET_LENGTH`; a `CASE` expression returns a diagnostic field only when it is at most
4,096 characters and 16 KiB. Oversized values produce a fixed notice rather than
transferring a prefix, and the raw model field stays deferred throughout rendering.
Stored JSON that cannot be parsed and redacted safely also produces a fixed notice;
its malformed raw text remains available only through the separately authorized
Sensitive data view.
Each complete page also has a fixed encoded-response ceiling and sends
`Cache-Control: no-store`. If ordinary template rendering raises an application
exception, the view discards it and returns a fixed diagnostic-free `503` within the
same ceiling with `no-store` and `nosniff`; process-control exceptions are not swallowed.
The ceiling runs at Django's lazy render boundary after in-place template-response
middleware changes and includes the response returned by post-render callbacks. A project
middleware that replaces the response object, or changes it later in `process_response`,
owns the replacement's size and cache/security headers; no view-level hook can inspect
that later object. Immutable change URLs accept only `GET` and `HEAD`; `POST` returns a
fixed `403` and other methods return `405` before an object query. Stock history and delete
URL patterns are not registered for executions or attempts, and matching direct paths
return `404` before either model is queried.
These ordinary bounds do not widen, replace, or bypass the separately authorized
Sensitive data view.
Numeric limits are surface-specific workload budgets, not a privilege hierarchy; a
larger limit does not imply broader authorization or access to more sensitive fields.

Redaction checks raw, normalized, control-removed, and composed terminal forms. A
configured marker therefore remains sensitive when terminal formatting, Unicode
zero-width shaping, or more than one control-sequence family splits its characters.
Accepted patterns compile during startup into one bounded program; evaluation has a hard
250,000-unit matcher ceiling and fails closed rather than enumerating ambiguous terminal
forms. The transition and character caches are independently capped; repeated ordinary
text benefits from them, while high-entropy inputs stop at the same deterministic budget.
One structured root shares that matcher budget, a 4,096-item traversal ceiling, and a
65,536-character aggregate ceiling across nested string keys and values.
Mapping keys, including structured-log extra keys, are normalized and matched through
the same projections; a
sensitive-looking key is replaced with the fixed `<redacted>` marker and its value is
redacted. Marker and normalized-key collisions cannot replace an earlier redaction.
Ordinary exact string keys retain their normalized text; other Python key types are
represented only by type so user-defined string conversion cannot enter diagnostics.
Workflow-progress producer and storage mappings persist normalized
keys, reject ambiguous collisions, and reject node/edge identities which normalization
would change. Structured logging consumes placeholders with already-redacted arguments
before removing terminal sequences; all positional arguments share one traversal budget,
as do adapter and call-time structured fields. A hidden placeholder therefore cannot
shift or invalidate visible arguments, while disabled levels remain lazy and do not
evaluate message text.
Normalization is not secret removal and does not weaken the existing requirement for
authorization and application-specific redaction patterns. Admin tracebacks and graph
failure messages are inserted as escaped text, retain line separation with scoped
`pre-wrap`, and wrap long unbroken paths without enabling HTML interpretation.

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
- `django_ray_tasks_by_execution_protocol_total{protocol=...,state=...}` with exactly
  the fixed protocol buckets `1` and `other` crossed with every fixed `TaskState`;
- `django_ray_queue_depth{queue=...}` for explicitly allowed queues;
- count, sum, average, and maximum gauges for queue wait, claim latency, and execution
  duration;
- durable retry, failure, and timeout observations;
- `django_ray_worker_leases{status=...}` for healthy, stale, and inactive leases;
- an observability schema information metric.

The execution-protocol family always emits all 16 bucket/state combinations, including
zero-valued series. Protocol `1` is the released compatibility epoch; `other` combines
every non-`1` integer without creating one label per future or corrupt value. Unknown
database states are excluded before grouping. Package versions, worker IDs, and raw
queue names never become labels.

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

The polling queryset projects its error through the same 4,096-character/16 KiB
database guard and defers both progress payload columns and the complete workflow plan
and strategy-selection snapshots. Its follow-up workflow read uses a separate database byte
guard for only the 16 KiB schema-v3 summary and explicitly disables the legacy fallback.
It never selects or parses a complete oversized error, `progress_data`, the plan blobs,
topology pages, or normalized detail rows. Runs without a schema-v3 summary therefore
appear as not yet reported in the high-frequency panel, regardless of plan policy. The
lazy workflow diagnostics
request distinguishes disabled reporting, active and missing terminal-only reporting,
an active requested-but-not-reported full run, and a terminal requested-but-missing
snapshot. A terminal-only summary shows its outcome and omitted-by-policy detail without
offering graph or collection actions. Compatibility tools may still opt into the
separately capped schema-v1/v2 reader.

The live summary also returns the execution protocol, SQL-guarded nullable package
provenance, `protocol_compatible_worker_available`, and
`queue_capacity_attested=false`. The ordinary change form uses the same bounded
provenance display and read-only availability annotation. These are database
compatibility observations only and never query Ray or infer per-queue capacity.

The initially collapsed **Workflow execution** section performs a separate authorized
read only when an operator opens it. The compact view verifies the persisted plan
fingerprint and selection schema, groups strategy rejection counts by stable code, and
explains whether bounded progress is available, partial, expired, disabled, legacy-only,
requested but not reported, missing, or corrupt. It does not render raw plan JSON,
rejection paths, or rejection messages in the task form.

Verified plan and selection JSON remain available as explicit, byte-bounded downloads.
The lazy summary and downloads are GET-only, repeat the per-object permission check, use
SQL byte guards before loading the blobs, and return `Cache-Control: no-store`. Topology
and node-detail actions in the compact diagnostics row appear only when the retained
schema-v3 collection can return useful data. For a verified full-reporting plan, the
nested graph control also exposes the three fixed bounded JSON routes as explicit
fallbacks whenever graph rendering degrades, including before a terminal publication
exists. An unverified plan or terminal-only policy exposes neither the graph control nor
those detail links. Every route repeats the same authorization and calls the package
read facade; none is fetched by the polling script.

Within that collapsed section, **Attempt execution graphs** keeps every retained run in
oldest-to-newest order. Each attempt is a second, independently collapsible control. The
current attempt makes exactly one same-origin GET after it is opened and caches a
successful response for the current page. A pre-terminal `NOT_REPORTED` response and a
transport or malformed-response failure remain retryable after the control is closed and
reopened. Authentication loss, missing objects, and stable terminal degradation do not
cause a request loop.

The private graph adapter accepts only one complete, terminal, internally coherent
schema-v3 publication. It performs fixed first-page reads with ceilings of 100 topology
nodes, 256 edges, 100 node details, and 128 KiB of encoded response data. Any cursor,
cycle, unknown edge endpoint, identity or publication mismatch, incomplete count,
truncation, or limit breach produces an explicit empty degraded response rather than a
partial graph.

The response is a redacted display projection, not another raw workflow API. Its
allowlist contains node identity, bounded label, task/map kind, fixed state, bounded
progress message, sanitized map fan-out counters, bounded failure text, edges, and
failure-path flags. It never includes callable paths, arguments, results, RuntimeEnv
data, execution identifiers, raw progress metrics or events, plan data, or storage
records. Semantic node links follow topological and tab order and are pinned to the same
attempt as the rendered graph. The graph summary visibly names that page-rendered
attempt; reload the page before inspecting a newer attempt shown by live polling.
Decorative connectors are hidden from assistive technology, and state, map, and
failure-origin/path distinctions use text and symbols in addition to color. For
full-reporting publications with verified useful retained collections, the bounded JSON
routes remain visible fallbacks whenever a graph cannot be shown safely.

By default, the same change form presents immutable attempt history contextually. The
inline selects at most the newest 25 rows, ordered newest first, and reports the exact
total, shown, and omitted counts. Its filtered paginated-list link retains access to
every attempt's exact bounded detail without embedding an unbounded formset. The inline
selects only attempt identity, state, timing, and an error guarded at 512 characters
and 2 KiB before transfer; it does not load complete tracebacks, results, result
references, or workflow summaries. Oversized error text is replaced by a fixed link
prompt next to the attempt-detail link rather than selecting or displaying an
arbitrary prefix.

Previous failed attempts inside that newest embedded window also receive individual
collapsed execution-graph panels in the same **Workflow execution** boundary. The
panels precede the current attempt in one chronological stack, reuse the same private
bounded readers, and do not request anything while hidden. Older omitted attempts use
their exact bounded detail and graph links from the paginated list instead of expanding
the parent page. The first open fetches the exact archived attempt once; its rendered
graph or fixed unavailable/error state is then cached until the page is reloaded. Every
endpoint and node-detail link retains the selected attempt number, and the browser
refuses mixed current/archive endpoint metadata before making a request. The current
attempt is rendered once at the end of the stack, so a successful recovery remains
visible without duplicating it in archived history.

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

The admin and operational API never return `runtime_env_json`, whether it contains
plaintext or an encrypted envelope. Encrypted snapshot storage protects that database
column from read-only database and backup exposure when keys are held separately; it
does not make ciphertext, nonces, keys, or raw envelopes appropriate operational
diagnostics. The profile and plaintext SHA-256 identity remain visible and can reveal
that two tasks used the same environment. Plaintext also necessarily exists in the
Django task manager and Ray execution path.

Protect the database, admin, metrics route, Ray dashboard, State API, and storage
backends independently. Never expose live log access by default.

## See Also

- [Operator Runbook](runbook.md)
- [API Reference](reference/api.md)
- [Architecture](architecture.md)
- [Queues](queues.md)
