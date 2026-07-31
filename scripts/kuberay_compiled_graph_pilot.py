"""Run the pinned Linux/KubeRay Compiled Graph capability pilot.

The host-side runner is deliberately limited to the dedicated
``django-ray-cgraph-pilot`` namespace and an explicitly named Kubernetes
context. Native beta execution happens in a process-group-contained child in
the Ray head pod. The machine-readable output contains an allowlisted profile;
it never copies environment variables, Kubernetes Secrets, or pod logs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from uuid import uuid4

from django_ray.runtime.compiled_graph import (
    _PROFILE_DISTRIBUTIONS,
    CompiledGraphReason,
    CompiledGraphRuntimeIdentity,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    CompiledGraphTransport,
    detect_compiled_graph_runtime,
    evaluate_compiled_graph_support,
)
from django_ray.runtime.compiled_graph_probe import (
    _CHILD_RECORD_PATH_ENV,
    PROBE_SCHEMA_VERSION,
    CompiledGraphProbeRequest,
    CompiledGraphProbeStatus,
    _wait_for_parent_start_gate,
    _write_child_record,
    run_compiled_graph_probe,
)

PILOT_SCHEMA_VERSION = 1
PROFILE_NAME = "django-ray-cgraph-kuberay-cpu-v1"
PILOT_NAMESPACE = "django-ray-cgraph-pilot"
RAYCLUSTER_NAME = "django-ray-cgraph-pilot"
PILOT_IMAGE_REPOSITORY = "django-ray-cgraph-pilot"
PILOT_PROFILE_LABEL_KEY = "django-ray.io/capability-profile"
PILOT_RUN_LABEL_KEY = "django-ray.io/pilot-run"
PILOT_NAMESPACE_UID_ANNOTATION_KEY = "django-ray.io/pilot-namespace-uid"
PILOT_LABEL = f"{PILOT_PROFILE_LABEL_KEY}={PROFILE_NAME}"
BLOCKER_TRACKERS = (
    "https://github.com/dariuszpanas/django-ray/issues/154",
    "https://github.com/ray-project/ray/issues/43836",
    "https://github.com/ray-project/ray/issues/59127",
)
CLEANUP_RETRY_DELAYS_SECONDS = (0, 5, 15, 30)
MUTABLE_OBJECT_CLEANUP_CLASSIFICATION = "ray_mutable_object_shared_memory_reclamation_blocked"
EXACT_PILOT_PROFILE_MATCH = "EXACT_PILOT_PROFILE_MATCH"
PILOT_PROFILE_MISMATCH = "PILOT_PROFILE_MISMATCH"
EXPECTED_CONTEXT_PATTERN = re.compile(r"^[a-zA-Z0-9._:@/-]{1,128}$")
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID_PATTERN = re.compile(r"docker://[0-9a-f]{64}")
PILOT_RUN_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
KUBERNETES_UID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
RAY_START_PARAMETER_KINDS = frozenset({"value", "valueless-true-switch"})
RAY_START_VALUE_LEXICAL_FORMS = frozenset({"equals-value", "separate-value"})
CRI_IMAGE_ID_PATTERN = re.compile(
    r"(?:docker://(?P<local>sha256:[0-9a-f]{64})|"
    r"docker-pullable://(?P<reference>[^@\s]{1,512})@"
    r"(?P<pullable>sha256:[0-9a-f]{64}))"
)
# Ray's PlasmaObjectHeader::Init uses ``<pid>-<Unix epoch nanoseconds>``.
RAY_MUTABLE_OBJECT_SEMAPHORE_PATTERN = re.compile(
    r"sem\.(?P<kind>hdr|obj)(?P<pair_id>[0-9]{1,10}-[0-9]{16,20})"
)
BLOCKED_EVIDENCE_FILENAME_PATTERN = re.compile(
    r"compiled-graph-kuberay-blocked-\d{4}-\d{2}-\d{2}(?:-[a-z0-9-]+)?\.json"
)
MAX_CAPTURE_CHARS = 32_768
MAX_STRUCTURED_CAPTURE_BYTES = 1024 * 1024
MAX_RETAINED_EVIDENCE_BYTES = 512 * 1024
MAX_BUILD_CONTEXT_FILES = 4_096
MAX_BUILD_CONTEXT_FILE_BYTES = 8 * 1024 * 1024
MAX_BUILD_CONTEXT_CONTENT_BYTES = 32 * 1024 * 1024
MAX_BUILD_CONTEXT_ARCHIVE_BYTES = 64 * 1024 * 1024
COMMAND_SHUTDOWN_TIMEOUT_SECONDS = 5.0
BUILD_CONTEXT_PATHS = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "src",
    "scripts/kuberay_compiled_graph_pilot.py",
    "k8s/pilots/compiled-graph",
)
REQUIRED_BUILD_CONTEXT_FILES = frozenset(
    {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "src/django_ray/__init__.py",
        "scripts/kuberay_compiled_graph_pilot.py",
        "k8s/pilots/compiled-graph/Dockerfile",
        "k8s/pilots/compiled-graph/Dockerfile.dockerignore",
        "k8s/pilots/compiled-graph/profile.json",
        "k8s/pilots/compiled-graph/raycluster.yaml",
    }
)
EXPECTED_BUILD_CONTEXT_IGNORE_RULES = (
    "**",
    "!pyproject.toml",
    "!README.md",
    "!LICENSE",
    "!src/",
    "!src/**",
    "!scripts/",
    "!scripts/kuberay_compiled_graph_pilot.py",
    "!k8s/",
    "!k8s/pilots/",
    "!k8s/pilots/compiled-graph/",
    "!k8s/pilots/compiled-graph/Dockerfile",
    "!k8s/pilots/compiled-graph/Dockerfile.dockerignore",
    "!k8s/pilots/compiled-graph/profile.json",
    "!k8s/pilots/compiled-graph/raycluster.yaml",
)
BLOCKED_EVIDENCE_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "evidence_id",
        "profile_name",
        "profile_id",
        "candidate_native",
        "supported_product_execution",
        "promotion_eligible",
        "pilot_evidence_passed",
        "started_at",
        "completed_at",
        "source_revision",
        "image",
        "image_id",
        "configuration_id",
        "rendered_manifest_id",
        "kubernetes_context",
        "namespace",
        "namespace_lease",
        "raycluster_lease",
        "docker",
        "kuberay_operator",
        "kubernetes",
        "profile",
        "pods",
        "near_neighbor",
        "hard_timeout",
        "topologies",
        "cleanup",
        "failure",
        "zero_residual_state",
    }
)
KUBERNETES_NODE_RESOURCE_KEYS = frozenset(
    {
        "cpu",
        "ephemeral-storage",
        "hugepages-1Gi",
        "hugepages-2Mi",
        "memory",
        "pods",
    }
)
ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = ROOT / "pyproject.toml"
PILOT_DIRECTORY = ROOT / "k8s" / "pilots" / "compiled-graph"
PROFILE_PATH = PILOT_DIRECTORY / "profile.json"
MANIFEST_PATH = PILOT_DIRECTORY / "raycluster.yaml"
DOCKERFILE_PATH = PILOT_DIRECTORY / "Dockerfile"
DOCKERIGNORE_PATH = PILOT_DIRECTORY / "Dockerfile.dockerignore"


class PilotError(RuntimeError):
    """Raised when the pilot cannot prove one of its fail-closed invariants."""


class PilotApplicationError(RuntimeError):
    """Expected application-level failure used by the native probe."""


@dataclass(frozen=True)
class CommandResult:
    """Bounded result of one external command."""

    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class NamespaceLease:
    """Exact create-response identity proving ownership of one pilot namespace."""

    name: str
    uid: str
    run_token: str

    def asdict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "uid": self.uid,
            "run_token": self.run_token,
            "profile_name": PROFILE_NAME,
        }


@dataclass(frozen=True)
class RayClusterLease:
    """Exact create-response identity for the pilot RayCluster."""

    name: str
    uid: str
    namespace_uid: str
    run_token: str

    def asdict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "uid": self.uid,
            "namespace_uid": self.namespace_uid,
            "run_token": self.run_token,
            "profile_name": PROFILE_NAME,
        }


def _tail(value: str, *, limit: int = MAX_CAPTURE_CHARS) -> str:
    return value if len(value) <= limit else value[-limit:]


def _append_bounded_tail(buffer: bytearray, chunk: bytes, *, limit: int) -> None:
    """Retain the newest bytes from a stream without allowing buffer growth."""

    if len(chunk) >= limit:
        buffer[:] = chunk[-limit:]
        return
    overflow = len(buffer) + len(chunk) - limit
    if overflow > 0:
        del buffer[:overflow]
    buffer.extend(chunk)


def _drain_command_stream(
    stream: BinaryIO,
    buffer: bytearray,
    *,
    preserve_complete: bool,
    overflow: threading.Event,
) -> None:
    """Drain one pipe while retaining either a complete cap or a rolling tail."""

    try:
        while chunk := stream.read(64 * 1024):
            if preserve_complete:
                remaining = MAX_STRUCTURED_CAPTURE_BYTES - len(buffer)
                if len(chunk) > remaining:
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    overflow.set()
                    return
                buffer.extend(chunk)
            else:
                _append_bounded_tail(buffer, chunk, limit=MAX_CAPTURE_CHARS)
    finally:
        stream.close()


def _write_command_input(stream: BinaryIO, value: bytes) -> None:
    try:
        stream.write(value)
        stream.flush()
    except BrokenPipeError:
        pass
    finally:
        stream.close()


def _windows_process_handle(process: subprocess.Popen[bytes]) -> int:
    try:
        return int(vars(process)["_handle"])
    except (KeyError, TypeError, ValueError) as error:  # pragma: no cover - Windows invariant
        raise PilotError("Windows command omitted its native process handle") from error


def _create_windows_command_job(process: subprocess.Popen[bytes]) -> int:
    """Atomically contain a suspended Windows command in a kill-on-close Job."""

    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_job(None, None)
    if not handle:
        raise PilotError(f"could not create a Windows command Job: error {ctypes.get_last_error()}")
    information = ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x00002000
    if not set_information(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
        error_code = ctypes.get_last_error()
        close_handle(handle)
        raise PilotError(f"could not configure a Windows command Job: error {error_code}")
    process_handle = wintypes.HANDLE(_windows_process_handle(process))
    if not assign_process(handle, process_handle):
        error_code = ctypes.get_last_error()
        close_handle(handle)
        raise PilotError(f"could not contain a Windows command: error {error_code}")
    return int(handle)


def _resume_windows_command(process: subprocess.Popen[bytes]) -> None:
    """Resume a command created suspended after its Job boundary is installed."""

    import ctypes
    from ctypes import wintypes

    resume_process = ctypes.WinDLL("ntdll").NtResumeProcess
    resume_process.argtypes = [wintypes.HANDLE]
    resume_process.restype = ctypes.c_long
    status = int(resume_process(wintypes.HANDLE(_windows_process_handle(process))))
    if status != 0:
        raise PilotError(f"could not resume a contained Windows command: status {status}")


def _terminate_windows_command_job(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    terminate_job = ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject
    terminate_job.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_job.restype = wintypes.BOOL
    if not terminate_job(wintypes.HANDLE(handle), 1):
        raise PilotError(
            f"could not terminate a Windows command Job: error {ctypes.get_last_error()}"
        )


def _close_windows_command_job(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(wintypes.HANDLE(handle))


def _stop_command_process(
    process: subprocess.Popen[bytes],
    *,
    windows_job_handle: int | None,
) -> None:
    """Terminate the complete command tree, including inherited-pipe descendants."""

    if os.name == "nt":
        if windows_job_handle is None:  # pragma: no cover - construction is fail closed
            raise PilotError("Windows command has no process-tree containment Job")
        _terminate_windows_command_job(windows_job_handle)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _wait_for_command_shutdown(
    process: subprocess.Popen[bytes],
    threads: list[threading.Thread],
) -> None:
    """Bound every post-termination process and pipe wait."""

    deadline = time.monotonic() + COMMAND_SHUTDOWN_TIMEOUT_SECONDS
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        raise PilotError("command process did not terminate within the shutdown bound") from error
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            raise PilotError("command pipe did not close within the shutdown bound")


def _run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout_seconds: float = 300,
    check: bool = True,
    cwd: Path = ROOT,
    preserve_stdout: bool = False,
) -> CommandResult:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise PilotError("command timeout must be a positive finite number")
    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004  # CREATE_SUSPENDED
        )
    else:
        process_options["start_new_session"] = True
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **process_options,
    )
    windows_job_handle: int | None = None
    try:
        if os.name == "nt":
            windows_job_handle = _create_windows_command_job(process)
            _resume_windows_command(process)
    except BaseException:
        try:
            if windows_job_handle is not None:
                _terminate_windows_command_job(windows_job_handle)
            else:
                process.kill()
            process.wait(timeout=COMMAND_SHUTDOWN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if windows_job_handle is not None:
            _close_windows_command_job(windows_job_handle)
        raise
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    stdout_thread = threading.Thread(
        target=_drain_command_stream,
        args=(process.stdout, stdout_buffer),
        kwargs={"preserve_complete": preserve_stdout, "overflow": stdout_overflow},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_command_stream,
        args=(process.stderr, stderr_buffer),
        kwargs={"preserve_complete": False, "overflow": stderr_overflow},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    input_thread: threading.Thread | None = None
    if input_text is not None:
        assert process.stdin is not None
        input_thread = threading.Thread(
            target=_write_command_input,
            args=(process.stdin, input_text.encode("utf-8")),
            daemon=True,
        )
        input_thread.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        command_threads = [stdout_thread, stderr_thread]
        if input_thread is not None:
            command_threads.append(input_thread)
        loop_error: BaseException | None = None
        try:
            while process.poll() is None:
                if stdout_overflow.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                time.sleep(min(0.01, remaining))
        except BaseException as error:
            loop_error = error
        termination_error: BaseException | None = None
        try:
            _stop_command_process(process, windows_job_handle=windows_job_handle)
        except BaseException as error:
            termination_error = error
            try:
                process.kill()
            except OSError:
                pass
        try:
            _wait_for_command_shutdown(process, command_threads)
        except BaseException as error:
            if termination_error is not None:
                raise PilotError(
                    "command tree termination and bounded pipe shutdown both failed"
                ) from error
            raise
        if termination_error is not None:
            raise termination_error
        if loop_error is not None:
            raise loop_error
    finally:
        if windows_job_handle is not None:
            _close_windows_command_job(windows_job_handle)

    if timed_out:
        raise PilotError(f"command timed out after {timeout_seconds:g}s: {command[0]}")
    if preserve_stdout and stdout_overflow.is_set():
        raise PilotError(
            f"structured command output exceeded the 1 MiB evidence-harness bound: {command[0]}"
        )
    try:
        stdout = stdout_buffer.decode("utf-8", errors="strict" if preserve_stdout else "replace")
    except UnicodeDecodeError as error:
        raise PilotError(f"structured command returned non-UTF-8 output: {command[0]}") from error
    stderr = stderr_buffer.decode("utf-8", errors="replace")
    result = CommandResult(
        stdout=stdout,
        stderr=stderr,
        returncode=process.returncode,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no bounded output"
        raise PilotError(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def _parse_json_command_output(
    result: CommandResult,
    command: list[str],
    *,
    expected_type: type[Any],
) -> Any:
    """Parse one complete bounded JSON document from a command."""

    if not result.stdout.strip():
        raise PilotError(f"structured command returned no JSON document: {command[0]}")

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-JSON numeric constant: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for name, item in pairs:
            if name in parsed:
                raise ValueError("duplicate JSON object key")
            parsed[name] = item
        return parsed

    try:
        value = json.loads(
            result.stdout,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise PilotError(
            f"structured command returned invalid or multiple JSON documents: {command[0]}"
        ) from error
    if not isinstance(value, expected_type):
        raise PilotError(
            f"structured command returned {type(value).__name__}, "
            f"expected {expected_type.__name__}: {command[0]}"
        )
    return value


def _run_json_command(
    command: list[str],
    *,
    expected_type: type[Any] = dict,
    input_text: str | None = None,
    timeout_seconds: float = 300,
    cwd: Path = ROOT,
) -> Any:
    """Run a command while preserving one JSON document up to the hard bound."""

    result = _run_command(
        command,
        input_text=input_text,
        timeout_seconds=timeout_seconds,
        cwd=cwd,
        preserve_stdout=True,
    )
    return _parse_json_command_output(result, command, expected_type=expected_type)


def _load_source_package_version(path: Path | None = None) -> str:
    pyproject_path = PYPROJECT_PATH if path is None else path
    try:
        with pyproject_path.open("rb") as pyproject_file:
            pyproject = tomllib.load(pyproject_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PilotError("Compiled Graph pilot could not read source package metadata") from error
    project = pyproject.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version or version != version.strip():
        raise PilotError("Compiled Graph pilot source package version is invalid")
    return version


def _load_profile() -> dict[str, Any]:
    value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not _is_exact_json_integer(
        value.get("schema_version"),
        expected=PILOT_SCHEMA_VERSION,
    ):
        raise PilotError("Compiled Graph pilot profile has an unsupported schema")
    if value.get("profile_name") != PROFILE_NAME:
        raise PilotError("Compiled Graph pilot profile name changed")
    cluster = value.get("cluster")
    if not isinstance(cluster, dict) or cluster.get("namespace") != PILOT_NAMESPACE:
        raise PilotError("Compiled Graph pilot profile must retain its dedicated namespace")
    dependencies = value.get("dependency_profile")
    source_version = _load_source_package_version()
    if not isinstance(dependencies, dict) or dependencies.get("django-ray") != source_version:
        raise PilotError(
            "Compiled Graph pilot django-ray dependency must match "
            f"source package version {source_version}"
        )
    return value


def _canonical_source_text_bytes(path: Path) -> bytes:
    """Return checkout-independent bytes for one tracked pilot text asset.

    Git stores the pilot assets with LF line endings, while a clean Windows
    checkout may materialize CRLF (or a mixture of CRLF and LF) under
    ``core.autocrlf``.  Configuration and policy identities must describe the
    immutable Git-archive/image inputs, not that checkout representation.

    The canonical contract is strict UTF-8 without a BOM or NUL bytes, permits
    only LF and CRLF line separators, preserves every other byte, and maps each
    CRLF pair to LF.  A bare carriage return is rejected instead of being
    assigned an ambiguous cross-platform identity.
    """

    try:
        value = path.read_bytes()
    except OSError as error:
        raise PilotError("pilot source-text asset could not be read") from error
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PilotError("pilot source-text asset is not strict UTF-8") from error
    if decoded.startswith("\ufeff"):
        raise PilotError("pilot source-text asset must not contain a UTF-8 BOM")
    if "\x00" in decoded:
        raise PilotError("pilot source-text asset must not contain NUL bytes")
    canonical = value.replace(b"\r\n", b"\n")
    if b"\r" in canonical:
        raise PilotError("pilot source-text asset contains a bare carriage return")
    return canonical


def _build_context_policy_identity(path: Path | None = None) -> str:
    policy_path = DOCKERIGNORE_PATH if path is None else path
    return f"sha256:{sha256(_canonical_source_text_bytes(policy_path)).hexdigest()}"


def _configuration_identity() -> str:
    digest = sha256()
    for path in (PROFILE_PATH, MANIFEST_PATH, DOCKERFILE_PATH, DOCKERIGNORE_PATH):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_canonical_source_text_bytes(path))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _profile_identity(profile: dict[str, Any]) -> str:
    serialized = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{sha256(serialized).hexdigest()}"


def _json_exact_equal(left: Any, right: Any) -> bool:
    """Compare JSON-shaped values without Python's bool/int equality coercion."""

    try:
        return json.dumps(
            left,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) == json.dumps(
            right,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


def _parse_exact_docker_image_id(value: Any) -> str:
    """Require Docker inspection's canonical immutable image-ID shape."""

    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise PilotError("Docker returned a noncanonical immutable image ID")
    return value


def _parse_exact_cri_image_id(value: Any) -> str:
    """Extract a digest only from an exact Docker CRI image-ID envelope."""

    if not isinstance(value, str):
        raise PilotError("Kubernetes returned a noncanonical immutable image ID")
    match = CRI_IMAGE_ID_PATTERN.fullmatch(value)
    if match is None:
        raise PilotError("Kubernetes returned a noncanonical immutable image ID")
    return str(match.group("local") or match.group("pullable"))


def _git_source_revision() -> str:
    revision_before = _run_command(["git", "rev-parse", "HEAD"]).stdout.strip().lower()
    status = _run_command(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        raise PilotError("pilot evidence requires a clean Git worktree")
    revision_after = _run_command(["git", "rev-parse", "HEAD"]).stdout.strip().lower()
    if revision_before != revision_after:
        raise PilotError("pilot source revision changed while cleanliness was checked")
    if not re.fullmatch(r"[0-9a-f]{40}", revision_after):
        raise PilotError("pilot source revision is not a full Git object ID")
    return revision_after


def _validated_archive_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    normalized = path.as_posix()
    windows_reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or re.fullmatch(r"[A-Za-z0-9._/-]+/?", value) is None
        or normalized != value.rstrip("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(
            part.rstrip(". ").split(".", 1)[0].upper() in windows_reserved for part in path.parts
        )
        or any(part.endswith((".", " ")) for part in path.parts)
        or any(ord(character) < 32 for character in value)
    ):
        raise PilotError("tracked build context contains an unsafe archive path")
    return path


def _tracked_build_inventory(
    revision: str,
    *,
    repository: Path,
) -> dict[str, tuple[int, int]]:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise PilotError("tracked build context requires a full Git commit ID")
    result = _run_command(
        [
            "git",
            "ls-tree",
            "-r",
            "-z",
            "--long",
            revision,
            "--",
            *BUILD_CONTEXT_PATHS,
        ],
        cwd=repository,
        preserve_stdout=True,
    )
    inventory: dict[str, tuple[int, int]] = {}
    total_bytes = 0
    for raw_record in result.stdout.split("\0"):
        if not raw_record:
            continue
        try:
            metadata_text, path_text = raw_record.split("\t", 1)
        except ValueError as error:
            raise PilotError("tracked build context inventory is malformed") from error
        match = re.fullmatch(
            r"(?P<mode>[0-9]{6}) blob [0-9a-f]{40,64} +(?P<size>[0-9]+)",
            metadata_text,
        )
        if match is None:
            raise PilotError("tracked build context contains a non-file Git object")
        path = _validated_archive_path(path_text).as_posix()
        mode_text = match.group("mode")
        if mode_text not in {"100644", "100755"}:
            raise PilotError("tracked build context contains a symlink or unsupported file mode")
        size = int(match.group("size"))
        if size > MAX_BUILD_CONTEXT_FILE_BYTES:
            raise PilotError("tracked build context contains an oversized file")
        if path in inventory:
            raise PilotError("tracked build context contains a duplicate path")
        if not (path in REQUIRED_BUILD_CONTEXT_FILES or path.startswith("src/")):
            raise PilotError("tracked build context contains a path outside the exact allowlist")
        inventory[path] = (int(mode_text[-3:], 8), size)
        total_bytes += size
        if len(inventory) > MAX_BUILD_CONTEXT_FILES:
            raise PilotError("tracked build context contains too many files")
        if total_bytes > MAX_BUILD_CONTEXT_CONTENT_BYTES:
            raise PilotError("tracked build context content exceeded its byte bound")
    if not REQUIRED_BUILD_CONTEXT_FILES.issubset(inventory):
        raise PilotError("tracked build context omitted a required source or pilot file")
    return inventory


def _validate_extracted_build_policy(context: Path) -> str:
    policy_path = context / "k8s" / "pilots" / "compiled-graph" / "Dockerfile.dockerignore"
    canonical_policy = _canonical_source_text_bytes(policy_path)
    rules = tuple(
        line.strip()
        for line in canonical_policy.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if rules != EXPECTED_BUILD_CONTEXT_IGNORE_RULES:
        raise PilotError("tracked Docker build-context policy changed")
    return f"sha256:{sha256(canonical_policy).hexdigest()}"


@contextmanager
def _tracked_build_context(
    revision: str,
    *,
    repository: Path = ROOT,
) -> Iterator[tuple[Path, str]]:
    """Materialize a bounded, regular-file-only archive of one Git commit."""

    inventory = _tracked_build_inventory(revision, repository=repository)
    with tempfile.TemporaryDirectory(prefix="django-ray-cgraph-build-") as temporary:
        temporary_path = Path(temporary)
        archive_path = temporary_path / "context.tar"
        context_path = temporary_path / "context"
        context_path.mkdir()
        _run_command(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.eol=lf",
                "archive",
                "--format=tar",
                f"--output={archive_path}",
                revision,
                "--",
                *BUILD_CONTEXT_PATHS,
            ],
            cwd=repository,
        )
        try:
            archive_size = archive_path.stat().st_size
        except OSError as error:
            raise PilotError("tracked build context archive was not created") from error
        if not 0 < archive_size <= MAX_BUILD_CONTEXT_ARCHIVE_BYTES:
            raise PilotError("tracked build context archive exceeded its byte bound")

        extracted: set[str] = set()
        try:
            with tarfile.open(archive_path, mode="r:") as archive:
                members = archive.getmembers()
                if len(members) > MAX_BUILD_CONTEXT_FILES * 2:
                    raise PilotError("tracked build context archive has too many members")
                for member in members:
                    member_path = _validated_archive_path(member.name)
                    relative = member_path.as_posix()
                    destination = context_path.joinpath(*member_path.parts)
                    if member.isdir():
                        if not any(path.startswith(f"{relative}/") for path in inventory):
                            raise PilotError(
                                "tracked build context contains an unexpected directory"
                            )
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    if (
                        not member.isfile()
                        or member.linkname
                        or not set(member.pax_headers).issubset({"comment"})
                        or member.pax_headers.get("comment", revision) != revision
                    ):
                        raise PilotError(
                            "tracked build context contains a non-regular archive member"
                        )
                    expected = inventory.get(relative)
                    if expected is None or relative in extracted:
                        raise PilotError(
                            "tracked build context archive does not match its inventory"
                        )
                    expected_mode, expected_size = expected
                    archive_mode = expected_mode | 0o020
                    if member.size != expected_size or member.mode & 0o777 != archive_mode:
                        raise PilotError(
                            "tracked build context archive metadata changed: "
                            f"size={member.size}/{expected_size}, "
                            f"mode={member.mode & 0o777:o}/{archive_mode:o}"
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise PilotError("tracked build context file could not be read")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    copied = 0
                    with destination.open("xb") as output:
                        while chunk := source.read(64 * 1024):
                            copied += len(chunk)
                            if copied > expected_size:
                                raise PilotError(
                                    "tracked build context file exceeded its inventory"
                                )
                            output.write(chunk)
                    if copied != expected_size:
                        raise PilotError("tracked build context file was truncated")
                    os.chmod(destination, expected_mode)
                    extracted.add(relative)
        except (OSError, tarfile.TarError) as error:
            raise PilotError(
                "tracked build context archive could not be safely extracted"
            ) from error
        if extracted != set(inventory):
            raise PilotError("tracked build context archive omitted an inventoried file")
        policy_id = _validate_extracted_build_policy(context_path)
        yield context_path, policy_id


def _operator_list_field(value: dict[str, Any], name: str, *, owner: str) -> list[Any]:
    observed = value.get(name, [])
    if not isinstance(observed, list):
        raise PilotError(f"KubeRay {owner} returned malformed {name}")
    return observed


def _exact_controller_reference(
    value: Any,
    *,
    api_version: str = "apps/v1",
    kind: str,
    name: str,
    uid: str,
) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value)
        == {
            "apiVersion",
            "kind",
            "name",
            "uid",
            "controller",
            "blockOwnerDeletion",
        }
        and value.get("apiVersion") == api_version
        and value.get("kind") == kind
        and value.get("name") == name
        and value.get("uid") == uid
        and value.get("controller") is True
        and value.get("blockOwnerDeletion") is True
    )


def _validate_kuberay_operator(
    context: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    expected_namespace = profile.get("operator_namespace")
    expected_deployment = profile.get("operator_deployment_name")
    expected_container = profile.get("operator_container_name")
    expected_strategy = profile.get("operator_strategy")
    expected_deployment_labels = profile.get("operator_deployment_labels")
    expected_selector = profile.get("operator_selector")
    expected_pod_labels = profile.get("operator_pod_labels")
    expected_service_account = profile.get("operator_service_account")
    operator_version = profile.get("operator_version")
    expected_image = f"quay.io/kuberay/operator:v{operator_version}"
    expected_image_id = str(profile.get("operator_image", "")).split("@", 1)[-1]
    if (
        expected_namespace != "kuberay-system"
        or expected_deployment != "kuberay-operator"
        or expected_container != "kuberay-operator"
        or expected_strategy != "Recreate"
        or not isinstance(expected_deployment_labels, dict)
        or not isinstance(expected_selector, dict)
        or not isinstance(expected_pod_labels, dict)
        or expected_service_account != "kuberay-operator"
        or not SHA256_PATTERN.fullmatch(expected_image_id)
    ):
        raise PilotError("Compiled Graph pilot KubeRay operator profile is incomplete")

    deployment = _run_json_command(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            expected_namespace,
            "get",
            "deployment",
            expected_deployment,
            "-o",
            "json",
        ]
    )
    metadata_value = deployment.get("metadata")
    spec_value = deployment.get("spec")
    status_value = deployment.get("status")
    if not all(isinstance(value, dict) for value in (metadata_value, spec_value, status_value)):
        raise PilotError("KubeRay operator Deployment is malformed")
    assert isinstance(metadata_value, dict)
    assert isinstance(spec_value, dict)
    assert isinstance(status_value, dict)
    deployment_uid = metadata_value.get("uid")
    generation = metadata_value.get("generation")
    template = spec_value.get("template")
    selector = spec_value.get("selector")
    if not isinstance(template, dict) or not isinstance(selector, dict):
        raise PilotError("KubeRay operator Deployment template is malformed")
    template_metadata = template.get("metadata")
    template_spec = template.get("spec")
    if not isinstance(template_metadata, dict) or not isinstance(template_spec, dict):
        raise PilotError("KubeRay operator Deployment template is malformed")
    deployment_containers = _operator_list_field(
        template_spec,
        "containers",
        owner="Deployment",
    )
    deployment_init = _operator_list_field(
        template_spec,
        "initContainers",
        owner="Deployment",
    )
    deployment_ephemeral = _operator_list_field(
        template_spec,
        "ephemeralContainers",
        owner="Deployment",
    )
    if len(deployment_containers) != 1 or not isinstance(deployment_containers[0], dict):
        raise PilotError("KubeRay operator Deployment container inventory changed")
    deployment_container = deployment_containers[0]
    if (
        metadata_value.get("name") != expected_deployment
        or metadata_value.get("namespace") != expected_namespace
        or metadata_value.get("deletionTimestamp") is not None
        or KUBERNETES_UID_PATTERN.fullmatch(str(deployment_uid)) is None
        or not _is_exact_json_integer(generation)
        or generation < 1
        or not _json_exact_equal(metadata_value.get("labels"), expected_deployment_labels)
        or not _is_exact_json_integer(spec_value.get("replicas"), expected=1)
        or selector.get("matchLabels") != expected_selector
        or spec_value.get("strategy") != {"type": expected_strategy}
        or template_metadata.get("labels") != expected_pod_labels
        or template_spec.get("serviceAccountName") != expected_service_account
        or deployment_init
        or deployment_ephemeral
        or deployment_container.get("name") != expected_container
        or deployment_container.get("image") != expected_image
        or deployment_container.get("imagePullPolicy") != "IfNotPresent"
        or deployment_container.get("command") != ["/manager"]
        or not _is_exact_json_integer(status_value.get("observedGeneration"), expected=generation)
        or not _is_exact_json_integer(status_value.get("replicas"), expected=1)
        or not _is_exact_json_integer(status_value.get("updatedReplicas"), expected=1)
        or not _is_exact_json_integer(status_value.get("readyReplicas"), expected=1)
        or not _is_exact_json_integer(status_value.get("availableReplicas"), expected=1)
    ):
        raise PilotError("KubeRay operator Deployment identity or readiness changed")

    selector_text = ",".join(f"{name}={value}" for name, value in sorted(expected_selector.items()))
    pods = _run_json_command(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            expected_namespace,
            "get",
            "pods",
            "-l",
            selector_text,
            "-o",
            "json",
        ]
    )
    pod_items = pods.get("items")
    if not isinstance(pod_items, list) or len(pod_items) != 1 or not isinstance(pod_items[0], dict):
        raise PilotError("expected exactly one KubeRay operator pod")
    pod = pod_items[0]
    pod_metadata = pod.get("metadata")
    pod_spec = pod.get("spec")
    pod_status = pod.get("status")
    if not all(isinstance(value, dict) for value in (pod_metadata, pod_spec, pod_status)):
        raise PilotError("KubeRay operator pod is malformed")
    assert isinstance(pod_metadata, dict)
    assert isinstance(pod_spec, dict)
    assert isinstance(pod_status, dict)
    pod_name = pod_metadata.get("name")
    pod_uid = pod_metadata.get("uid")
    pod_generation = pod_metadata.get("generation")
    pod_labels = pod_metadata.get("labels")
    if not isinstance(pod_labels, dict):
        raise PilotError("KubeRay operator pod labels are malformed")
    template_hash = pod_labels.get("pod-template-hash")
    expected_live_labels = {**expected_pod_labels, "pod-template-hash": template_hash}
    expected_replica_set_selector = {**expected_selector, "pod-template-hash": template_hash}
    replica_set_name = f"{expected_deployment}-{template_hash}"
    pod_owners = _operator_list_field(pod_metadata, "ownerReferences", owner="pod")
    pod_containers = _operator_list_field(pod_spec, "containers", owner="pod")
    pod_statuses = _operator_list_field(pod_status, "containerStatuses", owner="pod")
    pod_init = _operator_list_field(pod_spec, "initContainers", owner="pod")
    pod_init_statuses = _operator_list_field(
        pod_status,
        "initContainerStatuses",
        owner="pod",
    )
    pod_ephemeral = _operator_list_field(pod_spec, "ephemeralContainers", owner="pod")
    pod_ephemeral_statuses = _operator_list_field(
        pod_status,
        "ephemeralContainerStatuses",
        owner="pod",
    )
    pod_conditions = _operator_list_field(pod_status, "conditions", owner="pod")
    ready_conditions = [
        condition
        for condition in pod_conditions
        if isinstance(condition, dict) and condition.get("type") == "Ready"
    ]
    if len(pod_containers) != 1 or len(pod_statuses) != 1:
        raise PilotError("KubeRay operator pod container inventory changed")
    if not isinstance(pod_containers[0], dict) or not isinstance(pod_statuses[0], dict):
        raise PilotError("KubeRay operator pod container inventory is malformed")
    pod_container = pod_containers[0]
    container_status = pod_statuses[0]
    replica_set_uid = pod_owners[0].get("uid") if len(pod_owners) == 1 else None
    running_image_id = _parse_exact_cri_image_id(container_status.get("imageID"))
    container_id = container_status.get("containerID")
    restart_count = container_status.get("restartCount")
    container_state = container_status.get("state")
    if (
        not isinstance(template_hash, str)
        or re.fullmatch(r"[a-z0-9]{1,63}", template_hash) is None
        or not isinstance(pod_name, str)
        or re.fullmatch(rf"{re.escape(replica_set_name)}-[a-z0-9]{{5}}", pod_name) is None
        or pod_metadata.get("namespace") != expected_namespace
        or KUBERNETES_UID_PATTERN.fullmatch(str(pod_uid)) is None
        or not _is_exact_json_integer(pod_generation)
        or pod_generation < 1
        or pod_metadata.get("deletionTimestamp") is not None
        or not _json_exact_equal(pod_labels, expected_live_labels)
        or len(pod_owners) != 1
        or KUBERNETES_UID_PATTERN.fullmatch(str(replica_set_uid)) is None
        or not _exact_controller_reference(
            pod_owners[0],
            kind="ReplicaSet",
            name=replica_set_name,
            uid=str(replica_set_uid),
        )
        or pod_spec.get("serviceAccountName") != expected_service_account
        or pod_init
        or pod_init_statuses
        or pod_ephemeral
        or pod_ephemeral_statuses
        or pod_status.get("phase") != "Running"
        or len(ready_conditions) != 1
        or ready_conditions[0].get("status") != "True"
        or not _is_exact_json_integer(
            ready_conditions[0].get("observedGeneration"),
            expected=pod_generation,
        )
        or pod_container.get("name") != expected_container
        or container_status.get("name") != expected_container
        or pod_container.get("image") != expected_image
        or container_status.get("image") != expected_image
        or running_image_id != expected_image_id
        or not isinstance(container_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
        or container_status.get("ready") is not True
        or not _is_exact_json_integer(restart_count)
        or restart_count < 0
        or not isinstance(container_state, dict)
        or set(container_state) != {"running"}
        or not isinstance(container_state.get("running"), dict)
    ):
        raise PilotError("KubeRay operator pod execution identity or readiness changed")

    replica_set = _run_json_command(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            expected_namespace,
            "get",
            "replicaset",
            replica_set_name,
            "-o",
            "json",
        ]
    )
    rs_metadata = replica_set.get("metadata")
    rs_spec = replica_set.get("spec")
    rs_status = replica_set.get("status")
    if not all(isinstance(value, dict) for value in (rs_metadata, rs_spec, rs_status)):
        raise PilotError("KubeRay operator ReplicaSet is malformed")
    assert isinstance(rs_metadata, dict)
    assert isinstance(rs_spec, dict)
    assert isinstance(rs_status, dict)
    rs_owners = _operator_list_field(rs_metadata, "ownerReferences", owner="ReplicaSet")
    rs_generation = rs_metadata.get("generation")
    rs_template = rs_spec.get("template")
    rs_selector = rs_spec.get("selector")
    if not isinstance(rs_template, dict) or not isinstance(rs_selector, dict):
        raise PilotError("KubeRay operator ReplicaSet template is malformed")
    rs_template_metadata = rs_template.get("metadata")
    rs_template_spec = rs_template.get("spec")
    if not isinstance(rs_template_metadata, dict) or not isinstance(rs_template_spec, dict):
        raise PilotError("KubeRay operator ReplicaSet template is malformed")
    rs_containers = _operator_list_field(rs_template_spec, "containers", owner="ReplicaSet")
    rs_init = _operator_list_field(rs_template_spec, "initContainers", owner="ReplicaSet")
    rs_ephemeral = _operator_list_field(
        rs_template_spec,
        "ephemeralContainers",
        owner="ReplicaSet",
    )
    if len(rs_containers) != 1 or not isinstance(rs_containers[0], dict):
        raise PilotError("KubeRay operator ReplicaSet container inventory changed")
    if (
        rs_metadata.get("name") != replica_set_name
        or rs_metadata.get("namespace") != expected_namespace
        or rs_metadata.get("uid") != replica_set_uid
        or rs_metadata.get("deletionTimestamp") is not None
        or not _is_exact_json_integer(rs_generation)
        or rs_generation < 1
        or not _json_exact_equal(rs_metadata.get("labels"), expected_live_labels)
        or len(rs_owners) != 1
        or not _exact_controller_reference(
            rs_owners[0],
            kind="Deployment",
            name=expected_deployment,
            uid=str(deployment_uid),
        )
        or not _is_exact_json_integer(rs_spec.get("replicas"), expected=1)
        or rs_selector.get("matchLabels") != expected_replica_set_selector
        or rs_template_metadata.get("labels") != expected_live_labels
        or rs_template_spec.get("serviceAccountName") != expected_service_account
        or rs_init
        or rs_ephemeral
        or rs_containers[0].get("name") != expected_container
        or rs_containers[0].get("image") != expected_image
        or not _is_exact_json_integer(
            rs_status.get("observedGeneration"),
            expected=rs_generation,
        )
        or not _is_exact_json_integer(rs_status.get("replicas"), expected=1)
        or not _is_exact_json_integer(rs_status.get("readyReplicas"), expected=1)
        or not _is_exact_json_integer(rs_status.get("availableReplicas"), expected=1)
    ):
        raise PilotError("KubeRay operator controller ownership or ReplicaSet identity changed")

    return {
        "version": operator_version,
        "image": expected_image,
        "image_id": running_image_id,
        "deployment_name": expected_deployment,
        "deployment_uid": deployment_uid,
        "replica_set_name": replica_set_name,
        "replica_set_uid": replica_set_uid,
        "pod_name": pod_name,
        "pod_uid": pod_uid,
        "container_name": expected_container,
        "container_id": container_id,
        "restart_count": restart_count,
        "ready": True,
        "pod_phase": "Running",
        "controller_chain_verified": True,
        "container_inventory_verified": True,
    }


def _assert_kuberay_operator_identity_unchanged(
    context: str,
    profile: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    observed = _validate_kuberay_operator(context, profile)
    if not _json_exact_equal(observed, expected):
        raise PilotError("KubeRay operator identity changed during the pilot")


def _validate_host_target(context: str, namespace: str) -> dict[str, Any]:
    if not EXPECTED_CONTEXT_PATTERN.fullmatch(context):
        raise PilotError("Kubernetes context contains unsupported characters")
    if namespace != PILOT_NAMESPACE:
        raise PilotError(f"pilot namespace must be exactly {PILOT_NAMESPACE!r}")
    active = _run_command(["kubectl", "config", "current-context"]).stdout.strip()
    if active != context:
        raise PilotError(f"active Kubernetes context is {active!r}, expected {context!r}")

    profile = _load_profile()
    kubernetes_profile = profile["kubernetes"]
    if context != kubernetes_profile.get("context"):
        raise PilotError(
            f"Kubernetes context changed: {context!r}, "
            f"expected {kubernetes_profile.get('context')!r}"
        )
    version = _run_json_command(["kubectl", "--context", context, "version", "-o", "json"])
    server_version = version.get("serverVersion", {}).get("gitVersion")
    if server_version != kubernetes_profile["server_version"]:
        raise PilotError(
            f"Kubernetes server changed: {server_version!r}, "
            f"expected {kubernetes_profile['server_version']!r}"
        )

    expected_node = kubernetes_profile["node"]
    node = _run_json_command(
        [
            "kubectl",
            "--context",
            context,
            "get",
            "node",
            expected_node["name"],
            "-o",
            "json",
        ]
    )
    node_info = node.get("status", {}).get("nodeInfo", {})
    observed_node = {
        "name": node.get("metadata", {}).get("name"),
        "operating_system": node_info.get("operatingSystem"),
        "architecture": node_info.get("architecture"),
        "kernel_version": node_info.get("kernelVersion"),
        "container_runtime_version": node_info.get("containerRuntimeVersion"),
    }
    if observed_node != expected_node:
        raise PilotError(f"Kubernetes node/runtime profile changed: {observed_node!r}")

    operator = _validate_kuberay_operator(context, profile["kuberay"])
    return {
        "kuberay_operator": operator,
        "kubernetes": {
            "server_version": server_version,
            "node": {
                **observed_node,
                "capacity": node.get("status", {}).get("capacity", {}),
                "allocatable": node.get("status", {}).get("allocatable", {}),
            },
            "node_selector": kubernetes_profile["node_selector"],
        },
    }


def _validate_docker_context() -> dict[str, Any]:
    profile = _load_profile().get("docker")
    if not isinstance(profile, dict):
        raise PilotError("Compiled Graph pilot profile omitted its Docker context")
    expected_context = profile.get("context")
    expected_endpoint = profile.get("endpoint")
    expected_engine = profile.get("engine")
    if (
        not isinstance(expected_context, str)
        or not expected_context
        or not isinstance(expected_endpoint, str)
        or not expected_endpoint
        or not isinstance(expected_engine, dict)
    ):
        raise PilotError("Compiled Graph pilot Docker context profile is incomplete")

    active_context = _run_command(["docker", "context", "show"]).stdout.strip()
    if active_context != expected_context:
        raise PilotError(
            f"active Docker context is {active_context!r}, expected {expected_context!r}"
        )
    contexts = _run_json_command(
        ["docker", "context", "inspect", expected_context],
        expected_type=list,
    )
    if not isinstance(contexts, list) or len(contexts) != 1:
        raise PilotError("Docker returned an unexpected context inspection result")
    endpoint = contexts[0].get("Endpoints", {}).get("docker", {}).get("Host")
    if endpoint != expected_endpoint:
        raise PilotError(f"Docker endpoint changed: {endpoint!r}, expected {expected_endpoint!r}")

    server = _run_json_command(
        [
            "docker",
            "--context",
            expected_context,
            "version",
            "--format",
            "{{json .Server}}",
        ]
    )
    observed_engine = {
        "version": server.get("Version"),
        "operating_system": server.get("Os"),
        "architecture": server.get("Arch"),
        "kernel_version": server.get("KernelVersion"),
    }
    if observed_engine != expected_engine:
        raise PilotError(
            f"Docker engine profile changed: {observed_engine!r}, expected {expected_engine!r}"
        )
    return {
        "context": expected_context,
        "endpoint": expected_endpoint,
        "engine": observed_engine,
        "build_context_policy": "dockerfile-specific-deny-by-default",
        "build_context_policy_id": _build_context_policy_identity(),
    }


def _build_image(revision: str) -> tuple[str, str, dict[str, Any]]:
    docker_context = _validate_docker_context()
    context_name = docker_context["context"]
    image = f"{PILOT_IMAGE_REPOSITORY}:{revision[:12]}"
    with _tracked_build_context(revision) as (build_context, build_context_policy_id):
        docker_context["build_context_policy_id"] = build_context_policy_id
        _run_command(
            [
                "docker",
                "--context",
                context_name,
                "build",
                "--file",
                "k8s/pilots/compiled-graph/Dockerfile",
                "--build-arg",
                f"DJANGO_RAY_REVISION={revision}",
                "--tag",
                image,
                ".",
            ],
            timeout_seconds=1200,
            cwd=build_context,
        )
    inspected = _run_json_command(
        ["docker", "--context", context_name, "image", "inspect", image],
        expected_type=list,
    )
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise PilotError("Docker returned an unexpected image inspection result")
    image_id = _parse_exact_docker_image_id(inspected[0].get("Id"))
    labels = inspected[0].get("Config", {}).get("Labels", {}) or {}
    if labels.get("org.opencontainers.image.revision") != revision:
        raise PilotError("pilot image revision label does not match the tested Git commit")
    if inspected[0].get("Os") != "linux" or inspected[0].get("Architecture") != "amd64":
        raise PilotError("pilot image is not the pinned linux/amd64 platform")
    return image, image_id, docker_context


def _render_manifest(
    image: str,
    image_id: str,
    namespace_lease: NamespaceLease,
) -> tuple[str, str, str]:
    _validate_namespace_lease(namespace_lease)
    deployment_profile = _configuration_identity()
    container_profile = f"{image}@{image_id}"
    values = {
        "__PILOT_NAMESPACE__": namespace_lease.name,
        "__PILOT_NAMESPACE_UID__": namespace_lease.uid,
        "__PILOT_RUN_TOKEN__": namespace_lease.run_token,
        "__PILOT_IMAGE__": image,
        "__CONTAINER_PROFILE__": container_profile,
        "__DEPLOYMENT_PROFILE__": deployment_profile,
        "__PILOT_IMAGE_ID__": image_id,
    }
    rendered = MANIFEST_PATH.read_text(encoding="utf-8")
    for marker, value in values.items():
        rendered = rendered.replace(marker, value)
    if re.search(r"__[A-Z0-9_]+__", rendered):
        raise PilotError("rendered pilot manifest retains an unresolved marker")
    rendered_digest = f"sha256:{sha256(rendered.encode('utf-8')).hexdigest()}"
    return rendered, deployment_profile, rendered_digest


def _run_near_neighbor_container(
    image: str,
    image_id: str,
    configuration_id: str,
    docker_context: dict[str, Any],
) -> dict[str, Any]:
    profile = _load_profile()
    changed_shared_memory_bytes = profile["cluster"]["shared_memory_bytes_per_pod"] // 2
    changed_shared_memory_profile = f"tmpfs:/dev/shm:size={changed_shared_memory_bytes}"
    environment = {
        "DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE": f"{image}@{image_id}",
        "DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE": configuration_id,
        "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE": (changed_shared_memory_profile),
        "DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE": (
            f"plasma:{profile['cluster']['object_store_bytes_per_pod']}"
        ),
        "DJANGO_RAY_PILOT_IMAGE_ID": image_id,
        "DJANGO_RAY_PILOT_CONFIG_ID": configuration_id,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        "docker",
        "--context",
        docker_context["context"],
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--shm-size",
        str(changed_shared_memory_bytes),
    ]
    for name, value in sorted(environment.items()):
        command.extend(["--env", f"{name}={value}"])
    command.extend(
        [
            "--entrypoint",
            "python",
            image_id,
            "/opt/django-ray/scripts/kuberay_compiled_graph_pilot.py",
            "near-neighbor",
        ]
    )
    result = _run_command(command, timeout_seconds=60, preserve_stdout=True)
    observation = _parse_json_command_output(result, command, expected_type=dict)
    if observation.get("status") != "success":
        raise PilotError(f"physical near-neighbor profile did not fail closed: {observation!r}")
    return observation


def _validate_namespace_lease(lease: NamespaceLease) -> None:
    if (
        lease.name != PILOT_NAMESPACE
        or KUBERNETES_UID_PATTERN.fullmatch(lease.uid) is None
        or PILOT_RUN_TOKEN_PATTERN.fullmatch(lease.run_token) is None
    ):
        raise PilotError("pilot namespace lease identity is invalid")


def _namespace_metadata_matches_lease(
    value: Any,
    lease: NamespaceLease,
    *,
    require_live: bool,
) -> bool:
    if not isinstance(value, dict):
        return False
    labels = value.get("labels")
    return bool(
        isinstance(labels, dict)
        and value.get("name") == lease.name
        and value.get("uid") == lease.uid
        and labels.get(PILOT_PROFILE_LABEL_KEY) == PROFILE_NAME
        and labels.get(PILOT_RUN_LABEL_KEY) == lease.run_token
        and (not require_live or value.get("deletionTimestamp") is None)
    )


def _ensure_namespace(context: str, namespace: str) -> NamespaceLease:
    """Create a fresh namespace and return only its atomic create-response lease."""

    result = _run_command(
        [
            "kubectl",
            "--context",
            context,
            "get",
            "namespace",
            namespace,
            "--ignore-not-found=true",
            "-o",
            "json",
        ],
        check=False,
        preserve_stdout=True,
    )
    if result.returncode != 0:
        raise PilotError("could not determine whether the pilot namespace already exists")
    if result.stdout.strip():
        _parse_json_command_output(
            result,
            ["kubectl", "get", "namespace"],
            expected_type=dict,
        )
        raise PilotError(
            "pilot namespace already exists; refusing to claim a pre-existing namespace"
        )
    run_token = secrets.token_hex(16)
    if PILOT_RUN_TOKEN_PATTERN.fullmatch(run_token) is None:  # pragma: no cover - secrets invariant
        raise PilotError("could not create a valid pilot namespace run token")
    namespace_manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {
                PILOT_PROFILE_LABEL_KEY: PROFILE_NAME,
                PILOT_RUN_LABEL_KEY: run_token,
            },
        },
    }
    create_command = [
        "kubectl",
        "--context",
        context,
        "create",
        "-f",
        "-",
        "-o",
        "json",
    ]
    created = _run_command(
        create_command,
        input_text=json.dumps(namespace_manifest),
        preserve_stdout=True,
    )
    namespace_record = _parse_json_command_output(
        created,
        create_command,
        expected_type=dict,
    )
    metadata = namespace_record.get("metadata")
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    if not isinstance(uid, str):
        raise PilotError("namespace create response omitted its immutable UID")
    lease = NamespaceLease(name=namespace, uid=uid, run_token=run_token)
    _validate_namespace_lease(lease)
    if not _namespace_metadata_matches_lease(metadata, lease, require_live=True):
        raise PilotError(
            "namespace create response did not preserve the requested ownership identity"
        )
    return lease


def _assert_current_namespace_lease(
    context: str,
    namespace_lease: NamespaceLease,
) -> dict[str, Any]:
    """Prove that the current live namespace is still the create-response lease."""

    _validate_namespace_lease(namespace_lease)
    command = [
        "kubectl",
        "--context",
        context,
        "get",
        "namespace",
        namespace_lease.name,
        "--ignore-not-found=true",
        "-o",
        "json",
    ]
    result = _run_command(
        command,
        check=False,
        preserve_stdout=True,
    )
    if result.returncode != 0:
        raise PilotError("could not verify the current pilot namespace lease")
    if not result.stdout.strip():
        raise PilotError("current pilot namespace lease no longer exists")
    namespace_record = _parse_json_command_output(
        result,
        command,
        expected_type=dict,
    )
    metadata = namespace_record.get("metadata")
    if not _namespace_metadata_matches_lease(
        metadata,
        namespace_lease,
        require_live=True,
    ):
        raise PilotError("current pilot namespace no longer matches the exact live lease")
    assert isinstance(metadata, dict)
    return metadata


def _validate_raycluster_lease(
    raycluster_lease: RayClusterLease,
    namespace_lease: NamespaceLease,
) -> None:
    _validate_namespace_lease(namespace_lease)
    if (
        raycluster_lease.name != RAYCLUSTER_NAME
        or KUBERNETES_UID_PATTERN.fullmatch(raycluster_lease.uid) is None
        or raycluster_lease.namespace_uid != namespace_lease.uid
        or raycluster_lease.run_token != namespace_lease.run_token
    ):
        raise PilotError("pilot RayCluster lease identity is invalid")


def _raycluster_metadata_matches_lease(
    value: Any,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
    *,
    require_live: bool,
) -> bool:
    if not isinstance(value, dict):
        return False
    labels = value.get("labels")
    annotations = value.get("annotations")
    return bool(
        isinstance(labels, dict)
        and isinstance(annotations, dict)
        and value.get("name") == raycluster_lease.name
        and value.get("namespace") == namespace_lease.name
        and value.get("uid") == raycluster_lease.uid
        and labels.get(PILOT_PROFILE_LABEL_KEY) == PROFILE_NAME
        and labels.get(PILOT_RUN_LABEL_KEY) == raycluster_lease.run_token
        and annotations.get(PILOT_NAMESPACE_UID_ANNOTATION_KEY) == raycluster_lease.namespace_uid
        and (not require_live or value.get("deletionTimestamp") is None)
    )


def _assert_current_raycluster_lease(
    context: str,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
) -> dict[str, Any]:
    """Prove that the current live RayCluster is still the create response."""

    _validate_raycluster_lease(raycluster_lease, namespace_lease)
    command = [
        "kubectl",
        "--context",
        context,
        "-n",
        namespace_lease.name,
        "get",
        "raycluster",
        raycluster_lease.name,
        "--ignore-not-found=true",
        "-o",
        "json",
    ]
    result = _run_command(
        command,
        check=False,
        preserve_stdout=True,
    )
    if result.returncode != 0:
        raise PilotError("could not verify the current pilot RayCluster lease")
    if not result.stdout.strip():
        raise PilotError("current pilot RayCluster lease no longer exists")
    raycluster = _parse_json_command_output(result, command, expected_type=dict)
    metadata = raycluster.get("metadata")
    if not _raycluster_metadata_matches_lease(
        metadata,
        namespace_lease,
        raycluster_lease,
        require_live=True,
    ):
        raise PilotError("current pilot RayCluster no longer matches the exact live lease")
    assert isinstance(metadata, dict)
    return metadata


@contextmanager
def _current_pilot_lease_boundary(
    context: str,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease | None = None,
) -> Iterator[None]:
    """Bracket one cluster API boundary with current immutable lease checks."""

    _assert_current_namespace_lease(context, namespace_lease)
    if raycluster_lease is not None:
        _assert_current_raycluster_lease(context, namespace_lease, raycluster_lease)
    try:
        yield
    finally:
        try:
            if raycluster_lease is not None:
                _assert_current_raycluster_lease(context, namespace_lease, raycluster_lease)
        finally:
            _assert_current_namespace_lease(context, namespace_lease)


def _create_and_wait(
    context: str,
    namespace_lease: NamespaceLease,
    manifest: str,
    *,
    operator_profile: dict[str, Any],
    expected_operator: dict[str, Any],
) -> tuple[list[dict[str, Any]], RayClusterLease]:
    _validate_namespace_lease(namespace_lease)
    _assert_kuberay_operator_identity_unchanged(
        context,
        operator_profile,
        expected_operator,
    )
    namespace = namespace_lease.name
    create_command = [
        "kubectl",
        "--context",
        context,
        "create",
        "-f",
        "-",
        "-o",
        "json",
    ]
    with _current_pilot_lease_boundary(context, namespace_lease):
        created = _run_command(
            create_command,
            input_text=manifest,
            preserve_stdout=True,
        )
    raycluster = _parse_json_command_output(
        created,
        create_command,
        expected_type=dict,
    )
    metadata = raycluster.get("metadata")
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    if (
        raycluster.get("apiVersion") != "ray.io/v1"
        or raycluster.get("kind") != "RayCluster"
        or not isinstance(uid, str)
    ):
        raise PilotError("RayCluster create response omitted its immutable identity")
    raycluster_lease = RayClusterLease(
        name=RAYCLUSTER_NAME,
        uid=uid,
        namespace_uid=namespace_lease.uid,
        run_token=namespace_lease.run_token,
    )
    _validate_raycluster_lease(raycluster_lease, namespace_lease)
    if not _raycluster_metadata_matches_lease(
        metadata,
        namespace_lease,
        raycluster_lease,
        require_live=True,
    ):
        raise PilotError("RayCluster create response did not preserve the pilot lease identity")
    _assert_current_namespace_lease(context, namespace_lease)
    _assert_current_raycluster_lease(context, namespace_lease, raycluster_lease)
    deadline = time.monotonic() + 360
    last_summary = "no pods observed"
    while time.monotonic() < deadline:
        with _current_pilot_lease_boundary(
            context,
            namespace_lease,
            raycluster_lease,
        ):
            result = _run_command(
                [
                    "kubectl",
                    "--context",
                    context,
                    "-n",
                    namespace,
                    "get",
                    "pods",
                    "-l",
                    f"ray.io/cluster={RAYCLUSTER_NAME},{PILOT_RUN_LABEL_KEY}={namespace_lease.run_token}",
                    "--show-managed-fields=false",
                    "-o",
                    "json",
                ],
                check=False,
                preserve_stdout=True,
            )
        if result.returncode == 0:
            items = _parse_json_command_output(
                result,
                ["kubectl", "get", "pods"],
                expected_type=dict,
            ).get("items", [])
            roles = [
                item.get("metadata", {}).get("labels", {}).get("ray.io/node-type") for item in items
            ]
            ready = [
                bool(item.get("status", {}).get("containerStatuses", [{}])[0].get("ready"))
                for item in items
            ]
            last_summary = f"roles={roles!r}, ready={ready!r}"
            if (
                len(items) == 3
                and roles.count("head") == 1
                and roles.count("worker") == 2
                and all(ready)
            ):
                _assert_kuberay_operator_identity_unchanged(
                    context,
                    operator_profile,
                    expected_operator,
                )
                return items, raycluster_lease
        time.sleep(3)
    raise PilotError(f"pilot RayCluster did not become Ready: {last_summary}")


def _fetch_pilot_pods(
    context: str,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
) -> list[dict[str, Any]]:
    _validate_raycluster_lease(raycluster_lease, namespace_lease)
    namespace = namespace_lease.name
    with _current_pilot_lease_boundary(
        context,
        namespace_lease,
        raycluster_lease,
    ):
        value = _run_json_command(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                namespace,
                "get",
                "pods",
                "-l",
                f"ray.io/cluster={RAYCLUSTER_NAME},{PILOT_RUN_LABEL_KEY}={namespace_lease.run_token}",
                "--show-managed-fields=false",
                "-o",
                "json",
            ]
        )
    items = value.get("items")
    if not isinstance(items, list):
        raise PilotError("pilot pod query returned an invalid item list")
    return items


def _ray_start_command_tokens(arguments: Any) -> list[str]:
    """Return the sole structural ``ray start`` command's argument tokens."""

    if not isinstance(arguments, list) or any(not isinstance(value, str) for value in arguments):
        raise PilotError("pod returned malformed Ray start arguments")
    try:
        lexer = shlex.shlex(
            " ".join(arguments),
            posix=True,
            punctuation_chars=";&|",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as error:
        raise PilotError("pod returned malformed Ray start arguments") from error
    command_starts = [
        index
        for index in range(len(tokens) - 1)
        if PurePosixPath(tokens[index]).name == "ray" and tokens[index + 1] == "start"
    ]
    if len(command_starts) != 1:
        raise PilotError("pod does not contain exactly one structural Ray start command")
    command_start = command_starts[0] + 2
    command_end = next(
        (
            index
            for index in range(command_start, len(tokens))
            if tokens[index] in {";", "&", "&&", "|", "||"}
        ),
        len(tokens),
    )
    return tokens[command_start:command_end]


def _validate_ray_start_parameter_spec(option: Any, value: Any) -> dict[str, str]:
    """Validate one profile declaration for an effective Ray start parameter."""

    if (
        type(option) is not str
        or not option
        or option.startswith("-")
        or not isinstance(value, dict)
        or set(value) != {"kind", "value"}
        or type(value.get("kind")) is not str
        or value["kind"] not in RAY_START_PARAMETER_KINDS
        or type(value.get("value")) is not str
    ):
        raise PilotError("Compiled Graph pilot profile has invalid Ray start parameters")
    if value["kind"] == "valueless-true-switch" and value["value"] != "true":
        raise PilotError("Compiled Graph pilot profile has invalid Ray start parameters")
    return value


def _ray_start_option_observations(
    command_tokens: list[str],
    option: str,
    parameter_spec: dict[str, str],
) -> list[dict[str, Any]]:
    """Capture one option's sanitized lexical form and effective semantics."""

    flag = f"--{option}"
    observations: list[dict[str, Any]] = []
    for index, token in enumerate(command_tokens):
        if token == flag:
            next_token = command_tokens[index + 1] if index + 1 < len(command_tokens) else None
            if parameter_spec["kind"] == "valueless-true-switch":
                if next_token is not None and not (
                    next_token.startswith("--") and len(next_token) > 2
                ):
                    raise PilotError(f"pod Ray --{option} has an unexpected separate value")
                observations.append(
                    {
                        "lexical_form": "valueless-switch",
                        "lexical_value": None,
                        "semantic_value": True,
                    }
                )
            else:
                if next_token is None or next_token.startswith("-"):
                    raise PilotError(f"pod Ray --{option} has no value")
                observations.append(
                    {
                        "lexical_form": "separate-value",
                        "lexical_value": next_token,
                        "semantic_value": next_token,
                    }
                )
        elif token.startswith(f"{flag}="):
            lexical_value = token.removeprefix(f"{flag}=")
            semantic_value: Any = lexical_value
            if parameter_spec["kind"] == "valueless-true-switch":
                if lexical_value == "true":
                    semantic_value = True
                elif lexical_value == "false":
                    semantic_value = False
            observations.append(
                {
                    "lexical_form": "equals-value",
                    "lexical_value": lexical_value,
                    "semantic_value": semantic_value,
                }
            )
    return observations


def _ray_start_observation_matches_spec(
    observation: Any,
    parameter_spec: dict[str, str],
) -> bool:
    """Require an exact retained observation for one declared parameter."""

    if not isinstance(observation, dict) or set(observation) != {
        "lexical_form",
        "lexical_value",
        "semantic_value",
    }:
        return False
    if parameter_spec["kind"] == "valueless-true-switch":
        return (
            type(observation.get("lexical_form")) is str
            and observation["lexical_form"] == "valueless-switch"
            and observation.get("lexical_value") is None
            and observation.get("semantic_value") is True
        )
    return (
        type(observation.get("lexical_form")) is str
        and observation["lexical_form"] in RAY_START_VALUE_LEXICAL_FORMS
        and type(observation.get("lexical_value")) is str
        and observation.get("lexical_value") == parameter_spec["value"]
        and type(observation.get("semantic_value")) is str
        and observation.get("semantic_value") == parameter_spec["value"]
    )


def _pod_list_field(value: dict[str, Any], name: str, *, pod_name: str) -> list[Any]:
    observed = value.get(name, [])
    if not isinstance(observed, list):
        raise PilotError(f"pod {pod_name} returned malformed {name}")
    return observed


def _verify_init_container_inventory(
    pod_name: str,
    pod_spec: dict[str, Any],
    pod_status: dict[str, Any],
    *,
    expected_names: Any,
    image: str,
    image_id: str,
) -> list[dict[str, Any]]:
    if (
        not isinstance(expected_names, list)
        or any(not isinstance(name, str) or not name for name in expected_names)
        or len(set(expected_names)) != len(expected_names)
    ):
        raise PilotError("Compiled Graph pilot profile has an invalid init-container inventory")
    containers = _pod_list_field(pod_spec, "initContainers", pod_name=pod_name)
    statuses = _pod_list_field(pod_status, "initContainerStatuses", pod_name=pod_name)
    if len(containers) != len(expected_names) or len(statuses) != len(expected_names):
        raise PilotError(f"pod {pod_name} init-container inventory changed")
    if [item.get("name") for item in containers if isinstance(item, dict)] != expected_names:
        raise PilotError(f"pod {pod_name} init-container specification changed")
    if [item.get("name") for item in statuses if isinstance(item, dict)] != expected_names:
        raise PilotError(f"pod {pod_name} init-container status inventory changed")

    observed: list[dict[str, Any]] = []
    for expected_name, container, status in zip(
        expected_names,
        containers,
        statuses,
        strict=True,
    ):
        if not isinstance(container, dict) or not isinstance(status, dict):
            raise PilotError(f"pod {pod_name} returned malformed init-container evidence")
        container_id = status.get("containerID")
        restart_count = status.get("restartCount")
        state = status.get("state")
        terminated = state.get("terminated") if isinstance(state, dict) else None
        running_id = _parse_exact_cri_image_id(status.get("imageID"))
        if (
            container.get("name") != expected_name
            or status.get("name") != expected_name
            or container.get("image") != image
            or status.get("image") != image
            or running_id != image_id
            or not isinstance(container_id, str)
            or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
            or not _is_exact_json_integer(restart_count, expected=0)
            or status.get("ready") is not True
            or not isinstance(state, dict)
            or set(state) != {"terminated"}
            or not isinstance(terminated, dict)
            or not _is_exact_json_integer(terminated.get("exitCode"), expected=0)
            or terminated.get("reason") != "Completed"
        ):
            raise PilotError(f"pod {pod_name} init-container execution identity changed")
        observed.append(
            {
                "name": expected_name,
                "container_id": container_id,
                "image": container.get("image"),
                "image_id": running_id,
                "restart_count": restart_count,
                "ready": status.get("ready"),
                "state": "terminated",
                "exit_code": terminated.get("exitCode"),
                "reason": terminated.get("reason"),
            }
        )
    return observed


def _verify_pod_images(
    pods: list[dict[str, Any]],
    image: str,
    image_id: str,
    configuration_id: str,
    node_name: str,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
) -> list[dict[str, Any]]:
    _validate_raycluster_lease(raycluster_lease, namespace_lease)
    profile = _load_profile()
    roles = [pod.get("metadata", {}).get("labels", {}).get("ray.io/node-type") for pod in pods]
    if len(pods) != 3 or roles.count("head") != 1 or roles.count("worker") != 2:
        raise PilotError("pilot pod set no longer has one head and two workers")
    verified: list[dict[str, Any]] = []
    for pod in pods:
        pod_metadata = pod.get("metadata")
        if not isinstance(pod_metadata, dict):
            raise PilotError("pilot pod returned malformed metadata")
        name = pod_metadata.get("name")
        if not isinstance(name, str) or not name:
            raise PilotError("pilot pod returned an invalid name")
        labels = pod_metadata.get("labels")
        annotations = pod_metadata.get("annotations")
        owner_references = _pod_list_field(
            pod_metadata,
            "ownerReferences",
            pod_name=name,
        )
        if (
            pod_metadata.get("namespace") != namespace_lease.name
            or not isinstance(labels, dict)
            or labels.get("ray.io/cluster") != raycluster_lease.name
            or labels.get(PILOT_PROFILE_LABEL_KEY) != PROFILE_NAME
            or labels.get(PILOT_RUN_LABEL_KEY) != raycluster_lease.run_token
            or not isinstance(annotations, dict)
            or annotations.get(PILOT_NAMESPACE_UID_ANNOTATION_KEY) != raycluster_lease.namespace_uid
            or len(owner_references) != 1
            or not _exact_controller_reference(
                owner_references[0],
                api_version="ray.io/v1",
                kind="RayCluster",
                name=raycluster_lease.name,
                uid=raycluster_lease.uid,
            )
        ):
            raise PilotError(f"pod {name} is not bound to the exact pilot RayCluster lease")
        if pod_metadata.get("deletionTimestamp") is not None:
            raise PilotError(f"pod {name} entered deletion during pilot evidence capture")
        if pod.get("status", {}).get("phase") != "Running":
            raise PilotError(f"pod {name} is not Running during pilot evidence capture")
        uid = pod_metadata.get("uid")
        if not isinstance(uid, str) or not uid or len(uid) > 128:
            raise PilotError(f"pod {name} has an invalid Kubernetes UID")
        role = labels["ray.io/node-type"]
        running_node = pod["spec"].get("nodeName")
        if running_node != node_name:
            raise PilotError(f"pod {name} ran on node {running_node!r}, expected {node_name!r}")
        pod_spec = pod.get("spec")
        pod_status = pod.get("status")
        if not isinstance(pod_spec, dict) or not isinstance(pod_status, dict):
            raise PilotError(f"pod {name} has malformed specification or status")
        containers = _pod_list_field(pod_spec, "containers", pod_name=name)
        statuses = _pod_list_field(pod_status, "containerStatuses", pod_name=name)
        if len(containers) != 1 or len(statuses) != 1:
            raise PilotError(f"pod {name} regular-container inventory changed")
        container = containers[0]
        status = statuses[0]
        if not isinstance(container, dict) or not isinstance(status, dict):
            raise PilotError(f"pod {name} returned malformed regular-container evidence")
        expected_container_name = f"ray-{role}"
        if status.get("ready") is not True:
            raise PilotError(f"pod {name} is not Ready during pilot evidence capture")
        container_name = container.get("name")
        container_id = status.get("containerID")
        if container_name != expected_container_name or container_name != status.get("name"):
            raise PilotError(f"pod {name} container name and status identity differ")
        if not isinstance(container_id, str) or not CONTAINER_ID_PATTERN.fullmatch(container_id):
            raise PilotError(f"pod {name} has an invalid running container identity")
        running_id = _parse_exact_cri_image_id(status.get("imageID"))
        if (
            container.get("image") != image
            or status.get("image") != image
            or running_id != image_id
        ):
            raise PilotError(
                f"pod {name} does not run the tested image: {container.get('image')} {running_id}"
            )
        container_state = status.get("state")
        if (
            not isinstance(container_state, dict)
            or set(container_state) != {"running"}
            or not isinstance(container_state.get("running"), dict)
        ):
            raise PilotError(f"pod {name} regular container is not in an exact running state")
        restart_count = status.get("restartCount")
        if not _is_exact_json_integer(restart_count, expected=0):
            raise PilotError(f"pod {name} restarted during pilot preparation")
        environment = container.get("env", [])
        if not isinstance(environment, list):
            raise PilotError(f"pod {name} returned malformed identity environment")
        object_store = profile["cluster"]["object_store_bytes_per_pod"]
        expected_identity_environment = {
            "DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE": f"{image}@{image_id}",
            "DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE": configuration_id,
            "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE": (
                f"tmpfs:/dev/shm:size={profile['cluster']['shared_memory_bytes_per_pod']}"
            ),
            "DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE": f"plasma:{object_store}",
            "DJANGO_RAY_PILOT_IMAGE_ID": image_id,
            "DJANGO_RAY_PILOT_CONFIG_ID": configuration_id,
            "DJANGO_RAY_PILOT_KUBERAY_VERSION": profile["kuberay"]["operator_version"],
            "DJANGO_RAY_PILOT_NAMESPACE_UID": namespace_lease.uid,
            "DJANGO_RAY_PILOT_RUN_TOKEN": namespace_lease.run_token,
        }
        configuration_values = {
            variable_name: [
                value.get("value")
                for value in environment
                if isinstance(value, dict) and value.get("name") == variable_name
            ]
            for variable_name in expected_identity_environment
        }
        if any(
            configuration_values[variable_name] != [expected_value]
            for variable_name, expected_value in expected_identity_environment.items()
        ):
            raise PilotError(f"pod {name} configuration identity changed or is incomplete")
        observed_identity_environment = {
            variable_name: configuration_values[variable_name][0]
            for variable_name in expected_identity_environment
        }
        expected_role = (
            profile["cluster"]["head"] if role == "head" else profile["cluster"]["workers"]
        )
        observed_init_containers = _verify_init_container_inventory(
            name,
            pod_spec,
            pod_status,
            expected_names=expected_role.get("init_containers"),
            image=image,
            image_id=image_id,
        )
        ephemeral_containers = _pod_list_field(pod_spec, "ephemeralContainers", pod_name=name)
        ephemeral_statuses = _pod_list_field(
            pod_status,
            "ephemeralContainerStatuses",
            pod_name=name,
        )
        if ephemeral_containers or ephemeral_statuses:
            raise PilotError(f"pod {name} has an unexpected ephemeral-container inventory")
        expected_resources = {
            "requests": {
                "cpu": expected_role["cpu_request"],
                "memory": expected_role["memory_request"],
            },
            "limits": {
                "cpu": expected_role["cpu_limit"],
                "memory": expected_role["memory_limit"],
            },
        }
        if container.get("resources") != expected_resources:
            raise PilotError(f"pod {name} resources changed: {container.get('resources')!r}")
        if pod["spec"].get("nodeSelector") != profile["kubernetes"]["node_selector"]:
            raise PilotError(f"pod {name} node selector changed")
        mounts = {
            value.get("name"): value.get("mountPath") for value in container.get("volumeMounts", [])
        }
        volumes = {value.get("name"): value for value in pod["spec"].get("volumes", [])}
        shared_memory = volumes.get("shared-memory", {}).get("emptyDir", {})
        if mounts.get("shared-memory") != "/dev/shm" or shared_memory != {
            "medium": "Memory",
            "sizeLimit": "512Mi",
        }:
            raise PilotError(f"pod {name} shared-memory volume changed")
        expected_cpus = (
            expected_role["ray_cpus"] if role == "head" else expected_role["ray_cpus_per_pod"]
        )
        expected_ray_start_parameters = expected_role.get("ray_start_parameters")
        if (
            not isinstance(expected_ray_start_parameters, dict)
            or expected_ray_start_parameters.get("num-cpus")
            != {"kind": "value", "value": str(expected_cpus)}
            or expected_ray_start_parameters.get("object-store-memory")
            != {"kind": "value", "value": str(object_store)}
        ):
            raise PilotError("Compiled Graph pilot profile has invalid Ray start parameters")
        command_tokens = _ray_start_command_tokens(container.get("args"))
        observed_ray_start_parameters: dict[str, dict[str, Any]] = {}
        for option, value in sorted(expected_ray_start_parameters.items()):
            parameter_spec = _validate_ray_start_parameter_spec(option, value)
            observations = _ray_start_option_observations(
                command_tokens,
                option,
                parameter_spec,
            )
            if len(observations) != 1 or not _ray_start_observation_matches_spec(
                observations[0],
                parameter_spec,
            ):
                raise PilotError(f"pod {name} effective Ray --{option} changed")
            observed_ray_start_parameters[option] = observations[0]
        verified.append(
            {
                "name": name,
                "uid": uid,
                "namespace_uid": namespace_lease.uid,
                "run_token": namespace_lease.run_token,
                "raycluster_uid": raycluster_lease.uid,
                "owner_reference_verified": True,
                "role": role,
                "node": running_node,
                "container_name": container_name,
                "container_id": container_id,
                "image": container.get("image"),
                "image_id": running_id,
                "configuration_id": configuration_id,
                "restart_count": restart_count,
                "phase": "Running",
                "ready": True,
                "container_state": "running",
                "deletion_timestamp": None,
                "identity_environment": observed_identity_environment,
                "init_containers": observed_init_containers,
                "resources": expected_resources,
                "node_selector": profile["kubernetes"]["node_selector"],
                "shared_memory_volume": shared_memory,
                "ray_start_parameters": observed_ray_start_parameters,
            }
        )
    verified.sort(key=lambda value: (value["role"], value["name"]))
    if len({value["name"] for value in verified}) != len(verified):
        raise PilotError("pilot pod names are not unique")
    if len({value["uid"] for value in verified}) != len(verified):
        raise PilotError("pilot pod UIDs are not unique")
    if len({value["container_id"] for value in verified}) != len(verified):
        raise PilotError("pilot container identities are not unique")
    all_container_ids = [
        container_id
        for value in verified
        for container_id in (
            value["container_id"],
            *(container["container_id"] for container in value["init_containers"]),
        )
    ]
    if len(set(all_container_ids)) != len(all_container_ids):
        raise PilotError("pilot regular and init-container identities are not unique")
    return verified


def _verify_pod_execution_identity_unchanged(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(before) != 3 or len(after) != 3:
        raise PilotError("pilot pod identity proof requires exactly three pods")
    if not _json_exact_equal(before, after):
        raise PilotError(
            "pilot pod or container execution identity changed during cleanup wait; evidence differs"
        )
    return {
        "status": "success",
        "pod_set_unchanged": True,
        "pod_uids_unchanged": True,
        "container_ids_unchanged": True,
        "image_ids_unchanged": True,
        "configuration_ids_unchanged": True,
        "restart_counts_unchanged": True,
        "pod_lifecycle_unchanged": True,
        "identity_environments_unchanged": True,
        "ray_start_parameters_unchanged": True,
        "init_container_inventories_unchanged": True,
    }


def _head_pod(pods: list[dict[str, Any]]) -> str:
    heads = [
        pod["metadata"]["name"]
        for pod in pods
        if pod["metadata"]["labels"].get("ray.io/node-type") == "head"
    ]
    if len(heads) != 1:
        raise PilotError("expected exactly one pilot head pod")
    return heads[0]


def _capture_final_runtime_and_cluster_evidence(
    context: str,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
    *,
    baseline_pod_evidence: list[dict[str, Any]],
    image: str,
    image_id: str,
    configuration_id: str,
    node_name: str,
    operator_profile: dict[str, Any],
    expected_operator: dict[str, Any],
) -> dict[str, Any]:
    capture_pods_before = _fetch_pilot_pods(
        context,
        namespace_lease,
        raycluster_lease,
    )
    capture_evidence_before = _verify_pod_images(
        capture_pods_before,
        image,
        image_id,
        configuration_id,
        node_name,
        namespace_lease,
        raycluster_lease,
    )
    _verify_pod_execution_identity_unchanged(
        baseline_pod_evidence,
        capture_evidence_before,
    )
    runtime_after = _capture_pod_runtime_snapshots(
        context,
        namespace_lease,
        raycluster_lease,
        capture_pods_before,
    )
    final_head = _head_pod(capture_pods_before)
    cluster_state_after = _kubectl_exec_json(
        context,
        namespace_lease,
        raycluster_lease,
        final_head,
        ["inspect-cluster-state"],
        timeout_seconds=60,
    )
    if cluster_state_after.get("status") != "success":
        raise PilotError("final post-wait cluster cleanup inspection failed")

    capture_pods_after = _fetch_pilot_pods(
        context,
        namespace_lease,
        raycluster_lease,
    )
    capture_evidence_after = _verify_pod_images(
        capture_pods_after,
        image,
        image_id,
        configuration_id,
        node_name,
        namespace_lease,
        raycluster_lease,
    )
    capture_identity = _verify_pod_execution_identity_unchanged(
        capture_evidence_before,
        capture_evidence_after,
    )
    pod_identity = _verify_pod_execution_identity_unchanged(
        baseline_pod_evidence,
        capture_evidence_after,
    )
    _assert_kuberay_operator_identity_unchanged(
        context,
        operator_profile,
        expected_operator,
    )
    return {
        "pod_evidence_before": capture_evidence_before,
        "pod_evidence_after": capture_evidence_after,
        "capture_identity": capture_identity,
        "pod_identity": pod_identity,
        "runtime_after": runtime_after,
        "cluster_state_after": cluster_state_after,
    }


def _kubectl_exec_json(
    context: str,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
    pod: str,
    arguments: list[str],
    *,
    timeout_seconds: float,
    container: str = "ray-head",
) -> dict[str, Any]:
    _validate_raycluster_lease(raycluster_lease, namespace_lease)
    namespace = namespace_lease.name
    command = [
        "kubectl",
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        pod,
        "-c",
        container,
        "--",
        "python",
        "/opt/django-ray/scripts/kuberay_compiled_graph_pilot.py",
        *arguments,
    ]
    with _current_pilot_lease_boundary(
        context,
        namespace_lease,
        raycluster_lease,
    ):
        result = _run_command(
            command,
            timeout_seconds=timeout_seconds,
            preserve_stdout=True,
        )
    return _parse_json_command_output(result, command, expected_type=dict)


def _capture_pod_runtime_snapshots(
    context: str,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
    pods: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for pod in sorted(pods, key=lambda value: value["metadata"]["name"]):
        name = pod["metadata"]["name"]
        container = pod["spec"]["containers"][0]["name"]
        snapshot = _kubectl_exec_json(
            context,
            namespace_lease,
            raycluster_lease,
            name,
            ["inspect-pod-runtime"],
            timeout_seconds=30,
            container=container,
        )
        if snapshot.get("status") != "success":
            raise PilotError(f"pod {name} runtime inspection failed")
        snapshots[name] = snapshot
    return snapshots


def _assess_runtime_cleanup(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if before.keys() != after.keys():
        raise PilotError("pilot pod set changed during native execution")
    pod_results: dict[str, dict[str, Any]] = {}
    failure_reasons: set[str] = set()
    pilot_child_processes_remaining = 0
    for name in before:
        before_shm = before[name]["shared_memory"]
        after_shm = after[name]["shared_memory"]
        if before[name].get("pilot_child_process_count") != 0:
            raise PilotError(f"pod {name} had a pilot child process before native execution")
        if before_shm["entry_count"] != 0 or before_shm["entry_bytes"] != 0:
            raise PilotError(f"pod {name} did not have an empty shared-memory baseline")
        if before_shm["total_bytes"] != 536_870_912 or after_shm["total_bytes"] != 536_870_912:
            raise PilotError(f"pod {name} effective /dev/shm capacity changed")
        remaining_children_value = after[name].get("pilot_child_process_count")
        if not isinstance(remaining_children_value, int) or remaining_children_value < 0:
            raise PilotError(f"pod {name} returned an invalid pilot child-process count")
        remaining_children = remaining_children_value
        if remaining_children != 0:
            failure_reasons.add("pilot_child_processes_remaining")
            pilot_child_processes_remaining += remaining_children
        entries_restored = not (
            before_shm["entry_count"] != after_shm["entry_count"]
            or before_shm["entry_bytes"] != after_shm["entry_bytes"]
            or before_shm.get("entry_identity_digest") != after_shm.get("entry_identity_digest")
        )
        if not entries_restored:
            failure_reasons.add("shared_memory_entries_not_restored")
        available_delta = after_shm["available_bytes"] - before_shm["available_bytes"]
        available_memory_restored = available_delta >= -(1024 * 1024)
        if not available_memory_restored:
            failure_reasons.add("shared_memory_available_bytes_not_restored")
        pod_results[name] = {
            "before": dict(before_shm),
            "after": dict(after_shm),
            "pilot_child_process_count_before": 0,
            "pilot_child_process_count_after": remaining_children,
            "entries_restored": entries_restored,
            "available_memory_restored": available_memory_restored,
            "deltas": {
                "available_bytes": available_delta,
                "entry_count": after_shm["entry_count"] - before_shm["entry_count"],
                "entry_bytes": after_shm["entry_bytes"] - before_shm["entry_bytes"],
            },
        }
    failure_reason_list = sorted(failure_reasons)
    return {
        "status": "failure" if failure_reason_list else "success",
        "failure_classification": (
            "runtime_cleanup_invariant_failed" if failure_reason_list else None
        ),
        "failure_reasons": failure_reason_list,
        "pod_set_unchanged": True,
        "shared_memory_entries_restored": not any(
            not result["entries_restored"] for result in pod_results.values()
        ),
        "shared_memory_entry_identity_restored": not any(
            result["before"]["entry_identity_digest"] != result["after"]["entry_identity_digest"]
            for result in pod_results.values()
        ),
        "available_memory_restored": not any(
            not result["available_memory_restored"] for result in pod_results.values()
        ),
        "pilot_child_processes_remaining": pilot_child_processes_remaining,
        "available_memory_tolerance_bytes": 1024 * 1024,
        "stable_paired_semaphore_fingerprints": False,
        "pods": pod_results,
    }


def _finalize_runtime_cleanup_assessment(
    before: dict[str, dict[str, Any]],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    if not observations:
        raise PilotError("runtime cleanup produced no observations")
    recomputed_assessments: list[dict[str, Any]] = []
    for observation in observations:
        pods = observation.get("pods")
        if not isinstance(pods, dict):
            raise PilotError("runtime cleanup observation has an invalid pod snapshot")
        assessment = _assess_runtime_cleanup(before, pods)
        if not _json_exact_equal(observation.get("assessment"), assessment):
            raise PilotError("runtime cleanup observation assessment does not match its snapshot")
        recomputed_assessments.append(assessment)

    final_assessment = dict(recomputed_assessments[-1])
    if final_assessment["status"] == "success":
        return final_assessment

    required_cumulative_waits = [0]
    for wait_seconds in CLEANUP_RETRY_DELAYS_SECONDS[1:]:
        required_cumulative_waits.append(required_cumulative_waits[-1] + wait_seconds)
    complete_wait_window = (
        len(observations) >= len(required_cumulative_waits)
        and [
            observation.get("cumulative_wait_seconds")
            for observation in observations[: len(required_cumulative_waits)]
        ]
        == required_cumulative_waits
        and observations[-1].get("cumulative_wait_seconds") == required_cumulative_waits[-1]
    )

    shared_memory_failure_reasons = {
        "shared_memory_entries_not_restored",
        "shared_memory_available_bytes_not_restored",
    }
    stable_fingerprints = complete_wait_window and all(
        assessment["status"] == "failure"
        and "shared_memory_entries_not_restored" in assessment["failure_reasons"]
        and set(assessment["failure_reasons"]).issubset(shared_memory_failure_reasons)
        for assessment in recomputed_assessments
    )
    final_pair_count = 0
    fingerprint_rows: list[tuple[str, int, int, int, str, str]] = []
    for pod_name in sorted(before):
        pod_fingerprints: list[tuple[int, int, int, str, str]] = []
        for observation in observations:
            shared_memory = observation["pods"][pod_name]["shared_memory"]
            semaphore = shared_memory.get("ray_mutable_object_semaphores")
            if not isinstance(semaphore, dict):
                stable_fingerprints = False
                continue
            entry_count = shared_memory["entry_count"]
            pair_count = semaphore.get("pair_count")
            header_count = semaphore.get("header_count")
            object_count = semaphore.get("object_count")
            recognized = (
                isinstance(pair_count, int)
                and pair_count >= 0
                and header_count == pair_count
                and object_count == pair_count
                and semaphore.get("paired_entry_count") == pair_count * 2
                and semaphore.get("unpaired_entry_count") == 0
                and semaphore.get("other_entry_count") == 0
                and entry_count == pair_count * 2
                and (entry_count == 0 or semaphore.get("fully_paired_and_exclusive") is True)
            )
            if not recognized:
                stable_fingerprints = False
            pod_fingerprints.append(
                (
                    entry_count,
                    int(header_count) if isinstance(header_count, int) else -1,
                    int(object_count) if isinstance(object_count, int) else -1,
                    str(semaphore.get("pair_identity_digest", "")),
                    str(semaphore.get("semaphore_identity_digest", "")),
                )
            )
        if not pod_fingerprints or len(set(pod_fingerprints)) != 1:
            stable_fingerprints = False
            continue
        entry_count, header_count, object_count, pair_digest, semaphore_digest = pod_fingerprints[
            -1
        ]
        pair_count = header_count
        final_pair_count += pair_count
        fingerprint_rows.append(
            (
                pod_name,
                entry_count,
                header_count,
                object_count,
                pair_digest,
                semaphore_digest,
            )
        )
    if final_pair_count == 0:
        stable_fingerprints = False

    fingerprint_digest = sha256(
        json.dumps(fingerprint_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    final_assessment["stable_paired_semaphore_fingerprints"] = stable_fingerprints
    final_assessment["cleanup_observation_count"] = len(observations)
    final_assessment["cleanup_observation_wait_seconds"] = observations[-1].get(
        "cumulative_wait_seconds"
    )
    final_assessment["ray_mutable_object_semaphore_pair_count"] = final_pair_count
    final_assessment["ray_mutable_object_semaphore_pair_fingerprint_digest"] = fingerprint_digest
    if stable_fingerprints:
        final_assessment["failure_classification"] = MUTABLE_OBJECT_CLEANUP_CLASSIFICATION
    else:
        final_assessment["failure_reasons"] = sorted(
            {
                *final_assessment["failure_reasons"],
                "shared_memory_residual_not_stable_paired_ray_semaphores",
            }
        )
    return final_assessment


def _verify_runtime_cleanup(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    assessment = _assess_runtime_cleanup(before, after)
    if assessment["status"] == "success":
        return assessment
    if "shared_memory_entries_not_restored" in assessment["failure_reasons"]:
        raise PilotError("pilot pods did not restore exact shared-memory entries after teardown")
    if "pilot_child_processes_remaining" in assessment["failure_reasons"]:
        raise PilotError("pilot runtime cleanup retained a pilot child process")
    raise PilotError("pilot runtime cleanup invariants failed after teardown")


def _observe_runtime_cleanup(
    context: str,
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
    pods: list[dict[str, Any]],
    before: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    observations: list[dict[str, Any]] = []
    cumulative_wait_seconds = 0
    after: dict[str, dict[str, Any]] = {}
    assessment: dict[str, Any] = {}
    for attempt, wait_seconds in enumerate(CLEANUP_RETRY_DELAYS_SECONDS, start=1):
        if wait_seconds:
            time.sleep(wait_seconds)
            cumulative_wait_seconds += wait_seconds
        after = _capture_pod_runtime_snapshots(
            context,
            namespace_lease,
            raycluster_lease,
            pods,
        )
        assessment = _assess_runtime_cleanup(before, after)
        observations.append(
            {
                "attempt": attempt,
                "wait_before_seconds": wait_seconds,
                "cumulative_wait_seconds": cumulative_wait_seconds,
                "pods": after,
                "assessment": assessment,
            }
        )
        if assessment["status"] == "success":
            break
    assessment = _finalize_runtime_cleanup_assessment(before, observations)
    return after, assessment, observations


def _blocked_runtime_cleanup_result(
    common_result: dict[str, Any],
    shared_memory_cleanup: dict[str, Any],
    cluster_state_after: dict[str, Any],
    pod_runtime_after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    classification = shared_memory_cleanup.get("failure_classification")
    if shared_memory_cleanup.get("status") == "success" or not classification:
        raise PilotError("cannot build blocked evidence from a successful cleanup")
    known_upstream_blocker = classification == MUTABLE_OBJECT_CLEANUP_CLASSIFICATION
    zero_residual_state = {
        "active_pilot_actor_count": cluster_state_after["active_pilot_actor_count"],
        "active_pilot_task_count": cluster_state_after["active_pilot_task_count"],
        "object_count": cluster_state_after["object_count"],
        "object_bytes": cluster_state_after["object_bytes"],
        "pilot_child_process_count": sum(
            snapshot["pilot_child_process_count"] for snapshot in pod_runtime_after.values()
        ),
    }
    if any(zero_residual_state.values()):
        raise PilotError(
            "blocked shared-memory evidence requires every other residual count to be zero"
        )
    result = {
        **common_result,
        "status": "blocked" if known_upstream_blocker else "failure",
        "failure": {
            "classification": classification,
            "invariant": "exact_shared_memory_restoration_after_graph_teardown",
            "summary": (
                "Compiled Graph probes completed, but runtime cleanup did not "
                "restore the pinned pod state."
            ),
            "tracker_urls": (
                list(BLOCKER_TRACKERS) if known_upstream_blocker else [BLOCKER_TRACKERS[0]]
            ),
        },
        "zero_residual_state": zero_residual_state,
    }
    if known_upstream_blocker:
        _validate_current_blocked_evidence_record(
            result,
            require_namespace_deleted=False,
        )
    return result


def _cleanup_namespace(context: str, namespace_lease: NamespaceLease) -> None:
    _validate_namespace_lease(namespace_lease)
    namespace = namespace_lease.name
    current = _run_command(["kubectl", "config", "current-context"]).stdout.strip()
    if current != context or namespace != PILOT_NAMESPACE:
        raise PilotError("refusing pilot cleanup after context or namespace drift")
    existing = _run_command(
        [
            "kubectl",
            "--context",
            context,
            "get",
            "namespace",
            namespace,
            "--ignore-not-found=true",
            "-o",
            "json",
        ],
        check=False,
        preserve_stdout=True,
    )
    if existing.returncode != 0:
        raise PilotError("could not verify the pilot namespace before cleanup")
    if not existing.stdout.strip():
        return
    namespace_record = _parse_json_command_output(
        existing,
        ["kubectl", "get", "namespace"],
        expected_type=dict,
    )
    metadata = namespace_record.get("metadata")
    if not _namespace_metadata_matches_lease(metadata, namespace_lease, require_live=True):
        raise PilotError("refusing cleanup of a namespace outside the exact pilot lease")
    selector = (
        f"{PILOT_PROFILE_LABEL_KEY}={PROFILE_NAME},"
        f"{PILOT_RUN_LABEL_KEY}={namespace_lease.run_token}"
    )
    # ``kubectl delete`` deliberately performs no resource-version check and
    # exposes no UID precondition.  Keep the deletion server-filtered by the
    # fixed name plus both ownership labels, then verify that neither the exact
    # UID nor a same-name replacement remains.  This narrows but cannot make
    # the CLI operation an atomic Kubernetes DeleteOptions precondition.
    _run_command(
        [
            "kubectl",
            "--context",
            context,
            "delete",
            "namespace",
            "--field-selector",
            f"metadata.name={namespace}",
            "--selector",
            selector,
            "--ignore-not-found=true",
            "--wait=true",
            "--timeout=180s",
        ],
        timeout_seconds=240,
    )
    absent = _run_command(
        [
            "kubectl",
            "--context",
            context,
            "get",
            "namespace",
            namespace,
            "--ignore-not-found=true",
            "-o",
            "json",
        ],
        check=False,
        preserve_stdout=True,
    )
    if absent.returncode != 0:
        raise PilotError("could not verify pilot namespace absence after cleanup")
    if absent.stdout.strip():
        remaining_record = _parse_json_command_output(
            absent,
            ["kubectl", "get", "namespace"],
            expected_type=dict,
        )
        remaining_metadata = remaining_record.get("metadata")
        if isinstance(remaining_metadata, dict) and remaining_metadata.get("uid") == (
            namespace_lease.uid
        ):
            raise PilotError("exact pilot namespace UID still exists after cleanup")
        raise PilotError("pilot namespace name was replaced before cleanup completed")


def run_host_pilot(context: str, namespace: str, *, keep_cluster: bool) -> dict[str, Any]:
    """Build, deploy, execute, and tear down one exact local pilot."""
    profile = _load_profile()
    if not _json_exact_equal(
        profile.get("probe", {}).get("cleanup_retry_delays_seconds"),
        list(CLEANUP_RETRY_DELAYS_SECONDS),
    ):
        raise PilotError("Compiled Graph pilot cleanup observation schedule changed")
    profile_id = _profile_identity(profile)
    host = _validate_host_target(context, namespace)
    revision = _git_source_revision()
    image, image_id, docker_context = _build_image(revision)
    config_id = _configuration_identity()
    near_neighbor = _run_near_neighbor_container(
        image,
        image_id,
        config_id,
        docker_context,
    )
    started_at = datetime.now(UTC)
    pods: list[dict[str, Any]] = []
    result_record: dict[str, Any] | None = None
    namespace_lease: NamespaceLease | None = None
    raycluster_lease: RayClusterLease | None = None
    try:
        namespace_lease = _ensure_namespace(context, namespace)
        manifest, rendered_config_id, manifest_id = _render_manifest(
            image,
            image_id,
            namespace_lease,
        )
        if rendered_config_id != config_id:
            raise PilotError("pilot configuration identity changed before manifest rendering")
        pods, raycluster_lease = _create_and_wait(
            context,
            namespace_lease,
            manifest,
            operator_profile=profile["kuberay"],
            expected_operator=host["kuberay_operator"],
        )
        node_name = host["kubernetes"]["node"]["name"]
        pod_evidence = _verify_pod_images(
            pods,
            image,
            image_id,
            config_id,
            node_name,
            namespace_lease,
            raycluster_lease,
        )
        head = _head_pod(pods)
        if (
            near_neighbor.get("status") != "success"
            or near_neighbor.get("reason") != PILOT_PROFILE_MISMATCH
            or near_neighbor.get("baseline_admission", {}).get("admitted") is not True
            or near_neighbor.get("changed_admission", {}).get("admitted") is not False
            or not _json_exact_equal(
                near_neighbor.get("pilot_dependency_profile"), profile["dependency_profile"]
            )
            or near_neighbor.get("child_spawned") is not False
            or near_neighbor.get("native_started") is not False
        ):
            raise PilotError("near-neighbor capability did not fail closed before native execution")

        hard_timeout = _kubectl_exec_json(
            context,
            namespace_lease,
            raycluster_lease,
            head,
            [
                "hard-timeout",
                "--timeout-seconds",
                str(profile["probe"]["hard_timeout_self_test_seconds"]),
            ],
            timeout_seconds=20,
        )
        if (
            hard_timeout.get("status") != "success"
            or hard_timeout.get("hard_timeout_observed") is not True
            or hard_timeout.get("child_process_group_empty") is not True
        ):
            raise PilotError("subprocess hard-timeout containment self-test failed")

        pod_runtime_before = _capture_pod_runtime_snapshots(
            context,
            namespace_lease,
            raycluster_lease,
            pods,
        )
        cluster_state_before = _kubectl_exec_json(
            context,
            namespace_lease,
            raycluster_lease,
            head,
            ["inspect-cluster-state"],
            timeout_seconds=60,
        )
        if cluster_state_before.get("status") != "success":
            raise PilotError("pre-probe cluster cleanup baseline failed")

        topologies = []
        for topology in (
            CompiledGraphTopology.DIRECT_DRIVER.value,
            CompiledGraphTopology.NESTED_RAY_TASK.value,
        ):
            result = _kubectl_exec_json(
                context,
                namespace_lease,
                raycluster_lease,
                head,
                ["probe", "--topology", topology, "--timeout-seconds", "180"],
                timeout_seconds=210,
            )
            if result.get("status") != "success":
                raise PilotError(f"{topology} pilot failed: {result!r}")
            topologies.append(result)

        post_pods = _fetch_pilot_pods(
            context,
            namespace_lease,
            raycluster_lease,
        )
        _verify_pod_images(
            post_pods,
            image,
            image_id,
            config_id,
            node_name,
            namespace_lease,
            raycluster_lease,
        )
        _, _, cleanup_observations = _observe_runtime_cleanup(
            context,
            namespace_lease,
            raycluster_lease,
            post_pods,
            pod_runtime_before,
        )

        final_capture = _capture_final_runtime_and_cluster_evidence(
            context,
            namespace_lease,
            raycluster_lease,
            baseline_pod_evidence=pod_evidence,
            image=image,
            image_id=image_id,
            configuration_id=config_id,
            node_name=node_name,
            operator_profile=profile["kuberay"],
            expected_operator=host["kuberay_operator"],
        )
        final_capture_evidence_before = final_capture["pod_evidence_before"]
        final_capture_evidence_after = final_capture["pod_evidence_after"]
        final_capture_identity = final_capture["capture_identity"]
        pod_identity = final_capture["pod_identity"]
        pod_runtime_after = final_capture["runtime_after"]
        cluster_state_after = final_capture["cluster_state_after"]
        final_runtime_assessment = _assess_runtime_cleanup(
            pod_runtime_before,
            pod_runtime_after,
        )
        cleanup_observations.append(
            {
                "attempt": len(cleanup_observations) + 1,
                "phase": "final_capture_bracket_verified",
                "wait_before_seconds": 0,
                "cumulative_wait_seconds": sum(CLEANUP_RETRY_DELAYS_SECONDS),
                "pods": pod_runtime_after,
                "assessment": final_runtime_assessment,
            }
        )
        shared_memory_cleanup = _finalize_runtime_cleanup_assessment(
            pod_runtime_before,
            cleanup_observations,
        )

        cluster_cleanup = _verify_cluster_cleanup(cluster_state_before, cluster_state_after)
        common_result = {
            "schema_version": PILOT_SCHEMA_VERSION,
            "evidence_id": f"local-kuberay:{revision}:{image_id}",
            "profile_name": PROFILE_NAME,
            "profile_id": profile_id,
            "candidate_native": True,
            "supported_product_execution": False,
            "promotion_eligible": False,
            "pilot_evidence_passed": False,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "source_revision": revision,
            "image": image,
            "image_id": image_id,
            "configuration_id": config_id,
            "rendered_manifest_id": manifest_id,
            "kubernetes_context": context,
            "namespace": namespace,
            "namespace_lease": namespace_lease.asdict(),
            "raycluster_lease": raycluster_lease.asdict(),
            "docker": docker_context,
            "kuberay_operator": host["kuberay_operator"],
            "kubernetes": host["kubernetes"],
            "profile": profile,
            "pods": {
                "before": pod_evidence,
                "after": final_capture_evidence_after,
                "identity": pod_identity,
                "final_capture_before": final_capture_evidence_before,
                "final_capture_after": final_capture_evidence_after,
                "final_capture_identity": final_capture_identity,
                "runtime_before": pod_runtime_before,
                "runtime_after": pod_runtime_after,
            },
            "near_neighbor": near_neighbor,
            "hard_timeout": hard_timeout,
            "topologies": topologies,
            "cleanup": {
                "compiled_graph_teardown_verified": False,
                "shared_memory": shared_memory_cleanup,
                "shared_memory_observations": cleanup_observations,
                "cluster_state_before": cluster_state_before,
                "cluster_state_after": cluster_state_after,
                "cluster_state": cluster_cleanup,
                "pilot_namespace_deleted": False,
                "unrelated_namespaces_touched": [],
            },
        }
        if shared_memory_cleanup["status"] != "success":
            result_record = _blocked_runtime_cleanup_result(
                common_result,
                shared_memory_cleanup,
                cluster_state_after,
                pod_runtime_after,
            )
        else:
            shared_memory_cleanup = _verify_runtime_cleanup(
                pod_runtime_before,
                pod_runtime_after,
            )
            result_record = {
                **common_result,
                "status": "success",
                "pilot_evidence_passed": True,
                "cleanup": {
                    "compiled_graph_teardown_verified": True,
                    "shared_memory": shared_memory_cleanup,
                    "shared_memory_observations": cleanup_observations,
                    "cluster_state_before": cluster_state_before,
                    "cluster_state_after": cluster_state_after,
                    "cluster_state": cluster_cleanup,
                    "pilot_namespace_deleted": False,
                    "unrelated_namespaces_touched": [],
                },
            }
    finally:
        if not keep_cluster and namespace_lease is not None:
            _cleanup_namespace(context, namespace_lease)
            if result_record is not None:
                result_record["cleanup"]["pilot_namespace_deleted"] = True
                result_record["completed_at"] = datetime.now(UTC).isoformat()
    if result_record is None:  # pragma: no cover - every normal path assigns a result
        raise PilotError("pilot completed without a retained result")
    return result_record


class _PilotStage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.invocations = 0

    def apply(self, value: dict[str, Any]) -> dict[str, Any]:
        self.invocations += 1
        result = dict(value)
        trace = list(result.get("trace", []))
        trace.append({"stage": self.name, "invocation": self.invocations})
        result["trace"] = trace
        result["value"] = int(result["value"]) + 1
        return result

    def ping(self) -> str:
        return "alive"


class _PilotFailureStage:
    def fail(self, marker: str) -> None:
        raise PilotApplicationError(marker)

    def ping(self) -> str:
        return "alive"


class _PilotDelayStage:
    def delay(self, seconds: float) -> str:
        time.sleep(seconds)
        return "delayed-result-consumed"

    def ping(self) -> str:
        return "alive"


def _actor_options(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "lifetime": "non_detached",
        "num_cpus": 1,
        "max_restarts": 0,
        "max_task_retries": 0,
    }


def _dead_actor_observation(ray: Any, name: str, actor: Any) -> dict[str, Any] | None:
    from ray._private.state import actors

    try:
        ray.get(actor.ping.remote(), timeout=1)
    except ray.exceptions.RayActorError:
        actor_id = actor._actor_id.hex()
        state = actors(actor_id)
        if isinstance(state, dict) and state.get("State") == "DEAD":
            return {"name": name, "actor_id": actor_id, "state": "DEAD"}
        return None
    except BaseException as error:
        raise PilotError(
            f"actor {name!r} cleanup produced {type(error).__name__}, not RayActorError"
        ) from error
    return None


def _teardown_and_verify(
    ray: Any,
    compiled: Any,
    actors: list[tuple[str, Any]],
) -> list[dict[str, Any]]:
    compiled.teardown(kill_actors=True)
    deadline = time.monotonic() + 15
    last_observations: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        observations = []
        for name, actor in actors:
            observation = _dead_actor_observation(ray, name, actor)
            if observation is not None:
                observations.append(observation)
        last_observations = observations
        if len(observations) == len(actors):
            return observations
        time.sleep(0.25)
    raise PilotError(
        f"compiled graph actors were not all DEAD after explicit teardown: {last_observations!r}"
    )


def _normal_graph_case(ray: Any, namespace: str) -> dict[str, Any]:
    from ray.dag import InputNode

    prefix = f"cgraph-normal-{uuid4().hex}"
    stage = ray.remote(_PilotStage)
    left_name = f"{prefix}-left"
    right_name = f"{prefix}-right"
    left = stage.options(**_actor_options(left_name)).remote("left")
    right = stage.options(**_actor_options(right_name)).remote("right")
    actors = [(left_name, left), (right_name, right)]
    compiled = None
    compile_started = time.monotonic()
    try:
        with InputNode() as graph_input:
            graph = left.apply.bind(graph_input)
            graph = right.apply.bind(graph)
        compiled = graph.experimental_compile(
            _max_inflight_executions=1,
            _max_buffered_results=1,
        )
        compile_seconds = time.monotonic() - compile_started
        results = []
        for index in range(3):
            result = ray.get(
                compiled.execute({"value": index, "trace": [], "namespace": namespace}),
                timeout=20,
            )
            expected_trace = [
                {"stage": "left", "invocation": index + 1},
                {"stage": "right", "invocation": index + 1},
            ]
            if result.get("value") != index + 2 or result.get("trace") != expected_trace:
                raise PilotError(f"repeated invocation parity changed: {result!r}")
            results.append({"index": index, "value": result["value"], "trace": result["trace"]})
        actor_observations = _teardown_and_verify(ray, compiled, actors)
        compiled = None
        return {
            "compile_seconds": round(compile_seconds, 6),
            "invocations": results,
            "ordered_results_consumed": True,
            "results_submitted": 3,
            "results_consumed": 3,
            "results_discarded_by_teardown": 0,
            "teardown_returned": True,
            "actor_state_observations": actor_observations,
            "actors_terminated": True,
            "max_inflight_executions": 1,
            "max_buffered_results": 1,
        }
    finally:
        if compiled is not None:
            try:
                compiled.teardown(kill_actors=True)
            except BaseException:
                for _name, actor in actors:
                    try:
                        ray.kill(actor, no_restart=True)
                    except BaseException:
                        pass


def _application_exception_case(ray: Any) -> dict[str, Any]:
    from ray.dag import InputNode

    marker = f"pilot-application-error-{uuid4().hex}"
    actor_name = f"cgraph-failure-{uuid4().hex}"
    actor = ray.remote(_PilotFailureStage).options(**_actor_options(actor_name)).remote()
    compiled = None
    try:
        with InputNode() as graph_input:
            graph = actor.fail.bind(graph_input)
        compiled = graph.experimental_compile(
            _max_inflight_executions=1,
            _max_buffered_results=1,
        )
        try:
            ray.get(compiled.execute(marker), timeout=20)
        except BaseException as error:
            if marker not in str(error):
                raise PilotError(f"application exception lost its marker: {error}") from error
            error_type = type(error).__name__
        else:
            raise PilotError("application exception unexpectedly succeeded")
        actor_observations = _teardown_and_verify(ray, compiled, [(actor_name, actor)])
        compiled = None
        return {
            "error_type": error_type,
            "marker_preserved": True,
            "result_consumed": True,
            "results_submitted": 1,
            "results_consumed": 1,
            "results_discarded_by_teardown": 0,
            "teardown_returned": True,
            "actor_state_observations": actor_observations,
            "actors_terminated": True,
        }
    finally:
        if compiled is not None:
            try:
                compiled.teardown(kill_actors=True)
            except BaseException:
                try:
                    ray.kill(actor, no_restart=True)
                except BaseException:
                    pass


def _result_timeout_case(ray: Any) -> dict[str, Any]:
    from ray.dag import InputNode

    actor_name = f"cgraph-delay-{uuid4().hex}"
    actor = ray.remote(_PilotDelayStage).options(**_actor_options(actor_name)).remote()
    compiled = None
    try:
        with InputNode() as graph_input:
            graph = actor.delay.bind(graph_input)
        compiled = graph.experimental_compile(
            _max_inflight_executions=1,
            _max_buffered_results=1,
        )
        reference = compiled.execute(1.0)
        try:
            ray.get(reference, timeout=0.05)
        except BaseException as error:
            if (
                "timeout" not in type(error).__name__.lower()
                and "timeout" not in str(error).lower()
            ):
                raise PilotError(f"unexpected timeout classification: {error}") from error
            timeout_type = type(error).__name__
        else:
            raise PilotError("delayed Compiled Graph result did not time out")
        actor_observations = _teardown_and_verify(ray, compiled, [(actor_name, actor)])
        compiled = None
        return {
            "timeout_type": timeout_type,
            "result_consumption_attempted_once": True,
            "timed_out_result_discarded_by_teardown": True,
            "results_submitted": 1,
            "results_consumed": 0,
            "results_discarded_by_teardown": 1,
            "teardown_returned": True,
            "actor_state_observations": actor_observations,
            "actors_terminated": True,
        }
    finally:
        if compiled is not None:
            try:
                compiled.teardown(kill_actors=True)
            except BaseException:
                try:
                    ray.kill(actor, no_restart=True)
                except BaseException:
                    pass


def _sanitized_runtime(ray: Any) -> dict[str, Any]:
    dependency_names = (
        "django-ray",
        "django",
        "asgiref",
        "sqlparse",
        "ray",
        "numpy",
        "pyarrow",
        "cupy",
        "cupy-cuda11x",
        "cupy-cuda12x",
        "fastrlock",
    )
    dependencies: dict[str, str] = {}
    for name in dependency_names:
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = "absent"
    os_release: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in {"ID", "VERSION_ID", "PRETTY_NAME"}:
                    os_release[key] = value.strip('"')
    except OSError:
        pass
    stat = os.statvfs("/dev/shm")
    alive_nodes = [node for node in ray.nodes() if node.get("Alive") is True]
    return {
        "runtime_identity": detect_compiled_graph_runtime().asdict(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "libc": list(platform.libc_ver()),
        "os_release": os_release,
        "dependencies": dependencies,
        "shared_memory_bytes": stat.f_frsize * stat.f_blocks,
        "alive_ray_nodes": len(alive_nodes),
        "cluster_resources": {
            key: value
            for key, value in sorted(ray.cluster_resources().items())
            if key in {"CPU", "memory", "object_store_memory"}
        },
        "source_revision": os.environ.get("DJANGO_RAY_PILOT_SOURCE_REVISION", ""),
        "image_id": os.environ.get("DJANGO_RAY_PILOT_IMAGE_ID", ""),
        "configuration_id": os.environ.get("DJANGO_RAY_PILOT_CONFIG_ID", ""),
        "kuberay_version": os.environ.get("DJANGO_RAY_PILOT_KUBERAY_VERSION", ""),
    }


def _shared_memory_entry_summary(entries: list[tuple[str, int]]) -> dict[str, Any]:
    entry_identities = sorted(
        (
            {
                "name_sha256": sha256(name.encode("utf-8")).hexdigest(),
                "size": size,
            }
            for name, size in entries
        ),
        key=lambda value: (value["name_sha256"], value["size"]),
    )
    entry_identity_digest = sha256(
        json.dumps(entry_identities, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    pair_kinds: dict[str, set[str]] = {}
    semaphore_identities: list[tuple[str, str, int]] = []
    other_identities: list[tuple[str, int]] = []
    header_count = 0
    object_count = 0
    for name, size in entries:
        match = RAY_MUTABLE_OBJECT_SEMAPHORE_PATTERN.fullmatch(name)
        if match is None:
            other_identities.append((name, size))
            continue
        kind = match.group("kind")
        pair_id = match.group("pair_id")
        pair_kinds.setdefault(pair_id, set()).add(kind)
        semaphore_identities.append((kind, pair_id, size))
        if kind == "hdr":
            header_count += 1
        else:
            object_count += 1

    paired_ids = sorted(pair_id for pair_id, kinds in pair_kinds.items() if kinds == {"hdr", "obj"})
    unpaired_ids = sorted(
        (pair_id, sorted(kinds)) for pair_id, kinds in pair_kinds.items() if kinds != {"hdr", "obj"}
    )

    def digest(value: Any) -> str:
        return sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    paired_entry_count = len(paired_ids) * 2
    unpaired_entry_count = len(semaphore_identities) - paired_entry_count
    other_entry_count = len(other_identities)
    return {
        "entry_count": len(entries),
        "entry_bytes": sum(size for _name, size in entries),
        "entry_identity_digest": entry_identity_digest,
        "ray_mutable_object_semaphores": {
            "header_count": header_count,
            "object_count": object_count,
            "pair_count": len(paired_ids),
            "paired_entry_count": paired_entry_count,
            "unpaired_entry_count": unpaired_entry_count,
            "other_entry_count": other_entry_count,
            "fully_paired_and_exclusive": (
                bool(paired_ids)
                and unpaired_entry_count == 0
                and other_entry_count == 0
                and paired_entry_count == len(entries)
            ),
            "pair_identity_digest": digest(paired_ids),
            "semaphore_identity_digest": digest(sorted(semaphore_identities)),
            "unpaired_identity_digest": digest(unpaired_ids),
            "other_entry_identity_digest": digest(sorted(other_identities)),
        },
    }


def _inspect_pod_runtime() -> dict[str, Any]:
    stat = os.statvfs("/dev/shm")
    entries: list[tuple[str, int]] = []
    try:
        for entry in Path("/dev/shm").iterdir():
            try:
                entry_stat = entry.stat()
            except OSError:
                continue
            entries.append((entry.name, entry_stat.st_size))
    except OSError as error:
        raise PilotError("cannot inspect pod shared memory") from error
    shared_memory_entries = _shared_memory_entry_summary(entries)

    pilot_processes = []
    if os.name != "nt":
        for process_directory in Path("/proc").iterdir():
            if not process_directory.name.isdigit() or int(process_directory.name) == os.getpid():
                continue
            try:
                command = (process_directory / "cmdline").read_bytes()
            except OSError:
                continue
            if b"kuberay_compiled_graph_pilot.py" in command:
                pilot_processes.append(
                    {
                        "pid": int(process_directory.name),
                        "command_sha256": sha256(command).hexdigest(),
                    }
                )
    pilot_processes.sort(key=lambda value: value["pid"])
    return {
        "schema_version": PILOT_SCHEMA_VERSION,
        "status": "success",
        "shared_memory": {
            "total_bytes": stat.f_frsize * stat.f_blocks,
            "available_bytes": stat.f_frsize * stat.f_bavail,
            **shared_memory_entries,
        },
        "pilot_child_process_count": len(pilot_processes),
        "pilot_child_processes": pilot_processes,
        "runtime": {
            "kernel": platform.release(),
            "machine": platform.machine(),
            "source_revision": os.environ.get("DJANGO_RAY_PILOT_SOURCE_REVISION", ""),
            "image_id": os.environ.get("DJANGO_RAY_PILOT_IMAGE_ID", ""),
            "configuration_id": os.environ.get("DJANGO_RAY_PILOT_CONFIG_ID", ""),
        },
    }


def _inspect_cluster_state() -> dict[str, Any]:
    import ray
    from ray._private.internal_api import global_gc
    from ray._private.state import actors
    from ray.util.state import list_objects, list_tasks

    ray.init(address="auto", namespace=PILOT_NAMESPACE, logging_level="ERROR")
    try:
        global_gc()
        time.sleep(1)
        actor_table = actors()
        active_pilot_actors = sorted(
            (
                {
                    "actor_id": actor_id,
                    "name": value.get("Name", ""),
                    "state": value.get("State", ""),
                }
                for actor_id, value in actor_table.items()
                if str(value.get("Name", "")).startswith("cgraph-") and value.get("State") != "DEAD"
            ),
            key=lambda value: (value["name"], value["actor_id"]),
        )
        objects = list_objects(
            detail=True,
            limit=10_000,
            timeout=30,
            raise_on_missing_output=True,
        )
        object_bytes = sum(int(getattr(value, "object_size", 0) or 0) for value in objects)
        object_identities = sorted(
            (
                str(getattr(value, "object_id", "")),
                int(getattr(value, "object_size", 0) or 0),
            )
            for value in objects
        )
        object_identity_digest = sha256(
            json.dumps(object_identities, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        terminal_task_states = {"FINISHED", "FAILED"}
        tasks = list_tasks(
            detail=True,
            limit=10_000,
            timeout=30,
            raise_on_missing_output=True,
        )
        active_pilot_tasks = sorted(
            (
                {
                    "task_id": str(getattr(value, "task_id", "")),
                    "name": str(getattr(value, "name", "")),
                    "state": str(getattr(value, "state", "")),
                }
                for value in tasks
                if str(getattr(value, "name", "")).startswith("cgraph-")
                and str(getattr(value, "state", "")) not in terminal_task_states
            ),
            key=lambda value: (value["name"], value["task_id"]),
        )
        return {
            "schema_version": PILOT_SCHEMA_VERSION,
            "status": "success",
            "active_pilot_actors": active_pilot_actors,
            "active_pilot_actor_count": len(active_pilot_actors),
            "object_count": len(objects),
            "object_bytes": object_bytes,
            "object_identity_digest": object_identity_digest,
            "active_pilot_tasks": active_pilot_tasks,
            "active_pilot_task_count": len(active_pilot_tasks),
            "global_gc_completed": True,
        }
    finally:
        ray.shutdown()


def _verify_cluster_cleanup(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    if before.get("active_pilot_actor_count") != 0:
        raise PilotError("pilot cluster was not clean before native execution")
    if after.get("active_pilot_actor_count") != 0:
        raise PilotError("pilot actors remained active after native execution")
    if before.get("active_pilot_task_count") != 0:
        raise PilotError("pilot cluster had an active pilot task before native execution")
    if after.get("active_pilot_task_count") != 0:
        raise PilotError("pilot tasks remained active after native execution")
    if before.get("object_count") != 0 or before.get("object_bytes") != 0:
        raise PilotError("pilot cluster object store was not empty before native execution")
    if after.get("object_count") != 0 or after.get("object_bytes") != 0:
        raise PilotError("pilot cluster retained object-store results after native execution")
    object_delta = int(after.get("object_count", -1)) - int(before.get("object_count", -1))
    object_bytes_delta = int(after.get("object_bytes", -1)) - int(before.get("object_bytes", -1))
    if (
        object_delta != 0
        or object_bytes_delta != 0
        or after.get("object_identity_digest") != before.get("object_identity_digest")
    ):
        raise PilotError("Ray object-store identity did not return to its exact baseline")
    return {
        "status": "success",
        "active_pilot_actors": 0,
        "active_pilot_tasks": 0,
        "object_count_delta": object_delta,
        "object_bytes_delta": object_bytes_delta,
        "object_identity_restored": True,
        "global_gc_completed": True,
    }


def _execute_native_suite() -> dict[str, Any]:
    import ray

    cases = {
        "normal": _normal_graph_case(ray, PILOT_NAMESPACE),
        "application_exception": _application_exception_case(ray),
        "result_timeout": _result_timeout_case(ray),
    }
    submitted = sum(case["results_submitted"] for case in cases.values())
    consumed = sum(case["results_consumed"] for case in cases.values())
    discarded = sum(case["results_discarded_by_teardown"] for case in cases.values())
    unconsumed = submitted - consumed - discarded
    if unconsumed != 0:
        raise PilotError("native result accounting left an output unconsumed")
    return {
        **cases,
        "teardown_completed": True,
        "result_accounting": {
            "submitted": submitted,
            "consumed": consumed,
            "discarded_by_teardown": discarded,
            "unconsumed": unconsumed,
        },
        "unconsumed_results": unconsumed,
    }


def _nested_probe_owner(owner_task_name: str) -> dict[str, Any]:
    return {
        "owner_pid": os.getpid(),
        "owner_task_name": owner_task_name,
        "owner_max_retries": 0,
        "suite": _execute_native_suite(),
    }


def _run_probe_child(topology: str) -> dict[str, Any]:
    import ray

    ray.init(address="auto", namespace=PILOT_NAMESPACE, logging_level="ERROR")
    try:
        runtime = _sanitized_runtime(ray)
        if topology == CompiledGraphTopology.DIRECT_DRIVER.value:
            payload = {"driver_pid": os.getpid(), "suite": _execute_native_suite()}
        elif topology == CompiledGraphTopology.NESTED_RAY_TASK.value:
            owner_task_name = f"cgraph-owner-{uuid4().hex}"
            owner = ray.remote(num_cpus=0, max_retries=0)(_nested_probe_owner)
            payload = ray.get(
                owner.options(name=owner_task_name).remote(owner_task_name),
                timeout=150,
            )
        else:
            raise PilotError(f"unsupported pilot topology: {topology!r}")
        return {"schema_version": PILOT_SCHEMA_VERSION, "runtime": runtime, "payload": payload}
    finally:
        ray.shutdown()


def _run_probe_child_control_record(topology: str) -> int:
    """Participate in the package probe's bounded private control protocol."""
    try:
        _wait_for_parent_start_gate()
        details = _run_probe_child(topology)
        record: dict[str, Any] = {
            "status": CompiledGraphProbeStatus.SUCCESS.value,
            "details": details,
        }
    except BaseException as error:
        record = {
            "status": CompiledGraphProbeStatus.PYTHON_FAILURE.value,
            "error_type": type(error).__name__,
            "error_message": _tail(str(error), limit=8_192),
            "traceback_tail": _tail(traceback.format_exc(), limit=8_192),
        }
    record_path = os.environ.get(_CHILD_RECORD_PATH_ENV, "").strip()
    if not record_path:
        print("pilot child has no private control-record path", file=sys.stderr)
        return 2
    _write_child_record(Path(record_path), record)
    return 0 if record["status"] == CompiledGraphProbeStatus.SUCCESS.value else 2


def _process_group_is_empty(process: subprocess.Popen[str]) -> bool:
    if process.poll() is None:
        return False
    if os.name == "nt":
        # The promotion pilot is Linux-only. Windows cannot query an exited
        # process group without retaining a Job handle, so it remains a
        # non-blocking development fallback rather than evidence.
        return True
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        if process.poll() is not None:
            return
        process.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return

    if _process_group_is_empty(process):
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        if _process_group_is_empty(process):
            return
        time.sleep(0.05)
    try:
        if not _process_group_is_empty(process):
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=5)
    if not _process_group_is_empty(process):
        raise PilotError("pilot child process group survived forced termination")


def _run_hard_timeout_self_test(timeout_seconds: float) -> dict[str, Any]:
    if not 0.05 <= timeout_seconds <= 5:
        raise PilotError("hard-timeout self-test must be from 0.05 through 5 seconds")
    command = [sys.executable, str(Path(__file__).resolve()), "hang-child"]
    options: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    started = time.monotonic()
    process = subprocess.Popen(command, **options)
    try:
        process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
    else:
        raise PilotError("hard-timeout self-test child exited before its deadline")
    if not _process_group_is_empty(process):
        raise PilotError("hard-timeout self-test left its process group active")
    return {
        "schema_version": PILOT_SCHEMA_VERSION,
        "status": "success",
        "hard_timeout_observed": True,
        "timeout_seconds": timeout_seconds,
        "duration_seconds": round(time.monotonic() - started, 6),
        "child_exit_code": process.returncode,
        "child_process_group_empty": True,
    }


def _expected_policy_identity(
    profile: dict[str, Any],
    *,
    revision: str,
    image_id: str,
    configuration_id: str,
    shared_memory_profile: str,
) -> dict[str, str | None]:
    dependencies = profile["dependency_profile"]
    expected_runtime = profile["runtime_expectations"]
    dependency_profile = ";".join(f"{name}={dependencies[name]}" for name in _PROFILE_DISTRIBUTIONS)
    return {
        "ray_version": profile["ray_version"],
        "python_version": profile["python_version"],
        "operating_system": expected_runtime["operating_system"],
        "architecture": expected_runtime["architecture"],
        "python_implementation": expected_runtime["python_implementation"],
        "python_abi": expected_runtime["python_abi"],
        "dependency_profile": dependency_profile,
        "platform_profile": expected_runtime["platform_profile"],
        "libc_profile": "-".join(expected_runtime["libc"]),
        "container_profile": f"{PILOT_IMAGE_REPOSITORY}:{revision[:12]}@{image_id}",
        "deployment_profile": configuration_id,
        "shared_memory_profile": shared_memory_profile,
        "object_store_profile": (f"plasma:{profile['cluster']['object_store_bytes_per_pod']}"),
    }


def _require_exact_pilot_dependency_profile(profile: dict[str, Any]) -> dict[str, str]:
    expected_dependencies = profile["dependency_profile"]
    if (
        not isinstance(expected_dependencies, dict)
        or expected_dependencies.get("fastrlock") != "0.8.3"
    ):
        raise PilotError("pilot dependency profile must pin fastrlock==0.8.3")
    observed_dependencies: dict[str, str] = {}
    for name in expected_dependencies:
        try:
            observed_dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            observed_dependencies[name] = "absent"
    if observed_dependencies != expected_dependencies:
        raise PilotError(
            "pilot dependency profile changed before native execution: "
            f"expected {expected_dependencies!r}, observed {observed_dependencies!r}"
        )
    return observed_dependencies


def _evaluate_exact_pilot_profile_admission(
    expected_identity: dict[str, str | None],
    observed_identity: dict[str, str | None],
) -> dict[str, Any]:
    """Apply the pilot's exact-profile gate before any native subprocess."""

    if set(observed_identity) != set(expected_identity):
        raise PilotError("exact pilot-profile admission received an incomplete runtime identity")
    changed_dimensions = sorted(
        name for name, expected in expected_identity.items() if observed_identity[name] != expected
    )
    runtime = CompiledGraphRuntimeIdentity(**observed_identity)
    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.DIRECT_DRIVER,
        CompiledGraphTransport.CPU_SHARED_MEMORY,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=runtime,
    )
    admitted = not changed_dimensions
    return {
        "classification": EXACT_PILOT_PROFILE_MATCH if admitted else PILOT_PROFILE_MISMATCH,
        "admitted": admitted,
        "changed_dimensions": changed_dimensions,
        "decision": decision.asdict(),
    }


def _validate_native_observation(
    payload: dict[str, Any],
    decision: Any,
    topology: str,
    *,
    profile: dict[str, Any] | None = None,
    configuration_id: str | None = None,
) -> None:
    expected_profile = _load_profile() if profile is None else profile
    expected_configuration_id = (
        _configuration_identity() if configuration_id is None else configuration_id
    )
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        raise PilotError("native pilot omitted its sanitized runtime record")

    expected_dependencies = expected_profile["dependency_profile"]
    if runtime.get("dependencies") != expected_dependencies:
        raise PilotError(f"native dependency profile changed: {runtime.get('dependencies')!r}")
    expected_runtime = expected_profile["runtime_expectations"]
    if runtime.get("python_version") != expected_profile["python_version"]:
        raise PilotError("native Python version changed")
    if (
        runtime.get("python_implementation", "").strip().casefold()
        != expected_runtime["python_implementation"]
    ):
        raise PilotError("native Python implementation changed")
    if runtime.get("kernel") != expected_runtime["kernel_release"]:
        raise PilotError("native kernel release changed")
    if runtime.get("machine") != expected_runtime["architecture"]:
        raise PilotError("native machine architecture changed")
    if runtime.get("libc") != expected_runtime["libc"]:
        raise PilotError("native libc profile changed")
    if runtime.get("os_release") != expected_runtime["os_release"]:
        raise PilotError("native container operating-system profile changed")
    if not _is_exact_json_integer(
        runtime.get("shared_memory_bytes"),
        expected=expected_profile["cluster"]["shared_memory_bytes_per_pod"],
    ):
        raise PilotError("native /dev/shm capacity changed")
    if not _is_exact_json_integer(runtime.get("alive_ray_nodes"), expected=3):
        raise PilotError("native Ray node count changed")

    cluster_resources = runtime.get("cluster_resources", {})
    cpu_resources = cluster_resources.get("CPU")
    if not _is_nonnegative_finite_number(cpu_resources) or float(cpu_resources) != 2.0:
        raise PilotError("native Ray CPU resources changed")
    object_store_memory = cluster_resources.get("object_store_memory")
    node_count = (
        expected_profile["cluster"]["head"]["replicas"]
        + expected_profile["cluster"]["workers"]["replicas"]
    )
    expected_object_store_memory = (
        expected_profile["cluster"]["object_store_bytes_per_pod"] * node_count
    )
    if (
        isinstance(object_store_memory, bool)
        or not isinstance(object_store_memory, (int, float))
        or not math.isfinite(float(object_store_memory))
        or float(object_store_memory) != float(expected_object_store_memory)
    ):
        raise PilotError(
            "native Ray object-store resource changed: "
            f"expected {expected_object_store_memory}, observed {object_store_memory!r}"
        )

    revision = runtime.get("source_revision", "")
    image_id = runtime.get("image_id", "")
    configuration_id = runtime.get("configuration_id", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise PilotError("native source revision is not immutable")
    if not SHA256_PATTERN.fullmatch(image_id):
        raise PilotError("native image identity is not immutable")
    if configuration_id != expected_configuration_id:
        raise PilotError("native configuration identity changed")
    if runtime.get("kuberay_version") != expected_profile["kuberay"]["operator_version"]:
        raise PilotError("native KubeRay version changed")

    identity = runtime.get("runtime_identity")
    expected_identity = _expected_policy_identity(
        expected_profile,
        revision=revision,
        image_id=image_id,
        configuration_id=configuration_id,
        shared_memory_profile=(
            f"tmpfs:/dev/shm:size={expected_profile['cluster']['shared_memory_bytes_per_pod']}"
        ),
    )
    if identity != expected_identity:
        raise PilotError(f"policy-v2 runtime identity changed: {identity!r}")
    if decision.runtime.asdict() != identity:
        raise PilotError("parent capability decision and child runtime identity differ")
    if (
        decision.topology != topology
        or decision.submission_transport != CompiledGraphSubmissionTransport.DIRECT_RAY_CORE.value
        or decision.transport != CompiledGraphTransport.CPU_SHARED_MEMORY.value
    ):
        raise PilotError("native owner/submission/channel topology changed")


def _sanitized_probe_outcome(outcome: Any) -> dict[str, Any]:
    """Retain structured probe metadata without child output or tracebacks."""

    return {
        "schema_version": outcome.schema_version,
        "status": outcome.status.value,
        "successful": outcome.successful,
        "duration_seconds": round(outcome.duration_seconds, 6),
        "exit_code": outcome.exit_code,
        "termination_signal": outcome.termination_signal,
        "native_exit_code": outcome.native_exit_code,
        "decision": outcome.decision.asdict(),
    }


def _run_probe_parent(topology: str, timeout_seconds: float) -> dict[str, Any]:
    _require_exact_pilot_dependency_profile(_load_profile())
    topology_value = CompiledGraphTopology(topology)
    request = CompiledGraphProbeRequest(
        topology=topology_value,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        transport=CompiledGraphTransport.CPU_SHARED_MEMORY,
        candidate_native=True,
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "probe-child",
        "--topology",
        topology,
    ]
    outcome = run_compiled_graph_probe(
        request,
        timeout_seconds=timeout_seconds,
        _command=command,
    )
    decision = outcome.decision
    if decision.reason is not CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE:
        raise PilotError(
            f"exact pilot profile did not reach the candidate smoke gate: {decision.reason}"
        )
    if not outcome.successful:
        return {
            "schema_version": PILOT_SCHEMA_VERSION,
            "status": outcome.status.value,
            "topology": topology,
            "candidate_native": True,
            "supported_product_execution": False,
            "hardened_probe": _sanitized_probe_outcome(outcome),
        }
    payload = outcome.details
    if not isinstance(payload, dict) or not _is_exact_json_integer(
        payload.get("schema_version"),
        expected=PILOT_SCHEMA_VERSION,
    ):
        raise PilotError("hardened native pilot returned an invalid private record")
    _validate_native_observation(payload, decision, topology)
    suite = payload.get("payload", {}).get("suite", {})
    if (
        suite.get("teardown_completed") is not True
        or not _is_exact_json_integer(suite.get("unconsumed_results"), expected=0)
        or not _is_exact_json_integer(
            payload.get("runtime", {}).get("shared_memory_bytes"),
            expected=536_870_912,
        )
    ):
        raise PilotError("native pilot child did not prove cleanup and profile invariants")
    return {
        "schema_version": PILOT_SCHEMA_VERSION,
        "status": "success",
        "topology": topology,
        "duration_seconds": round(outcome.duration_seconds, 6),
        "decision": decision.asdict(),
        "candidate_native": True,
        "supported_product_execution": False,
        "hardened_subprocess": {
            **_sanitized_probe_outcome(outcome),
            "bounded_private_control_record": True,
            "process_tree_terminated_after_child_exit": True,
        },
        "observation": payload,
    }


def _near_neighbor_guard() -> dict[str, Any]:
    environment_name = "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE"
    profile = _load_profile()
    baseline_value = f"tmpfs:/dev/shm:size={profile['cluster']['shared_memory_bytes_per_pod']}"
    changed_shared_memory_bytes = profile["cluster"]["shared_memory_bytes_per_pod"] // 2
    changed_value = f"tmpfs:/dev/shm:size={changed_shared_memory_bytes}"
    declared_value = os.environ.get(environment_name)
    if declared_value != changed_value:
        raise PilotError(
            "near-neighbor shared-memory identity is not the changed profile: "
            f"expected {changed_value!r}, observed {declared_value!r}"
        )
    stat = os.statvfs("/dev/shm")
    physical_shared_memory_bytes = stat.f_frsize * stat.f_blocks
    if physical_shared_memory_bytes != changed_shared_memory_bytes:
        raise PilotError(
            "near-neighbor physical /dev/shm does not match the changed profile: "
            f"expected {changed_shared_memory_bytes}, observed {physical_shared_memory_bytes}"
        )
    pilot_dependency_profile = _require_exact_pilot_dependency_profile(profile)

    revision = os.environ.get("DJANGO_RAY_PILOT_SOURCE_REVISION", "")
    image_id = os.environ.get("DJANGO_RAY_PILOT_IMAGE_ID", "")
    configuration_id = os.environ.get("DJANGO_RAY_PILOT_CONFIG_ID", "")
    baseline_identity = _expected_policy_identity(
        profile,
        revision=revision,
        image_id=image_id,
        configuration_id=configuration_id,
        shared_memory_profile=baseline_value,
    )
    expected_identity = {**baseline_identity, "shared_memory_profile": changed_value}
    detected_identity = detect_compiled_graph_runtime().asdict()
    if detected_identity != expected_identity:
        raise PilotError("physical near-neighbor changed more than its shared-memory identity")

    baseline_admission = _evaluate_exact_pilot_profile_admission(
        baseline_identity,
        baseline_identity,
    )
    changed_admission = _evaluate_exact_pilot_profile_admission(
        baseline_identity,
        detected_identity,
    )
    if (
        baseline_admission.get("classification") != EXACT_PILOT_PROFILE_MATCH
        or baseline_admission.get("admitted") is not True
        or baseline_admission.get("changed_dimensions") != []
    ):
        raise PilotError(
            "tracked baseline identity was not admitted by the exact pilot-profile gate"
        )
    if (
        changed_admission.get("classification") != PILOT_PROFILE_MISMATCH
        or changed_admission.get("admitted") is not False
        or changed_admission.get("changed_dimensions") != ["shared_memory_profile"]
    ):
        raise PilotError("near-neighbor identity was not rejected for exact profile mismatch")
    return {
        "schema_version": PILOT_SCHEMA_VERSION,
        "status": "success",
        "changed_dimension": "shared_memory_profile",
        "changed_value": changed_value,
        "baseline_value": baseline_value,
        "physical_shared_memory_bytes": physical_shared_memory_bytes,
        "physical_resource_changed": True,
        "pilot_dependency_profile": pilot_dependency_profile,
        "reason": PILOT_PROFILE_MISMATCH,
        "baseline_admission": baseline_admission,
        "changed_admission": changed_admission,
        "child_spawned": False,
        "native_started": False,
    }


def _resolve_blocked_evidence_output(value: str) -> Path:
    investigations = (ROOT / "docs" / "investigations").resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(investigations)
    except ValueError as error:
        raise PilotError("blocked evidence output must stay under docs/investigations") from error
    if not BLOCKED_EVIDENCE_FILENAME_PATTERN.fullmatch(candidate.name):
        raise PilotError(
            "blocked evidence filename must be compiled-graph-kuberay-blocked-YYYY-MM-DD.json"
        )
    if candidate.exists():
        raise PilotError("refusing to overwrite retained Compiled Graph evidence")
    return candidate


def _serialize_retained_evidence(record: dict[str, Any], *, pretty: bool) -> str:
    serialized = json.dumps(
        record,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=True,
    )
    if len(serialized.encode("utf-8")) > MAX_RETAINED_EVIDENCE_BYTES:
        raise PilotError("retained Compiled Graph evidence exceeded its 512 KiB bound")
    return f"{serialized}\n" if pretty else serialized


def _is_nonnegative_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _is_exact_json_integer(value: Any, *, expected: int | None = None) -> bool:
    """Require the JSON integer shape; bool and numerically equal floats fail."""

    return type(value) is int and (expected is None or value == expected)


def _expected_retained_decision(
    profile: dict[str, Any],
    record: dict[str, Any],
    topology: str,
    *,
    shared_memory_bytes: int,
) -> Any:
    identity = CompiledGraphRuntimeIdentity(
        **_expected_policy_identity(
            profile,
            revision=record["source_revision"],
            image_id=record["image_id"],
            configuration_id=record["configuration_id"],
            shared_memory_profile=f"tmpfs:/dev/shm:size={shared_memory_bytes}",
        )
    )
    decision = evaluate_compiled_graph_support(
        topology,
        CompiledGraphTransport.CPU_SHARED_MEMORY,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=identity,
    )
    if (
        decision.reason is not CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE
        or decision.eligible
        or not decision.candidate
        or decision.verified
    ):
        raise PilotError("blocked evidence policy identity no longer reaches the smoke gate")
    return decision


def _validate_retained_probe_outcome(
    value: Any,
    expected_decision: dict[str, Any],
    *,
    expected_status: str,
    successful: bool,
    required_proof: dict[str, bool] | None = None,
) -> None:
    if not isinstance(value, dict):
        raise PilotError("blocked evidence hardened subprocess proof is incomplete")
    expected_keys = {
        "schema_version",
        "status",
        "successful",
        "duration_seconds",
        "exit_code",
        "termination_signal",
        "native_exit_code",
        "decision",
        *(required_proof or {}),
    }
    exit_code = value.get("exit_code")
    exit_code_valid = (
        _is_exact_json_integer(exit_code, expected=0) if successful else exit_code is None
    )
    if (
        set(value) != expected_keys
        or not _is_exact_json_integer(
            value.get("schema_version"),
            expected=PROBE_SCHEMA_VERSION,
        )
        or value.get("status") != expected_status
        or value.get("successful") is not successful
        or not _is_nonnegative_finite_number(value.get("duration_seconds"))
        or not exit_code_valid
        or value.get("termination_signal") is not None
        or value.get("native_exit_code") is not None
        or not _json_exact_equal(value.get("decision"), expected_decision)
        or any(value.get(name) is not expected for name, expected in (required_proof or {}).items())
    ):
        raise PilotError("blocked evidence hardened subprocess proof is inconsistent")


def _validate_dead_actor_observations(value: Any, *, expected_count: int) -> None:
    if not isinstance(value, list) or len(value) != expected_count:
        raise PilotError("blocked evidence native suite teardown proof is incomplete")
    names: set[str] = set()
    actor_ids: set[str] = set()
    for observation in value:
        if (
            not isinstance(observation, dict)
            or set(observation) != {"name", "actor_id", "state"}
            or not isinstance(observation.get("name"), str)
            or not observation["name"].startswith("cgraph-")
            or not isinstance(observation.get("actor_id"), str)
            or re.fullmatch(r"[0-9a-f]+", observation["actor_id"]) is None
            or observation.get("state") != "DEAD"
        ):
            raise PilotError("blocked evidence native suite teardown proof is inconsistent")
        names.add(observation["name"])
        actor_ids.add(observation["actor_id"])
    if len(names) != expected_count or len(actor_ids) != expected_count:
        raise PilotError("blocked evidence native suite teardown identities are duplicated")


def _validate_retained_native_suite(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "normal",
        "application_exception",
        "result_timeout",
        "teardown_completed",
        "result_accounting",
        "unconsumed_results",
    }:
        raise PilotError("blocked evidence native suite proof is incomplete")
    normal = value.get("normal")
    application = value.get("application_exception")
    timeout = value.get("result_timeout")
    if not all(isinstance(case, dict) for case in (normal, application, timeout)):
        raise PilotError("blocked evidence native suite case proof is incomplete")
    assert isinstance(normal, dict)
    assert isinstance(application, dict)
    assert isinstance(timeout, dict)
    expected_normal_keys = {
        "compile_seconds",
        "invocations",
        "ordered_results_consumed",
        "results_submitted",
        "results_consumed",
        "results_discarded_by_teardown",
        "teardown_returned",
        "actor_state_observations",
        "actors_terminated",
        "max_inflight_executions",
        "max_buffered_results",
    }
    expected_application_keys = {
        "error_type",
        "marker_preserved",
        "result_consumed",
        "results_submitted",
        "results_consumed",
        "results_discarded_by_teardown",
        "teardown_returned",
        "actor_state_observations",
        "actors_terminated",
    }
    expected_timeout_keys = {
        "timeout_type",
        "result_consumption_attempted_once",
        "timed_out_result_discarded_by_teardown",
        "results_submitted",
        "results_consumed",
        "results_discarded_by_teardown",
        "teardown_returned",
        "actor_state_observations",
        "actors_terminated",
    }
    if (
        set(normal) != expected_normal_keys
        or not _is_nonnegative_finite_number(normal.get("compile_seconds"))
        or normal.get("ordered_results_consumed") is not True
        or not _is_exact_json_integer(normal.get("max_inflight_executions"), expected=1)
        or not _is_exact_json_integer(normal.get("max_buffered_results"), expected=1)
        or set(application) != expected_application_keys
        or not isinstance(application.get("error_type"), str)
        or not application["error_type"]
        or application.get("marker_preserved") is not True
        or application.get("result_consumed") is not True
        or set(timeout) != expected_timeout_keys
        or not isinstance(timeout.get("timeout_type"), str)
        or "timeout" not in timeout["timeout_type"].casefold()
        or timeout.get("result_consumption_attempted_once") is not True
        or timeout.get("timed_out_result_discarded_by_teardown") is not True
    ):
        raise PilotError("blocked evidence native suite case proof is inconsistent")
    expected_invocations = [
        {
            "index": index,
            "value": index + 2,
            "trace": [
                {"stage": "left", "invocation": index + 1},
                {"stage": "right", "invocation": index + 1},
            ],
        }
        for index in range(3)
    ]
    if not _json_exact_equal(normal.get("invocations"), expected_invocations):
        raise PilotError("blocked evidence repeated-invocation proof is inconsistent")
    expected_counts = ((normal, (3, 3, 0)), (application, (1, 1, 0)), (timeout, (1, 0, 1)))
    count_names = (
        "results_submitted",
        "results_consumed",
        "results_discarded_by_teardown",
    )
    for case, counts in expected_counts:
        if (
            any(
                not _is_exact_json_integer(case.get(name), expected=expected)
                for name, expected in zip(count_names, counts, strict=True)
            )
            or case.get("teardown_returned") is not True
            or case.get("actors_terminated") is not True
        ):
            raise PilotError("blocked evidence native suite teardown or accounting changed")
    _validate_dead_actor_observations(normal.get("actor_state_observations"), expected_count=2)
    _validate_dead_actor_observations(
        application.get("actor_state_observations"),
        expected_count=1,
    )
    _validate_dead_actor_observations(timeout.get("actor_state_observations"), expected_count=1)
    result_accounting = value.get("result_accounting")
    expected_accounting = {
        "submitted": 5,
        "consumed": 4,
        "discarded_by_teardown": 1,
        "unconsumed": 0,
    }
    if (
        value.get("teardown_completed") is not True
        or not _json_exact_equal(result_accounting, expected_accounting)
        or not _is_exact_json_integer(value.get("unconsumed_results"), expected=0)
    ):
        raise PilotError("blocked evidence native suite has incomplete zero-result accounting")


def _validate_retained_topology_outcome(
    outcome: dict[str, Any],
    topology: str,
    profile: dict[str, Any],
    record: dict[str, Any],
) -> None:
    expected_decision = _expected_retained_decision(
        profile,
        record,
        topology,
        shared_memory_bytes=profile["cluster"]["shared_memory_bytes_per_pod"],
    )
    expected_keys = {
        "schema_version",
        "status",
        "topology",
        "duration_seconds",
        "decision",
        "candidate_native",
        "supported_product_execution",
        "hardened_subprocess",
        "observation",
    }
    if (
        set(outcome) != expected_keys
        or not _is_exact_json_integer(
            outcome.get("schema_version"),
            expected=PILOT_SCHEMA_VERSION,
        )
        or outcome.get("status") != "success"
        or outcome.get("topology") != topology
        or not _is_nonnegative_finite_number(outcome.get("duration_seconds"))
        or not _json_exact_equal(outcome.get("decision"), expected_decision.asdict())
        or outcome.get("candidate_native") is not True
        or outcome.get("supported_product_execution") is not False
    ):
        raise PilotError("blocked evidence topology identity or policy proof is inconsistent")
    hardened = outcome.get("hardened_subprocess")
    _validate_retained_probe_outcome(
        hardened,
        expected_decision.asdict(),
        expected_status=CompiledGraphProbeStatus.SUCCESS.value,
        successful=True,
        required_proof={
            "bounded_private_control_record": True,
            "process_tree_terminated_after_child_exit": True,
        },
    )
    assert isinstance(hardened, dict)
    if hardened.get("duration_seconds") != outcome.get("duration_seconds"):
        raise PilotError("blocked evidence topology and subprocess durations differ")
    observation = outcome.get("observation")
    if (
        not isinstance(observation, dict)
        or set(observation) != {"schema_version", "runtime", "payload"}
        or not _is_exact_json_integer(
            observation.get("schema_version"),
            expected=PILOT_SCHEMA_VERSION,
        )
    ):
        raise PilotError("blocked evidence native observation is incomplete")
    _validate_native_observation(
        observation,
        expected_decision,
        topology,
        profile=profile,
        configuration_id=record["configuration_id"],
    )
    runtime = observation.get("runtime")
    _require_exact_keys(
        runtime,
        {
            "runtime_identity",
            "python_version",
            "python_implementation",
            "kernel",
            "machine",
            "libc",
            "os_release",
            "dependencies",
            "shared_memory_bytes",
            "alive_ray_nodes",
            "cluster_resources",
            "source_revision",
            "image_id",
            "configuration_id",
            "kuberay_version",
        },
        "native runtime",
    )
    assert isinstance(runtime, dict)
    cluster_resources = runtime.get("cluster_resources")
    if (
        not isinstance(cluster_resources, dict)
        or not {"CPU", "object_store_memory"}.issubset(cluster_resources)
        or not set(cluster_resources).issubset({"CPU", "memory", "object_store_memory"})
        or any(not _is_nonnegative_finite_number(value) for value in cluster_resources.values())
    ):
        raise PilotError("blocked evidence native cluster resources are not allowlisted")
    if (
        runtime.get("source_revision") != record["source_revision"]
        or runtime.get("image_id") != record["image_id"]
        or runtime.get("configuration_id") != record["configuration_id"]
        or not _json_exact_equal(
            runtime.get("runtime_identity"), expected_decision.runtime.asdict()
        )
    ):
        raise PilotError("blocked evidence native runtime identity is inconsistent")
    payload = observation.get("payload")
    if not isinstance(payload, dict):
        raise PilotError("blocked evidence native topology payload is incomplete")
    if topology == CompiledGraphTopology.DIRECT_DRIVER.value:
        if (
            set(payload) != {"driver_pid", "suite"}
            or isinstance(payload.get("driver_pid"), bool)
            or not isinstance(payload.get("driver_pid"), int)
            or payload["driver_pid"] <= 0
        ):
            raise PilotError("blocked evidence direct-driver ownership proof is inconsistent")
    elif (
        set(payload) != {"owner_pid", "owner_task_name", "owner_max_retries", "suite"}
        or isinstance(payload.get("owner_pid"), bool)
        or not isinstance(payload.get("owner_pid"), int)
        or payload["owner_pid"] <= 0
        or not isinstance(payload.get("owner_task_name"), str)
        or re.fullmatch(r"cgraph-owner-[0-9a-f]{32}", payload["owner_task_name"]) is None
        or not _is_exact_json_integer(payload.get("owner_max_retries"), expected=0)
    ):
        raise PilotError("blocked evidence nested-owner proof is inconsistent")
    _validate_retained_native_suite(payload.get("suite"))


def _validate_retained_near_neighbor(
    value: Any,
    profile: dict[str, Any],
    record: dict[str, Any],
) -> None:
    if not isinstance(value, dict):
        raise PilotError("blocked evidence near-neighbor proof is incomplete")
    expected_keys = {
        "schema_version",
        "status",
        "changed_dimension",
        "changed_value",
        "baseline_value",
        "physical_shared_memory_bytes",
        "physical_resource_changed",
        "pilot_dependency_profile",
        "reason",
        "baseline_admission",
        "changed_admission",
        "child_spawned",
        "native_started",
    }
    baseline_bytes = profile["cluster"]["shared_memory_bytes_per_pod"]
    changed_bytes = baseline_bytes // 2
    baseline_identity = _expected_policy_identity(
        profile,
        revision=record["source_revision"],
        image_id=record["image_id"],
        configuration_id=record["configuration_id"],
        shared_memory_profile=f"tmpfs:/dev/shm:size={baseline_bytes}",
    )
    changed_identity = {
        **baseline_identity,
        "shared_memory_profile": f"tmpfs:/dev/shm:size={changed_bytes}",
    }
    expected_baseline_admission = _evaluate_exact_pilot_profile_admission(
        baseline_identity,
        baseline_identity,
    )
    expected_changed_admission = _evaluate_exact_pilot_profile_admission(
        baseline_identity,
        changed_identity,
    )
    if (
        set(value) != expected_keys
        or not _is_exact_json_integer(
            value.get("schema_version"),
            expected=PILOT_SCHEMA_VERSION,
        )
        or value.get("status") != "success"
        or value.get("changed_dimension") != "shared_memory_profile"
        or value.get("changed_value") != f"tmpfs:/dev/shm:size={changed_bytes}"
        or value.get("baseline_value") != f"tmpfs:/dev/shm:size={baseline_bytes}"
        or not _is_exact_json_integer(
            value.get("physical_shared_memory_bytes"),
            expected=changed_bytes,
        )
        or value.get("physical_resource_changed") is not True
        or not _json_exact_equal(
            value.get("pilot_dependency_profile"), profile["dependency_profile"]
        )
        or value.get("reason") != PILOT_PROFILE_MISMATCH
        or not _json_exact_equal(value.get("baseline_admission"), expected_baseline_admission)
        or not _json_exact_equal(value.get("changed_admission"), expected_changed_admission)
        or value.get("child_spawned") is not False
        or value.get("native_started") is not False
    ):
        raise PilotError("blocked evidence near-neighbor proof is inconsistent")


def _validate_retained_hard_timeout(value: Any, profile: dict[str, Any]) -> None:
    expected_timeout = profile["probe"]["hard_timeout_self_test_seconds"]
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "status",
            "hard_timeout_observed",
            "timeout_seconds",
            "duration_seconds",
            "child_exit_code",
            "child_process_group_empty",
        }
        or not _is_exact_json_integer(
            value.get("schema_version"),
            expected=PILOT_SCHEMA_VERSION,
        )
        or value.get("status") != "success"
        or value.get("hard_timeout_observed") is not True
        or value.get("timeout_seconds") != expected_timeout
        or not _is_nonnegative_finite_number(value.get("duration_seconds"))
        or value["duration_seconds"] < expected_timeout
        or not _is_exact_json_integer(value.get("child_exit_code"))
        or value["child_exit_code"] >= 0
        or value.get("child_process_group_empty") is not True
    ):
        raise PilotError("blocked evidence hard-timeout containment proof is inconsistent")


def _require_exact_keys(value: Any, expected: set[str] | frozenset[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise PilotError(f"blocked evidence {label} schema is not exact")


def _parse_retained_utc_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("+00:00") or len(value) > 64:
        raise PilotError(f"blocked evidence {label} is not an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PilotError(f"blocked evidence {label} is not a valid timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PilotError(f"blocked evidence {label} is not UTC")
    return parsed


def _validate_retained_host_context(record: dict[str, Any], profile: dict[str, Any]) -> None:
    if (
        record.get("kubernetes_context") != profile["kubernetes"]["context"]
        or record.get("namespace") != profile["cluster"]["namespace"]
        or record.get("namespace") != PILOT_NAMESPACE
    ):
        raise PilotError("blocked evidence Kubernetes context or namespace changed")

    docker = record.get("docker")
    _require_exact_keys(
        docker,
        {
            "context",
            "endpoint",
            "engine",
            "build_context_policy",
            "build_context_policy_id",
        },
        "Docker context",
    )
    assert isinstance(docker, dict)
    _require_exact_keys(
        docker["engine"],
        {"version", "operating_system", "architecture", "kernel_version"},
        "Docker engine",
    )
    expected_build_policy_id = _build_context_policy_identity()
    if (
        docker["context"] != profile["docker"]["context"]
        or docker["endpoint"] != profile["docker"]["endpoint"]
        or not _json_exact_equal(docker["engine"], profile["docker"]["engine"])
        or docker["build_context_policy"] != "dockerfile-specific-deny-by-default"
        or docker["build_context_policy_id"] != expected_build_policy_id
    ):
        raise PilotError("blocked evidence Docker context is inconsistent")

    operator = record.get("kuberay_operator")
    _require_exact_keys(
        operator,
        {
            "version",
            "image",
            "image_id",
            "deployment_name",
            "deployment_uid",
            "replica_set_name",
            "replica_set_uid",
            "pod_name",
            "pod_uid",
            "container_name",
            "container_id",
            "restart_count",
            "ready",
            "pod_phase",
            "controller_chain_verified",
            "container_inventory_verified",
        },
        "KubeRay operator",
    )
    assert isinstance(operator, dict)
    operator_version = profile["kuberay"]["operator_version"]
    expected_operator_image = f"quay.io/kuberay/operator:v{operator_version}"
    expected_operator_id = profile["kuberay"]["operator_image"].split("@", 1)[-1]
    deployment_name = profile["kuberay"]["operator_deployment_name"]
    replica_set_name = operator.get("replica_set_name")
    pod_name = operator.get("pod_name")
    restart_count = operator.get("restart_count")
    if (
        operator.get("version") != operator_version
        or operator.get("image") != expected_operator_image
        or operator.get("image_id") != expected_operator_id
        or operator.get("deployment_name") != deployment_name
        or operator.get("container_name") != profile["kuberay"]["operator_container_name"]
        or KUBERNETES_UID_PATTERN.fullmatch(str(operator.get("deployment_uid", ""))) is None
        or not isinstance(replica_set_name, str)
        or re.fullmatch(rf"{re.escape(deployment_name)}-[a-z0-9]{{1,63}}", replica_set_name) is None
        or KUBERNETES_UID_PATTERN.fullmatch(str(operator.get("replica_set_uid", ""))) is None
        or not isinstance(pod_name, str)
        or re.fullmatch(rf"{re.escape(replica_set_name)}-[a-z0-9]{{5}}", pod_name) is None
        or KUBERNETES_UID_PATTERN.fullmatch(str(operator.get("pod_uid", ""))) is None
        or not CONTAINER_ID_PATTERN.fullmatch(str(operator.get("container_id", "")))
        or not _is_exact_json_integer(restart_count)
        or restart_count < 0
        or operator.get("ready") is not True
        or operator.get("pod_phase") != "Running"
        or operator.get("controller_chain_verified") is not True
        or operator.get("container_inventory_verified") is not True
    ):
        raise PilotError("blocked evidence KubeRay operator identity is inconsistent")

    kubernetes = record.get("kubernetes")
    _require_exact_keys(
        kubernetes,
        {"server_version", "node", "node_selector"},
        "Kubernetes target",
    )
    assert isinstance(kubernetes, dict)
    node = kubernetes["node"]
    _require_exact_keys(
        node,
        {
            "name",
            "operating_system",
            "architecture",
            "kernel_version",
            "container_runtime_version",
            "capacity",
            "allocatable",
        },
        "Kubernetes node",
    )
    assert isinstance(node, dict)
    expected_node = profile["kubernetes"]["node"]
    if (
        kubernetes["server_version"] != profile["kubernetes"]["server_version"]
        or not _json_exact_equal({name: node[name] for name in expected_node}, expected_node)
        or not _json_exact_equal(
            kubernetes["node_selector"], profile["kubernetes"]["node_selector"]
        )
    ):
        raise PilotError("blocked evidence Kubernetes node identity is inconsistent")
    for resource_name in ("capacity", "allocatable"):
        resources = node[resource_name]
        if (
            not isinstance(resources, dict)
            or not resources
            or not set(resources).issubset(KUBERNETES_NODE_RESOURCE_KEYS)
            or any(
                not isinstance(value, str) or not value or len(value) > 64
                for value in resources.values()
            )
        ):
            raise PilotError(f"blocked evidence Kubernetes node {resource_name} is not allowlisted")
    if set(node["capacity"]) != set(node["allocatable"]):
        raise PilotError("blocked evidence Kubernetes capacity and allocatable keys differ")


def _validate_retained_namespace_lease(
    value: Any,
    record: dict[str, Any],
) -> NamespaceLease:
    _require_exact_keys(
        value,
        {"name", "uid", "run_token", "profile_name"},
        "namespace lease",
    )
    assert isinstance(value, dict)
    if not all(isinstance(value.get(name), str) for name in value):
        raise PilotError("blocked evidence namespace lease has invalid value types")
    lease = NamespaceLease(
        name=value["name"],
        uid=value["uid"],
        run_token=value["run_token"],
    )
    try:
        _validate_namespace_lease(lease)
    except PilotError as error:
        raise PilotError("blocked evidence namespace lease identity is invalid") from error
    if value.get("profile_name") != PROFILE_NAME or lease.name != record.get("namespace"):
        raise PilotError("blocked evidence namespace lease identity is inconsistent")
    return lease


def _validate_retained_raycluster_lease(
    value: Any,
    namespace_lease: NamespaceLease,
) -> RayClusterLease:
    _require_exact_keys(
        value,
        {"name", "uid", "namespace_uid", "run_token", "profile_name"},
        "RayCluster lease",
    )
    assert isinstance(value, dict)
    if not all(isinstance(value.get(name), str) for name in value):
        raise PilotError("blocked evidence RayCluster lease has invalid value types")
    lease = RayClusterLease(
        name=value["name"],
        uid=value["uid"],
        namespace_uid=value["namespace_uid"],
        run_token=value["run_token"],
    )
    try:
        _validate_raycluster_lease(lease, namespace_lease)
    except PilotError as error:
        raise PilotError("blocked evidence RayCluster lease identity is invalid") from error
    if value.get("profile_name") != PROFILE_NAME:
        raise PilotError("blocked evidence RayCluster lease profile is inconsistent")
    return lease


def _validate_retained_init_containers(
    value: Any,
    *,
    expected_names: Any,
    record: dict[str, Any],
) -> None:
    if not isinstance(expected_names, list) or any(
        not isinstance(name, str) or not name for name in expected_names
    ):
        raise PilotError("blocked evidence profile init-container inventory is invalid")
    if not isinstance(value, list) or len(value) != len(expected_names):
        raise PilotError("blocked evidence pod init-container inventory is incomplete")
    if [item.get("name") for item in value if isinstance(item, dict)] != expected_names:
        raise PilotError("blocked evidence pod init-container inventory changed")
    for item in value:
        _require_exact_keys(
            item,
            {
                "name",
                "container_id",
                "image",
                "image_id",
                "restart_count",
                "ready",
                "state",
                "exit_code",
                "reason",
            },
            "pod init container",
        )
        assert isinstance(item, dict)
        if (
            not CONTAINER_ID_PATTERN.fullmatch(str(item.get("container_id", "")))
            or item.get("image") != record["image"]
            or item.get("image_id") != record["image_id"]
            or not _is_exact_json_integer(item.get("restart_count"), expected=0)
            or item.get("ready") is not True
            or item.get("state") != "terminated"
            or not _is_exact_json_integer(item.get("exit_code"), expected=0)
            or item.get("reason") != "Completed"
        ):
            raise PilotError("blocked evidence pod init-container identity is inconsistent")


def _validate_retained_ray_start_parameters(
    value: Any,
    expected: Any,
) -> None:
    """Revalidate exact sanitized Ray CLI observations at the write boundary."""

    if not isinstance(value, dict) or not isinstance(expected, dict) or set(value) != set(expected):
        raise PilotError("blocked evidence pod Ray start parameters are inconsistent")
    for option, raw_spec in expected.items():
        try:
            parameter_spec = _validate_ray_start_parameter_spec(option, raw_spec)
        except PilotError as error:
            raise PilotError("blocked evidence profile Ray start parameters are invalid") from error
        if not _ray_start_observation_matches_spec(value.get(option), parameter_spec):
            raise PilotError("blocked evidence pod Ray start parameters are inconsistent")


def _validate_retained_pod(
    pod: Any,
    profile: dict[str, Any],
    record: dict[str, Any],
    namespace_lease: NamespaceLease,
    raycluster_lease: RayClusterLease,
) -> None:
    _require_exact_keys(
        pod,
        {
            "name",
            "uid",
            "namespace_uid",
            "run_token",
            "raycluster_uid",
            "owner_reference_verified",
            "role",
            "node",
            "container_name",
            "container_id",
            "image",
            "image_id",
            "configuration_id",
            "restart_count",
            "phase",
            "ready",
            "container_state",
            "deletion_timestamp",
            "identity_environment",
            "init_containers",
            "resources",
            "node_selector",
            "shared_memory_volume",
            "ray_start_parameters",
        },
        "pod",
    )
    assert isinstance(pod, dict)
    role = pod.get("role")
    if role not in {"head", "worker"}:
        raise PilotError("blocked evidence pod role is invalid")
    role_profile = profile["cluster"]["head" if role == "head" else "workers"]
    expected_resources = {
        "requests": {
            "cpu": role_profile["cpu_request"],
            "memory": role_profile["memory_request"],
        },
        "limits": {
            "cpu": role_profile["cpu_limit"],
            "memory": role_profile["memory_limit"],
        },
    }
    expected_object_store = profile["cluster"]["object_store_bytes_per_pod"]
    expected_identity_environment = {
        "DJANGO_RAY_COMPILED_GRAPH_CONTAINER_PROFILE": (f"{record['image']}@{record['image_id']}"),
        "DJANGO_RAY_COMPILED_GRAPH_DEPLOYMENT_PROFILE": record["configuration_id"],
        "DJANGO_RAY_COMPILED_GRAPH_SHARED_MEMORY_PROFILE": (
            f"tmpfs:/dev/shm:size={profile['cluster']['shared_memory_bytes_per_pod']}"
        ),
        "DJANGO_RAY_COMPILED_GRAPH_OBJECT_STORE_PROFILE": f"plasma:{expected_object_store}",
        "DJANGO_RAY_PILOT_IMAGE_ID": record["image_id"],
        "DJANGO_RAY_PILOT_CONFIG_ID": record["configuration_id"],
        "DJANGO_RAY_PILOT_KUBERAY_VERSION": profile["kuberay"]["operator_version"],
        "DJANGO_RAY_PILOT_NAMESPACE_UID": namespace_lease.uid,
        "DJANGO_RAY_PILOT_RUN_TOKEN": namespace_lease.run_token,
    }
    if (
        not isinstance(pod.get("name"), str)
        or not pod["name"]
        or len(pod["name"]) > 253
        or not isinstance(pod.get("uid"), str)
        or not pod["uid"]
        or len(pod["uid"]) > 128
        or pod.get("namespace_uid") != namespace_lease.uid
        or pod.get("run_token") != namespace_lease.run_token
        or pod.get("raycluster_uid") != raycluster_lease.uid
        or pod.get("owner_reference_verified") is not True
        or pod.get("node") != profile["kubernetes"]["node"]["name"]
        or pod.get("container_name") != f"ray-{role}"
        or not CONTAINER_ID_PATTERN.fullmatch(str(pod.get("container_id", "")))
        or pod.get("image") != record["image"]
        or pod.get("image_id") != record["image_id"]
        or pod.get("configuration_id") != record["configuration_id"]
        or isinstance(pod.get("restart_count"), bool)
        or not isinstance(pod.get("restart_count"), int)
        or pod.get("restart_count") != 0
        or pod.get("phase") != "Running"
        or pod.get("ready") is not True
        or pod.get("container_state") != "running"
        or pod.get("deletion_timestamp") is not None
        or not _json_exact_equal(pod.get("identity_environment"), expected_identity_environment)
        or not _json_exact_equal(pod.get("resources"), expected_resources)
        or not _json_exact_equal(pod.get("node_selector"), profile["kubernetes"]["node_selector"])
        or not _json_exact_equal(
            pod.get("shared_memory_volume"), {"medium": "Memory", "sizeLimit": "512Mi"}
        )
    ):
        raise PilotError("blocked evidence pod resources or execution identity are inconsistent")
    _validate_retained_ray_start_parameters(
        pod.get("ray_start_parameters"),
        role_profile["ray_start_parameters"],
    )
    _validate_retained_init_containers(
        pod.get("init_containers"),
        expected_names=role_profile["init_containers"],
        record=record,
    )


def _validate_retained_shared_memory(value: Any, profile: dict[str, Any]) -> None:
    _require_exact_keys(
        value,
        {
            "total_bytes",
            "available_bytes",
            "entry_count",
            "entry_bytes",
            "entry_identity_digest",
            "ray_mutable_object_semaphores",
        },
        "shared-memory snapshot",
    )
    assert isinstance(value, dict)
    for count_name in ("total_bytes", "available_bytes", "entry_count", "entry_bytes"):
        count = value[count_name]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PilotError("blocked evidence shared-memory counts are invalid")
    if (
        value["total_bytes"] != profile["cluster"]["shared_memory_bytes_per_pod"]
        or value["available_bytes"] > value["total_bytes"]
        or re.fullmatch(r"[0-9a-f]{64}", str(value["entry_identity_digest"])) is None
    ):
        raise PilotError("blocked evidence shared-memory capacity or identity is inconsistent")
    semaphores = value["ray_mutable_object_semaphores"]
    _require_exact_keys(
        semaphores,
        {
            "header_count",
            "object_count",
            "pair_count",
            "paired_entry_count",
            "unpaired_entry_count",
            "other_entry_count",
            "fully_paired_and_exclusive",
            "pair_identity_digest",
            "semaphore_identity_digest",
            "unpaired_identity_digest",
            "other_entry_identity_digest",
        },
        "Ray mutable-object semaphore summary",
    )
    assert isinstance(semaphores, dict)
    for count_name in (
        "header_count",
        "object_count",
        "pair_count",
        "paired_entry_count",
        "unpaired_entry_count",
        "other_entry_count",
    ):
        count = semaphores[count_name]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PilotError("blocked evidence semaphore counts are invalid")
    for digest_name in (
        "pair_identity_digest",
        "semaphore_identity_digest",
        "unpaired_identity_digest",
        "other_entry_identity_digest",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", str(semaphores[digest_name])) is None:
            raise PilotError("blocked evidence semaphore identity is invalid")
    if not isinstance(semaphores["fully_paired_and_exclusive"], bool):
        raise PilotError("blocked evidence semaphore exclusivity is invalid")
    expected_fully_paired = (
        semaphores["pair_count"] > 0
        and semaphores["header_count"] == semaphores["pair_count"]
        and semaphores["object_count"] == semaphores["pair_count"]
        and semaphores["unpaired_entry_count"] == 0
        and semaphores["other_entry_count"] == 0
        and semaphores["paired_entry_count"] == value["entry_count"]
    )
    if (
        semaphores["paired_entry_count"] != semaphores["pair_count"] * 2
        or semaphores["header_count"] + semaphores["object_count"]
        != semaphores["paired_entry_count"] + semaphores["unpaired_entry_count"]
        or value["entry_count"]
        != semaphores["paired_entry_count"]
        + semaphores["unpaired_entry_count"]
        + semaphores["other_entry_count"]
        or semaphores["fully_paired_and_exclusive"] is not expected_fully_paired
    ):
        raise PilotError("blocked evidence semaphore accounting is inconsistent")


def _validate_retained_runtime_snapshot(
    value: Any,
    profile: dict[str, Any],
    record: dict[str, Any],
) -> None:
    _require_exact_keys(
        value,
        {
            "schema_version",
            "status",
            "shared_memory",
            "pilot_child_process_count",
            "pilot_child_processes",
            "runtime",
        },
        "pod runtime snapshot",
    )
    assert isinstance(value, dict)
    _validate_retained_shared_memory(value["shared_memory"], profile)
    runtime = value["runtime"]
    _require_exact_keys(
        runtime,
        {"kernel", "machine", "source_revision", "image_id", "configuration_id"},
        "pod runtime identity",
    )
    expected_runtime = profile["runtime_expectations"]
    if (
        not _is_exact_json_integer(value["schema_version"], expected=PILOT_SCHEMA_VERSION)
        or value["status"] != "success"
        or isinstance(value["pilot_child_process_count"], bool)
        or not isinstance(value["pilot_child_process_count"], int)
        or value["pilot_child_process_count"] != 0
        or value["pilot_child_processes"] != []
        or runtime["kernel"] != expected_runtime["kernel_release"]
        or runtime["machine"] != expected_runtime["architecture"]
        or runtime["source_revision"] != record["source_revision"]
        or runtime["image_id"] != record["image_id"]
        or runtime["configuration_id"] != record["configuration_id"]
    ):
        raise PilotError("blocked evidence pod runtime identity is inconsistent")


def _validate_retained_cluster_state(value: Any) -> None:
    _require_exact_keys(
        value,
        {
            "schema_version",
            "status",
            "active_pilot_actors",
            "active_pilot_actor_count",
            "object_count",
            "object_bytes",
            "object_identity_digest",
            "active_pilot_tasks",
            "active_pilot_task_count",
            "global_gc_completed",
        },
        "cluster state",
    )
    assert isinstance(value, dict)
    zero_count_names = (
        "active_pilot_actor_count",
        "active_pilot_task_count",
        "object_count",
        "object_bytes",
    )
    if (
        not _is_exact_json_integer(value["schema_version"], expected=PILOT_SCHEMA_VERSION)
        or value["status"] != "success"
        or any(
            isinstance(value[name], bool) or not isinstance(value[name], int)
            for name in zero_count_names
        )
        or value["active_pilot_actors"] != []
        or value["active_pilot_actor_count"] != 0
        or value["active_pilot_tasks"] != []
        or value["active_pilot_task_count"] != 0
        or value["object_count"] != 0
        or value["object_bytes"] != 0
        or value["object_identity_digest"] != sha256(b"[]").hexdigest()
        or value["global_gc_completed"] is not True
    ):
        raise PilotError("blocked evidence cluster zero-state details are inconsistent")


def _validate_blocked_evidence_record(
    record: dict[str, Any],
    *,
    require_namespace_deleted: bool,
) -> None:
    _require_exact_keys(record, BLOCKED_EVIDENCE_ROOT_KEYS, "root")
    if (
        isinstance(record.get("schema_version"), bool)
        or not isinstance(record.get("schema_version"), int)
        or record.get("schema_version") != PILOT_SCHEMA_VERSION
        or record.get("status") != "blocked"
    ):
        raise PilotError("retained evidence is not a blocked pilot schema")
    if not re.fullmatch(r"[0-9a-f]{40}", str(record.get("source_revision", ""))):
        raise PilotError("blocked evidence requires an exact source revision")
    for identity_name in (
        "image_id",
        "configuration_id",
        "profile_id",
        "rendered_manifest_id",
    ):
        if not SHA256_PATTERN.fullmatch(str(record.get(identity_name, ""))):
            raise PilotError(f"blocked evidence requires an exact {identity_name}")
    profile = record.get("profile")
    if (
        not isinstance(profile, dict)
        or record.get("profile_name") != PROFILE_NAME
        or profile.get("profile_name") != PROFILE_NAME
        or _profile_identity(profile) != record.get("profile_id")
        or not _json_exact_equal(
            profile.get("probe", {}).get("cleanup_retry_delays_seconds"),
            list(CLEANUP_RETRY_DELAYS_SECONDS),
        )
    ):
        raise PilotError("blocked evidence profile identity is inconsistent")
    started_at = _parse_retained_utc_timestamp(record.get("started_at"), "started_at")
    completed_at = _parse_retained_utc_timestamp(record.get("completed_at"), "completed_at")
    if completed_at < started_at:
        raise PilotError("blocked evidence completion timestamp precedes its start")
    expected_evidence_id = f"local-kuberay:{record['source_revision']}:{record['image_id']}"
    if (
        record.get("evidence_id") != expected_evidence_id
        or record.get("image") != f"{PILOT_IMAGE_REPOSITORY}:{record['source_revision'][:12]}"
    ):
        raise PilotError("blocked evidence source and image identity are inconsistent")
    _validate_retained_host_context(record, profile)
    retained_namespace_lease = _validate_retained_namespace_lease(
        record.get("namespace_lease"),
        record,
    )
    retained_raycluster_lease = _validate_retained_raycluster_lease(
        record.get("raycluster_lease"),
        retained_namespace_lease,
    )
    if (
        record.get("candidate_native") is not True
        or record.get("promotion_eligible") is not False
        or record.get("supported_product_execution") is not False
        or record.get("pilot_evidence_passed") is not False
    ):
        raise PilotError("blocked evidence has an invalid pilot or promotion claim")

    topologies = record.get("topologies")
    expected_topologies = (
        CompiledGraphTopology.DIRECT_DRIVER.value,
        CompiledGraphTopology.NESTED_RAY_TASK.value,
    )
    if (
        not isinstance(topologies, list)
        or len(topologies) != 2
        or any(not isinstance(outcome, dict) for outcome in topologies)
        or tuple(outcome.get("topology") for outcome in topologies) != expected_topologies
    ):
        raise PilotError("blocked evidence requires both successful topology outcomes")
    for outcome, topology in zip(topologies, expected_topologies, strict=True):
        _validate_retained_topology_outcome(outcome, topology, profile, record)
    _validate_retained_near_neighbor(record.get("near_neighbor"), profile, record)
    _validate_retained_hard_timeout(record.get("hard_timeout"), profile)

    failure = record.get("failure")
    _require_exact_keys(
        failure,
        {"classification", "invariant", "summary", "tracker_urls"},
        "failure",
    )
    if (
        not isinstance(failure, dict)
        or failure.get("classification") != MUTABLE_OBJECT_CLEANUP_CLASSIFICATION
        or failure.get("invariant") != "exact_shared_memory_restoration_after_graph_teardown"
        or failure.get("summary")
        != "Compiled Graph probes completed, but runtime cleanup did not restore the pinned pod state."
        or failure.get("tracker_urls") != list(BLOCKER_TRACKERS)
    ):
        raise PilotError("blocked evidence does not identify the known upstream cleanup blocker")

    pods = record.get("pods")
    _require_exact_keys(
        pods,
        {
            "before",
            "after",
            "identity",
            "final_capture_before",
            "final_capture_after",
            "final_capture_identity",
            "runtime_before",
            "runtime_after",
        },
        "pod evidence",
    )
    assert isinstance(pods, dict)
    final_capture_before = pods.get("final_capture_before")
    final_capture_after = pods.get("final_capture_after")
    if not isinstance(final_capture_before, list) or not isinstance(final_capture_after, list):
        raise PilotError("blocked evidence has no final pod capture bracket")
    pod_identity = _verify_pod_execution_identity_unchanged(
        pods.get("before", []),
        pods.get("after", []),
    )
    if not _json_exact_equal(pods.get("identity"), pod_identity):
        raise PilotError("blocked evidence pod identity proof is inconsistent")
    _verify_pod_execution_identity_unchanged(pods["before"], final_capture_before)
    final_capture_identity = _verify_pod_execution_identity_unchanged(
        final_capture_before,
        final_capture_after,
    )
    if not _json_exact_equal(pods.get("after"), final_capture_after) or not _json_exact_equal(
        pods.get("final_capture_identity"), final_capture_identity
    ):
        raise PilotError("blocked evidence final pod capture bracket is inconsistent")
    for pod_group in (
        pods["before"],
        pods["after"],
        final_capture_before,
        final_capture_after,
    ):
        if (
            [pod.get("role") for pod in pod_group].count("head") != 1
            or [pod.get("role") for pod in pod_group].count("worker") != 2
            or len({pod.get("name") for pod in pod_group}) != 3
            or len({pod.get("uid") for pod in pod_group}) != 3
            or len({pod.get("container_id") for pod in pod_group}) != 3
        ):
            raise PilotError("blocked evidence pod identities are incomplete or duplicated")
        for pod in pod_group:
            _validate_retained_pod(
                pod,
                profile,
                record,
                retained_namespace_lease,
                retained_raycluster_lease,
            )
        all_container_ids = [
            container_id
            for pod in pod_group
            for container_id in (
                pod["container_id"],
                *(item["container_id"] for item in pod["init_containers"]),
            )
        ]
        if len(set(all_container_ids)) != len(all_container_ids):
            raise PilotError("blocked evidence pod container identities are duplicated")

    cleanup = record.get("cleanup")
    _require_exact_keys(
        cleanup,
        {
            "compiled_graph_teardown_verified",
            "shared_memory",
            "shared_memory_observations",
            "cluster_state_before",
            "cluster_state_after",
            "cluster_state",
            "pilot_namespace_deleted",
            "unrelated_namespaces_touched",
        },
        "cleanup",
    )
    assert isinstance(cleanup, dict)
    if cleanup.get("compiled_graph_teardown_verified") is not False:
        raise PilotError("blocked evidence cannot claim verified Compiled Graph teardown")
    if not isinstance(cleanup.get("pilot_namespace_deleted"), bool):
        raise PilotError("blocked evidence namespace deletion state is invalid")
    if require_namespace_deleted and cleanup.get("pilot_namespace_deleted") is not True:
        raise PilotError("blocked evidence persistence requires verified namespace deletion")
    if cleanup.get("unrelated_namespaces_touched") != []:
        raise PilotError("blocked evidence touched an unrelated namespace")
    observations = cleanup.get("shared_memory_observations")
    runtime_before = pods.get("runtime_before")
    runtime_after = pods.get("runtime_after")
    if (
        not isinstance(observations, list)
        or not isinstance(runtime_before, dict)
        or not isinstance(runtime_after, dict)
        or not observations
        or not _json_exact_equal(observations[-1].get("pods"), runtime_after)
        or observations[-1].get("phase") != "final_capture_bracket_verified"
        or set(runtime_before) != {pod["name"] for pod in pods["before"]}
        or set(runtime_after) != {pod["name"] for pod in pods["after"]}
    ):
        raise PilotError("blocked evidence runtime observations are incomplete")
    for snapshot_group in (runtime_before, runtime_after):
        for snapshot in snapshot_group.values():
            _validate_retained_runtime_snapshot(snapshot, profile, record)
    expected_waits = [0, 5, 20, 50, 50]
    if len(observations) != len(expected_waits):
        raise PilotError("blocked evidence cleanup observation window is incomplete")
    for index, (observation, expected_wait) in enumerate(
        zip(observations, expected_waits, strict=True),
        start=1,
    ):
        expected_observation_keys = {
            "attempt",
            "wait_before_seconds",
            "cumulative_wait_seconds",
            "pods",
            "assessment",
        }
        if index == len(expected_waits):
            expected_observation_keys.add("phase")
        _require_exact_keys(
            observation,
            expected_observation_keys,
            "cleanup observation",
        )
        assert isinstance(observation, dict)
        if (
            not _is_exact_json_integer(observation["attempt"], expected=index)
            or not _is_exact_json_integer(
                observation["cumulative_wait_seconds"],
                expected=expected_wait,
            )
            or not _is_exact_json_integer(
                observation["wait_before_seconds"],
                expected=(CLEANUP_RETRY_DELAYS_SECONDS[index - 1] if index <= 4 else 0),
            )
            or (index == 5 and observation.get("phase") != "final_capture_bracket_verified")
        ):
            raise PilotError("blocked evidence cleanup observation schedule changed")
        for snapshot in observation["pods"].values():
            _validate_retained_runtime_snapshot(snapshot, profile, record)
    recomputed_shared_memory = _finalize_runtime_cleanup_assessment(
        runtime_before,
        observations,
    )
    if (
        not _json_exact_equal(recomputed_shared_memory, cleanup.get("shared_memory"))
        or recomputed_shared_memory.get("failure_classification")
        != MUTABLE_OBJECT_CLEANUP_CLASSIFICATION
        or recomputed_shared_memory.get("stable_paired_semaphore_fingerprints") is not True
    ):
        raise PilotError("blocked evidence does not prove stable paired Ray semaphores")

    cluster_state_before = cleanup.get("cluster_state_before")
    cluster_state_after = cleanup.get("cluster_state_after")
    if not isinstance(cluster_state_before, dict) or not isinstance(cluster_state_after, dict):
        raise PilotError("blocked evidence cluster state is incomplete")
    for cluster_state in (cluster_state_before, cluster_state_after):
        _validate_retained_cluster_state(cluster_state)
    cluster_cleanup = _verify_cluster_cleanup(cluster_state_before, cluster_state_after)
    _require_exact_keys(
        cleanup.get("cluster_state"),
        {
            "status",
            "active_pilot_actors",
            "active_pilot_tasks",
            "object_count_delta",
            "object_bytes_delta",
            "object_identity_restored",
            "global_gc_completed",
        },
        "cluster cleanup",
    )
    if not _json_exact_equal(cleanup.get("cluster_state"), cluster_cleanup):
        raise PilotError("blocked evidence cluster cleanup proof is inconsistent")
    zero_residual_state = {
        "active_pilot_actor_count": cluster_state_after["active_pilot_actor_count"],
        "active_pilot_task_count": cluster_state_after["active_pilot_task_count"],
        "object_count": cluster_state_after["object_count"],
        "object_bytes": cluster_state_after["object_bytes"],
        "pilot_child_process_count": sum(
            snapshot["pilot_child_process_count"] for snapshot in runtime_after.values()
        ),
    }
    if any(snapshot.get("pilot_child_processes") != [] for snapshot in runtime_after.values()):
        raise PilotError("blocked evidence retains a pilot child-process identity")
    _require_exact_keys(
        record.get("zero_residual_state"),
        {
            "active_pilot_actor_count",
            "active_pilot_task_count",
            "object_count",
            "object_bytes",
            "pilot_child_process_count",
        },
        "zero residual state",
    )
    if any(zero_residual_state.values()) or not _json_exact_equal(
        record.get("zero_residual_state"), zero_residual_state
    ):
        raise PilotError("blocked evidence does not independently prove zero other residuals")


def _validate_current_blocked_evidence_record(
    record: dict[str, Any],
    *,
    require_namespace_deleted: bool,
) -> None:
    """Validate a record and bind it to the active source/profile assets."""

    _validate_blocked_evidence_record(
        record,
        require_namespace_deleted=require_namespace_deleted,
    )
    tracked_profile = _load_profile()
    if not _json_exact_equal(record["profile"], tracked_profile):
        raise PilotError("blocked evidence does not use the current tracked profile")
    retained_namespace_lease = _validate_retained_namespace_lease(
        record.get("namespace_lease"),
        record,
    )
    expected_configuration_id = _configuration_identity()
    _manifest, rendered_configuration_id, expected_manifest_id = _render_manifest(
        record["image"],
        record["image_id"],
        retained_namespace_lease,
    )
    if (
        record["configuration_id"] != expected_configuration_id
        or rendered_configuration_id != expected_configuration_id
        or record["rendered_manifest_id"] != expected_manifest_id
    ):
        raise PilotError("blocked evidence tracked configuration or rendered manifest changed")


def _write_blocked_evidence(path: Path, record: dict[str, Any]) -> None:
    current_revision = _git_source_revision()
    if current_revision != record.get("source_revision"):
        raise PilotError("blocked evidence source revision no longer matches clean current HEAD")
    _validate_current_blocked_evidence_record(record, require_namespace_deleted=True)
    serialized = _serialize_retained_evidence(record, pretty=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as evidence_file:
            evidence_file.write(serialized)
    except FileExistsError as error:
        raise PilotError("refusing to overwrite retained Compiled Graph evidence") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="build and execute the dedicated local pilot")
    run.add_argument("--context", required=True)
    run.add_argument("--namespace", default=PILOT_NAMESPACE)
    run.add_argument("--keep-cluster", action="store_true")
    run.add_argument(
        "--blocked-evidence-output",
        help=(
            "write a fresh non-promotion blocked record under docs/investigations; "
            "the command still exits nonzero"
        ),
    )

    probe = subparsers.add_parser("probe", help=argparse.SUPPRESS)
    probe.add_argument(
        "--topology",
        required=True,
        choices=(
            CompiledGraphTopology.DIRECT_DRIVER.value,
            CompiledGraphTopology.NESTED_RAY_TASK.value,
        ),
    )
    probe.add_argument("--timeout-seconds", type=float, default=180)

    hard_timeout = subparsers.add_parser("hard-timeout", help=argparse.SUPPRESS)
    hard_timeout.add_argument("--timeout-seconds", type=float, default=0.25)

    child = subparsers.add_parser("probe-child", help=argparse.SUPPRESS)
    child.add_argument(
        "--topology",
        required=True,
        choices=(
            CompiledGraphTopology.DIRECT_DRIVER.value,
            CompiledGraphTopology.NESTED_RAY_TASK.value,
        ),
    )
    subparsers.add_parser("near-neighbor", help=argparse.SUPPRESS)
    subparsers.add_parser("hang-child", help=argparse.SUPPRESS)
    subparsers.add_parser("inspect-pod-runtime", help=argparse.SUPPRESS)
    subparsers.add_parser("inspect-cluster-state", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "run":
            if arguments.keep_cluster and arguments.blocked_evidence_output:
                raise PilotError("--keep-cluster cannot be combined with --blocked-evidence-output")
            blocked_evidence_output = (
                _resolve_blocked_evidence_output(arguments.blocked_evidence_output)
                if arguments.blocked_evidence_output
                else None
            )
            result = run_host_pilot(
                arguments.context,
                arguments.namespace,
                keep_cluster=arguments.keep_cluster,
            )
            if result.get("status") == "blocked" and blocked_evidence_output is not None:
                _write_blocked_evidence(blocked_evidence_output, result)
        elif arguments.command == "probe":
            if not 1 <= arguments.timeout_seconds <= 600:
                raise PilotError("probe timeout must be from 1 through 600 seconds")
            result = _run_probe_parent(arguments.topology, arguments.timeout_seconds)
        elif arguments.command == "probe-child":
            return _run_probe_child_control_record(arguments.topology)
        elif arguments.command == "near-neighbor":
            result = _near_neighbor_guard()
        elif arguments.command == "hard-timeout":
            result = _run_hard_timeout_self_test(arguments.timeout_seconds)
        elif arguments.command == "hang-child":
            time.sleep(600)
            raise PilotError("hard-timeout child unexpectedly reached its terminal path")
        elif arguments.command == "inspect-pod-runtime":
            result = _inspect_pod_runtime()
        elif arguments.command == "inspect-cluster-state":
            result = _inspect_cluster_state()
        else:  # pragma: no cover
            raise PilotError(f"unsupported command: {arguments.command}")
    except BaseException as error:
        print(
            json.dumps(
                {
                    "schema_version": PILOT_SCHEMA_VERSION,
                    "status": "failure",
                    "error_type": type(error).__name__,
                    "error": _tail(str(error), limit=8_192),
                },
                sort_keys=True,
            )
        )
        return 1
    print(_serialize_retained_evidence(result, pretty=False))
    if arguments.command == "run" and result.get("status") != "success":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
