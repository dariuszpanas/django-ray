# Source-owned Kubernetes qualification

This directory owns the Kubernetes execution contract for django-ray's exact result-fold
qualification. The runbook invokes the same source scenario and real-Ray test as the Docker
qualification; it changes only the admitted runtime contract and the definition path recorded in
the source-owned evidence manifest.

The runbook requires a capacity-one `external-evidence-v1` Kubernetes target with Linux, Python,
and Ray capabilities. It neither provisions Kubernetes nor creates a namespace, Job, Pod, or
credential. The external control-plane adapter owns those resources, the evidence transport, and
exact cleanup.

Use one immutable django-ray commit for all four boundaries:

1. build `qualification/docker/Dockerfile` from `git archive <full-commit>`;
2. add that same archive at `/workspace` in the final target image;
3. register `qualification/kubernetes/runbook.yaml` with this repository, full commit, and path;
4. bind the external pool's target source to the same repository and full commit.

The target image and control-plane runner image must be digest pinned. The runbook's literal
`--definition-path qualification/kubernetes/runbook.yaml` argument selects the only Kubernetes
definition path accepted by the scenario. Arbitrary paths cannot be reflected into evidence.

The command writes `junit.xml` and `execution-manifest.json` beneath `/evidence`; the external
runner owns the merged `command.log`. The three evidence budgets and the cleanup timeout remain
identical to the Docker qualification. The target assertions and failure behavior are documented
in [the shared scenario guide](../docker/README.md).
