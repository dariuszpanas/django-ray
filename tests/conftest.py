"""Pytest configuration and fixtures for django-ray."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import django
import pytest
from django.conf import settings

from scripts.local_resource_coordinator import (
    LOCAL_RESOURCE_INHERITANCE_ENV_KEYS,
    LocalResourceCoordinationError,
    LocalResourceLease,
    acquire_local_resources,
)

pytest_plugins = ("scripts.pytest_taxonomy",)

# Locust's process-wide gevent patching is appropriate for the CLI but unsafe
# when its test module is collected after Django or urllib3 has imported SSL.
os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")

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
_REAL_RAY_OWNERSHIP_KEY: pytest.StashKey[LocalResourceLease] = pytest.StashKey()


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


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    """Own the host only when final collection will execute local-Ray tests."""
    if session.config.option.collectonly or session.testsfailed:
        return
    selected_count = sum(item.get_closest_marker("real_ray") is not None for item in session.items)
    if selected_count == 0:
        return

    def retain_ownership(ownership: LocalResourceLease) -> None:
        try:
            session.config.stash[_REAL_RAY_OWNERSHIP_KEY] = ownership
        except Exception as error:
            raise LocalResourceCoordinationError(
                "pytest could not retain acquired local resource ownership"
            ) from error

    try:
        ownership = acquire_local_resources(
            profile="real-ray",
            phase="pytest",
            rootpath=Path(str(session.config.rootpath)).resolve(),
            selected_count=selected_count,
            on_acquired=retain_ownership,
        )
        if session.config.stash.get(_REAL_RAY_OWNERSHIP_KEY, None) is not ownership:
            ownership.release(
                outcome="failed",
                postcondition="pytest acquisition callback did not retain ownership",
            )
            raise LocalResourceCoordinationError(
                "pytest acquisition callback did not retain returned ownership"
            )
        for key in LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
            os.environ.pop(key, None)
    except LocalResourceCoordinationError as error:
        try:
            _release_real_ray_ownership(
                session.config,
                outcome="failed",
                postcondition="pytest ownership initialization failed",
            )
        except LocalResourceCoordinationError as cleanup_error:
            raise pytest.UsageError(str(cleanup_error)) from cleanup_error
        raise pytest.UsageError(str(error)) from error
    except BaseException:
        _release_real_ray_ownership(
            session.config,
            outcome="failed",
            postcondition="pytest ownership initialization failed",
        )
        raise


def _release_real_ray_ownership(
    config: pytest.Config,
    *,
    outcome: str,
    postcondition: str,
) -> None:
    ownership = config.stash.get(_REAL_RAY_OWNERSHIP_KEY, None)
    if ownership is None:
        return
    ownership.release(outcome=outcome, postcondition=postcondition)
    del config.stash[_REAL_RAY_OWNERSHIP_KEY]


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Release local-Ray ownership after the selected session finishes."""
    _release_real_ray_ownership(
        session.config,
        outcome="passed" if exitstatus == 0 else "failed",
        postcondition="pytest session finished; Ray cleanup checks remain authoritative",
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    """Release ownership on pytest exits that bypass normal session finish."""
    _release_real_ray_ownership(
        config,
        outcome="interrupted",
        postcondition="pytest unconfigured; Ray cleanup checks remain authoritative",
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
                "RUNTIME_ENV_PROFILES": {"ray-data": {}},
            },
            RAY_DATA_INPUT_ROOT=str(PROJECT_ROOT / "ray-data-input"),
            RAY_DATA_OUTPUT_ROOT=str(PROJECT_ROOT / "ray-data-artifacts"),
            RAY_DATA_DEPLOYMENT_KEY="testproject-tests",
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
                "ray-data": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["ray-data"],
                    "OPTIONS": {
                        "RAY_ADDRESS": "auto",
                        "RUNTIME_ENV_PROFILE": "ray-data",
                        "RAY_JOB_ONLY": True,
                    },
                },
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            # Operational API routes are authenticated in the sample project. Keep
            # the fixture token deterministic and send it with every API test request.
            DJANGO_API_TOKEN="test-api-token-for-pytest",
        )

    django.setup()


@pytest.fixture()
def ray_cluster() -> Iterator[object]:
    """Provide one bounded local Ray runtime to an explicitly marked test."""
    import ray

    assert not ray.is_initialized()
    ray.init(address="local", include_dashboard=False, num_cpus=2)
    try:
        yield ray
    finally:
        ray.shutdown()


@pytest.fixture(autouse=True)
def _restore_execution_protocol_rollout_seed(request: pytest.FixtureRequest) -> None:
    """Restore migration-seeded protocol rows after transactional test flushes."""
    database_fixtures = {
        "db",
        "django_db_reset_sequences",
        "django_db_serialized_rollback",
        "live_server",
        "transactional_db",
    }
    has_database_marker = request.node.get_closest_marker("django_db") is not None
    has_database_fixture = not database_fixtures.isdisjoint(request.fixturenames)
    if not has_database_marker and not has_database_fixture:
        return

    request.getfixturevalue("django_db_setup")
    django_db_blocker = request.getfixturevalue("django_db_blocker")

    from django_ray.models import LegacyWorkerAdmissionToken, TaskExecutionProtocolPolicy

    with django_db_blocker.unblock():
        policy, _ = TaskExecutionProtocolPolicy.objects.get_or_create(
            singleton_key=1,
            defaults={
                "schema_version": 1,
                "active_write_protocol_version": 1,
                "legacy_worker_admission_enabled": True,
                "revision": 1,
            },
        )
        if policy.legacy_worker_admission_enabled:
            LegacyWorkerAdmissionToken.objects.get_or_create(singleton_key=1)


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
