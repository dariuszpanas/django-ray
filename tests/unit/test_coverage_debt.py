"""Coverage-debt report, classification, and tracker policy tests."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

import scripts.coverage_debt as coverage_debt_module
from scripts.coverage_debt import (
    ARTIFACT_NAMES,
    MAX_PHASE_LOG_BYTES,
    REPORT_COMMENT_MARKER,
    TRACKER_MARKER,
    CoverageDebtError,
    CoveragePhase,
    _parse_tracker_state,
    build_report,
    coverage_phases,
    load_classifications,
    prepare_output_directory,
    render_markdown,
    run_coverage_phases,
    update_tracker,
)

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "coverage-debt.yml"
CLASSIFICATIONS = ROOT / ".github" / "coverage-debt-classifications.json"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _coverage_file(
    *, statements: int, covered: int, missing: list[int], percent: str
) -> dict[str, object]:
    return {
        "executed_lines": list(range(1, covered + 1)),
        "missing_lines": missing,
        "excluded_lines": [],
        "summary": {
            "covered_lines": covered,
            "num_statements": statements,
            "percent_covered": float(percent),
            "percent_covered_display": percent,
            "missing_lines": len(missing),
            "excluded_lines": 0,
        },
    }


def _report(
    source_commit: str = "a" * 40,
    *,
    statements: int = 100,
    covered: int = 96,
) -> dict[str, Any]:
    missed = statements - covered
    percent = f"{covered / statements * 100:.2f}" if statements else "100.00"
    return {
        "schema_version": 1,
        "metric": "line",
        "source_commit": source_commit,
        "central_configuration": {
            "path": "pyproject.toml",
            "precision": 2,
            "fail_under": 95,
        },
        "totals": {
            "statements": statements,
            "covered_lines": covered,
            "missed_lines": missed,
            "coverage_percent": percent,
        },
        "files": [],
    }


class FakeTrackerApi:
    def __init__(
        self,
        *,
        issues: list[dict[str, Any]] | None = None,
        comments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.issues = issues or [{"number": 122, "body": TRACKER_MARKER}]
        self.comments = comments or []
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def paginate(self, path: str) -> list[dict[str, Any]]:
        if path.endswith("/issues?state=all"):
            return list(self.issues)
        if path.endswith("/issues/122/comments"):
            return list(self.comments)
        raise AssertionError(f"unexpected pagination path: {path}")

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> object:
        self.requests.append((method, path, payload))
        assert payload is not None
        body = payload["body"]
        assert isinstance(body, str)
        if method == "POST":
            assert path.endswith("/issues/122/comments")
            self.comments.append(
                {"id": 501, "body": body, "user": {"login": "github-actions[bot]"}}
            )
            return self.comments[-1]
        if method == "PATCH":
            assert path.endswith("/issues/comments/501")
            self.comments[0]["body"] = body
            return self.comments[0]
        raise AssertionError(f"unexpected request: {method} {path}")


def test_prepare_output_removes_only_owned_stale_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "coverage-debt"
    output.mkdir()
    owned = list(ARTIFACT_NAMES)
    for name in owned:
        (output / name).write_text("stale", encoding="utf-8")
        (output / f".{name}.pending").write_text("partial", encoding="utf-8")
    unrelated = output / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    prepare_output_directory(output)

    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert all(not (output / name).exists() for name in owned)
    assert all(not (output / f".{name}.pending").exists() for name in owned)


def test_coverage_phases_replace_then_append_manifest_owned_local_ray(tmp_path: Path) -> None:
    default_resources, local_ray = coverage_phases(
        tmp_path,
        default_timeout_seconds=1_200,
        local_ray_timeout_seconds=900,
    )

    assert default_resources.selection == "not real_ray and not live_cluster and not postgresql"
    assert default_resources.coverage_mode == "replace"
    assert "--cov=src" in default_resources.command
    assert "--cov-fail-under=0" in default_resources.command
    assert "--maxfail=1" in default_resources.command
    assert "--cov-append" not in default_resources.command
    assert local_ray.coverage_mode == "append"
    assert local_ray.timeout_seconds == 900
    assert "scripts/test_suite_inventory.py" in local_ray.command
    assert local_ray.command[local_ray.command.index("--lane") + 1] == "local-ray"
    assert "--cov-fail-under=0" in local_ray.command
    assert "--maxfail=1" in local_ray.command
    assert "--cov-append" in local_ray.command
    assert "-vv" in local_ray.command
    assert "-q" not in local_ray.command
    assert local_ray.timing_path == tmp_path / "local-ray-timing.json"

    taxonomy = json.loads(
        (ROOT / ".github" / "test-suite-taxonomy.json").read_text(encoding="utf-8")
    )
    lane = next(
        contract for contract in taxonomy["execution_contracts"] if contract["id"] == "local-ray"
    )
    assert lane["skip_policy"]["mode"] == "forbid"
    assert "compiled_graph_opt_in" in lane["selection"]["exclude_markers"]


def test_coverage_phase_stops_after_first_failure_with_exact_diagnostics(
    tmp_path: Path,
) -> None:
    blocked_marker = tmp_path / "second-test-started"
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_module = tests_dir / "test_fail_fast_phase.py"
    test_module.write_text(
        "\n".join(
            (
                "import time",
                "from pathlib import Path",
                "import pytest",
                "",
                "@pytest.mark.real_ray",
                "def test_first_failure():",
                "    assert False, 'intentional coverage phase failure'",
                "",
                "@pytest.mark.real_ray",
                "def test_second_would_block():",
                f"    Path({str(blocked_marker)!r}).write_text('started', encoding='utf-8')",
                "    time.sleep(30)",
                "",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        'pytest_plugins = ("scripts.pytest_taxonomy",)\n',
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    real_ray: owns a local Ray runtime\n",
        encoding="utf-8",
    )
    manifest_dir = tmp_path / ".github"
    manifest_dir.mkdir()
    (manifest_dir / "test-suite-taxonomy.json").write_text(
        (ROOT / ".github" / "test-suite-taxonomy.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(
        "artifacts/\n.pytest_cache/\n__pycache__/\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    timing_path = tmp_path / "artifacts" / "local-ray-timing.json"
    inventory_script = ROOT / "scripts" / "test_suite_inventory.py"
    inventory_bootstrap = (
        "import runpy,sys;"
        f"sys.path.insert(0,{str(ROOT)!r});"
        f"sys.argv=[{str(inventory_script)!r},*sys.argv[1:]];"
        f"runpy.run_path({str(inventory_script)!r},run_name='__main__')"
    )
    phase = CoveragePhase(
        name="local-ray",
        selection="first failure prevents later blocking work",
        coverage_mode="replace",
        timeout_seconds=10,
        command=(
            sys.executable,
            "-c",
            inventory_bootstrap,
            "--manifest",
            ".github/test-suite-taxonomy.json",
            "run",
            "--lane",
            "local-ray",
            "--observation",
            "coverage-debt-monthly",
            "--variant",
            "locked-dependencies",
            "--timing-output",
            timing_path.relative_to(tmp_path).as_posix(),
            "--external-note",
            "focused fail-fast regression",
            "--",
            "--maxfail=1",
            "-vv",
            "--tb=short",
        ),
        log_path=tmp_path / "fail-fast.log",
        timing_path=timing_path,
    )

    started = time.monotonic()
    record = coverage_debt_module.run_coverage_phase(tmp_path, phase)
    elapsed = time.monotonic() - started

    assert record["outcome"] == "failed"
    assert record["exit_code"] == 1
    assert record["timed_out"] is False
    assert elapsed < phase.timeout_seconds / 2
    assert not blocked_marker.exists()
    assert timing_path.is_file()
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    assert timing["schema_version"] == coverage_debt_module.TEST_TIMING_SCHEMA_VERSION
    assert timing["pytest"]["exit_code"] == 1
    assert timing["pytest"]["selected_count"] == 2
    assert timing["pytest"]["completed_count"] == 1
    assert timing["pytest"]["logfinished_count"] == 1
    assert timing["pytest"]["outcomes"]["failed"] == 1
    assert timing["integrity"]["valid"] is False
    assert record["timing_evidence"] is False
    assert record["timing_error"] == (
        "required timing evidence does not prove a complete passing phase"
    )
    retained = phase.log_path.read_text(encoding="utf-8")
    normalized_retained = retained.replace("\\", "/")
    assert "tests/test_fail_fast_phase.py::test_first_failure FAILED" in normalized_retained
    assert "tests/test_fail_fast_phase.py:7: in test_first_failure" in normalized_retained
    assert "intentional coverage phase failure" in normalized_retained
    assert "test_second_would_block" not in normalized_retained


def test_phase_environment_disables_persistent_git_fsmonitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor=true'")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "coverage fixture")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")

    environment = coverage_debt_module._phase_environment()

    assert "GIT_CONFIG_PARAMETERS" not in environment
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
    assert environment["GIT_CONFIG_VALUE_0"] == "false"
    assert "GIT_CONFIG_KEY_1" not in environment
    assert "GIT_CONFIG_VALUE_1" not in environment
    assert coverage_debt_module.os.environ["GIT_CONFIG_COUNT"] == "2"
    assert coverage_debt_module.os.environ["GIT_CONFIG_VALUE_1"] == "true"


def test_phase_environment_disables_ray_uv_worker_replication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "1")

    environment = coverage_debt_module._phase_environment()

    assert environment["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] == "0"
    assert coverage_debt_module.os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] == "1"


def test_coverage_phase_timeout_retains_bounded_incomplete_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "artifacts" / "coverage-debt"
    default_phase = CoveragePhase(
        name="default-resources",
        selection="focused passing fixture",
        coverage_mode="replace",
        timeout_seconds=1,
        command=(sys.executable, "-c", "print('default passed')"),
        log_path=output_dir / "coverage-default-resources.log",
    )
    local_ray_phase = CoveragePhase(
        name="local-ray",
        selection="focused timeout fixture",
        coverage_mode="append",
        timeout_seconds=0.25,
        command=(
            sys.executable,
            "-c",
            (
                "import sys,time; "
                f"sys.stdout.write('x' * {MAX_PHASE_LOG_BYTES + 4_096}); "
                "sys.stdout.write('before-timeout\\n'); sys.stdout.flush(); time.sleep(30)"
            ),
        ),
        log_path=output_dir / "coverage-local-ray.log",
    )
    monkeypatch.setattr(
        coverage_debt_module,
        "coverage_phases",
        lambda *_args, **_kwargs: (default_phase, local_ray_phase),
    )

    started = time.monotonic()
    with pytest.raises(CoverageDebtError, match="local-ray ended with outcome timed-out"):
        run_coverage_phases(
            tmp_path,
            output_dir,
            default_timeout_seconds=1,
            local_ray_timeout_seconds=0.25,
        )

    assert time.monotonic() - started < 10
    diagnostics = json.loads((output_dir / "coverage-phases.json").read_text(encoding="utf-8"))
    assert diagnostics["complete"] is False
    assert [phase["outcome"] for phase in diagnostics["phases"]] == ["passed", "timed-out"]
    record = diagnostics["phases"][1]
    assert record["timed_out"] is True
    output_bytes = record["output_bytes"]
    assert isinstance(output_bytes, int)
    assert output_bytes > MAX_PHASE_LOG_BYTES
    assert record["retained_output_bytes"] <= MAX_PHASE_LOG_BYTES
    assert record["log_truncated"] is True
    retained = (output_dir / "coverage-local-ray.log").read_bytes()
    assert b"before-timeout" in retained
    assert len(retained) <= MAX_PHASE_LOG_BYTES + 2_048
    summary = (output_dir / "coverage-phases.md").read_text(encoding="utf-8")
    assert "Overall phase collection: **incomplete**" in summary
    assert "timed-out" in summary


def test_coverage_phase_terminates_descendant_that_inherits_output_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "descendant-heartbeat.bin"
    descendant = "\n".join(
        (
            "import time",
            "from pathlib import Path",
            f"path = Path({str(heartbeat)!r})",
            "with path.open('ab', buffering=0) as stream:",
            "    while True:",
            "        stream.write(b'x')",
            "        time.sleep(0.02)",
        )
    )
    launcher = "\n".join(
        (
            "import subprocess, sys, time",
            "from pathlib import Path",
            f"heartbeat = Path({str(heartbeat)!r})",
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}])",
            "deadline = time.monotonic() + 2",
            "while not heartbeat.exists() and time.monotonic() < deadline:",
            "    time.sleep(0.01)",
            "if not heartbeat.exists():",
            "    raise SystemExit(4)",
            "print('descendant-ready', flush=True)",
        )
    )
    phase = CoveragePhase(
        name="default-resources",
        selection="successful launcher with inherited-pipe descendant",
        coverage_mode="replace",
        timeout_seconds=5,
        command=(sys.executable, "-c", launcher),
        log_path=tmp_path / "descendant.log",
    )
    monkeypatch.setattr(coverage_debt_module, "PHASE_OUTPUT_DRAIN_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(coverage_debt_module, "PHASE_FORCED_SHUTDOWN_TIMEOUT_SECONDS", 2.0)

    started = time.monotonic()
    record = coverage_debt_module.run_coverage_phase(tmp_path, phase)

    assert time.monotonic() - started < 4
    assert record["outcome"] == "cleanup-error"
    assert record["exit_code"] == 0
    assert record["post_exit_descendants_terminated"] is True
    assert record["cleanup_error"] == "owned coverage phase descendants outlived the launcher"
    assert record["termination_error"] is None
    assert record["capture_error"] is None
    assert heartbeat.is_file()
    heartbeat_size = heartbeat.stat().st_size
    time.sleep(0.15)
    assert heartbeat.stat().st_size == heartbeat_size
    assert not any(
        thread.name == "coverage-debt-default-resources-output" and thread.is_alive()
        for thread in coverage_debt_module.threading.enumerate()
    )
    phase_log = phase.log_path.read_text(encoding="utf-8")
    assert "outcome: cleanup-error" in phase_log
    assert "post_exit_descendants_terminated: true" in phase_log


def test_coverage_phase_reader_start_failure_terminates_owned_process_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phase = CoveragePhase(
        name="default-resources",
        selection="output reader start failure",
        coverage_mode="replace",
        timeout_seconds=30,
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        log_path=tmp_path / "reader-start-failure.log",
    )
    launched: list[coverage_debt_module._OwnedPhaseProcess] = []
    original_launch = coverage_debt_module._launch_phase

    def capture_launch(root: Path, selected: CoveragePhase):
        owned = original_launch(root, selected)
        launched.append(owned)
        return owned

    def fail_start(_thread: object) -> None:
        raise RuntimeError("thread capacity exhausted")

    monkeypatch.setattr(coverage_debt_module, "_launch_phase", capture_launch)
    monkeypatch.setattr(coverage_debt_module.threading.Thread, "start", fail_start)

    started = time.monotonic()
    record = coverage_debt_module.run_coverage_phase(tmp_path, phase)

    assert time.monotonic() - started < 10
    assert record["outcome"] == "launch-error"
    assert record["launch_error"] == (
        "coverage phase output reader failed to start: thread capacity exhausted"
    )
    assert record["exit_code"] is not None
    assert len(launched) == 1
    assert launched[0].process.poll() is not None
    assert launched[0].windows_job_handle is None
    assert not any(
        thread.name == "coverage-debt-default-resources-output" and thread.is_alive()
        for thread in coverage_debt_module.threading.enumerate()
    )


def test_coverage_phase_requires_fresh_valid_timing_evidence(tmp_path: Path) -> None:
    timing_path = tmp_path / "local-ray-timing.json"
    timing_value = {
        "schema_version": coverage_debt_module.TEST_TIMING_SCHEMA_VERSION,
        "lane": "local-ray",
        "observation": "coverage-debt-monthly",
        "variant": "locked-dependencies",
        "source": {
            "algorithm": "sha256",
            "digest": "a" * 64,
            "file_count": 1,
        },
        "source_after_digest": "a" * 64,
        "selection": "marker:real_ray AND NOT marker:compiled_graph_opt_in",
        "skip_policy": {"mode": "forbid", "reason": "all selected tests must execute"},
        "collection": {
            "mode": "serial",
            "selected_count": 1,
            "valid": True,
            "errors": [],
        },
        "integrity": {"valid": True, "errors": []},
        "pytest": {
            "exit_code": 0,
            "selected_count": 1,
            "completed_count": 1,
            "logfinished_count": 1,
            "coverage_enabled": True,
            "outcomes": {
                "failed": 0,
                "passed": 1,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
            },
        },
        "test_outcomes": [{"nodeid": "tests/test_example.py::test_passes", "outcome": "passed"}],
        "skipped_tests": [],
        "pytest_arguments": [
            "--cov=src",
            "--cov-config=pyproject.toml",
            "--cov-report=",
            "--cov-fail-under=0",
            "--cov-append",
            "--maxfail=1",
        ],
    }
    timing_path.write_text(json.dumps(timing_value), encoding="utf-8")
    missing = CoveragePhase(
        name="local-ray",
        selection="focused passing fixture",
        coverage_mode="append",
        timeout_seconds=5,
        command=(sys.executable, "-c", "print('passed without timing')"),
        log_path=tmp_path / "missing-timing.log",
        timing_path=timing_path,
    )

    missing_record = coverage_debt_module.run_coverage_phase(tmp_path, missing)

    assert missing_record["outcome"] == "invalid-timing-evidence"
    assert missing_record["timing_evidence"] is False
    assert missing_record["timing_error"] == "required timing evidence was not created"
    assert not timing_path.exists()

    payload = json.dumps(timing_value)
    passing = CoveragePhase(
        name="local-ray",
        selection="focused passing fixture",
        coverage_mode="append",
        timeout_seconds=5,
        command=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                f"Path({str(timing_path)!r}).write_text({payload!r}, encoding='utf-8')"
            ),
        ),
        log_path=tmp_path / "valid-timing.log",
        timing_path=timing_path,
    )

    passing_record = coverage_debt_module.run_coverage_phase(tmp_path, passing)

    assert passing_record["outcome"] == "passed"
    assert passing_record["timing_evidence"] is True
    assert passing_record["timing_error"] is None

    inconsistent = dict(timing_value)
    inconsistent["source_after_digest"] = "b" * 64
    timing_path.write_text(json.dumps(inconsistent), encoding="utf-8")
    assert (
        coverage_debt_module._timing_evidence_error(timing_path, expected_lane="local-ray")
        == "required timing evidence does not preserve its source fence"
    )


def test_timing_evidence_rejects_non_regular_file_without_blocking(tmp_path: Path) -> None:
    timing_directory = tmp_path / "local-ray-timing.json"
    timing_directory.mkdir()

    started = time.monotonic()
    error = coverage_debt_module._timing_evidence_error(
        timing_directory,
        expected_lane="local-ray",
    )

    assert time.monotonic() - started < 1
    assert error == "required timing evidence is not a regular file"


def test_report_records_exact_sorted_ranges_and_review_classifications(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage.json"
    classifications_path = tmp_path / "classifications.json"
    pyproject_path = tmp_path / "pyproject.toml"
    _write_json(
        coverage_path,
        {
            "meta": {"branch_coverage": False},
            "files": {
                "src\\django_ray\\small.py": _coverage_file(
                    statements=5, covered=4, missing=[9], percent="80.00"
                ),
                "src/django_ray/larger.py": _coverage_file(
                    statements=8, covered=4, missing=[2, 3, 4, 8], percent="50.00"
                ),
            },
            "totals": {
                "covered_lines": 8,
                "num_statements": 13,
                "percent_covered_display": "61.54",
                "missing_lines": 5,
            },
        },
    )
    _write_json(
        classifications_path,
        {
            "schema_version": 1,
            "files": {
                "src/django_ray/larger.py": {
                    "default": {
                        "category": "testable-behavior",
                        "rationale": "Assert the deterministic contract.",
                    },
                    "overrides": [
                        {
                            "ranges": ["3-4"],
                            "category": "environment-specific",
                            "rationale": "Exercise the matching runtime.",
                        }
                    ],
                },
                "src/django_ray/small.py": {
                    "default": {
                        "category": "defensive-invariant",
                        "rationale": "Prove or remove the guard.",
                    }
                },
            },
        },
    )
    pyproject_path.write_text(
        "[tool.coverage.report]\nfail_under = 95\nprecision = 2\n", encoding="utf-8"
    )

    report = build_report(
        coverage_path,
        classifications_path,
        pyproject_path,
        "a" * 40,
    )

    assert report["metric"] == "line"
    assert report["totals"] == {
        "statements": 13,
        "covered_lines": 8,
        "missed_lines": 5,
        "coverage_percent": "61.54",
    }
    assert [file_record["path"] for file_record in report["files"]] == [
        "src/django_ray/larger.py",
        "src/django_ray/small.py",
    ]
    assert [
        (
            uncovered["display"],
            uncovered["classification"]["category"],
        )
        for uncovered in report["files"][0]["uncovered_ranges"]
    ] == [
        ("2", "testable-behavior"),
        ("3-4", "environment-specific"),
        ("8", "testable-behavior"),
    ]
    markdown = render_markdown(report)
    assert "line coverage" in markdown
    assert "Branch coverage is a separate follow-up" in markdown
    assert markdown.index("larger.py") < markdown.index("small.py")


def test_repository_manifest_classifies_workflow_progress_rss_fallback() -> None:
    classifications = load_classifications(CLASSIFICATIONS)
    policy = classifications[
        "src/django_ray/management/commands/django_ray_benchmark_workflow_progress.py"
    ]

    assert policy.default.category == "testable-behavior"
    assert {policy.for_line(line).category for line in (135, 136)} == {"environment-specific"}


@pytest.mark.parametrize(
    ("branch_coverage", "classify_small", "message"),
    [
        (True, True, "line coverage only"),
        (False, False, "has no review-policy classification"),
    ],
)
def test_report_fails_closed_for_wrong_metric_or_unclassified_debt(
    tmp_path: Path,
    branch_coverage: bool,
    classify_small: bool,
    message: str,
) -> None:
    coverage_path = tmp_path / "coverage.json"
    classifications_path = tmp_path / "classifications.json"
    pyproject_path = tmp_path / "pyproject.toml"
    _write_json(
        coverage_path,
        {
            "meta": {"branch_coverage": branch_coverage},
            "files": {
                "src/django_ray/small.py": _coverage_file(
                    statements=1, covered=0, missing=[1], percent="0.00"
                )
            },
            "totals": {
                "covered_lines": 0,
                "num_statements": 1,
                "percent_covered_display": "0.00",
                "missing_lines": 1,
            },
        },
    )
    files = (
        {
            "src/django_ray/small.py": {
                "default": {
                    "category": "testable-behavior",
                    "rationale": "Assert the contract.",
                }
            }
        }
        if classify_small
        else {}
    )
    _write_json(classifications_path, {"schema_version": 1, "files": files})
    pyproject_path.write_text(
        "[tool.coverage.report]\nfail_under = 95\nprecision = 2\n", encoding="utf-8"
    )

    with pytest.raises(CoverageDebtError, match=message):
        build_report(coverage_path, classifications_path, pyproject_path, "a" * 40)


def test_identical_runs_reuse_one_bot_comment_and_seed_all_measurements() -> None:
    api = FakeTrackerApi()
    report = _report()

    assert update_tracker(api, "dariuszpanas/django-ray", report) == "created"
    first_body = api.comments[0]["body"]
    assert update_tracker(api, "dariuszpanas/django-ray", report) == "updated"

    assert len(api.comments) == 1
    assert [request[0] for request in api.requests] == ["POST", "PATCH"]
    assert api.comments[0]["body"] == first_body
    state = _parse_tracker_state(api.comments[0]["body"])
    assert state["current"] == state["previous"] == state["best"]
    assert api.comments[0]["body"].count(REPORT_COMMENT_MARKER) == 1


def test_tracker_moves_current_to_previous_and_retains_exact_high_water() -> None:
    api = FakeTrackerApi()
    first = _report("a" * 40, covered=97)
    lower = _report("b" * 40, covered=96)

    update_tracker(api, "dariuszpanas/django-ray", first)
    update_tracker(api, "dariuszpanas/django-ray", lower)

    state = _parse_tracker_state(api.comments[0]["body"])
    assert state["current"].source_commit == "b" * 40
    assert state["previous"].source_commit == "a" * 40
    assert state["best"].source_commit == "a" * 40
    assert "High water" in api.comments[0]["body"]


def test_tracker_refuses_duplicate_issue_markers_before_any_write() -> None:
    api = FakeTrackerApi(
        issues=[
            {"number": 122, "body": TRACKER_MARKER},
            {"number": 123, "body": TRACKER_MARKER},
        ]
    )

    with pytest.raises(CoverageDebtError, match="exactly one coverage-debt tracker marker"):
        update_tracker(api, "dariuszpanas/django-ray", _report())

    assert api.requests == []


def test_tracker_refuses_duplicate_or_non_bot_report_comments() -> None:
    duplicate_api = FakeTrackerApi(
        comments=[
            {"id": 501, "body": REPORT_COMMENT_MARKER, "user": {"login": "github-actions[bot]"}},
            {"id": 502, "body": REPORT_COMMENT_MARKER, "user": {"login": "github-actions[bot]"}},
        ]
    )
    with pytest.raises(CoverageDebtError, match="multiple coverage-debt latest-report markers"):
        update_tracker(duplicate_api, "dariuszpanas/django-ray", _report())
    assert duplicate_api.requests == []

    human_api = FakeTrackerApi(
        comments=[{"id": 501, "body": REPORT_COMMENT_MARKER, "user": {"login": "maintainer"}}]
    )
    with pytest.raises(CoverageDebtError, match="not owned by the expected bot"):
        update_tracker(human_api, "dariuszpanas/django-ray", _report())
    assert human_api.requests == []


def test_monthly_workflow_and_make_target_preserve_coverage_policy() -> None:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    events = workflow["on"]
    cron = events["schedule"][0]["cron"]
    permissions = workflow["permissions"]
    job = workflow["jobs"]["report"]
    steps = job["steps"]

    assert cron.split()[2] == "1"
    assert "workflow_dispatch" in events
    assert permissions == {"contents": "read", "issues": "write"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "45"
    assert job["env"]["COVERAGE_DEBT_SOURCE_COMMIT"] == "${{ github.sha }}"
    assert any(step.get("run") == "uv python install 3.12" for step in steps)
    assert any("make coverage-debt" in step.get("run", "") for step in steps)
    summary = next(step for step in steps if step.get("name") == "Add coverage-debt job summary")
    assert summary["if"] == "always()"
    assert "coverage-phases.md" in summary["run"]
    assert "coverage-debt.md" in summary["run"]
    assert "GITHUB_STEP_SUMMARY" in summary["run"]
    upload = next(step for step in steps if step.get("name") == "Upload coverage-debt evidence")
    assert upload["if"] == "always()"
    for artifact in (
        "coverage-debt.json",
        "coverage-debt.md",
        "coverage-phases.json",
        "coverage-phases.md",
        "coverage-default-resources.log",
        "coverage-local-ray.log",
        "local-ray-timing.json",
    ):
        assert artifact in upload["with"]["path"]
    update = next(
        step for step in steps if step.get("name") == "Update the marked coverage-debt tracker"
    )
    assert update["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("coverage-debt:\n", maxsplit=1)[1].split(
        "\n# Run formatting", maxsplit=1
    )[0]
    assert "coverage_debt.py run-phases" in target
    assert "--default-timeout-seconds $(COVERAGE_DEBT_DEFAULT_TIMEOUT_SECONDS)" in target
    assert "--local-ray-timeout-seconds $(COVERAGE_DEBT_LOCAL_RAY_TIMEOUT_SECONDS)" in target
    assert "COVERAGE_DEBT_DEFAULT_TIMEOUT_SECONDS ?= 1200" in makefile
    assert "COVERAGE_DEBT_LOCAL_RAY_TIMEOUT_SECONDS ?= 900" in makefile
    assert "coverage report --fail-under=$(COVERAGE_GLOBAL_MIN)" in target
    assert "--fail-under=$(COVERAGE_WORKER_MIN)" in target
    assert "--fail-under=$(COVERAGE_RAY_JOB_MIN)" in target
    assert "COVERAGE_GLOBAL_MIN ?= 95" in makefile
    assert "COVERAGE_WORKER_MIN ?= 90" in makefile
    assert "COVERAGE_RAY_JOB_MIN ?= 90" in makefile
    assert "COVERAGE_TESTPROJECT_MIN ?= 80" in makefile

    docs = (ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
    assert "line coverage only" in docs
    assert "separate follow-up" in docs
    assert all(
        label in docs
        for label in (
            "Testable behavior",
            "Environment-specific",
            "Upstream/native constraint",
            "Defensive invariant",
            "Dead or non-behavioral code",
        )
    )
