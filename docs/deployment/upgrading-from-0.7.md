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

There is **no django-ray database migration delta from 0.7.1**. The migration leaf
remains `0028_worker_lease_policy`; retain its common lease authority and normal
migration verification. This does not qualify mixed-version execution or prove
that old code can safely write candidate state. Current execution protocol 1,
rq2 request references and schema-v3 workflow history retain their separate
compatibility contracts. Dormant target tables do not activate target routing.

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
   the compatible Ray/Python tuple. Rebuild immutable RuntimeEnv artifacts and
   inspect the normal migration plan; django-ray has no new migration after 0028
   for this boundary. Verify one common lease policy and matching
   `WORKER_LEASE_SECONDS` across every process sharing the lease table.
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

No new migration means there is no django-ray schema reversal required for this
boundary. It does **not** establish a general code-only downgrade guarantee.
Keep old producers, managers and purgers stopped until the selected recovery path
has been rehearsed and the matching Ray/Python environment is restored.

| Observation | Decision boundary |
|---|---|
| Candidate has not accepted new writes | Start from the independently verified 0.7.1 database/artifact backup, retained keys and original application/Ray environment. Verify the restored state before reopening producers. |
| Candidate has written tasks, results, workflow history or cleanup tombstones | Preserve that state first. Restoring the pre-upgrade backup loses those writes; reconcile them and external effects before choosing restoration. |
| Released code reads selected candidate-written history | Read-only success proves those records and artifacts only. It does not authorize released managers, request execution or cleanup against candidate-written state. |
| Considering a code-only downgrade | The final candidate's rehearsal must explicitly prove every retained format and writer operation that the downgrade would use. Until then, use the stopped-writer restore procedure; unchanged migration files alone are insufficient. |
| Cleanup removed an artifact or a retained encryption key is missing | Database restoration cannot recreate the bytes or key. Restore and verify the matching artifact/key backup or keep the missing-data outcome visible. |
| A task may already have performed an external effect | Neither package rollback nor database restoration authorizes replay. Reconcile the effect before any explicit retry. |
| Custom persistent monitoring data changed | Follow the monitoring component's compatible backup/restore procedure separately; replacing an image tag does not reverse a data-format change. |

The native upgrade recipes test released-reader access to selected restored and
candidate-written task, result and workflow history. Their final PostgreSQL
read-only phase explicitly expects a refusal when the released workflow reader
requests row locks; it does not claim successful graph access in that phase.
Other restored graph reads and stored-row preservation have separate assertions.
These bounded read checks are distinct from restarting old writers.

## Final candidate qualification

The [upgrade recipes](https://github.com/dariuszpanas/django-ray/blob/main/qualification/upgrade/README.md)
select released 0.7.1 with its frozen Ray 2.58 environment for all four native
recipes: Core, Jobs, Jobs manager crash and Core/Ray loss. The candidate uses its
own installed wheel and Ray 2.60 environment. Data preservation also retains
0.4.0, 0.5.0, 0.6.0 and 0.7.0 baselines. Rebuilt released wheels establish their
source boundary; they do not verify independently downloaded published artifacts.

Before accepting 0.8.0:

1. Freeze versioned source and release notes. Require exact-source Linux CI,
   dependency audits and fresh wheel **and** sdist qualification on Python
   3.12–3.14 using the [nonpublishing candidate workflow](../contributing.md#qualify-built-artifacts-without-publishing).
   Review all six installed environments, including migrations, purge, doctor
   and request-reference assertions, with their actual dependency inventories.
2. Review all five data baselines on SQLite and PostgreSQL and all four native
   recipes against 0.7.1. Require old-manager drain, independent restore,
   preserved fields/artifacts, exact crash/retry identities, encrypted snapshots,
   selected workflow history, rollback-read boundaries and owned cleanup.
3. Run the full applicable [deployed matrix](local-kuberay-gate.md) on final
   source. Require standard-profile cold Ray, matching source/archive/images,
   authenticated API and Core/Jobs execution, current rq2 same-Job recovery,
   retired-carrier refusal, protected workflow/history/log observations,
   RuntimeEnv maintenance/refusal and preservation. Retain the separate worker,
   transaction, cancellation, latency and monitoring/authentication receipts.
   A co-resident transition must preserve data, credentials and task execution;
   it does not replace the full-profile cold gate.
4. Join the receipts into a reviewed release decision and rollback table.
   `complete_application_gate: false` and `complete_upgrade_gate: false` remain
   honest selected-workload limits; passing individual jobs does not complete the
   aggregate decision. Preserve failed observations and record missing assertions
   explicitly. Source changes require fresh applicable evidence.

Pre-release Ray 2.60 qualification in
[#703](https://github.com/dariuszpanas/django-ray/pull/703) passed selected hosted
and local scenarios. It identifies its tested source and explicitly stops short
of final release acceptance. Its observations cannot be relabeled for a changed
versioned candidate.

Keep the [Ray Client startup race](../compatibility.md#ray-client-startup-limitation),
the [native shutdown investigation](https://github.com/dariuszpanas/django-ray/issues/557),
and [constrained-profile readiness limits](https://github.com/dariuszpanas/django-ray/issues/608)
visible in the Beta decision. Direct Core/GCS or explicit HTTP(S) Jobs avoids the
Client connection path; a passing retry does not repair that race. Standard-profile
acceptance does not qualify the constrained diagnostic profile or sustained soak.
Python 3.15 and native Compiled Graph remain outside qualified runtime support.
