"""Keep debugger provisioning bounded and fail closed without running APT."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from scripts import install_ci_debugger as installer


@pytest.fixture
def ubuntu(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        installer.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_CODENAME": "resolute"},
    )
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0, raising=False)
    keyring = tmp_path / "ubuntu-archive-keyring.gpg"
    keyring.touch()
    monkeypatch.setattr(installer, "KEYRING", keyring)
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)


@pytest.mark.parametrize("code", [0, 1, 124])
def test_installed_debugger_is_verified_without_package_work(ubuntu, monkeypatch, code):
    calls = []
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdb")

    def run(phase, command, timeout):
        calls.append((phase, command, timeout))
        return code

    monkeypatch.setattr(installer, "run_phase", run)
    assert installer.install_debugger() == code
    assert calls == [("verify_existing", ["/usr/bin/gdb", "--version"], 5)]


@pytest.mark.parametrize(
    ("failure_phase", "code", "expected_phases"),
    [
        ("update_https", 100, ["update_https"]),
        ("download_https", 124, ["update_https", "download_https"]),
        ("install_offline", 100, ["update_https", "download_https", "install_offline"]),
        (
            "verify_installed",
            1,
            ["update_https", "download_https", "install_offline", "verify_installed"],
        ),
        (None, 0, ["update_https", "download_https", "install_offline", "verify_installed"]),
    ],
)
def test_isolated_download_precedes_offline_install_and_failures_stop(
    ubuntu, monkeypatch, failure_phase, code, expected_phases
):
    calls = []
    roots = []

    def run(phase, command, timeout):
        calls.append((phase, command, timeout))
        if phase != "verify_installed":
            options = dict(value.split("=", 1) for value in command if "=" in value)
            sources = Path(options["Dir::Etc::sourcelist"])
            roots.append(sources.parent)
            assert sources.read_text().splitlines() == [
                f"deb [signed-by={installer.KEYRING}] https://archive.ubuntu.com/ubuntu/ resolute main",
                f"deb [signed-by={installer.KEYRING}] https://archive.ubuntu.com/ubuntu/ resolute-updates main",
                f"deb [signed-by={installer.KEYRING}] https://security.ubuntu.com/ubuntu/ resolute-security main",
            ]
            assert options["Dir::Etc::sourceparts"] == "-"
            assert Path(options["Dir::State::lists"]).parent == sources.parent
            assert Path(options["Dir::Cache::archives"]).parent == sources.parent
            assert options["APT::Update::Error-Mode"] == "any"
            assert options["Acquire::AllowInsecureRepositories"] == "false"
            assert options["APT::Get::AllowUnauthenticated"] == "false"
            assert options["Acquire::Retries"] == "0"
            assert options["Acquire::Languages"] == "none"
            assert "--ignore-missing" not in command
            assert "--allow-unauthenticated" not in command
            if phase == "download_https":
                assert "--download-only" in command
                assert "--no-download" not in command
            elif phase == "install_offline":
                assert "--no-download" in command
                assert "--download-only" not in command
                monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdb")
        return code if phase == failure_phase else 0

    monkeypatch.setattr(installer, "run_phase", run)
    assert installer.install_debugger() == code
    assert [phase for phase, _, _ in calls] == expected_phases
    assert len(set(roots)) == 1
    assert not roots[0].exists()
    assert sum(timeout for _, _, timeout in calls) + 2 * installer.CLEANUP_SECONDS < 170


@pytest.mark.parametrize("missing", ["root", "keyring", "installed_binary"])
def test_missing_prerequisites_or_final_binary_fail(ubuntu, monkeypatch, capsys, missing):
    phases = []
    if missing == "root":
        monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    elif missing == "keyring":
        installer.KEYRING.unlink()

    def run(phase, command, timeout):
        phases.append(phase)
        return 0

    monkeypatch.setattr(installer, "run_phase", run)
    assert installer.install_debugger() == 2
    assert phases == (
        ["update_https", "download_https", "install_offline"]
        if missing == "installed_binary"
        else []
    )
    assert json.loads(capsys.readouterr().out)["event"] == "unavailable"


@pytest.mark.parametrize(
    ("system", "machine", "release"),
    [
        ("win32", "x86_64", {"ID": "ubuntu", "VERSION_CODENAME": "resolute"}),
        ("linux", "aarch64", {"ID": "ubuntu", "VERSION_CODENAME": "resolute"}),
        ("linux", "x86_64", {"ID": "debian", "VERSION_CODENAME": "trixie"}),
        ("linux", "x86_64", {"ID": "ubuntu"}),
        ("linux", "x86_64", {"ID": "ubuntu", "VERSION_CODENAME": "noble\nmalformed"}),
    ],
)
def test_unsupported_platform_never_starts_package_work(
    ubuntu, monkeypatch, system, machine, release
):
    monkeypatch.setattr(installer.sys, "platform", system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    monkeypatch.setattr(installer.platform, "freedesktop_os_release", lambda: release)
    monkeypatch.setattr(installer, "run_phase", lambda *args: pytest.fail("Unexpected command"))
    assert installer.install_debugger() == 2


@pytest.mark.parametrize("code", [0, 7, 100])
def test_real_child_exit_is_preserved(code, capsys):
    assert (
        installer.run_phase("probe", [sys.executable, "-c", f"raise SystemExit({code})"], 5) == code
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["event"] for record in records] == ["started", "completed"]
    assert records[-1]["returncode"] == code
    assert records[-1]["elapsed_seconds"] >= 0


def test_real_hanging_child_is_bounded(capsys):
    started = time.monotonic()
    assert (
        installer.run_phase("probe", [sys.executable, "-c", "import time; time.sleep(60)"], 0.2)
        == 124
    )
    assert time.monotonic() - started < 5
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1]["event"] == "timed_out"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux child-session cleanup")
def test_timeout_kills_transport_child_after_parent_exits(tmp_path, monkeypatch):
    marker = tmp_path / "child-pid"
    child = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    )
    popen = installer.subprocess.Popen

    def start_when_ready(*args, **kwargs):
        process = popen(*args, **kwargs)
        # Separate child readiness from the timeout being tested. A slow Python
        # startup must not turn this into a race against installing SIGTERM's handler.
        deadline = time.monotonic() + 10
        while not marker.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                installer._signal_process_group(process, force=True)
                process.wait(timeout=2)
                pytest.fail("Transport child failed to become ready")
            time.sleep(0.01)
        return process

    monkeypatch.setattr(installer.subprocess, "Popen", start_when_ready)
    try:
        assert installer.run_phase("probe", [sys.executable, "-c", parent], 0.2) == 124
        pid = int(marker.read_text())
        # A killed orphan may remain as a zombie until the runner's init reaps it.
        status = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 2
        while True:
            try:
                state = status.read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            assert time.monotonic() < deadline, "Transport child survived the phase timeout"
            time.sleep(0.01)
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_missing_command_fails_closed(tmp_path, capsys):
    assert installer.run_phase("probe", [str(tmp_path / "missing")], 1) == 2
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1]["event"] == "launch_failed"
