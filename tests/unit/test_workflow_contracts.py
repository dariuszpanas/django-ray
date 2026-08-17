"""Internal workflow dependency-boundary tests."""

from __future__ import annotations

import ast
from pathlib import Path


def test_plan_compiler_does_not_import_public_workflow_facade() -> None:
    """Planning depends on the leaf contract, not concrete public builders."""
    root = Path(__file__).parents[2]
    path = root / "src" / "django_ray" / "workflow" / "plans.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert "django_ray.workflows" not in imported_modules
