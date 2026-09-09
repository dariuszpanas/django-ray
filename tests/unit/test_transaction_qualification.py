"""A partial transaction run or failed PostgreSQL cleanup cannot pass the gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
import yaml

from qualification.docker import scenario as wheel
from qualification.docker.scenario import QualificationError
from qualification.transactions import contract, probe, scenario


def receipt():
    cases = {}
    for name in contract.CASES:
        observations = {}
        if name.startswith(contract.VISIBILITY):
            observations = {
                "writer_pid": 12,
                "observer_pid": 34,
                "before": [0, 0],
                "after": [1, 1] if name.endswith("[True]") else [0, 0],
            }
        cases[name] = {
            "phases": ["setup:passed", "call:passed", "teardown:passed"],
            "observations": observations,
        }
    return {
        "module": "/installed/module",
        "server_version": 170011,
        "cases": cases,
        "socket_only": True,
        "server_stopped": True,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-case",
        "wrong-module",
        "wrong-postgres",
        "tcp",
        "server-left",
        "skip",
        "teardown-failed",
        "same-connection",
        "visible-before-commit",
        "commit-lost",
        "rollback-visible",
        "bool-pid",
        "missing-observation",
    ],
)
def test_incomplete_or_inconsistent_receipts_cannot_emit_success(mutation):
    value = receipt()
    committed = value["cases"][f"{contract.VISIBILITY}[True]"]
    rolled_back = value["cases"][f"{contract.VISIBILITY}[False]"]
    if mutation == "missing-case":
        value["cases"].pop(contract.CASES[0])
    elif mutation == "wrong-module":
        value["module"] = "/editable/src/module"
    elif mutation == "wrong-postgres":
        value["server_version"] = 160011
    elif mutation == "tcp":
        value["socket_only"] = False
    elif mutation == "server-left":
        value["server_stopped"] = False
    elif mutation == "skip":
        committed["phases"][1] = "call:skipped"
    elif mutation == "teardown-failed":
        committed["phases"][2] = "teardown:failed"
    elif mutation == "same-connection":
        committed["observations"]["observer_pid"] = 12
    elif mutation == "visible-before-commit":
        committed["observations"]["before"] = [1, 0]
    elif mutation == "commit-lost":
        committed["observations"]["after"] = [0, 1]
    elif mutation == "rollback-visible":
        rolled_back["observations"]["after"] = [1, 1]
    elif mutation == "bool-pid":
        committed["observations"]["writer_pid"] = True
    else:
        committed["observations"] = {}
    with pytest.raises(QualificationError):
        scenario.junit(value, expected_module="/installed/module", failure=None)


def test_complete_receipt_emits_all_sixteen_cases_and_failure_stays_failure():
    suite = ElementTree.fromstring(
        scenario.junit(
            receipt(),
            expected_module="/installed/module",
            failure=None,
        )
    )
    assert suite.attrib["tests"] == "16" and suite.attrib["failures"] == "0"
    assert [case.attrib["name"] for case in suite] == list(contract.CASES)
    failed = ElementTree.fromstring(
        scenario.junit(
            None,
            expected_module="/installed/module",
            failure="postgres-shutdown-timeout",
        )
    )
    assert failed.attrib["failures"] == "1"


def test_server_stop_requires_clean_exit_and_kills_a_timed_out_child(monkeypatch):
    monkeypatch.setattr(probe.signal, "SIGINT", 2)
    server = Mock()
    server.poll.return_value = None
    server.wait.return_value = 0
    probe.stop_server(server)
    server.send_signal.assert_called_once_with(2)
    server.kill.assert_not_called()
    server.wait.side_effect = [subprocess.TimeoutExpired("postgres", 15), -9]
    with pytest.raises(QualificationError, match="shutdown-timeout"):
        probe.stop_server(server)
    server.kill.assert_called_once_with()
    server.wait.side_effect = None
    server.poll.return_value = 1
    with pytest.raises(QualificationError, match="exited-before-cleanup"):
        probe.stop_server(server)


def test_initdb_failure_keeps_bounded_diagnostics_and_never_starts_server(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setitem(sys.modules, "psycopg", ModuleType("psycopg"))
    monkeypatch.setattr(wheel, "_require_non_root", lambda: None)
    monkeypatch.setattr(wheel, "_require_linux_process_groups", lambda: None)
    monkeypatch.setattr(
        wheel,
        "_run_bounded_command",
        lambda *args, **kwargs: wheel.BoundedProcessResult(
            1, b"init progress", b"unknown UID " + b"x" * 40000
        ),
    )
    popen = Mock(side_effect=AssertionError("server must not start after init failure"))
    monkeypatch.setattr(probe.subprocess, "Popen", popen)
    with pytest.raises(QualificationError, match="postgres-initialization-failed"):
        probe.execute(tmp_path, "/installed/module", tmp_path / "receipt.json")
    diagnostic = capsys.readouterr().err
    assert "init progress" in diagnostic and "unknown UID" in diagnostic
    assert "[truncated]" in diagnostic and len(diagnostic) < 34000
    popen.assert_not_called()
    assert not (tmp_path / "receipt.json").exists()


def test_literal_profile_and_optimized_probe_refusal(tmp_path):
    import django_ray

    root = Path(__file__).resolve().parents[2]
    profile = yaml.safe_load((root / contract.DEFINITION_PATH).read_text())
    service = profile["services"]["receipts"]
    dockerfile = (root / "qualification/transactions/Dockerfile").read_text()
    assert "groupadd --gid 10001 qualification" in dockerfile
    assert "useradd --uid 10001 --gid qualification" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert service["command"] == [
        "python",
        "-m",
        "qualification.transactions.scenario",
    ]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["cpus"] == 1.0
    assert service["mem_limit"] == service["memswap_limit"] == "1g"
    assert service["pids_limit"] == 128
    assert service["volumes"][0]["read_only"] is True
    module = Path(django_ray.__file__).resolve()
    environment = dict(
        os.environ, PYTHONPATH=os.pathsep.join((str(module.parent.parent), str(root)))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-P",
            "-m",
            "qualification.transactions.probe",
            str(tmp_path),
            str(module),
            str(tmp_path / "receipt.json"),
        ],
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0 and b"requires assertions enabled" in result.stderr
    assert not any(tmp_path.iterdir())


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "attempt/target"
    module = str((target / "django_ray/__init__.py").resolve())
    candidate = wheel.Candidate("target", "a" * 64, module, "a" * 64, "0.5.0", "x.whl", "b" * 64)
    monkeypatch.setattr(wheel, "_require_non_root", lambda: None)
    monkeypatch.setattr(wheel, "_require_linux_process_groups", lambda: None)
    monkeypatch.setattr(wheel, "_select_wheel", lambda path: path / "x.whl")
    monkeypatch.setattr(wheel, "_install_wheel", lambda *args: None)
    monkeypatch.setattr(wheel, "_inspect_candidate", lambda *args: candidate)
    monkeypatch.setattr(wheel, "_dependency_manifest", lambda candidate: {})
    monkeypatch.setattr(wheel, "_target_manifest", lambda *args: {"python": "3.12"})
    monkeypatch.setattr(wheel, "_package_tree_digest", lambda path: "a" * 64)
    state = {"failure": None, "fixtures": []}

    def run(command, *, cwd, env, timeout):
        fixture = Path(command[-3])
        state["fixtures"].append(fixture)
        (fixture / "owned-postgres-data").write_bytes(b"owned database")
        assert command[1:4] == ("-P", "-m", "qualification.transactions.probe")
        assert timeout == 180 and cwd == source
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(target)
        if state["failure"] == "early-exit":
            return wheel.BoundedProcessResult(1, b"", b"postgres-initialization-failed")
        if state["failure"] == "timeout":
            raise wheel.BoundedProcessError("command-timeout", b"progress", b"failure")
        value = receipt()
        value["module"] = module
        if state["failure"] == "incomplete":
            value["cases"].pop(contract.CASES[0])
        payload = json.dumps(value).encode()
        if state["failure"] == "oversized":
            payload = b" " * (contract.MAX_PROBE_BYTES + 1)
        Path(command[-1]).write_bytes(payload)
        return wheel.BoundedProcessResult(1 if state["failure"] == "exit" else 0, b"progress", b"")

    monkeypatch.setattr(wheel, "_run_bounded_command", run)
    return {"source_root": source, "evidence_root": tmp_path / "evidence", "target": target}, state


@pytest.mark.parametrize(
    "failure", [None, "timeout", "incomplete", "oversized", "exit", "early-exit", "tree-drift"]
)
def test_scenario_reports_failure_and_removes_only_owned_fixtures(invocation, monkeypatch, failure):
    arguments, state = invocation
    state["failure"] = failure
    if failure == "tree-drift":
        monkeypatch.setattr(wheel, "_package_tree_digest", lambda path: "c" * 64)
    assert scenario.execute(**arguments) == (0 if failure is None else 1)
    manifest = json.loads((arguments["evidence_root"] / "execution-manifest.json").read_bytes())
    assert manifest["outcome"] == ("passed" if failure is None else "failed")
    assert manifest["fixture_cleanup"] is True
    if failure == "early-exit":
        assert manifest["failure"] == "transaction-probe-failed"
    assert state["fixtures"] and all(not path.exists() for path in state["fixtures"])
    assert arguments["target"].is_dir()
    junit = ElementTree.fromstring((arguments["evidence_root"] / "junit.xml").read_bytes())
    assert junit.attrib["failures"] == ("0" if failure is None else "1")


def test_existing_evidence_is_never_overwritten(invocation):
    arguments, state = invocation
    arguments["evidence_root"].mkdir()
    existing = arguments["evidence_root"] / "junit.xml"
    existing.write_bytes(b"original evidence")
    with pytest.raises(wheel.QualificationError, match="evidence-root-not-empty"):
        scenario.execute(**arguments)
    assert existing.read_bytes() == b"original evidence" and not state["fixtures"]
