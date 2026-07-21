# ADR-0004: Bounded Workflow Progress Storage

- **Status:** Accepted as a strategy-neutral storage contract; implementation pending
- **Date:** 2026-07-20
- **Decision owners:** django-ray maintainers
- **Related contracts:** [Ray-Native Workflows](../workflows.md),
  [ADR-0001](adr-0001-workflow-plan-contract.md),
  [ADR-0003](adr-0003-compiled-invocation-lifecycle.md)

## Context

The current workflow progress protocol stores one complete JSON snapshot in
`RayTaskExecution.progress_data`. Every changed flush asks the in-memory progress
actor to rebuild all node records and dependency edges, serializes the complete
graph, and replaces the database value. Readers then parse and redact that same
complete value. Graph retrieval returns all nodes and edges, and single-node lookup
scans the complete node list.

The recent-event list is capped by item count, but its values and the node, edge,
message, error, and metric collections have no write-side byte bound. Redacting only
after retrieval neither limits database and API work nor prevents an unsafe value
from being stored. Increasing `WORKFLOW_PROGRESS_FLUSH_SECONDS` reduces write
frequency but does not bound one write, collector memory, parse cost, row and WAL
traffic, or response size.

This is a current dynamic-workflow scaling and data-handling problem. It is not
specific to Ray Compiled Graph. Static and compiled strategies make the boundary
more important because instrumentation must not dominate a latency-sensitive path,
but no native Ray API is needed to define or test the durable contract.

Issue #81 already fences workflow progress by the complete durable run identity:
`(task_execution_pk, attempt_number, execution_generation, run_id)`. ADR-0003 adds a
fifth `invocation_id` for one invocation inside a run. Progress storage must preserve
those scopes without duplicating a static topology for every invocation or allowing
an obsolete writer to publish detail for a newer run.

## Decision

django-ray will replace complete task-row graph snapshots with three separately
bounded layers tied together by publication epochs:

1. an **always-bounded current task-row summary**, copied into one bounded per-attempt
   terminal summary;
2. a separately versioned **immutable topology manifest and topology pages**; and
3. **normalized latest-state node-detail rows** grouped under a published detail
   revision. The revision is a cursor and publication epoch, not a retained physical
   snapshot of every row.

Storage protocol version 1 uses package-owned database tables for topology and detail
rows. External object-storage backends are deferred until their authorization,
atomic publication, retention, integrity, and cleanup behavior can meet this
contract. The existing result-storage reference protocol is not silently reused for
workflow progress.

The new writer will emit workflow-progress summary schema version 3. The existing
schema-version-1 and schema-version-2 complete snapshots remain supported during
rollout through a compatibility reader with an explicit byte cap; they are not the
new storage representation. This ADR does not add fields, models, migrations,
settings, services, or writer behavior. Those are separate implementation changes.

## Summary contract

The summary is stored in a dedicated task-row field rather than sharing the legacy
complete-snapshot field. It contains only fixed-shape or explicitly bounded values:

- summary schema and storage-protocol versions;
- complete four-field durable run identity;
- reporting policy, selected execution strategy, and effective-plan fingerprint;
- monotonic summary revision and referenced topology/detail revisions;
- aggregate workflow state and node-state counts;
- declared/discovered and retained node/edge counts;
- progress percentage and bounded timestamps;
- exact detail availability, completeness, and truncation reasons;
- the database storage kind and an internal manifest identifier;
- the applied limits-profile identifier; and
- bounded terminal outcome metadata suitable for one `TaskAttempt`.

The public summary never exposes a database primary key, storage table name, object
URI, filesystem path, or backend credential. It exposes an opaque service route and
availability metadata. Plan manifests, node records, recent events, arbitrary
messages, metrics, errors, Ray IDs, Ray references, and native Compiled Graph handles
do not enter the task-row summary.

An accurate summary does not imply complete detail. Aggregate counters remain valid
when detail is truncated, omitted by policy, expired, missing, or corrupt. The
summary records both observed graph counts and retained-detail counts so truncation
cannot look like a smaller workflow.

## Topology and detail identity

Topology is run-scoped, not invocation-scoped. A topology manifest is identified by
the four-field run identity plus a monotonically increasing `topology_version`. It
contains ordered page digests and aggregate node/edge counts. Topology pages contain
sanitized node identity/metadata or edges and are immutable after publication.

A fixed graph publishes its topology once. Repeated invocation IDs may publish new
node-detail publication revisions that reference that same topology version; they
never duplicate the topology pages. A dynamic graph may publish a new topology
version when its observed node or edge set changes. Unchanged content-addressed pages
may be reused inside that run, while each topology manifest remains immutable.

`detail_revision` is a monotonically increasing run-level publication epoch stored in
the summary. It is not part of a normalized row's primary key and does not identify a
retained historical row set. V1 retains one bounded latest-state row per retained
node, keyed by the complete run identity and stable node identity. Topology version is
a publication-level reference in the summary, not part of the normalized row primary
key. A row may record the topology and detail epochs in which it was last changed, but
an unchanged row remains part of the current set when either global epoch advances.
Readers never filter the current set to rows whose last-updated epoch equals the
current publication epoch.

The accepted publication transaction batch-upserts only changed or added rows. Rows
for nodes removed from the accepted retained topology are deleted or marked
unavailable in that same transaction. A topology-version change reconciles only
added, removed, or semantically changed node identities; it does not re-key every
unchanged row. An invocation-specific row may carry the complete five-field identity
from ADR-0003, but the count and byte caps apply across all invocations in the run.
Historical detail row sets are not retained, and V1 is not an invocation history.

The tracked issue #124 PostgreSQL 17 matrix compared fixed 256-item pages with
normalized rows under uniformly distributed 1%, 10%, and terminal changes. For the
100,000-node short profile at 1% change, the page model touched 361 of 391 pages and
modeled 16,359,680 written bytes in 363 statements; normalized rows modeled 227,048
bytes in four batched statements. Append-only deltas were slightly smaller at
195,048 bytes but would add compaction and replay state that this observational
protocol does not need. The representative JSONB probe recorded row, relation, and
WAL evidence and cleaned up its transient table; it does not claim production
throughput. See the committed [summary](../benchmarks/workflow-progress-storage-postgresql17-windows-2026-07-20.md)
and [raw JSON](../benchmarks/workflow-progress-storage-postgresql17-windows-2026-07-20.json).

That evidence selects normalized latest state for mutable node detail while retaining
immutable pages for topology that normally publishes once. A node-detail row is not a
recovery log or an independently durable task. The outer `RayTaskExecution` remains
the durability and retry boundary.

## Publication protocol

Detail mutations are staged before the summary pointer is advanced. One accepted
publication follows this order:

1. Normalize, redact, and validate every record before persistence.
2. When topology changed, stage at most one bounded unpublished immutable manifest and
   page set. Staged topology is not reader-visible and is the only data allowed to
   exist outside the final fenced transaction.
3. Begin one database transaction, lock the current task row, and verify the exact
   run fence before mutating current detail.
4. Verify the staged topology's item counts, encoded and decoded byte counts, page and
   manifest digests, and all V1 limits. Batch-upsert only changed normalized detail
   rows and delete rows dropped from the accepted retained topology.
5. Update the locked normalized-set count and byte totals from the old and new sizes
   of changed/deleted rows; do not rescan every unchanged row on each sparse update.
   Database constraints and periodic integrity checks may recompute those aggregates.
   Advance the run-level detail publication epoch when current detail changed; a
   summary-only publication may retain it.
6. Conditionally update the task-row summary only when task primary key, `RUNNING`
   state, attempt number, execution generation, and `workflow_run_id` still match the
   publishing run.
7. Commit publication of the topology manifest, normalized latest state, and summary
   pointer atomically.

Detail is logically written before its summary pointer but becomes visible in the
same transaction. A rejected fence or process/database failure rolls back normalized
detail rather than leaving it current without a summary. Readers never discover a
pending topology manifest. Topology pages staged outside the final transaction are
bounded orphans and cannot become current by matching only a revision number or plan
fingerprint.

Terminal publication follows the same fence while the run still owns progress. The
bounded terminal summary is copied to `TaskAttempt` before retry reset. Complete node
detail is never copied into the task row or attempt history.

## Version 1 limits

V1 limits are protocol interoperability caps, not performance promises or default
retention targets. A writer may apply a smaller configured budget, but it records the
applied profile in the summary. Raising a hard cap requires a reviewed protocol
revision and new database/API evidence; a deployment setting cannot silently create
larger payloads that another V1 reader must accept.

Encoded sizes are stored or wire bytes. Decoded sizes are UTF-8 bytes after canonical
JSON encoding; identity encoding makes the two values equal. Boolean values never
satisfy integer limits.

| Boundary | V1 hard limit |
|---|---:|
| Task-row or per-attempt terminal summary | 16 KiB encoded |
| Topology storage-page items | 256 |
| One topology storage page | 256 KiB encoded |
| One topology storage page after decoding/decompression | 1 MiB |
| One node, edge, event, or diagnostic record | 16 KiB encoded |
| Paginated service response | 256 items and 512 KiB encoded |
| Service records before response encoding | 1 MiB decoded |
| Cursor | 2 KiB encoded |
| Retained topology node records per run | 25,000 |
| Retained topology edge records per run | 100,000 |
| One topology version | 16 MiB encoded and 32 MiB decoded |
| Normalized latest-state node-detail rows per run | 25,000 |
| Current normalized node detail | 8 MiB encoded and 16 MiB decoded |
| Published topology plus retained detail per run | 32 MiB encoded and 64 MiB decoded |
| One pending unpublished topology version per run | 16 MiB encoded and 32 MiB decoded |
| Legacy schema-v1/v2 compatibility snapshot | 64 MiB encoded |

The committed per-run budget permits one current maximum topology plus one bounded
normalized latest-state set. Only one unpublished topology version may exist, which
bounds orphan exposure. A normalized detail update lives inside the fenced database
transaction and therefore adds no separately retained pending revision. Writers
truncate detail deterministically before crossing a count or byte cap; they continue
publishing accurate aggregate counts in the summary.

V1 also applies these value-shape limits before a record enters durable storage:

| Value | V1 hard limit |
|---|---:|
| JSON container nesting below a record | 6 levels |
| Object keys in an application metrics object | 32 |
| Canonical metrics object | 4 KiB encoded |
| Metric key | 64 UTF-8 bytes |
| Metric string value | 256 UTF-8 bytes |
| Node ID or edge endpoint | 256 UTF-8 bytes |
| Callable path or human label | 512 UTF-8 bytes |
| Application progress message or classified error text | 2 KiB UTF-8 bytes |
| Recent event records retained in current detail | 32 |
| One recent event record | 1 KiB encoded |

Unsupported nested values, non-finite numbers, unknown record fields, and values that
cannot be made safe within these limits are rejected or replaced by an explicit
bounded omission marker according to the field contract. Arbitrary `repr()` output is
never persisted.

These initial caps deliberately make full detail optional above 25,000 retained
nodes while keeping the summary useful for much larger observed workflows. The
25,000/100,000 topology limits cover moderately large sparse DAGs without accepting a
dense edge explosion. The 256-item topology-page and service-page caps keep storage
and response construction bounded. The byte limits leave room for normal workflow
metadata while preventing a single record, topology page, or run from consuming an
open-ended database budget. Benchmarks may justify smaller defaults or a future
larger protocol profile; they do not alter V1 in place.

## Detail availability

The summary and service response use this exact availability vocabulary:

| State | Meaning |
|---|---|
| `NOT_REPORTED` | No accepted detail revision has been published for this run yet |
| `AVAILABLE` | The referenced revision is complete within the applied limits |
| `TRUNCATED` | A deterministic retained subset is available and the summary names every truncation reason |
| `OMITTED_BY_POLICY` | The reporting policy intentionally retained only the summary |
| `DISABLED` | Workflow node reporting was disabled for the run |
| `EXPIRED` | Previously published detail was removed by the recorded retention policy |
| `MISSING` | The summary references detail that cannot be found; this is an integrity or cleanup fault |
| `CORRUPT` | Stored bytes, counts, schema, or digests fail validation |

`MISSING` and `CORRUPT` never fall back to an older revision silently. They return a
bounded service error while the task-row summary remains readable. `TRUNCATED` is not
an error and never claims completeness. `NOT_REPORTED`, `OMITTED_BY_POLICY`, and
`DISABLED` are distinct operational facts rather than aliases for an empty graph.

## Pagination and cursors

Package services expose summary, topology-node, topology-edge, node-detail, and
single-node operations. `testproject` may demonstrate those services but does not own
the public contract.

Every collection has a deterministic indexed order. An opaque validated cursor binds
all of the following:

- cursor schema version;
- complete four-field run identity;
- topology version and, for state data, detail revision;
- collection kind;
- normalized filters and ordering;
- last returned stable key; and
- requested limit after the server hard cap is applied.

A cursor is not an authorization token or a storage reference. The service repeats
object-level authorization on every request. It validates the requested run-level
publication epoch against the current task summary before querying normalized rows; a
single statement or consistent database snapshot prevents the epoch check and row
read from straddling a publication. Rows may have an older last-updated epoch and
remain current. A different run, topology version, detail revision, collection,
filter, or order rejects the cursor. A cursor for a retired revision returns
`EXPIRED`; it never advances into the current revision.

The default page size is 100 and the hard maximum is 256. Page construction stops
before either the item or encoded-response byte limit. Because every stored record is
individually bounded, one valid record can always fit. Responses return the exact run
identity, topology/detail revision, availability, completeness, returned item count,
and `next_cursor`.

Single-node retrieval validates the summary's topology/detail epochs, then uses an
indexed run/node key for its current normalized detail record. It never scans or
decodes every page or filters the row by exact last-updated epoch.

## Write-side safety and authorization

Redaction and limits are applied before persistence, then validated again on read.
The live producer boundary owned by the reporting-policy work must reject oversized
wire and decoded messages before actor mailbox or collector memory can grow. This ADR
owns the second boundary: normalized values must fit the durable schema before
topology-page construction or normalized-row writes.

If topology pages or a future backend use compression, the decoder must enforce both
encoded and decoded page limits while streaming and stop before allocating beyond the
decoded cap. Compression metadata is not trusted. V1 topology pages may use identity
encoding; they cannot accept an unbounded compressed payload merely because
compressed bytes fit the row cap.

All detail access begins with authorization for the owning `RayTaskExecution`.
Manifest, topology-page, or normalized-row identifiers alone grant nothing. Database
queries include the exact run identity and validate the referenced publication epoch
against the current summary; they do not require every row's last-updated epoch to
equal it. Public responses omit internal references. Redaction rules apply before
digest calculation so a digest cannot become an oracle for secret values.

## Retention, cleanup, and failure behavior

While a run is active, V1 retains only the normalized latest-state detail referenced
by the current summary. A detail-changing publication atomically replaces changed row
values, advances the run-level epoch, and expires cursors for the previous detail
revision. Unchanged rows remain current even though their last-updated epoch is older.
V1 retains only the topology version referenced by the current summary plus one
bounded candidate topology while an update is in progress.

Terminal detail has a default retention of 7 days and a configurable range of 0 to
30 days. The 16 KiB terminal summary stored with `TaskAttempt` follows task-attempt
retention and remains available after detail expires. Retention values and expiry are
recorded in the current summary.

Only one pending unpublished topology version may exist for a run. Pending or orphaned
topology pages older than one hour are eligible for idempotent cleanup. A rejected
stale writer rolls back normalized detail and marks or deletes any pending topology
manifest immediately on a best-effort basis; periodic cleanup remains authoritative
after process loss.

Database rows use package-owned foreign keys and cascade when the durable task is
deleted. Cleanup first verifies that no current or retained manifest references a
page. A failed cleanup records bounded diagnostics and is retryable; it never deletes
the task summary or changes the task outcome. Future external storage requires an
equivalent tombstone and retry protocol before it can become a V1-compatible backend.

## Admin and service reads

Periodic Admin polling reads only the bounded task-row summary. It does not load,
parse, redact, count, or deserialize topology pages, normalized detail rows, or the
legacy `progress_data` value. The task change form presents explicit availability
and links to authorized paginated detail services rather than rendering a complete
graph field.

Package task summaries likewise read the bounded summary directly. A caller must
request detail explicitly. A summary poll therefore remains bounded even when the
run observed more nodes than V1 retained.

## Compatibility and rollout

Readers are deployed before writers:

1. Add nullable summary, package-owned topology storage, normalized-detail storage,
   and readers that understand schema versions 1, 2, and 3.
2. Keep existing workers on schema v1/v2 while the reader deployment settles.
3. Drain old workflow writers before enabling schema-v3 publication.
4. Enable the new writer, which publishes the dedicated summary, immutable topology
   pages, and normalized detail rows and stops writing a complete graph to
   `progress_data`.
5. Remove the legacy field only in a later, explicitly reviewed migration after the
   compatibility window closes.

The compatibility reader validates schema-v1/v2 JSON, identity rules, and the 64 MiB
legacy byte cap before full decoding. An oversized legacy snapshot produces bounded
legacy-truncation diagnostics rather than an unbounded parse. There is no mass data
migration and no attempt to reconstruct missing detail. The first schema-v3
publication for a run supersedes its legacy snapshot only after the fenced summary
pointer commits.

Old readers are not expected to interpret schema v3. The ordered rollout and writer
drain are therefore part of the persisted-protocol contract, not optional deployment
advice.

## Alternatives considered

### Keep one bounded inline graph

This is simple for small workflows but still rewrites immutable topology, makes
single-node lookup linear, and cannot provide stable page cursors. A bounded inline
preview may be added to a future summary version, but it is not the durable detail
store.

### Store mutable node detail in immutable revision pages

This makes whole-revision integrity and retention explicit, but randomly distributed
sparse changes can touch nearly every fixed page. The issue #124 model showed that
100,000 nodes with 1% changed would rewrite 361 of 391 pages. V1 therefore keeps
immutable pages only for topology and selects normalized latest-state rows for mutable
detail.

### Store append-only events or deltas

An event log avoids full rewrites but grows without bound unless it adds compaction,
checkpoints, replay validation, and retention. Progress is observational rather than
a recovery log. Although its sparse write model was slightly smaller than normalized
rows, V1 selects bounded latest state and avoids replay/compaction semantics.

### Put detail directly in result storage

Existing result backends do not provide workflow-detail authorization,
topology-manifest publication, normalized latest-state updates, indexed pagination,
retention, or orphan cleanup. Reusing their reference strings would hide required
semantics behind a storage URI. External detail remains a later backend decision.

### Keep detail only in the Ray actor

Live-only detail removes database cost but cannot support terminal inspection,
bounded retry diagnostics, or missing-owner behavior. Reporting policy may still
choose `OMITTED_BY_POLICY`; the durable contract cannot assume the actor survives.

## Consequences

- Summary polling, terminal history, and task-row storage have an exact upper bound.
- Static topology is stored once per run and reused across repeated invocations.
- Large workflows may expose accurate aggregates with intentionally truncated or
  omitted detail.
- Detail readers gain bounded pagination and indexed single-node lookup; a revision
  change explicitly expires an older cursor.
- Publication, retry, and stale-writer behavior continue to use the #81 run fence.
- The first implementation adds database rows and cleanup work, but avoids committing
  to external storage before its lifecycle is designed.
- Existing v1/v2 snapshots remain readable within an explicit compatibility cap.
- This decision adds no native Ray dependency, Compiled Graph capability, migration,
  or runtime behavior by itself.

## Implementation boundaries

Implementation should remain split into focused deliveries:

1. Add the bounded summary schema, migration, writer fence, terminal attempt summary,
   and v1/v2/v3 dual readers.
2. Add database topology manifests/pages, normalized latest-state detail rows, limits,
   integrity validation, retention, and topology-orphan cleanup.
3. Add package-owned paginated services, indexed single-node retrieval, authorization,
   and summary-only Admin polling.
4. Coordinate producer and collector memory limits with the separate reporting-policy
   work; do not claim this storage decision bounds the actor mailbox by itself.

Required evidence includes deterministic threshold tests, PostgreSQL write/WAL and
query measurements, retry and stale-writer races, missing/corrupt/orphan cleanup,
cursor consistency, Admin query assertions, and representative 1,000-, 10,000-,
25,000-, 50,000-, and larger-node benchmarks. Performance evidence may lower
defaults or motivate a new protocol version; it must not silently widen V1.

## Related work

- [GitHub issue #80: Bound and paginate workflow progress for large DAGs](https://github.com/dariuszpanas/django-ray/issues/80)
- [GitHub issue #124: Benchmark and decide bounded workflow-progress storage](https://github.com/dariuszpanas/django-ray/issues/124)
- [GitHub issue #125: Persist bounded schema-v3 workflow progress summaries](https://github.com/dariuszpanas/django-ray/issues/125)
- [GitHub issue #126: Persist immutable topology and normalized latest-state detail](https://github.com/dariuszpanas/django-ray/issues/126)
- [GitHub issue #127: Add bounded authorized workflow-progress read services](https://github.com/dariuszpanas/django-ray/issues/127)
- [GitHub issue #79: Make workflow observability cost measurable and configurable](https://github.com/dariuszpanas/django-ray/issues/79)
- [GitHub issue #81: Fence workflow progress writes](https://github.com/dariuszpanas/django-ray/issues/81)
- [GitHub issue #85: Define Compiled Graph invocation lifecycle](https://github.com/dariuszpanas/django-ray/issues/85)
