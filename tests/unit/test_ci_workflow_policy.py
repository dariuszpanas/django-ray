from __future__ import annotations

import json
import re
import textwrap
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from packaging.version import Version

PROJECT_ROOT = Path(__file__).parents[2]
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
DOCS_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "docs.yml"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
MAKEFILE = PROJECT_ROOT / "Makefile"
TEST_SUITE_TAXONOMY = PROJECT_ROOT / ".github" / "test-suite-taxonomy.json"
CONTRIBUTING = PROJECT_ROOT / "CONTRIBUTING.md"
CONTRIBUTING_DOCS = PROJECT_ROOT / "docs" / "contributing.md"
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
REQUIRED_CHECK_JOBS = {
    ("ci.yml", "ci-gate"): "CI Gate",
    ("codex-review.yml", "codex-review"): "Codex Review",
    ("commit-messages.yml", "conventional-commits"): "Commit Messages",
    ("maintainer-approval.yml", "maintainer-approval"): "Maintainer Approval",
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
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        "EVENT_REF": "${{ github.ref }}",
        "EVENT_SHA": "${{ github.sha }}",
    }
    assert "^[0-9a-f]{40}$" in input_validation["run"]
    assert '"$EVENT_REF" != "refs/heads/$DEFAULT_BRANCH"' in input_validation["run"]
    assert '"$CANDIDATE_SHA" != "$EVENT_SHA"' in input_validation["run"]

    checkout = by_name["Check out manual candidate"]
    assert checkout["if"] == "github.event_name == 'workflow_dispatch'"
    assert checkout["with"] == {
        "fetch-depth": "0",
        "persist-credentials": "false",
        "ref": "${{ github.event.repository.default_branch }}",
    }

    checked_out = by_name["Confirm checked out manual candidate"]
    assert checked_out["if"] == "github.event_name == 'workflow_dispatch'"
    assert checked_out["env"] == {
        "CANDIDATE_SHA": "${{ inputs.candidate_sha }}",
        "EVENT_SHA": "${{ github.sha }}",
    }
    assert 'checked_out_sha="$(git rev-parse HEAD)"' in checked_out["run"]
    assert '"$checked_out_sha" != "$CANDIDATE_SHA"' in checked_out["run"]
    assert '"$checked_out_sha" != "$EVENT_SHA"' in checked_out["run"]

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
    assert step_names.index("Check out manual candidate") < step_names.index(
        "Confirm checked out manual candidate"
    )
    assert step_names.index("Confirm checked out manual candidate") < step_names.index(
        "Refresh release refs"
    )
    assert step_names.index("Refresh release refs") < step_names.index(
        "Verify manual candidate source"
    )
    assert step_names.index("Verify manual candidate source") < step_names.index("Install uv")
    assert step_names.index("Verify manual candidate source") < step_names.index("Build package")
    assert _needs(_jobs(RELEASE_WORKFLOW)["build"]) == {
        "source-preflight",
        "dependency-audit",
    }


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


def test_workflows_declare_least_privilege_token_permissions() -> None:
    for path in _workflow_paths():
        permissions = _workflow(path).get("permissions")
        assert isinstance(permissions, dict), path

    for path in (CI_WORKFLOW, DOCS_WORKFLOW, RELEASE_WORKFLOW):
        assert _workflow(path)["permissions"] == {"contents": "read"}

    release_jobs = _jobs(RELEASE_WORKFLOW)
    assert release_jobs["publish-testpypi"]["permissions"] == {"id-token": "write"}
    assert release_jobs["publish-pypi"]["permissions"] == {"id-token": "write"}
    assert release_jobs["github-release"]["permissions"] == {"contents": "write"}


def test_release_never_checks_out_dispatch_input_or_persists_credentials() -> None:
    jobs = _jobs(RELEASE_WORKFLOW)
    checkout_steps = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkout_steps
    for checkout in checkout_steps:
        checkout_with = checkout.get("with", {})
        assert checkout_with.get("persist-credentials") == "false"
        checkout_ref = str(checkout_with.get("ref", ""))
        assert "inputs." not in checkout_ref
        assert "github.event.inputs" not in checkout_ref

    for job_id in ("source-preflight", "build"):
        steps = jobs[job_id]["steps"]
        by_name = {
            str(step["name"]): step for step in steps if isinstance(step, dict) and "name" in step
        }
        manual_checkout = by_name["Check out manual candidate"]
        assert manual_checkout["with"]["ref"] == ("${{ github.event.repository.default_branch }}")
        order = [str(step.get("name", step.get("uses", ""))) for step in steps]
        assert order.index("Confirm checked out manual candidate") < order.index(
            "Verify manual candidate source"
        )


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


def test_minimum_dependency_lane_pins_sample_extra_dependencies() -> None:
    minimum_install = next(
        step
        for step in _jobs()["dependency-compatibility"]["steps"]
        if step.get("name") == "Install minimum supported dependencies"
    )
    install_command = minimum_install["run"]

    assert '-e ".[sample]"' in install_command
    assert install_command.count('"gunicorn==23.0"') == 1
    assert '"django-unfold==' not in install_command


def test_minimum_dependency_lane_pins_runtime_encryption_dependency() -> None:
    """The minimum lane must exercise the declared cryptography compatibility floor."""
    minimum_install = next(
        step
        for step in _jobs()["dependency-compatibility"]["steps"]
        if step.get("name") == "Install minimum supported dependencies"
    )

    assert '"cryptography==42.0.8"' in minimum_install["run"]


def test_dependency_security_floor_and_runtime_audit_are_blocking() -> None:
    minimum_install = next(
        step
        for step in _jobs()["dependency-compatibility"]["steps"]
        if step.get("name") == "Install minimum supported dependencies"
    )
    audit_job = _jobs()["dependency-audit"]
    audit_command = next(
        step["run"]
        for step in audit_job["steps"]
        if step.get("name") == "Audit locked runtime dependencies"
    )

    assert '"pyasn1==0.6.4"' in minimum_install["run"]
    assert '"ray[default]==2.56.0"' in minimum_install["run"]
    assert audit_job["name"] == (
        "Runtime Dependency Audit (${{ matrix.os }}, Python ${{ matrix.python-version }})"
    )
    assert audit_job["runs-on"] == "${{ matrix.os }}"
    assert audit_job["timeout-minutes"] == "10"
    assert audit_job["strategy"] == {
        "fail-fast": "false",
        "matrix": {
            "include": [
                {"os": "ubuntu-latest", "python-version": "3.12"},
                {"os": "ubuntu-latest", "python-version": "3.13"},
                {"os": "ubuntu-latest", "python-version": "3.14"},
                {"os": "windows-latest", "python-version": "3.12"},
            ]
        },
    }
    audit_install = next(
        step["run"]
        for step in audit_job["steps"]
        if step.get("name") == "Install locked audit dependencies"
    )
    assert audit_install == (
        "uv sync --locked --only-group dev --no-install-project "
        "--python ${{ matrix.python-version }}"
    )
    assert audit_command == (
        "uv run --no-sync --python ${{ matrix.python-version }} "
        "python scripts/audit_runtime_dependencies.py"
    )
    assert "dependency-audit" in _needs(_jobs()["build"])
    assert "dependency-audit" in _needs(_gate_job())


def test_release_dependency_audit_rechecks_every_supported_environment() -> None:
    audit_job = _jobs(RELEASE_WORKFLOW)["dependency-audit"]
    audit_steps = {
        str(step["name"]): step
        for step in audit_job["steps"]
        if isinstance(step, dict) and "name" in step
    }

    assert audit_job["name"] == (
        "Runtime Dependency Audit (${{ matrix.os }}, Python ${{ matrix.python-version }})"
    )
    assert audit_job["runs-on"] == "${{ matrix.os }}"
    assert audit_job["timeout-minutes"] == "10"
    assert audit_job["strategy"] == {
        "fail-fast": "false",
        "matrix": {
            "include": [
                {"os": "ubuntu-latest", "python-version": "3.12"},
                {"os": "ubuntu-latest", "python-version": "3.13"},
                {"os": "ubuntu-latest", "python-version": "3.14"},
                {"os": "windows-latest", "python-version": "3.12"},
            ]
        },
    }
    assert audit_steps["Check out release candidate"]["with"]["ref"] == "${{ github.sha }}"
    assert audit_steps["Install locked audit dependencies"]["run"] == (
        "uv sync --locked --only-group dev --no-install-project "
        "--python ${{ matrix.python-version }}"
    )
    assert audit_steps["Audit locked runtime dependencies"]["run"] == (
        "uv run --no-sync --python ${{ matrix.python-version }} "
        "python scripts/audit_runtime_dependencies.py"
    )
    assert _needs(audit_job) == {"source-preflight"}
    assert _needs(_jobs(RELEASE_WORKFLOW)["build"]) == {
        "source-preflight",
        "dependency-audit",
    }


def test_release_source_preflight_runs_before_any_dependency_installation() -> None:
    jobs = _jobs(RELEASE_WORKFLOW)
    preflight = jobs["source-preflight"]
    steps = preflight["steps"]
    step_names = [str(step["name"]) for step in steps]
    commands = "\n".join(str(step.get("run", "")) for step in steps)

    assert preflight["name"] == "Verify Release Source"
    assert "Install uv" not in step_names
    assert "Install dependencies" not in step_names
    assert "uv sync" not in commands
    assert "scripts/verify_release_source.py" in commands
    assert step_names.index("Validate manual candidate input") < step_names.index(
        "Check out manual candidate"
    )
    assert step_names.index("Check out manual candidate") < step_names.index(
        "Confirm checked out manual candidate"
    )
    assert step_names.index("Confirm checked out manual candidate") < step_names.index(
        "Refresh release refs"
    )
    assert step_names.index("Refresh release refs") < step_names.index(
        "Verify manual candidate source"
    )
    assert _needs(jobs["dependency-audit"]) == {"source-preflight"}
    assert _needs(jobs["build"]) == {"source-preflight", "dependency-audit"}


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


def test_ray_data_golden_path_runs_real_optional_dependency_endpoints() -> None:
    job = _jobs()["ray-data-golden-path"]
    assert job["name"] == "Ray Data (${{ matrix.profile }}, Python ${{ matrix.python-version }})"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "10"
    assert job["strategy"] == {
        "fail-fast": "false",
        "matrix": {
            "include": [
                {
                    "profile": "supported-min-python",
                    "python-version": "3.12",
                    "ray-version": "2.56.0",
                },
                {
                    "profile": "newest-python",
                    "python-version": "3.14",
                    "ray-version": "2.56.0",
                },
            ]
        },
    }

    commands = "\n".join(
        str(step.get("run", "")) for step in job["steps"] if isinstance(step, dict)
    )
    assert "uv python install ${{ matrix.python-version }}" in commands
    assert "uv run --isolated --no-project --python ${{ matrix.python-version }}" in commands
    assert '--with-editable ".[sample]"' in commands
    assert '--with "ray[data]==${{ matrix.ray-version }}"' in commands
    assert "python scripts/ray_data_golden_path_probe.py" in commands

    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_ray = Version(
        next(package["version"] for package in lock["package"] if package["name"] == "ray")
    )
    endpoint_versions = {
        Version(endpoint["ray-version"]) for endpoint in job["strategy"]["matrix"]["include"]
    }
    # The workflow deliberately pins its reproducible support-floor probe. The
    # latest-dependency lane upgrades uv.lock before running this policy test,
    # so a newer compatible lock must not rewrite that historical endpoint.
    assert all(endpoint_version <= locked_ray for endpoint_version in endpoint_versions)


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
    assert "tests/integration/test_execution_protocol_schema_migration.py" in makefile_paths
    assert "tests/integration/test_protocol_coordination.py" in makefile_paths
    assert "tests/integration/test_ray_target_route_migration.py" in makefile_paths
    assert "tests/integration/test_ray_target_routing_coordination.py" in makefile_paths
    assert "tests/integration/test_ray_worker_target_capabilities.py" in makefile_paths
    assert "tests/integration/test_ray_worker_target_capability_migration.py" in makefile_paths
    assert "tests/integration/test_workflow_run_allocation_migration.py" in makefile_paths

    schema_migrations = next(
        domain
        for domain in taxonomy["domains"]
        if domain["id"] == "schema-migrations-and-bootstrap"
    )
    assert (
        "tests/integration/test_execution_protocol_schema_migration.py"
        in schema_migrations["selection"]["paths"]
    )
    assert (
        "tests/integration/test_workflow_run_allocation_migration.py"
        in schema_migrations["selection"]["paths"]
    )
    assert (
        "tests/integration/test_ray_target_route_migration.py"
        in schema_migrations["selection"]["paths"]
    )
    assert (
        "tests/integration/test_ray_worker_target_capability_migration.py"
        in schema_migrations["selection"]["paths"]
    )
    coordination = next(
        domain
        for domain in taxonomy["domains"]
        if domain["id"] == "coordination-polling-and-recovery"
    )
    assert "tests/integration/test_protocol_coordination.py" in coordination["selection"]["paths"]
    assert (
        "tests/integration/test_ray_target_routing_coordination.py"
        in coordination["selection"]["paths"]
    )
    assert (
        "tests/integration/test_ray_worker_target_capabilities.py"
        in coordination["selection"]["paths"]
    )
    repository_policy = next(
        domain for domain in taxonomy["domains"] if domain["id"] == "repository-policy-and-release"
    )
    assert "tests/unit/test_verify_wheel.py" in repository_policy["selection"]["paths"]


def test_live_cluster_scenarios_are_process_isolated_bounded_and_visible() -> None:
    live_cluster = _jobs()["live-cluster"]
    assert live_cluster["env"]["PYTHONUNBUFFERED"] == "1"
    assert live_cluster["env"]["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] == "0"
    indexed_steps = {
        step.get("name"): (index, step)
        for index, step in enumerate(live_cluster["steps"])
        if isinstance(step, dict)
    }
    python_pin_index, python_pin_step = indexed_steps["Pin exact cluster Python"]
    python_setup_index, python_setup_step = indexed_steps["Set up Python"]
    install_index = indexed_steps["Install dependencies"][0]
    readiness_index, readiness_step = indexed_steps["Verify Ray Client readiness"]
    scenario_index, step = indexed_steps["Run isolated live cluster fault scenarios"]
    assert python_pin_index < python_setup_index < install_index < readiness_index < scenario_index

    python_pin = python_pin_step["run"]
    assert "docker exec ray-head python" in python_pin
    assert "docker exec ray-worker python" in python_pin
    assert "^3\\.12\\.[0-9]+$" in python_pin
    assert '"$head_python" != "$worker_python"' in python_pin
    assert "DJANGO_RAY_LIVE_PYTHON=$head_python" in python_pin
    assert '>> "$GITHUB_ENV"' in python_pin
    assert python_setup_step["run"] == 'uv python install "$DJANGO_RAY_LIVE_PYTHON"'
    install_dependencies = indexed_steps["Install dependencies"][1]["run"]
    assert 'uv sync --frozen --python "$DJANGO_RAY_LIVE_PYTHON"' in install_dependencies
    assert ".venv/bin/python -c" in install_dependencies
    assert '"$client_python" != "$DJANGO_RAY_LIVE_PYTHON"' in install_dependencies

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
        "test_target_attestation_probes_every_package_free_ray_client_node",
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
    smoke = steps["Verify distribution metadata, installed wheel, and migrations"]["run"]
    assert "--isolated --no-project --python 3.12" in smoke
    assert '--with "$wheel"' in smoke
    assert "scripts/verify_wheel.py" in smoke
    assert "--dist-dir dist" in smoke
    assert '"$(uv version --short)"' in smoke


def test_release_matrix_verifies_wheel_and_sdist_metadata() -> None:
    release_job = _jobs(RELEASE_WORKFLOW)["test"]
    steps = {
        step["name"]: step
        for step in release_job["steps"]
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    }

    smoke = steps["Verify distribution metadata and installed wheel"]["run"]
    assert "scripts/verify_wheel.py" in smoke
    assert "--dist-dir dist" in smoke
    assert '"${{ needs.build.outputs.version }}"' in smoke


def test_pr_concurrency_cancels_only_stale_pr_workflows() -> None:
    ci = _workflow()
    codex_review = _workflow(WORKFLOWS / "codex-review.yml")
    commit_messages = _workflow(WORKFLOWS / "commit-messages.yml")
    maintainer_approval = _workflow(WORKFLOWS / "maintainer-approval.yml")

    assert ci["concurrency"] == {
        "group": "ci-${{ github.event.pull_request.number || github.run_id }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    assert commit_messages["concurrency"] == {
        "group": "commit-messages-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }
    assert maintainer_approval["concurrency"] == {
        "group": "maintainer-approval-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }
    assert codex_review["concurrency"] == {
        "group": "codex-review-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }


def test_review_policy_workflows_cover_current_head_lifecycle_events() -> None:
    lifecycle_types = [
        "opened",
        "synchronize",
        "reopened",
        "edited",
        "ready_for_review",
        "converted_to_draft",
    ]
    maintainer_events = _workflow(WORKFLOWS / "maintainer-approval.yml")["on"]
    codex_events = _workflow(WORKFLOWS / "codex-review.yml")["on"]

    assert maintainer_events == {
        "pull_request_target": {"types": lifecycle_types},
        "pull_request_review": {"types": ["submitted", "dismissed"]},
    }
    assert codex_events == {"pull_request_target": {"types": [*lifecycle_types, "closed"]}}


@pytest.mark.parametrize(
    ("workflow_name", "job_id", "timeout_minutes"),
    [
        ("maintainer-approval.yml", "maintainer-approval", "5"),
        ("codex-review.yml", "codex-review", "35"),
    ],
)
def test_review_policy_workflows_execute_only_trusted_default_branch_code(
    workflow_name: str,
    job_id: str,
    timeout_minutes: str,
) -> None:
    path = WORKFLOWS / workflow_name
    workflow = _workflow(path)
    job = _jobs(path)[job_id]

    expected_permissions = {"contents": "read", "pull-requests": "read"}
    if workflow_name == "codex-review.yml":
        expected_permissions["actions"] = "read"
        expected_permissions["pull-requests"] = "write"
    assert workflow["permissions"] == expected_permissions
    assert set(_jobs(path)) == {job_id}
    assert job["timeout-minutes"] == timeout_minutes
    assert "if" not in job
    assert "continue-on-error" not in job
    assert "needs" not in job

    steps = job["steps"]
    assert isinstance(steps, list)
    checkout = steps[0]
    assert checkout["uses"] == CHECKOUT_ACTION
    assert checkout["with"] == {
        "fetch-depth": "1",
        "ref": "${{ github.event.repository.default_branch }}",
        "persist-credentials": "false",
    }
    if workflow_name == "codex-review.yml":
        assert steps[1]["if"] == (
            "github.event.action != 'converted_to_draft' && github.event.action != 'closed'"
        )
        assert all("if" not in step for step in (steps[0], *steps[2:]))
    else:
        assert all("if" not in step for step in steps)
    assert not _contains_key(steps, "continue-on-error")

    validation = steps[1]
    assert validation["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert "scripts/check_pr_review_policy.py" in validation["run"]
    assert "${{" not in validation["run"]
    assert "git fetch" not in validation["run"]


def test_maintainer_approval_invokes_exact_base_and_head_policy() -> None:
    step = _jobs(WORKFLOWS / "maintainer-approval.yml")["maintainer-approval"]["steps"][1]

    assert step["env"] == {
        "BASE_REF": "${{ github.event.pull_request.base.ref }}",
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
        "PR_NUMBER": "${{ github.event.pull_request.number }}",
    }
    assert "--mode maintainer" in step["run"]
    assert '--repository "$GITHUB_REPOSITORY"' in step["run"]
    assert '--pull-request "$PR_NUMBER"' in step["run"]
    assert '--expected-base-ref "$BASE_REF"' in step["run"]
    assert '--expected-base-sha "$BASE_SHA"' in step["run"]
    assert '--expected-head "$HEAD_SHA"' in step["run"]


def test_codex_review_requests_and_polls_one_attempt_bound_comment() -> None:
    steps = _jobs(WORKFLOWS / "codex-review.yml")["codex-review"]["steps"]
    request = steps[1]
    step = steps[2]

    common_env = {
        "ACTION": "${{ github.event.action }}",
        "BASE_REF": "${{ github.event.pull_request.base.ref }}",
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "BASELINE_TIME": "${{ github.event.pull_request.updated_at }}",
        "GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
        "PR_NUMBER": "${{ github.event.pull_request.number }}",
        "RUN_ATTEMPT": "${{ github.run_attempt }}",
        "RUN_ID": "${{ github.run_id }}",
    }
    assert request["env"] == common_env
    assert request["id"] == "request"
    assert "--mode codex-request" in request["run"]
    assert '--workflow-run-id "$RUN_ID"' in request["run"]
    assert '--run-attempt "$RUN_ATTEMPT"' in request["run"]
    assert '--event-path "$GITHUB_EVENT_PATH"' in request["run"]
    assert "printf 'comment-id=%s\\n'" in request["run"]
    assert '>> "$GITHUB_OUTPUT"' in request["run"]
    assert step["env"] == {
        **common_env,
        "REQUEST_COMMENT_ID": "${{ steps.request.outputs.comment-id }}",
    }
    assert "--mode codex" in step["run"]
    assert '--expected-base-ref "$BASE_REF"' in step["run"]
    assert '--expected-base-sha "$BASE_SHA"' in step["run"]
    assert '--expected-head "$HEAD_SHA"' in step["run"]
    assert '--action "$ACTION"' in step["run"]
    assert '--baseline-time "$BASELINE_TIME"' in step["run"]
    assert '--workflow-run-id "$RUN_ID"' in step["run"]
    assert '--run-attempt "$RUN_ATTEMPT"' in step["run"]
    assert '--event-path "$GITHUB_EVENT_PATH"' in step["run"]
    assert 'REQUEST_COMMENT_ARGS=(--request-comment-id "$REQUEST_COMMENT_ID")' in step["run"]
    assert "--poll-timeout 1800" in step["run"]
    assert "--poll-interval 15" in step["run"]
    assert "BASE_CHANGED" not in step["run"]
    assert "--base-changed" not in step["run"]
    assert "eval " not in step["run"]
    for command in (request["run"], step["run"]):
        assert "PR_TITLE" not in command
        assert "PR_BODY" not in command
        assert "--expected-title" not in command
        assert "--expected-body" not in command


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
    documents = [
        CONTRIBUTING.read_text(encoding="utf-8"),
        CONTRIBUTING_DOCS.read_text(encoding="utf-8"),
    ]
    combined = "".join(documents)

    for documentation in documents:
        normalized_documentation = " ".join(documentation.split())
        for check_name in REQUIRED_CHECK_NAMES:
            assert f"`{check_name}`" in documentation
        assert "native required review-conversation resolution" in documentation
        assert (
            "django-ray:codex-review-head=<full head SHA>;run=<workflow run ID>;"
            "attempt=<run attempt>;metadata=<title/body digest>;lifecycle=<event digest>"
            in normalized_documentation
        )
        assert "current-head approval for every other author" in documentation
        assert "trusted event base ref, base SHA, and head SHA" in documentation
        assert (
            "base change, or title- or body-only edit requires a new Codex outcome" in documentation
        )
        assert "existing exact-head connector review is not reusable" in normalized_documentation
        assert "trusted `Codex Review` workflow posts one full-SHA" in documentation
        assert "immutable comment ID" in documentation
        assert "Contributors do not need to post a second request" in normalized_documentation
        assert "Only an `eyes` reaction from the immutable Codex connector" in documentation
        assert (
            "reactions from Actions, maintainers, or other users are ignored"
            in normalized_documentation
        )
        assert "that request or the pull-request root" in normalized_documentation
        assert "must first observe Codex `eyes`" in normalized_documentation
        assert "two consecutive eye-free polling observations" in normalized_documentation
        assert "does not interpret `+1`, formal review state" in normalized_documentation
        assert "attempt waits up to 30 minutes" in normalized_documentation
        assert (
            "reads title and body from the runner-provided `GITHUB_EVENT_PATH`"
            in normalized_documentation
        )
        assert (
            "never places the potentially large body in an environment variable or command argument"
            in normalized_documentation
        )
        assert "metadata digest binds those values" in normalized_documentation
        assert (
            "canonical digest of bounded close, reopen, draft-conversion, and ready events"
            in normalized_documentation
        )
        assert (
            "brackets a lifecycle re-read with two live pull-request reads"
            in normalized_documentation
        )
        assert "stable activity timestamp" in normalized_documentation
        assert "reaction-driven pull-request activity timestamps" in normalized_documentation
        assert "proves that the requested review settled" in normalized_documentation
        assert "does not reinterpret findings as an approval" in normalized_documentation
        assert "completed Codex review with findings cannot merge" in documentation
        assert "strict required-status freshness" in documentation
        assert "external or bot-authored canary" in documentation
        assert "not absolute enforcement" in documentation
    assert "guarded local KubeRay" in combined
    assert "benchmark workflows" in combined
    for reason in EXPLICIT_NONBLOCKING_PR_JOBS.values():
        assert reason.strip()
        assert all(reason in documentation for documentation in documents)
