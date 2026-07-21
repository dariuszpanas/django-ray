# Bounded Workflow Progress Preparation: WSL2 Linux Evidence

This report records the required preparation-only evidence matrix for
[ADR-0005](../design/adr-0005-bounded-workflow-preparation.md) and issue #140. The
authoritative measurements are in the adjacent
[JSON artifact](workflow-progress-preparation-sqlite-wsl2-linux-2026-07-21.json).

- **Collected:** 2026-07-21T13:24:05Z
- **Source revision:** `c42fb22634712e52d8aee74c86a62fea459da5e8`
- **Implementation digest:**
  `2d32b2b2175706aaecd1a3348134f0e27ab61f4f489a8820c57c2fc3d8de3ce9`
- **JSON SHA-256:**
  `8ea7b9e22182e9bbaf9962eedaea9dc4872893a34538f55adcbe9da23d3edf73`
- **JSON size:** 27,200 bytes
- **Result:** all six required cases and the forced-termination cleanup control passed

## Scope

The benchmark exercises the non-production SQLite spill prototype directly. Every case
runs in a fresh child process, consumes topology and initial-detail generators once,
prepares canonical V1 output, and removes its private workspace before the parent
accepts the result.

The run did not start Ray, access a Django database, contact Kubernetes, change a
runtime writer, or activate schema v3. It is evidence for the preparation contract, not
for end-to-end workflow latency or production admission capacity.

## Environment

| Field | Value |
| --- | --- |
| Platform | `Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.39` |
| Python | CPython 3.12.3 |
| Django | 6.0.7 |
| SQLite | 3.45.1 |
| Workspace filesystem | ext4, 4 KiB allocation blocks |
| Filesystem identity | `c6829391fef5d4dfb0f645124791a34b509c6c559bfb7029adaabd89e8684ab6` |
| RSS method | `resource.getrusage(RUSAGE_SELF).ru_maxrss` |
| RSS scope | Fresh child-process lifetime high-water; 10 ms sampler also recorded |

The checkout was created by Windows Git. The outer WSL command supplied
`core.autocrlf=input` and `core.filemode=false` through invocation-scoped `GIT_CONFIG_*`
variables so WSL evaluated the same index and working-tree content as Windows Git. This
did not rewrite the checkout or change repository configuration. Both source snapshots
recorded `dirty=false`, the same revision, and the same implementation digest.

## Selected resource profile

| Setting | Effective value |
| --- | ---: |
| SQLite page size | 4,096 bytes |
| SQLite cache | 8 MiB (`cache_size=-8192`) |
| Memory mapping | disabled (`mmap_size=0`) |
| Temporary storage | memory (`temp_store=2`) |
| Maximum database pages | 261,120 |
| Total workspace ceiling | 1 GiB, including a 4 MiB control reserve |
| Transaction batch | 256 items or 4 MiB decoded item bytes |
| Observed node / edge / detail ceilings | 1,000,000 / 4,000,000 / 1,000,000 |
| Journal / synchronous | `off` / `0` |
| Locking / foreign keys | `exclusive` / enabled |
| Trusted schema | disabled |

All cases reported the same effective settings and V1 output limits. The retained query
plans were five direct scans or primary-key searches; none used a temporary B-tree or
materialization.

## Results

Memory and spill values use MiB. RSS delta subtracts the current RSS captured at the
start of each fresh child process; the JSON also retains the process high-water baseline
and sampler output.

| Observed nodes | Profile | Observed edges | Retained nodes / edges / detail | Wall s | CPU s | RSS peak | RSS delta | Python peak | Spill peak | Pages | Truncation reasons |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 25,000 | sparse | 24,999 | 25,000 / 24,999 / 25,000 | 67.39 | 71.46 | 202.0 | 154.3 | 64.8 | 14.5 | 196 | none |
| 25,000 | high-edge | 200,000 | 25,000 / 100,000 / 25,000 | 127.82 | 135.42 | 300.9 | 253.4 | 101.2 | 21.0 | 489 | `edge_count_limit` |
| 100,000 | sparse | 99,999 | 25,000 / 24,999 / 25,000 | 89.19 | 94.96 | 204.6 | 156.6 | 64.8 | 33.5 | 196 | `node_count_limit` |
| 100,000 | high-edge | 800,000 | 25,000 / 100,000 / 25,000 | 206.03 | 219.03 | 301.3 | 253.2 | 101.2 | 59.5 | 489 | `edge_count_limit`, `node_count_limit` |
| 250,000 | sparse | 249,999 | 25,000 / 24,999 / 25,000 | 153.15 | 163.46 | 204.1 | 156.6 | 64.8 | 71.4 | 196 | `node_count_limit` |
| 250,000 | high-edge | 2,000,000 | 25,000 / 100,000 / 25,000 | 355.16 | 378.49 | 301.0 | 252.9 | 101.2 | 136.6 | 489 | `edge_count_limit`, `node_count_limit` |

## Interpretation

Once retained output reached the V1 caps, memory stayed effectively flat while observed
input grew:

- sparse RSS delta was 154.3, 156.6, and 156.6 MiB, while tracemalloc peak stayed at
  64.8 MiB;
- high-edge RSS delta was 253.4, 253.2, and 252.9 MiB, while tracemalloc peak stayed at
  101.2 MiB; and
- retained output stayed at 25,000 nodes and detail rows, with either 24,999 sparse
  edges or the 100,000-edge V1 cap.

The external spill grew with observed input, from 14.5 to 71.4 MiB for sparse cases and
from 21.0 to 136.6 MiB for high-edge cases. That is the intended trade: exact duplicate
and reference state scales on bounded local disk rather than as Python identity
collections. Wall time also grew with observed input, so these measurements establish a
memory contract and decision baseline, not a latency SLO.

## Source and cleanup controls

- Source snapshots before and after the matrix were identical and clean at revision
  `c42fb22634712e52d8aee74c86a62fea459da5e8`.
- Every normal case reported both worker-context and parent-watchdog cleanup as
  `removed`; no scenario root remained.
- The forced-termination child published its parent-owned readiness nonce while the
  SQLite workspace was open, was killed with return code `-9`, and produced no durable
  candidate.
- The parent removed the forced child's workspace and scenario root, and the configured
  benchmark parent was empty in an immediate post-run filesystem check.

## Decision and limitations

Together with the 76 focused canonical, failure, cancellation, and cleanup tests that
passed on both Windows and Linux/WSL2 at the measured revision, this matrix satisfies
ADR-0005's issue-#140 evidence gate for accepting the exact spill-backed preparation
contract. It does not deliver or authorize production writer activation. Issue #141
still owns topology integration, issue #142 owns composite topology/detail lifetime and
final production memory proof, and issue #79 owns live wire, mailbox, producer,
concurrency, and node-local spill admission bounds.

This is one run on one WSL2 host and filesystem, without repetitions or concurrent
preparers. Process RSS is a child-lifetime high-water measurement and includes Python
startup; the recorded baseline makes that scope explicit. Production integration must
repeat fault and capacity evidence for its configured spill root, janitor, aggregate
quota, and concurrency policy.

## Reproduction

The native-Linux form is documented in [Performance](../performance.md). This WSL run
used the following checkout-local command; the `GIT_CONFIG_*` values are only needed
when a Windows-created checkout otherwise appears dirty to WSL Git.

```bash
cd /mnt/d/Repos/django-ray
mkdir -p /tmp/django-ray-issue140-benchmark
GIT_CONFIG_COUNT=2 \
GIT_CONFIG_KEY_0=core.autocrlf \
GIT_CONFIG_VALUE_0=input \
GIT_CONFIG_KEY_1=core.filemode \
GIT_CONFIG_VALUE_1=false \
PYTHONPATH=/mnt/d/Repos/django-ray/src:/mnt/d/Repos/django-ray \
/tmp/django-ray-issue140-venv/bin/python \
  scripts/benchmark_workflow_progress_preparation.py \
  --required-scale \
  --timeout-seconds 7200 \
  --workspace-parent /tmp/django-ray-issue140-benchmark \
  --output \
  docs/benchmarks/workflow-progress-preparation-sqlite-wsl2-linux-2026-07-21.json
```
