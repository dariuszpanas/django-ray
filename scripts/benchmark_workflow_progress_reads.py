"""Measure bounded workflow-progress read services on PostgreSQL.

The benchmark writes synthetic workflow progress only to the configured database
and deletes its owning task rows before exit. Run it only against a disposable
database and opt in explicitly through the environment variable documented below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OPT_IN_ENV = "DJANGO_RAY_RUN_WORKFLOW_PROGRESS_READ_BENCHMARK"
SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 512 * 1024
MAX_DECODED_RECORD_BYTES = 1024 * 1024


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dependency_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def _git_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", nargs="+", type=int, default=[1_000, 10_000, 25_000])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--database-deployment", default="disposable-postgresql-17")
    return parser.parse_args()


def _validated_args(args: argparse.Namespace) -> tuple[list[int], int, int]:
    nodes = list(args.nodes)
    if not nodes or any(type(value) is not int or not 1 <= value <= 25_000 for value in nodes):
        raise ValueError("--nodes must contain unique values from 1 through 25000")
    if len(nodes) != len(set(nodes)):
        raise ValueError("--nodes values must be unique")
    if args.repetitions <= 0 or args.warmups < 0:
        raise ValueError("--repetitions must be positive and --warmups cannot be negative")
    return nodes, int(args.repetitions), int(args.warmups)


def _allow(_execution: object) -> bool:
    return True


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _summarize(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 3),
        "p50": round(statistics.median(values), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def _plan_indexes(value: object) -> list[str]:
    indexes: set[str] = set()
    if isinstance(value, dict):
        index_name = value.get("Index Name")
        if isinstance(index_name, str):
            indexes.add(index_name)
        for child in value.values():
            indexes.update(_plan_indexes(child))
    elif isinstance(value, list):
        for child in value:
            indexes.update(_plan_indexes(child))
    return sorted(indexes)


def main() -> int:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError(f"set {OPT_IN_ENV}=1 to run this destructive benchmark")

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.postgres_settings")
    import django

    django.setup()

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from django_ray.models import (
        RayTaskExecution,
        TaskState,
        WorkflowProgressNodeDetail,
        WorkflowProgressRunStorage,
        WorkflowProgressTopologyCollection,
        WorkflowProgressTopologyManifestPage,
    )
    from django_ray.runtime.context import WorkflowRunIdentity
    from django_ray.workflow_progress_reads import (
        get_workflow_node_detail,
        get_workflow_progress_summary,
        list_workflow_node_details,
        list_workflow_topology_edges,
        list_workflow_topology_nodes,
    )
    from django_ray.workflow_progress_storage import (
        persist_workflow_progress_publication,
        prepare_workflow_progress_detail,
        prepare_workflow_progress_topology,
        stage_workflow_progress_topology,
    )
    from tests.workflow_progress_storage_helpers import (
        workflow_detail,
        workflow_node,
        workflow_node_id,
        workflow_summary,
    )

    args = _parse_args()
    node_counts, repetitions, warmups = _validated_args(args)
    if connection.vendor != "postgresql":
        raise RuntimeError("workflow read benchmark requires PostgreSQL")

    def measure(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        for _ in range(warmups):
            operation()
        wall_samples: list[float] = []
        database_samples: list[float] = []
        select_counts: list[int] = []
        total_counts: list[int] = []
        response: dict[str, Any] | None = None
        for _ in range(repetitions):
            started = perf_counter()
            with CaptureQueriesContext(connection) as queries:
                response = operation()
            wall_samples.append((perf_counter() - started) * 1_000)
            database_samples.append(
                sum(float(query["time"]) for query in queries.captured_queries) * 1_000
            )
            select_counts.append(
                sum(
                    str(query["sql"]).lstrip().upper().startswith("SELECT")
                    for query in queries.captured_queries
                )
            )
            total_counts.append(len(queries))
        assert response is not None
        encoded = _canonical_bytes(response)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise RuntimeError("workflow read response exceeds the encoded byte limit")
        items = response.get("items", [])
        decoded_record_bytes = (
            sum(len(_canonical_bytes(item)) for item in items) if isinstance(items, list) else 0
        )
        if decoded_record_bytes > MAX_DECODED_RECORD_BYTES:
            raise RuntimeError("workflow read response exceeds the decoded record limit")
        return {
            "wall_ms": _summarize(wall_samples),
            "database_reported_ms": _summarize(database_samples),
            "select_counts": sorted(set(select_counts)),
            "total_query_counts": sorted(set(total_counts)),
            "response_encoded_bytes": len(encoded),
            "decoded_record_bytes": decoded_record_bytes,
            "returned_count": int(response.get("returned_count", 0)),
            "has_next_cursor": response.get("next_cursor") is not None,
        }

    def explain(
        sql: str,
        params: list[object],
        *,
        require_rows: bool = False,
    ) -> dict[str, Any]:
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", params)
            result = cursor.fetchone()[0]
        if not isinstance(result, list) or len(result) != 1:
            raise RuntimeError("PostgreSQL returned an invalid JSON query plan")
        plan = result[0]
        root = plan.get("Plan") if isinstance(plan, dict) else None
        if require_rows and (
            not isinstance(root, dict)
            or type(root.get("Actual Rows")) is not int
            or root["Actual Rows"] < 1
        ):
            raise RuntimeError("PostgreSQL query plan did not match a persisted row")
        return {
            "indexes": _plan_indexes(plan),
            "planning_ms": plan.get("Planning Time"),
            "execution_ms": plan.get("Execution Time"),
            "plan": plan,
        }

    execution_ids: list[int] = []
    cases: list[dict[str, Any]] = []
    try:
        for case_index, node_count in enumerate(node_counts, start=1):
            run_id = str(UUID(int=node_count * 10 + case_index))
            execution = RayTaskExecution.objects.create(
                task_id=f"benchmark-workflow-progress-read-{node_count}-{uuid4().hex[:8]}",
                callable_path="benchmarks.workflow_progress.sync_resource",
                state=TaskState.RUNNING,
                attempt_number=1,
                execution_generation=1,
                workflow_run_id=run_id,
            )
            execution_ids.append(execution.pk)
            identity = WorkflowRunIdentity(
                task_execution_pk=execution.pk,
                attempt_number=1,
                execution_generation=1,
                run_id=run_id,
            )
            topology = prepare_workflow_progress_topology(
                identity,
                1,
                (workflow_node(workflow_node_id(index)) for index in range(node_count)),
                (
                    {
                        "source": workflow_node_id(index),
                        "target": workflow_node_id(index + 1),
                    }
                    for index in range(node_count - 1)
                ),
            )
            prepared_detail = prepare_workflow_progress_detail(
                (workflow_detail(workflow_node_id(index)) for index in range(node_count)),
                topology=topology,
            )
            manifest_id = stage_workflow_progress_topology(topology)
            if manifest_id is None:
                raise RuntimeError("benchmark topology lost its exact run fence")
            summary_payload = workflow_summary(
                identity,
                summary_revision=1,
                node_count=node_count,
                running_count=0,
            )
            edge_counts = summary_payload["edge_counts"]
            if not isinstance(edge_counts, dict):
                raise RuntimeError("benchmark summary edge counts are invalid")
            edge_counts.update(
                declared=node_count - 1,
                discovered=node_count - 1,
            )
            published = persist_workflow_progress_publication(
                identity,
                summary_payload,
                manifest_id=manifest_id,
                prepared_topology=topology,
                prepared_detail=prepared_detail,
            )
            if not published.accepted:
                raise RuntimeError("benchmark publication was rejected")

            with connection.cursor() as cursor:
                cursor.execute("ANALYZE")
            run_storage = WorkflowProgressRunStorage.objects.get(execution=execution)
            node_table = connection.ops.quote_name(WorkflowProgressNodeDetail._meta.db_table)
            link_table = connection.ops.quote_name(
                WorkflowProgressTopologyManifestPage._meta.db_table
            )
            middle_key = hashlib.sha256(
                workflow_node_id(node_count // 2).encode("utf-8")
            ).hexdigest()
            node_collection = WorkflowProgressTopologyCollection.NODE.value
            if not WorkflowProgressTopologyManifestPage.objects.filter(
                manifest_id=manifest_id,
                collection=node_collection,
                page_index=1,
            ).exists():
                raise RuntimeError("benchmark topology plan query does not match a node page")
            plans = {
                "detail_keyset": explain(
                    f"SELECT id FROM {node_table} "
                    "WHERE run_storage_id = %s AND node_key > %s "
                    "ORDER BY node_key LIMIT 100",
                    [run_storage.pk, middle_key],
                ),
                "detail_state_keyset": explain(
                    f"SELECT id FROM {node_table} "
                    "WHERE run_storage_id = %s AND state = %s AND node_key > %s "
                    "ORDER BY node_key LIMIT 100",
                    [run_storage.pk, "PENDING", middle_key],
                ),
                "single_node": explain(
                    f"SELECT id FROM {node_table} WHERE run_storage_id = %s AND node_key = %s",
                    [run_storage.pk, middle_key],
                ),
                "topology_page": explain(
                    f"SELECT page_id FROM {link_table} "
                    "WHERE manifest_id = %s AND collection = %s AND page_index = %s",
                    [manifest_id, node_collection, 1],
                    require_rows=True,
                ),
            }

            node_first = list_workflow_topology_nodes(execution, authorize=_allow, limit=100)
            edge_first = list_workflow_topology_edges(execution, authorize=_allow, limit=100)
            detail_first = list_workflow_node_details(execution, authorize=_allow, limit=100)
            operations: dict[str, Callable[[], dict[str, Any]]] = {
                "summary": lambda execution=execution: get_workflow_progress_summary(
                    execution,
                    authorize=_allow,
                ),
                "topology_nodes_first": lambda execution=execution: list_workflow_topology_nodes(
                    execution,
                    authorize=_allow,
                    limit=100,
                ),
                "topology_edges_first": lambda execution=execution: list_workflow_topology_edges(
                    execution,
                    authorize=_allow,
                    limit=100,
                ),
                "detail_first": lambda execution=execution: list_workflow_node_details(
                    execution,
                    authorize=_allow,
                    limit=100,
                ),
                "detail_state_first": lambda execution=execution: list_workflow_node_details(
                    execution,
                    authorize=_allow,
                    state="PENDING",
                    limit=100,
                ),
                "single_middle": lambda execution=execution, node_count=node_count: (
                    get_workflow_node_detail(
                        execution,
                        workflow_node_id(node_count // 2),
                        authorize=_allow,
                    )
                ),
            }
            if node_first.get("next_cursor") is not None:
                operations["topology_nodes_next"] = (
                    lambda execution=execution, cursor=node_first["next_cursor"]: (
                        list_workflow_topology_nodes(
                            execution,
                            authorize=_allow,
                            cursor=cursor,
                            limit=100,
                        )
                    )
                )
            if edge_first.get("next_cursor") is not None:
                operations["topology_edges_next"] = (
                    lambda execution=execution, cursor=edge_first["next_cursor"]: (
                        list_workflow_topology_edges(
                            execution,
                            authorize=_allow,
                            cursor=cursor,
                            limit=100,
                        )
                    )
                )
            if detail_first.get("next_cursor") is not None:
                operations["detail_next"] = (
                    lambda execution=execution, cursor=detail_first["next_cursor"]: (
                        list_workflow_node_details(
                            execution,
                            authorize=_allow,
                            cursor=cursor,
                            limit=100,
                        )
                    )
                )

            cases.append(
                {
                    "case_index": case_index,
                    "retained_nodes": topology.retained_node_count,
                    "retained_edges": topology.retained_edge_count,
                    "plans": plans,
                    "operations": {
                        name: measure(operation) for name, operation in operations.items()
                    },
                }
            )
    finally:
        if execution_ids:
            RayTaskExecution.objects.filter(pk__in=execution_ids).delete()

    with connection.cursor() as cursor:
        cursor.execute("SELECT version()")
        database_version = str(cursor.fetchone()[0])
    reads_path = ROOT / "src" / "django_ray" / "workflow_progress_reads.py"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "django-ray-workflow-progress-bounded-reads",
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "django_version": django.get_version(),
            "django_ray_version": _dependency_version("django-ray"),
            "psycopg_version": _dependency_version("psycopg"),
            "database_vendor": connection.vendor,
            "database_version": database_version,
            "database_deployment": args.database_deployment,
            "source_revision": _git_revision(),
            "source_worktree_dirty": _git_dirty(),
            "reads_implementation_sha256": _sha256(reads_path),
            "benchmark_implementation_sha256": _sha256(Path(__file__)),
        },
        "method": {
            "warmups": warmups,
            "repetitions": repetitions,
            "timer": "time.perf_counter",
            "sql_count": "Django CaptureQueriesContext around each package read",
            "response_encoding": "canonical compact UTF-8 JSON",
            "cleanup": "owning synthetic RayTaskExecution rows deleted before exit",
        },
        "limits": {
            "response_encoded_bytes": MAX_RESPONSE_BYTES,
            "decoded_record_bytes": MAX_DECODED_RECORD_BYTES,
        },
        "cases": cases,
        "limitations": [
            "This local benchmark is scale evidence, not a production latency SLO.",
            "Dataset publication is excluded from read measurements.",
            "The database connection is local and does not model production network latency.",
            "PostgreSQL reported query durations use Django's coarse captured timings.",
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(serialized)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8", newline="\n")
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
