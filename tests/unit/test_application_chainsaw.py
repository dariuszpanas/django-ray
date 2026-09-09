"""Public runner receipt and owned-cleanup contracts without external processes."""

import json
import subprocess

import pytest

from qualification.application import run_chainsaw as runner


def receipt(layer="application_setup", **changes):
    return json.dumps(
        {
            "schema_version": 1,
            "layer": layer,
            "status": "passed",
            "complete_application_gate": False,
            **changes,
        }
    ).encode()


def test_receipts_keep_exact_node_bytes_and_skip_nonreceipt_lines():
    nodes = receipt("generic_ray_nodes")
    core = receipt("application_core")
    assert runner.parse_receipts(
        b"Ray connected\n" + nodes + b"\n" + core, ("before-nodes", "before-core")
    ) == {
        "before-nodes": nodes,
        "before-core": core,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        receipt() + b"\n" + receipt(),
        b"{",
        b"x" * 65537,
        receipt(status="failed"),
        receipt(schema_version=True),
        receipt(complete_application_gate=True),
        receipt("application_core"),
        receipt(unbounded="x" * 16384),
    ],
    ids=[
        "missing",
        "duplicate",
        "malformed",
        "large-log",
        "failed",
        "bool-schema",
        "full-gate",
        "wrong-layer",
        "large-receipt",
    ],
)
def test_receipts_refuse_partial_duplicate_wrong_or_unbounded_evidence(raw):
    with pytest.raises(ValueError):
        runner.parse_receipts(raw, ("setup",))


@pytest.mark.parametrize(
    "failure", [None, "test", "timeout", "receipt", "cleanup", "ownership", "source"]
)
def test_runner_requires_assertions_source_and_owned_namespace_cleanup(
    tmp_path, monkeypatch, failure
):
    output = tmp_path / "evidence"
    calls = []
    trees = 0

    def checked(argv, *, data=None, timeout=40):
        nonlocal trees
        calls.append(argv)
        if argv[0] == "git":
            if "rev-parse" in argv:
                trees += 1
                return b"b" if failure == "source" and trees > 1 else b"a"
            return b""
        if argv[0] == "chainsaw":
            return b"Version: 0.2.15\n"
        assert argv[:4] == ["kubectl", "--context", "admitted-test", "--request-timeout=30s"]
        if "create" in argv and "-f" in argv:
            secret = json.loads(data)
            assert set(secret["stringData"]) == set(runner.CREDENTIAL_KEYS)
            assert all(len(value) == 43 for value in secret["stringData"].values())
            assert not any(value in " ".join(argv) for value in secret["stringData"].values())
            return b""
        if "delete" in argv:
            assert "--timeout=180s" in argv
            if failure == "cleanup":
                raise RuntimeError("private API response")
            return b""
        if "--ignore-not-found" in argv:
            return b""
        uid = "other" if failure == "ownership" and "get" in argv else "owned"
        return json.dumps({"metadata": {"uid": uid}}).encode()

    def run(argv, *_args):
        assert argv[:2] == ["chainsaw", "test"]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1800)
        if failure == "test":
            raise RuntimeError("Test failed")

    def collect(*args):
        if failure == "receipt":
            raise ValueError("Incomplete receipt")

    monkeypatch.setattr(runner, "checked", checked)
    monkeypatch.setattr(runner, "run_test", run)
    monkeypatch.setattr(runner, "collect", collect)
    monkeypatch.setattr(runner, "diagnose", lambda *_args: None)
    result = runner.main(
        [
            "--context",
            "admitted-test",
            "--image",
            "example/core@sha256:" + "a" * 64,
            "--storage-class",
            "standard",
            "--output",
            str(output),
        ]
    )
    summary = json.loads((output / "summary.json").read_text())
    assert result == (0 if failure is None else 1)
    assert summary["status"] == ("passed" if failure is None else "failed")
    assert summary["namespace_removed"] is (failure not in {"cleanup", "ownership"})
    assert any("delete" in call for call in calls) is (failure != "ownership")
    assert not (output / "secret.json").exists()


def test_runner_rejects_mutable_image_before_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "checked", lambda *_args, **_kwargs: pytest.fail("command before image validation")
    )
    with pytest.raises(SystemExit):
        runner.main(
            [
                "--context",
                "unused",
                "--image",
                "example/core:latest",
                "--storage-class",
                "standard",
                "--output",
                str(tmp_path / "evidence"),
            ]
        )


def test_collector_rejects_a_different_running_image_digest(tmp_path, monkeypatch):
    image = "example/core@sha256:" + "a" * 64
    pod = {
        "metadata": {"name": "web", "uid": "owned", "labels": {"app": "django-web"}},
        "spec": {"initContainers": [{"name": "setup", "image": image}]},
        "status": {
            "initContainerStatuses": [
                {
                    "name": "setup",
                    "restartCount": 0,
                    "imageID": "example/core@sha256:" + "b" * 64,
                    "state": {"terminated": {"exitCode": 0}},
                }
            ]
        },
    }
    monkeypatch.setattr(
        runner,
        "checked",
        lambda argv: receipt() if "logs" in argv else json.dumps({"items": [pod]}).encode(),
    )
    with pytest.raises(ValueError, match="candidate container"):
        runner.collect(["kubectl"], "owned", tmp_path, image)
    assert not (tmp_path / "setup.json").exists()


def test_failure_diagnostics_exclude_resource_specs_and_bound_records(tmp_path, monkeypatch):
    item = {
        "metadata": {"name": "ray", "annotations": {"private": "excluded"}},
        "spec": {"private": "excluded"},
        "status": {"state": "pending"},
        "reason": "FailedCreate",
        "message": "Pod validation failed",
    }
    monkeypatch.setattr(
        runner, "checked", lambda _argv: json.dumps({"items": [item] * 101}).encode()
    )
    runner.diagnose(["kubectl"], "owned", tmp_path)
    for path in tmp_path.iterdir():
        assert len(json.loads(path.read_bytes())) == 100
        assert b"excluded" not in path.read_bytes()
    events = json.loads((tmp_path / "diagnostic-events.json").read_bytes())
    assert events[0]["reason"] == "FailedCreate"


def test_failure_diagnostics_include_bounded_application_logs(tmp_path, monkeypatch):
    names = ["django-web-one", "django-manager-one", *(f"ray-{i}" for i in range(5))]
    pods = [
        {
            "metadata": {"name": name},
            "status": {"containerStatuses": [{"name": "main", "restartCount": 1}]},
        }
        for name in names
    ]

    def checked(argv, **kwargs):
        if "logs" in argv:
            return b"x" * 40000
        return json.dumps({"items": pods if "pods" in argv else []}).encode()

    monkeypatch.setattr(runner, "checked", checked)
    runner.diagnose(["kubectl"], "owned", tmp_path)
    logs = list(tmp_path.glob("*.log"))
    assert len(logs) == 12
    assert all(path.stat().st_size == 32768 for path in logs)
    assert (tmp_path / "django-web-one-previous-False.log").exists()
    assert (tmp_path / "django-manager-one-previous-True.log").exists()
    assert not (tmp_path / "ray-4-previous-False.log").exists()


def test_failure_diagnostics_do_not_prevent_cleanup_on_api_failure(tmp_path, monkeypatch):
    def failed(_argv):
        raise RuntimeError("private API response")

    monkeypatch.setattr(runner, "checked", failed)
    runner.diagnose(["kubectl"], "owned", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("deleting", [None, "pod", "owner", "owner-absent"])
def test_container_failure_stops_chainsaw_but_allows_planned_deletion(monkeypatch, deleting):
    class Process:
        stopped = False
        waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                assert timeout == 15
                raise subprocess.TimeoutExpired("chainsaw", timeout)
            self.stopped = True
            return 0

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

    process = Process()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    pod = {
        "metadata": {
            "name": "ray-head",
            "ownerReferences": [{"kind": "RayCluster", "uid": "generation"}],
            **({"deletionTimestamp": "now"} if deleting == "pod" else {}),
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "ray-head",
                    "lastState": {"terminated": {"exitCode": 137, "reason": "OOMKilled"}},
                }
            ]
        },
    }
    cluster = {
        "metadata": {
            "uid": "generation",
            **({"deletionTimestamp": "now"} if deleting == "owner" else {}),
        }
    }

    def checked(argv):
        items = (
            ([cluster] if deleting != "owner-absent" else []) if "rayclusters" in argv else [pod]
        )
        return json.dumps({"items": items}).encode()

    monkeypatch.setattr(runner, "checked", checked)
    if deleting:
        runner.run_test(["chainsaw", "test"], ["kubectl"], "owned")
    else:
        with pytest.raises(RuntimeError, match="container failed"):
            runner.run_test(["chainsaw", "test"], ["kubectl"], "owned")
    assert process.stopped
