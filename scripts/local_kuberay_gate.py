"""Run the guarded local Docker Desktop/Kind KubeRay final integration gate.

The gate intentionally owns only resources rendered from the checked-in
``k8s/overlays/kuberay-kind`` overlay in the ``django-ray`` namespace.  It
never deletes the namespace, PostgreSQL, PVCs, or Docker data.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID

import yaml

from scripts.check_prometheus_targets import (
    EXPECTED_JOBS,
    fetch_active_targets,
    wait_for_healthy_targets,
)

EXPECTED_NAMESPACE = "django-ray"
LOCAL_CONTEXT_PATTERN = re.compile(r"(?:docker-desktop|kind-[a-z0-9][a-z0-9._-]*)\Z")
LOCAL_API_HOSTS = frozenset(
    {
        "127.0.0.1",
        "::1",
        "localhost",
        "host.docker.internal",
        "kubernetes.docker.internal",
    }
)
LOCAL_HTTP_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
OVERLAY = Path("k8s/overlays/kuberay-kind")
APP_DEPLOYMENTS = (
    "django-web",
    "django-ray-worker",
    "django-ray-worker-sync",
    "django-ray-worker-ml",
)
TASK_MANAGER_DEPLOYMENTS = APP_DEPLOYMENTS[1:]
RAY_COMPONENTS = frozenset({"head", "worker"})
RAY_CLUSTER_NAME = "ray"
RAY_CLUSTER_LABEL = "ray.io/cluster"
RAY_GROUP_LABEL = "ray.io/group"
KUBERAY_WAIT_GCS_INIT = "wait-gcs-ready"
MAX_RAY_DISCOVERY_PODS = 128
MAX_RAY_POD_CONTAINERS = 16
MAX_RAY_POD_NAME_CHARACTERS = 253
MAX_RAY_POD_UID_CHARACTERS = 128
MAX_RAY_IMAGE_REFERENCE_CHARACTERS = 2_048
MAX_APPLICATION_REPLICASETS = 512
MAX_APPLICATION_PODS = 512
SETUP_JOB = "django-setup"
SETUP_CONTAINER = "django-setup"
APP_IMAGE_NAME = "django-ray"
LEGACY_WORKER_IMAGE_NAME = "django-ray-worker"
APP_IMAGE_REPOSITORY = "django-ray"
LEGACY_WORKER_IMAGE_REPOSITORY = "django-ray-worker"
EXPECTED_PROBE_PATH = "/api/health"
EXPECTED_PROBE_HOST = "django-ray.localhost"
RUNTIME_ENV_ARCHIVE = "/runtime-env/django-ray-source.zip"
RUNTIME_ENV_REQUIRED_MEMBER = "src/django_ray/runtime/remote.py"
COMPLEX_WORKFLOW_ENQUEUE_PATH = (
    "/api/cluster/complex-workflow?fast_items=2&slow_items=1&fast_seconds=0.01&slow_seconds=0.02"
)
COMPLEX_WORKFLOW_ENQUEUE_KWARGS = {
    "fast_items": 2,
    "slow_items": 1,
    "fast_seconds": 0.01,
    "slow_seconds": 0.02,
}
COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH = (
    "/api/cluster/complex-workflow?fast_items=2&slow_items=1"
    "&fast_seconds=0.01&slow_seconds=0.05"
    "&failure_branch=slow&failure_item=0"
)
COMPLEX_WORKFLOW_FAILURE_ENQUEUE_KWARGS = {
    "fast_items": 2,
    "slow_items": 1,
    "fast_seconds": 0.01,
    "slow_seconds": 0.05,
    "failure_branch": "slow",
    "failure_item": 0,
}
COMPLEX_WORKFLOW_FAILURE_MESSAGE = "Intentional complex workflow fixture failure"
WORKFLOW_ADMIN_LOOPBACK_URL = "http://127.0.0.1:8000"
WORKFLOW_PROGRESS_SCHEMA_VERSION = 3
WORKFLOW_PROGRESS_PAGE_LIMIT = 16
WORKFLOW_PROGRESS_COLLECTION_PATHS = {
    "topology_nodes": "topology/nodes",
    "topology_edges": "topology/edges",
    "node_details": "nodes",
}
WORKFLOW_PROGRESS_TASK_STATES = frozenset(
    {
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "CANCELLING",
        "LOST",
    }
)
WORKFLOW_PROGRESS_FAILURE_STATES = frozenset({"FAILED", "CANCELLED", "LOST"})
MAX_COMMAND_ERROR_LINES = 60
MAX_DIAGNOSTIC_LINES = 80
MAX_OUTPUT_CHARACTERS = 16_000
MAX_GATE_ERROR_CHARACTERS = MAX_OUTPUT_CHARACTERS - 256
MAX_FAILURE_CONTEXT_CHARACTERS = 4_000
EVIDENCE_LINE_LIMIT = 72
BEARER_TOKEN68_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+={0,2}\Z")
DOCKER_CONTEXT_ALLOWLISTS = {
    "Dockerfile.dockerignore": (
        "**",
        "!Dockerfile",
        "!pyproject.toml",
        "!uv.lock",
        "!README.md",
        "!docker-entrypoint.sh",
        "!src/",
        "!src/**",
        "!testproject/",
        "!testproject/**",
    ),
    "Dockerfile.ray.dockerignore": (
        "**",
        "!Dockerfile.ray",
        "!pyproject.toml",
        "!uv.lock",
        "!README.md",
        "!src/",
        "!src/**",
        "!testproject/",
        "!testproject/**",
    ),
}
PROXY_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)
KUBECTL_ENVIRONMENT_KEYS = frozenset(
    {
        *PROXY_ENVIRONMENT_KEYS,
        "KUBECONFIG",
        "KUBERNETES_MASTER",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
)
DOCKER_ENVIRONMENT_KEYS = frozenset(
    {
        *PROXY_ENVIRONMENT_KEYS,
        "BUILDKIT_HOST",
        "BUILDX_BUILDER",
        "BUILDX_CONFIG",
        "DOCKER_API_VERSION",
        "DOCKER_BUILDKIT",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_CUSTOM_HEADERS",
        "DOCKER_DEFAULT_PLATFORM",
        "DOCKER_HOST",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
)
KIND_ENVIRONMENT_KEYS = frozenset(
    {
        *DOCKER_ENVIRONMENT_KEYS,
        "KIND_EXPERIMENTAL_PROVIDER",
    }
)
ResourceIdentity = tuple[str, str, str]
RayPodContractPhase = Literal["restart-discovery", "converged"]
PREREQUISITE_RESOURCE_IDENTITIES = frozenset(
    {
        ("v1", "Namespace", EXPECTED_NAMESPACE),
        ("v1", "ServiceAccount", "prometheus"),
        ("rbac.authorization.k8s.io/v1", "Role", "prometheus-django-ray"),
        ("rbac.authorization.k8s.io/v1", "RoleBinding", "prometheus-django-ray"),
        ("v1", "ConfigMap", "django-ray-config"),
        ("v1", "ConfigMap", "grafana-dashboard-import-script"),
        ("v1", "ConfigMap", "grafana-dashboards"),
        ("v1", "ConfigMap", "grafana-dashboards-provider"),
        ("v1", "ConfigMap", "grafana-datasources"),
        ("v1", "ConfigMap", "prometheus-config"),
        ("v1", "Service", "django-web-svc"),
        ("v1", "Service", "grafana-svc"),
        ("v1", "Service", "postgres-svc"),
        ("v1", "Service", "prometheus-svc"),
        ("v1", "Service", "ray-dashboard-svc"),
        ("v1", "PersistentVolumeClaim", "postgres-pvc"),
        ("v1", "PersistentVolumeClaim", "runtime-env-pvc"),
        ("apps/v1", "Deployment", "grafana"),
        ("apps/v1", "Deployment", "postgres"),
        ("apps/v1", "Deployment", "prometheus"),
        ("networking.k8s.io/v1", "Ingress", "django-ray-ingress"),
    }
)
PRESERVED_SECRET_IDENTITY = ("v1", "Secret", "django-ray-secret")
SETUP_RESOURCE_IDENTITY = ("batch/v1", "Job", SETUP_JOB)
WORKLOAD_RESOURCE_IDENTITIES = frozenset(
    {
        *(("apps/v1", "Deployment", name) for name in APP_DEPLOYMENTS),
        ("ray.io/v1", "RayCluster", RAY_CLUSTER_NAME),
    }
)
EXPECTED_RESOURCE_IDENTITIES = frozenset(
    {
        *PREREQUISITE_RESOURCE_IDENTITIES,
        PRESERVED_SECRET_IDENTITY,
        SETUP_RESOURCE_IDENTITY,
        *WORKLOAD_RESOURCE_IDENTITIES,
    }
)
SOURCE_BOUND_RESOURCE_IDENTITIES = frozenset(
    {
        SETUP_RESOURCE_IDENTITY,
        *(("apps/v1", "Deployment", name) for name in APP_DEPLOYMENTS),
    }
)


class GateError(RuntimeError):
    """A failure attributed to one bounded gate layer."""

    def __init__(self, layer: str, message: str) -> None:
        super().__init__(message)
        self.layer = layer


class ApplicationTopologyPendingError(ValueError):
    """A safe application rollout observation that may converge before timeout."""


class CommandError(RuntimeError):
    """A subprocess failure with bounded, already-redacted output."""


@dataclass(frozen=True)
class CommandResult:
    """Captured subprocess result."""

    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class PodImageContract:
    """Exact init and regular container image references for one pod template."""

    init_containers: tuple[tuple[str, str], ...]
    containers: tuple[tuple[str, str], ...]

    @property
    def all(self) -> tuple[tuple[str, str], ...]:
        return self.init_containers + self.containers


@dataclass(frozen=True)
class DeploymentContract:
    """Rendered application Deployment topology that live state must retain."""

    replicas: int
    pod_images: PodImageContract
    selector: tuple[tuple[str, str], ...]


@dataclass(frozen=True, order=True)
class ContainerRuntimeIdentity:
    """One named container's declared image and immutable runtime image ID."""

    name: str
    image: str
    image_id: str


@dataclass(frozen=True, order=True)
class PodRuntimeIdentity:
    """Stable pod UID plus the complete init and regular container identity sets."""

    name: str
    uid: str
    init_containers: tuple[ContainerRuntimeIdentity, ...]
    containers: tuple[ContainerRuntimeIdentity, ...]


@dataclass(frozen=True)
class RayWorkerGroupTopology:
    """Stable, order-independent topology for one named Ray worker group."""

    name: str
    minimum: int
    replicas: int
    maximum: int
    ray_start_params: tuple[tuple[str, str], ...]
    pod_images: PodImageContract


@dataclass(frozen=True)
class RayTopology:
    """Critical rendered RayCluster fields compared with every live observation."""

    ray_version: str
    enable_in_tree_autoscaling: bool
    head_service_type: str
    head_ray_start_params: tuple[tuple[str, str], ...]
    head_pod_images: PodImageContract
    worker_groups: tuple[RayWorkerGroupTopology, ...]

    @property
    def counts(self) -> tuple[int, int]:
        return 1, sum(group.replicas for group in self.worker_groups)


class Redactor:
    """Remove in-memory credentials from every user-visible message."""

    def __init__(self) -> None:
        self._values: list[str] = []
        self._percent_patterns: dict[str, tuple[int, re.Pattern[str]]] = {}

    @staticmethod
    def _percent_escape_pattern(value: str) -> re.Pattern[str] | None:
        """Match one encoded value with case-insensitive percent-escape hex only."""

        parts: list[str] = []
        found_escape = False
        index = 0
        while index < len(value):
            if (
                value[index] == "%"
                and index + 2 < len(value)
                and all(
                    character in "0123456789abcdefABCDEF"
                    for character in value[index + 1 : index + 3]
                )
            ):
                found_escape = True
                parts.append("%")
                for character in value[index + 1 : index + 3]:
                    if character.isalpha():
                        parts.append(f"[{character.lower()}{character.upper()}]")
                    else:
                        parts.append(character)
                index += 3
                continue
            parts.append(re.escape(value[index]))
            index += 1
        if not found_escape:
            return None
        return re.compile("".join(parts))

    def register(self, value: str) -> None:
        if not value:
            return
        json_escaped = json.dumps(value, ensure_ascii=True)[1:-1]
        variants = {
            value,
            json_escaped,
            json_escaped.replace("/", r"\/"),
        }
        represented = repr(value)
        if (
            len(represented) >= 2
            and represented[0] == represented[-1]
            and represented[0] in {"'", '"'}
        ):
            variants.add(represented[1:-1])
        else:
            variants.add(represented)
        try:
            # URL encoders differ in which token68 punctuation they preserve.
            # Register every safe-set combination for the only three token68
            # characters that may otherwise be percent encoded.
            for safe in ("", "+", "/", "=", "+/", "+=", "/=", "+/="):
                variants.add(quote(value, safe=safe))
                variants.add(quote_plus(value, safe=safe))
        except UnicodeEncodeError:
            # The raw and escaped representations remain registered even when
            # a malformed non-UTF-8 surrogate cannot be URL encoded.
            pass
        for variant in variants:
            if variant and variant not in self._values:
                self._values.append(variant)
            pattern = self._percent_escape_pattern(variant)
            if pattern is not None:
                existing = self._percent_patterns.get(pattern.pattern)
                if existing is None or existing[0] < len(variant):
                    self._percent_patterns[pattern.pattern] = (len(variant), pattern)

    def clean(self, value: object) -> str:
        text = str(value)
        for _, pattern in sorted(
            self._percent_patterns.values(), key=lambda item: item[0], reverse=True
        ):
            text = pattern.sub("[REDACTED]", text)
        for secret_value in sorted(self._values, key=len, reverse=True):
            text = text.replace(secret_value, "[REDACTED]")
        return text


def sanitized_environment(
    removed_keys: frozenset[str], *, additions: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Copy the process environment without case-insensitive routing overrides."""

    environment = {
        key: value for key, value in os.environ.items() if key.upper() not in removed_keys
    }
    environment.update(additions or {})
    return environment


def _bounded_text(value: str, *, lines: int, characters: int = MAX_OUTPUT_CHARACTERS) -> str:
    selected = value.replace("\r\n", "\n").splitlines()[-lines:]
    return "\n".join(selected)[-characters:]


def _bounded_redacted_error(
    value: object,
    *,
    redactor: Redactor,
    characters: int = MAX_OUTPUT_CHARACTERS,
) -> str:
    """Redact and tail-bound one failure while retaining truncation metadata."""

    if characters <= 0:
        raise ValueError("bounded error character limit must be positive")
    cleaned = redactor.clean(value).replace("\r\n", "\n").replace("\r", "\n")
    if len(cleaned) <= characters:
        return cleaned
    marker = f"[truncated redacted error; original_characters={len(cleaned)}]\n"
    retained = characters - len(marker)
    if retained <= 0:
        return marker[:characters]
    return f"{marker}{cleaned[-retained:]}"


def _gate_error_detail(
    primary: object,
    *,
    redactor: Redactor,
    contexts: Sequence[tuple[str, object]] = (),
) -> str:
    """Bound one primary failure plus independently bounded secondary context."""

    rendered_contexts = tuple(
        f"{label}: "
        + _bounded_redacted_error(
            error,
            redactor=redactor,
            characters=MAX_FAILURE_CONTEXT_CHARACTERS - len(label) - 2,
        )
        for label, error in contexts
    )
    suffix = "\n".join(rendered_contexts)
    separator = "\n" if suffix else ""
    primary_budget = MAX_GATE_ERROR_CHARACTERS - len(separator) - len(suffix)
    if primary_budget <= 0:
        raise ValueError("secondary gate failure context exhausted the error budget")
    bounded_primary = _bounded_redacted_error(
        primary,
        redactor=redactor,
        characters=primary_budget,
    )
    return f"{bounded_primary}{separator}{suffix}"


class Runner:
    """Run argument-vector commands without a shell and capture bounded failures."""

    def __init__(self, *, redactor: Redactor | None = None, timeout_seconds: float = 120.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("runner timeout must be positive")
        self.redactor = redactor or Redactor()
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        check: bool = True,
        timeout: float | None = None,
        sensitive_output: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        effective_timeout = self.timeout_seconds if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("command timeout must be positive")
        try:
            result = subprocess.run(
                list(args),
                cwd=cwd,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=effective_timeout,
                env=None if env is None else dict(env),
            )
        except subprocess.TimeoutExpired as error:

            def decoded(value: str | bytes | None) -> str:
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return value or ""

            command = " ".join(args)
            output = ""
            if not sensitive_output:
                output = _bounded_text(
                    self.redactor.clean(
                        "\n".join(
                            part for part in (decoded(error.stdout), decoded(error.stderr)) if part
                        )
                    ),
                    lines=MAX_COMMAND_ERROR_LINES,
                )
            detail = f"command timed out after {effective_timeout:g}s: {command}"
            if sensitive_output:
                detail = f"{detail}\n[sensitive command output suppressed]"
            elif output:
                detail = f"{detail}\n{output}"
            raise CommandError(self.redactor.clean(detail)) from error
        command_result = CommandResult(result.stdout, result.stderr, result.returncode)
        if check and result.returncode != 0:
            command = " ".join(args)
            output = ""
            if not sensitive_output:
                output = _bounded_text(
                    self.redactor.clean(
                        "\n".join(part for part in (result.stdout, result.stderr) if part)
                    ),
                    lines=MAX_COMMAND_ERROR_LINES,
                )
            detail = f"command failed ({result.returncode}): {command}"
            if sensitive_output:
                detail = f"{detail}\n[sensitive command output suppressed]"
            elif output:
                detail = f"{detail}\n{output}"
            raise CommandError(self.redactor.clean(detail))
        return command_result


@dataclass(frozen=True)
class GateConfig:
    """Explicit local-cluster gate inputs."""

    root: Path
    context: str
    namespace: str
    ray_restart: str
    web_url: str
    prometheus_url: str
    kind_cluster_name: str | None
    rollout_timeout: int
    task_timeout: int
    prometheus_timeout: int
    command_timeout: int
    build_timeout: int
    kubectl_request_timeout: int
    preflight_only: bool


@dataclass
class GateEvidence:
    """Concise secret-free evidence emitted after a successful run."""

    commit: str = ""
    source_tree: str = ""
    kubeconfig_sha256: str = ""
    kubernetes_server: str = ""
    docker_host: str = ""
    app_tag: str = ""
    worker_tag: str = ""
    app_image_id: str = ""
    worker_image_id: str = ""
    setup_bundle_bytes: int = 0
    setup_bundle_sha256: str = ""
    ray_restart: str = ""
    ray_cluster_uid: str = ""
    ray_pod_identity_sha256: str = ""
    ray_head_count: int = 0
    ray_worker_count: int = 0
    deployments: dict[str, int] = field(default_factory=dict)
    web_restart_count: int = 0
    task_id: str = ""
    task_state: str = ""
    task_result: object = None
    workflow_task_id: str = ""
    workflow_task_state: str = ""
    workflow_attempt_number: int = 0
    workflow_schema_version: int = 0
    workflow_availability: str = ""
    workflow_topology_nodes: int = 0
    workflow_topology_edges: int = 0
    workflow_node_details: int = 0
    workflow_leaf_tasks: int = 0
    workflow_admin_routes: int = 0
    workflow_admin_actions: int = 0
    workflow_current_manifests: int = 0
    workflow_pending_manifests: int = 0
    workflow_unlinked_pages: int = 0
    workflow_failure_task_id: str = ""
    workflow_failure_task_state: str = ""
    workflow_failure_attempt_number: int = 0
    workflow_failure_schema_version: int = 0
    workflow_failure_availability: str = ""
    workflow_failure_topology_nodes: int = 0
    workflow_failure_topology_edges: int = 0
    workflow_failure_node_details: int = 0
    workflow_failure_leaf_tasks: int = 0
    workflow_failure_pending_nodes: int = 0
    workflow_failure_running_nodes: int = 0
    workflow_failure_succeeded_nodes: int = 0
    workflow_failure_failed_nodes: int = 0
    workflow_failure_path_nodes: int = 0
    workflow_failure_origins: int = 0
    workflow_failure_incoming_edges: int = 0
    workflow_failure_admin_routes: int = 0
    workflow_failure_admin_actions: int = 0
    workflow_failure_current_manifests: int = 0
    workflow_failure_pending_manifests: int = 0
    workflow_failure_unlinked_pages: int = 0
    prometheus_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowGateObservation:
    """One terminal schema-v3 workflow verified through every bounded API reader."""

    task_id: str
    state: str
    attempt_number: int
    schema_version: int
    availability: str
    topology_nodes: int
    topology_edges: int
    node_details: int
    leaf_tasks: int
    pending_nodes: int
    running_nodes: int
    succeeded_nodes: int
    failed_nodes: int


def validate_namespace(namespace: str) -> None:
    """Accept only the dedicated local-demo namespace."""
    if namespace != EXPECTED_NAMESPACE:
        raise ValueError(
            f"namespace must be exactly {EXPECTED_NAMESPACE!r}; received {namespace!r}"
        )


def validate_local_context(*, current: str, expected: str, server_url: str) -> None:
    """Fail closed unless both context name and API endpoint identify a local cluster."""
    if current != expected:
        raise ValueError(f"active Kubernetes context does not match expected {expected!r}")
    if LOCAL_CONTEXT_PATTERN.fullmatch(expected) is None:
        raise ValueError(
            "context must be 'docker-desktop' or a named Kind context beginning with 'kind-'"
        )
    if not 1 <= len(server_url) <= 2048 or any(
        not 0x21 <= ord(character) <= 0x7E for character in server_url
    ):
        raise ValueError("Kubernetes API server URL must be bounded printable ASCII")
    parsed = urlparse(server_url)
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname not in LOCAL_API_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ValueError(
            f"context {expected!r} resolves to a non-local Kubernetes API or contains credentials"
        )


def inspect_kubeconfig_snapshot(payload: object, *, expected_context: str) -> str:
    """Validate one flattened, minified kubeconfig and return its local API server."""

    document = _mapping(payload, field_name="kubeconfig snapshot")
    current = document.get("current-context")
    if current != expected_context:
        raise ValueError("kubeconfig snapshot current context does not match the requested context")
    clusters = _sequence(document.get("clusters"), field_name="kubeconfig snapshot clusters")
    contexts = _sequence(document.get("contexts"), field_name="kubeconfig snapshot contexts")
    users = _sequence(document.get("users"), field_name="kubeconfig snapshot users")
    if len(clusters) != 1 or len(contexts) != 1 or len(users) != 1:
        raise ValueError("minified kubeconfig snapshot must contain one cluster, context, and user")
    cluster_entry = _mapping(clusters[0], field_name="kubeconfig snapshot cluster entry")
    context_entry = _mapping(contexts[0], field_name="kubeconfig snapshot context entry")
    user_entry = _mapping(users[0], field_name="kubeconfig snapshot user entry")
    if context_entry.get("name") != expected_context:
        raise ValueError("kubeconfig snapshot context entry does not match the requested context")
    context = _mapping(context_entry.get("context"), field_name="kubeconfig snapshot context")
    cluster_name = cluster_entry.get("name")
    user_name = user_entry.get("name")
    if context.get("cluster") != cluster_name or context.get("user") != user_name:
        raise ValueError("kubeconfig snapshot context does not reference its sole cluster and user")
    cluster = _mapping(cluster_entry.get("cluster"), field_name="kubeconfig snapshot cluster")
    if cluster.get("proxy-url") is not None:
        raise ValueError("kubeconfig snapshot must not route through a proxy URL")
    server = cluster.get("server")
    if not isinstance(server, str):
        raise ValueError("kubeconfig snapshot has no API server URL")
    validate_local_context(current=expected_context, expected=expected_context, server_url=server)
    return server


def _local_http_origin(
    value: str,
    *,
    option: str,
    allow_query: bool,
) -> tuple[str, str, int]:
    """Validate one local URL and return its effective credential boundary."""

    if not 1 <= len(value) <= 2048 or any(
        not 0x21 <= ord(character) <= 0x7E for character in value
    ):
        raise ValueError(f"{option} must be bounded printable ASCII without whitespace")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname or ""
        parsed_port = parsed.port
    except ValueError as error:
        raise ValueError(f"{option} must be a valid local HTTP URL") from error
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{option} must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{option} must not contain URL credentials")
    if parsed.fragment:
        raise ValueError(f"{option} must not contain a fragment")
    if parsed.query and not allow_query:
        raise ValueError(f"{option} must not contain a query string")
    if hostname not in LOCAL_HTTP_HOSTS and not hostname.endswith(".localhost"):
        raise ValueError(f"{option} must resolve through localhost")
    if parsed.netloc.endswith(":"):
        raise ValueError(f"{option} must contain a valid port when a port is declared")
    effective_port = parsed_port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, hostname, effective_port


def validate_local_http_url(value: str, *, option: str) -> None:
    """Prevent an operator token from ever being sent to a non-local HTTP endpoint."""

    _local_http_origin(value, option=option, allow_query=False)


def build_local_http_request_url(*, base_url: str, path: str) -> str:
    """Compose one internal request without crossing the configured local origin."""

    base_origin = _local_http_origin(
        base_url, option="configured local HTTP base", allow_query=False
    )
    if not 1 <= len(path) <= 4096 or any(not 0x21 <= ord(character) <= 0x7E for character in path):
        raise ValueError("local HTTP request path must be bounded printable ASCII")
    url = urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/"))
    request_origin = _local_http_origin(url, option="local HTTP request", allow_query=True)
    if request_origin != base_origin:
        raise ValueError("local HTTP request must remain on the exact configured origin")
    return url


def validate_local_docker_endpoint(value: str) -> None:
    """Reject remote Docker daemons before building source or creating tags."""
    if not 1 <= len(value) <= 2048 or any(
        not 0x21 <= ord(character) <= 0x7E for character in value
    ):
        raise ValueError("Docker endpoint must be bounded printable ASCII without whitespace")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("Docker endpoint must be a valid local endpoint") from error
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Docker endpoint must be local and contain no credentials, query, or fragment"
        )
    if parsed.scheme == "npipe":
        if (
            re.fullmatch(
                r"npipe:////\./pipe/[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                value,
                flags=re.IGNORECASE,
            )
            is None
        ):
            raise ValueError("Docker named-pipe endpoint must be local to this machine")
        return
    if parsed.scheme == "unix":
        if parsed.netloc or not parsed.path.startswith("/") or not parsed.path.strip("/"):
            raise ValueError("Docker Unix-socket endpoint must be local and use an absolute path")
        return
    if (
        parsed.scheme in {"tcp", "http", "https"}
        and hostname in LOCAL_API_HOSTS
        and port is not None
        and parsed.path in {"", "/"}
    ):
        return
    raise ValueError("Docker endpoint must be local: use a pinned socket or loopback TCP endpoint")


def source_bound_tag(source_tree: str, *, now: datetime, nonce: str) -> str:
    """Return a unique tag that visibly identifies the immutable source tree."""
    if re.fullmatch(r"[0-9a-f]{40}", source_tree) is None:
        raise ValueError("source tree must be a full lowercase Git SHA-1")
    if re.fullmatch(r"[0-9a-f]{8}", nonce) is None:
        raise ValueError("tag nonce must contain eight lowercase hexadecimal characters")
    timestamp = now.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    return f"local-gate-tree-{source_tree[:12]}-{timestamp}-{nonce}"


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return cast("Mapping[str, Any]", value)


def _sequence(value: object, *, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value


def _register_secret_value(redactor: Redactor, value: object) -> None:
    """Register one textual credential and a printable decoded base64 form."""

    if not isinstance(value, str) or not value:
        return
    redactor.register(value)
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return
    if decoded and all(0x20 <= ord(character) <= 0x7E for character in decoded):
        redactor.register(decoded)


def _sensitive_kubeconfig_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.lower().replace("_", "-")
    return any(
        marker in normalized
        for marker in ("credential", "key", "password", "secret", "token", "username")
    )


def register_kubeconfig_secrets(payload: object, *, redactor: Redactor) -> None:
    """Register static user, auth-provider, and exec credentials before kubectl runs."""

    document = _mapping(payload, field_name="kubeconfig snapshot")
    users = _sequence(document.get("users"), field_name="kubeconfig snapshot users")
    for index, value in enumerate(users):
        entry = _mapping(value, field_name=f"kubeconfig snapshot users[{index}]")
        user = _mapping(entry.get("user"), field_name=f"kubeconfig snapshot users[{index}].user")
        for key, candidate in user.items():
            if _sensitive_kubeconfig_key(key) and not isinstance(candidate, Mapping):
                _register_secret_value(redactor, candidate)

        auth_provider_value = user.get("auth-provider")
        if auth_provider_value is not None:
            auth_provider = _mapping(
                auth_provider_value,
                field_name=f"kubeconfig snapshot users[{index}].user.auth-provider",
            )
            config = _mapping(
                auth_provider.get("config", {}),
                field_name=f"kubeconfig snapshot users[{index}].user.auth-provider.config",
            )
            for key, candidate in config.items():
                if _sensitive_kubeconfig_key(key):
                    _register_secret_value(redactor, candidate)

        exec_value = user.get("exec")
        if exec_value is None:
            continue
        exec_config = _mapping(
            exec_value, field_name=f"kubeconfig snapshot users[{index}].user.exec"
        )
        arguments = _sequence(
            exec_config.get("args", []),
            field_name=f"kubeconfig snapshot users[{index}].user.exec.args",
        )
        previous_sensitive = False
        for argument in arguments:
            if not isinstance(argument, str):
                raise ValueError("kubeconfig exec arguments must be strings")
            if previous_sensitive:
                _register_secret_value(redactor, argument)
                previous_sensitive = False
                continue
            option, separator, candidate = argument.partition("=")
            if _sensitive_kubeconfig_key(option):
                if separator:
                    _register_secret_value(redactor, candidate)
                else:
                    previous_sensitive = True
            elif not argument.startswith("-") and len(argument) >= 20:
                _register_secret_value(redactor, argument)
        environment = _sequence(
            exec_config.get("env", []),
            field_name=f"kubeconfig snapshot users[{index}].user.exec.env",
        )
        for env_index, env_value in enumerate(environment):
            env_entry = _mapping(
                env_value,
                field_name=f"kubeconfig snapshot users[{index}].user.exec.env[{env_index}]",
            )
            if _sensitive_kubeconfig_key(env_entry.get("name")):
                _register_secret_value(redactor, env_entry.get("value"))


def normalize_label_selector(value: object, *, field_name: str) -> tuple[tuple[str, str], ...]:
    """Require one non-empty equality-only selector suitable for exact inventory checks."""

    selector = _mapping(value, field_name=field_name)
    expressions = selector.get("matchExpressions", [])
    if expressions not in (None, []):
        raise ValueError(f"{field_name} must not use matchExpressions")
    unexpected = set(selector) - {"matchLabels", "matchExpressions"}
    if unexpected:
        raise ValueError(f"{field_name} has unsupported fields: {sorted(unexpected)}")
    labels = _string_pairs(selector.get("matchLabels"), field_name=f"{field_name}.matchLabels")
    if not labels:
        raise ValueError(f"{field_name}.matchLabels must not be empty")
    return labels


def labels_match_selector(labels: object, selector: tuple[tuple[str, str], ...]) -> bool:
    """Return whether one label object matches every exact selector pair."""

    mapping = _mapping(labels, field_name="pod labels")
    return all(mapping.get(key) == value for key, value in selector)


def normalize_image_reference(value: str) -> str:
    """Normalize Docker Hub's optional canonical prefix for status/spec comparison."""

    if not value or any(character.isspace() for character in value):
        raise ValueError("container image reference must be a non-empty value without whitespace")
    if "://" in value:
        raise ValueError("container image reference must not contain a URL scheme")
    first, separator, remainder = value.partition("/")
    if not separator:
        return f"docker.io/library/{value}"
    if "." not in first and ":" not in first and first != "localhost":
        return f"docker.io/{value}"
    return value


def _metadata(resource: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(resource.get("metadata"), field_name="resource metadata")


def _resource_identity(resource: Mapping[str, Any]) -> ResourceIdentity:
    api_version = resource.get("apiVersion")
    kind = resource.get("kind")
    name = _metadata(resource).get("name")
    if not all(isinstance(value, str) and value for value in (api_version, kind, name)):
        raise ValueError("every rendered resource needs apiVersion, kind, and metadata.name")
    return cast("ResourceIdentity", (api_version, kind, name))


def _string_pairs(value: object, *, field_name: str) -> tuple[tuple[str, str], ...]:
    mapping = _mapping(value, field_name=field_name)
    pairs: list[tuple[str, str]] = []
    for key, item in mapping.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{field_name} keys and values must be strings")
        pairs.append((key, item))
    return tuple(sorted(pairs))


def pod_image_contract(pod_spec: Mapping[str, Any]) -> PodImageContract:
    """Return the exact named image references in one rendered or live pod spec."""

    if pod_spec.get("ephemeralContainers") not in (None, []):
        raise ValueError("guarded pods must not declare ephemeral containers")
    names: set[str] = set()

    def entries(key: str) -> tuple[tuple[str, str], ...]:
        result: list[tuple[str, str]] = []
        for index, value in enumerate(_sequence(pod_spec.get(key, []), field_name=key)):
            container = _mapping(value, field_name=f"{key}[{index}]")
            name = container.get("name")
            image = container.get("image")
            if not isinstance(name, str) or not name:
                raise ValueError(f"{key}[{index}].name must be a non-empty string")
            if name in names:
                raise ValueError(f"pod container name {name!r} is duplicated")
            if not isinstance(image, str) or not image:
                raise ValueError(f"{key}[{index}].image must be a non-empty string")
            names.add(name)
            result.append((name, image))
        return tuple(result)

    return PodImageContract(
        init_containers=entries("initContainers"),
        containers=entries("containers"),
    )


def normalize_ray_topology(ray_cluster: Mapping[str, Any]) -> RayTopology:
    """Normalize the critical rendered/live RayCluster topology for exact comparison."""

    if _resource_identity(ray_cluster) != ("ray.io/v1", "RayCluster", RAY_CLUSTER_NAME):
        raise ValueError(f"expected RayCluster/{RAY_CLUSTER_NAME}")
    spec = _mapping(ray_cluster.get("spec"), field_name="RayCluster spec")
    if spec.get("enableInTreeAutoscaling") is not False:
        raise ValueError("the guarded local RayCluster must disable in-tree autoscaling")
    ray_version = spec.get("rayVersion")
    if not isinstance(ray_version, str) or not ray_version:
        raise ValueError("the guarded local RayCluster must declare rayVersion")
    head = _mapping(spec.get("headGroupSpec"), field_name="RayCluster headGroupSpec")
    head_service_type = head.get("serviceType")
    if not isinstance(head_service_type, str) or not head_service_type:
        raise ValueError("RayCluster headGroupSpec.serviceType must be a non-empty string")
    head_template = _mapping(head.get("template"), field_name="RayCluster head template")
    head_pod_spec = _mapping(head_template.get("spec"), field_name="RayCluster head template spec")
    head_images = pod_image_contract(head_pod_spec)
    if not head_images.containers:
        raise ValueError("RayCluster headGroupSpec must declare containers")
    worker_groups = _sequence(
        spec.get("workerGroupSpecs"), field_name="RayCluster workerGroupSpecs"
    )
    if not worker_groups:
        raise ValueError("the guarded local RayCluster must declare worker groups")
    normalized_groups: list[RayWorkerGroupTopology] = []
    names: set[str] = set()
    for index, value in enumerate(worker_groups):
        group = _mapping(value, field_name=f"RayCluster workerGroupSpecs[{index}]")
        name = group.get("groupName")
        if not isinstance(name, str) or not name:
            raise ValueError(f"RayCluster worker group {index} needs a non-empty groupName")
        if name in names:
            raise ValueError(f"RayCluster worker group {name!r} is duplicated")
        names.add(name)
        replicas = group.get("replicas")
        minimum = group.get("minReplicas")
        maximum = group.get("maxReplicas")
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
            raise ValueError(f"RayCluster worker group {index} replicas must be positive")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (minimum, maximum)
        ):
            raise ValueError(
                f"RayCluster worker group {index} minReplicas and maxReplicas must be positive"
            )
        if minimum != replicas or maximum != replicas:
            raise ValueError(
                f"RayCluster worker group {index} must pin minReplicas, replicas, "
                "and maxReplicas to the same value"
            )
        template = _mapping(group.get("template"), field_name=f"RayCluster worker {name} template")
        pod_spec = _mapping(
            template.get("spec"), field_name=f"RayCluster worker {name} template spec"
        )
        images = pod_image_contract(pod_spec)
        if not images.containers:
            raise ValueError(f"RayCluster worker group {name!r} must declare containers")
        normalized_groups.append(
            RayWorkerGroupTopology(
                name=name,
                minimum=minimum,
                replicas=replicas,
                maximum=maximum,
                ray_start_params=_string_pairs(
                    group.get("rayStartParams", {}),
                    field_name=f"RayCluster worker {name} rayStartParams",
                ),
                pod_images=images,
            )
        )
    return RayTopology(
        ray_version=ray_version,
        enable_in_tree_autoscaling=False,
        head_service_type=head_service_type,
        head_ray_start_params=_string_pairs(
            head.get("rayStartParams", {}), field_name="RayCluster head rayStartParams"
        ),
        head_pod_images=head_images,
        worker_groups=tuple(sorted(normalized_groups, key=lambda group: group.name)),
    )


def expected_ray_topology(ray_cluster: Mapping[str, Any]) -> tuple[int, int]:
    """Return the exact static head and worker counts declared by the local overlay."""

    return normalize_ray_topology(ray_cluster).counts


def configure_overlay_copy(*, source_k8s: Path, destination_k8s: Path, tag: str) -> Path:
    """Copy manifests to a temporary tree and replace only local image tags."""
    shutil.copytree(source_k8s, destination_k8s)
    kustomization = destination_k8s / OVERLAY.relative_to("k8s") / "kustomization.yaml"
    payload = _mapping(
        yaml.safe_load(kustomization.read_text(encoding="utf-8")),
        field_name="Kustomization",
    )
    mutable = dict(payload)
    mutable["images"] = [
        {"name": APP_IMAGE_NAME, "newName": APP_IMAGE_REPOSITORY, "newTag": tag},
        {
            "name": LEGACY_WORKER_IMAGE_NAME,
            "newName": LEGACY_WORKER_IMAGE_REPOSITORY,
            "newTag": tag,
        },
    ]
    kustomization.write_text(
        yaml.safe_dump(mutable, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    return kustomization.parent


def load_rendered_resources(rendered: str) -> list[dict[str, Any]]:
    """Parse a Kustomize stream into concrete resources."""
    resources: list[dict[str, Any]] = []
    for index, value in enumerate(yaml.safe_load_all(rendered)):
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"rendered document {index} must be a Kubernetes object")
        resources.append(value)
    if not resources:
        raise ValueError("Kustomize rendered no resources")
    return resources


def _pod_spec(resource: Mapping[str, Any]) -> Mapping[str, Any] | None:
    kind = resource.get("kind")
    if kind in {"Deployment", "Job"}:
        spec = _mapping(resource.get("spec"), field_name=f"{kind} spec")
        template = _mapping(spec.get("template"), field_name=f"{kind} pod template")
        return _mapping(template.get("spec"), field_name=f"{kind} pod spec")
    if kind == "RayCluster":
        return None
    return None


def _container_images(pod_spec: Mapping[str, Any]) -> list[str]:
    images: list[str] = []
    for key in ("initContainers", "containers"):
        values = pod_spec.get(key, [])
        for index, value in enumerate(_sequence(values, field_name=key)):
            container = _mapping(value, field_name=f"{key}[{index}]")
            image = container.get("image")
            if not isinstance(image, str) or not image:
                raise ValueError(f"{key}[{index}].image must be a non-empty string")
            images.append(image)
    return images


def _all_declared_images(value: object) -> list[str]:
    images: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                images.append(child)
            else:
                images.extend(_all_declared_images(child))
    elif isinstance(value, list):
        for child in value:
            images.extend(_all_declared_images(child))
    return images


def inspect_rendered_resources(
    resources: Sequence[Mapping[str, Any]], *, namespace: str, tag: str
) -> None:
    """Prove the apply stream cannot escape the namespace or use floating app tags."""
    validate_namespace(namespace)
    identities: set[ResourceIdentity] = set()
    for resource in resources:
        identity = _resource_identity(resource)
        api_version, kind, name = identity
        metadata = _metadata(resource)
        if identity not in EXPECTED_RESOURCE_IDENTITIES:
            raise ValueError(
                f"rendered resource {api_version} {kind}/{name} is not in the guarded inventory"
            )
        if identity in identities:
            raise ValueError(f"rendered resource {api_version} {kind}/{name} is duplicated")
        identities.add(identity)

        resource_namespace = metadata.get("namespace")
        if kind == "Namespace":
            if name != namespace or resource_namespace is not None:
                raise ValueError("the only cluster-scoped object must be Namespace/django-ray")
        elif resource_namespace != namespace:
            raise ValueError(
                f"rendered {kind}/{name} targets namespace {resource_namespace!r}, "
                f"expected {namespace!r}"
            )

        for image in _all_declared_images(resource):
            if image in {
                f"{APP_IMAGE_REPOSITORY}:latest",
                f"{LEGACY_WORKER_IMAGE_REPOSITORY}:latest",
            }:
                raise ValueError(f"rendered application image uses floating tag: {image}")
        pod_spec = _pod_spec(resource)
        if identity in SOURCE_BOUND_RESOURCE_IDENTITIES:
            if pod_spec is None:
                raise ValueError(f"rendered {kind}/{name} has no pod spec")
            app_images = [
                image
                for image in _container_images(pod_spec)
                if image.startswith(f"{APP_IMAGE_REPOSITORY}:")
            ]
            expected_image = f"{APP_IMAGE_REPOSITORY}:{tag}"
            if not app_images or set(app_images) != {expected_image}:
                raise ValueError(
                    f"rendered {kind}/{name} must declare only source-bound "
                    f"{expected_image} application containers; found {app_images}"
                )

    missing = EXPECTED_RESOURCE_IDENTITIES - identities
    if missing:
        raise ValueError(f"rendered overlay is missing guarded resources: {sorted(missing)}")
    ray_cluster = next(
        resource
        for resource in resources
        if _resource_identity(resource) == ("ray.io/v1", "RayCluster", RAY_CLUSTER_NAME)
    )
    expected_ray_topology(ray_cluster)


def split_apply_resources(
    resources: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Stage prerequisites, setup, then application and Ray workloads."""
    setup: dict[str, Any] | None = None
    secret_found = False
    prerequisites: list[dict[str, Any]] = []
    workloads: list[dict[str, Any]] = []
    for resource in resources:
        identity = _resource_identity(resource)
        if identity == SETUP_RESOURCE_IDENTITY:
            if setup is not None:
                raise ValueError(f"expected exactly one rendered Job/{SETUP_JOB}")
            setup = resource
        elif identity == PRESERVED_SECRET_IDENTITY:
            if secret_found:
                raise ValueError("expected exactly one rendered Secret/django-ray-secret")
            secret_found = True
        elif identity in WORKLOAD_RESOURCE_IDENTITIES:
            workloads.append(resource)
        elif identity in PREREQUISITE_RESOURCE_IDENTITIES:
            prerequisites.append(resource)
        else:
            api_version, kind, name = identity
            raise ValueError(
                f"rendered resource {api_version} {kind}/{name} has no guarded apply phase"
            )
    if setup is None:
        raise ValueError(f"expected exactly one rendered Job/{SETUP_JOB}")
    if not secret_found:
        raise ValueError("expected exactly one rendered Secret/django-ray-secret")
    observed = {
        *(_resource_identity(resource) for resource in prerequisites),
        PRESERVED_SECRET_IDENTITY,
        SETUP_RESOURCE_IDENTITY,
        *(_resource_identity(resource) for resource in workloads),
    }
    missing = EXPECTED_RESOURCE_IDENTITIES - observed
    if missing:
        raise ValueError(f"staged resources are missing guarded identities: {sorted(missing)}")
    return prerequisites, setup, workloads


def inspect_setup_log(value: str) -> None:
    """Require evidence for every setup responsibility."""
    markers = (
        "Running migrations...",
        "Collecting static files...",
        "Building shared RuntimeEnv source bundle...",
        "RuntimeEnv bundle ready:",
        "Django setup complete!",
    )
    missing = [marker for marker in markers if marker not in value]
    if missing:
        raise ValueError(f"setup Job log is missing markers: {missing}")


def parse_docker_image_inspect(
    value: str, *, expected_tag: str, commit: str, source_tree: str
) -> str:
    """Verify image identity plus its commit-at-run and stable source-tree labels."""
    payload = json.loads(value)
    images = _sequence(payload, field_name="docker image inspect response")
    if len(images) != 1:
        raise ValueError(f"docker image inspect returned {len(images)} images")
    image = _mapping(images[0], field_name="docker image")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError("Docker image ID is not a sha256 digest")
    repo_tags = _sequence(image.get("RepoTags"), field_name="Docker RepoTags")
    if expected_tag not in repo_tags:
        raise ValueError(f"Docker image is missing expected tag {expected_tag!r}")
    config = _mapping(image.get("Config"), field_name="Docker image Config")
    labels = _mapping(config.get("Labels"), field_name="Docker image labels")
    if labels.get("org.opencontainers.image.revision") != commit:
        raise ValueError("Docker image revision label does not match the tested Git commit")
    if labels.get("org.opencontainers.image.source-tree") != source_tree:
        raise ValueError("Docker image source-tree label does not match the tested Git tree")
    return image_id


def inspect_controlling_owner(
    resource: Mapping[str, Any],
    *,
    namespace: str,
    api_version: str,
    kind: str,
    name: str,
    uid: str,
) -> None:
    """Require one exact controlling owner for a namespaced live resource."""
    metadata = _metadata(resource)
    if metadata.get("namespace") != namespace:
        raise ValueError(f"owned resource escaped namespace {namespace!r}")
    resource_uid = metadata.get("uid")
    if not isinstance(resource_uid, str) or not resource_uid:
        raise ValueError("owned resource has no stable UID")
    try:
        controller = controlling_owner(resource)
    except ValueError as error:
        raise ValueError(f"resource is not controlled by {kind}/{name} with UID {uid}") from error
    if not (
        controller.get("apiVersion") == api_version
        and controller.get("kind") == kind
        and controller.get("name") == name
        and controller.get("uid") == uid
    ):
        raise ValueError(f"resource is not controlled by {kind}/{name} with UID {uid}")


def owner_references(resource: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Return every structurally valid owner reference for one live resource."""

    metadata = _metadata(resource)
    owners = _sequence(metadata.get("ownerReferences", []), field_name="ownerReferences")
    return tuple(
        _mapping(owner, field_name=f"ownerReferences[{index}]")
        for index, owner in enumerate(owners)
    )


def controlling_owner(resource: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the sole complete controlling owner reference for a live resource."""

    controllers = [owner for owner in owner_references(resource) if owner.get("controller") is True]
    if len(controllers) != 1:
        raise ValueError("resource must have exactly one controlling owner")
    controller = controllers[0]
    for field_name in ("apiVersion", "kind", "name", "uid"):
        value = controller.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"controlling owner has no valid {field_name}")
    return controller


class RejectRedirects(HTTPRedirectHandler):
    """Turn every redirect into an HTTPError instead of forwarding credentials."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def build_local_http_opener() -> OpenerDirector:
    """Build an opener that ignores ambient proxies and never follows redirects."""
    return build_opener(ProxyHandler({}), RejectRedirects())


def inspect_docker_context_allowlists(context: Path) -> None:
    """Require exact Dockerfile-specific deny-by-default context policies."""
    for name, expected in DOCKER_CONTEXT_ALLOWLISTS.items():
        path = context / name
        if not path.is_file():
            raise ValueError(f"source archive is missing required Docker context policy {name}")
        effective = tuple(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if effective != expected:
            raise ValueError(
                f"{name} must be the reviewed deny-by-default allowlist; found {effective}"
            )


def create_source_build_context(
    *, runner: Runner, root: Path, temporary_root: Path, commit: str, source_tree: str
) -> Path:
    """Export only the tested Git commit into the Docker build context."""
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("captured Git commit must be a full lowercase SHA-1")
    if re.fullmatch(r"[0-9a-f]{40}", source_tree) is None:
        raise ValueError("captured Git source tree must be a full lowercase SHA-1")
    archive = temporary_root / "source.zip"
    context = temporary_root / "source"
    runner.run(
        [
            "git",
            "archive",
            "--format=zip",
            f"--output={archive}",
            commit,
        ],
        cwd=root,
    )
    context.mkdir()
    shutil.unpack_archive(archive, context, format="zip")
    archive.unlink()
    required = (
        "Dockerfile",
        "Dockerfile.ray",
        "Dockerfile.dockerignore",
        "Dockerfile.ray.dockerignore",
        "pyproject.toml",
        "uv.lock",
    )
    missing = [name for name in required if not (context / name).is_file()]
    if missing:
        raise ValueError(f"source archive is missing Docker build inputs: {missing}")
    if (context / ".git").exists():
        raise ValueError("source archive unexpectedly contains Git metadata")
    inspect_docker_context_allowlists(context)
    return context


def normalize_runtime_image_id(value: str) -> str:
    """Normalize Docker/containerd/Kubernetes image-ID spellings to sha256."""
    match = re.search(r"sha256:[0-9a-f]{64}\Z", value)
    if match is None:
        raise ValueError(f"runtime image ID is not a sha256 digest: {value!r}")
    return match.group(0)


def inspect_pod_runtime_identity(
    pod: Mapping[str, Any],
    *,
    namespace: str,
    expected_contract: PodImageContract,
    expected_source_tag: str | None = None,
    expected_source_id: str | None = None,
    require_ready: bool,
) -> PodRuntimeIdentity:
    """Verify a pod's exact container contract and return its immutable runtime identity."""

    metadata = _metadata(pod)
    if metadata.get("namespace") != namespace:
        raise ValueError("pod escaped the guarded namespace")
    name = metadata.get("name")
    uid = metadata.get("uid")
    if not isinstance(name, str) or not name:
        raise ValueError("pod has no stable name")
    if not isinstance(uid, str) or not uid:
        raise ValueError("pod has no stable UID")
    spec = _mapping(pod.get("spec"), field_name=f"Pod/{name} spec")
    actual_contract = pod_image_contract(spec)
    if actual_contract != expected_contract:
        raise ValueError(f"Pod/{name} image contract does not match its rendered source topology")
    status = _mapping(pod.get("status"), field_name=f"Pod/{name} status")
    if status.get("ephemeralContainerStatuses") not in (None, []):
        raise ValueError(f"Pod/{name} reported unexpected ephemeral container statuses")

    def identities(
        expected: tuple[tuple[str, str], ...], *, status_key: str, regular: bool
    ) -> tuple[ContainerRuntimeIdentity, ...]:
        raw_statuses = _sequence(status.get(status_key, []), field_name=f"Pod/{name} {status_key}")
        by_name: dict[str, Mapping[str, Any]] = {}
        for index, value in enumerate(raw_statuses):
            entry = _mapping(value, field_name=f"Pod/{name} {status_key}[{index}]")
            container_name = entry.get("name")
            if not isinstance(container_name, str) or not container_name:
                raise ValueError(f"Pod/{name} {status_key}[{index}] has no valid name")
            if container_name in by_name:
                raise ValueError(
                    f"Pod/{name} has duplicate status for container {container_name!r}"
                )
            by_name[container_name] = entry
        expected_names = {container_name for container_name, _ in expected}
        if set(by_name) != expected_names:
            raise ValueError(
                f"Pod/{name} {status_key} names {sorted(by_name)} do not match "
                f"the rendered names {sorted(expected_names)}"
            )
        result: list[ContainerRuntimeIdentity] = []
        for container_name, declared_image in expected:
            entry = by_name[container_name]
            status_image = entry.get("image")
            image_id = entry.get("imageID")
            if not isinstance(status_image, str) or (
                normalize_image_reference(status_image) != normalize_image_reference(declared_image)
            ):
                raise ValueError(
                    f"Pod/{name} status image for {container_name!r} does not match "
                    "its rendered image"
                )
            if not isinstance(image_id, str):
                raise ValueError(f"Pod/{name} container {container_name!r} has no runtime image ID")
            normalized_id = normalize_runtime_image_id(image_id)
            if declared_image == expected_source_tag and normalized_id != expected_source_id:
                raise ValueError(
                    f"Pod/{name} source container {container_name!r} does not run "
                    f"the locally built image ID {expected_source_id}"
                )
            if require_ready and regular and entry.get("ready") is not True:
                raise ValueError(f"Pod/{name} container {container_name!r} is not Ready")
            result.append(
                ContainerRuntimeIdentity(
                    name=container_name,
                    image=normalize_image_reference(declared_image),
                    image_id=normalized_id,
                )
            )
        return tuple(result)

    init_identities = identities(
        expected_contract.init_containers,
        status_key="initContainerStatuses",
        regular=False,
    )
    container_identities = identities(
        expected_contract.containers,
        status_key="containerStatuses",
        regular=True,
    )
    if require_ready:
        conditions = _sequence(status.get("conditions", []), field_name=f"Pod/{name} conditions")
        if not any(
            isinstance(condition, Mapping)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in conditions
        ):
            raise ValueError(f"Pod/{name} is not Ready")
    return PodRuntimeIdentity(
        name=name,
        uid=uid,
        init_containers=init_identities,
        containers=container_identities,
    )


def pod_identity_sha256(identities: Sequence[PodRuntimeIdentity]) -> str:
    """Return a stable digest for one exact pod UID/container/image set."""

    payload = [
        {
            "name": identity.name,
            "uid": identity.uid,
            "init_containers": [
                {
                    "name": container.name,
                    "image": container.image,
                    "image_id": container.image_id,
                }
                for container in identity.init_containers
            ],
            "containers": [
                {
                    "name": container.name,
                    "image": container.image,
                    "image_id": container.image_id,
                }
                for container in identity.containers
            ],
        }
        for identity in sorted(identities)
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def inspect_probe_contract(deployment: Mapping[str, Any], config_map: Mapping[str, Any]) -> str:
    """Verify live probe paths and Host headers match the local allow-list."""
    spec = _mapping(deployment.get("spec"), field_name="django-web spec")
    template = _mapping(spec.get("template"), field_name="django-web template")
    pod_spec = _mapping(template.get("spec"), field_name="django-web pod spec")
    containers = _sequence(pod_spec.get("containers"), field_name="django-web containers")
    web = next(
        (
            _mapping(value, field_name="django-web container")
            for value in containers
            if isinstance(value, Mapping) and value.get("name") == "django-web"
        ),
        None,
    )
    if web is None:
        raise ValueError("live django-web deployment has no django-web container")

    hosts: set[str] = set()
    for probe_name in ("readinessProbe", "livenessProbe"):
        probe = _mapping(web.get(probe_name), field_name=probe_name)
        http_get = _mapping(probe.get("httpGet"), field_name=f"{probe_name}.httpGet")
        if http_get.get("path") != EXPECTED_PROBE_PATH:
            raise ValueError(f"{probe_name} path is not {EXPECTED_PROBE_PATH}")
        headers = _sequence(http_get.get("httpHeaders"), field_name=f"{probe_name} headers")
        host_values = [
            header.get("value")
            for value in headers
            if (header := _mapping(value, field_name=f"{probe_name} header"))
            and str(header.get("name", "")).lower() == "host"
        ]
        if host_values != [EXPECTED_PROBE_HOST]:
            raise ValueError(f"{probe_name} must send exactly Host: {EXPECTED_PROBE_HOST}")
        hosts.add(cast("str", host_values[0]))

    data = _mapping(config_map.get("data"), field_name="django-ray ConfigMap data")
    allowed_hosts = {
        item.strip()
        for item in str(data.get("DJANGO_ALLOWED_HOSTS", "")).split(",")
        if item.strip()
    }
    if hosts - allowed_hosts:
        raise ValueError(f"probe hosts are absent from DJANGO_ALLOWED_HOSTS: {sorted(hosts)}")
    return EXPECTED_PROBE_HOST


def parse_runtime_archive_probe(value: str) -> tuple[int, str]:
    """Verify one generic Ray node and its mounted RuntimeEnv archive."""
    payload = _mapping(json.loads(value), field_name="Ray runtime probe")
    if payload.get("django_ray") != "absent":
        raise ValueError("generic Ray interpreter unexpectedly has django_ray installed")
    if payload.get("required_member") is not True:
        raise ValueError(f"RuntimeEnv archive is missing {RUNTIME_ENV_REQUIRED_MEMBER}")
    size = payload.get("bytes")
    digest = payload.get("sha256")
    if not isinstance(size, int) or size <= 0:
        raise ValueError("RuntimeEnv archive byte size is not positive")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("RuntimeEnv archive SHA-256 is invalid")
    return size, digest


def parse_task_result(value: object) -> object:
    """Decode the durable JSON result stored in the sample execution response."""
    if not isinstance(value, str):
        raise ValueError("durable task result_data must be a JSON string")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("durable task result_data is not valid JSON") from error


class LocalKubeRayGate:
    """Orchestrate the guarded gate and retain only secret-free evidence."""

    def __init__(
        self,
        config: GateConfig,
        *,
        runner: Runner | None = None,
        output: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        if runner is None:
            self.redactor = Redactor()
            self.runner = Runner(
                redactor=self.redactor, timeout_seconds=self.config.command_timeout
            )
        else:
            self.runner = runner
            self.redactor = runner.redactor
        self.output = output
        self.evidence = GateEvidence()
        self.resources: list[dict[str, Any]] = []
        self.rendered = ""
        self.temp_root: Path | None = None
        self.source_context: Path | None = None
        self.kubeconfig_path: Path | None = None
        self._kubeconfig_digest: str | None = None
        self._kubernetes_server: str | None = None
        self._docker_host: str | None = None
        self.mutated = False
        self._api_token: str | None = None
        self._ray_cluster_uid: str | None = None
        self._ray_pod_identities: frozenset[PodRuntimeIdentity] | None = None
        self.diagnostics_attempted = False
        self.rendered_ray_topology: RayTopology | None = None
        self.setup_pod_images: PodImageContract | None = None
        self.deployment_contracts: dict[str, DeploymentContract] = {}
        self.expected_ray_head_count = 0
        self.expected_ray_worker_count = 0
        self.http_opener = build_local_http_opener()

    def _emit(self, value: object) -> None:
        """Route every gate-owned output through one credential redactor."""

        self.output(self.redactor.clean(value))

    def _announce(self, layer: str) -> None:
        self._emit(f"[{layer}] starting")

    def _complete(self, layer: str) -> None:
        self._emit(f"[{layer}] passed")

    def _layer(
        self,
        layer: str,
        action: Callable[[], None],
        *,
        complete: bool = True,
    ) -> None:
        self._announce(layer)
        try:
            action()
        except GateError:
            raise
        except Exception as error:
            raise GateError(
                layer,
                _bounded_redacted_error(error, redactor=self.redactor),
            ) from error
        if complete:
            self._complete(layer)

    def _verify_kubeconfig_snapshot(self) -> None:
        """Fail before every API call if the private routing snapshot changed."""

        if self.kubeconfig_path is None or self._kubeconfig_digest is None:
            raise ValueError("private kubeconfig snapshot has not been initialized")
        try:
            payload = self.kubeconfig_path.read_bytes()
        except OSError as error:
            raise ValueError("private kubeconfig snapshot cannot be read") from error
        digest = hashlib.sha256(payload).hexdigest()
        if digest != self._kubeconfig_digest:
            raise ValueError("private kubeconfig snapshot changed during the gate")
        try:
            server = inspect_kubeconfig_snapshot(
                json.loads(payload), expected_context=self.config.context
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("private kubeconfig snapshot is no longer valid JSON") from error
        if server != self._kubernetes_server:
            raise ValueError("private kubeconfig snapshot API server identity changed")

    def _docker(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run Docker only through the immutable local endpoint validated in preflight."""

        if self._docker_host is None:
            raise ValueError("validated Docker endpoint has not been initialized")
        return self.runner.run(
            ["docker", "--host", self._docker_host, *args],
            cwd=self.config.root,
            timeout=self.config.command_timeout if timeout is None else timeout,
            env=sanitized_environment(DOCKER_ENVIRONMENT_KEYS),
        )

    def _kubectl(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        sensitive_output: bool = False,
    ) -> CommandResult:
        self._verify_kubeconfig_snapshot()
        assert self.kubeconfig_path is not None
        command = [
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig_path),
            "--context",
            self.config.context,
            f"--request-timeout={self.config.kubectl_request_timeout}s",
            "--namespace",
            self.config.namespace,
            *args,
        ]
        return self.runner.run(
            command,
            cwd=self.config.root,
            check=check,
            timeout=self.config.command_timeout if timeout is None else timeout,
            sensitive_output=sensitive_output,
            env=sanitized_environment(KUBECTL_ENVIRONMENT_KEYS),
        )

    def _kubectl_cluster(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        sensitive_output: bool = False,
    ) -> CommandResult:
        """Run an explicitly context-bound cluster-scoped kubectl command."""
        self._verify_kubeconfig_snapshot()
        assert self.kubeconfig_path is not None
        command = [
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig_path),
            "--context",
            self.config.context,
            f"--request-timeout={self.config.kubectl_request_timeout}s",
            *args,
        ]
        return self.runner.run(
            command,
            cwd=self.config.root,
            check=check,
            timeout=self.config.command_timeout if timeout is None else timeout,
            sensitive_output=sensitive_output,
            env=sanitized_environment(KUBECTL_ENVIRONMENT_KEYS),
        )

    def _rollout_command_timeout(self, logical_timeout: int | None = None) -> int:
        return (
            (self.config.rollout_timeout if logical_timeout is None else logical_timeout)
            + self.config.kubectl_request_timeout
            + 5
        )

    def _json_command(self, result: CommandResult, *, field_name: str) -> Mapping[str, Any]:
        return _mapping(json.loads(result.stdout), field_name=field_name)

    def _create_kubeconfig_snapshot(self, *, current_context: str) -> None:
        """Capture one private flattened kubeconfig without exposing credential output."""

        assert self.temp_root is not None
        result = self.runner.run(
            [
                "kubectl",
                "--context",
                self.config.context,
                "config",
                "view",
                "--minify",
                "--raw",
                "--flatten",
                "-o",
                "json",
            ],
            cwd=self.config.root,
            sensitive_output=True,
            env=sanitized_environment(KUBECTL_ENVIRONMENT_KEYS),
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ValueError("flattened kubeconfig snapshot is not valid JSON") from error
        register_kubeconfig_secrets(payload, redactor=self.redactor)
        if self.runner.redactor is not self.redactor:
            register_kubeconfig_secrets(payload, redactor=self.runner.redactor)
        server = inspect_kubeconfig_snapshot(payload, expected_context=self.config.context)
        validate_local_context(
            current=current_context,
            expected=self.config.context,
            server_url=server,
        )
        encoded = result.stdout.encode("utf-8")
        path = self.temp_root / "kubeconfig.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError as error:
            raise ValueError(
                "private kubeconfig snapshot permissions could not be restricted"
            ) from error
        self.kubeconfig_path = path
        self._kubeconfig_digest = hashlib.sha256(encoded).hexdigest()
        self._kubernetes_server = server
        self.evidence.kubeconfig_sha256 = self._kubeconfig_digest
        self.evidence.kubernetes_server = server
        self._verify_kubeconfig_snapshot()

    def _verify_source_identity(self) -> None:
        """Prove the checkout still identifies the immutable source captured at preflight."""

        current = self.runner.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=self.config.root
        ).stdout.strip()
        if current != self.evidence.commit:
            raise ValueError("Git HEAD changed after the gate captured its immutable commit")
        tree = self.runner.run(
            ["git", "rev-parse", "--verify", f"{self.evidence.commit}^{{tree}}"],
            cwd=self.config.root,
        ).stdout.strip()
        if tree != self.evidence.source_tree:
            raise ValueError("captured Git commit no longer resolves to the recorded source tree")
        status = self.runner.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.config.root
        ).stdout
        if status.strip():
            raise ValueError("the checkout changed after the gate captured its immutable source")

    def run(self) -> None:
        """Run preflight and, unless requested otherwise, every integration layer."""
        temporary: tempfile.TemporaryDirectory[str] | None = None
        primary_failure: GateError | None = None
        secondary_contexts: list[tuple[str, object]] = []
        interrupted: BaseException | None = None
        evidence_lines: tuple[str, ...] | None = None

        try:
            temporary = tempfile.TemporaryDirectory(prefix="django-ray-local-gate-")
        except Exception as error:
            self.diagnostics_attempted = True
            primary_failure = GateError(
                "preflight",
                _gate_error_detail(
                    "temporary workspace creation failed",
                    redactor=self.redactor,
                    contexts=(("workspace creation error", error),),
                ),
            )

        if temporary is not None:
            try:
                self.temp_root = Path(temporary.name)
                self._layer("preflight", self._preflight)
                if not self.config.preflight_only:
                    self._layer("images", self._build_images)
                    self._layer("apply", self._apply_overlay)
                    self._layer("setup", self._run_setup)
                    self._layer("workloads", self._apply_workloads)
                    self._layer("ray", self._prepare_ray)
                    self._layer("rollouts", self._restart_task_managers)
                    self._layer(
                        "app-convergence",
                        self._wait_for_application_topology,
                    )
                    self._layer("image-identity", self._verify_deployed_images)
                    self._layer("runtime-env", self._verify_generic_ray_nodes)
                    self._layer("probes", self._verify_probes)
                    self._layer("api-smoke", self._verify_api)
                    self._layer("workflow-progress", self._verify_complex_workflow_progress)
                    self._layer("workflow-admin", self._verify_workflow_admin)
                    self._layer("prometheus", self._verify_prometheus)

                    def prepare_evidence() -> None:
                        nonlocal evidence_lines
                        evidence_lines = self._evidence_lines()

                    # The final success line and evidence remain withheld until
                    # the private workspace and kubeconfig have been removed.
                    self._layer(
                        "final-identity",
                        prepare_evidence,
                        complete=False,
                    )
            except GateError as error:
                primary_failure = error
            except Exception as error:
                primary_failure = GateError(
                    "preflight",
                    _gate_error_detail(
                        "temporary workspace orchestration failed",
                        redactor=self.redactor,
                        contexts=(("workspace orchestration error", error),),
                    ),
                )
            except BaseException as error:
                interrupted = error

            if primary_failure is not None:
                self.diagnostics_attempted = True
                try:
                    self.diagnostics(primary_failure.layer)
                except Exception as diagnostic_error:
                    secondary_contexts.append(("bounded diagnostics unavailable", diagnostic_error))

            try:
                temporary.cleanup()
            except Exception as cleanup_error:
                self.diagnostics_attempted = True
                if interrupted is not None:
                    secondary_contexts.append(
                        ("temporary workspace cleanup also failed", cleanup_error)
                    )
                elif primary_failure is None:
                    cleanup_layer = "preflight" if self.config.preflight_only else "final-identity"
                    primary_failure = GateError(
                        cleanup_layer,
                        _gate_error_detail(
                            "temporary workspace cleanup failed",
                            redactor=self.redactor,
                            contexts=(("workspace cleanup error", cleanup_error),),
                        ),
                    )
                else:
                    secondary_contexts.append(
                        ("temporary workspace cleanup also failed", cleanup_error)
                    )
            finally:
                self.temp_root = None

        if interrupted is not None:
            if secondary_contexts:
                interrupted.add_note(
                    _gate_error_detail(
                        "gate interrupted",
                        redactor=self.redactor,
                        contexts=secondary_contexts,
                    )
                )
            raise interrupted

        if primary_failure is not None:
            if secondary_contexts:
                primary_failure = GateError(
                    primary_failure.layer,
                    _gate_error_detail(
                        primary_failure,
                        redactor=self.redactor,
                        contexts=secondary_contexts,
                    ),
                )
            raise primary_failure

        if self.config.preflight_only:
            self._emit(
                "Preflight-only mode made no Docker or Kubernetes changes; "
                f"planned source tag is {self.evidence.app_tag}."
            )
            return
        if evidence_lines is None:
            raise GateError(
                "final-identity",
                "final evidence was not prepared before workspace cleanup",
            )
        self._complete("final-identity")
        for line in evidence_lines:
            self._emit(line)

    def _preflight(self) -> None:
        validate_namespace(self.config.namespace)
        validate_local_http_url(self.config.web_url, option="--web-url")
        validate_local_http_url(self.config.prometheus_url, option="--prometheus-url")
        if self.config.ray_restart not in {"required", "skip"}:
            raise ValueError("Ray restart decision must be exactly 'required' or 'skip'")
        for executable in ("git", "docker", "kubectl"):
            if shutil.which(executable) is None:
                raise ValueError(f"required executable {executable!r} is not on PATH")

        root = self.runner.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=self.config.root
        ).stdout.strip()
        if Path(root).resolve() != self.config.root.resolve():
            raise ValueError(f"gate root {self.config.root} does not match Git root {root}")
        status = self.runner.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.config.root
        ).stdout
        if status.strip():
            raise ValueError("the gate requires a clean checkout so the image maps to exactly HEAD")
        commit = self.runner.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=self.config.root
        ).stdout.strip()
        source_tree = self.runner.run(
            ["git", "rev-parse", "--verify", f"{commit}^{{tree}}"], cwd=self.config.root
        ).stdout.strip()
        tag = source_bound_tag(
            source_tree,
            now=datetime.now(UTC),
            nonce=secrets.token_hex(4),
        )
        self.evidence.commit = commit
        self.evidence.source_tree = source_tree
        self.evidence.app_tag = f"{APP_IMAGE_REPOSITORY}:{tag}"
        self.evidence.worker_tag = f"{LEGACY_WORKER_IMAGE_REPOSITORY}:{tag}"

        current = self.runner.run(
            ["kubectl", "config", "current-context"],
            cwd=self.config.root,
            env=sanitized_environment(KUBECTL_ENVIRONMENT_KEYS),
        ).stdout.strip()
        self._create_kubeconfig_snapshot(current_context=current)

        docker_context = self.runner.run(
            ["docker", "context", "show"],
            cwd=self.config.root,
            env=sanitized_environment(DOCKER_ENVIRONMENT_KEYS),
        ).stdout.strip()
        docker_context_payload = self.runner.run(
            ["docker", "context", "inspect", docker_context],
            cwd=self.config.root,
            env=sanitized_environment(DOCKER_ENVIRONMENT_KEYS),
        )
        docker_contexts = _sequence(
            json.loads(docker_context_payload.stdout), field_name="Docker context inspect response"
        )
        if len(docker_contexts) != 1:
            raise ValueError("Docker context inspect must return exactly one context")
        docker_entry = _mapping(docker_contexts[0], field_name="Docker context")
        endpoints = _mapping(docker_entry.get("Endpoints"), field_name="Docker context endpoints")
        docker_endpoint = _mapping(endpoints.get("docker"), field_name="Docker endpoint")
        endpoint_host = docker_endpoint.get("Host")
        if not isinstance(endpoint_host, str):
            raise ValueError("Docker context has no daemon endpoint")
        validate_local_docker_endpoint(endpoint_host)
        docker_host_override = os.environ.get("DOCKER_HOST")
        if docker_host_override:
            validate_local_docker_endpoint(docker_host_override)
        self._docker_host = docker_host_override or endpoint_host
        self.evidence.docker_host = self._docker_host
        self._docker("info", "--format", "{{json .ServerVersion}}")
        self._kubectl_cluster(
            "get",
            "customresourcedefinition",
            "rayclusters.ray.io",
            "-o",
            "name",
        )
        self._secret_token()
        for kind in ("clusterrole", "clusterrolebinding"):
            legacy = self._kubectl_cluster(
                "get",
                kind,
                "prometheus-django-ray",
                "--ignore-not-found",
                "-o",
                "name",
            ).stdout.strip()
            if legacy:
                raise ValueError(
                    f"legacy cluster-scoped {legacy} still exists; remove it in a separately "
                    "reviewed one-time migration before this namespace-only gate"
                )

        assert self.temp_root is not None
        self.source_context = create_source_build_context(
            runner=self.runner,
            root=self.config.root,
            temporary_root=self.temp_root,
            commit=commit,
            source_tree=source_tree,
        )
        overlay = configure_overlay_copy(
            source_k8s=self.source_context / "k8s",
            destination_k8s=self.temp_root / "k8s",
            tag=tag,
        )
        rendered = self._kubectl_cluster("kustomize", str(overlay)).stdout
        resources = load_rendered_resources(rendered)
        inspect_rendered_resources(resources, namespace=self.config.namespace, tag=tag)
        expected_app_image = f"{APP_IMAGE_REPOSITORY}:{tag}"
        setup = next(
            resource
            for resource in resources
            if _resource_identity(resource) == SETUP_RESOURCE_IDENTITY
        )
        setup_spec = _pod_spec(setup)
        if setup_spec is None:
            raise ValueError(f"rendered Job/{SETUP_JOB} has no pod spec")
        self.setup_pod_images = pod_image_contract(setup_spec)
        if self.setup_pod_images.containers != ((SETUP_CONTAINER, expected_app_image),):
            raise ValueError(
                f"rendered Job/{SETUP_JOB} must have exactly the {SETUP_CONTAINER!r} "
                "source-bound regular container"
            )
        self.deployment_contracts = {}
        for name in APP_DEPLOYMENTS:
            deployment = next(
                resource
                for resource in resources
                if _resource_identity(resource) == ("apps/v1", "Deployment", name)
            )
            deployment_spec = _mapping(
                deployment.get("spec"), field_name=f"rendered Deployment/{name} spec"
            )
            selector = normalize_label_selector(
                deployment_spec.get("selector"),
                field_name=f"rendered Deployment/{name} selector",
            )
            replicas = deployment_spec.get("replicas")
            if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
                raise ValueError(f"rendered Deployment/{name} replicas must be positive")
            deployment_pod_spec = _pod_spec(deployment)
            if deployment_pod_spec is None:
                raise ValueError(f"rendered Deployment/{name} has no pod spec")
            images = pod_image_contract(deployment_pod_spec)
            if not any(image == expected_app_image for _, image in images.all):
                raise ValueError(
                    f"rendered Deployment/{name} has no source-bound application container"
                )
            self.deployment_contracts[name] = DeploymentContract(
                replicas=replicas,
                pod_images=images,
                selector=selector,
            )
        ray_cluster = next(
            resource
            for resource in resources
            if _resource_identity(resource) == ("ray.io/v1", "RayCluster", RAY_CLUSTER_NAME)
        )
        self.rendered_ray_topology = normalize_ray_topology(ray_cluster)
        (
            self.expected_ray_head_count,
            self.expected_ray_worker_count,
        ) = self.rendered_ray_topology.counts
        self.rendered = rendered
        self.resources = resources
        rendered_file = self.temp_root / "rendered.yaml"
        rendered_file.write_text(rendered, encoding="utf-8", newline="\n")
        self._kubectl("apply", "--dry-run=client", "-f", str(rendered_file))

        if self.config.context.startswith("kind-"):
            if shutil.which("kind") is None:
                raise ValueError("kind is required for a named Kind context")
            expected_name = self.config.context.removeprefix("kind-")
            if self.config.kind_cluster_name not in {None, expected_name}:
                raise ValueError(
                    "--kind-cluster-name must match the cluster encoded in the Kind context"
                )
        self._verify_source_identity()

    def _build_images(self) -> None:
        if self.source_context is None:
            raise ValueError("immutable source archive has not been initialized")
        self._verify_source_identity()
        context = self.source_context
        labels = (
            f"org.opencontainers.image.revision={self.evidence.commit}",
            f"org.opencontainers.image.source-tree={self.evidence.source_tree}",
        )
        builds = (
            (self.evidence.app_tag, context / "Dockerfile"),
            (self.evidence.worker_tag, context / "Dockerfile.ray"),
        )
        for tag, dockerfile in builds:
            command = ["docker", "build", "--tag", tag]
            for label in labels:
                command.extend(["--label", label])
            command.extend(["--file", str(dockerfile), str(context)])
            self._docker(*command[1:], timeout=self.config.build_timeout)

        self.evidence.app_image_id = parse_docker_image_inspect(
            self._docker("image", "inspect", self.evidence.app_tag).stdout,
            expected_tag=self.evidence.app_tag,
            commit=self.evidence.commit,
            source_tree=self.evidence.source_tree,
        )
        self.evidence.worker_image_id = parse_docker_image_inspect(
            self._docker("image", "inspect", self.evidence.worker_tag).stdout,
            expected_tag=self.evidence.worker_tag,
            commit=self.evidence.commit,
            source_tree=self.evidence.source_tree,
        )

        if self.config.context.startswith("kind-"):
            cluster_name = self.config.kind_cluster_name or self.config.context.removeprefix(
                "kind-"
            )
            assert self._docker_host is not None
            kind_environment = sanitized_environment(
                KIND_ENVIRONMENT_KEYS,
                additions={
                    "DOCKER_HOST": self._docker_host,
                    "KIND_EXPERIMENTAL_PROVIDER": "docker",
                },
            )
            self.runner.run(
                [
                    "kind",
                    "load",
                    "docker-image",
                    self.evidence.app_tag,
                    self.evidence.worker_tag,
                    "--name",
                    cluster_name,
                ],
                cwd=self.config.root,
                timeout=self.config.build_timeout,
                env=kind_environment,
            )

    def _apply_overlay(self) -> None:
        assert self.temp_root is not None
        prerequisites, setup, workloads = split_apply_resources(self.resources)
        prerequisites_file = self.temp_root / "prerequisites.yaml"
        setup_file = self.temp_root / "setup-job.yaml"
        workloads_file = self.temp_root / "workloads.yaml"
        prerequisites_file.write_text(
            yaml.safe_dump_all(prerequisites, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )
        setup_file.write_text(
            yaml.safe_dump(setup, sort_keys=False), encoding="utf-8", newline="\n"
        )
        workloads_file.write_text(
            yaml.safe_dump_all(workloads, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )
        self.mutated = True
        self._kubectl("apply", "-f", str(prerequisites_file))
        self._kubectl("rollout", "restart", "deployment/prometheus")
        self._kubectl(
            "rollout",
            "status",
            "deployment/postgres",
            f"--timeout={self.config.rollout_timeout}s",
            timeout=self._rollout_command_timeout(),
        )
        self._kubectl(
            "rollout",
            "status",
            "deployment/prometheus",
            f"--timeout={self.config.rollout_timeout}s",
            timeout=self._rollout_command_timeout(),
        )

    def _run_setup(self) -> None:
        assert self.temp_root is not None
        setup_file = self.temp_root / "setup-job.yaml"
        self._kubectl(
            "delete",
            "job",
            SETUP_JOB,
            "--ignore-not-found=true",
            "--wait=true",
            f"--timeout={self.config.rollout_timeout}s",
            timeout=self._rollout_command_timeout(),
        )
        self._kubectl("apply", "-f", str(setup_file))
        self._kubectl(
            "wait",
            "--for=condition=complete",
            f"job/{SETUP_JOB}",
            f"--timeout={self.config.rollout_timeout}s",
            timeout=self._rollout_command_timeout(),
        )
        setup_log = self._kubectl("logs", f"job/{SETUP_JOB}", "--tail=200").stdout
        inspect_setup_log(setup_log)
        setup_job = self._json_command(
            self._kubectl("get", "job", SETUP_JOB, "-o", "json"),
            field_name=f"Job/{SETUP_JOB}",
        )
        if _resource_identity(setup_job) != SETUP_RESOURCE_IDENTITY:
            raise ValueError(f"live setup object is not Job/{SETUP_JOB}")
        setup_job_metadata = _metadata(setup_job)
        if setup_job_metadata.get("namespace") != self.config.namespace:
            raise ValueError("setup Job escaped the guarded namespace")
        setup_job_uid = setup_job_metadata.get("uid")
        if not isinstance(setup_job_uid, str) or not setup_job_uid:
            raise ValueError("setup Job has no stable UID")
        setup_payload = self._json_command(
            self._kubectl("get", "pods", "-l", f"job-name={SETUP_JOB}", "-o", "json"),
            field_name="setup pod list",
        )
        setup_pods = _sequence(setup_payload.get("items"), field_name="setup pod items")
        if len(setup_pods) != 1:
            raise ValueError(f"expected one setup pod, found {len(setup_pods)}")
        setup_pod = _mapping(setup_pods[0], field_name="setup pod")
        inspect_controlling_owner(
            setup_pod,
            namespace=self.config.namespace,
            api_version="batch/v1",
            kind="Job",
            name=SETUP_JOB,
            uid=setup_job_uid,
        )
        if self.setup_pod_images is None:
            raise ValueError("rendered setup pod image contract was not captured in preflight")
        expected_containers = self._verify_pod_image_ids(
            setup_pod,
            expected_tag=self.evidence.app_tag,
            expected_id=self.evidence.app_image_id,
            expected_contract=self.setup_pod_images,
        )
        rendered_source_count = sum(
            image == self.evidence.app_tag for _, image in self.setup_pod_images.all
        )
        if expected_containers != rendered_source_count:
            raise ValueError(
                f"setup pod must run exactly {rendered_source_count} source-bound container(s); "
                f"found {expected_containers}"
            )

    def _apply_workloads(self) -> None:
        """Reconcile application and Ray workloads only after setup passed."""
        assert self.temp_root is not None
        self._kubectl("apply", "-f", str(self.temp_root / "workloads.yaml"))

    def _expected_ray_distribution(self) -> dict[str, int]:
        if self.rendered_ray_topology is None:
            raise ValueError("rendered Ray topology was not captured in preflight")
        return {
            "head": 1,
            **{
                f"worker:{group.name}": group.replicas
                for group in self.rendered_ray_topology.worker_groups
            },
        }

    def _rendered_ray_pod_contract(self, pod: Mapping[str, Any]) -> tuple[str, PodImageContract]:
        if self.rendered_ray_topology is None:
            raise ValueError("rendered Ray topology was not captured in preflight")
        labels = _mapping(_metadata(pod).get("labels"), field_name="Ray pod labels")
        component = labels.get("component")
        if component == "head":
            return "head", self.rendered_ray_topology.head_pod_images
        if component != "worker":
            raise ValueError(f"RayCluster pod has unexpected component {component!r}")
        group_name = labels.get(RAY_GROUP_LABEL)
        groups = {group.name: group for group in self.rendered_ray_topology.worker_groups}
        if not isinstance(group_name, str) or group_name not in groups:
            raise ValueError("Ray worker pod does not identify one rendered worker group")
        return f"worker:{group_name}", groups[group_name].pod_images

    def _ray_pod_contract(self, pod: Mapping[str, Any]) -> tuple[str, PodImageContract]:
        """Return the exact effective KubeRay 1.6.2 pod image inventory."""

        role, rendered = self._rendered_ray_pod_contract(pod)
        if role == "head":
            return role, rendered
        if rendered.init_containers:
            raise ValueError(
                "the supported KubeRay worker template must not declare init containers; "
                f"KubeRay 1.6.2 owns the exact {KUBERAY_WAIT_GCS_INIT!r} init contract"
            )
        if len(rendered.containers) != 1 or rendered.containers[0][0] != "ray-worker":
            raise ValueError(
                "the supported KubeRay worker template must declare exactly one "
                "ray-worker container"
            )
        ray_image = rendered.containers[0][1]
        leaf = ray_image.rsplit("/", 1)[-1]
        if ray_image.endswith(":latest") or (":" not in leaf and "@" not in leaf):
            raise ValueError("the supported KubeRay worker image must use a pinned reference")
        return role, PodImageContract(
            init_containers=((KUBERAY_WAIT_GCS_INIT, ray_image),),
            containers=rendered.containers,
        )

    def _expected_wait_gcs_script_lines(self) -> tuple[str, ...]:
        address = f"{RAY_CLUSTER_NAME}-head-svc.{self.config.namespace}.svc.cluster.local:6379"
        return (
            "SECONDS=0",
            "while true; do",
            "if (( SECONDS <= 120 )); then",
            f"if ray health-check --address {address} > /dev/null 2>&1; then",
            'echo "GCS is ready."',
            "break",
            "fi",
            'echo "$SECONDS seconds elapsed: Waiting for GCS to be ready."',
            "else",
            f"if ray health-check --address {address}; then",
            'echo "GCS is ready. Any error messages above can be safely ignored."',
            "break",
            "fi",
            (
                'echo "$SECONDS seconds elapsed: Still waiting for GCS to be ready. '
                "For troubleshooting, refer to the FAQ at "
                'https://docs.ray.io/en/master/cluster/kubernetes/troubleshooting.html."'
            ),
            "fi",
            "sleep 5",
            "done",
        )

    def _validate_effective_worker_init_spec(self, pod: Mapping[str, Any]) -> None:
        """Require KubeRay 1.6.2's sole injected worker readiness init."""

        role, expected_contract = self._ray_pod_contract(pod)
        if not role.startswith("worker:"):
            return
        spec = _mapping(pod.get("spec"), field_name="Ray worker pod spec")
        init_containers = _sequence(
            spec.get("initContainers", []), field_name="Ray worker initContainers"
        )
        if len(init_containers) != 1:
            raise ValueError(
                f"Ray worker must have exactly one {KUBERAY_WAIT_GCS_INIT!r} init container"
            )
        init = _mapping(init_containers[0], field_name="Ray worker wait-gcs-ready init")
        expected_name, expected_image = expected_contract.init_containers[0]
        if init.get("name") != expected_name or init.get("image") != expected_image:
            raise ValueError(
                "Ray worker wait-gcs-ready init name/image does not match the pinned "
                "KubeRay contract"
            )
        command = _sequence(init.get("command"), field_name="wait-gcs-ready command")
        if command != ["/bin/bash", "-c", "--"]:
            raise ValueError("wait-gcs-ready command does not match KubeRay 1.6.2")
        args = _sequence(init.get("args"), field_name="wait-gcs-ready args")
        if len(args) != 1 or not isinstance(args[0], str):
            raise ValueError("wait-gcs-ready must have exactly one shell-script argument")
        observed_lines = tuple(line.strip() for line in args[0].splitlines() if line.strip())
        if observed_lines != self._expected_wait_gcs_script_lines():
            raise ValueError(
                "wait-gcs-ready script does not match the exact KubeRay 1.6.2 "
                "GCS health-check semantics"
            )

    def _validate_effective_worker_init_status(self, pod: Mapping[str, Any]) -> None:
        role, _ = self._ray_pod_contract(pod)
        if not role.startswith("worker:"):
            return
        name = str(_metadata(pod).get("name"))
        status = _mapping(pod.get("status"), field_name=f"Pod/{name} status")
        init_statuses = _sequence(
            status.get("initContainerStatuses", []),
            field_name=f"Pod/{name} initContainerStatuses",
        )
        if len(init_statuses) != 1:
            raise ValueError(
                f"Pod/{name} must report exactly one {KUBERAY_WAIT_GCS_INIT!r} init status"
            )
        init_status = _mapping(init_statuses[0], field_name=f"Pod/{name} wait-gcs-ready status")
        if init_status.get("name") != KUBERAY_WAIT_GCS_INIT:
            raise ValueError(f"Pod/{name} substituted the wait-gcs-ready init status")
        restart_count = init_status.get("restartCount")
        if isinstance(restart_count, bool) or restart_count != 0:
            raise ValueError(f"Pod/{name} wait-gcs-ready init restarted")
        state = _mapping(init_status.get("state"), field_name=f"Pod/{name} wait-gcs-ready state")
        terminated = _mapping(
            state.get("terminated"), field_name=f"Pod/{name} wait-gcs-ready termination"
        )
        exit_code = terminated.get("exitCode")
        if isinstance(exit_code, bool) or exit_code != 0 or terminated.get("reason") != "Completed":
            raise ValueError(f"Pod/{name} wait-gcs-ready init did not terminate successfully")

    @staticmethod
    def _contract_names(contract: PodImageContract) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (
            tuple(name for name, _ in contract.init_containers),
            tuple(name for name, _ in contract.containers),
        )

    def _validate_restart_discovery_pod(self, pod: Mapping[str, Any]) -> None:
        """Validate a bounded old-generation pod without requiring new images."""

        name = _metadata(pod).get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= MAX_RAY_POD_NAME_CHARACTERS:
            raise ValueError("restart-discovery Ray pod has no bounded stable name")
        spec = _mapping(pod.get("spec"), field_name=f"Pod/{name} spec")
        observed = pod_image_contract(spec)
        _, expected = self._ray_pod_contract(pod)
        if self._contract_names(observed) != self._contract_names(expected):
            raise ValueError(
                f"Pod/{name} container names do not match the supported KubeRay inventory; "
                "refusing deletion"
            )
        if not observed.containers or len(observed.all) > MAX_RAY_POD_CONTAINERS:
            raise ValueError(f"Pod/{name} container inventory is not safely bounded")
        for _, image in observed.all:
            if len(image) > MAX_RAY_IMAGE_REFERENCE_CHARACTERS:
                raise ValueError(f"Pod/{name} image reference exceeds the discovery bound")
            normalize_image_reference(image)
        status = _mapping(pod.get("status", {}), field_name=f"Pod/{name} status")
        if status.get("ephemeralContainerStatuses") not in (None, []):
            raise ValueError(f"Pod/{name} reported unexpected ephemeral container statuses")
        expected_status_names = {
            "initContainerStatuses": set(self._contract_names(observed)[0]),
            "containerStatuses": set(self._contract_names(observed)[1]),
        }
        for status_key, allowed_names in expected_status_names.items():
            statuses = _sequence(status.get(status_key, []), field_name=f"Pod/{name} {status_key}")
            observed_names: list[str] = []
            for index, value in enumerate(statuses):
                entry = _mapping(value, field_name=f"Pod/{name} {status_key}[{index}]")
                status_name = entry.get("name")
                if not isinstance(status_name, str) or not status_name:
                    raise ValueError(f"Pod/{name} {status_key}[{index}] has no valid name")
                observed_names.append(status_name)
            if len(observed_names) != len(set(observed_names)):
                raise ValueError(f"Pod/{name} {status_key} duplicated a container name")
            if not set(observed_names).issubset(allowed_names):
                raise ValueError(f"Pod/{name} {status_key} contains an unknown container")

    def _ray_distribution(self, pods: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        counts = dict.fromkeys(self._expected_ray_distribution(), 0)
        for pod in pods:
            key, _ = self._ray_pod_contract(pod)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _ray_runtime_identities(
        self, pods: Sequence[Mapping[str, Any]]
    ) -> frozenset[PodRuntimeIdentity]:
        observed_identities: list[PodRuntimeIdentity] = []
        for pod in pods:
            self._validate_effective_worker_init_spec(pod)
            identity = inspect_pod_runtime_identity(
                pod,
                namespace=self.config.namespace,
                expected_contract=self._ray_pod_contract(pod)[1],
                require_ready=True,
            )
            self._validate_effective_worker_init_status(pod)
            observed_identities.append(identity)
        identities = frozenset(observed_identities)
        if len(identities) != len(pods):
            raise ValueError("Ray pod runtime inventory contains duplicate identities")
        if len({identity.uid for identity in identities}) != len(identities):
            raise ValueError("Ray pod runtime inventory contains duplicate UIDs")
        return identities

    def _ray_pods(
        self,
        *,
        expected_cluster_uid: str | None = None,
        allow_empty: bool = False,
        contract_phase: RayPodContractPhase = "converged",
    ) -> tuple[str, list[Mapping[str, Any]]]:
        if contract_phase not in {"restart-discovery", "converged"}:
            raise ValueError(f"unknown Ray pod contract phase {contract_phase!r}")
        cluster = self._json_command(
            self._kubectl("get", "raycluster", RAY_CLUSTER_NAME, "-o", "json"),
            field_name=f"RayCluster/{RAY_CLUSTER_NAME}",
        )
        if _resource_identity(cluster) != ("ray.io/v1", "RayCluster", RAY_CLUSTER_NAME):
            raise ValueError(f"live object is not RayCluster/{RAY_CLUSTER_NAME}")
        cluster_metadata = _metadata(cluster)
        if cluster_metadata.get("namespace") != self.config.namespace:
            raise ValueError("RayCluster escaped the guarded namespace")
        cluster_uid = cluster_metadata.get("uid")
        if not isinstance(cluster_uid, str) or not cluster_uid:
            raise ValueError(f"RayCluster/{RAY_CLUSTER_NAME} has no stable UID")
        if expected_cluster_uid is not None and cluster_uid != expected_cluster_uid:
            raise ValueError(
                f"RayCluster/{RAY_CLUSTER_NAME} UID changed from {expected_cluster_uid} "
                f"to {cluster_uid} during convergence"
            )
        live_topology = normalize_ray_topology(cluster)
        if self.rendered_ray_topology is None:
            self.rendered_ray_topology = live_topology
        elif live_topology != self.rendered_ray_topology:
            raise ValueError(
                "live RayCluster topology does not match the exact rendered head/worker contract"
            )
        configured_topology = self.rendered_ray_topology.counts
        self.expected_ray_head_count, self.expected_ray_worker_count = configured_topology
        response = self._json_command(
            self._kubectl("get", "pods", "-o", "json"),
            field_name="namespace pod list",
        )
        items = _sequence(response.get("items"), field_name="namespace pod items")
        pods: list[Mapping[str, Any]] = []
        for item in items:
            pod = _mapping(item, field_name="namespace pod")
            metadata = _metadata(pod)
            labels = _mapping(metadata.get("labels", {}), field_name="pod labels")
            ray_owner_uid = any(owner.get("uid") == cluster_uid for owner in owner_references(pod))
            selected = labels.get(RAY_CLUSTER_LABEL) == RAY_CLUSTER_NAME
            if not ray_owner_uid and not selected:
                continue
            inspect_controlling_owner(
                pod,
                namespace=self.config.namespace,
                api_version="ray.io/v1",
                kind="RayCluster",
                name=RAY_CLUSTER_NAME,
                uid=cluster_uid,
            )
            if labels.get(RAY_CLUSTER_LABEL) != RAY_CLUSTER_NAME:
                raise ValueError("RayCluster-owned pod is missing the exact cluster label")
            if labels.get("app") != "ray":
                raise ValueError("RayCluster pod does not carry app=ray")
            component = labels.get("component")
            if component not in RAY_COMPONENTS:
                raise ValueError(
                    f"RayCluster pod has unexpected component {component!r}; refusing deletion"
                )
            uid = metadata.get("uid")
            if not isinstance(uid, str) or not 1 <= len(uid) <= MAX_RAY_POD_UID_CHARACTERS:
                raise ValueError("Ray pod has no bounded stable UID")
            _, expected_contract = self._ray_pod_contract(pod)
            pod_spec = _mapping(pod.get("spec"), field_name="Ray pod spec")
            if contract_phase == "restart-discovery":
                self._validate_restart_discovery_pod(pod)
            else:
                if pod_image_contract(pod_spec) != expected_contract:
                    raise ValueError(
                        "Ray pod image contract does not match its effective KubeRay "
                        "component topology"
                    )
                self._validate_effective_worker_init_spec(pod)
            pods.append(pod)
            if len(pods) > MAX_RAY_DISCOVERY_PODS:
                raise ValueError("Ray pod inventory exceeds the safe discovery bound")
        names = [str(_metadata(pod).get("name")) for pod in pods]
        uids = [str(_metadata(pod).get("uid")) for pod in pods]
        if len(names) != len(set(names)):
            raise ValueError("Ray pod inventory contains duplicate names")
        if len(uids) != len(set(uids)):
            raise ValueError("Ray pod inventory contains duplicate UIDs")
        if not pods and not allow_empty:
            raise ValueError("no Ray pods were found")
        return cluster_uid, pods

    def _wait_for_ray(self, *, cluster_uid: str) -> list[Mapping[str, Any]]:
        deadline = time.monotonic() + self.config.rollout_timeout
        while True:
            _, pods = self._ray_pods(
                expected_cluster_uid=cluster_uid,
                allow_empty=True,
            )
            observed = self._ray_distribution(pods)
            expected = self._expected_ray_distribution()
            if observed == expected:
                break
            if any(observed.get(key, 0) > count for key, count in expected.items()):
                raise ValueError(
                    f"RayCluster produced excess pods {observed}; expected exactly {expected}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    f"RayCluster did not reach exact topology {expected} within "
                    f"{self.config.rollout_timeout}s (observed {observed})"
                )
            time.sleep(min(2, remaining))

        for component in ("head", "worker"):
            remaining_seconds = max(1, int(deadline - time.monotonic()) + 1)
            self._kubectl(
                "wait",
                "--for=condition=Ready",
                "pod",
                "-l",
                (f"{RAY_CLUSTER_LABEL}={RAY_CLUSTER_NAME},app=ray,component={component}"),
                f"--timeout={remaining_seconds}s",
                timeout=self._rollout_command_timeout(remaining_seconds),
            )
        _, ready_pods = self._ray_pods(expected_cluster_uid=cluster_uid)
        ready_counts = self._ray_distribution(ready_pods)
        expected_counts = self._expected_ray_distribution()
        if ready_counts != expected_counts:
            raise ValueError(
                f"Ray topology changed after readiness: {ready_counts}, expected {expected_counts}"
            )
        return ready_pods

    def _prepare_ray(self) -> None:
        discovery_phase: RayPodContractPhase = (
            "restart-discovery" if self.config.ray_restart == "required" else "converged"
        )
        cluster_uid, discovered_pods = self._ray_pods(
            allow_empty=True,
            contract_phase=discovery_phase,
        )
        self._ray_cluster_uid = cluster_uid
        self.evidence.ray_cluster_uid = cluster_uid
        if self.config.ray_restart == "required":
            old_uids = {str(_metadata(pod).get("uid")) for pod in discovered_pods}
            names = [str(_metadata(pod).get("name")) for pod in discovered_pods]
            if names:
                self._kubectl(
                    "delete",
                    "pod",
                    *names,
                    "--wait=true",
                    f"--timeout={self.config.rollout_timeout}s",
                    timeout=self._rollout_command_timeout(),
                )
            new_pods = self._wait_for_ray(cluster_uid=cluster_uid)
            new_uids = {str(_metadata(pod).get("uid")) for pod in new_pods}
            if old_uids & new_uids:
                raise ValueError("cold Ray restart retained at least one old pod UID")
            self.evidence.ray_restart = "performed"
        else:
            new_pods = self._wait_for_ray(cluster_uid=cluster_uid)
            self.evidence.ray_restart = "skipped-by-explicit-trigger-choice"
        distribution = self._ray_distribution(new_pods)
        self.evidence.ray_head_count = distribution.get("head", 0)
        self.evidence.ray_worker_count = sum(
            count for key, count in distribution.items() if key.startswith("worker:")
        )
        expected_counts = (self.expected_ray_head_count, self.expected_ray_worker_count)
        observed_counts = (self.evidence.ray_head_count, self.evidence.ray_worker_count)
        if observed_counts != expected_counts:
            raise ValueError(
                f"expected exact Ray topology {expected_counts}, observed {observed_counts}"
            )
        identities = self._ray_runtime_identities(new_pods)
        self._ray_pod_identities = identities
        self.evidence.ray_pod_identity_sha256 = pod_identity_sha256(tuple(identities))

    def _restart_task_managers(self) -> None:
        resources = [f"deployment/{name}" for name in TASK_MANAGER_DEPLOYMENTS]
        self._kubectl("rollout", "restart", *resources)
        for name in APP_DEPLOYMENTS:
            self._kubectl(
                "rollout",
                "status",
                f"deployment/{name}",
                f"--timeout={self.config.rollout_timeout}s",
                timeout=self._rollout_command_timeout(),
            )

    @staticmethod
    def _selector(deployment: Mapping[str, Any]) -> str:
        spec = _mapping(deployment.get("spec"), field_name="Deployment spec")
        selector = _mapping(spec.get("selector"), field_name="Deployment selector")
        labels = _mapping(selector.get("matchLabels"), field_name="Deployment selector labels")
        if not labels:
            raise ValueError("Deployment selector has no matchLabels")
        return ",".join(f"{key}={labels[key]}" for key in sorted(labels))

    def _verify_pod_image_ids(
        self,
        pod: Mapping[str, Any],
        *,
        expected_tag: str,
        expected_id: str,
        expected_contract: PodImageContract | None = None,
    ) -> int:
        spec = _mapping(pod.get("spec"), field_name="pod spec")
        actual_contract = pod_image_contract(spec)
        contract = actual_contract if expected_contract is None else expected_contract
        inspect_pod_runtime_identity(
            pod,
            namespace=self.config.namespace,
            expected_contract=contract,
            expected_source_tag=expected_tag,
            expected_source_id=expected_id,
            require_ready=False,
        )
        expected_names = {name for name, image in contract.all if image == expected_tag}
        if not expected_names:
            raise ValueError(f"pod does not declare any container using {expected_tag}")
        return len(expected_names)

    @staticmethod
    def _replica_count(value: object, *, field_name: str, default: int | None = None) -> int:
        """Return one non-negative Kubernetes replica count without bool coercion."""

        if value is None and default is not None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
        return value

    def _application_command_timeout(self, deadline: float | None) -> float | None:
        """Keep every application-inventory request inside one logical deadline."""

        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApplicationTopologyPendingError(
                "application topology deadline expired during namespace inventory"
            )
        return min(float(self.config.command_timeout), remaining)

    @staticmethod
    def _deletion_requested(metadata: Mapping[str, Any], *, field_name: str) -> bool:
        """Validate one optional Kubernetes RFC 3339 deletion timestamp."""

        value = metadata.get("deletionTimestamp")
        if value is None:
            return False
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise ValueError(f"{field_name}.deletionTimestamp is not a bounded RFC 3339 value")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                f"{field_name}.deletionTimestamp is not a valid RFC 3339 value"
            ) from error
        if parsed.tzinfo is None:
            raise ValueError(f"{field_name}.deletionTimestamp must include a timezone")
        return True

    def _application_pod_runtime_identity(
        self,
        pod: Mapping[str, Any],
        *,
        deployment_name: str,
        contract: DeploymentContract,
    ) -> PodRuntimeIdentity:
        """Validate one current pod, classifying readiness-only gaps as retryable."""

        metadata = _metadata(pod)
        pod_name = metadata.get("name")
        spec = _mapping(pod.get("spec"), field_name=f"Pod/{pod_name} spec")
        if pod_image_contract(spec) != contract.pod_images:
            raise ValueError(
                f"Pod/{pod_name} image contract does not match Deployment/"
                f"{deployment_name}'s rendered topology"
            )
        status = _mapping(pod.get("status"), field_name=f"Pod/{pod_name} status")
        if status.get("ephemeralContainerStatuses") not in (None, []):
            raise ValueError(f"Pod/{pod_name} reported unexpected ephemeral container statuses")
        if self._deletion_requested(metadata, field_name=f"Pod/{pod_name} metadata"):
            raise ApplicationTopologyPendingError(
                f"current Deployment/{deployment_name} Pod/{pod_name} is terminating"
            )
        for expected, status_key, regular in (
            (contract.pod_images.init_containers, "initContainerStatuses", False),
            (contract.pod_images.containers, "containerStatuses", True),
        ):
            statuses = _sequence(
                status.get(status_key, []),
                field_name=f"Pod/{pod_name} {status_key}",
            )
            by_name: dict[str, Mapping[str, Any]] = {}
            for index, value in enumerate(statuses):
                entry = _mapping(value, field_name=f"Pod/{pod_name} {status_key}[{index}]")
                container_name = entry.get("name")
                if not isinstance(container_name, str) or not container_name:
                    raise ValueError(f"Pod/{pod_name} {status_key}[{index}] has no valid name")
                if container_name in by_name:
                    raise ValueError(
                        f"Pod/{pod_name} duplicated {status_key} for {container_name!r}"
                    )
                by_name[container_name] = entry
            expected_by_name = dict(expected)
            unexpected = set(by_name) - set(expected_by_name)
            if unexpected:
                raise ValueError(
                    f"Pod/{pod_name} {status_key} contains unexpected names {sorted(unexpected)}"
                )
            missing = set(expected_by_name) - set(by_name)
            if missing:
                raise ApplicationTopologyPendingError(
                    f"current Deployment/{deployment_name} Pod/{pod_name} has not reported "
                    f"{status_key} for {sorted(missing)}"
                )
            for container_name, entry in by_name.items():
                declared_image = expected_by_name[container_name]
                status_image = entry.get("image")
                if status_image in (None, ""):
                    raise ApplicationTopologyPendingError(
                        f"current Deployment/{deployment_name} Pod/{pod_name} container "
                        f"{container_name!r} has not reported its image"
                    )
                if not isinstance(status_image, str) or (
                    normalize_image_reference(status_image)
                    != normalize_image_reference(declared_image)
                ):
                    raise ValueError(
                        f"Pod/{pod_name} status image for {container_name!r} does not match "
                        "its rendered image"
                    )
                image_id = entry.get("imageID")
                if image_id in (None, ""):
                    raise ApplicationTopologyPendingError(
                        f"current Deployment/{deployment_name} Pod/{pod_name} container "
                        f"{container_name!r} has not reported its runtime image ID"
                    )
                if not isinstance(image_id, str):
                    raise ValueError(
                        f"Pod/{pod_name} container {container_name!r} has an invalid image ID"
                    )
                normalized_id = normalize_runtime_image_id(image_id)
                if (
                    declared_image == self.evidence.app_tag
                    and normalized_id != self.evidence.app_image_id
                ):
                    raise ValueError(
                        f"Pod/{pod_name} source container {container_name!r} does not run "
                        f"the locally built image ID {self.evidence.app_image_id}"
                    )
                if regular and entry.get("ready") is not True:
                    raise ApplicationTopologyPendingError(
                        f"current Deployment/{deployment_name} Pod/{pod_name} container "
                        f"{container_name!r} is not Ready"
                    )
        conditions = _sequence(
            status.get("conditions", []), field_name=f"Pod/{pod_name} conditions"
        )
        if not any(
            isinstance(condition, Mapping)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in conditions
        ):
            raise ApplicationTopologyPendingError(
                f"current Deployment/{deployment_name} Pod/{pod_name} is not Ready"
            )
        return inspect_pod_runtime_identity(
            pod,
            namespace=self.config.namespace,
            expected_contract=contract.pod_images,
            expected_source_tag=self.evidence.app_tag,
            expected_source_id=self.evidence.app_image_id,
            require_ready=True,
        )

    def _inspect_application_topology(self, *, deadline: float | None = None) -> dict[str, int]:
        """Inspect one complete Deployment -> current ReplicaSet -> pod snapshot."""

        if set(self.deployment_contracts) != set(APP_DEPLOYMENTS):
            raise ValueError("rendered application Deployment contracts are incomplete")
        deployment_uids: dict[str, str] = {}
        deployment_revisions: dict[str, str] = {}
        pending: list[str] = []
        for name in APP_DEPLOYMENTS:
            contract = self.deployment_contracts[name]
            deployment = self._json_command(
                self._kubectl(
                    "get",
                    "deployment",
                    name,
                    "-o",
                    "json",
                    timeout=self._application_command_timeout(deadline),
                ),
                field_name=f"Deployment/{name}",
            )
            if _resource_identity(deployment) != ("apps/v1", "Deployment", name):
                raise ValueError(f"live object is not Deployment/{name}")
            metadata = _metadata(deployment)
            if metadata.get("namespace") != self.config.namespace:
                raise ValueError(f"Deployment/{name} escaped the guarded namespace")
            if self._deletion_requested(metadata, field_name=f"Deployment/{name} metadata"):
                raise ValueError(f"Deployment/{name} is being deleted")
            deployment_uid = metadata.get("uid")
            generation = metadata.get("generation")
            if not isinstance(deployment_uid, str) or not deployment_uid:
                raise ValueError(f"Deployment/{name} has no stable UID")
            if len(deployment_uid) > MAX_RAY_POD_UID_CHARACTERS:
                raise ValueError(f"Deployment/{name} UID exceeds the safe bound")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                raise ValueError(f"Deployment/{name} has no valid generation")
            spec = _mapping(deployment.get("spec"), field_name=f"Deployment/{name} spec")
            live_selector = normalize_label_selector(
                spec.get("selector"), field_name=f"Deployment/{name} selector"
            )
            if live_selector != contract.selector:
                raise ValueError(f"Deployment/{name} selector does not match rendered topology")
            template = _mapping(spec.get("template"), field_name=f"Deployment/{name} template")
            template_metadata = _mapping(
                template.get("metadata", {}),
                field_name=f"Deployment/{name} template metadata",
            )
            if not labels_match_selector(template_metadata.get("labels", {}), contract.selector):
                raise ValueError(
                    f"Deployment/{name} pod template is hidden from its rendered selector"
                )
            pod_spec = _mapping(template.get("spec"), field_name=f"Deployment/{name} pod spec")
            if pod_image_contract(pod_spec) != contract.pod_images:
                raise ValueError(
                    f"Deployment/{name} image contract does not match rendered topology"
                )
            desired = self._replica_count(
                spec.get("replicas"), field_name=f"Deployment/{name} spec.replicas"
            )
            status = _mapping(deployment.get("status"), field_name=f"Deployment/{name} status")
            if desired != contract.replicas:
                raise ValueError(
                    f"Deployment/{name} replicas changed from rendered {contract.replicas} "
                    f"to live {desired!r}"
                )
            if status.get("observedGeneration") != generation:
                pending.append(f"Deployment/{name} has not observed generation {generation}")
            for status_field in (
                "replicas",
                "updatedReplicas",
                "readyReplicas",
                "availableReplicas",
            ):
                count = self._replica_count(
                    status.get(status_field),
                    field_name=f"Deployment/{name} status.{status_field}",
                    default=0,
                )
                if count != contract.replicas:
                    pending.append(
                        f"Deployment/{name} status.{status_field} is "
                        f"{count!r}, expected exactly {contract.replicas}"
                    )
            unavailable = self._replica_count(
                status.get("unavailableReplicas"),
                field_name=f"Deployment/{name} status.unavailableReplicas",
                default=0,
            )
            if unavailable != 0:
                pending.append(f"Deployment/{name} still has unavailable replicas")
            annotations = _mapping(
                metadata.get("annotations", {}),
                field_name=f"Deployment/{name} annotations",
            )
            revision = annotations.get("deployment.kubernetes.io/revision")
            if not isinstance(revision, str) or re.fullmatch(r"[1-9][0-9]*", revision) is None:
                pending.append(f"Deployment/{name} has not published a valid current revision")
            else:
                deployment_revisions[name] = revision
            deployment_uids[name] = deployment_uid
        if len(set(deployment_uids.values())) != len(deployment_uids):
            raise ValueError("application Deployment inventory contains duplicate UIDs")
        deployment_names_by_uid = {
            deployment_uid: name for name, deployment_uid in deployment_uids.items()
        }

        replicasets_payload = self._json_command(
            self._kubectl(
                "get",
                "replicasets",
                "-o",
                "json",
                timeout=self._application_command_timeout(deadline),
            ),
            field_name="namespace ReplicaSet inventory",
        )
        replicasets = _sequence(
            replicasets_payload.get("items"), field_name="namespace ReplicaSet items"
        )
        if len(replicasets) > MAX_APPLICATION_REPLICASETS:
            raise ValueError("application ReplicaSet inventory exceeds the safe absolute bound")
        replicasets_by_uid: dict[str, Mapping[str, Any]] = {}
        for index, value in enumerate(replicasets):
            replicaset = _mapping(value, field_name=f"namespace ReplicaSet[{index}]")
            api_version, kind, replicaset_name = _resource_identity(replicaset)
            if (api_version, kind) != ("apps/v1", "ReplicaSet"):
                raise ValueError(
                    f"ReplicaSet inventory returned unexpected {api_version} {kind}/{replicaset_name}"
                )
            metadata = _metadata(replicaset)
            if metadata.get("namespace") != self.config.namespace:
                raise ValueError(f"ReplicaSet/{replicaset_name} escaped the guarded namespace")
            uid = metadata.get("uid")
            if not isinstance(uid, str) or not uid:
                raise ValueError(f"ReplicaSet/{replicaset_name} has no stable UID")
            if len(uid) > MAX_RAY_POD_UID_CHARACTERS:
                raise ValueError(f"ReplicaSet/{replicaset_name} UID exceeds the safe bound")
            if len(replicaset_name) > MAX_RAY_POD_NAME_CHARACTERS:
                raise ValueError("ReplicaSet inventory contains an overlong name")
            if uid in replicasets_by_uid:
                raise ValueError(f"ReplicaSet inventory duplicated UID {uid}")
            replicasets_by_uid[uid] = replicaset

        pods_payload = self._json_command(
            self._kubectl(
                "get",
                "pods",
                "-o",
                "json",
                timeout=self._application_command_timeout(deadline),
            ),
            field_name="namespace application pod inventory",
        )
        pod_items = _sequence(pods_payload.get("items"), field_name="namespace pod items")
        if len(pod_items) > MAX_APPLICATION_PODS:
            raise ValueError("application pod inventory exceeds the safe absolute bound")
        namespace_pods: list[Mapping[str, Any]] = []
        namespace_pod_names: set[str] = set()
        namespace_pod_uids: set[str] = set()
        for index, value in enumerate(pod_items):
            pod = _mapping(value, field_name=f"namespace pod[{index}]")
            metadata = _metadata(pod)
            pod_name = metadata.get("name")
            pod_uid = metadata.get("uid")
            if _resource_identity(pod) != ("v1", "Pod", pod_name):
                raise ValueError(f"pod inventory returned an unexpected object named {pod_name!r}")
            if metadata.get("namespace") != self.config.namespace:
                raise ValueError(f"Pod/{pod_name} escaped the guarded namespace")
            if (
                not isinstance(pod_name, str)
                or not 1 <= len(pod_name) <= MAX_RAY_POD_NAME_CHARACTERS
            ):
                raise ValueError("application pod inventory contains no bounded stable name")
            if not isinstance(pod_uid, str) or not 1 <= len(pod_uid) <= MAX_RAY_POD_UID_CHARACTERS:
                raise ValueError(f"Pod/{pod_name} has no bounded stable UID")
            if pod_name in namespace_pod_names:
                raise ValueError(f"application pod inventory duplicated Pod/{pod_name}")
            if pod_uid in namespace_pod_uids:
                raise ValueError("application pod inventory contains duplicate pod UIDs")
            namespace_pod_names.add(pod_name)
            namespace_pod_uids.add(pod_uid)
            namespace_pods.append(pod)
        guarded_replicaset_owners: dict[str, str] = {}
        guarded_replicaset_selectors: dict[str, tuple[tuple[str, str], ...]] = {}
        guarded_replicaset_pod_contracts: dict[str, PodImageContract] = {}
        current_replicaset_uids: dict[str, str] = {}
        for replicaset_uid, replicaset in replicasets_by_uid.items():
            references = owner_references(replicaset)
            uid_claims = {
                deployment_names_by_uid[uid]
                for reference in references
                if isinstance((uid := reference.get("uid")), str) and uid in deployment_names_by_uid
            }
            name_claims = {
                name
                for reference in references
                if isinstance((name := reference.get("name")), str) and name in APP_DEPLOYMENTS
            }
            if len(uid_claims) > 1:
                raise ValueError("ReplicaSet claims multiple guarded Deployment UIDs")
            if uid_claims:
                deployment_name = next(iter(uid_claims))
            elif len(name_claims) == 1:
                deployment_name = next(iter(name_claims))
            elif name_claims:
                raise ValueError("ReplicaSet claims multiple guarded Deployment names")
            else:
                continue
            inspect_controlling_owner(
                replicaset,
                namespace=self.config.namespace,
                api_version="apps/v1",
                kind="Deployment",
                name=deployment_name,
                uid=deployment_uids[deployment_name],
            )
            guarded_replicaset_owners[replicaset_uid] = deployment_name
            metadata = _metadata(replicaset)
            replicaset_name = str(metadata.get("name"))
            annotations = _mapping(
                metadata.get("annotations", {}),
                field_name=f"ReplicaSet/{replicaset_name} annotations",
            )
            revision = annotations.get("deployment.kubernetes.io/revision")
            current_revision = deployment_revisions.get(deployment_name)
            is_current = current_revision is not None and revision == current_revision
            spec = _mapping(replicaset.get("spec"), field_name=f"ReplicaSet/{replicaset_name} spec")
            replicaset_selector = normalize_label_selector(
                spec.get("selector"),
                field_name=f"ReplicaSet/{replicaset_name} selector",
            )
            guarded_replicaset_selectors[replicaset_uid] = replicaset_selector
            template = _mapping(
                spec.get("template"),
                field_name=f"ReplicaSet/{replicaset_name} template",
            )
            template_metadata = _mapping(
                template.get("metadata"),
                field_name=f"ReplicaSet/{replicaset_name} template metadata",
            )
            template_labels = template_metadata.get("labels")
            if not labels_match_selector(template_labels, replicaset_selector):
                raise ValueError(
                    f"ReplicaSet/{replicaset_name} template does not match its own selector"
                )
            replicaset_pod_spec = _mapping(
                template.get("spec"),
                field_name=f"ReplicaSet/{replicaset_name} pod spec",
            )
            replicaset_pod_contract = pod_image_contract(replicaset_pod_spec)
            guarded_replicaset_pod_contracts[replicaset_uid] = replicaset_pod_contract
            status = _mapping(
                replicaset.get("status", {}),
                field_name=f"ReplicaSet/{replicaset_name} status",
            )
            spec_replicas = self._replica_count(
                spec.get("replicas"),
                field_name=f"ReplicaSet/{replicaset_name} spec.replicas",
            )
            status_replicas = self._replica_count(
                status.get("replicas"),
                field_name=f"ReplicaSet/{replicaset_name} status.replicas",
                default=0,
            )
            ready_replicas = self._replica_count(
                status.get("readyReplicas"),
                field_name=f"ReplicaSet/{replicaset_name} status.readyReplicas",
                default=0,
            )
            available_replicas = self._replica_count(
                status.get("availableReplicas"),
                field_name=f"ReplicaSet/{replicaset_name} status.availableReplicas",
                default=0,
            )
            if is_current:
                if deployment_name in current_replicaset_uids:
                    raise ValueError(
                        f"Deployment/{deployment_name} has multiple ReplicaSets claiming "
                        f"current revision {revision}"
                    )
                current_replicaset_uids[deployment_name] = replicaset_uid
                if self._deletion_requested(
                    metadata, field_name=f"ReplicaSet/{replicaset_name} metadata"
                ):
                    pending.append(f"current ReplicaSet/{replicaset_name} is being deleted")
                expected_replicas = self.deployment_contracts[deployment_name].replicas
                for field_name, count in (
                    ("spec.replicas", spec_replicas),
                    ("status.replicas", status_replicas),
                    ("status.readyReplicas", ready_replicas),
                    ("status.availableReplicas", available_replicas),
                ):
                    if count != expected_replicas:
                        pending.append(
                            f"current ReplicaSet/{replicaset_name} {field_name} is {count}, "
                            f"expected exactly {expected_replicas}"
                        )
                contract = self.deployment_contracts[deployment_name]
                if not labels_match_selector(template_labels, contract.selector):
                    raise ValueError(
                        f"current ReplicaSet/{replicaset_name} template is hidden from "
                        f"Deployment/{deployment_name}'s selector"
                    )
                if replicaset_pod_contract != contract.pod_images:
                    raise ValueError(
                        f"current ReplicaSet/{replicaset_name} image contract does not "
                        f"match Deployment/{deployment_name}'s rendered topology"
                    )
            else:
                if self._deletion_requested(
                    metadata, field_name=f"ReplicaSet/{replicaset_name} metadata"
                ):
                    pending.append(f"old ReplicaSet/{replicaset_name} is still terminating")
                if any(
                    count != 0
                    for count in (
                        spec_replicas,
                        status_replicas,
                        ready_replicas,
                        available_replicas,
                    )
                ):
                    pending.append(f"old ReplicaSet/{replicaset_name} is not fully scaled to zero")

        for deployment_name in APP_DEPLOYMENTS:
            if deployment_name not in current_replicaset_uids:
                pending.append(
                    f"Deployment/{deployment_name} has no ReplicaSet for current revision "
                    f"{deployment_revisions.get(deployment_name)!r}"
                )

        deployment_pods: dict[str, list[PodRuntimeIdentity]] = {
            name: [] for name in APP_DEPLOYMENTS
        }
        for pod in namespace_pods:
            metadata = _metadata(pod)
            matching_deployments = {
                name
                for name in APP_DEPLOYMENTS
                if labels_match_selector(
                    metadata.get("labels", {}), self.deployment_contracts[name].selector
                )
            }
            replicaset_uid_claims = {
                uid
                for reference in owner_references(pod)
                if isinstance((uid := reference.get("uid")), str)
                and uid in guarded_replicaset_owners
            }
            if len(replicaset_uid_claims) > 1:
                raise ValueError("pod claims multiple guarded ReplicaSet UIDs")
            if not replicaset_uid_claims:
                if matching_deployments:
                    raise ValueError(
                        "application-selector pod is not controlled through an inventoried "
                        "guarded ReplicaSet UID"
                    )
                continue
            owner_uid = next(iter(replicaset_uid_claims))
            deployment_name = guarded_replicaset_owners[owner_uid]
            replicaset = replicasets_by_uid[owner_uid]
            replicaset_metadata = _metadata(replicaset)
            replicaset_name = replicaset_metadata.get("name")
            inspect_controlling_owner(
                pod,
                namespace=self.config.namespace,
                api_version="apps/v1",
                kind="ReplicaSet",
                name=str(replicaset_name),
                uid=owner_uid,
            )
            if not labels_match_selector(
                metadata.get("labels", {}),
                guarded_replicaset_selectors[owner_uid],
            ):
                raise ValueError(
                    f"Pod/{metadata.get('name')} is hidden from its exact owning "
                    f"ReplicaSet/{replicaset_name} selector"
                )
            pod_spec = _mapping(pod.get("spec"), field_name=f"Pod/{metadata.get('name')} spec")
            if pod_image_contract(pod_spec) != guarded_replicaset_pod_contracts[owner_uid]:
                raise ValueError(
                    f"Pod/{metadata.get('name')} image contract does not match its exact "
                    f"owning ReplicaSet/{replicaset_name} template"
                )
            if deployment_name not in matching_deployments:
                raise ValueError(
                    f"Deployment/{deployment_name}-owned pod is hidden from its exact selector"
                )
            pod_name = str(metadata.get("name"))
            if owner_uid != current_replicaset_uids.get(deployment_name):
                terminating = (
                    " and is terminating"
                    if self._deletion_requested(metadata, field_name=f"Pod/{pod_name} metadata")
                    else ""
                )
                pending.append(
                    f"old ReplicaSet/{replicaset_name} still owns Pod/{pod_name}{terminating}"
                )
                continue
            contract = self.deployment_contracts[deployment_name]
            try:
                identity = self._application_pod_runtime_identity(
                    pod,
                    deployment_name=deployment_name,
                    contract=contract,
                )
            except ApplicationTopologyPendingError as error:
                pending.append(str(error))
            else:
                deployment_pods[deployment_name].append(identity)

        for name in APP_DEPLOYMENTS:
            observed = deployment_pods[name]
            expected = self.deployment_contracts[name].replicas
            if len(observed) != expected:
                pending.append(
                    f"Deployment/{name} has {len(observed)} exactly owned pods, expected {expected}"
                )
            if len({identity.uid for identity in observed}) != len(observed):
                raise ValueError(f"Deployment/{name} pod inventory duplicated a pod UID")
        if pending:
            summary = _bounded_redacted_error(
                "; ".join(pending),
                redactor=self.redactor,
                characters=MAX_FAILURE_CONTEXT_CHARACTERS,
            )
            raise ApplicationTopologyPendingError(summary)
        return {name: self.deployment_contracts[name].replicas for name in APP_DEPLOYMENTS}

    def _verify_deployed_images(self) -> None:
        """Strictly verify one converged application topology for final identity."""

        self.evidence.deployments = self._inspect_application_topology()

    def _wait_for_application_topology(self) -> None:
        """Wait for rollout garbage collection without weakening ownership checks."""

        deadline = time.monotonic() + self.config.rollout_timeout
        last_observation = "no complete application topology observation"
        while True:
            try:
                deployments = self._inspect_application_topology(deadline=deadline)
            except ApplicationTopologyPendingError as error:
                last_observation = _bounded_redacted_error(
                    error,
                    redactor=self.redactor,
                    characters=MAX_FAILURE_CONTEXT_CHARACTERS,
                )
            else:
                self.evidence.deployments = deployments
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    "application topology did not converge within "
                    f"{self.config.rollout_timeout}s (last observation: "
                    f"{last_observation})"
                )
            time.sleep(min(2, remaining))

    def _verify_generic_ray_nodes(self) -> None:
        if self._ray_cluster_uid is None:
            raise ValueError("RayCluster UID was not pinned during convergence")
        probe_code = (
            "import hashlib,importlib.util,json,zipfile;"
            f"p={RUNTIME_ENV_ARCHIVE!r};"
            "data=open(p,'rb').read();"
            "z=zipfile.ZipFile(p);"
            "print(json.dumps({"
            "'django_ray':'absent' if importlib.util.find_spec('django_ray') is None else 'present',"
            "'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),"
            f"'required_member':{RUNTIME_ENV_REQUIRED_MEMBER!r} in z.namelist()"
            "}))"
        )
        observed: set[tuple[int, str]] = set()
        _, ray_pods = self._ray_pods(expected_cluster_uid=self._ray_cluster_uid)
        for pod in ray_pods:
            metadata = _metadata(pod)
            labels = _mapping(metadata.get("labels"), field_name="Ray pod labels")
            name = str(metadata.get("name"))
            component = str(labels.get("component"))
            container = "ray-head" if component == "head" else "ray-worker"
            result = self._kubectl(
                "exec",
                name,
                "-c",
                container,
                "--",
                "python",
                "-c",
                probe_code,
            )
            observed.add(parse_runtime_archive_probe(result.stdout))
        if len(observed) != 1:
            raise ValueError(f"Ray nodes observe inconsistent RuntimeEnv archives: {observed}")
        bundle_bytes, bundle_digest = observed.pop()
        self.evidence.setup_bundle_bytes = bundle_bytes
        self.evidence.setup_bundle_sha256 = bundle_digest

    def _verify_probes(self) -> None:
        deployment = self._json_command(
            self._kubectl("get", "deployment", "django-web", "-o", "json"),
            field_name="Deployment/django-web",
        )
        config_map = self._json_command(
            self._kubectl("get", "configmap", "django-ray-config", "-o", "json"),
            field_name="ConfigMap/django-ray-config",
        )
        host = inspect_probe_contract(deployment, config_map)
        pods_payload = self._json_command(
            self._kubectl("get", "pods", "-l", self._selector(deployment), "-o", "json"),
            field_name="django-web pod list",
        )
        pods = _sequence(pods_payload.get("items"), field_name="django-web pod items")
        restarts = 0
        for value in pods:
            pod = _mapping(value, field_name="django-web pod")
            status = _mapping(pod.get("status"), field_name="django-web pod status")
            conditions = _sequence(status.get("conditions"), field_name="django-web conditions")
            ready = any(
                isinstance(condition, Mapping)
                and condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in conditions
            )
            if not ready:
                raise ValueError("django-web pod is not Ready")
            for container_status in _sequence(
                status.get("containerStatuses", []), field_name="django-web container statuses"
            ):
                entry = _mapping(container_status, field_name="django-web container status")
                restart_count = entry.get("restartCount")
                if not isinstance(restart_count, int):
                    raise ValueError("django-web container restartCount is invalid")
                restarts += restart_count
        self.evidence.web_restart_count = restarts
        status, _ = self._http(
            EXPECTED_PROBE_PATH,
            method="GET",
            headers={"Host": host},
        )
        if status != 200:
            raise ValueError(f"probe HTTP request returned {status}, expected 200")

    def _secret_token(self) -> str:
        if self._api_token is not None:
            return self._api_token
        secret = self._json_command(
            self._kubectl(
                "get",
                "secret",
                "django-ray-secret",
                "-o",
                "json",
                sensitive_output=True,
            ),
            field_name="Secret/django-ray-secret",
        )
        data = _mapping(secret.get("data"), field_name="Secret/django-ray-secret data")
        encoded = data.get("DJANGO_API_TOKEN")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Secret/django-ray-secret has no DJANGO_API_TOKEN")
        try:
            token = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("DJANGO_API_TOKEN is not valid base64-encoded UTF-8") from error
        if not 32 <= len(token) <= 512 or BEARER_TOKEN68_PATTERN.fullmatch(token) is None:
            raise ValueError(
                "DJANGO_API_TOKEN must be 32-512 characters using the Bearer token68 alphabet "
                "with at most two trailing '=' padding characters"
            )
        self.redactor.register(token)
        self.redactor.register(encoded)
        self.runner.redactor.register(token)
        self.runner.redactor.register(encoded)
        self._api_token = token
        return token

    def _http(
        self,
        path: str,
        *,
        method: str,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        url = build_local_http_request_url(base_url=self.config.web_url, path=path)
        request = Request(url, method=method, headers=dict(headers or {}))
        try:
            with self.http_opener.open(request, timeout=10) as response:
                return response.status, response.read(MAX_OUTPUT_CHARACTERS)
        except HTTPError as error:
            return error.code, error.read(MAX_OUTPUT_CHARACTERS)
        except URLError as error:
            raise ValueError(f"local HTTP request failed: {error.reason}") from error

    def _json_body(self, body: bytes, *, endpoint: str) -> Mapping[str, Any]:
        try:
            return _mapping(json.loads(body), field_name=f"{endpoint} response")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"{endpoint} did not return valid JSON") from error

    def _verify_api(self) -> None:
        unauthenticated = (
            ("/api/enqueue/add/2/3", "POST"),
            ("/api/executions/stats", "GET"),
            ("/api/metrics", "GET"),
            ("/api/executions?limit=1", "GET"),
        )
        for endpoint, method in unauthenticated:
            status, _ = self._http(endpoint, method=method)
            if status != 401:
                raise ValueError(f"unauthenticated {endpoint} returned {status}, expected 401")

        token = self._secret_token()
        headers = {"Authorization": f"Bearer {token}"}
        for endpoint in ("/api/executions/stats", "/api/metrics", "/api/executions?limit=1"):
            status, _ = self._http(endpoint, method="GET", headers=headers)
            if status != 200:
                raise ValueError(f"authenticated {endpoint} returned {status}, expected 200")

        status, body = self._http(
            "/api/enqueue/add/2/3",
            method="POST",
            headers=headers,
        )
        if status != 200:
            raise ValueError(f"authenticated add_numbers enqueue returned {status}, expected 200")
        enqueue = self._json_body(body, endpoint="add_numbers enqueue")
        task_id = enqueue.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("add_numbers enqueue response has no task_id")
        try:
            parsed_task_id = UUID(task_id)
        except ValueError as error:
            raise ValueError("add_numbers enqueue task_id is not a canonical UUID") from error
        if parsed_task_id.version != 4 or str(parsed_task_id) != task_id:
            raise ValueError("add_numbers enqueue task_id is not a canonical UUIDv4")
        task_id = str(parsed_task_id)
        self.evidence.task_id = task_id

        deadline = time.monotonic() + self.config.task_timeout
        last_state = "missing"
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        while True:
            status, body = self._http(
                f"/api/executions?{execution_query}", method="GET", headers=headers
            )
            if status != 200:
                raise ValueError(f"execution polling returned {status}, expected 200")
            listing = self._json_body(body, endpoint="execution polling")
            tasks = _sequence(listing.get("tasks"), field_name="execution polling tasks")
            execution = next(
                (
                    _mapping(value, field_name="execution")
                    for value in tasks
                    if isinstance(value, Mapping) and value.get("task_id") == task_id
                ),
                None,
            )
            if execution is not None:
                last_state = str(execution.get("state"))
                if last_state == "SUCCEEDED":
                    result = parse_task_result(execution.get("result_data"))
                    if result != 5:
                        raise ValueError(f"add_numbers durable result is {result!r}, expected 5")
                    self.evidence.task_state = last_state
                    self.evidence.task_result = result
                    return
                if last_state in {"FAILED", "CANCELLED", "LOST"}:
                    raise ValueError(f"add_numbers reached terminal state {last_state}")
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"add_numbers did not reach SUCCEEDED within {self.config.task_timeout}s "
                    f"(last state: {last_state})"
                )
            time.sleep(2)

    def _workflow_envelope_contract(
        self,
        payload: Mapping[str, Any],
        *,
        task_id: str,
        endpoint: str,
        schema: str,
        expected_run_identity: Mapping[str, Any] | None = None,
        expected_publication: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Validate common bounded-reader identity without retaining response payloads."""

        if payload.get("schema") != schema or payload.get("schema_version") != 1:
            raise ValueError(f"{endpoint} returned an unsupported read envelope")
        if payload.get("task_id") != task_id:
            raise ValueError(f"{endpoint} returned the wrong workflow task")
        if payload.get("availability") != "AVAILABLE" or payload.get("complete") is not True:
            raise ValueError(f"{endpoint} did not return complete AVAILABLE workflow progress")

        run_identity = _mapping(
            payload.get("run_identity"),
            field_name=f"{endpoint} run_identity",
        )
        if set(run_identity) != {
            "schema_version",
            "run_id",
            "attempt_number",
            "execution_generation",
        }:
            raise ValueError(f"{endpoint} returned an invalid public run identity")
        run_id = run_identity.get("run_id")
        try:
            parsed_run_id = UUID(cast(str, run_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"{endpoint} returned an invalid workflow run UUID") from error
        if (
            run_identity.get("schema_version") != 1
            or str(parsed_run_id) != run_id
            or type(run_identity.get("attempt_number")) is not int
            or cast(int, run_identity["attempt_number"]) < 1
            or type(run_identity.get("execution_generation")) is not int
            or cast(int, run_identity["execution_generation"]) < 0
        ):
            raise ValueError(f"{endpoint} returned an invalid public run identity")
        if expected_run_identity is not None and run_identity != expected_run_identity:
            raise ValueError(f"{endpoint} returned a different workflow run")

        publication = _mapping(
            payload.get("publication"),
            field_name=f"{endpoint} publication",
        )
        if set(publication) != {
            "summary_revision",
            "topology_version",
            "detail_revision",
        } or any(
            type(publication.get(field_name)) is not int or cast(int, publication[field_name]) < 1
            for field_name in (
                "summary_revision",
                "topology_version",
                "detail_revision",
            )
        ):
            raise ValueError(f"{endpoint} returned invalid publication revisions")
        if expected_publication is not None and publication != expected_publication:
            raise ValueError(f"{endpoint} returned a different workflow publication")
        return run_identity, publication

    def _workflow_page(
        self,
        *,
        task_id: str,
        collection: str,
        headers: Mapping[str, str],
        run_identity: Mapping[str, Any],
        publication: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        """Read one deliberately small complete page from a bounded workflow collection."""

        suffix = WORKFLOW_PROGRESS_COLLECTION_PATHS[collection]
        query = urlencode({"limit": WORKFLOW_PROGRESS_PAGE_LIMIT})
        endpoint = f"/api/cluster/workflows/{task_id}/{suffix}?{query}"
        status, body = self._http(endpoint, method="GET", headers=headers)
        if status != 200:
            raise ValueError(f"{collection} workflow read returned a non-success status")
        page = self._json_body(body, endpoint=collection)
        self._workflow_envelope_contract(
            page,
            task_id=task_id,
            endpoint=collection,
            schema="django-ray.workflow-progress-page",
            expected_run_identity=run_identity,
            expected_publication=publication,
        )
        if page.get("collection") != collection:
            raise ValueError(f"{collection} workflow read returned the wrong collection")
        items = _sequence(page.get("items"), field_name=f"{collection} items")
        if (
            not items
            or type(page.get("returned_count")) is not int
            or page["returned_count"] != len(items)
            or len(items) > WORKFLOW_PROGRESS_PAGE_LIMIT
            or page.get("next_cursor") is not None
        ):
            raise ValueError(f"{collection} workflow read was empty, inconsistent, or incomplete")
        return [
            _mapping(item, field_name=f"{collection} item {index}")
            for index, item in enumerate(items)
        ]

    @staticmethod
    def _workflow_node_ids(
        items: Sequence[Mapping[str, Any]],
        *,
        collection: str,
    ) -> set[str]:
        """Return unique bounded node identities from one verified collection."""

        node_ids: set[str] = set()
        for item in items:
            node_id = item.get("node_id")
            if (
                not isinstance(node_id, str)
                or not node_id
                or len(node_id.encode("utf-8")) > 256
                or node_id in node_ids
            ):
                raise ValueError(f"{collection} contains an invalid or duplicate node identity")
            node_ids.add(node_id)
        return node_ids

    @staticmethod
    def _workflow_summary_count(
        counts: Mapping[str, Any],
        field_name: str,
    ) -> int:
        """Read one non-negative durable counter from the schema-v3 summary."""

        value = counts.get(field_name)
        if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
            raise ValueError("workflow summary contains an invalid durable count")
        return value

    def _verify_complex_workflow_run(
        self,
        *,
        enqueue_path: str,
        expected_enqueue_kwargs: Mapping[str, object],
        expected_state: str,
        expected_error: str | None = None,
    ) -> WorkflowGateObservation:
        """Verify one terminal nested workflow through every bounded API reader."""

        token = self._secret_token()
        headers = {"Authorization": f"Bearer {token}"}
        status, body = self._http(enqueue_path, method="POST", headers=headers)
        if status != 200:
            raise ValueError("complex workflow enqueue returned a non-success status")
        enqueue = self._json_body(body, endpoint="complex workflow enqueue")
        enqueue_kwargs = _mapping(
            enqueue.get("kwargs"),
            field_name="complex workflow enqueue kwargs",
        )
        if (
            enqueue.get("args") != []
            or set(enqueue_kwargs) != set(expected_enqueue_kwargs)
            or any(
                type(enqueue_kwargs.get(field_name)) is not type(expected_value)
                or enqueue_kwargs.get(field_name) != expected_value
                for field_name, expected_value in expected_enqueue_kwargs.items()
            )
        ):
            raise ValueError("complex workflow enqueue did not retain the exact requested inputs")
        fast_items = enqueue_kwargs.get("fast_items")
        slow_items = enqueue_kwargs.get("slow_items")
        if type(fast_items) is not int or type(slow_items) is not int:
            raise ValueError("complex workflow enqueue returned invalid branch item counts")
        expected_leaf_tasks = cast(int, fast_items) + cast(int, slow_items)
        if not 2 <= expected_leaf_tasks <= 200:
            raise ValueError("complex workflow enqueue returned invalid total leaf work")
        task_id = enqueue.get("task_id")
        try:
            parsed_task_id = UUID(cast(str, task_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("complex workflow enqueue task_id is not a canonical UUID") from error
        if (
            parsed_task_id.version != 4
            or str(parsed_task_id) != task_id
            or not isinstance(task_id, str)
        ):
            raise ValueError("complex workflow enqueue task_id is not a canonical UUIDv4")
        task_id = str(parsed_task_id)

        deadline = time.monotonic() + self.config.task_timeout
        last_state = "missing"
        terminal_states = WORKFLOW_PROGRESS_FAILURE_STATES | {"SUCCEEDED"}
        while True:
            status, body = self._http(
                f"/api/cluster/complex-workflow/{task_id}",
                method="GET",
                headers=headers,
            )
            if status != 200:
                raise ValueError("complex workflow polling returned a non-success status")
            execution = self._json_body(body, endpoint="complex workflow polling")
            state = execution.get("state")
            if not isinstance(state, str) or state not in WORKFLOW_PROGRESS_TASK_STATES:
                raise ValueError("complex workflow polling returned an invalid task state")
            last_state = state
            if state in terminal_states:
                if state != expected_state:
                    raise ValueError("complex workflow reached an unexpected terminal state")
                if state == "SUCCEEDED":
                    result = _mapping(
                        execution.get("result"),
                        field_name="complex workflow result",
                    )
                    if (
                        result.get("shape") != "chain(group(chain(map), chain(map)), step)"
                        or result.get("durability_boundary") != "single RayTaskExecution"
                        or result.get("total_leaf_tasks") != expected_leaf_tasks
                        or execution.get("error") is not None
                    ):
                        raise ValueError(
                            "complex workflow result did not match the tiny nested workload"
                        )
                elif (
                    execution.get("result") is not None or execution.get("error") != expected_error
                ):
                    raise ValueError(
                        "failed complex workflow did not retain its normalized fixture error"
                    )
                break
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"complex workflow did not reach {expected_state} within "
                    f"{self.config.task_timeout}s (last state: {last_state})"
                )
            time.sleep(2)

        execution_query = urlencode({"task_id": task_id, "limit": 1})
        status, body = self._http(
            f"/api/executions?{execution_query}",
            method="GET",
            headers=headers,
        )
        if status != 200:
            raise ValueError("complex workflow execution read returned a non-success status")
        execution_list = self._json_body(body, endpoint="complex workflow execution")
        task_records = _sequence(
            execution_list.get("tasks"),
            field_name="complex workflow execution tasks",
        )
        if len(task_records) != 1:
            raise ValueError("complex workflow execution read did not return exactly one row")
        task_record = _mapping(task_records[0], field_name="complex workflow execution row")
        if (
            task_record.get("task_id") != task_id
            or task_record.get("state") != expected_state
            or task_record.get("callable_path")
            != "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"
            or task_record.get("attempt_number") != 1
        ):
            raise ValueError("complex workflow did not remain on its first durable attempt")

        summary_endpoint = f"/api/cluster/workflows/{task_id}"
        status, body = self._http(summary_endpoint, method="GET", headers=headers)
        if status != 200:
            raise ValueError("workflow summary read returned a non-success status")
        summary_envelope = self._json_body(body, endpoint="workflow summary")
        run_identity, publication = self._workflow_envelope_contract(
            summary_envelope,
            task_id=task_id,
            endpoint="workflow summary",
            schema="django-ray.workflow-progress-summary",
        )
        summary = _mapping(
            summary_envelope.get("summary"),
            field_name="workflow summary payload",
        )
        if (
            summary_envelope.get("source_schema_version") != WORKFLOW_PROGRESS_SCHEMA_VERSION
            or summary.get("schema_version") != WORKFLOW_PROGRESS_SCHEMA_VERSION
            or summary.get("run_identity") != run_identity
            or run_identity.get("attempt_number") != 1
            or summary.get("state") != expected_state
            or summary.get("reporting_policy") != "full"
            or summary.get("selected_strategy") != "dynamic_tasks"
            or summary.get("summary_revision") != publication["summary_revision"]
            or summary.get("topology_version") != publication["topology_version"]
            or summary.get("detail_revision") != publication["detail_revision"]
        ):
            raise ValueError("workflow summary did not report the expected terminal schema-v3 run")
        if task_record.get("execution_generation") != run_identity.get(
            "execution_generation"
        ) or str(task_record.get("workflow_run_id")) != run_identity.get("run_id"):
            raise ValueError("workflow summary identity did not match the execution row")
        fingerprint = summary.get("plan_fingerprint")
        if (
            not isinstance(fingerprint, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
        ):
            raise ValueError("workflow summary did not retain a canonical plan fingerprint")
        detail = _mapping(summary.get("detail"), field_name="workflow summary detail")
        if detail.get("availability") != "AVAILABLE" or detail.get("complete") is not True:
            raise ValueError("workflow summary detail was not complete and AVAILABLE")

        topology_nodes = self._workflow_page(
            task_id=task_id,
            collection="topology_nodes",
            headers=headers,
            run_identity=run_identity,
            publication=publication,
        )
        topology_edges = self._workflow_page(
            task_id=task_id,
            collection="topology_edges",
            headers=headers,
            run_identity=run_identity,
            publication=publication,
        )
        node_details = self._workflow_page(
            task_id=task_id,
            collection="node_details",
            headers=headers,
            run_identity=run_identity,
            publication=publication,
        )

        topology_node_ids = self._workflow_node_ids(
            topology_nodes,
            collection="topology_nodes",
        )
        detail_node_ids = self._workflow_node_ids(
            node_details,
            collection="node_details",
        )
        if detail_node_ids != topology_node_ids:
            raise ValueError("workflow node detail did not match the retained topology")
        edges: set[tuple[str, str]] = set()
        for edge in topology_edges:
            source = edge.get("source")
            target = edge.get("target")
            if (
                not isinstance(source, str)
                or not isinstance(target, str)
                or source not in topology_node_ids
                or target not in topology_node_ids
                or source == target
                or (source, target) in edges
            ):
                raise ValueError("workflow topology contains an invalid dependency edge")
            edges.add((source, target))
        if not edges:
            raise ValueError("workflow topology did not contain dependency edges")
        states = [detail_item.get("state") for detail_item in node_details]
        if any(state not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"} for state in states):
            raise ValueError("terminal workflow detail contains an invalid node state")
        pending_nodes = states.count("PENDING")
        running_nodes = states.count("RUNNING")
        succeeded_nodes = states.count("SUCCEEDED")
        failed_nodes = states.count("FAILED")
        if expected_state == "SUCCEEDED":
            if (
                pending_nodes != 0
                or running_nodes != 0
                or succeeded_nodes != len(states)
                or failed_nodes != 0
            ):
                raise ValueError("successful workflow detail was not fully succeeded")
        elif failed_nodes < 1:
            raise ValueError("failed workflow detail did not retain a failed node")

        node_counts = _mapping(
            summary.get("node_counts"),
            field_name="workflow summary node_counts",
        )
        edge_counts = _mapping(
            summary.get("edge_counts"),
            field_name="workflow summary edge_counts",
        )
        expected_node_count = len(topology_node_ids)
        expected_edge_count = len(edges)
        expected_node_counts = {
            "discovered": expected_node_count,
            "retained_topology": expected_node_count,
            "retained_detail": len(detail_node_ids),
            "pending": pending_nodes,
            "running": running_nodes,
            "succeeded": succeeded_nodes,
            "failed": failed_nodes,
        }
        if any(
            self._workflow_summary_count(node_counts, field_name) != expected
            for field_name, expected in expected_node_counts.items()
        ):
            raise ValueError("workflow summary node counts did not match bounded reader evidence")
        declared_nodes = node_counts.get("declared")
        if declared_nodes is not None and (
            type(declared_nodes) is not int or declared_nodes != expected_node_count
        ):
            raise ValueError(
                "workflow summary declared nodes did not match bounded reader evidence"
            )
        if any(
            self._workflow_summary_count(edge_counts, field_name) != expected_edge_count
            for field_name in ("discovered", "retained_topology")
        ):
            raise ValueError("workflow summary edge counts did not match bounded reader evidence")
        declared_edges = edge_counts.get("declared")
        if declared_edges is not None and (
            type(declared_edges) is not int or declared_edges != expected_edge_count
        ):
            raise ValueError(
                "workflow summary declared edges did not match bounded reader evidence"
            )

        return WorkflowGateObservation(
            task_id=task_id,
            state=expected_state,
            attempt_number=1,
            schema_version=WORKFLOW_PROGRESS_SCHEMA_VERSION,
            availability="AVAILABLE",
            topology_nodes=expected_node_count,
            topology_edges=expected_edge_count,
            node_details=len(detail_node_ids),
            leaf_tasks=expected_leaf_tasks,
            pending_nodes=pending_nodes,
            running_nodes=running_nodes,
            succeeded_nodes=succeeded_nodes,
            failed_nodes=failed_nodes,
        )

    def _verify_complex_workflow_progress(self) -> None:
        """Prove successful and deterministic failed nested workflows end to end."""

        succeeded = self._verify_complex_workflow_run(
            enqueue_path=COMPLEX_WORKFLOW_ENQUEUE_PATH,
            expected_enqueue_kwargs=COMPLEX_WORKFLOW_ENQUEUE_KWARGS,
            expected_state="SUCCEEDED",
        )
        failed = self._verify_complex_workflow_run(
            enqueue_path=COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH,
            expected_enqueue_kwargs=COMPLEX_WORKFLOW_FAILURE_ENQUEUE_KWARGS,
            expected_state="FAILED",
            expected_error=COMPLEX_WORKFLOW_FAILURE_MESSAGE,
        )

        self.evidence.workflow_task_id = succeeded.task_id
        self.evidence.workflow_task_state = succeeded.state
        self.evidence.workflow_attempt_number = succeeded.attempt_number
        self.evidence.workflow_schema_version = succeeded.schema_version
        self.evidence.workflow_availability = succeeded.availability
        self.evidence.workflow_topology_nodes = succeeded.topology_nodes
        self.evidence.workflow_topology_edges = succeeded.topology_edges
        self.evidence.workflow_node_details = succeeded.node_details
        self.evidence.workflow_leaf_tasks = succeeded.leaf_tasks
        self.evidence.workflow_failure_task_id = failed.task_id
        self.evidence.workflow_failure_task_state = failed.state
        self.evidence.workflow_failure_attempt_number = failed.attempt_number
        self.evidence.workflow_failure_schema_version = failed.schema_version
        self.evidence.workflow_failure_availability = failed.availability
        self.evidence.workflow_failure_topology_nodes = failed.topology_nodes
        self.evidence.workflow_failure_topology_edges = failed.topology_edges
        self.evidence.workflow_failure_node_details = failed.node_details
        self.evidence.workflow_failure_leaf_tasks = failed.leaf_tasks
        self.evidence.workflow_failure_pending_nodes = failed.pending_nodes
        self.evidence.workflow_failure_running_nodes = failed.running_nodes
        self.evidence.workflow_failure_succeeded_nodes = failed.succeeded_nodes
        self.evidence.workflow_failure_failed_nodes = failed.failed_nodes

    def _verify_workflow_admin(self) -> None:
        """Exercise both terminal workflows through authenticated admin readers."""

        expected_fields = {
            "admin_workflow",
            "task_id",
            "task_state",
            "attempt_number",
            "admin_routes",
            "admin_actions",
            "topology_nodes",
            "topology_edges",
            "node_details",
            "graph_status",
            "graph_nodes",
            "graph_edges",
            "graph_pending_nodes",
            "graph_running_nodes",
            "graph_succeeded_nodes",
            "graph_failed_nodes",
            "graph_failure_path_nodes",
            "graph_failure_origins",
            "graph_incoming_failure_edges",
            "current_manifests",
            "pending_manifests",
            "unlinked_pages",
        }
        runs = (
            (
                "successful",
                self.evidence.workflow_task_id,
                self.evidence.workflow_task_state,
                self.evidence.workflow_attempt_number,
                {
                    "topology_nodes": self.evidence.workflow_topology_nodes,
                    "topology_edges": self.evidence.workflow_topology_edges,
                    "node_details": self.evidence.workflow_node_details,
                    "graph_nodes": self.evidence.workflow_topology_nodes,
                    "graph_edges": self.evidence.workflow_topology_edges,
                    "graph_pending_nodes": 0,
                    "graph_running_nodes": 0,
                    "graph_succeeded_nodes": self.evidence.workflow_node_details,
                    "graph_failed_nodes": 0,
                    "graph_failure_path_nodes": 0,
                    "graph_failure_origins": 0,
                    "graph_incoming_failure_edges": 0,
                },
            ),
            (
                "failed",
                self.evidence.workflow_failure_task_id,
                self.evidence.workflow_failure_task_state,
                self.evidence.workflow_failure_attempt_number,
                {
                    "topology_nodes": self.evidence.workflow_failure_topology_nodes,
                    "topology_edges": self.evidence.workflow_failure_topology_edges,
                    "node_details": self.evidence.workflow_failure_node_details,
                    "graph_nodes": self.evidence.workflow_failure_topology_nodes,
                    "graph_edges": self.evidence.workflow_failure_topology_edges,
                    "graph_pending_nodes": self.evidence.workflow_failure_pending_nodes,
                    "graph_running_nodes": self.evidence.workflow_failure_running_nodes,
                    "graph_succeeded_nodes": (self.evidence.workflow_failure_succeeded_nodes),
                    "graph_failed_nodes": self.evidence.workflow_failure_failed_nodes,
                },
            ),
        )
        verified: dict[str, Mapping[str, Any]] = {}
        for label, task_id, task_state, attempt_number, expected_counts in runs:
            if (
                not task_id
                or task_state not in {"SUCCEEDED", "FAILED"}
                or attempt_number != 1
                or any(type(value) is not int or value < 0 for value in expected_counts.values())
                or any(
                    expected_counts[field_name] < 1
                    for field_name in (
                        "topology_nodes",
                        "topology_edges",
                        "node_details",
                    )
                )
            ):
                raise ValueError("workflow API evidence was not ready for admin verification")

            result = self._kubectl(
                "exec",
                "deployment/django-web",
                "-c",
                "django-web",
                "--",
                "python",
                "-m",
                "testproject.docker_smoke",
                "--base-url",
                WORKFLOW_ADMIN_LOOPBACK_URL,
                "--timeout",
                str(self.config.task_timeout),
                "--existing-workflow-task-id",
                task_id,
                timeout=(self.config.task_timeout + self.config.kubectl_request_timeout + 5),
                sensitive_output=True,
            )
            payload = self._json_command(
                result,
                field_name=f"{label} workflow admin smoke response",
            )
            if set(payload) != expected_fields or any(
                type(payload.get(field_name)) is not int
                for field_name in expected_fields
                - {"admin_workflow", "task_id", "task_state", "graph_status"}
            ):
                raise ValueError("existing workflow admin smoke returned non-scalar evidence")
            if (
                payload.get("admin_workflow") != "verified"
                or payload.get("task_id") != task_id
                or payload.get("task_state") != task_state
                or payload.get("attempt_number") != attempt_number
                or payload.get("graph_status") != "AVAILABLE"
                or payload.get("admin_routes") != 6
                or payload.get("admin_actions") != 3
                or any(
                    payload.get(field_name) != value
                    for field_name, value in expected_counts.items()
                )
                or payload.get("current_manifests") != 1
                or payload.get("pending_manifests") != 0
                or payload.get("unlinked_pages") != 0
            ):
                raise ValueError(
                    "existing workflow admin smoke did not match API and storage evidence"
                )
            if label == "failed" and (
                payload.get("graph_failure_path_nodes", 0) < 1
                or payload.get("graph_failure_origins") != 1
                or payload.get("graph_incoming_failure_edges", 0) < 1
            ):
                raise ValueError("failed workflow admin graph lacked one incoming failed path")
            verified[label] = payload

        successful = verified["successful"]
        failed = verified["failed"]
        self.evidence.workflow_admin_routes = cast(int, successful["admin_routes"])
        self.evidence.workflow_admin_actions = cast(int, successful["admin_actions"])
        self.evidence.workflow_current_manifests = cast(int, successful["current_manifests"])
        self.evidence.workflow_pending_manifests = cast(int, successful["pending_manifests"])
        self.evidence.workflow_unlinked_pages = cast(int, successful["unlinked_pages"])
        self.evidence.workflow_failure_path_nodes = cast(int, failed["graph_failure_path_nodes"])
        self.evidence.workflow_failure_origins = cast(int, failed["graph_failure_origins"])
        self.evidence.workflow_failure_incoming_edges = cast(
            int, failed["graph_incoming_failure_edges"]
        )
        self.evidence.workflow_failure_admin_routes = cast(int, failed["admin_routes"])
        self.evidence.workflow_failure_admin_actions = cast(int, failed["admin_actions"])
        self.evidence.workflow_failure_current_manifests = cast(int, failed["current_manifests"])
        self.evidence.workflow_failure_pending_manifests = cast(int, failed["pending_manifests"])
        self.evidence.workflow_failure_unlinked_pages = cast(int, failed["unlinked_pages"])

    def _verify_ray_identity(self) -> None:
        """Re-pin the exact RayCluster UID, topology, and complete owned pod inventory."""

        if self._ray_cluster_uid is None:
            raise ValueError("RayCluster UID was not pinned during convergence")
        if self._ray_pod_identities is None:
            raise ValueError("converged Ray pod identities were not pinned during convergence")
        cluster_uid, pods = self._ray_pods(expected_cluster_uid=self._ray_cluster_uid)
        observed = self._ray_distribution(pods)
        expected = self._expected_ray_distribution()
        if observed != expected:
            raise ValueError(f"final Ray topology is {observed}, expected exactly {expected}")
        current_identities = self._ray_runtime_identities(pods)
        if current_identities != self._ray_pod_identities:
            raise ValueError("Ray pod UID/container/image identity changed after convergence")
        digest = pod_identity_sha256(tuple(current_identities))
        if digest != self.evidence.ray_pod_identity_sha256:
            raise ValueError("Ray pod identity evidence digest changed after convergence")
        self.evidence.ray_cluster_uid = cluster_uid

    def _verify_final_identity(self) -> None:
        """Verify immutable source and routing immediately before evidence is emitted."""

        self._verify_source_identity()
        self._verify_kubeconfig_snapshot()
        self._verify_ray_identity()
        self._verify_deployed_images()

    def _verify_prometheus(self) -> None:
        self._verify_ray_identity()
        expected_counts = {
            "django-ray": 1,
            "ray-head": self.expected_ray_head_count,
            "ray-workers": self.expected_ray_worker_count,
        }
        self.evidence.prometheus_counts = wait_for_healthy_targets(
            lambda: fetch_active_targets(
                self.config.prometheus_url,
                request_timeout=min(10, self.config.command_timeout),
                opener=self.http_opener,
            ),
            timeout=self.config.prometheus_timeout,
            interval=2,
            expected_counts=expected_counts,
        )
        self._verify_ray_identity()

    def _evidence_field_lines(self, key: str, value: object) -> tuple[str, ...]:
        """Render one redacted, reconstructable field within commit line limits."""

        if re.fullmatch(r"[a-z][a-z0-9_]*", key) is None:
            raise ValueError("evidence keys must use lowercase snake_case")
        serialized = self.redactor.clean(value)
        if any(character in serialized for character in "\r\n"):
            serialized = json.dumps(serialized, ensure_ascii=True)[1:-1]
        direct = f"{key}={serialized}"
        if len(direct) <= EVIDENCE_LINE_LIMIT:
            return (direct,)
        part_prefix_length = len(f"{key}_part_000=")
        chunk_size = EVIDENCE_LINE_LIMIT - part_prefix_length
        if chunk_size < 1:
            raise ValueError("evidence key is too long for a structured commit line")
        chunks = tuple(
            serialized[offset : offset + chunk_size]
            for offset in range(0, len(serialized), chunk_size)
        )
        if len(chunks) > 999:
            raise ValueError("evidence value needs too many bounded line parts")
        return (
            f"{key}_parts={len(chunks):03d}",
            *(f"{key}_part_{index:03d}={chunk}" for index, chunk in enumerate(chunks, start=1)),
        )

    def _evidence_lines(self) -> tuple[str, ...]:
        """Verify final identity and prepare evidence without emitting it."""

        self._verify_final_identity()
        fields: list[tuple[str, object]] = [
            ("source_commit_at_run", self.evidence.commit),
            ("source_tree", self.evidence.source_tree),
            ("context", self.config.context),
            ("namespace", self.config.namespace),
            ("kubeconfig_sha256", self.evidence.kubeconfig_sha256),
            ("kubernetes_server", self.evidence.kubernetes_server),
            ("docker_host", self.evidence.docker_host),
            ("app_image_tag", self.evidence.app_tag),
            ("app_image_id", self.evidence.app_image_id),
            ("legacy_worker_image_tag", self.evidence.worker_tag),
            ("legacy_worker_image_id", self.evidence.worker_image_id),
            ("legacy_worker_built", "true"),
            ("kuberay_uses_generic_ray", "true"),
            ("setup", "passed"),
            ("runtime_env_bytes", self.evidence.setup_bundle_bytes),
            ("runtime_env_sha256", self.evidence.setup_bundle_sha256),
            ("ray_restart", self.evidence.ray_restart),
            ("ray_cluster_uid", self.evidence.ray_cluster_uid),
            ("ray_heads", self.evidence.ray_head_count),
            ("ray_workers", self.evidence.ray_worker_count),
            ("ray_pods_sha256", self.evidence.ray_pod_identity_sha256),
            ("generic_django_ray", "absent"),
            ("api_unauthenticated", 401),
            ("api_authenticated", 200),
            ("task_id", self.evidence.task_id),
            ("task_state", self.evidence.task_state),
            ("task_result", self.evidence.task_result),
            ("workflow_task_id", self.evidence.workflow_task_id),
            ("workflow_task_state", self.evidence.workflow_task_state),
            ("workflow_attempt_number", self.evidence.workflow_attempt_number),
            ("workflow_schema_version", self.evidence.workflow_schema_version),
            ("workflow_availability", self.evidence.workflow_availability),
            ("workflow_topology_nodes", self.evidence.workflow_topology_nodes),
            ("workflow_topology_edges", self.evidence.workflow_topology_edges),
            ("workflow_node_details", self.evidence.workflow_node_details),
            ("workflow_leaf_tasks", self.evidence.workflow_leaf_tasks),
            ("workflow_admin_routes", self.evidence.workflow_admin_routes),
            ("workflow_admin_actions", self.evidence.workflow_admin_actions),
            (
                "workflow_current_manifests",
                self.evidence.workflow_current_manifests,
            ),
            (
                "workflow_pending_manifests",
                self.evidence.workflow_pending_manifests,
            ),
            ("workflow_unlinked_pages", self.evidence.workflow_unlinked_pages),
            ("workflow_failure_task_id", self.evidence.workflow_failure_task_id),
            (
                "workflow_failure_task_state",
                self.evidence.workflow_failure_task_state,
            ),
            (
                "workflow_failure_attempt_number",
                self.evidence.workflow_failure_attempt_number,
            ),
            (
                "workflow_failure_schema_version",
                self.evidence.workflow_failure_schema_version,
            ),
            (
                "workflow_failure_availability",
                self.evidence.workflow_failure_availability,
            ),
            (
                "workflow_failure_topology_nodes",
                self.evidence.workflow_failure_topology_nodes,
            ),
            (
                "workflow_failure_topology_edges",
                self.evidence.workflow_failure_topology_edges,
            ),
            (
                "workflow_failure_node_details",
                self.evidence.workflow_failure_node_details,
            ),
            (
                "workflow_failure_leaf_tasks",
                self.evidence.workflow_failure_leaf_tasks,
            ),
            (
                "workflow_failure_pending_nodes",
                self.evidence.workflow_failure_pending_nodes,
            ),
            (
                "workflow_failure_running_nodes",
                self.evidence.workflow_failure_running_nodes,
            ),
            (
                "workflow_failure_succeeded_nodes",
                self.evidence.workflow_failure_succeeded_nodes,
            ),
            (
                "workflow_failure_failed_nodes",
                self.evidence.workflow_failure_failed_nodes,
            ),
            (
                "workflow_failure_path_nodes",
                self.evidence.workflow_failure_path_nodes,
            ),
            (
                "workflow_failure_origins",
                self.evidence.workflow_failure_origins,
            ),
            (
                "workflow_failure_incoming_edges",
                self.evidence.workflow_failure_incoming_edges,
            ),
            (
                "workflow_failure_admin_routes",
                self.evidence.workflow_failure_admin_routes,
            ),
            (
                "workflow_failure_admin_actions",
                self.evidence.workflow_failure_admin_actions,
            ),
            (
                "workflow_failure_current_manifests",
                self.evidence.workflow_failure_current_manifests,
            ),
            (
                "workflow_failure_pending_manifests",
                self.evidence.workflow_failure_pending_manifests,
            ),
            (
                "workflow_failure_unlinked_pages",
                self.evidence.workflow_failure_unlinked_pages,
            ),
            ("probe_path", EXPECTED_PROBE_PATH),
            ("probe_host", EXPECTED_PROBE_HOST),
            ("web_restarts", self.evidence.web_restart_count),
            ("prometheus_removed_worker_pool", "absent"),
            ("preserved_postgresql", "true"),
            ("preserved_pvcs", "true"),
            ("preserved_unrelated_namespaces", "true"),
            ("preserved_unrelated_docker_data", "true"),
        ]
        fields.extend(
            (f"deployment_{name.replace('-', '_')}_ready", self.evidence.deployments[name])
            for name in APP_DEPLOYMENTS
        )
        fields.extend(
            (
                f"prometheus_{job.replace('-', '_')}",
                self.evidence.prometheus_counts[job],
            )
            for job in EXPECTED_JOBS
        )
        lines = ["=== Local KubeRay final gate evidence ==="]
        for key, value in fields:
            lines.extend(self._evidence_field_lines(key, value))
        return tuple(lines)

    def _emit_evidence(self) -> None:
        """Emit a freshly verified evidence block for focused callers and tests."""

        for line in self._evidence_lines():
            self._emit(line)

    def diagnostics(self, layer: str) -> None:
        """Print bounded, layer-relevant diagnostics without changing cluster state."""
        if not self.mutated:
            return
        self._emit(f"--- bounded diagnostics for {layer} ---")
        commands: list[tuple[str, ...]] = [
            ("get", "pods,deployments,jobs,pvc", "-o", "wide"),
        ]
        if layer == "setup":
            commands.append(("logs", f"job/{SETUP_JOB}", "--tail=60"))
        if layer in {"workloads", "ray", "runtime-env", "rollouts"}:
            commands.append(
                (
                    "logs",
                    "-l",
                    f"{RAY_CLUSTER_LABEL}={RAY_CLUSTER_NAME}",
                    "--all-containers=true",
                    "--tail=20",
                    "--prefix=true",
                )
            )
        if layer in {
            "rollouts",
            "app-convergence",
            "image-identity",
            "probes",
            "api-smoke",
            "workflow-progress",
            "workflow-admin",
        }:
            commands.extend(
                [
                    ("logs", "deployment/django-web", "--all-containers=true", "--tail=40"),
                    (
                        "logs",
                        "-l",
                        "app=django-ray,component=worker",
                        "--all-containers=true",
                        "--tail=20",
                        "--prefix=true",
                    ),
                ]
            )
        if layer == "prometheus":
            commands.append(("logs", "deployment/prometheus", "--all-containers=true", "--tail=40"))
        for command in commands:
            result = self._kubectl(
                *command,
                check=False,
                timeout=min(self.config.command_timeout, 30),
            )
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            bounded = _bounded_text(
                self.redactor.clean(output),
                lines=MAX_DIAGNOSTIC_LINES,
            )
            if bounded:
                self._emit(bounded)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the guarded local Docker Desktop/Kind KubeRay final integration gate"
    )
    parser.add_argument(
        "--context",
        required=True,
        help="Expected active local context: docker-desktop or kind-<name>",
    )
    parser.add_argument(
        "--namespace",
        required=True,
        help=f"Explicit local namespace; must be {EXPECTED_NAMESPACE}",
    )
    parser.add_argument(
        "--ray-restart",
        required=True,
        choices=("required", "skip"),
        help="Cold-replace verified Ray head/worker pods or explicitly skip that trigger",
    )
    parser.add_argument(
        "--web-url",
        default="http://localhost:30080",
        help="Local Django base URL (default: http://localhost:30080)",
    )
    parser.add_argument(
        "--prometheus-url",
        default="http://localhost:30090",
        help="Local Prometheus base URL (default: http://localhost:30090)",
    )
    parser.add_argument(
        "--kind-cluster-name",
        help="Optional Kind cluster name; must match the kind-<name> context",
    )
    parser.add_argument("--rollout-timeout", type=int, default=300)
    parser.add_argument("--task-timeout", type=int, default=180)
    parser.add_argument("--prometheus-timeout", type=int, default=120)
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=120,
        help="Maximum seconds for ordinary subprocesses (default: 120)",
    )
    parser.add_argument(
        "--build-timeout",
        type=int,
        default=1200,
        help="Maximum seconds for each Docker build or Kind image load (default: 1200)",
    )
    parser.add_argument(
        "--kubectl-request-timeout",
        type=int,
        default=30,
        help="Maximum seconds for one Kubernetes API request (default: 30)",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run clean-tree, context, render, and client-dry-run checks without mutations",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = _parser().parse_args(argv)
    if (
        args.rollout_timeout <= 0
        or args.task_timeout <= 0
        or args.prometheus_timeout < 0
        or args.command_timeout <= 0
        or args.build_timeout <= 0
        or args.kubectl_request_timeout <= 0
    ):
        print("timeouts must be positive (Prometheus timeout may be zero)", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    config = GateConfig(
        root=root,
        context=args.context,
        namespace=args.namespace,
        ray_restart=args.ray_restart,
        web_url=args.web_url,
        prometheus_url=args.prometheus_url,
        kind_cluster_name=args.kind_cluster_name,
        rollout_timeout=args.rollout_timeout,
        task_timeout=args.task_timeout,
        prometheus_timeout=args.prometheus_timeout,
        command_timeout=args.command_timeout,
        build_timeout=args.build_timeout,
        kubectl_request_timeout=args.kubectl_request_timeout,
        preflight_only=args.preflight_only,
    )
    gate = LocalKubeRayGate(config)
    try:
        gate.run()
    except GateError as error:
        primary_failure = f"FAILED [{error.layer}]: {gate.redactor.clean(error)}"
        if getattr(gate, "diagnostics_attempted", False):
            print(
                _bounded_redacted_error(
                    primary_failure,
                    redactor=gate.redactor,
                    characters=MAX_OUTPUT_CHARACTERS - 1,
                ),
                file=sys.stderr,
            )
        else:
            try:
                gate.diagnostics(error.layer)
            except Exception as diagnostic_error:
                print(
                    _bounded_redacted_error(
                        (f"bounded diagnostics unavailable: {diagnostic_error}\n{primary_failure}"),
                        redactor=gate.redactor,
                        characters=MAX_OUTPUT_CHARACTERS - 1,
                    ),
                    file=sys.stderr,
                )
            else:
                print(
                    _bounded_redacted_error(
                        primary_failure,
                        redactor=gate.redactor,
                        characters=MAX_OUTPUT_CHARACTERS - 1,
                    ),
                    file=sys.stderr,
                )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
