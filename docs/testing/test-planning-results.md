# Advisory result measurements

The receipt normalizer and measurement/report builders are resource-free APIs.
The manual shadow workflow uses them to observe one complete qualification run;
it does not change required checks or activate selective execution.
The [shadow guide](test-planning-shadow.md) describes normalization. YAGA's installed
`test evaluate` checks the normalized cells against a trusted plan and independent
policy, manifest, source, run, and aggregate-attempt identities.

The normalizer now supports the reviewed receipt contract at
`45180ea0e47aab600ed5b77312b636c7dfdb5282`. Required `Commit Messages`, `CI Gate`,
and `Qualification Gate` remain authoritative. This separate tooling change does
not inherit that accepted candidate's publication readiness or authorize a release.
Selective execution and adoption remain disabled. Manual full-run publication
observations are an initial measurement step, not the controlled independent-arm
experiment required for an adoption decision.

The first full hosted observation completed all eight qualification workloads,
but its result observer and aggregate failed. Complete offline replay passed;
the hosted failure's cause remains unconfirmed. Feedback timing and savings are
unavailable. [The reporting investigation](https://github.com/dariuszpanas/django-ray/issues/644)
now has bounded observer diagnostics and offline failure-path coverage. The original
failure remains unexplained and a fresh hosted confirmation remains outstanding.
Release evidence uses the existing standalone qualification workflows. Offline
replay does not replace failed hosted publications.

## Diagnose an incomplete observer

The observer retains `diagnostics.json` beside the existing `error.json` on a caught
failure. The existing upload step includes both files even when the observer exits
with status 1. The separate `test-shadow-diagnostics-version: 1` document is at most
8 KiB including its final newline. It has `authority: diagnostic-only`,
`activation: disabled`, and `status: incomplete`; it cannot satisfy a result,
publication, or required check. Successful publication formats are unchanged.

| Field | Meaning |
| --- | --- |
| `stage` | Fixed boundary: `context`, `planning`, `plan-validation`, `acquisition`, `evaluator-loading`, `normalization`, `evaluation`, `observation`, or `publication` |
| `arm`, `group` | Fixed current/proposed/full identifiers; `arm` is null before an individual arm is processed |
| `category`, `code` | Fixed failure classification; acquisition codes use an explicit allowlist, and unknown errors never publish their text |
| `run_id`, `attempt`, `source` | Syntactically validated caller-supplied context, with invalid values replaced by null; these fields do not authenticate the caller |
| `acquisition.request_count`, `response_count`, `response_bytes` | Attempted requests and complete bounded responses across all polls, including pending polls; counts saturate at the portable integer limit with `counters_saturated: true` |
| `acquisition.last_route_kind`, `last_object_id` | Fixed route category and bounded numeric run/job/artifact identifier, never a raw URL |
| `acquisition.last_response_bytes`, `last_response_sha256` | Last bounded transport response size and hash, reset before each request; a hash does not mean validation passed |

For example, `stage: acquisition`, `code: github-request-timeout`, and
`last_route_kind: job-log` distinguish a failed log request from receipt normalization
or evaluation. `stage: publication`, `code: io-failed` identifies local output failure,
not successful GitHub artifact upload. No raw exception message, response body, job
log, local path, credential, or redirect URL enters the diagnostic document. Original
workload evidence must still be retained and verified separately. An unwritable
output directory may prevent retention; the observer still exits nonzero and prints
only its fixed failure classification when failure occurs after directory creation.

Pending jobs and uploads keep their existing bounded polling behavior. Terminal
transport, identity, validation, and output failures are not retried. A failure in a
later alias may leave an earlier arm's files, but the final `publication.json` marker
is written only after all aliases complete. The collector still requires and
re-evaluates the full bundle; a diagnostic file cannot replace any missing payload.

Focused resource-free tests cover every observer stage, partial alias output,
unwritable diagnostics, hostile labels, transport limits, and publication rejection.
Retained-evidence replay can supply original API snapshots, upload logs and ZIPs
through an offline acquisition transport, then invoke the real normalizer and YAGA
evaluator. Keep original bytes immutable and inject faults only into replay inputs.
Such replay validates adapter behavior against historical evidence; it does not
recreate the original live failure or qualify another source revision.

## Retain one observation

`scripts.test_planning_experiment.build_observation(identities, measurements)` is a
pure, resource-free validator and report builder. `encode_observation(report)` emits
canonical UTF-8 JSON, bounded to 4 MiB including its newline. It neither reads files
nor queries a provider. Acquisition callers own bounded JSON reads, duplicate-key
rejection, authenticated provenance, and exact input/output artifact hashes.

The resulting `test-experiment-observation-version: 1` is independent of the frozen
plan, handoff, comparison, result, and evaluation schemas. It always reports
`activation: disabled`, `authority: advisory-measurement-only`, and `comparison: null`.
This schema validates recorded measurements; it does not authenticate evidence or
re-evaluate test obligations. First normalize receipts and evaluate them with YAGA,
then retain those exact artifacts with the observation.

### Evaluate with the released implementation

Released `yaga-cli==0.3.0` provides `test evaluate` in an isolated tool environment.
Tool provisioning may require a download, but evaluation itself reads only the
supplied local artifacts:

```bash
uvx --from yaga-cli==0.3.0 \
  yaga test evaluate --plan plan.json --results results.json \
  --policy-sha256 "$EXPECTED_POLICY_SHA256" \
  --bindings-sha256 "$EXPECTED_MANIFEST_SHA256" \
  --execution-commit "$EXPECTED_COMMIT" --execution-tree "$EXPECTED_TREE" \
  --run-id "$EXPECTED_RUN_ID" --attempt "$EXPECTED_ATTEMPT" --format json
```

Every expected identity comes from trusted acquisition context and the reviewed
plan, independently of the result file. Exit 0 means the plan's selected cells
passed; exit 1 means failed or incomplete, and exit 2 means invalid input. None
authorizes publication or changes a required gate.

### Observation fields

The closed `identities` object contains:

| Field | Meaning |
| --- | --- |
| `source` | Exact plan `base_commit`, `head_commit`, `comparison_commit`, `execution_commit`, `execution_tree`; comparison fields may all be null for a full revision plan |
| `adapter_source` | Reviewed normalizer/report adapter `execution_commit` and `execution_tree` |
| `policy_sha256`, `bindings_sha256`, `plan_digest` | Identities from the independently verified plan and manifest |
| `results_sha256` | Canonical result fingerprint from YAGA's evaluation report, excluding the final newline |
| `evaluation_sha256` | SHA-256 of the exact retained evaluation artifact bytes, including any newline |
| `run_id`, `attempt` | Expected aggregate run and attempt; no cross-run or cross-attempt assembly |

Digests are lowercase SHA-256. Source identities are same-format lowercase Git
object IDs. Identifiers have the portable run-ID grammar used by YAGA; attempts
are positive integers at most 2,147,483,647, never booleans. Matching a digest is
correlation, not authenticity. Retain raw artifact hashes separately below.

The closed `measurements` object contains:

| Field | Meaning |
| --- | --- |
| `experiment_id` | Bounded identifier shared by the planned comparison arms |
| `arm` | Exactly `current`, `proposed`, or `full` |
| `fingerprints` | `environment_sha256`, `images_sha256`, and `dependencies_sha256`; each digest or explicit null when unavailable |
| `cache_state` | `cold`, `warm`, `mixed`, or `unknown` |
| `timing` | The six timestamp fields below; each UTC timestamp or explicit null |
| `cells` | Complete observed obligations, including missing cells; at most 512 unique suite/variant pairs |
| `artifacts` | Up to 512 unique `{name, sha256}` records for original artifacts, normalized results, and evaluation |
| `incomplete_reason` | Explicit reason, at most 1,000 UTF-8 bytes, or null for complete observations |

Each cell contains `suite`, `variant`, `outcome`, `evidence_sha256`, and `attempts`.
Outcomes are `passed`, `failed`, `pending`, `cancelled`, `blocked`, or `missing`.
`evidence_sha256` is null or the normalized cell's evidence digest; passed cells
require a digest. Every non-missing cell retains 1–32 authorized job attempts, each
with `job_id`, `run_id`, `attempt`, positive `retry_ordinal`, `outcome`, and
`runner_seconds` (number or null). The job's authenticated actual execution run and
attempt must equal the observation's `run_id` and `attempt`. A successful row
copied into a later provider attempt does not establish execution in that attempt.
Retry ordinals are increasing adapter-assigned numbers for retries within one
aggregate attempt; they are not provider attempt numbers. Job IDs cannot be reused
within or across cells. Retain the
final authorized attempt last, and make its outcome match the cell. A failed retry
followed by a successful one retains both costs and outcomes; never choose the
greenest attempt. Missing cells have no attempts. Missing, pending, blocked, and
cancelled observations require `incomplete_reason`.

The retained release qualification history illustrates this distinction. Run
`39101458923` executed the four native cells and latency, cancellation and
monitoring in attempt 1, and the successful application job in attempt 2, on
`85da4256b04112454228d301f2c81a959876bff4`. These form separate observations and
plans for their actual attempt and selected cells. They cannot form one green
eight-cell attempt-2 result. Preserve the failed application attempt-1 history
separately; a successful retry does not prove its cause was repaired. The release
coordinator's explicit acceptance of that history and the identical tree at
`45180ea` does not relax YAGA's exact-source, single-attempt contract. Never relabel
the original receipts with the merged commit or this tooling change's source.

## Timing and resource accounting

Timestamps are UTC `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`; offset variants and invalid dates
are rejected. All fields are required, using null when a boundary was not observed:

| Boundary | Observation |
| --- | --- |
| `dispatched_at` | Trusted dispatch receipt, when available |
| `workflow_created_at` | Provider workflow creation time |
| `feedback_at` | Validated selected-result publication after assertions and cleanup |
| `candidate_ready_at` | Trusted candidate-ready event, never a Git author date |
| `required_ci_ready_at` | Existing required CI completion for this exact source |
| `first_failure_at` | First observed failure retained in this observation's retry lineage |

The report computes dispatch-to-creation, advisory feedback, developer feedback,
required-CI readiness, and first-failure intervals only when both boundaries are
available. Reversed or greater-than-31-day intervals fail validation. All-green
observations leave first failure null, not zero. Failed retries remain visible.
Per-attempt durations must be finite, nonnegative, and at most 31 days. Runner job
seconds include every recorded retry; the total is null if any attempt duration
or missing cell's resource cost is unknown, or an attempt is still pending or blocked.
Partial attempt durations remain visible without implying a complete total.
The count is not billing evidence.

The observation format deliberately emits no savings estimate. A shorter catalog
or sum of excluded historical job times is not a measurement. Independent experiments
must compare same-source current and proposed execution with a full failure oracle,
balanced arm order, fixed environments/cache policy, interference controls, and a
declared resource budget. Required jobs remain running. The owner chooses adoption
criteria after discovery and before activation; incomplete and slow observations
must remain in the cohort.

The primary benefit target is shorter pipeline feedback time. Reduced aggregate
runner usage is a separate benefit target: avoiding an unnecessary 30-minute job
saves those runner minutes even when another parallel job still determines pipeline
completion. Compare actual same-source current and proposed arms for both elapsed
feedback time and total runner minutes, counting setup, retries, cleanup and observer
overhead. Record runner classes and applicable billing multipliers separately when
translating usage into private-repository quota or cost. A shared full-run observation
has already consumed every workload's resources and cannot demonstrate those savings.

## Shared full-run publication feedback

The manual shadow workflow always executes all five qualification workflows and
all eight cells. Separate observers wait for the cells in the current required
selector, the proposed selection, and the full catalog. They authenticate actual
job attempts and original artifacts, normalize acceptance and cleanup receipts,
and evaluate each selection before publishing its result. Existing required CI
continues independently. This integration may propose the full catalog while
the current selector is empty: full-versus-proposed lead is then zero, and a
current-versus-proposed benefit comparison is unavailable.

An observer cannot embed its eventual upload completion time in the artifact it
is uploading. Its frozen observation therefore uses `feedback_at: null`. After
upload, the final collector authenticates the artifact ID, original ZIP SHA-256
and successful `Publish validated advisory feedback` step. That step's
`completed_at` is the conservative confirmed-publication boundary. Artifact
`created_at` remains provenance only: creation can precede upload and finalization.
Polling, acquisition, validation, evaluation and upload delays are included;
job completion times and sums of job durations are not substitutes. Identical
selections share one evaluation/publication, with each arm's own plan and
observation retained inside that artifact. Empty identical selections also share
one publication; neither case receives a fabricated speedup.

`scripts.test_planning_feedback.build_feedback_report(expected, publications)`
produces a separate `test-feedback-version: 1` report.
`encode_feedback_report(report)` emits canonical UTF-8 JSON bounded to 4 MiB
including its newline. Observation-v1 remains frozen with `comparison: null`.
Both functions are pure; acquisition callers establish authenticity and YAGA
evaluation first. A hash in a JSON object is not verification of either step.

The closed `expected` context contains exact `execution_commit`, `execution_tree`,
`adapter_source`, `run_id`, `attempt`, `bindings_sha256`, and `arms`. Each of the
three arms (`current`, `proposed`, `full`) contains independently verified
`policy_sha256`, `plan_digest`, and `selected: [{suite, variant}]`. The full arm
must contain all eight cells. Any native selection contains all four variants.
Proposed and full plans share the reviewed policy; the current selector's generated
policy has its own identity. All arms retain the same actual source, run, attempt,
execution manifest and shared-cell evidence.

Each `publications` entry is either null for unavailable evidence, or the closed
record `{observation, observation_sha256, publication_id, published_at,
publication_time_source, publication_sha256}`. The observation hash covers its exact canonical bytes,
including the newline. The publication hash covers the original provider ZIP.
`published_at` is the authenticated UTC upload-step completion timestamp, with
`publication_time_source: successful-upload-step-completion`. Both fields are null
when the boundary is unknown. Runner estimates and artifact creation timestamps
cannot substitute for this confirmed boundary.
Identical selections must name the same publication ID, timestamp and ZIP digest.
Cross-source or cross-attempt assembly, missing native variants, changed shared-cell
evidence and inconsistent policies fail with `FeedbackError`.

The report retains signed publication differences for current versus proposed,
full versus proposed, and full versus current. Positive seconds mean the selected
arm published earlier; negative seconds mean it published later. A validated lead
requires passing selected cells and a complete passing full oracle. Failed or
incomplete full cells block that claim. Excluded failures are listed for each
selection, including failed retries whose final outcome was passed. Retained pending,
blocked or cancelled retries make the oracle incomplete even after a passing retry.
Unavailable publications and timestamps remain null with explicit reasons. Comparing an empty
selection with a nonempty one also leaves the validated lead unavailable. Any
measured raw difference remains visible even when the oracle or comparator blocks
a validated result.

This shared execution measures earlier **advisory publication**, including its
observer overhead. It cannot establish selective-only runtime, runner-minute
savings, earlier merge readiness or independent-arm causality; those report fields
remain null. Required checks still govern merging, and all full-run resources were
consumed. The retained `runtime-costs.json` separately records observed qualification
and observer runner seconds, including observers' idle polling overhead. This is
additional runner usage that can incur charges; it is not billing evidence or saved
compute. Aggregate duration stays unknown while that job is still running.
Record a zero or negative result without choosing a different base after
seeing the outcome. Choose any later representative same-source revision and its
comparison base in advance. The owner chooses the benefit threshold after discovery
and before held-out confirmation or activation.

## Resource-free validation

Run the new unit files together with the existing shadow adapter tests in an
environment with pytest and the repository's ordinary test dependencies. These
tests use synthetic receipts and timestamps and start no Ray, Docker, or Kubernetes
resources. Consumer normalization does not add YAGA to package runtime dependencies;
offline evaluator integration uses the released version above. Feedback
tests exercise missing publications, exclusions, retry failures, identical aliases,
identity mismatch, signed lead and bounded output without running qualifications.
