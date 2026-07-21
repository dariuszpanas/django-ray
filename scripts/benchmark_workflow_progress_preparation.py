"""Run isolated workflow-progress preparation prototype benchmarks.

Each scenario runs in a fresh subprocess.  The parent owns an ephemeral
scenario directory and removes it after reading the worker result, including
when the worker times out or exits abnormally.  This benchmark never writes
durable workflow-progress rows and does not activate schema-v3 production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from secrets import compare_digest, token_hex
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_SCHEMA_VERSION = 1
DEFAULT_NODES = (100, 500)
REQUIRED_SCALE_NODES = (25_000, 100_000, 250_000)
DEFAULT_PROFILES = ("sparse", "high-edge")
DEFAULT_TIMEOUT_SECONDS = 1_800.0
REQUIRED_SCALE_TIMEOUT_SECONDS = 7_200.0
SCENARIO_PREFIX = "django-ray-preparation-benchmark-"
WORKER_READY_NAME = "worker-ready.json"
RSS_SAMPLE_INTERVAL_SECONDS = 0.01
WORKSPACE_PREFIX = "django-ray-preparation-"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", nargs="+", type=int)
    parser.add_argument(
        "--required-scale",
        action="store_true",
        help="run 25k, 100k, and 250k observed-node scenarios",
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=DEFAULT_PROFILES,
        default=list(DEFAULT_PROFILES),
    )
    parser.add_argument("--high-edge-factor", type=int, default=8)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="per-scenario timeout (default: 1800s, or 7200s with --required-scale)",
    )
    parser.add_argument("--cache-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--spill-max-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--control-reserve-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--node-max-items", type=int, default=1_000_000)
    parser.add_argument("--edge-max-items", type=int, default=4_000_000)
    parser.add_argument("--detail-max-items", type=int, default=1_000_000)
    parser.add_argument("--batch-items", type=int, default=256)
    parser.add_argument("--batch-decoded-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--workspace-parent", type=Path)

    worker = parser.add_argument_group("internal worker arguments")
    worker.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    worker.add_argument("--worker-nodes", type=int, help=argparse.SUPPRESS)
    worker.add_argument("--worker-profile", choices=DEFAULT_PROFILES, help=argparse.SUPPRESS)
    worker.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    worker.add_argument("--worker-workspace-root", type=Path, help=argparse.SUPPRESS)
    worker.add_argument("--worker-ready", type=Path, help=argparse.SUPPRESS)
    worker.add_argument("--worker-nonce", help=argparse.SUPPRESS)
    worker.add_argument(
        "--worker-fault",
        choices=("hold-open",),
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def _validated_nodes(args: argparse.Namespace) -> tuple[int, ...]:
    if args.required_scale and args.nodes:
        raise ValueError("--required-scale and --nodes are mutually exclusive")
    if args.required_scale and (
        len(args.profiles) != len(DEFAULT_PROFILES) or set(args.profiles) != set(DEFAULT_PROFILES)
    ):
        raise ValueError("--required-scale requires both sparse and high-edge profiles")
    values = tuple(args.nodes or (REQUIRED_SCALE_NODES if args.required_scale else DEFAULT_NODES))
    if not values or len(values) != len(set(values)):
        raise ValueError("node cardinalities must be non-empty and unique")
    if any(value <= 0 or value > args.node_max_items for value in values):
        raise ValueError("node cardinalities exceed the configured observed-node budget")
    if args.high_edge_factor <= 0:
        raise ValueError("--high-edge-factor must be positive")
    if args.timeout_seconds is None:
        args.timeout_seconds = (
            REQUIRED_SCALE_TIMEOUT_SECONDS if args.required_scale else DEFAULT_TIMEOUT_SECONDS
        )
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    for value in values:
        if "high-edge" in args.profiles:
            if args.high_edge_factor >= value:
                raise ValueError("--high-edge-factor must be smaller than every node count")
            if value * args.high_edge_factor > args.edge_max_items:
                raise ValueError("high-edge cardinality exceeds the configured edge budget")
    return values


def _git_revision() -> tuple[str, bool]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--short"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "unavailable", False
    return revision, dirty


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "source-checkout" if package == "django-ray" else "unavailable"


def _implementation_digest() -> str:
    digest = hashlib.sha256()
    for path in (
        Path(__file__),
        ROOT / "scripts" / "workflow_progress_preparation_prototype.py",
        ROOT / "src" / "django_ray" / "workflow_progress_storage.py",
    ):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _source_snapshot() -> dict[str, Any]:
    revision, dirty = _git_revision()
    return {
        "revision": revision,
        "dirty": dirty,
        "implementation_digest": _implementation_digest(),
    }


def _require_unchanged_source(
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    if before != after:
        raise RuntimeError(
            "benchmark source revision, worktree state, or implementation digest changed during run"
        )


def _node_id(index: int) -> str:
    return f"node-{index:09d}"


def _nodes(count: int):
    for index in range(count):
        node_id = _node_id(index)
        yield {
            "node_id": node_id,
            "kind": "task",
            "label": f"Node {node_id}",
            "callable_path": "benchmarks.sync_resource",
            "runtime_env": {},
            "ray_options": {},
        }


def _edges(count: int, profile: str, high_edge_factor: int):
    if profile == "sparse":
        for index in range(1, count):
            yield {"source": _node_id(index - 1), "target": _node_id(index)}
        return
    for source_index in range(count):
        for offset in range(1, high_edge_factor + 1):
            yield {
                "source": _node_id(source_index),
                "target": _node_id((source_index + offset) % count),
            }


def _details(count: int):
    for index in range(count):
        yield {
            "schema_version": 1,
            "node_id": _node_id(index),
            "invocation_identity": None,
            "state": "PENDING",
            "progress": None,
            "execution": None,
            "fanout": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "recent_events": [],
        }


def _peak_resource_rss() -> int | None:
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        return None
    return value if sys.platform == "darwin" else value * 1024


class _RssSampler:
    """Record explicitly scoped process high-water and sampled-current RSS."""

    def __init__(self) -> None:
        self.baseline_current_bytes: int | None = None
        self.baseline_high_water_bytes: int | None = None
        self.sampled_peak_bytes: int | None = None
        self.process_high_water_bytes: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _RssSampler:
        self.baseline_high_water_bytes = _peak_resource_rss()
        try:
            import psutil

            process = psutil.Process()
        except (ImportError, OSError):
            return self

        def sample() -> None:
            while not self._stop.wait(RSS_SAMPLE_INTERVAL_SECONDS):
                try:
                    current = int(process.memory_info().rss)
                    self.sampled_peak_bytes = max(self.sampled_peak_bytes or 0, current)
                except (OSError, RuntimeError):
                    return

        self.baseline_current_bytes = int(process.memory_info().rss)
        self.sampled_peak_bytes = self.baseline_current_bytes
        self._thread = threading.Thread(target=sample, name="preparation-rss", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.process_high_water_bytes = _peak_resource_rss()

    def report(self) -> dict[str, Any]:
        if self.process_high_water_bytes is not None:
            method = "resource.getrusage(RUSAGE_SELF).ru_maxrss"
            scope = "fresh child process lifetime high-water"
            baseline = self.baseline_high_water_bytes
            peak = self.process_high_water_bytes
        elif self.sampled_peak_bytes is not None:
            method = "psutil.Process.memory_info().rss sampling"
            scope = "preparation measurement window"
            baseline = self.baseline_current_bytes
            peak = self.sampled_peak_bytes
        else:
            method = "unavailable"
            scope = "unavailable"
            baseline = None
            peak = None
        return {
            "peak_bytes": peak,
            "method": method,
            "scope": scope,
            "baseline_bytes": baseline,
            "baseline_current_bytes": self.baseline_current_bytes,
            "baseline_high_water_bytes": self.baseline_high_water_bytes,
            "sample_interval_seconds": (
                RSS_SAMPLE_INTERVAL_SECONDS if self._thread is not None else None
            ),
            "sampled_peak_bytes": self.sampled_peak_bytes,
            "process_high_water_bytes": self.process_high_water_bytes,
        }


def _filesystem_metadata(path: Path) -> dict[str, Any]:
    """Return useful volume metadata without disclosing the workspace path."""
    resolved = path.resolve()
    stat = resolved.stat()
    identity = hashlib.sha256(f"{platform.system()}:{stat.st_dev}".encode()).hexdigest()
    allocation_block_bytes = getattr(stat, "st_blksize", None)
    try:
        file_system = os.statvfs(resolved)
    except (AttributeError, OSError):
        pass
    else:
        allocation_block_bytes = int(file_system.f_frsize or file_system.f_bsize)

    filesystem_type = "unavailable"
    try:
        import psutil

        candidates = []
        for partition in psutil.disk_partitions(all=True):
            try:
                mountpoint = Path(partition.mountpoint).resolve()
                if resolved == mountpoint or resolved.is_relative_to(mountpoint):
                    candidates.append((len(str(mountpoint)), partition.fstype or "unknown"))
            except (OSError, RuntimeError, ValueError):
                continue
        if candidates:
            filesystem_type = max(candidates)[1]
    except (ImportError, OSError):
        pass
    return {
        "identity_sha256": identity,
        "identity_method": "sha256(platform, st_dev)",
        "filesystem_type": filesystem_type,
        "allocation_block_bytes": allocation_block_bytes,
    }


def _canonical_workspace_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith(WORKSPACE_PREFIX):
        return None
    suffix = value.removeprefix(WORKSPACE_PREFIX)
    try:
        workspace_id = UUID(suffix)
    except ValueError:
        return None
    return value if value == f"{WORKSPACE_PREFIX}{workspace_id}" else None


def _validated_worker_readiness(
    readiness: dict[str, Any],
    *,
    expected_nonce: str,
) -> str:
    nonce = readiness.get("nonce")
    if not isinstance(nonce, str) or not compare_digest(nonce, expected_nonce):
        raise RuntimeError("forced-termination worker readiness nonce is invalid")
    if readiness.get("state") != "workspace-open":
        raise RuntimeError("forced-termination worker readiness state is invalid")
    workspace_name = _canonical_workspace_name(readiness.get("workspace_name"))
    if workspace_name is None:
        raise RuntimeError("forced-termination workspace identity is invalid")
    return workspace_name


def _write_atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _worker_environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
        "django": _package_version("Django"),
        "django_ray": _package_version("django-ray"),
        "pid": os.getpid(),
    }


def _v1_output_limits() -> dict[str, Any]:
    import django_ray.workflow_progress_storage as storage

    names = (
        "WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION",
        "WORKFLOW_PROGRESS_LIMITS_PROFILE",
        "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS",
        "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES",
        "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES",
        "WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES",
        "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS",
        "WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS",
        "WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES",
        "WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES",
        "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS",
        "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
        "WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES",
        "WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES",
        "WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES",
        "WORKFLOW_PROGRESS_VALUE_MAX_DEPTH",
        "WORKFLOW_PROGRESS_METRICS_MAX_ITEMS",
        "WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES",
        "WORKFLOW_PROGRESS_METRIC_KEY_MAX_BYTES",
        "WORKFLOW_PROGRESS_METRIC_STRING_MAX_BYTES",
        "WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES",
        "WORKFLOW_PROGRESS_LABEL_MAX_BYTES",
        "WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES",
        "WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS",
        "WORKFLOW_PROGRESS_EVENT_MAX_ENCODED_BYTES",
        "WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES",
        "WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER",
    )
    return {
        name.removeprefix("WORKFLOW_PROGRESS_").lower(): getattr(storage, name) for name in names
    }


def _workspace_config(args: argparse.Namespace):
    from scripts.workflow_progress_preparation_prototype import (
        SQLitePreparationConfig,
    )

    return SQLitePreparationConfig(
        cache_bytes=args.cache_bytes,
        max_spill_bytes=args.spill_max_bytes,
        control_reserve_bytes=args.control_reserve_bytes,
        max_node_items=args.node_max_items,
        max_edge_items=args.edge_max_items,
        max_detail_items=args.detail_max_items,
        batch_max_items=args.batch_items,
        batch_max_decoded_bytes=args.batch_decoded_bytes,
    ).validated()


def _hold_open_worker(args: argparse.Namespace) -> int:
    if args.worker_workspace_root is None or args.worker_ready is None or args.worker_nonce is None:
        raise ValueError("hold-open worker requires workspace, readiness, and nonce values")
    from scripts.workflow_progress_preparation_prototype import SQLitePreparationWorkspace

    workspace = SQLitePreparationWorkspace(
        _workspace_config(args),
        parent_directory=args.worker_workspace_root,
    )
    workspace.__enter__()
    if workspace.directory is None:
        raise AssertionError("entered preparation workspace has no directory")
    _write_atomic_json(
        args.worker_ready,
        {
            "nonce": args.worker_nonce,
            "state": "workspace-open",
            "workspace_name": workspace.directory.name,
        },
    )
    threading.Event().wait()
    return 1


def _worker(args: argparse.Namespace) -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings")
    import django

    django.setup()
    if args.worker_fault == "hold-open":
        return _hold_open_worker(args)
    if (
        args.worker_nodes is None
        or args.worker_profile is None
        or args.worker_output is None
        or args.worker_workspace_root is None
    ):
        raise ValueError("worker mode requires one complete scenario")

    from django_ray.runtime.context import WorkflowRunIdentity
    from scripts.workflow_progress_preparation_prototype import SQLitePreparationWorkspace

    config = _workspace_config(args)
    node_count = args.worker_nodes
    edge_count = (
        max(0, node_count - 1)
        if args.worker_profile == "sparse"
        else node_count * args.high_edge_factor
    )
    identity = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=1,
        run_id="00000000-0000-0000-0000-000000000140",
    )
    workspace = SQLitePreparationWorkspace(
        config,
        parent_directory=args.worker_workspace_root,
    )
    tracemalloc.start()
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    with _RssSampler() as rss:
        with workspace:
            pragmas = workspace.sqlite_pragmas()
            topology = workspace.prepare_topology(
                identity,
                1,
                _nodes(node_count),
                _edges(node_count, args.worker_profile, args.high_edge_factor),
            )
            detail = workspace.prepare_detail(_details(node_count), topology=topology)
            query_plans = workspace.retained_query_plans()
            spill_peak_bytes = workspace.spill_peak_bytes
            spill_items = workspace.spill_items
    cpu_seconds = time.process_time() - cpu_started
    wall_seconds = time.perf_counter() - wall_started
    _, tracemalloc_peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_measurement = rss.report()
    report = {
        "profile": args.worker_profile,
        "observed_nodes": node_count,
        "observed_edges": edge_count,
        "observed_detail": detail.observed_count,
        "retained_nodes": topology.retained_node_count,
        "retained_edges": topology.retained_edge_count,
        "retained_detail": len(detail.records),
        "topology_pages": len(topology.pages),
        "topology_encoded_bytes": topology.encoded_bytes,
        "topology_decoded_bytes": topology.decoded_bytes,
        "detail_encoded_bytes": detail.encoded_bytes,
        "detail_decoded_bytes": detail.decoded_bytes,
        "manifest_digest": topology.manifest_digest,
        "truncation_reasons": sorted(
            set(topology.truncation_reasons) | set(detail.truncation_reasons)
        ),
        "topology_truncation_reasons": list(topology.truncation_reasons),
        "detail_truncation_reasons": list(detail.truncation_reasons),
        "wall_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "tracemalloc_peak_bytes": tracemalloc_peak_bytes,
        "peak_rss_bytes": rss_measurement["peak_bytes"],
        "rss_measurement": rss_measurement,
        "spill_peak_bytes": spill_peak_bytes,
        "spill_items": spill_items,
        "cleanup": {
            "worker_context": workspace.cleanup_outcome,
            "workspace_exists_after_context": workspace.path_exists,
        },
        "budgets": asdict(config),
        "v1_output_limits": _v1_output_limits(),
        "sqlite_pragmas": pragmas,
        "query_plans": list(query_plans),
        "environment": {
            **_worker_environment(),
            "filesystem": _filesystem_metadata(args.worker_workspace_root),
        },
    }
    args.worker_output.parent.mkdir(parents=True, exist_ok=True)
    args.worker_output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _worker_command(
    args: argparse.Namespace,
    *,
    nodes: int,
    profile: str,
    output: Path,
    workspace_root: Path,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--worker-nodes",
        str(nodes),
        "--worker-profile",
        profile,
        "--worker-output",
        str(output),
        "--worker-workspace-root",
        str(workspace_root),
        *_configuration_arguments(args),
    ]


def _configuration_arguments(args: argparse.Namespace) -> list[str]:
    return [
        "--high-edge-factor",
        str(args.high_edge_factor),
        "--cache-bytes",
        str(args.cache_bytes),
        "--spill-max-bytes",
        str(args.spill_max_bytes),
        "--control-reserve-bytes",
        str(args.control_reserve_bytes),
        "--node-max-items",
        str(args.node_max_items),
        "--edge-max-items",
        str(args.edge_max_items),
        "--detail-max-items",
        str(args.detail_max_items),
        "--batch-items",
        str(args.batch_items),
        "--batch-decoded-bytes",
        str(args.batch_decoded_bytes),
    ]


def _hold_open_worker_command(
    args: argparse.Namespace,
    *,
    nonce: str,
    ready: Path,
    workspace_root: Path,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--worker-fault",
        "hold-open",
        "--worker-ready",
        str(ready),
        "--worker-nonce",
        nonce,
        "--worker-workspace-root",
        str(workspace_root),
        *_configuration_arguments(args),
    ]


def _make_scenario_root(args: argparse.Namespace) -> tuple[Path, Path]:
    parent_candidate = (
        args.workspace_parent if args.workspace_parent else Path(tempfile.gettempdir())
    )
    parent_candidate.mkdir(parents=True, exist_ok=True)
    parent = parent_candidate.resolve(strict=True)
    if not parent.is_dir():
        raise RuntimeError("benchmark workspace parent is not a directory")
    scenario_root = Path(tempfile.mkdtemp(prefix=SCENARIO_PREFIX, dir=parent)).resolve()
    return parent, scenario_root


def _remove_scenario_root(path: Path, *, expected_parent: Path) -> str:
    if (
        path.name == SCENARIO_PREFIX
        or not path.name.startswith(SCENARIO_PREFIX)
        or path.parent != expected_parent
        or path.is_symlink()
        or path.resolve() != path
    ):
        return "refused"
    if not os.path.lexists(path):
        return "failed"
    try:
        shutil.rmtree(path)
    except OSError:
        return "failed"
    return "removed" if not os.path.lexists(path) else "failed"


def _launch_worker(command: list[str]) -> subprocess.Popen[str]:
    launch_options: dict[str, Any] = {}
    if os.name == "posix":
        launch_options["start_new_session"] = True
    elif os.name == "nt":
        launch_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        raise RuntimeError(f"unsupported benchmark process-tree platform: {os.name}")
    return subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **launch_options,
    )


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    tree_error: BaseException | None = None
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            tree_error = error
    elif os.name == "nt":
        try:
            terminated = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30.0,
            )
        except (OSError, subprocess.SubprocessError) as error:
            tree_error = error
        else:
            if terminated.returncode != 0 and process.poll() is None:
                tree_error = RuntimeError(
                    "taskkill did not terminate the owned benchmark launcher tree: "
                    f"{terminated.stderr.strip()!r}"
                )
    else:
        tree_error = RuntimeError(f"unsupported benchmark process-tree platform: {os.name}")

    if tree_error is not None and process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.communicate(timeout=10.0)
    except subprocess.TimeoutExpired as error:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.communicate(timeout=10.0)
        except subprocess.TimeoutExpired as repeated:
            raise RuntimeError("owned benchmark launcher did not terminate") from repeated
        if tree_error is None:
            tree_error = error
    if tree_error is not None:
        raise RuntimeError("failed to terminate the owned benchmark process tree") from tree_error


def _run_worker_process(command: list[str], *, timeout_seconds: float) -> tuple[str, str]:
    process = _launch_worker(command)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _terminate_process_tree(process)
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=error.output,
            stderr=error.stderr,
        ) from error
    except BaseException:
        _terminate_process_tree(process)
        raise
    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return stdout, stderr


def _run_scenario(args: argparse.Namespace, *, nodes: int, profile: str) -> dict[str, Any]:
    parent, scenario_root = _make_scenario_root(args)
    output = scenario_root / "worker-result.json"
    report: dict[str, Any] | None = None
    error: BaseException | None = None
    try:
        _run_worker_process(
            _worker_command(
                args,
                nodes=nodes,
                profile=profile,
                output=output,
                workspace_root=scenario_root,
            ),
            timeout_seconds=args.timeout_seconds,
        )
        report = json.loads(output.read_text(encoding="utf-8"))
    except BaseException as caught:
        error = caught
    parent_cleanup = _remove_scenario_root(scenario_root, expected_parent=parent)
    if parent_cleanup != "removed":
        raise RuntimeError(
            f"parent watchdog cleanup {parent_cleanup} for benchmark scenario"
        ) from error
    if error is not None:
        raise error
    if report is None:
        raise RuntimeError("preparation benchmark worker produced no report")
    report["cleanup"]["parent_watchdog"] = parent_cleanup
    report["cleanup"]["scenario_root_exists_after_parent"] = scenario_root.exists()
    return report


def _forced_termination_probe(args: argparse.Namespace) -> dict[str, Any]:
    parent, scenario_root = _make_scenario_root(args)
    ready = scenario_root / WORKER_READY_NAME
    nonce = token_hex(32)
    process = _launch_worker(
        _hold_open_worker_command(
            args,
            nonce=nonce,
            ready=ready,
            workspace_root=scenario_root,
        )
    )
    readiness: dict[str, Any] | None = None
    error: BaseException | None = None
    returncode: int | None = None
    workspace_open_before_kill = False
    candidate_exists_before_cleanup = False
    try:
        deadline = time.monotonic() + min(args.timeout_seconds, 30.0)
        while time.monotonic() < deadline:
            if ready.is_file():
                readiness = json.loads(ready.read_text(encoding="utf-8"))
                workspace_name = _validated_worker_readiness(
                    readiness,
                    expected_nonce=nonce,
                )
                if process.poll() is not None:
                    raise RuntimeError(
                        "forced-termination launcher exited before the owned tree was terminated"
                    )
                workspace_open_before_kill = (scenario_root / workspace_name).is_dir()
                if not workspace_open_before_kill:
                    raise RuntimeError("forced-termination worker workspace is not open")
                break
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise RuntimeError(
                    "forced-termination worker exited before readiness: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
        else:
            raise TimeoutError("forced-termination worker did not become ready")
        _terminate_process_tree(process)
        returncode = process.returncode
        if returncode in (None, 0):
            raise RuntimeError("forced-termination worker was not forcibly terminated")
        candidate_exists_before_cleanup = (scenario_root / "worker-result.json").exists()
    except BaseException as caught:
        error = caught
        try:
            _terminate_process_tree(process)
        except BaseException as termination_error:
            error = termination_error
        returncode = process.returncode
    filesystem = _filesystem_metadata(scenario_root)
    cleanup = _remove_scenario_root(scenario_root, expected_parent=parent)
    if cleanup != "removed":
        raise RuntimeError(
            f"parent watchdog cleanup {cleanup} for forced-termination probe"
        ) from error
    if error is not None:
        raise error
    return {
        "outcome": "forcibly-terminated",
        "readiness_observed": readiness is not None,
        "workspace_open_before_kill": workspace_open_before_kill,
        "worker_returncode": returncode,
        "durable_candidate_exists_before_cleanup": candidate_exists_before_cleanup,
        "durable_candidate_exists_after_cleanup": False,
        "parent_watchdog": cleanup,
        "scenario_root_exists_after_parent": scenario_root.exists(),
        "filesystem": filesystem,
    }


def _write_report(report: dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(report, indent=2, sort_keys=True)
    if output is None:
        print(serialized)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized + "\n", encoding="utf-8")


def main() -> int:
    args = _parse_args()
    if args.worker:
        return _worker(args)
    nodes = _validated_nodes(args)
    source_before = _source_snapshot()
    forced_termination = _forced_termination_probe(args)
    cases: list[dict[str, Any]] = []
    total_cases = len(nodes) * len(args.profiles)
    for index, (node_count, profile) in enumerate(
        ((node_count, profile) for node_count in nodes for profile in args.profiles),
        start=1,
    ):
        print(
            f"[{index}/{total_cases}] starting nodes={node_count} profile={profile}",
            file=sys.stderr,
            flush=True,
        )
        case = _run_scenario(args, nodes=node_count, profile=profile)
        cases.append(case)
        print(
            f"[{index}/{total_cases}] completed nodes={node_count} profile={profile} "
            f"wall_seconds={case['wall_seconds']}",
            file=sys.stderr,
            flush=True,
        )
    source_after = _source_snapshot()
    _require_unchanged_source(source_before, source_after)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_revision": source_before["revision"],
        "source_dirty": source_before["dirty"],
        "implementation_digest": source_before["implementation_digest"],
        "source_snapshot_before": source_before,
        "source_snapshot_after": source_after,
        "command": " ".join(sys.argv),
        "cases": cases,
        "forced_termination": forced_termination,
        "cleanup_contract": (
            "Context cleanup handles normal return and Python exceptions; the parent watchdog "
            "owns the ephemeral scenario directory after worker exit or termination."
        ),
    }
    _write_report(report, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
