# Test suite taxonomy

The pytest suite is classified by the resources and isolation each case requires. The checked-in
[manifest](https://github.com/dariuszpanas/django-ray/blob/main/.github/test-suite-taxonomy.json)
is the shared contract for measurement, worker-loaded selection, the bounded xdist experiment, and
future CI lanes. Directory names still describe test intent, but they do not determine whether a
case can run concurrently.

The dated generated [baseline](../investigations/test-suite-baseline-2026-07-22.md) records exact
counts, large files and parameterized families, fixture usage, CI repetition, and candidate overlap
domains. Its JSON companion is available at
[`docs/investigations/test-suite-baseline-2026-07-22.json`](https://github.com/dariuszpanas/django-ray/blob/main/docs/investigations/test-suite-baseline-2026-07-22.json).
The baseline hashes the working contents of every tracked or nonignored Git-visible file, excluding
only generated dated baseline JSON and Markdown. The runner captures that digest before pytest and
rejects a run if it changes before pytest returns. Timing records therefore cannot be merged across
source states, and they retain Python, platform, processor, Django, Ray, coverage, pytest,
pytest-cov, pytest-django, and pytest-xdist identity.

The dated baseline is comparison evidence, not a claim that counts never change and not a new gate on
every test-only pull request. Regenerate artifacts for any tree being measured; add a new dated
snapshot when a later optimization decision needs a durable before/after reference.
The frozen 2026-07-22 snapshot records 33 `local-ray` cases. The post-#168 taxonomy instead assigns
22 cases to required `local-ray` evidence and one capability-gated case to
`compiled-graph-opt-in`; the raw `real_ray` marker still selects all 23.

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
lane. Schema 3 records whether a run is serial or xdist, the fixed execution policy, selected and
complete pre-selection collection identities, exact per-test outcomes, and worker collection
evidence. The selector is an importable pytest plugin loaded by the controller and every xdist
worker. It remains inert during an ordinary pytest run and activates only when the manifest runner
supplies an explicit lane. Each observation and variant label is stable for grouping; a generated
sample UUID makes repeated measurements of the same pair independently mergeable:

```bash
uv run python scripts/test_suite_inventory.py run \
  --lane portable-local \
  --execution serial \
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

Only `hermetic` may request xdist, and its manifest policy is exactly two workers with work stealing
and no restart:

```bash
uv run python scripts/test_suite_inventory.py run \
  --lane hermetic \
  --execution xdist \
  --observation local-hermetic-xdist \
  --variant locked-dependencies \
  --timing-output artifacts/test-suite-inventory/hermetic-xdist.json \
  --external-note "Queue and environment setup are outside pytest timing." \
  -- -q
```

The runner supplies `-n 2 --dist worksteal --max-worker-restart=0`; callers cannot replace those
arguments through pytest passthrough. It fails closed if a worker starts or restarts outside that
topology, applies a different configuration, or reports different selected or complete
pre-selection node IDs and fixture contracts.

## Execution contracts

Each collected pytest case belongs to exactly one execution contract:

| Contract | Resource and scheduling boundary | Selection ownership |
|---|---|---|
| `hermetic` | No Django database, local Ray runtime, PostgreSQL, or live cluster; serial by default and eligible for the fixed two-worker experiment | External markers and database fixture closure excluded |
| `sqlite-django` | pytest-django's default SQLite database; always serial | Inherited/direct `django_db` or a database-owning fixture; external markers excluded |
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

## Opt-in phased canonical coverage

`make test-cov-phased` is an evidence target for the bounded experiment. It is not the default
`test-cov` target and is not called by the blocking supported-Python matrix. Keep the single outer
environment boundary:

```bash
uv run make test-cov-phased
```

The default keeps the hermetic phase serial. To exercise the candidate topology, use a new ignored
output directory and select xdist explicitly:

```bash
uv run make test-cov-phased \
  TEST_SUITE_PHASED_OUTPUT_DIR=artifacts/test-suite-phased-coverage/xdist-sample \
  TEST_SUITE_HERMETIC_EXECUTION=xdist
```

Ray's Unix-domain sockets can exceed the platform path limit when hosted-runner artifact paths are
deep. The paired benchmark therefore gives Ray a short `/tmp` symlink through
`TEST_SUITE_RAY_TMP_DIR`. The resolved target must still be the output directory's exact
`ray-tmp` sibling; residue checks and deletion continue to use that canonical repository-owned
path. Local runs use the canonical path by default.

The output directory must be new or empty; stale evidence is rejected rather than overwritten. The
target executes four nonoverlapping phases that recreate the default-settings `supported-python`
selection:

1. `hermetic`, using either serial execution or the exact manifest-owned
   `-n 2 --dist worksteal --max-worker-restart=0` candidate;
2. `sqlite-django`, serially;
3. required `local-ray`, serially and with skips forbidden;
4. `default-serial-remainder`, serially, retaining the PostgreSQL and Compiled Graph cases as their
   intentional default-settings self-skips.

The remainder preserves the normal suite's collected outcomes; it is not PostgreSQL or native
Compiled Graph execution evidence. The dedicated PostgreSQL job owns its backend proof. Native
Compiled Graph proof belongs to the guarded local KubeRay pilot in issue #102, not a public hosted
workflow. `live-cluster` remains outside the normal supported-Python selection and keeps its
dedicated serial opt-in lane.

Coverage is erased once before the first phase. Every phase, including hermetic, uses
`--cov-append` against that proven-empty data path and does not enforce an intermediate floor. Only
after all four timing records merge successfully does the target enforce global 95%, worker-command
90%, and Ray Job runner 90% coverage, then write `coverage.xml` and line-level `coverage.json`. This
prevents a partial phase from passing or failing against a dataset that is not the canonical union.

Every phase retains the schema-3 source fence, collection identity, and exact test outcomes. The
merged inventory must contain all four timing records, prove that their node IDs do not overlap, and
match the current `supported-python` node-ID digest. Serial and xdist comparisons also require the
same statement and excluded-line sets, with every serial covered line still covered by xdist.

The target snapshots Ray processes, listening sockets, shared-memory objects, and global Ray
temporary entries before execution. That baseline must be a clean fresh-runner state. All phases use
the target-owned `ray-tmp` directory, and final validation rejects any new Ray process, listener,
shared-memory object, or unowned global temporary entry. It removes only that owned directory, and
only after the residue checks pass. Its entry-count diagnostic scans at most 10,001 entries and
records truncation instead of serializing or retaining an unbounded directory inventory. Truncation
or a bounded scan error does not block deletion: absence after removing the exact validated owned
root is the cleanup proof. External process, listener, shared-memory, or global-temp residue still
preserves that directory for diagnosis.
A Python guard runs the internal Make body and invokes cleanup exactly once even when a phase,
inventory merge, or coverage command fails. A primary failure keeps its exit status when cleanup
succeeds; a cleanup failure exits with the cleanup status and records both statuses in bounded
`ray-residue.json`. The resulting inventory, timing records, coverage files, and residue report stay
under the ignored output directory as diagnostic evidence.

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

Phase sums are diagnostic work totals and can overlap across xdist workers; execution wall time is
the speed metric. Schema 3 therefore records them separately, together with execution topology and
per-worker collection evidence. Outcome counts and exact per-test records distinguish passed,
failed, skipped, expected-failed, and unexpected-passed cases. Every runnable group declares whether
skips are allowed; external-resource contracts and dedicated resource lanes forbid them, except for
the explicit `compiled-graph-opt-in` contract's capability-gated skip. The machine JSON retains every
fixture and parameterized family, while Markdown limits those tables for readability. It also
retains the slowest tests and files, making changes comparable without parsing `--durations` output.

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

## Hosted paired benchmark and retention decision

The `CI` workflow exposes optional `workflow_dispatch` jobs for comparable Linux evidence. They are
not pull-request checks and do not replace the serial supported-Python matrix. Keep the selected ref
unchanged until the complete procedure finishes so every dispatch checks out the same commit.

Run three pair dispatches:

1. Select `xdist_benchmark_mode=pair`.
2. Give each run a unique nonempty `xdist_benchmark_sample`.
3. Alternate `xdist_benchmark_order` between `serial-xdist` and `xdist-serial`; the three retained
   runs must collectively contain both orders.
4. Wait for the `Optional pytest-xdist paired benchmark` job and retain its numeric workflow run ID.

The equivalent GitHub CLI calls are:

```bash
gh workflow run ci.yml --ref <unchanged-branch> \
  -f xdist_benchmark_mode=pair \
  -f xdist_benchmark_sample=linux-pair-1 \
  -f xdist_benchmark_order=serial-xdist

gh workflow run ci.yml --ref <unchanged-branch> \
  -f xdist_benchmark_mode=pair \
  -f xdist_benchmark_sample=linux-pair-2 \
  -f xdist_benchmark_order=xdist-serial

gh workflow run ci.yml --ref <unchanged-branch> \
  -f xdist_benchmark_mode=pair \
  -f xdist_benchmark_sample=linux-pair-3 \
  -f xdist_benchmark_order=serial-xdist
```

Each dispatch runs the serial and xdist canonical plans on the same fresh hosted runner and in the
requested order. It records the outer four-phase plan interval separately from pytest timing, then
compares exact phase selection, collection contracts, per-test outcomes, combined coverage, source
identity, environment identity, and Ray cleanup. The
`pytest-xdist-pair-<run-id>` artifact contains the pair report and both plans' bounded evidence for
14 days.

After all three pair jobs pass, dispatch the aggregate job from that same unchanged ref:

```bash
gh workflow run ci.yml --ref <unchanged-branch> \
  -f xdist_benchmark_mode=aggregate \
  -f xdist_benchmark_pair_run_ids=<run-id-1>,<run-id-2>,<run-id-3>
```

Aggregate mode requires exactly three distinct pair workflow run IDs, downloads their named
artifacts, and binds them to its own repository, exact `GITHUB_SHA`, full `HEAD^{tree}` Git tree SHA,
and source digest. Run it before the 14-day pair artifacts expire. The resulting
`pytest-xdist-retention-<run-id>-<run-attempt>` JSON and Markdown artifact is also retained for 14
days.

The aggregate fails as invalid evidence unless all three samples have distinct run IDs and labels,
the same repository commit, Git tree, and source digest, the same package/environment identity and
Linux runner-image OS family, both execution orders, identical canonical node outcomes, identical
combined coverage line sets, and valid residue evidence. Hosted runners may legitimately use
different image versions during one source-frozen sample set; aggregate schema 2 retains every
observed version as provenance instead of rejecting otherwise comparable fresh-runner evidence.
Each pair must also prove exact serial/xdist phase parity, the fixed zero-restart topology, no
incomplete or unexpected outcome, no source drift, no Ray residue, and xdist coverage equal to or
better than serial while preserving every floor.

Structurally valid evidence is eligible to retain bounded xdist only when both performance gates
pass:

- median hermetic pytest execution-wall time improves by at least 25% over serial;
- median wall time for the complete canonical plan does not regress.

Queue delay and dependency/environment setup remain external intervals and never count as pytest or
canonical-plan speed. The aggregate runs with `--require-retention`: it exits with status 3 when
valid evidence says to reject xdist, and with status 2 when evidence is incomplete or inconsistent.

A passing aggregate for the opt-in candidate is preliminary retention evidence, not authorization
to promote it. The blocking supported-Python jobs and the default `make test-cov` path remain serial,
and neither dispatch mode edits them. If this issue delivers only the harness, activation remains a
separately reviewed and gated change. Because changing the Makefile or workflow changes the source
identity, the proposed activation must already be present in one source-frozen candidate branch
commit used for all three pair runs and the authoritative aggregate. A retained aggregate from an
earlier groundwork commit cannot cross that source fence, and any later tracked tree edit requires
the complete procedure to be repeated. A rebase merge may change the commit SHA without changing
the evidence-bearing source: before promotion, verify that merged `main` preserves the candidate's
complete Git tree and source digest exactly. Until that final-source evidence passes both
performance gates, xdist stays opt-in and the serial gate remains authoritative.

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

Follow-up ownership is intentionally separated:

- issue #168 makes implicit Ray and external-resource ownership explicit;
- issue #169 benchmarks bounded xdist against these named contracts;
- issue #170 reduces duplicated supported-Python and CI-lane work;
- issue #171 consolidates only scenarios proven equivalent within a domain review.
