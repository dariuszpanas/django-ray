"""Contracts for the evaluation-only Kubernetes documentation boundary."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

APPLY_SURFACES = {
    Path("README.md"): ("make k8s-deploy",),
    Path("docs/deployment/kubernetes.md"): ("kubectl apply", "make k8s-deploy"),
    Path("docs/deployment/tls.md"): ("kubectl apply",),
    Path("k8s/README.md"): ("kubectl apply", "make k8s-deploy"),
    Path("k8s/overlays/dev/kustomization.yaml"): ("kubectl apply",),
    Path("k8s/overlays/dev-tls/kustomization.yaml"): ("kubectl apply",),
    Path("mk/k8s.mk"): ("kubectl apply",),
    Path("mk/tls.mk"): ("kubectl apply",),
}

MAKE_MUTATION_MARKERS = (
    "helm install",
    "helm uninstall",
    "helm upgrade --install",
    "kind load docker-image",
    "kubectl apply",
    "kubectl create",
    "kubectl delete",
    "kubectl rollout restart",
    "kubectl scale",
)

INDIRECT_MAKE_MUTATORS = ((Path("mk/k8s.mk"), "k8s-final-gate"),)

BOUNDARY_SURFACES = (
    Path("README.md"),
    Path("docs/getting-started.md"),
    Path("docs/deployment/kubernetes.md"),
    Path("docs/deployment/tls.md"),
    Path("docs/deployment/local-kuberay-gate.md"),
    Path("docs/worker-modes.md"),
    Path("docs/celery-migration.md"),
    Path("k8s/README.md"),
    Path("k8s/base/configmap.yaml"),
    Path("k8s/base/kustomization.yaml"),
    Path("k8s/base/secret.yaml"),
    Path("k8s/base/django-web.yaml"),
    Path("k8s/base/ray-cluster.yaml"),
    Path("k8s/base/ray-tls-secret.yaml"),
    Path("k8s/overlays/dev/kustomization.yaml"),
    Path("k8s/overlays/dev-tls/kustomization.yaml"),
    Path("k8s/overlays/local/kustomization.yaml"),
    Path("k8s/overlays/kuberay-kind/kustomization.yaml"),
    Path("k8s/overlays/kong-local/kustomization.yaml"),
    Path("mk/k8s.mk"),
    Path("mk/tls.mk"),
    Path("testproject/apps/__init__.py"),
    Path("testproject/apps/cluster_tasks/__init__.py"),
)


def _read(path: Path) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _make_target_section(content: str, target: str) -> str:
    marker = f"{target}:"
    start = content.index(marker)
    next_target = content.find("\n\n", start)
    return content[start:] if next_target == -1 else content[start:next_target]


def _make_target_names(content: str) -> tuple[str, ...]:
    return tuple(re.findall(r"(?m)^([a-zA-Z0-9_.%-]+):", content))


def _make_mutator_targets() -> set[tuple[Path, str]]:
    targets = set(INDIRECT_MAKE_MUTATORS)
    for makefile in sorted((ROOT / "mk").glob("*.mk")):
        path = makefile.relative_to(ROOT)
        content = _read(path)
        for target in _make_target_names(content):
            section = _make_target_section(content, target)
            if any(marker in section for marker in MAKE_MUTATION_MARKERS):
                targets.add((path, target))
    return targets


def test_local_evaluation_warning_precedes_every_sample_apply_path() -> None:
    for path, markers in APPLY_SURFACES.items():
        content = _read(path).lower()
        warning_index = content.index("evaluation")

        for marker in markers:
            marker_index = content.find(marker)
            while marker_index != -1:
                assert warning_index < marker_index, f"{path}: {marker!r} precedes its warning"
                marker_index = content.find(marker, marker_index + 1)


def test_every_make_kubernetes_mutator_warns_before_its_recipe() -> None:
    makefile = _read(Path("mk/k8s.mk"))
    warning = _make_target_section(makefile, "k8s-evaluation-warning")
    assert "trusted, disposable local evaluation only" in warning
    assert "not a production-ready deployment" in warning
    assert "create, update, restart, scale, or delete local cluster resources" in warning

    mutators = _make_mutator_targets()
    assert (Path("mk/tls.mk"), "k8s-create-tls-secret") in mutators
    assert (Path("mk/k8s.mk"), "k8s-install-kuberay") in mutators
    assert (Path("mk/k8s.mk"), "k8s-install-kong-local") in mutators
    assert (Path("mk/k8s.mk"), "k8s-final-gate") in mutators

    for path, target in sorted(mutators):
        section = _make_target_section(_read(path), target)
        header = section.splitlines()[0]
        assert "k8s-evaluation-warning" in header, f"{path}:{target} mutates without warning"


def test_samples_do_not_claim_a_production_deployment_path() -> None:
    content = "\n".join(_read(path).lower() for path in BOUNDARY_SURFACES)

    for contradictory_claim in (
        "sample production stack",
        "production-capable",
        "for a production deployment, start from `k8s/base`",
        "production must use the base",
        "use the base production mode",
        "deriving a production overlay from this base",
        "secrets (override in production!)",
        "for production topology",
        "production deployment is\nin [kubernetes deployment]",
        "production deployments on kubernetes",
        "for production, use cert-manager",
        "remote ray cluster for production",
        "cluster mode (production)",
    ):
        assert contradictory_claim not in content


def test_docs_keep_the_sample_hazards_and_production_checklist_explicit() -> None:
    guide = _read(Path("docs/deployment/kubernetes.md")).lower()
    normalized_guide = " ".join(guide.split())
    for hazard in (
        "bundled testproject",
        "static ray deployments",
        "mutable `latest`",
        "sample superuser",
        "operator-token",
        "shared `django-ray-secret`",
    ):
        assert hazard in guide

    for architecture_requirement in (
        "kuberay",
        "immutable supply chain",
        "service identity",
        "network security",
        "managed state and storage",
        "scoped secrets",
        "resource policy",
        "backups",
        "migration",
        "rollback",
    ):
        assert architecture_requirement in guide

    configmap = _read(Path("k8s/base/configmap.yaml"))
    assert 'DJANGO_DEPLOYMENT_MODE: "production"' in configmap
    assert "fail-closed Django configuration checks" in configmap
    assert "does not certify" in configmap

    local_gate = _read(Path("docs/deployment/local-kuberay-gate.md")).lower()
    assert "maintainer integration-validation gate" in local_gate
    assert "not deployment certification" in local_gate

    shared_secret = _read(Path("k8s/base/secret.yaml")).lower()
    assert "render reference for evaluation and local validation only" in shared_secret
    assert "component-scoped credentials" in shared_secret

    assert (
        "generic upstream kuberay head and worker pods import every value through `envfrom`"
        in normalized_guide
    )
    assert "evaluation-only credential blast radius" in normalized_guide
    ray_profile = _read(Path("k8s/overlays/kuberay-kind/ray-cluster-kuberay.yaml")).lower()
    assert ray_profile.count("image: rayproject/ray:") == 2
    assert ray_profile.count("name: django-ray-secret") == 2

    testproject_apps = _read(Path("testproject/apps/__init__.py")).lower()
    assert "remote ray cluster integration example" in testproject_apps
    assert "cluster mode (remote ray integration)" in testproject_apps

    testproject_app = _read(Path("testproject/apps/cluster_tasks/__init__.py")).lower()
    assert "trusted local kubernetes evaluation" in testproject_app
    assert "not a production-ready deployment" in testproject_app
