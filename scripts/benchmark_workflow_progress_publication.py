"""Measure the normalized workflow-progress publication path on PostgreSQL.

This opt-in benchmark writes only synthetic records to the configured Django
database and deletes the owning task rows before exiting. Run it only against a
disposable database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OPT_IN_ENV = "DJANGO_RAY_RUN_WORKFLOW_PROGRESS_PUBLICATION_BENCHMARK"
SCHEMA_VERSION = 1


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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", nargs="+", type=int, default=[10_000, 25_000])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--database-deployment", default="disposable-postgresql-17")
    return parser.parse_args()


def _validated_nodes(values: list[int], *, maximum: int) -> list[int]:
    if not values or any(value <= 0 or value > maximum for value in values):
        raise ValueError(f"--nodes must contain values from 1 through {maximum}")
    if len(values) != len(set(values)):
        raise ValueError("--nodes values must be unique")
    return values


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
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologyManifestPage,
        WorkflowProgressTopologyPage,
    )
    from django_ray.runtime.context import WorkflowRunIdentity
    from django_ray.workflow_progress_storage import (
        persist_workflow_progress_publication,
        prepare_workflow_progress_detail,
        prepare_workflow_progress_node_detail,
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
    observed_node_counts = _validated_nodes(args.nodes, maximum=1_000_000)
    if connection.vendor != "postgresql":
        raise RuntimeError("workflow publication benchmark requires PostgreSQL")

    storage_tables = (
        WorkflowProgressRunStorage._meta.db_table,
        WorkflowProgressTopologyManifest._meta.db_table,
        WorkflowProgressTopologyPage._meta.db_table,
        WorkflowProgressTopologyManifestPage._meta.db_table,
        WorkflowProgressNodeDetail._meta.db_table,
    )

    def wal_lsn() -> str:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_current_wal_insert_lsn()")
            return str(cursor.fetchone()[0])

    def wal_bytes(start_lsn: str, end_lsn: str) -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_wal_lsn_diff(%s, %s)", [end_lsn, start_lsn])
            return int(cursor.fetchone()[0])

    def relation_size_bytes() -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT SUM(pg_total_relation_size(table_name::regclass)) "
                "FROM unnest(%s::text[]) AS table_name",
                [list(storage_tables)],
            )
            return int(cursor.fetchone()[0])

    with connection.cursor() as cursor:
        cursor.execute("SELECT version()")
        database_version = str(cursor.fetchone()[0])

    execution_ids: list[int] = []
    cases: list[dict[str, Any]] = []
    try:
        for case_index, node_count in enumerate(observed_node_counts, start=1):
            run_id = str(UUID(int=node_count))
            execution = RayTaskExecution.objects.create(
                task_id=(f"benchmark-workflow-progress-publication-{node_count}-{uuid4().hex[:8]}"),
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

            prepared_started = perf_counter()
            topology = prepare_workflow_progress_topology(
                identity,
                1,
                (workflow_node(workflow_node_id(index)) for index in range(node_count)),
                (),
            )
            prepared_detail = prepare_workflow_progress_detail(
                (workflow_detail(workflow_node_id(index)) for index in range(node_count)),
                topology=topology,
            )
            initial_prepare_ms = (perf_counter() - prepared_started) * 1_000

            relation_before = relation_size_bytes()
            initial_wal_start = wal_lsn()
            initial_started = perf_counter()
            manifest_id = stage_workflow_progress_topology(topology)
            if manifest_id is None:
                raise RuntimeError("initial topology staging lost its exact run fence")
            initial_result = persist_workflow_progress_publication(
                identity,
                workflow_summary(
                    identity,
                    summary_revision=1,
                    node_count=node_count,
                    running_count=0,
                ),
                manifest_id=manifest_id,
                prepared_topology=topology,
                prepared_detail=prepared_detail,
            )
            initial_publication_ms = (perf_counter() - initial_started) * 1_000
            if not initial_result.accepted:
                raise RuntimeError("initial publication was rejected")
            initial_wal_end = wal_lsn()
            relation_after_initial = relation_size_bytes()
            del prepared_detail

            sparse_prepare_started = perf_counter()
            retained_node_count = topology.retained_node_count
            if retained_node_count <= 0:
                raise RuntimeError("benchmark topology did not retain a sparse-update node")
            changed_index = retained_node_count // 2
            changed = prepare_workflow_progress_node_detail(
                workflow_detail(workflow_node_id(changed_index), state="RUNNING"),
                identity=identity,
            )
            sparse_prepare_ms = (perf_counter() - sparse_prepare_started) * 1_000
            sparse_wal_start = wal_lsn()
            sparse_started = perf_counter()
            with CaptureQueriesContext(connection) as queries:
                sparse_result = persist_workflow_progress_publication(
                    identity,
                    workflow_summary(
                        identity,
                        summary_revision=2,
                        node_count=node_count,
                        running_count=1,
                    ),
                    manifest_id=manifest_id,
                    prepared_topology=topology,
                    detail_records=(changed,),
                )
            sparse_publication_ms = (perf_counter() - sparse_started) * 1_000
            sparse_wal_end = wal_lsn()
            relation_after_sparse = relation_size_bytes()
            if not sparse_result.accepted or sparse_result.changed_node_count != 1:
                raise RuntimeError("sparse publication did not change exactly one node")

            run_storage = WorkflowProgressRunStorage.objects.get(execution=execution)
            row_count = WorkflowProgressNodeDetail.objects.filter(run_storage=run_storage).count()
            if row_count != retained_node_count:
                raise RuntimeError("published normalized row count is incomplete")
            sql_values = [str(query["sql"]) for query in queries]
            sql_upper = "\n".join(value.upper() for value in sql_values)
            page_table = WorkflowProgressTopologyPage._meta.db_table.upper()
            topology_payload_reads = sum(
                page_table in value.upper() and '"PAYLOAD"' in value.upper()
                for value in sql_values
                if value.lstrip().upper().startswith("SELECT")
            )
            if "COUNT(" in sql_upper or "SUM(" in sql_upper or topology_payload_reads:
                raise RuntimeError("sparse publication performed an unbounded storage read")

            cases.append(
                {
                    "case_index": case_index,
                    "observed_nodes": node_count,
                    "retained_nodes": retained_node_count,
                    "topology_pages": len(topology.pages),
                    "topology_encoded_bytes": topology.encoded_bytes,
                    "detail_encoded_bytes": run_storage.detail_encoded_bytes,
                    "initial_prepare_ms": round(initial_prepare_ms, 3),
                    "initial_publication_ms": round(initial_publication_ms, 3),
                    "initial_wal_bytes": wal_bytes(initial_wal_start, initial_wal_end),
                    "initial_relation_delta_bytes": (relation_after_initial - relation_before),
                    "sparse_prepare_ms": round(sparse_prepare_ms, 3),
                    "sparse_publication_ms": round(sparse_publication_ms, 3),
                    "sparse_sql_count": len(queries),
                    "sparse_database_reported_ms": round(
                        sum(float(query["time"]) for query in queries) * 1_000,
                        3,
                    ),
                    "sparse_wal_bytes": wal_bytes(sparse_wal_start, sparse_wal_end),
                    "sparse_relation_delta_bytes": (relation_after_sparse - relation_after_initial),
                    "normalized_row_count": row_count,
                    "changed_node_count": sparse_result.changed_node_count,
                    "removed_node_count": sparse_result.removed_node_count,
                    "aggregate_scan_detected": False,
                    "topology_payload_reads": topology_payload_reads,
                }
            )
    finally:
        if execution_ids:
            RayTaskExecution.objects.filter(pk__in=execution_ids).delete()

    payload = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "django-ray-workflow-progress-production-publication",
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
            "storage_implementation_sha256": _sha256(
                ROOT / "src" / "django_ray" / "workflow_progress_storage.py"
            ),
            "benchmark_implementation_sha256": _sha256(Path(__file__)),
        },
        "method": {
            "initial": (
                "prepare all topology/detail records, stage immutable topology, and atomically "
                "publish normalized detail plus the summary pointer"
            ),
            "sparse": (
                "prepare one RUNNING node and publish it against prepared immutable current "
                "topology through the exact-fenced production transaction"
            ),
            "sql_count": "Django CaptureQueriesContext around sparse publication",
            "wal": "pg_wal_lsn_diff around each publication on an otherwise idle database",
            "relation_size": (
                "sum of pg_total_relation_size for the five workflow-progress storage tables; "
                "deltas are allocator-dependent"
            ),
            "cleanup": "owning synthetic RayTaskExecution rows deleted before exit",
        },
        "cases": cases,
        "limitations": [
            "One cold measurement was recorded for each size; this is scale evidence, not a latency SLO.",
            "The benchmark runs locally through the Django database connection, not across a production network.",
            "Relation deltas depend on PostgreSQL page reuse and do not shrink when deleted rows are cleaned up.",
            "The producer-side in-memory preparation cost is reported separately from database publication.",
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
