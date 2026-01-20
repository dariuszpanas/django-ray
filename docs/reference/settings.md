# Settings Reference

Complete reference for all django-ray settings.

## DJANGO_RAY

All settings are configured under the `DJANGO_RAY` dictionary in your Django settings:

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://localhost:10001",
    "DEFAULT_CONCURRENCY": 10,
    # ... other settings
}
```

## Ray Connection

### RAY_ADDRESS

- **Type**: `str | None`
- **Default**: `None`
- **Required**: Yes (for cluster/ray-job modes)

Ray cluster address. Use `"auto"` for automatic detection or `"ray://host:port"` for explicit address.

```python
# Local Ray (auto-detect)
"RAY_ADDRESS": "auto"

# Remote cluster
"RAY_ADDRESS": "ray://ray-head-svc:10001"
```

### RAY_RUNTIME_ENV

- **Type**: `dict`
- **Default**: `{}`

Ray runtime environment configuration. Passed to Ray when initializing.

```python
"RAY_RUNTIME_ENV": {
    "pip": ["pandas", "numpy"],
    "env_vars": {"MY_VAR": "value"},
}
```

## Concurrency

### DEFAULT_CONCURRENCY

- **Type**: `int`
- **Default**: `10`

Maximum number of concurrent tasks per worker.

```python
"DEFAULT_CONCURRENCY": 50
```

## Retry Policy

### MAX_TASK_ATTEMPTS

- **Type**: `int`
- **Default**: `3`

Maximum number of attempts before marking a task as failed. Includes the initial attempt.

```python
"MAX_TASK_ATTEMPTS": 5  # Initial + 4 retries
```

### RETRY_BACKOFF_SECONDS

- **Type**: `int`
- **Default**: `60`

Base delay in seconds between retry attempts. Uses exponential backoff:
- Attempt 2: `RETRY_BACKOFF_SECONDS * 1`
- Attempt 3: `RETRY_BACKOFF_SECONDS * 2`
- Attempt 4: `RETRY_BACKOFF_SECONDS * 4`

```python
"RETRY_BACKOFF_SECONDS": 120  # 2 minutes base delay
```

### RETRY_EXCEPTION_DENYLIST

- **Type**: `list[str]`
- **Default**: `[]`

List of exception class names that should not be retried. Use full dotted path.

```python
"RETRY_EXCEPTION_DENYLIST": [
    "ValueError",
    "KeyError",
    "myapp.exceptions.PermanentError",
]
```

## Reliability

### STUCK_TASK_TIMEOUT_SECONDS

- **Type**: `int`
- **Default**: `300` (5 minutes)

Time in seconds after which a running task with no updates is considered stuck and marked as LOST.

```python
"STUCK_TASK_TIMEOUT_SECONDS": 600  # 10 minutes
```

### WORKER_LEASE_SECONDS

- **Type**: `int`
- **Default**: `60`

Duration of worker lease for distributed coordination. Workers must renew their lease within this period.

```python
"WORKER_LEASE_SECONDS": 120
```

### WORKER_HEARTBEAT_SECONDS

- **Type**: `int`
- **Default**: `15`

Interval between worker heartbeats. Should be less than `WORKER_LEASE_SECONDS`.

```python
"WORKER_HEARTBEAT_SECONDS": 30
```

## Results

### MAX_RESULT_SIZE_BYTES

- **Type**: `int`
- **Default**: `1048576` (1 MB)

Maximum size of task results to store in the database. Larger results should be stored externally.

```python
"MAX_RESULT_SIZE_BYTES": 10 * 1024 * 1024  # 10 MB
```

## Django Settings

These settings are configured directly in Django settings, not in `DJANGO_RAY`:

### RAY_DASHBOARD_URL

- **Type**: `str`
- **Default**: `"http://localhost:30265"`

URL of the Ray Dashboard. Used by Django Admin to generate deep links to tasks in the Ray Dashboard.

```python
# settings.py
RAY_DASHBOARD_URL = "http://ray-dashboard.example.com:8265"
```

## Example Configurations

### Minimal (Development)

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
}
```

### Standard (Production)

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "DEFAULT_CONCURRENCY": 50,
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 60,
    "STUCK_TASK_TIMEOUT_SECONDS": 300,
    "WORKER_LEASE_SECONDS": 60,
    "WORKER_HEARTBEAT_SECONDS": 15,
}
```

### High Throughput

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "DEFAULT_CONCURRENCY": 200,
    "MAX_TASK_ATTEMPTS": 5,
    "RETRY_BACKOFF_SECONDS": 30,
    "STUCK_TASK_TIMEOUT_SECONDS": 600,
}
```

### Fail Fast (Testing)

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "DEFAULT_CONCURRENCY": 1,
    "MAX_TASK_ATTEMPTS": 1,
    "STUCK_TASK_TIMEOUT_SECONDS": 30,
}
```

## Django Tasks Configuration

Configure Django's native Tasks framework to use django-ray:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": [
            "default",
            "high-priority",
            "low-priority",
        ],
    },
}
```

## Environment Variables

These environment variables are read by the worker:

| Variable | Setting Equivalent |
|----------|-------------------|
| `RAY_ADDRESS` | `RAY_ADDRESS` |
| `DJANGO_RAY_QUEUE` | CLI `--queue` |
| `DJANGO_RAY_CONCURRENCY` | `DEFAULT_CONCURRENCY` |

## See Also

- [Configuration Guide](../configuration.md) - Usage guide
- [CLI Reference](cli.md) - Command-line options

