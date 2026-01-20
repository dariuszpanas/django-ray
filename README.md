# django-ray

A Ray-based backend for [Django Tasks](https://github.com/django/django) that enables distributed task execution with database-backed reliability.

## Why django-ray?

Django projects often need background task execution. While Celery has been the go-to solution for years, [Ray](https://ray.io) offers a more powerful and flexible approach to distributed computing:

- **True distributed computing**: Ray was built for distributed workloads from the ground up, not just task queues
- **Horizontal scaling**: Scale from a single machine to thousands of nodes without changing your code
- **Resource-aware scheduling**: Request specific CPU, GPU, or memory for tasks
- **Actor model support**: Maintain stateful workers when needed
- **Rich ecosystem**: Access to Ray's ML libraries, data processing, and more

Despite Ray's capabilities, there was no straightforward way to use it with Django's built-in Tasks framework. django-ray bridges this gap, letting you leverage Ray's distributed computing power while keeping Django's familiar patterns and database-backed reliability.

## Overview

django-ray bridges Django's built-in Tasks framework with Ray's distributed computing capabilities, providing:

- **Database-backed reliability**: Task state is tracked in your Django database, ensuring no tasks are lost
- **Multiple execution modes**: Sync, local Ray, Ray cluster, or Ray Job API
- **Automatic retries**: Failed tasks are retried with exponential backoff
- **Admin visibility**: Monitor and manage tasks through Django admin
- **Graceful shutdown**: Workers handle signals properly for clean shutdown

## Requirements

- Python 3.12+
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
    "RAY_ADDRESS": "ray://localhost:10001",
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
# Local Ray (recommended for development)
python manage.py django_ray_worker --queue=default --local

# Connect to Ray cluster
python manage.py django_ray_worker --queue=default --cluster=ray://localhost:10001

# Sync mode (no Ray, for testing)
python manage.py django_ray_worker --queue=default --sync
```

## Worker Execution Modes

| Mode | Flag | Description |
|------|------|-------------|
| **sync** | `--sync` | Direct execution, no Ray (testing) |
| **local** | `--local` | Local Ray cluster, tasks via `@ray.remote` |
| **cluster** | `--cluster=<addr>` | Remote Ray cluster, tasks via `@ray.remote` |
| **ray-job** | *(default)* | Ray Job Submission API (process isolation) |

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `RAY_ADDRESS` | *required* | Ray cluster address |
| `DEFAULT_CONCURRENCY` | `10` | Max concurrent tasks per worker |
| `MAX_TASK_ATTEMPTS` | `3` | Max retry attempts |
| `RETRY_BACKOFF_SECONDS` | `60` | Base backoff for retries |
| `RETRY_EXCEPTION_DENYLIST` | `[]` | Exception types that skip auto-retry |
| `STUCK_TASK_TIMEOUT_SECONDS` | `300` | Timeout before marking tasks as LOST |

## Development Setup

### Prerequisites
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager

### Installation

```bash
git clone <repository-url>
cd django-ray
uv sync
```

### Development Commands

```bash
make install     # Install dependencies
make format      # Format code with Ruff
make lint        # Lint code with Ruff
make typecheck   # Type check with ty
make test        # Run tests
make check       # Run lint + typecheck
make ci          # Run all CI checks
```

### Django Commands

```bash
make migrate          # Run migrations
make runserver        # Start dev server
make shell            # Django shell
make createsuperuser  # Create admin user
```

### Worker Commands

```bash
make worker           # Ray Job API mode
make worker-local     # Local Ray (recommended)
make worker-sync      # Sync mode (no Ray)
make worker-all       # All queues, local Ray
make worker-cluster   # Connect to cluster
```

### Quick Start (End-to-End Testing)

**Terminal 1 - Start Django server:**
```bash
make runserver
```

**Terminal 2 - Start worker:**
```bash
make worker-all
```

**Browser - Test via API:**
1. Open http://127.0.0.1:8000/api/docs (Swagger UI)
2. Try `POST /api/enqueue/add/100/200`
3. Check `GET /api/executions` - see task completed with result `300`
4. View in Admin: http://127.0.0.1:8000/admin/django_ray/raytaskexecution/

### Queue Configuration

```bash
# Single queue
python manage.py django_ray_worker --queue=default

# Multiple queues
python manage.py django_ray_worker --queue=default,high-priority,low-priority

# All configured queues
python manage.py django_ray_worker --all-queues
```

## Docker

```bash
make docker-build

# Run modes:
docker run -p 8000:8000 django-ray:latest web          # Production
docker run -p 8000:8000 django-ray:latest web-dev      # Development
docker run django-ray:latest worker                     # Worker (local Ray)
docker run django-ray:latest worker-cluster             # Worker (cluster)
```

## Kubernetes Deployment

Deploy using Kustomize manifests in `k8s/`:

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


See [k8s/README.md](k8s/README.md) for detailed deployment documentation.

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

Full documentation is available in the [docs/](https://github.com/dariuszpanas/django-ray/tree/main/docs) directory:

- [Getting Started](https://github.com/dariuszpanas/django-ray/blob/main/docs/getting-started.md) - Installation and basic setup
- [Configuration](https://github.com/dariuszpanas/django-ray/blob/main/docs/configuration.md) - All configuration options
- [Worker Modes](https://github.com/dariuszpanas/django-ray/blob/main/docs/worker-modes.md) - Execution modes explained
- [Tasks](https://github.com/dariuszpanas/django-ray/blob/main/docs/tasks.md) - Defining and enqueueing tasks
- [Queues](https://github.com/dariuszpanas/django-ray/blob/main/docs/queues.md) - Working with task queues
- [Retry & Error Handling](https://github.com/dariuszpanas/django-ray/blob/main/docs/retry.md) - Configuring retries

### Deployment

- [Kubernetes](https://github.com/dariuszpanas/django-ray/blob/main/docs/deployment/kubernetes.md) - Deploy to Kubernetes
- [Docker](https://github.com/dariuszpanas/django-ray/blob/main/docs/deployment/docker.md) - Running with Docker
- [TLS](https://github.com/dariuszpanas/django-ray/blob/main/docs/deployment/tls.md) - Securing Ray communication

### Reference

- [CLI Reference](https://github.com/dariuszpanas/django-ray/blob/main/docs/reference/cli.md) - Command-line options
- [Settings Reference](https://github.com/dariuszpanas/django-ray/blob/main/docs/reference/settings.md) - All settings
- [API Reference](https://github.com/dariuszpanas/django-ray/blob/main/docs/reference/api.md) - REST API endpoints

## License

This project is licensed under the BSD 3-Clause License - see the [LICENSE](https://github.com/dariuszpanas/django-ray/blob/main/LICENSE) file for details.

