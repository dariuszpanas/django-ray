# Bounded Workflow Topology Preparation: WSL2 Linux Evidence

This report records the required production-topology evidence matrix for
[ADR-0005](../design/adr-0005-bounded-workflow-preparation.md) and issue #141. The
authoritative measurements are in the adjacent
[JSON artifact](workflow-progress-topology-sqlite-wsl2-linux-2026-07-21.json).

- **Collected:** 2026-07-22T01:30:09Z (2026-07-21 local)
- **Source revision:** `fd08ab21373c0bf1e0586e54ec0a564e3d4ce4d5`
- **Source tree:** `a886ace2a976ddb68f6e4c5535b447dd80e9c84f`
- **Implementation digest:**
  `93ae4c2165b3fa1a7ffcf9ae5a80ce6dd0b794f4284660560d93239e0a6c42e4`
- **JSON SHA-256:**
  `7314c9aac63bcbce5635e31c5e089760dabf06f9d6a8abc8a658bdd6313f5661`
- **JSON size:** 32,802 bytes
- **Result:** all six required cases and the forced-termination cleanup control passed

## Scope

The benchmark exercises the issue-#141 production topology adapter through final
legacy detachment. Every case consumes one-shot topology generators in a fresh child
process, prepares canonical V1 pages and manifests through the private SQLite spill
workspace, materializes the compatibility `observed_node_ids` value, and removes the
workspace before the parent accepts the result.

Report schema v2 records a bounded-phase checkpoint before legacy detachment and an
end-to-end high-water mark after detachment. The run did not start Ray, write a Django
database row, contact Kubernetes, activate a writer, or enable schema v3.

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
| RSS scope | Fresh child-process lifetime high-water |

The branch was exported as a Git bundle and cloned into native WSL `/tmp`; the run did
not depend on the unavailable `/mnt/d` mount. `PYTHONPATH` pointed at that clone's
`src`, and both source snapshots recorded the same clean revision and implementation
digest.

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
| Observed node / edge ceilings | 1,000,000 / 4,000,000 |
| Journal / synchronous | `off` / `0` |
| Locking / foreign keys | `exclusive` / enabled |
| Trusted schema | disabled |

All cases retained the same five direct scan or primary-key-search plans. No plan used
a temporary B-tree or implicit materialization.

## Results

Memory and spill values use MiB. The bounded and end-to-end columns are distinct
schema-v2 measurements even where this run recorded the same process high-water.

| Observed nodes | Profile | Observed edges | Retained nodes / edges | Wall s | CPU s | Spill | Bounded Python | End-to-end Python | Bounded RSS | End-to-end RSS | Legacy IDs | Truncation |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 25,000 | sparse | 24,999 | 25,000 / 24,999 | 53.92 | 54.36 | 5.61 | 55.67 | 55.67 | 184.60 | 184.60 | 25,000 | none |
| 25,000 | high-edge | 200,000 | 25,000 / 100,000 | 113.99 | 115.01 | 12.13 | 101.22 | 101.22 | 303.23 | 303.23 | 25,000 | edge cap |
| 100,000 | sparse | 99,999 | 25,000 / 24,999 | 73.64 | 74.37 | 22.38 | 55.67 | 55.67 | 186.94 | 186.94 | 100,000 | node cap |
| 100,000 | high-edge | 800,000 | 25,000 / 100,000 | 175.19 | 177.17 | 48.45 | 101.21 | 101.21 | 303.44 | 303.44 | 100,000 | edge, node caps |
| 250,000 | sparse | 249,999 | 25,000 / 24,999 | 111.78 | 113.33 | 55.94 | 55.67 | 55.67 | 186.47 | 186.47 | 250,000 | node cap |
| 250,000 | high-edge | 2,000,000 | 25,000 / 100,000 | 297.55 | 301.21 | 121.11 | 101.21 | 101.21 | 303.48 | 303.48 | 250,000 | edge, node caps |

## Interpretation

After retained output reached the V1 caps, bounded-phase memory stayed effectively
flat while observed input grew:

- sparse tracemalloc remained 55.67 MiB and RSS ranged from 184.60 to 186.94 MiB;
- high-edge tracemalloc ranged from 101.21 to 101.22 MiB and RSS ranged from 303.23 to
  303.48 MiB; and
- retained output stayed at 25,000 nodes, with either 24,999 sparse edges or the
  100,000-edge cap.

External spill grew with observed input, from 5.61 to 55.94 MiB for sparse cases and
from 12.13 to 121.11 MiB for high-edge cases. That is the selected trade: exact
uniqueness and endpoint-reference state scales on bounded local disk rather than in
complete Python node and edge collections.

The end-to-end high-water equaled the bounded-phase high-water in this run. That does
not erase the compatibility boundary: the artifact records 25,000, 100,000, and
250,000 legacy observed IDs, and issue #142 still owns removing or confining that
O(observed) final value.

## Source, validation, and cleanup controls

- The source snapshots before and after the matrix were clean and identical at
  `fd08ab21373c0bf1e0586e54ec0a564e3d4ce4d5`.
- The same tree passed `uv run make ci`, 267 focused preparation tests with 5 expected
  Windows skips, 214 Compiled Graph-selected tests with 1 expected Windows skip, and
  500 seeded randomized parity cases before the Linux run.
- Every normal case reported worker-context and parent-watchdog cleanup as `removed`;
  no scenario root or workspace remained.
- The forced-termination child reached an open workspace, was killed with return code
  `-9`, created no durable candidate, and had its scenario root removed by the parent.
- The benchmark's schema-v2 validator accepted the retained artifact again after the
  run, and the configured workspace parent was empty.

## Decision and limitations

This matrix and the focused canonical, failure, cancellation, ownership, cleanup, and
capability tests satisfy issue #141's production-topology evidence gate. They show that
the spill-backed path removes complete Python node and edge validation collections
without changing canonical V1 output.

This is one run on one WSL2 host and ext4 filesystem, without concurrent preparers or
repetitions, and is a memory-contract decision record rather than a latency SLO. It
does not satisfy issue #142's composite topology/detail lifetime, issue #79's live
wire/mailbox/aggregate admission limits, or any schema-v3 activation gate.

## Reproduction

The retained run used the following command from the clean native WSL clone:

```bash
PYTHONPATH=/tmp/django-ray-issue141-20260721181601/src \
/tmp/django-ray-issue140-venv/bin/python \
  scripts/benchmark_workflow_progress_preparation.py \
  --implementation production-topology \
  --required-scale \
  --timeout-seconds 7200 \
  --workspace-parent \
    /tmp/django-ray-issue141-20260721181601-benchmark \
  --output \
    /tmp/django-ray-issue141-20260721181601-production-topology.json
```
