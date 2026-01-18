# django-ray

A Ray-based backend for [Django Tasks](https://github.com/django/django) that enables distributed task execution with database-backed reliability.

## Overview

django-ray bridges Django's built-in Tasks framework with Ray's distributed computing capabilities, providing:

- **Database-backed reliability**: Task state is tracked in your Django database, ensuring no tasks are lost
- **Ray Job Submission**: Execute tasks on a Ray cluster via the Job Submission API
- **Automatic retries**: Failed tasks are retried with exponential backoff
- **Admin visibility**: Monitor and manage tasks through Django admin
- **Graceful shutdown**: Workers handle signals properly for clean shutdown

## Requirements

- Python 3.12 or higher
- Django 6.0+
- Ray 2.53.0+

## Installation

```bash
pip install django-ray
```

Or with uv:

```bash
uv add django-ray
```

## Quick Start

1. Add `django_ray` to your `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ...
    "django_ray",
]
```

2. Configure django-ray settings:

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://localhost:10001",  # Required
    "DEFAULT_CONCURRENCY": 10,
    "MAX_TASK_ATTEMPTS": 3,
}
```

3. Run migrations:

```bash
python manage.py migrate django_ray
```

4. Start the worker:

```bash
python manage.py django_ray_worker --queue=default
```

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `RAY_ADDRESS` | *required* | Ray cluster address (e.g., `ray://localhost:10001`) |
| `RUNNER` | `ray_job` | Runner type (`ray_job` or `ray_core`) |
| `DEFAULT_CONCURRENCY` | `10` | Maximum concurrent tasks per worker |
| `MAX_TASK_ATTEMPTS` | `3` | Maximum retry attempts for failed tasks |
| `RETRY_BACKOFF_SECONDS` | `60` | Base backoff for retries (exponential) |
| `STUCK_TASK_TIMEOUT_SECONDS` | `300` | Timeout before marking tasks as LOST |
| `MAX_RESULT_SIZE_BYTES` | `1048576` | Maximum result size for DB storage (1MB) |

## Development Setup

### Prerequisites
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd django-ray
```

2. Sync dependencies using uv:
```bash
uv sync
```

### Development Tools

This project uses:
- **Ruff**: Fast Python linter and formatter
- **ty**: Astral's static type checker
- **pytest**: Testing framework
- **pytest-django**: Django testing utilities

### Using Make Commands

Run all checks (format, lint, type check, tests):
```bash
make all
```

Development workflow:
```bash
make install     # Install dependencies with uv
make format      # Format code with Ruff
make lint        # Lint code with Ruff
make typecheck   # Type check with ty
make test        # Run tests with pytest
make test-cov    # Run tests with coverage report
make check       # Run lint + typecheck (no formatting)
make ci          # Run all CI checks (no modifications)
make build       # Build the package
make clean       # Clean up cache files
```

Django test project commands:
```bash
make migrate          # Run Django migrations
make runserver        # Start Django dev server
make shell            # Open Django shell
make makemigrations   # Create new migrations
make createsuperuser  # Create Django superuser
```

### API (Swagger UI)

The test project includes a Django Ninja API for managing tasks. Start the server and visit:

- **Swagger UI**: http://127.0.0.1:8000/api/docs
- **OpenAPI Schema**: http://127.0.0.1:8000/api/openapi.json

Available endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tasks` | GET | List all tasks with filtering |
| `/api/tasks` | POST | Create a new task |
| `/api/tasks/stats` | GET | Get task statistics |
| `/api/tasks/{id}` | GET | Get task details |
| `/api/tasks/{id}` | DELETE | Delete a task |
| `/api/tasks/{id}/cancel` | POST | Cancel a task |
| `/api/tasks/{id}/retry` | POST | Retry a failed task |
| `/api/tasks/reset` | POST | Reset stuck tasks to QUEUED |
| `/api/enqueue/add/{a}/{b}` | POST | Quick add_numbers task |
| `/api/enqueue/slow/{seconds}` | POST | Quick slow_task |
| `/api/enqueue/fail` | POST | Quick failing_task |

### Quick Start (End-to-End Testing)

**Terminal 1 - Start Django server:**
```bash
make runserver
```

**Terminal 2 - Start worker:**
```bash
make worker-local   # Uses Ray locally (recommended)
# or
make worker-sync    # No Ray, runs tasks in-process
```

**Browser - Test via API:**
1. Open http://127.0.0.1:8000/api/docs
2. Try `POST /api/enqueue/add/100/200` - enqueues add_numbers(100, 200)
3. Check `GET /api/tasks` - see task completed with result `300`
4. View in Admin: http://127.0.0.1:8000/admin/django_ray/raytaskexecution/

### Manual Commands

If you prefer running commands directly:

```bash
uv run ruff format .           # Format code
uv run ruff check . --fix      # Lint and auto-fix
uv run ty check                # Type checking
uv run pytest                  # Run tests
uv run pytest --cov=src        # Tests with coverage
```

### Test Project

A test Django project is included at `testproject/` for development and testing:

```bash
make migrate      # Run migrations
make runserver    # Start dev server
make shell        # Django shell
```

## CI/CD

This project uses GitHub Actions for continuous integration. The workflow runs on every push and pull request to `main`:

- **Lint & Format Check**: Runs Ruff formatting and linting checks
- **Type Check**: Runs ty for static type analysis
- **Test**: Runs the test suite with coverage
- **Build**: Builds the package and uploads artifacts
- **Docker**: Builds and pushes Docker images to GitHub Container Registry
- **K3d Integration**: Runs Kubernetes-based integration tests

## Docker

Build and run the Docker image:

```bash
# Build production image
make docker-build

# Run modes:
docker run -p 8000:8000 django-ray:latest web          # Production (gunicorn)
docker run -p 8000:8000 django-ray:latest web-dev      # Development server
docker run django-ray:latest worker                     # Task worker (local Ray)
docker run django-ray:latest worker-cluster             # Worker (Ray cluster)
docker run django-ray:latest migrate                    # Run migrations
docker run django-ray:latest shell                      # Django shell
```

The production image is a multi-stage build optimized for size (~170MB) and uses gunicorn for serving.

## Kubernetes Deployment

Deploy to a Kubernetes cluster using the included Kustomize manifests.

### Quick Start with k3d (Local Kubernetes)

```bash
# Create local k3d cluster
make k3d-create

# Build image and deploy
make k3d-deploy

# Check status
make k3d-status

# Access the application
# Django Web/API: http://localhost:8080
# Swagger UI: http://localhost:8080/api/docs
# Ray Dashboard: http://localhost:8265

# View logs
make k3d-logs-web      # Django web logs
make k3d-logs-worker   # Django-Ray worker logs
make k3d-logs-ray      # Ray cluster logs

# Cleanup
make k3d-delete
```

### Components

| Component | Description |
|-----------|-------------|
| PostgreSQL | Database for Django and task metadata |
| Ray Head | Ray cluster coordinator with dashboard |
| Ray Workers | Ray execution nodes |
| Django Web | Web application and API |
| Django-Ray Worker | Task processor |

See [k8s/README.md](k8s/README.md) for detailed deployment documentation.

## Project Structure

```
├── Makefile             # Development task automation
├── pyproject.toml       # Project configuration
├── Dockerfile           # Production Docker image
├── Dockerfile.dev       # Development Docker image
├── testproject/         # Django test project
│   ├── settings.py
│   ├── urls.py
│   ├── api.py           # REST API (Django Ninja)
│   ├── tasks.py         # Sample tasks
│   └── manage.py
├── k8s/                 # Kubernetes manifests
│   ├── base/            # Base Kustomize config
│   └── overlays/dev/    # Dev overlay for k3d
├── src/django_ray/      # Main package
├── __init__.py
├── apps.py              # Django app configuration
├── models.py            # RayTaskExecution, RayWorkerLease
├── admin.py             # Admin interface
├── conf/                # Configuration
│   ├── defaults.py
│   └── settings.py
├── runtime/             # Ray execution environment
│   ├── entrypoint.py    # Task execution entrypoint
│   ├── import_utils.py  # Callable import utilities
│   ├── serialization.py # Argument serialization
│   └── redaction.py     # Sensitive data redaction
├── runner/              # Task submission and control
│   ├── base.py          # Base runner interface
│   ├── ray_job.py       # Ray Job Submission runner
│   ├── ray_core.py      # Ray Core runner (Phase 3)
│   ├── reconciliation.py
│   ├── leasing.py
│   ├── retry.py
│   └── cancellation.py
├── results/             # Result storage
│   ├── base.py
│   ├── db.py            # Database result store
│   └── external.py      # External storage (Phase 4)
├── metrics/             # Observability
│   └── prometheus.py
└── management/commands/
    └── django_ray_worker.py
```

## License

MIT


