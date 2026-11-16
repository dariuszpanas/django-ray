"""Resource-free producer checks; synthetic records do not authenticate a run."""

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

from qualification.application import execution_witness as witness
from qualification.application.resource_profiles import select_profile
from qualification.application.run_chainsaw import RECEIPTS

ROOT = Path(__file__).resolve().parents[2]
HEAD, TREE, BASE, MERGE = (character * 40 for character in "abcd")
SECRET = "private-provider-value-that-must-not-be-published"


@pytest.fixture
def sample(tmp_path):
    env = {
        "GITHUB_REPOSITORY": "dariuszpanas/django-ray",
        "GITHUB_REPOSITORY_ID": "790856348",
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_ACTOR_ID": "15094983",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_SHA": MERGE,
        "GITHUB_WORKFLOW_SHA": MERGE,
        "GITHUB_WORKFLOW_REF": "dariuszpanas/django-ray/.github/workflows/qualification-gate.yml@refs/pull/724/merge",
        "SOURCE_SHA": HEAD,
        "RESOURCE_PROFILE": "standard",
        "VALIDATION_INTENT": "acceptance",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "ImageOS": "ubuntu26",
        "ImageVersion": "20261101.186.1",
        "WITNESS_ARTIFACT_ID": "5678",
        "WITNESS_ARTIFACT_DIGEST": "e" * 64,
        "GITHUB_TOKEN": SECRET,
        **dict.fromkeys(("WITNESS_STEP_" + step.upper() for step in witness.STEPS), "success"),
    }
    event = {
        "repository": {"id": 790856348, "full_name": env["GITHUB_REPOSITORY"]},
        "sender": {"id": 15094983},
        "pull_request": {
            "number": 724,
            "user": {"id": 15094983},
            "base": {"sha": BASE, "repo": {"id": 790856348}},
            "head": {"sha": HEAD, "repo": {"id": 790856348}},
            "title": SECRET,
            "body": SECRET,
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    for name in witness.FILES:
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(json.dumps({"unpublished_payload": SECRET}).encode())
    build = tmp_path / "application-build"
    (build / "source.txt").write_text(HEAD + "\n" + TREE + "\n", encoding="ascii")
    (build / "source.sha256").write_text("f" * 64 + "  /temporary/source.tar\n", encoding="ascii")
    (build / "resource-profile.txt").write_text("standard\n", encoding="ascii")
    (build / "validation-intent.txt").write_text("acceptance\n", encoding="ascii")
    images = [
        {
            "RepoDigests": [f"localhost:5001/application-{role}@sha256:" + "e" * 64],
            "Config": {"Env": [SECRET]},
        }
        for role in ("base", "core")
    ]
    (build / "images.json").write_text(json.dumps(images), encoding="utf-8")
    summary = {
        "status": "passed",
        "namespace_removed": True,
        "source_tree": TREE,
        "image": images[1]["RepoDigests"][0],
        "cold_ray": "required",
        "complete_application_gate": False,
        "qualification_profile": select_profile("standard", "acceptance"),
        "namespace": SECRET,
    }
    summary_path = tmp_path / "application-evidence/summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    return env, event, event_path, tmp_path, summary_path


def produce(sample):
    env, _, event_path, temporary, _ = sample
    return witness.produce(env, event_path=event_path, temporary=temporary)


def test_direct_record_has_fixed_schema_and_no_raw_secrets(sample):
    result = produce(sample)
    assert result["failures"] == []
    assert set(result) == {
        "schema",
        "schema_version",
        "mode",
        "status",
        "failures",
        "github",
        "profile",
        "runner",
        "steps",
        "source",
        "tested_images",
        "observation",
        "receipt_artifact",
        "receipt_files",
        "self_authenticating",
        "reuse_enabled",
        "complete_application_gate",
    }
    assert result["schema"] == witness.SCHEMA and result["schema_version"] == 1
    assert result["mode"] == "direct_execution" and result["status"] == "passed"
    assert result["failures"] == []
    assert (
        result["self_authenticating"]
        is result["reuse_enabled"]
        is result["complete_application_gate"]
        is False
    )
    assert result["github"]["attempt"] == 2
    assert result["github"]["event_sha"] == MERGE
    assert result["github"]["pull_request"] == {
        "number": 724,
        "author_id": 15094983,
        "base_repository_id": 790856348,
        "head_repository_id": 790856348,
        "base_sha": BASE,
        "head_sha": HEAD,
    }
    assert result["source"] == {"head": HEAD, "tree": TREE, "archive_sha256": "f" * 64}
    assert result["receipt_artifact"] == {
        "id": 5678,
        "name": "application-core-1234",
        "digest": "sha256:" + "e" * 64,
    }
    assert len(result["receipt_files"]) == 25
    for row in result["receipt_files"]:
        raw = (sample[3] / row["path"]).read_bytes()
        assert set(row) == {"path", "bytes", "sha256"}
        assert row["bytes"] == len(raw) and row["sha256"] == hashlib.sha256(raw).hexdigest()
    encoded = witness.encode(result)
    assert len(encoded) <= witness.MAX_OUTPUT_BYTES
    assert SECRET.encode() not in encoded
    assert b"GITHUB_TOKEN" not in encoded and b'namespace"' not in encoded


def test_fixed_file_inventory_covers_current_receipt_names_without_discovery():
    names = {name for _, group in RECEIPTS.values() for name in group}
    names |= {"before-protected-logs", "after-protected-logs", "producers", "summary"}
    assert set(witness.RECEIPT_NAMES) == names
    assert len(witness.FILES) == len(set(witness.FILES)) == 25
    parsed = ast.parse(Path(witness.__file__).read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
    assert imports <= sys.stdlib_module_names | {"__future__"}
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"rglob", "glob"}
        for node in ast.walk(parsed)
    )


@pytest.mark.parametrize("step", witness.STEPS)
def test_failed_or_skipped_step_never_becomes_a_passing_record(sample, step):
    sample[0]["WITNESS_STEP_" + step.upper()] = "failure"
    result = produce(sample)
    assert result["status"] == "incomplete" and "steps_not_successful" in result["failures"]
    assert result["steps"][step] == "failure"
    assert result["source"]["tree"] == TREE
    if step == "receipts":
        assert result["receipt_artifact"] is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_RUN_ATTEMPT", "0"),
        ("GITHUB_RUN_ID", "9" * 10000),
        ("GITHUB_ACTOR_ID", True),
        ("GITHUB_WORKFLOW_SHA", SECRET),
        ("WITNESS_ARTIFACT_ID", "-1"),
        ("WITNESS_ARTIFACT_ID", ""),
        ("WITNESS_ARTIFACT_DIGEST", SECRET),
        ("WITNESS_ARTIFACT_DIGEST", ""),
        ("WITNESS_STEP_NATIVE", SECRET),
        ("ImageVersion", SECRET + "\n"),
    ],
)
def test_invalid_fixed_context_is_partial_without_echoing_values(sample, key, value):
    sample[0][key] = value
    result = produce(sample)
    assert result["status"] == "incomplete" and set(result["failures"]) <= witness.FAILURES
    assert SECRET.encode() not in witness.encode(result)


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "summary_tree",
        "summary_image",
        "summary_failed",
        "summary_cleanup",
        "summary_profile",
        "missing_file",
        "empty_file",
    ],
)
def test_missing_or_contradictory_execution_facts_remain_partial(sample, change):
    _, _, _, temporary, summary_path = sample
    summary = json.loads(summary_path.read_bytes())
    if change == "source":
        (temporary / "application-build/source.txt").write_text(
            BASE + "\n" + TREE, encoding="ascii"
        )
    elif change == "missing_file":
        (temporary / "application-evidence/before-core.json").unlink()
    elif change == "empty_file":
        (temporary / "application-evidence/before-core.json").write_bytes(b"")
    else:
        key, value = {
            "summary_tree": ("source_tree", BASE),
            "summary_image": ("image", "wrong-image"),
            "summary_failed": ("status", "failed"),
            "summary_cleanup": ("namespace_removed", False),
            "summary_profile": (
                "qualification_profile",
                {"resource_profile": "constrained-ray", "validation_intent": "diagnostic"},
            ),
        }[change]
        summary[key] = value
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
    assert produce(sample)["status"] == "incomplete"


@pytest.mark.parametrize("reference", ["refs/heads/main", "refs/tags/v0.8.0+build.1"])
@pytest.mark.parametrize("intent", ["acceptance", "diagnostic"])
def test_manual_branch_and_tag_records_retain_selected_intent(sample, reference, intent):
    env, event, event_path, temporary, summary_path = sample
    profile = "constrained-ray" if intent == "diagnostic" else "standard"
    env.update(
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_SHA=HEAD,
        VALIDATION_INTENT=intent,
        RESOURCE_PROFILE=profile,
        WITNESS_STEP_CI="skipped" if intent == "diagnostic" else "success",
    )
    env["GITHUB_WORKFLOW_REF"] = (
        "dariuszpanas/django-ray/.github/workflows/application-qualification.yml@" + reference
    )
    event.pop("pull_request")
    event_path.write_text(json.dumps(event), encoding="utf-8")
    for name, value in (
        ("resource-profile.txt", profile),
        ("validation-intent.txt", intent),
    ):
        (temporary / "application-build" / name).write_text(value, encoding="ascii")
    summary = json.loads(summary_path.read_bytes())
    summary["qualification_profile"] = select_profile(profile, intent)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    result = produce(sample)
    assert result["status"] == "passed" and result["github"]["pull_request"] is None
    assert result["profile"] == {"resource_profile": profile, "validation_intent": intent}
    assert result["reuse_enabled"] is False


@pytest.mark.parametrize(
    "reference", ["refs/heads/main", "refs/tags/v0.8.0", "refs/pull/725/merge"]
)
def test_pr_context_still_requires_its_exact_merge_ref(sample, reference):
    sample[0]["GITHUB_WORKFLOW_REF"] = (
        "dariuszpanas/django-ray/.github/workflows/qualification-gate.yml@" + reference
    )
    assert produce(sample)["github"] is None


def test_missing_optional_runner_image_environment_is_unobserved(sample):
    sample[0].pop("ImageOS")
    sample[0].pop("ImageVersion")
    result = produce(sample)
    assert result["status"] == "passed"
    assert result["runner"]["ImageOS"] is result["runner"]["ImageVersion"] is None


@pytest.mark.parametrize("literal", ["1e999", "-1e999", "NaN", "Infinity"])
def test_nonfinite_summary_profile_retains_available_direct_facts(sample, literal):
    summary_path = sample[4]
    summary = json.loads(summary_path.read_bytes())
    summary["qualification_profile"]["invalid_number"] = "replace-with-numeric-input"
    raw = json.dumps(summary).replace('"replace-with-numeric-input"', literal)
    summary_path.write_text(raw, encoding="utf-8")
    result = produce(sample)
    assert result["status"] == "incomplete"
    assert result["failures"] == ["summary_unavailable"]
    assert result["observation"] is None
    assert result["github"]["run_id"] == 1234 and result["source"]["tree"] == TREE
    assert result["receipt_artifact"]["id"] == 5678
    assert result["tested_images"]["application"].startswith("localhost:5001/application-core@")
    assert len(result["receipt_files"]) == 25


def test_event_and_evidence_byte_limits_and_duplicate_keys(sample, monkeypatch):
    event_path = sample[2]
    original = event_path.read_bytes()
    event_path.write_bytes(b" " * (witness.MAX_EVENT_BYTES + 1))
    assert produce(sample)["github"] is None
    event_path.write_bytes(b'{"repository":{},"repository":{}}')
    assert produce(sample)["github"] is None
    event_path.write_bytes(original)
    (sample[3] / "application-evidence/before-core.json").write_bytes(
        b"x" * (witness.MAX_FILE_BYTES + 1)
    )
    assert "receipt_inventory_incomplete" in produce(sample)["failures"]
    monkeypatch.setattr(witness, "MAX_TOTAL_FILE_BYTES", 100)
    result = produce(sample)
    assert result["status"] == "incomplete"
    assert sum(row["bytes"] for row in result["receipt_files"]) <= 100


def test_linked_receipt_is_not_read(sample):
    path = sample[3] / "application-evidence/before-core.json"
    path.unlink()
    try:
        path.symlink_to(sample[2])
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    result = produce(sample)
    assert result["status"] == "incomplete"
    assert not any(row["path"].endswith("before-core.json") for row in result["receipt_files"])


def test_output_bound_and_cli_fixed_failure_record(sample, monkeypatch, capsys):
    for key, value in sample[0].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RUNNER_TEMP", str(sample[3]))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(sample[2]))
    monkeypatch.setenv("WITNESS_ARTIFACT_DIGEST", SECRET)
    assert witness.main() == 1
    output = sample[3] / "application-witness/execution-witness.json"
    raw = output.read_bytes()
    assert json.loads(raw)["status"] == "incomplete" and SECRET.encode() not in raw
    assert SECRET not in capsys.readouterr().out
    assert witness.main() == 1  # Never overwrite a previous attempt's local record.
    assert output.read_bytes() == raw
    monkeypatch.setattr(witness, "MAX_OUTPUT_BYTES", 1)
    with pytest.raises(witness.WitnessError):
        produce(sample)


def test_workflow_records_after_execution_cleanup_and_upload_without_enabling_reuse():
    workflow = yaml.load(
        (ROOT / ".github/workflows/application-qualification.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert workflow["permissions"] == {"contents": "read", "checks": "read", "actions": "read"}
    job = workflow["jobs"]["application-core"]
    assert job["timeout-minutes"] == "132"
    steps = job["steps"]
    original = {step["id"]: step for step in steps if "id" in step}
    assert set(original) == set(witness.STEPS)
    assert [
        original[name].get("timeout-minutes")
        for name in ("ci", "tools", "build", "cluster", "native", "cleanup")
    ] == ["36", "5", "15", "10", "55", "5"]
    assert original["receipts"]["with"]["name"] == "application-core-${{ github.run_id }}"
    producer, upload = steps[-2:]
    assert producer["if"] == upload["if"] == "always()"
    assert producer["timeout-minutes"] == upload["timeout-minutes"] == "1"
    assert (
        steps.index(original["native"])
        < steps.index(original["cleanup"])
        < steps.index(original["receipts"])
        < steps.index(producer)
    )
    expected = {
        "WITNESS_STEP_" + key.upper(): "${{ steps." + key + ".outcome }}" for key in witness.STEPS
    }
    expected.update(
        WITNESS_ARTIFACT_ID="${{ steps.receipts.outputs.artifact-id }}",
        WITNESS_ARTIFACT_DIGEST="${{ steps.receipts.outputs.artifact-digest }}",
    )
    assert producer["env"] == expected
    assert producer["run"] == "python -m qualification.application.execution_witness"
    assert (
        upload["with"]["name"]
        == "application-execution-witness-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert upload["with"]["if-no-files-found"] == "error"
    assert not any("continue-on-error" in step for step in steps)
    assert "toJSON(steps)" not in json.dumps(workflow)
