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
    return f"inputfs://sha256/{suffix * 64}?bytes=128"


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
