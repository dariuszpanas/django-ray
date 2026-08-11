# Durable Input Storage

`django-ray` stores task arguments inline by default. An optional size threshold can
move an oversized combined argument envelope to durable storage and keep one
`input_reference` on the execution row. Ray Job mode also stores every complete
canonical execution request in the same retrievable backend and keeps its separate
`ray_job_request_reference`; this outer rq2 carrier is mandatory even when the task's
arguments remain inline.

## When to Use It

Use automatic spillover for JSON task inputs that are occasionally too large for
comfortable database rows but are still ordinary task arguments. Prefer an
application-owned S3, GCS, or database URI when the task consumes a large dataset,
dataframe, model artifact, or other independently managed object. Pass that URI as a
small task argument and let the application own its lifecycle and authorization.

Durable input storage does not add Python serialization and does not persist Ray
`ObjectRef` values. Arguments must remain JSON-serializable.

Configure a retrievable backend before starting a Ray Job task manager. Synchronous and
Ray Core workers can remain storage-free while input spillover is disabled. A Ray Job
manager validates the backend before it creates a worker lease or claims work, so a
missing or malformed configuration cannot strand newly claimed tasks.

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
reactivate a referenced task must see the same shared path in a multi-host deployment.
For rq2, the task manager needs write access and each Ray Job driver needs read access.

### S3

Set `INPUT_STORAGE_BACKEND` to `"s3"` and configure
`INPUT_STORAGE_S3_BUCKET`. `INPUT_STORAGE_S3_PREFIX` defaults to
`django-ray/inputs`; region and S3-compatible endpoint settings are optional. Install
`django-ray[s3]` and give manager/cleanup identities the required write/delete access
and Ray Job drivers read access only to the configured bucket and prefix. S3-compatible endpoints must honor the
conditional create and ETag-conditional delete requests used to prevent overwrite or
cleanup of a replaced content-addressed input.

### GCS

Set `INPUT_STORAGE_BACKEND` to `"gcs"` and configure
`INPUT_STORAGE_GCS_BUCKET`. `INPUT_STORAGE_GCS_PREFIX` defaults to
`django-ray/inputs`. Install `django-ray[gcs]` and scope manager/cleanup and driver
identities to the configured bucket and prefix with only their required access. Writes
are create-only, and reads and deletes are pinned to one GCS generation.

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
Ordinary task-input retrieval/storage failures follow the configured retry policy when
an object-store or mount outage may be transient. Rq2 request preparation treats a
definitely pre-submission storage outage as retryable, but once Ray submission begins a
driver-side missing/unreadable request is not automatically replayed: reconcile or stop
the exact persisted job and prove it quiescent first.

Ray Core passes only an `input_reference` to the executor when argument spillover is
active. Ray Job rq2 always stores the *outer* canonical execution request and puts only
`--request-ref-b64 <bounded-locator>` in the process command. The stored request still
uses inner transport version `1` for inline JSON arguments or `2` for an opaque
`input_reference`. Neither form places application arguments or the callable path in
the rq2 command or metadata.

The locator is a strict, unpadded base64url encoding of bounded canonical JSON. It
contains the content-addressed reference, request digest and size, backend kind, and an
allowlisted non-secret filesystem root or object-store bucket/prefix/region/endpoint.
It is validated against independently bound metadata before storage I/O. Provider
credentials are never serialized into the locator, JobInfo, or process arguments; they
remain ambient workload identity or environment. The driver constructs the storage
reader directly from the locator and does not load Django settings, query the payload
registry, or import application code to discover the request.

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

An rq2 driver locator freezes the non-secret namespace coordinates needed for that one
request, but it does not make retention cleanup namespace-independent. The manager-side
registry still dispatches deletion through the configured per-scheme authority. Keep the
old namespace configuration and ambient credentials until its last execution reference
and tombstone is retired, and do not use the locator as a general storage migration tool.

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
they do not upload a replacement argument payload. Each freshly claimed Ray Job attempt
builds and stores its own exact request, while the prior job ID, address, and request
reference stay together until that claim or an explicit retry clears the old tuple.
Terminal and uncertain executions retain the reference for reconciliation and purge.
`TaskInputPayload` records content metadata,
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
updates the execution row. Cleanup safety depends on that common lock order; rq2
registration/attachment and its definite pre-submission release path follow it.

Command output identifies a reference only by a 16-character SHA-256 fingerprint. It
does not print the bucket, prefix, digest locator, provider exception text, or full
reference. `cleanup_error` retains only the bounded exception class so command logs and
database diagnostics cannot become a storage-URI or credential oracle.

Purging makes historical manual retry impossible until the same object is restored or
reactivated. Choose a retention window that covers the application's audit and manual
recovery requirements. The command never runs automatically.

## Rolling Upgrade

Migration `0021_ray_job_request_reference` is additive preparation for rq2. It gives
`payload_kind` the Python and database default `task_input`, so released writers that
omit the column remain compatible. Applying it alone does not change submissions.

1. Apply all additive migrations while old writers still run.
2. Configure one retrievable backend/namespace and ambient credentials reachable by the
   new task managers and Ray Job drivers. Keep argument spillover disabled if its own
   reader rollout is not complete.
3. Deploy the exact final rq2 reader everywhere that may reconcile or start a Ray Job.
4. Upgrade or disable every released/intermediate scheduled or manual
   `django_ray_purge_inputs` invocation. Older binaries understand neither
   `payload_kind` nor `ray_job_request_reference`; their dry run misreports an aged active
   request as unreferenced and `--delete` can remove it. Resume purge only from the exact
   final rq2 code, and revoke storage delete permission from retired runtime identities
   where practical.
5. Pause claims/producers as needed, retire every released 0.4.0 and intermediate rq1
   task-manager claimer, then close the existing legacy-admission latch with its reviewed
   revision and producer-retirement fence. Do not edit policy/token rows directly.
6. Resume Ray Job claims. Already submitted legacy and rq1 jobs can drain under upgraded
   reconciliation; all new submissions use rq2 while active protocol remains `1`.
7. Retain every old storage namespace and credential set until no queued/running task,
   retained request reference, or registry tombstone needs it.

Existing inline argument rows need no rewrite. Before rolling back to a manager that
cannot write rq2, stop new claims and reconcile or drain every rq2 job; do not clear a
request reference to manufacture replay safety. Keep old purge invocations disabled and
run retention from the exact final rq2 code until every retained rq2 request reference
and registry tombstone has expired; restoring an older binary sooner can destroy retry
and audit bytes it cannot see through the new reference column. Before rolling back input
spillover, disable new spillover and drain every task with `input_reference`.

## See Also

- [Settings Reference](settings.md)
- [CLI Reference](cli.md#django_ray_purge_inputs)
- [Result Storage](result-storage.md)
- [Operator Runbook](../runbook.md)
