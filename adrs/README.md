# Historical architecture decision records

These records preserve selected design discussions and decisions. They are an
incomplete historical archive, not a catalogue of current features or a record
of every architectural change. They are kept in GitHub and are not published
with the documentation site.

An **accepted** status means the decision was accepted when recorded. It does
not mean all proposed work shipped, remains implemented, or is supported today.
Implementation and supersession were not tracked consistently; later changes
may have replaced a design without a follow-up ADR. Original statuses are kept
as historical information and have not been revalidated as current claims.

For current behavior, start with the maintained [architecture guide](../docs/architecture.md),
[user documentation](../docs/README.md), source, and tests. Use the linked issues
and Git history to establish what shipped, changed, or remains pending. An ADR
alone is not implementation or release-readiness evidence.

## New decisions and updates

Copy [TEMPLATE.md](TEMPLATE.md) into the next numbered ADR. Review the decision
through a normal pull request; link that review when accepting it. Use an ADR
for a significant architectural choice, not every feature or implementation PR.
Keep these Markdown records outside `docs/`; GitHub is their browsing interface.

Track two independent states:

- Decision: **Proposed**, **Accepted**, **Rejected**, **Superseded**, or **Retired**.
- Implementation: **Not started**, **Partial**, **Implemented**, **Removed**, or
  **Unverified**. An accepted decision can have any of these implementation states.

Acceptance needs a decision review link. Implementation claims need source and
validation evidence, a checked revision and date, and delivery issues for gaps.
An implementing PR should update the affected record. A replacement decision
should link both records and state whether supersession is partial or complete.
Removal should retain the rationale and link the removing change. Do not infer
implementation from acceptance or infer retirement merely from age.

Existing records have a dated, scoped source and issue audit above their original
bodies. That audit distinguishes retained implementation, pending integration,
and later design changes; it does not certify every historical assertion or
replace runtime qualification. Update individual claims only after checking
source, tests, merged changes, and remaining issues. The historical status
preserves the original wording.

## Records

- [ADR-0001: Workflow Plans and Execution Strategies](adr-0001-workflow-plan-contract.md)
- [ADR-0002: Compiled Session Ownership and Reuse](adr-0002-compiled-session-ownership.md)
- [ADR-0003: Compiled Invocation Lifecycle](adr-0003-compiled-invocation-lifecycle.md)
- [ADR-0004: Bounded Workflow Progress Storage](adr-0004-bounded-workflow-progress.md)
- [ADR-0005: Bounded Workflow Progress Preparation](adr-0005-bounded-workflow-preparation.md)
