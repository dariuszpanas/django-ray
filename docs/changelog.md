# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Development scope

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
  without changing the workflow result. Schema-v2 live writes remain the rolling
  compatibility path. The bundled testproject enables the pilot through
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
  compatibility uses the shared policy-v2 runtime, owner, submission, and channel
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

### Fixed

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

- Apply migrations `0007` through `0014` before starting upgraded workers:
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

[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/dariuszpanas/django-ray/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dariuszpanas/django-ray/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/dariuszpanas/django-ray/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dariuszpanas/django-ray/releases/tag/v0.1.0
