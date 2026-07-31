from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parents[2]
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
MAKEFILE = PROJECT_ROOT / "Makefile"
TEST_SUITE_TAXONOMY = PROJECT_ROOT / ".github" / "test-suite-taxonomy.json"
CONTRIBUTING = PROJECT_ROOT / "CONTRIBUTING.md"
CONTRIBUTING_DOCS = PROJECT_ROOT / "docs" / "contributing.md"
REQUIRED_CHECK_JOBS = {
    ("ci.yml", "ci-gate"): "CI Gate",
    ("commit-messages.yml", "conventional-commits"): "Commit Messages",
}
REQUIRED_CHECK_NAMES = set(REQUIRED_CHECK_JOBS.values())
EXPLICIT_NONBLOCKING_PR_JOBS: dict[tuple[str, str], str] = {}


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


def test_manual_release_is_bound_to_full_fetched_main_sha_before_build() -> None:
    workflow = _workflow(RELEASE_WORKFLOW)
    dispatch = workflow["on"]["workflow_dispatch"]
    inputs = dispatch["inputs"]
    assert inputs["candidate_sha"] == {
        "description": "Authorized full origin/main commit SHA",
        "required": "true",
        "type": "string",
    }

    steps = _jobs(RELEASE_WORKFLOW)["build"]["steps"]
    by_name = {
        str(step["name"]): step for step in steps if isinstance(step, dict) and "name" in step
    }
    input_validation = by_name["Validate manual candidate input"]
    assert input_validation["if"] == "github.event_name == 'workflow_dispatch'"
    assert input_validation["env"] == {
        "CANDIDATE_SHA": "${{ inputs.candidate_sha }}",
        "EVENT_SHA": "${{ github.sha }}",
    }
    assert "^[0-9a-f]{40}$" in input_validation["run"]
    assert '"$CANDIDATE_SHA" != "$EVENT_SHA"' in input_validation["run"]

    checkout = by_name["Check out manual candidate"]
    assert checkout["if"] == "github.event_name == 'workflow_dispatch'"
    assert checkout["with"]["ref"] == "${{ inputs.candidate_sha }}"

    refresh = by_name["Refresh release refs"]
    assert "git fetch --force --prune --tags origin" in refresh["run"]
    assert "+refs/heads/main:refs/remotes/origin/main" in refresh["run"]

    manual = by_name["Verify manual candidate source"]
    assert manual["if"] == "github.event_name == 'workflow_dispatch'"
    assert manual["env"] == {
        "CANDIDATE_SHA": "${{ inputs.candidate_sha }}",
        "EVENT_SHA": "${{ github.sha }}",
    }
    assert "scripts/verify_release_source.py" in manual["run"]
    assert '--manual-candidate "$CANDIDATE_SHA"' in manual["run"]
    assert '--event-sha "$EVENT_SHA"' in manual["run"]

    step_names = [
        str(step.get("name", step.get("uses", ""))) for step in steps if isinstance(step, dict)
    ]
    assert step_names.index("Validate manual candidate input") < step_names.index(
        "Check out manual candidate"
    )
    assert step_names.index("Check out manual candidate") < step_names.index("Refresh release refs")
    assert step_names.index("Refresh release refs") < step_names.index(
        "Verify manual candidate source"
    )
    assert step_names.index("Verify manual candidate source") < step_names.index("Install uv")
    assert step_names.index("Verify manual candidate source") < step_names.index("Build package")


def test_manual_and_production_release_validation_remain_distinct() -> None:
    steps = _jobs(RELEASE_WORKFLOW)["build"]["steps"]
    by_name = {
        str(step["name"]): step for step in steps if isinstance(step, dict) and "name" in step
    }

    production = by_name["Verify production tag source"]
    assert production["if"] == (
        "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    )
    assert '--production-tag "$RELEASE_TAG"' in production["run"]

    candidate_validation = by_name["Validate TestPyPI candidate version"]
    assert candidate_validation["if"] == "github.event_name == 'workflow_dispatch'"
    assert "--testpypi-candidate" in candidate_validation["run"]

    production_validation = by_name["Validate production release version"]
    assert production_validation["if"] == (
        "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    )
    assert (
        'version="$(uv run --no-sync python scripts/validate_release.py "$RELEASE_REF")"'
        in production_validation["run"]
    )


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
    blocking = set(jobs) - {"build", "ci-gate"}

    assert gate["name"] == "CI Gate"
    assert gate["if"] == "always()"
    assert gate["steps"][0]["env"]["BLOCKING_JOB_RESULTS_JSON"] == "${{ toJSON(needs) }}"
    assert _needs(gate) == blocking | {"build"}
    assert _needs(jobs["build"]) == blocking


def test_manual_ci_has_no_obsolete_xdist_retention_controls_or_jobs() -> None:
    workflow = _workflow()
    assert workflow["on"]["workflow_dispatch"] == ""
    assert {
        "pytest-xdist-benchmark-pair",
        "pytest-xdist-benchmark-aggregate",
    }.isdisjoint(_jobs())

    source = CI_WORKFLOW.read_text(encoding="utf-8")
    for obsolete in (
        "xdist_benchmark_mode",
        "xdist_benchmark_sample",
        "xdist_benchmark_order",
        "xdist_benchmark_pair_run_ids",
        "test-cov-phased",
        "test_suite_benchmark.py",
    ):
        assert obsolete not in source


def test_obsolete_xdist_retention_harness_is_absent() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert "test-cov-phased" not in makefile
    assert "_test-cov-phased-body" not in makefile
    assert "TEST_SUITE_PHASED_" not in makefile

    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "artifacts/test-suite-phased-coverage/" not in gitignore
    assert "artifacts/pytest-xdist-" not in gitignore

    for relative in (
        "scripts/ray_residue.py",
        "scripts/test_suite_benchmark.py",
        "tests/unit/test_ray_residue.py",
        "tests/unit/test_test_suite_benchmark.py",
    ):
        assert not (PROJECT_ROOT / relative).exists()


def test_minimum_dependency_lane_pins_local_xdist_dependency() -> None:
    minimum_install = next(
        step
        for step in _jobs()["dependency-compatibility"]["steps"]
        if step.get("name") == "Install minimum supported dependencies"
    )
    assert '"pytest-xdist==3.8.0"' in minimum_install["run"]


def test_minimum_dependency_lane_pins_runtime_encryption_dependency() -> None:
    """The minimum lane must exercise the declared cryptography compatibility floor."""
    minimum_install = next(
        step
        for step in _jobs()["dependency-compatibility"]["steps"]
        if step.get("name") == "Install minimum supported dependencies"
    )

    assert '"cryptography==42.0.8"' in minimum_install["run"]


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
    assert postgresql["name"] == "PostgreSQL Coordination & Polling"
    assert (
        next(
            step["run"]
            for step in postgresql["steps"]
            if step.get("name") == "Run PostgreSQL coordination and polling tests"
        )
        == "uv run --no-sync --python 3.12 make test-postgres"
    )
    postgres_target = re.search(
        r"(?m)^test-postgres:\n(?P<recipe>(?:\t.*\n)+)",
        MAKEFILE.read_text(encoding="utf-8"),
    )
    assert postgres_target is not None
    makefile_paths = set(re.findall(r"tests/[^\s\\]+\.py", postgres_target.group("recipe")))
    taxonomy = json.loads(TEST_SUITE_TAXONOMY.read_text(encoding="utf-8"))
    postgresql_lane = next(
        lane for lane in taxonomy["ci_lanes"] if lane["id"] == "postgresql-evidence"
    )

    assert makefile_paths == set(postgresql_lane["selection"]["paths"])


def test_live_cluster_scenarios_are_process_isolated_bounded_and_visible() -> None:
    live_cluster = _jobs()["live-cluster"]
    assert live_cluster["env"]["PYTHONUNBUFFERED"] == "1"
    assert live_cluster["env"]["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] == "0"
    indexed_steps = {
        step.get("name"): (index, step)
        for index, step in enumerate(live_cluster["steps"])
        if isinstance(step, dict)
    }
    install_index = indexed_steps["Install dependencies"][0]
    readiness_index, readiness_step = indexed_steps["Verify Ray Client readiness"]
    scenario_index, step = indexed_steps["Run isolated live cluster fault scenarios"]
    assert install_index < readiness_index < scenario_index

    readiness = readiness_step["run"]
    assert readiness_step["timeout-minutes"] == "3"
    assert "for attempt in 1 2; do" in readiness
    assert readiness.count('.venv/bin/python - "$attempt"') == 1
    assert "timeout --signal=TERM --kill-after=5s 70s" in readiness
    assert "RAY_CLIENT_INITIAL_CONNECTION_TIMEOUT_S=3" in readiness
    assert "RAY_CLIENT_MAX_CONNECTION_TIMEOUT_S=3" in readiness
    assert "faulthandler.dump_traceback_later(45)" in readiness
    assert 'address=os.environ["DJANGO_RAY_LIVE_RAY_ADDRESS"]' in readiness
    assert 'os.environ["DJANGO_RAY_LIVE_MIN_NODES"]' in readiness
    assert 'node.get("Alive")' in readiness
    assert "@ray.remote" in readiness
    assert "ray.get(readiness_probe.remote(), timeout=10)" in readiness
    assert "ray.shutdown()" in readiness
    assert "RAY_CLIENT_READINESS_START attempt=$attempt/2" in readiness
    assert "RAY_CLIENT_READINESS_CONNECTED" in readiness
    assert "RAY_CLIENT_READINESS_PASS attempt=$attempt/2" in readiness
    assert "::warning title=Ray Client readiness retry::" in readiness
    assert "::error title=Ray Client readiness exhausted::" in readiness
    assert '"$readiness_ready" -eq 1' in readiness
    assert '"$readiness_ready" -ne 1' in readiness
    assert readiness.count("sleep 2") == 1
    assert readiness.count("sleep ") == 1
    assert "&" not in readiness
    assert "pytest" not in readiness
    assert "uv run" not in readiness
    assert readiness_step.get("continue-on-error") is None

    command = step["run"]
    scenarios = (
        "tests/integration/test_live_failure_injection.py::"
        "TestLiveFailureInjection::"
        "test_ray_core_runner_submits_project_code_to_generic_cluster",
        "tests/integration/test_live_failure_injection.py::"
        "TestLiveFailureInjection::"
        "test_disconnect_retries_pending_ray_core_task",
        "tests/integration/test_live_failure_injection.py::"
        "TestLiveFailureInjection::"
        "test_cancellation_finalizes_live_pending_task",
    )

    assert command.count(".venv/bin/python -m pytest") == 1
    assert 'for scenario in "${live_cluster_scenarios[@]}"; do' in command
    scenario_block = re.search(
        r"^live_cluster_scenarios=\(\n(?P<body>.*?)^\)\n",
        command,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert scenario_block is not None
    scenario_lines = tuple(
        line.strip() for line in scenario_block.group("body").splitlines() if line.strip()
    )
    assert all(re.fullmatch(r'"[^"]+"', line) for line in scenario_lines)
    declared_scenarios = tuple(line[1:-1] for line in scenario_lines)
    assert declared_scenarios == scenarios
    assert "timeout --signal=TERM --kill-after=15s 165s" in command
    assert '"$scenario" -m live_cluster -vv -s' in command
    assert "--setup-show -o faulthandler_timeout=90" in command
    assert "LIVE_CLUSTER_SCENARIO_START node_id=$scenario" in command
    assert "LIVE_CLUSTER_SCENARIO_PASS node_id=$scenario" in command
    assert "::error title=Live cluster scenario timed out::" in command
    assert "180-second hard ceiling; elapsed ${elapsed_seconds}s" in command
    assert '"$status" -eq 137 && "$elapsed_seconds" -ge 180' in command
    assert "reached the 180-second forced-kill ceiling" in command
    assert "::error title=Live cluster scenario was killed early::" in command
    assert "received SIGKILL before the timeout ceiling" in command
    assert "::error title=Live cluster scenario failed::" in command
    assert "uv run" not in command
    assert re.search(r"(?<!\S)-n(?:=?(?:auto|logical|\d+))?(?:\s|$)", command) is None
    assert re.search(r"(?<!\S)--numprocesses(?:[=\s]|$)", command) is None
    assert re.search(r"(?<!\S)--dist(?:[=\s]|$)", command) is None
    assert "strategy" not in live_cluster

    diagnostics_index, diagnostics_step = indexed_steps["Show bounded Ray Client diagnostics"]
    logs_index, logs_step = indexed_steps["Show Ray container logs"]
    cleanup_index, cleanup_step = indexed_steps["Remove Ray containers"]
    assert scenario_index < diagnostics_index < logs_index < cleanup_index
    assert diagnostics_step["if"] == "always()"
    diagnostics = diagnostics_step["run"]
    assert "/tmp/ray/session_latest/logs/ray_client_server*.err*" in diagnostics
    assert '[[ -s "$path" ]] || continue' in diagnostics
    assert 'tail -n 400 "$path"' in diagnostics
    assert "scripts/bounded_redact.py" in diagnostics
    assert "--max-chars 65536" in diagnostics
    secret_env_names = (
        "DJANGO_API_TOKEN",
        "DJANGO_SECRET_KEY",
        "DATABASE_URL",
        "DATABASE_PASSWORD",
        "POSTGRES_PASSWORD",
        "GITHUB_TOKEN",
        "RAY_JOB_HEADERS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    )
    assert diagnostics.count("--secret-env") == len(secret_env_names)
    for secret_env_name in secret_env_names:
        assert f"--secret-env {secret_env_name}" in diagnostics
    start_cluster = indexed_steps["Start a two-node Ray cluster"][1]["run"]
    assert re.search(r"(?<!\S)(?:-e|--env)(?:[=\s]|$)", start_cluster) is None
    assert "--env-file" not in start_cluster
    assert logs_step["if"] == "always()"
    assert "docker logs ray-head || true" in logs_step["run"]
    assert "docker logs ray-worker || true" in logs_step["run"]
    assert cleanup_step["if"] == "always()"
    assert "docker rm --force ray-worker ray-head || true" in cleanup_step["run"]
    assert "docker network rm django-ray-live-cluster || true" in cleanup_step["run"]


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
            assert sum((gated, nonblocking)) == 1, key
            if nonblocking:
                observed_nonblocking.add(key)

    assert observed_nonblocking == set(EXPLICIT_NONBLOCKING_PR_JOBS)


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
