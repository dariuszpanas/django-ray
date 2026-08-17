# API Reference

django-ray is a library that provides a Django Tasks backend and supported, versioned
Python observability services. It does **not** prescribe a REST framework or mount a
general REST API. The HTTP endpoints below are part of the **testproject** and adapt the
package services with Django Ninja and bearer authentication.

## What django-ray Provides

django-ray provides:

- `RayTaskBackend` - Django Tasks backend
- `RayTaskExecution` model - Task execution tracking
- `TaskWorkerLease` model - Worker coordination
- `django_ray_worker` management command - Task processing
- Django Admin integration - Task monitoring
- Versioned task, queue, attempt, workflow, and live-Ray observability services
- Reusable bounded-cardinality Prometheus rendering

## testproject API (Example Only)

The testproject in this repository includes a REST API built with [Django Ninja](https://django-ninja.dev/) to demonstrate django-ray functionality. **This API is not part of the django-ray package.**

If you need a REST API for task management in your project, you can use the testproject as a reference implementation.

---

## Example Endpoints (testproject)

> ⚠️ **Note**: These endpoints are from the testproject, not the django-ray library.

| Endpoint | Description |
|----------|-------------|
| `GET /api/livez` | Lightweight process liveness check |
| `GET /api/readyz` | Readiness check with database reachability |
| `GET /api/health` | Health check |
| `GET /api/metrics` | Prometheus metrics |
| `GET /api/tasks/{task_id}` | Get one bounded task-status projection; return values and failure diagnostics are omitted |
| `GET /api/executions` | Page through bounded, redacted task-execution projections |
| `GET /api/executions/stats` | Get statistics |
| `GET /api/executions/{id}` | Get one bounded, redacted execution-detail projection |
| `POST /api/executions/{id}/cancel` | Request cancellation with a bounded `202`, `404`, or `409` outcome |
| `POST /api/executions/{id}/retry` | Request retry with bounded `202`, `404`, or `409` outcome |
| `GET /api/cluster/workflow-benchmark/{task_id}` | Poll bounded workflow state, aggregate summary, and current diagnostics |
| `GET /api/cluster/complex-workflow/{task_id}` | Poll bounded workflow state, aggregate summary, and current diagnostics |
| `POST /api/cluster/workflow-showcase` | Enqueue the bounded full-reporting order-fulfillment showcase |
| `GET /api/cluster/workflow-showcase/{task_id}` | Poll its bounded aggregate summary and current diagnostics |
| `GET /api/cluster/workflow-recovery-showcase/{task_id}` | Poll its bounded current diagnostics and archived attempt errors |
| `GET /api/cluster/runtime-env/{task_id}` | Poll bounded RuntimeEnv identity and current diagnostics |
| `GET /api/cluster/workflows/{task_id}` | Get the bounded compatible workflow summary |
| `GET /api/cluster/workflows/{task_id}/topology/nodes` | Page through immutable topology nodes |
| `GET /api/cluster/workflows/{task_id}/topology/edges` | Page through immutable topology edges |
| `GET /api/cluster/workflows/{task_id}/nodes` | Page through normalized node detail, optionally by state |
| `GET /api/cluster/workflows/{task_id}/node-detail?node_id={node_id}` | Get one indexed durable node record without scanning the graph |

`/api/executions` exposes only its ordinary redacted result/error fields. The 0.4.0
testproject has no pattern-unredacted diagnostics HTTP endpoint; privileged failure
inspection is deliberately confined to the separately authorized Django Admin view.

The task-status response and every execution list/detail item include
`execution_protocol_version`, `created_with_django_ray_version`,
`managed_with_django_ray_version`, `executor_django_ray_version`,
`protocol_compatible_worker_available`, and `queue_capacity_attested=false`. Package
versions are nullable diagnostic provenance, never a compatibility switch. Each is
limited to 128 UTF-8 bytes in the database projection, so an oversized SQLite value
becomes `null` before transfer; included values pass through configured presentation
redaction. Availability uses one frozen heartbeat cutoff for
the query and accepts only valid policy-controlled legacy protocol-`1` capacity or a
valid explicit lease range containing the row protocol. It ignores informational lease
queue text; true does not prove queue capacity, free concurrency, Ray/Python
compatibility, Ray readiness, or cluster identity. Global bearer authentication still
runs before any of these execution queries.

False is fail-closed: it can mean no matching heartbeat-live lease, an invalid
policy/token relationship, or any malformed lease advertisement. It must not be
interpreted as proof that the queue is empty or that adding an arbitrary worker is safe.

### Bounded task-status example

`GET /api/tasks/{task_id}` is a monitoring projection, not a serialized Django
`TaskResult`. One unlocked database snapshot selects only its public fields. It keeps
the Django-style `status` and adds the exact durable `state`, `attempt_number`, and
`execution_generation` plus the common protocol visibility fields above. The combined
inline `args` and `kwargs` source is limited to
16,384 bytes before transfer. Both fields are nullable, and an external input is never
loaded by this route. The path accepts the backend's task identifier up to 255
characters; it does not impose a UUID-only format.

`input_omission_reason` is either `null` or one of the fixed values
`external_input_not_loaded`, `stored_input_exceeds_status_limit`,
`malformed_inline_input`, and `encoded_response_limit`. An included value still passes
through configured presentation redaction; `[REDACTED]` therefore remains an available
bounded value rather than an authorization decision. The complete response is at most
65,536 bytes and advertises both `input_max_bytes=16384` and
`response_max_bytes=65536`. Responses disable caching and MIME sniffing, and a missing
task uses one fixed `404` body. The route does not import the callable, retrieve durable
external input, or access result storage. Its database byte expression is supported on
the testproject's SQLite and PostgreSQL paths; another database fails configuration
clearly instead of inheriting the bound.

The package-level Python `TaskResult` contract is intentionally different. Application
code can use it to retrieve the task's full arguments, keyword arguments, and successful
return value under that application's own trust boundary. The testproject HTTP status
adapter is deliberately narrower and must not be used as evidence that package results
are truncated to these limits.

### Bounded execution-list example

`GET /api/executions` accepts a `limit` from 1 through 100 and defaults to 50. It
orders by newest creation time and primary key, keeps the existing filters and global
state counts, and returns `returned_count`, `has_more`, and a signed, filter-bound
`next_cursor` for continuation. The cursor is bound to the exact state, queue, and
task-id filters, so it cannot be reused under a different query. Keyset continuation
remains anchored after the last complete returned item when newer executions are
inserted. A malformed, oversized, modified, or filter-mismatched cursor is rejected
with `422`.
`truncation_reason` is the fixed `page_limit` value when another requested-size page
exists or `response_size_limit` when the encoded response ceiling is reached.

The list query measures `result_data` and `error_message` in the database. A stored
value over 4,096 bytes is not transferred to the application; its field is `null` and
the corresponding `*_omission_reason` is the fixed
`stored_value_exceeds_list_limit` value. Included values still pass through the normal
redaction policy. Malformed, deep, or conversion-failing included result JSON becomes
the fixed `[REDACTED]` marker; because this is redaction rather than omission, its
omission reason remains `null`. This database expression is deliberately supported
only on the testproject's SQLite walkthrough and PostgreSQL deployment paths; another
database fails configuration clearly instead of pretending that its LOB byte semantics
are bounded. The complete encoded response is at most 256 KiB, and only complete items
that fit that ceiling are returned. Continue with the unchanged filters and
`next_cursor` rather than increasing `limit`.

Protocol compatibility is an `EXISTS` annotation in that same bounded page query; it
does not issue one worker-lease query per returned execution.

### Bounded execution-detail example

`GET /api/executions/{id}` is the separate exact operator lookup used for focused
diagnosis. One database statement selects only the public response fields. Stored
arguments and input references, traceback and cancellation details, RuntimeEnv data,
workflow plans and progress, and completion envelopes are not selected. The query
measures inline `result_data` and `error_message` before transfer and returns either
value only when it is at most 65,536 bytes. A larger stored value is `null` with the
fixed `stored_value_exceeds_detail_limit` reason.

The same statement carries the common protocol visibility annotation and SQL-guarded
provenance; it does not hydrate a worker lease or issue a follow-up capacity query.

An external result reference is never returned or resolved by this route. When a row
has only an external result, `result_data` is `null` and
`result_data_omission_reason` is `external_result_not_loaded`; applications should use
their separately authorized result-retrieval policy if they choose to expose that
data. Included inline values still pass through the normal redaction policy. If that
result is malformed or truncated JSON, too deeply nested to decode, or cannot complete
its Unicode-safe conversion, the field is the fixed `[REDACTED]` marker. That is
redaction rather than omission, so its omission reason remains `null`. If processing a
valid value would make the encoded response exceed 256 KiB, the example first omits the
result so a failure diagnostic can remain, then omits the error if necessary. Each such
field uses `response_size_limit`. If fixed metadata still cannot fit, or the configured
renderer raises an ordinary exception while encoding either response, the route returns
a small `503` response with the code `execution_detail_response_limit`. Process-control
exceptions are not swallowed.

Successful detail responses and the fixed limit failure use `Cache-Control: no-store`
and `X-Content-Type-Options: nosniff`. As with the list, the database byte expression
is supported on the testproject's SQLite and PostgreSQL paths; another database fails
configuration clearly. The example keeps global bearer authentication for
walkthroughs. Production adapters should add their tenant, ownership, or object policy
before exposing an exact lookup.

### 0.4.0 testproject endpoint migration

The pre-1.0 complete-graph route
`GET /api/cluster/workflows/{task_id}/graph` was removed without an alias or
deprecation window. Replace each use with the bounded operation that matches the data
needed:

| Former complete-graph use | Bounded replacement |
|---|---|
| Aggregate workflow state and counts | `GET /api/cluster/workflows/{task_id}` |
| Immutable topology nodes | `GET /api/cluster/workflows/{task_id}/topology/nodes` |
| Immutable dependency edges | `GET /api/cluster/workflows/{task_id}/topology/edges` |
| Current normalized node states | `GET /api/cluster/workflows/{task_id}/nodes` |
| One durable node by its full identifier | `GET /api/cluster/workflows/{task_id}/node-detail?node_id={node_id}` |

Follow each page's bounded cursor instead of reconstructing one unbounded graph
response. Existing schema-v1/v2 database snapshots are not rewritten or deleted; the
summary route can still expose their sanitized aggregate counts. It never returns the
stored complete graph. The private Django Admin visualization remains available to
authorized staff and builds its display from bounded readers.

The pre-0.4.0 live-node adapter
`GET /api/cluster/workflows/{task_id}/nodes/{node_id}` was also removed without an
alias. Use the indexed durable
`GET /api/cluster/workflows/{task_id}/node-detail?node_id={node_id}` read after the
same object authorization as the other workflow routes. Applications that truly need
live Ray state or logs can call the package `get_workflow_node_snapshot()` helper from
an application-owned, separately authorized surface; the testproject no longer exposes
that data over HTTP.

The arbitrary bulk-reset example `POST /api/executions/reset` was removed as well. Use
the exact `POST /api/executions/{id}/retry` route when an application has authorized one
observed execution, attempt, and generation. Operators can continue to use the bounded,
signed Django Admin confirmation for eligible single or multi-row retry. Neither
replacement treats workflow progress as a checkpoint or bypasses the warning that a
retry can repeat external effects.

### Bounded workflow and RuntimeEnv polling examples

The workflow benchmark, complex-workflow, workflow-showcase, recovery-showcase, and
RuntimeEnv `GET` pollers listed above use exact database projections. They do not import
task callables or transfer task input, RuntimeEnv snapshots, workflow plans, completion
envelopes, or unrelated payload columns. Current inline `result` and `error` values are
guarded at 16,384 bytes each before transfer, external result storage is never resolved,
and the complete encoded response is at most 65,536 bytes. These byte projections have
the same explicit SQLite/PostgreSQL support boundary as the task-status and execution
examples.

Current `result_omission_reason` is either `null` or
`external_result_not_loaded`, `stored_result_exceeds_poll_limit`,
`malformed_inline_result`, or `encoded_response_limit`. Current
`error_omission_reason` is either `null`, `stored_error_exceeds_poll_limit`, or
`encoded_response_limit`. The response advertises `diagnostic_max_bytes=16384` and
`response_max_bytes=65536`. Workflow `progress` is exposed only through a bounded
aggregate summary envelope. A published schema-v3 summary is preferred; supported older
stored progress may contribute sanitized aggregate counts, but the pollers never return
its complete legacy graph.

The recovery poller additionally selects at most its four expected ordered attempt
rows. Each archived attempt error is guarded at 4,096 bytes and uses either
`stored_error_exceeds_attempt_limit` or `encoded_response_limit` when omitted. Its
response advertises `attempt_error_max_bytes=4096` and remains under the same 65,536-byte
ceiling.

When the testproject server is running:
- **Swagger UI**: http://localhost:8000/api/docs
- **OpenAPI Schema**: http://localhost:8000/api/openapi.json

---

## Building Your Own API

Query django-ray models for authorized reads, but route cancellation and retry writes
through the package lifecycle services:

```python
from django.db.models import Count

from django_ray.lifecycle import request_task_cancellation, request_task_retry
from django_ray.models import RayTaskExecution, TaskState

# List executions
executions = RayTaskExecution.objects.filter(state=TaskState.QUEUED)

# Get stats
stats = RayTaskExecution.objects.values("state").annotate(count=Count("id"))


def cancel_authorized_execution(execution: RayTaskExecution):
    # Perform object/tenant authorization before this call.
    return request_task_cancellation(
        execution.pk,
        expected_attempt_number=execution.attempt_number,
        expected_execution_generation=execution.execution_generation,
    )


def retry_authorized_execution(execution: RayTaskExecution):
    # The generation fence prevents a stale request from retrying a newer attempt.
    # RuntimeEnvSnapshotError remains distinct for the HTTP boundary to map.
    return request_task_retry(
        execution.pk,
        expected_attempt_number=execution.attempt_number,
        expected_execution_generation=execution.execution_generation,
        expected_workflow_identity=(
            str(execution.workflow_run_id) if execution.workflow_run_id else None,
            execution.workflow_plan_fingerprint,
        ),
    )
```

`request_task_retry()` uses one `select_for_update()` query for lifecycle and workflow
identity fields. Only after every state and identity fence passes does it read the exact
RuntimeEnv, routing, deadline, and attempt-archive fields needed for the accepted
transition while the row remains locked. Rejected paths do not transfer them. It does
not reload deferred fields implicitly or transfer unrelated task inputs, progress,
workflow plan body/selection, completion, or cancellation payload columns. Its bounded
`TaskRetryRequestResult.status` is one of:

| Status | Meaning |
|---|---|
| `ACCEPTED` | This call queued the replacement attempt and returns its new attempt/generation identity. |
| `NOT_RETRYABLE` | The current state is not one of the caller's allowed retry states. A successful execution should remain completed history; enqueue a new task for new business intent. |
| `NOT_FOUND` | No durable execution has that primary key. |
| `STALE_ATTEMPT` | The caller authorized an older attempt of the same durable execution. |
| `STALE_GENERATION` | The caller authorized an older execution generation. |
| `STALE_WORKFLOW_IDENTITY` | The caller authorized a different workflow run or plan identity. |
| `UNSUPPORTED_PROTOCOL` | This package build cannot mutate the execution's durable protocol. Route it to a compatible cohort. |

The service does not authorize the caller or perform an operator confirmation. Resolve
the object through the application's tenant/ownership policy first, then apply that
application's confirmation, idempotency, and audit requirements. The bundled
testproject endpoint returns `202` with `ACCEPTED`, `404` with `NOT_FOUND`, and `409`
for other bounded outcomes; it never returns the task result or error in this response.
The public service always uses the installed package's supported protocol range; an
application caller cannot widen that range.
Adapters must treat any future status they do not recognize as a bounded conflict and
leave the execution unchanged; only the exact `ACCEPTED` value authorizes success.
`RuntimeEnvSnapshotError` instead means the locked row has an identified snapshot that
cannot be verified. Do not retry it or include the stored payload in an API response.
The testproject maps it to one fixed redaction-safe `409`, while bulk Admin retry skips
the corrupt row and continues.

`retry_task()` retains the earlier model-or-`None` compatibility contract for callers
that do not need to distinguish rejection reasons. Unrelated fields on the returned
model remain deferred and may load after the transaction if a caller accesses them.

`request_task_cancellation()` locks only lifecycle identity. An accepted queued
cancellation then reads the exact attempt-archive fields it needs while retaining that
lock. Running work uses a database-side completion-presence check without transferring
the completion envelope, and rejected paths do not transfer archive fields. No path
reloads a deferred execution field implicitly inside the transaction. Its bounded
`TaskCancellationRequestResult.status` is one of:

| Status | Meaning |
|---|---|
| `ACCEPTED` | This call won. `state` is `CANCELLED` for queued work or `CANCELLING` for running work. |
| `ALREADY_REQUESTED` | The current execution is already `CANCELLING`. |
| `ALREADY_TERMINAL` | The current execution already finished, failed, was lost, or was cancelled. |
| `COMPLETION_PENDING` | The Ray Job entrypoint already published a durable terminal envelope; reconciliation owns the still-`RUNNING` row. |
| `NOT_FOUND` | No durable execution has that primary key. |
| `STALE_ATTEMPT` | The caller authorized an older attempt of the same durable execution. |
| `STALE_GENERATION` | The caller authorized an older execution generation. |
| `INVALID_STATE` | The persisted state is outside django-ray's lifecycle vocabulary. |
| `UNSUPPORTED_PROTOCOL` | This package build cannot mutate the execution's durable protocol. Route it to a compatible cohort. |

The service does not authorize the caller. Resolve the object through the application's
tenant/ownership policy first, then pass its primary key, observed attempt number, and
observed execution generation. Queued work is cancelled and archived immediately.
The public service always uses the installed package's supported protocol range; an
application caller cannot widen that range.
Adapters must treat any future status they do not recognize as a bounded conflict and
leave the execution unchanged; only the exact `ACCEPTED` value authorizes success.
Running work moves to `CANCELLING` unless its Ray Job entrypoint already published
`completion_data`; that case returns `COMPLETION_PENDING` and leaves reconciliation
to consume the terminal envelope. Otherwise a worker requests backend interruption
and finalizes the durable state. That interruption is best effort: cancellation
cannot guarantee that already-running synchronous Python code stops immediately.

The bundled testproject cancellation endpoint returns `202` only for `ACCEPTED`, `404`
for `NOT_FOUND`, and `409` for every other service outcome. Its response contains only
`code`, `message`, `execution_id`, `state`, `attempt_number`,
`execution_generation`, `next_action`, and `response_max_bytes=4096`; the complete body
is at most 4,096 bytes. It does not broadly refresh or serialize the execution model,
and no task input, result, error, traceback, workflow, RuntimeEnv, or cancellation
diagnostic is returned.

Do not expose `RayTaskExecution.delete()` as a cancellation or cleanup shortcut.
Deleting an active row can leave Ray work running without its durable lifecycle owner,
and deleting a terminal row does not by itself reclaim externally stored results or
workflow detail. The bundled testproject therefore has no execution-deletion route.
Use the fenced cancellation service for active work and design retention around every
owned durable and external artifact before adding application-specific cleanup.

For a complete REST API example, see `testproject/api.py` in the repository.

The reusable helpers in `django_ray.observability` expose schema-versioned task, queue,
attempt, and workflow snapshots, then optionally query Ray's live State and Log APIs.
The bounded functions in `django_ray.workflow.progress.reads` expose summary,
topology-node, topology-edge, node-detail, and indexed-node reads. Every call requires
an object authorizer; applications must replace the testproject's callable allowlist
with their tenant or ownership policy. `django_ray.metrics.render_prometheus_metrics()`
supplies the package-owned text format used by the sample endpoint. Treat node logs and
operational metadata as sensitive.

The indexed example accepts `node_id` as a query parameter so URL encoding round-trips
the full bounded UTF-8 identifier, including values such as `namespace/apply`. The
testproject exposes only this durable indexed facade; package live-node helpers remain
available for application-owned, separately authorized integrations.

See [Observability Services](../observability.md) for the supported Python schemas,
metrics, degradation behavior, and security boundary.

## See Also

- [Getting Started](../getting-started.md) - Basic setup
- [Tasks](../tasks.md) - Defining tasks

