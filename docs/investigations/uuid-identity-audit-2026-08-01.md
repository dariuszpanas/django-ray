# Durable identifier collision audit (2026-08-01)

This audit reviewed every UUID-derived or collision-sensitive identity reachable from
task enqueue, worker ownership, Ray submission, workflow execution, progress storage,
and bounded preparation. The source baseline was `origin/main` at
`a26f15944b54a5c269b29126cd498204f1a413ac`. Probability was not accepted as an
integrity boundary: each conclusion below follows the database, ownership, or
generation fence used after an identifier is created.

## Findings

| Identity | Forced-collision behavior | Release action |
|---|---|---|
| Public Django task-result UUID | The baseline allowed duplicate durable rows. Distinct database primary keys let both rows execute, while public result lookup became ambiguous. Migration `0015` and the enqueue allocator add a named global uniqueness constraint, exact database-error classification, bounded candidate regeneration, and fail-closed exhaustion. | Fix in #292 before 0.4.0. |
| Worker lease UUID | The baseline worker uses `update_or_create()`, so a second process can overwrite and adopt a live lease. Ownership can alias and leave a Ray Job indefinitely `RUNNING`. | Fix in #293 before 0.4.0. |
| Workflow run UUID | Fresh allocation reserves an opaque namespace under a database uniqueness constraint, advances a non-resetting per-execution sequence, and injectively encodes both values as UUIDv8. Repeated namespace candidates are retried instead of adopted, while a separate exact-current reclaim path preserves coordinator restart semantics without allocating a fresh identity. | Fixed in #295 for 0.4.0. |
| Compiled invocation UUID | Duplicate admission is rejected, and every adapter callback carries a monotonic action token. | No release action. |
| Topology manifest UUID | The UUID is a database primary key. A collision becomes a storage conflict and the staging transaction rolls back without adoption. | No release action. |
| Preparation workspace and quarantine UUID | Exclusive directory creation or an explicit existence check fails closed without touching the pre-existing path. | No release action. |
| Ray Job submission ID | A deterministic SHA-256 identity covers task primary key, public task ID, attempt, and execution generation. Completion envelopes repeat the same exact identity fence. | No release action. |
| Ray Core task/object IDs | Durable ownership remains the database task primary key plus attempt and generation; the in-memory `ObjectRef` is not a public durable identity. | No release action. |
| Attempt and execution generation | Row-locked monotonic counters and generation-qualified updates reject stale writers. | No release action. |
| Workflow node/page hashes | Producers and readers compare exact canonical bytes and complete run identities. A detected digest collision raises an integrity error rather than adopting content. | No release action. |
| External result digest reference | The baseline did not verify loaded bytes against the advertised digest and length. This is storage integrity rather than UUID allocation. | Fix in #294 before 0.4.0. |
| Enqueue request identity | There is no caller-supplied idempotency key. Every successful enqueue intentionally creates a new task. | Keep application operation-receipt guidance; future effect-receipt work belongs to #289. |

## Public task-result consumer trace

The task UUID is returned as Django's `TaskResult.id`, but the database primary key is
the internal ownership root. Before `0015`, two rows could share the public value while
remaining independently claimable by primary key. Ray Job submission made the split
more concrete because its deterministic job ID includes that distinct primary key.
`RayTaskBackend.get_result()`, task-ID APIs, and Admin lookups could then encounter an
ambiguous public identity.

The repaired boundary is intentionally global across task backend aliases because
current result retrieval is not alias-qualified. The database constraint arbitrates
concurrent writers. Enqueue retries only when PostgreSQL reports the named
`ray_task_id_unique` constraint or SQLite reports the exact task-ID column uniqueness
violation. Input registry failures, priority checks, and every other integrity error
escape unchanged. Encrypted RuntimeEnv storage is regenerated for a replacement ID so
its task-ID-bound authenticated data is never reused.

After a row is created, API, Admin, retry, Ray Core, Ray Job, workflow-run, and attempt
paths all resolve one durable task row. External task inputs remain content-addressed
and are registered once in the same outer transaction. Collision exhaustion rolls that
registration back and creates no claimable task.

## Scale interpretation

UUIDv4 supplies 122 random bits. Its birthday-collision probability remains tiny even at
very large volumes, but a repeated or faulty randomness source can collide immediately.
Database uniqueness, exact error classification, generation fences, and exclusive
ownership are therefore the safety controls; UUID probability is only a capacity
characteristic. Workflow UUIDv8 values use those 122 payload bits as an exact 63-bit
database-unique namespace plus a 59-bit non-resetting row sequence. The database retries
a colliding namespace candidate, and the injective encoding then makes every fresh run
distinct across retained execution rows as well as repeated allocations of one row.
