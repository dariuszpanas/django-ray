"""Shared fixture builders and constants for local KubeRay gate unit tests."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, quote_plus, urlencode

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_gate import (
    APP_DEPLOYMENTS,
    EXPECTED_NAMESPACE,
    EXPECTED_POLL_DIAGNOSTIC_MAX_BYTES,
    EXPECTED_POLL_RESPONSE_MAX_BYTES,
    EXPECTED_RESOURCE_IDENTITIES,
    EXPECTED_TASK_STATUS_INPUT_MAX_BYTES,
    EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
    RAY_CLUSTER_LABEL,
    RAY_CLUSTER_NAME,
    RUNTIME_ENV_ENCRYPTION_ENV,
    SETUP_JOB,
    DeploymentContract,
    GateConfig,
    LocalKubeRayGate,
    PodImageContract,
    normalize_ray_topology,
)

ROOT = Path(__file__).resolve().parents[2]


COMMIT = "a" * 40


SOURCE_TREE = "e" * 40


TAG = "local-gate-tree-eeeeeeeeeeee-20260721123456-deadbeef"


APP_TAG = f"django-ray:{TAG}"


IMAGE_ID = f"sha256:{'b' * 64}"


PRIOR_RELEASED_V040_TAG = (
    f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:"
    "released-v040-local-gate-tree-abcdef123456-20260813010203-cafebabe"
)


PRIOR_RELEASED_V040_HOSTNAME = f"dr-v040-{'1' * 32}"


PRIOR_RELEASED_V040_WORKER_ID = "11234567-89ab-4cde-8fab-0123456789ab"


PROTOCOL_V2_POISON = "protocol_v2_application_poison_0123456789abcdef0123456789abcdef"


TASK_ID = "15200000-0000-4000-8000-000000000001"


PROTOCOL_V1_SURVIVAL_TASK_ID = "16200000-0000-4000-8000-000000000011"


PROTOCOL_V1_SURVIVAL_MARKER = "protocol_v1_handoff_queued_22222222222242228222222222222222"


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


WORKFLOW_RECOVERY_TASK_ID = "e5200000-0000-4000-8000-00000000000e"


WORKFLOW_RECOVERY_RUN_IDS = (
    "f5200000-0000-4000-8000-00000000000f",
    "06200000-0000-4000-8000-000000000010",
    "16200000-0000-4000-8000-000000000011",
)


TOKEN68 = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/=="


RUNTIME_ENV_CANARY_TASK_ID = "a5200000-0000-4000-8000-000000000010"


RUNTIME_ENV_TAMPER_TASK_ID = "b5200000-0000-4000-8000-000000000011"


RUNTIME_ENV_UNKNOWN_KEY_TASK_ID = "c5200000-0000-4000-8000-000000000012"


def _task_status_payload(
    *,
    state: str = "SUCCEEDED",
    task_id: str = TASK_ID,
    omission_reason: str | None = None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "status": gate_module.TASK_STATUS_BY_STATE[state],
        "state": state,
        "attempt_number": 1,
        "execution_generation": 1,
        "execution_protocol_version": 1,
        "created_with_django_ray_version": "0.5.0-producer",
        "managed_with_django_ray_version": "0.5.0-manager",
        "executor_django_ray_version": "0.5.0-executor",
        "protocol_compatible_worker_available": True,
        "queue_capacity_attested": False,
        "args": [2, 3] if omission_reason is None else None,
        "kwargs": {} if omission_reason is None else None,
        "input_omission_reason": omission_reason,
        "input_max_bytes": EXPECTED_TASK_STATUS_INPUT_MAX_BYTES,
        "response_max_bytes": EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
    }


def _execution_protocol_metrics(*, unsupported_queued: int = 0) -> bytes:
    name = gate_module.EXPECTED_EXECUTION_PROTOCOL_METRIC
    lines = [
        f"# HELP {name} Total tasks by bounded execution-protocol bucket and state",
        f"# TYPE {name} gauge",
    ]
    lines.extend(
        (
            f'{name}{{protocol="{protocol}",state="{state}"}} '
            f"{unsupported_queued if (protocol, state) == ('other', 'QUEUED') else 0}"
        )
        for protocol in ("1", "other")
        for state in gate_module.TASK_STATUS_BY_STATE
    )
    return ("\n".join(lines) + "\n").encode()


def _protocol_v2_rejection_payload() -> dict[str, object]:
    return {
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


def _protocol_v2_target_execution_payload() -> dict[str, object]:
    return {
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


def _bounded_poll_metadata() -> dict[str, object]:
    return {
        "result_omission_reason": None,
        "error_omission_reason": None,
        "diagnostic_max_bytes": EXPECTED_POLL_DIAGNOSTIC_MAX_BYTES,
        "response_max_bytes": EXPECTED_POLL_RESPONSE_MAX_BYTES,
    }


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
    head_sidecar: bool = False,
) -> dict[str, object]:
    metadata: dict[str, object] = {"name": RAY_CLUSTER_NAME, "namespace": namespace}
    if uid is not None:
        metadata["uid"] = uid
    head_containers = [{"name": "ray-head", "image": "rayproject/ray:2.60.0-py312"}]
    if head_sidecar:
        head_containers.append({"name": "dashboard-importer", "image": "python:3.12-slim"})
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayCluster",
        "metadata": metadata,
        "spec": {
            "rayVersion": "2.60.0",
            "enableInTreeAutoscaling": False,
            "headGroupSpec": {
                "serviceType": "ClusterIP",
                "rayStartParams": {"num-cpus": "1"},
                "template": {"spec": {"containers": head_containers}},
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
                                    "image": "rayproject/ray:2.60.0-py312",
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
    image: str = "rayproject/ray:2.60.0-py312",
    head_sidecar: bool = False,
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
    if component == "head" and head_sidecar:
        sidecar = {"name": "dashboard-importer", "image": "python:3.12-slim"}
        spec["containers"].append(sidecar)
        cast(list[dict[str, object]], status["containerStatuses"]).append(
            {
                **sidecar,
                "imageID": f"containerd://sha256:{'d' * 64}",
                "ready": True,
            }
        )
    if component == "worker":
        spec["initContainers"] = [
            {
                "name": gate_module.KUBERAY_WAIT_GCS_INIT,
                "image": image,
                "command": ["/bin/bash", "-c", "--"],
                "args": [_wait_gcs_ready_script()],
                "env": [
                    {"name": "RAY_AUTH_MODE", "value": "token"},
                    {
                        "name": "RAY_AUTH_TOKEN",
                        "valueFrom": {
                            "secretKeyRef": {"name": "django-ray-auth", "key": "RAY_AUTH_TOKEN"}
                        },
                    },
                ],
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
