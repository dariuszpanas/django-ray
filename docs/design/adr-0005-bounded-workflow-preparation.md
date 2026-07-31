# ADR-0005: Bounded Workflow Progress Preparation

- **Status:** Accepted; production topology integrated, composite detail pending
- **Date:** 2026-07-21
- **Decision owners:** django-ray maintainers
- **Related contracts:** [ADR-0004](adr-0004-bounded-workflow-progress.md),
  [Ray-Native Workflows](../workflows.md),
  [ADR-0003](adr-0003-compiled-invocation-lifecycle.md)

## Context

ADR-0004 bounds every durable workflow-progress summary, topology page,
manifest, normalized-detail row, and public response. Its current preparation
implementation does not provide the same bound before persistence.
`prepare_workflow_progress_topology()` materializes all normalized nodes before
sorting and truncating them, retains every valid observed node identity, retains
every valid edge identity for duplicate detection, and can materialize all
eligible edges before selecting the V1 subset. Initial detail preparation also
retains a complete `seen` identity set. The resulting database candidate is
bounded, but Python memory can remain proportional to observed workflow size.

This distinction matters for large Kubernetes inventories. A workflow may
accurately report hundreds of thousands of discovered objects while V1 retains
at most 25,000 topology nodes, 100,000 edges, and 25,000 detail rows. Durable
truncation must not require resident identity state for every omitted object.

Selecting the lexicographically smallest `k` records is not the hard part. A
bounded max-heap can do that in O(`k`) resident memory while maintaining an exact
observed count. The other current invariants require exact membership:

- a duplicate node or edge may occur after an arbitrarily long prefix;
- an edge may reference a valid observed node whose record is not retained;
- detail may refer to a valid omitted node or an unknown node;
- full reporting must contain exactly one detail record for every valid observed
  node identity; and
- the same valid input in any iteration order must produce identical pages,
  manifests, digests, counts, and truncation reasons.

For arbitrary identities, a one-pass algorithm cannot distinguish every unique
stream from one with a late duplicate using memory independent of stream
cardinality. Exact unknown-reference and full-set equality checks have the same
constraint. A caller-provided count, checksum, Bloom filter, or unordered digest
does not change that proof unless django-ray has already validated and bound the
producer that issued it. A general one-shot contract must therefore externalize
exact state or weaken an existing invariant. This decision preserves the
invariants.

The runtime workflow actor persists schema-v2 compatibility snapshots of retained
bounded state. Schema-v3 storage and readers are deployed, and a default-off stricter
pilot may publish one admitted terminal snapshot. General writer activation still
waits for this preparation boundary and the remaining documented gates.

## Decision

django-ray will use a package-owned, bounded-RAM SQLite spill workspace as the
general exact workflow-progress preparation contract. The workspace is a private,
single-owner, disposable local database. It is neither application data nor a
durable recovery source. It exists only while django-ray validates observed input,
selects the deterministic V1 subset, and constructs bounded prepared values.

The general path will:

1. consume each topology-node, topology-edge, and initial-detail iterable exactly
   once;
2. validate and admit each exact identity at the same point as the current duplicate
   and reference-error precedence, then normalize, redact, and apply body record-shape
   limits before payload bytes are admitted;
3. externalize exact identity, uniqueness, and reference state into indexed BLOB
   keys;
4. retain only fixed-size Python batches plus the V1 retained output and one page
   under construction;
5. construct the same canonical V1 topology pages, manifest, detail rows, counts,
   truncation reasons, and digests as the current preparer; and
6. close and remove the workspace before returning package-issued bounded prepared
   evidence to staging and publication.

The final contract is a composite initial-publication preparation lifetime.
Topology and initial detail share the external observed-identity index, so exact
detail validation finishes before that index is deleted. Sparse updates after the
initial publication need only the retained topology and the existing bounded
single-record preparer.

The workspace is local to the process that owns preparation. A path, SQLite
connection, cursor, row identifier, or spill digest never enters a manifest,
summary, database row, Ray message, public response, or transferred
`PreparedWorkflowProgressTopology` value.

## Exact state and canonical selection

Identity keys are stored as validated UTF-8 bytes in SQLite BLOB primary keys.
BLOB ordering is used deliberately so database collation cannot vary by locale.
Parity tests must prove that this order matches the existing Python canonical
order for every accepted identity shape, including multibyte UTF-8.

The conceptual workspace contains separate indexed state for:

- every valid observed node identity, even when the corresponding normalized
  topology record is omitted by a record-size rule;
- every normalized topology-node record eligible for retained selection;
- every valid normalized edge identity, with both endpoints referencing the
  observed-node identity index; and
- every supplied initial-detail identity, with a reference to the observed-node
  identity index and a normalized record only when required by the retained
  topology and reporting policy.

Primary-key conflicts preserve exact duplicate-node, duplicate-edge, and
duplicate-detail failures. Foreign-key or indexed anti-join checks preserve exact
unknown edge and detail failures. Full-policy completeness is an exact set check
inside the workspace, not a comparison of untrusted counts or digests.

Raw observed counters advance in the same place relative to validation as the
current preparer. In particular, an oversized node identity contributes to the
observed node count but does not become a known identity, while a valid identity
whose full record is omitted remains known for duplicate and reference semantics.
Equivalent edge and detail boundary behavior must remain unchanged. A workspace
item-limit failure is not truncation: it aborts the entire preparation and cannot
produce incomplete observed counts.

Indexed ascending scans select retained nodes and eligible edges. Page construction
continues to use ADR-0004's canonical envelopes and byte budgets. Total-topology
byte pressure continues to remove complete pages in the existing deterministic
order. The spill representation is an implementation detail and must not change
the durable output for an equivalent accepted input.

## Workspace lifecycle

One workspace follows a monotonic state machine:

1. **Validate configuration.** Reject invalid or internally inconsistent budgets
   before creating a directory or consuming input.
2. **Acquire.** Resolve and identity-pin a safe temporary parent, atomically create a
   UUID-named private directory, and write an exclusive PID-bound owner lease. POSIX
   parents must be owner-controlled or a sticky root-owned shared temporary directory;
   the workspace and lease use modes `0700` and `0600`.
3. **Initialize.** Open one SQLite database, apply the preparation profile, create
   exact indexes and constraints, and verify the effective pragma values.
4. **Observe nodes.** Consume the node iterable once, update the raw count, and
   insert valid identity and normalized-record state in bounded batches.
5. **Observe edges.** Consume the edge iterable once and perform exact duplicate
   and observed-node reference validation.
6. **Observe initial detail.** In the composite delivery, consume detail once and
   validate duplicate, unknown-node, full-policy, and retained-kind semantics using
   the same identity index.
7. **Seal.** Refuse further input, select canonical retained records, construct
   bounded pages and prepared detail, and verify value and digest parity before any
   durable operation is allowed.
8. **Detach.** Bind the package-issued in-process capability only to the final
   bounded prepared values. No capability authenticates caller-created counts,
   workspace paths, or unsealed state.
9. **Close.** Close SQLite handles, release the lease, and delete the complete
   private directory before prepared values are handed to topology staging or
   publication.

Phases cannot be repeated, reordered, or resumed. Any validation, SQLite, I/O,
resource, cleanup, or cancellation failure poisons the workspace. A poisoned
workspace cannot seal, detach evidence, stage a topology, or publish a summary.
The process-local capability is not serialized. A copied or transferred prepared
value is fully revalidated from its bounded pages, manifest, detail records, and
digests before the receiving process issues a new capability; it never reopens or
trusts the deleted spill workspace.

Issue #141 may retain the current `observed_node_ids` compatibility field while it
introduces the topology phase. That intermediate delivery does not satisfy the
final O(retained) contract. Issue #142 completes the shared topology/detail lifetime
and removes, replaces, or confines the complete observed-identity value before
issue #132 may close.

## Initial preparation-v1 resource profile

The following are initial package implementation bounds. They are not durable V1
interoperability limits and are not encoded into topology manifests or digests.
They may be tightened or widened through benchmark-backed review without changing
the storage protocol, provided canonical retained output remains identical for every
input accepted by both profiles.

| Workspace boundary | Initial preparation-v1 bound |
|---|---:|
| SQLite database page size | 4 KiB |
| SQLite page-cache target per workspace | 8 MiB |
| SQLite memory-mapped I/O | 0 bytes; disabled |
| Total workspace-directory bytes | 1 GiB |
| Main database page cap | 261,120 pages (1 GiB minus a 4 MiB control reserve) |
| Consumed topology-node input items | 1,000,000 |
| Consumed topology-edge input items | 4,000,000 |
| Consumed initial-detail input items | 1,000,000 |
| One normalization/insert transaction | 256 items and 4 MiB accounted item bytes |
| One normalized node, edge, event, or detail record | ADR-0004 V1 limit: 16 KiB |
| One topology output page | ADR-0004 V1 limits: 256 items, 256 KiB encoded, 1 MiB decoded |
| Retained topology nodes | ADR-0004 V1 limit: 25,000 |
| Retained topology edges | ADR-0004 V1 limit: 100,000 |
| Retained initial-detail rows | ADR-0004 V1 limit: 25,000 |

Input-item limits count consumed records, including records later omitted by a
record-size rule. This prevents a stream of invalid or oversized values from using
unbounded CPU while adding no spill rows. The first item beyond a limit fails before
normalization or further iteration. It does not publish a capped observed count.

Transaction accounting includes validated identity bytes plus the bounded normalized
or prepared payload allowance for each item. It is an admission guard for one
transaction, not a claim that those bytes describe total process RSS.

The 8 MiB SQLite cache setting is a configured cache target rather than, by itself,
a proof of total process RSS. The fixed Python batch and retained-output budgets,
SQLite query plans, disabled mmap, subprocess measurements, and allocator-aware
evidence together establish the resident-memory boundary. Activation cannot rely on
the pragma alone.

The per-workspace limit also does not by itself bound aggregate node-local disk when
many owners prepare concurrently. The production adapter delivered through #141,
#142, and #79 must admit preparation against a quota-backed local spill root, such as
a Kubernetes ephemeral volume with an explicit size limit, and account for the maximum
number of concurrent workspaces. The #140 prototype validates its configured ceiling
but does not reserve node-local capacity. Production preparation must fail before
consuming input when it cannot reserve its workspace budget.

## SQLite operating rules

The workspace uses SQLite because it supplies exact uniqueness, indexed ordering,
and reference validation without inventing a general external-sort framework. Its
configuration is intentionally different from a durable application database:

- `page_size` is 4096 and is applied before schema creation;
- `cache_size` is `-8192`, selecting an 8 MiB cache target;
- `mmap_size` is zero and its effective value is verified;
- `journal_mode` is `OFF` and `synchronous` is `OFF`;
- `temp_store` is `MEMORY`, but every fixed selection and membership query plan is
  preflighted before input consumption and must match its statement-specific primary-
  key scan/search order without a temporary B-tree, materialization, automatic index,
  or unrecognized planner drift;
- foreign-key enforcement is enabled;
- locking mode is exclusive for the workspace lifetime;
- preparation operations have exactly one owner thread and creator process. The SQLite
  connection permits a different thread to close it only while holding the workspace
  lifecycle lock, allowing best-effort interpreter cleanup without racing an active
  owner operation;
- untrusted SQL, extensions, and application-defined functions are not accepted; and
- the production package selects its workspace root. The benchmark-only
  `--workspace-parent` option places an isolated evidence run on a chosen test volume;
  it is not a caller-selected runtime path.

Disabling the journal is safe only because this database is disposable and never a
recovery source. Successful inserts use explicit transactions of at most one bounded
batch. Any statement, constraint, commit, cancellation, or I/O error discards the
entire workspace instead of attempting to recover or reuse it. WAL, rollback-journal,
and shared-memory sidecars are therefore not expected and their appearance is a
contract violation.

The 261,120-page `max_page_count` hard-limits the main database file and leaves a
4 MiB reserve for the lease and other bounded control files inside the 1 GiB
directory budget. The prototype accounts for the exact lease and database entries
after initialization and every committed transaction; its fixed selection queries are
read-only. The production adapter must recheck the complete workspace before handoff.
An unexpected file, sidecar, or total above 1 GiB poisons the workspace.

All retained selection and membership queries must use declared primary-key or
covering-index order. Before consuming input, focused `EXPLAIN QUERY PLAN` assertions
reject `USE TEMP B-TREE` and other unplanned materialization. The schema and query set
are then frozen for the workspace lifetime. `temp_store=MEMORY` avoids an SQLite
temporary file outside the package-owned directory; it is not permission to execute a
temporary-storage plan. The implementation does not run `VACUUM`, `ANALYZE`, or
another operation that can change a plan or create an unbudgeted copy of the
workspace.

## Failure, cancellation, and cleanup

Workspace limits fail closed with a stable bounded package error. Spill exhaustion,
`SQLITE_FULL`, item exhaustion, invalid configuration, and query-plan drift are not
workflow-progress truncation reasons. No partial prepared value or durable candidate
is returned. Diagnostics identify the phase and public limit name but never include
record content, node identities, SQL text containing values, or filesystem paths.

The caller supplies a cooperative cancellation check. Preparation checks it before
consuming each input item, before and after each batch commit, between selection
queries, before each output page, and before sealing. Cancellation raises through the
same poisoned-workspace path. Iterators that expose `close()` receive a best-effort
close after consumption failure, but arbitrary user iterator cleanup is not part of
the memory or latency guarantee.

Workspace acquisition owns every resource as soon as it exists. The connection is
registered before post-connect configuration, live-workspace registration is inside
the acquisition failure boundary, and a partially written lease is removed only
after its exact file identity is revalidated. Temporary-root discovery, canonical
resolution, lease creation, and cleanup failures become bounded pathless diagnostics;
this includes symlink-resolution loops that raise a runtime error rather than an
ordinary filesystem error.

On success, ordinary exception, cooperative cancellation, `GeneratorExit`, or a
graceful termination that unwinds Python, a `finally` path closes cursors and the
connection and cleans only the resolved UUID workspace directory. Cleanup first
renames that exact directory to a random quarantine name under the already validated
same parent. It then revalidates the moved directory identity, lease identity,
contents, permissions, and absence of a replacement at the source name before
unlinking any member. A rename failure, identity change, source reappearance, or
quarantine replacement refuses cleanup and can never be reported as removal.
Operational unlink failures retain the validated quarantine for a bounded retry;
cleanup never broadens deletion to the spill root. Cleanup failure prevents handoff
and emits a bounded operational diagnostic.

Python cleanup cannot run after `SIGKILL`, native process abort, power loss, or host
loss. This ADR does not claim otherwise. Subprocess preparation is parent-owned: the
parent creates or reserves the workspace and removes it after every child exit,
including forced termination. The #140 prototype proves that parent-owned path and
ordinary context cleanup only. The production in-process integration must add
best-effort `atexit` cleanup. A later package process may remove an orphan only after
validating the package root and UUID name, acquiring the exclusive lease, proving no
live owner, and observing the configured stale age. A fork child drops inherited live-
workspace and prepared-capability registries, uses fresh locks, and cannot validate a
parent PID lease or detach a parent candidate. #141, #142, and #79 must supply
that stale-cleanup and quota-backed deployment behavior before activation.

An abrupt failure can therefore leave at most the admitted per-workspace spill until
the parent or stale cleanup runs. It cannot leave a published summary or staged
topology because staging begins only after successful workspace deletion. A later
publication failure follows ADR-0004's pending-candidate cleanup and has no spill
resource to clean.

## Iterable and future fast-path contracts

The general path accepts arbitrary one-shot iterables. It calls `iter()` once, never
calls `len()`, never rewinds, and never consumes an item twice. An exception during a
later phase does not replay an earlier iterable. Replayable sequences receive the same
general behavior initially; replayability is not inferred from Python type.

A caller that has already materialized a million-record list owns that list's memory.
The workspace bounds incremental consumption and package-owned preparation state; it
cannot retroactively bound a caller allocation. Schema-v3 activation must use the
bounded producer/transfer contract from issue #79 rather than pass the current
complete actor snapshot into this API.

Future optimizations may avoid spill only when they present a package-issued
capability bound to the exact normalized identity, ordering, limits profile, and
producer lifecycle. The capability must be non-forgeable outside its owner process or
fully revalidated after transfer. Candidate fast paths include:

- a replayable source whose package-owned first pass produced validated canonical
  runs;
- producer-canonical bounded pages with exact uniqueness and membership evidence;
  and
- a package-owned topology identity reused by repeated invocations of one static
  graph.

Caller-supplied counts, checksums, digests, sorted flags, or page descriptors are not
such a capability. Two passes alone do not remove the exact duplicate-membership
requirement, and a probabilistic membership structure cannot preserve exact error
semantics. A fast path must prove byte/digest parity against the general path and must
fall back before preparation begins. It cannot fall back after consuming input or
creating application effects.

## Composition with the live producer

Issue #79 owns the earlier live boundary. Before a record reaches this workspace, the
producer path must independently bound:

- each serialized message and its decoded or decompressed representation;
- each leaf invocation's replaceable application-progress state while actor
  acknowledgements are pending;
- aggregate mailbox admission, queued bytes, and coalesced state across forked actor
  handles;
- workflow-wide update frequency and backpressure behavior;
- metric and event cardinality; and
- the number of concurrent preparation workspaces admitted on a worker or node.

Issue #259 implements the per-leaf line with one outstanding acknowledgement, one
canonical latest-value slot, and one bounded terminal handoff/report. It deliberately
does not coalesce structural or lifecycle evidence and does not satisfy the aggregate
multi-handle lines that follow it.

The preparation-v1 adapter consumes at most 256 records and 4 MiB of canonical record
bytes in one package batch. Issue #79 may impose a smaller wire limit and must define
separate encoded and decoded limits. It must reject an oversized or compressed
transfer before allocating its complete decoded value or placing it in an actor
mailbox. A small compressed payload is not permission to create a large Python object.

Spill protects the preparer after bounded admission. It does not make an unbounded Ray
object, actor message, JSON array, mailbox, collector snapshot, or database round trip
safe. Documentation and benchmarks must report the live-input and preparation bounds
separately.

## Compatibility and activation

This decision does not change database models, migration `0013`, summary schema v3,
storage protocol V1, durable retention caps, page envelopes, cursor semantics, or
authorization. For equivalent accepted inputs, retained topology and detail values,
pages, manifests, byte counts, digests, observed counts, and truncation reasons remain
byte-for-byte or value-for-value identical.

The preparation profile is package behavior, not a persisted interoperability
profile. A deployment with insufficient spill capacity fails preparation and publishes
no candidate; it does not silently select a smaller topology. Raising or lowering a
workspace resource limit requires evidence and release documentation but no migration
or storage-protocol bump unless canonical durable behavior also changes.

General/default schema-v3 producer activation remains disabled while issue #142
completes bounded composite preparation. After #142 proves that boundary, activation
still waits for #79's aggregate mailbox admission/coalescing, sampled reporting
policy, and workspace admission behavior, plus the ADR-0004 reader-first writer drain.
The producer-local session is bounded, but many forked handles can each create one
such session. The default-off strict terminal pilot is the only current producer
exception; neither this ADR nor a successful preparation benchmark authorizes broader
production writer activation.

## Prototype boundary

Issue #140's prototype and benchmark live under `scripts/`; production source does not
import them. They prove exact canonical selection, bounded spill accounting,
cooperative cancellation checkpoints, context cleanup, and parent-owned subprocess
cleanup without changing a model, migration, public API, runtime preparer, or writer
flag.

The prototype does not reserve aggregate node-local capacity, register production
cleanup, issue a runtime preparation capability, stage a durable candidate, or
configure Kubernetes. Issue #141 now supplies the in-process topology capability and
best-effort `atexit` cleanup. Stale-workspace cleanup, quota-backed deployment, and
aggregate admission remain activation requirements shared with #142 and #79. The
prototype's explicit workspace-parent option is only an evidence sandbox so filesystem
behavior can be recorded.

## Production topology delivery

Issue #141 moves the public topology preparer onto the package-owned SQLite workspace
without changing its call signature or `PreparedWorkflowProgressTopology` result. The
workspace streams exact node and edge identity state, selects only retained V1 records
into Python, constructs the existing pages and manifest, and deletes its UUID directory
before issuing the process-local prepared-topology capability. Successful cleanup,
ordinary exceptions, cooperative cancellation, item or byte exhaustion, injected
insert/selection/page failures, and best-effort graceful-process cleanup are covered at
the production adapter boundary.

By default, each workspace is an atomic private UUID child directly beneath the
resolved operating-system temporary location; there is no reusable package root whose
unsafe pre-creation could grant ownership. An explicit benchmark parent is accepted
only after the same identity, ownership, and permission checks. Parent and workspace
file identities are rechecked before accounting. Deletion uses the same-parent atomic
quarantine and post-rename identity checks defined above, including exact partial-lease
cleanup during failed acquisition. Package filesystem failures become phase-specific
pathless diagnostics, while caller iterator exceptions remain unchanged. A deployment
must still provide quota-backed ephemeral storage, stale-orphan cleanup, and aggregate
concurrent-workspace admission before schema-v3 activation. The lock-serialized
cross-thread `atexit` hook is best effort and is not a substitute for those node-level
controls.

The intermediate public result still contains the historical complete
`observed_node_ids` field required by the materialized initial-detail API. The bounded
candidate does not create that set while the workspace is observing, validating, or
selecting topology. It is materialized from the indexed identity scan immediately
before successful cleanup and is authenticated only for that exact package-issued
object. Capability records retain strong references to every identity-bound immutable
evidence value until the weak topology owner is collected, preventing allocator ID
reuse after hostile frozen-field replacement. A copied or durably revalidated topology
can prove its bounded pages and manifest, but it cannot impersonate complete
observed-membership authority. This compatibility detachment remains O(observed) and
is deliberately visible in production resource reports; issue #142 must confine or
remove it through composite preparation.

The existing subprocess harness accepts `--implementation production-topology`. That
mode runs the production workspace and compatibility detachment in a fresh child,
reports the same cache, batch, item, spill, CPU, wall, query-plan, and cleanup evidence,
and emits no detail measurement. Report schema v2 separates the bounded pre-legacy
tracemalloc/RSS checkpoint from the end-to-end peak that includes compatibility
detachment. It retains the schema-v1 peak field names as aliases for additive readers,
requires all v2 fields to be present and internally consistent, and ignores unknown
extra fields. The default `prototype-composite` mode and the committed issue-#140
evidence remain unchanged in meaning. Neither mode starts a writer or authorizes
schema-v3 production.

## Evidence gate

Issue #140 supplies an executable preparation-only prototype and subprocess benchmark.
Each measured case runs in a fresh child process so peak RSS is attributable to that
case rather than a previous process high-water mark. The structured benchmark artifact
records:

- source revision, worktree state, implementation hashes, platform, Python, SQLite,
  and filesystem or volume identity;
- observed and retained node, edge, and detail cardinality;
- configured cache, item, batch, page, retained, and spill budgets;
- wall time, process CPU time, spill high-water bytes, and explicitly scoped memory
  evidence. Production mode records a bounded topology checkpoint before legacy
  detachment plus an end-to-end peak that includes the O(observed) compatibility set;
  and
- successful scale cases plus forced-termination and cleanup outcomes.

Focused prototype tests separately record success, validation error, spill and item
exhaustion, cooperative cancellation, iterator failure, cleanup refusal, and canonical
parity outcomes. The source revision, dirty state, and implementation digest are
captured before and after the long-running matrix; a changed source snapshot fails the
evidence run.

The matrix includes 25,000, 100,000, and at least 250,000 observed nodes, with a sparse
chain and a deterministic high-edge profile that exceeds the 100,000 retained-edge
cap without constructing a complete quadratic graph. Focused parity tests prove that
ordered, reverse, and shuffled inputs produce identical accepted output. Representative
oversized identities and records, late duplicate nodes and edges, unknown edge and
detail references, and full-policy omissions exercise exact failure parity.

A report marked `required_scale` is accepted only when all six node/profile
combinations are present exactly once. Schema-v2 validation also rejects missing or
inconsistent phase evidence, mismatched legacy/end-to-end aliases, and non-monotonic
memory peaks. RSS may remain explicitly unavailable as `null`; it is never inferred as
zero. Unknown additive fields do not invalidate an otherwise complete v2 report.

The prototype must compare every retained record, page payload, manifest payload,
digest, byte count, observed count, and truncation reason with the current preparer for
inputs accepted by both. #140 fault coverage exercises configuration, workspace
creation, constraint insertion, selection/page failures, spill and item exhaustion,
cooperative cancellation, cleanup failure, timeout, and child termination at the
prototype boundary. #141 repeats those faults at the production topology adapter;
#142 must repeat them at the composite detail and staging boundaries. Forced child
termination passes only when the parent removes
the workspace and no durable candidate exists.

Benchmark evidence is a release decision aid, not a latency SLO. The contract is
accepted only when increasing observed cardinality beyond retained caps does not add
Python identity collections and measured resident growth is explained by the fixed
cache, batch, and retained-output budgets rather than total observed identities.

The required 2026-07-21 WSL2 Linux [evidence summary](../benchmarks/workflow-progress-preparation-sqlite-wsl2-linux-2026-07-21.md)
and [authoritative JSON](../benchmarks/workflow-progress-preparation-sqlite-wsl2-linux-2026-07-21.json),
together with the focused canonical and lifecycle suite, satisfy this gate for the
issue-#140 prototype at revision `c42fb22634712e52d8aee74c86a62fea459da5e8`.
Sparse and high-edge memory peaks plateaued after retained caps while the external spill
grew with observed input. All six cases, source-identity checks, focused parity and
fault paths, normal cleanup paths, and the forced-termination cleanup control passed.
This accepts the preparation contract only; by itself it does not satisfy #141, #142, #79, or the
schema-v3 activation gate.

The issue-#141 production adapter has its own required WSL2 Linux
[evidence summary](../benchmarks/workflow-progress-topology-sqlite-wsl2-linux-2026-07-21.md)
and [authoritative JSON](../benchmarks/workflow-progress-topology-sqlite-wsl2-linux-2026-07-21.json).
At clean revision `fd08ab21373c0bf1e0586e54ec0a564e3d4ce4d5`, all six
production-topology cases, exact source checks, schema-v2 phase evidence, normal
cleanup paths, and forced-termination cleanup passed. Sparse bounded-phase
tracemalloc stayed at 55.67 MiB and high-edge tracemalloc stayed from 101.21 to
101.22 MiB while observed input grew from 25,000 to 250,000 nodes. Spill grew with
the exact external identity state. This satisfies #141's topology integration gate;
the explicit 25,000/100,000/250,000 legacy identity counts remain owned by #142.

## Alternatives considered

### Keep bounded top-k values in Python

A heap bounds deterministic retained selection but cannot prove exact uniqueness or
arbitrary endpoint membership. Adding complete Python sets recreates the problem this
decision addresses.

### Trust caller-supplied counts or digests

Counts do not prove uniqueness, membership, or full-policy set equality. Cryptographic
digests make accidental disagreement unlikely but do not identify which detail or edge
is invalid and are not package authorization. They become useful only inside a
package-issued validated capability.

### Require replayable two-pass iterables

This breaks current one-shot generator behavior and a second pass still needs exact
duplicate/reference state unless the input is already package-validated and sorted.
Failures during replay also introduce a new partial-consumption lifecycle. Replay may
support a future fast path but is not the general contract.

### Require producer-canonical pages

Canonical pages can remove sorting work and are attractive for static graph reuse.
Without a bounded producer and validated page capability, they merely move unbounded
identity state into issue #79 or trust caller claims. They remain a future optimization
after the general path and live boundary are proved.

### Use probabilistic membership

Bloom or cuckoo filters bound memory but permit false positives. They could accept an
unknown reference or reject a unique identity, changing deterministic validation
semantics. They are unsuitable for the authoritative preparation path.

### Implement custom chunk files and an external merge

Sorted chunk files can meet the memory bound, but django-ray would need to implement
and verify merge ordering, uniqueness, reference joins, crash cleanup, and disk
accounting itself. Private SQLite provides those indexed primitives with a smaller,
better-tested surface.

### Spill into the configured Django database

Application-database staging would add round trips, transactions, migrations,
authorization, orphan cleanup, and contention before a candidate is valid. It would
also blur disposable validation state with ADR-0004's durable protocol. The workspace
must remain local and package-owned.

## Consequences

- Exact one-shot duplicate, membership, reference, and completeness semantics are
  preserved without O(observed) Python identity sets.
- Resident preparation memory is governed by explicit cache, batch, page, and retained
  budgets; observed cardinality consumes bounded local disk and CPU until an explicit
  limit.
- Preparation now requires quota-backed writable local storage and operational cleanup
  for abrupt process loss.
- Inputs beyond the preparation item or spill profile fail rather than publish a
  misleading truncated summary.
- Initial publication gains a composite topology/detail API, while sparse retained
  detail updates remain lightweight.
- Durable V1 bytes and database schema remain unchanged.
- SQLite configuration and query plans become security and performance invariants that
  require focused tests and benchmark evidence.
- This bound remains independent of, and insufficient without, issue #79's live
  producer and transfer bounds.

## Staged delivery

1. **Issue #140:** accept this decision, add a non-production prototype, prove
   canonical parity, and record the preparation-only subprocess benchmark. No runtime
   preparer switches and schema-v3 production remains disabled.
2. **Issue #141 (implemented):** implement bounded spill-backed topology observation, exact edge
   validation, canonical selection, and cleanup. Preserve the current materialized
   API and observed-identity compatibility field while downstream detail preparation
   still needs them. This is an intermediate bound, not completion of #132.
3. **Issue #142:** introduce composite topology/detail preparation, confine or remove
   O(observed) prepared evidence, migrate package callers and benchmarks, and record
   the final memory proof. Issue #132 closes only after this delivery.
4. **Issue #79 and activation:** compose the implemented message and per-leaf producer
   bounds with aggregate mailbox admission/coalescing, the later sampled policy, and
   workspace admission. Drain old writers under ADR-0004 before enabling schema-v3
   production.

## Related work

- [GitHub issue #79: Make workflow observability cost measurable and configurable](https://github.com/dariuszpanas/django-ray/issues/79)
- [GitHub issue #80: Bound and paginate workflow progress for large DAGs](https://github.com/dariuszpanas/django-ray/issues/80)
- [GitHub issue #126: Persist immutable topology and normalized latest-state detail](https://github.com/dariuszpanas/django-ray/issues/126)
- [GitHub issue #132: Bound workflow-progress preparation memory](https://github.com/dariuszpanas/django-ray/issues/132)
- [GitHub issue #140: Select bounded workflow-progress preparation contract](https://github.com/dariuszpanas/django-ray/issues/140)
- [GitHub issue #141: Stream workflow topology preparation through bounded spill](https://github.com/dariuszpanas/django-ray/issues/141)
- [GitHub issue #142: Compose bounded topology and detail preparation](https://github.com/dariuszpanas/django-ray/issues/142)
- [WSL2 Linux preparation evidence summary](../benchmarks/workflow-progress-preparation-sqlite-wsl2-linux-2026-07-21.md)
- [Authoritative preparation evidence JSON](../benchmarks/workflow-progress-preparation-sqlite-wsl2-linux-2026-07-21.json)
