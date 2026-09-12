"""Unit tests for cancellation helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace

import pytest

from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.models import RayTaskCohortClaim, RayTaskExecution, TaskAttempt, TaskState
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
    finalize_cancellation,
    prepare_remote_cancellation,
    request_cancellation,
    request_remote_cancellation,
)
from tests.migration_cleanup import preactivation_protocol_schema as preactivation_protocol_schema


@pytest.fixture
def current_claim(monkeypatch):
    """Use a real current claim and the existing stopped SQLite ledger teardown."""
    from tests.integration.test_cohort_claim_storage import (
        case,
        isolated_sqlite_ledger_maintenance,
    )

    cleanup = isolated_sqlite_ledger_maintenance.__wrapped__()
    next(cleanup)
    try:
        yield case.__wrapped__(monkeypatch)
    finally:
        with pytest.raises(StopIteration):
            next(cleanup)


@pytest.fixture
def historical_generation(preactivation_protocol_schema, monkeypatch):
    """Retain the released raw owner/generation replacement fixture on0034."""
    from django_ray.lifecycle import _request_task_cancellation

    monkeypatch.setattr(
        "django_ray.runner.cancellation.request_task_cancellation",
        partial(_request_task_cancellation, supported_protocols=ExecutionProtocolRange(1, 1)),
    )


@pytest.mark.django_db
class TestCancellationHelpers:
    """Tests for request/finalize cancellation behavior."""

    def test_request_cancellation_rejects_terminal_state(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="[]",
            kwargs_json="{}",
        )

        ok = request_cancellation(task, runner=SimpleNamespace(cancel=lambda handle: True))

        task.refresh_from_db()
        assert ok is False
        assert task.state == TaskState.SUCCEEDED

    @pytest.mark.django_db(transaction=True)
    def test_request_cancellation_does_not_overwrite_stale_terminal_state(
        self, current_claim
    ) -> None:
        from tests.integration.test_cohort_completion import _apply, _started

        dispatch = _started(current_claim)
        task = RayTaskExecution.objects.get(pk=dispatch.execution.pk)
        assert _apply(dispatch, current_claim).applied
        assert task.state == TaskState.RUNNING

        seen: list[object] = []
        ok = request_cancellation(
            task,
            runner=SimpleNamespace(cancel=lambda handle: seen.append(handle) or True),
        )

        task.refresh_from_db()
        assert ok is False
        assert task.state == TaskState.SUCCEEDED
        assert seen == []

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.usefixtures("historical_generation")
    def test_request_cancellation_does_not_overwrite_newer_generation(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-stale-generation-001",
            execution_protocol_version=1,
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            execution_generation=4,
            claimed_by_worker="old-worker",
            ray_job_id="raysubmit_old",
            args_json="[]",
            kwargs_json="{}",
        )
        RayTaskExecution.objects.filter(pk=task.pk).update(
            execution_generation=5,
            claimed_by_worker="new-worker",
            ray_job_id="raysubmit_new",
        )

        seen: list[object] = []
        ok = request_cancellation(
            task,
            runner=SimpleNamespace(cancel=lambda handle: seen.append(handle) or True),
        )

        task.refresh_from_db()
        assert ok is False
        assert task.state == TaskState.RUNNING
        assert task.execution_generation == 5
        assert task.claimed_by_worker == "new-worker"
        assert task.ray_job_id == "raysubmit_new"
        assert seen == []

    def test_request_cancellation_marks_state_and_calls_runner(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-002",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_cancel_001",
            ray_address="ray://cluster:10001",
            started_at=datetime.now(UTC),
            args_json="[]",
            kwargs_json="{}",
        )

        seen: list[object] = []

        class Runner:
            def cancel(self, handle) -> bool:
                seen.append(handle)
                return True

        ok = request_cancellation(task, runner=Runner())

        task.refresh_from_db()
        assert ok is True
        assert task.state == TaskState.CANCELLING
        assert len(seen) == 1
        assert seen[0].ray_job_id == "raysubmit_cancel_001"
        assert seen[0].ray_address == "ray://cluster:10001"

    def test_request_cancellation_uses_exact_pending_ray_core_handle(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-ray-core-exact-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            attempt_number=3,
            execution_generation=7,
            ray_job_id="ray_core:123",
            args_json="[]",
            kwargs_json="{}",
        )
        exact_handle = object()
        lookups: list[tuple[int, int, int]] = []
        cancelled: list[object] = []

        class Runner:
            def get_pending_handle(
                self,
                task_pk: int,
                *,
                attempt_number: int,
                execution_generation: int,
            ) -> object:
                lookups.append((task_pk, attempt_number, execution_generation))
                return exact_handle

            def cancel_pending(self, handle: object) -> bool:
                cancelled.append(handle)
                return True

            def cancel(self, _handle: object) -> bool:
                pytest.fail("legacy Ray Core cancellation must use the exact capability")

        assert request_cancellation(task, runner=Runner()) is True

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert lookups == [(task.pk, 3, 7)]
        assert cancelled == [exact_handle]

    def test_request_cancellation_fails_closed_without_exact_ray_core_handle(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-ray-core-missing-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=5,
            ray_job_id="ray_core:456",
            args_json="[]",
            kwargs_json="{}",
        )
        lookups: list[tuple[int, int, int]] = []

        class Runner:
            def get_pending_handle(
                self,
                task_pk: int,
                *,
                attempt_number: int,
                execution_generation: int,
            ) -> None:
                lookups.append((task_pk, attempt_number, execution_generation))
                return None

            def cancel_pending(self, _handle: object) -> bool:
                pytest.fail("no replacement handle may be cancelled")

            def cancel(self, _handle: object) -> bool:
                pytest.fail("PK-only fallback cancellation is ambiguous")

        assert request_cancellation(task, runner=Runner()) is True

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert lookups == [(task.pk, 2, 5)]

    def test_request_cancellation_finalizes_queued_without_runner(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-queued-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
        )
        seen: list[object] = []

        ok = request_cancellation(
            task,
            runner=SimpleNamespace(cancel=lambda handle: seen.append(handle) or True),
        )

        assert ok is True
        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert TaskAttempt.objects.get(execution=task).state == TaskState.CANCELLED
        assert seen == []

    def test_request_cancellation_ignores_runner_errors(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-003",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_cancel_002",
            args_json="[]",
            kwargs_json="{}",
        )

        class Runner:
            def cancel(self, handle) -> bool:  # noqa: ARG002
                raise RuntimeError("ray unavailable")

        ok = request_cancellation(task, runner=Runner())

        task.refresh_from_db()
        assert ok is True
        assert task.state == TaskState.CANCELLING

    @pytest.mark.django_db(transaction=True)
    def test_finalize_cancellation_sets_terminal_state(self, current_claim) -> None:
        from django_ray.lifecycle import request_task_cancellation
        from django_ray.runner.cohort_cancellation import apply_cohort_cancellation
        from tests.integration.test_cohort_completion import _started

        dispatch = _started(current_claim, "ray_core")
        task = dispatch.execution
        assert request_task_cancellation(task.pk).accepted
        applied = apply_cohort_cancellation(
            dispatch,
            evidence_kind="owned_core_terminal",
            apply_cancel=finalize_cancellation,
            now=current_claim.now,
        )
        assert applied.applied
        task.refresh_from_db()

        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert TaskAttempt.objects.get(execution=task).state == TaskState.CANCELLED
        assert RayTaskCohortClaim.objects.get(pk=dispatch.claim.claim_id).disposition == "RESOLVED"

    @pytest.mark.django_db(transaction=True)
    def test_finalize_cancellation_does_not_overwrite_race(self, current_claim) -> None:
        from django_ray.lifecycle import request_task_cancellation
        from tests.integration.test_cohort_completion import _apply, _started

        dispatch = _started(current_claim)
        assert request_task_cancellation(dispatch.execution.pk).accepted
        task = RayTaskExecution.objects.get(pk=dispatch.execution.pk)
        assert _apply(dispatch, current_claim).applied
        assert task.state == TaskState.CANCELLING

        assert (
            finalize_cancellation(task, expected_worker_id=current_claim.owner.worker_id) is False
        )
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED

    def test_request_remote_cancellation_uses_status_aware_runner(self) -> None:
        expected = CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        class Runner:
            def cancel_with_status(self, _handle):
                return expected

        outcome = request_remote_cancellation(
            Runner(), SubmissionHandle("job", "", datetime.now(UTC))
        )

        assert outcome == expected

    def test_request_remote_cancellation_records_indeterminate_exception(self) -> None:
        class Runner:
            def cancel(self, _handle):
                raise RuntimeError("ray unavailable")

        outcome = request_remote_cancellation(
            Runner(), SubmissionHandle("job", "", datetime.now(UTC))
        )

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "ray unavailable" in (outcome.message or "")

    def test_request_remote_cancellation_records_status_aware_exception(self) -> None:
        class Runner:
            def cancel_with_status(self, _handle):
                raise RuntimeError("status-aware client unavailable")

        outcome = request_remote_cancellation(
            Runner(), SubmissionHandle("job", "", datetime.now(UTC))
        )

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "status-aware client unavailable" in (outcome.message or "")

    def test_request_remote_cancellation_survives_broken_exception_message(self) -> None:
        calls = 0

        class BrokenCancellationError(RuntimeError):
            def __str__(self) -> str:
                nonlocal calls
                calls += 1
                raise RuntimeError("secondary password=do-not-expose")

        class Runner:
            def cancel_with_status(self, _handle):
                raise BrokenCancellationError()

        outcome = request_remote_cancellation(
            Runner(), SubmissionHandle("job", "", datetime.now(UTC))
        )

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert outcome.message == (
            "Cancellation request raised BrokenCancellationError: exception message unavailable"
        )
        assert "secondary password" not in (outcome.message or "")
        assert calls == 1

    def test_request_remote_cancellation_uses_prepared_capability(self) -> None:
        expected = CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
        capability = object()
        handle = SubmissionHandle("job", "ray://cluster:10001", datetime.now(UTC))

        class Runner:
            def prepare_cancellation(self, prepared_handle):
                assert prepared_handle is handle
                return capability

            def cancel_prepared_with_status(self, prepared_handle, prepared_capability):
                assert prepared_handle is handle
                assert prepared_capability is capability
                return expected

            def cancel_with_status(self, _handle):
                pytest.fail("prepared cancellation must not resolve the client again")

        runner = Runner()
        prepared = prepare_remote_cancellation(runner, handle)
        outcome = request_remote_cancellation(runner, handle, prepared=prepared)

        assert prepared.supported is True
        assert prepared.error is None
        assert outcome == expected

    def test_prepared_cancellation_records_resolution_timeout(self) -> None:
        handle = SubmissionHandle("job", "ray://cluster:10001", datetime.now(UTC))

        class Runner:
            def prepare_cancellation(self, _handle):
                raise TimeoutError("Ray address resolution timed out")

            def cancel_prepared_with_status(self, _handle, _capability):
                pytest.fail("failed preparation must not execute a stop")

        runner = Runner()
        prepared = prepare_remote_cancellation(runner, handle)
        outcome = request_remote_cancellation(runner, handle, prepared=prepared)

        assert prepared.supported is True
        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "Ray address resolution timed out" in (outcome.message or "")

    def test_prepared_cancellation_requires_matching_execution_capability(self) -> None:
        handle = SubmissionHandle("job", "ray://cluster:10001", datetime.now(UTC))

        class Runner:
            def prepare_cancellation(self, _handle):
                return object()

        runner = Runner()
        prepared = prepare_remote_cancellation(runner, handle)
        outcome = request_remote_cancellation(runner, handle, prepared=prepared)

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "cannot execute the prepared capability" in (outcome.message or "")

    def test_prepared_cancellation_bounds_execution_failure(self) -> None:
        handle = SubmissionHandle("job", "ray://cluster:10001", datetime.now(UTC))

        class Runner:
            def prepare_cancellation(self, _handle):
                return object()

            def cancel_prepared_with_status(self, _handle, _capability):
                raise RuntimeError("prepared client disconnected")

        runner = Runner()
        prepared = prepare_remote_cancellation(runner, handle)
        outcome = request_remote_cancellation(runner, handle, prepared=prepared)

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "prepared client disconnected" in (outcome.message or "")

    def test_request_cancellation_rejects_unsaved_execution(self) -> None:
        task = RayTaskExecution(
            task_id="cancel-unsaved",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json="{}",
        )

        assert (
            request_cancellation(
                task,
                runner=SimpleNamespace(cancel=lambda _handle: True),
            )
            is False
        )
