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
only to the configured bucket and prefix. S3-compatible endpoints must honor the
conditional create and ETag-conditional delete requests used to prevent overwrite or
cleanup of a replaced content-addressed input.

### GCS

Set `INPUT_STORAGE_BACKEND` to `"gcs"` and configure
`INPUT_STORAGE_GCS_BUCKET`. `INPUT_STORAGE_GCS_PREFIX` defaults to
`django-ray/inputs`. Install `django-ray[gcs]` and scope credentials to the configured
bucket and prefix. Writes are create-only, and reads and deletes are pinned to one GCS
generation.

Digest-only storage is rejected for inputs because workers must retrieve the original
payload.

## Integrity and Execution

The stored envelope uses schema `django-ray.task-input`, currently at version `1`.
Workers validate the configured backend, bucket or filesystem root, object prefix,
SHA-256 digest, byte length, schema, version, field set, and canonical encoding before
importing or calling application code.

Reference grammar and namespace authorization complete before filesystem access, object-store
client construction, credential discovery, or provider calls. Scheme, authority, digest-derived
path, query field order, decimal byte count, and percent-encoding must be exact. Runtime readers
retain only the configuration-bound raw S3/GCS key encoding used by older shared storage writers;
the configured bucket and raw prefix must still select the exact digest-derived key. Malformed
references raise a stable bounded validation error without retaining raw query tokens or parser
exceptions in the durable task traceback.

Filesystem containment assumes `INPUT_STORAGE_FILESYSTEM_PATH` and its parent directory
tree are operator-controlled. It is not a sandbox against a process that can concurrently
replace digest directories with symlinks or Windows reparse points between validation and
I/O. Do not give untrusted task code or unrelated workloads write access to that tree; use
a dedicated mount and identity for shared durable-input storage.

Missing, malformed, unauthorized, or corrupt input references fail the execution
without running user code. Malformed, unauthorized, and integrity-validation failures
are non-retryable because another attempt would read the same invalid reference.
Retrieval/storage failures follow the configured retry policy because an object-store
or mount outage may be transient. Restore a missing object or correct the deployment
configuration before forcing a manual retry.

Ray Core passes only the reference to the executor. Ray Job uses transport version 2
for referenced inputs and does not place the raw argument payload in its command line.
Inline tasks retain the version 1 transport.

## Backend and Namespace Rotation

The active `INPUT_STORAGE_BACKEND` selects new writes. Reads and cleanup instead dispatch from
the validated reference scheme, so a different-scheme namespace can remain configured for queued,
running, retryable, and retained historical tasks after the writer changes. For example, after
switching new writes from S3 to GCS, retain the old S3 bucket, prefix, connection settings, and
credentials until no execution or registry tombstone needs that namespace.

Only one namespace can be configured per scheme. Treat an S3-to-S3 or GCS-to-GCS bucket/prefix
change as a data migration: pause spillover writes, copy every object byte-for-byte to its exact
digest-derived key, verify byte counts and SHA-256 digests, update execution and registry references
in one reviewed restartable migration, exercise representative reads, and only then switch settings.
For a filesystem-root change, copy and verify the complete digest tree before changing the root;
filesystem references do not encode which root created them. Rotating credentials without changing
the authorized namespace requires no reference rewrite, but both enqueue and worker identities must
have the required access before rollout.

Do not reuse a result-storage namespace for inputs. Input retention cleanup may delete an
object while a result still references the same content-addressed key. Startup rejects
identical input/result filesystem roots, S3 endpoint/bucket/prefix namespaces, and GCS
bucket/prefix namespaces. A shared object-store bucket is supported with distinct
prefixes, and separate explicit S3-compatible endpoints are separate namespaces;
filesystem storage requires distinct root directories. Different configured aliases to
the same physical storage remain an operator responsibility and must also stay disjoint.

An existing deployment that shares a namespace must complete the input-namespace
rotation above before starting upgraded web processes or workers. For object storage,
copy and verify the input objects and atomically rewrite both execution and registry
references; for filesystem storage, copy and verify the digest tree into a distinct input
root before switching that setting. Pause enqueue, execution, and input purge across the
cutover so an old process cannot recreate the unsafe shared ownership boundary.

Retain the exact old raw prefix for v0.2/v0.3 references containing percent-escape-like
text. A raw `%25` segment can also be parsed as the canonical encoding of a different
object key; the configured retained prefix is what resolves that ambiguity. Do not
change the prefix before the reviewed object-and-reference migration above.

Those releases stripped leading and trailing `/` characters from configured prefixes.
For example, `/prod/inputs/` wrote objects and references under `prod/inputs`; normalize
the setting to `prod/inputs` before starting 0.4.0 and verify representative queued and
retained payloads. Use the empty string, not `/`, for a prefix-free namespace. Internal
empty or single-dot segments, backslashes, and control characters are deliberately
unsupported by the 0.4.0 authority parser; parent segments were already unreadable and
require the same migration if an old writer created their objects. Migrate affected
objects while the old release can still read the readable cases: pause enqueue and purge,
copy and verify each payload into a canonical prefix, atomically rewrite execution and
registry references, change the setting, and exercise reads before upgrading.

## Retries and Retention

Automatic and manual retries reuse the execution row's immutable `input_reference`;
they do not upload a replacement payload. `TaskInputPayload` records content metadata,
last use, cleanup state, cleanup errors, and whether the object is a task-input envelope
or a Ray Job execution request. Multiple execution rows may safely share one
content-addressed object. The two payload kinds use separate execution columns;
wrong-kind or dual-column references remain retained rather than being guessed.

Inspect eligible payloads with the dry-run command:

```bash
python manage.py django_ray_purge_inputs --retention-days=30
```

Delete only after reviewing the report:

```bash
python manage.py django_ray_purge_inputs --retention-days=30 --delete
```

A payload is eligible only when its registry entry is old enough and every execution
referencing the kind's exact column is terminal with an old enough `finished_at`.
Cleanup locks the registry first and then all executions referencing either payload
column, so a concurrent writer cannot lose a shared payload and a cross-kind collision
fails closed. Successful cleanup keeps execution references and a `PURGED` tombstone for
audit. A future writer of the same kind and content may reactivate the object.
Every attachment/reactivation path must lock or register the payload before it locks and
updates the execution row. Cleanup safety depends on that common lock order; the dormant
`0021` schema does not authorize a request writer that bypasses it.

Command output identifies a reference only by a 16-character SHA-256 fingerprint. It
does not print the bucket, prefix, digest locator, provider exception text, or full
reference. `cleanup_error` retains only the bounded exception class so command logs and
database diagnostics cannot become a storage-URI or credential oracle.

Purging makes historical manual retry impossible until the same object is restored or
reactivated. Choose a retention window that covers the application's audit and manual
recovery requirements. The command never runs automatically.

## Rolling Upgrade

Migration `0021_ray_job_request_reference` is additive preparation for the bounded Ray
Job request transport. It gives `payload_kind` the Python and database default
`task_input`, so released writers that omit the column remain compatible. Applying it
does not enable request-reference submissions.

1. Apply the additive migrations while `MAX_INLINE_INPUT_SIZE_BYTES` remains `None`.
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
