<p align="center">
  <img src="https://raw.githubusercontent.com/dariuszpanas/django-ray/main/docs/assets/images/django-ray.svg" alt="django-ray logo" width="96" height="96">
</p>

<h1 align="center">django-ray</h1>

<p align="center">
  A Ray-based backend for <a href="https://github.com/django/django">Django Tasks</a> that enables distributed task execution with database-backed reliability.
</p>

<p align="center">
  <a href="https://github.com/dariuszpanas/django-ray/actions/workflows/ci.yml"><img src="https://github.com/dariuszpanas/django-ray/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dariuszpanas/django-ray/actions/workflows/docs.yml"><img src="https://github.com/dariuszpanas/django-ray/actions/workflows/docs.yml/badge.svg" alt="Docs"></a>
  <a href="https://pypi.org/project/django-ray/"><img src="https://img.shields.io/pypi/pyversions/django-ray.svg" alt="Python versions"></a>
  <a href="https://github.com/dariuszpanas/django-ray/blob/main/LICENSE"><img src="https://img.shields.io/github/license/dariuszpanas/django-ray.svg" alt="License"></a>
</p>

## Why django-ray?

Django projects often need background task execution. While Celery has been the go-to solution for years, [Ray](https://ray.io) offers a more powerful and flexible approach to distributed computing:

- **True distributed computing**: Ray was built for distributed workloads from the ground up, not just task queues
- **Horizontal scaling**: Scale from a single machine to thousands of nodes without changing your code
- **Resource-aware scheduling**: Request specific CPU, GPU, or memory for tasks
- **Actor model support**: Maintain stateful workers when needed
- **Explicit Ray ecosystem boundary**: Run Ray Data, Train, Tune, or RLlib in
  application-owned code after installing its component extra; keep online serving on
  Ray Serve's separate lifecycle. These are not first-class adapters in django-ray.

Despite Ray's capabilities, there was no straightforward way to use it with Django's built-in Tasks framework. django-ray bridges this gap, letting you leverage Ray's distributed computing power while keeping Django's familiar patterns and database-backed reliability.

Moving an existing workload? Use
[Migrating from Celery](https://django-ray.readthedocs.io/en/latest/celery-migration/) to classify
semantic gaps, run both backends during adoption, and prove the old queue is drained.

## Overview

django-ray bridges Django's built-in Tasks framework with Ray's distributed computing capabilities, providing:

- **Durable visibility and recovery**: Task state remains in your Django database so workers and
  operators can reconcile lost or stuck execution; queued work can expire or be cancelled before
  it starts, while work that does start may be replayed after uncertain completion, so side effects
  must be idempotent
- **Multiple execution modes**: Sync, local Ray, Ray cluster, or Ray Job API
- **Coroutine tasks**: Await Django async task functions consistently in every mode
- **Automatic retries**: Failed tasks are retried with exponential backoff
- **Admin visibility**: Monitor and manage tasks through Django admin
- **Graceful shutdown**: Workers handle signals properly for clean shutdown
- **Django-durable workflows on Ray Core**: Chain, group, and dynamically fan out
  low-overhead internal steps behind one durable Django task; this is unrelated to the
  former, removed `ray.workflow` package
- **RuntimeEnv profiles**: Run versioned or lightweight Python environments on a
  generic Ray cluster, with immutable environment identity per durable task
- **Observable workflow graphs**: Track dependency edges, node progress, Ray
  execution identifiers, and correlated logs for custom monitoring UIs
- **Operational observability**: Use versioned task services, bounded-cardinality
  Prometheus metrics, and authenticated live updates in Django admin

The repository includes a sample `testproject/` with a small landing page for exploring the bundled API,
task stats, project links, and smoke-task trigger:

![django-ray testproject landing page](https://raw.githubusercontent.com/dariuszpanas/django-ray/main/docs/assets/images/testproject-landing.png)

## Requirements

- Python 3.12, 3.13, or 3.14
- Django 6.0+
- Ray 2.56.0+

## Installation

```bash
pip install django-ray
```

Or with uv:

```bash
uv add django-ray
```

The base package installs `ray[default]` for Ray Core, Ray Client, Ray Jobs, and live
Dashboard/State diagnostics. It does not install usable Ray Data, Train, Tune, RLlib,
Serve, Serve LLM, or native Compiled Graph dependencies. Review the
[Ray Ecosystem Support and Install Matrix](https://django-ray.readthedocs.io/en/latest/ray-ecosystem/)
before adding one of those components to a task image or RuntimeEnv.

## Quick Start

1. Add `django_ray` to your `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ...
    "django_ray",
]
```

2. Configure Django Tasks and django-ray:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
    },
}

DJANGO_RAY = {
    "RAY_ADDRESS": "auto",  # Use "ray://host:port" for bounded remote Ray Core work
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 10,
    "MAX_TASK_ATTEMPTS": 3,
}
```

3. Run migrations:

```bash
python manage.py migrate django_ray
```

4. Define a task in `myapp/tasks.py`:

```python
from django.tasks import task


@task(queue_name="default")
def add_numbers(left: int, right: int) -> int:
    return left + right
```

5. In a separate terminal, start the worker:

```bash
# Local Ray (recommended for development)
python manage.py django_ray_worker --queue=default --local

# Connect through Ray Client for bounded, low-latency work
python manage.py django_ray_worker --queue=default --cluster=ray://localhost:10001

# Sync mode (no Ray, for testing)
python manage.py django_ray_worker --queue=default --sync
```

6. Enqueue the task from `python manage.py shell`:

```python
from myapp.tasks import add_numbers

enqueued = add_numbers.enqueue(20, 22)
print(enqueued.id)
```

After the worker reports completion, refresh the durable result in the same shell:

```python
from django.tasks import TaskResultStatus, task_backends

current = task_backends["default"].get_result(enqueued.id)
if current.status == TaskResultStatus.SUCCESSFUL:
    print(current.return_value)  # 42
```

The object returned by `enqueue()` is a snapshot; call `get_result()` again when you need current
state. Continue with
[Getting Started](https://django-ray.readthedocs.io/en/latest/getting-started/) for the complete
walkthrough and its before-production checklist.

## Worker Execution Modes

| Mode | Flag | Description |
|------|------|-------------|
| **sync** | `--sync` | Direct execution, no Ray (testing) |
| **local** | `--local` | Local Ray cluster, tasks via `@ray.remote` |
| **cluster** | `--cluster=<addr>` | Bounded low-latency work through Ray Client |
| **ray-job** | *(default)* | Ray Job Submission API (process isolation) |

Cluster mode is tied to the task manager's Ray Client connection. If that connection
is lost beyond Ray's reconnect grace period, Ray terminates its in-flight workload;
django-ray can reconcile and retry the outer task, but it does not resume completed
workflow leaves or roll back side effects. Use idempotent work and prefer Ray Job mode
for long or coarse execution that must continue independently of the submitting
connection. See Ray's
[Ray Client lifetime guidance](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/ray-client.html).

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `RAY_ADDRESS` | `None` | Must be set at runtime; use `"auto"` locally or `"ray://host:port"` for a cluster |
| `DEFAULT_CONCURRENCY` | `10` | Max concurrent tasks per worker |
| `MAX_TASK_ATTEMPTS` | `3` | Max retry attempts |
| `RETRY_BACKOFF_SECONDS` | `60` | Base backoff for retries |
| `RETRY_EXCEPTION_DENYLIST` | `[]` | Exception types that skip auto-retry |
| `STUCK_TASK_TIMEOUT_SECONDS` | `300` | Timeout before marking tasks as LOST |
| `TASK_MONITOR_HEARTBEAT_SECONDS` | `15` | Database heartbeat interval for in-flight Ray Core tasks |
| `RUNTIME_ENV_PROFILES` | `{}` | Named Ray environments for code and dependencies |
| `DEFAULT_RUNTIME_ENV_PROFILE` | `None` | Default named environment |
| `MAX_INLINE_INPUT_SIZE_BYTES` | `None` | Opt-in durable input spillover threshold |

See [Ray-Native Workflows](https://django-ray.readthedocs.io/en/latest/workflows/) for low-latency `chain`, `group`,
and `map_step` execution.
See [Django Gateway to Private Ray Serve](https://django-ray.readthedocs.io/en/latest/ray-serve-gateway/)
for bounded authenticated online inference without writing a FastAPI ingress or adding
package-owned Serve orchestration.
See [Runtime Environments](https://django-ray.readthedocs.io/en/latest/runtime-environments/) for per-task profiles,
workflow overrides, and generic KubeRay images.
See [Performance](https://django-ray.readthedocs.io/en/latest/performance/) for choosing durable task boundaries,
execution modes, and useful fan-out granularity.
See [Durable Input Storage](https://django-ray.readthedocs.io/en/latest/reference/input-storage/) for oversized JSON
arguments, storage backends, rollout, and retention.
See [Defining Tasks](https://django-ray.readthedocs.io/en/latest/tasks/#coroutine-tasks) for async task and ORM safety
guidance.

## Development Setup

### Prerequisites
- Python 3.12, 3.13, or 3.14
- [uv](https://github.com/astral-sh/uv) package manager

### Installation

```bash
git clone https://github.com/dariuszpanas/django-ray.git
cd django-ray
uv sync
```

### Development Commands

Run development targets through `uv run` unless the virtual environment is already active. Targets
named `lint`, `check`, and `ci` are non-mutating; use `format` or `fix` when files should change.

```bash
uv sync               # Install locked dependencies
uv run make format    # Format code with Ruff
uv run make fix       # Format and apply safe Ruff lint fixes
uv run make lint      # Check lint without modifying files
uv run make typecheck # Type check with ty
uv run make test      # Run tests
uv run make test-xdist # Run the default-resource subset with four xdist workers
uv run make test-cov  # Run tests with CI coverage floors
uv run make check     # Check formatting, lint, and types without changes
uv run make ci        # Check coverage, docs, and package build for this interpreter
uv run make docs-build       # Build docs (Zensical)
uv run make docs-build-strict # Build docs in strict mode
uv run make docs-serve       # Serve docs locally
```

### Django Commands

```bash
uv run make migrate          # Run migrations
uv run make runserver        # Start dev server
uv run make shell            # Django shell
uv run make createsuperuser  # Create admin user
```

### Worker Commands

```bash
uv run make worker           # Ray Job API mode
uv run make worker-local     # Native local Ray for single-host development
uv run make worker-sync      # Sync mode (no Ray)
uv run make worker-all       # All django-ray backend queues, local Ray
uv run make worker-cluster   # Connect to cluster
```

Linux is the production target. Ray's native Windows support is beta, so prefer WSL2 or
the documented Docker path for repeatable development and keep one native local-Ray
owner on a Windows host at a time. See the
[platform compatibility boundary](https://django-ray.readthedocs.io/en/latest/compatibility/#platforms)
before choosing native local mode.

### Quick Start (End-to-End Testing)

Generate a disposable bearer token in the shell that will start Django, then migrate the database.
On POSIX:

```bash
export DJANGO_API_TOKEN="$(
  uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
uv run make migrate
```

On PowerShell:

```powershell
$env:DJANGO_API_TOKEN = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run make migrate
```

Start the web process and worker in separate terminals that share the same project checkout and
database:

```bash
uv run make runserver
uv run make worker-all
```

**Browser - Test via API:**

1. Open http://127.0.0.1:8000/api/docs (Swagger UI)
2. Select **Authorize** and paste the generated token value
3. Try `POST /api/enqueue/add/100/200`
4. Refresh `GET /api/tasks/{task_id}`, then check `GET /api/executions`
5. View in Admin: http://127.0.0.1:8000/admin/django_ray/raytaskexecution/

The tracked Docker Compose path below is the reproducible bundled-application smoke, including
PostgreSQL and migration ordering.

### Queue Configuration

The unqualified commands below select Ray Job mode. Configure a retrievable
`INPUT_STORAGE_BACKEND` (including its filesystem root or object-store namespace) before
starting them; new rq2 submissions always externalize the canonical execution request.
For source-checkout development without shared request storage, add `--local` or use
`uv run make worker-local`/`worker-all` instead.

```bash
# Single queue
uv run python testproject/manage.py django_ray_worker --queue=default

# Multiple queues
uv run python testproject/manage.py django_ray_worker --queue=default,high-priority,low-priority

# All queues configured on django-ray backend aliases
uv run python testproject/manage.py django_ray_worker --all-queues
```

## Docker

The canonical local evaluation path is the tracked Compose application. Generate the required
disposable credentials, start the web and worker services, then run the bounded end-to-end smoke:

```bash
export DJANGO_API_TOKEN="$(
  uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
export POSTGRES_PASSWORD="$(
  uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"

docker compose up --build --detach web worker
docker compose --profile smoke run --rm --no-deps smoke
```

PowerShell, authenticated request, result-refresh, admin, and cleanup commands are in the
[bundled testproject quickstart](https://github.com/dariuszpanas/django-ray/blob/main/testproject/README.md). The local Compose topology and its generated
credentials are not production hardening.

## Kubernetes Deployment

Evaluate the bundled Kustomize manifests in `k8s/` only on a trusted, disposable local
environment. They are maintainer-validation assets, not a production-ready deployment, and
replacing their placeholder values does not make the sample topology production-ready.

```bash
# Build images
make k8s-build

# Deploy
make k8s-deploy

# Check status
make k8s-status

# With TLS enabled
make k8s-gen-tls-certs
make k8s-deploy-tls
```


See [k8s/README.md](https://github.com/dariuszpanas/django-ray/blob/main/k8s/README.md) for detailed deployment documentation.

## Project Structure

```
django-ray/
├── src/django_ray/          # Library source code
│   ├── models.py            # RayTaskExecution, TaskWorkerLease
│   ├── admin.py             # Admin interface
│   ├── backends.py          # Django Task Backend
│   ├── conf/                # Settings
│   ├── runner/              # Task runners
│   │   ├── ray_job.py       # Ray Job Submission API
│   │   ├── ray_core.py      # Ray Core (@ray.remote)
│   │   ├── leasing.py       # Worker coordination
│   │   └── retry.py         # Retry logic
│   ├── runtime/             # Task execution
│   │   ├── entrypoint.py    # Execution entry point
│   │   ├── distributed.py   # parallel_map, scatter_gather
│   │   └── serialization.py
│   └── management/commands/
│       └── django_ray_worker.py
│
├── testproject/             # Example project (development only)
│   ├── api.py               # Example REST API
│   ├── tasks.py             # Example tasks
│   └── apps/                # Example apps
│
├── tests/                   # Test suite
├── docs/                    # Documentation
└── k8s/                     # Kubernetes manifests
```

## Documentation

Published docs are served with Zensical at:

- https://django-ray.readthedocs.io/en/latest/

Agents and documentation tools can start with
[`llms.txt`](https://django-ray.readthedocs.io/en/latest/llms.txt). The published
documentation also serves `/llms.txt`.

Read the Docs builds are configured in `.readthedocs.yaml`. The build installs `uv`, runs the
strict Zensical build, and copies the generated `site/` output into Read the Docs' HTML output
directory.

Source docs remain in the [`docs/`](https://github.com/dariuszpanas/django-ray/tree/main/docs) directory:

- [Getting Started](https://github.com/dariuszpanas/django-ray/blob/main/docs/getting-started.md) - Installation and basic setup
- [Configuration](https://github.com/dariuszpanas/django-ray/blob/main/docs/configuration.md) - All configuration options
- [Worker Modes](https://github.com/dariuszpanas/django-ray/blob/main/docs/worker-modes.md) - Execution modes explained
- [Ray Ecosystem Support](https://github.com/dariuszpanas/django-ray/blob/main/docs/ray-ecosystem.md) -
  Component installs, durable exchange, lifecycle ownership, and evidence
- [Tasks](https://github.com/dariuszpanas/django-ray/blob/main/docs/tasks.md) - Defining and enqueueing tasks
- [Queues](https://github.com/dariuszpanas/django-ray/blob/main/docs/queues.md) - Working with task queues
- [Retry & Error Handling](https://github.com/dariuszpanas/django-ray/blob/main/docs/retry.md) - Configuring retries
- [Migrating from Celery](https://github.com/dariuszpanas/django-ray/blob/main/docs/celery-migration.md) -
  Classifying workloads, running both backends, and draining Celery safely

### Deployment

- [Kubernetes](https://github.com/dariuszpanas/django-ray/blob/main/docs/deployment/kubernetes.md) - Deploy to Kubernetes
- [Docker](https://github.com/dariuszpanas/django-ray/blob/main/docs/deployment/docker.md) - Running with Docker
- [TLS](https://github.com/dariuszpanas/django-ray/blob/main/docs/deployment/tls.md) - Securing Ray communication

### Reference

- [CLI Reference](https://github.com/dariuszpanas/django-ray/blob/main/docs/reference/cli.md) - Command-line options
- [Settings Reference](https://github.com/dariuszpanas/django-ray/blob/main/docs/reference/settings.md) - All settings
- [API Reference](https://github.com/dariuszpanas/django-ray/blob/main/docs/reference/api.md) - REST API endpoints

## Contributing

See [`CONTRIBUTING.md`](https://github.com/dariuszpanas/django-ray/blob/main/CONTRIBUTING.md) for
branch, commit, pull request, staging, and validation conventions. Automated contributors must also
follow [`AGENTS.md`](https://github.com/dariuszpanas/django-ray/blob/main/AGENTS.md).

## Security

Report suspected vulnerabilities through the private channel in
the [security policy](https://github.com/dariuszpanas/django-ray/security/policy). Do not put
vulnerability details, exploit instructions, credentials, or secrets in a public issue.

## License

This project is licensed under the BSD 3-Clause License - see the [LICENSE](https://github.com/dariuszpanas/django-ray/blob/main/LICENSE) file for details.

