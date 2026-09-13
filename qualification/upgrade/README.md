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
2. The released lifecycle cancels the queued fixture and records the explicitly
   synthetic uncertain fixture as LOST without retry. Its synthetic lease is
   stopped. This is fixture preparation, **not deployed drain evidence**.
3. Back up the settled database and all fixture artifacts. SQLite uses its native
   backup API; PostgreSQL uses `pg_dump` with a separate socket-only database.
4. Restore into an independent database and artifact directory. A fresh released
   process compares every original model field and reads the actual filesystem
   input and result through released storage APIs.
5. The candidate migrates that restored database. It compares all historical
   fields, reads input/result artifacts, and proves historical result reads do
   not import the removed callable or let the historical Task enqueue work.
   Temporarily missing and corrupt result files are rejected by the storage API;
   the original bytes are restored and the complete artifact digest must match.
6. Enqueue one current task without starting a worker.
7. Open the migrated database with a fresh exact 0.4.0 process in database-enforced
   read-only mode. Retain migrations through `0026`, compare the original history,
   read the original artifacts, and fetch the candidate's queued task through the
   released backend. No reverse migration, old writer, or task execution runs.
8. Restore the original backup into a second independent database and read it
   with the old version. The candidate-only write is absent: restoring this
   backup after new writes would lose those writes. The rehearsal never replaces
   the database that received the candidate write.

The runtime ZIP and progress JSON are preservation fixtures. This stage compares
their retained bytes; it does not claim a supported workflow detail rendering,
encrypted RuntimeEnv delivery or execution from that archive. The installed
package bytes and the backup are checked again after all child processes exit.
The read-only code rollback proves this fixture's data readability after an enqueue;
it does not qualify restarting old managers or reading every result produced by
current execution. See the [0.5 upgrade procedure](../../docs/deployment/upgrading-0.5.md)
for the migration and rollback boundaries that still need deployed rehearsal.

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

### Opt-in native Core rehearsal

`native.yaml` adds a sequential real-manager rehearsal on the same exact released
and candidate wheels. It is not part of the routine hosted matrix. Use the source
export and baseline preparation above, select `qualification/upgrade/native.yaml`,
and build/run service `native` under an outer 1,200-second timeout. The native
service requires two CPUs, 8 GiB RAM without swap, 1,024 PIDs, 512 MiB shared
memory and 3 GiB temporary storage. The container has no external network.

For each of SQLite and PostgreSQL it observes queued and running blockers, runs
success/failure/retry work with the old manager, cancels queued work through the
old lifecycle, and requires terminal rows and inactive leases after the manager
stops. Ray itself is shut down before backup. The database and external input/result
artifacts are independently restored; old code reads that restore before candidate
migrations and current Core execution. Original task/attempt fields and artifacts
are checked through the installed versions' APIs.

This opt-in stage is under qualification. Its receipt keeps
`complete_upgrade_gate: false`: Core task preservation does not supply Jobs,
workflow rendering, encrypted RuntimeEnv, uncertain-outcome reconciliation,
manager-crash recovery or execution-retirement proof. Keep the database-only
recipe's independent rollback evidence alongside this additional runtime stage.

`jobs.yaml` selects service `jobs` for the same sequence using real Ray Jobs
instead of Core. The owned Ray dashboard listens only inside the no-network
container, and each Job receives the selected installed package and disposable
settings through its RuntimeEnv. Jobs run serially through the actual manager;
the receipt identifies the runner. No Ray Client connection is used.

Both runtime recipes finish with a fresh 0.4.0 reader after candidate execution.
Database-enforced read-only mode retains migrations through `0026` and checks
all eight old/current task records, attempts and input/result artifacts. This
extends the enqueue-only rollback fixture with actual completed work. It does
not restart old managers or qualify old execution of current request carriers.

Python executable selection follows each phase's installed environment through
`PATH`, and remote tasks assert the corresponding Ray version.
The Jobs recipe stores its RuntimeEnv as an encrypted snapshot and ships a tiny
Python module in a local ZIP. Successful remote work must import that module from
the delivered working directory. A temporary key stays separately in the owned
fixture; it is not included in the database/artifact backup or execution manifest.
Old and current readers decrypt retained snapshots after restore, while missing
or incorrect keys must fail before application invocation. The archive is covered
by the existing artifact backup/digest checks. This does not qualify key rotation
or replace the separate manager-crash recovery requirement.

The success task also executes a two-step Ray workflow with schema-v3 reporting
explicitly enabled. The snapshot includes its run, topology and node-detail rows,
including binary page content. Each version's own authorized progress readers and
admin graph projection must produce a complete two-node, one-edge successful graph
after restore. Old read-only SQLite code checks both old and candidate-created
graphs. On PostgreSQL, the released reader requests row locks; the final read-only
phase verifies its SQLSTATE 25006 refusal instead of claiming graph access. Stored
workflow rows still match, and earlier restored old/current graph reads pass.
This exercises the admin's graph data, not a browser-rendering test.
SQLite execution phases use WAL and immediate transactions for the concurrent
manager/progress writers; read-only phases retain ordinary read transactions.
PostgreSQL remains the multi-connection coordination evidence.

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

## Remaining execution-retirement inventory

The database fixture deliberately does not remove these current paths:

| Boundary | Current source to audit in the retirement change |
| --- | --- |
| Old Ray Job payload execution | `src/django_ray/runtime/entrypoint.py`, `_execute_legacy_payload` |
| Positional unversioned remote invocation | `src/django_ray/runtime/remote.py` |
| Non-strict Job failure/log fallback | `src/django_ray/management/commands/django_ray_worker.py`, legacy `get_logs` path; `src/django_ray/runner/ray_job.py` |
| Strict request-family discrimination | `src/django_ray/ray_job_protocol.py`, rq1/rq2 classification |
| Input artifact purgers and reference readers | `src/django_ray/input_storage.py`, `src/django_ray/ray_job_request_storage.py` and their cleanup commands |

The runtime follow-up must distinguish inert historical reads from execution
acceptance, retain malformed/retired strict-family rejection, and preserve
current rq2 receipts and manager recovery. Neither a current migration pass nor
a terminal fixture row establishes that an old producer can safely keep writing.
