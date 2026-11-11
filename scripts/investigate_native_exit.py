"""Bounded hosted diagnostics for an unexplained pytest driver exit, not certification."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.coverage_debt import _BoundedPhaseOutput  # noqa: E402
from scripts.investigate_pytest_shutdown import cleanup_owned, enable_subreaper  # noqa: E402
from scripts.require_linux import require_linux  # noqa: E402

MAX_SECONDS = 18 * 60
CLEANUP_RESERVE = 15
MAX_OUTPUT_BYTES = 262_144
MAX_RESOURCE_SAMPLES = 128
CHILD_POLL_SECONDS = 0.1
RESOURCE_SAMPLE_SECONDS = 1
MAX_RAY_LOG_BYTES = 32 * 1024
OVERLAYS = ("scripts/observe_pytest_exit.py", "scripts/pytest_exit_gdb.py")
OMITTED_CONTRACT = "tests/unit/test_observe_pytest_exit.py"
RAY_LOGS = ("raylet.err", "raylet.out", "gcs_server.err", "gcs_server.out", "runtime_env_agent.log")
MEMORY_EVENTS = ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")
PACKAGES = ("ray", "django", "pytest", "pytest-cov", "coverage", "grpcio", "protobuf")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_identity(source: Path, timeout: float) -> dict[str, object]:
    """Bind the base tree and the two diagnostic overlays without calling it an exact tree."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD", "HEAD^{tree}"],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    head, tree = result.stdout.split()
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, tree)):
        raise ValueError("Invalid source identity")
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    ).stdout.splitlines()
    if set(changed) - set(OVERLAYS):
        raise ValueError("Unexpected tracked source modification")
    return {
        "source_head": head,
        "source_tree": tree,
        "modified_overlay_paths": sorted(changed),
        "overlay_sha256": {name: digest(source / name) for name in OVERLAYS},
        "lock_sha256_before": digest(source / "uv.lock"),
        "pyproject_sha256": digest(source / "pyproject.toml"),
        "diagnostic_overlay_is_a_reproduction_perturbation": True,
    }


def probe_command(probe: str, trace: bool, phase_file: Path) -> list[str]:
    """Keep uv ancestor options faithful: --no-sync would change Ray's worker hook."""
    command = [
        "uv",
        "run",
        "--python",
        "3.14.10",
        "python",
        "scripts/observe_pytest_exit.py",
        "--native-debug",
        "--phase-journal",
        str(phase_file),
    ]
    if trace:
        command.append("--trace-native-exit")
    command.append("--")
    if probe == "distributed":
        command.append("tests/unit/test_distributed.py")
        floor = "0"
    elif probe == "prefix":
        command.extend(["tests", "-m", "not live_cluster", f"--ignore={OMITTED_CONTRACT}"])
        floor = "95"
    else:
        raise ValueError("Unknown fixed probe")
    return [*command, "--cov=src", "--cov-report=term", f"--cov-fail-under={floor}", "-v"]


def probe_environment(hook_mode: str, ray_tmpdir: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["RAY_TMPDIR"] = str(ray_tmpdir)
    environment["PYTHONFAULTHANDLER"] = "1"
    environment["DJANGO_RAY_REQUIRE_KUSTOMIZE_PROBE_TESTS"] = "1"
    if hook_mode == "historical":
        environment.pop("RAY_ENABLE_UV_RUN_RUNTIME_ENV", None)
    elif hook_mode == "disabled":
        environment["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"
    else:
        raise ValueError("Unknown hook mode")
    return environment


def create_ray_tmpdir() -> Path:
    """Leave space for Ray's session/socket suffix within Linux's 107-byte limit."""
    return Path(tempfile.mkdtemp(prefix="drx-", dir="/tmp"))


def cgroup_directory() -> Path:
    root = Path("/sys/fs/cgroup")
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            path = (root / line[3:].lstrip("/")).resolve()
            if path.is_relative_to(root):
                return path
    return root


def resource_sample(ray_tmpdir: Path) -> dict[str, object]:
    """Read only numeric capacity/counters; never process arguments or environment values."""
    values: dict[str, object] = {}
    try:
        disk = os.statvfs(ray_tmpdir)
        values.update(
            disk_free_bytes=disk.f_bavail * disk.f_frsize,
            disk_total_bytes=disk.f_blocks * disk.f_frsize,
            inodes_free=disk.f_favail,
            inodes_total=disk.f_files,
        )
    except OSError:
        values["disk_unavailable"] = True
    memory = psutil.virtual_memory()
    values.update(memory_total_bytes=memory.total, memory_available_bytes=memory.available)
    try:
        cgroup = cgroup_directory()
        for name in ("current", "max"):
            raw = (cgroup / f"memory.{name}").read_text()[:128].strip()
            values[f"cgroup_memory_{name}"] = "max" if raw == "max" else int(raw)
        events = dict(line.split() for line in (cgroup / "memory.events").read_text().splitlines())
        values["cgroup_memory_events"] = {
            key: int(events[key]) for key in MEMORY_EVENTS if key in events
        }
    except (OSError, ValueError):
        values["cgroup_unavailable"] = True
    count = 0
    for child in psutil.Process().children(recursive=True):
        try:
            count += child.name() == "uv"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    values["uv_descendants"] = count
    return values


class ResourceWindow:
    """Retain a fixed ring and whole-trial extrema, including early resource pressure."""

    def __init__(self) -> None:
        self.samples: deque[dict[str, object]] = deque(maxlen=MAX_RESOURCE_SAMPLES)
        self.minimum: dict[str, int] = {}
        self.maximum: dict[str, int] = {}
        self.count = 0

    def add(self, elapsed: float, sample: dict[str, object]) -> None:
        self.count += 1
        self.samples.append({"elapsed_seconds": round(elapsed, 3), **sample})
        counters = {**sample}
        events = counters.pop("cgroup_memory_events", {})
        if isinstance(events, dict):
            counters.update({f"cgroup_event_{key}": value for key, value in events.items()})
        for key, value in counters.items():
            if type(value) is int:
                self.minimum[key] = min(self.minimum.get(key, value), value)
                self.maximum[key] = max(self.maximum.get(key, value), value)

    def receipt(self) -> dict[str, object]:
        return {
            "sample_count": self.count,
            "sample_interval_seconds": RESOURCE_SAMPLE_SECONDS,
            "retained_samples": list(self.samples),
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


def retain_ray_logs(ray_tmpdir: Path, output: Path) -> list[dict[str, object]]:
    """Read five named tails and one native driver tail from three owned sessions."""
    ray_root = ray_tmpdir / "ray"
    if ray_root.is_symlink() or not ray_root.is_dir():
        return []
    sessions = heapq.nlargest(
        3,
        (
            path
            for path in ray_root.iterdir()
            if path.name.startswith("session_") and not path.is_symlink() and path.is_dir()
        ),
        key=lambda path: path.stat().st_mtime_ns,
    )
    retained: list[dict[str, object]] = []
    for number, session in enumerate(sessions, 1):
        logs = session / "logs"
        if logs.is_symlink() or not logs.is_dir():
            continue
        drivers = heapq.nlargest(
            1,
            (
                path
                for path in logs.iterdir()
                if re.fullmatch(r"python-core-driver-[0-9a-f]+_[0-9]+\.log", path.name)
                and not path.is_symlink()
                and path.is_file()
            ),
            key=lambda path: path.stat().st_mtime_ns,
        )
        for name in (*RAY_LOGS, *(path.name for path in drivers)):
            path = logs / name
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    stream.seek(max(0, info.st_size - MAX_RAY_LOG_BYTES))
                    tail = stream.read(MAX_RAY_LOG_BYTES)
            except OSError:
                continue
            label = "core-driver.log" if name.startswith("python-core-driver-") else name
            target = f"ray-session-{number}-{label}"
            (output / target).write_bytes(tail)
            retained.append(
                {"file": target, "source_bytes": info.st_size, "retained_bytes": len(tail)}
            )
    return retained


def phase_snapshot(path: Path) -> dict[str, object] | None:
    from scripts.observe_pytest_exit import read_phase

    return read_phase(path)


def reap_adopted_zombies(launcher_pid: int) -> int:
    """Reap adopted direct children without consuming the Popen launcher's status.

    This isolated supervisor has only one outstanding Popen child during its
    polling loop. Other direct children are adopted orphans. Waiting on -1 could
    race the launcher's exit and make Popen report a fabricated success, so use
    explicit adopted zombie PIDs only. Live children remain owned for cleanup.
    """
    reaped = 0
    for child in psutil.Process().children(recursive=False):
        if child.pid == launcher_pid:
            continue
        try:
            if child.status() != psutil.STATUS_ZOMBIE:
                continue
            pid, _ = os.waitpid(child.pid, os.WNOHANG)
        except (psutil.NoSuchProcess, ChildProcessError):
            continue
        reaped += pid != 0
    return reaped


def supervise(
    source: Path,
    output: Path,
    *,
    probe: str,
    hook_mode: str,
    trace: bool,
    timeout: int = MAX_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    started = clock()
    deadline = started + timeout
    source, output = source.resolve(), output.resolve()
    if not CLEANUP_RESERVE < timeout <= MAX_SECONDS:
        raise ValueError("Diagnostic timeout must reserve cleanup within eighteen minutes")
    if output.is_relative_to(source):
        raise ValueError("Diagnostic output must be outside the source archive")
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("receipt.json", "pytest.log", "pytest-phase.bin")):
        raise ValueError("Diagnostic output already contains a trial")
    identity = source_identity(source, min(5, timeout - CLEANUP_RESERVE))
    versions = {package: metadata.version(package) for package in PACKAGES}
    uv_version = subprocess.run(
        ["uv", "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=min(5, max(0.1, deadline - clock() - CLEANUP_RESERVE)),
    ).stdout.strip()
    if not re.fullmatch(r"uv [0-9]+\.[0-9]+\.[0-9]+(?: \([^\r\n]{1,100}\))?", uv_version):
        raise ValueError("Invalid uv version identity")
    ray_tmpdir = create_ray_tmpdir()
    if ray_tmpdir.is_relative_to(source) or ray_tmpdir.is_relative_to(output):
        raise ValueError("Ray temporary directory must be outside source and diagnostic output")
    phase_file = output / "pytest-phase.bin"
    resources = ResourceWindow()
    capture = _BoundedPhaseOutput(MAX_OUTPUT_BYTES)
    process = None
    reader = None
    timed_out = interrupted = False
    cleanup_complete = True
    descendants = 0
    adopted_zombies_reaped = 0
    failure = None
    try:
        if deadline - clock() <= CLEANUP_RESERVE:
            timed_out = True
        else:
            resources.add(clock() - started, resource_sample(ray_tmpdir))
            next_resource_sample = clock() + RESOURCE_SAMPLE_SECONDS
            process = subprocess.Popen(
                probe_command(probe, trace, phase_file),
                cwd=source,
                env=probe_environment(hook_mode, ray_tmpdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            assert process.stdout is not None
            reader = threading.Thread(target=capture.consume, args=(process.stdout,), daemon=True)
            reader.start()
            while True:
                remaining = deadline - clock() - CLEANUP_RESERVE
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    process.wait(timeout=min(CHILD_POLL_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    finished = False
                else:
                    finished = True
                adopted_zombies_reaped += reap_adopted_zombies(process.pid)
                sampled_at = clock()
                if finished or sampled_at >= next_resource_sample:
                    resources.add(sampled_at - started, resource_sample(ray_tmpdir))
                    next_resource_sample = sampled_at + RESOURCE_SAMPLE_SECONDS
                if finished:
                    break
    except KeyboardInterrupt:
        interrupted = True
    except Exception as error:
        failure = type(error).__name__
    finally:
        if process is not None:
            try:
                cleanup_complete, descendants = cleanup_owned(process)
            except Exception as error:
                cleanup_complete = False
                failure = type(error).__name__
        if reader is not None and reader.ident is not None:
            reader.join(timeout=2)
        elif process is not None and process.stdout is not None:
            process.stdout.close()
    size, tail, capture_error = capture.snapshot()
    (output / "pytest.log").write_bytes(tail)
    raw_code = None if process is None else process.returncode
    code = 124 if timed_out else 130 if interrupted else raw_code
    code = 2 if code is None else code if code >= 0 else 128 - code
    identity["lock_sha256_after"] = digest(source / "uv.lock")
    lock_unchanged = identity["lock_sha256_before"] == identity["lock_sha256_after"]
    capture_complete = capture_error is None and (reader is None or not reader.is_alive())
    if failure or not cleanup_complete or not capture_complete or not lock_unchanged:
        code = 2
    receipt = {
        "intent": "diagnostic-only",
        "outcome": "no-reproduction" if code == 0 else "failed-observation",
        "probe": probe,
        "hook_mode": hook_mode,
        "native_exit_tracing": trace,
        "omitted_superseded_contracts": [OMITTED_CONTRACT] if probe == "prefix" else [],
        "python": sys.version.split()[0],
        "uv": uv_version,
        "versions": versions,
        "cpu_count": os.cpu_count(),
        "identity": identity,
        "lock_unchanged": lock_unchanged,
        "returncode": raw_code,
        "exit_code": code,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "failure_kind": failure,
        "cleanup_complete": cleanup_complete,
        "descendants_terminated": descendants,
        "adopted_zombies_reaped_during_probe": adopted_zombies_reaped,
        "capture_complete": capture_complete,
        "output_bytes": size,
        "retained_output_bytes": len(tail),
        "last_pytest_phase": phase_snapshot(phase_file),
        "resources": resources.receipt(),
        "ray_logs": retain_ray_logs(ray_tmpdir, output),
        "owned_ray_tmpdir_retained_for_disposable_runner_teardown": True,
        "elapsed_seconds": round(clock() - started, 3),
        "timeout_seconds": timeout,
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--probe", choices=("distributed", "prefix"), required=True)
    parser.add_argument("--hook-mode", choices=("historical", "disabled"), required=True)
    parser.add_argument("--trace-native-exit", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=MAX_SECONDS)
    options = parser.parse_args(arguments)
    require_linux()
    enable_subreaper()

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        receipt = supervise(
            options.source,
            options.output,
            probe=options.probe,
            hook_mode=options.hook_mode,
            trace=options.trace_native_exit,
            timeout=options.timeout_seconds,
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(
        json.dumps(
            {"native_exit_investigation": receipt["outcome"], "exit_code": receipt["exit_code"]}
        )
    )
    return int(receipt["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
