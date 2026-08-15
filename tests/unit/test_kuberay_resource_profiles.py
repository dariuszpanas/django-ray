"""Contract tests for the lean and heavier local KubeRay profiles."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATHS = {
    "direct": Path("k8s/overlays/kuberay-kind"),
    "kong": Path("k8s/overlays/kong-local"),
}
REQUIRE_KUSTOMIZE_ENV = "DJANGO_RAY_REQUIRE_KUSTOMIZE_PROBE_TESTS"


def _kubectl_executable() -> str:
    executable = shutil.which("kubectl")
    if executable is not None:
        return executable
    if os.environ.get(REQUIRE_KUSTOMIZE_ENV, "").lower() in {"1", "true", "yes"}:
        pytest.fail(f"kubectl is required when {REQUIRE_KUSTOMIZE_ENV} is enabled")
    pytest.skip("kubectl is not installed; skipping Kustomize profile contract tests")


@pytest.fixture(scope="module")
def rendered_profiles() -> dict[str, list[dict[str, Any]]]:
    """Render both local profiles through the same Kustomize implementation users run."""

    kubectl = _kubectl_executable()

    rendered: dict[str, list[dict[str, Any]]] = {}
    for name, path in PROFILE_PATHS.items():
        result = subprocess.run(
            [kubectl, "kustomize", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        rendered[name] = [resource for resource in yaml.safe_load_all(result.stdout) if resource]
    return rendered


def _resource(resources: list[dict[str, Any]], *, kind: str, name: str) -> dict[str, Any]:
    matches = [
        resource
        for resource in resources
        if resource.get("kind") == kind and resource.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, found {len(matches)}"
    return matches[0]


def _deployment_replicas(resources: list[dict[str, Any]], name: str) -> int:
    return int(_resource(resources, kind="Deployment", name=name)["spec"]["replicas"])


def _deployment_container(resources: list[dict[str, Any]], deployment_name: str) -> dict[str, Any]:
    return _named_deployment_container(resources, deployment_name, "django-ray-worker")


def _named_deployment_container(
    resources: list[dict[str, Any]], deployment_name: str, container_name: str
) -> dict[str, Any]:
    deployment = _resource(resources, kind="Deployment", name=deployment_name)
    containers = deployment["spec"]["template"]["spec"]["containers"]
    matches = [container for container in containers if container["name"] == container_name]
    assert len(matches) == 1
    return matches[0]


def _ray_worker_group(resources: list[dict[str, Any]]) -> dict[str, Any]:
    ray_cluster = _resource(resources, kind="RayCluster", name="ray")
    worker_groups = ray_cluster["spec"]["workerGroupSpecs"]
    assert len(worker_groups) == 1
    return worker_groups[0]


def _ray_head_group(resources: list[dict[str, Any]]) -> dict[str, Any]:
    return _resource(resources, kind="RayCluster", name="ray")["spec"]["headGroupSpec"]


def _resource_names(resources: list[dict[str, Any]], *, kind: str) -> set[str]:
    return {
        str(resource["metadata"]["name"]) for resource in resources if resource.get("kind") == kind
    }


def _make_target_block(makefile: str, target: str) -> str:
    marker = f"{target}:"
    assert makefile.count(marker) == 1
    return makefile.split(marker, maxsplit=1)[1].split("\n\n", maxsplit=1)[0]


def _ray_worker_replicas(resources: list[dict[str, Any]]) -> int:
    worker_group = _ray_worker_group(resources)
    replicas = int(worker_group["replicas"])
    assert int(worker_group["minReplicas"]) == replicas
    assert int(worker_group["maxReplicas"]) == replicas
    return replicas


def _env_value(container: dict[str, Any], name: str) -> str:
    matches = [item["value"] for item in container.get("env", []) if item["name"] == name]
    assert len(matches) == 1
    return str(matches[0])


def _argument_value(container: dict[str, Any], name: str) -> str:
    arguments = container["args"]
    index = arguments.index(name)
    return str(arguments[index + 1])


def _cpu_millicores(value: str | None) -> int:
    if value is None:
        return 0
    if value.endswith("m"):
        return int(value[:-1])
    return int(float(value) * 1000)


def _memory_mibibytes(value: str | None) -> int:
    if value is None:
        return 0
    factors = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}
    for suffix, factor in factors.items():
        if value.endswith(suffix):
            return int(float(value.removesuffix(suffix)) * factor)
    return int(value) // (1024 * 1024)


def _pod_resources(pod_spec: dict[str, Any], field: str) -> tuple[int, int]:
    regular_cpu = 0
    regular_memory = 0
    for container in pod_spec.get("containers", []):
        values = container.get("resources", {}).get(field, {})
        regular_cpu += _cpu_millicores(values.get("cpu"))
        regular_memory += _memory_mibibytes(values.get("memory"))

    init_cpu = 0
    init_memory = 0
    for container in pod_spec.get("initContainers", []):
        values = container.get("resources", {}).get(field, {})
        init_cpu = max(init_cpu, _cpu_millicores(values.get("cpu")))
        init_memory = max(init_memory, _memory_mibibytes(values.get("memory")))

    return max(regular_cpu, init_cpu), max(regular_memory, init_memory)


def _profile_totals(resources: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "pods": 0,
        "requested_millicores": 0,
        "requested_mibibytes": 0,
        "limit_millicores": 0,
        "limit_mibibytes": 0,
    }

    def add_pods(pod_spec: dict[str, Any], replicas: int) -> None:
        requested_cpu, requested_memory = _pod_resources(pod_spec, "requests")
        limit_cpu, limit_memory = _pod_resources(pod_spec, "limits")
        totals["pods"] += replicas
        totals["requested_millicores"] += requested_cpu * replicas
        totals["requested_mibibytes"] += requested_memory * replicas
        totals["limit_millicores"] += limit_cpu * replicas
        totals["limit_mibibytes"] += limit_memory * replicas

    for resource in resources:
        if resource.get("kind") != "Deployment":
            continue
        spec = resource["spec"]
        add_pods(spec["template"]["spec"], int(spec["replicas"]))

    ray_cluster = _resource(resources, kind="RayCluster", name="ray")
    ray_spec = ray_cluster["spec"]
    add_pods(ray_spec["headGroupSpec"]["template"]["spec"], 1)
    for worker_group in ray_spec["workerGroupSpecs"]:
        add_pods(worker_group["template"]["spec"], int(worker_group["replicas"]))

    return totals


@pytest.mark.parametrize("profile", ("direct", "kong"))
def test_local_profiles_keep_every_queue_consumer(
    rendered_profiles: dict[str, list[dict[str, Any]]],
    profile: str,
) -> None:
    resources = rendered_profiles[profile]

    assert _deployment_replicas(resources, "django-ray-worker-sync") == 1
    assert _deployment_replicas(resources, "django-ray-worker-ml") == 1

    default_worker = _deployment_container(resources, "django-ray-worker")
    assert _env_value(default_worker, "DJANGO_RAY_QUEUE") == ("default,high-priority,low-priority")

    sync_worker = _deployment_container(resources, "django-ray-worker-sync")
    assert "--sync" in sync_worker["args"]
    assert _argument_value(sync_worker, "--queue") == "sync"

    ml_worker = _deployment_container(resources, "django-ray-worker-ml")
    assert _argument_value(ml_worker, "--queue") == "ml"


def test_local_profiles_pin_distinct_capacity_contracts(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    direct = rendered_profiles["direct"]
    kong = rendered_profiles["kong"]

    assert _deployment_replicas(direct, "django-ray-worker") == 1
    assert _ray_worker_replicas(direct) == 2
    assert _ray_worker_group(direct)["rayStartParams"]["num-cpus"] == "2"

    assert _deployment_replicas(kong, "django-ray-worker") == 2
    assert _ray_worker_replicas(kong) == 4
    assert _ray_worker_group(kong)["rayStartParams"]["num-cpus"] == "3"


def test_local_profiles_pin_distinct_routing_contracts(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    direct = rendered_profiles["direct"]
    kong = rendered_profiles["kong"]
    dashboard_ingresses = {
        "grafana-ingress",
        "prometheus-ingress",
        "ray-dashboard-ingress",
    }

    assert _ray_head_group(direct)["serviceType"] == "NodePort"
    assert _ray_head_group(kong)["serviceType"] == "ClusterIP"
    assert _resource_names(direct, kind="Ingress").isdisjoint(dashboard_ingresses)
    assert dashboard_ingresses <= _resource_names(kong, kind="Ingress")


def test_direct_profile_retains_per_pod_resource_contracts(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    resources = rendered_profiles["direct"]
    expected_deployments = {
        ("django-ray-worker", "django-ray-worker"): (
            {"cpu": "100m", "memory": "256Mi"},
            {"cpu": "500m", "memory": "512Mi"},
        ),
        ("django-ray-worker-sync", "django-ray-worker"): (
            {"cpu": "100m", "memory": "256Mi"},
            {"cpu": "500m", "memory": "512Mi"},
        ),
        ("django-ray-worker-ml", "django-ray-worker"): (
            {"cpu": "100m", "memory": "256Mi"},
            {"cpu": "500m", "memory": "512Mi"},
        ),
        ("django-web", "django-web"): (
            {"cpu": "100m", "memory": "256Mi"},
            {"cpu": "500m", "memory": "512Mi"},
        ),
        ("postgres", "postgres"): (
            {"cpu": "100m", "memory": "256Mi"},
            {"cpu": "500m", "memory": "512Mi"},
        ),
        ("prometheus", "prometheus"): (
            {"cpu": "100m", "memory": "256Mi"},
            {"cpu": "500m", "memory": "512Mi"},
        ),
        ("grafana", "grafana"): (
            {"cpu": "50m", "memory": "128Mi"},
            {"cpu": "200m", "memory": "256Mi"},
        ),
    }
    for (deployment, container_name), (requests, limits) in expected_deployments.items():
        resources_block = _named_deployment_container(resources, deployment, container_name)[
            "resources"
        ]
        assert resources_block["requests"] == requests
        assert resources_block["limits"] == limits

    ray_cluster = _resource(resources, kind="RayCluster", name="ray")
    head_group = ray_cluster["spec"]["headGroupSpec"]
    assert head_group["rayStartParams"]["num-cpus"] == "2"
    head_containers = {
        container["name"]: container for container in head_group["template"]["spec"]["containers"]
    }
    assert head_containers["ray-head"]["resources"] == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "2Gi"},
    }
    assert head_containers["dashboard-importer"]["resources"] == {
        "requests": {"cpu": "50m", "memory": "64Mi"},
        "limits": {"cpu": "100m", "memory": "128Mi"},
    }
    assert _ray_worker_group(resources)["template"]["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "1", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "3Gi"},
    }


def test_local_profile_resource_totals_match_documented_baselines(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    assert _profile_totals(rendered_profiles["direct"]) == {
        "pods": 11,
        "requested_millicores": 3300,
        "requested_mibibytes": 5056,
        "limit_millicores": 9800,
        "limit_mibibytes": 12160,
    }
    assert _profile_totals(rendered_profiles["kong"]) == {
        "pods": 17,
        "requested_millicores": 10200,
        "requested_mibibytes": 17088,
        "limit_millicores": 27300,
        "limit_mibibytes": 38272,
    }


def test_kong_deploy_does_not_apply_the_lean_profile_first() -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")
    prerequisite_line = next(
        line for line in makefile.splitlines() if line.startswith("k8s-deploy-kong-local:")
    )
    assert "k8s-prepare-kuberay-kind" in prerequisite_line
    assert "k8s-deploy-kuberay-kind" not in prerequisite_line

    recipe = _make_target_block(makefile, "k8s-deploy-kong-local")
    assert recipe.index("k8s-delete-local-raycluster") < recipe.index(
        "kubectl apply -k k8s/overlays/kong-local"
    )
    assert "kubectl apply -k k8s/overlays/kong-local" in recipe
    assert "kubectl apply -k k8s/overlays/kuberay-kind" not in recipe
    assert "kubectl delete pod -l app=ray,component=head" not in recipe
    assert "status.desiredWorkerReplicas}'=4" in recipe
    assert "status.readyWorkerReplicas}'=4" in recipe
    assert "status.availableWorkerReplicas}'=4" in recipe
    assert recipe.index("--for=create service/ray-head-svc") < recipe.index(
        "--for=create pod -l app=ray,component=head"
    )
    assert recipe.index("--for=create pod -l app=ray,component=head") < recipe.index(
        "--for=condition=Ready pod -l app=ray,component=head"
    )
    assert recipe.index("--for=create pod -l app=ray,component=worker") < recipe.index(
        "status.desiredWorkerReplicas}'=4"
    )
    assert recipe.index("status.availableWorkerReplicas}'=4") < recipe.index(
        "--for=condition=Ready pod -l app=ray,component=worker"
    )
    assert "rollout restart deployment/django-ray-worker-ray-job" in recipe
    assert "rollout status deployment/django-ray-worker-ray-job" in recipe


def test_direct_deploy_cold_replaces_ray_without_uninstalling_kong() -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")
    recipe = _make_target_block(makefile, "k8s-deploy-kuberay-kind")

    delete_index = recipe.index("k8s-delete-local-raycluster")
    apply_index = recipe.index("kubectl apply -k k8s/overlays/kuberay-kind")
    assert delete_index < apply_index
    assert "k8s-uninstall-kong-local" not in recipe
    assert "helm uninstall" not in recipe
    assert "kubectl delete ingress/" not in recipe
    assert "kubectl apply -k k8s/overlays/kong-local" not in recipe
    assert "status.desiredWorkerReplicas}'=2" in recipe
    assert "status.readyWorkerReplicas}'=2" in recipe
    assert "status.availableWorkerReplicas}'=2" in recipe
    assert recipe.index("--for=create service/ray-head-svc") < recipe.index(
        "--for=create pod -l app=ray,component=head"
    )
    assert recipe.index("--for=create pod -l app=ray,component=head") < recipe.index(
        "--for=condition=Ready pod -l app=ray,component=head"
    )
    assert recipe.index("--for=create pod -l app=ray,component=worker") < recipe.index(
        "status.desiredWorkerReplicas}'=2"
    )
    assert recipe.index("status.availableWorkerReplicas}'=2") < recipe.index(
        "--for=condition=Ready pod -l app=ray,component=worker"
    )


def test_local_raycluster_delete_is_foreground_and_removes_generated_service() -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")
    recipe = _make_target_block(makefile, "k8s-delete-local-raycluster")

    assert "kubectl delete raycluster/ray -n django-ray" in recipe
    assert "--ignore-not-found" in recipe
    assert "--cascade=foreground" in recipe
    assert "--wait=true" in recipe
    assert "--timeout=240s" in recipe
    assert "kubectl delete service/ray-head-svc -n django-ray" in recipe


def test_local_kong_uninstall_targets_the_documented_release_and_routes() -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")
    recipe = _make_target_block(makefile, "k8s-uninstall-kong-local")

    assert "helm uninstall kong --namespace kong" in recipe
    assert "--ignore-not-found" in recipe
    assert "--wait" in recipe
    assert "--timeout 180s" in recipe
    assert "kubectl delete" in recipe
    for ingress in (
        "ingress/grafana-ingress",
        "ingress/prometheus-ingress",
        "ingress/ray-dashboard-ingress",
    ):
        assert ingress in recipe
    assert "namespace/kong" not in recipe
