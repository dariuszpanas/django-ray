# Test planning shadow pilot

This pilot compares a proposed YAGA selection with the existing five-workflow
qualification policy. **Selective execution is not activated.** `Commit Messages`,
`CI Gate`, and `Qualification Gate` remain required with their existing behavior.
The advisory `Test Planning Shadow` workflow does not replace any of them.

This catalog covers the five workflows selected by `scripts/qualification_policy.json`.
Its full plan does not cover all repository qualification: Transaction Qualification,
Upgrade Data Qualification, release-candidate artifact checks, and applicable local
deployment gates remain independent obligations with their existing triggers.

## Pull request comparison

`Shadow Plan Comparison` collects the complete explicit three-dot base-to-head
change through YAGA. It applies `.yaga/test-planning-shadow.toml` with the fixed
`development` profile and compares the existing selector over those same paths.
The baseline selector and policy come from the immutable adapter snapshot. This
is a reproducible policy comparison, not an observation of the authoritative
PR job's selection or results. Every baseline omission fails the advisory job;
additional obligations and full fallbacks remain visible.

The candidate checkout uses the exact PR head. A separate adapter checkout uses
`github.workflow_sha`, identifying the workflow definitions GitHub loaded. Both
commit and tree identities are recorded. The adapter's complete source tree pins
its workflow definitions, transitive scripts, tool/image definitions, configuration,
and lockfile. It is distinct from the execution tree during historical replay.

The fixed registry maps five IDs to existing workflows. Policy values never
become commands, executable paths, credentials, or generated workflow YAML.
Native qualification is one workflow call with all four serial matrix cells.
The PR route never calls qualification workflows: their existing per-PR
concurrency groups would otherwise cancel authoritative jobs.

### Reviewed application-test ownership

The advisory application rule names eight reviewed files individually under
`tests/unit/`: `test_application_chainsaw.py`, `test_application_core.py`,
`test_application_generation.py`, `test_application_result_presentation.py`,
`test_application_runtime_env_refusal.py`, `test_application_profile.py`,
`test_application_api_runner.py`, and `test_application_workflow_runner.py`.
An isolated change to one of these files proposes application qualification.
There is no application-test wildcard: new or otherwise unknown paths still
force the complete catalog, as does a change combined with `uv.lock` or another
full trigger. This ownership does not change the required selector or activate
selective execution. A smaller proposed plan is not measured feedback savings.

The shared `qualification/docker/scenario.py` helper is consumed by native,
latency, and cancellation qualification. Both the required selector and the
advisory policy select all three for an isolated helper change. This additive
dependency correction does not relax any existing trigger or native variant.

## Full same-source observation

After this workflow is registered on the default branch, manually dispatch
`test-planning-shadow.yml` on a reviewed source ref with explicit `base_commit`.
GitHub requires default-branch registration before dispatching a new workflow.
The run uses immutable `github.sha`; the base is only for selective comparison.
The manual route also creates an `integration` full plan for that exact
commit/tree and calls all five existing reusable workflows once.

| Fixed ID | Existing workflow | Required variants |
| --- | --- | --- |
| `application` | `application-qualification.yml` | `default` |
| `native` | `upgrade-native-qualification.yml` | `native`, `jobs`, `jobs-crash`, `core-loss` |
| `latency` | `latency-qualification.yml` | `default` |
| `cancellation` | `cancellation-qualification.yml` | `default` |
| `monitoring` | `monitoring-qualification.yml` | `default` |

The original exact-source Linux CI checkpoint, standard acceptance resource
profile, timeouts, serial native matrix, assertions, artifacts, and owned cleanup
remain in those workflows. Manual run IDs isolate concurrency from PR runs.
The always-running `Shadow Experiment Aggregate` rejects missing, failed,
cancelled, or skipped full-shadow jobs and mismatched commit/tree identities.
On PRs it instead requires all five execution jobs to remain skipped.

The aggregate records workflow completion, not authenticated per-cell receipts.
Retain the original workflow artifacts for exact image/source identities,
every native recipe, acceptance assertions and cleanup. A green aggregate or
eight counted cells cannot replace those records. Cancellation preventing the
aggregate from running never establishes completion.

## Local comparison and evidence

The workflow pins an immutable YAGA implementation commit in an isolated uv
environment; project dependencies and `uv.lock` are unchanged. That source commit
must be published before the consumer workflow can install it. Run the module
with that same dependency available, from the reviewed adapter checkout:

```bash
python -m scripts.test_planning_shadow plan \
  --repo /path/to/candidate \
  --policy .yaga/test-planning-shadow.toml \
  --baseline-policy scripts/qualification_policy.json \
  --range "$BASE...$HEAD" \
  --execution-commit "$HEAD" --execution-tree "$HEAD_TREE" \
  --adapter-commit "$ADAPTER" --adapter-tree "$ADAPTER_TREE" \
  --output /path/to/new-evidence-directory
```

Add `--full-shadow` to emit a full plan/handoff too; this local script still
does not execute tests or dispatch jobs. It writes bounded `comparison.json`,
`proposed-plan.json`, `proposed-handoff.json` and `executor-manifest.json`.
Full mode adds `full-plan.json` and `full-handoff.json`. Duplicate JSON keys and
oversized input/output fail. Successful workflow outputs contain only fixed keys
and validated identities; omissions retain evidence but publish no success output.

The versioned comparison records adapter/source identities, both policy hashes,
binding identity, plan digest, selected cells, baseline omissions/additions and
`planning_seconds`. This timing includes Git collection, planning, handoff validation
and baseline comparison; it excludes registry creation, environment/dependency
setup and artifact upload. Complete reasons remain in the proposed plan.
Runtime metrics are explicitly unobserved until real execution supplies evidence.

## Offline normalization of acquired receipts

`scripts.test_planning_results.normalize_results` converts authenticated,
source-matched qualification observations into YAGA `test-results-version: 1`.
It performs no network calls, Git inspection, test execution, workflow dispatch,
or writes. Existing required CI and the advisory workflow are unchanged; no
workflow installs or calls this normalizer yet. The existing YAGA workflow pin
is unchanged.

The API has two independent inputs:

- `ExpectedRun` contains the recomputed plan digest, exact policy-byte SHA-256,
  executor registry SHA-256, execution commit/tree, run ID/attempt, and selected
  `Cell(suite, variant, binding)` tuple. Derive these from trusted execution
  context and a validated handoff. Do not copy claims from result receipts.
- `VerifiedRun` contains the independently authenticated source/run/attempt,
  supported contract profile, `VerifiedJob` records, and `VerifiedFile` records.
  A job record contains the cell, unique numeric job ID, actual execution attempt,
  outcome and successful step names. The actual attempt must match `ExpectedRun`
  and `VerifiedRun`; a successful row copied into a later attempt is insufficient.
  A file record contains its artifact-relative POSIX path, byte
  count and SHA-256 derived from the original authenticated archive.

`VerifiedRun` is a Python caller contract, **not an authentication format**.
Before constructing it, a trusted acquisition adapter must verify the repository,
workflow path/event and immutable definitions, exact run and attempt, job-to-cell
mapping and conclusions, source commit/tree, and each artifact's API identity,
run/source association, size and SHA-256. Retain the original ZIP bytes, safely
extract them into a private directory, and derive the per-file inventory from
those bytes. Recheck current attempt identity; never select the greenest retry
or assemble results across attempts. A copied artifact filename, successful exit
code, caller-supplied constant, or green aggregate does not perform these checks.
This module deliberately has no parser that promotes an arbitrary metadata JSON
document into authenticated provenance.

The profile is `django-ray-45180ea-receipts-v1`, reviewed against the qualification
producers at `45180ea0e47aab600ed5b77312b636c7dfdb5282`. The profile identifies the
reviewed receipt contract, not a substitute source identity. The trusted caller
must establish that the executed source implements this exact contract. Unknown
or changed contracts are unsupported until reviewed. Both application cold
generations now require all 11 lease-policy and 20 runtime-environment-maintenance
facts. Native candidate crash/recovery cohorts also require the public stopped-
cohort lease-policy transitions before admission and after drain. Historical e4f
pilot receipts do not satisfy this newer contract.

The fixed mappings cover all five suites and eight cells:

| Suite | Required retained acceptance and cleanup |
| --- | --- |
| Application | Standard acceptance resource profile, exact source/image ancestry and producer identities; all 16 ordered receipts; setup, generic nodes, API/Admin/browser and workflow assertions; both cold generations and their byte-hash linkage; lease-policy and runtime-environment-maintenance facts in both Core receipts; worker-image modes and protected-log cleanup; namespace removal and successful outer infrastructure cleanup step. |
| Native | All selected `native`, `jobs`, `jobs-crash`, `core-loss` cells independently; exact candidate/released installed packages and archive digest; SQLite and PostgreSQL; every ordered phase, task/history/carrier observations, completion encoding, variant-specific crash/reconciliation/worker-mode and lease-policy-transition facts; fixture cleanup and successful outer cleanup step. |
| Latency | Source and installed module identity; the complete source-owned five-case timestamp, query, manager, outage and duration validator; stopped managers/Ray, fixture removal and outer cleanup. |
| Cancellation | Source and installed module identity; all 15 source-owned cancellation facts; stopped manager/children/PostgreSQL, fixture removal and outer cleanup. |
| Monitoring | Source/image ancestry and exact live image mapping; Ray topology, HTTP/authentication, dashboard replacement/replay, rendered browser/query evidence, operator/resources/private surfaces; browser, namespace, registry and disposable-cluster cleanup. |

The normalizer reuses the source-owned validators and checks additional retained
fields that the original log envelope parser does not validate. It imports only
the reviewed checkout's modules; it never imports or executes artifact contents.
The receipts cannot reproduce discarded live state, namespace ownership, image
layer observations or database history. Authenticated successful execution of the
exact supported producer remains a necessary part of the evidence boundary.
`complete_application_gate: false` and `complete_upgrade_gate: false` remain true
scope limitations; normalization never establishes whole-repository acceptance.

Every consumed file must match its independent byte count and SHA-256. Reads
reject links, junctions, nonregular files, changed identities, duplicate JSON
keys, nonfinite numbers and excessive nesting. Bounds are 512 inventory entries,
1 MiB per file, 16 MiB of unique consumed bytes, 50,000 JSON values and depth 32.
The extraction directory must remain private throughout normalization.

For a passed job, missing custom evidence, wrong source, malformed assertions or
unconfirmed cleanup raises `NormalizationError`, an operational input error.
It does not fabricate a failed receipt or preserve passed status. Independently
established `failed`, `pending`, `cancelled` and `blocked` jobs retain that outcome;
the normalizer does not upgrade them by reading a leftover green receipt. Missing,
duplicate, extra or ambiguously mapped jobs also fail. Each output cell includes
an evidence SHA-256 binding its used-file hashes, exact job, source, run/attempt
and contract profile.

The local integration sequence is:

```python
from scripts.test_planning_results import normalize_results

# expected and verified are constructed by the trusted adapter described above.
document = normalize_results(artifact_root, expected=expected, provenance=verified)
```

Then parse the output with YAGA's results parser and evaluate it against the
original validated plan and independently expected policy, registry, source,
run and attempt. The evaluator owns complete selected-variant and prerequisite
coverage; normalizing one native cell does not satisfy the other three. Serialize
the resulting artifacts and record their exact digests in the separate
[offline measurement report](test-planning-results.md). Timing/report generation
does not validate receipts or authorize activation.

Resource-free tests use bounded historical receipt projections and synthetic
acquisition contexts. They cover all eight cells, corrupted acceptance and
cleanup facts, source/attempt mismatch, invalid metadata, file substitution and
parser budgets. Those test fixtures do not authenticate a new run or replace
current exact-source hosted qualification.

The retained release receipts keep their original `85da4256` source and actual
execution attempts: seven native/latency/cancellation/monitoring cells in attempt 1
and application in attempt 2 of run `39101458923`. Normalize and evaluate those
separate selections independently. The release coordinator's reviewed identical-
tree acceptance at `45180ea` does not permit this API to assemble them into a full
green attempt-2 result. It also does not grant this new tooling source publication
readiness. Required `Commit Messages`, `CI Gate`, and `Qualification Gate` remain
authoritative; no publication is authorized by this tooling.

## Promotion remains disabled

Start with at least 30 representative revisions across backend, assets,
infrastructure, dependencies, tests/configuration and cross-component work.
Historical comparison alone does not complete the runtime pilot. Retain full
same-source observations and controlled selected replays, including excluded
failures, setup/cleanup, cancellations, runner minutes and critical-path wall time.
Do not sum parallel durations as wall time or infer speedup from test counts.

The owner must choose a useful measured feedback-time improvement before
activation; no threshold is presumed. Promotion also requires no unexplained
omissions or known missed regressions, tested invalid/missing/cancelled-result
blocking, preserved matrices/cleanup and regression replay. The offline receipt
normalizer is implemented separately; workflow adoption and controlled activation
remain pending. Full-profile rollback
and consolidated full qualification remain mandatory.

Focused resource-free adapter tests cover fixed mappings, the PR/manual split,
source mismatch, malformed inputs, missing jobs, failures, cancellation and bounds.
Existing workflow tests retain complete native matrix and cleanup contracts.
Exact-head hosted Linux CI provides the full consumer checkpoint; no broad suite
or native Ray qualification runs on Windows. Deployment-independent planning
does not change the local KubeRay gate or shared infrastructure. Implementation
and historical selection evidence do not establish live shadow qualification.
