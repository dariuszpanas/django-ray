# Application qualification workloads

`api.py` contains the source-owned HTTP assertion layer used by
`scripts/local_kuberay_gate.py`. A bounded assertion Job can import this module using only the
Python standard library. It does not import the host gate, Django, Ray, or Kubernetes tooling.

`verify_application_api` receives an HTTP transport, a credential supplier and the task deadline.
It retains the existing gate checks: unauthenticated rejection, safe OpenAPI routes, authenticated
statistics and protocol metrics, canonical enqueue identity, bounded task polling and provenance,
durable result `5`, and rejected execution deletion with unchanged detail. The host gate calls the
same function, so these assertions have one implementation.

The caller owns URL scope, redirects, credential handling, per-request timeouts, response byte
limits and required response headers. The function specifies the OpenAPI and task-status limits
and required polling headers through its transport protocol; omitted limits use the transport's
bounded default. Supply a positive finite task timeout. The credential supplier is invoked only
after unauthenticated protection and schema checks pass.

The returned `ApiEvidence` records observations from this layer. A caller may instead supply a
compatible mutable evidence object, as the host gate does, to retain partial progress and the
enqueued task identity after a later assertion fails. Success requires the function to return;
partial flags do not establish a passing layer. Credentials and raw response bodies are not stored
in that evidence object.

The API function proves one layer. The namespace workload below adds generic-node, manager and
encrypted RuntimeEnv assertions, followed by the separate workflow observations described below.
Remaining deployment stages stay tracked in django-ray issue #455. Source/image identity and bounded run evidence must be established
by the workload and its executor before a test result can serve as application proof.

## Running the API layer

From a source checkout or an assertion image containing this directory:

```sh
python -m qualification.application.run_api \
  --base-url http://django-web:8000 \
  --token-file /run/application-credentials/DJANGO_API_TOKEN \
  --task-timeout 180
```

The caller provides the admitted application service origin and a mounted application token file.
The token must contain 32 to 512 ASCII token68 characters, without a trailing newline. It is read only
after the unauthenticated and schema assertions pass. HTTP(S) origins cannot include credentials,
paths, queries or fragments. The transport ignores ambient proxies, does not follow redirects,
verifies HTTPS with the standard system trust store, and enforces response byte limits for success
and error responses. Each blocking socket operation has a ten-second timeout. Task polling accepts
a positive finite timeout of at most 600 seconds; the enclosing Job must also set a hard execution
deadline because socket timeouts do not bound an entire slow-streaming response.

Exit zero means the shared API function returned successfully. A failed assertion exits one. Both
outcomes emit a single JSON receipt containing the layer name, status, elapsed time, request count,
last HTTP status and typed partial observations. Failure messages and raw bodies are omitted because
they may contain application data. Partial observations remain diagnostic even when the layer
fails. The receipt always reports `complete_application_gate: false`; it does not establish image
identity, Ray generation, manager ownership, cleanup or any other gate layer.

The command starts no services and has no Kubernetes client. `core.yaml` wires the API layer
into a bounded application stage; its admission, image binding and evidence requirements follow below.

## Collector pressure within the workflow layer

`run_workflows` also requires `workflow_pressure.py` to pass before writing a
successful workflow receipt. Before and after the existing required cold-Ray
replacement, four serial actors exercise ordinary-node and map success/failure
with injected 1,300/1,400-byte retained-state limits. Each actor uses 0.25 logical
CPU, no restart or method retry, and the source-verified `/runtime/recovery.zip`.
The local archive is supplied at Ray job initialization and inherited by actors;
Ray actor options cannot accept a local archive path.
The probe owns its Ray Client connection, limits individual result waits to 20
seconds within a 120-second budget, kills each actor and observes its removal.
The surrounding assertion Job bounds connection/shutdown time and owns namespace
cleanup; this adds no shared cluster or capacity changes.

The fixed `collector_pressure` receipt proves actual pressure eviction/drop,
canonical byte accounting, admitted lifecycle events, unchanged failure details,
retained map counts and successful terminal detail preparation. A missing or
failed probe prevents the workflow layer from passing. This is collector and
preparation evidence from synthetic identities, not a persisted Django task,
default graph activation or aggregate mailbox admission. The separate workflow
cases retain their real task, API/Admin, history and diagnostic assertions.

## Reporting-policy cost evidence

After cold-generation API/Admin acceptance, a separate serial Job runs
`run_reporting_benchmark` against the same installed candidate, PostgreSQL,
manager and Ray generation. It executes three counterbalanced cycles of full,
terminal-only and disabled reporting: nine tiny durable tasks in total. Each
task has a 45-second observation deadline; the Job has a 600-second hard deadline,
no retries, 200m CPU and 512 MiB memory. It uses existing namespace capacity.

The command validates terminal graph/storage/counter consistency and saves the
full report before deleting only its nine owned terminal executions. The receipt
requires completed cleanup and all three policies. `reporting.json` retains every
sample and aggregate inside a 512 KiB envelope; its producer must be the completed,
unrestarted candidate-image container. Existing 16 KiB receipt and 64 KiB log
limits remain unchanged for other layers. Namespace removal remains mandatory.
The outer source-tree and running-image checks supply deployment attestation;
an unavailable revision string inside the benchmark is not independent proof.

The comparison measures the documented observable cost layers, not a production
SLO, causal speedup, physical Ray mailbox bound or complete process lifetime.
The storage-oriented `Workflow Progress Benchmark` workflow is a different
measurement and cannot substitute for this report. A missing/failed benchmark
or cleanup receipt blocks this application qualification run.

## Generic Ray nodes and cold generations

`generic_nodes.py` supplies a separate assertion Job layer. It connects only to an explicit Ray
Client address and sends a by-value function to every live node using hard node affinity, zero
logical CPUs, no task retries and an empty RuntimeEnv. The function imports only Python's standard
library and Ray. A generic node must have no preinstalled `django_ray`, must see the exact bounded
source and recovery archives, and must contain the application image's `remote.py` in both archive
layouts. Required task modules, Ray version and Python major/minor must match. Python patch-level
differences are allowed; this inspection does not prove native-extension ABI compatibility. The
application's remote task assertions must establish that separately.

For example, with archives mounted read-only at the same paths in the assertion image and generic
Ray nodes:

```sh
python -m qualification.application.generic_nodes \
  --address ray://ray-head:10001 \
  --source-archive /runtime/project.zip \
  --recovery-archive /runtime/recovery.zip \
  --remote-source /app/src/django_ray/runtime/remote.py \
  --receipt /receipts/before.json
```

After the executor has deleted the first RayCluster, confirmed removal and created its replacement,
run a second Job with the same source, image, archives and arguments, adding
`--previous-receipt /receipts/before.json` and selecting `--receipt /receipts/after.json`. Every new
node ID must differ from every previous ID. Membership must remain unchanged during each probe,
and the source/archive/runtime observations must match across generations. The second receipt
includes the SHA-256 of the exact first receipt bytes. Existing output files are never overwritten.
Use a fresh, namespace-owned receipt volume for each run and require both Jobs to succeed.

The default task-result timeout is 120 seconds, configurable up to 300; the default node count is
two, configurable from one to three. Each archive is capped at 32 MiB, the remote source member at
1 MiB and each receipt at 16 KiB. Enclosing Jobs must also enforce a hard deadline: Ray Client
connection, file I/O and shutdown do not share the result timeout. The probe requests cancellation
of its submitted tasks and disconnects its own client on failure. It refuses to adopt an existing Ray connection.
No local Ray cluster is started.

Exit zero requires all assertions and exclusive receipt-file creation to succeed. A failed command
prints a fixed failed receipt with no partial node observations or raw dependency exception. Every
receipt reports `complete_application_gate: false`. The executor still owns immutable source/image
binding, Pod/image and RayCluster lifecycle evidence, resource admission, bounded receipt collection
and cleanup. The module has no Kubernetes authority and does not replace the host gate.
Its existence or resource-free unit tests do not establish a passing live generic-node layer.

## Offline application core stage

`core.yaml` is a native public Chainsaw Test. Its ordered steps start disposable PostgreSQL 17,
prepare the sample web application and locked recovery archives, and start one current core task
manager with stock Ray 2.58.0 head/worker nodes. Two serial Jobs require authenticated API execution,
exact durable task/attempt/current-manager ownership, authenticated encrypted RuntimeEnv snapshots
and the remote decrypted canary. Between them Chainsaw foreground-deletes and recreates RayCluster.
Both generic-node receipts require identical archives/runtime and disjoint node identities.
The web and Core manager Deployments remain. The separate rq2 layer below replaces its own
Jobs-only manager while requiring the same running Job to survive.

`settings_qualification` retains production validation and replaces the project RuntimeEnv with the
locked recovery ZIP; `thin` and `recovery-showcase` inherit it and retain their own profile labels.
No pip download or `PYTHONPATH=src` is used in those profiles.
The task environment preserves the validated Ray Client target in the separate
`DJANGO_RAY_QUALIFICATION_CLIENT_ADDRESS` variable. Ray's Jobs supervisor replaces `RAY_ADDRESS`
with its native GCS target after applying RuntimeEnv; the settings overlay validates the separate
Client target for application configuration while leaving the supervisor's process environment
unchanged. Core workers retain the same explicit Client configuration. Database and encryption
keys remain inherited from the disposable Pod's configuration and Secret. This owned overlay
requires encrypted snapshots with `RUNTIME_ENV_STORAGE_MODE="required"`; the package default
remains unchanged.
The fixture declares a retry environment revision derived only from its validated Ray address,
fixed profile labels and non-secret storage probe. Credentials are excluded. The recovery endpoint
requires this identity for the explicit environment variables; the locked archive retains its
separate content identity. This fixture declaration is not a general trust default for consumers.
Ordinary sample settings remain unchanged. The core task assertions do not certify their
dependency-download behavior, workflow recovery, or a complete release upgrade.
Workflow evidence requires the separate workflow receipts below.

Each core observer first checks the database-owned 60-second lease authority.
A real worker registration creates one inert owned control lease; the control
must be healthy. A cluster-mode worker configured for a conflicting six-second
lease must refuse at startup before lease creation, runtime initialization or
claiming. Fail-fast instrumentation observes those later entry points without
allowing work to start. Readiness and operator status must report the mismatch,
and compatible-worker availability must be `null`. Existing peers retain their
identity and active state while normal heartbeats may advance; the authority
remains unchanged and only the exact control is removed. Eleven fixed boolean
facts, a 32-lease inventory cap, a 20-second Linux alarm and finite database
timeouts bound this negative probe. It does not replace the normal matched-policy
worker workload or authorize a policy transition under live managers.

Each core observer also commits exactly two malformed RuntimeEnv fixtures atomically:
one changed ciphertext and one unavailable key. The existing manager must claim both and
reject them before dispatch, with the expected fixed failure category, attempt 1 and
generation 1, one matching failed attempt, and current manager ownership. Neither row may
retain a Ray job, request reference, execution address, executor version, completion,
result, progress or traceback. Authenticated retry of each row must return the fixed
snapshot-validation HTTP 409 and preserve every concrete execution and attempt field.
The observer compares those snapshots again after both cases finish.

These cases run before and after cold Ray replacement within the existing core Jobs.
They have a shared 60-second observation budget, bounded 64-KiB HTTP responses, the
existing per-request transport deadlines and the enclosing 600-second generation phase
and the accounted 2820-second test deadline. No extra cluster, process, queue or resource allocation is
introduced. Namespace teardown owns the four fixture rows across the two generations.
The core receipt retains only four booleans per fixed case; a second-case timeout keeps
the first case's observations but fails the stage. Missing, partial or unexpected negative
case evidence cannot pass collection. Task identities, snapshot hashes, ciphertext,
envelopes, keys and raw exceptions from these assertions never enter their receipts.

The separate `runtime_env_maintenance` observer then exercises the public maintenance
command in each generation. It bounds inventory to 800 rows and 8 MiB, each snapshot
to 64 KiB, and each rewrite/check pass to eight 100-row pages within one shared
60-second observation deadline. It creates two captured probes in an unconsumed
owned queue: one plaintext snapshot and one encrypted under a temporary old key.
Only the maintenance command receives that old key. Rewrite must preserve every
other concrete field and all unrelated rows; a complete check must authenticate
both replacements under the current key. Expected malformed fixture rows remain
visible and unchanged, including the exact prior-generation identities from the
validated receipt. No later successful page can erase an earlier unexpected outcome.

After verification, both probes enter the ordinary queue and the existing manager
must execute the original canary with the installed package. A third plaintext
probe must fail before dispatch under required mode, and its API retry must return
409 without changing its execution or attempt. Only these three known terminal
owned rows are removed. Twenty fixed boolean assertions require bounded scans,
identity and unrelated-row preservation, real execution, refusal, protected HTTP
output and successful cleanup. Missing or partial evidence fails collection.
The existing three-row protected-log and process observer keeps its original
scope; these new probes do not extend that proof to arbitrary process surfaces.
Namespace cleanup remains mandatory, and no resource allocation or outer phase
deadline increases. This current-table fixture does not qualify backup key retirement.

This provides deployed assertions for manager refusal, absence of retained submission/result
identity and retry preservation; it does not independently observe every possible external
effect. The focused worker regression also traps dispatch calls before Ray. Protected-value
scanning and ordinary-result presentation use the separate observers below; cancellation and
monitoring have their own qualification fixtures. The broader catalogue remains in #455.
A fixture definition or mocked test is not a passing hosted observation;
acceptance still requires the exact-source receipts and owned cleanup described below.

### Request-reference Jobs assertions

`run_rq2.py` runs after the existing first/warm, node, Core and workflow assertions in each
generation. It owns one `rq2-qualification` queue and one child manager at concurrency one,
using the fixed `http://ray-head:8265` Jobs target. `settings_qualification_jobs` overlays only
these subprocesses; the existing Ray Client settings and Core manager remain unchanged. The
overlay and two-case callable travel in the same locked recovery archive whose bytes the
generic-node layer compares on both fresh Ray generations.

The Jobs overlay inherits the Core manager's common 60-second lease duration and uses a
two-second heartbeat. Lease cleanup spans the shared table, so a shorter Jobs-only lease
would also expire healthy Core workers on other queues between their 15-second heartbeats.
Manager replacement waits for natural expiry and reconciliation; it never forces a lease
inactive. The callable's 90-second wait and the helper's 170-second work, 175-second process
and 180-second phase limits remain fixed. Both generations must pass within those limits.

The positive callable records one bounded effect and waits for an explicit release. The helper
requires a new `rq2ref:v1:` reference, matching active registry record, exact request-reference
entrypoint and metadata/content/DB identity binding. Bounded raw JobInfo and logs must omit the
callable, arguments, public task identifier and credential values, including the RuntimeEnv key.
It kills its current manager with SIGKILL, waits for a different live manager lease to adopt the
same Job, attempt, generation and reference, then releases the callable. Completion must preserve
that identity and exactly one effect. The bounded Jobs inventory must contain one submission with
the identity and one matching coordination, request-content and reference binding, preventing a
second submission ID from passing the same-request check.

With that manager stopped, the negative case uses the production runner to prepare an actual
submission, deletes only its owned object through the typed request deleter, then submits the
unchanged spec once. Exit 78 and the fixed request-refusal diagnostic must accompany absence of
both the settings-import sentinel and callable effect. A fresh current manager must reconcile
one failed archived attempt with no result, completion or scheduled retry. Retries remain enabled;
the helper observes the terminal fence for at least four seconds, covering two configured
Jobs heartbeat periods without asserting that two heartbeat writes occurred. No arbitrary external
effect, general process-surface secrecy, multi-node storage backend or complete upgrade claim is
made by these two cases.

The setup container creates `/receipts/rq2`; both generic Ray Pod templates and both assertion
Jobs mount only that subdirectory at `/rq2`, writable by their existing UID 1000. Request objects
and effect markers never enter `/runtime` or the source archive. Archives retain read-only mounts.
This reuses the owned receipts claim and current container CPU/memory limits, adding no Pod,
Deployment, PVC or cluster. Child-manager output is suppressed. Ten fixed boolean assertions are
retained separately for each generation; partial or missing evidence cannot pass collection.
Failure output retains only fixed phase, error and manager-exit categories. Before the first
effect, at most one task row, two matching leases and one bounded JobInfo call are sampled every
two seconds. Fixed categories distinguish an unclaimed row, a missing/inactive manager lease,
request/submission bindings, task error presence/origin, Job status, Ray's declared error type and driver
exit category. Terminal task states fail immediately instead of waiting for an absent effect until
timeout. No identifiers, free-form errors, Job messages or logs enter these diagnostics. An unreadable
Job is unavailable, not evidence of absence. Error origins use exact fixed preparation errors or
the production reservation/submission prefixes, evaluated in SQL; prefix categories do not prove
the remote acceptance outcome or identify a root cause. The last completed observations are retained; they are
not necessarily simultaneous with the failure. The first failure remains separate from cleanup.
Namespace cleanup owns the four fixture rows and their files. These definitions and local tests
are not a passing deployed receipt: exact-source Linux CI, both live generations, source/archive
correspondence and owned cleanup must still pass for acceptance.

### Protected application surfaces

The core observer compares its existing RuntimeEnv canary enqueue/poll and two
refusal retry responses against the fixed marker, unavailable key label and the
three owned encrypted envelopes, nonces and ciphertexts. Capture stays in memory:
128 bodies, 64 KiB each and 4 MiB total. It does not add network waits to the
existing workflow phase. Exact owned row IDs and the observation start time are
safe coordination fields; protected values never enter receipts. Database-side
byte guards prevent oversized envelopes from being transferred into the observer.

After each generation finishes, a separate host observer has 30 seconds to read
the current web and manager container logs and compare them in the existing web
Pod. It preserves the caller's explicit Kubernetes context and original absolute
kubeconfig path list across Chainsaw's child environment, without copying config
contents. It binds the candidate image,
requires the exact Pod UIDs with no restarts before and after, and rejects a
replacement or a log whose earliest timestamp does not cover the observation
start. Each full log has a 1 MiB cap plus one-byte overflow detection; accepting a
truncated tail cannot pass. Before either log snapshot, a private checker reads
the two exact refused task-status APIs with one-second socket limits under a
four-second in-Pod alarm and seven-second host command limit. The host allowance
also covers exec transport, initial Python module loading and receipt return outside the
child alarm; it does not extend the child's work deadline. Both must report the
bounded contract, exact task identity, one failed attempt/generation and no
protected value. The subsequent log-only checker receives at most 3 MiB on private
stdin and has a five-second in-Pod alarm and six-second host limit. Both use
direct read-only PostgreSQL projections of exactly the three owned rows, with
a two-second connection timeout and one-second statement and lock limits. The
private processes do not initialize Django or its Admin applications. Database-side
byte guards bound each encrypted envelope before transfer; shared pure validators
retain the same row identity, fixture, state, envelope and refused-task UUID checks
as the ORM observations. Credentials and protected values stay inside the web Pod.
The connection closes before either status request or the log-only comparison.
Metadata and receipt reads allow two seconds each, and each full log read allows
five seconds. These
seven sequential command limits total 29 seconds within the fixed 30-second
observer, leaving one second for other overhead including final receipt handling;
final identity revalidation is already in those command limits. Every command is
clipped to the remaining observer time, and no command resets that deadline.
Successful checkers emit only their existing fixed boolean facts. On failure,
the host emits at most 512 bytes of fixed JSON identifying the observation phase
and a `timeout`, `invalid` or `unavailable` category. The private checker retains
its version-1 failure schema over stdout; the version-5 host failure schema retains
inventory attribution and adds nullable fixed worker-readiness and log-reason
diagnostics. The host propagates its child phase
only after exact schema validation on a failed command. Unknown fields, duplicate
keys, oversized output and arbitrary exception or subprocess text cannot enter
these diagnostics. A child database or API timeout and a host command timeout remain
distinct observations; elapsed time alone does not identify a cause. The larger
status host allowance leaves child deadlines, command order and successful receipt
schemas unchanged; it adds no retry or resource allocation.

The host sends that closed failure line to stderr and also attempts to retain
the exact same UTF-8 line beside its caller-selected receipt output, appending
`.failure.json` to the complete filename. The existing fixture therefore uses
`before-protected-logs.json.failure.json` or
`after-protected-logs.json.failure.json`. Each sidecar is at most 512 bytes,
including its newline, and accepts only the canonical current host schema.
Private child mode and invalid host arguments do not write sidecars. Exclusive
creation never overwrites an earlier file or follows an existing target symlink.
Storage errors emit no raw text and cannot replace the original failure or exit
status. Interrupted execution or failed storage can leave evidence absent or
incomplete; a sidecar is diagnostic only and cannot replace a required passing
receipt. This adds no observation call, retry, deadline or resource allocation.

Inventory failures additionally identify a fixed source role and the rejected
identity, lifecycle or readiness predicate. These fields never contain Pod names,
images, labels, raw probe output or exception text. A running container that is
not ready remains a failure; a recent manager heartbeat alone does not replace
Kubernetes readiness or the required protected-log observation.

Web and manager log acquisition failures additionally retain a fixed `log_reason`
for the predicate that failed or could not complete. `source_timestamp` and
`source_interval` identify container start parsing and interval comparison;
`command` identifies command execution or its remaining deadline; `command_exit`,
`output_bound` and `command_stderr` identify nonzero exit, excess bytes and stderr;
`empty_log`, `first_timestamp` and `start_coverage` identify absent evidence,
timestamp decoding/length/parsing and observation-start coverage. No command,
exit value, output bytes, timestamp or exception text is exported. Other phases
retain a null reason, and child failure and successful receipt schemas remain
unchanged. A log acquisition or coverage refusal happens before private-content
comparison and cannot establish either a leak or clear log surfaces. These
classifications improve future failure attribution; they neither recover the
cause of an older ambiguous record nor repair or accept its failed run.

Only a running, unrestarted manager with the expected source image that fails
readiness receives one follow-up command. It calls the existing
`django_ray_worker_ready` command in that manager with the same queue and Pod
hostname environment as its Kubernetes probe. The follow-up keeps the original
inventory failure even if it observes a ready lease; it does not read any logs
after that failed inventory or retry the qualification.

This optional command spends at most ten seconds within the unchanged 30-second
observer deadline, retaining one second for the failure report. With fewer than
four seconds available it records `incomplete` without spawning. A separate
`timeout --signal=KILL` owner in the manager limits Python/Django startup and the
probe independently of the host transport. The host reserves a second to signal
its owned kubectl process group before reaping the leader. Missing `timeout`,
unavailable transport, malformed output, or unconfirmed local cleanup produces
an incomplete/error diagnostic, never acceptance. The qualification image copies
this helper with its other application modules; no image or probe configuration
is changed, and the optional utility is not assumed available as product proof.

The child suppresses ambient stdout/stderr before emitting four ordered, fixed
milestones: `helper_entry` before importing Django, `django_setup` before setup,
`django_setup_complete` immediately after setup returns and before importing the
management module, and `worker_ready` before calling the readiness command.
These private milestone frames use version 3, with a fixed phase index and bounded
resource sample; the worker-lease terminal report remains version 1. The host
requires the complete current sequence, so missing,
duplicated or older-version frames cannot precede an accepted terminal report.
Older-version, reordered or duplicated frames invalidate even phase evidence.
The first milestone does not cover interpreter startup or this helper's own
module imports. The host reads at most 513 bytes to enforce the unchanged
512-byte stream ceiling and
discards stderr. Only the exact milestone sequence and a complete worker-lease
report with a matching actual exit code supply a finite status and reason; that
observation has phase `complete`. Other results retain unknown status/reason.

On timeout, the last complete milestone can survive only successful local owned
process cleanup. An incomplete version-3 milestone invalidates phase and sample
evidence; the host does not repair or guess a truncated numeric record. After all
four milestones, an incomplete suffix may be an exact byte prefix of a permitted
finite terminal report. Malformed, reordered, extra or oversized output invalidates
phase evidence too. Cleanup failure or lost process ownership discards all
phase/report/sample proof. Milestones do not reset the deadline, and even a
complete buffered report cannot override a timeout or contradictory exit. Exit
137 is a timeout-class observation compatible with SIGKILL; it does not prove
that the deadline owner fired or that the remote process was cleaned up.

The entire version-5 host failure still fits 512 bytes, including phase, outcome,
bounded exit code and elapsed milliseconds (capped at 10,000). A milestone marks
the last received boundary, not the exact stalled operation. In a new observation,
`django_setup` without `django_setup_complete` means the host did not observe the
setup-return milestone; it does not prove setup was still running when stopped.
`django_setup_complete` without `worker_ready` separates that return from the
following management import and command preparation. `worker_ready`
includes command discovery and settings/database/policy/lease reads. The private
wrapper does not reproduce the normal management CLI's connection-close teardown.
This remains a later observation without another Pod UID validation, not proof of
the original readiness failure's cause or replacement source-bound qualification.
Older version-4 observations retain their original, less precise boundary; the
new checkpoint does not retrospectively diagnose the timeout tracked in
[#669](https://github.com/dariuszpanas/django-ray/issues/669).
The API/Admin log surface here means the shared django-web server log, not a
separate rendered Admin-page assertion.

Each existing checkpoint synchronously samples elapsed monotonic time and the
helper process's user-plus-system CPU since its initial checkpoint. No sampler
thread, timer, signal handler, supervisor or extra Kubernetes call is added.
The same process and timeout owners remain responsible for the observation.
These samples answer how much time elapsed before a received setup-entry or
setup-return boundary. They exclude interpreter startup, initial helper imports
and all work after the last sample. In particular, a timeout whose last phase is
`django_setup` does not measure the subsequent setup interval or its CPU cost.
Process CPU includes all helper threads and instrumentation, not descendants or
only the main thread. CPU can exceed wall time; their ratio does not identify
waiting, scheduling pressure or a particular cause.

Optional cgroup-v2 samples use only bounded fixed files after verifying the
supported namespace-root mapping. They retain deltas for usage, periods,
throttled periods and throttled time, without exporting cgroup paths or process
identifiers. Unsupported topology, unavailable or malformed data, changed scope
or limits, counter reset and overflow remain explicit unavailable values, never
zero or a new baseline. Additional bounded kernel counters, including dotted
names such as `core_sched.force_idle_usec`, are validated and ignored; they do
not invalidate the four selected counters or add fields to the record. Duplicate
keys, malformed names or values, and missing required counters still fail closed.
All samples use the same initial baseline. Clock brackets around the reads
identify the sampled interval; these endpoints are
not atomic and cannot prove that the scope never changed between them.

Cgroup values include the manager, ordinary probes, other exec commands and
descendants in that group. They are not the helper's CPU usage and cannot name a
competing process. The group's bandwidth counters do not cover inherited
ancestor throttling, so a zero delta cannot rule out scheduling or ancestor
limits. Do not subtract helper CPU from group usage to attribute other work.
The four synchronous samples spend the existing observation budget. Bounded
reads limit retained bytes, not kernel syscall latency; collection and encoding
have overhead and may themselves be interrupted.

Private version-3 frames map phase indices 0 through 3 to the four checkpoints
above. Their `r` sample is either unavailable (`null`) or this fixed tuple:

| Slot | Meaning | Unit |
| --- | --- | --- |
| 0, 1 | Start and end of this sample's read bracket since the initial helper clock | milliseconds |
| 2 | Helper process CPU since its immutable initial CPU reading | microseconds |
| 3 | Cgroup usage delta | microseconds |
| 4, 5 | Cgroup period and throttled-period deltas | counts |
| 6 | Cgroup throttled-time delta | microseconds |
| 7 | Cgroup availability category | fixed integer |

Wall values are integers from 0 through 10,000; CPU and cgroup values are integers
from 0 through 99,999,999 or `null`. Booleans are not integers in this contract.
Cgroup category 0 means observed; 1 means unsupported, 2 unavailable, 3 reset,
4 scope changed and 5 overflow. Categories 1 through 5 require all four cgroup
values to be `null`. Overflow is not clamped into an apparently valid sample.

The host also records when the first and last complete milestone lines arrive,
within its existing elapsed-time bound. These are receive-time upper bounds;
buffering and transport prevent treating them as child emission timestamps.
Only the newest complete validated sample is retained, including its unavailable
values. A newer unavailable sample never borrows older counters. Malformed output
or unconfirmed local cleanup discards resource evidence together with phase proof.

An optional, separate `before-protected-logs.json.failure-resources.json` or
`after-protected-logs.json.failure-resources.json` retains the fixed resource
record beside the original failure sidecar. Both the private stream and this
canonical resource sidecar remain capped at 512 bytes including newlines. The
record explicitly describes the last observed prefix and disclaims terminal
accounting. Its exclusive, best-effort write cannot overwrite existing evidence,
follow an existing target symlink, replace the original failure or alter a
successful receipt. Missing or partial storage supplies no usable measurement.
Resource retention requires a newly and completely written host failure. An
existing resource sibling, including an orphaned symlink, prevents a new pair.
These are exclusively owned output paths, not an atomic multi-writer protocol.
The host failure schema stays at version 5; earlier private streams retain their
original parsers. Neither these measurements nor a later ready result accept the
failed qualification or establish its cause.

The separate [isolated startup profile](../startup/README.md) compares full Django
setup with and without project bytecode in bounded containers. Its measurements
are diagnostic controls, not readiness results or a replacement for this fixture.

The fixed positive rq2 callable also inspects actual visible `/proc` argv before
its first effect. Its strict task context, current process and decoded locator
must bind the exact persisted canonical request. The scan includes the driver and
at most 256 visible processes in that owned Ray Pod, capped at 64 KiB per argv,
1 MiB total and two seconds. Only a verified vanished non-driver PID is tolerated;
permission failures, reused identities, absent driver or overflow refuse the
observation. This is one Pod's visible process namespace, not a global secrecy
claim. The existing rq2 deadline, task result, single effect, recovery and cleanup
assertions remain required; the process receipt contains three fixed booleans.

Both `before-protected-logs.json` and `after-protected-logs.json` are mandatory.
Unexpected private bytes are never retained as raw failure artifacts. Successful
source receipts keep their existing exact-byte identity and layer validators;
unknown top-level fields are refused. Failed application receipts retain only
finite HTTP, generation and rq2 diagnostic fields. Resource diagnostics omit
free-text messages and raw Pod stdout/stderr; the workflow also omits raw operator
logs. Actor diagnostics are projected
through fixed categories, bounded numeric values and validated identifiers.
Actual source-matched Linux execution is required for these assertions. Local
fixture tests establish validation/refusal behavior, not deployed log or argv proof.

### Shared workflow assertions

`workflow_envelopes.validate_workflow_envelope()` is the shared workflow response
identity assertion used by the retained local gate. It rejects mismatched tasks,
runs, attempts, execution generations, publication revisions and completeness,
including detail revisions on a summary-only response. Schema epochs and counters
must be integers, not JSON booleans or floating-point lookalikes.

This helper consumes an already bounded, decoded mapping. Its caller must enforce
HTTP byte limits, authentication, trusted expected identities, source/image binding,
execution and cleanup. It does not itself perform requests or prove graph contents.
The public workflow runner and its native success/failure/retry/history evidence
remain [#512](https://github.com/dariuszpanas/django-ray/issues/512); importing this
helper does not qualify workflow visualization or change publisher defaults.

### Public prerequisites and invocation

Use an explicitly admitted Linux Kubernetes environment with public KubeRay 1.6.2 already installed,
a compatible `kubectl`, [Chainsaw 0.2.15](https://github.com/kyverno/chainsaw/releases/tag/v0.2.15),
and Python 3.12+. No private test service, schema, image registry API or executor is required.
The host runner and receipt validators use only Python's standard library. Django and
Ray execution helpers load inside the application fixture, not during host startup or
receipt collection.
Never create or resize a shared cluster merely to run this test. The path-selected
[Application Qualification workflow](../../.github/workflows/application-qualification.yml) waits
for the current source Linux `CI Gate`, then hosts this stage on a disposable GitHub Actions Linux
Kind cluster with a 3-CPU/12-GiB node limit. Its local image registry has a separate 0.25-CPU/128-MiB
limit. Builds precede cluster creation on the hosted 4-CPU/16-GiB VM; normal docs-only PRs do not
trigger this supplemental workload. It also supports manual `workflow_dispatch`. The three PVCs require same-node ReadWriteOnce storage;
pass Kind's `standard` StorageClass or Docker Desktop's `hostpath` explicitly.

Build the root Dockerfile and `qualification/application/Dockerfile` from the same clean committed
Git archive, with Python 3.12/Linux amd64 matching the pinned stock Ray image. The derived image
inherits the application entrypoint and adds these assertion modules. For example, after admitting
build capacity and selecting a registry you can write to:

```sh
candidate=$(git rev-parse HEAD)
build=$(mktemp -d)
git archive "$candidate" | tar -x -C "$build"
docker build --platform linux/amd64 --provenance=false -t "$REGISTRY/django-ray-base:$candidate" "$build"
docker push "$REGISTRY/django-ray-base:$candidate"
base=$(docker image inspect "$REGISTRY/django-ray-base:$candidate" --format '{{index .RepoDigests 0}}')
docker build --platform linux/amd64 --provenance=false -f "$build/qualification/application/Dockerfile" \
  --build-arg DJANGO_RAY_RUNTIME_IMAGE="$base" \
  -t "$REGISTRY/django-ray-core:$candidate" "$build"
docker push "$REGISTRY/django-ray-core:$candidate"
image=$(docker image inspect "$REGISTRY/django-ray-core:$candidate" --format '{{index .RepoDigests 0}}')
python -m qualification.application.run_chainsaw --context "$KUBE_CONTEXT" \
  --image "$image" --storage-class standard --output "$EVIDENCE_DIRECTORY"
```

The output directory must be new and outside the clean checkout. The caller retains the Git archive,
build identity and source/package verification with the receipts; an image label alone is not source
proof. The wrapper never builds, pulls or pushes images, installs an operator or changes cluster-wide
configuration. Registry access and image availability on the admitted nodes are caller prerequisites.
Build archives may be removed after retaining their identity; no unrelated Docker images are pruned.

The runner requires an explicit context and the immutable digest of a concrete Linux-amd64 image
manifest. The single-platform builds disable provenance so an attestation-bearing image index does
not obscure comparison with the manifest digest reported by the container runtime. It creates one fresh
`django-ray-core-*` namespace and six random application Secret values via stdin, without storing
them in evidence or exposing them in command arguments. The two ordered Django secret parts compose
the production secret; the separate unpadded base64url key selects authenticated encryption under
key ID `qualification`, with Django-secret fallback disabled.

`chainsaw.yaml` pins serial execution, foreground deletion and operation timeouts. The runner uses
[public external values](https://kyverno.github.io/chainsaw/0.2.3/configuration/options/values/)
for the image and StorageClass. Automatic Chainsaw teardown is delayed with `skipDelete` so receipts
can be collected; the explicit mid-test RayCluster delete still runs. The wrapper then checks the
namespace UID, deletes only its owned namespace and requires observed absence. It returns nonzero
if the test, receipt validation or cleanup fails. An interrupted host process may require manual
cleanup of the exact recorded namespace; no cluster deletion or shared resource cleanup is attempted.

### Resource and evidence boundaries

Source inventory, conservatively counting all three serial Jobs and init peaks, is **2.8 CPU, 10240 MiB
memory, 3456 MiB ephemeral storage, eight Pods and 1408 MiB PVC requests**. Every source container has
non-root identity, no service-account token, a read-only root filesystem and explicit limits.
KubeRay, Kubernetes, image builds and the host-side Chainsaw process are additional reservations.
Storage requests do not prove physical disk enforcement. The caller admits total capacity and
network reachability/isolation; this test does not install or qualify a network-policy provider.

Each Ray Pod initializes `/tmp/workflow` as a worker-owned mode-0700 directory and sets `TMPDIR`
to it before Ray starts. The non-root initializer mounts only the existing scratch volume and
receives no credentials. Its resources fit beneath the regular container's reservation, so the
inventory above is unchanged. An fsGroup-backed emptyDir can expose a root-owned, non-sticky
mode-2777 `/tmp`; the workflow publisher correctly refuses that shared parent for SQLite preparation.
Creating a private child and explicitly clearing inherited setgid preserves the ownership checks.

Each generation preserves the original phase budgets: first/warm pair 180 seconds,
node/Core/workflow matrix 600 seconds, and rq2 180 seconds including its child-manager
cleanup. Unused time never extends a later phase. The rq2 helper reserves
170 seconds including Django bootstrap and observations. A work alarm interrupts blocking calls,
then only final shutdown receives a five-second alarm: two seconds for TERM, two for KILL/wait,
and receipt margin. Startup cannot borrow cleanup time. The parent generation retains its
process-group bound.

Two additional child phases run serially in that same assertion Job: the single-attempt
reservation-failure showcase has 180 seconds including cleanup, and unsupported queued
protocol observation has 120 seconds including cleanup. A separate host-side protected-log
observer has 30 seconds after each generation's assertion Job, before Ray replacement.
The showcase's work alarm is 150 seconds. The protocol child reserves 90 seconds for work,
25 for cleanup and one for its receipt. The parent reserves its four-second TERM/KILL
allowance inside each new child phase, so their subprocess waits are at most 176 and 116
seconds respectively. The [protocol observer contract](protocol.md) describes the exact
queued-row preservation, shared-worker control and admission restoration requirements.
No new namespace, Pod, cluster, queue capacity or resource reservation is introduced.

`budgets.py` records the explicit additive accounting. The assertion Job deadline is
1290 seconds (the five child phases plus the original 30-second startup margin), and
its Chainsaw assertion is 1305 seconds. Reporting Jobs and other assertions retain
600 seconds. The host test ceiling increases from 2160 to **2820 seconds**:
`2160 + 2 * (showcase 180 + protocol 120 + protected logs 30)`. The native workflow
step reserves **55 minutes**, including the separate 180-second worker-image phase
and the inherited 300-second external aggregate allowance; its whole job stays
75 minutes. Owned namespace deletion retains its separate 180-second allowance within
that external allowance. The allowance is not guaranteed worst-case startup, collection
or cleanup coverage. These are ceilings, not expected durations: every phase maximum
cannot be consumed in a single passing run. Actual combined-source hosted receipts must
establish that the workload fits; source tests and arithmetic do not establish that proof.

The pre-catalogue 2160-second envelope is the qualified pre-rq2 1800 seconds
plus one 180-second rq2 allowance for each generation; first/warm was already in that
baseline. The two rq2 phases therefore retain the earlier aggregate allowance for setup,
cold replacement and reporting. Catalogue phases add their separate 660 seconds to this
repaired baseline. These are ceilings, not expected durations or a performance guarantee.

The standard-library generation runner launches fixed argument lists in the original
order and emits bounded phase timing markers outside the receipt schema. A failure or
timeout stops later commands. On interruption or timeout it terminates the owned POSIX
process group, allows two seconds, escalates to a kill and waits at most two more seconds.
It never signals a group whose leader it has already reaped. The runner is the container's
initial process, and Pod teardown bounds remaining descendants after an ordinary child
failure. This adds no retries or product runtime deadline changes.

The workflow API/graph observer opts into one additional GET attempt only after a
connection reset or remote disconnect while awaiting response headers, before any
HTTP status arrives. The attempts share the original request-timeout window (at most
10 seconds), with the remaining time used for the next connection; an expired response
cannot pass. Socket timeouts still bound individual blocking operations, so the outer
Job remains the hard bound for slow streaming. Timeouts, writes, HTTP statuses, response
bodies and contract mismatches are never retried. Other transports retain fail-fast
behavior. Passing workflow receipts retain fixed-category `http_transport_recoveries`
counts, and transport failures expose a fixed `transport_failure` category without
dependency exception text. A hosted pre-status failure coincided with Gunicorn worker
recycling; the retained evidence did not establish which transport exception occurred.

Typed HTTP failures also retain a fixed `observer` (API, core, first-workflow,
workflow, or nested Admin) and `request_substage` (task status, workflow summary or
collection, Admin graph or page, or other). These labels never contain paths, query
strings, task identities, credentials, response bodies or dependency exception text.
The receipt preserves the existing phase, status and transport category, and adds the
method, attempt number, configured socket timeout, actual attempt socket timeout,
effective retry policy and remaining retry window. Socket waits are per blocking
operation; these fields do not claim a hard whole-request deadline.

The generation runner passes its existing monotonic deadline only to its local child
process, which shares that clock. It is never sent between hosts, Pods or Ray nodes.
Failures observe the remaining outer budget when raised; absent, malformed or
out-of-range context produces `null`. Nested Admin reads additionally report their
own observer deadline and retain their separate at-most-five-second socket timeout
with no retry. Their failures propagate through the workflow and first-workflow
receipts; the main client's recovery counters do not describe nested Admin reads.
All timing observations are finite, nonnegative and at most 600 seconds. They do not
change timeout enforcement, retry eligibility or receipt/log byte limits.

The repeated header timeouts tracked in [#613](https://github.com/dariuszpanas/django-ray/issues/613)
occurred near graceful web-worker recycling, but the historical receipts lack the
caller and effective deadline needed to establish a cause. Passing an unchanged-source
repeat does not repair that intermittent failure. Deterministic transport regressions
verify attribution; source-matched Linux application qualification checks integration.

The runner retrieves at most 64 KiB from each exact setup/assertion container's log. It requires
zero successful-container restarts, exit zero, the requested immutable image with its manifest
digest in the observed container image ID, and every expected
passing JSON receipt at no more than 16 KiB, except the workflow receipt's 32 KiB
budget for the fixed success, failure, recovery and nested-retry matrix. The shared
64 KiB container-log ceiling remains unchanged. It retains `setup.json`, `before-first.json`,
`after-first.json`, `before-nodes.json`, `before-core.json`, `before-workflows.json`, `after-nodes.json`, `after-core.json`,
`after-workflows.json`, `before-rq2.json`, `after-rq2.json`, `before-showcase.json`,
`after-showcase.json`, `before-protocol.json`, `after-protocol.json`, producer Pod/image identities, Chainsaw's
XML report and `summary.json`. Chainsaw streams progress to the foreground; hosted runs retain it
in the public Actions job log. The directory artifact also includes any bounded
host failure sidecars written directly by the protected-log observer; the
collector does not parse or copy its foreground stderr. Older failed artifacts
may lack these records. Receipts are transported from the modules' existing stdout; they are
not inferred from Job status. The cold receipt's predecessor digest must match the retained first
node receipt. All receipts report `complete_application_gate: false`. Failure output is diagnostic;
a missing receipt, timeout, failed cleanup or partial report cannot establish a passing stage.

Each Ray generation begins with two serial, identical order-fulfillment showcases
(`item_count=3`, `work_seconds=0.05`) before generic-node probe tasks or core task smoke.
Both must publish the full 21-node, 28-edge graph, complete both three-item maps,
match API/Admin history and render in Chromium. The original 15-second terminal-flush
budget remains unchanged. The first/warm receipt records exact fixture inputs,
observation durations and both outcomes. If the first task settles but graph observation
fails, one matched warm diagnostic still runs; its success cannot erase the first failure.
An unsettled first task stops further submissions. Assertion Job logs retain that failure
before namespace cleanup.

This controls workflow ordering within each fresh Ray generation, not host cache state.
The standard acceptance profile still grants each Ray Pod 750m physical CPU and one
logical Ray CPU; it does not reproduce the more constrained local profile or establish
a startup latency guarantee. The separate 180-second first-pair budget and authoritative
2820-second outer deadline bound these cases.

For the head-placement hypothesis, the fixed `constrained-ray` diagnostic profile
requests zero logical CPUs and a 350m physical CPU limit on the head, plus one logical
CPU and a 500m physical CPU limit on the worker. Requests equal limits. Both Ray
generations use the same profile. Ray's zero-resource progress actor may still run
on the zero-logical-CPU head; positive-CPU workflow leaves cannot. This experiment
changes no collector scheduling or terminal deadline.

The constrained inventory is 2.15 CPU; memory, scratch, Pods, storage, outer hosted
capacity and cleanup are unchanged. This reproduces the Ray CPU placement boundary,
not the entire local environment: head memory remains 5 GiB, manager CPU remains 250m,
and the local combined 1.6-CPU/9-GiB quota and host-cache state are not reproduced.
`summary.json` and each first/warm receipt identify the intent, profile, and
`requested_ray_resources`. These are requested configuration, not proof of observed
Pod limits or actor placement. Receipt collection refuses a different profile.

The workflow observation stage runs twelve fixed tasks serially in each Ray generation: complex
workflow success and failure under `full`, `terminal_only` and `disabled`, followed by the recovery showcase's
two failed attempts and successful third attempt. Production settings disable the complex-workflow
demo HTTP route, so those six cases use the bounded Django task enqueue API inside the fixture.
The recovery case uses its production HTTP endpoint. A fixed 65-leaf serial chain uses
full reporting and the bounded task enqueue API. Its saved plan crosses the real 64-node limit;
the durable result must remain exactly `42`, and the graph must retain all 65 successful nodes
and 64 serial dependencies. The additional fixed 101-node case below covers the separate Admin
display ceiling. Three retry cases add finite retry success, exhausted retries, and unlimited retries
that deterministically settle after two failures. Success uses a two-leaf chain and
checks result 42, final preview/progress and exact invocation count. Each owns one
zero-CPU counter actor, uses quarter-CPU leaves and observes counter removal.
Together the twelve cases cover sixteen durable attempts. Existing case inputs and resource
limits remain unchanged. All cases use authenticated HTTP polling and
attempt-pinned workflow reads, and compare those publications with authenticated Admin graph JSON.
Temporary database-backed Admin sessions and the HTML checker's disposable user are deleted and
their absence checked after observation. The retained Admin smoke checker uses the same fixed-origin,
no-redirect transport with an explicit 1 MiB HTML response ceiling; ordinary API reads keep their
256 KiB default and graph JSON keeps its smaller limit. It checks current and archived HTML,
graph routes, terminal controls, diagnostic presentation and retained storage. Server-side
fingerprints verify that viewing these surfaces preserves stored diagnostics and task history;
neither raw diagnostics nor their fingerprints enter receipts.

The separate reservation-failure phase runs exactly one additional HTTP-enqueued task
per generation with `item_count=1`, `work_seconds=0.01`, `failure_stage=reserve_inventory`
and `failure_item=0`. It requires the fixed durable failure and exactly one failed
attempt. Its graph has 16 nodes and 21 edges: 15 successful nodes and failed map `0.5`.
The five descendants that were never submitted must be absent, including from retained
detail pages. Every one of the 16 indexed node-detail reads must match the pinned
publication. The aggregation, validation-map, failed-projector and failed-map previews
have fixed availability/value expectations. Authenticated API and Admin graphs agree;
real Admin HTML and Chromium rendering retain the original failure presentation, while
diagnostic and task-history fingerprints remain unchanged. Temporary Admin users and
sessions are removed before success; namespace teardown owns the task/history rows.

The bounded `application_showcase_failure` receipt is at most 8 KiB and must pass in
both cold-Ray generations. Missing facts, a different attempt, fabricated descendants,
partial indexed details, timeout or cleanup failure fail the stage. This is the fixed
catalogue failure showcase, not a proof of all workflows or arbitrary external effects.
It does not change the original twelve-case matrix or borrow its 600-second budget.
The Core assertion also opens the actual task's Admin Dashboard link in Chromium,
before and after cold Ray replacement. A loopback GET-only proxy forwards the
`/ray/` base path to the fixed Ray Dashboard service. The observer requires the
matching job and task identity, a successful State API response, and a rendered
`FINISHED` task detail with no JavaScript errors. Anonymous Admin access must be
denied. A URL or HTTP success alone does not pass this assertion.

The proxy permits four concurrent requests, limits each response to 8 MiB, and
does not forward browser credentials. The browser subprocess has a 60-second
deadline; its process group, temporary Admin session and proxy are removed on
success and failure. The fixed receipt contains no cookies or page contents.
This uses the existing assertion Job's resource limits and proves this fixture's
base-path integration, not arbitrary ingress, authentication or production load.

Anonymous API and graph requests must be denied. Credentials and workflow contents are omitted
from receipts. The existing Job deadlines and resource limits also bound this stage.
Terminal-only runs must have no stored detail rows, topology manifests or topology pages, including
staged data that public readers could hide. Their summaries must report zero observed/retained
counts and one publication without a manifest.
Failure receipts include only the last qualification or retained Admin checker module, function
and line, rather than raw
exception messages, response bodies or traceback contents. This identifies assertion failures
against the recorded source without exposing application diagnostics or credentials.

This stage verifies package-default terminal publication without a pilot override.
The reader checks publication identity, API/Admin agreement and independent
fixture topology and state expectations. The reused HTML checker verifies escaped and redacted
diagnostic presentation. A separate Chromium child executes the shipped Admin JavaScript, opens
current and archived graph disclosures, and checks visible terminal node/edge counts, edge geometry,
failure states and disabled/terminal-only explanations against independently verified API facts.
Failed archived attempts retain the exact verified count of unstarted nodes; terminal graphs
must not contain running nodes. A failed workflow does not imply that every planned node ran.
The integrated size cases check the visible 65-node graph and the 101-node display-limit guidance
without rendering a partial over-limit graph.
This provides scoped rendering and redaction evidence for the fixed fixture, not exhaustive
coverage of arbitrary workflows. Receipts retain `complete_workflow_gate: false`;
release acceptance requires a combined source-matched review with the applicable upgrade
and other qualification stages.
Before failure cleanup, the runner retains at most 100 projected RayCluster, Pod
and Event records per resource, capped at 64 KiB per file. Only fixed states,
reasons, counts and bounded container exit/restart fields survive; free-text
messages, resource specs and raw current/previous Pod logs are omitted. Parsed
HTTP attribution, generation phase and rq2 diagnostics use finite vocabularies.
No raw assertion stdout/stderr log is copied into the evidence artifact.
Pod and event projections retain fixed workload roles and validated event timing
so a readiness failure can be correlated with its source. Probe failure categories
are conservative: unknown messages stay unknown, and a probe failure category alone
does not establish CPU contention, database failure or a dependency regression.
Manager readiness events additionally expose a fixed `readiness_detail` only for
Warning/Unhealthy events attributed to the manager container. An exact timeout
message for the pinned Kubernetes version, fixture command and ten-second probe
is classified `exec_timeout`. A complete closed worker-readiness JSON report with
the fixed CLI failure suffix is classified `worker_report`, retaining only its
known not-ready/error status and reason. Events do not provide an independently
observed exit code. Ready reports inside failure events, changed commands or
durations, arbitrary timeout phrases, incomplete output and unknown variants
remain unknown. Candidate messages are capped at 512 bytes; raw text and command
arguments are never exported, and the 100-record/64-KiB artifact limits remain.
An event is a historical aggregate, not UID-linked proof of the later follow-up.
Even `no_live_lease` does not distinguish an expired lease from an absent,
inactive or coordinate-mismatched row. These classifications do not repair or
accept a failed qualification.


The [affected-scenario policy](../../docs/deployment/local-kuberay-gate.md) determines which product
assertions a change needs. This stage needs actual exact-source Linux execution after the full Linux
CI checkpoint. Its source tests and a schema-valid native Test do not establish that live result.

The stock Ray head reserves 5 GiB and the worker 1.5 GiB. A 3-GiB head exhausted its memory
during full dashboard startup, which loads nine subprocess modules in the pinned Ray version.
The dashboard stays enabled because its State and Job APIs are part of the qualification.
The runner checks Pod status every 15 seconds and aborts on nonzero container termination,
retaining diagnostics before cleanup. This avoids spending the readiness timeout retrying
an OOM-killed or failed container.

The owned web fixture uses one Gunicorn worker and two threads. In both resource
profiles it explicitly sets `GUNICORN_MAX_REQUESTS=0` and
`GUNICORN_MAX_REQUESTS_JITTER=0`: request-count recycling is outside this finite
acceptance workload's declared lifecycle faults. This keeps a spontaneous worker
drain from interrupting unrelated API/Admin assertions. The separate bounded
[recycle experiment](https://github.com/dariuszpanas/django-ray/pull/622) retains
actual locked-Gunicorn recycle and no-recycle coverage, including a fresh-connection
header timeout during the controlled single-worker drain.

This is a qualification-fixture decision. The sample entrypoint still defaults to
1,000 requests with jitter 100; production settings, worker/thread counts, resource
limits, request timeouts, retry rules and cleanup deadlines are unchanged. The
mechanism experiment and passing application baseline do not identify the cause of
the historical [#613 failures](https://github.com/dariuszpanas/django-ray/issues/613).
Qualification still fails on timeouts and other unexpected lifecycle failures.

For fixture debugging, push a branch without an open PR and manually dispatch the same workflow:

```sh
gh workflow run application-qualification.yml --ref "$BRANCH" -f mode=diagnostic
```

To select the constrained Ray placement diagnostic:

```sh
gh workflow run application-qualification.yml --ref "$BRANCH" \
  -f mode=diagnostic -f resource_profile=constrained-ray
```

The runner equivalents are `--validation-intent diagnostic --resource-profile constrained-ray`.
Both the workflow and runner reject constrained acceptance before allocating resources.
Automatic qualification and omitted profile inputs keep the standard acceptance profile.

This runs only the bounded application job, without waiting for or launching the full CI matrix.
The job summary and artifact explicitly identify diagnostic intent. Diagnostic execution does not
satisfy final acceptance: publish the completed candidate to its PR, where the default acceptance
mode requires current-source Linux CI before repeating the application test.

After a failed run only, the existing diagnostic collector also attempts three read-only
loopback Ray State queries from the owned head: progress actors, their snapshot tasks,
and node identity/address mapping. It retains at most 32 allowlisted scalar records per
kind and 16 KiB total, with 128 KiB response caps, three-second socket timeouts, a
12-second remote process deadline and a 15-second host command deadline. It submits
no actor or task RPC, performs no warm-up, and cannot prevent namespace cleanup.
The diagnostic receipt reports missing/truncated observations explicitly; State data
can lag, and these records do not measure actor-constructor or worker-startup duration.
Runtime environments, arguments, error bodies and unapproved State fields are omitted.


Disabled-policy cases check authenticated API and Admin JSON responses for an
explicit disabled explanation without a run identity, publication or graph.
Database checks reject current/archived progress and staged run storage. These
cases verify the disabled policy separately from full reporting. Full-policy
cases use the package-default terminal publisher without a pilot setting;
passing receipts must match the candidate source before establishing acceptance.


The fixed Admin display-limit case runs 101 serial identity leaves with scalar result `42`.
It crosses the actual 100-node Admin ceiling, independently of the 64-node saved-plan limit.
Authenticated attempt/publication-pinned API reads must retain all 101 successful nodes,
101 details and 100 serial dependencies. Each collection permits at most four pages of 100
records, with a 256 KiB response ceiling; repeated cursors, missing records and changed
identities fail the stage. Admin must return a bounded empty `LIMIT_EXCEEDED` graph and
point to the paginated API. Anonymous access is denied and diagnostic/history fingerprints
must remain unchanged. The separate browser observer checks the rendered limit guidance and
absence of graph nodes. It uses existing deadlines and capacity, no new submission
route, and full reporting through the package-default terminal publisher. The exact
callable is added to the sample's authenticated read policy. The fixture definition
alone does not establish passing rendered or deployed qualification.

### Source-derived worker image observation

After both Ray generations and reporting assertions, the runner checks the current
application image with the shipped sync and ML queue manager commands. It renders
the KubeRay Kind overlay, validates the exact CLI, queue and concurrency of 20,
stops the original manager and verifies its observed scale-down, zero ReplicaSet
replicas and Pod absence, then runs one temporary Deployment at a time. Each
manager must become lease-ready, execute the
fixed `add_numbers(2, 3)` task exactly once, leave an older `low-priority` queue
sentinel unchanged, and release its lease on graceful shutdown. The actual PID1
command, pinned image identity, zero restarts, result `5`, generation, attempt and
claiming lease are checked. The claim must belong to PID1 of the exact captured
Pod name; its UID, image and restart count are rechecked before success-path
deletion, which also verifies the created Deployment UID. This is simple task
consumption on the ML queue; it does not qualify a machine-learning framework,
model lifecycle or custom topology.

The fixed receipt lists every deployment substitution: disposable namespace and
database secrets, qualification settings and offline recovery archive, existing
Ray Client DNS/auth profile, the existing manager's 250m CPU/768 MiB footprint,
volume/security/lease-readiness profile, and fixture-only SIGINT shutdown of the
owned Pod's PID1. No additional Ray process or concurrent manager is admitted.
Source CLI/queue/concurrency are preserved; the complete production manifests are
not deployed unchanged. Negative Ray authentication is a separate observation.

The worker phase starts only after the separate cold Chainsaw workload succeeds.
It owns a fresh 180-second total from one monotonic start for original-manager
quiescence and both sequential worker lifecycles. Observation ends at
135 seconds, reserving the final 45 seconds for cleanup. Temporary controller/Pod
removal allows at most five seconds per mode; database cleanup uses a command
capped at ten seconds and preserves the final 25 seconds for original manager
restoration. Cleanup can start earlier after a failure, and restoration may use
unused time from earlier cleanup steps. Every step remains inside the original
180-second deadline. It runs separately from Chainsaw's 2820-second catalogue window.
The hosted step is 55 minutes: 2820 seconds for Chainsaw, 180 for
worker images and the previous 300-second aggregate allowance for command
startup, receipt collection and namespace cleanup. Those operations retain
their existing individual timeouts; the allowance is not a promise that every
timeout can be exhausted sequentially. The whole job remains capped at 75
minutes. Existing outer collection and namespace deletion run after either
phase fails. Individual remote database observers have an eight-second alarm,
two-second statement and one-second lock timeouts; host commands are at most ten
seconds. Receipts are at most 4 KiB, local ownership state 128 KiB, and history
hash queries return at most 512 rows per fixed task/attempt/lease table, without
returning persisted payload columns.

Ownership is saved before the enqueue transaction commits, so an interrupted
response cannot leave unidentified tasks. Full persisted history hashes are
compared after owned task/attempt/lease removal. Assertion or row-cleanup failure
still attempts restoration, provided foreground deletion and subsequent checks
prove the temporary Deployment, ReplicaSets and Pods absent. Unconfirmed removal
fails closed instead of starting an overlapping manager; outer owned namespace
cleanup remains the fallback. Passing simulated unit observers are not deployed
evidence: acceptance requires the `worker_images` summary from the exact current
image, along with the existing cold-generation receipts and namespace cleanup.

An unready container in `ContainerCreating` may not have an image ID yet. The
observer waits within the existing deadline only while its declared image
matches and its integer restart count is zero; a running ready container still
requires the matching observed digest. A failure leaves `worker_images` null
and records a separate `worker_images_failure` object with a fixed initiating
phase/category, separate cleanup failure, and independently confirmed temporary
removal, row removal, history preservation and original-manager restoration.
False cleanup flags mean unconfirmed, not proof that cleanup was attempted or
failed. Timeout failures also identify the local bound that fired: the deadline
before launching a command, the polling reserve, a command limited by remaining
time, the command's own cap, or the total phase deadline. Unknown timeout sources
remain explicit; this attribution does not identify a remote root cause. The
first failure and its timeout source survive later cleanup errors. These diagnostics
contain no raw exception, command, response, payload or Pod identity, and never
substitute for the complete success receipt.

### Rendered Admin observation

The qualification image adds Playwright 1.63.0 and its matching Chromium headless shell in an
isolated environment; application dependencies and the production base image are unchanged.
Each fixed workflow case launches one browser subprocess with a 45-second wall deadline and a
fresh process group. The caller kills any remaining descendants and waits for its child on success
or failure. The existing assertion Pod CPU, memory, scratch and overall deadline remain enforced;
this does not claim a separate browser memory limit. Browser traffic is limited to same-origin GET
requests. The disposable session travels on private stdin and is removed by the existing session
owner. No screenshots, DOM text, cookies, raw browser errors or arbitrary URLs enter receipts.
Server-side fingerprints additionally verify that the browser visit preserves protected diagnostics
and task history. The result retains policy, attempt count, preservation and a zero-JavaScript-error
assertion only. A browser
failure may retain the verifier source line, without exception text. A failed browser
observation fails qualification; unit mocks are not rendered evidence. Source-matched Linux execution
before and after cold Ray is required before this workload can be accepted.

### Ordinary result presentation

Each generation's core receipt also requires `ordinary_results`: fixed scalar
observations of the ordinary current execution and archived attempt Admin pages,
plus the current execution API detail. There is no archived-result API endpoint;
the archived result assertion uses the ordinary TaskAttempt Admin page.

This presentation-only fixture creates one terminal execution and one archived
attempt with distinct fixed JSON markup and sensitive-key canaries. It does not
enqueue these synthetic rows or claim that a task produced their values. Actual
HTTP reads must deny anonymous access, render the markup as escaped inert text,
redact the sensitive key and value, and leave every original row field unchanged.
The HTML parser rejects injected canary elements, attributes or executable text;
this is server-HTML evidence, not a claim that a browser executed the page. The
fixture removes only its opaque-identity-bound rows and temporary session, even
after an observation fails; missing cleanup prevents a passing receipt.

The fixed six HTTP reads use five-second-or-shorter request timeouts and a
30-second observation budget. HTML is capped at 1 MiB per response, API data at
64 KiB, and the existing core receipt remains capped at 16 KiB. ORM work and
cleanup are not independently interrupted by the observation budget; the existing
generation supervisor still enforces the unchanged 600-second process budget.
The collector requires every typed result-presentation field in both generations.
These assertions cover the ordinary-result presentation part of #455; they do
not complete the whole application catalogue or the release checklist in #372.
