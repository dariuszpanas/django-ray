# Getting Started

This guide creates a complete task that can be copied into a Django project. Every
name used below is either imported in the snippet or defined in the indicated file.

## Requirements

- Python 3.12, 3.13, or 3.14
- Django 6.0.8+
- Ray 2.58.0+
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

### Projects that prohibit prerelease dependencies

If your project sets uv's `prerelease = "disallow"`, resolution can fail at
`opentelemetry-exporter-prometheus`. django-ray depends on `ray[default]`, which
includes OpenTelemetry dependencies with prerelease version numbers. The
project's stable-only policy rejects those dependencies even when the django-ray
and Ray version identifiers have no prerelease suffix. django-ray remains Beta.
Ordinary installs using the default resolver policy do not need this exception.

Keep the project's stable-only policy and explicitly permit only the required
packages. Pin a compatible OpenTelemetry exporter and semantic-conventions pair
as direct dependencies, then use uv's
[per-package prerelease setting](https://docs.astral.sh/uv/reference/settings/#prerelease-package).
This standalone example was verified on 2026-10-11 with uv 0.12.17 and CPython
3.14.2 against the published django-ray 0.6.0 package:

```toml
[project]
name = "django-ray-install-reproduction"
version = "0.0.0"
requires-python = ">=3.12,<3.15"
dependencies = [
    "django-ray==0.6.0",
    "opentelemetry-exporter-prometheus==0.64b0",
    "opentelemetry-semantic-conventions==0.64b0",
]

[tool.uv]
prerelease = "disallow"
prerelease-package = { opentelemetry-exporter-prometheus = "explicit", opentelemetry-semantic-conventions = "explicit" }
```

Merge the relevant entries into your existing `pyproject.toml`; retain its
Python support range, existing dependencies, constraints and security floors.
The exact versions above are a dated installation example, not permanent
OpenTelemetry compatibility pins or a recommendation to select an older
django-ray release. For another release or Ray version, review the resolver's
dependency chain and the matching OpenTelemetry package requirements before
updating those pins. Do not replace the project's policy with a global
`--prerelease allow` just to resolve these two packages. This configuration was
tested with uv 0.12.17; older uv versions may not support `prerelease-package`.

In a fresh directory containing the example, verify resolution and installation:

```bash
uv lock --python 3.14 --default-index https://pypi.org/simple
uv sync --frozen --python 3.14 --default-index https://pypi.org/simple
uv pip check
```

The verified example resolved 64 packages and installed 63, including Ray
2.59.0, OpenTelemetry API/SDK 1.43.0 and the two 0.64b0 packages. The installed
metadata check passed in a fresh environment without starting Ray. Removing
the package exceptions while retaining `prerelease = "disallow"` reproduced
the resolution failure; the ordinary default policy resolved successfully.
These checks establish installation and dependency consistency, not Linux
runtime or cluster qualification. Follow [Compatibility](compatibility.md)
when choosing and validating the execution environment.

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
`ray://ray-head.example:10001` only for bounded, low-latency work whose lifetime
includes the task manager's client connection. See the
[worker execution modes](worker-modes.md#cluster-ray-core) before choosing it for
production work.

This walkthrough uses local Ray for development. For ordinary durable background
jobs on Kubernetes, begin with the [Ray Job execution mode](worker-modes.md#ray-job)
and its required retrievable input storage. Configure how the Ray environment gets
your application code and dependencies before submitting work. A remote address
alone does not deliver the application or expose its dashboard to your browser.

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

Linux is the supported execution target. On Windows or macOS, use an explicitly bounded Linux
environment such as WSL2 or the [Docker walkthrough](deployment/docker.md). Native Windows
compatibility is best effort through hosted GitHub Actions checks; do not run broad suites or native
Ray validation on a non-Linux workstation. See [Compatibility](compatibility.md#platforms).

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

## Before Production

> **Before production:** make these decisions explicitly.
>
> - Execution is not exactly once. Queued work can expire or be cancelled before
>   application code runs, while work that starts may repeat after lost completion
>   evidence. Make externally visible side effects idempotent. See
>   [Retry and Error Handling](retry.md#make-side-effects-idempotent).
> - A Ray-native workflow retry starts again at its entry node; successful internal
>   leaves are not durable checkpoints. Keep leaves pure or idempotent, and use separate
>   durable tasks for side-effecting stages that need independent recovery. See
>   [Workflow retries](retry.md#workflow-retries).
> - When work depends on a database transaction, enqueue it with
>   `transaction.on_commit()`. See
>   [Enqueue after a database commit](tasks.md#enqueue-after-a-database-commit).
> - `run_after` is the earliest eligibility time for one submission, not an exact start
>   time or a periodic scheduler.
> - Use PostgreSQL for production. SQLite is only supported for local walkthroughs and
>   tests.
> - Every queue selected by a producer must have a matching running task manager. See
>   [Working with Queues](queues.md#run-queue-specific-workers).
> - Queued work expires after 24 hours by default, measured from its eligibility time.
>   Choose each backend's queue-wait budget deliberately; use an unlimited queue only
>   with idempotent tasks, backlog alerts, and a drain or discard policy. See
>   [Queue expiration](tasks.md#queue-expiration).
> - Cluster Ray Core uses Ray Client. Losing its task-manager connection beyond Ray's
>   reconnect grace period terminates in-flight Ray work; an outer django-ray retry is
>   a replay, not a resume. Prefer Ray Job mode for long or coarse execution that must
>   continue independently of that connection. See
>   [Cluster Ray Core](worker-modes.md#cluster-ray-core).
>
> Review [Defining Tasks](tasks.md), [Retry and Error Handling](retry.md), and the
> workflow [durability semantics](workflows.md#durability-semantics) before moving a
> side-effecting workload into production.

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
- [Migrating from Celery](celery-migration.md) for workload classification,
  coexistence, and safe drain
- [Performance](performance.md) for task granularity and mode selection
- [Ray-Native Workflows](workflows.md) for chain, group, and fan-out
- [Runtime Environments](runtime-environments.md) for per-task dependencies
- [Kubernetes Deployment](deployment/kubernetes.md) for local evaluation and a production
  architecture checklist
