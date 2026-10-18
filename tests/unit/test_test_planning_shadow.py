"""The advisory pilot cannot weaken selection or infer missing execution success."""

import json
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import test_planning_shadow as shadow

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "c" * 64


def outputs(full=True):
    return {
        "full_shadow": "true" if full else "false",
        "execution_commit": COMMIT,
        "execution_tree": TREE,
        "policy_sha256": DIGEST,
        "bindings_sha256": DIGEST,
        "plan_digest": DIGEST,
    }


def results(full=True):
    return {
        "planning": {"result": "success", "outputs": outputs(full)},
        **{name: {"result": "success" if full else "skipped"} for name in shadow.JOBS},
    }


def verify(needs, full=True):
    return shadow.verify_jobs(needs, full_shadow=full, execution_commit=COMMIT, execution_tree=TREE)


def test_policy_preserves_every_existing_trigger_and_native_cell():
    baseline = json.loads((ROOT / "scripts/qualification_policy.json").read_text())
    policy = tomllib.loads((ROOT / ".yaga/test-planning-shadow.toml").read_text())
    assert policy["unknown-paths"] == "full"
    rules = {rule["id"]: rule for rule in policy["rules"]}
    suites = {suite["id"]: suite for suite in policy["suites"]}
    assert set(rules) == set(shadow.JOBS)
    for entry in baseline:
        assert set(entry["paths"]) <= set(rules[entry["id"]]["when-any"])
        suite = suites["qualification." + entry["id"]]
        assert suite["binding"] + ".yml" == entry["workflow"]
        assert sorted(suite.get("variants", ["default"])) == list(shadow.VARIANTS[entry["id"]])
        assert sorted(suite["requires"]) == list(shadow.CAPABILITIES[entry["id"]])
    assert policy["profiles"]["integration"]["mode"] == "full"


def test_pr_route_never_calls_qualification_workflows():
    pilot = yaml.load(
        (ROOT / ".github/workflows/test-planning-shadow.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(pilot["on"]) == {"pull_request", "workflow_dispatch"}
    assert pilot["permissions"] == {"contents": "read", "actions": "read", "checks": "read"}
    jobs = pilot["jobs"]
    assert jobs["shadow-aggregate"]["if"] == "always()"
    assert set(jobs["shadow-aggregate"]["needs"]) == {"planning", *shadow.JOBS}
    for name in shadow.JOBS:
        job = jobs[name]
        assert job["needs"] == "planning"
        assert job["if"] == (
            "github.event_name == 'workflow_dispatch' && "
            "needs.planning.outputs.full_shadow == 'true'"
        )
        assert job["uses"] == "./.github/workflows/" + shadow.WORKFLOWS[name] + ".yml"
        assert set(job) == {"needs", "if", "uses"}
    for name in ("planning", "shadow-aggregate"):
        steps = jobs[name]["steps"]
        checkouts = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
        assert [step["with"]["path"] for step in checkouts] == ["candidate", "adapter"]
        assert "github.event.pull_request.head.sha || github.sha" in checkouts[0]["with"]["ref"]
        assert "github.workflow_sha" in checkouts[1]["with"]["ref"]
        assert all(step["with"]["persist-credentials"] == "false" for step in checkouts)


def test_existing_native_binding_does_not_hide_matrix_or_cleanup_failures():
    native = yaml.load(
        (ROOT / ".github/workflows/upgrade-native-qualification.yml").read_text(),
        Loader=yaml.BaseLoader,
    )["jobs"]["native"]
    assert sorted(item["recipe"] for item in native["strategy"]["matrix"]["include"]) == list(
        shadow.VARIANTS["native"]
    )
    assert "if" not in native and "continue-on-error" not in native
    assert all("continue-on-error" not in step for step in native["steps"])
    cleanup = next(
        step for step in native["steps"] if step.get("name") == "Remove owned test resources"
    )
    assert cleanup["if"] == "always()"
    assert 'test -z "$(docker ps -aq' in cleanup["run"]
    assert "|| true" not in cleanup["run"]


@pytest.mark.parametrize("full", [False, True])
def test_aggregate_records_only_workflow_completion(full):
    result = verify(results(full), full)
    assert result["activation"] == "disabled"
    assert result["status"] == ("workflows-completed" if full else "comparison-completed")
    assert "no cell receipts inferred" in result["evidence"]


@pytest.mark.parametrize("job", ("planning", *shadow.JOBS))
@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", "timed_out", None])
def test_selected_missing_cancelled_skipped_or_failed_jobs_block(job, outcome):
    needs = results()
    needs[job]["result"] = outcome
    with pytest.raises(shadow.ShadowError):
        verify(needs)


@pytest.mark.parametrize("job", ("planning", *shadow.JOBS))
def test_missing_dependency_cannot_pass(job):
    needs = results()
    del needs[job]
    with pytest.raises(shadow.ShadowError):
        verify(needs)


@pytest.mark.parametrize("key", sorted(shadow.OUTPUT_KEYS))
def test_missing_planning_output_cannot_pass(key):
    needs = results()
    del needs["planning"]["outputs"][key]
    with pytest.raises(shadow.ShadowError):
        verify(needs)


@pytest.mark.parametrize(
    "key,value",
    [
        ("execution_commit", "d" * 40),
        ("execution_tree", "d" * 40),
        ("full_shadow", "false"),
        ("plan_digest", "invalid"),
        ("bindings_sha256", None),
    ],
)
def test_wrong_source_or_incomplete_manifest_blocks(key, value):
    needs = results()
    needs["planning"]["outputs"][key] = value
    with pytest.raises(shadow.ShadowError):
        verify(needs)


def test_pr_comparison_rejects_accidental_qualification_execution():
    needs = results(False)
    needs["application"]["result"] = "success"
    with pytest.raises(shadow.ShadowError):
        verify(needs, False)


def test_baseline_uses_the_current_fixed_selector_over_complete_paths():
    raw = (ROOT / "scripts/qualification_policy.json").read_bytes()
    assert shadow.baseline_selection(raw, ("docs/readme.md",)) == []
    assert shadow.baseline_selection(raw, ("scripts/qualification_policy.json",)) == sorted(
        shadow.JOBS
    )
    assert "monitoring" in shadow.baseline_selection(raw, ("qualification/platform_auth.py",))


@pytest.mark.parametrize("edit", ["missing", "unknown", "workflow", "paths"])
def test_baseline_incomplete_or_changed_bindings_fail(edit):
    policy = json.loads((ROOT / "scripts/qualification_policy.json").read_bytes())
    if edit == "missing":
        policy.pop()
    elif edit == "unknown":
        policy[0]["id"] = "unknown"
    elif edit == "workflow":
        policy[0]["workflow"] = "other.yml"
    else:
        policy[0]["paths"] = ["x"] * 129
    with pytest.raises(shadow.ShadowError):
        shadow.baseline_selection(json.dumps(policy).encode(), ())


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"x":{"a":1,"a":2}}',
        b"not json",
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
    ],
)
def test_duplicate_or_malformed_json_rejected(raw):
    with pytest.raises(shadow.ShadowError):
        shadow.decode(raw)


def test_input_and_output_bounds_are_exact(tmp_path, monkeypatch):
    path = tmp_path / "input.json"
    path.write_bytes(b"12345")
    assert shadow.read_bounded(path, 5) == b"12345"
    with pytest.raises(shadow.ShadowError):
        shadow.read_bounded(path, 4)
    with pytest.raises(shadow.ShadowError):
        shadow.decode(b"12345", 4)
    monkeypatch.setattr(shadow, "MAX_DOCUMENT_BYTES", 2)
    assert shadow.encode({}) == b"{}"
    with pytest.raises(shadow.ShadowError):
        shadow.encode({"a": 1})


def test_omissions_never_publish_success_outputs():
    with pytest.raises(shadow.ShadowError):
        shadow.job_outputs({"comparison.json": {"missing": ["native"]}}, full_shadow=False)


def test_cli_aggregate_failure_stays_bounded(monkeypatch, capsys):
    needs = results()
    needs["native"]["result"] = "cancelled\n::notice::injected"
    monkeypatch.setenv("SHADOW_NEEDS", json.dumps(needs))
    assert (
        shadow.main(
            ["verify", "--execution-commit", COMMIT, "--execution-tree", TREE, "--full-shadow"]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {
        "test-shadow-version": 1,
        "status": "error",
        "activation": "disabled",
        "error": "Shadow planning or aggregation failed; inspect bounded artifacts and inputs",
    }
