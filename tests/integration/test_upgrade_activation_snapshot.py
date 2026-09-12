"""Exercise the database-only upgrade phases without starting an executor."""

import json

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import override_settings

from django_ray.models import RayTaskCohortIntent, RayTaskExecution
from qualification.upgrade import step
from tests.migration_cleanup import preactivation_protocol_schema as preactivation_protocol_schema

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(
    autouse=True,
    params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)],
)
def selected_database(request):
    if connection.vendor != request.param:
        pytest.skip(f"This case requires {request.param}")


def test_blocked_snapshot_refuses_activation_without_changing_old_fields(
    preactivation_protocol_schema, tmp_path
):
    historical = (
        MigrationExecutor(connection)
        .loader.project_state([("django_ray", "0034_cohort_timeouts")])
        .apps
    )
    task_model = historical.get_model("django_ray", "RayTaskExecution")
    for task_id in step.FIXTURE_IDS:
        task_model.objects.create(
            task_id=task_id,
            callable_path="retired_upgrade_application.task",
            state={"upgrade-queued": "QUEUED", "upgrade-uncertain": "RUNNING"}.get(
                task_id, "SUCCEEDED"
            ),
            execution_protocol_version=1,
        )
    historical.get_model("django_ray", "TaskWorkerLease").objects.create(
        worker_id="released-upgrade-worker",
        hostname="synthetic-fixture",
        pid=1,
        is_active=True,
    )
    before = step._snapshot()
    (tmp_path / "seed-snapshot.json").write_text(step._json(before), encoding="utf-8")

    observations = step._refuse_activation(tmp_path)

    assert observations == {
        "blocked_tasks": 2,
        "active_leases": 1,
        "activation_refused": True,
        "activation_recorded": False,
        "active_write_protocol_version": 1,
        "legacy_token_present": True,
        "original_fields_unchanged": True,
    }
    assert step._snapshot() == before
    assert (
        not MigrationRecorder(connection)
        .migration_qs.filter(app="django_ray", name="0035_activate_current_cohort")
        .exists()
    )


def test_current_write_phase_persists_protocol_three_intent_without_execution(
    preactivation_protocol_schema, tmp_path
):
    for task_id in step.FIXTURE_IDS:
        RayTaskExecution.objects.create(
            task_id=task_id,
            callable_path="retired_upgrade_application.task",
            state="SUCCEEDED",
            execution_protocol_version=1,
        )
    before = step._snapshot()
    MigrationExecutor(connection).migrate([("django_ray", "0035_activate_current_cohort")])
    with override_settings(
        TASKS={"default": {"BACKEND": "django_ray.backends.RayTaskBackend"}},
        DJANGO_RAY={
            "RAY_ADDRESS": "auto",
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "inputs"),
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "results"),
        },
    ):
        observations = step._current_write()

    assert observations == {
        "current_enqueue": True,
        "candidate_only_rows": 1,
        "execution_protocol_version": 3,
        "persisted_intent_matches": True,
    }
    current = RayTaskExecution.objects.exclude(task_id__in=step.FIXTURE_IDS).get()
    assert current.state == "QUEUED"
    assert current.execution_generation == 0
    assert json.loads(current.args_json) == [7]
    assert RayTaskCohortIntent.objects.filter(execution=current).count() == 1
    assert step._snapshot() == before
    # Retain the immutable intent and settle only this never-claimed test task
    # through the real lifecycle before the historical fixture restores schema.
    from django_ray.lifecycle import request_task_cancellation

    outcome = request_task_cancellation(
        current.pk,
        expected_attempt_number=current.attempt_number,
        expected_execution_generation=current.execution_generation,
    )
    assert outcome.state == "CANCELLED"
