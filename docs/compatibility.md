# Compatibility and Version Policy

This page defines the tested dependency and platform matrix. The separate
[Stability and Deprecation Policy](stability.md) defines the proposed 1.0 public
contract, experimental boundary, and removal process while django-ray remains Beta.

Beta upgrades follow the [coordinated upgrade procedure](stability.md#coordinated-beta-upgrades):
stop submissions, drain work, back up, stop old writers, migrate and update all components
together. Historical data preservation and current-version failure recovery remain required;
mixed-version managers and old-payload execution are not the Beta upgrade commitment.
The dormant protocol-2 and historical migration details below do not add a rolling-upgrade
requirement. Protocol 3 is the 0.5 execution boundary; preserved historical results do not
authorize older payloads to execute in the new cohort.

## Module Path Compatibility

`django_ray.workflows` remains the public defining module for workflow builders. Private
workflow planning, execution support, progress, storage, and Admin rendering live under the
inert `django_ray.workflow` package, while private target contracts and coordination live
under `django_ray.target`. The former flat internal module paths were removed during Beta;
they were never part of the checked public API inventory and have no compatibility shims.
Persisted workflow schemas and execution protocols retain their independent compatibility
rules.

Beta users that imported those private modules must move to the canonical package paths:

| Removed private path | Canonical path |
|---|---|
| `django_ray.workflow_plans` | `django_ray.workflow.plans` |
| `django_ray.admin_workflow_graph` | `django_ray.workflow.admin_graph` |
| `django_ray.workflow_output_previews` | `django_ray.workflow.previews` |
| `django_ray.workflow_progress` | `django_ray.workflow.progress.runs` |
| `django_ray.workflow_progress_storage.prepare_workflow_progress_topology` | `django_ray.workflow.progress.preparation.prepare_workflow_progress_topology` |
| `django_ray.workflow_progress_<name>` | `django_ray.workflow.progress.<name>` |
| `django_ray.ray_target_probe` | `django_ray.target.probe` |
| `django_ray.target_<name>` | `django_ray.target.<name>` |

## Supported Versions

| Component | Supported |
|---|---|
| Python | 3.12, 3.13, 3.14 |
| Django | 6.0.8 or newer compatible release |
| Ray | Resolver floor 2.58.0; current Core/Jobs qualification requires exactly 2.58.0 |
| Production operating system | Linux |

Python 3.12 is the floor because Django 6.0 requires Python 3.12+, not because Ray does.
Current [Ray releases support a wider Python range](https://pypi.org/project/ray/).

Django 6.0.8 is the django-ray security floor. Django 6.0.0 through 6.0.7 are not
supported; newer compatible releases are exercised by the latest-dependency lane.

Ray 2.58.0 is the django-ray 0.5 security and runtime baseline. Ray 2.57 added
[Dashboard log-path validation](https://github.com/ray-project/ray/pull/64701),
which is absent from 2.56.0 and 2.56.1. Ray 2.58 additionally fixes
[nested Parquet/Lance pickle handling](https://github.com/ray-project/ray/pull/64881)
and the [Serve internal authentication/TLS boundary](https://github.com/ray-project/ray/pull/65189).
The latter fixes concern optional application-owned Data and Serve workloads; the
bundled Data recipe reads JSON, not Lance or nested pickle data. These extend the
earlier fixes behind django-ray 0.4's Ray 2.56.0 floor.

Upgrade the task managers, Ray head, and Ray workers together before installing
django-ray 0.5; do not use a mixed Ray minor-version cluster as a rolling-upgrade
shortcut. The Ray upgrade does not activate Compiled Graph, task-event migration,
Sandbox, or a new execution protocol. Existing cancellation, ownership and durable
completion fences remain necessary.

The package dependency range is a resolver boundary, not permission to mix remote
runtime tuples. A target-attested task-manager cohort must match the configured Ray
version and the Python implementation plus `major.minor.patch` exactly across the
manager and every live schedulable cluster node. Ray's connection-time warning or
`RAY_IGNORE_VERSION_MISMATCH` does not weaken that django-ray rule. Current-cohort
workers use the bounded Ray 2.58.0 probe, immutable target identity, revisioned policy
and fresh canonical observations to qualify Core and Jobs claims. Verified versus
expired is derived from the latest matching proof and its bounded expiry; mismatches,
unreachable nodes, identity drift and malformed observations grant no capacity.
The initial protocol-2 APIs and route records remain dormant historical infrastructure;
their existence does not authorize protocol-2 execution or legacy binding/backfill.
Upgrade task managers and every cluster node together and treat any Ray or Python patch
difference as unsupported. See [current-cohort execution](#current-cohort-execution).

The root `Dockerfile` and ordinary Compose path intentionally remain patch-flexible on
Python `3.12`. After non-mutating preflight, the guarded local KubeRay gate's mutable images
layer requires one exact shared rendered Ray head/worker image reference and runs that
image's interpreter to discover its canonical `3.12.X` patch. It supplies the discovered
`PYTHON_VERSION` only to the current and released-`v0.4.0` application image builds, not to
`Dockerfile.ray`. This corrects the protocol-`2` probe precondition/runtime mismatch without
weakening exact tuple checks: final live attestation remains authoritative, including over
any local-Docker/Kubernetes cache divergence. A supported `py312` Ray image patch refresh
automatically rediscovers the local image patch and must still pass the cold proof; a different
Python minor is rejected.

Both binding foreign keys use `PROTECT`: once a binding exists, deleting its execution or
target-policy revision is rejected by the ORM and database. Current protocol-3 claims
use schema-2 bindings and retain their generation history and cleanup obligations.
A binding may
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
audit record. Protocol-3 claims separately archive authenticated target evidence
per generation and attempt.

The standalone capability coordinator accepts Ray Core only. A fresh exact lease and
latest unexpired proof may support an `active` policy or preserve capacity for already-pinned
work while its policy is `draining`; draining never permits a new route or enqueue. Ray Job
capability APIs remain unsupported through that standalone coordinator. Current workers
use the separate authenticated cohort publisher for Core and Jobs qualification.
The read-only doctor may aggregate scalar metadata without granting eligibility.
Existing exact-lease deletion, including supported Admin inactive-lease cleanup,
may only fail-closed cascade-withdraw current capability. Row presence alone is
never authority: every consumer must revalidate the exact live lease, current policy,
same latest verified attestation, and proof expiry under its ownership locks.

Protocol `2` now has a separate, package-private Ray Core transport and provenance boundary.
Its canonical request binds the durable task identity to a positive target-execution-evidence
ID and digest, its canonical `claimed_at`, the selected target-expectation digest, and the exact
claim-attestation digest. The private submit seam receives the complete canonical evidence claim,
expectation, attestation, and attestation-recorded time; it recomputes the digests, validates the
exact `RUNNING` task owner/route/generation/start and manager-runtime lineage, and requires
`attestation.observed_at <= recorded_at <= claimed_at < expires_at` before crossing Ray. The remote
bootstrap revalidates those request-bound controls, then takes a fresh bounded resource-state
snapshot. It requires the complete current schedulable node-ID set to equal the attested set and
the executing node's current session and runtime to match that still-valid claim before importing
Django setup, input-storage code, or the application callable. A matching proof can return a
`completion`; a proven mismatch returns only a `compatibility_rejection` with complete observed
evidence and `application_invoked=false`.
Malformed transport or a missing authenticated observation is uncertain, not a remote
compatibility rejection. A future authoritative manager may durably record that as an
`UNCERTAIN` outcome with `application_invoked=NULL` and no claimed observed proof so drain
remains blocked. Ray Job has no equivalent authenticated channel and remains unsupported for
protocol `2`.

The manager independently requires the authenticated observation time to satisfy
`claimed_at <= observed_at <= receipt_time`. A pre-claim or not-yet-valid timestamp, backwards
clock, or observation dated after manager receipt is uncertainty and retains the exact Ray handle;
it cannot authorize compatibility handback. The result and observed-proof preimage must also echo
the exact request-bound `claimed_at`; even a different canonical UTC timestamp is uncertainty.

This protocol-2 transport remains dormant. The package production protocol and supported
range are `3` and `3..3`; migration `0035` selects that policy and closes legacy admission.
No backend enqueues protocol `2`, no worker
claims it, no capability producer supplies the required generation claim, and no production
runner calls this protocol-2 submission seam. Historical protocol-1 records retain
their original bytes and are not executable by current workers.

Migration `0026_ray_task_target_execution_evidence` adds two unseeded, immutable provenance
records for that future activation. `RayTaskTargetExecutionEvidence` binds an exact execution,
positive attempt and claimed generation, and required route selection to the target, policy,
claim attestation, capability, lease-incarnation, and runner/manager runtime snapshots reviewed
for the claim. `RayTaskTargetExecutionOutcome` is a separate optional one-to-one record for the
matching completion evidence, proven compatibility rejection, or a future manager's durable
`UNCERTAIN` disposition. An uncertain outcome has null application-invocation state and no
claimed observed proof. At insert, the claim must match the exact `RUNNING` execution task, owner,
attempt, generation, route selection, and claim-time capability lineage. A complete outcome must
satisfy `claimed_at <= observed_at <= recorded_at`. The claim is create-once, the outcome is
create-once, retained evidence survives later execution lifecycle changes, and neither row is a
current-capacity signal. No production writer or reader creates or consumes either table in this
slice.

The Django-free `django_ray.target.execution_evidence` codec canonically encodes every immutable
claim snapshot and computes its domain-separated digest. The positive database evidence ID is carried
separately; protocol `2` binds that ID and digest together in the request and observed proof. The
codec is package-private provenance infrastructure and does not create a claim or authorize work.

Migrations `0022_ray_target_persistence`, `0023_ray_task_target_binding`, and
`0024_ray_target_routes`, `0025_ray_worker_target_capabilities`, and
`0026_ray_task_target_execution_evidence` are additive for a schema-first upgrade from 0.4.0.
Exact 0.4.0 code ignores those additive tables. This observation applies only before
the later activation migration; it does not permit code-only rollback after `0035`.
Schema reversal is a separate stopped-writer operation. Delete every
outcome and generation claim before reversing `0026`; delete every current capability before
reversing `0025`; reverse `0024` only after exporting or auditing and deliberately deleting
every selection, route revision, and route; reverse `0023` only after every binding is deleted;
reverse `0022` only after all target history is deleted. Database guards reject invalid bounded
inserts and unsafe capability transitions while leaving explicit withdrawal and maintenance
deletion paths. A binding, route revision, capability, claim, or outcome row is not permission
to ignore a later policy or proof change. Schema reversal is not part of an ordinary binary
rollback.

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

## Current-cohort execution

django-ray 0.5 producers and workers use protocol 3. Core and Jobs claims require
fresh qualification for the exact current runtime and verified Ray session; Sync
checks the current package and Python runtime without requiring Ray. Protocol 2
remains dormant. Legacy task execution entry points refuse before request hydration
or application import; historical terminal results remain readable without replay.

Migration `0035_activate_current_cohort` is a stopped-writer transition. Stop
submissions, finish or explicitly resolve all older queued/running/cancelling work,
verify remote cleanup, retire old writers, and take the coordinated backup before
applying it. The migration refuses unsupported nonterminal work, active incompatible
leases, unresolved claims, or open Jobs cleanup obligations. It preserves historical
rows, closes legacy admission and selects protocol 3 atomically; it does not convert
old payloads. Subsequent database guards reject new legacy work and incompatible
active leases. Changing the version constant or reopening legacy admission is not
an upgrade or rollback procedure. Reversal requires empty current-cohort history;
after activation, downgrade through the rehearsed stopped-writer backup/restore
procedure with the matching artifacts and retained encryption keys, not a code-only swap.

The release still requires matching Linux, PostgreSQL, native Ray and preserved-data
upgrade evidence for the integrated candidate. Source activation alone is not that proof.

A new producer intent records the package version, backend alias, selection policy,
and a digest of the exact declared endpoint and current trust configuration. This
finite declaration can be matched before a worker limits its queue query. The
task's original normalized RuntimeEnv JSON declaration has a separate immutable digest;
task-specific environments do not create additional worker eligibility keys.
Neither digest invents a cluster instance from an address or authenticates imported
source. The producer validates intent before preparing inputs and stores
it with the new execution in one transaction. Migration `0028` adds an immutable,
deliberately unseeded relation for schema-2 protocol-3 intent; historical protocol-1
rows receive no inferred identity. This draft schema replaces the earlier unmerged
schema-1 draft. A database that already applied that draft needs a fresh qualification
database or its reviewed, empty-table reversal before reapplying it.

The RuntimeEnv declaration digest is retained for audit. It does not scan filesystem
contents or authenticate package/source bytes. Admission does not compare observations
from different hosts, require reusable identities, or change existing RuntimeEnv
integrity and submission-snapshot checks.

Ordinary backends retain worker-selected synchronous, local Core, connected Core,
and Jobs modes; `RAY_JOB_ONLY` continues to select Jobs only. The first verified
claim must bind the actual selected mode and runtime. Subsequent generations must
retain that binding. A replacement Ray session, including a new local Ray session
after worker restart, cannot silently inherit already-bound work. Synchronous
execution needs its own package/Python binding, without a fabricated Ray session.
The current protocol-3 claim path persists these bindings; historical terminal
rows keep their original protocol and receive no inferred current binding.

Migration `0030` supplies those bindings and a separate per-generation claim ledger.
It preserves the meaning of schema-1 protocol-2 bindings. Schema-2 protocol-3 bindings
record the first actual runner and package; only Sync records a local Python tuple
without Ray fields. Claim facts retain the exact task, attempt, generation, intent,
RuntimeEnv snapshot, original manager incarnation and verified Ray proof when relevant.
The claim's current owner is separate from those immutable facts, so deleting an old
lease does not erase provenance or let a reused worker ID inherit ownership.

Private, caller-transaction services record request preparation, dispatch, held
uncertainty and authenticated resolution with compare-and-set revisions. Held work
has no automatic expiry or replay path. A valid late completion can resolve its exact
generation without a fresh target probe; proof freshness controls new admission,
not the lifetime of an execution. Resolution digests record independently verified
evidence and do not authenticate a caller or prove that application work had no effects.
Reverse `0030` only in a stopped-writer maintenance window after exporting or auditing
and deliberately removing every claim and schema-2 binding. No historical claims are
backfilled; protocol-3 claims and lifecycle transitions use this ledger after `0035`.
Claim facts use schema 2 to retain each Jobs configuration's own qualification.
The earlier unmerged schema-1 claim draft is rejected. A database that already
applied draft `0030` needs a fresh qualification database or its reviewed empty
reversal before reapplication; no claim facts or digests are rewritten automatically.

The first Core or Jobs probe can discover an observed session only after checking
the trusted manager/driver tuple against every schedulable node. Refresh probes
require an already-bound session and policy. First discovery derives its target key
from the verified runner family and session using a fixed, versioned rule, then
constructs the original attestation with that key. Endpoint, alias, configuration,
package and runtime changes cannot create a second identity for the same session
and bypass its drain. Core and Jobs retain separate target policies, so a target
drain applies to its runner family. The private adapter is reviewed for exact
Ray **2.58.0**; a later Ray release passing the dependency floor does not qualify
its private observation APIs. Package version and Python implementation plus
`major.minor.patch` must match exactly. Ambient Ray mismatch bypasses cannot alter
the comparison. The helper uses the existing bounded collector and never starts
or reconnects Ray on its caller's behalf.

Migration `0027` stores one pending challenge per exact lease incarnation and
configuration digest, bounded to one Core endpoint or 64 Jobs endpoints. First
discovery does not require a pre-existing target policy; a refresh may bind one.
The random challenge is stored only as a digest, rotates on explicit replacement,
and can be consumed once before its deadline. Issuance defaults to 300 seconds
and is capped at 600 seconds. Issued or consumed challenges are not capability.
An integrated Jobs manager must keep heartbeating while a probe is pending and
stop its exact probe job at the deadline. The private publisher commits a positive
observation, worker capability, and consumption of the exact pending challenge in
one authoritative transaction. It obtains Core observations or inspects Jobs
receipts itself outside database locks, then checks the live lease, nonce,
challenge, policy, revisions, and observation deadline again under locks. A
replaced challenge cannot publish its old result.

First discovery creates a draining policy at revision 1. A manager bootstrap option
can append active revision 2 in that same transaction, only when this publication
actually created the target. Existing targets retain their current desired state,
including explicit drains. The returned activation policy identifies a revision
awaiting a fresh probe; the revision-1 proof and capability are not relabeled and
do not authorize claims against revision 2. After a lost bootstrap response, a
manager must read the retained current policy and issue a new challenge for it.
The private publication path supports Core and Jobs, with one Core target or up to
64 Jobs targets per lease and no family mixing. Existing standalone target APIs
remain Core-only. Production workers enable newly created verified sessions automatically.
An existing target's drain remains in force, including after a manager restart or an
alias/configuration change; a new local Ray session receives its own verification.

Migration `0029` adds an immutable Jobs submission reservation and a one-time
driver receipt for that exact pending challenge revision. The reservation binds
the nonce-free request, actual Jobs endpoint, deterministic owned submission ID,
entrypoint, and submitted RuntimeEnv. The driver receipt records the actual native
Ray Job ID, package version, and complete node observation. Reservation, receipt,
and read services check the live lease and challenge again after acquiring locks;
neither a pending receipt nor a consumed challenge grants execution eligibility.
Replacing a challenge or deleting its lease removes the old reservation. Reversing
`0029` requires serialized empty receipt storage before reversing its parents.
The Jobs request uses schema 2 and a new digest domain: first discovery carries
no target key or session, while refresh carries its exact retained identity.
Receipt validation and publication independently derive and check the discovery
key. The earlier unmerged schema-1 request draft is rejected, including inside
retained launch or receipt envelopes. The amended draft `0029` database guard
requires a fresh qualification database or reviewed empty-table reversal before
reapplication; no pending evidence is rewritten or deleted automatically.

The private manager inspector fetches the reserved Job directly from its pinned
authenticated endpoint outside database transactions. It corroborates the exact
request controls, successful Job status, native driver ID, and unexpired receipt.
The HTTP reader caps the response at 128 KiB and applies connection/read timeouts
with a progressive-read budget. It refuses redirects, compression, and chunked
responses. An external manager deadline is still required for blocking OS DNS.
Job metadata and native IDs alone are not independent authentication.

The fixed private Jobs entry point accepts a canonical, bounded, nonce-free launch
request. The manager reserves its exact command, endpoint, submitted RuntimeEnv
digest, and explicit Django settings module before submission. The driver checks
the actual running Jobs record, collects observations with its own fresh Ray
connection, shuts that connection down, and corroborates the actual native driver
ID before importing Django. Only then does it initialize the pinned settings and
write a pending receipt through the default database. The manager separately
requires a successful Job and corroborates the stored receipt before publication.
Package versions and transport digests do not attest imported source bytes or
protect interpreter, installation, or setup hooks that execute before the entry
point. A qualified probe profile remains a prerequisite; ordinary RuntimeEnv
semantics are unchanged. The manager still owns external deadlines and exact Job
cleanup. The worker owns this manager lifecycle; a published capability alone does
not enable claims without current configuration, endpoint and shared-proof checks.
Each successful private Jobs publication returns its own endpoint qualification,
separate from the current shared target proof. If a slower Job observed the same
membership before a newer compatible shared proof, publication can use that fresh
shared proof without appending or relabeling the older observation. Its caller
still supplies current revisions, and the original endpoint proof keeps its own
expiry. A stale observation of different membership cannot replace newer state.

Private Jobs claims snapshot the original configuration, endpoint, exact Job and
receipt identities, control-environment digests, observation and consumption times.
They validate the consumed challenge revision and immutable receipt under the
claim locks, then recheck the endpoint, challenge and shared-proof expiries before
admission. Later receipt replacement does not erase an existing claim's audit
history or prevent independently authenticated completion. Core and Sync claims
do not invent Jobs qualification.

Production manager integration must retain a configuration's qualification only
from a successful authenticated publisher return. A consumed challenge alone
cannot reconstruct that positive state: challenge consumption is also available
without publication. After restart or an ambiguous response, obtain fresh
qualification. The private manager lifecycle keeps bounded current alias and shared
target state, rejects changed configuration epochs, and retains each Jobs alias's
independent receipt expiry when another alias supplies newer compatible cluster proof.
Refreshing a Jobs slot immediately withdraws that alias's old qualification. The source
control-profile digest is bound separately from the submitted mapping after upload;
task-specific RuntimeEnv changes do not replace the trusted manager profile. Existing
queue spelling, including Unicode and spaces, remains significant.

The private Core path separates parent-side database authentication/publication from
the observation on its existing connection. One owned observation thread performs its
local cancellation calls synchronously. Its external acceptance deadline cannot kill
a blocked native call; late or failed observations retain the operation slot until
local calls finish and independent remote cleanup is confirmed. A completed thread
alone is not cleanup proof. Unsupported thread-local multi-client contexts are refused.

Private Jobs control uses one owned Linux exec helper with bounded,
private JSON IPC and separate termination and reaping stages. The helper cannot publish
database eligibility, receive the manager's consumption nonce, or choose an arbitrary
Python callable from request data. The parent retains the exact reserved submission
and checks its current operation before publishing. Helper exit is not remote cleanup
or permission to retry an ambiguous submission. HTTP(S), GCS and `auto` use the
fixed preparation path. A `ray://` declaration first creates an isolated Client driver,
corroborates its native identity and dashboard endpoint, disconnects, and independently
observes that same driver as dead. Disconnect alone is insufficient, and a lost discovery
response keeps the operation quarantined. Native driver IDs never enter the Jobs stop
endpoint. Production workers call these adapters before protocol-3 claims. The final
release still requires matching deployed producer, recovery and cancellation evidence;
an adapter test alone does not qualify the integrated application.

The private Jobs parent binds one immutable prepared configuration to its exact lease
incarnation. Its selected addresses come from the same declaration snapshot as its
admission digests. Configuration replacement requires invalidation, confirmed cleanup
and a fresh manager incarnation. At most 64 alias records retain their nonce, reservation
and cleanup ownership even after failure. Only an independently inspected result from
the current helper can reach atomic publication; rereading the database cannot restore
positive qualification.

An unchanged declaration may discover a new session after the parent independently
confirms cleanup of its exact old probe Job. A private CAS service then retires only
that ephemeral challenge/receipt pair and issues a fresh challenge identity and nonce.
It preserves bindings, claims, target policy and sibling capabilities. The ordinary
same-configuration replacement restriction remains intact. Probe terminal confirmation
is not a report that ordinary tasks or the cluster are drained.

`eligible_aliases()` remains ACTIVE-only for first claims. A separate private
`qualified_aliases()` view exposes fresh DRAINING observations for task-specific
continuation filtering. Before applying the task limit, that query must prove prior
resolved claim history and the original same-target binding, then revalidate under
the authoritative claim locks. Merely having a binding, or an OPEN or HELD claim,
cannot authorize another generation.

The current transport binds the complete outer request separately from its
cohort claim. Nested work carries a compact claim and independently derived
digest, including the original membership digest. Point checks compare the actual
package, Ray/Python tuple, session, and current schedulable node set before Django
setup or application entry. An attestation's admission TTL is checked at claim
time; it does not become a deadline for RuntimeEnv setup or a long workflow.
Unknown observations and mismatches do not establish that earlier application
work or sibling leaves had no effects. They require fenced disposition and cannot
be converted into ordinary automatic retry.

The worker captures immutable Core/Jobs preparation inputs on its owning thread,
then runs filesystem scans and submission snapshots, uploads and remote calls in
owned callbacks. The parent alone commits prepared requests and dispatch ownership
to SQL before authorizing submission. It keeps heartbeats and completion polling
moving while those callbacks are pending, retains late Core handles even after an
uncertain response, and counts pending callbacks against capacity until actual exit
and required local cleanup. A cancellation acknowledgment, terminal database row or
local callback exit is not remote cleanup proof. Database operations and Sync
execution can still block the parent; this is not a blanket latency guarantee.

The Core connection has a retained creator thread. It publishes local readiness
without exiting, then runs exact disconnect on that same thread after the owner
has established probe and task quiescence. This preserves Linux parent-death
ownership for locally started Ray processes. Disconnect waits for those owned
processes, and the parent releases the connection ticket only after the creator
exits. Failed or late callbacks remain held; neither a deadline nor creator exit
alone proves remote cleanup.

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

- Linux is the supported execution target, including local Ray, clusters, and Kubernetes.
- Windows compatibility is best effort, observed only through a small advisory GitHub Actions
  Windows packaging/import lane. Native Windows/macOS execution is outside production and release
  certification. Full CI, broad test suites, and native-Ray validation must not run on non-Linux
  workstations. Focused resource-free checks and host-side tools remain usable there; use an explicitly
  bounded Linux environment for execution tests. No container or cluster is started automatically.
- Historical native Windows startup-abort investigation is retained in
  [ray-project/ray#65181](https://github.com/ray-project/ray/issues/65181). Keeping existing platform
  accommodations does not expand the supported execution promise; removal is sequenced after Linux
  replacement qualification in [#456](https://github.com/dariuszpanas/django-ray/issues/456).
- Ray publishes Linux aarch64 wheels for supported Python versions, but users must
  confirm their OS, architecture, and Python ABI match an available Ray wheel.

Compiled Graph is more restrictive than ordinary Ray use: policy version 3 rejects
Windows, aarch64, Ray Client, GPU transport, and every unverified native tuple before
calling `experimental_compile()`. Dynamic workflows remain supported according to the
version matrix above.
