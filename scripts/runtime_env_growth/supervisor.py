"""Linux-only external ownership of the persistent native drivers."""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from scripts.investigate_pytest_shutdown import cleanup_owned, enable_subreaper
from scripts.require_linux import require_linux
from scripts.runtime_env_growth.evidence import (
    MAX_EVENTS,
    MAX_REPORT,
    PHASE_SECONDS,
    PHASES,
    SESSION_SECONDS,
    TOTAL_SECONDS,
    GrowthError,
    OwnedRoot,
    admission,
    boundary_snapshot,
    decode,
    validate_value,
)


class SessionClock:
    """Progress cannot renew the enclosing session or experiment deadline."""

    def __init__(self, total_deadline: float) -> None:
        self.total_deadline = total_deadline
        self.session = 0
        self.index = 0
        self.deadline = total_deadline
        self.stage_deadline = total_deadline

    def begin(self, now: float) -> None:
        self.session += 1
        self.index = 0
        self.deadline = min(self.total_deadline, now + SESSION_SECONDS)
        self.stage_deadline = min(self.deadline, now + PHASE_SECONDS["startup"])

    def accept(self, event: dict[str, Any], now: float) -> None:
        self.check(now)
        if set(event) != {"kind", "session", "index", "phase"} or (
            type(event["session"]) is not int
            or type(event["index"]) is not int
            or event["session"] != self.session
            or event["index"] != self.index
            or self.index >= len(PHASES)
            or event["phase"] != PHASES[self.index]
        ):
            raise GrowthError("protocol")
        if self.index:
            self.stage_deadline = min(self.deadline, now + PHASE_SECONDS[event["phase"]])
        self.index += 1

    def check(self, now: float) -> None:
        if now >= min(self.deadline, self.stage_deadline):
            raise GrowthError("deadline")

    def check_session(self, now: float) -> None:
        if now >= min(self.deadline, self.total_deadline):
            raise GrowthError("deadline")


def quiescent(driver_pid: int) -> bool:
    """A persistent driver may remain; Ray descendants may not cross sessions."""
    children = psutil.Process().children(recursive=True)
    for child in children:
        if child.pid != driver_pid:
            try:
                if child.status() == psutil.STATUS_ZOMBIE and child.ppid() == os.getpid():
                    os.waitpid(child.pid, os.WNOHANG)
                else:
                    return False
            except (psutil.NoSuchProcess, ChildProcessError):
                pass
    return True


def settle_session(driver_pid: int, before: dict[str, Any], clock: SessionClock) -> None:
    """Two seconds after the final scan, wholly inside the original session."""
    clock.check_session(time.monotonic())
    deadline = min(clock.deadline, clock.total_deadline, time.monotonic() + 2)
    while not quiescent(driver_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    clean = quiescent(driver_pid)
    unchanged = boundary_snapshot() == before
    now = time.monotonic()
    clock.check_session(now)
    if now >= deadline:
        raise GrowthError("deadline")
    if not clean or not unchanged:
        raise GrowthError("residue")


def wait_driver_exit(process: subprocess.Popen[bytes], clock: SessionClock) -> None:
    clock.check_session(time.monotonic())
    process.wait(timeout=min(5, clock.deadline - time.monotonic()))
    clock.check_session(time.monotonic())


def supervise(
    command: list[str],
    *,
    environment: dict[str, str],
    total_deadline: float,
    before: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    """Private command seam for Linux fault tests; the public CLI has fixed recipes only."""
    require_linux()
    read_fd, write_fd = os.pipe()
    environment = {**environment, "DR_GROWTH_FD": str(write_fd)}
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        pass_fds=(write_fd,),
        start_new_session=True,
    )
    os.close(write_fd)
    assert process.stdin is not None
    records: list[dict[str, Any]] = []
    pending = b""
    total_bytes = 0
    failure = None
    completed = 0
    clean = False
    boundary_unchanged = False
    clock = SessionClock(total_deadline)
    selector = selectors.DefaultSelector()
    selector.register(read_fd, selectors.EVENT_READ)
    try:
        clock.begin(time.monotonic())
        process.stdin.write(b"start\n")
        process.stdin.flush()
        while completed < 3:
            now = time.monotonic()
            clock.check(now)
            ready = selector.select(min(0.05, max(0, clock.stage_deadline - now)))
            if not ready:
                if process.poll() is not None:
                    raise GrowthError("driver_exit")
                continue
            chunk = os.read(read_fd, 4096)
            if not chunk:
                raise GrowthError("driver_exit")
            total_bytes += len(chunk)
            pending += chunk
            if total_bytes > MAX_REPORT or len(pending) > 65536:
                raise GrowthError("output_limit")
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if len(line) > 32768 or len(records) >= MAX_EVENTS:
                    raise GrowthError("output_limit")
                try:
                    event = decode(line, 32768)
                    validate_value(event)
                except (GrowthError, ValueError, TypeError):
                    raise GrowthError("protocol") from None
                if not isinstance(event, dict):
                    raise GrowthError("protocol")
                kind = event.get("kind")
                if kind == "phase":
                    clock.accept(event, time.monotonic())
                elif kind == "failure":
                    if set(event) != {"kind", "reason"}:
                        raise GrowthError("protocol")
                    records.append(event)
                    raise GrowthError("driver_failure")
                elif kind == "data":
                    if set(event) not in ({"kind", "value"}, {"kind", "session", "value"}):
                        raise GrowthError("protocol")
                    if (
                        type(event.get("session", clock.session)) is not int
                        or event.get("session", clock.session) != clock.session
                    ):
                        raise GrowthError("protocol")
                    clock.check(time.monotonic())
                elif kind == "done":
                    if set(event) != {"kind", "session"} or (
                        type(event["session"]) is not int
                        or event["session"] != clock.session
                        or clock.index != len(PHASES)
                    ):
                        raise GrowthError("protocol")
                    clock.check(time.monotonic())
                    settle_session(process.pid, before, clock)
                    completed += 1
                    if completed < 3:
                        clock.begin(time.monotonic())
                        process.stdin.write(b"start\n")
                        process.stdin.flush()
                else:
                    raise GrowthError("protocol")
                records.append(event)
        process.stdin.close()
        wait_driver_exit(process, clock)
        if process.returncode != 0 or pending:
            raise GrowthError("driver_exit")
        # Drain the channel: trailing output cannot be silently accepted.
        if selector.select(0.1) and os.read(read_fd, 1):
            raise GrowthError("protocol")
        clock.check_session(time.monotonic())
    except GrowthError as exc:
        failure = str(exc)
    except (OSError, subprocess.TimeoutExpired, psutil.Error):
        failure = "supervisor_error"
    finally:
        selector.close()
        os.close(read_fd)
        if not process.stdin.closed:
            process.stdin.close()
        try:
            clean, terminated = cleanup_owned(process)
        except (OSError, psutil.Error, subprocess.SubprocessError):
            clean, terminated = False, 0
        try:
            boundary_unchanged = boundary_snapshot() == before
        except (OSError, GrowthError):
            boundary_unchanged = False
    return {
        "completed_sessions": completed,
        "failure": failure,
        "records": records,
        "owned_processes_stopped": clean,
        "forced_cleanup_count": terminated,
        "boundary_unchanged": boundary_unchanged,
        "passed": failure is None
        and completed == 3
        and clean
        and terminated == 0
        and boundary_unchanged,
    }


def run_container() -> dict[str, Any]:
    require_linux()
    from scripts.runtime_env_growth.identity import installed_identity

    producer = installed_identity()
    admitted = admission()
    enable_subreaper()
    # Ray's AF_UNIX socket paths include this prefix and a session timestamp.
    # Exclusive mkdir and captured inode identity, not token length, own cleanup.
    token = uuid.uuid4().hex[:12]
    root = Path("/scratch") / token
    root.mkdir(mode=0o700)
    owner = OwnedRoot.capture(root)
    deadline = time.monotonic() + TOTAL_SECONDS - 30
    arms = []
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "admission": admitted,
        "arms": arms,
        "root_removed": False,
        "producer": producer,
    }
    before = boundary_snapshot()
    try:
        for arm in ("raw", "django"):
            arm_root = root / arm
            (arm_root / "tmp").mkdir(parents=True)
            (arm_root / "home").mkdir()
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "RAY_ADDRESS",
                    "RAY_OVERRIDE_RUNTIME_ENV_DEFAULT_EXCLUDES",
                    "RAY_RUNTIME_ENV_IGNORE_GITIGNORE",
                    "DJANGO_SETTINGS_MODULE",
                }
            }
            env.update(
                TMPDIR=str(arm_root / "tmp"),
                HOME=str(arm_root / "home"),
                PYTHONDONTWRITEBYTECODE="1",
                RAY_ENABLE_UV_RUN_RUNTIME_ENV="0",
            )
            command = [sys.executable, "-m", "scripts.runtime_env_growth.driver", arm, token]
            result = supervise(
                command, environment=env, total_deadline=deadline, before=before, root=root
            )
            arms.append({"arm": arm, **result})
            if not result["passed"]:
                break
        quiescent_now = not psutil.Process().children(recursive=True)
        report["root_removed"] = owner.remove(
            quiescent=quiescent_now and boundary_snapshot() == before
        )
        if len(arms) == 2 and all(arm["passed"] for arm in arms) and report["root_removed"]:
            report["status"] = "complete"
    except (OSError, GrowthError, psutil.Error):
        report["failure"] = "container_error"
    return report
