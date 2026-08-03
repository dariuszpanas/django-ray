# Compiled Graph Compatibility

Ray Compiled Graph is not currently an enabled django-ray execution strategy. This page
defines the fail-closed compatibility contract and the evidence required before a
future strategy may call Ray's beta native APIs.

Compiled Graph is an execution strategy for a validated static actor region in an
[effective workflow plan](workflow-plans.md). It is not a Django task type, and it does
not make a data-dependent workflow static. When this policy rejects compilation,
ordinary local and dynamic Ray task execution remain available.

The production target for native enablement is Linux x86_64 on Kubernetes/KubeRay.
Windows remains useful for local ordinary execution and tests, but delayed Ray Windows
builds or a failing Windows Compiled Graph probe do not block Linux/Kubernetes
groundwork or promotion evidence. Every platform still fails closed unless its exact
capability tuple is independently verified.

## Current support state

Policy version 3 has **no verified native capability rows**. The rows below are canary
candidates, not a support claim. The required isolated canaries succeeded for both
current candidates, and the 2026-08-02 policy review retained the empty verified set:
the hosted runners did not provide the exact container, immutable deployment,
shared-memory, and object-store profiles required by policy version 3. An incomplete
candidate returns `INCOMPLETE_CAPABILITY_CONTEXT`; a complete but unpromoted exact
tuple returns `CANDIDATE_REQUIRES_SMOKE`.

Those hosted observations are historical provenance. Repository-managed public
workflows no longer invoke native Compiled Graph APIs; current native validation is
performed only through the guarded local KubeRay pilot owned by issue #102. Ordinary
CI still covers the default-off decision policy, probe parsing and containment, and
the pilot evidence validator without enabling native execution.

| Ray | Python | OS and architecture | Status | Why it is listed |
|---|---|---|---|---|
| 2.56.0 | 3.12 | Linux x86_64 | Candidate | Package security floor, current lock, and initial Windows investigation |
| 2.56.1 | 3.12 | Linux x86_64 | Candidate | Latest PyPI and Ray release reviewed on 2026-07-19 |

The versions are exact. A new patch, minor, prerelease, or nightly is rejected until it
is deliberately added as a candidate and then independently verified. Python 3.13 and
3.14 remain supported for ordinary django-ray execution but are not Compiled Graph
candidates in policy version 3.

Version sources:

- [Ray 2.56.0 on PyPI](https://pypi.org/project/ray/2.56.0/)
- [Ray 2.56.1 on PyPI](https://pypi.org/project/ray/2.56.1/) and the
  [Ray 2.56.1 release](https://github.com/ray-project/ray/releases/tag/ray-2.56.1)

The general django-ray dependency is `ray[default]>=2.56.0`. That broader range
does not imply Compiled Graph eligibility.

Ray 2.53.0 remains named only in the retained 2026-07-19 and 2026-07-20
investigation records below. It predates the current package security floor and is no
longer a candidate or supported installation target; preserving the old observation
does not make it eligible for a future capability promotion.

### Pinned Linux/KubeRay pilot profile

Issue #102 defines one promotion-grade evidence profile named
`django-ray-cgraph-kuberay-cpu-v1`. It is intentionally narrower than ordinary
django-ray support:

- Linux `amd64`, Python 3.12.12, and `ray[cgraph]` 2.56.0;
- the official `rayproject/ray:2.56.0-py312` base pinned at
  `sha256:2951c07de396a8b746f9c678b52c6e2282e614e00f80e6846a9ccd12945ae6b0`;
- KubeRay operator 1.6.2 pinned at
  `sha256:f9eb07d0d3384554763d739f0eed27aa5d0c2ed4c727ceb075930c1f3f4b9f47`;
- Docker CLI context `desktop-linux`, its local Docker Desktop Linux-engine endpoint,
  and engine 29.4.3 on the pinned Linux/amd64 kernel;
- Kubernetes 1.34.1 on the `docker-desktop` Linux/amd64 node, pinned to its
  Docker runtime and `6.6.87.2-microsoft-standard-WSL2` kernel for this evidence row;
- Ubuntu 22.04.5 with glibc 2.35 inside the pinned image, with exact Python ABI
  and `platform.platform()` identities checked before accepting native evidence;
- `cupy-cuda12x` 13.4.0 and its required `fastrlock` 0.8.3 dependency are both
  installed or reinstalled without dependency resolution and observed exactly;
- one zero-CPU Ray head and two fixed one-CPU workers, with autoscaling disabled;
- explicit OS, architecture, and hostname node selectors for every Ray pod;
- 512 MiB memory-backed `/dev/shm` and a 256 MiB Ray object store on every Ray pod;
- direct Ray Core submission only, for a cluster-side driver control and a nested
  owner task with `max_retries=0`;
- a deliberately terminated hard-timeout child process group, three ordered
  invocations, an application exception, a one-shot result timeout, explicit result
  discard/consumption, actor termination, and graph teardown.

The Dockerfile, Dockerfile-specific ignore policy, profile, and RayCluster template live
under `k8s/pilots/compiled-graph/`. The runner captures one clean full Git revision, validates a
bounded regular-file inventory, and safely materializes a tracked-only `git archive` build
context. The Dockerfile-specific policy remains a required deny-by-default second boundary, so
ignored credentials, generated source artifacts, `.vault`, Git metadata, tests, docs, and unrelated
Kubernetes assets are not sent to the daemon. The runner verifies the pinned local Docker context
before it builds. Configuration and build-policy identities use a strict UTF-8 source-text
contract that maps clean-checkout CRLF pairs to the Git archive's LF bytes while preserving every
other byte; a BOM, NUL byte, or bare carriage return fails closed. The same commit therefore keeps
one identity across Windows and Linux checkouts. The runner refuses a dirty source tree, a changed
Kubernetes context, any namespace except `django-ray-cgraph-pilot`, a changed KubeRay operator
identity, a mutable/restarted Ray pod image, or an unowned cleanup target. The operator check
requires the exact Deployment, ReplicaSet, and pod names and UIDs; their controller ownership;
the sole spec/status container; the configured image and running digest; linked Ready status; and
an exact nonnegative restart count. The restart count is retained as observational evidence, not
required to be zero, and the runner never rolls or otherwise mutates the shared operator. The full
observation is compared for exact equality immediately before RayCluster creation, after the
operator reports the pilot pods ready, and after the final runtime capture. An operator rollout,
container restart, readiness change, or controller/container identity drift at any bracket
invalidates the run. The runner builds a source-labelled immutable local image, calculates
configuration and rendered manifest identities, and verifies the sole regular container, the pinned
KubeRay worker init-container inventory, running image IDs, exact restart counts, identity
environment, and every profile-declared effective Ray start parameter before native execution. The
profile distinguishes ordinary valued parameters
from KubeRay's valueless `--disable-usage-stats` true switch. Pod evidence retains each
parameter's sanitized lexical form, lexical value, and effective semantic value, so a
valued, duplicated, missing, or changed switch cannot pass as the pinned form. Namespace
discovery uses explicit structured
not-found semantics; an API failure cannot fall through to creation, and every existing
namespace is refused. A successful namespace create response supplies a cryptographically random
run-token label and immutable UID lease. The RayCluster is also create-only: its create-response UID,
the namespace UID, and the run token form a second lease. Those values are rendered into RayCluster
and pod labels, annotations, and identity environment, and each pod must have the exact RayCluster
controller reference. Namespace and RayCluster leases are checked before and after pod reads and
exec boundaries. Cleanup first verifies the exact live namespace lease, deletes only through
name/profile/run-token selectors, and fails if either the leased UID or a replacement namespace
remains.

Kubernetes does not offer this runner a namespace-UID precondition on namespaced create, and
`kubectl delete namespace` exposes neither a UID nor resource-version precondition. The pre/post
lease checks, create-only RayCluster, embedded identities, selector-bound delete, and absence check
make any replacement fail evidence collection; they do not make those API calls atomic. External
namespace deletion and recreation inside a check/call window is outside the supported pilot
coordination contract and is not claimed safe from a scoped create reaching the replacement.

Before creating the RayCluster, the runner starts the same immutable local image ID in a
network-isolated, read-only one-shot container with a physically smaller 256 MiB
`/dev/shm`. It requires the complete policy identity to differ only on that declared and
observed resource. A pilot-specific admission layer proves that the tracked baseline is
admitted, then rejects the changed identity as `PILOT_PROFILE_MISMATCH` before the
hardened probe or any native command can be invoked. Both policy decisions and both
admission outcomes are retained. This is the required physical near-neighbor rejection,
not a claim that a changed KubeRay profile was executed natively.

From a clean commit on the supported local Docker Desktop/KubeRay setup, run:

```powershell
uv run python scripts/kuberay_compiled_graph_pilot.py run `
  --context docker-desktop
```

The active profile's `django-ray` dependency must equal the source package version in
`pyproject.toml`, and the image build derives the same expectation from its archived
project metadata. A source version change therefore fails closed until the tracked
profile is updated and creates a new profile identity. Retained evidence embeds its
original dependency profile and remains immutable. Its self-contained record is
validated against that embedded profile, while the fresh-evidence writer separately
requires the current tracked profile and configuration. Updating the active profile
therefore does not rewrite or reclassify an older record.

The reference operator installation uses the versioned upstream chart and tag; the
runner then verifies the running image digest before it creates the pilot namespace:

```powershell
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update kuberay
helm upgrade --install kuberay-operator kuberay/kuberay-operator `
  --namespace kuberay-system --create-namespace `
  --version 1.6.2 `
  --set image.repository=quay.io/kuberay/operator `
  --set image.tag=v1.6.2 `
  --wait
```

The command starts each subprocess inside an operating-system process-tree boundary, actively caps
and concurrently drains its streams, bounds post-termination waits, and terminates descendants that
retain inherited pipes. It accepts exactly one complete JSON document, prints one bounded allowlisted
record, and deletes only its dedicated pilot namespace by default. Retained subprocess records contain structured status,
exit, timing, decision, and private control-record observations; they omit stdout,
stderr, tracebacks, and arbitrary process errors.
The runner does not read or print Kubernetes Secrets. Retain a successful record under
`docs/investigations/` and open a
separate capability promotion review; the runner does not edit `_VERIFIED_CAPABILITIES`,
enable product execution, or turn a candidate-native probe into support by itself. A
changed image, Ray/Python dependency, Docker context/engine/build-context policy,
KubeRay operator, kernel/runtime, `/dev/shm`, object store, topology, or submission
transport requires a new profile identity and evidence.

The hard wall-clock containment self-test deliberately kills and reaps a Linux process
group. Each direct and nested native suite separately exercises a one-shot Compiled
Graph result timeout followed by explicit teardown. Successful cleanup evidence requires
every named graph actor to reach Ray `DEAD`, no named nested-owner task to remain active,
no pilot child process in any Ray pod, exact restoration of hashed `/dev/shm` entry
identities, and exact restoration of the Ray object identity digest after global GC.

Ray 2.56.0 does not currently satisfy that cleanup boundary on the pinned profile.
Both owner topologies can complete while Experimental MutableObject allocations and
their POSIX semaphore entries remain in `/dev/shm`. The project tracks that blocker in
[django-ray issue #154](https://github.com/dariuszpanas/django-ray/issues/154) and the
upstream reclamation work in
[ray-project/ray issue #43836](https://github.com/ray-project/ray/issues/43836) and
[ray-project/ray issue #59127](https://github.com/ray-project/ray/issues/59127).
The pilot observes immediately, then waits `5`, `15`, and `30` more seconds (50 seconds
total) before classifying this outcome. A retained `status: blocked` record proves the
failure; it is not successful #102 evidence, is never promotion-eligible, and the
command exits nonzero after deleting the dedicated namespace. Classification requires
stable, fully paired `sem.hdr`/`sem.obj` fingerprints across the complete observation
window; aggregate kind/pair counts and digests are retained, never raw names. The final
proof also refetches exact pod UID, regular/init-container, image, restart, identity-
environment, and Ray-start-parameter observations before a
fresh actor/task/object-state inspection. Evidence persistence is rejected unless
the create-response namespace UID lease and namespace absence are verified, and it cannot
be combined with `--keep-cluster`.
Do not unlink Ray-owned semaphores,
restart pods, destroy the cluster early, increase `/dev/shm`, or relax exact restoration
to manufacture a passing result. Re-run the same profile against the first upstream fix,
and create a promotion review only after teardown restores the state without pod or
cluster destruction.

The retained record's source revision, local image ID, profile ID, and configuration ID
are the comparison tuple for #97, #87, and #88. Those pilots must verify that the
still-loaded local image matches that exact ID. If the image is rebuilt or no longer
exists, its new ID is a revalidation trigger rather than an interchangeable replacement;
run this pilot again and retain the new evidence before comparing results. The project does not claim
that independent Docker builds are bit-for-bit identical or publish this candidate image
as a supported registry artifact.

Ray still classifies Compiled Graph as beta and describes its benefit for workloads that
repeatedly execute the same static graph. The pilot therefore establishes an exact
platform capability only; issues #87 and #88 retain workload benchmarking and the final
adoption decision. See the upstream
[Compiled Graph overview](https://docs.ray.io/en/latest/ray-core/compiled-graph/ray-compiled-graph.html)
and [RayCluster configuration](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/config.html).

## Topology decisions

The topology names identify the process that compiles the graph and must perform every
invocation. They do not describe the actor tasks inside the graph. The ownership and
reuse design is defined separately in
[ADR-0002](design/adr-0002-compiled-session-ownership.md); this page decides whether an
exact runtime tuple may attempt that design.

| Topology | Policy version 3 | Contract |
|---|---|---|
| Direct Ray Core driver | Candidate | A directly connected, non-Ray-Client driver compiles, invokes, drains, and tears down the graph in the same process. This is the diagnostic control topology. |
| Local nested Ray Core owner | Candidate | A directly connected Ray Core driver submits one outer task, which owns compilation and all invocations for the durable run. This is the production-intended intra-task pilot topology; the smoke owner has retries disabled to avoid multiplying a native crash. |
| Ray Job driver | Candidate | Compilation and invocation must occur in the submitted Ray Job driver, not in the Django worker or Job Submission client. Driver drain must finish before job exit. |
| Ray Client driver | Rejected | Compilation in the Ray Client driver itself would place the compiler owner across the proxy boundary. No such owner contract is validated; use Ray Jobs or dynamic Ray tasks. |
| Nested cluster-side owner submitted through Ray Client | Deferred/unverified | The Ray Client process submits an ordinary outer task, but compilation and every invocation occur in that cluster-side task. Policy version 3 models this as a separate `ray-client` submission transport and rejects it; it cannot inherit evidence from a `direct-ray-core` nested owner. |
| Windows policy gate | Rejected | Every Windows tuple fails closed before Ray import. Retained evidence below covers only the three release candidates with direct-driver and nested-task owners; it does not claim every Windows version or topology crashes. |

Ray documents that only the compiling process may invoke a graph. It also documents
that Compiled Graph currently supports actor tasks only, that an actor can participate
in one graph at a time, and that the graph must be explicitly torn down before actor
reuse. See the upstream [quickstart](https://docs.ray.io/en/latest/ray-core/compiled-graph/quickstart.html)
and [troubleshooting limitations](https://docs.ray.io/en/latest/ray-core/compiled-graph/troubleshooting.html).

Ray Job and nested-task rows are separate evidence tuples even when they eventually
call the same Python helper. A passing direct-driver probe does not verify either one.
Likewise, a nested owner submitted through Ray Client needs its own submission-path
evidence and its own exact capability row. Submission paths never share a verified
nested-task row.

## Transport and dependency policy

| Transport | Policy version 3 | Dependencies and limits |
|---|---|---|
| CPU shared memory | Candidate | The django-ray runtime keeps its normal `ray[default]` dependency. Native canaries install `ray[cgraph]` as recommended by Ray so a missing Compiled Graph extra cannot produce a false compatibility result. No GPU package becomes an application dependency. |
| GPU/NCCL | Rejected | Requires a separately tested `ray[cgraph]`, CuPy package matching the CUDA major, NVIDIA driver/runtime, NCCL, GPU topology, tensor schema, and peer-to-peer transport matrix. |

Ray 2.56's `cgraph` extra currently selects `cupy-cuda12x` outside macOS, and that
distribution requires `fastrlock`. The pinned pilot therefore records
`cupy-cuda12x==13.4.0` and `fastrlock==0.8.3` independently so a changed base image cannot
silently omit or substitute the transitive package. These remain canary or future
GPU-strategy dependencies, not mandatory django-ray dependencies. The old `ray[adag]`
spelling is not used for the candidate versions. Current tensor paths must use
`with_tensor_transport`; older `with_type_hint` examples are not the policy.

GPU support must be introduced in its own policy revision. Ray currently documents
peer-to-peer GPU transfers and says broader collective support is still forthcoming.
No CUDA or NCCL version is supported merely because imports succeed.

## Fail-closed capability adapter

`django_ray.runtime.compiled_graph` evaluates only JSON-safe runtime facts. It does not
import Ray and never creates or persists a `DAGNode`, `CompiledDAG`, `CompiledDAGRef`,
actor handle, channel, or object reference.

```python
from django_ray.runtime.compiled_graph import (
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    require_compiled_graph_support,
)

decision = require_compiled_graph_support(
    CompiledGraphTopology.NESTED_RAY_TASK,
    submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
)
```

Today this raises `CompiledGraphUnsupportedError` because no exact tuple is verified.
A future strategy must call this guard before importing or invoking
`experimental_compile()`. An explicit strategy request fails before actor creation or
side effects; automatic selection may record the rejection and continue with bounded
dynamic Ray tasks.

The capability record contains:

- compatibility schema and policy versions;
- exact Ray and Python versions, implementation and runtime ABI;
- dependency, kernel/platform, libc, specific container, immutable deployment/image,
  shared-memory, and object-store profiles;
- compiler-owner topology, submission transport, and channel transport;
- separate `candidate`, `verified`, and `eligible` flags;
- a stable reason and workflow-plan rejection code; and
- a capability-set identifier only for a recognized candidate or verified tuple.

Changing a field changes compatibility identity. Missing context also fails closed;
the coarse candidate table remains useful for choosing canary versions but can never
make a runtime eligible. Beta Ray objects are never part of the effective-plan
fingerprint or durable diagnostics.

Every identity field is limited to 1,024 characters. An oversized value produces
`INVALID_RUNTIME_IDENTITY` and cannot match a verified row. Serialized decisions replace
the value with its original character count and SHA-256 digest, so even malformed
programmatic input or an oversized profile environment variable cannot create an
unbounded capability or probe record.

The runtime does not guess promotable infrastructure identity. Blank values, the
sentinels `unknown`, `unavailable`, and `unresolved`, and generic container labels such
as `host`, `container`, or `docker` produce `INCOMPLETE_CAPABILITY_CONTEXT`. Before a
tuple can be eligible, its operator or evidence harness must supply all four explicit,
non-secret profiles:

- `DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE` identifies the specific container or
  host build rather than its generic runtime family;
- `DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE` is an immutable `sha256:<64 hex>`
  deployment/image digest (an optional stable name may precede it as
  `name@sha256:<64 hex>`);
- `DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE` records the configured shared-memory
  transport, mount, and capacity; and
- `DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE` records the configured Ray object
  store capacity and spill policy.

Automatically observing `/dev/shm`, a Docker marker, or a host label is useful
diagnostic context but cannot prove an exact deployable capability. A candidate canary
may still run with `--candidate-native` to gather missing evidence; its success does
not make the incomplete identity eligible or promotable.

## Isolated smoke probe

The probe always consults the adapter first. A normal invocation on Windows, a Ray
Client driver, GPU, an unknown version, or any other unsupported tuple returns
`unsupported_guard` without spawning a child or importing Ray.

Candidate rows require an explicit canary opt-in:

```bash
python -m django_ray.runtime.compiled_graph_probe \
  --topology nested-ray-task \
  --submission-transport direct-ray-core \
  --candidate-native \
  --require-success \
  --timeout-seconds 90
```

The child creates one actor, compiles one input/echo graph, executes it once, consumes
the result, and tears the graph down. The nested-task probe uses `max_retries=0` for its
compiler owner. The parent drains stdout and stderr continuously into separate
fixed-size human-log tail buffers. The structured child result uses a private per-run
control file with an independent 512 KiB bound sized for worst-case JSON escaping of
both bounded error fields, so log pressure cannot share or truncate the record. The
parent enforces a hard wall-clock limit and owns the child process group or Windows Job
Object. The native child waits on a private start gate; on Windows the parent assigns
the child to the Job Object before releasing that gate, so Ray cannot create descendants
in the pre-assignment window. The parent terminates remaining descendants after every
outcome, including a clean return or abrupt root crash, then emits one bounded,
versioned JSON outcome.

| Outcome | Meaning |
|---|---|
| `success` | The exact native smoke returned and teardown completed. |
| `unsupported_guard` | Policy rejected the tuple before native execution. |
| `python_failure` | The child caught and serialized a normal Python exception. |
| `timeout` | The parent killed the process tree after the wall-clock bound. |
| `signal` | The probe process ended from a POSIX signal. |
| `native_crash` | The process exited without a valid record, returned a native exit code, or Ray reported a crashed compiler worker. |

### Unsafe investigation mode

There is a deliberately awkward bypass for reproducing a native failure on an
unsupported runtime. It requires both `--unsafe-native` and the environment variable
`DJANGO_RAY_ALLOW_UNSAFE_COMPILED_GRAPH_PROBE=1`. It cannot bypass an unsupported
owner topology, submission transport, or channel transport. Do not use it in an
application process or ordinary test run.

### Retained platform evidence

The 2026-07-19 evidence pass used CPU shared-memory transport for three exact Ray
releases, including the now historical pre-floor 2.53.0 observation. Linux jobs ran on
`Linux-6.17.0-1020-azure-x86_64-with-glibc2.39`, with CPython 3.12.13 and
`ray[cgraph]`, in GitHub Actions run `29714241117`. The Windows investigation used
Windows 11 build 26200 AMD64, CPython 3.12.12, the synced virtual-environment
interpreter, and a hard 60-second parent bound.

| Ray | Linux direct owner | Linux local nested owner | Ray Client-submitted nested owner | Windows direct owner | Windows local nested owner |
|---|---|---|---|---|---|
| 2.53.0 | Not run | `success` | Not run | `native_crash`; exit `0xC0000005` | `native_crash`; `WorkerCrashedError` |
| 2.56.0 | Not run | `success` | Not run | `native_crash`; exit `0xC0000005` | `native_crash`; `WorkerCrashedError` |
| 2.56.1 | Not run | `success` | Not run | `native_crash`; exit `0xC0000005` | `native_crash`; `WorkerCrashedError` |

The matrix records observations, not support decisions. In particular, the Linux
results cover a local nested owner submitted by a directly connected Ray Core driver.
They do not cover direct-driver compilation, a nested owner submitted through Ray
Client, or a Ray Job driver, and policy version 2 remains fail-closed with no verified
rows. The Windows failures reached
`ray.experimental.channel.shared_memory_channel.ensure_registered_as_writer` during
`experimental_compile()`; they do not establish that every Windows tuple crashes.

The [machine-readable evidence record](investigations/compiled-graph-platform-evidence-2026-07-19.json),
[standalone Ray reproducer](investigations/reproduce_ray_compiled_graph_windows.py),
and [draft upstream report](investigations/ray-compiled-graph-windows-report-draft.md)
retain the evidence without filing or claiming an upstream diagnosis. A read-only
search of `ray-project/ray` on 2026-07-19 found no matching issue.

This retained record is explicitly incomplete: the original Windows pass did not keep
raw stdout/stderr, an exact dependency inventory, or the policy-v2 Python ABI, libc,
specific container, immutable deployment/image, shared-memory, and object-store
profiles. It cannot support a Windows capability promotion or an upstream report by
itself. Issue #100 owns the fresh Windows release/nightly evidence and upstream report
and remains an open, non-blocking upstream follow-up. The Linux/Kubernetes fail-closed
infrastructure delivered by issue #86 / PR #92 does not wait for it; fresh Windows or
nightly evidence is not required before guarded local KubeRay work continues.

No nightly wheel was run. Nightly wheel URLs and ABIs rotate, while this pass was
designed to classify the three exact published candidates. A nightly result would be
a separate tuple: it would neither verify these releases nor safely widen the policy.
This is not evidence that nightly passes or fails. If issue #100 tests a nightly, its
record must name the resolved wheel identity rather than treating it as a release
neighbor; a nightly run is not a Linux/Kubernetes merge gate.

### Reviewed Linux promotion decision

The required PR #92 canaries ran the hardened policy-v2 probe again in GitHub Actions
run [`29759326381`](https://github.com/dariuszpanas/django-ray/actions/runs/29759326381).
Ray 2.53.0, 2.56.0, and 2.56.1 each completed the local nested owner smoke and verified
its echo result. Those successes are discovery evidence, not permission to compile.
The 2.53.0 row is additionally ineligible because it is below the current package
security floor.
Every decision remained ineligible because the generic hosted runner reported `host`
as its container profile and left the immutable deployment, shared-memory, and object-
store profiles unresolved.

The [2026-08-02 policy review](investigations/compiled-graph-capability-review-2026-08-02.json)
records `no_promotion` and an empty verified row list for policy version 3 after the
package security floor removed Ray 2.53.0 from the candidate set. It retains the current
2.56.0 and 2.56.1 observations from the
[2026-07-20 capability review](investigations/compiled-graph-capability-review-2026-07-20.json),
including their exact workflow/head/tested-tree identities, mutable runner-image
context, job and artifact IDs, archive digests, and per-file hashes and sizes. The
hosted runner was requested through `ubuntu-latest`; its `ubuntu-24.04` image is not an
immutable production KubeRay image, so no exact production tuple can be inferred from
it. The earlier policy-v2 review remains immutable historical provenance.

GitHub expires these candidate artifacts on 2026-10-18. Expiry does not invalidate the
safe no-promotion decision: an unavailable discovery artifact cannot make an empty
verified set less fail-closed. It does mean the evidence must be collected again before
any later promotion. Issue #102 owns a pinned Linux/KubeRay pilot with all four explicit
policy profiles and immutable image identity.

### Retained Linux/KubeRay blocker evidence

The [2026-07-21 retained KubeRay record](investigations/compiled-graph-kuberay-blocked-2026-07-21.json)
is the canonical 164,686-byte blocked record (SHA-256
`972d9d9ad3f39f2e97ebc9bd491cd5222a69cb39f01ec7c28578b7ae0976d702`). It binds source
`e03208f7b3a0a1eb6a54611d4bc43efb17dddf7b`, image ID
`sha256:b9ace4b8cc89f586442f4c83cacd8a2bb8875ea473f6d135b42d6508bb81ab7b`, profile ID
`sha256:0d95a99dfd7fe8c4bf8258d937e034bb917fb4fb7bd9079961fed63bc551ae99`, configuration ID
`sha256:cad1f9c5633873bec6bb53f9cea8f40175213fcc19b6b4196553eec3014c332a`, and rendered
manifest ID `sha256:3ddecb07cbacb6b8edcd171020854547d651ec3111e0fe256fc093a8f3db818c`.

Both the direct driver and retry-disabled nested owner completed their native suites. A
physically halved shared-memory near neighbor changed only that profile and was rejected
before a child or native execution started. After the final 50-second cleanup bracket,
the record proves zero active pilot actors and tasks, zero object-store objects and bytes,
and zero pilot child processes. The only residue was 22 stable, fully paired Ray
mutable-object semaphore pairs, with no unpaired or unrelated shared-memory entries.
The selector-bound pilot namespace was subsequently proven absent, and no unrelated
namespace was touched.

This is candidate-native failure evidence for the reclamation blocker tracked in
[issue #154](https://github.com/dariuszpanas/django-ray/issues/154). It does not satisfy
issue #102, make product execution eligible, trigger a capability-promotion review, or
add a verified capability row.

## Evidence promotion and maintenance

A candidate becomes verified only through a reviewed policy change with all of the
following evidence:

1. The exact Ray release exists in an official Ray release or PyPI record.
2. A clean Linux x86_64/Python environment installs the exact `ray[cgraph]` release.
3. The subprocess probe succeeds for the exact production-intended owner topology,
   submission transport, and channel transport. Control-topology or local-submission
   success cannot substitute for it.
4. The artifact retains the probe JSON, exact Python patch/implementation/ABI,
   Ray/package versions, platform/kernel/libc, specific container, immutable
   deployment/image, explicit shared-memory and object-store profiles, exit code, and
   reviewed redacted bounded stdout/stderr. A timeout or crash is evidence of failure,
   not permission to fall back after compilation starts.
   The pinned KubeRay pilot invokes its in-container interpreter directly for
   environment capture and the native probe. Do not wrap those in-container commands
   in `uv run`: Ray workers inherit the wrapper context and may resolve their worker
   directory as a separate uv project.
5. Unit tests prove exact-tuple matching and rejection of every neighboring unverified
   tuple. Dynamic workflow tests remain green without selecting Compiled Graph.
6. A dated machine-readable review records provenance, expiry, revalidation, and
   quarantine policy before a reviewer adds only the passing exact capability tuple to
   the verified set. No row may inherit an image, deployment, shared-memory, or
   object-store profile from a near neighbor. Changes to compatibility meaning bump the
   policy version. Issue #99 completed the first review with `no_promotion`; issue #102
   owns the missing promotion-grade KubeRay evidence.

Public CI exercises only the hermetic policy, guard, parsing, containment, lifecycle,
and evidence-validation contracts. Native candidate execution is an explicit guarded
local KubeRay action under issue #102. An unknown stable, upstream, or nightly build
requires a separately reviewed immutable local profile; a future self-hosted or EKS
path likewise requires its own issue and may not inherit support from generic hosted
runner observations.

Review the capability candidate table and pinned KubeRay pilot profile whenever Ray,
Python, operating-system images, channel APIs, `ray[cgraph]` dependencies, immutable
deployment/image identity, shared-memory or object-store configuration, or the workflow
owner contract changes. Never widen a range from version ordering alone.

Release validation loads the newest dated capability-review record and compares its
reviewed capability identities exactly with the runtime verified set. A future verified
row must reference retained evidence IDs, carry matching review and revalidation dates,
use artifacts that have not expired, and remain outside quarantine. A timeout, native
crash, mismatched result, changed identity dimension, or relevant upstream regression
quarantines the exact row; the dynamic strategy remains available while a fresh review
is prepared. Artifact expiry is deliberately non-blocking only while both the review
and runtime verified sets remain empty.

## Invocation and result constraints

Compatibility is necessary but not sufficient. A future strategy must also enforce the
[workflow-plan contract](workflow-plans.md), including bounded in-flight invocations and
buffered results, actor isolation, stable RuntimeEnv and code identity, one-time result
consumption, explicit discard/drain behavior, cancellation deadlines, and teardown.

Ray documents that `CompiledDAGRef` results cannot be passed to another task or actor,
that `ray.get()` may be called only once for each result, and that retained zero-copy
values can block later executions. These are strategy lifecycle requirements; they are
not behaviors that the compatibility adapter can infer from a platform tuple.
