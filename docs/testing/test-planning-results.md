# Advisory result measurements

Phase 4B adds an offline receipt normalizer and a separate measurement-record format.
They do not dispatch jobs, change required checks, or activate selective execution.
The [shadow guide](test-planning-shadow.md) describes normalization. YAGA's installed
`test evaluate` checks the normalized cells against a trusted plan and independent
policy, manifest, source, run, and aggregate-attempt identities.

The normalizer now supports the reviewed receipt contract at
`45180ea0e47aab600ed5b77312b636c7dfdb5282`. Required `Commit Messages`, `CI Gate`,
and `Qualification Gate` remain authoritative. This separate tooling change does
not inherit that accepted candidate's publication readiness or authorize a release.
Workflow adoption, measurement experiments and selective execution remain disabled.

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

### Evaluate with the reviewed implementation

Released `yaga-cli==0.2.0` does not contain `test evaluate`. Use the reviewed
phase-4B source in an isolated tool environment; the existing comparison workflow
keeps its phase-3 pin. Tool provisioning may require a download, but evaluation
itself reads only the supplied local artifacts:

```bash
uvx --from "yaga-cli @ git+https://github.com/dariuszpanas/yaga.git@3ad2ef919d83d82b917e5d7a6f4ab161970e959f" \
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

The format deliberately emits no savings estimate. A shorter selected catalog or
sum of excluded historical job times is not a measurement. Phase 4C must compare
same-source current and proposed execution with a compatible full failure oracle,
balanced arm order, fixed environments/cache policy, interference controls, and a
declared resource budget. Required jobs remain running. The owner chooses adoption
criteria after discovery and before activation; incomplete and slow observations
must remain in the cohort.

## Resource-free validation

Run the new unit files together with the existing shadow adapter tests in an
environment with pytest and the repository's ordinary test dependencies. These
tests use synthetic receipts and timestamps and start no Ray, Docker, or Kubernetes
resources. Consumer normalization does not add YAGA to package runtime dependencies;
offline evaluator integration uses the reviewed phase-4B revision above.
No workflow or YAGA pin is changed by this implementation.
