# ADR-0008: Direct Qualification Witnesses and Proposed Evidence Reuse

## Tracking

| Field | Value |
| --- | --- |
| Decision status | Proposed |
| Implementation status | Partial |
| Owners | `django-ray` maintainers |
| Proposed | 2026-11-16 |
| Accepted | Not accepted |
| Decision review | Pending the implementing PR review; no reuse decision accepted |
| Delivery tracking | [#469](https://github.com/dariuszpanas/django-ray/issues/469) |
| Supersedes | None |
| Superseded by | None |
| Last verified | 2026-11-16, candidate layered on worker maintenance #726 and sample API input handling #730; local producer/workflow checks; source-specific hosted validation is tracked in the delivery PR and #469 |

## Context and scope

Commit-message amendments can retain exactly the same source tree while causing
another complete deployed qualification. Existing policy permits a reviewer to
carry unchanged-tree evidence, but the hosted recipes still execute. Their
artifacts identify tested source and images without a separate bounded statement
of direct execution, its attempt and the original upload identity.

This first delivery records that statement for Application qualification only.
It does not select, authenticate, accept or reuse old results. All existing
current-head CI, selected qualification, review and merge requirements remain.
Other recipes and the broader resource/cost requirements in #469 remain open.

## Decision

Propose a provenance-first sequence. Record direct execution before designing
any reuse consumer. The current producer uses only the standard library and
reads a fixed whitelist of context fields and 25 known receipt/build files.
It publishes:

- Repository, actor, PR author/base/head repository IDs, base/head/source commits,
  run and attempt, event SHA, caller workflow path and workflow SHA.
- Exported source tree and archive digest, selected profile/intent, observed
  tested base/application image digests and summary source-tree agreement.
- Fixed checkout, profile, CI, tools, build, cluster, native, cleanup and receipt
  upload outcomes; the upload step's artifact ID, digest and existing name.
- A path, byte count and SHA-256 for each available whitelisted file, a digest
  of the effective qualification profile, and optional runner image observations.
- Fixed `direct_execution`, `self_authenticating=false`, `reuse_enabled=false`
  and `complete_application_gate=false` boundaries.

Workflow path/SHA describe the caller's
[GitHub context](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#github-context)
when this recipe is called; they do not attest the called workflow independently.
Manual branch and tag refs remain supported; PR records require their exact
`refs/pull/<number>/merge` context. Linux/X64/GitHub-hosted fields use the
[documented runner defaults](https://docs.github.com/en/actions/reference/workflows-and-actions/variables#default-environment-variables).
`ImageOS` and `ImageVersion` are optional image-environment observations, recorded
as unavailable when absent, rather than a trusted platform-material inventory.

The producer runs after native execution, owned cleanup and receipt upload.
Two additional one-minute steps have their own two-minute outer-job allowance;
no workload, checkpoint or cleanup deadline is shortened. Missing or invalid
facts and unsuccessful steps produce a partial record with fixed failure codes.
No raw event body, exception text, settings, credentials, image configuration
environment, task payloads or unbounded logs are copied into the witness.

Input limits are 1 MiB for the GitHub event, 256 KiB per whitelisted file and
2 MiB across those files. The final JSON is at most 32 KiB including newline.
No directory discovery, network request, API polling or added permission occurs.
The producer records file identities; existing native receipt validators retain
semantic ownership. A valid synthetic JSON record cannot prove execution.

## Alternatives considered

- Reuse an old green check based only on equal source trees: insufficient run,
  attempt, workflow, artifact and failed-observation attribution.
- Pin or retain every build input immediately: a separate reproducibility and
  storage decision. Current Python/uv/frontend tags and browser/APT delivery
  remain mutable; output image digests identify what ran, not a hypothetical
  future build. This witness is not complete build-material provenance.
- Rename existing receipt artifacts now: deferred to compatibility review.
  Frozen historical observation adapters and receipt names remain unchanged.

## Consequences and rollout

The new `application-execution-witness-<run>-<attempt>` artifact is separate from
`application-core-<run>`. It binds the receipt upload's immutable ID and digest;
it cannot include its own eventual artifact ID. A future consumer must obtain
both identities through GitHub, verify exact archive bytes and their provenance,
and reject unavailable, replaced or deleted artifacts. Existing naming does not
promise retention of every attempt's receipts. Cancellation, checkout failure
or output failure can prevent final witness publication; absence grants nothing.

Authentication and reuse remain unimplemented and unaccepted. Before activation,
review a trusted verifier, current owner/same-PR/base admission, immutable run and
attempt association, merge-test tree and workflow-code parity, latest-failure
vetoes, artifact/ZIP bounds and digest checks, complete receipt revalidation,
direct-origin-only proof and a finite original-execution TTL. A proposed 24-hour
TTL is policy for later review, not a measured reliability guarantee. Preserve
current-head Linux CI and every applicable job/review/ruleset; a reuse result
must never mint a new origin or imply fresh mutable build inputs. Release,
coordinated upgrade, soak and external-contribution acceptance remain separate.

## Implementation and evidence

| Requirement | State | Source or delivery issue | Validation evidence |
| --- | --- | --- | --- |
| Bounded direct Application witness and attempt-qualified artifact | Partial | `qualification/application/execution_witness.py`, `.github/workflows/application-qualification.yml` | Focused producer/workflow tests; source-specific hosted witness validation belongs to the delivery PR and #469 |
| Existing receipt artifact compatibility | Implemented in candidate | Existing receipt name and acquisition adapter unchanged | Extra-witness artifact acquisition regression |
| Bounded read-only metadata/artifact/source binding diagnostic | Partial in candidate | `scripts/verify_application_witness.py`, `qualification/witness_binding/`, #469 | Focused mocked/host-Git controls and a historical owner-run binding; exit 3 and explicit semantic/admission false boundaries |
| Trusted pure semantic bundle and qualification admission | Not started | #469 | Existing runner-module imports are not a standalone trusted semantic bundle; the binding diagnostic performs no product receipt validation |
| Reuse consumer, complete failure-history policy and TTL | Not started | #469 | No reuse or authorization path exists |
| Frozen build materials or retained reusable candidate images | Not started | #469, separate scope review | Not established by output image metadata |
| Enforced OS budgets and measured validation savings | Not started here | #469 | No performance or resource-peak claim |

## Revisit and supersession

Review the future consumer before enabling reuse. Update this record when the
decision or implemented scope changes; record each checked revision and hosted
witness audit in the delivery PR and #469. Source delivery does not accept the
proposed reuse design. Revisit artifact naming separately if multiple attempts
must retain all original receipt archives.

## Change history

| Date | Change | Evidence |
| --- | --- | --- |
| 2026-11-16 | Proposed provenance-first Application slice; reuse remains disabled | Candidate source and focused tests in this PR |
| 2026-11-16 | Added candidate binding-only reader without accepting semantic validation or reuse | Isolated diagnostic, bounded mocked/host-Git controls and outside-Git original-run binding reports |
