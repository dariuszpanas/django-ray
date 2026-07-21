"""Boundaries, compatibility, and fencing for schema-v3 progress summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

import django_ray.workflow_progress as progress_module
import django_ray.workflow_progress_summary as summary_module
from django_ray.lifecycle import cancel_task, record_failure, retry_task, succeed_task
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.observability import (
    get_workflow_graph,
)
from django_ray.observability import (
    get_workflow_progress as get_public_workflow_progress,
)
from django_ray.runner.reconciliation import mark_task_lost, mark_task_timed_out
from django_ray.runtime.context import WORKFLOW_PROGRESS_SCHEMA_VERSION, WorkflowRunIdentity
from django_ray.workflow_plans import PlanEligibility
from django_ray.workflow_progress import (
    WorkflowProgressDiagnosticCode,
    WorkflowProgressReadSource,
    WorkflowProgressSummaryConflictError,
    _assign_workflow_progress_summary_locked,
    claim_workflow_run,
    persist_workflow_progress_summary,
    read_workflow_progress,
)
from django_ray.workflow_progress_summary import (
    WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS,
    WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES,
    WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
    WorkflowProgressSummaryError,
    deserialize_workflow_progress_summary,
    normalize_workflow_progress_summary,
    public_workflow_progress_summary,
    serialize_workflow_progress_summary,
)


def _identity(
    execution: RayTaskExecution,
    run_id: str = "00000000-0000-0000-0000-000000000125",
) -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=run_id,
    )


def _persist_locked_summary(identity, summary) -> bool:
    """Exercise the package-owned publication hook reserved for #126."""
    serialized = serialize_workflow_progress_summary(summary, expected_identity=identity)
    with transaction.atomic():
        locked = RayTaskExecution.objects.select_for_update().get(pk=identity.task_execution_pk)
        return _assign_workflow_progress_summary_locked(locked, identity, serialized)


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


@pytest.fixture
def running_execution(db) -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id="workflow-summary-125",
        callable_path="tests.unit.test_workflow_progress_summary.workflow",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=4,
        workflow_run_id="00000000-0000-0000-0000-000000000125",
    )


def test_schema_v3_is_canonical_bounded_and_hides_internal_manifest(running_execution) -> None:
    identity = _identity(running_execution)
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
    assert decoded["run_identity"]["task_execution_pk"] == running_execution.pk


def test_summary_byte_limit_accepts_exact_boundary_and_rejects_next_byte(
    running_execution,
    monkeypatch,
) -> None:
    value = _summary(_identity(running_execution))
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
def test_summary_rejects_graph_records_and_unknown_fields(running_execution, mutation) -> None:
    value = _summary(_identity(running_execution))
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
    running_execution,
    path,
    value,
    message,
) -> None:
    summary = _summary(_identity(running_execution))
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
def test_summary_requires_canonical_complete_run_identity(running_execution, field, value) -> None:
    summary = _summary(_identity(running_execution))
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
    running_execution,
    mutation,
    message,
) -> None:
    summary = _summary(_identity(running_execution))
    mutation(summary)

    with pytest.raises(WorkflowProgressSummaryError, match=message):
        normalize_workflow_progress_summary(summary)


def test_summary_requires_topology_for_detail_and_complete_available_detail(
    running_execution,
) -> None:
    identity = _identity(running_execution)
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
    running_execution,
    state,
    expires_at,
    message,
) -> None:
    summary = _summary(
        _identity(running_execution),
        published_detail=True,
        state=state,
    )
    summary["retention"]["detail_expires_at"] = expires_at

    with pytest.raises(WorkflowProgressSummaryError, match=message):
        normalize_workflow_progress_summary(summary)


def test_canonical_json_wraps_unserializable_values() -> None:
    with pytest.raises(WorkflowProgressSummaryError, match="not canonical JSON"):
        summary_module._canonical_json({"unsupported": object()})


def test_summary_rejects_a_different_expected_run(running_execution) -> None:
    summary = _summary(_identity(running_execution))
    other = _identity(running_execution, "00000000-0000-0000-0000-000000000126")

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
    running_execution,
    mutation,
) -> None:
    summary = _summary(_identity(running_execution))
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
def test_summary_rejects_inconsistent_aggregate_counts(running_execution, mutate) -> None:
    summary = _summary(_identity(running_execution))
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
    running_execution,
    mutate,
) -> None:
    summary = _summary(_identity(running_execution), state="SUCCEEDED")
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
def test_summary_rejects_inconsistent_detail_availability(running_execution, mutate) -> None:
    summary = _summary(_identity(running_execution))
    mutate(summary)

    with pytest.raises(WorkflowProgressSummaryError):
        normalize_workflow_progress_summary(summary)


def test_truncated_detail_requires_sorted_unique_protocol_reasons(running_execution) -> None:
    summary = _summary(_identity(running_execution), published_detail=True)
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


def test_expired_detail_requires_terminal_workflow_state(running_execution) -> None:
    summary = _summary(_identity(running_execution), published_detail=True)
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
def test_summary_rejects_noncanonical_time_and_terminal_metadata(running_execution, mutate) -> None:
    summary = _summary(_identity(running_execution))
    mutate(summary)

    with pytest.raises(WorkflowProgressSummaryError):
        normalize_workflow_progress_summary(summary)


def test_deserializer_rejects_non_text_malformed_and_non_object(running_execution) -> None:
    with pytest.raises(WorkflowProgressSummaryError, match="must be JSON text"):
        deserialize_workflow_progress_summary({})
    with pytest.raises(WorkflowProgressSummaryError, match="invalid JSON"):
        deserialize_workflow_progress_summary("{")
    with pytest.raises(WorkflowProgressSummaryError, match="exact protocol fields"):
        deserialize_workflow_progress_summary("[]")


def test_deserializer_rejects_invalid_utf8_surrogates(running_execution) -> None:
    serialized = serialize_workflow_progress_summary(_summary(_identity(running_execution)))
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


@pytest.mark.django_db
def test_fenced_summary_writer_is_monotonic_idempotent_and_exact(running_execution) -> None:
    identity = _identity(running_execution)
    first = _summary(identity)

    assert persist_workflow_progress_summary(identity, first) is True
    assert persist_workflow_progress_summary(identity, first) is True

    conflicting = _summary(identity)
    conflicting["progress_percent"] = 1.0
    with pytest.raises(WorkflowProgressSummaryConflictError, match="did not advance"):
        persist_workflow_progress_summary(identity, conflicting)

    second = _summary(identity, summary_revision=2)
    second["progress_percent"] = 1.0
    assert persist_workflow_progress_summary(identity, second) is True
    running_execution.refresh_from_db()
    assert running_execution.workflow_progress_summary_json == serialize_workflow_progress_summary(
        second,
        expected_identity=identity,
    )


@pytest.mark.django_db
def test_locked_summary_writer_rejects_noncanonical_json(running_execution) -> None:
    identity = _identity(running_execution)
    noncanonical = json.dumps(_summary(identity), indent=2)

    with transaction.atomic():
        locked = RayTaskExecution.objects.select_for_update().get(pk=running_execution.pk)
        with pytest.raises(WorkflowProgressSummaryConflictError, match="canonical JSON"):
            _assign_workflow_progress_summary_locked(locked, identity, noncanonical)

    running_execution.refresh_from_db()
    assert running_execution.workflow_progress_summary_json is None


@pytest.mark.django_db
def test_locked_summary_writer_rejects_stale_fence_and_invalid_summary(running_execution) -> None:
    identity = _identity(running_execution)
    serialized = serialize_workflow_progress_summary(_summary(identity))

    with transaction.atomic():
        locked = RayTaskExecution.objects.select_for_update().get(pk=running_execution.pk)
        stale = _identity(
            running_execution,
            "00000000-0000-0000-0000-000000000126",
        )
        assert _assign_workflow_progress_summary_locked(locked, stale, serialized) is False
        with pytest.raises(WorkflowProgressSummaryConflictError, match="schema validation"):
            _assign_workflow_progress_summary_locked(locked, identity, "{}")


@pytest.mark.django_db
def test_summary_writer_rejects_invalid_plan_selection_and_noncanonical_accepted_state(
    running_execution,
) -> None:
    identity = _identity(running_execution)
    running_execution.workflow_plan_selection = "{"
    running_execution.save(update_fields=["workflow_plan_selection"])
    with pytest.raises(WorkflowProgressSummaryConflictError, match="strategy selection is invalid"):
        persist_workflow_progress_summary(identity, _summary(identity))

    running_execution.workflow_plan_selection = None
    running_execution.workflow_progress_summary_json = json.dumps(_summary(identity), indent=2)
    running_execution.save(
        update_fields=["workflow_plan_selection", "workflow_progress_summary_json"]
    )
    with pytest.raises(WorkflowProgressSummaryConflictError, match="accepted .* not canonical"):
        persist_workflow_progress_summary(
            identity,
            _summary(identity, summary_revision=2),
        )


@pytest.mark.django_db
def test_summary_writer_rejects_revision_regression_and_corrupt_accepted_state(
    running_execution,
) -> None:
    identity = _identity(running_execution)
    published = _summary(identity, published_detail=True)
    assert _persist_locked_summary(identity, published)

    regressed = _summary(identity, summary_revision=2)
    with pytest.raises(WorkflowProgressSummaryConflictError, match="topology_version regressed"):
        _persist_locked_summary(identity, regressed)

    RayTaskExecution.objects.filter(pk=running_execution.pk).update(
        workflow_progress_summary_json="{}"
    )
    with pytest.raises(WorkflowProgressSummaryConflictError, match="is corrupt"):
        persist_workflow_progress_summary(identity, _summary(identity, summary_revision=3))


@pytest.mark.django_db
def test_summary_writer_rejects_time_state_and_manifest_regressions(running_execution) -> None:
    identity = _identity(running_execution)
    first = _summary(identity, published_detail=True)
    assert _persist_locked_summary(identity, first)

    earlier_update = _summary(identity, summary_revision=2, published_detail=True)
    earlier_update["timestamps"]["updated_at"] = "2026-07-20T12:00:00Z"  # type: ignore[index]
    with pytest.raises(WorkflowProgressSummaryConflictError, match="timestamp regressed"):
        _persist_locked_summary(identity, earlier_update)

    changed_start = _summary(identity, summary_revision=2, published_detail=True)
    changed_start["timestamps"]["started_at"] = "2026-07-20T11:59:59Z"  # type: ignore[index]
    with pytest.raises(WorkflowProgressSummaryConflictError, match="start timestamp"):
        _persist_locked_summary(identity, changed_start)

    changed_manifest = _summary(identity, summary_revision=2, published_detail=True)
    changed_manifest["storage"]["manifest_id"] = "manifest_126"  # type: ignore[index]
    with pytest.raises(WorkflowProgressSummaryConflictError, match="manifest changed"):
        _persist_locked_summary(identity, changed_manifest)

    terminal = _summary(identity, summary_revision=2, published_detail=True, state="FAILED")
    assert _persist_locked_summary(identity, terminal)
    assert _persist_locked_summary(identity, terminal)
    with pytest.raises(WorkflowProgressSummaryConflictError, match="terminal .* cannot advance"):
        _persist_locked_summary(
            identity,
            _summary(identity, summary_revision=3, published_detail=True, state="FAILED"),
        )


@pytest.mark.django_db
def test_standalone_summary_writer_cannot_publish_detail_pointers(running_execution) -> None:
    identity = _identity(running_execution)

    with pytest.raises(WorkflowProgressSummaryConflictError, match="atomic storage publication"):
        persist_workflow_progress_summary(
            identity,
            _summary(identity, published_detail=True),
        )

    running_execution.refresh_from_db()
    assert running_execution.workflow_progress_summary_json is None


@pytest.mark.django_db
def test_summary_writer_reserves_final_revision_for_lifecycle(running_execution) -> None:
    identity = _identity(running_execution)

    with pytest.raises(WorkflowProgressSummaryConflictError, match="lifecycle terminal revision"):
        persist_workflow_progress_summary(
            identity,
            _summary(
                identity,
                summary_revision=(1 << 63) - 1,
                state="FAILED",
            ),
        )

    running_execution.refresh_from_db()
    assert running_execution.workflow_progress_summary_json is None


@pytest.mark.django_db
def test_summary_writer_checks_pinned_plan_and_strategy(running_execution) -> None:
    fingerprint = "sha256:" + "a" * 64
    selection = PlanEligibility(("dynamic_tasks",), (), 0).select(
        "dynamic_tasks",
        requested_policy="auto",
    )
    running_execution.workflow_plan_fingerprint = fingerprint
    running_execution.workflow_plan_selection = json.dumps(selection.as_dict())
    running_execution.save(update_fields=["workflow_plan_fingerprint", "workflow_plan_selection"])
    identity = _identity(running_execution)

    assert persist_workflow_progress_summary(
        identity,
        _summary(
            identity,
            selected_strategy="dynamic_tasks",
            plan_fingerprint=fingerprint,
        ),
    )

    wrong_plan = _summary(
        identity,
        summary_revision=2,
        selected_strategy="dynamic_tasks",
        plan_fingerprint="sha256:" + "b" * 64,
    )
    with pytest.raises(WorkflowProgressSummaryConflictError, match="plan fingerprint"):
        persist_workflow_progress_summary(identity, wrong_plan)

    wrong_strategy = _summary(
        identity,
        summary_revision=2,
        selected_strategy="local",
        plan_fingerprint=fingerprint,
    )
    with pytest.raises(WorkflowProgressSummaryConflictError, match="execution strategy"):
        persist_workflow_progress_summary(identity, wrong_strategy)


@pytest.mark.django_db
def test_summary_writer_projects_only_bounded_coordination_fields(running_execution) -> None:
    running_execution.progress_data = "legacy-graph" * 1_000
    running_execution.args_json = json.dumps(["private-input"])
    running_execution.save(update_fields=["progress_data", "args_json"])

    with CaptureQueriesContext(connection) as queries:
        assert persist_workflow_progress_summary(
            _identity(running_execution),
            _summary(_identity(running_execution)),
        )

    task_selects = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
        and "django_ray_raytaskexecution" in query["sql"]
    ]
    assert len(task_selects) == 1
    assert "progress_data" not in task_selects[0]
    assert "args_json" not in task_selects[0]


@pytest.mark.django_db
@pytest.mark.parametrize("fence", ["state", "attempt", "generation", "run_id"])
def test_summary_writer_rejects_each_stale_fence(running_execution, fence) -> None:
    identity = _identity(running_execution)
    updates = {
        "state": {"state": TaskState.CANCELLING},
        "attempt": {"attempt_number": 3},
        "generation": {"execution_generation": 5},
        "run_id": {"workflow_run_id": "00000000-0000-0000-0000-000000000126"},
    }
    RayTaskExecution.objects.filter(pk=running_execution.pk).update(**updates[fence])

    assert persist_workflow_progress_summary(identity, _summary(identity)) is False


@pytest.mark.django_db
def test_claiming_replacement_run_clears_v3_and_legacy_progress(running_execution) -> None:
    old = _identity(running_execution)
    assert persist_workflow_progress_summary(old, _summary(old))
    running_execution.progress_data = json.dumps({"schema_version": 1, "revision": 7})
    running_execution.save(update_fields=["progress_data"])
    replacement = _identity(
        running_execution,
        "00000000-0000-0000-0000-000000000126",
    )

    assert claim_workflow_run(replacement)
    running_execution.refresh_from_db()
    assert running_execution.workflow_progress_summary_json is None
    assert running_execution.progress_data is None


@pytest.mark.django_db
def test_bounded_reader_prefers_v3_and_uses_one_database_statement(running_execution) -> None:
    identity = _identity(running_execution)
    summary = _summary(identity, published_detail=True)
    running_execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
    running_execution.progress_data = json.dumps({"schema_version": 1, "revision": 99})
    running_execution.save(update_fields=["workflow_progress_summary_json", "progress_data"])
    deferred = RayTaskExecution.objects.defer(
        "workflow_progress_summary_json",
        "progress_data",
    ).get(pk=running_execution.pk)

    with CaptureQueriesContext(connection) as queries:
        result = read_workflow_progress(deferred)

    assert len(queries) == 1
    assert result.ok
    assert result.source is WorkflowProgressReadSource.SUMMARY
    assert result.schema_version == 3
    assert result.payload == normalize_workflow_progress_summary(summary)


@pytest.mark.django_db
def test_public_v3_reader_hides_database_and_manifest_identifiers(running_execution) -> None:
    summary = _summary(_identity(running_execution), published_detail=True)
    running_execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
    running_execution.save(update_fields=["workflow_progress_summary_json"])

    public = get_public_workflow_progress(running_execution)

    assert public is not None
    assert "task_execution_pk" not in public["run_identity"]
    assert public["storage"]["manifest_id"] is None
    assert get_workflow_graph(running_execution) is None


@pytest.mark.django_db
def test_bounded_reader_never_falls_back_from_corrupt_v3(running_execution) -> None:
    running_execution.workflow_progress_summary_json = "{}"
    running_execution.progress_data = json.dumps({"schema_version": 1, "revision": 99})
    running_execution.save(update_fields=["workflow_progress_summary_json", "progress_data"])

    result = read_workflow_progress(running_execution)

    assert result.source is WorkflowProgressReadSource.SUMMARY
    assert result.payload is None
    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.SUMMARY_INVALID


@pytest.mark.django_db
def test_bounded_reader_rejects_noncanonical_v3(running_execution) -> None:
    running_execution.workflow_progress_summary_json = json.dumps(
        _summary(_identity(running_execution)),
        indent=2,
    )
    running_execution.progress_data = json.dumps({"schema_version": 1, "revision": 99})
    running_execution.save(update_fields=["workflow_progress_summary_json", "progress_data"])

    result = read_workflow_progress(running_execution)

    assert result.source is WorkflowProgressReadSource.SUMMARY
    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.SUMMARY_INVALID
    assert result.payload is None


@pytest.mark.django_db
def test_bounded_reader_reports_invalid_utf8_surrogate_as_bounded_diagnostic(
    running_execution,
) -> None:
    serialized = serialize_workflow_progress_summary(_summary(_identity(running_execution)))
    running_execution.workflow_progress_summary_json = serialized.replace(
        '"started_at":"2026-07-20T12:00:00Z"',
        r'"started_at":"\ud800Z"',
    )
    running_execution.save(update_fields=["workflow_progress_summary_json"])

    result = read_workflow_progress(running_execution)

    assert result.source is WorkflowProgressReadSource.SUMMARY
    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.SUMMARY_INVALID
    assert result.payload is None
    assert result.diagnostic_message == (
        "workflow progress summary failed bounded schema validation"
    )


@pytest.mark.django_db
def test_bounded_reader_reports_mismatched_v3_identity(running_execution) -> None:
    other_identity = _identity(
        running_execution,
        "00000000-0000-0000-0000-000000000126",
    )
    running_execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
        _summary(other_identity)
    )
    running_execution.save(update_fields=["workflow_progress_summary_json"])

    result = read_workflow_progress(running_execution)

    assert result.source is WorkflowProgressReadSource.SUMMARY
    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.IDENTITY_MISMATCH
    assert result.payload is None


def test_bounded_reader_requires_current_identity_for_v3_summary() -> None:
    execution = SimpleNamespace(
        pk=1,
        task_id="v3-without-current-run",
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=None,
    )
    identity = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=1,
        run_id="00000000-0000-0000-0000-000000000125",
    )
    execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
        _summary(identity)
    )

    result = read_workflow_progress(execution)

    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.IDENTITY_MISMATCH


@pytest.mark.django_db
def test_bounded_reader_does_not_return_oversized_summary(
    running_execution,
    monkeypatch,
) -> None:
    serialized = serialize_workflow_progress_summary(_summary(_identity(running_execution)))
    running_execution.workflow_progress_summary_json = serialized
    running_execution.save(update_fields=["workflow_progress_summary_json"])
    monkeypatch.setattr(progress_module, "WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES", len(serialized) - 1)

    with CaptureQueriesContext(connection) as queries:
        result = read_workflow_progress(running_execution)

    assert len(queries) == 1
    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.SUMMARY_OVERSIZED
    assert result.payload is None


@pytest.mark.django_db
def test_bounded_reader_reports_a_deleted_task_row(running_execution) -> None:
    execution_id = running_execution.pk
    deferred = RayTaskExecution.objects.only("pk").get(pk=execution_id)
    RayTaskExecution.objects.filter(pk=execution_id).delete()

    result = read_workflow_progress(deferred)

    assert result.source is WorkflowProgressReadSource.NONE
    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.ROW_MISSING
    assert result.payload is None


@pytest.mark.django_db
def test_legacy_reader_counts_multibyte_utf8_in_database(
    running_execution,
    monkeypatch,
) -> None:
    running_execution.workflow_progress_summary_json = None
    running_execution.progress_data = json.dumps(
        {"schema_version": 1, "message": "ééé"},
        ensure_ascii=False,
    )
    running_execution.save(update_fields=["workflow_progress_summary_json", "progress_data"])
    encoded = running_execution.progress_data.encode("utf-8")
    monkeypatch.setattr(progress_module, "WORKFLOW_PROGRESS_LEGACY_MAX_BYTES", len(encoded) - 1)

    with CaptureQueriesContext(connection) as queries:
        result = read_workflow_progress(running_execution)

    assert len(queries) == 1
    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.LEGACY_OVERSIZED
    assert result.payload is None


def test_octet_length_uses_oracle_byte_function(monkeypatch) -> None:
    captured = {}

    def fake_as_sql(self, compiler, connection, **extra_context):
        captured.update(extra_context)
        return "LENGTHB(value)", []

    monkeypatch.setattr(progress_module._OctetLength, "as_sql", fake_as_sql)
    expression = progress_module._OctetLength("progress_data")

    assert expression.as_oracle(None, None) == ("LENGTHB(value)", [])
    assert captured["function"] == "LENGTHB"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("{", WorkflowProgressDiagnosticCode.MALFORMED_JSON),
        ("[]", WorkflowProgressDiagnosticCode.INVALID_SHAPE),
        (json.dumps({"schema_version": True}), WorkflowProgressDiagnosticCode.INVALID_VERSION),
        (json.dumps({"schema_version": 9}), WorkflowProgressDiagnosticCode.UNKNOWN_VERSION),
    ],
)
def test_legacy_reader_returns_stable_bounded_diagnostics(value, code) -> None:
    result = read_workflow_progress(SimpleNamespace(progress_data=value, task_id="legacy"))

    assert result.source is WorkflowProgressReadSource.LEGACY
    assert result.diagnostic_code is code
    assert result.payload is None
    assert result.diagnostic_message is not None
    assert len(result.diagnostic_message.encode("utf-8")) < 256


def test_legacy_reader_bounds_integer_conversion_failures() -> None:
    value = '{"revision":' + "1" * 5_000 + "}"

    result = read_workflow_progress(SimpleNamespace(progress_data=value, task_id="legacy"))

    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.MALFORMED_JSON
    assert result.payload is None


def test_legacy_reader_supports_v1_empty_and_exact_v2_identity() -> None:
    assert read_workflow_progress(SimpleNamespace(progress_data=None, task_id="none")).source is (
        WorkflowProgressReadSource.NONE
    )
    assert read_workflow_progress(SimpleNamespace(progress_data="", task_id="empty")).source is (
        WorkflowProgressReadSource.NONE
    )
    v1 = read_workflow_progress(
        SimpleNamespace(progress_data=json.dumps({"revision": 2}), task_id="v1")
    )
    assert v1.payload == {"revision": 2}
    assert v1.schema_version == 1

    execution = SimpleNamespace(
        pk=7,
        task_id="v2",
        attempt_number=2,
        execution_generation=3,
        workflow_run_id="00000000-0000-0000-0000-000000000127",
        progress_data=json.dumps(
            {
                "schema_version": 2,
                "run_identity": {
                    "schema_version": 1,
                    "task_execution_pk": 7,
                    "attempt_number": 2,
                    "execution_generation": 3,
                    "run_id": "00000000-0000-0000-0000-000000000127",
                },
            }
        ),
    )
    v2 = read_workflow_progress(execution)
    assert v2.ok
    assert v2.schema_version == 2

    mismatched = json.loads(execution.progress_data)
    mismatched["run_identity"]["execution_generation"] = 4
    execution.progress_data = json.dumps(mismatched)
    assert read_workflow_progress(execution).diagnostic_code is (
        WorkflowProgressDiagnosticCode.IDENTITY_MISMATCH
    )


@pytest.mark.parametrize(
    "case",
    [
        "boolean_schema",
        "boolean_pk",
        "boolean_attempt",
        "boolean_generation",
        "float_pk",
        "extra_field",
        "missing_field",
        "noncanonical_uuid",
        "invalid_uuid",
        "non_string_uuid",
    ],
)
def test_legacy_v2_reader_strictly_validates_run_identity(case) -> None:
    run_id = "00000000-0000-0000-0000-00000000012a"
    identity = {
        "schema_version": 1,
        "task_execution_pk": 1,
        "attempt_number": 1,
        "execution_generation": 1,
        "run_id": run_id,
    }
    mutations = {
        "boolean_schema": lambda: identity.update(schema_version=True),
        "boolean_pk": lambda: identity.update(task_execution_pk=True),
        "boolean_attempt": lambda: identity.update(attempt_number=True),
        "boolean_generation": lambda: identity.update(execution_generation=True),
        "float_pk": lambda: identity.update(task_execution_pk=1.0),
        "extra_field": lambda: identity.update(extra="not-allowed"),
        "missing_field": lambda: identity.pop("attempt_number"),
        "noncanonical_uuid": lambda: identity.update(run_id=run_id.upper()),
        "invalid_uuid": lambda: identity.update(run_id="not-a-uuid"),
        "non_string_uuid": lambda: identity.update(run_id=None),
    }
    mutations[case]()
    execution = SimpleNamespace(
        pk=1,
        task_id="strict-v2",
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=run_id,
        progress_data=json.dumps({"schema_version": 2, "run_identity": identity}),
    )

    result = read_workflow_progress(execution)

    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.IDENTITY_MISMATCH


@pytest.mark.django_db
def test_retry_archives_one_bounded_summary_before_clearing_current(running_execution) -> None:
    identity = _identity(running_execution)
    terminal = _summary(identity, state="FAILED")
    assert persist_workflow_progress_summary(identity, terminal)

    assert record_failure(running_execution, error_message="retry", retry=True)

    running_execution.refresh_from_db()
    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    assert attempt.workflow_progress_summary_json == serialize_workflow_progress_summary(terminal)
    assert running_execution.workflow_progress_summary_json is None
    assert running_execution.workflow_run_id is None
    assert running_execution.attempt_number == 3
    assert TaskAttempt.objects.filter(execution=running_execution, attempt_number=2).count() == 1
    assert "graph" not in deserialize_workflow_progress_summary(
        attempt.workflow_progress_summary_json
    )


@pytest.mark.django_db
def test_matching_producer_terminal_summary_archives_exact_detail_expiry(
    running_execution,
) -> None:
    identity = _identity(running_execution)
    terminal = _summary(
        identity,
        published_detail=True,
        state="FAILED",
    )
    serialized = serialize_workflow_progress_summary(terminal)
    assert _persist_locked_summary(identity, terminal)

    assert record_failure(running_execution, error_message="failed", retry=False)

    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    assert attempt.workflow_progress_summary_json == serialized
    archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
    assert archived["retention"]["detail_expires_at"] == "2026-07-27T12:00:02Z"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("transition", "summary_state"),
    [
        ("success", "SUCCEEDED"),
        ("failure", "FAILED"),
        ("cancellation", "CANCELLED"),
        ("timeout", "FAILED"),
        ("lost", "LOST"),
    ],
)
def test_terminal_lifecycle_transitions_derive_one_bounded_terminal_summary(
    running_execution,
    transition,
    summary_state,
) -> None:
    identity = _identity(running_execution)
    running = _summary(identity)
    assert persist_workflow_progress_summary(identity, running)

    if transition == "success":
        assert succeed_task(running_execution, result_data="{}", result_reference=None)
    elif transition == "failure":
        assert record_failure(running_execution, error_message="failed", retry=False)
    elif transition == "cancellation":
        RayTaskExecution.objects.filter(pk=running_execution.pk).update(state=TaskState.CANCELLING)
        running_execution.refresh_from_db()
        assert cancel_task(running_execution)
    elif transition == "timeout":
        running_execution.timeout_seconds = 5
        running_execution.save(update_fields=["timeout_seconds"])
        assert mark_task_timed_out(running_execution)
    else:
        assert mark_task_lost(running_execution)

    running_execution.refresh_from_db()
    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    assert attempt.state == summary_state
    archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
    assert archived["state"] == summary_state
    assert archived["terminal"]["outcome"] == summary_state
    assert archived["summary_revision"] == 2
    assert archived["timestamps"]["finished_at"] is not None
    if summary_state == "SUCCEEDED":
        assert archived["node_counts"]["pending"] == 0
        assert archived["node_counts"]["succeeded"] == 1
        assert archived["progress_percent"] == 100.0
    else:
        assert archived["node_counts"]["pending"] == 1
        assert archived["progress_percent"] == 0.0
    assert "graph" not in archived
    assert (
        running_execution.workflow_progress_summary_json == attempt.workflow_progress_summary_json
    )
    assert TaskAttempt.objects.filter(execution=running_execution, attempt_number=2).count() == 1


@pytest.mark.django_db
def test_lifecycle_reserves_final_revision_and_sets_published_detail_expiry(
    running_execution,
) -> None:
    identity = _identity(running_execution)
    running = _summary(
        identity,
        summary_revision=(1 << 63) - 2,
        published_detail=True,
    )
    assert _persist_locked_summary(identity, running)

    assert mark_task_lost(running_execution)

    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
    assert archived["summary_revision"] == (1 << 63) - 1
    assert archived["retention"]["detail_expires_at"] is not None


@pytest.mark.django_db
def test_lifecycle_derives_expiry_at_protocol_timestamp_boundary(running_execution) -> None:
    identity = _identity(running_execution)
    maximum_run_timestamp = datetime.max.replace(tzinfo=UTC) - timedelta(
        days=WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS
    )
    maximum_run_text = maximum_run_timestamp.isoformat().replace("+00:00", "Z")
    running = _summary(identity, published_detail=True)
    running["timestamps"].update(
        started_at=maximum_run_text,
        updated_at=maximum_run_text,
    )
    running["retention"]["detail_days"] = WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS
    assert _persist_locked_summary(identity, running)

    assert mark_task_lost(running_execution)

    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
    assert archived["timestamps"]["finished_at"] == maximum_run_text
    assert archived["retention"]["detail_expires_at"] == (
        datetime.max.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    )


@pytest.mark.django_db
def test_unrepresentable_detail_expiry_never_blocks_retry(running_execution) -> None:
    identity = _identity(running_execution)
    assert _persist_locked_summary(
        identity,
        _summary(identity, published_detail=True),
    )
    RayTaskExecution.objects.filter(pk=running_execution.pk).update(
        state=TaskState.FAILED,
        finished_at=datetime.max.replace(tzinfo=UTC),
    )

    assert retry_task(running_execution.pk) is not None

    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    assert attempt.workflow_progress_summary_json is None


@pytest.mark.django_db
def test_lost_transition_rejects_a_stale_nonrunning_snapshot(running_execution) -> None:
    RayTaskExecution.objects.filter(pk=running_execution.pk).update(state=TaskState.SUCCEEDED)

    assert mark_task_lost(running_execution) is False
    assert not TaskAttempt.objects.filter(execution=running_execution).exists()


@pytest.mark.django_db
def test_outer_terminal_outcome_overrides_conflicting_accepted_summary(
    running_execution,
) -> None:
    identity = _identity(running_execution)
    assert persist_workflow_progress_summary(
        identity,
        _summary(identity, state="SUCCEEDED"),
    )

    assert mark_task_lost(running_execution)

    running_execution.refresh_from_db()
    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
    assert running_execution.state == TaskState.LOST
    assert archived["state"] == "LOST"
    assert archived["terminal"]["outcome"] == "LOST"
    assert archived["summary_revision"] == 2


@pytest.mark.django_db
def test_retry_preserves_an_already_archived_terminal_summary(running_execution) -> None:
    identity = _identity(running_execution)
    terminal = _summary(identity, state="LOST")
    serialized = serialize_workflow_progress_summary(terminal)
    assert persist_workflow_progress_summary(identity, terminal)
    assert mark_task_lost(running_execution)
    RayTaskExecution.objects.filter(pk=running_execution.pk).update(
        workflow_progress_summary_json="{}"
    )

    assert retry_task(running_execution.pk) is not None

    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    assert attempt.workflow_progress_summary_json == serialized


@pytest.mark.django_db
def test_corrupt_summary_never_blocks_retry_or_enters_attempt_history(running_execution) -> None:
    RayTaskExecution.objects.filter(pk=running_execution.pk).update(
        state=TaskState.FAILED,
        workflow_progress_summary_json="x" * (WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES + 1),
    )

    assert retry_task(running_execution.pk) is not None

    running_execution.refresh_from_db()
    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    assert attempt.workflow_progress_summary_json is None
    assert running_execution.workflow_progress_summary_json is None


@pytest.mark.django_db
def test_retry_derives_terminal_summary_from_last_running_summary(running_execution) -> None:
    identity = _identity(running_execution)
    serialized = serialize_workflow_progress_summary(_summary(identity))
    RayTaskExecution.objects.filter(pk=running_execution.pk).update(
        state=TaskState.FAILED,
        workflow_progress_summary_json=serialized,
    )

    assert retry_task(running_execution.pk) is not None

    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
    assert archived["state"] == "FAILED"
    assert archived["terminal"]["outcome"] == "FAILED"


@pytest.mark.django_db
def test_retry_does_not_archive_noncanonical_terminal_summary(running_execution) -> None:
    identity = _identity(running_execution)
    serialized = json.dumps(_summary(identity, state="FAILED"), indent=2)
    RayTaskExecution.objects.filter(pk=running_execution.pk).update(
        state=TaskState.FAILED,
        workflow_progress_summary_json=serialized,
    )

    assert retry_task(running_execution.pk) is not None

    attempt = TaskAttempt.objects.get(execution=running_execution, attempt_number=2)
    assert attempt.workflow_progress_summary_json is None


def test_runtime_complete_snapshot_producer_remains_schema_v2() -> None:
    assert WORKFLOW_PROGRESS_SCHEMA_VERSION == 2
    assert WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION == 3
