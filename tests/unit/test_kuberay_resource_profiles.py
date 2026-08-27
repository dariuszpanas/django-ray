"""Contract tests for the lean and heavier local KubeRay profiles."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.local_kuberay_gate import (
    CO_RESIDENT_RAY_HEAD_CONTAINER_NAMES,
    GUARDED_GATE_RAY_HEAD_CONTAINER_NAMES,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATHS = {
    "co_resident": Path("k8s/overlays/co-resident"),
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
    """Render local profiles through the same Kustomize implementation users run."""

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


def _job_container(resources: list[dict[str, Any]], job_name: str) -> dict[str, Any]:
    job = _resource(resources, kind="Job", name=job_name)
    containers = job["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _shared_memory_volume(group: dict[str, Any]) -> dict[str, Any]:
    volumes = group["template"]["spec"]["volumes"]
    matches = [volume for volume in volumes if volume["name"] == "shared-memory"]
    assert len(matches) == 1
    return matches[0]


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_co_resident_profile_has_exact_five_pod_topology(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    resources = rendered_profiles["co_resident"]
    actual: dict[str, set[str]] = {}
    for resource in resources:
        actual.setdefault(str(resource["kind"]), set()).add(str(resource["metadata"]["name"]))

    assert actual == {
        "ConfigMap": {"django-ray-config"},
        "Deployment": {"django-ray-worker", "django-web", "postgres"},
        "Job": {"django-setup"},
        "LimitRange": {"django-ray-co-resident-defaults"},
        "Namespace": {"django-ray"},
        "PersistentVolumeClaim": {
            "payload-storage-pvc",
            "postgres-pvc",
            "runtime-env-pvc",
        },
        "RayCluster": {"ray"},
        "ResourceQuota": {"django-ray-co-resident-budget"},
        "Service": {"django-web-svc", "postgres-svc"},
    }
    for resource in resources:
        if resource["kind"] != "Namespace":
            assert resource["metadata"]["namespace"] == "django-ray"

    config = _resource(resources, kind="ConfigMap", name="django-ray-config")
    assert config["data"]["DJANGO_DEPLOYMENT_MODE"] == "demo"
    assert _profile_totals(resources)["pods"] == 5


def test_co_resident_profile_manages_config_without_rendering_credentials(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    resources = rendered_profiles["co_resident"]

    assert _resource_names(resources, kind="Secret") == set()
    config = _resource(resources, kind="ConfigMap", name="django-ray-config")
    assert config["data"]["DJANGO_DEPLOYMENT_MODE"] == "demo"
    assert config["data"]["RAY_DASHBOARD_URL"] == "http://ray-head-svc:8265"


def test_co_resident_profile_pins_single_unit_execution_capacity(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    resources = rendered_profiles["co_resident"]
    task_manager = _deployment_container(resources, "django-ray-worker")
    assert _env_value(task_manager, "DJANGO_RAY_QUEUE") == "default"
    assert _env_value(task_manager, "DJANGO_RAY_CONCURRENCY") == "1"

    head = _ray_head_group(resources)
    worker = _ray_worker_group(resources)
    assert head["serviceType"] == "ClusterIP"
    assert head["rayStartParams"]["num-cpus"] == "0"
    assert head["rayStartParams"]["object-store-memory"] == "268435456"
    assert int(worker["replicas"]) == 1
    assert int(worker["minReplicas"]) == 1
    assert int(worker["maxReplicas"]) == 1
    assert worker["rayStartParams"]["num-cpus"] == "1"
    assert worker["rayStartParams"]["object-store-memory"] == "268435456"

    head_container = head["template"]["spec"]["containers"][0]
    assert head_container["resources"] == {
        "requests": {"cpu": "100m", "memory": "1Gi"},
        "limits": {"cpu": "350m", "memory": "2Gi"},
    }

    for group, container_name in ((head, "ray-head"), (worker, "ray-worker")):
        containers = group["template"]["spec"]["containers"]
        assert [container["name"] for container in containers] == [container_name]
        assert containers[0]["envFrom"] == [
            {"configMapRef": {"name": "django-ray-config"}},
            {"secretRef": {"name": "django-ray-secret"}},
        ]
        mounts = containers[0]["volumeMounts"]
        assert {mount["mountPath"] for mount in mounts if mount["name"] == "shared-memory"} == {
            "/dev/shm"
        }
        assert _shared_memory_volume(group)["emptyDir"] == {
            "medium": "Memory",
            "sizeLimit": "512Mi",
        }


def test_co_resident_profile_fits_setup_below_namespace_resource_quota(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    resources = rendered_profiles["co_resident"]
    totals = _profile_totals(resources)
    assert totals == {
        "pods": 5,
        "requested_millicores": 500,
        "requested_mibibytes": 2176,
        "limit_millicores": 1450,
        "limit_mibibytes": 4736,
    }

    quota = _resource(resources, kind="ResourceQuota", name="django-ray-co-resident-budget")
    hard = quota["spec"]["hard"]
    assert hard == {
        "limits.cpu": "1600m",
        "limits.ephemeral-storage": "2Gi",
        "limits.memory": "5Gi",
        "requests.cpu": "600m",
        "requests.ephemeral-storage": "512Mi",
        "requests.memory": "3Gi",
        "requests.storage": "2Gi",
    }

    setup_resources = _job_container(resources, "django-setup")["resources"]
    assert setup_resources == {
        "limits": {"cpu": "100m", "memory": "256Mi"},
        "requests": {"cpu": "50m", "memory": "128Mi"},
    }
    quota_limit = _cpu_millicores(hard["limits.cpu"])
    setup_limit = _cpu_millicores(setup_resources["limits"]["cpu"])
    assert setup_limit == 100
    assert totals["limit_millicores"] + setup_limit == 1550
    assert quota_limit - totals["limit_millicores"] - setup_limit == 50
    quota_memory = _memory_mibibytes(hard["limits.memory"])
    setup_memory = _memory_mibibytes(setup_resources["limits"]["memory"])
    assert totals["limit_mibibytes"] + setup_memory == 4992
    assert quota_memory - totals["limit_mibibytes"] - setup_memory == 128

    pvc_storage = {
        resource["metadata"]["name"]: resource["spec"]["resources"]["requests"]["storage"]
        for resource in resources
        if resource["kind"] == "PersistentVolumeClaim"
    }
    assert pvc_storage == {
        "payload-storage-pvc": "256Mi",
        "postgres-pvc": "1Gi",
        "runtime-env-pvc": "256Mi",
    }


def test_co_resident_profile_defaults_all_container_quota_dimensions(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    resources = rendered_profiles["co_resident"]
    limit_range = _resource(
        resources,
        kind="LimitRange",
        name="django-ray-co-resident-defaults",
    )
    assert limit_range["spec"]["limits"] == [
        {
            "type": "Container",
            "defaultRequest": {
                "cpu": "25m",
                "memory": "32Mi",
                "ephemeral-storage": "64Mi",
            },
            "default": {
                "cpu": "100m",
                "memory": "256Mi",
                "ephemeral-storage": "256Mi",
            },
            "max": {
                "cpu": "500m",
                "memory": "2Gi",
                "ephemeral-storage": "256Mi",
            },
        }
    ]

    init_container_names: set[str] = set()
    for resource in resources:
        if resource["kind"] not in {"Deployment", "Job"}:
            continue
        pod_spec = resource["spec"]["template"]["spec"]
        init_container_names.update(
            container["name"] for container in pod_spec.get("initContainers", [])
        )
    assert init_container_names == {
        "collect-static",
        "run-migrations",
        "wait-for-postgres",
        "wait-for-ray",
        "wait-for-runtime-env",
    }


def test_co_resident_profile_is_cluster_internal_and_recreate_only(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    resources = rendered_profiles["co_resident"]
    for name in ("django-ray-worker", "django-web", "postgres"):
        deployment = _resource(resources, kind="Deployment", name=name)
        assert int(deployment["spec"]["replicas"]) == 1
        assert deployment["spec"]["strategy"] == {"type": "Recreate"}

    assert _resource_names(resources, kind="Ingress") == set()
    services = [resource for resource in resources if resource["kind"] == "Service"]
    assert services
    assert {service["spec"].get("type", "ClusterIP") for service in services} == {"ClusterIP"}
    for forbidden in ("hostNetwork", "hostPort", "nodePort"):
        assert not _contains_key(resources, forbidden)


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


def test_guarded_restart_head_inventories_match_rendered_profiles(
    rendered_profiles: dict[str, list[dict[str, Any]]],
) -> None:
    co_resident_names = tuple(
        container["name"]
        for container in _ray_head_group(rendered_profiles["co_resident"])["template"]["spec"][
            "containers"
        ]
    )
    direct_names = tuple(
        container["name"]
        for container in _ray_head_group(rendered_profiles["direct"])["template"]["spec"][
            "containers"
        ]
    )

    assert ((), co_resident_names) == CO_RESIDENT_RAY_HEAD_CONTAINER_NAMES
    assert ((), direct_names) == GUARDED_GATE_RAY_HEAD_CONTAINER_NAMES


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


@pytest.mark.parametrize("profile", ("direct", "kong"))
def test_local_profiles_do_not_render_bootstrap_credentials(
    rendered_profiles: dict[str, list[dict[str, Any]]],
    profile: str,
) -> None:
    assert _resource_names(rendered_profiles[profile], kind="Secret") == set()


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
    assert recipe.index("k8s-delete-co-resident-policy") < recipe.index(
        "k8s-delete-local-raycluster"
    )
    assert recipe.index("k8s-delete-local-raycluster") < recipe.index(
        "apply -k k8s/overlays/kong-local"
    )
    assert 'kubectl --context "$(K8S_CONTEXT)" apply -k k8s/overlays/kong-local' in recipe
    assert "apply -k k8s/overlays/kuberay-kind" not in recipe
    assert "delete pod -l app=ray,component=head" not in recipe
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

    policy_index = recipe.index("k8s-delete-co-resident-policy")
    delete_index = recipe.index("k8s-delete-local-raycluster")
    apply_index = recipe.index("apply -k k8s/overlays/kuberay-kind")
    assert policy_index < delete_index < apply_index
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

    assert "kubectl $(KUBECTL_CONTEXT_ARG) delete raycluster/ray -n django-ray" in recipe
    assert "--ignore-not-found" in recipe
    assert "--cascade=foreground" in recipe
    assert "--wait=true" in recipe
    assert "--timeout=240s" in recipe
    assert "kubectl $(KUBECTL_CONTEXT_ARG) delete service/ray-head-svc -n django-ray" in recipe


def test_local_kong_uninstall_targets_the_documented_release_and_routes() -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")
    recipe = _make_target_block(makefile, "k8s-uninstall-kong-local")

    assert "helm uninstall $(HELM_CONTEXT_ARG) kong --namespace kong" in recipe
    assert "--ignore-not-found" in recipe
    assert "--wait" in recipe
    assert "--timeout 180s" in recipe
    assert 'kubectl --context "$(K8S_CONTEXT)" delete' in recipe
    for ingress in (
        "ingress/grafana-ingress",
        "ingress/prometheus-ingress",
        "ingress/ray-dashboard-ingress",
    ):
        assert ingress in recipe
    assert "namespace/kong" not in recipe


def test_local_url_targets_use_posix_safe_echo_syntax() -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")
    direct_recipe = _make_target_block(makefile, "k8s-urls")
    kong_recipe = _make_target_block(makefile, "k8s-urls-kong")

    assert 'echo "=== Project URLs ==="' in direct_recipe
    assert 'echo "=== Project URLs (Kong) ==="' in kong_recipe
    for recipe in (direct_recipe, kong_recipe):
        assert "echo." not in recipe
        assert 'echo ""' in recipe


@pytest.mark.parametrize(
    ("caller", "url_target"),
    [
        ("k8s-deploy", "k8s-urls"),
        ("k8s-deploy-local", "k8s-urls"),
        ("k8s-deploy-tls", "k8s-urls"),
        ("k8s-deploy-kuberay-kind", "k8s-urls"),
        ("k8s-deploy-kong-local", "k8s-urls-kong"),
    ],
)
def test_deploy_callers_delegate_to_posix_safe_url_targets(
    caller: str,
    url_target: str,
) -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")
    recipe = _make_target_block(makefile, caller)

    assert f"$(MAKE) --no-print-directory {url_target}" in recipe
