# PostgreSQL maintenance query-plan diagnostic

The manual **Polling Benchmark** workflow has a fixed `maintenance` mode for issue
#718. Default `claim` mode retains its command, scaling series and installation.
The new mode installs only the tracked frozen lock with the PostgreSQL extra.
It runs no worker turn, Ray runtime, task function, lease authority or lifecycle
transition. This diagnostic does not close #718.

## Admission and execution

The owner must review the exact source and admit this disposable job. There are
no SQL, script, dataset or baseline-ref inputs. The new job requires the owner
actor/repository IDs, exact clean source, Linux, at least two CPUs, 6 GB host
memory and 4 GiB available disk. It verifies exact-source Linux CI before
frozen dependency installation, PostgreSQL allocation or collection. Its
checkpoint wait is at most 90 seconds; dispatch only after the intended CI passed.

Only this job's PostgreSQL 17 container and fixed `maintenance_plans`
database/user are supported. Source and capacity preflight runs again after
frozen installation, immediately before allocation. The job uses the runner's
explicit local Docker Unix socket. It clears Docker
context/host/TLS overrides and publishes the database only at `127.0.0.1:5432`.
It requests one CPU, 1 GiB combined memory/swap and 128 PIDs, with no host or
named-volume mount.

Creation and startup are separate steps. The full validated container ID must
be published before startup; startup verifies that exact ID. Readiness permits
ten observations five seconds apart, inside a two-minute step. Docker's separate
health check has a five-second interval/timeout and ten retries. These are
requested limits and refusal paths, not observed Docker or PostgreSQL behavior.
The collector checks the database name, user, owner and empty user-table catalog
**before it calls migrations**. Existing tables, views or sequences refuse entry.
The database name alone is not admission; never use an existing shared database.

The job deadline is 15 minutes. Collection has a 360-second TERM supervisor plus
10 seconds to forced termination, 300 CPU seconds, a 2 GiB virtual-address limit
and a file-size ceiling. These are requested limits, not observed RSS or reserved
capacity. Migrations have 15-second statements and two-second locks. Fixture
transactions have two-second statements, 500 ms locks and 4 MiB work memory.
Runner and container are disposable; shared Docker/Kubernetes resources are unused.

After review, CI and resource admission, select `maintenance` in the registered
workflow's manual dispatch interface at the reviewed branch. Its command is fixed:

```sh
timeout --signal=TERM --kill-after=10s 360s \
  uv run --no-sync --python 3.12 python -m qualification.maintenance_plans \
  --output "$RUNNER_TEMP/maintenance-plans/report.json"
```

The implementation's presence is not a passing run. This mode must be exercised
and its exact-source/cleanup evidence reviewed before acceptance. It is an
investigation, not a latency threshold or deployed qualification gate.

## Exact query boundary

Ordinary worker scan preludes can lock lease/policy authority or reconcile active
work. Instead, a strict AST slice of the trusted worker source retains each real
queryset assignment and optional queue refinement. It admits only read-only ORM
filters, the known pure defer helper and literal field tuple. Unknown structure
refuses. The full worker module is neither imported nor run. Reports retain each
original method's normalized AST hash.

Actual `MaintenanceScan.page` runs under `execute_wrapper`. It captures the
original high-water, first-page and continuation SELECT statements and parameters. A first
page must have more than 32 eligible rows; only `scan.advance` changes its cursor.
A short/new cycle is refused, never called continuation. No fixture-prefix filter
changes the product predicate. All/single/multiple-queue cases cover orphan,
running and cancelling scans. Orphan cases model both an empty active page and
two successfully loaded active-page IDs, within the production 32-ID bound.
No handles are adopted. Running keeps completion-NULL; orphan keeps its escaped
Ray Jobs prefix and exclusion. The private default database alias is required.

PostgreSQL's typed integer parameters remain intact for both the captured SELECT
and its EXPLAIN. JSON records their integer values. The adapter allowance is limited
to the three integer classes Django's PostgreSQL backend emits. It does not extend
to numeric-looking strings, arbitrary integer subclasses or other vendors.

## Fixed fixture and evidence

Each profile has at most 8,192 synthetic executions in batches of 256: dense,
sparse and an ineligible high-PK tail. Rows include RUNNING/CANCELLING/terminal
states, several queues and completed RUNNING rows. No user data is accepted.
Supported protocol bounds are recorded; other protocol distributions are not
modeled in this initial investigation.

The fixed matrix captures at most 108 original queries/plans. `EXPLAIN (ANALYZE,
BUFFERS, TIMING OFF, FORMAT JSON)` executes those reads. Bounded original JSON and
a numeric summary retain row estimates, actual rows/loops, rows removed and buffer
work. Evidence also records exact parameterized synthetic SQL, source/tree,
method hashes, installed versions, migrations, indexes and profile. Each plan is
limited to 64 nodes, depth 16 and 64 KiB; the full report is at most 1 MiB.
Failures expose fixed categories without exception text or credentials. The
run/attempt-specific JSON artifact is retained for 14 days.

Profiles roll back their owned rows. Application-data emptiness is checked before
seeding and after successful rollback; cleanup failure cannot yield success. No
unknown rows are deleted. PostgreSQL sequences/statistics may change on rollback;
this is not bit-for-bit preservation. Forced termination relies on connection
rollback and disposal of the owned container, and may leave no report.

Cleanup uses only the retained full container ID, confirms its exact identity,
and requests container and anonymous-volume removal with `--volumes`. It requires
both a failed inspect and a successful local-daemon listing with no matching ID
before writing `owned_postgres_removed=true`. This confirms container absence;
there is no independent volume-absence check. Daemon unavailability cannot confirm removal.
Without a published ID, cleanup is skipped and no successful removal is claimed.
An abrupt cancellation between creation and ID publication, or loss of the
runner, can prevent explicit cleanup; the disposable hosted runner remains the
final boundary. No workload starts before ID publication. A missing report or
cleanup-only artifact does not qualify the diagnostic.

The hosted PostgreSQL lane exercises a small separately created database through
fresh admission, actual migrations, plan capture, timeout settings and rollback
or failure refusal. Resource-free tests cover source/query guards and evidence
budgets on contributor hosts. No native workload runs on Windows.

These synthetic plans establish no production database cost, worker/RSS bound,
throughput, savings, index need, migration recommendation, lifecycle/payload
acceptance or release certification. Inspect original plans and distributions
before proposing indexes.
