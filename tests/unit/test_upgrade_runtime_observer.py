"""Snapshot corroboration only; these tests do not create Kubernetes resources."""

import json
from copy import deepcopy
from typing import Any

import pytest

from qualification.upgrade.runtime_manifest import render_runtime_manifest
from qualification.upgrade.runtime_observer import (
    MAX_LOG_BYTES,
    ObserverError,
    collect_observer,
    corroborate_observer,
    read_owned_observer,
)
from tests.unit.test_upgrade_runtime_manifest import config


@pytest.fixture
def snapshots():
    expected = render_runtime_manifest(
        "observer",
        config(),
        build="baseline",
        database="primary",
        action="prepare",
        job_name="owned-observer",
    )
    job = deepcopy(expected)
    job["metadata"]["uid"] = "job-uid"
    job["status"] = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
    pod: dict[str, Any] = {"kind": "Pod", **deepcopy(expected["spec"]["template"])}
    pod["metadata"].update(
        name="observer-pod",
        namespace=expected["metadata"]["namespace"],
        uid="pod-uid",
        ownerReferences=[
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": "owned-observer",
                "uid": "job-uid",
                "controller": True,
            }
        ],
    )
    pod["status"] = {
        "phase": "Succeeded",
        "containerStatuses": [
            {
                "name": "observer",
                "restartCount": 0,
                "imageID": "containerd://"
                + expected["spec"]["template"]["spec"]["containers"][0]["image"],
                "state": {
                    "terminated": {
                        "exitCode": 0,
                        "startedAt": "2026-09-12T12:00:00Z",
                        "finishedAt": "2026-09-12T12:00:02Z",
                    }
                },
            }
        ],
    }
    env = {item["name"]: item.get("value") for item in pod["spec"]["containers"][0]["env"]}
    result = {
        "schema": 1,
        "action": "prepare",
        "pid": 1,
        "python": env["DJANGO_RAY_UPGRADE_PYTHON_VERSION"],
        "observed_at": "2026-09-12T12:00:01+00:00",
        "observations": {},
        "complete_upgrade_gate": False,
    }
    return expected, job, pod, result


def check(snapshots, raw=None):
    expected, job, pod, result = snapshots
    return corroborate_observer(
        expected, job, pod, json.dumps(result).encode() if raw is None else raw, job_uid="job-uid"
    )


def test_completed_owned_observer_retains_process_and_job_identity(snapshots):
    result = check(snapshots)
    assert result["pod_uid"] == "pod-uid" and result["job_uid"] == "job-uid"
    assert result["result"] == snapshots[3]
    assert result["complete_upgrade_gate"] is False


@pytest.mark.parametrize(
    "change",
    [
        "job-uid",
        "owner",
        "image",
        "args",
        "restart",
        "exit",
        "phase",
        "action",
        "python",
        "time",
        "pid",
        "gate",
        "sidecar",
    ],
)
def test_refuses_wrong_producer_or_incomplete_result(snapshots, change):
    _, job, pod, result = snapshots
    status = pod["status"]["containerStatuses"][0]
    if change == "job-uid":
        job["metadata"]["uid"] = "replacement"
    elif change == "owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "replacement"
    elif change == "image":
        status["imageID"] = "sha256:" + "0" * 64
    elif change == "args":
        pod["spec"]["containers"][0]["args"] = ["enqueue", "--case", "old-success"]
    elif change == "restart":
        status["restartCount"] = 1
    elif change == "exit":
        status["state"]["terminated"]["exitCode"] = 1
    elif change == "phase":
        pod["status"]["phase"] = "Running"
    elif change == "action":
        result["action"] = "enqueue"
    elif change == "python":
        result["python"] = "3.9.0"
    elif change == "time":
        result["observed_at"] = "2026-09-12T11:00:00Z"
    elif change == "pid":
        result["pid"] = True
    elif change == "gate":
        result["complete_upgrade_gate"] = True
    elif change == "sidecar":
        pod["spec"]["containers"].append({"name": "extra"})
    with pytest.raises(ObserverError, match="^upgrade-observer-refused$"):
        check(snapshots)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * (MAX_LOG_BYTES + 1),
        b'{"schema":1,"schema":1}',
        b"{}\n{}",
        b'{"step_failed":true}',
    ],
    ids=["missing", "oversized", "duplicate-keys", "multiple", "failed"],
)
def test_refuses_missing_duplicate_failed_or_oversized_logs(snapshots, raw):
    with pytest.raises(ObserverError):
        check(snapshots, raw)


@pytest.mark.parametrize("fraction", ["000001", "999999"])
def test_accepts_result_within_final_kubernetes_timestamp_second(snapshots, fraction):
    snapshots[3]["observed_at"] = f"2026-09-12T12:00:02.{fraction}+00:00"
    assert check(snapshots)["result"] == snapshots[3]


def test_rejects_result_at_next_second_boundary(snapshots):
    snapshots[3]["observed_at"] = "2026-09-12T12:00:03+00:00"
    with pytest.raises(ObserverError):
        check(snapshots)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "infinity", "negative-infinity"],
)
def test_rejects_non_json_numbers_inside_observations(snapshots, value):
    snapshots[3]["observations"] = {"value": value}
    with pytest.raises(ObserverError):
        check(snapshots)


def test_rejects_contradictory_job_completion(snapshots):
    snapshots[1]["status"]["conditions"].append({"type": "Failed", "status": "True"})
    with pytest.raises(ObserverError):
        check(snapshots)


def responses(snapshots):
    expected, job, pod, result = snapshots
    namespace = {"metadata": {"name": expected["metadata"]["namespace"], "uid": "namespace-uid"}}
    return [
        namespace,
        job,
        {"items": [pod]},
        result,
        deepcopy(pod),
        deepcopy(job),
        deepcopy(namespace),
    ]


def collect(snapshots, documents):
    calls = []
    values = iter(documents)

    def read(arguments):
        calls.append(arguments)
        return json.dumps(next(values)).encode()

    result = collect_observer(
        read,
        context="admitted-context",
        namespace_uid="namespace-uid",
        expected=snapshots[0],
        job_uid="job-uid",
    )
    return result, calls


def test_collection_rechecks_owned_resources_after_bounded_logs(snapshots):
    result, calls = collect(snapshots, responses(snapshots))
    assert result == check(snapshots)
    assert len(calls) == 7
    assert all(
        call[:3] == ("--context", "admitted-context", "--request-timeout=15s") for call in calls
    )
    assert calls[3][-1] == "--limit-bytes=" + str(MAX_LOG_BYTES + 1)
    assert [call[3] for call in calls] == ["get", "get", "get", "logs", "get", "get", "get"]


@pytest.mark.parametrize(
    "change", ["namespace", "job", "pod", "duplicate-pod", "terminating-namespace"]
)
def test_collection_refuses_replacement_or_ambiguous_resource(snapshots, change):
    documents = responses(snapshots)
    if change == "namespace":
        documents[6]["metadata"]["uid"] = "replacement"
    elif change == "job":
        documents[5]["metadata"]["uid"] = "replacement"
    elif change == "pod":
        documents[4]["metadata"]["uid"] = "replacement"
    elif change == "duplicate-pod":
        documents[2]["items"].append(deepcopy(documents[2]["items"][0]))
    else:
        documents[6]["metadata"]["deletionTimestamp"] = "2026-09-12T12:00:03Z"
    with pytest.raises(ObserverError, match="^upgrade-observer-collection-refused$"):
        collect(snapshots, documents)


def test_collection_suppresses_transport_failure_and_does_not_retry(snapshots):
    calls = []

    def read(arguments):
        calls.append(arguments)
        raise RuntimeError("private provider response")

    with pytest.raises(ObserverError, match="^upgrade-observer-collection-refused$"):
        collect_observer(
            read,
            context="admitted",
            namespace_uid="namespace-uid",
            expected=snapshots[0],
            job_uid="job-uid",
        )
    assert len(calls) == 1


def test_owned_reader_uses_fixed_executable_and_bounded_transport(snapshots, tmp_path, monkeypatch):
    from qualification.upgrade import runtime_database, runtime_observer

    executable = tmp_path / "kubectl"
    executable.write_bytes(b"not executed")
    monkeypatch.setattr(runtime_observer.platform, "system", lambda: "Linux")
    values = iter(responses(snapshots))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps(next(values)).encode()

    monkeypatch.setattr(runtime_database, "_run_client", run)
    environment = {"KUBECONFIG": "/owned/config"}
    result = read_owned_observer(
        kubectl=executable,
        directory=tmp_path,
        environment=environment,
        context="admitted",
        namespace_uid="namespace-uid",
        expected=snapshots[0],
        job_uid="job-uid",
    )
    assert result == check(snapshots) and len(calls) == 7
    for command, kwargs in calls:
        assert command[0] == str(executable.resolve())
        assert kwargs["timeout"] == 20 and kwargs["maximum"] == MAX_LOG_BYTES + 1
        assert kwargs["environment"] == environment and kwargs["environment"] is not environment


def test_owned_reader_refuses_non_linux_before_process_creation(snapshots, tmp_path, monkeypatch):
    from qualification.upgrade import runtime_database, runtime_observer

    monkeypatch.setattr(runtime_observer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        runtime_database, "_run_client", lambda *_args, **_kwargs: pytest.fail("started process")
    )
    with pytest.raises(ObserverError, match="^upgrade-observer-transport-refused$"):
        read_owned_observer(
            kubectl=tmp_path / "missing",
            directory=tmp_path,
            environment={},
            context="admitted",
            namespace_uid="namespace-uid",
            expected=snapshots[0],
            job_uid="job-uid",
        )
