# Bounded read-only doctor

`django_ray_doctor` collects dated configuration and database observations on
Linux. This first slice deliberately returns **exit 2 (incomplete)** even when
its available observations show no blockers: Ray reachability, target
attestation, queue capacity, artifact availability and orphan inventory remain
unavailable. An observed blocker produces exit 1. The command does not authorize
an upgrade, rollback, deletion, drain or quarantine.

```bash
python manage.py django_ray_doctor --database default --format json
```

The management command's budget begins after Django has initialized the caller.
Use the standalone entry point to include settings import and model startup in
the supervised budget:

```bash
python -m django_ray.doctor --settings project.settings --database default --format json
```

The settings module must be importable by a fresh interpreter in the current
directory/environment. `settings.configure()`, `override_settings()`, and
in-memory databases cannot be reproduced by an independent connection and are
unsupported. Runtime changes to an imported settings object are not transferred;
use an importable module that represents the intended configuration. A Django
application that cannot initialize normally can still use the standalone entry
point with a suitable trusted settings module.

## Observations and boundaries

| Section | Evidence and interpretation |
| --- | --- |
| `configuration` | Package version, selected runner/storage names and bounded RuntimeEnv encryption configuration shape. No key IDs, key bytes, storage locations, decryption or complete application-settings validation. |
| `database` | A dedicated supported connection opened with read-only protection. No task execution or application health claim. |
| `migrations` | Counts of expected shipped django-ray migrations, applied names and unknown names. Missing/unknown migrations block further model observations. Other applications' migrations are outside this check. |
| `protocol` | Current supported execution range and validated durable rollout policy. Invalid policy is a blocker. |
| `leases` | Shared `lease_policy_seconds` and `lease_policy_revision`, plus active, fresh, stale, malformed and active legacy counts. Fresh requires an active unstopped lease, start/heartbeat no later than the observation, and heartbeat within the database authority's duration, which must match `WORKER_LEASE_SECONDS`. |
| `work` | Queued, running, cancelling and terminal counts; unknown states, retired/unsupported nonterminal protocol or metadata, expired queued work, missing/stale/future active heartbeats and queued work without observed compatible protocol leases are blockers. |
| `registered_cleanup` | Counts of active/purged registry rows, invalid states and recorded cleanup errors. This never loads error text, references, task arguments, results or progress. |
| `target`, `queue_capacity`, `artifact_availability`, `orphan_inventory` | Explicitly unavailable. No Ray, HTTP or storage-provider request is made by these probes. |

Lease queue names are informational and are not authoritative consumer routing.
A compatible protocol lease does not establish capacity for a particular queue
or Ray target. A missing/stale heartbeat is an observation, not proof that a
process or remote task has died. Missing registered cleanup errors do not prove
that storage is empty or that there are no orphaned objects.

Before observing leases, work or registered cleanup, the database probe checks
the shared lease authority against the selected settings. A disagreement returns
`lease_policy_mismatch`; missing or malformed authority returns
`lease_policy_invalid`; an unreadable authority returns
`lease_policy_unavailable`. These sections are unavailable, not counts of zero
workers or tasks. Completed database, migration and protocol observations remain
visible. Use the read-only
[`django_ray_lease_policy`](cli.md#django_ray_lease_policy) command to inspect
configured and authoritative values. Doctor never initializes, repairs or changes
the authority; an intentional policy transition requires the command's documented
stopped, drained cohort and revision checks.

Configuration and database are separate probes with their own timestamps. The
database observations share one transaction (repeatable read and read only on
PostgreSQL); the complete report is not an atomic cross-system snapshot.
Completed sections survive a later probe failure. Cleanup failure is reported
explicitly and prevents a successful/inferred complete result.
Completed dated counts remain visible when cleanup is unconfirmed; missing
sections receive `cleanup_unconfirmed`, and the top-level
`cleanup_confirmed=false` forces exit 2 even if retained counts show blockers.

## Finite contract

Schema `django-ray.doctor`, version 1, uses only fixed diagnostic codes and a
closed vocabulary of scalar values. Each section has `outcome`, `code`,
`observed_at` and `values`; the report includes limits, elapsed time,
`cleanup_confirmed`, `complete` and `exit_code`.

| Limit | Default / maximum |
| --- | --- |
| Total supervised budget | 15 / 60 seconds (`--total-seconds`) |
| Each probe, including its normal cleanup | 5 / 20 seconds (`--probe-seconds`) |
| Cleanup reserve | 2 seconds within each probe; an additional 2 seconds reserved within the total for parent fallback cleanup |
| Minimum valid budgets | Total 5 seconds; probe 3 seconds; probe must fit within total minus outer cleanup reserve |
| Concurrent owned processes | One supervisor and one probe; probes run serially |
| PostgreSQL connection timeout | At most 3 seconds, reduced to remaining probe work time (libpq integer-second minimum is 1); supervision also covers connection establishment |
| PostgreSQL statement / lock timeout | 1,000 / 250 milliseconds, with the probe deadline as the outer bound |
| SQLite busy timeout / progress callback | 250 milliseconds / every 1,000 virtual-machine instructions, checking the monotonic deadline |
| Probe-issued SQL statements / returned rows | 32 / 64; aggregates return scalar counts rather than task/lease text or payloads |
| Package-owned external requests | 0 |
| Probe stdout / stderr | 64 KiB / 4 KiB, enforced while reading; stderr is discarded |
| Individual observation frame | 2 KiB, validated before forwarding |
| Final stdout report | 32 KiB including newline; eleven independently bounded observations fit before final rendering |
| Configuration shape | At most 256 configured keys, 32 encryption keys, 64 characters per key ID, 43 per encoded key; at most 8 secret fallbacks of 4,096 bytes/characters each |

The SQL budget covers commands issued through the diagnostic wrapper, including
transaction commands, not internal driver connection handshakes. The row limit
bounds returned data, not database work: aggregates can scan many rows and must
still meet statement and process deadlines. SQLite opens an existing file with
`mode=ro` and `query_only`; PostgreSQL uses a new connection with enforced
read-only and statement/lock settings. The caller's transaction, settings,
connections and SQLite callbacks are untouched.

Only standard Django SQLite and PostgreSQL backends with `USE_TZ=True` and UTC
database timestamps (`TIME_ZONE` absent/`None` or `UTC`) are supported. Naive or
non-UTC database configurations, custom backends, SQLite URI/memory databases,
SQLite options other than `timeout`, and PostgreSQL options outside the finite
connection/TLS/options allowlist return unsupported. No pool or custom
connection callable is initialized.

## Isolation and cleanup

The package's probes perform read-only observations. **The selected settings
module is trusted Python code:** importing it can itself perform arbitrary
network activity or mutations. Process isolation and deadlines do not sandbox
those side effects. The probe initializes only django-ray's model registry; it
does not run application `AppConfig.ready()` hooks, database routers, persisted
task callables or payload deserializers.

On timeout, output overflow or interruption, an owned Linux supervisor terminates
and reaps its probe process group. Subreaper state is changed only in that
supervisor, never in the caller. Descendants that deliberately create another
session/process group are outside the signal scope; observed escaped adopted
descendants prevent confirmed cleanup. The parent never signals a cached probe
group after its supervisor may have released that identity. If the supervisor
cannot complete cleanup, parent fallback stops only its still-owned supervisor
and reports cleanup unconfirmed. Leaders remain unreaped until their final
group signal to prevent signaling a recycled process-group identifier. Process
creation and kernel-uninterruptible tasks can exceed ordinary user-space timing guarantees;
the report must preserve `cleanup_unconfirmed` instead of promising successful
cleanup. The command does not kill unrelated application or Ray processes.

A success receipt cannot override an unsuccessful supervisor exit. With confirmed
cleanup, doctor marks the affected probe's primary section `unavailable` /
`probe_failed` and retains its other completed sections as dated observations.

The caller must run on its main thread with the default `SIGCHLD` disposition;
ignored/custom child-reaping handlers are unsupported and are not changed. Other
caller code must not independently reap the command's owned children. The
dedicated supervisor sets its own `SIGCHLD` default before spawning a probe.

## Exit codes and next steps

| Exit | Meaning | Operator action |
| --- | --- | --- |
| 0 | Reserved for a future complete report with every section observed | Still a dated observation, never maintenance authorization |
| 1 | At least one observed blocker, with confirmed owned cleanup | Inspect the named migration, protocol, lease, work or registered-cleanup condition using its existing supported interface |
| 2 | Incomplete/unsupported/budget-exhausted evidence or unconfirmed cleanup | Read section codes; correct the supported settings selection or investigate the bounded dependency separately. Do not infer health from omitted evidence |

For this first slice, exit 2 is the expected result for an otherwise clear
database. Avoid putting the command directly into a readiness probe that treats
only exit 0 as ready. Existing exact-worker readiness and protocol-status
commands retain their own narrower contracts.
