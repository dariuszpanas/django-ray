# Compatibility and Version Policy

This page defines the tested dependency and platform matrix. The separate
[Stability and Deprecation Policy](stability.md) defines the proposed 1.0 public
contract, experimental boundary, and removal process while django-ray remains Beta.

## Supported Versions

| Component | Supported |
|---|---|
| Python | 3.12, 3.13, 3.14 |
| Django | 6.0 or newer compatible release |
| Ray | 2.56.0 or newer compatible release |
| Production operating system | Linux recommended |

Python 3.12 is the floor because Django 6.0 requires Python 3.12+, not because Ray does.
Current [Ray releases support a wider Python range](https://pypi.org/project/ray/).

Ray 2.56.0 is the django-ray 0.4 security floor. Earlier releases fall below fixes in
published upstream Ray advisories for the
[dashboard and Jobs boundary](https://github.com/ray-project/ray/security/advisories/GHSA-q5fh-2hc8-f6rq),
[Ray Data Parquet reads](https://github.com/ray-project/ray/security/advisories/GHSA-mw35-8rx3-xf9r),
and the
[Ray Data WebDataset reader](https://github.com/ray-project/ray/security/advisories/GHSA-hhrp-gw25-jr43).
Upgrade the task managers, Ray head, and Ray workers together before installing
django-ray 0.4; do not use a mixed Ray minor-version cluster as a rolling-upgrade
shortcut.

The package dependency range is a resolver boundary, not permission to mix remote
runtime tuples. A target-attested task-manager cohort must match the configured Ray
version and the Python implementation plus `major.minor.patch` exactly across the
manager and every live schedulable cluster node. Ray's connection-time warning or
`RAY_IGNORE_VERSION_MISMATCH` does not weaken that django-ray rule. The initial bounded
attestation codec and Ray 2.56.0 probe are dormant infrastructure: current workers do
not yet advertise target capacity or fence claims with their output. The additive
target-persistence schema likewise records only immutable target intent, append-only
policy revisions, and verified canonical observation history. Verified versus expired
is derived from the latest matching proof and its bounded expiry; mismatch,
unreachable, identity-drift, malformed, and expired probe outcomes are not fabricated
as observation rows. The private coordinator registers Ray Core targets in `draining`,
allows only revision-checked `active`/`draining` policy transitions, reserves `retired`
for #368, and rejects Ray Job persistence until its authenticated response channel
exists. Migration `0023` adds a deliberately unseeded, create-once relationship from an
execution to one immutable target-policy revision. A future target-aware consumer must
treat absence as unbound and fail closed; current workers remain target-unaware and do
not consult the table. `created_at` records only when the relationship was written, not
proof of enqueue-time selection. No writer, reader, Admin surface, enqueue, claim,
adoption, lifecycle, routing, or backfill consumer exists. Legacy binding remains
forbidden until #381 supplies exact mapping lineage. Until those later boundaries land,
upgrade task managers and every cluster node together and treat any Ray or Python patch
difference as unsupported.

Both binding foreign keys use `PROTECT`: once a binding exists, deleting its execution or
target-policy revision is rejected by the ORM and database. Current cleanup paths remain
unchanged only because the table is unseeded. Activation therefore requires every task-
and policy-retention or cleanup path to define and test explicit ordering. A binding may
be deleted first only under that audit and retention policy, never through an implicit
cascade or ordinary task cleanup.

Migration `0024` adds a bounded backend-alias namespace and immutable append-only route
revisions that select exact target-policy revisions. Its private coordinator registers a
route or compare-and-set appends its next revision only for the latest active Ray Core
policy. That route intent is not a live attestation, current capacity, claim authorization,
or work placement. A separate, initially empty route-selection table can preserve which
exact route revision explains an existing task binding, but no package task or binding
writer, reader, enqueue path, worker, lifecycle path, or runtime consumer creates or reads
that provenance. Absence is unproved provenance, never permission to infer a default route.
Legacy 0.4 mapping is a distinct boundary deferred to #381; neither route history nor an
absent selection supplies its lineage.

Both route-selection parents use `PROTECT`, and route revisions in turn protect their route
and target-policy parents. Cleanup must delete a selection before either its binding or
route revision, delete all route revisions before their route, and preserve every binding
or route-revision reference before deleting a target-policy revision. Those orders require
an explicit audit and retention policy before any task-selection writer can activate.

Migration `0025` adds a normalized, unseeded current-capability row per exact task-manager
lease incarnation and Ray target. It snapshots the lease identity and manager's exact
Ray/Python tuple and points to one exact target-policy and verified-attestation revision.
Renewal changes that one ephemeral row under a bounded compare-and-set revision; it does not
append another audit history. Current Django ORM lease deletion cascades to the capability while
raw parent deletion remains foreign-key restricted, so worker-ID reuse cannot inherit capacity.
The immutable policy and attestation revisions remain the
audit record, and a future execution path must separately archive authenticated target evidence
per generation or attempt.

The private capability coordinator currently accepts Ray Core only. A fresh exact lease and
latest unexpired proof may support an `active` policy or preserve capacity for already-pinned
work while its policy is `draining`; draining never permits a new route or enqueue. Ray Job
capability APIs remain unsupported until an authenticated pre-Django proof channel exists.
No production lease creation, heartbeat, reconnect, enqueue, claim, adoption, lifecycle,
status, runner, or transport path creates, renews, reads, or treats a capability row as
capacity. Existing exact-lease deletion, including supported Admin inactive-lease cleanup,
may only fail-closed cascade-withdraw an otherwise unreachable row. Row presence alone is
never authority: every future consumer must revalidate the exact live lease, current policy,
same latest verified attestation, and proof expiry under its ownership locks.

Migrations `0022_ray_target_persistence`, `0023_ray_task_target_binding`, and
`0024_ray_target_routes`, and `0025_ray_worker_target_capabilities` are dormant and additive
for a schema-first upgrade from 0.4.0. Exact 0.4.0 code ignores their new tables, so a code-only
rollback retains the durable history while no old process consumes a capability row. Schema
reversal is a separate stopped-writer operation. Delete every current capability before
reversing `0025`; reverse `0024` only after exporting or auditing and deliberately deleting
every selection, route revision, and route; reverse `0023` only after every binding is deleted;
reverse `0022` only after all target history is deleted. Database guards reject invalid bounded
inserts and unsafe capability transitions while leaving explicit withdrawal and maintenance
deletion paths. A binding, route revision, or capability row records neither self-sufficient
claim authority nor permission to ignore a later policy or proof change. Schema reversal is
not part of an ordinary binary rollback.

The general version range and base `ray[default]` dependency do not install or promise
every optional Ray component. See the
[Ray Ecosystem Support and Install Matrix](ray-ecosystem.md) before adding Data, Train,
Tune, RLlib, Serve, or Compiled Graph to an application workload.

Ray Compiled Graph has a separate, exact, fail-closed capability policy because its
native beta channels have narrower version, platform, transport, and process-owner
constraints. The general Ray version range in this table does not enable compilation.
Generic or unresolved host/container context is also insufficient: an eligible row
requires an immutable deployment/image digest plus explicit shared-memory and Ray
object-store profiles.
See [Compiled Graph Compatibility](compiled-graph-compatibility.md).

## Dependency Policy

`pyproject.toml` uses lower bounds so applications can resolve compatible updates
instead of being locked to the versions used for one django-ray release. The committed
`uv.lock` gives contributors and CI a reproducible current environment.

CI covers:

- the committed lock on every supported Python minor;
- minimum direct dependencies on the oldest supported Python;
- the newest resolvable dependencies on the newest supported Python;
- matching wheel and sdist security metadata plus package installation from the built
  wheel on every supported Python minor.

Updating the lock is therefore separate from raising a package's minimum supported
version. A lower bound should move only when django-ray uses a newer API or the older
dependency is no longer supportable. A published dependency security fix is such a
support boundary: the repository lock protects its own reproducible environment, while
the declared lower bound controls what a downstream fresh install may resolve.

## Platforms

Ray publishes platform-specific wheels. A pure-Python django-ray wheel does not imply
that Ray is available on every Python/platform combination.

- Linux is the production target for clusters and Kubernetes.
- Ray's
  [native Windows support remains beta](https://docs.ray.io/en/latest/ray-overview/installation.html#windows-support),
  and multi-node Windows clusters are untested upstream. django-ray retains Windows for
  best-effort local development and
  test visibility, not as a production or release-certification target. Repeated native
  local-Ray lifecycles have intermittently aborted during startup before any job, worker,
  object, or application task registered; the upstream investigation is
  [ray-project/ray#65181](https://github.com/ray-project/ray/issues/65181). No Ray release
  is currently identified as the fix. Use Linux, WSL2, or the documented Docker path for
  repeatable evaluation, and keep one native local-Ray owner on a Windows host at a time.
- Ray publishes Linux aarch64 wheels for supported Python versions, but users must
  confirm their OS, architecture, and Python ABI match an available Ray wheel.

Compiled Graph is more restrictive than ordinary Ray use: policy version 3 rejects
Windows, aarch64, Ray Client, GPU transport, and every unverified native tuple before
calling `experimental_compile()`. Dynamic workflows remain supported according to the
version matrix above.
