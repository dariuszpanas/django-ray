"""Run inside GDB and retain bounded native symbols without inspecting values.

The debugger follows only the pytest interpreter, detaching its subprocesses.
On a signal it records at most 16 threads and 32 frames per thread, including
the stopped thread first. Signals are then delivered normally to the inferior.
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
    print(json.dumps({"pytest_exit_observer": stage, **values}, sort_keys=True), flush=True)


class NativeObserver:
    """Preserve the inferior exit outcome independently of GDB's default exit."""

    def __init__(self, debugger: Any) -> None:
        self.debugger = debugger
        self.returncode: int | None = None
        self.last_signal = 0
        self.last_signal_name = ""
        self.stops = 0

    def stopped(self, event: Any) -> None:
        """Remember the signal and collect symbols while native frames exist."""
        self.stops += 1
        name = getattr(event, "stop_signal", "")
        value = getattr(signal, name, None)
        self.last_signal = int(value) if isinstance(value, int) and value > 0 else 0
        self.last_signal_name = name if self.last_signal else ""
        emit("native_signal", signal=self.last_signal, stop=self.stops)
        if self.stops > MAX_STOPS:
            return
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
        debugger.events.stop.connect(self.stopped)
        debugger.events.exited.connect(self.exited)
        emit("native_debugger_started", version=str(debugger.VERSION)[:40])
        # GDB quotes --args for its startup shell. Disabling that shell passes
        # quoting escapes literally, corrupting arguments containing spaces.
        debugger.execute("run")
        while self.returncode is None and self.stops <= MAX_STOPS:
            if self.stops == 0 or self.last_signal == 0:
                break
            # Explicit delivery also preserves signals GDB normally consumes,
            # such as SIGINT/SIGTRAP, instead of turning them into success.
            # GDB's numeric signal mapping differs from the host's POSIX IDs.
            debugger.execute(f"signal {self.last_signal_name}")
        if self.returncode is None:
            emit("native_exit_unobserved")
            return 2
        return 128 - self.returncode if self.returncode < 0 else self.returncode


def main() -> None:
    """The helper is sourced by GDB, never by the test interpreter."""
    gdb = import_module("gdb")

    code = 2
    try:
        code = NativeObserver(gdb).run()
    except Exception:
        emit("native_debugger_error")
    gdb.execute(f"quit {code}")


if __name__ == "__main__":
    main()
