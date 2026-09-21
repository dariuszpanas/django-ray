"""Conditional qualification must never infer success from absent evidence."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.qualification_gate import GateError, changed_files, select, verify

SHA = "a" * 40
ENTRY = {
    "id": "latency",
    "workflow": "latency-qualification.yml",
    "paths": ["src/**"],
}


def results():
    return {
        "selection": {
            "result": "success",
            "outputs": {"application": "true", "native": "false", "latency": "true"},
        },
        "application": {"result": "success"},
        "native": {"result": "skipped"},
        "latency": {"result": "success"},
    }


def test_workflow_owns_selection_and_every_qualification_dependency():
    root = Path(__file__).resolve().parents[2]
    policy = json.loads((root / "scripts/qualification_policy.json").read_text())
    gate = yaml.load(
        (root / ".github/workflows/qualification-gate.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert gate["on"]["pull_request"] == {"branches": ["main"]}
    assert set(gate["jobs"]["qualification-gate"]["needs"]) == {
        "selection",
        "application",
        "native",
        "latency",
    }
    assert gate["jobs"]["qualification-gate"]["if"] == "always()"
    assert {entry["id"] for entry in policy} == {"application", "native", "latency"}
    for entry in policy:
        child = yaml.load(
            (root / ".github/workflows" / entry["workflow"]).read_text(), Loader=yaml.BaseLoader
        )
        assert set(child["on"]) == {"workflow_call", "workflow_dispatch"}
        assert "scripts/qualification_gate.py" in entry["paths"]
        job = gate["jobs"][entry["id"]]
        assert job["needs"] == "selection"
        assert job["uses"] == "./.github/workflows/" + entry["workflow"]
        assert job["if"] == f"needs.selection.outputs.{entry['id']} == 'true'"
        if entry["id"] == "native":
            assert [
                item["recipe"] for item in child["jobs"]["native"]["strategy"]["matrix"]["include"]
            ] == ["native", "jobs", "jobs-crash", "core-loss"]


def test_applicability_is_explicit():
    assert select([ENTRY], {"docs/readme.md"}) == []
    assert select([ENTRY], {"src/nested/code.py"}) == [ENTRY]


def test_only_selected_success_and_explicit_unselected_skip_pass():
    assert verify(results()) == {
        "application": "passed",
        "native": "not_applicable",
        "latency": "passed",
    }


@pytest.mark.parametrize("name", ["application", "latency"])
@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", "", None])
def test_selected_missing_or_failed_result_never_passes(name, state):
    value = results()
    value[name]["result"] = state
    with pytest.raises(GateError):
        verify(value)


@pytest.mark.parametrize("state", ["success", "failure", "cancelled", None])
def test_unselected_job_must_be_skipped(state):
    value = results()
    value["native"]["result"] = state
    with pytest.raises(GateError):
        verify(value)


@pytest.mark.parametrize(
    "change", ["missing", "extra", "selection_failed", "missing_output", "invalid_output"]
)
def test_incomplete_selection_or_dependency_inventory_fails(change):
    value = results()
    if change == "missing":
        del value["native"]
    elif change == "extra":
        value["unexpected"] = {"result": "success"}
    elif change == "selection_failed":
        value["selection"]["result"] = "failure"
    elif change == "missing_output":
        del value["selection"]["outputs"]["native"]
    else:
        value["selection"]["outputs"]["native"] = "unknown"
    with pytest.raises(GateError):
        verify(value)


def test_rename_includes_original_and_current_paths():
    def api(endpoint):
        if "/files?" in endpoint:
            return [{"filename": "docs/new.md", "previous_filename": "src/old.py"}]
        return {"state": "open", "head": {"sha": SHA}, "changed_files": 1}

    assert changed_files(api, "owner/repo", 2, SHA) == {"docs/new.md", "src/old.py"}


@pytest.mark.parametrize("change", ["stale", "closed", "oversized", "missing_page"])
def test_changed_file_inventory_fails_closed(change):
    pr = {"state": "open", "head": {"sha": SHA}, "changed_files": 1}
    if change == "stale":
        pr["head"]["sha"] = "b" * 40
    elif change == "closed":
        pr["state"] = "closed"
    elif change == "oversized":
        pr["changed_files"] = 3001

    def api(endpoint):
        return [] if "/files?" in endpoint else deepcopy(pr)

    with pytest.raises(GateError):
        changed_files(api, "owner/repo", 2, SHA)
