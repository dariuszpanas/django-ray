# Upgrade from 0.7.1 to 0.8.0

This guide prepares the coordinated **0.7.1 → 0.8.0 Beta** upgrade. The candidate
raises the Ray minimum from 2.58.0 to 2.60.0 and changes worker, workflow and
cleanup behavior. Preparing these instructions does not publish the release or
accept its final qualification. Keep the frozen source, installed artifacts and
deployed receipts together under the [release procedure](../contributing.md#releasing).

Deployments upgrading from an older release must also follow the relevant
[0.6 to 0.7](upgrading-from-0.6.md) and earlier guides. Their request-reference,
lease-policy, encryption-key and cleanup boundaries still apply.

## Compatibility and deployment changes

Use Linux with Python 3.12, 3.13 or 3.14. Rebuild application images, task-manager
environments, RuntimeEnv archives and generic Ray node images from one reviewed
dependency set. Update managers, Ray head and Ray workers together to the matching
Ray/Python tuple; do not connect a released 2.58 manager to a candidate 2.60 cluster.
The supported package floor is Ray 2.60.0, while the repository's reproducible
qualification and deployment pins select that exact version. A newer resolver
result needs its own compatibility checks. Python 3.15 remains preparation-only.

Explicit-address clients and joining workers need the cluster's matching token
and `RAY_AUTH_MODE=token` when using token authentication. The shipped Kubernetes
profile supplies both. Verify authorized and denied Core/Client/Jobs control
paths after replacing Ray, and preserve credentials separately from public
qualification evidence. See the [Ray 2.60 review](../compatibility.md#ray-2600-baseline-for-080).

This upgrade includes migration **`0029_lease_engine_and_hot_indexes`** after
`0028_worker_lease_policy`. Apply it during the stopped-writer window before starting
upgraded workers. Migration `0029_lease_engine_and_hot_indexes`:

- adds database-clock `lease_expires_at` (intentionally unindexed to preserve PostgreSQL
  HOT heartbeat updates) and `lease_policy_revision` columns to `TaskWorkerLease`, and
  backfills existing leases from the active `TaskWorkerLeasePolicy`;
- widens `TaskWorkerLease.queue_name` to `1024` characters for multi-queue workers;
- replaces superseded full-table indexes (`ray_task_heartbeat_idx`, legacy claim/expiry
  indexes, and redundant reverse indexes on dormant target-revision tables) with partial
  hot-path indexes on `RayTaskExecution` (`QUEUED`, `RUNNING`/`CANCELLING`,
  `queue_deadline_at`, terminal `finished_at`, and `result_reference`) and `TaskAttempt`
  (`state` and `result_reference`); and
- captures and restores SQLite triggers around `TaskWorkerLease` table rebuild DDL in
  both forward and reverse directions.

Retain `0028_worker_lease_policy`'s shared lease authority and normal migration
verification. This migration does not qualify mixed-version execution or prove that old
code can safely write candidate state. Current execution protocol 1, rq2 request
references, and schema-v3 workflow history retain their separate compatibility contracts.
Dormant target tables do not activate target routing.

For deployments using the sample manifests:

- Web probes use `/api/livez` and `/api/readyz`; database failure makes readiness
  return HTTP 503. Worker probes use `django_ray_worker_ready`. Preserve the
  configured probe host, timeout and authentication boundaries when adapting them.
- Grafana now has a 180-second startup-probe budget and a 512 MiB memory limit.
  Its CPU limit remains 200m. Include the extra 256 MiB in the monitoring quota
  and capacity budget, then verify dashboard import, datasource queries and
  rendered panels after replacement. Persistent custom monitoring data still
  needs its own compatible backup and rollback procedure.
- Disconnected Ray Core managers no longer claim queued work before their driver
  is initialized. A disconnect during a claim batch returns its unsubmitted
  items; managers keep their lease heartbeat during reconnect backoff. A live
  lease still does not prove that remote execution has stopped.

The new workflow modules include explicit strategy selection, bounded transport
and input/concurrency primitives, checkpoint planning and manually invoked tracing
hooks. They do not automatically enable distributed tracing, durable step resume
or cross-run scheduling in existing tasks. Outer-task retry still replays the
workflow from its entry node. Native Compiled Graph remains default-off with an
empty verified capability set. Review the [workflow contract](../workflows.md)
before opting into any new primitive; preserve idempotency for external effects.

Unregistered-payload discovery and deletion are explicit cleanup modes. Keep
existing cleanup stopped through the upgrade, and review the bounded inventory,
grace-period, tombstone and continuation rules before scheduling these modes.
Do not treat an incomplete scan or exit status 2 as deletion authority. See
[`django_ray_purge_inputs`](../reference/cli.md#django_ray_purge_inputs).

## Coordinated procedure

1. Stop new submissions, schedules and other producers. Stop purge and maintenance
   processes. Drain queued/running work using the released 0.7.1 managers and
   explicitly reconcile uncertain remote outcomes. Do not discard a backlog or
   replay an uncertain side effect to make the drain appear complete.
2. Verify the old managers and remote work have stopped. Back up the database and
   every referenced input, request, result, workflow and RuntimeEnv artifact.
   Retain encryption keys separately and verify an independent restore with the
   released reader. Preserve the original backup before testing candidate writes.
3. Keep all writers stopped while installing the candidate package and replacing
   the compatible Ray/Python tuple. Rebuild immutable RuntimeEnv artifacts,
   inspect the migration plan (`python manage.py migrate --plan django_ray`), and
   apply `0029_lease_engine_and_hot_indexes` (`python manage.py migrate django_ray`).
   Verify one common lease policy and matching `WORKER_LEASE_SECONDS` across every
   process sharing the lease table.
4. Start only the candidate managers. Verify historical tasks, attempts, results
   and workflow graphs through authorized readers. Exercise current Core and Jobs
   execution, encrypted RuntimeEnv recovery, cancellation and manager recovery on
   the candidate tuple. Check protected APIs/Admin, probes and monitoring where
   deployed. Missing artifacts or unsupported execution must have bounded visible
   outcomes; never rewrite a protocol or reference to force acceptance.
5. Reopen producers only after these checks pass and all uncertain outcomes have
   explicit dispositions. Resume cleanup under its reviewed finite-page and
   retention contract, retaining every page's result and following continuation
   cursors until the pass is complete.

The read-only [doctor](../reference/doctor.md) can report local blockers and
incomplete observations, but it cannot perform or certify the drain, remote
termination, artifact availability or rollback. In particular, lease expiry and
a durable `CANCELLED` row do not prove that an external effect stopped.

## Rollback decisions

Migration `0029_lease_engine_and_hot_indexes` is reversible during a stopped-writer
maintenance window (`python manage.py migrate django_ray 0028_worker_lease_policy`),
which drops the additive lease-engine columns and partial indexes while restoring
SQLite triggers and previous indexes. Schema reversibility does **not** establish a
general code-only downgrade guarantee once candidate writes, cleanup tombstones, or
artifact deletions have occurred. Rehearse both forward application and reversal on a
staging copy before upgrading, and keep old producers, managers, and purgers stopped
until the selected recovery path has been verified and the matching Ray/Python
environment is restored.

| Observation | Decision boundary |
|---|---|
| Candidate has not accepted new writes | Start from the independently verified 0.7.1 database/artifact backup, retained keys and original application/Ray environment. Verify the restored state before reopening producers. |
| Candidate has written tasks, results, workflow history or cleanup tombstones | Preserve that state first. Restoring the pre-upgrade backup loses those writes; reconcile them and external effects before choosing restoration. |
| Released code reads selected candidate-written history | Read-only success proves those records and artifacts only. It does not authorize released managers, request execution or cleanup against candidate-written state. |
| Considering a code-only downgrade | Rehearse forward and reverse `0029_lease_engine_and_hot_indexes` migration steps and verify every retained format and writer operation before downgrading. Until verified, use the stopped-writer backup-restore procedure. |
| Cleanup removed an artifact or a retained encryption key is missing | Database restoration cannot recreate the bytes or key. Restore and verify the matching artifact/key backup or keep the missing-data outcome visible. |
| A task may already have performed an external effect | Neither package rollback nor database restoration authorizes replay. Reconcile the effect before any explicit retry. |
| Custom persistent monitoring data changed | Follow the monitoring component's compatible backup/restore procedure separately; replacing an image tag does not reverse a data-format change. |

The native upgrade recipes test released-reader access to selected restored and
candidate-written task, result and workflow history. Their final PostgreSQL
read-only phase explicitly expects a refusal when the released workflow reader
requests row locks; it does not claim successful graph access in that phase.
Other restored graph reads and stored-row preservation have separate assertions.
These bounded read checks are distinct from restarting old writers.

## Upgrade verification checklist

Before cutting production traffic over to 0.8.0:

1. **Package and environment alignment**: Verify Python 3.12–3.14, Django 6.0.8+, and
   Ray 2.60.0+ match across web processes, task managers, Ray head, Ray workers, and
   rebuilt RuntimeEnv archives.
2. **Migration and rollback rehearsal**: Rehearse `python manage.py migrate django_ray`
   through `0029_lease_engine_and_hot_indexes` (and its reverse to `0028_worker_lease_policy`)
   against a restored copy of your 0.7.1 database, verifying lease-policy agreement,
   historical task/result/workflow reads, and encrypted RuntimeEnv recovery.
3. **Runtime and operations checks**: Exercise Core and Jobs task execution, cancellation,
   `django_ray_worker --drain`, `django_ray_doctor`, `django_ray_worker_ready`, protected
   APIs/Admin views, and monitoring dashboards after upgrading.
4. **Known platform boundaries**: Prefer direct Ray Core/GCS or explicit HTTP(S) Ray Jobs
   when avoiding the upstream [Ray Client startup limitation](../compatibility.md#ray-client-startup-limitation).
   Python 3.15 and native Compiled Graph remain outside supported production execution.

Maintainers qualifying a release candidate also follow the full deployed matrix and
artifact checks in [Local KubeRay Final Gate](local-kuberay-gate.md) and
[Contributing](../contributing.md#releasing).
