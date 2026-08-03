"""Tests for retention-safe task-input payload cleanup."""

from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from io import StringIO
from types import ModuleType

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from django_ray.management.commands.django_ray_purge_inputs import Command
from django_ray.models import InputPayloadState, RayTaskExecution, TaskInputPayload, TaskState


def _reference(suffix: str) -> str:
    return f"inputfs://sha256/{suffix * 64}?bytes=128"


def _fingerprint(reference: str) -> str:
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16]


def _payload(reference: str, *, age_days: int = 60) -> TaskInputPayload:
    used_at = timezone.now() - timedelta(days=age_days)
    return TaskInputPayload.objects.create(
        reference=reference,
        backend="filesystem",
        digest="a" * 64,
        size_bytes=128,
        envelope_version=1,
        created_at=used_at,
        last_used_at=used_at,
    )


def _execution(
    reference: str,
    *,
    state: str = TaskState.SUCCEEDED,
    age_days: int = 60,
) -> RayTaskExecution:
    timestamp = timezone.now() - timedelta(days=age_days)
    terminal_states = {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.LOST,
        TaskState.EXPIRED,
    }
    return RayTaskExecution.objects.create(
        task_id=f"purge-{state.lower()}-{age_days}",
        callable_path="testproject.tasks.add_numbers",
        state=state,
        input_reference=reference,
        created_at=timestamp,
        finished_at=timestamp if state in terminal_states else None,
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
def test_delete_tombstones_registry_and_retains_execution_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _reference("b")
    payload = _payload(reference)
    first = _execution(reference, state=TaskState.FAILED)
    second = _execution(reference, state=TaskState.CANCELLED)
    third = _execution(reference, state=TaskState.EXPIRED)
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
    assert first.input_reference == reference
    assert second.input_reference == reference
    assert third.input_reference == reference
    assert f"reference_sha256={_fingerprint(reference)}" in stdout.getvalue()
    assert reference not in stdout.getvalue()
    assert "1 eligible, 1 purged, 0 failed" in stdout.getvalue()


@pytest.mark.django_db
def test_delete_skips_reference_with_active_recent_or_undated_terminal_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references = [_reference("c"), _reference("d"), _reference("e")]
    for reference in references:
        _payload(reference)

    _execution(references[0], state=TaskState.RUNNING)
    _execution(references[1], age_days=1)
    undated = _execution(references[2], state=TaskState.LOST)
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
def test_orphaned_old_registry_entry_is_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = _reference("f")
    payload = _payload(reference)
    deleted: list[str] = []
    _install_input_storage(monkeypatch, deleted.append)

    call_command("django_ray_purge_inputs", retention_days=30, delete=True)

    payload.refresh_from_db()
    assert deleted == [reference]
    assert payload.state == InputPayloadState.PURGED


@pytest.mark.django_db
def test_delete_failure_is_recorded_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = _reference("1")
    payload = _payload(reference)
    _execution(reference)

    def fail_delete(_reference: str) -> None:
        raise OSError("storage unavailable")

    _install_input_storage(monkeypatch, fail_delete)
    stderr = StringIO()

    with pytest.raises(CommandError, match="Failed to purge 1 input payload"):
        call_command(
            "django_ray_purge_inputs",
            retention_days=30,
            delete=True,
            stderr=stderr,
        )

    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.purged_at is None
    assert payload.cleanup_error == "OSError"
    assert f"reference_sha256={_fingerprint(reference)}" in stderr.getvalue()
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

    assert str(payload) == f"filesystem input {'a' * 12} (ACTIVE)"
