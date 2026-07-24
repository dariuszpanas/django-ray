"""Boundaries, compatibility, and fencing for schema-v3 progress summaries."""

from __future__ import annotations

import json

import pytest

import django_ray.workflow_progress_summary as summary_module
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_progress_summary import (
    WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES,
    WorkflowProgressSummaryError,
    deserialize_workflow_progress_summary,
    normalize_workflow_progress_summary,
    public_workflow_progress_summary,
    serialize_workflow_progress_summary,
)


@pytest.fixture
def fixed_run_identity() -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=2,
        execution_generation=4,
        run_id="00000000-0000-0000-0000-000000000125",
    )


def _summary(
    identity: WorkflowRunIdentity,
    *,
    summary_revision: int = 1,
    selected_strategy: str | None = None,
    plan_fingerprint: str | None = None,
    published_detail: bool = False,
    state: str = "RUNNING",
) -> dict[str, object]:
    terminal = state in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}
    finished_at = "2026-07-20T12:00:02Z" if terminal else None
    return {
        "schema_version": 3,
        "storage_protocol_version": 1,
        "run_identity": identity.as_dict(),
        "reporting_policy": "full",
        "selected_strategy": selected_strategy,
        "plan_fingerprint": plan_fingerprint,
        "limits_profile": "v1",
        "summary_revision": summary_revision,
        "topology_version": 1 if published_detail else None,
        "detail_revision": 1 if published_detail else None,
        "state": state,
        "node_counts": {
            "declared": 1,
            "discovered": 1,
            "retained_topology": 1 if published_detail else 0,
            "retained_detail": 1 if published_detail else 0,
            "pending": 0 if terminal else 1,
            "running": 0,
            "succeeded": 1 if state == "SUCCEEDED" else 0,
            "failed": 1 if state in {"FAILED", "CANCELLED", "LOST"} else 0,
        },
        "edge_counts": {
            "declared": 0,
            "discovered": 0,
            "retained_topology": 0,
        },
        "progress_percent": 100.0 if terminal else 0.0,
        "timestamps": {
            "started_at": "2026-07-20T12:00:00Z",
            "updated_at": finished_at or "2026-07-20T12:00:01Z",
            "finished_at": finished_at,
        },
        "detail": {
            "availability": "AVAILABLE" if published_detail else "NOT_REPORTED",
            "complete": published_detail,
            "truncation_reasons": [],
        },
        "storage": {
            "kind": "database",
            "manifest_id": "manifest_125" if published_detail else None,
        },
        "retention": {
            "detail_days": 7,
            "detail_expires_at": (
                "2026-07-27T12:00:02Z" if terminal and published_detail else None
            ),
        },
        "terminal": {"outcome": state if terminal else None, "finished_at": finished_at},
    }


def test_schema_v3_is_canonical_bounded_and_hides_internal_manifest(fixed_run_identity) -> None:
    identity = fixed_run_identity
    value = _summary(identity, published_detail=True)

    serialized = serialize_workflow_progress_summary(value, expected_identity=identity)
    decoded = deserialize_workflow_progress_summary(serialized, expected_identity=identity)

    assert serialized == json.dumps(
        decoded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert len(serialized.encode("utf-8")) <= WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES
    assert decoded["storage"]["manifest_id"] == "manifest_125"
    public = public_workflow_progress_summary(decoded)
    assert public["storage"] == {"kind": "database", "manifest_id": None}
    assert "task_execution_pk" not in public["run_identity"]
    assert decoded["storage"]["manifest_id"] == "manifest_125"
    assert decoded["run_identity"]["task_execution_pk"] == fixed_run_identity.task_execution_pk


def test_summary_byte_limit_accepts_exact_boundary_and_rejects_next_byte(
    fixed_run_identity,
    monkeypatch,
) -> None:
    value = _summary(fixed_run_identity)
    serialized = serialize_workflow_progress_summary(value)
    byte_count = len(serialized.encode("utf-8"))

    monkeypatch.setattr(summary_module, "WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES", byte_count)
    assert serialize_workflow_progress_summary(value) == serialized

    monkeypatch.setattr(summary_module, "WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES", byte_count - 1)
    with pytest.raises(WorkflowProgressSummaryError, match="16 KiB"):
        serialize_workflow_progress_summary(value)
    with pytest.raises(WorkflowProgressSummaryError, match="16 KiB"):
        deserialize_workflow_progress_summary(serialized)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(graph={"nodes": []}),
        lambda value: value.update(recent_events=[]),
        lambda value: value.update(metrics={"password": "secret"}),
        lambda value: value.update(error="secret"),
        lambda value: value["storage"].update(uri="s3://private"),
        lambda value: value["node_counts"].update(extra=1),
        lambda value: value["run_identity"].update(invocation_id="private"),
    ],
)
def test_summary_rejects_graph_records_and_unknown_fields(fixed_run_identity, mutation) -> None:
    value = _summary(fixed_run_identity)
    mutation(value)

    with pytest.raises(WorkflowProgressSummaryError, match="exact protocol fields"):
        normalize_workflow_progress_summary(value)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("schema_version",), 2, "unsupported schema"),
        (("schema_version",), True, "unsupported schema"),
        (("schema_version",), 3.0, "unsupported schema"),
        (("storage_protocol_version",), 2, "protocol is unsupported"),
        (("storage_protocol_version",), True, "protocol is unsupported"),
        (("storage_protocol_version",), 1.0, "protocol is unsupported"),
        (("reporting_policy",), "everything", "reporting policy"),
        (("reporting_policy",), [], "reporting policy"),
        (("selected_strategy",), "Bad Strategy", "protocol identifier"),
        (("plan_fingerprint",), "sha256:nope", "canonical SHA-256"),
        (("limits_profile",), "V1", "protocol identifier"),
        (("summary_revision",), True, "non-negative integer"),
        (("summary_revision",), -1, "non-negative integer"),
        (("summary_revision",), (1 << 63) - 1, "reserve the terminal transition"),
        (("topology_version",), 0, "must be positive"),
        (("detail_revision",), 0, "must be positive"),
        (("state",), "QUEUED", "state is unsupported"),
        (("progress_percent",), True, "finite"),
        (("progress_percent",), float("nan"), "finite"),
        (("progress_percent",), 101, "finite"),
        (("progress_percent",), 10**1_000, "finite"),
        (("retention", "detail_days"), 31, "retention exceeds"),
        (("storage", "kind"), "s3", "must be database"),
        (("storage", "manifest_id"), "s3://private", "opaque identifier"),
    ],
)
def test_summary_rejects_invalid_scalar_boundaries(
    fixed_run_identity,
    path,
    value,
    message,
) -> None:
    summary = _summary(fixed_run_identity)
    target: object = summary
    for item in path[:-1]:
        assert isinstance(target, dict)
        target = target[item]
    assert isinstance(target, dict)
    target[path[-1]] = value

    with pytest.raises(WorkflowProgressSummaryError, match=message):
        normalize_workflow_progress_summary(summary)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "not-a-uuid"),
        ("run_id", None),
        ("run_id", "00000000-0000-0000-0000-00000000012A"),
        ("task_execution_pk", 0),
        ("attempt_number", 0),
        ("execution_generation", False),
        ("schema_version", 2),
        ("schema_version", True),
        ("schema_version", 1.0),
    ],
)
def test_summary_requires_canonical_complete_run_identity(fixed_run_identity, field, value) -> None:
    summary = _summary(fixed_run_identity)
    summary["run_identity"][field] = value  # type: ignore[index]

    with pytest.raises(WorkflowProgressSummaryError):
        normalize_workflow_progress_summary(summary)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["timestamps"].update(started_at="not-a-timestampZ"),
            "bounded UTC timestamp",
        ),
        (
            lambda value: value["timestamps"].update(started_at="2026-07-20T12:00:00.000000Z"),
            "canonical UTC encoding",
        ),
        (
            lambda value: value["timestamps"].update(
                started_at="9999-12-31T23:59:59Z",
                updated_at="9999-12-31T23:59:59Z",
            ),
            "bounded UTC timestamp",
        ),
        (
            lambda value: value["detail"].update(
                truncation_reasons=[
                    reason.value for reason in summary_module.WorkflowProgressTruncationReason
                ]
                + ["node_count_limit"]
            ),
            "reasons exceed",
        ),
        (
            lambda value: value["detail"].update(complete=True),
            "completeness and availability",
        ),
        (
            lambda value: value["storage"].update(manifest_id="manifest_125"),
            "manifest identity and topology version",
        ),
        (
            lambda value: value["retention"].update(detail_expires_at="2026-07-27T12:00:02Z"),
            "expiration requires",
        ),
    ],
)
def test_summary_rejects_remaining_protocol_inconsistencies(
    fixed_run_identity,
    mutation,
    message,
) -> None:
    summary = _summary(fixed_run_identity)
    mutation(summary)

    with pytest.raises(WorkflowProgressSummaryError, match=message):
        normalize_workflow_progress_summary(summary)


def test_summary_requires_topology_for_detail_and_complete_available_detail(
    fixed_run_identity,
) -> None:
    identity = fixed_run_identity
    missing_topology = _summary(identity)
    missing_topology["detail_revision"] = 1
    missing_topology["detail"] = {
        "availability": "MISSING",
        "complete": False,
        "truncation_reasons": [],
    }
    with pytest.raises(WorkflowProgressSummaryError, match="requires a topology version"):
        normalize_workflow_progress_summary(missing_topology)

    incomplete = _summary(identity, published_detail=True)
    incomplete["node_counts"]["retained_detail"] = 0  # type: ignore[index]
    with pytest.raises(WorkflowProgressSummaryError, match="retain every discovered node"):
        normalize_workflow_progress_summary(incomplete)


@pytest.mark.parametrize(
    ("state", "expires_at", "message"),
    [
        ("RUNNING", "2026-07-27T12:00:02Z", "active workflow detail"),
        ("FAILED", None, "must match its retention policy"),
        ("FAILED", "2026-07-28T12:00:02Z", "must match its retention policy"),
    ],
)
def test_published_detail_expiry_matches_terminal_retention(
    fixed_run_identity,
    state,
    expires_at,
    message,
) -> None:
    summary = _summary(
        fixed_run_identity,
        published_detail=True,
        state=state,
    )
    summary["retention"]["detail_expires_at"] = expires_at

    with pytest.raises(WorkflowProgressSummaryError, match=message):
        normalize_workflow_progress_summary(summary)


def test_canonical_json_wraps_unserializable_values() -> None:
    with pytest.raises(WorkflowProgressSummaryError, match="not canonical JSON"):
        summary_module._canonical_json({"unsupported": object()})


def test_summary_rejects_a_different_expected_run(fixed_run_identity) -> None:
    summary = _summary(fixed_run_identity)
    other = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=2,
        execution_generation=4,
        run_id="00000000-0000-0000-0000-000000000126",
    )

    with pytest.raises(WorkflowProgressSummaryError, match="does not match"):
        normalize_workflow_progress_summary(summary, expected_identity=other)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["node_counts"].update(retained_topology=1),
        lambda value: value["edge_counts"].update(
            declared=1,
            discovered=1,
            retained_topology=1,
        ),
        lambda value: (
            value.update(topology_version=1),
            value["storage"].update(manifest_id="manifest_125"),
            value["node_counts"].update(retained_topology=1, retained_detail=1),
        ),
    ],
)
def test_summary_requires_publication_revisions_for_retained_counts(
    fixed_run_identity,
    mutation,
) -> None:
    summary = _summary(fixed_run_identity)
    mutation(summary)

    with pytest.raises(WorkflowProgressSummaryError, match="published .* (?:version|revision)"):
        normalize_workflow_progress_summary(summary)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["node_counts"].update(declared=0),
        lambda value: value["edge_counts"].update(declared=0, discovered=1),
        lambda value: value["node_counts"].update(retained_topology=2),
        lambda value: value["node_counts"].update(retained_detail=1),
        lambda value: value["edge_counts"].update(discovered=0, retained_topology=1),
        lambda value: value["node_counts"].update(pending=0),
    ],
)
def test_summary_rejects_inconsistent_aggregate_counts(fixed_run_identity, mutate) -> None:
    summary = _summary(fixed_run_identity)
    mutate(summary)

    with pytest.raises(WorkflowProgressSummaryError):
        normalize_workflow_progress_summary(summary)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["node_counts"].update(
            pending=1,
            succeeded=0,
        ),
        lambda value: value.update(progress_percent=99.0),
    ],
)
def test_successful_summary_requires_complete_discovered_counts(
    fixed_run_identity,
    mutate,
) -> None:
    summary = _summary(fixed_run_identity, state="SUCCEEDED")
    mutate(summary)

    with pytest.raises(WorkflowProgressSummaryError, match="every discovered node succeeded"):
        normalize_workflow_progress_summary(summary)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["detail"].update(availability="UNKNOWN"),
        lambda value: value["detail"].update(complete=1),
        lambda value: value["detail"].update(availability="AVAILABLE", complete=True),
        lambda value: value["detail"].update(
            availability="TRUNCATED",
            complete=False,
            truncation_reasons=[],
        ),
        lambda value: value["detail"].update(truncation_reasons=["node_count_limit"]),
        lambda value: value["detail"].update(
            availability="TRUNCATED",
            truncation_reasons=["node_count_limit", "node_count_limit"],
        ),
        lambda value: value["detail"].update(
            availability="TRUNCATED",
            truncation_reasons=["unknown_limit"],
        ),
    ],
)
def test_summary_rejects_inconsistent_detail_availability(fixed_run_identity, mutate) -> None:
    summary = _summary(fixed_run_identity)
    mutate(summary)

    with pytest.raises(WorkflowProgressSummaryError):
        normalize_workflow_progress_summary(summary)


@pytest.mark.parametrize(
    ("reporting_policy", "availability"),
    [
        ("disabled", "NOT_REPORTED"),
        ("full", "DISABLED"),
        ("full", "OMITTED_BY_POLICY"),
        ("disabled", "OMITTED_BY_POLICY"),
    ],
)
def test_summary_rejects_reporting_policy_availability_conflicts(
    fixed_run_identity,
    reporting_policy: str,
    availability: str,
) -> None:
    summary = _summary(fixed_run_identity)
    summary["reporting_policy"] = reporting_policy
    summary["detail"]["availability"] = availability  # type: ignore[index]

    with pytest.raises(
        WorkflowProgressSummaryError,
        match="reporting policy and detail availability",
    ):
        normalize_workflow_progress_summary(summary)


def test_truncated_detail_requires_sorted_unique_protocol_reasons(fixed_run_identity) -> None:
    summary = _summary(fixed_run_identity, published_detail=True)
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit", "node_count_limit"],
    }
    summary["node_counts"]["retained_detail"] = 0  # type: ignore[index]

    normalized = normalize_workflow_progress_summary(summary)

    assert normalized["detail"]["truncation_reasons"] == [
        "detail_count_limit",
        "node_count_limit",
    ]


def test_expired_detail_requires_terminal_workflow_state(fixed_run_identity) -> None:
    summary = _summary(fixed_run_identity, published_detail=True)
    summary["detail"].update(
        availability="EXPIRED",
        complete=False,
    )

    with pytest.raises(WorkflowProgressSummaryError, match="requires a terminal"):
        normalize_workflow_progress_summary(summary)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["timestamps"].update(started_at="2026-07-20 12:00:00"),
        lambda value: value["timestamps"].update(updated_at="2026-07-20T11:59:59Z"),
        lambda value: value["timestamps"].update(finished_at="2026-07-20T12:00:02Z"),
        lambda value: value["terminal"].update(outcome="FAILED"),
        lambda value: value["terminal"].update(finished_at="2026-07-20T12:00:02Z"),
    ],
)
def test_summary_rejects_noncanonical_time_and_terminal_metadata(
    fixed_run_identity, mutate
) -> None:
    summary = _summary(fixed_run_identity)
    mutate(summary)

    with pytest.raises(WorkflowProgressSummaryError):
        normalize_workflow_progress_summary(summary)


def test_deserializer_rejects_non_text_malformed_and_non_object(fixed_run_identity) -> None:
    with pytest.raises(WorkflowProgressSummaryError, match="must be JSON text"):
        deserialize_workflow_progress_summary({})
    with pytest.raises(WorkflowProgressSummaryError, match="invalid JSON"):
        deserialize_workflow_progress_summary("{")
    with pytest.raises(WorkflowProgressSummaryError, match="exact protocol fields"):
        deserialize_workflow_progress_summary("[]")


def test_deserializer_rejects_invalid_utf8_surrogates(fixed_run_identity) -> None:
    serialized = serialize_workflow_progress_summary(_summary(fixed_run_identity))
    corrupted = serialized.replace(
        '"started_at":"2026-07-20T12:00:00Z"',
        r'"started_at":"\ud800Z"',
    )

    with pytest.raises(WorkflowProgressSummaryError, match="valid UTF-8"):
        deserialize_workflow_progress_summary(corrupted)


def test_deserializer_bounds_integer_conversion_failures() -> None:
    serialized = '{"summary_revision":' + "1" * 5_000 + "}"

    with pytest.raises(WorkflowProgressSummaryError, match="invalid JSON"):
        deserialize_workflow_progress_summary(serialized)
