# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Upgrade from 0.4.0

- Private flat workflow and target implementation imports have moved without compatibility
  shims while the project remains Beta. Replace `django_ray.workflow_plans` with
  `django_ray.workflow.plans`, `django_ray.admin_workflow_graph` with
  `django_ray.workflow.admin_graph`, `django_ray.workflow_output_previews` with
  `django_ray.workflow.previews`, `django_ray.workflow_progress` with
  `django_ray.workflow.progress.runs`,
  `django_ray.workflow_progress_storage.prepare_workflow_progress_topology` with
  `django_ray.workflow.progress.preparation.prepare_workflow_progress_topology`, other
  `django_ray.workflow_progress_<name>` imports with `django_ray.workflow.progress.<name>`,
  `django_ray.ray_target_probe` with `django_ray.target.probe`, and
  `django_ray.target_<name>` imports with `django_ray.target.<name>`. The public
  `django_ray.workflows` module and persisted workflow protocols remain unchanged.
- The `0019_execution_protocol_schema` migration supports the exact published 0.4.0
  baseline at migration `0018`. It records existing executions and attempts as protocol
  `1`, but cannot prove the contract of retained nonterminal work written directly by
  pre-0.4 code. Drain, cancel, or audit that work before applying `0019`; the migration
  number alone does not prove which writer created a row.
- The seeded rollout policy remains dormant at active write protocol `1`, with legacy
  worker admission open. A code-only rollback to exact 0.4.0 retains `0019`, `0020`,
  and the legacy database defaults after upgraded task managers are stopped and
  reconciled. Reverse `0020` and then `0019` only in a separate stopped-writer
  maintenance window; reversal drops the protocol, provenance, capability, policy,
  token, and database-fence metadata.
- A preparatory private revision-checked coordination primitive now proves the database
  transition needed by a later operator adapter. It accepts closure only after its
  caller asserts that capability-unaware producers are retired and every legacy worker
  lease is durably inactive. Closing detaches inactive lease history before deleting
  the admission token; reopening never revives those identities and refuses while any
  nonterminal execution uses a protocol other than `1`. Operators must keep legacy
  admission open in this slice: it adds no supported command, mutable Admin action, or
  protocol `2` activation surface.
- Migration `0020` makes that rollback boundary persistent: while legacy admission is
  open, PostgreSQL and SQLite reject non-protocol-`1` nonterminal inserts and
  terminal-to-nonterminal transitions. PostgreSQL reopening fences execution writers
  before policy evaluation; installation refuses an already-open policy containing
  incompatible nonterminal work.
- Migration `0021` adds a dormant, attempt-scoped Ray Job request-reference column and
  types the shared payload registry as `task_input` or `ray_job_request`. Its Python and
  database default remains `task_input`, so released writers that omit the new column
  continue to register task inputs. Applying the migration alone does not activate rq2.
  Before resuming reference-only submissions, configure shared retrievable storage,
  deploy the exact final rq2 reader, retire released 0.4.0 and intermediate rq1 task
  managers, and close legacy admission through its reviewed revision/producer-retirement
  fence. Already submitted legacy/rq1 protocol-`1` jobs remain drainable; rq2 does not
  activate protocol `2`.
- Migration `0022` adds only dormant Ray-target identity, immutable policy revisions,
  and verified attestation history. Published 0.4.0 code ignores the new tables, so a
  code-only rollback retains `0022`; schema reversal is a separate stopped-writer
  operation that refuses while retained target history exists. After export or audit,
  deliberate maintenance deletion leaves the destructive reversal path available.
  Applying the migration does not assign a target to work, advertise worker capacity,
  or activate routing.
- Migration `0023` adds an unseeded, create-once relationship from an execution to one
  immutable target-policy revision. A future target-aware consumer must treat absence as
  unbound and fail closed; current workers remain target-unaware and do not consult the
  table. The row's creation time is not proof of enqueue-time selection. Current code has
  no binding writer, reader, Admin, enqueue, claim, adoption, lifecycle, routing, or
  backfill consumer, and published 0.4.0 code ignores the table. Legacy binding remains
  forbidden until #381 supplies exact mapping lineage. Both foreign keys use `PROTECT`,
  so a retained binding blocks deletion of its execution and policy revision. Activation
  remains blocked until every task and policy retention or cleanup path defines and tests
  explicit ordering; deleting a binding first is never an implicit cascade or ordinary
  task-cleanup step. Reverse `0023` only in a stopped-writer maintenance window after
  exporting or auditing and deliberately deleting every retained binding.
- Migration `0024` adds bounded backend-alias route history and a separate, initially
  empty binding-to-route-revision selection table. The private coordinator may register
  or compare-and-set append route intent for an exact latest active Ray Core policy, but
  it does not probe Ray, write or read a task binding or route selection, or activate any
  enqueue, worker, claim, adoption, lifecycle, or runtime path. Route intent is not live
  attestation, capacity, claim authorization, or work placement; an absent selection is
  unproved provenance, not a default route. Legacy 0.4 mapping remains distinct and
  deferred to #381. Both selection parents are protected, so cleanup must delete the
  selection before its binding or route revision and must delete every revision before
  its route. Reverse `0024`, then `0023`, then `0022` only through their stopped-writer,
  empty-table maintenance paths.
- Migration `0025` adds one unseeded, ephemeral current-capability row per exact
  task-manager lease incarnation and Ray target. It snapshots the lease identity and
  manager Ray/Python tuple, references one exact target policy and verified attestation,
  and uses a bounded compare-and-set revision for renewal. Current Django ORM lease deletion
  cascades to the capability while raw parent deletion remains foreign-key restricted, so
  worker-ID reuse starts without inherited capacity. The private
  coordinator supports exact Ray Core capability for a fresh `active` or `draining`
  policy and proof; draining preserves only already-pinned work and never enables a new
  route or enqueue. Ray Job remains unsupported. No production path creates, renews,
  reads, or treats the row as capacity. Existing exact-lease deletion, including supported
  Admin inactive-lease cleanup, may only fail-closed cascade-withdraw an otherwise
  unreachable row. Presence alone is never authority. Policy and attestation revisions
  remain the audit history; future execution generations must archive their own
  authenticated target evidence. KubeRay is not applicable to this dormant database-only
  slice because no production producer can create a capability row; SQLite and PostgreSQL
  migration and coordination evidence remain mandatory.
- Migration `0026` adds unseeded, immutable per-generation target execution evidence and an
  optional immutable outcome. One claim binds the exact protocol-`2` execution generation and
  required route selection to its target, current same-target policy, claim attestation,
  capability, lease-incarnation, runtime, and canonical digests. Its one-to-one outcome can
  retain authenticated completion evidence, a proven compatibility rejection, or a future
  manager's durable `UNCERTAIN` disposition with null invocation state and no claimed observed
  proof. Its insert fence requires the exact `RUNNING` task, owner, attempt, generation, and route
  selection, while complete outcomes require `claimed_at <= observed_at <= recorded_at`.
  Transport, pre-claim or future-clock observations, and malformed uncertainty are never
  fabricated as remote compatibility rejection. No production writer or reader creates or
  consumes either table. Exact 0.4.0 ignores
  them during a code-only rollback; reverse `0026` only in a stopped-writer maintenance window
  after deliberately deleting every outcome and generation claim.
- A Django-free canonical evidence codec now covers every immutable per-generation claim snapshot
  with a domain-separated digest. The positive database evidence ID remains a separate control;
  protocol `2` binds the ID and digest together in both its request and observed proof. This
  package-private codec does not persist claims or authorize execution.

### Fixed

- Maintainer approval lifecycle and review events now replace one authoritative commit status instead
  of creating contradictory same-named check runs. The trusted publisher invalidates every affected
  head before evaluation and isolates shared-head recovery so cancellation for one commit cannot
  suppress another commit's result. At GitHub's per-context status limit it leaves either `pending` or
  an explicit failure as the latest state, requiring a new head instead of preserving stale success;
  delayed source events restore an already closed head once live association reads complete. The
  publisher runs as a repository module so its default-branch checkout can import the shared review
  policy during live workflow execution.
- Codex review gating now uses an immutable YAGA v2 action that separates lifecycle invalidation from
  quota-consuming requests. Exact-title native CI must succeed first; a failed prerequisite publishes
  a terminal gate error without requesting Codex. Owner-authored pull requests can receive one marked
  request directly, while every other author first crosses a protected, owner-only GitHub Environment
  whose exact candidate-bound marker authorizes the serialized request worker. Deterministic wake
  election prevents the CI and lifecycle completions from both writing or requesting. Bounded polling
  accepts exact-head connector comments, formal reviews, and pull-request-body `+1` reactions only
  when they are strictly later than the exact current-boundary Actions-owned YAGA request. The same
  request-first rule covers eyes reactions and the initial ready opened candidate; same-second or
  unsolicited evidence fails closed without a duplicate request. There are no schedule, comment,
  review, close, or merge-group publisher triggers. Post-merge `push` completions skip every publisher
  job before YAGA runs, so merging cannot start another Codex request.
- The staged YAGA v2 bootstrap adds native `CI Prerequisites` while retaining the currently required
  native `CI Gate` compatibility bridge. With `YAGA_CODEX_V2_ENABLED` absent or not exactly `true`,
  every v2 entry job skips and the pinned v1 publisher remains active behind the inverse condition.
  After the owner variables and protected environment are verified, enabling the flag renames the
  bridge to `Legacy CI Gate`, lets YAGA alone publish classic `CI Gate`, and starts the owner and
  external-author canaries. Automatic Codex reviews are disabled before activation, making YAGA the
  sole legitimate automatic requester from the first canary. Both publisher queues,
  protected-environment waits, and all outstanding provider review tasks must be cancelled or drained
  before either flag transition
  because changing a variable cannot revoke queued work. Activation also freezes new pull requests
  and auto-merge at zero open pull requests; a fresh post-flag canary creates the lifecycle boundary
  that a CI rerun on an older PR cannot supply. The expanded native contexts become required only
  after exact-head green evidence.
- The raw JSON `Review Policy Event` run title feeds Maintainer Approval and the inverse-gated v1
  publisher during bootstrap. After cutover it remains temporary transport only for Maintainer
  Approval, including close and displaced-head recovery, until a separate human-readable protocol
  replaces it. YAGA's v2 lifecycle never handles `closed`, and a direct human or app
  `@codex review` comment remains a provider-side quota loophole that repository workflows cannot
  prevent. Visible unsolicited connector activity fails closed without a duplicate YAGA request, and
  protected external approval never retroactively authorizes or reuses it; approval can authorize
  only a strictly later current-boundary marked request.
- Runtime dependency floors now require Django 6.0.8 and sqlparse 0.6.0, preventing fresh
  and locked installs from resolving versions covered by current security advisories. Minimum
  dependency and benchmark lanes exercise the same patched pair, while the Admin breadcrumb
  assertion accepts both Django 6.0 and 6.1's equivalent semantic markup.
- The test-only real-Ray ownership lock now opens its shared path without following symlinks or
  Windows reparse points and verifies a stable, owned regular file before writing diagnostics.
- The local KubeRay gate now validates its pinned v0.4.0 commit and tree even when tags were not
  fetched, while still rejecting a drifted tag when present, and Kubernetes URL helper targets now
  use POSIX-safe shell output syntax.
- Graceful-shutdown signals now wake adaptive worker polling within a bounded 100 ms slice, so a
  long idle backoff cannot postpone Ray cancellation handoff or lease cleanup.
- Malformed workflow plan-selection rows now fail with the bounded validation error used by
  observability readers, and signed pagination cursors expire whenever the stored summary revision
  advances so pages cannot mix publication epochs.
- The sample browser dashboard now keeps its verified bearer token only in loaded-page memory;
  reload starts unauthenticated, no token is written to browser storage, cookies, or URLs, and the
  exact storage entry used by older releases is removed without being read or restored.
- Docker build contexts now recursively re-exclude nested environment files, SQLite databases,
  and SQLite journal, WAL, and shared-memory sidecars, including after broad source inclusions;
  direct KubeRay deployment no longer removes a potentially unrelated local Kong release or routes.
- Commit-message validation now bounds mixed validation-evidence suffix scans, parses Markdown
  tables in one pass, rejects oversized generated counts and duplicate metadata markers without
  unbounded conversion or collection, and preserves leading blank headers for rejection.
- Coverage-debt tracker discovery now considers only maintainer-owned issues and the expected
  Actions bot's report comments, so public marker text cannot redirect or block the privileged
  monthly update while duplicate trusted markers still fail closed.
- Operational redaction now replaces sensitive-looking mapping keys with one fixed
  marker as well as redacting their values. Admin, API, structured logging, workflow
  previews, and Ray observability therefore no longer retain sensitive key text, and
  marker or normalized-key collisions remain fail-closed regardless of mapping order.
- The guarded local KubeRay gate now requires one exact shared rendered Ray head/worker image,
  discovers its canonical Python `3.12.X` patch by running that image's interpreter during the
  mutable images layer, and passes the exact `PYTHON_VERSION` to current and released-`v0.4.0`
  application image builds but not to `Dockerfile.ray`. This corrects the protocol-`2` probe
  precondition/runtime mismatch while the root `Dockerfile` and ordinary Compose path remain
  patch-flexible on `3.12`. Supported `py312` Ray image patch refreshes automatically rediscover
  the local image patch, while final live attestation remains authoritative over the actual pods
  and retains the exact Ray/Python tuple check before cold proof can pass.
- A mismatched Ray Jobs API submission return is now treated only as fixed,
  acceptance-uncertain evidence. Arbitrary, oversized, unhashable, or merely
  well-formed alternate values are neither retained nor logged and never become a stop
  capability for an unrelated job; reconciliation remains pinned to the durable rq2
  identity reserved before submission. Concurrent direct callers likewise cannot issue
  a second submission or clear another caller's exact reservation.
- Development checks remain compatible with the latest `ty`, and sample-only
  `django-unfold` handling now keeps its exact sample/dev and lockfile pin synchronized
  with the installed-version RuntimeEnv requirement and minimum-dependency CI, without
  stale duplicate literals.
- Required Codex review evidence now fails closed across synchronized heads, changed bases, drafts,
  ambiguous shared heads, displaced ownership, malformed observer provenance, and publication races.
  The publisher never treats an Actions comment or a lingering `eyes` reaction as proof of review;
  findings remain subject to native required conversation resolution.
- Contributor validation now uses fast static and focused pre-push checks plus one explicit full-gate
  checkpoint for executable package/runtime, dependency, packaging, build, CI-composition, release,
  break-glass, and required local KubeRay boundaries. Documentation-, test-, and PR/commit-metadata-only
  follow-ups no longer repeat the complete local suite; exact-head hosted `CI Gate` remains the broad
  merge proof.
- GitHub workflows now declare least-privilege token permissions, and manual TestPyPI rehearsals
  check out only the trusted default branch before proving its exact authorized candidate identity.

### Added

- A disjoint protocol-`2` Ray Core target-execution codec now binds each canonical request to a
  positive target-execution-evidence ID and digest, the canonical claim time, the selected
  target-expectation digest, and the exact claim-attestation digest. The package-private submit
  seam recomputes those controls from the complete canonical claim, verifies the exact running
  task/owner/route/generation/start and manager-runtime lineage, and requires the recorded
  attestation to bracket the claim before crossing Ray. The remote boundary compares the canonical
  expectation and full-node claim attestation, then takes a fresh bounded resource-state snapshot.
  Exact current schedulable
  node-ID-set equality plus the executing node's current session/runtime are required before
  Django setup, input loading, or application import. The result proof must echo the request-bound
  claim time. Exact proof returns a `completion`; only a fully observed mismatch may return
  `compatibility_rejection` with `application_invoked=false`. Missing observation, malformed
  result, transport failure, and an observation outside the canonical claim-to-manager-receipt
  interval remain runner uncertainty for future durable `UNCERTAIN` handling. Production remains
  on active write protocol `1` with `1..1` package support and
  worker leases: no backend enqueues protocol `2`, no worker claims it, no capability producer
  creates its generation evidence, and Ray Job remains unsupported.
- Required `Maintainer Approval` and `Codex Review` merge checks now let the repository owner merge
  without self-approval while requiring the owner's current-head approval for every other author, a
  trusted current-candidate Codex outcome, and native resolution of every review
  conversation.
  Their staged ruleset activation also requires strict status freshness and separate owner plus
  external-or-bot canaries before rollout is considered complete.
- A Django-free, versioned target-attestation contract now defines bounded canonical
  target expectations, exact Ray/Python runtime tuples, per-node observations, and a
  before/after resource-state boundary. Its dormant Ray 2.56.0 probe hard-pins one
  zero-CPU observation to every live schedulable node and requires the exact cluster
  session and node set to remain stable while resource-state counters do not regress.
  The counters are recorded as an observation interval because ordinary heartbeats
  advance them; they are not claimed as a membership epoch. This slice does not add
  persistence, worker capability, claim or routing changes, and it does not claim a
  Ray Job response channel or activate blue/green handoff.
- A private dormant persistence coordinator now registers immutable exact Ray Core
  targets in `draining`, appends revision-checked `active`/`draining` policy intent, and
  records only canonical attestations that verify against the exact current policy. Ray
  Job persistence remains unsupported. Expiry status is derived from the immutable
  observation window; mismatch, unreachable, identity-drift, malformed, and expired
  outcomes never become fabricated negative history. The `retired` transition remains
  reserved for #368, and this slice adds no task, lease, claim, routing, status, renewal,
  or activation behavior.
- A dormant task-target binding schema can retain one create-once execution-to-policy
  selection relationship for a later enqueue writer. It is initially empty, has no
  production consumer, and never turns a historical policy state into target capacity or
  claim authorization. Its protected parent relationships require an explicit tested
  audit and retention order before any writer or cleanup integration can activate it.
- A dormant backend-alias route substrate retains immutable append-only route revisions
  selected through revision-checked database coordination. A separate create-once
  route-selection schema can preserve the exact route revision for an existing task
  binding while enforcing target-policy equality. No package path writes or reads that
  task provenance, and the route coordinator supplies intent only, not attestation,
  capacity, a worker capability, or runtime routing activation.

- The guarded local KubeRay final gate now certifies the supported task-manager rolling
  boundary with a manager built from the pinned released `v0.4.0` tree and the exact current
  candidate. A released capability-schema-`0` manager submits one slow protocol-`1` Ray
  Job; the current explicit schema-`1`, `1..1` manager must adopt the same persisted job,
  attempt, and generation without resubmission. A second deferred protocol-`1` row must
  remain byte-for-byte queued across that replacement and then complete through one current
  request-reference submission. A separate protocol-`2` fixture is terminal-staged while
  admission remains open, moved to `QUEUED` only after a revision-checked close, and required
  to remain unchanged and visible as unsupported before a direct strict Ray Core executor
  request rejects it prior to application invocation with its unique marker absent. Active
  write protocol remains `1`; no protocol-`2` writer or live `1..2` capability is activated.
  The same cold cluster separately proves the package-private protocol-`2` target boundary:
  exact target evidence completes, while a fully observed mismatch produces an authenticated
  compatibility rejection without invoking the marker callable.
  Passing evidence waits for exact fixture cleanup, legacy admission reopened with a
  consistent token at its next revision, removal of the ephemeral release Deployment, and
  restoration of the rendered current-manager replica count. A later run may reclaim only
  exact reserved, unambiguous interrupted gate residue. Missing ownership, foreign residue,
  an orphan live lease, or ambiguity fails closed. Recovery runs before any live task layer
  and repeats immediately before handoff certification.
- External payload retention now distinguishes durable task-input envelopes from Ray
  Job execution requests, follows the kind's exact execution-reference column, and
  retains unknown, wrong-kind, or dual-column ambiguity. Explicit retry and fresh claim
  clear the prior Ray Job tracking tuple; automatic retry retains its job ID, address,
  and request reference together until that fresh claim. Terminal, cancellation, and
  manager-loss state likewise retains the tuple for audit and cleanup. Ordinary Admin
  projections keep the opaque request reference deferred and undisplayed.

- The PostgreSQL polling benchmark now emits additive, bounded schema-v1 evidence for
  the execution-protocol claim predicate. It intercepts and shape-verifies the actual
  production priority claim SQL, compares it with a control that removes only the
  protocol range over the same exact active-write-protocol rows, counterbalances 12 timing pairs,
  and retains fixed-vocabulary `EXPLAIN ANALYZE` summaries plus signed p50/p95 deltas.
  Raw SQL and durable identifiers are omitted, existing polling result keys remain
  unchanged, and timings remain evidence rather than a flaky threshold. Portable and
  PostgreSQL coordination tests also prove that a `1..1` worker excludes protocol `2`
  claims and Ray Job reconciliation while a synthetic `1..2` worker processes both;
  production still advertises only protocol `1`.

- Bounded task-status, execution-list, execution-detail, and Django Admin reads now
  expose the immutable execution protocol, nullable creator/manager/executor package
  provenance, and whether a valid heartbeat-live lease can read that protocol. One
  frozen cutoff and SQL `EXISTS` annotation avoid per-row lease reads; package
  provenance is guarded at 128 UTF-8 bytes before transfer and presentation-redacted.
  Every public example marks `queue_capacity_attested=false` because lease queue text,
  available concurrency, Ray/Python compatibility, Ray readiness, and cluster identity
  remain unattested. Prometheus adds exactly 16 fixed
  `django_ray_tasks_by_execution_protocol_total{protocol=1|other,state=...}` series,
  including zeros, without package-version labels.

- New Ray Job submissions now use the rq2 request-reference carrier. The manager builds
  the same bounded canonical execution request from durable JSON or an opaque input
  reference, stores and registry-attaches it before opening the Ray client or uploading
  RuntimeEnv artifacts, and puts only a bounded canonical locator in
  `--request-ref-b64`. Independently bounded metadata contains fixed markers, an opaque
  coordination digest, execution protocol, request digest/size, request-reference hash,
  and exact canonical locator-token hash; it omits the public task ID, callable,
  arguments, raw RuntimeEnv identity, and credentials. The driver validates the whole
  locator binding before storage I/O and the
  loaded canonical bytes before Django setup, input hydration, or application import or
  invocation. Deterministic preparation/validation failures use fixed diagnostics and
  no automatic replay; definitely pre-submission storage outages may retry, while any
  uncertain submission retains its exact job ID/address/reference tuple. Concurrent
  public submissions share one durable reservation: only its owner may submit,
  while contenders receive a fixed uncertain result for reconciliation. Compatible
  managers selected for that execution's queue can adopt and reconcile the tuple;
  out-of-queue managers exclude it from orphan reconciliation, timeout recovery, and
  cancellation takeover before Ray status/stop I/O. Retention purges request objects
  only through the existing registry-first lock order; rq2 activation must first upgrade
  or disable older purge invocations that do not understand the request-reference
  column. Released
  unversioned payloads and rq1 remain protocol-`1` drain adapters only. This does not
  attest Ray/Python or cluster identity.
- Strict outer tasks now propagate one canonical bounded execution request through
  workflow steps, result-fold actors, and distributed map, starmap, and scatter leaves.
  Exact outer identity/protocol, boundary identity, primary and optional preview
  callable paths or opaque-byte digest, and checksummed RuntimeEnv plan identity are
  validated before Django setup and application callable import, django-ray
  `pickle.loads`, or invocation. Partial strict controls cannot downgrade to the
  released direct-call path; validated contexts remain strict through deeper nesting. A
  typed mismatch becomes a fixed outer non-retryable completion without a remote
  traceback because sibling leaves may already have effects.
  Ray has already deserialized the bootstrap and ordinary arguments at that point, so
  exact Ray/Python and cluster-instance attestation remains a separate pre-submission
  requirement. This completes the unreleased 0.5 explicit protocol-`1` contract;
  intermediate development snapshots that advertised that protocol are not a supported
  rolling cohort and must be drained before the exact final candidate.
- A read-only `django_ray_protocol_status` command now emits bounded text or canonical
  versioned JSON for the rollout policy/token relationship, live and stale-active lease
  capability aggregates, nonterminal protocol groups, protocol-only unsupported work,
  work lacking a live explicit upgraded reader, and fixed closure/rollback blockers.
  It reads one consistent database snapshot, bounds queue text before materialization,
  never mutates rollout state, and exposes no durable identities. Queue capacity, Ray
  readiness, Ray/Python or cluster identity, and capability-unaware process retirement
  remain explicitly unattested.
- A schema-first durable execution-protocol boundary records immutable protocol `1` on
  executions and attempts, advertises upgraded worker support as the bounded range
  `1` through `1`, and installs a dormant ownership fence for future protocols and the
  post-legacy boundary. Read-only Admin surfaces expose the singleton rollout policy,
  execution and attempt provenance, and worker capability ranges. Integer protocol
  versions are normative; package Semantic Versions remain diagnostic provenance only,
  and no protocol `2` writer or policy activation surface is enabled.
- Upgraded task managers now filter queued expiry and claim selection by the inclusive
  range on their exact locked worker lease, then recheck the locked execution before
  adoption or mutation. Unsupported rows remain unchanged without being mistaken for
  lease loss. The informational lease queue text and protocol capability do not attest
  per-queue capacity, Ray/Python compatibility, or target-cluster readiness.
- Ray Job reconciliation, stuck/timeout recovery, and cancellation processing now
  capture the exact live lease range before global execution scans. Unsupported active
  tracking is retired locally before a status, log, or stop call, while the durable
  execution and its source lease remain available to a compatible worker cohort.
- Package-owned producers now persist explicit metadata schema `1`, execution protocol
  `1`, and creator provenance. Compatible claims/adoptions stamp attempt-scoped manager
  provenance; terminal archival copies the exact protocol and manager/executor values,
  while retry preserves task-chain identity and clears those current attempt fields.
  Historical and unreported provenance remains null rather than being inferred.
- Package-owned cancellation, retry, expiry, and terminal lifecycle services now check
  the immutable execution protocol under the row lock before RuntimeEnv hydration,
  attempt archival, cancellation effects, or durable mutation.
  Unsupported cancellation/retry requests return a distinct bounded status, Admin
  reports them separately, and other transitions leave the row unchanged.
- Ray Core monitoring now admits only exact pending handles whose durable owner,
  attempt, generation, state, and protocol match the manager's live locked lease.
  Unsupported or stale handles retire locally before task-specific Ray polling, bulk
  monitor heartbeats include the protocol fence, and completions plus reconnect loss
  handling re-enter the lease-then-execution boundary before storage or lifecycle
  effects. This does not make driver-owned `ObjectRef` handles transferable.
- Completion consumers now support both unversioned protocol-v1 records and a strict
  flat versioned-v1 envelope carrying the exact task identity, protocol, and bounded
  executor provenance. Any reserved versioned field disables legacy fallback. Schema,
  protocol, identity, or shape mismatch cannot reach result storage or automatic retry;
  accepted executor provenance is archived atomically with the terminal attempt. A
  fixed byte/depth/node budget rejects deterministic resource-limit violations without
  replay while preserving released legacy non-finite results and long diagnostics
  within that whole-envelope boundary.
- New Ray Core submissions now carry one canonical bounded request sourced from durable
  JSON or an opaque input reference, plus independent expected task and protocol
  primitives. The by-value executor bootstrap rejects malformed, unsupported, and
  mismatched requests with a fixed non-retryable enriched completion before Django
  setup, input hydration, or application callable import/invocation. Released
  positional submissions remain the protocol-v1 compatibility path. A strict handle
  that returns no executor envelope becomes a fixed non-retryable transport failure
  without remote exception text or executor provenance. This does not attest Ray/Python
  or cluster identity.
- PostgreSQL and SQLite rollout coordination now serializes exact-0.4 execution and
  lease inserts, legacy heartbeats, and concurrent policy transitions without using
  package Semantic Versions as compatibility evidence. Callers must provide the
  policy revision they reviewed; stale revisions fail without partial mutation. A
  PostgreSQL advisory mutex serializes coordination calls, and redundant reopen checks
  avoid an execution-table lock while legacy workers remain admitted.

## [0.4.0] - 2026-08-03

### Upgrade from 0.3.1

Before starting 0.4.0 task managers:

- Pause producers so no new rows are added, and preserve queued rows for the `0016`
  policy review instead of submitting them merely to complete the upgrade. Quiesce new
  claims while already claimed Ray Jobs and active workflows finish and reconcile, or
  stop the old managers and verify remote quiescence before retrying uncertain work.
  Then stop every old task manager and workflow coordinator. Do not start 0.4.0
  processes yet.
- Back up the database. With writers stopped, run migration `0015`'s duplicate-ID
  preflight on a production-sized staging copy and budget for the unique-index build.
  Preview the queued backlog before crossing migration `0016`; decide whether existing
  queued work should receive the 24-hour default deadline or the deliberate
  `DJANGO_RAY_EXISTING_QUEUED_UNLIMITED=1` opt-out.
- Upgrade task managers, the Ray head, and every Ray worker together to Ray 2.56.0 or a
  newer compatible release. Keep task managers and the cluster on the same Ray version
  and Python minor version; do not operate a mixed Ray minor-version cluster. See
  [Compatibility and Version Policy](compatibility.md#supported-versions).
- Deploy 0.4.0 code to every producer, web/admin/retry process, task manager, and Ray
  Job or RuntimeEnv execution environment while those processes remain stopped. Apply
  django-ray migrations `0007` through `0018` with
  `python manage.py migrate django_ray`, using the chosen `0016` backlog policy. Start
  only the 0.4.0 fleet after every enqueue writer and task manager is upgraded; do not
  run old and new writers, task managers, or workflow coordinators together.
- Keep input spillover disabled until old Ray Job drivers are drained, and follow each
  feature's reader-first activation guide before enabling input spillover or schema-v3
  detail writes.
- For RuntimeEnv encryption, deploy dual-read code and distribute the retained keys to
  Django producers, task managers, retry APIs, and admin readers while writes remain
  plaintext. Do not give database-encryption keys to generic Ray nodes. Enable encrypted
  writes only after every required Django reader is ready.
- Keep schema-v3 workflow detail publication default-off. The schema-v2 coordinator
  remains the supported live writer; the stricter schema-v3 path is still an opt-in
  pilot.
- Drain pre-`0014` managers before relying on backend-alias Ray Job targets. Drain all
  targeted tasks before reversing migration `0014` or rolling back to code that does
  not preserve the immutable target.
- Before a code rollback, stop new workflow coordinators and drain active workflows.
  Retain migration `0018` during that rollback; reverse it only in a separate
  stopped-writer maintenance window because reversal drops allocation metadata.

See [Queue expiration](tasks.md#queue-expiration),
[Durable Input Storage](reference/input-storage.md#rolling-upgrade),
[Runtime Environment encryption](runtime-environments.md#roll-out-encrypted-writes),
and the detailed [migration notes](#migration) before enabling their opt-in writers.

### Development scope

- Native Windows Ray remains a best-effort development boundary; Linux is the production
  target. Real-Ray pytest sessions now take an OS-released host-wide lock across processes
  and worktrees so concurrent local clusters cannot contaminate development evidence. The
  guard fails before test execution instead of waiting, retrying, or skipping coverage.
- Supported execution remains synchronous, dynamic Ray Core, and Ray Job. The current
  unreleased development line adds durable priority scheduling, bounded worker polling,
  opt-in durable oversized inputs, coroutine tasks, workflow plan and bounded map/reduce
  improvements, and authenticated observability without changing those default execution
  paths.
- Compiled Graph compatibility policy, lifecycle, probe containment, and the guarded KubeRay
  pilot ship as experimental, default-off groundwork. No capability row or native product
  execution is enabled. Public native candidate jobs and the scheduled hosted canary were
  removed; public CI is hermetic, and native evidence is produced only by the guarded local
  KubeRay pilot. A functional native probe does not make the current profile promotable because
  Ray 2.56 teardown still violates the residual-resource invariant.
- Schema-v3 workflow-progress readers and storage are present while the production coordinator
  remains a schema-v2 live writer. A default-off, stricter pilot can additionally publish one
  admitted terminal snapshot; the bundled testproject and guarded local KubeRay gate enable and
  prove that path. Static-actor and Compiled Graph workflow strategies and a resident
  prepared-graph cache remain inactive.
- Full workflow progress remains the default. Terminal-only reporting is an actor-free,
  summary-only option; it does not enable the default-off full-detail schema-v3 pilot or
  close the remaining #79 full-mode scale boundaries. Full mode now bounds each leaf's
  application-progress producer state, while aggregate admission across forked handles
  remains open before a sampled policy can be offered.

### Added

- A checked-in security policy and issue-reporting guidance now route suspected
  vulnerabilities to GitHub's private reporting form, define the pre-1.0
  latest-release support boundary, and keep actionable exploit evidence and
  secrets out of public issues while a fix is being developed.
- Django Admin execution and archived-attempt details now offer a separate **Sensitive
  data** action to superusers or operators who hold both ordinary object
  view and the new `django_ray.view_sensitive_task_data` permission. The default page
  remains redacted. The GET-only response exposes only pattern-unredacted, terminal-inert
  projections of the authorized stored task inputs and selected execution/attempt outcome
  (including execution cancellation errors), autoescapes HTML, disables caching, rejects
  per-field values above 64 KiB in SQL, enforces a 4 MiB rendered-response ceiling, and
  never includes RuntimeEnv, completion, workflow-progress, or log payloads. Stock Django
  Admin and Unfold are both covered, with explicit action affordances, readable
  light- and dark-theme diagnostic cards, and a matching standalone response-limit
  fallback that omits Admin branding and stored diagnostic payloads.
- Workflow steps can opt into a strict operator-facing output projection with
  `step(...).with_output_preview(projector)`. Full-reporting Ray leaves validate,
  redact, and fence a small exact-JSON value before terminal schema-v3 node detail is
  published; the Admin labels it as a preview in the card's single **Output** row. Raw
  results are never inspected by default, preview failures cannot change task success,
  actor-free and local policies do not run projectors, and stored node-detail schema v1
  remains readable. The private Admin graph envelope advances to schema version 2 for
  the new exact node field. The preview is explicitly diagnostic rather than a result,
  checkpoint, retry input, selective-resume marker, or external-effect receipt.
  Stored previews are authenticated before current redaction policy is applied; a
  newly sensitive current or archived value becomes one `REDACTED` marker without
  rewriting durable bytes or degrading the whole graph. The real showcase now proves
  exact validation and reservation map projections, a non-fatal projector failure,
  and `UNAVAILABLE` output on the failed reservation leaf. Process-control exceptions
  continue to propagate across every diagnostic seam.
- A bundled, application-owned Ray Data batch-job golden path now demonstrates one
  coarse Ray Job from a server-rooted immutable JSON Lines input through a deterministic
  bounded `map_batches()` transform to attempt-scoped Parquet output and a create-only
  artifact manifest. Deployment identity plus the durable task UUID isolate databases
  that share storage; output byte/file limits run before Parquet parsing and the exact
  content identity is revalidated afterward. Same-attempt replay is immutable, later
  retries use new namespaces, and missing, changed, linked, sidecar, or partial output
  fails closed. The manifest is explicitly `artifact_complete`, not durable task
  success: application adoption additionally requires the matching execution row to be
  `SUCCEEDED`, which rejects the tested post-manifest failure window. The sample now has
  a dedicated backend/queue enforced through default-off Ray Job worker affinity,
  explicit `udf_modifying_row_count=False`, and a real management-worker probe on Ray
  2.56 across supported-minimum and newest Python. The probe now injects a
  post-manifest failure, observes django-ray archive and resubmit the durable task as a
  second distinct Ray Job, rejects the first orphan, and adopts only the successful
  fenced artifact. Retained Job metadata proves both successful outer transports while
  archived Django attempts remain the application-level failure/success authority.
  Documentation records the trusted-storage, immutable-input,
  permissions, process-atomic publication, and non-power-loss-durable boundaries rather
  than claiming database/filesystem atomicity, checkpoint/resume, or exactly-once.
- Full-reporting workflow leaf invocations now keep at most one outstanding
  application-progress acknowledgement and one canonical latest-value replacement.
  Slow acknowledgements coalesce replaceable progress locally, leaf exit makes at most
  one bounded latest-value handoff, and structural plus `STARTED`, `COMPLETED`, and
  `FAILED` evidence remains ordered and uncoalesced. `report_progress()` returning
  `True` means the validated value entered this best-effort session, not that the actor
  processed or Django persisted it. One fixed-shape secret-free producer report per
  participating leaf invocation lets the actor aggregate offers, submissions,
  supersession, local drops, producer-observed acknowledgements, and terminal-handoff
  outcomes.
  Full reporting remains acknowledgement-driven rather than time-sampled; aggregate
  multi-handle mailbox admission is still required before adding a sampled policy.
- An opt-in testproject/local-KubeRay workflow reporting benchmark now runs the
  same tiny nested workflow sequentially under full, terminal-only, and disabled
  policies with counterbalanced order. Benchmark-report schema 3 uses secret-free JSON
  to separate durable server timing, useful leaf work, processed actor ingress,
  logical progress storage, and shared lifecycle bytes. Full-mode snapshots now add
  fixed-shape saturating actor-observed cost evidence for received logical
  calls/bytes, exact-run-fenced decoded event kinds, processed delivery delay,
  ingest handling, and snapshot building without another RPC or database write.
  It also aggregates the producer reports with explicit count units and reconciles
  offered/submitted/terminal outcomes. Structural and lifecycle calls, actual network
  traffic, true aggregate mailbox depth, complete actor-lifetime resources, and
  physical PostgreSQL attribution remain explicitly outside the measurement. The
  report retains bounded executions for Admin inspection by default and can delete
  only its exact owned rows after successful validation.
- Opt-in authenticated encryption for durable RuntimeEnv snapshots, using strict
  AES-256-GCM envelopes with either a dedicated rotating key ring or an explicit
  Django-secret fallback. Readers remain plaintext/encrypted compatible; encrypted
  envelopes fail closed on corruption, unknown keys, or identity mismatch before Ray
  submission. Dual-read compatibility does not authenticate row provenance against a
  database writer. Rollout, retention, downgrade, and threat-boundary guidance
  accompanies a local KubeRay configuration that exercises encrypted writes on Django
  processes only.
- Workflow progress now crosses Ray through one canonical, identity-fenced bytes
  envelope. Producers redact and cap every event before submission, dependency
  edges are chunked, and the collector revalidates the complete run identity while
  bounding nodes, edges, recent events, retained bytes, and diagnostic counters with
  the durable V1 limits. The actor exposes only `ingest`, `snapshot`, and `disable`;
  aggregate mailbox admission, coalescing, and default schema-v3 production remain
  follow-up work.
- A boolean `WORKFLOW_PROGRESS_SCHEMA_V3_PILOT` setting, disabled by default, applies
  the `schema-v3-pilot-v1` profile to actor collection and one terminal publication:
  512 nodes, 2,048 edges, 2 MiB of topology, 1 MiB of node detail, and 4 MiB combined,
  with encoded and decoded byte ceilings. The adapter revalidates the pinned plan,
  complete actor evidence, and exact run fence, then uses the existing bounded storage
  primitives to atomically publish summary, topology, and detail. Rejected or
  truncated ingress, malformed or over-limit evidence, preparation truncation, stale
  ownership, and storage failure all refuse publication with bounded diagnostics and
  without changing the workflow result. Failed runs also wait within the existing
  bounded terminal flush deadline until every transitive prerequisite of each failed
  node is reported succeeded, preventing cross-sender actor delivery from freezing a
  causally impossible graph. Schema-v2 live writes remain the rolling compatibility
  path. The bundled testproject enables the pilot through
  `DJANGO_RAY_WORKFLOW_PROGRESS_SCHEMA_V3_PILOT`, and the guarded local KubeRay gate
  proves non-empty mutually consistent summary, topology, edges, and node detail from
  the real producer.
- Terminal schema-v3 workflow publications now have an accessible, lazy Django admin
  execution graph. Its private GET-only adapter projects only bounded redacted display
  fields, refuses partial or incoherent data, caps reads at 100 nodes, 256 edges,
  100 details, and 128 KiB, pins node links to the displayed attempt, and highlights
  failure origins with their incoming ancestor paths. Raw result, callable, argument,
  RuntimeEnv, execution, metric, event, and plan data never enter the graph response.
  The package renderer extends stock Django Admin with scoped fallback styles and does
  not depend on the testproject-only Unfold shell.
  The guarded KubeRay gate proves successful and deterministic first-attempt failed
  workflows through the schema-v3 readers and authenticated admin projection.
- A realistic testproject order-fulfillment workflow showcase now exercises repeated
  split/join stages, dynamic item validation and reservation, commercial sibling work,
  and three fulfillment sinks before finalization inside one durable execution. The
  Admin presents its graph
  in derived longest-path layers with per-node detail links. An explicitly selected,
  one-user Locust scenario submits successful three-item runs sequentially, while the
  guarded local KubeRay gate proves a 21-node, 28-edge, 12-layer one-item success and a
  deterministic single reservation failure with useful successful-ancestor context
  and pending descendants.
- A separate deterministic recovery showcase now preserves one outer task across
  three exact attempts: an early failure, a mid-workflow failure after successful
  upstream work, and a complete success. Its polling API returns bounded ordered
  attempt outcomes. The parent Admin's **Workflow execution** boundary stacks the two
  failed graphs and current successful graph in chronological order, with each attempt
  independently collapsible and the current attempt rendered exactly once. Opening an
  archived panel performs one exact attempt-scoped read, caches either its graph or
  bounded unavailable state for that page, and never follows the current run. The
  guarded local KubeRay gate proves distinct run identities with one stable workflow
  plan. The example makes the current
  replay-from-entry boundary explicit: progress is
  diagnostic rather than a checkpoint, leaf outputs are not retained in node detail,
  and replayed application steps must be idempotent. The route explicitly selects a
  dedicated `recovery-showcase` RuntimeEnv profile, fails closed if that backend,
  profile, archive, or immutable identity is unavailable, and the guarded gate proves a
  bounded content-addressed source-and-dependency bundle on generic Ray images instead
  of treating the sample project's mutable package profile as retry-safe.
- Ray Client task managers can now package readable local per-task `working_dir` and
  `py_modules` paths into Ray's content-addressed package store. This brings the same
  local-code path behavior to direct and Ray Client task managers while preserving the
  durable workflow plan's pre-upload content identity.
- Ray-native workflows can keep the default full node-progress reporting, select
  terminal-only reporting, or disable progress globally and per invocation.
  Terminal-only runs create no progress actor, node/application-progress RPC, legacy
  `progress_data` write, topology, or node-detail row. Durable success or failure makes
  one best-effort fenced schema-v3 summary publication containing pinned plan identity,
  declared counts, terminal outcome, and bounded timestamps while explicitly reporting
  zero discovered nodes and `OMITTED_BY_POLICY` detail. API and Admin readers present
  that terminal summary without topology, node-detail, or graph links. Disabled runs
  retain the outer-task lifecycle and bounded plan/strategy metadata without making the
  terminal summary attempt. The testproject and guarded local KubeRay gate exercise
  terminal-only success and deterministic failure without changing the existing
  full-reporting fixture default.
- A practical Celery migration and coexistence guide that separates direct Django
  Tasks mappings from semantic rewrites and unsupported broker behavior, provides
  copyable enqueue/result-storage recipes, and requires producer-first Celery drain
  evidence before service removal.
- A manual and monthly line-coverage debt report now records exact JSON and Markdown evidence,
  classifies every uncovered range, and idempotently updates one marked GitHub tracker comment while
  preserving the existing global, worker, Ray Job, and testproject coverage floors.
- A guarded, repeatable local Docker Desktop/Kind KubeRay final integration gate with a checked-in
  trigger matrix, one commit-bound archive for rendering and deny-by-default Docker contexts,
  private digest-checked and credential-redacted kubeconfig routing, sanitized subprocess routing,
  a local-only explicitly pinned Docker endpoint, namespace and context confinement, fail-closed
  setup before workload reconciliation, UID-owned namespace-wide application inventory, retained
  Ray pod UID/container/image identity, exact application/Ray topology and image contracts, owned and
  timeout-bounded cold Ray replacement, redirect/proxy-safe bounded API task smoke, live image-ID,
  probe, RuntimeEnv, and Prometheus checks, bounded redacted diagnostics, and explicit
  data-preservation guarantees. Prometheus discovery RBAC is now namespace-scoped to match the gate
  boundary.
- Canonical, bounded, secret-free effective workflow plan snapshots with SHA-256
  identities, per-step RuntimeEnv resolution, deterministic strategy diagnostics,
  retry pinning, future-owner cache-key and stale-owner validation helpers, and
  durable observability independent of node progress. Current workflows still execute
  through local or dynamic Ray-task
  strategies; static actors and Compiled Graph remain guarded future strategies.
  RuntimeEnv and strategy metadata use strict versioned envelopes, stale fenced runs
  fail before side effects, and local step code is snapshotted and rebound through
  Ray's package store after fenced plan pinning and before leaf submission. Local paths
  remain dynamic-only until Ray delivery can be strongly verified. Reusable eligibility
  requires an explicit or detected immutable application revision, while the current
  local and dynamic-task baseline remains compatible without one. Oversized legacy
  DAGs retain that baseline through a bounded, fingerprinted dynamic-only overflow
  snapshot instead of turning plan storage limits into execution limits. RuntimeEnv
  profile names remain transported diagnostics rather than semantic identity, build
  and container-image revisions are composed independently, and Compiled Graph
  compatibility uses the shared policy-v3 runtime, owner, submission, and channel
  decision. Retry safety is tracked separately from reusable-strategy eligibility:
  later attempts fail closed for opaque or mutable RuntimeEnv bindings that collapse
  under secret-free redaction, while content-hashed local paths and declared revision
  contracts remain retryable. No resident prepared-graph cache or drain loop is
  implemented yet.
- Opt-in bounded workflow `map_step` admission with lazy generator consumption,
  incremental ordered collection, expansion guards, aggregate progress, and bounded
  dependency-aware failure cleanup. Ordered result bytes still materialize in the
  workflow coordinator; bounded in-Ray aggregation is tracked in
  [GitHub issue #91](https://github.com/dariuszpanas/django-ray/issues/91).
- An explicit bounded-map result buffer can now retain versioned `ray.cloudpickle`
  bytes in a non-detached, resource-accounted Ray actor and forward one direct ordered
  payload reference to a downstream Ray step without coordinator decoding. Caller
  limits, the narrow placement/resource surface, actor lifetime/restart semantics,
  and the two-return protocol are fingerprinted for durable retry drift. Actor execution
  remains serial and the coordinator resolves every acknowledgement before issuing the
  next call; `max_pending_calls=2` only leaves one sender-side bookkeeping slot for Ray
  to retire the prior completed call. Because this result-buffer work remains unreleased
  and the exact bound is already fingerprinted, the change updates pre-release plan
  identity without a plan- or protocol-format version bump. Terminal and final-consumer
  memory remains O(total output); chunk/reducer transport and production benchmarking
  remain tracked separately.
- Bounded maps can now opt into a strict input-order `reduce()` contract backed by one
  resource-accounted Ray actor. Incorporation-based admission bounds out-of-order
  retention, the coordinator forwards mapped values without decoding them, and one
  direct accumulator reference replaces the complete intermediate list. Reducer
  callable and RuntimeEnv identity, initial binding schema, byte and item bounds,
  actor placement/lifetime, ordering, credits, and finalization semantics participate
  in durable plan identity; local execution remains actor-free.
- Native Django task priority scheduling with durable `-100` through `100` priorities,
  higher-value-first claims, and FIFO ordering within equal priorities.
- Validated worker polling intervals, bounded jittered idle backoff, and a PostgreSQL
  fixed-versus-adaptive polling benchmark command.
- Opt-in durable references for oversized JSON task inputs, with versioned envelopes,
  filesystem/S3/GCS retrieval, immutable retry reuse, and retention-safe cleanup.
- Versioned package observability services, bounded-cardinality Prometheus metrics,
  byte-bounded live Ray logs, and authenticated live task updates in Django admin.
- Compact, lazy workflow-execution diagnostics in Django admin replace inline plan JSON,
  verify the plan fingerprint and selection schema before presentation, bind schema-v3
  progress to the validated fingerprint, selected strategy, and reporting policy, explain
  bounded-progress availability, reveal topology actions only for useful retained
  collections, and keep complete redacted diagnostics behind authorized byte-bounded
  downloads.
- A Django Unfold-themed bundled testproject admin that shares the documentation icon and type
  treatment, adopts the landing page's sky/slate palette and graph artwork, and provides compact
  execution/attempt layouts. Reproducibly pinned development, sample-image, and RuntimeEnv
  dependencies, django-ray-specific navigation, authenticated branded-admin/static smoke coverage,
  and an optional package-side `ModelAdmin` fallback keep Unfold out of the published package's
  required dependencies.
- Coroutine-based Django tasks across sync, Ray Core, and Ray Job modes, with
  per-invocation event loops and preserved retry exception classification.
- A Ray Serve integration boundary that defers package orchestration, keeps deployment
  state separate from durable tasks, and records adoption gates for a companion package.
- A Compiled Graph ownership ADR and opt-in raw-Ray topology probe that select one
  Ray Core outer task as the initial local/direct within-run CPU-pilot owner without
  claiming production Ray Client transport or cross-schedule reuse.
- A source-bound Linux/KubeRay Compiled Graph pilot profile with digest-pinned Ray and
  KubeRay inputs, fixed CPU/shared-memory/object-store resources, contained direct and
  nested-owner probes, one-shot result and teardown checks, exact running-image
  verification, a bounded tracked-only Git archive build context, checkout-independent
  strict UTF-8 configuration and policy identities, actively capped and process-tree-contained
  subprocesses, create-response namespace and create-only RayCluster leases with unique run
  tokens and UIDs, lease-bound pod ownership, exactly bracketed KubeRay
  controller/container/readiness/restart evidence, independently pinned `fastrlock`, exact
  regular/init-container and lexical/semantic
  Ray-start-parameter observations (including KubeRay's valueless usage-stats switch), and
  an exact-profile pre-native near-neighbor rejection through the immutable image ID. A fail-closed
  retained-record path now captures the known Ray 2.56 mutable-object reclamation
  blocker only after a bounded cleanup wait, stable paired semaphore fingerprints,
  unchanged final pod and container identities, verified namespace deletion, and
  exact nested JSON types plus independent proof of zero actors, tasks, object results,
  and pilot child processes; it remains nonzero and cannot be used as promotion evidence.
  Pilot success remains unsupported candidate evidence until a separate review promotes
  the exact tuple.
- A versioned, standard-library-only Compiled Graph lifecycle reducer that separates
  session health from invocation outcome, applies complete run/invocation fencing and
  absolute outer-capped deadlines, closes fallback before preparation and replay before
  submission, accounts for one-shot outputs, and emits bounded secret-free snapshots.
  Lifecycle protocol version 1 participates in effective-plan identity; no native
  execution adapter or capability promotion is included.
- A Ray-free workflow-progress storage benchmark command, manually dispatched
  PostgreSQL 17 evidence workflow, and committed environment-tagged comparison of
  full-row, bounded, normalized, delta, external, and live-only storage models.
- A strategy-neutral bounded workflow-progress storage ADR that selects an
  always-bounded task-row summary, database-backed immutable topology pages,
  normalized latest-state detail rows, fenced publication, paginated authorized reads,
  exact V1 budgets, retention, cleanup, and schema-v1/v2 compatibility.
- Additive current/per-attempt workflow-progress summary fields, a strict canonical
  schema-v3 codec with a 16 KiB UTF-8 cap, monotonic exact-run writer primitive,
  one-query bounded v1/v2/v3 compatibility reads, bounded diagnostics, public internal
  identifier removal, and terminal lifecycle archival or derivation under the task-row
  lock. Routine Admin and bundled monitoring reads omit or defer complete progress
  payloads. The standalone writer cannot claim topology/detail pointers. The workflow
  actor remains a schema-v2 live writer, while the explicit stricter pilot may publish
  one terminal schema-v3 record; default and higher-scale activation still wait for
  the remaining ingestion, preparation, capacity, migration, and old-writer-drain work.
- Package-owned, run-scoped workflow-progress topology manifests and content-addressed
  pages, normalized bounded latest-state node rows, deterministic truncation evidence,
  sparse aggregate updates, bounded integrity verification, and exact-fence atomic
  promotion with the schema-v3 summary pointer. Intentional `DISABLED` and
  `OMITTED_BY_POLICY` reporting stays on the summary-only path and creates no empty
  detail store. Terminal expiry uses a validated 0-30 day retention setting, and a
  bounded dry-run-first management command removes only due inactive detail and old
  unpublished orphans while preserving task and attempt summaries. Authorized public
  readers are implemented, and the default-off terminal pilot is the only runtime
  topology/detail producer in the current unreleased development line. Durable rows
  and pages are bounded
  independently from preparer memory.
- A bounded workflow-progress preparation decision and non-production SQLite spill
  prototype. The exact one-shot path externalizes duplicate and reference state into a
  private, fixed-cache, no-mmap workspace with explicit item and file budgets,
  deterministic canonical output, and parent-owned abnormal-termination cleanup. It
  does not itself switch the runtime preparer or activate schema v3. The production
  delivery below now covers #141, while #142 owns composite detail preparation and #79
  still owns wire, mailbox, and producer bounds. The
  required WSL2 Linux matrix records flat memory peaks after retained caps, increasing
  external spill, exact truncation, clean source identity, and successful normal plus
  forced-termination cleanup.
- Spill-backed production topology preparation using the same canonical storage
  contract. Node and edge identity, duplicate, reference, ordering, and retained
  selection state now live in a private package-owned SQLite workspace with an 8 MiB
  cache target, disabled mmap, a 1 GiB file ceiling, explicit input-item and batch
  limits, fail-closed exhaustion, and cleanup before package capability issuance.
  The public prepared-topology type and durable bytes remain unchanged. Its legacy
  complete `observed_node_ids` compatibility field is still materialized only at
  detachment, so #142 must complete composite topology/detail preparation before #132
  can claim an end-to-end O(retained) bound. Schema-v3 workflow publication remains
  default-off and limited to the stricter terminal pilot; #79 still owns live transfer,
  mailbox, and concurrent-workspace admission limits.
- A fail-closed Ray Compiled Graph capability policy, subprocess-isolated native probe
  with a dedicated bounded control-record channel, hermetic public policy coverage, and
  guarded local KubeRay evidence tooling. Exact capability identity includes immutable
  deployment, shared-memory, and object-store profiles; the verified capability set is
  empty.
- A machine-readable review of fresh Linux Compiled Graph candidate artifacts, retaining
  exact provenance and hashes while making an explicit no-promotion decision. Release
  validation now enforces runtime/review parity and future evidence revalidation and
  quarantine gates.

### Changed

- A Ray ecosystem support and install matrix now distinguishes the
  `ray[default]>=2.56.0` Core/Jobs base, live-only Dashboard/State diagnostics, the
  tested application-owned Ray Data recipe, untested application-owned Train, Tune,
  and RLlib workloads, the separate Serve lifecycle, and disabled Compiled Graph
  groundwork. It defines bounded JSON plus immutable artifact/checkpoint URIs,
  idempotent completion manifests, explicit lifecycle/recovery ownership, and a
  Ray Client-versus-Jobs adoption path without adding package extras, adapters, APIs,
  models, or migrations. It also distinguishes django-ray workflows from the removed
  upstream `ray.workflow` package and records the current Ray Data and base-import
  evidence instead of repeating stale support claims.
- A Django-to-private-Ray-Serve guide now provides a canonical executable gateway for
  bounded authenticated online inference without writing a FastAPI ingress. It maps
  upstream timeout, application-owned overload, unavailable, model-error, and
  invalid-response outcomes into a fixed public contract; rejects redirects; and keeps
  request, response, and diagnostic data bounded. The recipe targets the private
  KubeRay `*-serve-svc` data plane and is tested against a loopback fake upstream.
  Serve configuration, replicas, health, rollout, traffic switching, and rollback
  remain application/platform-owned; no package dependency, API, model, migration, or
  deployment integration is added.
- The required Ray dependency now starts at 2.56.0 instead of 2.53.0 so a fresh
  django-ray installation cannot resolve below published upstream security fixes.
  The minimum-dependency lane, installed-wheel verification, ordinary compatibility
  guidance, and fail-closed Compiled Graph candidates use the same floor. Upgrade the
  task managers and every Ray node together before installing django-ray 0.4;
  historical 2.53.0 investigation records remain provenance, not support evidence. Two
  unused 2.53-generated Grafana snapshots were removed; the deployed stack continues
  to import dashboards generated by its running Ray head.
- Manual retry and cancellation now lock only lifecycle and workflow-fence fields.
  Accepted paths then read the exact RuntimeEnv, routing, deadline, and attempt-archive
  fields they need while the row remains locked; stale, duplicate, terminal, and other
  rejected paths never transfer those payloads. Task inputs, progress, workflow plan
  body/selection, completion, and unrelated cancellation data remain excluded. Running
  cancellation tests completion presence without loading the envelope. Durable inline
  or external inputs remain unchanged, result references still move into attempt
  history, and the compatibility retry model defers unrelated columns until after the
  transaction.
- The testproject task-status route now uses one bounded public database projection
  instead of hydrating a Django `TaskResult`. Its nullable inline arguments share a
  16 KiB pre-transfer guard, external input and result storage are never loaded, and the
  complete response is capped at 64 KiB with fixed omission metadata. Workflow and
  RuntimeEnv pollers now apply 16 KiB current result/error guards, return only bounded
  workflow-summary envelopes, and share the same 64 KiB response ceiling. Published
  schema-v3 summaries are preferred; older progress contributes sanitized aggregate
  counts without returning its complete graph. Recovery
  polling additionally guards each of at most four archived attempt errors at 4 KiB.
  These are testproject HTTP-adapter bounds; package `TaskResult` continues to return
  full application arguments, keyword arguments, and successful result data.
- The testproject cancellation route now maps only `ACCEPTED` to `202`, `NOT_FOUND` to
  `404`, and every other fixed lifecycle outcome to `409`. Its response projects only
  bounded control metadata, advertises a 4 KiB ceiling, and does not serialize task
  payloads or diagnostics.
- Admin task retry now requires an explicit signed confirmation before any selected
  row is mutated. The page warns that workflows replay from their entry node and can
  repeat external effects, distinguishes diagnostic progress/output from checkpoints,
  shows only bounded counts, expires after 15 minutes, is bound to the operator's
  current Admin session and exact state/attempt/generation/workflow identity, and caps
  one confirmation at 100 eligible rows. Failed, lost, or expired execution detail
  pages now expose the same fenced flow through a discoverable **Retry task...**
  button; other states explain why retry is unavailable, and succeeded work directs
  the operator to a fresh enqueue. Stale, replayed-after-transition, or tampered
  confirmations fail closed without queueing work. The sample retry API now returns a
  bounded `202` accepted outcome or explicit `404`/`409` reasons instead of returning
  an unchanged successful or raced execution with a misleading `200`. The detail-page
  control also uses a high-contrast border, halo, and shadow on hover so its clickability
  remains apparent in stock Django Admin and Unfold, including dark and reduced-motion
  presentations.
- The README and Getting Started guide now carry a newcomer through task definition,
  enqueue, durable result refresh, and a first-production checklist. Reliability and
  Ray ecosystem claims now state their supported boundaries, Celery migration is
  surfaced at the decision point, and the 0.3.1 upgrade sequence appears before the
  detailed unreleased history. Workflow retry guidance now states explicitly that 0.4
  replays from the entry node and directs side-effecting stages to idempotency receipts,
  an application outbox/reconciliation record, or separate durable task boundaries.
  Execution-mode guidance now states that cluster Ray Core is owned by the task
  manager's Ray Client connection and directs long or coarse connection-independent
  work to Ray Jobs.
- The direct local KubeRay exploratory profile now keeps one default/priority
  task manager and two fixed Ray workers while retaining dedicated sync and ML
  consumers, monitoring, encrypted RuntimeEnv coverage, and the complete
  guarded workflow surface. Its rendered steady state drops from 13 to 10
  running workload pods, excluding the completed setup Job, from 5.3 to 3.2
  requested CPUs, and from 7,104 to 4,800 MiB requested memory. The heavier
  Kong profile now explicitly restores two default task managers and four Ray
  workers, and local log guidance separates Django task managers from Ray
  execution processes.
- Manual TestPyPI rehearsals now validate the still-`Unreleased` candidate form
  without weakening production tag validation. Dispatches require a canonical full
  commit SHA and fail before dependency installation or build unless the input,
  dispatch, checkout, and freshly fetched `origin/main` identities agree. Production
  publication additionally requires an annotated tag resolving to that same commit.
- The authenticated one-user Locust observability demo now compares tiny nested
  workflows under full, terminal-only, and disabled reporting. Stable per-policy
  request labels separate enqueue, terminal polling, and bounded-summary reads, and
  the demo stops when a durable policy contract is missing or malformed.
- The Celery migration guide now treats Django Tasks backends as a simultaneous
  portfolio. It provides a copyable Celery-default plus django-ray configuration,
  backend-qualified result tracking, allowlisted per-submission rollout and rollback,
  durable alias-retention rules, and an explicit application-owned boundary for
  mixed-backend orchestration without presenting django-ray workflows as a generic
  cross-backend engine.
- Documentation now follows the operating system color preference by default and offers explicit
  light, dark, and automatic controls. The existing light presentation keeps a high-contrast blue
  link treatment, while the new dark presentation shares the testproject's neutral black and grey
  foundation and reserves django-ray blue for interactive accents.
- Task-attempt history now defaults to an ordered, bounded, read-only inline on its
  execution page. The standalone app-index entry is hidden by default, while existing
  authorized list/detail bookmarks remain valid; `TASK_ATTEMPT_ADMIN_MODE` can restore
  standalone navigation or enable both presentations. The same package registrations
  support Unfold and standard Django admin.
- Live task status now uses a compact card hierarchy, state badge, and clearly labelled
  workflow topology actions in both admin implementations. Task detail pages intentionally
  omit the raw durable RuntimeEnv snapshot because arbitrary environment values and package
  URIs can contain credentials; the profile and content hash remain available for correlation.
  Execution rows are otherwise read-only: queue, priority, lifecycle, generation, and worker
  ownership remain visible, while controlled writes use the fenced Retry and Cancel actions.
- Persisted RuntimeEnv snapshots now cross one storage seam and fail closed when an
  identified row is missing, malformed, unsupported, unknown-key, authentication-failed,
  noncanonical, or hash-mismatched. Sync, Ray Core, and Ray Job workers classify the
  failure as permanent before submission. Manual and automatic retry paths verify under
  the lifecycle row lock before attempt archival or metadata reset, while exact
  no-profile/no-hash legacy rows keep their default-resolution compatibility. Plaintext
  writes remain the compatibility default; encrypted envelopes use the same boundary.
- `make test-xdist` provides an ordinary four-worker local speed path for the
  default-resource subset. Markers identify tests that own real Ray, live-cluster, or
  PostgreSQL resources; they do not create CI shards or cross-test coordination, and
  supported-Python CI remains non-xdist.
- CI retains one visible test job per supported Python version and cancels superseded
  pull-request workflows as a unit. Public hosted native Compiled Graph candidate jobs
  and the scheduled canary are removed; native evidence now belongs exclusively to the
  guarded local KubeRay pilot.
- Local KubeRay handoffs now retain concise semantic validation summaries with the exact command,
  cold-Ray decision, source-tree match, behavior, and preservation outcomes instead of copying raw
  image IDs, pod hashes, cluster UIDs, and checksums into commits and pull requests. The complete
  bounded secret-free evidence block remains available at runtime for focused diagnostics.
- Queue names now serve only as workload-isolation boundaries and no longer imply
  scheduling precedence.
- Commit-history validation now requires descriptive body context without prescribing section
  headings, rejects development placeholders, and documents the final history review required
  before rebase merges.
- Worker heartbeat, completion, reconciliation, timeout, cancellation, and lease
  cleanup schedules now remain independent from idle claim polling.
- Workflow metadata inspection no longer imports or initializes Ray before execution
  actually needs it.
- Legacy `ray_core:<pk>` task handles remain readable throughout the current unreleased
  development line. Reconstructed PK-only handles now fail closed for low-level polling
  and cancellation when a pending submission occupies that row; identity-aware task
  controls remain supported. A future removal still requires explicit release notes and
  migration guidance.

### Removed

- The pre-1.0 testproject
  `GET /api/cluster/workflows/{task_id}/graph` complete-graph endpoint has been removed
  without a compatibility alias. Clients should use the bounded workflow summary,
  topology-node, topology-edge, node-detail-page, and indexed-node routes. Existing
  schema-v1/v2 database rows remain unchanged and aggregate-readable, and the private
  bounded Admin graph remains available.
- The pre-1.0 testproject live-node adapter at
  `GET /api/cluster/workflows/{task_id}/nodes/{node_id}` and arbitrary bulk retry adapter
  at `POST /api/executions/reset` have been removed without aliases. Use the authorized
  durable indexed `node-detail` read, exact-ID retry endpoint, or bounded signed Django
  Admin retry confirmation. The package live-node helper remains available for
  separately authorized application integrations.
- The manual paired/aggregate pytest-xdist retention experiment and its phased
  coverage, fixed-topology taxonomy extensions, residue checks, timing aggregation,
  and artifact tooling have been removed. `make test-xdist` remains one configurable
  ordinary pytest invocation, while the supported-Python matrix and serial
  external-resource jobs are unchanged. Retained serial inventory now uses internal
  test-suite evidence schema 4; the dated baseline remains immutable.

### Fixed

- The authenticated testproject exact execution lookup now uses one explicit public
  database projection instead of hydrating a complete execution row. Inline result and
  error values are guarded at 64 KiB before transfer, external result references are
  acknowledged without storage access, and fixed omission reasons distinguish stored,
  external, and rendered-response limits. Across the list and exact lookup, malformed,
  deep, or conversion-failing result JSON becomes one fixed `[REDACTED]` marker instead
  of returning undecoded source text. The exact response is capped at 256 KiB; ordinary
  renderer exceptions use the same fixed bounded `503` fallback without swallowing
  process-control exceptions. Detail responses disable caching and MIME sniffing.
  SQLite and PostgreSQL byte semantics, exact boundary behavior, and unsupported
  databases are covered explicitly.
- Ordinary execution and archived-attempt Admin details now use explicit column
  allowlists and database byte guards before diagnostic text can reach Python on
  SQLite or PostgreSQL. Values above the 4,096-character/16 KiB ordinary-field
  ceiling become a fixed omission notice, rendered pages have aggregate byte
  ceilings and disable caching, malformed stored JSON fails closed with a fixed
  diagnostic-free notice, ordinary template-render failures return a fixed bounded
  `503` without swallowing process-control exceptions, and the package ceiling includes
  in-place template-response middleware changes and post-render replacement responses.
  Live-status polling applies the same guarded error read. Immutable change URLs accept
  only `GET` and `HEAD`; inapplicable stock history and delete routes are absent, and
  their direct URL shapes return `404` before an execution or attempt query.
  Contextual attempt history renders at most the newest 25 rows, states the exact
  shown and omitted counts, and links to the paginated attempt list where every
  retained row still has an exact bounded detail. The privileged Sensitive data
  views retain their separate authorization and existing 64 KiB/4 MiB bounds.
- Read-only execution and archived-attempt details no longer advertise Django
  Admin's empty `LogEntry` history page. Durable task attempt and workflow
  diagnostics remain unchanged, while the permission-gated **Sensitive data**
  action stays as a standalone object tool in stock Django Admin and Unfold.
- Package-owned management commands now treat exception and Ray control-plane
  text as untrusted console diagnostics. Worker connection, lease, heartbeat,
  reconnect, polling, reconciliation, retry, shutdown, result-storage, and Ray
  status paths apply a 4 KiB console cap after the shared bounded terminal-safe
  matcher, so output truncation and mixed control finals cannot bypass configured
  patterns. Exception class labels and provider messages enter that matcher as one
  value; type-only benchmark and input-purge diagnostics match the validated label
  without materializing a provider message. Successful task status uses a fixed
  marker instead of traversing the application result again. Workflow audits and
  polling benchmark failures use the same boundary, machine-readable benchmark
  schemas and ordinary identifiers remain stable, and resource summaries redact
  custom names while retaining numeric counts. Structured cleanup logging keeps
  its shared lazy exception boundary. Worker and cancellation lifecycle paths use
  a fixed fallback when an exception message cannot be rendered, without changing
  durable success, cancellation, or reconciliation semantics; durable diagnostic
  fields remain protected by their existing authorization boundary rather than
  being rewritten as console output.
- Stock Django Admin execution detail pages now wrap long task object labels in
  both the native subtitle and breadcrumb. The rule is scoped to the custom
  execution change page, preserving durable identities, unrelated Admin pages,
  Unfold presentation, and existing inline/table scrolling without creating
  document-level mobile overflow.
- Django task-result IDs are now globally unique at the database boundary. Enqueue
  retries a proven UUIDv4 collision a small bounded number of times, recomputes the
  task-ID-bound RuntimeEnv snapshot, and otherwise fails before creating claimable
  work. Unrelated integrity failures are not retried, and this does not add enqueue
  deduplication or exactly-once semantics.
- Fresh workflow runs now reserve a database-unique opaque namespace and advance a
  non-resetting row sequence, then encode both injectively as UUIDv8. Forced namespace
  collisions are retried only for the named database constraint, so repeated random
  candidates cannot alias another retained execution and unrelated integrity failures
  are not masked. Fresh allocation no longer accepts a caller-selected identity; exact
  coordinator reclaim is a separate operation that preserves its existing snapshot-reset
  and run-storage behavior. Migration `0018_workflow_run_allocation` retains active legacy
  run IDs with a null namespace and sequence zero for exact reclaim, while the first fresh
  allocation advances past a derived collision. A persistent database default keeps
  pre-migration enqueue writers compatible with the schema-first window and a code-only
  rollback while assigning their rows sequence zero. Drain old workflow coordinators
  and workers before starting 0.4 processes; mixed old/new coordinators are outside the
  strengthened guarantee. Bounded namespace or sequence exhaustion fails closed with a
  secret-free diagnostic.
- The mandatory runtime graph now requires `pyasn1>=0.6.4`, excluding the three
  high-severity resource-exhaustion advisories published for `pyasn1<=0.6.3`
  (`CVE-2026-59884`, `CVE-2026-59885`, and `CVE-2026-59886`). A pinned
  fail-closed runtime-only `pip-audit` check covers the exact locked default and
  optional-extra non-development graph in the full local gate, every supported Python
  version on Linux plus Python 3.12 on Windows in blocking CI and again before release
  package building. The hashed requirements export is cross-checked against a locked
  CycloneDX graph before scanning, without claiming that django-ray directly decodes
  attacker-controlled ASN.1.
- Queued tasks now snapshot a 24-hour wait budget by default and become terminal
  `EXPIRED` before Ray submission when their indexed absolute deadline is due. Backend
  aliases may choose a positive `QUEUE_TIMEOUT_SECONDS` or explicit `None` for an
  unlimited backlog; retries receive a fresh deadline while pre-submission handoff does
  not. Apply migration `0016_raytaskexecution_queue_expiration` before starting upgraded
  workers.
- Task-manager workers now acquire their lease row before Ray initialization or task
  claims. Exact SQLite/PostgreSQL primary-key collisions receive bounded fresh-ID
  retries with identity-derived logging and polling jitter rebuilt after each retry;
  retained in-flight task claims also reserve an ID after Admin lease cleanup, and
  unrelated database failures abort startup. Heartbeats, queue-expiry and claim checks,
  and shutdown release are fenced by the original host, PID, start time, active state,
  and lease freshness. Expired, inactive, deleted, or replaced ownership is irrevocably
  lost. Recovery locks leases in deterministic order before the execution, validates
  the exact live adopter, and serializes timeout, LOST, cancellation, and Ray Job
  terminal effects with ownership transfer and attempt archival. Sync and Ray Core
  terminal writes and monitor heartbeats require the command's captured owner; Ray Job
  mutations revalidate the complete live lease after read-only status and log RPCs.
  Public cancellation remains durable best-effort intent rather than exactly-once
  interruption. Once ownership loss is detected, the worker performs no further queue
  mutation, completion polling, claims, cancellation, or reconciliation. Benchmark
  cleanup uses exact acquired lease identities, release database failures remain
  distinct from fence misses, and signal-driven handoff revalidates the complete live
  lease before an unsubmitted-task requeue, Ray Core stop, or Ray Job owner release. A
  handoff database failure cannot skip later lease and Ray cleanup. Ray Core stop waits
  at most five seconds, records timeout as indeterminate, and retires only the exact
  tracked ObjectRef so a late control return cannot affect a replacement attempt. A
  process-wide one-request cap prevents permanently blocked Ray Client cancellation
  calls from accumulating daemon threads across runner reconnects. A valid exact owner
  that receives a mismatched Ray Job submission identity quiesces
  the untracked observed capability before the durable reservation under the
  lease-to-execution fence and consumes any durable completion before closing the
  channel; a stale or transferred submitter stops only the untracked observed
  capability. Supported Admin bulk lease deactivation and inactive deletion share the
  worker-ID lock order used by recovery; generic lease deletion is disabled so it
  cannot bypass that fence, and view-only Admin users cannot invoke either controlled
  action.
- External result retrieval now fails closed unless a reference has the exact canonical
  scheme, authority, digest-derived path/key, query, and byte count for the configured
  filesystem, S3, or GCS namespace. A configuration-bound migration reader retains the
  raw object-key encoding emitted by v0.2/v0.3. Provider/stat sizes and bounded raw reads
  are byte-count and SHA-256 verified before UTF-8 decode; concurrent filesystem writers
  atomically install complete objects, including on volumes without hard-link support,
  without replacing corrupt content. S3 and GCS writes are also create-only, collision
  reuse requires an integrity-verified read, GCS reads pin the checked generation, and
  cleanup deletes only the verified S3 ETag or GCS generation. Startup validates active
  and retained namespaces, while bounded errors omit references, payloads,
  credentials, and backend exception chains. Task-input references now share that
  exact pre-client grammar, keep malformed query tokens out of durable tracebacks,
  and dispatch historical reads and cleanup to configured retained schemes. Startup
  also rejects identical input/result filesystem roots, S3 endpoint/bucket/prefix
  namespaces, or GCS bucket/prefix namespaces, preventing input retention from
  deleting a still-referenced result. Execution Admin reference fields now use the
  same bounded redaction as attempt diagnostics.
- Ray status, log, traceback, lost-task, and cancellation diagnostics now preserve raw
  redaction evidence in protected task and attempt fields while every ordinary
  package-owned Admin, API, graph, structured-log, observability, and Django
  `TaskResult.errors` / `TaskError.traceback` projection removes bounded terminal
  controls before redaction and display limits. Privileged task-data views keep
  printable text pattern-unredacted but still render controls inert.
  Traceback and graph failure text retains escaped line
  separation with safe wrapping, while control-split secret markers still fail closed
  under redaction. The bounded parser remains linear for unterminated control strings;
  CAN/SUB cancellation retains subsequent visible text across every 7-bit and C1
  control-string form. Structured mapping keys and logging placeholders follow the same
  normalization and fail-closed redaction boundary, normalized key collisions cannot
  replace redactions or enter workflow-progress storage, disabled log levels remain
  lazy, and UNKNOWN Ray Job console details are normalized and redacted. Unsafe Unicode
  format/bidi controls are inert while emoji shaping and private-use glyphs remain
  printable; matching still ignores default-ignorable characters. A startup-validated
  bounded pattern program evaluates every accepted pattern consistently across terminal
  representations, applies an aggregate 250,000-unit matcher ceiling plus structured
  item/text limits, and fails closed rather than enumerating terminal interpretations.
  Its bounded character and transition caches keep repeated diagnostics fast, while
  high-entropy input stops deterministically. Pattern iterables stop at their count cap;
  rejected sources are neither echoed nor retained through exception chaining. A frozen
  Unicode 16.0 safety table keeps projection stable across supported Python versions.
  Oversized low-level text fails closed before projection.
  Unsupported zero-width, backreference, lookaround, flag-changing, or otherwise
  non-regular configured expressions now fail startup by entry index without echoing the
  source; documented consuming expressions retain case-insensitive search semantics.
  Logging arguments and adapter/call-time fields share aggregate traversal budgets,
  non-string mapping keys expose type only, and traceback-controlled text can select only
  an import-free built-in exception class for Django `TaskError`. Workflow output
  previews distinguish harmless terminal
  normalization from policy redaction/truncation, opaque node identities are rejected
  rather than rewritten, and entrypoint persistence failures use the same redacting
  structured logger.
- `django_ray_worker --all-queues` now discovers and deduplicates queues across
  every configured django-ray backend alias while ignoring Celery and other
  backends. Queue selectors and explicit execution-mode flags are mutually
  exclusive, so mixed-backend deployments cannot silently omit Ray work or
  accept contradictory worker arguments. Ray Core mode also rejects
  `--all-queues` across incompatible per-alias cluster targets.
- Workflow execution graphs now keep every dependency connector neutral when a
  downstream node fails. Danger emphasis is limited to the originating failed
  node, so successful ancestors, dependency-propagated failures, and unrelated
  paths no longer turn the graph red.
- Graph cards now label structural paths as `Node ID` and show an explicit
  state-derived `Output` availability value. Pending, running, failed, and
  completed-without-retained-value states are unambiguous without exposing or
  fabricating internal node results.
- Kubernetes adoption guidance now identifies every checked-in manifest and overlay as an
  evaluation or maintainer-validation asset rather than a production-ready stack. It explains the
  fail-closed `DJANGO_DEPLOYMENT_MODE=production` setting without treating it as topology
  certification, documents the shared-Secret and sample-service hazards, emits that boundary before
  every in-repository Kubernetes Make mutator, and replaces promotion of `k8s/base` with an explicit
  production architecture checklist.
- Monthly coverage-debt reporting now separates default-resource tests from the manifest-owned,
  skip-forbidden local-Ray lane, excludes the default-off Compiled Graph probe, appends both phases
  into one line-coverage report, and retains capped phase logs and source-fenced timing evidence.
  Internal 20-minute and 15-minute process-tree deadlines keep a failing hosted runtime inside the
  existing 45-minute workflow ceiling without weakening the global, worker, Ray Job, or tracker
  contracts. Subprocess output is continuously drained into a bounded in-memory tail instead of an
  unbounded temporary file, post-launcher descendants are terminated and fail closed, and missing
  or invalid local-Ray timing evidence fails the phase.
- Local environment bootstrap now selects the available Python 3.12 patch release instead of
  requiring an unavailable historical patch, so `uv` and `make` commands work in fresh development
  containers and continue receiving compatible Python patch updates.
- Documentation now uses the rich `docs/README.md` as its single homepage source.
  Removing the competing `docs/index.md` prevents Zensical's undefined duplicate-index
  resolution from replacing the published product landing page with a source-orientation
  stub on hosted Linux builds.
- The PostgreSQL evidence manifest now includes the priority-migration contract already
  executed by the blocking serial database target, and a repository test keeps those two
  path selections identical. Contributor guidance now points to live inventory commands
  instead of embedding test totals that drift as coverage grows. The supported-Python
  matrix and ordinary local pytest-xdist workflow are unchanged.
- The guarded local Compiled Graph pilot now derives the installed django-ray
  expectation from archived project metadata and rejects an active dependency
  profile that disagrees with the source package version. Project version bumps
  can no longer leave the documented pilot failing against a duplicated stale
  literal before KubeRay execution. Historical records remain self-consistent
  against their embedded profile, while new evidence must match current tracked
  assets; no retained evidence is changed and no capability is promoted.
- The authenticated low-resource Locust demo now grants its active scenario a
  bounded graceful-stop window after the five-minute scheduling period. The
  window exceeds the longest scenario polling deadline, preventing a final
  enqueued task or workflow-summary check from being silently abandoned while
  the run still exits successfully.
- The headless Locust observability demo now also requires one complete ordered
  task-family tour before it can exit successfully. Interactive observation can
  still stop early, while documentation distinguishes Django task-manager logs
  from Ray worker logs and states that the stack and completed rows are retained.
- Live-cluster fault scenarios now keep one visible serial CI job and one disposable Ray
  cluster while executing in fresh pytest processes with individual hard deadlines,
  diagnostic thread dumps, and explicit node-ID timing markers. A bounded host-side
  preflight now proves the Ray Client proxy and per-client backend without propagating
  the runner's implicit `uv run` environment into generic cluster containers, while
  bounded, credential-redacted internal Ray Client error logs remain available before
  cleanup. The package-free submission smoke now installs the testproject's declared
  remote dependencies explicitly rather than receiving testproject-only packages such
  as Unfold from the CI driver. A Ray Client disconnect can no longer contaminate the
  following cancellation scenario or hide its blocked phase behind the aggregate job
  timeout.
- Full-reporting Ray workflows now retain and retry one delayed terminal progress
  snapshot within the configurable total flush deadline, allowing progress-actor
  startup or queued leaf events to complete after execution finishes. If a current
  terminal run still has no admitted schema-v3 summary, bounded Admin and API readers
  report `MISSING` instead of presenting an empty topology; the workflow outcome is
  unchanged and full-detail schema-v3 publication remains a default-off pilot.
- The bundled testproject no longer exposes direct execution-row deletion. Removing a
  running row could orphan Ray work from its durable lifecycle owner, while deleting a
  terminal row would not constitute complete external-result or workflow-detail
  cleanup. Example integrations now keep reads plus fenced cancellation and retry
  without presenting model deletion as a lifecycle operation.
- The repository-root and published `llms.txt` agent guides now share one exact
  content contract, restoring current workflow bounds, schema-v3 pilot status,
  numeric priority behavior, Celery migration, durable-input, and RuntimeEnv
  encryption guidance while preventing the tracked copies from drifting again.
- Changelog integrity checks now keep all post-release work under `Unreleased`, match
  dated headings and comparison chains to the complete Git tag inventory, and preserve
  one fully validated release-candidate path before its tag is cut.
- Read the Docs layouts now reserve fixed-footer clearance in the page footer and both
  independently scrolling navigation sidebars, keeping the final navigation rows above
  the hosted EthicalAds footer at desktop and narrow widths.
- The tracked Locust harness now authenticates with a secret-safe environment token and provides
  a deterministic one-user observability demo that follows lightweight default, priority, sync,
  cluster/workflow, RuntimeEnv, and ML tasks to terminal state. Quick, moderate, historical
  18-user, and stress profiles select their intended classes explicitly instead of activating an
  accidental broad weighted mix, and stress tasks no longer shadow their submission helpers.
- Ray Job task claims and retries now preserve the immutable cluster target selected
  by a Django Tasks backend alias instead of clearing it with stale submission-handle
  metadata. New tasks snapshot the global django-ray address when an alias does not
  override it. The selected target also takes precedence over Ray's ambient address
  variables when clients are constructed.
- Public cancellation and retry examples now use package-owned, attempt-and-generation
  fenced, row-locked control services. Queued cancellation archives the attempt
  immediately; running cancellation remains a best-effort worker request, and terminal
  or racing completion cannot be overwritten by a stale API or admin save. A Ray Job
  stop response of `false` is recorded as `NOT_APPLICABLE` instead of incorrectly
  claiming that interruption was requested.
- Ray Job submission now reserves a deterministic per-execution job ID and selected
  cluster address before making the remote request. Accepted requests whose responses
  are lost retain that exact identity for reconciliation instead of starting a duplicate;
  definite pre-request failures release the reservation, and post-request tracking
  failures retain the durable capability without automatic retry. Ownership-only
  handoff drops the expired submitter's local tracker without stopping the adopted job,
  while genuine identity replacement receives an exact stop.
- A stale Ray Job whose status remains `UNKNOWN` now consumes a valid durable
  completion first. Otherwise it becomes `LOST` only after the stuck-task timeout,
  receives an exact best-effort stop, persists that outcome before manual retry can
  proceed, and is never retried automatically. Expired malformed or invalid envelopes
  receive the same treatment while Ray still reports `PENDING` or `RUNNING`; normal
  failure/retry handling is reserved for terminal Ray states. Failure, success, stop,
  grace-expiry, and timeout decisions now revalidate the observed completion envelope
  so publication during status, log, or cancellation work wins the next reconciliation
  pass. Reconciliation consumes that envelope even while Ray still reports the wrapper
  as running; cancellation returns `COMPLETION_PENDING` and timeout recovery leaves the
  row untouched when publication already won.
- Address-pinned Ray Job version checks and lifecycle status, stop, and log requests
  now have a five-second HTTP timeout. Status timeouts reconcile as `UNKNOWN`; stop
  timeouts persist as `INDETERMINATE` instead of holding an execution row lock
  indefinitely. Ray Client, `auto`, and GCS address discovery now occurs before the
  row lock; the prepared exact-stop capability executes only after ownership and
  identity are revalidated.
- Minimum-supported `django-ninja` versions now render the bundled testproject
  responses correctly.
- PostgreSQL cancellation and terminal coordination now fence updates by execution
  generation.
- The bundled testproject dashboard now retains a successfully verified operator-supplied bearer
  token in tab-scoped `sessionStorage`, so statistics, smoke-task enqueue, metrics, and execution
  views remain authenticated across reloads. It clears credentials rejected with 401 or explicitly
  forgotten and never embeds them in rendered HTML or long-lived browser storage.
- Ray Client task submission now keeps the pre-RuntimeEnv bootstrap import-free on generic Ray
  images and discards failed remote-function definitions before retrying.
- Kubernetes web probes now send explicit `Host` headers that match each production or local
  allow-list without admitting dynamic pod IPs or wildcard hosts.
- The bundled Prometheus configuration no longer scrapes task-manager pods on a
  nonexistent port. Ray process metrics remain separate from authenticated,
  database-backed django-ray application metrics, with a deployment target-health
  acceptance check for that boundary.

### Migration

- Apply migrations `0007` through `0018` before starting upgraded workers:
  `python manage.py migrate django_ray`.
- Drain and stop every Ray Job task manager running `0.3.1`-or-older code before
  starting code from this development line. Let in-flight jobs finish and reconcile,
  or explicitly verify remote quiescence before retrying them; do not run a mixed
  old/new worker fleet. The deterministic pre-reserved submission ID, `UNKNOWN`
  no-auto-retry policy, and completion-envelope fences form one lifecycle recovery
  protocol.
- `0007_raytaskexecution_priority` adds task priority and gives existing rows the
  neutral default `0`.
- `0008_raytaskexecution_priority_constraint` enforces the `-100` through `100`
  database range. It is intentionally non-atomic: PostgreSQL adds the constraint as
  `NOT VALID` and then validates it, while other databases add the check directly.
  Its PostgreSQL operations are rerunnable.
- `0009_taskinputpayload_and_input_reference` adds the durable input
  registry and nullable task reference. Existing inline inputs remain valid; keep
  spillover opt-in until upgraded code is deployed and old Ray Job drivers are drained.
- `0010_raytaskexecution_workflow_run_id` adds nullable workflow-run identity without
  rewriting legacy progress. `0011_raytaskexecution_workflow_plan`
  adds nullable plan identity, selection, and pinned-attempt fields, so old rows and
  rolling old writers remain valid.
- `0012_workflow_progress_summary` adds nullable current/per-attempt schema-v3 summary
  fields reader-first. It does not modify legacy `progress_data`.
- `0013_workflow_progress_detail_storage` adds reader-first run, manifest, page, link,
  and normalized-detail tables without backfilling or reinterpreting legacy snapshots.
  They remain default-dormant except for accepted opt-in terminal-pilot publications.
  Reverse the migration only after disabling schema-v3 publication and exporting
  retained detail needed for audit; reversal deletes those package-owned tables.
- `0014_raytaskexecution_ray_target_address` adds a nullable immutable Ray Job routing
  target. New enqueues snapshot their effective alias-or-global address. New task
  managers lazily preserve a legacy non-`"auto"` Ray Job `ray_address` under the claim
  or retry lock without promoting Ray Core handle metadata; legacy `"auto"` remains on
  the global fallback because old writers used it even without an alias target. Drain
  pre-`0014` task managers before relying on alias routing and drain targeted tasks
  before reversing the migration.
- `0015_raytaskexecution_task_id_unique` rejects ambiguous legacy duplicate task IDs
  without changing rows, then makes the public Django task-result ID globally unique.
  Keep producers and task managers stopped while resolving any bounded preflight
  diagnostic and applying the constraint. On a large execution table, measure the
  preflight and unique-index build on a production-sized staging copy and plan database
  capacity plus a maintenance window; this is not a zero-downtime migration. Reversal
  removes the uniqueness guarantee.
- Before `0016_raytaskexecution_queue_expiration`, stop old workers and preview the
  existing queued backlog as documented in [Queue expiration](tasks.md#queue-expiration).
  The migration gives queued rows a deadline one day after their latest stored
  eligibility time and snapshots the 24-hour policy on other existing executions for
  future retries. Set `DJANGO_RAY_EXISTING_QUEUED_UNLIMITED=1` only when the existing
  queued backlog was intentionally durable. Upgraded workers expire due rows without Ray
  submission. Pause enqueue traffic for the migration and upgrade every enqueue producer
  before resuming it: old writers cannot populate a deliberate policy snapshot. Do not run
  a mixed old/new worker fleet because old workers do not honor the deadline. Reversing
  `0016` maps `EXPIRED` execution and attempt rows to `FAILED` before dropping the policy
  fields while leaving the `0015` task-ID uniqueness guarantee intact; review all
  still-queued work before starting older workers because they have no deadline fence.
- `0017_raytaskexecution_sensitive_data_permission` adds the
  `django_ray.view_sensitive_task_data` model permission without rewriting task rows.
  Apply it before granting the incident-response role; remove any temporary user or
  group grants when an investigation ends.
- `0018_workflow_run_allocation` adds a nullable workflow-run namespace and a non-null
  allocation sequence with a persistent database default of zero. That default permits
  old enqueue writers to omit the new fields during a schema-first rollout or code-only
  rollback. Drain older workflow coordinators before starting 0.4 code. Before a code
  rollback, stop new coordinators and drain active workflows; retain `0018` for that
  rollback and reverse it only in a separate stopped-writer maintenance window because
  reversal drops allocation metadata.
- Keep the schema-v3 pilot disabled by default until the documented activation gates
  are complete. The current schema-v2 coordinator remains the live compatibility
  writer for full reporting; opt-in full-detail terminal publication is limited to the
  documented pilot profile until the remaining ingestion, preparation, capacity,
  migration, and old-writer-drain work is complete. Terminal-only summary publication
  does not enable that detail producer and requires no migration.

## [0.3.1] - 2026-07-18

### Added

- Repository contribution and automated-agent guidance for branch naming, Conventional Commits,
  worktree safety, validation reporting, and optional local Obsidian project memory.
- Explicit `make fix` and CI-equivalent `make ci` developer targets.
- Per-task timeout configuration through the Django Tasks backend.
- Durable execution-generation, completion-envelope, cancellation, and per-attempt history fields,
  with additive migrations and admin visibility.
- Runtime validation for worker scheduling, retry, result-storage, and redaction settings.
- Redaction of sensitive operational data in logs, task metadata, admin views, and sample endpoints.
- Release-version and installed-wheel validation for reproducible package releases.

### Changed

- Local coverage now enforces the same 95% global floor and targeted module floors as CI.
- `make all`, `make lint`, and `make check` are non-mutating; formatting and automatic fixes require
  explicit targets.
- Ray Job completion now uses a durable completion envelope rather than task logs, and timed-out jobs
  receive a best-effort remote stop request before a fenced terminal update.
- Ray Core distributed helpers cache remote definitions, validate inputs, and support bounded
  in-flight submissions while preserving result order.
- Worker shutdown, runner control boundaries, persisted Ray addresses, and stale-task reconciliation
  are more durable across restarts and retries.
- Sample Docker and Kubernetes deployments now use safer defaults and explicit migration/setup flows;
  the sample dependencies are available through the `sample` extra.
- CI pins the release tooling and enforces descriptive Conventional Commit history, including a
  non-empty body and a 72-character line limit for every PR commit.

### Fixed

- Make the tracked Docker quickstart a migrated, authenticated Compose application: web, worker,
  one-shot migration, and bounded smoke services now share PostgreSQL; CI proves fail-closed bearer
  authentication, enqueue, worker execution, and result retrieval with disposable credentials.
- Recover task lifecycle edge cases without allowing stale attempts to overwrite newer execution state.
- Keep Ray Job outcomes independent of operational log output.
- Avoid leaking credentials, tokens, and large result payloads through operator-facing output.
- Use an absolute README logo URL so the PyPI-rendered project page resolves the SVG correctly.

Applications upgrading from 0.3.0 must run the new Django migrations before starting 0.3.1 workers.
No public API or legacy Ray Core handle format was removed in this release; legacy-format removal is
deferred to a future release with explicit migration guidance.

## [0.3.0] - 2026-07-04

### Added

- Performance, compatibility, and `llms.txt` guidance.
- Python 3.14, minimum-direct dependency, and latest dependency CI coverage.
- Ray-native workflow signatures with `step`, `chain`, `group`, and dynamic
  `map_step` primitives.
- Local workflow execution fallback for sync workers and unit tests.
- Per-step Django bootstrap opt-in and Ray resource options.
- Live workflow progress snapshots with node states, counts, and recent events.
- Test-project endpoints for simple fan-out and nested fast/slow workflow examples.
- `TASK_MONITOR_HEARTBEAT_SECONDS` setting for controlling in-flight task
  heartbeat persistence.
- Named RuntimeEnv profiles with inheritance, startup validation, and backend-alias
  selection through Django Tasks.
- Immutable RuntimeEnv JSON and SHA-256 identity persisted on each task execution.
- Per-workflow-step RuntimeEnv profile and inline environment overrides.
- Test-project RuntimeEnv probe and cold-versus-cached benchmark endpoints.
- UI-ready workflow graph snapshots containing stable nodes, dependency edges,
  runtime environment identity, and Ray execution identifiers.
- Application-level `report_progress()` for long-running workflow leaves.
- Workflow graph, node-state, and bounded Ray log-tail example endpoints.
- Ray Job workflow drivers now carry durable task context and lazily initialize
  Ray so they produce the same graph/progress snapshots as Ray Core workflows.

### Changed

- Documentation examples are now complete, file-scoped, and aligned with the public
  result refresh, queue priority, workflow, and RuntimeEnv APIs.
- Ray Core durable tasks now use a module-level remote executor instead of defining
  and serializing a nested remote function for every submission.
- Ray Core monitor heartbeats are written in one batch at a configurable interval
  instead of on every 100 ms polling iteration.
- The KubeRay example now uses the upstream Ray image for head and worker
  containers; project code and Python dependencies arrive through RuntimeEnv.
- Workflow progress persistence now follows coordinator revisions instead of
  writing an unchanged snapshot every polling interval.
- Workflow leaf output now uses structured, correlation-friendly logging.
- Ray Core converts trusted local RuntimeEnv code paths into content-addressed
  GCS package URIs before per-task submission; local uploads now fail early with
  a clear message when attempted through Ray Client.
- Ray Client submissions serialize the outer bootstrap executor by value, allowing
  generic Ray head images to apply the task RuntimeEnv before importing django-ray.

## [0.2.0] - 2026-05-15

### Added

- Pluggable oversized result storage backends:
  - `RESULT_STORAGE_BACKEND="digest"` (default metadata-only pointer)
  - `RESULT_STORAGE_BACKEND="filesystem"` with `RESULT_STORAGE_FILESYSTEM_PATH`
  - `RESULT_STORAGE_BACKEND="s3"` with `RESULT_STORAGE_S3_BUCKET` (+ optional prefix/region/endpoint)
  - `RESULT_STORAGE_BACKEND="gcs"` with `RESULT_STORAGE_GCS_BUCKET` (+ optional prefix)
- Filesystem-backed `result_reference` format (`resultfs://...`) for retrievable oversized payloads.
- Object-storage `result_reference` formats (`s3://...`, `gs://...`) for retrievable oversized payloads.
- Result storage reference documentation: `docs/reference/result-storage.md`.
- Opt-in live Ray cluster fault-injection integration suite:
  - `tests/integration/test_live_failure_injection.py`
  - configured by `DJANGO_RAY_LIVE_CLUSTER_TESTS`, `DJANGO_RAY_LIVE_RAY_ADDRESS`, `DJANGO_RAY_LIVE_MIN_NODES`.
- Optional package extras for result storage SDKs:
  - `django-ray[s3]`
  - `django-ray[gcs]`
  - `django-ray[object-storage]`
- Explicit documentation of at-least-once execution semantics and idempotency
  requirements for side-effecting tasks.
- Dedicated manual GitHub Actions workflow for live cluster fault tests:
  - `.github/workflows/live-cluster.yml`
  - default CI now excludes `live_cluster` tests.
- Zensical documentation toolchain:
  - `zensical.toml` site configuration
  - strict docs build workflow: `.github/workflows/docs.yml`
  - Make targets for docs build/serve.
- Read the Docs configuration for building the Zensical site as a custom static HTML build.
- KubeRay operator integration assets for Kubernetes:
  - `k8s/overlays/kuberay-kind/ray-cluster-kuberay.yaml` (`RayCluster` + dashboard service)
  - `k8s/overlays/kuberay-kind/` local kind overlay (removes legacy static Ray Deployments from base)
  - Make targets for install/deploy/delete/status using KubeRay.

### Changed

- Worker oversized result handling now routes through configurable storage backends and
  falls back to digest-only references if backend persistence fails at runtime.
- `RayTaskBackend.get_result()` now reloads retrievable oversized payloads from
  `result_reference` when `result_data` is empty.
- Ray Job reconciliation now adopts orphaned persisted jobs from inactive workers before
  falling back to timeout-based stuck recovery.
- Ray Core reconnect cleanup now routes stale in-flight tasks through the normal retry policy.
- Ray-dependent integration tests now skip cleanly when local embedded Ray cannot start.
- Versioned docs deployment is deferred; docs currently build as a single Zensical site.

## [0.1.1] - 2026-01-20

### Fixed

- Documentation and README link corrections.

## [0.1.0] - 2026-01-19

Initial release.

### Added

- **Django Tasks Integration**: Ray-based backend for Django 6's native Tasks framework
- **Multiple Execution Modes**:
  - `--sync`: Direct execution without Ray (for testing)
  - `--local`: Local Ray cluster via `@ray.remote`
  - `--cluster`: Remote Ray cluster via `@ray.remote`
  - Default: Ray Job Submission API (process isolation)
- **Database-Backed Reliability**:
  - Task state tracking in PostgreSQL/SQLite
  - Automatic retries with exponential backoff
  - Configurable retry exception denylist
  - Stuck task detection and recovery
- **Worker Management**:
  - `django_ray_worker` management command
  - Worker lease coordination for distributed deployments
  - Graceful shutdown handling
  - Concurrent task processing
- **Django Admin Integration**:
  - Task execution monitoring
  - Manual retry and cancel actions
  - Ray Dashboard deep links
  - Color-coded task states
- **Kubernetes Deployment**:
  - Kustomize manifests for K8s deployment
  - TLS support for Ray cluster communication
  - PostgreSQL and Ray cluster configuration
  - Prometheus/Grafana monitoring setup
- **Distributed Computing Utilities**:
  - `parallel_map` for parallel task execution
  - `scatter_gather` for heterogeneous parallel tasks
  - Full Django ORM access from Ray workers

### Requirements

- Python 3.12 or 3.13
- Django 6.0+
- Ray 2.53.0+
- PostgreSQL (recommended) or SQLite

[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/dariuszpanas/django-ray/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dariuszpanas/django-ray/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/dariuszpanas/django-ray/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dariuszpanas/django-ray/releases/tag/v0.1.0
