# Configuration

django-ray is configured through the `DJANGO_RAY` setting in your Django settings file.

## Basic Configuration

```python
# settings.py
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
}
```

## All Settings

### Ray Connection

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `RAY_ADDRESS` | `str` | `None` | Ray cluster address. Use `"auto"` for local, or `"ray://host:port"` for cluster |
| `RAY_RUNTIME_ENV` | `dict` | `{}` | Ray runtime environment configuration |
| `RAY_STATE_API_ADDRESS` | `str \| None` | `None` | Ray dashboard URL used for live task state and log lookup |
| `RAY_STATE_API_TIMEOUT_SECONDS` | `int` | `5` | Timeout for optional Ray state and log queries |
| `RUNTIME_ENV_PROFILES` | `dict` | `{}` | Named, validated Ray RuntimeEnv definitions |
| `DEFAULT_RUNTIME_ENV_PROFILE` | `str \| None` | `None` | Profile used when a backend does not select one |
| `WORKFLOW_PLAN_CODE_REVISION` | `str \| None` | `None` | Immutable non-secret application build, artifact, or image revision; required for reusable-plan eligibility |
| `WORKFLOW_PLAN_TRUST_IDENTITY` | `dict` | `{}` | Bounded non-secret trust, credential-provider, and optional full-environment revision used to decide safe actor reuse |
| `RUNNER` | `str` | `"ray_job"` | Default runner when no mode flag is passed: `"ray_job"` or `"ray_core"` |

### Concurrency

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `DEFAULT_CONCURRENCY` | `int` | `10` | Maximum concurrent tasks per worker |

### Worker Polling

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `WORKER_POLL_INTERVAL_SECONDS` | `int \| float` | `0.1` | Base delay between claim queries (`0.01`-`10` seconds) |
| `WORKER_POLL_MAX_INTERVAL_SECONDS` | `int \| float` | `0.1` | Maximum idle claim delay (`0.01`-`60` seconds and not below the base) |

The default maximum equals the 100 ms base, preserving the existing polling cadence.
Set a larger maximum to opt into exponential idle backoff with bounded jitter, which
keeps multiple idle workers from repeatedly querying in lockstep. A claim, completion,
cancellation, timeout/recovery transition, or other lifecycle activity resets an
opted-in backoff immediately.

The maximum bounds how long an idle, available worker waits between observations of its
queue; it is not an end-to-end task-start guarantee. Heartbeats, Ray completion polling,
reconciliation, timeout checks, cancellation recovery, and lease cleanup use independent
monotonic schedules; idle claim backoff does not postpone them.

### Retry Policy

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `MAX_TASK_ATTEMPTS` | `int` | `3` | Maximum number of attempts before marking as failed |
| `RETRY_BACKOFF_SECONDS` | `int` | `60` | Base delay between retries (exponential backoff), from `0` to `3600` seconds |
| `RETRY_EXCEPTION_DENYLIST` | `list[str]` | `[]` | Exception types that should not be retried |

### Reliability

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `STUCK_TASK_TIMEOUT_SECONDS` | `int` | `300` | Time before a running task with no worker or monitor heartbeat is considered stuck |
| `WORKER_LEASE_SECONDS` | `int` | `60` | Worker lease duration (`1`-`86400` seconds) for distributed coordination |
| `WORKER_HEARTBEAT_SECONDS` | `int` | `15` | Heartbeat interval (`1`-`86400` seconds), which must be below the lease duration |
| `TASK_MONITOR_HEARTBEAT_SECONDS` | `int` | `15` | Minimum interval between database heartbeat writes for in-flight Ray Core tasks |
| `WORKFLOW_PROGRESS_REPORTING_POLICY` | `str` | `"full"` | Default Ray workflow node-reporting policy: `"full"` or `"disabled"` |
| `WORKFLOW_PROGRESS_FLUSH_SECONDS` | `int` | `1` | Minimum interval between full-mode workflow progress snapshot writes |
| `WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS` | `int` | `7` | Terminal workflow topology and node-detail retention (`0`-`30` days) |

`django-ray` validates numeric settings at startup, rejects booleans passed as integers, and enforces
that worker/task-monitor heartbeats are shorter than their lease/stuck-task windows.
`django-ray` uses worker lease heartbeats to track worker liveness and task monitor
heartbeats to show that a worker is still actively reconciling in-flight work. For
persisted Ray Job handles from inactive workers, another worker will first try to
reconcile or adopt the existing job before timeout-based stuck recovery marks it lost.
Task monitor heartbeats are batched into one update for all in-flight tasks and
throttled by `TASK_MONITOR_HEARTBEAT_SECONDS`.
Ray-native workflow node reporting defaults to `"full"`. Set
`WORKFLOW_PROGRESS_REPORTING_POLICY` to `"disabled"` when a workload needs the
durable outer task lifecycle without a workflow progress actor, node-reporting RPCs,
or `progress_data` writes. Calling
`WorkflowSignature.with_progress_reporting("disabled").run(...)` overrides the
setting for one invocation without reserving an application task keyword. Full mode
collects node events in memory and writes a snapshot no more often than
`WORKFLOW_PROGRESS_FLUSH_SECONDS`; the interval limits database write frequency, not
producer RPCs or actor memory.
Terminal topology and node detail become eligible for cleanup after
`WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS`; `0` makes them eligible as soon as the
terminal state is durably archived. Active current detail is not expired by this
setting, and bounded per-attempt summaries remain subject to task-attempt retention.

RuntimeEnv profiles are resolved and stored when a task is enqueued. See
[Runtime Environments](runtime-environments.md) for inheritance, backend aliases,
workflow leaf overrides, and cache behavior.

### Inputs

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `MAX_INLINE_INPUT_SIZE_BYTES` | `int \| None` | `None` | Combined input-envelope threshold; `None` disables spillover |
| `INPUT_STORAGE_BACKEND` | `str \| None` | `None` | Retrievable input backend: `"filesystem"`, `"s3"`, or `"gcs"` |
| `INPUT_STORAGE_FILESYSTEM_PATH` | `str \| None` | `None` | Shared root required for the filesystem backend |
| `INPUT_STORAGE_S3_BUCKET` | `str \| None` | `None` | Bucket required for the S3 backend |
| `INPUT_STORAGE_S3_PREFIX` | `str` | `"django-ray/inputs"` | S3 object-key prefix |
| `INPUT_STORAGE_S3_REGION` | `str \| None` | `None` | Optional S3 region |
| `INPUT_STORAGE_S3_ENDPOINT_URL` | `str \| None` | `None` | Optional S3-compatible endpoint |
| `INPUT_STORAGE_GCS_BUCKET` | `str \| None` | `None` | Bucket required for the GCS backend |
| `INPUT_STORAGE_GCS_PREFIX` | `str` | `"django-ray/inputs"` | GCS object-key prefix |

Spillover is opt-in. Configure a retrievable backend before setting the threshold;
digest-only storage is not valid for inputs. See
[Durable Input Storage](reference/input-storage.md) for rollout, retention, and backend
requirements.

### Results

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `MAX_RESULT_SIZE_BYTES` | `int` | `1048576` | Maximum inline result size in `result_data` (larger payloads use `result_reference`) |
| `RESULT_STORAGE_BACKEND` | `str` | `"digest"` | Oversized result backend: `"digest"`, `"filesystem"`, `"s3"`, or `"gcs"` |
| `RESULT_STORAGE_FILESYSTEM_PATH` | `str \| None` | `None` | Required when backend is `"filesystem"` |
| `RESULT_STORAGE_S3_BUCKET` | `str \| None` | `None` | Required when backend is `"s3"` |
| `RESULT_STORAGE_S3_PREFIX` | `str` | `"django-ray/results"` | Object key prefix for `"s3"` backend |
| `RESULT_STORAGE_S3_REGION` | `str \| None` | `None` | Optional S3 region |
| `RESULT_STORAGE_S3_ENDPOINT_URL` | `str \| None` | `None` | Optional S3-compatible endpoint |
| `RESULT_STORAGE_GCS_BUCKET` | `str \| None` | `None` | Required when backend is `"gcs"` |
| `RESULT_STORAGE_GCS_PREFIX` | `str` | `"django-ray/results"` | Object key prefix for `"gcs"` backend |

When `RESULT_STORAGE_BACKEND` is:

- `digest` (default): oversized payload is not persisted externally; a digest pointer is written.
- `filesystem`: oversized payload is written to the configured filesystem path and a pointer is written.
- `s3`: oversized payload is written to S3 (or compatible object storage) and a pointer is written.
- `gcs`: oversized payload is written to Google Cloud Storage and a pointer is written.

`RayTaskBackend.get_result()` can automatically reload oversized payloads from
`filesystem`, `s3`, and `gcs` references when the reading process has the same
storage configuration and credentials available. `digest` references remain
metadata-only and do not restore the original return value.

Optional install extras:

- `pip install "django-ray[s3]"` for S3 backend dependencies.
- `pip install "django-ray[gcs]"` for GCS backend dependencies.
- `pip install "django-ray[object-storage]"` for both.

### Redaction and operational output

`REDACT_PATTERNS` is an optional sequence of regular expressions used for
worker logs, structured log fields, Ray State API/log responses, the sample
operational API, and the Django admin task detail view. When it is `None`, the
built-in patterns cover common names such as `password`, `secret`, `token`,
`authorization`, `cookie`, and `private_key`. A matching mapping key redacts
its value; a matching string is replaced with `[REDACTED]`.

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "REDACT_PATTERNS": [
        r"password",
        r"access[_-]?token",
        r"customer[_-]?email",
    ],
}
```

Successful task logs contain only result type and serialized size, never the
complete return value. Ray Job completion envelopes are persisted through the
database channel and are not printed to Ray stdout. Redaction is a safety
boundary for operational output, not encryption: task results and arguments
remain in the database/result backend for authorized readers, and application
code that prints directly to stdout bypasses this policy. Protect the API,
admin, Ray dashboard, and result storage with the appropriate access controls.

## Startup Validation

django-ray validates `DJANGO_RAY` at Django app startup (`AppConfig.ready()`).
Invalid configuration raises `ImproperlyConfigured` by default (fail-fast).

Validation is skipped only in these cases:

- management command is one of: `migrate`, `makemigrations`, `showmigrations`, `collectstatic`
- environment variable `DJANGO_RAY_SKIP_VALIDATION` is set to `1`, `true`, or `yes`

The env override is intended for bootstrap/maintenance flows where full Ray config is
not available yet.

## Example Configurations

### Development

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 5,
    "MAX_TASK_ATTEMPTS": 1,  # Fail fast during development
    "STUCK_TASK_TIMEOUT_SECONDS": 60,
}
```

### Production

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 50,
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 120,
    "STUCK_TASK_TIMEOUT_SECONDS": 600,
    "WORKER_LEASE_SECONDS": 120,
    "WORKER_HEARTBEAT_SECONDS": 30,
    "TASK_MONITOR_HEARTBEAT_SECONDS": 15,
    "WORKER_POLL_INTERVAL_SECONDS": 0.1,
    "WORKER_POLL_MAX_INTERVAL_SECONDS": 0.5,
}
```

### High-Throughput Starting Point

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 100,
    "MAX_TASK_ATTEMPTS": 5,
    "RETRY_BACKOFF_SECONDS": 30,
}
```

## Environment Variables

`django-ray` reads runtime settings from Django's `DJANGO_RAY` setting. The sample project and
Docker entrypoint in this repository also map these environment variables into settings or worker
CLI flags:

| Variable | Used by | Description |
|----------|---------|-------------|
| `RAY_ADDRESS` | sample settings, Docker entrypoint | Ray cluster address (`"auto"` locally, `ray://...` for clusters) |
| `RAY_DASHBOARD_URL` | sample settings | Ray Dashboard URL used by Django admin deep links |
| `DJANGO_RAY_QUEUE` | Docker entrypoint | Queue name passed to `django_ray_worker --queue` |
| `DJANGO_RAY_QUEUES` | Docker entrypoint | Comma-separated queues passed to `django_ray_worker --queue`; takes precedence over `DJANGO_RAY_QUEUE` |
| `DJANGO_RAY_CONCURRENCY` | Docker entrypoint | Concurrency passed to `django_ray_worker --concurrency` |
| `DJANGO_RAY_SKIP_VALIDATION` | django-ray app config | Skip startup setting validation (maintenance/bootstrap only) |

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

`DEFAULT_CONCURRENCY=100` is not a universal recommendation. It controls how many
durable tasks one task manager can keep active; Ray resources, database capacity, and
external API limits may require a much lower value. See [Performance](performance.md).

Backend `OPTIONS` may select a named environment. This is a complete second alias:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"RUNTIME_ENV_PROFILE": "project"},
    },
    "numpy": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"RUNTIME_ENV_PROFILE": "numpy-2-3"},
    },
}
```

## See Also

- [Worker Modes](worker-modes.md) - How different modes affect configuration
- [Runtime Environments](runtime-environments.md) - Per-task dependencies and code
- [Durable Input Storage](reference/input-storage.md) - Oversized JSON input handling
- [Retry & Error Handling](retry.md) - Detailed retry configuration

