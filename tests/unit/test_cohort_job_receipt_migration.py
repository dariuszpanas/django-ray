"""Check PostgreSQL DDL composition without opening a database connection."""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps
from django.db import connection


def test_postgresql_receipt_ddl_preserves_literal_percent_without_parameters():
    queries = pytest.importorskip("psycopg._queries")
    adapt = pytest.importorskip("psycopg.adapt")
    migration = importlib.import_module("django_ray.migrations.0029_cohort_job_receipts")
    statements = []

    class PostgreSQLDDLCollector:
        quote_name = staticmethod(connection.ops.quote_name)

        def execute(self, sql, params=()):
            # Django's PostgreSQL schema editor composes DDL with psycopg's
            # client-side parameter conversion before sending it to the server.
            query = queries.PostgresClientQuery(adapt.Transformer())
            query.convert(sql, params)
            statements.append(query.query.decode("utf-8"))

    migration._postgresql(apps, PostgreSQLDDLCollector())

    assert len(statements) == 6
    assert "NEW.ray_address ~ '[@?#%]'" in statements[0]
    assert "CREATE FUNCTION django_ray_guard_probe_job_0029()" in statements[0]
    assert "NEW.request_json::jsonb #>> '{schema_version}' = '2'" in statements[0]
    assert "jsonb_typeof(NEW.request_json::jsonb -> 'target_key') = 'null'" in statements[0]
    assert "jsonb_typeof(NEW.request_json::jsonb -> 'target_key') = 'string'" in statements[0]
    assert all("%%" not in statement for statement in statements)
