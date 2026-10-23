"""The reusable doctor comparison is bounded evidence, never CI authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import qualification_gate
from scripts import test_planning_candidate as historical
from scripts import test_planning_doctor_candidate as candidate
from scripts import test_planning_shadow as shadow

ROOT = Path(__file__).resolve().parents[2]
BASE, HEAD, TREE = "1" * 40, "2" * 40, "3" * 40
BASE_TREE = "4" * 40
SOURCE = "src/django_ray/doctor/probe.py"


@dataclass(frozen=True)
class ModeEntry:
    """Test stand-in for the installed reader's already validated typed metadata."""

    path: str
    oid: str
    mode: str = "100644"
    object_type: str = "blob"


@dataclass(frozen=True)
class ModeSelection:
    commit_sha: str
    tree_sha: str
    entries: tuple[ModeEntry, ...]


@pytest.fixture
def baseline():
    raw = (ROOT / "scripts/qualification_policy.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() in {
        historical.BASELINE_SHA256,
        historical.SOURCE_BASELINE_SHA256,
    }
    return raw


@pytest.fixture
def collector(monkeypatch):
    calls, structure_reads, imports = [], [], []
    paths = sorted((*candidate.SOURCE_PATHS, *candidate.COMPANION_PATHS, "README.md"))
    modes = {
        commit: ModeSelection(
            commit_sha=commit,
            tree_sha=tree,
            entries=tuple(
                ModeEntry(path, f"{index + offset:040x}") for index, path in enumerate(paths)
            ),
        )
        for commit, tree, offset in ((BASE, BASE_TREE, 10), (HEAD, TREE, 100))
    }
    reader_failures = {}
    collected = SimpleNamespace(
        source=SimpleNamespace(
            base_commit=BASE,
            head_commit=HEAD,
            comparison_commit=BASE,
            execution_commit=HEAD,
            execution_tree=TREE,
        ),
        changed_paths=(SOURCE,),
    )

    def collect(repo, *, revision_range):
        calls.append((repo, revision_range))
        return collected

    def read_modes(repo, revision):
        structure_reads.append((repo, revision))
        if revision in reader_failures:
            raise reader_failures[revision]
        return modes[revision]

    monkeypatch.setattr(candidate.importlib.metadata, "version", lambda _: "0.3.0")
    original = candidate.importlib.import_module

    def load(name):
        imports.append(name)
        if name == "yaga.test_planning.git":
            return SimpleNamespace(collect_test_source=collect)
        if name == "yaga.modes.git":
            return SimpleNamespace(read_mode_selection=read_modes)
        if name == "yaga.modes.models":
            return SimpleNamespace(ModeSelection=ModeSelection)
        return original(name)

    monkeypatch.setattr(candidate.importlib, "import_module", load)
    return SimpleNamespace(
        calls=calls,
        collected=collected,
        structure_reads=structure_reads,
        modes=modes,
        imports=imports,
        reader_failures=reader_failures,
    )


def evaluate(baseline, **kwargs):
    return candidate.evaluate_range(
        ROOT, baseline, **({"base": BASE, "head": HEAD, "expected_tree": TREE} | kwargs)
    )


@pytest.mark.parametrize("path", candidate.SOURCE_PATHS)
def test_each_exact_reviewed_source_is_path_eligible_only(baseline, path):
    assert candidate.compare([path], baseline) == {
        "baseline_selected": ["application"],
        "candidate_selected": [],
        "reason": candidate.ELIGIBLE_REASON,
        "changed_paths": [path],
    }


def test_exact_inventory_and_companions_preserve_complete_sorted_paths(baseline):
    assert candidate.SOURCE_PATHS == (
        "src/django_ray/doctor/__init__.py",
        "src/django_ray/doctor/__main__.py",
        "src/django_ray/doctor/contracts.py",
        "src/django_ray/doctor/probe.py",
        "src/django_ray/doctor/runner.py",
        "src/django_ray/doctor/supervisor.py",
        "src/django_ray/management/commands/django_ray_doctor.py",
    )
    assert candidate.COMPANION_PATHS == (
        "docs/reference/doctor.md",
        "scripts/verify_doctor_wheel.py",
        "tests/integration/test_doctor_postgresql.py",
        "tests/unit/test_doctor.py",
        "tests/unit/test_doctor_supervision.py",
        "tests/unit/test_verify_doctor_wheel.py",
    )
    paths = list(reversed(candidate.SOURCE_PATHS + candidate.COMPANION_PATHS))
    original = list(paths)
    result = candidate.compare(paths, baseline)
    assert result["reason"] == candidate.ELIGIBLE_REASON
    assert result["changed_paths"] == sorted(paths)
    assert paths == original


@pytest.mark.parametrize("paths", [[], candidate.COMPANION_PATHS])
def test_empty_and_companion_only_changes_never_qualify(baseline, paths):
    result = candidate.compare(paths, baseline)
    assert result["reason"] == "no-reviewed-doctor-source"
    assert result["candidate_selected"] == result["baseline_selected"] == []


@pytest.mark.parametrize(
    "other",
    [
        "src/django_ray/backends.py",
        "src/django_ray/doctor/future.py",
        "src/django_ray/doctor/Probe.py",
        "src/django_ray/management/commands/django_ray_worker.py",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/application-qualification.yml",
        ".github/test-suite-taxonomy.json",
        "scripts/qualification_policy.json",
        "scripts/verify_wheel.py",
        "tests/unit/test_unreviewed_doctor.py",
        "docs/reference/cli.md",
        "adrs/adr-0006-bounded-operator-diagnosis.md",
        "Makefile",
        "zensical.toml",
    ],
)
def test_any_path_outside_inventory_retains_every_baseline_suite(baseline, other):
    result = candidate.compare([SOURCE, other], baseline)
    expected = shadow.baseline_selection(baseline, (SOURCE, other))
    assert result["reason"] == "outside-reviewed-doctor-paths"
    assert result["candidate_selected"] == result["baseline_selected"] == expected


def test_cross_boundary_rename_keeps_old_and_new_paths_in_baseline(collector, baseline):
    collector.collected.changed_paths = ("src/django_ray/backends.py", SOURCE)
    result = evaluate(baseline)
    assert not result["path_eligible"]
    assert result["baseline_selected"] == result["candidate_selected"]
    assert len(result["baseline_selected"]) > 1
    assert result["changed_paths"] == sorted(collector.collected.changed_paths)
    assert not result["candidate_eligible"]
    assert collector.structure_reads == []


@pytest.mark.parametrize("selected", [[], ["application", "native"]])
def test_even_allowed_paths_require_exact_application_only_baseline(
    baseline, monkeypatch, selected
):
    monkeypatch.setattr(shadow, "baseline_selection", lambda *_: selected)
    result = candidate.compare([SOURCE], baseline)
    assert result["reason"] == "baseline-not-application-only"
    assert result["candidate_selected"] == selected


@pytest.mark.parametrize("line_endings", ["LF", "CRLF"])
def test_only_the_two_reviewed_byte_policies_are_accepted(collector, baseline, line_endings):
    raw = baseline.replace(b"\r\n", b"\n")
    if line_endings == "CRLF":
        raw = raw.replace(b"\n", b"\r\n")
    report = evaluate(raw)
    expected = (
        historical.BASELINE_SHA256 if line_endings == "CRLF" else historical.SOURCE_BASELINE_SHA256
    )
    assert report["baseline_policy_sha256"] == hashlib.sha256(raw).hexdigest() == expected
    assert report["path_eligible"]


@pytest.mark.parametrize(
    "fault", ["space", "bom", "mixed", "semantic", "duplicate", "truncated", "type", "size"]
)
def test_baseline_drift_fails_before_git_collection(collector, baseline, fault):
    raw = baseline.replace(b"\r\n", b"\n")
    if fault == "space":
        raw += b" "
    elif fault == "bom":
        raw = b"\xef\xbb\xbf" + raw
    elif fault == "mixed":
        raw = raw.replace(b"\n", b"\r\n", 1)
    elif fault == "semantic":
        raw = raw.replace(b"src/django_ray/**", b"src/django_ray/doctor/**")
    elif fault == "duplicate":
        raw = raw.replace(b'"id": "application"', b'"id": "application", "id": "application"')
    elif fault == "truncated":
        raw = raw[:-1]
    elif fault == "type":
        raw = bytearray(raw)
    else:
        raw = b"x" * (shadow.MAX_BASELINE_BYTES + 1)
    with pytest.raises(shadow.ShadowError):
        evaluate(raw)
    assert collector.calls == []


@pytest.mark.parametrize(
    "paths",
    [
        None,
        SOURCE,
        {SOURCE},
        [None],
        [1],
        [b"path"],
        [""],
        ["/absolute"],
        ["C:/absolute"],
        ["C:relative"],
        ["../escape"],
        ["a/../b"],
        ["a/./b"],
        ["a//b"],
        ["a/"],
        ["a\\b"],
        ["a\x00b"],
        ["a\x1bb"],
        ["a\x7fb"],
        ["\ud800"],
        [SOURCE, SOURCE],
        ["a" * 4097],
        ["é" * 2049],
        [f"path-{index}" for index in range(3001)],
    ],
)
def test_malformed_paths_are_errors_even_when_baseline_would_select_nothing(baseline, paths):
    with pytest.raises(shadow.ShadowError):
        candidate.compare(paths, baseline)


@pytest.mark.parametrize(
    "paths", [["a" * 4096], ["é" * 2048], [f"path-{index}" for index in range(3000)]]
)
def test_path_limits_accept_complete_inventories_at_the_boundary(baseline, paths):
    result = candidate.compare(paths, baseline)
    assert result["changed_paths"] == sorted(paths)
    assert result["candidate_selected"] == result["baseline_selected"]


def test_complete_comparison_must_fit_bounded_json(baseline):
    paths = [f"{index:04d}" + "a" * 4092 for index in range(1100)]
    with pytest.raises(shadow.ShadowError, match="byte limit"):
        candidate.compare(paths, baseline)


def test_report_binds_exact_range_without_claiming_coverage_or_runtime(collector, baseline):
    report = evaluate(baseline)
    assert collector.calls == [(ROOT, f"{BASE}..{HEAD}")]
    assert report["source"] == vars(collector.collected.source)
    assert report["range"] == f"{BASE}..{HEAD}"
    assert report["comparison_kind"] == "explicit-two-dot-range"
    assert report["authority"] == "offline-counterfactual-only"
    assert report["activation"] == "disabled"
    assert report["path_eligible"]
    assert report["candidate_eligible"]
    assert report["dependency_review"] == report["coverage_review"] == "required"
    assert (
        report["rule_path_inventory_sha256"]
        == hashlib.sha256(shadow.encode(report["rule_path_inventory"])).hexdigest()
    )
    assert report["summary"] == {
        "baseline_suite_selections": 1,
        "candidate_suite_selections": 0,
        "baseline_variant_cells": 1,
        "candidate_variant_cells": 0,
        "feedback_time_savings_seconds": None,
        "runner_minute_savings": None,
        "new_runtime_observations": 0,
    }


def test_structure_metadata_binds_both_endpoints_without_requiring_unchanged_blob_ids(
    collector, baseline
):
    report = evaluate(baseline)
    assert collector.structure_reads == [(ROOT, BASE), (ROOT, HEAD)]
    structure = report["structure"]
    assert structure["status"] == "preserved"
    assert structure["base"] == {"commit": BASE, "tree": BASE_TREE}
    assert structure["head"] == {"commit": HEAD, "tree": TREE}
    paths = sorted(candidate.SOURCE_PATHS + candidate.COMPANION_PATHS)
    assert len(paths) == len(structure["paths"]) == 13
    assert [row["path"] for row in structure["paths"]] == paths
    for row in structure["paths"]:
        for label in ("base", "head"):
            assert set(row[label]) == {"mode", "type", "oid"}
            assert row[label]["mode"] == "100644" and row[label]["type"] == "blob"
        assert row["base"]["oid"] != row["head"]["oid"]
    assert report["structure_sha256"] == hashlib.sha256(shadow.encode(structure)).hexdigest()
    assert report["candidate_eligible"]
    assert report["candidate_selected"] == []


@pytest.mark.parametrize("path", candidate.SOURCE_PATHS + candidate.COMPANION_PATHS)
@pytest.mark.parametrize("endpoint", [BASE, HEAD])
@pytest.mark.parametrize("fault", ["missing", "executable", "symlink", "gitlink"])
def test_every_reviewed_file_must_exist_as_regular_blob_at_both_ends(
    collector, baseline, path, endpoint, fault
):
    selection = collector.modes[endpoint]
    if fault == "missing":
        entries = tuple(entry for entry in selection.entries if entry.path != path)
    else:
        mode, object_type = {
            "executable": ("100755", "blob"),
            "symlink": ("120000", "blob"),
            "gitlink": ("160000", "commit"),
        }[fault]
        entries = tuple(
            replace(entry, mode=mode, object_type=object_type) if entry.path == path else entry
            for entry in selection.entries
        )
    collector.modes[endpoint] = replace(selection, entries=entries)
    report = evaluate(baseline)
    assert report["path_eligible"] and not report["candidate_eligible"]
    assert report["baseline_selected"] == report["candidate_selected"] == ["application"]
    assert report["reason"] == "doctor-structure-not-preserved"
    assert report["structure"]["status"] == "not-preserved"
    row = next(item for item in report["structure"]["paths"] if item["path"] == path)
    actual = row["base" if endpoint == BASE else "head"]
    if fault == "missing":
        assert actual is None
    else:
        assert actual["mode"] == mode and actual["type"] == object_type
    assert report["summary"]["candidate_variant_cells"] == 1
    assert report["dependency_review"] == report["coverage_review"] == "required"


def test_unchanged_missing_coverage_companion_also_prevents_eligibility(collector, baseline):
    path = "tests/unit/test_doctor_supervision.py"
    assert path not in collector.collected.changed_paths
    for endpoint in (BASE, HEAD):
        selection = collector.modes[endpoint]
        collector.modes[endpoint] = replace(
            selection, entries=tuple(entry for entry in selection.entries if entry.path != path)
        )
    report = evaluate(baseline)
    assert not report["candidate_eligible"]
    row = next(item for item in report["structure"]["paths"] if item["path"] == path)
    assert row == {"path": path, "base": None, "head": None}


@pytest.mark.parametrize("paths", [(), ("docs/reference/doctor.md",), (SOURCE, "pyproject.toml")])
def test_ineligible_paths_never_import_or_read_committed_structure(collector, baseline, paths):
    collector.collected.changed_paths = paths
    report = evaluate(baseline)
    assert not report["path_eligible"] and not report["candidate_eligible"]
    assert report["structure"] == {
        "status": "not-evaluated",
        "base": None,
        "head": None,
        "paths": [],
    }
    assert report["structure_sha256"] is None
    assert collector.structure_reads == []
    assert collector.imports == ["yaga.test_planning.git"]


@pytest.mark.parametrize(
    "endpoint,field,value",
    [
        (BASE, "commit_sha", HEAD),
        (HEAD, "commit_sha", BASE),
        (HEAD, "tree_sha", BASE_TREE),
        (BASE, "tree_sha", None),
        (BASE, "tree_sha", "HEAD"),
        (BASE, "tree_sha", "A" * 40),
        (BASE, "tree_sha", "a" * 64),
    ],
)
def test_structural_endpoint_identity_failure_is_an_error(
    collector, baseline, endpoint, field, value
):
    collector.modes[endpoint] = replace(collector.modes[endpoint], **{field: value})
    with pytest.raises(shadow.ShadowError):
        evaluate(baseline)
    assert len(collector.structure_reads) == (1 if endpoint == BASE else 2)


@pytest.mark.parametrize("endpoint", [BASE, HEAD])
@pytest.mark.parametrize("result", [None, {}, (), SimpleNamespace(entries=())])
def test_untyped_or_incomplete_reader_results_cannot_substitute_for_validated_models(
    collector, baseline, endpoint, result
):
    collector.modes[endpoint] = result
    with pytest.raises(shadow.ShadowError, match="committed structure"):
        evaluate(baseline)


@pytest.mark.parametrize("endpoint", [BASE, HEAD])
@pytest.mark.parametrize(
    "failure", ["duplicate paths", "incomplete record", "invalid mode/type", "malformed oid"]
)
def test_pinned_reader_validation_failure_is_not_a_structural_fallback(
    collector, baseline, tmp_path, capsys, endpoint, failure
):
    # The installed ModeSelection/ModeEntry validation owns these malformed
    # metadata cases; its failure must propagate instead of yielding a report.
    collector.reader_failures[endpoint] = ValueError(failure)
    argv, output = arguments(tmp_path, baseline)
    assert candidate.main(argv) == 2
    assert not output.exists()
    assert capsys.readouterr().out == (
        "Doctor candidate could not be evaluated; no selection was activated\n"
    )


@pytest.mark.parametrize(
    "field",
    ["base_commit", "head_commit", "comparison_commit", "execution_commit", "execution_tree"],
)
@pytest.mark.parametrize("value", [None, "f" * 40])
def test_every_collected_identity_must_match_independent_expected_source(
    collector, baseline, field, value
):
    setattr(collector.collected.source, field, value)
    with pytest.raises(shadow.ShadowError, match="expected range"):
        evaluate(baseline)
    assert len(collector.calls) == 1


@pytest.mark.parametrize("field", ["base", "head", "expected_tree"])
@pytest.mark.parametrize("value", [None, "HEAD", "--help", "a" * 39, "A" * 40, "a" * 64])
def test_expected_source_requires_full_exact_oids_before_git(collector, baseline, field, value):
    with pytest.raises(shadow.ShadowError):
        evaluate(baseline, **{field: value})
    assert collector.calls == []


@pytest.mark.parametrize("fault", ["source", "paths", "duplicate-paths", "version"])
def test_incomplete_collection_and_wrong_version_fail_closed(
    collector, baseline, monkeypatch, fault
):
    if fault == "source":
        collector.collected.source = None
    elif fault == "paths":
        collector.collected.changed_paths = None
    elif fault == "duplicate-paths":
        collector.collected.changed_paths = (SOURCE, SOURCE)
    else:
        monkeypatch.setattr(candidate.importlib.metadata, "version", lambda _: "0.3.1")
    with pytest.raises(shadow.ShadowError):
        evaluate(baseline)
    assert len(collector.calls) == (0 if fault == "version" else 1)


def test_compare_is_pure_and_does_not_load_yaga_or_run_the_gate(baseline, monkeypatch):
    monkeypatch.setattr(
        candidate.importlib, "import_module", lambda *_: pytest.fail("Unexpected YAGA import")
    )
    monkeypatch.setattr(
        qualification_gate, "main", lambda: pytest.fail("Unexpected qualification dispatch")
    )
    monkeypatch.setattr(
        qualification_gate.subprocess, "run", lambda *_a, **_kw: pytest.fail("Unexpected process")
    )
    assert candidate.compare([SOURCE], baseline)["reason"] == candidate.ELIGIBLE_REASON


def arguments(tmp_path, baseline):
    policy, output = tmp_path / "policy.json", tmp_path / "report.json"
    policy.write_bytes(baseline)
    return [
        "--repo",
        str(ROOT),
        "--baseline-policy",
        str(policy),
        "--base",
        BASE,
        "--head",
        HEAD,
        "--expected-tree",
        TREE,
        "--output",
        str(output),
    ], output


def test_cli_creates_only_report_and_preserves_workflow_outputs(
    collector, baseline, tmp_path, monkeypatch, capsys
):
    argv, output = arguments(tmp_path, baseline)
    outputs = [tmp_path / "github-output", tmp_path / "github-summary"]
    for name, path in zip(("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"), outputs, strict=True):
        path.write_text("preserve")
        monkeypatch.setenv(name, str(path))
    monkeypatch.setattr(shadow, "generate", lambda *_a, **_kw: pytest.fail("Unexpected planning"))
    monkeypatch.setattr(qualification_gate, "main", lambda: pytest.fail("Unexpected gate"))
    assert candidate.main(argv) == 0
    original = output.read_bytes()
    assert json.loads(original)["activation"] == "disabled"
    assert candidate.main(argv) == 2
    assert output.read_bytes() == original
    assert all(path.read_text() == "preserve" for path in outputs)
    assert capsys.readouterr().out == (
        "Doctor candidate could not be evaluated; no selection was activated\n"
    )


def test_cli_failure_has_fixed_diagnostic_and_does_not_create_report(
    collector, baseline, tmp_path, monkeypatch, capsys
):
    argv, output = arguments(tmp_path, baseline)

    def fail(*_a, **_kw):
        raise RuntimeError("sensitive repository contents")

    monkeypatch.setattr(candidate, "evaluate_range", fail)
    assert candidate.main(argv) == 2
    assert not output.exists()
    assert capsys.readouterr().out == (
        "Doctor candidate could not be evaluated; no selection was activated\n"
    )


def test_cli_rejects_oversized_output_before_creating_file(
    collector, baseline, tmp_path, monkeypatch
):
    argv, output = arguments(tmp_path, baseline)
    monkeypatch.setattr(
        candidate, "evaluate_range", lambda *_a, **_kw: {"large": "a" * shadow.MAX_DOCUMENT_BYTES}
    )
    assert candidate.main(argv) == 2
    assert not output.exists()
