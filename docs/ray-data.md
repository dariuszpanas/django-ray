# Ray Data Batch Jobs

Ray Data fits django-ray best as one finite, coarse-grained task. Django owns the
durable task boundary; one outer Ray Job owns the distributed read, transform, and
write. The task result contains only bounded JSON metadata and artifact URIs. It never
contains a `Dataset`, `ObjectRef`, batch, row payload, or framework handle.

django-ray 0.4 ships an executable **application recipe**, not a generic Dataset API.
The implementation is in `testproject/apps/cluster_tasks/ray_data_job.py`, and the
outer task is in `testproject/apps/cluster_tasks/tasks.py`. Copy and adapt both in the
application that owns the data format, storage, model, authorization, and retention
policy.

For how this recipe fits alongside Ray Core, Jobs, Train, Tune, RLlib, Serve, and
Compiled Graph, see the [Ray Ecosystem Support and Install Matrix](ray-ecosystem.md).

## Supported Ray Data boundary

Install the Ray Data extra at the exact Ray version used by the cluster. The bundled
profile and blocking probe pin Ray 2.56.0:

```console
uv add "ray[data]==2.56.0"
```

django-ray 0.4 requires Ray 2.56 or newer, and this recipe must use that same or a
newer reviewed cluster version. Ray 2.56 is the first release that contains both the
[Parquet deserialization fix](https://github.com/ray-project/ray/security/advisories/GHSA-mw35-8rx3-xf9r)
and the
[WebDataset deserialization fix](https://github.com/advisories/GHSA-hhrp-gw25-jr43).
Build the pinned dependency into the Ray image or one reviewed RuntimeEnv shared by the
Ray Job driver and every Ray worker. A missing Ray Data or PyArrow dependency fails
before the recipe reserves an attempt directory.

The reference recipe accepts only absolute, credential-free `file://` URIs. On a
multi-node cluster, the configured paths must be mounted at the same absolute locations
in the Django producer, Ray Job driver, and every Ray worker that can read or write the
Dataset. A cloud/object-store implementation needs provider-native immutable object
versions and conditional-create semantics; changing the URI scheme alone is not
sufficient.

## Configure storage and routing

The input and output roots are server settings, not task arguments:

```console
DJANGO_RAY_DATA_INPUT_ROOT=/shared/ray-data-input
DJANGO_RAY_DATA_OUTPUT_ROOT=/shared/ray-data-artifacts
DJANGO_RAY_DATA_DEPLOYMENT_KEY=orders-production-us-west
```

All three values are copied into the bundled `ray-data` RuntimeEnv. Use absolute,
disjoint roots and provision both directories before starting workers; the recipe does
not create a missing configured root. Choose a stable deployment key that is unique for
every database lineage sharing the artifact volume. The task also includes the durable
task UUID in the namespace, so equal integer primary keys from two databases do not
collide.

Treat these mounts as a storage security boundary:

- Inputs must be immutable for the complete job. Prefer a read-only, versioned input
  mount or provider generation/version. Digest checks detect ordinary drift, but they
  are not a defense against a writer that swaps an input and restores it between
  checks.
- Only the trusted recipe and retention service may write the output root. A SHA-256
  digest detects later mutation for an honest reader; it does not authenticate which
  writer supplied bytes before the recipe inspected and published them.
- Do not overlap input and output roots. The recipe rejects paths outside the
  configured input root, symlinked parents, symlink roots, special files, and remote
  file authorities.

The bundled task is fixed to the `ray-data` backend alias and `ray-data` queue. That
alias declares `OPTIONS["RAY_JOB_ONLY"] = True`, so only a Ray Job mode management
worker may claim it:

```console
python manage.py django_ray_worker --queue ray-data --concurrency 1
```

Do not add `--local`, `--cluster`, or `--sync`; those select a different execution
mode and the worker rejects the queue. Ray Core and synchronous `--all-queues` workers
report and skip `ray-data`, with the same affinity check repeated at the durable claim
boundary. Start with one outer Ray Data job and increase concurrency only after
measuring cluster and storage pressure. The development Kubernetes overlay deliberately
excludes `ray-data` from its Ray Core all-ordinary-queues worker; an adopter must add the
Ray Job management worker only after mounting and proving the shared storage boundary.

## Enqueue one immutable input

Publish the input before enqueueing, then identify its immutable bytes:

```python
import hashlib
from pathlib import Path

from testproject.apps.cluster_tasks.tasks import ray_data_batch_score


input_path = Path("/shared/ray-data-input/orders-2026-08-01.jsonl")
enqueued = ray_data_batch_score.enqueue(
    input_uri=input_path.as_uri(),
    input_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
    run_key="orders-2026-08-01-score-v3",
    application_revision="git-7f30a1",
    model_revision="score-v3",
    scale=2.0,
    bias=1.0,
)
```

Hash large inputs while publishing them; the compact example reads the file only to
keep the snippet clear. `run_key` is a bounded correlation key, not business-level
deduplication. Enforce a unique business key in the application's enqueue transaction
if duplicate requests must collapse. Keep credentials, signed query strings, tenant
secrets, and sensitive path components out of persisted URIs.

## Execution and namespace

The driver performs this bounded sequence:

1. Validate short control fields, the canonical task UUID, configured roots, input
   path, and input digest. The sample caps input at 256 MiB.
2. Reserve this create-only namespace:

   ```text
   deployments/<deployment>/tasks/<task-uuid>/executions/<pk>/runs/<run-key>/g-<generation>/a-NNNN/
   ```

   The deployment key and task UUID separate database lineages; the execution primary
   key, monotonic generation, and attempt number fence one durable owner.
3. Read JSON Lines and run a side-effect-free callable class through
   [`Dataset.map_batches()`](https://docs.ray.io/en/latest/data/api/doc/ray.data.Dataset.map_batches.html).
   The recipe fixes one actor, 256-row NumPy batches, one CPU, zero-copy input, and
   `udf_modifying_row_count=False`. The explicit row-count contract avoids depending on
   Ray's changing default.
4. Write Parquet with `mode="error"` into the new `data/` directory. Before any
   Parquet parser sees a file, the driver enforces at most 128 directory entries, 64
   Parquet files, and 512 MiB. It then bounds rows to 10 million and schema fields to
   64, and rechecks the complete sorted path/size/content identity after metadata
   parsing. Symlinks, special files, non-Parquet sidecars, and changes during reads fail
   closed.
5. Recheck the input digest and publish `completion.json` from a fully flushed
   same-directory temporary file using create-only hard-link publication. A competing
   manifest is never overwritten.
6. Return at most 4 KiB of JSON containing the manifest/output URIs, their exact
   digests and counts, and the durable identity.

Ray Data execution is lazy; `write_parquet()` drives the transform. Ray documents that
[`write_parquet()` is not atomic](https://docs.ray.io/en/latest/data/api/doc/ray.data.Dataset.write_parquet.html).
Never infer completion from a `data/` directory or individual Parquet files.

The distributed transform module imports neither Django nor django-ray. Keep ORM use,
external side effects, secrets, application checkpoints, and registration writes out
of `map_batches()` workers. Choose the input before enqueueing and register the result
only after the durable task succeeds.

## Artifact completion is not task success

The manifest deliberately uses `"status": "artifact_complete"`. That means the
bounded Parquet output was inspected and the manifest was published. It does **not**
mean the matching `RayTaskExecution` committed `SUCCEEDED`.

There is an unavoidable cross-system fault window: the process can publish the
manifest and then fail, be cancelled, or lose its database success transaction. Such a
manifest is an orphan and must not be adopted. An authorized application service must:

1. read the canonical execution row;
2. require its current state to be exactly `SUCCEEDED`;
3. pass that row's task UUID, primary key, generation, and attempt together with the
   result to `validate_adoptable_artifact()`;
4. revalidate the manifest digest and current Parquet content before persisting an
   application reference.

The helper intentionally has no Django import. Its caller is responsible for row-level
authorization and for obtaining identity from the database rather than request data.
Do not adopt a manifest found by scanning storage, and do not treat a failed or
cancelled row as successful merely because its artifact is complete.

A completed empty Dataset has one explicit identity: zero files, rows, and bytes, an
empty schema, and the empty content digest. Missing output, a changed output, or an
attempt directory without `completion.json` fails closed.

## Atomicity, crash durability, and permissions

The reference publisher provides process-level, create-only publication on a
filesystem with working same-directory hard links. It fsyncs the temporary manifest,
but it does not fsync every output file or the parent directory after linking. It
therefore does not claim power-loss durability, database/filesystem atomicity, or
exactly-once output. An infrastructure operator must prove the selected filesystem's
close, hard-link, visibility, and directory-durability behavior. On a restart, absence
or mismatch remains non-adoptable.

On POSIX the manifest temporary file is set to mode `0640` before publication. Parquet
permissions still depend on Ray, the filesystem, and process umask. Configure a shared
group/default ACL and test the actual producer/reader/retention identities. On Windows,
access is inherited from the directory ACL; the recipe does not establish a Windows
ACL. Neither mode is a substitute for deployment authorization.

An object-store variant must use provider conditional-create plus immutable object
versions. An unconditional `PutObject` of `completion.json` is not equivalent.

## Retry, cancellation, and cleanup

| Event | Outcome |
|---|---|
| Same completed durable identity is replayed | Validate request, manifest, and current bounded output, then return the existing result without importing Ray Data or rewriting output. |
| A later generation or attempt starts | Recompute the complete Dataset in a new fenced namespace. Never append to or overwrite the old attempt. |
| The writer fails before manifest publication | Partial output can remain; the attempt is incomplete and never reused. |
| The writer fails after manifest publication but before durable DB success | Artifact is `artifact_complete` but orphaned; the `SUCCEEDED` adoption gate rejects it. |
| Ray retries internal stateless work | Recalculation is allowed only because the bundled transform is side-effect-free. This does not make arbitrary transforms exactly-once. |

Stopping the outer Ray Job is the cancellation mechanism. Cancellation can race with
Ray Data workers and storage writes. Prove that no writer remains before deleting an
incomplete attempt. Retention must delete only an exact owned attempt, preserve failed
artifacts for bounded diagnostics as policy requires, and remove a completed artifact
only after all durable application references expire.

## Real routed probe

Hermetic tests cover bounds, path controls, publication races, post-manifest failure,
adoption, and mutation without pretending mocks prove Ray integration. The blocking
probe runs a real two-CPU local Ray cluster with its dashboard/Job API, enqueues through
the public Django task, and starts a real management worker for only `ray-data` with no
Ray Core mode flag. Its first real `raysubmit_...` Ray Job publishes a manifest and
then triggers the bounded fixture failure. django-ray archives that failed attempt and
durably resubmits a second, distinct Ray Job, which succeeds:

```console
uv run --isolated --no-project --python 3.12 \
  --with-editable ".[sample]" \
  --with "ray[data]==2.56.0" \
  python scripts/ray_data_golden_path_probe.py
```

The isolated command supplies `ray[data]==2.56.0` as a prebuilt disposable node
environment. The probe records an empty task RuntimeEnv pip overlay instead of asking
Ray to create a redundant nested virtualenv; this also works with pip-less uv Python
installations on Windows. Before importing Ray, the probe disables Ray's automatic
`uv run` propagation so workers use that preinstalled interpreter instead of replaying
the outer editable-project command inside the deliberately minimal source archive.
It also configures one disposable filesystem input-storage root before Django setup, so
both real submissions exercise the rq2 request-reference carrier even though their
ordinary task arguments remain small.
Both Ray Jobs still use the persisted immutable source archive, RuntimeEnv environment
variables, outer Job driver, and real Ray Data workers. The probe pins the durable
backend target to the exact address of its new local cluster, so another developer
cluster cannot capture the work through `auto` discovery.

The probe requires two archived attempt outcomes and builds a bounded allowlist of rq2
submission IDs from the known durable execution primary key/public ID, archived attempt
protocols, and generation range. It matches JobInfo by ID before parsing metadata, then
checks the exact coordination/protocol/submission binding and the current persisted
request-reference hash plus embedded request digest/size. It never searches by public
identity metadata or retrieves request bytes from JobInfo. The released rq1 ID remains
accepted only as a drain-evidence candidate. The address-pinned, request-bounded client
polls until both submission identities have terminal states; an unrelated malformed job
is ignored rather than parsed. The probe does not depend on catching a short-lived
database state or allow an ambient Ray address to redirect the evidence read. Both Ray Jobs are
expected to be `SUCCEEDED` at the transport layer because the entrypoint exits
successfully after durably delivering either a success or failure completion envelope.
The authoritative application states remain the archived Django attempts: first
`FAILED`, then `SUCCEEDED`. The probe rejects adoption of the first orphaned artifact,
validates repeated read-only adoption of the successful artifact without rewriting its
manifest, checks attempt fences and namespace isolation, verifies exact Parquet rows
and bounded metadata, and rejects tampered output. Blocking CI runs the same Ray 2.56
probe on the supported-minimum Python 3.12 and newest Python 3.14 endpoints. This is
real routed local Ray evidence, not multi-node shared-storage proof. An adopter must
separately prove its mounts, permissions, pinned image, and failure behavior on the
intended cluster.

A Windows 11, Python 3.12, Ray 2.56 rehearsal on 2026-08-03 reached two correctly
pinned Ray Job submissions, but each Ray Data worker terminated with native exit
`0xC0000005` before the post-manifest application fixture. That is recorded as failing
platform evidence, not a product pass or a completed retry proof. The blocking Linux
probe remains authoritative for this recipe; adopters targeting Windows need their own
passing native evidence before enabling it.

A bounded Debian Bookworm container rehearsal on 2026-08-03 passed the complete proof
with Python 3.12.12 and Ray 2.56.0: django-ray archived the fixture failure, submitted a
second distinct Ray Job, completed the durable retry, and passed the adoption, fence,
namespace, bounded-result, and tamper checks. The container used an isolated network,
read-only source mount, and disposable storage; it did not exercise or mutate KubeRay.

## Deferred boundary

Selective partition resume requires immutable partition identities, per-partition
digests, independently published manifests, conflict rules, and retention. It remains
future work and still must not persist a Dataset, execution plan, ObjectRef, or worker
handle in Django.

Online request serving, streaming ingestion, training/tuning orchestration, and generic
cloud artifact adapters have different lifecycle and security boundaries. They are not
implied by this finite batch recipe.
