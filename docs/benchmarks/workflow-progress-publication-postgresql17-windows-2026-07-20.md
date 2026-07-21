# Workflow Progress Production Publication Benchmark

This benchmark exercises issue #126's production preparation, immutable-topology
staging, normalized initial publication, and one-node sparse-publication paths. It
complements the earlier [storage-shape comparison](workflow-progress-storage-postgresql17-windows-2026-07-20.md),
which modeled alternative schemas but did not execute the selected ORM protocol.

The [raw JSON](workflow-progress-publication-postgresql17-windows-2026-07-20.json)
is authoritative.

## Environment

- Host: Windows 11 `10.0.26200`, Python 3.12.12, Django 6.0.7, django-ray 0.3.1.
- Database: PostgreSQL 17.10 in a disposable Docker Desktop container; psycopg 3.3.4.
- Source base: `74046713f9ae1164b71026bf38685e56c82f4c88` with the uncommitted issue #126
  implementation under measurement.
- Storage implementation SHA-256:
  `206d86ea170d13b1d72344099cd85f9bd8eb891ecb2dd53c110f723e5f03ed65`.
- Benchmark implementation SHA-256:
  `19e4e2cf1e31cf2baa1cb35d669b31b04cfd9a03b285bff0521c86b5ca6805fa`.
- Collected at `2026-07-21T05:20:46.575654+00:00` (`2026-07-20` PDT).

## Results

Each case prepared all observed topology/detail inputs and published the deterministic
retained subset, then changed one retained node from `PENDING` to `RUNNING` against
the immutable current topology. V1 retains at most 25,000 nodes, so the 50,000- and
100,000-node cases deliberately separate observed input size from durable row count.

| Observed nodes | Retained nodes | Initial prepare | Initial publication | Sparse prepare | Sparse publication | DB-reported sparse SQL |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1,000 | 216 ms | 498 ms | 0.153 ms | 22.861 ms | 0 ms |
| 10,000 | 10,000 | 3,250 ms | 8,087 ms | 0.220 ms | 29.123 ms | 31 ms |
| 25,000 | 25,000 | 9,613 ms | 14,038 ms | 0.159 ms | 26.128 ms | 0 ms |
| 50,000 | 25,000 | 8,198 ms | 13,781 ms | 0.163 ms | 25.965 ms | 31 ms |
| 100,000 | 25,000 | 11,887 ms | 13,969 ms | 0.147 ms | 53.976 ms | 32 ms |

| Observed nodes | Retained rows | Topology pages | Initial WAL | Initial relation delta | Sparse SQL | Sparse WAL | Sparse relation delta |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1,000 | 4 | 1,042,144 B | 1,024,000 B | 11 | 2,544 B | 0 B |
| 10,000 | 10,000 | 40 | 10,393,760 B | 7,536,640 B | 11 | 2,552 B | 0 B |
| 25,000 | 25,000 | 98 | 25,979,968 B | 18,857,984 B | 11 | 2,456 B | 0 B |
| 50,000 | 25,000 | 98 | 26,183,256 B | 18,857,984 B | 11 | 2,456 B | 0 B |
| 100,000 | 25,000 | 98 | 25,991,952 B | 18,817,024 B | 11 | 2,648 B | 0 B |

All sparse cases:

- changed exactly one normalized row and retained the exact expected row count;
- executed 11 captured SQL statements independent of retained workflow size;
- performed no `COUNT`/`SUM` aggregate scan;
- did not read immutable topology-page payloads; and
- produced only a few KiB of WAL and no measured relation growth.

## Finding

The normalized protocol now demonstrates the intended bounded sparse-write shape.
The same package-issued `PreparedWorkflowProgressTopology` object carries a
process-local capability, so a normal in-process sparse flush checks a fixed-size
signature instead of decoding and normalizing every topology page. This is reflected
in 0.147-0.220 ms sparse preparation, 11 captured SQL statements at every size, and
no topology-payload or aggregate reads.

The shortcut does not trust transferred or reconstructed evidence. A copied,
deserialized, or manually constructed prepared topology is fully verified once before
receiving a new process-local capability; a copy that changes immutable evidence is
rejected. The capability therefore removes repeated validation work without turning
caller-controlled data into trusted storage evidence.

Initial preparation still processes all observed input after durable retention reaches
the 25,000-node cap. The single cold samples took 9,613 ms for 25,000 observed nodes,
8,198 ms for 50,000, and 11,887 ms for 100,000; they are scale evidence rather than a
monotonic latency curve. The current preparer materializes complete observed identity
sets and sorts candidates before selecting the retained subset. Durable storage is
bounded, but producer preparation memory is not yet proven to be O(retained).
[Issue #132](https://github.com/dariuszpanas/django-ray/issues/132)
owns the streaming-preparation follow-up, including accurate observed counts and
bounded duplicate/reference validation.

The whole-run integrity audit is intentionally separate from sparse publication. It
streams the retained rows and recomputes digests and aggregates for periodic or
operator-triggered verification; its full-scan cost is neither on the sparse flush
path nor measured by this benchmark.

## Method and reproduction

The opt-in [benchmark script](../../scripts/benchmark_workflow_progress_publication.py)
uses the package's actual preparation and persistence functions. Preparation time is
reported separately from database publication. SQL count uses Django's
`CaptureQueriesContext`; WAL uses `pg_wal_lsn_diff`; relation bytes sum
`pg_total_relation_size` for the five workflow-progress storage tables. Synthetic task
rows are deleted before the script exits. PostgreSQL's per-query timings have coarse
rounding, so their sum can slightly exceed the independently measured wall time in a
single cold sample.

From PowerShell in the repository root:

```powershell
docker run --rm -d --name django-ray-issue126-postgres `
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
$env:DJANGO_RAY_RUN_WORKFLOW_PROGRESS_PUBLICATION_BENCHMARK = "1"

uv run --extra postgres python testproject/manage.py migrate --noinput
uv run --extra postgres python scripts/benchmark_workflow_progress_publication.py `
  --nodes 1000 10000 25000 50000 100000 `
  --database-deployment docker-desktop:postgres:17 `
  --output docs/benchmarks/workflow-progress-publication-postgresql17-windows-2026-07-20.json

docker stop django-ray-issue126-postgres
```

This is one cold local measurement per size, not a latency SLO. It does not include a
production network, and PostgreSQL relation deltas depend on page allocation and reuse.
