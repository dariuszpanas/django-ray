"""Views for the django-ray sample project."""

from __future__ import annotations

from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from django_ray import __version__ as django_ray_version
from django_ray.models import RayTaskExecution, TaskState
from testproject.api_boundary import ApiTokenAuth


def _validated_ray_dashboard_url() -> str | None:
    """Return a validated HTTP(S) Ray dashboard base URL, or None when unsafe/unset."""
    value = getattr(settings, "RAY_DASHBOARD_URL", None)
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or "\\" in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value.rstrip("/")


def _landing_page_requires_auth() -> bool:
    """Require authentication when DEBUG is disabled or production mode is active."""
    if getattr(settings, "DJANGO_ALLOW_UNAUTHENTICATED_SAMPLE_SURFACES", False):
        return False
    return not settings.DEBUG or getattr(settings, "DEPLOYMENT_MODE", "demo") == "production"


def _has_valid_operator_bearer(request: HttpRequest) -> bool:
    """Validate an operator bearer token without touching the database."""
    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    if not isinstance(authorization, str):
        return False
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return False
    return ApiTokenAuth().authenticate(request, token) is not None


def _task_counts() -> dict[str, int]:
    return {
        row["state"]: row["count"]
        for row in RayTaskExecution.objects.filter(state__in=TaskState.values)
        .values("state")
        .annotate(count=Count("id"))
    }


def landing_page(request: HttpRequest) -> HttpResponse:
    """Render a small dashboard for the sample project."""
    if _landing_page_requires_auth():
        authorized = _has_valid_operator_bearer(request)
        if not authorized:
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                if user.is_active and user.is_staff:
                    authorized = True
                else:
                    forbidden = HttpResponse(
                        "Forbidden: staff or operator authentication is required.",
                        status=403,
                        content_type="text/plain; charset=utf-8",
                    )
                    forbidden["Cache-Control"] = "no-store"
                    forbidden["X-Content-Type-Options"] = "nosniff"
                    return forbidden
        if not authorized:
            unauthorized = HttpResponse(
                "Authentication required. Sign in at /admin/login/ or supply a valid "
                "operator Bearer token.",
                status=401,
                content_type="text/plain; charset=utf-8",
            )
            unauthorized["WWW-Authenticate"] = "Bearer"
            unauthorized["Cache-Control"] = "no-store"
            unauthorized["X-Content-Type-Options"] = "nosniff"
            return unauthorized

    counts = _task_counts()
    queued = counts.get(TaskState.QUEUED, 0)
    running = counts.get(TaskState.RUNNING, 0)
    succeeded = counts.get(TaskState.SUCCEEDED, 0)
    failed = counts.get(TaskState.FAILED, 0)
    expired = counts.get(TaskState.EXPIRED, 0)
    cancelled = counts.get(TaskState.CANCELLED, 0)
    cancelling = counts.get(TaskState.CANCELLING, 0)
    lost = counts.get(TaskState.LOST, 0)
    other = cancelled + cancelling + lost
    total = queued + running + succeeded + failed + expired + other

    context = {
        "django_ray_version": django_ray_version,
        "debug": settings.DEBUG,
        "ray_dashboard_url": _validated_ray_dashboard_url(),
        "total": total,
        "queued": queued,
        "running": running,
        "succeeded": succeeded,
        "failed": failed,
        "expired": expired,
        "cancelled": cancelled,
        "cancelling": cancelling,
        "lost": lost,
        "other": other,
    }
    response = render(request, "testproject/landing.html", context)
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
