"""Regression tests for Django web probes rendered by Kustomize."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from django.core.exceptions import DisallowedHost
from django.test import Client, RequestFactory, override_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KUSTOMIZATIONS = (
    Path("k8s/base"),
    Path("k8s/overlays/dev"),
    Path("k8s/overlays/dev-tls"),
    Path("k8s/overlays/local"),
    Path("k8s/overlays/kuberay-kind"),
    Path("k8s/overlays/kong-local"),
)
REQUIRE_KUSTOMIZE_ENV = "DJANGO_RAY_REQUIRE_KUSTOMIZE_PROBE_TESTS"


def _kubectl_executable() -> str:
    executable = shutil.which("kubectl")
    if executable is not None:
        return executable
    if os.environ.get(REQUIRE_KUSTOMIZE_ENV, "").lower() in {"1", "true", "yes"}:
        pytest.fail(f"kubectl is required when {REQUIRE_KUSTOMIZE_ENV} is enabled")
    pytest.skip("kubectl is not installed; skipping Kustomize probe regression tests")


@pytest.fixture(scope="session", params=KUSTOMIZATIONS, ids=str)
def rendered_kustomization(request: pytest.FixtureRequest) -> tuple[Path, list[dict[str, Any]]]:
    path: Path = request.param
    result = subprocess.run(
        [_kubectl_executable(), "kustomize", str(path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    resources = [resource for resource in yaml.safe_load_all(result.stdout) if resource]
    return path, resources


def _resource(resources: list[dict[str, Any]], *, kind: str, name: str) -> dict[str, Any]:
    matches = [
        resource
        for resource in resources
        if resource.get("kind") == kind and resource.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, found {len(matches)}"
    return matches[0]


def _web_container(resources: list[dict[str, Any]]) -> dict[str, Any]:
    deployment = _resource(resources, kind="Deployment", name="django-web")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return next(container for container in containers if container["name"] == "django-web")


def _allowed_hosts(resources: list[dict[str, Any]]) -> list[str]:
    config_map = _resource(resources, kind="ConfigMap", name="django-ray-config")
    return [host.strip() for host in config_map["data"]["DJANGO_ALLOWED_HOSTS"].split(",")]


@pytest.mark.django_db
def test_http_web_probes_send_a_host_django_accepts(
    rendered_kustomization: tuple[Path, list[dict[str, Any]]],
) -> None:
    """Exercise each rendered HTTP probe through Django's host validation."""
    path, resources = rendered_kustomization
    container = _web_container(resources)
    allowed_hosts = _allowed_hosts(resources)
    http_probes: list[tuple[str, dict[str, Any]]] = []

    for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
        probe = container.get(probe_name)
        if probe is not None and "httpGet" in probe:
            http_probes.append((probe_name, probe["httpGet"]))

    assert http_probes, f"{path} must retain at least one HTTP application-startup probe"
    assert "*" not in allowed_hosts

    with override_settings(ALLOWED_HOSTS=allowed_hosts):
        for probe_name, http_get in http_probes:
            host_headers = [
                header["value"]
                for header in http_get.get("httpHeaders", [])
                if header["name"].lower() == "host"
            ]
            assert len(host_headers) == 1, f"{path} {probe_name} must send one Host header"

            request = RequestFactory().get(http_get["path"], HTTP_HOST=host_headers[0])
            assert request.get_host() == host_headers[0]

            response = Client().get(http_get["path"], HTTP_HOST=host_headers[0])
            assert response.status_code == 200, (
                f"{path} {probe_name} Host {host_headers[0]!r} was rejected"
            )


def test_rendered_allowed_hosts_still_reject_a_dynamic_pod_ip(
    rendered_kustomization: tuple[Path, list[dict[str, Any]]],
) -> None:
    """Keep pod-IP discovery out of both local and production allow-lists."""
    path, resources = rendered_kustomization
    allowed_hosts = _allowed_hosts(resources)
    assert "*" not in allowed_hosts, f"{path} must keep an explicit host allow-list"

    with override_settings(ALLOWED_HOSTS=allowed_hosts):
        request = RequestFactory().get("/api/livez", HTTP_HOST="10.244.12.34:8000")
        with pytest.raises(DisallowedHost):
            request.get_host()
