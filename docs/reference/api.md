# API Reference

django-ray is a library that provides a Django Tasks backend - it does **not** include a REST API. The API endpoints described below are part of the **testproject** included in this repository for demonstration purposes.

## What django-ray Provides

django-ray provides:

- `RayTaskBackend` - Django Tasks backend
- `RayTaskExecution` model - Task execution tracking
- `TaskWorkerLease` model - Worker coordination
- `django_ray_worker` management command - Task processing
- Django Admin integration - Task monitoring

## testproject API (Example Only)

The testproject in this repository includes a REST API built with [Django Ninja](https://django-ninja.dev/) to demonstrate django-ray functionality. **This API is not part of the django-ray package.**

If you need a REST API for task management in your project, you can use the testproject as a reference implementation.

---

## Example Endpoints (testproject)

> ⚠️ **Note**: These endpoints are from the testproject, not the django-ray library.

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/metrics` | Prometheus metrics |
| `GET /api/executions` | List task executions |
| `GET /api/executions/{id}` | Get execution details |
| `POST /api/executions/{id}/cancel` | Cancel execution |
| `POST /api/executions/{id}/retry` | Retry failed execution |
| `DELETE /api/executions/{id}` | Delete execution |
| `GET /api/executions/stats` | Get statistics |

When the testproject server is running:
- **Swagger UI**: http://localhost:8000/api/docs
- **OpenAPI Schema**: http://localhost:8000/api/openapi.json

---

## Building Your Own API

To add task management endpoints to your project, query the django-ray models directly:

```python
from django_ray.models import RayTaskExecution, TaskState

# List executions
executions = RayTaskExecution.objects.filter(state=TaskState.QUEUED)

# Get stats
from django.db.models import Count
stats = RayTaskExecution.objects.values('state').annotate(count=Count('id'))

# Cancel a task
execution = RayTaskExecution.objects.get(pk=execution_id)
if execution.state in (TaskState.QUEUED, TaskState.RUNNING):
    execution.state = TaskState.CANCELLED
    execution.save()
```

For a complete REST API example, see `testproject/api.py` in the repository.

## See Also

- [Getting Started](../getting-started.md) - Basic setup
- [Tasks](../tasks.md) - Defining tasks

