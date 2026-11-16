"""Bound old-attempt reads while retaining the ordinary Django save boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from django.db.models.signals import post_save, pre_save

from django_ray.lifecycle import cancel_task
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

pytestmark = pytest.mark.django_db

_PROTECTED_FIELDS = {
    "error_message",
    "error_traceback",
    "result_data",
    "workflow_progress_summary_json",
}


@pytest.mark.parametrize("existing_archive", [False, True])
def test_archive_projection_preserves_data_creation_time_and_save_signals(
    monkeypatch: pytest.MonkeyPatch,
    existing_archive: bool,
) -> None:
    task = RayTaskExecution.objects.create(
        task_id=f"archive-projection-{existing_archive}",
        callable_path="synthetic.task",
        state=TaskState.CANCELLING,
        attempt_number=3,
        execution_generation=8,
        started_at=datetime(2026, 11, 15, tzinfo=UTC),
        error_message="new protected error",
        error_traceback="new protected traceback\nsecond line",
        result_data='{"preserved":"result"}',
        result_reference="digest:" + "b" * 64,
    )
    original_created = datetime(2026, 1, 1, tzinfo=UTC)
    old_payload = "old-protected-archive-" + "x" * (512 * 1024)
    if existing_archive:
        TaskAttempt.objects.create(
            execution=task,
            attempt_number=3,
            state=TaskState.FAILED,
            error_message=old_payload,
            error_traceback=old_payload,
            result_data=old_payload,
            workflow_progress_summary_json=old_payload,
            created_at=original_created,
        )
    observed_projections: list[set[str]] = []
    original_from_db = TaskAttempt.from_db
    original_save = TaskAttempt.save
    save_calls: list[set[str]] = []
    events: list[dict[str, Any]] = []

    def guarded_from_db(cls, database, field_names, values, **kwargs):
        fields = set(field_names)
        assert fields.isdisjoint(_PROTECTED_FIELDS)
        observed_projections.append(fields)
        return original_from_db(database, field_names, values, **kwargs)

    def observe_save(instance, *args, **kwargs):
        save_calls.append(set(kwargs.get("update_fields") or ()))
        return original_save(instance, *args, **kwargs)

    def observe_signal(sender, instance, **kwargs):
        events.append(
            {
                "created": kwargs.get("created"),
                "state": instance.state,
                "error_message": instance.error_message,
                "error_traceback": instance.error_traceback,
                "result_data": instance.result_data,
            }
        )

    pre_save.connect(observe_signal, sender=TaskAttempt, weak=False)
    post_save.connect(observe_signal, sender=TaskAttempt, weak=False)
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(TaskAttempt, "from_db", classmethod(guarded_from_db))
            patcher.setattr(TaskAttempt, "save", observe_save)
            assert cancel_task(task, expected_attempt_number=3, expected_execution_generation=8)
    finally:
        pre_save.disconnect(observe_signal, sender=TaskAttempt)
        post_save.disconnect(observe_signal, sender=TaskAttempt)

    archive = TaskAttempt.objects.get(execution=task, attempt_number=3)
    assert archive.state == TaskState.CANCELLED
    assert archive.error_message == task.error_message == "new protected error"
    assert archive.error_traceback == task.error_traceback
    assert archive.result_data == task.result_data
    assert archive.result_reference == task.result_reference
    assert archive.started_at == task.started_at
    assert archive.finished_at == task.finished_at
    assert archive.workflow_progress_summary_json == (old_payload if existing_archive else None)
    if existing_archive:
        assert archive.created_at == original_created
        assert observed_projections == [{"id", "created_at"}]
        assert "created_at" in save_calls[0]
    else:
        assert archive.created_at >= original_created
        assert not observed_projections
    assert len(save_calls) == 1
    assert len(events) == 2
    assert events[0]["created"] is None
    assert events[1]["created"] is not existing_archive
    assert all(event["state"] == TaskState.CANCELLED for event in events)
    assert all(event["error_traceback"] == task.error_traceback for event in events)
    assert TaskAttempt.objects.filter(execution=task, attempt_number=3).count() == 1


def test_trusted_save_receiver_can_still_read_an_omitted_deferred_summary() -> None:
    task = RayTaskExecution.objects.create(
        task_id="archive-receiver-deferred-summary",
        callable_path="synthetic.task",
        state=TaskState.CANCELLING,
    )
    retained_summary = "retained old summary"
    TaskAttempt.objects.create(
        execution=task,
        attempt_number=1,
        state=TaskState.FAILED,
        workflow_progress_summary_json=retained_summary,
    )
    observed: list[str] = []

    def read_summary(sender, instance, **kwargs):
        observed.append(instance.workflow_progress_summary_json)

    post_save.connect(read_summary, sender=TaskAttempt, weak=False)
    try:
        assert cancel_task(task)
    finally:
        post_save.disconnect(read_summary, sender=TaskAttempt)
    assert observed == [retained_summary]


def test_archive_projection_rejects_stale_generation_before_save() -> None:
    task = RayTaskExecution.objects.create(
        task_id="archive-projection-stale",
        callable_path="synthetic.task",
        state=TaskState.CANCELLING,
        execution_generation=8,
    )
    assert not cancel_task(task, expected_execution_generation=7)
    assert not TaskAttempt.objects.filter(execution=task).exists()
    task.refresh_from_db()
    assert task.state == TaskState.CANCELLING
