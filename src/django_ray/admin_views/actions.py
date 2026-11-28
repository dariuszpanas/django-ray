"""Bounded bulk cancel/retry actions, detail action views, and per-object authorization."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import quote, unquote

from django.contrib import messages
from django.contrib.admin import helpers
from django.contrib.auth import get_permission_codename
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db.models import QuerySet
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
)
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

from django_ray.admin_views.diagnostics import secure_admin_response
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    TaskRetryRequestStatus,
    request_task_cancellation,
    request_task_retry,
)
from django_ray.models import TaskState, TaskWorkerLease
from django_ray.runtime.runtime_env import RuntimeEnvSnapshotError

ADMIN_RETRY_CONFIRMATION_MAX_TASKS = 100
ADMIN_CANCEL_MAX_TASKS = 100
ADMIN_RETRY_CONFIRMATION_MAX_AGE_SECONDS = 15 * 60
ADMIN_RETRYABLE_STATES = (
    TaskState.FAILED,
    TaskState.LOST,
    TaskState.EXPIRED,
)
ADMIN_CANCELLABLE_STATES = (
    TaskState.QUEUED,
    TaskState.RUNNING,
)
ADMIN_TERMINAL_STATES = (
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.LOST,
    TaskState.EXPIRED,
)
ADMIN_RETRY_CONFIRMATION_SALT = "django_ray.admin.retry-confirmation.v1"
ADMIN_RETRY_CONFIRMATION_MAX_TOKEN_CHARS = 512
ADMIN_RETRY_CONFIRMATION_SESSION_KEY = "django_ray_admin_retry_confirmation_nonce_v1"
ADMIN_RETRY_CONFIRMATION_SESSION_NONCE = re.compile(r"[A-Za-z0-9_-]{43}")


def has_object_view_permission(request: HttpRequest, opts: Any, obj: Any) -> bool:
    """Check object-level view or change grants when global view permission is absent."""
    if obj is None:
        return False
    user = cast(Any, request).user
    return bool(
        user.has_perm(f"{opts.app_label}.{get_permission_codename('view', opts)}", obj)
        or user.has_perm(f"{opts.app_label}.{get_permission_codename('change', opts)}", obj)
    )


def has_execution_action_permission(
    model_admin: Any,
    request: HttpRequest,
    obj: Any = None,
) -> bool:
    """Authorize task lifecycle actions at both model and per-object scope."""
    if not model_admin.has_change_permission(request):
        return False
    if obj is None:
        return True
    if not model_admin.has_view_permission(request, obj):
        return False
    opts = model_admin.opts
    change_perm = f"{opts.app_label}.{get_permission_codename('change', opts)}"
    user = cast(Any, request).user
    return bool(user.has_perm(change_perm) or user.has_perm(change_perm, obj))


def has_manage_worker_leases_permission(opts: Any, request: HttpRequest) -> bool:
    """Require the model change permission for controlled lease actions."""
    codename = get_permission_codename("change", opts)
    user = cast(Any, request).user
    return bool(user.has_perm(f"{opts.app_label}.{codename}"))


def retry_detail_guidance(state: str) -> str:
    """Return human-facing guidance for the execution detail retry card."""
    if state in ADMIN_RETRYABLE_STATES:
        return (
            "Retrying creates a new attempt and runs the callable again from its "
            "entry point. Review external side effects before continuing."
        )
    if state == TaskState.SUCCEEDED:
        return (
            "Retry is unavailable because this execution succeeded. To run the work "
            "again, enqueue a new task; that preserves this result and attempt history."
        )
    if state in {TaskState.QUEUED, TaskState.RUNNING}:
        return (
            f"This page opened while the execution was {state.lower()}. "
            "After a terminal outcome, reload this page to review recovery actions. "
            "You can request best-effort cancellation below while this task is active."
        )
    if state == TaskState.CANCELLING:
        return (
            f"This page opened while the execution was {state.lower()}. "
            "After a terminal outcome, reload this page to review recovery actions."
        )
    if state == TaskState.CANCELLED:
        return (
            "The Admin retry flow is limited to failed, lost, or expired executions. "
            "To run cancelled work again, enqueue a new task."
        )
    return "Retry is unavailable for this execution state."


def admin_retry_snapshot(
    queryset: QuerySet,
    *,
    max_tasks: int = ADMIN_RETRY_CONFIRMATION_MAX_TASKS,
) -> list[dict[str, Any]]:
    """Capture a bounded, canonical snapshot of retryable executions for signing."""
    rows = queryset.order_by("pk").values_list(
        "pk",
        "state",
        "attempt_number",
        "execution_generation",
        "metadata_schema_version",
        "execution_protocol_version",
        "created_with_django_ray_version",
        "managed_with_django_ray_version",
        "executor_django_ray_version",
        "workflow_run_id",
        "workflow_plan_fingerprint",
    )[: max_tasks + 1]
    return [
        {
            "pk": str(pk),
            "state": str(state),
            "attempt_number": int(attempt_number),
            "execution_generation": int(execution_generation),
            "metadata_schema_version": int(metadata_schema_version),
            "execution_protocol_version": int(execution_protocol_version),
            "created_with_django_ray_version": (
                str(created_with_django_ray_version)
                if created_with_django_ray_version is not None
                else None
            ),
            "managed_with_django_ray_version": (
                str(managed_with_django_ray_version)
                if managed_with_django_ray_version is not None
                else None
            ),
            "executor_django_ray_version": (
                str(executor_django_ray_version)
                if executor_django_ray_version is not None
                else None
            ),
            "workflow_run_id": str(workflow_run_id) if workflow_run_id is not None else None,
            "workflow_plan_fingerprint": (
                str(workflow_plan_fingerprint) if workflow_plan_fingerprint is not None else None
            ),
            "known_workflow": bool(workflow_run_id or workflow_plan_fingerprint),
        }
        for (
            pk,
            state,
            attempt_number,
            execution_generation,
            metadata_schema_version,
            execution_protocol_version,
            created_with_django_ray_version,
            managed_with_django_ray_version,
            executor_django_ray_version,
            workflow_run_id,
            workflow_plan_fingerprint,
        ) in rows
    ]


def admin_retry_snapshot_digest(snapshot: list[dict[str, Any]]) -> str:
    """Compute a deterministic SHA-256 digest of the canonical retry snapshot."""
    canonical = json.dumps(
        snapshot,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def admin_retry_actor_fingerprint(request: HttpRequest) -> str:
    """Bind retry confirmation tokens to the authenticated Django actor."""
    user = cast(Any, request).user
    actor_id = getattr(user, "pk", None)
    model_meta = getattr(user, "_meta", None)
    model_label = getattr(model_meta, "label_lower", None)
    if (
        not getattr(user, "is_authenticated", False)
        or actor_id is None
        or not isinstance(model_label, str)
        or not model_label
    ):
        raise PermissionDenied
    canonical = json.dumps(
        [model_label, str(actor_id)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def admin_retry_session_fingerprint(
    request: HttpRequest,
    *,
    create: bool,
) -> str | None:
    """Bind retry confirmation tokens to a per-session random nonce."""
    session = getattr(request, "session", None)
    if session is None:
        return None
    nonce = session.get(ADMIN_RETRY_CONFIRMATION_SESSION_KEY)
    if nonce is None and create:
        nonce = secrets.token_urlsafe(32)
        session[ADMIN_RETRY_CONFIRMATION_SESSION_KEY] = nonce
    if (
        not isinstance(nonce, str)
        or ADMIN_RETRY_CONFIRMATION_SESSION_NONCE.fullmatch(nonce) is None
    ):
        return None
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


def admin_retry_confirmation_token(
    snapshot: list[dict[str, Any]],
    *,
    actor_fingerprint: str,
    session_fingerprint: str,
) -> str:
    """Sign a retry confirmation payload bound to actor, session, and snapshot."""
    return signing.dumps(
        {
            "version": 1,
            "actor": actor_fingerprint,
            "session": session_fingerprint,
            "count": len(snapshot),
            "digest": admin_retry_snapshot_digest(snapshot),
        },
        salt=ADMIN_RETRY_CONFIRMATION_SALT,
        compress=False,
    )


def admin_retry_confirmation_matches(
    token: str,
    snapshot: list[dict[str, Any]],
    *,
    actor_fingerprint: str,
    session_fingerprint: str,
) -> bool:
    """Verify a signed retry confirmation token in constant time."""
    if not token or len(token) > ADMIN_RETRY_CONFIRMATION_MAX_TOKEN_CHARS:
        return False
    try:
        payload = signing.loads(
            token,
            salt=ADMIN_RETRY_CONFIRMATION_SALT,
            max_age=ADMIN_RETRY_CONFIRMATION_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return False
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "actor",
        "session",
        "count",
        "digest",
    }:
        return False
    actor = payload.get("actor")
    session = payload.get("session")
    digest = payload.get("digest")
    if (
        payload.get("version") != 1
        or payload.get("count") != len(snapshot)
        or not isinstance(actor, str)
        or not isinstance(session, str)
        or not isinstance(digest, str)
    ):
        return False
    return (
        secrets.compare_digest(actor, actor_fingerprint)
        and secrets.compare_digest(session, session_fingerprint)
        and secrets.compare_digest(digest, admin_retry_snapshot_digest(snapshot))
    )


def retry_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
) -> HttpResponse:
    """Open and complete the fenced retry confirmation from one detail page."""
    if request.method != "POST":
        return secure_admin_response(HttpResponseNotAllowed(["POST"]))
    if not model_admin.has_change_permission(request):
        raise PermissionDenied

    try:
        execution_id = model_admin.model._meta.pk.to_python(unquote(object_id))
        execution = model_admin.get_queryset(request).only("pk", "state").get(pk=execution_id)
    except (ObjectDoesNotExist, ValidationError, ValueError) as error:
        raise Http404("Ray task execution was not found") from error
    if not model_admin.has_view_permission(request, execution):
        raise PermissionDenied

    admin_site = model_admin.admin_site
    opts = model_admin.opts
    detail_url = reverse(
        f"{admin_site.name}:{opts.app_label}_{opts.model_name}_change",
        args=[quote(str(execution.pk))],
        current_app=admin_site.name,
    )
    response = model_admin._retry_tasks_with_confirmation(
        request,
        model_admin.get_queryset(request).filter(pk=execution.pk),
        cancel_url=detail_url,
    )
    if response is not None:
        return cast(HttpResponse, response)
    return HttpResponseRedirect(detail_url)


def cancel_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
    *,
    cancel_fn: Callable[..., Any] = request_task_cancellation,
) -> HttpResponse:
    """Request fenced package-owned cancellation from one execution detail page."""
    if request.method != "POST":
        return secure_admin_response(HttpResponseNotAllowed(["POST"]))
    if not model_admin.has_change_permission(request):
        raise PermissionDenied

    try:
        execution_id = model_admin.model._meta.pk.to_python(unquote(object_id))
        execution = (
            model_admin.get_queryset(request)
            .only(
                "pk",
                "task_id",
                "queue_name",
                "state",
                "attempt_number",
                "execution_generation",
            )
            .get(pk=execution_id)
        )
    except (ObjectDoesNotExist, ValidationError, ValueError) as error:
        raise Http404("Ray task execution was not found") from error
    if not model_admin.has_action_permission(request, execution):
        raise PermissionDenied

    admin_site = model_admin.admin_site
    opts = model_admin.opts
    detail_url = reverse(
        f"{admin_site.name}:{opts.app_label}_{opts.model_name}_change",
        args=[quote(str(execution.pk))],
        current_app=admin_site.name,
    )
    result = cancel_fn(
        execution.pk,
        expected_attempt_number=execution.attempt_number,
        expected_execution_generation=execution.execution_generation,
    )
    if result.accepted:
        model_admin.message_user(
            request,
            (
                "Accepted cancellation for 1 task(s). "
                "Workers will attempt best-effort interruption for running work."
            ),
        )
    elif result.status is TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL:
        model_admin.message_user(
            request,
            (
                "Cancellation was not accepted because this django-ray build does "
                "not support this execution protocol."
            ),
            level=messages.WARNING,
        )
    else:
        model_admin.message_user(
            request,
            "This task did not accept cancellation in its current state.",
            level=messages.INFO,
        )
    return secure_admin_response(HttpResponseRedirect(detail_url))


def retry_tasks_with_confirmation(
    model_admin: Any,
    request: HttpRequest,
    queryset: QuerySet,
    *,
    cancel_url: str | None = None,
    snapshot_fn: Callable[[QuerySet], list[dict[str, Any]]] = admin_retry_snapshot,
    retry_fn: Callable[..., Any] = request_task_retry,
    max_tasks: int = ADMIN_RETRY_CONFIRMATION_MAX_TASKS,
) -> HttpResponse | None:
    """Share one confirmation and mutation path across list and detail controls."""
    if not model_admin.has_change_permission(request):
        raise PermissionDenied
    tasks_to_retry = queryset.filter(state__in=ADMIN_RETRYABLE_STATES)
    if not tasks_to_retry.exists():
        model_admin.message_user(
            request,
            "No failed, lost, or expired tasks found in selection.",
        )
        return None

    snapshot = snapshot_fn(tasks_to_retry)
    if not snapshot:
        model_admin.message_user(
            request,
            "No failed, lost, or expired tasks found in selection.",
        )
        return None
    if len(snapshot) > max_tasks:
        model_admin.message_user(
            request,
            (
                f"Retry at most {max_tasks} failed, lost, or expired tasks "
                "at once. No tasks were queued."
            ),
            level=messages.ERROR,
        )
        return None

    snapshot_pks = [row["pk"] for row in snapshot]
    authorized_rows = list(
        queryset.filter(pk__in=snapshot_pks).only(
            "pk",
            "task_id",
            "queue_name",
            "state",
            "attempt_number",
            "execution_generation",
        )
    )
    if len(authorized_rows) != len(snapshot_pks) or any(
        not model_admin.has_action_permission(request, execution) for execution in authorized_rows
    ):
        raise PermissionDenied

    confirmation_token = request.POST.get("retry_confirmation_token", "")
    confirmed = request.POST.get("post") == "yes"
    actor_fingerprint = admin_retry_actor_fingerprint(request)
    session_fingerprint = admin_retry_session_fingerprint(
        request,
        create=not confirmed,
    )
    if not confirmed:
        if session_fingerprint is None:
            raise PermissionDenied
        selected_count = queryset.count()
        retryable_count = len(snapshot)
        if cancel_url is None:
            cancel_url = reverse(
                f"{model_admin.admin_site.name}:"
                f"{model_admin.opts.app_label}_{model_admin.opts.model_name}_changelist"
            )
            changelist_query = request.GET.urlencode()
            if changelist_query:
                cancel_url = f"{cancel_url}?{changelist_query}"
        context = {
            **model_admin.admin_site.each_context(request),
            "title": "Confirm full task retry",
            "opts": model_admin.model._meta,
            "media": model_admin.media,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "selected_ids": [row["pk"] for row in snapshot],
            "selected_count": selected_count,
            "retryable_count": retryable_count,
            "non_retryable_count": max(selected_count - retryable_count, 0),
            "known_workflow_count": sum(int(row["known_workflow"]) for row in snapshot),
            "confirmation_token": admin_retry_confirmation_token(
                snapshot,
                actor_fingerprint=actor_fingerprint,
                session_fingerprint=session_fingerprint,
            ),
            "confirmation_max_age_minutes": (ADMIN_RETRY_CONFIRMATION_MAX_AGE_SECONDS // 60),
            "cancel_url": cancel_url,
            # Kept for template extensions written against the original bulk flow.
            "changelist_url": cancel_url,
        }
        return TemplateResponse(
            request,
            model_admin.retry_confirmation_template,
            context,
        )

    if (
        not confirmation_token
        or session_fingerprint is None
        or not admin_retry_confirmation_matches(
            confirmation_token,
            snapshot,
            actor_fingerprint=actor_fingerprint,
            session_fingerprint=session_fingerprint,
        )
    ):
        model_admin.message_user(
            request,
            (
                "Retry confirmation expired or the selected tasks changed. "
                "Review their current state and confirm again. No tasks were queued."
            ),
            level=messages.ERROR,
        )
        return None

    count = 0
    blocked = 0
    changed = 0
    unsupported = 0
    for row in snapshot:
        try:
            result = retry_fn(
                row["pk"],
                allowed_states=(row["state"],),
                expected_attempt_number=row["attempt_number"],
                expected_execution_generation=row["execution_generation"],
                expected_workflow_identity=(
                    row["workflow_run_id"],
                    row["workflow_plan_fingerprint"],
                ),
            )
        except RuntimeEnvSnapshotError:
            blocked += 1
            continue
        count += int(result.accepted)
        unsupported += int(result.status is TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL)
        changed += int(
            not result.accepted and result.status is not TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL
        )

    message = f"Queued {count} task(s) for retry."
    if changed:
        message += f" Skipped {changed} task(s) because they changed after confirmation."
    if blocked:
        message += (
            f" Skipped {blocked} task(s) because their persisted RuntimeEnv "
            "snapshots failed validation."
        )
    if unsupported:
        message += (
            f" Skipped {unsupported} task(s) because this django-ray build does not "
            "support their execution protocol."
        )
    model_admin.message_user(request, message)
    return None


def _cancel_tasks_projection_fields(model_admin: Any) -> tuple[str, ...]:
    """Keep the minimal projection unless per-object authorization hooks are customized."""
    action_fn = getattr(getattr(model_admin, "has_action_permission", None), "__func__", None)
    view_fn = getattr(getattr(model_admin, "has_view_permission", None), "__func__", None)
    default_action_perm = (
        getattr(action_fn, "__module__", None) == "django_ray.admin"
        and getattr(action_fn, "__qualname__", None)
        == "RayTaskExecutionAdmin.has_action_permission"
        and getattr(view_fn, "__module__", None) == "django_ray.admin"
        and getattr(view_fn, "__qualname__", None) == "RayTaskExecutionAdmin.has_view_permission"
    )
    if default_action_perm:
        return ("pk", "attempt_number", "execution_generation")
    return (
        "pk",
        "task_id",
        "queue_name",
        "state",
        "attempt_number",
        "execution_generation",
    )


def cancel_tasks(
    model_admin: Any,
    request: HttpRequest,
    queryset: QuerySet,
    *,
    cancel_fn: Callable[..., Any] = request_task_cancellation,
    max_tasks: int = ADMIN_CANCEL_MAX_TASKS,
) -> None:
    """Request package-owned cancellation for each authorized selection."""
    if not model_admin.has_change_permission(request):
        raise PermissionDenied
    selected = list(queryset.only(*_cancel_tasks_projection_fields(model_admin))[: max_tasks + 1])
    if len(selected) > max_tasks:
        model_admin.message_user(
            request,
            (f"Cancel at most {max_tasks} tasks at once. No tasks were cancelled."),
            level=messages.ERROR,
        )
        return
    for task in selected:
        if not model_admin.has_action_permission(request, task):
            raise PermissionDenied
    accepted_count = 0
    unsupported_count = 0
    for task in selected:
        if task.pk is None:  # pragma: no cover - querysets contain persisted rows
            continue
        result = cancel_fn(
            task.pk,
            expected_attempt_number=task.attempt_number,
            expected_execution_generation=task.execution_generation,
        )
        accepted_count += int(result.accepted)
        unsupported_count += int(
            result.status is TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL
        )
    if accepted_count:
        message = (
            f"Accepted cancellation for {accepted_count} task(s). "
            "Workers will attempt best-effort interruption for running work."
        )
    else:
        message = "No selected tasks accepted cancellation."
    if unsupported_count:
        message += (
            f" Skipped {unsupported_count} task(s) because this django-ray build does "
            "not support their execution protocol."
        )
    model_admin.message_user(request, message)


def mark_leases_inactive(
    model_admin: Any,
    request: HttpRequest,
    queryset: QuerySet,
) -> None:
    """Refuse unavailable authority without presenting a successful action."""
    from django_ray.lease_policy import LeasePolicyError

    try:
        model_admin._mark_inactive_under_policy(request, queryset)
    except LeasePolicyError:
        model_admin.message_user(
            request,
            "Worker lease policy unavailable; no leases changed.",
            level=messages.ERROR,
        )


def mark_leases_inactive_under_policy(
    model_admin: Any,
    request: HttpRequest,
    queryset: QuerySet,
) -> None:
    """Mark selected worker leases as inactive under the lease policy guard."""
    from django.db import connections

    from django_ray.lease_policy import lease_policy_guard

    active = queryset.filter(is_active=True)
    using = queryset.db
    with lease_policy_guard(using=using):
        if connections[using].features.has_select_for_update:
            # Match recovery's global lease lock order so an Admin bulk
            # action cannot deadlock with a worker adopting the same rows.
            worker_ids = list(
                active.select_for_update().order_by("worker_id").values_list("worker_id", flat=True)
            )
            count = (
                cast(Any, TaskWorkerLease)
                .objects.using(using)
                .filter(
                    worker_id__in=worker_ids,
                    is_active=True,
                )
                .update(
                    is_active=False,
                    stopped_at=timezone.now(),
                )
            )
        else:
            # SQLite's write transaction is already database-wide.
            count = active.update(
                is_active=False,
                stopped_at=timezone.now(),
            )

    if count > 0:
        model_admin.message_user(
            request,
            f"Marked {count} worker lease(s) as inactive.",
        )
    else:
        model_admin.message_user(
            request,
            "No active leases found in selection.",
        )


def delete_inactive_leases(
    model_admin: Any,
    request: HttpRequest,
    queryset: QuerySet,
) -> None:
    """Refuse unavailable authority without deleting retained lease history."""
    from django_ray.lease_policy import LeasePolicyError

    try:
        model_admin._delete_inactive_under_policy(request, queryset)
    except LeasePolicyError:
        model_admin.message_user(
            request,
            "Worker lease policy unavailable; no leases deleted.",
            level=messages.ERROR,
        )


def delete_inactive_leases_under_policy(
    model_admin: Any,
    request: HttpRequest,
    queryset: QuerySet,
) -> None:
    """Delete inactive worker leases from selected rows under the lease policy guard."""
    from django.db import connections

    from django_ray.lease_policy import lease_policy_guard

    inactive = queryset.filter(is_active=False)
    using = queryset.db
    with lease_policy_guard(using=using):
        if connections[using].features.has_select_for_update:
            # Recovery can lock one inactive source plus the current
            # worker lease. Lock every deletion target in the same global
            # order before issuing the bulk delete.
            worker_ids = list(
                inactive.select_for_update()
                .order_by("worker_id")
                .values_list("worker_id", flat=True)
            )
            _total_deleted, deleted_by_model = (
                cast(Any, TaskWorkerLease)
                .objects.using(using)
                .filter(
                    worker_id__in=worker_ids,
                    is_active=False,
                )
                .delete()
            )
        else:
            # SQLite's write transaction is already database-wide.
            _total_deleted, deleted_by_model = inactive.delete()

        deleted_count = deleted_by_model.get(cast(Any, TaskWorkerLease)._meta.label, 0)

    if deleted_count > 0:
        model_admin.message_user(
            request,
            f"Deleted {deleted_count} inactive worker lease(s).",
        )
    else:
        model_admin.message_user(
            request,
            "No inactive leases found in selection.",
        )
