"""Reject incomplete result-read proof and clean fixtures after probe failure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
import yaml

from qualification.docker import scenario as wheel
from qualification.results import contract, scenario


def _receipt(kind: str, module: str) -> dict:
    return {
        "schema_version": 1,
        "kind": kind,
        "status": "passed",
        "module_location": module,
        "assertions": contract.assertions_for(kind),
        "refused_operations": 28,
    }


def test_probe_cannot_pass_with_assertions_disabled(tmp_path):
    import django_ray

    root = Path(__file__).resolve().parents[2]
    module = Path(django_ray.__file__).resolve()
    environment = dict(os.environ)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment["PYTHONPATH"] = os.pathsep.join((str(module.parent.parent), str(root)))
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-P",
            "-m",
            "qualification.results.probe",
            str(tmp_path),
            contract.CASES[0],
            str(module),
        ],
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert b"requires assertions enabled" in result.stderr
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("kind", "another-case"),
        ("status", "skipped"),
        ("module_location", "/editable/src/django_ray/__init__.py"),
        ("assertions", []),
        ("refused_operations", 27),
        ("refused_operations", True),
    ],
)
def test_missing_or_mismatched_observations_cannot_pass(field, value):
    receipt = _receipt(contract.CASES[0], "/installed/django_ray/__init__.py")
    receipt[field] = value
    with pytest.raises(wheel.QualificationError):
        contract.validate_probe(
            receipt, kind=contract.CASES[0], expected_module="/installed/django_ray/__init__.py"
        )


@pytest.mark.parametrize("change", ["missing", "repeated", "reordered", "extra"])
def test_junit_requires_all_distinct_cases_in_the_fixed_order(change):
    receipts = [_receipt(kind, "/candidate") for kind in contract.CASES]
    if change == "missing":
        receipts.pop()
    elif change == "repeated":
        receipts[1] = receipts[0]
    elif change == "reordered":
        receipts.reverse()
    else:
        receipts.append(receipts[0])
    with pytest.raises(wheel.QualificationError):
        contract.junit(receipts, expected_module="/candidate", failure=None)


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "attempt/target"
    evidence = tmp_path / "evidence"
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
    state: dict[str, Any] = {"failure": None, "calls": []}

    def run(command, *, cwd, env, timeout):
        state["calls"].append((command, cwd, env, timeout))
        fixture, kind = Path(command[-3]), command[-2]
        (fixture / "db.sqlite3").write_bytes(b"owned fixture")
        receipt = _receipt(kind, module)
        if len(state["calls"]) == 2:
            if state["failure"] == "timeout":
                raise wheel.BoundedProcessError("command-timeout", b"progress", b"failure")
            if state["failure"] == "exit":
                return wheel.BoundedProcessResult(1, b"probe failed", b"")
            if state["failure"] == "identity":
                receipt["module_location"] = "/wrong/candidate"
            if state["failure"] == "oversized":
                return wheel.BoundedProcessResult(0, b" " * (contract.MAX_PROBE_BYTES + 1), b"")
        return wheel.BoundedProcessResult(0, json.dumps(receipt).encode(), b"")

    monkeypatch.setattr(wheel, "_run_bounded_command", run)
    return {"source_root": source, "evidence_root": evidence, "target": target}, state


def test_success_requires_each_child_identity_and_removes_owned_fixtures(invocation):
    arguments, state = invocation
    assert scenario.execute(**arguments) == 0
    evidence = arguments["evidence_root"]
    manifest = json.loads((evidence / "execution-manifest.json").read_bytes())
    assert manifest["outcome"] == "passed"
    assert manifest["fixture_cleanup"] is True
    assert [r["kind"] for r in manifest["cases"]] == list(contract.CASES)
    assert sum(r["refused_operations"] for r in manifest["cases"]) == 84
    for command, cwd, environment, timeout in state["calls"]:
        assert command[1:4] == ("-P", "-m", "qualification.results.probe")
        assert not Path(command[-3]).exists()
        assert cwd == arguments["source_root"]
        assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(arguments["target"])
        assert timeout == 45
    junit = ElementTree.fromstring((evidence / "junit.xml").read_bytes())
    assert junit.attrib == {
        "name": contract.WORKLOAD,
        "tests": "3",
        "failures": "0",
        "errors": "0",
        "skipped": "0",
    }


@pytest.mark.parametrize("failure", ["timeout", "exit", "identity", "oversized", "tree-drift"])
def test_failed_probe_keeps_partial_evidence_and_cleans_fixtures(invocation, monkeypatch, failure):
    arguments, state = invocation
    state["failure"] = failure
    if failure == "tree-drift":
        monkeypatch.setattr(wheel, "_package_tree_digest", lambda path: "c" * 64)
    assert scenario.execute(**arguments) == 1
    manifest = json.loads((arguments["evidence_root"] / "execution-manifest.json").read_bytes())
    assert manifest["outcome"] == "failed"
    assert manifest["failure"]
    assert manifest["fixture_cleanup"] is True
    assert len(state["calls"]) == (3 if failure == "tree-drift" else 2)
    assert all(not Path(call[0][-3]).exists() for call in state["calls"])
    junit = ElementTree.fromstring((arguments["evidence_root"] / "junit.xml").read_bytes())
    assert junit.attrib["failures"] == "1"


def test_failed_dependency_inspection_still_retains_candidate_evidence(invocation, monkeypatch):
    arguments, state = invocation

    def fail(candidate):
        raise wheel.QualificationError("dependencies-unavailable")

    monkeypatch.setattr(wheel, "_dependency_manifest", fail)
    assert scenario.execute(**arguments) == 1
    manifest = json.loads((arguments["evidence_root"] / "execution-manifest.json").read_bytes())
    assert manifest["candidate"] is not None
    assert manifest["target"] is None
    assert manifest["failure"] == "dependencies-unavailable"
    assert not state["calls"]


def test_existing_evidence_is_preserved(invocation):
    arguments, state = invocation
    arguments["evidence_root"].mkdir()
    existing = arguments["evidence_root"] / "junit.xml"
    existing.write_bytes(b"previous attempt")
    with pytest.raises(wheel.QualificationError, match="evidence-root-not-empty"):
        scenario.execute(**arguments)
    assert existing.read_bytes() == b"previous attempt"
    assert not state["calls"]


def test_definition_uses_the_fixed_linux_command_without_starting_ray():
    root = Path(__file__).resolve().parents[2]
    definition = yaml.safe_load((root / contract.DEFINITION_PATH).read_text())["definition"]
    assert definition["executor"]["payload"]["argv"] == [
        "python",
        "-m",
        "qualification.results.scenario",
    ]
    assert definition["requirements"]["capabilities"] == [
        "external-evidence-v1",
        "kubernetes",
        "linux",
        "python",
    ]
    assert definition["timeout_seconds"] == 420
    assert definition["cleanup"] == {"policy": "always", "timeout_seconds": 180}
