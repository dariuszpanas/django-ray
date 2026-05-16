# Result Storage Reference

`django-ray` can keep small task results inline in `RayTaskExecution.result_data`
and move oversized results to `result_reference`.

## Size Threshold

`MAX_RESULT_SIZE_BYTES` controls when inline storage is used.

- result size <= threshold: stored in `result_data`
- result size > threshold: stored externally via configured backend and referenced by `result_reference`

## Backends

## `digest` (default)

- Setting: `"RESULT_STORAGE_BACKEND": "digest"`
- Behavior: stores no external payload, only a deterministic digest pointer.
- Reference format: `oversize://sha256/<digest>?bytes=<n>`

Use this when you only need metadata for oversized results and do not require retrieval.
`RayTaskBackend.get_result()` cannot reconstruct the original return value from digest-only references.

## `filesystem`

- Setting: `"RESULT_STORAGE_BACKEND": "filesystem"`
- Requires: `"RESULT_STORAGE_FILESYSTEM_PATH": "<path>"`
- Behavior: writes oversized JSON payloads to the configured directory and stores a pointer in
  `result_reference`.
- Reference format: `resultfs://sha256/<digest>?rel=<relative-path>&bytes=<n>`

For multi-worker deployments, this path should be a shared volume accessible by all workers
that need to read stored payloads.

## `s3`

- Setting: `"RESULT_STORAGE_BACKEND": "s3"`
- Requires: `"RESULT_STORAGE_S3_BUCKET": "<bucket>"`
- Optional:
  - `"RESULT_STORAGE_S3_PREFIX"` (default: `"django-ray/results"`)
  - `"RESULT_STORAGE_S3_REGION"`
  - `"RESULT_STORAGE_S3_ENDPOINT_URL"` (for S3-compatible providers)
- Behavior: writes oversized JSON payloads to object storage and stores a pointer in
  `result_reference`.
- Reference format: `s3://<bucket>/<key>?bytes=<n>`

Dependency:

- install `boto3` to use this backend.
- package extra: `pip install "django-ray[s3]"`.

## `gcs`

- Setting: `"RESULT_STORAGE_BACKEND": "gcs"`
- Requires: `"RESULT_STORAGE_GCS_BUCKET": "<bucket>"`
- Optional:
  - `"RESULT_STORAGE_GCS_PREFIX"` (default: `"django-ray/results"`)
- Behavior: writes oversized JSON payloads to GCS and stores a pointer in `result_reference`.
- Reference format: `gs://<bucket>/<key>?bytes=<n>`

Dependency:

- install `google-cloud-storage` to use this backend.
- package extra: `pip install "django-ray[gcs]"`.

If you need both cloud backends:

- package extra: `pip install "django-ray[object-storage]"`.

## Configuration Example

```python
DJANGO_RAY = {
    "MAX_RESULT_SIZE_BYTES": 1024 * 1024,  # 1MB
    "RESULT_STORAGE_BACKEND": "filesystem",
    "RESULT_STORAGE_FILESYSTEM_PATH": "/var/lib/django-ray/results",
}
```

```python
DJANGO_RAY = {
    "MAX_RESULT_SIZE_BYTES": 1024 * 1024,
    "RESULT_STORAGE_BACKEND": "s3",
    "RESULT_STORAGE_S3_BUCKET": "django-ray-results",
    "RESULT_STORAGE_S3_PREFIX": "prod/results",
    # Optional for S3-compatible providers:
    # "RESULT_STORAGE_S3_ENDPOINT_URL": "https://minio.internal:9000",
}
```

```python
DJANGO_RAY = {
    "MAX_RESULT_SIZE_BYTES": 1024 * 1024,
    "RESULT_STORAGE_BACKEND": "gcs",
    "RESULT_STORAGE_GCS_BUCKET": "django-ray-results",
    "RESULT_STORAGE_GCS_PREFIX": "prod/results",
}
```

## Retrieval Example (`filesystem`)

When `result_data` is empty and `result_reference` points at a retrievable backend
(`filesystem`, `s3`, or `gcs`), `RayTaskBackend.get_result()` will attempt to load and
decode the referenced payload automatically before exposing `TaskResult.return_value`.

This requires the reading process to have the same storage configuration available:

- `filesystem`: `RESULT_STORAGE_FILESYSTEM_PATH`
- `s3`: credentials plus optional endpoint/region settings
- `gcs`: application default credentials or equivalent GCS client auth

`digest` references remain metadata-only and do not hydrate `return_value`.

```python
import json
from django_ray.result_storage import FilesystemResultStorage

storage = FilesystemResultStorage("/var/lib/django-ray/results")
serialized = storage.load(reference=task.result_reference)
result_value = json.loads(serialized)
```

## Failure Behavior

If backend resolution/storage fails at runtime, worker execution remains successful and falls back
to digest-only references to avoid converting successful task execution into task failure.

If result loading fails later during `get_result()`, the task still appears successful but
`TaskResult.return_value` remains unavailable until the referenced payload can be read.
