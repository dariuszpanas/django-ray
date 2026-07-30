"""Contract tests for the guarded local KubeRay final integration gate."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, quote_plus, urlencode
from urllib.request import ProxyHandler

import pytest
import yaml

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_gate import (
    APP_DEPLOYMENTS,
    DOCKER_CONTEXT_ALLOWLISTS,
    EVIDENCE_LINE_LIMIT,
    EXPECTED_NAMESPACE,
    EXPECTED_PROBE_HOST,
    EXPECTED_PROBE_PATH,
    EXPECTED_RESOURCE_IDENTITIES,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_OUTPUT_CHARACTERS,
    RAY_CLUSTER_LABEL,
    RAY_CLUSTER_NAME,
    RUNTIME_ENV_ENCRYPTION_ENV,
    RUNTIME_ENV_ENCRYPTION_PROBE_PATH,
    RUNTIME_ENV_FAILURE_FIXTURE_SCRIPT,
    RUNTIME_ENV_REQUIRED_MEMBER,
    RUNTIME_ENV_STORAGE_PROBE_MARKER,
    SETUP_JOB,
    TASK_MANAGER_DEPLOYMENTS,
    CommandError,
    CommandResult,
    DeploymentContract,
    GateConfig,
    GateError,
    LocalKubeRayGate,
    PodImageContract,
    Redactor,
    RejectRedirects,
    Runner,
    build_local_http_opener,
    configure_overlay_copy,
    create_source_build_context,
    expected_ray_topology,
    inspect_docker_context_allowlists,
    inspect_kubeconfig_snapshot,
    inspect_probe_contract,
    inspect_rendered_resources,
    inspect_runtime_env_encryption_overlay,
    inspect_runtime_env_encryption_secret_data,
    inspect_setup_log,
    load_rendered_resources,
    normalize_ray_topology,
    normalize_runtime_image_id,
    parse_docker_image_inspect,
    parse_runtime_archive_probe,
    parse_task_result,
    pod_image_contract,
    register_kubeconfig_secrets,
    secret_data_sha256,
    source_bound_tag,
    split_apply_resources,
    validate_local_context,
    validate_local_docker_endpoint,
    validate_local_http_url,
    validate_namespace,
    validate_runtime_env_encryption_envelope,
)

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
SOURCE_TREE = "e" * 40
TAG = "local-gate-tree-eeeeeeeeeeee-20260721123456-deadbeef"
APP_TAG = f"django-ray:{TAG}"
IMAGE_ID = f"sha256:{'b' * 64}"
TASK_ID = "15200000-0000-4000-8000-000000000001"
WORKFLOW_TASK_ID = "25200000-0000-4000-8000-000000000002"
WORKFLOW_RUN_ID = "35200000-0000-4000-8000-000000000003"
FAILED_WORKFLOW_TASK_ID = "45200000-0000-4000-8000-000000000004"
FAILED_WORKFLOW_RUN_ID = "55200000-0000-4000-8000-000000000005"
TERMINAL_ONLY_WORKFLOW_TASK_ID = "65200000-0000-4000-8000-000000000006"
TERMINAL_ONLY_WORKFLOW_RUN_ID = "75200000-0000-4000-8000-000000000007"
TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID = "85200000-0000-4000-8000-000000000008"
TERMINAL_ONLY_FAILED_WORKFLOW_RUN_ID = "95200000-0000-4000-8000-000000000009"
WORKFLOW_SHOWCASE_TASK_ID = "a5200000-0000-4000-8000-00000000000a"
WORKFLOW_SHOWCASE_RUN_ID = "b5200000-0000-4000-8000-00000000000b"
FAILED_WORKFLOW_SHOWCASE_TASK_ID = "c5200000-0000-4000-8000-00000000000c"
FAILED_WORKFLOW_SHOWCASE_RUN_ID = "d5200000-0000-4000-8000-00000000000d"
TOKEN68 = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/=="
RUNTIME_ENV_CANARY_TASK_ID = "a5200000-0000-4000-8000-000000000010"
RUNTIME_ENV_TAMPER_TASK_ID = "b5200000-0000-4000-8000-000000000011"
RUNTIME_ENV_UNKNOWN_KEY_TASK_ID = "c5200000-0000-4000-8000-000000000012"


def _token_representations(token: str) -> tuple[str, ...]:
    json_value = json.dumps(token)
    return (
        token,
        json_value,
        json_value.replace("/", r"\/"),
        repr(token),
        f"Authorization: Bearer {token}",
        quote(token, safe=""),
        quote(token),
        quote(token, safe="+/"),
        quote_plus(token, safe="+/"),
        urlencode({"token": token}),
        base64.b64encode(token.encode()).decode(),
    )


def _percent_hex_case_variants(value: str) -> tuple[str, ...]:
    """Return every upper/lower hex-letter spelling of percent escapes."""

    offsets = [
        offset
        for match in re.finditer(r"%[0-9A-Fa-f]{2}", value)
        for offset in range(match.start() + 1, match.end())
        if value[offset].isalpha()
    ]
    variants: list[str] = []
    for mask in range(1 << len(offsets)):
        characters = list(value)
        for bit, offset in enumerate(offsets):
            characters[offset] = (
                characters[offset].lower() if mask & (1 << bit) else characters[offset].upper()
            )
        variants.append("".join(characters))
    return tuple(variants)


def _container(name: str, image: str = APP_TAG) -> dict[str, object]:
    return {"name": name, "image": image}


def _deployment(
    name: str,
    *,
    namespace: str = EXPECTED_NAMESPACE,
    image: str = APP_TAG,
) -> dict[str, object]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [_container(name, image)]},
            },
        },
    }


def _setup_job(*, namespace: str = EXPECTED_NAMESPACE) -> dict[str, object]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": SETUP_JOB, "namespace": namespace},
        "spec": {
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [_container("django-setup")],
                }
            }
        },
    }


def _ray_cluster(
    *,
    namespace: str = EXPECTED_NAMESPACE,
    uid: str | None = None,
    workers: int = 1,
) -> dict[str, object]:
    metadata: dict[str, object] = {"name": RAY_CLUSTER_NAME, "namespace": namespace}
    if uid is not None:
        metadata["uid"] = uid
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayCluster",
        "metadata": metadata,
        "spec": {
            "rayVersion": "2.56.0",
            "enableInTreeAutoscaling": False,
            "headGroupSpec": {
                "serviceType": "NodePort",
                "rayStartParams": {"num-cpus": "1"},
                "template": {
                    "spec": {
                        "containers": [{"name": "ray-head", "image": "rayproject/ray:2.56.0-py312"}]
                    }
                },
            },
            "workerGroupSpecs": [
                {
                    "groupName": "worker-group",
                    "minReplicas": workers,
                    "replicas": workers,
                    "maxReplicas": workers,
                    "rayStartParams": {"num-cpus": "1"},
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "ray-worker",
                                    "image": "rayproject/ray:2.56.0-py312",
                                }
                            ]
                        }
                    },
                }
            ],
        },
    }


def _resources() -> list[dict[str, object]]:
    resources: list[dict[str, object]] = []
    for api_version, kind, name in sorted(EXPECTED_RESOURCE_IDENTITIES):
        if kind == "Namespace":
            resources.append({"apiVersion": api_version, "kind": kind, "metadata": {"name": name}})
        elif (api_version, kind, name) == ("batch/v1", "Job", SETUP_JOB):
            resources.append(_setup_job())
        elif (api_version, kind, name) == ("ray.io/v1", "RayCluster", RAY_CLUSTER_NAME):
            resources.append(_ray_cluster())
        elif kind == "Deployment":
            resources.append(
                _deployment(
                    name,
                    image=APP_TAG if name in APP_DEPLOYMENTS else "busybox:1.36",
                )
            )
        else:
            resources.append(
                {
                    "apiVersion": api_version,
                    "kind": kind,
                    "metadata": {"name": name, "namespace": EXPECTED_NAMESPACE},
                }
            )
    return resources


def _runtime_env_encryption_resources() -> list[dict[str, Any]]:
    resources = cast(list[dict[str, Any]], _resources())
    for resource in resources:
        if (
            resource.get("kind") == "Deployment"
            and resource.get("metadata", {}).get("name") in APP_DEPLOYMENTS
        ):
            container = resource["spec"]["template"]["spec"]["containers"][0]
            container["env"] = [
                {"name": name, "value": value} for name, value in RUNTIME_ENV_ENCRYPTION_ENV.items()
            ]
    return resources


def _config(
    *,
    ray_restart: str = "required",
    context: str = "docker-desktop",
    kind_cluster_name: str | None = None,
    rollout_timeout: int = 300,
) -> GateConfig:
    return GateConfig(
        root=ROOT,
        context=context,
        namespace=EXPECTED_NAMESPACE,
        ray_restart=ray_restart,
        web_url="http://django-ray.localhost:30080",
        prometheus_url="http://prometheus.localhost:30080",
        kind_cluster_name=kind_cluster_name,
        rollout_timeout=rollout_timeout,
        task_timeout=180,
        prometheus_timeout=120,
        command_timeout=120,
        build_timeout=1200,
        kubectl_request_timeout=30,
        preflight_only=False,
    )


def _kubeconfig_payload(
    *,
    context: str = "docker-desktop",
    server: str = "https://kubernetes.docker.internal:6443",
) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": context,
        "clusters": [{"name": "local-cluster", "cluster": {"server": server}}],
        "contexts": [
            {
                "name": context,
                "context": {"cluster": "local-cluster", "user": "local-user"},
            }
        ],
        "users": [{"name": "local-user", "user": {"token": "kube-token"}}],
    }


def _wait_gcs_ready_script() -> str:
    address = f"{RAY_CLUSTER_NAME}-head-svc.{EXPECTED_NAMESPACE}.svc.cluster.local:6379"
    return "\n".join(
        (
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
    )


def _ray_pod(
    name: str,
    component: str,
    uid: str,
    *,
    image: str = "rayproject/ray:2.56.0-py312",
) -> dict[str, object]:
    container_name = "ray-head" if component == "head" else "ray-worker"
    labels = {
        "app": "ray",
        "component": component,
        RAY_CLUSTER_LABEL: RAY_CLUSTER_NAME,
    }
    if component == "worker":
        labels[gate_module.RAY_GROUP_LABEL] = "worker-group"
    spec: dict[str, object] = {"containers": [{"name": container_name, "image": image}]}
    status: dict[str, object] = {
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": [
            {
                "name": container_name,
                "image": image,
                "imageID": f"containerd://sha256:{'c' * 64}",
                "ready": True,
            }
        ],
    }
    if component == "worker":
        spec["initContainers"] = [
            {
                "name": gate_module.KUBERAY_WAIT_GCS_INIT,
                "image": image,
                "command": ["/bin/bash", "-c", "--"],
                "args": [_wait_gcs_ready_script()],
            }
        ]
        status["initContainerStatuses"] = [
            {
                "name": gate_module.KUBERAY_WAIT_GCS_INIT,
                "image": image,
                "imageID": f"containerd://sha256:{'c' * 64}",
                "ready": True,
                "restartCount": 0,
                "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
            }
        ]
    return {
        "metadata": {
            "name": name,
            "namespace": EXPECTED_NAMESPACE,
            "uid": uid,
            "labels": labels,
            "ownerReferences": [
                {
                    "apiVersion": "ray.io/v1",
                    "kind": "RayCluster",
                    "name": RAY_CLUSTER_NAME,
                    "uid": "cluster-owner",
                    "controller": True,
                }
            ],
        },
        "spec": spec,
        "status": status,
    }


def _set_ray_topology(gate: LocalKubeRayGate, *, workers: int = 1) -> None:
    gate.rendered_ray_topology = normalize_ray_topology(
        cast(dict[str, Any], _ray_cluster(workers=workers))
    )
    gate.expected_ray_head_count = 1
    gate.expected_ray_worker_count = workers


def _setup_pod(
    *,
    owner_uid: str = "setup-owner",
    name: str = "django-setup",
    image: str = APP_TAG,
    image_id: str = IMAGE_ID,
    extra_containers: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    containers = [{"name": name, "image": image}, *(extra_containers or [])]
    statuses = [
        {
            "name": container["name"],
            "image": container["image"],
            "imageID": (
                f"containerd://{image_id}"
                if container["image"] == APP_TAG
                else f"containerd://sha256:{'d' * 64}"
            ),
            "ready": False,
        }
        for container in containers
    ]
    return {
        "metadata": {
            "name": "django-setup-pod",
            "namespace": EXPECTED_NAMESPACE,
            "uid": "setup-pod-uid",
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": SETUP_JOB,
                    "uid": owner_uid,
                    "controller": True,
                }
            ],
        },
        "spec": {"containers": containers},
        "status": {"containerStatuses": statuses},
    }


def _live_application_deployment(
    name: str, *, labels: dict[str, str], replicas: int = 1, revision: str = "3"
) -> dict[str, object]:
    deployment = _deployment(name)
    metadata = cast(dict[str, object], deployment["metadata"])
    metadata.update(
        {
            "uid": f"deployment-{name}",
            "generation": 3,
            "annotations": {"deployment.kubernetes.io/revision": revision},
        }
    )
    spec = cast(dict[str, object], deployment["spec"])
    spec["replicas"] = replicas
    spec["selector"] = {"matchLabels": labels}
    template = cast(dict[str, Any], spec["template"])
    template["metadata"] = {"labels": labels}
    deployment["status"] = {
        "observedGeneration": 3,
        "replicas": replicas,
        "updatedReplicas": replicas,
        "readyReplicas": replicas,
        "availableReplicas": replicas,
    }
    return deployment


def _application_replicaset(
    name: str,
    *,
    labels: dict[str, str] | None = None,
    revision: str = "3",
    replicas: int = 1,
    image: str = APP_TAG,
) -> dict[str, object]:
    selector_labels = dict(labels or {"app": name})
    selector_labels["pod-template-hash"] = f"hash-{revision}"
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {
            "name": f"{name}-rs",
            "namespace": EXPECTED_NAMESPACE,
            "uid": f"replicaset-{name}",
            "annotations": {"deployment.kubernetes.io/revision": revision},
            "labels": selector_labels,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": name,
                    "uid": f"deployment-{name}",
                    "controller": True,
                }
            ],
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector_labels},
            "template": {
                "metadata": {"labels": selector_labels},
                "spec": {"containers": [{"name": name, "image": image}]},
            },
        },
        "status": {
            "replicas": replicas,
            "readyReplicas": replicas,
            "availableReplicas": replicas,
        },
    }


def _application_pod(
    name: str, *, labels: dict[str, str], revision: str = "3"
) -> dict[str, object]:
    pod_labels = dict(labels)
    pod_labels["pod-template-hash"] = f"hash-{revision}"
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"{name}-pod",
            "namespace": EXPECTED_NAMESPACE,
            "uid": f"pod-{name}",
            "labels": pod_labels,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": f"{name}-rs",
                    "uid": f"replicaset-{name}",
                    "controller": True,
                }
            ],
        },
        "spec": {"containers": [{"name": name, "image": APP_TAG}]},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": name,
                    "image": APP_TAG,
                    "imageID": f"containerd://{IMAGE_ID}",
                    "ready": True,
                }
            ],
        },
    }


def _application_inventory_fixture(
    *, rollout_timeout: int = 300
) -> tuple[
    LocalKubeRayGate,
    dict[str, dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, dict[str, str]],
]:
    selectors = {name: {"app": name} for name in APP_DEPLOYMENTS}
    gate = LocalKubeRayGate(_config(rollout_timeout=rollout_timeout))
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    deployments = {
        name: _live_application_deployment(name, labels=selectors[name]) for name in APP_DEPLOYMENTS
    }
    replicasets = [
        _application_replicaset(name, labels=selectors[name]) for name in APP_DEPLOYMENTS
    ]
    pods = [_application_pod(name, labels=selectors[name]) for name in APP_DEPLOYMENTS]
    for name in APP_DEPLOYMENTS:
        gate.deployment_contracts[name] = DeploymentContract(
            1,
            PodImageContract((), ((name, APP_TAG),)),
            tuple(sorted(selectors[name].items())),
        )
    return gate, deployments, replicasets, pods, selectors


def _old_terminating_application_generation(
    name: str,
    *,
    labels: dict[str, str],
) -> tuple[dict[str, object], dict[str, object]]:
    old_image = "django-ray:old-generation"
    replicaset = _application_replicaset(
        name,
        labels=labels,
        revision="2",
        replicas=0,
        image=old_image,
    )
    replicaset_metadata = cast(dict[str, Any], replicaset["metadata"])
    replicaset_metadata["name"] = f"{name}-old-rs"
    replicaset_metadata["uid"] = f"replicaset-{name}-old"
    pod = _application_pod(name, labels=labels, revision="2")
    pod_metadata = cast(dict[str, Any], pod["metadata"])
    pod_metadata.update(
        {
            "name": f"{name}-old-pod",
            "uid": f"pod-{name}-old",
            "deletionTimestamp": "2026-07-22T03:00:00Z",
        }
    )
    owner = cast(list[dict[str, Any]], pod_metadata["ownerReferences"])[0]
    owner["name"] = f"{name}-old-rs"
    owner["uid"] = f"replicaset-{name}-old"
    pod_spec = cast(dict[str, Any], pod["spec"])
    containers = cast(list[dict[str, Any]], pod_spec["containers"])
    containers[0]["image"] = old_image
    pod_status = cast(dict[str, Any], pod["status"])
    statuses = cast(list[dict[str, Any]], pod_status["containerStatuses"])
    statuses[0].update({"image": old_image, "imageID": "", "ready": False})
    pod_status["conditions"] = [{"type": "Ready", "status": "False"}]
    return replicaset, pod


def test_namespace_guard_accepts_only_the_dedicated_local_namespace() -> None:
    validate_namespace(EXPECTED_NAMESPACE)

    with pytest.raises(ValueError, match="exactly 'django-ray'"):
        validate_namespace("default")


@pytest.mark.parametrize(
    ("context", "server"),
    [
        ("docker-desktop", "https://kubernetes.docker.internal:6443"),
        ("kind-django-ray", "https://127.0.0.1:57321"),
    ],
)
def test_context_guard_accepts_named_local_clusters(context: str, server: str) -> None:
    validate_local_context(current=context, expected=context, server_url=server)


@pytest.mark.parametrize(
    ("current", "expected", "server", "message"),
    [
        ("production", "docker-desktop", "https://127.0.0.1:6443", "active Kubernetes"),
        ("production", "production", "https://127.0.0.1:6443", "context must be"),
        (
            "kind-local",
            "kind-local",
            "https://api.production.example.com:6443",
            "non-local Kubernetes API",
        ),
    ],
)
def test_context_guard_fails_closed(current: str, expected: str, server: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_local_context(current=current, expected=expected, server_url=server)


def test_flattened_kubeconfig_snapshot_binds_one_local_context() -> None:
    payload = _kubeconfig_payload()

    assert (
        inspect_kubeconfig_snapshot(payload, expected_context="docker-desktop")
        == "https://kubernetes.docker.internal:6443"
    )

    drifted = _kubeconfig_payload(server="https://production.example.com:6443")
    with pytest.raises(ValueError, match="non-local Kubernetes API"):
        inspect_kubeconfig_snapshot(drifted, expected_context="docker-desktop")

    proxied = _kubeconfig_payload()
    clusters = cast(list[dict[str, Any]], proxied["clusters"])
    cluster = cast(dict[str, Any], clusters[0]["cluster"])
    cluster["proxy-url"] = "https://production.example.invalid"
    with pytest.raises(ValueError, match="must not route through a proxy URL"):
        inspect_kubeconfig_snapshot(proxied, expected_context="docker-desktop")


def test_kubeconfig_static_auth_and_exec_credentials_are_registered_before_use() -> None:
    secrets = {
        "token": "kube-static-token-marker",
        "password": "kube-password-marker",
        "client-key-data": base64.b64encode(b"private-key-marker").decode(),
        "auth": "auth-provider-token-marker",
        "exec_arg": "exec-argument-token-marker",
        "exec_inline": "exec-inline-secret-marker",
        "exec_env": "exec-environment-token-marker",
    }
    payload = _kubeconfig_payload()
    users = cast(list[dict[str, Any]], payload["users"])
    users[0]["user"] = {
        "token": secrets["token"],
        "password": secrets["password"],
        "client-key-data": secrets["client-key-data"],
        "auth-provider": {
            "name": "oidc",
            "config": {"id-token": secrets["auth"], "issuer": "https://issuer.invalid"},
        },
        "exec": {
            "command": "credential-helper",
            "args": [
                "get",
                "--token",
                secrets["exec_arg"],
                f"--client-secret={secrets['exec_inline']}",
            ],
            "env": [{"name": "ACCESS_TOKEN", "value": secrets["exec_env"]}],
        },
    }
    redactor = Redactor()

    register_kubeconfig_secrets(payload, redactor=redactor)

    serialized = redactor.clean(" ".join(secrets.values()) + " private-key-marker")
    assert "marker" not in serialized
    assert serialized.count("[REDACTED]") == len(secrets) + 1


def test_kubeconfig_credentials_redact_every_percent_escape_hex_case() -> None:
    payload = _kubeconfig_payload()
    users = cast(list[dict[str, Any]], payload["users"])
    users[0]["user"] = {"password": TOKEN68}
    redactor = Redactor()

    register_kubeconfig_secrets(payload, redactor=redactor)

    encoded = quote(TOKEN68, safe="")
    variants = _percent_hex_case_variants(encoded)
    assert len(variants) > 1
    assert all(redactor.clean(variant) == "[REDACTED]" for variant in variants)
    unrelated = quote(TOKEN68.swapcase(), safe="")
    assert redactor.clean(unrelated) == unrelated


def test_cli_defaults_use_gate_owned_direct_nodeports() -> None:
    args = gate_module._parser().parse_args(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "required",
        ]
    )

    assert args.web_url == "http://localhost:30080"
    assert args.prometheus_url == "http://localhost:30090"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:30080",
        "http://django-ray.localhost:30080",
        "https://127.0.0.1:30443",
    ],
)
def test_http_guard_accepts_only_local_urls(url: str) -> None:
    validate_local_http_url(url, option="--web-url")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://token@localhost:30080",
        "http://localhost:30080/?token=secret",
        "file:///tmp/socket",
    ],
)
def test_http_guard_rejects_token_exfiltration_paths(url: str) -> None:
    with pytest.raises(ValueError):
        validate_local_http_url(url, option="--web-url")


def test_http_guard_never_echoes_rejected_credentials_or_query_values() -> None:
    marker = "URL-CREDENTIAL-MARKER"
    for url in (
        f"http://{marker}@evil.example:30080",
        f"http://evil.example:30080/?token={marker}",
    ):
        with pytest.raises(ValueError) as captured:
            validate_local_http_url(url, option="--web-url")
        assert marker not in str(captured.value)
        assert url not in str(captured.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "npipe:////./pipe/dockerDesktopLinuxEngine",
        "unix:///var/run/docker.sock",
        "tcp://127.0.0.1:2375",
    ],
)
def test_docker_guard_accepts_only_local_daemons(endpoint: str) -> None:
    validate_local_docker_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "ssh://builder@example.com",
        "tcp://docker.example.com:2376",
        "https://10.20.30.40:2376",
        "npipe:////remote-host/pipe/docker_engine",
        "unix://remote-host/var/run/docker.sock",
    ],
)
def test_docker_guard_rejects_remote_daemons(endpoint: str) -> None:
    with pytest.raises(ValueError, match="must be local"):
        validate_local_docker_endpoint(endpoint)


def test_docker_guard_does_not_echo_rejected_credentials() -> None:
    marker = "password-marker"

    with pytest.raises(ValueError, match="credentials") as captured:
        validate_local_docker_endpoint(f"tcp://user:{marker}@localhost:2375")

    assert marker not in str(captured.value)


def test_local_http_opener_disables_proxies_and_rejects_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    opener = build_local_http_opener()
    redirects = next(handler for handler in opener.handlers if isinstance(handler, RejectRedirects))

    assert not any(isinstance(handler, ProxyHandler) for handler in opener.handlers)
    assert (
        redirects.redirect_request(
            None,  # type: ignore[arg-type]
            None,
            302,
            "Found",
            {},
            "https://example.invalid/token-sink",
        )
        is None
    )


def test_real_http_path_allows_internal_queries_without_crossing_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "local-token-that-must-never-be-printed-123456"

    class Response:
        def __init__(self, status: int, body: object) -> None:
            self.status = status
            self.body = json.dumps(body).encode()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

    class ScriptedOpener:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, bool]] = []

        def open(self, request: Any, *, timeout: float) -> Response:
            assert timeout == 10
            authenticated = request.get_header("Authorization") == f"Bearer {token}"
            self.requests.append((request.full_url, request.get_method(), authenticated))
            path = request.full_url.removeprefix("http://django-ray.localhost:30080")
            if path == "/api/openapi.json":
                return Response(
                    200,
                    {
                        "paths": {
                            "/api/executions/{execution_id}": {
                                "get": {"operationId": "get_execution"}
                            }
                        },
                        "components": {
                            "schemas": {
                                "bounded-large-schema": {"description": "x" * MAX_OUTPUT_CHARACTERS}
                            }
                        },
                    },
                )
            if not authenticated:
                return Response(401, {})
            if path == "/api/enqueue/add/2/3":
                return Response(200, {"task_id": TASK_ID})
            if path == f"/api/executions?task_id={TASK_ID}&limit=1":
                return Response(
                    200,
                    {
                        "tasks": [
                            {
                                "id": 17,
                                "task_id": TASK_ID,
                                "state": "SUCCEEDED",
                                "result_data": "5",
                            }
                        ]
                    },
                )
            if path == "/api/executions/17" and request.get_method() == "DELETE":
                return Response(405, {})
            if path == "/api/executions/17":
                return Response(
                    200,
                    {
                        "id": 17,
                        "task_id": TASK_ID,
                        "state": "SUCCEEDED",
                        "result_data": "5",
                    },
                )
            return Response(200, {})

    opener = ScriptedOpener()
    gate = LocalKubeRayGate(_config())
    gate.http_opener = cast(Any, opener)
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    gate._verify_api()

    assert (
        f"http://django-ray.localhost:30080/api/executions?task_id={TASK_ID}&limit=1",
        "GET",
        True,
    ) in opener.requests
    assert gate.evidence.task_state == "SUCCEEDED"
    assert gate.evidence.task_result == 5
    assert gate.evidence.api_execution_delete_rejected is True
    assert gate.evidence.api_legacy_workflow_graph_absent is True


def test_http_reader_accepts_workflow_pages_larger_than_diagnostic_output() -> None:
    payload = {
        "schema": "django-ray.workflow-progress-page",
        "items": [{"message": "x" * (MAX_OUTPUT_CHARACTERS + 1_000)}],
    }
    encoded = json.dumps(payload).encode()
    read_limits: list[int] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            read_limits.append(limit)
            return encoded[:limit]

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            assert request.full_url.endswith("/api/cluster/workflows/task/nodes?limit=64")
            assert timeout == 10
            return Response()

    assert MAX_OUTPUT_CHARACTERS < len(encoded) < MAX_HTTP_RESPONSE_BYTES
    gate = LocalKubeRayGate(_config())
    gate.http_opener = cast(Any, Opener())

    status, body = gate._http(
        "/api/cluster/workflows/task/nodes?limit=64",
        method="GET",
    )

    assert status == 200
    assert body == encoded
    assert json.loads(body) == payload
    assert read_limits == [MAX_HTTP_RESPONSE_BYTES + 1]


@pytest.mark.parametrize(
    "path",
    [
        "http://localhost:30080/api/executions?limit=1",
        "http://django-ray.localhost:30081/api/executions?limit=1",
        "https://django-ray.localhost:30080/api/executions?limit=1",
        "http://user@django-ray.localhost:30080/api/executions?limit=1",
        "http://django-ray.localhost:30080/api/executions#fragment",
    ],
)
def test_real_http_path_rejects_local_credential_boundary_changes(path: str) -> None:
    gate = LocalKubeRayGate(_config())

    with pytest.raises(ValueError):
        gate._http(path, method="GET", headers={"Authorization": "Bearer marker"})


def test_runner_timeout_is_bounded_and_redacts_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "timeout-token-that-must-be-redacted"
    redactor = Redactor()
    redactor.register(token)
    runner = Runner(redactor=redactor, timeout_seconds=7)

    def time_out(*args: object, **kwargs: object) -> None:
        assert kwargs["timeout"] == 3
        raise subprocess.TimeoutExpired(
            cmd=["fake"],
            timeout=3,
            output=f"partial {token}",
            stderr=f"error {token}",
        )

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(CommandError, match="timed out after 3s") as captured:
        runner.run(["fake"], cwd=ROOT, timeout=3)

    assert token not in str(captured.value)
    assert str(captured.value).count("[REDACTED]") == 2
    assert isinstance(captured.value.__cause__, subprocess.TimeoutExpired)


def test_runner_sensitive_failures_never_expose_unregistered_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded_secret = base64.b64encode(b"A" * 40).decode()

    def time_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=["kubectl"],
            timeout=3,
            output=f'{{"data":{{"DJANGO_API_TOKEN":"{encoded_secret}"}}}}',
            stderr=encoded_secret,
        )

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(CommandError, match="sensitive command output suppressed") as captured:
        Runner(timeout_seconds=3).run(
            ["kubectl", "get", "secret"],
            cwd=ROOT,
            sensitive_output=True,
        )

    assert encoded_secret not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_runner_sensitive_nonzero_failures_never_expose_unregistered_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "unregistered-secret-from-stderr"
    encoded_secret = base64.b64encode(secret.encode()).decode()

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["kubectl"],
            returncode=1,
            stdout=f"plaintext={secret}",
            stderr=f"encoded={encoded_secret}",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(CommandError, match="sensitive command output suppressed") as captured:
        Runner().run(
            ["kubectl", "get", "secret"],
            cwd=ROOT,
            sensitive_output=True,
        )

    assert secret not in str(captured.value)
    assert encoded_secret not in str(captured.value)


def test_private_json_parsers_drop_raw_payload_from_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_value = "private-json-value-that-must-not-enter-the-exception-graph"
    malformed = f'{{"private":"{private_value}"'
    gate = LocalKubeRayGate(_config())

    with pytest.raises(ValueError, match="did not return valid JSON") as api_error:
        gate._json_body(malformed.encode(), endpoint="private API")

    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(malformed, "", 0),
    )
    with pytest.raises(ValueError, match="valid private JSON") as shell_error:
        gate._sensitive_django_shell("print('private')", field_name="private shell")
    with pytest.raises(
        ValueError, match="Secret/django-ray-secret is not valid JSON"
    ) as secret_error:
        gate._secret_data()

    for error in (api_error.value, shell_error.value, secret_error.value):
        assert private_value not in str(error)
        assert error.__cause__ is None
        assert error.__context__ is None


def test_private_kubeconfig_json_failures_drop_raw_payload_from_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_value = "private-kubeconfig-value-that-must-not-enter-the-exception-graph"
    malformed = f'{{"private":"{private_value}"'

    creation_gate = LocalKubeRayGate(_config())
    creation_gate.temp_root = tmp_path
    monkeypatch.setattr(
        creation_gate.runner,
        "run",
        lambda *args, **kwargs: CommandResult(malformed, "", 0),
    )
    with pytest.raises(ValueError, match="flattened kubeconfig") as creation_error:
        creation_gate._create_kubeconfig_snapshot(current_context=_config().context)

    snapshot = tmp_path / "malformed-kubeconfig.json"
    snapshot.write_text(malformed, encoding="utf-8")
    verification_gate = LocalKubeRayGate(_config())
    verification_gate.kubeconfig_path = snapshot
    verification_gate._kubeconfig_digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="no longer valid JSON") as verification_error:
        verification_gate._verify_kubeconfig_snapshot()

    for error in (creation_error.value, verification_error.value):
        assert private_value not in str(error)
        assert error.__cause__ is None
        assert error.__context__ is None


def test_runner_redacts_before_bounding_failure_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "0123456789abcdefghijKLMNOPQRSTuvwxyzABCD"
    redactor = Redactor()
    redactor.register(token)

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["fake"],
            returncode=1,
            stdout=("p" * 100) + token + ("z" * (MAX_OUTPUT_CHARACTERS - 20)),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(CommandError) as captured:
        Runner(redactor=redactor).run(["fake"], cwd=ROOT)

    assert token[20:] not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


def test_source_build_context_exports_only_the_committed_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Gate Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "gate@example.invalid"],
        cwd=repository,
        check=True,
    )
    for name in ("Dockerfile", "Dockerfile.ray", "pyproject.toml", "uv.lock"):
        (repository / name).write_text(f"tracked {name}\n", encoding="utf-8")
    for name, patterns in DOCKER_CONTEXT_ALLOWLISTS.items():
        (repository / name).write_text("\n".join(patterns) + "\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".env\n", encoding="utf-8")
    (repository / ".env").write_text("DJANGO_API_TOKEN=secret\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            "Dockerfile",
            "Dockerfile.ray",
            "Dockerfile.dockerignore",
            "Dockerfile.ray.dockerignore",
            "pyproject.toml",
            "uv.lock",
            ".gitignore",
        ],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "test: seed archive"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source_tree = subprocess.run(
        ["git", "rev-parse", f"{commit}^{{tree}}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "Dockerfile").write_text("working tree drift\n", encoding="utf-8")
    export_root = tmp_path / "export"
    export_root.mkdir()

    context = create_source_build_context(
        runner=Runner(),
        root=repository,
        temporary_root=export_root,
        commit=commit,
        source_tree=source_tree,
    )

    assert (context / "Dockerfile").is_file()
    assert (context / "Dockerfile").read_text(encoding="utf-8") == "tracked Dockerfile\n"
    assert not (context / ".env").exists()
    assert not (context / ".git").exists()


def test_docker_context_policies_fail_closed_without_exact_specific_allowlists(
    tmp_path: Path,
) -> None:
    for name, patterns in DOCKER_CONTEXT_ALLOWLISTS.items():
        (tmp_path / name).write_text("\n".join(patterns) + "\n", encoding="utf-8")

    inspect_docker_context_allowlists(tmp_path)

    (tmp_path / "Dockerfile.dockerignore").unlink()
    with pytest.raises(ValueError, match="missing required Docker context policy"):
        inspect_docker_context_allowlists(tmp_path)

    (tmp_path / "Dockerfile.dockerignore").write_text("!src/**\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reviewed deny-by-default allowlist"):
        inspect_docker_context_allowlists(tmp_path)


def test_source_bound_tag_contains_tree_time_and_uniqueness() -> None:
    tag = source_bound_tag(
        SOURCE_TREE,
        now=datetime(2026, 7, 21, 12, 34, 56, tzinfo=UTC),
        nonce="deadbeef",
    )

    assert tag == TAG
    assert re.fullmatch(r"local-gate-tree-[0-9a-f]{12}-\d{14}-[0-9a-f]{8}", tag)


def _runtime_env_envelope() -> tuple[str, str, str]:
    nonce = base64.urlsafe_b64encode(b"n" * 12).rstrip(b"=").decode()
    ciphertext = base64.urlsafe_b64encode(b"c" * 48).rstrip(b"=").decode()
    serialized = json.dumps(
        {
            "algorithm": "AES-256-GCM",
            "ciphertext": ciphertext,
            "format": "django-ray.runtime-env.encrypted",
            "key_id": "django-secret",
            "nonce": nonce,
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return serialized, nonce, ciphertext


def test_runtime_env_encryption_envelope_contract_is_exact_and_canonical() -> None:
    serialized, nonce, ciphertext = _runtime_env_envelope()

    assert validate_runtime_env_encryption_envelope(serialized) == (nonce, ciphertext)

    noncanonical = json.dumps(json.loads(serialized))
    with pytest.raises(ValueError, match="not canonical"):
        validate_runtime_env_encryption_envelope(noncanonical)

    exposed = serialized.replace(ciphertext, RUNTIME_ENV_STORAGE_PROBE_MARKER)
    with pytest.raises(ValueError, match="plaintext probe marker"):
        validate_runtime_env_encryption_envelope(exposed)


def test_runtime_env_encryption_envelope_parse_failure_has_no_private_cause() -> None:
    private_value = "raw-envelope-value-that-must-not-enter-the-exception-graph"

    with pytest.raises(ValueError, match="not valid JSON") as captured:
        validate_runtime_env_encryption_envelope(f'{{"ciphertext":"{private_value}"')

    assert private_value not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secret_data_digest_covers_every_value_without_order_dependence() -> None:
    original = {"DJANGO_API_TOKEN": "dG9rZW4=", "DJANGO_SECRET_KEY": "c2VjcmV0"}
    reordered = dict(reversed(tuple(original.items())))

    assert secret_data_sha256(original) == secret_data_sha256(reordered)
    assert secret_data_sha256(original) != secret_data_sha256(
        {**original, "DJANGO_SECRET_KEY": "Y2hhbmdlZA=="}
    )


def test_overlay_copy_replaces_tags_without_editing_repository(tmp_path: Path) -> None:
    source = ROOT / "k8s"
    original = (source / "overlays/kuberay-kind/kustomization.yaml").read_text(encoding="utf-8")

    overlay = configure_overlay_copy(
        source_k8s=source,
        destination_k8s=tmp_path / "k8s",
        tag=TAG,
    )

    copied = yaml.safe_load((overlay / "kustomization.yaml").read_text(encoding="utf-8"))
    assert copied["images"] == [
        {"name": "django-ray", "newName": "django-ray", "newTag": TAG},
        {"name": "django-ray-worker", "newName": "django-ray-worker", "newTag": TAG},
    ]
    assert (source / "overlays/kuberay-kind/kustomization.yaml").read_text(
        encoding="utf-8"
    ) == original


def test_real_kuberay_overlay_is_namespace_scoped_and_source_bound(tmp_path: Path) -> None:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is required to render the KubeRay overlay")
    overlay = configure_overlay_copy(
        source_k8s=ROOT / "k8s",
        destination_k8s=tmp_path / "k8s",
        tag=TAG,
    )
    result = subprocess.run(
        [kubectl, "kustomize", str(overlay)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    resources = load_rendered_resources(result.stdout)
    inspect_rendered_resources(resources, namespace=EXPECTED_NAMESPACE, tag=TAG)
    inspect_runtime_env_encryption_overlay(resources)

    assert all(
        resource["kind"] == "Namespace"
        or resource.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE
        for resource in resources
    )
    assert not any(resource["kind"].startswith("ClusterRole") for resource in resources)


def test_runtime_env_encryption_overlay_is_scoped_to_application_containers() -> None:
    resources = _runtime_env_encryption_resources()

    inspect_runtime_env_encryption_overlay(resources)

    target = next(
        resource
        for resource in resources
        if resource.get("kind") == "Deployment"
        and resource.get("metadata", {}).get("name") == "django-ray-worker-sync"
    )
    target["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "plaintext"
    with pytest.raises(ValueError, match="exactly on django-web"):
        inspect_runtime_env_encryption_overlay(resources)


@pytest.mark.parametrize("location", ["configmap", "secret", "ray", "unknown-envfrom"])
def test_runtime_env_encryption_overlay_rejects_shared_or_ray_selectors(location: str) -> None:
    resources = _runtime_env_encryption_resources()
    if location == "configmap":
        configmap = next(resource for resource in resources if resource.get("kind") == "ConfigMap")
        configmap["data"] = {
            "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE": "encrypted",
        }
    elif location == "secret":
        secret = next(resource for resource in resources if resource.get("kind") == "Secret")
        secret["data"] = {
            "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE": "ZW5jcnlwdGVk",
        }
    elif location == "unknown-envfrom":
        target = next(
            resource
            for resource in resources
            if resource.get("kind") == "Deployment"
            and resource.get("metadata", {}).get("name") == "django-ray-worker-sync"
        )
        target["spec"]["template"]["spec"]["containers"][0].setdefault("envFrom", []).append(
            {"secretRef": {"name": "external-runtime-env"}}
        )
    else:
        ray_cluster = next(
            resource for resource in resources if resource.get("kind") == "RayCluster"
        )
        ray_cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["env"] = [
            {
                "name": "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE",
                "value": "encrypted",
            }
        ]

    with pytest.raises(ValueError):
        inspect_runtime_env_encryption_overlay(resources)


def test_preserved_secret_rejects_runtime_env_selector_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "DJANGO_API_TOKEN": base64.b64encode(TOKEN68.encode()).decode(),
        "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE": "ZW5jcnlwdGVk",
    }

    with pytest.raises(ValueError, match="must not contain"):
        inspect_runtime_env_encryption_secret_data(data)

    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(gate, "_secret_data", lambda: data)
    with pytest.raises(ValueError, match="must not contain"):
        gate._secret_token()


def test_real_kuberay_overlay_pins_exact_static_ray_topology(tmp_path: Path) -> None:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is required to render the KubeRay overlay")
    overlay = configure_overlay_copy(
        source_k8s=ROOT / "k8s",
        destination_k8s=tmp_path / "k8s",
        tag=TAG,
    )
    rendered = subprocess.run(
        [kubectl, "kustomize", str(overlay)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ray_cluster = next(
        resource
        for resource in load_rendered_resources(rendered)
        if resource.get("kind") == "RayCluster"
    )

    assert expected_ray_topology(ray_cluster) == (1, 4)

    ray_cluster["spec"]["workerGroupSpecs"][0]["minReplicas"] = 3
    with pytest.raises(ValueError, match="must pin minReplicas"):
        expected_ray_topology(ray_cluster)


def test_ray_topology_normalization_rejects_equal_count_structural_drift() -> None:
    rendered = _ray_cluster(workers=4)
    live = json.loads(json.dumps(rendered))
    original_group = live["spec"]["workerGroupSpecs"][0]
    first = dict(original_group, groupName="first", minReplicas=2, replicas=2, maxReplicas=2)
    second = dict(original_group, groupName="second", minReplicas=2, replicas=2, maxReplicas=2)
    live["spec"]["workerGroupSpecs"] = [first, second]

    assert expected_ray_topology(rendered) == expected_ray_topology(live) == (1, 4)
    assert normalize_ray_topology(rendered) != normalize_ray_topology(live)

    image_drift = json.loads(json.dumps(rendered))
    image_drift["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = (
        "rayproject/ray:stale"
    )
    assert normalize_ray_topology(rendered) != normalize_ray_topology(image_drift)


def test_live_ray_topology_must_match_the_exact_rendered_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = _ray_cluster(uid="cluster-owner", workers=4)
    live = json.loads(json.dumps(rendered))
    original_group = live["spec"]["workerGroupSpecs"][0]
    live["spec"]["workerGroupSpecs"] = [
        dict(original_group, groupName="first", minReplicas=2, replicas=2, maxReplicas=2),
        dict(original_group, groupName="second", minReplicas=2, replicas=2, maxReplicas=2),
    ]
    gate = LocalKubeRayGate(_config())
    gate.rendered_ray_topology = normalize_ray_topology(rendered)

    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(json.dumps(live), "", 0),
    )

    with pytest.raises(ValueError, match="exact rendered head/worker contract"):
        gate._ray_pods(allow_empty=True)


def test_prometheus_rbac_is_namespaced_and_has_no_node_permission() -> None:
    resources = list(
        yaml.safe_load_all((ROOT / "k8s/base/monitoring.yaml").read_text(encoding="utf-8"))
    )
    role = next(resource for resource in resources if resource.get("kind") == "Role")
    binding = next(resource for resource in resources if resource.get("kind") == "RoleBinding")

    assert role["metadata"] == {"name": "prometheus-django-ray", "namespace": EXPECTED_NAMESPACE}
    assert binding["metadata"]["namespace"] == EXPECTED_NAMESPACE
    assert binding["roleRef"]["kind"] == "Role"
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}
    ]


def test_render_guard_rejects_cluster_scoped_and_cross_namespace_resources() -> None:
    cluster_scoped = _resources()
    cluster_scoped.append(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "unexpected", "namespace": EXPECTED_NAMESPACE},
        }
    )
    with pytest.raises(ValueError, match="not in the guarded inventory"):
        inspect_rendered_resources(cluster_scoped, namespace=EXPECTED_NAMESPACE, tag=TAG)

    cross_namespace = _resources()
    cast_deployment = next(
        resource
        for resource in cross_namespace
        if resource.get("kind") == "Deployment"
        and resource.get("metadata", {}).get("name") == "django-web"  # type: ignore[union-attr]
    )
    cast_deployment["metadata"]["namespace"] = "default"  # type: ignore[index]
    with pytest.raises(ValueError, match="targets namespace 'default'"):
        inspect_rendered_resources(cross_namespace, namespace=EXPECTED_NAMESPACE, tag=TAG)


def test_unknown_workloads_have_no_pre_setup_apply_phase() -> None:
    resources = _resources()
    unexpected = _deployment("unexpected-workload", image="busybox:1.36")
    resources.append(unexpected)

    with pytest.raises(ValueError, match="not in the guarded inventory"):
        inspect_rendered_resources(resources, namespace=EXPECTED_NAMESPACE, tag=TAG)
    with pytest.raises(ValueError, match="has no guarded apply phase"):
        split_apply_resources(resources)  # type: ignore[arg-type]


def test_render_guard_rejects_floating_application_images() -> None:
    resources = _resources()
    deployment = next(
        resource
        for resource in resources
        if resource.get("kind") == "Deployment"
        and resource.get("metadata", {}).get("name") == "django-web"  # type: ignore[union-attr]
    )
    deployment["spec"] = {
        "template": {"spec": {"containers": [_container("django-web", "django-ray:latest")]}}
    }

    with pytest.raises(ValueError, match="floating tag"):
        inspect_rendered_resources(resources, namespace=EXPECTED_NAMESPACE, tag=TAG)


def test_setup_job_is_separated_and_live_secret_is_preserved() -> None:
    resources = _resources()

    prerequisites, setup, workloads = split_apply_resources(resources)  # type: ignore[arg-type]

    assert setup["metadata"]["name"] == SETUP_JOB
    assert all(resource.get("kind") != "Job" for resource in prerequisites)
    assert not any(resource.get("kind") == "Secret" for resource in prerequisites)
    assert {resource["metadata"]["name"] for resource in workloads} == {
        *APP_DEPLOYMENTS,
        RAY_CLUSTER_NAME,
    }
    assert not any(
        resource.get("kind") in {"Deployment", "RayCluster"}
        and resource.get("metadata", {}).get("name") in {*APP_DEPLOYMENTS, RAY_CLUSTER_NAME}
        for resource in prerequisites
    )


def test_setup_log_requires_migrations_static_and_runtime_env_markers() -> None:
    complete = "\n".join(
        (
            "Running migrations...",
            "Collecting static files...",
            "Building shared RuntimeEnv source bundle...",
            "RuntimeEnv bundle ready: /runtime-env/django-ray-source.zip (42 bytes)",
            "Django setup complete!",
        )
    )
    inspect_setup_log(complete)

    with pytest.raises(ValueError, match="RuntimeEnv bundle ready"):
        inspect_setup_log(
            "Running migrations...\nCollecting static files...\nDjango setup complete!"
        )


def test_setup_job_fails_closed_before_reporting_completion() -> None:
    resources = list(
        yaml.safe_load_all((ROOT / "k8s/base/django-setup-job.yaml").read_text(encoding="utf-8"))
    )
    job = next(resource for resource in resources if resource.get("kind") == "Job")
    script = job["spec"]["template"]["spec"]["containers"][0]["args"][0]

    assert script.lstrip().startswith("set -euo pipefail")
    assert script.index("set -euo pipefail") < script.index("Running migrations")


def test_setup_failure_prevents_workload_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(gate, "_preflight", lambda: order.append("preflight"))
    monkeypatch.setattr(gate, "_build_images", lambda: order.append("images"))
    monkeypatch.setattr(gate, "_apply_overlay", lambda: order.append("apply"))

    def fail_setup() -> None:
        order.append("setup")
        raise ValueError("migration failed")

    monkeypatch.setattr(gate, "_run_setup", fail_setup)
    monkeypatch.setattr(gate, "_apply_workloads", lambda: order.append("workloads"))

    with pytest.raises(GateError, match="migration failed") as captured:
        gate.run()

    assert captured.value.layer == "setup"
    assert order == ["preflight", "images", "apply", "setup"]


def test_setup_delete_and_completion_wait_are_timeout_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    gate.setup_pod_images = pod_image_contract(cast(dict[str, Any], _setup_pod()["spec"]))
    calls: list[tuple[str, ...]] = []
    setup_log = "\n".join(
        (
            "Running migrations...",
            "Collecting static files...",
            "Building shared RuntimeEnv source bundle...",
            "RuntimeEnv bundle ready:",
            "Django setup complete!",
        )
    )

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[0] == "logs":
            return CommandResult(setup_log, "", 0)
        if args[:2] == ("get", "job"):
            return CommandResult(
                json.dumps(
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {
                            "name": SETUP_JOB,
                            "namespace": EXPECTED_NAMESPACE,
                            "uid": "setup-owner",
                        },
                    }
                ),
                "",
                0,
            )
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": [_setup_pod()]}), "", 0)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(gate, "_verify_pod_image_ids", lambda *args, **kwargs: 1)

    gate._run_setup()

    assert calls[0][-2:] == ("--wait=true", "--timeout=300s")
    wait = next(call for call in calls if call[0] == "wait")
    assert wait[-1] == "--timeout=300s"


@pytest.mark.parametrize(
    ("pod", "message"),
    [
        (_setup_pod(owner_uid="stale-job"), "not controlled by Job/django-setup"),
        (_setup_pod(image="django-ray:stale"), "image contract does not match"),
        (_setup_pod(image_id=f"sha256:{'c' * 64}"), "does not run the locally built image ID"),
        (
            _setup_pod(
                name="source-proof-sidecar",
                extra_containers=[{"name": "django-setup", "image": "attacker:latest"}],
            ),
            "image contract does not match",
        ),
    ],
)
def test_setup_identity_rejects_stale_owner_tag_or_image_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pod: dict[str, object],
    message: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    gate.setup_pod_images = pod_image_contract(cast(dict[str, Any], _setup_pod()["spec"]))
    setup_log = "\n".join(
        (
            "Running migrations...",
            "Collecting static files...",
            "Building shared RuntimeEnv source bundle...",
            "RuntimeEnv bundle ready:",
            "Django setup complete!",
        )
    )

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[0] == "logs":
            return CommandResult(setup_log, "", 0)
        if args[:2] == ("get", "job"):
            return CommandResult(
                json.dumps(
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {
                            "name": SETUP_JOB,
                            "namespace": EXPECTED_NAMESPACE,
                            "uid": "setup-owner",
                        },
                    }
                ),
                "",
                0,
            )
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": [pod]}), "", 0)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match=message):
        gate._run_setup()


def test_docker_inspect_binds_tag_id_revision_and_source_tree() -> None:
    payload = json.dumps(
        [
            {
                "Id": IMAGE_ID,
                "RepoTags": [APP_TAG],
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.revision": COMMIT,
                        "org.opencontainers.image.source-tree": SOURCE_TREE,
                    }
                },
            }
        ]
    )

    assert (
        parse_docker_image_inspect(
            payload,
            expected_tag=APP_TAG,
            commit=COMMIT,
            source_tree=SOURCE_TREE,
        )
        == IMAGE_ID
    )

    wrong_revision = payload.replace(COMMIT, "c" * 40)
    with pytest.raises(ValueError, match="revision label"):
        parse_docker_image_inspect(
            wrong_revision,
            expected_tag=APP_TAG,
            commit=COMMIT,
            source_tree=SOURCE_TREE,
        )

    wrong_tree = payload.replace(SOURCE_TREE, "d" * 40)
    with pytest.raises(ValueError, match="source-tree label"):
        parse_docker_image_inspect(
            wrong_tree,
            expected_tag=APP_TAG,
            commit=COMMIT,
            source_tree=SOURCE_TREE,
        )


@pytest.mark.parametrize(
    "runtime_id",
    [IMAGE_ID, f"docker://{IMAGE_ID}", f"containerd://{IMAGE_ID}", f"repo@{IMAGE_ID}"],
)
def test_runtime_image_id_normalization(runtime_id: str) -> None:
    assert normalize_runtime_image_id(runtime_id) == IMAGE_ID


def test_pod_image_identity_matches_source_tag_and_build_digest() -> None:
    pod = {
        "metadata": {"name": "worker", "namespace": EXPECTED_NAMESPACE, "uid": "worker-uid"},
        "spec": {"containers": [{"name": "worker", "image": APP_TAG}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "worker",
                    "image": APP_TAG,
                    "imageID": f"containerd://{IMAGE_ID}",
                }
            ]
        },
    }
    gate = LocalKubeRayGate(_config())

    assert gate._verify_pod_image_ids(pod, expected_tag=APP_TAG, expected_id=IMAGE_ID) == 1

    wrong_pod = json.loads(json.dumps(pod))
    wrong_pod["status"]["containerStatuses"][0]["imageID"] = f"containerd://sha256:{'c' * 64}"
    with pytest.raises(ValueError, match="does not run the locally built image ID"):
        gate._verify_pod_image_ids(wrong_pod, expected_tag=APP_TAG, expected_id=IMAGE_ID)

    substituted = {
        "metadata": {
            "name": "substituted",
            "namespace": EXPECTED_NAMESPACE,
            "uid": "substituted-uid",
        },
        "spec": {
            "containers": [
                {"name": "attacker-main", "image": "attacker:latest"},
                {"name": "source-proof-sidecar", "image": APP_TAG},
            ]
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "attacker-main",
                    "image": "attacker:latest",
                    "imageID": f"containerd://sha256:{'d' * 64}",
                },
                {
                    "name": "source-proof-sidecar",
                    "image": APP_TAG,
                    "imageID": f"containerd://{IMAGE_ID}",
                },
            ]
        },
    }
    expected_contract = PodImageContract((), (("django-ray-worker", APP_TAG),))
    with pytest.raises(ValueError, match="image contract does not match"):
        gate._verify_pod_image_ids(
            substituted,
            expected_tag=APP_TAG,
            expected_id=IMAGE_ID,
            expected_contract=expected_contract,
        )


def test_each_application_deployment_must_retain_rendered_positive_replicas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    for name in APP_DEPLOYMENTS:
        contract = PodImageContract((), ((name, APP_TAG),))
        gate.deployment_contracts[name] = DeploymentContract(1, contract, (("app", name),))

    def deployment(name: str) -> dict[str, object]:
        replicas = 0 if name == "django-ray-worker-sync" else 1
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": EXPECTED_NAMESPACE,
                "uid": f"deployment-{name}",
                "generation": 1,
            },
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [{"name": name, "image": APP_TAG}]},
                },
            },
            "status": {
                "observedGeneration": 1,
                "replicas": replicas,
                "updatedReplicas": replicas,
                "readyReplicas": replicas,
                "availableReplicas": replicas,
            },
        }

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployment(args[2])), "", 0)
        raise AssertionError("replica drift must fail before pod inventory")

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="replicas changed from rendered 1 to live 0"):
        gate._verify_deployed_images()


def test_application_inventory_binds_pods_through_exact_replicaset_and_deployment_uids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selectors = {
        "django-web": {"app": "django-ray", "component": "web"},
        "django-ray-worker": {"app": "django-ray", "component": "worker"},
        "django-ray-worker-sync": {
            "app": "django-ray",
            "component": "worker",
            "queues": "sync",
        },
        "django-ray-worker-ml": {
            "app": "django-ray",
            "component": "worker",
            "queues": "ml",
        },
    }
    gate = LocalKubeRayGate(_config())
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    deployments = {
        name: _live_application_deployment(name, labels=selectors[name]) for name in APP_DEPLOYMENTS
    }
    replicasets = [
        _application_replicaset(name, labels=selectors[name]) for name in APP_DEPLOYMENTS
    ]
    pods = [_application_pod(name, labels=selectors[name]) for name in APP_DEPLOYMENTS]
    for name in APP_DEPLOYMENTS:
        gate.deployment_contracts[name] = DeploymentContract(
            replicas=1,
            pod_images=PodImageContract((), ((name, APP_TAG),)),
            selector=tuple(sorted(selectors[name].items())),
        )

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": pods}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._verify_deployed_images()

    assert gate.evidence.deployments == dict.fromkeys(APP_DEPLOYMENTS, 1)

    attacker = _application_pod("django-web", labels=selectors["django-web"])
    attacker_metadata = cast(dict[str, Any], attacker["metadata"])
    attacker_metadata["name"] = "selector-attacker"
    attacker_metadata["uid"] = "selector-attacker-uid"
    attacker_metadata["ownerReferences"] = [
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "name": "attacker",
            "uid": "attacker-owner",
            "controller": True,
        }
    ]
    pods.append(attacker)

    with pytest.raises(
        ValueError, match="not controlled through an inventoried guarded ReplicaSet"
    ):
        gate._verify_deployed_images()


def test_application_convergence_waits_for_old_terminating_pod_to_disappear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, selectors = _application_inventory_fixture()
    old_replicaset, old_pod = _old_terminating_application_generation(
        "django-ray-worker",
        labels=selectors["django-ray-worker"],
    )
    replicasets.append(old_replicaset)
    pod_batches = iter([pods + [old_pod], pods])
    pod_inventory_calls = 0
    sleeps: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        nonlocal pod_inventory_calls
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            pod_inventory_calls += 1
            return CommandResult(json.dumps({"items": next(pod_batches)}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    gate._wait_for_application_topology()

    assert pod_inventory_calls == 2
    assert sleeps == [2]
    assert gate.evidence.deployments == dict.fromkeys(APP_DEPLOYMENTS, 1)


def test_application_convergence_polls_exact_deployment_status_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, _ = _application_inventory_fixture()
    web_status = cast(dict[str, Any], deployments["django-web"]["status"])
    web_status["readyReplicas"] = 0
    pod_inventory_calls = 0
    sleeps: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        nonlocal pod_inventory_calls
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            pod_inventory_calls += 1
            web_status["readyReplicas"] = 1
            return CommandResult(json.dumps({"items": pods}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    gate._wait_for_application_topology()

    assert pod_inventory_calls == 2
    assert sleeps == [2]
    assert gate.evidence.deployments == dict.fromkeys(APP_DEPLOYMENTS, 1)


def test_application_convergence_times_out_bounded_when_old_pod_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, selectors = _application_inventory_fixture(
        rollout_timeout=1
    )
    old_replicaset, old_pod = _old_terminating_application_generation(
        "django-ray-worker",
        labels=selectors["django-ray-worker"],
    )
    replicasets.append(old_replicaset)
    now = [0.0]
    sleeps: list[float] = []
    pod_inventory_calls = 0

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        nonlocal pod_inventory_calls
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            pod_inventory_calls += 1
            now[0] = 1.1
            return CommandResult(json.dumps({"items": pods + [old_pod]}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr("scripts.local_kuberay_gate.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    with pytest.raises(ValueError, match=r"did not converge within 1s.*old ReplicaSet") as error:
        gate._wait_for_application_topology()

    assert len(str(error.value)) <= MAX_OUTPUT_CHARACTERS
    assert pod_inventory_calls == 1
    assert sleeps == []
    assert gate.evidence.deployments == {}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("substituted", "image contract"),
        ("replicaset-substituted", "current ReplicaSet.*image contract"),
        ("unowned", "not controlled through an inventoried guarded ReplicaSet"),
        ("hidden", "hidden from its exact owning ReplicaSet"),
    ],
)
def test_application_convergence_fails_closed_without_retrying_structural_pods(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    gate, deployments, replicasets, pods, _ = _application_inventory_fixture()
    target = pods[0]
    metadata = cast(dict[str, Any], target["metadata"])
    if mutation == "substituted":
        spec = cast(dict[str, Any], target["spec"])
        containers = cast(list[dict[str, Any]], spec["containers"])
        containers[0]["image"] = "attacker.invalid/substitute:latest"
    elif mutation == "replicaset-substituted":
        replicaset_spec = cast(dict[str, Any], replicasets[0]["spec"])
        template = cast(dict[str, Any], replicaset_spec["template"])
        template_spec = cast(dict[str, Any], template["spec"])
        containers = cast(list[dict[str, Any]], template_spec["containers"])
        containers[0]["image"] = "attacker.invalid/substitute:latest"
    elif mutation == "unowned":
        metadata["ownerReferences"] = [
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "name": "attacker",
                "uid": "attacker-owner",
                "controller": True,
            }
        ]
    else:
        metadata["labels"] = {"hidden": "true"}
    pod_inventory_calls = 0
    sleeps: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        nonlocal pod_inventory_calls
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            pod_inventory_calls += 1
            return CommandResult(json.dumps({"items": pods}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    with pytest.raises(ValueError, match=message):
        gate._wait_for_application_topology()

    assert pod_inventory_calls == 1
    assert sleeps == []
    assert gate.evidence.deployments == {}


def test_strict_application_identity_rejects_old_terminating_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, selectors = _application_inventory_fixture()
    old_replicaset, old_pod = _old_terminating_application_generation(
        "django-ray-worker",
        labels=selectors["django-ray-worker"],
    )
    replicasets.append(old_replicaset)

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": pods + [old_pod]}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="old ReplicaSet.*still owns Pod"):
        gate._verify_deployed_images()

    assert gate.evidence.deployments == {}


def test_application_convergence_rejects_substituted_old_terminating_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, selectors = _application_inventory_fixture()
    old_replicaset, old_pod = _old_terminating_application_generation(
        "django-ray-worker",
        labels=selectors["django-ray-worker"],
    )
    old_spec = cast(dict[str, Any], old_pod["spec"])
    old_containers = cast(list[dict[str, Any]], old_spec["containers"])
    old_containers.append({"name": "substituted-sidecar", "image": "busybox:1.36"})
    replicasets.append(old_replicaset)
    sleeps: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": pods + [old_pod]}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    with pytest.raises(ValueError, match="image contract.*owning ReplicaSet"):
        gate._wait_for_application_topology()

    assert sleeps == []
    assert gate.evidence.deployments == {}


def test_application_inventory_rejects_stale_replicaset_deployment_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = {name: {"app": name} for name in APP_DEPLOYMENTS}
    gate = LocalKubeRayGate(_config())
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    deployments = {
        name: _live_application_deployment(name, labels=labels[name]) for name in APP_DEPLOYMENTS
    }
    replicasets = [_application_replicaset(name) for name in APP_DEPLOYMENTS]
    stale_metadata = cast(dict[str, Any], replicasets[0]["metadata"])
    stale_owner = cast(list[dict[str, Any]], stale_metadata["ownerReferences"])
    stale_owner[0]["uid"] = "stale-deployment-uid"
    pods = [_application_pod(name, labels=labels[name]) for name in APP_DEPLOYMENTS]
    for name in APP_DEPLOYMENTS:
        gate.deployment_contracts[name] = DeploymentContract(
            1,
            PodImageContract((), ((name, APP_TAG),)),
            tuple(sorted(labels[name].items())),
        )

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        return CommandResult(json.dumps({"items": pods}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by Deployment"):
        gate._verify_deployed_images()


def test_application_inventory_rejects_hidden_replicaset_with_pinned_deployment_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, _ = _application_inventory_fixture()
    hidden_replicaset = _application_replicaset("django-web")
    metadata = cast(dict[str, Any], hidden_replicaset["metadata"])
    metadata["name"] = "hidden-replicaset"
    metadata["uid"] = "hidden-replicaset-uid"
    owner = cast(list[dict[str, Any]], metadata["ownerReferences"])[0]
    owner["name"] = "altered-name-with-live-deployment-uid"
    replicasets.append(hidden_replicaset)

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        return CommandResult(json.dumps({"items": pods}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by Deployment/django-web"):
        gate._verify_deployed_images()


def test_application_inventory_rejects_hidden_pod_claiming_pinned_replicaset_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, _ = _application_inventory_fixture()
    hidden_replicaset = _application_replicaset(
        "django-web",
        revision="2",
        replicas=0,
    )
    replicaset_metadata = cast(dict[str, Any], hidden_replicaset["metadata"])
    replicaset_metadata["name"] = "hidden-replicaset"
    replicaset_metadata["uid"] = "hidden-replicaset-uid"
    replicasets.append(hidden_replicaset)
    hidden_pod = _application_pod("django-web", labels={"hidden": "true"})
    pod_metadata = cast(dict[str, Any], hidden_pod["metadata"])
    pod_metadata["name"] = "hidden-pod"
    pod_metadata["uid"] = "hidden-pod-uid"
    pod_metadata["ownerReferences"] = [
        {
            "apiVersion": "apps/v1",
            "kind": "ReplicaSet",
            "name": "altered-name-with-live-replicaset-uid",
            "uid": "hidden-replicaset-uid",
            "controller": False,
        },
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "name": "unrelated-controller",
            "uid": "unrelated-controller-uid",
            "controller": True,
        },
    ]
    pods.append(hidden_pod)

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        return CommandResult(json.dumps({"items": pods}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by ReplicaSet/hidden-replicaset"):
        gate._verify_deployed_images()


def _live_probe_deployment(*, host: str = EXPECTED_PROBE_HOST) -> dict[str, object]:
    probe = {
        "httpGet": {
            "path": EXPECTED_PROBE_PATH,
            "port": 8000,
            "httpHeaders": [{"name": "Host", "value": host}],
        }
    }
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "django-web",
                            "readinessProbe": probe,
                            "livenessProbe": probe,
                        }
                    ]
                }
            }
        }
    }


def test_probe_contract_matches_live_allowlist() -> None:
    config_map = {"data": {"DJANGO_ALLOWED_HOSTS": f"localhost,{EXPECTED_PROBE_HOST}"}}

    assert inspect_probe_contract(_live_probe_deployment(), config_map) == EXPECTED_PROBE_HOST

    with pytest.raises(ValueError, match="absent from DJANGO_ALLOWED_HOSTS"):
        inspect_probe_contract(
            _live_probe_deployment(), {"data": {"DJANGO_ALLOWED_HOSTS": "localhost"}}
        )


def test_runtime_archive_probe_requires_generic_ray_and_fixed_bootstrap_member() -> None:
    digest = "d" * 64
    payload = json.dumps(
        {
            "django_ray": "absent",
            "bytes": 293_956,
            "sha256": digest,
            "required_member": True,
        }
    )
    assert parse_runtime_archive_probe(payload) == (293_956, digest)

    installed = payload.replace('"absent"', '"present"')
    with pytest.raises(ValueError, match="unexpectedly has django_ray"):
        parse_runtime_archive_probe(installed)

    missing = json.loads(payload)
    missing["required_member"] = False
    with pytest.raises(ValueError, match=re.escape(RUNTIME_ENV_REQUIRED_MEMBER)):
        parse_runtime_archive_probe(json.dumps(missing))


def test_durable_task_result_must_be_json_and_equal_value_is_preserved() -> None:
    assert parse_task_result("5") == 5
    with pytest.raises(ValueError, match="valid JSON"):
        parse_task_result("not-json")


def test_cold_ray_restart_deletes_only_verified_pod_names(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    old = [
        _ray_pod("ray-head-old", "head", "old-head"),
        _ray_pod("ray-worker-old", "worker", "old-worker"),
    ]
    new = [
        _ray_pod("ray-head-new", "head", "new-head"),
        _ray_pod("ray-worker-new", "worker", "new-worker"),
    ]
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        gate,
        "_ray_pods",
        lambda **kwargs: ("cluster-owner", old),
    )
    monkeypatch.setattr(gate, "_wait_for_ray", lambda **kwargs: new)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: calls.append(args) or CommandResult("", "", 0),
    )

    gate._prepare_ray()

    assert calls == [
        (
            "delete",
            "pod",
            "ray-head-old",
            "ray-worker-old",
            "--wait=true",
            "--timeout=300s",
        )
    ]
    assert gate.evidence.ray_restart == "performed"
    assert gate.evidence.ray_head_count == 1
    assert gate.evidence.ray_worker_count == 1
    assert gate._ray_pod_identities is not None and len(gate._ray_pod_identities) == 2
    assert re.fullmatch(r"[0-9a-f]{64}", gate.evidence.ray_pod_identity_sha256)


def test_effective_kuberay_worker_contract_accepts_the_injected_wait_gcs_init() -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    worker = _ray_pod("ray-worker", "worker", "worker-uid")

    identities = gate._ray_runtime_identities([worker])

    assert len(identities) == 1
    identity = next(iter(identities))
    assert [container.name for container in identity.init_containers] == [
        gate_module.KUBERAY_WAIT_GCS_INIT
    ]
    assert [container.name for container in identity.containers] == ["ray-worker"]


@pytest.mark.parametrize("mutation", ["extra", "substituted", "script"])
def test_effective_kuberay_worker_contract_rejects_extra_or_substituted_init(
    mutation: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    worker = _ray_pod("ray-worker", "worker", "worker-uid")
    spec = cast(dict[str, Any], worker["spec"])
    init_containers = cast(list[dict[str, Any]], spec["initContainers"])
    status = cast(dict[str, Any], worker["status"])
    init_statuses = cast(list[dict[str, Any]], status["initContainerStatuses"])
    if mutation == "extra":
        init_containers.append(
            {
                "name": "foreign-init",
                "image": "busybox:1.36",
                "command": ["true"],
                "args": [],
            }
        )
        init_statuses.append(
            {
                "name": "foreign-init",
                "image": "busybox:1.36",
                "imageID": f"containerd://sha256:{'d' * 64}",
                "restartCount": 0,
                "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
            }
        )
    elif mutation == "substituted":
        init_containers[0]["name"] = "substituted-init"
        init_statuses[0]["name"] = "substituted-init"
    else:
        init_containers[0]["args"] = ["ray health-check --address attacker.invalid:6379"]

    with pytest.raises(ValueError, match="KubeRay|wait-gcs-ready|image contract"):
        gate._ray_runtime_identities([worker])


@pytest.mark.parametrize("mismatch", ["missing", "name", "image", "termination"])
def test_effective_kuberay_worker_contract_rejects_spec_status_mismatch(
    mismatch: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    worker = _ray_pod("ray-worker", "worker", "worker-uid")
    status = cast(dict[str, Any], worker["status"])
    init_statuses = cast(list[dict[str, Any]], status["initContainerStatuses"])
    if mismatch == "missing":
        init_statuses.clear()
    elif mismatch == "name":
        init_statuses[0]["name"] = "foreign-init"
    elif mismatch == "image":
        init_statuses[0]["image"] = "rayproject/ray:2.55.0-py312"
    else:
        init_statuses[0]["state"] = {"terminated": {"exitCode": 1, "reason": "Error"}}

    with pytest.raises(ValueError, match="init|image|terminate"):
        gate._ray_runtime_identities([worker])


def test_required_restart_discovers_owned_old_images_then_requires_new_effective_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    old_pods = [
        _ray_pod(
            "ray-head-old",
            "head",
            "old-head-uid",
            image="rayproject/ray:2.55.0-py312",
        ),
        _ray_pod(
            "ray-worker-old",
            "worker",
            "old-worker-uid",
            image="rayproject/ray:2.55.0-py312",
        ),
    ]
    new_pods = [
        _ray_pod("ray-head-new", "head", "new-head-uid"),
        _ray_pod("ray-worker-new", "worker", "new-worker-uid"),
    ]
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": old_pods}), "", 0)
        if args[:2] == ("delete", "pod"):
            return CommandResult("", "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(gate, "_wait_for_ray", lambda **kwargs: new_pods)

    cluster_uid, discovered = gate._ray_pods(
        allow_empty=True,
        contract_phase="restart-discovery",
    )
    assert cluster_uid == "cluster-owner"
    assert discovered == old_pods
    with pytest.raises(ValueError, match="effective KubeRay"):
        gate._ray_pods(allow_empty=True)

    gate._prepare_ray()

    deletion = next(call for call in calls if call[:2] == ("delete", "pod"))
    assert deletion[2:4] == ("ray-head-old", "ray-worker-old")
    assert gate.evidence.ray_restart == "performed"
    assert gate._ray_pod_identities is not None
    assert {identity.uid for identity in gate._ray_pod_identities} == {
        "new-head-uid",
        "new-worker-uid",
    }


def test_restart_discovery_accepts_bounded_owned_scale_down_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate, workers=1)
    old_pods = [
        _ray_pod(
            "ray-head-old",
            "head",
            "old-head-uid",
            image="rayproject/ray:2.55.0-py312",
        ),
        *[
            _ray_pod(
                f"ray-worker-old-{index}",
                "worker",
                f"old-worker-uid-{index}",
                image="rayproject/ray:2.55.0-py312",
            )
            for index in range(3)
        ],
    ]

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": old_pods}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    cluster_uid, discovered = gate._ray_pods(
        allow_empty=True,
        contract_phase="restart-discovery",
    )

    assert cluster_uid == "cluster-owner"
    assert discovered == old_pods


def test_final_ray_identity_rejects_uid_or_runtime_image_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    gate._ray_cluster_uid = "cluster-owner"
    original = [
        _ray_pod("ray-head", "head", "head-original"),
        _ray_pod("ray-worker", "worker", "worker-original"),
    ]
    gate._ray_pod_identities = gate._ray_runtime_identities(original)
    gate.evidence.ray_pod_identity_sha256 = gate_module.pod_identity_sha256(
        tuple(gate._ray_pod_identities)
    )
    churned = [
        _ray_pod("ray-head", "head", "head-replaced"),
        _ray_pod("ray-worker", "worker", "worker-replaced"),
    ]
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: ("cluster-owner", churned))

    with pytest.raises(ValueError, match="UID/container/image identity changed"):
        gate._verify_ray_identity()

    image_churn = json.loads(json.dumps(original))
    image_churn[1]["status"]["containerStatuses"][0]["imageID"] = f"containerd://sha256:{'d' * 64}"
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: ("cluster-owner", image_churn))

    with pytest.raises(ValueError, match="UID/container/image identity changed"):
        gate._verify_ray_identity()


def test_ray_pod_inventory_matches_the_rendered_component_image_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod = _ray_pod("ray-worker", "worker", "worker-uid")
    pod_spec = cast(dict[str, Any], pod["spec"])
    containers = cast(list[dict[str, Any]], pod_spec["containers"])
    containers[0]["image"] = "attacker:latest"
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="effective KubeRay"):
        gate._ray_pods()


def test_ray_pod_inventory_rejects_noncontrolling_hidden_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod = _ray_pod("ray-hidden", "worker", "hidden-uid")
    metadata = cast(dict[str, Any], pod["metadata"])
    labels = cast(dict[str, Any], metadata["labels"])
    labels.pop(RAY_CLUSTER_LABEL)
    owners = cast(list[dict[str, Any]], metadata["ownerReferences"])
    owners[0]["controller"] = False
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by RayCluster/ray"):
        gate._ray_pods()


def test_ray_pod_inventory_rejects_hidden_pod_with_pinned_cluster_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod = _ray_pod("ray-hidden", "worker", "hidden-uid")
    metadata = cast(dict[str, Any], pod["metadata"])
    labels = cast(dict[str, Any], metadata["labels"])
    labels.pop(RAY_CLUSTER_LABEL)
    owner = cast(list[dict[str, Any]], metadata["ownerReferences"])[0]
    owner["name"] = "altered-name-with-live-cluster-uid"
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by RayCluster/ray"):
        gate._ray_pods()


def test_ray_pod_discovery_requires_the_live_cluster_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod = _ray_pod("ray-head", "head", "head-uid")
    calls: list[tuple[str, ...]] = []
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    assert gate._ray_pods() == ("cluster-owner", [pod])
    assert calls[0] == ("get", "raycluster", RAY_CLUSTER_NAME, "-o", "json")


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("cluster", "another-cluster", "missing the exact cluster label"),
        ("owner", "another-cluster", "not controlled by RayCluster/ray"),
        ("owner_uid", "stale-owner", "not controlled by RayCluster/ray"),
    ],
)
def test_ray_pod_discovery_rejects_non_owned_pods(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: str,
    message: str,
) -> None:
    pod = _ray_pod("ray-head", "head", "head-uid")
    metadata = cast(dict[str, Any], pod["metadata"])
    if field == "cluster":
        labels = cast(dict[str, Any], metadata["labels"])
        labels[RAY_CLUSTER_LABEL] = replacement
    elif field == "owner":
        owners = cast(list[dict[str, Any]], metadata["ownerReferences"])
        owners[0]["name"] = replacement
    else:
        owners = cast(list[dict[str, Any]], metadata["ownerReferences"])
        owners[0]["uid"] = replacement
    calls: list[tuple[str, ...]] = []
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match=message):
        gate._ray_pods()

    assert calls == [
        ("get", "raycluster", RAY_CLUSTER_NAME, "-o", "json"),
        (
            "get",
            "pods",
            "-o",
            "json",
        ),
    ]


def test_explicit_ray_skip_never_deletes_a_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config(ray_restart="skip"))
    _set_ray_topology(gate)
    pods = [_ray_pod("ray-head", "head", "head"), _ray_pod("ray-worker", "worker", "worker")]
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        gate,
        "_ray_pods",
        lambda **kwargs: ("cluster-owner", []),
    )
    monkeypatch.setattr(gate, "_wait_for_ray", lambda **kwargs: pods)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: calls.append(args) or CommandResult("", "", 0),
    )

    gate._prepare_ray()

    assert calls == []
    assert gate.evidence.ray_restart == "skipped-by-explicit-trigger-choice"


def test_ray_wait_handles_the_controller_creation_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    calls: list[tuple[str, ...]] = []
    head = _ray_pod("ray-head", "head", "head")
    worker = _ray_pod("ray-worker", "worker", "worker")
    batches = iter(
        [
            ("cluster-owner", []),
            ("cluster-owner", [head]),
            ("cluster-owner", [head, worker]),
            ("cluster-owner", [head, worker]),
        ]
    )
    monkeypatch.setattr("scripts.local_kuberay_gate.time.sleep", lambda seconds: None)
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: next(batches))

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    assert gate._wait_for_ray(cluster_uid="cluster-owner") == [head, worker]

    assert [call[0] for call in calls] == ["wait", "wait"]


def test_ray_wait_rejects_underprovisioned_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate, workers=4)
    pods = [_ray_pod("ray-head", "head", "head"), _ray_pod("ray-worker", "worker", "worker")]
    clock = iter([0.0, 301.0])
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: ("cluster-owner", pods))
    monkeypatch.setattr("scripts.local_kuberay_gate.time.monotonic", lambda: next(clock))

    with pytest.raises(ValueError, match=r"did not reach exact topology.*worker:worker-group.*4"):
        gate._wait_for_ray(cluster_uid="cluster-owner")


def test_ray_pod_discovery_rejects_cluster_uid_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="new-owner")), "", 0)
        return CommandResult(json.dumps({"items": []}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="UID changed from old-owner to new-owner"):
        gate._ray_pods(expected_cluster_uid="old-owner", allow_empty=True)


def test_task_manager_restart_is_exact_and_web_is_only_waited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: calls.append(args) or CommandResult("", "", 0),
    )

    gate._restart_task_managers()

    assert calls[0] == (
        "rollout",
        "restart",
        *(f"deployment/{name}" for name in TASK_MANAGER_DEPLOYMENTS),
    )
    assert all("postgres" not in call and "pvc" not in call for call in calls)
    assert {call[2] for call in calls[1:]} == {f"deployment/{name}" for name in APP_DEPLOYMENTS}


def test_every_kubectl_command_carries_pinned_routing_and_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.args: list[tuple[str, ...]] = []
            self.timeouts: list[float | None] = []
            self.environments: list[dict[str, str] | None] = []

        def run(
            self,
            args: list[str] | tuple[str, ...],
            *,
            cwd: Path,
            input_text: str | None = None,
            check: bool = True,
            timeout: float | None = None,
            sensitive_output: bool = False,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            self.args.append(tuple(args))
            self.timeouts.append(timeout)
            self.environments.append(env)
            return CommandResult("{}", "", 0)

    runner = RecordingRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]
    payload = json.dumps(_kubeconfig_payload()).encode()
    gate.kubeconfig_path = ROOT / "tests/unit/fixtures/nonexistent-kubeconfig.json"
    gate._kubeconfig_digest = hashlib.sha256(payload).hexdigest()
    gate._kubernetes_server = "https://kubernetes.docker.internal:6443"

    # Keep this command-construction test independent of filesystem snapshot validation.
    gate._verify_kubeconfig_snapshot = lambda: None  # type: ignore[method-assign]
    for key in gate_module.KUBECTL_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, f"hostile-{key.lower()}")
    monkeypatch.setenv("DJANGO_RAY_ENV_MARKER", "preserved")

    gate._kubectl("get", "pods")
    gate._kubectl_cluster("get", "customresourcedefinitions")

    assert runner.args == [
        (
            "kubectl",
            "--kubeconfig",
            str(gate.kubeconfig_path),
            "--context",
            "docker-desktop",
            "--request-timeout=30s",
            "--namespace",
            EXPECTED_NAMESPACE,
            "get",
            "pods",
        ),
        (
            "kubectl",
            "--kubeconfig",
            str(gate.kubeconfig_path),
            "--context",
            "docker-desktop",
            "--request-timeout=30s",
            "get",
            "customresourcedefinitions",
        ),
    ]
    assert runner.timeouts == [120, 120]
    for environment in runner.environments:
        assert environment is not None
        assert not ({key.upper() for key in environment} & gate_module.KUBECTL_ENVIRONMENT_KEYS)
        assert environment["DJANGO_RAY_ENV_MARKER"] == "preserved"


def test_ambient_context_changes_cannot_redirect_pinned_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.args: list[tuple[str, ...]] = []
            self.environments: list[dict[str, str] | None] = []

        def run(self, args: list[str], **kwargs: object) -> CommandResult:
            self.args.append(tuple(args))
            self.environments.append(cast(dict[str, str] | None, kwargs.get("env")))
            return CommandResult("{}", "", 0)

    snapshot = json.dumps(_kubeconfig_payload()).encode()
    snapshot_path = tmp_path / "kubeconfig.json"
    snapshot_path.write_bytes(snapshot)
    runner = RecordingRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]
    gate.kubeconfig_path = snapshot_path
    gate._kubeconfig_digest = hashlib.sha256(snapshot).hexdigest()
    gate._kubernetes_server = "https://kubernetes.docker.internal:6443"
    gate._docker_host = "npipe:////./pipe/dockerDesktopLinuxEngine"
    monkeypatch.setenv("DOCKER_HOST", "tcp://production.example.invalid:2376")
    monkeypatch.setenv("DOCKER_CONTEXT", "production")
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "BUILDX_BUILDER",
        "BUILDKIT_HOST",
    ):
        monkeypatch.setenv(key, f"hostile-{key.lower()}")

    gate._kubectl("get", "pods")
    gate._docker("info")

    assert runner.args[0][:5] == (
        "kubectl",
        "--kubeconfig",
        str(snapshot_path),
        "--context",
        "docker-desktop",
    )
    assert runner.args[1] == (
        "docker",
        "--host",
        "npipe:////./pipe/dockerDesktopLinuxEngine",
        "info",
    )
    assert runner.environments[0] is not None
    assert not (
        {key.upper() for key in runner.environments[0]} & gate_module.KUBECTL_ENVIRONMENT_KEYS
    )
    assert runner.environments[1] is not None
    assert not (
        {key.upper() for key in runner.environments[1]} & gate_module.DOCKER_ENVIRONMENT_KEYS
    )

    snapshot_path.write_text(json.dumps(_kubeconfig_payload(context="production")))
    with pytest.raises(ValueError, match="snapshot changed"):
        gate._kubectl("get", "pods")


def test_kind_load_uses_only_the_pinned_docker_endpoint_and_sanitized_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

        def run(self, args: list[str], **kwargs: object) -> CommandResult:
            self.calls.append((tuple(args), cast(dict[str, str] | None, kwargs.get("env"))))
            return CommandResult("", "", 0)

    runner = RecordingRunner()
    gate = LocalKubeRayGate(
        _config(context="kind-local", kind_cluster_name="local"),
        runner=runner,  # type: ignore[arg-type]
    )
    gate.source_context = tmp_path
    gate._docker_host = "unix:///var/run/docker.sock"
    gate.evidence.commit = COMMIT
    gate.evidence.source_tree = SOURCE_TREE
    gate.evidence.app_tag = APP_TAG
    gate.evidence.worker_tag = f"django-ray-worker:{TAG}"
    monkeypatch.setattr(gate, "_verify_source_identity", lambda: None)
    for key in gate_module.KIND_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, f"hostile-{key.lower()}")

    def docker(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] != ("image", "inspect"):
            return CommandResult("", "", 0)
        tag = args[2]
        return CommandResult(
            json.dumps(
                [
                    {
                        "Id": IMAGE_ID,
                        "RepoTags": [tag],
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": COMMIT,
                                "org.opencontainers.image.source-tree": SOURCE_TREE,
                            }
                        },
                    }
                ]
            ),
            "",
            0,
        )

    monkeypatch.setattr(gate, "_docker", docker)

    gate._build_images()

    kind_args, environment = runner.calls[-1]
    assert kind_args[:3] == ("kind", "load", "docker-image")
    assert environment is not None
    assert environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert environment["KIND_EXPERIMENTAL_PROVIDER"] == "docker"
    assert not (
        ({key.upper() for key in environment} - {"DOCKER_HOST", "KIND_EXPERIMENTAL_PROVIDER"})
        & gate_module.KIND_ENVIRONMENT_KEYS
    )


def test_token_redactor_covers_diagnostics_and_evidence_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = TOKEN68
    encoded = base64.b64encode(token.encode()).decode()
    representations = _token_representations(token)
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            json.dumps({"data": {"DJANGO_API_TOKEN": encoded}}), "", 0
        ),
    )
    assert gate._secret_token() == token
    gate.mutated = True
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            "\n".join(f"line {index} {' | '.join(representations)}" for index in range(200)),
            "",
            0,
        ),
    )

    gate.diagnostics("api-smoke")

    serialized = "\n".join(output)
    assert all(representation not in serialized for representation in representations)
    assert token[:24] not in serialized
    assert "[REDACTED]" in serialized
    assert len(serialized.splitlines()) <= 1 + 3 * 80


def test_diagnostics_redact_before_bounding_output(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "0123456789abcdefghijKLMNOPQRSTuvwxyzABCD"
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    gate.redactor.register(token)
    gate.mutated = True
    diagnostic = ("p" * 100) + token + ("z" * (MAX_OUTPUT_CHARACTERS - 20))
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(diagnostic, "", 0),
    )

    gate.diagnostics("setup")

    serialized = "\n".join(output)
    assert token[20:] not in serialized
    assert "[REDACTED]" in serialized


def test_every_evidence_field_passes_through_the_token_redactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "evidence-token-that-must-never-be-printed-123456"
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    gate.redactor.register(token)
    monkeypatch.setattr(gate, "_verify_final_identity", lambda: None)
    evidence = gate.evidence
    for field_name in (
        "commit",
        "source_tree",
        "kubeconfig_sha256",
        "kubernetes_server",
        "docker_host",
        "app_tag",
        "worker_tag",
        "app_image_id",
        "worker_image_id",
        "setup_bundle_sha256",
        "ray_restart",
        "ray_cluster_uid",
        "ray_pod_identity_sha256",
        "task_id",
        "task_state",
        "workflow_task_id",
        "workflow_task_state",
        "workflow_availability",
        "workflow_failure_task_id",
        "workflow_failure_task_state",
        "workflow_failure_availability",
        "workflow_terminal_only_task_id",
        "workflow_terminal_only_task_state",
        "workflow_terminal_only_failure_task_id",
        "workflow_terminal_only_failure_task_state",
        "workflow_showcase_task_id",
        "workflow_showcase_task_state",
        "workflow_showcase_failure_task_id",
        "workflow_showcase_failure_task_state",
    ):
        setattr(evidence, field_name, token)
    evidence.setup_bundle_bytes = cast(Any, token)
    evidence.ray_head_count = cast(Any, token)
    evidence.ray_worker_count = cast(Any, token)
    evidence.web_restart_count = cast(Any, token)
    evidence.task_result = token
    evidence.api_execution_delete_rejected = cast(Any, token)
    evidence.api_legacy_workflow_graph_absent = cast(Any, token)
    evidence.runtime_env_encryption_overlay = cast(Any, token)
    evidence.runtime_env_encryption_canary = cast(Any, token)
    evidence.runtime_env_encryption_envelope = cast(Any, token)
    evidence.runtime_env_encryption_marker_absent = cast(Any, token)
    evidence.runtime_env_encryption_tamper_rejected = cast(Any, token)
    evidence.runtime_env_encryption_unknown_key_rejected = cast(Any, token)
    evidence.runtime_env_encryption_retry_preserved = cast(Any, token)
    evidence.runtime_env_encryption_logs_clear = cast(Any, token)
    evidence.django_ray_secret_preserved = cast(Any, token)
    evidence.workflow_schema_version = cast(Any, token)
    evidence.workflow_attempt_number = cast(Any, token)
    evidence.workflow_topology_nodes = cast(Any, token)
    evidence.workflow_topology_edges = cast(Any, token)
    evidence.workflow_node_details = cast(Any, token)
    evidence.workflow_leaf_tasks = cast(Any, token)
    evidence.workflow_admin_routes = cast(Any, token)
    evidence.workflow_admin_actions = cast(Any, token)
    evidence.workflow_current_manifests = cast(Any, token)
    evidence.workflow_pending_manifests = cast(Any, token)
    evidence.workflow_unlinked_pages = cast(Any, token)
    evidence.workflow_failure_attempt_number = cast(Any, token)
    evidence.workflow_failure_schema_version = cast(Any, token)
    evidence.workflow_failure_topology_nodes = cast(Any, token)
    evidence.workflow_failure_topology_edges = cast(Any, token)
    evidence.workflow_failure_node_details = cast(Any, token)
    evidence.workflow_failure_leaf_tasks = cast(Any, token)
    evidence.workflow_failure_pending_nodes = cast(Any, token)
    evidence.workflow_failure_running_nodes = cast(Any, token)
    evidence.workflow_failure_succeeded_nodes = cast(Any, token)
    evidence.workflow_failure_failed_nodes = cast(Any, token)
    evidence.workflow_failure_path_nodes = cast(Any, token)
    evidence.workflow_failure_origins = cast(Any, token)
    evidence.workflow_failure_incoming_edges = cast(Any, token)
    evidence.workflow_failure_admin_routes = cast(Any, token)
    evidence.workflow_failure_admin_actions = cast(Any, token)
    evidence.workflow_failure_current_manifests = cast(Any, token)
    evidence.workflow_failure_pending_manifests = cast(Any, token)
    evidence.workflow_failure_unlinked_pages = cast(Any, token)
    evidence.workflow_terminal_only_attempt_number = cast(Any, token)
    evidence.workflow_terminal_only_schema_version = cast(Any, token)
    evidence.workflow_terminal_only_summary_revision = cast(Any, token)
    evidence.workflow_terminal_only_declared_nodes = cast(Any, token)
    evidence.workflow_terminal_only_declared_edges = cast(Any, token)
    evidence.workflow_terminal_only_admin_actions = cast(Any, token)
    evidence.workflow_terminal_only_graph_advertised = cast(Any, token)
    evidence.workflow_terminal_only_storage_rows = cast(Any, token)
    evidence.workflow_terminal_only_failure_attempt_number = cast(Any, token)
    evidence.workflow_terminal_only_failure_schema_version = cast(Any, token)
    evidence.workflow_terminal_only_failure_summary_revision = cast(Any, token)
    evidence.workflow_terminal_only_failure_declared_nodes = cast(Any, token)
    evidence.workflow_terminal_only_failure_declared_edges = cast(Any, token)
    evidence.workflow_terminal_only_failure_admin_actions = cast(Any, token)
    evidence.workflow_terminal_only_failure_graph_advertised = cast(Any, token)
    evidence.workflow_terminal_only_failure_storage_rows = cast(Any, token)
    evidence.workflow_showcase_attempt_number = cast(Any, token)
    evidence.workflow_showcase_topology_nodes = cast(Any, token)
    evidence.workflow_showcase_topology_edges = cast(Any, token)
    evidence.workflow_showcase_longest_path_layers = cast(Any, token)
    evidence.workflow_showcase_detail_links = cast(Any, token)
    evidence.workflow_showcase_failure_attempt_number = cast(Any, token)
    evidence.workflow_showcase_failure_failed_nodes = cast(Any, token)
    evidence.workflow_showcase_failure_pending_descendants = cast(Any, token)
    evidence.workflow_showcase_failure_running_nodes = cast(Any, token)
    evidence.workflow_showcase_failure_succeeded_nodes = cast(Any, token)
    evidence.workflow_showcase_failure_path_nodes = cast(Any, token)
    evidence.workflow_showcase_failure_detail_links = cast(Any, token)
    evidence.deployments = cast(dict[str, int], dict.fromkeys(APP_DEPLOYMENTS, token))
    evidence.prometheus_counts = cast(
        dict[str, int], dict.fromkeys(("django-ray", "ray-head", "ray-workers"), token)
    )

    gate._emit_evidence()

    serialized = "\n".join(output)
    assert token not in serialized
    assert serialized.count("[REDACTED]") >= 75


def test_secret_token_is_decoded_in_memory_and_registered_for_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = TOKEN68
    payload = json.dumps({"data": {"DJANGO_API_TOKEN": base64.b64encode(token.encode()).decode()}})
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(payload, "", 0),
    )

    assert gate._secret_token() == token
    assert gate.redactor.clean(f"Bearer {token}") == "Bearer [REDACTED]"
    assert gate.redactor.clean(payload) == '{"data": {"DJANGO_API_TOKEN": "[REDACTED]"}}'
    assert all(
        gate.redactor.clean(variant) == "[REDACTED]"
        for variant in _percent_hex_case_variants(quote(token, safe=""))
    )


def test_secret_token_decode_failure_has_no_private_exception_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_bytes = b"\xffprivate-token-bytes-that-must-not-enter-the-exception-graph"
    encoded = base64.b64encode(private_bytes).decode()
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate,
        "_secret_data",
        lambda: {"DJANGO_API_TOKEN": encoded},
    )

    with pytest.raises(ValueError, match="base64-encoded UTF-8") as captured:
        gate._secret_token()

    assert encoded not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secret_preservation_compares_the_complete_data_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = base64.b64encode(TOKEN68.encode()).decode()
    baseline = {
        "DJANGO_API_TOKEN": encoded,
        "DJANGO_SECRET_KEY": base64.b64encode(b"django-secret").decode(),
        "DATABASE_PASSWORD": base64.b64encode(b"database-secret").decode(),
    }
    gate = LocalKubeRayGate(_config())
    responses = iter((baseline, dict(reversed(tuple(baseline.items())))))
    monkeypatch.setattr(gate, "_secret_data", lambda: next(responses))

    assert gate._secret_token() == TOKEN68
    gate._verify_preserved_secret()

    assert gate.evidence.django_ray_secret_preserved is True
    assert gate._secret_data_sha256 == secret_data_sha256(baseline)


def test_secret_preservation_rejects_any_data_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = base64.b64encode(TOKEN68.encode()).decode()
    baseline = {
        "DJANGO_API_TOKEN": encoded,
        "DJANGO_SECRET_KEY": base64.b64encode(b"django-secret").decode(),
    }
    changed = {
        **baseline,
        "DJANGO_SECRET_KEY": base64.b64encode(b"changed-secret").decode(),
    }
    gate = LocalKubeRayGate(_config())
    responses = iter((baseline, changed))
    monkeypatch.setattr(gate, "_secret_data", lambda: next(responses))

    gate._secret_token()
    with pytest.raises(ValueError, match="data changed"):
        gate._verify_preserved_secret()

    assert gate.evidence.django_ray_secret_preserved is False


@pytest.mark.parametrize(
    "token",
    [
        "local-token-that-must-never-be-printed-123456",
        "A" * 32,
        "A" * 31 + "=",
        "A" * 30 + "==",
        "A" * 510 + "==",
        "A" * 511 + "=",
        "A" * 512,
        TOKEN68,
    ],
)
def test_secret_token_accepts_strict_token68_values(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    encoded = base64.b64encode(token.encode()).decode()
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            json.dumps({"data": {"DJANGO_API_TOKEN": encoded}}), "", 0
        ),
    )

    assert gate._secret_token() == token


@pytest.mark.parametrize(
    "token",
    [
        "A" * 31,
        "A" * 32 + "\n",
        "A" * 32 + "\t",
        "A" * 32 + " ",
        "A" * 31 + '"',
        "A" * 31 + "\\",
        "A" * 16 + "=" + "A" * 16,
        "A" * 32 + "===",
        "A" * 32 + "€",
        "A" * 512 + "=",
        "A" * 511 + "==",
        "A" * 513,
    ],
)
def test_secret_token_rejects_unbounded_or_non_header_safe_values(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    encoded = base64.b64encode(token.encode()).decode()
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        assert kwargs["sensitive_output"] is True
        return CommandResult(json.dumps({"data": {"DJANGO_API_TOKEN": encoded}}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="Bearer token68 alphabet"):
        gate._secret_token()


def test_prometheus_layer_bounds_and_redacts_a_large_last_error() -> None:
    token = TOKEN68
    encoded = base64.b64encode(token.encode()).decode()
    representations = _token_representations(token)
    gate = LocalKubeRayGate(_config(), output=lambda value: None)
    gate.redactor.register(token)
    gate.redactor.register(encoded)
    last_error = f"{'x' * 20_000}|{'|'.join(representations)}"
    payload = {
        "status": "success",
        "data": {
            "activeTargets": [
                {
                    "labels": {"instance": "django-ray:80", "job": "django-ray"},
                    "health": "down",
                    "lastError": last_error,
                },
                {
                    "labels": {"instance": "ray-head:8080", "job": "ray-head"},
                    "health": "up",
                    "lastError": "",
                },
                {
                    "labels": {"instance": "ray-worker:8080", "job": "ray-workers"},
                    "health": "up",
                    "lastError": "",
                },
            ]
        },
    }

    def fail_with_prometheus_error() -> None:
        gate_module.wait_for_healthy_targets(
            lambda: payload,
            timeout=0,
            interval=1,
        )

    with pytest.raises(GateError) as captured:
        gate._layer("prometheus", fail_with_prometheus_error)

    detail = str(captured.value)
    assert captured.value.layer == "prometheus"
    assert len(detail) <= MAX_OUTPUT_CHARACTERS
    assert detail.startswith("[truncated redacted error; original_characters=")
    assert all(representation not in detail for representation in representations)
    assert token[:24] not in detail
    assert "[REDACTED]" in detail


def test_preflight_registers_secret_before_any_mutation_or_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class PreflightRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.sensitive_commands: list[tuple[str, ...]] = []
            self.routing_environments: list[tuple[str, dict[str, str] | None]] = []

        def run(
            self,
            args: list[str] | tuple[str, ...],
            *,
            cwd: Path,
            input_text: str | None = None,
            check: bool = True,
            timeout: float | None = None,
            sensitive_output: bool = False,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            command = tuple(args)
            if sensitive_output:
                self.sensitive_commands.append(command)
            if command[0] in {"docker", "kubectl"}:
                self.routing_environments.append((command[0], env))
            if command[:3] == ("git", "rev-parse", "--show-toplevel"):
                return CommandResult(str(ROOT), "", 0)
            if command[:2] == ("git", "status"):
                return CommandResult("", "", 0)
            if command[:3] == ("git", "rev-parse", "--verify"):
                return CommandResult(SOURCE_TREE if "tree" in command[3] else COMMIT, "", 0)
            if command == ("kubectl", "config", "current-context"):
                return CommandResult("docker-desktop", "", 0)
            if "config" in command and "view" in command:
                return CommandResult(json.dumps(_kubeconfig_payload()), "", 0)
            if command[:3] == ("docker", "context", "show"):
                return CommandResult("desktop-linux", "", 0)
            if command[:3] == ("docker", "context", "inspect"):
                return CommandResult(
                    json.dumps(
                        [
                            {
                                "Endpoints": {
                                    "docker": {"Host": "npipe:////./pipe/dockerDesktopLinuxEngine"}
                                }
                            }
                        ]
                    ),
                    "",
                    0,
                )
            if "info" in command and command[0] == "docker":
                return CommandResult('"28.0.0"', "", 0)
            if "customresourcedefinition" in command:
                return CommandResult("customresourcedefinition/rayclusters.ray.io", "", 0)
            if "clusterrole" in command:
                return CommandResult("clusterrole/prometheus-django-ray", "", 0)
            raise AssertionError(f"unexpected preflight command: {command}")

    runner = PreflightRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]
    gate.temp_root = tmp_path
    events: list[str] = []
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    for key in gate_module.PROXY_ENVIRONMENT_KEYS | {
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "BUILDX_BUILDER",
        "KUBECONFIG",
    }:
        monkeypatch.setenv(key, f"hostile-{key.lower()}")
    monkeypatch.setattr(shutil, "which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(
        gate,
        "_secret_token",
        lambda: events.append("secret-registered") or "local-token",
    )

    with pytest.raises(ValueError, match="legacy cluster-scoped"):
        gate._preflight()

    assert events == ["secret-registered"]
    assert gate.mutated is False
    assert len(runner.sensitive_commands) == 1
    assert "config" in runner.sensitive_commands[0]
    assert gate.kubeconfig_path is not None and gate.kubeconfig_path.is_file()
    assert (
        gate.evidence.kubeconfig_sha256
        == hashlib.sha256(gate.kubeconfig_path.read_bytes()).hexdigest()
    )
    assert runner.redactor.clean("kube-token") == "[REDACTED]"
    for executable, environment in runner.routing_environments:
        assert environment is not None
        removed = (
            gate_module.DOCKER_ENVIRONMENT_KEYS
            if executable == "docker"
            else gate_module.KUBECTL_ENVIRONMENT_KEYS
        )
        assert not ({key.upper() for key in environment} & removed)


def test_api_smoke_requires_401_200_and_durable_five(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        authenticated = headers == {"Authorization": f"Bearer {token}"}
        calls.append((path, method, authenticated))
        if path == "/api/openapi.json":
            assert response_limit > MAX_OUTPUT_CHARACTERS
            return 200, json.dumps(
                {
                    "paths": {
                        "/api/executions/{execution_id}": {"get": {"operationId": "get_execution"}}
                    }
                }
            ).encode()
        if not authenticated:
            return 401, b"{}"
        if path == "/api/enqueue/add/2/3":
            return 200, json.dumps({"task_id": TASK_ID}).encode()
        if path == f"/api/executions?task_id={TASK_ID}&limit=1":
            return 200, json.dumps(
                {
                    "tasks": [
                        {
                            "id": 17,
                            "task_id": TASK_ID,
                            "state": "SUCCEEDED",
                            "result_data": "5",
                        }
                    ]
                }
            ).encode()
        if path == "/api/executions/17" and method == "DELETE":
            return 405, b"{}"
        if path == "/api/executions/17":
            return 200, json.dumps(
                {
                    "id": 17,
                    "task_id": TASK_ID,
                    "state": "SUCCEEDED",
                    "result_data": "5",
                }
            ).encode()
        return 200, b"{}"

    monkeypatch.setattr(gate, "_http", request)

    gate._verify_api()

    assert gate.evidence.task_id == TASK_ID
    assert gate.evidence.task_state == "SUCCEEDED"
    assert gate.evidence.task_result == 5
    assert gate.evidence.api_execution_delete_rejected is True
    assert gate.evidence.api_legacy_workflow_graph_absent is True
    assert calls[:4] == [
        ("/api/enqueue/add/2/3", "POST", False),
        ("/api/executions/stats", "GET", False),
        ("/api/metrics", "GET", False),
        ("/api/executions?limit=1", "GET", False),
    ]
    assert calls[-2:] == [
        ("/api/executions/17", "DELETE", True),
        ("/api/executions/17", "GET", True),
    ]


def test_api_smoke_rejects_an_openapi_execution_delete() -> None:
    gate = LocalKubeRayGate(_config())

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        if path == "/api/openapi.json":
            assert response_limit > MAX_OUTPUT_CHARACTERS
            return 200, json.dumps(
                {
                    "paths": {
                        "/api/executions/{execution_id}": {
                            "get": {},
                            "delete": {},
                        }
                    }
                }
            ).encode()
        return 401, b"{}"

    gate._http = request  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="advertises unsafe DELETE"):
        gate._verify_api()


def test_api_smoke_rejects_the_legacy_workflow_graph_in_openapi() -> None:
    gate = LocalKubeRayGate(_config())

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        if path == "/api/openapi.json":
            assert response_limit > MAX_OUTPUT_CHARACTERS
            return 200, json.dumps(
                {
                    "paths": {
                        "/api/executions/{execution_id}": {"get": {}},
                        "/api/cluster/workflows/{task_id}/graph": {"get": {}},
                    }
                }
            ).encode()
        return 401, b"{}"

    gate._http = request  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="removed legacy workflow graph"):
        gate._verify_api()


def test_api_smoke_rejects_task_id_evidence_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        if path == "/api/openapi.json":
            assert response_limit > MAX_OUTPUT_CHARACTERS
            return 200, json.dumps(
                {"paths": {"/api/executions/{execution_id}": {"get": {}}}}
            ).encode()
        if headers is None:
            return 401, b"{}"
        if path == "/api/enqueue/add/2/3":
            return 200, b'{"task_id":"forged\\npreserved=everything"}'
        return 200, b"{}"

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError, match="canonical UUID"):
        gate._verify_api()


def _runtime_env_failure_observations() -> dict[str, dict[str, object]]:
    common: dict[str, object] = {
        "state": "FAILED",
        "attempt_number": 1,
        "execution_generation": 1,
        "claimed": True,
        "lifecycle_timestamps": True,
        "no_ray_submission": True,
        "no_result": True,
        "attempts": [{"attempt_number": 1, "state": "FAILED"}],
    }
    return {
        "ciphertext": {
            **common,
            "archive_fingerprint": "a" * 64,
            "authentication_failed": True,
            "key_unavailable": False,
        },
        "key_id": {
            **common,
            "archive_fingerprint": "b" * 64,
            "authentication_failed": False,
            "key_unavailable": True,
        },
    }


def test_private_runtime_env_envelope_inspection_registers_every_raw_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized, nonce, ciphertext = _runtime_env_envelope()
    digest = "d" * 64
    gate = LocalKubeRayGate(_config())
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append((args, kwargs))
        return CommandResult(
            json.dumps(
                {
                    "envelope": serialized,
                    "profile": "thin",
                    "runtime_env_hash": digest,
                }
            ),
            "",
            0,
        )

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._inspect_runtime_env_canary_envelope(
        task_id=RUNTIME_ENV_CANARY_TASK_ID,
        profile="thin",
        digest=digest,
    )

    assert calls[0][1]["sensitive_output"] is True
    assert "testproject/manage.py" in calls[0][0]
    no_imports = calls[0][0].index("--no-imports")
    assert calls[0][0][no_imports + 1] == "-c"
    assert gate.evidence.runtime_env_encryption_envelope is True
    assert gate.evidence.runtime_env_encryption_marker_absent is True
    assert gate.redactor.clean(f"{serialized}|{nonce}|{ciphertext}") == (
        "[REDACTED]|[REDACTED]|[REDACTED]"
    )
    assert gate.runner.redactor.clean(serialized) == "[REDACTED]"


def test_failure_fixture_creation_is_one_sensitive_atomic_storage_seam_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    serialized, nonce, ciphertext = _runtime_env_envelope()
    tampered_payload = json.loads(serialized)
    tampered_payload["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    tampered = json.dumps(tampered_payload, sort_keys=True, separators=(",", ":"))
    unknown_payload = json.loads(serialized)
    unknown_payload["key_id"] = "django-ray-gate-unknown"
    unknown = json.dumps(unknown_payload, sort_keys=True, separators=(",", ":"))

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append((args, kwargs))
        return CommandResult(
            json.dumps(
                {
                    "ciphertext": {
                        "id": 11,
                        "task_id": RUNTIME_ENV_TAMPER_TASK_ID,
                        "envelope": tampered,
                        "nonce": nonce,
                        "ciphertext": tampered_payload["ciphertext"],
                    },
                    "key_id": {
                        "id": 12,
                        "task_id": RUNTIME_ENV_UNKNOWN_KEY_TASK_ID,
                        "envelope": unknown,
                        "nonce": nonce,
                        "ciphertext": ciphertext,
                    },
                }
            ),
            "",
            0,
        )

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    assert gate._create_runtime_env_failure_fixtures() == {
        "ciphertext": (11, RUNTIME_ENV_TAMPER_TASK_ID),
        "key_id": (12, RUNTIME_ENV_UNKNOWN_KEY_TASK_ID),
    }

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["sensitive_output"] is True
    no_imports = args.index("--no-imports")
    assert args[no_imports + 1] == "-c"
    script = args[-1]
    assert script == RUNTIME_ENV_FAILURE_FIXTURE_SCRIPT
    assert script.count("with transaction.atomic():") == 1
    assert "runtime_env_for_storage(resolved, task_id=task_id)" in script
    assert script.index('envelope["ciphertext"] =') < script.index(
        "RayTaskExecution.objects.create"
    )
    assert script.index('envelope["key_id"] =') < script.index("RayTaskExecution.objects.create")
    assert gate._runtime_env_fixture_values_registered is True
    for value in (
        tampered,
        tampered_payload["ciphertext"],
        unknown,
        nonce,
        ciphertext,
    ):
        assert gate.redactor.clean(value) == "[REDACTED]"


def test_malformed_private_fixture_payload_suppresses_runtime_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_value = "unregistered-mutated-envelope-that-must-remain-private"
    malformed = f'{{"ciphertext":{{"envelope":"{private_value}"'
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(malformed, "", 0),
    )

    with pytest.raises(ValueError, match="valid private JSON") as captured:
        gate._create_runtime_env_failure_fixtures()

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert private_value not in str(captured.value)
    assert gate._runtime_env_fixture_values_registered is False

    commands: list[tuple[str, ...]] = []

    def diagnostics_kubectl(*args: str, **kwargs: object) -> CommandResult:
        commands.append(args)
        return CommandResult("ordinary resource status", "", 0)

    monkeypatch.setattr(gate, "_kubectl", diagnostics_kubectl)
    gate.mutated = True
    gate.diagnostics("runtime-env-encryption")

    assert commands == [("get", "pods,deployments,jobs,pvc", "-o", "wide")]
    assert private_value not in "\n".join(output)


def test_runtime_env_failure_invariants_require_pre_ray_permanent_failures() -> None:
    observations = _runtime_env_failure_observations()

    LocalKubeRayGate._validate_runtime_env_failure_invariants(observations)

    observations["ciphertext"]["no_ray_submission"] = False
    with pytest.raises(ValueError, match="lifecycle boundary"):
        LocalKubeRayGate._validate_runtime_env_failure_invariants(observations)


def test_runtime_env_encryption_layer_proves_canary_corruption_and_retry_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.resources = _runtime_env_encryption_resources()
    token = "local-token-that-must-never-be-printed-123456"
    digest = "d" * 64
    fixtures = {
        "ciphertext": (11, RUNTIME_ENV_TAMPER_TASK_ID),
        "key_id": (12, RUNTIME_ENV_UNKNOWN_KEY_TASK_ID),
    }
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        authenticated = headers == {"Authorization": f"Bearer {token}"}
        calls.append((path, method, authenticated))
        if path == RUNTIME_ENV_ENCRYPTION_PROBE_PATH:
            return 200, json.dumps({"task_id": RUNTIME_ENV_CANARY_TASK_ID}).encode()
        if path == f"/api/cluster/runtime-env/{RUNTIME_ENV_CANARY_TASK_ID}":
            return 200, json.dumps(
                {
                    "task_id": RUNTIME_ENV_CANARY_TASK_ID,
                    "state": "SUCCEEDED",
                    "runtime_env_profile": "thin",
                    "runtime_env_hash": digest,
                    "result": {"storage_encryption_verified": True},
                    "error": None,
                }
            ).encode()
        for execution_id, task_id in fixtures.values():
            if path == f"/api/executions?task_id={task_id}&limit=1":
                return 200, json.dumps(
                    {
                        "tasks": [
                            {
                                "id": execution_id,
                                "task_id": task_id,
                                "state": "FAILED",
                                "attempt_number": 1,
                                "execution_generation": 1,
                                "result_data": None,
                                "runtime_env_profile": "thin",
                            }
                        ]
                    }
                ).encode()
        if path == "/api/executions/11/retry":
            return 409, b'{"detail":"snapshot integrity failure"}'
        raise AssertionError(path)

    monkeypatch.setattr(gate, "_http", request)

    def inspect_envelope(**kwargs: object) -> None:
        assert kwargs == {
            "task_id": RUNTIME_ENV_CANARY_TASK_ID,
            "profile": "thin",
            "digest": digest,
        }
        gate.evidence.runtime_env_encryption_envelope = True
        gate.evidence.runtime_env_encryption_marker_absent = True

    monkeypatch.setattr(gate, "_inspect_runtime_env_canary_envelope", inspect_envelope)
    monkeypatch.setattr(gate, "_create_runtime_env_failure_fixtures", lambda: fixtures)
    observations = _runtime_env_failure_observations()
    monkeypatch.setattr(gate, "_runtime_env_failure_invariants", lambda _fixtures: observations)
    monkeypatch.setattr(
        gate,
        "_verify_runtime_env_logs_clear",
        lambda: setattr(gate.evidence, "runtime_env_encryption_logs_clear", True),
    )

    gate._verify_runtime_env_encryption()

    assert all(authenticated for _, _, authenticated in calls)
    assert calls[0][:2] == (RUNTIME_ENV_ENCRYPTION_PROBE_PATH, "POST")
    assert calls[-1][:2] == ("/api/executions/11/retry", "POST")
    assert gate.evidence.runtime_env_encryption_overlay is True
    assert gate.evidence.runtime_env_encryption_canary is True
    assert gate.evidence.runtime_env_encryption_envelope is True
    assert gate.evidence.runtime_env_encryption_marker_absent is True
    assert gate.evidence.runtime_env_encryption_tamper_rejected is True
    assert gate.evidence.runtime_env_encryption_unknown_key_rejected is True
    assert gate.evidence.runtime_env_encryption_retry_preserved is True
    assert gate.evidence.runtime_env_encryption_logs_clear is True


def test_runtime_env_log_scan_rejects_protected_values_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    serialized, nonce, ciphertext = _runtime_env_envelope()
    for value in (serialized, nonce, ciphertext):
        gate._register_runtime_env_protected_value(value)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            f"ordinary log\n{RUNTIME_ENV_STORAGE_PROBE_MARKER}\n",
            "",
            0,
        ),
    )

    with pytest.raises(ValueError, match="protected RuntimeEnv storage value") as captured:
        gate._verify_runtime_env_logs_clear()

    assert RUNTIME_ENV_STORAGE_PROBE_MARKER not in str(captured.value)
    assert serialized not in str(captured.value)
    assert gate.evidence.runtime_env_encryption_logs_clear is False


def test_runtime_env_log_scan_reads_the_complete_bounded_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        return CommandResult("ordinary logs", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._verify_runtime_env_logs_clear()

    assert len(calls) == 2
    assert all("--tail=-1" in call for call in calls)
    assert all("--since=15m" in call for call in calls)
    assert all("--limit-bytes=1048576" in call for call in calls)


def _complex_workflow_gate_responses() -> dict[str, dict[str, Any]]:
    page_query = f"?limit={gate_module.WORKFLOW_PROGRESS_PAGE_LIMIT}"

    def run_responses(
        *,
        enqueue_path: str,
        enqueue_kwargs: dict[str, object],
        task_id: str,
        run_id: str,
        state: str,
        node_states: dict[str, str],
        edges: list[dict[str, str]],
        leaf_tasks: int,
    ) -> dict[str, dict[str, Any]]:
        run_identity = {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_number": 1,
            "execution_generation": 1,
        }
        publication = {
            "summary_revision": 9,
            "topology_version": 8,
            "detail_revision": 1,
        }

        def envelope() -> dict[str, Any]:
            return {
                "schema_version": 1,
                "task_id": task_id,
                "run_identity": dict(run_identity),
                "publication": dict(publication),
                "availability": "AVAILABLE",
                "complete": True,
            }

        def page(collection: str, items: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                **envelope(),
                "schema": "django-ray.workflow-progress-page",
                "collection": collection,
                "returned_count": len(items),
                "items": items,
                "next_cursor": None,
            }

        state_counts = {
            node_state: sum(value == node_state for value in node_states.values())
            for node_state in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
        }
        summary = {
            "schema_version": 3,
            "run_identity": dict(run_identity),
            "reporting_policy": "full",
            "selected_strategy": "dynamic_tasks",
            "plan_fingerprint": f"sha256:{'a' * 64}",
            **publication,
            "state": state,
            "node_counts": {
                "declared": None,
                "discovered": len(node_states),
                "retained_topology": len(node_states),
                "retained_detail": len(node_states),
                "pending": state_counts["PENDING"],
                "running": state_counts["RUNNING"],
                "succeeded": state_counts["SUCCEEDED"],
                "failed": state_counts["FAILED"],
            },
            "edge_counts": {
                "declared": None,
                "discovered": len(edges),
                "retained_topology": len(edges),
            },
            "detail": {
                "availability": "AVAILABLE",
                "complete": True,
                "truncation_reasons": [],
            },
        }
        poll_result: dict[str, Any] | None = None
        poll_error: str | None = gate_module.COMPLEX_WORKFLOW_FAILURE_MESSAGE
        if state == "SUCCEEDED":
            poll_result = {
                "shape": "chain(group(chain(map), chain(map)), step)",
                "durability_boundary": "single RayTaskExecution",
                "total_leaf_tasks": leaf_tasks,
            }
            poll_error = None
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        return {
            enqueue_path: {
                "task_id": task_id,
                "status": "READY",
                "args": [],
                "kwargs": dict(enqueue_kwargs),
            },
            f"/api/cluster/complex-workflow/{task_id}": {
                "task_id": task_id,
                "state": state,
                "result": poll_result,
                "error": poll_error,
            },
            f"/api/executions?{execution_query}": {
                "tasks": [
                    {
                        "task_id": task_id,
                        "state": state,
                        "callable_path": (
                            "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"
                        ),
                        "attempt_number": 1,
                        "execution_generation": 1,
                        "workflow_run_id": run_id,
                    }
                ]
            },
            f"/api/cluster/workflows/{task_id}": {
                **envelope(),
                "schema": "django-ray.workflow-progress-summary",
                "source_schema_version": 3,
                "summary": summary,
            },
            f"/api/cluster/workflows/{task_id}/topology/nodes{page_query}": page(
                "topology_nodes",
                [{"node_id": node_id, "kind": "step"} for node_id in node_states],
            ),
            f"/api/cluster/workflows/{task_id}/topology/edges{page_query}": page(
                "topology_edges",
                edges,
            ),
            f"/api/cluster/workflows/{task_id}/nodes{page_query}": page(
                "node_details",
                [
                    {"node_id": node_id, "state": node_state}
                    for node_id, node_state in node_states.items()
                ],
            ),
        }

    def terminal_only_responses(
        *,
        enqueue_path: str,
        enqueue_kwargs: dict[str, object],
        task_id: str,
        run_id: str,
        state: str,
    ) -> dict[str, dict[str, Any]]:
        run_identity = {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_number": 1,
            "execution_generation": 1,
        }
        publication = {
            "summary_revision": 1,
            "topology_version": None,
            "detail_revision": None,
        }

        def envelope() -> dict[str, Any]:
            return {
                "schema_version": 1,
                "task_id": task_id,
                "run_identity": dict(run_identity),
                "publication": dict(publication),
                "availability": "OMITTED_BY_POLICY",
                "complete": False,
            }

        def empty_page(collection: str) -> dict[str, Any]:
            return {
                **envelope(),
                "schema": "django-ray.workflow-progress-page",
                "collection": collection,
                "returned_count": 0,
                "items": [],
                "next_cursor": None,
            }

        finished_at = "2026-07-29T12:00:02Z"
        summary = {
            "schema_version": 3,
            "storage_protocol_version": 1,
            "run_identity": dict(run_identity),
            "reporting_policy": "terminal_only",
            "selected_strategy": "dynamic_tasks",
            "plan_fingerprint": f"sha256:{'a' * 64}",
            "limits_profile": "v1",
            **publication,
            "state": state,
            "node_counts": {
                "declared": 13,
                "discovered": 0,
                "retained_topology": 0,
                "retained_detail": 0,
                "pending": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
            },
            "edge_counts": {
                "declared": 13,
                "discovered": 0,
                "retained_topology": 0,
            },
            "progress_percent": 100.0 if state == "SUCCEEDED" else 0.0,
            "timestamps": {
                "started_at": "2026-07-29T12:00:00Z",
                "updated_at": finished_at,
                "finished_at": finished_at,
            },
            "detail": {
                "availability": "OMITTED_BY_POLICY",
                "complete": False,
                "truncation_reasons": [],
            },
            "storage": {"kind": "database", "manifest_id": None},
            "retention": {"detail_days": 7, "detail_expires_at": None},
            "terminal": {"outcome": state, "finished_at": finished_at},
        }
        poll_result: dict[str, Any] | None = None
        poll_error: str | None = gate_module.COMPLEX_WORKFLOW_FAILURE_MESSAGE
        if state == "SUCCEEDED":
            poll_result = {
                "shape": "chain(group(chain(map), chain(map)), step)",
                "durability_boundary": "single RayTaskExecution",
                "total_leaf_tasks": 3,
            }
            poll_error = None
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        return {
            enqueue_path: {
                "task_id": task_id,
                "status": "READY",
                "args": [],
                "kwargs": dict(enqueue_kwargs),
            },
            f"/api/cluster/complex-workflow/{task_id}": {
                "task_id": task_id,
                "state": state,
                "result": poll_result,
                "error": poll_error,
            },
            f"/api/executions?{execution_query}": {
                "tasks": [
                    {
                        "task_id": task_id,
                        "state": state,
                        "callable_path": (
                            "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"
                        ),
                        "attempt_number": 1,
                        "execution_generation": 1,
                        "workflow_run_id": run_id,
                    }
                ]
            },
            f"/api/cluster/workflows/{task_id}": {
                **envelope(),
                "schema": "django-ray.workflow-progress-summary",
                "source_schema_version": 3,
                "summary": summary,
            },
            f"/api/cluster/workflows/{task_id}/topology/nodes{page_query}": (
                empty_page("topology_nodes")
            ),
            f"/api/cluster/workflows/{task_id}/topology/edges{page_query}": (
                empty_page("topology_edges")
            ),
            f"/api/cluster/workflows/{task_id}/nodes{page_query}": empty_page("node_details"),
        }

    success_nodes = {
        "0.0": "SUCCEEDED",
        "0.1.g0.0": "SUCCEEDED",
        "0.2": "SUCCEEDED",
    }
    failure_nodes = {
        "0.0": "SUCCEEDED",
        "0.1.g0.0": "SUCCEEDED",
        "0.1.g0.1": "FAILED",
        "0.1.g1.0": "RUNNING",
        "0.2": "PENDING",
    }
    responses = run_responses(
        enqueue_path=gate_module.COMPLEX_WORKFLOW_ENQUEUE_PATH,
        enqueue_kwargs=gate_module.COMPLEX_WORKFLOW_ENQUEUE_KWARGS,
        task_id=WORKFLOW_TASK_ID,
        run_id=WORKFLOW_RUN_ID,
        state="SUCCEEDED",
        node_states=success_nodes,
        edges=[
            {"source": "0.0", "target": "0.1.g0.0"},
            {"source": "0.1.g0.0", "target": "0.2"},
        ],
        leaf_tasks=3,
    )
    responses.update(
        run_responses(
            enqueue_path=gate_module.COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH,
            enqueue_kwargs=gate_module.COMPLEX_WORKFLOW_FAILURE_ENQUEUE_KWARGS,
            task_id=FAILED_WORKFLOW_TASK_ID,
            run_id=FAILED_WORKFLOW_RUN_ID,
            state="FAILED",
            node_states=failure_nodes,
            edges=[
                {"source": "0.0", "target": "0.1.g0.0"},
                {"source": "0.0", "target": "0.1.g0.1"},
                {"source": "0.0", "target": "0.1.g1.0"},
                {"source": "0.1.g0.0", "target": "0.2"},
                {"source": "0.1.g0.1", "target": "0.2"},
                {"source": "0.1.g1.0", "target": "0.2"},
            ],
            leaf_tasks=3,
        )
    )
    responses.update(
        terminal_only_responses(
            enqueue_path=gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_PATH,
            enqueue_kwargs=gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_KWARGS,
            task_id=TERMINAL_ONLY_WORKFLOW_TASK_ID,
            run_id=TERMINAL_ONLY_WORKFLOW_RUN_ID,
            state="SUCCEEDED",
        )
    )
    responses.update(
        terminal_only_responses(
            enqueue_path=(gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_PATH),
            enqueue_kwargs=(gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_KWARGS),
            task_id=TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID,
            run_id=TERMINAL_ONLY_FAILED_WORKFLOW_RUN_ID,
            state="FAILED",
        )
    )
    return responses


def test_complex_workflow_gate_requires_terminal_consistent_schema_v3_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _complex_workflow_gate_responses()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert headers == {"Authorization": f"Bearer {token}"}
        calls.append((path, method))
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    gate._verify_complex_workflow_progress()

    def run_calls(enqueue_path: str, task_id: str) -> list[tuple[str, str]]:
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        return [
            (enqueue_path, "POST"),
            (f"/api/cluster/complex-workflow/{task_id}", "GET"),
            (f"/api/executions?{execution_query}", "GET"),
            (f"/api/cluster/workflows/{task_id}", "GET"),
            (
                f"/api/cluster/workflows/{task_id}/topology/nodes?limit=16",
                "GET",
            ),
            (
                f"/api/cluster/workflows/{task_id}/topology/edges?limit=16",
                "GET",
            ),
            (f"/api/cluster/workflows/{task_id}/nodes?limit=16", "GET"),
        ]

    assert calls == run_calls(
        gate_module.COMPLEX_WORKFLOW_ENQUEUE_PATH,
        WORKFLOW_TASK_ID,
    ) + run_calls(
        gate_module.COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH,
        FAILED_WORKFLOW_TASK_ID,
    ) + run_calls(
        gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_PATH,
        TERMINAL_ONLY_WORKFLOW_TASK_ID,
    ) + run_calls(
        gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_PATH,
        TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID,
    )
    assert gate.evidence.workflow_task_id == WORKFLOW_TASK_ID
    assert gate.evidence.workflow_task_state == "SUCCEEDED"
    assert gate.evidence.workflow_attempt_number == 1
    assert gate.evidence.workflow_schema_version == 3
    assert gate.evidence.workflow_availability == "AVAILABLE"
    assert gate.evidence.workflow_topology_nodes == 3
    assert gate.evidence.workflow_topology_edges == 2
    assert gate.evidence.workflow_node_details == 3
    assert gate.evidence.workflow_leaf_tasks == 3
    assert gate.evidence.workflow_failure_task_id == FAILED_WORKFLOW_TASK_ID
    assert gate.evidence.workflow_failure_task_state == "FAILED"
    assert gate.evidence.workflow_failure_attempt_number == 1
    assert gate.evidence.workflow_failure_schema_version == 3
    assert gate.evidence.workflow_failure_availability == "AVAILABLE"
    assert gate.evidence.workflow_failure_topology_nodes == 5
    assert gate.evidence.workflow_failure_topology_edges == 6
    assert gate.evidence.workflow_failure_node_details == 5
    assert gate.evidence.workflow_failure_leaf_tasks == 3
    assert gate.evidence.workflow_failure_pending_nodes == 1
    assert gate.evidence.workflow_failure_running_nodes == 1
    assert gate.evidence.workflow_failure_succeeded_nodes == 2
    assert gate.evidence.workflow_failure_failed_nodes == 1
    assert gate.evidence.workflow_terminal_only_task_id == TERMINAL_ONLY_WORKFLOW_TASK_ID
    assert gate.evidence.workflow_terminal_only_task_state == "SUCCEEDED"
    assert gate.evidence.workflow_terminal_only_attempt_number == 1
    assert gate.evidence.workflow_terminal_only_schema_version == 3
    assert gate.evidence.workflow_terminal_only_summary_revision == 1
    assert gate.evidence.workflow_terminal_only_declared_nodes == 13
    assert gate.evidence.workflow_terminal_only_declared_edges == 13
    assert (
        gate.evidence.workflow_terminal_only_failure_task_id
        == TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    )
    assert gate.evidence.workflow_terminal_only_failure_task_state == "FAILED"
    assert gate.evidence.workflow_terminal_only_failure_attempt_number == 1
    assert gate.evidence.workflow_terminal_only_failure_schema_version == 3
    assert gate.evidence.workflow_terminal_only_failure_summary_revision == 1
    assert gate.evidence.workflow_terminal_only_failure_declared_nodes == 13
    assert gate.evidence.workflow_terminal_only_failure_declared_edges == 13


@pytest.mark.parametrize(
    "failure",
    [
        "terminal_failure",
        "legacy_summary",
        "not_available",
        "empty_nodes",
        "unknown_edge_node",
        "missing_detail",
        "publication_mismatch",
        "count_mismatch",
        "failed_error",
        "failed_retry_attempt",
        "missing_failed_enqueue_kwargs",
        "forged_failed_enqueue_kwargs",
    ],
)
def test_complex_workflow_gate_rejects_incomplete_or_inconsistent_api_evidence(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _complex_workflow_gate_responses()
    summary_path = f"/api/cluster/workflows/{WORKFLOW_TASK_ID}"
    page_query = f"?limit={gate_module.WORKFLOW_PROGRESS_PAGE_LIMIT}"
    node_path = f"{summary_path}/topology/nodes{page_query}"
    edge_path = f"{summary_path}/topology/edges{page_query}"
    detail_path = f"{summary_path}/nodes{page_query}"
    poll_path = f"/api/cluster/complex-workflow/{WORKFLOW_TASK_ID}"
    failed_poll_path = f"/api/cluster/complex-workflow/{FAILED_WORKFLOW_TASK_ID}"
    failed_execution_path = (
        f"/api/executions?{urlencode({'task_id': FAILED_WORKFLOW_TASK_ID, 'limit': 1})}"
    )
    failed_enqueue_path = gate_module.COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH

    if failure == "terminal_failure":
        responses[poll_path]["state"] = "FAILED"
        responses[poll_path]["result"] = None
    elif failure == "legacy_summary":
        responses[summary_path]["source_schema_version"] = 2
    elif failure == "not_available":
        responses[summary_path]["availability"] = "NOT_REPORTED"
        responses[summary_path]["complete"] = False
    elif failure == "empty_nodes":
        responses[node_path]["items"] = []
        responses[node_path]["returned_count"] = 0
    elif failure == "unknown_edge_node":
        responses[edge_path]["items"][0]["target"] = "missing-node"
    elif failure == "missing_detail":
        responses[detail_path]["items"].pop()
        responses[detail_path]["returned_count"] -= 1
    elif failure == "publication_mismatch":
        responses[edge_path]["publication"]["summary_revision"] += 1
    elif failure == "count_mismatch":
        responses[summary_path]["summary"]["node_counts"]["discovered"] -= 1
    elif failure == "failed_error":
        responses[failed_poll_path]["error"] = "unexpected failure"
    elif failure == "failed_retry_attempt":
        responses[failed_execution_path]["tasks"][0]["attempt_number"] = 2
    elif failure == "missing_failed_enqueue_kwargs":
        responses[failed_enqueue_path].pop("kwargs")
    elif failure == "forged_failed_enqueue_kwargs":
        responses[failed_enqueue_path]["kwargs"]["slow_items"] = 2

    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert method in {"GET", "POST"}
        assert headers == {"Authorization": f"Bearer {token}"}
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError):
        gate._verify_complex_workflow_progress()

    assert gate.evidence.workflow_task_id == ""
    assert gate.evidence.workflow_topology_nodes == 0


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_policy",
        "extra_summary_revision",
        "topology_revision",
        "discovered_nodes",
        "retained_page",
        "declared_count_mismatch",
        "failed_retry_attempt",
        "forged_enqueue_policy",
    ],
)
def test_terminal_only_workflow_gate_rejects_detail_or_inconsistent_summary(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _complex_workflow_gate_responses()
    task_id = TERMINAL_ONLY_WORKFLOW_TASK_ID
    summary_path = f"/api/cluster/workflows/{task_id}"
    page_query = f"?limit={gate_module.WORKFLOW_PROGRESS_PAGE_LIMIT}"
    node_path = f"{summary_path}/topology/nodes{page_query}"
    execution_path = f"/api/executions?{urlencode({'task_id': task_id, 'limit': 1})}"
    enqueue_path = gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_PATH

    if failure == "wrong_policy":
        responses[summary_path]["summary"]["reporting_policy"] = "full"
    elif failure == "extra_summary_revision":
        responses[summary_path]["summary"]["summary_revision"] = 2
        responses[summary_path]["publication"]["summary_revision"] = 2
    elif failure == "topology_revision":
        responses[summary_path]["summary"]["topology_version"] = 1
        responses[summary_path]["publication"]["topology_version"] = 1
    elif failure == "discovered_nodes":
        responses[summary_path]["summary"]["node_counts"]["discovered"] = 1
    elif failure == "retained_page":
        responses[node_path]["items"] = [{"node_id": "0.0"}]
        responses[node_path]["returned_count"] = 1
    elif failure == "declared_count_mismatch":
        responses[summary_path]["summary"]["node_counts"]["declared"] = 4
    elif failure == "failed_retry_attempt":
        failed_id = TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
        failed_execution_path = f"/api/executions?{urlencode({'task_id': failed_id, 'limit': 1})}"
        responses[failed_execution_path]["tasks"][0]["attempt_number"] = 2
    elif failure == "forged_enqueue_policy":
        responses[enqueue_path]["kwargs"]["reporting_policy"] = "full"

    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert method in {"GET", "POST"}
        assert headers == {"Authorization": f"Bearer {token}"}
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError):
        gate._verify_complex_workflow_progress()

    assert responses[execution_path]["tasks"][0]["attempt_number"] == 1
    assert gate.evidence.workflow_terminal_only_task_id == ""


def _workflow_showcase_gate_responses() -> dict[str, dict[str, Any]]:
    page_query = f"?limit={gate_module.WORKFLOW_SHOWCASE_PAGE_LIMIT}"
    node_ids = sorted(set().union(*gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS))
    edges = [
        {"source": source, "target": target}
        for source, target in sorted(gate_module.WORKFLOW_SHOWCASE_EDGES)
    ]

    def run_responses(
        *,
        enqueue_path: str,
        enqueue_kwargs: dict[str, object],
        task_id: str,
        run_id: str,
        state: str,
        node_states: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        run_identity = {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_number": 1,
            "execution_generation": 1,
        }
        publication = {
            "summary_revision": 9,
            "topology_version": 8,
            "detail_revision": 7,
        }

        def envelope() -> dict[str, Any]:
            return {
                "schema_version": 1,
                "task_id": task_id,
                "run_identity": dict(run_identity),
                "publication": dict(publication),
                "availability": "AVAILABLE",
                "complete": True,
            }

        def page(collection: str, items: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                **envelope(),
                "schema": "django-ray.workflow-progress-page",
                "collection": collection,
                "returned_count": len(items),
                "items": items,
                "next_cursor": None,
            }

        node_details = [
            {
                "node_id": node_id,
                "state": node_states[node_id],
                "label": node_id,
            }
            for node_id in node_ids
        ]
        state_counts = {
            node_state: sum(value == node_state for value in node_states.values())
            for node_state in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
        }
        summary = {
            "schema_version": 3,
            "run_identity": dict(run_identity),
            "reporting_policy": "full",
            "selected_strategy": "dynamic_tasks",
            "plan_fingerprint": f"sha256:{'a' * 64}",
            **publication,
            "state": state,
            "node_counts": {
                "declared": 31,
                "discovered": len(node_ids),
                "retained_topology": len(node_ids),
                "retained_detail": len(node_ids),
                "pending": state_counts["PENDING"],
                "running": state_counts["RUNNING"],
                "succeeded": state_counts["SUCCEEDED"],
                "failed": state_counts["FAILED"],
            },
            "edge_counts": {
                "declared": 38,
                "discovered": len(edges),
                "retained_topology": len(edges),
            },
            "detail": {
                "availability": "AVAILABLE",
                "complete": True,
                "truncation_reasons": [],
            },
        }
        poll_result: dict[str, Any] | None = None
        poll_error: str | None = gate_module.WORKFLOW_SHOWCASE_FAILURE_MESSAGE
        if state == "SUCCEEDED":
            poll_result = json.loads(json.dumps(gate_module.WORKFLOW_SHOWCASE_SUCCESS_RESULT))
            poll_error = None
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        responses = {
            enqueue_path: {
                "task_id": task_id,
                "status": "READY",
                "args": [],
                "kwargs": dict(enqueue_kwargs),
            },
            f"/api/cluster/workflow-showcase/{task_id}": {
                "task_id": task_id,
                "state": state,
                "result": poll_result,
                "error": poll_error,
            },
            f"/api/executions?{execution_query}": {
                "tasks": [
                    {
                        "task_id": task_id,
                        "state": state,
                        "callable_path": gate_module.WORKFLOW_SHOWCASE_CALLABLE,
                        "attempt_number": 1,
                        "execution_generation": 1,
                        "workflow_run_id": run_id,
                    }
                ]
            },
            f"/api/cluster/workflows/{task_id}": {
                **envelope(),
                "schema": "django-ray.workflow-progress-summary",
                "source_schema_version": 3,
                "summary": summary,
            },
            f"/api/cluster/workflows/{task_id}/topology/nodes{page_query}": page(
                "topology_nodes",
                [{"node_id": node_id, "kind": "step"} for node_id in node_ids],
            ),
            f"/api/cluster/workflows/{task_id}/topology/edges{page_query}": page(
                "topology_edges",
                edges,
            ),
            f"/api/cluster/workflows/{task_id}/nodes{page_query}": page(
                "node_details",
                node_details,
            ),
        }
        for detail in node_details:
            query = urlencode(
                {
                    "node_id": detail["node_id"],
                    "attempt_number": 1,
                }
            )
            responses[f"/api/cluster/workflows/{task_id}/node-detail?{query}"] = {
                **envelope(),
                "schema": "django-ray.workflow-progress-node",
                "found": True,
                "item": dict(detail),
            }
        return responses

    success_states = dict.fromkeys(node_ids, "SUCCEEDED")
    failure_states = dict.fromkeys(node_ids, "SUCCEEDED")
    failure_states[gate_module.WORKFLOW_SHOWCASE_FAILURE_NODE_ID] = "FAILED"
    for node_id in gate_module.WORKFLOW_SHOWCASE_FAILURE_DESCENDANTS:
        failure_states[node_id] = "PENDING"

    responses = run_responses(
        enqueue_path=gate_module.WORKFLOW_SHOWCASE_ENQUEUE_PATH,
        enqueue_kwargs=gate_module.WORKFLOW_SHOWCASE_ENQUEUE_KWARGS,
        task_id=WORKFLOW_SHOWCASE_TASK_ID,
        run_id=WORKFLOW_SHOWCASE_RUN_ID,
        state="SUCCEEDED",
        node_states=success_states,
    )
    responses.update(
        run_responses(
            enqueue_path=gate_module.WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_PATH,
            enqueue_kwargs=gate_module.WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_KWARGS,
            task_id=FAILED_WORKFLOW_SHOWCASE_TASK_ID,
            run_id=FAILED_WORKFLOW_SHOWCASE_RUN_ID,
            state="FAILED",
            node_states=failure_states,
        )
    )
    return responses


def test_workflow_showcase_gate_requires_layered_success_and_isolated_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _workflow_showcase_gate_responses()
    indexed_paths: set[str] = set()
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert headers == {"Authorization": f"Bearer {token}"}
        assert method in {"GET", "POST"}
        if "/node-detail?" in path:
            indexed_paths.add(path)
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    gate._verify_workflow_showcase_progress()

    assert len(set().union(*gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS)) == 21
    assert len(gate_module.WORKFLOW_SHOWCASE_EDGES) == 28
    assert len(gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS) == 12
    assert len(indexed_paths) == 42
    assert gate.evidence.workflow_showcase_task_id == WORKFLOW_SHOWCASE_TASK_ID
    assert gate.evidence.workflow_showcase_task_state == "SUCCEEDED"
    assert gate.evidence.workflow_showcase_attempt_number == 1
    assert gate.evidence.workflow_showcase_topology_nodes == 21
    assert gate.evidence.workflow_showcase_topology_edges == 28
    assert gate.evidence.workflow_showcase_longest_path_layers == 12
    assert gate.evidence.workflow_showcase_detail_links == 21
    assert gate.evidence.workflow_showcase_failure_task_id == FAILED_WORKFLOW_SHOWCASE_TASK_ID
    assert gate.evidence.workflow_showcase_failure_task_state == "FAILED"
    assert gate.evidence.workflow_showcase_failure_attempt_number == 1
    assert gate.evidence.workflow_showcase_failure_failed_nodes == 1
    assert gate.evidence.workflow_showcase_failure_pending_descendants == 5
    assert gate.evidence.workflow_showcase_failure_running_nodes == 0
    assert gate.evidence.workflow_showcase_failure_succeeded_nodes == 15
    assert gate.evidence.workflow_showcase_failure_path_nodes == 16
    assert gate.evidence.workflow_showcase_failure_detail_links == 21


def test_workflow_progress_layer_includes_compatibility_and_showcase_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    verified: list[str] = []
    monkeypatch.setattr(
        gate,
        "_verify_complex_workflow_progress",
        lambda: verified.append("compatibility"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_workflow_showcase_progress",
        lambda: verified.append("showcase"),
    )

    gate._verify_workflow_progress()

    assert verified == ["compatibility", "showcase"]


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_result",
        "partial_topology",
        "wrong_edge",
        "missing_detail_link",
        "failed_error",
        "failed_retry_attempt",
        "wrong_failed_root",
        "descendant_not_pending",
        "required_prerequisite_not_succeeded",
        "forged_enqueue_kwargs",
    ],
)
def test_workflow_showcase_gate_rejects_incomplete_or_misleading_graph_evidence(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _workflow_showcase_gate_responses()
    success_summary_path = f"/api/cluster/workflows/{WORKFLOW_SHOWCASE_TASK_ID}"
    failed_summary_path = f"/api/cluster/workflows/{FAILED_WORKFLOW_SHOWCASE_TASK_ID}"
    page_query = f"?limit={gate_module.WORKFLOW_SHOWCASE_PAGE_LIMIT}"
    success_nodes_path = f"{success_summary_path}/topology/nodes{page_query}"
    success_edges_path = f"{success_summary_path}/topology/edges{page_query}"
    failed_details_path = f"{failed_summary_path}/nodes{page_query}"
    success_poll_path = f"/api/cluster/workflow-showcase/{WORKFLOW_SHOWCASE_TASK_ID}"
    failed_poll_path = f"/api/cluster/workflow-showcase/{FAILED_WORKFLOW_SHOWCASE_TASK_ID}"
    failed_execution_path = (
        f"/api/executions?{urlencode({'task_id': FAILED_WORKFLOW_SHOWCASE_TASK_ID, 'limit': 1})}"
    )

    if failure == "wrong_result":
        responses[success_poll_path]["result"]["status"] = "REVIEW"
    elif failure == "partial_topology":
        responses[success_nodes_path]["items"].pop()
        responses[success_nodes_path]["returned_count"] -= 1
    elif failure == "wrong_edge":
        responses[success_edges_path]["items"][0]["target"] = "0.6"
    elif failure == "missing_detail_link":
        node_id = sorted(set().union(*gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS))[0]
        query = urlencode({"node_id": node_id, "attempt_number": 1})
        indexed_path = f"/api/cluster/workflows/{WORKFLOW_SHOWCASE_TASK_ID}/node-detail?{query}"
        responses[indexed_path]["found"] = False
        responses[indexed_path]["item"] = None
    elif failure == "failed_error":
        responses[failed_poll_path]["error"] = "unexpected failure"
    elif failure == "failed_retry_attempt":
        responses[failed_execution_path]["tasks"][0]["attempt_number"] = 2
    elif failure == "wrong_failed_root":
        details = responses[failed_details_path]["items"]
        by_node = {item["node_id"]: item for item in details}
        by_node[gate_module.WORKFLOW_SHOWCASE_FAILURE_NODE_ID]["state"] = "SUCCEEDED"
        by_node["0.3.g1.0.g0"]["state"] = "FAILED"
    elif failure == "descendant_not_pending":
        details = responses[failed_details_path]["items"]
        by_node = {item["node_id"]: item for item in details}
        descendant = sorted(gate_module.WORKFLOW_SHOWCASE_FAILURE_DESCENDANTS)[0]
        by_node[descendant]["state"] = "RUNNING"
    elif failure == "required_prerequisite_not_succeeded":
        details = responses[failed_details_path]["items"]
        by_node = {item["node_id"]: item for item in details}
        by_node["0.3.g1.0.g0"]["state"] = "RUNNING"
    elif failure == "forged_enqueue_kwargs":
        responses[gate_module.WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_PATH]["kwargs"]["failure_item"] = 1

    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert method in {"GET", "POST"}
        assert headers == {"Authorization": f"Bearer {token}"}
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError):
        gate._verify_workflow_showcase_progress()

    assert gate.evidence.workflow_showcase_task_id == ""
    assert gate.evidence.workflow_showcase_topology_nodes == 0


def _workflow_admin_smoke_evidence(
    *,
    task_id: str = WORKFLOW_TASK_ID,
    task_state: str = "SUCCEEDED",
    topology_nodes: int = 3,
    topology_edges: int = 2,
    pending_nodes: int = 0,
    running_nodes: int = 0,
    succeeded_nodes: int = 3,
    failed_nodes: int = 0,
    failure_path_nodes: int = 0,
    failure_origins: int = 0,
    incoming_failure_edges: int = 0,
) -> dict[str, str | int]:
    return {
        "admin_workflow": "verified",
        "task_id": task_id,
        "task_state": task_state,
        "attempt_number": 1,
        "admin_routes": 6,
        "admin_actions": 3,
        "topology_nodes": topology_nodes,
        "topology_edges": topology_edges,
        "node_details": topology_nodes,
        "graph_status": "AVAILABLE",
        "graph_nodes": topology_nodes,
        "graph_edges": topology_edges,
        "graph_pending_nodes": pending_nodes,
        "graph_running_nodes": running_nodes,
        "graph_succeeded_nodes": succeeded_nodes,
        "graph_failed_nodes": failed_nodes,
        "graph_failure_path_nodes": failure_path_nodes,
        "graph_failure_origins": failure_origins,
        "graph_incoming_failure_edges": incoming_failure_edges,
        "current_manifests": 1,
        "pending_manifests": 0,
        "unlinked_pages": 0,
    }


def _terminal_only_admin_smoke_evidence(
    *,
    task_id: str = TERMINAL_ONLY_WORKFLOW_TASK_ID,
    task_state: str = "SUCCEEDED",
) -> dict[str, bool | int | str]:
    return {
        "admin_workflow": "terminal-summary-verified",
        "task_id": task_id,
        "task_state": task_state,
        "attempt_number": 1,
        "admin_actions": 0,
        "graph_advertised": False,
        "graph_status": "UNAVAILABLE",
        "summary_revision": 1,
        "reporting_policy": "terminal_only",
        "detail_availability": "OMITTED_BY_POLICY",
        "declared_nodes": 13,
        "declared_edges": 13,
        "legacy_progress_null": True,
        "attempt_summary_matches": True,
        "storage_rows": 0,
        "topology_manifests": 0,
        "topology_pages": 0,
        "manifest_links": 0,
        "node_details": 0,
    }


def _seed_workflow_admin_evidence(gate: LocalKubeRayGate) -> None:
    gate.evidence.workflow_task_id = WORKFLOW_TASK_ID
    gate.evidence.workflow_task_state = "SUCCEEDED"
    gate.evidence.workflow_attempt_number = 1
    gate.evidence.workflow_topology_nodes = 3
    gate.evidence.workflow_topology_edges = 2
    gate.evidence.workflow_node_details = 3
    gate.evidence.workflow_failure_task_id = FAILED_WORKFLOW_TASK_ID
    gate.evidence.workflow_failure_task_state = "FAILED"
    gate.evidence.workflow_failure_attempt_number = 1
    gate.evidence.workflow_failure_topology_nodes = 5
    gate.evidence.workflow_failure_topology_edges = 6
    gate.evidence.workflow_failure_node_details = 5
    gate.evidence.workflow_failure_pending_nodes = 1
    gate.evidence.workflow_failure_running_nodes = 1
    gate.evidence.workflow_failure_succeeded_nodes = 2
    gate.evidence.workflow_failure_failed_nodes = 1
    gate.evidence.workflow_terminal_only_task_id = TERMINAL_ONLY_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_task_state = "SUCCEEDED"
    gate.evidence.workflow_terminal_only_attempt_number = 1
    gate.evidence.workflow_terminal_only_schema_version = 3
    gate.evidence.workflow_terminal_only_summary_revision = 1
    gate.evidence.workflow_terminal_only_declared_nodes = 13
    gate.evidence.workflow_terminal_only_declared_edges = 13
    gate.evidence.workflow_terminal_only_failure_task_id = TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_failure_task_state = "FAILED"
    gate.evidence.workflow_terminal_only_failure_attempt_number = 1
    gate.evidence.workflow_terminal_only_failure_schema_version = 3
    gate.evidence.workflow_terminal_only_failure_summary_revision = 1
    gate.evidence.workflow_terminal_only_failure_declared_nodes = 13
    gate.evidence.workflow_terminal_only_failure_declared_edges = 13
    gate.evidence.workflow_showcase_task_id = WORKFLOW_SHOWCASE_TASK_ID
    gate.evidence.workflow_showcase_task_state = "SUCCEEDED"
    gate.evidence.workflow_showcase_attempt_number = 1
    gate.evidence.workflow_showcase_topology_nodes = 21
    gate.evidence.workflow_showcase_topology_edges = 28
    gate.evidence.workflow_showcase_detail_links = 21
    gate.evidence.workflow_showcase_failure_task_id = FAILED_WORKFLOW_SHOWCASE_TASK_ID
    gate.evidence.workflow_showcase_failure_task_state = "FAILED"
    gate.evidence.workflow_showcase_failure_attempt_number = 1
    gate.evidence.workflow_showcase_failure_failed_nodes = 1
    gate.evidence.workflow_showcase_failure_pending_descendants = 5
    gate.evidence.workflow_showcase_failure_running_nodes = 0
    gate.evidence.workflow_showcase_failure_succeeded_nodes = 15
    gate.evidence.workflow_showcase_failure_path_nodes = 16
    gate.evidence.workflow_showcase_failure_detail_links = 21


def test_workflow_admin_gate_executes_same_task_inside_django_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    _seed_workflow_admin_evidence(gate)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append((args, kwargs))
        task_id = args[args.index("--existing-workflow-task-id") + 1]
        terminal_only = "--expected-workflow-reporting-policy" in args
        if terminal_only:
            payload = (
                _terminal_only_admin_smoke_evidence()
                if task_id == TERMINAL_ONLY_WORKFLOW_TASK_ID
                else _terminal_only_admin_smoke_evidence(
                    task_id=TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID,
                    task_state="FAILED",
                )
            )
        else:
            if task_id == WORKFLOW_TASK_ID:
                payload = _workflow_admin_smoke_evidence()
            elif task_id == FAILED_WORKFLOW_TASK_ID:
                payload = _workflow_admin_smoke_evidence(
                    task_id=FAILED_WORKFLOW_TASK_ID,
                    task_state="FAILED",
                    topology_nodes=5,
                    topology_edges=6,
                    pending_nodes=1,
                    running_nodes=1,
                    succeeded_nodes=2,
                    failed_nodes=1,
                    failure_path_nodes=2,
                    failure_origins=1,
                    incoming_failure_edges=1,
                )
            elif task_id == WORKFLOW_SHOWCASE_TASK_ID:
                payload = _workflow_admin_smoke_evidence(
                    task_id=WORKFLOW_SHOWCASE_TASK_ID,
                    topology_nodes=21,
                    topology_edges=28,
                    succeeded_nodes=21,
                )
            else:
                payload = _workflow_admin_smoke_evidence(
                    task_id=FAILED_WORKFLOW_SHOWCASE_TASK_ID,
                    task_state="FAILED",
                    topology_nodes=21,
                    topology_edges=28,
                    pending_nodes=5,
                    running_nodes=0,
                    succeeded_nodes=15,
                    failed_nodes=1,
                    failure_path_nodes=16,
                    failure_origins=1,
                    incoming_failure_edges=1,
                )
        return CommandResult(json.dumps(payload), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._verify_workflow_admin()

    def expected_call(task_id: str, *, terminal_only: bool = False):
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
            "http://127.0.0.1:8000",
            "--timeout",
            "180",
            "--existing-workflow-task-id",
            task_id,
        )
        if terminal_only:
            command = (
                *command,
                "--expected-workflow-reporting-policy",
                "terminal_only",
            )
        return (
            command,
            {
                "timeout": 215,
                "sensitive_output": True,
            },
        )

    assert calls == [
        expected_call(WORKFLOW_TASK_ID),
        expected_call(FAILED_WORKFLOW_TASK_ID),
        expected_call(WORKFLOW_SHOWCASE_TASK_ID),
        expected_call(FAILED_WORKFLOW_SHOWCASE_TASK_ID),
        expected_call(TERMINAL_ONLY_WORKFLOW_TASK_ID, terminal_only=True),
        expected_call(TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID, terminal_only=True),
    ]
    assert gate.evidence.workflow_admin_routes == 6
    assert gate.evidence.workflow_admin_actions == 3
    assert gate.evidence.workflow_current_manifests == 1
    assert gate.evidence.workflow_pending_manifests == 0
    assert gate.evidence.workflow_unlinked_pages == 0
    assert gate.evidence.workflow_failure_path_nodes == 2
    assert gate.evidence.workflow_failure_origins == 1
    assert gate.evidence.workflow_failure_incoming_edges == 1
    assert gate.evidence.workflow_failure_admin_routes == 6
    assert gate.evidence.workflow_failure_current_manifests == 1
    assert gate.evidence.workflow_terminal_only_admin_actions == 0
    assert gate.evidence.workflow_terminal_only_graph_advertised is False
    assert gate.evidence.workflow_terminal_only_storage_rows == 0
    assert gate.evidence.workflow_terminal_only_failure_admin_actions == 0
    assert gate.evidence.workflow_terminal_only_failure_graph_advertised is False
    assert gate.evidence.workflow_terminal_only_failure_storage_rows == 0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("graph_pending_nodes", 4),
        ("graph_succeeded_nodes", 14),
        ("graph_failure_path_nodes", 15),
    ],
)
def test_workflow_admin_gate_rejects_inexact_showcase_failure_projection(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: int,
) -> None:
    gate = LocalKubeRayGate(_config())
    _seed_workflow_admin_evidence(gate)

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        task_id = args[args.index("--existing-workflow-task-id") + 1]
        if task_id == WORKFLOW_TASK_ID:
            payload = _workflow_admin_smoke_evidence()
        elif task_id == FAILED_WORKFLOW_TASK_ID:
            payload = _workflow_admin_smoke_evidence(
                task_id=FAILED_WORKFLOW_TASK_ID,
                task_state="FAILED",
                topology_nodes=5,
                topology_edges=6,
                pending_nodes=1,
                running_nodes=1,
                succeeded_nodes=2,
                failed_nodes=1,
                failure_path_nodes=2,
                failure_origins=1,
                incoming_failure_edges=1,
            )
        elif task_id == WORKFLOW_SHOWCASE_TASK_ID:
            payload = _workflow_admin_smoke_evidence(
                task_id=WORKFLOW_SHOWCASE_TASK_ID,
                topology_nodes=21,
                topology_edges=28,
                succeeded_nodes=21,
            )
        else:
            payload = _workflow_admin_smoke_evidence(
                task_id=FAILED_WORKFLOW_SHOWCASE_TASK_ID,
                task_state="FAILED",
                topology_nodes=21,
                topology_edges=28,
                pending_nodes=5,
                succeeded_nodes=15,
                failed_nodes=1,
                failure_path_nodes=16,
                failure_origins=1,
                incoming_failure_edges=1,
            )
            payload[field_name] = value
        return CommandResult(json.dumps(payload), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="did not match API and storage evidence"):
        gate._verify_workflow_admin()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("task_id", TASK_ID),
        ("admin_routes", {"unexpected": 5}),
        ("topology_nodes", 2),
        ("current_manifests", 0),
        ("pending_manifests", 1),
        ("unlinked_pages", 1),
    ],
)
def test_workflow_admin_gate_rejects_non_scalar_or_inconsistent_evidence(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: object,
) -> None:
    gate = LocalKubeRayGate(_config())
    _seed_workflow_admin_evidence(gate)
    payload = _workflow_admin_smoke_evidence()
    payload[field_name] = cast(Any, value)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(json.dumps(payload), "", 0),
    )

    with pytest.raises(ValueError):
        gate._verify_workflow_admin()

    assert gate.evidence.workflow_admin_routes == 0
    assert gate.evidence.workflow_current_manifests == 0


def test_prometheus_uses_safe_opener_and_exact_ray_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.expected_ray_head_count = 1
    gate.expected_ray_worker_count = 4
    observed: dict[str, object] = {}
    identity_checks: list[str] = []
    monkeypatch.setattr(gate, "_verify_ray_identity", lambda: identity_checks.append("checked"))

    def fetch(
        url: str,
        *,
        request_timeout: float,
        opener: object,
    ) -> object:
        observed.update(
            url=url,
            request_timeout=request_timeout,
            opener=opener,
        )
        return {"status": "success"}

    def wait(
        fetcher: Any,
        *,
        timeout: float,
        interval: float,
        expected_counts: dict[str, int],
    ) -> dict[str, int]:
        assert callable(fetcher)
        fetcher()
        assert timeout == 120
        assert interval == 2
        assert expected_counts == {"django-ray": 1, "ray-head": 1, "ray-workers": 4}
        return expected_counts

    monkeypatch.setattr("scripts.local_kuberay_gate.fetch_active_targets", fetch)
    monkeypatch.setattr("scripts.local_kuberay_gate.wait_for_healthy_targets", wait)

    gate._verify_prometheus()

    assert observed == {
        "url": "http://prometheus.localhost:30080",
        "request_timeout": 10,
        "opener": gate.http_opener,
    }
    assert not any(isinstance(handler, ProxyHandler) for handler in gate.http_opener.handlers)
    assert any(isinstance(handler, RejectRedirects) for handler in gate.http_opener.handlers)
    assert gate.evidence.prometheus_counts["ray-workers"] == 4
    assert identity_checks == ["checked", "checked"]


def test_gate_source_has_no_destructive_preservation_shortcuts() -> None:
    source = (ROOT / "scripts/local_kuberay_gate.py").read_text(encoding="utf-8")
    forbidden = (
        '"delete", "namespace"',
        '"delete", "persistentvolumeclaim"',
        '"delete", "pvc"',
        '"delete", "deployment", "postgres"',
        '"docker", "system", "prune"',
        '"docker", "image", "rm"',
        '"docker", "container", "rm"',
    )

    assert all(fragment not in source for fragment in forbidden)


def test_local_memory_and_rendered_docs_are_outside_docker_build_context() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".vault/" in dockerignore
    assert "site/" in dockerignore
    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    for name in ("Dockerfile.dockerignore", "Dockerfile.ray.dockerignore"):
        patterns = (ROOT / name).read_text(encoding="utf-8").splitlines()
        assert "**" in patterns
        assert not any(".env" in pattern for pattern in patterns)
        assert "!pyproject.toml" in patterns
        assert "!src/**" in patterns


def test_evidence_binds_the_stable_source_tree_not_only_the_pre_amend_commit() -> None:
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    gate._verify_final_identity = lambda: None  # type: ignore[method-assign]
    gate.evidence.commit = COMMIT
    gate.evidence.source_tree = SOURCE_TREE
    gate.evidence.kubeconfig_sha256 = "9" * 64
    gate.evidence.kubernetes_server = "https://kubernetes.docker.internal:6443"
    gate.evidence.docker_host = "npipe:////./pipe/dockerDesktopLinuxEngine"
    gate.evidence.app_tag = APP_TAG
    gate.evidence.worker_tag = f"django-ray-worker:{TAG}"
    gate.evidence.app_image_id = IMAGE_ID
    gate.evidence.worker_image_id = IMAGE_ID
    gate.evidence.setup_bundle_bytes = 1
    gate.evidence.setup_bundle_sha256 = "f" * 64
    gate.evidence.ray_restart = "performed"
    gate.evidence.ray_cluster_uid = "cluster-owner"
    gate.evidence.ray_head_count = 1
    gate.evidence.ray_worker_count = 1
    gate.evidence.deployments = dict.fromkeys(APP_DEPLOYMENTS, 1)
    gate.evidence.task_id = TASK_ID
    gate.evidence.task_state = "SUCCEEDED"
    gate.evidence.task_result = 5
    gate.evidence.api_execution_delete_rejected = True
    gate.evidence.api_legacy_workflow_graph_absent = True
    gate.evidence.runtime_env_encryption_overlay = True
    gate.evidence.runtime_env_encryption_canary = True
    gate.evidence.runtime_env_encryption_envelope = True
    gate.evidence.runtime_env_encryption_marker_absent = True
    gate.evidence.runtime_env_encryption_tamper_rejected = True
    gate.evidence.runtime_env_encryption_unknown_key_rejected = True
    gate.evidence.runtime_env_encryption_retry_preserved = True
    gate.evidence.runtime_env_encryption_logs_clear = True
    gate.evidence.django_ray_secret_preserved = True
    gate.evidence.workflow_task_id = WORKFLOW_TASK_ID
    gate.evidence.workflow_task_state = "SUCCEEDED"
    gate.evidence.workflow_schema_version = 3
    gate.evidence.workflow_availability = "AVAILABLE"
    gate.evidence.workflow_topology_nodes = 8
    gate.evidence.workflow_topology_edges = 8
    gate.evidence.workflow_node_details = 8
    gate.evidence.workflow_leaf_tasks = 3
    gate.evidence.workflow_admin_routes = 5
    gate.evidence.workflow_admin_actions = 3
    gate.evidence.workflow_current_manifests = 1
    gate.evidence.workflow_pending_manifests = 0
    gate.evidence.workflow_unlinked_pages = 0
    gate.evidence.workflow_terminal_only_task_id = TERMINAL_ONLY_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_task_state = "SUCCEEDED"
    gate.evidence.workflow_terminal_only_attempt_number = 1
    gate.evidence.workflow_terminal_only_schema_version = 3
    gate.evidence.workflow_terminal_only_summary_revision = 1
    gate.evidence.workflow_terminal_only_declared_nodes = 13
    gate.evidence.workflow_terminal_only_declared_edges = 13
    gate.evidence.workflow_terminal_only_admin_actions = 0
    gate.evidence.workflow_terminal_only_graph_advertised = False
    gate.evidence.workflow_terminal_only_storage_rows = 0
    gate.evidence.workflow_terminal_only_failure_task_id = TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_failure_task_state = "FAILED"
    gate.evidence.workflow_terminal_only_failure_attempt_number = 1
    gate.evidence.workflow_terminal_only_failure_schema_version = 3
    gate.evidence.workflow_terminal_only_failure_summary_revision = 1
    gate.evidence.workflow_terminal_only_failure_declared_nodes = 13
    gate.evidence.workflow_terminal_only_failure_declared_edges = 13
    gate.evidence.workflow_terminal_only_failure_admin_actions = 0
    gate.evidence.workflow_terminal_only_failure_graph_advertised = False
    gate.evidence.workflow_terminal_only_failure_storage_rows = 0
    gate.evidence.prometheus_counts = dict.fromkeys(("django-ray", "ray-head", "ray-workers"), 1)

    gate._emit_evidence()

    assert f"source_commit_at_run={COMMIT}" in output
    assert f"source_tree={SOURCE_TREE}" in output


def test_complete_runtime_evidence_block_is_reconstructable_and_bounded() -> None:
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    gate._verify_final_identity = lambda: None  # type: ignore[method-assign]
    gate.evidence.commit = COMMIT
    gate.evidence.source_tree = SOURCE_TREE
    gate.evidence.kubeconfig_sha256 = "9" * 64
    gate.evidence.kubernetes_server = "https://localhost:6443/" + ("api/" * 80)
    gate.evidence.docker_host = "unix:///" + ("local-socket-segment/" * 30)
    gate.evidence.app_tag = APP_TAG
    gate.evidence.worker_tag = f"django-ray-worker:{TAG}"
    gate.evidence.app_image_id = IMAGE_ID
    gate.evidence.worker_image_id = IMAGE_ID
    gate.evidence.setup_bundle_bytes = 293_956
    gate.evidence.setup_bundle_sha256 = "f" * 64
    gate.evidence.ray_restart = "performed"
    gate.evidence.ray_cluster_uid = "cluster-owner"
    gate.evidence.ray_pod_identity_sha256 = "8" * 64
    gate.evidence.ray_head_count = 1
    gate.evidence.ray_worker_count = 4
    gate.evidence.deployments = dict.fromkeys(APP_DEPLOYMENTS, 1)
    gate.evidence.task_id = TASK_ID
    gate.evidence.task_state = "SUCCEEDED"
    gate.evidence.task_result = 5
    gate.evidence.api_execution_delete_rejected = True
    gate.evidence.api_legacy_workflow_graph_absent = True
    gate.evidence.runtime_env_encryption_overlay = True
    gate.evidence.runtime_env_encryption_canary = True
    gate.evidence.runtime_env_encryption_envelope = True
    gate.evidence.runtime_env_encryption_marker_absent = True
    gate.evidence.runtime_env_encryption_tamper_rejected = True
    gate.evidence.runtime_env_encryption_unknown_key_rejected = True
    gate.evidence.runtime_env_encryption_retry_preserved = True
    gate.evidence.runtime_env_encryption_logs_clear = True
    gate.evidence.django_ray_secret_preserved = True
    gate.evidence.workflow_task_id = WORKFLOW_TASK_ID
    gate.evidence.workflow_task_state = "SUCCEEDED"
    gate.evidence.workflow_schema_version = 3
    gate.evidence.workflow_availability = "AVAILABLE"
    gate.evidence.workflow_topology_nodes = 8
    gate.evidence.workflow_topology_edges = 8
    gate.evidence.workflow_node_details = 8
    gate.evidence.workflow_leaf_tasks = 3
    gate.evidence.workflow_admin_routes = 5
    gate.evidence.workflow_admin_actions = 3
    gate.evidence.workflow_current_manifests = 1
    gate.evidence.workflow_pending_manifests = 0
    gate.evidence.workflow_unlinked_pages = 0
    gate.evidence.workflow_terminal_only_task_id = TERMINAL_ONLY_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_task_state = "SUCCEEDED"
    gate.evidence.workflow_terminal_only_attempt_number = 1
    gate.evidence.workflow_terminal_only_schema_version = 3
    gate.evidence.workflow_terminal_only_summary_revision = 1
    gate.evidence.workflow_terminal_only_declared_nodes = 13
    gate.evidence.workflow_terminal_only_declared_edges = 13
    gate.evidence.workflow_terminal_only_admin_actions = 0
    gate.evidence.workflow_terminal_only_graph_advertised = False
    gate.evidence.workflow_terminal_only_storage_rows = 0
    gate.evidence.workflow_terminal_only_failure_task_id = TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_failure_task_state = "FAILED"
    gate.evidence.workflow_terminal_only_failure_attempt_number = 1
    gate.evidence.workflow_terminal_only_failure_schema_version = 3
    gate.evidence.workflow_terminal_only_failure_summary_revision = 1
    gate.evidence.workflow_terminal_only_failure_declared_nodes = 13
    gate.evidence.workflow_terminal_only_failure_declared_edges = 13
    gate.evidence.workflow_terminal_only_failure_admin_actions = 0
    gate.evidence.workflow_terminal_only_failure_graph_advertised = False
    gate.evidence.workflow_terminal_only_failure_storage_rows = 0
    gate.evidence.prometheus_counts = {
        "django-ray": 1,
        "ray-head": 1,
        "ray-workers": 4,
    }

    gate._emit_evidence()

    def reconstructed(key: str) -> str:
        direct = f"{key}="
        direct_values = [line.removeprefix(direct) for line in output if line.startswith(direct)]
        if direct_values:
            assert len(direct_values) == 1
            return direct_values[0]
        count_line = next(line for line in output if line.startswith(f"{key}_parts="))
        count = int(count_line.partition("=")[2])
        return "".join(
            next(
                line.partition("=")[2]
                for line in output
                if line.startswith(f"{key}_part_{index:03d}=")
            )
            for index in range(1, count + 1)
        )

    assert reconstructed("kubernetes_server") == gate.evidence.kubernetes_server
    assert reconstructed("docker_host") == gate.evidence.docker_host
    assert reconstructed("app_image_id") == IMAGE_ID
    assert reconstructed("api_execution_delete_rejected") == "True"
    assert reconstructed("api_legacy_workflow_graph_absent") == "True"
    assert reconstructed("runtime_env_encryption_canary") == "True"
    assert reconstructed("runtime_env_encryption_envelope") == "True"
    assert reconstructed("runtime_env_encryption_retry_preserved") == "True"
    assert reconstructed("django_ray_secret_preserved") == "True"
    assert reconstructed("workflow_task_id") == WORKFLOW_TASK_ID
    assert reconstructed("workflow_availability") == "AVAILABLE"
    assert reconstructed("workflow_terminal_only_task_id") == TERMINAL_ONLY_WORKFLOW_TASK_ID
    assert reconstructed("workflow_showcase_task_id") == ""
    assert (
        reconstructed("workflow_terminal_only_failure_task_id")
        == TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    )
    encryption_evidence = "\n".join(
        line
        for line in output
        if line.startswith(("runtime_env_encryption_", "django_ray_secret_preserved="))
    )
    for forbidden in ("task_id", "sha256", "key_id", "nonce", "ciphertext", "envelope={"):
        assert forbidden not in encryption_evidence
    assert all(len(line) <= EVIDENCE_LINE_LIMIT for line in output)


def test_guidance_requires_concise_semantic_gate_summaries() -> None:
    guidance = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in ("AGENTS.md", "CONTRIBUTING.md", "docs/contributing.md")
    }

    for path, content in guidance.items():
        normalized = " ".join(content.split())
        assert "local-kuberay-gate.md" in normalized, path
        assert "concise semantic validation summary" in normalized, path
        assert "exact gate command" in normalized, path
        assert "cold-Ray decision" in normalized, path
        assert "source-tree match" in normalized, path
        assert "complete secret-free evidence block" in normalized, path
        assert "runtime diagnostics" in normalized, path
        assert "clean checkout" in normalized, path
        assert "do not paste" in normalized, path

    combined = "\n".join(guidance.values())
    assert "copy the command's complete secret-free evidence block" not in combined
    assert "Record the complete secret-free evidence block" not in combined


def test_gate_guide_separates_runtime_evidence_from_durable_summary() -> None:
    guide = (ROOT / "docs/deployment/local-kuberay-gate.md").read_text(encoding="utf-8")

    assert "## Runtime evidence and durable validation summary" in guide
    assert "=== Local KubeRay final gate evidence ===" in guide
    assert "Do not copy the complete block into a retained commit or PR" in guide
    assert "exact `uv run make k8s-final-gate` command and arguments" in guide
    assert "`K8S_RAY_RESTART=required`: passed" in guide
    assert "authenticated API smoke" in guide
    assert "task succeeded with result 5" in guide
    assert "`workflow-progress`" in guide
    assert "`workflow-admin`" in guide
    assert "`runtime-env-encryption`" in guide
    assert "storage_encryption_verified=true" in guide
    assert "corrupt and unknown-key rows failed before Ray" in guide
    assert "deterministic first-attempt failure" in guide
    assert "authenticated admin graph retained the incoming" in guide
    assert "all Ray pods were cold-replaced" in guide
    assert "data-bearing resources were preserved" in guide
    assert "focused value or artifact in an issue or PR comment" in guide
    assert "without copying the tree hash into history" in guide
    assert "complete block can be copied into a commit" not in guide


def test_gate_document_retains_trigger_matrix_reference_evidence_and_preservation() -> None:
    guide = (ROOT / "docs/deployment/local-kuberay-gate.md").read_text(encoding="utf-8")

    assert "## Trigger matrix" in guide
    assert "Required" in guide
    assert "Recommended" in guide
    assert "Not applicable" in guide
    assert "k8s/pilots/compiled-graph/" in guide
    assert "scripts/kuberay_compiled_graph_pilot.py" in guide
    assert "#102" in guide
    assert "Issues #144, #145, #146, and #147" in guide
    assert "PRs #148, #149, #150, and #151" in guide
    assert "1cef8e6042ed0fe811cc9ee99b8332a75c887c75" in guide
    assert "PostgreSQL" in guide and "PVC" in guide
    assert "source_tree" in guide and "git rev-parse HEAD^{tree}" in guide
    assert "Do not automate token retrieval into browser logs" in guide
    assert "private, flattened kubeconfig snapshot" in guide
    assert "same one-time archive" in guide
    assert "32-512 characters from the Bearer" in guide
    assert "`token68` alphabet" in guide
    assert "exact desired, updated, ready, and available replicas" in guide
    assert "Deployment UID -> current ReplicaSet UID -> Pod UID" in guide
    assert "deadline-bounded convergence barrier" in guide
    assert "zero-replica historical ReplicaSets" in guide
    assert "`app-convergence`" in guide
    assert "UID/container/image identity-set SHA-256" in guide
    assert "sanitized environments" in guide
    assert "rechecked before and after Prometheus" in guide
    assert "direct NodePort pair" in guide
    assert "`http://localhost:30090`" in guide
    assert "K8S_PROMETHEUS_URL=http://prometheus.localhost:30080" not in guide
    assert "Each emitted line is at most 72 characters" in guide
    assert "key_part_001" in guide
    assert "RuntimeEnv snapshot storage, encryption settings or dependencies" in guide
    assert "with no selector in an init container, shared ConfigMap, setup Job, or Ray pod" in guide
    assert "task IDs, hashes, key IDs, nonces, ciphertext, or envelopes" in guide
    assert "full base64 `django-ray-secret.data` mapping" in guide
    encryption_row = next(
        line for line in guide.splitlines() if line.startswith("| RuntimeEnv snapshot storage")
    )
    assert "| Required | `required` |" in encryption_row


def test_make_gate_requires_explicit_context_namespace_and_ray_decision() -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")

    assert "k8s-final-gate-preflight:" in makefile
    assert "k8s-final-gate:" in makefile
    assert '--context "$(K8S_CONTEXT)"' in makefile
    assert '--namespace "$(K8S_NAMESPACE)"' in makefile
    assert '--ray-restart "$(K8S_RAY_RESTART)"' in makefile
    assert "--preflight-only" in makefile
    assert "if [ -z" not in makefile
    assert "$(if $(strip $(K8S_CONTEXT))" in makefile
    assert "$(if $(strip $(K8S_RAY_RESTART))" in makefile


@pytest.mark.parametrize("target", ["k8s-final-gate-preflight", "k8s-final-gate"])
def test_make_gate_wrapper_expands_on_the_host_shell(target: str) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository wrapper")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            target,
            "K8S_CONTEXT=docker-desktop",
            f"K8S_NAMESPACE={EXPECTED_NAMESPACE}",
            "K8S_RAY_RESTART=required",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "python -m scripts.local_kuberay_gate" in result.stdout
    assert '--web-url "http://localhost:30080"' in result.stdout
    assert '--prometheus-url "http://localhost:30090"' in result.stdout
    assert "if [ -z" not in result.stdout


def test_make_gate_wrapper_rejects_a_missing_context_before_python() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository wrapper")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "k8s-final-gate-preflight",
            f"K8S_NAMESPACE={EXPECTED_NAMESPACE}",
            "K8S_RAY_RESTART=required",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "K8S_CONTEXT is required" in result.stderr
    assert "python -m scripts.local_kuberay_gate" not in result.stdout


def _stub_successful_gate_layers(
    gate: LocalKubeRayGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for method_name in (
        "_preflight",
        "_build_images",
        "_apply_overlay",
        "_run_setup",
        "_apply_workloads",
        "_prepare_ray",
        "_restart_task_managers",
        "_wait_for_application_topology",
        "_verify_deployed_images",
        "_verify_generic_ray_nodes",
        "_verify_probes",
        "_verify_api",
        "_verify_runtime_env_encryption",
        "_verify_workflow_progress",
        "_verify_workflow_admin",
        "_verify_prometheus",
    ):
        monkeypatch.setattr(gate, method_name, lambda: None)


def test_runtime_env_encryption_runs_after_api_and_before_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    gate = LocalKubeRayGate(_config(), output=lambda _value: None)
    _stub_successful_gate_layers(gate, monkeypatch)
    monkeypatch.setattr(gate, "_verify_api", lambda: events.append("api-smoke"))
    monkeypatch.setattr(
        gate,
        "_verify_runtime_env_encryption",
        lambda: events.append("runtime-env-encryption"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_workflow_progress",
        lambda: events.append("workflow-progress"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_workflow_admin",
        lambda: events.append("workflow-admin"),
    )
    monkeypatch.setattr(gate, "_verify_prometheus", lambda: events.append("prometheus"))
    monkeypatch.setattr(
        gate,
        "_evidence_lines",
        lambda: ("=== Local KubeRay final gate evidence ===",),
    )

    gate.run()

    assert events == [
        "api-smoke",
        "runtime-env-encryption",
        "workflow-progress",
        "workflow-admin",
        "prometheus",
    ]


def test_temporary_workspace_creation_failure_is_bounded_redacted_and_has_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = TOKEN68
    encoded = base64.b64encode(token.encode()).decode()
    mixed_case_encoded = _percent_hex_case_variants(quote(encoded, safe=""))[-1]
    gate = LocalKubeRayGate(_config())
    gate.redactor.register(token)
    gate.redactor.register(encoded)

    def fail_creation(*args: object, **kwargs: object) -> None:
        raise OSError(f"{'x' * 20_000}{mixed_case_encoded}")

    monkeypatch.setattr(gate_module.tempfile, "TemporaryDirectory", fail_creation)
    monkeypatch.setattr(gate_module, "LocalKubeRayGate", lambda config: gate)

    result = gate_module.main(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "skip",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert len(captured.err) <= MAX_OUTPUT_CHARACTERS
    assert "FAILED [preflight]: temporary workspace creation failed" in captured.err
    assert "workspace creation error:" in captured.err
    assert token not in captured.err
    assert encoded not in captured.err
    assert mixed_case_encoded not in captured.err
    assert "Traceback" not in captured.err
    assert "=== Local KubeRay final gate evidence ===" not in captured.out


def test_successful_gate_with_cleanup_failure_withholds_all_final_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    gate = LocalKubeRayGate(_config(), output=lambda value: events.append(f"output:{value}"))
    _stub_successful_gate_layers(gate, monkeypatch)
    monkeypatch.setattr(
        gate,
        "_evidence_lines",
        lambda: ("=== Local KubeRay final gate evidence ===", f"source_tree={SOURCE_TREE}"),
    )

    class CleanupFailure:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.name = str(tmp_path)
            events.append("workspace-created")

        def cleanup(self) -> None:
            events.append("workspace-cleanup")
            raise OSError("cleanup-failure-marker")

    monkeypatch.setattr(gate_module.tempfile, "TemporaryDirectory", CleanupFailure)
    monkeypatch.setattr(gate_module, "LocalKubeRayGate", lambda config: gate)

    result = gate_module.main(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "skip",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "FAILED [final-identity]: temporary workspace cleanup failed" in captured.err
    assert "cleanup-failure-marker" in captured.err
    assert len(captured.err) <= MAX_OUTPUT_CHARACTERS
    assert "Traceback" not in captured.err
    assert "workspace-cleanup" in events
    assert not any("=== Local KubeRay final gate evidence ===" in event for event in events)
    assert "output:[final-identity] passed" not in events


def test_primary_and_cleanup_failures_are_both_preserved_without_false_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = TOKEN68
    encoded = base64.b64encode(token.encode()).decode()
    mixed_case_encoded = _percent_hex_case_variants(quote(encoded, safe=""))[-1]
    events: list[str] = []
    gate = LocalKubeRayGate(_config(), output=lambda value: events.append(f"output:{value}"))
    gate.redactor.register(token)
    gate.redactor.register(encoded)
    _stub_successful_gate_layers(gate, monkeypatch)

    def fail_api() -> None:
        raise RuntimeError(f"{'p' * 20_000}primary-failure-marker")

    monkeypatch.setattr(gate, "_verify_api", fail_api)
    monkeypatch.setattr(
        gate,
        "diagnostics",
        lambda layer: events.append(f"diagnostics:{layer}"),
    )

    class PrimaryAndCleanupFailure:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.name = str(tmp_path)
            events.append("workspace-created")

        def cleanup(self) -> None:
            events.append("workspace-cleanup")
            raise OSError(f"{'c' * 20_000}{mixed_case_encoded}")

    monkeypatch.setattr(
        gate_module.tempfile,
        "TemporaryDirectory",
        PrimaryAndCleanupFailure,
    )
    monkeypatch.setattr(gate_module, "LocalKubeRayGate", lambda config: gate)

    result = gate_module.main(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "skip",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert len(captured.err) <= MAX_OUTPUT_CHARACTERS
    assert "FAILED [api-smoke]:" in captured.err
    assert "primary-failure-marker" in captured.err
    assert "temporary workspace cleanup also failed:" in captured.err
    assert token not in captured.err
    assert encoded not in captured.err
    assert mixed_case_encoded not in captured.err
    assert "Traceback" not in captured.err
    assert events.index("diagnostics:api-smoke") < events.index("workspace-cleanup")
    assert not any("=== Local KubeRay final gate evidence ===" in event for event in events)
    assert "output:[final-identity] passed" not in events


def test_success_evidence_is_emitted_only_after_workspace_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    gate = LocalKubeRayGate(_config(), output=lambda value: events.append(f"output:{value}"))
    _stub_successful_gate_layers(gate, monkeypatch)
    monkeypatch.setattr(
        gate,
        "_evidence_lines",
        lambda: ("=== Local KubeRay final gate evidence ===", f"source_tree={SOURCE_TREE}"),
    )

    class SuccessfulTemporaryDirectory:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.name = str(tmp_path)
            events.append("workspace-created")

        def cleanup(self) -> None:
            events.append("workspace-cleanup")

    monkeypatch.setattr(
        gate_module.tempfile,
        "TemporaryDirectory",
        SuccessfulTemporaryDirectory,
    )

    gate.run()

    cleanup_index = events.index("workspace-cleanup")
    final_pass_index = events.index("output:[final-identity] passed")
    evidence_index = events.index("output:=== Local KubeRay final gate evidence ===")
    assert cleanup_index < final_pass_index < evidence_index
    assert gate.temp_root is None
    assert gate.diagnostics_attempted is False


def test_final_evidence_identity_failure_is_labeled_once_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate = LocalKubeRayGate(_config())
    for method_name in (
        "_preflight",
        "_build_images",
        "_apply_overlay",
        "_run_setup",
        "_apply_workloads",
        "_prepare_ray",
        "_restart_task_managers",
        "_wait_for_application_topology",
        "_verify_deployed_images",
        "_verify_generic_ray_nodes",
        "_verify_probes",
        "_verify_api",
        "_verify_runtime_env_encryption",
        "_verify_workflow_progress",
        "_verify_workflow_admin",
        "_verify_prometheus",
    ):
        monkeypatch.setattr(gate, method_name, lambda: None)
    identity_checks = 0

    def fail_final_identity() -> None:
        nonlocal identity_checks
        identity_checks += 1
        raise RuntimeError("final identity changed before evidence")

    monkeypatch.setattr(gate, "_verify_final_identity", fail_final_identity)
    monkeypatch.setattr(gate_module, "LocalKubeRayGate", lambda config: gate)

    result = gate_module.main(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "skip",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert identity_checks == 1
    assert "FAILED [final-identity]: final identity changed before evidence" in captured.err
    assert "Traceback" not in captured.err
    assert "=== Local KubeRay final gate evidence ===" not in captured.out


def test_main_preserves_primary_failure_when_diagnostics_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "diagnostic-token-that-must-never-be-printed"

    class FailingGate:
        def __init__(self, config: GateConfig) -> None:
            self.redactor = Redactor()
            self.redactor.register(token)

        def run(self) -> None:
            raise GateError("api-smoke", token)

        def diagnostics(self, layer: str) -> None:
            raise RuntimeError(token)

    monkeypatch.setattr(gate_module, "LocalKubeRayGate", FailingGate)

    assert (
        gate_module.main(
            [
                "--context",
                "docker-desktop",
                "--namespace",
                EXPECTED_NAMESPACE,
                "--ray-restart",
                "skip",
            ]
        )
        == 1
    )

    stderr = capsys.readouterr().err
    assert token not in stderr
    assert "FAILED [api-smoke]: [REDACTED]" in stderr
    assert "bounded diagnostics unavailable: [REDACTED]" in stderr


def test_main_bounds_and_redacts_mixed_case_encoded_diagnostics_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    encoded_variants = _percent_hex_case_variants(quote(TOKEN68, safe=""))
    mixed_case_encoded = encoded_variants[len(encoded_variants) // 2 + 1]

    class FailingGate:
        def __init__(self, config: GateConfig) -> None:
            self.redactor = Redactor()
            self.redactor.register(TOKEN68)

        def run(self) -> None:
            raise GateError("api-smoke", "primary failure")

        def diagnostics(self, layer: str) -> None:
            raise RuntimeError(f"{'x' * 20_000}{mixed_case_encoded}")

    monkeypatch.setattr(gate_module, "LocalKubeRayGate", FailingGate)

    assert (
        gate_module.main(
            [
                "--context",
                "docker-desktop",
                "--namespace",
                EXPECTED_NAMESPACE,
                "--ray-restart",
                "skip",
            ]
        )
        == 1
    )

    stderr = capsys.readouterr().err
    assert len(stderr) <= MAX_OUTPUT_CHARACTERS
    assert stderr.startswith("[truncated redacted error; original_characters=")
    assert "FAILED [api-smoke]: primary failure" in stderr
    assert TOKEN68 not in stderr
    assert mixed_case_encoded not in stderr
