"""Workflow package structure and compatibility contracts."""

from __future__ import annotations

import ast
import importlib
import inspect
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
LEGACY_WORKFLOW_MODULES = (
    "django_ray.workflow_output_previews",
    "django_ray.workflow_progress_limits",
    "django_ray.workflow_progress_summary",
)
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


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    [
        ("django_ray.workflow_output_previews", "django_ray.workflow.previews"),
        (
            "django_ray.workflow_progress_limits",
            "django_ray.workflow.progress.limits",
        ),
        (
            "django_ray.workflow_progress_summary",
            "django_ray.workflow.progress.summary",
        ),
    ],
)
def test_legacy_workflow_modules_export_canonical_symbols_with_pickle_identity(
    legacy_name: str,
    canonical_name: str,
) -> None:
    canonical = importlib.import_module(canonical_name)
    legacy = importlib.import_module(legacy_name)

    assert legacy.__all__ == canonical.__all__
    defined_exports = []
    for name in canonical.__all__:
        canonical_value = getattr(canonical, name)
        assert getattr(legacy, name) is canonical_value
        if inspect.isclass(canonical_value) or inspect.isfunction(canonical_value):
            assert canonical_value.__module__ == legacy_name
            defined_exports.append(name)
            serialized = pickle.dumps(canonical_value)
            assert legacy_name.encode() in serialized
            assert pickle.loads(serialized) is canonical_value

    assert defined_exports


def test_workflow_progress_limits_instance_keeps_legacy_pickle_identity() -> None:
    limits = importlib.import_module("django_ray.workflow.progress.limits")

    serialized = pickle.dumps(limits.WORKFLOW_PROGRESS_LIMITS_V1)

    assert b"django_ray.workflow_progress_limits" in serialized
    assert pickle.loads(serialized) == limits.WORKFLOW_PROGRESS_LIMITS_V1


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


def test_repository_imports_canonical_workflow_foundation_modules() -> None:
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
        for legacy_module in LEGACY_WORKFLOW_MODULES:
            assert legacy_module not in imported_modules, (
                f"{path.relative_to(ROOT)} imports compatibility module {legacy_module}"
            )
