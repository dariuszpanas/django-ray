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

Five cases execute nine real current-protocol `rq2` Jobs against an owned SQLite WAL
database and filesystem request store:

- A three-task recovery-only control disables the new receipt method in the same installed
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

The report requires all five cases, nine distinct rq2 identities, installed imports
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

## Matched completion-window costs

Receipt schema 3 uses three successful tasks in both the recovery-only control
and the fast capacity-one case. Each task obtains a manager counter snapshot after
its callable has started and immediately before fixture release, then another
after terminal state is observed. Recovery-only tasks are released after a scan
has observed the held Job. At most 16 ordered, create-only snapshots are served
per manager; the matched cases each use six.

The before/after differences report SQL count/time, elapsed observation-window
time and HTTP requests whose monotonic timestamps fall within the same window.
Startup, the deliberate pre-release hold, and shutdown lie outside these windows.
The end snapshot is taken on a later manager loop turn, so the window may include
slot reuse or the next submission. Retain the raw boundaries and do not label the
elapsed window as exact persistence time or interpret it as production throughput.
Fixed-shape receipts reject manager replacement, cumulative counter resets,
overlapping windows and HTTP totals inconsistent with the proxy log.

These are same-binary, matched finite completion-window observations. They do not
provide a previous-release benchmark. The original
300-second driver and 420-second workload budgets remain unchanged; a timeout
fails qualification rather than yielding a partial passing comparison. The updated
nine-Job workload still requires clean-source Linux qualification before acceptance.

## Phase baseline

Each of the same nine tasks now retains thirteen monotonic timestamps and thirteen
derived intervals. All processes run on the same Linux host and clock. Creation,
claim and finish database timestamps remain a separate wall-clock measurement;
the existing capacity-one successor-claim observations use only that clock.

The driver observes entry/return from enqueue. The manager observes entry to
`process_task` after the claim transaction and entry/return of the real
`JobSubmissionClient.submit_job` call. The wrapper forwards unchanged arguments
and results; it never substitutes a submission response. Remote callable entry,
fixture release, the callable's final exit marker and its autocommitted completion
receipt separate startup and deliberate hold from released execution and receipt
writing. Submission acknowledgement and remote start can overlap; the contract
does not impose a false order between them.

The manager records entry to the actual success/failure persistence helper and an
`on_commit` callback after its enclosing transaction commits. Receipt-to-helper
time includes polling, validation and any earlier ownership-lock acquisition.
The actual receipt commit is bracketed by entry/return from its autocommitted
write. A manager may read the committed row before the producer records return;
receipt waiting is therefore reported as lower/upper bounds, including zero when
those observations overlap, rather than assuming a false timestamp order.
Helper-to-callback time includes result handling, transition and transaction
completion; it is not isolated SQL execution time. The parent waits for both a
terminal row and the callback receipt before recording terminal observation.
These observer boundaries include small instrumentation/scheduling costs and
must not be presented as exact internal Ray or database timings.

Four create-only observation files per task contain fixed timestamps and the
existing submission identity, never callable arguments, results, SQL, RuntimeEnv
or HTTP bodies. Duplicate, missing, malformed, misordered or inconsistent evidence
fails the fixture. Phase durations are recomputed from raw timestamps and checked
against the existing completion observations. No extra task, retry, resource or
time budget is introduced. Native source-matched proof of the phase instrumented
candidate is still required before issue #467 can accept this baseline.
