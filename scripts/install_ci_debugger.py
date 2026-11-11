"""Provision required GDB on a disposable Ubuntu x86-64 CI runner.

Run with sudo when GDB is absent. Canonical HTTPS sources and package caches
are temporary; no host source configuration is rewritten. Whole-phase limits
bound APT's cumulative requests. Download failures cannot start dpkg, and an
offline install failure is terminal: discard the disposable runner, never retry
the install against a different source or suppress the diagnostic requirement.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KEYRING = Path("/usr/share/keyrings/ubuntu-archive-keyring.gpg")
UPDATE_SECONDS = 50
DOWNLOAD_SECONDS = 50
INSTALL_SECONDS = 35
VERIFY_SECONDS = 5
CLEANUP_SECONDS = 2


def emit(phase: str, event: str, **values: int | float) -> None:
    """Keep phase boundaries visible even when APT stops producing output."""
    print(json.dumps({"ci_debugger": phase, "event": event, **values}, sort_keys=True), flush=True)


def _signal_process_group(process: subprocess.Popen, *, force: bool) -> None:
    """Stop the entire owned session, including APT transport subprocesses."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def run_phase(phase: str, command: list[str], timeout: float) -> int:
    """Preserve command failures and stop an entire hung phase within its budget."""
    started = time.monotonic()
    emit(phase, "started", timeout_seconds=timeout)
    try:
        process = subprocess.Popen(
            command,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"},
            start_new_session=os.name == "posix",
        )
    except OSError:
        emit(phase, "launch_failed", returncode=2)
        return 2
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, force=False)
        try:
            process.wait(timeout=CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        # A parent can exit on TERM while a transport child ignores it.
        _signal_process_group(process, force=True)
        try:
            process.wait(timeout=CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            emit(phase, "cleanup_failed", returncode=124)
        code = 124
        event = "timed_out"
    else:
        event = "completed"
        if code < 0:
            code = 128 - code
    emit(phase, event, returncode=code, elapsed_seconds=round(time.monotonic() - started, 3))
    return code


def apt_command(root: Path, codename: str) -> list[str]:
    """Use signed Ubuntu metadata in one isolated configuration for all phases."""
    # APT's unprivileged download user needs to traverse the temporary parent.
    root.chmod(0o755)
    for name in ("lists", "archives"):
        (root / name).mkdir(mode=0o755)
    sources = root / "ubuntu.list"
    sources.write_text(
        "".join(
            f"deb [signed-by={KEYRING}] https://{host}/ubuntu/ {suite} main\n"
            for host, suite in (
                ("archive.ubuntu.com", codename),
                ("archive.ubuntu.com", f"{codename}-updates"),
                ("security.ubuntu.com", f"{codename}-security"),
            )
        ),
        encoding="utf-8",
    )
    sources.chmod(0o644)
    options = {
        "Dir::Etc::sourcelist": str(sources),
        "Dir::Etc::sourceparts": "-",
        "Dir::State::lists": str(root / "lists"),
        "Dir::Cache::archives": str(root / "archives"),
        "Dir::Cache::pkgcache": "",
        "Dir::Cache::srcpkgcache": "",
        "Acquire::Retries": "0",
        "Acquire::http::Timeout": "10",
        "Acquire::https::Timeout": "10",
        "Acquire::Languages": "none",
        "Acquire::AllowInsecureRepositories": "false",
        "APT::Get::AllowUnauthenticated": "false",
        "APT::Update::Error-Mode": "any",
        "DPkg::Lock::Timeout": "5",
    }
    return [
        "/usr/bin/apt-get",
        *(value for key, item in options.items() for value in ("-o", f"{key}={item}")),
    ]


def install_debugger() -> int:
    """Require a working debugger; never turn failed provisioning into a pass."""
    if sys.platform != "linux" or platform.machine() != "x86_64":
        emit("platform", "unsupported", returncode=2)
        return 2
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        emit("platform", "unavailable", returncode=2)
        return 2
    codename = release.get("VERSION_CODENAME", "")
    if release.get("ID") != "ubuntu" or re.fullmatch(r"[a-z][a-z0-9]*", codename) is None:
        emit("platform", "unsupported", returncode=2)
        return 2
    debugger = shutil.which("gdb")
    if debugger is not None:
        return run_phase("verify_existing", [debugger, "--version"], VERIFY_SECONDS)
    if os.geteuid() != 0 or not KEYRING.is_file():
        emit("prerequisites", "unavailable", returncode=2)
        return 2
    with tempfile.TemporaryDirectory(prefix="django-ray-gdb-") as temporary:
        apt = apt_command(Path(temporary), codename)
        install = ["install", "--yes", "--no-install-recommends", "--no-remove", "gdb"]
        phases = (
            ("update_https", [*apt, "update"], UPDATE_SECONDS),
            ("download_https", [*apt, "--download-only", *install], DOWNLOAD_SECONDS),
            ("install_offline", [*apt, "--no-download", *install], INSTALL_SECONDS),
        )
        for phase, command, timeout in phases:
            code = run_phase(phase, command, timeout)
            if code:
                return code
        debugger = shutil.which("gdb")
        if debugger is None:
            emit("verify_installed", "unavailable", returncode=2)
            return 2
        return run_phase("verify_installed", [debugger, "--version"], VERIFY_SECONDS)


if __name__ == "__main__":
    raise SystemExit(install_debugger())
