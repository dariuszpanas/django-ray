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
blocking, preserved matrices/cleanup and regression replay. Generic result
receipts and controlled activation are a later YAGA phase. Full-profile rollback
and consolidated full qualification remain mandatory.

Focused resource-free adapter tests cover fixed mappings, the PR/manual split,
source mismatch, malformed inputs, missing jobs, failures, cancellation and bounds.
Existing workflow tests retain complete native matrix and cleanup contracts.
Exact-head hosted Linux CI provides the full consumer checkpoint; no broad suite
or native Ray qualification runs on Windows. Deployment-independent planning
does not change the local KubeRay gate or shared infrastructure. Implementation
and historical selection evidence do not establish live shadow qualification.
