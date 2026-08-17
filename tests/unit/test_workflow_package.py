"""Workflow package structure and cold-import contracts."""

from __future__ import annotations

import ast
import importlib
import pickle
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
REMOVED_WORKFLOW_IMPORTS = (
    "django_ray.admin_workflow_graph",
    "django_ray.workflow._compat",
    "django_ray.workflow_output_previews",
    "django_ray.workflow_plans",
    "django_ray.workflow_progress",
    "django_ray.workflow_progress_cleanup",
    "django_ray.workflow_progress_limits",
    "django_ray.workflow_progress_preparation",
    "django_ray.workflow_progress_producer",
    "django_ray.workflow_progress_protocol",
    "django_ray.workflow_progress_publication",
    "django_ray.workflow_progress_reads",
    "django_ray.workflow_progress_storage",
    "django_ray.workflow_progress_summary",
)
WORKFLOW_MODULES = {
    "__init__.py",
    "admin_graph.py",
    "contracts.py",
    "plans.py",
    "previews.py",
}
WORKFLOW_PROGRESS_MODULES = {
    "__init__.py",
    "cleanup.py",
    "limits.py",
    "preparation.py",
    "producer.py",
    "protocol.py",
    "publication.py",
    "reads.py",
    "runs.py",
    "storage.py",
    "summary.py",
}
PUBLIC_WORKFLOW_FACADE_SYMBOLS = (
    "Chain",
    "Group",
    "Map",
    "Step",
    "WorkflowDefinitionError",
    "WorkflowSignature",
    "chain",
    "group",
    "map_step",
    "report_progress",
    "step",
)


def test_workflow_private_modules_are_grouped_under_canonical_packages() -> None:
    source_root = ROOT / "src" / "django_ray"
    workflow_root = source_root / "workflow"

    assert not (source_root / "admin_workflow_graph.py").exists()
    assert {path.name for path in source_root.glob("workflow*.py")} == {"workflows.py"}
    assert {path.name for path in workflow_root.glob("*.py")} == WORKFLOW_MODULES
    assert {
        path.name for path in (workflow_root / "progress").glob("*.py")
    } == WORKFLOW_PROGRESS_MODULES


def test_public_workflow_facade_keeps_defining_module_identity() -> None:
    workflows = importlib.import_module("django_ray.workflows")

    for name in PUBLIC_WORKFLOW_FACADE_SYMBOLS:
        assert getattr(workflows, name).__module__ == "django_ray.workflows"

    serialized = pickle.dumps(workflows.Step)
    assert b"django_ray.workflows" in serialized
    assert pickle.loads(serialized) is workflows.Step


def test_workflow_package_initializers_are_inert() -> None:
    source_root = ROOT / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source_root)!r})
import django_ray.workflow
import django_ray.workflow.progress

allowed = {{"django_ray.workflow", "django_ray.workflow.progress"}}
unexpected = sorted(
    name
    for name in sys.modules
    if (
        name.startswith("django_ray.workflow.")
        and name not in allowed
    )
    or name == "django"
    or name.startswith("django.")
    or name == "ray"
    or name.startswith("ray.")
)
if unexpected:
    raise SystemExit(f"workflow package imported cold-boundary modules: {{unexpected}}")
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_pure_workflow_foundation_modules_do_not_import_django_or_ray() -> None:
    source_root = ROOT / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source_root)!r})
import django_ray.workflow.previews
import django_ray.workflow.progress.limits

unexpected = sorted(
    name
    for name in sys.modules
    if name == "django"
    or name.startswith("django.")
    or name == "ray"
    or name.startswith("ray.")
)
if unexpected:
    raise SystemExit(f"workflow foundation imported dependencies: {{unexpected}}")
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_workflow_progress_summary_does_not_import_ray() -> None:
    source_root = ROOT / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source_root)!r})
import django_ray.workflow.progress.summary

unexpected = sorted(
    name
    for name in sys.modules
    if name == "ray" or name.startswith("ray.")
)
if unexpected:
    raise SystemExit(f"workflow progress summary imported Ray: {{unexpected}}")
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_repository_does_not_use_removed_workflow_imports() -> None:
    paths = [
        *sorted((ROOT / "src" / "django_ray").rglob("*.py")),
        *sorted((ROOT / "scripts").rglob("*.py")),
        *sorted((ROOT / "testproject").rglob("*.py")),
        *sorted((ROOT / "tests").rglob("*.py")),
    ]

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = {
            name.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for name in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        imported_modules.update(
            f"django_ray.{name.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "django_ray"
            for name in node.names
        )
        for removed_import in REMOVED_WORKFLOW_IMPORTS:
            assert removed_import not in imported_modules, (
                f"{path.relative_to(ROOT)} imports removed private module {removed_import}"
            )
