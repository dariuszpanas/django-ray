"""Compare bounded workflow-progress storage shapes without importing Ray."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import re
import statistics
import sys
import time
import tracemalloc
import uuid
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, TypedDict, cast

import django
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from django_ray.redaction import redact_value

BENCHMARK_SCHEMA_VERSION = 1
PAGE_ITEMS = 256
SUMMARY_BYTES = 2_048
REPRESENTATIVE_BYTES = 128 * 1_024
DEFAULT_NODES = (1_000, 10_000, 50_000, 100_000)
DEFAULT_CHANGE_RATES = (0.01, 0.10, 1.0)

PROFILE_IDS = (
    "short",
    "max_size",
    "multibyte_utf8",
    "secret_bearing",
    "metric_cardinality",
    "compressible",
    "incompressible",
)
CANDIDATE_IDS = (
    "current_full_row",
    "bounded_inline",
    "chunked_database",
    "normalized",
    "append_delta",
    "external_chunk",
    "live_only",
)

_PROFILE_DESCRIPTIONS = {
    "short": "Short labels and one scalar metric.",
    "max_size": "Near-limit labels, messages, errors, and metric strings.",
    "multibyte_utf8": "Multibyte UTF-8 values measured by encoded bytes.",
    "secret_bearing": "Sensitive keys and values redacted before modeling.",
    "metric_cardinality": "Thirty-two bounded application metric keys.",
    "compressible": "Repeated content with a high potential compression ratio.",
    "incompressible": "Seeded high-entropy hexadecimal content.",
}
_CANDIDATE_DESCRIPTIONS = {
    "current_full_row": "Rewrite one complete task-row graph snapshot.",
    "bounded_inline": "Bound the task row and retain a small inline preview.",
    "chunked_database": "Write a bounded summary plus changed database pages.",
    "normalized": "Batch-update independently addressable latest-state rows.",
    "append_delta": "Append bounded change records plus a summary pointer.",
    "external_chunk": "Write a database summary/manifest and external pages.",
    "live_only": "Persist only a bounded summary; keep detail live-only.",
}


class _WriteAmplification(TypedDict):
    changed_items: int
    modeled_touched_units: int
    touched_unit_kind: str
    estimated_database_statements: int
    task_bytes: int
    detail_bytes: int
    external_bytes: int
    total_bytes: int


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _package_version() -> str:
    try:
        return version("django-ray")
    except PackageNotFoundError:
        return "source-checkout"


def _dependency_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def _source_revision() -> str:
    revision = os.environ.get("GITHUB_SHA", "")
    return revision.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", revision) else "unavailable"


def _implementation_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _schema_version() -> str:
    try:
        applied = sorted(
            name
            for app, name in MigrationRecorder(connection).applied_migrations()
            if app == "django_ray"
        )
    except Exception as error:  # pragma: no cover - backend-specific defensive path
        return f"unavailable:{type(error).__name__}"
    return applied[-1] if applied else "unmigrated"


def _rss_bytes() -> int | None:
    try:
        import resource

        usage = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        return None
    return usage if sys.platform == "darwin" else usage * 1024


def _deep_size(value: object, seen: set[int] | None = None) -> int:
    visited = set() if seen is None else seen
    object_id = id(value)
    if object_id in visited:
        return 0
    visited.add(object_id)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(
            _deep_size(key, visited) + _deep_size(item, visited) for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return size + sum(_deep_size(item, visited) for item in value)
    return size


def _profile_record(profile: str, *, seed: int) -> dict[str, object]:
    common: dict[str, object] = {
        "node_id": "node-000001",
        "label": "sync namespace",
        "state": "RUNNING",
        "message": "reconciling",
        "error": None,
        "metrics": {"objects": 1},
    }
    if profile == "max_size":
        common.update(
            label="L" * 512,
            message="M" * 2_048,
            error="E" * 2_048,
            metrics={f"metric_{index:02d}": "V" * 96 for index in range(32)},
        )
    elif profile == "multibyte_utf8":
        common.update(
            label="界" * 128,
            message="進捗" * 256,
            metrics={"処理済み": "値" * 64},
        )
    elif profile == "secret_bearing":
        common.update(
            message="authorization bearer benchmark-secret",
            metrics={"api_token": "benchmark-secret", "objects": 1},
        )
    elif profile == "metric_cardinality":
        common["metrics"] = {f"metric_{index:02d}": index for index in range(32)}
    elif profile == "compressible":
        common.update(message="A" * 2_048, metrics={"payload": "A" * 2_048})
    elif profile == "incompressible":
        generator = random.Random(seed)
        common.update(
            message=f"entropy-{generator.getrandbits(8_192):02048x}",
            metrics={"payload": f"{generator.getrandbits(8_192):02048x}"},
        )
    elif profile != "short":
        raise ValueError(f"unknown benchmark profile: {profile}")
    redacted = redact_value(common)
    if not isinstance(redacted, dict):  # pragma: no cover - redaction contract guard
        raise RuntimeError("redaction did not return a mapping")
    return redacted


def _workload_fingerprint(
    *,
    seed: int,
    profile: str,
    nodes: int,
    change_rate: float,
) -> str:
    material = f"v1:{seed}:{profile}:{nodes}:{change_rate:.12g}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _expected_changed_pages(*, total_pages: int, changed_items: int) -> int:
    if total_pages <= 0 or changed_items <= 0:
        return 0
    if total_pages == 1:
        return 1
    untouched_probability = ((total_pages - 1) / total_pages) ** changed_items
    return min(total_pages, max(1, math.ceil(total_pages * (1 - untouched_probability))))


def _write_amplification(
    candidate: str,
    *,
    nodes: int,
    change_rate: float,
    record_bytes: int,
) -> _WriteAmplification:
    changed_items = min(nodes, max(1, math.ceil(nodes * change_rate)))
    total_pages = math.ceil(nodes / PAGE_ITEMS)
    touched_units = _expected_changed_pages(
        total_pages=total_pages,
        changed_items=changed_items,
    )
    touched_unit_kind = "expected_random_page"
    page_bytes = min(256 * 1_024, PAGE_ITEMS * (record_bytes + 48))
    full_graph_bytes = SUMMARY_BYTES + nodes * (record_bytes + 96)
    task_bytes = SUMMARY_BYTES
    detail_bytes = 0
    external_bytes = 0
    statements = 1

    if candidate == "current_full_row":
        task_bytes = full_graph_bytes
        touched_units = total_pages
        touched_unit_kind = "full_graph_page_equivalent"
    elif candidate == "bounded_inline":
        task_bytes = min(16 * 1_024, SUMMARY_BYTES + min(nodes, 32) * record_bytes)
        touched_units = 0
        touched_unit_kind = "none"
    elif candidate == "chunked_database":
        detail_bytes = touched_units * page_bytes
        statements = 2 + touched_units
    elif candidate == "normalized":
        touched_units = 0
        touched_unit_kind = "none"
        detail_bytes = changed_items * (record_bytes + 96)
        statements = 2 + math.ceil(changed_items / 500)
    elif candidate == "append_delta":
        touched_units = math.ceil(changed_items / PAGE_ITEMS)
        touched_unit_kind = "delta_chunk"
        detail_bytes = changed_items * (record_bytes + 64)
        statements = 2 + touched_units
    elif candidate == "external_chunk":
        external_bytes = touched_units * page_bytes
        detail_bytes = 1_024 + touched_units * 96
        statements = 2
    elif candidate == "live_only":
        touched_units = 0
        touched_unit_kind = "none"
    else:
        raise ValueError(f"unknown benchmark candidate: {candidate}")

    return {
        "changed_items": changed_items,
        "modeled_touched_units": touched_units,
        "touched_unit_kind": touched_unit_kind,
        "estimated_database_statements": statements,
        "task_bytes": task_bytes,
        "detail_bytes": detail_bytes,
        "external_bytes": external_bytes,
        "total_bytes": task_bytes + detail_bytes + external_bytes,
    }


def _representative_count(
    candidate: str,
    *,
    nodes: int,
    changed_items: int,
    record_bytes: int,
) -> int:
    if candidate == "live_only":
        return 0
    if candidate == "current_full_row":
        desired = min(nodes, 512)
    elif candidate == "bounded_inline":
        desired = min(nodes, 32)
    else:
        desired = min(changed_items, PAGE_ITEMS)
    byte_limited = max(1, REPRESENTATIVE_BYTES // max(1, record_bytes))
    return min(desired, byte_limited)


def _representative_payload(
    candidate: str,
    *,
    record: dict[str, object],
    record_count: int,
    nodes: int,
    change_rate: float,
) -> dict[str, object]:
    summary = {
        "schema_version": 3,
        "revision": 7,
        "state": "RUNNING",
        "total_nodes": nodes,
        "change_rate": change_rate,
        "detail_available": candidate != "live_only",
    }
    records = [{**record, "node_id": f"node-{index:06d}"} for index in range(record_count)]
    if candidate == "external_chunk":
        return {
            "summary": summary,
            "manifest": {"opaque_id": "benchmark", "page_items": record_count},
            "records": records,
        }
    if candidate == "live_only":
        return {"summary": summary, "records": []}
    return {"summary": summary, "records": records}


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1_000


def _measure_sample(
    candidate: str,
    *,
    record: dict[str, object],
    record_count: int,
    nodes: int,
    change_rate: float,
) -> dict[str, int | float | None]:
    tracemalloc.start()
    started = time.perf_counter()
    payload = _representative_payload(
        candidate,
        record=record,
        record_count=record_count,
        nodes=nodes,
        change_rate=change_rate,
    )
    construct_ms = _elapsed_ms(started)
    _, collector_peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    started = time.perf_counter()
    encoded = _canonical_bytes(payload)
    serialize_ms = _elapsed_ms(started)
    started = time.perf_counter()
    decoded = json.loads(encoded)
    parse_ms = _elapsed_ms(started)
    started = time.perf_counter()
    redacted = redact_value(decoded)
    redact_ms = _elapsed_ms(started)

    started = time.perf_counter()
    summary_response = _canonical_bytes(redacted["summary"])
    summary_read_ms = _elapsed_ms(started)
    records = redacted.get("records", [])
    page = records[:100] if isinstance(records, list) else []
    started = time.perf_counter()
    page_response = _canonical_bytes({"items": page})
    page_read_ms = _elapsed_ms(started)

    target = f"node-{max(0, record_count - 1):06d}"
    index = {item.get("node_id"): item for item in page if isinstance(item, dict)}
    started = time.perf_counter()
    if candidate in {"current_full_row", "bounded_inline"}:
        single = next(
            (item for item in page if isinstance(item, dict) and item.get("node_id") == target),
            None,
        )
    else:
        single = index.get(target)
    single_node_read_ms = _elapsed_ms(started)
    single_response = _canonical_bytes(single)

    return {
        "construct_ms": construct_ms,
        "serialize_ms": serialize_ms,
        "parse_ms": parse_ms,
        "redact_ms": redact_ms,
        "summary_read_ms": summary_read_ms,
        "page_read_ms": page_read_ms,
        "single_node_read_ms": single_node_read_ms,
        "encoded_bytes": len(encoded),
        "decoded_bytes": _deep_size(decoded),
        "summary_response_bytes": len(summary_response),
        "page_response_bytes": len(page_response),
        "single_node_response_bytes": len(single_response),
        "collector_peak_bytes": collector_peak_bytes,
        "rss_bytes": _rss_bytes(),
        "representative_records": record_count,
        "modeled_nodes": nodes,
    }


def _database_evidence(documents: dict[str, dict[str, object]]) -> dict[str, object]:
    if connection.vendor != "postgresql":
        return {
            "status": "unavailable",
            "reason": f"requires PostgreSQL; active vendor is {connection.vendor}",
            "vendor": connection.vendor,
        }

    table = f"django_ray_progress_benchmark_{uuid.uuid4().hex}"
    quoted = connection.ops.quote_name(table)
    cleaned = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE {quoted} (candidate varchar(64) PRIMARY KEY, payload jsonb NOT NULL)"
            )
            cursor.execute("SELECT pg_total_relation_size(%s::regclass)", [table])
            empty_relation_bytes = int(cursor.fetchone()[0])
            cursor.execute("SELECT pg_current_wal_insert_lsn()")
            starting_lsn = cursor.fetchone()[0]
            cursor.executemany(
                f"INSERT INTO {quoted} (candidate, payload) VALUES (%s, %s::jsonb)",
                [
                    (candidate, _canonical_bytes(document).decode("utf-8"))
                    for candidate, document in documents.items()
                ],
            )
            cursor.execute(
                f"SELECT candidate, pg_column_size(payload) FROM {quoted} ORDER BY candidate"
            )
            column_sizes = {str(candidate): int(size) for candidate, size in cursor.fetchall()}
            cursor.execute("SELECT pg_total_relation_size(%s::regclass)", [table])
            relation_bytes = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s)",
                [starting_lsn],
            )
            wal_bytes = int(cursor.fetchone()[0])
        evidence: dict[str, object] = {
            "status": "available",
            "vendor": "postgresql",
            "probe": "transient-jsonb-table",
            "rows": len(documents),
            "column_size_bytes": column_sizes,
            "empty_relation_bytes": empty_relation_bytes,
            "relation_bytes": relation_bytes,
            "relation_growth_bytes": max(0, relation_bytes - empty_relation_bytes),
            "wal_bytes": wal_bytes,
            "limitations": [
                "The probe uses bounded representative documents, not full modeled graphs.",
                "The probe does not execute candidate schemas or measure round-trip throughput.",
                "WAL and relation sizes depend on PostgreSQL settings and concurrent activity.",
            ],
        }
    except Exception as error:
        evidence = {
            "status": "unavailable",
            "vendor": "postgresql",
            "reason": f"{type(error).__name__}: database probe failed",
        }
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
            cleaned = True
        except Exception:
            cleaned = False
    evidence["table_cleaned"] = cleaned
    return evidence


def _environment(*, database_deployment: str) -> dict[str, object]:
    database_version = getattr(connection, "pg_version", None)
    return {
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "python_version": platform.python_version(),
        "django_version": django.get_version(),
        "django_ray_version": _package_version(),
        "psycopg_version": _dependency_version("psycopg"),
        "source_revision": _source_revision(),
        "benchmark_implementation_sha256": _implementation_digest(),
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "database_vendor": connection.vendor,
        "database_server_version": str(database_version or "unknown"),
        "database_deployment": database_deployment,
        "django_ray_schema_version": _schema_version(),
        "timer": "time.perf_counter",
        "rss_available": _rss_bytes() is not None,
    }


def _median(values: list[float | int]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _markdown(payload: dict[str, object]) -> str:
    cases = cast(list[dict[str, Any]], payload["cases"])
    environment = cast(dict[str, Any], payload.get("environment", {}))
    configuration = cast(dict[str, Any], payload.get("configuration", {}))
    rows = [
        "# Workflow Progress Storage Benchmark",
        "",
        "Raw JSON is authoritative. This summary uses medians across all modeled cases and warm samples.",
        "",
        f"Environment: `{environment.get('platform', 'unknown')}`; Python "
        f"`{environment.get('python_version', 'unknown')}`; "
        f"{environment.get('database_vendor', 'unknown')} "
        f"`{environment.get('database_server_version', 'unknown')}`.",
        "",
        f"Collected: `{environment.get('collected_at_utc', 'unknown')}`; database deployment: "
        f"`{environment.get('database_deployment', 'unspecified')}`; psycopg "
        f"`{environment.get('psycopg_version', 'unavailable')}`.",
        "",
        f"Source revision: `{environment.get('source_revision', 'unavailable')}`; "
        f"benchmark implementation SHA-256: "
        f"`{environment.get('benchmark_implementation_sha256', 'unavailable')}`.",
        "",
        f"Matrix: nodes `{configuration.get('nodes', 'unknown')}`; change rates "
        f"`{configuration.get('change_rates', 'unknown')}`; repetitions "
        f"`{configuration.get('repetitions', 'unknown')}`; seed "
        f"`{configuration.get('seed', 'unknown')}`.",
        "",
        "Execution scope: configured Django database only; this command does not start "
        "Ray or access Kubernetes.",
        "",
        "Python timings cover bounded in-process structures. Write bytes and database "
        "statement counts are analytical; the representative JSONB probe does not measure "
        "candidate-schema round trips or throughput.",
        "",
        "| Candidate | Median total write bytes | Median estimated DB statements | Median warm serialize (ms) |",
        "|---|---:|---:|---:|",
    ]
    for candidate in CANDIDATE_IDS:
        selected = [case for case in cases if case["candidate"] == candidate]
        total_bytes = [case["write_amplification"]["total_bytes"] for case in selected]
        statements = [
            case["write_amplification"]["estimated_database_statements"] for case in selected
        ]
        warm_serialize = [
            sample["serialize_ms"] for case in selected for sample in case["warm_samples"]
        ]
        rows.append(
            f"| `{candidate}` | {_median(total_bytes):.0f} | "
            f"{_median(statements):.1f} | {_median(warm_serialize):.4f} |"
        )

    focus_nodes = max((case["nodes"] for case in cases), default=None)
    focus_rate = min((case["change_rate"] for case in cases), default=None)
    focus = [
        case
        for case in cases
        if case["profile"] == "short"
        and case["nodes"] == focus_nodes
        and case["change_rate"] == focus_rate
    ]
    if focus:
        rows.extend(
            [
                "",
                f"## Sparse short-profile focus ({focus_nodes} nodes, {float(focus_rate):.0%} changed)",
                "",
                "| Candidate | Modeled unit kind | Touched units | Estimated DB statements | Total write bytes |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for case in focus:
            amplification = case["write_amplification"]
            rows.append(
                f"| `{case['candidate']}` | `{amplification['touched_unit_kind']}` | "
                f"{amplification['modeled_touched_units']} | "
                f"{amplification['estimated_database_statements']} | "
                f"{amplification['total_bytes']} |"
            )

    database = cast(dict[str, Any], payload["database_evidence"])
    rows.extend(
        [
            "",
            f"Database evidence: **{database.get('status', 'unknown')}** "
            f"({database.get('vendor', 'unknown')}).",
            (
                f"Representative relation growth: `{database['relation_growth_bytes']}` bytes; "
                f"WAL: `{database['wal_bytes']}` bytes; table cleaned: "
                f"`{database.get('table_cleaned', 'unknown')}`."
                if database.get("status") == "available"
                else f" Reason: {database.get('reason', 'not reported')}."
            ),
            "",
            "Timings are observations, not CI thresholds or service-level objectives.",
            "",
        ]
    )
    return "\n".join(rows)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


class Command(BaseCommand):
    """Emit reproducible evidence for bounded progress-storage decisions."""

    help = "Benchmark workflow-progress storage shapes without importing Ray"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--nodes", nargs="+", type=int, default=list(DEFAULT_NODES))
        parser.add_argument(
            "--change-rates",
            nargs="+",
            type=float,
            default=list(DEFAULT_CHANGE_RATES),
        )
        parser.add_argument("--repetitions", type=int, default=5)
        parser.add_argument("--warmups", type=int, default=1)
        parser.add_argument("--seed", type=int, default=20260720)
        parser.add_argument("--database-deployment", default="unspecified")
        parser.add_argument("--output-json", type=Path)
        parser.add_argument("--output-markdown", type=Path)

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        nodes = self._nodes(options["nodes"])
        change_rates = self._change_rates(options["change_rates"])
        repetitions = self._positive_int(options["repetitions"], "--repetitions")
        warmups = self._non_negative_int(options["warmups"], "--warmups")
        seed = self._integer(options["seed"], "--seed")
        database_deployment = self._metadata_label(
            options["database_deployment"],
            "--database-deployment",
        )
        output_json = options["output_json"]
        output_markdown = options["output_markdown"]
        if output_json is not None and output_markdown is not None:
            if output_json.resolve() == output_markdown.resolve():
                raise CommandError("--output-json and --output-markdown must be different paths")

        profiles = []
        profile_records: dict[str, dict[str, object]] = {}
        for profile in PROFILE_IDS:
            record = _profile_record(profile, seed=seed)
            profile_records[profile] = record
            profiles.append(
                {
                    "id": profile,
                    "description": _PROFILE_DESCRIPTIONS[profile],
                    "record_encoded_bytes": len(_canonical_bytes(record)),
                }
            )

        cases: list[dict[str, object]] = []
        probe_documents: dict[str, dict[str, object]] = {}
        for profile in PROFILE_IDS:
            record = profile_records[profile]
            record_bytes = len(_canonical_bytes(record))
            for node_count in nodes:
                for change_rate in change_rates:
                    for candidate in CANDIDATE_IDS:
                        amplification = _write_amplification(
                            candidate,
                            nodes=node_count,
                            change_rate=change_rate,
                            record_bytes=record_bytes,
                        )
                        record_count = _representative_count(
                            candidate,
                            nodes=node_count,
                            changed_items=amplification["changed_items"],
                            record_bytes=record_bytes,
                        )
                        sample_arguments = {
                            "record": record,
                            "record_count": record_count,
                            "nodes": node_count,
                            "change_rate": change_rate,
                        }
                        cold_sample = _measure_sample(candidate, **sample_arguments)
                        for _ in range(warmups):
                            _measure_sample(candidate, **sample_arguments)
                        warm_samples = [
                            _measure_sample(candidate, **sample_arguments)
                            for _ in range(repetitions)
                        ]
                        cases.append(
                            {
                                "profile": profile,
                                "candidate": candidate,
                                "nodes": node_count,
                                "change_rate": change_rate,
                                "changed_nodes": amplification["changed_items"],
                                "workload_fingerprint": _workload_fingerprint(
                                    seed=seed,
                                    profile=profile,
                                    nodes=node_count,
                                    change_rate=change_rate,
                                ),
                                "write_amplification": amplification,
                                "cold_sample": cold_sample,
                                "warm_samples": warm_samples,
                            }
                        )
                        if (
                            profile == "short"
                            and node_count == nodes[0]
                            and change_rate == change_rates[0]
                        ):
                            probe_documents[candidate] = _representative_payload(
                                candidate,
                                record=record,
                                record_count=min(record_count, 8),
                                nodes=node_count,
                                change_rate=change_rate,
                            )

        payload: dict[str, object] = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "benchmark": "django-ray-workflow-progress-storage",
            "environment": _environment(database_deployment=database_deployment),
            "configuration": {
                "nodes": nodes,
                "change_rates": change_rates,
                "repetitions": repetitions,
                "warmups": warmups,
                "seed": seed,
                "page_items": PAGE_ITEMS,
                "representative_bytes": REPRESENTATIVE_BYTES,
                "change_distribution": "uniform-random expected page occupancy",
                "timing_scope": (
                    "bounded in-process representative structures; no ORM, network, "
                    "or candidate-schema round trips"
                ),
                "write_model_scope": (
                    "bytes and database statements are analytical; external operations "
                    "are excluded from statement counts"
                ),
                "database_probe_scope": ("one bounded representative JSONB document per candidate"),
                "execution_scope": ("configured Django database only; no Ray or Kubernetes access"),
            },
            "profiles": profiles,
            "candidates": [
                {"id": candidate, "description": _CANDIDATE_DESCRIPTIONS[candidate]}
                for candidate in CANDIDATE_IDS
            ],
            "cases": cases,
            "database_evidence": _database_evidence(probe_documents),
        }
        json_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        markdown_text = _markdown(payload)
        if output_json is not None:
            _write_atomic(output_json, json_text)
        if output_markdown is not None:
            _write_atomic(output_markdown, markdown_text)
        if output_json is None and output_markdown is None:
            self.stdout.write(markdown_text)
        else:
            self.stdout.write(
                f"Recorded {len(cases)} cases; database evidence "
                f"{payload['database_evidence']['status']}."
            )

    @staticmethod
    def _nodes(values: object) -> list[int]:
        if not isinstance(values, list) or not values:
            raise CommandError("--nodes requires at least one positive integer")
        if any(type(value) is not int or value <= 0 for value in values):
            raise CommandError("--nodes values must be positive integers")
        if len(values) != len(set(values)):
            raise CommandError("--nodes values must be unique")
        return cast(list[int], values)

    @staticmethod
    def _change_rates(values: object) -> list[float]:
        if not isinstance(values, list) or not values:
            raise CommandError("--change-rates requires at least one value")
        normalized: list[float] = []
        for value in values:
            if type(value) not in (int, float):
                raise CommandError("--change-rates values must be finite numbers in (0, 1]")
            numeric = float(value)
            if not math.isfinite(numeric) or numeric <= 0 or numeric > 1:
                raise CommandError("--change-rates values must be finite numbers in (0, 1]")
            normalized.append(numeric)
        if len(normalized) != len(set(normalized)):
            raise CommandError("--change-rates values must be unique")
        return normalized

    @staticmethod
    def _positive_int(value: object, option: str) -> int:
        if type(value) is not int or value <= 0:
            raise CommandError(f"{option} must be a positive integer")
        return value

    @staticmethod
    def _non_negative_int(value: object, option: str) -> int:
        if type(value) is not int or value < 0:
            raise CommandError(f"{option} must be a non-negative integer")
        return value

    @staticmethod
    def _integer(value: object, option: str) -> int:
        if type(value) is not int:
            raise CommandError(f"{option} must be an integer")
        return value

    @staticmethod
    def _metadata_label(value: object, option: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}",
            value,
        ):
            raise CommandError(f"{option} must be a 1-128 character non-secret identifier")
        return value


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "CANDIDATE_IDS",
    "DEFAULT_CHANGE_RATES",
    "DEFAULT_NODES",
    "PROFILE_IDS",
]
