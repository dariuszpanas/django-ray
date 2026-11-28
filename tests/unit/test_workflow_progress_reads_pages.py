"""Pagination, storage integrity, presentation redaction, and single-node read tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from django.core import signing

import django_ray.workflow.progress.reads as reads
import django_ray.workflow.progress.storage as storage
from django_ray.lifecycle import succeed_task
from django_ray.models import (
    RayTaskExecution,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
)
from django_ray.redaction import REDACTED
from django_ray.workflow.progress.preparation import prepare_workflow_progress_topology
from django_ray.workflow.progress.reads import (
    WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES,
    WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES,
    WorkflowProgressReadErrorCode,
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
)
from django_ray.workflow.progress.storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_node_detail,
    stage_workflow_progress_topology,
)
from tests.unit._workflow_progress_reads_helpers import (
    _allow,
    _archive_workflow_attempt,
    _canonical_bytes,
    _decoded_cursor,
    _error_code,
    _legacy_ansi_workflow,
    _publish_with_legacy_terminal_redaction,
    _publish_workflow_with_edges,
    _signed_cursor,
    _store_summary,
    _stored_summary,
    _summary_only_execution,
    _wire_bytes,
)
from tests.workflow_progress_storage_helpers import (
    publish_initial_workflow,
    workflow_detail,
    workflow_node,
    workflow_node_id,
    workflow_summary,
)

pytestmark = pytest.mark.django_db


def test_cursor_transport_rejects_empty_nontext_nonencodable_and_nonobject_values() -> None:
    published = publish_initial_workflow(2, case_id=127_206)
    invalid_cursors = [
        "",
        object(),
        "\ud800",
        signing.dumps([], salt=reads._CURSOR_SALT, compress=False),
        signing.dumps({"unexpected": True}, salt=reads._CURSOR_SALT, compress=False),
    ]

    for cursor in invalid_cursors:
        assert (
            _error_code(
                lambda cursor=cursor: list_workflow_node_details(
                    published.execution,
                    authorize=_allow,
                    cursor=cursor,  # type: ignore[arg-type]
                    limit=1,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_CURSOR
        )

    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                cursor="",
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_CURSOR
    )


def test_resigned_cursor_rejects_malformed_bound_fields_and_positions() -> None:
    published = publish_initial_workflow(2, case_id=127_207)
    detail = list_workflow_node_details(published.execution, authorize=_allow, limit=1)
    topology = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=1)
    detail_cursor = detail["next_cursor"]
    topology_cursor = topology["next_cursor"]
    assert detail_cursor is not None
    assert topology_cursor is not None

    identity = _decoded_cursor(detail_cursor)["run_identity"]
    malformed_detail = [
        _signed_cursor(detail_cursor, position=[0, 1]),
        _signed_cursor(detail_cursor, after="g" * 64),
        _signed_cursor(detail_cursor, run="g" * 64),
        _signed_cursor(detail_cursor, run="0" * 64),
        _signed_cursor(
            detail_cursor,
            run_identity={**identity, "run_id": "z" * 36},
        ),
    ]
    for cursor in malformed_detail:
        assert (
            _error_code(
                lambda cursor=cursor: list_workflow_node_details(
                    published.execution,
                    authorize=_allow,
                    cursor=cursor,
                    limit=1,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_CURSOR
        )

    malformed_topology = [
        _signed_cursor(topology_cursor, position=None),
        _signed_cursor(topology_cursor, after=""),
        _signed_cursor(topology_cursor, detail_revision=1),
    ]
    for cursor in malformed_topology:
        assert (
            _error_code(
                lambda cursor=cursor: list_workflow_topology_nodes(
                    published.execution,
                    authorize=_allow,
                    cursor=cursor,
                    limit=1,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_CURSOR
        )

    for malformed_after in (None, ["only-one"], ["", "target"]):
        cursor = _signed_cursor(
            topology_cursor,
            collection="topology_edges",
            order="source_target_asc",
            after=malformed_after,
        )
        assert (
            _error_code(
                lambda cursor=cursor: list_workflow_topology_edges(
                    published.execution,
                    authorize=_allow,
                    cursor=cursor,
                    limit=1,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_CURSOR
        )


def test_genuine_cross_endpoint_cursors_report_request_mismatch() -> None:
    published = publish_initial_workflow(2, case_id=127_208)
    detail = list_workflow_node_details(published.execution, authorize=_allow, limit=1)
    topology = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=1)
    assert detail["next_cursor"] is not None
    assert topology["next_cursor"] is not None

    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                cursor=detail["next_cursor"],
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    )
    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=topology["next_cursor"],
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    )


def test_cursor_without_a_current_summary_returns_its_original_expired_envelope() -> None:
    published = publish_initial_workflow(2, case_id=127_209)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=1)
    cursor = first["next_cursor"]
    assert cursor is not None
    RayTaskExecution.objects.filter(pk=published.execution.pk).update(
        workflow_progress_summary_json=None
    )

    expired = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        cursor=cursor,
        limit=1,
    )

    assert expired["availability"] == "EXPIRED"
    assert expired["run_identity"] == first["run_identity"]
    assert expired["publication"] == first["publication"]


@pytest.mark.parametrize("collection", ["detail", "topology"])
def test_matching_cursor_epoch_uses_expired_page_after_time_retention(
    collection: str,
    monkeypatch,
) -> None:
    published = publish_initial_workflow(
        2,
        case_id=127_210 if collection == "detail" else 127_211,
    )
    operation = (
        list_workflow_node_details if collection == "detail" else list_workflow_topology_nodes
    )
    first = operation(published.execution, authorize=_allow, limit=1)
    cursor = first["next_cursor"]
    assert cursor is not None
    summary = _stored_summary(published.execution)
    finished_at = "2026-07-20T12:00:02Z"
    summary.update(state="SUCCEEDED", progress_percent=100.0)
    summary["node_counts"].update(pending=0, succeeded=2)
    summary["timestamps"].update(updated_at=finished_at, finished_at=finished_at)
    summary["retention"].update(detail_days=0, detail_expires_at=finished_at)
    summary["terminal"] = {"outcome": "SUCCEEDED", "finished_at": finished_at}
    _store_summary(published.execution, summary)
    after_expiry = datetime(2026, 7, 20, 12, 0, 3, tzinfo=UTC)

    class _AfterExpiry(datetime):
        @classmethod
        def now(cls, tz=None):
            return after_expiry if tz is not None else after_expiry.replace(tzinfo=None)

    monkeypatch.setattr(reads, "datetime", _AfterExpiry)
    expired = operation(
        published.execution,
        authorize=_allow,
        cursor=cursor,
        limit=1,
    )

    assert expired["availability"] == "EXPIRED"
    assert expired["run_identity"] == first["run_identity"]
    expected_publication = dict(first["publication"])
    if collection == "topology":
        expected_publication["detail_revision"] = None
    assert expired["publication"] == expected_publication
    assert expired["items"] == []


def test_read_size_caps_fail_closed_for_cursor_summary_empty_and_expired_pages(
    monkeypatch,
) -> None:
    cursor_workflow = publish_initial_workflow(2, case_id=127_212)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES", 1)
    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                cursor_workflow.execution,
                authorize=_allow,
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )
    monkeypatch.setattr(
        reads,
        "WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES",
        WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES,
    )

    empty_execution = _summary_only_execution("NOT_REPORTED", case_id=127_213)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", 1)
    assert (
        _error_code(lambda: list_workflow_topology_nodes(empty_execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )
    assert (
        _error_code(lambda: get_workflow_progress_summary(empty_execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    monkeypatch.setattr(
        reads,
        "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES",
        WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES,
    )
    expired_workflow = publish_initial_workflow(2, case_id=127_214)
    first = list_workflow_node_details(
        expired_workflow.execution,
        authorize=_allow,
        limit=1,
    )
    assert first["next_cursor"] is not None
    RayTaskExecution.objects.filter(pk=expired_workflow.execution.pk).update(
        workflow_progress_summary_json=None
    )
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", 1)
    assert (
        _error_code(
            lambda: list_workflow_node_details(
                expired_workflow.execution,
                authorize=_allow,
                cursor=first["next_cursor"],
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_topology_response_shrinks_to_the_wire_cap(monkeypatch) -> None:
    published = publish_initial_workflow(10, case_id=127_215)
    full = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=10)
    full_size = len(_wire_bytes(full))
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", full_size - 1)

    bounded = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=10)

    assert 0 < bounded["returned_count"] < 10
    assert bounded["next_cursor"] is not None
    assert len(_wire_bytes(bounded)) <= full_size - 1


def test_topology_pages_keep_stable_node_and_edge_order_across_cursors() -> None:
    published = _publish_workflow_with_edges(257, 257, case_id=127_217)
    node_first = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=256,
    )
    assert node_first["next_cursor"] is not None
    node_second = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        cursor=node_first["next_cursor"],
        limit=256,
    )
    assert node_second["items"][0]["node_id"] == workflow_node_id(256)

    edge_first = list_workflow_topology_edges(
        published.execution,
        authorize=_allow,
        limit=256,
    )
    assert edge_first["next_cursor"] is not None
    edge_second = list_workflow_topology_edges(
        published.execution,
        authorize=_allow,
        cursor=edge_first["next_cursor"],
        limit=256,
    )
    first_key = [edge_first["items"][0]["source"], edge_first["items"][0]["target"]]
    second_key = [edge_second["items"][0]["source"], edge_second["items"][0]["target"]]
    assert first_key < second_key


def test_topology_cursor_positions_are_checked_against_authenticated_pages() -> None:
    published = publish_initial_workflow(257, case_id=127_218)
    first = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=100)
    boundary = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=256,
    )
    cursor = first["next_cursor"]
    boundary_cursor = boundary["next_cursor"]
    assert cursor is not None
    assert boundary_cursor is not None

    cases = [
        (_signed_cursor(cursor, position=[99, 0], seen=0), 100),
        (_signed_cursor(cursor, seen=99), 100),
        (_signed_cursor(cursor, position=[0, 999], seen=999), 100),
        (_signed_cursor(cursor, after="wrong-stable-key"), 100),
        (_signed_cursor(boundary_cursor, after="zzzzzzzz"), 256),
    ]
    expected_codes = [
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        WorkflowProgressReadErrorCode.CORRUPT,
    ]
    for (value, limit), expected in zip(cases, expected_codes, strict=True):
        assert (
            _error_code(
                lambda value=value, limit=limit: list_workflow_topology_nodes(
                    published.execution,
                    authorize=_allow,
                    cursor=value,
                    limit=limit,
                )
            )
            is expected
        )

    edge_cursor = _signed_cursor(
        cursor,
        collection="topology_edges",
        order="source_target_asc",
        after=[workflow_node_id(99), workflow_node_id(100)],
    )
    assert (
        _error_code(
            lambda: list_workflow_topology_edges(
                published.execution,
                authorize=_allow,
                cursor=edge_cursor,
                limit=100,
            )
        )
        is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    )


def test_topology_missing_manifest_link_and_page_are_bounded_failures() -> None:
    missing_manifest = publish_initial_workflow(1, case_id=127_219)
    WorkflowProgressTopologyManifest.objects.filter(pk=missing_manifest.manifest_id).delete()
    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                missing_manifest.execution,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )

    missing_link = publish_initial_workflow(1, case_id=127_220)
    WorkflowProgressTopologyManifestPage.objects.filter(
        manifest_id=missing_link.manifest_id,
        collection="NODE",
        page_index=0,
    ).delete()
    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                missing_link.execution,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_topology_rejects_summary_count_and_unexpected_link_mismatches() -> None:
    count_mismatch = publish_initial_workflow(2, case_id=127_221)
    summary = _stored_summary(count_mismatch.execution)
    summary["node_counts"].update(retained_topology=1, retained_detail=1)
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit"],
    }
    _store_summary(count_mismatch.execution, summary)
    WorkflowProgressRunStorage.objects.filter(execution=count_mismatch.execution).update(
        detail_truncation_reasons="detail_count_limit"
    )
    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                count_mismatch.execution,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    extra_link = publish_initial_workflow(1, case_id=127_222)
    foreign = publish_initial_workflow(1, case_id=127_223)
    foreign_page = WorkflowProgressTopologyPage.objects.get(
        run_storage__execution=foreign.execution,
        collection="NODE",
    )
    WorkflowProgressTopologyManifestPage.objects.create(
        manifest_id=extra_link.manifest_id,
        page=foreign_page,
        collection="NODE",
        page_index=1,
    )
    assert (
        _error_code(lambda: list_workflow_topology_nodes(extra_link.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_topology_decoded_record_budget_rejects_an_unreadable_first_item(
    monkeypatch,
) -> None:
    published = publish_initial_workflow(1, case_id=127_224)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES", 0)

    assert (
        _error_code(lambda: list_workflow_topology_nodes(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_topology_decoded_budget_stops_before_the_second_item(monkeypatch) -> None:
    published = publish_initial_workflow(2, case_id=127_225)
    full = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=2)
    first_item_bytes = len(_canonical_bytes(full["items"][0]))
    monkeypatch.setattr(
        reads,
        "WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES",
        first_item_bytes,
    )

    bounded = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=2)

    assert bounded["returned_count"] == 1
    assert bounded["next_cursor"] is not None


def test_persisted_truncation_reason_corruption_is_rejected_by_both_page_families() -> None:
    published = publish_initial_workflow(1, case_id=127_226)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_truncation_reasons="not-a-protocol-reason"
    )

    assert (
        _error_code(lambda: list_workflow_topology_nodes(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )
    assert (
        _error_code(lambda: list_workflow_node_details(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_detail_summary_and_run_epoch_counts_must_agree() -> None:
    published = publish_initial_workflow(1, case_id=127_227)
    summary = _stored_summary(published.execution)
    summary["node_counts"]["retained_detail"] = 0
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit"],
    }
    _store_summary(published.execution, summary)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_truncation_reasons="detail_count_limit"
    )

    assert (
        _error_code(lambda: list_workflow_node_details(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_missing_detail_epoch_is_reported_for_page_and_single_node_reads() -> None:
    published = publish_initial_workflow(1, case_id=127_228)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).delete()

    assert (
        _error_code(lambda: list_workflow_node_details(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.MISSING
    )
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_detail_cursor_seen_and_after_must_match_the_retained_epoch() -> None:
    published = publish_initial_workflow(3, case_id=127_229)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=1)
    cursor = first["next_cursor"]
    assert cursor is not None

    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=_signed_cursor(cursor, seen=4),
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    )
    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=_signed_cursor(cursor, after="f" * 64),
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_detail_metadata_and_payload_races_fail_closed(monkeypatch) -> None:
    malformed = publish_initial_workflow(1, case_id=127_230)
    WorkflowProgressNodeDetail.objects.filter(run_storage__execution=malformed.execution).update(
        node_key="short"
    )
    assert (
        _error_code(lambda: list_workflow_node_details(malformed.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    disappeared = publish_initial_workflow(1, case_id=127_231)
    monkeypatch.setattr(reads, "_detail_payload_rows", lambda _query, _keys: [])
    assert (
        _error_code(lambda: list_workflow_node_details(disappeared.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_detail_list_verifies_payload_digest_and_final_retained_count() -> None:
    corrupted = publish_initial_workflow(1, case_id=127_232)
    WorkflowProgressNodeDetail.objects.filter(run_storage__execution=corrupted.execution).update(
        digest="d" * 64
    )
    assert (
        _error_code(lambda: list_workflow_node_details(corrupted.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    extra = publish_initial_workflow(1, case_id=127_233)
    run_storage = WorkflowProgressRunStorage.objects.get(execution=extra.execution)
    prepared = prepare_workflow_progress_node_detail(
        workflow_detail("unaccounted-final-node"),
        identity=extra.identity,
    )
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key=prepared.node_key,
        node_id=prepared.node_id,
        invocation_id=prepared.invocation_id,
        state=prepared.state,
        event_count=prepared.event_count,
        truncated=prepared.truncated,
        payload=prepared.payload,
        digest=prepared.digest,
        encoded_bytes=prepared.encoded_bytes,
        decoded_bytes=prepared.decoded_bytes,
        last_topology_version=1,
        last_detail_revision=1,
    )
    assert (
        _error_code(lambda: list_workflow_node_details(extra.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_detail_response_too_small_for_one_cursor_page_fails_closed(monkeypatch) -> None:
    published = publish_initial_workflow(2, case_id=127_234)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", 1)

    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_public_detail_redacts_private_execution_pk_from_invocation_identity() -> None:
    published = publish_initial_workflow(1, case_id=127_235)
    detail = workflow_detail(workflow_node_id(0))
    detail["invocation_identity"] = {
        **published.identity.as_dict(),
        "invocation_id": "bounded-public-invocation",
    }
    changed = prepare_workflow_progress_node_detail(detail, identity=published.identity)
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=1,
            running_count=0,
        ),
        manifest_id=published.manifest_id,
        prepared_topology=published.topology,
        detail_records=(changed,),
    )
    assert result.accepted

    page = list_workflow_node_details(published.execution, authorize=_allow)
    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(0),
        authorize=_allow,
    )

    assert page["items"][0]["invocation_identity"]["invocation_id"] == ("bounded-public-invocation")
    assert "task_execution_pk" not in page["items"][0]["invocation_identity"]
    assert "task_execution_pk" not in node["item"]["invocation_identity"]


def test_public_detail_exposes_only_normalized_metric_and_resource_keys() -> None:
    published = publish_initial_workflow(1, case_id=127_250)
    node_input = workflow_node(workflow_node_id(0))
    node_input["runtime_env"] = {"\x1b[31mprofile\x1b[0m": "default"}
    node_input["ray_options"] = {
        "metadata": {"\x9dsafe\x18queue": "ordinary"},
    }
    topology = prepare_workflow_progress_topology(
        published.identity,
        2,
        (node_input,),
        (),
    )
    manifest_id = stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    detail = workflow_detail(workflow_node_id(0), state="RUNNING")
    detail["progress"] = {
        "current": 1,
        "total": 2,
        "percent": 50,
        "message": None,
        "metrics": {"\x1b[32mrows\x1b[0m": 12},
        "updated_at": "2026-07-20T12:00:00Z",
    }
    detail["execution"] = {
        "ray_task_id": None,
        "ray_job_id": None,
        "ray_node_id": None,
        "ray_worker_id": None,
        "assigned_resources": {"\x1b[33mCPU\x1b[0m": 1.0},
    }
    changed = prepare_workflow_progress_node_detail(detail, identity=published.identity)
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=1,
            running_count=1,
        ),
        manifest_id=manifest_id,
        prepared_topology=topology,
        detail_records=(changed,),
    )
    assert result.accepted

    topology_page = list_workflow_topology_nodes(published.execution, authorize=_allow)
    page = list_workflow_node_details(published.execution, authorize=_allow)
    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(0),
        authorize=_allow,
    )

    assert topology_page["items"][0]["runtime_env"] == {"profile": "default"}
    assert topology_page["items"][0]["ray_options"] == {"metadata": {"queue": "ordinary"}}
    assert page["items"][0]["progress"]["metrics"] == {"rows": 12}
    assert page["items"][0]["execution"]["assigned_resources"] == {"CPU": 1.0}
    assert node["item"]["progress"]["metrics"] == {"rows": 12}
    assert node["item"]["execution"]["assigned_resources"] == {"CPU": 1.0}
    assert "\x1b" not in repr(topology_page)
    assert "\x1b" not in repr(page)
    assert "\x1b" not in repr(node)


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
def test_authenticated_legacy_ansi_records_are_normalized_only_for_presentation(
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
) -> None:
    published = _legacy_ansi_workflow(monkeypatch, case_id=127_251)
    node_page = WorkflowProgressTopologyPage.objects.get(
        run_storage__execution=published.execution,
        collection="NODE",
    )
    detail_row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=workflow_node_id(0),
    )
    original_node_payload = bytes(node_page.payload)
    original_node_digest = node_page.digest
    original_detail_payload = bytes(detail_row.payload)
    original_detail_digest = detail_row.digest
    assert b"\\u001b" in original_node_payload
    assert b"\\u001b" in original_detail_payload
    attempt_number = _archive_workflow_attempt(published, next_run=127_252) if archived else None

    topology = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )
    edges = list_workflow_topology_edges(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )
    details = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )
    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(0),
        authorize=_allow,
        attempt_number=attempt_number,
    )

    topology_by_id = {item["node_id"]: item for item in topology["items"]}
    detail_by_id = {item["node_id"]: item for item in details["items"]}
    assert topology_by_id[workflow_node_id(0)]["label"] == "Prepare"
    assert topology_by_id[workflow_node_id(0)]["runtime_env"] == {"profile": "default"}
    assert edges["items"] == [{"source": workflow_node_id(0), "target": workflow_node_id(1)}]
    assert detail_by_id[workflow_node_id(0)]["progress"]["message"] == "Working"
    assert detail_by_id[workflow_node_id(0)]["progress"]["metrics"] == {"rows": 1}
    assert detail_by_id[workflow_node_id(0)]["recent_events"][0]["label"] == "Started"
    assert node["item"] == detail_by_id[workflow_node_id(0)]
    assert "\x1b" not in repr((topology, edges, details, node))

    node_page.refresh_from_db()
    detail_row.refresh_from_db()
    assert bytes(node_page.payload) == original_node_payload
    assert node_page.digest == original_node_digest
    assert bytes(detail_row.payload) == original_detail_payload
    assert detail_row.digest == original_detail_digest


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
def test_authenticated_records_follow_new_redaction_policy_only_in_presentation(
    monkeypatch: pytest.MonkeyPatch,
    settings,
    archived: bool,
) -> None:
    node_id = workflow_node_id(0)
    node = workflow_node(node_id)
    node.update(
        {
            "label": "newly-sensitive-label",
            "runtime_env": {
                "ordinary": "newly-sensitive-runtime-value",
                "newly-sensitive-runtime-key": "omitted",
            },
        }
    )
    detail = workflow_detail(node_id, state="RUNNING")
    detail["progress"] = {
        "current": 1.0,
        "total": 2.0,
        "percent": 50.0,
        "message": "newly-sensitive-message",
        "metrics": {
            "ordinary": "newly-sensitive-metric-value",
            "newly-sensitive-metric-key": 1,
        },
        "updated_at": "2026-07-20T12:00:00Z",
    }
    detail["execution"] = {
        "ray_task_id": None,
        "ray_job_id": None,
        "ray_node_id": None,
        "ray_worker_id": None,
        "assigned_resources": {
            "CPU": 1.0,
            "newly-sensitive-resource": 2.0,
        },
    }
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_259,
        nodes=[node],
        edges=[],
        details=[detail],
    )
    node_page = WorkflowProgressTopologyPage.objects.get(
        run_storage__execution=published.execution,
        collection="NODE",
    )
    detail_row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=node_id,
    )
    original_node_payload = bytes(node_page.payload)
    original_node_digest = node_page.digest
    original_detail_payload = bytes(detail_row.payload)
    original_detail_digest = detail_row.digest
    attempt_number = _archive_workflow_attempt(published, next_run=127_260) if archived else None

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"newly-sensitive"],
    }

    topology = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )
    details = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )

    assert topology["items"][0]["label"] == REDACTED
    assert topology["items"][0]["runtime_env"] == {"ordinary": REDACTED}
    assert details["items"][0]["progress"] == {
        "current": 1.0,
        "message": REDACTED,
        "metrics": {"ordinary": REDACTED},
        "percent": 50.0,
        "total": 2.0,
        "updated_at": "2026-07-20T12:00:00Z",
    }
    assert details["items"][0]["execution"]["assigned_resources"] == {"CPU": 1.0}
    assert details["items"][0]["truncated"] is True

    node_page.refresh_from_db()
    detail_row.refresh_from_db()
    assert bytes(node_page.payload) == original_node_payload
    assert node_page.digest == original_node_digest
    assert bytes(detail_row.payload) == original_detail_payload
    assert detail_row.digest == original_detail_digest


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
@pytest.mark.parametrize("surface", ["node", "edge", "detail"])
def test_legacy_identity_that_normalizes_is_rejected_without_remapping(
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
    surface: str,
) -> None:
    unsafe_id = "\x1b[31mnode-source\x1b[0m"
    safe_id = "node-source"
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_253,
        nodes=[workflow_node(unsafe_id), workflow_node(safe_id)],
        edges=[{"source": unsafe_id, "target": safe_id}],
        details=[workflow_detail(unsafe_id), workflow_detail(safe_id)],
    )
    attempt_number = _archive_workflow_attempt(published, next_run=127_254) if archived else None
    operations = {
        "node": lambda: list_workflow_topology_nodes(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        ),
        "edge": lambda: list_workflow_topology_edges(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        ),
        "detail": lambda: list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        ),
    }

    assert storage.redact_text(unsafe_id) == safe_id
    assert _error_code(operations[surface]) is WorkflowProgressReadErrorCode.CORRUPT


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
@pytest.mark.parametrize("surface", ["topology", "detail"])
def test_new_redaction_policy_cannot_remap_an_authenticated_identity(
    monkeypatch: pytest.MonkeyPatch,
    settings,
    archived: bool,
    surface: str,
) -> None:
    node_id = "newly-sensitive-node-identity"
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_261,
        nodes=[workflow_node(node_id)],
        edges=[],
        details=[workflow_detail(node_id)],
    )
    attempt_number = _archive_workflow_attempt(published, next_run=127_262) if archived else None
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"newly-sensitive"],
    }

    def operation() -> dict[str, Any]:
        if surface == "topology":
            return list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                attempt_number=attempt_number,
            )
        return list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )

    assert _error_code(operation) is WorkflowProgressReadErrorCode.CORRUPT


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
@pytest.mark.parametrize("surface", ["topology", "detail"])
def test_legacy_display_key_collisions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
    surface: str,
) -> None:
    node_id = workflow_node_id(0)
    node = workflow_node(node_id)
    node["runtime_env"] = {
        "profile": "first",
        "\x1b[31mprofile\x1b[0m": "second",
    }
    detail = workflow_detail(node_id, state="RUNNING")
    detail["progress"] = {
        "current": 1,
        "total": 2,
        "percent": 50,
        "message": None,
        "metrics": {"rows": 1, "\x1b[32mrows\x1b[0m": 2},
        "updated_at": "2026-07-20T12:00:00Z",
    }
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_255,
        nodes=[node],
        edges=[],
        details=[detail],
    )
    attempt_number = _archive_workflow_attempt(published, next_run=127_256) if archived else None

    def operation() -> dict[str, Any]:
        if surface == "topology":
            return list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                attempt_number=attempt_number,
            )
        return list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )

    assert _error_code(operation) is WorkflowProgressReadErrorCode.CORRUPT


@pytest.mark.parametrize(
    "tampering",
    ["numeric_coercion", "oversized_message", "event_order", "event_retention"],
)
def test_recomputed_detail_digest_cannot_authenticate_a_noncanonical_record(
    monkeypatch: pytest.MonkeyPatch,
    tampering: str,
) -> None:
    node_id = workflow_node_id(0)
    detail = workflow_detail(node_id, state="RUNNING")
    detail["progress"] = {
        "current": 1.0,
        "total": 2.0,
        "percent": 50.0,
        "message": "working",
        "metrics": {},
        "updated_at": "2026-07-20T12:00:00Z",
    }
    detail["recent_events"] = [
        {
            "event": "STATE_CHANGE",
            "state": "PENDING",
            "label": "queued",
            "timestamp": "2026-07-20T11:59:59Z",
        },
        {
            "event": "STATE_CHANGE",
            "state": "RUNNING",
            "label": "started",
            "timestamp": "2026-07-20T12:00:00Z",
        },
    ]
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_263,
        nodes=[workflow_node(node_id)],
        edges=[],
        details=[detail],
    )
    row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=node_id,
    )
    value = json.loads(bytes(row.payload))
    if tampering == "numeric_coercion":
        value["progress"]["current"] = 1
    elif tampering == "oversized_message":
        value["progress"]["message"] = "x" * (storage.WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES + 1)
    elif tampering == "event_order":
        value["recent_events"].reverse()
    else:
        value["recent_events"] = [
            {
                "event": "STATE_CHANGE",
                "state": "RUNNING",
                "label": f"event-{index:02d}",
                "timestamp": f"2026-07-20T12:00:{index:02d}Z",
            }
            for index in range(storage.WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS + 1)
        ]
        value["truncated"] = True
    payload = _canonical_bytes(value)
    updates: dict[str, Any] = {
        "payload": payload,
        "digest": storage._digest(storage._DETAIL_DOMAIN, payload),
        "encoded_bytes": len(payload),
        "decoded_bytes": len(payload),
    }
    if tampering == "event_retention":
        updates.update(
            event_count=storage.WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS,
            truncated=True,
        )
    WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(**updates)

    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                node_id,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_recomputed_topology_digest_cannot_authenticate_an_oversized_label() -> None:
    published = publish_initial_workflow(1, case_id=127_264)
    page = WorkflowProgressTopologyPage.objects.get(
        run_storage__execution=published.execution,
        collection="NODE",
    )
    value = json.loads(bytes(page.payload))
    value["records"][0]["label"] = "x" * (storage.WORKFLOW_PROGRESS_LABEL_MAX_BYTES + 1)
    payload = _canonical_bytes(value)
    digest = storage._digest(storage._PAGE_DOMAIN, payload)
    descriptor = {
        "collection": "NODE",
        "decoded_bytes": len(payload),
        "digest": digest,
        "encoding": "identity",
        "encoded_bytes": len(payload),
        "item_count": 1,
        "page_index": 0,
    }
    row = {
        "collection": "NODE",
        "page_index": 0,
        "page__run_storage_id": page.run_storage_id,
        "page__collection": "NODE",
        "page__encoding": "identity",
        "page__digest": digest,
        "page__item_count": 1,
        "page__encoded_bytes": len(payload),
        "page__decoded_bytes": len(payload),
        "_payload_octets": len(payload),
        "_bounded_payload": payload,
    }

    with pytest.raises(storage.WorkflowProgressStorageIntegrityError):
        storage.verify_workflow_progress_topology_page_record(
            row,
            descriptor=descriptor,
            expected_run_storage_id=page.run_storage_id,
        )


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
@pytest.mark.parametrize("surface", ["topology", "detail"])
def test_legacy_payload_digest_authenticates_original_bytes_before_presentation(
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
    surface: str,
) -> None:
    published = _legacy_ansi_workflow(monkeypatch, case_id=127_257)
    attempt_number = _archive_workflow_attempt(published, next_run=127_258) if archived else None
    if surface == "topology":
        row = WorkflowProgressTopologyPage.objects.get(
            run_storage__execution=published.execution,
            collection="NODE",
        )
        original = bytes(row.payload)
        tampered = original.replace(b"\\u001b[31m", b"\\u001b[32m", 1)
    else:
        row = WorkflowProgressNodeDetail.objects.get(
            run_storage__execution=published.execution,
            node_id=workflow_node_id(0),
        )
        original = bytes(row.payload)
        tampered = original.replace(b"\\u001b[33m", b"\\u001b[32m", 1)

    def operation() -> dict[str, Any]:
        if surface == "topology":
            return list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                attempt_number=attempt_number,
            )
        return list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )

    assert tampered != original
    assert len(tampered) == len(original)
    type(row).objects.filter(pk=row.pk).update(payload=tampered)
    assert storage.redact_text("\x1b[31mPrepare\x1b[0m") == storage.redact_text(
        "\x1b[32mPrepare\x1b[0m"
    )

    assert _error_code(operation) is WorkflowProgressReadErrorCode.CORRUPT


def test_invalid_state_and_utf8_node_identifiers_are_bounded_arguments() -> None:
    published = publish_initial_workflow(1, case_id=127_236)
    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                state="not-a-workflow-state",
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )
    for node_id in ("\ud800", "\U0001f600" * 256):
        assert (
            _error_code(
                lambda node_id=node_id: get_workflow_node_detail(
                    published.execution,
                    node_id,
                    authorize=_allow,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
        )


def test_single_node_read_rejects_duplicate_mismatched_and_oversized_results(
    monkeypatch,
) -> None:
    published = publish_initial_workflow(1, case_id=127_237)
    original_rows = reads._detail_payload_rows
    original_verify = reads.verify_workflow_progress_node_detail_record

    def duplicate_rows(query, keys):
        rows = original_rows(query, keys)
        return [*rows, *rows]

    monkeypatch.setattr(reads, "_detail_payload_rows", duplicate_rows)
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    monkeypatch.setattr(reads, "_detail_payload_rows", original_rows)

    def mismatched_node(*args, **kwargs):
        value = original_verify(*args, **kwargs)
        return {**value, "node_id": "different-node"}

    monkeypatch.setattr(reads, "verify_workflow_progress_node_detail_record", mismatched_node)
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    monkeypatch.setattr(
        reads,
        "verify_workflow_progress_node_detail_record",
        original_verify,
    )
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", 1)
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_single_node_read_validates_run_epoch_counters() -> None:
    published = publish_initial_workflow(1, case_id=127_238)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_node_count=2,
        detail_pending_count=2,
    )

    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_single_node_rejects_a_row_when_authenticated_epoch_count_is_zero() -> None:
    published = publish_initial_workflow(1, case_id=127_240)
    summary = _stored_summary(published.execution)
    summary["node_counts"]["retained_detail"] = 0
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit"],
    }
    _store_summary(published.execution, summary)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_node_count=0,
        detail_pending_count=0,
        detail_event_count=0,
        detail_encoded_bytes=0,
        detail_decoded_bytes=0,
        detail_truncation_reasons="detail_count_limit",
    )

    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_lifecycle_success_keeps_preterminal_retained_detail_readable() -> None:
    published = publish_initial_workflow(1, case_id=127_241)

    assert succeed_task(
        published.execution,
        result_data='{"ok":true}',
        result_reference=None,
    )

    for attempt_number in (None, 1):
        summary = get_workflow_progress_summary(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )
        topology = list_workflow_topology_nodes(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )
        details = list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )
        node = get_workflow_node_detail(
            published.execution,
            workflow_node_id(0),
            authorize=_allow,
            attempt_number=attempt_number,
        )

        for response in (summary, topology, details, node):
            assert response["availability"] == "TRUNCATED"
            assert response["complete"] is False
        assert summary["summary"]["state"] == "SUCCEEDED"
        assert summary["summary"]["detail"]["truncation_reasons"] == ["terminal_state_unreported"]
        assert topology["returned_count"] == 1
        assert details["items"][0]["state"] == "PENDING"
        assert node["found"] is True
        assert node["item"]["state"] == "PENDING"
