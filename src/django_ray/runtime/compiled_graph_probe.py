"""Subprocess-isolated smoke probe for Ray Compiled Graph.

The parent process always evaluates :mod:`django_ray.runtime.compiled_graph` first.
Unsupported combinations return a structured guard result without starting a child.
Native compilation is only attempted in a disposable subprocess with a hard timeout.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from django_ray.runtime.compiled_graph import (
    CompiledGraphCapabilityDecision,
    CompiledGraphReason,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    CompiledGraphTransport,
    evaluate_compiled_graph_support,
)

PROBE_SCHEMA_VERSION = 2
UNSAFE_PROBE_ENV = "DJANGO_RAY_ALLOW_UNSAFE_COMPILED_GRAPH_PROBE"
_CHILD_RECORD_PATH_ENV = "DJANGO_RAY_COMPILED_GRAPH_PROBE_RECORD_PATH"
_CHILD_START_GATE_PATH_ENV = "DJANGO_RAY_COMPILED_GRAPH_PROBE_START_GATE_PATH"
_CHILD_START_GATE_TIMEOUT_SECONDS = 10.0
_MAX_OUTPUT_CHARS = 16_384
_MAX_ERROR_CHARS = 8_192
_MAX_STATUS_CHARS = 64
_MAX_ERROR_TYPE_CHARS = 1_024
_MAX_CONTROL_DETAILS_BYTES = 32_768
# Two maximum-length error fields can each require twelve ASCII bytes per Unicode
# code point when JSON escapes a non-BMP character. This independent channel remains
# bounded while leaving ample room for the status, error type, and bounded details.
_MAX_CONTROL_RECORD_BYTES = 512 * 1_024


class CompiledGraphProbeStatus(StrEnum):
    """Stable terminal classifications produced by the probe parent."""

    SUCCESS = "success"
    UNSUPPORTED_GUARD = "unsupported_guard"
    PYTHON_FAILURE = "python_failure"
    TIMEOUT = "timeout"
    SIGNAL = "signal"
    NATIVE_CRASH = "native_crash"


@dataclass(frozen=True)
class CompiledGraphProbeRequest:
    """Serializable request for one minimal CPU Compiled Graph execution."""

    topology: CompiledGraphTopology
    submission_transport: CompiledGraphSubmissionTransport = (
        CompiledGraphSubmissionTransport.DIRECT_RAY_CORE
    )
    transport: CompiledGraphTransport = CompiledGraphTransport.CPU_SHARED_MEMORY
    candidate_native: bool = False
    unsafe_native: bool = False

    def asdict(self) -> dict[str, Any]:
        return {
            "schema_version": PROBE_SCHEMA_VERSION,
            "topology": self.topology.value,
            "submission_transport": self.submission_transport.value,
            "transport": self.transport.value,
            "candidate_native": self.candidate_native,
            "unsafe_native": self.unsafe_native,
        }

    @classmethod
    def fromdict(cls, payload: dict[str, Any]) -> CompiledGraphProbeRequest:
        if payload.get("schema_version") != PROBE_SCHEMA_VERSION:
            raise ValueError("Unsupported Compiled Graph probe request schema")
        return cls(
            topology=CompiledGraphTopology(payload["topology"]),
            submission_transport=CompiledGraphSubmissionTransport(payload["submission_transport"]),
            transport=CompiledGraphTransport(payload["transport"]),
            candidate_native=payload.get("candidate_native") is True,
            unsafe_native=payload.get("unsafe_native") is True,
        )


@dataclass(frozen=True)
class CompiledGraphProbeOutcome:
    """Bounded, JSON-safe observation from a single subprocess probe."""

    status: CompiledGraphProbeStatus
    decision: CompiledGraphCapabilityDecision
    duration_seconds: float
    exit_code: int | None = None
    termination_signal: int | None = None
    native_exit_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    traceback_tail: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    details: dict[str, Any] | None = None
    schema_version: int = PROBE_SCHEMA_VERSION

    @property
    def successful(self) -> bool:
        return self.status is CompiledGraphProbeStatus.SUCCESS

    def asdict(self) -> dict[str, Any]:
        """Return an end-to-end bounded, JSON-safe probe record."""
        details = _bounded_json_object(self.details)
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "successful": self.successful,
            "decision": self.decision.asdict(),
            "duration_seconds": round(self.duration_seconds, 6),
            "exit_code": self.exit_code,
            "termination_signal": self.termination_signal,
            "native_exit_code": _tail(self.native_exit_code, limit=_MAX_STATUS_CHARS),
            "error_type": _tail(self.error_type, limit=_MAX_ERROR_TYPE_CHARS) or None,
            "error_message": _tail(self.error_message, limit=_MAX_ERROR_CHARS) or None,
            "traceback_tail": _tail(self.traceback_tail, limit=_MAX_ERROR_CHARS) or None,
            "stdout_tail": _tail(self.stdout_tail),
            "stderr_tail": _tail(self.stderr_tail),
            "details": details or {},
        }


def run_compiled_graph_probe(
    request: CompiledGraphProbeRequest,
    *,
    timeout_seconds: float = 60.0,
    python_executable: str | None = None,
    _command: Sequence[str] | None = None,
    _environment: dict[str, str] | None = None,
) -> CompiledGraphProbeOutcome:
    """Run one hard-bounded native probe, or return before spawn when guarded."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    started_at = time.monotonic()
    decision = evaluate_compiled_graph_support(
        request.topology,
        request.transport,
        submission_transport=request.submission_transport,
    )
    candidate_canary = _candidate_canary_requested(decision, request)
    bypassable = decision.reason not in {
        CompiledGraphReason.UNSUPPORTED_TOPOLOGY,
        CompiledGraphReason.UNSUPPORTED_SUBMISSION_TRANSPORT,
        CompiledGraphReason.UNSUPPORTED_TRANSPORT,
    }
    if (
        not decision.eligible
        and not candidate_canary
        and (not request.unsafe_native or not bypassable)
    ):
        return CompiledGraphProbeOutcome(
            status=CompiledGraphProbeStatus.UNSUPPORTED_GUARD,
            decision=decision,
            duration_seconds=time.monotonic() - started_at,
            error_type="CompiledGraphUnsupportedError",
            error_message=decision.message,
        )

    environment = dict(os.environ if _environment is None else _environment)
    if request.unsafe_native and environment.get(UNSAFE_PROBE_ENV) != "1":
        return CompiledGraphProbeOutcome(
            status=CompiledGraphProbeStatus.PYTHON_FAILURE,
            decision=decision,
            duration_seconds=time.monotonic() - started_at,
            error_type="UnsafeProbeNotAcknowledged",
            error_message=(
                f"Set {UNSAFE_PROBE_ENV}=1 as well as unsafe_native=True to acknowledge "
                "that the reproduction may terminate a Python or Ray worker process."
            ),
        )

    environment.setdefault("RAY_CGRAPH_submit_timeout", "10")
    environment.setdefault("RAY_CGRAPH_get_timeout", "10")
    environment.setdefault("RAY_DEDUP_LOGS", "0")
    environment.setdefault("RAY_USAGE_STATS_ENABLED", "0")
    with tempfile.TemporaryDirectory(prefix="django-ray-cgraph-probe-") as control_directory:
        child_record_path = Path(control_directory) / "child-record.json"
        child_start_gate_path = Path(control_directory) / "child-start-ready"
        environment[_CHILD_RECORD_PATH_ENV] = str(child_record_path)
        environment[_CHILD_START_GATE_PATH_ENV] = str(child_start_gate_path)
        return _run_probe_subprocess(
            request,
            decision=decision,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            python_executable=python_executable,
            command_override=_command,
            environment=environment,
            child_record_path=child_record_path,
            child_start_gate_path=child_start_gate_path,
        )


def _run_probe_subprocess(
    request: CompiledGraphProbeRequest,
    *,
    decision: CompiledGraphCapabilityDecision,
    started_at: float,
    timeout_seconds: float,
    python_executable: str | None,
    command_override: Sequence[str] | None,
    environment: dict[str, str],
    child_record_path: Path,
    child_start_gate_path: Path,
) -> CompiledGraphProbeOutcome:
    command = list(
        command_override
        or _child_command(
            request,
            python_executable=python_executable or sys.executable,
        )
    )
    popen_options: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": environment,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True

    process = subprocess.Popen(command, **popen_options)
    stdout_capture = _LiveOutputCapture(process.stdout)
    stderr_capture = _LiveOutputCapture(process.stderr)
    windows_job: _WindowsJob | None = None
    try:
        windows_job = _arm_child_process(process, child_start_gate_path)
    except OSError as error:
        stdout = stdout_capture.finish()
        stderr = stderr_capture.finish()
        return CompiledGraphProbeOutcome(
            status=CompiledGraphProbeStatus.PYTHON_FAILURE,
            decision=decision,
            duration_seconds=time.monotonic() - started_at,
            exit_code=process.returncode,
            error_type="ProcessContainmentError",
            error_message=_tail(str(error), limit=_MAX_ERROR_CHARS),
            stdout_tail=stdout,
            stderr_tail=stderr,
        )

    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        _terminate_process_tree(process, windows_job=windows_job)
        _wait_after_termination(process)
        if windows_job is not None:
            windows_job.close()

    stdout = stdout_capture.finish()
    stderr = stderr_capture.finish()
    if timed_out:
        return CompiledGraphProbeOutcome(
            status=CompiledGraphProbeStatus.TIMEOUT,
            decision=decision,
            duration_seconds=time.monotonic() - started_at,
            exit_code=process.returncode,
            error_type="TimeoutExpired",
            error_message=f"Native probe exceeded the {timeout_seconds:g}s wall-clock limit.",
            stdout_tail=stdout,
            stderr_tail=stderr,
        )

    duration = time.monotonic() - started_at
    child_record = _read_child_record(child_record_path)
    if child_record is not None:
        try:
            status = CompiledGraphProbeStatus(child_record["status"])
        except (KeyError, ValueError):
            status = CompiledGraphProbeStatus.NATIVE_CRASH
            child_record = {
                "error_type": "InvalidChildRecord",
                "error_message": "Probe child emitted an invalid status record.",
            }
        if process.returncode == 0 or status in {
            CompiledGraphProbeStatus.PYTHON_FAILURE,
            CompiledGraphProbeStatus.NATIVE_CRASH,
        }:
            return CompiledGraphProbeOutcome(
                status=status,
                decision=decision,
                duration_seconds=duration,
                exit_code=process.returncode,
                error_type=_bounded_optional(child_record.get("error_type")),
                error_message=_bounded_optional(child_record.get("error_message")),
                traceback_tail=_bounded_optional(child_record.get("traceback_tail")),
                stdout_tail=_tail(stdout),
                stderr_tail=_tail(stderr),
                details=_json_object(child_record.get("details")),
            )

    return _abrupt_process_outcome(
        decision,
        duration_seconds=duration,
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _child_command(
    request: CompiledGraphProbeRequest,
    *,
    python_executable: str,
) -> tuple[str, ...]:
    payload = json.dumps(request.asdict(), sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    return (
        python_executable,
        "-m",
        "django_ray.runtime.compiled_graph_probe",
        "--child-payload-b64",
        encoded,
    )


def _candidate_canary_requested(
    decision: CompiledGraphCapabilityDecision,
    request: CompiledGraphProbeRequest,
) -> bool:
    return bool(
        request.candidate_native
        and decision.candidate
        and decision.reason
        in {
            CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE,
            CompiledGraphReason.INCOMPLETE_CAPABILITY_CONTEXT,
        }
    )


def _execute_child_request(request: CompiledGraphProbeRequest) -> dict[str, Any]:
    decision = evaluate_compiled_graph_support(
        request.topology,
        request.transport,
        submission_transport=request.submission_transport,
    )
    if request.unsafe_native and os.environ.get(UNSAFE_PROBE_ENV) != "1":
        return {
            "status": CompiledGraphProbeStatus.PYTHON_FAILURE.value,
            "error_type": "UnsafeProbeNotAcknowledged",
            "error_message": f"Set {UNSAFE_PROBE_ENV}=1 before an unsafe native probe.",
        }
    candidate_canary = _candidate_canary_requested(decision, request)
    unsafe_semantic_rejection = decision.reason in {
        CompiledGraphReason.UNSUPPORTED_TOPOLOGY,
        CompiledGraphReason.UNSUPPORTED_SUBMISSION_TRANSPORT,
        CompiledGraphReason.UNSUPPORTED_TRANSPORT,
    }
    if (
        not decision.eligible
        and not candidate_canary
        and (not request.unsafe_native or unsafe_semantic_rejection)
    ):
        return {
            "status": CompiledGraphProbeStatus.UNSUPPORTED_GUARD.value,
            "error_type": "CompiledGraphUnsupportedError",
            "error_message": decision.message,
        }
    try:
        details = _run_native_probe(request.topology)
    except BaseException as error:
        native_worker_crash = _looks_like_native_worker_crash(error)
        return {
            "status": (
                CompiledGraphProbeStatus.NATIVE_CRASH.value
                if native_worker_crash
                else CompiledGraphProbeStatus.PYTHON_FAILURE.value
            ),
            "error_type": type(error).__name__,
            "error_message": _tail(str(error), limit=_MAX_ERROR_CHARS),
            "traceback_tail": _tail(traceback.format_exc(), limit=_MAX_ERROR_CHARS),
            "details": {"native_worker_crash": native_worker_crash},
        }
    return {
        "status": CompiledGraphProbeStatus.SUCCESS.value,
        "details": details,
    }


def _run_native_probe(topology: CompiledGraphTopology) -> dict[str, Any]:  # pragma: no cover
    if topology is CompiledGraphTopology.NESTED_RAY_TASK:
        return _run_nested_ray_task_probe()
    if topology in {
        CompiledGraphTopology.DIRECT_DRIVER,
        CompiledGraphTopology.RAY_JOB_DRIVER,
    }:
        return _run_driver_probe(topology)
    raise ValueError(f"Native probe does not implement topology {topology.value!r}")


def _run_driver_probe(topology: CompiledGraphTopology) -> dict[str, Any]:  # pragma: no cover
    import ray

    started_ray = not ray.is_initialized()
    if started_ray:
        if topology is CompiledGraphTopology.RAY_JOB_DRIVER:
            ray.init(address="auto", include_dashboard=False, logging_level="ERROR")
        else:
            ray.init(num_cpus=2, include_dashboard=False, logging_level="ERROR")
    try:
        return _execute_compiled_graph_once()
    finally:
        if started_ray:
            ray.shutdown()


def _run_nested_ray_task_probe() -> dict[str, Any]:  # pragma: no cover
    import ray

    started_ray = not ray.is_initialized()
    if started_ray:
        ray.init(num_cpus=2, include_dashboard=False, logging_level="ERROR")
    owner = ray.remote(max_retries=0)(_nested_compiler_owner)
    try:
        return ray.get(owner.remote(), timeout=30)
    finally:
        if started_ray:
            ray.shutdown()


def _nested_compiler_owner() -> dict[str, Any]:  # pragma: no cover
    return _execute_compiled_graph_once()


def _execute_compiled_graph_once() -> dict[str, Any]:  # pragma: no cover
    import ray
    from ray.dag import InputNode

    class ProbeEchoActor:
        def echo(self, value: str) -> str:
            return value

    actor = ray.remote(ProbeEchoActor).remote()
    compiled = None
    try:
        with InputNode() as graph_input:
            graph = actor.echo.bind(graph_input)
        compiled = graph.experimental_compile()
        result = ray.get(compiled.execute("django-ray-compiled-graph-probe"), timeout=20)
        if result != "django-ray-compiled-graph-probe":
            raise AssertionError(f"Unexpected Compiled Graph result: {result!r}")
        return {
            "ray_version": ray.__version__,
            "result_verified": True,
        }
    finally:
        if compiled is not None:
            compiled.teardown(kill_actors=True)
        else:
            ray.kill(actor, no_restart=True)


def _looks_like_native_worker_crash(error: BaseException) -> bool:
    names = {type(error).__name__}
    names.update(base.__name__ for base in type(error).__mro__)
    message = str(error).lower()
    return bool(
        names.intersection({"WorkerCrashedError", "ActorDiedError", "RayActorError"})
        or "worker died unexpectedly" in message
        or "worker crashed" in message
        or "system error" in message
    )


def _abrupt_process_outcome(
    decision: CompiledGraphCapabilityDecision,
    *,
    duration_seconds: float,
    exit_code: int | None,
    stdout: str,
    stderr: str,
) -> CompiledGraphProbeOutcome:
    if exit_code is not None and exit_code < 0:
        return CompiledGraphProbeOutcome(
            status=CompiledGraphProbeStatus.SIGNAL,
            decision=decision,
            duration_seconds=duration_seconds,
            exit_code=exit_code,
            termination_signal=-exit_code,
            error_type="ProcessSignal",
            error_message=f"Probe process terminated by signal {-exit_code}.",
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
        )

    native_exit_code = None
    if exit_code is not None:
        native_exit_code = f"0x{exit_code & 0xFFFFFFFF:08X}"
    return CompiledGraphProbeOutcome(
        status=CompiledGraphProbeStatus.NATIVE_CRASH,
        decision=decision,
        duration_seconds=duration_seconds,
        exit_code=exit_code,
        native_exit_code=native_exit_code,
        error_type="AbruptProcessExit",
        error_message="Probe process exited without a valid structured child record.",
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
    )


class _BoundedTailBuffer:
    """Thread-safe tail storage whose retained memory never exceeds its limit."""

    def __init__(self, limit: int = _MAX_OUTPUT_CHARS) -> None:
        self._limit = limit
        self._value = ""
        self._lock = threading.Lock()

    def append(self, value: str) -> None:
        with self._lock:
            self._value = (self._value + value)[-self._limit :]

    def value(self) -> str:
        with self._lock:
            return self._value


class _LiveOutputCapture:
    """Continuously drain one child pipe into a bounded tail buffer."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._buffer = _BoundedTailBuffer()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        if self._stream is None:
            return
        try:
            while chunk := self._stream.read(4096):
                self._buffer.append(chunk)
        except (OSError, ValueError):
            pass

    def finish(self) -> str:
        self._thread.join(timeout=2)
        if self._thread.is_alive() and self._stream is not None:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass
            self._thread.join(timeout=1)
        return self._buffer.value()


class _WindowsJob:
    """Windows Job Object that retains descendants after the root process exits."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        self._kernel32 = kernel32
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            self._handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not kernel32.AssignProcessToJobObject(self._handle, process_handle):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _arm_child_process(
    process: subprocess.Popen[str],
    start_gate_path: Path,
) -> _WindowsJob | None:
    """Establish descendant containment before allowing native child work."""
    windows_job: _WindowsJob | None = None
    try:
        if os.name == "nt":
            windows_job = _WindowsJob(process)
        _release_child_start_gate(start_gate_path)
    except OSError:
        _terminate_process_tree(process, windows_job=windows_job)
        _wait_after_termination(process)
        if windows_job is not None:
            windows_job.close()
        raise
    return windows_job


def _release_child_start_gate(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"ready")


def _wait_for_parent_start_gate(
    *,
    timeout_seconds: float = _CHILD_START_GATE_TIMEOUT_SECONDS,
) -> None:
    gate_value = os.environ.get(_CHILD_START_GATE_PATH_ENV, "").strip()
    if not gate_value:
        raise RuntimeError("Compiled Graph probe child has no parent start gate")
    gate_path = Path(gate_value)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if gate_path.read_bytes() == b"ready":
                return
        except FileNotFoundError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("Compiled Graph probe parent did not release the child start gate")
        time.sleep(0.01)


def _terminate_process_tree(
    process: subprocess.Popen[str],
    *,
    windows_job: _WindowsJob | None,
) -> None:
    if os.name == "nt":
        if windows_job is not None:
            windows_job.terminate()
        else:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _wait_after_termination(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _read_child_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            encoded = stream.read(_MAX_CONTROL_RECORD_BYTES + 1)
    except OSError:
        return None
    if len(encoded) > _MAX_CONTROL_RECORD_BYTES:
        return {"status": "invalid-oversized-control-record"}
    try:
        record = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "invalid-control-record"}
    if not isinstance(record, dict):
        return {"status": "invalid-control-record"}
    return record


def _write_child_record(path: Path, record: dict[str, Any]) -> None:
    normalized: dict[str, Any] = {
        "status": _tail(str(record.get("status", "")), limit=_MAX_STATUS_CHARS)
    }
    error_type = record.get("error_type")
    if error_type is not None:
        normalized["error_type"] = _tail(str(error_type), limit=_MAX_ERROR_TYPE_CHARS)
    for field in ("error_message", "traceback_tail"):
        value = record.get(field)
        if value is not None:
            normalized[field] = _tail(str(value), limit=_MAX_ERROR_CHARS)

    details = _bounded_json_object(record.get("details"))
    if details is not None:
        normalized["details"] = details

    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > _MAX_CONTROL_RECORD_BYTES:
        raise ValueError("Compiled Graph probe control record exceeds its safe bound")

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)


def _tail(value: str | bytes | None, *, limit: int = _MAX_OUTPUT_CHARS) -> str:
    text = _coerce_output(value)
    return text[-limit:]


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _join_output(first: str | bytes | None, second: str | bytes | None) -> str:
    first_text = _coerce_output(first)
    second_text = _coerce_output(second)
    if first_text and second_text.startswith(first_text):
        return second_text
    return first_text + second_text


def _bounded_optional(value: Any) -> str | None:
    if value is None:
        return None
    return _tail(str(value), limit=_MAX_ERROR_CHARS)


def _json_object(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _bounded_json_object(value: Any) -> dict[str, Any] | None:
    details = _json_object(value)
    if details is None:
        return None
    try:
        encoded = json.dumps(
            details,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError):
        return {"serialization_error": True}
    if len(encoded) <= _MAX_CONTROL_DETAILS_BYTES:
        return details
    return {
        "control_record_details_truncated": True,
        "original_bytes": len(encoded),
        "sha256": sha256(encoded).hexdigest(),
    }


def _decode_child_payload(value: str) -> CompiledGraphProbeRequest:
    try:
        payload = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid Compiled Graph probe child payload") from error
    if not isinstance(payload, dict):
        raise ValueError("Compiled Graph probe child payload must be an object")
    return CompiledGraphProbeRequest.fromdict(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topology",
        choices=[item.value for item in CompiledGraphTopology],
        default=CompiledGraphTopology.DIRECT_DRIVER.value,
    )
    parser.add_argument(
        "--submission-transport",
        choices=[item.value for item in CompiledGraphSubmissionTransport],
        default=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE.value,
    )
    parser.add_argument(
        "--transport",
        choices=[item.value for item in CompiledGraphTransport],
        default=CompiledGraphTransport.CPU_SHARED_MEMORY.value,
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--candidate-native",
        action="store_true",
        help=(
            "Run a native canary only when the versioned policy marks the exact tuple as "
            "a candidate awaiting smoke evidence."
        ),
    )
    parser.add_argument(
        "--unsafe-native",
        action="store_true",
        help=(
            "Bypass only the runtime/platform allowlist for reproduction or canary use. "
            f"Also requires {UNSAFE_PROBE_ENV}=1."
        ),
    )
    parser.add_argument(
        "--require-success",
        action="store_true",
        help="Return a non-zero exit status unless the native probe succeeds.",
    )
    parser.add_argument("--child-payload-b64", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)

    if arguments.child_payload_b64:
        try:
            _wait_for_parent_start_gate()
            request = _decode_child_payload(arguments.child_payload_b64)
            record = _execute_child_request(request)
        except BaseException as error:
            record = {
                "status": CompiledGraphProbeStatus.PYTHON_FAILURE.value,
                "error_type": type(error).__name__,
                "error_message": _tail(str(error), limit=_MAX_ERROR_CHARS),
                "traceback_tail": _tail(traceback.format_exc(), limit=_MAX_ERROR_CHARS),
            }
        child_record_path = os.environ.get(_CHILD_RECORD_PATH_ENV, "").strip()
        if not child_record_path:
            print("Compiled Graph probe child has no private control-record path.", file=sys.stderr)
            return 2
        _write_child_record(Path(child_record_path), record)
        return 0 if record["status"] in {"success", "unsupported_guard"} else 2

    request = CompiledGraphProbeRequest(
        topology=CompiledGraphTopology(arguments.topology),
        submission_transport=CompiledGraphSubmissionTransport(arguments.submission_transport),
        transport=CompiledGraphTransport(arguments.transport),
        candidate_native=arguments.candidate_native,
        unsafe_native=arguments.unsafe_native,
    )
    outcome = run_compiled_graph_probe(request, timeout_seconds=arguments.timeout_seconds)
    print(json.dumps(outcome.asdict(), indent=2, sort_keys=True))
    if arguments.require_success and not outcome.successful:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
