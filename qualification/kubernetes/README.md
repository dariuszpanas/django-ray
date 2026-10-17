# Kubernetes qualification entrypoints

## Public contributor path

Use the [public application fixture](../application/README.md#public-prerequisites-and-invocation)
for affected deployed behavior. It has a source-owned Chainsaw command and a
[hosted Application Qualification workflow](../../.github/workflows/application-qualification.yml)
with explicit resource admission, source/image binding, assertion receipts and
owned cleanup. A maintainer can request its standard acceptance profile for a
branch or tag resolving to the candidate:

```bash
gh workflow run application-qualification.yml --ref "$CANDIDATE_REF" \
  -f mode=acceptance -f resource_profile=standard
```

Acceptance requires the exact-source Linux CI checkpoint and inspection of the
completed run's source, selected assertions, cold-Ray decision and cleanup.
Dispatch alone establishes none of these. The fixture's selected receipts do
not certify the complete application or release matrix. Follow the
[affected-gate matrix](../../docs/deployment/local-kuberay-gate.md) for the
required scenarios. Neither this workflow nor the public Chainsaw command needs
the adapter schema below.

For the isolated result-fold source assertion, use the
[public Linux test path](../docker/README.md#public-contributor-path). That test
does not require a Kubernetes deployment.

## Optional historical adapter contract

`runbook.yaml` preserves an earlier downstream Kubernetes adapter definition.
This repository does not supply an `external-evidence-v1` registration service
or control-plane runner. It is not a Kubernetes manifest and cannot be submitted
with `kubectl apply`. The remaining instructions describe the contract for a
separately reviewed adapter; they are not contributor prerequisites or a claim
that the public application workflow runs this installed-wheel scenario.

This directory owns the Kubernetes execution contract for django-ray's exact result-fold
qualification. The runbook invokes the same source scenario and real-Ray test as the Docker
qualification; it changes only the admitted runtime contract and the definition path recorded in
the source-owned evidence manifest.

The runbook requires a capacity-one `external-evidence-v1` Kubernetes target with Linux, Python,
and Ray capabilities. It neither provisions Kubernetes nor creates a namespace, Job, Pod, or
credential. The external control-plane adapter owns those resources, the evidence transport, and
exact cleanup.

Use one immutable django-ray commit for all four boundaries:

1. build `qualification/docker/Dockerfile` from
   `git -c core.autocrlf=false archive --format=tar <full-commit>`;
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
