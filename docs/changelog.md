# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
- Coroutine-based Django tasks across sync, Ray Core, and Ray Job modes, with
  per-invocation event loops and preserved retry exception classification.
- A Ray Serve integration boundary that defers package orchestration, keeps deployment
  state separate from durable tasks, and records adoption gates for a companion package.
- A Compiled Graph ownership ADR and opt-in raw-Ray topology probe that select one
  Ray Core outer task as the initial local/direct within-run CPU-pilot owner without
  claiming production Ray Client transport or cross-schedule reuse.
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
  payloads. The standalone writer cannot claim topology/detail pointers, and the current
  workflow actor remains a schema-v2 writer; schema-v3 activation waits for bounded
  live ingestion, bounded preparation, and the old-writer drain.
- Package-owned, run-scoped workflow-progress topology manifests and content-addressed
  pages, normalized bounded latest-state node rows, deterministic truncation evidence,
  sparse aggregate updates, bounded integrity verification, and exact-fence atomic
  promotion with the schema-v3 summary pointer. Intentional `DISABLED` and
  `OMITTED_BY_POLICY` reporting stays on the summary-only path and creates no empty
  detail store. Terminal expiry uses a validated 0-30 day retention setting, and a
  bounded dry-run-first management command removes only due inactive detail and old
  unpublished orphans while preserving task and attempt summaries. Authorized public
  readers are implemented, but the runtime writer remains inactive until bounded live
  ingestion, bounded preparation, and the old-writer drain are complete. Durable rows
  and pages are bounded independently from preparer memory.
- A bounded workflow-progress preparation decision and non-production SQLite spill
  prototype. The exact one-shot path externalizes duplicate and reference state into a
  private, fixed-cache, no-mmap workspace with explicit item and file budgets,
  deterministic canonical output, and parent-owned abnormal-termination cleanup. It
  does not switch the runtime preparer or activate schema v3; #141 and #142 own the
  production integration, while #79 still owns wire, mailbox, and producer bounds. The
  required WSL2 Linux matrix records flat memory peaks after retained caps, increasing
  external spill, exact truncation, clean source identity, and successful normal plus
  forced-termination cleanup.
- A fail-closed Ray Compiled Graph capability policy, subprocess-isolated native probe
  with a dedicated bounded control-record channel, and Linux candidate canaries. Exact
  capability identity includes immutable deployment, shared-memory, and object-store
  profiles; no native capability tuple is enabled yet.
- A machine-readable review of fresh Linux Compiled Graph candidate artifacts, retaining
  exact provenance and hashes while making an explicit no-promotion decision. Release
  validation now enforces runtime/review parity and future evidence revalidation and
  quarantine gates.

### Changed

- Queue names now serve only as workload-isolation boundaries and no longer imply
  scheduling precedence.
- Commit-history validation now requires descriptive body context without prescribing section
  headings, rejects development placeholders, and documents the final history review required
  before rebase merges.
- Worker heartbeat, completion, reconciliation, timeout, cancellation, and lease
  cleanup schedules now remain independent from idle claim polling.

### Fixed

- The bundled testproject dashboard now accepts an operator-supplied bearer token in page memory
  for statistics, smoke-task enqueue, metrics, and execution views without embedding or persisting
  the configured credential.

### Migration

- Apply `0012_workflow_progress_summary` before deploying upgraded readers. The two new
  columns are nullable, so existing rows and rolling old writers remain valid. Keep the
  schema-v3 producer disabled through the public-reader rollout, #79 live-ingestion
  bound, and #132 preparation integration, then drain old workflow writers before
  activation. Reversing this migration drops the new summary history columns but does
  not modify legacy `progress_data`.
- Apply `0013_workflow_progress_detail_storage` after `0012` to add dormant run,
  manifest, page, link, and normalized-detail tables. It does not backfill or rewrite
  legacy snapshots, so old schema-v2 writers remain valid during the reader-first rollout.
  Reverse it only after disabling schema-v3 publication and exporting any retained
  topology/detail needed for audit; reversal deletes those package-owned detail tables.

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
