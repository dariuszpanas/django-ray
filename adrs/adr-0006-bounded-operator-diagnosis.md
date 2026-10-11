# ADR-0006: Bounded read-only operator diagnosis

## Tracking

| Field | Value |
| --- | --- |
| Decision status | Proposed |
| Implementation status | Partial: bounded read-only first slice implemented and initially qualified; remaining observations and final integrated artifact qualification pending |
| Owners | django-ray maintainers |
| Proposed | 2026-09-21 |
| Accepted | Not accepted |
| Decision review | Pending; merging this proposal does not record acceptance |
| Delivery tracking | [#368](https://github.com/dariuszpanas/django-ray/issues/368); bounded first slice [#620](https://github.com/dariuszpanas/django-ray/issues/620) |
| Supersedes | None |
| Superseded by | None |
| Last verified | Initial first-slice source review, Linux supervision, independent PostgreSQL and installed-wheel checks passed; rebased qualification and separate fresh installed-sdist verification remain pending as of 2026-10-12 |

## Context and scope

Operators need one supported diagnostic entry point before maintenance and when
work stops progressing. A healthy database connection or fresh worker lease is
insufficient evidence that a task can execute, a queue has capacity, or a
deployment is safe to upgrade.

The existing protocol report bounds rendered output to 65,536 bytes and owns a
consistent read-only PostgreSQL transaction. Its aggregate queries do not have
a command-owned deadline. The existing worker readiness probe checks one exact
lease and explicitly does not establish Ray or queue readiness. These are useful
foundations, not an implementation of the doctor described here.

This proposal covers read-only diagnosis under #368. Drain, retirement,
quarantine, claim exclusion and their concurrent-operation guarantees remain
required follow-up work in that issue. It does not expand the accepted 0.6.0
candidate, activate target attestation under #386, or authorize mixed-version
execution. Linux remains the execution target.

## Decision

Propose a `django_ray_doctor` command with human-readable output and versioned
JSON. Report observations and their limits rather than one inferred healthy flag.
Each section must identify its observation time, outcome, bounded evidence and
missing prerequisites. Outcomes must distinguish observed success, an observed
blocker, unavailable evidence, unsupported configuration and budget exhaustion.
Timeout, omission and unsupported checks must never become successful results.

The first implementation must define and validate finite defaults and hard
maximums for total elapsed time, each probe's elapsed time, database query and
lock time, output bytes, returned rows, and external requests. Those numeric
limits and exit-code semantics are part of the reviewed command contract, not
an implementation detail. Small result sets do not prove bounded query work.

Use an owned, supervised probe process for operations that can block outside a
database statement, including connection establishment and remote calls. The
supervisor must enforce the deadline and output limit while reading; collecting
unbounded output and truncating it afterward is insufficient. Terminate and
reap only the owned process group when a budget expires. A database-side
statement/lock deadline is additional protection, not the total-time guarantee.
The implementation must prove cleanup on success, failure and interruption.

Use dedicated diagnostic connections and read-only transactions where supported.
Do not change the caller's connection settings, transaction, SQLite callbacks or
worker leases. Unsupported database configurations must produce an explicit
unsupported result. Django startup is necessary, but probes must not resolve
persisted task callables, deserialize task payloads or execute user work.

Required observation areas are:

- Package version, applied django-ray migrations and database availability.
- Protocol compatibility, fresh leases and queued work without an observed
  compatible consumer, preserving queue and worker identity boundaries.
- Running and uncertain work, with precise definitions and observation limits.
- Configured Ray transport reachability, separately from execution readiness.
- Artifact and encryption readiness without publishing keys or artifact content.
- Result/progress storage and cleanup evidence, including unavailable inventory.
- Coordinated upgrade and rollback blockers supported by current evidence.

Target attestation remains unavailable until #386 activates its evidence source.
Unregistered-object discovery remains #406; absence of registered cleanup errors
does not establish absence of orphaned objects. A passing doctor report is a
dated observation, not authorization to upgrade, delete, drain or quarantine.

Only fixed diagnostic codes and explicitly bounded, redacted fields may reach
stdout. Raw exceptions, credentials, payloads, backend URLs and child-process
stderr must not be copied into the report. An unavailable probe should not erase
other completed sections. Cross-section observations are not an atomic snapshot;
their timestamps and consistency boundaries must remain visible.

## Alternatives considered

- Wrap protocol status and worker readiness in one command. This reuses useful
  observations but cannot alone bound connection/query latency or establish
  runtime, storage and maintenance readiness.
- Run every probe in the command process with client timeouts. This is simpler,
  but a client timeout is not proof that all connection, resolver or cleanup
  paths stop within the command deadline.
- Run an active sample task. This can provide separate execution evidence but
  mutates the deployment and may execute application code. It does not belong
  in the default read-only doctor contract.

## Consequences and rollout

Process supervision adds packaging, startup and cleanup complexity. Validate
the installed wheel as well as source execution, and make the selected Django
settings/database explicit without putting secrets on a command line. Do not
claim a hard deadline until a blocked child and its owned descendants have been
tested. The supervisor also needs bounded shutdown after external interruption.

The command is additive and requires no schema change. Existing readiness and
protocol-status interfaces retain their contracts. Implement diagnosis before
mutation controls, document actionable next steps for each outcome, and retain
#368 until its full acceptance criteria are met. A rollback removes the new
entry point; it must not require reversing durable state or restoring leases.

## Implementation and evidence

The first slice tracked by #620 is intentionally incomplete: configuration and
database observations do not implement remote reachability, target attestation,
queue capacity, artifact availability or orphan inventory. Those sections remain
explicitly unavailable and an otherwise clear report returns exit 2. Its proposed
[numeric and ownership contract](../docs/reference/doctor.md) includes the trust
boundary around settings-module import and unconfirmed cleanup when process
ownership cannot be established. This implementation-status update records
initial source-specific qualification. It does not accept the ADR, authorize
maintenance, establish unavailable observations, or qualify a later combined
release tree.

| Requirement | State | Source or delivery issue | Validation evidence |
| --- | --- | --- | --- |
| Existing protocol observation foundation | Existing, limited | [protocol_status.py](../src/django_ray/protocol_status.py) | [Protocol integration tests](../tests/integration/test_protocol_status.py); not doctor qualification |
| Existing exact-lease readiness foundation | Existing, limited | [worker_readiness.py](../src/django_ray/worker_readiness.py) | Does not attest Ray or queue capacity |
| Versioned doctor report and CLI | Implemented first slice | #620; [doctor package](../src/django_ray/doctor/) | Initial #621 Linux CI passed; exact rebased and release-candidate checks remain required |
| Total/probe/query/output bounds and owned cleanup | Implemented first slice | #620; [supervision tests](../tests/unit/test_doctor_supervision.py) | Nine actual Linux supervision cases passed on each supported interpreter, including blocked/escaped descendants, output, timeout and interruption; process-group and unconfirmed-cleanup limits remain explicit |
| Read-only behavior and connection isolation | Implemented first slice | #620; [PostgreSQL tests](../tests/integration/test_doctor_postgresql.py) | Owned SQLite checks and five actual PostgreSQL cases passed: dedicated read-only session/write refusal, statement/lock/connect bounds, caller preservation and unsupported-row preservation without payload hydration |
| Secret-safe partial failure and unsupported results | Implemented first slice | #620; [doctor tests](../tests/unit/test_doctor.py) | Injected-error, retained partial observation, unsupported-setting and bounded scalar-output checks passed in initial Linux CI |
| Installed Linux integration and user guidance | Partial qualification | #620; [owned installed fixture](../scripts/verify_doctor_wheel.py) | Initial installed-wheel doctor invocation passed with owned SQLite observations, incomplete exit 2, preservation and cleanup. Separate fresh sdist installation remains pending the release-candidate artifact matrix |
| Drain, retirement and quarantine | Outside this proposal; still pending | #368 | Separate fenced mutation and race acceptance required |

The selected cold-Ray application qualification also passed for the initial
#621 source, including source/image correspondence, both cold generations,
workload/API/task checks, preservation and owned cleanup. That application smoke
does not invoke doctor and does not replace the separate supervision, PostgreSQL
or installed-package evidence. It is not the complete release application
matrix. Final integration requires current-source checks again.

## Revisit and supersession

Revisit before acceptance if process isolation cannot preserve supported Django
configuration or meet startup/cleanup budgets. Revisit observation schemas when
#386 or #406 introduces new evidence. Any future active execution probe or
maintenance mutation requires a separate explicit contract and evidence; it
must not silently change the read-only command's meaning.

## Change history

| Date | Change | Evidence |
| --- | --- | --- |
| 2026-09-21 | Proposed bounded diagnosis, independent from mutation controls | #368 and source inspection at `add2fd8`; implementation evidence missing |
| 2026-10-12 | Record partial first-slice implementation and initial source-specific qualification; retain Proposed decision and unfinished observations | #620 / #621; actual Linux supervision, PostgreSQL and installed-wheel checks; final combined tree and fresh installed-sdist acceptance pending |
