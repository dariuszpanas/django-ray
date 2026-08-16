"""Target package structure and compatibility contracts."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
LEGACY_TARGET_IMPORTS = (
    "django_ray.ray_target_probe",
    "django_ray.target_attestation",
    "django_ray.target_capabilities",
    "django_ray.target_coordination",
    "django_ray.target_execution_codec",
    "django_ray.target_execution_evidence",
    "django_ray.target_routing",
)


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    [
        ("django_ray.target_attestation", "django_ray.target.attestation"),
        ("django_ray.target_capabilities", "django_ray.target.capabilities"),
        ("django_ray.target_coordination", "django_ray.target.coordination"),
        ("django_ray.target_execution_codec", "django_ray.target.execution_codec"),
        (
            "django_ray.target_execution_evidence",
            "django_ray.target.execution_evidence",
        ),
        ("django_ray.ray_target_probe", "django_ray.target.probe"),
        ("django_ray.target_routing", "django_ray.target.routing"),
    ],
)
def test_legacy_target_modules_export_canonical_public_symbols(
    legacy_name: str,
    canonical_name: str,
) -> None:
    legacy = importlib.import_module(legacy_name)
    canonical = importlib.import_module(canonical_name)

    assert legacy.__all__ == canonical.__all__
    for name in canonical.__all__:
        assert getattr(legacy, name) is getattr(canonical, name)


def test_target_package_import_is_inert() -> None:
    source_root = ROOT / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source_root)!r})
import django_ray.target

unexpected = sorted(
    name
    for name in sys.modules
    if name.startswith("django_ray.target.") or name == "ray"
)
if unexpected:
    raise SystemExit(f"target package imported cold-boundary modules: {{unexpected}}")
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_pure_target_modules_do_not_import_django_or_ray() -> None:
    source_root = ROOT / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source_root)!r})
import django_ray.target.attestation
import django_ray.target.execution_evidence
import django_ray.target.probe

unexpected = sorted(
    name
    for name in sys.modules
    if name == "django"
    or name.startswith("django.")
    or name == "ray"
    or name.startswith("ray.")
)
if unexpected:
    raise SystemExit(f"pure target modules imported dependencies: {{unexpected}}")
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_package_and_scripts_use_canonical_target_imports() -> None:
    paths = [
        *sorted((ROOT / "src" / "django_ray").rglob("*.py")),
        *sorted((ROOT / "scripts").glob("*.py")),
    ]

    for path in paths:
        contents = path.read_text(encoding="utf-8")
        for legacy_import in LEGACY_TARGET_IMPORTS:
            assert legacy_import not in contents, (
                f"{path.relative_to(ROOT)} imports the compatibility module {legacy_import}"
            )
