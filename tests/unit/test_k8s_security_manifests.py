"""Static security & reliability invariants for Kubernetes, Dockerfiles, and helper scripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_yaml_docs(rel_path: str) -> list[dict[str, Any]]:
    raw = (ROOT / rel_path).read_text(encoding="utf-8")
    return [doc for doc in yaml.safe_load_all(raw) if isinstance(doc, dict)]


def _iter_pod_specs(resource: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    kind = resource.get("kind")
    name = resource.get("metadata", {}).get("name", "<unnamed>")
    spec = resource.get("spec", {})
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return [(f"{kind}/{name}", spec["template"]["spec"])]
    if kind == "RayCluster":
        pods: list[tuple[str, dict[str, Any]]] = [
            (f"RayCluster/{name}:head", spec["headGroupSpec"]["template"]["spec"])
        ]
        for group in spec.get("workerGroupSpecs", []):
            group_name = group.get("groupName", "worker")
            pods.append((f"RayCluster/{name}:worker:{group_name}", group["template"]["spec"]))
        return pods
    return []


class TestRayPodSecretIsolation:
    """Ray compute pods must never hold DJANGO_SECRET_KEY."""

    @pytest.mark.parametrize(
        "manifest_path",
        [
            "k8s/base/ray-cluster.yaml",
            "k8s/overlays/kuberay-kind/ray-cluster-kuberay.yaml",
            "k8s/overlays/co-resident/ray-cluster.yaml",
        ],
    )
    def test_ray_pods_strip_django_secret_key(self, manifest_path: str) -> None:
        docs = _load_yaml_docs(manifest_path)
        ray_containers_checked = 0
        for doc in docs:
            for label, pod_spec in _iter_pod_specs(doc):
                for container in pod_spec.get("containers", []):
                    env_names = {entry.get("name") for entry in container.get("env", [])}
                    assert "DJANGO_SECRET_KEY" not in env_names, (
                        f"{manifest_path} {label}/{container['name']} must not set "
                        "DJANGO_SECRET_KEY"
                    )
                    if container["name"] in {"ray-head", "ray-worker"}:
                        ray_containers_checked += 1
        assert ray_containers_checked >= 2


class TestRayPayloadMountSeparation:
    """Ray pods keep /payload-storage read-only while isolating writable results."""

    @pytest.mark.parametrize(
        "manifest_path",
        [
            "k8s/base/ray-cluster.yaml",
            "k8s/overlays/kuberay-kind/ray-cluster-kuberay.yaml",
            "k8s/overlays/co-resident/ray-cluster.yaml",
        ],
    )
    def test_ray_pods_separate_ro_inputs_from_rw_results_mount(self, manifest_path: str) -> None:
        docs = _load_yaml_docs(manifest_path)
        checked = 0
        for doc in docs:
            for _label, pod_spec in _iter_pod_specs(doc):
                for container in pod_spec.get("containers", []):
                    if container["name"] not in {"ray-head", "ray-worker"}:
                        continue
                    checked += 1
                    mounts = container.get("volumeMounts", [])
                    payload_mount = next(m for m in mounts if m["name"] == "payload-storage")
                    assert payload_mount == {
                        "name": "payload-storage",
                        "mountPath": "/payload-storage",
                        "readOnly": True,
                    }
                    result_mount = next(m for m in mounts if m["name"] == "result-storage")
                    assert result_mount == {
                        "name": "result-storage",
                        "mountPath": "/payload-storage/results",
                        "subPath": "results",
                        "readOnly": False,
                    }
        assert checked >= 2

    def test_base_configmap_configures_filesystem_result_storage_path(self) -> None:
        config = _load_yaml_docs("k8s/base/configmap.yaml")[0]
        assert config["data"]["DJANGO_RAY_INPUT_STORAGE_FILESYSTEM_PATH"] == (
            "/payload-storage/inputs"
        )
        assert config["data"]["DJANGO_RAY_RESULT_STORAGE_BACKEND"] == "filesystem"
        assert config["data"]["DJANGO_RAY_RESULT_STORAGE_FILESYSTEM_PATH"] == (
            "/payload-storage/results"
        )


class TestKubernetesSecurityContextAndPolicies:
    """K8s workloads enforce securityContext, token opt-out, NetworkPolicy, and probes."""

    @pytest.mark.parametrize(
        "manifest_path",
        [
            "k8s/base/django-web.yaml",
            "k8s/base/django-ray-worker.yaml",
            "k8s/base/django-setup-job.yaml",
            "k8s/base/postgres.yaml",
            "k8s/base/ray-cluster.yaml",
            "k8s/base/monitoring.yaml",
            "k8s/overlays/kuberay-kind/ray-cluster-kuberay.yaml",
            "k8s/overlays/kuberay-kind/worker-sync.yaml",
            "k8s/overlays/kuberay-kind/worker-ml.yaml",
            "k8s/overlays/kuberay-kind/worker-ray-job.yaml",
            "k8s/overlays/co-resident/ray-cluster.yaml",
            "k8s/overlays/dev/worker-all-queues.yaml",
            "k8s/overlays/dev/worker-ml.yaml",
        ],
    )
    def test_pods_enforce_seccomp_least_privilege_and_sa_token_opt_out(
        self, manifest_path: str
    ) -> None:
        for doc in _load_yaml_docs(manifest_path):
            for label, pod_spec in _iter_pod_specs(doc):
                pod_sc = pod_spec.get("securityContext", {})
                assert pod_sc.get("seccompProfile", {}).get("type") == "RuntimeDefault", (
                    f"{manifest_path} {label} missing RuntimeDefault seccompProfile"
                )
                if doc.get("metadata", {}).get("name") != "postgres":
                    assert pod_sc.get("runAsNonRoot") is True, (
                        f"{manifest_path} {label} missing runAsNonRoot: true"
                    )
                if doc.get("metadata", {}).get("name") != "prometheus":
                    assert pod_spec.get("automountServiceAccountToken") is False, (
                        f"{manifest_path} {label} must disable automountServiceAccountToken"
                    )
                for container in pod_spec.get("containers", []):
                    c_sc = container.get("securityContext", {})
                    assert c_sc.get("allowPrivilegeEscalation") is False, (
                        f"{manifest_path} {label}/{container['name']} must disable "
                        "allowPrivilegeEscalation"
                    )

    def test_prometheus_disables_lifecycle_and_dashboard_importer_is_read_only(self) -> None:
        monitoring = _load_yaml_docs("k8s/base/monitoring.yaml")
        prometheus = next(
            doc
            for doc in monitoring
            if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "prometheus"
        )
        prom_args = prometheus["spec"]["template"]["spec"]["containers"][0]["args"]
        assert "--web.enable-lifecycle" not in prom_args

        for manifest_path in (
            "k8s/base/ray-cluster.yaml",
            "k8s/overlays/kuberay-kind/ray-cluster-kuberay.yaml",
        ):
            docs = _load_yaml_docs(manifest_path)
            found = False
            for doc in docs:
                for _label, pod_spec in _iter_pod_specs(doc):
                    for container in pod_spec.get("containers", []):
                        if container["name"] == "dashboard-importer":
                            found = True
                            for mount in container["volumeMounts"]:
                                assert mount.get("readOnly") is True, (
                                    f"{manifest_path} dashboard-importer mount "
                                    f"{mount['mountPath']} must be readOnly"
                                )
                            assert container["securityContext"]["readOnlyRootFilesystem"] is True
            assert found

    def test_network_policies_include_default_deny_ingress(self) -> None:
        policies = _load_yaml_docs("k8s/base/network-policy.yaml")
        assert all(doc["kind"] == "NetworkPolicy" for doc in policies)
        default_deny = next(
            doc for doc in policies if doc["metadata"]["name"] == "default-deny-ingress"
        )
        assert default_deny["spec"]["podSelector"] == {}
        assert default_deny["spec"]["policyTypes"] == ["Ingress"]

        base_kustomization = _load_yaml_docs("k8s/base/kustomization.yaml")[0]
        assert "network-policy.yaml" in base_kustomization["resources"]

    def test_workers_define_grace_period_non_zombie_liveness_and_sync_concurrency_one(self) -> None:
        worker_files = (
            "k8s/base/django-ray-worker.yaml",
            "k8s/overlays/kuberay-kind/worker-sync.yaml",
            "k8s/overlays/kuberay-kind/worker-ml.yaml",
            "k8s/overlays/kuberay-kind/worker-ray-job.yaml",
            "k8s/overlays/dev/worker-ml.yaml",
        )
        for manifest_path in worker_files:
            deployment = _load_yaml_docs(manifest_path)[0]
            pod_spec = deployment["spec"]["template"]["spec"]
            assert pod_spec.get("terminationGracePeriodSeconds", 0) >= 60
            container = pod_spec["containers"][0]
            liveness_cmd = " ".join(container["livenessProbe"]["exec"]["command"])
            assert "/proc/1/stat" in liveness_cmd
            assert "django_ray_worker" in liveness_cmd
            assert "django_ray_worker_ready" not in liveness_cmd
            if manifest_path == "k8s/base/django-ray-worker.yaml":
                assert "/proc/1/status" in liveness_cmd
                assert "voluntary|nonvoluntary" in liveness_cmd
                assert '"D"' in liveness_cmd
                assert '"T"' in liveness_cmd

        sync_worker = _load_yaml_docs("k8s/overlays/kuberay-kind/worker-sync.yaml")[0]
        args = sync_worker["spec"]["template"]["spec"]["containers"][0]["args"]
        concurrency_idx = args.index("--concurrency")
        assert args[concurrency_idx + 1] == "1"


class TestDockerAndScriptHardening:
    """Dockerfiles and operational helper scripts enforce supply-chain and secret hygiene."""

    def test_dockerfiles_enforce_locked_installs_and_non_root_runtime(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY --from=builder --chown=root:root /app/.venv /app/.venv" in dockerfile
        assert "COPY --chown=root:root src/ src/" in dockerfile
        assert "USER django" in dockerfile

        dockerfile_dev = (ROOT / "Dockerfile.dev").read_text(encoding="utf-8")
        assert "USER django" in dockerfile_dev

        dockerfile_ray = (ROOT / "Dockerfile.ray").read_text(encoding="utf-8")
        assert "uv export --frozen" in dockerfile_ray
        assert "--require-hashes" in dockerfile_ray
        assert "uv pip install --system -e" not in dockerfile_ray
        assert "USER ray" in dockerfile_ray

    def test_tls_cert_script_hardens_permissions_constraints_and_never_prints_key(self) -> None:
        script = (ROOT / "scripts/generate-ray-tls-certs.sh").read_text(encoding="utf-8")
        assert "set -euo pipefail" in script
        assert "umask 077" in script
        assert "basicConstraints = critical, CA:TRUE" in script
        assert "keyUsage = critical, keyCertSign, cRLSign" in script
        assert "ray-head-svc" in script
        assert "base64" not in script
        assert "tls.key:" not in script

    def test_standalone_import_dashboards_script_requires_auth_and_validates_url(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        spec = importlib.util.spec_from_file_location(
            "standalone_import_dashboards",
            ROOT / "scripts/import_dashboards.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        dashboard_file = tmp_path / "ray_dashboard.json"
        dashboard_file.write_text(json.dumps({"uid": "ray-uid", "panels": []}), encoding="utf-8")

        monkeypatch.delenv("GF_SECURITY_ADMIN_USER", raising=False)
        monkeypatch.delenv("GF_SECURITY_ADMIN_PASSWORD", raising=False)
        urlopen = Mock(side_effect=AssertionError("must not call HTTP without auth"))
        monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
        assert module.import_dashboard(dashboard_file) is False
        urlopen.assert_not_called()

        monkeypatch.setenv("GF_SECURITY_ADMIN_USER", "admin")
        monkeypatch.setenv("GF_SECURITY_ADMIN_PASSWORD", "secret-pass")
        monkeypatch.setattr(module, "GRAFANA_URL", "file:///etc/passwd")
        assert module.import_dashboard(dashboard_file) is False
        urlopen.assert_not_called()
