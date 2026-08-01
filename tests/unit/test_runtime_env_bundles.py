"""Tests for deterministic testproject RuntimeEnv archives."""

from __future__ import annotations

import os
from importlib import metadata
from pathlib import Path
from zipfile import ZipFile

import pytest

from testproject.runtime_env_bundles import (
    RECOVERY_BUNDLE_MAX_BYTES,
    RECOVERY_DISTRIBUTIONS,
    RECOVERY_REQUIRED_MEMBERS,
    build_recovery_bundle,
    build_source_bundle,
)

ROOT = Path(__file__).resolve().parents[2]


def test_source_bundle_is_content_deterministic(tmp_path: Path) -> None:
    base = tmp_path / "source"
    remote = base / "src" / "django_ray" / "runtime" / "remote.py"
    workflow = base / "testproject" / "apps" / "cluster_tasks" / "workflows.py"
    remote.parent.mkdir(parents=True)
    workflow.parent.mkdir(parents=True)
    remote.write_text("VALUE = 1\n", encoding="utf-8")
    workflow.write_text("VALUE = 2\n", encoding="utf-8")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    build_source_bundle(base=base, target=first)
    os.utime(remote, None)
    build_source_bundle(base=base, target=second)

    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        names = set(archive.namelist())
    assert "src/django_ray/runtime/remote.py" in names
    assert "testproject/apps/cluster_tasks/workflows.py" in names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_recovery_bundle_contains_locked_task_runtime(tmp_path: Path) -> None:
    missing = []
    for name in RECOVERY_DISTRIBUTIONS:
        try:
            metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    if missing:
        pytest.skip(f"sample image dependency extras are not installed: {missing}")
    target = tmp_path / "recovery.zip"

    build_recovery_bundle(base=ROOT, target=target)

    assert 0 < target.stat().st_size <= RECOVERY_BUNDLE_MAX_BYTES
    with ZipFile(target) as archive:
        names = set(archive.namelist())
    assert RECOVERY_REQUIRED_MEMBERS <= names
    assert "src/django_ray/runtime/remote.py" not in names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
