"""Pytest configuration and fixtures for django-ray."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Generator, Iterator
from pathlib import Path

import django
import pytest
from django.conf import settings

from scripts.require_linux import require_linux
from tests.local_ray import local_ray_runtime, shutdown_shared_local_ray
from tests.real_ray_ownership import (
    RealRayOwnershipLock,
    RealRayOwnershipPathError,
    RealRayOwnershipUnavailableError,
    build_owner_metadata,
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
    "module_ray_cluster": "real_ray",
    "ray_cluster": "real_ray",
}
MARKER_IMPLICATIONS = {
    "compiled_graph_opt_in": "real_ray",
}
TOOLING_TEST_PREFIXES: tuple[str, ...] = (
    "test_application_",
    "test_audit_runtime_dependencies",
    "test_cancellation_qualification",
    "test_check_commits",
    "test_check_conventional_commits",
    "test_check_spelling",
    "test_ci_checkpoint",
    "test_ci_workflow_policy",
    "test_coverage_debt",
    "test_distributed_preparation_benchmark",
    "test_docker_qualification",
    "test_docker_quickstart",
    "test_docs_",
    "test_grafana_dashboard_importer",
    "test_http_recycle_probe",
    "test_install_ci_debugger",
    "test_installed_candidate_requirements",
    "test_kuberay_",
    "test_kubernetes_",
    "test_latency_",
    "test_linux_test_",
    "test_live_witness_observation",
    "test_local_kuberay_",
    "test_local_ray",
    "test_locustfile",
    "test_makefile_test_targets",
    "test_monitoring_",
    "test_native_exit_investigation",
    "test_native_shutdown_investigation",
    "test_observe_pytest_exit",
    "test_package_metadata",
    "test_platform_auth",
    "test_pr_review_admission",
    "test_prometheus_targets",
    "test_python315_compatibility",
    "test_qualification_gate",
    "test_ray_ecosystem_docs",
    "test_ray_serve_gateway_docs",
    "test_release",
    "test_repository_policy",
    "test_require_linux",
    "test_result_read_qualification",
    "test_retry_pypi_audit",
    "test_runtime_qualification",
    "test_soak_contract",
    "test_stability_contract",
    "test_test_resource_ownership",
    "test_test_suite_",
    "test_transaction_qualification",
    "test_upgrade_qualification",
    "test_verify_doctor_wheel",
    "test_verify_wheel",
    "test_worker_image_qualification",
    "test_worker_mode_qualification",
    "test_workflow_preparation_benchmark_platform",
)
_EXTERNAL_RESOURCE_MARKERS = ("real_ray", "postgresql", "live_cluster")
_REAL_RAY_OWNERSHIP_KEY: pytest.StashKey[RealRayOwnershipLock] = pytest.StashKey()


def _is_tooling_test_item(item: pytest.Item) -> bool:
    item_path = getattr(item, "path", None)
    if item_path is None:
        return False
    path = Path(str(item_path))
    if path.parent.name != "unit" or path.parent.parent.name != "tests":
        return False
    if not any(path.stem.startswith(prefix) for prefix in TOOLING_TEST_PREFIXES):
        return False
    return all(item.get_closest_marker(marker) is None for marker in _EXTERNAL_RESOURCE_MARKERS)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Require external-resource fixtures to declare their execution contract."""
    for item in items:
        if _is_tooling_test_item(item) and item.get_closest_marker("tooling") is None:
            item.add_marker(pytest.mark.tooling)
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

    try:
        require_linux()
    except SystemExit as error:
        raise pytest.UsageError(str(error)) from error

    ownership = RealRayOwnershipLock()
    owner = build_owner_metadata(
        rootpath=Path(str(session.config.rootpath)).resolve(),
        selected_count=selected_count,
    )
    try:
        ownership.acquire(owner)
    except (RealRayOwnershipPathError, RealRayOwnershipUnavailableError) as error:
        raise pytest.UsageError(str(error)) from error
    try:
        session.config.stash[_REAL_RAY_OWNERSHIP_KEY] = ownership
    except BaseException:
        ownership.release()
        raise


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """A skipped required local-Ray case cannot become passing runtime evidence."""
    del call
    report = yield
    if (
        report.skipped
        and item.get_closest_marker("real_ray") is not None
        and item.get_closest_marker("compiled_graph_opt_in") is None
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"Required local-Ray test must execute; skip/xfail is forbidden. "
            f"Original outcome: {report.longrepr}"
        )
        if hasattr(report, "wasxfail"):
            del report.wasxfail
    return report


def _release_real_ray_ownership(config: pytest.Config) -> None:
    ownership = config.stash.get(_REAL_RAY_OWNERSHIP_KEY, None)
    if ownership is None:
        return
    del config.stash[_REAL_RAY_OWNERSHIP_KEY]
    ownership.release()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Shut down any shared local Ray runtime before releasing host ownership."""
    del exitstatus
    try:
        shutdown_shared_local_ray()
    finally:
        _release_real_ray_ownership(session.config)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Release ownership on pytest exits that bypass normal session finish."""
    try:
        shutdown_shared_local_ray()
    finally:
        _release_real_ray_ownership(config)


def pytest_configure(config: object) -> None:
    """Configure Django for testing."""
    if isinstance(config, pytest.Config):
        config.addinivalue_line(
            "markers",
            "tooling: Repository policy, qualification-harness, KubeRay, packaging, and docs tooling tests",
        )
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
    with local_ray_runtime(num_cpus=2) as ray:
        yield ray


@pytest.fixture(scope="module")
def module_ray_cluster() -> Iterator[object]:
    """Share one bounded local Ray runtime across clean-teardown module cases."""
    with local_ray_runtime(num_cpus=2) as ray:
        yield ray


_DATABASE_FIXTURES = frozenset(
    {
        "db",
        "django_db_reset_sequences",
        "django_db_serialized_rollback",
        "live_server",
        "transactional_db",
    }
)
_TRANSACTIONAL_DATABASE_FIXTURES = frozenset(
    {
        "django_db_reset_sequences",
        "django_db_serialized_rollback",
        "live_server",
        "transactional_db",
    }
)
_rollout_seed_needs_restore = True


def _is_transactional_db_request(request: pytest.FixtureRequest, marker: object | None) -> bool:
    fixturenames = getattr(request, "fixturenames", ())
    if not _TRANSACTIONAL_DATABASE_FIXTURES.isdisjoint(fixturenames):
        return True
    if marker is not None:
        args = getattr(marker, "args", ())
        kwargs = getattr(marker, "kwargs", {})
        if (args and bool(args[0])) or any(
            bool(kwargs.get(name))
            for name in ("transaction", "reset_sequences", "serialized_rollback")
        ):
            return True
    instance = getattr(request, "instance", None)
    if instance is not None:
        from django.test import TestCase, TransactionTestCase

        if isinstance(instance, TransactionTestCase) and not isinstance(instance, TestCase):
            return True
    return False


@pytest.fixture(autouse=True)
def _restore_execution_protocol_rollout_seed(request: pytest.FixtureRequest) -> None:
    """Restore migration-seeded protocol rows after transactional test flushes."""
    global _rollout_seed_needs_restore
    django_db_marker = request.node.get_closest_marker("django_db")
    has_database_fixture = not _DATABASE_FIXTURES.isdisjoint(request.fixturenames)
    if django_db_marker is None and not has_database_fixture:
        return

    is_transactional = _is_transactional_db_request(request, django_db_marker)
    if not _rollout_seed_needs_restore and not is_transactional:
        return

    request.getfixturevalue("django_db_setup")
    django_db_blocker = request.getfixturevalue("django_db_blocker")

    from django.apps import apps
    from django.db import connections
    from django.utils import timezone

    from django_ray.models import (
        LegacyWorkerAdmissionToken,
        TaskExecutionProtocolPolicy,
        TaskWorkerLeasePolicy,
    )

    with django_db_blocker.unblock():
        _, lease_created = TaskWorkerLeasePolicy.objects.get_or_create(singleton_key=1)
        created_any = lease_created
        if apps.is_installed("testproject"):
            SampleAdmissionBudget = apps.get_model("testproject", "SampleAdmissionBudget")
            _, budget_created = SampleAdmissionBudget.objects.get_or_create(
                pk=1, defaults={"window_started_at": timezone.now()}
            )
            created_any = created_any or budget_created
        policy, policy_created = TaskExecutionProtocolPolicy.objects.get_or_create(
            singleton_key=1,
            defaults={
                "schema_version": 1,
                "active_write_protocol_version": 1,
                "legacy_worker_admission_enabled": True,
                "revision": 1,
            },
        )
        created_any = created_any or policy_created
        if policy.legacy_worker_admission_enabled:
            _, token_created = LegacyWorkerAdmissionToken.objects.get_or_create(singleton_key=1)
            created_any = created_any or token_created
        in_atomic_block = connections["default"].in_atomic_block
    # Only clear the restore flag when the seed rows are guaranteed to survive
    # test teardown: transactional tests flush on teardown, and rows newly
    # created inside a rollback-isolated atomic block will be rolled back.
    rows_persisted = not is_transactional and (not created_any or not in_atomic_block)
    _rollout_seed_needs_restore = not rows_persisted


@pytest.fixture(autouse=True)
def _clear_django_ray_remote_caches() -> None:
    ray_core = sys.modules.get("django_ray.runner.ray_core")
    if ray_core is not None:
        ray_core._execute_django_task_remote_cached = None

    workflows = sys.modules.get("django_ray.workflows")
    if workflows is not None:
        workflows._execute_workflow_step_remote_cached = None
        workflows._collect_workflow_results_remote_cached = None
        workflows._workflow_progress_actor_cached = None
        workflows._workflow_result_buffer_actor_cached = None
        workflows._workflow_result_fold_actor_cached = None

    distributed = sys.modules.get("django_ray.runtime.distributed")
    if distributed is not None:
        distributed._parallel_map_remote_cached = None
        distributed._parallel_starmap_remote_cached = None
        distributed._scatter_gather_remote_cached = None

    django_ray_logger = logging.getLogger("django_ray")
    if not django_ray_logger.propagate:
        django_ray_logger.propagate = True
