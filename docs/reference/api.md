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
| `GET /api/tasks/{task_id}` | Get Django task result/status by task id |
| `GET /api/executions` | List task executions |
| `GET /api/executions/stats` | Get statistics |
| `GET /api/executions/{id}` | Get execution details |
| `POST /api/executions/{id}/cancel` | Cancel or request cancellation for an execution |
| `POST /api/executions/{id}/retry` | Retry failed, cancelled, or lost execution |
| `POST /api/executions/reset` | Retry matching `FAILED`, `CANCELLED`, or `LOST` executions |
| `DELETE /api/executions/{id}` | Delete execution |
| `GET /api/cluster/workflows/{task_id}` | Get the bounded compatible workflow summary |
| `GET /api/cluster/workflows/{task_id}/topology/nodes` | Page through immutable topology nodes |
| `GET /api/cluster/workflows/{task_id}/topology/edges` | Page through immutable topology edges |
| `GET /api/cluster/workflows/{task_id}/nodes` | Page through normalized node detail, optionally by state |
| `GET /api/cluster/workflows/{task_id}/node-detail?node_id={node_id}` | Get one indexed durable node record without scanning the graph |
| `GET /api/cluster/workflows/{task_id}/nodes/{node_id}` | Get legacy durable node metadata and live Ray state |
| `GET /api/cluster/workflows/{task_id}/nodes/{node_id}?include_logs=true` | Include bounded Ray stdout/stderr tails |
| `GET /api/cluster/workflows/{task_id}/graph` | Deprecated schema-v1/v2 complete-graph example |

When the testproject server is running:
- **Swagger UI**: http://localhost:8000/api/docs
- **OpenAPI Schema**: http://localhost:8000/api/openapi.json

---

## Building Your Own API

Query django-ray models for authorized reads, but route cancellation and retry writes
through the package lifecycle services:

```python
from django.db.models import Count

from django_ray.lifecycle import request_task_cancellation, retry_task
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


def retry_authorized_execution(execution: RayTaskExecution) -> bool:
    # The generation fence prevents a stale request from retrying a newer attempt.
    return (
        retry_task(
            execution.pk,
            expected_attempt_number=execution.attempt_number,
            expected_execution_generation=execution.execution_generation,
        )
        is not None
    )
```

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

