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
| `POST /api/executions/reset` | Reset matching executions to queued |
| `DELETE /api/executions/{id}` | Delete execution |
| `GET /api/cluster/workflows/{task_id}/graph` | Get the versioned workflow node/edge graph |
| `GET /api/cluster/workflows/{task_id}/nodes/{node_id}` | Get durable node metadata and live Ray state |
| `GET /api/cluster/workflows/{task_id}/nodes/{node_id}?include_logs=true` | Include bounded Ray stdout/stderr tails |

When the testproject server is running:
- **Swagger UI**: http://localhost:8000/api/docs
- **OpenAPI Schema**: http://localhost:8000/api/openapi.json

---

## Building Your Own API

To add task management endpoints to your project, query the django-ray models directly:

```python
from django.db.models import Count

from django_ray.models import RayTaskExecution, TaskState

# List executions
executions = RayTaskExecution.objects.filter(state=TaskState.QUEUED)

# Get stats
stats = RayTaskExecution.objects.values("state").annotate(count=Count("id"))


def request_cancellation(execution_id: int) -> None:
    execution = RayTaskExecution.objects.get(pk=execution_id)
    if execution.state == TaskState.QUEUED:
        execution.state = TaskState.CANCELLED
    elif execution.state == TaskState.RUNNING:
        execution.state = TaskState.CANCELLING
    else:
        return
    execution.save(update_fields=["state"])
```

For a complete REST API example, see `testproject/api.py` in the repository.

The reusable library helpers in `django_ray.observability` expose schema-versioned task,
queue, attempt, and workflow snapshots, then optionally query Ray's live State and Log
APIs. `django_ray.metrics.render_prometheus_metrics()` supplies the package-owned text
format used by the sample endpoint. Treat node logs and operational metadata as
sensitive and protect every adapter with authorization appropriate to the deployment.

See [Observability Services](../observability.md) for the supported Python schemas,
metrics, degradation behavior, and security boundary.

## See Also

- [Getting Started](../getting-started.md) - Basic setup
- [Tasks](../tasks.md) - Defining tasks

