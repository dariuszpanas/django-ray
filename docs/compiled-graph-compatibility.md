# Compiled Graph Compatibility

Ray Compiled Graph is not currently an enabled django-ray execution strategy. This page
defines the fail-closed compatibility contract and the evidence required before a
future strategy may call Ray's beta native APIs.

Compiled Graph is an execution strategy for a validated static actor region in an
[effective workflow plan](workflow-plans.md). It is not a Django task type, and it does
not make a data-dependent workflow static. When this policy rejects compilation,
ordinary local and dynamic Ray task execution remain available.

## Current support state

Policy version 2 has **no verified native capability rows**. The rows below are canary
candidates, not a support claim. `require_compiled_graph_support()` rejects them with
`CANDIDATE_REQUIRES_SMOKE` until a real isolated smoke result for the exact tuple is
reviewed and promoted in a later policy change.

| Ray | Python | OS and architecture | Status | Why it is listed |
|---|---|---|---|---|
| 2.53.0 | 3.12 | Linux x86_64 | Candidate | django-ray's minimum Ray release |
| 2.56.0 | 3.12 | Linux x86_64 | Candidate | Version in the current lock and initial Windows investigation |
| 2.56.1 | 3.12 | Linux x86_64 | Candidate | Latest PyPI and Ray release reviewed on 2026-07-19 |

The versions are exact. A new patch, minor, prerelease, or nightly is rejected until it
is deliberately added as a candidate and then independently verified. Python 3.13 and
3.14 remain supported for ordinary django-ray execution but are not Compiled Graph
candidates in policy version 2.

Version sources:

- [Ray 2.53.0 on PyPI](https://pypi.org/project/ray/2.53.0/)
- [Ray 2.56.0 on PyPI](https://pypi.org/project/ray/2.56.0/)
- [Ray 2.56.1 on PyPI](https://pypi.org/project/ray/2.56.1/) and the
  [Ray 2.56.1 release](https://github.com/ray-project/ray/releases/tag/ray-2.56.1)

The general django-ray dependency remains `ray[default]>=2.53.0`. That broader range
does not imply Compiled Graph eligibility.

## Topology decisions

The topology names identify the process that compiles the graph and must perform every
invocation. They do not describe the actor tasks inside the graph. The ownership and
reuse design is defined separately in
[ADR-0002](design/adr-0002-compiled-session-ownership.md); this page decides whether an
exact runtime tuple may attempt that design.

| Topology | Policy version 2 | Contract |
|---|---|---|
| Direct Ray Core driver | Candidate | A directly connected, non-Ray-Client driver compiles, invokes, drains, and tears down the graph in the same process. This is the diagnostic control topology. |
| Local nested Ray Core owner | Candidate | A directly connected Ray Core driver submits one outer task, which owns compilation and all invocations for the durable run. This is the production-intended intra-task pilot topology; the smoke owner has retries disabled to avoid multiplying a native crash. |
| Ray Job driver | Candidate | Compilation and invocation must occur in the submitted Ray Job driver, not in the Django worker or Job Submission client. Driver drain must finish before job exit. |
| Ray Client driver | Rejected | Compilation in the Ray Client driver itself would place the compiler owner across the proxy boundary. No such owner contract is validated; use Ray Jobs or dynamic Ray tasks. |
| Nested cluster-side owner submitted through Ray Client | Deferred/unverified | The Ray Client process submits an ordinary outer task, but compilation and every invocation occur in that cluster-side task. Policy version 2 models this as a separate `ray-client` submission transport and rejects it; it cannot inherit evidence from a `direct-ray-core` nested owner. |
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

| Transport | Policy version 2 | Dependencies and limits |
|---|---|---|
| CPU shared memory | Candidate | The django-ray runtime keeps its normal `ray[default]` dependency. Native canaries install `ray[cgraph]` as recommended by Ray so a missing Compiled Graph extra cannot produce a false compatibility result. No GPU package becomes an application dependency. |
| GPU/NCCL | Rejected | Requires a separately tested `ray[cgraph]`, CuPy package matching the CUDA major, NVIDIA driver/runtime, NCCL, GPU topology, tensor schema, and peer-to-peer transport matrix. |

Ray 2.56's `cgraph` extra currently selects `cupy-cuda12x` outside macOS. That is a
canary or future GPU-strategy dependency, not a mandatory django-ray dependency. The
old `ray[adag]` spelling is not used for the candidate versions. Current tensor paths
must use `with_tensor_transport`; older `with_type_hint` examples are not the policy.

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
releases. Linux jobs ran on
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
itself. Issue #100 owns the fresh Windows release/nightly evidence and upstream report,
targeted for 2026-07-27. That follow-up is non-blocking for issue #86 / PR #92's
Linux/Kubernetes fail-closed infrastructure; fresh Windows or nightly evidence is not
required before this infrastructure change merges.

No nightly wheel was run. Nightly wheel URLs and ABIs rotate, while this pass was
designed to classify the three exact published candidates. A nightly result would be
a separate tuple: it would neither verify these releases nor safely widen the policy.
This is not evidence that nightly passes or fails. If issue #100 tests a nightly, its
record must name the resolved wheel identity rather than treating it as a release
neighbor; a nightly run is not a Linux/Kubernetes merge gate.

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
   CI invokes the synced `.venv/bin/python` directly for environment capture and the
   native probe. Do not wrap those commands in `uv run`: Ray workers inherit the
   wrapper context and may resolve their worker directory as a separate uv project.
5. Unit tests prove exact-tuple matching and rejection of every neighboring unverified
   tuple. Dynamic workflow tests remain green without selecting Compiled Graph.
6. Issue #99 reviews provenance, expiry, and quarantine policy before a reviewer adds
   only the passing exact capability tuple to the verified set. No row may inherit an
   image, deployment, shared-memory, or object-store profile from a near neighbor.
   Changes to compatibility meaning bump the policy version.

The required CI smoke exercises the minimum, repository-lock, and reviewed-latest
candidate releases against the local nested Ray task owner. The repository-controlled
scheduled canary resolves the official latest stable `ray[cgraph]` release and
deliberately supplies both unsafe acknowledgements so an unknown new release is
actually exercised. A manual dispatch supplies neither acknowledgement unless its
trusted operator enables the `unsafe-native` boolean. An upstream/nightly wheel remains
manual because its URL and ABI rotate. Candidate smoke without all explicit profiles is
discovery evidence only and cannot populate issue #99's verified table.

Review the matrix whenever Ray, Python, operating-system images, channel APIs,
`ray[cgraph]` dependencies, immutable deployment/image identity, shared-memory or
object-store configuration, or the workflow owner contract changes. Never widen a
range from version ordering alone.

## Invocation and result constraints

Compatibility is necessary but not sufficient. A future strategy must also enforce the
[workflow-plan contract](workflow-plans.md), including bounded in-flight invocations and
buffered results, actor isolation, stable RuntimeEnv and code identity, one-time result
consumption, explicit discard/drain behavior, cancellation deadlines, and teardown.

Ray documents that `CompiledDAGRef` results cannot be passed to another task or actor,
that `ray.get()` may be called only once for each result, and that retained zero-copy
values can block later executions. These are strategy lifecycle requirements; they are
not behaviors that the compatibility adapter can infer from a platform tuple.
