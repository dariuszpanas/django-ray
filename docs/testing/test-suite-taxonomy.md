# Test suite taxonomy

The pytest suite is classified by the resources and isolation each case requires. The checked-in
[manifest](https://github.com/dariuszpanas/django-ray/blob/main/.github/test-suite-taxonomy.json)
is the shared selection contract for source-fenced serial inventory evidence. Its current callers
are the blocking Python 3.12 `supported-python` job, `make test-suite-inventory`, and deliberate
serial resource-lane observations. It does not schedule ordinary pytest runs, control `test-xdist`,
or define marker-derived CI shards. Directory names still describe test intent, but they do not
determine whether a case can run concurrently.

The dated generated [baseline](../investigations/test-suite-baseline-2026-07-22.md) records exact
counts, large files and parameterized families, fixture usage, CI repetition, and candidate overlap
domains. Its JSON companion is available at
[`docs/investigations/test-suite-baseline-2026-07-22.json`](https://github.com/dariuszpanas/django-ray/blob/main/docs/investigations/test-suite-baseline-2026-07-22.json).
The baseline hashes the working contents of every tracked or nonignored Git-visible file, excluding
only generated dated baseline JSON and Markdown. The runner captures that digest before pytest and
rejects a run if it changes before pytest returns. Timing records therefore cannot be merged across
source states, and they retain Python, platform, processor, and test-toolchain identity.

The dated baseline is comparison evidence, not a claim that counts never change and not a new gate on
every test-only pull request. Regenerate artifacts for any tree being measured; add a new dated
snapshot when a later optimization decision needs a durable before/after reference.
The frozen 2026-07-22 snapshot records 33 `local-ray` cases. Current counts belong to
the checked-in manifest and a fresh generated inventory: the taxonomy distinguishes
required `local-ray` evidence from the capability-gated `compiled-graph-opt-in` case,
while the raw `real_ray` marker selects both groups.

## Reproduce the collection baseline

Collect and classify the suite without executing tests:

```bash
uv run make test-suite-inventory
```

The command writes JSON and Markdown under `artifacts/test-suite-inventory/`. Timing outputs inside
the repository must be ignored. Collection outputs may instead use the generated dated-baseline
path; other tracked outputs would invalidate their own source digest.

Use structured JSON to inspect any named selection, including fixture-aware execution contracts:

```bash
uv run python scripts/test_suite_inventory.py select \
  --lane hermetic \
  --format json
```

The JSON includes paths, marker rules, fixture rules, the diagnostic expression, owner, skip policy,
allowed Django settings identities, any equivalent native pytest arguments, and the manifest-runner
prefix. `--format shell` is available only when paths and marker expressions can represent the
selection exactly, and it describes selection only: it does not provision or configure the named
resources. Fixture-aware contracts must use the manifest runner so they cannot silently include
database cases.

The `run` command supports every execution contract, domain, product boundary, local profile, and CI
lane as serial, source-fenced evidence. Test-suite evidence schema 4 records selected and complete
pre-selection collection identities and exact per-test outcomes without the retired execution or
worker fields. This internal test-tooling schema is unrelated to django-ray's workflow-progress
schema versions. The selector is an importable pytest plugin loaded by the serial pytest process. It
remains inert during an ordinary pytest run and activates only when the manifest runner supplies an
explicit lane. Each observation and variant label is stable for grouping; a generated sample UUID
distinguishes repeated measurements:

```bash
uv run python scripts/test_suite_inventory.py run \
  --lane portable-local \
  --observation windows-local-py312 \
  --variant locked-dependencies \
  --timing-output artifacts/test-suite-inventory/local-timing.json \
  --external-note "uv environment was already synchronized; dependency setup excluded" \
  -- --cov=src --cov-report=term --cov-fail-under=95 -q
```

Merge one or more matching timing records into a generated inventory with repeated `--timing`
arguments:

```bash
uv run python scripts/test_suite_inventory.py collect \
  --timing artifacts/test-suite-inventory/local-timing.json \
  --json-output artifacts/test-suite-inventory/test-suite-inventory.json \
  --markdown-output artifacts/test-suite-inventory/test-suite-inventory.md
```

Do not claim unobserved environment or queue time as zero. Supply `--runner-queue-seconds` and
`--environment-setup-seconds` only when those external intervals were measured for the same run.
The runner rejects ambient pytest option/plugin overrides, selection-changing passthrough options,
non-finite numbers, collection/setup-only modes, incomplete outcome sets, forbidden skips, missing
timing phases, pytest failure, and source drift. The merge step revalidates all of those invariants
against a fresh collection. Collection baselines require an unset `DJANGO_SETTINGS_MODULE`; a named
PostgreSQL run permits and records only `tests.postgres_settings`. Timing evidence never records
database credentials.

Manifest-backed inventory evidence is deliberately serial. `uv run make test-xdist` is a separate
developer-speed path: it invokes ordinary pytest once with a configurable worker count, zero worker
restarts, and the external-resource marker exclusions. It does not load a taxonomy lane or produce
manifest timing evidence.

## Execution contracts

Each collected pytest case belongs to exactly one execution contract:

| Contract | Resource and manifest-evidence scheduling boundary | Selection ownership |
|---|---|---|
| `hermetic` | No Django database, local Ray runtime, PostgreSQL, or live cluster; serial in manifest evidence and eligible for ordinary local `test-xdist` | External markers and database fixture closure excluded |
| `sqlite-django` | pytest-django's default SQLite database; serial in manifest evidence and eligible for ordinary local `test-xdist` | Inherited/direct `django_db` or a database-owning fixture; external markers excluded |
| `local-ray` | Starts and stops required local Ray; always serial | Explicit `real_ray` without `compiled_graph_opt_in`; skips forbidden |
| `compiled-graph-opt-in` | Starts local Ray for the capability-gated native topology probe; always serial | Both `compiled_graph_opt_in` and `real_ray`; deliberate opt-in skip allowed |
| `postgresql` | Disposable PostgreSQL service; always serial | Explicit `postgresql`; dedicated evidence lane |
| `live-cluster` | Disposable two-node Ray cluster; always serial | Explicit `live_cluster`; dedicated opt-in lane |

The partition fails closed when a case matches no contract or more than one. Database ownership uses
the resolved fixture closure, including `db`, `transactional_db`, reset/rollback fixtures, Django
admin fixtures, and `live_server`; it does not rely on an explicit marker being present. Direct
parametrization pseudo-fixtures are removed from fixture counts, while genuine indirect fixtures are
retained.

Collection also enforces that `compiled_graph_opt_in` implies `real_ray`. This keeps the optional
capability probe out of in-process lanes while allowing required local-Ray evidence to remain
fail-closed.

A boundary such as `bundled-testproject` intentionally overlaps an execution contract: it answers
which product surface is being proven, while the execution contract answers what resources the case
consumes. `portable-local` is a measurement profile, not a product boundary or CI topology promise.

`tests/unit/` and `tests/integration/` remain useful code-organization boundaries. They are not
scheduling promises. Integration files can be hermetic when they mock transport boundaries, while
many unit-directory files use the SQLite database. We therefore reserve explicit markers for
resource or isolation behavior and rely on module/class inheritance where possible; adding a marker
to every test would create maintenance work without improving the contract.

## Runtime evidence model

The timing record separates intervals that have different owners:

| Interval | Meaning |
|---|---|
| Runner queue | Workflow job creation until a hosted runner starts; external to pytest |
| Environment setup | Checkout, Python setup, and dependency installation; external to pytest |
| Initialization | pytest startup before collection begins |
| Collection | Import, discovery, parametrization, marker classification, and lane selection |
| Test execution wall | Wall time after collection through the final test's log finish |
| Setup/call/teardown sums | Pytest report durations accumulated by phase |
| Post-test/coverage | Coverage aggregation and remaining pytest work through session finish |
| Terminal rendering | Terminal summary output, including a precomputed coverage report |
| Cleanup | Remaining framework/plugin shutdown after terminal reporting |

Timing sums are diagnostic work totals, while execution wall time is the speed metric. Test-suite
evidence schema 4 records them separately. Outcome counts and exact per-test records distinguish
passed, failed, skipped, expected-failed, and unexpected-passed cases. Every runnable group declares
whether skips are allowed; external-resource contracts and dedicated resource lanes forbid them,
except for the explicit `compiled-graph-opt-in` contract's capability-gated skip. The machine JSON
retains every fixture and parameterized family, while Markdown limits those tables for readability.
It also retains the slowest tests and files, making changes comparable without parsing `--durations`
output.

Two Linux GitHub Actions observations show why queue delay must not be attributed to the suite:

| Observation | Queue to Python 3.12 job | Job setup through dependency install | Pytest/coverage step | Separate coverage checks |
|---|---:|---:|---:|---:|
| [Main run after PR #163](https://github.com/dariuszpanas/django-ray/actions/runs/29961086153) | 2 s | 11 s | 248 s | 3 s |
| [PR #163 during the hosted-runner incident](https://github.com/dariuszpanas/django-ray/actions/runs/29960404522) | 301 s | 11 s | 248 s | 2 s |

Those older Actions observations deliberately preserve their coarse step boundary instead of
inventing unavailable precision. The blocking Python 3.12 job now uses the manifest runner, validates
its source digest, selected count, completed outcomes, pytest status, and skip policy, then uploads
any emitted timing diagnostics when the test step fails. Queue and environment setup remain unset
in that JSON because Actions metadata owns those intervals. The dated baseline records source-fenced
local observations and merges hosted Linux observations from exact-branch CI artifacts when
available. The absence of a hosted record explicitly means that evidence is pending. Merging it does
not change the source digest because dated baseline files are excluded from it.

The estimated CI total is selected pytest case slots multiplied by current lane variants. Selected
cases may later skip, so it is not labeled completed execution. The JavaScript subtests launched
from the bundled API suite are real gate work, but they are outside that selected-slot count rather
than disguised as additional pytest cases. Public CI does not run native Compiled Graph probes.

## Ownership and overlap review

The largest files and domain candidates have named owners in the generated baseline. “Candidate”
means compare scenario contracts and shared setup; it does not authorize deleting a test merely
because another layer uses similar inputs.

Issue #171 should be split into three focused domain reviews:

1. move workflow-progress protocol cases that only need a fixed run identity out of the database
   contract, and reduce read fixtures to the exact cursor/order cardinality;
2. share bundled-testproject authentication setup and remove only endpoint overlap proven to assert
   the same contract;
3. share one immutable KubeRay overlay render while retaining separate source-binding and topology
   assertions.

The bundled dashboard's nested Node suite remains a distinct boundary. One pytest case launches
multiple JavaScript subtests, so collected pytest cases alone understate its repeated CI work.
Preparation prototype and production subprocess tests look similar but prove different topologies;
do not consolidate them without a scheduled benchmark or replacement harness. Commit-policy
parameter tables are fast and preserve useful failure ownership, so they do not justify a cleanup
issue.
