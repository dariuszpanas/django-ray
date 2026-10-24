"""Bounded filesystem evidence; this module never imports Ray or Django."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
SCRATCH_BYTES = 1024 * MIB
MEMORY_BYTES = 6144 * MIB
SHM_BYTES = 256 * MIB
MAX_ENTRIES = 25_000
MAX_BYTES = 512 * MIB
MAX_REPORT = 256 * 1024
MAX_EVENTS = 256
SCAN_SECONDS = 2.0
EXCLUDES = ["artifacts", "ray-tmp", ".vault"]
CANARIES = [".venv", "build", "dist", "artifacts", "ray-tmp", ".vault"]
DEFAULT_EXCLUDES = [".git", ".venv", "venv", "__pycache__"]
PHASES = ["startup", "scan"] + ["prepare", "task", "scan"] * 5 + ["shutdown", "scan"]
PHASE_SECONDS = {"startup": 60, "scan": 2, "prepare": 20, "task": 15, "shutdown": 20}
SESSION_SECONDS = 180
TOTAL_SECONDS = 1200


class GrowthError(Exception):
    """A fixed reason suitable for a public diagnostic report."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value: object, limit: int = MAX_REPORT) -> bytes:
    data = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    if len(data) > limit:
        raise GrowthError("output_limit")
    return data


def decode(data: bytes, limit: int = MAX_REPORT) -> Any:
    """Bounded JSON with no duplicate keys or non-finite extension values."""
    if len(data) > limit:
        raise GrowthError("output_limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise GrowthError("report_invalid")
            result[key] = value
        return result

    def invalid(_value: str) -> None:
        raise GrowthError("report_invalid")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, RecursionError):
        raise GrowthError("report_invalid") from None


def role(relative: tuple[str, ...]) -> str:
    if "source" in relative:
        return "source"
    if any(part.startswith("django-ray-runtime-env-snapshot-") for part in relative):
        return "snapshot"
    if any(part.startswith("django-ray-runtime-env-") for part in relative):
        return "upload_scratch"
    if "working_dir_files" in relative:
        return "ray_extracted"
    return "other"


def scan_tree(
    root: Path,
    *,
    max_entries: int = MAX_ENTRIES,
    max_bytes: int = MAX_BYTES,
    seconds: float = SCAN_SECONDS,
) -> dict[str, Any]:
    """Count without following links or retaining filenames; partial counts stay explicit."""
    totals = {
        key: {
            "bytes": 0,
            "allocated_bytes": 0,
            "files": 0,
            "directories": 0,
            "symlinks": 0,
            "special": 0,
        }
        for key in ("source", "snapshot", "upload_scratch", "ray_extracted", "other")
    }
    entries = byte_count = 0
    deadline = time.monotonic() + seconds
    pending = [(root, ())]
    reason = "complete"
    try:
        if root.is_symlink() or not root.is_dir():
            raise GrowthError("unsafe_root")
        while pending:
            directory, prefix = pending.pop()
            with os.scandir(directory) as children:
                for child in children:
                    if time.monotonic() >= deadline:
                        raise GrowthError("scan_timeout")
                    if entries >= max_entries:
                        raise GrowthError("entry_limit")
                    info = child.stat(follow_symlinks=False)
                    relative = (*prefix, child.name)
                    bucket = totals[role(relative)]
                    entries += 1
                    if stat.S_ISLNK(info.st_mode):
                        bucket["symlinks"] += 1
                    elif stat.S_ISDIR(info.st_mode):
                        bucket["directories"] += 1
                        pending.append((Path(child.path), relative))
                    elif stat.S_ISREG(info.st_mode):
                        byte_count += info.st_size
                        bucket["files"] += 1
                        bucket["bytes"] += info.st_size
                        bucket["allocated_bytes"] += getattr(info, "st_blocks", 0) * 512
                        if byte_count > max_bytes:
                            raise GrowthError("byte_limit")
                    else:
                        bucket["special"] += 1
    except GrowthError as exc:
        reason = str(exc)
    except OSError:
        reason = "scan_error"
    return {"status": reason, "entries": entries, "bytes": byte_count, "roles": totals}


def archive_record(path: Path) -> dict[str, Any]:
    """Observe the actual generated ZIP, before the uploader removes it."""
    if path.is_symlink() or path.stat().st_size > 16 * MIB:
        raise GrowthError("archive_limit")
    content = path.read_bytes()
    if len(content) > 16 * MIB:
        raise GrowthError("archive_limit")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 1024 or sum(member.file_size for member in members) > 16 * MIB:
            raise GrowthError("archive_limit")
        return {
            "compressed_bytes": len(content),
            "uncompressed_bytes": sum(member.file_size for member in members),
            "entries": len(members),
            "sha256": digest(content),
        }


def make_fixture(parent: Path) -> tuple[Path, Path]:
    """The only package inputs are generated, bounded, non-secret application fixtures."""
    roots = []
    for variant in ("A", "B"):
        root = parent / variant
        root.mkdir(parents=True)
        (root / "app_value.txt").write_text(variant, encoding="ascii")
        (root / "included.txt").write_text("included", encoding="ascii")
        (root / ".gitignore").write_text("build/\n", encoding="ascii")
        (root / ".rayignore").write_text("dist/\n", encoding="ascii")
        for index in range(128):
            (root / f"data-{index:03d}.bin").write_bytes(bytes([index]) * 32768)
        for name in CANARIES:
            directory = root / name
            directory.mkdir()
            (directory / "excluded.bin").write_bytes(b"excluded" * 1024)
        for item in root.rglob("*"):
            if item.is_file():
                item.chmod(0o444)
        roots.append(root)
    result = scan_tree(parent, max_entries=2048, max_bytes=32 * MIB)
    if result["status"] != "complete":
        raise GrowthError("source_limit")
    return roots[0], roots[1]


def fixture_identity(root: Path) -> str:
    """Hash only the small generated fixture, independently of Ray's package key."""
    records = []
    total = 0
    for item in root.rglob("*"):
        if len(records) >= 2048 or item.is_symlink():
            raise GrowthError("source_limit")
        if item.is_file():
            total += item.stat().st_size
            if total > 32 * MIB:
                raise GrowthError("source_limit")
            records.append((item.relative_to(root).as_posix(), digest(item.read_bytes())))
    return digest(encode(sorted(records)))


def boundary_snapshot() -> dict[str, Any]:
    """Bounded names-only digests; never read or delete foreign files."""
    records = []
    for path in (Path("/dev/shm"), Path("/tmp/ray")):
        if not path.exists():
            records.append({"entries": 0, "sha256": digest(b"")})
            continue
        if path.is_symlink():
            raise GrowthError("external_state")
        names = []
        with os.scandir(path) as entries:
            for entry in entries:
                if len(names) >= 1024:
                    raise GrowthError("external_state")
                names.append(entry.name)
        records.append({"entries": len(names), "sha256": digest(encode(sorted(names), 65536))})
    # Only the isolated container's network namespace is observed.
    listeners = []
    for filename in ("tcp", "tcp6", "udp", "udp6"):
        with (Path("/proc/net") / filename).open("rb") as handle:
            content = handle.read(65537)
        if len(content) > 65536:
            raise GrowthError("external_state")
        rows = []
        for line in content.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4:
                raise GrowthError("external_state")
            if filename.startswith("udp") or fields[3] == b"0A":
                rows.append(fields[1])
        listeners.append(digest(b"\n".join(sorted(rows))))
    return {"roots": records, "listeners": listeners}


@dataclass(frozen=True)
class OwnedRoot:
    path: Path
    parent_identity: tuple[int, int]
    identity: tuple[int, int]

    @classmethod
    def capture(cls, path: Path) -> OwnedRoot:
        if path.is_symlink() or path.parent.is_symlink():
            raise GrowthError("unsafe_root")
        parent, current = path.parent.stat(), path.stat()
        if not stat.S_ISDIR(current.st_mode):
            raise GrowthError("unsafe_root")
        return cls(path, (parent.st_dev, parent.st_ino), (current.st_dev, current.st_ino))

    def remove(self, *, quiescent: bool) -> bool:
        """Remove contents through the captured directory, never a replacement root."""
        if not quiescent or self.path.is_symlink() or self.path.parent.is_symlink():
            return False
        try:
            parent = self.path.parent.stat()
            current = self.path.stat()
            if (parent.st_dev, parent.st_ino) != self.parent_identity or (
                current.st_dev,
                current.st_ino,
            ) != self.identity:
                return False
            parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                root_fd = os.open(
                    self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                )
                try:
                    opened = os.fstat(root_fd)
                    if (opened.st_dev, opened.st_ino) != self.identity:
                        return False
                    with os.scandir(root_fd) as entries:
                        for entry in entries:
                            if entry.is_dir(follow_symlinks=False):
                                shutil.rmtree(entry.name, dir_fd=root_fd)
                            else:
                                os.unlink(entry.name, dir_fd=root_fd)
                finally:
                    os.close(root_fd)
                final = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (final.st_dev, final.st_ino) != self.identity:
                    return False
                os.rmdir(self.path.name, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            return not self.path.exists()
        except OSError:
            return False


def admission() -> dict[str, int]:
    """Require effective cgroup-v2 and tmpfs limits before creating a runtime."""
    try:
        group = Path("/sys/fs/cgroup")
        memory = int((group / "memory.max").read_text())
        pids = int((group / "pids.max").read_text())
        quota, period = map(int, (group / "cpu.max").read_text().split())
        with Path("/proc/self/mountinfo").open("rb") as handle:
            mounts = handle.read(65537)
        if len(mounts) > 65536:
            raise ValueError
        scratch = os.statvfs("/scratch")
        shm = os.statvfs("/dev/shm")
        scratch_bytes = scratch.f_blocks * scratch.f_frsize
        shm_bytes = shm.f_blocks * shm.f_frsize
        if not any(b" /scratch " in line and b" - tmpfs " in line for line in mounts.splitlines()):
            raise ValueError
        if not (
            0 < memory <= MEMORY_BYTES
            and 0 < pids <= 256
            and 0 < quota <= 2 * period
            and 0 < scratch_bytes <= SCRATCH_BYTES
            and 0 < scratch.f_files <= 50_000
            and 128 * MIB <= shm_bytes <= SHM_BYTES
        ):
            raise ValueError
    except (ValueError, OSError):
        raise GrowthError("admission") from None
    return {
        "memory_bytes": memory,
        "pids": pids,
        "cpu_quota": quota,
        "cpu_period": period,
        "scratch_bytes": scratch_bytes,
        "scratch_inodes": scratch.f_files,
        "shm_bytes": shm_bytes,
    }


def validate_value(value: object, *, depth: int = 0) -> None:
    """No arbitrary driver strings or keys may cross into the public evidence."""
    words = {
        "raw",
        "django",
        "A",
        "B",
        "phase",
        "data",
        "done",
        "failure",
        "complete",
        "startup",
        "scan",
        "prepare",
        "task",
        "shutdown",
        "source",
        "snapshot",
        "upload_scratch",
        "ray_extracted",
        "other",
        "uploaded",
        "cache_hit",
        "uri_reuse",
        "emitted",
        "failed",
        "scan_timeout",
        "entry_limit",
        "byte_limit",
        "scan_error",
        "unsafe_root",
        "archive_limit",
        "source_limit",
        "external_state",
        "identity",
        "configuration",
        "application",
        "runtime",
        "output_limit",
        "settings",
        "admission",
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "artifacts",
        "ray-tmp",
        ".vault",
    }
    keys = {
        "kind",
        "session",
        "index",
        "phase",
        "value",
        "status",
        "entries",
        "bytes",
        "allocated_bytes",
        "files",
        "directories",
        "symlinks",
        "special",
        "roles",
        "source",
        "snapshot",
        "upload_scratch",
        "ray_extracted",
        "other",
        "archives",
        "scope",
        "compressed_bytes",
        "uncompressed_bytes",
        "sha256",
        "uri_sha256",
        "operation",
        "variant",
        "elapsed_seconds",
        "settings_sha256",
        "ray_version",
        "django_ray_version",
        "python_version",
        "reason",
        "snapshot_removed",
        "identity_sha256",
        "before",
        "after",
        "extracted",
        "session_count",
        "included",
        "default_excludes",
        "excludes",
        "gitignore_sha256",
        "rayignore_sha256",
    }
    if depth > 10:
        raise GrowthError("output_limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int | float):
        if not math.isfinite(value) or value < 0 or value > 4 * SCRATCH_BYTES:
            raise GrowthError("output_limit")
        return
    if isinstance(value, str):
        if value in words or re.fullmatch(r"[0-9a-f]{64}|[0-9]+(?:\.[0-9]+){1,3}", value):
            return
        raise GrowthError("output_limit")
    if isinstance(value, list) and len(value) <= 32:
        for item in value:
            validate_value(item, depth=depth + 1)
        return
    if isinstance(value, dict) and set(value) <= keys and len(value) <= 32:
        for item in value.values():
            validate_value(item, depth=depth + 1)
        return
    raise GrowthError("output_limit")
