"""The exit observer must preserve failures, including native child signals."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.observe_pytest_exit import observe, observe_native
from scripts.pytest_exit_gdb import MAX_FRAMES, MAX_THREADS, NativeObserver


@pytest.mark.parametrize("passes, expected", [(True, 0), (False, 1)])
def test_real_pytest_return_and_shutdown_are_observed(tmp_path, passes, expected):
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(f"def test_sample():\n    assert {passes}\n")
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    script = Path(__file__).resolve().parents[2] / "scripts" / "observe_pytest_exit.py"
    result = subprocess.run(
        [sys.executable, str(script), "--", "-q", str(test_file)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert [record["pytest_exit_observer"] for record in records] == [
        "child_started",
        "pytest_returned",
        "atexit_checkpoint",
        "process_exit",
    ]
    assert records[1]["returncode"] == expected
    assert records[-1]["returncode"] == expected


@pytest.mark.parametrize("code", [0, 1, 5, 139])
def test_observe_preserves_child_exit(code, capsys):
    assert observe([sys.executable, "-c", f"raise SystemExit({code})"]) == code
    assert json.loads(capsys.readouterr().out) == {
        "pytest_exit_observer": "process_exit",
        "returncode": code,
        "signal": 0,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX child signal return codes")
def test_observe_reports_signal_without_converting_to_success(capsys):
    code = observe(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]
    )
    assert code == 128 + signal.SIGTERM
    record = json.loads(capsys.readouterr().out)
    assert record["returncode"] == -signal.SIGTERM
    assert record["signal"] == signal.SIGTERM


def test_native_debugger_missing_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert observe_native([sys.executable, "-c", "raise SystemExit(0)"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "pytest_exit_observer": "native_debugger_unavailable"
    }


@pytest.mark.parametrize("code", [0, 5, 139])
def test_native_observer_retains_numeric_inferior_exit(code, capsys):
    observer = NativeObserver(None)
    observer.exited(SimpleNamespace(exit_code=code))
    assert observer.returncode == code
    record = json.loads(capsys.readouterr().out)
    assert record["returncode"] == code
    assert record["signal"] == 0


def test_native_observer_retains_signal_inferior_exit(capsys):
    observer = NativeObserver(None)
    observer.last_signal = signal.SIGSEGV
    observer.exited(SimpleNamespace())
    assert observer.returncode == -signal.SIGSEGV
    record = json.loads(capsys.readouterr().out)
    assert record["returncode"] == -signal.SIGSEGV
    assert record["signal"] == signal.SIGSEGV


def test_native_observer_unknown_inferior_exit_fails_closed(capsys):
    observer = NativeObserver(None)
    observer.exited(SimpleNamespace())
    assert observer.returncode == 2


@pytest.mark.parametrize("name, number", [("SIGBUS", 7), ("SIGUSR1", 10)])
def test_native_observer_forwards_signal_name_not_host_number(monkeypatch, name, number):
    # GDB numbers these signals differently from Linux. Model Linux on every host.
    monkeypatch.setattr(signal, name, number, raising=False)
    callbacks = {}
    commands = []

    def execute(command):
        commands.append(command)
        if command == "run":
            callbacks["stop"](SimpleNamespace(stop_signal=name))
        elif command == f"signal {name}":
            callbacks["exited"](SimpleNamespace())

    debugger = SimpleNamespace(
        VERSION="test-debugger",
        execute=execute,
        events=SimpleNamespace(
            stop=SimpleNamespace(connect=lambda callback: callbacks.update(stop=callback)),
            exited=SimpleNamespace(connect=lambda callback: callbacks.update(exited=callback)),
        ),
        selected_thread=lambda: None,
        selected_inferior=lambda: SimpleNamespace(threads=list),
    )
    assert NativeObserver(debugger).run() == 128 + number
    assert f"signal {name}" in commands
    assert f"signal {number}" not in commands


def test_native_stack_is_bounded_and_stopped_thread_is_first(capsys):
    class Frame:
        def name(self):
            return "native_function_" + "x" * 300

        def pc(self):
            return 1

        def older(self):
            return self

    class Thread:
        def __init__(self, number):
            self.global_num = number

        def switch(self):
            pass

    threads = [Thread(number) for number in range(MAX_THREADS + 4)]
    stopped = threads[-1]
    debugger = SimpleNamespace(
        selected_thread=lambda: stopped,
        selected_inferior=lambda: SimpleNamespace(threads=lambda: threads),
        newest_frame=Frame,
        solib_name=lambda pc: "/private/runtime/location/native.so",
    )
    observer = NativeObserver(debugger)
    observer.stopped(SimpleNamespace(stop_signal="SIGSEGV"))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    stacks = [record for record in records if record["pytest_exit_observer"] == "native_stack"]
    assert len(stacks) == MAX_THREADS
    assert stacks[0]["thread"] == stopped.global_num
    assert stacks[0]["stopped"] is True
    assert all(len(stack["frames"]) == MAX_FRAMES for stack in stacks)
    assert all(stack["frames_truncated"] for stack in stacks)
    for stack in stacks:
        for frame in stack["frames"]:
            assert set(frame) == {"symbol", "library", "pc"}
            assert len(frame["symbol"]) == 192
            assert frame["library"] == "native.so"
            assert frame["pc"] == "0x1"
    assert records[-1] == {
        "pytest_exit_observer": "native_threads",
        "total": MAX_THREADS + 4,
        "captured": MAX_THREADS,
    }


@pytest.mark.parametrize(
    "action, expected, expected_signal",
    [
        ("raise SystemExit(0)", 0, 0),
        ("raise SystemExit(5)", 5, 0),
        ("raise SystemExit(139)", 139, 0),
        ("os.kill(os.getpid(), signal.SIGSEGV)", 139, 11),
        ("os.kill(os.getpid(), signal.SIGABRT)", 134, 6),
        ("os.kill(os.getpid(), signal.SIGTERM)", 143, 15),
        ("os.kill(os.getpid(), signal.SIGBUS)", 135, 7),
        ("os.kill(os.getpid(), signal.SIGUSR1)", 138, 10),
        (
            "signal.signal(signal.SIGINT, signal.SIG_DFL); os.kill(os.getpid(), signal.SIGINT)",
            130,
            2,
        ),
    ],
)
def test_native_debugger_preserves_process_outcomes(action, expected, expected_signal):
    if sys.platform != "linux" or shutil.which("gdb") is None:
        if os.environ.get("DJANGO_RAY_REQUIRE_NATIVE_DEBUGGER") == "1":
            pytest.fail("Required Linux native debugger smoke needs GDB")
        pytest.skip("Native debugger smoke requires Linux and GDB")

    # The crash happens during interpreter shutdown, after the fixed checkpoint.
    child = (
        "import atexit, os, signal; "
        f"atexit.register(lambda: exec({action!r})); "
        "atexit.register(lambda: print('native-smoke-atexit', flush=True)); "
        "print('native-smoke-return', flush=True)"
        if expected_signal
        else action
    )
    launcher = (
        "import sys; from scripts.observe_pytest_exit import observe_native; "
        f"raise SystemExit(observe_native([sys.executable, '-u', '-c', {child!r}]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", launcher],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    exits = [
        record for record in records if record["pytest_exit_observer"] == "native_process_exit"
    ]
    assert exits == [
        {
            "pytest_exit_observer": "native_process_exit",
            "returncode": -expected_signal if expected_signal else expected,
            "signal": expected_signal,
        }
    ]
    if expected_signal:
        assert result.stdout.index("native-smoke-return") < result.stdout.index(
            "native-smoke-atexit"
        )
        stacks = [record for record in records if record["pytest_exit_observer"] == "native_stack"]
        assert stacks
        assert stacks[0]["stopped"] is True
        assert stacks[0]["frames"]


def test_native_debugger_keeps_pytest_marker_expression(tmp_path):
    if sys.platform != "linux" or shutil.which("gdb") is None:
        if os.environ.get("DJANGO_RAY_REQUIRE_NATIVE_DEBUGGER") == "1":
            pytest.fail("Required Linux native debugger smoke needs GDB")
        pytest.skip("Native debugger smoke requires Linux and GDB")
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import pytest\n"
        "def test_passes():\n    assert True\n"
        "@pytest.mark.live_cluster\n"
        "def test_must_be_deselected():\n    assert False\n"
    )
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    script = Path(__file__).resolve().parents[2] / "scripts" / "observe_pytest_exit.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--native-debug",
            "--",
            "-q",
            "-m",
            "not live_cluster",
            str(sample),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 deselected" in result.stdout
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    stages = [record["pytest_exit_observer"] for record in records]
    assert stages.index("pytest_returned") < stages.index("atexit_checkpoint")
    assert stages.index("atexit_checkpoint") < stages.index("native_process_exit")
