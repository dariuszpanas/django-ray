from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runtime import compiled_graph
from scripts import kuberay_compiled_graph_pilot as pilot


def test_structured_command_output_preserves_json_with_a_live_hard_bound() -> None:
    result = pilot._run_command(
        [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'items': [], 'padding': 'x' * 40000}))",
        ],
        preserve_stdout=True,
    )

    assert json.loads(result.stdout)["items"] == []
    assert len(result.stdout) > pilot.MAX_CAPTURE_CHARS

    started = time.monotonic()
    with pytest.raises(pilot.PilotError, match="exceeded the 1 MiB"):
        pilot._run_command(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time; "
                    f"sys.stdout.buffer.write(b'x' * {pilot.MAX_STRUCTURED_CAPTURE_BYTES + 65_536}); "
                    "sys.stdout.flush(); time.sleep(10)"
                ),
            ],
            preserve_stdout=True,
            timeout_seconds=10,
        )
    assert time.monotonic() - started < 5


def test_command_timeout_and_unstructured_streams_stay_bounded() -> None:
    result = pilot._run_command(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.write('o' * 100000 + 'stdout-end'); "
                "sys.stderr.write('e' * 100000 + 'stderr-end')"
            ),
        ]
    )

    assert result.stdout.endswith("stdout-end")
    assert result.stderr.endswith("stderr-end")
    assert len(result.stdout.encode("utf-8")) <= pilot.MAX_CAPTURE_CHARS
    assert len(result.stderr.encode("utf-8")) <= pilot.MAX_CAPTURE_CHARS

    with pytest.raises(pilot.PilotError, match="command timed out after 0.1s"):
        pilot._run_command(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=0.1,
        )


def test_command_timeout_terminates_descendant_with_inherited_pipes() -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
            "time.sleep(10)"
        ),
    ]
    started = time.monotonic()

    with pytest.raises(pilot.PilotError, match="command timed out after 0.1s"):
        pilot._run_command(command, timeout_seconds=0.1)

    assert time.monotonic() - started < pilot.COMMAND_SHUTDOWN_TIMEOUT_SECONDS + 2


def test_structured_overflow_terminates_descendant_with_inherited_pipes() -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
            f"sys.stdout.buffer.write(b'x' * {pilot.MAX_STRUCTURED_CAPTURE_BYTES + 65_536}); "
            "sys.stdout.flush(); time.sleep(10)"
        ),
    ]
    started = time.monotonic()

    with pytest.raises(pilot.PilotError, match="exceeded the 1 MiB"):
        pilot._run_command(command, preserve_stdout=True, timeout_seconds=10)

    assert time.monotonic() - started < pilot.COMMAND_SHUTDOWN_TIMEOUT_SECONDS + 2


def test_command_preserves_input_and_nonzero_exit_semantics() -> None:
    echoed = pilot._run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        input_text="bounded-input",
    )
    assert echoed == pilot.CommandResult("bounded-input", "", 0)

    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('out'); sys.stderr.write('err'); raise SystemExit(7)",
    ]
    unchecked = pilot._run_command(command, check=False)
    assert unchecked == pilot.CommandResult("out", "err", 7)
    with pytest.raises(pilot.PilotError, match=r"command failed \(7\)"):
        pilot._run_command(command)


def test_kubectl_exec_json_parses_one_complete_large_document_and_rejects_multiple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    stdout = json.dumps({"status": "success", "padding": "x" * 40_000})

    def fake_run(_command: list[str], **kwargs: Any) -> pilot.CommandResult:
        calls.append(kwargs)
        return pilot.CommandResult(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(pilot, "_run_command", fake_run)
    monkeypatch.setattr(pilot, "_assert_current_namespace_lease", lambda *_args: {})
    monkeypatch.setattr(pilot, "_assert_current_raycluster_lease", lambda *_args: {})
    namespace_lease = _namespace_lease()
    raycluster_lease = _raycluster_lease(namespace_lease)

    result = pilot._kubectl_exec_json(
        "docker-desktop",
        namespace_lease,
        raycluster_lease,
        "head",
        ["inspect-cluster-state"],
        timeout_seconds=60,
    )

    assert result["status"] == "success"
    assert len(result["padding"]) == 40_000
    assert calls == [{"timeout_seconds": 60, "preserve_stdout": True}]

    stdout = '{"status":"success"}\n{"status":"success"}'
    with pytest.raises(pilot.PilotError, match="invalid or multiple JSON documents"):
        pilot._kubectl_exec_json(
            "docker-desktop",
            namespace_lease,
            raycluster_lease,
            "head",
            ["inspect-cluster-state"],
            timeout_seconds=60,
        )

    for invalid_stdout in ('{"status":"first","status":"second"}', '{"value":NaN}'):
        stdout = invalid_stdout
        with pytest.raises(pilot.PilotError, match="invalid or multiple JSON documents"):
            pilot._kubectl_exec_json(
                "docker-desktop",
                namespace_lease,
                raycluster_lease,
                "head",
                ["inspect-cluster-state"],
                timeout_seconds=60,
            )


def test_structured_command_rejects_non_utf8_output() -> None:
    with pytest.raises(pilot.PilotError, match="non-UTF-8 output"):
        pilot._run_command(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"],
            preserve_stdout=True,
        )


def _namespace_lease(*, uid: str = "pilot-namespace-uid") -> pilot.NamespaceLease:
    return pilot.NamespaceLease(
        name=pilot.PILOT_NAMESPACE,
        uid=uid,
        run_token="1" * 32,
    )


def _raycluster_lease(
    namespace_lease: pilot.NamespaceLease | None = None,
    *,
    uid: str = "pilot-raycluster-uid",
) -> pilot.RayClusterLease:
    namespace_lease = namespace_lease or _namespace_lease()
    return pilot.RayClusterLease(
        name=pilot.RAYCLUSTER_NAME,
        uid=uid,
        namespace_uid=namespace_lease.uid,
        run_token=namespace_lease.run_token,
    )


def _verify_test_pod_images(
    pods: list[dict[str, Any]],
    image: str,
    image_id: str,
    configuration_id: str,
    node_name: str,
) -> list[dict[str, Any]]:
    namespace_lease = _namespace_lease()
    return pilot._verify_pod_images(
        pods,
        image,
        image_id,
        configuration_id,
        node_name,
        namespace_lease,
        _raycluster_lease(namespace_lease),
    )


def _pod(
    name: str,
    role: str,
    *,
    image: str,
    image_id: str,
    configuration_id: str | None = None,
    restarts: int = 0,
    uid: str | None = None,
    container_id: str | None = None,
) -> dict[str, Any]:
    is_head = role == "head"
    identity = pilot.sha256(name.encode("utf-8")).hexdigest()
    configuration_id = configuration_id or pilot._configuration_identity()
    profile = pilot._load_profile()
    object_store = profile["cluster"]["object_store_bytes_per_pod"]
    shared_memory = profile["cluster"]["shared_memory_bytes_per_pod"]
    role_profile = profile["cluster"]["head" if is_head else "workers"]
    namespace_lease = _namespace_lease()
    raycluster_lease = _raycluster_lease(namespace_lease)
    ray_start_arguments = " ".join(
        [
            "ray",
            "start",
            *(
                (
                    f"--{option}"
                    if parameter["kind"] == "valueless-true-switch"
                    else f"--{option}={parameter['value']}"
                )
                for option, parameter in role_profile["ray_start_parameters"].items()
            ),
        ]
    )
    init_identity = pilot.sha256(f"{name}:wait-gcs-ready".encode()).hexdigest()
    init_containers = [] if is_head else [{"name": "wait-gcs-ready", "image": image}]
    init_statuses = (
        []
        if is_head
        else [
            {
                "name": "wait-gcs-ready",
                "containerID": f"docker://{init_identity}",
                "image": image,
                "imageID": f"docker://{image_id}",
                "ready": True,
                "restartCount": 0,
                "state": {
                    "terminated": {
                        "exitCode": 0,
                        "reason": "Completed",
                    }
                },
            }
        ]
    )
    return {
        "metadata": {
            "name": name,
            "uid": uid or f"uid-{identity}",
            "namespace": namespace_lease.name,
            "labels": {
                "ray.io/node-type": role,
                "ray.io/cluster": raycluster_lease.name,
                pilot.PILOT_PROFILE_LABEL_KEY: pilot.PROFILE_NAME,
                pilot.PILOT_RUN_LABEL_KEY: namespace_lease.run_token,
            },
            "annotations": {
                pilot.PILOT_NAMESPACE_UID_ANNOTATION_KEY: namespace_lease.uid,
            },
            "ownerReferences": [
                {
                    "apiVersion": "ray.io/v1",
                    "kind": "RayCluster",
                    "name": raycluster_lease.name,
                    "uid": raycluster_lease.uid,
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
        "spec": {
            "nodeName": "docker-desktop",
            "nodeSelector": {
                "kubernetes.io/os": "linux",
                "kubernetes.io/arch": "amd64",
                "kubernetes.io/hostname": "docker-desktop",
            },
            "containers": [
                {
                    "name": f"ray-{role}",
                    "image": image,
                    "env": [
                        {
                            "name": "DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE",
                            "value": f"{image}@{image_id}",
                        },
                        {
                            "name": "DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE",
                            "value": configuration_id,
                        },
                        {
                            "name": "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE",
                            "value": f"tmpfs:/dev/shm:size={shared_memory}",
                        },
                        {
                            "name": "DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE",
                            "value": f"plasma:{object_store}",
                        },
                        {"name": "DJANGO_RAY_PILOT_IMAGE_ID", "value": image_id},
                        {"name": "DJANGO_RAY_PILOT_CONFIG_ID", "value": configuration_id},
                        {
                            "name": "DJANGO_RAY_PILOT_KUBERAY_VERSION",
                            "value": profile["kuberay"]["operator_version"],
                        },
                        {
                            "name": "DJANGO_RAY_PILOT_NAMESPACE_UID",
                            "value": namespace_lease.uid,
                        },
                        {
                            "name": "DJANGO_RAY_PILOT_RUN_TOKEN",
                            "value": namespace_lease.run_token,
                        },
                    ],
                    "args": [ray_start_arguments],
                    "resources": {
                        "requests": {
                            "cpu": "250m" if is_head else "500m",
                            "memory": "1Gi",
                        },
                        "limits": {"cpu": "1", "memory": "2Gi"},
                    },
                    "volumeMounts": [{"name": "shared-memory", "mountPath": "/dev/shm"}],
                }
            ],
            "initContainers": init_containers,
            "volumes": [
                {
                    "name": "shared-memory",
                    "emptyDir": {"medium": "Memory", "sizeLimit": "512Mi"},
                }
            ],
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": f"ray-{role}",
                    "containerID": container_id or f"docker://{identity}",
                    "image": image,
                    "imageID": f"docker://{image_id}",
                    "ready": True,
                    "restartCount": restarts,
                    "state": {"running": {"startedAt": "2026-07-21T12:00:00Z"}},
                }
            ],
            "initContainerStatuses": init_statuses,
        },
    }


def _runtime_snapshot(
    entries: list[tuple[str, int]],
    *,
    available_bytes: int = 536_862_720,
    child_process_count: int = 0,
) -> dict[str, Any]:
    profile = pilot._load_profile()
    return {
        "schema_version": pilot.PILOT_SCHEMA_VERSION,
        "status": "success",
        "shared_memory": {
            "total_bytes": 536_870_912,
            "available_bytes": available_bytes,
            **pilot._shared_memory_entry_summary(entries),
        },
        "pilot_child_process_count": child_process_count,
        "pilot_child_processes": [],
        "runtime": {
            "kernel": profile["runtime_expectations"]["kernel_release"],
            "machine": profile["runtime_expectations"]["architecture"],
            "source_revision": "a" * 40,
            "image_id": f"sha256:{'b' * 64}",
            "configuration_id": pilot._configuration_identity(),
        },
    }


def _cleanup_observations(
    before: dict[str, dict[str, Any]],
    snapshots: list[dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    cumulative_waits = [0, 5, 20, 50]
    observations = []
    for attempt, (cumulative_wait, pods) in enumerate(
        zip(cumulative_waits, snapshots, strict=True),
        start=1,
    ):
        observations.append(
            {
                "attempt": attempt,
                "wait_before_seconds": pilot.CLEANUP_RETRY_DELAYS_SECONDS[attempt - 1],
                "cumulative_wait_seconds": cumulative_wait,
                "pods": pods,
                "assessment": pilot._assess_runtime_cleanup(before, pods),
            }
        )
    return observations


def _actor_observations(prefix: str, count: int) -> list[dict[str, Any]]:
    return [
        {
            "name": f"cgraph-{prefix}-{index}",
            "actor_id": f"{index + 1:032x}",
            "state": "DEAD",
        }
        for index in range(count)
    ]


def _valid_native_suite() -> dict[str, Any]:
    return {
        "normal": {
            "compile_seconds": 0.25,
            "invocations": [
                {
                    "index": index,
                    "value": index + 2,
                    "trace": [
                        {"stage": "left", "invocation": index + 1},
                        {"stage": "right", "invocation": index + 1},
                    ],
                }
                for index in range(3)
            ],
            "ordered_results_consumed": True,
            "results_submitted": 3,
            "results_consumed": 3,
            "results_discarded_by_teardown": 0,
            "teardown_returned": True,
            "actor_state_observations": _actor_observations("normal", 2),
            "actors_terminated": True,
            "max_inflight_executions": 1,
            "max_buffered_results": 1,
        },
        "application_exception": {
            "error_type": "RayTaskError",
            "marker_preserved": True,
            "result_consumed": True,
            "results_submitted": 1,
            "results_consumed": 1,
            "results_discarded_by_teardown": 0,
            "teardown_returned": True,
            "actor_state_observations": _actor_observations("application", 1),
            "actors_terminated": True,
        },
        "result_timeout": {
            "timeout_type": "GetTimeoutError",
            "result_consumption_attempted_once": True,
            "timed_out_result_discarded_by_teardown": True,
            "results_submitted": 1,
            "results_consumed": 0,
            "results_discarded_by_teardown": 1,
            "teardown_returned": True,
            "actor_state_observations": _actor_observations("timeout", 1),
            "actors_terminated": True,
        },
        "teardown_completed": True,
        "result_accounting": {
            "submitted": 5,
            "consumed": 4,
            "discarded_by_teardown": 1,
            "unconsumed": 0,
        },
        "unconsumed_results": 0,
    }


def _valid_native_runtime(
    profile: dict[str, Any],
    revision: str,
    image_id: str,
    configuration_id: str,
    identity: dict[str, Any],
) -> dict[str, Any]:
    expected_runtime = profile["runtime_expectations"]
    return {
        "runtime_identity": identity,
        "python_version": profile["python_version"],
        "python_implementation": "CPython",
        "kernel": expected_runtime["kernel_release"],
        "machine": expected_runtime["architecture"],
        "libc": expected_runtime["libc"],
        "os_release": expected_runtime["os_release"],
        "dependencies": profile["dependency_profile"],
        "shared_memory_bytes": profile["cluster"]["shared_memory_bytes_per_pod"],
        "alive_ray_nodes": 3,
        "cluster_resources": {"CPU": 2.0, "object_store_memory": 805_306_368.0},
        "source_revision": revision,
        "image_id": image_id,
        "configuration_id": configuration_id,
        "kuberay_version": profile["kuberay"]["operator_version"],
    }


def _valid_topology_outcome(
    topology: str,
    profile: dict[str, Any],
    record_identity: dict[str, Any],
) -> dict[str, Any]:
    decision = pilot._expected_retained_decision(
        profile,
        record_identity,
        topology,
        shared_memory_bytes=profile["cluster"]["shared_memory_bytes_per_pod"],
    )
    serialized_decision = decision.asdict()
    duration = 1.25
    suite = _valid_native_suite()
    if topology == "direct-driver":
        payload = {"driver_pid": 101, "suite": suite}
    else:
        payload = {
            "owner_pid": 202,
            "owner_task_name": f"cgraph-owner-{'e' * 32}",
            "owner_max_retries": 0,
            "suite": suite,
        }
    observation = {
        "schema_version": pilot.PILOT_SCHEMA_VERSION,
        "runtime": _valid_native_runtime(
            profile,
            record_identity["source_revision"],
            record_identity["image_id"],
            record_identity["configuration_id"],
            decision.runtime.asdict(),
        ),
        "payload": payload,
    }
    return {
        "schema_version": pilot.PILOT_SCHEMA_VERSION,
        "status": "success",
        "topology": topology,
        "duration_seconds": duration,
        "decision": serialized_decision,
        "candidate_native": True,
        "supported_product_execution": False,
        "hardened_subprocess": {
            "schema_version": pilot.PROBE_SCHEMA_VERSION,
            "status": "success",
            "successful": True,
            "duration_seconds": duration,
            "exit_code": 0,
            "termination_signal": None,
            "native_exit_code": None,
            "decision": serialized_decision,
            "bounded_private_control_record": True,
            "process_tree_terminated_after_child_exit": True,
        },
        "observation": observation,
    }


def _valid_blocked_record(*, namespace_deleted: bool = True) -> dict[str, Any]:
    revision = "a" * 40
    image = f"{pilot.PILOT_IMAGE_REPOSITORY}:{revision[:12]}"
    image_id = f"sha256:{'b' * 64}"
    configuration_id = pilot._configuration_identity()
    profile = pilot._load_profile()
    namespace_lease = _namespace_lease()
    raycluster_lease = _raycluster_lease(namespace_lease)
    _rendered, rendered_configuration_id, rendered_manifest_id = pilot._render_manifest(
        image,
        image_id,
        namespace_lease,
    )
    assert rendered_configuration_id == configuration_id
    raw_pods = [
        _pod(
            "head",
            "head",
            image=image,
            image_id=image_id,
            configuration_id=configuration_id,
        ),
        _pod(
            "worker-a",
            "worker",
            image=image,
            image_id=image_id,
            configuration_id=configuration_id,
        ),
        _pod(
            "worker-b",
            "worker",
            image=image,
            image_id=image_id,
            configuration_id=configuration_id,
        ),
    ]
    pod_evidence = _verify_test_pod_images(
        raw_pods,
        image,
        image_id,
        configuration_id,
        "docker-desktop",
    )
    runtime_before = {pod["metadata"]["name"]: _runtime_snapshot([]) for pod in raw_pods}
    runtime_after = {
        pod["metadata"]["name"]: _runtime_snapshot(
            [
                (f"sem.hdr{1000 + index}-{1721590000000000000 + index}", 32),
                (f"sem.obj{1000 + index}-{1721590000000000000 + index}", 32),
            ],
            available_bytes=524_763_136,
        )
        for index, pod in enumerate(raw_pods)
    }
    observations = _cleanup_observations(
        runtime_before,
        [runtime_after, runtime_after, runtime_after, runtime_after],
    )
    observations.append(
        {
            "attempt": 5,
            "phase": "final_capture_bracket_verified",
            "wait_before_seconds": 0,
            "cumulative_wait_seconds": 50,
            "pods": runtime_after,
            "assessment": pilot._assess_runtime_cleanup(runtime_before, runtime_after),
        }
    )
    shared_memory = pilot._finalize_runtime_cleanup_assessment(
        runtime_before,
        observations,
    )
    cluster_state = {
        "schema_version": 1,
        "status": "success",
        "active_pilot_actors": [],
        "active_pilot_actor_count": 0,
        "active_pilot_tasks": [],
        "active_pilot_task_count": 0,
        "object_count": 0,
        "object_bytes": 0,
        "object_identity_digest": pilot.sha256(b"[]").hexdigest(),
        "global_gc_completed": True,
    }
    cluster_cleanup = pilot._verify_cluster_cleanup(cluster_state, cluster_state)
    identity = {
        "schema_version": 1,
        "evidence_id": f"local-kuberay:{revision}:{image_id}",
        "source_revision": revision,
        "image": image,
        "image_id": image_id,
        "configuration_id": configuration_id,
        "rendered_manifest_id": rendered_manifest_id,
        "profile_name": pilot.PROFILE_NAME,
        "profile_id": pilot._profile_identity(profile),
        "started_at": "2026-07-21T12:00:00+00:00",
        "completed_at": "2026-07-21T12:10:00+00:00",
        "kubernetes_context": profile["kubernetes"]["context"],
        "namespace": pilot.PILOT_NAMESPACE,
        "namespace_lease": namespace_lease.asdict(),
        "raycluster_lease": raycluster_lease.asdict(),
        "docker": {
            "context": profile["docker"]["context"],
            "endpoint": profile["docker"]["endpoint"],
            "engine": json.loads(json.dumps(profile["docker"]["engine"])),
            "build_context_policy": "dockerfile-specific-deny-by-default",
            "build_context_policy_id": pilot._build_context_policy_identity(),
        },
        "kuberay_operator": {
            "version": profile["kuberay"]["operator_version"],
            "image": f"quay.io/kuberay/operator:v{profile['kuberay']['operator_version']}",
            "image_id": profile["kuberay"]["operator_image"].split("@", 1)[-1],
            "deployment_name": "kuberay-operator",
            "deployment_uid": "operator-deployment-uid",
            "replica_set_name": "kuberay-operator-7c6759ffd8",
            "replica_set_uid": "operator-replicaset-uid",
            "pod_name": "kuberay-operator-7c6759ffd8-abc12",
            "pod_uid": "operator-pod-uid",
            "container_name": "kuberay-operator",
            "container_id": f"docker://{'c' * 64}",
            "restart_count": 6,
            "ready": True,
            "pod_phase": "Running",
            "controller_chain_verified": True,
            "container_inventory_verified": True,
        },
        "kubernetes": {
            "server_version": profile["kubernetes"]["server_version"],
            "node": {
                **profile["kubernetes"]["node"],
                "capacity": {"cpu": "8", "memory": "8Gi", "pods": "110"},
                "allocatable": {"cpu": "8", "memory": "8Gi", "pods": "110"},
            },
            "node_selector": json.loads(json.dumps(profile["kubernetes"]["node_selector"])),
        },
        "profile": profile,
    }
    topologies = [
        _valid_topology_outcome(topology, profile, identity)
        for topology in ("direct-driver", "nested-ray-task")
    ]
    baseline_bytes = profile["cluster"]["shared_memory_bytes_per_pod"]
    changed_bytes = baseline_bytes // 2
    baseline_identity = pilot._expected_policy_identity(
        profile,
        revision=revision,
        image_id=image_id,
        configuration_id=configuration_id,
        shared_memory_profile=f"tmpfs:/dev/shm:size={baseline_bytes}",
    )
    changed_identity = {
        **baseline_identity,
        "shared_memory_profile": f"tmpfs:/dev/shm:size={changed_bytes}",
    }
    common = {
        **identity,
        "candidate_native": True,
        "promotion_eligible": False,
        "supported_product_execution": False,
        "pilot_evidence_passed": False,
        "topologies": topologies,
        "pods": {
            "before": pod_evidence,
            "after": json.loads(json.dumps(pod_evidence)),
            "identity": pilot._verify_pod_execution_identity_unchanged(
                pod_evidence,
                pod_evidence,
            ),
            "final_capture_before": json.loads(json.dumps(pod_evidence)),
            "final_capture_after": json.loads(json.dumps(pod_evidence)),
            "final_capture_identity": pilot._verify_pod_execution_identity_unchanged(
                pod_evidence,
                pod_evidence,
            ),
            "runtime_before": runtime_before,
            "runtime_after": runtime_after,
        },
        "near_neighbor": {
            "schema_version": pilot.PILOT_SCHEMA_VERSION,
            "status": "success",
            "changed_dimension": "shared_memory_profile",
            "changed_value": f"tmpfs:/dev/shm:size={changed_bytes}",
            "baseline_value": f"tmpfs:/dev/shm:size={baseline_bytes}",
            "physical_shared_memory_bytes": changed_bytes,
            "physical_resource_changed": True,
            "pilot_dependency_profile": profile["dependency_profile"],
            "reason": pilot.PILOT_PROFILE_MISMATCH,
            "baseline_admission": pilot._evaluate_exact_pilot_profile_admission(
                baseline_identity,
                baseline_identity,
            ),
            "changed_admission": pilot._evaluate_exact_pilot_profile_admission(
                baseline_identity,
                changed_identity,
            ),
            "child_spawned": False,
            "native_started": False,
        },
        "hard_timeout": {
            "schema_version": pilot.PILOT_SCHEMA_VERSION,
            "status": "success",
            "hard_timeout_observed": True,
            "timeout_seconds": profile["probe"]["hard_timeout_self_test_seconds"],
            "duration_seconds": 0.3,
            "child_exit_code": -15,
            "child_process_group_empty": True,
        },
        "cleanup": {
            "compiled_graph_teardown_verified": False,
            "shared_memory": shared_memory,
            "shared_memory_observations": observations,
            "cluster_state_before": cluster_state,
            "cluster_state_after": cluster_state,
            "cluster_state": cluster_cleanup,
            "pilot_namespace_deleted": namespace_deleted,
            "unrelated_namespaces_touched": [],
        },
    }
    return pilot._blocked_runtime_cleanup_result(
        common,
        shared_memory,
        cluster_state,
        runtime_after,
    )


def _materialize_pilot_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    line_endings: str,
) -> dict[str, Path]:
    sources = {
        "PROFILE_PATH": pilot.PROFILE_PATH,
        "MANIFEST_PATH": pilot.MANIFEST_PATH,
        "DOCKERFILE_PATH": pilot.DOCKERFILE_PATH,
        "DOCKERIGNORE_PATH": pilot.DOCKERIGNORE_PATH,
    }
    pilot_directory = tmp_path / "k8s" / "pilots" / "compiled-graph"
    pilot_directory.mkdir(parents=True)
    paths = {name: pilot_directory / source.name for name, source in sources.items()}
    for name, source in sources.items():
        paths[name].write_bytes(pilot._canonical_source_text_bytes(source))
    _rewrite_pilot_asset_line_endings(paths, line_endings=line_endings)
    for name, path in paths.items():
        monkeypatch.setattr(pilot, name, path)
    return paths


def _rewrite_pilot_asset_line_endings(
    paths: dict[str, Path],
    *,
    line_endings: str,
) -> None:
    for path in paths.values():
        canonical = path.read_bytes().replace(b"\r\n", b"\n")
        if line_endings == "lf":
            rendered = canonical
        elif line_endings == "crlf":
            rendered = canonical.replace(b"\n", b"\r\n")
        elif line_endings == "mixed-crlf":
            rendered_parts: list[bytes] = []
            for index, line in enumerate(canonical.splitlines(keepends=True)):
                if line.endswith(b"\n") and index % 2:
                    line = line[:-1] + b"\r\n"
                rendered_parts.append(line)
            rendered = b"".join(rendered_parts)
        else:  # pragma: no cover - test helper invariant
            raise AssertionError(f"unsupported test line endings: {line_endings}")
        path.write_bytes(rendered)


def test_profile_pins_every_runtime_and_resource_dimension() -> None:
    profile = pilot._load_profile()
    source_version = pilot._load_source_package_version()

    assert profile["profile_name"] == pilot.PROFILE_NAME
    assert profile["base_image"].endswith(
        "@sha256:2951c07de396a8b746f9c678b52c6e2282e614e00f80e6846a9ccd12945ae6b0"
    )
    assert profile["dependency_profile"] == {
        "django-ray": source_version,
        "django": "6.0.7",
        "asgiref": "3.11.1",
        "sqlparse": "0.5.5",
        "ray": "2.56.0",
        "numpy": "1.26.4",
        "pyarrow": "19.0.1",
        "cupy": "absent",
        "cupy-cuda11x": "absent",
        "cupy-cuda12x": "13.4.0",
        "fastrlock": "0.8.3",
    }
    assert profile["kuberay"]["operator_version"] == "1.6.2"
    assert profile["kuberay"]["autoscaling"] is False
    assert profile["compiled_graph_distribution"].startswith("ray[cgraph]==2.56.0")
    assert profile["docker"] == {
        "context": "desktop-linux",
        "endpoint": "npipe:////./pipe/dockerDesktopLinuxEngine",
        "engine": {
            "version": "29.4.3",
            "operating_system": "linux",
            "architecture": "amd64",
            "kernel_version": "6.6.87.2-microsoft-standard-WSL2",
        },
    }
    assert profile["runtime_expectations"]["kernel_release"] == ("6.6.87.2-microsoft-standard-WSL2")
    assert profile["runtime_expectations"]["libc"] == ["glibc", "2.35"]
    assert profile["kubernetes"]["node_selector"] == {
        "kubernetes.io/os": "linux",
        "kubernetes.io/arch": "amd64",
        "kubernetes.io/hostname": "docker-desktop",
    }
    assert profile["kubernetes"]["context"] == "docker-desktop"
    assert profile["cluster"]["namespace"] == pilot.PILOT_NAMESPACE
    assert profile["cluster"]["workers"]["replicas"] == 2
    assert profile["cluster"]["workers"]["min_replicas"] == 2
    assert profile["cluster"]["workers"]["max_replicas"] == 2
    assert profile["cluster"]["head"]["ray_start_parameters"] == {
        "dashboard-host": {"kind": "value", "value": "0.0.0.0"},
        "disable-usage-stats": {
            "kind": "valueless-true-switch",
            "value": "true",
        },
        "num-cpus": {"kind": "value", "value": "0"},
        "object-store-memory": {"kind": "value", "value": "268435456"},
    }
    assert profile["cluster"]["head"]["init_containers"] == []
    assert profile["cluster"]["workers"]["ray_start_parameters"] == {
        "num-cpus": {"kind": "value", "value": "1"},
        "object-store-memory": {"kind": "value", "value": "268435456"},
    }
    assert profile["cluster"]["workers"]["init_containers"] == ["wait-gcs-ready"]
    assert profile["cluster"]["shared_memory_bytes_per_pod"] == 536_870_912
    assert profile["cluster"]["object_store_bytes_per_pod"] == 268_435_456
    assert profile["probe"]["nested_owner_max_retries"] == 0
    assert profile["probe"]["hard_timeout_self_test_seconds"] == 0.25
    assert profile["probe"]["cleanup_retry_delays_seconds"] == [0, 5, 15, 30]
    assert profile["probe"]["topologies"] == ["direct-driver", "nested-ray-task"]


def test_profile_rejects_django_ray_version_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = json.loads(pilot.PROFILE_PATH.read_text(encoding="utf-8"))
    source_version = pilot._load_source_package_version()
    profile["dependency_profile"]["django-ray"] = f"{source_version}.drift"
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(pilot, "PROFILE_PATH", profile_path)

    with pytest.raises(
        pilot.PilotError,
        match="django-ray dependency must match source package version",
    ):
        pilot._load_profile()


def test_dockerfile_keeps_runtime_dependencies_on_the_pinned_base() -> None:
    dockerfile = pilot.DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert (
        "rayproject/ray@sha256:"
        "2951c07de396a8b746f9c678b52c6e2282e614e00f80e6846a9ccd12945ae6b0" in dockerfile
    )
    assert "django==6.0.7 asgiref==3.11.1 sqlparse==0.5.5 fastrlock==0.8.3" in dockerfile
    assert "pip install --disable-pip-version-check --no-cache-dir --no-deps ." in dockerfile
    assert 'project_version = tomllib.load(project_file)["project"]["version"]' in dockerfile
    assert '"django-ray": project_version' in dockerfile
    assert '"ray": "2.56.0"' in dockerfile
    assert '"cupy-cuda12x": "13.4.0"' in dockerfile
    assert '"fastrlock": "0.8.3"' in dockerfile
    assert "USER 1000" in dockerfile


def test_pilot_dockerignore_is_deny_by_default_and_exactly_allowlisted() -> None:
    rules = tuple(
        line.strip()
        for line in pilot.DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    assert rules == (
        "**",
        "!pyproject.toml",
        "!README.md",
        "!LICENSE",
        "!src/",
        "!src/**",
        "!scripts/",
        "!scripts/kuberay_compiled_graph_pilot.py",
        "!k8s/",
        "!k8s/pilots/",
        "!k8s/pilots/compiled-graph/",
        "!k8s/pilots/compiled-graph/Dockerfile",
        "!k8s/pilots/compiled-graph/Dockerfile.dockerignore",
        "!k8s/pilots/compiled-graph/profile.json",
        "!k8s/pilots/compiled-graph/raycluster.yaml",
    )
    assert not any(rule.startswith(("!.env", "!.vault")) for rule in rules)


def test_tracked_build_context_excludes_ignored_and_dirty_worktree_files(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    files = {
        ".gitignore": "*.log\n",
        "pyproject.toml": "[build-system]\nrequires = []\n",
        "README.md": "tracked readme\n",
        "LICENSE": "tracked license\n",
        "src/django_ray/__init__.py": "__version__ = 'tracked'\n",
        "src/django_ray/runtime.py": "VALUE = 'committed'\n",
        "scripts/kuberay_compiled_graph_pilot.py": "print('tracked')\n",
        "k8s/pilots/compiled-graph/Dockerfile": "FROM scratch\n",
        "k8s/pilots/compiled-graph/Dockerfile.dockerignore": (
            pilot.DOCKERIGNORE_PATH.read_text(encoding="utf-8")
        ),
        "k8s/pilots/compiled-graph/profile.json": "{}\n",
        "k8s/pilots/compiled-graph/raycluster.yaml": "kind: RayCluster\n",
    }
    for relative, contents in files.items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    for arguments in (
        ("init",),
        ("config", "user.email", "pilot@example.invalid"),
        ("config", "user.name", "Pilot Test"),
        ("add", "."),
        ("commit", "-m", "test: tracked context"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            check=True,
        )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()

    (repository / "src/django_ray/runtime.py").write_text("VALUE = 'dirty'\n", encoding="utf-8")
    ignored_canary = repository / "src/django_ray/credential.log"
    ignored_canary.write_text("must-not-enter-context\n", encoding="utf-8")

    with pilot._tracked_build_context(revision, repository=repository) as (
        context,
        policy_id,
    ):
        assert (context / "src/django_ray/runtime.py").read_text(encoding="utf-8") == (
            "VALUE = 'committed'\n"
        )
        assert not (context / "src/django_ray/credential.log").exists()
        assert not (context / ".git").exists()
        assert policy_id == (
            "sha256:"
            + pilot.sha256(
                (context / "k8s/pilots/compiled-graph/Dockerfile.dockerignore").read_bytes()
            ).hexdigest()
        )
        context_path = context
    assert not context_path.exists()


@pytest.mark.parametrize(
    "value",
    (
        "/absolute.py",
        "../escape.py",
        "src/../escape.py",
        "src//duplicate.py",
        "src/alternate:data.py",
        "src/CON.py",
        "src/back\\slash.py",
        "src/control\nname.py",
    ),
)
def test_tracked_build_context_rejects_unsafe_archive_paths(value: str) -> None:
    with pytest.raises(pilot.PilotError, match="unsafe archive path"):
        pilot._validated_archive_path(value)


def test_rendered_manifest_is_source_bound_and_fully_resolved() -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    namespace_lease = _namespace_lease()

    rendered, configuration_id, rendered_id = pilot._render_manifest(
        image,
        image_id,
        namespace_lease,
    )

    assert "__PILOT_" not in rendered
    assert "__CONTAINER_PROFILE__" not in rendered
    assert "__DEPLOYMENT_PROFILE__" not in rendered
    assert rendered.count(f"image: {image}") == 2
    assert rendered.count("imagePullPolicy: Never") == 2
    assert rendered.count("medium: Memory") == 2
    assert rendered.count('sizeLimit: "512Mi"') == 2
    assert rendered.count('object-store-memory: "268435456"') == 2
    assert rendered.count("kubernetes.io/hostname: docker-desktop") == 2
    assert "enableInTreeAutoscaling: false" in rendered
    assert "maxReplicas: 2" in rendered
    assert configuration_id.startswith("sha256:") and len(configuration_id) == 71
    assert rendered_id.startswith("sha256:") and len(rendered_id) == 71
    assert configuration_id in rendered
    assert image_id in rendered
    assert rendered.count(namespace_lease.run_token) == 5
    assert sum(line.endswith(f": {namespace_lease.uid}") for line in rendered.splitlines()) == 5


@pytest.mark.parametrize("line_endings", ["lf", "crlf", "mixed-crlf"])
def test_source_text_identities_match_git_archive_bytes_across_clean_checkouts(
    line_endings: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected_configuration_id = pilot._configuration_identity()
    expected_policy_id = pilot._build_context_policy_identity()
    paths = _materialize_pilot_assets(
        tmp_path,
        monkeypatch,
        line_endings=line_endings,
    )

    assert pilot._configuration_identity() == expected_configuration_id
    assert pilot._build_context_policy_identity() == expected_policy_id
    assert pilot._validate_extracted_build_policy(tmp_path) == expected_policy_id
    assert all(b"\r" not in pilot._canonical_source_text_bytes(path) for path in paths.values())


@pytest.mark.parametrize(
    ("value", "error_match"),
    [
        (b"\xef\xbb\xbfprofile\n", "BOM"),
        (b"profile\x00value\n", "NUL"),
        (b"profile\rvalue\n", "bare carriage return"),
        (b"profile\xffvalue\n", "strict UTF-8"),
    ],
)
def test_source_text_identity_rejects_noncanonical_inputs(
    value: bytes,
    error_match: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "asset"
    path.write_bytes(value)

    with pytest.raises(pilot.PilotError, match=error_match):
        pilot._canonical_source_text_bytes(path)


def test_configuration_identity_changes_with_any_profile_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = []
    for index, source in enumerate(
        (
            pilot.PROFILE_PATH,
            pilot.MANIFEST_PATH,
            pilot.DOCKERFILE_PATH,
            pilot.DOCKERIGNORE_PATH,
        )
    ):
        target = tmp_path / f"asset-{index}"
        target.write_bytes(source.read_bytes())
        paths.append(target)
    monkeypatch.setattr(pilot, "PROFILE_PATH", paths[0])
    monkeypatch.setattr(pilot, "MANIFEST_PATH", paths[1])
    monkeypatch.setattr(pilot, "DOCKERFILE_PATH", paths[2])
    monkeypatch.setattr(pilot, "DOCKERIGNORE_PATH", paths[3])
    baseline = pilot._configuration_identity()

    paths[3].write_text(paths[3].read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    assert pilot._configuration_identity() != baseline


def test_image_id_parsers_require_exact_canonical_envelopes() -> None:
    digest = f"sha256:{'a' * 64}"

    assert pilot._parse_exact_docker_image_id(digest) == digest
    assert pilot._parse_exact_cri_image_id(f"docker://{digest}") == digest
    assert (
        pilot._parse_exact_cri_image_id(f"docker-pullable://registry.example/image@{digest}")
        == digest
    )
    for malformed in (
        f"prefix-{digest}",
        f"{digest}-suffix",
        f"{digest}{digest}",
        f"docker://{digest}-suffix",
        f"docker-pullable://registry.example/image@{digest}@{digest}",
        digest.upper(),
    ):
        with pytest.raises(pilot.PilotError, match="noncanonical immutable image ID"):
            pilot._parse_exact_docker_image_id(malformed)
        with pytest.raises(pilot.PilotError, match="noncanonical immutable image ID"):
            pilot._parse_exact_cri_image_id(malformed)


def test_build_image_validates_and_reuses_the_pinned_local_docker_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    revision = "a" * 40
    image_id = f"sha256:{'b' * 64}"
    commands: list[list[str]] = []
    command_cwds: list[Path | None] = []
    responses = iter(
        (
            pilot.CommandResult("desktop-linux\n", "", 0),
            pilot.CommandResult(
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
            ),
            pilot.CommandResult(
                json.dumps(
                    {
                        "Version": "29.4.3",
                        "Os": "linux",
                        "Arch": "amd64",
                        "KernelVersion": "6.6.87.2-microsoft-standard-WSL2",
                    }
                ),
                "",
                0,
            ),
            pilot.CommandResult("", "", 0),
            pilot.CommandResult(
                json.dumps(
                    [
                        {
                            "Id": image_id,
                            "Os": "linux",
                            "Architecture": "amd64",
                            "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
                        }
                    ]
                ),
                "",
                0,
            ),
        )
    )

    def fake_run(command: list[str], **kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        command_cwds.append(kwargs.get("cwd"))
        return next(responses)

    monkeypatch.setattr(pilot, "_run_command", fake_run)
    build_context = tmp_path / "tracked-context"
    build_context.mkdir()

    @contextmanager
    def fake_context(_revision: str) -> Any:
        yield build_context, f"sha256:{'c' * 64}"

    monkeypatch.setattr(pilot, "_tracked_build_context", fake_context)

    image, observed_image_id, docker_context = pilot._build_image(revision)

    assert image == f"{pilot.PILOT_IMAGE_REPOSITORY}:{revision[:12]}"
    assert observed_image_id == image_id
    assert docker_context["context"] == "desktop-linux"
    assert commands[:3] == [
        ["docker", "context", "show"],
        ["docker", "context", "inspect", "desktop-linux"],
        [
            "docker",
            "--context",
            "desktop-linux",
            "version",
            "--format",
            "{{json .Server}}",
        ],
    ]
    assert commands[3][:4] == ["docker", "--context", "desktop-linux", "build"]
    assert commands[3][-1] == "."
    assert command_cwds[3] == build_context
    assert docker_context["build_context_policy_id"] == f"sha256:{'c' * 64}"
    assert commands[4] == [
        "docker",
        "--context",
        "desktop-linux",
        "image",
        "inspect",
        image,
    ]


def test_pod_image_verification_requires_exact_image_id_and_zero_restarts() -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]

    verified = _verify_test_pod_images(
        pods,
        image,
        image_id,
        configuration_id,
        "docker-desktop",
    )

    assert [item["role"] for item in verified] == ["head", "worker", "worker"]
    assert all(item["image_id"] == image_id for item in verified)
    assert all(item["uid"].startswith("uid-") for item in verified)
    assert verified[0]["init_containers"] == []
    assert [item["name"] for item in verified[1]["init_containers"]] == ["wait-gcs-ready"]
    assert [item["name"] for item in verified[2]["init_containers"]] == ["wait-gcs-ready"]
    assert verified[0]["ray_start_parameters"] == {
        "dashboard-host": {
            "lexical_form": "equals-value",
            "lexical_value": "0.0.0.0",
            "semantic_value": "0.0.0.0",
        },
        "disable-usage-stats": {
            "lexical_form": "valueless-switch",
            "lexical_value": None,
            "semantic_value": True,
        },
        "num-cpus": {
            "lexical_form": "equals-value",
            "lexical_value": "0",
            "semantic_value": "0",
        },
        "object-store-memory": {
            "lexical_form": "equals-value",
            "lexical_value": "268435456",
            "semantic_value": "268435456",
        },
    }
    assert all(
        set(item["identity_environment"])
        == {
            "DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE",
            "DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE",
            "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE",
            "DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE",
            "DJANGO_RAY_PILOT_IMAGE_ID",
            "DJANGO_RAY_PILOT_CONFIG_ID",
            "DJANGO_RAY_PILOT_KUBERAY_VERSION",
            "DJANGO_RAY_PILOT_NAMESPACE_UID",
            "DJANGO_RAY_PILOT_RUN_TOKEN",
        }
        for item in verified
    )
    assert all(item["namespace_uid"] == "pilot-namespace-uid" for item in verified)
    assert all(item["run_token"] == "1" * 32 for item in verified)
    assert all(item["raycluster_uid"] == "pilot-raycluster-uid" for item in verified)
    assert all(item["owner_reference_verified"] is True for item in verified)
    assert pilot._verify_pod_execution_identity_unchanged(verified, verified)["status"] == "success"

    for field, changed_value in (
        ("uid", "replacement-pod-uid"),
        ("container_id", f"docker://{'f' * 64}"),
        ("image_id", f"sha256:{'f' * 64}"),
        ("configuration_id", f"sha256:{'f' * 64}"),
        ("node", "replacement-node"),
        ("restart_count", 1),
    ):
        changed_identity = json.loads(json.dumps(verified))
        changed_identity[1][field] = changed_value
        with pytest.raises(pilot.PilotError, match="execution identity changed"):
            pilot._verify_pod_execution_identity_unchanged(verified, changed_identity)

    for path, changed_value in (
        (("identity_environment", "DJANGO_RAY_PILOT_IMAGE_ID"), f"sha256:{'f' * 64}"),
        (("ray_start_parameters", "num-cpus"), "99"),
        (("init_containers", 0, "restart_count"), 1),
    ):
        changed_identity = json.loads(json.dumps(verified))
        target: Any = changed_identity[1]
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = changed_value
        with pytest.raises(pilot.PilotError, match="execution identity changed"):
            pilot._verify_pod_execution_identity_unchanged(verified, changed_identity)

    changed_identity = json.loads(json.dumps(verified))
    changed_identity[0]["ray_start_parameters"]["disable-usage-stats"]["lexical_form"] = (
        "equals-value"
    )
    with pytest.raises(pilot.PilotError, match="execution identity changed"):
        pilot._verify_pod_execution_identity_unchanged(verified, changed_identity)

    pods[1]["status"]["containerStatuses"][0]["imageID"] = f"docker://sha256:{'b' * 64}"
    with pytest.raises(pilot.PilotError, match="does not run the tested image"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )

    pods[1] = _pod("worker-a", "worker", image=image, image_id=image_id, restarts=1)
    with pytest.raises(pilot.PilotError, match="restarted"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )

    pods[1]["spec"]["nodeName"] = "another-node"
    with pytest.raises(pilot.PilotError, match="ran on node"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


@pytest.mark.parametrize("restart_count", [False, "0", 0.0, 0.5])
def test_pod_restart_count_requires_exact_json_integer_zero(restart_count: Any) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    pods[1]["status"]["containerStatuses"][0]["restartCount"] = restart_count

    with pytest.raises(pilot.PilotError, match="restarted"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


def test_pod_rejects_extra_regular_container_and_its_restarts() -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    pods[1]["spec"]["containers"].append(
        {"name": "foreign-sidecar", "image": "foreign:latest", "env": [], "args": []}
    )
    pods[1]["status"]["containerStatuses"].append(
        {
            "name": "foreign-sidecar",
            "containerID": f"docker://{'f' * 64}",
            "image": "foreign:latest",
            "imageID": f"docker://sha256:{'f' * 64}",
            "ready": True,
            "restartCount": 9,
            "state": {"running": {}},
        }
    )

    with pytest.raises(pilot.PilotError, match="regular-container inventory changed"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


@pytest.mark.parametrize(
    "mutation",
    ["missing_spec", "missing_status", "extra", "restart", "image", "state"],
)
def test_worker_pod_requires_exact_successful_init_container_inventory(mutation: str) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    worker = pods[1]
    if mutation == "missing_spec":
        worker["spec"]["initContainers"] = []
    elif mutation == "missing_status":
        worker["status"]["initContainerStatuses"] = []
    elif mutation == "extra":
        worker["spec"]["initContainers"].append({"name": "foreign-init", "image": image})
        worker["status"]["initContainerStatuses"].append(
            {
                **worker["status"]["initContainerStatuses"][0],
                "name": "foreign-init",
                "containerID": f"docker://{'f' * 64}",
            }
        )
    elif mutation == "restart":
        worker["status"]["initContainerStatuses"][0]["restartCount"] = 1
    elif mutation == "image":
        worker["status"]["initContainerStatuses"][0]["imageID"] = f"docker://sha256:{'f' * 64}"
    else:
        worker["status"]["initContainerStatuses"][0]["state"] = {"running": {}}

    with pytest.raises(pilot.PilotError, match="init-container"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


@pytest.mark.parametrize(
    "option",
    ["dashboard-host", "disable-usage-stats", "num-cpus", "object-store-memory"],
)
@pytest.mark.parametrize("mutation", ["missing", "changed", "duplicate"])
def test_head_pod_requires_every_profile_ray_start_parameter_once(
    option: str,
    mutation: str,
) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    expected = pilot._load_profile()["cluster"]["head"]["ray_start_parameters"][option]
    arguments = pods[0]["spec"]["containers"][0]["args"][0]
    token = (
        f"--{option}"
        if expected["kind"] == "valueless-true-switch"
        else f"--{option}={expected['value']}"
    )
    if mutation == "missing":
        arguments = arguments.replace(token, "")
    elif mutation == "changed":
        arguments = arguments.replace(token, f"--{option}=changed")
    else:
        arguments = f"{arguments} {token}"
    pods[0]["spec"]["containers"][0]["args"] = [arguments]

    with pytest.raises(pilot.PilotError, match=rf"Ray --{option} changed"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


def test_head_pod_accepts_valueless_true_switch_and_retains_its_semantics() -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]

    verified = _verify_test_pod_images(
        pods,
        image,
        image_id,
        configuration_id,
        "docker-desktop",
    )

    assert verified[0]["ray_start_parameters"]["disable-usage-stats"] == {
        "lexical_form": "valueless-switch",
        "lexical_value": None,
        "semantic_value": True,
    }


@pytest.mark.parametrize("value", ["false", "true", "changed", "-1"])
def test_head_pod_rejects_a_separate_value_for_valueless_true_switch(value: str) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    arguments = pods[0]["spec"]["containers"][0]["args"][0]
    pods[0]["spec"]["containers"][0]["args"] = [
        arguments.replace("--disable-usage-stats", f"--disable-usage-stats {value}")
    ]

    with pytest.raises(pilot.PilotError, match="unexpected separate value"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


@pytest.mark.parametrize("value", ["true", "false", "changed", ""])
def test_head_pod_rejects_equals_value_for_valueless_true_switch(value: str) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    arguments = pods[0]["spec"]["containers"][0]["args"][0]
    pods[0]["spec"]["containers"][0]["args"] = [
        arguments.replace("--disable-usage-stats", f"--disable-usage-stats={value}")
    ]

    with pytest.raises(pilot.PilotError, match="Ray --disable-usage-stats changed"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


@pytest.mark.parametrize(
    "variable_name",
    [
        "DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE",
        "DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE",
        "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE",
        "DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE",
        "DJANGO_RAY_PILOT_IMAGE_ID",
        "DJANGO_RAY_PILOT_CONFIG_ID",
        "DJANGO_RAY_PILOT_KUBERAY_VERSION",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "changed", "duplicate"])
def test_pod_requires_each_exact_identity_environment_entry_once(
    variable_name: str,
    mutation: str,
) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    environment = pods[1]["spec"]["containers"][0]["env"]
    entry = next(value for value in environment if value["name"] == variable_name)
    if mutation == "missing":
        environment.remove(entry)
    elif mutation == "changed":
        entry["value"] = "changed"
    else:
        environment.append(dict(entry))

    with pytest.raises(pilot.PilotError, match="configuration identity changed"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


@pytest.mark.parametrize(
    "arguments",
    [
        ["ray start --object-store-memory=268435456 --object-store-memory=268435456 --num-cpus=1"],
        ["ray start --object-store-memory=268435456 --num-cpus=1 --num-cpus=99"],
        ["ray start --object-store-memory=268435456 --x-num-cpus=1"],
        ["ray start --object-store-memory --num-cpus=1"],
        ["echo --num-cpus 1 ; ray start --object-store-memory=268435456"],
    ],
)
def test_pod_rejects_duplicate_conflicting_or_substring_ray_start_flags(
    arguments: list[str],
) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    pods[1]["spec"]["containers"][0]["args"] = arguments

    with pytest.raises(pilot.PilotError, match="Ray --"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


def test_pod_rejects_multiple_structural_ray_start_commands() -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    pods[1]["spec"]["containers"][0]["args"] = [
        "ray start --object-store-memory=268435456 --num-cpus=1 ; "
        "ray start --object-store-memory=268435456 --num-cpus=1"
    ]

    with pytest.raises(pilot.PilotError, match="exactly one structural Ray start"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


def test_pod_accepts_structurally_split_exact_ray_start_flags() -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    pods[1]["spec"]["containers"][0]["args"] = [
        "ray",
        "start",
        "--object-store-memory",
        "268435456",
        "--num-cpus",
        "1",
    ]

    verified = _verify_test_pod_images(
        pods,
        image,
        image_id,
        configuration_id,
        "docker-desktop",
    )

    assert verified[1]["ray_start_parameters"] == {
        "num-cpus": {
            "lexical_form": "separate-value",
            "lexical_value": "1",
            "semantic_value": "1",
        },
        "object-store-memory": {
            "lexical_form": "separate-value",
            "lexical_value": "268435456",
            "semantic_value": "268435456",
        },
    }


@pytest.mark.parametrize(
    "mutation, error_match",
    [
        ("phase", "not Running"),
        ("ready", "not Ready"),
        ("deleting", "entered deletion"),
        ("configuration", "configuration identity changed"),
    ],
)
def test_pod_evidence_requires_running_ready_non_deleting_configuration_identity(
    mutation: str,
    error_match: str,
) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    if mutation == "phase":
        pods[1]["status"]["phase"] = "Pending"
    elif mutation == "ready":
        pods[1]["status"]["containerStatuses"][0]["ready"] = False
    elif mutation == "deleting":
        pods[1]["metadata"]["deletionTimestamp"] = "2026-07-21T12:00:00Z"
    elif mutation == "configuration":
        environment = pods[1]["spec"]["containers"][0]["env"]
        next(value for value in environment if value["name"] == "DJANGO_RAY_PILOT_CONFIG_ID")[
            "value"
        ] = f"sha256:{'f' * 64}"

    with pytest.raises(pilot.PilotError, match=error_match):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "namespace",
        "profile_label",
        "run_label",
        "namespace_annotation",
        "owner_uid",
        "owner_name",
        "owner_api",
        "namespace_env",
        "run_env",
    ],
)
def test_pod_evidence_is_bound_to_the_exact_namespace_and_raycluster_lease(
    mutation: str,
) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    metadata = pods[1]["metadata"]
    if mutation == "namespace":
        metadata["namespace"] = "replacement-namespace"
    elif mutation == "profile_label":
        metadata["labels"][pilot.PILOT_PROFILE_LABEL_KEY] = "replacement-profile"
    elif mutation == "run_label":
        metadata["labels"][pilot.PILOT_RUN_LABEL_KEY] = "2" * 32
    elif mutation == "namespace_annotation":
        metadata["annotations"][pilot.PILOT_NAMESPACE_UID_ANNOTATION_KEY] = "replacement-uid"
    elif mutation == "owner_uid":
        metadata["ownerReferences"][0]["uid"] = "replacement-raycluster-uid"
    elif mutation == "owner_name":
        metadata["ownerReferences"][0]["name"] = "replacement-raycluster"
    elif mutation == "owner_api":
        metadata["ownerReferences"][0]["apiVersion"] = "apps/v1"
    else:
        environment = pods[1]["spec"]["containers"][0]["env"]
        variable = (
            "DJANGO_RAY_PILOT_NAMESPACE_UID"
            if mutation == "namespace_env"
            else "DJANGO_RAY_PILOT_RUN_TOKEN"
        )
        next(item for item in environment if item["name"] == variable)["value"] = "changed"

    with pytest.raises(pilot.PilotError, match="lease|configuration identity"):
        _verify_test_pod_images(
            pods,
            image,
            image_id,
            configuration_id,
            "docker-desktop",
        )


def test_final_runtime_and_cluster_capture_is_bracketed_by_pod_identity_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = "django-ray-cgraph-pilot:0123456789ab"
    image_id = f"sha256:{'a' * 64}"
    configuration_id = pilot._configuration_identity()
    pods = [
        _pod("head", "head", image=image, image_id=image_id),
        _pod("worker-a", "worker", image=image, image_id=image_id),
        _pod("worker-b", "worker", image=image, image_id=image_id),
    ]
    baseline = _verify_test_pod_images(
        pods,
        image,
        image_id,
        configuration_id,
        "docker-desktop",
    )
    events: list[str] = []
    fetches = iter((json.loads(json.dumps(pods)), json.loads(json.dumps(pods))))

    def fetch(*_args: Any) -> list[dict[str, Any]]:
        events.append("fetch")
        return next(fetches)

    def runtime(*_args: Any) -> dict[str, Any]:
        events.append("runtime")
        return {"captured": True}

    def cluster(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        events.append("cluster")
        return {"status": "success"}

    monkeypatch.setattr(pilot, "_fetch_pilot_pods", fetch)
    monkeypatch.setattr(pilot, "_capture_pod_runtime_snapshots", runtime)
    monkeypatch.setattr(pilot, "_kubectl_exec_json", cluster)
    monkeypatch.setattr(
        pilot,
        "_assert_kuberay_operator_identity_unchanged",
        lambda *_args: events.append("operator"),
    )
    namespace_lease = _namespace_lease()

    result = pilot._capture_final_runtime_and_cluster_evidence(
        "docker-desktop",
        namespace_lease,
        _raycluster_lease(namespace_lease),
        baseline_pod_evidence=baseline,
        image=image,
        image_id=image_id,
        configuration_id=configuration_id,
        node_name="docker-desktop",
        operator_profile={"profile": "operator"},
        expected_operator={"identity": "operator"},
    )

    assert events == ["fetch", "runtime", "cluster", "fetch", "operator"]
    assert result["runtime_after"] == {"captured": True}
    assert result["capture_identity"]["configuration_ids_unchanged"] is True
    assert result["capture_identity"]["pod_lifecycle_unchanged"] is True


def test_host_target_rejects_context_drift_before_operator_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        return pilot.CommandResult(stdout="unexpected-context\n", stderr="", returncode=0)

    monkeypatch.setattr(pilot, "_run_command", fake_run)

    with pytest.raises(pilot.PilotError, match="active Kubernetes context"):
        pilot._validate_host_target("docker-desktop", pilot.PILOT_NAMESPACE)

    assert commands == [["kubectl", "config", "current-context"]]


def test_host_target_rejects_any_other_namespace_without_calling_kubectl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pilot,
        "_run_command",
        lambda *_args, **_kwargs: pytest.fail("kubectl must not run"),
    )

    with pytest.raises(pilot.PilotError, match="must be exactly"):
        pilot._validate_host_target("docker-desktop", "django-ray")


def _operator_resources(*, restart_count: int = 6) -> list[dict[str, Any]]:
    profile = pilot._load_profile()["kuberay"]
    namespace = profile["operator_namespace"]
    deployment_name = profile["operator_deployment_name"]
    container_name = profile["operator_container_name"]
    image = f"quay.io/kuberay/operator:v{profile['operator_version']}"
    image_id = profile["operator_image"].split("@", 1)[-1]
    deployment_uid = "operator-deployment-uid"
    template_hash = "7c6759ffd8"
    replica_set_name = f"{deployment_name}-{template_hash}"
    replica_set_uid = "operator-replicaset-uid"
    live_labels = {**profile["operator_pod_labels"], "pod-template-hash": template_hash}
    replica_set_selector = {**profile["operator_selector"], "pod-template-hash": template_hash}

    deployment = {
        "metadata": {
            "name": deployment_name,
            "namespace": namespace,
            "uid": deployment_uid,
            "generation": 1,
            "labels": profile["operator_deployment_labels"],
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": profile["operator_selector"]},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": profile["operator_pod_labels"]},
                "spec": {
                    "serviceAccountName": profile["operator_service_account"],
                    "containers": [
                        {
                            "name": container_name,
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/manager"],
                        }
                    ],
                },
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    }
    pod = {
        "metadata": {
            "name": f"{replica_set_name}-abc12",
            "namespace": namespace,
            "uid": "operator-pod-uid",
            "generation": 1,
            "labels": live_labels,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": replica_set_name,
                    "uid": replica_set_uid,
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
        "spec": {
            "serviceAccountName": profile["operator_service_account"],
            "containers": [{"name": container_name, "image": image}],
        },
        "status": {
            "phase": "Running",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "observedGeneration": 1,
                }
            ],
            "containerStatuses": [
                {
                    "name": container_name,
                    "image": image,
                    "imageID": f"docker-pullable://quay.io/kuberay/operator@{image_id}",
                    "containerID": f"docker://{'c' * 64}",
                    "ready": True,
                    "restartCount": restart_count,
                    "state": {"running": {"startedAt": "2026-07-21T12:00:00Z"}},
                }
            ],
        },
    }
    replica_set = {
        "metadata": {
            "name": replica_set_name,
            "namespace": namespace,
            "uid": replica_set_uid,
            "generation": 1,
            "labels": live_labels,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": deployment_name,
                    "uid": deployment_uid,
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": replica_set_selector},
            "template": {
                "metadata": {"labels": live_labels},
                "spec": {
                    "serviceAccountName": profile["operator_service_account"],
                    "containers": [{"name": container_name, "image": image}],
                },
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    }
    return [deployment, {"items": [pod]}, replica_set]


def test_kuberay_operator_retains_exact_ready_controller_and_container_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = iter(_operator_resources(restart_count=6))
    commands: list[list[str]] = []

    def fake_json(command: list[str], **_kwargs: Any) -> dict[str, Any]:
        commands.append(command)
        return next(resources)

    monkeypatch.setattr(pilot, "_run_json_command", fake_json)

    evidence = pilot._validate_kuberay_operator(
        "docker-desktop",
        pilot._load_profile()["kuberay"],
    )

    assert evidence["restart_count"] == 6
    assert evidence["ready"] is True
    assert evidence["controller_chain_verified"] is True
    assert evidence["container_inventory_verified"] is True
    assert [command[command.index("get") + 1] for command in commands] == [
        "deployment",
        "pods",
        "replicaset",
    ]


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("deployment_uid", "replacement-deployment-uid"),
        ("replica_set_uid", "replacement-replicaset-uid"),
        ("pod_uid", "replacement-pod-uid"),
        ("container_id", f"docker://{'f' * 64}"),
        ("image_id", f"sha256:{'f' * 64}"),
        ("restart_count", 7),
        ("ready", False),
    ],
)
def test_kuberay_operator_revalidation_rejects_any_observed_identity_drift(
    field: str,
    changed: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(_operator_resources())
    monkeypatch.setattr(
        pilot,
        "_run_json_command",
        lambda *_args, **_kwargs: next(responses),
    )
    expected = pilot._validate_kuberay_operator(
        "docker-desktop",
        pilot._load_profile()["kuberay"],
    )
    observed = {**expected, field: changed}
    monkeypatch.setattr(pilot, "_validate_kuberay_operator", lambda *_args: observed)

    with pytest.raises(pilot.PilotError, match="changed during the pilot"):
        pilot._assert_kuberay_operator_identity_unchanged(
            "docker-desktop",
            pilot._load_profile()["kuberay"],
            expected,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "deployment_decoy_container",
        "deployment_status",
        "deployment_deleting",
        "extra_operator_pod",
        "pod_missing_status",
        "pod_reordered_decoy_status",
        "pod_wrong_status_name",
        "pod_wrong_digest",
        "pod_not_ready",
        "pod_ready_generation",
        "pod_negative_restart",
        "pod_bool_restart",
        "pod_extra_init",
        "pod_extra_ephemeral",
        "pod_wrong_owner",
        "replicaset_wrong_owner",
        "replicaset_deleting",
        "replicaset_missing_container",
    ],
)
def test_kuberay_operator_rejects_decoys_inventory_and_ownership_drift(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _operator_resources()
    deployment = resources[0]
    pods = resources[1]
    replica_set = resources[2]
    pod = pods["items"][0]
    if mutation == "deployment_decoy_container":
        deployment["spec"]["template"]["spec"]["containers"].append(
            {"name": "decoy", "image": "quay.io/kuberay/operator:v1.6.2"}
        )
    elif mutation == "deployment_status":
        deployment["status"]["readyReplicas"] = 0
    elif mutation == "deployment_deleting":
        deployment["metadata"]["deletionTimestamp"] = "2026-07-21T12:00:00Z"
    elif mutation == "extra_operator_pod":
        pods["items"].append(json.loads(json.dumps(pod)))
    elif mutation == "pod_missing_status":
        pod["status"]["containerStatuses"] = []
    elif mutation == "pod_reordered_decoy_status":
        pod["status"]["containerStatuses"].insert(
            0,
            {**pod["status"]["containerStatuses"][0], "name": "decoy"},
        )
    elif mutation == "pod_wrong_status_name":
        pod["status"]["containerStatuses"][0]["name"] = "decoy"
    elif mutation == "pod_wrong_digest":
        pod["status"]["containerStatuses"][0]["imageID"] = (
            f"docker-pullable://quay.io/kuberay/operator@sha256:{'f' * 64}"
        )
    elif mutation == "pod_not_ready":
        pod["status"]["containerStatuses"][0]["ready"] = False
    elif mutation == "pod_ready_generation":
        pod["status"]["conditions"][0]["observedGeneration"] = 2
    elif mutation == "pod_negative_restart":
        pod["status"]["containerStatuses"][0]["restartCount"] = -1
    elif mutation == "pod_bool_restart":
        pod["status"]["containerStatuses"][0]["restartCount"] = True
    elif mutation == "pod_extra_init":
        pod["spec"]["initContainers"] = [{"name": "decoy"}]
    elif mutation == "pod_extra_ephemeral":
        pod["spec"]["ephemeralContainers"] = [{"name": "decoy"}]
    elif mutation == "pod_wrong_owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "replacement-replicaset-uid"
    elif mutation == "replicaset_wrong_owner":
        replica_set["metadata"]["ownerReferences"][0]["uid"] = "replacement-deployment-uid"
    elif mutation == "replicaset_deleting":
        replica_set["metadata"]["deletionTimestamp"] = "2026-07-21T12:00:00Z"
    else:
        replica_set["spec"]["template"]["spec"]["containers"] = []
    responses = iter(resources)
    monkeypatch.setattr(
        pilot,
        "_run_json_command",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(pilot.PilotError, match="KubeRay|operator"):
        pilot._validate_kuberay_operator(
            "docker-desktop",
            pilot._load_profile()["kuberay"],
        )


def test_git_revision_requires_a_clean_full_object_id(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            pilot.CommandResult(stdout=f"{'a' * 40}\n", stderr="", returncode=0),
            pilot.CommandResult(stdout=" M source.py\n", stderr="", returncode=0),
        ]
    )
    monkeypatch.setattr(pilot, "_run_command", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(pilot.PilotError, match="clean Git worktree"):
        pilot._git_source_revision()


def test_git_revision_rejects_a_head_race(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            pilot.CommandResult(stdout=f"{'a' * 40}\n", stderr="", returncode=0),
            pilot.CommandResult(stdout="", stderr="", returncode=0),
            pilot.CommandResult(stdout=f"{'b' * 40}\n", stderr="", returncode=0),
        ]
    )
    monkeypatch.setattr(pilot, "_run_command", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(pilot.PilotError, match="changed while cleanliness was checked"):
        pilot._git_source_revision()


def test_namespace_lookup_failure_never_falls_through_to_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        return pilot.CommandResult("", "Unauthorized", 1)

    monkeypatch.setattr(pilot, "_run_command", fake_run)

    with pytest.raises(pilot.PilotError, match="whether the pilot namespace already exists"):
        pilot._ensure_namespace("docker-desktop", pilot.PILOT_NAMESPACE)

    assert len(commands) == 1
    assert "--ignore-not-found=true" in commands[0]
    assert "create" not in commands[0]


def _namespace_record(
    lease: pilot.NamespaceLease,
    *,
    deletion_timestamp: str | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": lease.name,
            "uid": lease.uid,
            "deletionTimestamp": deletion_timestamp,
            "labels": {
                pilot.PILOT_PROFILE_LABEL_KEY: pilot.PROFILE_NAME,
                pilot.PILOT_RUN_LABEL_KEY: lease.run_token,
            },
        }
    }


def _raycluster_record(
    namespace_lease: pilot.NamespaceLease,
    raycluster_lease: pilot.RayClusterLease | None = None,
    *,
    deletion_timestamp: str | None = None,
) -> dict[str, Any]:
    raycluster_lease = raycluster_lease or _raycluster_lease(namespace_lease)
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayCluster",
        "metadata": {
            "name": raycluster_lease.name,
            "namespace": namespace_lease.name,
            "uid": raycluster_lease.uid,
            "deletionTimestamp": deletion_timestamp,
            "labels": {
                pilot.PILOT_PROFILE_LABEL_KEY: pilot.PROFILE_NAME,
                pilot.PILOT_RUN_LABEL_KEY: raycluster_lease.run_token,
            },
            "annotations": {
                pilot.PILOT_NAMESPACE_UID_ANNOTATION_KEY: raycluster_lease.namespace_uid,
            },
        },
    }


@pytest.mark.parametrize(
    "mutation",
    ["missing", "uid", "profile", "run_token", "deleting", "api_failure"],
)
def test_current_namespace_lease_fails_closed_for_every_identity_change(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _namespace_lease()
    record = _namespace_record(lease)
    if mutation == "missing":
        result = pilot.CommandResult("", "", 0)
    elif mutation == "api_failure":
        result = pilot.CommandResult("", "Unauthorized", 1)
    else:
        metadata = record["metadata"]
        if mutation == "uid":
            metadata["uid"] = "replacement-namespace-uid"
        elif mutation == "profile":
            metadata["labels"][pilot.PILOT_PROFILE_LABEL_KEY] = "replacement-profile"
        elif mutation == "run_token":
            metadata["labels"][pilot.PILOT_RUN_LABEL_KEY] = "2" * 32
        else:
            metadata["deletionTimestamp"] = "2026-07-21T12:00:00Z"
        result = pilot.CommandResult(json.dumps(record), "", 0)
    monkeypatch.setattr(pilot, "_run_command", lambda *_args, **_kwargs: result)

    with pytest.raises(pilot.PilotError, match="namespace"):
        pilot._assert_current_namespace_lease("docker-desktop", lease)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "uid", "profile", "run_token", "namespace_uid", "deleting", "api_failure"],
)
def test_current_raycluster_lease_fails_closed_for_every_identity_change(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace_lease = _namespace_lease()
    raycluster_lease = _raycluster_lease(namespace_lease)
    record = _raycluster_record(namespace_lease, raycluster_lease)
    if mutation == "missing":
        result = pilot.CommandResult("", "", 0)
    elif mutation == "api_failure":
        result = pilot.CommandResult("", "Unauthorized", 1)
    else:
        metadata = record["metadata"]
        if mutation == "uid":
            metadata["uid"] = "replacement-raycluster-uid"
        elif mutation == "profile":
            metadata["labels"][pilot.PILOT_PROFILE_LABEL_KEY] = "replacement-profile"
        elif mutation == "run_token":
            metadata["labels"][pilot.PILOT_RUN_LABEL_KEY] = "2" * 32
        elif mutation == "namespace_uid":
            metadata["annotations"][pilot.PILOT_NAMESPACE_UID_ANNOTATION_KEY] = (
                "replacement-namespace-uid"
            )
        else:
            metadata["deletionTimestamp"] = "2026-07-21T12:00:00Z"
        result = pilot.CommandResult(json.dumps(record), "", 0)
    monkeypatch.setattr(pilot, "_run_command", lambda *_args, **_kwargs: result)

    with pytest.raises(pilot.PilotError, match="RayCluster"):
        pilot._assert_current_raycluster_lease(
            "docker-desktop",
            namespace_lease,
            raycluster_lease,
        )


def test_raycluster_is_created_once_and_wait_queries_are_bound_to_the_run_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace_lease = _namespace_lease()
    raycluster_lease = _raycluster_lease(namespace_lease)
    ready_pods = [
        {
            "metadata": {"labels": {"ray.io/node-type": "head"}},
            "status": {"containerStatuses": [{"ready": True}]},
        },
        {
            "metadata": {"labels": {"ray.io/node-type": "worker"}},
            "status": {"containerStatuses": [{"ready": True}]},
        },
        {
            "metadata": {"labels": {"ray.io/node-type": "worker"}},
            "status": {"containerStatuses": [{"ready": True}]},
        },
    ]
    responses = iter(
        (
            pilot.CommandResult(json.dumps(_raycluster_record(namespace_lease)), "", 0),
            pilot.CommandResult(json.dumps({"items": ready_pods}), "", 0),
        )
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> pilot.CommandResult:
        calls.append((command, kwargs))
        return next(responses)

    monkeypatch.setattr(pilot, "_run_command", fake_run)
    monkeypatch.setattr(pilot, "_assert_current_namespace_lease", lambda *_args: {})
    monkeypatch.setattr(pilot, "_assert_current_raycluster_lease", lambda *_args: {})
    operator_checks: list[str] = []
    monkeypatch.setattr(
        pilot,
        "_assert_kuberay_operator_identity_unchanged",
        lambda *_args: operator_checks.append("checked"),
    )

    pods, observed_lease = pilot._create_and_wait(
        "docker-desktop",
        namespace_lease,
        "rendered-manifest",
        operator_profile={"profile": "operator"},
        expected_operator={"identity": "operator"},
    )

    assert pods == ready_pods
    assert observed_lease == raycluster_lease
    assert operator_checks == ["checked", "checked"]
    create_command, create_kwargs = calls[0]
    assert "create" in create_command
    assert "apply" not in create_command
    assert create_command[-2:] == ["-o", "json"]
    assert create_kwargs["input_text"] == "rendered-manifest"
    pod_command = calls[1][0]
    selector = pod_command[pod_command.index("-l") + 1]
    assert selector == (
        f"ray.io/cluster={pilot.RAYCLUSTER_NAME},"
        f"{pilot.PILOT_RUN_LABEL_KEY}={namespace_lease.run_token}"
    )


def test_operator_drift_during_build_blocks_raycluster_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pilot,
        "_assert_kuberay_operator_identity_unchanged",
        lambda *_args: (_ for _ in ()).throw(
            pilot.PilotError("KubeRay operator identity changed during the pilot")
        ),
    )
    monkeypatch.setattr(
        pilot,
        "_run_command",
        lambda *_args, **_kwargs: pytest.fail(
            "RayCluster create must not run after operator drift"
        ),
    )

    with pytest.raises(pilot.PilotError, match="operator identity changed"):
        pilot._create_and_wait(
            "docker-desktop",
            _namespace_lease(),
            "manifest",
            operator_profile={"profile": "operator"},
            expected_operator={"identity": "before-build"},
        )


def test_operator_drift_after_raycluster_readiness_invalidates_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace_lease = _namespace_lease()
    ready_pods = [
        {
            "metadata": {"labels": {"ray.io/node-type": role}},
            "status": {"containerStatuses": [{"ready": True}]},
        }
        for role in ("head", "worker", "worker")
    ]
    responses = iter(
        (
            pilot.CommandResult(json.dumps(_raycluster_record(namespace_lease)), "", 0),
            pilot.CommandResult(json.dumps({"items": ready_pods}), "", 0),
        )
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        return next(responses)

    checks: list[str] = []

    def check_operator(*_args: Any) -> None:
        checks.append("checked")
        if len(checks) == 2:
            raise pilot.PilotError("KubeRay operator identity changed during the pilot")

    monkeypatch.setattr(pilot, "_run_command", fake_run)
    monkeypatch.setattr(pilot, "_assert_current_namespace_lease", lambda *_args: {})
    monkeypatch.setattr(pilot, "_assert_current_raycluster_lease", lambda *_args: {})
    monkeypatch.setattr(pilot, "_assert_kuberay_operator_identity_unchanged", check_operator)

    with pytest.raises(pilot.PilotError, match="operator identity changed"):
        pilot._create_and_wait(
            "docker-desktop",
            namespace_lease,
            "manifest",
            operator_profile={"profile": "operator"},
            expected_operator={"identity": "pre-create"},
        )

    assert checks == ["checked", "checked"]
    assert any("create" in command for command in commands)
    assert any("pods" in command for command in commands)


def test_namespace_lease_failure_before_create_never_mutates_the_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pilot,
        "_assert_kuberay_operator_identity_unchanged",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        pilot,
        "_assert_current_namespace_lease",
        lambda *_args: (_ for _ in ()).throw(pilot.PilotError("replacement namespace")),
    )
    monkeypatch.setattr(
        pilot,
        "_run_command",
        lambda *_args, **_kwargs: pytest.fail("create must not run after lease loss"),
    )

    with pytest.raises(pilot.PilotError, match="replacement namespace"):
        pilot._create_and_wait(
            "docker-desktop",
            _namespace_lease(),
            "manifest",
            operator_profile={},
            expected_operator={},
        )


def test_raycluster_lease_failure_before_exec_never_enters_the_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace_lease = _namespace_lease()
    monkeypatch.setattr(pilot, "_assert_current_namespace_lease", lambda *_args: {})
    monkeypatch.setattr(
        pilot,
        "_assert_current_raycluster_lease",
        lambda *_args: (_ for _ in ()).throw(pilot.PilotError("replacement RayCluster")),
    )
    monkeypatch.setattr(
        pilot,
        "_run_command",
        lambda *_args, **_kwargs: pytest.fail("exec must not run after lease loss"),
    )

    with pytest.raises(pilot.PilotError, match="replacement RayCluster"):
        pilot._kubectl_exec_json(
            "docker-desktop",
            namespace_lease,
            _raycluster_lease(namespace_lease),
            "head",
            ["inspect-cluster-state"],
            timeout_seconds=60,
        )


def test_failed_exec_still_runs_both_post_boundary_lease_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace_lease = _namespace_lease()
    events: list[str] = []
    monkeypatch.setattr(
        pilot,
        "_assert_current_namespace_lease",
        lambda *_args: events.append("namespace"),
    )
    monkeypatch.setattr(
        pilot,
        "_assert_current_raycluster_lease",
        lambda *_args: events.append("raycluster"),
    )
    monkeypatch.setattr(
        pilot,
        "_run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(pilot.PilotError("exec failed")),
    )

    with pytest.raises(pilot.PilotError, match="exec failed"):
        pilot._kubectl_exec_json(
            "docker-desktop",
            namespace_lease,
            _raycluster_lease(namespace_lease),
            "head",
            ["inspect-cluster-state"],
            timeout_seconds=60,
        )

    assert events == ["namespace", "raycluster", "raycluster", "namespace"]


def test_explicitly_absent_namespace_returns_atomic_create_response_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _namespace_lease()
    commands: list[list[str]] = []
    inputs: list[str | None] = []
    responses = iter(
        (
            pilot.CommandResult("", "", 0),
            pilot.CommandResult(json.dumps(_namespace_record(lease)), "", 0),
        )
    )

    def fake_run(command: list[str], **kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        inputs.append(kwargs.get("input_text"))
        return next(responses)

    monkeypatch.setattr(pilot, "_run_command", fake_run)
    monkeypatch.setattr(pilot.secrets, "token_hex", lambda _bytes: lease.run_token)

    observed = pilot._ensure_namespace("docker-desktop", pilot.PILOT_NAMESPACE)

    assert observed == lease
    assert "--ignore-not-found=true" in commands[0]
    assert "create" in commands[1]
    assert "apply" not in commands[1]
    assert commands[1][-2:] == ["-o", "json"]
    assert json.loads(inputs[1] or "") == {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": pilot.PILOT_NAMESPACE,
            "labels": {
                pilot.PILOT_PROFILE_LABEL_KEY: pilot.PROFILE_NAME,
                pilot.PILOT_RUN_LABEL_KEY: lease.run_token,
            },
        },
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {"name": pilot.PILOT_NAMESPACE, "labels": {"owner": "somebody-else"}},
        {
            "name": pilot.PILOT_NAMESPACE,
            "uid": "stale-pilot-uid",
            "labels": {
                pilot.PILOT_PROFILE_LABEL_KEY: pilot.PROFILE_NAME,
                pilot.PILOT_RUN_LABEL_KEY: "2" * 32,
            },
        },
        {
            "name": pilot.PILOT_NAMESPACE,
            "deletionTimestamp": "2026-07-21T12:00:00Z",
            "labels": {pilot.PILOT_PROFILE_LABEL_KEY: pilot.PROFILE_NAME},
        },
    ],
)
def test_every_preexisting_namespace_is_refused_without_claim_or_mutation(
    metadata: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        return pilot.CommandResult(json.dumps({"metadata": metadata}), "", 0)

    monkeypatch.setattr(pilot, "_run_command", fake_run)

    with pytest.raises(pilot.PilotError, match="already exists"):
        pilot._ensure_namespace("docker-desktop", pilot.PILOT_NAMESPACE)

    assert len(commands) == 1


def test_namespace_create_race_returns_no_lease_and_does_not_retry_or_adopt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        if len(commands) == 1:
            return pilot.CommandResult("", "", 0)
        raise pilot.PilotError("command failed (1): kubectl\nAlreadyExists")

    monkeypatch.setattr(pilot, "_run_command", fake_run)

    with pytest.raises(pilot.PilotError, match="AlreadyExists"):
        pilot._ensure_namespace("docker-desktop", pilot.PILOT_NAMESPACE)

    assert len(commands) == 2
    assert "create" in commands[1]


def test_host_runner_never_cleans_up_when_namespace_lease_was_not_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    image = f"{pilot.PILOT_IMAGE_REPOSITORY}:{revision[:12]}"
    image_id = f"sha256:{'b' * 64}"
    monkeypatch.setattr(pilot, "_validate_host_target", lambda *_args: {})
    monkeypatch.setattr(pilot, "_git_source_revision", lambda: revision)
    monkeypatch.setattr(
        pilot,
        "_build_image",
        lambda _revision: (image, image_id, {"context": "desktop-linux"}),
    )
    monkeypatch.setattr(
        pilot,
        "_run_near_neighbor_container",
        lambda *_args: {"status": "success"},
    )
    monkeypatch.setattr(
        pilot,
        "_ensure_namespace",
        lambda *_args: (_ for _ in ()).throw(pilot.PilotError("AlreadyExists")),
    )
    monkeypatch.setattr(
        pilot,
        "_cleanup_namespace",
        lambda *_args: pytest.fail("unproven namespace ownership must never trigger cleanup"),
    )

    with pytest.raises(pilot.PilotError, match="AlreadyExists"):
        pilot.run_host_pilot(
            "docker-desktop",
            pilot.PILOT_NAMESPACE,
            keep_cluster=False,
        )


@pytest.mark.parametrize("mutation", ["name", "uid", "profile", "run_token", "deleting"])
def test_cleanup_refuses_namespace_outside_exact_live_lease(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _namespace_lease()
    record = _namespace_record(lease)
    metadata = record["metadata"]
    if mutation == "name":
        metadata["name"] = "replacement-namespace"
    elif mutation == "uid":
        metadata["uid"] = "replacement-uid"
    elif mutation == "profile":
        metadata["labels"][pilot.PILOT_PROFILE_LABEL_KEY] = "another-profile"
    elif mutation == "run_token":
        metadata["labels"][pilot.PILOT_RUN_LABEL_KEY] = "2" * 32
    else:
        metadata["deletionTimestamp"] = "2026-07-21T12:00:00Z"
    responses = iter(
        [
            pilot.CommandResult(stdout="docker-desktop\n", stderr="", returncode=0),
            pilot.CommandResult(stdout=json.dumps(record), stderr="", returncode=0),
        ]
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(pilot, "_run_command", fake_run)

    with pytest.raises(pilot.PilotError, match="outside the exact pilot lease"):
        pilot._cleanup_namespace("docker-desktop", lease)

    assert not any("delete" in command for command in commands)


def test_cleanup_treats_only_an_explicit_not_found_as_already_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            pilot.CommandResult("docker-desktop\n", "", 0),
            pilot.CommandResult("", "", 0),
        )
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(pilot, "_run_command", fake_run)

    pilot._cleanup_namespace("docker-desktop", _namespace_lease())

    assert "--ignore-not-found=true" in commands[1]
    assert not any("delete" in command for command in commands)


def test_cleanup_does_not_misclassify_namespace_api_failure_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            pilot.CommandResult("docker-desktop\n", "", 0),
            pilot.CommandResult("", "Unauthorized", 1),
        )
    )
    monkeypatch.setattr(pilot, "_run_command", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(pilot.PilotError, match="before cleanup"):
        pilot._cleanup_namespace("docker-desktop", _namespace_lease())


def test_cleanup_deletes_owned_namespace_and_verifies_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _namespace_lease()
    owned = json.dumps(_namespace_record(lease))
    responses = iter(
        (
            pilot.CommandResult("docker-desktop\n", "", 0),
            pilot.CommandResult(owned, "", 0),
            pilot.CommandResult("namespace deleted", "", 0),
            pilot.CommandResult("", "", 0),
        )
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(pilot, "_run_command", fake_run)

    pilot._cleanup_namespace("docker-desktop", lease)

    assert "delete" in commands[2]
    assert "--selector" in commands[2]
    assert commands[2][commands[2].index("--field-selector") + 1] == (
        f"metadata.name={pilot.PILOT_NAMESPACE}"
    )
    selector = commands[2][commands[2].index("--selector") + 1]
    assert f"{pilot.PILOT_PROFILE_LABEL_KEY}={pilot.PROFILE_NAME}" in selector
    assert f"{pilot.PILOT_RUN_LABEL_KEY}={lease.run_token}" in selector
    assert commands[3][-2:] == ["-o", "json"]


@pytest.mark.parametrize("replacement", [False, True])
def test_cleanup_fails_when_exact_or_replacement_namespace_still_exists_after_delete(
    replacement: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _namespace_lease()
    remaining_lease = _namespace_lease(uid="replacement-uid") if replacement else lease
    owned = json.dumps(_namespace_record(lease))
    responses = iter(
        (
            pilot.CommandResult("docker-desktop\n", "", 0),
            pilot.CommandResult(owned, "", 0),
            pilot.CommandResult("namespace deleted", "", 0),
            pilot.CommandResult(json.dumps(_namespace_record(remaining_lease)), "", 0),
        )
    )
    monkeypatch.setattr(pilot, "_run_command", lambda *_args, **_kwargs: next(responses))

    error = "replaced" if replacement else "UID still exists"
    with pytest.raises(pilot.PilotError, match=error):
        pilot._cleanup_namespace("docker-desktop", lease)


def test_probe_source_keeps_nested_owner_retries_and_result_bounds_explicit() -> None:
    source = Path(pilot.__file__).read_text(encoding="utf-8")

    assert "ray.remote(num_cpus=0, max_retries=0)(_nested_probe_owner)" in source
    assert source.count("_max_inflight_executions=1") == 3
    assert source.count("_max_buffered_results=1") == 3
    assert '"unconsumed_results": unconsumed' in source
    assert '"timed_out_result_discarded_by_teardown": True' in source
    assert "compiled.teardown(kill_actors=True)" in source
    assert "os.killpg(process.pid, signal.SIGKILL)" in source
    assert '"hard_timeout_observed": True' in source
    assert '"child_process_group_empty": True' in source
    assert "run_compiled_graph_probe(" in source
    assert "_run_probe_child_control_record" in source
    assert "os.environ.items" not in source


def test_generic_actor_ping_failure_is_not_misclassified_as_actor_death() -> None:
    class RayActorError(Exception):
        pass

    class RemoteMethod:
        @staticmethod
        def remote() -> object:
            return object()

    fake_ray = SimpleNamespace(
        exceptions=SimpleNamespace(RayActorError=RayActorError),
        get=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("transient")),
    )
    actor = SimpleNamespace(ping=RemoteMethod())

    with pytest.raises(pilot.PilotError, match="not RayActorError"):
        pilot._dead_actor_observation(fake_ray, "actor-name", actor)


def test_native_observation_validation_binds_every_policy_dimension() -> None:
    profile = pilot._load_profile()
    expected_runtime = profile["runtime_expectations"]
    revision = "a" * 40
    image_id = f"sha256:{'b' * 64}"
    configuration_id = pilot._configuration_identity()
    dependencies = profile["dependency_profile"]
    identity = {
        "ray_version": profile["ray_version"],
        "python_version": profile["python_version"],
        "operating_system": expected_runtime["operating_system"],
        "architecture": expected_runtime["architecture"],
        "python_implementation": expected_runtime["python_implementation"],
        "python_abi": expected_runtime["python_abi"],
        "dependency_profile": ";".join(
            f"{name}={dependencies[name]}"
            for name in (
                "ray",
                "numpy",
                "pyarrow",
                "cupy",
                "cupy-cuda11x",
                "cupy-cuda12x",
            )
        ),
        "platform_profile": expected_runtime["platform_profile"],
        "libc_profile": "glibc-2.35",
        "container_profile": f"{pilot.PILOT_IMAGE_REPOSITORY}:{revision[:12]}@{image_id}",
        "deployment_profile": configuration_id,
        "shared_memory_profile": "tmpfs:/dev/shm:size=536870912",
        "object_store_profile": "plasma:268435456",
    }
    payload = {
        "runtime": {
            "dependencies": dependencies,
            "python_version": profile["python_version"],
            "python_implementation": "CPython",
            "kernel": expected_runtime["kernel_release"],
            "machine": expected_runtime["architecture"],
            "libc": expected_runtime["libc"],
            "os_release": expected_runtime["os_release"],
            "shared_memory_bytes": 536_870_912,
            "alive_ray_nodes": 3,
            "cluster_resources": {"CPU": 2.0, "object_store_memory": 805_306_368.0},
            "source_revision": revision,
            "image_id": image_id,
            "configuration_id": configuration_id,
            "kuberay_version": "1.6.2",
            "runtime_identity": identity,
        }
    }
    decision = SimpleNamespace(
        runtime=SimpleNamespace(asdict=lambda: identity),
        topology="direct-driver",
        submission_transport="direct-ray-core",
        transport="cpu-shared-memory",
    )

    pilot._validate_native_observation(payload, decision, "direct-driver")

    for observed_fastrlock in ("absent", "0.8.2", None):
        changed_dependencies = dict(dependencies)
        if observed_fastrlock is None:
            changed_dependencies.pop("fastrlock")
        else:
            changed_dependencies["fastrlock"] = observed_fastrlock
        payload["runtime"]["dependencies"] = changed_dependencies
        with pytest.raises(pilot.PilotError, match="native dependency profile changed"):
            pilot._validate_native_observation(payload, decision, "direct-driver")
    payload["runtime"]["dependencies"] = dependencies

    payload["runtime"]["cluster_resources"]["object_store_memory"] = 805_306_369.0
    with pytest.raises(pilot.PilotError, match="object-store resource changed"):
        pilot._validate_native_observation(payload, decision, "direct-driver")
    payload["runtime"]["cluster_resources"]["object_store_memory"] = 805_306_368.0

    payload["runtime"]["kernel"] = "changed"
    with pytest.raises(pilot.PilotError, match="kernel release changed"):
        pilot._validate_native_observation(payload, decision, "direct-driver")


def test_native_observation_recomputes_checkout_independent_configuration_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _materialize_pilot_assets(
        tmp_path,
        monkeypatch,
        line_endings="lf",
    )
    profile = pilot._load_profile()
    revision = "a" * 40
    image_id = f"sha256:{'b' * 64}"
    configuration_id = pilot._configuration_identity()
    record_identity = {
        "source_revision": revision,
        "image_id": image_id,
        "configuration_id": configuration_id,
    }
    decision = pilot._expected_retained_decision(
        profile,
        record_identity,
        "direct-driver",
        shared_memory_bytes=profile["cluster"]["shared_memory_bytes_per_pod"],
    )
    payload = {
        "runtime": _valid_native_runtime(
            profile,
            revision,
            image_id,
            configuration_id,
            decision.runtime.asdict(),
        )
    }

    _rewrite_pilot_asset_line_endings(paths, line_endings="mixed-crlf")
    pilot._validate_native_observation(payload, decision, "direct-driver")

    with paths["DOCKERFILE_PATH"].open("ab") as dockerfile:
        dockerfile.write(b"# changed content\n")
    with pytest.raises(pilot.PilotError, match="native configuration identity changed"):
        pilot._validate_native_observation(payload, decision, "direct-driver")


def test_sanitized_probe_outcome_omits_arbitrary_process_output() -> None:
    secret = "SENTINEL-MUST-NOT-BE-RETAINED"
    decision = SimpleNamespace(asdict=lambda: {"reason": "CANDIDATE_REQUIRES_SMOKE"})
    outcome = SimpleNamespace(
        schema_version=2,
        status=pilot.CompiledGraphProbeStatus.SUCCESS,
        successful=True,
        duration_seconds=1.23456789,
        exit_code=0,
        termination_signal=None,
        native_exit_code=None,
        decision=decision,
        stdout_tail=secret,
        stderr_tail=secret,
        error_message=secret,
        traceback_tail=secret,
        details={"arbitrary": secret},
    )

    retained = pilot._sanitized_probe_outcome(outcome)
    serialized = json.dumps(retained, sort_keys=True)

    assert secret not in serialized
    for forbidden_field in (
        "stdout_tail",
        "stderr_tail",
        "error_message",
        "traceback_tail",
        "details",
    ):
        assert forbidden_field not in retained


def test_runtime_cleanup_requires_shared_memory_and_object_state_to_return() -> None:
    snapshot = {
        "pod": {
            "shared_memory": {
                "total_bytes": 536_870_912,
                "available_bytes": 400_000_000,
                "entry_count": 0,
                "entry_bytes": 0,
                "entry_identity_digest": "a" * 64,
            },
            "pilot_child_process_count": 0,
        }
    }
    result = pilot._verify_runtime_cleanup(snapshot, json.loads(json.dumps(snapshot)))
    assert result["shared_memory_entries_restored"] is True

    after = json.loads(json.dumps(snapshot))
    after["pod"]["shared_memory"]["entry_count"] = 2
    with pytest.raises(pilot.PilotError, match="exact shared-memory entries"):
        pilot._verify_runtime_cleanup(snapshot, after)

    after = json.loads(json.dumps(snapshot))
    after["pod"]["pilot_child_process_count"] = 1
    with pytest.raises(pilot.PilotError, match="retained a pilot child process"):
        pilot._verify_runtime_cleanup(snapshot, after)

    cluster = {
        "active_pilot_actor_count": 0,
        "active_pilot_task_count": 0,
        "object_count": 0,
        "object_bytes": 0,
        "object_identity_digest": "b" * 64,
    }
    assert pilot._verify_cluster_cleanup(cluster, dict(cluster))["object_count_delta"] == 0
    with pytest.raises(pilot.PilotError, match="object-store identity"):
        pilot._verify_cluster_cleanup(
            cluster,
            {**cluster, "object_identity_digest": "c" * 64},
        )
    with pytest.raises(pilot.PilotError, match="pilot tasks remained active"):
        pilot._verify_cluster_cleanup(cluster, {**cluster, "active_pilot_task_count": 1})
    with pytest.raises(pilot.PilotError, match="retained object-store results"):
        pilot._verify_cluster_cleanup(cluster, {**cluster, "object_count": 1})


def test_runtime_cleanup_blocker_retains_exact_sanitized_observations() -> None:
    pair_id = "1234-1721590000000000000"
    raw_names = [f"sem.hdr{pair_id}", f"sem.obj{pair_id}"]
    before = {"worker": _runtime_snapshot([])}
    after = {
        "worker": _runtime_snapshot(
            [(name, 32) for name in raw_names],
            available_bytes=524_763_136,
        )
    }
    observations = _cleanup_observations(before, [after, after, after, after])

    assessment = pilot._finalize_runtime_cleanup_assessment(before, observations)

    assert assessment["status"] == "failure"
    assert assessment["failure_classification"] == (pilot.MUTABLE_OBJECT_CLEANUP_CLASSIFICATION)
    assert assessment["failure_reasons"] == [
        "shared_memory_available_bytes_not_restored",
        "shared_memory_entries_not_restored",
    ]
    assert assessment["pilot_child_processes_remaining"] == 0
    assert assessment["stable_paired_semaphore_fingerprints"] is True
    assert assessment["ray_mutable_object_semaphore_pair_count"] == 1
    retained = assessment["pods"]["worker"]
    assert retained["before"]["entry_count"] == 0
    assert retained["after"]["entry_count"] == 2
    assert retained["deltas"] == {
        "available_bytes": -12_099_584,
        "entry_count": 2,
        "entry_bytes": 64,
    }
    serialized = json.dumps(observations, sort_keys=True)
    assert all(name not in serialized for name in raw_names)


@pytest.mark.parametrize(
    "snapshot_sets",
    [
        [
            [
                ("sem.hdr1234-1721590000000000000", 32),
                ("sem.obj1234-1721590000000000000", 32),
                ("unrelated-shared-memory-entry", 8),
            ]
        ]
        * 4,
        [[("sem.hdr1234-1721590000000000000", 32)]] * 4,
        [
            [
                ("sem.hdr1234-1721590000000000000", 32),
                ("sem.obj1234-1721590000000000000", 32),
            ],
            [
                ("sem.hdr1234-1721590000000000001", 32),
                ("sem.obj1234-1721590000000000001", 32),
            ],
            [
                ("sem.hdr1234-1721590000000000001", 32),
                ("sem.obj1234-1721590000000000001", 32),
            ],
            [
                ("sem.hdr1234-1721590000000000001", 32),
                ("sem.obj1234-1721590000000000001", 32),
            ],
        ],
    ],
    ids=("stray-entry", "unpaired-semaphore", "churning-pair"),
)
def test_runtime_cleanup_does_not_misclassify_nonstable_ray_semaphores(
    snapshot_sets: list[list[tuple[str, int]]],
) -> None:
    before = {"worker": _runtime_snapshot([])}
    snapshots = [
        {
            "worker": _runtime_snapshot(
                entries,
                available_bytes=524_763_136,
            )
        }
        for entries in snapshot_sets
    ]
    observations = _cleanup_observations(before, snapshots)

    assessment = pilot._finalize_runtime_cleanup_assessment(before, observations)

    assert assessment["failure_classification"] == "runtime_cleanup_invariant_failed"
    assert assessment["stable_paired_semaphore_fingerprints"] is False
    assert (
        "shared_memory_residual_not_stable_paired_ray_semaphores" in assessment["failure_reasons"]
    )


def test_runtime_cleanup_observation_waits_before_classifying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = {"worker": _runtime_snapshot([])}
    residual = {
        "worker": _runtime_snapshot(
            [
                ("sem.hdr1234-1721590000000000000", 32),
                ("sem.obj1234-1721590000000000000", 32),
            ],
            available_bytes=524_763_136,
        )
    }
    snapshots = iter((residual, before))
    waits: list[int] = []
    monkeypatch.setattr(
        pilot,
        "_capture_pod_runtime_snapshots",
        lambda *_args: next(snapshots),
    )
    monkeypatch.setattr(pilot.time, "sleep", waits.append)

    after, assessment, observations = pilot._observe_runtime_cleanup(
        "docker-desktop",
        _namespace_lease(),
        _raycluster_lease(),
        [],
        before,
    )

    assert after == before
    assert assessment["status"] == "success"
    assert waits == [5]
    assert [item["cumulative_wait_seconds"] for item in observations] == [0, 5]


def test_blocked_record_requires_zero_other_residuals_and_links_trackers() -> None:
    result = _valid_blocked_record()

    assert result["status"] == "blocked"
    assert result["promotion_eligible"] is False
    assert result["zero_residual_state"] == {
        "active_pilot_actor_count": 0,
        "active_pilot_task_count": 0,
        "object_count": 0,
        "object_bytes": 0,
        "pilot_child_process_count": 0,
    }
    assert result["failure"]["tracker_urls"] == list(pilot.BLOCKER_TRACKERS)
    pilot._validate_current_blocked_evidence_record(
        result,
        require_namespace_deleted=True,
    )


@pytest.mark.parametrize("field", sorted(pilot.BLOCKED_EVIDENCE_ROOT_KEYS))
def test_blocked_evidence_requires_every_root_field(field: str) -> None:
    record = _valid_blocked_record()
    del record[field]

    with pytest.raises(pilot.PilotError, match="root schema is not exact"):
        pilot._validate_current_blocked_evidence_record(
            record,
            require_namespace_deleted=True,
        )


def test_blocked_evidence_rejects_an_unallowlisted_root_field() -> None:
    record = _valid_blocked_record()
    record["raw_pod_logs"] = "must not be retained"

    with pytest.raises(pilot.PilotError, match="root schema is not exact"):
        pilot._validate_current_blocked_evidence_record(
            record,
            require_namespace_deleted=True,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "profile_exact",
        "profile_bool_coercion",
        "root_schema_type",
        "started_at_timezone",
        "timestamp_order",
        "configuration_id",
        "rendered_manifest_id",
        "kubernetes_context",
        "namespace",
        "namespace_lease_uid",
        "namespace_lease_token",
        "raycluster_lease_uid",
        "raycluster_namespace_uid",
        "raycluster_run_token",
        "docker_extra",
        "docker_engine_extra",
        "kuberay_operator_extra",
        "kuberay_operator_restart",
        "kuberay_operator_digest",
        "kuberay_operator_owner",
        "kubernetes_extra",
        "node_extra",
        "capacity_resource_extra",
        "capacity_value_type",
        "pod_extra",
        "pod_resources",
        "pod_node_selector",
        "pod_shared_memory",
        "pod_identity_environment",
        "pod_ray_start",
        "pod_ray_start_bool_coercion",
        "cleanup_extra",
        "unrelated_namespace",
        "runtime_extra",
        "shared_memory_extra",
        "cluster_state_extra",
        "native_runtime_extra",
        "zero_state_bool_coercion",
    ],
)
def test_blocked_evidence_rejects_mutated_nested_context(mutation: str) -> None:
    record = _valid_blocked_record()
    if mutation == "profile_exact":
        record["profile"]["ray_version"] = "2.55.0"
        record["profile_id"] = pilot._profile_identity(record["profile"])
    elif mutation == "profile_bool_coercion":
        record["profile"]["schema_version"] = True
        record["profile_id"] = pilot._profile_identity(record["profile"])
    elif mutation == "root_schema_type":
        record["schema_version"] = True
    elif mutation == "started_at_timezone":
        record["started_at"] = "2026-07-21T12:00:00-07:00"
    elif mutation == "timestamp_order":
        record["completed_at"] = "2026-07-21T11:59:59+00:00"
    elif mutation == "configuration_id":
        record["configuration_id"] = f"sha256:{'0' * 64}"
    elif mutation == "rendered_manifest_id":
        record["rendered_manifest_id"] = f"sha256:{'0' * 64}"
    elif mutation == "kubernetes_context":
        record["kubernetes_context"] = "another-context"
    elif mutation == "namespace":
        record["namespace"] = "another-namespace"
    elif mutation == "namespace_lease_uid":
        record["namespace_lease"]["uid"] = ""
    elif mutation == "namespace_lease_token":
        record["namespace_lease"]["run_token"] = "not-a-run-token"
    elif mutation == "raycluster_lease_uid":
        record["raycluster_lease"]["uid"] = ""
    elif mutation == "raycluster_namespace_uid":
        record["raycluster_lease"]["namespace_uid"] = "replacement-namespace-uid"
    elif mutation == "raycluster_run_token":
        record["raycluster_lease"]["run_token"] = "2" * 32
    elif mutation == "docker_extra":
        record["docker"]["raw_inspect"] = {}
    elif mutation == "docker_engine_extra":
        record["docker"]["engine"]["raw_info"] = "unsafe"
    elif mutation == "kuberay_operator_extra":
        record["kuberay_operator"]["pod_name"] = "raw-operator-name"
    elif mutation == "kuberay_operator_restart":
        record["kuberay_operator"]["restart_count"] = -1
    elif mutation == "kuberay_operator_digest":
        record["kuberay_operator"]["image_id"] = f"sha256:{'0' * 64}"
    elif mutation == "kuberay_operator_owner":
        record["kuberay_operator"]["controller_chain_verified"] = False
    elif mutation == "kubernetes_extra":
        record["kubernetes"]["raw_version"] = {}
    elif mutation == "node_extra":
        record["kubernetes"]["node"]["raw_labels"] = {}
    elif mutation == "capacity_resource_extra":
        record["kubernetes"]["node"]["capacity"]["example.com/device"] = "1"
        record["kubernetes"]["node"]["allocatable"]["example.com/device"] = "1"
    elif mutation == "capacity_value_type":
        record["kubernetes"]["node"]["capacity"]["cpu"] = 8
    elif mutation == "pod_extra":
        record["pods"]["before"][0]["raw_spec"] = {}
    elif mutation == "pod_resources":
        record["pods"]["before"][0]["resources"]["requests"]["cpu"] = "999"
    elif mutation == "pod_node_selector":
        record["pods"]["before"][0]["node_selector"]["example.com/node"] = "changed"
    elif mutation == "pod_shared_memory":
        record["pods"]["before"][0]["shared_memory_volume"]["sizeLimit"] = "1Gi"
    elif mutation == "pod_identity_environment":
        record["pods"]["before"][0]["identity_environment"]["DJANGO_RAY_PILOT_IMAGE_ID"] = (
            f"sha256:{'0' * 64}"
        )
    elif mutation == "pod_ray_start":
        record["pods"]["before"][0]["ray_start_parameters"]["num-cpus"] = "99"
    elif mutation == "pod_ray_start_bool_coercion":
        record["pods"]["before"][0]["ray_start_parameters"]["num-cpus"] = False
    elif mutation == "cleanup_extra":
        record["cleanup"]["raw_cleanup_details"] = {}
    elif mutation == "unrelated_namespace":
        record["cleanup"]["unrelated_namespaces_touched"] = ["default"]
    elif mutation == "runtime_extra":
        record["pods"]["runtime_before"]["head"]["runtime"]["environment"] = {}
    elif mutation == "shared_memory_extra":
        record["pods"]["runtime_before"]["head"]["shared_memory"]["raw_names"] = []
    elif mutation == "cluster_state_extra":
        record["cleanup"]["cluster_state_before"]["raw_objects"] = []
    elif mutation == "native_runtime_extra":
        record["topologies"][0]["observation"]["runtime"]["environment"] = {}
    elif mutation == "zero_state_bool_coercion":
        record["zero_residual_state"]["object_count"] = False

    with pytest.raises(pilot.PilotError, match="evidence"):
        pilot._validate_current_blocked_evidence_record(
            record,
            require_namespace_deleted=True,
        )


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    [
        (("topologies", 0, "schema_version"), True),
        (
            ("topologies", 0, "hardened_subprocess", "schema_version"),
            float(pilot.PROBE_SCHEMA_VERSION),
        ),
        (("topologies", 0, "hardened_subprocess", "exit_code"), False),
        (("topologies", 0, "observation", "schema_version"), 1.0),
        (
            (
                "topologies",
                0,
                "observation",
                "payload",
                "suite",
                "normal",
                "max_inflight_executions",
            ),
            1.0,
        ),
        (
            (
                "topologies",
                0,
                "observation",
                "payload",
                "suite",
                "normal",
                "results_submitted",
            ),
            3.0,
        ),
        (
            (
                "topologies",
                0,
                "observation",
                "payload",
                "suite",
                "unconsumed_results",
            ),
            False,
        ),
        (
            ("topologies", 1, "observation", "payload", "owner_max_retries"),
            0.0,
        ),
        (("topologies", 0, "observation", "runtime", "shared_memory_bytes"), 536_870_912.0),
        (("topologies", 0, "observation", "runtime", "alive_ray_nodes"), 3.0),
        (("near_neighbor", "schema_version"), True),
        (("near_neighbor", "physical_shared_memory_bytes"), 268_435_456.0),
        (("hard_timeout", "schema_version"), 1.0),
        (("pods", "runtime_before", "head", "schema_version"), True),
        (("cleanup", "cluster_state_after", "schema_version"), 1.0),
        (("cleanup", "shared_memory_observations", 0, "attempt"), True),
        (("cleanup", "shared_memory_observations", 0, "wait_before_seconds"), 0.0),
        (
            (
                "cleanup",
                "shared_memory_observations",
                0,
                "assessment",
                "pilot_child_processes_remaining",
            ),
            False,
        ),
        (
            (
                "cleanup",
                "shared_memory_observations",
                0,
                "assessment",
                "pods",
                "head",
                "deltas",
                "entry_count",
            ),
            2.0,
        ),
    ],
)
def test_blocked_evidence_rejects_bool_and_float_integer_coercion(
    path: tuple[str | int, ...],
    invalid_value: Any,
) -> None:
    record = json.loads(json.dumps(_valid_blocked_record()))
    target: Any = record
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = invalid_value

    with pytest.raises(pilot.PilotError):
        pilot._validate_current_blocked_evidence_record(
            record,
            require_namespace_deleted=True,
        )


def test_blocked_evidence_writer_requires_clean_current_head_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _valid_blocked_record()
    monkeypatch.setattr(pilot, "_git_source_revision", lambda: "f" * 40)
    output = tmp_path / "compiled-graph-kuberay-blocked-2026-07-21.json"

    with pytest.raises(pilot.PilotError, match="clean current HEAD"):
        pilot._write_blocked_evidence(output, record)

    assert not output.exists()


def test_retained_context_and_writer_accept_checkout_eol_changes_but_not_content_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _materialize_pilot_assets(
        tmp_path / "assets",
        monkeypatch,
        line_endings="lf",
    )
    record = _valid_blocked_record()
    _rewrite_pilot_asset_line_endings(paths, line_endings="crlf")

    pilot._validate_current_blocked_evidence_record(
        record,
        require_namespace_deleted=True,
    )
    monkeypatch.setattr(pilot, "_git_source_revision", lambda: record["source_revision"])
    output = tmp_path / "compiled-graph-kuberay-blocked-2026-07-21.json"
    pilot._write_blocked_evidence(output, record)
    assert json.loads(output.read_text(encoding="utf-8")) == record

    with paths["DOCKERIGNORE_PATH"].open("ab") as policy:
        policy.write(b"# changed policy content\n")
    rejected_output = tmp_path / "compiled-graph-kuberay-blocked-2026-07-21-changed.json"
    with pytest.raises(pilot.PilotError, match="Docker context is inconsistent"):
        pilot._write_blocked_evidence(rejected_output, record)
    assert not rejected_output.exists()


def test_historical_blocked_evidence_remains_self_consistent_across_profile_versions() -> None:
    evidence_path = (
        pilot.ROOT / "docs" / "investigations" / "compiled-graph-kuberay-blocked-2026-07-21.json"
    )
    evidence_bytes = pilot._canonical_source_text_bytes(evidence_path)
    assert len(evidence_bytes) == 164_686
    assert (
        pilot.sha256(evidence_bytes).hexdigest()
        == "972d9d9ad3f39f2e97ebc9bd491cd5222a69cb39f01ec7c28578b7ae0976d702"
    )
    record = json.loads(evidence_bytes)

    pilot._validate_blocked_evidence_record(
        record,
        require_namespace_deleted=True,
    )
    with pytest.raises(
        pilot.PilotError,
        match="does not use the current tracked profile",
    ):
        pilot._validate_current_blocked_evidence_record(
            record,
            require_namespace_deleted=True,
        )


def test_exact_profile_admission_accepts_baseline_and_rejects_near_neighbor_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = pilot._load_profile()
    revision = "a" * 40
    image_id = f"sha256:{'b' * 64}"
    configuration_id = pilot._configuration_identity()
    expected_value = f"tmpfs:/dev/shm:size={profile['cluster']['shared_memory_bytes_per_pod']}"
    changed_value = "tmpfs:/dev/shm:size=268435456"
    baseline_identity = pilot._expected_policy_identity(
        profile,
        revision=revision,
        image_id=image_id,
        configuration_id=configuration_id,
        shared_memory_profile=expected_value,
    )
    changed_identity = {
        **baseline_identity,
        "shared_memory_profile": changed_value,
    }
    monkeypatch.setattr(
        pilot,
        "run_compiled_graph_probe",
        lambda *_args, **_kwargs: pytest.fail("profile mismatch must not invoke the probe"),
    )
    monkeypatch.setattr(
        pilot,
        "_require_exact_pilot_dependency_profile",
        lambda _profile: dict(_profile["dependency_profile"]),
    )
    monkeypatch.setattr(
        pilot,
        "detect_compiled_graph_runtime",
        lambda: SimpleNamespace(asdict=lambda: dict(changed_identity)),
    )
    monkeypatch.setattr(
        pilot.os,
        "statvfs",
        lambda _path: SimpleNamespace(f_frsize=1, f_blocks=268_435_456),
        raising=False,
    )
    monkeypatch.setenv(
        "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE",
        changed_value,
    )
    monkeypatch.setenv("DJANGO_RAY_PILOT_SOURCE_REVISION", revision)
    monkeypatch.setenv("DJANGO_RAY_PILOT_IMAGE_ID", image_id)
    monkeypatch.setenv("DJANGO_RAY_PILOT_CONFIG_ID", configuration_id)

    result = pilot._near_neighbor_guard()

    assert result["changed_dimension"] == "shared_memory_profile"
    assert result["changed_value"] == changed_value
    assert result["child_spawned"] is False
    assert result["native_started"] is False
    assert result["reason"] == pilot.PILOT_PROFILE_MISMATCH
    assert result["baseline_admission"]["classification"] == pilot.EXACT_PILOT_PROFILE_MATCH
    assert result["baseline_admission"]["admitted"] is True
    assert result["baseline_admission"]["changed_dimensions"] == []
    assert result["changed_admission"]["classification"] == pilot.PILOT_PROFILE_MISMATCH
    assert result["changed_admission"]["admitted"] is False
    assert result["changed_admission"]["changed_dimensions"] == ["shared_memory_profile"]
    assert (
        result["baseline_admission"]["decision"]["runtime"]["shared_memory_profile"]
        == expected_value
    )
    assert (
        result["changed_admission"]["decision"]["runtime"]["shared_memory_profile"] == changed_value
    )
    assert result["physical_resource_changed"] is True
    assert result["physical_shared_memory_bytes"] == 268_435_456
    assert result["pilot_dependency_profile"] == profile["dependency_profile"]
    assert os.environ["DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE"] == changed_value

    monkeypatch.setattr(
        pilot,
        "detect_compiled_graph_runtime",
        lambda: SimpleNamespace(asdict=lambda: dict(baseline_identity)),
    )
    with pytest.raises(pilot.PilotError, match="changed more than its shared-memory identity"):
        pilot._near_neighbor_guard()
    assert os.environ["DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE"] == changed_value


def test_physical_near_neighbor_keeps_pilot_dependencies_outside_policy_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = pilot._load_profile()
    expected_runtime = profile["runtime_expectations"]
    revision = "a" * 40
    image_id = f"sha256:{'b' * 64}"
    configuration_id = pilot._configuration_identity()
    changed_shared_memory_bytes = profile["cluster"]["shared_memory_bytes_per_pod"] // 2
    changed_value = f"tmpfs:/dev/shm:size={changed_shared_memory_bytes}"
    expected_policy_distributions = (
        "ray",
        "numpy",
        "pyarrow",
        "cupy",
        "cupy-cuda11x",
        "cupy-cuda12x",
    )
    versions = dict(profile["dependency_profile"])
    lookups: list[str] = []

    def version(name: str) -> str:
        lookups.append(name)
        value = versions[name]
        if value == "absent":
            raise compiled_graph.metadata.PackageNotFoundError(name)
        return value

    monkeypatch.setattr(compiled_graph.metadata, "version", version)
    monkeypatch.setattr(
        compiled_graph.platform,
        "python_version",
        lambda: profile["python_version"],
    )
    monkeypatch.setattr(compiled_graph.platform, "system", lambda: "Linux")
    monkeypatch.setattr(compiled_graph.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(compiled_graph.platform, "python_implementation", lambda: "CPython")
    monkeypatch.setattr(
        compiled_graph.platform,
        "platform",
        lambda: expected_runtime["platform_profile"],
    )
    monkeypatch.setattr(
        compiled_graph.platform,
        "libc_ver",
        lambda: tuple(expected_runtime["libc"]),
    )
    monkeypatch.setattr(
        compiled_graph.sysconfig,
        "get_config_var",
        lambda _name: expected_runtime["python_abi"],
    )
    monkeypatch.setattr(
        pilot,
        "run_compiled_graph_probe",
        lambda *_args, **_kwargs: pytest.fail(
            "physical profile mismatch must not invoke native compilation"
        ),
    )
    monkeypatch.setattr(
        pilot.os,
        "statvfs",
        lambda _path: SimpleNamespace(f_frsize=1, f_blocks=changed_shared_memory_bytes),
        raising=False,
    )
    monkeypatch.setenv(
        "DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE",
        f"{pilot.PILOT_IMAGE_REPOSITORY}:{revision[:12]}@{image_id}",
    )
    monkeypatch.setenv(
        "DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE",
        configuration_id,
    )
    monkeypatch.setenv("DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE", changed_value)
    monkeypatch.setenv(
        "DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE",
        f"plasma:{profile['cluster']['object_store_bytes_per_pod']}",
    )
    monkeypatch.setenv("DJANGO_RAY_PILOT_SOURCE_REVISION", revision)
    monkeypatch.setenv("DJANGO_RAY_PILOT_IMAGE_ID", image_id)
    monkeypatch.setenv("DJANGO_RAY_PILOT_CONFIG_ID", configuration_id)

    result = pilot._near_neighbor_guard()

    assert expected_policy_distributions == pilot._PROFILE_DISTRIBUTIONS
    assert lookups == [
        *profile["dependency_profile"],
        "ray",
        *expected_policy_distributions,
    ]
    assert result["pilot_dependency_profile"] == profile["dependency_profile"]
    assert result["changed_admission"]["changed_dimensions"] == ["shared_memory_profile"]
    assert (
        "fastrlock=" not in result["changed_admission"]["decision"]["runtime"]["dependency_profile"]
    )
    assert result["native_started"] is False

    monkeypatch.setattr(
        pilot,
        "detect_compiled_graph_runtime",
        lambda: pytest.fail("pilot dependency drift must fail before policy admission"),
    )
    for observed_fastrlock in ("absent", "0.8.2"):
        versions["fastrlock"] = observed_fastrlock
        with pytest.raises(
            pilot.PilotError,
            match="pilot dependency profile changed before native execution",
        ):
            pilot._near_neighbor_guard()


def test_probe_parent_checks_pilot_dependencies_before_native_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_dependencies(_profile: dict[str, Any]) -> dict[str, str]:
        raise pilot.PilotError("pilot dependency profile changed before native execution")

    monkeypatch.setattr(pilot, "_require_exact_pilot_dependency_profile", reject_dependencies)
    monkeypatch.setattr(
        pilot,
        "run_compiled_graph_probe",
        lambda *_args, **_kwargs: pytest.fail("dependency mismatch must prevent native spawn"),
    )

    with pytest.raises(
        pilot.PilotError,
        match="pilot dependency profile changed before native execution",
    ):
        pilot._run_probe_parent("direct-driver", 1)


def test_near_neighbor_container_is_physically_changed_and_network_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed_value = "tmpfs:/dev/shm:size=268435456"
    mutable_image = "django-ray-cgraph-pilot:revision"
    immutable_image_id = f"sha256:{'a' * 64}"
    commands: list[list[str]] = []
    observation = {
        "schema_version": 1,
        "status": "success",
        "reason": pilot.PILOT_PROFILE_MISMATCH,
        "physical_resource_changed": True,
    }

    def fake_run(command: list[str], **_kwargs: Any) -> pilot.CommandResult:
        commands.append(command)
        return pilot.CommandResult(json.dumps(observation), "", 0)

    monkeypatch.setattr(pilot, "_run_command", fake_run)

    result = pilot._run_near_neighbor_container(
        mutable_image,
        immutable_image_id,
        f"sha256:{'b' * 64}",
        {"context": "desktop-linux"},
    )

    command = commands[0]
    assert result == observation
    assert command[:4] == ["docker", "--context", "desktop-linux", "run"]
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--shm-size") + 1] == "268435456"
    assert "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE=" + changed_value in command
    assert command[command.index("--entrypoint") + 2] == immutable_image_id
    assert mutable_image not in command


def test_blocked_evidence_output_is_fresh_sanitized_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    investigations = tmp_path / "docs" / "investigations"
    investigations.mkdir(parents=True)
    output = investigations / "compiled-graph-kuberay-blocked-2026-07-21.json"
    record = _valid_blocked_record()
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    monkeypatch.setattr(pilot, "run_host_pilot", lambda *_args, **_kwargs: record)
    monkeypatch.setattr(pilot, "_git_source_revision", lambda: record["source_revision"])

    exit_code = pilot.main(
        [
            "run",
            "--context",
            "docker-desktop",
            "--blocked-evidence-output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == record
    assert json.loads(output.read_text(encoding="utf-8")) == record

    exit_code = pilot.main(
        [
            "run",
            "--context",
            "docker-desktop",
            "--blocked-evidence-output",
            str(output),
        ]
    )
    assert exit_code == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["status"] == "failure"
    assert "refusing to overwrite" in failure["error"]


@pytest.mark.parametrize(
    "mutation, error_match",
    [
        ("identity", "exact source revision"),
        ("profile", "profile identity"),
        ("topology", "both successful topology"),
        ("topology_minimal", "topology identity or policy proof"),
        ("topology_policy", "topology identity or policy proof"),
        ("topology_hardened", "hardened subprocess proof"),
        ("topology_runtime", "native configuration identity"),
        ("topology_dependency", "native dependency profile changed"),
        ("topology_suite", "teardown or accounting"),
        ("topology_result_accounting", "zero-result accounting"),
        ("near_neighbor", "near-neighbor proof"),
        ("near_neighbor_dependency", "near-neighbor proof"),
        ("hard_timeout", "hard-timeout containment proof"),
        ("pod_capture", "execution identity changed"),
        ("pod_init_inventory", "init-container identity"),
        ("pod_ray_parameters", "Ray start parameters"),
        ("pod_ray_switch_semantics", "Ray start parameters"),
        ("promotion", "promotion claim"),
        ("zero_state", "zero other residuals"),
        ("classification", "known upstream cleanup blocker"),
        ("semaphore_proof", "stable paired Ray semaphores"),
        ("namespace", "namespace deletion"),
        ("namespace_lease", "namespace lease"),
    ],
)
def test_blocked_evidence_writer_independently_revalidates_every_invariant(
    mutation: str,
    error_match: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _valid_blocked_record()
    monkeypatch.setattr(pilot, "_git_source_revision", lambda: record["source_revision"])
    if mutation == "identity":
        record["source_revision"] = "not-a-revision"
    elif mutation == "profile":
        record["profile_id"] = f"sha256:{'0' * 64}"
    elif mutation == "topology":
        record["topologies"] = record["topologies"][:1]
    elif mutation == "topology_minimal":
        record["topologies"][0] = {"status": "success", "topology": "direct-driver"}
    elif mutation == "topology_policy":
        record["topologies"][0]["decision"]["policy_version"] = 999
    elif mutation == "topology_hardened":
        del record["topologies"][0]["hardened_subprocess"]["bounded_private_control_record"]
    elif mutation == "topology_runtime":
        record["topologies"][0]["observation"]["runtime"]["configuration_id"] = f"sha256:{'0' * 64}"
    elif mutation == "topology_dependency":
        dependencies = record["topologies"][0]["observation"]["runtime"]["dependencies"]
        record["topologies"][0]["observation"]["runtime"]["dependencies"] = {
            **dependencies,
            "fastrlock": "0.8.2",
        }
    elif mutation == "topology_suite":
        record["topologies"][0]["observation"]["payload"]["suite"]["normal"][
            "teardown_returned"
        ] = False
    elif mutation == "topology_result_accounting":
        record["topologies"][0]["observation"]["payload"]["suite"]["unconsumed_results"] = 1
    elif mutation == "near_neighbor":
        record["near_neighbor"]["child_spawned"] = True
    elif mutation == "near_neighbor_dependency":
        dependencies = record["near_neighbor"]["pilot_dependency_profile"]
        record["near_neighbor"]["pilot_dependency_profile"] = {
            **dependencies,
            "fastrlock": "0.8.2",
        }
    elif mutation == "hard_timeout":
        record["hard_timeout"]["child_process_group_empty"] = False
    elif mutation == "pod_capture":
        record["pods"]["final_capture_after"][0]["uid"] = "replacement-uid"
    elif mutation in {
        "pod_init_inventory",
        "pod_ray_parameters",
        "pod_ray_switch_semantics",
    }:
        for group_name in ("before", "after", "final_capture_before", "final_capture_after"):
            pod_role = "head" if mutation == "pod_ray_switch_semantics" else "worker"
            worker = next(pod for pod in record["pods"][group_name] if pod["role"] == pod_role)
            if mutation == "pod_init_inventory":
                worker["init_containers"][0]["restart_count"] = 1
            elif mutation == "pod_ray_switch_semantics":
                worker["ray_start_parameters"]["disable-usage-stats"]["semantic_value"] = False
            else:
                worker["ray_start_parameters"]["num-cpus"] = "99"
        record["pods"]["identity"] = pilot._verify_pod_execution_identity_unchanged(
            record["pods"]["before"],
            record["pods"]["after"],
        )
        record["pods"]["final_capture_identity"] = pilot._verify_pod_execution_identity_unchanged(
            record["pods"]["final_capture_before"],
            record["pods"]["final_capture_after"],
        )
    elif mutation == "promotion":
        record["promotion_eligible"] = True
    elif mutation == "zero_state":
        record["zero_residual_state"]["object_count"] = 1
    elif mutation == "classification":
        record["failure"]["classification"] = "arbitrary_shared_memory_residue"
    elif mutation == "semaphore_proof":
        observation = record["cleanup"]["shared_memory_observations"][0]
        observation["pods"] = json.loads(json.dumps(observation["pods"]))
        pod_name = sorted(observation["pods"])[0]
        semaphore = observation["pods"][pod_name]["shared_memory"]["ray_mutable_object_semaphores"]
        semaphore["pair_identity_digest"] = "0" * 64
        observation["assessment"] = pilot._assess_runtime_cleanup(
            record["pods"]["runtime_before"],
            observation["pods"],
        )
    elif mutation == "namespace":
        record["cleanup"]["pilot_namespace_deleted"] = False
    elif mutation == "namespace_lease":
        record["namespace_lease"]["run_token"] = "invalid"

    with pytest.raises(pilot.PilotError, match=error_match):
        pilot._write_blocked_evidence(
            tmp_path / "compiled-graph-kuberay-blocked-2026-07-21.json",
            record,
        )


def test_blocked_evidence_output_rejects_keep_cluster_before_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "docs" / "investigations").mkdir(parents=True)
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    monkeypatch.setattr(
        pilot,
        "run_host_pilot",
        lambda *_args, **_kwargs: pytest.fail("pilot must not run"),
    )

    exit_code = pilot.main(
        [
            "run",
            "--context",
            "docker-desktop",
            "--keep-cluster",
            "--blocked-evidence-output",
            "docs/investigations/compiled-graph-kuberay-blocked-2026-07-21.json",
        ]
    )

    assert exit_code == 1
    assert "cannot be combined" in json.loads(capsys.readouterr().out)["error"]


def test_blocked_evidence_output_rejects_paths_outside_investigations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "docs" / "investigations").mkdir(parents=True)
    monkeypatch.setattr(pilot, "ROOT", tmp_path)

    with pytest.raises(pilot.PilotError, match="must stay under"):
        pilot._resolve_blocked_evidence_output(
            str(tmp_path / "compiled-graph-kuberay-blocked-2026-07-21.json")
        )


def test_internal_probe_failure_record_remains_parseable_for_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = {"schema_version": 1, "status": "hard_timeout", "topology": "direct-driver"}
    monkeypatch.setattr(pilot, "_run_probe_parent", lambda *_args: record)

    exit_code = pilot.main(
        [
            "probe",
            "--topology",
            "direct-driver",
            "--timeout-seconds",
            "1",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == record


@pytest.mark.skipif(os.name == "nt", reason="promotion-grade process-group proof is Linux-only")
def test_hard_timeout_self_test_reaps_its_process_group() -> None:
    result = pilot._run_hard_timeout_self_test(0.05)

    assert result["status"] == "success"
    assert result["hard_timeout_observed"] is True
    assert result["child_process_group_empty"] is True


def test_documentation_keeps_pilot_evidence_separate_from_promotion() -> None:
    compatibility = (pilot.ROOT / "docs" / "compiled-graph-compatibility.md").read_text(
        encoding="utf-8"
    )
    contributing = (pilot.ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")

    assert "django-ray-cgraph-kuberay-cpu-v1" in compatibility
    assert "--context docker-desktop" in compatibility
    assert "separate capability promotion" in compatibility
    assert "does not read or print Kubernetes Secrets" in compatibility
    assert "ray-project/ray/issues/43836" in compatibility
    assert "ray-project/ray/issues/59127" in compatibility
    assert "never promotion-eligible" in compatibility
    assert "Pinned Compiled Graph KubeRay Pilot (Opt-In)" in contributing
    assert "--blocked-evidence-output" in contributing
    assert "still exits nonzero" in contributing
    assert "A passing pilot remains candidate-native evidence" in contributing
