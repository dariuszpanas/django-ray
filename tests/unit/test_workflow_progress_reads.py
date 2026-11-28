"""Contract tests for bounded authorized workflow-progress reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime, tzinfo
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

import django_ray.workflow.progress.reads as reads
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyPage,
)
from django_ray.workflow.plans import (
    PLAN_SELECTION_FORMAT_VERSION,
    PLAN_SELECTION_LEGACY_FORMAT_VERSION,
)
from django_ray.workflow.progress.reads import (
    WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES,
    WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES,
    WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES,
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
)
from django_ray.workflow.progress.runs import (
    WorkflowProgressDiagnosticCode,
    WorkflowProgressReadResult,
    WorkflowProgressReadSource,
    read_workflow_progress,
)
from django_ray.workflow.progress.storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_node_detail,
)
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from tests.unit._workflow_progress_reads_helpers import (
    _allow,
    _canonical_bytes,
    _decoded_cursor,
    _error_code,
    _plan_selection_manifest,
    _signed_cursor,
    _store_summary,
    _stored_summary,
    _summary_only_execution,
    _wire_bytes,
)
from tests.workflow_progress_storage_helpers import (
    publish_initial_workflow,
    workflow_detail,
    workflow_node_id,
    workflow_summary,
)
from tests.workflow_progress_summary_helpers import workflow_progress_summary

pytestmark = pytest.mark.django_db


def test_summary_pages_and_indexed_node_use_public_bounded_contract() -> None:
    published = publish_initial_workflow(257, case_id=127_001)

    summary = get_workflow_progress_summary(published.execution, authorize=_allow)
    topology = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=100,
    )
    topology_next = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        cursor=topology["next_cursor"],
        limit=100,
    )
    details = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=999,
    )
    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(150),
        authorize=_allow,
    )

    assert summary["source_schema_version"] == 3
    assert summary["availability"] == "AVAILABLE"
    assert summary["summary"]["storage"]["manifest_id"] is None
    assert "task_execution_pk" not in summary["run_identity"]
    assert topology["returned_count"] == 100
    assert topology["items"][0]["node_id"] == workflow_node_id(0)
    assert topology_next["items"][0]["node_id"] == workflow_node_id(100)
    assert details["returned_count"] == 256
    assert node["found"] is True
    assert node["item"]["node_id"] == workflow_node_id(150)
    assert "task_execution_pk" not in json.dumps(node)
    assert len(_canonical_bytes(details)) <= WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES
    assert len(details["next_cursor"].encode()) <= WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES


def test_authorization_is_repeated_before_cursor_or_storage_disclosure() -> None:
    published = publish_initial_workflow(3, case_id=127_002)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=2)
    calls: list[int] = []

    def deny(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return False

    with pytest.raises(WorkflowProgressReadError) as denied:
        list_workflow_node_details(
            published.execution,
            authorize=deny,
            cursor=first["next_cursor"],
            limit=2,
        )

    assert denied.value.code is WorkflowProgressReadErrorCode.ACCESS_DENIED
    assert calls == [published.execution.pk]


def test_cursor_tampering_and_request_mismatch_fail_explicitly() -> None:
    published = publish_initial_workflow(3, case_id=127_003)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=2)
    cursor = first["next_cursor"]
    assert cursor is not None

    with pytest.raises(WorkflowProgressReadError) as tampered:
        list_workflow_node_details(
            published.execution,
            authorize=_allow,
            cursor=f"{cursor[:-1]}x",
            limit=2,
        )
    with pytest.raises(WorkflowProgressReadError) as wrong_limit:
        list_workflow_node_details(
            published.execution,
            authorize=_allow,
            cursor=cursor,
            limit=1,
        )
    with pytest.raises(WorkflowProgressReadError) as wrong_filter:
        list_workflow_node_details(
            published.execution,
            authorize=_allow,
            cursor=cursor,
            state="PENDING",
            limit=2,
        )

    assert tampered.value.code is WorkflowProgressReadErrorCode.INVALID_CURSOR
    assert wrong_limit.value.code is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    assert wrong_filter.value.code is WorkflowProgressReadErrorCode.CURSOR_MISMATCH


def test_summary_update_expires_detail_and_topology_cursors() -> None:
    published = publish_initial_workflow(4, case_id=127_004)
    topology_first = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=2,
    )
    detail_first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=2,
    )
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(3), state="RUNNING"),
        identity=published.identity,
    )
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=4,
            running_count=1,
        ),
        manifest_id=published.manifest_id,
        prepared_topology=published.topology,
        detail_records=(changed,),
    )
    assert result.accepted

    topology_next = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        cursor=topology_first["next_cursor"],
        limit=2,
    )
    detail_next = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        cursor=detail_first["next_cursor"],
        limit=2,
    )

    assert topology_next["availability"] == "EXPIRED"
    assert topology_next["publication"]["summary_revision"] == 1
    assert topology_next["items"] == []
    assert topology_next["next_cursor"] is None
    assert detail_next["availability"] == "EXPIRED"
    assert detail_next["items"] == []
    assert detail_next["next_cursor"] is None


def test_state_filter_and_empty_single_node_preserve_availability() -> None:
    published = publish_initial_workflow(4, case_id=127_005)

    pending = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        state="pending",
    )
    running = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        state="RUNNING",
    )
    unknown = get_workflow_node_detail(
        published.execution,
        "unknown-node",
        authorize=_allow,
    )

    assert pending["returned_count"] == 4
    assert running["returned_count"] == 0
    assert running["availability"] == "AVAILABLE"
    assert unknown["found"] is False
    assert unknown["availability"] == "AVAILABLE"


def test_missing_storage_is_a_bounded_error_without_fallback() -> None:
    published = publish_initial_workflow(1, case_id=127_006)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).delete()

    with pytest.raises(WorkflowProgressReadError) as missing:
        list_workflow_topology_nodes(published.execution, authorize=_allow)

    assert missing.value.code is WorkflowProgressReadErrorCode.MISSING
    assert str(missing.value) == "Referenced workflow progress storage is missing."


def test_no_v3_summary_is_not_reported_and_legacy_compatibility_is_aggregate_only() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="legacy-read-127",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
        workflow_run_id="00000000-0000-0000-0000-000000127007",
    )
    execution.progress_data = json.dumps(
        {
            "schema_version": 2,
            "run_identity": {
                "schema_version": 1,
                "run_id": str(execution.workflow_run_id),
                "task_execution_pk": execution.pk,
                "attempt_number": execution.attempt_number,
                "execution_generation": execution.execution_generation,
            },
            "revision": 3,
            "state": "RUNNING",
            "total_nodes": 2,
            "completed_nodes": 1,
            "failed_nodes": 0,
            "running_nodes": 1,
            "pending_nodes": 0,
            "progress_percent": 50.0,
            "graph": {"nodes": [{"secret": "not-returned"}], "edges": []},
        }
    )
    execution.save(update_fields=["progress_data"])

    current_only = get_workflow_progress_summary(execution, authorize=_allow)
    compatibility = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    page = list_workflow_node_details(execution, authorize=_allow)

    assert current_only["availability"] == "NOT_REPORTED"
    assert current_only["summary"] is None
    assert compatibility["source_schema_version"] == 2
    assert compatibility["availability"] == "NOT_REPORTED"
    assert compatibility["complete"] is False
    assert compatibility["run_identity"] == compatibility["summary"]["run_identity"]
    assert compatibility["summary"]["node_counts"]["discovered"] == 2
    assert compatibility["summary"]["node_counts"]["retained_topology"] == 0
    assert compatibility["summary"]["node_counts"]["retained_detail"] == 0
    assert compatibility["summary"]["edge_counts"]["retained_topology"] == 0
    assert "graph" not in compatibility["summary"]
    assert "secret" not in json.dumps(compatibility)
    assert page["availability"] == "NOT_REPORTED"


@pytest.mark.parametrize(
    ("selection_version", "selected_strategy"),
    [
        (PLAN_SELECTION_FORMAT_VERSION, "dynamic_tasks"),
        (PLAN_SELECTION_LEGACY_FORMAT_VERSION, "local"),
    ],
)
def test_disabled_plan_selection_is_a_bounded_summary_only_signal(
    selection_version: int,
    selected_strategy: str,
) -> None:
    selection = _plan_selection_manifest(
        version=selection_version,
        selected_strategy=selected_strategy,
        reporting_policy="disabled",
    )
    execution = RayTaskExecution.objects.create(
        task_id=f"disabled-selection-{selection_version}-{selected_strategy}",
        callable_path="tests.disabled.workflow",
        state=TaskState.RUNNING,
        workflow_plan_selection=json.dumps(selection),
    )

    with CaptureQueriesContext(connection) as queries:
        response = get_workflow_progress_summary(execution, authorize=_allow)

    statements = "\n".join(query["sql"] for query in queries.captured_queries).lower()
    assert response["source_schema_version"] is None
    assert response["summary"] is None
    assert response["availability"] == "DISABLED"
    assert response["complete"] is False
    assert "progress_data" not in statements
    assert "workflowprogressnodedetail" not in statements
    assert "workflowprogresstopolog" not in statements
    assert "case" in statements
    assert "length" in statements
    assert len(queries) == 2


def test_dynamic_legacy_selection_without_a_summary_remains_not_reported() -> None:
    selection = _plan_selection_manifest(version=PLAN_SELECTION_LEGACY_FORMAT_VERSION)
    execution = RayTaskExecution.objects.create(
        task_id="full-legacy-selection",
        callable_path="tests.full.workflow",
        state=TaskState.RUNNING,
        workflow_plan_selection=json.dumps(selection),
    )

    response = get_workflow_progress_summary(execution, authorize=_allow)

    assert response["summary"] is None
    assert response["availability"] == "NOT_REPORTED"


def test_terminal_full_selection_without_v3_publication_is_missing() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="terminal-full-selection",
        callable_path="tests.full.workflow",
        state=TaskState.SUCCEEDED,
        workflow_plan_selection=json.dumps(_plan_selection_manifest()),
    )

    summary = get_workflow_progress_summary(execution, authorize=_allow)

    assert summary["summary"] is None
    assert summary["availability"] == "MISSING"
    with pytest.raises(WorkflowProgressReadError) as missing:
        list_workflow_topology_nodes(execution, authorize=_allow)
    assert missing.value.code is WorkflowProgressReadErrorCode.MISSING


def test_disabled_selection_query_is_fenced_against_a_new_workflow_run(
    monkeypatch,
) -> None:
    old_run_id = "00000000-0000-0000-0000-000000127230"
    new_run_id = "00000000-0000-0000-0000-000000127231"
    execution = RayTaskExecution.objects.create(
        task_id="selection-race",
        callable_path="tests.racing.workflow",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=old_run_id,
        workflow_plan_selection=json.dumps(_plan_selection_manifest()),
    )

    def advance_run(_execution, **_kwargs):
        RayTaskExecution.objects.filter(pk=execution.pk).update(
            attempt_number=2,
            execution_generation=2,
            workflow_run_id=new_run_id,
            workflow_plan_selection=json.dumps(
                _plan_selection_manifest(reporting_policy="disabled")
            ),
        )
        return None

    monkeypatch.setattr(reads, "_read_schema_v3_summary", advance_run)

    response = get_workflow_progress_summary(execution, authorize=_allow)

    execution.refresh_from_db()
    assert execution.attempt_number == 2
    assert response["availability"] == "NOT_REPORTED"
    assert response["summary"] is None


def test_legacy_summary_clamps_untrusted_primitives_and_bounds_the_response() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="legacy-malformed-primitives-127",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
    )
    huge_counter = 10**1_000
    execution.progress_data = json.dumps(
        {
            "schema_version": 1,
            "run_identity": {
                "run_id": "x" * 100_000,
                "attempt_number": True,
                "execution_generation": -1,
            },
            "revision": huge_counter,
            "state": "x" * 100_000,
            "total_nodes": huge_counter,
            "completed_nodes": -1,
            "failed_nodes": True,
            "running_nodes": "not-an-integer" * 10_000,
            "pending_nodes": huge_counter,
            "progress_percent": float("inf"),
            "updated_at": {"untrusted": "value" * 10_000},
            "graph": {"nodes": [], "edges": [{"source": "a", "target": "b"}]},
            "diagnostic": "private" * 10_000,
        }
    )
    execution.save(update_fields=["progress_data"])

    response = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    summary = response["summary"]

    assert response["availability"] == "NOT_REPORTED"
    assert response["complete"] is False
    assert response["run_identity"] is None
    assert summary["run_identity"] is None
    assert summary["revision"] == (1 << 63) - 1
    assert summary["state"] == "RUNNING"
    assert summary["node_counts"] == {
        "declared": (1 << 63) - 1,
        "discovered": (1 << 63) - 1,
        "retained_topology": 0,
        "retained_detail": 0,
        "pending": (1 << 63) - 1,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert summary["edge_counts"]["retained_topology"] == 0
    assert summary["progress_percent"] == 0.0
    assert summary["updated_at"] == 0.0
    assert "private" not in json.dumps(response)
    assert len(_wire_bytes(response)) <= WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES


def test_retained_attempt_reads_authorize_the_owner_after_current_task_advances() -> None:
    published = publish_initial_workflow(3, case_id=127_008)
    published.execution.refresh_from_db()
    terminal_summary = published.execution.workflow_progress_summary_json
    assert terminal_summary is not None
    TaskAttempt.objects.create(
        execution=published.execution,
        attempt_number=1,
        state=TaskState.SUCCEEDED,
        workflow_progress_summary_json=terminal_summary,
    )
    published.execution.attempt_number = 2
    published.execution.execution_generation = 2
    published.execution.workflow_run_id = "00000000-0000-0000-0000-000000127009"
    published.execution.workflow_progress_summary_json = None
    published.execution.save(
        update_fields=[
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_progress_summary_json",
        ]
    )
    calls: list[int] = []

    def authorize_owner(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return True

    summary = get_workflow_progress_summary(
        published.execution,
        authorize=authorize_owner,
        attempt_number=1,
    )
    detail = list_workflow_node_details(
        published.execution,
        authorize=authorize_owner,
        attempt_number=1,
    )

    assert summary["run_identity"]["attempt_number"] == 1
    assert detail["returned_count"] == 3
    assert calls == [published.execution.pk, published.execution.pk]


def test_archived_attempt_summary_uses_one_bounded_attempt_projection() -> None:
    published = publish_initial_workflow(1, case_id=127_133)
    terminal_summary = _stored_summary(published.execution)
    TaskAttempt.objects.create(
        execution=published.execution,
        attempt_number=1,
        state=TaskState.SUCCEEDED,
        workflow_progress_summary_json=serialize_workflow_progress_summary(terminal_summary),
    )
    published.execution.attempt_number = 2
    published.execution.execution_generation = 2
    published.execution.workflow_run_id = "00000000-0000-0000-0000-000000127134"
    published.execution.workflow_progress_summary_json = None
    published.execution.save(
        update_fields=[
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_progress_summary_json",
        ]
    )

    with CaptureQueriesContext(connection) as queries:
        response = get_workflow_progress_summary(
            published.execution,
            authorize=_allow,
            attempt_number=1,
        )

    selects = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
    ]
    attempt_selects = [sql for sql in selects if "django_ray_taskattempt" in sql.lower()]
    assert response["run_identity"]["attempt_number"] == 1
    assert len(selects) == 2
    assert len(attempt_selects) == 1
    assert "CASE" in attempt_selects[0].upper()
    assert "LENGTH" in attempt_selects[0].upper()


def test_empty_topology_edges_return_a_complete_bounded_page() -> None:
    published = publish_initial_workflow(3, case_id=127_010)

    edges = list_workflow_topology_edges(published.execution, authorize=_allow)

    assert edges["availability"] == "AVAILABLE"
    assert edges["complete"] is True
    assert edges["returned_count"] == 0
    assert edges["next_cursor"] is None


@pytest.mark.parametrize(
    "availability",
    ["NOT_REPORTED", "OMITTED_BY_POLICY", "DISABLED"],
)
def test_summary_only_availability_states_return_empty_detail_contracts(
    availability: str,
) -> None:
    execution = _summary_only_execution(
        availability,
        case_id={
            "NOT_REPORTED": 127_101,
            "OMITTED_BY_POLICY": 127_102,
            "DISABLED": 127_103,
        }[availability],
    )

    summary = get_workflow_progress_summary(execution, authorize=_allow)
    page = list_workflow_node_details(execution, authorize=_allow)
    node = get_workflow_node_detail(execution, "node-00000", authorize=_allow)

    assert summary["availability"] == availability
    assert summary["summary"]["detail"]["availability"] == availability
    assert summary["complete"] is False
    assert page["availability"] == availability
    assert page["items"] == []
    assert page["next_cursor"] is None
    assert node["availability"] == availability
    assert node["found"] is False


def test_truncated_detail_remains_readable_without_claiming_completeness() -> None:
    published = publish_initial_workflow(3, case_id=127_104)
    summary = _stored_summary(published.execution)
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit"],
    }
    _store_summary(published.execution, summary)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_truncation_reasons="detail_count_limit"
    )

    envelope = get_workflow_progress_summary(published.execution, authorize=_allow)
    topology = list_workflow_topology_nodes(published.execution, authorize=_allow)
    details = list_workflow_node_details(published.execution, authorize=_allow)

    assert envelope["availability"] == "TRUNCATED"
    assert envelope["complete"] is False
    assert topology["returned_count"] == 3
    assert topology["complete"] is False
    assert details["returned_count"] == 3
    assert details["complete"] is False


@pytest.mark.parametrize(
    ("availability", "expected_code"),
    [
        ("MISSING", WorkflowProgressReadErrorCode.MISSING),
        ("CORRUPT", WorkflowProgressReadErrorCode.CORRUPT),
    ],
)
def test_fault_availability_keeps_summary_readable_but_blocks_detail(
    availability: str,
    expected_code: WorkflowProgressReadErrorCode,
) -> None:
    published = publish_initial_workflow(
        1,
        case_id=127_105 if availability == "MISSING" else 127_106,
    )
    summary = _stored_summary(published.execution)
    summary["detail"] = {
        "availability": availability,
        "complete": False,
        "truncation_reasons": [],
    }
    _store_summary(published.execution, summary)

    envelope = get_workflow_progress_summary(published.execution, authorize=_allow)

    assert envelope["availability"] == availability
    assert envelope["summary"] is not None
    assert (
        _error_code(lambda: list_workflow_topology_nodes(published.execution, authorize=_allow))
        is expected_code
    )
    assert (
        _error_code(lambda: list_workflow_node_details(published.execution, authorize=_allow))
        is expected_code
    )
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is expected_code
    )


def test_terminal_expiry_overrides_available_storage_without_reading_it(
    monkeypatch,
) -> None:
    published = publish_initial_workflow(1, case_id=127_107)
    summary = _stored_summary(published.execution)
    finished_at = "2026-07-20T12:00:02Z"
    summary.update(state="SUCCEEDED", progress_percent=100.0)
    summary["node_counts"].update(pending=0, succeeded=1)
    summary["timestamps"].update(updated_at=finished_at, finished_at=finished_at)
    summary["retention"].update(detail_days=0, detail_expires_at=finished_at)
    summary["terminal"] = {"outcome": "SUCCEEDED", "finished_at": finished_at}
    _store_summary(published.execution, summary)
    after_expiry = datetime(2026, 7, 20, 12, 0, 3, tzinfo=UTC)

    class _AfterExpiry(datetime):
        @classmethod
        def now(cls, tz=None):
            return after_expiry if tz is not None else after_expiry.replace(tzinfo=None)

    envelope = get_workflow_progress_summary(
        published.execution,
        authorize=_allow,
        generated_at=after_expiry,
    )
    monkeypatch.setattr(reads, "datetime", _AfterExpiry)
    page = list_workflow_node_details(published.execution, authorize=_allow)

    assert envelope["availability"] == "EXPIRED"
    assert envelope["summary"]["detail"]["availability"] == "EXPIRED"
    assert page["availability"] == "EXPIRED"
    assert page["items"] == []
    assert page["next_cursor"] is None


@pytest.mark.parametrize(
    "summary_case",
    ["malformed", "oversized", "noncanonical", "cross_run"],
)
def test_invalid_v3_summary_never_falls_back_to_legacy(
    summary_case: str,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id=f"invalid-v3-{summary_case}",
        callable_path="tests.invalid.workflow",
        state=TaskState.RUNNING,
        workflow_run_id="00000000-0000-0000-0000-000000127108",
    )
    valid = workflow_progress_summary(execution)
    if summary_case == "malformed":
        stored = "{"
    elif summary_case == "oversized":
        stored = "x" * (reads.WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES + 1)
    elif summary_case == "noncanonical":
        stored = json.dumps(valid, indent=2)
    else:
        other = RayTaskExecution.objects.create(
            task_id="cross-run-summary-owner",
            callable_path="tests.invalid.workflow",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000127109",
        )
        stored = serialize_workflow_progress_summary(workflow_progress_summary(other))
    execution.workflow_progress_summary_json = stored
    execution.progress_data = json.dumps(
        {
            "schema_version": 1,
            "revision": 99,
            "graph": {"nodes": [{"secret": "must-not-fallback"}], "edges": []},
        }
    )
    execution.save(update_fields=["workflow_progress_summary_json", "progress_data"])

    with pytest.raises(WorkflowProgressReadError) as raised:
        get_workflow_progress_summary(execution, authorize=_allow, include_legacy=True)

    assert raised.value.code is WorkflowProgressReadErrorCode.CORRUPT
    assert "must-not-fallback" not in str(raised.value)


def test_every_operation_repeats_object_authorization() -> None:
    published = publish_initial_workflow(3, case_id=127_110)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=2)
    cursor = first["next_cursor"]
    assert cursor is not None
    calls: list[int] = []

    def deny(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return False

    operations = [
        lambda: get_workflow_progress_summary(published.execution, authorize=deny),
        lambda: list_workflow_topology_nodes(published.execution, authorize=deny),
        lambda: list_workflow_node_details(published.execution, authorize=deny),
        lambda: list_workflow_node_details(
            published.execution,
            authorize=deny,
            cursor=cursor,
            limit=2,
        ),
        lambda: get_workflow_node_detail(
            published.execution,
            workflow_node_id(0),
            authorize=deny,
        ),
    ]

    assert [_error_code(operation) for operation in operations] == [
        WorkflowProgressReadErrorCode.ACCESS_DENIED
    ] * len(operations)
    assert calls == [published.execution.pk] * len(operations)


def test_authorization_policy_exceptions_are_bounded_and_redacted() -> None:
    published = publish_initial_workflow(1, case_id=127_111)

    def fail_policy(_execution: RayTaskExecution) -> bool:
        raise RuntimeError("private policy implementation detail")

    with pytest.raises(WorkflowProgressReadError) as raised:
        get_workflow_progress_summary(published.execution, authorize=fail_policy)

    assert raised.value.code is WorkflowProgressReadErrorCode.ACCESS_DENIED
    assert str(raised.value) == "Workflow progress access is denied."
    assert "private" not in str(raised.value)


def test_authorization_precedes_oversized_cursor_validation() -> None:
    published = publish_initial_workflow(1, case_id=127_112)
    calls: list[int] = []

    def deny(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return False

    code = _error_code(
        lambda: list_workflow_node_details(
            published.execution,
            authorize=deny,
            cursor="x" * (WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES + 1),
        )
    )

    assert code is WorkflowProgressReadErrorCode.ACCESS_DENIED
    assert calls == [published.execution.pk]


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    [
        (
            lambda execution, authorize: get_workflow_progress_summary(
                execution,
                authorize=authorize,
                include_legacy="yes",
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: get_workflow_progress_summary(
                execution,
                authorize=authorize,
                generated_at="not-a-datetime",
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: list_workflow_topology_nodes(
                execution,
                authorize=authorize,
                limit=0,
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: list_workflow_node_details(
                execution,
                authorize=authorize,
                state=object(),
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: get_workflow_node_detail(
                execution,
                "",
                authorize=authorize,
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: list_workflow_node_details(
                execution,
                authorize=authorize,
                cursor="x" * (WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES + 1),
            ),
            WorkflowProgressReadErrorCode.INVALID_CURSOR,
        ),
    ],
)
def test_saved_execution_authorizes_once_before_request_validation(
    operation,
    expected_code: WorkflowProgressReadErrorCode,
) -> None:
    published = publish_initial_workflow(1, case_id=127_123)
    calls: list[int] = []

    def allow(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return True

    assert _error_code(lambda: operation(published.execution, allow)) is expected_code
    assert calls == [published.execution.pk]

    calls.clear()

    def deny(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return False

    assert (
        _error_code(lambda: operation(published.execution, deny))
        is WorkflowProgressReadErrorCode.ACCESS_DENIED
    )
    assert calls == [published.execution.pk]


def test_cursor_rejects_oversize_schema_owner_collection_and_order_mismatch() -> None:
    published = publish_initial_workflow(3, case_id=127_113)
    other = publish_initial_workflow(3, case_id=127_114)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=2)
    cursor = first["next_cursor"]
    assert cursor is not None

    cases = {
        "oversized": (
            "x" * (WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES + 1),
            published.execution,
            WorkflowProgressReadErrorCode.INVALID_CURSOR,
        ),
        "schema": (
            _signed_cursor(cursor, v=2),
            published.execution,
            WorkflowProgressReadErrorCode.INVALID_CURSOR,
        ),
        "owner": (
            cursor,
            other.execution,
            WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        ),
        "collection": (
            _signed_cursor(cursor, collection="topology_nodes"),
            published.execution,
            WorkflowProgressReadErrorCode.INVALID_CURSOR,
        ),
        "order": (
            _signed_cursor(cursor, order="node_id_desc"),
            published.execution,
            WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        ),
    }

    for value, execution, expected in cases.values():
        assert (
            _error_code(
                lambda value=value, execution=execution: list_workflow_node_details(
                    execution,
                    authorize=_allow,
                    cursor=value,
                    limit=2,
                )
            )
            is expected
        )


def test_detail_cursor_carries_strict_publication_identity_and_cumulative_seen() -> None:
    published = publish_initial_workflow(5, case_id=127_124)
    first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=2,
    )
    first_cursor = first["next_cursor"]
    assert first_cursor is not None
    first_payload = _decoded_cursor(first_cursor)

    second = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        cursor=first_cursor,
        limit=2,
    )
    second_cursor = second["next_cursor"]
    assert second_cursor is not None
    second_payload = _decoded_cursor(second_cursor)

    assert set(first_payload) == reads._CURSOR_FIELDS
    assert first_payload["run_identity"] == first["run_identity"]
    assert first_payload["summary_revision"] == first["publication"]["summary_revision"]
    assert first_payload["seen"] == first["returned_count"] == 2
    assert second_payload["seen"] == 4
    assert second_payload["run_identity"] == first_payload["run_identity"]
    assert second_payload["summary_revision"] == first_payload["summary_revision"]


def test_topology_cursor_binds_summary_but_not_detail_revision() -> None:
    published = publish_initial_workflow(3, case_id=127_125)

    first = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=2,
    )
    cursor = first["next_cursor"]
    assert cursor is not None
    payload = _decoded_cursor(cursor)

    assert payload["run_identity"] == first["run_identity"]
    assert payload["summary_revision"] == first["publication"]["summary_revision"]
    assert payload["topology_version"] == first["publication"]["topology_version"]
    assert payload["detail_revision"] is None
    assert payload["seen"] == 2


def test_cursor_rejects_invalid_bound_identity_revision_and_seen_shapes() -> None:
    published = publish_initial_workflow(3, case_id=127_126)
    first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=2,
    )
    cursor = first["next_cursor"]
    assert cursor is not None
    payload = _decoded_cursor(cursor)
    identity = payload["run_identity"]
    assert isinstance(identity, dict)
    invalid_identities = [
        {},
        {**identity, "task_execution_pk": published.execution.pk},
        {**identity, "schema_version": 2},
        {**identity, "schema_version": True},
        {**identity, "run_id": "not-a-uuid"},
        {**identity, "attempt_number": 0},
        {**identity, "attempt_number": True},
        {**identity, "execution_generation": -1},
        {**identity, "execution_generation": True},
    ]
    invalid_updates = [{"run_identity": value} for value in invalid_identities] + [
        {"summary_revision": 0},
        {"summary_revision": True},
        {"summary_revision": "1"},
        {"seen": -1},
        {"seen": True},
        {"seen": "2"},
    ]

    for update in invalid_updates:
        invalid = _signed_cursor(cursor, **update)
        try:
            list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=invalid,
                limit=2,
            )
        except WorkflowProgressReadError as error:
            assert error.code is WorkflowProgressReadErrorCode.INVALID_CURSOR
        else:
            pytest.fail(f"cursor accepted invalid bound fields: {update!r}")


@pytest.mark.parametrize("collection", ["detail", "topology"])
def test_expired_cursor_preserves_its_original_public_run_and_publication(
    collection: str,
) -> None:
    published = publish_initial_workflow(
        3,
        case_id=127_127 if collection == "detail" else 127_128,
    )
    if collection == "detail":
        first = list_workflow_node_details(
            published.execution,
            authorize=_allow,
            limit=2,
        )
    else:
        first = list_workflow_topology_nodes(
            published.execution,
            authorize=_allow,
            limit=2,
        )
    cursor = first["next_cursor"]
    assert cursor is not None
    original_identity = first["run_identity"]
    original_publication = {
        **first["publication"],
        "detail_revision": (
            first["publication"]["detail_revision"] if collection == "detail" else None
        ),
    }

    published.execution.execution_generation = 2
    published.execution.workflow_run_id = "00000000-0000-0000-0000-000000127129"
    published.execution.save(update_fields=["execution_generation", "workflow_run_id"])
    _store_summary(
        published.execution,
        workflow_progress_summary(published.execution),
    )

    if collection == "detail":
        retired = list_workflow_node_details(
            published.execution,
            authorize=_allow,
            cursor=cursor,
            limit=2,
        )
    else:
        retired = list_workflow_topology_nodes(
            published.execution,
            authorize=_allow,
            cursor=cursor,
            limit=2,
        )

    assert retired["availability"] == "EXPIRED"
    assert retired["complete"] is False
    assert retired["run_identity"] == original_identity
    assert retired["publication"] == original_publication
    assert retired["items"] == []
    assert retired["next_cursor"] is None


@pytest.mark.parametrize("corruption", ["manifest", "page", "node"])
def test_corrupted_storage_metadata_returns_only_the_bounded_error(
    corruption: str,
) -> None:
    published = publish_initial_workflow(
        1,
        case_id={"manifest": 127_115, "page": 127_116, "node": 127_117}[corruption],
    )
    if corruption == "manifest":
        WorkflowProgressTopologyManifest.objects.filter(pk=published.manifest_id).update(
            manifest_digest="f" * 64
        )
    elif corruption == "page":
        WorkflowProgressTopologyPage.objects.filter(
            run_storage__execution=published.execution
        ).update(digest="e" * 64)
    else:
        WorkflowProgressNodeDetail.objects.filter(
            run_storage__execution=published.execution,
            node_id=workflow_node_id(0),
        ).update(digest="d" * 64)

    def operation() -> dict[str, Any]:
        if corruption in {"manifest", "page"}:
            return list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
            )
        return get_workflow_node_detail(
            published.execution,
            workflow_node_id(0),
            authorize=_allow,
        )

    with pytest.raises(WorkflowProgressReadError) as raised:
        operation()

    assert raised.value.code is WorkflowProgressReadErrorCode.CORRUPT
    assert str(raised.value) == "Workflow progress storage failed validation."
    assert "digest" not in str(raised.value).lower()


def test_sparse_publication_accepts_unchanged_rows_with_older_epochs() -> None:
    published = publish_initial_workflow(3, case_id=127_118)
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(2), state="RUNNING"),
        identity=published.identity,
    )
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=3,
            running_count=1,
        ),
        manifest_id=published.manifest_id,
        prepared_topology=published.topology,
        detail_records=(changed,),
    )
    assert result.accepted
    unchanged_row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=workflow_node_id(0),
    )
    changed_row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=workflow_node_id(2),
    )

    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(0),
        authorize=_allow,
    )

    assert unchanged_row.last_detail_revision == 1
    assert changed_row.last_detail_revision == 2
    assert node["publication"]["detail_revision"] == 2
    assert node["found"] is True
    assert node["item"]["state"] == "PENDING"


def test_detail_final_page_reports_a_missing_retained_child() -> None:
    published = publish_initial_workflow(4, case_id=127_130)
    last_row = (
        WorkflowProgressNodeDetail.objects.filter(run_storage__execution=published.execution)
        .order_by("-node_key")
        .first()
    )
    assert last_row is not None
    last_row.delete()

    first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=2,
    )
    assert first["returned_count"] == 2
    assert first["next_cursor"] is not None

    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=first["next_cursor"],
                limit=2,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_detail_final_page_reports_an_unaccounted_extra_child() -> None:
    published = publish_initial_workflow(4, case_id=127_131)
    run_storage = WorkflowProgressRunStorage.objects.get(execution=published.execution)
    extra = prepare_workflow_progress_node_detail(
        workflow_detail("unaccounted-extra-node"),
        identity=published.identity,
    )
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key=extra.node_key,
        node_id=extra.node_id,
        invocation_id=extra.invocation_id,
        state=extra.state,
        event_count=extra.event_count,
        truncated=extra.truncated,
        payload=extra.payload,
        digest=extra.digest,
        encoded_bytes=extra.encoded_bytes,
        decoded_bytes=extra.decoded_bytes,
        last_topology_version=1,
        last_detail_revision=1,
    )

    cursor = None
    observed_code = None
    for _ in range(4):
        try:
            page = list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=cursor,
                limit=2,
            )
        except WorkflowProgressReadError as error:
            observed_code = error.code
            break
        cursor = page["next_cursor"]
        assert cursor is not None, "extra retained row was silently accepted"

    assert observed_code is WorkflowProgressReadErrorCode.CORRUPT


def test_filtered_detail_final_page_uses_the_expected_state_count() -> None:
    published = publish_initial_workflow(5, case_id=127_132)
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(0), state="RUNNING"),
        identity=published.identity,
    )
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=5,
            running_count=1,
        ),
        manifest_id=published.manifest_id,
        prepared_topology=published.topology,
        detail_records=(changed,),
    )
    assert result.accepted
    running = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        state="RUNNING",
        limit=2,
    )
    assert running["returned_count"] == 1
    assert running["next_cursor"] is None

    last_pending = (
        WorkflowProgressNodeDetail.objects.filter(
            run_storage__execution=published.execution,
            state="PENDING",
        )
        .order_by("-node_key")
        .first()
    )
    assert last_pending is not None
    last_pending.delete()
    first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        state="PENDING",
        limit=2,
    )
    assert first["returned_count"] == 2
    assert first["next_cursor"] is not None

    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                state="PENDING",
                cursor=first["next_cursor"],
                limit=2,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_explicit_current_attempt_needs_no_archived_attempt_row() -> None:
    published = publish_initial_workflow(1, case_id=127_119)
    assert not TaskAttempt.objects.filter(execution=published.execution).exists()

    summary = get_workflow_progress_summary(
        published.execution,
        authorize=_allow,
        attempt_number=published.execution.attempt_number,
    )
    page = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        attempt_number=published.execution.attempt_number,
    )

    assert summary["run_identity"]["attempt_number"] == 1
    assert page["returned_count"] == 1


@pytest.mark.parametrize(
    "operation",
    [
        lambda execution: get_workflow_progress_summary(
            execution,
            authorize=_allow,
            attempt_number=99,
        ),
        lambda execution: list_workflow_node_details(
            execution,
            authorize=_allow,
            attempt_number=99,
        ),
        lambda execution: get_workflow_node_detail(
            execution,
            workflow_node_id(0),
            authorize=_allow,
            attempt_number=99,
        ),
    ],
)
def test_missing_archived_attempt_is_not_found(operation) -> None:
    published = publish_initial_workflow(1, case_id=127_120)

    assert (
        _error_code(lambda: operation(published.execution))
        is WorkflowProgressReadErrorCode.NOT_FOUND
    )


def test_page_construction_obeys_decoded_response_and_cursor_caps(monkeypatch) -> None:
    published = publish_initial_workflow(10, case_id=127_121)
    decoded_limit = 700
    response_limit = 2_048
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES", decoded_limit)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", response_limit)

    page = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=256,
    )

    assert 0 < page["returned_count"] < 10
    assert sum(len(_canonical_bytes(item)) for item in page["items"]) <= decoded_limit
    assert len(_wire_bytes(page)) <= response_limit
    assert page["next_cursor"] is not None
    assert len(page["next_cursor"].encode("utf-8")) <= WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES
    assert decoded_limit < WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES


def test_v3_summary_only_sql_never_references_legacy_or_detail_storage() -> None:
    published = publish_initial_workflow(1, case_id=127_122)
    deferred = RayTaskExecution.objects.only("pk").get(pk=published.execution.pk)

    with CaptureQueriesContext(connection) as queries:
        response = get_workflow_progress_summary(
            deferred,
            authorize=_allow,
            include_legacy=False,
        )

    statements = "\n".join(query["sql"] for query in queries.captured_queries).lower()
    assert response["source_schema_version"] == 3
    assert "progress_data" not in statements
    assert "workflowprogressnodedetail" not in statements
    assert "workflowprogresstopolog" not in statements
    assert "workflowprogressrunstorage" not in statements
    assert len(queries) <= 2


def test_compatibility_reader_routes_through_the_execution_database_alias(
    monkeypatch,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="compatibility-reader-database-alias",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
        progress_data=json.dumps({"schema_version": 1, "revision": 1}),
    )
    manager = RayTaskExecution.objects
    original_using = manager.using
    aliases: list[str] = []

    def routed_using(alias: str):
        aliases.append(alias)
        return original_using("default")

    monkeypatch.setattr(manager, "using", routed_using)
    execution._state.db = "workflow-progress-replica"

    result = read_workflow_progress(execution)

    assert result.ok
    assert result.schema_version == 1
    assert aliases == ["workflow-progress-replica"]


def test_invalid_execution_attempt_authorizer_and_deleted_owner_are_bounded() -> None:
    unsaved = RayTaskExecution(
        task_id="unsaved-workflow-read",
        callable_path="tests.invalid.workflow",
    )
    assert (
        _error_code(lambda: get_workflow_progress_summary(unsaved, authorize=_allow))
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )
    unsaved.pk = 1 << 63
    assert (
        _error_code(lambda: get_workflow_progress_summary(unsaved, authorize=_allow))
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )

    published = publish_initial_workflow(1, case_id=127_201)
    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=_allow,
                attempt_number=0,
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )
    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=_allow,
                attempt_number=1 << 63,
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )
    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=None,  # type: ignore[arg-type]
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )

    stale = published.execution
    RayTaskExecution.objects.filter(pk=stale.pk).delete()
    assert (
        _error_code(lambda: get_workflow_progress_summary(stale, authorize=_allow))
        is WorkflowProgressReadErrorCode.NOT_FOUND
    )


def test_current_summary_must_match_the_current_run_generation() -> None:
    published = publish_initial_workflow(1, case_id=127_202)
    summary = _stored_summary(published.execution)
    summary["run_identity"]["execution_generation"] = 2
    _store_summary(published.execution, summary)

    assert (
        _error_code(lambda: get_workflow_progress_summary(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_invalid_persisted_current_attempt_fails_closed() -> None:
    published = publish_initial_workflow(1, case_id=127_2021)
    RayTaskExecution.objects.filter(pk=published.execution.pk).update(attempt_number=0)
    published.execution.refresh_from_db()

    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_naive_generated_at_is_normalized_before_terminal_expiry_comparison() -> None:
    published = publish_initial_workflow(1, case_id=127_203)
    summary = _stored_summary(published.execution)
    finished_at = "2026-07-20T12:00:02Z"
    summary.update(state="SUCCEEDED", progress_percent=100.0)
    summary["node_counts"].update(pending=0, succeeded=1)
    summary["timestamps"].update(updated_at=finished_at, finished_at=finished_at)
    summary["retention"].update(detail_days=0, detail_expires_at=finished_at)
    summary["terminal"] = {"outcome": "SUCCEEDED", "finished_at": finished_at}
    _store_summary(published.execution, summary)

    response = get_workflow_progress_summary(
        published.execution,
        authorize=_allow,
        generated_at=datetime(2026, 7, 20, 12, 0, 3),
    )

    assert response["generated_at"] == "2026-07-20T12:00:03Z"
    assert response["availability"] == "EXPIRED"


def test_invalid_protocol_expiry_is_ignored_without_exposing_storage(
    monkeypatch,
) -> None:
    published = publish_initial_workflow(1, case_id=127_204)
    original = reads.public_workflow_progress_summary

    def invalid_expiry(summary: dict[str, Any]) -> dict[str, Any]:
        public = original(summary)
        public["retention"]["detail_expires_at"] = "not-a-protocol-time"
        return public

    monkeypatch.setattr(reads, "public_workflow_progress_summary", invalid_expiry)

    response = get_workflow_progress_summary(published.execution, authorize=_allow)

    assert response["availability"] == "AVAILABLE"
    assert response["summary"]["storage"]["manifest_id"] is None

    def naive_expiry(summary: dict[str, Any]) -> dict[str, Any]:
        public = original(summary)
        public["retention"]["detail_expires_at"] = "2099-07-20T12:00:00"
        return public

    monkeypatch.setattr(reads, "public_workflow_progress_summary", naive_expiry)
    still_available = get_workflow_progress_summary(
        published.execution,
        authorize=_allow,
        generated_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )
    assert still_available["availability"] == "AVAILABLE"


def test_invalid_generated_at_timezone_is_a_bounded_argument() -> None:
    published = publish_initial_workflow(1, case_id=127_239)

    class _BrokenTimezone(tzinfo):
        def utcoffset(self, _value):
            raise ValueError("untrusted timezone implementation detail")

        def dst(self, _value):
            return None

        def tzname(self, _value: datetime | None) -> None:
            return None

    generated_at = datetime(2026, 7, 20, tzinfo=_BrokenTimezone())

    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=_allow,
                generated_at=generated_at,
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )


def test_legacy_numeric_fields_are_bounded_across_finite_and_overflow_inputs() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="legacy-numeric-bounds-127",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
    )
    huge = 10**1_000
    execution.progress_data = json.dumps(
        {
            "schema_version": 1,
            "progress_percent": huge,
            "updated_at": 123.25,
        }
    )
    execution.save(update_fields=["progress_data"])

    finite = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    assert finite["summary"]["progress_percent"] == 0.0
    assert finite["summary"]["updated_at"] == 123.25

    execution.progress_data = json.dumps(
        {
            "schema_version": 1,
            "progress_percent": 25,
            "updated_at": huge,
        }
    )
    execution.save(update_fields=["progress_data"])
    overflow = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    assert overflow["summary"]["progress_percent"] == 25.0
    assert overflow["summary"]["updated_at"] == 0.0


def test_compatibility_reader_diagnostics_and_summary_race_map_to_public_contract(
    monkeypatch,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="compatibility-result-mapping-127",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
        workflow_run_id="00000000-0000-0000-0000-000000127205",
    )
    cases = [
        (
            WorkflowProgressReadResult(
                source=WorkflowProgressReadSource.NONE,
                diagnostic_code=WorkflowProgressDiagnosticCode.ROW_MISSING,
            ),
            WorkflowProgressReadErrorCode.NOT_FOUND,
        ),
        (
            WorkflowProgressReadResult(
                source=WorkflowProgressReadSource.LEGACY,
                diagnostic_code=WorkflowProgressDiagnosticCode.MALFORMED_JSON,
            ),
            WorkflowProgressReadErrorCode.CORRUPT,
        ),
    ]
    for result, expected in cases:
        monkeypatch.setattr(reads, "read_workflow_progress", lambda _execution, r=result: r)
        assert (
            _error_code(
                lambda: get_workflow_progress_summary(
                    execution,
                    authorize=_allow,
                    include_legacy=True,
                )
            )
            is expected
        )

    summary = workflow_progress_summary(execution)
    monkeypatch.setattr(
        reads,
        "read_workflow_progress",
        lambda _execution: WorkflowProgressReadResult(
            source=WorkflowProgressReadSource.SUMMARY,
            payload=summary,
            schema_version=3,
        ),
    )
    recovered = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    assert recovered["source_schema_version"] == 3
    assert recovered["summary"]["schema_version"] == 3

    mismatched_legacy = {
        "schema_version": 2,
        "run_identity": {
            "schema_version": 1,
            "run_id": "00000000-0000-0000-0000-000000127999",
            "task_execution_pk": execution.pk,
            "attempt_number": execution.attempt_number,
            "execution_generation": execution.execution_generation,
        },
    }
    monkeypatch.setattr(
        reads,
        "read_workflow_progress",
        lambda _execution: WorkflowProgressReadResult(
            source=WorkflowProgressReadSource.LEGACY,
            payload=mismatched_legacy,
            schema_version=2,
        ),
    )
    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                execution,
                authorize=_allow,
                include_legacy=True,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )
