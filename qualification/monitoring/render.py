"""Derive a small owned topology from the shipped Kustomize render."""

from __future__ import annotations

import copy
import hashlib
import json
import re

import yaml

NAMESPACE_PATTERN = r"monitoring-[0-9]{1,20}"
IMAGE_PATTERN = r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}"
ALLOW = {
    "ConfigMap": {
        "django-ray-config",
        "prometheus-config",
        "grafana-datasources",
        "grafana-dashboards-provider",
        "grafana-dashboards",
        "grafana-dashboard-import-script",
    },
    "Deployment": {"postgres", "django-web", "prometheus", "grafana"},
    "Service": {
        "postgres-svc",
        "django-web-svc",
        "prometheus-svc",
        "grafana-svc",
        "ray-dashboard-svc",
    },
    "ServiceAccount": {"prometheus"},
    "Role": {"prometheus-django-ray"},
    "RoleBinding": {"prometheus-django-ray"},
    "RayCluster": {"ray"},
}


def resources(cpu: str, memory: str) -> dict:
    """Fixed requests/limits for fixture-only application components."""
    return {
        key: {"cpu": cpu, "memory": memory, "ephemeral-storage": "1Gi"}
        for key in ("requests", "limits")
    }


def render(documents: list[dict], namespace: str, images: dict[str, str]) -> list[dict]:
    """Preserve shipped selectors, auth, probes and monitoring resources."""
    if re.fullmatch(NAMESPACE_PATTERN, namespace) is None:
        raise ValueError("Invalid owned namespace")
    if set(images) != {
        "application",
        "ray",
        "postgres",
        "prometheus",
        "grafana",
        "importer",
    } or any(re.fullmatch(IMAGE_PATTERN, image) is None for image in images.values()):
        raise ValueError("Every fixture image must be immutable")
    selected = [
        copy.deepcopy(doc)
        for doc in documents
        if doc.get("metadata", {}).get("name") in ALLOW.get(doc.get("kind"), set())
    ]
    expected = {(kind, name) for kind, names in ALLOW.items() for name in names}
    if {(doc["kind"], doc["metadata"]["name"]) for doc in selected} != expected or len(
        selected
    ) != len(expected):
        raise ValueError("Shipped monitoring topology changed")
    for doc in selected:
        doc["metadata"]["namespace"] = namespace
        kind, name = doc["kind"], doc["metadata"]["name"]
        if kind == "RoleBinding":
            for subject in doc["subjects"]:
                subject["namespace"] = namespace
        if kind == "ConfigMap" and name == "prometheus-config":
            config = yaml.safe_load(doc["data"]["prometheus.yml"])
            jobs = {job["job_name"]: job for job in config["scrape_configs"]}
            jobs["ray-workers"]["kubernetes_sd_configs"][0]["namespaces"]["names"] = [namespace]
            doc["data"]["prometheus.yml"] = yaml.safe_dump(config, sort_keys=False)
        if kind == "ConfigMap" and name == "django-ray-config":
            # One worker and explicit lifecycle match the finite application
            # profile. These are fixture substitutions, not sample defaults.
            doc["data"].update(GUNICORN_MAX_REQUESTS="0", GUNICORN_MAX_REQUESTS_JITTER="0")
        if kind == "Deployment":
            pod = doc["spec"]["template"]["spec"]
            container = pod["containers"][0]
            if name in {"prometheus", "grafana"}:
                container["image"] = images[name]
            elif name == "postgres":
                container["image"] = images["postgres"]
                container["resources"] = resources("200m", "512Mi")
            else:
                pod.setdefault("securityContext", {})["fsGroup"] = 1000
                container["image"] = images["application"]
                container["resources"] = resources("250m", "768Mi")
                for env in container["env"]:
                    if env["name"] in {"GUNICORN_WORKERS", "GUNICORN_THREADS"}:
                        env["value"] = "1" if env["name"] == "GUNICORN_WORKERS" else "2"
                # The observer executes inside this existing resource envelope.
                container["env"].extend(
                    [
                        {
                            "name": key,
                            "valueFrom": {
                                "secretKeyRef": {"name": "django-ray-grafana", "key": key}
                            },
                        }
                        for key in ("GF_SECURITY_ADMIN_USER", "GF_SECURITY_ADMIN_PASSWORD")
                    ]
                )
                for init in pod["initContainers"]:
                    init["image"] = images["application"]
                    init["resources"] = resources("250m", "768Mi")
                    if init["name"] == "wait-for-postgres":
                        init["command"] = [
                            "python",
                            "-m",
                            "qualification.monitoring.probe",
                            "wait-database",
                        ]
            _owned_volumes(pod)
        if kind == "RayCluster":
            _ray(doc["spec"], images)
    validate_resources(selected)
    selected.insert(
        0,
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "monitoring-envelope", "namespace": namespace},
            "spec": {"hard": {"limits.cpu": "2750m", "limits.memory": "8832Mi", "pods": "6"}},
        },
    )
    return selected


def pod_limits(pod: dict) -> tuple[int, int]:
    """Calculate effective pod limits including ordinary init containers."""

    def values(container):
        limits = container["resources"]["limits"]
        cpu, memory = limits["cpu"], limits["memory"]
        if (
            re.fullmatch(r"[0-9]+m", cpu) is None
            or re.fullmatch(r"[0-9]+(?:Mi|Gi)", memory) is None
        ):
            raise ValueError("Unexpected monitoring resource quantity")
        return int(cpu[:-1]), int(memory[:-2]) * (1024 if memory.endswith("Gi") else 1)

    main = [values(container) for container in pod["containers"]]
    init = []
    for container in pod.get("initContainers", []):
        if container.get("restartPolicy") == "Always":
            raise ValueError("Review injected restartable init resource accounting")
        init.append(values(container))
    if pod.get("overhead"):
        raise ValueError("Review unexpected pod resource overhead")
    return tuple(
        max([sum(item[index] for item in main), *(item[index] for item in init)])
        for index in (0, 1)
    )


def validate_resources(documents: list[dict]) -> dict:
    """Fail closed if source changes exceed the admitted fixture envelope."""
    pods = []
    for doc in documents:
        if doc["kind"] in {"Deployment", "Pod"}:
            pods.append(
                doc["spec"]["template"]["spec"] if doc["kind"] == "Deployment" else doc["spec"]
            )
        elif doc["kind"] == "RayCluster":
            pods.append(doc["spec"]["headGroupSpec"]["template"]["spec"])
            pods.extend(group["template"]["spec"] for group in doc["spec"]["workerGroupSpecs"])
    limits = [pod_limits(pod) for pod in pods]
    cpu, memory = sum(item[0] for item in limits), sum(item[1] for item in limits)
    if len(pods) != 6 or cpu > 2750 or memory > 8832:
        raise ValueError("Rendered or live pods exceed the admitted monitoring envelope")
    return {"cpu_millicores": cpu, "memory_mib": memory, "pods": len(pods)}


def _owned_volumes(pod: dict) -> None:
    # Nothing here mounts an existing PVC or host path. Static, database,
    # runtime and monitor data die with the namespace.
    for volume in pod.get("volumes", []):
        if "persistentVolumeClaim" in volume or "emptyDir" in volume:
            volume.pop("persistentVolumeClaim", None)
            volume["emptyDir"] = {"sizeLimit": "1Gi"}


def _ray(spec: dict, images: dict[str, str]) -> None:
    if spec["rayVersion"] != "2.58.0" or len(spec["workerGroupSpecs"]) != 1:
        raise ValueError("Review the standard Ray profile before qualification")
    groups = [spec["headGroupSpec"], spec["workerGroupSpecs"][0]]
    groups[1].update(replicas=1, minReplicas=1, maxReplicas=1)
    for index, group in enumerate(groups):
        group["rayStartParams"].update(
            {"num-cpus": "1", "num-gpus": "0", "object-store-memory": "134217728"}
        )
        pod = group["template"]["spec"]
        pod.setdefault("securityContext", {})["fsGroup"] = 1000
        for container in pod["containers"]:
            if container["name"] == "dashboard-importer":
                container["image"] = images["importer"]
            else:
                container["image"] = images["ray"]
                container["resources"] = resources("750m", "5120Mi" if index == 0 else "1536Mi")
                container.setdefault("volumeMounts", []).append(
                    {"name": "shm", "mountPath": "/dev/shm"}
                )
        _owned_volumes(pod)
        pod["volumes"].append(
            {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "128Mi"}}
        )


def config_digest(documents: list[dict]) -> str:
    """Hash only non-secret rendered configuration for source matching."""
    if any(doc.get("kind") == "Secret" for doc in documents):
        raise ValueError("Never retain credentials in configuration evidence")
    return hashlib.sha256(
        json.dumps(documents, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
