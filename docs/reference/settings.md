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

## Startup Validation Policy

django-ray validates settings on app startup and fails fast on invalid config.
This happens in `django_ray.apps.DjangoRayConfig.ready()`.

Validation is skipped only when:

- running one of: `migrate`, `makemigrations`, `showmigrations`, `collectstatic`
- `DJANGO_RAY_SKIP_VALIDATION` is set to `1`, `true`, or `yes`

`DJANGO_RAY_SKIP_VALIDATION` is an environment override, not a `DJANGO_RAY` key.

## Ray Connection

### RAY_ADDRESS

- **Type**: `str | None`
- **Default**: `None`
- **Required**: Yes at runtime unless startup validation is explicitly skipped

Ray cluster address. Use `"auto"` for local development or `"ray://host:port"` for an explicit
cluster address. Sync mode does not submit work to Ray, but application startup validation still
requires this setting unless `DJANGO_RAY_SKIP_VALIDATION` is used for a maintenance/bootstrap flow.

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

## Runner Selection

### RUNNER

- **Type**: `str`
- **Default**: `"ray_job"`
- **Allowed**: `"ray_job"`, `"ray_core"`

Default runner selection when no execution mode CLI flag is provided:

- `ray_job`: use Ray Job Submission API mode.
- `ray_core`: use Ray Core mode:
  - if `RAY_ADDRESS == "auto"` -> local mode
  - otherwise -> cluster mode using `RAY_ADDRESS`

CLI flags (`--sync`, `--local`, `--cluster`) always take precedence.

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

List of exception class names that should not be retried. Supports short names
and fully qualified names.

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

This timeout is evaluated from `last_heartbeat_at` (falling back to `started_at`).
That heartbeat can come from the worker lease path or from active task-monitor updates
while a worker is still reconciling in-flight Ray work.

For persisted Ray Job handles from inactive workers, `django-ray` first attempts to
reconcile or adopt the existing job before the stale task is marked `LOST` and routed
through retry handling.

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

This controls lease freshness for worker coordination. Task monitor heartbeats for
actively reconciled in-flight work are updated separately.

```python
"WORKER_HEARTBEAT_SECONDS": 30
```

## Results

### MAX_RESULT_SIZE_BYTES

- **Type**: `int`
- **Default**: `1048576` (1 MB)

Maximum size of task results to store inline in `result_data`.
When exceeded, django-ray stores a compact pointer in `result_reference`
and leaves `result_data` empty according to `RESULT_STORAGE_BACKEND`.

```python
"MAX_RESULT_SIZE_BYTES": 10 * 1024 * 1024  # 10 MB
```

### RESULT_STORAGE_BACKEND

- **Type**: `str`
- **Default**: `"digest"`
- **Allowed**: `"digest"`, `"filesystem"`, `"s3"`, `"gcs"`

Backend used when result payload exceeds `MAX_RESULT_SIZE_BYTES`.

- `digest`: store a deterministic digest pointer only (no external payload persistence).
- `filesystem`: persist oversized payload to disk and store a reference pointer.
- `s3`: persist oversized payload to S3/object storage and store a `s3://...` reference pointer.
- `gcs`: persist oversized payload to Google Cloud Storage and store a `gs://...` reference pointer.

`RayTaskBackend.get_result()` can rehydrate oversized results from `filesystem`, `s3`,
and `gcs` references when the reading process has matching storage configuration and
credentials. `digest` references remain retrieval metadata only.

Install extras:

- `pip install "django-ray[s3]"` for S3 SDK dependency.
- `pip install "django-ray[gcs]"` for GCS SDK dependency.
- `pip install "django-ray[object-storage]"` for both.

```python
"RESULT_STORAGE_BACKEND": "filesystem"
```

### RESULT_STORAGE_FILESYSTEM_PATH

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `RESULT_STORAGE_BACKEND == "filesystem"`

Filesystem root used by the `filesystem` backend for oversized result payloads.
In multi-worker setups, use a shared volume if retrieval may happen on a different worker.

```python
"RESULT_STORAGE_FILESYSTEM_PATH": "/var/lib/django-ray/results"
```

### RESULT_STORAGE_S3_BUCKET

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `RESULT_STORAGE_BACKEND == "s3"`

S3 bucket name used for oversized result payload storage.

```python
"RESULT_STORAGE_S3_BUCKET": "my-django-ray-results"
```

### RESULT_STORAGE_S3_PREFIX

- **Type**: `str`
- **Default**: `"django-ray/results"`

Object key prefix used by S3 backend.

```python
"RESULT_STORAGE_S3_PREFIX": "prod/django-ray/results"
```

### RESULT_STORAGE_S3_REGION

- **Type**: `str | None`
- **Default**: `None`

Optional S3 region passed when creating the S3 client.

```python
"RESULT_STORAGE_S3_REGION": "us-east-1"
```

### RESULT_STORAGE_S3_ENDPOINT_URL

- **Type**: `str | None`
- **Default**: `None`

Optional endpoint URL for S3-compatible providers (for example MinIO).

```python
"RESULT_STORAGE_S3_ENDPOINT_URL": "https://minio.internal:9000"
```

### RESULT_STORAGE_GCS_BUCKET

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `RESULT_STORAGE_BACKEND == "gcs"`

Google Cloud Storage bucket used for oversized result payload storage.

```python
"RESULT_STORAGE_GCS_BUCKET": "my-django-ray-results"
```

### RESULT_STORAGE_GCS_PREFIX

- **Type**: `str`
- **Default**: `"django-ray/results"`

Object key prefix used by GCS backend.

```python
"RESULT_STORAGE_GCS_PREFIX": "prod/django-ray/results"
```

## Django Settings

These settings are configured directly in Django settings, not in `DJANGO_RAY`:

### RAY_DASHBOARD_URL

- **Type**: `str`
- **Default**: `"http://localhost:8265"`

URL of the Ray Dashboard. Used by Django Admin to generate deep links to tasks in the Ray Dashboard.
In the sample Kubernetes manifests this is set explicitly via environment/config:

- base NodePort manifests: `http://localhost:30265`
- Kong local overlay: `http://ray.localhost:30080`

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

`django-ray` itself reads the `DJANGO_RAY` Django setting. The sample project and Docker entrypoint
map these environment variables into settings or worker CLI flags:

| Variable | Used by | Equivalent |
|----------|---------|------------|
| `RAY_ADDRESS` | sample settings, Docker entrypoint | `DJANGO_RAY["RAY_ADDRESS"]` / cluster address |
| `RAY_DASHBOARD_URL` | sample settings | Django `RAY_DASHBOARD_URL` |
| `DJANGO_RAY_QUEUE` | Docker entrypoint | CLI `--queue` |
| `DJANGO_RAY_QUEUES` | Docker entrypoint | CLI `--queue` with comma-separated queues |
| `DJANGO_RAY_CONCURRENCY` | Docker entrypoint | CLI `--concurrency` |
| `DJANGO_RAY_SKIP_VALIDATION` | django-ray app config | Startup validation bypass |

## See Also

- [Configuration Guide](../configuration.md) - Usage guide
- [CLI Reference](cli.md) - Command-line options
- [Result Storage](result-storage.md) - Oversized result backend behavior

