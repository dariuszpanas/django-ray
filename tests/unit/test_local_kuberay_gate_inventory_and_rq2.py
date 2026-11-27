"""Unit tests for local KubeRay gate RQ2 reconciliation, pod inventory, probes, and image builds."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_gate import (
    APP_DEPLOYMENTS,
    EXPECTED_NAMESPACE,
    EXPECTED_PROBE_HOST,
    EXPECTED_PROBE_PATH,
    MAX_OUTPUT_CHARACTERS,
    RAY_CLUSTER_LABEL,
    RAY_CLUSTER_NAME,
    RUNTIME_ENV_REQUIRED_MEMBER,
    SETUP_JOB,
    TASK_MANAGER_DEPLOYMENTS,
    CommandResult,
    DeploymentContract,
    GateError,
    LocalKubeRayGate,
    PodImageContract,
    Redactor,
    inspect_probe_contract,
    inspect_setup_log,
    normalize_ray_topology,
    normalize_runtime_image_id,
    parse_docker_image_inspect,
    parse_runtime_archive_probe,
    parse_task_result,
    pod_image_contract,
)
from tests.unit._local_kuberay_gate_helpers import (
    APP_TAG,
    COMMIT,
    IMAGE_ID,
    ROOT,
    SOURCE_TREE,
    TAG,
    TASK_ID,
    _application_inventory_fixture,
    _application_pod,
    _application_replicaset,
    _config,
    _kubeconfig_payload,
    _live_application_deployment,
    _old_terminating_application_generation,
    _ray_cluster,
    _ray_pod,
    _set_ray_topology,
    _setup_pod,
)


def test_live_rq2_in_pod_inspectors_are_valid_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    observed_fields: list[str] = []

    def compile_private_script(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, f"<{field_name}>", "exec")
        observed_fields.append(field_name)
        return {
            "rq2 positive task enqueue": {"task_id": TASK_ID},
            "rq2 positive task observation": {},
            "rq2 missing-reference submission": {},
            "rq2 missing-reference observation": {},
            "rq2 missing-reference stale fence": {"updated": 1},
            "rq2 missing-reference disposition": {},
        }[field_name]

    monkeypatch.setattr(gate, "_sensitive_django_shell", compile_private_script)

    assert gate._enqueue_ray_job_gate_task() == TASK_ID
    gate._observe_ray_job_gate_task(TASK_ID)
    gate._submit_missing_ray_job_request_reference("private-marker")
    gate._observe_missing_ray_job_request_reference(
        task_id=TASK_ID,
        marker="private-marker",
    )
    gate._age_missing_ray_job_execution(TASK_ID)
    gate._observe_missing_ray_job_disposition(TASK_ID)

    assert observed_fields == [
        "rq2 positive task enqueue",
        "rq2 positive task observation",
        "rq2 missing-reference submission",
        "rq2 missing-reference observation",
        "rq2 missing-reference stale fence",
        "rq2 missing-reference disposition",
    ]


def test_ray_process_probe_tolerates_exits_but_still_detects_exposure(tmp_path: Path) -> None:
    unreadable = tmp_path / "99999991" / "cmdline"
    unreadable.mkdir(parents=True)
    clear = tmp_path / "99999992" / "cmdline"
    clear.parent.mkdir()
    clear.write_bytes(b"python\x00-m\x00ray.worker")

    probe = gate_module._build_ray_process_surface_probe(
        ("private-marker",),
        proc_root=tmp_path,
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"clear": True}

    exposed = tmp_path / "99999993" / "cmdline"
    exposed.parent.mkdir()
    exposed.write_bytes(b"python\x00private-marker")
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"clear": False}


def test_missing_reference_gate_restores_manager_and_rejects_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    job_id = f"raysubmit_django_ray_rq2_{'c' * 64}"
    events: list[str] = []

    monkeypatch.setattr(gate, "_register_ray_job_gate_value", lambda _value: None)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale:{replicas}"),
    )
    monkeypatch.setattr(
        gate,
        "_submit_missing_ray_job_request_reference",
        lambda _marker: {
            "task_id": TASK_ID,
            "job_id": job_id,
            "attempt_number": 1,
            "execution_generation": 7,
        },
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_missing_ray_job_failure",
        lambda **_kwargs: {
            "job_id": job_id,
            "carrier_ok": True,
            "binding_ok": True,
            "info_clear": True,
            "logs_clear": True,
            "submission_count": 1,
            "attempt_number": 1,
        },
    )
    monkeypatch.setattr(gate, "_decoded_credential_values", lambda: ())
    monkeypatch.setattr(gate, "_ray_process_surfaces_clear", lambda _values: True)
    monkeypatch.setattr(
        gate,
        "_age_missing_ray_job_execution",
        lambda _task_id: events.append("aged"),
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("topology"),
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_missing_ray_job_disposition",
        lambda _task_id: {
            "state": "FAILED",
            "attempt_number": 1,
            "execution_generation": 7,
            "run_after_is_none": True,
            "completion_is_none": True,
            "result_is_none": True,
            "fixed_error": True,
            "archived_attempts": 1,
            "submission_count": 1,
        },
    )

    gate._verify_missing_ray_job_request_reference()

    assert events == ["scale:0", "aged", "scale:1", "topology"]
    assert gate.evidence.ray_job_missing_reference_no_marker is True
    assert gate.evidence.ray_job_missing_reference_no_retry is True


def test_missing_reference_gate_restores_manager_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    events: list[str] = []

    monkeypatch.setattr(gate, "_register_ray_job_gate_value", lambda _value: None)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale:{replicas}"),
    )
    monkeypatch.setattr(
        gate,
        "_submit_missing_ray_job_request_reference",
        lambda _marker: (_ for _ in ()).throw(RuntimeError("fixture failed")),
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("topology"),
    )

    with pytest.raises(RuntimeError, match="fixture failed"):
        gate._verify_missing_ray_job_request_reference()

    assert events == ["scale:0", "scale:1", "topology"]


def test_setup_log_requires_migrations_static_and_runtime_env_markers() -> None:
    complete = "\n".join(
        (
            "Running migrations...",
            "Collecting static files...",
            "Building shared RuntimeEnv source bundle...",
            "RuntimeEnv bundle ready: /runtime-env/django-ray-source.zip (42 bytes)",
            "Recovery RuntimeEnv bundle ready: /runtime-env/django-ray-recovery.zip (84 bytes)",
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
            "Recovery RuntimeEnv bundle ready:",
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
            "Recovery RuntimeEnv bundle ready:",
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
        "django-ray-worker-ray-job": {
            "app": "django-ray",
            "component": "worker",
            "queues": "ray-data",
            "runner": "ray-job",
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
    recovery_digest = "e" * 64
    payload = json.dumps(
        {
            "django_ray": "absent",
            "bytes": 293_956,
            "sha256": digest,
            "required_member": True,
            "recovery_bytes": 20_000_000,
            "recovery_sha256": recovery_digest,
            "recovery_required_members": True,
        }
    )
    assert parse_runtime_archive_probe(payload) == (
        293_956,
        digest,
        20_000_000,
        recovery_digest,
    )

    installed = payload.replace('"absent"', '"present"')
    with pytest.raises(ValueError, match="unexpectedly has django_ray"):
        parse_runtime_archive_probe(installed)

    missing = json.loads(payload)
    missing["required_member"] = False
    with pytest.raises(ValueError, match=re.escape(RUNTIME_ENV_REQUIRED_MEMBER)):
        parse_runtime_archive_probe(json.dumps(missing))

    incomplete_recovery = json.loads(payload)
    incomplete_recovery["recovery_required_members"] = False
    with pytest.raises(ValueError, match="required package closure"):
        parse_runtime_archive_probe(json.dumps(incomplete_recovery))


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


@pytest.mark.parametrize(
    "mutation", ["extra", "substituted", "script", "missing-auth", "wrong-auth"]
)
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
    elif mutation == "missing-auth":
        init_containers[0].pop("env")
    elif mutation == "wrong-auth":
        init_containers[0]["env"][1]["valueFrom"]["secretKeyRef"]["name"] = "foreign-secret"
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
    desired_cluster = _ray_cluster(uid="cluster-owner", head_sidecar=True)
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], desired_cluster))
    gate.expected_ray_head_count = 1
    gate.expected_ray_worker_count = 1
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
        _ray_pod("ray-head-new", "head", "new-head-uid", head_sidecar=True),
        _ray_pod("ray-worker-new", "worker", "new-worker-uid"),
    ]
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(desired_cluster), "", 0)
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


@pytest.mark.parametrize(
    "mutation",
    ("missing-ray-head", "co-resident-plus-foreign", "full-plus-foreign", "head-init"),
)
def test_restart_discovery_rejects_unknown_co_resident_head_inventory(mutation: str) -> None:
    gate = LocalKubeRayGate(_config())
    desired_cluster = _ray_cluster(uid="cluster-owner", head_sidecar=True)
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], desired_cluster))
    head = _ray_pod(
        "ray-head-old",
        "head",
        "old-head-uid",
        head_sidecar=mutation == "full-plus-foreign",
    )
    spec = cast(dict[str, Any], head["spec"])
    containers = cast(list[dict[str, str]], spec["containers"])
    if mutation == "missing-ray-head":
        containers.clear()
    elif mutation in {"co-resident-plus-foreign", "full-plus-foreign"}:
        containers.append({"name": "foreign-sidecar", "image": "busybox:1.36"})
    else:
        spec["initContainers"] = [{"name": "foreign-init", "image": "busybox:1.36"}]

    with pytest.raises(ValueError, match="supported KubeRay inventory"):
        gate._validate_restart_discovery_pod(head)


def test_restart_discovery_keeps_worker_inventory_exact() -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    worker = _ray_pod("ray-worker-old", "worker", "old-worker-uid")
    spec = cast(dict[str, Any], worker["spec"])
    cast(list[dict[str, str]], spec["containers"]).append(
        {"name": "foreign-sidecar", "image": "busybox:1.36"}
    )

    with pytest.raises(ValueError, match="supported KubeRay inventory"):
        gate._validate_restart_discovery_pod(worker)


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


def test_released_v040_image_build_preserves_source_and_aligns_ray(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    released_context = tmp_path / "released-v040-source"
    released_context.mkdir()
    dockerfile = released_context / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    (released_context / "Dockerfile.dockerignore").write_text("*\n!src/\n", encoding="utf-8")
    gate.released_v040_source_context = released_context
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], _ray_cluster()))
    gate.evidence.released_v040_image_tag = "django-ray-released-v040:test-source"
    gate._ray_image_python_version = "3.12.12"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gate, "_verify_released_v040_source_identity", lambda: None)

    def docker(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        if args[0] == "run":
            return CommandResult("3.12.12\n", "", 0)
        if args[:2] != ("image", "inspect"):
            return CommandResult("", "", 0)
        return CommandResult(
            json.dumps(
                [
                    {
                        "Id": IMAGE_ID,
                        "RepoTags": [gate.evidence.released_v040_image_tag],
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": (
                                    gate_module.RELEASED_V040_COMMIT
                                ),
                                "org.opencontainers.image.source-tree": (
                                    gate_module.RELEASED_V040_SOURCE_TREE
                                ),
                            }
                        },
                    }
                ]
            ),
            "",
            0,
        )

    monkeypatch.setattr(gate, "_docker", docker)

    gate._build_released_v040_image()

    build_call, inspect_call, verify_call = calls
    build_arg_index = build_call.index("--build-arg")
    assert build_call[build_arg_index + 1] == "PYTHON_VERSION=3.12.12"
    assert build_call[:build_arg_index] + build_call[build_arg_index + 2 :] == (
        "build",
        "--tag",
        gate.evidence.released_v040_image_tag,
        "--label",
        f"org.opencontainers.image.revision={gate_module.RELEASED_V040_COMMIT}",
        "--label",
        f"org.opencontainers.image.source-tree={gate_module.RELEASED_V040_SOURCE_TREE}",
        "--file",
        str(tmp_path / "released-v040-handoff.Dockerfile"),
        str(released_context),
    )
    assert dockerfile.read_text(encoding="utf-8") == "FROM scratch\n"
    handoff_dockerfile = (tmp_path / "released-v040-handoff.Dockerfile").read_text(encoding="utf-8")
    assert '--no-cache-dir "ray[default]==2.60.0" && python -m pip check' in handoff_dockerfile
    assert "--no-deps" not in handoff_dockerfile
    assert (tmp_path / "released-v040-handoff.Dockerfile.dockerignore").read_text() == "*\n!src/\n"
    assert inspect_call == ("image", "inspect", gate.evidence.released_v040_image_tag)
    assert verify_call == (
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint",
        "python",
        "--",
        gate.evidence.released_v040_image_tag,
        "-I",
        "-S",
        "-B",
        "-c",
        'import sys; print(".".join(map(str, sys.version_info[:3])))',
    )
    assert gate.evidence.released_v040_image_id == IMAGE_ID


def test_image_builds_discover_one_ray_python_and_align_only_application_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    current_context = tmp_path / "current-source"
    released_context = tmp_path / "released-v040-source"
    current_context.mkdir()
    released_context.mkdir()
    (released_context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (released_context / "Dockerfile.dockerignore").write_text("*\n!src/\n", encoding="utf-8")
    gate.source_context = current_context
    gate.released_v040_source_context = released_context
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], _ray_cluster()))
    gate.evidence.commit = COMMIT
    gate.evidence.source_tree = SOURCE_TREE
    gate.evidence.app_tag = APP_TAG
    gate.evidence.worker_tag = f"django-ray-worker:{TAG}"
    gate.evidence.released_v040_image_tag = f"django-ray-released-v040:{TAG}"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gate, "_verify_source_identity", lambda: None)
    monkeypatch.setattr(gate, "_verify_released_v040_source_identity", lambda: None)

    def docker(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        if args[0] == "run":
            return CommandResult("3.12.12\n", "", 0)
        if args[:2] != ("image", "inspect"):
            return CommandResult("", "", 0)
        tag = args[2]
        if tag == gate.evidence.released_v040_image_tag:
            commit = gate_module.RELEASED_V040_COMMIT
            source_tree = gate_module.RELEASED_V040_SOURCE_TREE
        else:
            commit = COMMIT
            source_tree = SOURCE_TREE
        return CommandResult(
            json.dumps(
                [
                    {
                        "Id": IMAGE_ID,
                        "RepoTags": [tag],
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": commit,
                                "org.opencontainers.image.source-tree": source_tree,
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

    discovery = (
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint",
        "python",
        "--",
        "rayproject/ray:2.60.0-py312",
        "-I",
        "-S",
        "-B",
        "-c",
        'import sys; print(".".join(map(str, sys.version_info[:3])))',
    )
    assert calls.count(discovery) == 1
    assert calls.index(discovery) < next(
        index for index, call in enumerate(calls) if call[0] == "build"
    )
    assert gate._ray_image_python_version == "3.12.12"

    python_probes = [call for call in calls if call[0] == "run"]
    assert len(python_probes) == 3
    assert {call[call.index("--") + 1] for call in python_probes} == {
        "rayproject/ray:2.60.0-py312",
        gate.evidence.app_tag,
        gate.evidence.released_v040_image_tag,
    }

    builds = [call for call in calls if call[0] == "build"]
    assert len(builds) == 3
    application_builds = [
        call for call in builds if Path(call[call.index("--file") + 1]).name != "Dockerfile.ray"
    ]
    assert len(application_builds) == 2
    assert {Path(call[call.index("--file") + 1]) for call in application_builds} == {
        current_context / "Dockerfile",
        released_context.parent / "released-v040-handoff.Dockerfile",
    }
    for call in application_builds:
        assert call.count("--build-arg") == 1
        build_arg_index = call.index("--build-arg")
        assert call[build_arg_index + 1] == "PYTHON_VERSION=3.12.12"

    worker_build = next(
        call for call in builds if Path(call[call.index("--file") + 1]).name == "Dockerfile.ray"
    )
    assert "--build-arg" not in worker_build
    assert all(not value.startswith("PYTHON_VERSION=") for value in worker_build)
