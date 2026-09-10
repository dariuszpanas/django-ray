"""Git-free archive identities retain the existing taxonomy digest semantics."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import test_suite_inventory as inventory
from scripts import test_suite_source as source
from scripts.test_suite_taxonomy import InventoryError
from tests.unit.test_test_suite_inventory import _inventory_cli, _mini_inventory_repository


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True).stdout


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Source Test")
    _git(root, "config", "user.email", "source-test@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (root / ".github").mkdir()
    (root / source.DEFAULT_MANIFEST).write_text("{}\n", encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\n__pycache__/\n", encoding="utf-8")
    (root / "sample.py").write_bytes(b"answer = 42\n")
    (root / "binary.png").write_bytes(b"image\r\nbytes\x00")
    baseline = root / "docs/investigations/test-suite-baseline-2026-01-01.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("{}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "test: prepare miniature committed source")
    archive = tmp_path / "archive"
    archive.mkdir()
    with tarfile.open(
        fileobj=io.BytesIO(_git(root, "-c", "core.autocrlf=false", "archive", "HEAD"))
    ) as bundle:
        bundle.extractall(archive, filter="data")
    return root, archive, tmp_path / "source.json"


def _seal(candidate: tuple[Path, Path, Path]) -> dict[str, Any]:
    root, _, manifest = candidate
    return source.seal_source(root, manifest)


def test_sealed_archive_verifies_without_git_and_preserves_taxonomy_identity(
    candidate: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archive, manifest = candidate
    expected = source.source_digest(root, root / source.DEFAULT_MANIFEST)
    sealed = _seal(candidate)
    assert sealed["source_digest"] == expected
    assert len(sealed["files"]) == cast(int, expected["file_count"]) + 1

    def no_git(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("archive verification must not invoke Git")

    monkeypatch.setattr(source, "_git", no_git)
    assert source.verify_source(archive, manifest) == sealed
    assert source.source_digest(archive, archive / source.DEFAULT_MANIFEST, manifest) == expected
    assert (
        inventory._source_digest(archive, archive / source.DEFAULT_MANIFEST, manifest) == expected
    )
    assert source.main(["--root", str(archive), "verify", "--source-manifest", str(manifest)]) == 0


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "staged"])
def test_sealing_requires_a_clean_checkout(candidate: tuple[Path, Path, Path], dirty: str) -> None:
    root, _, manifest = candidate
    if dirty == "tracked":
        (root / "sample.py").write_text("changed\n", encoding="utf-8")
    else:
        (root / "additional.py").write_text("new\n", encoding="utf-8")
        if dirty == "staged":
            _git(root, "add", "additional.py")
    with pytest.raises(InventoryError, match="clean Git"):
        _seal(candidate)
    assert not manifest.exists()


def test_sealing_uses_committed_bytes_despite_checkout_line_endings(
    candidate: tuple[Path, Path, Path],
) -> None:
    root, archive, manifest = candidate
    _git(root, "config", "core.autocrlf", "true")
    (root / "sample.py").write_bytes(b"answer = 42\r\n")
    _git(root, "add", "sample.py")
    assert not _git(root, "status", "--porcelain")
    sealed = _seal(candidate)
    assert source.verify_source(archive, manifest) == sealed
    assert sealed["source_digest"] == source.source_digest(root, root / source.DEFAULT_MANIFEST)


@pytest.mark.parametrize("change", ["changed", "crlf", "missing", "extra", "baseline"])
def test_archive_rejects_changes_and_incomplete_membership(
    candidate: tuple[Path, Path, Path], change: str
) -> None:
    _, archive, manifest = candidate
    _seal(candidate)
    if change == "missing":
        (archive / "sample.py").unlink()
    elif change == "extra":
        (archive / "injected.py").write_bytes(b"extra\n")
    elif change == "baseline":
        (archive / "docs/investigations/test-suite-baseline-2026-01-01.json").write_bytes(
            b"changed\n"
        )
    else:
        (archive / "sample.py").write_bytes(
            b"answer = 42\r\n" if change == "crlf" else b"changed\n"
        )
    with pytest.raises(InventoryError, match="source"):
        source.verify_source(archive, manifest)


def test_runtime_outputs_do_not_become_source(candidate: tuple[Path, Path, Path]) -> None:
    _, archive, manifest = candidate
    sealed = _seal(candidate)
    for relative in (
        ".venv/lib/cache.py",
        ".cache/zensical-cache",
        "site/index.html",
        "docs/_build/index.html",
        "__pycache__/sample.pyc",
        "nested/__pycache__/test.pyc",
    ):
        path = archive / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated")
    (archive / ".coverage").write_bytes(b"coverage")
    assert source.verify_source(archive, manifest) == sealed


@pytest.mark.parametrize(
    "unsafe",
    [
        "../outside.py",
        "/absolute.py",
        "a//b.py",
        "a/./b.py",
        "a\\b.py",
        "C:sample.py",
        ".git/config",
    ],
)
def test_source_manifest_rejects_noncanonical_paths(
    candidate: tuple[Path, Path, Path], unsafe: str
) -> None:
    _, archive, manifest = candidate
    sealed = _seal(candidate)
    sealed["files"][0]["path"] = unsafe
    manifest.write_text(json.dumps(sealed), encoding="utf-8")
    with pytest.raises(InventoryError, match="canonical"):
        source.verify_source(archive, manifest)


def test_source_manifest_rejects_duplicate_paths_and_keys(
    candidate: tuple[Path, Path, Path],
) -> None:
    _, archive, manifest = candidate
    sealed = _seal(candidate)
    sealed["files"].append(sealed["files"][0])
    manifest.write_text(json.dumps(sealed), encoding="utf-8")
    with pytest.raises(InventoryError, match="unique"):
        source.verify_source(archive, manifest)
    manifest.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    with pytest.raises(InventoryError, match="cannot read"):
        source.verify_source(archive, manifest)


def test_omitting_a_file_cannot_claim_the_original_git_tree(
    candidate: tuple[Path, Path, Path],
) -> None:
    _, archive, manifest = candidate
    sealed = _seal(candidate)
    sealed["files"] = [entry for entry in sealed["files"] if entry["path"] != "sample.py"]
    (archive / "sample.py").unlink()
    contents = {entry["path"]: (archive / entry["path"]).read_bytes() for entry in sealed["files"]}
    sealed["source_digest"] = source._digest_record(contents)
    manifest.write_text(json.dumps(sealed), encoding="utf-8")
    with pytest.raises(InventoryError, match="committed Git tree"):
        source.verify_source(archive, manifest)


def test_source_mutation_during_sealing_is_rejected(
    candidate: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, manifest = candidate
    original = source._git

    def mutate(checkout: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
        result = original(checkout, *arguments, input_bytes=input_bytes)
        if arguments[0] == "cat-file":
            (root / "sample.py").write_bytes(b"changed\n")
        return result

    monkeypatch.setattr(source, "_git", mutate)
    with pytest.raises(InventoryError, match="clean Git"):
        _seal(candidate)
    assert not manifest.exists()


def test_symlink_source_is_rejected(candidate: tuple[Path, Path, Path]) -> None:
    _, archive, manifest = candidate
    _seal(candidate)
    (archive / "sample.py").unlink()
    try:
        (archive / "sample.py").symlink_to(manifest)
    except OSError:
        pytest.skip("host does not permit test-owned symlink creation")
    with pytest.raises(InventoryError, match="unsafe"):
        source.verify_source(archive, manifest)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not preserve Git executable modes")
def test_executable_mode_is_verified(candidate: tuple[Path, Path, Path]) -> None:
    _, archive, manifest = candidate
    _seal(candidate)
    (archive / "sample.py").chmod(0o755)
    with pytest.raises(InventoryError, match="executable mode"):
        source.verify_source(archive, manifest)


def test_manifest_and_evidence_outputs_must_stay_external(
    candidate: tuple[Path, Path, Path],
) -> None:
    root, archive, manifest = candidate
    with pytest.raises(InventoryError, match="outside"):
        source.seal_source(root, root / "source.json")
    _seal(candidate)
    with pytest.raises(InventoryError, match="outside"):
        inventory._validate_output_path(
            archive, Path("timing.json"), "timing", source_manifest=manifest
        )
    inventory._validate_output_path(
        archive, manifest.parent / "timing.json", "timing", source_manifest=manifest
    )
    parsed = inventory._parser().parse_args(
        [
            "--source-manifest",
            str(manifest),
            "collect",
            "--json-output",
            str(manifest.parent / "inventory.json"),
            "--markdown-output",
            str(manifest.parent / "inventory.md"),
        ]
    )
    assert parsed.source_manifest == manifest


def test_archive_inventory_cli_collects_runs_and_merges_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _mini_inventory_repository(tmp_path / "checkout")
    with (root / "tests/test_sample.py").open("a", encoding="utf-8") as stream:
        stream.write(
            "\ndef test_nested_source_identity():\n"
            "    from pathlib import Path\n"
            "    from scripts.test_suite_source import source_digest\n"
            "    root = Path(__file__).resolve().parents[1]\n"
            "    assert source_digest(root, root / '.github/test-suite-taxonomy.json')['digest']\n"
        )
    _git(root, "add", "tests/test_sample.py")
    _git(root, "config", "user.name", "Source Test")
    _git(root, "config", "user.email", "source-test@example.invalid")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "commit", "-qm", "test: prepare miniature inventory archive")
    manifest = tmp_path / "source.json"
    sealed = source.seal_source(root, manifest)
    archive = tmp_path / "archive"
    archive.mkdir()
    with tarfile.open(
        fileobj=io.BytesIO(_git(root, "-c", "core.autocrlf=false", "archive", "HEAD"))
    ) as bundle:
        bundle.extractall(archive, filter="data")
    monkeypatch.setenv("PATH", "")
    timing = tmp_path / "timing.json"
    result = _inventory_cli(
        archive,
        "--source-manifest",
        str(manifest),
        "run",
        "--lane",
        "portable",
        "--observation",
        "archive-fixture",
        "--variant",
        "unit-test",
        "--timing-output",
        str(timing),
        "--external-note",
        "test fixture setup excluded",
        "--",
        "-q",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(timing.read_text(encoding="utf-8"))
    assert record["source"] == sealed["source_digest"]
    assert record["pytest"]["completed_count"] == 4
    collected = tmp_path / "inventory.json"
    result = _inventory_cli(
        archive,
        "--source-manifest",
        str(manifest),
        "collect",
        "--timing",
        str(timing),
        "--json-output",
        str(collected),
        "--markdown-output",
        str(tmp_path / "inventory.md"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(collected.read_text(encoding="utf-8"))["source"] == sealed["source_digest"]


def test_explicit_source_binding_is_scoped_to_one_root(candidate: tuple[Path, Path, Path]) -> None:
    root, archive, manifest = candidate
    sealed = _seal(candidate)
    with source.bound_source(archive, manifest):
        assert (
            source.source_digest(archive, archive / source.DEFAULT_MANIFEST)
            == sealed["source_digest"]
        )
        assert source.source_digest(root, root / source.DEFAULT_MANIFEST) == sealed["source_digest"]
    with pytest.raises(InventoryError, match="Git source"):
        source.source_digest(archive, archive / source.DEFAULT_MANIFEST)
