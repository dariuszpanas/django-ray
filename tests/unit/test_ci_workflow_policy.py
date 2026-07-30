from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parents[2]
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
MAKEFILE = PROJECT_ROOT / "Makefile"
CONTRIBUTING = PROJECT_ROOT / "CONTRIBUTING.md"
CONTRIBUTING_DOCS = PROJECT_ROOT / "docs" / "contributing.md"
REQUIRED_CHECK_JOBS = {
    ("ci.yml", "ci-gate"): "CI Gate",
    ("commit-messages.yml", "conventional-commits"): "Commit Messages",
}
REQUIRED_CHECK_NAMES = set(REQUIRED_CHECK_JOBS.values())
EXPLICIT_NONBLOCKING_PR_JOBS: dict[tuple[str, str], str] = {}
EXPLICIT_WORKFLOW_DISPATCH_ONLY_JOBS = {
    ("ci.yml", "pytest-xdist-benchmark-pair"): "pair",
    ("ci.yml", "pytest-xdist-benchmark-aggregate"): "aggregate",
}


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


def _events(path: Path) -> set[str]:
    events = _workflow(path).get("on")
    if isinstance(events, str):
        return {events}
    if isinstance(events, list):
        return {event for event in events if isinstance(event, str)}
    if isinstance(events, dict):
        return {event for event in events if isinstance(event, str)}
    return set()


@pytest.mark.parametrize(
    ("workflow_name", "job_id"),
    [
        ("ci.yml", "docs"),
        ("docs.yml", "build"),
        ("release.yml", "build"),
    ],
)
def test_changelog_tag_validation_jobs_fetch_complete_tag_inventory(
    workflow_name: str,
    job_id: str,
) -> None:
    job = _jobs(WORKFLOWS / workflow_name)[job_id]
    steps = job["steps"]
    assert isinstance(steps, list)
    checkout = next(
        step
        for step in steps
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout@")
    )

    assert checkout["with"]["fetch-depth"] == "0"
    commands = "\n".join(
        str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step
    )
    assert "scripts/validate_release.py" in commands
    assert "--development" in commands
    assert "--require-git-tags" in commands
    assert "--allow-release-candidate" in commands


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
    steps = _gate_job().get("steps")
    assert isinstance(steps, list)
    gate_step = next(
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Require every blocking CI job to succeed"
    )
    run = gate_step.get("run")
    assert isinstance(run, str)
    script = run.split("python3 - <<'PY'\n", maxsplit=1)[1].split("\nPY", maxsplit=1)[0]
    return textwrap.dedent(script)


def _execute_gate(monkeypatch: pytest.MonkeyPatch, results: dict[str, str]) -> None:
    payload = {job_id: {"result": result} for job_id, result in results.items()}
    monkeypatch.setenv("BLOCKING_JOB_RESULTS_JSON", json.dumps(payload))
    exec(compile(_gate_script(), "<ci-gate>", "exec"))


def _all_successful_results() -> dict[str, str]:
    return dict.fromkeys(_needs(_gate_job()), "success")


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False


def test_ci_gate_covers_every_pr_ci_job_and_runs_after_failures() -> None:
    jobs = _jobs()
    gate = jobs["ci-gate"]
    dispatch_only = {
        job_id
        for workflow_name, job_id in EXPLICIT_WORKFLOW_DISPATCH_ONLY_JOBS
        if workflow_name == CI_WORKFLOW.name
    }
    blocking = set(jobs) - {"build", "ci-gate"} - dispatch_only

    assert gate["name"] == "CI Gate"
    assert gate["if"] == "always()"
    assert gate["steps"][0]["env"]["BLOCKING_JOB_RESULTS_JSON"] == "${{ toJSON(needs) }}"
    assert _needs(gate) == blocking | {"build"}
    assert _needs(jobs["build"]) == blocking


def test_optional_benchmark_jobs_are_exactly_workflow_dispatch_only() -> None:
    jobs = _jobs()
    gate_needs = _needs(jobs["ci-gate"])
    build_needs = _needs(jobs["build"])

    assert set(EXPLICIT_WORKFLOW_DISPATCH_ONLY_JOBS) == {
        ("ci.yml", "pytest-xdist-benchmark-pair"),
        ("ci.yml", "pytest-xdist-benchmark-aggregate"),
    }
    for (workflow_name, job_id), mode in EXPLICIT_WORKFLOW_DISPATCH_ONLY_JOBS.items():
        assert workflow_name == CI_WORKFLOW.name
        assert job_id not in gate_needs
        assert job_id not in build_needs
        assert jobs[job_id]["if"] == (
            f"github.event_name == 'workflow_dispatch' && inputs.xdist_benchmark_mode == '{mode}'"
        )
        install_uv = next(
            step for step in jobs[job_id]["steps"] if step.get("name") == "Install uv"
        )
        assert install_uv["with"]["version"] == "0.9.18"

    minimum_install = next(
        step
        for step in jobs["dependency-compatibility"]["steps"]
        if step.get("name") == "Install minimum supported dependencies"
    )
    assert '"pytest-xdist==3.8.0"' in minimum_install["run"]


def test_supported_python_matrix_keeps_visible_interpreter_boundaries() -> None:
    test_job = _jobs()["test"]
    strategy = test_job["strategy"]
    matrix = strategy["matrix"]
    steps = {
        step["name"]: step
        for step in test_job["steps"]
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    }

    assert test_job["name"] == "Test (Python ${{ matrix.python-version }})"
    assert strategy["fail-fast"] == "false"
    assert matrix == {"python-version": ["3.12", "3.13", "3.14"]}
    assert "--lane supported-python" in steps["Run tests with suite timing"]["run"]
    assert steps["Run tests with suite timing"]["run"].endswith("-v")
    assert steps["Run tests"]["run"].endswith("-v")
    test_commands = "\n".join(
        str(step.get("run", "")) for step in test_job["steps"] if isinstance(step, dict)
    )
    assert "test-cov-phased" not in test_commands
    assert "test-xdist" not in test_commands
    assert "pytest -n" not in test_commands
    assert "--execution xdist" not in test_commands


def test_proven_external_test_jobs_remain_separate_and_visible() -> None:
    jobs = _jobs()
    testproject = jobs["testproject"]
    live_cluster = jobs["live-cluster"]
    postgresql = jobs["postgresql-coordination"]

    assert testproject["name"] == "Testproject Smoke"
    assert testproject["steps"][-1] == {
        "name": "Validate sample project",
        "run": "uv run make test-testproject",
    }
    assert live_cluster["name"] == "Live Cluster Fault Tests"
    assert "concurrency" not in live_cluster
    assert next(
        step["run"]
        for step in live_cluster["steps"]
        if step.get("name") == "Run live cluster fault tests"
    ) == ("uv run pytest tests/integration/test_live_failure_injection.py -m live_cluster -v")
    assert postgresql["name"] == "PostgreSQL Coordination & Polling"
    assert (
        next(
            step["run"]
            for step in postgresql["steps"]
            if step.get("name") == "Run PostgreSQL coordination and polling tests"
        )
        == "uv run --no-sync --python 3.12 make test-postgres"
    )
    postgres_target = MAKEFILE.read_text(encoding="utf-8").split(
        "# Validate the bundled sample project's user-facing boundary", maxsplit=1
    )[0]
    assert "tests/integration/test_priority_migration.py" in postgres_target


def test_package_job_smokes_the_installed_wheel_without_a_new_matrix() -> None:
    package_job = _jobs()["build"]
    steps = {
        step["name"]: step
        for step in package_job["steps"]
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    }

    assert "strategy" not in package_job
    smoke = steps["Verify installed wheel and migrations"]["run"]
    assert "--isolated --no-project --python 3.12" in smoke
    assert '--with "$wheel"' in smoke
    assert "scripts/verify_wheel.py" in smoke
    assert '"$(uv version --short)"' in smoke


def test_pr_concurrency_cancels_only_stale_pr_workflows() -> None:
    ci = _workflow()
    commit_messages = _workflow(WORKFLOWS / "commit-messages.yml")

    assert ci["concurrency"] == {
        "group": "ci-${{ github.event.pull_request.number || github.run_id }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    assert commit_messages["concurrency"] == {
        "group": "commit-messages-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }


def test_public_workflows_do_not_invoke_native_compiled_graph() -> None:
    assert not (WORKFLOWS / "compiled-graph-canary.yml").exists()
    forbidden = {
        "django_ray.runtime.compiled_graph_probe",
        "scripts/compiled_session_topology_probe.py",
        "--candidate-native",
        "--unsafe-native",
        "DJANGO_RAY_ALLOW_UNSAFE_COMPILED_GRAPH_PROBE",
        "DJANGO_RAY_RUN_COMPILED_SESSION_TOPOLOGY_PROBE",
    }

    for path in _workflow_paths():
        workflow = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in workflow, (path.name, token)


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


def test_blocking_ci_jobs_cannot_tolerate_job_or_step_failures() -> None:
    blocking_jobs = {(CI_WORKFLOW.name, job_id): job for job_id, job in _jobs().items()}
    for workflow_job in REQUIRED_CHECK_JOBS:
        workflow_name, job_id = workflow_job
        blocking_jobs[workflow_job] = _jobs(WORKFLOWS / workflow_name)[job_id]

    for workflow_job, job in blocking_jobs.items():
        assert not _contains_key(job, "continue-on-error"), workflow_job


def test_pull_request_jobs_are_gated_required_or_explicitly_nonblocking() -> None:
    gate_needs = _needs(_gate_job())
    observed_nonblocking: set[tuple[str, str]] = set()
    observed_dispatch_only: set[tuple[str, str]] = set()

    for path in _workflow_paths():
        if not _events(path) & {"pull_request", "pull_request_target"}:
            continue
        for job_id, job in _jobs(path).items():
            key = (path.name, job_id)
            if key in REQUIRED_CHECK_JOBS:
                assert job.get("name") == REQUIRED_CHECK_JOBS[key]
                continue
            gated = path == CI_WORKFLOW and job_id in gate_needs
            nonblocking = key in EXPLICIT_NONBLOCKING_PR_JOBS
            dispatch_only = key in EXPLICIT_WORKFLOW_DISPATCH_ONLY_JOBS
            assert sum((gated, nonblocking, dispatch_only)) == 1, key
            if nonblocking:
                observed_nonblocking.add(key)
            if dispatch_only:
                observed_dispatch_only.add(key)

    assert observed_nonblocking == set(EXPLICIT_NONBLOCKING_PR_JOBS)
    assert observed_dispatch_only == set(EXPLICIT_WORKFLOW_DISPATCH_ONLY_JOBS)


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
    results["lint"] = result

    with pytest.raises(SystemExit, match=rf"CI Gate blocked: lint={result}"):
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
    partial.pop("build")
    with pytest.raises(SystemExit, match="missing=build; unexpected=-"):
        _execute_gate(monkeypatch, partial)

    unexpected = _all_successful_results() | {"unreviewed-job": "success"}
    with pytest.raises(SystemExit, match="missing=-; unexpected=unreviewed-job"):
        _execute_gate(monkeypatch, unexpected)


def test_required_and_nonblocking_workflows_are_documented() -> None:
    documentation = CONTRIBUTING.read_text(encoding="utf-8") + CONTRIBUTING_DOCS.read_text(
        encoding="utf-8"
    )

    assert "`CI Gate`" in documentation
    assert "`Commit Messages`" in documentation
    assert "guarded local KubeRay" in documentation
    assert "benchmark workflows" in documentation
    for reason in EXPLICIT_NONBLOCKING_PR_JOBS.values():
        assert reason.strip()
        assert reason in documentation
