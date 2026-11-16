"""Hosted-only owned PostgreSQL database; no Ray, worker turns or shared data."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
from django.db import connection, connections

from qualification import maintenance_plans as probe

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def require_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("requires the hosted PostgreSQL coordination service")


@contextmanager
def fresh_database():
    from django.db.backends.postgresql.base import DatabaseWrapper

    original = connections["default"]
    with original.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = 'maintenance_plans'")
        if cursor.fetchone() is not None:
            pytest.fail("refuse to adopt an existing diagnostic database")
        cursor.execute('CREATE DATABASE "maintenance_plans" OWNER django_ray TEMPLATE template0')
    configuration = deepcopy(original.settings_dict)
    configuration["NAME"] = "maintenance_plans"
    owned = DatabaseWrapper(configuration, alias="default")
    connections["default"] = owned
    try:
        yield owned
    finally:
        owned.close()
        connections["default"] = original
        with original.cursor() as cursor:
            cursor.execute('DROP DATABASE "maintenance_plans"')


def test_fresh_database_entry_migrates_captures_actual_plans_and_rolls_back(monkeypatch):
    with fresh_database() as owned:
        probe.prepare_database(owned)
        summarize = probe.plan_summary

        def inspect_deadlines(raw):
            with owned.cursor() as cursor:
                cursor.execute("SHOW statement_timeout")
                assert cursor.fetchone()[0] == "2s"
                cursor.execute("SHOW lock_timeout")
                assert cursor.fetchone()[0] == "500ms"
            return summarize(raw)

        monkeypatch.setattr(probe, "plan_summary", inspect_deadlines)
        report = probe.collect(profiles=("dense",), count=384, queue_groups=(None,))
        assert len(report["records"]) == 12
        assert report["fixture_rows_rolled_back"] is True
        assert report["complete_worker_gate"] is False
        assert report["indexes"] and report["migrations"]
        assert {r["phase"] for r in report["records"]} == {"high-water", "first", "continuation"}
        assert all(r["nodes"] <= 64 for r in report["records"])
        assert any(r["reconciled_active_page_ids"] for r in report["records"])
        assert any("Shared Hit Blocks" in r["plan"] for r in report["records"])
        probe.fixture_empty()
        # This second entry is refused before migrations: owned does not mean fresh.
        with pytest.raises(probe.PlanRefusalError, match="DATABASE_NOT_FRESH"):
            probe.prepare_database(owned)


def test_plan_failure_rolls_back_owned_fixture_rows(monkeypatch):
    with fresh_database() as owned:
        probe.prepare_database(owned)

        def refuse(_value):
            raise probe.PlanRefusalError("PLAN_BUDGET")

        monkeypatch.setattr(probe, "plan_summary", refuse)
        with pytest.raises(probe.PlanRefusalError, match="PLAN_BUDGET"):
            probe.collect(profiles=("dense",), count=384, queue_groups=(None,))
        probe.fixture_empty()
        with owned.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            assert cursor.fetchone()[0] == "15s"
            cursor.execute("SHOW lock_timeout")
            assert cursor.fetchone()[0] == "2s"


def test_nonempty_catalog_refuses_before_migrate(monkeypatch):
    with fresh_database() as owned:
        with owned.cursor() as cursor:
            cursor.execute("CREATE TABLE unknown_fixture (id integer)")
        migrate = []
        monkeypatch.setattr(
            "django.core.management.call_command", lambda *a, **kw: migrate.append(a)
        )
        with pytest.raises(probe.PlanRefusalError, match="DATABASE_NOT_FRESH"):
            probe.prepare_database(owned)
        assert not migrate
        with owned.cursor() as cursor:
            cursor.execute("SELECT to_regclass('unknown_fixture')")
            assert cursor.fetchone()[0] == "unknown_fixture"
