"""Resource-free refusal and complete-execution tests for the fanout workload."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest
import yaml

from qualification.docker import scenario as wheel
from qualification.runtime import contract, driver, scenario


def _receipt():
    nodes = sorted(contract.REQUIRED_SELECTORS)
    return {
        "schema_version": 1,
        "selected": nodes,
        "reports": {
            node: {phase: {"outcome": "passed", "seconds": 0.1} for phase in contract.PHASES}
            for node in nodes
        },
        "errors": [],
        "exit_code": 0,
        "ray_shutdown": True,
    }


def test_all_required_assertions_and_phases_produce_exact_junit():
    receipt = _receipt()
    assert contract.validate_receipt(receipt) is receipt
    root = ElementTree.fromstring(contract.success_junit(receipt))
    assert root.attrib == {
        "name": contract.WORKLOAD,
        "tests": "7",
        "failures": "0",
        "errors": "0",
        "skipped": "0",
    }
    assert len(root) == 7
    assert {case.attrib["name"] for case in root} == {
        node.rsplit("::", 1)[1] for node in receipt["selected"]
    }
    assert all(not list(case) for case in root)


@pytest.mark.parametrize("phase", contract.PHASES)
@pytest.mark.parametrize("outcome", ["failed", "skipped", "xfail", "xpassed"])
def test_any_nonpassing_phase_refuses_success(phase, outcome):
    receipt = _receipt()
    receipt["reports"][receipt["selected"][0]][phase]["outcome"] = outcome
    with pytest.raises(wheel.QualificationError, match="required-runtime-phase-did-not-pass"):
        contract.success_junit(receipt)


@pytest.mark.parametrize("phase", contract.PHASES)
def test_missing_phase_cannot_be_certified(phase):
    receipt = _receipt()
    del receipt["reports"][receipt["selected"][0]][phase]
    with pytest.raises(wheel.QualificationError, match="missing-runtime-phase"):
        contract.validate_receipt(receipt)


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf"), True, "1"])
def test_invalid_duration_refuses_success(seconds):
    receipt = _receipt()
    receipt["reports"][receipt["selected"][0]]["call"]["seconds"] = seconds
    with pytest.raises(wheel.QualificationError, match="invalid-runtime-duration"):
        contract.validate_receipt(receipt)


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "out-of-scope", "baseline"])
def test_changed_execution_identity_refuses_success(change):
    receipt = _receipt()
    node = receipt["selected"][0]
    if change == "missing":
        del receipt["reports"][node]
    elif change == "extra":
        receipt["reports"][node + "-extra"] = receipt["reports"][node]
    elif change == "duplicate":
        receipt["selected"].insert(0, node)
    elif change == "out-of-scope":
        receipt["selected"][0] = "tests/unrelated.py::test_wrong"
    else:
        receipt["selected"].remove(node)
        del receipt["reports"][node]
    with pytest.raises(wheel.QualificationError):
        contract.validate_receipt(receipt)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("exit_code", False),
        ("exit_code", 1),
        ("ray_shutdown", False),
        ("errors", ["duplicate-phase"]),
    ],
)
def test_terminal_and_teardown_refusals(field, value):
    receipt = _receipt()
    receipt[field] = value
    with pytest.raises(wheel.QualificationError):
        contract.validate_receipt(receipt)


def test_additional_real_ray_regression_is_retained_and_must_complete():
    receipt = _receipt()
    node = contract.TEST_PATH + "::TestDistributedWithRay::test_future_regression"
    receipt["selected"] = sorted([*receipt["selected"], node])
    with pytest.raises(wheel.QualificationError, match="runtime-execution-identity-mismatch"):
        contract.validate_receipt(receipt)
    receipt["reports"][node] = copy.deepcopy(next(iter(receipt["reports"].values())))
    assert node in contract.validate_receipt(receipt)["selected"]


def test_observer_retains_all_phases_and_refuses_duplicate_or_unselected_reports():
    observer = driver.Observations()
    observer.selected = sorted(contract.REQUIRED_SELECTORS)
    for node in observer.selected:
        for phase in contract.PHASES:
            observer.pytest_runtest_logreport(
                SimpleNamespace(nodeid=node, when=phase, outcome="passed", duration=0.2)
            )
    assert contract.validate_receipt(observer.receipt(0, ray_shutdown=True))
    observer.pytest_runtest_logreport(
        SimpleNamespace(nodeid=observer.selected[0], when="call", outcome="passed", duration=0.2)
    )
    observer.pytest_runtest_logreport(SimpleNamespace(nodeid="unselected", when="call"))
    assert observer.errors == ["duplicate-phase", "unexpected-test-report"]
    with pytest.raises(wheel.QualificationError):
        contract.validate_receipt(observer.receipt(0, ray_shutdown=True))


def test_observer_never_interprets_expected_failure_as_success():
    observer = driver.Observations()
    observer.selected = sorted(contract.REQUIRED_SELECTORS)
    node = observer.selected[0]
    observer.pytest_runtest_logreport(
        SimpleNamespace(nodeid=node, when="call", outcome="passed", duration=0.2, wasxfail="reason")
    )
    assert observer.reports[node]["call"]["outcome"] == "xfail"


def test_collection_requires_every_baseline_family_and_real_ray_marker():
    observer = driver.Observations()
    items = [
        SimpleNamespace(nodeid=node, get_closest_marker=lambda name: True)
        for node in contract.REQUIRED_SELECTORS
    ]
    observer.pytest_collection_finish(SimpleNamespace(items=items))
    assert observer.selected == sorted(contract.REQUIRED_SELECTORS)
    items[0].get_closest_marker = lambda name: None
    with pytest.raises(pytest.UsageError, match="real-Ray"):
        observer.pytest_collection_finish(SimpleNamespace(items=items))
    with pytest.raises(pytest.UsageError, match="selection failed"):
        observer.pytest_collection_finish(SimpleNamespace(items=[]))


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "attempt/target"
    evidence = tmp_path / "evidence"
    receipt_path = target.parent / "pytest-receipt.json"
    selected_wheel = tmp_path / "candidate.whl"
    selected_wheel.write_bytes(b"wheel")
    candidate = wheel.Candidate(
        "target", "a" * 64, "module", "a" * 64, "0.5.0", "candidate.whl", "b" * 64
    )
    monkeypatch.setattr(wheel, "_require_non_root", lambda: None)
    monkeypatch.setattr(wheel, "_require_linux_process_groups", lambda: None)
    monkeypatch.setattr(wheel, "_select_wheel", lambda path: selected_wheel)
    monkeypatch.setattr(wheel, "_install_wheel", lambda *args: None)
    monkeypatch.setattr(wheel, "_inspect_candidate", lambda *args: candidate)
    monkeypatch.setattr(wheel, "_package_tree_digest", lambda path: "a" * 64)
    monkeypatch.setattr(
        wheel,
        "_dependency_manifest",
        lambda candidate: {"python": "3.12", "django": "6", "pytest": "9", "ray": "2.56"},
    )
    state: dict[str, Any] = {"receipt": _receipt(), "returncode": 0, "error": None, "calls": []}

    def run(command, *, cwd, env, timeout):
        state["calls"].append((command, cwd, env, timeout))
        if state["error"] is not None:
            raise state["error"]
        if state["receipt"] is not None:
            receipt_path.write_text(json.dumps(state["receipt"]), encoding="utf-8")
        return wheel.BoundedProcessResult(state["returncode"], b"bounded progress", b"")

    monkeypatch.setattr(wheel, "_run_bounded_command", run)
    arguments = {
        "source_root": source,
        "evidence_root": evidence,
        "target": target,
        "receipt_path": receipt_path,
        "wheel_directory": tmp_path,
    }
    return arguments, state


def test_scenario_emits_candidate_exact_selection_and_real_observations(invocation):
    arguments, state = invocation
    assert scenario.execute(**arguments) == 0
    manifest = json.loads((arguments["evidence_root"] / "execution-manifest.json").read_bytes())
    assert manifest["outcome"] == "passed"
    assert manifest["definition_path"] == contract.DEFINITION_PATH
    assert manifest["tests"] == state["receipt"]
    assert manifest["candidate"]["installed_package_tree_sha256"] == "a" * 64
    command, cwd, env, timeout = state["calls"][0]
    assert command[-2:] == ("-m", "qualification.runtime.driver")
    assert cwd == arguments["source_root"]
    assert str(arguments["target"]) in env["PYTHONPATH"]
    assert timeout == 300


@pytest.mark.parametrize("failure", ["missing", "skipped", "child-failed", "timeout", "tree-drift"])
def test_scenario_retains_failure_evidence_without_certifying_partial_execution(
    invocation, monkeypatch, failure
):
    arguments, state = invocation
    if failure == "missing":
        state["receipt"] = None
    elif failure == "skipped":
        first = state["receipt"]["selected"][0]
        state["receipt"]["reports"][first]["call"]["outcome"] = "skipped"
    elif failure == "child-failed":
        state["returncode"] = 1
    elif failure == "tree-drift":
        monkeypatch.setattr(wheel, "_package_tree_digest", lambda path: "c" * 64)
    else:
        state["error"] = wheel.BoundedProcessError("command-timeout", b"progress", b"failure")
    assert scenario.execute(**arguments) == 1
    evidence = arguments["evidence_root"]
    manifest = json.loads((evidence / "execution-manifest.json").read_bytes())
    assert manifest["outcome"] == "failed"
    assert manifest["candidate"] is not None
    assert ElementTree.fromstring((evidence / "junit.xml").read_bytes()).attrib["failures"] == "1"


def test_reused_evidence_is_untouched(invocation):
    arguments, state = invocation
    evidence = arguments["evidence_root"]
    evidence.mkdir()
    existing = evidence / "junit.xml"
    existing.write_bytes(b"prior evidence")
    with pytest.raises(wheel.QualificationError, match="evidence-root-not-empty"):
        scenario.execute(**arguments)
    assert existing.read_bytes() == b"prior evidence"
    assert not state["calls"]


def test_runbook_keeps_the_supported_literal_command_and_external_budgets():
    root = Path(__file__).resolve().parents[2]
    value = yaml.safe_load((root / contract.DEFINITION_PATH).read_text())
    assert value["schema_version"] == 1
    definition = value["definition"]
    assert definition["executor"]["payload"]["argv"] == [
        "python",
        "-m",
        "qualification.runtime.scenario",
    ]
    assert set(definition) == {"executor", "requirements", "timeout_seconds", "evidence", "cleanup"}
    assert definition["timeout_seconds"] == 420
    assert definition["cleanup"] == {"policy": "always", "timeout_seconds": 180}
    assert definition["evidence"]["required_kinds"] == ["junit", "log", "manifest"]


@pytest.mark.parametrize("mode", ["pass", "skip", "teardown-failure"])
def test_driver_observes_real_pytest_phases_in_a_fresh_resource_free_process(tmp_path, mode):
    """Synthetic assertions exercise pytest itself without starting Ray or Django."""
    source = tmp_path / "source"
    tests = source / "tests/unit"
    tests.mkdir(parents=True)
    (source / "pytest.ini").write_text("[pytest]\nmarkers = real_ray: synthetic runtime marker\n")
    lines = ["import pytest", "pytestmark = pytest.mark.real_ray", "class TestDistributedWithRay:"]
    class_names = sorted(
        {node.rsplit("::", 1)[1].split("[")[0] for node in contract.REQUIRED_SELECTORS[:-1]}
    )
    for name in class_names:
        if name == "test_strict_rejection_survives_ray_without_invoking_callable":
            lines.extend(
                [
                    "    @pytest.mark.parametrize('value', [1, 2], ids=['protocol-protocol_mismatch', 'callable-callable_mismatch'])",
                    f"    def {name}(self, value):",
                    "        assert value in (1, 2)",
                ]
            )
        else:
            lines.extend([f"    def {name}(self):", "        assert True"])
    lines.extend(
        [
            "def test_nested_rejection_reaches_outer_enriched_completion():",
            "    pytest.skip('intentional synthetic skip')"
            if mode == "skip"
            else "    assert True",
        ]
    )
    (tests / "test_distributed.py").write_text("\n".join(lines) + "\n")
    if mode == "teardown-failure":
        (source / "conftest.py").write_text(
            "import pytest\n@pytest.fixture(autouse=True)\ndef broken_cleanup():\n"
            "    yield\n    raise RuntimeError('synthetic cleanup failure')\n"
        )
    target = tmp_path / "target"
    receipt_path = tmp_path / "pytest-receipt.json"
    script = tmp_path / "invoke.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
        "from qualification.runtime.driver import run\n"
        "root, target, receipt = map(Path, sys.argv[1:])\n"
        "sys.modules['django_ray'] = SimpleNamespace(__file__=str(target / 'django_ray/__init__.py'))\n"
        "sys.modules['ray'] = SimpleNamespace(is_initialized=lambda: False)\n"
        "raise SystemExit(run(root=root, target=target, receipt_path=receipt))\n"
    )
    environment = wheel._subprocess_environment()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, str(script), str(source), str(target), str(receipt_path)],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    receipt = json.loads(receipt_path.read_bytes())
    assert (result.returncode == 0) is (mode == "pass"), result.stdout + result.stderr
    if mode == "pass":
        assert len(contract.validate_receipt(receipt)["reports"]) == 7
    elif mode == "skip":
        assert any(row["call"]["outcome"] == "skipped" for row in receipt["reports"].values())
    else:
        assert any(row["teardown"]["outcome"] == "failed" for row in receipt["reports"].values())
