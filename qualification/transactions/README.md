# Transactional enqueue qualification

`receipts.yaml` is a public Docker Compose file. It runs
`python -m qualification.transactions.scenario` with no private test service,
Kubernetes cluster or Ray runtime. Build `qualification/transactions/Dockerfile`
from the same committed archive mounted into the workload. Its Debian 13 image includes
PostgreSQL 17.11 and the locked Python development and PostgreSQL dependencies.
Installation disables package-managed cluster creation and service startup.
The image and Compose service use operating-system user/group 10001. PostgreSQL
requires that account even when its SQL superuser is supplied with `initdb -U`.

## Public Linux invocation

Use Linux with Docker Engine, Docker Compose v2, Git and GNU `timeout`. Allocate
build capacity separately; the Compose CPU/memory limits apply to execution,
not image construction. The checked-in `Transaction Qualification` GitHub Actions
workflow provides an isolated Linux build and run for affected pull requests or
manual dispatch. Its logs and uploaded receipts are public validation evidence.

From a clean checkout, export the exact commit into a temporary directory:

```bash
set -euo pipefail
test -z "$(git status --porcelain)"
source_dir=$(mktemp -d)
git archive HEAD | tar -x -C "$source_dir"
chmod 755 "$source_dir"
export TRANSACTION_EVIDENCE_DIR=$(mktemp -d)
sudo chown 10001:10001 "$TRANSACTION_EVIDENCE_DIR"
cd "$source_dir"
compose=(docker compose -p "transaction-$RANDOM-$$" -f qualification/transactions/receipts.yaml)
trap '"${compose[@]}" down --timeout 20 --remove-orphans' EXIT
"${compose[@]}" config --quiet
"${compose[@]}" build receipts
timeout --signal=INT --kill-after=20s 300s "${compose[@]}" run --rm receipts
sudo cat "$TRANSACTION_EVIDENCE_DIR/execution-manifest.json"
```

The runner prints progress in the foreground, returns nonzero on any failed case
or cleanup, and leaves `junit.xml` and `execution-manifest.json` in the evidence
directory. Retain console output separately from that initially empty directory.
Stop the foreground command to cancel; the exit trap removes its Compose
resources. The caller retains the two temporary directories for inspection and
removes them when finished. This command touches no shared database or cluster.

The workload installs the sole candidate wheel offline, checks it against every
package source file, then runs exactly the 16 PostgreSQL cases in
`tests/integration/test_transactional_enqueue.py`. A fresh process verifies the
installed import location, PostgreSQL major version and disabled TCP listener.
The existing tests prove commit/rollback, nested savepoints, input and receipt
failures, backend-alias interpretation, route refusal, pinned registry writes,
and the external filesystem object that survives database rollback. Two cases
retain independent writer/observer process IDs and the actual before/after row
counts for the execution, application receipt and immutable cohort intent, in
that order. The current protocol-3 producer must expose none before the outer
transaction finishes, all three after commit, and none after rollback. The
installed package selects its exact execution epoch; the manifest retains that
epoch. Missing cases, skips, failed teardown, incomplete counts or a missing
committed intent fail. The same validator retains historical protocol-1 receipt
semantics, which require a zero intent count, without selecting that epoch for
the current installed package.

The server uses a fresh `initdb` directory and private mode-0700 Unix socket
under the target's `/tmp`; it never addresses a shared database. Limits are ten
connections, 16 MiB shared buffers, 1 MiB work memory, 32 MiB temporary files
per process and 64 MiB nominal WAL maximum. The 512 MiB `/tmp` mount, one CPU,
1 GiB RAM without swap and 128-process limit bound the container. The probe
has a 180-second deadline inside the 300-second workload. It owns the server
as a direct process-group child and requires a clean fast shutdown within
15 seconds. Forced termination is failure. The outer wrapper verifies fixture
removal and unchanged installed package bytes. Compose removes the test container;
the caller owns evidence retention and any surrounding infrastructure.

The bounded evidence is JUnit (all 16 cases), foreground execution log and the
manifest containing source/wheel/dependency identities and observations. No
database passwords or Kubernetes API tokens enter the fixture.

This is the affected gate for default-database enqueue binding. Cold Ray is
explicitly `skip`: the workload observes enqueue persistence without cluster
submission, a worker or remote bootstrap. No Ray runtime starts, and this gate
does not qualify the broader execution-protocol activation. It does not claim
remote execution, rollback of external objects, cross-database atomicity, or a
complete application qualification. Passing exact-head hosted Linux CI is the
full-suite checkpoint. The focused container workflow proves this database-only
boundary; a KubeRay run adds no coverage because the scenario never submits to Ray.
