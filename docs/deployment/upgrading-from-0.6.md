# Upgrade from 0.6 to 0.7

This guide covers the coordinated **0.6.0 → 0.7.0 Beta** upgrade. A version bump,
passing unit tests or a selected qualification receipt does not establish release
acceptance. Keep the final source, artifact and deployed evidence together before
publishing or upgrading.

The [0.5 to 0.6 guide](upgrading-from-0.5.md) remains the historical guide for
that boundary. django-ray remains Beta; this release does not change the
[1.0 readiness decision](../one-zero-readiness.md).

## Compatibility changes

The `django_ray_purge_inputs` command now processes a finite page of registered
payloads, with separate registry, attachment and output budgets. Its default
page contains at most 100 candidates. **Exit status 2 is an incomplete result**:
another cursor page or unresolved attachment evidence remains. Existing
schedulers that assume a single invocation traverses the full registry must be
updated before cleanup resumes.

- Retain the JSON outcome from every page and follow `next_cursor` using the same
  retention settings and explicit deletion mode. Pass the token as
  `--cursor=TOKEN` (or `--cursor="$NEXT_CURSOR"` in a shell): quoting a separate
  argument does not protect a valid token beginning with `-` from option parsing.
  Dry-run remains the default.
- Start a fresh pass after completing the cursor chain to revisit unresolved
  candidates and concurrent inserts before the previous page boundary. Repeating
  the original command without its cursor can revisit retained leading rows.
- Treat over-budget attachment histories as retained, unresolved evidence. Do
  not turn a continuation limit into deletion permission. `retained` counts
  candidates not deleted; it does not prove that their external objects exist.
  A final page with exit 0 does not erase failed or incomplete earlier pages.
- Cursor tokens hide storage locations and become invalid when Django's
  `SECRET_KEY` changes. A cursor does not establish a hard storage I/O deadline.

See the [CLI reference](../reference/cli.md#django_ray_purge_inputs) for the
options, output fields and continuation rules. Review this behavior against the
actual scheduler's exit-code handling and retained audit output.

New Ray Job request objects use references beginning with `rq2ref:v1:`. The
discriminator separates request references from generic task-input and result
references; their underlying filesystem, S3 or GCS object remains content
addressed. The prefix counts toward the existing 500-byte ASCII reference limit,
so review long object-storage prefixes before restarting managers. It does not
change the execution protocol or grant a new storage location permission.

Current request readers and authorized cleanup still accept canonical historical
native references. New attachments require the wrapped form and refuse a matching
native registry alias, including a tombstone or a row recorded under another
payload kind. If both spellings already exist, cleanup retains their shared
object and reports incomplete evidence. Resolve that state explicitly; do not
strip prefixes, rewrite registry keys or delete tombstones to bypass the refusal.

Stop old cleanup processes as well as producers and managers during the upgrade.
Released generic input deleters reject wrapped references, but that rejection
does not qualify mixed-version writers or cleanup. Once the candidate has stored
wrapped references, returning to 0.6.0 requires an independently restored
pre-upgrade database and matching artifact copy, with candidate writes and
external effects reconciled. Read-only historical checks do not authorize old
request loading, old cleanup or old execution against candidate-written state.

Mandatory dependency metadata now requires patched `multidict`, `urllib3` and
`virtualenv` versions; rebuild application and RuntimeEnv artifacts from the
candidate dependency set. Supported native execution remains Linux with Python
3.12–3.14 and Ray 2.58 or later. Python 3.15 package preparation without Ray Core
does not add native runtime support. See [compatibility](../compatibility.md).

The bundled monitoring stack moves to Prometheus 3.13.4 LTS and Grafana 12.4.12
([#627](https://github.com/dariuszpanas/django-ray/pull/627)). The sample uses
ephemeral storage. For persistent custom deployments, take compatible backups
and follow the upstream upgrade procedures before changing images: **Prometheus
3 data cannot be opened by the formerly bundled 2.48 image**. Returning to the
old container image alone is not a data rollback. Back up Grafana configuration,
plugins and its database separately, and follow its own upgrade/rollback plan.
The finite sample checks ephemeral replacement, selected queries and one rendered
Ray panel; it does not qualify a persistent database migration or every plugin.
See [monitoring versions and upgrades](kubernetes.md#monitoring-versions-and-upgrades).

After ephemeral Grafana replacement, the production importer can restore its
generated Ray dashboards with `python /scripts/import_dashboards.py --once`.
It confirms every generated UID and fails if an import fails. It overwrites
those generated dashboards; preserve intentional customizations separately.
The default sidecar lifetime is unchanged. Verify datasource queries and the
rendered panels after replay instead of treating successful import as sufficient.

## Bounded diagnosis

The candidate adds Linux read-only diagnosis:

```bash
python manage.py django_ray_doctor --database default --format json
python -m django_ray.doctor --settings project.settings --database default --format json
```

The standalone command includes settings import and model startup in its
supervised budget. It observes scalar configuration, migration, protocol, lease,
work and registered-cleanup state without hydrating task callables or payloads
or issuing package-owned Ray, HTTP or storage-provider requests. Settings remain
trusted Python; the supervisor does not sandbox their own side effects.

This first slice cannot confirm remote targets, queue capacity, artifact
availability or orphan inventory. An otherwise clear report therefore exits 2
for incomplete evidence. An observed blocker exits 1 when cleanup is confirmed;
unconfirmed cleanup also forces exit 2 while retaining completed observations.
Neither outcome permits drain, cleanup, replay or other maintenance. Keep the
explicit runtime and artifact checks below even when the report contains no
observed blocker.

## Migration and execution boundary

The candidate also adds bounded retained RuntimeEnv maintenance and opt-in
`RUNTIME_ENV_STORAGE_MODE="required"`. Existing `plaintext` and `encrypted` modes
keep their dual-read behavior. Required mode encrypts new snapshots and refuses
plaintext execution and retry; it must be an explicit coordinated policy change.
Use [`django_ray_runtime_env`](../reference/cli.md#django_ray_runtime_env) dry-run,
rewrite and complete check passes with the retained decryption ring before
enabling it. Preserve immutable task/profile/hash and attempt identity; malformed,
unidentified, missing-key or concurrently changed rows require explicit attention.
Current-table verification never authorizes removing keys needed by backups.
Review the [key rotation and rollback procedure](../runtime-environments.md#maintain-retained-snapshots-and-require-encryption)
and exercise encrypted execution plus plaintext refusal before resuming submissions.

Migration `0028_worker_lease_policy` adds one database-scoped common lease policy,
seeded explicitly to 60 seconds. It preserves existing task and lease rows and does
not infer the old deployment's configured duration. Retain migration 0027 and run
the normal migration command against the restored candidate database.

Use a coordinated stopped-writer upgrade. Stop producers, schedules and cleanup
processes; drain old work with the 0.6.0 managers and explicitly reconcile any
uncertain effects. Stop every old manager before starting the candidate.
Mixed-version execution, rolling handoff and executing retired payload formats
remain outside this Beta contract. The candidate does not activate the dormant
target-compatibility protocol or make old execution carriers safe to replay.
Current production execution remains protocol 1.

Before migration, stop and drain all old workers, purgers and operational callers.
Reconcile stale active lease rows under the old deployment's agreed policy before
switching code, particularly when it used a duration other than 60 seconds. Expiry
alone does not prove that a process or remote side effect has stopped.

Before restarting managers, verify one common `WORKER_LEASE_SECONDS` across all
processes sharing the lease table and compare it with `django_ray_lease_policy`.
Custom deployments must explicitly change the newly seeded authority while the
cohort is stopped. The revision-checked command refuses every active lease,
including stale rows, and running/cancelling work; it never retires them for you.
Update every process's settings before restart. Queue names and backend aliases
do not isolate lease expiry, and each heartbeat interval must remain below the
common duration. See the [policy command](../reference/cli.md#django_ray_lease_policy).
Old binaries do not honor the new guard: mixed old/new writers remain unsupported.

Before any migration or deletion, back up the database and every referenced
input, result, workflow and RuntimeEnv artifact. Keep encryption keys separately
and verify them with an independent restore. Database backup alone cannot
recover an object already removed by cleanup or reverse an external task effect.

After installation and migration, prove current Core and Jobs execution,
historical task/result reads, workflow graph access, encrypted RuntimeEnv
recovery and the configured Dashboard/proxy path. Reopen submissions only after
the selected checks pass and uncertain outcomes have explicit dispositions.
Resume cleanup with the revised scheduler contract after those checks.

## Rollback decisions

| Observation | Decision boundary |
|---|---|
| Candidate has not written new data | An independently verified 0.6.0 database and artifact restore is the rollback starting point. Keep old producers, managers and cleanup stopped until the restored state is checked. |
| Candidate has written tasks, results or workflow history | Restoring the old backup loses those writes. Reconcile them before choosing restoration; do not overwrite the only copy containing candidate writes. |
| Old code reads candidate-written history | Database-enforced read-only success proves only the selected records and artifacts. It does not authorize old managers, old payload execution or the old unbounded cleanup command. |
| Candidate has persisted `rq2ref:v1:` request references | Do not run 0.6.0 managers or cleanup against that state. Use an independently restored pre-upgrade database and matching artifacts after reconciling candidate writes; never remove the discriminator as a rollback shortcut. |
| Cleanup deleted an external object | A database-only rollback cannot restore its bytes. Restore and verify the corresponding artifact backup or retain the missing-artifact outcome explicitly. |
| Persistent monitoring data has been opened by Prometheus 3 | The former Prometheus 2.48 image cannot read that data. Use a compatible independently retained backup and the upstream rollback procedure; changing the container tag alone is insufficient. |
| An uncertain task may have produced an external effect | Neither package rollback nor database restoration proves that retry is safe. Reconcile the effect before explicit retry. |

Keep migration 0028 applied during old-code read-only checks; the old reader may
ignore its additive table. Reversing 0028 removes the policy authority and refuses
active leases or running/cancelling work. That database check is not proof that
old processes or remote effects stopped, and does not waive the artifact, key,
request-carrier or monitoring rollback boundaries above. Retain migration 0027.

## Candidate qualification checklist

The [upgrade recipes](https://github.com/dariuszpanas/django-ray/blob/main/qualification/upgrade/README.md)
pin the released 0.6.0 tag and build its wheel using its own locked environment.
The candidate uses a separate installed wheel and environment. The data workflow
also retains 0.4.0 and 0.5.0 baselines for historical readability. These rebuilt
wheels are not a verification of the separately published release binaries.

Before accepting the final candidate:

1. Freeze the intended source and verify version agreement, Beta classification,
   release notes, dependency audits and the full exact-source Linux CI matrix.
   Preserve installed wheel/source-distribution checks across supported Python
   versions using the [nonpublishing candidate workflow](../contributing.md#qualify-built-artifacts-without-publishing);
   require the installed doctor, purge and request-reference assertions in all
   six fresh environments: wheel and sdist on Python 3.12, 3.13 and 3.14.
   A wheel built from an sdist is not a separate fresh sdist installation, and
   source-checkout tests do not replace either artifact check.
2. Run the data matrix for all three released baselines, 0.4.0, 0.5.0 and 0.6.0,
   on SQLite and PostgreSQL against the final 0.7.0 candidate. Require all eight
   phases in each of the six runs, including independent restore, original
   field/artifact equality, candidate writes, database-enforced read-only old-code
   access and the old-backup write-loss observation. The 0.6.0 pair covers the
   immediate release boundary; older pairs add historical-reader evidence and
   do not authorize mixed-version execution.
3. Run all four native recipes against 0.6.0: Core, Jobs, Jobs manager crash and
   Core/Ray loss. Require current execution, preserved history, encrypted
   RuntimeEnv, exact recovery identities and owned-resource cleanup.
   On both databases, require the ordinary, non-crash candidate Core phase's
   `worker_modes` receipt with all nine facts for each installed `--sync` and
   `--cluster auto` manager. Each must actually consume its own queue with one
   attempt/generation and the expected mode,
   preserve older unselected work, exit gracefully, release its lease and remove
   only owned rows without changing prior history. These sequential observations
   share existing capacity and a finite budget; they do not qualify every sample
   Deployment unchanged.
4. Run the applicable [deployed matrix](local-kuberay-gate.md) with the final
   candidate's **standard acceptance profile**. Require source, archive and
   image correspondence, rendered current and archived workflow history, the
   configured Dashboard proxy, API/task smoke and an explicit required cold-Ray
   pass with two disjoint two-node generations. A constrained diagnostic run
   cannot substitute for this release gate.
   Require both invalid RuntimeEnv cases and all direct Jobs request-reference
   assertions in each Ray generation. Require the protected HTTP/log observations
   and the actual driver-argument check, unsupported queued-work preservation
   while the current manager heartbeats, pre-invocation protocol refusal, and
   the fixed reservation-failure showcase. Inspect all sixteen application log
   receipts and the two separate host protected-log receipts. Active-write
   protocol 1 and the original admission state must be preserved; this is not
   mixed-version or new-target activation evidence. The normal application
   smoke does not invoke doctor; its Linux process, database and installed-package
   evidence remains separate.
   Also require the current image's `worker_images` receipt for source-derived
   sync and ML queue managers after the cold generations and reporting. Verify
   shipped CLI/queue/concurrency, actual PID1 and Pod/image ownership, one result
   and attempt, unchanged unselected work, lease release and owned cleanup.
   Confirm the original manager is absent before replacements and restore it
   only after temporary controllers and Pods are gone. Keep fixture substitutions
   explicit. Simple ML queue consumption does not qualify an ML framework or
   model lifecycle.
5. Retain the separate controlled Core and Jobs cancellation proof
   ([#626](https://github.com/dariuszpanas/django-ray/pull/626)): actual progress,
   no-cancel completion, accepted lifecycle cancellation, durable status, and
   independent callable or original-driver exit. Do not equate a `CANCELLED`
   row with stopped execution or extend the result to arbitrary descendants
   and external effects. Require ordinary-result presentation checks and the
   monitoring replacement/reimport/query/browser assertions on the final source;
   server-HTML inspection and a browser-rendered panel are distinct evidence.
   Require all twelve `ray_auth` facts: Dashboard, Jobs, Client and GCS each deny
   anonymous and invalid credentials and accept the authorized client. This
   observation follows dashboard replay and precedes browser rendering, with
   bounded observation and cleanup. Successful authentication does not attest
   target compatibility or authorize a worker to claim a different cohort.
6. Retain PostgreSQL transactional-enqueue evidence and assess other affected
   performance, lifecycle and storage scenarios. Inspect actual child-process
   and supervisor exits as well as pytest outcomes; a successful pytest result
   cannot negate a native child signal. Keep the
   [native shutdown investigation](https://github.com/dariuszpanas/django-ray/issues/557)
   and [diagnostic readiness investigation](https://github.com/dariuszpanas/django-ray/issues/608)
   visible in the final Beta risk decision. A diagnostic resource profile or a
   passing retry does not resolve an earlier failure's cause. The owned
   single-worker acceptance fixture excludes spontaneous request-count recycling;
   the bounded recycle experiment remains separate mechanism evidence, not a
   diagnosis of the earlier timeout samples or a production settings change.
7. Inspect the final receipts and unresolved qualification issues. Record any
   missing assertion as missing, together with the operator's drain, backup,
   rollback and uncertain-outcome decisions.

Database and native receipts intentionally retain `complete_upgrade_gate: false`;
the application stage similarly describes its selected coverage. Their scope
must be reconciled with the full release checklist. Historical receipts from
another candidate source cannot be relabeled as 0.7.0 evidence. The new worker
and authentication assertions still require exact-source hosted proof. The
source-derived worker phase has its own 180-second budget: 135 seconds for
observation and 45 seconds reserved for cleanup, after the 2820-second cold
application phase. That application allowance combines the
repaired 2160-second core/rq2 envelope with 660 seconds for two generations of
showcase, protocol and protected-log observations. The resource footprint
stays unchanged. The native step permits 55 minutes, including the separate
180-second worker phase and inherited 300-second external aggregate allowance.
That allowance does not guarantee capacity for every operation's maximum
timeout. Review actual combined timing evidence for the selected source.
Preserve finite deadlines, cleanup reserves and owned capacity, and
inspect actual integrated receipts before acceptance. Preparation does not
authorize a release workflow dispatch, tag or publication; follow the
[release procedure](../contributing.md#releasing) after candidate acceptance.
