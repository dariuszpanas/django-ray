"""Audit the exact locked runtime graph against current PyPI advisories."""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Protocol

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIP_AUDIT_VERSION = "2.10.1"


class CommandRunner(Protocol):
    """Callable boundary used to keep command construction hermetically testable."""

    def __call__(self, command: list[str], *, cwd: Path, check: bool) -> object: ...


def _run_command(command: list[str], *, cwd: Path, check: bool) -> object:
    return subprocess.run(command, cwd=cwd, check=check)


def _project_runtime_roots(project_root: Path) -> tuple[str, set[str]]:
    with (project_root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    project_name = canonicalize_name(project["name"])
    requirements = list(project.get("dependencies", ()))
    for extra_requirements in project.get("optional-dependencies", {}).values():
        requirements.extend(extra_requirements)
    return project_name, {canonicalize_name(Requirement(value).name) for value in requirements}


def _validate_exported_graph(
    requirements_path: Path, *, project_root: Path
) -> set[tuple[str, str]]:
    """Reject empty, incomplete, unpinned, or development-contaminated exports."""
    exported_names: set[str] = set()
    exported_packages: set[tuple[str, str]] = set()
    current_requirement: str | None = None
    current_has_hash = False

    def finish_requirement() -> None:
        nonlocal current_requirement, current_has_hash
        if current_requirement is None:
            return
        try:
            requirement = Requirement(current_requirement)
        except InvalidRequirement as error:
            raise RuntimeError(
                f"runtime dependency export contains an invalid requirement: {current_requirement!r}"
            ) from error
        specifiers = list(requirement.specifier)
        if requirement.url is not None or len(specifiers) != 1:
            raise RuntimeError(
                f"runtime dependency export is not exactly pinned: {current_requirement!r}"
            )
        specifier = specifiers[0]
        if specifier.operator != "==" or specifier.version.endswith(".*"):
            raise RuntimeError(
                f"runtime dependency export is not exactly pinned: {current_requirement!r}"
            )
        if not current_has_hash:
            raise RuntimeError(
                f"runtime dependency export is missing hashes for {requirement.name!r}"
            )
        canonical_name = canonicalize_name(requirement.name)
        exported_names.add(canonical_name)
        exported_packages.add((canonical_name, specifier.version))
        current_requirement = None
        current_has_hash = False

    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[0].isspace():
            if current_requirement is not None and stripped.startswith("--hash="):
                current_has_hash = True
            continue
        finish_requirement()
        current_has_hash = "--hash=" in stripped
        current_requirement = stripped.removesuffix("\\").strip()
    finish_requirement()

    if not exported_names:
        raise RuntimeError("runtime dependency export is empty")

    project_name, runtime_roots = _project_runtime_roots(project_root)
    missing_roots = sorted(runtime_roots - exported_names)
    if missing_roots:
        raise RuntimeError(
            "runtime dependency export is missing project runtime roots: "
            + ", ".join(missing_roots)
        )
    if project_name in exported_names:
        raise RuntimeError("runtime dependency export must not include the django-ray project")
    auditor_name = canonicalize_name("pip-audit")
    if auditor_name not in runtime_roots and auditor_name in exported_names:
        raise RuntimeError("runtime dependency export must not include the development audit tool")
    return exported_packages


def _validate_sbom_graph(sbom_path: Path, *, exported_packages: set[tuple[str, str]]) -> None:
    """Cross-check the hashed requirements against uv's locked SBOM export."""
    try:
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError("locked runtime SBOM export is unreadable") from error

    components = sbom.get("components") if isinstance(sbom, dict) else None
    if not isinstance(components, list) or not components:
        raise RuntimeError("locked runtime SBOM export has no components")

    sbom_packages: set[tuple[str, str]] = set()
    for component in components:
        if not isinstance(component, dict):
            raise RuntimeError("locked runtime SBOM export has an invalid component")
        name = component.get("name")
        version = component.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise RuntimeError("locked runtime SBOM export has an invalid component identity")
        sbom_packages.add((canonicalize_name(name), version))

    if sbom_packages != exported_packages:
        missing = sorted(sbom_packages - exported_packages)
        unexpected = sorted(exported_packages - sbom_packages)
        details: list[str] = []
        if missing:
            details.append(
                "missing from hashed requirements: "
                + ", ".join(f"{name}=={version}" for name, version in missing)
            )
        if unexpected:
            details.append(
                "missing from locked SBOM: "
                + ", ".join(f"{name}=={version}" for name, version in unexpected)
            )
        raise RuntimeError("runtime dependency exports disagree: " + "; ".join(details))


def audit_runtime_dependencies(
    *,
    project_root: Path = PROJECT_ROOT,
    run: CommandRunner = _run_command,
) -> None:
    """Export only locked runtime dependencies and fail on known vulnerabilities."""
    installed_version = importlib.metadata.version("pip-audit")
    if installed_version != PIP_AUDIT_VERSION:
        raise RuntimeError(
            "runtime dependency audit requires the repository-pinned "
            f"pip-audit=={PIP_AUDIT_VERSION}, found {installed_version}"
        )

    with tempfile.TemporaryDirectory(prefix="django-ray-runtime-audit-") as temporary:
        temporary_path = Path(temporary)
        requirements_path = temporary_path / "runtime-requirements.txt"
        sbom_path = temporary_path / "runtime-sbom.json"
        cache_path = temporary_path / "pip-audit-cache"
        run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--all-extras",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--quiet",
                "--output-file",
                str(requirements_path),
            ],
            cwd=project_root,
            check=True,
        )
        exported_packages = _validate_exported_graph(requirements_path, project_root=project_root)
        run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--all-extras",
                "--no-emit-project",
                "--format",
                "cyclonedx1.5",
                "--quiet",
                "--output-file",
                str(sbom_path),
            ],
            cwd=project_root,
            check=True,
        )
        _validate_sbom_graph(sbom_path, exported_packages=exported_packages)
        run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--strict",
                "--require-hashes",
                "--disable-pip",
                "--vulnerability-service",
                "pypi",
                "--progress-spinner",
                "off",
                "--timeout",
                "30",
                "--cache-dir",
                str(cache_path),
                "--requirement",
                str(requirements_path),
            ],
            cwd=project_root,
            check=True,
        )


def main() -> int:
    """Run the repository audit command."""
    audit_runtime_dependencies()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
