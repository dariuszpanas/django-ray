"""Reuse the retained Admin smoke assertions through the qualification transport."""

import math
import os
import time

from qualification.application.run_api import MAX_EXPLICIT_RESPONSE_BYTES, ApplicationHttp

FIXTURE_ORIGIN = "http://django-web:8000"


def read_admin_text(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
    expected_content_type: str | tuple[str, ...] | None = None,
    deadline: float | None = None,
) -> str:
    """Read one fixed-origin response without redirects, proxies or unbounded bodies."""
    if (
        os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
        or base_url != FIXTURE_ORIGIN
        or deadline is None
        or not math.isfinite(deadline)
    ):
        raise ValueError("Admin observation requires the fixed qualification service and deadline")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("Admin observation deadline expired")
    media_types = (
        frozenset({expected_content_type})
        if isinstance(expected_content_type, str)
        else frozenset(expected_content_type or ())
    )
    request = ApplicationHttp(base_url, request_timeout=min(5, remaining))
    status, body = request(
        path,
        method="GET",
        headers={"Accept": "text/html", **(headers or {})},
        response_limit=MAX_EXPLICIT_RESPONSE_BYTES,
        required_media_types=media_types,
    )
    if status != expected_status or time.monotonic() > deadline:
        raise ValueError("Admin observation status or deadline did not match")
    return body.decode("utf-8")


def observe_admin_contract(*, task_id: str, policy: str, attempt: int | None) -> dict:
    """Verify real HTML/routes, raw diagnostic presentation and retained storage."""
    from testproject.docker_smoke import _run_existing_workflow_admin_smoke

    if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
        raise ValueError("Admin observation requires disposable qualification settings")
    before = _protected_diagnostics(task_id)
    evidence = _run_existing_workflow_admin_smoke(
        base_url=FIXTURE_ORIGIN,
        task_id=task_id,
        timeout_seconds=180,
        expected_reporting_policy=policy,
        attempt_number=attempt,
        read_text=read_admin_text,
    )
    if _protected_diagnostics(task_id) != before:
        raise ValueError("Admin observation changed protected diagnostics or task history")
    return {**evidence, "diagnostics_preserved": True}


def _protected_diagnostics(task_id: str) -> tuple:
    """Compare server-side fingerprints without exporting protected raw diagnostics."""
    from django.db.models.functions import MD5

    from django_ray.models import RayTaskExecution, TaskAttempt

    current = list(
        RayTaskExecution.objects.filter(task_id=task_id)
        .annotate(message_digest=MD5("error_message"), traceback_digest=MD5("error_traceback"))
        .values_list(
            "pk",
            "state",
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "message_digest",
            "traceback_digest",
        )[:2]
    )
    if len(current) != 1:
        raise ValueError("Admin observation has no unique durable task")
    attempts = list(
        TaskAttempt.objects.filter(execution_id=current[0][0])
        .annotate(message_digest=MD5("error_message"), traceback_digest=MD5("error_traceback"))
        .order_by("attempt_number")
        .values_list("pk", "state", "attempt_number", "message_digest", "traceback_digest")[:4]
    )
    if not attempts or len(attempts) > 3:
        raise ValueError("Admin observation has unexpected fixture history")
    return tuple(current), tuple(attempts)
