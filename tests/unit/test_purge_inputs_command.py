"""Tests for retention-safe task-input payload cleanup."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta
from io import StringIO
from types import ModuleType

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_ray.management.commands.django_ray_purge_inputs import OUTPUT_MAX_BYTES, Command
from django_ray.management.purge_cursor import PurgeCursor, decode_purge_cursor, encode_purge_cursor
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
    TaskState,
)


def _reference(suffix: str) -> str:
    digest = (suffix * 64)[:64]
    return f"resultfs://sha256/{digest}?rel={digest[:2]}/{digest[2:4]}/{digest}.json&bytes=128"


def _fingerprint(reference: str) -> str:
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16]


def _payload(
    reference: str,
    *,
    age_days: int = 60,
    payload_kind: str = InputPayloadKind.TASK_INPUT,
) -> TaskInputPayload:
    used_at = timezone.now() - timedelta(days=age_days)
    return TaskInputPayload.objects.create(
        reference=reference,
        backend="filesystem",
        digest="a" * 64,
        size_bytes=128,
        envelope_version=1,
        payload_kind=payload_kind,
        created_at=used_at,
        last_used_at=used_at,
    )


def _execution(
    reference: str,
    *,
    state: str = TaskState.SUCCEEDED,
    age_days: int = 60,
    reference_field: str = "input_reference",
) -> RayTaskExecution:
    timestamp = timezone.now() - timedelta(days=age_days)
    terminal_states = {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.LOST,
        TaskState.EXPIRED,
    }
    reference_fields = {reference_field: reference}
    return RayTaskExecution.objects.create(
        task_id=(
            f"purge-{state.lower()}-{age_days}-{hashlib.sha256(reference.encode()).hexdigest()[:8]}"
        ),
        callable_path="testproject.tasks.add_numbers",
        state=state,
        created_at=timestamp,
        finished_at=timestamp if state in terminal_states else None,
        **reference_fields,
    )


def _install_input_storage(monkeypatch: pytest.MonkeyPatch, delete) -> None:
    module = ModuleType("django_ray.input_storage")
    module.delete_input_reference = delete  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "django_ray.input_storage", module)


@pytest.mark.django_db
def test_purge_inputs_is_dry_run_by_default() -> None:
    reference = _reference("a")
    payload = _payload(reference)
    execution = _execution(reference)
    stdout = StringIO()

    call_command("django_ray_purge_inputs", retention_days=30, stdout=stdout)

    payload.refresh_from_db()
    execution.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.purged_at is None
    assert execution.input_reference == reference
    assert f"reference_sha256={_fingerprint(reference)}" in stdout.getvalue()
    assert reference not in stdout.getvalue()
    assert "1 eligible, 0 purged, 0 failed" in stdout.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload_kind", "reference_field"),
    [
        (InputPayloadKind.TASK_INPUT, "input_reference"),
        (InputPayloadKind.RAY_JOB_REQUEST, "ray_job_request_reference"),
    ],
)
def test_delete_tombstones_registry_and_retains_execution_reference(
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
    reference_field: str,
) -> None:
    reference = _reference("b")
    payload = _payload(reference, payload_kind=payload_kind)
    first = _execution(
        reference,
        state=TaskState.FAILED,
        reference_field=reference_field,
    )
    second = _execution(
        reference,
        state=TaskState.CANCELLED,
        reference_field=reference_field,
    )
    third = _execution(
        reference,
        state=TaskState.EXPIRED,
        reference_field=reference_field,
    )
    deleted: list[str] = []
    _install_input_storage(monkeypatch, deleted.append)
    stdout = StringIO()

    call_command(
        "django_ray_purge_inputs",
        retention_days=30,
        delete=True,
        stdout=stdout,
    )

    payload.refresh_from_db()
    first.refresh_from_db()
    second.refresh_from_db()
    third.refresh_from_db()
    assert deleted == [reference]
    assert payload.state == InputPayloadState.PURGED
    assert payload.purged_at is not None
    assert payload.cleanup_error == ""
    assert getattr(first, reference_field) == reference
    assert getattr(second, reference_field) == reference
    assert getattr(third, reference_field) == reference
    assert f"reference_sha256={_fingerprint(reference)}" in stdout.getvalue()
    assert reference not in stdout.getvalue()
    assert "1 eligible, 1 purged, 0 failed" in stdout.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload_kind", "reference_field"),
    [
        (InputPayloadKind.TASK_INPUT, "input_reference"),
        (InputPayloadKind.RAY_JOB_REQUEST, "ray_job_request_reference"),
    ],
)
def test_delete_skips_reference_with_active_recent_or_undated_terminal_execution(
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
    reference_field: str,
) -> None:
    references = [_reference("c"), _reference("d"), _reference("e")]
    for reference in references:
        _payload(reference, payload_kind=payload_kind)

    _execution(
        references[0],
        state=TaskState.RUNNING,
        reference_field=reference_field,
    )
    _execution(references[1], age_days=1, reference_field=reference_field)
    undated = _execution(
        references[2],
        state=TaskState.LOST,
        reference_field=reference_field,
    )
    undated.finished_at = None
    undated.save(update_fields=["finished_at"])
    deleted: list[str] = []
    _install_input_storage(monkeypatch, deleted.append)
    stdout = StringIO()

    call_command(
        "django_ray_purge_inputs",
        retention_days=30,
        delete=True,
        stdout=stdout,
    )

    assert deleted == []
    assert set(TaskInputPayload.objects.values_list("state", flat=True)) == {
        InputPayloadState.ACTIVE
    }
    assert "0 eligible, 0 purged, 0 failed" in stdout.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload_kind",
    [InputPayloadKind.TASK_INPUT, InputPayloadKind.RAY_JOB_REQUEST],
)
def test_orphaned_old_registry_entry_is_eligible(
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
) -> None:
    reference = _reference("f")
    payload = _payload(reference, payload_kind=payload_kind)
    deleted: list[str] = []
    _install_input_storage(monkeypatch, deleted.append)

    call_command("django_ray_purge_inputs", retention_days=30, delete=True)

    payload.refresh_from_db()
    assert deleted == [reference]
    assert payload.state == InputPayloadState.PURGED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload_kind", "wrong_reference_field"),
    [
        (InputPayloadKind.TASK_INPUT, "ray_job_request_reference"),
        (InputPayloadKind.RAY_JOB_REQUEST, "input_reference"),
    ],
)
def test_payload_kind_mismatch_retains_reference(
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
    wrong_reference_field: str,
) -> None:
    reference = _reference("6")
    payload = _payload(reference, payload_kind=payload_kind)
    _execution(reference, reference_field=wrong_reference_field)
    deleted: list[str] = []
    _install_input_storage(monkeypatch, deleted.append)

    call_command("django_ray_purge_inputs", retention_days=30, delete=True)

    payload.refresh_from_db()
    assert deleted == []
    assert payload.state == InputPayloadState.ACTIVE


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload_kind",
    [InputPayloadKind.TASK_INPUT, InputPayloadKind.RAY_JOB_REQUEST],
)
def test_ambiguous_execution_reference_retains_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
) -> None:
    reference = _reference("7")
    payload = _payload(reference, payload_kind=payload_kind)
    execution = _execution(reference)
    execution.ray_job_request_reference = reference
    execution.save(update_fields=["ray_job_request_reference"])
    deleted: list[str] = []
    _install_input_storage(monkeypatch, deleted.append)

    call_command("django_ray_purge_inputs", retention_days=30, delete=True)

    payload.refresh_from_db()
    assert deleted == []
    assert payload.state == InputPayloadState.ACTIVE


def test_unknown_payload_kind_is_not_eligible_even_without_references() -> None:
    assert (
        Command._all_references_are_old_and_terminal(
            [],
            reference=_reference("8"),
            payload_kind="unknown",
            cutoff=timezone.now(),
        )
        is False
    )


@pytest.mark.django_db
def test_delete_failure_is_recorded_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = _reference("1")
    payload = _payload(reference)
    _execution(reference)

    def fail_delete(_reference: str) -> None:
        raise OSError("storage unavailable")

    _install_input_storage(monkeypatch, fail_delete)
    stderr = StringIO()
    stdout = StringIO()

    with pytest.raises(CommandError, match="Failed to purge 1 input payload"):
        call_command(
            "django_ray_purge_inputs",
            retention_days=30,
            delete=True,
            stderr=stderr,
            stdout=stdout,
        )

    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.purged_at is None
    assert payload.cleanup_error == "OSError"
    assert f"reference_sha256={_fingerprint(reference)}" in stdout.getvalue()
    assert reference not in stderr.getvalue()
    assert "storage unavailable" not in stderr.getvalue()


@pytest.mark.django_db
def test_recent_and_already_purged_registry_entries_are_not_candidates() -> None:
    recent = _payload(_reference("2"), age_days=1)
    purged = _payload(_reference("3"))
    purged.state = InputPayloadState.PURGED
    purged.purged_at = timezone.now()
    purged.save(update_fields=["state", "purged_at"])
    stdout = StringIO()

    call_command("django_ray_purge_inputs", retention_days=30, stdout=stdout)

    recent.refresh_from_db()
    purged.refresh_from_db()
    assert recent.state == InputPayloadState.ACTIVE
    assert purged.state == InputPayloadState.PURGED
    assert "0 eligible, 0 purged, 0 failed" in stdout.getvalue()


def test_negative_retention_is_rejected() -> None:
    with pytest.raises(CommandError, match="zero or greater"):
        call_command("django_ray_purge_inputs", retention_days=-1)


def test_cleanup_error_class_name_is_bounded_and_safe() -> None:
    unsafe_error = type("Credential\nLeak", (Exception,), {})()

    assert Command._format_cleanup_error(unsafe_error) == "Exception"
    assert Command._format_cleanup_error(OSError("private path")) == "OSError"


@override_settings(DJANGO_RAY={"REDACT_PATTERNS": [r"TenantCanaryError"]})
def test_cleanup_error_class_name_uses_configured_redaction_without_message() -> None:
    calls = 0

    class TenantCanaryError(RuntimeError):
        def __str__(self) -> str:
            nonlocal calls
            calls += 1
            return "provider password=do-not-expose"

    assert Command._format_cleanup_error(TenantCanaryError()) == "[REDACTED]"
    assert calls == 0


@pytest.mark.django_db
def test_candidate_disappearing_before_lock_is_skipped() -> None:
    outcome = Command()._process_reference(
        _reference("5"),
        cutoff=timezone.now(),
        delete=True,
    )

    assert outcome == "skipped"


@pytest.mark.django_db
def test_input_payload_string_identifies_backend_digest_and_state() -> None:
    payload = _payload(_reference("4"))

    assert str(payload) == f"filesystem Task input {'a' * 12} (ACTIVE)"


def _report(**options):
    stdout = StringIO()
    code = 0
    try:
        call_command("django_ray_purge_inputs", format="json", stdout=stdout, **options)
    except CommandError as error:
        code = error.returncode
    return json.loads(stdout.getvalue()), code, stdout.getvalue()


@pytest.mark.django_db
def test_candidate_limit_and_cursor_survive_deleted_boundary_and_fixed_cutoff(monkeypatch):
    references = [_reference(str(index)) for index in range(4)]
    for reference in references:
        _payload(reference)
    deleted = []
    _install_input_storage(monkeypatch, deleted.append)
    with CaptureQueriesContext(connection) as queries:
        first, code, output = _report(max_candidates=2, delete=True)
    assert code == 2
    assert first["has_more"] is True
    assert first["counts"]["purged"] == 2
    candidate_query = next(q["sql"] for q in queries if "ORDER BY" in q["sql"])
    assert "LIMIT 3" in candidate_query
    assert all(reference not in output for reference in references)
    token = first["next_cursor"]
    decoded = decode_purge_cursor(token)
    assert decoded.after == references[1]
    TaskInputPayload.objects.filter(pk=references[1]).delete()
    # A later current timestamp cannot advance the retained cutoff.
    TaskInputPayload.objects.filter(pk=references[2]).update(last_used_at=timezone.now())
    _payload(_reference("z"), age_days=1)
    second, code, _ = _report(max_candidates=2, delete=True, cursor=token)
    assert code == 0
    assert second["cutoff"] == first["cutoff"]
    assert second["has_more"] is False
    assert second["next_cursor"] is None
    assert deleted == [references[0], references[1], references[3]]


@pytest.mark.django_db
@pytest.mark.parametrize("delete", [False, True])
def test_repeated_continuation_visits_every_candidate_with_bounded_pages(monkeypatch, delete):
    references = [_reference(f"{index:02x}") for index in range(17)]
    for reference in references:
        _payload(reference)
    deleted = []
    _install_input_storage(monkeypatch, deleted.append)
    seen = []
    cursor = None
    cutoff = None
    after = ""
    page_counts = []
    for _ in range(7):
        options = {"delete": delete, "max_candidates": 3, "max_details": 3}
        if cursor is not None:
            options["cursor"] = cursor
        with CaptureQueriesContext(connection) as queries:
            report, code, output = _report(**options)
        candidate_query = next(q["sql"] for q in queries if "ORDER BY" in q["sql"])
        assert "LIMIT 4" in candidate_query
        assert report["counts"]["processed"] <= 3
        assert len(output.encode()) <= OUTPUT_MAX_BYTES
        cutoff = report["cutoff"] if cutoff is None else cutoff
        assert report["cutoff"] == cutoff
        page_counts.append(report["counts"]["processed"])
        seen.extend(detail["reference_sha256"] for detail in report["details"])
        cursor = report["next_cursor"]
        if not report["has_more"]:
            assert code == 0
            assert cursor is None
            break
        assert code == 2
        decoded = decode_purge_cursor(cursor)
        assert decoded.after > after
        after = decoded.after
    assert page_counts == [3, 3, 3, 3, 3, 2]
    assert seen == [_fingerprint(reference) for reference in references]
    assert deleted == (references if delete else [])
    assert TaskInputPayload.objects.filter(state=InputPayloadState.ACTIVE).count() == (
        0 if delete else 17
    )


@pytest.mark.django_db
@pytest.mark.parametrize("count, expected_code", [(2, 0), (3, 2)])
def test_attachment_boundary_retains_incomplete_history(monkeypatch, count, expected_code):
    reference = _reference("b")
    payload = _payload(reference)
    for index in range(count):
        _execution(reference, age_days=60 + index)
    deleted = []
    _install_input_storage(monkeypatch, deleted.append)
    with CaptureQueriesContext(connection) as queries:
        report, code, _ = _report(delete=True, max_attachments=2)
    assert code == expected_code
    assert any("LIMIT 3" in q["sql"] and "django_ray_raytaskexecution" in q["sql"] for q in queries)
    payload.refresh_from_db()
    if count == 3:
        assert deleted == []
        assert payload.state == InputPayloadState.ACTIVE
        assert report["counts"]["incomplete"] == 1
        assert report["counts"]["retained"] == 1
        assert report["has_more"] is False
        assert report["details"][0]["outcome"] == "incomplete"
    else:
        assert deleted == [reference]
        assert report["counts"]["purged"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["disappear", "refresh", "new-active-reference"])
def test_candidate_revalidation_preserves_concurrent_changes(monkeypatch, change):
    reference = _reference("c")
    payload = _payload(reference)
    original = Command._process_reference
    deleted = []
    _install_input_storage(monkeypatch, deleted.append)

    def changed(self, reference, **kwargs):
        if change == "disappear":
            payload.delete()
        elif change == "refresh":
            TaskInputPayload.objects.filter(pk=payload.pk).update(last_used_at=timezone.now())
        else:
            _execution(reference, state=TaskState.RUNNING)
        return original(self, reference, **kwargs)

    monkeypatch.setattr(Command, "_process_reference", changed)
    report, code, _ = _report(delete=True)
    assert code == 0
    assert deleted == []
    assert report["counts"]["retained"] == 1
    assert report["details"][0]["outcome"] == "skipped"


@pytest.mark.django_db
def test_new_earlier_key_requires_a_new_pass(monkeypatch):
    _payload(_reference("b"))
    _payload(_reference("c"))
    first, code, _ = _report(max_candidates=1)
    assert code == 2
    earlier = _payload(_reference("a"))
    second, code, _ = _report(cursor=first["next_cursor"])
    assert code == 0
    assert second["counts"]["processed"] == 1
    assert second["has_more"] is False
    earlier.refresh_from_db()
    assert earlier.state == InputPayloadState.ACTIVE
    fresh, _, _ = _report()
    assert fresh["counts"]["processed"] == 3


@pytest.mark.django_db
def test_interruption_can_restart_without_redeleting_tombstone(monkeypatch):
    for index in range(3):
        _payload(_reference(str(index)))
    deleted = []

    def interrupt(reference):
        if len(deleted) == 1:
            raise KeyboardInterrupt
        deleted.append(reference)

    _install_input_storage(monkeypatch, interrupt)
    with pytest.raises(KeyboardInterrupt):
        _report(delete=True)
    assert TaskInputPayload.objects.filter(state=InputPayloadState.PURGED).count() == 1
    _install_input_storage(monkeypatch, deleted.append)
    report, code, _ = _report(delete=True)
    assert code == 0
    assert report["counts"]["purged"] == 2
    assert len(set(deleted)) == 3


@pytest.mark.django_db
@pytest.mark.parametrize("output_format", ["text", "json"])
def test_output_bound_includes_unicode_cursor_and_maximum_details(output_format):
    # UTF-8 four-byte characters exercise the 500-character PK's maximum size.
    for index in range(102):
        _payload(f"{index:03}" + "\U0001f600" * 497)
    stdout = StringIO()
    with pytest.raises(CommandError) as error:
        call_command(
            "django_ray_purge_inputs",
            format=output_format,
            max_details=100,
            max_candidates=100,
            stdout=stdout,
        )
    output = stdout.getvalue()
    assert error.value.returncode == 2
    assert (
        len(output.encode("utf-8")) + len(str(error.value).encode("utf-8")) + 20 <= OUTPUT_MAX_BYTES
    )
    assert "\U0001f600" not in output
    if output_format == "json":
        report = json.loads(output)
        assert report["counts"]["processed"] == 100
        assert len(report["details"]) <= 100
        assert decode_purge_cursor(report["next_cursor"]).after.startswith("099")


@pytest.mark.parametrize(
    "options",
    [
        {"max_candidates": 0},
        {"max_candidates": 1001},
        {"max_attachments": 0},
        {"max_attachments": 1001},
        {"max_details": -1},
        {"max_details": 101},
        {"retention_days": 365001},
    ],
)
def test_invalid_budgets_reject_before_database_access(options):
    with pytest.raises(CommandError):
        call_command("django_ray_purge_inputs", **options)


@pytest.mark.django_db
def test_zero_detail_budget_keeps_counts_and_failure_exit(monkeypatch):
    _payload(_reference("d"))
    _payload(_reference("e"))

    def fail(_reference):
        raise OSError("private storage location")

    _install_input_storage(monkeypatch, fail)
    report, code, output = _report(delete=True, max_details=0, max_candidates=1)
    assert code == 1  # Failure takes precedence over continuation.
    assert report["details"] == []
    assert report["details_omitted"] == 1
    assert report["counts"]["failed"] == 1
    assert report["next_cursor"]
    assert "private storage" not in output


@pytest.mark.parametrize("options", [{"delete": True}, {"retention_days": 29}])
def test_cursor_refuses_policy_conflicts_without_database_access(options):
    cursor = encode_purge_cursor(PurgeCursor("private-reference", timezone.now(), 30, False))
    with pytest.raises(CommandError, match="conflicts"):
        call_command("django_ray_purge_inputs", cursor=cursor, **options)


@pytest.mark.django_db
def test_cursor_inherits_explicit_initial_retention():
    _payload(_reference("a"))
    _payload(_reference("b"))
    first, _, _ = _report(retention_days=45, max_candidates=1)
    second, code, _ = _report(cursor=first["next_cursor"])
    assert code == 0
    assert second["retention_days"] == 45
    assert second["cutoff"] == first["cutoff"]


@pytest.mark.django_db
def test_registry_lock_omits_unbounded_existing_cleanup_diagnostics():
    payload = _payload(_reference("f"))
    TaskInputPayload.objects.filter(pk=payload.pk).update(cleanup_error="old diagnostic" * 10000)
    with CaptureQueriesContext(connection) as queries:
        assert (
            Command()._process_reference(payload.pk, cutoff=timezone.now(), delete=False)
            == "eligible"
        )
    locked_select = next(
        query["sql"]
        for query in queries
        if query["sql"].startswith("SELECT") and "django_ray_taskinputpayload" in query["sql"]
    )
    assert "cleanup_error" not in locked_select
    assert "last_used_at" in locked_select
    payload.refresh_from_db()
    assert len(payload.cleanup_error) == 140000


@pytest.mark.django_db
@pytest.mark.parametrize(
    "reference",
    [pytest.param("x" * 10000, id="oversized"), pytest.param("x\x00suffix", id="nul-suffix")],
)
@pytest.mark.parametrize("prefix_collision", [False, True])
def test_sqlite_malformed_registry_key_is_bounded_before_materialization(
    monkeypatch, reference, prefix_collision
):
    _payload(reference)
    _payload("z")  # A truncated first key must never become a repeating cursor.
    if prefix_collision:
        _payload("x")  # Invalid lookahead must prevent mutation of this valid row.
    monkeypatch.setattr(
        Command, "_process_reference", lambda *args, **kwargs: pytest.fail("invalid batch mutated")
    )
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(CommandError, match="invalid primary key"):
            call_command("django_ray_purge_inputs", delete=True, max_candidates=1)
    select = next(query["sql"] for query in queries if query["sql"].startswith("SELECT"))
    assert "SUBSTR" in select.upper()
    assert "501" in select


@pytest.mark.django_db
@pytest.mark.parametrize("malformed_state", [False, True])
@pytest.mark.parametrize(
    "suffix",
    [pytest.param("x" * 10000, id="oversized"), pytest.param("\x00" + "x" * 100, id="nul-suffix")],
)
@pytest.mark.parametrize(
    ("payload_kind", "reference_field", "opposite_field"),
    [
        (InputPayloadKind.TASK_INPUT, "input_reference", "ray_job_request_reference"),
        (InputPayloadKind.RAY_JOB_REQUEST, "ray_job_request_reference", "input_reference"),
    ],
)
def test_sqlite_malformed_attachment_fields_are_bounded_and_retained(
    monkeypatch, malformed_state, suffix, payload_kind, reference_field, opposite_field
):
    reference = _reference("f")
    payload = _payload(reference, payload_kind=payload_kind)
    execution = _execution(reference, reference_field=reference_field)
    field = "state" if malformed_state else opposite_field
    original = (TaskState.SUCCEEDED if malformed_state else reference) + suffix
    RayTaskExecution.objects.filter(pk=execution.pk).update(**{field: original})
    deleted = []
    _install_input_storage(monkeypatch, deleted.append)
    check = Command._all_references_are_old_and_terminal
    observed = []

    def inspect(executions, **kwargs):
        assert len(executions) == 1
        row = executions[0]
        assert not isinstance(row, RayTaskExecution)
        with CaptureQueriesContext(connection) as queries:
            assert row.state_is_terminal is (not malformed_state)
            assert all(
                value is None or len(value) <= 501
                for value in (row.input_reference, row.ray_job_request_reference)
            )
            if not malformed_state:
                assert getattr(row, field) == original.split("\x00", 1)[0][:501]
                assert row.references_complete is False
            eligible = check(executions, **kwargs)
        assert len(queries) == 0  # Access cannot reload a deferred raw column.
        observed.append(eligible)
        return eligible

    monkeypatch.setattr(Command, "_all_references_are_old_and_terminal", staticmethod(inspect))
    report, code, _ = _report(delete=True)
    assert code == 0
    assert observed == [False]
    assert report["counts"]["retained"] == 1
    assert report["counts"]["eligible"] == 0
    assert deleted == []
    payload.refresh_from_db()
    execution.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert getattr(execution, field) == original


@pytest.mark.django_db
def test_filesystem_unregistered_inventory_and_coordinated_reap(tmp_path, settings):
    import os
    from datetime import UTC, datetime, timedelta

    from django_ray.execution_codec import (
        ExecutionIdentity,
        ExecutionRequest,
        encode_execution_request,
    )
    from django_ray.input_storage import prepare_task_input, register_task_input
    from django_ray.payload_registry import discover_and_reap_unregistered_payloads
    from django_ray.ray_job_request_storage import prepare_ray_job_request
    from django_ray.result_storage import FilesystemResultStorage

    root = tmp_path / "inputs"
    root.mkdir()
    config = {
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(root),
    }
    settings.DJANGO_RAY = {**getattr(settings, "DJANGO_RAY", {}), **config}
    storage = FilesystemResultStorage(root)
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    old_mtime = (now - timedelta(hours=3)).timestamp()
    recent_mtime = (now - timedelta(minutes=5)).timestamp()

    # 1. Old unregistered TASK_INPUT (also used to test post-reap writer reactivation)
    prepared_old_input = prepare_task_input(["orphan-old-input"], {"k": 1}, config)
    assert prepared_old_input.input_reference is not None
    assert prepared_old_input.digest is not None
    old_input_path = (
        root
        / prepared_old_input.digest[:2]
        / prepared_old_input.digest[2:4]
        / f"{prepared_old_input.digest}.json"
    )
    os.utime(old_input_path, (old_mtime, old_mtime))

    # 2. Recent unregistered TASK_INPUT (retained by grace period)
    prepared_recent_input = prepare_task_input(["orphan-recent-input"], {"k": 2}, config)
    assert prepared_recent_input.digest is not None
    recent_input_path = (
        root
        / prepared_recent_input.digest[:2]
        / prepared_recent_input.digest[2:4]
        / f"{prepared_recent_input.digest}.json"
    )
    os.utime(recent_input_path, (recent_mtime, recent_mtime))

    # 3. Registered ACTIVE TASK_INPUT
    prepared_active_input = prepare_task_input(["registered-active-input"], {"k": 3}, config)
    register_task_input(prepared_active_input, config)

    # 4. Old unregistered RAY_JOB_REQUEST
    request_obj = ExecutionRequest(
        identity=ExecutionIdentity(1, "orphan-request-task", 1, 1),
        execution_protocol_version=1,
        callable_path="testproject.tasks.add_numbers",
        transport_version=1,
        serialized_args="[1,2]",
        serialized_kwargs="{}",
        input_reference=None,
        runtime_env_profile=None,
        runtime_env_hash="a" * 64,
        runtime_env_plan_identity={},
        compiled_graph_submission_transport="ray-job",
    )
    prepared_old_request = prepare_ray_job_request(encode_execution_request(request_obj), config)
    old_request_path = (
        root
        / prepared_old_request.digest[:2]
        / prepared_old_request.digest[2:4]
        / f"{prepared_old_request.digest}.json"
    )
    os.utime(old_request_path, (old_mtime, old_mtime))

    # 5. Ignored temp file and lock dir, plus malformed key and ambiguous non-envelope JSON
    (root / ".django-ray-result-0123456789abcdef0123456789abcdef.tmp").write_text("tmp")
    (root / ".lock.install-lock").mkdir()
    (root / "unexpected.txt").write_text("malformed")
    ambiguous_raw = '{"not":"a-valid-envelope"}'
    ambiguous_ref = storage.store_payload(serialized_payload=ambiguous_raw)
    ambiguous_digest = ambiguous_ref.split("/")[3].split("?")[0]
    ambiguous_path = (
        root / ambiguous_digest[:2] / ambiguous_digest[2:4] / f"{ambiguous_digest}.json"
    )
    os.utime(ambiguous_path, (old_mtime, old_mtime))

    # 6. Registered ACTIVE row missing from backend
    missing_digest = "e" * 64
    missing_ref = f"resultfs://sha256/{missing_digest}?rel=ee/ee/{missing_digest}.json&bytes=12"
    TaskInputPayload.objects.create(
        reference=missing_ref,
        payload_kind=InputPayloadKind.TASK_INPUT,
        backend="filesystem",
        digest=missing_digest,
        size_bytes=12,
        envelope_version=1,
        state=InputPayloadState.ACTIVE,
        last_used_at=now,
    )

    dry_report = discover_and_reap_unregistered_payloads(
        config=config,
        grace_seconds=3600,
        delete_unregistered=False,
        now=now,
    )
    assert dry_report.counts["scanned"] == 6
    assert (
        dry_report.counts["unregistered_eligible"] == 3
    )  # old input, old request, ambiguous before read
    assert dry_report.counts["unregistered_recent"] == 1
    assert dry_report.counts["registered_active"] == 1
    assert dry_report.counts["malformed_key"] == 1
    assert dry_report.counts["registered_missing"] == 1
    assert dry_report.counts["purged"] == 0
    assert old_input_path.exists()
    assert old_request_path.exists()

    # Now run delete_unregistered=True: old_input and old_request are reaped; ambiguous is retained!
    reap_report = discover_and_reap_unregistered_payloads(
        config=config,
        grace_seconds=3600,
        delete_unregistered=True,
        now=now,
    )
    assert reap_report.counts["purged"] == 2
    assert reap_report.counts["ambiguous"] == 1
    assert not old_input_path.exists()
    assert not old_request_path.exists()
    assert recent_input_path.exists()
    assert ambiguous_path.exists()

    # Both reaped objects now have PURGED tombstones with their exact canonical references
    tombstone_input = TaskInputPayload.objects.get(reference=prepared_old_input.input_reference)
    tombstone_request = TaskInputPayload.objects.get(reference=prepared_old_request.reference)
    assert tombstone_input.state == InputPayloadState.PURGED
    assert tombstone_input.payload_kind == InputPayloadKind.TASK_INPUT
    assert tombstone_request.state == InputPayloadState.PURGED
    assert tombstone_request.payload_kind == InputPayloadKind.RAY_JOB_REQUEST

    # Concurrent writer coordination: a writer that prepared old_input before reap
    # now calls register_task_input -> restores the file and reactivates the tombstone!
    reactivated = register_task_input(prepared_old_input, config)
    assert reactivated is not None
    assert reactivated.state == InputPayloadState.ACTIVE
    assert old_input_path.exists()


@pytest.mark.django_db
def test_s3_and_gcs_unregistered_inventory_pagination_and_prefix_guard():
    import hashlib
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    from django_ray.input_storage import _serialize_envelope
    from django_ray.payload_registry import discover_and_reap_unregistered_payloads
    from django_ray.result_storage import GCSResultStorage, ResultStorageError, S3ResultStorage

    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    old_time = now - timedelta(hours=2)
    serialized = _serialize_envelope(["s3-orphan"], {})
    payload_bytes = serialized.encode("utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    key = f"django-ray/inputs/{digest[:2]}/{digest[2:4]}/{digest}.json"

    s3_client = Mock()
    s3_client.list_objects_v2.side_effect = [
        {
            "Contents": [{"Key": key, "Size": len(payload_bytes), "LastModified": old_time}],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        },
        {
            "Contents": [
                {"Key": "django-ray/inputs/malformed.txt", "Size": 4, "LastModified": old_time}
            ],
            "IsTruncated": False,
        },
    ]
    s3_client.head_object.return_value = {"LastModified": old_time}
    s3_client.get_object.return_value = {
        "ContentLength": len(payload_bytes),
        "Body": SimpleNamespace(read=lambda _n: payload_bytes, close=lambda: None),
        "ETag": '"etag-1"',
    }
    s3_config = {
        "INPUT_STORAGE_BACKEND": "s3",
        "INPUT_STORAGE_S3_BUCKET": "my-bucket",
        "INPUT_STORAGE_S3_PREFIX": "django-ray/inputs",
    }
    s3_storage = S3ResultStorage(bucket="my-bucket", prefix="django-ray/inputs", client=s3_client)

    report = discover_and_reap_unregistered_payloads(
        config=s3_config,
        grace_seconds=3600,
        max_objects=10,
        max_pages=2,
        delete_unregistered=True,
        now=now,
        storage_backend=("s3", s3_storage),
    )
    assert report.counts["scanned"] == 2
    assert report.counts["purged"] == 1
    assert report.counts["malformed_key"] == 1
    assert report.has_more is False
    s3_client.delete_object.assert_called_once_with(Bucket="my-bucket", Key=key, IfMatch='"etag-1"')

    # Prefix violation must fail closed
    s3_client.list_objects_v2.side_effect = [
        {
            "Contents": [
                {
                    "Key": "other-prefix/aa/bb/" + digest + ".json",
                    "Size": 10,
                    "LastModified": old_time,
                }
            ],
            "IsTruncated": False,
        }
    ]
    with pytest.raises(ResultStorageError, match="outside the authorized prefix"):
        discover_and_reap_unregistered_payloads(
            config=s3_config,
            now=now,
            storage_backend=("s3", s3_storage),
        )

    # GCS pagination and reap
    gcs_digest = "b" * 64
    gcs_serialized = _serialize_envelope(["gcs-orphan"], {})
    gcs_bytes = gcs_serialized.encode("utf-8")
    gcs_digest = hashlib.sha256(gcs_bytes).hexdigest()
    gcs_key = f"django-ray/inputs/{gcs_digest[:2]}/{gcs_digest[2:4]}/{gcs_digest}.json"
    fake_blob = Mock()
    fake_blob.name = gcs_key
    fake_blob.size = len(gcs_bytes)
    fake_blob.updated = old_time
    fake_blob.generation = 42
    fake_blob.download_as_bytes.return_value = gcs_bytes

    gcs_client = Mock()
    gcs_client.list_blobs.return_value = SimpleNamespace(
        pages=[[fake_blob]],
        next_page_token=None,
    )
    gcs_client.bucket.return_value.blob.return_value = fake_blob
    gcs_config = {
        "INPUT_STORAGE_BACKEND": "gcs",
        "INPUT_STORAGE_GCS_BUCKET": "my-gcs-bucket",
        "INPUT_STORAGE_GCS_PREFIX": "django-ray/inputs",
    }
    gcs_storage = GCSResultStorage(
        bucket="my-gcs-bucket", prefix="django-ray/inputs", client=gcs_client
    )
    gcs_report = discover_and_reap_unregistered_payloads(
        config=gcs_config,
        grace_seconds=3600,
        delete_unregistered=True,
        now=now,
        storage_backend=("gcs", gcs_storage),
    )
    assert gcs_report.counts["purged"] == 1
    fake_blob.delete.assert_called_once_with(if_generation_match=42)


@pytest.mark.django_db
def test_purge_inputs_cli_discover_and_delete_unregistered_modes(tmp_path, settings):
    import os

    from django_ray.input_storage import prepare_task_input

    with pytest.raises(CommandError, match="requires both --discover-unregistered and --delete"):
        call_command("django_ray_purge_inputs", delete_unregistered=True)

    root = tmp_path / "cli-inputs"
    root.mkdir()
    config = {
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(root),
    }
    settings.DJANGO_RAY = {**getattr(settings, "DJANGO_RAY", {}), **config}
    prepared = prepare_task_input(["cli-orphan"], {}, config)
    assert prepared.digest is not None
    orphan_path = root / prepared.digest[:2] / prepared.digest[2:4] / f"{prepared.digest}.json"
    old_mtime = (timezone.now() - timedelta(hours=2)).timestamp()
    os.utime(orphan_path, (old_mtime, old_mtime))

    dry_report, dry_code, _ = _report(discover_unregistered=True, grace_seconds=60)
    assert dry_code == 0
    assert dry_report["unregistered_inventory"]["counts"]["unregistered_eligible"] == 1
    assert orphan_path.exists()

    del_report, del_code, _ = _report(
        discover_unregistered=True,
        delete=True,
        delete_unregistered=True,
        grace_seconds=60,
    )
    assert del_code == 0
    assert del_report["unregistered_inventory"]["counts"]["purged"] == 1
    assert not orphan_path.exists()


@pytest.mark.django_db
def test_payload_registry_validation_and_inventory_edge_branches():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    from django_ray.payload_registry import (
        _normalize_timestamp,
        discover_and_reap_unregistered_payloads,
    )
    from django_ray.result_storage import GCSResultStorage, ResultStorageError, S3ResultStorage

    assert _normalize_timestamp(None) is None
    assert _normalize_timestamp(123) is None
    assert _normalize_timestamp("   ") is None
    assert _normalize_timestamp("not-iso") is None
    naive = datetime(2026, 11, 1, 10, 0, 0)
    assert _normalize_timestamp(naive) == datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
    assert _normalize_timestamp("2026-11-01T10:00:00Z") is None

    with pytest.raises(ValueError, match="grace_seconds"):
        discover_and_reap_unregistered_payloads(grace_seconds=10)
    with pytest.raises(ValueError, match="max_objects"):
        discover_and_reap_unregistered_payloads(max_objects=0)
    with pytest.raises(ValueError, match="max_pages"):
        discover_and_reap_unregistered_payloads(max_pages=0)
    with pytest.raises(ValueError, match="max_details"):
        discover_and_reap_unregistered_payloads(max_details=-1)
    with pytest.raises(ResultStorageError, match="Unsupported payload inventory backend"):
        discover_and_reap_unregistered_payloads(storage_backend=("unknown", object()))

    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    old_time = now - timedelta(hours=2)

    # GCS iterator without .pages (fallback iteration + TypeError fallback on list_blobs)
    gcs_digest = "c" * 64
    gcs_key = f"django-ray/inputs/{gcs_digest[:2]}/{gcs_digest[2:4]}/{gcs_digest}.json"
    blob1 = SimpleNamespace(
        name=gcs_key,
        size=15,
        updated=None,
        time_created=old_time,
    )
    blob_malformed = SimpleNamespace(
        name="django-ray/inputs/not-sharded.json",
        size=10,
        updated=old_time,
    )

    class _FallbackGCSClient:
        def list_blobs(self, bucket, prefix="", **kwargs):
            if kwargs:
                raise TypeError("unexpected kwargs")
            return [blob1, blob_malformed]

    gcs_storage = GCSResultStorage(
        bucket="my-gcs",
        prefix="django-ray/inputs",
        client=_FallbackGCSClient(),
    )
    gcs_config = {
        "INPUT_STORAGE_BACKEND": "gcs",
        "INPUT_STORAGE_GCS_BUCKET": "my-gcs",
        "INPUT_STORAGE_GCS_PREFIX": "django-ray/inputs",
    }
    # Create a PURGED tombstone row for blob1 to cover tombstone classification
    canonical_gcs_ref = f"gs://my-gcs/{gcs_key}?bytes=15"
    TaskInputPayload.objects.create(
        reference=canonical_gcs_ref,
        payload_kind=InputPayloadKind.TASK_INPUT,
        backend="gcs",
        digest=gcs_digest,
        size_bytes=15,
        envelope_version=1,
        state=InputPayloadState.PURGED,
        last_used_at=old_time,
        purged_at=now,
    )
    rep = discover_and_reap_unregistered_payloads(
        config=gcs_config,
        now=now,
        storage_backend=("gcs", gcs_storage),
    )
    assert rep.counts["tombstone"] == 1
    assert rep.counts["malformed_key"] == 1

    # S3 missing NextContinuationToken when IsTruncated=True
    s3_bad = Mock()
    s3_bad.list_objects_v2.return_value = {"Contents": [], "IsTruncated": True}
    s3_storage = S3ResultStorage(bucket="b", prefix="django-ray/inputs", client=s3_bad)
    with pytest.raises(ResultStorageError, match="NextContinuationToken"):
        discover_and_reap_unregistered_payloads(
            config={
                "INPUT_STORAGE_BACKEND": "s3",
                "INPUT_STORAGE_S3_BUCKET": "b",
                "INPUT_STORAGE_S3_PREFIX": "django-ray/inputs",
            },
            now=now,
            storage_backend=("s3", s3_storage),
        )


@pytest.mark.django_db
def test_payload_registry_reap_and_modified_at_edge_cases(tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    from django_ray.payload_registry import (
        _current_object_modified_at,
        _payload_registry_references,
        _RawDiscoveredObject,
        _reap_unregistered_object,
        discover_and_reap_unregistered_payloads,
    )
    from django_ray.result_storage import (
        FilesystemResultStorage,
        GCSResultStorage,
        ResultStorageError,
        S3ResultStorage,
    )

    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    old_time = now - timedelta(hours=2)
    cutoff = now - timedelta(hours=1)

    # _current_object_modified_at edge cases
    no_key_item = _RawDiscoveredObject(fingerprint_sha256="fp", malformed=False, storage_key=None)
    assert _current_object_modified_at(object(), no_key_item) is None

    fs_storage = FilesystemResultStorage(tmp_path)
    missing_item = _RawDiscoveredObject(
        fingerprint_sha256="fp",
        malformed=False,
        storage_key="aa/bb/" + "a" * 64 + ".json",
        modified_at=old_time,
    )
    assert _current_object_modified_at(fs_storage, missing_item) is None
    assert _current_object_modified_at(object(), missing_item) == old_time

    # S3 without head_object callable falls back to item.modified_at; head_object error raises
    s3_no_head = S3ResultStorage(bucket="b", prefix="p", client=SimpleNamespace(head_object=None))
    assert _current_object_modified_at(s3_no_head, missing_item) == old_time
    s3_err_client = Mock()
    s3_err_client.head_object.side_effect = RuntimeError("head boom")
    s3_err = S3ResultStorage(bucket="b", prefix="p", client=s3_err_client)
    with pytest.raises(ResultStorageError, match="revalidate S3 payload age"):
        _current_object_modified_at(s3_err, missing_item)

    # GCS reload error raises
    gcs_err_client = Mock()
    gcs_err_client.bucket.return_value.blob.return_value.reload.side_effect = RuntimeError(
        "gcs boom"
    )
    gcs_err = GCSResultStorage(bucket="b", prefix="p", client=gcs_err_client)
    with pytest.raises(ResultStorageError, match="revalidate GCS payload age"):
        _current_object_modified_at(gcs_err, missing_item)

    # _reap_unregistered_object edge outcomes: skipped (missing file), unregistered_recent, failed, attached execution
    digest = "d" * 64
    native_ref = f"resultfs://sha256/{digest}?rel=dd/dd/{digest}.json&bytes=10"
    refs = _payload_registry_references(native_ref, {})
    raw_obj = _RawDiscoveredObject(
        fingerprint_sha256="fp-d",
        malformed=False,
        digest=digest,
        size_bytes=10,
        native_reference=native_ref,
        modified_at=old_time,
        storage_key=f"dd/dd/{digest}.json",
    )
    assert (
        _reap_unregistered_object(
            fs_storage,
            backend_name="filesystem",
            item=raw_obj,
            references=refs,
            cutoff=cutoff,
            use_default_delete=False,
        )
        == "skipped"
    )

    # Create file with recent mtime -> unregistered_recent
    dd_dir = tmp_path / "dd" / "dd"
    dd_dir.mkdir(parents=True)
    f_path = dd_dir / f"{digest}.json"
    f_path.write_text('{"a":"bc"}', encoding="utf-8")
    assert (
        _reap_unregistered_object(
            fs_storage,
            backend_name="filesystem",
            item=raw_obj,
            references=refs,
            cutoff=cutoff,
            use_default_delete=False,
        )
        == "unregistered_recent"
    )

    # Discover with mismatched digest in DB row -> ambiguous, and attached RayTaskExecution -> ambiguous
    import os

    os.utime(f_path, (old_time.timestamp(), old_time.timestamp()))
    TaskInputPayload.objects.create(
        reference=native_ref,
        payload_kind=InputPayloadKind.TASK_INPUT,
        backend="filesystem",
        digest="0" * 64,  # mismatch!
        size_bytes=10,
        envelope_version=1,
        state=InputPayloadState.ACTIVE,
        last_used_at=old_time,
    )
    rep_mismatch = discover_and_reap_unregistered_payloads(
        config={
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        },
        now=now,
        storage_backend=("filesystem", fs_storage),
    )
    assert rep_mismatch.counts["ambiguous"] >= 1

    TaskInputPayload.objects.filter(reference=native_ref).delete()
    _execution(native_ref)
    rep_attached = discover_and_reap_unregistered_payloads(
        config={
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        },
        now=now,
        storage_backend=("filesystem", fs_storage),
    )
    assert rep_attached.counts["ambiguous"] >= 1


@pytest.mark.django_db
def test_payload_registry_s3_gcs_and_filesystem_error_branches(tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    from django_ray.payload_registry import (
        _discover_filesystem_objects,
        _discover_gcs_objects,
        _discover_s3_objects,
        _payload_registry_references,
        _RawDiscoveredObject,
        _reap_unregistered_object,
    )
    from django_ray.result_storage import (
        FilesystemResultStorage,
        GCSResultStorage,
        ResultStorageError,
        S3ResultStorage,
    )

    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    old_time = now - timedelta(hours=2)
    cutoff = now - timedelta(hours=1)

    # _discover_filesystem_objects: missing root returns ([], False); file root raises; non-hex subdir
    missing_fs = FilesystemResultStorage(tmp_path / "missing-root")
    assert _discover_filesystem_objects(missing_fs, max_objects=10) == ([], False)

    file_root = tmp_path / "file-root"
    file_root.write_text("x", encoding="utf-8")
    with pytest.raises(ResultStorageError, match="not a directory"):
        _discover_filesystem_objects(FilesystemResultStorage(file_root), max_objects=10)

    hex_root = tmp_path / "hex-root"
    (hex_root / "bad_dir_1").mkdir(parents=True)
    (hex_root / "bad_dir_2").mkdir(parents=True)
    items, has_more = _discover_filesystem_objects(FilesystemResultStorage(hex_root), max_objects=1)
    assert len(items) == 1 and items[0].malformed is True and has_more is True

    # _discover_s3_objects error branches
    s3_client = Mock()
    s3_storage = S3ResultStorage(bucket="b", prefix="p", client=s3_client)
    s3_client.list_objects_v2.side_effect = RuntimeError("s3 down")
    with pytest.raises(ResultStorageError, match="Failed to list S3"):
        _discover_s3_objects(s3_storage, max_objects=5, max_pages=2)

    s3_client.list_objects_v2.side_effect = None
    s3_client.list_objects_v2.return_value = "not-a-dict"
    with pytest.raises(ResultStorageError, match="invalid response"):
        _discover_s3_objects(s3_storage, max_objects=5, max_pages=2)

    s3_client.list_objects_v2.return_value = {"Contents": "not-a-list"}
    with pytest.raises(ResultStorageError, match="Contents must be a list"):
        _discover_s3_objects(s3_storage, max_objects=5, max_pages=2)

    s3_client.list_objects_v2.return_value = {"Contents": ["not-a-dict"]}
    with pytest.raises(ResultStorageError, match="entry must be an object"):
        _discover_s3_objects(s3_storage, max_objects=5, max_pages=2)

    s3_client.list_objects_v2.return_value = {"Contents": [{"Key": ""}]}
    with pytest.raises(ResultStorageError, match="missing a valid Key"):
        _discover_s3_objects(s3_storage, max_objects=5, max_pages=2)

    s3_client.list_objects_v2.return_value = {"Contents": [], "IsTruncated": "bad"}
    with pytest.raises(ResultStorageError, match="IsTruncated flag is invalid"):
        _discover_s3_objects(s3_storage, max_objects=5, max_pages=2)

    # _discover_gcs_objects error and max_pages branches
    gcs_client = Mock()
    gcs_storage = GCSResultStorage(bucket="b", prefix="p", client=gcs_client)
    gcs_client.list_blobs.side_effect = RuntimeError("gcs down")
    with pytest.raises(ResultStorageError, match="Failed to list GCS"):
        _discover_gcs_objects(gcs_storage, max_objects=5, max_pages=1)

    gcs_client.list_blobs.side_effect = None
    bad_blob = SimpleNamespace(name="", size=10, updated=old_time)
    gcs_client.list_blobs.return_value = SimpleNamespace(pages=[[bad_blob]], next_page_token=None)
    with pytest.raises(ResultStorageError, match="missing a valid name"):
        _discover_gcs_objects(gcs_storage, max_objects=5, max_pages=1)

    good_blob = SimpleNamespace(name="p/not-digest.json", size=10, updated=old_time)
    gcs_client.list_blobs.return_value = SimpleNamespace(
        pages=[[good_blob], [good_blob]], next_page_token="more"
    )
    g_items, g_more = _discover_gcs_objects(gcs_storage, max_objects=5, max_pages=1)
    assert len(g_items) == 1 and g_more is True

    # _reap_unregistered_object when load returns None, when row exists, or when attached
    from django_ray.input_storage import _serialize_envelope

    env_str = _serialize_envelope(["x"], {})
    digest = "f" * 64
    native_ref = f"resultfs://sha256/{digest}?rel=ff/ff/{digest}.json&bytes={len(env_str)}"
    refs = _payload_registry_references(native_ref, {})
    raw_obj = _RawDiscoveredObject(
        fingerprint_sha256="fp-f",
        malformed=False,
        digest=digest,
        size_bytes=len(env_str),
        native_reference=native_ref,
        modified_at=old_time,
        storage_key=f"ff/ff/{digest}.json",
    )
    fake_backend = SimpleNamespace(load=lambda **_kw: None, delete=lambda **_kw: None)
    assert (
        _reap_unregistered_object(
            fake_backend,
            backend_name="s3",
            item=raw_obj,
            references=refs,
            cutoff=cutoff,
            use_default_delete=False,
        )
        == "skipped"
    )

    fake_backend_valid = SimpleNamespace(load=lambda **_kw: env_str, delete=lambda **_kw: None)
    _execution(native_ref)
    assert (
        _reap_unregistered_object(
            fake_backend_valid,
            backend_name="s3",
            item=raw_obj,
            references=refs,
            cutoff=cutoff,
            use_default_delete=False,
        )
        == "ambiguous"
    )

    _payload(native_ref)
    assert (
        _reap_unregistered_object(
            fake_backend_valid,
            backend_name="s3",
            item=raw_obj,
            references=refs,
            cutoff=cutoff,
            use_default_delete=False,
        )
        == "skipped"
    )
