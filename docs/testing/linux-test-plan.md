# Finite Linux regression stages

`scripts/linux_test_plan.py` exposes independently executable assertions for an
admitted Linux environment. An external caller owns admission, service setup,
stage ordering, first-failure abort, and resource release. Each invocation runs
one stage with a finite process-group deadline and bounded diagnostics, reusing
the [test suite taxonomy](test-suite-taxonomy.md) and its exact outcome receipts.
The runner never creates a container, cluster, database service, or scheduler.

Inspect the machine-readable catalogue before admitting work:

```bash
python scripts/linux_test_plan.py catalogue
# Equivalent: make linux-test-catalogue
```

The catalogue declares CPU, memory, PID, scratch, and time bounds for every stage.
The caller must enforce those resource bounds. Local Ray is serial and starts
only through the existing explicitly sized local test fixtures. The runner
rejects Windows execution and ambient external Ray configuration. `kubectl` is
required for local Kustomize contract rendering; these stages do not contact a
cluster. Provision the locked development environment, Node dependencies and
PostgreSQL driver before execution. Missing Node, make, Git or kubectl fails
before collection so module-level skips cannot silently shrink the inventory.

The two local-Ray stages request 3 CPU, 6 GiB RAM, 7 GiB scratch and 1,024 PIDs
for their assertion process tree, leaving room for an external runner's overhead.
Some fixtures declare four Ray logical scheduling slots; these do not require
four dedicated physical CPUs. Their assertions and timeouts remain unchanged.
The PID count is a caller-enforced limit. A platform that cannot enforce it has
not qualified this execution contract. Resource peaks and these budgets still
require measurement in the admitted Linux environment.

The PostgreSQL stage has a 20-minute deadline. Its covered one-CPU replay took
about 15 minutes, dominated by the 10,000- and 25,000-node workflow read fixtures.
The deadline leaves room for execution variance without omitting either retained
size or weakening the query, response-size, and coverage assertions.

| Stage | Assertions |
|---|---|
| `collection` | Exact collected metadata and proof that the resource lanes partition the current `supported-python` selection |
| `static` | Commit-policy fixtures, Ruff format/lint, types and current runtime advisory audit |
| `hermetic` | Fixture-aware tests without database or Ray access |
| `sqlite-django` | SQLite ORM and application integration |
| `local-ray` | Serial real Ray; skipped or xfailed required work fails |
| `compiled-graph-opt-in` | Existing explicitly optional capability probe; its skip remains visible |
| `postgresql` | All PostgreSQL-marked cases on a real disposable database; skips fail |
| `testproject` | Django system check, exact sample API/security/workflow rerun, including the Node browser contract; 80% sample coverage |
| `docs-build` | Strict clean documentation build and wheel/sdist build |
| `aggregate` | Complete same-source receipts, exact test partition and final 95% source, 90% worker and 90% Ray-job line coverage |

The non-live resource lanes are disjoint and together equal the current
interpreter's `supported-python` selection. Sample boundary tests intentionally
run again with their separate coverage target. The existing taxonomy skip
policies apply: portable lanes retain explicit optional capability skips with
per-test outcomes; PostgreSQL, required Ray and sample work forbid skips.
The sample stage uses SQLite. PostgreSQL-marked sample admission variants run
in the required PostgreSQL stage instead of being selected into a SQLite run
where they cannot execute. Both database variants remain required by the full
partition and aggregate.
Skipping or omitting a stage does not produce a successful aggregate.

This establishes the current-interpreter Linux regression baseline with real
PostgreSQL and the sample boundary. It does not establish the complete hosted
matrix or the complete application gate. The catalogue lists live-cluster,
application and hosted matrix evidence as separate required boundaries.
Supported Python/dependency variants, Compose, real Ray Data and installed-wheel
validation remain in their existing hosted lanes. Deployed application
assertions follow the [local gate trigger matrix](../deployment/local-kuberay-gate.md).

## Source and artifact contract

For immutable archives, seal the clean candidate before exporting its source:

```bash
python scripts/test_suite_source.py seal --output /evidence/source-manifest.json
```

Keep this manifest outside the source root. Its committed file identities are
verified before and after execution; a missing or changed source file fails.
The same manifest is passed to every stage and the aggregate. Git metadata is
not needed inside the archive. For a local Git checkout the runner can derive
the taxonomy source digest directly; that mode still fences changes during
execution and rejects mixed source receipts.

Run each admitted stage in a fresh output directory. This example runs one
stage; the caller releases its resources before admitting the next:

```bash
python scripts/linux_test_plan.py run --stage hermetic \
  --source-manifest /evidence/source-manifest.json \
  --output-dir /evidence/run/hermetic
```

For PostgreSQL, explicitly supply `DATABASE_HOST`, `DATABASE_NAME`,
`DATABASE_USER`, `DATABASE_PASSWORD` and a distinct `DATABASE_TEST_NAME` for the
disposable test database. The stage uses `tests.postgres_settings`; pytest owns
only its test database lifecycle. Credentials are never copied into receipts.

Every stage emits `receipt.json`, a bounded `stage.log`, and applicable
`timing.json.gz`, `collection.json.gz` or `coverage.data.gz` artifacts. The receipt
records source identity, stage contract, interpreter and package versions,
commands, outcome, cleanup/timeout evidence, artifact sizes and SHA-256 digests.
Uncompressed data remains local for investigation. Export only the receipt,
bounded log and compressed artifacts. Each compressed artifact is capped at
1 MiB, decompression at 64 MiB and the stage export at 3 MiB. Exceeding a bound
fails; truncated coverage or test selection is never accepted.

After all required stages pass, place their exported artifacts in directories
named for the stage IDs and run the aggregate on the same source/environment:

```bash
python scripts/linux_test_plan.py aggregate \
  --source-manifest /evidence/source-manifest.json \
  --evidence-dir /evidence/run \
  --output-dir /evidence/aggregate
```

The aggregate recollects the suite, verifies exact selection and completed
outcomes against the taxonomy, rejects duplicate/missing/stale receipts, checks
artifact digests, and combines only verified source line data. Paths are remapped
from the recorded source root, allowing stages to use different extraction
directories. The per-fragment zero coverage threshold defers enforcement to
this mandatory aggregate; it does not weaken any final floor. Source, worker
and Ray-job floors are enforced after combination; all three sample modules are
independently parsed and checked against their 80% floor again. The final compressed summary includes the exact
partition, outcomes and measured coverage.

## Portable test image

[`testing/linux/Dockerfile`](https://github.com/dariuszpanas/django-ray/blob/main/testing/linux/Dockerfile)
prepares the complete locked development, PostgreSQL and sample environment,
including Node/npm, Git, make and kubectl for local rendering. It starts no test
or service during preparation. Its default command describes the catalogue.
The production and development application images retain their existing roles.

Prepare a new external build directory from a clean committed candidate:

```bash
python scripts/test_suite_source.py seal --output "$BUNDLE/source-manifest.json"
git -c core.autocrlf=false archive --format=tar --output="$BUNDLE/source.tar" HEAD
```

The explicit Git setting preserves committed bytes even when the preparation
host uses automatic CRLF conversion. Extraction happens on Linux and preserves
executable modes. The image verifies every archived file before dependency
installation and again afterward. The source manifest remains outside the
source directory; the runtime creates a source-only Git index after
extraction solely for existing repository-sensitive assertions. It imports no
host history and does not change the sealed source identity.

The external bundle contains exactly four inputs: `source.tar`,
`source-manifest.json`, `build-constraints.txt`, and the Linux amd64 `kubectl`
binary. The Dockerfile uses the same kubectl v1.35.8 checksum as the existing
application qualification workflow. Prepare and verify that binary from
`https://dl.k8s.io/release/v1.35.8/bin/linux/amd64/kubectl` before the build.
Its required SHA-256 is
`874d5e72dbb819f43cff16bcd1e4f8bac5b7f2361fe1e55049b0a6c676fb0cbf`.

The build backend is not part of `uv.lock`. In the selected Linux Python/uv
preparation environment, export `pyproject.toml`'s `build-system.requires` to a
requirements input, resolve all its transitive dependencies with
`uv pip compile --generate-hashes --only-binary=:all:`, and retain the reviewed
result as `build-constraints.txt`. Every build requirement must be pinned with
hashes. Reuse that exact file for repeated builds. The image constrains both
editable installation and package builds, then warms the isolated backend
cache with `uv build --require-hashes`. It removes the earlier dependency cache
and caps the retained build cache at 128 MiB. Test dependencies remain selected
from the unchanged frozen lock.

Supply full digest references for a Python slim-trixie image, a Node image
matching `.node-version`, and a compatible uv image. Supply a fixed Debian
snapshot timestamp, such as the selected preparation snapshot's
`YYYYMMDDTHHMMSSZ` value. No floating image default or live Debian mirror is
used. The recipe uses the Python interpreter already in that base image;
the initial regression target uses the repository's Python 3.12 baseline.

```bash
docker build --platform linux/amd64 -f testing/linux/Dockerfile \
  --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
  --build-arg NODE_IMAGE="$NODE_IMAGE" \
  --build-arg UV_IMAGE="$UV_IMAGE" \
  --build-arg DEBIAN_SNAPSHOT="$DEBIAN_SNAPSHOT" \
  --tag "$TEST_IMAGE" "$BUNDLE"
```

Run that build only within the separately admitted preparation budget. Retain
the resulting image digest and `/opt/test-inputs/image-environment.json`, which
records input hashes, image references, observed tool versions and cache size.
Image creation is preparation evidence; it is not a test result.

At execution, mount new writable directories at `/workspace`, `/tmp` and the
chosen evidence path, owned by UID/GID 10001. A read-only image root is supported.
The entrypoint copies the verified source and installed Node modules from
`/opt/test-source` into the empty workspace, copies the prepared build cache
into `/tmp`, and invokes the existing stage runner with its sealed manifest:

```text
run --stage hermetic --output-dir /evidence/hermetic
aggregate --evidence-dir /evidence/stages --output-dir /evidence/aggregate
```

Use one fresh container and workspace per stage, enforcing its catalogue limits.
Use Docker's `--init` option, or an equivalent init process that reaps orphaned
children in the container's PID namespace. Some cleanup tests deliberately let a
launcher exit before its descendant. Without a reaper, a terminated descendant
can remain a zombie and keep its process group observable, causing the cleanup
assertion to fail. Do not skip that assertion or accept a failed cleanup receipt.
The virtual environment stays at `/opt/test-env`; its editable source path is
`/workspace/src`. Git remains available for tests that create their own small
fixture repositories. Neither Ray nor PostgreSQL starts through the entrypoint.
The `postgresql` stage connects to its separately admitted disposable service.

Dependency installation and Python downloads are disabled at execution. The
`static` stage still requires HTTPS access to current PyPI advisory data;
`UV_OFFLINE=1` does not disable that independent security check. A denied or
failed advisory request fails the stage. Docs and distribution builds use the
prepared cache. No Kubernetes credentials are needed for local rendering.

Keep exported compressed data within 2 MiB so receipts, bounded logs and an
executor's base evidence fit the existing 3 MiB total budget. A single artifact
remains capped at 1 MiB. Actual coverage/timing sizes, image size, memory peaks
and Linux execution remain unmeasured until the admitted run; preparation never
raises a transport limit or treats missing evidence as passing.
