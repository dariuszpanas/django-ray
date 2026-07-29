from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import zlib
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from django_ray.workflow_progress_limits import WORKFLOW_PROGRESS_LIMITS_V1
from django_ray.workflow_progress_protocol import (
    WORKFLOW_PROGRESS_EVENT_ENCODING,
    WORKFLOW_PROGRESS_EVENT_SCHEMA_VERSION,
    WorkflowProgressEventKind,
    WorkflowProgressProtocolError,
    WorkflowProgressProtocolLimitError,
    canonical_workflow_progress_json_bytes,
    decode_workflow_progress_event,
    prepare_workflow_progress_event,
    send_workflow_progress_event,
)

_OCCURRED_AT = datetime(2026, 7, 29, 12, 34, 56, tzinfo=UTC)
_FINGERPRINT = f"sha256:{'a' * 64}"
_REVISION = f"sha256:{'b' * 64}"
_ENVELOPE_KEYS = {
    "encoding",
    "kind",
    "limits_profile",
    "occurred_at",
    "payload",
    "run_identity",
    "schema_version",
    "truncated",
}


def _run_identity() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_execution_pk": 41,
        "attempt_number": 2,
        "execution_generation": 7,
        "run_id": "f7fb7a64-a589-4d6f-a2a8-d7e493cb647b",
    }


def _payloads() -> dict[WorkflowProgressEventKind, dict[str, Any]]:
    return {
        WorkflowProgressEventKind.INITIALIZED: {
            "plan": {
                "plan_format": "django-ray.workflow-plan",
                "plan_format_version": 1,
                "fingerprint": _FINGERPRINT,
                "definition_name": "sample_workflow",
                "definition_revision": _REVISION,
                "topology_class": "dynamic_tasks",
                "node_count": 3,
            }
        },
        WorkflowProgressEventKind.NODE_REGISTERED: {
            "callable_path": "tests.tasks.increment",
            "label": "increment",
            "node_id": "node-a",
            "ray_options": {"num_cpus": 1},
            "runtime_env": {"profile": "default"},
        },
        WorkflowProgressEventKind.EDGES_REGISTERED: {
            "edges": [{"source": "node-a", "target": "node-b"}]
        },
        WorkflowProgressEventKind.MAP_REGISTERED: {
            "label": "mapped",
            "max_concurrency": 4,
            "max_items": 100,
            "node_id": "map-a",
        },
        WorkflowProgressEventKind.SUBMITTED: {
            "label": "increment",
            "node_id": "node-a",
            "ray_task_id": "ray-task-a",
        },
        WorkflowProgressEventKind.STARTED: {
            "execution": {
                "assigned_resources": {"CPU": 1.0},
                "ray_job_id": "ray-job-a",
                "ray_node_id": "ray-node-a",
                "ray_task_id": "ray-task-a",
                "ray_worker_id": "ray-worker-a",
            },
            "label": "increment",
            "node_id": "node-a",
        },
        WorkflowProgressEventKind.APPLICATION_PROGRESS: {
            "current": 5,
            "message": "halfway",
            "metrics": {"ratio": 0.5, "rows": 5},
            "node_id": "node-a",
            "total": 10,
        },
        WorkflowProgressEventKind.MAP_PROGRESS: {
            "completed": 3,
            "input_exhausted": False,
            "label": "mapped",
            "node_id": "map-a",
            "submitted": 4,
        },
        WorkflowProgressEventKind.COMPLETED: {
            "label": "increment",
            "node_id": "node-a",
        },
        WorkflowProgressEventKind.FAILED: {
            "error": "ValueError: expected failure",
            "label": "increment",
            "node_id": "node-a",
        },
    }


def _prepare(
    kind: WorkflowProgressEventKind,
    payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> bytes:
    return prepare_workflow_progress_event(
        _run_identity(),
        kind,
        _payloads()[kind] if payload is None else payload,
        occurred_at=_OCCURRED_AT,
        **kwargs,
    )


def _mutate_envelope(wire: bytes, field: str, value: Any) -> bytes:
    envelope = json.loads(wire)
    envelope[field] = value
    return canonical_workflow_progress_json_bytes(envelope)


@pytest.mark.parametrize("kind", list(WorkflowProgressEventKind), ids=lambda kind: kind.value)
def test_canonical_round_trip_for_every_event_kind(kind: WorkflowProgressEventKind) -> None:
    identity = _run_identity()
    wire = prepare_workflow_progress_event(
        identity,
        kind,
        _payloads()[kind],
        occurred_at=_OCCURRED_AT,
    )

    envelope = json.loads(wire)
    event = decode_workflow_progress_event(wire, expected_run_identity=identity)

    assert type(wire) is bytes
    assert canonical_workflow_progress_json_bytes(envelope) == wire
    assert set(envelope) == _ENVELOPE_KEYS
    assert envelope["schema_version"] == WORKFLOW_PROGRESS_EVENT_SCHEMA_VERSION
    assert envelope["encoding"] == WORKFLOW_PROGRESS_EVENT_ENCODING
    assert envelope["limits_profile"] == "v1"
    assert event.kind is kind
    assert event.run_identity == identity
    assert event.run_identity is not identity
    assert event.occurred_at == "2026-07-29T12:34:56Z"
    assert event.payload == envelope["payload"]
    assert type(event.truncated) is bool


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("task_execution_pk", 42),
        ("attempt_number", 3),
        ("execution_generation", 8),
        ("run_id", "79a0f8a7-aeb4-412a-821a-7500f2f31890"),
    ],
)
def test_decode_rejects_each_complete_run_fence_mismatch(
    field: str,
    replacement: Any,
) -> None:
    wire = _prepare(WorkflowProgressEventKind.COMPLETED)
    expected = _run_identity()
    expected[field] = replacement

    with pytest.raises(WorkflowProgressProtocolError) as raised:
        decode_workflow_progress_event(wire, expected_run_identity=expected)

    assert raised.value.reason == "fence_mismatch"


class _RemoteMethod:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def remote(self, *args: Any) -> None:
        self.calls.append(args)


class _Actor:
    def __init__(self) -> None:
        self.ingest = _RemoteMethod()


def test_send_helper_uses_only_one_ingest_call_after_full_preparation() -> None:
    actor = _Actor()

    wire = send_workflow_progress_event(
        actor,
        _run_identity(),
        WorkflowProgressEventKind.COMPLETED,
        _payloads()[WorkflowProgressEventKind.COMPLETED],
        occurred_at=_OCCURRED_AT,
    )

    assert actor.ingest.calls == [(wire,)]


def test_send_helper_makes_zero_remote_calls_for_invalid_input() -> None:
    actor = _Actor()
    invalid = _payloads()[WorkflowProgressEventKind.COMPLETED]
    invalid["node_id"] = "n" * (WORKFLOW_PROGRESS_LIMITS_V1.node_id_max_bytes + 1)

    with pytest.raises(WorkflowProgressProtocolLimitError):
        send_workflow_progress_event(
            actor,
            _run_identity(),
            WorkflowProgressEventKind.COMPLETED,
            invalid,
            occurred_at=_OCCURRED_AT,
        )

    assert actor.ingest.calls == []


def test_prepare_redacts_secrets_before_the_wire_boundary() -> None:
    first_secret = "correct-horse-battery-staple"
    second_secret = "Bearer top-secret-value"
    payload = _payloads()[WorkflowProgressEventKind.NODE_REGISTERED]
    payload["label"] = f"api_key={first_secret}"
    payload["runtime_env"] = {
        "env_vars": {
            "DATABASE_PASSWORD": first_secret,
            "PUBLIC_SETTING": "visible",
        }
    }
    payload["ray_options"] = {
        "headers": {
            "Authorization": second_secret,
            "Accept": "application/json",
        }
    }

    wire = _prepare(WorkflowProgressEventKind.NODE_REGISTERED, payload)
    event = decode_workflow_progress_event(wire, expected_run_identity=_run_identity())

    assert first_secret.encode() not in wire
    assert second_secret.encode() not in wire
    assert b"DATABASE_PASSWORD" not in wire
    assert b"Authorization" not in wire
    assert event.truncated is True


@pytest.mark.parametrize("value", ["{}", bytearray(b"{}"), memoryview(b"{}")])
def test_decode_requires_exact_bytes(value: Any) -> None:
    with pytest.raises(WorkflowProgressProtocolError):
        decode_workflow_progress_event(value)


@pytest.mark.parametrize("wire", [b"", b"{", b"\xff", b"[]"])
def test_decode_rejects_malformed_or_non_envelope_json(wire: bytes) -> None:
    with pytest.raises(WorkflowProgressProtocolError):
        decode_workflow_progress_event(wire)


def test_decode_rejects_duplicate_json_keys() -> None:
    wire = _prepare(WorkflowProgressEventKind.COMPLETED)
    duplicate = wire.replace(
        b'{"encoding":"identity"',
        b'{"encoding":"identity","encoding":"identity"',
        1,
    )
    assert duplicate != wire

    with pytest.raises(WorkflowProgressProtocolError):
        decode_workflow_progress_event(duplicate)


def test_decode_rejects_noncanonical_identity_json() -> None:
    wire = _prepare(WorkflowProgressEventKind.COMPLETED)
    noncanonical = json.dumps(json.loads(wire), indent=2).encode()

    with pytest.raises(WorkflowProgressProtocolError):
        decode_workflow_progress_event(noncanonical)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("kind", "unknown"),
        ("limits_profile", "v2"),
        ("encoding", "gzip"),
    ],
)
def test_decode_rejects_unknown_envelope_protocol_values(field: str, value: Any) -> None:
    wire = _prepare(WorkflowProgressEventKind.COMPLETED)

    with pytest.raises(WorkflowProgressProtocolError):
        decode_workflow_progress_event(_mutate_envelope(wire, field, value))


def test_decode_rejects_unknown_envelope_fields() -> None:
    wire = _prepare(WorkflowProgressEventKind.COMPLETED)
    envelope = json.loads(wire)
    envelope["extension"] = None

    with pytest.raises(WorkflowProgressProtocolError):
        decode_workflow_progress_event(canonical_workflow_progress_json_bytes(envelope))


@pytest.mark.parametrize("compress", [gzip.compress, zlib.compress])
def test_decode_never_decompresses_wire_bytes(compress: Any) -> None:
    compressed = compress(_prepare(WorkflowProgressEventKind.COMPLETED))

    with pytest.raises(WorkflowProgressProtocolError):
        decode_workflow_progress_event(compressed)


def _deterministic_incompressible_bytes(size: int) -> bytes:
    chunks = (
        hashlib.sha256(index.to_bytes(8, "big")).digest() for index in range((size + 31) // 32)
    )
    return b"".join(chunks)[:size]


@pytest.mark.parametrize("compressible", [True, False], ids=["compressible", "incompressible"])
def test_decode_rejects_raw_wire_above_the_hard_cap_before_parsing(
    compressible: bool,
) -> None:
    size = WORKFLOW_PROGRESS_LIMITS_V1.event_wire_max_bytes + 1
    wire = b"x" * size if compressible else _deterministic_incompressible_bytes(size)
    if not compressible:
        assert len(zlib.compress(wire)) > WORKFLOW_PROGRESS_LIMITS_V1.event_wire_max_bytes

    with pytest.raises(WorkflowProgressProtocolLimitError) as raised:
        decode_workflow_progress_event(wire)

    assert raised.value.reason == "limit_exceeded"


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_decode_rejects_nonfinite_json_constants(constant: bytes) -> None:
    wire = _prepare(WorkflowProgressEventKind.APPLICATION_PROGRESS)
    invalid = wire.replace(b'"current":5.0', b'"current":' + constant, 1)
    assert invalid != wire

    with pytest.raises(WorkflowProgressProtocolError):
        decode_workflow_progress_event(invalid)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_nonfinite_numbers(value: float) -> None:
    with pytest.raises(WorkflowProgressProtocolError):
        canonical_workflow_progress_json_bytes({"value": value})


@pytest.mark.parametrize(
    "field",
    ["task_execution_pk", "attempt_number", "execution_generation"],
)
def test_prepare_rejects_booleans_for_run_identity_integers(field: str) -> None:
    identity = _run_identity()
    identity[field] = True

    with pytest.raises(WorkflowProgressProtocolError):
        prepare_workflow_progress_event(
            identity,
            WorkflowProgressEventKind.COMPLETED,
            _payloads()[WorkflowProgressEventKind.COMPLETED],
            occurred_at=_OCCURRED_AT,
        )


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        (WorkflowProgressEventKind.APPLICATION_PROGRESS, "current"),
        (WorkflowProgressEventKind.APPLICATION_PROGRESS, "total"),
        (WorkflowProgressEventKind.MAP_PROGRESS, "submitted"),
        (WorkflowProgressEventKind.MAP_PROGRESS, "completed"),
    ],
)
def test_prepare_rejects_booleans_for_payload_numbers(
    kind: WorkflowProgressEventKind,
    field: str,
) -> None:
    payload = _payloads()[kind]
    payload[field] = True

    with pytest.raises(WorkflowProgressProtocolError):
        _prepare(kind, payload)


def _nested_metadata(depth: int) -> Any:
    value: Any = "leaf"
    for index in range(depth):
        value = {f"level_{index}": value}
    return value


def test_metadata_depth_accepts_v1_limit_and_rejects_limit_plus_one() -> None:
    kind = WorkflowProgressEventKind.NODE_REGISTERED
    accepted = _payloads()[kind]
    accepted["runtime_env"] = _nested_metadata(WORKFLOW_PROGRESS_LIMITS_V1.value_max_depth)
    _prepare(kind, accepted)

    rejected = _payloads()[kind]
    rejected["runtime_env"] = _nested_metadata(WORKFLOW_PROGRESS_LIMITS_V1.value_max_depth + 1)
    with pytest.raises(WorkflowProgressProtocolLimitError):
        _prepare(kind, rejected)


def _application_payload(metrics: dict[str, Any]) -> dict[str, Any]:
    payload = _payloads()[WorkflowProgressEventKind.APPLICATION_PROGRESS]
    payload["metrics"] = metrics
    return payload


def test_metrics_item_limit_accepts_v1_limit_and_rejects_limit_plus_one() -> None:
    maximum = WORKFLOW_PROGRESS_LIMITS_V1.metrics_max_items
    _prepare(
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        _application_payload({f"m{index:02d}": index for index in range(maximum)}),
    )

    with pytest.raises(WorkflowProgressProtocolLimitError):
        _prepare(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            _application_payload({f"m{index:02d}": index for index in range(maximum + 1)}),
        )


def test_metric_key_limit_accepts_v1_limit_and_rejects_limit_plus_one() -> None:
    maximum = WORKFLOW_PROGRESS_LIMITS_V1.metric_key_max_bytes
    _prepare(
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        _application_payload({"k" * maximum: 1}),
    )

    with pytest.raises(WorkflowProgressProtocolLimitError):
        _prepare(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            _application_payload({"k" * (maximum + 1): 1}),
        )


def test_metric_string_limit_accepts_v1_limit_and_omits_limit_plus_one() -> None:
    maximum = WORKFLOW_PROGRESS_LIMITS_V1.metric_string_max_bytes
    accepted = decode_workflow_progress_event(
        _prepare(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            _application_payload({"value": "v" * maximum}),
        )
    )
    omitted = decode_workflow_progress_event(
        _prepare(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            _application_payload({"value": "v" * (maximum + 1)}),
        )
    )

    assert accepted.payload["metrics"]["value"] == "v" * maximum
    assert accepted.truncated is False
    assert omitted.payload["metrics"] == {"value": "<omitted:oversized>"}
    assert omitted.truncated is True


def test_reduced_metrics_encoded_cap_retains_exact_and_omits_cap_minus_one() -> None:
    metrics = {"value": "v" * 100}
    payload = _application_payload(metrics)
    exact_size = len(canonical_workflow_progress_json_bytes(metrics))
    exact_limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        metrics_max_encoded_bytes=exact_size,
    )
    reduced_limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        metrics_max_encoded_bytes=exact_size - 1,
    )

    exact = decode_workflow_progress_event(
        _prepare(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            payload,
            limits=exact_limits,
        ),
        limits=exact_limits,
    )
    omitted = decode_workflow_progress_event(
        _prepare(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            payload,
            limits=reduced_limits,
        ),
        limits=reduced_limits,
    )

    assert exact.payload["metrics"] == metrics
    assert exact.truncated is False
    assert set(omitted.payload["metrics"]) == {"_omitted"}
    assert omitted.payload["metrics"]["_omitted"].startswith("sha256:")
    assert omitted.truncated is True


def test_node_id_limit_accepts_v1_limit_and_rejects_limit_plus_one() -> None:
    maximum = WORKFLOW_PROGRESS_LIMITS_V1.node_id_max_bytes
    accepted = _payloads()[WorkflowProgressEventKind.COMPLETED]
    accepted["node_id"] = "n" * maximum
    _prepare(WorkflowProgressEventKind.COMPLETED, accepted)

    rejected = _payloads()[WorkflowProgressEventKind.COMPLETED]
    rejected["node_id"] = "n" * (maximum + 1)
    with pytest.raises(WorkflowProgressProtocolLimitError):
        _prepare(WorkflowProgressEventKind.COMPLETED, rejected)


@pytest.mark.parametrize(
    ("kind", "field", "limit_field"),
    [
        (WorkflowProgressEventKind.COMPLETED, "label", "label_max_bytes"),
        (WorkflowProgressEventKind.APPLICATION_PROGRESS, "message", "message_max_bytes"),
    ],
)
def test_operator_text_accepts_v1_limit_and_omits_limit_plus_one(
    kind: WorkflowProgressEventKind,
    field: str,
    limit_field: str,
) -> None:
    maximum = getattr(WORKFLOW_PROGRESS_LIMITS_V1, limit_field)
    accepted_payload = _payloads()[kind]
    accepted_payload[field] = "x" * maximum
    accepted = decode_workflow_progress_event(_prepare(kind, accepted_payload))

    omitted_payload = _payloads()[kind]
    omitted_payload[field] = "x" * (maximum + 1)
    omitted = decode_workflow_progress_event(_prepare(kind, omitted_payload))

    assert accepted.payload[field] == "x" * maximum
    assert accepted.truncated is False
    assert omitted.payload[field] == "<omitted:oversized>"
    assert omitted.truncated is True


def _edges(count: int) -> list[dict[str, str]]:
    return [{"source": f"source-{index}", "target": f"target-{index}"} for index in range(count)]


def test_edge_batch_accepts_32_and_rejects_33() -> None:
    maximum = WORKFLOW_PROGRESS_LIMITS_V1.edge_batch_max_items
    assert maximum == 32
    _prepare(WorkflowProgressEventKind.EDGES_REGISTERED, {"edges": _edges(maximum)})

    with pytest.raises(WorkflowProgressProtocolLimitError):
        _prepare(
            WorkflowProgressEventKind.EDGES_REGISTERED,
            {"edges": _edges(maximum + 1)},
        )


@pytest.mark.parametrize("kind", list(WorkflowProgressEventKind), ids=lambda kind: kind.value)
def test_payload_envelopes_require_exact_fields(kind: WorkflowProgressEventKind) -> None:
    payload = _payloads()[kind]
    payload["unexpected"] = None

    with pytest.raises(WorkflowProgressProtocolError):
        _prepare(kind, payload)


def test_initialized_plan_requires_exact_fields() -> None:
    payload = _payloads()[WorkflowProgressEventKind.INITIALIZED]
    payload["plan"]["unexpected"] = None

    with pytest.raises(WorkflowProgressProtocolError):
        _prepare(WorkflowProgressEventKind.INITIALIZED, payload)


@pytest.mark.parametrize(
    "limit_field",
    [
        "event_wire_max_bytes",
        "event_payload_max_bytes",
        "event_decoded_max_bytes",
    ],
)
def test_reduced_event_caps_accept_exact_and_reject_cap_minus_one(
    limit_field: str,
) -> None:
    kind = WorkflowProgressEventKind.COMPLETED
    payload = _payloads()[kind]
    wire = _prepare(kind, payload)
    exact_size = (
        len(canonical_workflow_progress_json_bytes(payload))
        if limit_field == "event_payload_max_bytes"
        else len(wire)
    )
    exact_limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        **{limit_field: exact_size},
    )
    reduced_limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        **{limit_field: exact_size - 1},
    )

    assert _prepare(kind, payload, limits=exact_limits) == wire
    assert decode_workflow_progress_event(wire, limits=exact_limits).kind is kind
    with pytest.raises(WorkflowProgressProtocolLimitError):
        _prepare(kind, payload, limits=reduced_limits)
    with pytest.raises(WorkflowProgressProtocolLimitError):
        decode_workflow_progress_event(wire, limits=reduced_limits)


def test_protocol_v1_limits_can_only_be_reduced() -> None:
    for item in fields(WORKFLOW_PROGRESS_LIMITS_V1):
        with pytest.raises(ValueError):
            replace(
                WORKFLOW_PROGRESS_LIMITS_V1,
                **{item.name: getattr(WORKFLOW_PROGRESS_LIMITS_V1, item.name) + 1},
            )


def test_protocol_exception_reasons_are_fixed_and_bounded() -> None:
    assert issubclass(WorkflowProgressProtocolError, ValueError)
    assert issubclass(WorkflowProgressProtocolLimitError, WorkflowProgressProtocolError)
    assert WorkflowProgressProtocolError("invalid").reason == "protocol_error"
    assert WorkflowProgressProtocolLimitError("large").reason == "limit_exceeded"

    with pytest.raises(ValueError):
        WorkflowProgressProtocolError("invalid", reason="unbounded-attacker-input")


def test_protocol_module_imports_cold_without_django_setup_or_models() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment["PYTHONPATH"] = str(root / "src")
    code = """
import sys
assert "django" not in sys.modules
import django_ray.workflow_progress_protocol as protocol
assert protocol.WORKFLOW_PROGRESS_EVENT_SCHEMA_VERSION == 1
assert "django" not in sys.modules
assert "django_ray.models" not in sys.modules
"""

    completed = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
