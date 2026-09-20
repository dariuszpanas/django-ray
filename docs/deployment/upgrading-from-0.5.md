# Upgrade from 0.5.0 to the unreleased candidate

This page describes changes currently on main after 0.5.0. It is preparation for
an eventual release, not a release announcement or approval to deploy a candidate.
The planned next release is 0.6.0, tracked in
[the release scope](https://github.com/dariuszpanas/django-ray/issues/508).
Default workflow graph activation and final upgrade acceptance remain pending.

## Changes currently on main

### Changed

- Unversioned Ray Job payloads and positional Core durable-task submissions
  are retired. They return a fixed, non-retryable execution rejection before
  application setup or input loading. Malformed payloads also fail the CLI
  with its unsupported-execution exit code, 78. Use current managers and their
  independently bound request carriers; do not replay stored old submissions.
  Complete the drain and stop old writers before upgrading. Resolve uncertain
  outcomes explicitly. Historical rows/artifacts and standalone nested
  workflow execution remain supported without rewriting their identities.
- Django Admin dashboard links now require an explicit top-level Django
  `RAY_DASHBOARD_URL` setting. The implicit `http://localhost:8265` fallback is
  removed. Configure a URL reachable from the operator's browser; local
  port-forward users must explicitly set localhost. Invalid or missing URLs show
  a configuration message while retaining task identifiers. Generated links
  normalize the base URL and encode job/task identifiers.
- Admin workflow messages distinguish a running workflow, terminal-only
  reporting, expired details and missing details. These explanations do not
  enable full graph publication by default or reconstruct older missing graphs.

### Documentation and qualification

- Document a Kubernetes onboarding path with separate execution and browser
  dashboard addresses, and worker-owned temporary storage prepared before Python
  starts on both Ray head and workers.
- Add public workflow/Admin qualification for full and terminal-only success,
  failure and recovery, including cold-Ray observations. This qualifies the
  explicitly enabled publisher; it does not establish default graph availability.
- Qualify upgrades from an immutable 0.5.0 baseline on SQLite and PostgreSQL,
  including independent restores, Core/Jobs execution and manager-loss recovery.
  The receipts retain their incomplete release-acceptance status.
- Add pinned YAGA workflow checks and repository-wide Typos checks for docs,
  source, tests and configuration.
- Replace commitlint with released YAGA 0.2.0 in the required commit workflow,
  local hook and Make commands while preserving the existing structural rules.
  Add isolated commit spelling checks and retain the narrow trusted Dependabot
  exemption; repository spelling and ordinary CI still apply to dependency PRs.

## Configure the operator's dashboard address

Admin no longer assumes that Ray Dashboard is available at localhost. Set the
browser-facing address explicitly in your application's Django settings:

```python
# settings.py; this is a top-level setting, outside DJANGO_RAY.
import os

RAY_DASHBOARD_URL = os.environ.get("RAY_DASHBOARD_URL")
```

Setting a process environment variable alone is insufficient unless your settings
module reads it. Use an HTTP(S) base URL without embedded credentials, query
parameters or a fragment. It must resolve and be reachable from the operator's
browser. An internal Kubernetes Service hostname usually does not meet that
requirement. Missing or invalid settings show a configuration message instead of
a link; the durable task identifiers remain available.

For local port forwarding, explicitly configure `http://localhost:8265` and keep
the forward running on the browser's computer. For shared access, configure the
protected ingress or proxy address. Django Admin authentication does not protect
that separate endpoint. Follow the [dashboard access procedure](kubernetes.md#ray-dashboard-links-from-django-admin)
and verify both the dashboard root and an actual submitted task's Admin link.
A dashboard root returning HTTP 200 alone does not prove a deep link works.

This setting does not change how managers connect to Ray. Keep the working
execution address and mode for the existing deployment. The
[worker guidance](../worker-modes.md) explains the supported choices.

## Preserve workflow history and storage

Admin now explains whether a workflow is still running, used terminal-only
reporting, or has expired or missing details. Terminal-only reporting retains a
summary without graph details. Missing details cannot be reconstructed by
changing settings after the task completes.

These repairs do not enable the full-detail publisher by default. Existing
schema-v3 pilot qualification is evidence for that explicitly enabled path only.
Do not promise default graphs or switch reporting modes solely to hide an Admin
availability message.

Where the full publisher uses SQLite preparation, provide a private worker-owned
mode-0700 `TMPDIR` before Python starts on both Ray head and workers. Preserve
workflow artifacts, input/result objects, RuntimeEnv archives and their encryption
keys. Changing the temporary directory does not recover an earlier failed
publication. Follow the [Kubernetes deployment guide](kubernetes.md) for the
storage and dashboard setup details.

## Reconcile legacy Ray Job failures

A legacy Ray Job that reports failure without a trusted completion now becomes
`LOST`, with an explicit unknown-effects message and no automatic retry. Managers
do not fetch or persist its job logs to infer an outcome. A terminal legacy Job
with a missing or malformed completion also becomes `LOST` after the existing
completion grace period, including when Ray reports success. Review external effects
before an explicit recovery decision; `LOST` does not mean the callable never ran.
The transition remains fenced by the current worker lease, job, attempt,
generation and completion. A concurrent trusted completion or replacement owner
must win over a stale failure observation.

Current Ray Jobs remain supported: authenticated completion, same-version manager
recovery and retries authorized by a valid completion retain their existing
behavior. Historical rows and artifacts are not rewritten or removed. This
change retires only the untrusted failed-job diagnostic/retry fallback; it does
not activate another protocol or restore mixed-version execution support.

## Coordinated stopped-writer upgrade

The current changes after 0.5.0 add no django-ray database migrations or new task
execution protocol. Recheck the final selected release and your own application's
migration plan; this statement is not a mixed-version execution guarantee.
Use the [coordinated Beta procedure](../stability.md#coordinated-beta-upgrades):

1. Record the exact released and candidate package, image, Python and Ray versions.
   Verify the selected runtime's matching requirements. Stop submissions,
   schedules and other producers, and let old managers drain. Resolve uncertain
   outcomes explicitly; do not delete or relabel work to make the inventory empty.
2. Stop old managers and purgers. Verify that no old writer remains before taking
   the final database-and-artifact backup. Back up encryption keys separately
   through the existing secret backup process. Independently restore and verify
   the backup before modifying the stopped database.
3. Run `python manage.py migrate --plan` with the final candidate installed and
   review the complete plan. Apply required migrations, replace the runtime
   components together, and start only candidate managers. Do not carry active
   ObjectRefs into another Ray session.
4. Read historical success, failure and retry records and their retained artifacts.
   Check missing/corrupt artifact behavior and the expected Admin availability
   messages. Test a new task's dashboard link through the actual proxy. Run bounded
   Core or Jobs smoke and recovery checks for the deployment's selected modes
   before reopening submissions.
5. Retain the old backup, observed outcomes and cleanup evidence until the
   deployment's acceptance and retention policy allows removal.

## Rollback decisions

| Situation | Required decision |
| --- | --- |
| Candidate has not written new work | Keep writers stopped, verify the independently restored baseline, and review the actual migration plan before choosing code or backup rollback. |
| Candidate has written new work | Restoring the old backup discards those writes. Stop writers and reconcile the new work before choosing rollback; restoring a database cannot undo external task effects. |
| Old code reads candidate-written history | Read-only fixture success does not authorize restarting old managers. Verify the retained schema and execution boundary separately. |
| PostgreSQL read-only graph access | The released reader requests row locks and is refused by the database fence. The rehearsal verifies that refusal; it does not claim that those graph reads work in read-only mode. |

Never replace the original backup during a rollback experiment. Preserve keys and
referenced artifacts together with the matching database state.

## What the public evidence establishes

The [public upgrade recipes](https://github.com/dariuszpanas/django-ray/blob/main/qualification/upgrade/README.md) export the
reviewed 0.5.0 baseline and the exact candidate into independent installed-wheel
environments. Database and native Core/Jobs scenarios cover SQLite and PostgreSQL,
independent restore, retained history, candidate execution, encrypted RuntimeEnv
and defined manager-loss recovery. The Jobs crash case preserves job, attempt and
generation with one application invocation. Core/Ray loss records LOST without
automatic replay, then exercises an explicit retry after old processes retire.

These results cover the qualified source and fixture, not an inaccessible private
deployment. Receipts deliberately retain `complete_upgrade_gate: false`.
Final-candidate rendered workflow history, uncertain-side-effect reconciliation,
execution retirement, applicable deployed cold-Ray observations and the operator's
actual dashboard/proxy still require acceptance. Run the affected checks again
when their source or deployment changes; do not relabel an old receipt as a new
release result.
