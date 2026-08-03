# Getting Started

This guide creates a complete task that can be copied into a Django project. Every
name used below is either imported in the snippet or defined in the indicated file.

## Requirements

- Python 3.12, 3.13, or 3.14
- Django 6.0+
- Ray 2.56.0+
- PostgreSQL for production; SQLite is sufficient for a local walkthrough

Python 3.12 is the minimum because Django 6.0 requires it. See
[Compatibility](compatibility.md) for the tested version policy.

## Install

```bash
python -m pip install django-ray
```

With PostgreSQL:

```bash
python -m pip install "django-ray[postgres]"
```

The equivalent uv command is `uv add django-ray`.

## Configure Django

Add the application and task backend in `settings.py`:

```python
# settings.py
INSTALLED_APPS = [
    # Your Django applications...
    "django_ray",
]

TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
    },
}

DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 10,
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 60,
}
```

`TASKS` tells Django where to enqueue work. `DJANGO_RAY` configures the worker that
claims it. For a remote cluster, use a Ray Client address such as
`ray://ray-head.example:10001`.

Apply the database migrations:

```bash
python manage.py migrate
```

## Define a Complete Task

Create `myapp/tasks.py`:

```python
from django.tasks import task


@task(queue_name="default")
def add_numbers(left: int, right: int) -> int:
    return left + right
```

This deliberately small task verifies the queue and Ray connection without requiring
models, email configuration, or application-specific helper functions.

## Start a Worker

In a separate terminal:

```bash
python manage.py django_ray_worker --queue=default --local
```

`--local` starts Ray on the same machine and uses the low-overhead Ray Core runner.
For a logic-only check without Ray, use `--sync`. See
[Choosing an execution model](performance.md#choose-an-execution-model) before
selecting a production mode.

## Enqueue and Read the Result

Open `python manage.py shell`:

```python
from django.tasks import TaskResultStatus, task_backends

from myapp.tasks import add_numbers

enqueued = add_numbers.enqueue(20, 22)
print(enqueued.id)
print(enqueued.status)  # READY: this object is the enqueue-time snapshot

# Run this again after the worker finishes the task.
current = task_backends["default"].get_result(enqueued.id)
print(current.status)
if current.status == TaskResultStatus.SUCCESSFUL:
    print(current.return_value)  # 42
```

`TaskResult` does not poll in the background. Call the backend's `get_result()` again
when a UI, API, or management command needs the current state.

## A Real Django Task

Tasks may use the ORM normally. This example uses only Django APIs and assumes the
built-in user model has an email address:

```python
# myapp/tasks.py
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.tasks import task


@task(queue_name="default")
def send_welcome_email(user_id: int) -> dict[str, str]:
    user = get_user_model().objects.get(pk=user_id)
    send_mail(
        subject="Welcome",
        message=f"Hello {user.get_username()}!",
        from_email=None,  # Uses DEFAULT_FROM_EMAIL.
        recipient_list=[user.email],
    )
    return {"sent_to": user.email}
```

Pass model primary keys rather than model instances. Task arguments and results cross
process boundaries and must be JSON-serializable.

## Verify and Monitor

The Django admin page `/admin/django_ray/raytaskexecution/` shows queue state,
attempts, errors, RuntimeEnv identity, and workflow progress. Programmatic operational
queries use the durable model:

```python
from django_ray.models import RayTaskExecution, TaskState

running = RayTaskExecution.objects.filter(state=TaskState.RUNNING)
failed = RayTaskExecution.objects.filter(state=TaskState.FAILED)
```

## Next Steps

- [Tasks](tasks.md) for arguments, results, and error behavior
- [Performance](performance.md) for task granularity and mode selection
- [Ray-Native Workflows](workflows.md) for chain, group, and fan-out
- [Runtime Environments](runtime-environments.md) for per-task dependencies
- [Kubernetes Deployment](deployment/kubernetes.md) for local evaluation and a production
  architecture checklist
