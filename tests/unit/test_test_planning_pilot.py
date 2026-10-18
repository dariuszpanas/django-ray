"""Advisory publication views preserve the fixed qualification obligations."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import test_planning_pilot as pilot
from scripts import test_planning_shadow as shadow

ROOT = Path(__file__).resolve().parents[2]
BASELINE_SHA = "d" * 64


def inventory():
    policy = tomllib.loads((ROOT / ".yaga/test-planning-shadow.toml").read_text())
    return [
        {
            "id": suite["id"],
            "binding": suite["binding"],
            "tags": sorted(suite.get("tags", [])),
            "variants": sorted(suite.get("variants", ["default"])),
            "needs": sorted(suite.get("needs", [])),
            "requires": sorted(suite.get("requires", [])),
        }
        for suite in sorted(policy["suites"], key=lambda item: item["id"])
    ]


def plan(*names):
    catalog = inventory()
    return {
        "plan": {
            "inventory": catalog,
            "selected": [
                {"suite": suite["id"], "variants": list(suite["variants"])}
                for suite in catalog
                if suite["id"].removeprefix("qualification.") in names
            ],
        }
    }


@pytest.mark.parametrize("selected", [[], ["application"], ["native"], sorted(shadow.JOBS)])
def test_materialized_baseline_selects_only_the_explicit_current_obligations(selected):
    catalog = inventory()
    before = copy.deepcopy(catalog)
    encoded = pilot.baseline_policy(catalog, selected, BASELINE_SHA)
    document = tomllib.loads(encoded.decode("utf-8"))

    assert document == {
        "test-policy-version": 1,
        "no-impact": ["**"],
        "profiles": {
            "current": {
                "mode": "selective",
                "always": ["qualification." + name for name in selected],
            }
        },
        "suites": catalog,
    }
    assert encoded.endswith(b"\n") and not encoded.endswith(b"\n\n")
    assert f"Original baseline policy SHA-256: {BASELINE_SHA}".encode() in encoded
    assert b"ONE explicit source comparison" in encoded
    assert catalog == before


def test_materialized_current_policy_retains_unselected_native_matrix_and_capabilities():
    document = tomllib.loads(pilot.baseline_policy(inventory(), [], BASELINE_SHA).decode())
    suites = {suite["id"]: suite for suite in document["suites"]}
    assert len(suites) == 5
    assert sum(len(suite["variants"]) for suite in suites.values()) == 8
    assert suites["qualification.native"]["variants"] == [
        "core-loss",
        "jobs",
        "jobs-crash",
        "native",
    ]
    assert suites["qualification.native"]["binding"] == "upgrade-native-qualification"
    assert suites["qualification.application"]["requires"] == [
        "chainsaw",
        "docker",
        "helm",
        "kind",
        "linux",
    ]


@pytest.mark.parametrize(
    "selected",
    [
        ["application", "application"],
        ["native", "application"],
        ["unknown"],
        ["qualification.application"],
        ["Application"],
    ],
)
def test_materialized_baseline_rejects_ambiguous_or_unknown_selection(selected):
    with pytest.raises(shadow.ShadowError):
        pilot.baseline_policy(inventory(), selected, BASELINE_SHA)


@pytest.mark.parametrize("edit", ["missing", "duplicate", "unknown", "extra"])
def test_materialized_baseline_requires_the_complete_exact_catalog(edit):
    catalog = inventory()
    if edit == "missing":
        catalog.pop()
    elif edit == "duplicate":
        catalog[-1] = copy.deepcopy(catalog[0])
    elif edit == "unknown":
        catalog[-1]["id"] = "qualification.unknown"
    else:
        catalog.append(copy.deepcopy(catalog[0]))
    with pytest.raises(shadow.ShadowError):
        pilot.baseline_policy(catalog, [], BASELINE_SHA)


@pytest.mark.parametrize(
    "digest",
    [None, True, 64, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n"]
    + ["a" * 64 + '\n[profiles.injected]\nmode = "full"'],
)
def test_baseline_identity_cannot_inject_policy_or_accept_noncanonical_hashes(digest):
    with pytest.raises(shadow.ShadowError):
        pilot.baseline_policy(inventory(), [], digest)


def test_selected_cells_expand_native_matrix_without_adding_unselected_suites():
    source = plan("native", "monitoring")
    original = copy.deepcopy(source)
    cells = pilot.selected_cells(source)
    assert cells == [
        {
            "suite": "qualification.monitoring",
            "variant": "default",
            "binding": "monitoring-qualification",
        },
        *[
            {
                "suite": "qualification.native",
                "variant": variant,
                "binding": "upgrade-native-qualification",
            }
            for variant in ("core-loss", "jobs", "jobs-crash", "native")
        ],
    ]
    assert source == original


@pytest.mark.parametrize(
    "current,proposed,full,expected",
    [
        ((), (), (), {"full": ["full", "proposed", "current"]}),
        (
            shadow.JOBS,
            shadow.JOBS,
            shadow.JOBS,
            {"full": ["full", "proposed", "current"]},
        ),
        (
            (),
            shadow.JOBS,
            shadow.JOBS,
            {"full": ["full", "proposed"], "current": ["current"]},
        ),
        (
            ("application",),
            ("application",),
            shadow.JOBS,
            {"full": ["full"], "proposed": ["proposed", "current"]},
        ),
        ((), (), shadow.JOBS, {"full": ["full"], "proposed": ["proposed", "current"]}),
        (
            ("monitoring",),
            ("application",),
            shadow.JOBS,
            {"full": ["full"], "proposed": ["proposed"], "current": ["current"]},
        ),
    ],
)
def test_publication_groups_share_identical_obligations_and_preserve_distinct_arms(
    current, proposed, full, expected
):
    plans = {"current": plan(*current), "proposed": plan(*proposed), "full": plan(*full)}
    original = copy.deepcopy(plans)
    groups = pilot.publication_groups(plans)
    assert groups == expected
    assert sorted(arm for arms in groups.values() for arm in arms) == sorted(pilot.ARMS)
    assert plans == original


def test_equal_cell_counts_do_not_deduplicate_different_native_variants():
    plans = {"current": plan("native"), "proposed": plan("native"), "full": plan(*shadow.JOBS)}
    plans["current"]["plan"]["selected"][0]["variants"] = ["jobs"]
    plans["proposed"]["plan"]["selected"][0]["variants"] = ["jobs-crash"]
    assert pilot.publication_groups(plans) == {
        "full": ["full"],
        "proposed": ["proposed"],
        "current": ["current"],
    }


def test_order_does_not_create_a_second_publication_for_identical_obligations():
    plans = {arm: plan(*shadow.JOBS) for arm in pilot.ARMS}
    plans["current"]["plan"]["selected"].reverse()
    plans["current"]["plan"]["inventory"].reverse()
    native = next(
        suite
        for suite in plans["proposed"]["plan"]["selected"]
        if suite["suite"] == "qualification.native"
    )
    native["variants"].reverse()
    assert pilot.publication_groups(plans) == {"full": ["full", "proposed", "current"]}


def test_same_cell_names_with_different_bindings_are_distinct_obligations():
    plans = {"current": plan("application"), "proposed": plan("application"), "full": plan()}
    plans["current"]["plan"]["inventory"][0]["binding"] = "different-executor"
    assert pilot.publication_groups(plans) == {
        "full": ["full"],
        "proposed": ["proposed"],
        "current": ["current"],
    }


def test_pure_helpers_remain_available_when_yaga_cannot_be_imported():
    script = """
import importlib.abc
import json
import sys

class NoYaga(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "yaga" or fullname.startswith("yaga."):
            raise AssertionError("Pure pilot helpers imported YAGA")
        return None

sys.meta_path.insert(0, NoYaga())
sys.path.insert(0, sys.argv[1])
from scripts import test_planning_pilot as pilot
catalog = json.loads(sys.argv[2])
pilot.baseline_policy(catalog, [], "a" * 64)
empty = {"plan": {"inventory": catalog, "selected": []}}
assert pilot.publication_groups(dict.fromkeys(pilot.ARMS, empty)) == {
    "full": ["full", "proposed", "current"]
}
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT), json.dumps(inventory())],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def workflow():
    return yaml.load(
        (ROOT / ".github/workflows/test-planning-shadow.yml").read_text(), Loader=yaml.BaseLoader
    )


def test_observer_starts_concurrently_only_for_a_fresh_manual_full_run():
    document = workflow()
    job = document["jobs"]["evaluation"]
    assert job["needs"] == "planning"
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' && github.run_attempt == 1 && "
        "needs.planning.outputs.full_shadow == 'true'"
    )
    assert job["strategy"]["fail-fast"] == "false"
    assert job["strategy"]["max-parallel"] == "3"
    assert job["strategy"]["matrix"] == "${{ fromJSON(needs.planning.outputs.evaluation_matrix) }}"
    assert 265 <= int(job["timeout-minutes"]) <= 300
    assert "continue-on-error" not in job
    assert document["permissions"] == {"contents": "read", "actions": "read", "checks": "read"}
    assert all("permissions" not in item for item in document["jobs"].values())


def test_observer_uses_exact_sources_and_always_retains_failures():
    job = workflow()["jobs"]["evaluation"]
    checkouts = [
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [(step["with"]["path"], step["with"]["ref"]) for step in checkouts] == [
        ("candidate", "${{ github.sha }}"),
        ("adapter", "${{ github.workflow_sha }}"),
    ]
    assert all(step["with"]["persist-credentials"] == "false" for step in checkouts)
    assert checkouts[0]["with"]["fetch-depth"] == "0"
    evaluate = next(
        step for step in job["steps"] if "scripts.test_planning_observer" in step.get("run", "")
    )
    assert evaluate["working-directory"] == "adapter"
    assert evaluate["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert '--run-id "$RUN_ID" --attempt "$RUN_ATTEMPT"' in evaluate["run"]
    assert '--range "$BASE_SHA...$SOURCE_SHA"' in evaluate["run"]
    assert "continue-on-error" not in evaluate and "|| true" not in evaluate["run"]
    publish = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert publish["if"] == "always()"
    assert publish["with"]["if-no-files-found"] == "error"
    assert publish["with"]["name"] == (
        "test-planning-evaluation-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.group }}"
    )


def test_planning_and_evaluation_share_one_pinned_installed_yaga_runtime():
    jobs = workflow()["jobs"]
    sources = [
        step["env"]["YAGA_SOURCE"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if "YAGA_SOURCE" in step.get("env", {})
    ]
    assert len(sources) >= 4 and set(sources) == {pilot.YAGA_SOURCE}
    planning = jobs["planning"]
    assert planning["outputs"]["evaluation_matrix"] == "${{ steps.pilot.outputs.matrix }}"
    prepare = next(step for step in planning["steps"] if step.get("id") == "pilot")
    assert "python -m scripts.test_planning_pilot" in prepare["run"]
    assert "--baseline-policy scripts/qualification_policy.json" in prepare["run"]


def test_feedback_collection_runs_after_all_jobs_and_survives_a_failed_observer():
    aggregate = workflow()["jobs"]["shadow-aggregate"]
    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == {"planning", "evaluation", *shadow.JOBS}
    collection = next(
        step
        for step in aggregate["steps"]
        if "scripts.test_planning_publications collect" in step.get("run", "")
    )
    assert collection["if"] == (
        "always() && github.event_name == 'workflow_dispatch' && github.run_attempt == 1 && "
        "needs.planning.result == 'success'"
    )
    assert "continue-on-error" not in collection


@pytest.mark.parametrize("name", ["ci.yml", "qualification-gate.yml"])
def test_required_workflows_do_not_depend_on_advisory_observation(name):
    text = (ROOT / ".github/workflows" / name).read_text()
    for integration in (
        "test-planning-shadow.yml",
        "scripts.test_planning_observer",
        "scripts.test_planning_publications",
        "scripts.test_planning_pilot",
        "Shadow Result Evaluation",
    ):
        assert integration not in text
