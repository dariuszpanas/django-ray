# Installed-wheel runtime qualification

`fanout.yaml` runs the real-Ray cases in `tests/unit/test_distributed.py` against the exact installed
wheel. It covers ordinary fanout, repeated remote reuse, strict context delivery, pre-invocation
protocol/callable rejection, and propagation into the outer completion. Additional real-Ray cases
in that file are included automatically and must also complete. This includes the failure cleanup
and bounded preparation regressions when their source changes are present in the candidate.

This is a separately registered workload for the existing `external-evidence-v1` Linux Kubernetes
Job path. It does not replace the full CI matrix or the conditional application KubeRay gate. In
particular, these tests do not deploy managers, restart a remote cluster, prove PostgreSQL behavior,
or measure end-to-end task throughput. The original Docker and Kubernetes result-fold runbooks and
their schema-v1 evidence remain unchanged and replayable.

## Exact candidate and bounded execution

Use the same reviewed image procedure as the [Kubernetes result-fold qualification](../kubernetes/README.md):
build `qualification/docker/Dockerfile` from one exact Git archive, retain that archive at
`/workspace`, and register **`qualification/runtime/fanout.yaml`** from the same commit. The external
control plane must bind the exact source candidate to the verified digest-pinned image. A passing
run at another commit is not evidence for the candidate under review.

The literal command is `python -m qualification.runtime.scenario`; it accepts no caller arguments,
test selectors, environment settings, or credentials. It reuses the result-fold qualification's
offline wheel installation, source/package tree verification and bounded subprocess capture. The
wheel is installed under `/tmp/django-ray-runtime-qualification/target`; no editable installation
or dependency download happens during the workload. A second interpreter verifies that its package
import came from that installed target before invoking pytest. The installed package tree is
checked again after execution.

Execution is serial, with no pytest-xdist. Each real-Ray fixture starts an isolated local runtime
with two logical CPUs, zero GPUs and a 128-MiB object store and shuts it down afterward. Logical Ray
allocation is distinct from the operator's physical CPU and memory limits. The reviewed DRT Job
profile requests two CPUs and 4 GiB memory and limits the target to 3.5 CPUs, 7.5 GiB memory and
7 GiB ephemeral storage. Resource admission, namespace isolation, process/shared-memory budgets,
target stopping and cleanup belong to the control plane; capability strings in schema v1 are not
resource limits. Do not resize a pool or start another cluster to run this definition.

The command uses a 60-second offline-install deadline and a 300-second pytest deadline within the
runbook's 420-second execution budget. Subprocess output is bounded to 32 KiB per stream using the
existing process-group cleanup path. The separate cleanup budget is 180 seconds. These are hard
ceilings, not a measured completion-time promise. Run this workload only on admitted Linux capacity;
resource-free contract tests can run on a development host without starting Ray.

## Evidence and refusal behavior

The pytest observer retains the exact collected identities and every setup, call and teardown
outcome and duration. The selection must contain each baseline assertion family, stay within the
fanout test module, and contain at most 64 unique cases. Every selected case must report each phase
exactly once and pass it. Missing reports, skipped or expected-failure cases, duplicated phases,
failed teardown, a nonzero interpreter exit, or a Ray runtime still initialized after pytest all
fail qualification. A result containing only some of the selected tests cannot become success.

The command creates `junit.xml` from those validated observations and writes
`execution-manifest.json` with the workload/definition, candidate wheel and package digests,
dependency versions, selection, phase observations, elapsed time and outcome. The external runner
retains `command.log`, including phase progress and bounded pytest output. Timings are retained in
the manifest; passing JUnit identities are deterministic. Failed runs contain an explicit failing
contract testcase and retain available candidate/phase evidence. Existing evidence is never
overwritten. Missing or failed evidence cannot pass the workload.

The external control plane still verifies artifact bytes and budgets and proves removal of the
owned Job/Pod/namespace and release of execution ownership. `ray.is_initialized() == False` only
proves the driver disconnected; it is not evidence of Kubernetes cleanup or universal cancellation
quiescence. A PR claiming a particular regression must show that regression's identity in the
manifest for the matching source candidate.
