"""No service, worker turn, Ray runtime or process is started by these controls."""

import ast
import builtins
import importlib
import json
import re
import shlex
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from django.db import connection

from django_ray.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_RANGE as PROTOCOLS
from qualification import maintenance_plans as probe


@pytest.mark.parametrize("kind", probe.METHODS)
@pytest.mark.parametrize("queues", [None, ("plan-a",), ("plan-a", "plan-b")])
def test_source_factory_compiles_real_read_only_queries_without_worker_import(kind, queues):
    query, source_hash = probe.worker_queryset(
        kind, queues, PROTOCOLS, (12, 14) if kind == "orphan" else ()
    )
    sql, params = query.query.sql_with_params()
    assert len(source_hash) == 64
    assert '"state"' in sql and '"execution_protocol_version"' in sql
    assert ("IS NULL" in sql) == (kind == "running")
    assert (" LIKE " in sql) == (kind == "orphan")
    assert ("NOT (" in sql) == (kind == "orphan")
    assert ("queue_name" in sql.split("WHERE")[1]) == (queues is not None)
    assert query.db == "default"


def test_ast_refuses_side_effect_or_unrecognized_placement():
    original = """def scan(self, queues):
    query = self._defer_maintenance_scan_fields(RayTaskExecution.objects.filter(state=TaskState.RUNNING))
    if queues is not None:
        query = query.filter(queue_name__in=queues)
    candidates = self._maintenance_scan_page("running", query, queues, supported_protocols)
"""
    assert isinstance(probe.query_slice(original)[0], ast.Call)
    for changed in [
        original.replace(".filter(state=", ".update(state="),
        original.replace("    candidates =", "    query.delete()\n    candidates ="),
        original.replace("    candidates =", "    query.query.high_mark = 33\n    candidates ="),
        original.replace("queue_name__in=queues", "queue_name='other'"),
        original.replace("TaskState.RUNNING", "__import__('os')"),
    ]:
        with pytest.raises(probe.PlanRefusalError, match="SOURCE_QUERY_SHAPE"):
            probe.query_slice(changed)


@pytest.mark.parametrize("ids", [(True,), tuple(range(1, 34)), (-1,)])
def test_source_factory_refuses_invalid_active_page(ids):
    with pytest.raises(probe.PlanRefusalError, match="SOURCE_QUERY_SHAPE"):
        probe.worker_queryset("orphan", None, PROTOCOLS, ids)


def plan():
    return [
        {
            "Plan": {
                "Node Type": "Limit",
                "Actual Rows": 33,
                "Actual Loops": 1,
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Index Name": "example_pkey",
                        "Rows Removed by Filter": 200,
                        "Shared Hit Blocks": 8,
                        "Filter": "not retained",
                    }
                ],
            }
        }
    ]


def test_plan_keeps_work_counts_but_no_conditions_or_arbitrary_payload():
    result = probe.plan_summary(plan())
    assert result["nodes"] == 2
    child = result["plan"]["Plans"][0]
    assert child["Rows Removed by Filter"] == 200 and child["Shared Hit Blocks"] == 8
    assert "Filter" not in child


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, 2**64])
def test_plan_rejects_nonfinite_or_invalid_counts(bad):
    value = plan()
    value[0]["Plan"]["Actual Rows"] = bad
    with pytest.raises(probe.PlanRefusalError):
        probe.plan_summary(value)


def test_plan_rejects_depth_nodes_bytes_and_report_overflow(monkeypatch):
    for setting, limit in [("MAX_PLAN_DEPTH", 0), ("MAX_PLAN_NODES", 1), ("MAX_PLAN_BYTES", 5)]:
        with monkeypatch.context() as patch:
            patch.setattr(probe, setting, limit)
            with pytest.raises(probe.PlanRefusalError, match="PLAN_BUDGET"):
                probe.plan_summary(plan())
    monkeypatch.setattr(probe, "MAX_REPORT_BYTES", 4)
    with pytest.raises(probe.PlanRefusalError, match="REPORT_BUDGET"):
        probe.bounded_report({"large": "value"})


def test_report_accepts_complete_json_line_at_exact_byte_limit(monkeypatch):
    monkeypatch.setattr(probe, "MAX_REPORT_BYTES", 3)
    assert probe.bounded_report({}) == b"{}\n"


def test_report_budget_includes_trailing_newline(monkeypatch):
    monkeypatch.setattr(probe, "MAX_REPORT_BYTES", 2)
    with pytest.raises(probe.PlanRefusalError, match="REPORT_BUDGET"):
        probe.bounded_report({})


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_admission_refuses_before_source_or_database(monkeypatch, system):
    source = Mock(side_effect=AssertionError("source must remain untouched"))
    monkeypatch.setattr(probe, "source_identity", source)
    with pytest.raises(probe.PlanRefusalError, match="ADMISSION"):
        probe.admission({}, system)
    source.assert_not_called()


def test_source_identity_rejects_dirty_wrong_head_and_bad_sha(monkeypatch):
    for values in [("a" * 40, "b" * 40, " M source.py"), ("c" * 40, "b" * 40, "")]:
        iterator = iter(values)
        monkeypatch.setattr(probe, "_git", lambda *args, current=iterator: next(current))
        with pytest.raises(probe.PlanRefusalError, match="SOURCE_IDENTITY"):
            probe.source_identity("a" * 40)
    with pytest.raises(probe.PlanRefusalError, match="SOURCE_IDENTITY"):
        probe.source_identity("main")


@pytest.mark.django_db
def test_real_page_capture_does_not_read_payloads_and_proves_continuation():
    from django_ray.models import RayTaskExecution

    probe.seed("dense", 384)
    query, _ = probe.worker_queryset("running", None, PROTOCOLS)
    statements = probe.capture_pages(query, connection)
    assert len(statements) == 3
    assert "DESC LIMIT 1" in statements[0][0]
    assert "LIMIT 33" in statements[1][0] and "LIMIT 33" in statements[2][0]
    assert '"id" >' in statements[2][0] and '"id" >' not in statements[1][0]
    assert "args_json" not in statements[1][0]
    if connection.vendor == "sqlite":
        assert "TYPEOF(" not in statements[0][0]
        assert all(sql.count("TYPEOF(") == 3 for sql, _params in statements[1:])
        assert all(params.count("integer") == 3 for _sql, params in statements[1:])
    assert RayTaskExecution.objects.count() == 384


@pytest.mark.django_db
def test_capture_refuses_short_cycle_instead_of_mislabeling_restart():
    from django_ray.models import RayTaskExecution

    RayTaskExecution.objects.create(task_id="only", callable_path="never", state="RUNNING")
    query, _ = probe.worker_queryset("running", None, PROTOCOLS)
    with pytest.raises(probe.PlanRefusalError, match="FIXTURE_SELECTION"):
        probe.capture_pages(query, connection)


@pytest.mark.parametrize(
    ("vendor", "value", "allowed"),
    [
        ("sqlite", "integer", True),
        ("postgresql", "integer", False),
        ("unknown", "integer", False),
        ("sqlite", "INTEGER", False),
        ("sqlite", "Integer", False),
        ("sqlite", "integer ", False),
        ("sqlite", "text", False),
        ("sqlite", "blob", False),
        ("sqlite", "real", False),
        ("sqlite", "null", False),
        ("sqlite", "RUNNING", True),
        ("postgresql", "RUNNING", True),
    ],
)
def test_capture_limits_sqlite_storage_label_to_exact_vendor(monkeypatch, vendor, value, allowed):
    from django_ray.runner import maintenance

    # These synthetic callbacks isolate strict parameter admission; the real
    # page-capture test above separately checks the production SQLite compiler.
    execute = Mock()
    scan = Mock(high_water=33, page_has_more=True)
    pages = iter(([SimpleNamespace(pk=i) for i in range(1, 33)], [SimpleNamespace(pk=33)]))

    @contextmanager
    def execute_wrapper(observe):
        def page(query, *, scope):
            if scan.page.call_count == 1:
                observe(execute, "SELECT %s", (value,), False, None)
            observe(execute, "SELECT %s", (value,), False, None)
            return next(pages)

        scan.page.side_effect = page
        yield

    monkeypatch.setattr(maintenance, "MaintenanceScan", Mock(return_value=scan))
    database = SimpleNamespace(alias="default", vendor=vendor, execute_wrapper=execute_wrapper)
    query = SimpleNamespace(db="default")
    if allowed:
        assert probe.capture_pages(query, database) == [("SELECT %s", (value,))] * 3
        assert execute.call_count == 3
    else:
        with pytest.raises(probe.PlanRefusalError, match="^QUERY_CAPTURE$"):
            probe.capture_pages(query, database)
        execute.assert_not_called()


@pytest.mark.django_db
def test_migration_seeded_control_rows_remain_unchanged():
    from django_ray.models import LegacyWorkerAdmissionToken

    token, _ = LegacyWorkerAdmissionToken.objects.get_or_create(singleton_key=1)
    before = list(LegacyWorkerAdmissionToken.objects.values())
    probe.fixture_empty()
    assert list(LegacyWorkerAdmissionToken.objects.values()) == before
    assert LegacyWorkerAdmissionToken.objects.filter(pk=token.pk).exists()


def _capture_driver_parameter_frames(
    monkeypatch, params, *, vendor="postgresql", sql="SELECT %s", query=None
):
    """Exercise the real observer with fake pages, never database execution."""
    from django_ray.runner import maintenance

    execute = Mock()
    scan = Mock(high_water=33, page_has_more=True)
    pages = iter(([SimpleNamespace(pk=i) for i in range(1, 33)], [SimpleNamespace(pk=33)]))

    @contextmanager
    def execute_wrapper(observe):
        def page(_query, *, scope):
            assert scope == ("plan",)
            if scan.page.call_count == 1:
                observe(execute, sql, params, False, None)
            observe(execute, sql, params, False, None)
            return next(pages)

        scan.page.side_effect = page
        yield

    monkeypatch.setattr(maintenance, "MaintenanceScan", Mock(return_value=scan))
    database = SimpleNamespace(alias="default", vendor=vendor, execute_wrapper=execute_wrapper)
    query = SimpleNamespace(db="default") if query is None else query
    return lambda: probe.capture_pages(query, database), execute


@pytest.mark.parametrize("name", ["Int2", "Int4", "Int8"])
def test_postgresql_adapted_known_integer_retains_object_and_json_value(monkeypatch, name):
    numeric = pytest.importorskip("psycopg.types.numeric", reason="requires the PostgreSQL extra")
    value = getattr(numeric, name)(7)
    params = (value,)
    capture, execute = _capture_driver_parameter_frames(monkeypatch, params)
    statements = capture()
    assert len(statements) == execute.call_count == 3
    assert all(
        call.args[1] is params and call.args[1][0] is value for call in execute.call_args_list
    )
    assert all(values[0] is value for _sql, values in statements)
    decoded = json.loads(probe.encode({"parameters": statements[0][1]}))
    assert decoded == {"parameters": [7]} and type(decoded["parameters"][0]) is int


@pytest.mark.parametrize("vendor", ["sqlite", "unknown"])
@pytest.mark.parametrize("name", ["Int2", "Int4", "Int8"])
def test_postgresql_adapted_known_integer_refuses_other_vendor(monkeypatch, vendor, name):
    numeric = pytest.importorskip("psycopg.types.numeric", reason="requires the PostgreSQL extra")
    capture, execute = _capture_driver_parameter_frames(
        monkeypatch, (getattr(numeric, name)(7),), vendor=vendor
    )
    with pytest.raises(probe.PlanRefusalError, match="^QUERY_CAPTURE$"):
        capture()
    execute.assert_not_called()


@pytest.mark.parametrize("name", ["IntNumeric", "Oid", "derived-known"])
def test_postgresql_adapted_unknown_or_derived_numeric_type_refuses(monkeypatch, name):
    numeric = pytest.importorskip("psycopg.types.numeric", reason="requires the PostgreSQL extra")
    if name == "derived-known":

        class Derived(numeric.Int2):
            pass

        value = Derived(7)
    else:
        value = getattr(numeric, name)(7)
    capture, execute = _capture_driver_parameter_frames(monkeypatch, (value,))
    with pytest.raises(probe.PlanRefusalError, match="^QUERY_CAPTURE$"):
        capture()
    execute.assert_not_called()


def test_postgresql_adapted_arbitrary_integer_does_not_call_conversion(monkeypatch):
    conversions = []

    class UnknownInt(int):
        def __int__(self):
            conversions.append(True)
            raise AssertionError("unknown integer conversion must not execute")

    capture, execute = _capture_driver_parameter_frames(monkeypatch, (UnknownInt(7),))
    with pytest.raises(probe.PlanRefusalError, match="^QUERY_CAPTURE$"):
        capture()
    assert conversions == []
    execute.assert_not_called()


def _refuse_optional_numeric_import(monkeypatch, exception_type):
    original_import = builtins.__import__
    original_module = importlib.import_module

    def imported(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            raise exception_type("synthetic optional PostgreSQL driver refusal")
        return original_import(name, *args, **kwargs)

    def module(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            raise exception_type("synthetic optional PostgreSQL driver refusal")
        return original_module(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", imported)
    monkeypatch.setattr(importlib, "import_module", module)


def test_postgresql_adapted_missing_driver_is_fixed_refusal(monkeypatch):
    class UnknownInt(int):
        pass

    _refuse_optional_numeric_import(monkeypatch, ImportError)
    capture, execute = _capture_driver_parameter_frames(monkeypatch, (UnknownInt(7),))
    with pytest.raises(probe.PlanRefusalError, match="^QUERY_CAPTURE$"):
        capture()
    execute.assert_not_called()


@pytest.mark.parametrize("value", [7, True, None, "RUNNING"])
def test_postgresql_adapted_plain_scalars_need_no_optional_import(monkeypatch, value):
    _refuse_optional_numeric_import(monkeypatch, AssertionError)
    capture, execute = _capture_driver_parameter_frames(monkeypatch, (value,))
    statements = capture()
    assert execute.call_count == len(statements) == 3
    assert all(values[0] is value for _sql, values in statements)


@pytest.mark.parametrize("kind", probe.METHODS)
@pytest.mark.parametrize("queues", [None, ("plan-a",), ("plan-a", "plan-b")])
def test_postgresql_adapted_real_compiler_without_connection(monkeypatch, kind, queues):
    """Real predicates/PG compiler; fake page delivery proves no DB plan or continuation."""
    numeric = pytest.importorskip("psycopg.types.numeric", reason="requires the PostgreSQL extra")
    from django.db.backends.postgresql.base import DatabaseWrapper

    database = DatabaseWrapper(
        {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "compile-only",
            "USER": "compile-only",
            "PASSWORD": "",
            "HOST": "127.0.0.1",
            "PORT": "0",
            "OPTIONS": {},
            "TIME_ZONE": None,
            "AUTOCOMMIT": True,
            "ATOMIC_REQUESTS": False,
            "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": False,
            "TEST": {},
        },
        alias="compile-only",
    )
    connection_attempts = []

    def forbidden(*_args, **_kwargs):
        connection_attempts.append(True)
        raise AssertionError("compile-only control cannot open a database")

    for name in ("ensure_connection", "get_new_connection", "cursor", "connect"):
        monkeypatch.setattr(database, name, forbidden)
    query, _source_hash = probe.worker_queryset(
        kind, queues, PROTOCOLS, (12, 14) if kind == "orphan" else ()
    )
    high_water = query.order_by("-pk").values_list("pk", flat=True)[:1]
    sql, params = high_water.query.get_compiler(connection=database).as_sql()
    assert sum(type(v) is numeric.Int2 for v in params) == 2
    capture, execute = _capture_driver_parameter_frames(monkeypatch, params, sql=sql, query=query)
    statements = capture()
    assert execute.call_count == len(statements) == 3
    assert all(call.args[1] is params for call in execute.call_args_list)
    assert all(
        captured is original
        for _sql, values in statements
        for captured, original in zip(values, params, strict=True)
        if type(original) in (numeric.Int2, numeric.Int4, numeric.Int8)
    )
    assert database.connection is None and connection_attempts == []


@pytest.mark.django_db
def test_nonempty_fixture_refuses_without_deleting_unknown_rows():
    from django_ray.models import RayTaskExecution

    task = RayTaskExecution.objects.create(task_id="unknown", callable_path="never")
    with pytest.raises(probe.PlanRefusalError, match="FIXTURE_NOT_EMPTY"):
        probe.fixture_empty()
    assert RayTaskExecution.objects.filter(pk=task.pk).exists()


def test_nonpostgres_refusal_precedes_cursor_and_migration():
    database = SimpleNamespace(vendor="sqlite", cursor=Mock())
    with pytest.raises(probe.PlanRefusalError, match="DATABASE_ADMISSION"):
        probe.prepare_database(database)
    database.cursor.assert_not_called()


def test_nonempty_catalog_refuses_before_any_migration_write(monkeypatch):
    from unittest.mock import MagicMock

    migrate = Mock()
    monkeypatch.setattr("django.core.management.call_command", migrate)
    cursor = Mock()
    cursor.fetchone.side_effect = [("maintenance_plans", "django_ray", "django_ray"), (1,)]
    manager = MagicMock()
    manager.__enter__.return_value = cursor
    database = SimpleNamespace(
        vendor="postgresql",
        pg_version=170000,
        in_atomic_block=False,
        cursor=Mock(return_value=manager),
    )
    with pytest.raises(probe.PlanRefusalError, match="DATABASE_NOT_FRESH"):
        probe.prepare_database(database)
    migrate.assert_not_called()
    assert all(call.args[0].startswith("SELECT ") for call in cursor.execute.call_args_list)


def test_query_factory_does_not_import_worker(monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "django_ray.management.commands.django_ray_worker":
            raise AssertionError("worker module imported")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    query, _ = probe.worker_queryset("orphan", ("plan-a",), PROTOCOLS, (1, 2))
    assert "NOT (" in query.query.sql_with_params()[0]


@pytest.mark.django_db
@pytest.mark.parametrize("profile", probe.PROFILES)
def test_fixed_profiles_keep_real_continuations_for_each_queue_selection(profile):
    probe.seed(profile, 8192)
    for queues in (None, ("plan-a",), ("plan-a", "plan-b")):
        for kind in probe.METHODS:
            for excluded in ((), (1, 2)) if kind == "orphan" else ((),):
                query, _ = probe.worker_queryset(kind, queues, PROTOCOLS, excluded)
                assert len(probe.capture_pages(query, connection)) == 3


@pytest.mark.django_db
def test_hosted_tiny_profile_also_has_real_continuation_for_every_scenario():
    probe.seed("dense", 384)
    for kind in probe.METHODS:
        for excluded in ((), (1, 2)) if kind == "orphan" else ((),):
            query, _ = probe.worker_queryset(kind, None, PROTOCOLS, excluded)
            assert len(probe.capture_pages(query, connection)) == 3


def test_owner_admission_binds_actor_repo_event_and_source(monkeypatch):
    env = {
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
        "GITHUB_SHA": "a" * 40,
    }
    source = Mock(return_value={"source_commit": "a" * 40})
    monkeypatch.setattr(probe, "source_identity", source)
    assert probe.admission(env, "Linux")["source_commit"] == "a" * 40
    for key in ("GITHUB_ACTOR_ID", "GITHUB_REPOSITORY_ID", "GITHUB_EVENT_NAME"):
        with pytest.raises(probe.PlanRefusalError, match="ADMISSION"):
            probe.admission({**env, key: "other"}, "Linux")
    assert source.call_count == 1


def _tail_scaling_part(count):
    """Synthetic report assembly frames, not database or plan evidence."""
    records = []
    for index, kind in enumerate(probe.METHODS):
        for queues in ("all", ["plan-a"], ["plan-a", "plan-b"]):
            for ids in ([], [11, 13]) if kind == "orphan" else ([],):
                for phase in ("high-water", "first", "continuation"):
                    records.append(
                        {
                            "profile": "high-tail",
                            "seeded_rows": count,
                            "kind": kind,
                            "queues": queues,
                            "phase": phase,
                            "reconciled_active_page_ids": ids,
                            "worker_method_sha256": ("abc"[index]) * 64,
                            "sql": "SELECT %s",
                            "parameters": (7,),
                            "plan": {"Node Type": "Limit", "Actual Rows": 1},
                            "nodes": 1,
                            "explain": [{"Plan": {"Node Type": "Limit", "Actual Rows": 1}}],
                        }
                    )
    return {
        "schema_version": 1,
        "limits": probe.LIMITS,
        "records": records,
        "postgres_version": 170011,
        "migrations": ["synthetic"],
        "indexes": [["synthetic", "synthetic"]],
        "fixture_rows_rolled_back": True,
        "complete_worker_gate": False,
        "production_performance_acceptance": False,
        "scope": "Synthetic report assembly only",
        "protocol_range": [1, 1],
        "orphan_active_page_scenarios": "Synthetic empty/two-ID cases",
    }


def test_tail_scaling_uses_fixed_population_grid_and_preserves_original_records(monkeypatch):
    parts = {count: _tail_scaling_part(count) for count in (2048, 4096, 8192)}
    collect = Mock(side_effect=lambda *, profiles, count: parts[count])
    monkeypatch.setattr(probe, "collect", collect)

    report = probe.collect_tail_scaling()

    assert [call.kwargs for call in collect.call_args_list] == [
        {"profiles": ("high-tail",), "count": count} for count in (2048, 4096, 8192)
    ]
    assert report["schema_version"] == 2
    assert report["series"] == {
        "name": "high-tail-scaling-v1",
        "populations": [2048, 4096, 8192],
        "shared_fixture_prefix_rows": 1024,
    }
    assert len(report["records"]) == 108
    assert {
        count: sum(r["seeded_rows"] == count for r in report["records"]) for count in parts
    } == {
        2048: 36,
        4096: 36,
        8192: 36,
    }
    original = parts[2048]["records"][0]
    assert report["records"][0] is original
    assert report["records"][0]["parameters"] is original["parameters"]
    assert all(part["schema_version"] == 1 and "series" not in part for part in parts.values())
    assert "Serial populations share aborted-tuple, index and cache history" in report["scope"]
    assert all(part["scope"] == "Synthetic report assembly only" for part in parts.values())
    assert report["complete_worker_gate"] is False
    assert report["production_performance_acceptance"] is False


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "wrong_population",
        "bool_population",
        "wrong_profile",
        "wrong_queue",
        "wrong_phase",
        "wrong_kind",
        "extra_ids",
        "unordered_ids",
        "bool_id",
        "inconsistent_ids",
        "changed_method_hash",
        "extra_field",
    ],
)
def test_tail_scaling_grid_refuses_missing_duplicate_or_mixed_scenarios(damage):
    records = deepcopy(
        [row for count in (2048, 4096, 8192) for row in _tail_scaling_part(count)["records"]]
    )
    active = next(row for row in records if row["reconciled_active_page_ids"])
    if damage == "missing":
        records.pop()
    elif damage == "duplicate":
        records[-1] = deepcopy(records[0])
    elif damage == "wrong_population":
        records[0]["seeded_rows"] = 8193
    elif damage == "bool_population":
        records[0]["seeded_rows"] = True
    elif damage == "wrong_profile":
        records[0]["profile"] = "dense"
    elif damage == "wrong_queue":
        records[0]["queues"] = ["plan-c"]
    elif damage == "wrong_phase":
        records[0]["phase"] = "restart"
    elif damage == "wrong_kind":
        records[0]["kind"] = "completed"
    elif damage == "extra_ids":
        active["reconciled_active_page_ids"] = [11, 13, 15]
    elif damage == "unordered_ids":
        active["reconciled_active_page_ids"] = [13, 11]
    elif damage == "bool_id":
        active["reconciled_active_page_ids"] = [True, 13]
    elif damage == "inconsistent_ids":
        active["reconciled_active_page_ids"] = [21, 23]
    elif damage == "changed_method_hash":
        records[0]["worker_method_sha256"] = "f" * 64
    else:
        records[0]["unexpected"] = "synthetic-private-canary"
    with pytest.raises(probe.PlanRefusalError, match="^SERIES_GRID$"):
        probe._validate_tail_scaling_grid(records)


@pytest.mark.parametrize("failure", ["metadata", "collection", "report_budget"])
def test_tail_scaling_stops_before_a_partial_series_can_be_success(monkeypatch, failure):
    seen = []

    def collect(*, profiles, count):
        seen.append(count)
        if failure == "collection" and count == 4096:
            raise probe.PlanRefusalError("PLAN_BUDGET")
        part = _tail_scaling_part(count)
        if failure == "metadata" and count == 4096:
            part["postgres_version"] = 170012
        return part

    monkeypatch.setattr(probe, "collect", collect)
    if failure == "report_budget":
        monkeypatch.setattr(probe, "MAX_REPORT_BYTES", 1)
    code = {
        "metadata": "SERIES_METADATA",
        "collection": "PLAN_BUDGET",
        "report_budget": "REPORT_BUDGET",
    }[failure]
    with pytest.raises(probe.PlanRefusalError, match=f"^{code}$"):
        probe.collect_tail_scaling()
    assert seen == ([2048] if failure == "report_budget" else [2048, 4096])


@pytest.mark.parametrize("damage", ["schema", "rollback", "worker", "performance", "count"])
def test_tail_scaling_refuses_invalid_part_before_collecting_another_population(
    monkeypatch, damage
):
    part = _tail_scaling_part(2048)
    if damage == "schema":
        part["schema_version"] = True
    elif damage == "rollback":
        part["fixture_rows_rolled_back"] = False
    elif damage == "worker":
        part["complete_worker_gate"] = True
    elif damage == "performance":
        part["production_performance_acceptance"] = True
    else:
        part["records"][0]["seeded_rows"] = 4096
    collect = Mock(return_value=part)
    monkeypatch.setattr(probe, "collect", collect)
    category = "SERIES_GRID" if damage == "count" else "SERIES_METADATA"
    with pytest.raises(probe.PlanRefusalError, match=f"^{category}$"):
        probe.collect_tail_scaling()
    collect.assert_called_once_with(profiles=("high-tail",), count=2048)


def test_unknown_preset_refuses_before_source_or_database_admission(monkeypatch):
    admission = Mock(side_effect=AssertionError("admission must remain untouched"))
    monkeypatch.setattr(probe, "admission", admission)
    with pytest.raises(SystemExit) as error:
        probe.main(["--preset", "arbitrary-population"])
    assert error.value.code == 2
    admission.assert_not_called()


def test_manual_workflow_admits_before_private_postgres_allocation():
    document = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / ".github/workflows/polling-benchmark.yml"
        ).read_text()
    )
    job = document["jobs"]["maintenance"]
    assert "services" not in job and job["timeout-minutes"] == 15
    assert "inputs.mode == 'maintenance'" in job["if"]
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    phases = [
        "Admit the owner source and disposable runner",
        "Require the exact-source Linux CI checkpoint",
        "Install only frozen tracked PostgreSQL dependencies",
        "Create only this job's private PostgreSQL container",
        "Start and wait for only the owned PostgreSQL container",
        "Capture fixed migrated-schema maintenance query plans",
    ]
    positions = [names.index(name) for name in phases]
    assert positions == sorted(positions) and len(set(positions)) == len(phases)
    assert all("docker" not in step.get("run", "") for step in steps[: positions[3]])
    assert "--frozen" in steps[positions[2]]["run"]
    assert "--checkpoint" in steps[positions[1]]["run"]
    create = steps[positions[3]]
    assert create["id"] == "postgres" and create["timeout-minutes"] == 2
    preflight = "python -m qualification.maintenance_plans --preflight"
    assert create["run"].index(preflight) < create["run"].index("docker --host")
    command = next(
        line.strip()
        for line in create["run"].replace("\\\n", " ").splitlines()
        if line.lstrip().startswith("docker --host ") and " create " in line
    )
    argv = shlex.split(command)
    assert argv[:4] == ["docker", "--host", "unix:///var/run/docker.sock", "create"]
    for flag, value in {
        "--cpus": "1",
        "--memory": "1g",
        "--memory-swap": "1g",
        "--pids-limit": "128",
        "--publish": "127.0.0.1:5432:5432",
        "--health-interval": "5s",
        "--health-timeout": "5s",
        "--health-retries": "10",
    }.items():
        assert argv.count(flag) == 1 and argv[argv.index(flag) + 1] == value
    assert not {"--mount", "--volume", "-v"}.intersection(argv)
    image = document["jobs"]["benchmark"]["services"]["postgres"]["image"]
    assert re.fullmatch(r"postgres:17@sha256:[0-9a-f]{64}", image) and image in argv
    assert '>> "$GITHUB_OUTPUT"' in create["run"] and " start " not in create["run"]
    start = steps[positions[4]]
    identity = "${{ steps.postgres.outputs.container_id }}"
    assert start["env"]["DIAGNOSTIC_POSTGRES_ID"] == identity and start["timeout-minutes"] == 2
    assert "for _attempt in {1..10}; do" in start["run"] and "starting) sleep 5" in start["run"]
    exact_id = 'test "$actual_id" = "$DIAGNOSTIC_POSTGRES_ID" || refuse'
    assert exact_id in start["run"]
    for step in (create, start):
        assert (
            "unset DOCKER_CONTEXT DOCKER_HOST DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH"
            in step["run"]
        )
    cleanup = next(step for step in steps if step.get("name", "").startswith("Remove only "))
    assert cleanup["if"] == "always() && steps.postgres.outputs.container_id != ''"
    assert cleanup["env"]["DIAGNOSTIC_POSTGRES_ID"] == identity
    assert cleanup["timeout-minutes"] == 1 and exact_id in cleanup["run"]
    assert (
        'docker --host unix:///var/run/docker.sock rm --force --volumes "$DIAGNOSTIC_POSTGRES_ID"'
        in cleanup["run"]
    )
    assert (
        "unset DOCKER_CONTEXT DOCKER_HOST DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH"
        in cleanup["run"]
    )
    assert "docker --host unix:///var/run/docker.sock inspect" in cleanup["run"]
    assert "docker --host unix:///var/run/docker.sock container ls" in cleanup["run"]
    for step in (create, start, cleanup):
        commands = re.findall(r"\bdocker[ \t]+([^\n]+)", step["run"])
        assert commands and all(
            command.startswith("--host unix:///var/run/docker.sock ") for command in commands
        )
    assert not job.get("continue-on-error") and not cleanup.get("continue-on-error")
    artifact = steps[-1]
    assert artifact["if"] == "always()" and artifact["with"]["if-no-files-found"] == "error"
    assert artifact["with"]["path"].splitlines() == [
        "${{ runner.temp }}/maintenance-plans/report.json",
        "${{ runner.temp }}/maintenance-plans/cleanup.json",
    ]


def test_manual_tail_scaling_is_a_fixed_choice_through_the_existing_owned_job():
    document = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / ".github/workflows/polling-benchmark.yml"
        ).read_text()
    )
    trigger = document.get("on", document.get(True))
    mode = trigger["workflow_dispatch"]["inputs"]["mode"]
    assert mode["options"] == ["claim", "maintenance", "maintenance-tail-scaling"]
    assert mode["default"] == "claim"
    assert document["jobs"]["benchmark"]["if"] == (
        "inputs.mode != 'maintenance' && inputs.mode != 'maintenance-tail-scaling'"
    )
    job = document["jobs"]["maintenance"]
    assert "inputs.mode == 'maintenance-tail-scaling'" in job["if"]
    assert "github.actor_id == '15094983'" in job["if"]
    assert "github.repository_id == '790856348'" in job["if"]
    assert "github.repository_owner_id == '15094983'" in job["if"]
    assert job["env"]["MAINTENANCE_PRESET"] == (
        "${{ inputs.mode == 'maintenance-tail-scaling' && 'tail-scaling' || 'fixed' }}"
    )
    commands = [step.get("run", "") for step in job["steps"]]
    preset = '--preset "$MAINTENANCE_PRESET"'
    assert all(
        preset in command for command in commands if "qualification.maintenance_plans" in command
    )
    assert (
        len([command for command in commands if "qualification.maintenance_plans" in command]) == 4
    )
