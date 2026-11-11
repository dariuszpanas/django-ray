"""Resource-free contracts for the bounded hosted historical-exit supervisor."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from scripts import investigate_native_exit as investigation


def test_fixed_commands_preserve_uv_hook_options_capture_and_coverage(tmp_path):
    phase = tmp_path / "phase.bin"
    command = investigation.probe_command("distributed", True, phase)
    assert command[:6] == [
        "uv",
        "run",
        "--python",
        "3.14.10",
        "python",
        "scripts/observe_pytest_exit.py",
    ]
    assert command[6:11] == [
        "--native-debug",
        "--phase-journal",
        str(phase),
        "--trace-native-exit",
        "--",
    ]
    assert command[11:] == [
        "tests/unit/test_distributed.py",
        "--cov=src",
        "--cov-report=term",
        "--cov-fail-under=0",
        "-v",
    ]
    full = investigation.probe_command("prefix", False, phase)
    assert "--trace-native-exit" not in full
    assert "--cov-fail-under=95" in full
    assert full[full.index("--") + 1 :][:4] == [
        "tests",
        "-m",
        "not live_cluster",
        "--ignore=tests/unit/test_observe_pytest_exit.py",
    ]
    for forbidden in ("--no-sync", "--frozen", "-s", "-x"):
        assert forbidden not in command + full
    with pytest.raises(ValueError, match="Unknown fixed"):
        investigation.probe_command("custom-command", False, phase)


def test_hook_control_is_explicit_and_does_not_leak_harness_pythonpath(monkeypatch, tmp_path):
    monkeypatch.setenv("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    monkeypatch.setenv("PYTHONPATH", "unrelated-harness")
    historical = investigation.probe_environment("historical", tmp_path)
    assert "RAY_ENABLE_UV_RUN_RUNTIME_ENV" not in historical
    assert "PYTHONPATH" not in historical
    assert historical["RAY_TMPDIR"] == str(tmp_path)
    assert historical["DJANGO_RAY_REQUIRE_KUSTOMIZE_PROBE_TESTS"] == "1"
    assert (
        investigation.probe_environment("disabled", tmp_path)["RAY_ENABLE_UV_RUN_RUNTIME_ENV"]
        == "0"
    )
    with pytest.raises(ValueError):
        investigation.probe_environment("unknown", tmp_path)


def test_resource_ring_retains_early_extrema_after_samples_rotate():
    window = investigation.ResourceWindow()
    for number in range(400):
        window.add(
            number,
            {
                "disk_free_bytes": number,
                "uv_descendants": 400 - number,
                "cgroup_memory_events": {"oom_kill": number},
            },
        )
    result = window.receipt()
    assert result["sample_count"] == 400
    assert len(result["retained_samples"]) == investigation.MAX_RESOURCE_SAMPLES
    minimum, maximum = result["minimum"], result["maximum"]
    assert isinstance(minimum, dict) and isinstance(maximum, dict)
    assert minimum["disk_free_bytes"] == 0
    assert maximum["uv_descendants"] == 400
    assert maximum["cgroup_event_oom_kill"] == 399


def test_owned_ray_root_leaves_room_for_realistic_session_socket_name(monkeypatch):
    calls = []

    def temporary_directory(**options):
        calls.append(options)
        return "/tmp/drx-abcdefgh"

    monkeypatch.setattr(investigation.tempfile, "mkdtemp", temporary_directory)
    root = PurePosixPath(investigation.create_ray_tmpdir().as_posix())
    suffix = "ray/session_2026-11-11_22-58-49_788421_4194304/sockets/plasma_store"
    assert calls == [{"prefix": "drx-", "dir": "/tmp"}]
    assert len(str(root / suffix).encode()) <= 107
    previous = PurePosixPath("/home/runner/work/_temp/django-ray-native-exit-abcdefgh")
    assert len(str(previous / suffix).encode()) > 107


def test_ray_tail_collection_uses_actual_nested_layout_and_fixed_bounds(monkeypatch, tmp_path):
    # Windows runs these file-only tests; Linux uses the actual no-follow/nonblock flags.
    monkeypatch.setattr(investigation.os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0), raising=False)
    monkeypatch.setattr(investigation.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), raising=False)
    owned, output = tmp_path / "owned", tmp_path / "output"
    output.mkdir()
    for index in range(5):
        session = owned / "ray" / f"session_{index}"
        logs = session / "logs"
        logs.mkdir(parents=True)
        for name in (*investigation.RAY_LOGS, "python-core-driver-abc_12.log", "worker-secret.out"):
            (logs / name).write_bytes(bytes([index]) * 40_000)
        os.utime(session, (index + 1, index + 1))
    result = investigation.retain_ray_logs(owned, output)
    assert len(result) == 18
    assert all(item["retained_bytes"] == 32 * 1024 for item in result)
    assert all("worker-secret" not in str(item["file"]) for item in result)
    assert (output / "ray-session-1-raylet.err").read_bytes() == bytes([4]) * (32 * 1024)
    assert investigation.retain_ray_logs(tmp_path / "absent", output) == []


@pytest.fixture
def harness(monkeypatch, tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    (source / "uv.lock").write_text("frozen lock")
    owned_tmpdir = tmp_path / "owned-ray"
    owned_tmpdir.mkdir()
    monkeypatch.setattr(investigation, "create_ray_tmpdir", lambda: owned_tmpdir)
    now = [0.0]
    launched = []
    reaped = []
    monkeypatch.setattr(investigation, "reap_adopted_zombies", lambda pid: reaped.append(pid) or 0)
    monkeypatch.setattr(
        investigation,
        "source_identity",
        lambda source, timeout: {
            "source_head": "a" * 40,
            "source_tree": "b" * 40,
            "lock_sha256_before": investigation.digest(source / "uv.lock"),
        },
    )
    monkeypatch.setattr(investigation.metadata, "version", lambda name: "1.0")
    monkeypatch.setattr(
        investigation.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="uv 0.13.7\n"),
    )
    monkeypatch.setattr(
        investigation, "resource_sample", lambda path: {"disk_free_bytes": 100, "uv_descendants": 1}
    )
    monkeypatch.setattr(
        investigation,
        "phase_snapshot",
        lambda path: {"phase": "call", "ordinal": 20, "sequence": 40},
    )

    class Process:
        def __init__(self, finish, code, payload):
            self.pid = 12345
            self.finish, self.code = finish, code
            self.returncode = None
            self.stdout = io.BytesIO(payload)

        def wait(self, timeout):
            if now[0] + timeout >= self.finish:
                now[0] = max(now[0], self.finish)
                self.returncode = self.code
                return self.code
            now[0] += timeout
            raise subprocess.TimeoutExpired("fixed probe", timeout)

    def start(*, finish=2, code=0, payload=b"diagnostic output", clean=True):
        process = Process(finish, code, payload)

        def launch(command, **kwargs):
            launched.append((command, kwargs))
            return process

        def cleanup(child):
            assert child is process
            now[0] += 6
            if child.returncode is None:
                child.returncode = -15
            return clean, 1

        monkeypatch.setattr(investigation.subprocess, "Popen", launch)
        monkeypatch.setattr(investigation, "cleanup_owned", cleanup)
        return process

    def run(**kwargs):
        return investigation.supervise(
            source,
            output,
            probe="distributed",
            hook_mode="historical",
            trace=False,
            timeout=60,
            clock=lambda: now[0],
            **kwargs,
        )

    return SimpleNamespace(
        source=source,
        output=output,
        now=now,
        start=start,
        run=run,
        launched=launched,
        reaped=reaped,
    )


@pytest.mark.parametrize("child_code,expected", [(0, 0), (1, 1), (-11, 139)])
def test_process_result_and_signal_are_preserved_with_bounded_evidence(
    harness, child_code, expected
):
    harness.start(code=child_code, payload=b"x" * 300_000)
    result = harness.run()
    assert result["returncode"] == child_code
    assert result["exit_code"] == expected
    assert result["outcome"] == ("no-reproduction" if expected == 0 else "failed-observation")
    assert result["retained_output_bytes"] == investigation.MAX_OUTPUT_BYTES
    assert result["output_bytes"] == 300_000
    assert result["last_pytest_phase"]["ordinal"] == 20
    assert json.loads((harness.output / "receipt.json").read_text()) == result
    command, options = harness.launched[0]
    assert options["cwd"] == harness.source
    assert options["start_new_session"] is True
    owned = Path(options["env"]["RAY_TMPDIR"])
    assert owned.parent == harness.output.parent
    assert not owned.is_relative_to(harness.output)
    assert str(harness.output / "pytest-phase.bin") in command


def test_deadline_reserves_cleanup_and_retains_phase_without_normal_observer_exit(harness):
    harness.start(finish=500)
    result = harness.run()
    assert result["timed_out"] is True
    assert result["exit_code"] == 124
    assert result["cleanup_complete"] is True
    assert result["elapsed_seconds"] == 60 - investigation.CLEANUP_RESERVE + 6
    assert result["last_pytest_phase"]["phase"] == "call"


def test_polling_reaps_while_launcher_runs_without_sampling_resources_ten_times_faster(harness):
    harness.start(finish=3)
    result = harness.run()
    assert len(harness.reaped) >= 29
    assert set(harness.reaped) == {12345}
    assert 3 <= result["resources"]["sample_count"] <= 5
    assert result["adopted_zombies_reaped_during_probe"] == 0


def test_reaper_only_waits_for_adopted_direct_zombies(monkeypatch):
    waited = []

    def status(pid):
        if pid == 10:
            pytest.fail("The Popen launcher must never be inspected or reaped here")
        if pid == 13:
            raise investigation.psutil.NoSuchProcess(pid)
        return (
            investigation.psutil.STATUS_RUNNING if pid == 11 else investigation.psutil.STATUS_ZOMBIE
        )

    children = [
        SimpleNamespace(pid=pid, status=lambda pid=pid: status(pid)) for pid in range(10, 16)
    ]

    def direct_children(*, recursive):
        assert recursive is False
        return children

    def waitpid(pid, flags):
        assert flags == 1
        waited.append(pid)
        if pid == 14:
            raise ChildProcessError
        return (0, 0) if pid == 15 else (pid, 0)

    monkeypatch.setattr(
        investigation.psutil, "Process", lambda: SimpleNamespace(children=direct_children)
    )
    monkeypatch.setattr(investigation.os, "waitpid", waitpid, raising=False)
    monkeypatch.setattr(investigation.os, "WNOHANG", 1, raising=False)
    assert investigation.reap_adopted_zombies(10) == 1
    assert waited == [12, 14, 15]


@pytest.mark.skipif(
    sys.platform != "linux", reason="Adoption and process-group reaping require Linux"
)
def test_supervisor_reaps_orphan_group_during_probe_and_preserves_launcher_status(tmp_path):
    # Isolate PR_SET_CHILD_SUBREAPER from the test runner and its own Popen children.
    source, output, owned = (tmp_path / name for name in ("source", "output", "owned"))
    source.mkdir()
    owned.mkdir()
    (source / "uv.lock").write_text("unchanged lock")
    pid_file = tmp_path / "orphan.pid"
    orphan = tmp_path / "orphan.py"
    orphan.write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n"
    )
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys, time
            from pathlib import Path
            subprocess.Popen([sys.executable, {str(orphan)!r}])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if Path({str(pid_file)!r}).exists() and Path({str(pid_file)!r}).read_text().isdigit():
                    break
                time.sleep(0.01)
            assert Path({str(pid_file)!r}).read_text().isdigit()
            """
        )
    )
    probe = tmp_path / "probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            import os, signal, subprocess, sys, time
            from pathlib import Path
            import psutil
            launcher = subprocess.Popen([sys.executable, {str(launcher)!r}], start_new_session=True)
            assert launcher.wait(timeout=5) == 0
            orphan_pid = int(Path({str(pid_file)!r}).read_text())
            deadline = time.monotonic() + 2
            while psutil.Process(orphan_pid).ppid() != os.getppid():
                assert time.monotonic() < deadline, 'orphan was not adopted by supervisor'
                time.sleep(0.01)
            os.killpg(launcher.pid, signal.SIGKILL)
            deadline = time.monotonic() + 2
            while True:
                try:
                    os.killpg(launcher.pid, 0)
                except ProcessLookupError:
                    break
                assert time.monotonic() < deadline, 'adopted zombie kept the group visible'
                time.sleep(0.01)
            raise SystemExit(7)
            """
        )
    )
    driver = tmp_path / "supervisor.py"
    driver.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys, time
            from pathlib import Path
            from types import SimpleNamespace
            import psutil
            sys.path.insert(0, {str(investigation.ROOT)!r})
            from scripts import investigate_native_exit as investigation
            investigation.enable_subreaper()
            direct = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(9)'])
            deadline = time.monotonic() + 3
            while psutil.Process(direct.pid).status() != psutil.STATUS_ZOMBIE:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert investigation.reap_adopted_zombies(direct.pid) == 0
            assert direct.returncode is None
            assert direct.wait(timeout=1) == 9
            investigation.source_identity = lambda source, timeout: {{
                'lock_sha256_before': investigation.digest(source / 'uv.lock')
            }}
            investigation.metadata.version = lambda package: 'test-version'
            investigation.subprocess.run = lambda *args, **kwargs: SimpleNamespace(stdout='uv 0.13.7')
            investigation.create_ray_tmpdir = lambda: Path({str(owned)!r})
            investigation.resource_sample = lambda path: {{}}
            investigation.probe_command = lambda *args: [sys.executable, {str(probe)!r}]
            receipt = investigation.supervise(
                Path({str(source)!r}), Path({str(output)!r}), probe='distributed',
                hook_mode='disabled', trace=False, timeout=30,
            )
            assert receipt['returncode'] == receipt['exit_code'] == 7, receipt
            assert receipt['adopted_zombies_reaped_during_probe'] == 1, receipt
            assert receipt['cleanup_complete'] is True, receipt
            """
        )
    )
    result = subprocess.run(
        [sys.executable, str(driver)], capture_output=True, text=True, timeout=40, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cleanup_failure_fails_closed_even_when_pytest_passes(harness):
    harness.start(clean=False)
    result = harness.run()
    assert result["returncode"] == 0
    assert result["exit_code"] == 2
    assert result["cleanup_complete"] is False


def test_lock_mutation_rejects_environment_parity_without_hiding_child_result(harness, monkeypatch):
    harness.start()

    def cleanup(process):
        (harness.source / "uv.lock").write_text("changed by child uv")
        return True, 0

    monkeypatch.setattr(investigation, "cleanup_owned", cleanup)
    result = harness.run()
    assert result["returncode"] == 0 and result["exit_code"] == 2
    assert result["lock_unchanged"] is False


def test_non_linux_entrypoint_rejects_before_creating_output(monkeypatch, tmp_path):
    monkeypatch.setattr(investigation.sys, "platform", "win32")
    with pytest.raises(SystemExit, match="requires Linux"):
        investigation.main(
            [
                "--source",
                str(tmp_path),
                "--output",
                str(tmp_path / "out"),
                "--probe",
                "distributed",
                "--hook-mode",
                "historical",
            ]
        )
    assert not (tmp_path / "out").exists()


def test_output_inside_source_and_reused_trial_are_rejected(harness):
    with pytest.raises(ValueError, match="outside"):
        investigation.supervise(
            harness.source,
            harness.source / "out",
            probe="distributed",
            hook_mode="historical",
            trace=False,
        )
    harness.output.mkdir()
    (harness.output / "receipt.json").write_text("previous evidence")
    with pytest.raises(ValueError, match="already contains"):
        harness.run()
    assert not harness.launched
