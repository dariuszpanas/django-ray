"""Integration tests for admin cancellation, attempt, lease, and protocol policy models."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.contrib import admin
from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import signing as django_signing
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.template.response import TemplateResponse
from django.test import Client, RequestFactory, override_settings
from django.test.html import Element, parse_html
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from django_ray.admin import (
    ADMIN_ATTEMPT_INLINE_MAX_CHARS,
    ADMIN_DIAGNOSTIC_MAX_CHARS,
    ADMIN_RETRY_CONFIRMATION_MAX_TASKS,
    ActiveWorkerFilter,
    ExecutionProtocolFilter,
    TaskAttemptInline,
)
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from tests.integration._admin_helpers import (
    _ADMIN_MIDDLEWARE,
    _attempt_admin,
    _confirmed_retry_request,
    _lease_admin,
    _protocol_policy_admin,
    _request,
    _retry_confirmation,
    _retry_confirmation_context,
    _retry_request,
    _task_admin,
    _walk_html,
)
from tests.workflow_progress_summary_helpers import (
    workflow_progress_summary,
)


@pytest.mark.django_db
class TestRayTaskExecutionAdminCancellation:
    """Tests for task admin CSRF and cancellation actions."""

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_detail_retry_requires_csrf_for_both_posts(self) -> None:
        user = get_user_model().objects.create_superuser(username="retry-detail-csrf")
        task = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-detail-csrf",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        detail_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[task.pk],
        )
        retry_url = reverse(
            "admin:django_ray_raytaskexecution_retry",
            args=[task.pk],
        )

        assert client.post(retry_url).status_code == 403
        detail = client.get(detail_url)
        csrf_match = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"',
            detail.content.decode(),
        )
        assert csrf_match is not None
        confirmation = client.post(
            retry_url,
            {"csrfmiddlewaretoken": csrf_match.group(1)},
        )
        assert confirmation.status_code == 200
        confirmation_html = confirmation.content.decode()
        confirmation_token_match = re.search(
            r'name="retry_confirmation_token"[^>]*value="([^"]+)"',
            confirmation_html,
            re.DOTALL,
        )
        assert confirmation_token_match is not None
        confirmed_data = {
            "post": "yes",
            "retry_confirmation_token": confirmation_token_match.group(1),
        }

        assert client.post(retry_url, confirmed_data).status_code == 403
        confirmed = client.post(
            retry_url,
            {**confirmed_data, "csrfmiddlewaretoken": csrf_match.group(1)},
        )
        assert confirmed.status_code == 302
        assert confirmed.headers["Location"] == detail_url
        task.refresh_from_db()
        assert task.state == TaskState.QUEUED

    def test_retry_confirmation_cancel_preserves_changelist_context(self) -> None:
        admin_obj = _task_admin()
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-cancel-context",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        changelist_url = reverse("admin:django_ray_raytaskexecution_changelist")
        request = _retry_request(path=f"{changelist_url}?state__exact=FAILED&q=invoice&page=2")

        response = admin_obj.retry_tasks(
            request,
            RayTaskExecution.objects.filter(pk=task.pk),
        )

        assert isinstance(response, TemplateResponse)
        response.render()
        context = _retry_confirmation_context(response)
        assert context["changelist_url"] == (
            f"{changelist_url}?state__exact=FAILED&q=invoice&page=2"
        )
        assert (
            f'href="{changelist_url}?state__exact=FAILED&amp;q=invoice&amp;page=2"'
            in response.content.decode()
        )

    def test_retry_confirmation_fails_closed_without_session_or_snapshot(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, message: messages.append(str(message)),
        )
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-confirmation-session-snapshot",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        queryset = RayTaskExecution.objects.filter(pk=task.pk)
        invalid_session = _retry_request()
        invalid_session.session = {"django_ray_admin_retry_confirmation_nonce_v1": "invalid"}

        with pytest.raises(PermissionDenied):
            admin_obj.retry_tasks(invalid_session, queryset)

        monkeypatch.setattr("django_ray.admin._admin_retry_snapshot", lambda _queryset: [])
        assert admin_obj.retry_tasks(_retry_request(), queryset) is None
        assert messages == ["No failed, lost, or expired tasks found in selection."]
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_retry_confirmation_is_actor_and_session_bound_and_stale_fails_closed(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[tuple[str, int | None]] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, msg, level=None: messages.append((str(msg), level)),
        )
        first_actor = get_user_model().objects.create_superuser(
            username="retry-confirmation-first-actor"
        )
        second_actor = get_user_model().objects.create_superuser(
            username="retry-confirmation-second-actor"
        )
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-confirmation-fence-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=3,
            execution_generation=7,
            workflow_run_id=uuid4(),
            error_message="do-not-render-this-error",
        )
        queryset = RayTaskExecution.objects.filter(pk=task.pk)

        first_request = _retry_request(
            user=first_actor,
            session_nonce="f" * 43,
        )
        response = admin_obj.retry_tasks(first_request, queryset)
        assert isinstance(response, TemplateResponse)
        response.render()
        context = _retry_confirmation_context(response)
        assert context["known_workflow_count"] == 1
        assert "do-not-render-this-error" not in response.content.decode()

        other_actor_request = _retry_request(
            data={
                "post": "yes",
                "retry_confirmation_token": context["confirmation_token"],
            },
            user=second_actor,
            session_nonce="f" * 43,
        )
        assert admin_obj.retry_tasks(other_actor_request, queryset) is None
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 3
        assert not TaskAttempt.objects.filter(execution=task).exists()

        other_session_request = _retry_request(
            data={
                "post": "yes",
                "retry_confirmation_token": context["confirmation_token"],
            },
            user=first_actor,
            session_nonce="s" * 43,
        )
        assert admin_obj.retry_tasks(other_session_request, queryset) is None
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 3
        assert not TaskAttempt.objects.filter(execution=task).exists()

        fresh_request = _retry_request(
            user=first_actor,
            session_nonce="n" * 43,
        )
        fresh_response = admin_obj.retry_tasks(fresh_request, queryset)
        assert isinstance(fresh_response, TemplateResponse)
        fresh_context = _retry_confirmation_context(fresh_response)
        RayTaskExecution.objects.filter(pk=task.pk).update(
            attempt_number=4,
            execution_generation=8,
        )
        stale_request = _retry_request(
            data={
                "post": "yes",
                "retry_confirmation_token": fresh_context["confirmation_token"],
            },
            user=first_actor,
            session_nonce="n" * 43,
        )
        assert admin_obj.retry_tasks(stale_request, queryset) is None
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 4
        assert task.execution_generation == 8
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert len(messages) == 3
        assert all("No tasks were queued" in message for message, _level in messages)

    def test_retry_confirmation_requires_admin_change_permission(self) -> None:
        admin_obj = _task_admin()
        actor = get_user_model().objects.create_user(
            username="retry-confirmation-no-change-permission",
            is_staff=True,
        )
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-confirmation-permission-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )

        with pytest.raises(PermissionDenied):
            admin_obj.retry_tasks(
                _retry_request(user=actor),
                RayTaskExecution.objects.filter(pk=task.pk),
            )

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert not TaskAttempt.objects.filter(execution=task).exists()

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_retry_confirmation_requires_csrf_for_both_posts(self) -> None:
        actor = get_user_model().objects.create_superuser(username="retry-confirmation-csrf-actor")
        task = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-confirmation-csrf-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        changelist_url = reverse("admin:django_ray_raytaskexecution_changelist")
        selection = {
            "action": "retry_tasks",
            "_selected_action": [str(task.pk)],
        }
        client = Client(enforce_csrf_checks=True)
        client.force_login(actor)

        assert client.post(changelist_url, selection).status_code == 403
        changelist = client.get(changelist_url)
        assert changelist.status_code == 200
        csrf_match = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"',
            changelist.content.decode(),
        )
        assert csrf_match is not None
        confirmation = client.post(
            changelist_url,
            {**selection, "csrfmiddlewaretoken": csrf_match.group(1)},
        )
        assert confirmation.status_code == 200
        confirmation_token_match = re.search(
            r'name="retry_confirmation_token"[^>]*value="([^"]+)"',
            confirmation.content.decode(),
            re.DOTALL,
        )
        assert confirmation_token_match is not None
        confirmed_selection = {
            **selection,
            "post": "yes",
            "retry_confirmation_token": confirmation_token_match.group(1),
        }

        assert client.post(changelist_url, confirmed_selection).status_code == 403
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        confirmed = client.post(
            changelist_url,
            {**confirmed_selection, "csrfmiddlewaretoken": csrf_match.group(1)},
        )
        assert confirmed.status_code == 302
        task.refresh_from_db()
        assert task.state == TaskState.QUEUED

    @pytest.mark.parametrize(
        ("field", "replacement"),
        [
            ("workflow_run_id", "00000000-0000-0000-0000-000000000292"),
            ("workflow_plan_fingerprint", "sha256:" + ("2" * 64)),
        ],
    )
    def test_retry_confirmation_binds_exact_workflow_identity_without_rendering_it(
        self,
        monkeypatch,
        field: str,
        replacement: str,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, message, level=None: messages.append(str(message)),
        )
        run_id = "00000000-0000-0000-0000-000000000291"
        fingerprint = "sha256:" + ("1" * 64)
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-confirmation-workflow-fence-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=5,
            workflow_run_id=run_id,
            workflow_plan_fingerprint=fingerprint,
        )
        queryset = RayTaskExecution.objects.filter(pk=task.pk)
        response = _retry_confirmation(admin_obj, queryset)

        content = response.content.decode()
        assert run_id not in content
        assert fingerprint not in content
        RayTaskExecution.objects.filter(pk=task.pk).update(**{field: replacement})

        assert admin_obj.retry_tasks(_confirmed_retry_request(response), queryset) is None
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 2
        assert task.execution_generation == 5
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert messages == [
            "Retry confirmation expired or the selected tasks changed. Review their "
            "current state and confirm again. No tasks were queued."
        ]

    def test_retry_confirmation_accepts_django_secret_key_fallback(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        monkeypatch.setattr(admin_obj, "message_user", lambda request, message: None)
        task = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-confirmation-key-rotation-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        queryset = RayTaskExecution.objects.filter(pk=task.pk)
        previous_key = "previous-retry-confirmation-secret-key"
        active_key = "active-retry-confirmation-secret-key"

        with override_settings(SECRET_KEY=previous_key, SECRET_KEY_FALLBACKS=[]):
            response = _retry_confirmation(admin_obj, queryset)
        with override_settings(
            SECRET_KEY=active_key,
            SECRET_KEY_FALLBACKS=[previous_key],
        ):
            assert admin_obj.retry_tasks(_confirmed_retry_request(response), queryset) is None

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2

    def test_retry_confirmation_rejects_tampering_and_expiry(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, message, level=None: messages.append(str(message)),
        )
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-confirmation-expiry-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        queryset = RayTaskExecution.objects.filter(pk=task.pk)
        clock = [1_000.0]
        monkeypatch.setattr(django_signing.time, "time", lambda: clock[0])
        response = _retry_confirmation(admin_obj, queryset)
        context = _retry_confirmation_context(response)
        token = context["confirmation_token"]
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

        tampered_request = _retry_request(
            data={
                "post": "yes",
                "retry_confirmation_token": tampered,
            }
        )
        assert admin_obj.retry_tasks(tampered_request, queryset) is None
        clock[0] += 15 * 60 + 1
        assert admin_obj.retry_tasks(_confirmed_retry_request(response), queryset) is None

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert len(messages) == 2
        assert all("No tasks were queued" in message for message in messages)

    def test_retry_confirmation_state_fence_survives_the_post_validation_race(
        self,
        monkeypatch,
    ) -> None:
        from django_ray.lifecycle import request_task_retry as locked_request_task_retry

        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, message: messages.append(str(message)),
        )
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-confirmation-state-race-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=3,
            execution_generation=7,
        )
        queryset = RayTaskExecution.objects.filter(pk=task.pk)
        response = _retry_confirmation(admin_obj, queryset)

        def change_state_before_row_lock(execution_id: int | str, **kwargs: Any) -> Any:
            RayTaskExecution.objects.filter(pk=execution_id).update(state=TaskState.CANCELLED)
            return locked_request_task_retry(execution_id, **kwargs)

        monkeypatch.setattr("django_ray.admin.request_task_retry", change_state_before_row_lock)
        assert admin_obj.retry_tasks(_confirmed_retry_request(response), queryset) is None

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.attempt_number == 3
        assert task.execution_generation == 7
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert messages == [
            "Queued 0 task(s) for retry. Skipped 1 task(s) because they changed after confirmation."
        ]

    @pytest.mark.parametrize(
        ("field", "replacement"),
        [
            ("workflow_run_id", "00000000-0000-0000-0000-000000000294"),
            ("workflow_plan_fingerprint", "sha256:" + ("4" * 64)),
        ],
    )
    def test_retry_confirmation_workflow_fence_survives_the_post_validation_race(
        self,
        monkeypatch,
        field: str,
        replacement: str,
    ) -> None:
        from django_ray.lifecycle import request_task_retry as locked_request_task_retry

        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, message: messages.append(str(message)),
        )
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-confirmation-workflow-race-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=3,
            execution_generation=7,
            workflow_run_id="00000000-0000-0000-0000-000000000293",
            workflow_plan_fingerprint="sha256:" + ("3" * 64),
        )
        queryset = RayTaskExecution.objects.filter(pk=task.pk)
        response = _retry_confirmation(admin_obj, queryset)

        def change_workflow_before_row_lock(execution_id: int | str, **kwargs: Any) -> Any:
            RayTaskExecution.objects.filter(pk=execution_id).update(**{field: replacement})
            return locked_request_task_retry(execution_id, **kwargs)

        monkeypatch.setattr("django_ray.admin.request_task_retry", change_workflow_before_row_lock)
        assert admin_obj.retry_tasks(_confirmed_retry_request(response), queryset) is None

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 3
        assert task.execution_generation == 7
        assert str(getattr(task, field)) == replacement
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert messages == [
            "Queued 0 task(s) for retry. Skipped 1 task(s) because they changed after confirmation."
        ]

    def test_retry_confirmation_bounds_bulk_selection(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[tuple[str, int | None]] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, msg, level=None: messages.append((str(msg), level)),
        )
        RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"admin-retry-bounded-{index:03d}",
                    callable_path="testproject.tasks.failing_task",
                    state=TaskState.FAILED,
                )
                for index in range(ADMIN_RETRY_CONFIRMATION_MAX_TASKS + 1)
            ]
        )
        queryset = RayTaskExecution.objects.filter(task_id__startswith="admin-retry-bounded-")

        assert admin_obj.retry_tasks(_retry_request(), queryset) is None
        assert queryset.filter(state=TaskState.FAILED).count() == (
            ADMIN_RETRY_CONFIRMATION_MAX_TASKS + 1
        )
        assert messages == [
            (
                "Retry at most 100 failed, lost, or expired tasks at once. No tasks were queued.",
                django_messages.ERROR,
            )
        ]

    def test_retry_tasks_skips_corrupt_runtime_env_without_aborting_selection(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, msg: messages.append(str(msg)),
        )
        corrupt = RayTaskExecution.objects.create(
            task_id="admin-retry-runtime-env-corrupt-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=4,
            error_message="original failure",
            runtime_env_json=('{"env_vars":{"VALUE":"arbitrary-customer-marker-7cf3"}}'),
            runtime_env_hash="0" * 64,
        )
        valid = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-runtime-env-valid-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.LOST,
            attempt_number=2,
            execution_generation=4,
            error_message="worker lost",
        )

        response = _retry_confirmation(
            admin_obj,
            RayTaskExecution.objects.filter(pk__in=[corrupt.pk, valid.pk]),
        )
        context = _retry_confirmation_context(response)
        admin_obj.retry_tasks(
            _confirmed_retry_request(response),
            RayTaskExecution.objects.filter(pk__in=context["selected_ids"]),
        )

        corrupt.refresh_from_db()
        valid.refresh_from_db()
        assert corrupt.state == TaskState.FAILED
        assert corrupt.attempt_number == 2
        assert corrupt.execution_generation == 4
        assert corrupt.error_message == "original failure"
        assert not TaskAttempt.objects.filter(execution=corrupt).exists()
        assert valid.state == TaskState.QUEUED
        assert valid.attempt_number == 3
        assert messages == [
            "Queued 1 task(s) for retry. Skipped 1 task(s) because their persisted "
            "RuntimeEnv snapshots failed validation."
        ]
        assert "arbitrary-customer-marker-7cf3" not in messages[0]

    def test_retry_tasks_noop_when_nothing_retryable(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        task = RayTaskExecution.objects.create(
            task_id="admin-retry-004",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="[]",
            kwargs_json="{}",
        )

        admin_obj.retry_tasks(_retry_request(), RayTaskExecution.objects.filter(pk=task.pk))

        assert messages[-1] == "No failed, lost, or expired tasks found in selection."

    def test_retry_and_cancel_actions_report_unsupported_protocol_without_mutation(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, message: messages.append(str(message)),
        )
        task = RayTaskExecution.objects.create(
            task_id="admin-unsupported-protocol-actions",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            execution_protocol_version=2,
            attempt_number=3,
            execution_generation=7,
            error_message="retained failure",
        )
        before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
        queryset = RayTaskExecution.objects.filter(pk=task.pk)

        confirmation = _retry_confirmation(admin_obj, queryset)
        assert admin_obj.retry_tasks(_confirmed_retry_request(confirmation), queryset) is None
        admin_obj.cancel_tasks(_retry_request(), queryset)

        assert messages == [
            (
                "Queued 0 task(s) for retry. Skipped 1 task(s) because this django-ray "
                "build does not support their execution protocol."
            ),
            (
                "No selected tasks accepted cancellation. Skipped 1 task(s) because this "
                "django-ray build does not support their execution protocol."
            ),
        ]
        assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_cancel_tasks_handles_queued_and_running_paths(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        queued = RayTaskExecution.objects.create(
            task_id="admin-cancel-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
        )
        running_ray_job = RayTaskExecution.objects.create(
            task_id="admin-cancel-002",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_cancel_admin",
            ray_address="ray://cluster:10001",
            started_at=datetime.now(UTC) - timedelta(seconds=5),
            args_json="[]",
            kwargs_json="{}",
        )
        running_core = RayTaskExecution.objects.create(
            task_id="admin-cancel-003",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.RUNNING,
            ray_job_id="02000000:abcdef",
            args_json="[]",
            kwargs_json="{}",
        )

        qs = RayTaskExecution.objects.filter(
            pk__in=[queued.pk, running_ray_job.pk, running_core.pk]
        )
        admin_obj.cancel_tasks(_retry_request(), qs)

        queued.refresh_from_db()
        running_ray_job.refresh_from_db()
        running_core.refresh_from_db()

        assert queued.state == TaskState.CANCELLED
        assert queued.finished_at is not None
        assert running_ray_job.state == TaskState.CANCELLING
        assert running_core.state == TaskState.CANCELLING
        assert TaskAttempt.objects.get(execution=queued).state == TaskState.CANCELLED
        assert messages[-1] == (
            "Accepted cancellation for 3 task(s). "
            "Workers will attempt best-effort interruption for running work."
        )

    def test_cancel_tasks_noop_when_nothing_cancellable(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        failed = RayTaskExecution.objects.create(
            task_id="admin-cancel-004",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            args_json="[]",
            kwargs_json="{}",
        )

        admin_obj.cancel_tasks(_retry_request(), RayTaskExecution.objects.filter(pk=failed.pk))
        assert messages[-1] == "No selected tasks accepted cancellation."

    def test_cancel_tasks_reports_duplicate_request_as_noop(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        task = RayTaskExecution.objects.create(
            task_id="admin-cancel-duplicate-001",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.CANCELLING,
            args_json="[]",
            kwargs_json="{}",
        )

        admin_obj.cancel_tasks(_retry_request(), RayTaskExecution.objects.filter(pk=task.pk))

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert messages[-1] == "No selected tasks accepted cancellation."


@pytest.mark.django_db
class TestTaskAttemptAdmin:
    def test_attempt_history_cannot_be_added_changed_or_deleted(self) -> None:
        admin_obj = _attempt_admin()
        request = _request()
        inline = TaskAttemptInline(TaskAttempt, admin.site)
        protocol_fields = {
            "execution_protocol_version",
            "managed_with_django_ray_version",
            "executor_django_ray_version",
        }

        assert admin_obj.has_add_permission(request) is False
        assert admin_obj.has_change_permission(request) is False
        assert admin_obj.has_delete_permission(request) is False
        assert protocol_fields <= set(admin_obj.fields)
        assert protocol_fields <= set(admin_obj.readonly_fields)
        assert ExecutionProtocolFilter in admin_obj.list_filter
        assert inline.has_add_permission(request) is False
        assert inline.has_change_permission(request) is False
        assert inline.has_delete_permission(request) is False
        assert inline.can_delete is False
        assert inline.show_title is False
        assert inline.show_change_link is False
        assert inline.fields == (
            "attempt_detail_link",
            "state_display",
            "started_display",
            "finished_display",
            "error_summary",
        )
        assert inline.readonly_fields == inline.fields

    def test_attempt_inline_uses_one_compact_detail_link(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-link-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.SUCCEEDED,
        )
        inline = TaskAttemptInline(TaskAttempt, admin.site)

        rendered = str(inline.attempt_detail_link(attempt))

        assert rendered == (
            f'<a href="{reverse("admin:django_ray_taskattempt_change", args=[attempt.pk])}">#2</a>'
        )
        assert inline.attempt_detail_link.short_description == "Attempt"

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_attempt_detail_links_full_progress_to_its_pinned_graph(self, client) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-graph-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-graph-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            attempt_number=1,
            execution_generation=1,
            workflow_run_id="35200000-0000-4000-8000-000000000001",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            workflow_progress_summary_json=serialize_workflow_progress_summary(
                workflow_progress_summary(
                    execution,
                    published_detail=True,
                    state="FAILED",
                )
            ),
        )
        client.force_login(user)
        graph_url = (
            reverse(
                "admin:django_ray_raytaskexecution_workflow_graph",
                args=[execution.pk],
            )
            + "?attempt_number=1"
        )

        detail = client.get(
            reverse(
                "admin:django_ray_taskattempt_change",
                args=[attempt.pk],
            )
        )

        execution_graph_anchor = (
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
            + "#workflow-graph-attempt-1"
        )
        content = detail.content.decode("utf-8")
        assert detail.status_code == 200
        assert "Workflow graph" in content
        assert f'href="{execution_graph_anchor}"' in content
        assert f'href="{graph_url}"' in content
        assert "Open graph for attempt #1" in content
        assert "Graph JSON" in content
        assert "workflow_progress_summary_json" not in content

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_parent_renders_exact_collapsed_graphs_for_archived_failed_attempts(
        self,
        client,
    ) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-inline-graphs-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-inline-graphs-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            attempt_number=3,
            execution_generation=3,
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.FAILED,
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=3,
            state=TaskState.SUCCEEDED,
        )
        client.force_login(user)

        response = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
        )

        content = response.content.decode("utf-8")
        endpoint_names = (
            "workflow_graph",
            "workflow_topology_nodes",
            "workflow_topology_edges",
            "workflow_node_details",
            "workflow_node_detail",
        )
        endpoint_urls = {
            name: reverse(
                f"admin:django_ray_raytaskexecution_{name}",
                args=[execution.pk],
            )
            for name in endpoint_names
        }
        assert response.status_code == 200
        assert "Attempt execution graphs" in content
        assert content.count("data-workflow-attempt-graph=") == 2
        document = parse_html(content)
        workflow_disclosure = next(
            element
            for element in _walk_html(document)
            if dict(element.attributes).get("id") == "django-ray-workflow-diagnostics"
        )
        assert workflow_disclosure.name == "details"
        assert "open" not in dict(workflow_disclosure.attributes)
        attempt_stack = next(
            element
            for element in _walk_html(workflow_disclosure)
            if "data-workflow-attempt-graphs" in dict(element.attributes)
        )
        stack_children = [child for child in attempt_stack.children if isinstance(child, Element)]
        ordered_attempts = [
            dict(child.attributes).get("data-workflow-graph-attempt")
            if child.name == "details"
            else "current"
            for child in stack_children
            if child.name == "details" or "data-workflow-current-graph" in dict(child.attributes)
        ]
        assert ordered_attempts == ["1", "2", "current"]
        current_mount = next(
            child
            for child in stack_children
            if "data-workflow-current-graph" in dict(child.attributes)
        )
        current_attributes = dict(current_mount.attributes)
        assert current_attributes["data-current-attempt-number"] == "3"
        assert current_attributes["data-current-attempt-state"] == "SUCCEEDED"
        assert "hidden" in current_attributes
        for attempt_number in (1, 2):
            assert f'data-workflow-attempt-graph="{attempt_number}"' in content
            assert f'data-pinned-attempt-number="{attempt_number}"' in content
            assert 'data-current-attempt-number="3"' in content
            for endpoint_url in endpoint_urls.values():
                assert f'="{endpoint_url}?attempt_number={attempt_number}"' in content
            panel_start = content.index(f'data-workflow-attempt-graph="{attempt_number}"')
            opening_tag_start = content.rfind("<details", 0, panel_start)
            opening_tag = content[opening_tag_start : content.index(">", panel_start)]
            assert " open" not in opening_tag
            panel = next(
                child
                for child in stack_children
                if dict(child.attributes).get("data-workflow-attempt-graph") == str(attempt_number)
            )
            panel_attributes = dict(panel.attributes)
            title_id = f"django-ray-workflow-attempt-{attempt_number}-title"
            assert panel_attributes["aria-labelledby"] == title_id
            panel_elements = list(_walk_html(panel))
            assert panel_elements[1].name == "summary"
            assert any(dict(element.attributes).get("id") == title_id for element in panel_elements)
        assert 'data-workflow-attempt-graph="3"' not in content
        assert 'data-workflow-graph-attempt="3"' not in content
        assert f'data-graph-url="{endpoint_urls["workflow_graph"]}?attempt_number=3"' in content
        assert "workflow_progress_summary_json" not in content

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_archived_graph_panel_list_fails_closed_at_fixed_bound(
        self,
        monkeypatch,
        client,
    ) -> None:
        monkeypatch.setattr(
            "django_ray.admin.ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS",
            2,
        )
        user = get_user_model().objects.create_superuser(
            username="attempt-inline-graph-bound-admin"
        )
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-inline-graph-bound-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            attempt_number=4,
        )
        for attempt_number in range(1, 4):
            TaskAttempt.objects.create(
                execution=execution,
                attempt_number=attempt_number,
                state=TaskState.FAILED,
            )
        client.force_login(user)

        response = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
        )

        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert content.count("data-workflow-attempt-graph=") == 2
        assert 'data-workflow-attempt-graph="1"' not in content
        assert 'data-workflow-attempt-graph="2"' in content
        assert 'data-workflow-attempt-graph="3"' in content
        assert "2-panel safety limit" in content

    def test_diagnostics_are_redacted_bounded_and_not_raw_model_fields(self, settings) -> None:
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-redaction-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="password=error-secret",
            error_traceback="safe trace " + ("x" * (ADMIN_DIAGNOSTIC_MAX_CHARS + 100)),
            result_data='{"password":"result-secret"}',
            result_reference="password=result-reference-secret",
        )
        admin_obj = _attempt_admin()

        rendered = [
            admin_obj.error_message_display(attempt),
            admin_obj.error_traceback_display(attempt),
            admin_obj.result_data_display(attempt),
            admin_obj.result_reference_display(attempt),
        ]

        assert "error-secret" not in rendered[0]
        assert "result-secret" not in rendered[2]
        assert "result-reference-secret" not in rendered[3]
        assert all(len(rendered[index]) <= ADMIN_DIAGNOSTIC_MAX_CHARS for index in (0, 2, 3))
        assert len(rendered[1]) <= ADMIN_DIAGNOSTIC_MAX_CHARS + 100
        assert "... [truncated]" in rendered[1]
        assert "error_message" not in admin_obj.fields
        assert "error_traceback" not in admin_obj.fields
        assert "result_data" not in admin_obj.fields
        assert "result_reference" not in admin_obj.fields

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_legacy_traceback_is_inert_line_preserving_and_safely_wrapped(
        self,
        client,
        settings,
    ) -> None:
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "TASK_ATTEMPT_ADMIN_MODE": "standalone",
        }
        user = get_user_model().objects.create_superuser(username="terminal-traceback-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-terminal-traceback-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_traceback=(
                "\x1b[36mray::django_ray:task()\x1b[39m\r\n"
                'File "/app/task.py", line 1\n'
                "<script>unsafe</script>"
            ),
        )
        client.force_login(user)

        response = client.get(reverse("admin:django_ray_taskattempt_change", args=[attempt.pk]))

        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert "django_ray/admin/diagnostics.css" in content
        assert "django-ray-diagnostic" in content
        assert "ray::django_ray:task()\nFile" in content
        assert "&lt;script&gt;unsafe&lt;/script&gt;" in content
        assert "<script>unsafe</script>" not in content
        assert "\x1b" not in content

    def test_empty_attempt_diagnostics_render_as_placeholders(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-empty-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.SUCCEEDED,
        )
        admin_obj = _attempt_admin()

        assert admin_obj.error_message_display(attempt) == "-"
        assert admin_obj.error_traceback_display(attempt) == "-"
        assert admin_obj.result_data_display(attempt) == "-"
        assert admin_obj.result_reference_display(attempt) == "-"
        assert admin_obj.workflow_graph_link(attempt) == "-"

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_default_index_hides_attempt_module_but_detail_remains_linked(
        self,
        client,
    ) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-inline-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-inline-001",
            callable_path="testproject.tasks.add_numbers",
        )
        second = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.FAILED,
            error_message="second-attempt-marker",
        )
        first = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="first-attempt-marker",
        )
        client.force_login(user)

        index = client.get(reverse("admin:index"))
        change = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
        )
        first_detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[first.pk],
        )
        second_detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[second.pk],
        )
        detail = client.get(first_detail_url)
        attempt_list = client.get(reverse("admin:django_ray_taskattempt_changelist"))

        index_html = index.content.decode("utf-8")
        change_html = change.content.decode("utf-8")
        assert index.status_code == 200
        assert reverse("admin:django_ray_taskattempt_changelist") not in index_html
        assert change.status_code == 200
        assert "Attempt history" in change_html
        assert change_html.count(first_detail_url) == 1
        assert change_html.count(second_detail_url) == 1
        assert change_html.index("second-attempt-marker") < change_html.index(
            "first-attempt-marker"
        )
        assert detail.status_code == 200
        assert attempt_list.status_code == 200

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_parent_permission_does_not_expose_attempts_without_child_permission(
        self,
        client,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-permissions-001",
            callable_path="testproject.tasks.add_numbers",
            attempt_number=2,
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
        )
        user = get_user_model().objects.create_user(
            username="attempt-parent-viewer",
            is_staff=True,
        )
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="django_ray",
                codename="view_raytaskexecution",
            )
        )
        client.force_login(user)
        change_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        )
        detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[attempt.pk],
        )

        parent_only = client.get(change_url)

        assert parent_only.status_code == 200
        assert detail_url not in parent_only.content.decode("utf-8")
        assert 'data-workflow-attempt-graph="' not in parent_only.content.decode("utf-8")

        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="django_ray",
                codename="view_taskattempt",
            )
        )
        with_child = client.get(change_url)

        assert with_child.status_code == 200
        with_child_content = with_child.content.decode("utf-8")
        assert detail_url in with_child_content
        assert 'data-workflow-attempt-graph="1"' in with_child_content

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_empty_attempt_history_change_form_is_usable(self, client) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-empty-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-empty-history-001",
            callable_path="testproject.tasks.add_numbers",
        )
        client.force_login(user)

        response = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
        )

        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert "Attempt history" in content
        assert "/admin/django_ray/taskattempt/" not in content
        assert 'name="attempts-0-DELETE"' not in content

    def test_inline_honors_parent_object_permission(self, monkeypatch) -> None:
        allowed = RayTaskExecution.objects.create(
            task_id="admin-attempt-parent-object-allowed",
            callable_path="testproject.tasks.add_numbers",
        )
        denied = RayTaskExecution.objects.create(
            task_id="admin-attempt-parent-object-denied",
            callable_path="testproject.tasks.add_numbers",
        )
        user = get_user_model().objects.create_user(
            username="attempt-parent-object-viewer",
            is_staff=True,
        )

        def has_perm(permission: str, obj: object | None = None) -> bool:
            if permission in {
                "django_ray.view_taskattempt",
                "django_ray.change_taskattempt",
            }:
                return obj is None
            return (
                permission
                in {
                    "django_ray.view_raytaskexecution",
                    "django_ray.change_raytaskexecution",
                }
                and obj == allowed
            )

        monkeypatch.setattr(user, "has_perm", has_perm)
        request = RequestFactory().get("/admin/")
        request.user = user
        inline = TaskAttemptInline(TaskAttempt, admin.site)

        assert inline.has_view_permission(request, allowed) is True
        assert inline.has_view_permission(request, denied) is False

    def test_attempt_detail_honors_object_specific_permission(
        self,
        monkeypatch,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-object-permission-001",
            callable_path="testproject.tasks.add_numbers",
        )
        allowed = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.SUCCEEDED,
        )
        denied = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.FAILED,
        )
        user = get_user_model().objects.create_user(
            username="attempt-object-viewer",
            is_staff=True,
        )

        def has_perm(permission: str, obj: object | None = None) -> bool:
            return (
                permission
                in {
                    "django_ray.view_taskattempt",
                    "django_ray.change_taskattempt",
                }
                and obj == allowed
            )

        monkeypatch.setattr(user, "has_perm", has_perm)
        admin_obj = _attempt_admin()
        allowed_request = RequestFactory().get(
            reverse("admin:django_ray_taskattempt_change", args=[allowed.pk])
        )
        allowed_request.user = user
        denied_request = RequestFactory().get(
            reverse("admin:django_ray_taskattempt_change", args=[denied.pk])
        )
        denied_request.user = user

        assert admin_obj.has_view_permission(allowed_request) is False
        assert admin_obj.has_view_permission(allowed_request, allowed) is True
        assert admin_obj.has_view_permission(denied_request, denied) is False
        assert admin_obj.change_view(allowed_request, str(allowed.pk)).status_code == 200
        with pytest.raises(PermissionDenied):
            admin_obj.change_view(denied_request, str(denied.pk))

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_attempt_detail_rejects_forged_mutation(self, client) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-forged-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-forged-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="original-error",
        )
        client.force_login(user)
        detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[attempt.pk],
        )

        detail = client.get(detail_url)
        forged = client.post(
            detail_url,
            {
                "attempt_number": 99,
                "state": TaskState.SUCCEEDED,
                "error_message": "forged-error",
                "_save": "Save",
            },
        )

        attempt.refresh_from_db()
        detail_html = detail.content.decode("utf-8")
        assert detail.status_code == 200
        assert 'name="_save"' not in detail_html
        assert "deletelink" not in detail_html
        assert forged.status_code == 403
        assert attempt.attempt_number == 1
        assert attempt.state == TaskState.FAILED
        assert attempt.error_message == "original-error"

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_inline_summary_is_bounded_redacted_escaped_and_payload_free(
        self,
        settings,
        client,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "ray://localhost:10001",
            "REDACT_PATTERNS": [r"password"],
        }
        user = get_user_model().objects.create_superuser(username="attempt-summary-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-summary-001",
            callable_path="testproject.tasks.add_numbers",
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="password=inline-secret",
            error_traceback="traceback-heavy-sentinel",
            result_data='"result-heavy-sentinel"',
            result_reference="reference-heavy-sentinel",
            workflow_progress_summary_json='"workflow-heavy-sentinel"',
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.FAILED,
            error_message="<script>unsafe</script>",
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=3,
            state=TaskState.FAILED,
            error_message="a" * ADMIN_ATTEMPT_INLINE_MAX_CHARS,
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=4,
            state=TaskState.FAILED,
            error_message="b" * (ADMIN_ATTEMPT_INLINE_MAX_CHARS + 1),
        )
        client.force_login(user)

        response = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
        )

        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert "inline-secret" not in content
        assert "[REDACTED]" in content
        assert "&lt;script&gt;unsafe&lt;/script&gt;" in content
        assert ("a" * ADMIN_ATTEMPT_INLINE_MAX_CHARS) in content
        assert "Open the attempt detail to view bounded diagnostics." in content
        assert ("b" * (ADMIN_ATTEMPT_INLINE_MAX_CHARS + 1)) not in content
        assert "traceback-heavy-sentinel" not in content
        assert "result-heavy-sentinel" not in content
        assert "reference-heavy-sentinel" not in content
        assert "workflow-heavy-sentinel" not in content

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_inline_uses_one_bounded_attempt_query_for_many_rows(
        self,
        client,
    ) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-query-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-query-001",
            callable_path="testproject.tasks.add_numbers",
        )
        for attempt_number in range(1, 13):
            TaskAttempt.objects.create(
                execution=execution,
                attempt_number=attempt_number,
                state=TaskState.FAILED,
                error_message=f"attempt-{attempt_number}",
                error_traceback="traceback-heavy-sentinel",
                result_data='"result-heavy-sentinel"',
                result_reference="reference-heavy-sentinel",
                workflow_progress_summary_json='"workflow-heavy-sentinel"',
            )
        client.force_login(user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(
                reverse(
                    "admin:django_ray_raytaskexecution_change",
                    args=[execution.pk],
                )
            )

        attempt_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and 'FROM "django_ray_taskattempt"' in query["sql"]
        ]
        assert response.status_code == 200
        assert len(attempt_selects) == 1
        assert "error_message" in attempt_selects[0]
        assert "error_traceback" not in attempt_selects[0]
        assert "result_data" not in attempt_selects[0]
        assert "result_reference" not in attempt_selects[0]
        assert "workflow_progress_summary_json" not in attempt_selects[0]

    @override_settings(
        MIDDLEWARE=_ADMIN_MIDDLEWARE,
        DJANGO_RAY={
            "RAY_ADDRESS": "ray://localhost:10001",
            "TASK_ATTEMPT_ADMIN_MODE": "standalone",
        },
    )
    def test_standalone_changelist_avoids_parent_join_and_diagnostics(
        self,
        client,
    ) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-list-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-list-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="error-heavy-sentinel",
            error_traceback="traceback-heavy-sentinel",
            result_data='"result-heavy-sentinel"',
            result_reference="reference-heavy-sentinel",
            workflow_progress_summary_json='"workflow-heavy-sentinel"',
        )
        client.force_login(user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse("admin:django_ray_taskattempt_changelist"))

        attempt_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and 'FROM "django_ray_taskattempt"' in query["sql"]
            and "COUNT(" not in query["sql"].upper()
        ]
        assert response.status_code == 200
        assert len(attempt_selects) == 1
        query = attempt_selects[0]
        assert "JOIN" not in query.upper()
        assert "error_message" not in query
        assert "error_traceback" not in query
        assert "result_data" not in query
        assert "result_reference" not in query
        assert "workflow_progress_summary_json" not in query
        content = response.content.decode("utf-8")
        detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[attempt.pk],
        )
        execution_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        )
        assert detail_url in content
        assert execution_url in content
        assert f'<a href="{detail_url}"><a' not in content


@pytest.mark.django_db
class TestTaskWorkerLeaseAdmin:
    """Tests for worker lease admin helper behavior."""

    def test_execution_protocol_capability_is_read_only_and_bounded(self) -> None:
        admin_obj = _lease_admin()
        protocol_fields = {
            "capability_schema_version",
            "django_ray_version",
            "min_supported_execution_protocol_version",
            "max_supported_execution_protocol_version",
            "legacy_admission_token",
        }
        fieldset_fields = {
            field for _, options in admin_obj.fieldsets for field in options.get("fields", ())
        }

        assert protocol_fields <= fieldset_fields
        assert protocol_fields <= set(admin_obj.readonly_fields)
        assert "capability_schema_version" in admin_obj.list_display
        assert "supported_execution_protocols" in admin_obj.list_display
        assert "django_ray_version" in admin_obj.list_display
        assert (
            admin_obj.supported_execution_protocols(
                SimpleNamespace(
                    min_supported_execution_protocol_version=None,
                    max_supported_execution_protocol_version=None,
                )
            )
            == "Legacy (policy-controlled)"
        )
        assert (
            admin_obj.supported_execution_protocols(
                SimpleNamespace(
                    min_supported_execution_protocol_version=1,
                    max_supported_execution_protocol_version=1,
                )
            )
            == "1"
        )
        assert (
            admin_obj.supported_execution_protocols(
                SimpleNamespace(
                    min_supported_execution_protocol_version=1,
                    max_supported_execution_protocol_version=2,
                )
            )
            == "1-2"
        )

    def test_worker_id_short_and_time_since_heartbeat(self) -> None:
        admin_obj = _lease_admin()
        lease = TaskWorkerLease.objects.create(
            worker_id="worker-1234567890abcdef",
            hostname="host-a",
            pid=1111,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=30),
            is_active=True,
        )

        assert (
            admin_obj.worker_id_short(lease)
            == '<span title="worker-1234567890abcdef">worker-12345...</span>'
        )
        assert "ago" in admin_obj.time_since_heartbeat(lease)
        assert admin_obj._is_heartbeat_expired(lease) is False

        lease.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=90)
        assert admin_obj.time_since_heartbeat(lease).endswith("m 30s ago")

        lease.last_heartbeat_at = None
        assert admin_obj.time_since_heartbeat(lease) == "Never"
        assert admin_obj.get_queryset(_request()).model is TaskWorkerLease

    def test_mark_inactive_and_delete_inactive_actions(self, monkeypatch) -> None:
        admin_obj = _lease_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        active = TaskWorkerLease.objects.create(
            worker_id="active-worker",
            hostname="host-b",
            pid=2222,
            queue_name="default",
            is_active=True,
        )
        inactive = TaskWorkerLease.objects.create(
            worker_id="inactive-worker",
            hostname="host-c",
            pid=3333,
            queue_name="default",
            is_active=False,
        )

        qs = TaskWorkerLease.objects.filter(worker_id__in=[active.worker_id, inactive.worker_id])
        admin_obj.mark_inactive(_request(), qs)
        active.refresh_from_db()
        assert active.is_active is False
        assert active.stopped_at is not None
        assert messages[-1] == "Marked 1 worker lease(s) as inactive."

        admin_obj.delete_inactive(_request(), qs)
        assert not TaskWorkerLease.objects.filter(worker_id=active.worker_id).exists()
        assert not TaskWorkerLease.objects.filter(worker_id=inactive.worker_id).exists()
        assert messages[-1] == "Deleted 2 inactive worker lease(s)."

    def test_delete_inactive_reports_only_deleted_lease_rows(self, monkeypatch) -> None:
        admin_obj = _lease_admin()
        messages: list[str] = []

        class _CascadingDeletion:
            db = "default"

            def filter(self, **_filters):
                return self

            def delete(self):
                return (
                    2,
                    {
                        TaskWorkerLease._meta.label: 1,
                        "django_ray.RayWorkerTargetCapability": 1,
                    },
                )

        monkeypatch.setattr(connection.features, "has_select_for_update", False)
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, msg: messages.append(str(msg)),
        )

        admin_obj.delete_inactive(_request(), _CascadingDeletion())

        assert messages == ["Deleted 1 inactive worker lease(s)."]

    def test_permissions_are_disabled(self) -> None:
        admin_obj = _lease_admin()
        request = _request()

        assert admin_obj.has_add_permission(request) is False
        assert admin_obj.has_change_permission(request) is False
        assert admin_obj.has_delete_permission(request) is False

    def test_only_authorized_fenced_worker_lease_actions_are_exposed(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _lease_admin()
        path = "/admin/django_ray/taskworkerlease/"
        superuser_request = RequestFactory().get(path)
        superuser_request.user = SimpleNamespace(has_perm=lambda permission, obj=None: True)

        actions = admin_obj.get_actions(superuser_request)

        assert set(actions) == {"mark_inactive", "delete_inactive"}
        assert "delete_selected" not in actions

        active = TaskWorkerLease.objects.create(
            worker_id="view-only-action-target",
            hostname="view-only-host",
            pid=6666,
            queue_name="default",
            is_active=True,
        )
        view_permission = f"{admin_obj.opts.app_label}.view_{admin_obj.opts.model_name}"
        view_only_user = SimpleNamespace(
            has_perm=lambda permission, obj=None: permission == view_permission
        )
        view_request = RequestFactory().post(
            path,
            {
                "action": "mark_inactive",
                "index": "0",
                "select_across": "0",
                "_selected_action": str(active.pk),
            },
        )
        view_request.user = view_only_user
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, message, *args, **kwargs: messages.append(str(message)),
        )

        assert admin_obj.get_actions(view_request) == {}
        assert (
            admin_obj.response_action(
                view_request,
                TaskWorkerLease.objects.all(),
            )
            is None
        )
        active.refresh_from_db()
        assert active.is_active is True
        assert messages == ["No action selected."]

    def test_lease_displays_actions_and_filter_variants(self, monkeypatch) -> None:
        admin_obj = _lease_admin()
        lease = TaskWorkerLease.objects.create(
            worker_id="lease-display-worker",
            hostname="host-d",
            pid=4444,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=2, minutes=3),
            is_active=True,
        )
        monkeypatch.setattr(admin_obj, "_is_heartbeat_expired", lambda _: False)
        assert admin_obj.is_active_display_list(lease) is True
        assert admin_obj.time_since_heartbeat(lease).endswith("h 3m ago")

        inactive = TaskWorkerLease.objects.create(
            worker_id="lease-inactive-worker",
            hostname="host-e",
            pid=5555,
            queue_name="default",
            is_active=False,
        )
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )
        admin_obj.mark_inactive(_request(), TaskWorkerLease.objects.filter(pk=inactive.pk))
        admin_obj.delete_inactive(_request(), TaskWorkerLease.objects.filter(pk=lease.pk))
        assert messages == [
            "No active leases found in selection.",
            "No inactive leases found in selection.",
        ]

        filter_obj = object.__new__(ActiveWorkerFilter)
        monkeypatch.setattr(filter_obj, "value", lambda: "inactive")
        assert list(filter_obj.lookups(_request(), admin_obj)) == [
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("all", "All"),
        ]
        assert list(filter_obj.queryset(_request(), TaskWorkerLease.objects.all())) == [inactive]
        monkeypatch.setattr(filter_obj, "value", lambda: "all")
        assert list(filter_obj.queryset(_request(), TaskWorkerLease.objects.all())) == [
            lease,
            inactive,
        ]
        monkeypatch.setattr(filter_obj, "value", lambda: None)
        assert list(filter_obj.queryset(_request(), TaskWorkerLease.objects.all())) == [lease]
        filter_obj.lookup_choices = list(filter_obj.lookups(_request(), admin_obj))

        class ChangeList:
            @staticmethod
            def get_query_string(params):
                return f"?is_active={params['is_active']}"

        choices = list(filter_obj.choices(ChangeList()))
        assert choices[0]["selected"] is True
        assert choices[0]["query_string"] == "?is_active=active"


@pytest.mark.django_db
class TestTaskExecutionProtocolPolicyAdmin:
    def test_policy_is_registered_read_only_and_not_an_activation_surface(self) -> None:
        admin_obj = _protocol_policy_admin()
        request = _request()

        assert admin.site.is_registered(TaskExecutionProtocolPolicy)
        assert not admin.site.is_registered(LegacyWorkerAdmissionToken)
        assert admin_obj.readonly_fields == admin_obj.fields
        assert set(admin_obj.fields) == {
            "singleton_key",
            "schema_version",
            "active_write_protocol_version",
            "legacy_worker_admission_enabled",
            "revision",
            "updated_at",
        }
        assert admin_obj.has_add_permission(request) is False
        assert admin_obj.has_change_permission(request) is False
        assert admin_obj.has_delete_permission(request) is False
