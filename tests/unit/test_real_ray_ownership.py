"""Cross-process contracts for local-Ray pytest ownership."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest

import tests.real_ray_ownership as real_ray_ownership
from scripts import local_resource_coordinator as coordinator
from tests.real_ray_ownership import (
    MAX_OWNER_METADATA_BYTES,
    OWNER_METADATA_OFFSET,
    RealRayOwnershipLock,
    RealRayOwnershipPathError,
    RealRayOwnershipUnavailableError,
)

_CHILD_OWNER = """
import os
import sys
from pathlib import Path

from tests.real_ray_ownership import RealRayOwnershipLock

ownership = RealRayOwnershipLock(Path(sys.argv[1]))
ownership.acquire(
    {
        "pid": os.getpid(),
        "hostname": "subprocess-host",
        "acquired_at": "2026-08-03T00:00:00+00:00",
        "rootpath": "subprocess-worktree",
        "selected_count": 3,
    }
)
print(f"LOCKED:{os.getpid()}", flush=True)
sys.stdin.readline()
os._exit(0)
"""


def test_legacy_module_reexports_the_coordinator_implementation() -> None:
    for name in (
        "DEFAULT_REAL_RAY_LOCK_PATH",
        "LOCK_BYTE_OFFSET",
        "MAX_OWNER_METADATA_BYTES",
        "OWNER_METADATA_OFFSET",
        "RealRayOwnershipLock",
        "RealRayOwnershipPathError",
        "RealRayOwnershipUnavailableError",
        "build_owner_metadata",
    ):
        assert getattr(real_ray_ownership, name) is getattr(coordinator, name)


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def test_subprocess_contention_reports_owner_and_process_exit_releases_lock(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "real-ray-owner.lock"
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_OWNER, str(lock_path)],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            ready_line = executor.submit(process.stdout.readline)
            try:
                ready = ready_line.result(timeout=10)
            except FutureTimeoutError:
                _stop_process(process)
                raise AssertionError("subprocess did not acquire ownership in time") from None
        if not ready.startswith("LOCKED:"):
            assert process.stderr is not None
            raise AssertionError(
                f"subprocess failed before acquiring ownership: {process.stderr.read()}"
            )
        child_pid = int(ready.removeprefix("LOCKED:").strip())

        contender = RealRayOwnershipLock(lock_path)
        with pytest.raises(RealRayOwnershipUnavailableError) as raised:
            contender.acquire(
                {
                    "pid": os.getpid(),
                    "hostname": "contender-host",
                    "acquired_at": "2026-08-03T00:00:01+00:00",
                    "rootpath": "contender-worktree",
                    "selected_count": 1,
                }
            )

        assert raised.value.owner == {
            "pid": child_pid,
            "hostname": "subprocess-host",
            "acquired_at": "2026-08-03T00:00:00+00:00",
            "rootpath": "subprocess-worktree",
            "selected_count": 3,
        }
        assert "owner metadata" in str(raised.value)

        assert process.stdin is not None
        process.stdin.write("exit\n")
        process.stdin.flush()
        process.stdin.close()
        process.wait(timeout=10)
        assert process.returncode == 0

        contender.acquire(
            {
                "pid": os.getpid(),
                "hostname": "contender-host",
                "acquired_at": "2026-08-03T00:00:01+00:00",
                "rootpath": "contender-worktree",
                "selected_count": 1,
            }
        )
        assert contender.acquired is True
        contender.release()
        assert contender.acquired is False
        assert lock_path.exists()
    finally:
        _stop_process(process)


def test_owner_metadata_is_allowlisted_and_bounded(tmp_path: Path) -> None:
    lock_path = tmp_path / "real-ray-owner.lock"
    ownership = RealRayOwnershipLock(lock_path)
    ownership.acquire(
        {
            "pid": os.getpid(),
            "hostname": "h" * 10_000,
            "acquired_at": "time" * 10_000,
            "rootpath": "é" * 10_000,
            "selected_count": 4,
            "unexpected": "must not be persisted",
        }
    )
    ownership.release()

    payload = lock_path.read_bytes()[OWNER_METADATA_OFFSET:]
    assert len(payload) <= MAX_OWNER_METADATA_BYTES
    decoded = json.loads(payload)
    assert set(decoded) == {
        "acquired_at",
        "hostname",
        "pid",
        "rootpath",
        "selected_count",
    }
    assert all(
        len(decoded[field_name]) <= 512 for field_name in ("acquired_at", "hostname", "rootpath")
    )
    assert decoded["rootpath"].endswith("...")


def test_unlocked_stale_file_never_blocks_a_new_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "real-ray-owner.lock"
    lock_path.write_bytes(b"\0stale and invalid owner metadata")
    ownership = RealRayOwnershipLock(lock_path)

    ownership.acquire(
        {
            "pid": os.getpid(),
            "hostname": "new-owner",
            "acquired_at": "2026-08-03T00:00:00+00:00",
            "rootpath": "new-worktree",
            "selected_count": 1,
        }
    )

    assert ownership.acquired is True
    ownership.release()


def test_symlink_lock_path_is_refused_without_modifying_synthetic_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "synthetic-target"
    original = b"synthetic target must remain unchanged"
    target.write_bytes(original)
    lock_path = tmp_path / "real-ray-owner.lock"
    try:
        lock_path.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this platform: {error}")

    ownership = RealRayOwnershipLock(lock_path)
    with pytest.raises(RealRayOwnershipPathError, match="stable, regular lock file"):
        ownership.acquire({"pid": os.getpid()})

    assert ownership.acquired is False
    assert lock_path.is_symlink()
    assert target.read_bytes() == original


def test_nonregular_lock_path_is_refused(tmp_path: Path) -> None:
    lock_path = tmp_path / "real-ray-owner.lock"
    lock_path.mkdir()
    ownership = RealRayOwnershipLock(lock_path)

    with pytest.raises(RealRayOwnershipPathError, match="stable, regular lock file"):
        ownership.acquire({"pid": os.getpid()})

    assert ownership.acquired is False
    assert lock_path.is_dir()


def test_hard_link_lock_path_is_refused_without_modifying_synthetic_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "synthetic-target"
    original = b"synthetic hard-link target must remain unchanged"
    target.write_bytes(original)
    lock_path = tmp_path / "real-ray-owner.lock"
    os.link(target, lock_path)
    ownership = RealRayOwnershipLock(lock_path)

    with pytest.raises(RealRayOwnershipPathError, match="stable, regular lock file"):
        ownership.acquire({"pid": os.getpid()})

    assert ownership.acquired is False
    assert lock_path.stat().st_nlink == 2
    assert target.read_bytes() == original


def test_path_identity_change_after_lock_is_refused_before_metadata_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "real-ray-owner.lock"
    original = b"\0synthetic stale metadata"
    lock_path.write_bytes(original)
    ownership = RealRayOwnershipLock(lock_path)
    original_stat = real_ray_ownership.os.stat
    lock_path_stat_calls = 0

    def raced_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal lock_path_stat_calls
        value = original_stat(path, *args, **kwargs)
        if Path(path) == lock_path and kwargs.get("follow_symlinks") is False:
            lock_path_stat_calls += 1
            if lock_path_stat_calls == 2:
                fields = list(value)
                fields[1] = value.st_ino + 1
                return os.stat_result(fields)
        return value

    with monkeypatch.context() as scoped:
        scoped.setattr(real_ray_ownership.os, "stat", raced_stat)
        with pytest.raises(RealRayOwnershipPathError, match="stable, regular lock file"):
            ownership.acquire({"pid": os.getpid()})

    assert lock_path_stat_calls == 2
    assert ownership.acquired is False
    assert lock_path.read_bytes() == original

    replacement = RealRayOwnershipLock(lock_path)
    replacement.acquire({"pid": os.getpid(), "hostname": "replacement"})
    assert replacement.acquired is True
    replacement.release()
