# Upgrade from 0.4.0 to the planned 0.5 release

Use the [coordinated Beta procedure](../stability.md#coordinated-beta-upgrades):
drain with the old version, stop writers, preserve the database and referenced
artifacts, then update the package and Ray/Python tuple together. This procedure
describes the protocol-1 candidate. It does not activate the separately developed
cohort runtime or promise a mixed-version rolling upgrade.

## Candidate boundary

The released baseline is exactly 0.4.0 at library migration
`0018_workflow_run_allocation`. The candidate adds migrations `0019` through
`0026`; the sample app also adds `0001_sample_admission_budget`. The active write
protocol remains 1. Creating dormant target tables does not enable target routing
or move work between Ray sessions.

Install matching Ray versions on managers, head and workers; the candidate requires
Ray 2.58.0 or newer. Preserve exact runtime matching required by the selected
transport. The [Ray Client startup limitation](../compatibility.md#ray-client-startup-limitation)
remains unresolved by the version increase.

Before replacing a sample deployment, prepare its split credentials and explicit
superuser as described in [Local Credentials](local-credentials.md). Preserve
existing secrets and data; generating different encryption keys is not an upgrade
step. Backups must include input/result objects, workflow detail storage and
RuntimeEnv artifacts as well as the database. Retain the keys needed to decrypt
those artifacts separately through the deployment's existing secret backup process.

## Stopped-writer sequence

1. Record the exact old and candidate package, image, Ray and Python versions.
   Stop application submissions, schedules and other producers. Let old managers
   drain queued and running work. A missing remote outcome remains a blocker;
   do not relabel, delete or replay a task to make the inventory empty.
2. Stop old managers and purgers after reconciliation. Confirm that every producer
   and writer is stopped before taking the final backup. Verify an independent
   database-and-artifact restore while retaining the original backup unchanged.
3. Inspect `python manage.py migrate --plan` with the candidate installed. Apply
   the migration plan to the stopped database, then replace the complete runtime
   tuple and start only candidate managers. Do not copy active ObjectRefs into
   the new Ray session or treat a ready pod as proof that Jobs can execute.
4. Read historical successful, failed and retried tasks, their results and workflow
   detail. Check missing/corrupt artifact behavior explicitly. Run bounded current
   Core and Jobs tasks and the applicable manager-loss recovery checks before
   reopening submissions.
5. Record the actual migration, runtime, preservation and cleanup outcomes. Keep
   the pre-write backup until the deployment's acceptance and retention policy
   allows its removal.

## Migration-specific rollback boundaries

| Change | Boundary |
| --- | --- |
| `0019` protocol/provenance fields, policy, admission token and database fences | Reversal removes that metadata. Retaining the schema while using old code is a different operation from reversing migrations. |
| `0020` legacy-open rollback fence | Preserve this fence with `0019` for the dormant protocol-1 compatibility path; do not edit policy rows or delete the admission token to bypass an error. |
| `0021` Ray Job request references and input payload kinds | Reversal drops the new reference/kind fields. Stop managers and artifact purgers first; a schema reversal is not proof that retained request artifacts are understood by the old code. |
| `0022`–`0026` target, binding, route, capability and execution-evidence tables | Each reverse guard requires its owned tables to be empty. A refusal is a rollback blocker, not permission to delete evidence. |
| Sample admission `0001` | Reversal removes the sample admission table. It does not undo task effects or restore credentials. |

The database rehearsal checks exact 0.4.0 read-only access to the migrated schema
after one candidate enqueue, including the retained candidate row. It also restores
the pre-write backup into a different database and proves that the candidate row is
absent there. These are separate checks:

- **Read-only code rollback:** verifies the fixture's history, artifacts and queued
  candidate task without reversing schema or admitting old writers.
- **Backup restore:** verifies recovery of the pre-write snapshot. It loses later
  writes and must not replace the current database automatically.
- **Resuming old execution:** still requires deployed qualification of the selected
  migrations, persisted formats, managers and Ray/Python tuple. Neither data check
  proves this is safe.

## Qualification still required

The released 0.4.0 workflow topology reader uses `SELECT FOR UPDATE`. PostgreSQL
rejects that operation in a read-only transaction, so read-only code rollback
does not provide full workflow graph access. Retain the database fence; use the
qualified current reader for graph access rather than enabling old writers.

The [database recipe](https://github.com/dariuszpanas/django-ray/blob/main/qualification/upgrade/README.md) is bounded to SQLite
and PostgreSQL with no Ray process or manager. Its receipt deliberately retains
`complete_upgrade_gate: false`. Complete the deployed old-manager drain,
writer/purger inventory, supported-format and carrier-retirement decision, current
execution/recovery, rendered historical workflow and encrypted RuntimeEnv checks
before calling the coordinated upgrade qualified. The
[local KubeRay gate](local-kuberay-gate.md) supplies current-runtime evidence; its
passing handoff scenario does not establish the stopped-writer backup sequence.
