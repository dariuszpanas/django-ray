"""Contracts for the shared KubeRay operator in co-resident mode."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OPERATOR_ROOT = ROOT / "k8s" / "operators" / "kuberay-co-resident"


def _documents(path: Path) -> list[dict[str, Any]]:
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def _resource(documents: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    matches = [document for document in documents if document["kind"] == kind]
    assert len(matches) == 1
    return matches[0]


def _target_block(makefile: str, target: str) -> str:
    marker = f"{target}:"
    assert makefile.count(marker) == 1
    return makefile.split(marker, maxsplit=1)[1].split("\n\n", maxsplit=1)[0]


def test_operator_policy_is_one_complete_unscoped_quota() -> None:
    documents = _documents(OPERATOR_ROOT / "resource-policy.yaml")
    quota = _resource(documents, "ResourceQuota")

    assert quota["metadata"] == {
        "name": "kuberay-co-resident-budget",
        "namespace": "kuberay-system",
    }
    assert "scopes" not in quota["spec"]
    assert "scopeSelector" not in quota["spec"]
    assert quota["spec"]["hard"] == {
        "requests.cpu": "200m",
        "requests.memory": "1Gi",
        "requests.ephemeral-storage": "256Mi",
        "limits.cpu": "200m",
        "limits.memory": "1Gi",
        "limits.ephemeral-storage": "512Mi",
        "requests.storage": "1Mi",
    }

    application_documents = _documents(
        ROOT / "k8s" / "overlays" / "co-resident" / "resource-policy.yaml"
    )
    application_quota = _resource(application_documents, "ResourceQuota")
    application_cpu = application_quota["spec"]["hard"]["limits.cpu"]
    operator_cpu = quota["spec"]["hard"]["limits.cpu"]
    assert int(application_cpu.removesuffix("m")) + int(operator_cpu.removesuffix("m")) == 1800


def test_operator_limit_range_and_helm_values_fit_two_rollout_pods() -> None:
    documents = _documents(OPERATOR_ROOT / "resource-policy.yaml")
    limit_range = _resource(documents, "LimitRange")["spec"]["limits"]
    assert limit_range == [
        {
            "type": "Container",
            "defaultRequest": {
                "cpu": "100m",
                "memory": "512Mi",
                "ephemeral-storage": "64Mi",
            },
            "default": {
                "cpu": "100m",
                "memory": "512Mi",
                "ephemeral-storage": "256Mi",
            },
            "max": {
                "cpu": "100m",
                "memory": "512Mi",
                "ephemeral-storage": "256Mi",
            },
        }
    ]

    values = yaml.safe_load((OPERATOR_ROOT / "values.yaml").read_text())
    assert values["replicas"] == 1
    assert values["image"] == {
        "repository": "quay.io/kuberay/operator",
        "tag": "v1.6.2",
        "pullPolicy": "IfNotPresent",
    }
    assert values["resources"]["requests"] == {
        "cpu": "100m",
        "memory": "512Mi",
        "ephemeral-storage": "64Mi",
    }
    assert values["resources"]["limits"] == {
        "cpu": "100m",
        "memory": "512Mi",
        "ephemeral-storage": "256Mi",
    }


def test_make_targets_pin_and_bound_operator_before_co_resident_apply() -> None:
    makefile = (ROOT / "mk" / "k8s.mk").read_text()
    install = _target_block(makefile, "k8s-install-kuberay")
    bounded_install = _target_block(makefile, "k8s-install-kuberay-co-resident")
    deploy = _target_block(makefile, "k8s-deploy-co-resident")
    cleanup = _target_block(makefile, "k8s-delete-co-resident-superseded")
    bootstrap = _target_block(makefile, "k8s-bootstrap-django-ray-secret")
    reverse = _target_block(makefile, "k8s-deploy-kuberay-kind")
    kong = _target_block(makefile, "k8s-deploy-kong-local")
    policy_cleanup = _target_block(makefile, "k8s-delete-co-resident-policy")

    assert "KUBERAY_OPERATOR_CHART_VERSION ?= 1.6.2" in makefile
    assert "--version $(KUBERAY_OPERATOR_CHART_VERSION)" in install
    assert "-f $(KUBERAY_OPERATOR_VALUES)" in install
    assert bounded_install.index("k8s/operators/kuberay-co-resident") < bounded_install.index(
        "k8s-install-kuberay"
    )
    assert deploy.index("k8s-delete-co-resident-superseded") < deploy.index(
        "apply -k k8s/overlays/co-resident"
    )
    assert "status.availableWorkerReplicas}'=1" in deploy
    assert "get secret/django-ray-secret" in bootstrap
    assert "create -f k8s/base/secret.yaml" in bootstrap
    assert "apply -f k8s/base/secret.yaml" not in bootstrap
    assert reverse.index("k8s-delete-co-resident-policy") < reverse.index(
        "k8s/overlays/kuberay-kind"
    )
    assert kong.index("k8s-delete-co-resident-policy") < kong.index("k8s/overlays/kong-local")
    assert "resourcequota/django-ray-co-resident-budget" in policy_cleanup
    assert "limitrange/django-ray-co-resident-defaults" in policy_cleanup
    assert "namespace/django-ray" not in cleanup
    assert "persistentvolumeclaim" not in cleanup.lower()
    assert "pvc/" not in cleanup.lower()

    context_bound_blocks = (
        bounded_install,
        bootstrap,
        cleanup,
        deploy,
        kong,
        policy_cleanup,
    )
    for block in context_bound_blocks:
        kubectl_lines = [line for line in block.splitlines() if "kubectl " in line]
        assert kubectl_lines
        assert all('--context "$(K8S_CONTEXT)"' in line for line in kubectl_lines)


def test_make_co_resident_transition_requires_an_explicit_local_context() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the transition guard")

    missing = subprocess.run(
        [make, "--no-print-directory", "--dry-run", "k8s-deploy-co-resident"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert "K8S_CONTEXT is required" in missing.stderr

    remote = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "k8s-deploy-co-resident",
            "K8S_CONTEXT=production",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert remote.returncode != 0
    assert "K8S_CONTEXT must be docker-desktop or kind-<name>" in remote.stderr
