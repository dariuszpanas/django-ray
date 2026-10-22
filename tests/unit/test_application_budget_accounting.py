"""New work has explicit additive limits instead of borrowing old phase time."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from qualification.application import budgets, run_chainsaw, run_generation

ROOT = Path(__file__).resolve().parents[2]


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def test_outer_wall_clock_accounts_for_every_new_sequential_phase():
    assert (budgets.FIRST_SECONDS, budgets.EXISTING_SECONDS, budgets.RQ2_SECONDS) == (180, 600, 180)
    assert (budgets.SHOWCASE_SECONDS, budgets.PROTOCOL_SECONDS, budgets.SURFACE_SECONDS) == (
        180,
        120,
        30,
    )
    assert budgets.APPLICATION_SECONDS == 2160 + 2 * (180 + 120 + 30) == 2820
    assert budgets.NATIVE_STEP_SECONDS == 2820 + 180 + 300 == 55 * 60
    assert budgets.GENERATION_JOB_SECONDS == 180 + 600 + 180 + 180 + 120 + 30 == 1290
    assert budgets.GENERATION_ASSERT_SECONDS == 1305
    config = yaml.safe_load((ROOT / ".github/workflows/application-qualification.yml").read_text())
    job = config["jobs"]["application-core"]
    assert job["timeout-minutes"] == 130
    native = next(
        step
        for step in job["steps"]
        if "qualification.application.run_chainsaw" in step.get("run", "")
    )
    assert native["timeout-minutes"] * 60 == budgets.NATIVE_STEP_SECONDS
    manifest = yaml.safe_load((ROOT / "qualification/application/core.yaml").read_text())
    jobs = [
        item
        for item in dictionaries(manifest)
        if item.get("kind") == "Job"
        and item.get("metadata", {}).get("name") in {"assert-before", "assert-after"}
        and "template" in item.get("spec", {})
    ]
    assert len(jobs) == 2
    assert all(
        job["spec"]["activeDeadlineSeconds"] == budgets.GENERATION_JOB_SECONDS for job in jobs
    )


def test_accounted_wall_limit_stops_chainsaw_and_owned_process(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(run_chainsaw.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(run_chainsaw.time, "monotonic", Mock(side_effect=[0, 2821]))
    with pytest.raises(TimeoutError, match="accounted wall-clock"):
        run_chainsaw.run_test(["owned-chainsaw"], ["kubectl"], "owned-namespace")
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=10)


@pytest.mark.parametrize("change", ["missing", "duplicate", "reordered"])
def test_missing_or_reordered_generation_work_cannot_pass(monkeypatch, change):
    names = ["first", "nodes", "core", "workflows", "rq2", "showcase", "protocol"]
    if change == "missing":
        names.pop()
    elif change == "duplicate":
        names[-1] = "showcase"
    else:
        names[-2:] = reversed(names[-2:])
    execute = Mock()
    monkeypatch.setattr(run_generation, "run_command", execute)
    assert run_generation.run_phases(tuple((name, [name]) for name in names)) == 1
    execute.assert_not_called()
