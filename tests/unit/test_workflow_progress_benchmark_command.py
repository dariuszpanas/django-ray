"""Tests for the Ray-free workflow-progress storage benchmark."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_ray.management.commands import django_ray_benchmark_workflow_progress as benchmark


def _run_benchmark(tmp_path: Path, *, stem: str = "evidence") -> dict[str, Any]:
    json_path = tmp_path / f"{stem}.json"
    markdown_path = tmp_path / f"{stem}.md"
    stdout = StringIO()
    call_command(
        "django_ray_benchmark_workflow_progress",
        nodes=[4],
        change_rates=[0.5],
        repetitions=1,
        warmups=1,
        seed=7,
        output_json=json_path,
        output_markdown=markdown_path,
        stdout=stdout,
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert "Recorded 49 cases" in stdout.getvalue()
    assert markdown_path.read_text(encoding="utf-8").startswith(
        "# Workflow Progress Storage Benchmark"
    )
    return payload


def test_command_emits_versioned_safe_deterministic_evidence(tmp_path) -> None:
    first = _run_benchmark(tmp_path, stem="first")
    second = _run_benchmark(tmp_path, stem="second")

    assert list(first) == [
        "benchmark",
        "candidates",
        "cases",
        "configuration",
        "database_evidence",
        "environment",
        "profiles",
        "schema_version",
    ]
    assert first["schema_version"] == benchmark.BENCHMARK_SCHEMA_VERSION == 1
    assert first["benchmark"] == "django-ray-workflow-progress-storage"
    assert (
        first["environment"]["source_revision"] == "unavailable"
        or len(first["environment"]["source_revision"]) == 40
    )
    assert len(first["environment"]["benchmark_implementation_sha256"]) == 64
    assert [item["id"] for item in first["profiles"]] == list(benchmark.PROFILE_IDS)
    assert [item["id"] for item in first["candidates"]] == list(benchmark.CANDIDATE_IDS)
    assert len(first["cases"]) == len(benchmark.PROFILE_IDS) * len(benchmark.CANDIDATE_IDS)
    assert first["database_evidence"]["status"] == "unavailable"
    assert first["database_evidence"]["vendor"] == "sqlite"
    assert first["configuration"]["execution_scope"] == (
        "configured Django database only; no Ray or Kubernetes access"
    )
    assert "benchmark-secret" not in json.dumps(first)

    first_identity = [
        (case["workload_fingerprint"], case["write_amplification"]) for case in first["cases"]
    ]
    second_identity = [
        (case["workload_fingerprint"], case["write_amplification"]) for case in second["cases"]
    ]
    assert first_identity == second_identity
    assert all(len(case["warm_samples"]) == 1 for case in first["cases"])
    assert all(case["changed_nodes"] == 2 for case in first["cases"])
    sample = first["cases"][0]["cold_sample"]
    assert sample["encoded_bytes"] > 0
    assert sample["decoded_bytes"] > sample["encoded_bytes"]
    assert sample["collector_peak_bytes"] >= 0


def test_command_prints_markdown_when_no_output_paths() -> None:
    stdout = StringIO()
    call_command(
        "django_ray_benchmark_workflow_progress",
        nodes=[1],
        change_rates=[1.0],
        repetitions=1,
        warmups=0,
        seed=8,
        stdout=stdout,
    )

    output = stdout.getvalue()
    assert output.startswith("# Workflow Progress Storage Benchmark")
    assert "`normalized`" in output
    assert "does not start Ray or access Kubernetes" in output
    assert "Database evidence: **unavailable** (sqlite)." in output


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([], "at least one"),
        ([0], "positive integers"),
        ([True], "positive integers"),
        ([1, 1], "unique"),
        ("1", "at least one"),
    ],
)
def test_node_validation(values, message) -> None:
    with pytest.raises(CommandError, match=message):
        benchmark.Command._nodes(values)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([], "at least one"),
        ([0.0], "finite numbers"),
        ([1.1], "finite numbers"),
        ([float("nan")], "finite numbers"),
        ([True], "finite numbers"),
        ([0.5, 0.5], "unique"),
        ("0.5", "at least one"),
    ],
)
def test_change_rate_validation(values, message) -> None:
    with pytest.raises(CommandError, match=message):
        benchmark.Command._change_rates(values)


@pytest.mark.parametrize(
    ("method", "value", "message"),
    [
        ("_positive_int", 0, "positive integer"),
        ("_positive_int", True, "positive integer"),
        ("_non_negative_int", -1, "non-negative integer"),
        ("_non_negative_int", False, "non-negative integer"),
        ("_integer", 1.5, "integer"),
        ("_integer", True, "integer"),
    ],
)
def test_scalar_validation(method, value, message) -> None:
    with pytest.raises(CommandError, match=message):
        getattr(benchmark.Command, method)(value, "--value")


def test_validators_preserve_valid_values() -> None:
    assert benchmark.Command._nodes([2, 1]) == [2, 1]
    assert benchmark.Command._change_rates([1, 0.25]) == [1.0, 0.25]
    assert benchmark.Command._positive_int(2, "--value") == 2
    assert benchmark.Command._non_negative_int(0, "--value") == 0
    assert benchmark.Command._integer(-2, "--value") == -2


def test_output_paths_must_be_distinct(tmp_path) -> None:
    output = tmp_path / "same"
    with pytest.raises(CommandError, match="must be different paths"):
        call_command(
            "django_ray_benchmark_workflow_progress",
            nodes=[1],
            change_rates=[1.0],
            repetitions=1,
            warmups=0,
            output_json=output,
            output_markdown=output,
        )


@pytest.mark.parametrize("profile", benchmark.PROFILE_IDS)
def test_profiles_are_bounded_redacted_json(profile) -> None:
    record = benchmark._profile_record(profile, seed=53)
    encoded = benchmark._canonical_bytes(record)

    assert json.loads(encoded) == record
    assert b"benchmark-secret" not in encoded
    assert record["node_id"] == "node-000001"


def test_profile_rejects_unknown_identifier(monkeypatch) -> None:
    with pytest.raises(ValueError, match="unknown benchmark profile"):
        benchmark._profile_record("unknown", seed=1)

    monkeypatch.setattr(benchmark, "redact_value", lambda _value: [])
    with pytest.raises(RuntimeError, match="redaction did not return a mapping"):
        benchmark._profile_record("short", seed=1)


@pytest.mark.parametrize("candidate", benchmark.CANDIDATE_IDS)
def test_candidate_models_and_samples_are_finite(candidate) -> None:
    record = benchmark._profile_record("short", seed=1)
    record_bytes = len(benchmark._canonical_bytes(record))
    amplification = benchmark._write_amplification(
        candidate,
        nodes=1_000,
        change_rate=0.01,
        record_bytes=record_bytes,
    )
    count = benchmark._representative_count(
        candidate,
        nodes=1_000,
        changed_items=amplification["changed_items"],
        record_bytes=record_bytes,
    )
    payload = benchmark._representative_payload(
        candidate,
        record=record,
        record_count=count,
        nodes=1_000,
        change_rate=0.01,
    )
    sample = benchmark._measure_sample(
        candidate,
        record=record,
        record_count=count,
        nodes=1_000,
        change_rate=0.01,
    )

    assert amplification["changed_items"] == 10
    assert amplification["total_bytes"] >= amplification["task_bytes"]
    assert amplification["estimated_database_statements"] >= 1
    assert amplification["touched_unit_kind"]
    assert len(payload["records"]) == count
    assert sample["encoded_bytes"] == len(benchmark._canonical_bytes(payload))
    assert sample["modeled_nodes"] == 1_000
    assert sample["representative_records"] == count


def test_candidate_helpers_reject_unknown_identifier() -> None:
    with pytest.raises(ValueError, match="unknown benchmark candidate"):
        benchmark._write_amplification(
            "unknown",
            nodes=1,
            change_rate=1.0,
            record_bytes=1,
        )


def test_page_and_size_helpers_cover_boundaries() -> None:
    assert benchmark._expected_changed_pages(total_pages=0, changed_items=1) == 0
    assert benchmark._expected_changed_pages(total_pages=2, changed_items=0) == 0
    assert benchmark._expected_changed_pages(total_pages=1, changed_items=1) == 1
    assert benchmark._expected_changed_pages(total_pages=100, changed_items=100_000) == 100
    assert benchmark._workload_fingerprint(
        seed=1, profile="short", nodes=2, change_rate=0.5
    ) == benchmark._workload_fingerprint(seed=1, profile="short", nodes=2, change_rate=0.5)
    shared: list[object] = []
    assert benchmark._deep_size({"a": shared, "b": shared}) > 0
    assert benchmark._median([]) == 0.0
    assert benchmark._median([1, 3]) == 2.0


def test_atomic_writer_replaces_existing_file(tmp_path) -> None:
    path = tmp_path / "nested" / "evidence.json"
    benchmark._write_atomic(path, "first\n")
    benchmark._write_atomic(path, "second\n")

    assert path.read_text(encoding="utf-8") == "second\n"
    assert list(path.parent.glob("*.tmp")) == []


def test_atomic_writer_removes_temporary_file_after_replace_failure(tmp_path, monkeypatch) -> None:
    path = tmp_path / "evidence.json"

    def fail_replace(_self, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        benchmark._write_atomic(path, "evidence\n")

    assert list(tmp_path.iterdir()) == []


def test_package_and_schema_metadata_fallbacks(monkeypatch) -> None:
    monkeypatch.setattr(
        benchmark,
        "version",
        Mock(side_effect=benchmark.PackageNotFoundError),
    )
    assert benchmark._package_version() == "source-checkout"
    assert benchmark._dependency_version("psycopg") == "unavailable"

    class BrokenRecorder:
        def __init__(self, _connection) -> None:
            raise RuntimeError("no schema")

    monkeypatch.setattr(benchmark, "MigrationRecorder", BrokenRecorder)
    assert benchmark._schema_version() == "unavailable:RuntimeError"
    empty_recorder = Mock()
    empty_recorder.return_value.applied_migrations.return_value = set()
    monkeypatch.setattr(benchmark, "MigrationRecorder", empty_recorder)
    assert benchmark._schema_version() == "unmigrated"


def test_source_revision_is_strict_and_normalized(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "A" * 40)
    assert benchmark._source_revision() == "a" * 40

    monkeypatch.setenv("GITHUB_SHA", "not-a-commit")
    assert benchmark._source_revision() == "unavailable"


@pytest.mark.parametrize("value", ["", "has spaces", "secret=value", "../path", 1])
def test_metadata_label_rejects_unsafe_values(value) -> None:
    with pytest.raises(CommandError, match="non-secret identifier"):
        benchmark.Command._metadata_label(value, "--label")


def test_metadata_label_preserves_safe_value() -> None:
    assert (
        benchmark.Command._metadata_label(
            "docker-desktop:postgres:17@sha256:abc",
            "--label",
        )
        == "docker-desktop:postgres:17@sha256:abc"
    )


def test_rss_bytes_handles_linux_and_macos_units(monkeypatch) -> None:
    fake_resource = SimpleNamespace(
        RUSAGE_SELF=1,
        getrusage=lambda _kind: SimpleNamespace(ru_maxrss=2),
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(benchmark.sys, "platform", "linux")
    assert benchmark._rss_bytes() == 2_048
    monkeypatch.setattr(benchmark.sys, "platform", "darwin")
    assert benchmark._rss_bytes() == 2


class _FakeCursor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.relation_calls = 0
        self.result = None
        self.rows: list[tuple[str, int]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql, params=None) -> None:
        del params
        if self.fail and sql.startswith(("CREATE TABLE", "DROP TABLE")):
            raise RuntimeError("probe failed")
        if "pg_total_relation_size" in sql:
            self.relation_calls += 1
            self.result = (100 if self.relation_calls == 1 else 180,)
        elif "pg_current_wal_insert_lsn" in sql and "pg_wal_lsn_diff" not in sql:
            self.result = ("0/1",)
        elif "pg_wal_lsn_diff" in sql:
            self.result = (40,)
        elif "pg_column_size" in sql:
            self.rows = [("short", 12)]

    def executemany(self, _sql, rows) -> None:
        self.rows = [(str(row[0]), 12) for row in rows]

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.rows


class _FakeConnection:
    vendor = "postgresql"
    ops = SimpleNamespace(quote_name=lambda name: f'"{name}"')

    def __init__(self, *, fail: bool = False) -> None:
        self.cursor_object = _FakeCursor(fail=fail)

    def cursor(self):
        return self.cursor_object


def test_postgresql_database_evidence_and_failure(monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "connection", _FakeConnection())
    evidence = benchmark._database_evidence({"short": {"summary": {"state": "RUNNING"}}})

    assert evidence == {
        "status": "available",
        "vendor": "postgresql",
        "probe": "transient-jsonb-table",
        "rows": 1,
        "column_size_bytes": {"short": 12},
        "empty_relation_bytes": 100,
        "relation_bytes": 180,
        "relation_growth_bytes": 80,
        "wal_bytes": 40,
        "table_cleaned": True,
        "limitations": [
            "The probe uses bounded representative documents, not full modeled graphs.",
            "The probe does not execute candidate schemas or measure round-trip throughput.",
            "WAL and relation sizes depend on PostgreSQL settings and concurrent activity.",
        ],
    }

    monkeypatch.setattr(benchmark, "connection", _FakeConnection(fail=True))
    failed = benchmark._database_evidence({"short": {}})
    assert failed["status"] == "unavailable"
    assert failed["reason"] == "RuntimeError: database probe failed"
    assert failed["table_cleaned"] is False


def test_markdown_reports_available_database() -> None:
    payload = {
        "cases": [
            {
                "candidate": candidate,
                "profile": "short",
                "nodes": 100,
                "change_rate": 0.01,
                "write_amplification": {
                    "modeled_touched_units": 1,
                    "touched_unit_kind": "expected_random_page",
                    "total_bytes": 10,
                    "estimated_database_statements": 2,
                },
                "warm_samples": [{"serialize_ms": 1.5}],
            }
            for candidate in benchmark.CANDIDATE_IDS
        ],
        "database_evidence": {
            "status": "available",
            "vendor": "postgresql",
            "relation_growth_bytes": 100,
            "wal_bytes": 50,
            "table_cleaned": True,
        },
    }

    markdown = benchmark._markdown(payload)

    assert "**available** (postgresql)" in markdown
    assert "\nRepresentative relation growth" in markdown
    assert "| `live_only` | 10 | 2.0 | 1.5000 |" in markdown
    assert "| `live_only` | `expected_random_page` | 1 | 2 | 10 |" in markdown
