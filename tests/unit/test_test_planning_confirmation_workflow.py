"""The historical observer confirmation cannot dispatch qualification workloads."""

from pathlib import Path

import yaml

from scripts import test_planning_shadow as shadow

ROOT = Path(__file__).resolve().parents[2]


def test_confirmation_is_one_bounded_read_only_manual_job():
    workflow = yaml.load(
        (ROOT / ".github/workflows/test-planning-observer-confirmation.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"] == ""
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert set(workflow["jobs"]) == {"confirmation"}
    job = workflow["jobs"]["confirmation"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "10"
    assert not {"uses", "strategy", "services", "container", "permissions"} & job.keys()
    steps = job["steps"]
    assert len(steps) == 4
    assert [step.get("uses", "").split("@", 1)[0] for step in steps] == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "",
        "actions/upload-artifact",
    ]
    assert steps[0]["with"]["persist-credentials"] == "false"
    assert steps[1]["with"]["enable-cache"] == "false"
    command = steps[2]["run"]
    assert "--python 3.12 --with yaga-cli==0.3.0" in command
    assert "python -m scripts.test_planning_confirmation" in command
    assert 'test "$OBSERVER_SHA" = "$WORKFLOW_SHA"' in command
    assert steps[3]["if"] == "always()"
    assert steps[3]["with"]["name"].startswith("observer-diagnostic-confirmation-")
    assert steps[3]["with"]["path"] == "${{ runner.temp }}/observer-confirmation/"


def test_confirmation_changes_select_no_qualification_family():
    paths = (
        ".github/workflows/test-planning-observer-confirmation.yml",
        "scripts/test_planning_confirmation.py",
        "scripts/test_planning_observer.py",
        "tests/unit/test_test_planning_confirmation.py",
        "tests/unit/test_test_planning_confirmation_workflow.py",
        "docs/testing/test-planning-results.md",
    )
    assert (
        shadow.baseline_selection((ROOT / "scripts/qualification_policy.json").read_bytes(), paths)
        == []
    )
