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

from tests.real_ray_ownership import (
    MAX_OWNER_METADATA_BYTES,
    OWNER_METADATA_OFFSET,
    RealRayOwnershipLock,
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
