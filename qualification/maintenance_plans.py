"""Fixed, disposable PostgreSQL maintenance-plan investigation (no worker turns)."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_ROWS = 8192
BATCH_ROWS = 256
MAX_PLAN_NODES = 64
MAX_PLAN_DEPTH = 16
MAX_PLAN_BYTES = 65536
MAX_REPORT_BYTES = 1024 * 1024
PROFILES = ("dense", "sparse", "high-tail")
QUEUES = ("plan-a", "plan-b", "plan-c")
METHODS = {
    "orphan": "reconcile_tasks",
    "running": "detect_stuck_tasks",
    "cancelling": "process_cancellations",
}
LIMITS = {
    "rows": MAX_ROWS,
    "batch_rows": BATCH_ROWS,
    "plan_nodes": MAX_PLAN_NODES,
    "plan_depth": MAX_PLAN_DEPTH,
    "plan_bytes": MAX_PLAN_BYTES,
    "report_bytes": MAX_REPORT_BYTES,
    "statement_ms": 2000,
    "lock_ms": 500,
    "process_seconds": 360,
    "postgres_memory_mib": 1024,
    "postgres_cpus": 1,
}


class PlanRefusalError(Exception):
    """Only fixed, secret-free categories may cross the CLI boundary."""


def encode(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, OverflowError):
        raise PlanRefusalError("REPORT_SHAPE") from None


def bounded_report(value) -> bytes:
    raw = encode(value)
    if len(raw) + 1 > MAX_REPORT_BYTES:
        raise PlanRefusalError("REPORT_BUDGET")
    return raw + b"\n"


def _git(*args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, env=env, capture_output=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        raise PlanRefusalError("SOURCE_IDENTITY") from None
    if len(result.stdout) > 65536:
        raise PlanRefusalError("SOURCE_IDENTITY")
    return result.stdout.decode("ascii").strip()


def source_identity(expected: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise PlanRefusalError("SOURCE_IDENTITY")
    head = _git("rev-parse", "HEAD")
    tree = _git("rev-parse", "HEAD^{tree}")
    if head != expected or not re.fullmatch(r"[0-9a-f]{40}", tree) or _git("status", "--porcelain"):
        raise PlanRefusalError("SOURCE_IDENTITY")
    return {"source_commit": head, "source_tree": tree}


def admission(env, system: str) -> dict:
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY_ID": "790856348",
        "GITHUB_REPOSITORY_OWNER_ID": "15094983",
        "GITHUB_ACTOR_ID": "15094983",
        "DJANGO_SETTINGS_MODULE": "tests.postgres_settings",
        "DJANGO_RAY_MAINTENANCE_PLANS": "owned-disposable-v1",
        "DATABASE_NAME": "maintenance_plans",
        "DATABASE_HOST": "127.0.0.1",
        "DATABASE_PORT": "5432",
        "DATABASE_USER": "django_ray",
    }
    if system != "Linux" or any(env.get(k) != v for k, v in expected.items()):
        raise PlanRefusalError("ADMISSION")
    return source_identity(env.get("GITHUB_SHA", ""))


def capacity() -> None:
    try:
        memory = int(
            next(
                x.split()[1]
                for x in Path("/proc/meminfo").read_text().splitlines()
                if x.startswith("MemTotal:")
            )
        )
        if (
            (os.cpu_count() or 0) < 2
            or memory < 6_000_000
            or shutil.disk_usage(ROOT).free < 4 * 1024**3
        ):
            raise PlanRefusalError("CAPACITY")
    except (OSError, StopIteration, ValueError):
        raise PlanRefusalError("CAPACITY") from None


def checkpoint(identity):
    from scripts.wait_for_ci_checkpoint import read_metadata, wait_for_checkpoint

    deadline = time.monotonic() + 90
    try:
        result = wait_for_checkpoint(
            lambda endpoint: read_metadata(endpoint, timeout=deadline - time.monotonic()),
            "dariuszpanas/django-ray",
            identity["source_commit"],
            deadline=deadline,
            interval=1,
        )
    except Exception:
        raise PlanRefusalError("CI_CHECKPOINT") from None
    return {
        "source_commit": identity["source_commit"],
        "ci_run_id": result.run_id,
        "ci_attempt": result.attempt,
        "checkpoint": "passed",
    }


def query_slice(source: str) -> tuple[ast.expr, ast.expr, str]:
    """Extract only the real source assignment and its queue refinement.

    Never call a worker scan entrypoint: its prelude can acquire authority or
    reconcile remote work. Unknown syntax/placement refuses before fixture writes.
    """
    try:
        method = ast.parse(textwrap.dedent(source)).body[0]
        if not isinstance(method, ast.FunctionDef):
            raise ValueError
        matches = []
        for index, node in enumerate(method.body):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "_maintenance_scan_page"
            ):
                matches.append((index, node.value))
        if len(matches) != 1:
            raise ValueError
        index, call = matches[0]
        if len(call.args) != 4 or not isinstance(call.args[1], ast.Name):
            raise ValueError
        name = call.args[1].id
        assignments = [
            (i, n)
            for i, n in enumerate(method.body[:index])
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
        ]
        if len(assignments) != 1:
            raise ValueError
        start, assignment = assignments[0]
        refinement = method.body[start + 1]
        if not (
            isinstance(refinement, ast.If)
            and ast.unparse(refinement.test) == "queues is not None"
            and len(refinement.body) == 1
            and not refinement.orelse
        ):
            raise ValueError
        refined = refinement.body[0]
        if not (
            isinstance(refined, ast.Assign)
            and len(refined.targets) == 1
            and isinstance(refined.targets[0], ast.Name)
            and refined.targets[0].id == name
            and isinstance(refined.value, ast.Call)
            and ast.unparse(refined.value) == f"{name}.filter(queue_name__in=queues)"
        ):
            raise ValueError
        for intervening in method.body[start + 2 : index]:
            if not (
                isinstance(intervening, ast.Assign)
                and len(intervening.targets) == 1
                and isinstance(intervening.targets[0], ast.Name)
                and intervening.targets[0].id == "finalized_count"
                and isinstance(intervening.value, ast.Constant)
                and type(intervening.value.value) is int
                and intervening.value.value == 0
            ):
                raise ValueError
        allowed_names = {
            "self",
            "RayTaskExecution",
            "TaskState",
            "supported_protocols",
            "reconciled_task_ids",
            "queues",
            name,
        }
        allowed_attrs = {
            "_defer_maintenance_scan_fields",
            "objects",
            "filter",
            "exclude",
            "RUNNING",
            "CANCELLING",
            "minimum",
            "maximum",
        }
        for expression in [assignment.value, refined.value]:
            for node in ast.walk(expression):
                if not isinstance(
                    node, (ast.Call, ast.Attribute, ast.Name, ast.Constant, ast.keyword, ast.Load)
                ):
                    raise ValueError
                if isinstance(node, ast.Name) and node.id not in allowed_names:
                    raise ValueError
                if isinstance(node, ast.Attribute) and node.attr not in allowed_attrs:
                    raise ValueError
                if isinstance(node, ast.Call) and (
                    not isinstance(node.func, ast.Attribute)
                    or node.func.attr not in {"filter", "exclude", "_defer_maintenance_scan_fields"}
                ):
                    raise ValueError
                if isinstance(node, ast.keyword) and node.arg is None:
                    raise ValueError
        return assignment.value, refined.value, name
    except (SyntaxError, ValueError, IndexError):
        raise PlanRefusalError("SOURCE_QUERY_SHAPE") from None


def worker_queryset(kind: str, queues, protocols, reconciled_task_ids=()):
    from django_ray.models import RayTaskExecution, TaskState

    if (
        kind not in METHODS
        or len(reconciled_task_ids) > 32
        or any(type(pk) is not int or pk <= 0 for pk in reconciled_task_ids)
    ):
        raise PlanRefusalError("SOURCE_QUERY_SHAPE")
    source_file = (ROOT / "src/django_ray/management/commands/django_ray_worker.py").read_text()
    worker = next(
        n
        for n in ast.parse(source_file).body
        if isinstance(n, ast.ClassDef) and n.name == "Command"
    )
    method = next(
        n for n in worker.body if isinstance(n, ast.FunctionDef) and n.name == METHODS[kind]
    )
    source = ast.unparse(method)
    base, refinement, name = query_slice(source)
    fields = next(
        n
        for n in worker.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "_DEFERRED_MAINTENANCE_SCAN_FIELDS"
    )
    helper = next(
        n
        for n in worker.body
        if isinstance(n, ast.FunctionDef) and n.name == "_defer_maintenance_scan_fields"
    )
    # This tiny pure helper is admitted separately; unknown implementation changes
    # must be reviewed rather than importing the worker's authority/Ray graph.
    expected_helper = """@classmethod
def _defer_maintenance_scan_fields(cls, queryset: Any) -> Any:
    from django.db.models import QuerySet
    if isinstance(queryset, QuerySet):
        return queryset.defer(*cls._DEFERRED_MAINTENANCE_SCAN_FIELDS)
    return queryset
"""
    if ast.dump(helper, include_attributes=False) != ast.dump(
        ast.parse(expected_helper).body[0], include_attributes=False
    ):
        raise PlanRefusalError("SOURCE_QUERY_SHAPE")
    field_values = ast.literal_eval(fields.value)
    if (
        not isinstance(field_values, tuple)
        or len(field_values) > 32
        or any(type(v) is not str for v in field_values)
    ):
        raise PlanRefusalError("SOURCE_QUERY_SHAPE")
    namespace = {"Any": object, "__name__": __name__}
    factory = ast.ClassDef(
        name="QueryFactory",
        bases=[],
        keywords=[],
        body=[fields, helper],
        decorator_list=[],
        type_params=[],
    )
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
            "<maintenance-helper>",
            "exec",
        ),
        namespace,
    )
    namespace.update(
        {
            "self": namespace["QueryFactory"],
            "RayTaskExecution": RayTaskExecution,
            "TaskState": TaskState,
            "supported_protocols": protocols,
            "reconciled_task_ids": set(reconciled_task_ids),
            "queues": queues,
        }
    )
    # Evaluate only validated, read-only ORM expressions from this trusted source;
    # no method prelude, worker instance, lease, task processing or RPC is invoked.
    query = eval(
        compile(ast.Expression(base), "<maintenance-query>", "eval"),
        {"__builtins__": {}},
        namespace,
    )
    if queues is not None:
        namespace[name] = query
        query = eval(
            compile(ast.Expression(refinement), "<maintenance-query>", "eval"),
            {"__builtins__": {}},
            namespace,
        )
    return query, hashlib.sha256(source.encode()).hexdigest()


def prepare_database(connection):
    """Refuse an unknown/nonempty catalog before migrate can write anything."""
    from django.core.management import call_command

    if (
        connection.vendor != "postgresql"
        or not 170000 <= connection.pg_version < 180000
        or connection.in_atomic_block
    ):
        raise PlanRefusalError("DATABASE_ADMISSION")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), current_user, pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        )
        if cursor.fetchone() != ("maintenance_plans", "django_ray", "django_ray"):
            raise PlanRefusalError("DATABASE_ADMISSION")
        cursor.execute(
            "SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')"
        )
        if cursor.fetchone()[0] != 0:
            raise PlanRefusalError("DATABASE_NOT_FRESH")
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '15000ms'")
        cursor.execute("SET lock_timeout = '2000ms'")
    call_command("migrate", database="default", interactive=False, verbosity=0)
    fixture_empty()


def plan_summary(raw):
    if len(encode(raw)) > MAX_PLAN_BYTES or not isinstance(raw, list) or len(raw) != 1:
        raise PlanRefusalError("PLAN_BUDGET")
    nodes = 0
    numeric = {
        "Startup Cost",
        "Total Cost",
        "Plan Rows",
        "Plan Width",
        "Actual Rows",
        "Actual Loops",
        "Rows Removed by Filter",
        "Rows Removed by Index Recheck",
        "Shared Hit Blocks",
        "Shared Read Blocks",
        "Shared Dirtied Blocks",
        "Shared Written Blocks",
        "Local Hit Blocks",
        "Local Read Blocks",
        "Temp Read Blocks",
        "Temp Written Blocks",
        "Sort Space Used",
    }

    def visit(value, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_PLAN_NODES or depth > MAX_PLAN_DEPTH or not isinstance(value, dict):
            raise PlanRefusalError("PLAN_BUDGET")
        result = {}
        for key in ["Node Type", "Index Name", "Scan Direction", "Sort Method", "Sort Space Type"]:
            if key in value:
                text = value[key]
                if (
                    type(text) is not str
                    or len(text) > 128
                    or not re.fullmatch(r"[A-Za-z0-9_ .-]+", text)
                ):
                    raise PlanRefusalError("PLAN_SHAPE")
                result[key] = text
        if "Node Type" not in result:
            raise PlanRefusalError("PLAN_SHAPE")
        for key in numeric & value.keys():
            n = value[key]
            if type(n) not in (float, int) or n < 0 or n > 2**63 - 1 or not math.isfinite(n):
                raise PlanRefusalError("PLAN_SHAPE")
            result[key] = n
        children = value.get("Plans", [])
        if not isinstance(children, list):
            raise PlanRefusalError("PLAN_SHAPE")
        if children:
            result["Plans"] = [visit(child, depth + 1) for child in children]
        return result

    try:
        result = visit(raw[0]["Plan"], 0)
    except (KeyError, TypeError):
        raise PlanRefusalError("PLAN_SHAPE") from None
    return {"plan": result, "nodes": nodes, "explain": raw}


def capture_pages(query, connection):
    from django_ray.runner.maintenance import MaintenanceScan

    captured = []
    integer_types = (int,)
    if query.db != connection.alias:
        raise PlanRefusalError("QUERY_CAPTURE")

    def observe(execute, sql, params, many, context):
        nonlocal integer_types
        if (
            many
            or len(captured) >= 3
            or not sql.lstrip().upper().startswith("SELECT ")
            or len(sql) > 8192
            or len(params or ()) > 32
        ):
            raise PlanRefusalError("QUERY_CAPTURE")
        for value in params or ():
            if (
                integer_types == (int,)
                and connection.vendor == "postgresql"
                and isinstance(value, int)
                and type(value) not in (int, bool)
            ):
                try:
                    numeric = importlib.import_module("psycopg.types.numeric")
                except ImportError:
                    raise PlanRefusalError("QUERY_CAPTURE") from None
                # Keep exact driver-width objects for the SELECT and EXPLAIN.
                integer_types = (int, numeric.Int2, numeric.Int4, numeric.Int8)
            if not (
                type(value) in integer_types
                or value is None
                or type(value) is bool
                or isinstance(value, str)
                and len(value) <= 32
                and (
                    value in QUEUES
                    or value in {"RUNNING", "CANCELLING", "raysubmit\\_%", "raysubmit_%"}
                    or (connection.vendor == "sqlite" and value == "integer")
                )
            ):
                raise PlanRefusalError("QUERY_CAPTURE")
        captured.append(
            (str(sql), tuple(str(v) if isinstance(v, str) else v for v in params or ()))
        )
        return execute(sql, params, many, context)

    scan = MaintenanceScan()
    with connection.execute_wrapper(observe):
        first = scan.page(query, scope=("plan",))
        if len(first) != 32 or not scan.page_has_more:
            raise PlanRefusalError("FIXTURE_SELECTION")
        first_ids = [c.pk for c in first]
        for candidate in first:
            scan.advance(candidate)
        continuation = scan.page(query, scope=("plan",))
    if len(captured) != 3 or not continuation or min(c.pk for c in continuation) <= max(first_ids):
        raise PlanRefusalError("QUERY_CAPTURE")
    if query.db != connection.alias or scan.high_water is None:
        raise PlanRefusalError("QUERY_CAPTURE")
    return captured


def fixture_empty():
    from django.apps import apps

    # Migrations seed policy and legacy-admission controls. Every other model must be
    # empty. No fixture prefix narrows the actual production scan predicate.
    controls = {
        "LegacyWorkerAdmissionToken",
        "TaskWorkerLeasePolicy",
        "TaskExecutionProtocolPolicy",
    }
    if any(
        model.objects.exists()
        for model in apps.get_app_config("django_ray").get_models()
        if model.__name__ not in controls
    ):
        raise PlanRefusalError("FIXTURE_NOT_EMPTY")


def seed(profile: str, count: int):
    from django_ray.models import RayTaskExecution, TaskState

    if profile not in PROFILES or type(count) is not int or not 384 <= count <= MAX_ROWS:
        raise PlanRefusalError("FIXTURE_PROFILE")
    for start in range(0, count, BATCH_ROWS):
        rows = []
        for i in range(start, min(start + BATCH_ROWS, count)):
            eligible = (
                profile == "dense"
                or profile == "high-tail"
                and i < 1024
                or profile == "sparse"
                and i % 8 < 2
            )
            state = (
                (TaskState.RUNNING if i % 2 == 0 else TaskState.CANCELLING)
                if eligible
                else TaskState.SUCCEEDED
            )
            rows.append(
                RayTaskExecution(
                    task_id=f"maintenance-plan-{i}",
                    callable_path="diagnostic.not_executed",
                    state=state,
                    queue_name=QUEUES[(i // 2) % 3],
                    ray_job_id=f"raysubmit_plan_{i}",
                    claimed_by_worker="plan-owner",
                    completion_data="{}" if i % 16 == 0 else None,
                )
            )
        RayTaskExecution.objects.bulk_create(rows, batch_size=BATCH_ROWS)


def collect(*, profiles=PROFILES, count=MAX_ROWS, queue_groups=(None, (QUEUES[0],), QUEUES[:2])):
    """Internal tiny integration selection is separate from the fixed manual CLI."""
    from django.db import connection, transaction
    from django.db.migrations.executor import MigrationExecutor
    from django.db.migrations.recorder import MigrationRecorder

    from django_ray.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_RANGE
    from django_ray.models import RayTaskExecution

    if (
        type(count) is not int
        or not 384 <= count <= MAX_ROWS
        or not 1 <= len(profiles) <= 3
        or len(set(profiles)) != len(profiles)
        or not set(profiles) <= set(PROFILES)
        or not 1 <= len(queue_groups) <= 3
        or any(queues not in (None, (QUEUES[0],), QUEUES[:2]) for queues in queue_groups)
    ):
        raise PlanRefusalError("FIXTURE_PROFILE")
    if (
        connection.vendor != "postgresql"
        or not 170000 <= connection.pg_version < 180000
        or connection.in_atomic_block
    ):
        raise PlanRefusalError("DATABASE_ADMISSION")
    if MigrationExecutor(connection).migration_plan(
        MigrationExecutor(connection).loader.graph.leaf_nodes()
    ):
        raise PlanRefusalError("DATABASE_MIGRATIONS")
    # All source definitions are parsed before any write, even for a tiny run.
    for kind in METHODS:
        worker_queryset(kind, None, SUPPORTED_EXECUTION_PROTOCOL_RANGE)
    fixture_empty()
    records = []
    for profile in profiles:
        with transaction.atomic():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '2000ms'")
                    cursor.execute("SET LOCAL lock_timeout = '500ms'")
                    cursor.execute("SET LOCAL work_mem = '4MB'")
                seed(profile, count)
                with connection.cursor() as cursor:
                    cursor.execute('ANALYZE "django_ray_raytaskexecution"')
                modeled_ids = tuple(
                    RayTaskExecution.objects.filter(state="RUNNING")
                    .order_by("pk")
                    .values_list("pk", flat=True)[:2]
                )
                for kind in METHODS:
                    for queues in queue_groups:
                        for reconciled in ((), modeled_ids) if kind == "orphan" else ((),):
                            query, source_hash = worker_queryset(
                                kind, queues, SUPPORTED_EXECUTION_PROTOCOL_RANGE, reconciled
                            )
                            captured = capture_pages(query, connection)
                            for phase, (sql, params) in zip(
                                ("high-water", "first", "continuation"), captured, strict=True
                            ):
                                with connection.cursor() as cursor:
                                    cursor.execute(
                                        "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) "
                                        + sql,
                                        params,
                                    )
                                    plan = plan_summary(cursor.fetchone()[0])
                                records.append(
                                    {
                                        "profile": profile,
                                        "seeded_rows": count,
                                        "kind": kind,
                                        "queues": "all" if queues is None else list(queues),
                                        "phase": phase,
                                        "reconciled_active_page_ids": list(reconciled),
                                        "worker_method_sha256": source_hash,
                                        "sql": sql,
                                        "parameters": params,
                                        **plan,
                                    }
                                )
                                bounded_report(records)
            finally:
                transaction.set_rollback(True)
        fixture_empty()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = %s ORDER BY indexname",
            [RayTaskExecution._meta.db_table],
        )
        indexes = cursor.fetchall()
    return {
        "schema_version": 1,
        "limits": LIMITS,
        "records": records,
        "postgres_version": connection.pg_version,
        "migrations": sorted(
            name
            for app, name in MigrationRecorder(connection).applied_migrations()
            if app == "django_ray"
        ),
        "indexes": indexes,
        "fixture_rows_rolled_back": True,
        "complete_worker_gate": False,
        "production_performance_acceptance": False,
        "scope": "Synthetic query plans only; no RSS/throughput/savings, authority, Ray or lifecycle proof. Sequences/statistics may change in the disposable database.",
        "protocol_range": [
            SUPPORTED_EXECUTION_PROTOCOL_RANGE.minimum,
            SUPPORTED_EXECUTION_PROTOCOL_RANGE.maximum,
        ],
        "orphan_active_page_scenarios": "Empty active page and modeled two successfully loaded active-page IDs; no active handle or lifecycle is executed.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        identity = admission(os.environ, platform.system())
        capacity()
        if args.preflight:
            print(json.dumps({"preflight": "passed", **identity, "limits": LIMITS}))
            return 0
        if args.checkpoint:
            print(json.dumps(checkpoint(identity)))
            return 0
        if (
            args.output is None
            or args.output.exists()
            or args.output.resolve().is_relative_to(ROOT)
        ):
            raise PlanRefusalError("OUTPUT")
        import django

        django.setup()
        from importlib.metadata import version

        from django.conf import settings
        from django.db import connection

        db = settings.DATABASES["default"]
        if (
            set(settings.DATABASES) != {"default"}
            or settings.DATABASE_ROUTERS
            or db["NAME"] != "maintenance_plans"
            or db["HOST"] != "127.0.0.1"
            or str(db["PORT"]) != "5432"
            or db["USER"] != "django_ray"
        ):
            raise PlanRefusalError("DATABASE_ADMISSION")
        try:
            from django_ray.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_RANGE

            for kind in METHODS:
                worker_queryset(kind, None, SUPPORTED_EXECUTION_PROTOCOL_RANGE)
            prepare_database(connection)
            report = collect()
        finally:
            connection.close()
        if source_identity(identity["source_commit"]) != identity:
            raise PlanRefusalError("SOURCE_IDENTITY")
        report.update(
            identity,
            versions={name: version(name) for name in ("Django", "psycopg", "django-ray")},
            python=platform.python_version(),
        )
        report["github"] = {
            key: os.environ.get(key, "")
            for key in (
                "GITHUB_REPOSITORY_ID",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_ACTOR_ID",
            )
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        encoded = bounded_report(report)
        with args.output.open("xb") as stream:
            stream.write(encoded)
        print(
            json.dumps(
                {
                    "result": "fixture_plans_collected",
                    "records": len(report["records"]),
                    "complete_worker_gate": False,
                }
            )
        )
        return 0
    except PlanRefusalError as error:
        print(json.dumps({"result": "refused", "category": str(error)}))
    except Exception:
        print(json.dumps({"result": "refused", "category": "INVESTIGATION_FAILED"}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
