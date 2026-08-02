"""Views for the django-ray sample project."""

from __future__ import annotations

from django.conf import settings
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from django_ray import __version__ as django_ray_version
from django_ray.models import RayTaskExecution, TaskState


def _task_counts() -> dict[str, int]:
    return {
        row["state"]: row["count"]
        for row in RayTaskExecution.objects.values("state").annotate(count=Count("id"))
    }


def landing_page(request: HttpRequest) -> HttpResponse:
    """Render a small dashboard for the sample project."""
    counts = _task_counts()
    context = {
        "django_ray_version": django_ray_version,
        "debug": settings.DEBUG,
        "ray_dashboard_url": getattr(settings, "RAY_DASHBOARD_URL", "http://localhost:8265"),
        "total": sum(counts.values()),
        "queued": counts.get(TaskState.QUEUED, 0),
        "running": counts.get(TaskState.RUNNING, 0),
        "succeeded": counts.get(TaskState.SUCCEEDED, 0),
        "failed": counts.get(TaskState.FAILED, 0),
        "expired": counts.get(TaskState.EXPIRED, 0),
    }
    return render(request, "testproject/landing.html", context)
