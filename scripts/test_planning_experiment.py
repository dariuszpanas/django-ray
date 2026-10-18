"""Pure advisory measurement records; never an execution or qualification gate."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_SECONDS = 31 * 24 * 60 * 60
OUTCOMES = {"passed", "failed", "pending", "cancelled", "blocked", "missing"}
TIMES = {
    "dispatched_at",
    "workflow_created_at",
    "feedback_at",
    "candidate_ready_at",
    "required_ci_ready_at",
    "first_failure_at",
}


class ObservationError(ValueError):
    """Fixed, bounded diagnostics for malformed advisory measurements."""


def _object(value, fields):
    if type(value) is not dict or len(value) != len(fields) or set(value) != set(fields):
        raise ObservationError("Invalid observation fields")
    return value


def _token(value, maximum=128):
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value) is None
    ):
        raise ObservationError("Invalid observation identifier")
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ObservationError("Invalid observation digest")
    return value


def _integer(value, maximum=2_147_483_647):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ObservationError("Invalid observation attempt")
    return value


def _choice(value, choices):
    if type(value) is not str or value not in choices:
        raise ObservationError("Unsupported observation value")
    return value


def _source(value, *, adapter=False):
    fields = {"execution_commit", "execution_tree"}
    if not adapter:
        fields |= {"base_commit", "head_commit", "comparison_commit"}
    value = _object(value, fields)
    widths = set()
    for key, identity in value.items():
        if identity is None and not key.startswith("execution_"):
            continue
        if (
            type(identity) is not str
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", identity) is None
        ):
            raise ObservationError("Invalid observation source identity")
        widths.add(len(identity))
    if len(widths) != 1:
        raise ObservationError("Mixed observation source formats")
    if not adapter:
        optional = [
            value[key] is None for key in ("base_commit", "head_commit", "comparison_commit")
        ]
        if any(optional) and not all(optional):
            raise ObservationError("Incomplete observation comparison source")
        if value["head_commit"] is not None and value["head_commit"] != value["execution_commit"]:
            raise ObservationError("Observation execution differs from its comparison head")
    return dict(value)


def _duration(value):
    if value is not None and (
        type(value) not in (int, float) or not 0 <= value <= MAX_SECONDS or not math.isfinite(value)
    ):
        raise ObservationError("Invalid bounded observation duration")
    return value


def _timestamp(value):
    if value is None:
        return None
    if (
        type(value) is not str
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value
        )
        is None
    ):
        raise ObservationError("Observation timestamps require bounded UTC values")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ObservationError("Invalid observation calendar timestamp") from None


def _interval(start, end):
    if start is None or end is None:
        return None
    duration = (end - start).total_seconds()
    return _duration(duration)


def _timing(value):
    raw = _object(value, TIMES)
    times = {name: _timestamp(raw[name]) for name in TIMES}
    metrics = {
        "dispatch_to_creation_seconds": _interval(
            times["dispatched_at"], times["workflow_created_at"]
        ),
        "advisory_feedback_seconds": _interval(times["workflow_created_at"], times["feedback_at"]),
        "developer_feedback_seconds": _interval(times["candidate_ready_at"], times["feedback_at"]),
        "required_ci_readiness_seconds": _interval(
            times["candidate_ready_at"], times["required_ci_ready_at"]
        ),
        "first_failure_seconds": _interval(times["workflow_created_at"], times["first_failure_at"]),
    }
    _interval(times["candidate_ready_at"], times["workflow_created_at"])
    _interval(times["first_failure_at"], times["feedback_at"])
    return dict(raw), metrics


def _array(value, maximum):
    if type(value) is not list or len(value) > maximum:
        raise ObservationError("Observation array exceeds its bound")
    return value


def _cells(value, *, run_id, run_attempt):
    cells = []
    seen = set()
    jobs = set()
    runner_seconds = []
    for raw in _array(value, 512):
        cell = _object(raw, {"suite", "variant", "outcome", "evidence_sha256", "attempts"})
        key = (_token(cell["suite"], 64), _token(cell["variant"], 64))
        if key in seen:
            raise ObservationError("Duplicate observation cell")
        seen.add(key)
        outcome = _choice(cell["outcome"], OUTCOMES)
        evidence = cell["evidence_sha256"]
        if evidence is not None:
            _digest(evidence)
        attempts = []
        for raw_attempt in _array(cell["attempts"], 32):
            attempt = _object(
                raw_attempt,
                {"job_id", "run_id", "attempt", "retry_ordinal", "outcome", "runner_seconds"},
            )
            job_id = _token(attempt["job_id"])
            if _token(attempt["run_id"]) != run_id or _integer(attempt["attempt"]) != run_attempt:
                raise ObservationError(
                    "Observation jobs require the exact execution run and attempt"
                )
            number = _integer(attempt["retry_ordinal"])
            if job_id in jobs:
                raise ObservationError("Duplicate observation job attempt")
            jobs.add(job_id)
            _choice(attempt["outcome"], OUTCOMES - {"missing"})
            seconds = _duration(attempt["runner_seconds"])
            runner_seconds.append(seconds)
            if attempts and number <= attempts[-1]["retry_ordinal"]:
                raise ObservationError("Observation retries must preserve attempt order")
            attempts.append(dict(attempt))
        if outcome == "missing" and attempts:
            raise ObservationError("Missing observation cannot contain a job attempt")
        if outcome != "missing" and (not attempts or attempts[-1]["outcome"] != outcome):
            raise ObservationError("Observation must retain its final authorized attempt")
        if outcome == "passed" and evidence is None:
            raise ObservationError("Passed observation requires retained evidence")
        cells.append({**cell, "attempts": attempts})
    total = (
        None
        if any(value is None for value in runner_seconds)
        or any(cell["outcome"] == "missing" for cell in cells)
        or any(
            attempt["outcome"] in {"pending", "blocked"}
            for cell in cells
            for attempt in cell["attempts"]
        )
        else sum(runner_seconds)
    )
    if total is not None and total > 2**53 - 1:
        raise ObservationError("Observation runner total exceeds portable bounds")
    return sorted(cells, key=lambda cell: (cell["suite"], cell["variant"])), total


def build_observation(identities: object, measurements: object) -> dict:
    """Validate one retained measurement, without authenticating or evaluating results.

    The acquisition caller supplies independently verified identities and hashes of
    the normalized result and evaluation artifacts. This format records evidence;
    it cannot authorize qualification, selection, or a timing-savings claim.
    """
    identity = _object(
        identities,
        {
            "source",
            "adapter_source",
            "policy_sha256",
            "bindings_sha256",
            "plan_digest",
            "results_sha256",
            "evaluation_sha256",
            "run_id",
            "attempt",
        },
    )
    copied = {
        name: _digest(identity[name])
        for name in (
            "policy_sha256",
            "bindings_sha256",
            "plan_digest",
            "results_sha256",
            "evaluation_sha256",
        )
    }
    copied.update(
        source=_source(identity["source"]),
        adapter_source=_source(identity["adapter_source"], adapter=True),
        run_id=_token(identity["run_id"]),
        attempt=_integer(identity["attempt"]),
    )
    measured = _object(
        measurements,
        {
            "experiment_id",
            "arm",
            "fingerprints",
            "cache_state",
            "timing",
            "cells",
            "artifacts",
            "incomplete_reason",
        },
    )
    fingerprints = _object(
        measured["fingerprints"], {"environment_sha256", "images_sha256", "dependencies_sha256"}
    )
    for value in fingerprints.values():
        if value is not None:
            _digest(value)
    timing, metrics = _timing(measured["timing"])
    cells, total = _cells(measured["cells"], run_id=copied["run_id"], run_attempt=copied["attempt"])
    incomplete = measured["incomplete_reason"]
    if incomplete is not None:
        if type(incomplete) is not str or not incomplete or len(incomplete) > 1000:
            raise ObservationError("Invalid observation incompleteness reason")
        try:
            if len(incomplete.encode("utf-8")) > 1000:
                raise ObservationError("Observation incompleteness reason exceeds its bound")
        except UnicodeError:
            raise ObservationError("Invalid observation text encoding") from None
    if (
        any(cell["outcome"] in {"missing", "pending", "blocked", "cancelled"} for cell in cells)
        and incomplete is None
    ):
        raise ObservationError("Incomplete observations require an explicit reason")
    if timing["first_failure_at"] is not None and not any(
        attempt["outcome"] == "failed" for cell in cells for attempt in cell["attempts"]
    ):
        raise ObservationError("First failure timing requires a retained failure")
    artifacts = []
    names = set()
    for raw in _array(measured["artifacts"], 512):
        artifact = _object(raw, {"name", "sha256"})
        name = _token(artifact["name"])
        if name in names:
            raise ObservationError("Duplicate observation artifact")
        names.add(name)
        artifacts.append({"name": name, "sha256": _digest(artifact["sha256"])})
    report = {
        "test-experiment-observation-version": 1,
        "activation": "disabled",
        "authority": "advisory-measurement-only",
        "identities": copied,
        "experiment_id": _token(measured["experiment_id"]),
        "arm": _choice(measured["arm"], {"current", "proposed", "full"}),
        "fingerprints": dict(fingerprints),
        "cache_state": _choice(measured["cache_state"], {"cold", "warm", "mixed", "unknown"}),
        "timing": timing,
        "metrics": {**metrics, "runner_job_seconds": total, "billing_seconds": None},
        "cells": cells,
        "artifacts": sorted(artifacts, key=lambda item: item["name"]),
        "incomplete_reason": incomplete,
        "comparison": None,
    }
    encode_observation(report)
    return report


def encode_observation(document: dict) -> bytes:
    """Encode one built report within its complete UTF-8/newline bound."""
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        remaining = MAX_DOCUMENT_BYTES - 1
        chunks = []
        for chunk in encoder.iterencode(document):
            raw = chunk.encode("utf-8")
            remaining -= len(raw)
            if remaining < 0:
                raise ObservationError("Observation document exceeds its byte limit")
            chunks.append(raw)
        return b"".join(chunks) + b"\n"
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ObservationError("Cannot encode bounded observation JSON") from None
