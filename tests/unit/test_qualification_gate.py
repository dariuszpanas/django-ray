"""Conditional qualification must never infer success from absent evidence."""

import ast
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.qualification_gate import GateError, changed_files, select, verify

SHA = "a" * 40
ENTRY = {
    "id": "latency",
    "workflow": "latency-qualification.yml",
    "paths": ["src/**"],
}


def results():
    return {
        "selection": {
            "result": "success",
            "outputs": {
                "application": "true",
                "native": "false",
                "latency": "true",
                "cancellation": "true",
                "monitoring": "true",
            },
        },
        "application": {"result": "success"},
        "native": {"result": "skipped"},
        "latency": {"result": "success"},
        "cancellation": {"result": "success"},
        "monitoring": {"result": "success"},
    }


def test_workflow_owns_selection_and_every_qualification_dependency():
    root = Path(__file__).resolve().parents[2]
    policy = json.loads((root / "scripts/qualification_policy.json").read_text())
    gate = yaml.load(
        (root / ".github/workflows/qualification-gate.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert gate["on"]["pull_request"] == {"branches": ["main"]}
    assert set(gate["jobs"]["qualification-gate"]["needs"]) == {
        "selection",
        "application",
        "native",
        "latency",
        "cancellation",
        "monitoring",
    }
    assert gate["jobs"]["qualification-gate"]["if"] == "always()"
    assert {entry["id"] for entry in policy} == {
        "application",
        "native",
        "latency",
        "cancellation",
        "monitoring",
    }
    for entry in policy:
        child = yaml.load(
            (root / ".github/workflows" / entry["workflow"]).read_text(), Loader=yaml.BaseLoader
        )
        assert set(child["on"]) == {"workflow_call", "workflow_dispatch"}
        assert "scripts/qualification_gate.py" in entry["paths"]
        job = gate["jobs"][entry["id"]]
        assert job["needs"] == "selection"
        assert job["uses"] == "./.github/workflows/" + entry["workflow"]
        assert job["if"] == f"needs.selection.outputs.{entry['id']} == 'true'"
        if entry["id"] == "native":
            assert [
                item["recipe"] for item in child["jobs"]["native"]["strategy"]["matrix"]["include"]
            ] == ["native", "jobs", "jobs-crash", "core-loss"]


def test_applicability_is_explicit():
    assert select([ENTRY], {"docs/readme.md"}) == []
    assert select([ENTRY], {"src/nested/code.py"}) == [ENTRY]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("qualification/docker/scenario.py", {"native", "latency", "cancellation"}),
        (
            "src/django_ray/lease_policy.py",
            {"application", "native", "cancellation", "monitoring"},
        ),
        (
            "src/django_ray/management/commands/django_ray_lease_policy.py",
            {"application", "native"},
        ),
    ],
)
def test_shared_dependency_selects_every_affected_qualification(path, expected):
    root = Path(__file__).resolve().parents[2]
    policy = json.loads((root / "scripts/qualification_policy.json").read_text())
    assert {entry["id"] for entry in select(policy, {path})} == expected


@pytest.mark.parametrize(
    "path",
    [
        "runner/leasing.py",
        "runner/reconciliation.py",
        "runner/retry.py",
        "runner/polling.py",
        "management/commands/django_ray_worker.py",
        "ray_job_protocol.py",
        "ray_job_request_storage.py",
        "conf/settings.py",
        "backends.py",
        "runtime/entrypoint.py",
    ],
)
def test_cancellation_consumed_manager_and_request_paths_are_selected(path):
    root = Path(__file__).resolve().parents[2]
    policy = json.loads((root / "scripts/qualification_policy.json").read_text())
    assert "cancellation" in {item["id"] for item in select(policy, {"src/django_ray/" + path})}


@pytest.mark.parametrize(
    "path",
    [
        "src/django_ray/metrics.py",
        "testproject/api.py",
        "testproject/api_boundary.py",
        "testproject/route_policy.py",
        "testproject/settings.py",
        "testproject/urls.py",
    ],
)
def test_monitoring_selects_real_metrics_probe_routing_and_authentication_owners(path):
    root = Path(__file__).resolve().parents[2]
    policy = json.loads((root / "scripts/qualification_policy.json").read_text())
    assert "monitoring" in {entry["id"] for entry in select(policy, {path})}


@pytest.mark.parametrize(
    "path",
    [
        "qualification/platform_auth.py",
        "scripts/local_kuberay_auth.py",
        "tests/unit/test_monitoring_auth_orchestration.py",
        "tests/unit/test_platform_auth.py",
    ],
)
def test_isolated_auth_owner_changes_select_monitoring_without_another_fixture_change(path):
    root = Path(__file__).resolve().parents[2]
    policy = json.loads((root / "scripts/qualification_policy.json").read_text())
    assert (root / path).is_file()
    assert {item["id"] for item in select(policy, {path})} == {"monitoring"}


_APPLICATION_EXEMPT_PACKAGE_PATHS = frozenset(
    {
        "src/django_ray/doctor/__init__.py",
        "src/django_ray/doctor/__main__.py",
        "src/django_ray/doctor/contracts.py",
        "src/django_ray/doctor/probe.py",
        "src/django_ray/doctor/runner.py",
        "src/django_ray/doctor/supervisor.py",
        "src/django_ray/management/commands/django_ray_audit_workflow_progress.py",
        "src/django_ray/management/commands/django_ray_benchmark_polling.py",
        "src/django_ray/management/commands/django_ray_benchmark_workflow_progress.py",
        "src/django_ray/management/commands/django_ray_cleanup_workflow_progress.py",
        "src/django_ray/management/commands/django_ray_doctor.py",
        "src/django_ray/management/commands/django_ray_purge_inputs.py",
        "src/django_ray/management/purge_cursor.py",
        "src/django_ray/runtime/compiled_graph_probe.py",
        "src/django_ray/target/capabilities.py",
        "src/django_ray/target/coordination.py",
        "src/django_ray/target/routing.py",
        "src/django_ray/tracing.py",
    }
)


def _django_ray_imports(path: Path, known_modules: dict[str, str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    raw: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("django_ray"):
                    raw.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("django_ray"):
                raw.add(node.module)
                for alias in node.names:
                    candidate = f"{node.module}.{alias.name}"
                    if candidate in known_modules:
                        raw.add(candidate)
    resolved: set[str] = set()
    for module_name in raw:
        parts = module_name.split(".")
        for index in range(1, len(parts) + 1):
            prefix = ".".join(parts[:index])
            if prefix in known_modules:
                resolved.add(prefix)
    return resolved


def test_application_policy_classifies_every_package_module_explicitly():
    """Fail closed if a new package module is neither covered nor explicitly exempt."""
    root = Path(__file__).resolve().parents[2]
    policy = json.loads((root / "scripts/qualification_policy.json").read_text())
    application = next(entry for entry in policy if entry["id"] == "application")

    # Every exact path declared in application.paths must exist in the repository.
    for pattern in application["paths"]:
        if not pattern.endswith("/**"):
            assert (root / pattern).is_file(), pattern

    # Every source and packaged asset file under src/django_ray must be either
    # selected by application qualification or explicitly listed as offline/dormant.
    package_files = {
        path.relative_to(root).as_posix()
        for path in (root / "src/django_ray").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }
    selected = {path for path in package_files if select([application], {path})}
    exempt = {path for path in _APPLICATION_EXEMPT_PACKAGE_PATHS if (root / path).is_file()}
    assert exempt == _APPLICATION_EXEMPT_PACKAGE_PATHS
    assert selected.isdisjoint(exempt)
    assert selected | exempt == package_files
    assert "src/django_ray/static/django_ray/admin/task_live.js" in selected
    assert "src/django_ray/static/django_ray/admin/workflow_diagnostics.js" in selected
    assert "src/django_ray/templates/admin/django_ray/raytaskexecution/change_form.html" in selected

    # Prove no exempt module is transitively imported by deployed runtime paths.
    src_root = root / "src"
    known_modules: dict[str, str] = {}
    for path in (src_root / "django_ray").rglob("*.py"):
        parts = list(path.relative_to(src_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        known_modules[".".join(parts)] = path.relative_to(root).as_posix()

    seeds = {
        "django_ray",
        "django_ray.admin",
        "django_ray.apps",
        "django_ray.backends",
        "django_ray.models",
        "django_ray.workflows",
        "django_ray.runtime.entrypoint",
        "django_ray.management.commands.django_ray_worker",
        "django_ray.management.commands.django_ray_worker_ready",
        "django_ray.management.commands.django_ray_lease_policy",
        "django_ray.management.commands.django_ray_protocol_status",
        "django_ray.management.commands.django_ray_runtime_env",
    }
    for extra in (
        *(root / "testproject").rglob("*.py"),
        *(root / "qualification/application").rglob("*.py"),
    ):
        seeds |= _django_ray_imports(extra, known_modules)

    reachable_modules: set[str] = set()
    stack = list(seeds & set(known_modules))
    while stack:
        current = stack.pop()
        if current in reachable_modules:
            continue
        reachable_modules.add(current)
        for dependency in _django_ray_imports(root / known_modules[current], known_modules):
            if dependency not in reachable_modules:
                stack.append(dependency)

    reachable_paths = {known_modules[module] for module in reachable_modules}
    assert reachable_paths.isdisjoint(exempt), sorted(reachable_paths & exempt)


def test_only_selected_success_and_explicit_unselected_skip_pass():
    assert verify(results()) == {
        "application": "passed",
        "native": "not_applicable",
        "latency": "passed",
        "cancellation": "passed",
        "monitoring": "passed",
    }


@pytest.mark.parametrize("name", ["application", "latency", "cancellation", "monitoring"])
@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", "", None])
def test_selected_missing_or_failed_result_never_passes(name, state):
    value = results()
    value[name]["result"] = state
    with pytest.raises(GateError):
        verify(value)


@pytest.mark.parametrize("state", ["success", "failure", "cancelled", None])
def test_unselected_job_must_be_skipped(state):
    value = results()
    value["native"]["result"] = state
    with pytest.raises(GateError):
        verify(value)


@pytest.mark.parametrize(
    "change", ["missing", "extra", "selection_failed", "missing_output", "invalid_output"]
)
def test_incomplete_selection_or_dependency_inventory_fails(change):
    value = results()
    if change == "missing":
        del value["native"]
    elif change == "extra":
        value["unexpected"] = {"result": "success"}
    elif change == "selection_failed":
        value["selection"]["result"] = "failure"
    elif change == "missing_output":
        del value["selection"]["outputs"]["native"]
    else:
        value["selection"]["outputs"]["native"] = "unknown"
    with pytest.raises(GateError):
        verify(value)


def test_rename_includes_original_and_current_paths():
    def api(endpoint):
        if "/files?" in endpoint:
            return [{"filename": "docs/new.md", "previous_filename": "src/old.py"}]
        return {"state": "open", "head": {"sha": SHA}, "changed_files": 1}

    assert changed_files(api, "owner/repo", 2, SHA) == {"docs/new.md", "src/old.py"}


@pytest.mark.parametrize("change", ["stale", "closed", "oversized", "missing_page"])
def test_changed_file_inventory_fails_closed(change):
    pr = {"state": "open", "head": {"sha": SHA}, "changed_files": 1}
    if change == "stale":
        pr["head"]["sha"] = "b" * 40
    elif change == "closed":
        pr["state"] = "closed"
    elif change == "oversized":
        pr["changed_files"] = 3001

    def api(endpoint):
        return [] if "/files?" in endpoint else deepcopy(pr)

    with pytest.raises(GateError):
        changed_files(api, "owner/repo", 2, SHA)
