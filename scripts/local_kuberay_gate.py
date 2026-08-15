"""Run the guarded local Docker Desktop/Kind KubeRay final integration gate.

The gate intentionally owns only resources rendered from the checked-in
``k8s/overlays/kuberay-kind`` overlay in the ``django-ray`` namespace.  It
never deletes the namespace, PostgreSQL, PVCs, or Docker data.
"""

from __future__ import annotations

import argparse
import base64
import binascii
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
from uuid import UUID, uuid4

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
    "django-ray-worker-ray-job",
)
TASK_MANAGER_DEPLOYMENTS = APP_DEPLOYMENTS[1:]
RAY_JOB_MANAGER_DEPLOYMENT = "django-ray-worker-ray-job"
RAY_JOB_GATE_QUEUE = "ray-data"
RAY_JOB_REQUEST_REFERENCE_CARRIER = "--request-ref-b64"
RAY_JOB_RELEASED_PAYLOAD_CARRIER = "--payload-b64"
RAY_JOB_GATE_CALLABLE = "testproject.tasks.slow_task"
RAY_JOB_GATE_SECONDS = 90.125
RAY_JOB_GATE_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "STOPPED"})
RELEASED_V040_TAG = "v0.4.0"
RELEASED_V040_COMMIT = "95ee5dfe95b1c1bed95ff28c4fcb5fcdc491e485"
RELEASED_V040_SOURCE_TREE = "6ce02dfe51832db6227cc886bcced62399167f8b"
RELEASED_V040_IMAGE_REPOSITORY = "django-ray-released-v040"
RELEASED_V040_MANAGER_DEPLOYMENT = "django-ray-worker-ray-job-v040"
RELEASED_V040_MANAGER_CONTAINER = "django-ray-worker-v040"
RELEASED_V040_HOSTNAME_PATTERN = re.compile(r"dr-v040-[0-9a-f]{32}\Z")
RELEASED_V040_IMAGE_TAG_PATTERN = re.compile(
    rf"{re.escape(RELEASED_V040_IMAGE_REPOSITORY)}:"
    r"released-v040-local-gate-tree-[0-9a-f]{12}-[0-9]{14}-[0-9a-f]{8}\Z"
)
PROTOCOL_V2_POISON_PATTERN = re.compile(r"protocol_v2_application_poison_[0-9a-f]{32}\Z")
PROTOCOL_V1_SURVIVAL_PATTERN = re.compile(r"protocol_v1_handoff_queued_[0-9a-f]{32}\Z")
RAY_JOB_REQUEST_STORAGE_CLAIM = "payload-storage-pvc"
RAY_JOB_REQUEST_STORAGE_VOLUME = "payload-storage"
RAY_JOB_REQUEST_STORAGE_MOUNT_PATH = "/payload-storage"
RAY_JOB_REQUEST_STORAGE_CONFIG = {
    "DJANGO_RAY_INPUT_STORAGE_BACKEND": "filesystem",
    "DJANGO_RAY_INPUT_STORAGE_FILESYSTEM_PATH": "/payload-storage/inputs",
}
RAY_COMPONENTS = frozenset({"head", "worker"})
RAY_CLUSTER_NAME = "ray"
RAY_CLUSTER_LABEL = "ray.io/cluster"
RAY_GROUP_LABEL = "ray.io/group"
KUBERAY_WAIT_GCS_INIT = "wait-gcs-ready"
RAY_IMAGE_PYTHON_VERSION_PATTERN = re.compile(r"3\.12\.(?:0|[1-9][0-9]*)\Z")
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
RECOVERY_RUNTIME_ENV_ARCHIVE = "/runtime-env/django-ray-recovery.zip"
RECOVERY_RUNTIME_ENV_MAX_BYTES = 32 * 1024 * 1024
RECOVERY_RUNTIME_ENV_REQUIRED_MEMBERS = (
    "cryptography/__init__.py",
    "django/__init__.py",
    "django_ray/runtime/remote.py",
    "psycopg/__init__.py",
    "testproject/apps/cluster_tasks/workflows.py",
    "unfold/__init__.py",
)
RUNTIME_ENV_ENCRYPTION_PROBE_PATH = "/api/cluster/runtime-env/probe?profile=thin"
RUNTIME_ENV_ENCRYPTION_RESULT_PATH = "/api/cluster/runtime-env/{task_id}"
RUNTIME_ENV_STORAGE_PROBE_MARKER = "django-ray-runtime-env-encryption-canary-v1-7c4e2a91"
RUNTIME_ENV_FAILURE_UNKNOWN_KEY_ID = "django-ray-gate-unknown"
RUNTIME_ENV_ENVELOPE_FORMAT = "django-ray.runtime-env.encrypted"
RUNTIME_ENV_ENVELOPE_VERSION = 1
RUNTIME_ENV_ENVELOPE_ALGORITHM = "AES-256-GCM"
RUNTIME_ENV_ENVELOPE_FIELDS = frozenset(
    {
        "format",
        "version",
        "algorithm",
        "key_id",
        "nonce",
        "ciphertext",
    }
)
RUNTIME_ENV_ENCRYPTION_ENV = {
    "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE": "encrypted",
    "DJANGO_RAY_RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "django-secret",
    "DJANGO_RAY_RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": "true",
}
RUNTIME_ENV_FAILURE_FIXTURE_CALLABLE = "testproject.apps.cluster_tasks.tasks.runtime_env_probe"
RUNTIME_ENV_FAILURE_FIXTURE_SCRIPT = """
import json
import uuid

from django.db import transaction

from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.runtime_env import (
    resolve_runtime_env_profile,
    runtime_env_for_storage,
)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


with transaction.atomic():
    resolved = resolve_runtime_env_profile("thin")
    fixtures = {}
    for mutation in ("ciphertext", "key_id"):
        task_id = str(uuid.uuid4())
        stored = runtime_env_for_storage(resolved, task_id=task_id)
        envelope = json.loads(stored.serialized)
        if mutation == "ciphertext":
            ciphertext = envelope["ciphertext"]
            envelope["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        else:
            envelope["key_id"] = "django-ray-gate-unknown"
        serialized = canonical_json(envelope)
        execution = RayTaskExecution.objects.create(
            task_id=task_id,
            callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            queue_name="default",
            priority=100,
            state=TaskState.QUEUED,
            runtime_env_profile=stored.profile,
            runtime_env_json=serialized,
            runtime_env_hash=stored.digest,
            timeout_seconds=30,
        )
        fixtures[mutation] = {
            "id": execution.pk,
            "task_id": task_id,
            "envelope": serialized,
            "nonce": envelope["nonce"],
            "ciphertext": envelope["ciphertext"],
        }

print(canonical_json(fixtures))
""".strip()
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
COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_PATH = (
    "/api/cluster/complex-workflow?fast_items=2&slow_items=1"
    "&fast_seconds=0.01&slow_seconds=0.02"
    "&reporting_policy=terminal_only"
)
COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_KWARGS = {
    **COMPLEX_WORKFLOW_ENQUEUE_KWARGS,
    "reporting_policy": "terminal_only",
}
COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_PATH = (
    "/api/cluster/complex-workflow?fast_items=2&slow_items=1"
    "&fast_seconds=0.01&slow_seconds=0.05"
    "&failure_branch=slow&failure_item=0"
    "&reporting_policy=terminal_only"
)
COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_KWARGS = {
    **COMPLEX_WORKFLOW_FAILURE_ENQUEUE_KWARGS,
    "reporting_policy": "terminal_only",
}
COMPLEX_WORKFLOW_FAILURE_MESSAGE = "Intentional complex workflow fixture failure"
WORKFLOW_SHOWCASE_ENQUEUE_PATH = "/api/cluster/workflow-showcase?item_count=1&work_seconds=0.01"
WORKFLOW_SHOWCASE_ENQUEUE_KWARGS = {
    "item_count": 1,
    "work_seconds": 0.01,
}
WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_PATH = (
    "/api/cluster/workflow-showcase?item_count=1&work_seconds=0.01"
    "&failure_stage=reserve_inventory&failure_item=0"
)
WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_KWARGS = {
    **WORKFLOW_SHOWCASE_ENQUEUE_KWARGS,
    "failure_stage": "reserve_inventory",
    "failure_item": 0,
}
WORKFLOW_SHOWCASE_FAILURE_MESSAGE = (
    "Intentional workflow showcase reserve_inventory failure at item 0"
)
WORKFLOW_SHOWCASE_CALLABLE = "testproject.apps.cluster_tasks.tasks.order_fulfillment_showcase_task"
WORKFLOW_SHOWCASE_PAGE_LIMIT = 64
WORKFLOW_SHOWCASE_NODE_LAYERS = (
    frozenset({"0.0"}),
    frozenset(
        {
            "0.1.g0.0",
            "0.1.g1.0.g0",
            "0.1.g1.0.g1",
            "0.1.g2",
        }
    ),
    frozenset({"0.1.g0.1.m0", "0.1.g1.1"}),
    frozenset({"0.2"}),
    frozenset(
        {
            "0.3.g0",
            "0.3.g1.0.g0",
            "0.3.g1.0.g1.0.g0",
            "0.3.g1.0.g1.0.g1",
        }
    ),
    frozenset({"0.3.g1.0.g1.1"}),
    frozenset({"0.3.g1.1"}),
    frozenset({"0.4"}),
    frozenset({"0.5.m0"}),
    frozenset({"0.6"}),
    frozenset({"0.7.g0", "0.7.g1", "0.7.g2"}),
    frozenset({"0.8"}),
)
WORKFLOW_SHOWCASE_EDGES = frozenset(
    {
        ("0.0", "0.1.g0.0"),
        ("0.1.g0.0", "0.1.g0.1.m0"),
        ("0.0", "0.1.g1.0.g0"),
        ("0.0", "0.1.g1.0.g1"),
        ("0.1.g1.0.g0", "0.1.g1.1"),
        ("0.1.g1.0.g1", "0.1.g1.1"),
        ("0.0", "0.1.g2"),
        ("0.1.g0.1.m0", "0.2"),
        ("0.1.g1.1", "0.2"),
        ("0.1.g2", "0.2"),
        ("0.2", "0.3.g0"),
        ("0.2", "0.3.g1.0.g0"),
        ("0.2", "0.3.g1.0.g1.0.g0"),
        ("0.2", "0.3.g1.0.g1.0.g1"),
        ("0.3.g1.0.g1.0.g0", "0.3.g1.0.g1.1"),
        ("0.3.g1.0.g1.0.g1", "0.3.g1.0.g1.1"),
        ("0.3.g1.0.g0", "0.3.g1.1"),
        ("0.3.g1.0.g1.1", "0.3.g1.1"),
        ("0.3.g0", "0.4"),
        ("0.3.g1.1", "0.4"),
        ("0.4", "0.5.m0"),
        ("0.5.m0", "0.6"),
        ("0.6", "0.7.g0"),
        ("0.6", "0.7.g1"),
        ("0.6", "0.7.g2"),
        ("0.7.g0", "0.8"),
        ("0.7.g1", "0.8"),
        ("0.7.g2", "0.8"),
    }
)
WORKFLOW_SHOWCASE_FAILURE_NODE_ID = "0.5.m0"
WORKFLOW_SHOWCASE_VALIDATION_NODE_ID = "0.1.g0.1.m0"
WORKFLOW_SHOWCASE_PROJECTOR_FAILURE_NODE_ID = "0.1.g1.0.g1"
WORKFLOW_SHOWCASE_FAILURE_DESCENDANTS = frozenset(
    {
        "0.6",
        "0.7.g0",
        "0.7.g1",
        "0.7.g2",
        "0.8",
    }
)
WORKFLOW_SHOWCASE_FAILURE_SUCCEEDED_NODES = (
    frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS)
    - {WORKFLOW_SHOWCASE_FAILURE_NODE_ID}
    - WORKFLOW_SHOWCASE_FAILURE_DESCENDANTS
)
WORKFLOW_SHOWCASE_SUCCESS_RESULT = {
    "engine": "django-ray-workflow",
    "workflow": "order-fulfillment-showcase",
    "durability_boundary": "single RayTaskExecution",
    "order_id": "showcase-order-0001",
    "status": "FULFILLED",
    "item_count": 1,
    "reserved_units": 1,
    "currency": "USD",
    "total_cents": 1000,
    "risk": "LOW",
    "recommendation": "PRIORITY_FULFILLMENT",
    "decision": "APPROVED",
    "sinks": {
        "primary": "WRITTEN",
        "audit": "WRITTEN",
        "notification": "SENT",
    },
}
WORKFLOW_RECOVERY_ENQUEUE_PATH = (
    "/api/cluster/workflow-recovery-showcase?item_count=1&work_seconds=0.01"
)
WORKFLOW_RECOVERY_ENQUEUE_KWARGS = {
    "item_count": 1,
    "work_seconds": 0.01,
}
WORKFLOW_RECOVERY_POLL_PATH = "/api/cluster/workflow-recovery-showcase"
WORKFLOW_RECOVERY_CALLABLE = (
    "testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task"
)
WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE = (
    "Intentional workflow recovery failure at build_order_batch on durable attempt 1"
)
WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE = (
    "Intentional workflow recovery failure at join_order_inputs on durable attempt 2"
)
WORKFLOW_RECOVERY_SUCCESS_RESULT = {
    **WORKFLOW_SHOWCASE_SUCCESS_RESULT,
    "recovery": {
        "scenario": "three-attempt-recovery",
        "attempt_number": 3,
        "outcome": "SUCCEEDED",
    },
}
WORKFLOW_RECOVERY_EARLY_NODE_IDS = frozenset({"0.0", "0.1.g0.0"})
WORKFLOW_RECOVERY_EARLY_EDGES = frozenset({("0.0", "0.1.g0.0")})
WORKFLOW_RECOVERY_EARLY_FAILURE_NODE_ID = "0.0"
WORKFLOW_RECOVERY_EARLY_PENDING_NODES = frozenset({"0.1.g0.0"})
WORKFLOW_RECOVERY_MID_NODE_IDS = frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS[:8])
WORKFLOW_RECOVERY_MID_EDGES = frozenset(
    (source, target)
    for source, target in WORKFLOW_SHOWCASE_EDGES
    if source in WORKFLOW_RECOVERY_MID_NODE_IDS and target in WORKFLOW_RECOVERY_MID_NODE_IDS
)
WORKFLOW_RECOVERY_MID_FAILURE_NODE_ID = "0.2"
WORKFLOW_RECOVERY_MID_SUCCEEDED_NODES = frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS[:3])
WORKFLOW_RECOVERY_MID_PENDING_NODES = frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS[4:8])
WORKFLOW_ADMIN_LOOPBACK_URL = "http://127.0.0.1:8000"
WORKFLOW_PROGRESS_SCHEMA_VERSION = 3
WORKFLOW_PROGRESS_PAGE_LIMIT = 16
WORKFLOW_PROGRESS_COLLECTION_PATHS = {
    "topology_nodes": "topology/nodes",
    "topology_edges": "topology/edges",
    "node_details": "nodes",
}
TASK_FAILURE_STATES = frozenset({"FAILED", "CANCELLED", "LOST", "EXPIRED"})
WORKFLOW_PROGRESS_TASK_STATES = frozenset(
    {
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "CANCELLING",
        "LOST",
        "EXPIRED",
    }
)
WORKFLOW_PROGRESS_FAILURE_STATES = TASK_FAILURE_STATES
MAX_COMMAND_ERROR_LINES = 60
MAX_DIAGNOSTIC_LINES = 80
MAX_OUTPUT_CHARACTERS = 16_000
MAX_GATE_ERROR_CHARACTERS = MAX_OUTPUT_CHARACTERS - 256
MAX_HTTP_RESPONSE_BYTES = 64 * 1024
MAX_OPENAPI_SCHEMA_BYTES = 128_000
EXPECTED_TASK_STATUS_INPUT_MAX_BYTES = 16 * 1024
EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES = 64 * 1024
EXPECTED_EXECUTION_PROTOCOL_VERSION = 1
EXPECTED_EXECUTION_PROVENANCE_MAX_BYTES = 128
EXPECTED_EXECUTION_PROTOCOL_METRIC = "django_ray_tasks_by_execution_protocol_total"
TASK_STATUS_INPUT_OMISSION_REASONS = frozenset(
    {
        None,
        "external_input_not_loaded",
        "stored_input_exceeds_status_limit",
        "malformed_inline_input",
        "encoded_response_limit",
    }
)
TASK_STATUS_BY_STATE = {
    "QUEUED": "READY",
    "RUNNING": "RUNNING",
    "SUCCEEDED": "SUCCESSFUL",
    "FAILED": "FAILED",
    "CANCELLED": "FAILED",
    "CANCELLING": "RUNNING",
    "LOST": "FAILED",
    "EXPIRED": "FAILED",
}
EXPECTED_EXECUTION_PROTOCOL_METRIC_PROTOCOLS = frozenset({"1", "other"})
EXPECTED_EXECUTION_PROTOCOL_METRIC_STATES = frozenset(TASK_STATUS_BY_STATE)
EXECUTION_PROTOCOL_METRIC_SAMPLE_PATTERN = re.compile(
    rf"{EXPECTED_EXECUTION_PROTOCOL_METRIC}"
    r'\{protocol="(1|other)",state="([A-Z]+)"\} '
    r"(?:0|[1-9][0-9]{0,18})\Z"
)
EXPECTED_POLL_DIAGNOSTIC_MAX_BYTES = 16 * 1024
EXPECTED_POLL_RESPONSE_MAX_BYTES = 64 * 1024
EXPECTED_POLL_ATTEMPT_ERROR_MAX_BYTES = 4 * 1024
POLL_RESULT_OMISSION_REASONS = frozenset(
    {
        None,
        "external_result_not_loaded",
        "stored_result_exceeds_poll_limit",
        "malformed_inline_result",
        "encoded_response_limit",
    }
)
POLL_ERROR_OMISSION_REASONS = frozenset(
    {None, "stored_error_exceeds_poll_limit", "encoded_response_limit"}
)
POLL_ATTEMPT_ERROR_OMISSION_REASONS = frozenset(
    {None, "stored_error_exceeds_attempt_limit", "encoded_response_limit"}
)
EXPECTED_EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES = 64 * 1024
EXPECTED_EXECUTION_DETAIL_RESPONSE_MAX_BYTES = 256 * 1024
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
        "**/.env",
        "**/.env.*",
        "**/*.sqlite3",
        "**/*.sqlite3-*",
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
        "**/.env",
        "**/.env.*",
        "**/*.sqlite3",
        "**/*.sqlite3-*",
    ),
}
RELEASED_V040_DOCKER_CONTEXT_ALLOWLISTS = {
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
        ("v1", "PersistentVolumeClaim", "payload-storage-pvc"),
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
        sensitive_timeout_error: CommandError | None = None
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
            command_error = CommandError(self.redactor.clean(detail))
            if sensitive_output:
                sensitive_timeout_error = command_error
            else:
                raise command_error from error
        if sensitive_timeout_error is not None:
            # Raise after leaving the except block so the TimeoutExpired payload
            # is absent from both __cause__ and __context__.
            raise sensitive_timeout_error
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
    released_v040_image_tag: str = ""
    released_v040_image_id: str = ""
    setup_bundle_bytes: int = 0
    setup_bundle_sha256: str = ""
    recovery_bundle_bytes: int = 0
    recovery_bundle_sha256: str = ""
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
    api_task_status_bounded: bool = False
    api_workflow_runtime_polls_bounded: bool = False
    api_bulk_reset_absent: bool = False
    api_legacy_workflow_node_absent: bool = False
    api_execution_delete_rejected: bool = False
    api_legacy_workflow_graph_absent: bool = False
    runtime_env_encryption_overlay: bool = False
    runtime_env_encryption_canary: bool = False
    runtime_env_encryption_envelope: bool = False
    runtime_env_encryption_marker_absent: bool = False
    runtime_env_encryption_tamper_rejected: bool = False
    runtime_env_encryption_unknown_key_rejected: bool = False
    runtime_env_encryption_retry_preserved: bool = False
    runtime_env_encryption_logs_clear: bool = False
    django_ray_secret_preserved: bool = False
    ray_job_request_reference_carrier: bool = False
    ray_job_raw_info_clear: bool = False
    ray_job_processes_clear: bool = False
    ray_job_logs_clear: bool = False
    ray_job_manager_reconciled_same_job: bool = False
    ray_job_missing_reference_no_marker: bool = False
    ray_job_missing_reference_no_retry: bool = False
    protocol_legacy_cohort_visible: bool = False
    protocol_explicit_cohort_visible: bool = False
    protocol_v1_handoff_same_job: bool = False
    protocol_v1_handoff_no_resubmit: bool = False
    protocol_v1_queued_survived_handoff: bool = False
    protocol_v2_queued_unchanged: bool = False
    protocol_v2_unsupported_visible: bool = False
    protocol_v2_preinvocation_rejected: bool = False
    protocol_v2_application_marker_absent: bool = False
    protocol_v2_target_exact_completed: bool = False
    protocol_v2_target_mismatch_rejected: bool = False
    protocol_v2_target_mismatch_marker_absent: bool = False
    protocol_handoff_cleanup_restored: bool = False
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
    workflow_terminal_only_task_id: str = ""
    workflow_terminal_only_task_state: str = ""
    workflow_terminal_only_attempt_number: int = 0
    workflow_terminal_only_schema_version: int = 0
    workflow_terminal_only_summary_revision: int = 0
    workflow_terminal_only_declared_nodes: int = 0
    workflow_terminal_only_declared_edges: int = 0
    workflow_terminal_only_admin_actions: int = 0
    workflow_terminal_only_graph_advertised: bool = True
    workflow_terminal_only_storage_rows: int = -1
    workflow_terminal_only_failure_task_id: str = ""
    workflow_terminal_only_failure_task_state: str = ""
    workflow_terminal_only_failure_attempt_number: int = 0
    workflow_terminal_only_failure_schema_version: int = 0
    workflow_terminal_only_failure_summary_revision: int = 0
    workflow_terminal_only_failure_declared_nodes: int = 0
    workflow_terminal_only_failure_declared_edges: int = 0
    workflow_terminal_only_failure_admin_actions: int = 0
    workflow_terminal_only_failure_graph_advertised: bool = True
    workflow_terminal_only_failure_storage_rows: int = -1
    workflow_showcase_task_id: str = ""
    workflow_showcase_task_state: str = ""
    workflow_showcase_attempt_number: int = 0
    workflow_showcase_topology_nodes: int = 0
    workflow_showcase_topology_edges: int = 0
    workflow_showcase_longest_path_layers: int = 0
    workflow_showcase_detail_links: int = 0
    workflow_showcase_failure_task_id: str = ""
    workflow_showcase_failure_task_state: str = ""
    workflow_showcase_failure_attempt_number: int = 0
    workflow_showcase_failure_failed_nodes: int = 0
    workflow_showcase_failure_pending_descendants: int = 0
    workflow_showcase_failure_running_nodes: int = 0
    workflow_showcase_failure_succeeded_nodes: int = 0
    workflow_showcase_failure_path_nodes: int = 0
    workflow_showcase_failure_detail_links: int = 0
    workflow_recovery_task_id: str = ""
    workflow_recovery_task_state: str = ""
    workflow_recovery_attempt_number: int = 0
    workflow_recovery_attempt_count: int = 0
    workflow_recovery_distinct_runs: bool = False
    workflow_recovery_early_topology_nodes: int = 0
    workflow_recovery_early_topology_edges: int = 0
    workflow_recovery_early_pending_nodes: int = 0
    workflow_recovery_early_succeeded_nodes: int = 0
    workflow_recovery_early_failed_nodes: int = 0
    workflow_recovery_early_detail_links: int = 0
    workflow_recovery_mid_topology_nodes: int = 0
    workflow_recovery_mid_topology_edges: int = 0
    workflow_recovery_mid_pending_nodes: int = 0
    workflow_recovery_mid_succeeded_nodes: int = 0
    workflow_recovery_mid_failed_nodes: int = 0
    workflow_recovery_mid_detail_links: int = 0
    workflow_recovery_success_topology_nodes: int = 0
    workflow_recovery_success_topology_edges: int = 0
    workflow_recovery_success_succeeded_nodes: int = 0
    workflow_recovery_success_detail_links: int = 0
    workflow_recovery_admin_attempts: int = 0
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
    longest_path_layers: int = 0
    detail_links: int = 0
    pending_descendants: int = 0


@dataclass(frozen=True)
class TerminalOnlyWorkflowGateObservation:
    """One terminal summary-only workflow verified without retained detail."""

    task_id: str
    state: str
    attempt_number: int
    schema_version: int
    summary_revision: int
    declared_nodes: int
    declared_edges: int


@dataclass(frozen=True)
class WorkflowRecoveryAttemptObservation:
    """One independently readable attempt in the recovery demonstration."""

    attempt_number: int
    state: str
    execution_generation: int
    run_id: str
    plan_fingerprint: str
    topology_nodes: int
    topology_edges: int
    pending_nodes: int
    running_nodes: int
    succeeded_nodes: int
    failed_nodes: int
    detail_links: int


def validate_namespace(namespace: str) -> None:
    """Accept only the dedicated local-demo namespace."""
    if namespace != EXPECTED_NAMESPACE:
        raise ValueError(
            f"namespace must be exactly {EXPECTED_NAMESPACE!r}; received {namespace!r}"
        )


def validate_terminal_diagnostic_text(value: object, *, field_name: str) -> str:
    """Require one presented diagnostic to contain only inert text controls."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be non-empty text")
    if any(
        character not in {"\n", "\t"} and (ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F)
        for character in value
    ):
        raise ValueError(f"{field_name} retained terminal control characters")
    return value


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


def _parse_json_without_cause(
    value: str | bytes,
    *,
    error_message: str,
) -> Any:
    """Parse JSON while keeping raw input out of the raised exception graph."""
    parsed: Any = None
    parsed_ok = False
    try:
        parsed = json.loads(value)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        pass
    else:
        parsed_ok = True
    if not parsed_ok:
        # This raise must remain outside the except block. JSONDecodeError.doc
        # and UnicodeDecodeError.object may contain the complete private input.
        raise ValueError(error_message)
    return parsed


def _parse_single_json_object_line_without_cause(
    value: str,
    *,
    completion_marker: str,
    error_message: str,
) -> Mapping[str, Any]:
    """Extract one private JSON object completed by the controlled script."""
    lines = value.splitlines()
    marker_positions = [
        index for index, line in enumerate(lines) if line.strip() == completion_marker
    ]
    if len(marker_positions) != 1:
        raise ValueError(error_message)
    candidates: list[Mapping[str, Any]] = []
    for line in lines[: marker_positions[0]]:
        stripped = line.strip()
        if not stripped:
            continue
        parsed: Any = None
        parsed_ok = False
        try:
            parsed = json.loads(stripped)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            pass
        else:
            parsed_ok = True
        if parsed_ok and isinstance(parsed, Mapping):
            candidates.append(cast("Mapping[str, Any]", parsed))
            if len(candidates) > 1:
                break
    if len(candidates) != 1:
        # Keep this raise outside the except block. JSONDecodeError.doc may
        # contain a complete private diagnostic or response line.
        raise ValueError(error_message)
    return candidates[0]


def secret_data_sha256(data: Mapping[str, Any]) -> str:
    """Hash the complete base64 Secret data mapping without retaining its values."""
    canonical: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise ValueError("Secret/django-ray-secret data must map names to base64 strings")
        canonical[key] = value
    if not canonical:
        raise ValueError("Secret/django-ray-secret data must not be empty")
    serialized = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def inspect_runtime_env_encryption_secret_data(data: Mapping[str, Any]) -> None:
    """Reject selectors inherited from the preserved application Secret."""
    if any(name in data for name in RUNTIME_ENV_ENCRYPTION_ENV):
        raise ValueError(
            "Secret/django-ray-secret must not contain RuntimeEnv encryption selectors"
        )


def _runtime_env_encryption_pod_specs(
    resource: Mapping[str, Any],
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Return every rendered application or Ray pod spec with a stable label."""
    identity = _resource_identity(resource)
    kind = identity[1]
    if kind in {"Deployment", "Job"}:
        pod_spec = _pod_spec(resource)
        if pod_spec is None:  # pragma: no cover - guarded by the kind branch
            return ()
        return ((f"{kind}/{identity[2]}", pod_spec),)
    if kind != "RayCluster":
        return ()

    spec = _mapping(resource.get("spec"), field_name="RayCluster spec")
    head = _mapping(spec.get("headGroupSpec"), field_name="RayCluster headGroupSpec")
    head_template = _mapping(head.get("template"), field_name="RayCluster head template")
    head_pod_spec = _mapping(
        head_template.get("spec"),
        field_name="RayCluster head pod spec",
    )
    result: list[tuple[str, Mapping[str, Any]]] = [
        (f"RayCluster/{identity[2]} head", head_pod_spec)
    ]
    for index, value in enumerate(
        _sequence(spec.get("workerGroupSpecs"), field_name="RayCluster workerGroupSpecs")
    ):
        group = _mapping(value, field_name=f"RayCluster workerGroupSpecs[{index}]")
        name = group.get("groupName")
        if not isinstance(name, str) or not name:
            raise ValueError(f"RayCluster workerGroupSpecs[{index}] has no groupName")
        template = _mapping(
            group.get("template"),
            field_name=f"RayCluster workerGroupSpecs[{index}] template",
        )
        pod_spec = _mapping(
            template.get("spec"),
            field_name=f"RayCluster workerGroupSpecs[{index}] pod spec",
        )
        result.append((f"RayCluster/{identity[2]} worker {name}", pod_spec))
    return tuple(result)


def inspect_runtime_env_encryption_overlay(resources: Sequence[Mapping[str, Any]]) -> None:
    """Require encrypted Django-secret selectors only on application containers."""
    rendered_sources = {
        (identity[1], identity[2])
        for resource in resources
        if (identity := _resource_identity(resource))[1] in {"ConfigMap", "Secret"}
    }
    carriers: dict[tuple[str, str, str], dict[str, str]] = {}
    for resource in resources:
        identity = _resource_identity(resource)
        if identity[1] in {"ConfigMap", "Secret"}:
            for source_field in ("data", "stringData"):
                data = resource.get(source_field, {})
                if data is None:
                    continue
                mapping = _mapping(
                    data,
                    field_name=f"{identity[1]}/{identity[2]} {source_field}",
                )
                if any(name in mapping for name in RUNTIME_ENV_ENCRYPTION_ENV):
                    raise ValueError(
                        "RuntimeEnv encryption selectors must not use a shared ConfigMap or Secret"
                    )

        for workload, pod_spec in _runtime_env_encryption_pod_specs(resource):
            for collection in ("initContainers", "containers"):
                entries = _sequence(
                    pod_spec.get(collection, []),
                    field_name=f"{workload} {collection}",
                )
                for index, value in enumerate(entries):
                    container = _mapping(
                        value,
                        field_name=f"{workload} {collection}[{index}]",
                    )
                    container_name = container.get("name")
                    if not isinstance(container_name, str) or not container_name:
                        raise ValueError(f"{workload} {collection}[{index}] has no container name")
                    selected: dict[str, str] = {}
                    env_entries = _sequence(
                        container.get("env", []),
                        field_name=f"{workload} {container_name} env",
                    )
                    env_from_entries = _sequence(
                        container.get("envFrom", []),
                        field_name=f"{workload} {container_name} envFrom",
                    )
                    for env_from_index, env_from_value in enumerate(env_from_entries):
                        env_from = _mapping(
                            env_from_value,
                            field_name=(f"{workload} {container_name} envFrom[{env_from_index}]"),
                        )
                        references = tuple(
                            (field, kind)
                            for field, kind in (
                                ("configMapRef", "ConfigMap"),
                                ("secretRef", "Secret"),
                            )
                            if field in env_from
                        )
                        if len(references) != 1:
                            raise ValueError(
                                f"{workload} {container_name} envFrom must name exactly one source"
                            )
                        field, kind = references[0]
                        reference = _mapping(
                            env_from.get(field),
                            field_name=f"{workload} {container_name} {field}",
                        )
                        source_name = reference.get("name")
                        if (
                            not isinstance(source_name, str)
                            or not source_name
                            or (kind, source_name) not in rendered_sources
                        ):
                            raise ValueError(
                                f"{workload} {container_name} envFrom source is not rendered"
                            )
                    for env_index, env_value in enumerate(env_entries):
                        entry = _mapping(
                            env_value,
                            field_name=f"{workload} {container_name} env[{env_index}]",
                        )
                        name = entry.get("name")
                        if name not in RUNTIME_ENV_ENCRYPTION_ENV:
                            continue
                        if name in selected:
                            raise ValueError(
                                f"{workload} {container_name} duplicates RuntimeEnv selector {name}"
                            )
                        literal = entry.get("value")
                        if not isinstance(literal, str) or set(entry) != {"name", "value"}:
                            raise ValueError(
                                f"{workload} {container_name} must set RuntimeEnv selector "
                                f"{name} as a literal value"
                            )
                        selected[cast(str, name)] = literal
                    if selected:
                        carriers[(workload, collection, container_name)] = selected

    expected_workloads = {f"Deployment/{name}" for name in APP_DEPLOYMENTS}
    if (
        len(carriers) != len(expected_workloads)
        or {workload for workload, _collection, _container in carriers} != expected_workloads
        or any(collection != "containers" for _workload, collection, _container in carriers)
        or any(values != dict(RUNTIME_ENV_ENCRYPTION_ENV) for values in carriers.values())
    ):
        raise ValueError(
            "RuntimeEnv encrypted Django-secret mode must appear exactly on django-web "
            "and the default, synchronous, ML, and Ray Job task-manager containers"
        )


def inspect_ray_job_request_storage_overlay(
    resources: Sequence[Mapping[str, Any]],
) -> None:
    """Require exact rq2 writer/read-only-reader boundaries in the rendered overlay."""

    indexed = {_resource_identity(resource): resource for resource in resources}
    claim = indexed.get(("v1", "PersistentVolumeClaim", RAY_JOB_REQUEST_STORAGE_CLAIM))
    if claim is None:
        raise ValueError("rendered rq2 request storage claim is missing")
    claim_spec = _mapping(claim.get("spec"), field_name="rq2 request storage claim spec")
    if claim_spec.get("accessModes") != ["ReadWriteMany"]:
        raise ValueError("rq2 request storage claim must require exactly ReadWriteMany")

    config = indexed.get(("v1", "ConfigMap", "django-ray-config"))
    if config is None:
        raise ValueError("rendered django-ray ConfigMap is missing")
    config_data = _mapping(config.get("data"), field_name="django-ray ConfigMap data")
    if any(
        config_data.get(name) != value for name, value in RAY_JOB_REQUEST_STORAGE_CONFIG.items()
    ):
        raise ValueError("rendered rq2 request storage configuration is not exact")

    expected_mounts = {
        ("Deployment/django-web", "containers", "django-web"): False,
        ("Deployment/django-ray-worker", "containers", "django-ray-worker"): False,
        ("Deployment/django-ray-worker-ray-job", "containers", "django-ray-worker"): False,
        ("RayCluster/ray head", "containers", "ray-head"): True,
        ("RayCluster/ray worker worker-group", "containers", "ray-worker"): True,
    }
    expected_volume_workloads = {workload for workload, _collection, _name in expected_mounts}
    observed_mounts: set[tuple[str, str, str]] = set()
    observed_volume_workloads: set[str] = set()

    for resource in resources:
        for workload, pod_spec in _runtime_env_encryption_pod_specs(resource):
            volumes = _sequence(
                pod_spec.get("volumes", []),
                field_name=f"{workload} volumes",
            )
            normalized_volumes = [
                _mapping(value, field_name=f"{workload} volumes[{index}]")
                for index, value in enumerate(volumes)
            ]
            payload_volumes = [
                value
                for value in normalized_volumes
                if value.get("name") == RAY_JOB_REQUEST_STORAGE_VOLUME
            ]
            if len(payload_volumes) > 1:
                raise ValueError(f"{workload} duplicates the rq2 request storage volume")
            if payload_volumes:
                payload_volume = payload_volumes[0]
                persistent_claim = _mapping(
                    payload_volume.get("persistentVolumeClaim"),
                    field_name=f"{workload} rq2 request storage claim",
                )
                if set(payload_volume) != {"name", "persistentVolumeClaim"} or persistent_claim != {
                    "claimName": RAY_JOB_REQUEST_STORAGE_CLAIM
                }:
                    raise ValueError(f"{workload} rq2 request storage volume is not exact")
                observed_volume_workloads.add(workload)

            for collection in ("initContainers", "containers"):
                containers = _sequence(
                    pod_spec.get(collection, []),
                    field_name=f"{workload} {collection}",
                )
                for index, value in enumerate(containers):
                    container = _mapping(
                        value,
                        field_name=f"{workload} {collection}[{index}]",
                    )
                    container_name = container.get("name")
                    if not isinstance(container_name, str) or not container_name:
                        raise ValueError(f"{workload} {collection}[{index}] has no container name")
                    mounts = _sequence(
                        container.get("volumeMounts", []),
                        field_name=f"{workload} {container_name} volumeMounts",
                    )
                    normalized_mounts = [
                        _mapping(
                            mount,
                            field_name=f"{workload} {container_name} volumeMounts[{mount_index}]",
                        )
                        for mount_index, mount in enumerate(mounts)
                    ]
                    payload_mounts = [
                        mount
                        for mount in normalized_mounts
                        if mount.get("name") == RAY_JOB_REQUEST_STORAGE_VOLUME
                    ]
                    if len(payload_mounts) > 1:
                        raise ValueError(
                            f"{workload} {container_name} duplicates the rq2 request storage mount"
                        )
                    if not payload_mounts:
                        continue
                    key = (workload, collection, container_name)
                    read_only = expected_mounts.get(key)
                    if read_only is None:
                        raise ValueError(
                            f"{workload} {container_name} unexpectedly mounts rq2 request storage"
                        )
                    expected_mount: dict[str, object] = {
                        "name": RAY_JOB_REQUEST_STORAGE_VOLUME,
                        "mountPath": RAY_JOB_REQUEST_STORAGE_MOUNT_PATH,
                    }
                    if read_only:
                        expected_mount["readOnly"] = True
                    if payload_mounts[0] != expected_mount:
                        access = "read-only" if read_only else "read-write"
                        raise ValueError(
                            f"{workload} {container_name} rq2 request storage mount must be {access}"
                        )
                    observed_mounts.add(key)

    if observed_mounts != set(expected_mounts):
        raise ValueError("rendered rq2 request storage mount inventory is not exact")
    if observed_volume_workloads != expected_volume_workloads:
        raise ValueError("rendered rq2 request storage volume inventory is not exact")


def _decode_canonical_base64url(
    value: object,
    *,
    exact_bytes: int | None = None,
    minimum_bytes: int | None = None,
) -> bytes | None:
    """Decode one strict unpadded base64url value for envelope inspection."""
    if not isinstance(value, str) or not value or "=" in value:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        return None
    try:
        decoded = base64.b64decode(
            f"{value}{'=' * (-len(value) % 4)}".encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        return None
    if exact_bytes is not None and len(decoded) != exact_bytes:
        return None
    if minimum_bytes is not None and len(decoded) < minimum_bytes:
        return None
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    return decoded if canonical == value else None


def validate_runtime_env_encryption_envelope(
    serialized: str,
    *,
    marker: str = RUNTIME_ENV_STORAGE_PROBE_MARKER,
) -> tuple[str, str]:
    """Validate the strict persisted canary envelope without decrypting it."""
    if not isinstance(serialized, str) or not serialized or marker in serialized:
        raise ValueError("persisted RuntimeEnv envelope exposed the plaintext probe marker")
    value = _parse_json_without_cause(
        serialized,
        error_message="persisted RuntimeEnv envelope is not valid JSON",
    )
    envelope = _mapping(value, field_name="persisted RuntimeEnv envelope")
    if set(envelope) != RUNTIME_ENV_ENVELOPE_FIELDS:
        raise ValueError("persisted RuntimeEnv envelope fields are not exact")
    if (
        envelope.get("format") != RUNTIME_ENV_ENVELOPE_FORMAT
        or type(envelope.get("version")) is not int
        or envelope.get("version") != RUNTIME_ENV_ENVELOPE_VERSION
        or envelope.get("algorithm") != RUNTIME_ENV_ENVELOPE_ALGORITHM
        or envelope.get("key_id") != "django-secret"
    ):
        raise ValueError("persisted RuntimeEnv envelope metadata is not the guarded contract")
    canonical = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if serialized != canonical:
        raise ValueError("persisted RuntimeEnv envelope is not canonical")
    nonce = envelope.get("nonce")
    ciphertext = envelope.get("ciphertext")
    if _decode_canonical_base64url(nonce, exact_bytes=12) is None:
        raise ValueError("persisted RuntimeEnv envelope nonce is malformed")
    if _decode_canonical_base64url(ciphertext, minimum_bytes=16) is None:
        raise ValueError("persisted RuntimeEnv envelope ciphertext is malformed")
    return cast(str, nonce), cast(str, ciphertext)


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


def ray_runtime_image_reference(topology: RayTopology) -> str:
    """Return one exact pinned image reference shared by every rendered Ray runtime."""

    def named_image(contract: PodImageContract, *, name: str, field_name: str) -> str:
        matches = tuple(
            image for container_name, image in contract.containers if container_name == name
        )
        if len(matches) != 1:
            raise ValueError(f"{field_name} must declare exactly one {name!r} container")
        return matches[0]

    images = [
        named_image(
            topology.head_pod_images,
            name="ray-head",
            field_name="RayCluster head template",
        )
    ]
    images.extend(
        named_image(
            group.pod_images,
            name="ray-worker",
            field_name=f"RayCluster worker group {group.name!r}",
        )
        for group in topology.worker_groups
    )
    distinct = set(images)
    if len(distinct) != 1:
        raise ValueError("rendered Ray head and workers must share one exact image reference")
    image = images[0]
    if (
        len(image) > MAX_RAY_IMAGE_REFERENCE_CHARACTERS
        or not image.isascii()
        or not image.isprintable()
        or image.startswith("-")
    ):
        raise ValueError("the rendered Ray runtime image reference is not bounded canonical ASCII")
    normalized = normalize_image_reference(image)
    leaf = normalized.rsplit("/", 1)[-1]
    if normalized.endswith(":latest") or (":" not in leaf and "@" not in leaf):
        raise ValueError("the rendered Ray runtime image must use a pinned reference")
    return image


def parse_ray_image_python_version(stdout: str) -> str:
    """Parse one canonical Python 3.12 patch emitted by a Ray image probe."""

    if stdout.endswith("\n"):
        value = stdout[:-1]
    else:
        value = stdout
    if RAY_IMAGE_PYTHON_VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("Ray image Python probe must emit exactly one canonical 3.12.X line")
    return value


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
        "Recovery RuntimeEnv bundle ready:",
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


def inspect_docker_context_allowlists(
    context: Path,
    *,
    expected_allowlists: Mapping[str, tuple[str, ...]] = DOCKER_CONTEXT_ALLOWLISTS,
) -> None:
    """Require exact Dockerfile-specific deny-by-default context policies."""
    for name, expected in expected_allowlists.items():
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


def _docker_context_allowlists_for_source(
    *, commit: str, source_tree: str
) -> Mapping[str, tuple[str, ...]]:
    """Select the exact policy bound to a reviewed source identity."""
    if commit == RELEASED_V040_COMMIT:
        if source_tree != RELEASED_V040_SOURCE_TREE:
            raise ValueError("the pinned v0.4.0 commit has an unexpected source tree")
        return RELEASED_V040_DOCKER_CONTEXT_ALLOWLISTS
    return DOCKER_CONTEXT_ALLOWLISTS


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
    inspect_docker_context_allowlists(
        context,
        expected_allowlists=_docker_context_allowlists_for_source(
            commit=commit,
            source_tree=source_tree,
        ),
    )
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


def parse_runtime_archive_probe(value: str) -> tuple[int, str, int, str]:
    """Verify one generic Ray node and both mounted RuntimeEnv archives."""
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
    if payload.get("recovery_required_members") is not True:
        raise ValueError("Recovery RuntimeEnv archive is missing its required package closure")
    recovery_size = payload.get("recovery_bytes")
    recovery_digest = payload.get("recovery_sha256")
    if (
        not isinstance(recovery_size, int)
        or recovery_size <= 0
        or recovery_size > RECOVERY_RUNTIME_ENV_MAX_BYTES
    ):
        raise ValueError("Recovery RuntimeEnv archive byte size is outside its identity limit")
    if (
        not isinstance(recovery_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", recovery_digest) is None
    ):
        raise ValueError("Recovery RuntimeEnv archive SHA-256 is invalid")
    return size, digest, recovery_size, recovery_digest


def parse_task_result(value: object) -> object:
    """Decode the durable JSON result stored in the sample execution response."""
    if not isinstance(value, str):
        raise ValueError("durable task result_data must be a JSON string")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("durable task result_data is not valid JSON") from error


def validate_task_status_payload(
    payload: Mapping[str, Any],
    *,
    task_id: str,
) -> str:
    """Validate one bounded task-status response and return its durable state."""
    if payload.get("task_id") != task_id:
        raise ValueError("task status polling returned the wrong task")
    state = payload.get("state")
    expected_status = TASK_STATUS_BY_STATE.get(state)
    if expected_status is None or payload.get("status") != expected_status:
        raise ValueError("task status polling returned an inconsistent state/status pair")
    attempt_number = payload.get("attempt_number")
    if type(attempt_number) is not int or attempt_number < 1:
        raise ValueError("task status polling returned an invalid attempt number")
    execution_generation = payload.get("execution_generation")
    if type(execution_generation) is not int or execution_generation < 0:
        raise ValueError("task status polling returned an invalid execution generation")
    if payload.get("input_max_bytes") != EXPECTED_TASK_STATUS_INPUT_MAX_BYTES:
        raise ValueError("task status polling changed its input byte limit")
    if payload.get("response_max_bytes") != EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES:
        raise ValueError("task status polling changed its response byte limit")

    if "input_omission_reason" not in payload:
        raise ValueError("task status polling omitted its input omission reason")
    omission_reason = payload.get("input_omission_reason")
    if omission_reason is not None and (
        not isinstance(omission_reason, str)
        or omission_reason not in TASK_STATUS_INPUT_OMISSION_REASONS
    ):
        raise ValueError("task status polling returned an unknown input omission reason")
    args = payload.get("args")
    kwargs = payload.get("kwargs")
    if omission_reason is None:
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError("task status polling omitted inline input without a reason")
    elif args is not None or kwargs is not None:
        raise ValueError("task status polling mixed input with an omission reason")
    return cast(str, state)


def validate_execution_protocol_visibility(
    payload: Mapping[str, Any],
    *,
    surface: str,
    expected_protocol: int = EXPECTED_EXECUTION_PROTOCOL_VERSION,
    expected_compatible: bool = True,
) -> None:
    """Validate the fixed protocol and bounded provenance API projection."""
    if type(expected_protocol) is not int or expected_protocol < 1:
        raise ValueError("expected execution protocol must be a positive integer")
    if type(expected_compatible) is not bool:
        raise ValueError("expected protocol compatibility must be boolean")
    if type(payload.get("execution_protocol_version")) is not int or (
        payload.get("execution_protocol_version") != expected_protocol
    ):
        raise ValueError(f"{surface} did not report execution protocol version {expected_protocol}")

    state = payload.get("state")
    required_provenance = {
        "created_with_django_ray_version": True,
        "managed_with_django_ray_version": state in {"RUNNING", "SUCCEEDED"},
        "executor_django_ray_version": state == "SUCCEEDED",
    }
    for field_name, required in required_provenance.items():
        value = payload.get(field_name)
        if value is None:
            if required:
                raise ValueError(f"{surface} omitted applicable package provenance")
            continue
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"{surface} returned invalid package provenance")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(f"{surface} returned invalid package provenance") from error
        if len(encoded) > EXPECTED_EXECUTION_PROVENANCE_MAX_BYTES:
            raise ValueError(f"{surface} returned unbounded package provenance")

    if payload.get("protocol_compatible_worker_available") is not expected_compatible:
        expectation = "has no" if expected_compatible else "unexpectedly has"
        raise ValueError(f"{surface} {expectation} protocol-compatible worker capacity")
    if payload.get("queue_capacity_attested") is not False:
        raise ValueError(f"{surface} unexpectedly attested queue-specific capacity")


def validate_execution_protocol_metrics(body: bytes) -> dict[tuple[str, str], int]:
    """Require the complete fixed-bucket execution-protocol metric family."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("authenticated metrics were not valid UTF-8") from error

    help_line = (
        f"# HELP {EXPECTED_EXECUTION_PROTOCOL_METRIC} "
        "Total tasks by bounded execution-protocol bucket and state"
    )
    type_line = f"# TYPE {EXPECTED_EXECUTION_PROTOCOL_METRIC} gauge"
    lines = text.splitlines()
    if lines.count(help_line) != 1 or lines.count(type_line) != 1:
        raise ValueError("authenticated metrics omitted the bounded protocol metric family")

    samples: dict[tuple[str, str], int] = {}
    for line in lines:
        if not line.startswith(EXPECTED_EXECUTION_PROTOCOL_METRIC):
            continue
        match = EXECUTION_PROTOCOL_METRIC_SAMPLE_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError("authenticated metrics changed the protocol metric label contract")
        key = (match.group(1), match.group(2))
        if key in samples:
            raise ValueError("authenticated metrics duplicated a protocol metric bucket")
        samples[key] = int(line.rsplit(" ", 1)[1])

    expected = {
        (protocol, state)
        for protocol in EXPECTED_EXECUTION_PROTOCOL_METRIC_PROTOCOLS
        for state in EXPECTED_EXECUTION_PROTOCOL_METRIC_STATES
    }
    if set(samples) != expected:
        raise ValueError("authenticated metrics returned incomplete protocol metric buckets")
    return samples


def validate_protocol_v2_rejection(payload: Mapping[str, Any]) -> None:
    """Validate the fixed pre-invocation result of the protocol-v2 probe."""

    expected = {
        "classification": "unsupported_protocol",
        "success": False,
        "retryable": False,
        "traceback_absent": True,
        "result_absent": True,
        "result_reference_absent": True,
        "exception_type": "RayExecutionRequestIncompatible",
        "transport_version": 1,
        "input_reference_absent": True,
        "application_marker_present": False,
    }
    if set(payload) != set(expected) or any(
        type(payload.get(field_name)) is not type(value) or payload.get(field_name) != value
        for field_name, value in expected.items()
    ):
        raise ValueError("protocol-v2 probe did not return the fixed pre-invocation rejection")


def validate_protocol_v2_target_execution(payload: Mapping[str, Any]) -> None:
    """Validate secret-free exact and mismatched package-private p2 evidence."""

    expected = {
        "exact_result_kind": "completion",
        "exact_application_invoked": True,
        "exact_application_success": True,
        "exact_application_result": 5,
        "exact_observed_proof_bound": True,
        "mismatch_result_kind": "compatibility_rejection",
        "mismatch_reason": "python_version_mismatch",
        "mismatch_application_invoked": False,
        "mismatch_marker_present": False,
        "mismatch_observed_proof_bound": True,
    }
    if set(payload) != set(expected) or any(
        type(payload.get(field_name)) is not type(value) or payload.get(field_name) != value
        for field_name, value in expected.items()
    ):
        raise ValueError("protocol-v2 target execution proof was incomplete or unsafe")


def validate_bounded_poll_projection(
    payload: Mapping[str, Any],
    *,
    surface: str,
    task_id: str,
) -> None:
    """Validate shared Workflow and RuntimeEnv poll bounds and omission fields."""
    if payload.get("task_id") != task_id:
        raise ValueError(f"{surface} returned the wrong task")
    if payload.get("diagnostic_max_bytes") != EXPECTED_POLL_DIAGNOSTIC_MAX_BYTES:
        raise ValueError(f"{surface} changed its diagnostic byte limit")
    if payload.get("response_max_bytes") != EXPECTED_POLL_RESPONSE_MAX_BYTES:
        raise ValueError(f"{surface} changed its response byte limit")

    if "result_omission_reason" not in payload:
        raise ValueError(f"{surface} omitted its result omission reason")
    result_reason = payload.get("result_omission_reason")
    if result_reason is not None and (
        not isinstance(result_reason, str) or result_reason not in POLL_RESULT_OMISSION_REASONS
    ):
        raise ValueError(f"{surface} returned an unknown result omission reason")
    result = payload.get("result")
    if result is not None and not isinstance(result, Mapping):
        raise ValueError(f"{surface} returned a non-object result")
    if result_reason is not None and result is not None:
        raise ValueError(f"{surface} mixed a result with its omission reason")

    if "error_omission_reason" not in payload:
        raise ValueError(f"{surface} omitted its error omission reason")
    error_reason = payload.get("error_omission_reason")
    if error_reason is not None and (
        not isinstance(error_reason, str) or error_reason not in POLL_ERROR_OMISSION_REASONS
    ):
        raise ValueError(f"{surface} returned an unknown error omission reason")
    error = payload.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError(f"{surface} returned a non-string error")
    if error_reason is not None and error is not None:
        raise ValueError(f"{surface} mixed an error with its omission reason")

    if "attempts" not in payload:
        return
    if payload.get("attempt_error_max_bytes") != EXPECTED_POLL_ATTEMPT_ERROR_MAX_BYTES:
        raise ValueError(f"{surface} changed its attempt error byte limit")
    attempts = _sequence(payload.get("attempts"), field_name=f"{surface} attempts")
    for value in attempts:
        attempt = _mapping(value, field_name=f"{surface} attempt")
        if "error_omission_reason" not in attempt:
            raise ValueError(f"{surface} attempt omitted its error omission reason")
        attempt_reason = attempt.get("error_omission_reason")
        if attempt_reason is not None and (
            not isinstance(attempt_reason, str)
            or attempt_reason not in POLL_ATTEMPT_ERROR_OMISSION_REASONS
        ):
            raise ValueError(f"{surface} attempt returned an unknown error omission reason")
        attempt_error = attempt.get("error")
        if attempt_error is not None and not isinstance(attempt_error, str):
            raise ValueError(f"{surface} attempt returned a non-string error")
        if attempt_reason is not None and attempt_error is not None:
            raise ValueError(f"{surface} attempt mixed an error with its omission reason")


def _build_ray_process_surface_probe(
    forbidden_values: Sequence[str],
    *,
    proc_root: str | Path = "/proc",
) -> str:
    """Build the private Ray argv probe with per-process exit-race tolerance."""

    encoded = base64.urlsafe_b64encode(
        json.dumps(
            [value for value in forbidden_values if value],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    return f"""
import base64
import json
import os
import pathlib

values = json.loads(base64.urlsafe_b64decode({encoded!r}))
me = os.getpid()
exposed = False
for path in pathlib.Path({str(proc_root)!r}).glob("[0-9]*/cmdline"):
    if int(path.parent.name) == me:
        continue
    try:
        command_line = path.read_bytes()
    except OSError:
        continue
    if any(value.encode() in command_line for value in values):
        exposed = True
        break
print(json.dumps({{"clear": not exposed}}))
""".strip()


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
        self.released_v040_source_context: Path | None = None
        self.kubeconfig_path: Path | None = None
        self._kubeconfig_digest: str | None = None
        self._kubernetes_server: str | None = None
        self._docker_host: str | None = None
        self.mutated = False
        self._api_token: str | None = None
        self._secret_data_sha256: str | None = None
        self._runtime_env_protected_values = [
            RUNTIME_ENV_STORAGE_PROBE_MARKER,
            RUNTIME_ENV_FAILURE_UNKNOWN_KEY_ID,
        ]
        self._runtime_env_fixture_values_registered = False
        self._bounded_poll_projection_families: set[str] = set()
        self.redactor.register(RUNTIME_ENV_STORAGE_PROBE_MARKER)
        self.redactor.register(RUNTIME_ENV_FAILURE_UNKNOWN_KEY_ID)
        if self.runner.redactor is not self.redactor:
            self.runner.redactor.register(RUNTIME_ENV_STORAGE_PROBE_MARKER)
            self.runner.redactor.register(RUNTIME_ENV_FAILURE_UNKNOWN_KEY_ID)
        self._ray_cluster_uid: str | None = None
        self._ray_pod_identities: frozenset[PodRuntimeIdentity] | None = None
        self._ray_image_python_version: str | None = None
        self.diagnostics_attempted = False
        self.rendered_ray_topology: RayTopology | None = None
        self.setup_pod_images: PodImageContract | None = None
        self.deployment_contracts: dict[str, DeploymentContract] = {}
        self._released_v040_worker_id: str | None = None
        self._released_v040_hostname: str | None = None
        self._protocol_v1_survival_fixture: Mapping[str, Any] | None = None
        self._protocol_v2_fixture: Mapping[str, Any] | None = None
        self._protocol_handoff_initial_revision: int | None = None
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
        parsed = _parse_json_without_cause(
            payload,
            error_message="private kubeconfig snapshot is no longer valid JSON",
        )
        server = inspect_kubeconfig_snapshot(parsed, expected_context=self.config.context)
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
        payload = _parse_json_without_cause(
            result.stdout,
            error_message=f"{field_name} is not valid JSON",
        )
        return _mapping(payload, field_name=field_name)

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
        payload = _parse_json_without_cause(
            result.stdout,
            error_message="flattened kubeconfig snapshot is not valid JSON",
        )
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

    def _verify_released_v040_source_identity(self) -> None:
        """Pin v0.4.0 to its reviewed commit/tree and verify an optional local tag."""

        commit = self.runner.run(
            ["git", "rev-parse", "--verify", f"{RELEASED_V040_COMMIT}^{{commit}}"],
            cwd=self.config.root,
        ).stdout.strip()
        if commit != RELEASED_V040_COMMIT:
            raise ValueError("the pinned v0.4.0 release commit no longer resolves exactly")
        source_tree = self.runner.run(
            ["git", "rev-parse", "--verify", f"{commit}^{{tree}}"],
            cwd=self.config.root,
        ).stdout.strip()
        if source_tree != RELEASED_V040_SOURCE_TREE:
            raise ValueError(
                "the pinned v0.4.0 commit no longer resolves to its reviewed source tree"
            )

        tag_ref = f"refs/tags/{RELEASED_V040_TAG}"
        tag_presence = self.runner.run(
            ["git", "show-ref", "--verify", "--quiet", tag_ref],
            cwd=self.config.root,
            check=False,
        )
        if tag_presence.returncode == 1:
            return
        if tag_presence.returncode != 0:
            raise ValueError(f"could not verify the optional local {RELEASED_V040_TAG} tag ref")
        object_type = self.runner.run(
            ["git", "cat-file", "-t", tag_ref], cwd=self.config.root
        ).stdout.strip()
        if object_type != "tag":
            raise ValueError(f"{RELEASED_V040_TAG} must resolve through an annotated tag object")
        tag_commit = self.runner.run(
            ["git", "rev-parse", "--verify", f"{tag_ref}^{{commit}}"],
            cwd=self.config.root,
        ).stdout.strip()
        if tag_commit != RELEASED_V040_COMMIT:
            raise ValueError(f"{RELEASED_V040_TAG} no longer resolves to the pinned release commit")

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
                    self._layer(
                        "protocol-handoff-recovery",
                        self._recover_protocol_handoff_residue,
                    )
                    self._layer("runtime-env", self._verify_generic_ray_nodes)
                    self._layer("probes", self._verify_probes)
                    self._layer("api-smoke", self._verify_api)
                    self._layer(
                        "ray-job-request-reference",
                        self._verify_ray_job_request_reference,
                    )
                    self._layer(
                        "protocol-handoff",
                        self._verify_protocol_handoff_certification,
                    )
                    self._layer(
                        "runtime-env-encryption",
                        self._verify_runtime_env_encryption,
                    )
                    self._layer("workflow-progress", self._verify_workflow_progress)
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
        self.evidence.released_v040_image_tag = (
            f"{RELEASED_V040_IMAGE_REPOSITORY}:released-v040-{tag}"
        )
        self._verify_released_v040_source_identity()

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
        released_root = self.temp_root / "released-v040"
        released_root.mkdir()
        self.released_v040_source_context = create_source_build_context(
            runner=self.runner,
            root=self.config.root,
            temporary_root=released_root,
            commit=RELEASED_V040_COMMIT,
            source_tree=RELEASED_V040_SOURCE_TREE,
        )
        overlay = configure_overlay_copy(
            source_k8s=self.source_context / "k8s",
            destination_k8s=self.temp_root / "k8s",
            tag=tag,
        )
        rendered = self._kubectl_cluster("kustomize", str(overlay)).stdout
        resources = load_rendered_resources(rendered)
        inspect_rendered_resources(resources, namespace=self.config.namespace, tag=tag)
        inspect_runtime_env_encryption_overlay(resources)
        inspect_ray_job_request_storage_overlay(resources)
        self.evidence.runtime_env_encryption_overlay = True
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

    def _build_released_v040_image(self) -> None:
        """Build the exact released v0.4.0 application image from its pinned archive."""

        if self.released_v040_source_context is None:
            raise ValueError("released v0.4.0 source archive has not been initialized")
        if self._ray_image_python_version is None:
            raise ValueError("rendered Ray image Python version has not been discovered")
        self._verify_released_v040_source_identity()
        command = [
            "docker",
            "build",
            "--tag",
            self.evidence.released_v040_image_tag,
            "--build-arg",
            f"PYTHON_VERSION={self._ray_image_python_version}",
            "--label",
            f"org.opencontainers.image.revision={RELEASED_V040_COMMIT}",
            "--label",
            f"org.opencontainers.image.source-tree={RELEASED_V040_SOURCE_TREE}",
            "--file",
            str(self.released_v040_source_context / "Dockerfile"),
            str(self.released_v040_source_context),
        ]
        self._docker(*command[1:], timeout=self.config.build_timeout)
        self.evidence.released_v040_image_id = parse_docker_image_inspect(
            self._docker("image", "inspect", self.evidence.released_v040_image_tag).stdout,
            expected_tag=self.evidence.released_v040_image_tag,
            commit=RELEASED_V040_COMMIT,
            source_tree=RELEASED_V040_SOURCE_TREE,
        )
        if (
            self._docker_image_python_version(self.evidence.released_v040_image_tag)
            != self._ray_image_python_version
        ):
            raise ValueError("released v0.4.0 image Python version does not match the Ray image")

    def _docker_image_python_version(self, image: str) -> str:
        """Read one image's interpreter tuple without network or writable state."""

        result = self._docker(
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--entrypoint",
            "python",
            "--",
            image,
            "-I",
            "-S",
            "-B",
            "-c",
            'import sys; print(".".join(map(str, sys.version_info[:3])))',
            timeout=self.config.build_timeout,
        )
        return parse_ray_image_python_version(result.stdout)

    def _discover_ray_image_python_version(self) -> str:
        """Read the exact Python patch from the rendered Ray runtime image."""

        if self.rendered_ray_topology is None:
            raise ValueError("rendered Ray topology was not captured in preflight")
        image = ray_runtime_image_reference(self.rendered_ray_topology)
        version = self._docker_image_python_version(image)
        self._ray_image_python_version = version
        return version

    def _build_images(self) -> None:
        if self.source_context is None:
            raise ValueError("immutable source archive has not been initialized")
        self._verify_source_identity()
        ray_image_python_version = self._discover_ray_image_python_version()
        context = self.source_context
        labels = (
            f"org.opencontainers.image.revision={self.evidence.commit}",
            f"org.opencontainers.image.source-tree={self.evidence.source_tree}",
        )
        builds = (
            (self.evidence.app_tag, context / "Dockerfile", True),
            (self.evidence.worker_tag, context / "Dockerfile.ray", False),
        )
        for tag, dockerfile, align_with_ray in builds:
            command = ["docker", "build", "--tag", tag]
            if align_with_ray:
                command.extend(["--build-arg", f"PYTHON_VERSION={ray_image_python_version}"])
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
        if self._docker_image_python_version(self.evidence.app_tag) != ray_image_python_version:
            raise ValueError("application image Python version does not match the Ray image")
        self._build_released_v040_image()
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
                    self.evidence.released_v040_image_tag,
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
            f"r={RECOVERY_RUNTIME_ENV_ARCHIVE!r};"
            "data=open(p,'rb').read();"
            "recovery_data=open(r,'rb').read();"
            "z=zipfile.ZipFile(p);"
            "recovery_zip=zipfile.ZipFile(r);"
            "print(json.dumps({"
            "'django_ray':'absent' if importlib.util.find_spec('django_ray') is None else 'present',"
            "'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),"
            f"'required_member':{RUNTIME_ENV_REQUIRED_MEMBER!r} in z.namelist(),"
            "'recovery_bytes':len(recovery_data),"
            "'recovery_sha256':hashlib.sha256(recovery_data).hexdigest(),"
            "'recovery_required_members':all(member in recovery_zip.namelist() for member in "
            f"{RECOVERY_RUNTIME_ENV_REQUIRED_MEMBERS!r})"
            "}))"
        )
        observed: set[tuple[int, str, int, str]] = set()
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
        bundle_bytes, bundle_digest, recovery_bytes, recovery_digest = observed.pop()
        self.evidence.setup_bundle_bytes = bundle_bytes
        self.evidence.setup_bundle_sha256 = bundle_digest
        self.evidence.recovery_bundle_bytes = recovery_bytes
        self.evidence.recovery_bundle_sha256 = recovery_digest

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

    def _secret_data(self) -> Mapping[str, Any]:
        """Read the preserved Secret through a suppressed-output command."""
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
        return _mapping(secret.get("data"), field_name="Secret/django-ray-secret data")

    def _secret_token(self) -> str:
        if self._api_token is not None:
            return self._api_token
        data = self._secret_data()
        inspect_runtime_env_encryption_secret_data(data)
        digest = secret_data_sha256(data)
        if self._secret_data_sha256 is None:
            self._secret_data_sha256 = digest
        encoded = data.get("DJANGO_API_TOKEN")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Secret/django-ray-secret has no DJANGO_API_TOKEN")
        token: str | None = None
        try:
            token = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
        if token is None:
            # Raise outside the except block so invalid decoded bytes are not
            # retained by UnicodeDecodeError.object in the exception graph.
            raise ValueError("DJANGO_API_TOKEN is not valid base64-encoded UTF-8")
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

    def _verify_preserved_secret(self) -> None:
        """Compare all Secret data to the preflight identity without emitting its digest."""
        if self._secret_data_sha256 is None:
            raise ValueError("Secret/django-ray-secret identity was not captured during preflight")
        current = secret_data_sha256(self._secret_data())
        if current != self._secret_data_sha256:
            raise ValueError("Secret/django-ray-secret data changed during the guarded gate")
        self.evidence.django_ray_secret_preserved = True

    def _http(
        self,
        path: str,
        *,
        method: str,
        headers: Mapping[str, str] | None = None,
        response_limit: int = MAX_HTTP_RESPONSE_BYTES,
        required_response_headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        url = build_local_http_request_url(base_url=self.config.web_url, path=path)
        request = Request(url, method=method, headers=dict(headers or {}))
        try:
            with self.http_opener.open(request, timeout=10) as response:
                if required_response_headers is not None:
                    response_headers = getattr(response, "headers", None)
                    if not hasattr(response_headers, "get"):
                        raise ValueError("local HTTP response did not expose headers")
                    for header_name, expected_value in required_response_headers.items():
                        if response_headers.get(header_name) != expected_value:
                            raise ValueError(
                                f"local HTTP response returned an invalid {header_name} header"
                            )
                body = response.read(response_limit + 1)
                if len(body) > response_limit:
                    raise ValueError("local HTTP response exceeded its bounded read limit")
                return response.status, body
        except HTTPError as error:
            return error.code, error.read(response_limit)
        except URLError as error:
            raise ValueError(f"local HTTP request failed: {error.reason}") from error

    def _json_body(self, body: bytes, *, endpoint: str) -> Mapping[str, Any]:
        payload = _parse_json_without_cause(
            body,
            error_message=f"{endpoint} did not return valid JSON",
        )
        return _mapping(payload, field_name=f"{endpoint} response")

    def _observe_bounded_poll_projection(
        self,
        payload: Mapping[str, Any],
        *,
        family: str,
        surface: str,
        task_id: str,
    ) -> None:
        """Record one valid bounded Workflow or RuntimeEnv polling projection."""
        if family not in {"runtime_env", "workflow"}:
            raise ValueError("bounded poll projection family is invalid")
        validate_bounded_poll_projection(
            payload,
            surface=surface,
            task_id=task_id,
        )
        self._bounded_poll_projection_families.add(family)
        self.evidence.api_workflow_runtime_polls_bounded = (
            self._bounded_poll_projection_families == {"runtime_env", "workflow"}
        )

    def _verify_api(self) -> None:
        unauthenticated = (
            ("/api/enqueue/add/2/3", "POST"),
            ("/api/executions/stats", "GET"),
            ("/api/metrics", "GET"),
            ("/api/executions?limit=1", "GET"),
            ("/api/tasks/00000000-0000-4000-8000-000000000000", "GET"),
        )
        for endpoint, method in unauthenticated:
            status, _ = self._http(endpoint, method=method)
            if status != 401:
                raise ValueError(f"unauthenticated {endpoint} returned {status}, expected 401")

        status, body = self._http(
            "/api/openapi.json",
            method="GET",
            response_limit=MAX_OPENAPI_SCHEMA_BYTES,
        )
        if status != 200:
            raise ValueError(f"OpenAPI schema returned {status}, expected 200")
        schema = self._json_body(body, endpoint="OpenAPI schema")
        paths = _mapping(schema.get("paths"), field_name="OpenAPI paths")
        execution_path = _mapping(
            paths.get("/api/executions/{execution_id}"),
            field_name="OpenAPI execution detail path",
        )
        if "get" not in execution_path:
            raise ValueError("OpenAPI execution detail path does not advertise GET")
        if "delete" in execution_path:
            raise ValueError("OpenAPI execution detail path advertises unsafe DELETE")
        if "/api/cluster/workflows/{task_id}/graph" in paths:
            raise ValueError("OpenAPI advertises the removed legacy workflow graph")
        self.evidence.api_legacy_workflow_graph_absent = True
        if "/api/executions/reset" in paths:
            raise ValueError("OpenAPI advertises the removed bulk execution reset")
        self.evidence.api_bulk_reset_absent = True
        if "/api/cluster/workflows/{task_id}/nodes/{node_id}" in paths:
            raise ValueError("OpenAPI advertises the removed legacy workflow node route")
        self.evidence.api_legacy_workflow_node_absent = True
        retry_path = _mapping(
            paths.get("/api/executions/{execution_id}/retry"),
            field_name="OpenAPI execution retry path",
        )
        if "post" not in retry_path:
            raise ValueError("OpenAPI execution retry path does not advertise POST")
        node_detail_path = _mapping(
            paths.get("/api/cluster/workflows/{task_id}/node-detail"),
            field_name="OpenAPI workflow node-detail path",
        )
        if "get" not in node_detail_path:
            raise ValueError("OpenAPI workflow node-detail path does not advertise GET")

        token = self._secret_token()
        headers = {"Authorization": f"Bearer {token}"}
        for endpoint in ("/api/executions/stats", "/api/executions?limit=1"):
            status, _ = self._http(endpoint, method="GET", headers=headers)
            if status != 200:
                raise ValueError(f"authenticated {endpoint} returned {status}, expected 200")
        status, body = self._http("/api/metrics", method="GET", headers=headers)
        if status != 200:
            raise ValueError(f"authenticated /api/metrics returned {status}, expected 200")
        validate_execution_protocol_metrics(body)

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
        while True:
            status, body = self._http(
                f"/api/tasks/{task_id}",
                method="GET",
                headers=headers,
                response_limit=EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
                required_response_headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
            if status != 200:
                raise ValueError(f"task status polling returned {status}, expected 200")
            task_status = self._json_body(body, endpoint="task status polling")
            last_state = validate_task_status_payload(task_status, task_id=task_id)
            validate_execution_protocol_visibility(
                task_status,
                surface="task status polling",
            )
            if task_status.get("input_omission_reason") is not None:
                raise ValueError("add_numbers task status unexpectedly omitted inline input")
            if task_status.get("args") != [2, 3] or task_status.get("kwargs") != {}:
                raise ValueError("add_numbers task status changed its exact inline input")
            if last_state == "SUCCEEDED":
                self.evidence.api_task_status_bounded = True
                break
            if last_state in TASK_FAILURE_STATES:
                raise ValueError(f"add_numbers reached terminal state {last_state}")
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"add_numbers did not reach SUCCEEDED within {self.config.task_timeout}s "
                    f"(last state: {last_state})"
                )
            time.sleep(2)

        execution_query = urlencode({"task_id": task_id, "limit": 1})
        status, body = self._http(
            f"/api/executions?{execution_query}", method="GET", headers=headers
        )
        if status != 200:
            raise ValueError(f"execution lookup returned {status}, expected 200")
        listing = self._json_body(body, endpoint="execution lookup")
        tasks = _sequence(listing.get("tasks"), field_name="execution lookup tasks")
        execution = next(
            (
                _mapping(value, field_name="execution")
                for value in tasks
                if isinstance(value, Mapping) and value.get("task_id") == task_id
            ),
            None,
        )
        if execution is None or execution.get("state") != "SUCCEEDED":
            raise ValueError("add_numbers durable execution lookup did not return SUCCEEDED")
        validate_execution_protocol_visibility(
            execution,
            surface="execution lookup",
        )
        execution_id = execution.get("id")
        if not isinstance(execution_id, int) or isinstance(execution_id, bool) or execution_id < 1:
            raise ValueError("add_numbers execution has no positive integer id")
        result = parse_task_result(execution.get("result_data"))
        if result != 5:
            raise ValueError(f"add_numbers durable result is {result!r}, expected 5")
        detail_path = f"/api/executions/{execution_id}"
        status, _ = self._http(detail_path, method="DELETE", headers=headers)
        if status != 405:
            raise ValueError(f"authenticated execution DELETE returned {status}, expected 405")
        status, body = self._http(detail_path, method="GET", headers=headers)
        if status != 200:
            raise ValueError(
                f"execution detail after rejected DELETE returned {status}, expected 200"
            )
        detail = self._json_body(body, endpoint="execution detail")
        validate_execution_protocol_visibility(
            detail,
            surface="execution detail",
        )
        detail_result = parse_task_result(detail.get("result_data"))
        if (
            detail.get("id") != execution_id
            or detail.get("task_id") != task_id
            or detail.get("state") != "SUCCEEDED"
            or detail_result != 5
            or detail.get("result_data_omission_reason") is not None
            or detail.get("error_message_omission_reason") is not None
            or detail.get("diagnostic_max_bytes") != EXPECTED_EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
            or detail.get("response_max_bytes") != EXPECTED_EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        ):
            raise ValueError("execution detail changed or lost its bounded projection contract")
        self.evidence.task_state = last_state
        self.evidence.task_result = result
        self.evidence.api_execution_delete_rejected = True

    @staticmethod
    def _ray_job_gate_status(value: object, *, field_name: str) -> str:
        if not isinstance(value, str) or value not in {
            "PENDING",
            "RUNNING",
            *RAY_JOB_GATE_TERMINAL_STATES,
        }:
            raise ValueError(f"{field_name} has an invalid Ray Job state")
        return value

    def _register_ray_job_gate_value(self, value: str) -> None:
        """Keep one fixture-only value out of diagnostics and evidence."""

        if not value:
            return
        self.redactor.register(value)
        if self.runner.redactor is not self.redactor:
            self.runner.redactor.register(value)

    def _enqueue_ray_job_gate_task(self) -> str:
        script = f"""
import json

from testproject.tasks import slow_task

result = slow_task.using(
    backend={RAY_JOB_GATE_QUEUE!r},
    queue_name={RAY_JOB_GATE_QUEUE!r},
).enqueue(seconds={RAY_JOB_GATE_SECONDS!r})
print(json.dumps({{"task_id": result.id}}, sort_keys=True, separators=(",", ":")))
""".strip()
        payload = self._sensitive_django_shell(
            script,
            field_name="rq2 positive task enqueue",
        )
        task_id = self._canonical_uuid4(
            payload.get("task_id"),
            field_name="rq2 positive task id",
        )
        self._register_ray_job_gate_value(task_id)
        self._register_ray_job_gate_value(RAY_JOB_GATE_CALLABLE)
        self._register_ray_job_gate_value(str(RAY_JOB_GATE_SECONDS))
        return task_id

    def _observe_ray_job_gate_task(self, task_id: str) -> Mapping[str, Any]:
        script = f"""
import json
import os
import shlex

from django_ray.execution_codec import ExecutionIdentity
from django_ray.models import RayTaskExecution
from django_ray.ray_job_protocol import (
    RayJobRequestReferenceExpectation,
    parse_ray_job_request_metadata,
    validate_ray_job_request_reference_expectation,
)
from django_ray.ray_job_request_storage import (
    decode_ray_job_request_locator,
    load_ray_job_request,
    ray_job_request_reference_content_identity,
)
from django_ray.runner.ray_job import _address_pinned_job_client

row = RayTaskExecution.objects.get(task_id={task_id!r})
if not row.ray_job_id or not row.ray_job_request_reference or not row.ray_address:
    print(json.dumps({{"ready": False, "state": str(row.state)}}))
else:
    client = _address_pinned_job_client(row.ray_address)
    info = client.get_job_info(row.ray_job_id)
    entrypoint = getattr(info, "entrypoint", "")
    parts = shlex.split(entrypoint)
    carrier_ok = (
        len(parts) == 5
        and parts[:4] == ["python", "-m", "django_ray.runtime.entrypoint", {RAY_JOB_REQUEST_REFERENCE_CARRIER!r}]
        and {RAY_JOB_RELEASED_PAYLOAD_CARRIER!r} not in parts
    )
    locator = decode_ray_job_request_locator(parts[4]) if carrier_ok else None
    loaded = load_ray_job_request(locator) if locator is not None else None
    expectation = parse_ray_job_request_metadata(getattr(info, "metadata", None), required=True)
    request_digest, request_size = ray_job_request_reference_content_identity(
        row.ray_job_request_reference
    )
    identity = ExecutionIdentity(
        task_execution_pk=row.pk,
        task_id=row.task_id,
        attempt_number=row.attempt_number,
        execution_generation=row.execution_generation,
    )
    binding_ok = isinstance(expectation, RayJobRequestReferenceExpectation)
    if binding_ok:
        validate_ray_job_request_reference_expectation(
            expectation,
            expected_identity=identity,
            expected_execution_protocol_version=row.execution_protocol_version,
            expected_request_sha256=request_digest,
            expected_request_size_bytes=request_size,
            expected_submission_id=row.ray_job_id,
            serialized_request=loaded.serialized_request if loaded is not None else None,
            request_reference=row.ray_job_request_reference,
            request_locator=parts[4] if carrier_ok else None,
        )
    binding_ok = binding_ok and carrier_ok
    request_ok = (
        loaded is not None
        and loaded.reference == row.ray_job_request_reference
        and loaded.request.identity == identity
        and loaded.request.callable_path == row.callable_path
    )
    if hasattr(info, "model_dump"):
        info_payload = info.model_dump()
    elif hasattr(info, "dict"):
        info_payload = info.dict()
    else:
        info_payload = vars(info)
    raw_info = json.dumps(info_payload, sort_keys=True, separators=(",", ":"), default=str)
    raw_logs = str(client.get_job_logs(row.ray_job_id) or "")
    credential_values = [
        value
        for key, value in os.environ.items()
        if value
        and any(token in key.upper() for token in ("PASSWORD", "SECRET", "TOKEN", "API_KEY"))
    ]
    forbidden = [
        row.task_id,
        row.callable_path,
        row.args_json,
        row.kwargs_json,
        {str(RAY_JOB_GATE_SECONDS)!r},
        loaded.serialized_request if loaded is not None else "",
        *credential_values,
    ]
    forbidden = [value for value in forbidden if value]
    info_clear = all(value not in raw_info for value in forbidden)
    logs_clear = all(value not in raw_logs for value in forbidden)
    submissions = [
        job
        for job in client.list_jobs()
        if getattr(job, "submission_id", None) == row.ray_job_id
    ]
    print(json.dumps({{
        "ready": True,
        "state": str(getattr(info, "status", "")),
        "durable_state": str(row.state),
        "attempt_number": row.attempt_number,
        "execution_generation": row.execution_generation,
        "worker_id": row.claimed_by_worker,
        "job_id": row.ray_job_id,
        "carrier_ok": carrier_ok,
        "binding_ok": binding_ok,
        "request_ok": request_ok,
        "info_clear": info_clear,
        "logs_clear": logs_clear,
        "submission_count": len(submissions),
    }}, sort_keys=True, separators=(",", ":"), default=str))
""".strip()
        return self._sensitive_django_shell(
            script,
            field_name="rq2 positive task observation",
        )

    def _wait_for_ray_job_gate_task(
        self,
        task_id: str,
        *,
        accepted_states: frozenset[str],
        different_worker: str | None = None,
        durable_state: str | None = None,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.config.task_timeout
        last_state = "not submitted"
        while True:
            observation = self._observe_ray_job_gate_task(task_id)
            state_value = observation.get("state")
            if observation.get("ready") is True:
                state = self._ray_job_gate_status(
                    state_value,
                    field_name="rq2 positive Ray Job",
                )
                last_state = state
                if state in RAY_JOB_GATE_TERMINAL_STATES - accepted_states:
                    raise ValueError(f"rq2 positive Ray Job reached unexpected state {state}")
                worker = observation.get("worker_id")
                worker_changed = different_worker is None or (
                    isinstance(worker, str) and worker and worker != different_worker
                )
                durable_ready = (
                    durable_state is None or observation.get("durable_state") == durable_state
                )
                if state in accepted_states and worker_changed and durable_ready:
                    return observation
            elif isinstance(state_value, str):
                last_state = state_value
            if time.monotonic() >= deadline:
                raise ValueError(
                    "rq2 positive Ray Job did not reach the required state within "
                    f"{self.config.task_timeout}s (last state: {last_state})"
                )
            time.sleep(2)

    def _ray_process_surfaces_clear(self, forbidden_values: Sequence[str]) -> bool:
        probe_code = _build_ray_process_surface_probe(forbidden_values)
        if self._ray_cluster_uid is None:
            raise ValueError("RayCluster UID was not pinned before rq2 process inspection")
        _, ray_pods = self._ray_pods(expected_cluster_uid=self._ray_cluster_uid)
        for pod in ray_pods:
            metadata = _metadata(pod)
            labels = _mapping(metadata.get("labels"), field_name="Ray pod labels")
            name = str(metadata.get("name"))
            container = "ray-head" if labels.get("component") == "head" else "ray-worker"
            result = self._kubectl(
                "exec",
                name,
                "-c",
                container,
                "--",
                "python",
                "-c",
                probe_code,
                sensitive_output=True,
            )
            payload = _parse_json_without_cause(
                result.stdout,
                error_message="rq2 Ray process probe did not return valid private JSON",
            )
            if _mapping(payload, field_name="rq2 Ray process probe").get("clear") is not True:
                return False
        return True

    def _decoded_credential_values(self) -> tuple[str, ...]:
        values: list[str] = []
        for key, encoded in self._secret_data().items():
            if not isinstance(key, str) or not any(
                token in key.upper() for token in ("PASSWORD", "SECRET", "TOKEN", "API_KEY")
            ):
                continue
            if not isinstance(encoded, str):
                raise ValueError("Secret/django-ray-secret contains a non-string data value")
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise ValueError(
                    "Secret/django-ray-secret contains invalid credential data"
                ) from None
            if decoded:
                self._register_ray_job_gate_value(decoded)
                values.append(decoded)
        return tuple(values)

    def _restart_ray_job_manager(self) -> None:
        self._kubectl(
            "rollout",
            "restart",
            f"deployment/{RAY_JOB_MANAGER_DEPLOYMENT}",
        )
        self._kubectl(
            "rollout",
            "status",
            f"deployment/{RAY_JOB_MANAGER_DEPLOYMENT}",
            f"--timeout={self.config.rollout_timeout}s",
            timeout=self._rollout_command_timeout(),
        )
        self._wait_for_application_topology()

    def _verify_ray_job_request_reference(self) -> None:
        """Exercise rq2 across the live Jobs API and a manager replacement."""

        task_id = self._enqueue_ray_job_gate_task()
        running = self._wait_for_ray_job_gate_task(
            task_id,
            accepted_states=frozenset({"RUNNING"}),
        )
        for field_name in ("job_id", "worker_id"):
            if not isinstance(running.get(field_name), str) or not running[field_name]:
                raise ValueError(f"rq2 running observation has no {field_name}")
        if (
            running.get("carrier_ok") is not True
            or running.get("binding_ok") is not True
            or running.get("request_ok") is not True
            or running.get("info_clear") is not True
            or running.get("logs_clear") is not True
            or running.get("submission_count") != 1
            or running.get("attempt_number") != 1
        ):
            raise ValueError("rq2 running observation failed its strict request contract")

        credential_values = self._decoded_credential_values()
        process_values = (
            task_id,
            RAY_JOB_GATE_CALLABLE,
            str(RAY_JOB_GATE_SECONDS),
            RAY_JOB_RELEASED_PAYLOAD_CARRIER,
            *credential_values,
        )
        if not self._ray_process_surfaces_clear(process_values):
            raise ValueError("rq2 Ray process argv exposed an application or credential value")
        self.evidence.ray_job_processes_clear = True

        self._restart_ray_job_manager()
        reconciled = self._wait_for_ray_job_gate_task(
            task_id,
            accepted_states=frozenset({"RUNNING", "SUCCEEDED"}),
            different_worker=str(running["worker_id"]),
        )
        if (
            reconciled.get("job_id") != running["job_id"]
            or reconciled.get("attempt_number") != 1
            or reconciled.get("execution_generation") != running.get("execution_generation")
            or reconciled.get("submission_count") != 1
        ):
            raise ValueError("rq2 manager replacement did not retain the exact submitted job")
        self.evidence.ray_job_manager_reconciled_same_job = True

        terminal = self._wait_for_ray_job_gate_task(
            task_id,
            accepted_states=frozenset({"SUCCEEDED"}),
            durable_state="SUCCEEDED",
        )
        if (
            terminal.get("durable_state") != "SUCCEEDED"
            or terminal.get("carrier_ok") is not True
            or terminal.get("binding_ok") is not True
            or terminal.get("request_ok") is not True
            or terminal.get("info_clear") is not True
            or terminal.get("logs_clear") is not True
            or terminal.get("submission_count") != 1
        ):
            raise ValueError("rq2 terminal observation lost its strict request contract")
        self.evidence.ray_job_request_reference_carrier = True
        self.evidence.ray_job_raw_info_clear = True
        self.evidence.ray_job_logs_clear = True

        self._verify_missing_ray_job_request_reference()

    def _scale_ray_job_manager(self, replicas: int) -> None:
        if replicas not in {0, 1}:
            raise ValueError("rq2 gate manager replicas must be zero or one")
        self._kubectl(
            "scale",
            f"deployment/{RAY_JOB_MANAGER_DEPLOYMENT}",
            f"--replicas={replicas}",
        )
        self._kubectl(
            "rollout",
            "status",
            f"deployment/{RAY_JOB_MANAGER_DEPLOYMENT}",
            f"--timeout={self.config.rollout_timeout}s",
            timeout=self._rollout_command_timeout(),
        )

    def _ray_job_manager_replica_observation(self) -> Mapping[str, Any]:
        """Return the exact current Ray Job manager replica counts."""

        deployment = self._json_command(
            self._kubectl("get", "deployment", RAY_JOB_MANAGER_DEPLOYMENT, "-o", "json"),
            field_name=f"Deployment/{RAY_JOB_MANAGER_DEPLOYMENT}",
        )
        spec = _mapping(deployment.get("spec"), field_name="Ray Job manager spec")
        status = _mapping(deployment.get("status"), field_name="Ray Job manager status")
        replicas = spec.get("replicas", 1)
        ready_replicas = status.get("readyReplicas", 0)
        if (
            type(replicas) is not int
            or replicas not in {0, 1}
            or type(ready_replicas) is not int
            or ready_replicas not in {0, 1}
            or ready_replicas > replicas
        ):
            raise ValueError("Ray Job manager returned invalid replica counts")
        return {"replicas": replicas, "ready_replicas": ready_replicas}

    def _wait_for_protocol_cohorts(self) -> Mapping[str, Any]:
        """Wait for one genuine released cohort beside current explicit readers."""

        deadline = time.monotonic() + self.config.task_timeout
        while True:
            observation = self._observe_protocol_cohorts()
            report = _mapping(
                observation.get("report"),
                field_name="protocol cohort report",
            )
            policy = _mapping(report.get("policy"), field_name="protocol cohort policy")
            policy_active_write = policy.get("active_write_protocol_version")
            capabilities = _mapping(
                report.get("capabilities"),
                field_name="protocol cohort capabilities",
            )
            groups = [
                dict(_mapping(value, field_name="protocol capability group"))
                for value in _sequence(
                    capabilities.get("groups"),
                    field_name="protocol capability groups",
                )
            ]
            legacy_groups = [
                group
                for group in groups
                if group.get("kind") == "legacy"
                and type(group.get("minimum")) is int
                and group.get("minimum") == 1
                and type(group.get("maximum")) is int
                and group.get("maximum") == 1
                and type(group.get("heartbeat_live_leases")) is int
                and group.get("heartbeat_live_leases") == 1
            ]
            explicit_groups = [
                group
                for group in groups
                if group.get("kind") == "explicit"
                and type(group.get("minimum")) is int
                and group.get("minimum") == 1
                and type(group.get("maximum")) is int
                and group.get("maximum") == 1
                and type(group.get("heartbeat_live_leases")) is int
                and group.get("heartbeat_live_leases", 0) >= 1
            ]
            worker_ids = observation.get("legacy_worker_ids")
            legacy_worker_count = observation.get("legacy_worker_count")
            omitted_groups = capabilities.get("omitted_groups")
            omitted_leases = capabilities.get("omitted_leases")
            ready = (
                policy.get("legacy_worker_admission_enabled") is True
                and policy.get("legacy_admission_token_present") is True
                and type(policy_active_write) is int
                and policy_active_write == 1
                and type(legacy_worker_count) is int
                and legacy_worker_count == 1
                and isinstance(worker_ids, list)
                and len(worker_ids) == 1
                and len(legacy_groups) == 1
                and len(explicit_groups) == 1
                and type(omitted_groups) is int
                and omitted_groups == 0
                and type(omitted_leases) is int
                and omitted_leases == 0
            )
            if ready:
                worker_id = self._canonical_uuid4(
                    worker_ids[0],
                    field_name="released v0.4.0 worker id",
                )
                self._released_v040_worker_id = worker_id
                self._register_ray_job_gate_value(worker_id)
                self.evidence.protocol_legacy_cohort_visible = True
                self.evidence.protocol_explicit_cohort_visible = True
                return observation
            if type(legacy_worker_count) is not int or legacy_worker_count not in {0, 1}:
                raise ValueError("unexpected live legacy worker cohort appeared")
            if time.monotonic() >= deadline:
                raise ValueError("released and current protocol cohorts did not become visible")
            time.sleep(2)

    @staticmethod
    def _released_v040_manager_labels() -> dict[str, str]:
        """Return the reserved selector shared only by the transient manager."""

        return {
            "app": "django-ray",
            "component": "worker",
            "queues": RAY_JOB_GATE_QUEUE,
            "runner": "ray-job-v040",
        }

    def _released_v040_manager_manifest(
        self,
        *,
        image_tag: str | None = None,
        hostname: str | None = None,
    ) -> dict[str, Any]:
        """Return the one gate-owned legacy Ray Job manager Deployment."""

        selected_image_tag = image_tag or self.evidence.released_v040_image_tag
        if not selected_image_tag:
            raise ValueError("released v0.4.0 image tag was not captured")
        selected_hostname = hostname
        if selected_hostname is None and self._released_v040_hostname is None:
            self._released_v040_hostname = f"dr-v040-{uuid4().hex}"
            self._register_ray_job_gate_value(self._released_v040_hostname)
        if selected_hostname is None:
            selected_hostname = self._released_v040_hostname
        if (
            not isinstance(selected_hostname, str)
            or RELEASED_V040_HOSTNAME_PATTERN.fullmatch(selected_hostname) is None
        ):
            raise ValueError("released v0.4.0 gate hostname is invalid")
        labels = self._released_v040_manager_labels()
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": RELEASED_V040_MANAGER_DEPLOYMENT,
                "namespace": self.config.namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "hostname": selected_hostname,
                        "initContainers": [
                            {
                                "name": "wait-for-postgres",
                                "image": "busybox:1.36",
                                "command": [
                                    "sh",
                                    "-c",
                                    "until nc -z postgres-svc 5432; do sleep 2; done;",
                                ],
                            },
                            {
                                "name": "wait-for-ray-jobs",
                                "image": "busybox:1.36",
                                "command": [
                                    "sh",
                                    "-c",
                                    "until nc -z ray-head-svc 8265; do sleep 2; done;",
                                ],
                            },
                            {
                                "name": "wait-for-runtime-env",
                                "image": "busybox:1.36",
                                "command": [
                                    "sh",
                                    "-c",
                                    f"until test -f {RUNTIME_ENV_ARCHIVE}; do sleep 2; done;",
                                ],
                                "volumeMounts": [
                                    {
                                        "name": "runtime-env",
                                        "mountPath": "/runtime-env",
                                        "readOnly": True,
                                    }
                                ],
                            },
                        ],
                        "containers": [
                            {
                                "name": RELEASED_V040_MANAGER_CONTAINER,
                                "image": selected_image_tag,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "python",
                                    "testproject/manage.py",
                                    "django_ray_worker",
                                ],
                                "args": [
                                    "--queue",
                                    RAY_JOB_GATE_QUEUE,
                                    "--concurrency",
                                    "1",
                                ],
                                "envFrom": [
                                    {"configMapRef": {"name": "django-ray-config"}},
                                    {"secretRef": {"name": "django-ray-secret"}},
                                ],
                                "env": [
                                    {
                                        "name": "RAY_ADDRESS",
                                        "value": "ray://ray-head-svc:10001",
                                    },
                                    *(
                                        {"name": name, "value": value}
                                        for name, value in RUNTIME_ENV_ENCRYPTION_ENV.items()
                                    ),
                                ],
                                "resources": {
                                    "requests": {"memory": "256Mi", "cpu": "100m"},
                                    "limits": {"memory": "512Mi", "cpu": "500m"},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "runtime-env",
                                        "mountPath": "/runtime-env",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": RAY_JOB_REQUEST_STORAGE_VOLUME,
                                        "mountPath": RAY_JOB_REQUEST_STORAGE_MOUNT_PATH,
                                    },
                                ],
                                "readinessProbe": {
                                    "exec": {
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "test -d /proc/1 && grep -q python /proc/1/cmdline",
                                        ]
                                    },
                                    "initialDelaySeconds": 30,
                                    "periodSeconds": 10,
                                    "timeoutSeconds": 5,
                                    "successThreshold": 1,
                                    "failureThreshold": 6,
                                },
                                "livenessProbe": {
                                    "exec": {
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "test -d /proc/1 && grep -q python /proc/1/cmdline",
                                        ]
                                    },
                                    "initialDelaySeconds": 30,
                                    "periodSeconds": 15,
                                    "timeoutSeconds": 5,
                                    "successThreshold": 1,
                                    "failureThreshold": 3,
                                },
                            }
                        ],
                        "volumes": [
                            {
                                "name": "runtime-env",
                                "persistentVolumeClaim": {"claimName": "runtime-env-pvc"},
                            },
                            {
                                "name": RAY_JOB_REQUEST_STORAGE_VOLUME,
                                "persistentVolumeClaim": {
                                    "claimName": RAY_JOB_REQUEST_STORAGE_CLAIM
                                },
                            },
                        ],
                    },
                },
            },
        }

    def _apply_released_v040_manager(self) -> None:
        """Create and verify the exact released legacy manager as a transient cohort."""

        if self.temp_root is None:
            raise ValueError("temporary workspace is unavailable")
        if not self.evidence.released_v040_image_id:
            raise ValueError("released v0.4.0 image identity was not verified")
        manifest = self._released_v040_manager_manifest()
        path = self.temp_root / "released-v040-manager.yaml"
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
        self._kubectl("create", "-f", str(path))
        self._kubectl(
            "rollout",
            "status",
            f"deployment/{RELEASED_V040_MANAGER_DEPLOYMENT}",
            f"--timeout={self.config.rollout_timeout}s",
            timeout=self._rollout_command_timeout(),
        )
        deployment = self._json_command(
            self._kubectl("get", "deployment", RELEASED_V040_MANAGER_DEPLOYMENT, "-o", "json"),
            field_name=f"Deployment/{RELEASED_V040_MANAGER_DEPLOYMENT}",
        )
        if _resource_identity(deployment) != (
            "apps/v1",
            "Deployment",
            RELEASED_V040_MANAGER_DEPLOYMENT,
        ):
            raise ValueError("released v0.4.0 manager creation returned the wrong Deployment")
        spec = _mapping(deployment.get("spec"), field_name="released v0.4.0 Deployment spec")
        status = _mapping(deployment.get("status"), field_name="released v0.4.0 Deployment status")
        if spec.get("replicas") != 1 or status.get("readyReplicas") != 1:
            raise ValueError("released v0.4.0 manager did not converge to exactly one replica")
        rendered_spec = _pod_spec(manifest)
        live_spec = _pod_spec(deployment)
        if rendered_spec is None or live_spec is None:
            raise ValueError("released v0.4.0 manager has no pod template")
        expected_contract = pod_image_contract(rendered_spec)
        if pod_image_contract(live_spec) != expected_contract:
            raise ValueError("released v0.4.0 manager image contract changed during creation")
        selector = self._selector(deployment)
        pods_payload = self._json_command(
            self._kubectl("get", "pods", "-l", selector, "-o", "json"),
            field_name="released v0.4.0 manager pod list",
        )
        pods = _sequence(pods_payload.get("items"), field_name="released v0.4.0 manager pods")
        if len(pods) != 1:
            raise ValueError("released v0.4.0 manager must own exactly one pod")
        pod = _mapping(pods[0], field_name="released v0.4.0 manager pod")
        inspect_pod_runtime_identity(
            pod,
            namespace=self.config.namespace,
            expected_contract=expected_contract,
            expected_source_tag=self.evidence.released_v040_image_tag,
            expected_source_id=self.evidence.released_v040_image_id,
            require_ready=True,
        )

    def _delete_released_v040_manager(self) -> None:
        """Delete only the transient released manager Deployment and verify absence."""

        existing_result = self._kubectl(
            "get",
            "deployment",
            RELEASED_V040_MANAGER_DEPLOYMENT,
            "--ignore-not-found",
            "-o",
            "json",
        )
        if existing_result.stdout.strip():
            deployment = self._json_command(
                existing_result,
                field_name="released v0.4.0 pre-delete Deployment",
            )
            retained_hostname = self._released_v040_hostname
            observed_hostname = self._validate_released_v040_recovery_deployment(deployment)
            if retained_hostname is not None and observed_hostname != retained_hostname:
                self._released_v040_hostname = retained_hostname
                raise ValueError("released v0.4.0 pre-delete Deployment hostname changed ownership")
            self._kubectl(
                "delete",
                "deployment",
                RELEASED_V040_MANAGER_DEPLOYMENT,
                "--ignore-not-found=true",
                "--wait=true",
                f"--timeout={self.config.rollout_timeout}s",
                timeout=self._rollout_command_timeout(),
            )
        remaining = self._kubectl(
            "get",
            "deployment",
            RELEASED_V040_MANAGER_DEPLOYMENT,
            "--ignore-not-found",
            "-o",
            "name",
        ).stdout.strip()
        if remaining:
            raise ValueError("released v0.4.0 manager Deployment remained after deletion")
        selector = ",".join(
            f"{key}={value}" for key, value in self._released_v040_manager_labels().items()
        )
        deadline = time.monotonic() + self.config.rollout_timeout
        while True:
            payload = self._json_command(
                self._kubectl(
                    "get",
                    "pods,replicasets",
                    "-l",
                    selector,
                    "-o",
                    "json",
                ),
                field_name="released v0.4.0 residual resources",
            )
            items = _sequence(
                payload.get("items"),
                field_name="released v0.4.0 residual resources",
            )
            if len(items) > MAX_APPLICATION_PODS + MAX_APPLICATION_REPLICASETS:
                raise ValueError("released v0.4.0 residual resource list was unbounded")
            if not items:
                break
            if time.monotonic() >= deadline:
                raise ValueError(
                    "released v0.4.0 manager pods or ReplicaSets remained after deletion"
                )
            time.sleep(2)

    def _observe_protocol_cohorts(self) -> Mapping[str, Any]:
        """Read one bounded status report plus private live legacy lease identity."""

        script = """
import json

from django_ray.models import TaskWorkerLease
from django_ray.protocol_status import build_protocol_status, protocol_status_to_dict
from django_ray.runner.leasing import get_lease_duration
from django.utils import timezone

report = protocol_status_to_dict(build_protocol_status())
cutoff = timezone.now() - get_lease_duration()
legacy_leases = TaskWorkerLease.objects.filter(
    capability_schema_version=0,
    is_active=True,
    last_heartbeat_at__gte=cutoff,
).order_by("worker_id")
legacy_worker_count = legacy_leases.count()
legacy_worker_ids = list(legacy_leases.values_list("worker_id", flat=True)[:2])
print(json.dumps({
    "report": report,
    "legacy_worker_count": legacy_worker_count,
    "legacy_worker_ids": legacy_worker_ids,
}, sort_keys=True, separators=(",", ":")))
""".strip()
        return self._sensitive_django_shell(script, field_name="protocol cohort observation")

    def _enqueue_released_v040_handoff_task(self) -> str:
        """Enqueue one marker-free protocol-v1 task for the released manager."""

        return self._enqueue_ray_job_gate_task()

    def _enqueue_protocol_v1_survival_task(self) -> Mapping[str, Any]:
        """Enqueue one deferred protocol-v1 row while the released manager is busy."""

        marker = "protocol_v1_handoff_queued_" + uuid4().hex
        self._register_ray_job_gate_value(marker)
        delay_seconds = float(self.config.task_timeout * 2)
        script = f"""
import hashlib
import json
from datetime import timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskState
from testproject.tasks import echo_task


def row_sha256(row):
    fields = sorted(field.attname for field in row._meta.concrete_fields)
    payload = {{name: getattr(row, name) for name in fields}}
    canonical = json.dumps(
        payload,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


marker = {marker!r}
run_after = timezone.now() + timedelta(seconds={delay_seconds!r})
result = echo_task.using(
    backend={RAY_JOB_GATE_QUEUE!r},
    queue_name={RAY_JOB_GATE_QUEUE!r},
    run_after=run_after,
).enqueue(marker)
row = RayTaskExecution.objects.get(task_id=result.id)
if row.state != TaskState.QUEUED:
    raise RuntimeError("deferred protocol-v1 survival task was not queued")
print(json.dumps({{
    "pk": row.pk,
    "task_id": row.task_id,
    "marker": marker,
    "row_sha256": row_sha256(row),
    "state": str(row.state),
    "execution_protocol_version": row.execution_protocol_version,
    "queue_name": row.queue_name,
    "attempt_number": row.attempt_number,
    "execution_generation": row.execution_generation,
    "run_after": row.run_after.isoformat() if row.run_after is not None else None,
    "claimed_by_worker": row.claimed_by_worker,
    "ray_job_id": row.ray_job_id,
    "ray_job_request_reference": row.ray_job_request_reference,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        fixture = self._sensitive_django_shell(
            script,
            field_name="protocol-v1 deferred survival enqueue",
        )
        task_id = self._canonical_uuid4(
            fixture.get("task_id"),
            field_name="protocol-v1 survival task id",
        )
        run_after = fixture.get("run_after")
        try:
            run_after_value = datetime.fromisoformat(str(run_after))
        except ValueError as error:
            raise ValueError("protocol-v1 survival run-after timestamp is invalid") from error
        row_sha256 = fixture.get("row_sha256")
        pk = fixture.get("pk")
        if (
            type(pk) is not int
            or pk < 1
            or fixture.get("marker") != marker
            or not isinstance(row_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", row_sha256) is None
            or fixture.get("state") != "QUEUED"
            or type(fixture.get("execution_protocol_version")) is not int
            or fixture.get("execution_protocol_version") != 1
            or fixture.get("queue_name") != RAY_JOB_GATE_QUEUE
            or type(fixture.get("attempt_number")) is not int
            or fixture.get("attempt_number") != 1
            or type(fixture.get("execution_generation")) is not int
            or fixture.get("execution_generation") != 0
            or run_after_value.tzinfo is None
            or fixture.get("claimed_by_worker") is not None
            or fixture.get("ray_job_id") is not None
            or fixture.get("ray_job_request_reference") is not None
        ):
            raise ValueError("protocol-v1 survival task did not retain its queued identity")
        self._register_ray_job_gate_value(task_id)
        confirmed = dict(fixture)
        self._protocol_v1_survival_fixture = confirmed
        return confirmed

    def _observe_protocol_v1_survival_task(
        self,
        fixture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Observe the exact deferred protocol-v1 row without claiming it."""

        task_id = self._canonical_uuid4(
            fixture.get("task_id"),
            field_name="protocol-v1 survival observation task id",
        )
        pk = fixture.get("pk")
        marker = fixture.get("marker")
        if (
            type(pk) is not int
            or pk < 1
            or not isinstance(marker, str)
            or PROTOCOL_V1_SURVIVAL_PATTERN.fullmatch(marker) is None
        ):
            raise ValueError("protocol-v1 survival observation identity is invalid")
        script = f"""
import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder

from django_ray.models import RayTaskExecution


def row_sha256(row):
    fields = sorted(field.attname for field in row._meta.concrete_fields)
    payload = {{name: getattr(row, name) for name in fields}}
    canonical = json.dumps(
        payload,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


row = RayTaskExecution.objects.get(pk={pk!r}, task_id={task_id!r})
print(json.dumps({{
    "pk": row.pk,
    "task_id": row.task_id,
    "row_sha256": row_sha256(row),
    "state": str(row.state),
    "execution_protocol_version": row.execution_protocol_version,
    "queue_name": row.queue_name,
    "callable_path": row.callable_path,
    "args_json": row.args_json,
    "kwargs_json": row.kwargs_json,
    "attempt_number": row.attempt_number,
    "execution_generation": row.execution_generation,
    "run_after": row.run_after.isoformat() if row.run_after is not None else None,
    "claimed_by_worker": row.claimed_by_worker,
    "ray_job_id": row.ray_job_id,
    "ray_address": row.ray_address,
    "ray_job_request_reference": row.ray_job_request_reference,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        return self._sensitive_django_shell(
            script,
            field_name="protocol-v1 deferred survival observation",
        )

    def _require_protocol_v1_survival_unchanged(
        self,
        fixture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Require the deferred v1 row to remain byte-for-byte queued."""

        observation = self._observe_protocol_v1_survival_task(fixture)
        if (
            observation.get("pk") != fixture.get("pk")
            or observation.get("task_id") != fixture.get("task_id")
            or observation.get("row_sha256") != fixture.get("row_sha256")
            or observation.get("state") != "QUEUED"
            or type(observation.get("execution_protocol_version")) is not int
            or observation.get("execution_protocol_version") != 1
            or observation.get("queue_name") != RAY_JOB_GATE_QUEUE
            or observation.get("callable_path") != "testproject.tasks.echo_task"
            or observation.get("args_json")
            != json.dumps([fixture.get("marker")], separators=(",", ":"))
            or observation.get("kwargs_json") != "{}"
            or type(observation.get("attempt_number")) is not int
            or observation.get("attempt_number") != 1
            or type(observation.get("execution_generation")) is not int
            or observation.get("execution_generation") != 0
            or observation.get("run_after") != fixture.get("run_after")
            or observation.get("claimed_by_worker") is not None
            or observation.get("ray_job_id") is not None
            or observation.get("ray_address") is not None
            or observation.get("ray_job_request_reference") is not None
        ):
            raise ValueError("protocol-v1 deferred row changed during manager handoff")
        return observation

    def _release_protocol_v1_survival_task(
        self,
        fixture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Make the exact survived v1 row eligible after current-manager adoption."""

        task_id = self._canonical_uuid4(
            fixture.get("task_id"),
            field_name="protocol-v1 survival release task id",
        )
        pk = fixture.get("pk")
        marker = fixture.get("marker")
        run_after = fixture.get("run_after")
        if (
            type(pk) is not int
            or pk < 1
            or not isinstance(marker, str)
            or PROTOCOL_V1_SURVIVAL_PATTERN.fullmatch(marker) is None
            or not isinstance(run_after, str)
        ):
            raise ValueError("protocol-v1 survival release identity is invalid")
        script = f"""
import json
from datetime import datetime

from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskState

marker = {marker!r}
previous_run_after = datetime.fromisoformat({run_after!r})
queryset = RayTaskExecution.objects.filter(
    pk={pk!r},
    task_id={task_id!r},
    state=TaskState.QUEUED,
    execution_protocol_version=1,
    queue_name={RAY_JOB_GATE_QUEUE!r},
    callable_path="testproject.tasks.echo_task",
    args_json=json.dumps([marker], separators=(",", ":")),
    kwargs_json="{{}}",
    attempt_number=1,
    execution_generation=0,
    run_after=previous_run_after,
    claimed_by_worker__isnull=True,
    ray_job_id__isnull=True,
    ray_address__isnull=True,
    ray_job_request_reference__isnull=True,
)
matched = queryset.count()
released_at = timezone.now()
updated = queryset.update(run_after=released_at)
row = RayTaskExecution.objects.get(pk={pk!r}, task_id={task_id!r})
print(json.dumps({{
    "matched": matched,
    "updated": updated,
    "pk": row.pk,
    "task_id": row.task_id,
    "state": str(row.state),
    "attempt_number": row.attempt_number,
    "execution_generation": row.execution_generation,
    "released_at": released_at.isoformat(),
    "run_after": row.run_after.isoformat() if row.run_after is not None else None,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        result = self._sensitive_django_shell(
            script,
            field_name="protocol-v1 deferred survival release",
        )
        if (
            type(result.get("matched")) is not int
            or result.get("matched") != 1
            or type(result.get("updated")) is not int
            or result.get("updated") != 1
            or result.get("pk") != pk
            or result.get("task_id") != task_id
            or result.get("state") != "QUEUED"
            or type(result.get("attempt_number")) is not int
            or result.get("attempt_number") != 1
            or type(result.get("execution_generation")) is not int
            or result.get("execution_generation") != 0
            or result.get("run_after") != result.get("released_at")
        ):
            raise ValueError("protocol-v1 survived row was not released exactly once")
        return result

    def _cleanup_protocol_v1_survival_task(
        self,
        fixture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Delete only an exact, still-unclaimed deferred v1 failure fixture."""

        task_id = self._canonical_uuid4(
            fixture.get("task_id"),
            field_name="protocol-v1 survival cleanup task id",
        )
        pk = fixture.get("pk")
        marker = fixture.get("marker")
        if (
            type(pk) is not int
            or pk < 1
            or not isinstance(marker, str)
            or PROTOCOL_V1_SURVIVAL_PATTERN.fullmatch(marker) is None
        ):
            raise ValueError("protocol-v1 survival cleanup identity is invalid")
        script = f"""
import json

from django_ray.models import RayTaskExecution, TaskState

marker = {marker!r}
owned = RayTaskExecution.objects.filter(
    pk={pk!r},
    task_id={task_id!r},
    execution_protocol_version=1,
    queue_name={RAY_JOB_GATE_QUEUE!r},
    callable_path="testproject.tasks.echo_task",
    args_json=json.dumps([marker], separators=(",", ":")),
    kwargs_json="{{}}",
    attempt_number=1,
)
matched = owned.count()
states = list(owned.values_list("state", flat=True)[:2])
deletable = owned.filter(
    state=TaskState.QUEUED,
    execution_generation=0,
    claimed_by_worker__isnull=True,
    ray_job_id__isnull=True,
    ray_address__isnull=True,
    ray_job_request_reference__isnull=True,
)
deleted, _ = deletable.delete()
print(json.dumps({{
    "matched": matched,
    "states": states,
    "deleted": deleted,
    "queued_absent": not owned.filter(state=TaskState.QUEUED).exists(),
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        result = self._sensitive_django_shell(
            script,
            field_name="protocol-v1 deferred survival cleanup",
        )
        matched = result.get("matched")
        deleted = result.get("deleted")
        states = result.get("states")
        if (
            type(matched) is not int
            or matched not in {0, 1}
            or not isinstance(states, list)
            or len(states) != matched
            or type(deleted) is not int
            or deleted not in {0, 1}
            or (deleted == 1 and states != ["QUEUED"])
            or result.get("queued_absent") is not True
        ):
            raise ValueError("protocol-v1 survival failure fixture cleanup was not exact")
        self._protocol_v1_survival_fixture = None
        return result

    def _observe_protocol_handoff_task(self, task_id: str) -> Mapping[str, Any]:
        """Observe the durable row and the released payload-carrier Ray Job."""

        script = f"""
import json
import shlex

from django_ray.models import RayTaskExecution
from django_ray.runner.ray_job import _address_pinned_job_client

row = RayTaskExecution.objects.get(task_id={task_id!r})
if not row.ray_job_id or not row.ray_address:
    print(json.dumps({{"ready": False, "state": str(row.state)}}))
else:
    client = _address_pinned_job_client(row.ray_address)
    info = client.get_job_info(row.ray_job_id)
    submissions = sum(
        1
        for job in client.list_jobs()
        if getattr(job, "submission_id", None) == row.ray_job_id
    )
    status = str(getattr(info, "status", "")) if info is not None else ""
    if info is None or submissions < 1 or status not in {
            "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "STOPPED"
        }:
        print(json.dumps({{"ready": False, "state": str(row.state)}}))
    else:
        parts = shlex.split(str(getattr(info, "entrypoint", "")))
        released_carrier = (
            len(parts) == 5
            and parts[:4] == [
                "python",
                "-m",
                "django_ray.runtime.entrypoint",
                {RAY_JOB_RELEASED_PAYLOAD_CARRIER!r},
            ]
            and {RAY_JOB_REQUEST_REFERENCE_CARRIER!r} not in parts
        )
        print(json.dumps({{
            "ready": True,
            "state": status,
            "durable_state": str(row.state),
            "attempt_number": row.attempt_number,
            "execution_generation": row.execution_generation,
            "worker_id": row.claimed_by_worker,
            "job_id": row.ray_job_id,
            "released_carrier": released_carrier,
            "request_reference_absent": row.ray_job_request_reference is None,
            "submission_count": submissions,
        }}, sort_keys=True, separators=(",", ":")))
""".strip()
        return self._sensitive_django_shell(
            script,
            field_name="released v0.4.0 handoff task observation",
        )

    def _wait_for_protocol_handoff_task(
        self,
        task_id: str,
        *,
        accepted_states: frozenset[str],
        different_worker: str | None = None,
        durable_state: str | None = None,
    ) -> Mapping[str, Any]:
        """Wait for one released-carrier job without permitting resubmission."""

        deadline = time.monotonic() + self.config.task_timeout
        last_state = "not submitted"
        while True:
            observation = self._observe_protocol_handoff_task(task_id)
            state_value = observation.get("state")
            if observation.get("ready") is True:
                state = self._ray_job_gate_status(
                    state_value,
                    field_name="released v0.4.0 handoff Ray Job",
                )
                last_state = state
                if state in RAY_JOB_GATE_TERMINAL_STATES - accepted_states:
                    raise ValueError(
                        f"released v0.4.0 handoff Ray Job reached unexpected state {state}"
                    )
                worker = observation.get("worker_id")
                worker_changed = different_worker is None or (
                    isinstance(worker, str) and worker and worker != different_worker
                )
                durable_ready = (
                    durable_state is None or observation.get("durable_state") == durable_state
                )
                if state in accepted_states and worker_changed and durable_ready:
                    return observation
            elif isinstance(state_value, str):
                last_state = state_value
            if time.monotonic() >= deadline:
                raise ValueError(
                    "released v0.4.0 handoff Ray Job did not reach the required state within "
                    f"{self.config.task_timeout}s (last state: {last_state})"
                )
            time.sleep(2)

    def _delete_released_v040_worker_lease(self) -> None:
        """Delete only schema-0 leases bound to the gate-unique pod hostname."""

        if self._released_v040_hostname is None:
            raise ValueError("released v0.4.0 gate hostname was not retained")
        script = f"""
import json

from django_ray.models import TaskWorkerLease

queryset = TaskWorkerLease.objects.filter(
    hostname={self._released_v040_hostname!r},
    capability_schema_version=0,
)
matched = queryset.count()
rows = list(queryset.order_by("worker_id").values(
    "worker_id",
    "queue_name",
    "django_ray_version",
    "min_supported_execution_protocol_version",
    "max_supported_execution_protocol_version",
)[:2])
if matched > 1 or len(rows) != matched:
    raise RuntimeError("released v0.4.0 worker lease cleanup is ambiguous")
if rows and (
    rows[0]["queue_name"] != {RAY_JOB_GATE_QUEUE!r}
    or rows[0]["django_ray_version"] is not None
    or rows[0]["min_supported_execution_protocol_version"] is not None
    or rows[0]["max_supported_execution_protocol_version"] is not None
):
    raise RuntimeError("released v0.4.0 worker lease cleanup identity is foreign")
worker_ids = [row["worker_id"] for row in rows]
expected_worker_id = {self._released_v040_worker_id!r}
if expected_worker_id is not None and worker_ids != [expected_worker_id]:
    raise RuntimeError("released v0.4.0 worker lease cleanup worker identity changed")
deleted, _ = queryset.delete()
print(json.dumps({{
    "matched": matched,
    "deleted": deleted,
    "absent": not TaskWorkerLease.objects.filter(
        hostname={self._released_v040_hostname!r},
        capability_schema_version=0,
    ).exists(),
    "worker_ids": worker_ids,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        result = self._sensitive_django_shell(
            script,
            field_name="released v0.4.0 worker lease cleanup",
        )
        matched = result.get("matched")
        deleted = result.get("deleted")
        worker_ids = result.get("worker_ids")
        worker_identity_exact = worker_ids == []
        if matched == 1 and isinstance(worker_ids, list) and len(worker_ids) == 1:
            observed_worker_id = self._canonical_uuid4(
                worker_ids[0],
                field_name="released v0.4.0 cleaned worker id",
            )
            worker_identity_exact = self._released_v040_worker_id in {
                None,
                observed_worker_id,
            }
            if self._released_v040_worker_id is None:
                self._released_v040_worker_id = observed_worker_id
                self._register_ray_job_gate_value(observed_worker_id)
        if (
            type(matched) is not int
            or matched not in {0, 1}
            or type(deleted) is not int
            or deleted != matched
            or not worker_identity_exact
            or (self._released_v040_worker_id is not None and matched != 1)
            or result.get("absent") is not True
        ):
            raise ValueError("released v0.4.0 worker lease cleanup was not exact")

    def _observe_released_v040_reserved_leases(self) -> Mapping[str, Any]:
        """Bound any lease using the gate-reserved hostname namespace."""

        script = """
import json

from django_ray.models import TaskWorkerLease
from django_ray.runner.leasing import get_lease_duration
from django.utils import timezone

cutoff = timezone.now() - get_lease_duration()
queryset = TaskWorkerLease.objects.filter(hostname__startswith="dr-v040-").order_by(
    "hostname", "worker_id"
)
count = queryset.count()
rows = list(queryset.values(
    "worker_id",
    "hostname",
    "queue_name",
    "capability_schema_version",
    "django_ray_version",
    "min_supported_execution_protocol_version",
    "max_supported_execution_protocol_version",
    "is_active",
    "last_heartbeat_at",
    "stopped_at",
)[:2])
for row in rows:
    row["heartbeat_live"] = bool(
        row["is_active"] and row["last_heartbeat_at"] >= cutoff
    )
    row["last_heartbeat_at"] = row["last_heartbeat_at"].isoformat()
    row["stopped_at"] = (
        row["stopped_at"].isoformat() if row["stopped_at"] is not None else None
    )
print(json.dumps({
    "count": count,
    "rows": rows,
}, sort_keys=True, separators=(",", ":")))
""".strip()
        return self._sensitive_django_shell(
            script,
            field_name="released v0.4.0 reserved lease recovery observation",
        )

    def _validate_released_v040_recovery_deployment(
        self,
        deployment: Mapping[str, Any],
    ) -> str:
        """Validate one pre-existing transient Deployment and return its hostname."""

        if _resource_identity(deployment) != (
            "apps/v1",
            "Deployment",
            RELEASED_V040_MANAGER_DEPLOYMENT,
        ):
            raise ValueError("pre-existing released v0.4.0 resource is not the gate Deployment")
        metadata = _metadata(deployment)
        if metadata.get("namespace") != self.config.namespace:
            raise ValueError("pre-existing released v0.4.0 Deployment escaped the namespace")
        expected_labels = self._released_v040_manager_labels()
        if (
            dict(_mapping(metadata.get("labels"), field_name="released recovery Deployment labels"))
            != expected_labels
        ):
            raise ValueError("pre-existing released v0.4.0 Deployment labels are foreign")
        spec = _mapping(deployment.get("spec"), field_name="released recovery Deployment spec")
        replicas = spec.get("replicas")
        if type(replicas) is not int or replicas != 1:
            raise ValueError("pre-existing released v0.4.0 Deployment replicas are not exact")
        selector = normalize_label_selector(
            spec.get("selector"),
            field_name="released recovery Deployment selector",
        )
        if selector != tuple(sorted(expected_labels.items())):
            raise ValueError("pre-existing released v0.4.0 Deployment selector is foreign")
        template = _mapping(spec.get("template"), field_name="released recovery pod template")
        template_metadata = _mapping(
            template.get("metadata"),
            field_name="released recovery pod metadata",
        )
        if (
            dict(
                _mapping(
                    template_metadata.get("labels"),
                    field_name="released recovery pod labels",
                )
            )
            != expected_labels
        ):
            raise ValueError("pre-existing released v0.4.0 pod labels are foreign")
        live_spec = _mapping(template.get("spec"), field_name="released recovery pod spec")
        hostname = live_spec.get("hostname")
        if (
            not isinstance(hostname, str)
            or RELEASED_V040_HOSTNAME_PATTERN.fullmatch(hostname) is None
        ):
            raise ValueError("pre-existing released v0.4.0 Deployment hostname is foreign")

        live_main_containers = _sequence(
            live_spec.get("containers"),
            field_name="released recovery containers",
        )
        if len(live_main_containers) != 1:
            raise ValueError("pre-existing released v0.4.0 Deployment containers are foreign")
        live_main_container = _mapping(
            live_main_containers[0],
            field_name="released recovery main container",
        )
        live_image_tag = live_main_container.get("image")
        if (
            not isinstance(live_image_tag, str)
            or RELEASED_V040_IMAGE_TAG_PATTERN.fullmatch(live_image_tag) is None
        ):
            raise ValueError("pre-existing released v0.4.0 Deployment image tag is foreign")
        self._register_ray_job_gate_value(live_image_tag)
        parse_docker_image_inspect(
            self._docker("image", "inspect", live_image_tag).stdout,
            expected_tag=live_image_tag,
            commit=RELEASED_V040_COMMIT,
            source_tree=RELEASED_V040_SOURCE_TREE,
        )
        expected_spec = _mapping(
            _mapping(
                _mapping(
                    self._released_v040_manager_manifest(
                        image_tag=live_image_tag,
                        hostname=hostname,
                    ).get("spec"),
                    field_name="expected released recovery Deployment spec",
                ).get("template"),
                field_name="expected released recovery pod template",
            ).get("spec"),
            field_name="expected released recovery pod spec",
        )
        if pod_image_contract(live_spec) != pod_image_contract(expected_spec):
            raise ValueError("pre-existing released v0.4.0 Deployment images are foreign")
        for field_name in ("hostname", "volumes"):
            if live_spec.get(field_name) != expected_spec.get(field_name):
                raise ValueError(f"pre-existing released v0.4.0 Deployment {field_name} is foreign")
        for container_field in ("initContainers", "containers"):
            live_containers = _sequence(
                live_spec.get(container_field),
                field_name=f"released recovery {container_field}",
            )
            expected_containers = _sequence(
                expected_spec.get(container_field),
                field_name=f"expected released recovery {container_field}",
            )
            if len(live_containers) != len(expected_containers):
                raise ValueError(
                    f"pre-existing released v0.4.0 Deployment {container_field} are foreign"
                )
            for index, expected_value in enumerate(expected_containers):
                live_container = _mapping(
                    live_containers[index],
                    field_name=f"released recovery {container_field}[{index}]",
                )
                expected_container = _mapping(
                    expected_value,
                    field_name=f"expected released recovery {container_field}[{index}]",
                )
                for key, value in expected_container.items():
                    if live_container.get(key) != value:
                        raise ValueError(
                            "pre-existing released v0.4.0 Deployment container contract is foreign"
                        )
        self._released_v040_hostname = hostname
        return hostname

    def _recover_released_v040_startup_residue(self) -> None:
        """Recover only an exact interrupted transient manager and its lease."""

        result = self._kubectl(
            "get",
            "deployment",
            RELEASED_V040_MANAGER_DEPLOYMENT,
            "--ignore-not-found",
            "-o",
            "json",
        )
        if result.stdout.strip():
            deployment = self._json_command(
                result,
                field_name="pre-existing released v0.4.0 Deployment",
            )
            hostname = self._validate_released_v040_recovery_deployment(deployment)
            self._register_ray_job_gate_value(hostname)
            leases = self._observe_released_v040_reserved_leases()
            count = leases.get("count")
            rows = leases.get("rows")
            if (
                type(count) is not int
                or count not in {0, 1}
                or not isinstance(rows, list)
                or len(rows) != count
            ):
                raise ValueError("released v0.4.0 Deployment lease residue is ambiguous")
            if rows:
                row = _mapping(rows[0], field_name="released v0.4.0 Deployment lease")
                if (
                    row.get("hostname") != hostname
                    or row.get("queue_name") != RAY_JOB_GATE_QUEUE
                    or type(row.get("capability_schema_version")) is not int
                    or row.get("capability_schema_version") != 0
                    or row.get("django_ray_version") is not None
                    or row.get("min_supported_execution_protocol_version") is not None
                    or row.get("max_supported_execution_protocol_version") is not None
                ):
                    raise ValueError("released v0.4.0 Deployment lease residue is foreign")
                worker_id = self._canonical_uuid4(
                    row.get("worker_id"),
                    field_name="released v0.4.0 Deployment recovery worker id",
                )
                self._released_v040_worker_id = worker_id
                self._register_ray_job_gate_value(worker_id)
            self._delete_released_v040_manager()
            self._delete_released_v040_worker_lease()
            self._released_v040_worker_id = None
            self._released_v040_hostname = None
            return

        leases = self._observe_released_v040_reserved_leases()
        count = leases.get("count")
        rows = leases.get("rows")
        if type(count) is not int or count < 0 or not isinstance(rows, list):
            raise ValueError("released v0.4.0 recovery lease observation is invalid")
        if count == 0 and rows == []:
            self._delete_released_v040_manager()
            self._released_v040_worker_id = None
            self._released_v040_hostname = None
            return
        if count != 1 or len(rows) != 1:
            raise ValueError("released v0.4.0 recovery lease residue is ambiguous")
        row = _mapping(rows[0], field_name="released v0.4.0 recovery lease")
        hostname = row.get("hostname")
        if (
            not isinstance(hostname, str)
            or RELEASED_V040_HOSTNAME_PATTERN.fullmatch(hostname) is None
            or row.get("queue_name") != RAY_JOB_GATE_QUEUE
            or type(row.get("capability_schema_version")) is not int
            or row.get("capability_schema_version") != 0
            or row.get("django_ray_version") is not None
            or row.get("min_supported_execution_protocol_version") is not None
            or row.get("max_supported_execution_protocol_version") is not None
            or row.get("heartbeat_live") is not False
        ):
            raise ValueError("released v0.4.0 recovery lease residue is live or foreign")
        worker_id = self._canonical_uuid4(
            row.get("worker_id"),
            field_name="released v0.4.0 recovery worker id",
        )
        self._released_v040_hostname = hostname
        self._released_v040_worker_id = worker_id
        self._register_ray_job_gate_value(hostname)
        self._register_ray_job_gate_value(worker_id)
        self._delete_released_v040_manager()
        self._delete_released_v040_worker_lease()
        self._released_v040_worker_id = None
        self._released_v040_hostname = None

    def _recover_protocol_v2_startup_fixture(self) -> Mapping[str, Any]:
        """Recover at most one exact interrupted protocol-2 gate fixture."""

        script = f"""
import json
import re
from uuid import UUID

from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskExecutionProtocolPolicy,
    TaskState,
)
from django_ray.protocol_coordination import reopen_legacy_worker_admission


def canonical_uuid4(value):
    try:
        parsed = UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.version == 4 and str(parsed) == str(value)


reserved = RayTaskExecution.objects.filter(
    callable_path__startswith="testproject.tasks.protocol_v2_application_poison_"
).order_by("pk")
count = reserved.count()
rows = list(reserved[:2])
if count > 1 or len(rows) != count:
    raise RuntimeError("protocol-v2 startup fixture residue is ambiguous")
policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
token_count = LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).count()
if (
    policy.schema_version != 1
    or policy.active_write_protocol_version != 1
    or policy.revision < 1
    or token_count not in [0, 1]
    or (policy.legacy_worker_admission_enabled and token_count != 1)
    or (not policy.legacy_worker_admission_enabled and token_count != 0)
):
    raise RuntimeError("protocol-v2 startup recovery policy is not exact")
if not rows:
    if not policy.legacy_worker_admission_enabled:
        raise RuntimeError("closed legacy admission has no exact gate fixture identity")
    print(json.dumps({{
        "recovered": False,
        "matched": 0,
        "terminalized": 0,
        "deleted": 0,
        "row_absent": True,
        "schema_version": policy.schema_version,
        "active_write_protocol_version": policy.active_write_protocol_version,
        "legacy_worker_admission_enabled": policy.legacy_worker_admission_enabled,
        "revision": policy.revision,
        "token_count": token_count,
    }}, sort_keys=True, separators=(",", ":")))
else:
    row = rows[0]
    poison = row.callable_path.removeprefix("testproject.tasks.")
    exact = (
        re.fullmatch(r"protocol_v2_application_poison_[0-9a-f]{{32}}", poison) is not None
        and canonical_uuid4(row.task_id)
        and row.callable_path == "testproject.tasks." + poison
        and row.execution_protocol_version == 2
        and row.metadata_schema_version == 1
        and row.queue_name == {RAY_JOB_GATE_QUEUE!r}
        and row.priority == 0
        and row.state in [TaskState.FAILED, TaskState.QUEUED]
        and row.attempt_number == 1
        and row.execution_generation == 0
        and row.args_json == json.dumps([poison], separators=(",", ":"))
        and row.kwargs_json == "{{}}"
        and row.claimed_by_worker is None
        and row.ray_target_address is None
        and row.ray_job_id is None
        and row.ray_address is None
        and row.ray_job_request_reference is None
        and row.input_reference is None
        and row.started_at is None
        and row.finished_at is None
        and row.last_heartbeat_at is None
        and row.run_after is None
        and row.timeout_seconds is None
        and row.queue_timeout_seconds is None
        and row.queue_deadline_at is None
        and row.result_data is None
        and row.result_reference is None
        and row.completion_data is None
        and row.error_message is None
        and row.error_traceback is None
        and row.cancellation_status is None
        and row.cancellation_error is None
    )
    if not exact:
        raise RuntimeError("reserved protocol-v2 startup fixture is foreign")
    if policy.legacy_worker_admission_enabled and row.state != TaskState.FAILED:
        raise RuntimeError("queued protocol-v2 residue exists while legacy admission is open")
    terminalized = 0
    if row.state == TaskState.QUEUED:
        terminalized = RayTaskExecution.objects.filter(
            pk=row.pk,
            task_id=row.task_id,
            state=TaskState.QUEUED,
            execution_protocol_version=2,
            queue_name={RAY_JOB_GATE_QUEUE!r},
            callable_path=row.callable_path,
            args_json=row.args_json,
            kwargs_json="{{}}",
            claimed_by_worker__isnull=True,
            ray_job_id__isnull=True,
            ray_address__isnull=True,
            ray_target_address__isnull=True,
            ray_job_request_reference__isnull=True,
            input_reference__isnull=True,
        ).update(state=TaskState.FAILED)
        if terminalized != 1:
            raise RuntimeError("protocol-v2 startup fixture did not terminalize exactly once")
    if not policy.legacy_worker_admission_enabled:
        transition = reopen_legacy_worker_admission(expected_revision=int(policy.revision))
        if not transition.enabled or not transition.changed:
            raise RuntimeError("legacy admission did not reopen during startup recovery")
    policy.refresh_from_db()
    token_count = LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).count()
    exact_terminal = RayTaskExecution.objects.filter(
        pk=row.pk,
        task_id=row.task_id,
        state=TaskState.FAILED,
        execution_protocol_version=2,
        queue_name={RAY_JOB_GATE_QUEUE!r},
        callable_path=row.callable_path,
        args_json=row.args_json,
        kwargs_json="{{}}",
        claimed_by_worker__isnull=True,
        ray_job_id__isnull=True,
        ray_address__isnull=True,
        ray_target_address__isnull=True,
        ray_job_request_reference__isnull=True,
        input_reference__isnull=True,
    )
    matched = exact_terminal.count()
    deleted, _ = exact_terminal.delete()
    print(json.dumps({{
        "recovered": True,
        "matched": matched,
        "terminalized": terminalized,
        "deleted": deleted,
        "row_absent": not reserved.exists(),
        "schema_version": policy.schema_version,
        "active_write_protocol_version": policy.active_write_protocol_version,
        "legacy_worker_admission_enabled": policy.legacy_worker_admission_enabled,
        "revision": policy.revision,
        "token_count": token_count,
    }}, sort_keys=True, separators=(",", ":")))
""".strip()
        result = self._sensitive_django_shell(
            script,
            field_name="protocol-v2 startup fixture recovery",
        )
        recovered = result.get("recovered")
        matched = result.get("matched")
        terminalized = result.get("terminalized")
        deleted = result.get("deleted")
        if recovered is True:
            row_recovery_exact = (
                type(matched) is int
                and matched == 1
                and type(terminalized) is int
                and terminalized in {0, 1}
                and type(deleted) is int
                and deleted == 1
            )
        elif recovered is False:
            row_recovery_exact = (
                type(matched) is int
                and matched == 0
                and type(terminalized) is int
                and terminalized == 0
                and type(deleted) is int
                and deleted == 0
            )
        else:
            row_recovery_exact = False
        revision = result.get("revision")
        if (
            not row_recovery_exact
            or result.get("row_absent") is not True
            or type(result.get("schema_version")) is not int
            or result.get("schema_version") != 1
            or type(result.get("active_write_protocol_version")) is not int
            or result.get("active_write_protocol_version") != 1
            or result.get("legacy_worker_admission_enabled") is not True
            or type(revision) is not int
            or revision < 1
            or type(result.get("token_count")) is not int
            or result.get("token_count") != 1
        ):
            raise ValueError("protocol-v2 startup fixture recovery was not exact")
        return result

    def _recover_protocol_handoff_residue(self) -> None:
        """Remove exact interrupted fixtures and restore the current manager."""

        self._recover_released_v040_startup_residue()
        self._recover_protocol_v2_startup_fixture()
        manager = self._ray_job_manager_replica_observation()
        if manager != {"replicas": 1, "ready_replicas": 1}:
            self._scale_ray_job_manager(1)
            self._wait_for_application_topology()
            manager = self._ray_job_manager_replica_observation()
        if manager != {"replicas": 1, "ready_replicas": 1}:
            raise ValueError("current Ray Job manager recovery did not reach one ready replica")

    def _protocol_metric_counts(self) -> dict[tuple[str, str], int]:
        """Read the authenticated bounded protocol metric family."""

        headers = {"Authorization": f"Bearer {self._secret_token()}"}
        status, body = self._http("/api/metrics", method="GET", headers=headers)
        if status != 200:
            raise ValueError(f"authenticated /api/metrics returned {status}, expected 200")
        return validate_execution_protocol_metrics(body)

    def _seed_protocol_v2_fixture(self) -> Mapping[str, Any]:
        """Stage one terminal protocol-2 row, close admission, then queue it."""

        initial_revision = self._protocol_handoff_initial_revision
        if type(initial_revision) is not int or initial_revision < 1:
            raise ValueError("protocol handoff did not retain its initial policy revision")
        generated_task_id = str(uuid4())
        generated_poison = "protocol_v2_application_poison_" + uuid4().hex
        cleanup_identity: dict[str, Any] = {
            "pk": None,
            "task_id": generated_task_id,
            "queue_name": RAY_JOB_GATE_QUEUE,
            "poison": generated_poison,
            "initial_revision": initial_revision,
            "closed_revision": initial_revision + 1,
            "seed_confirmed": False,
        }
        self._protocol_v2_fixture = cleanup_identity
        self._register_ray_job_gate_value(generated_task_id)
        self._register_ray_job_gate_value(generated_poison)
        script = f"""
import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from django_ray import __version__
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_coordination import (
    close_legacy_worker_admission,
)


def row_sha256(row):
    fields = sorted(field.attname for field in row._meta.concrete_fields)
    payload = {{name: getattr(row, name) for name in fields}}
    canonical = json.dumps(
        payload,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
token_present = LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()
active_legacy = TaskWorkerLease.objects.filter(
    capability_schema_version=0,
    is_active=True,
).count()
non_v1_nonterminal = RayTaskExecution.objects.filter(
    state__in=[TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING],
).exclude(execution_protocol_version=1).count()
if (
    policy.schema_version != 1
    or policy.active_write_protocol_version != 1
    or policy.legacy_worker_admission_enabled is not True
    or token_present is not True
    or policy.revision != {initial_revision!r}
    or active_legacy != 0
    or non_v1_nonterminal != 0
):
    raise RuntimeError("protocol-v2 fixture preconditions are not exact")
initial_revision = {initial_revision!r}
task_id = {generated_task_id!r}
poison = {generated_poison!r}
callable_path = "testproject.tasks." + poison
row = RayTaskExecution.objects.create(
    task_id=task_id,
    callable_path=callable_path,
    execution_protocol_version=2,
    created_with_django_ray_version=__version__,
    queue_name={RAY_JOB_GATE_QUEUE!r},
    state=TaskState.FAILED,
    args_json=json.dumps([poison], separators=(",", ":")),
    kwargs_json="{{}}",
)
transition = close_legacy_worker_admission(
    expected_revision=initial_revision,
    legacy_producers_retired=True,
)
if transition.enabled or not transition.changed or transition.revision != initial_revision + 1:
    raise RuntimeError("legacy admission did not close exactly once")
updated = RayTaskExecution.objects.filter(
    pk=row.pk,
    task_id=task_id,
    callable_path=callable_path,
    execution_protocol_version=2,
    queue_name={RAY_JOB_GATE_QUEUE!r},
    state=TaskState.FAILED,
    args_json=json.dumps([poison], separators=(",", ":")),
    kwargs_json="{{}}",
    claimed_by_worker__isnull=True,
    ray_job_id__isnull=True,
    ray_address__isnull=True,
    ray_target_address__isnull=True,
    ray_job_request_reference__isnull=True,
    input_reference__isnull=True,
).update(state=TaskState.QUEUED)
if updated != 1:
    raise RuntimeError("protocol-v2 fixture did not transition to queued exactly once")
queued_at = timezone.now()
row.refresh_from_db()
print(json.dumps({{
    "pk": row.pk,
    "task_id": row.task_id,
    "queue_name": row.queue_name,
    "poison": poison,
    "row_sha256": row_sha256(row),
    "created_at": row.created_at.isoformat(),
    "queued_at": queued_at.isoformat(),
    "initial_revision": initial_revision,
    "closed_revision": transition.revision,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        fixture = self._sensitive_django_shell(
            script,
            field_name="protocol-v2 queued fixture creation",
        )
        returned_task_id = self._canonical_uuid4(
            fixture.get("task_id"),
            field_name="protocol-v2 queued fixture task id",
        )
        pk = fixture.get("pk")
        returned_poison = fixture.get("poison")
        queue_name = fixture.get("queue_name")
        row_sha256 = fixture.get("row_sha256")
        created_at = fixture.get("created_at")
        queued_at = fixture.get("queued_at")
        initial_revision = fixture.get("initial_revision")
        closed_revision = fixture.get("closed_revision")
        if (
            type(pk) is not int
            or pk < 1
            or returned_task_id != generated_task_id
            or returned_poison != generated_poison
            or queue_name != RAY_JOB_GATE_QUEUE
            or not isinstance(row_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", row_sha256) is None
            or not isinstance(created_at, str)
            or not created_at
            or not isinstance(queued_at, str)
            or not queued_at
            or type(initial_revision) is not int
            or type(closed_revision) is not int
            or initial_revision != self._protocol_handoff_initial_revision
            or closed_revision != initial_revision + 1
        ):
            raise ValueError("protocol-v2 queued fixture returned invalid identity")
        try:
            created_at_value = datetime.fromisoformat(created_at)
            queued_at_value = datetime.fromisoformat(queued_at)
        except ValueError as error:
            raise ValueError("protocol-v2 fixture timestamps are invalid") from error
        if (
            created_at_value.tzinfo is None
            or queued_at_value.tzinfo is None
            or queued_at_value < created_at_value
        ):
            raise ValueError("protocol-v2 fixture timestamps are not ordered and aware")
        confirmed = dict(fixture)
        confirmed["seed_confirmed"] = True
        self._protocol_v2_fixture = confirmed
        return confirmed

    def _observe_protocol_v2_fixture(self, fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return a full row digest and post-queue ray-data manager heartbeat proof."""

        queued_at = fixture.get("queued_at")
        if not isinstance(queued_at, str):
            raise ValueError("protocol-v2 fixture queued timestamp is missing")
        try:
            queued_at_value = datetime.fromisoformat(queued_at)
        except ValueError as error:
            raise ValueError("protocol-v2 fixture queued timestamp is invalid") from error
        if queued_at_value.tzinfo is None:
            raise ValueError("protocol-v2 fixture queued timestamp must be timezone-aware")

        script = f"""
import hashlib
import json
from datetime import datetime

from django.core.serializers.json import DjangoJSONEncoder

from django_ray.models import RayTaskExecution, TaskWorkerLease


def row_sha256(row):
    fields = sorted(field.attname for field in row._meta.concrete_fields)
    payload = {{name: getattr(row, name) for name in fields}}
    canonical = json.dumps(
        payload,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


row = RayTaskExecution.objects.get(pk={fixture["pk"]!r}, task_id={fixture["task_id"]!r})
leases = TaskWorkerLease.objects.filter(
    capability_schema_version=1,
    is_active=True,
    queue_name={RAY_JOB_GATE_QUEUE!r},
    min_supported_execution_protocol_version=1,
    max_supported_execution_protocol_version=1,
)
lease_count = leases.count()
queued_at = datetime.fromisoformat({queued_at!r})
post_queue_heartbeat = leases.filter(last_heartbeat_at__gt=queued_at).exists()
print(json.dumps({{
    "row_sha256": row_sha256(row),
    "state": str(row.state),
    "execution_protocol_version": row.execution_protocol_version,
    "queue_name": row.queue_name,
    "claimed_by_worker": row.claimed_by_worker,
    "ray_job_id": row.ray_job_id,
    "ray_address": row.ray_address,
    "ray_data_explicit_lease_count": lease_count,
    "post_queue_heartbeat": post_queue_heartbeat,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        return self._sensitive_django_shell(
            script,
            field_name="protocol-v2 queued fixture observation",
        )

    def _wait_for_protocol_v2_survival(
        self,
        fixture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Wait for the live ray-data manager to poll after the fixture was queued."""

        deadline = time.monotonic() + self.config.task_timeout
        while True:
            observation = self._observe_protocol_v2_fixture(fixture)
            if (
                observation.get("row_sha256") != fixture.get("row_sha256")
                or observation.get("state") != "QUEUED"
                or observation.get("execution_protocol_version") != 2
                or observation.get("queue_name") != RAY_JOB_GATE_QUEUE
                or observation.get("claimed_by_worker") is not None
                or observation.get("ray_job_id") is not None
                or observation.get("ray_address") is not None
            ):
                raise ValueError("protocol-v2 queued row changed before a manager poll completed")
            lease_count = observation.get("ray_data_explicit_lease_count")
            if type(lease_count) is not int or lease_count != 1:
                raise ValueError("ray-data protocol-v1 manager lease count was not exactly one")
            if observation.get("post_queue_heartbeat") is True:
                return observation
            if time.monotonic() >= deadline:
                raise ValueError(
                    "ray-data manager did not heartbeat after the protocol-v2 row was queued"
                )
            time.sleep(2)

    def _verify_protocol_v2_visibility(
        self,
        fixture: Mapping[str, Any],
        *,
        baseline_metrics: Mapping[tuple[str, str], int],
    ) -> None:
        """Prove unsupported work stays queued and visible on bounded live surfaces."""

        self._wait_for_protocol_v2_survival(fixture)
        headers = {"Authorization": f"Bearer {self._secret_token()}"}
        task_id = str(fixture["task_id"])
        status, body = self._http(
            f"/api/tasks/{task_id}",
            method="GET",
            headers=headers,
            response_limit=EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
        )
        if status != 200:
            raise ValueError(f"protocol-v2 task status returned {status}, expected 200")
        task_status = self._json_body(body, endpoint="protocol-v2 task status")
        if validate_task_status_payload(task_status, task_id=task_id) != "QUEUED":
            raise ValueError("protocol-v2 task status did not remain queued")
        validate_execution_protocol_visibility(
            task_status,
            surface="protocol-v2 task status",
            expected_protocol=2,
            expected_compatible=False,
        )

        query = urlencode({"task_id": task_id, "limit": 1})
        status, body = self._http(
            f"/api/executions?{query}",
            method="GET",
            headers=headers,
        )
        if status != 200:
            raise ValueError(f"protocol-v2 execution lookup returned {status}, expected 200")
        listing = self._json_body(body, endpoint="protocol-v2 execution lookup")
        tasks = _sequence(listing.get("tasks"), field_name="protocol-v2 execution tasks")
        if len(tasks) != 1:
            raise ValueError("protocol-v2 execution lookup did not return exactly one task")
        execution = _mapping(tasks[0], field_name="protocol-v2 execution")
        if execution.get("task_id") != task_id or execution.get("state") != "QUEUED":
            raise ValueError("protocol-v2 execution lookup returned the wrong queued task")
        validate_execution_protocol_visibility(
            execution,
            surface="protocol-v2 execution lookup",
            expected_protocol=2,
            expected_compatible=False,
        )

        cohorts = self._observe_protocol_cohorts()
        report = _mapping(cohorts.get("report"), field_name="protocol status report")
        policy = _mapping(report.get("policy"), field_name="protocol status policy")
        unsupported = _mapping(
            report.get("unsupported_work"),
            field_name="protocol status unsupported work",
        )
        groups = _sequence(
            unsupported.get("groups"),
            field_name="protocol status unsupported work groups",
        )
        expected_group = {
            "count": 1,
            "execution_protocol_version": 2,
            "queue": RAY_JOB_GATE_QUEUE,
            "state": "QUEUED",
        }
        non_v1_nonterminal_count = report.get("non_v1_nonterminal_count")
        unsupported_total_tasks = unsupported.get("total_tasks")
        unsupported_omitted_tasks = unsupported.get("omitted_tasks")
        active_write_protocol = policy.get("active_write_protocol_version")
        observed_group = (
            dict(_mapping(groups[0], field_name="unsupported protocol group"))
            if len(groups) == 1
            else {}
        )
        group_exact = set(observed_group) == set(expected_group) and all(
            type(observed_group.get(field_name)) is type(expected_value)
            and observed_group.get(field_name) == expected_value
            for field_name, expected_value in expected_group.items()
        )
        if (
            policy.get("legacy_worker_admission_enabled") is not False
            or policy.get("legacy_admission_token_present") is not False
            or type(active_write_protocol) is not int
            or active_write_protocol != 1
            or type(non_v1_nonterminal_count) is not int
            or non_v1_nonterminal_count != 1
            or type(unsupported_total_tasks) is not int
            or unsupported_total_tasks != 1
            or type(unsupported_omitted_tasks) is not int
            or unsupported_omitted_tasks != 0
            or len(groups) != 1
            or not group_exact
        ):
            raise ValueError("protocol status did not expose the exact unsupported queued task")

        current_metrics = self._protocol_metric_counts()
        if current_metrics.get(("other", "QUEUED")) != (
            baseline_metrics.get(("other", "QUEUED"), 0) + 1
        ):
            raise ValueError("protocol-v2 queued metric did not increase by exactly one")
        final_row = self._observe_protocol_v2_fixture(fixture)
        if final_row.get("row_sha256") != fixture.get("row_sha256"):
            raise ValueError("protocol-v2 queued row changed while visibility was inspected")
        self.evidence.protocol_v2_queued_unchanged = True
        self.evidence.protocol_v2_unsupported_visible = True

    def _verify_protocol_v2_rejection(
        self,
        fixture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Submit protocol 2 directly and require its fixed pre-invocation rejection."""

        script = f"""
import json
import os

import ray

from django_ray.conf.settings import get_settings
from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    decode_execution_completion,
    encode_execution_request,
)
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.runner.ray_core import (
    _compiled_graph_submission_transport,
    _get_remote_execute_django_task,
)
from django_ray.runtime.runtime_env import (
    prepare_runtime_env_for_ray_core,
    resolve_runtime_env_profile,
    snapshot_local_runtime_env,
)
from django_ray.workflow_plans import runtime_env_plan_identity

if not ray.is_initialized():
    ray.init(address=os.environ["RAY_ADDRESS"], ignore_reinit_error=True)

runtime_env = resolve_runtime_env_profile("project")
trust_identity = get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {{}})
with snapshot_local_runtime_env(runtime_env) as immutable_snapshot:
    snapshot_identity = runtime_env_plan_identity(
        immutable_snapshot,
        trust_identity=trust_identity,
    )
    submitted_runtime_env = prepare_runtime_env_for_ray_core(immutable_snapshot)

cloudpickle = getattr(ray, "cloudpickle", None)
if cloudpickle is not None:
    import django_ray.runtime.remote as remote_module

    cloudpickle.register_pickle_by_value(remote_module)

identity = ExecutionIdentity(
    task_execution_pk={fixture["pk"]!r},
    task_id={fixture["task_id"]!r},
    attempt_number=1,
    execution_generation=0,
)
poison = {fixture["poison"]!r}
canonical_v1 = encode_execution_request(ExecutionRequest(
    identity=identity,
    execution_protocol_version=1,
    callable_path="testproject.tasks." + poison,
    transport_version=1,
    serialized_args=json.dumps([poison], separators=(",", ":")),
    serialized_kwargs="{{}}",
    input_reference=None,
    runtime_env_profile=runtime_env.profile,
    runtime_env_hash=runtime_env.digest,
    runtime_env_plan_identity=snapshot_identity.as_transport_dict(),
    compiled_graph_submission_transport=_compiled_graph_submission_transport(ray),
))
request = json.loads(canonical_v1)
request["execution_protocol_version"] = 2
protocol_v2 = json.dumps(
    request,
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
)
remote_options = {{"name": "django_ray:protocol-v2-preinvocation"}}
if submitted_runtime_env:
    remote_options["runtime_env"] = submitted_runtime_env
raw_completion = ray.get(
    _get_remote_execute_django_task().options(**remote_options).remote(
        protocol_v2,
        expected_task_execution_pk=identity.task_execution_pk,
        expected_task_id=identity.task_id,
        expected_attempt_number=identity.attempt_number,
        expected_execution_generation=identity.execution_generation,
        expected_execution_protocol_version=2,
    )
)
completion = decode_execution_completion(
    raw_completion,
    expected_identity=identity,
    expected_execution_protocol_version=2,
    supported_protocols=ExecutionProtocolRange(minimum=1, maximum=2),
).completion
prefix = "execution request rejected: "
classification = (
    completion.error[len(prefix):]
    if isinstance(completion.error, str) and completion.error.startswith(prefix)
    else None
)
print(json.dumps({{
    "classification": classification,
    "success": completion.success,
    "retryable": completion.retryable,
    "traceback_absent": completion.traceback is None,
    "result_absent": completion.result is None,
    "result_reference_absent": completion.result_reference is None,
    "exception_type": completion.exception_type,
    "transport_version": request["transport_version"],
    "input_reference_absent": request["input_reference"] is None,
    "application_marker_present": poison in raw_completion,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        outcome = self._sensitive_django_shell(
            script,
            field_name="protocol-v2 direct Ray Core rejection",
        )
        validate_protocol_v2_rejection(outcome)
        final_row = self._observe_protocol_v2_fixture(fixture)
        if final_row.get("row_sha256") != fixture.get("row_sha256"):
            raise ValueError("protocol-v2 queued row changed during direct rejection")
        self.evidence.protocol_v2_preinvocation_rejected = True
        self.evidence.protocol_v2_application_marker_absent = True
        return outcome

    def _verify_protocol_v2_target_execution(
        self,
        fixture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Exercise the private p2 Core seam without creating durable p2 evidence."""

        script = f"""
import json
import os
import platform
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime

import ray

from django_ray.models import RayTaskExecution
from django_ray.ray_target_probe import probe_ray_target
from django_ray.runner.ray_core import (
    RayCoreRunner,
    RayCoreTargetExecutionTransportState,
)
from django_ray.runtime.runtime_env import resolve_runtime_env_profile
from django_ray.target_attestation import (
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    ray_cluster_attestation_digest,
    ray_target_expectation_digest,
)
from django_ray.target_execution_codec import (
    TargetExecutionCompatibilityReason,
    TargetExecutionCompatibilityRejection,
    TargetExecutionCompletion,
    encode_target_execution_result,
)
from django_ray.target_execution_evidence import (
    RayTaskTargetExecutionEvidenceClaim,
    ray_task_target_execution_evidence_digest,
)

if not ray.is_initialized():
    ray.init(address=os.environ["RAY_ADDRESS"], ignore_reinit_error=True)


def canonical_evidence_claim(
    *,
    evidence_id,
    execution_generation,
    expected,
    expectation_digest,
    attestation_digest,
    snapshot_at,
):
    return RayTaskTargetExecutionEvidenceClaim(
            execution_id={fixture["pk"]!r},
            task_id={fixture["task_id"]!r},
            attempt_number=1,
            execution_generation=execution_generation,
            route_selection_id={fixture["pk"]!r},
            route_backend_alias="local-kuberay-private-p2-core",
            route_revision_id=evidence_id,
            route_revision=1,
            selected_target_policy_id=evidence_id,
            target_id=expected.target_key,
            target_policy_id=evidence_id,
            claim_attestation_id=evidence_id,
            target_expectation_digest=expectation_digest,
            claim_attestation_digest=attestation_digest,
            worker_target_capability_id=evidence_id,
            worker_target_capability_schema_version=1,
            worker_target_capability_revision=1,
            worker_target_capability_advertised_at=snapshot_at,
            worker_lease_id="local-kuberay-private-p2-core-lease",
            worker_lease_hostname="local-kuberay-private-p2-core-host",
            worker_lease_pid=1,
            worker_lease_started_at=snapshot_at,
            runner_family=RayRunnerFamily.RAY_CORE.value,
            manager_ray_major=expected.runtime.ray_major,
            manager_ray_minor=expected.runtime.ray_minor,
            manager_ray_patch=expected.runtime.ray_patch,
            manager_python_implementation=expected.runtime.python_implementation,
            manager_python_major=expected.runtime.python_major,
            manager_python_minor=expected.runtime.python_minor,
            manager_python_patch=expected.runtime.python_patch,
            claimed_at=snapshot_at,
        )


def await_target_result(runner, submission):
    deadline = time.monotonic() + 120
    while True:
        results = runner._poll_target_execution_results((submission.pending_handle,))
        if results:
            if len(results) != 1:
                raise RuntimeError("private target execution returned an inexact result count")
            return results[0]
        if time.monotonic() >= deadline:
            runner.cancel(submission)
            raise RuntimeError("private target execution did not finish within its bound")
        time.sleep(0.25)


context = ray.get_runtime_context()
runtime = RayRuntimeVersion(
    ray_major=2,
    ray_minor=56,
    ray_patch=0,
    python_implementation=platform.python_implementation().strip().lower(),
    python_major=sys.version_info.major,
    python_minor=sys.version_info.minor,
    python_patch=sys.version_info.micro,
)
expectation = RayTargetExpectation(
    target_key="local-kuberay-private-p2-core",
    runner_family=RayRunnerFamily.RAY_CORE,
    cluster_session=context.get_session_name(),
    policy_revision=1,
    runtime=runtime,
)
claim_attestation = probe_ray_target(
    expectation,
    ttl_seconds=900,
    timeout_seconds=120,
)
expectation_digest = ray_target_expectation_digest(expectation)
attestation_digest = ray_cluster_attestation_digest(claim_attestation)

execution = RayTaskExecution.objects.get(
    pk={fixture["pk"]!r},
    task_id={fixture["task_id"]!r},
)
runtime_env = resolve_runtime_env_profile("project")
execution.runtime_env_profile = runtime_env.profile
execution.runtime_env_json = runtime_env.serialized
execution.runtime_env_hash = runtime_env.digest
execution.execution_protocol_version = 2
execution.attempt_number = 1
execution.execution_generation = 1
execution.state = "RUNNING"
execution.claimed_by_worker = "local-kuberay-private-p2-core-lease"
execution.callable_path = "testproject.tasks.add_numbers"
execution.args_json = "[2,3]"
execution.kwargs_json = "{{}}"
execution.input_reference = None

exact_evidence_id = int(execution.pk)
exact_claimed_at = datetime.now(UTC)
execution.started_at = exact_claimed_at
execution.finished_at = None
exact_evidence_claim = canonical_evidence_claim(
    evidence_id=exact_evidence_id,
    execution_generation=execution.execution_generation,
    expected=expectation,
    expectation_digest=expectation_digest,
    attestation_digest=attestation_digest,
    snapshot_at=exact_claimed_at,
)
exact_evidence_digest = ray_task_target_execution_evidence_digest(exact_evidence_claim)
runner = RayCoreRunner()
exact_submission = runner._submit_target_execution(
    execution,
    target_execution_evidence_id=exact_evidence_id,
    target_execution_evidence_claim=exact_evidence_claim,
    target_expectation=expectation,
    claim_attestation=claim_attestation,
    claim_attestation_recorded_at=claim_attestation.observed_at,
)
exact_outcome = await_target_result(runner, exact_submission)
exact_result = exact_outcome.result
exact_wire = (
    json.loads(encode_target_execution_result(exact_result))
    if isinstance(exact_result, TargetExecutionCompletion)
    else {{}}
)
exact_observed_proof_bound = (
    isinstance(exact_result, TargetExecutionCompletion)
    and exact_outcome.transport_state is RayCoreTargetExecutionTransportState.COMPLETION
    and exact_outcome.uncertainty is None
    and exact_result.target_execution_evidence_id == exact_evidence_id
    and exact_result.target_execution_evidence_digest == exact_evidence_digest
    and exact_result.target_expectation_digest == expectation_digest
    and exact_result.claim_attestation_digest == attestation_digest
    and exact_result.observed_target.observed_cluster_session == expectation.cluster_session
    and exact_result.observed_target.observed_runtime == expectation.runtime
    and exact_result.observed_target.observed_membership_digest
    == claim_attestation.membership_digest
)

mismatch_runtime = replace(runtime, python_patch=runtime.python_patch + 1)
mismatch_expectation = replace(expectation, runtime=mismatch_runtime)
mismatch_claim_attestation = build_ray_cluster_attestation(
    expectation=mismatch_expectation,
    boundary=claim_attestation.boundary,
    nodes=tuple(
        build_ray_node_observation(
            node_id=node.node_id,
            cluster_session=node.cluster_session,
            runtime=mismatch_runtime,
        )
        for node in claim_attestation.nodes
    ),
    observed_at=claim_attestation.observed_at,
    expires_at=claim_attestation.expires_at,
)
mismatch_expectation_digest = ray_target_expectation_digest(mismatch_expectation)
mismatch_attestation_digest = ray_cluster_attestation_digest(mismatch_claim_attestation)
mismatch_evidence_id = exact_evidence_id + 1
execution.execution_generation = 2
poison = {fixture["poison"]!r}
execution.callable_path = "testproject.tasks.echo_task"
execution.args_json = json.dumps([poison], separators=(",", ":"))
mismatch_claimed_at = datetime.now(UTC)
mismatch_evidence_claim = canonical_evidence_claim(
    evidence_id=mismatch_evidence_id,
    execution_generation=execution.execution_generation,
    expected=mismatch_expectation,
    expectation_digest=mismatch_expectation_digest,
    attestation_digest=mismatch_attestation_digest,
    snapshot_at=mismatch_claimed_at,
)
mismatch_evidence_digest = ray_task_target_execution_evidence_digest(
    mismatch_evidence_claim
)
mismatch_submission = runner._submit_target_execution(
    execution,
    target_execution_evidence_id=mismatch_evidence_id,
    target_execution_evidence_claim=mismatch_evidence_claim,
    target_expectation=mismatch_expectation,
    claim_attestation=mismatch_claim_attestation,
    claim_attestation_recorded_at=mismatch_claim_attestation.observed_at,
)
mismatch_outcome = await_target_result(runner, mismatch_submission)
mismatch_result = mismatch_outcome.result
mismatch_wire_text = (
    encode_target_execution_result(mismatch_result)
    if isinstance(mismatch_result, TargetExecutionCompatibilityRejection)
    else "{{}}"
)
mismatch_wire = json.loads(mismatch_wire_text)
mismatch_observed_proof_bound = (
    isinstance(mismatch_result, TargetExecutionCompatibilityRejection)
    and mismatch_outcome.transport_state
    is RayCoreTargetExecutionTransportState.COMPATIBILITY_REJECTION
    and mismatch_outcome.uncertainty is None
    and mismatch_result.compatibility_reason
    is TargetExecutionCompatibilityReason.PYTHON_VERSION_MISMATCH
    and mismatch_result.target_execution_evidence_id == mismatch_evidence_id
    and mismatch_result.target_execution_evidence_digest == mismatch_evidence_digest
    and mismatch_result.target_expectation_digest == mismatch_expectation_digest
    and mismatch_result.claim_attestation_digest == mismatch_attestation_digest
    and mismatch_result.observed_target.observed_cluster_session
    == mismatch_expectation.cluster_session
    and mismatch_result.observed_target.observed_runtime == runtime
    and mismatch_result.observed_target.observed_membership_digest
    == mismatch_claim_attestation.membership_digest
)

print(json.dumps({{
    "exact_result_kind": exact_wire.get("result_kind"),
    "exact_application_invoked": exact_wire.get("application_invoked"),
    "exact_application_success": (
        exact_result.application_completion.success
        if isinstance(exact_result, TargetExecutionCompletion)
        else None
    ),
    "exact_application_result": (
        exact_result.application_completion.result
        if isinstance(exact_result, TargetExecutionCompletion)
        else None
    ),
    "exact_observed_proof_bound": exact_observed_proof_bound,
    "mismatch_result_kind": mismatch_wire.get("result_kind"),
    "mismatch_reason": (
        mismatch_result.compatibility_reason.value
        if isinstance(mismatch_result, TargetExecutionCompatibilityRejection)
        else None
    ),
    "mismatch_application_invoked": mismatch_wire.get("application_invoked"),
    "mismatch_marker_present": poison in mismatch_wire_text,
    "mismatch_observed_proof_bound": mismatch_observed_proof_bound,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        outcome = self._sensitive_django_shell(
            script,
            field_name="protocol-v2 private target execution",
        )
        validate_protocol_v2_target_execution(outcome)
        final_row = self._observe_protocol_v2_fixture(fixture)
        if final_row.get("row_sha256") != fixture.get("row_sha256"):
            raise ValueError("protocol-v2 queued row changed during private target execution")
        self.evidence.protocol_v2_target_exact_completed = True
        self.evidence.protocol_v2_target_mismatch_rejected = True
        self.evidence.protocol_v2_target_mismatch_marker_absent = True
        return outcome

    def _cleanup_protocol_v2_fixture(
        self,
        fixture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Terminalize the gate row, reopen admission, then delete it exactly."""

        task_id = self._canonical_uuid4(
            fixture.get("task_id"),
            field_name="protocol-v2 cleanup task id",
        )
        expected_pk = fixture.get("pk")
        poison = fixture.get("poison")
        queue_name = fixture.get("queue_name")
        initial_revision = fixture.get("initial_revision")
        closed_revision = fixture.get("closed_revision")
        seed_confirmed = fixture.get("seed_confirmed") is True
        if (
            (expected_pk is not None and (type(expected_pk) is not int or expected_pk < 1))
            or not isinstance(poison, str)
            or PROTOCOL_V2_POISON_PATTERN.fullmatch(poison) is None
            or queue_name != RAY_JOB_GATE_QUEUE
            or type(initial_revision) is not int
            or initial_revision < 1
            or type(closed_revision) is not int
            or closed_revision != initial_revision + 1
        ):
            raise ValueError("protocol-v2 cleanup identity is invalid")
        script = f"""
import json

from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskExecutionProtocolPolicy,
    TaskState,
)
from django_ray.protocol_coordination import reopen_legacy_worker_admission

poison = {poison!r}
queryset = RayTaskExecution.objects.filter(
    task_id={task_id!r},
    execution_protocol_version=2,
    metadata_schema_version=1,
    queue_name={RAY_JOB_GATE_QUEUE!r},
    priority=0,
    attempt_number=1,
    execution_generation=0,
    callable_path="testproject.tasks." + poison,
    args_json=json.dumps([poison], separators=(",", ":")),
    kwargs_json="{{}}",
    claimed_by_worker__isnull=True,
    ray_target_address__isnull=True,
    ray_job_id__isnull=True,
    ray_address__isnull=True,
    ray_job_request_reference__isnull=True,
    input_reference__isnull=True,
    started_at__isnull=True,
    finished_at__isnull=True,
    last_heartbeat_at__isnull=True,
    run_after__isnull=True,
    timeout_seconds__isnull=True,
    queue_timeout_seconds__isnull=True,
    queue_deadline_at__isnull=True,
    result_data__isnull=True,
    result_reference__isnull=True,
    completion_data__isnull=True,
    error_message__isnull=True,
    error_traceback__isnull=True,
    cancellation_status__isnull=True,
    cancellation_error__isnull=True,
)
expected_pk = {expected_pk!r}
if expected_pk is not None:
    queryset = queryset.filter(pk=expected_pk)
matched = queryset.count()
protocols = list(queryset.values_list("execution_protocol_version", flat=True)[:2])
states = list(queryset.values_list("state", flat=True)[:2])
if matched > 1 or len(states) != matched:
    raise RuntimeError("protocol-v2 cleanup identity is ambiguous")
policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
token_count_before = LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).count()
if (
    policy.schema_version != 1
    or policy.active_write_protocol_version != 1
    or (policy.legacy_worker_admission_enabled and token_count_before != 1)
    or (not policy.legacy_worker_admission_enabled and token_count_before != 0)
    or (
        policy.legacy_worker_admission_enabled
        and policy.revision not in [{initial_revision!r}, {initial_revision + 2!r}]
    )
    or (
        not policy.legacy_worker_admission_enabled
        and policy.revision != {closed_revision!r}
    )
):
    raise RuntimeError("protocol-v2 cleanup policy is not exact")
terminalized = 0
if matched == 1:
    state = states[0]
    if state not in [TaskState.FAILED, TaskState.QUEUED]:
        raise RuntimeError("protocol-v2 cleanup row is not staged or queued")
    if policy.legacy_worker_admission_enabled and state == TaskState.QUEUED:
        raise RuntimeError("queued protocol-v2 row exists while legacy admission is open")
    if state == TaskState.QUEUED:
        terminalized = queryset.filter(state=TaskState.QUEUED).update(state=TaskState.FAILED)
        if terminalized != 1:
            raise RuntimeError("protocol-v2 cleanup did not terminalize exactly once")
elif not policy.legacy_worker_admission_enabled:
    raise RuntimeError("closed legacy admission lost the exact protocol-v2 fixture")
if not policy.legacy_worker_admission_enabled:
    if policy.revision != {closed_revision!r}:
        raise RuntimeError("closed legacy admission has an unexpected revision")
    transition = reopen_legacy_worker_admission(expected_revision=int(policy.revision))
    if not transition.enabled:
        raise RuntimeError("legacy admission did not reopen")
elif policy.revision not in [{initial_revision!r}, {initial_revision + 2!r}]:
    raise RuntimeError("open legacy admission has an unexpected revision")
policy.refresh_from_db()
token_count = LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).count()
terminal_queryset = queryset.filter(state=TaskState.FAILED)
deleted, _ = terminal_queryset.delete()
print(json.dumps({{
    "matched": matched,
    "terminalized": terminalized,
    "deleted": deleted,
    "protocols": protocols,
    "states": states,
    "row_absent": not queryset.exists(),
    "schema_version": policy.schema_version,
    "active_write_protocol_version": policy.active_write_protocol_version,
    "legacy_worker_admission_enabled": policy.legacy_worker_admission_enabled,
    "revision": policy.revision,
    "token_count": token_count,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        result = self._sensitive_django_shell(
            script,
            field_name="protocol-v2 fixture and policy cleanup",
        )
        matched = result.get("matched")
        deleted = result.get("deleted")
        terminalized = result.get("terminalized")
        observed_protocols = result.get("protocols")
        observed_states = result.get("states")
        if seed_confirmed:
            row_cleanup_exact = (
                type(matched) is int
                and matched == 1
                and type(deleted) is int
                and deleted == 1
                and observed_protocols == [2]
                and observed_states in (["FAILED"], ["QUEUED"])
                and type(terminalized) is int
                and terminalized == (1 if observed_states == ["QUEUED"] else 0)
            )
        else:
            row_cleanup_exact = (
                type(matched) is int
                and matched in {0, 1}
                and type(deleted) is int
                and deleted == matched
                and observed_protocols in ([], [2])
                and observed_states in ([], ["FAILED"], ["QUEUED"])
                and type(terminalized) is int
                and terminalized == (1 if observed_states == ["QUEUED"] else 0)
            )
        acceptable_revisions = {initial_revision, initial_revision + 2}
        schema_version = result.get("schema_version")
        active_write_protocol = result.get("active_write_protocol_version")
        revision = result.get("revision")
        token_count = result.get("token_count")
        if (
            not row_cleanup_exact
            or result.get("row_absent") is not True
            or type(schema_version) is not int
            or schema_version != 1
            or type(active_write_protocol) is not int
            or active_write_protocol != 1
            or result.get("legacy_worker_admission_enabled") is not True
            or type(revision) is not int
            or revision not in acceptable_revisions
            or (seed_confirmed and revision != initial_revision + 2)
            or type(token_count) is not int
            or token_count != 1
        ):
            raise ValueError("protocol-v2 fixture or admission policy was not restored exactly")
        self._protocol_v2_fixture = None
        return result

    def _verify_protocol_handoff_certification(self) -> None:
        """Certify released-v1 handoff and current-build protocol fencing live."""

        failure: BaseException | None = None
        cleanup_errors: list[tuple[str, object]] = []
        initial_revision: int | None = None
        baseline_metrics: Mapping[tuple[str, str], int] | None = None
        handoff_completed = False
        released_deployment_absent = False
        released_lease_absent = False
        released_manager_phase = False
        startup_recovery_complete = False
        policy_fixture_restored = False
        restored_policy_revision: int | None = None
        manager_restored = False

        try:
            self._recover_protocol_handoff_residue()
            startup_recovery_complete = True
            released_deployment_absent = True
            released_lease_absent = True
            initial = self._observe_protocol_cohorts()
            initial_report = _mapping(
                initial.get("report"),
                field_name="initial protocol status report",
            )
            initial_policy = _mapping(
                initial_report.get("policy"),
                field_name="initial protocol policy",
            )
            initial_revision_value = initial_policy.get("revision")
            initial_legacy_worker_count = initial.get("legacy_worker_count")
            initial_schema_version = initial_policy.get("schema_version")
            initial_active_write = initial_policy.get("active_write_protocol_version")
            if (
                type(initial_schema_version) is not int
                or initial_schema_version != 1
                or type(initial_active_write) is not int
                or initial_active_write != 1
                or initial_policy.get("legacy_worker_admission_enabled") is not True
                or initial_policy.get("legacy_admission_token_present") is not True
                or type(initial_revision_value) is not int
                or initial_revision_value < 1
                or type(initial_legacy_worker_count) is not int
                or initial_legacy_worker_count != 0
            ):
                raise ValueError("protocol handoff did not begin from the exact open-v1 policy")
            initial_revision = initial_revision_value
            self._protocol_handoff_initial_revision = initial_revision
            released_lease_absent = True

            released_manager_phase = True
            released_deployment_absent = False
            released_lease_absent = False
            self._apply_released_v040_manager()
            self._wait_for_protocol_cohorts()
            if self._released_v040_worker_id is None:
                raise ValueError("released v0.4.0 manager did not advertise a legacy lease")

            self._scale_ray_job_manager(0)
            task_id = self._enqueue_released_v040_handoff_task()
            released = self._wait_for_protocol_handoff_task(
                task_id,
                accepted_states=frozenset({"RUNNING"}),
                durable_state="RUNNING",
            )
            released_worker = released.get("worker_id")
            generation = released.get("execution_generation")
            job_id = released.get("job_id")
            released_attempt = released.get("attempt_number")
            released_submission_count = released.get("submission_count")
            if (
                released_worker != self._released_v040_worker_id
                or type(released_attempt) is not int
                or released_attempt != 1
                or type(generation) is not int
                or generation < 0
                or not isinstance(job_id, str)
                or re.fullmatch(r"raysubmit_django_ray_v1_[0-9a-f]{64}", job_id) is None
                or released.get("released_carrier") is not True
                or released.get("request_reference_absent") is not True
                or type(released_submission_count) is not int
                or released_submission_count != 1
            ):
                raise ValueError("released v0.4.0 manager lost the exact v1 submission contract")
            self._register_ray_job_gate_value(job_id)
            queued_fixture = self._enqueue_protocol_v1_survival_task()
            self._require_protocol_v1_survival_unchanged(queued_fixture)

            self._delete_released_v040_manager()
            released_deployment_absent = True
            self._delete_released_v040_worker_lease()
            released_lease_absent = True
            self._scale_ray_job_manager(1)

            adopted = self._wait_for_protocol_handoff_task(
                task_id,
                accepted_states=frozenset({"RUNNING"}),
                different_worker=str(released_worker),
                durable_state="RUNNING",
            )
            adopted_attempt = adopted.get("attempt_number")
            adopted_submission_count = adopted.get("submission_count")
            if (
                adopted.get("job_id") != job_id
                or type(adopted_attempt) is not int
                or adopted_attempt != 1
                or adopted.get("execution_generation") != generation
                or adopted.get("released_carrier") is not True
                or adopted.get("request_reference_absent") is not True
                or type(adopted_submission_count) is not int
                or adopted_submission_count != 1
            ):
                raise ValueError("current manager did not adopt the released v1 Ray Job exactly")
            self.evidence.protocol_v1_handoff_same_job = True
            self.evidence.protocol_v1_handoff_no_resubmit = True
            self._require_protocol_v1_survival_unchanged(queued_fixture)
            self._release_protocol_v1_survival_task(queued_fixture)

            terminal = self._wait_for_protocol_handoff_task(
                task_id,
                accepted_states=frozenset({"SUCCEEDED"}),
                durable_state="SUCCEEDED",
            )
            terminal_attempt = terminal.get("attempt_number")
            terminal_submission_count = terminal.get("submission_count")
            if (
                terminal.get("job_id") != job_id
                or type(terminal_attempt) is not int
                or terminal_attempt != 1
                or terminal.get("execution_generation") != generation
                or terminal.get("released_carrier") is not True
                or terminal.get("request_reference_absent") is not True
                or type(terminal_submission_count) is not int
                or terminal_submission_count != 1
            ):
                raise ValueError("released v1 handoff did not finish on the exact adopted job")

            queued_terminal = self._wait_for_ray_job_gate_task(
                str(queued_fixture["task_id"]),
                accepted_states=frozenset({"SUCCEEDED"}),
                durable_state="SUCCEEDED",
            )
            queued_job_id = queued_terminal.get("job_id")
            queued_generation = queued_terminal.get("execution_generation")
            queued_attempt = queued_terminal.get("attempt_number")
            queued_submission_count = queued_terminal.get("submission_count")
            queued_row = self._observe_protocol_v1_survival_task(queued_fixture)
            if (
                not isinstance(queued_job_id, str)
                or re.fullmatch(r"raysubmit_django_ray_rq2_[0-9a-f]{64}", queued_job_id) is None
                or type(queued_attempt) is not int
                or queued_attempt != 1
                or type(queued_generation) is not int
                or queued_generation < 1
                or type(queued_submission_count) is not int
                or queued_submission_count != 1
                or queued_terminal.get("carrier_ok") is not True
                or queued_terminal.get("binding_ok") is not True
                or queued_terminal.get("request_ok") is not True
                or queued_terminal.get("info_clear") is not True
                or queued_terminal.get("logs_clear") is not True
                or queued_row.get("pk") != queued_fixture.get("pk")
                or queued_row.get("task_id") != queued_fixture.get("task_id")
                or queued_row.get("state") != "SUCCEEDED"
                or queued_row.get("attempt_number") != 1
                or queued_row.get("execution_generation") != queued_generation
                or queued_row.get("ray_job_id") != queued_job_id
                or queued_row.get("ray_job_request_reference") is None
            ):
                raise ValueError("survived protocol-v1 row did not complete exactly once via rq2")
            self._register_ray_job_gate_value(queued_job_id)
            self.evidence.protocol_v1_queued_survived_handoff = True
            self._protocol_v1_survival_fixture = None
            handoff_completed = True

            baseline_metrics = self._protocol_metric_counts()
            fixture = self._seed_protocol_v2_fixture()
            self._verify_protocol_v2_visibility(
                fixture,
                baseline_metrics=baseline_metrics,
            )
            self._verify_protocol_v2_rejection(fixture)
            self._verify_protocol_v2_target_execution(fixture)
        except BaseException as error:
            failure = error
        finally:
            if released_manager_phase and self._released_v040_worker_id is None:
                try:
                    cleanup_cohorts = self._observe_protocol_cohorts()
                    cleanup_worker_ids = cleanup_cohorts.get("legacy_worker_ids")
                    cleanup_worker_count = cleanup_cohorts.get("legacy_worker_count")
                    if (
                        type(cleanup_worker_count) is int
                        and cleanup_worker_count == 0
                        and cleanup_worker_ids == []
                    ):
                        released_lease_absent = self._released_v040_hostname is None
                    elif not (
                        type(cleanup_worker_count) is int
                        and cleanup_worker_count == 1
                        and isinstance(cleanup_worker_ids, list)
                        and len(cleanup_worker_ids) == 1
                    ):
                        raise ValueError(
                            "gate-owned live legacy lease could not be identified exactly"
                        )
                    else:
                        cleanup_worker_id = self._canonical_uuid4(
                            cleanup_worker_ids[0],
                            field_name="released v0.4.0 cleanup worker id",
                        )
                        self._released_v040_worker_id = cleanup_worker_id
                        self._register_ray_job_gate_value(cleanup_worker_id)
                except BaseException as error:
                    cleanup_errors.append(("released lease identity recovery", error))
            if (
                released_manager_phase
                or startup_recovery_complete
                or self._released_v040_hostname is not None
            ):
                try:
                    self._delete_released_v040_manager()
                    released_deployment_absent = True
                except BaseException as error:
                    cleanup_errors.append(("released manager cleanup", error))

            if (
                (released_manager_phase or self._released_v040_hostname is not None)
                and released_deployment_absent
                and not released_lease_absent
            ):
                try:
                    self._delete_released_v040_worker_lease()
                    released_lease_absent = True
                    self._released_v040_worker_id = None
                    self._released_v040_hostname = None
                except BaseException as error:
                    cleanup_errors.append(("released lease cleanup", error))

            queued_fixture = self._protocol_v1_survival_fixture
            if queued_fixture is not None:
                try:
                    self._cleanup_protocol_v1_survival_task(queued_fixture)
                except BaseException as error:
                    cleanup_errors.append(("protocol-v1 survival fixture cleanup", error))

            try:
                self._scale_ray_job_manager(1)
                manager_restored = True
            except BaseException as error:
                cleanup_errors.append(("current manager replica restoration", error))

            fixture = self._protocol_v2_fixture
            if fixture is not None:
                try:
                    cleanup_result = self._cleanup_protocol_v2_fixture(fixture)
                    cleanup_revision = cleanup_result.get("revision")
                    if type(cleanup_revision) is not int:
                        raise ValueError("protocol cleanup returned an invalid revision")
                    restored_policy_revision = cleanup_revision
                    policy_fixture_restored = True
                except BaseException as error:
                    cleanup_errors.append(("protocol-v2 fixture and policy cleanup", error))
            else:
                policy_fixture_restored = True
                restored_policy_revision = initial_revision

            if manager_restored:
                try:
                    self._wait_for_application_topology()
                    manager = self._ray_job_manager_replica_observation()
                    if manager != {"replicas": 1, "ready_replicas": 1}:
                        raise ValueError(
                            "current Ray Job manager replica restoration was not exact"
                        )
                except BaseException as error:
                    manager_restored = False
                    cleanup_errors.append(("current manager topology verification", error))

            try:
                final = self._observe_protocol_cohorts()
                final_report = _mapping(
                    final.get("report"),
                    field_name="restored protocol status report",
                )
                final_policy = _mapping(
                    final_report.get("policy"),
                    field_name="restored protocol policy",
                )
                expected_revision = restored_policy_revision
                final_legacy_worker_count = final.get("legacy_worker_count")
                final_schema_version = final_policy.get("schema_version")
                final_active_write = final_policy.get("active_write_protocol_version")
                final_revision = final_policy.get("revision")
                if initial_revision is not None and (
                    type(final_schema_version) is not int
                    or final_schema_version != 1
                    or type(final_active_write) is not int
                    or final_active_write != 1
                    or final_policy.get("legacy_worker_admission_enabled") is not True
                    or final_policy.get("legacy_admission_token_present") is not True
                    or type(final_revision) is not int
                    or final_revision != expected_revision
                    or type(final_legacy_worker_count) is not int
                    or final_legacy_worker_count != 0
                ):
                    raise ValueError("execution-protocol policy was not restored exactly")
            except BaseException as error:
                policy_fixture_restored = False
                cleanup_errors.append(("protocol policy restoration verification", error))

            if (
                released_deployment_absent
                and released_lease_absent
                and manager_restored
                and policy_fixture_restored
                and not cleanup_errors
            ):
                self.evidence.protocol_handoff_cleanup_restored = True

        if failure is not None:
            if cleanup_errors:
                failure.add_note(
                    _gate_error_detail(
                        "protocol handoff cleanup also failed",
                        redactor=self.redactor,
                        contexts=tuple(cleanup_errors),
                    )
                )
            raise failure.with_traceback(failure.__traceback__)
        if cleanup_errors:
            raise ValueError(
                _gate_error_detail(
                    "protocol handoff cleanup failed",
                    redactor=self.redactor,
                    contexts=tuple(cleanup_errors),
                )
            )
        if not handoff_completed:
            raise ValueError("protocol-v1 handoff certification did not complete")

    def _submit_missing_ray_job_request_reference(self, marker: str) -> Mapping[str, Any]:
        script = f"""
import copy
import json

from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskState
from django_ray.ray_job_request_storage import (
    _storage_for_locator,
    decode_ray_job_request_locator,
)
from django_ray.runner.ray_job import RayJobRunner
from testproject.tasks import echo_task

result = echo_task.using(
    backend={RAY_JOB_GATE_QUEUE!r},
    queue_name={RAY_JOB_GATE_QUEUE!r},
).enqueue({marker!r})
row = RayTaskExecution.objects.get(task_id=result.id)
row.callable_path = "builtins.print"
row.state = TaskState.RUNNING
row.claimed_by_worker = "rq2-gate-missing-reference"
row.started_at = timezone.now()
row.last_heartbeat_at = row.started_at
runner = RayJobRunner()
handle = runner.submission_handle(row)
row.ray_job_id = handle.ray_job_id
row.ray_address = handle.ray_address
row.save(update_fields=[
    "callable_path",
    "state",
    "claimed_by_worker",
    "started_at",
    "last_heartbeat_at",
    "ray_job_id",
    "ray_address",
])

real_client = runner._get_client(handle.ray_address)
captured = {{}}

class CaptureClient:
    def _upload_working_dir_if_needed(self, runtime_env):
        return real_client._upload_working_dir_if_needed(runtime_env)

    def _upload_py_modules_if_needed(self, runtime_env):
        return real_client._upload_py_modules_if_needed(runtime_env)

    def submit_job(self, **kwargs):
        captured.update(copy.deepcopy(kwargs))
        return kwargs["submission_id"]

runner._get_client = lambda _address=None: CaptureClient()
submitted = runner.submit_durable(row)
if submitted.ray_job_id != handle.ray_job_id:
    raise AssertionError("captured rq2 submission changed its stable ID")
entrypoint = captured.get("entrypoint", "")
parts = entrypoint.split()
if (
    len(parts) != 5
    or parts[:4] != ["python", "-m", "django_ray.runtime.entrypoint", {RAY_JOB_REQUEST_REFERENCE_CARRIER!r}]
):
    raise AssertionError("captured rq2 submission did not use the request-reference carrier")
locator = decode_ray_job_request_locator(parts[4])
if locator.reference != row.ray_job_request_reference:
    raise AssertionError("captured rq2 locator changed the durable request reference")
_storage_for_locator(locator).delete(reference=locator.reference)
returned = real_client.submit_job(**captured)
if returned != handle.ray_job_id:
    raise AssertionError("missing-reference submission returned an unexpected ID")
print(json.dumps({{
    "task_id": row.task_id,
    "job_id": row.ray_job_id,
    "attempt_number": row.attempt_number,
    "execution_generation": row.execution_generation,
}}, sort_keys=True, separators=(",", ":")))
""".strip()
        return self._sensitive_django_shell(
            script,
            field_name="rq2 missing-reference submission",
        )

    def _observe_missing_ray_job_request_reference(
        self,
        *,
        task_id: str,
        marker: str,
    ) -> Mapping[str, Any]:
        script = f"""
import json
import os
import shlex

from django_ray.execution_codec import ExecutionIdentity
from django_ray.models import RayTaskExecution
from django_ray.ray_job_protocol import (
    RayJobRequestReferenceExpectation,
    parse_ray_job_request_metadata,
    validate_ray_job_request_reference_expectation,
)
from django_ray.ray_job_request_storage import ray_job_request_reference_content_identity
from django_ray.runner.ray_job import _address_pinned_job_client

row = RayTaskExecution.objects.get(task_id={task_id!r})
client = _address_pinned_job_client(row.ray_address)
info = client.get_job_info(row.ray_job_id)
entrypoint = getattr(info, "entrypoint", "")
parts = shlex.split(entrypoint)
carrier_ok = (
    len(parts) == 5
    and parts[:4] == ["python", "-m", "django_ray.runtime.entrypoint", {RAY_JOB_REQUEST_REFERENCE_CARRIER!r}]
    and {RAY_JOB_RELEASED_PAYLOAD_CARRIER!r} not in parts
)
expectation = parse_ray_job_request_metadata(getattr(info, "metadata", None), required=True)
request_digest, request_size = ray_job_request_reference_content_identity(
    row.ray_job_request_reference
)
binding_ok = isinstance(expectation, RayJobRequestReferenceExpectation)
if binding_ok:
    validate_ray_job_request_reference_expectation(
        expectation,
        expected_identity=ExecutionIdentity(
            task_execution_pk=row.pk,
            task_id=row.task_id,
            attempt_number=row.attempt_number,
            execution_generation=row.execution_generation,
        ),
        expected_execution_protocol_version=row.execution_protocol_version,
        expected_request_sha256=request_digest,
        expected_request_size_bytes=request_size,
        expected_submission_id=row.ray_job_id,
        request_reference=row.ray_job_request_reference,
        request_locator=parts[4] if carrier_ok else None,
    )
binding_ok = binding_ok and carrier_ok
if hasattr(info, "model_dump"):
    info_payload = info.model_dump()
elif hasattr(info, "dict"):
    info_payload = info.dict()
else:
    info_payload = vars(info)
raw_info = json.dumps(info_payload, sort_keys=True, separators=(",", ":"), default=str)
raw_logs = str(client.get_job_logs(row.ray_job_id) or "")
credential_values = [
    value
    for key, value in os.environ.items()
    if value
    and any(token in key.upper() for token in ("PASSWORD", "SECRET", "TOKEN", "API_KEY"))
]
forbidden = [
    row.task_id,
    row.callable_path,
    row.args_json,
    row.kwargs_json,
    {marker!r},
    *credential_values,
]
forbidden = [value for value in forbidden if value]
submissions = [
    job
    for job in client.list_jobs()
    if getattr(job, "submission_id", None) == row.ray_job_id
]
print(json.dumps({{
    "state": str(getattr(info, "status", "")),
    "durable_state": str(row.state),
    "attempt_number": row.attempt_number,
    "execution_generation": row.execution_generation,
    "job_id": row.ray_job_id,
    "carrier_ok": carrier_ok,
    "binding_ok": binding_ok,
    "info_clear": all(value not in raw_info for value in forbidden),
    "logs_clear": all(value not in raw_logs for value in forbidden),
    "submission_count": len(submissions),
}}, sort_keys=True, separators=(",", ":"), default=str))
""".strip()
        return self._sensitive_django_shell(
            script,
            field_name="rq2 missing-reference observation",
        )

    def _wait_for_missing_ray_job_failure(
        self,
        *,
        task_id: str,
        marker: str,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.config.task_timeout
        last_state = "missing"
        while True:
            observation = self._observe_missing_ray_job_request_reference(
                task_id=task_id,
                marker=marker,
            )
            state = self._ray_job_gate_status(
                observation.get("state"),
                field_name="rq2 missing-reference Ray Job",
            )
            last_state = state
            if state == "FAILED":
                return observation
            if state in {"SUCCEEDED", "STOPPED"}:
                raise ValueError(f"rq2 missing-reference Ray Job reached unexpected state {state}")
            if time.monotonic() >= deadline:
                raise ValueError(
                    "rq2 missing-reference Ray Job did not fail within "
                    f"{self.config.task_timeout}s (last state: {last_state})"
                )
            time.sleep(2)

    def _age_missing_ray_job_execution(self, task_id: str) -> None:
        script = f"""
import json
from datetime import timedelta

from django.utils import timezone

from django_ray.models import RayTaskExecution

stale = timezone.now() - timedelta(minutes=10)
updated = RayTaskExecution.objects.filter(task_id={task_id!r}).update(
    started_at=stale,
    last_heartbeat_at=stale,
)
print(json.dumps({{"updated": updated}}))
""".strip()
        payload = self._sensitive_django_shell(
            script,
            field_name="rq2 missing-reference stale fence",
        )
        if payload.get("updated") != 1:
            raise ValueError("rq2 missing-reference stale fence did not update exactly one row")

    def _observe_missing_ray_job_disposition(self, task_id: str) -> Mapping[str, Any]:
        script = f"""
import json

from django_ray.models import RayTaskExecution, TaskAttempt
from django_ray.runner.ray_job import _address_pinned_job_client

row = RayTaskExecution.objects.get(task_id={task_id!r})
client = _address_pinned_job_client(row.ray_address)
submissions = [
    job
    for job in client.list_jobs()
    if getattr(job, "submission_id", None) == row.ray_job_id
]
print(json.dumps({{
    "state": str(row.state),
    "attempt_number": row.attempt_number,
    "execution_generation": row.execution_generation,
    "run_after_is_none": row.run_after is None,
    "completion_is_none": row.completion_data is None,
    "result_is_none": row.result_data is None,
    "fixed_error": row.error_message == "Strict Ray Job terminated without an exact completion envelope",
    "archived_attempts": TaskAttempt.objects.filter(execution=row).count(),
    "submission_count": len(submissions),
}}, sort_keys=True, separators=(",", ":"), default=str))
""".strip()
        return self._sensitive_django_shell(
            script,
            field_name="rq2 missing-reference disposition",
        )

    def _wait_for_missing_ray_job_disposition(self, task_id: str) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.config.task_timeout
        last_state = "missing"
        while True:
            observation = self._observe_missing_ray_job_disposition(task_id)
            state = observation.get("state")
            if isinstance(state, str):
                last_state = state
            if state == "FAILED":
                return observation
            if state == "QUEUED" or observation.get("attempt_number") != 1:
                raise ValueError("rq2 missing-reference execution started an automatic retry")
            if time.monotonic() >= deadline:
                raise ValueError(
                    "rq2 missing-reference execution did not terminalize within "
                    f"{self.config.task_timeout}s (last state: {last_state})"
                )
            time.sleep(2)

    def _verify_missing_ray_job_request_reference(self) -> None:
        marker = f"django-ray-rq2-missing-{secrets.token_hex(16)}"
        self._register_ray_job_gate_value(marker)
        manager_scaled_down = False
        manager_restored = False
        try:
            self._scale_ray_job_manager(0)
            manager_scaled_down = True
            submitted = self._submit_missing_ray_job_request_reference(marker)
            task_id = self._canonical_uuid4(
                submitted.get("task_id"),
                field_name="rq2 missing-reference task id",
            )
            self._register_ray_job_gate_value(task_id)
            self._register_ray_job_gate_value("builtins.print")
            job_id = submitted.get("job_id")
            if not isinstance(job_id, str) or not job_id or submitted.get("attempt_number") != 1:
                raise ValueError("rq2 missing-reference submission returned invalid identity")

            failed = self._wait_for_missing_ray_job_failure(
                task_id=task_id,
                marker=marker,
            )
            if (
                failed.get("job_id") != job_id
                or failed.get("carrier_ok") is not True
                or failed.get("binding_ok") is not True
                or failed.get("info_clear") is not True
                or failed.get("logs_clear") is not True
                or failed.get("submission_count") != 1
                or failed.get("attempt_number") != 1
            ):
                raise ValueError("rq2 missing-reference failure exposed or changed its request")
            if not self._ray_process_surfaces_clear(
                (
                    task_id,
                    marker,
                    "builtins.print",
                    RAY_JOB_RELEASED_PAYLOAD_CARRIER,
                    *self._decoded_credential_values(),
                )
            ):
                raise ValueError("rq2 missing-reference process surface exposed its marker")
            self.evidence.ray_job_missing_reference_no_marker = True

            self._age_missing_ray_job_execution(task_id)
            self._scale_ray_job_manager(1)
            manager_restored = True
            self._wait_for_application_topology()
            disposition = self._wait_for_missing_ray_job_disposition(task_id)
            if (
                disposition.get("state") != "FAILED"
                or disposition.get("attempt_number") != 1
                or disposition.get("execution_generation") != submitted.get("execution_generation")
                or disposition.get("run_after_is_none") is not True
                or disposition.get("completion_is_none") is not True
                or disposition.get("result_is_none") is not True
                or disposition.get("fixed_error") is not True
                or disposition.get("archived_attempts") != 1
                or disposition.get("submission_count") != 1
            ):
                raise ValueError("rq2 missing-reference failure was retried or lost its fence")
            self.evidence.ray_job_missing_reference_no_retry = True
        finally:
            if manager_scaled_down and not manager_restored:
                self._scale_ray_job_manager(1)
                self._wait_for_application_topology()

    def _register_runtime_env_protected_value(self, value: str) -> None:
        """Register one gate-only storage value before any later diagnostics."""
        if not value:
            return
        if value not in self._runtime_env_protected_values:
            self._runtime_env_protected_values.append(value)
        self.redactor.register(value)
        if self.runner.redactor is not self.redactor:
            self.runner.redactor.register(value)

    def _assert_runtime_env_values_absent(self, value: object, *, surface: str) -> None:
        """Reject a protected storage value without including it in the failure."""
        serialized = (
            value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        )
        if any(protected in serialized for protected in self._runtime_env_protected_values):
            raise ValueError(f"{surface} exposed a protected RuntimeEnv storage value")

    @staticmethod
    def _canonical_uuid4(value: object, *, field_name: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} is missing")
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise ValueError(f"{field_name} is not a canonical UUID") from error
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError(f"{field_name} is not a canonical UUIDv4")
        return str(parsed)

    def _sensitive_django_shell(
        self,
        script: str,
        *,
        field_name: str,
    ) -> Mapping[str, Any]:
        """Run one bounded in-pod inspector whose successful stdout stays private."""
        completion_marker = f"django_ray_private_json_complete_v1_{uuid4().hex}"
        wrapped_script = f"{script.rstrip()}\nprint({completion_marker!r})\n"
        result = self._kubectl(
            "exec",
            "deployment/django-web",
            "-c",
            "django-web",
            "--",
            "python",
            "testproject/manage.py",
            "shell",
            "--no-imports",
            "-c",
            wrapped_script,
            sensitive_output=True,
            timeout=self.config.command_timeout,
        )
        if len(result.stdout) > MAX_OUTPUT_CHARACTERS:
            raise ValueError(f"{field_name} exceeded the private JSON size limit")
        return _parse_single_json_object_line_without_cause(
            result.stdout,
            completion_marker=completion_marker,
            error_message=f"{field_name} did not return valid private JSON",
        )

    def _poll_runtime_env_canary(
        self,
        task_id: str,
        *,
        headers: Mapping[str, str],
        surfaces: list[bytes],
    ) -> Mapping[str, Any]:
        """Poll the public sanitized RuntimeEnv result until it is terminal."""
        deadline = time.monotonic() + self.config.task_timeout
        last_state = "missing"
        path = RUNTIME_ENV_ENCRYPTION_RESULT_PATH.format(task_id=task_id)
        while True:
            status, body = self._http(path, method="GET", headers=headers)
            surfaces.append(body)
            if status != 200:
                raise ValueError(
                    f"RuntimeEnv encryption canary polling returned {status}, expected 200"
                )
            execution = self._json_body(body, endpoint="RuntimeEnv encryption canary polling")
            self._observe_bounded_poll_projection(
                execution,
                family="runtime_env",
                surface="RuntimeEnv encryption canary polling",
                task_id=task_id,
            )
            if execution.get("task_id") != task_id:
                raise ValueError("RuntimeEnv encryption canary polling returned the wrong task")
            last_state = str(execution.get("state"))
            if last_state == "SUCCEEDED":
                if execution.get("runtime_env_profile") != "thin":
                    raise ValueError("RuntimeEnv encryption canary did not retain the thin profile")
                digest = execution.get("runtime_env_hash")
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise ValueError(
                        "RuntimeEnv encryption canary returned an invalid content hash"
                    )
                result = _mapping(
                    execution.get("result"),
                    field_name="RuntimeEnv encryption canary result",
                )
                if result.get("storage_encryption_verified") is not True:
                    raise ValueError(
                        "RuntimeEnv encryption canary did not observe the fixed Ray marker"
                    )
                if execution.get("error") is not None:
                    raise ValueError("RuntimeEnv encryption canary succeeded with an error")
                return execution
            if last_state in TASK_FAILURE_STATES:
                raise ValueError(
                    f"RuntimeEnv encryption canary reached terminal state {last_state}"
                )
            if time.monotonic() >= deadline:
                raise ValueError(
                    "RuntimeEnv encryption canary did not reach SUCCEEDED within "
                    f"{self.config.task_timeout}s (last state: {last_state})"
                )
            time.sleep(2)

    def _inspect_runtime_env_canary_envelope(
        self,
        *,
        task_id: str,
        profile: str,
        digest: str,
    ) -> None:
        """Inspect and protect the raw canary envelope without emitting it."""
        script = "\n".join(
            (
                "import json",
                "from django_ray.models import RayTaskExecution",
                (
                    "row = RayTaskExecution.objects.only("
                    "'runtime_env_json', 'runtime_env_profile', 'runtime_env_hash'"
                    f").get(task_id={task_id!r})"
                ),
                (
                    "print(json.dumps({"
                    "'envelope': row.runtime_env_json, "
                    "'profile': row.runtime_env_profile, "
                    "'runtime_env_hash': row.runtime_env_hash"
                    "}, sort_keys=True, separators=(',', ':')))"
                ),
            )
        )
        payload = self._sensitive_django_shell(
            script,
            field_name="RuntimeEnv encryption envelope inspection",
        )
        envelope = payload.get("envelope")
        if not isinstance(envelope, str) or not envelope:
            raise ValueError("RuntimeEnv encryption envelope inspection returned no envelope")
        self._register_runtime_env_protected_value(envelope)

        parsed: object | None
        try:
            parsed = json.loads(envelope)
        except (TypeError, ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, Mapping):
            for field in ("nonce", "ciphertext"):
                protected = parsed.get(field)
                if isinstance(protected, str) and protected:
                    self._register_runtime_env_protected_value(protected)

        nonce, ciphertext = validate_runtime_env_encryption_envelope(envelope)
        self._register_runtime_env_protected_value(nonce)
        self._register_runtime_env_protected_value(ciphertext)
        if payload.get("profile") != profile or payload.get("runtime_env_hash") != digest:
            raise ValueError(
                "RuntimeEnv encryption envelope identity did not match the sanitized API"
            )
        self.evidence.runtime_env_encryption_envelope = True
        self.evidence.runtime_env_encryption_marker_absent = True

    def _create_runtime_env_failure_fixtures(self) -> dict[str, tuple[int, str]]:
        """Atomically create two encrypted rows corrupted before workers can claim them."""
        payload = self._sensitive_django_shell(
            RUNTIME_ENV_FAILURE_FIXTURE_SCRIPT,
            field_name="RuntimeEnv encryption failure fixtures",
        )
        if set(payload) != {"ciphertext", "key_id"}:
            raise ValueError("RuntimeEnv encryption failure fixture set is incomplete")
        for label in ("ciphertext", "key_id"):
            raw_fixture = payload.get(label)
            if not isinstance(raw_fixture, Mapping):
                continue
            for protected_field in ("envelope", "nonce", "ciphertext"):
                protected = raw_fixture.get(protected_field)
                if isinstance(protected, str) and protected:
                    self._register_runtime_env_protected_value(protected)
        fixtures: dict[str, tuple[int, str]] = {}
        for label in ("ciphertext", "key_id"):
            fixture = _mapping(
                payload.get(label),
                field_name=f"RuntimeEnv encryption {label} fixture",
            )
            if set(fixture) != {"id", "task_id", "envelope", "nonce", "ciphertext"}:
                raise ValueError(f"RuntimeEnv encryption {label} fixture fields are invalid")
            envelope = fixture.get("envelope")
            nonce = fixture.get("nonce")
            ciphertext = fixture.get("ciphertext")
            if (
                not isinstance(envelope, str)
                or not isinstance(nonce, str)
                or not isinstance(ciphertext, str)
                or RUNTIME_ENV_STORAGE_PROBE_MARKER in envelope
            ):
                raise ValueError(f"RuntimeEnv encryption {label} fixture values are invalid")
            try:
                parsed = json.loads(envelope)
            except (TypeError, ValueError, RecursionError):
                parsed = None
            if (
                not isinstance(parsed, Mapping)
                or set(parsed) != RUNTIME_ENV_ENVELOPE_FIELDS
                or json.dumps(
                    parsed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                != envelope
                or parsed.get("nonce") != nonce
                or parsed.get("ciphertext") != ciphertext
                or parsed.get("format") != RUNTIME_ENV_ENVELOPE_FORMAT
                or parsed.get("version") != RUNTIME_ENV_ENVELOPE_VERSION
                or parsed.get("algorithm") != RUNTIME_ENV_ENVELOPE_ALGORITHM
                or parsed.get("key_id")
                != (
                    "django-secret" if label == "ciphertext" else RUNTIME_ENV_FAILURE_UNKNOWN_KEY_ID
                )
                or _decode_canonical_base64url(nonce, exact_bytes=12) is None
                or _decode_canonical_base64url(ciphertext, minimum_bytes=16) is None
            ):
                raise ValueError(f"RuntimeEnv encryption {label} fixture envelope is invalid")
            execution_id = fixture.get("id")
            if (
                isinstance(execution_id, bool)
                or not isinstance(execution_id, int)
                or execution_id < 1
            ):
                raise ValueError(f"RuntimeEnv encryption {label} fixture ID is invalid")
            task_id = self._canonical_uuid4(
                fixture.get("task_id"),
                field_name=f"RuntimeEnv encryption {label} fixture task_id",
            )
            fixtures[label] = (execution_id, task_id)
        self._runtime_env_fixture_values_registered = True
        return fixtures

    def _poll_runtime_env_failure(
        self,
        *,
        execution_id: int,
        task_id: str,
        headers: Mapping[str, str],
        surfaces: list[bytes],
    ) -> None:
        """Wait for a corrupt snapshot to fail permanently through the task manager."""
        deadline = time.monotonic() + self.config.task_timeout
        last_state = "missing"
        query = urlencode({"task_id": task_id, "limit": 1})
        while True:
            status, body = self._http(
                f"/api/executions?{query}",
                method="GET",
                headers=headers,
            )
            surfaces.append(body)
            if status != 200:
                raise ValueError(f"RuntimeEnv corruption polling returned {status}, expected 200")
            listing = self._json_body(body, endpoint="RuntimeEnv corruption polling")
            tasks = _sequence(listing.get("tasks"), field_name="RuntimeEnv corruption tasks")
            execution = next(
                (
                    _mapping(value, field_name="RuntimeEnv corruption execution")
                    for value in tasks
                    if isinstance(value, Mapping) and value.get("task_id") == task_id
                ),
                None,
            )
            if execution is not None:
                if execution.get("id") != execution_id:
                    raise ValueError("RuntimeEnv corruption polling returned the wrong row")
                last_state = str(execution.get("state"))
                if last_state == "FAILED":
                    if (
                        execution.get("attempt_number") != 1
                        or execution.get("execution_generation") != 1
                        or execution.get("result_data") is not None
                        or execution.get("runtime_env_profile") != "thin"
                    ):
                        raise ValueError(
                            "RuntimeEnv corruption API lifecycle evidence is inconsistent"
                        )
                    return
                if last_state in (TASK_FAILURE_STATES - {"FAILED"}) | {"SUCCEEDED"}:
                    raise ValueError(
                        f"RuntimeEnv corruption reached unexpected terminal state {last_state}"
                    )
            if time.monotonic() >= deadline:
                raise ValueError(
                    "RuntimeEnv corruption did not reach FAILED within "
                    f"{self.config.task_timeout}s (last state: {last_state})"
                )
            time.sleep(2)

    def _runtime_env_failure_invariants(
        self,
        fixtures: Mapping[str, tuple[int, str]],
    ) -> Mapping[str, Any]:
        """Read only scalar pre-Ray and attempt-history invariants for fresh fixtures."""
        identifiers = {label: execution_id for label, (execution_id, _) in fixtures.items()}
        script = "\n".join(
            (
                "import hashlib",
                "import json",
                "from django.core.serializers.json import DjangoJSONEncoder",
                "from django_ray.models import RayTaskExecution",
                f"identifiers = {identifiers!r}",
                "def concrete_fields(instance):",
                "    return {",
                "        field.attname: field.value_from_object(instance)",
                "        for field in instance._meta.concrete_fields",
                "    }",
                "observations = {}",
                "for label, execution_id in identifiers.items():",
                "    row = RayTaskExecution.objects.get(pk=execution_id)",
                "    error = (row.error_message or '').lower()",
                "    attempt_rows = list(row.attempts.order_by('attempt_number', 'pk'))",
                "    attempts = list(row.attempts.order_by('attempt_number').values(",
                "        'attempt_number', 'state'",
                "    ))",
                "    archive = {",
                "        'row': concrete_fields(row),",
                "        'attempts': [concrete_fields(attempt) for attempt in attempt_rows],",
                "    }",
                "    archive_json = json.dumps(",
                "        archive,",
                "        sort_keys=True,",
                "        separators=(',', ':'),",
                "        cls=DjangoJSONEncoder,",
                "    )",
                "    observations[label] = {",
                "        'archive_fingerprint': hashlib.sha256(archive_json.encode()).hexdigest(),",
                "        'state': row.state,",
                "        'attempt_number': row.attempt_number,",
                "        'execution_generation': row.execution_generation,",
                "        'claimed': bool(row.claimed_by_worker),",
                "        'lifecycle_timestamps': bool(row.started_at and row.finished_at),",
                "        'no_ray_submission': bool(",
                "            row.ray_job_id is None",
                "            and row.ray_address is None",
                "            and row.completion_data is None",
                "        ),",
                "        'no_result': bool(",
                "            row.result_data is None",
                "            and row.result_reference is None",
                "            and row.progress_data is None",
                "        ),",
                "        'attempts': attempts,",
                "        'authentication_failed': 'authentication failed' in error,",
                "        'key_unavailable': 'decryption key is unavailable' in error,",
                "    }",
                "print(json.dumps(observations, sort_keys=True, separators=(',', ':')))",
            )
        )
        return self._sensitive_django_shell(
            script,
            field_name="RuntimeEnv encryption failure invariants",
        )

    @staticmethod
    def _validate_runtime_env_failure_invariants(
        observations: Mapping[str, Any],
    ) -> None:
        if set(observations) != {"ciphertext", "key_id"}:
            raise ValueError("RuntimeEnv corruption invariant set is incomplete")
        expected_attempts = [{"attempt_number": 1, "state": "FAILED"}]
        for label in ("ciphertext", "key_id"):
            observation = _mapping(
                observations.get(label),
                field_name=f"RuntimeEnv {label} failure invariants",
            )
            archive_fingerprint = observation.get("archive_fingerprint")
            if (
                not isinstance(archive_fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", archive_fingerprint) is None
                or observation.get("state") != "FAILED"
                or observation.get("attempt_number") != 1
                or observation.get("execution_generation") != 1
                or observation.get("claimed") is not True
                or observation.get("lifecycle_timestamps") is not True
                or observation.get("no_ray_submission") is not True
                or observation.get("no_result") is not True
                or observation.get("attempts") != expected_attempts
            ):
                raise ValueError(f"RuntimeEnv {label} failure crossed a lifecycle boundary")
        ciphertext = _mapping(
            observations.get("ciphertext"),
            field_name="RuntimeEnv ciphertext failure invariants",
        )
        unknown = _mapping(
            observations.get("key_id"),
            field_name="RuntimeEnv unknown-key failure invariants",
        )
        if (
            ciphertext.get("authentication_failed") is not True
            or ciphertext.get("key_unavailable") is not False
            or unknown.get("key_unavailable") is not True
            or unknown.get("authentication_failed") is not False
        ):
            raise ValueError("RuntimeEnv corruption failures were not classified safely")

    def _verify_runtime_env_logs_clear(self) -> None:
        """Search current API/admin and task-manager logs for protected values."""
        commands = (
            (
                "API/admin",
                (
                    "logs",
                    "deployment/django-web",
                    "--all-containers=true",
                    "--since=15m",
                    "--tail=-1",
                    "--limit-bytes=1048576",
                ),
            ),
            (
                "task-manager",
                (
                    "logs",
                    "-l",
                    "app=django-ray,component=worker",
                    "--all-containers=true",
                    "--since=15m",
                    "--tail=-1",
                    "--limit-bytes=1048576",
                    "--max-log-requests=20",
                    "--prefix=true",
                ),
            ),
        )
        for label, command in commands:
            result = self._kubectl(
                *command,
                timeout=min(self.config.command_timeout, 60),
            )
            combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
            self._assert_runtime_env_values_absent(
                combined,
                surface=f"{label} logs",
            )
        self.evidence.runtime_env_encryption_logs_clear = True

    def _verify_runtime_env_encryption(self) -> None:
        """Prove encrypted storage, Ray delivery, and fail-closed corruption paths."""
        inspect_runtime_env_encryption_overlay(self.resources)
        self.evidence.runtime_env_encryption_overlay = True
        token = self._secret_token()
        headers = {"Authorization": f"Bearer {token}"}
        surfaces: list[bytes] = []

        status, body = self._http(
            RUNTIME_ENV_ENCRYPTION_PROBE_PATH,
            method="POST",
            headers=headers,
        )
        surfaces.append(body)
        if status != 200:
            raise ValueError(
                f"RuntimeEnv encryption canary enqueue returned {status}, expected 200"
            )
        enqueue = self._json_body(body, endpoint="RuntimeEnv encryption canary enqueue")
        task_id = self._canonical_uuid4(
            enqueue.get("task_id"),
            field_name="RuntimeEnv encryption canary task_id",
        )
        canary = self._poll_runtime_env_canary(
            task_id,
            headers=headers,
            surfaces=surfaces,
        )
        digest = cast(str, canary["runtime_env_hash"])
        self.evidence.runtime_env_encryption_canary = True
        self._inspect_runtime_env_canary_envelope(
            task_id=task_id,
            profile="thin",
            digest=digest,
        )

        fixtures = self._create_runtime_env_failure_fixtures()
        for execution_id, fixture_task_id in fixtures.values():
            self._poll_runtime_env_failure(
                execution_id=execution_id,
                task_id=fixture_task_id,
                headers=headers,
                surfaces=surfaces,
            )
        before_retry = self._runtime_env_failure_invariants(fixtures)
        self._validate_runtime_env_failure_invariants(before_retry)
        self.evidence.runtime_env_encryption_tamper_rejected = True
        self.evidence.runtime_env_encryption_unknown_key_rejected = True

        retry_id = fixtures["ciphertext"][0]
        retry_status, retry_body = self._http(
            f"/api/executions/{retry_id}/retry",
            method="POST",
            headers=headers,
        )
        surfaces.append(retry_body)
        if retry_status != 409:
            raise ValueError(f"RuntimeEnv corruption retry returned {retry_status}, expected 409")
        after_retry = self._runtime_env_failure_invariants(fixtures)
        self._validate_runtime_env_failure_invariants(after_retry)
        if after_retry != before_retry:
            raise ValueError("RuntimeEnv corruption retry changed row or attempt history")
        self.evidence.runtime_env_encryption_retry_preserved = True

        for surface in surfaces:
            self._assert_runtime_env_values_absent(
                surface,
                surface="authenticated RuntimeEnv API",
            )
        self._verify_runtime_env_logs_clear()

    def _workflow_envelope_contract(
        self,
        payload: Mapping[str, Any],
        *,
        task_id: str,
        endpoint: str,
        schema: str,
        expected_run_identity: Mapping[str, Any] | None = None,
        expected_publication: Mapping[str, Any] | None = None,
        expected_availability: str = "AVAILABLE",
        expected_complete: bool = True,
        expect_detail_revisions: bool = True,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Validate common bounded-reader identity without retaining response payloads."""

        if payload.get("schema") != schema or payload.get("schema_version") != 1:
            raise ValueError(f"{endpoint} returned an unsupported read envelope")
        if payload.get("task_id") != task_id:
            raise ValueError(f"{endpoint} returned the wrong workflow task")
        if (
            payload.get("availability") != expected_availability
            or payload.get("complete") is not expected_complete
        ):
            raise ValueError(f"{endpoint} returned the wrong workflow detail availability")

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
        }:
            raise ValueError(f"{endpoint} returned invalid publication revisions")
        summary_revision = publication.get("summary_revision")
        topology_version = publication.get("topology_version")
        detail_revision = publication.get("detail_revision")
        if type(summary_revision) is not int or cast(int, summary_revision) < 1:
            raise ValueError(f"{endpoint} returned invalid publication revisions")
        if expect_detail_revisions:
            if (
                type(topology_version) is not int
                or cast(int, topology_version) < 1
                or type(detail_revision) is not int
                or cast(int, detail_revision) < 1
            ):
                raise ValueError(f"{endpoint} returned invalid publication revisions")
        elif topology_version is not None or detail_revision is not None:
            raise ValueError(f"{endpoint} unexpectedly advertised retained workflow detail")
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
        limit: int = WORKFLOW_PROGRESS_PAGE_LIMIT,
        attempt_number: int | None = None,
    ) -> list[Mapping[str, Any]]:
        """Read one deliberately small complete page from a bounded workflow collection."""

        suffix = WORKFLOW_PROGRESS_COLLECTION_PATHS[collection]
        query_values = {"limit": limit}
        if attempt_number is not None:
            query_values["attempt_number"] = attempt_number
        query = urlencode(query_values)
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
            or len(items) > limit
            or page.get("next_cursor") is not None
        ):
            raise ValueError(f"{collection} workflow read was empty, inconsistent, or incomplete")
        return [
            _mapping(item, field_name=f"{collection} item {index}")
            for index, item in enumerate(items)
        ]

    def _workflow_empty_terminal_only_page(
        self,
        *,
        task_id: str,
        collection: str,
        headers: Mapping[str, str],
        run_identity: Mapping[str, Any],
        publication: Mapping[str, Any],
    ) -> None:
        """Prove summary-only readers expose no retained topology or node detail."""

        suffix = WORKFLOW_PROGRESS_COLLECTION_PATHS[collection]
        query = urlencode({"limit": WORKFLOW_PROGRESS_PAGE_LIMIT})
        endpoint = f"/api/cluster/workflows/{task_id}/{suffix}?{query}"
        status, body = self._http(endpoint, method="GET", headers=headers)
        if status != 200:
            raise ValueError(f"{collection} terminal-only read returned a non-success status")
        page = self._json_body(body, endpoint=f"terminal-only {collection}")
        self._workflow_envelope_contract(
            page,
            task_id=task_id,
            endpoint=f"terminal-only {collection}",
            schema="django-ray.workflow-progress-page",
            expected_run_identity=run_identity,
            expected_publication=publication,
            expected_availability="OMITTED_BY_POLICY",
            expected_complete=False,
            expect_detail_revisions=False,
        )
        if (
            page.get("collection") != collection
            or page.get("returned_count") != 0
            or page.get("items") != []
            or page.get("next_cursor") is not None
        ):
            raise ValueError(f"{collection} terminal-only read exposed retained workflow detail")

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

    @staticmethod
    def _workflow_longest_path_layers(
        node_ids: set[str],
        edges: set[tuple[str, str]],
    ) -> tuple[frozenset[str], ...]:
        """Derive deterministic longest-path layers and reject cyclic topology."""

        incoming: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
        outgoing: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
        for source, target in edges:
            incoming[target].add(source)
            outgoing[source].add(target)

        remaining_predecessors = {
            node_id: len(predecessors) for node_id, predecessors in incoming.items()
        }
        ready = sorted(
            node_id
            for node_id, predecessor_count in remaining_predecessors.items()
            if predecessor_count == 0
        )
        layer_by_node: dict[str, int] = {}
        while ready:
            node_id = ready.pop(0)
            predecessors = incoming[node_id]
            layer_by_node[node_id] = (
                max(layer_by_node[predecessor] for predecessor in predecessors) + 1
                if predecessors
                else 0
            )
            for target in sorted(outgoing[node_id]):
                remaining_predecessors[target] -= 1
                if remaining_predecessors[target] == 0:
                    ready.append(target)
                    ready.sort()

        if set(layer_by_node) != node_ids:
            raise ValueError("workflow topology was cyclic or could not be fully layered")
        if any(layer_by_node[source] >= layer_by_node[target] for source, target in edges):
            raise ValueError("workflow topology edge did not advance the longest-path layer")

        layer_count = max(layer_by_node.values(), default=-1) + 1
        return tuple(
            frozenset(
                node_id for node_id, node_layer in layer_by_node.items() if node_layer == layer
            )
            for layer in range(layer_count)
        )

    def _workflow_indexed_details(
        self,
        *,
        task_id: str,
        headers: Mapping[str, str],
        run_identity: Mapping[str, Any],
        publication: Mapping[str, Any],
        node_details: Sequence[Mapping[str, Any]],
    ) -> int:
        """Prove every retained node has a complete indexed detail-link target."""

        detail_by_node = {cast(str, detail["node_id"]): detail for detail in node_details}
        for node_id, expected_detail in sorted(detail_by_node.items()):
            query = urlencode(
                {
                    "node_id": node_id,
                    "attempt_number": run_identity["attempt_number"],
                }
            )
            endpoint = f"/api/cluster/workflows/{task_id}/node-detail?{query}"
            status, body = self._http(endpoint, method="GET", headers=headers)
            if status != 200:
                raise ValueError("workflow indexed node detail returned a non-success status")
            indexed = self._json_body(body, endpoint="workflow indexed node detail")
            self._workflow_envelope_contract(
                indexed,
                task_id=task_id,
                endpoint="workflow indexed node detail",
                schema="django-ray.workflow-progress-node",
                expected_run_identity=run_identity,
                expected_publication=publication,
            )
            item = _mapping(
                indexed.get("item"),
                field_name="workflow indexed node detail item",
            )
            if indexed.get("found") is not True or item != expected_detail:
                raise ValueError(
                    "workflow indexed node detail did not match its retained graph node"
                )
        return len(detail_by_node)

    def _verify_workflow_run(
        self,
        *,
        workflow_label: str,
        enqueue_path: str,
        expected_enqueue_kwargs: Mapping[str, object],
        expected_state: str,
        expected_error: str | None = None,
        poll_path: str = "/api/cluster/complex-workflow",
        callable_path: str = ("testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"),
        expected_leaf_tasks: int | None = None,
        expected_success_result: Mapping[str, Any] | None = None,
        expected_node_layers: tuple[frozenset[str], ...] | None = None,
        expected_edges: frozenset[tuple[str, str]] | None = None,
        failure_node_id: str | None = None,
        expected_pending_descendants: frozenset[str] = frozenset(),
        required_succeeded_nodes: frozenset[str] = frozenset(),
        require_indexed_details: bool = False,
        expected_output_previews: Mapping[str, tuple[str, Mapping[str, Any]]] | None = None,
        page_limit: int = WORKFLOW_PROGRESS_PAGE_LIMIT,
    ) -> WorkflowGateObservation:
        """Verify one terminal workflow through every required bounded API reader."""

        token = self._secret_token()
        headers = {"Authorization": f"Bearer {token}"}
        status, body = self._http(enqueue_path, method="POST", headers=headers)
        if status != 200:
            raise ValueError(f"{workflow_label} enqueue returned a non-success status")
        enqueue = self._json_body(body, endpoint=f"{workflow_label} enqueue")
        enqueue_kwargs = _mapping(
            enqueue.get("kwargs"),
            field_name=f"{workflow_label} enqueue kwargs",
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
            raise ValueError(f"{workflow_label} enqueue did not retain the exact requested inputs")
        if expected_leaf_tasks is None:
            fast_items = enqueue_kwargs.get("fast_items")
            slow_items = enqueue_kwargs.get("slow_items")
            if type(fast_items) is not int or type(slow_items) is not int:
                raise ValueError(f"{workflow_label} enqueue returned invalid branch item counts")
            expected_leaf_tasks = cast(int, fast_items) + cast(int, slow_items)
        if not 1 <= expected_leaf_tasks <= 200:
            raise ValueError("workflow enqueue returned invalid total leaf work")
        task_id = enqueue.get("task_id")
        try:
            parsed_task_id = UUID(cast(str, task_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"{workflow_label} enqueue task_id is not a canonical UUID") from error
        if (
            parsed_task_id.version != 4
            or str(parsed_task_id) != task_id
            or not isinstance(task_id, str)
        ):
            raise ValueError(f"{workflow_label} enqueue task_id is not a canonical UUIDv4")
        task_id = str(parsed_task_id)

        deadline = time.monotonic() + self.config.task_timeout
        last_state = "missing"
        terminal_states = WORKFLOW_PROGRESS_FAILURE_STATES | {"SUCCEEDED"}
        while True:
            status, body = self._http(
                f"{poll_path}/{task_id}",
                method="GET",
                headers=headers,
            )
            if status != 200:
                raise ValueError(f"{workflow_label} polling returned a non-success status")
            execution = self._json_body(body, endpoint=f"{workflow_label} polling")
            self._observe_bounded_poll_projection(
                execution,
                family="workflow",
                surface=f"{workflow_label} polling",
                task_id=task_id,
            )
            state = execution.get("state")
            if not isinstance(state, str) or state not in WORKFLOW_PROGRESS_TASK_STATES:
                raise ValueError(f"{workflow_label} polling returned an invalid task state")
            last_state = state
            if state in terminal_states:
                if state != expected_state:
                    raise ValueError(f"{workflow_label} reached an unexpected terminal state")
                if state == "SUCCEEDED":
                    result = _mapping(
                        execution.get("result"),
                        field_name=f"{workflow_label} result",
                    )
                    if expected_success_result is not None:
                        result_matches = result == expected_success_result
                    else:
                        result_matches = (
                            result.get("shape") == "chain(group(chain(map), chain(map)), step)"
                            and result.get("durability_boundary") == "single RayTaskExecution"
                            and result.get("total_leaf_tasks") == expected_leaf_tasks
                        )
                    if not result_matches or execution.get("error") is not None:
                        raise ValueError(
                            "workflow result did not match the requested deterministic workload"
                        )
                else:
                    presented_error = validate_terminal_diagnostic_text(
                        execution.get("error"),
                        field_name=f"{workflow_label} polling error",
                    )
                    if execution.get("result") is not None or presented_error != expected_error:
                        raise ValueError(
                            f"failed {workflow_label} did not retain its normalized fixture error"
                        )
                break
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"{workflow_label} did not reach {expected_state} within "
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
            raise ValueError(f"{workflow_label} execution read returned a non-success status")
        execution_list = self._json_body(body, endpoint=f"{workflow_label} execution")
        task_records = _sequence(
            execution_list.get("tasks"),
            field_name=f"{workflow_label} execution tasks",
        )
        if len(task_records) != 1:
            raise ValueError(f"{workflow_label} execution read did not return exactly one row")
        task_record = _mapping(
            task_records[0],
            field_name=f"{workflow_label} execution row",
        )
        if (
            task_record.get("task_id") != task_id
            or task_record.get("state") != expected_state
            or task_record.get("callable_path") != callable_path
            or task_record.get("attempt_number") != 1
        ):
            raise ValueError(f"{workflow_label} did not remain on its first durable attempt")
        if expected_state == "FAILED":
            persisted_error = validate_terminal_diagnostic_text(
                task_record.get("error_message"),
                field_name=f"{workflow_label} execution error",
            )
            if persisted_error != expected_error:
                raise ValueError(
                    f"failed {workflow_label} execution row changed its normalized error"
                )

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
            limit=page_limit,
        )
        topology_edges = self._workflow_page(
            task_id=task_id,
            collection="topology_edges",
            headers=headers,
            run_identity=run_identity,
            publication=publication,
            limit=page_limit,
        )
        node_details = self._workflow_page(
            task_id=task_id,
            collection="node_details",
            headers=headers,
            run_identity=run_identity,
            publication=publication,
            limit=page_limit,
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
        expected_node_ids: set[str] | None = None
        if expected_node_layers is not None:
            expected_node_ids = set().union(*expected_node_layers)
            if topology_node_ids != expected_node_ids:
                raise ValueError("workflow topology did not match the expected runtime nodes")
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
        longest_path_layers = 0
        if expected_edges is not None:
            if edges != expected_edges:
                raise ValueError("workflow topology did not match the expected dependency edges")
            layers = self._workflow_longest_path_layers(
                topology_node_ids,
                edges,
            )
            if layers != expected_node_layers:
                raise ValueError("workflow topology did not match its expected longest-path layers")
            longest_path_layers = len(layers)
        states = [detail_item.get("state") for detail_item in node_details]
        if any(state not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"} for state in states):
            raise ValueError("terminal workflow detail contains an invalid node state")
        pending_nodes = states.count("PENDING")
        running_nodes = states.count("RUNNING")
        succeeded_nodes = states.count("SUCCEEDED")
        failed_nodes = states.count("FAILED")
        pending_descendants = 0
        if expected_state == "SUCCEEDED":
            if (
                pending_nodes != 0
                or running_nodes != 0
                or succeeded_nodes != len(states)
                or failed_nodes != 0
            ):
                raise ValueError("successful workflow detail was not fully succeeded")
        elif failure_node_id is None:
            if failed_nodes < 1:
                raise ValueError("failed workflow detail did not retain a failed node")
        else:
            states_by_node = {
                cast(str, detail_item["node_id"]): detail_item.get("state")
                for detail_item in node_details
            }
            # Full-mode terminal detail is one immutable actor flush. Retrying
            # this reader cannot turn incomplete published node evidence
            # into success, so treat it as a producer-contract failure.
            failed_node_ids = {
                node_id for node_id, state in states_by_node.items() if state == "FAILED"
            }
            if (
                failed_node_ids != {failure_node_id}
                or any(
                    states_by_node[node_id] != "PENDING" for node_id in expected_pending_descendants
                )
                or any(
                    states_by_node[node_id] != "SUCCEEDED" for node_id in required_succeeded_nodes
                )
            ):
                raise ValueError(
                    "failed workflow did not isolate its failure, pending descendants, "
                    "and successful prerequisite work"
                )
            pending_descendants = len(expected_pending_descendants)

        detail_links = 0
        if expected_output_previews is not None:
            details_by_node = {
                cast(str, detail_item["node_id"]): detail_item for detail_item in node_details
            }
            for node_id, (expected_node_state, expected_preview) in sorted(
                expected_output_previews.items()
            ):
                detail_item = details_by_node.get(node_id)
                if (
                    detail_item is None
                    or detail_item.get("state") != expected_node_state
                    or detail_item.get("output_preview") != expected_preview
                ):
                    raise ValueError(
                        "workflow output preview did not match its exact safe value, "
                        "projector-failure, or application-failure contract"
                    )
        if require_indexed_details:
            detail_links = self._workflow_indexed_details(
                task_id=task_id,
                headers=headers,
                run_identity=run_identity,
                publication=publication,
                node_details=node_details,
            )
            if expected_node_ids is not None and detail_links != len(expected_node_ids):
                raise ValueError("workflow did not expose a detail target for every graph node")

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
            type(declared_nodes) is not int
            or cast(int, declared_nodes) < expected_node_count
            or (expected_node_layers is None and declared_nodes != expected_node_count)
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
            type(declared_edges) is not int
            or cast(int, declared_edges) < expected_edge_count
            or (expected_node_layers is None and declared_edges != expected_edge_count)
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
            longest_path_layers=longest_path_layers,
            detail_links=detail_links,
            pending_descendants=pending_descendants,
        )

    def _verify_terminal_only_complex_workflow_run(
        self,
        *,
        enqueue_path: str,
        expected_enqueue_kwargs: Mapping[str, object],
        expected_state: str,
        expected_error: str | None = None,
    ) -> TerminalOnlyWorkflowGateObservation:
        """Verify one terminal-only run exposes exactly one summary and no detail."""

        token = self._secret_token()
        headers = {"Authorization": f"Bearer {token}"}
        status, body = self._http(enqueue_path, method="POST", headers=headers)
        if status != 200:
            raise ValueError("terminal-only workflow enqueue returned a non-success status")
        enqueue = self._json_body(body, endpoint="terminal-only workflow enqueue")
        enqueue_kwargs = _mapping(
            enqueue.get("kwargs"),
            field_name="terminal-only workflow enqueue kwargs",
        )
        if (
            enqueue.get("args") != []
            or set(enqueue_kwargs) != set(expected_enqueue_kwargs)
            or any(
                type(enqueue_kwargs.get(field_name)) is not type(expected_value)
                or enqueue_kwargs.get(field_name) != expected_value
                for field_name, expected_value in expected_enqueue_kwargs.items()
            )
            or enqueue_kwargs.get("reporting_policy") != "terminal_only"
        ):
            raise ValueError(
                "terminal-only workflow enqueue did not retain the exact requested inputs"
            )
        fast_items = enqueue_kwargs.get("fast_items")
        slow_items = enqueue_kwargs.get("slow_items")
        if type(fast_items) is not int or type(slow_items) is not int:
            raise ValueError("terminal-only workflow enqueue returned invalid branch item counts")
        expected_leaf_tasks = cast(int, fast_items) + cast(int, slow_items)
        if not 2 <= expected_leaf_tasks <= 200:
            raise ValueError("terminal-only workflow enqueue returned invalid total leaf work")

        task_id = enqueue.get("task_id")
        try:
            parsed_task_id = UUID(cast(str, task_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(
                "terminal-only workflow enqueue task_id is not a canonical UUID"
            ) from error
        if (
            parsed_task_id.version != 4
            or str(parsed_task_id) != task_id
            or not isinstance(task_id, str)
        ):
            raise ValueError("terminal-only workflow enqueue task_id is not a canonical UUIDv4")
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
                raise ValueError("terminal-only workflow polling returned a non-success status")
            execution = self._json_body(body, endpoint="terminal-only workflow polling")
            self._observe_bounded_poll_projection(
                execution,
                family="workflow",
                surface="terminal-only workflow polling",
                task_id=task_id,
            )
            state = execution.get("state")
            if not isinstance(state, str) or state not in WORKFLOW_PROGRESS_TASK_STATES:
                raise ValueError("terminal-only workflow polling returned an invalid task state")
            last_state = state
            if state in terminal_states:
                if state != expected_state:
                    raise ValueError("terminal-only workflow reached an unexpected terminal state")
                if state == "SUCCEEDED":
                    result = _mapping(
                        execution.get("result"),
                        field_name="terminal-only workflow result",
                    )
                    if (
                        result.get("shape") != "chain(group(chain(map), chain(map)), step)"
                        or result.get("durability_boundary") != "single RayTaskExecution"
                        or result.get("total_leaf_tasks") != expected_leaf_tasks
                        or execution.get("error") is not None
                    ):
                        raise ValueError(
                            "terminal-only workflow result did not match the tiny nested workload"
                        )
                elif (
                    execution.get("result") is not None or execution.get("error") != expected_error
                ):
                    raise ValueError(
                        "failed terminal-only workflow did not retain its normalized fixture error"
                    )
                break
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"terminal-only workflow did not reach {expected_state} within "
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
            raise ValueError("terminal-only execution read returned a non-success status")
        execution_list = self._json_body(body, endpoint="terminal-only workflow execution")
        task_records = _sequence(
            execution_list.get("tasks"),
            field_name="terminal-only workflow execution tasks",
        )
        if len(task_records) != 1:
            raise ValueError("terminal-only execution read did not return exactly one row")
        task_record = _mapping(
            task_records[0],
            field_name="terminal-only workflow execution row",
        )
        if (
            task_record.get("task_id") != task_id
            or task_record.get("state") != expected_state
            or task_record.get("callable_path")
            != "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"
            or task_record.get("attempt_number") != 1
        ):
            raise ValueError("terminal-only workflow did not remain on its first durable attempt")

        summary_endpoint = f"/api/cluster/workflows/{task_id}"
        status, body = self._http(summary_endpoint, method="GET", headers=headers)
        if status != 200:
            raise ValueError("terminal-only summary read returned a non-success status")
        summary_envelope = self._json_body(body, endpoint="terminal-only workflow summary")
        run_identity, publication = self._workflow_envelope_contract(
            summary_envelope,
            task_id=task_id,
            endpoint="terminal-only workflow summary",
            schema="django-ray.workflow-progress-summary",
            expected_availability="OMITTED_BY_POLICY",
            expected_complete=False,
            expect_detail_revisions=False,
        )
        summary = _mapping(
            summary_envelope.get("summary"),
            field_name="terminal-only workflow summary payload",
        )
        summary_revision = publication["summary_revision"]
        if (
            summary_envelope.get("source_schema_version") != WORKFLOW_PROGRESS_SCHEMA_VERSION
            or summary.get("schema_version") != WORKFLOW_PROGRESS_SCHEMA_VERSION
            or summary.get("storage_protocol_version") != 1
            or summary.get("run_identity") != run_identity
            or run_identity.get("attempt_number") != 1
            or summary.get("state") != expected_state
            or summary.get("reporting_policy") != "terminal_only"
            or summary.get("selected_strategy") != "dynamic_tasks"
            or summary_revision != 1
            or summary.get("summary_revision") != summary_revision
            or summary.get("topology_version") is not None
            or summary.get("detail_revision") is not None
            or summary.get("limits_profile") != "v1"
        ):
            raise ValueError(
                "terminal-only summary did not report one terminal schema-v3 publication"
            )
        if task_record.get("execution_generation") != run_identity.get(
            "execution_generation"
        ) or str(task_record.get("workflow_run_id")) != run_identity.get("run_id"):
            raise ValueError("terminal-only summary identity did not match the execution row")
        fingerprint = summary.get("plan_fingerprint")
        if (
            not isinstance(fingerprint, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
        ):
            raise ValueError("terminal-only summary did not retain a canonical plan fingerprint")

        detail = _mapping(summary.get("detail"), field_name="terminal-only summary detail")
        storage = _mapping(summary.get("storage"), field_name="terminal-only summary storage")
        retention = _mapping(
            summary.get("retention"),
            field_name="terminal-only summary retention",
        )
        terminal = _mapping(summary.get("terminal"), field_name="terminal-only summary terminal")
        timestamps = _mapping(
            summary.get("timestamps"),
            field_name="terminal-only summary timestamps",
        )
        finished_at = timestamps.get("finished_at")
        if (
            detail
            != {
                "availability": "OMITTED_BY_POLICY",
                "complete": False,
                "truncation_reasons": [],
            }
            or storage != {"kind": "database", "manifest_id": None}
            or type(retention.get("detail_days")) is not int
            or not 0 <= cast(int, retention["detail_days"]) <= 30
            or retention.get("detail_expires_at") is not None
            or not isinstance(timestamps.get("started_at"), str)
            or not isinstance(timestamps.get("updated_at"), str)
            or not isinstance(finished_at, str)
            or not finished_at
            or terminal != {"outcome": expected_state, "finished_at": finished_at}
            or summary.get("progress_percent") != (100.0 if expected_state == "SUCCEEDED" else 0.0)
        ):
            raise ValueError("terminal-only summary retained invalid terminal metadata")

        node_counts = _mapping(
            summary.get("node_counts"),
            field_name="terminal-only summary node_counts",
        )
        edge_counts = _mapping(
            summary.get("edge_counts"),
            field_name="terminal-only summary edge_counts",
        )
        declared_nodes = node_counts.get("declared")
        declared_edges = edge_counts.get("declared")
        if (
            set(node_counts)
            != {
                "declared",
                "discovered",
                "retained_topology",
                "retained_detail",
                "pending",
                "running",
                "succeeded",
                "failed",
            }
            or type(declared_nodes) is not int
            or cast(int, declared_nodes) < 1
            or any(
                node_counts.get(field_name) != 0
                for field_name in (
                    "discovered",
                    "retained_topology",
                    "retained_detail",
                    "pending",
                    "running",
                    "succeeded",
                    "failed",
                )
            )
            or set(edge_counts) != {"declared", "discovered", "retained_topology"}
            or type(declared_edges) is not int
            or cast(int, declared_edges) < 1
            or edge_counts.get("discovered") != 0
            or edge_counts.get("retained_topology") != 0
        ):
            raise ValueError("terminal-only summary claimed discovered or retained workflow detail")

        for collection in WORKFLOW_PROGRESS_COLLECTION_PATHS:
            self._workflow_empty_terminal_only_page(
                task_id=task_id,
                collection=collection,
                headers=headers,
                run_identity=run_identity,
                publication=publication,
            )

        return TerminalOnlyWorkflowGateObservation(
            task_id=task_id,
            state=expected_state,
            attempt_number=1,
            schema_version=WORKFLOW_PROGRESS_SCHEMA_VERSION,
            summary_revision=cast(int, summary_revision),
            declared_nodes=cast(int, declared_nodes),
            declared_edges=cast(int, declared_edges),
        )

    def _verify_complex_workflow_progress(self) -> None:
        """Prove default-full and terminal-only nested workflows end to end."""

        succeeded = self._verify_workflow_run(
            workflow_label="complex workflow",
            enqueue_path=COMPLEX_WORKFLOW_ENQUEUE_PATH,
            expected_enqueue_kwargs=COMPLEX_WORKFLOW_ENQUEUE_KWARGS,
            expected_state="SUCCEEDED",
        )
        failed = self._verify_workflow_run(
            workflow_label="complex workflow",
            enqueue_path=COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH,
            expected_enqueue_kwargs=COMPLEX_WORKFLOW_FAILURE_ENQUEUE_KWARGS,
            expected_state="FAILED",
            expected_error=COMPLEX_WORKFLOW_FAILURE_MESSAGE,
        )
        terminal_only_succeeded = self._verify_terminal_only_complex_workflow_run(
            enqueue_path=COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_PATH,
            expected_enqueue_kwargs=COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_KWARGS,
            expected_state="SUCCEEDED",
        )
        terminal_only_failed = self._verify_terminal_only_complex_workflow_run(
            enqueue_path=COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_PATH,
            expected_enqueue_kwargs=(COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_KWARGS),
            expected_state="FAILED",
            expected_error=COMPLEX_WORKFLOW_FAILURE_MESSAGE,
        )
        if (
            terminal_only_succeeded.declared_nodes,
            terminal_only_succeeded.declared_edges,
        ) != (
            terminal_only_failed.declared_nodes,
            terminal_only_failed.declared_edges,
        ):
            raise ValueError(
                "equivalent terminal-only runs reported different declared plan counts"
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
        self.evidence.workflow_terminal_only_task_id = terminal_only_succeeded.task_id
        self.evidence.workflow_terminal_only_task_state = terminal_only_succeeded.state
        self.evidence.workflow_terminal_only_attempt_number = terminal_only_succeeded.attempt_number
        self.evidence.workflow_terminal_only_schema_version = terminal_only_succeeded.schema_version
        self.evidence.workflow_terminal_only_summary_revision = (
            terminal_only_succeeded.summary_revision
        )
        self.evidence.workflow_terminal_only_declared_nodes = terminal_only_succeeded.declared_nodes
        self.evidence.workflow_terminal_only_declared_edges = terminal_only_succeeded.declared_edges
        self.evidence.workflow_terminal_only_failure_task_id = terminal_only_failed.task_id
        self.evidence.workflow_terminal_only_failure_task_state = terminal_only_failed.state
        self.evidence.workflow_terminal_only_failure_attempt_number = (
            terminal_only_failed.attempt_number
        )
        self.evidence.workflow_terminal_only_failure_schema_version = (
            terminal_only_failed.schema_version
        )
        self.evidence.workflow_terminal_only_failure_summary_revision = (
            terminal_only_failed.summary_revision
        )
        self.evidence.workflow_terminal_only_failure_declared_nodes = (
            terminal_only_failed.declared_nodes
        )
        self.evidence.workflow_terminal_only_failure_declared_edges = (
            terminal_only_failed.declared_edges
        )

    def _verify_workflow_showcase_progress(self) -> None:
        """Prove the showcase success and deterministic reservation failure."""

        showcase_contract = {
            "workflow_label": "workflow showcase",
            "poll_path": "/api/cluster/workflow-showcase",
            "callable_path": WORKFLOW_SHOWCASE_CALLABLE,
            "expected_leaf_tasks": 1,
            "expected_success_result": WORKFLOW_SHOWCASE_SUCCESS_RESULT,
            "expected_node_layers": WORKFLOW_SHOWCASE_NODE_LAYERS,
            "expected_edges": WORKFLOW_SHOWCASE_EDGES,
            "require_indexed_details": True,
            "page_limit": WORKFLOW_SHOWCASE_PAGE_LIMIT,
        }
        succeeded = self._verify_workflow_run(
            enqueue_path=WORKFLOW_SHOWCASE_ENQUEUE_PATH,
            expected_enqueue_kwargs=WORKFLOW_SHOWCASE_ENQUEUE_KWARGS,
            expected_state="SUCCEEDED",
            expected_output_previews={
                WORKFLOW_SHOWCASE_VALIDATION_NODE_ID: (
                    "SUCCEEDED",
                    {
                        "schema_version": 1,
                        "availability": "AVAILABLE",
                        "value": {"item_id": 0, "valid": True},
                    },
                ),
                WORKFLOW_SHOWCASE_PROJECTOR_FAILURE_NODE_ID: (
                    "SUCCEEDED",
                    {
                        "schema_version": 1,
                        "availability": "FAILED",
                        "value": None,
                    },
                ),
                WORKFLOW_SHOWCASE_FAILURE_NODE_ID: (
                    "SUCCEEDED",
                    {
                        "schema_version": 1,
                        "availability": "AVAILABLE",
                        "value": {"item_id": 0, "reserved_units": 1},
                    },
                ),
            },
            **showcase_contract,
        )
        failed = self._verify_workflow_run(
            enqueue_path=WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_PATH,
            expected_enqueue_kwargs=WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_KWARGS,
            expected_state="FAILED",
            expected_error=WORKFLOW_SHOWCASE_FAILURE_MESSAGE,
            failure_node_id=WORKFLOW_SHOWCASE_FAILURE_NODE_ID,
            expected_pending_descendants=WORKFLOW_SHOWCASE_FAILURE_DESCENDANTS,
            required_succeeded_nodes=WORKFLOW_SHOWCASE_FAILURE_SUCCEEDED_NODES,
            expected_output_previews={
                WORKFLOW_SHOWCASE_VALIDATION_NODE_ID: (
                    "SUCCEEDED",
                    {
                        "schema_version": 1,
                        "availability": "AVAILABLE",
                        "value": {"item_id": 0, "valid": True},
                    },
                ),
                WORKFLOW_SHOWCASE_PROJECTOR_FAILURE_NODE_ID: (
                    "SUCCEEDED",
                    {
                        "schema_version": 1,
                        "availability": "FAILED",
                        "value": None,
                    },
                ),
                WORKFLOW_SHOWCASE_FAILURE_NODE_ID: (
                    "FAILED",
                    {
                        "schema_version": 1,
                        "availability": "UNAVAILABLE",
                        "value": None,
                    },
                ),
            },
            **showcase_contract,
        )
        if (
            succeeded.topology_nodes,
            succeeded.topology_edges,
            succeeded.longest_path_layers,
            succeeded.detail_links,
        ) != (
            failed.topology_nodes,
            failed.topology_edges,
            failed.longest_path_layers,
            failed.detail_links,
        ):
            raise ValueError("equivalent workflow showcase runs exposed different topology")

        self.evidence.workflow_showcase_task_id = succeeded.task_id
        self.evidence.workflow_showcase_task_state = succeeded.state
        self.evidence.workflow_showcase_attempt_number = succeeded.attempt_number
        self.evidence.workflow_showcase_topology_nodes = succeeded.topology_nodes
        self.evidence.workflow_showcase_topology_edges = succeeded.topology_edges
        self.evidence.workflow_showcase_longest_path_layers = succeeded.longest_path_layers
        self.evidence.workflow_showcase_detail_links = succeeded.detail_links
        self.evidence.workflow_showcase_failure_task_id = failed.task_id
        self.evidence.workflow_showcase_failure_task_state = failed.state
        self.evidence.workflow_showcase_failure_attempt_number = failed.attempt_number
        self.evidence.workflow_showcase_failure_failed_nodes = failed.failed_nodes
        self.evidence.workflow_showcase_failure_pending_descendants = failed.pending_descendants
        self.evidence.workflow_showcase_failure_running_nodes = failed.running_nodes
        self.evidence.workflow_showcase_failure_succeeded_nodes = failed.succeeded_nodes
        self.evidence.workflow_showcase_failure_path_nodes = (
            len(WORKFLOW_SHOWCASE_FAILURE_SUCCEEDED_NODES) + 1
        )
        self.evidence.workflow_showcase_failure_detail_links = failed.detail_links

    def _verify_workflow_recovery_attempt(
        self,
        *,
        task_id: str,
        attempt_number: int,
        expected_state: str,
        expected_node_states: Mapping[str, str],
        expected_edges: frozenset[tuple[str, str]],
        expected_error: str | None,
        headers: Mapping[str, str],
    ) -> WorkflowRecoveryAttemptObservation:
        """Verify one current or archived recovery attempt through bounded readers."""

        query = urlencode({"attempt_number": attempt_number})
        summary_endpoint = f"/api/cluster/workflows/{task_id}?{query}"
        status, body = self._http(summary_endpoint, method="GET", headers=headers)
        if status != 200:
            raise ValueError("workflow recovery summary returned a non-success status")
        summary_envelope = self._json_body(
            body,
            endpoint=f"workflow recovery attempt {attempt_number} summary",
        )
        run_identity, publication = self._workflow_envelope_contract(
            summary_envelope,
            task_id=task_id,
            endpoint=f"workflow recovery attempt {attempt_number} summary",
            schema="django-ray.workflow-progress-summary",
        )
        summary = _mapping(
            summary_envelope.get("summary"),
            field_name=f"workflow recovery attempt {attempt_number} summary payload",
        )
        execution_generation = run_identity.get("execution_generation")
        run_id = run_identity.get("run_id")
        fingerprint = summary.get("plan_fingerprint")
        if (
            summary_envelope.get("source_schema_version") != WORKFLOW_PROGRESS_SCHEMA_VERSION
            or summary.get("schema_version") != WORKFLOW_PROGRESS_SCHEMA_VERSION
            or summary.get("run_identity") != run_identity
            or run_identity.get("attempt_number") != attempt_number
            or execution_generation != attempt_number
            or not isinstance(run_id, str)
            or summary.get("state") != expected_state
            or summary.get("reporting_policy") != "full"
            or summary.get("selected_strategy") != "dynamic_tasks"
            or summary.get("summary_revision") != publication["summary_revision"]
            or summary.get("topology_version") != publication["topology_version"]
            or summary.get("detail_revision") != publication["detail_revision"]
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
        ):
            raise ValueError("workflow recovery attempt did not retain its fenced schema-v3 run")
        detail = _mapping(
            summary.get("detail"),
            field_name=f"workflow recovery attempt {attempt_number} detail",
        )
        if detail.get("availability") != "AVAILABLE" or detail.get("complete") is not True:
            raise ValueError("workflow recovery attempt detail was not complete and AVAILABLE")

        topology_nodes = self._workflow_page(
            task_id=task_id,
            collection="topology_nodes",
            headers=headers,
            run_identity=run_identity,
            publication=publication,
            limit=WORKFLOW_SHOWCASE_PAGE_LIMIT,
            attempt_number=attempt_number,
        )
        topology_edges = self._workflow_page(
            task_id=task_id,
            collection="topology_edges",
            headers=headers,
            run_identity=run_identity,
            publication=publication,
            limit=WORKFLOW_SHOWCASE_PAGE_LIMIT,
            attempt_number=attempt_number,
        )
        node_details = self._workflow_page(
            task_id=task_id,
            collection="node_details",
            headers=headers,
            run_identity=run_identity,
            publication=publication,
            limit=WORKFLOW_SHOWCASE_PAGE_LIMIT,
            attempt_number=attempt_number,
        )
        expected_node_ids = set(expected_node_states)
        topology_node_ids = self._workflow_node_ids(
            topology_nodes,
            collection="workflow recovery topology_nodes",
        )
        detail_node_ids = self._workflow_node_ids(
            node_details,
            collection="workflow recovery node_details",
        )
        if topology_node_ids != expected_node_ids or detail_node_ids != expected_node_ids:
            raise ValueError("workflow recovery attempt exposed unexpected graph membership")
        edges: set[tuple[str, str]] = set()
        for edge in topology_edges:
            source = edge.get("source")
            target = edge.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError("workflow recovery attempt exposed an invalid edge")
            edges.add((source, target))
        if edges != expected_edges or len(edges) != len(topology_edges):
            raise ValueError("workflow recovery attempt exposed unexpected dependency edges")

        details_by_node = {
            cast(str, detail_item["node_id"]): detail_item for detail_item in node_details
        }
        states_by_node = {
            node_id: detail_item.get("state") for node_id, detail_item in details_by_node.items()
        }
        if states_by_node != dict(expected_node_states):
            raise ValueError("workflow recovery attempt exposed unexpected terminal node states")
        failed_node_ids = {
            node_id for node_id, state in expected_node_states.items() if state == "FAILED"
        }
        if expected_error is None:
            if failed_node_ids or any(item.get("error") is not None for item in node_details):
                raise ValueError("successful workflow recovery attempt retained a node failure")
        elif (
            len(failed_node_ids) != 1
            or details_by_node[next(iter(failed_node_ids))].get("error") != expected_error
            or any(
                item.get("error") is not None and item["node_id"] not in failed_node_ids
                for item in node_details
            )
        ):
            raise ValueError("failed workflow recovery attempt retained the wrong fixture error")

        pending_nodes = tuple(expected_node_states.values()).count("PENDING")
        running_nodes = tuple(expected_node_states.values()).count("RUNNING")
        succeeded_nodes = tuple(expected_node_states.values()).count("SUCCEEDED")
        failed_nodes = tuple(expected_node_states.values()).count("FAILED")
        node_counts = _mapping(
            summary.get("node_counts"),
            field_name=f"workflow recovery attempt {attempt_number} node_counts",
        )
        edge_counts = _mapping(
            summary.get("edge_counts"),
            field_name=f"workflow recovery attempt {attempt_number} edge_counts",
        )
        expected_counts = {
            "discovered": len(expected_node_ids),
            "retained_topology": len(expected_node_ids),
            "retained_detail": len(expected_node_ids),
            "pending": pending_nodes,
            "running": running_nodes,
            "succeeded": succeeded_nodes,
            "failed": failed_nodes,
        }
        if any(
            self._workflow_summary_count(node_counts, field_name) != expected
            for field_name, expected in expected_counts.items()
        ):
            raise ValueError("workflow recovery summary counts did not match node detail")
        declared_nodes = node_counts.get("declared")
        if declared_nodes is not None and (
            type(declared_nodes) is not int or declared_nodes < len(expected_node_ids)
        ):
            raise ValueError("workflow recovery declared nodes did not cover its graph")
        if any(
            self._workflow_summary_count(edge_counts, field_name) != len(expected_edges)
            for field_name in ("discovered", "retained_topology")
        ):
            raise ValueError("workflow recovery summary counts did not match topology edges")
        declared_edges = edge_counts.get("declared")
        if declared_edges is not None and (
            type(declared_edges) is not int or declared_edges < len(expected_edges)
        ):
            raise ValueError("workflow recovery declared edges did not cover its graph")
        detail_links = self._workflow_indexed_details(
            task_id=task_id,
            headers=headers,
            run_identity=run_identity,
            publication=publication,
            node_details=node_details,
        )
        if detail_links != len(expected_node_ids):
            raise ValueError("workflow recovery attempt lacked an indexed detail target")
        return WorkflowRecoveryAttemptObservation(
            attempt_number=attempt_number,
            state=expected_state,
            execution_generation=cast(int, execution_generation),
            run_id=run_id,
            plan_fingerprint=fingerprint,
            topology_nodes=len(expected_node_ids),
            topology_edges=len(expected_edges),
            pending_nodes=pending_nodes,
            running_nodes=running_nodes,
            succeeded_nodes=succeeded_nodes,
            failed_nodes=failed_nodes,
            detail_links=detail_links,
        )

    def _verify_workflow_recovery_progress(self) -> None:
        """Prove two fenced failed attempts followed by one current success."""

        token = self._secret_token()
        headers = {"Authorization": f"Bearer {token}"}
        status, body = self._http(
            WORKFLOW_RECOVERY_ENQUEUE_PATH,
            method="POST",
            headers=headers,
        )
        if status != 200:
            raise ValueError("workflow recovery enqueue returned a non-success status")
        enqueue = self._json_body(body, endpoint="workflow recovery enqueue")
        if enqueue.get("args") != [] or enqueue.get("kwargs") != WORKFLOW_RECOVERY_ENQUEUE_KWARGS:
            raise ValueError("workflow recovery enqueue did not retain the exact bounded inputs")
        task_id = enqueue.get("task_id")
        try:
            parsed_task_id = UUID(cast(str, task_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("workflow recovery task_id is not a canonical UUID") from error
        if parsed_task_id.version != 4 or str(parsed_task_id) != task_id:
            raise ValueError("workflow recovery task_id is not a canonical UUIDv4")
        task_id = str(parsed_task_id)

        deadline = time.monotonic() + self.config.task_timeout
        while True:
            status, body = self._http(
                f"{WORKFLOW_RECOVERY_POLL_PATH}/{task_id}",
                method="GET",
                headers=headers,
            )
            if status != 200:
                raise ValueError("workflow recovery polling returned a non-success status")
            execution = self._json_body(body, endpoint="workflow recovery polling")
            self._observe_bounded_poll_projection(
                execution,
                family="workflow",
                surface="workflow recovery polling",
                task_id=task_id,
            )
            state = execution.get("state")
            if state == "SUCCEEDED":
                break
            if state in WORKFLOW_PROGRESS_FAILURE_STATES:
                raise ValueError("workflow recovery reached a premature terminal state")
            if not isinstance(state, str) or state not in WORKFLOW_PROGRESS_TASK_STATES:
                raise ValueError("workflow recovery polling returned an invalid state")
            if time.monotonic() >= deadline:
                raise ValueError(
                    "workflow recovery did not reach attempt-three success within "
                    f"{self.config.task_timeout}s (last state: {state})"
                )
            time.sleep(2)

        expected_attempts = [
            {
                "attempt_number": 1,
                "state": "FAILED",
                "error": WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE,
                "error_omission_reason": None,
            },
            {
                "attempt_number": 2,
                "state": "FAILED",
                "error": WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE,
                "error_omission_reason": None,
            },
            {
                "attempt_number": 3,
                "state": "SUCCEEDED",
                "error": None,
                "error_omission_reason": None,
            },
        ]
        if (
            execution.get("attempt_number") != 3
            or execution.get("runtime_env_profile") != "recovery-showcase"
            or execution.get("attempts") != expected_attempts
            or execution.get("result") != WORKFLOW_RECOVERY_SUCCESS_RESULT
            or execution.get("error") is not None
        ):
            raise ValueError("workflow recovery did not retain exactly two failures and success")

        execution_query = urlencode({"task_id": task_id, "limit": 1})
        status, body = self._http(
            f"/api/executions?{execution_query}",
            method="GET",
            headers=headers,
        )
        if status != 200:
            raise ValueError("workflow recovery execution read returned a non-success status")
        execution_list = self._json_body(body, endpoint="workflow recovery execution")
        task_records = _sequence(
            execution_list.get("tasks"),
            field_name="workflow recovery execution tasks",
        )
        if len(task_records) != 1:
            raise ValueError("workflow recovery execution read did not return exactly one row")
        task_record = _mapping(task_records[0], field_name="workflow recovery execution row")
        if (
            task_record.get("task_id") != task_id
            or task_record.get("state") != "SUCCEEDED"
            or task_record.get("callable_path") != WORKFLOW_RECOVERY_CALLABLE
            or task_record.get("attempt_number") != 3
            or task_record.get("execution_generation") != 3
        ):
            raise ValueError("workflow recovery current execution was not attempt-three success")

        early_states = dict.fromkeys(WORKFLOW_RECOVERY_EARLY_NODE_IDS, "PENDING")
        early_states[WORKFLOW_RECOVERY_EARLY_FAILURE_NODE_ID] = "FAILED"
        mid_states = dict.fromkeys(WORKFLOW_RECOVERY_MID_NODE_IDS, "PENDING")
        mid_states.update(dict.fromkeys(WORKFLOW_RECOVERY_MID_SUCCEEDED_NODES, "SUCCEEDED"))
        mid_states[WORKFLOW_RECOVERY_MID_FAILURE_NODE_ID] = "FAILED"
        success_node_ids = frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS)
        success_states = dict.fromkeys(success_node_ids, "SUCCEEDED")
        early = self._verify_workflow_recovery_attempt(
            task_id=task_id,
            attempt_number=1,
            expected_state="FAILED",
            expected_node_states=early_states,
            expected_edges=WORKFLOW_RECOVERY_EARLY_EDGES,
            expected_error=WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE,
            headers=headers,
        )
        middle = self._verify_workflow_recovery_attempt(
            task_id=task_id,
            attempt_number=2,
            expected_state="FAILED",
            expected_node_states=mid_states,
            expected_edges=WORKFLOW_RECOVERY_MID_EDGES,
            expected_error=WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE,
            headers=headers,
        )
        succeeded = self._verify_workflow_recovery_attempt(
            task_id=task_id,
            attempt_number=3,
            expected_state="SUCCEEDED",
            expected_node_states=success_states,
            expected_edges=WORKFLOW_SHOWCASE_EDGES,
            expected_error=None,
            headers=headers,
        )
        observations = (early, middle, succeeded)
        if (
            {observation.execution_generation for observation in observations} != {1, 2, 3}
            or len({observation.run_id for observation in observations}) != 3
            or len({observation.plan_fingerprint for observation in observations}) != 1
            or task_record.get("workflow_run_id") != succeeded.run_id
        ):
            raise ValueError("workflow recovery attempts were not distinctly fenced to one plan")

        self.evidence.workflow_recovery_task_id = task_id
        self.evidence.workflow_recovery_task_state = succeeded.state
        self.evidence.workflow_recovery_attempt_number = succeeded.attempt_number
        self.evidence.workflow_recovery_attempt_count = len(observations)
        self.evidence.workflow_recovery_distinct_runs = True
        self.evidence.workflow_recovery_early_topology_nodes = early.topology_nodes
        self.evidence.workflow_recovery_early_topology_edges = early.topology_edges
        self.evidence.workflow_recovery_early_pending_nodes = early.pending_nodes
        self.evidence.workflow_recovery_early_succeeded_nodes = early.succeeded_nodes
        self.evidence.workflow_recovery_early_failed_nodes = early.failed_nodes
        self.evidence.workflow_recovery_early_detail_links = early.detail_links
        self.evidence.workflow_recovery_mid_topology_nodes = middle.topology_nodes
        self.evidence.workflow_recovery_mid_topology_edges = middle.topology_edges
        self.evidence.workflow_recovery_mid_pending_nodes = middle.pending_nodes
        self.evidence.workflow_recovery_mid_succeeded_nodes = middle.succeeded_nodes
        self.evidence.workflow_recovery_mid_failed_nodes = middle.failed_nodes
        self.evidence.workflow_recovery_mid_detail_links = middle.detail_links
        self.evidence.workflow_recovery_success_topology_nodes = succeeded.topology_nodes
        self.evidence.workflow_recovery_success_topology_edges = succeeded.topology_edges
        self.evidence.workflow_recovery_success_succeeded_nodes = succeeded.succeeded_nodes
        self.evidence.workflow_recovery_success_detail_links = succeeded.detail_links

    def _verify_workflow_progress(self) -> None:
        """Verify compatibility, visual showcase, and durable recovery workflows."""

        self._verify_complex_workflow_progress()
        self._verify_workflow_showcase_progress()
        self._verify_workflow_recovery_progress()

    def _verify_workflow_admin(self) -> None:
        """Exercise current and archived workflow runs through authenticated Admin readers."""

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
            "graph_available_previews",
            "graph_failed_previews",
            "graph_unavailable_previews",
            "graph_preview_contract",
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
                None,
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
                None,
            ),
            (
                "showcase-successful",
                self.evidence.workflow_showcase_task_id,
                self.evidence.workflow_showcase_task_state,
                self.evidence.workflow_showcase_attempt_number,
                {
                    "topology_nodes": self.evidence.workflow_showcase_topology_nodes,
                    "topology_edges": self.evidence.workflow_showcase_topology_edges,
                    "node_details": self.evidence.workflow_showcase_detail_links,
                    "graph_nodes": self.evidence.workflow_showcase_topology_nodes,
                    "graph_edges": self.evidence.workflow_showcase_topology_edges,
                    "graph_pending_nodes": 0,
                    "graph_running_nodes": 0,
                    "graph_succeeded_nodes": (self.evidence.workflow_showcase_detail_links),
                    "graph_failed_nodes": 0,
                    "graph_failure_path_nodes": 0,
                    "graph_failure_origins": 0,
                    "graph_incoming_failure_edges": 0,
                    "graph_available_previews": 20,
                    "graph_failed_previews": 1,
                    "graph_unavailable_previews": 0,
                },
                None,
            ),
            (
                "showcase-failed",
                self.evidence.workflow_showcase_failure_task_id,
                self.evidence.workflow_showcase_failure_task_state,
                self.evidence.workflow_showcase_failure_attempt_number,
                {
                    "topology_nodes": self.evidence.workflow_showcase_topology_nodes,
                    "topology_edges": self.evidence.workflow_showcase_topology_edges,
                    "node_details": (self.evidence.workflow_showcase_failure_detail_links),
                    "graph_nodes": self.evidence.workflow_showcase_topology_nodes,
                    "graph_edges": self.evidence.workflow_showcase_topology_edges,
                    "graph_pending_nodes": (
                        self.evidence.workflow_showcase_failure_pending_descendants
                    ),
                    "graph_running_nodes": (self.evidence.workflow_showcase_failure_running_nodes),
                    "graph_succeeded_nodes": (
                        self.evidence.workflow_showcase_failure_succeeded_nodes
                    ),
                    "graph_failed_nodes": (self.evidence.workflow_showcase_failure_failed_nodes),
                    "graph_failure_path_nodes": (
                        self.evidence.workflow_showcase_failure_path_nodes
                    ),
                    "graph_failure_origins": 1,
                    "graph_incoming_failure_edges": 1,
                    "graph_available_previews": 14,
                    "graph_failed_previews": 1,
                    "graph_unavailable_previews": (
                        self.evidence.workflow_showcase_failure_pending_descendants
                        + self.evidence.workflow_showcase_failure_failed_nodes
                    ),
                },
                None,
            ),
            (
                "recovery-early-failed",
                self.evidence.workflow_recovery_task_id,
                "FAILED",
                1,
                {
                    "topology_nodes": self.evidence.workflow_recovery_early_topology_nodes,
                    "topology_edges": self.evidence.workflow_recovery_early_topology_edges,
                    "node_details": self.evidence.workflow_recovery_early_detail_links,
                    "graph_nodes": self.evidence.workflow_recovery_early_topology_nodes,
                    "graph_edges": self.evidence.workflow_recovery_early_topology_edges,
                    "graph_pending_nodes": self.evidence.workflow_recovery_early_pending_nodes,
                    "graph_running_nodes": 0,
                    "graph_succeeded_nodes": (
                        self.evidence.workflow_recovery_early_succeeded_nodes
                    ),
                    "graph_failed_nodes": self.evidence.workflow_recovery_early_failed_nodes,
                    "graph_failure_path_nodes": 1,
                    "graph_failure_origins": 1,
                    "graph_incoming_failure_edges": 0,
                    "graph_available_previews": 0,
                    "graph_failed_previews": 0,
                    "graph_unavailable_previews": (
                        self.evidence.workflow_recovery_early_pending_nodes
                        + self.evidence.workflow_recovery_early_failed_nodes
                    ),
                },
                1,
            ),
            (
                "recovery-mid-failed",
                self.evidence.workflow_recovery_task_id,
                "FAILED",
                2,
                {
                    "topology_nodes": self.evidence.workflow_recovery_mid_topology_nodes,
                    "topology_edges": self.evidence.workflow_recovery_mid_topology_edges,
                    "node_details": self.evidence.workflow_recovery_mid_detail_links,
                    "graph_nodes": self.evidence.workflow_recovery_mid_topology_nodes,
                    "graph_edges": self.evidence.workflow_recovery_mid_topology_edges,
                    "graph_pending_nodes": self.evidence.workflow_recovery_mid_pending_nodes,
                    "graph_running_nodes": 0,
                    "graph_succeeded_nodes": self.evidence.workflow_recovery_mid_succeeded_nodes,
                    "graph_failed_nodes": self.evidence.workflow_recovery_mid_failed_nodes,
                    "graph_failure_path_nodes": len(WORKFLOW_RECOVERY_MID_SUCCEEDED_NODES) + 1,
                    "graph_failure_origins": 1,
                    "graph_incoming_failure_edges": sum(
                        target == WORKFLOW_RECOVERY_MID_FAILURE_NODE_ID
                        for _source, target in WORKFLOW_RECOVERY_MID_EDGES
                    ),
                    "graph_available_previews": (
                        self.evidence.workflow_recovery_mid_succeeded_nodes - 1
                    ),
                    "graph_failed_previews": 1,
                    "graph_unavailable_previews": (
                        self.evidence.workflow_recovery_mid_pending_nodes
                        + self.evidence.workflow_recovery_mid_failed_nodes
                    ),
                },
                2,
            ),
            (
                "recovery-successful",
                self.evidence.workflow_recovery_task_id,
                self.evidence.workflow_recovery_task_state,
                self.evidence.workflow_recovery_attempt_number,
                {
                    "topology_nodes": self.evidence.workflow_recovery_success_topology_nodes,
                    "topology_edges": self.evidence.workflow_recovery_success_topology_edges,
                    "node_details": self.evidence.workflow_recovery_success_detail_links,
                    "graph_nodes": self.evidence.workflow_recovery_success_topology_nodes,
                    "graph_edges": self.evidence.workflow_recovery_success_topology_edges,
                    "graph_pending_nodes": 0,
                    "graph_running_nodes": 0,
                    "graph_succeeded_nodes": (
                        self.evidence.workflow_recovery_success_succeeded_nodes
                    ),
                    "graph_failed_nodes": 0,
                    "graph_failure_path_nodes": 0,
                    "graph_failure_origins": 0,
                    "graph_incoming_failure_edges": 0,
                    "graph_available_previews": (
                        self.evidence.workflow_recovery_success_succeeded_nodes - 2
                    ),
                    "graph_failed_previews": 1,
                    "graph_unavailable_previews": 0,
                },
                3,
            ),
        )
        verified: dict[str, Mapping[str, Any]] = {}
        for (
            label,
            task_id,
            task_state,
            attempt_number,
            expected_counts,
            selected_attempt,
        ) in runs:
            if (
                not task_id
                or task_state not in {"SUCCEEDED", "FAILED"}
                or type(attempt_number) is not int
                or attempt_number < 1
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

            command = (
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
            )
            if selected_attempt is not None:
                command = (
                    *command,
                    "--existing-workflow-attempt-number",
                    str(selected_attempt),
                )
            result = self._kubectl(
                *command,
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
                - {
                    "admin_workflow",
                    "task_id",
                    "task_state",
                    "graph_status",
                    "graph_preview_contract",
                }
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
                or payload.get("graph_preview_contract")
                != (
                    f"showcase-{task_state.lower()}-verified"
                    if label.startswith("showcase-")
                    else "not-applicable"
                )
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
            if label.endswith("failed"):
                minimum_incoming_edges = 0 if label == "recovery-early-failed" else 1
                if (
                    payload.get("graph_failure_path_nodes", 0) < 1
                    or payload.get("graph_failure_origins") != 1
                    or payload.get("graph_incoming_failure_edges", 0) < minimum_incoming_edges
                ):
                    raise ValueError("failed workflow admin graph lacked its expected failed path")
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
        self.evidence.workflow_recovery_admin_attempts = sum(
            label.startswith("recovery-") for label in verified
        )

        terminal_expected_fields = {
            "admin_workflow",
            "task_id",
            "task_state",
            "attempt_number",
            "admin_actions",
            "graph_advertised",
            "graph_status",
            "summary_revision",
            "reporting_policy",
            "detail_availability",
            "declared_nodes",
            "declared_edges",
            "legacy_progress_null",
            "attempt_summary_matches",
            "storage_rows",
            "topology_manifests",
            "topology_pages",
            "manifest_links",
            "node_details",
        }
        terminal_runs = (
            (
                "successful",
                self.evidence.workflow_terminal_only_task_id,
                self.evidence.workflow_terminal_only_task_state,
                self.evidence.workflow_terminal_only_attempt_number,
                self.evidence.workflow_terminal_only_summary_revision,
                self.evidence.workflow_terminal_only_declared_nodes,
                self.evidence.workflow_terminal_only_declared_edges,
            ),
            (
                "failed",
                self.evidence.workflow_terminal_only_failure_task_id,
                self.evidence.workflow_terminal_only_failure_task_state,
                self.evidence.workflow_terminal_only_failure_attempt_number,
                self.evidence.workflow_terminal_only_failure_summary_revision,
                self.evidence.workflow_terminal_only_failure_declared_nodes,
                self.evidence.workflow_terminal_only_failure_declared_edges,
            ),
        )
        terminal_verified: dict[str, Mapping[str, Any]] = {}
        for (
            label,
            task_id,
            task_state,
            attempt_number,
            summary_revision,
            declared_nodes,
            declared_edges,
        ) in terminal_runs:
            if (
                not task_id
                or task_state not in {"SUCCEEDED", "FAILED"}
                or attempt_number != 1
                or summary_revision != 1
                or declared_nodes < 1
                or declared_edges < 1
            ):
                raise ValueError(
                    "terminal-only workflow API evidence was not ready for admin verification"
                )
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
                "--expected-workflow-reporting-policy",
                "terminal_only",
                timeout=(self.config.task_timeout + self.config.kubectl_request_timeout + 5),
                sensitive_output=True,
            )
            payload = self._json_command(
                result,
                field_name=f"{label} terminal-only workflow admin smoke response",
            )
            if set(payload) != terminal_expected_fields:
                raise ValueError(
                    "terminal-only workflow admin smoke returned unexpected evidence fields"
                )
            if (
                payload.get("admin_workflow") != "terminal-summary-verified"
                or payload.get("task_id") != task_id
                or payload.get("task_state") != task_state
                or payload.get("attempt_number") != attempt_number
                or payload.get("summary_revision") != summary_revision
                or payload.get("reporting_policy") != "terminal_only"
                or payload.get("detail_availability") != "OMITTED_BY_POLICY"
                or payload.get("declared_nodes") != declared_nodes
                or payload.get("declared_edges") != declared_edges
                or payload.get("legacy_progress_null") is not True
                or payload.get("attempt_summary_matches") is not True
                or payload.get("admin_actions") != 0
                or payload.get("graph_advertised") is not False
                or payload.get("graph_status") != "UNAVAILABLE"
                or any(
                    payload.get(field_name) != 0
                    for field_name in (
                        "storage_rows",
                        "topology_manifests",
                        "topology_pages",
                        "manifest_links",
                        "node_details",
                    )
                )
            ):
                raise ValueError(
                    "terminal-only admin or storage evidence did not match its API summary"
                )
            terminal_verified[label] = payload

        terminal_success = terminal_verified["successful"]
        terminal_failure = terminal_verified["failed"]
        self.evidence.workflow_terminal_only_admin_actions = cast(
            int,
            terminal_success["admin_actions"],
        )
        self.evidence.workflow_terminal_only_graph_advertised = cast(
            bool,
            terminal_success["graph_advertised"],
        )
        self.evidence.workflow_terminal_only_storage_rows = cast(
            int,
            terminal_success["storage_rows"],
        )
        self.evidence.workflow_terminal_only_failure_admin_actions = cast(
            int,
            terminal_failure["admin_actions"],
        )
        self.evidence.workflow_terminal_only_failure_graph_advertised = cast(
            bool,
            terminal_failure["graph_advertised"],
        )
        self.evidence.workflow_terminal_only_failure_storage_rows = cast(
            int,
            terminal_failure["storage_rows"],
        )

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
        self._verify_released_v040_source_identity()
        self._verify_kubeconfig_snapshot()
        self._verify_ray_identity()
        self._verify_deployed_images()
        self._verify_preserved_secret()
        released_image_id = parse_docker_image_inspect(
            self._docker(
                "image",
                "inspect",
                self.evidence.released_v040_image_tag,
            ).stdout,
            expected_tag=self.evidence.released_v040_image_tag,
            commit=RELEASED_V040_COMMIT,
            source_tree=RELEASED_V040_SOURCE_TREE,
        )
        if released_image_id != self.evidence.released_v040_image_id:
            raise ValueError("released v0.4.0 image identity changed after certification")
        protocol_evidence = (
            self.evidence.protocol_legacy_cohort_visible,
            self.evidence.protocol_explicit_cohort_visible,
            self.evidence.protocol_v1_handoff_same_job,
            self.evidence.protocol_v1_handoff_no_resubmit,
            self.evidence.protocol_v1_queued_survived_handoff,
            self.evidence.protocol_v2_queued_unchanged,
            self.evidence.protocol_v2_unsupported_visible,
            self.evidence.protocol_v2_preinvocation_rejected,
            self.evidence.protocol_v2_application_marker_absent,
            self.evidence.protocol_v2_target_exact_completed,
            self.evidence.protocol_v2_target_mismatch_rejected,
            self.evidence.protocol_v2_target_mismatch_marker_absent,
            self.evidence.protocol_handoff_cleanup_restored,
        )
        if not all(value is True for value in protocol_evidence):
            raise ValueError("protocol handoff certification evidence is incomplete")

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
            ("released_v040_image_tag", self.evidence.released_v040_image_tag),
            ("released_v040_image_id", self.evidence.released_v040_image_id),
            ("legacy_worker_built", "true"),
            ("kuberay_uses_generic_ray", "true"),
            ("setup", "passed"),
            ("runtime_env_bytes", self.evidence.setup_bundle_bytes),
            ("runtime_env_sha256", self.evidence.setup_bundle_sha256),
            ("recovery_runtime_env_bytes", self.evidence.recovery_bundle_bytes),
            ("recovery_runtime_env_sha256", self.evidence.recovery_bundle_sha256),
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
            ("api_task_status_bounded", self.evidence.api_task_status_bounded),
            (
                "api_workflow_runtime_polls_bounded",
                self.evidence.api_workflow_runtime_polls_bounded,
            ),
            ("api_bulk_reset_absent", self.evidence.api_bulk_reset_absent),
            (
                "api_legacy_workflow_node_absent",
                self.evidence.api_legacy_workflow_node_absent,
            ),
            (
                "api_execution_delete_rejected",
                self.evidence.api_execution_delete_rejected,
            ),
            (
                "api_legacy_workflow_graph_absent",
                self.evidence.api_legacy_workflow_graph_absent,
            ),
            (
                "runtime_env_encryption_overlay",
                self.evidence.runtime_env_encryption_overlay,
            ),
            (
                "runtime_env_encryption_canary",
                self.evidence.runtime_env_encryption_canary,
            ),
            (
                "runtime_env_encryption_envelope",
                self.evidence.runtime_env_encryption_envelope,
            ),
            (
                "runtime_env_encryption_marker_absent",
                self.evidence.runtime_env_encryption_marker_absent,
            ),
            (
                "runtime_env_encryption_tamper_rejected",
                self.evidence.runtime_env_encryption_tamper_rejected,
            ),
            (
                "runtime_env_encryption_unknown_key_rejected",
                self.evidence.runtime_env_encryption_unknown_key_rejected,
            ),
            (
                "runtime_env_encryption_retry_preserved",
                self.evidence.runtime_env_encryption_retry_preserved,
            ),
            (
                "runtime_env_encryption_logs_clear",
                self.evidence.runtime_env_encryption_logs_clear,
            ),
            (
                "django_ray_secret_preserved",
                self.evidence.django_ray_secret_preserved,
            ),
            (
                "ray_job_request_reference_carrier",
                self.evidence.ray_job_request_reference_carrier,
            ),
            ("ray_job_raw_info_clear", self.evidence.ray_job_raw_info_clear),
            ("ray_job_processes_clear", self.evidence.ray_job_processes_clear),
            ("ray_job_logs_clear", self.evidence.ray_job_logs_clear),
            (
                "ray_job_manager_reconciled_same_job",
                self.evidence.ray_job_manager_reconciled_same_job,
            ),
            (
                "ray_job_missing_reference_no_marker",
                self.evidence.ray_job_missing_reference_no_marker,
            ),
            (
                "ray_job_missing_reference_no_retry",
                self.evidence.ray_job_missing_reference_no_retry,
            ),
            (
                "protocol_legacy_cohort_visible",
                self.evidence.protocol_legacy_cohort_visible,
            ),
            (
                "protocol_explicit_cohort_visible",
                self.evidence.protocol_explicit_cohort_visible,
            ),
            (
                "protocol_v1_handoff_same_job",
                self.evidence.protocol_v1_handoff_same_job,
            ),
            (
                "protocol_v1_handoff_no_resubmit",
                self.evidence.protocol_v1_handoff_no_resubmit,
            ),
            (
                "protocol_v1_queued_survived_handoff",
                self.evidence.protocol_v1_queued_survived_handoff,
            ),
            (
                "protocol_v2_queued_unchanged",
                self.evidence.protocol_v2_queued_unchanged,
            ),
            (
                "protocol_v2_unsupported_visible",
                self.evidence.protocol_v2_unsupported_visible,
            ),
            (
                "protocol_v2_preinvocation_rejected",
                self.evidence.protocol_v2_preinvocation_rejected,
            ),
            (
                "protocol_v2_application_marker_absent",
                self.evidence.protocol_v2_application_marker_absent,
            ),
            (
                "protocol_v2_target_exact_completed",
                self.evidence.protocol_v2_target_exact_completed,
            ),
            (
                "protocol_v2_target_mismatch_rejected",
                self.evidence.protocol_v2_target_mismatch_rejected,
            ),
            (
                "protocol_v2_target_mismatch_marker_absent",
                self.evidence.protocol_v2_target_mismatch_marker_absent,
            ),
            (
                "protocol_handoff_cleanup_restored",
                self.evidence.protocol_handoff_cleanup_restored,
            ),
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
            (
                "workflow_terminal_only_task_id",
                self.evidence.workflow_terminal_only_task_id,
            ),
            (
                "workflow_terminal_only_task_state",
                self.evidence.workflow_terminal_only_task_state,
            ),
            (
                "workflow_terminal_only_attempt_number",
                self.evidence.workflow_terminal_only_attempt_number,
            ),
            (
                "workflow_terminal_only_schema_version",
                self.evidence.workflow_terminal_only_schema_version,
            ),
            (
                "workflow_terminal_only_summary_revision",
                self.evidence.workflow_terminal_only_summary_revision,
            ),
            ("workflow_terminal_only_reporting_policy", "terminal_only"),
            (
                "workflow_terminal_only_detail_availability",
                "OMITTED_BY_POLICY",
            ),
            ("workflow_terminal_only_topology_version", None),
            ("workflow_terminal_only_detail_revision", None),
            (
                "workflow_terminal_only_declared_nodes",
                self.evidence.workflow_terminal_only_declared_nodes,
            ),
            (
                "workflow_terminal_only_declared_edges",
                self.evidence.workflow_terminal_only_declared_edges,
            ),
            ("workflow_terminal_only_legacy_progress", None),
            (
                "workflow_terminal_only_admin_actions",
                self.evidence.workflow_terminal_only_admin_actions,
            ),
            (
                "workflow_terminal_only_graph_advertised",
                self.evidence.workflow_terminal_only_graph_advertised,
            ),
            ("workflow_terminal_only_graph_status", "UNAVAILABLE"),
            (
                "workflow_terminal_only_storage_rows",
                self.evidence.workflow_terminal_only_storage_rows,
            ),
            (
                "workflow_terminal_only_failure_task_id",
                self.evidence.workflow_terminal_only_failure_task_id,
            ),
            (
                "workflow_terminal_only_failure_task_state",
                self.evidence.workflow_terminal_only_failure_task_state,
            ),
            (
                "workflow_terminal_only_failure_attempt_number",
                self.evidence.workflow_terminal_only_failure_attempt_number,
            ),
            (
                "workflow_terminal_only_failure_schema_version",
                self.evidence.workflow_terminal_only_failure_schema_version,
            ),
            (
                "workflow_terminal_only_failure_summary_revision",
                self.evidence.workflow_terminal_only_failure_summary_revision,
            ),
            ("workflow_terminal_only_failure_reporting_policy", "terminal_only"),
            (
                "workflow_terminal_only_failure_detail_availability",
                "OMITTED_BY_POLICY",
            ),
            ("workflow_terminal_only_failure_topology_version", None),
            ("workflow_terminal_only_failure_detail_revision", None),
            (
                "workflow_terminal_only_failure_declared_nodes",
                self.evidence.workflow_terminal_only_failure_declared_nodes,
            ),
            (
                "workflow_terminal_only_failure_declared_edges",
                self.evidence.workflow_terminal_only_failure_declared_edges,
            ),
            ("workflow_terminal_only_failure_legacy_progress", None),
            (
                "workflow_terminal_only_failure_admin_actions",
                self.evidence.workflow_terminal_only_failure_admin_actions,
            ),
            (
                "workflow_terminal_only_failure_graph_advertised",
                self.evidence.workflow_terminal_only_failure_graph_advertised,
            ),
            ("workflow_terminal_only_failure_graph_status", "UNAVAILABLE"),
            (
                "workflow_terminal_only_failure_storage_rows",
                self.evidence.workflow_terminal_only_failure_storage_rows,
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
        showcase_evidence_fields = (
            "task_id",
            "task_state",
            "attempt_number",
            "topology_nodes",
            "topology_edges",
            "longest_path_layers",
            "detail_links",
            "failure_task_id",
            "failure_task_state",
            "failure_attempt_number",
            "failure_failed_nodes",
            "failure_pending_descendants",
            "failure_running_nodes",
            "failure_succeeded_nodes",
            "failure_path_nodes",
            "failure_detail_links",
        )
        fields.extend(
            (
                f"workflow_showcase_{field_name}",
                getattr(self.evidence, f"workflow_showcase_{field_name}"),
            )
            for field_name in showcase_evidence_fields
        )
        recovery_evidence_fields = (
            "task_id",
            "task_state",
            "attempt_number",
            "attempt_count",
            "distinct_runs",
            "early_topology_nodes",
            "early_topology_edges",
            "early_pending_nodes",
            "early_succeeded_nodes",
            "early_failed_nodes",
            "early_detail_links",
            "mid_topology_nodes",
            "mid_topology_edges",
            "mid_pending_nodes",
            "mid_succeeded_nodes",
            "mid_failed_nodes",
            "mid_detail_links",
            "success_topology_nodes",
            "success_topology_edges",
            "success_succeeded_nodes",
            "success_detail_links",
            "admin_attempts",
        )
        fields.extend(
            (
                f"workflow_recovery_{field_name}",
                getattr(self.evidence, f"workflow_recovery_{field_name}"),
            )
            for field_name in recovery_evidence_fields
        )
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
        runtime_env_logs_safe = (
            layer != "runtime-env-encryption" or self._runtime_env_fixture_values_registered
        )
        commands: list[tuple[str, ...]] = [
            ("get", "pods,deployments,jobs,pvc", "-o", "wide"),
        ]
        if layer == "setup":
            commands.append(("logs", f"job/{SETUP_JOB}", "--tail=60"))
        if (
            layer
            in {
                "workloads",
                "ray",
                "runtime-env",
                "protocol-handoff-recovery",
                "ray-job-request-reference",
                "protocol-handoff",
                "runtime-env-encryption",
                "rollouts",
            }
            and runtime_env_logs_safe
        ):
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
        if (
            layer
            in {
                "rollouts",
                "app-convergence",
                "image-identity",
                "protocol-handoff-recovery",
                "probes",
                "api-smoke",
                "ray-job-request-reference",
                "protocol-handoff",
                "runtime-env-encryption",
                "workflow-progress",
                "workflow-admin",
            }
            and runtime_env_logs_safe
        ):
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
