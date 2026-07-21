# Workflow Progress Bounded Read Benchmark

This benchmark exercises issue #127's package-level summary, topology-page,
node-detail, and single-node read services against the normalized PostgreSQL
storage introduced by issue #126. It measures both the first and next keyset
pages and records the complete PostgreSQL plans used for representative queries.

The [raw JSON](workflow-progress-reads-postgresql17-windows-2026-07-21.json)
is authoritative.

## Environment

- Host: Windows 11 `10.0.26200`, Python 3.12.12, Django 6.0.7, django-ray 0.3.1.
- Database: PostgreSQL 17.10 in a disposable Docker Desktop container; psycopg 3.3.4.
- Source base: `49ebeb384bd3e33a9817dfcc78e28c4c132d3664` with the uncommitted issue #127
  implementation under measurement.
- Read implementation SHA-256:
  `27fddcba296d2c1f71da3dbb17a0945c7f5ce20ad095449eed37df4dcc8f5f2e`.
- Benchmark implementation SHA-256:
  `f72bb4f2bfeee3a46a58f07f3b8e89fb380c436fc08a733a4affc9b497987229`.
- Collected at `2026-07-21T07:58:15.253202+00:00` (`2026-07-21` PDT).

## Results

Each case retained a chain with one fewer edge than nodes. The benchmark used a
100-item page, one warmup, and five measured repetitions. Times below are
wall-clock p50 / p95 milliseconds; synthetic dataset publication is excluded.

| Read | 1,000 nodes | 10,000 nodes | 25,000 nodes |
|---|---:|---:|---:|
| Summary | 1.439 / 1.738 | 1.422 / 1.544 | 2.552 / 3.686 |
| Topology nodes, first | 20.266 / 41.646 | 20.233 / 20.685 | 23.641 / 30.345 |
| Topology nodes, next | 22.184 / 28.095 | 22.258 / 24.247 | 22.612 / 27.420 |
| Topology edges, first | 20.582 / 25.617 | 16.846 / 18.092 | 19.444 / 30.027 |
| Topology edges, next | 16.626 / 16.818 | 17.189 / 17.461 | 29.475 / 31.836 |
| Node details, first | 20.498 / 25.670 | 24.053 / 30.378 | 30.516 / 32.888 |
| Node details, next | 20.389 / 20.539 | 33.928 / 35.329 | 41.156 / 59.480 |
| Node details, state-filtered | 20.191 / 21.215 | 24.118 / 24.497 | 32.876 / 33.640 |
| Single middle node | 8.539 / 9.717 | 8.630 / 9.441 | 9.138 / 9.804 |

Query count and response size did not grow with retained workflow size:

| Read shape | Captured SQL / SELECTs | Returned items | Encoded response across all sizes | Decoded item bytes |
|---|---:|---:|---:|---:|
| Summary | 1 / 1 | n/a | 1,392-1,404 B | n/a |
| Topology nodes | 5 / 3 | 100 | 15,182-15,183 B | 13,900 B |
| Topology edges | 5 / 3 | 100 | 5,810-5,811 B | 4,500 B |
| Node details | 5 / 3 | 100 | 23,552-23,553 B | 22,200 B |
| Node details, state-filtered | 5 / 3 | 100 | 23,577-23,578 B | 22,200 B |
| Single node | 4 / 2 | one record | 661-662 B | n/a |

The page reads deliberately use a two-phase bounded shape: resolve the authorized
run/publication fence, select at most the page-sized set of keys, then project only
those retained rows. The authorization query carries a SQL-length-guarded summary
projection, avoiding a separate current-run summary lookup. The two non-SELECT
statements captured for paged and single reads are transaction boundaries. Summary
reads require no transaction boundary.

Natural PostgreSQL plans varied appropriately with relation size. The unique
`ray_wf_node_key_uniq` index was selected for the single-node lookup at every size
and for both detail keyset queries at every size. PostgreSQL preferred sequential
scans for the small topology manifest-page relation at 1,000 and 10,000 nodes, then
selected `ray_wf_link_position_uniq` at 25,000 nodes. Each recorded topology plan
matched exactly one persisted `NODE` link; a zero-row plan is rejected by the
benchmark. The repository's PostgreSQL integration test additionally disables
sequential scans for its index eligibility assertions and proves the exact predicates
can use:

- `ray_wf_node_key_uniq` for detail keyset and single-node reads;
- `ray_wf_node_state_idx` for state-filtered detail reads; and
- `ray_wf_link_position_uniq` for topology page positions.

## Boundedness and snapshot evidence

The [PostgreSQL integration suite](../../tests/integration/test_postgresql_workflow_progress_reads.py)
runs the same representative 1,000-, 10,000-, and 25,000-node sizes in CI and
asserts package query counts rather than only recording them. It also verifies every
returned response against the V1 limits: at most 256 items, 512 KiB of encoded JSON,
1 MiB of decoded records, and a 2 KiB cursor.

Separate integration cases cover first and next pages, terminal historical reads by
optional `attempt_number` after the current task advances to attempt 2 and a new run,
an exact `EXPIRED` envelope after a detail publication retires a cursor, and a
concurrent reader/writer race. The race accepts either complete publication epoch,
but rejects a page whose epoch and rows come from different publications. This is the
consistency property that latency measurements alone cannot demonstrate.

The historical case also exercises lifecycle-owned success before a producer's final
detail flush. Its archived summary is authoritatively `SUCCEEDED`, while detail is
truthfully `TRUNCATED` and incomplete with `terminal_state_unreported`; the retained
last-observed `PENDING` rows remain readable. The marker relaxes only per-state
reconciliation for that exact lifecycle condition. Totals, publication epochs,
digests, size bounds, and the remaining truncation-reason partition stay strict.

## Finding

The measured package reads are bounded by requested page size rather than total
retained workflow size. From 1,000 through the V1 cap of 25,000 retained nodes, each
operation preserved the same SQL count and nearly identical response size. Local
p95 latency ranged from 1.544 ms for a summary to 59.480 ms for one 25,000-row
detail sample. Those timings are supporting evidence, not a production service-level
objective or a monotonic scale curve.

The three SELECTs used by page reads are intentional V1 correctness work, not a claim
that three round trips are irreducible. Database network latency remains unmodeled and
could dominate in a remote deployment. A future optimization should first preserve
authorization, exact publication fencing, response limits, and epoch consistency,
then use this artifact as the before-measurement.

## Method and reproduction

The opt-in [benchmark script](../../scripts/benchmark_workflow_progress_reads.py)
uses the package's public read services and actual normalized storage. Django's
`CaptureQueriesContext` records every statement around each call. Response bytes are
canonical compact UTF-8 JSON; decoded bytes sum the returned list records. Complete
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` results are retained in the raw artifact.
The topology plan uses the persisted uppercase `NODE` enum and must return at least
one row. Synthetic owning task rows are deleted before the script exits.

From PowerShell in the repository root:

```powershell
docker run --rm -d --name django-ray-issue127-postgres `
  -e POSTGRES_DB=django_ray `
  -e POSTGRES_USER=django_ray `
  -e POSTGRES_PASSWORD=django_ray `
  -p 55432:5432 postgres:17

$env:DJANGO_SETTINGS_MODULE = "tests.postgres_settings"
$env:DATABASE_NAME = "django_ray"
$env:DATABASE_USER = "django_ray"
$env:DATABASE_PASSWORD = "django_ray"
$env:DATABASE_HOST = "127.0.0.1"
$env:DATABASE_PORT = "55432"
$env:DJANGO_RAY_RUN_WORKFLOW_PROGRESS_READ_BENCHMARK = "1"

uv run --extra postgres python testproject/manage.py migrate --noinput
uv run --extra postgres python scripts/benchmark_workflow_progress_reads.py `
  --nodes 1000 10000 25000 `
  --repetitions 5 `
  --warmups 1 `
  --database-deployment docker-desktop:postgres:17 `
  --output docs/benchmarks/workflow-progress-reads-postgresql17-windows-2026-07-21.json

docker stop django-ray-issue127-postgres
```

This is a local benchmark against a disposable database, not a latency SLO. It does
not model production network delay, connection-pool contention, concurrent readers,
or application rendering time. PostgreSQL's captured per-query durations are
coarsely rounded, so wall-clock measurements are the primary latency values.
