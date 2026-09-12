"""Bounded independent artifact copies for the admitted native upgrade fixture.

The orchestrator binds its observed run identity before starting writers. These
helpers verify that local binding and copied bytes; they do not authenticate a
PVC, stop writers, snapshot PostgreSQL, or qualify the complete upgrade gate.
Incomplete copies remain reserved for inspection and cannot be reused.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path

from qualification.upgrade import runtime_steps as steps

DIRECTORIES = ("inputs", "results", "runtime-effects", "observations")
MAX_FILES = 512
MAX_DIRECTORIES = 64
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
FREE_RESERVE_BYTES = 64 * 1024 * 1024
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ArtifactCopyError(ValueError):
    """A fixed refusal without paths, payloads, or provider exception text."""


def _require(condition: bool) -> None:
    if not condition:
        raise ArtifactCopyError("invalid-upgrade-artifact-copy")


def _digest(value: str) -> None:
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _directory(path: Path) -> None:
    _require(not path.is_symlink() and path.is_dir() and path.resolve() == path)


def _new_directory(path: Path) -> None:
    _directory(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise ArtifactCopyError("upgrade-artifact-destination-exists") from None


def _regular(path: Path):
    metadata = path.lstat()
    _require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1)
    _require(0 <= metadata.st_size <= MAX_FILE_BYTES)
    return metadata


def _file_digest(path: Path) -> tuple[int, str]:
    before = _regular(path)
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        _require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino))
        _require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1)
        while block := stream.read(65536):
            total += len(block)
            _require(total <= MAX_FILE_BYTES)
            digest.update(block)
        after = os.fstat(stream.fileno())
    latest = _regular(path)
    _require(
        total == before.st_size == after.st_size == latest.st_size
        and before.st_dev == after.st_dev == latest.st_dev
        and before.st_ino == after.st_ino == latest.st_ino
        and before.st_mtime_ns == after.st_mtime_ns == latest.st_mtime_ns
        and stat.S_ISREG(after.st_mode)
        and after.st_nlink == 1
    )
    return total, digest.hexdigest()


def _inventory(root: Path) -> dict:
    """Read only the four fixed application trees, including empty directories."""
    _directory(root)
    directories: list[str] = []
    files: list[dict] = []
    total = 0

    def visit(path: Path, depth: int) -> None:
        nonlocal total
        _require(depth <= 4)
        _directory(path)
        directories.append(path.relative_to(root).as_posix())
        _require(len(directories) <= MAX_DIRECTORIES)
        # Iterate with a bound before sorting, rather than materializing an
        # unbounded directory supplied by an unexpected writer.
        children = []
        for child in path.iterdir():
            _require(_COMPONENT.fullmatch(child.name) is not None)
            children.append(child)
            _require(len(children) <= MAX_FILES + MAX_DIRECTORIES)
        for child in sorted(children):
            metadata = child.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                visit(child, depth + 1)
            else:
                size, digest = _file_digest(child)
                total += size
                files.append(
                    {"path": child.relative_to(root).as_posix(), "bytes": size, "sha256": digest}
                )
                _require(len(files) <= MAX_FILES and total <= MAX_TOTAL_BYTES)

    for name in DIRECTORIES:
        visit(root / name, 1)
    return {"directories": sorted(directories), "files": files, "bytes": total}


def bind_artifact_store(run_digest: str) -> dict:
    """Bind an empty prepared store to the orchestrator's already observed run."""
    _digest(run_digest)
    root = steps._root()
    _require({path.name for path in root.iterdir()} == set(DIRECTORIES))
    inventory = _inventory(root)
    _require(not inventory["files"] and len(inventory["directories"]) == 4)
    control = root / ".upgrade-control"
    _new_directory(control)
    scope = {"schema": 1, "run_digest": run_digest}
    steps._write_once(control / "artifact-scope.json", scope)
    _new_directory(root / ".upgrade-backups")
    _new_directory(root / ".upgrade-restores")
    return scope


def _bound_root(run_digest: str) -> Path:
    _digest(run_digest)
    root = steps._root()
    control = root / ".upgrade-control"
    _directory(control)
    _require(
        steps._read(control / "artifact-scope.json") == {"schema": 1, "run_digest": run_digest}
    )
    _directory(root / ".upgrade-backups")
    _directory(root / ".upgrade-restores")
    return root


def _copy(source: Path, destination: Path, inventory: dict) -> None:
    _require(shutil.disk_usage(destination.parent).free >= inventory["bytes"] + FREE_RESERVE_BYTES)
    _new_directory(destination)
    for name in inventory["directories"]:
        _new_directory(destination / name)
    for item in inventory["files"]:
        source_file = source / item["path"]
        target = destination / item["path"]
        _directory(source_file.parent)
        before = _regular(source_file)
        copied = 0
        digest = hashlib.sha256()
        with source_file.open("rb") as reader, target.open("xb") as writer:
            opened = os.fstat(reader.fileno())
            _require(
                stat.S_ISREG(opened.st_mode)
                and opened.st_nlink == 1
                and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino)
            )
            while block := reader.read(65536):
                copied += len(block)
                _require(copied <= item["bytes"])
                writer.write(block)
                digest.update(block)
            writer.flush()
            os.fsync(writer.fileno())
        _require(copied == item["bytes"] and digest.hexdigest() == item["sha256"])
    _require(_inventory(source) == inventory and _inventory(destination) == inventory)


def backup_artifacts(point: str, *, run_digest: str) -> dict:
    """Reserve one immutable blocked/final copy; never include prior backups."""
    _require(type(point) is str and point in {"blocked", "final"})
    root = _bound_root(run_digest)
    inventory = _inventory(root)
    _require(shutil.disk_usage(root).free >= inventory["bytes"] + FREE_RESERVE_BYTES)
    destination = root / ".upgrade-backups" / point
    _new_directory(destination)
    _copy(root, destination / "artifacts", inventory)
    receipt = {
        "schema": 1,
        "run_digest": run_digest,
        "backup_point": point,
        "inventory": inventory,
        "artifacts_sha256": hashlib.sha256(steps._json(inventory)).hexdigest(),
    }
    # Publication happens last. A reserved directory without this receipt is
    # incomplete, and subsequent calls must refuse to overwrite it.
    steps._write_once(destination / "artifacts.json", receipt)
    return receipt


def restore_artifacts(point: str, *, run_digest: str, expected_artifacts_sha256: str) -> dict:
    """Copy into a fresh fixed subPath; rollback always uses the final backup."""
    _require(type(point) is str and point in {"blocked", "final", "rollback"})
    _digest(expected_artifacts_sha256)
    root = _bound_root(run_digest)
    backup_point = "final" if point == "rollback" else point
    backup = root / ".upgrade-backups" / backup_point
    _directory(backup)
    receipt = steps._read(backup / "artifacts.json")
    inventory = _inventory(backup / "artifacts")
    expected = {
        "schema": 1,
        "run_digest": run_digest,
        "backup_point": backup_point,
        "inventory": inventory,
        "artifacts_sha256": hashlib.sha256(steps._json(inventory)).hexdigest(),
    }
    _require(receipt == expected and expected["artifacts_sha256"] == expected_artifacts_sha256)
    destination = root / ".upgrade-restores" / point
    _copy(backup / "artifacts", destination, inventory)
    restored = {
        "schema": 1,
        "run_digest": run_digest,
        "restore_point": point,
        "backup_point": backup_point,
        "artifacts_sha256": expected_artifacts_sha256,
        "independent_artifact_copy": True,
        "complete_upgrade_gate": False,
    }
    steps._write_once(root / ".upgrade-control" / (point + "-artifacts-restored.json"), restored)
    return restored
