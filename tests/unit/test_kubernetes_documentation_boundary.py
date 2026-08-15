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


def test_dormant_target_attestation_has_an_explicit_gate_boundary() -> None:
    guide = _read(Path("docs/deployment/local-kuberay-gate.md"))
    row = next(
        line for line in guide.splitlines() if "A Django-free target-attestation codec" in line
    )

    assert "KubeRay not applicable" in row
    assert "mandatory serial local-Ray plus package-free two-node Ray Client evidence" in row
    assert "unreachable from settings, enqueue, worker, runner, transport, persistence" in row
    assert "narrow exception" in row
    assert "Once a production path consumes the proof" in row
    assert "two-cluster handoff extension" in row

    assert "remote bootstrap/import behavior other than the narrow dormant-attestation" in guide
    assert (
        "For the dormant-attestation exception, retain the exact serial local-Ray result" in guide
    )
    assert "explicit guarded-KubeRay-not-applicable decision" in guide


def test_dormant_target_persistence_has_a_database_only_gate_boundary() -> None:
    guide = _read(Path("docs/deployment/local-kuberay-gate.md"))
    normalized_guide = " ".join(guide.split())
    row = next(
        line for line in guide.splitlines() if "Additive Ray-target persistence tables" in line
    )

    assert "KubeRay not applicable" in row
    assert "mandatory SQLite and PostgreSQL migration/coordination evidence" in row
    assert "coordinator-enforced append history" in row
    assert "database immutable-update/insert-bound guards" in row
    assert (
        "no production task/attempt, worker-lease, enqueue, claim, adoption, lifecycle, routing, "
        "status, operator, or deployment path consumes those records"
    ) in row
    assert "creates no target capacity, work placement, cluster mutation" in row
    assert "final target routing requires the two-cluster handoff extension" in row

    assert (
        "For the dormant target-persistence exception, retain the exact SQLite and PostgreSQL "
        "migration and coordination results plus the explicit guarded-KubeRay-not-applicable "
        "decision"
    ) in normalized_guide
    assert "Do not report this database evidence as a live attestation" in normalized_guide


def test_dormant_task_target_binding_has_a_database_only_gate_boundary() -> None:
    guide = _read(Path("docs/deployment/local-kuberay-gate.md"))
    normalized_guide = " ".join(guide.split())
    row = next(
        line
        for line in guide.splitlines()
        if "An additive, unseeded, create-once execution-to-immutable-target-policy" in line
    )

    assert "KubeRay not applicable" in row
    assert "mandatory SQLite and PostgreSQL binding-migration evidence" in row
    assert "no binding writer, reader, Admin surface, enqueue hook" in row
    assert "claim/adoption predicate, lifecycle or routing path, backfill" in row
    assert "Current workers are target-unaware" in row
    assert "future target-aware consumer must treat absence as unbound and fail closed" in row
    assert "Both parents are deletion-protected" in row
    assert "activation must first adapt and test every execution and policy retention" in row
    assert "binding deletion requires explicit audit and retention ordering" in row
    assert "`created_at` is not enqueue provenance" in row
    assert "historical policy state is not capacity or claim authorization" in row
    assert "Legacy adoption remains forbidden until #381 supplies exact mapping lineage" in row
    assert "final target routing requires the two-cluster handoff extension" in row

    assert (
        "For the dormant task-target-binding exception, retain the exact SQLite and PostgreSQL "
        "binding migration results plus the explicit guarded-KubeRay-not-applicable decision"
    ) in normalized_guide
    assert "Do not report this database evidence as enqueue provenance" in normalized_guide
    assert "both parents are protected" in normalized_guide
    assert "tested task and policy cleanup ordering" in normalized_guide


def test_dormant_task_target_binding_has_no_production_consumer() -> None:
    production_root = ROOT / "src" / "django_ray"
    references = {
        path.relative_to(ROOT).as_posix()
        for path in production_root.rglob("*.py")
        if "RayTaskTargetBinding" in path.read_text(encoding="utf-8")
    }

    assert references == {
        "src/django_ray/migrations/0023_ray_task_target_binding.py",
        "src/django_ray/migrations/0024_ray_target_routes.py",
        "src/django_ray/migrations/0026_ray_task_target_execution_evidence.py",
        "src/django_ray/models.py",
    }


def test_dormant_target_routing_has_a_database_only_gate_boundary() -> None:
    guide = _read(Path("docs/deployment/local-kuberay-gate.md"))
    normalized_guide = " ".join(guide.split())
    row = next(line for line in guide.splitlines() if "Bounded backend-alias route history" in line)

    assert "KubeRay not applicable" in row
    assert "mandatory SQLite and PostgreSQL routing migration/coordination evidence" in row
    assert "append-only backend-alias route history" in row
    assert "separate, unseeded, create-once binding-to-route-revision selection table" in row
    assert "no task or binding writer, reader, Admin surface, enqueue hook" in row
    assert "claim/adoption predicate, lifecycle path, lease, or runtime consumer" in row
    assert "Route intent is not a live attestation, target capacity" in row
    assert "An absent selection is unproved provenance" in row
    assert "Legacy mapping is distinct and deferred to #381" in row
    assert "Cleanup must delete a selection before either its binding or route revision" in row
    assert "every revision before its route" in row
    assert "final target routing requires the two-cluster handoff extension" in row

    assert (
        "For the dormant target-routing exception, retain the exact SQLite and PostgreSQL "
        "routing migration and coordination results plus the explicit guarded-KubeRay-not-"
        "applicable decision"
    ) in normalized_guide
    assert "Do not report this database evidence as task or binding provenance" in normalized_guide
    assert "absent route-selection provenance is unproved" in normalized_guide
    assert "both selection parents are protected" in normalized_guide


def test_dormant_task_target_route_selection_has_no_production_consumer() -> None:
    production_root = ROOT / "src" / "django_ray"
    references = {
        path.relative_to(ROOT).as_posix()
        for path in production_root.rglob("*.py")
        if "RayTaskTargetRouteSelection" in path.read_text(encoding="utf-8")
    }

    assert references == {
        "src/django_ray/migrations/0024_ray_target_routes.py",
        "src/django_ray/migrations/0026_ray_task_target_execution_evidence.py",
        "src/django_ray/models.py",
    }


def test_dormant_worker_target_capability_has_a_database_only_gate_boundary() -> None:
    guide = _read(Path("docs/deployment/local-kuberay-gate.md"))
    normalized_guide = " ".join(guide.split())
    row = next(
        line
        for line in guide.splitlines()
        if "lease-cascading worker/target current-capability table" in line
    )

    assert "KubeRay not applicable" in row
    assert "mandatory SQLite and PostgreSQL capability migration/coordination evidence" in row
    assert "private compare-and-set coordinator" in row
    assert "no production path creates, renews, reads, or treats capability rows as capacity" in row
    assert "exact-lease deletion may only fail-closed cascade-withdraw" in row
    assert "CAS renewal, lease-cascade withdrawal" in row
    assert "latest `active` or `draining` Ray Core policy" in row
    assert "draining never authorizes a new route or enqueue" in row
    assert "Row presence alone is never claim authority" in row
    assert "Policy and attestation revisions remain the audit history" in row
    assert "future generations or attempts must archive their own observed tuple" in row
    assert "Ray Job capability APIs remain unsupported" in row
    assert "supported Admin inactive-lease cleanup" in row
    assert "KubeRay remains not applicable because no production producer can create" in row
    assert "final target routing requires the two-cluster handoff extension" in row

    assert (
        "For the dormant worker-target-capability exception, retain the exact SQLite and "
        "PostgreSQL capability migration and coordination results plus the explicit guarded-"
        "KubeRay-not-applicable decision"
    ) in normalized_guide
    assert "lease deletion cascades the ephemeral current row" in normalized_guide
    assert "fail-closed withdrawal is the only indirect production mutation" in normalized_guide
    assert "CAS revision is not audit history" in normalized_guide
    assert "row presence never replaces live lease, policy, proof" in normalized_guide
    assert "no production producer can create, renew, or advertise" in normalized_guide


def test_dormant_worker_target_capability_has_no_production_consumer() -> None:
    production_root = ROOT / "src" / "django_ray"
    references = {
        path.relative_to(ROOT).as_posix()
        for path in production_root.rglob("*.py")
        if "RayWorkerTargetCapability" in path.read_text(encoding="utf-8")
    }

    assert references == {
        "src/django_ray/migrations/0025_ray_worker_target_capabilities.py",
        "src/django_ray/migrations/0026_ray_task_target_execution_evidence.py",
        "src/django_ray/models.py",
        "src/django_ray/target_capabilities.py",
    }

    coordinator_symbols = (
        "advertise_ray_worker_target_capability",
        "withdraw_ray_worker_target_capability",
        "withdraw_all_ray_worker_target_capabilities",
    )
    for symbol in coordinator_symbols:
        callers = {
            path.relative_to(ROOT).as_posix()
            for path in production_root.rglob("*.py")
            if symbol in path.read_text(encoding="utf-8")
        }
        assert callers == {"src/django_ray/target_capabilities.py"}


def test_protocol_v2_evidence_has_no_production_persistence_consumer() -> None:
    production_root = ROOT / "src" / "django_ray"
    expected_model_references = {
        "src/django_ray/migrations/0026_ray_task_target_execution_evidence.py",
        "src/django_ray/models.py",
    }

    for model_name in (
        "RayTaskTargetExecutionEvidence",
        "RayTaskTargetExecutionOutcome",
    ):
        pattern = re.compile(rf"\b{model_name}\b")
        references = {
            path.relative_to(ROOT).as_posix()
            for path in production_root.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        }
        assert references == expected_model_references

    for private_seam in (
        "_submit_target_execution(",
        "_poll_target_execution_results(",
    ):
        references = {
            path.relative_to(ROOT).as_posix()
            for path in production_root.rglob("*.py")
            if private_seam in path.read_text(encoding="utf-8")
        }
        assert references == {"src/django_ray/runner/ray_core.py"}
