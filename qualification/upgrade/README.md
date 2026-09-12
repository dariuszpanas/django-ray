# Coordinated Beta upgrade: database and artifact stage

This public Docker Compose recipe runs the first bounded part of
[#381](https://github.com/dariuszpanas/django-ray/issues/381): preserve released
database history and filesystem artifacts across a stopped-writer migration.
It uses the exact `v0.4.0` Git archive and independently installed released and
candidate wheels. Each version uses its own locked dependency environment.
No task manager, Ray process, Kubernetes client or shared database starts.

The recipe runs SQLite and PostgreSQL 17 serially. Every phase uses a fresh
Python process and checks its installed module path. The released version creates
eight synthetic task records representing success, failure, cancellation, retry,
an external result/input, a Ray Job, queued work and uncertain running work.
It also creates attempts, a worker lease and stored progress/RuntimeEnv fields.
These records exercise released persistence; they are not claims that those
outcomes were produced by real remote execution.

1. Read-only inventory observes two nonterminal tasks and an active lease. It
   verifies the database is unchanged; that state must block a real upgrade.
2. Back up that blocked snapshot and restore it into an independent database.
   The candidate applies migrations through `0034` there, then attempts `0035`. The
   activation must refuse the old queued/running work and leave every original
   field, the protocol 1 policy and the legacy admission token unchanged. The
   source database is never opened by the candidate during this negative phase;
   its blocked backup digest must still match after the entire rehearsal.
3. The released lifecycle cancels the queued fixture and records the explicitly
   synthetic uncertain fixture as LOST without retry. Its synthetic lease is
   stopped. This is fixture preparation, **not deployed drain evidence**.
4. Back up the settled database and all fixture artifacts. SQLite uses its native
   backup API; PostgreSQL uses `pg_dump` with a separate socket-only database.
5. Restore into an independent database and artifact directory. A fresh released
   process compares every original model field and reads the actual filesystem
   input and result through released storage APIs.
6. The candidate migrates that restored database. It compares all historical
   fields, reads input/result artifacts, and proves historical result reads do
   not import the removed callable or let the historical Task enqueue work.
   Temporarily missing and corrupt result files are rejected by the storage API;
   the original bytes are restored and the complete artifact digest must match.
7. Enqueue one current protocol 3 task and read its persisted immutable intent
   back against the package, configured declaration and normalized RuntimeEnv
   snapshot. No worker starts and the application callable must not execute.
8. Restore the settled backup into another independent database and read it
   with the old version. The candidate-only write is absent: restoring this
   backup after new writes would lose those writes. The rehearsal never replaces
   the database that received the candidate write.

The runtime ZIP and progress JSON are preservation fixtures. This stage compares
their retained bytes; it does not claim a supported workflow detail rendering,
encrypted RuntimeEnv delivery or execution from that archive. The installed
package bytes and the backup are checked again after all child processes exit.

## Public Linux invocation

Use an admitted Linux Docker Engine build/run environment, Docker Compose 2.17+
(named build contexts), Git, Python 3.12+ and GNU `timeout`. The existing
transaction qualification Dockerfile supplies the candidate environment and
private PostgreSQL tools; no separate database service or custom executor is used.
Build resources need their own admission. The execution limits do not bound image
construction. Do not run this on the host's Python environment.

From a clean committed candidate checkout with the released tag fetched:

```bash
set -euo pipefail
test -z "$(git status --porcelain)"
baseline_parent=$(mktemp -d)
export UPGRADE_BASELINE_SOURCE="$baseline_parent/released"
python -m qualification.upgrade.prepare "$UPGRADE_BASELINE_SOURCE"
sha256sum "$UPGRADE_BASELINE_SOURCE/source.tar" | cut -d ' ' -f 1 > "$baseline_parent/baseline-sha256.txt"
source_dir=$(mktemp -d)
git archive HEAD | tar -x -C "$source_dir"
git rev-parse HEAD HEAD^{tree} > "$baseline_parent/candidate-source.txt"
chmod 755 "$source_dir" "$baseline_parent" "$UPGRADE_BASELINE_SOURCE"
export UPGRADE_EVIDENCE_DIR=$(mktemp -d)
sudo chown 10001:10001 "$UPGRADE_EVIDENCE_DIR"
cd "$source_dir"
compose=(docker compose -p "upgrade-$RANDOM-$$" -f qualification/upgrade/data.yaml)
trap '"${compose[@]}" down --timeout 20 --remove-orphans' EXIT
"${compose[@]}" config --quiet
"${compose[@]}" build data
timeout --signal=INT --kill-after=20s 900s "${compose[@]}" run --no-deps --rm data
sudo chown "$(id -u):$(id -g)" "$UPGRADE_EVIDENCE_DIR"
python - "$baseline_parent/baseline-sha256.txt" "$UPGRADE_EVIDENCE_DIR/execution-manifest.json" <<'PY'
import json
import sys
from pathlib import Path

expected = Path(sys.argv[1]).read_text().strip()
manifest = json.loads(Path(sys.argv[2]).read_text())
assert manifest["baseline_archive_sha256"] == expected, "released archive differs from caller export"
PY
cat "$UPGRADE_EVIDENCE_DIR/execution-manifest.json"
```

The export command refuses a different `v0.4.0` commit. The caller's fresh Git
export is the baseline authority; its retained SHA256 must match the archive
inside the executing image. The archive's PAX commit header is an additional
metadata check, not independent proof of its contents. Runtime verification also
compares every installed package file with its source tree. Keep the host baseline
digest, candidate archive identity,
build output/image identity, console output, `execution-manifest.json` and
`junit.xml` together. The caller retains the temporary directories for inspection
and removes them when finished.

The path-selected [Upgrade Data Qualification workflow](../../.github/workflows/upgrade-qualification.yml)
runs this same command against Git archives on a disposable hosted Linux runner.
It records source and image identity and retains JUnit, receipts and bounded
console output. A passing hosted run proves this database stage only.

Execution has no network, one CPU, 1 GiB RAM without swap, 128 processes and a
512 MiB `/tmp`. Each child has a 60-second timeout with bounded output. The outer
900-second deadline covers the entire process. PostgreSQL has no TCP listener,
ten connections, 16 MiB shared buffers, 1 MiB work memory and a 32 MiB temporary
file limit. A clean server shutdown is required; timeout/forced shutdown, missing
phases, version/import drift, changed history or failed fixture cleanup fails the
stage. The caller's Compose trap removes the container after failure or cancellation.

## Remaining release acceptance

Every receipt always contains `complete_upgrade_gate: false` and the remaining
acceptance list. A successful database stage does not close #381. Before release:

- Prove real old-version producers/managers drain their work, and unresolved or
  uncertain remote outcomes remain visible blockers until explicitly reconciled.
- Inventory/retire old execution carriers/readers/purgers and prove unsupported
  execution is rejected before application invocation without relabeling or replay.
- Run current-version Ray Core/Jobs smoke and manager crash recovery with exact
  lease, attempt and generation fencing on the candidate Ray/Python tuple.
- Prove rendered historical workflow reads and encrypted RuntimeEnv recovery,
  including missing/corrupt artifact outcomes.
- Rehearse the migration-specific code-rollback boundary and publish the operator
  decision table. This recipe only proves restoration of a pre-write backup and
  explicitly demonstrates its data-loss boundary after candidate writes.

Cold Ray is `skip` for this database-only stage; the remaining runtime stages
require the applicable cold-Ray proof under the
[deployed gate matrix](../../docs/deployment/local-kuberay-gate.md). The full
Linux CI checkpoint and exact-source application/release evidence remain required.
Mixed-version running cohorts, live ObjectRef migration and two-cluster handoff
are outside the coordinated Beta commitment.

## Separate runtime preservation boundaries

The current runtime closes the old generic execution entries when protocol 3 is
active. This database fixture does not exercise their remote rejection or the
following preservation boundaries:

| Boundary | Current source to audit in the retirement change |
| --- | --- |
| Old Ray Job payload refusal before invocation | `src/django_ray/runtime/entrypoint.py`, `_execute_legacy_payload` |
| Positional unversioned remote refusal | `src/django_ray/runtime/remote.py` |
| Exact current Job completion and retained uncertainty | `src/django_ray/management/commands/django_ray_worker.py`, cohort completion/control paths; `src/django_ray/runner/ray_job.py` |
| Strict request-family discrimination | `src/django_ray/ray_job_protocol.py`, rq1/rq2 classification |
| Input artifact purgers and reference readers | `src/django_ray/input_storage.py`, `src/django_ray/ray_job_request_storage.py` and their cleanup commands |

The runtime follow-up must distinguish inert historical reads from execution
acceptance, retain malformed/retired strict-family rejection, and preserve
current rq2 receipts and manager recovery. Neither a current migration pass nor
a terminal fixture row establishes that an old producer can safely keep writing.
