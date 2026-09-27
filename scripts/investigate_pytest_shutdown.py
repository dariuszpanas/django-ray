"""Bounded Linux diagnostics for interpreter shutdown; never release certification.

Every observation uses a fresh interpreter under the existing native observer.
The supervisor owns only its descendants, including children adopted when GDB
or Ray changes process groups. It stops on the first failed observation.
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module, metadata
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.coverage_debt import MAX_PHASE_LOG_BYTES, _BoundedPhaseOutput  # noqa: E402
from scripts.observe_pytest_exit import observe_native, run_pytest  # noqa: E402
from scripts.require_linux import require_linux  # noqa: E402
from tests.real_ray_ownership import RealRayOwnershipLock, build_owner_metadata  # noqa: E402

TOTAL_SECONDS = 20 * 60
CLEANUP_SECONDS = 10
STAGE_SECONDS = {"imports": 300, "collection": 300, "ray": 600}
IMPORTS = {
    "grpc": ("grpc",),
    "ray": ("ray",),
    "grpc-ray": ("grpc", "ray"),
    "ray-grpc": ("ray", "grpc"),
}


@dataclass(frozen=True)
class Probe:
    """One fixed diagnostic recipe, with no user-supplied executable code."""

    stage: str
    recipe: str
    repetition: int
    timeout: int

    @property
    def name(self) -> str:
        return f"{self.stage}-{self.recipe}-{self.repetition:02d}"


def probes() -> list[Probe]:
    """Keep the same order and workload for both supported interpreters."""
    return (
        [Probe("imports", recipe, repeat, 30) for repeat in range(10) for recipe in IMPORTS]
        + [Probe("collection", "collect", repeat, 60) for repeat in range(5)]
        + [
            Probe("ray", recipe, repeat, 120)
            for repeat in range(4)
            for recipe in ("ray-bare", "ray-wait")
        ]
    )


def emit(event: str, **values: object) -> None:
    print(json.dumps({"shutdown_investigation": event, **values}, sort_keys=True), flush=True)


def run_child(recipe: str) -> int:
    """Execute synthetic imports or a small, owned local-Ray lifecycle."""
    require_linux()
    emit("child_started", recipe=recipe)
    atexit.register(emit, "atexit_checkpoint", recipe=recipe)
    if recipe in IMPORTS:
        for module in IMPORTS[recipe]:
            import_module(module)
    elif recipe == "collect":
        return run_pytest(["--collect-only", "-q", "-m", "not live_cluster", "tests"])
    elif recipe in {"ray-bare", "ray-wait"}:
        import ray

        from tests.local_ray import init_local_ray

        wait = recipe == "ray-wait"
        for cycle in range(3):
            init_local_ray(num_cpus=2, include_dashboard=False)
            try:
                task = ray.remote(lambda value: value + 1)
                assert ray.get(task.remote(41), timeout=30) == 42
            finally:
                ray.shutdown(wait_for_processes=wait)
            emit("ray_cycle_finished", cycle=cycle, wait=wait)
    else:
        raise ValueError("Unknown shutdown diagnostic recipe")
    emit("child_returned", recipe=recipe)
    return 0


def enable_subreaper() -> None:
    """Adopt only this isolated supervisor's orphaned descendants on Linux."""
    require_linux()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise RuntimeError("Cannot establish owned descendant cleanup")


def cleanup_owned(process: subprocess.Popen[bytes]) -> tuple[bool, int]:
    """Stop this supervisor's descendants, never processes found by name."""
    started = time.monotonic()
    deadline = started + 6
    # Process identities include creation time and protect signal delivery from
    # PID reuse. Retain handles even when a later tree snapshot loses a parent.
    owned: set[psutil.Process] = set()
    terminated: set[psutil.Process] = set()
    killed: set[psutil.Process] = set()
    while time.monotonic() < deadline:
        owned.update(psutil.Process().children(recursive=True))
        force = time.monotonic() >= started + 3
        signalled = killed if force else terminated
        for child in owned - signalled:
            try:
                if force:
                    child.kill()
                else:
                    child.terminate()
            except psutil.NoSuchProcess:
                pass
            signalled.add(child)
        try:
            # Only Popen may reap the launcher, preserving its actual exit code.
            process.wait(timeout=0)
        except subprocess.TimeoutExpired:
            pass
        else:
            # A recursive /proc snapshot can miss a child whose parent exits
            # during enumeration. Only kernel ECHILD proves the subreaper has
            # no remaining descendants; reap adopted zombies and rescan live ones.
            while time.monotonic() < deadline:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return True, len(owned)
                if pid == 0:
                    break
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    return False, len(owned)


def run_probe(probe: Probe, timeout: float, output: Path) -> dict[str, object]:
    """Reuse bounded log capture while containing detached native descendants."""
    command = [sys.executable, str(Path(__file__).resolve()), "--observe", probe.recipe]
    capture = _BoundedPhaseOutput(MAX_PHASE_LOG_BYTES)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    reader = threading.Thread(target=capture.consume, args=(process.stdout,), daemon=True)
    timed_out = False
    interrupted = False
    clean = False
    retained_count = 0
    try:
        reader.start()
        try:
            process.wait(timeout=timeout)
            # Allow ordinary reaper shutdown without silently accepting leftovers.
            settle = time.monotonic() + 1
            while psutil.Process().children(recursive=True) and time.monotonic() < settle:
                time.sleep(0.05)
        except subprocess.TimeoutExpired:
            timed_out = True
        except KeyboardInterrupt:
            interrupted = True
    finally:
        clean, retained_count = cleanup_owned(process)
        if reader.ident is not None:
            reader.join(timeout=2)
    size, tail, capture_error = capture.snapshot()
    (output / f"{probe.name}.log").write_bytes(tail)
    code = None if timed_out or interrupted else process.returncode
    passed = (
        code == 0
        and not timed_out
        and not interrupted
        and clean
        and capture_error is None
        and not reader.is_alive()
    )
    return {
        "name": probe.name,
        "recipe": probe.recipe,
        "stage": probe.stage,
        "returncode": code,
        "passed": passed,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "cleanup_complete": clean,
        "descendants_terminated": retained_count,
        "cleanup_needed_after_exit": retained_count > 0 and code == 0,
        "capture_complete": capture_error is None and not reader.is_alive(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output_bytes": size,
        "retained_bytes": len(tail),
    }


def investigate(
    output: Path,
    *,
    runner: Callable[[Probe, float, Path], dict[str, object]] = run_probe,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Stop on failure or deadline; never turn exhausted sampling into a pass."""
    output.mkdir(parents=True, exist_ok=True)
    deadline = clock() + TOTAL_SECONDS
    stage = ""
    stage_deadline = deadline
    observations: list[dict[str, object]] = []
    outcome = "complete-no-reproduction"
    plan = probes()
    for probe in plan:
        if probe.stage != stage:
            stage = probe.stage
            stage_deadline = min(deadline, clock() + STAGE_SECONDS[stage])
        remaining = min(deadline, stage_deadline) - clock() - CLEANUP_SECONDS
        if remaining <= 0:
            outcome = "deadline-exhausted"
            break
        emit("probe_started", name=probe.name)
        observation = runner(probe, min(probe.timeout, remaining), output)
        observations.append(observation)
        emit("probe_finished", **observation)
        if not observation["passed"]:
            outcome = "failed-observation"
            break
    receipt = {
        "intent": "diagnostic-only",
        "outcome": outcome,
        "planned": len(plan),
        "completed": len(observations),
        "cleanup_interventions": sum(
            bool(observation.get("cleanup_needed_after_exit")) for observation in observations
        ),
        "observations": observations,
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=(*IMPORTS, "collect", "ray-bare", "ray-wait"))
    parser.add_argument("--observe", choices=(*IMPORTS, "collect", "ray-bare", "ray-wait"))
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/native-shutdown-investigation"
    )
    options = parser.parse_args(arguments)
    require_linux()
    if options.child:
        return run_child(options.child)
    if options.observe:
        return observe_native(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--child", options.observe]
        )
    enable_subreaper()

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupt)
    ownership = RealRayOwnershipLock()
    ownership.acquire(build_owner_metadata(rootpath=ROOT, selected_count=8))
    try:
        emit(
            "environment",
            python=sys.version.split()[0],
            versions={
                package: metadata.version(package) for package in ("ray", "grpcio", "pytest")
            },
        )
        receipt = investigate(options.output)
    finally:
        ownership.release()
        signal.signal(signal.SIGTERM, previous_handler)
    return 0 if receipt["outcome"] == "complete-no-reproduction" else 1


if __name__ == "__main__":
    raise SystemExit(main())
