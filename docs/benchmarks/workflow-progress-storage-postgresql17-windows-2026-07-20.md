# Workflow Progress Storage Benchmark

Raw JSON is authoritative. This summary uses medians across all modeled cases and warm samples.

Environment: `Windows-11-10.0.26200-SP0`; Python `3.12.12`; postgresql `170010`.

Collected: `2026-07-20T23:42:21.378055+00:00`; database deployment: `docker-desktop:postgres:17@sha256:a426e44bac0b759c95894d68e1a0ac03ecc20b619f498a91aae373bf06d8508d`; psycopg `3.3.4`.

Source revision: `unavailable`; benchmark implementation SHA-256: `da4616a613cf8643a0822bba451bbb8c1b8feb8d59a5048871dc9b8acff787fd`.

Matrix: nodes `[1000, 10000, 50000, 100000]`; change rates `[0.01, 0.1, 1.0]`; repetitions `5`; seed `20260720`.

Execution scope: configured Django database only; this command does not start Ray or access Kubernetes.

Python timings cover bounded in-process structures. Write bytes and database statement counts are analytical; the representative JSONB probe does not measure candidate-schema round trips or throughput.

| Candidate | Median total write bytes | Median estimated DB statements | Median warm serialize (ms) |
|---|---:|---:|---:|
| `current_full_row` | 24042048 | 1.0 | 0.5183 |
| `bounded_inline` | 16384 | 1.0 | 0.1953 |
| `chunked_database` | 10487808 | 112.5 | 0.3684 |
| `normalized` | 2320048 | 4.0 | 0.3664 |
| `append_delta` | 2230048 | 6.0 | 0.3690 |
| `external_chunk` | 10492672 | 2.0 | 0.3677 |
| `live_only` | 2048 | 1.0 | 0.0080 |

## Sparse short-profile focus (100000 nodes, 1% changed)

| Candidate | Modeled unit kind | Touched units | Estimated DB statements | Total write bytes |
|---|---|---:|---:|---:|
| `current_full_row` | `full_graph_page_equivalent` | 391 | 1 | 22502048 |
| `bounded_inline` | `none` | 0 | 1 | 6176 |
| `chunked_database` | `expected_random_page` | 361 | 363 | 16359680 |
| `normalized` | `none` | 0 | 4 | 227048 |
| `append_delta` | `delta_chunk` | 4 | 6 | 195048 |
| `external_chunk` | `expected_random_page` | 361 | 2 | 16395360 |
| `live_only` | `none` | 0 | 1 | 2048 |

Database evidence: **available** (postgresql).
Representative relation growth: `49152` bytes; WAL: `10544` bytes; table cleaned: `True`.

Timings are observations, not CI thresholds or service-level objectives.
