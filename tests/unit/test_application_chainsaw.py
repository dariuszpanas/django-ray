"""Public runner receipt and owned-cleanup contracts without external processes."""

import json
import subprocess

import pytest

from qualification.application import run_chainsaw as runner
from qualification.application.result_presentation import EVIDENCE as RESULT_EVIDENCE
from qualification.application.runtime_env_refusal import CASES, FACTS


def receipt(layer="application_setup", **changes):
    defaults = {}
    if layer == "application_core":
        defaults["runtime_env_refusals"] = {case: dict.fromkeys(FACTS, True) for case in CASES}
        defaults["ordinary_results"] = dict(RESULT_EVIDENCE)
        defaults["protected_runtime"] = {
            "api_clear": True,
            "row_ids": [1, 2, 3],
            "started_at": "2026-10-12T00:00:00Z",
        }
    return json.dumps(
        {
            "schema_version": 1,
            "layer": layer,
            "status": "passed",
            "complete_application_gate": False,
            **defaults,
            **changes,
        }
    ).encode()


@pytest.mark.parametrize("failure", ["missing", "partial", "extra", "non-boolean"])
def test_core_receipt_requires_both_exact_negative_runtime_env_cases(failure):
    value = json.loads(receipt("application_core"))
    if failure == "missing":
        del value["runtime_env_refusals"]
    elif failure == "extra":
        value["runtime_env_refusals"]["extra"] = {}
    else:
        value["runtime_env_refusals"][CASES[1]][FACTS[0]] = 1 if failure == "non-boolean" else False
    with pytest.raises(ValueError):
        runner.parse_receipts(json.dumps(value).encode(), ("after-core",))


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {},
        {**RESULT_EVIDENCE, "owned_rows_removed": False},
        {**RESULT_EVIDENCE, "current_api": 1},
    ],
)
def test_core_receipts_require_complete_typed_result_presentation_in_both_generations(evidence):
    for generation in ("before", "after"):
        with pytest.raises(ValueError, match="result evidence"):
            runner.parse_receipts(
                receipt("application_core", ordinary_results=evidence),
                (f"{generation}-core",),
            )


@pytest.mark.parametrize("extra", [0, 1])
def test_workflow_receipt_has_its_own_exact_byte_ceiling(extra):
    raw = receipt("workflow_api_admin", observations="")
    raw = receipt(
        "workflow_api_admin",
        observations="x" * (runner.WORKFLOW_RECEIPT_MAX_BYTES - len(raw) + extra),
    )
    assert len(raw) == runner.WORKFLOW_RECEIPT_MAX_BYTES + extra
    if extra:
        with pytest.raises(ValueError):
            runner.parse_receipts(raw, ("before-workflows",))
    else:
        assert runner.parse_receipts(raw, ("before-workflows",)) == {"before-workflows": raw}


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
    "failure",
    [
        None,
        "test",
        "timeout",
        "receipt",
        "receipt-type",
        "cleanup",
        "ownership",
        "source",
        "worker-images",
        "worker-diagnostic",
        "worker-diagnostic-invalid",
        "worker-diagnostic-unavailable",
        "worker-diagnostic-collection",
    ],
)
@pytest.mark.parametrize(
    "profile, intent", [("standard", "acceptance"), ("constrained-ray", "diagnostic")]
)
def test_runner_requires_assertions_source_and_owned_namespace_cleanup(
    tmp_path, monkeypatch, capsys, failure, profile, intent
):
    output = tmp_path / "evidence"
    calls = []
    trees = 0
    diagnostic = {
        "schema_version": 2,
        "phase": "ready_sync",
        "category": "validation",
        "timeout_source": None,
        "cleanup_phase": "cleanup_restore",
        "cleanup_category": "timeout",
        "cleanup_timeout_source": "remaining_budget_limit",
        "temporary_removed": True,
        "rows_removed": True,
        "history_preserved": True,
        "original_restored": False,
    }
    private_error = "PRIVATE_WORKER_ERROR_MUST_NOT_BE_RETAINED"

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
            raise subprocess.TimeoutExpired(argv, runner.APPLICATION_SECONDS)
        if failure == "test":
            raise RuntimeError("Test failed")
        return 0.001  # A prior phase's remaining budget must not constrain the next one.

    def worker_images(**kwargs):
        assert "deadline" not in kwargs
        if failure == "worker-images":
            raise ValueError(private_error)
        if failure and failure.startswith("worker-diagnostic"):
            error = runner.WorkerImageObservationError(diagnostic)
            error.args = (private_error,)
            if failure == "worker-diagnostic-invalid":
                monkeypatch.setattr(
                    error, "diagnostic", lambda: {**diagnostic, "raw": private_error}
                )
            elif failure == "worker-diagnostic-unavailable":

                def unavailable():
                    raise ValueError(private_error)

                monkeypatch.setattr(error, "diagnostic", unavailable)
            raise error
        return {"test_fixture": True}

    def collect(*args, expected_profile):
        assert expected_profile == runner.select_profile(profile, intent)
        if failure in {"receipt", "worker-diagnostic-collection"}:
            raise ValueError("Incomplete receipt")
        if failure == "receipt-type":
            raise TypeError("private malformed response")

    monkeypatch.setattr(runner, "checked", checked)
    monkeypatch.setattr(runner, "run_test", run)
    monkeypatch.setattr(runner, "observe_worker_images", worker_images)
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
            "--resource-profile",
            profile,
            "--validation-intent",
            intent,
        ]
    )
    summary = json.loads((output / "summary.json").read_text())
    assert result == (0 if failure is None else 1)
    assert summary["status"] == ("passed" if failure is None else "failed")
    assert summary["namespace_removed"] is (failure not in {"cleanup", "ownership"})
    assert summary["qualification_profile"] == runner.select_profile(profile, intent)
    assert summary["worker_images"] == (
        None
        if failure in {"test", "timeout", "worker-images"}
        or (failure and failure.startswith("worker-diagnostic"))
        else {"test_fixture": True}
    )
    if failure in {"worker-diagnostic", "worker-diagnostic-collection"}:
        assert summary["worker_images_failure"] == diagnostic
    else:
        assert "worker_images_failure" not in summary
    values = json.loads((output / "values.json").read_text())
    assert values["resourceProfile"] == profile
    assert values["validationIntent"] == intent
    assert values["rayHeadLogicalCPU"] == ("0" if profile == "constrained-ray" else "1")
    assert any("delete" in call for call in calls) is (failure != "ownership")
    assert not (output / "secret.json").exists()
    captured = capsys.readouterr()
    assert private_error not in captured.out + captured.err
    assert all(private_error.encode() not in path.read_bytes() for path in output.iterdir())


def test_constrained_acceptance_rejected_before_commands_or_output(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "checked", lambda *_a, **_k: pytest.fail("must reject first"))
    output = tmp_path / "evidence"
    with pytest.raises(SystemExit):
        runner.main(
            [
                "--context",
                "unused",
                "--image",
                "example/core@sha256:" + "a" * 64,
                "--storage-class",
                "standard",
                "--output",
                str(output),
                "--resource-profile",
                "constrained-ray",
            ]
        )
    assert not output.exists()


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


def test_failure_diagnostics_never_collect_raw_application_logs(tmp_path, monkeypatch):
    names = [
        "assert-before-one",
        "assert-after-one",
        "django-web-one",
        "django-manager-one",
        *(f"ray-{i}" for i in range(5)),
    ]
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
    assert not list(tmp_path.glob("*.log"))
    assert (tmp_path / "diagnostic-pods.json").exists()


@pytest.mark.parametrize("failure", [None, "timeout", "oversized", "invalid"])
def test_actor_diagnostics_are_bounded_and_cannot_prevent_owned_cleanup(
    tmp_path, monkeypatch, failure
):
    pods = [{"metadata": {"name": "ray-owned-head", "labels": {"app": "ray-head"}}}]
    calls = []

    def checked(argv, *, timeout):
        calls.append(argv)
        assert timeout == 15
        assert argv[:3] == ["kubectl", "exec", "--request-timeout=5s"]
        assert argv[argv.index("--") + 1 : -1] == [
            "timeout",
            "--signal=TERM",
            "--kill-after=1s",
            "12s",
            "python",
            "-c",
        ]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if failure == "invalid":
            return b"[]"
        return (
            b"x" * 16385
            if failure == "oversized"
            else json.dumps(
                {
                    "diagnostic_only": True,
                    "status": "unavailable",
                    "resources": {
                        kind: {"status": "unavailable", "records": []}
                        for kind in ("actors", "tasks", "nodes")
                    },
                }
            ).encode()
        )

    monkeypatch.setattr(runner, "checked", checked)
    runner.diagnose_progress_actors(["kubectl"], "owned", tmp_path, pods)
    assert len(calls) == 1
    assert (tmp_path / "diagnostic-progress-actors.json").exists() is (failure is None)
    runner.diagnose_progress_actors(["kubectl"], "owned", tmp_path, pods * 2)
    assert len(calls) == 1


def test_failure_diagnostics_do_not_prevent_cleanup_on_api_failure(tmp_path, monkeypatch):
    def failed(_argv):
        raise RuntimeError("private API response")

    monkeypatch.setattr(runner, "checked", failed)
    runner.diagnose(["kubectl"], "owned", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("completion_seconds", "ignore_terminate"),
    [(2161, False), (2819.5, False), (2821, False), (2821, True)],
)
def test_accounted_outer_budget_completes_or_stops_at_its_bound(
    monkeypatch, completion_seconds, ignore_terminate
):
    clock = [100.0]
    started = clock[0]
    monitored_waits = []
    cleanup_waits = []
    health_checks = []

    class Process:
        stopped = False
        terminated_at = None
        killed = False

        def wait(self, timeout):
            if self.terminated_at is not None:
                cleanup_waits.append(timeout)
                if ignore_terminate and not self.killed:
                    clock[0] += timeout
                    raise subprocess.TimeoutExpired("chainsaw", timeout)
                self.stopped = True
                return 0
            assert 0 < timeout <= 15
            monitored_waits.append(timeout)
            remaining = started + completion_seconds - clock[0]
            if remaining <= timeout:
                clock[0] += remaining
                self.stopped = True
                return 0
            clock[0] += timeout
            raise subprocess.TimeoutExpired("chainsaw", timeout)

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.terminated_at = clock[0] - started

        def kill(self):
            self.killed = True

    process = Process()

    def checked(argv):
        assert argv[:2] == ["kubectl", "get"]
        assert argv[2] in {"pods", "rayclusters"}
        assert argv[3:] == ["-n", "owned", "-o", "json"]
        health_checks.append(argv[2])
        return b'{"items": []}'

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(runner, "checked", checked)
    if completion_seconds < 2820:
        runner.run_test(["chainsaw", "test"], ["kubectl"], "owned")
        assert clock[0] - started == completion_seconds > 2160
        assert process.terminated_at is None
        assert cleanup_waits == []
    else:
        with pytest.raises(TimeoutError, match="accounted wall-clock"):
            runner.run_test(["chainsaw", "test"], ["kubectl"], "owned")
        assert sum(monitored_waits) == 2820
        assert process.terminated_at == 2820
        assert cleanup_waits == ([10, 10] if ignore_terminate else [10])
    assert health_checks
    assert process.stopped
    assert process.killed is ignore_terminate


@pytest.mark.parametrize("deleting", [None, "pod", "owner", "owner-absent"])
@pytest.mark.parametrize(
    "exit_code, expected", [(137, 137), ("private-canary", None), (True, None)]
)
def test_container_failure_stops_chainsaw_but_allows_planned_deletion(
    monkeypatch, capsys, deleting, exit_code, expected
):
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
            "name": "private-canary-pod",
            "ownerReferences": [{"kind": "RayCluster", "uid": "generation"}],
            **({"deletionTimestamp": "now"} if deleting == "pod" else {}),
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "private-canary-container",
                    "lastState": {
                        "terminated": {"exitCode": exit_code, "reason": "private-canary-reason"}
                    },
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
    output = capsys.readouterr().out
    assert "private-canary" not in output
    if deleting:
        assert not output
    else:
        assert json.loads(output) == {"application_container_failed": True, "exit_code": expected}
