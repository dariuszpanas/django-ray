from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parents[2]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
COMMIT_WORKFLOW = WORKFLOWS / "commit-messages.yml"
XDIST_RETENTION_WORKFLOW = WORKFLOWS / "pytest-xdist-retention.yml"
REAL_RAY_WORKFLOW = WORKFLOWS / "real-ray-compatibility.yml"
CONTRIBUTING = PROJECT_ROOT / "CONTRIBUTING.md"
CONTRIBUTING_DOCS = PROJECT_ROOT / "docs" / "contributing.md"
TAXONOMY_DOCS = PROJECT_ROOT / "docs" / "testing" / "test-suite-taxonomy.md"

CI_ROOT_JOBS = {
    "canonical-project": "Canonical Project",
    "compatibility": "Compatibility",
    "postgresql-coordination": "PostgreSQL Coordination & Polling",
    "live-cluster": "Live Cluster Fault Tests",
}
REQUIRED_CHECK_JOBS = {
    ("ci.yml", "ci-gate"): "CI Gate",
    ("commit-messages.yml", "conventional-commits"): "Commit Messages",
}
REQUIRED_CHECK_NAMES = set(REQUIRED_CHECK_JOBS.values())


def _workflow_paths() -> list[Path]:
    return sorted(path for path in WORKFLOWS.iterdir() if path.suffix in {".yml", ".yaml"})


def _workflow(path: Path = CI_WORKFLOW) -> dict[str, Any]:
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict), path
    return loaded


def _jobs(path: Path = CI_WORKFLOW) -> dict[str, dict[str, Any]]:
    jobs = _workflow(path).get("jobs")
    assert isinstance(jobs, dict), path
    assert all(isinstance(job_id, str) and isinstance(job, dict) for job_id, job in jobs.items())
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return cast(list[dict[str, Any]], steps)


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in _steps(job) if step.get("name") == name)


def _events(path: Path) -> set[str]:
    events = _workflow(path).get("on")
    if isinstance(events, str):
        return {events}
    if isinstance(events, list):
        return {event for event in events if isinstance(event, str)}
    if isinstance(events, dict):
        return {event for event in events if isinstance(event, str)}
    return set()


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return {needs}
    assert isinstance(needs, list)
    assert all(isinstance(job_id, str) for job_id in needs)
    return set(cast(list[str], needs))


def _gate_job() -> dict[str, Any]:
    return _jobs()["ci-gate"]


def _gate_script() -> str:
    gate_step = _step(_gate_job(), "Require every blocking CI job to succeed")
    run = gate_step.get("run")
    assert isinstance(run, str)
    script = run.split("python3 - <<'PY'\n", maxsplit=1)[1].split("\nPY", maxsplit=1)[0]
    return textwrap.dedent(script)


def _execute_gate(monkeypatch: pytest.MonkeyPatch, results: dict[str, str]) -> None:
    payload = {job_id: {"result": result} for job_id, result in results.items()}
    monkeypatch.setenv("BLOCKING_JOB_RESULTS_JSON", json.dumps(payload))
    exec(compile(_gate_script(), "<ci-gate>", "exec"))


def _all_successful_results() -> dict[str, str]:
    return dict.fromkeys(CI_ROOT_JOBS, "success")


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False


def _upload_steps(path: Path) -> list[dict[str, Any]]:
    return [
        step
        for job in _jobs(path).values()
        for step in _steps(job)
        if isinstance(step.get("uses"), str)
        and cast(str, step["uses"]).startswith("actions/upload-artifact@")
    ]


def test_ci_has_exactly_four_root_jobs_and_one_gate_without_a_matrix() -> None:
    workflow = _workflow()
    jobs = _jobs()

    assert set(jobs) == set(CI_ROOT_JOBS) | {"ci-gate"}
    assert not _contains_key(workflow, "matrix")
    for job_id, check_name in CI_ROOT_JOBS.items():
        assert jobs[job_id]["name"] == check_name
        assert not _needs(jobs[job_id])

    gate = jobs["ci-gate"]
    assert gate["name"] == "CI Gate"
    assert gate["if"] == "always()"
    assert _needs(gate) == set(CI_ROOT_JOBS)
    gate_step = _step(gate, "Require every blocking CI job to succeed")
    assert gate_step["env"]["BLOCKING_JOB_RESULTS_JSON"] == "${{ toJSON(needs) }}"


def test_ci_cancels_only_superseded_pull_request_runs() -> None:
    concurrency = _workflow()["concurrency"]
    commit_concurrency = _workflow(COMMIT_WORKFLOW)["concurrency"]

    assert concurrency == {
        "group": "ci-${{ github.event.pull_request.number || github.run_id }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    assert commit_concurrency == {
        "group": "commit-messages-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }


def test_required_check_names_are_globally_unique() -> None:
    for (workflow_name, job_id), check_name in REQUIRED_CHECK_JOBS.items():
        required_job = _jobs(WORKFLOWS / workflow_name)[job_id]
        assert required_job.get("name") == check_name

    check_names = [
        job.get("name")
        for path in _workflow_paths()
        for job in _jobs(path).values()
        if isinstance(job.get("name"), str)
    ]
    for required_name in REQUIRED_CHECK_NAMES:
        assert check_names.count(required_name) == 1


def test_blocking_workflows_never_tolerate_job_or_step_failures() -> None:
    assert not _contains_key(_workflow(CI_WORKFLOW), "continue-on-error")
    assert not _contains_key(_workflow(COMMIT_WORKFLOW), "continue-on-error")


def test_ci_gate_accepts_only_complete_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = _all_successful_results()

    _execute_gate(monkeypatch, results)

    output = capsys.readouterr().out
    assert set(output.splitlines()) == {f"{job_id}: success" for job_id in results}


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "timed_out"])
def test_ci_gate_rejects_every_non_success_result(
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    results = _all_successful_results()
    results["canonical-project"] = result

    with pytest.raises(SystemExit, match=rf"CI Gate blocked: canonical-project={result}"):
        _execute_gate(monkeypatch, results)


def test_ci_gate_fails_closed_without_dependency_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="CI Gate blocked: no blocking job results"):
        _execute_gate(monkeypatch, {})


def test_ci_gate_rejects_partial_or_unexpected_result_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = _all_successful_results()
    partial.pop("canonical-project")
    with pytest.raises(
        SystemExit,
        match="missing=canonical-project; unexpected=-",
    ):
        _execute_gate(monkeypatch, partial)

    unexpected = _all_successful_results() | {"unreviewed-job": "success"}
    with pytest.raises(
        SystemExit,
        match="missing=-; unexpected=unreviewed-job",
    ):
        _execute_gate(monkeypatch, unexpected)


def test_canonical_project_is_serial_source_fenced_and_complete() -> None:
    job = _jobs()["canonical-project"]
    coverage = _step(job, "Run serial source-fenced canonical coverage")
    script = cast(str, coverage["run"])

    assert _step(job, "Set up Python")["run"] == "uv python install 3.12.12"
    assert _step(job, "Install locked dependencies")["run"] == "uv sync --frozen"
    assert all("--python 3.12" not in cast(str, step.get("run", "")) for step in _steps(job))
    assert coverage["if"] == "${{ !cancelled() }}"
    assert "make test-cov-phased" in script
    assert "TEST_SUITE_HERMETIC_EXECUTION=serial" in script
    assert "TEST_SUITE_PHASED_OUTPUT_DIR=artifacts/canonical-project" in script
    assert job["env"] == {
        "COVERAGE_GLOBAL_MIN": "95",
        "COVERAGE_WORKER_MIN": "90",
        "COVERAGE_RAY_JOB_MIN": "90",
        "COVERAGE_TESTPROJECT_MIN": "80",
        "DJANGO_RAY_REQUIRE_KUSTOMIZE_PROBE_TESTS": "1",
    }
    expected_work = {
        "Check formatting",
        "Check linting",
        "Type check",
        "Run serial source-fenced canonical coverage",
        "Check bundled testproject",
        "Build strict documentation",
        "Build package",
        "Validate canonical evidence completeness",
        "Upload canonical diagnostics",
    }
    for name in expected_work:
        assert "!cancelled()" in cast(str, _step(job, name)["if"])

    codecov = _step(job, "Upload coverage to Codecov")
    assert codecov["with"]["files"] == "artifacts/canonical-project/coverage.xml"
    assert codecov["with"]["fail_ci_if_error"] == "false"
    assert (
        sum("codecov/codecov-action@" in cast(str, step.get("uses", "")) for step in _steps(job))
        == 1
    )

    diagnostic = _step(job, "Upload canonical diagnostics")
    assert diagnostic["with"]["name"] == (
        "canonical-project-evidence-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    distribution = _step(job, "Upload distribution artifact")
    assert distribution["with"]["name"] == "dist-${{ github.run_id }}-${{ github.run_attempt }}"


def test_compatibility_runs_four_fresh_variants_sequentially_and_fails_closed() -> None:
    job = _jobs()["compatibility"]
    run_step = _step(job, "Run four fresh compatibility variants")
    script = cast(str, run_step["run"])
    variants = (
        ("locked-py313", "3.13", "django-ray-compat-locked-py313"),
        ("locked-py314", "3.14", "django-ray-compat-locked-py314"),
        ("minimum-py312", "3.12", "django-ray-compat-minimum-py312"),
        ("latest-py314", "3.14", "django-ray-compat-latest-py314"),
    )

    assert run_step["if"] == "${{ !cancelled() }}"
    assert "--lane dependency-compatibility" in script
    assert "--lane ray-compat-smoke" in script
    assert 'local active_path="${env_dir}/bin:${PATH}"' in script
    assert script.count('env PATH="$active_path"') >= 2
    assert "isolation_failure=86" in script
    assert '"psutil==6.0.0"' in script
    assert 'if [[ -e "$env_dir" || -L "$env_dir" ]]' in script
    assert 'if [[ "$cleanup_status" != "0" ]]' in script
    assert "overall_status=1" in script
    assert 'exit "$overall_status"' in script
    assert "artifacts/compatibility/${slug}" in script
    setup_failure = script.index('echo "Locked environment installation failed for $slug" >&2')
    setup_failure_block = script[setup_failure : setup_failure + 180]
    assert "return 1" in setup_failure_block
    assert 'return "$isolation_failure"' not in setup_failure_block
    baseline_failure = script.index('echo "Ray baseline failed for $slug" >&2')
    baseline_failure_block = script[baseline_failure : baseline_failure + 150]
    assert 'return "$isolation_failure"' in baseline_failure_block
    assert "local source_state" in script
    assert 'if ! source_state="$(git status --porcelain=v1 --untracked-files=all)"; then' in script
    assert 'if [[ -n "$(git status' not in script
    assert "Compatibility source-state inspection failed" in script
    assert "printf '%s\\n' \"$source_state\"" in script
    positions = []
    for slug, python_version, env_dir in variants:
        invocation = f"{slug} {python_version}"
        assert invocation in script
        assert env_dir in script
        positions.append(script.index(invocation))
    assert positions == sorted(positions)
    assert len(positions) == len(set(positions))

    validation = _step(job, "Validate compatibility evidence completeness")
    upload = _step(job, "Upload compatibility evidence")
    assert validation["if"] == "${{ !cancelled() }}"
    assert upload["if"] == "${{ !cancelled() }}"
    assert upload["with"]["name"] == (
        "compatibility-evidence-${{ github.run_id }}-${{ github.run_attempt }}"
    )


def test_external_resource_roots_remain_single_serial_jobs() -> None:
    jobs = _jobs()
    postgresql = jobs["postgresql-coordination"]
    live_cluster = jobs["live-cluster"]

    assert "strategy" not in postgresql
    assert "strategy" not in live_cluster
    assert "concurrency" not in live_cluster
    assert _step(live_cluster, "Set up Python")["run"] == "uv python install 3.12.12"
    assert _step(live_cluster, "Install dependencies")["run"] == "uv sync --frozen"
    assert _step(live_cluster, "Run live cluster fault tests")["run"] == (
        "uv run --no-sync pytest tests/integration/test_live_failure_injection.py "
        "-m live_cluster -v"
    )
    assert "make test-postgres" in cast(
        str,
        _step(postgresql, "Run PostgreSQL coordination and polling tests")["run"],
    )
    assert "-m live_cluster" in cast(
        str,
        _step(live_cluster, "Run live cluster fault tests")["run"],
    )


def test_all_compact_gate_artifacts_are_retained_for_fourteen_days() -> None:
    uploads = _upload_steps(CI_WORKFLOW)

    assert uploads
    assert all(step["with"]["retention-days"] == "14" for step in uploads)


def test_xdist_retention_is_a_manual_only_workflow() -> None:
    workflow = _workflow(XDIST_RETENTION_WORKFLOW)
    jobs = _jobs(XDIST_RETENTION_WORKFLOW)

    assert workflow["name"] == "pytest-xdist Retention Evidence"
    assert _events(XDIST_RETENTION_WORKFLOW) == {"workflow_dispatch"}
    assert set(jobs) == {
        "pytest-xdist-benchmark-pair",
        "pytest-xdist-benchmark-aggregate",
    }
    assert not _contains_key(workflow, "matrix")
    assert not set(jobs) & set(_jobs())
    for job in jobs.values():
        assert _step(job, "Set up Python")["run"] == "uv python install 3.12.12"
        assert _step(job, "Install locked dependencies")["run"] == "uv sync --frozen"
        assert all("--python 3.12" not in cast(str, step.get("run", "")) for step in _steps(job))
    assert all(
        step["with"]["retention-days"] == "14" for step in _upload_steps(XDIST_RETENTION_WORKFLOW)
    )


def test_real_ray_compatibility_is_weekly_manual_and_sequential() -> None:
    workflow = _workflow(REAL_RAY_WORKFLOW)
    jobs = _jobs(REAL_RAY_WORKFLOW)

    assert workflow["name"] == "Real-Ray Compatibility"
    assert _events(REAL_RAY_WORKFLOW) == {"schedule", "workflow_dispatch"}
    assert set(jobs) == {"real-ray-compatibility"}
    assert not _contains_key(workflow, "matrix")
    job = jobs["real-ray-compatibility"]
    assert job["name"] == "Sequential real-Ray compatibility"
    run_step = _step(job, "Run complete local-Ray contract sequentially")
    script = cast(str, run_step["run"])
    versions = (
        ("3.12", "python-3.12", "django-ray-real-py312", "/tmp/drr312"),
        ("3.13", "python-3.13", "django-ray-real-py313", "/tmp/drr313"),
        ("3.14", "python-3.14", "django-ray-real-py314", "/tmp/drr314"),
    )

    assert run_step["if"] == "${{ !cancelled() }}"
    assert "--lane local-ray" in script
    assert "scripts/ray_residue.py guard" in script
    assert 'local active_path="${env_dir}/bin:${PATH}"' in script
    assert 'env PATH="$active_path"' in script
    assert "isolation_failure=86" in script
    assert 'if [[ -e "$env_dir" || -L "$env_dir" ]]' in script
    assert 'if [[ "$cleanup_status" != "0" ]]' in script
    assert "overall_status=1" in script
    assert 'exit "$overall_status"' in script
    assert "artifacts/real-ray-compatibility/${slug}" in script
    setup_failure = script.index(
        'echo "Real-Ray environment installation failed for Python $python_version" >&2'
    )
    setup_failure_block = script[setup_failure : setup_failure + 210]
    assert "return 1" in setup_failure_block
    assert 'return "$isolation_failure"' not in setup_failure_block
    baseline_failure = script.index(
        'echo "Real-Ray baseline failed for Python $python_version" >&2'
    )
    baseline_failure_block = script[baseline_failure : baseline_failure + 190]
    assert 'return "$isolation_failure"' in baseline_failure_block
    assert "local source_state" in script
    assert 'if ! source_state="$(git status --porcelain=v1 --untracked-files=all)"; then' in script
    assert 'if [[ -n "$(git status' not in script
    assert "Real-Ray source-state inspection failed" in script
    assert "printf '%s\\n' \"$source_state\"" in script
    positions = []
    for python_version, slug, env_dir, ray_alias in versions:
        invocation = f"{python_version} {slug}"
        assert invocation in script
        assert env_dir in script
        assert ray_alias in script
        positions.append(script.index(invocation))
    assert positions == sorted(positions)
    assert len(positions) == len(set(positions))

    validation = _step(job, "Validate real-Ray evidence completeness")
    assert validation["if"] == "${{ !cancelled() }}"
    upload = _step(job, "Upload real-Ray compatibility evidence")
    assert upload["if"] == "${{ !cancelled() }}"
    assert upload["with"]["name"] == (
        "real-ray-compatibility-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert upload["with"]["retention-days"] == "14"


def test_compact_gate_and_nonblocking_evidence_workflows_are_documented() -> None:
    documentation = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (CONTRIBUTING, CONTRIBUTING_DOCS, TAXONOMY_DOCS)
    )

    assert "`CI Gate`" in documentation
    assert "`Commit Messages`" in documentation
    for check_name in CI_ROOT_JOBS.values():
        assert f"`{check_name}`" in documentation
    assert "pytest-xdist Retention Evidence" in documentation
    assert "weekly/manual real-Ray compatibility" in documentation
    assert "no job matrix" in documentation
