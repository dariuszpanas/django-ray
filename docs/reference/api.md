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
| `GET /api/tasks/{task_id}` | Get status, timestamps, and redacted arguments by task ID; return values and failure diagnostics are omitted |
| `GET /api/executions` | Page through bounded, redacted task-execution projections |
| `GET /api/executions/stats` | Get statistics |
| `GET /api/executions/{id}` | Get exact redacted execution details; list bounds do not apply |
| `POST /api/executions/{id}/cancel` | Cancel or request cancellation for an execution |
| `POST /api/executions/{id}/retry` | Request retry with bounded `202`, `404`, or `409` outcome |
| `POST /api/executions/reset` | Retry matching `FAILED`, `CANCELLED`, `LOST`, or `EXPIRED` executions |
| `POST /api/cluster/workflow-showcase` | Enqueue the bounded full-reporting order-fulfillment showcase |
| `GET /api/cluster/workflow-showcase/{task_id}` | Poll its bounded summary and compact result or failure |
| `GET /api/cluster/workflows/{task_id}` | Get the bounded compatible workflow summary |
| `GET /api/cluster/workflows/{task_id}/topology/nodes` | Page through immutable topology nodes |
| `GET /api/cluster/workflows/{task_id}/topology/edges` | Page through immutable topology edges |
| `GET /api/cluster/workflows/{task_id}/nodes` | Page through normalized node detail, optionally by state |
| `GET /api/cluster/workflows/{task_id}/node-detail?node_id={node_id}` | Get one indexed durable node record without scanning the graph |
| `GET /api/cluster/workflows/{task_id}/nodes/{node_id}` | Get legacy durable node metadata and live Ray state |
| `GET /api/cluster/workflows/{task_id}/nodes/{node_id}?include_logs=true` | Include bounded Ray stdout/stderr tails |

`/api/executions` exposes only its ordinary redacted result/error fields. The 0.4.0
testproject has no pattern-unredacted diagnostics HTTP endpoint; privileged failure
inspection is deliberately confined to the separately authorized Django Admin view.

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
redaction policy. This database expression is deliberately supported only on the
testproject's SQLite walkthrough and PostgreSQL deployment paths; another database
fails configuration clearly instead of pretending that its LOB byte semantics are
bounded. The complete encoded response is at most 256 KiB, and only complete items
that fit that ceiling are returned. Continue with the unchanged filters and
`next_cursor` rather than increasing `limit`.

`GET /api/executions/{id}` remains the separate exact operator lookup used for focused
diagnosis. It is authenticated and redacted, but it is not covered by the list's
per-field or aggregate bounds. Production adapters should authorize that detail route
more narrowly or replace it with an application-specific bounded projection.

### 0.4.0 workflow graph migration

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

`request_task_retry()` locks and reloads the durable execution row. Its bounded
`TaskRetryRequestResult.status` is one of:

| Status | Meaning |
|---|---|
| `ACCEPTED` | This call queued the replacement attempt and returns its new attempt/generation identity. |
| `NOT_RETRYABLE` | The current state is not one of the caller's allowed retry states. A successful execution should remain completed history; enqueue a new task for new business intent. |
| `NOT_FOUND` | No durable execution has that primary key. |
| `STALE_ATTEMPT` | The caller authorized an older attempt of the same durable execution. |
| `STALE_GENERATION` | The caller authorized an older execution generation. |
| `STALE_WORKFLOW_IDENTITY` | The caller authorized a different workflow run or plan identity. |

The service does not authorize the caller or perform an operator confirmation. Resolve
the object through the application's tenant/ownership policy first, then apply that
application's confirmation, idempotency, and audit requirements. The bundled
testproject endpoint returns `202` with `ACCEPTED`, `404` with `NOT_FOUND`, and `409`
for other bounded outcomes; it never returns the task result or error in this response.
`RuntimeEnvSnapshotError` instead means the locked row has an identified snapshot that
cannot be verified. Do not retry it or include the stored payload in an API response.
The testproject maps it to one fixed redaction-safe `409`, while bulk Admin retry skips
the corrupt row and continues.

`retry_task()` retains the earlier model-or-`None` compatibility contract for callers
that do not need to distinguish rejection reasons.

`request_task_cancellation()` locks and reloads the durable execution row. Its bounded
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

The service does not authorize the caller. Resolve the object through the application's
tenant/ownership policy first, then pass its primary key, observed attempt number, and
observed execution generation. Queued work is cancelled and archived immediately.
Running work moves to `CANCELLING` unless its Ray Job entrypoint already published
`completion_data`; that case returns `COMPLETION_PENDING` and leaves reconciliation
to consume the terminal envelope. Otherwise a worker requests backend interruption
and finalizes the durable state. That interruption is best effort: cancellation
cannot guarantee that already-running synchronous Python code stops immediately.

Do not expose `RayTaskExecution.delete()` as a cancellation or cleanup shortcut.
Deleting an active row can leave Ray work running without its durable lifecycle owner,
and deleting a terminal row does not by itself reclaim externally stored results or
workflow detail. The bundled testproject therefore has no execution-deletion route.
Use the fenced cancellation service for active work and design retention around every
owned durable and external artifact before adding application-specific cleanup.

For a complete REST API example, see `testproject/api.py` in the repository.

The reusable helpers in `django_ray.observability` expose schema-versioned task, queue,
attempt, and workflow snapshots, then optionally query Ray's live State and Log APIs.
The bounded functions in `django_ray.workflow_progress_reads` expose summary,
topology-node, topology-edge, node-detail, and indexed-node reads. Every call requires
an object authorizer; applications must replace the testproject's callable allowlist
with their tenant or ownership policy. `django_ray.metrics.render_prometheus_metrics()`
supplies the package-owned text format used by the sample endpoint. Treat node logs and
operational metadata as sensitive.

The indexed example accepts `node_id` as a query parameter so URL encoding round-trips
the full bounded UTF-8 identifier, including values such as `namespace/apply`. The
older `/nodes/{node_id}` route retains its live-Ray and optional-log behavior for
testproject compatibility; it is not the normalized indexed read facade.

See [Observability Services](../observability.md) for the supported Python schemas,
metrics, degradation behavior, and security boundary.

## See Also

- [Getting Started](../getting-started.md) - Basic setup
- [Tasks](../tasks.md) - Defining tasks

