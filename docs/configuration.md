# Configuration

django-ray is configured through the `DJANGO_RAY` setting in your Django settings file.

## Basic Configuration

```python
# settings.py
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "DEFAULT_CONCURRENCY": 10,
    "MAX_TASK_ATTEMPTS": 3,
}
```

## All Settings

### Ray Connection

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `RAY_ADDRESS` | `str` | `None` | Ray cluster address. Use `"auto"` for local, or `"ray://host:port"` for cluster |
| `RAY_RUNTIME_ENV` | `dict` | `{}` | Ray runtime environment configuration |

### Concurrency

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `DEFAULT_CONCURRENCY` | `int` | `10` | Maximum concurrent tasks per worker |

### Retry Policy

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `MAX_TASK_ATTEMPTS` | `int` | `3` | Maximum number of attempts before marking as failed |
| `RETRY_BACKOFF_SECONDS` | `int` | `60` | Base delay between retries (exponential backoff) |
| `RETRY_EXCEPTION_DENYLIST` | `list[str]` | `[]` | Exception types that should not be retried |

### Reliability

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `STUCK_TASK_TIMEOUT_SECONDS` | `int` | `300` | Time before a running task is considered stuck |
| `WORKER_LEASE_SECONDS` | `int` | `60` | Worker lease duration for distributed coordination |
| `WORKER_HEARTBEAT_SECONDS` | `int` | `15` | Heartbeat interval for worker health checks |

### Results

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `MAX_RESULT_SIZE_BYTES` | `int` | `1048576` | Maximum result size to store in database (1MB) |

## Example Configurations

### Development

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "DEFAULT_CONCURRENCY": 5,
    "MAX_TASK_ATTEMPTS": 1,  # Fail fast during development
    "STUCK_TASK_TIMEOUT_SECONDS": 60,
}
```

### Production

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "DEFAULT_CONCURRENCY": 50,
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 120,
    "STUCK_TASK_TIMEOUT_SECONDS": 600,
    "WORKER_LEASE_SECONDS": 120,
    "WORKER_HEARTBEAT_SECONDS": 30,
}
```

### High-Throughput

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "DEFAULT_CONCURRENCY": 100,
    "MAX_TASK_ATTEMPTS": 5,
    "RETRY_BACKOFF_SECONDS": 30,
}
```

## Environment Variables

Settings can also be configured via environment variables. The worker reads these directly:

| Variable | Description |
|----------|-------------|
| `RAY_ADDRESS` | Ray cluster address |
| `DJANGO_RAY_QUEUE` | Default queue name |
| `DJANGO_RAY_CONCURRENCY` | Concurrency limit |

## Django Tasks Configuration

django-ray integrates with Django's native Tasks framework. Configure the backend in `TASKS`:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default", "high-priority", "low-priority"],
    },
}
```

## See Also

- [Worker Modes](worker-modes.md) - How different modes affect configuration
- [Retry & Error Handling](retry.md) - Detailed retry configuration

