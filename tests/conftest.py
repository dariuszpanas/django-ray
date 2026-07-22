"""Pytest configuration and fixtures for django-ray."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import django
import pytest
from django.conf import settings

# Add testproject to path so it can be imported
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXTERNAL_RESOURCE_FIXTURE_MARKERS = {
    "live_ray_cluster": "live_cluster",
    "ray_cluster": "real_ray",
}
MARKER_IMPLICATIONS = {
    "compiled_graph_opt_in": "real_ray",
}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Require external-resource fixtures to declare their execution contract."""
    for item in items:
        for marker_name, required_marker_name in MARKER_IMPLICATIONS.items():
            if item.get_closest_marker(marker_name) is None:
                continue
            if item.get_closest_marker(required_marker_name) is None:
                raise pytest.UsageError(
                    f"{item.nodeid} is marked {marker_name!r} but is not marked "
                    f"{required_marker_name!r}"
                )

        fixture_names = set(getattr(item, "fixturenames", ()))
        for fixture_name, marker_name in EXTERNAL_RESOURCE_FIXTURE_MARKERS.items():
            if fixture_name not in fixture_names:
                continue
            if item.get_closest_marker(marker_name) is None:
                raise pytest.UsageError(
                    f"{item.nodeid} uses {fixture_name!r} but is not marked {marker_name!r}"
                )


def pytest_configure(config: object) -> None:
    """Configure Django for testing."""
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if not settings.configured and not settings_module:
        settings.configure(
            DEBUG=True,
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.admin",
                "django.contrib.sessions",
                "django.contrib.messages",
                "django_ray",
                "testproject",
            ],
            TEMPLATES=[
                {
                    "BACKEND": "django.template.backends.django.DjangoTemplates",
                    "DIRS": [PROJECT_ROOT / "testproject" / "templates"],
                    "APP_DIRS": True,
                    "OPTIONS": {
                        "context_processors": [
                            "django.template.context_processors.debug",
                            "django.template.context_processors.request",
                            "django.contrib.auth.context_processors.auth",
                            "django.contrib.messages.context_processors.messages",
                        ],
                    },
                },
            ],
            ROOT_URLCONF="testproject.urls",
            STATIC_URL="static/",
            SECRET_KEY="django-ray-tests-only-cursor-signing-key",
            DJANGO_RAY={
                "RAY_ADDRESS": "ray://localhost:10001",
            },
            # Django 6 Tasks configuration
            TASKS={
                "default": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": [
                        "default",
                        "high-priority",
                        "low-priority",
                        "sync",
                        "ml",
                    ],
                    "OPTIONS": {
                        "RAY_ADDRESS": "auto",
                    },
                },
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            # Operational API routes are authenticated in the sample project. Keep
            # the fixture token deterministic and send it with every API test request.
            DJANGO_API_TOKEN="test-api-token-for-pytest",
        )

    django.setup()


@pytest.fixture(autouse=True)
def _clear_django_ray_remote_caches():
    try:
        import django_ray.runner.ray_core as ray_core

        ray_core._execute_django_task_remote_cached = None
    except ImportError:
        pass

    try:
        import django_ray.workflows as workflows

        workflows._execute_workflow_step_remote_cached = None
        workflows._collect_workflow_results_remote_cached = None
        workflows._workflow_progress_actor_cached = None
        workflows._workflow_result_buffer_actor_cached = None
        workflows._workflow_result_fold_actor_cached = None
    except ImportError:
        pass

    try:
        import django_ray.runtime.distributed as distributed

        distributed._parallel_map_remote_cached = None
        distributed._parallel_starmap_remote_cached = None
        distributed._scatter_gather_remote_cached = None
    except ImportError:
        pass
