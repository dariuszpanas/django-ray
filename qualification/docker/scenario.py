"""Run one exact, source-owned real-Ray qualification."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import BinaryIO
from xml.etree import ElementTree

_SCHEMA = "django-ray.docker-qualification"
_SCHEMA_VERSION = 1
_SCENARIO = "result-fold-real-ray"
_DEFINITION_PATH = "qualification/docker/runbook.yaml"
_TEST_NODE = (
    "tests/unit/test_result_fold.py::"
    "test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup"
)
_TEST_NAME = "test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup"
_ASSERTIONS = (
    "actor-cleanup",
    "direct-object-ref-return",
    "exact-actor-resources",
    "out-of-order-fold",
    "ray-shutdown",
    "runtime-env",
)
_WHEEL_DIRECTORY = Path("/opt/django-ray-wheels")
_INSTALL_TARGET = Path("/tmp/django-ray-qualification/target")
_EVIDENCE_ROOT = Path("/evidence")
_UV_EXECUTABLE = Path("/usr/local/bin/uv")
_HOLD_ENVIRONMENT = "DJANGO_RAY_QUALIFICATION_HOLD_SECONDS"
_PYTEST_TIMEOUT_SECONDS = 240
_INSTALL_TIMEOUT_SECONDS = 60
_MAX_COMMAND_OUTPUT_BYTES = 32 * 1024
_MAX_JUNIT_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_TARGET_BYTES = 4096
_MAX_DISTRIBUTIONS = 256
_IGNORED_TREE_PARTS = frozenset(("__pycache__",))
_IGNORED_TREE_SUFFIXES = frozenset((".pyc", ".pyo"))
_SAFE_ENVIRONMENT_KEYS = frozenset(
    ("HOME", "LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "TZ", "WINDIR")
)
_CANONICAL_JUNIT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<testsuite name="result-fold-real-ray" tests="1" failures="0" errors="0" skipped="0">\n'
    b'  <testcase classname="tests.unit.test_result_fold" '
    b'name="test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup" />\n'
    b"</testsuite>\n"
)


class QualificationError(RuntimeError):
    """A bounded, caller-safe qualification failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BoundedProcessError(QualificationError):
    """A subprocess failure with already-bounded diagnostic streams."""

    def __init__(self, code: str, stdout: bytes, stderr: bytes) -> None:
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """The bounded result of one shell-free subprocess invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class _CaptureSignals:
    changed: threading.Event
    closing: threading.Event
    overflow: threading.Event
    read_failed: threading.Event


@dataclass(frozen=True, slots=True)
class Candidate:
    """Verified installed candidate identity."""

    distribution_location: str
    installed_package_tree_sha256: str
    module_location: str
    source_package_tree_sha256: str
    version: str
    wheel_filename: str
    wheel_sha256: str

    def as_manifest(self) -> dict[str, str]:
        return {
            "distribution_location": self.distribution_location,
            "distribution_name": "django-ray",
            "installed_package_tree_sha256": self.installed_package_tree_sha256,
            "module_location": self.module_location,
            "source_package_tree_sha256": self.source_package_tree_sha256,
            "version": self.version,
            "wheel_filename": self.wheel_filename,
            "wheel_sha256": self.wheel_sha256,
        }


class CandidateQualificationError(QualificationError):
    """Retain a verified candidate for every post-inspection failure."""

    def __init__(self, candidate: Candidate, code: str, exit_code: int | None) -> None:
        self.candidate = candidate
        self.exit_code = exit_code
        super().__init__(code)


def _source_root() -> Path:
    return Path(__file__).resolve(strict=True).parents[2]


def _source_version(source_root: Path) -> str:
    with (source_root / "pyproject.toml").open("rb") as stream:
        value = tomllib.load(stream)["project"]["version"]
    if not isinstance(value, str) or not value:
        raise QualificationError("invalid-source-version")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _package_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if any(part in _IGNORED_TREE_PARTS for part in relative.parts):
            continue
        if candidate.suffix in _IGNORED_TREE_SUFFIXES:
            continue
        if candidate.is_symlink():
            raise QualificationError("package-tree-symlink")
        if candidate.is_file():
            files.append(candidate)
    if not files:
        raise QualificationError("empty-package-tree")
    return tuple(sorted(files, key=lambda item: item.relative_to(root).as_posix()))


def _package_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for source in _package_files(root):
        relative = source.relative_to(root).as_posix().encode("utf-8")
        content = source.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _require_non_root() -> None:
    get_effective_user = getattr(os, "geteuid", None)
    if get_effective_user is None or get_effective_user() == 0:
        raise QualificationError("non-root-required")


def _require_linux_process_groups() -> None:
    if sys.platform != "linux" or not hasattr(os, "killpg"):
        raise QualificationError("linux-process-groups-required")


def _select_wheel(directory: Path) -> Path:
    if directory.is_symlink() or not directory.is_dir():
        raise QualificationError("invalid-wheel-directory")
    entries = tuple(directory.iterdir())
    wheels = tuple(path for path in entries if path.suffix == ".whl" and path.is_file())
    if len(entries) != 1 or len(wheels) != 1 or wheels[0].is_symlink():
        raise QualificationError("expected-one-wheel")
    return wheels[0].resolve(strict=True)


def _prepare_install_target(target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise QualificationError("install-target-exists")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise QualificationError("invalid-install-parent")
    target.mkdir(mode=0o700)


def _subprocess_environment(
    *,
    install_target: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, str]:
    selected = {key: value for key, value in os.environ.items() if key in _SAFE_ENVIRONMENT_KEYS}
    selected.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTEST_ADDOPTS": "",
            "RAY_DEDUP_LOGS": "0",
            "RAY_USAGE_STATS_ENABLED": "0",
            "UV_NO_CACHE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    if install_target is not None:
        paths = [str(install_target)]
        if source_root is not None:
            paths.append(str(source_root))
        selected["PYTHONPATH"] = os.pathsep.join(paths)
    return selected


def _bounded_output(value: bytes | None) -> str:
    payload = value or b""
    if len(payload) > _MAX_COMMAND_OUTPUT_BYTES:
        payload = payload[:_MAX_COMMAND_OUTPUT_BYTES] + b"\n...[truncated]...\n"
    return payload.decode("utf-8", errors="replace")


def _emit_failure_output(label: str, value: bytes | None) -> None:
    output = _bounded_output(value)
    if output:
        print(f"qualification={_SCENARIO} diagnostic={label}", file=sys.stderr)
        print(output, end="" if output.endswith("\n") else "\n", file=sys.stderr)


def _drain_stream(
    stream: BinaryIO,
    captured: bytearray,
    signals: _CaptureSignals,
    maximum: int,
) -> None:
    try:
        while chunk := stream.read(8192):
            remaining = maximum + 1 - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining or len(captured) > maximum:
                signals.overflow.set()
                signals.changed.set()
    except (OSError, ValueError):
        if not signals.closing.is_set():
            signals.read_failed.set()
            signals.changed.set()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> bool:
    terminated = True
    if os.name == "posix":
        kill_group = getattr(os, "killpg", None)
        try:
            if kill_group is None:
                terminated = False
            else:
                kill_group(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            terminated = False
    elif process.poll() is None:
        try:
            process.kill()
        except OSError:
            terminated = False
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return terminated


def _close_capture_pipes(
    process: subprocess.Popen[bytes],
    signals: _CaptureSignals,
) -> None:
    signals.closing.set()
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _join_readers(readers: tuple[threading.Thread, ...]) -> bool:
    for reader in readers:
        reader.join(timeout=1)
    return not any(reader.is_alive() for reader in readers)


def _capture_failure(signals: _CaptureSignals) -> str | None:
    if signals.read_failed.is_set():
        return "command-output-read-failed"
    if signals.overflow.is_set():
        return "command-output-limit"
    return None


def _finish_capture(
    process: subprocess.Popen[bytes],
    readers: tuple[threading.Thread, ...],
    stdout: bytearray,
    stderr: bytearray,
    signals: _CaptureSignals,
) -> tuple[bytes, bytes, str | None]:
    output_not_closed = not _join_readers(readers)
    if output_not_closed:
        _terminate_process_tree(process)
        _join_readers(readers)
    if any(reader.is_alive() for reader in readers):
        _close_capture_pipes(process, signals)
        _join_readers(readers)
    else:
        _close_capture_pipes(process, signals)
    failure = "command-output-not-closed" if output_not_closed else _capture_failure(signals)
    return bytes(stdout), bytes(stderr), failure


def _wait_for_child(
    process: subprocess.Popen[bytes],
    signals: _CaptureSignals,
    timeout: float,
) -> str | None:
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if failure := _capture_failure(signals):
            return failure
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "command-timeout"
        signals.changed.wait(timeout=min(0.02, remaining))
    return _capture_failure(signals)


def _run_bounded_command(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> BoundedProcessResult:
    process = subprocess.Popen(
        command,
        bufsize=0,
        cwd=cwd,
        env=env,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    if process.stdout is None or process.stderr is None:
        _terminate_process_tree(process)
        raise QualificationError("command-output-unavailable")
    maximum = _MAX_COMMAND_OUTPUT_BYTES
    signals = _CaptureSignals(
        changed=threading.Event(),
        closing=threading.Event(),
        overflow=threading.Event(),
        read_failed=threading.Event(),
    )
    stdout = bytearray()
    stderr = bytearray()
    readers = (
        threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout, signals, maximum),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr, signals, maximum),
            daemon=True,
        ),
    )
    started: list[threading.Thread] = []
    failure = None
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
    except RuntimeError:
        failure = "command-reader-start-failed"
    if failure is None:
        failure = _wait_for_child(process, signals, timeout)
    stop_failed = failure is not None and not _terminate_process_tree(process)
    captured_stdout, captured_stderr, capture_failure = _finish_capture(
        process,
        tuple(started),
        stdout,
        stderr,
        signals,
    )
    if failure is None:
        failure = capture_failure
    elif stop_failed:
        failure = f"{failure}-stop-failed"
    if failure is not None:
        raise BoundedProcessError(failure, captured_stdout, captured_stderr)
    return BoundedProcessResult(process.wait(), captured_stdout, captured_stderr)


def _emit_process_failure(prefix: str, error: BoundedProcessError) -> None:
    _emit_failure_output(f"{prefix}-stdout", error.stdout)
    _emit_failure_output(f"{prefix}-stderr", error.stderr)


def _install_wheel(wheel: Path, target: Path, uv_executable: Path) -> None:
    command = (
        str(uv_executable),
        "pip",
        "install",
        "--quiet",
        "--offline",
        "--no-index",
        "--no-deps",
        "--python",
        sys.executable,
        "--target",
        str(target),
        str(wheel),
    )
    try:
        result = _run_bounded_command(
            command,
            cwd=target.parent,
            env=_subprocess_environment(),
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except BoundedProcessError as error:
        _emit_process_failure("wheel-install", error)
        suffix = error.code.removeprefix("command-")
        raise QualificationError(f"wheel-install-{suffix}") from None
    except OSError:
        raise QualificationError("wheel-install-unavailable") from None
    if result.returncode != 0:
        _emit_failure_output("wheel-install-stdout", result.stdout)
        _emit_failure_output("wheel-install-stderr", result.stderr)
        raise QualificationError("wheel-install-failed")


def _canonical_name(value: object) -> str:
    if not isinstance(value, str):
        raise QualificationError("invalid-distribution-name")
    selected = re.sub(r"[-_.]+", "-", value).lower()
    if (
        len(selected.encode("ascii", errors="ignore")) != len(selected)
        or not 1 <= len(selected) <= 128
    ):
        raise QualificationError("invalid-distribution-name")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", selected):
        raise QualificationError("invalid-distribution-name")
    return selected


def _target_distribution(target: Path) -> metadata.Distribution:
    distributions = tuple(metadata.distributions(path=[str(target)]))
    if len(distributions) != 1:
        raise QualificationError("expected-one-installed-distribution")
    distribution = distributions[0]
    if _canonical_name(distribution.metadata.get("Name")) != "django-ray":
        raise QualificationError("unexpected-installed-distribution")
    if Path(distribution.locate_file("")).resolve(strict=True) != target.resolve(strict=True):
        raise QualificationError("distribution-outside-target")
    return distribution


def _reject_preinstalled_django_ray() -> None:
    if any(
        _canonical_name(distribution.metadata.get("Name")) == "django-ray"
        for distribution in metadata.distributions()
    ):
        raise QualificationError("django-ray-preinstalled-outside-target")


def _import_candidate(target: Path) -> ModuleType:
    if any(name == "django_ray" or name.startswith("django_ray.") for name in sys.modules):
        raise QualificationError("django-ray-imported-before-install-verification")
    sys.path.insert(0, str(target))
    importlib.invalidate_caches()
    module = importlib.import_module("django_ray")
    module_file = getattr(module, "__file__", None)
    expected = (target / "django_ray" / "__init__.py").resolve(strict=True)
    if not isinstance(module_file, str) or Path(module_file).resolve(strict=True) != expected:
        raise QualificationError("django-ray-import-outside-target")
    return module


def _inspect_candidate(wheel: Path, target: Path, source_root: Path) -> Candidate:
    distribution = _target_distribution(target)
    source_version = _safe_version(_source_version(source_root))
    candidate_version = _safe_version(distribution.version)
    if candidate_version != source_version:
        raise QualificationError("candidate-version-mismatch")
    _reject_preinstalled_django_ray()
    module = _import_candidate(target)
    module_file = Path(str(module.__file__)).resolve(strict=True)
    installed_package = module_file.parent
    source_package = (source_root / "src" / "django_ray").resolve(strict=True)
    installed_digest = _package_tree_digest(installed_package)
    source_digest = _package_tree_digest(source_package)
    if installed_digest != source_digest:
        raise QualificationError("candidate-source-tree-mismatch")
    return Candidate(
        distribution_location=str(target.resolve(strict=True)),
        installed_package_tree_sha256=installed_digest,
        module_location=str(module_file),
        source_package_tree_sha256=source_digest,
        version=candidate_version,
        wheel_filename=wheel.name,
        wheel_sha256=_sha256(wheel),
    )


def _safe_version(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 128
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise QualificationError("invalid-distribution-version")
    return value


def _installed_distributions() -> list[dict[str, str]]:
    selected: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = _canonical_name(distribution.metadata.get("Name"))
        version = _safe_version(distribution.version)
        if name in selected:
            raise QualificationError("ambiguous-installed-distribution")
        selected[name] = version
    if not 1 <= len(selected) <= _MAX_DISTRIBUTIONS:
        raise QualificationError("installed-distribution-count-out-of-bounds")
    return [{"name": name, "version": selected[name]} for name in sorted(selected)]


def _dependency_manifest(candidate: Candidate) -> dict[str, object]:
    direct = {
        "django": _safe_version(metadata.version("django")),
        "django_ray": candidate.version,
        "pytest": _safe_version(metadata.version("pytest")),
        "python": _safe_version(platform.python_version()),
        "ray": _safe_version(metadata.version("ray")),
    }
    return {**direct, "installed_distributions": _installed_distributions()}


def _target_manifest(candidate: Candidate, dependencies: dict[str, object]) -> dict[str, object]:
    target = {
        "dependencies": {
            "django": dependencies["django"],
            "django-ray": candidate.version,
            "pytest": dependencies["pytest"],
            "ray": dependencies["ray"],
        },
        "python": dependencies["python"],
    }
    encoded = json.dumps(target, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )
    if len(encoded) > _MAX_TARGET_BYTES:
        raise QualificationError("target-manifest-too-large")
    return target


def _run_exact_test(source_root: Path, target: Path, junit_path: Path) -> int:
    command = (
        sys.executable,
        "-P",
        "-m",
        "pytest",
        _TEST_NODE,
        "-q",
        "--tb=short",
        "--color=no",
        "--maxfail=1",
        "-p",
        "no:cacheprovider",
        "--junitxml",
        str(junit_path),
    )
    try:
        result = _run_bounded_command(
            command,
            cwd=source_root,
            env=_subprocess_environment(install_target=target, source_root=source_root),
            timeout=_PYTEST_TIMEOUT_SECONDS,
        )
    except BoundedProcessError as error:
        _emit_process_failure("pytest", error)
        suffix = error.code.removeprefix("command-")
        raise QualificationError(f"pytest-{suffix}") from None
    except OSError:
        raise QualificationError("pytest-unavailable") from None
    if result.returncode != 0:
        _emit_failure_output("pytest-stdout", result.stdout)
        _emit_failure_output("pytest-stderr", result.stderr)
    return result.returncode


def _bounded_regular_bytes(path: Path, *, maximum: int) -> bytes:
    if path.is_symlink():
        raise QualificationError("missing-or-unsafe-junit")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise QualificationError("missing-or-unsafe-junit") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise QualificationError("missing-or-unsafe-junit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(maximum + 1)
    except OSError:
        raise QualificationError("missing-or-unsafe-junit") from None
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        raise QualificationError("junit-too-large")
    return payload


def _canonicalize_success_junit(path: Path) -> None:
    payload = _bounded_regular_bytes(path, maximum=_MAX_JUNIT_BYTES)
    if not payload.startswith(b'<?xml version="1.0" encoding="utf-8"?>'):
        raise QualificationError("unexpected-pytest-junit")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        raise QualificationError("invalid-pytest-junit") from None
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
        raise QualificationError("unexpected-pytest-junit")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        raise QualificationError("invalid-pytest-junit") from None

    suites = list(root)
    if root.tag != "testsuites" or root.attrib != {"name": "pytest tests"}:
        raise QualificationError("unexpected-pytest-junit")
    if len(suites) != 1 or suites[0].tag != "testsuite":
        raise QualificationError("unexpected-pytest-junit")
    suite = suites[0]
    expected_suite_attributes = {
        "errors": "0",
        "failures": "0",
        "name": "pytest",
        "skipped": "0",
        "tests": "1",
    }
    if set(suite.attrib) != {*expected_suite_attributes, "hostname", "time", "timestamp"}:
        raise QualificationError("unexpected-pytest-junit")
    if any(suite.attrib.get(name) != value for name, value in expected_suite_attributes.items()):
        raise QualificationError("unexpected-pytest-junit")
    if re.fullmatch(r"\d+(?:\.\d+)?", suite.attrib["time"]) is None:
        raise QualificationError("unexpected-pytest-junit")
    if not suite.attrib["timestamp"] or not suite.attrib["hostname"]:
        raise QualificationError("unexpected-pytest-junit")

    cases = list(suite)
    if len(cases) != 1 or cases[0].tag != "testcase":
        raise QualificationError("unexpected-pytest-junit")
    case = cases[0]
    if set(case.attrib) != {"classname", "name", "time"}:
        raise QualificationError("unexpected-pytest-junit")
    if case.attrib["classname"] != "tests.unit.test_result_fold":
        raise QualificationError("unexpected-pytest-junit")
    if case.attrib["name"] != _TEST_NAME:
        raise QualificationError("unexpected-pytest-junit")
    if re.fullmatch(r"\d+(?:\.\d+)?", case.attrib["time"]) is None or list(case):
        raise QualificationError("unexpected-pytest-junit")
    if any((element.text or "").strip() for element in (root, suite, case)):
        raise QualificationError("unexpected-pytest-junit")
    if any((element.tail or "").strip() for element in (suite, case)):
        raise QualificationError("unexpected-pytest-junit")
    temporary = path.with_suffix(".canonical.tmp")
    with temporary.open("xb") as stream:
        stream.write(_CANONICAL_JUNIT)
    os.replace(temporary, path)


def _failure_junit(code: str) -> bytes:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,127}", code) is None:
        code = "unexpected-error"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="result-fold-real-ray" tests="1" failures="1" errors="0" skipped="0">\n'
        '  <testcase classname="qualification.docker" name="qualification-contract">\n'
        f'    <failure message="{code}" />\n'
        "  </testcase>\n"
        "</testsuite>\n"
    ).encode("ascii")


def _ensure_failure_junit(path: Path, code: str) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise QualificationError("unsafe-existing-junit")
        path.unlink()
    with path.open("xb") as stream:
        stream.write(_failure_junit(code))


def _manifest(candidate: Candidate | None, *, exit_code: int | None, outcome: str) -> bytes:
    dependencies = None if candidate is None else _dependency_manifest(candidate)
    payload = {
        "candidate": None if candidate is None else candidate.as_manifest(),
        "definition_path": _DEFINITION_PATH,
        "dependencies": dependencies,
        "outcome": outcome,
        "scenario": _SCENARIO,
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "test": {
            "assertions": list(_ASSERTIONS),
            "node_id": _TEST_NODE,
            "pytest_exit_code": exit_code,
        },
    }
    if candidate is not None and dependencies is not None:
        payload["target"] = _target_manifest(candidate, dependencies)
    encoded = (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("ascii")
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise QualificationError("manifest-too-large")
    return encoded


def _ensure_evidence_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise QualificationError("invalid-evidence-root")
    if any(root.iterdir()):
        raise QualificationError("evidence-root-not-empty")


def _write_manifest(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _run_candidate(
    *,
    evidence_root: Path,
    wheel_directory: Path,
    install_target: Path,
    source_root: Path,
    uv_executable: Path,
) -> tuple[Candidate, int]:
    wheel = _select_wheel(wheel_directory)
    _prepare_install_target(install_target)
    _install_wheel(wheel, install_target, uv_executable)
    candidate = _inspect_candidate(wheel, install_target, source_root)
    junit_path = evidence_root / "junit.xml"
    exit_code: int | None = None
    try:
        exit_code = _run_exact_test(source_root, install_target, junit_path)
        if exit_code != 0:
            raise QualificationError("pytest-failed")
        _canonicalize_success_junit(junit_path)
    except Exception as error:
        code = error.code if isinstance(error, QualificationError) else type(error).__name__
        raise CandidateQualificationError(candidate, code, exit_code) from None
    return candidate, exit_code


def _record_failure(
    error: Exception,
    *,
    evidence_root: Path,
    candidate: Candidate | None,
    exit_code: int | None,
) -> int:
    if isinstance(error, CandidateQualificationError):
        candidate = error.candidate
        exit_code = error.exit_code
    code = error.code if isinstance(error, QualificationError) else type(error).__name__
    try:
        _ensure_failure_junit(evidence_root / "junit.xml", code)
        _write_manifest(
            evidence_root / "execution-manifest.json",
            _manifest(candidate, exit_code=exit_code, outcome="failed"),
        )
    except (OSError, QualificationError):
        pass
    print(f"qualification={_SCENARIO} phase=failed code={code}", file=sys.stderr)
    return 1


def _finish_success(hold_seconds: float) -> int:
    print(
        json.dumps(
            {"outcome": "passed", "scenario": _SCENARIO, "test_node": _TEST_NODE},
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )
    if hold_seconds:
        print(f"qualification={_SCENARIO} phase=holding", flush=True)
        time.sleep(hold_seconds)
    print(f"qualification={_SCENARIO} phase=finished", flush=True)
    return 0


def execute(  # noqa: PLR0913 - explicit injectable qualification boundaries
    *,
    evidence_root: Path,
    wheel_directory: Path,
    install_target: Path,
    source_root: Path,
    uv_executable: Path,
    hold_seconds: float,
    require_non_root: bool,
) -> int:
    """Run the exact target and retain bounded evidence for every reachable outcome."""
    candidate: Candidate | None = None
    exit_code: int | None = None
    print(f"qualification={_SCENARIO} phase=started", flush=True)
    try:
        _ensure_evidence_root(evidence_root)
        if require_non_root:
            _require_non_root()
            _require_linux_process_groups()
        candidate, exit_code = _run_candidate(
            evidence_root=evidence_root,
            wheel_directory=wheel_directory,
            install_target=install_target,
            source_root=source_root,
            uv_executable=uv_executable,
        )
        _write_manifest(
            evidence_root / "execution-manifest.json",
            _manifest(candidate, exit_code=exit_code, outcome="passed"),
        )
    except Exception as error:  # noqa: BLE001 - convert failures to bounded evidence
        return _record_failure(
            error,
            evidence_root=evidence_root,
            candidate=candidate,
            exit_code=exit_code,
        )
    return _finish_success(hold_seconds)


def _bounded_hold(value: str) -> float:
    try:
        selected = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("hold seconds must be a number") from None
    if not 0 <= selected <= 30:
        raise argparse.ArgumentTypeError("hold seconds must be between 0 and 30")
    return selected


def _hold_seconds() -> float:
    return _bounded_hold(os.environ.get(_HOLD_ENVIRONMENT, "0"))


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    parser.parse_args(argv)
    try:
        hold_seconds = _hold_seconds()
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    return execute(
        evidence_root=_EVIDENCE_ROOT,
        wheel_directory=_WHEEL_DIRECTORY,
        install_target=_INSTALL_TARGET,
        source_root=_source_root(),
        uv_executable=_UV_EXECUTABLE,
        hold_seconds=hold_seconds,
        require_non_root=True,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
