"""Management-command coverage for workflow-progress cleanup."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskState, WorkflowProgressRunStorage


def _expired_run(suffix: str) -> WorkflowProgressRunStorage:
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-cleanup-command-{suffix}",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.FAILED,
        attempt_number=1,
        execution_generation=1,
        finished_at=timezone.now(),
    )
    return WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=1,
        execution_generation=1,
        run_id=uuid4(),
        detail_expires_at=timezone.now() - timedelta(minutes=1),
    )


def _empty_inactive_run(suffix: str) -> WorkflowProgressRunStorage:
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-cleanup-command-empty-{suffix}",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
    )
    return WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=1,
        execution_generation=1,
        run_id=uuid4(),
    )


@pytest.mark.django_db
def test_command_is_dry_run_by_default() -> None:
    run_storage = _expired_run("dry-run")
    stdout = StringIO()

    call_command("django_ray_cleanup_workflow_progress", stdout=stdout)

    assert WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    assert "cleanup dry-run: 1 eligible, 0 deleted, 0 failed" in stdout.getvalue()


@pytest.mark.django_db
def test_command_requires_explicit_delete_flag() -> None:
    run_storage = _expired_run("delete")
    stdout = StringIO()

    call_command(
        "django_ray_cleanup_workflow_progress",
        delete=True,
        stdout=stdout,
    )

    assert not WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    assert "cleanup delete: 1 eligible, 1 deleted, 0 failed" in stdout.getvalue()


@pytest.mark.django_db
def test_command_reports_and_deletes_empty_inactive_runs() -> None:
    run_storage = _empty_inactive_run("delete")
    stdout = StringIO()

    call_command(
        "django_ray_cleanup_workflow_progress",
        delete=True,
        stdout=stdout,
    )

    assert not WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    assert "1 empty runs" in stdout.getvalue()


@pytest.mark.django_db
def test_command_continues_then_exits_nonzero_after_item_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = _expired_run("failed")
    good = _expired_run("good")
    original_delete = WorkflowProgressRunStorage.delete

    def fail_one(self, *args, **kwargs):
        if self.pk == failed.pk:
            raise OSError("password=do-not-log")
        return original_delete(self, *args, **kwargs)

    monkeypatch.setattr(WorkflowProgressRunStorage, "delete", fail_one)
    stdout = StringIO()

    with pytest.raises(CommandError, match="Failed to clean 1 workflow progress item"):
        call_command(
            "django_ray_cleanup_workflow_progress",
            delete=True,
            stdout=stdout,
        )

    assert WorkflowProgressRunStorage.objects.filter(pk=failed.pk).exists()
    assert not WorkflowProgressRunStorage.objects.filter(pk=good.pk).exists()
    failed.refresh_from_db()
    assert failed.cleanup_error == "EXPIRED_RUN_DELETE_FAILED: OSError; message=<redacted>"
    assert "do-not-log" not in stdout.getvalue()
    assert "1 deleted, 1 failed" in stdout.getvalue()


@pytest.mark.parametrize("batch_size", [0, 1001])
def test_command_rejects_out_of_range_batch_size(batch_size: int) -> None:
    with pytest.raises(CommandError, match="--batch-size must be between 1 and 1000"):
        call_command("django_ray_cleanup_workflow_progress", batch_size=batch_size)
