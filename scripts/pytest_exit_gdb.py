"""Run inside GDB and retain bounded native symbols without inspecting values.

The debugger follows only the pytest interpreter, detaching its subprocesses.
On a signal it records at most 16 threads and 32 frames per thread, including
the stopped thread first. Signals are then delivered normally to the inferior.
Opt-in Linux x86-64 libc _exit tracing also records nonzero exit callers before
their frames disappear. The owned breakpoint permits pending shared-library
resolution; direct exit syscalls bypass it and retain status without a caller.
Stopping at libc still perturbs scheduling in bounded diagnostic reproductions.
No core dumps, Python objects, frame arguments, locals, or environment are read.
The enclosing CI job supplies the execution deadline.
"""

from __future__ import annotations

import json
import signal
from importlib import import_module
from pathlib import Path
from typing import Any

MAX_THREADS = 16
MAX_FRAMES = 32
MAX_STOPS = 4


def emit(stage: str, **values: Any) -> None:
    """Emit only explicitly selected bounded diagnostic fields."""
    print("\n" + json.dumps({"pytest_exit_observer": stage, **values}, sort_keys=True), flush=True)


class NativeObserver:
    """Preserve the inferior exit outcome independently of GDB's default exit."""

    def __init__(self, debugger: Any, *, trace_exits: bool = False) -> None:
        self.debugger = debugger
        self.returncode: int | None = None
        self.last_signal = 0
        self.last_signal_name = ""
        self.stops = 0
        self.trace_exits = trace_exits
        self.exit_breakpoint: Any = None
        self.exit_entry_seen = False
        self.resume_command: str | None = None

    def install_exit_breakpoint(self) -> bool:
        """Validate the inferior ABI before using its numeric status register."""
        debugger = self.debugger
        architecture = debugger.selected_inferior().architecture().name()
        pointer_size = debugger.lookup_type("void").pointer().sizeof
        if architecture != "i386:x86-64" or pointer_size != 8:
            emit("native_exit_trace_unsupported_abi")
            return False
        # libc may not be loaded before `run`. The Linux glibc _Exit symbol is
        # an alias of _exit, also reached after normal exit/atexit processing.
        # A code breakpoint avoids ptrace syscall-entry/return stops throughout
        # the workload, which failed during hosted process teardown.
        debugger.execute("set breakpoint pending on")
        point = debugger.Breakpoint("_exit", type=debugger.BP_BREAKPOINT, internal=True)
        if point.type != debugger.BP_BREAKPOINT:
            emit("native_exit_breakpoint_unavailable")
            return False
        self.exit_breakpoint = point
        point.silent = True
        emit(
            "native_exit_trace_enabled",
            mechanism="libc-_exit",
            pending=bool(point.pending),
            timing_perturbed=True,
        )
        return True

    def stopped(self, event: Any) -> None:
        """Separate owned libc stops from signals without injecting signal 0."""
        self.stops += 1
        self.resume_command = None
        breakpoints = getattr(event, "breakpoints", ())
        if self.exit_breakpoint is not None and self.exit_breakpoint in breakpoints:
            self.last_signal = 0
            self.last_signal_name = ""
            # The owned point identifies the libc entry, including a location
            # resolved after startup. Silent breakpoints may omit MI details.
            reason = getattr(event, "details", {}).get("reason")
            if reason not in (None, "breakpoint-hit"):
                emit("native_exit_breakpoint_unexpected_stop")
                return
            status = int(self.debugger.newest_frame().read_register("rdi")) & 255
            self.exit_entry_seen = True
            emit("native_exit_entry", mechanism="libc-_exit", status=status, stop=self.stops)
            self.resume_command = "continue"
            if status and self.stops <= MAX_STOPS:
                self.capture_stacks()
            return
        name = getattr(event, "stop_signal", "")
        value = getattr(signal, name, None)
        self.last_signal = int(value) if isinstance(value, int) and value > 0 else 0
        self.last_signal_name = name if self.last_signal else ""
        if not self.last_signal:
            emit("native_stop_unrecognized")
            return
        self.resume_command = f"signal {self.last_signal_name}"
        emit("native_signal", signal=self.last_signal, stop=self.stops)
        if self.stops > MAX_STOPS:
            return
        self.capture_stacks()

    def capture_stacks(self) -> None:
        """Retain only bounded native symbols, never argument or object values."""
        debugger = self.debugger
        stopped_thread = debugger.selected_thread()
        threads = list(debugger.selected_inferior().threads())
        threads.sort(key=lambda thread: (thread != stopped_thread, thread.global_num))
        for thread in threads[:MAX_THREADS]:
            frames: list[dict[str, str]] = []
            truncated = False
            try:
                thread.switch()
                frame = debugger.newest_frame()
                for _ in range(MAX_FRAMES):
                    if frame is None:
                        break
                    library = debugger.solib_name(frame.pc())
                    frames.append(
                        {
                            "symbol": (frame.name() or "unknown")[:192],
                            "library": Path(library).name[:80] if library else "unknown",
                            "pc": hex(int(frame.pc()))[:20],
                        }
                    )
                    frame = frame.older()
                truncated = frame is not None
            except Exception:
                emit("native_unwind_unavailable", thread=thread.global_num)
            emit(
                "native_stack",
                thread=thread.global_num,
                stopped=thread == stopped_thread,
                frames=frames,
                frames_truncated=truncated,
            )
        emit("native_threads", total=len(threads), captured=min(len(threads), MAX_THREADS))
        if stopped_thread is not None:
            stopped_thread.switch()

    def exited(self, event: Any) -> None:
        """GDB omits exit_code when the inferior terminates because of a signal."""
        code = getattr(event, "exit_code", None)
        self.returncode = int(code) if code is not None else -self.last_signal or 2
        if self.trace_exits and code is not None and not self.exit_entry_seen:
            emit(
                "native_exit_caller_unobserved",
                mechanism="libc-_exit",
                returncode=self.returncode,
            )
        emit(
            "native_process_exit",
            returncode=self.returncode,
            signal=-self.returncode if self.returncode < 0 else 0,
        )

    def run(self) -> int:
        """Continue through signal delivery; incomplete observation fails closed."""
        debugger = self.debugger
        for command in (
            "set confirm off",
            "set pagination off",
            "set auto-load off",
            "set debuginfod enabled off",
            "set print thread-events off",
            "set print inferior-events off",
            "set print frame-arguments none",
            "set print frame-info location",
            "set python print-stack none",
            "set disable-randomization off",
            "set detach-on-fork on",
            "set follow-fork-mode parent",
            "handle SIGPIPE nostop noprint pass",
        ):
            debugger.execute(command)
        if self.trace_exits and not self.install_exit_breakpoint():
            return 2
        debugger.events.stop.connect(self.stopped)
        debugger.events.exited.connect(self.exited)
        emit("native_debugger_started", version=str(debugger.VERSION)[:40])
        # GDB quotes --args for its startup shell. Disabling that shell passes
        # quoting escapes literally, corrupting arguments containing spaces.
        debugger.execute("run")
        while self.returncode is None and self.stops <= MAX_STOPS:
            if self.resume_command is None:
                break
            # Explicit delivery also preserves signals GDB normally consumes,
            # such as SIGINT/SIGTRAP, instead of turning them into success.
            # GDB's numeric signal mapping differs from the host's POSIX IDs.
            command = self.resume_command
            self.resume_command = None
            debugger.execute(command)
        if self.returncode is None:
            emit("native_exit_unobserved")
            return 2
        return 128 - self.returncode if self.returncode < 0 else self.returncode


def main() -> None:
    """The helper is sourced by GDB, never by the test interpreter."""
    gdb = import_module("gdb")

    code = 2
    try:
        code = NativeObserver(
            gdb, trace_exits=bool(gdb.convenience_variable("django_ray_trace_exits"))
        ).run()
    except Exception:
        emit("native_debugger_error")
    gdb.execute(f"quit {code}")


if __name__ == "__main__":
    main()
