# Ray Job completion latency qualification

Register `qualification/latency/jobs.yaml` from the exact candidate archive and run
`python -m qualification.latency.scenario` on the existing capacity-one Linux
`external-evidence-v1` Kubernetes Job lane. Use the reviewed offline image procedure
in [the Kubernetes qualification](../kubernetes/README.md). Retain the complete
archive at `/workspace` and bind its candidate to the verified image digest.

The scenario installs that candidate wheel offline, checks its package tree against
the archive, and starts one fresh local Ray runtime with its Jobs dashboard. It uses
two logical Ray CPUs, no GPUs and a 128-MiB object store. The existing admitted target
limits remain 3.5 CPUs, 7.5 GiB RAM and 7 GiB scratch; do not resize the pool. Runtime
allocation and OS limits are different measurements. The driver has a 300-second
deadline after the 60-second offline install, within the 420-second workload budget;
external cleanup has a separate 180-second ceiling. Managers run serially at
concurrency one. No local Windows Ray or full CI is part of this workload.

Five cases execute seven real current-protocol `rq2` Jobs against an owned SQLite WAL
database and filesystem request store:

- A recovery-only control disables the new receipt method in the same installed
  binary. It retains the actual 30-second reconciliation interval and must observe
  at least ten seconds between completion commit and terminal observation. The
  held task is released after a scan has observed its real Job, excluding variable
  interpreter startup from that comparison. It is
  not a previous-release binary or a production baseline.
- Three queued successful tasks prove capacity-one admission and reclamation. Each
  successor must be claimed within five seconds after its predecessor finishes.
- A deterministic failure must terminalize on its first attempt without replay.
- A loopback HTTP proxy returns a verified 503 after submission. The real Job must
  finish through its database receipt without another manager Jobs API request.
- SIGTERM stops the original manager through its normal handoff and lease-release
  path. A fresh manager process adopts the same running Job and finishes it without
  another submission, attempt or generation.

The ordinary cases retain the production 250-ms fast receipt interval, 30-second
recovery interval, and an adaptive claim backoff up to five seconds. Every ordinary
receipt must become terminal within five seconds, including the replacement case.
These tolerances detect a return to the recovery clock; they do not promise a
250-ms end-to-end service latency. The recovery-only control may still wake on the
fast clock, so its CPU/SQL totals are not an exact old-version cost comparison.

Each held task reports its actual installed-package import and waits for an explicit
fixture release. A Django SQL observer timestamps the real autocommitted completion
write after it returns; it never replaces the writer or fabricates completion data.
The parent retains task-release, Job-start, commit and terminal-observation monotonic
times. Database creation/claim/finish timestamps separately measure successor claim
delay. Reports include each manager's total and fast-path SQL counts, SQL time,
reconciliation/completion counts, peak tracked concurrency and bounded Jobs HTTP
requests. SQL text, parameters and request bodies are not retained. These small
samples measure this fixture and include instrumentation overhead.

The report requires all five cases, seven distinct rq2 identities, installed imports
in the driver/managers/Jobs, first-attempt outcomes, consistent timing arithmetic,
one submission per task, successful manager shutdown, inactive leases, terminal Jobs
and removal of the owned fixture. Missing, partial, skipped, oversized or failed
observations produce failing JUnit. Evidence is create-only: `junit.xml`, the external
`command.log`, and `execution-manifest.json`, each at most 1 MiB. The installed tree is
checked again after execution. The external runner separately proves namespace/Pod
removal, allocation release and preservation of the existing platform.

This is the affected Ray Job manager/polling proof. The fresh isolated Ray runtime is
the explicit cold-Ray decision; the workload does not restart shared KubeRay Pods.
It does not prove PostgreSQL multi-manager locking, abrupt manager-crash recovery,
remote-node RuntimeEnv delivery, deployment replica convergence, unsupported-payload
retirement or a coordinated release upgrade. Those boundaries retain their own
tests and gates. The source change leaves submission, encoding and orphan-adoption
rules intact; the existing failure-fence unit tests remain required alongside this
runtime measurement and current-head hosted Linux CI.
