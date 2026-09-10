"""Resource-free checks of disposable source preparation and image evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from testing.linux import image_support


def test_source_copy_requires_a_new_directory_and_preserves_bytes(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "workspace"
    source.mkdir()
    (source / "test.py").write_bytes(b"exact source\n")
    image_support.prepare_directory(source, destination)
    assert (destination / "test.py").read_bytes() == b"exact source\n"
    with pytest.raises(ValueError, match="empty real directory"):
        image_support.prepare_directory(source, destination)


def test_runtime_repository_is_materialized_from_the_copied_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[list[str], Path]] = []

    def run(command: list[str], *, cwd: Path, check: bool) -> None:
        calls.append((command, cwd))

    monkeypatch.setattr(image_support.subprocess, "run", run)
    image_support.initialize_runtime_repository(workspace)

    assert [command for command, _ in calls] == [
        ["git", "init", "--quiet"],
        ["git", "config", "user.name", "linux-test-runner"],
        ["git", "config", "user.email", "linux-test-runner@localhost"],
        ["git", "add", "--all"],
        [
            "git",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--quiet",
            "-m",
            "sealed candidate archive",
        ],
    ]
    assert all(cwd == workspace for _, cwd in calls)


def test_entrypoint_binds_manifest_and_keeps_cache_outside_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, workspace, cache = tmp_path / "source", tmp_path / "workspace", tmp_path / "cache"
    source.mkdir()
    cache.mkdir()
    (source / "candidate.txt").write_text("source", encoding="utf-8")
    monkeypatch.setattr(image_support, "SOURCE", source)
    monkeypatch.setattr(image_support, "WORKSPACE", workspace)
    monkeypatch.setattr(image_support, "CACHE", cache)
    monkeypatch.setattr(image_support, "INPUTS", tmp_path / "inputs")
    monkeypatch.setattr(image_support, "WRITABLE_CACHE", tmp_path / "writable-cache")
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.chdir(tmp_path)
    called = []
    monkeypatch.setattr(image_support.os, "execv", lambda *args: called.append(args))
    image_support.execute(["run", "--stage", "hermetic", "--output-dir", "/evidence/run"])
    assert called[0][1][-2:] == ["--source-manifest", str(tmp_path / "inputs/source-manifest.json")]
    assert Path(image_support.os.environ["UV_CACHE_DIR"]).parent == tmp_path
    assert (workspace / "candidate.txt").read_text(encoding="utf-8") == "source"


@pytest.mark.parametrize(
    "arguments", [[], ["_execute"], ["catalogue", "--source-manifest", "other.json"]]
)
def test_entrypoint_rejects_unbound_commands_and_manifest_replacement(arguments: list[str]) -> None:
    with pytest.raises(ValueError):
        image_support.execute(arguments)


def test_image_receipt_records_observed_tools_and_exact_build_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "backend.whl").write_bytes(b"wheel")
    monkeypatch.setattr(image_support, "CACHE", cache)
    monkeypatch.setattr(image_support, "INPUTS", tmp_path)
    for name in ("source.tar", "source-manifest.json", "build-constraints.txt"):
        (tmp_path / name).write_bytes(name.encode())
    for name in ("PYTHON_IMAGE", "NODE_IMAGE", "UV_IMAGE"):
        monkeypatch.setenv(name, "example/image@sha256:" + "a" * 64)
    monkeypatch.setenv("DEBIAN_SNAPSHOT", "20260910T000000Z")
    monkeypatch.setattr(image_support.subprocess, "check_output", lambda *args, **kwargs: "tool\n")
    output = tmp_path / "environment.json"
    image_support.record_environment(output)
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["execution"] == "not_run"
    assert receipt["build_cache_bytes"] == 5
    assert receipt["inputs"]["source.tar"] == hashlib.sha256(b"source.tar").hexdigest()
    assert receipt["tools"]["kubectl"] == "tool"
    monkeypatch.setattr(image_support, "MAX_BUILD_CACHE_BYTES", 4)
    with pytest.raises(ValueError, match="128 MiB"):
        image_support.record_environment(tmp_path / "oversized.json")
