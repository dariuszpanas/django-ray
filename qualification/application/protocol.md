# Current-cohort unsupported execution observation

`run_protocol` supplies one finite catalogue phase in each cold generation.
It runs after the ordinary first/existing/rq2 phases, in the existing assertion
container, PostgreSQL database and two-node Ray cluster. Its sentinel uses the
existing `/rq2` shared mount. It adds no Pod, CPU or memory reservation.

The phase requires the ordinary current-writer policy: protocol 1, open legacy
admission with its token, no legacy manager lease, no other unsupported
nonterminal row, and exactly one fresh current `default` manager. It does not
activate protocol 2 or exercise the private target execution transport.
Inactive legacy leases are also refused because closing admission would detach
their token links; the fixture does not rewrite unrelated historical leases.

The helper saves one exact execution identity and full-field fingerprints before
its first database write. It stages the owned protocol-2 row as terminal, closes
admission through the production expected-revision API, and queues that row.
A later heartbeat from the same compatible manager, unchanged concrete row
fields and empty attempt history establish the observation interval. The real
task-status and execution-list APIs must report queued protocol 2 without a
compatible worker; the fixed `other`/`QUEUED` metrics bucket must rise from zero
to one; the production protocol-status report must contain the exact group.

A small source-matched control runs on the sole generic Ray worker with hard
node affinity. It reads an unpredictable control file, checks the poison
callable's source digest and writes a response on the shared mount. The local
observer must read that response. The strict Core entrypoint then receives an
ordinary request with unsupported protocol 2, with the same worker affinity and
runtime environment. Like the production Core runner, the fixture registers the
executor module for serialization by value before creating its Ray definition:
the generic Ray Client head must deserialize it before the task RuntimeEnv makes
django-ray importable. It must return the exact nonretryable unsupported-protocol
completion before invoking the callable. A second control and the local mount
both require the invocation marker to be absent. All owned remote calls disable
automatic retries; timeout or ambiguous submission cannot pass noninvocation.

The inclusive parent cap is 120 seconds. Work, startup and preflights share the
first 90 seconds. Child cleanup has at most 25 further seconds, leaving one for
the fixed receipt and four for the parent's process-group termination. This is
a finite fixture budget, not a package operation latency guarantee. Individual
HTTP reads are at most 5 seconds/64 KiB and do not retry. The owned PostgreSQL
session uses bounded statement and lock timeouts. The helper retains the first
fixed failure phase/type and exports no row identity, request path, raw error,
token, source digest or node identity.

Cleanup first cancels any outstanding owned references. Completed results and
explicit cancellation/task exceptions can confirm termination; transport errors,
crashes and timeouts remain unconfirmed. In particular, a `RayTaskError` wrapping
a transport error is not treated as cancellation. Database cleanup still runs
independently: it locks and verifies the exact original row, terminalizes it,
reopens admission through the production revision check, verifies the original
open/write-protocol/token baseline with an advanced revision, then removes the
unchanged owned row. Foreign revisions, claims or history refuse restoration or
deletion. An initially closed baseline is refused without reopening it.

The sentinel directory is removed only after confirmed remote cleanup, exact
file names/content checks and no foreign entries. Uncertainty leaves the
sentinel as evidence and fails the receipt; the outer owned namespace teardown
remains mandatory. A passed receipt requires all eleven facts, including policy,
row, remote and file cleanup. It always reports `complete_application_gate=false`.

Focused local checks use real SQLite coordination/triggers/manager/API logic,
the production strict decoder, and simulated transport/orchestration failures.
Actual PostgreSQL, Ray Client shared-mount and both-generation behavior require
the source-matched hosted application qualification after integration. This
slice does not establish mixed-version cohorts, protocol-2 execution support,
arbitrary descendant termination, soak reliability or historical failure causes.
