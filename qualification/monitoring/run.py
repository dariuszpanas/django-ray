"""Observe only this job's disposable monitoring namespace and remove it."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from qualification.monitoring.census_diagnostics import CensusError, record
from qualification.monitoring.render import config_digest, pod_limits, render, validate_resources
from qualification.platform_auth import observe as observe_ray_auth

CONTEXT = "kind-monitoring-qualification"
MAX_COMMAND_BYTES = 2 * 1024 * 1024
OWNER_LABEL = "qualification.django-ray/owner"
OPERATOR_IMAGE = "quay.io/kuberay/operator:v1.6.2@sha256:2602383ef61afe1b4c55bdcb24da2ff55ee31b6674cbe53b1583e5219e5e3376"
PHASES = {
    "replacement",
    "replay",
    "browser",
    "preflight",
    "render",
    "create",
    "readiness",
    "ray",
    "ray_auth",
    "http",
    "census",
    "cleanup",
    "complete",
}


def command(
    argv: list[str], *, seconds: float, data: str | None = None, env: dict[str, str] | None = None
) -> str:
    """Bound subprocess time and materialized output; never echo raw failures."""
    if seconds <= 0:
        raise TimeoutError("Monitoring command deadline expired")
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(
            argv,
            input=None if data is None else data.encode(),
            stdout=stdout,
            stderr=stderr,
            timeout=seconds,
            env=env,
            check=False,
        )
        if (
            result.returncode
            or stdout.tell() > MAX_COMMAND_BYTES
            or stderr.tell() > MAX_COMMAND_BYTES
        ):
            raise RuntimeError("Monitoring command failed")
        stdout.seek(0)
        return stdout.read(MAX_COMMAND_BYTES + 1).decode("utf-8")


class Kubernetes:
    def __init__(self, namespace: str, deadline: float):
        self.namespace = namespace
        self.deadline = deadline

    def call(self, *args: str, data: str | None = None, timeout: float = 30) -> str:
        seconds = min(timeout, self.deadline - time.monotonic())
        return command(
            ["kubectl", "--context", CONTEXT, "--request-timeout=20s", "-n", self.namespace, *args],
            seconds=seconds,
            data=data,
        )

    def get(self, resource: str) -> dict:
        return json.loads(self.call("get", resource, "-o", "json"))


def secret_documents(namespace: str) -> list[dict]:
    """Generate unrelated credentials and pass them only through kubectl stdin."""
    password = secrets.token_urlsafe(48)
    values = {
        "django-ray-runtime": {
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
            "DATABASE_USER": "django_ray",
            "DATABASE_PASSWORD": password,
        },
        "django-ray-database": {
            "POSTGRES_USER": "django_ray",
            "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": "django_ray",
        },
        "django-ray-secret": {"DJANGO_API_TOKEN": secrets.token_urlsafe(48)},
        "django-ray-metrics": {"DJANGO_METRICS_TOKEN": secrets.token_urlsafe(48)},
        "django-ray-demo": {"DJANGO_DEMO_TOKEN": secrets.token_urlsafe(48)},
        "django-ray-auth": {"RAY_AUTH_TOKEN": secrets.token_urlsafe(48)},
        "django-ray-grafana": {
            "GF_SECURITY_ADMIN_USER": "monitoring_admin",
            "GF_SECURITY_ADMIN_PASSWORD": secrets.token_urlsafe(48),
        },
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": "Opaque",
            "stringData": value,
        }
        for name, value in values.items()
    ]


def pods_ready(payload: dict) -> bool:
    pods = payload.get("items", [])
    if len(pods) != 6:
        return False
    return all(
        pod.get("status", {}).get("phase") == "Running"
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
        for pod in pods
    )


def census(payload: dict, images: dict[str, str]) -> list[dict]:
    """Reject missing/extra components, restarts and identity mismatches."""
    if not pods_ready(payload):
        # Classify only the existing failed predicate, using the same snapshot.
        # Keep pods_ready's acceptance and short-circuit ordering unchanged.
        pods = payload.get("items", [])
        reason = "pod_count" if len(pods) != 6 else "unclassified"
        if len(pods) == 6:
            for pod in pods:
                if pod.get("status", {}).get("phase") != "Running":
                    reason = "pod_phase"
                    break
                if not any(
                    condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in pod.get("status", {}).get("conditions", [])
                ):
                    reason = "pod_ready_condition"
                    break
        raise CensusError(reason, "Monitoring pod census is not ready")
    expected = {
        "django-web": "application",
        "postgres": "postgres",
        "prometheus": "prometheus",
        "grafana": "grafana",
        "ray-head": "ray",
        "ray-worker": "ray",
        "dashboard-importer": "importer",
    }
    observed: list[dict] = []
    seen: set[str] = set()
    for pod in payload["items"]:
        statuses = {item["name"]: item for item in pod["status"].get("containerStatuses", [])}
        for container in pod["spec"]["containers"]:
            name = container["name"]
            status = statuses.get(name, {})
            image_id = status.get("imageID", "")
            message = "Monitoring container census differs"
            if name not in expected:
                raise CensusError("unexpected_component", message)
            if name in seen:
                raise CensusError("duplicate_component", message)
            if container["image"] != images[expected[name]]:
                raise CensusError("spec_image_mismatch", message)
            if status.get("restartCount") != 0:
                reason = (
                    "container_restart_count" if name in statuses else "missing_container_status"
                )
                raise CensusError(reason, message)
            if not status.get("ready"):
                raise CensusError("container_not_ready", message)
            if re.fullmatch(r"[a-zA-Z0-9./:_@-]{0,240}sha256:[0-9a-f]{64}", image_id) is None:
                raise CensusError("image_id_invalid", message)
            seen.add(name)
            observed.append(
                {
                    "component": name,
                    "image": container["image"],
                    "image_id": image_id,
                    "ready": True,
                    "restarts": 0,
                }
            )
        for status in pod["status"].get("initContainerStatuses", []):
            message = "Monitoring initialization did not finish cleanly"
            if status.get("restartCount") != 0:
                raise CensusError("init_restart_count", message)
            if status.get("state", {}).get("terminated", {}).get("exitCode") != 0:
                raise CensusError("init_not_successful", message)
    if seen != set(expected):
        raise CensusError("component_set_incomplete", "Monitoring component census is incomplete")
    return sorted(observed, key=lambda item: item["component"])


def final_pod_census(kube: Kubernetes, images: dict[str, str], receipt: dict) -> dict:
    """Retain a failure code without another query or changing acceptance."""
    try:
        live_pods = kube.get("pods")
    except Exception:
        receipt["census_diagnostic"] = record("pod_fetch", "query_failed")
        raise
    try:
        receipt["containers"] = census(live_pods, images)
    except CensusError as error:
        receipt["census_diagnostic"] = record("pod_census", error.reason)
        raise
    except (AttributeError, KeyError, TypeError, IndexError):
        receipt["census_diagnostic"] = record("pod_census", "malformed_payload")
        raise
    except Exception:
        receipt["census_diagnostic"] = record("pod_census", "unclassified")
        raise
    return live_pods


def surfaces(services: dict, ingresses: dict, deployment: dict, config: dict) -> None:
    """Assert live private service and effective shipped probe contracts."""
    if ingresses.get("items") or not services.get("items"):
        raise ValueError("Unexpected monitoring ingress surface")
    for service in services["items"]:
        spec = service["spec"]
        if (
            spec.get("type", "ClusterIP") != "ClusterIP"
            or spec.get("externalIPs")
            or any(port.get("nodePort") for port in spec["ports"])
        ):
            raise ValueError("Monitoring service is not private")
    pod = deployment["spec"]["template"]["spec"]
    if pod.get("hostNetwork") or any(
        port.get("hostPort")
        for container in pod["containers"]
        for port in container.get("ports", [])
    ):
        raise ValueError("Monitoring pod exposes host ports")
    allowed = config["data"]["DJANGO_ALLOWED_HOSTS"].split(",")
    for key in ("livenessProbe", "readinessProbe"):
        probe = pod["containers"][0][key]["httpGet"]
        if (
            probe["path"] != "/api/health"
            or probe.get("httpHeaders") != [{"name": "Host", "value": "django-ray.localhost"}]
            or "django-ray.localhost" not in allowed
            or "django-web-svc" not in allowed
        ):
            raise ValueError("Shipped probe Host contract differs")


def cleanup_namespace(kube: Kubernetes, namespace: str, owner: str, uid: str | None) -> bool:
    """Delete only the owned identity, including an ambiguous create outcome."""
    raw = kube.call("get", "namespace", namespace, "--ignore-not-found", "-o", "json")
    if not raw.strip():
        return True
    existing = json.loads(raw)["metadata"]
    if existing.get("labels", {}).get(OWNER_LABEL) != owner or (
        uid is not None and existing["uid"] != uid
    ):
        return False
    kube.call("delete", "namespace", namespace, "--wait=true", "--timeout=210s", timeout=220)
    return not kube.call("get", "namespace", namespace, "--ignore-not-found", "-o", "name").strip()


def operator_census(payload: dict) -> dict:
    pods = payload.get("items", [])
    if len(pods) != 1 or pod_limits(pods[0]["spec"]) != (100, 512):
        raise ValueError("Operator envelope differs")
    pod = pods[0]
    containers = pod["spec"]["containers"]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if (
        len(containers) != 1
        or len(statuses) != 1
        or containers[0]["image"] != OPERATOR_IMAGE
        or statuses[0].get("restartCount") != 0
        or not statuses[0].get("ready")
    ):
        raise ValueError("Operator identity or readiness differs")
    image_id = statuses[0].get("imageID", "")
    if re.fullmatch(r"[a-zA-Z0-9./:_@-]{0,240}sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError("Operator image identity unavailable")
    return {
        "image": OPERATOR_IMAGE,
        "image_id": image_id,
        "ready": True,
        "restarts": 0,
        "version": "1.6.2",
        "cpu_millicores": 100,
        "memory_mib": 512,
    }


def run(images: dict[str, str], output: Path) -> int:
    receipt: dict = {
        "status": "failed",
        "phase": "preflight",
        "namespace_removed": False,
        "cold_ray": "skip",
        "complete_application_gate": False,
    }
    namespace = "monitoring-" + os.environ.get("GITHUB_RUN_ID", "")
    owner = secrets.token_hex(16)
    namespace_uid = None
    kube = Kubernetes(namespace, time.monotonic() + 600)
    output.mkdir(parents=True, exist_ok=False)
    try:
        if (
            os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        ):
            raise ValueError("Monitoring qualification requires a disposable hosted runner")
        source = command(["git", "rev-parse", "HEAD", "HEAD^{tree}"], seconds=5).splitlines()
        if source[0] != os.environ.get("SOURCE_SHA"):
            raise ValueError("Candidate source does not match checkout")
        command(["git", "diff", "--exit-code", "HEAD", "--"], seconds=5)
        receipt.update(source_head=source[0], source_tree=source[1])
        receipt["phase"] = "render"
        documents = list(
            yaml.safe_load_all(
                command(["kubectl", "kustomize", "k8s/overlays/kuberay-kind"], seconds=30)
            )
        )
        manifest = render(documents, namespace, images)
        receipt["rendered_config_sha256"] = config_digest(manifest)
        receipt["phase"] = "create"
        # Create, never apply/adopt a pre-existing namespace. The random owner
        # label also identifies a successful create after an ambiguous timeout.
        ns = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": {OWNER_LABEL: owner}},
        }
        kube.call("create", "-f", "-", data=json.dumps(ns))
        namespace_uid = kube.get("namespace/" + namespace)["metadata"]["uid"]
        credentials = secret_documents(namespace)
        grafana_credentials = next(
            doc["stringData"]
            for doc in credentials
            if doc["metadata"]["name"] == "django-ray-grafana"
        )
        kube.call("apply", "-f", "-", data=yaml.safe_dump_all(credentials))
        kube.call("apply", "-f", "-", data=yaml.safe_dump_all(manifest))
        receipt["phase"] = "readiness"
        while not pods_ready(kube.get("pods")):
            if kube.deadline - time.monotonic() <= 0:
                raise TimeoutError("Monitoring readiness deadline expired")
            time.sleep(2)
        receipt["phase"] = "ray"
        pods = kube.get("pods")
        head = next(
            pod["metadata"]["name"]
            for pod in pods["items"]
            if pod["metadata"]["labels"].get("component") == "head"
        )
        ray_script = Path(__file__).with_name("ray_probe.py").read_text(encoding="utf-8")
        receipt["ray"] = json.loads(
            kube.call("exec", head, "-c", "ray-head", "--", "python", "-c", ray_script, timeout=60)
        )
        receipt["phase"] = "http"
        seconds = min(60, kube.deadline - time.monotonic() - 5)
        receipt["http"] = json.loads(
            kube.call(
                "exec",
                "deployment/django-web",
                "-c",
                "django-web",
                "--",
                "python",
                "-m",
                "qualification.monitoring.probe",
                str(seconds),
                timeout=seconds + 5,
            )
        )
        if receipt["http"].get("status") != "passed":
            raise ValueError("Monitoring HTTP receipt did not pass")
        receipt["phase"] = "replacement"
        before = kube.get("pods")["items"]
        ray_uids = {
            pod["metadata"]["uid"]
            for pod in before
            if pod["metadata"]["labels"].get("app") == "ray"
        }
        grafana = next(pod for pod in before if pod["metadata"]["labels"].get("app") == "grafana")
        kube.call(
            "delete", "pod", grafana["metadata"]["name"], "--wait=true", "--timeout=30s", timeout=35
        )
        while not pods_ready(kube.get("pods")):
            time.sleep(2)
        after = kube.get("pods")["items"]
        if (
            len(ray_uids) != 2
            or ray_uids
            != {
                pod["metadata"]["uid"]
                for pod in after
                if pod["metadata"]["labels"].get("app") == "ray"
            }
            or grafana["metadata"]["uid"] in {pod["metadata"]["uid"] for pod in after}
        ):
            raise ValueError("Grafana-only replacement identity differs")
        receipt["replacement"] = json.loads(
            kube.call(
                "exec",
                "deployment/django-web",
                "-c",
                "django-web",
                "--",
                "python",
                "-m",
                "qualification.monitoring.probe",
                "dashboard-reset",
                timeout=25,
            )
        )
        receipt["phase"] = "replay"
        kube.call(
            "exec",
            head,
            "-c",
            "dashboard-importer",
            "--",
            "python",
            "/scripts/import_dashboards.py",
            "--once",
            timeout=60,
        )
        seconds = min(60, kube.deadline - time.monotonic() - 5)
        receipt["after_replay"] = json.loads(
            kube.call(
                "exec",
                "deployment/django-web",
                "-c",
                "django-web",
                "--",
                "python",
                "-m",
                "qualification.monitoring.probe",
                str(seconds),
                timeout=seconds + 5,
            )
        )
        if receipt["after_replay"].get("status") != "passed":
            raise ValueError("Dashboard replay did not restore monitoring")
        receipt["phase"] = "ray_auth"
        receipt["ray_auth"] = observe_ray_auth(kube, head)
        receipt["phase"] = "browser"
        from qualification.monitoring.host_browser import observe

        receipt["browser"] = observe(
            namespace=namespace,
            image=images["application"],
            credentials=grafana_credentials,
            deadline=kube.deadline,
        )
        receipt["phase"] = "census"
        live_pods = final_pod_census(kube, images, receipt)
        receipt["live_resources"] = validate_resources(live_pods["items"])
        receipt["operator"] = operator_census(
            json.loads(kube.call("get", "pods", "--namespace=kuberay-system", "-o", "json"))
        )
        surfaces(
            kube.get("services"),
            kube.get("ingresses"),
            kube.get("deployment/django-web"),
            kube.get("configmap/django-ray-config"),
        )
        receipt.update(status="passed", phase="complete", private_surfaces=True)
    except Exception:
        receipt["failure_phase"] = receipt["phase"] if receipt["phase"] in PHASES else "preflight"
        receipt["status"] = "failed"
    finally:
        kube.deadline = time.monotonic() + 240
        try:
            receipt["namespace_removed"] = cleanup_namespace(kube, namespace, owner, namespace_uid)
            if not receipt["namespace_removed"]:
                raise ValueError("Owned namespace cleanup was not confirmed")
        except Exception:
            receipt["status"] = "failed"
            receipt.setdefault("failure_phase", "cleanup")
        (output / "summary.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {key: receipt.get(key) for key in ("status", "failure_phase", "namespace_removed")},
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] == "passed" and receipt["namespace_removed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.images.stat().st_size > 4096:
        raise SystemExit("Monitoring image inventory exceeds its bound")
    return run(json.loads(args.images.read_text(encoding="utf-8")), args.output)


if __name__ == "__main__":
    raise SystemExit(main())
