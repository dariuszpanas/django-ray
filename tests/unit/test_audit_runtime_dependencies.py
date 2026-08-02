"""Contracts for the locked runtime dependency advisory audit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import audit_runtime_dependencies as audit


def _write_test_project(project_root: Path) -> None:
    (project_root / "pyproject.toml").write_text(
        """\
[project]
name = "django-ray"
dependencies = ["django>=6.0", "pyasn1>=0.6.4", "ray[default]>=2.53.0"]

[project.optional-dependencies]
s3 = ["boto3>=1.34"]

[dependency-groups]
dev = ["pip-audit==2.10.1", "pytest>=8.3.3"]
""",
        encoding="utf-8",
    )


def _write_valid_export(requirements_path: Path) -> None:
    requirements_path.write_text(
        """\
boto3==1.34.0 \\
    --hash=sha256:aaa
django==6.0 \\
    --hash=sha256:bbb
pyasn1==0.6.4 \\
    --hash=sha256:ccc
ray==2.53.0 \\
    --hash=sha256:ddd
""",
        encoding="utf-8",
    )


def _write_valid_sbom(sbom_path: Path) -> None:
    sbom_path.write_text(
        json.dumps(
            {
                "components": [
                    {"name": "boto3", "version": "1.34.0"},
                    {"name": "django", "version": "6.0"},
                    {"name": "pyasn1", "version": "0.6.4"},
                    {"name": "ray", "version": "2.53.0"},
                ]
            }
        ),
        encoding="utf-8",
    )


def test_audit_exports_only_the_locked_runtime_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[list[str], dict[str, Any]]] = []
    _write_test_project(tmp_path)
    monkeypatch.setattr(
        audit.importlib.metadata,
        "version",
        lambda package: audit.PIP_AUDIT_VERSION if package == "pip-audit" else "unexpected",
    )

    def run(command: list[str], **options: Any) -> object:
        commands.append((command, options))
        if command[:2] == ["uv", "export"]:
            output = Path(command[command.index("--output-file") + 1])
            export_format = command[command.index("--format") + 1]
            if export_format == "requirements.txt":
                _write_valid_export(output)
            else:
                assert export_format == "cyclonedx1.5"
                _write_valid_sbom(output)
        return object()

    audit.audit_runtime_dependencies(project_root=tmp_path, run=run)

    assert len(commands) == 3
    export, export_options = commands[0]
    assert export[:2] == ["uv", "export"]
    assert {
        "--locked",
        "--no-dev",
        "--all-extras",
        "--no-emit-project",
        "--quiet",
    }.issubset(export)
    assert export[export.index("--format") + 1] == "requirements.txt"
    requirements = Path(export[export.index("--output-file") + 1])
    assert requirements.name == "runtime-requirements.txt"
    assert export_options == {"cwd": tmp_path, "check": True}

    sbom_export, sbom_options = commands[1]
    assert sbom_export[:2] == ["uv", "export"]
    assert {
        "--locked",
        "--no-dev",
        "--all-extras",
        "--no-emit-project",
        "--quiet",
    }.issubset(sbom_export)
    assert sbom_export[sbom_export.index("--format") + 1] == "cyclonedx1.5"
    assert Path(sbom_export[sbom_export.index("--output-file") + 1]).name == ("runtime-sbom.json")
    assert sbom_options == {"cwd": tmp_path, "check": True}

    scanner, scanner_options = commands[2]
    assert scanner[:3] == [sys.executable, "-m", "pip_audit"]
    assert {
        "--strict",
        "--require-hashes",
        "--disable-pip",
    }.issubset(scanner)
    assert scanner[scanner.index("--vulnerability-service") + 1] == "pypi"
    assert scanner[scanner.index("--progress-spinner") + 1] == "off"
    assert scanner[scanner.index("--timeout") + 1] == "30"
    assert Path(scanner[scanner.index("--cache-dir") + 1]).name == "pip-audit-cache"
    assert Path(scanner[scanner.index("--requirement") + 1]) == requirements
    assert scanner_options == {"cwd": tmp_path, "check": True}


def test_audit_requires_the_repository_pinned_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit.importlib.metadata, "version", lambda package: "2.10.0")

    with pytest.raises(RuntimeError, match=r"pip-audit==2\.10\.1, found 2\.10\.0"):
        audit.audit_runtime_dependencies(
            project_root=tmp_path,
            run=lambda *args, **kwargs: pytest.fail("version mismatch must fail before export"),
        )


def test_audit_propagates_scanner_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_test_project(tmp_path)
    monkeypatch.setattr(
        audit.importlib.metadata,
        "version",
        lambda package: audit.PIP_AUDIT_VERSION,
    )
    calls = 0

    def run(command: list[str], **options: Any) -> object:
        nonlocal calls
        calls += 1
        if command[:2] == ["uv", "export"]:
            output = Path(command[command.index("--output-file") + 1])
            if command[command.index("--format") + 1] == "requirements.txt":
                _write_valid_export(output)
            else:
                _write_valid_sbom(output)
        if calls == 3:
            raise subprocess.CalledProcessError(1, command)
        return object()

    with pytest.raises(subprocess.CalledProcessError):
        audit.audit_runtime_dependencies(project_root=tmp_path, run=run)

    assert calls == 3


@pytest.mark.parametrize(
    ("exported", "message"),
    [
        ("", "runtime dependency export is empty"),
        (
            "django==6.0 \\\n    --hash=sha256:bbb\n",
            "missing project runtime roots",
        ),
        (
            "boto3==1.34.0 \\\n    --hash=sha256:aaa\n"
            "django==6.0 \\\n    --hash=sha256:bbb\n"
            "django-ray==0.4.0 \\\n    --hash=sha256:eee\n"
            "pyasn1==0.6.4 \\\n    --hash=sha256:ccc\n"
            "ray==2.53.0 \\\n    --hash=sha256:ddd\n",
            "must not include the django-ray project",
        ),
        (
            "boto3==1.34.0 \\\n    --hash=sha256:aaa\n"
            "django==6.0 \\\n    --hash=sha256:bbb\n"
            "pip-audit==2.10.1 \\\n    --hash=sha256:fff\n"
            "pyasn1==0.6.4 \\\n    --hash=sha256:ccc\n"
            "ray==2.53.0 \\\n    --hash=sha256:ddd\n",
            "must not include the development audit tool",
        ),
        (
            "boto3==1.34.0 \\\n    --hash=sha256:aaa\n"
            "django==6.0 \\\n    --hash=sha256:bbb\n"
            "pyasn1==0.6.4\n"
            "ray==2.53.0 \\\n    --hash=sha256:ddd\n",
            "missing hashes for 'pyasn1'",
        ),
    ],
)
def test_audit_rejects_incomplete_or_contaminated_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exported: str,
    message: str,
) -> None:
    _write_test_project(tmp_path)
    monkeypatch.setattr(
        audit.importlib.metadata,
        "version",
        lambda package: audit.PIP_AUDIT_VERSION,
    )
    calls = 0

    def run(command: list[str], **options: Any) -> object:
        nonlocal calls
        calls += 1
        assert command[:2] == ["uv", "export"]
        Path(command[command.index("--output-file") + 1]).write_text(exported, encoding="utf-8")
        return object()

    with pytest.raises(RuntimeError, match=message):
        audit.audit_runtime_dependencies(project_root=tmp_path, run=run)

    assert calls == 1


def test_audit_rejects_a_hashed_export_missing_a_locked_transitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_test_project(tmp_path)
    monkeypatch.setattr(
        audit.importlib.metadata,
        "version",
        lambda package: audit.PIP_AUDIT_VERSION,
    )
    calls = 0

    def run(command: list[str], **options: Any) -> object:
        nonlocal calls
        calls += 1
        output = Path(command[command.index("--output-file") + 1])
        if command[command.index("--format") + 1] == "requirements.txt":
            _write_valid_export(output)
        else:
            _write_valid_sbom(output)
            sbom = json.loads(output.read_text(encoding="utf-8"))
            sbom["components"].append({"name": "urllib3", "version": "2.6.3"})
            output.write_text(json.dumps(sbom), encoding="utf-8")
        return object()

    with pytest.raises(
        RuntimeError,
        match=r"exports disagree: missing from hashed requirements: urllib3==2\.6\.3",
    ):
        audit.audit_runtime_dependencies(project_root=tmp_path, run=run)

    assert calls == 2


@pytest.mark.parametrize(
    "sbom",
    [
        "not-json",
        json.dumps({"components": []}),
        json.dumps({"components": [{"name": "django"}]}),
    ],
)
def test_audit_rejects_an_invalid_locked_sbom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sbom: str,
) -> None:
    _write_test_project(tmp_path)
    monkeypatch.setattr(
        audit.importlib.metadata,
        "version",
        lambda package: audit.PIP_AUDIT_VERSION,
    )

    def run(command: list[str], **options: Any) -> object:
        output = Path(command[command.index("--output-file") + 1])
        if command[command.index("--format") + 1] == "requirements.txt":
            _write_valid_export(output)
        else:
            output.write_text(sbom, encoding="utf-8")
        return object()

    with pytest.raises(RuntimeError, match="locked runtime SBOM export"):
        audit.audit_runtime_dependencies(project_root=tmp_path, run=run)
