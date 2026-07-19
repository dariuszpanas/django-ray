# Durable Input Storage

`django-ray` stores task arguments inline by default. An optional size threshold can
move an oversized combined argument envelope to durable storage and keep one
`input_reference` on the execution row.

## When to Use It

Use automatic spillover for JSON task inputs that are occasionally too large for
comfortable database rows but are still ordinary task arguments. Prefer an
application-owned S3, GCS, or database URI when the task consumes a large dataset,
dataframe, model artifact, or other independently managed object. Pass that URI as a
small task argument and let the application own its lifecycle and authorization.

Durable input storage does not add Python serialization and does not persist Ray
`ObjectRef` values. Arguments must remain JSON-serializable.

## Enable Spillover

Spillover is disabled when `MAX_INLINE_INPUT_SIZE_BYTES` is `None`, which is the
default. To enable it, configure a retrievable backend and a threshold from 1 KiB
through 100 MiB:

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head:10001",
    "MAX_INLINE_INPUT_SIZE_BYTES": 1024 * 1024,
    "INPUT_STORAGE_BACKEND": "filesystem",
    "INPUT_STORAGE_FILESYSTEM_PATH": "/var/lib/django-ray/inputs",
}
```

The threshold applies to the UTF-8 byte length of one canonical, versioned envelope
containing both positional and keyword arguments. Payloads at or below the threshold
remain in `args_json` and `kwargs_json`. Larger payloads are content-addressed and the
database fields contain JSON `null` placeholders plus `input_reference`.

## Backends

### Filesystem

Set `INPUT_STORAGE_BACKEND` to `"filesystem"` and configure
`INPUT_STORAGE_FILESYSTEM_PATH`. Every process that can enqueue, execute, inspect, or
reactivate a task must see the same shared path in a multi-host deployment.

### S3

Set `INPUT_STORAGE_BACKEND` to `"s3"` and configure
`INPUT_STORAGE_S3_BUCKET`. `INPUT_STORAGE_S3_PREFIX` defaults to
`django-ray/inputs`; region and S3-compatible endpoint settings are optional. Install
`django-ray[s3]` and give enqueueing and worker identities read/write/delete access
only to the configured bucket and prefix.

### GCS

Set `INPUT_STORAGE_BACKEND` to `"gcs"` and configure
`INPUT_STORAGE_GCS_BUCKET`. `INPUT_STORAGE_GCS_PREFIX` defaults to
`django-ray/inputs`. Install `django-ray[gcs]` and scope credentials to the configured
bucket and prefix.

Digest-only storage is rejected for inputs because workers must retrieve the original
payload.

## Integrity and Execution

The stored envelope uses schema `django-ray.task-input`, currently at version `1`.
Workers validate the configured backend, bucket or filesystem root, object prefix,
SHA-256 digest, byte length, schema, version, field set, and canonical encoding before
importing or calling application code.

Missing, malformed, unauthorized, or corrupt input references fail the execution
without running user code. Malformed, unauthorized, and integrity-validation failures
are non-retryable because another attempt would read the same invalid reference.
Retrieval/storage failures follow the configured retry policy because an object-store
or mount outage may be transient. Restore a missing object or correct the deployment
configuration before forcing a manual retry.

Ray Core passes only the reference to the executor. Ray Job uses transport version 2
for referenced inputs and does not place the raw argument payload in its command line.
Inline tasks retain the version 1 transport.

## Retries and Retention

Automatic and manual retries reuse the execution row's immutable `input_reference`;
they do not upload a replacement payload. `TaskInputPayload` records content metadata,
last use, cleanup state, and cleanup errors. Multiple execution rows may safely share
one content-addressed object.

Inspect eligible payloads with the dry-run command:

```bash
python manage.py django_ray_purge_inputs --retention-days=30
```

Delete only after reviewing the report:

```bash
python manage.py django_ray_purge_inputs --retention-days=30 --delete
```

A payload is eligible only when its registry entry is old enough and every referencing
execution is terminal with an old enough `finished_at`. Cleanup locks the registry and
execution rows so a concurrent enqueue cannot lose a shared payload. Successful cleanup
keeps execution references and a `PURGED` tombstone for audit. A future enqueue of the
same content reactivates the object.

Purging makes historical manual retry impossible until the same object is restored or
reactivated. Choose a retention window that covers the application's audit and manual
recovery requirements. The command never runs automatically.

## Rolling Upgrade

1. Apply the additive migration while `MAX_INLINE_INPUT_SIZE_BYTES` remains `None`.
2. Deploy the new code to every web, worker, and Ray runtime environment.
3. Drain or finish jobs started by old Ray Job drivers.
4. Enable a retrievable input backend and then set the threshold.

Existing inline rows need no rewrite. Do not enable spillover while old workers can
still claim tasks: they do not understand `input_reference` and will reject the JSON
`null` placeholders before application code runs. Before rolling back to an older
release, disable new spillover and drain all referenced tasks.

## See Also

- [Settings Reference](settings.md)
- [CLI Reference](cli.md#django_ray_purge_inputs)
- [Result Storage](result-storage.md)
- [Operator Runbook](../runbook.md)
