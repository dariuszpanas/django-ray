# django-ray 1.0 Readiness

This is the canonical graduation checklist for removing django-ray's Beta
classifier and publishing 1.0.0. It turns the release decision into explicit
evidence rather than a feature count or a date on the calendar.

The versioned machine-readable companion is
`tests/contracts/one_zero_readiness_v1.json`. Release validation checks its
shape, its evidence paths, the criterion identifiers on this page, and the
relationship between its decision and the package classifier. The registry is
the status record; this page defines how the evidence is interpreted.

The v1 criterion identities, categories, required flags, and owners are also
fixed in the release validator. Changing a status, evidence reference, or
disposition updates readiness state; removing, renaming, re-owning, or making a
v1 criterion optional requires an explicit versioned contract change and cannot
be accomplished by editing the registry and documentation together.

## Delivery sequence after 0.7

Version [0.7.0](https://github.com/dariuszpanas/django-ray/releases/tag/v0.7.0)
was published on 2026-10-18 with the Beta classifier. It includes bounded read-only
diagnostics, shared worker lease policy, retained RuntimeEnv maintenance and opt-in
required encryption. These delivered controls contribute to operational hardening;
their release does not satisfy the broader operator-controls criterion or accept
a later candidate. The [graduation roadmap](https://github.com/dariuszpanas/django-ray/issues/372)
tracks the remaining work. Closed issues for API definitions, readiness
validation, and an earlier release's upgrade rehearsal establish those
deliverables; they do not accept a later release candidate.

Use the following sequence to select small releases. These are work stages,
not assigned version numbers, dates, or a promise that each stage fits in one
release. Each release selects and qualifies its own scope.

| Stage | Work and exit evidence |
|---|---|
| Maintenance | Deliver validated dependency and correctness repairs; keep unresolved native shutdown and workflow-publication findings visible. A passing rerun alone does not establish a repair. |
| Operational hardening | Complete bounded operator diagnosis and maintenance controls, safe artifact cleanup, and the selected upgrade and encryption lifecycle work. Retain the affected concurrency and deployed-behavior evidence. |
| Certification candidate | Freeze the proposed stable API and support policies, run sustained soak/failure certification, rehearse the released-baseline upgrade, and review adopter observations. Accept only scoped fixes and compatibility work against that candidate. |
| Stable 1.0 | Accept every required registry criterion, retain final source-matching evidence, then make the classifier and release decision. |

Start [adopter evidence collection](https://github.com/dariuszpanas/django-ray/issues/606)
during the Beta releases so its observation window can overlap implementation.
The [API and support acceptance tracker](https://github.com/dariuszpanas/django-ray/issues/607)
owns the final contract review. Operator controls remain in
[#368](https://github.com/dariuszpanas/django-ray/issues/368), and sustained
certification remains in [#370](https://github.com/dariuszpanas/django-ray/issues/370).
Each issue records work toward the registry; closing a tracker never substitutes
for reviewing its evidence.

Durable step resume, paged map inputs, shared workflow budgets, larger graph
profiles, optional tracing, and Compiled Graph support are separate feature
decisions. They are not prerequisites for stabilizing the documented dynamic
workflow and entry-point replay contract. An observed defect in that supported
contract still needs an explicit severity and release disposition.

## Decision states

The registry-level decision has two states:

- `tracking` means the checklist is incomplete. The package must retain
  `Development Status :: 4 - Beta`.
- `accepted` means every required criterion is `satisfied`, the acceptance date
  and accepting maintainer are recorded, and the final evidence has been
  reviewed. Acceptance permits the release change to remove Beta; it does not
  publish a release by itself.

Individual criteria use these states:

| Status | Meaning |
|---|---|
| `pending` | Evidence or a maintainer decision is still required. |
| `blocked` | A named dependency prevents completion and the blocker is recorded. |
| `satisfied` | The cited evidence has been reviewed and accepted for this criterion. |
| `deferred` | An optional criterion is explicitly outside 1.0; required criteria cannot use this state at acceptance. |

Changing a criterion to `satisfied` is a review decision, not a claim inferred
from a file merely existing. The cited source must directly establish the
criterion for the release candidate being evaluated.

## Product contract

<!-- readiness-criterion: product.stable-api -->
- `product.stable-api`: accept the checked Python API inventory and the stable
  settings, commands, statuses, metrics, durable formats, and extension seams.
  The current inventory remains a candidate while the project is Beta.

<!-- readiness-criterion: product.compatibility-matrix -->
- `product.compatibility-matrix`: accept the final supported Python, Django,
  Ray, database, operating-system, architecture, and production-topology
  boundaries in the compatibility documentation.

<!-- readiness-criterion: product.no-severe-defects -->
- `product.no-severe-defects`: record a release-blocker triage showing that no
  known critical or high-severity defect remains in the stable core. A list of
  deferred lower-severity work is acceptable when its stability impact is
  explicitly assessed.

<!-- readiness-criterion: product.experimental-boundary -->
- `product.experimental-boundary`: keep experimental, private, and example
  surfaces outside the 1.x compatibility promise. Compiled Graph, native
  Windows production use, optional Ray ecosystem components, and the bundled
  test project do not silently become stable by shipping in the distribution.

## Upgrade and operational evidence

<!-- readiness-criterion: operations.protocol-fencing -->
- `operations.protocol-fencing`: prove execution-protocol versioning,
  unsupported-worker rejection, current-version manager recovery and explicit
  rollback limits without using package SemVer as a durable protocol switch.
  Mixed-version rolling handoff requires a separate product decision; it is not
  an implicit prerequisite for the coordinated Beta upgrade contract.

<!-- readiness-criterion: operations.operator-controls -->
- `operations.operator-controls`: accept bounded diagnostics, deterministic
  drain, safe worker retirement, quarantine, and their concurrent lifecycle
  behavior on SQLite and PostgreSQL.

<!-- readiness-criterion: operations.preserved-data-upgrade -->
- `operations.preserved-data-upgrade`: retain a repository-owned upgrade
  rehearsal from the documented released baseline to the final candidate. The
  record must identify completed drain, backup/restore, stopped-writer migrations,
  coordinated component updates, readable historical data, rollback boundary and
  final durable outcomes. No queued or running work is silently discarded.

<!-- readiness-criterion: operations.soak-certification -->
- `operations.soak-certification`: accept the versioned bounded soak and
  failure-certification report owned by issue 370. Every submitted task needs
  an explainable durable outcome and cleanup must remain within named limits.

## Adoption evidence

<!-- readiness-criterion: adoption.external-applications -->
- `adoption.external-applications`: record at least two applications outside
  the bundled test project. Evidence may be anonymized, but must identify the
  django-ray version, workload class, execution modes, database, duration, and
  actionable findings.

<!-- readiness-criterion: adoption.kuberay-deployment -->
- `adoption.kuberay-deployment`: record at least one Linux Kubernetes/KubeRay
  deployment. A passing disposable gate is necessary release evidence but does
  not replace an adopter observation.

<!-- readiness-criterion: adoption.preserved-state-upgrade -->
- `adoption.preserved-state-upgrade`: record at least one adopter upgrade that
  preserves historical tasks, results and referenced artifacts through the
  documented coordinated upgrade. A completed drain is valid evidence; silently
  deleting a backlog or unresolved execution is not.

<!-- readiness-criterion: adoption.documentation-review -->
- `adoption.documentation-review`: retain one installation and operation review
  performed from documentation alone by somebody other than the primary
  implementation author.

<!-- readiness-criterion: adoption.observation-window -->
- `adoption.observation-window`: retain a meaningful observation period,
  normally 60 to 90 days. A different period requires an explicit maintainer
  disposition explaining why the collected workload and failure evidence is
  equivalent.

### Sanitized adopter record

An adopter record must contain only the minimum evidence needed for review:

- anonymous record identifier and observation dates;
- django-ray, Python, Django, Ray, database, Kubernetes, and KubeRay versions
  when applicable;
- coarse topology, workload class, execution modes, approximate volume, and
  duration;
- upgrade source and target, compatible state retained, and rollback outcome;
- incidents, resource trends, operator actions, and resulting project issues;
- reviewer role and an explicit statement that the record contains no secrets,
  payloads, customer identity, private infrastructure names, or credentials.

Use ranges or categories when exact counts could identify an adopter. Never
retain task arguments, results, RuntimeEnv contents, tokens, private hostnames,
cluster identifiers, or raw logs. Private evidence may be reviewed out of band;
the registry should cite only a sanitized repository record or an explicit
maintainer disposition.

A satisfied externally owned criterion must cite its sanitized record as a
tracked repository path. Every path reference must resolve inside the checkout
without using a symlink and name a regular file committed in, and unchanged
from, the candidate tree. Generated, ignored, staged-only, locally modified,
and linked content therefore cannot become release evidence. The validator
proves that durability boundary; the accepting maintainer is responsible for
reviewing the record against the minimum contents and privacy rules above.

## Project support

<!-- readiness-criterion: support.security-fix-policy -->
- `support.security-fix-policy`: publish the supported 1.x release and
  security-fix boundary, including whether and when fixes are backported.

<!-- readiness-criterion: support.vulnerability-reporting -->
- `support.vulnerability-reporting`: retain the private vulnerability-reporting
  path, safe public fallback, and prohibition on public exploit evidence and
  secrets.

<!-- readiness-criterion: support.release-and-rollback -->
- `support.release-and-rollback`: accept a reproducible release procedure and
  an operational rollback procedure that names persisted-protocol limits,
  migration boundaries, and cases requiring a compatible worker cohort.

<!-- readiness-criterion: support.contributor-triage -->
- `support.contributor-triage`: publish contributor, pull-request, issue-intake,
  and security-routing expectations that match the enforced repository flow.

<!-- readiness-criterion: support.maintainer-capacity -->
- `support.maintainer-capacity`: publish a realistic statement of maintainer
  capacity and non-guaranteed response, support, security-fix, and release
  timing. Stable describes compatibility, not a service-level agreement.

## Final release evidence

<!-- readiness-criterion: release.final-evidence -->
- `release.final-evidence`: bind the final candidate to passing Linux/KubeRay,
  PostgreSQL, upgrade, soak, security, documentation, wheel, and sdist evidence.
  The exact source and relevant dependency identities must match the release
  candidate; earlier or unrelated runs cannot be substituted.

Before accepting the registry:

1. verify every required criterion is `satisfied` and directly supported by its
   cited evidence;
2. ensure deferred features remain outside the stable contract;
3. record the acceptance date and maintainer in the registry;
4. run the full release validation against the exact candidate; and
5. remove Beta and add `Development Status :: 5 - Production/Stable` only in
   the final accepted release change.

The release validator fails closed if Beta is removed while the registry is
still tracking, if an accepted decision retains an incomplete required
criterion, or if a 1.0 release lacks the sole Production/Stable development
classifier. Acceptance dates use canonical `YYYY-MM-DD` form and are evaluated
against one date captured for the complete validation run.
