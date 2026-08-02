# Result Storage Reference

`django-ray` can keep small task results inline in `RayTaskExecution.result_data`
and move oversized results to `result_reference`.

The filesystem, S3, and GCS implementations also provide the internal load/delete
protocol used by [Durable Input Storage](input-storage.md). Input and result settings,
prefixes, references, retention rules, and failure semantics remain separate.

## Size Threshold

`MAX_RESULT_SIZE_BYTES` controls when inline storage is used.

- result size <= threshold: stored in `result_data`
- result size > threshold: stored externally via configured backend and referenced by `result_reference`

This is an inline threshold, not a maximum result size. Retrieval still materializes the
complete declared JSON payload in the Django process. Use these backends for moderate
JSON results; for datasets, model artifacts, or other large outputs, return a small
application-owned URI and give that object its own streaming, authorization, and
lifecycle contract.

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

The relative path is not arbitrary. It is exactly
`<digest[0:2]>/<digest[2:4]>/<digest>.json` under the configured root.

For multi-worker deployments, this path should be a shared volume accessible by all workers
that need to read stored payloads.

Writers first try an atomic no-replace hard-link installation. Filesystems that report
hard links as unsupported use a same-directory temporary file, a cooperative
digest-specific lock directory, and an atomic replace while holding that lock. Existing
digest objects are verified and never overwritten. A stale `*.install-lock` directory
means a writer died during installation; preserve the referenced object and audit
evidence, confirm that no writer is active, then remove only that digest's stale lock.

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

The key is exactly
`<configured-prefix>/<digest[0:2]>/<digest[2:4]>/<digest>.json`. The stored
bucket and prefix must match the reader's current S3 configuration.

Dependency:

- install `boto3` to use this backend.
- package extra: `pip install "django-ray[s3]"`.

S3 writes use `If-None-Match: *`; a digest key that already exists is read and
verified instead of overwritten. Cleanup first verifies the referenced bytes and
then deletes with the observed ETag as an `If-Match` precondition. S3-compatible
endpoints must implement these conditional `PutObject` and `DeleteObject`
semantics. An endpoint that ignores or rejects them is not a supported durable
storage target.

## `gcs`

- Setting: `"RESULT_STORAGE_BACKEND": "gcs"`
- Requires: `"RESULT_STORAGE_GCS_BUCKET": "<bucket>"`
- Optional:
  - `"RESULT_STORAGE_GCS_PREFIX"` (default: `"django-ray/results"`)
- Behavior: writes oversized JSON payloads to GCS and stores a pointer in `result_reference`.
- Reference format: `gs://<bucket>/<key>?bytes=<n>`

The key is exactly
`<configured-prefix>/<digest[0:2]>/<digest[2:4]>/<digest>.json`. The stored
bucket and prefix must match the reader's current GCS configuration.

Dependency:

- install `google-cloud-storage` to use this backend.
- package extra: `pip install "django-ray[gcs]"`.

GCS writes use `if_generation_match=0`; an existing digest object is read and
verified rather than overwritten. Reads pin the generation discovered with the
size metadata, and cleanup deletes only that verified generation. A concurrent
replacement therefore fails closed instead of being adopted or removed.

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

- `filesystem`: the same `RESULT_STORAGE_FILESYSTEM_PATH` contents
- `s3`: the same bucket and prefix, plus credentials and optional endpoint/region settings
- `gcs`: the same bucket and prefix, plus application default credentials or equivalent auth

`digest` references remain metadata-only and do not hydrate `return_value`.

```python
import json

from django_ray.models import RayTaskExecution
from django_ray.result_storage import FilesystemResultStorage


def load_raw_result(task_id: str) -> object:
    execution = RayTaskExecution.objects.get(task_id=task_id)
    if not execution.result_reference:
        return json.loads(execution.result_data)
    storage = FilesystemResultStorage("/var/lib/django-ray/results")
    serialized = storage.load(reference=execution.result_reference)
    return json.loads(serialized)
```

## Integrity and Authority Contract

Every newly generated reference is a canonical content-addressed locator. The canonical
validator rejects a reference unless all of these properties hold; runtime readers add
only the configuration-bound v0.2/v0.3 encoding compatibility described below:

- the scheme, authority, lowercase 64-character SHA-256 digest, and canonical decimal
  `bytes` value are exact;
- the query has only the expected fields, in canonical order, with no duplicates,
  blanks, fragments, user information, ports, traversal, or non-canonical encoding;
- a filesystem `rel` value is the exact digest-derived path under the configured root;
- an S3 or GCS authority equals the configured bucket and its decoded object key equals
  the configured prefix plus the digest-derived suffix.

Startup validation applies the same rules to the active writer and to configured
filesystem, S3, and GCS namespaces retained for historical reads. Object prefixes must
already be canonical (no leading/trailing slash, empty segment, traversal, backslash, or
control character), and the longest supported byte count must still fit the 500-character
database reference field. This makes an invalid active backend a startup error instead of
a late write failure that could otherwise produce a digest-only fallback.

Result and durable-input storage must not use the same content-addressed namespace.
Their retention policies differ: input cleanup is allowed to delete an object while a
result reference may still need the same bytes. Startup therefore rejects identical
filesystem roots, identical S3 endpoint/bucket/prefix namespaces, or identical GCS
bucket/prefix namespaces. A shared S3 or GCS bucket remains valid when inputs and
results use distinct prefixes; separate explicit S3-compatible endpoints are separate
namespaces; filesystem storage needs distinct root directories. The S3 signing region
does not distinguish a namespace. This check cannot detect two different paths or
provider aliases that resolve to the same underlying storage, so operators must keep
those aliases disjoint as well.

Before a read, filesystem stat size or the provider's S3/GCS object size must equal the
declared byte count. Each loader then requests at most `bytes + 1`, hashes the raw bytes,
and checks the exact byte count before strict UTF-8 decoding or returning the serialized
JSON. GCS pins that bounded download to the generation whose size was checked. A payload
with valid JSON but the wrong bytes is still corruption and is never surfaced as the task
return value.

Every backend refuses to overwrite an existing content-addressed object. Filesystem
writers verify and atomically reuse the existing file; S3 and GCS writers use provider
create-only preconditions and then verify a collision before reuse. S3/GCS cleanup reads
and verifies the exact payload first, then uses its ETag or generation as a delete
precondition. Corruption and concurrent replacement both fail closed.

The canonical signed-64-bit `bytes` field is the exact upper bound supplied to each
backend read, plus one byte for overrun detection. It is durable Django metadata, not a
provider-supplied allocation hint. Loading is intentionally not a streaming API and can
materialize as many bytes as the authorized reference declares; protect database write
access and use an application-owned artifact URI when that bound is too large for a web
or worker process.

Read, decode, and SDK failures raise a bounded `ResultStorageError`. Diagnostics do not
include the unrestricted reference, object contents, credential-bearing SDK exception,
or chained backend exception. The task's durable successful state is unchanged, but its
`TaskResult.return_value` remains unavailable until the exact referenced bytes can be
read and verified.

This contract detects accidental corruption or replacement relative to the metadata in
the Django database. It does not make an untrusted filesystem root or object namespace
confidential, prevent deletion, or authenticate an attacker who can rewrite both the
database reference and stored object. Restrict namespace and database write access,
enable provider versioning or backups where appropriate, and treat availability and
rollback protection as deployment responsibilities.

Filesystem containment assumes the configured directory tree is controlled by the
operator. It is not a sandbox against a process that can concurrently replace digest
directories with symlinks or Windows reparse points between validation and I/O. Do not
give untrusted task code or unrelated workloads write access to the result root or its
parents; use a dedicated mount and identity for shared storage.

## Configuration Rotation and Legacy References

Reference authorization uses the current scheme-specific settings, even when
`RESULT_STORAGE_BACKEND` now selects a different backend for new writes. Keep the old
scheme's settings and credentials only while its references remain readable; a stored
bucket or prefix is never trusted as configuration.

Plan namespace changes as data migrations, not ordinary credential rotations:

1. Drain result-producing workers or use another reviewed maintenance boundary.
2. Copy every retained object byte-for-byte to its digest-derived location in the new
   namespace and verify its declared byte count and SHA-256 digest.
3. In one controlled migration, update all current execution and archived-attempt
   `result_reference` values to the new canonical authority/key. Retain a rollback copy.
4. Deploy the new bucket/prefix settings, exercise representative historical reads, and
   remove old data or credentials only after the retention window and rollback period.

For a filesystem-root change, references do not encode the root. Copy the complete
digest tree and verify it before switching `RESULT_STORAGE_FILESYSTEM_PATH`; reference
rewrites are not required. Rotating credentials without changing the authorized
root/bucket/prefix does not change references, but the new identity must have read,
write, and cleanup permissions before rollout.

New references use canonical percent-encoding. django-ray 0.2 and 0.3 wrote S3/GCS keys
verbatim, so retained references whose configured prefixes contain spaces, Unicode,
`+`, or `%` can have a non-canonical encoding. Readers support only that encoding legacy:
the current bucket and raw configured prefix must select the exact digest-derived key.
They do not permit traversal, a different namespace, a reordered query, or any automatic
cross-namespace read. An upgraded task manager applies the same configuration-bound check
to a legacy reference in a durable completion envelope and stores its canonical encoding;
this validation does not construct an object-storage client or perform provider I/O.

The retained prefix is the authority for percent-escape-like legacy text. For example,
an old raw `%25` segment is syntactically indistinguishable from a new canonical segment
whose object key contains `%`. Preserve the old raw prefix while reading or canonicalizing
those rows, and canonicalize them before any namespace migration. Changing the prefix
first can select a different object key even though the reference string itself did not
change; the migration sequence above is mandatory for this reason.

The compatibility direction is reader-first: upgraded readers accept old raw references, but
v0.2/v0.3 readers treat a new percent-encoded path as the literal provider key and cannot hydrate
it when the configured prefix contains spaces, Unicode, `+`, or `%`. For such prefixes, pause
oversized-result writes, deploy the upgraded code to every web process, task manager, worker, and
Ray runtime that can read or write results, then resume writes. Do not allow an upgraded writer to
publish canonical references while an old reader still serves result retrieval.

The documented prefix type has always been `str`, but v0.2/v0.3 validation also admitted an
explicit `None`; those writers converted it to the literal object prefix `None/`. The current code
treats `None` as omission and selects the documented default. Before upgrading an affected
deployment, set the retained prefix explicitly to the string `"None"` and verify historical reads.
Later move those objects and references with the namespace-migration procedure above; do not switch
directly from explicit `None` to the default prefix and assume the old objects moved.

Those releases also stripped leading and trailing `/` characters from a configured
prefix. A setting such as `/prod/results/` therefore wrote objects and references under
`prod/results`; normalize the setting to `prod/results` before starting 0.4.0 and verify
representative historical reads. Use the empty string, not `/`, for an intentionally
prefix-free namespace.

Other previously admitted shapes are not safe compatibility inputs. Internal empty or
single-dot segments, backslashes, and control characters could create provider keys that
the 0.4.0 authority parser deliberately rejects; parent segments were already unreadable
and require the same migration if an old writer created their objects. If retained
objects use one of those prefixes, migrate while the old release can still read the
readable cases: pause writers and cleanup, copy and verify every object into a canonical
prefix, atomically rewrite current and archived-attempt references, change the setting,
and exercise reads before upgrading. Do not start 0.4.0 with the old prefix and expect
startup validation to normalize it.

After verifying that the current namespace still contains the exact object bytes, an
operator can rewrite an encoding-legacy result reference without moving the object:

```python
from django_ray.result_storage import canonicalize_result_reference

execution.result_reference = canonicalize_result_reference(execution.result_reference)
execution.save(update_fields=["result_reference"])
```

Perform this in a reviewed, restartable data migration and exercise representative reads
before removing the compatibility dependency. `is_valid_result_reference()` intentionally
recognizes only canonical references; use `canonicalize_result_reference()` with the
current settings for the bounded encoding-legacy migration path. References produced
under the unsupported historical prefix shapes above require the pre-upgrade object and
metadata migration; other manually created, modified, non-canonical, or
namespace-mismatched references fail closed.

## Corruption Incident Recovery

Treat an integrity failure as lost or replaced external data, not as metadata to adjust.
Pause writes to the affected namespace, preserve bounded provider and filesystem audit
evidence, quarantine the bad object, and restore the original exact bytes from a trusted
backup or reproducible source. Never change the stored digest or byte count to match an
unexpected payload: that would redefine a completed task's result. Validate repaired
historical reads before resuming producers. Keep credentials, raw references, and object
contents out of tickets and unrestricted logs.

## Failure Behavior

If backend resolution/storage fails at runtime, worker execution remains successful and falls back
to digest-only references to avoid converting successful task execution into task failure.

If result loading fails later during `get_result()`, the task still appears successful but
`TaskResult.return_value` remains unavailable until the referenced payload can be read and
passes the exact authority, byte-count, digest, and UTF-8 checks.
