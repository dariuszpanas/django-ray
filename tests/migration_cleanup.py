"""Cleanup for stopped, isolated historical migration fixtures only."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def clear_historical_admission_fixtures() -> None:
    """Discard this test's active fixtures after its preservation assertions.

    Historical schemas deliberately admit old writers. Their test rows cannot
    cross the stopped-writer activation in0035. Use the actually applied model
    state, retain terminal history, and never disable a database product fence.
    Callers with protected target/claim history must remove that exact fixture
    history through their existing teardown before using this helper.
    """
    executor = MigrationExecutor(connection)
    historical = executor._create_project_state(with_applied_migrations=True).apps
    execution = historical.get_model("django_ray", "RayTaskExecution")
    execution.objects.filter(state__in=("QUEUED", "RUNNING", "CANCELLING")).only("pk").delete()
    lease = historical.get_model("django_ray", "TaskWorkerLease")
    lease.objects.filter(is_active=True).only("pk").delete()


@pytest.fixture
def preactivation_protocol_schema(request, _restore_execution_protocol_rollout_seed):
    """Exercise reserved historical contracts without widening current admission."""
    if request.node.get_closest_marker("django_db") is None and not {
        "db",
        "transactional_db",
        "django_db_reset_sequences",
        "django_db_serialized_rollback",
    }.intersection(request.fixturenames):
        yield
        return
    from django_ray.protocol_coordination import reopen_legacy_worker_admission

    previous = [("django_ray", "0034_cohort_timeouts")]
    latest = [("django_ray", "0035_activate_current_cohort")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    state = executor.loader.project_state(previous).apps
    policy = state.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.get()
    reopen_legacy_worker_admission(expected_revision=policy.revision)
    # Reproduce the initial historical0019 test seed, including the revision
    # expected by its explicit-CAS examples; this is not a current policy reset.
    state.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.update(revision=1)
    try:
        yield
    finally:
        # These suites own only protocol1/2 test fixtures. Their assertions run
        # before this dependency-ordered cleanup; terminal task history remains.
        executor = MigrationExecutor(connection)
        state = executor._create_project_state(with_applied_migrations=True).apps
        for name in (
            "RayTaskTargetExecutionOutcome",
            "RayTaskTargetExecutionEvidence",
            "RayTaskTargetRouteSelection",
            "RayTaskTargetBinding",
            "RayWorkerTargetCapability",
        ):
            try:
                model = state.get_model("django_ray", name)
            except LookupError:
                continue
            model.objects.all().delete()
        clear_historical_admission_fixtures()
        MigrationExecutor(connection).migrate(latest)


@pytest.fixture
def closed_preactivation_protocol_schema(preactivation_protocol_schema):
    """Permit private protocol3 fixtures at the actual preactivation schema."""
    from django_ray.models import TaskExecutionProtocolPolicy
    from django_ray.protocol_coordination import close_legacy_worker_admission

    close_legacy_worker_admission(
        expected_revision=TaskExecutionProtocolPolicy.objects.get().revision,
        legacy_producers_retired=True,
    )
