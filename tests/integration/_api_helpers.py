"""Shared client fixtures and helpers for Django Ninja API integration tests."""

from __future__ import annotations

from django.test import Client
from django.utils import timezone

from testproject.api_boundary import is_demo_route
from testproject.models import SampleAdmissionBudget


def build_demo_client(settings) -> Client:
    """Explicitly opted-in demo client for existing positive route coverage."""
    token = settings.DJANGO_API_TOKEN
    assert isinstance(token, str) and token
    settings.DEPLOYMENT_MODE = "demo"
    settings.DJANGO_DEMO_WORKLOADS_ENABLED = True
    settings.DJANGO_DEMO_TOKEN = "dedicated-demo-route-test-credential"

    SampleAdmissionBudget.objects.get_or_create(
        pk=1, defaults={"window_started_at": timezone.now()}
    )

    class DemoClient(Client):
        def post(self, path, *args, **kwargs):
            if is_demo_route(path.split("?", 1)[0]):
                kwargs.setdefault("HTTP_AUTHORIZATION", f"Bearer {settings.DJANGO_DEMO_TOKEN}")
            return super().post(path, *args, **kwargs)

    return DemoClient(HTTP_AUTHORIZATION=f"Bearer {token}")
