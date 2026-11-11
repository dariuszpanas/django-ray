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

from scripts.observe_pytest_exit import (
    PHASE_SLOT_BYTES,
    PhaseJournal,
    observe,
    observe_native,
    read_phase,
)
from scripts.pytest_exit_gdb import MAX_FRAMES, MAX_THREADS, NativeObserver
from scripts.pytest_exit_gdb import emit as emit_native


@pytest.mark.parametrize("passes, expected", [(True, 0), (False, 1)])
@pytest.mark.parametrize("patch_json", [False, True])
def test_real_pytest_return_and_shutdown_are_observed(tmp_path, passes, expected, patch_json):
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        "import json\n"
        "def test_sample(monkeypatch):\n"
        "    def broken_json(*args, **kwargs):\n"
        "        raise TypeError('serialization intentionally unavailable')\n"
        f"    if {patch_json}:\n"
        "        monkeypatch.setattr(json, 'dumps', broken_json)\n"
        f"    assert {passes}\n"
    )
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
    stages = [record["pytest_exit_observer"] for record in records]
    assert [
        stage
        for stage in stages
        if stage in {"child_started", "pytest_returned", "atexit_checkpoint", "process_exit"}
    ] == [
        "child_started",
        "pytest_returned",
        "atexit_checkpoint",
        "process_exit",
    ]
    assert (
        next(record for record in records if record["pytest_exit_observer"] == "pytest_returned")[
            "returncode"
        ]
        == expected
    )
    assert (
        next(record for record in records if record["pytest_exit_observer"] == "process_exit")[
            "returncode"
        ]
        == expected
    )
    assert records[-1]["pytest_exit_observer"] == "last_pytest_phase"
    assert records[-1]["phase"] == "atexit_checkpoint"
    assert records[-1]["ordinal"] == 1
    assert stages.index("collection_start") < stages.index("collection_finished")
    assert stages.index("session_finish_start") < stages.index("session_finish_finished")


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
    records = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
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


def test_phase_journal_is_bounded_and_recovers_previous_slot(tmp_path):
    path = tmp_path / "phase.bin"
    path.write_bytes(b"\0" * PHASE_SLOT_BYTES * 2)
    journal = PhaseJournal(path)
    assert not os.get_inheritable(journal.fd)
    for ordinal in range(1, 501):
        journal.ordinal = ordinal
        journal.record("test_call")
    journal.record("test_teardown")
    journal.close()
    assert path.stat().st_size == PHASE_SLOT_BYTES * 2
    assert read_phase(path) == {"phase": "test_teardown", "sequence": 501, "ordinal": 500}
    with path.open("r+b") as stream:
        stream.seek(PHASE_SLOT_BYTES)
        stream.write(b"torn newest slot")
    assert read_phase(path) == {"phase": "test_call", "sequence": 500, "ordinal": 500}


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_abrupt_pytest_exit_retains_phase_outside_fd_capture(tmp_path, phase):
    fixture_body = (
        "    os._exit(1)\n    yield\n"
        if phase == "setup"
        else "    yield\n    os._exit(1)\n"
        if phase == "teardown"
        else "    yield\n"
    )
    (tmp_path / "test_sample.py").write_text(
        "import os, pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def fixture():\n" + fixture_body + "def test_sample():\n"
        "    print('captured-private-sentinel', flush=True)\n"
        + ("    os._exit(1)\n" if phase == "call" else "    assert True\n")
    )
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    script = Path(__file__).resolve().parents[2] / "scripts" / "observe_pytest_exit.py"
    phase_path = tmp_path / "retained-phase.bin"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--phase-journal",
            str(phase_path),
            "--",
            "-q",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "captured-private-sentinel" not in result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    stages = [record["pytest_exit_observer"] for record in records]
    assert "runtest_loop_start" in stages
    assert "pytest_returned" not in stages
    assert "atexit_checkpoint" not in stages
    assert records[-2] == {"pytest_exit_observer": "process_exit", "returncode": 1, "signal": 0}
    assert records[-1]["phase"] == f"test_{phase}"
    assert records[-1]["ordinal"] == 1
    assert phase_path.stat().st_size == PHASE_SLOT_BYTES * 2
    retained_phase = read_phase(phase_path)
    assert retained_phase is not None
    assert retained_phase["phase"] == f"test_{phase}"


@pytest.mark.parametrize("status", [0, 1, 139, 256, -1])
@pytest.mark.parametrize("reason", [None, "breakpoint-hit"])
def test_native_exit_breakpoint_resolves_and_preserves_final_status(status, reason, capsys):
    callbacks = {}
    commands = []
    point = SimpleNamespace(type=1, silent=False, pending=True)

    def read_register(name):
        assert name == "rdi"
        return status

    frame = SimpleNamespace(read_register=read_register)

    def create_breakpoint(spec, **kwargs):
        assert spec == "_exit"
        assert kwargs == {"type": 1, "internal": True}
        assert "set breakpoint pending on" in commands
        assert "run" not in commands
        return point

    def execute(command, **kwargs):
        commands.append(command)
        if command == "run":
            # Model libc loading after a pending breakpoint was installed.
            point.pending = False
            callbacks["stop"](SimpleNamespace(breakpoints=[point], details={"reason": reason}))
        elif command == "continue":
            callbacks["exited"](SimpleNamespace(exit_code=status & 255))

    debugger = SimpleNamespace(
        VERSION="test-debugger",
        BP_BREAKPOINT=1,
        Breakpoint=create_breakpoint,
        execute=execute,
        lookup_type=lambda name: SimpleNamespace(pointer=lambda: SimpleNamespace(sizeof=8)),
        events=SimpleNamespace(
            stop=SimpleNamespace(connect=lambda callback: callbacks.update(stop=callback)),
            exited=SimpleNamespace(connect=lambda callback: callbacks.update(exited=callback)),
        ),
        selected_thread=lambda: None,
        newest_frame=lambda: frame,
        selected_inferior=lambda: SimpleNamespace(
            threads=list, architecture=lambda: SimpleNamespace(name=lambda: "i386:x86-64")
        ),
    )
    assert NativeObserver(debugger, trace_exits=True).run() == status & 255
    assert commands.count("continue") == 1
    assert not any(command.startswith("signal ") for command in commands)
    assert not any("catch syscall" in command for command in commands)
    records = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert (
        next(record for record in records if record["pytest_exit_observer"] == "native_exit_entry")[
            "status"
        ]
        == status & 255
    )
    assert records[0] == {
        "pytest_exit_observer": "native_exit_trace_enabled",
        "mechanism": "libc-_exit",
        "pending": True,
        "timing_perturbed": True,
    }
    stages = [record["pytest_exit_observer"] for record in records]
    assert ("native_threads" in stages) is bool(status & 255)
    assert "native_exit_caller_unobserved" not in stages
    assert records[-1] == {
        "pytest_exit_observer": "native_process_exit",
        "returncode": status & 255,
        "signal": 0,
    }
    assert point.silent


def test_native_record_starts_after_unterminated_progress(capsys):
    print("partial test progress", end="")
    emit_native("native_exit_entry", status=1)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "partial test progress"
    assert json.loads(lines[1]) == {"pytest_exit_observer": "native_exit_entry", "status": 1}


def test_unowned_breakpoint_is_not_resumed_as_libc_exit(capsys):
    observer = NativeObserver(None, trace_exits=True)
    observer.exit_breakpoint = object()
    observer.stopped(SimpleNamespace(breakpoints=[object()], details={"reason": "breakpoint-hit"}))
    assert observer.resume_command is None
    assert "native_stop_unrecognized" in capsys.readouterr().out


@pytest.mark.parametrize("status", [0, 1, 139])
def test_native_exit_without_libc_breakpoint_preserves_status_without_caller(status, capsys):
    observer = NativeObserver(None, trace_exits=True)
    observer.exited(SimpleNamespace(exit_code=status))
    assert observer.returncode == status
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert records == [
        {
            "pytest_exit_observer": "native_exit_caller_unobserved",
            "mechanism": "libc-_exit",
            "returncode": status,
        },
        {"pytest_exit_observer": "native_process_exit", "returncode": status, "signal": 0},
    ]


def test_owned_exit_breakpoint_rejects_unexpected_stop_reason(capsys):
    observer = NativeObserver(None, trace_exits=True)
    observer.exit_breakpoint = object()
    observer.stopped(
        SimpleNamespace(breakpoints=[observer.exit_breakpoint], details={"reason": "syscall-entry"})
    )
    assert observer.resume_command is None
    assert not observer.exit_entry_seen
    assert "native_exit_breakpoint_unexpected_stop" in capsys.readouterr().out


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Fork descriptor ownership needs POSIX")
def test_fork_child_cannot_publish_parent_phase_or_atexit_marker(tmp_path):
    sample = tmp_path / "test_fork.py"
    sample.write_text(
        "import atexit, os\n"
        "def test_fork():\n"
        "    child = os.fork()\n"
        "    if child == 0:\n"
        "        atexit._run_exitfuncs()\n"
        "        os._exit(0)\n"
        "    assert os.waitpid(child, 0)[1] == 0\n"
    )
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    script = Path(__file__).resolve().parents[2] / "scripts" / "observe_pytest_exit.py"
    result = subprocess.run(
        [sys.executable, str(script), "--", "-q", str(sample)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    stages = [record["pytest_exit_observer"] for record in records]
    assert stages.count("atexit_checkpoint") == 1
    assert stages.index("pytest_returned") < stages.index("atexit_checkpoint")
    assert records[-1]["ordinal"] == 1


def test_native_exit_trace_keeps_record_after_partial_progress():
    if sys.platform != "linux" or shutil.which("gdb") is None:
        if os.environ.get("DJANGO_RAY_REQUIRE_NATIVE_DEBUGGER") == "1":
            pytest.fail("Required Linux native debugger smoke needs GDB")
        pytest.skip("Native debugger smoke requires Linux x86-64 and GDB")
    child = "import os; os.write(1, b'partial-progress'); os._exit(1)"
    launcher = (
        "import sys; from scripts.observe_pytest_exit import observe_native; "
        f"raise SystemExit(observe_native([sys.executable, '-u', '-c', {child!r}], trace_exits=True))"
    )
    result = subprocess.run(
        [sys.executable, "-c", launcher],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    entry = next(
        record for record in records if record["pytest_exit_observer"] == "native_exit_entry"
    )
    assert entry["status"] == 1
    assert "partial-progress\n" in result.stdout


@pytest.mark.parametrize("status", [0, 1])
@pytest.mark.parametrize("action", ["raise SystemExit(STATUS)", "ctypes.CDLL(None)._Exit(STATUS)"])
def test_native_exit_trace_preserves_multithreaded_process_teardown(action, status):
    if sys.platform != "linux" or shutil.which("gdb") is None:
        if os.environ.get("DJANGO_RAY_REQUIRE_NATIVE_DEBUGGER") == "1":
            pytest.fail("Required Linux native debugger smoke needs GDB")
        pytest.skip("Native debugger smoke requires Linux x86-64 and GDB")
    import platform

    if platform.machine() != "x86_64":
        pytest.skip("Libc exit register decoding is supported on x86-64 only")
    # Every daemon thread reaches the barrier before the main thread exits.
    # This exercises exit with live native threads without Ray or infrastructure.
    child = (
        "import ctypes, threading\n"
        "ready = threading.Barrier(5)\n"
        "wait = threading.Event()\n"
        "def idle():\n"
        "    ready.wait()\n"
        "    wait.wait()\n"
        "for _ in range(4):\n"
        "    threading.Thread(target=idle, daemon=True).start()\n"
        "ready.wait()\n" + action.replace("STATUS", str(status))
    )
    launcher = (
        "import sys; from scripts.observe_pytest_exit import observe_native; "
        f"raise SystemExit(observe_native([sys.executable, '-u', '-c', {child!r}], trace_exits=True))"
    )
    result = subprocess.run(
        [sys.executable, "-c", launcher],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == status, result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    stages = [record["pytest_exit_observer"] for record in records]
    assert "native_debugger_error" not in stages
    assert "native_exit_unobserved" not in stages
    assert "native_exit_caller_unobserved" not in stages
    entries = [
        record for record in records if record["pytest_exit_observer"] == "native_exit_entry"
    ]
    assert len(entries) == 1
    assert entries[0]["status"] == status
    assert entries[0]["mechanism"] == "libc-_exit"
    assert ("native_stack" in stages) is bool(status)
    exits = [
        record for record in records if record["pytest_exit_observer"] == "native_process_exit"
    ]
    assert exits == [
        {"pytest_exit_observer": "native_process_exit", "returncode": status, "signal": 0}
    ]


@pytest.mark.parametrize("architecture, pointer_size", [("aarch64", 8), ("i386:x86-64", 4)])
def test_native_exit_trace_rejects_unsupported_inferior_before_launch(architecture, pointer_size):
    commands = []
    debugger = SimpleNamespace(
        execute=commands.append,
        lookup_type=lambda name: SimpleNamespace(
            pointer=lambda: SimpleNamespace(sizeof=pointer_size)
        ),
        selected_inferior=lambda: SimpleNamespace(
            architecture=lambda: SimpleNamespace(name=lambda: architecture)
        ),
    )
    assert NativeObserver(debugger, trace_exits=True).run() == 2
    assert "run" not in commands


@pytest.mark.parametrize(
    "action, returned, intercepted",
    [
        ("os._exit(1)", False, True),
        ("ctypes.CDLL(None)._Exit(1)", False, True),
        ("ctypes.CDLL(None).exit(1)", False, True),
        ("ctypes.CDLL(None).syscall(231, 1)", False, False),
        ("assert False", True, True),
        ("pytest.exit('synthetic stop', returncode=1)", True, True),
    ],
)
def test_native_exit_trace_retains_caller_and_captured_phase(
    tmp_path, action, returned, intercepted
):
    if sys.platform != "linux" or shutil.which("gdb") is None:
        if os.environ.get("DJANGO_RAY_REQUIRE_NATIVE_DEBUGGER") == "1":
            pytest.fail("Required Linux native debugger smoke needs GDB")
        pytest.skip("Native debugger smoke requires Linux x86-64 and GDB")
    import platform

    if platform.machine() != "x86_64":
        pytest.skip("Libc exit register decoding is supported on x86-64 only")
    sample = tmp_path / "test_native_sample.py"
    sample.write_text(f"import ctypes, os, pytest\ndef test_sample():\n    {action}\n")
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    script = Path(__file__).resolve().parents[2] / "scripts" / "observe_pytest_exit.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--native-debug",
            "--trace-native-exit",
            "--",
            "-q",
            str(sample),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    stages = [record["pytest_exit_observer"] for record in records]
    assert ("pytest_returned" in stages) is returned
    entries = [
        record for record in records if record["pytest_exit_observer"] == "native_exit_entry"
    ]
    stacks = [record for record in records if record["pytest_exit_observer"] == "native_stack"]
    if intercepted:
        assert len(entries) == 1 and entries[0]["status"] == 1
        assert entries[0]["mechanism"] == "libc-_exit"
        assert stacks and stacks[0]["stopped"] and stacks[0]["frames"]
        assert "native_exit_caller_unobserved" not in stages
    else:
        assert entries == [] and stacks == []
        assert "native_exit_caller_unobserved" in stages
    assert records[-1]["phase"] == ("atexit_checkpoint" if returned else "test_call")
    assert records[-1]["ordinal"] == 1
    assert next(
        record for record in records if record["pytest_exit_observer"] == "native_process_exit"
    ) == {
        "pytest_exit_observer": "native_process_exit",
        "returncode": 1,
        "signal": 0,
    }
