"""Unit tests for local KubeRay gate released-baseline startup recovery."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_gate import (
    APP_DEPLOYMENTS,
    EXPECTED_NAMESPACE,
    CommandResult,
    LocalKubeRayGate,
)
from tests.unit._local_kuberay_gate_helpers import (
    IMAGE_ID,
    PRIOR_RELEASED_V040_HOSTNAME,
    PRIOR_RELEASED_V040_TAG,
    PRIOR_RELEASED_V040_WORKER_ID,
    TAG,
    TASK_ID,
    _config,
)


def test_released_v040_manager_manifest_is_ephemeral_exact_and_protocol_honest() -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.released_v040_image_tag = (
        f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:test-source"
    )

    manifest = gate._released_v040_manager_manifest()

    assert gate_module.RELEASED_V040_MANAGER_DEPLOYMENT not in APP_DEPLOYMENTS
    assert manifest["apiVersion"] == "apps/v1"
    assert manifest["kind"] == "Deployment"
    assert manifest["metadata"] == {
        "name": gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        "namespace": EXPECTED_NAMESPACE,
        "labels": {
            "app": "django-ray",
            "component": "worker",
            "queues": gate_module.RAY_JOB_GATE_QUEUE,
            "runner": "ray-job-v040",
        },
    }
    spec = manifest["spec"]
    assert spec["replicas"] == 1
    assert spec["selector"]["matchLabels"] == spec["template"]["metadata"]["labels"]
    pod_spec = spec["template"]["spec"]
    container = pod_spec["containers"][0]
    assert container["name"] == gate_module.RELEASED_V040_MANAGER_CONTAINER
    assert container["image"] == gate.evidence.released_v040_image_tag
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["command"] == [
        "python",
        "testproject/manage.py",
        "django_ray_worker",
    ]
    assert container["args"] == [
        "--queue",
        gate_module.RAY_JOB_GATE_QUEUE,
        "--concurrency",
        "1",
    ]
    assert container["envFrom"] == [
        {"configMapRef": {"name": "django-ray-config"}},
    ]
    assert {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry} == {
        "RAY_ADDRESS": "ray://ray-head-svc:10001",
        "RAY_AUTH_MODE": "token",
        "DJANGO_API_ENABLED": "false",
        "RAY_MAX_RETRIES": "1",
        **gate_module.RUNTIME_ENV_ENCRYPTION_ENV,
    }
    references = {
        entry["name"]: entry["valueFrom"]["secretKeyRef"]
        for entry in container["env"]
        if "valueFrom" in entry
    }
    assert set(references) == {
        "DJANGO_SECRET_KEY",
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "RAY_AUTH_TOKEN",
    }
    assert references["RAY_AUTH_TOKEN"]["name"] == "django-ray-auth"
    assert container["readinessProbe"]["successThreshold"] == 1
    assert container["livenessProbe"]["successThreshold"] == 1
    serialized = json.dumps(manifest, sort_keys=True)
    assert "min_supported_execution_protocol_version" not in serialized
    assert "max_supported_execution_protocol_version" not in serialized
    assert "capability_schema_version" not in serialized


def test_released_v040_manager_create_and_delete_are_exact_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.evidence.released_v040_image_tag = PRIOR_RELEASED_V040_TAG
    gate.evidence.released_v040_image_id = IMAGE_ID
    manifest = gate._released_v040_manager_manifest()
    deployment = json.loads(json.dumps(manifest))
    deployment["status"] = {"readyReplicas": 1}
    pod_spec = deployment["spec"]["template"]["spec"]
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "django-ray-worker-ray-job-v040-test",
            "namespace": EXPECTED_NAMESPACE,
            "uid": "released-v040-pod-uid",
        },
        "spec": pod_spec,
        "status": {
            "initContainerStatuses": [
                {
                    "name": entry["name"],
                    "image": entry["image"],
                    "imageID": f"sha256:{'c' * 64}",
                }
                for entry in pod_spec["initContainers"]
            ],
            "containerStatuses": [
                {
                    "name": gate_module.RELEASED_V040_MANAGER_CONTAINER,
                    "image": gate.evidence.released_v040_image_tag,
                    "imageID": IMAGE_ID,
                    "ready": True,
                }
            ],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == (
            "get",
            "deployment",
            gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        ):
            if args[-2:] == ("-o", "json"):
                return CommandResult(json.dumps(deployment), "", 0)
            return CommandResult("", "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": [pod]}), "", 0)
        if args[:2] == ("get", "pods,replicasets"):
            return CommandResult(json.dumps({"items": []}), "", 0)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(_released_v040_image_inspect(), "", 0),
    )

    gate._apply_released_v040_manager()
    gate._delete_released_v040_manager()

    manifest_path = tmp_path / "released-v040-manager.yaml"
    assert yaml.safe_load(manifest_path.read_text(encoding="utf-8")) == manifest
    assert calls[0] == ("create", "-f", str(manifest_path))
    assert calls[1][:3] == (
        "rollout",
        "status",
        f"deployment/{gate_module.RELEASED_V040_MANAGER_DEPLOYMENT}",
    )
    assert calls[-3][:3] == (
        "delete",
        "deployment",
        gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
    )
    assert "--ignore-not-found=true" in calls[-3]
    assert "--wait=true" in calls[-3]
    assert calls[-2] == (
        "get",
        "deployment",
        gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        "--ignore-not-found",
        "-o",
        "name",
    )
    assert calls[-1][:2] == ("get", "pods,replicasets")
    assert calls[-1][-2:] == ("-o", "json")


def _released_v040_recovery_deployment(gate: LocalKubeRayGate) -> dict[str, Any]:
    return gate._released_v040_manager_manifest(
        image_tag=PRIOR_RELEASED_V040_TAG,
        hostname=PRIOR_RELEASED_V040_HOSTNAME,
    )


def _released_v040_image_inspect(
    *,
    commit: str = gate_module.RELEASED_V040_COMMIT,
    source_tree: str = gate_module.RELEASED_V040_SOURCE_TREE,
) -> str:
    return json.dumps(
        [
            {
                "Id": IMAGE_ID,
                "RepoTags": [PRIOR_RELEASED_V040_TAG],
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.revision": commit,
                        "org.opencontainers.image.source-tree": source_tree,
                    }
                },
            }
        ]
    )


def test_released_v040_predelete_accepts_live_defaulted_probe_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    deployment = _released_v040_recovery_deployment(gate)
    main = deployment["spec"]["template"]["spec"]["containers"][0]
    main["readinessProbe"]["successThreshold"] = 1
    main["livenessProbe"]["successThreshold"] = 1
    assert main["readinessProbe"]["successThreshold"] == 1
    assert main["livenessProbe"]["successThreshold"] == 1
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == (
            "get",
            "deployment",
            gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        ):
            if args[-2:] == ("-o", "json"):
                return CommandResult(json.dumps(deployment), "", 0)
            return CommandResult("", "", 0)
        if args[:2] == ("get", "pods,replicasets"):
            return CommandResult(json.dumps({"items": []}), "", 0)
        return CommandResult("", "", 0)

    gate._released_v040_hostname = PRIOR_RELEASED_V040_HOSTNAME
    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(_released_v040_image_inspect(), "", 0),
    )

    gate._delete_released_v040_manager()

    assert calls[0][-2:] == ("-o", "json")
    assert calls[1][:3] == (
        "delete",
        "deployment",
        gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
    )
    assert sum(call[0] == "delete" for call in calls) == 1


def _released_v040_recovery_lease(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "worker_id": PRIOR_RELEASED_V040_WORKER_ID,
        "hostname": PRIOR_RELEASED_V040_HOSTNAME,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "capability_schema_version": 0,
        "django_ray_version": None,
        "min_supported_execution_protocol_version": None,
        "max_supported_execution_protocol_version": None,
        "is_active": False,
        "last_heartbeat_at": "2026-08-13T00:00:00+00:00",
        "stopped_at": "2026-08-13T00:01:00+00:00",
        "heartbeat_live": False,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("foreign_after_create_failure", [False, True])
def test_released_v040_ordinary_finally_never_deletes_unowned_same_name_after_create_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    foreign_after_create_failure: bool,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.evidence.released_v040_image_tag = (
        f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:released-v040-{TAG}"
    )
    gate.evidence.released_v040_image_id = IMAGE_ID
    foreign_deployment = _released_v040_recovery_deployment(gate)
    create_failed = False
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **_kwargs: object) -> CommandResult:
        nonlocal create_failed
        calls.append(args)
        if args[:3] == (
            "get",
            "deployment",
            gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        ):
            if args[-2:] == ("-o", "json"):
                payload = (
                    json.dumps(foreign_deployment)
                    if create_failed and foreign_after_create_failure
                    else ""
                )
                return CommandResult(payload, "", 0)
            return CommandResult("", "", 0)
        if args[:2] == ("get", "pods,replicasets"):
            return CommandResult(json.dumps({"items": []}), "", 0)
        if args[0] == "create":
            create_failed = True
            raise RuntimeError("released manager create failed")
        if args[0] == "delete":
            pytest.fail("ordinary cleanup must not delete a Deployment it did not create")
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        gate,
        "_observe_released_v040_reserved_leases",
        lambda: {"count": 0, "rows": []},
    )
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(
            _released_v040_image_inspect(commit="f" * 40),
            "",
            0,
        ),
    )

    gate._recover_released_v040_startup_residue()
    with pytest.raises(RuntimeError, match="released manager create failed"):
        gate._apply_released_v040_manager()

    if foreign_after_create_failure:
        with pytest.raises(ValueError):
            gate._delete_released_v040_manager()
    else:
        gate._delete_released_v040_manager()

    assert sum(call[0] == "create" for call in calls) == 1
    assert not any(call[0] == "delete" for call in calls)


def test_released_v040_startup_recovery_accepts_only_a_pinned_prior_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.released_v040_image_tag = (
        f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:released-v040-{TAG}"
    )
    assert gate.evidence.released_v040_image_tag != PRIOR_RELEASED_V040_TAG
    deployment = _released_v040_recovery_deployment(gate)
    docker_calls: list[tuple[str, ...]] = []
    cleanup_events: list[str] = []

    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult(json.dumps(deployment), "", 0),
    )

    def docker(*args: str, **_kwargs: object) -> CommandResult:
        docker_calls.append(args)
        return CommandResult(_released_v040_image_inspect(), "", 0)

    monkeypatch.setattr(gate, "_docker", docker)
    monkeypatch.setattr(
        gate,
        "_observe_released_v040_reserved_leases",
        lambda: {"count": 0, "rows": []},
    )

    def delete_manager() -> None:
        assert gate._released_v040_hostname == PRIOR_RELEASED_V040_HOSTNAME
        cleanup_events.append("deployment")

    def delete_lease() -> None:
        assert gate._released_v040_hostname == PRIOR_RELEASED_V040_HOSTNAME
        gate._released_v040_worker_id = PRIOR_RELEASED_V040_WORKER_ID
        cleanup_events.append("lease")

    monkeypatch.setattr(gate, "_delete_released_v040_manager", delete_manager)
    monkeypatch.setattr(gate, "_delete_released_v040_worker_lease", delete_lease)

    gate._recover_released_v040_startup_residue()

    assert docker_calls == [("image", "inspect", PRIOR_RELEASED_V040_TAG)]
    assert cleanup_events == ["deployment", "lease"]
    assert gate._released_v040_hostname is None
    assert gate._released_v040_worker_id is None


@pytest.mark.parametrize(
    "residue",
    [
        {"count": 2, "rows": [_released_v040_recovery_lease()] * 2},
        {
            "count": 1,
            "rows": [_released_v040_recovery_lease(hostname=f"dr-v040-{'9' * 32}")],
        },
    ],
)
def test_released_v040_prior_deployment_refuses_ambiguous_or_foreign_lease_without_deletion(
    monkeypatch: pytest.MonkeyPatch,
    residue: Mapping[str, Any],
) -> None:
    gate = LocalKubeRayGate(_config())
    deployment = _released_v040_recovery_deployment(gate)
    cleanup_events: list[str] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult(json.dumps(deployment), "", 0),
    )
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(_released_v040_image_inspect(), "", 0),
    )
    monkeypatch.setattr(gate, "_observe_released_v040_reserved_leases", lambda: residue)
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: cleanup_events.append("deployment"),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_worker_lease",
        lambda: cleanup_events.append("lease"),
    )

    with pytest.raises(ValueError, match="ambiguous|foreign"):
        gate._recover_released_v040_startup_residue()

    assert cleanup_events == []


@pytest.mark.parametrize(
    "drift",
    ["image-tag", "docker-revision", "docker-tree", "container-contract"],
)
def test_released_v040_startup_recovery_refuses_foreign_deployment_without_deletion(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    deployment = _released_v040_recovery_deployment(gate)
    inspect_payload = _released_v040_image_inspect()
    if drift == "image-tag":
        deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "foreign.example/django-ray:v0.4.0"
        )
    elif drift == "docker-revision":
        inspect_payload = _released_v040_image_inspect(commit="f" * 40)
    elif drift == "docker-tree":
        inspect_payload = _released_v040_image_inspect(source_tree="f" * 40)
    else:
        deployment["spec"]["template"]["spec"]["containers"][0]["args"][-1] = "2"
    cleanup_events: list[str] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult(json.dumps(deployment), "", 0),
    )
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(inspect_payload, "", 0),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: cleanup_events.append("deployment"),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_worker_lease",
        lambda: cleanup_events.append("lease"),
    )

    with pytest.raises(ValueError):
        gate._recover_released_v040_startup_residue()

    assert cleanup_events == []


def test_released_v040_reserved_lease_observer_is_prefix_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    payload = {"count": 1, "rows": [_released_v040_recovery_lease()]}
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<released-v040-reserved-leases>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return payload

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._observe_released_v040_reserved_leases() == payload
    assert observed["field_name"] == "released v0.4.0 reserved lease recovery observation"
    assert 'hostname__startswith="dr-v040-"' in observed["script"]
    assert '"hostname", "worker_id"' in observed["script"]
    assert ")[:2]" in observed["script"]
    assert "get_lease_duration" in observed["script"]
    assert 'row["heartbeat_live"]' in observed["script"]


def test_released_v040_startup_recovery_accepts_one_expired_exact_lease_and_resets_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    cleanup_events: list[str] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult("", "", 0),
    )
    monkeypatch.setattr(
        gate,
        "_observe_released_v040_reserved_leases",
        lambda: {"count": 1, "rows": [_released_v040_recovery_lease()]},
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: cleanup_events.append("deployment"),
    )

    def delete_lease() -> None:
        assert gate._released_v040_hostname == PRIOR_RELEASED_V040_HOSTNAME
        assert gate._released_v040_worker_id == PRIOR_RELEASED_V040_WORKER_ID
        cleanup_events.append("lease")

    monkeypatch.setattr(gate, "_delete_released_v040_worker_lease", delete_lease)

    gate._recover_released_v040_startup_residue()

    assert cleanup_events == ["deployment", "lease"]
    assert gate._released_v040_hostname is None
    assert gate._released_v040_worker_id is None

    next_hostname_uuid = gate_module.UUID("21234567-89ab-4cde-8fab-0123456789ab")
    gate.temp_root = tmp_path
    gate.evidence.released_v040_image_tag = (
        f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:released-v040-{TAG}"
    )
    gate.evidence.released_v040_image_id = IMAGE_ID
    monkeypatch.setattr(gate_module, "uuid4", lambda: next_hostname_uuid)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("later apply failed")),
    )

    with pytest.raises(RuntimeError, match="later apply failed"):
        gate._apply_released_v040_manager()

    assert gate._released_v040_hostname == f"dr-v040-{next_hostname_uuid.hex}"
    assert gate._released_v040_hostname != PRIOR_RELEASED_V040_HOSTNAME
    assert gate._released_v040_worker_id is None


@pytest.mark.parametrize(
    ("residue", "message"),
    [
        (
            {"count": 1, "rows": [_released_v040_recovery_lease(heartbeat_live=True)]},
            "live or foreign",
        ),
        (
            {
                "count": 2,
                "rows": [
                    _released_v040_recovery_lease(),
                    _released_v040_recovery_lease(
                        worker_id="31234567-89ab-4cde-8fab-0123456789ab",
                        hostname=f"dr-v040-{'3' * 32}",
                    ),
                ],
            },
            "ambiguous",
        ),
        (
            {
                "count": 1,
                "rows": [_released_v040_recovery_lease(queue_name="foreign-queue")],
            },
            "live or foreign",
        ),
        (
            {
                "count": 1,
                "rows": [_released_v040_recovery_lease(capability_schema_version=1)],
            },
            "live or foreign",
        ),
        (
            {
                "count": 1,
                "rows": [_released_v040_recovery_lease(django_ray_version="0.4.0")],
            },
            "live or foreign",
        ),
    ],
)
def test_released_v040_startup_recovery_refuses_live_ambiguous_or_foreign_leases(
    monkeypatch: pytest.MonkeyPatch,
    residue: Mapping[str, Any],
    message: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    cleanup_events: list[str] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult("", "", 0),
    )
    monkeypatch.setattr(gate, "_observe_released_v040_reserved_leases", lambda: residue)
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: cleanup_events.append("deployment"),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_worker_lease",
        lambda: cleanup_events.append("lease"),
    )

    with pytest.raises(ValueError, match=message):
        gate._recover_released_v040_startup_residue()

    assert cleanup_events == []
    assert gate._released_v040_hostname is None
    assert gate._released_v040_worker_id is None


def test_protocol_cohort_observer_reads_real_legacy_and_explicit_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    observed: dict[str, str] = {}
    payload = {
        "report": {
            "capabilities": {
                "groups": [
                    {
                        "kind": "legacy",
                        "minimum": 1,
                        "maximum": 1,
                        "heartbeat_live_leases": 1,
                    },
                    {
                        "kind": "explicit",
                        "minimum": 1,
                        "maximum": 1,
                        "heartbeat_live_leases": 1,
                    },
                ],
                "total_groups": 2,
                "total_leases": 2,
                "omitted_groups": 0,
                "omitted_leases": 0,
            },
        },
        "legacy_worker_count": 1,
        "legacy_worker_ids": ["released-v040-worker"],
    }

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-cohort-observation>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return payload

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._observe_protocol_cohorts() == payload
    assert observed["field_name"] == "protocol cohort observation"
    assert "build_protocol_status" in observed["script"]
    assert "capability_schema_version=0" in observed["script"]
    assert "last_heartbeat_at__gte=cutoff" in observed["script"]


@pytest.mark.parametrize("job_state", [None, "RUNNING", "FAILED"])
def test_protocol_handoff_observer_waits_for_reserved_job_visibility(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], job_state: str | None
) -> None:
    from django_ray.models import RayTaskExecution, TaskAttempt
    from django_ray.runner import ray_job

    row = SimpleNamespace(
        ray_job_id="reserved-job",
        ray_address="http://ray-head:8265",
        state="RUNNING",
        attempt_number=1,
        execution_generation=1,
        claimed_by_worker="released-worker",
        ray_job_request_reference=None,
        completion_data=None,
        result_data=None,
        result_reference=None,
        run_after=None,
    )
    jobs = (
        []
        if job_state is None
        else [
            SimpleNamespace(
                submission_id=row.ray_job_id,
                status=job_state,
                entrypoint="python -m django_ray.runtime.entrypoint --payload-b64 opaque",
            )
        ]
    )
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **_kwargs: row)
    monkeypatch.setattr(
        TaskAttempt.objects, "filter", lambda **_kwargs: SimpleNamespace(values=lambda *_args: [])
    )
    monkeypatch.setattr(
        ray_job,
        "_address_pinned_job_client",
        lambda _address: SimpleNamespace(list_jobs=lambda: jobs, get_job_logs=lambda _job_id: ""),
    )
    gate = LocalKubeRayGate(_config())

    def observe(script: str, **_kwargs: object) -> Mapping[str, Any]:
        exec(compile(script, "<handoff-observer>", "exec"), {})
        return json.loads(capsys.readouterr().out)

    monkeypatch.setattr(gate, "_sensitive_django_shell", observe)
    result = gate._observe_protocol_handoff_task(TASK_ID)
    assert result["ready"] is (job_state is not None)
    if job_state is not None:
        assert result["state"] == job_state
        assert result["submission_count"] == 1
        assert result["released_carrier"] is True
    if job_state == "FAILED":
        with pytest.raises(ValueError, match="unexpected state FAILED"):
            gate._wait_for_protocol_handoff_task(TASK_ID, accepted_states=frozenset({"RUNNING"}))


def test_protocol_handoff_wait_rejects_failure_before_job_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate, "_observe_protocol_handoff_task", lambda _task_id: {"ready": False, "state": "FAILED"}
    )
    with pytest.raises(ValueError, match="handoff task reached unexpected state FAILED"):
        gate._wait_for_protocol_handoff_task(TASK_ID, accepted_states=frozenset({"RUNNING"}))


def test_protocol_handoff_wait_requires_a_replacement_worker_without_resubmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    job_id = f"raysubmit_django_ray_v1_{'a' * 64}"
    observations = iter(
        (
            {
                "ready": True,
                "state": "RUNNING",
                "durable_state": "RUNNING",
                "attempt_number": 1,
                "execution_generation": 4,
                "worker_id": "released-v040-worker",
                "job_id": job_id,
                "released_carrier": True,
                "request_reference_absent": True,
                "submission_count": 1,
            },
            {
                "ready": True,
                "state": "RUNNING",
                "durable_state": "RUNNING",
                "attempt_number": 1,
                "execution_generation": 4,
                "worker_id": "current-explicit-worker",
                "job_id": job_id,
                "released_carrier": True,
                "request_reference_absent": True,
                "submission_count": 1,
            },
        )
    )
    monkeypatch.setattr(
        gate,
        "_observe_protocol_handoff_task",
        lambda _task_id: next(observations),
    )
    monkeypatch.setattr(gate_module.time, "sleep", lambda _seconds: None)

    result = gate._wait_for_protocol_handoff_task(
        TASK_ID,
        accepted_states=frozenset({"RUNNING"}),
        different_worker="released-v040-worker",
    )

    assert result["worker_id"] == "current-explicit-worker"
    assert result["job_id"] == job_id
    assert result["attempt_number"] == 1
    assert result["execution_generation"] == 4
    assert result["submission_count"] == 1
