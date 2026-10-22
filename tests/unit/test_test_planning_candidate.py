"""Offline candidate checks cannot authorize a path policy or dispatch workloads."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import test_planning_candidate as candidate
from scripts import test_planning_shadow as shadow

ROOT = Path(__file__).resolve().parents[2]


def options():
    return {
        "expected_replay_sha256": candidate.REPLAY_SHA256,
        "expected_consumer_commit": candidate.AUDITED_CONSUMER["commit"],
        "expected_consumer_tree": candidate.AUDITED_CONSUMER["tree"],
    }


@pytest.fixture
def replay(monkeypatch):
    """Small generated cohort isolates consumer validation from installed YAGA."""
    baseline = (ROOT / "scripts/qualification_policy.json").read_bytes()
    assert hashlib.sha256(baseline).hexdigest() in {
        candidate.BASELINE_SHA256,
        candidate.SOURCE_BASELINE_SHA256,
    }
    cohort, observations, parsed = [], [], []
    for index in range(31):
        revision = (
            {**candidate.REVIEWED_REVISION, "merged_at": "2026-10-12T04:16:10Z"}
            if index == 0
            else {
                "number": 1000 + index,
                "base": f"{index:040x}",
                "head": f"{index + 31:040x}",
                "tree": f"{index + 62:040x}",
                "merged_at": "2026-10-12T04:16:10Z",
            }
        )
        paths = (
            list(candidate.REVIEWED_PATHS)
            if index == 0
            else [
                "scripts/qualification_policy.json"
                if index <= 9
                else "src/django_ray/unreviewed.py"
                if index <= 13
                else "docs/unreviewed.md"
            ]
        )
        source = {
            "base_commit": revision["base"],
            "comparison_commit": revision["base"],
            "head_commit": revision["head"],
            "execution_commit": revision["head"],
            "execution_tree": revision["tree"],
        }
        plan = {
            "source": source,
            "changed_paths": paths,
            "policy_sha256": candidate.REPLAY_POLICY_SHA256,
            "bindings_sha256": candidate.REPLAY_BINDINGS_SHA256,
        }
        digest = hashlib.sha256(shadow.encode(plan)).hexdigest()
        comparison = {
            "source": source,
            "adapter_source": dict(candidate.REPLAY_ADAPTER),
            "policy_sha256": candidate.REPLAY_POLICY_SHA256,
            "bindings_sha256": candidate.REPLAY_BINDINGS_SHA256,
            "baseline_policy_sha256": candidate.BASELINE_SHA256,
            "plan_digest": digest,
            "activation": "disabled",
            "runtime_observation": "not-observed",
            "baseline_selected": shadow.baseline_selection(baseline, tuple(paths)),
        }
        cohort.append(revision)
        observations.append(
            {
                "revision": revision,
                "comparison": comparison,
                "plan": {"plan": plan, "plan_digest": digest},
            }
        )
    document = {
        "activation": "disabled",
        "adapter_file_sha256": "1" * 64,
        "cohort": cohort,
        "experiment_version": 1,
        "mode": "historical-selection-replay",
        "observations": observations,
        "replay_file_sha256": "2" * 64,
        "repository": "dariuszpanas/django-ray",
        "summary": {"revisions": 31, "test_runtime_observations": 0},
    }

    def parse(value):
        parsed.append(value)
        if value["plan_digest"] != hashlib.sha256(shadow.encode(value["plan"])).hexdigest():
            raise ValueError("invalid plan digest")

    monkeypatch.setattr(candidate.importlib.metadata, "version", lambda _: "0.3.0")
    original = candidate.importlib.import_module
    monkeypatch.setattr(
        candidate.importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(parse_plan_document=parse)
            if name == "yaga.test_planning.diagnostics"
            else original(name)
        ),
    )
    raw = shadow.encode(document)
    monkeypatch.setattr(candidate, "REPLAY_SHA256", hashlib.sha256(raw).hexdigest())
    return SimpleNamespace(raw=raw, baseline=baseline, document=document, parsed=parsed)


def repin(monkeypatch, document):
    raw = shadow.encode(document)
    monkeypatch.setattr(candidate, "REPLAY_SHA256", hashlib.sha256(raw).hexdigest())
    return raw


def test_fixed_counterfactual_recomputes_every_baseline_and_preserves_native_cells(
    replay, monkeypatch
):
    calls = []
    baseline_selection = shadow.baseline_selection

    def select(raw, paths):
        calls.append(paths)
        return baseline_selection(raw, paths)

    monkeypatch.setattr(shadow, "baseline_selection", select)
    report = candidate.evaluate(replay.raw, replay.baseline, **options())
    assert len(calls) == len(replay.parsed) == 31
    assert (
        report["authority"] == "historical-counterfactual-only"
        and report["activation"] == "disabled"
    )
    assert report["audited_consumer_source"] != report["retained_replay_adapter_source"]
    assert report["future_coverage_review"] == "required"
    assert report["baseline_policy_sha256"] == hashlib.sha256(replay.baseline).hexdigest()
    assert report["retained_replay_baseline_policy_sha256"] == candidate.BASELINE_SHA256
    assert report["git_source_baseline_policy_sha256"] == candidate.SOURCE_BASELINE_SHA256
    assert report["summary"] == {
        "revisions": 31,
        "baseline_suite_selections": 50,
        "candidate_suite_selections": 49,
        "baseline_variant_cells": 77,
        "candidate_variant_cells": 76,
        "unchanged_revisions": 30,
        "feedback_time_savings_seconds": None,
        "runner_minute_savings": None,
        "new_runtime_observations": 0,
    }
    changed = [row for row in report["observations"] if row["reviewed_exception"]]
    assert len(changed) == 1 and changed[0]["revision"]["number"] == 621
    assert (
        changed[0]["baseline_selected"] == ["application"]
        and changed[0]["candidate_selected"] == []
    )


@pytest.mark.parametrize(
    "change",
    ["head", "tree", "base", "number", "runtime", "new-module", "missing-path", "additional-suite"],
)
def test_only_reviewed_source_and_complete_mixed_inventory_can_remove_application(change):
    revision, paths, baseline = (
        dict(candidate.REVIEWED_REVISION),
        list(candidate.REVIEWED_PATHS),
        ["application"],
    )
    if change in {"head", "tree", "base"}:
        revision[change] = "f" * 40
    elif change == "number":
        revision["number"] = 622
    elif change == "runtime":
        paths.append("src/django_ray/backends.py")
    elif change == "new-module":
        paths.append("src/django_ray/doctor/future.py")
    elif change == "missing-path":
        paths.remove("scripts/verify_wheel.py")
    else:
        baseline.append("native")
    assert candidate.candidate_selection(revision, paths, baseline) == baseline


@pytest.mark.parametrize(
    "revision,paths,baseline",
    [
        (None, [], []),
        ({**candidate.REVIEWED_REVISION, "number": True}, [], []),
        ({**candidate.REVIEWED_REVISION, "tree": "not-a-tree"}, [], []),
        (candidate.REVIEWED_REVISION, None, []),
        (candidate.REVIEWED_REVISION, [None], []),
        (candidate.REVIEWED_REVISION, ["../escape"], []),
        (candidate.REVIEWED_REVISION, ["/absolute"], []),
        (candidate.REVIEWED_REVISION, ["a", "a"], []),
        (candidate.REVIEWED_REVISION, ["a\\b"], []),
        (candidate.REVIEWED_REVISION, [], "application"),
        (candidate.REVIEWED_REVISION, [], ["unknown"]),
        (candidate.REVIEWED_REVISION, [], ["application", "application"]),
    ],
)
def test_pure_candidate_rejects_malformed_inputs(revision, paths, baseline):
    with pytest.raises(shadow.ShadowError):
        candidate.candidate_selection(revision, paths, baseline)


@pytest.mark.parametrize(
    "fault",
    [
        "corrupt",
        "truncated",
        "supplied-hash",
        "caller-repinned",
        "stale-policy",
        "future-consumer",
        "old-adapter-as-consumer",
        "oversized",
    ],
)
def test_fixed_review_identity_cannot_be_replaced_by_caller_supplied_hash(replay, fault):
    raw, baseline, expected = replay.raw, replay.baseline, options()
    if fault == "corrupt":
        raw += b"tamper"
    elif fault == "truncated":
        raw = raw[:-1]
    elif fault == "supplied-hash":
        expected["expected_replay_sha256"] = "0" * 64
    elif fault == "caller-repinned":
        raw += b" "
        expected["expected_replay_sha256"] = hashlib.sha256(raw).hexdigest()
    elif fault == "stale-policy":
        baseline += b" "
    elif fault == "future-consumer":
        expected["expected_consumer_commit"] = "f" * 40
    elif fault == "old-adapter-as-consumer":
        expected["expected_consumer_commit"] = candidate.REPLAY_ADAPTER["commit"]
        expected["expected_consumer_tree"] = candidate.REPLAY_ADAPTER["tree"]
    else:
        raw = b"x" * (shadow.MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(shadow.ShadowError):
        candidate.evaluate(raw, baseline, **expected)
    assert replay.parsed == []


def test_duplicate_json_keys_are_rejected_before_plan_parsing(replay, monkeypatch):
    raw = replay.raw.replace(
        b'"activation":"disabled"', b'"activation":"disabled","activation":"disabled"', 1
    )
    monkeypatch.setattr(candidate, "REPLAY_SHA256", hashlib.sha256(raw).hexdigest())
    with pytest.raises(shadow.ShadowError, match="Duplicate"):
        candidate.evaluate(raw, replay.baseline, **options())
    assert replay.parsed == []


@pytest.mark.parametrize("line_endings", ["LF", "CRLF"])
def test_only_reviewed_lf_and_crlf_baselines_preserve_distinct_evidence_hashes(
    replay, line_endings
):
    source_bytes = replay.baseline.replace(b"\r\n", b"\n")
    retained_bytes = source_bytes.replace(b"\n", b"\r\n")
    assert hashlib.sha256(source_bytes).hexdigest() == candidate.SOURCE_BASELINE_SHA256
    assert hashlib.sha256(retained_bytes).hexdigest() == candidate.BASELINE_SHA256
    raw = source_bytes if line_endings == "LF" else retained_bytes
    report = candidate.evaluate(replay.raw, raw, **options())
    assert report["baseline_policy_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["retained_replay_baseline_policy_sha256"] == candidate.BASELINE_SHA256
    assert report["git_source_baseline_policy_sha256"] == candidate.SOURCE_BASELINE_SHA256
    assert report["summary"]["baseline_suite_selections"] == 50
    assert report["summary"]["candidate_suite_selections"] == 49
    assert len(replay.parsed) == 31


@pytest.mark.parametrize(
    "drift", ["space", "bom", "trailing-newline", "cr-only", "mixed", "semantic"]
)
def test_other_baseline_bytes_fail_before_replaying_any_row(replay, drift):
    raw = replay.baseline.replace(b"\r\n", b"\n")
    if drift == "space":
        raw += b" "
    elif drift == "bom":
        raw = b"\xef\xbb\xbf" + raw
    elif drift == "trailing-newline":
        raw += b"\n"
    elif drift == "cr-only":
        raw = raw.replace(b"\n", b"\r")
    elif drift == "mixed":
        raw = raw.replace(b"\n", b"\r\n", 1)
    else:
        raw = raw.replace(b"src/django_ray/**", b"src/django_ray/doctor/**")
    with pytest.raises(shadow.ShadowError, match="baseline policy changed"):
        candidate.evaluate(replay.raw, raw, **options())
    assert replay.parsed == []


def test_lf_input_cannot_rewrite_the_retained_replay_baseline_identity(replay, monkeypatch):
    document = copy.deepcopy(replay.document)
    document["observations"][0]["comparison"]["baseline_policy_sha256"] = (
        candidate.SOURCE_BASELINE_SHA256
    )
    raw = repin(monkeypatch, document)
    with pytest.raises(shadow.ShadowError, match="comparison identities differ"):
        candidate.evaluate(raw, replay.baseline.replace(b"\r\n", b"\n"), **options())


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate-revision",
        "missing-row",
        "wrong-source",
        "wrong-plan-digest",
        "wrong-adapter",
        "wrong-policy",
        "wrong-bindings",
        "wrong-baseline",
    ],
)
def test_semantic_identity_checks_remain_independent_of_archive_integrity(
    replay, monkeypatch, fault
):
    document = copy.deepcopy(replay.document)
    row = document["observations"][0]
    if fault == "duplicate-revision":
        document["cohort"][1] = document["cohort"][0]
        document["observations"][1] = row
    elif fault == "missing-row":
        document["observations"].pop()
    elif fault == "wrong-source":
        row["comparison"]["source"] = {**row["comparison"]["source"], "execution_tree": "f" * 40}
    elif fault == "wrong-plan-digest":
        row["plan"]["plan_digest"] = "f" * 64
    elif fault == "wrong-adapter":
        row["comparison"]["adapter_source"] = dict(candidate.AUDITED_CONSUMER)
    elif fault == "wrong-policy":
        row["comparison"]["policy_sha256"] = "f" * 64
    elif fault == "wrong-bindings":
        row["comparison"]["bindings_sha256"] = "f" * 64
    else:
        row["comparison"]["baseline_selected"] = []
    with pytest.raises((shadow.ShadowError, ValueError)):
        candidate.evaluate(repin(monkeypatch, document), replay.baseline, **options())


def test_cli_writes_only_a_new_report_and_never_github_output(
    replay, tmp_path, monkeypatch, capsys
):
    replay_path, policy_path, output = (
        tmp_path / "replay.json",
        tmp_path / "policy.json",
        tmp_path / "candidate.json",
    )
    replay_path.write_bytes(replay.raw)
    policy_path.write_bytes(replay.baseline)
    github_output = tmp_path / "github-output"
    github_output.write_text("preserve")
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        shadow, "generate", lambda *_a, **_kw: pytest.fail("Candidate dispatched planning")
    )
    arguments = [
        "--replay",
        str(replay_path),
        "--baseline-policy",
        str(policy_path),
        "--output",
        str(output),
    ]
    for key, value in options().items():
        arguments.extend(("--" + key.replace("_", "-"), value))
    assert candidate.main(arguments) == 0
    original = output.read_bytes()
    assert json.loads(original)["activation"] == "disabled"
    assert github_output.read_text() == "preserve"
    assert candidate.main(arguments) == 2
    assert output.read_bytes() == original
    assert "no selection was activated" in capsys.readouterr().out


def test_pure_exception_helper_never_loads_yaga_or_planning(monkeypatch):
    monkeypatch.setattr(
        candidate.importlib, "import_module", lambda *_a: pytest.fail("Pure helper loaded YAGA")
    )
    assert (
        candidate.candidate_selection(
            dict(candidate.REVIEWED_REVISION), candidate.REVIEWED_PATHS, ["application"]
        )
        == []
    )


def test_cli_help_identifies_frozen_replay_and_audited_consumer(capsys):
    with pytest.raises(SystemExit) as exit_status:
        candidate.main(["--help"])
    assert exit_status.value.code == 0
    help_text = capsys.readouterr().out
    assert candidate.REPLAY_SHA256 in help_text
    assert candidate.AUDITED_CONSUMER["commit"] in help_text
    assert "Selection remains disabled" in help_text
