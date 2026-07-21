"""Run isolated workflow-progress preparation benchmarks.

Each scenario runs in a fresh subprocess.  The parent owns an ephemeral
scenario directory and removes it after reading the worker result, including
when the worker times out or exits abnormally.  This benchmark never writes
durable workflow-progress rows and does not activate schema-v3 production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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

REPORT_SCHEMA_VERSION = 2
DEFAULT_NODES = (100, 500)
REQUIRED_SCALE_NODES = (25_000, 100_000, 250_000)
DEFAULT_PROFILES = ("sparse", "high-edge")
IMPLEMENTATIONS = ("prototype-composite", "production-topology")
DEFAULT_TIMEOUT_SECONDS = 1_800.0
REQUIRED_SCALE_TIMEOUT_SECONDS = 7_200.0
SCENARIO_PREFIX = "django-ray-preparation-benchmark-"
WORKER_READY_NAME = "worker-ready.json"
RSS_SAMPLE_INTERVAL_SECONDS = 0.01
WORKSPACE_PREFIX = "django-ray-preparation-"

_PREPARATION_PAGE_BYTES = 4 * 1024
_PREPARATION_CACHE_MAX_BYTES = 8 * 1024 * 1024
_PREPARATION_SPILL_MAX_BYTES = 1024 * 1024 * 1024
_PREPARATION_CONTROL_RESERVE_MAX_BYTES = 4 * 1024 * 1024
_PREPARATION_MIN_DATABASE_BYTES = 64 * 1024
_PREPARATION_NODE_MAX_ITEMS = 1_000_000
_PREPARATION_EDGE_MAX_ITEMS = 4_000_000
_PREPARATION_DETAIL_MAX_ITEMS = 1_000_000
_PREPARATION_BATCH_MAX_ITEMS = 256
_PREPARATION_BATCH_MAX_DECODED_BYTES = 4 * 1024 * 1024

_PRODUCTION_RESIDENT_CONTRACT = (
    "phase-separated bounded topology candidate plus end-to-end legacy "
    "O(observed) observed_node_ids detachment"
)
_PROTOTYPE_RESIDENT_CONTRACT = (
    "bounded prototype composite without legacy observed_node_ids detachment"
)
_PRODUCTION_MEMORY_EVIDENCE_CONTRACT = (
    "production cases retain a bounded pre-legacy checkpoint and an end-to-end "
    "legacy-detachment peak"
)
_PROTOTYPE_MEMORY_EVIDENCE_CONTRACT = (
    "prototype cases retain one end-to-end composite preparation peak"
)
_CLEANUP_CONTRACT = (
    "Context cleanup handles normal return and Python exceptions; the parent watchdog "
    "owns the ephemeral scenario directory after worker exit or termination."
)

_REPORT_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "implementation",
        "required_scale",
        "created_at",
        "source_revision",
        "source_dirty",
        "implementation_digest",
        "source_snapshot_before",
        "source_snapshot_after",
        "command",
        "cases",
        "forced_termination",
        "memory_evidence_contract",
        "cleanup_contract",
    }
)
_SOURCE_SNAPSHOT_REQUIRED_FIELDS = frozenset({"revision", "dirty", "implementation_digest"})
_CASE_REQUIRED_FIELDS = frozenset(
    {
        "implementation",
        "profile",
        "observed_nodes",
        "observed_edges",
        "observed_detail",
        "retained_nodes",
        "retained_edges",
        "retained_detail",
        "topology_pages",
        "topology_encoded_bytes",
        "topology_decoded_bytes",
        "detail_encoded_bytes",
        "detail_decoded_bytes",
        "legacy_observed_node_ids",
        "manifest_digest",
        "truncation_reasons",
        "topology_truncation_reasons",
        "detail_truncation_reasons",
        "wall_seconds",
        "cpu_seconds",
        "bounded_phase_tracemalloc_current_bytes",
        "bounded_phase_tracemalloc_peak_bytes",
        "bounded_phase_peak_rss_bytes",
        "bounded_phase_rss_measurement",
        "tracemalloc_peak_bytes",
        "peak_rss_bytes",
        "end_to_end_tracemalloc_peak_bytes",
        "end_to_end_peak_rss_bytes",
        "rss_measurement",
        "spill_peak_bytes",
        "spill_items",
        "cleanup",
        "budgets",
        "v1_output_limits",
        "sqlite_pragmas",
        "query_plans",
        "resident_contract",
        "environment",
    }
)
_RSS_REQUIRED_FIELDS = frozenset(
    {
        "peak_bytes",
        "method",
        "scope",
        "baseline_bytes",
        "baseline_current_bytes",
        "baseline_high_water_bytes",
        "sample_interval_seconds",
        "sampled_peak_bytes",
        "process_high_water_bytes",
    }
)
_FILESYSTEM_REQUIRED_FIELDS = frozenset(
    {
        "identity_sha256",
        "identity_method",
        "filesystem_type",
        "allocation_block_bytes",
    }
)
_ENVIRONMENT_REQUIRED_FIELDS = frozenset(
    {
        "platform",
        "python",
        "python_implementation",
        "sqlite",
        "django",
        "django_ray",
        "pid",
        "filesystem",
    }
)
_CLEANUP_REQUIRED_FIELDS = frozenset(
    {
        "worker_context",
        "workspace_exists_after_context",
        "parent_watchdog",
        "scenario_root_exists_after_parent",
    }
)
_FORCED_TERMINATION_REQUIRED_FIELDS = frozenset(
    {
        "outcome",
        "readiness_observed",
        "workspace_open_before_kill",
        "worker_returncode",
        "durable_candidate_exists_before_cleanup",
        "durable_candidate_exists_after_cleanup",
        "parent_watchdog",
        "scenario_root_exists_after_parent",
        "filesystem",
    }
)
_COMMON_BUDGET_REQUIRED_FIELDS = frozenset(
    {
        "page_bytes",
        "cache_bytes",
        "mmap_bytes",
        "max_spill_bytes",
        "control_reserve_bytes",
        "max_node_items",
        "max_edge_items",
        "batch_max_items",
        "batch_max_decoded_bytes",
    }
)
_SQLITE_PRAGMA_REQUIRED_FIELDS = frozenset(
    {
        "page_size",
        "cache_size",
        "mmap_size",
        "temp_store",
        "journal_mode",
        "synchronous",
        "locking_mode",
        "foreign_keys",
        "trusted_schema",
        "max_page_count",
    }
)
_V1_OUTPUT_LIMIT_REQUIRED_FIELDS = frozenset(
    {
        "storage_protocol_version",
        "limits_profile",
        "topology_page_max_items",
        "topology_page_max_encoded_bytes",
        "topology_page_max_decoded_bytes",
        "record_max_encoded_bytes",
        "topology_node_max_items",
        "topology_edge_max_items",
        "topology_max_encoded_bytes",
        "topology_max_decoded_bytes",
        "detail_max_items",
        "detail_max_encoded_bytes",
        "detail_max_decoded_bytes",
        "combined_max_encoded_bytes",
        "combined_max_decoded_bytes",
        "value_max_depth",
        "metrics_max_items",
        "metrics_max_encoded_bytes",
        "metric_key_max_bytes",
        "metric_string_max_bytes",
        "node_id_max_bytes",
        "label_max_bytes",
        "message_max_bytes",
        "recent_event_max_items",
        "event_max_encoded_bytes",
        "topology_manifest_max_encoded_bytes",
        "identity_max_integer",
    }
)


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
    parser.add_argument(
        "--implementation",
        choices=IMPLEMENTATIONS,
        default="prototype-composite",
        help=(
            "prototype-composite measures issue #140 topology plus detail; "
            "production-topology measures issue #141 including legacy detachment"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="per-scenario timeout (default: 1800s, or 7200s with --required-scale)",
    )
    parser.add_argument("--cache-bytes", type=int, default=_PREPARATION_CACHE_MAX_BYTES)
    parser.add_argument("--spill-max-bytes", type=int, default=_PREPARATION_SPILL_MAX_BYTES)
    parser.add_argument(
        "--control-reserve-bytes",
        type=int,
        default=_PREPARATION_CONTROL_RESERVE_MAX_BYTES,
    )
    parser.add_argument("--node-max-items", type=int, default=_PREPARATION_NODE_MAX_ITEMS)
    parser.add_argument("--edge-max-items", type=int, default=_PREPARATION_EDGE_MAX_ITEMS)
    parser.add_argument("--detail-max-items", type=int, default=_PREPARATION_DETAIL_MAX_ITEMS)
    parser.add_argument("--batch-items", type=int, default=_PREPARATION_BATCH_MAX_ITEMS)
    parser.add_argument(
        "--batch-decoded-bytes",
        type=int,
        default=_PREPARATION_BATCH_MAX_DECODED_BYTES,
    )
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
        ROOT / "src" / "django_ray" / "workflow_progress_preparation.py",
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
        self._process: Any | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _RssSampler:
        self.baseline_high_water_bytes = _peak_resource_rss()
        try:
            import psutil

            process = psutil.Process()
        except (ImportError, OSError):
            return self

        self._process = process

        def sample() -> None:
            while not self._stop.wait(RSS_SAMPLE_INTERVAL_SECONDS):
                if self._sample_current() is None:
                    return

        self.baseline_current_bytes = self._sample_current()
        self.sampled_peak_bytes = self.baseline_current_bytes
        self._thread = threading.Thread(target=sample, name="preparation-rss", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._sample_current()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.process_high_water_bytes = _peak_resource_rss()

    def _sample_current(self) -> int | None:
        if self._process is None:
            return None
        try:
            current = int(self._process.memory_info().rss)
        except (OSError, RuntimeError):
            return None
        self.sampled_peak_bytes = max(self.sampled_peak_bytes or 0, current)
        return current

    def checkpoint(self, *, scope: str) -> dict[str, Any]:
        """Return high-water evidence through one named in-window phase."""
        self._sample_current()
        return self._report(
            process_high_water_bytes=_peak_resource_rss(),
            high_water_scope=f"fresh child process high-water through {scope}",
            sampled_scope=f"measurement window through {scope}",
        )

    def report(self) -> dict[str, Any]:
        return self._report(
            process_high_water_bytes=self.process_high_water_bytes,
            high_water_scope="fresh child process lifetime high-water",
            sampled_scope="preparation measurement window",
        )

    def _report(
        self,
        *,
        process_high_water_bytes: int | None,
        high_water_scope: str,
        sampled_scope: str,
    ) -> dict[str, Any]:
        if process_high_water_bytes is not None:
            method = "resource.getrusage(RUSAGE_SELF).ru_maxrss"
            scope = high_water_scope
            baseline = self.baseline_high_water_bytes
            peak = process_high_water_bytes
        elif self.sampled_peak_bytes is not None:
            method = "psutil.Process.memory_info().rss sampling"
            scope = sampled_scope
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
            "process_high_water_bytes": process_high_water_bytes,
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


def _implementation(args: argparse.Namespace) -> str:
    return str(getattr(args, "implementation", "prototype-composite"))


def _workspace_config(args: argparse.Namespace):
    common = {
        "cache_bytes": args.cache_bytes,
        "max_spill_bytes": args.spill_max_bytes,
        "control_reserve_bytes": args.control_reserve_bytes,
        "max_node_items": args.node_max_items,
        "max_edge_items": args.edge_max_items,
        "batch_max_items": args.batch_items,
        "batch_max_decoded_bytes": args.batch_decoded_bytes,
    }
    if _implementation(args) == "production-topology":
        from django_ray.workflow_progress_preparation import SQLitePreparationConfig

        return SQLitePreparationConfig(**common).validated()

    from scripts.workflow_progress_preparation_prototype import (
        SQLitePreparationConfig,
    )

    return SQLitePreparationConfig(
        **common,
        max_detail_items=args.detail_max_items,
    ).validated()


def _workspace_type(args: argparse.Namespace):
    if _implementation(args) == "production-topology":
        from django_ray.workflow_progress_preparation import SQLitePreparationWorkspace

        return SQLitePreparationWorkspace

    from scripts.workflow_progress_preparation_prototype import SQLitePreparationWorkspace

    return SQLitePreparationWorkspace


def _hold_open_worker(args: argparse.Namespace) -> int:
    if args.worker_workspace_root is None or args.worker_ready is None or args.worker_nonce is None:
        raise ValueError("hold-open worker requires workspace, readiness, and nonce values")
    workspace = _workspace_type(args)(
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

    config = _workspace_config(args)
    implementation = _implementation(args)
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
    workspace = _workspace_type(args)(
        config,
        parent_directory=args.worker_workspace_root,
    )
    tracemalloc.start()
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    bounded_phase_tracemalloc_current_bytes: int | None = None
    bounded_phase_tracemalloc_peak_bytes: int | None = None
    bounded_phase_rss_measurement: dict[str, Any] | None = None
    with _RssSampler() as rss:
        detail = None
        with workspace:
            pragmas = workspace.sqlite_pragmas()
            topology = workspace.prepare_topology(
                identity,
                1,
                _nodes(node_count),
                _edges(node_count, args.worker_profile, args.high_edge_factor),
            )
            if implementation == "production-topology":
                query_plans = workspace.retained_query_plans()
                spill_peak_bytes = workspace.spill_peak_bytes
                spill_items = workspace.spill_items
                (
                    bounded_phase_tracemalloc_current_bytes,
                    bounded_phase_tracemalloc_peak_bytes,
                ) = tracemalloc.get_traced_memory()
                bounded_phase_rss_measurement = rss.checkpoint(scope="bounded topology preparation")
                workspace.prepare_legacy_detachment(topology)
            else:
                detail = workspace.prepare_detail(_details(node_count), topology=topology)
                query_plans = workspace.retained_query_plans()
                spill_peak_bytes = workspace.spill_peak_bytes
                spill_items = workspace.spill_items
        if implementation == "production-topology":
            topology = workspace.detach_legacy_topology(topology)
    cpu_seconds = time.process_time() - cpu_started
    wall_seconds = time.perf_counter() - wall_started
    _, tracemalloc_peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_measurement = rss.report()
    observed_detail = None if detail is None else detail.observed_count
    retained_detail = None if detail is None else len(detail.records)
    detail_encoded_bytes = None if detail is None else detail.encoded_bytes
    detail_decoded_bytes = None if detail is None else detail.decoded_bytes
    detail_truncation_reasons = [] if detail is None else list(detail.truncation_reasons)
    report = {
        "implementation": implementation,
        "profile": args.worker_profile,
        "observed_nodes": node_count,
        "observed_edges": edge_count,
        "observed_detail": observed_detail,
        "retained_nodes": topology.retained_node_count,
        "retained_edges": topology.retained_edge_count,
        "retained_detail": retained_detail,
        "topology_pages": len(topology.pages),
        "topology_encoded_bytes": topology.encoded_bytes,
        "topology_decoded_bytes": topology.decoded_bytes,
        "detail_encoded_bytes": detail_encoded_bytes,
        "detail_decoded_bytes": detail_decoded_bytes,
        "legacy_observed_node_ids": (
            len(topology.observed_node_ids) if implementation == "production-topology" else None
        ),
        "manifest_digest": topology.manifest_digest,
        "truncation_reasons": sorted(
            set(topology.truncation_reasons) | set(detail_truncation_reasons)
        ),
        "topology_truncation_reasons": list(topology.truncation_reasons),
        "detail_truncation_reasons": detail_truncation_reasons,
        "wall_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "bounded_phase_tracemalloc_current_bytes": (bounded_phase_tracemalloc_current_bytes),
        "bounded_phase_tracemalloc_peak_bytes": bounded_phase_tracemalloc_peak_bytes,
        "bounded_phase_peak_rss_bytes": (
            None
            if bounded_phase_rss_measurement is None
            else bounded_phase_rss_measurement["peak_bytes"]
        ),
        "bounded_phase_rss_measurement": bounded_phase_rss_measurement,
        "tracemalloc_peak_bytes": tracemalloc_peak_bytes,
        "peak_rss_bytes": rss_measurement["peak_bytes"],
        "end_to_end_tracemalloc_peak_bytes": tracemalloc_peak_bytes,
        "end_to_end_peak_rss_bytes": rss_measurement["peak_bytes"],
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
        "resident_contract": (
            _PRODUCTION_RESIDENT_CONTRACT
            if implementation == "production-topology"
            else _PROTOTYPE_RESIDENT_CONTRACT
        ),
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
        "--implementation",
        _implementation(args),
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


def _required_mapping(
    value: Any,
    required_fields: frozenset[str],
    *,
    message: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not required_fields.issubset(value):
        raise RuntimeError(message)
    return value


def _is_sha256(value: Any) -> bool:
    return bool(
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_nonnegative_number(value: Any) -> bool:
    return bool(type(value) in {int, float} and math.isfinite(float(value)) and value >= 0)


def _is_optional_nonnegative_int(value: Any) -> bool:
    return value is None or (type(value) is int and value >= 0)


def _validate_filesystem_evidence(value: Any) -> tuple[Any, ...]:
    filesystem = _required_mapping(
        value,
        _FILESYSTEM_REQUIRED_FIELDS,
        message="preparation benchmark filesystem evidence is incomplete",
    )
    identity = filesystem["identity_sha256"]
    allocation = filesystem["allocation_block_bytes"]
    if (
        not _is_sha256(identity)
        or type(filesystem["identity_method"]) is not str
        or filesystem["identity_method"] != "sha256(platform, st_dev)"
        or type(filesystem["filesystem_type"]) is not str
        or not filesystem["filesystem_type"]
        or (allocation is not None and (type(allocation) is not int or allocation <= 0))
    ):
        raise RuntimeError("preparation benchmark filesystem evidence is invalid")
    return (
        identity,
        filesystem["identity_method"],
        filesystem["filesystem_type"],
        allocation,
    )


def _validate_rss_measurement(
    value: Any,
    *,
    label: str,
    phase: str,
) -> int | None:
    measurement = _required_mapping(
        value,
        _RSS_REQUIRED_FIELDS,
        message=f"preparation benchmark {label} RSS evidence is incomplete",
    )
    peak = measurement["peak_bytes"]
    optional_int_fields = (
        "baseline_bytes",
        "baseline_current_bytes",
        "baseline_high_water_bytes",
        "sampled_peak_bytes",
        "process_high_water_bytes",
    )
    if not _is_optional_nonnegative_int(peak) or (peak is not None and peak <= 0):
        raise RuntimeError(f"preparation benchmark {label} RSS peak is invalid")
    if any(not _is_optional_nonnegative_int(measurement[name]) for name in optional_int_fields):
        raise RuntimeError(f"preparation benchmark {label} RSS evidence is invalid")
    interval = measurement["sample_interval_seconds"]
    if interval is not None and (not _is_nonnegative_number(interval) or float(interval) <= 0):
        raise RuntimeError(f"preparation benchmark {label} RSS interval is invalid")
    method = measurement["method"]
    scope = measurement["scope"]
    if (
        type(method) is not str
        or method
        not in {
            "resource.getrusage(RUSAGE_SELF).ru_maxrss",
            "psutil.Process.memory_info().rss sampling",
            "unavailable",
        }
        or type(scope) is not str
        or not scope
    ):
        raise RuntimeError(f"preparation benchmark {label} RSS evidence is invalid")
    expected_scopes = {
        "end-to-end": {
            "resource.getrusage(RUSAGE_SELF).ru_maxrss": (
                "fresh child process lifetime high-water"
            ),
            "psutil.Process.memory_info().rss sampling": "preparation measurement window",
            "unavailable": "unavailable",
        },
        "bounded-phase": {
            "resource.getrusage(RUSAGE_SELF).ru_maxrss": (
                "fresh child process high-water through bounded topology preparation"
            ),
            "psutil.Process.memory_info().rss sampling": (
                "measurement window through bounded topology preparation"
            ),
            "unavailable": "unavailable",
        },
    }
    if phase not in expected_scopes or scope != expected_scopes[phase][method]:
        raise RuntimeError(f"preparation benchmark {label} RSS scope is inconsistent")
    if interval is not None and (
        type(interval) is not float or interval != RSS_SAMPLE_INTERVAL_SECONDS
    ):
        raise RuntimeError(f"preparation benchmark {label} RSS interval is inconsistent")
    baseline_current = measurement["baseline_current_bytes"]
    baseline_high_water = measurement["baseline_high_water_bytes"]
    sampled_peak = measurement["sampled_peak_bytes"]
    process_high_water = measurement["process_high_water_bytes"]
    if (
        baseline_current is not None
        and sampled_peak is not None
        and sampled_peak < baseline_current
    ) or (
        baseline_high_water is not None
        and process_high_water is not None
        and process_high_water < baseline_high_water
    ):
        raise RuntimeError(f"preparation benchmark {label} RSS peaks are inconsistent")
    if method == "resource.getrusage(RUSAGE_SELF).ru_maxrss" and (
        peak != process_high_water or measurement["baseline_bytes"] != baseline_high_water
    ):
        raise RuntimeError(f"preparation benchmark {label} RSS evidence is inconsistent")
    if method == "psutil.Process.memory_info().rss sampling" and (
        peak != sampled_peak
        or measurement["baseline_bytes"] != baseline_current
        or interval is None
    ):
        raise RuntimeError(f"preparation benchmark {label} RSS evidence is inconsistent")
    if method == "unavailable" and any(
        measurement[name] is not None
        for name in (
            "peak_bytes",
            "baseline_bytes",
            "sampled_peak_bytes",
            "process_high_water_bytes",
        )
    ):
        raise RuntimeError(f"preparation benchmark {label} RSS evidence is inconsistent")
    return peak


def _validate_worker_memory_evidence(
    report: dict[str, Any],
    *,
    implementation: str,
) -> None:
    """Fail a scenario whose phase or end-to-end memory evidence is incomplete."""
    required_fields = frozenset(
        {
            "tracemalloc_peak_bytes",
            "peak_rss_bytes",
            "end_to_end_tracemalloc_peak_bytes",
            "end_to_end_peak_rss_bytes",
            "bounded_phase_tracemalloc_current_bytes",
            "bounded_phase_tracemalloc_peak_bytes",
            "bounded_phase_peak_rss_bytes",
            "bounded_phase_rss_measurement",
            "rss_measurement",
        }
    )
    if not required_fields.issubset(report):
        raise RuntimeError("preparation benchmark v2 memory evidence is incomplete")
    end_tracemalloc = report["tracemalloc_peak_bytes"]
    end_rss = report["peak_rss_bytes"]
    if type(end_tracemalloc) is not int or end_tracemalloc <= 0:
        raise RuntimeError("preparation benchmark end-to-end tracemalloc peak is invalid")
    if end_rss is not None and (type(end_rss) is not int or end_rss <= 0):
        raise RuntimeError("preparation benchmark end-to-end RSS peak is invalid")
    if (
        type(report["end_to_end_tracemalloc_peak_bytes"]) is not type(end_tracemalloc)
        or report["end_to_end_tracemalloc_peak_bytes"] != end_tracemalloc
        or type(report["end_to_end_peak_rss_bytes"]) is not type(end_rss)
        or report["end_to_end_peak_rss_bytes"] != end_rss
    ):
        raise RuntimeError("preparation benchmark end-to-end memory evidence is inconsistent")
    measured_end_rss = _validate_rss_measurement(
        report["rss_measurement"],
        label="end-to-end",
        phase="end-to-end",
    )
    if measured_end_rss != end_rss:
        raise RuntimeError("preparation benchmark end-to-end RSS evidence is inconsistent")
    if implementation != "production-topology":
        bounded_fields = (
            "bounded_phase_tracemalloc_current_bytes",
            "bounded_phase_tracemalloc_peak_bytes",
            "bounded_phase_peak_rss_bytes",
            "bounded_phase_rss_measurement",
        )
        if any(report[name] is not None for name in bounded_fields):
            raise RuntimeError(
                "prototype preparation report unexpectedly contains production phase evidence"
            )
        return

    bounded_current = report["bounded_phase_tracemalloc_current_bytes"]
    bounded_peak = report["bounded_phase_tracemalloc_peak_bytes"]
    bounded_rss = report["bounded_phase_peak_rss_bytes"]
    bounded_measurement = report["bounded_phase_rss_measurement"]
    if (
        type(bounded_current) is not int
        or bounded_current <= 0
        or type(bounded_peak) is not int
        or bounded_peak < bounded_current
        or end_tracemalloc < bounded_peak
    ):
        raise RuntimeError(
            "production preparation benchmark bounded tracemalloc evidence is invalid"
        )
    measured_bounded_rss = _validate_rss_measurement(
        bounded_measurement,
        label="bounded-phase",
        phase="bounded-phase",
    )
    if (
        measured_bounded_rss != bounded_rss
        or "bounded topology preparation" not in bounded_measurement["scope"]
    ):
        raise RuntimeError("production preparation benchmark bounded RSS evidence is invalid")
    if bounded_rss is not None and (type(bounded_rss) is not int or bounded_rss <= 0):
        raise RuntimeError("production preparation benchmark bounded RSS peak is invalid")
    if bounded_rss is not None and end_rss is not None and end_rss < bounded_rss:
        raise RuntimeError("production preparation benchmark RSS peaks are not monotonic")


def _validate_cleanup_evidence(value: Any) -> None:
    cleanup = _required_mapping(
        value,
        _CLEANUP_REQUIRED_FIELDS,
        message="preparation benchmark case cleanup evidence is incomplete",
    )
    if (
        type(cleanup["worker_context"]) is not str
        or cleanup["worker_context"] != "removed"
        or cleanup["workspace_exists_after_context"] is not False
        or type(cleanup["parent_watchdog"]) is not str
        or cleanup["parent_watchdog"] != "removed"
        or cleanup["scenario_root_exists_after_parent"] is not False
    ):
        raise RuntimeError("preparation benchmark case cleanup did not succeed")


def _validate_budget_evidence(value: Any, *, implementation: str) -> dict[str, Any]:
    required = _COMMON_BUDGET_REQUIRED_FIELDS | (
        frozenset({"max_detail_items"}) if implementation == "prototype-composite" else frozenset()
    )
    budgets = _required_mapping(
        value,
        required,
        message="preparation benchmark case budget evidence is incomplete",
    )
    if any(type(budgets[name]) is not int for name in required):
        raise RuntimeError("preparation benchmark case budget evidence is invalid")
    if (
        budgets["page_bytes"] != _PREPARATION_PAGE_BYTES
        or not _PREPARATION_PAGE_BYTES <= budgets["cache_bytes"] <= _PREPARATION_CACHE_MAX_BYTES
        or budgets["cache_bytes"] % 1024
        or budgets["mmap_bytes"] != 0
        or budgets["max_spill_bytes"] > _PREPARATION_SPILL_MAX_BYTES
        or budgets["max_spill_bytes"] % budgets["page_bytes"]
        or not _PREPARATION_PAGE_BYTES
        <= budgets["control_reserve_bytes"]
        <= _PREPARATION_CONTROL_RESERVE_MAX_BYTES
        or budgets["control_reserve_bytes"] % budgets["page_bytes"]
        or budgets["max_spill_bytes"] - budgets["control_reserve_bytes"]
        < _PREPARATION_MIN_DATABASE_BYTES
        or not 1 <= budgets["max_node_items"] <= _PREPARATION_NODE_MAX_ITEMS
        or not 1 <= budgets["max_edge_items"] <= _PREPARATION_EDGE_MAX_ITEMS
        or not 1 <= budgets["batch_max_items"] <= _PREPARATION_BATCH_MAX_ITEMS
        or not _PREPARATION_PAGE_BYTES
        <= budgets["batch_max_decoded_bytes"]
        <= _PREPARATION_BATCH_MAX_DECODED_BYTES
        or (
            implementation == "prototype-composite"
            and not 1 <= budgets["max_detail_items"] <= _PREPARATION_DETAIL_MAX_ITEMS
        )
    ):
        raise RuntimeError("preparation benchmark case budget evidence is invalid")
    return {name: budgets[name] for name in required}


def _validate_output_limit_evidence(value: Any) -> dict[str, Any]:
    limits = _required_mapping(
        value,
        _V1_OUTPUT_LIMIT_REQUIRED_FIELDS,
        message="preparation benchmark V1 output-limit evidence is incomplete",
    )
    if (
        type(limits["limits_profile"]) is not str
        or limits["storage_protocol_version"] != 1
        or limits["limits_profile"] != "v1"
    ):
        raise RuntimeError("preparation benchmark V1 output-limit profile is invalid")
    integer_fields = _V1_OUTPUT_LIMIT_REQUIRED_FIELDS - frozenset({"limits_profile"})
    if any(type(limits[name]) is not int or limits[name] <= 0 for name in integer_fields):
        raise RuntimeError("preparation benchmark V1 output-limit evidence is invalid")
    if (
        limits["topology_page_max_items"]
        > max(limits["topology_node_max_items"], limits["topology_edge_max_items"])
        or limits["topology_page_max_encoded_bytes"] > limits["topology_max_encoded_bytes"]
        or limits["topology_page_max_decoded_bytes"] > limits["topology_max_decoded_bytes"]
        or limits["topology_manifest_max_encoded_bytes"] > limits["topology_max_encoded_bytes"]
        or limits["record_max_encoded_bytes"]
        > min(
            limits["topology_page_max_encoded_bytes"],
            limits["detail_max_encoded_bytes"],
        )
        or limits["topology_max_encoded_bytes"] + limits["detail_max_encoded_bytes"]
        > limits["combined_max_encoded_bytes"]
        or limits["topology_max_decoded_bytes"] + limits["detail_max_decoded_bytes"]
        > limits["combined_max_decoded_bytes"]
        or limits["metrics_max_encoded_bytes"] > limits["record_max_encoded_bytes"]
        or limits["event_max_encoded_bytes"] > limits["record_max_encoded_bytes"]
    ):
        raise RuntimeError("preparation benchmark V1 output limits are inconsistent")
    return {name: limits[name] for name in _V1_OUTPUT_LIMIT_REQUIRED_FIELDS}


def _validate_sqlite_evidence(
    value: Any,
    *,
    budgets: dict[str, Any],
) -> None:
    pragmas = _required_mapping(
        value,
        _SQLITE_PRAGMA_REQUIRED_FIELDS,
        message="preparation benchmark SQLite evidence is incomplete",
    )
    expected = {
        "page_size": budgets["page_bytes"],
        "cache_size": -budgets["cache_bytes"] // 1024,
        "mmap_size": budgets["mmap_bytes"],
        "temp_store": 2,
        "journal_mode": "off",
        "synchronous": 0,
        "locking_mode": "exclusive",
        "foreign_keys": 1,
        "trusted_schema": 0,
        "max_page_count": (budgets["max_spill_bytes"] - budgets["control_reserve_bytes"])
        // budgets["page_bytes"],
    }
    if any(type(pragmas[name]) is not type(expected[name]) for name in expected) or any(
        pragmas[name] != expected[name] for name in expected
    ):
        raise RuntimeError("preparation benchmark SQLite evidence is inconsistent")


def _validate_query_plan_evidence(value: Any, *, implementation: str) -> None:
    if not isinstance(value, list) or any(type(plan) is not str or not plan for plan in value):
        raise RuntimeError("preparation benchmark query-plan evidence is incomplete")
    normalized = tuple(" ".join(plan.upper().split()) for plan in value)
    expected = (
        "SCAN NODES",
        "SCAN E",
        "SEARCH SOURCE_NODE USING PRIMARY KEY (NODE_ID=?)",
        "SEARCH TARGET_NODE USING PRIMARY KEY (NODE_ID=?)",
        "SCAN NODES" if implementation == "production-topology" else "SCAN DETAIL",
    )
    if normalized != expected:
        raise RuntimeError("preparation benchmark query-plan evidence is inconsistent")


def _validate_environment_evidence(value: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    environment = _required_mapping(
        value,
        _ENVIRONMENT_REQUIRED_FIELDS,
        message="preparation benchmark environment evidence is incomplete",
    )
    for name in ("platform", "python", "python_implementation", "sqlite", "django", "django_ray"):
        if type(environment[name]) is not str or not environment[name]:
            raise RuntimeError("preparation benchmark environment evidence is invalid")
    if type(environment["pid"]) is not int or environment["pid"] <= 0:
        raise RuntimeError("preparation benchmark environment evidence is invalid")
    filesystem = _validate_filesystem_evidence(environment["filesystem"])
    stable_environment = tuple(
        environment[name]
        for name in (
            "platform",
            "python",
            "python_implementation",
            "sqlite",
            "django",
            "django_ray",
        )
    )
    return stable_environment, filesystem


def _validate_truncation_reasons(value: Any, *, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(type(reason) is not str or not reason for reason in value)
        or value != sorted(set(value))
    ):
        raise RuntimeError(f"preparation benchmark {label} truncation evidence is invalid")
    return value


def _validate_case(
    value: Any,
    *,
    implementation: str,
) -> tuple[
    tuple[int, str],
    dict[str, Any],
    dict[str, Any],
    tuple[Any, ...],
    tuple[Any, ...],
    int | None,
]:
    case = _required_mapping(
        value,
        _CASE_REQUIRED_FIELDS,
        message="preparation benchmark case evidence is incomplete",
    )
    if type(case["implementation"]) is not str or case["implementation"] != implementation:
        raise RuntimeError("preparation benchmark case implementation is inconsistent")
    observed_nodes = case["observed_nodes"]
    observed_edges = case["observed_edges"]
    profile = case["profile"]
    count_fields = (
        "observed_nodes",
        "observed_edges",
        "retained_nodes",
        "retained_edges",
        "topology_pages",
        "topology_encoded_bytes",
        "topology_decoded_bytes",
        "spill_peak_bytes",
        "spill_items",
    )
    if (
        any(type(case[name]) is not int or case[name] < 0 for name in count_fields)
        or observed_nodes <= 0
        or case["topology_pages"] <= 0
        or case["topology_encoded_bytes"] <= 0
        or case["topology_decoded_bytes"] <= 0
        or case["spill_peak_bytes"] <= 0
        or type(profile) is not str
        or profile not in DEFAULT_PROFILES
    ):
        raise RuntimeError("preparation benchmark case cardinality evidence is invalid")
    if case["retained_nodes"] > observed_nodes or case["retained_edges"] > observed_edges:
        raise RuntimeError("preparation benchmark retained cardinality exceeds observation")
    if profile == "sparse" and observed_edges != max(0, observed_nodes - 1):
        raise RuntimeError("preparation benchmark sparse edge cardinality is inconsistent")
    high_edge_factor: int | None = None
    if profile == "high-edge":
        if observed_edges <= 0 or observed_edges % observed_nodes:
            raise RuntimeError("preparation benchmark high-edge cardinality is inconsistent")
        high_edge_factor = observed_edges // observed_nodes
        if high_edge_factor <= 0 or high_edge_factor >= observed_nodes:
            raise RuntimeError("preparation benchmark high-edge factor is invalid")

    if not _is_sha256(case["manifest_digest"]):
        raise RuntimeError("preparation benchmark manifest digest is invalid")
    topology_reasons = _validate_truncation_reasons(
        case["topology_truncation_reasons"],
        label="topology",
    )
    detail_reasons = _validate_truncation_reasons(
        case["detail_truncation_reasons"],
        label="detail",
    )
    combined_reasons = _validate_truncation_reasons(
        case["truncation_reasons"],
        label="combined",
    )
    if combined_reasons != sorted(set(topology_reasons) | set(detail_reasons)):
        raise RuntimeError("preparation benchmark truncation evidence is inconsistent")
    if (
        not _is_nonnegative_number(case["wall_seconds"])
        or not _is_nonnegative_number(case["cpu_seconds"])
        or case["wall_seconds"] <= 0
        or case["cpu_seconds"] <= 0
    ):
        raise RuntimeError("preparation benchmark timing evidence is invalid")

    _validate_worker_memory_evidence(case, implementation=implementation)
    _validate_cleanup_evidence(case["cleanup"])
    budgets = _validate_budget_evidence(case["budgets"], implementation=implementation)
    limits = _validate_output_limit_evidence(case["v1_output_limits"])
    _validate_sqlite_evidence(case["sqlite_pragmas"], budgets=budgets)
    _validate_query_plan_evidence(case["query_plans"], implementation=implementation)
    environment_identity, filesystem_identity = _validate_environment_evidence(case["environment"])
    expected_resident_contract = (
        _PRODUCTION_RESIDENT_CONTRACT
        if implementation == "production-topology"
        else _PROTOTYPE_RESIDENT_CONTRACT
    )
    if (
        type(case["resident_contract"]) is not str
        or case["resident_contract"] != expected_resident_contract
    ):
        raise RuntimeError("preparation benchmark resident contract is invalid")
    if (
        observed_nodes > budgets["max_node_items"]
        or observed_edges > budgets["max_edge_items"]
        or case["spill_peak_bytes"] > budgets["max_spill_bytes"]
        or case["retained_nodes"] > limits["topology_node_max_items"]
        or case["retained_edges"] > limits["topology_edge_max_items"]
        or case["topology_encoded_bytes"] > limits["topology_max_encoded_bytes"]
        or case["topology_decoded_bytes"] > limits["topology_max_decoded_bytes"]
    ):
        raise RuntimeError("preparation benchmark case exceeds its recorded limits")

    expected_retained_nodes = min(observed_nodes, limits["topology_node_max_items"])
    if profile == "sparse":
        eligible_retained_edges = max(0, expected_retained_nodes - 1)
    elif expected_retained_nodes == observed_nodes:
        if high_edge_factor is None:
            raise AssertionError("validated high-edge case has no factor")
        eligible_retained_edges = observed_edges
    else:
        if high_edge_factor is None:
            raise AssertionError("validated high-edge case has no factor")
        retained_offsets = min(high_edge_factor, expected_retained_nodes - 1)
        eligible_retained_edges = (
            retained_offsets * expected_retained_nodes
            - retained_offsets * (retained_offsets + 1) // 2
        )
    expected_retained_edges = min(
        eligible_retained_edges,
        limits["topology_edge_max_items"],
    )
    expected_topology_reasons = sorted(
        reason
        for reason, truncated in (
            (
                "node_count_limit",
                observed_nodes > limits["topology_node_max_items"],
            ),
            (
                "edge_count_limit",
                eligible_retained_edges > limits["topology_edge_max_items"],
            ),
        )
        if truncated
    )
    if (
        case["retained_nodes"] != expected_retained_nodes
        or case["retained_edges"] != expected_retained_edges
        or topology_reasons != expected_topology_reasons
    ):
        raise RuntimeError("preparation benchmark retained topology evidence is inconsistent")
    minimum_topology_pages = (
        expected_retained_nodes + limits["topology_page_max_items"] - 1
    ) // limits["topology_page_max_items"]
    if expected_retained_edges:
        minimum_topology_pages += (
            expected_retained_edges + limits["topology_page_max_items"] - 1
        ) // limits["topology_page_max_items"]
    if (
        case["topology_pages"] < minimum_topology_pages
        or case["topology_pages"] > expected_retained_nodes + expected_retained_edges
        or case["topology_encoded_bytes"] != case["topology_decoded_bytes"]
    ):
        raise RuntimeError("preparation benchmark topology encoding evidence is inconsistent")

    if implementation == "production-topology":
        if (
            any(
                case[name] is not None
                for name in (
                    "observed_detail",
                    "retained_detail",
                    "detail_encoded_bytes",
                    "detail_decoded_bytes",
                )
            )
            or detail_reasons != []
        ):
            raise RuntimeError("production preparation benchmark detail evidence is invalid")
        if (
            type(case["legacy_observed_node_ids"]) is not int
            or case["legacy_observed_node_ids"] != observed_nodes
            or case["spill_items"] != observed_nodes + observed_edges
            or "legacy" not in case["resident_contract"]
        ):
            raise RuntimeError("production preparation compatibility evidence is inconsistent")
    else:
        detail_count_fields = (
            "observed_detail",
            "retained_detail",
            "detail_encoded_bytes",
            "detail_decoded_bytes",
        )
        if (
            any(type(case[name]) is not int or case[name] <= 0 for name in detail_count_fields)
            or case["observed_detail"] != observed_nodes
            or case["retained_detail"] != expected_retained_nodes
            or case["observed_detail"] > budgets["max_detail_items"]
            or case["retained_detail"] > limits["detail_max_items"]
            or case["detail_encoded_bytes"] > limits["detail_max_encoded_bytes"]
            or case["detail_decoded_bytes"] > limits["detail_max_decoded_bytes"]
            or case["detail_encoded_bytes"] != case["detail_decoded_bytes"]
            or case["topology_encoded_bytes"] + case["detail_encoded_bytes"]
            > limits["combined_max_encoded_bytes"]
            or case["topology_decoded_bytes"] + case["detail_decoded_bytes"]
            > limits["combined_max_decoded_bytes"]
            or detail_reasons != topology_reasons
            or case["legacy_observed_node_ids"] is not None
            or case["spill_items"] != observed_nodes + observed_edges + case["observed_detail"]
        ):
            raise RuntimeError("prototype preparation detail evidence is inconsistent")
    return (
        (observed_nodes, str(profile)),
        budgets,
        limits,
        environment_identity,
        filesystem_identity,
        high_edge_factor,
    )


def _validate_source_snapshot(value: Any) -> dict[str, Any]:
    snapshot = _required_mapping(
        value,
        _SOURCE_SNAPSHOT_REQUIRED_FIELDS,
        message="preparation benchmark source snapshot is incomplete",
    )
    revision = snapshot["revision"]
    if (
        type(revision) is not str
        or not (
            revision == "unavailable"
            or (
                len(revision) == 40
                and all(character in "0123456789abcdef" for character in revision)
            )
        )
        or type(snapshot["dirty"]) is not bool
        or (revision == "unavailable" and snapshot["dirty"] is not False)
        or not _is_sha256(snapshot["implementation_digest"])
    ):
        raise RuntimeError("preparation benchmark source snapshot is invalid")
    return {name: snapshot[name] for name in _SOURCE_SNAPSHOT_REQUIRED_FIELDS}


def _validate_forced_termination(value: Any) -> tuple[Any, ...]:
    evidence = _required_mapping(
        value,
        _FORCED_TERMINATION_REQUIRED_FIELDS,
        message="preparation benchmark forced-termination evidence is incomplete",
    )
    if (
        type(evidence["outcome"]) is not str
        or evidence["outcome"] != "forcibly-terminated"
        or evidence["readiness_observed"] is not True
        or evidence["workspace_open_before_kill"] is not True
        or type(evidence["worker_returncode"]) is not int
        or evidence["worker_returncode"] == 0
        or evidence["durable_candidate_exists_before_cleanup"] is not False
        or evidence["durable_candidate_exists_after_cleanup"] is not False
        or type(evidence["parent_watchdog"]) is not str
        or evidence["parent_watchdog"] != "removed"
        or evidence["scenario_root_exists_after_parent"] is not False
    ):
        raise RuntimeError("preparation benchmark forced-termination control failed")
    return _validate_filesystem_evidence(evidence["filesystem"])


def _validate_report(report: dict[str, Any]) -> None:
    """Validate every required v2 field while allowing additive future fields."""
    report = _required_mapping(
        report,
        _REPORT_REQUIRED_FIELDS,
        message="preparation benchmark v2 report is incomplete",
    )
    if type(report["schema_version"]) is not int or (
        report["schema_version"] != REPORT_SCHEMA_VERSION
    ):
        raise RuntimeError("preparation benchmark report schema is unsupported")

    implementation = report["implementation"]
    if type(implementation) is not str or implementation not in IMPLEMENTATIONS:
        raise RuntimeError("preparation benchmark implementation is invalid")
    if type(report["required_scale"]) is not bool:
        raise RuntimeError("preparation benchmark required-scale marker is invalid")
    try:
        parsed_timestamp = (
            time.strptime(report["created_at"], "%Y-%m-%dT%H:%M:%SZ")
            if type(report["created_at"]) is str
            else None
        )
        timestamp_valid = bool(
            parsed_timestamp is not None
            and time.strftime("%Y-%m-%dT%H:%M:%SZ", parsed_timestamp) == report["created_at"]
        )
    except (TypeError, ValueError):
        timestamp_valid = False
    if not timestamp_valid:
        raise RuntimeError("preparation benchmark creation timestamp is invalid")
    if type(report["command"]) is not str or not report["command"]:
        raise RuntimeError("preparation benchmark command evidence is invalid")
    expected_memory_contract = (
        _PRODUCTION_MEMORY_EVIDENCE_CONTRACT
        if implementation == "production-topology"
        else _PROTOTYPE_MEMORY_EVIDENCE_CONTRACT
    )
    if (
        type(report["memory_evidence_contract"]) is not str
        or report["memory_evidence_contract"] != expected_memory_contract
        or type(report["cleanup_contract"]) is not str
        or report["cleanup_contract"] != _CLEANUP_CONTRACT
    ):
        raise RuntimeError("preparation benchmark evidence contract is invalid")

    source_before = _validate_source_snapshot(report["source_snapshot_before"])
    source_after = _validate_source_snapshot(report["source_snapshot_after"])
    if source_before != source_after:
        raise RuntimeError("preparation benchmark source snapshots are inconsistent")
    if (
        type(report["source_revision"]) is not str
        or report["source_revision"] != source_before["revision"]
        or type(report["source_dirty"]) is not bool
        or report["source_dirty"] is not source_before["dirty"]
        or not _is_sha256(report["implementation_digest"])
        or report["implementation_digest"] != source_before["implementation_digest"]
    ):
        raise RuntimeError("preparation benchmark source identity is inconsistent")

    cases = report["cases"]
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("preparation benchmark cases are incomplete")
    observed_matrix: list[tuple[int, str]] = []
    first_budgets: dict[str, Any] | None = None
    first_limits: dict[str, Any] | None = None
    environment_identities: set[tuple[Any, ...]] = set()
    filesystem_identities: set[tuple[Any, ...]] = set()
    high_edge_factors: set[int] = set()
    for case in cases:
        (
            coordinates,
            budgets,
            limits,
            environment_identity,
            filesystem_identity,
            high_edge_factor,
        ) = _validate_case(case, implementation=implementation)
        observed_matrix.append(coordinates)
        environment_identities.add(environment_identity)
        filesystem_identities.add(filesystem_identity)
        if high_edge_factor is not None:
            high_edge_factors.add(high_edge_factor)
        if first_budgets is None:
            first_budgets = budgets
            first_limits = limits
        elif budgets != first_budgets or limits != first_limits:
            raise RuntimeError("preparation benchmark case limits are inconsistent")

    forced_filesystem = _validate_forced_termination(report["forced_termination"])
    filesystem_identities.add(forced_filesystem)
    if len(environment_identities) != 1:
        raise RuntimeError("preparation benchmark environment changed during run")
    if len(filesystem_identities) != 1:
        raise RuntimeError("preparation benchmark filesystem identity changed during run")
    if len(high_edge_factors) > 1:
        raise RuntimeError("preparation benchmark high-edge factor changed during run")
    if len(observed_matrix) != len(set(observed_matrix)):
        raise RuntimeError("preparation benchmark case matrix contains duplicates")
    if report["required_scale"]:
        required_matrix = {
            (nodes, profile) for nodes in REQUIRED_SCALE_NODES for profile in DEFAULT_PROFILES
        }
        if set(observed_matrix) != required_matrix:
            raise RuntimeError("preparation benchmark required-scale matrix is incomplete")
        if first_limits is None or any(
            case["observed_edges"] <= first_limits["topology_edge_max_items"]
            for case in cases
            if case["profile"] == "high-edge"
        ):
            raise RuntimeError("preparation benchmark required high-edge scale is insufficient")


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
        _validate_worker_memory_evidence(
            report,
            implementation=_implementation(args),
        )
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
        "implementation": _implementation(args),
        "required_scale": args.required_scale,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_revision": source_before["revision"],
        "source_dirty": source_before["dirty"],
        "implementation_digest": source_before["implementation_digest"],
        "source_snapshot_before": source_before,
        "source_snapshot_after": source_after,
        "command": " ".join(sys.argv),
        "cases": cases,
        "forced_termination": forced_termination,
        "memory_evidence_contract": (
            _PRODUCTION_MEMORY_EVIDENCE_CONTRACT
            if _implementation(args) == "production-topology"
            else _PROTOTYPE_MEMORY_EVIDENCE_CONTRACT
        ),
        "cleanup_contract": _CLEANUP_CONTRACT,
    }
    _validate_report(report)
    _write_report(report, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
