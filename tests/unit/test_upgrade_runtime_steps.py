"""SQLite/public-producer checks; these do not prove native upgrade execution."""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader

from django_ray.models import RayTaskExecution, TaskInputPayload
from qualification.upgrade import runtime_steps as steps
from tests.migration_cleanup import preactivation_protocol_schema as preactivation_protocol_schema


@pytest.fixture
def runtime_environment(tmp_path, monkeypatch, settings):
    import ray

    def forbidden(*_args, **_kwargs):
        pytest.fail("a producer or database observer must not initialize Ray")

    monkeypatch.setattr(ray, "init", forbidden)
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(root))
    steps.prepare()
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "upgrade-head:6379",
        "RUNNER": "ray_job",
        "RUNTIME_ENV_PROFILES": {"upgrade": {}},
        "DEFAULT_RUNTIME_ENV_PROFILE": "upgrade",
        "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(root / "inputs"),
    }
    settings.TASKS = {
        "default": {
            "BACKEND": "django_ray.backends.RayTaskBackend",
            "QUEUES": ["upgrade-core"],
            "OPTIONS": {"RAY_ADDRESS": "upgrade-head:6379", "RUNTIME_ENV_PROFILE": "upgrade"},
        },
        "jobs": {
            "BACKEND": "django_ray.backends.RayTaskBackend",
            "QUEUES": ["upgrade-jobs"],
            "OPTIONS": {
                "RAY_ADDRESS": "http://upgrade-head:8265",
                "RAY_JOB_ONLY": True,
                "RUNTIME_ENV_PROFILE": "upgrade",
            },
        },
    }
    return root


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["current-core", "current-jobs"])
def test_public_producer_creates_one_current_intent_without_connecting(runtime_environment, case):
    record = steps.enqueue(case)
    row = RayTaskExecution.objects.get(pk=record["task_pk"])
    assert record["task_id"] == row.task_id
    assert row.execution_protocol_version == 3
    assert row.state == "QUEUED"
    assert row.queue_name == ("upgrade-core" if case == "current-core" else "upgrade-jobs")
    assert row.cohort_intent is not None
    observed = steps.inspect(case)
    assert observed["task_id"] == row.task_id
    assert observed["execution_protocol_version"] == 3
    assert observed["absent_fields"] == []
    assert observed["attempts"] == []
    assert observed["input_referenced"] is (case == "current-core")
    if case == "current-core":
        assert TaskInputPayload.objects.count() == 1
    with pytest.raises(steps.StepError, match="already-started"):
        steps.enqueue(case)
    assert RayTaskExecution.objects.count() == 1


@pytest.mark.django_db
def test_lost_index_write_cannot_turn_into_another_public_enqueue(runtime_environment, monkeypatch):
    original = steps._write_once

    def fail_index(path, value):
        if path.name == "current-core.json":
            raise OSError("fixture-only index write failure")
        original(path, value)

    monkeypatch.setattr(steps, "_write_once", fail_index)
    with pytest.raises(OSError):
        steps.enqueue("current-core")
    assert RayTaskExecution.objects.count() == 1
    with pytest.raises(steps.StepError, match="already-started"):
        steps.enqueue("current-core")
    assert RayTaskExecution.objects.count() == 1


@pytest.mark.django_db
def test_observer_reads_actual_released_model_shape_without_inventing_protocol(
    runtime_environment, monkeypatch
):
    record = steps.enqueue("current-core")
    old_apps = (
        MigrationLoader(connection)
        .project_state([("django_ray", "0018_workflow_run_allocation")])
        .apps
    )
    old_model = old_apps.get_model("django_ray", "RayTaskExecution")
    # Read an actual SQL row through the released migration model. This checks
    # schema absence only, not execution of an old task or migration qualification.
    row = old_model.objects.get(pk=record["task_pk"])
    monkeypatch.setattr(steps, "_task", lambda _case: row)
    observed = steps.inspect("old-success")
    assert observed["absent_fields"] == [
        "execution_protocol_version",
        "created_with_django_ray_version",
        "managed_with_django_ray_version",
    ]
    for name in observed["absent_fields"]:
        assert name not in observed
    assert observed["task_id"] == record["task_id"]


@pytest.mark.django_db
def test_observer_refuses_changed_task_identity_before_reporting(runtime_environment):
    record = steps.enqueue("current-core")
    RayTaskExecution.objects.filter(pk=record["task_pk"]).update(callable_path="different.task")
    with pytest.raises(steps.StepError, match="identity-changed"):
        steps.inspect("current-core")


def test_history_codec_preserves_sub_millisecond_timestamp_changes():
    first = datetime(2026, 9, 12, 1, 2, 3, 123001, tzinfo=UTC)
    second = first.replace(microsecond=123999)
    assert steps._json({"created_at": first}) != steps._json({"created_at": second})
    assert json.loads(steps._json({"created_at": first}))["created_at"].endswith(".123001+00:00")


@pytest.mark.parametrize("value", ['{"case":"a","case":"b"}', '{"value":NaN}', "{"])
def test_case_record_parser_rejects_ambiguous_json(tmp_path, value):
    path = tmp_path / "record.json"
    path.write_text(value)
    with pytest.raises(steps.StepError, match="invalid-upgrade-record"):
        steps._read(path)


def test_prepare_uses_only_an_empty_existing_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(tmp_path))
    assert steps.prepare() == {"artifact_directories_prepared": True}
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "inputs",
        "observations",
        "results",
        "runtime-effects",
    ]
    with pytest.raises(steps.StepError, match="root-not-empty"):
        steps.prepare()


@pytest.mark.parametrize("action", ["cancel", "release"])
def test_mutating_actions_refuse_unowned_cases_before_database(action, monkeypatch):
    def forbidden(_case):
        pytest.fail("unsupported mutation reached the database")

    monkeypatch.setattr(steps, "_task", forbidden)
    with pytest.raises(steps.StepError, match="unsupported-upgrade"):
        getattr(steps, action)("current-core")


@pytest.fixture
def cancellation_observation(runtime_environment, monkeypatch):
    row = SimpleNamespace(
        pk=1, task_id="fixture-task", state="RUNNING", attempt_number=1, execution_generation=2
    )
    marker = {
        "schema": 1,
        "case": "old-cancel",
        "phase": "started",
        "observed_at": datetime.now(UTC).isoformat(),
        "identity": {
            "task_pk": row.pk,
            "task_id": row.task_id,
            "attempt": row.attempt_number,
            "generation": row.execution_generation,
            "native_job_id": "01000000",
            "package_version": "0.4.0",
            "ray_version": "2.56.0",
            "python": platform.python_version(),
            "implementation": "cpython",
            "context_protocol": None,
        },
    }
    path = runtime_environment / "runtime-effects/old-cancel.1.started.json"
    steps._write_once(path, marker)
    monkeypatch.setattr(steps, "_task", lambda _case: row)
    return row, path, marker


def test_cancellation_refuses_unobserved_application_entry(cancellation_observation, monkeypatch):
    from django_ray import lifecycle

    _row, path, marker = cancellation_observation
    marker["identity"]["generation"] += 1
    path.write_bytes(steps._json(marker))

    def forbidden(*_args, **_kwargs):
        pytest.fail("cancellation crossed an unmatched application-entry observation")

    monkeypatch.setattr(lifecycle, "request_task_cancellation", forbidden)
    with pytest.raises(steps.StepError, match="effect-identity-mismatch"):
        steps.cancel("old-cancel")


@pytest.mark.parametrize("accepted", [True, False])
def test_cancellation_requires_actual_request_acceptance(
    cancellation_observation, monkeypatch, accepted
):
    from django_ray import lifecycle

    row, _path, _marker = cancellation_observation
    status = (
        lifecycle.TaskCancellationRequestStatus.ACCEPTED
        if accepted
        else lifecycle.TaskCancellationRequestStatus.COMPLETION_PENDING
    )
    outcome = lifecycle.TaskCancellationRequestResult(status, row.pk, row.state, 1, 2)
    calls = []

    def request(pk, **kwargs):
        calls.append((pk, kwargs))
        return outcome

    monkeypatch.setattr(lifecycle, "request_task_cancellation", request)
    monkeypatch.setattr(steps, "inspect", lambda _case: {"state": "CANCELLING"})
    if accepted:
        observed = steps.cancel("old-cancel")
        assert observed == {
            "state": "CANCELLING",
            "cancellation_requested": True,
            "cancellation_request_status": "ACCEPTED",
        }
    else:
        with pytest.raises(steps.StepError, match="cancellation-not-accepted"):
            steps.cancel("old-cancel")
    assert calls == [(1, {"expected_attempt_number": 1, "expected_execution_generation": 2})]


@pytest.mark.django_db(transaction=True)
def test_blocked_clone_runs_real_activation_refusal_and_preserves_original_sql_fields(
    preactivation_protocol_schema, runtime_environment, monkeypatch
):
    old_apps = (
        MigrationLoader(connection)
        .project_state([("django_ray", "0018_workflow_run_allocation")])
        .apps
    )
    old_model = old_apps.get_model("django_ray", "RayTaskExecution")
    states = ["QUEUED", "FAILED", "CANCELLED", "SUCCEEDED", "RUNNING"]
    pks = []
    for case, state in zip(steps.OLD_CASES, states, strict=True):
        row = old_model.objects.create(
            task_id=case,
            callable_path="qualification.upgrade.runtime_tasks.value",
            queue_name="upgrade-jobs",
            state=state,
            attempt_number=2 if case == "old-retry" else 1,
        )
        pks.append(row.pk)
        steps._write_once(
            runtime_environment / "observations" / f"{case}.json",
            {
                "case": case,
                "task_pk": row.pk,
                "task_id": row.task_id,
                "callable_path": row.callable_path,
            },
        )
    expected = {}
    for name in ("RayTaskExecution", "TaskAttempt", "TaskInputPayload"):
        model = old_apps.get_model("django_ray", name)
        fields = [field.attname for field in model._meta.concrete_fields]
        query = model.objects.order_by(model._meta.pk.attname)
        if name == "RayTaskExecution":
            query = query.filter(pk__in=pks)
        expected[name] = {"fields": fields, "rows": list(query.values(*fields))}
    steps._write_once(runtime_environment / "observations/blocked-rows.json", expected)
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_BUILD", "candidate")
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_DATABASE", "scratch")
    observed = steps.blocked_migrate()
    assert observed["activation_refused"] is True
    assert observed["activation_recorded"] is False
    assert observed["original_rows_before_sha256"] == observed["original_rows_after_sha256"]
    assert observed["policy_protocol"] == 1
    assert observed["legacy_token_present"] is True
    assert old_model.objects.filter(pk__in=pks).count() == 5


@pytest.mark.parametrize("action", ["migrate", "blocked_migrate", "blocked_history"])
def test_migration_steps_refuse_wrong_epoch_database_before_commands(action, monkeypatch):
    import django.core.management

    monkeypatch.setenv("DJANGO_RAY_UPGRADE_BUILD", "candidate")
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_DATABASE", "unowned")

    def forbidden(*_args, **_kwargs):
        pytest.fail("migration command crossed its finite database boundary")

    monkeypatch.setattr(django.core.management, "call_command", forbidden)
    with pytest.raises(steps.StepError, match="epoch-mismatch"):
        getattr(steps, action)()


def test_blocked_history_refuses_live_primary_before_observing_rows(monkeypatch):
    import django_ray

    monkeypatch.setattr(django_ray, "__version__", "0.4.0")
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_BUILD", "baseline")
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_DATABASE", "primary")

    def forbidden(_case):
        pytest.fail("live primary cannot supply the restored clone's original-field snapshot")

    monkeypatch.setattr(steps, "_task", forbidden)
    with pytest.raises(steps.StepError, match="epoch-mismatch"):
        steps.blocked_history()


@pytest.mark.parametrize(
    "argv,module",
    [
        (["read-history"], "qualification.upgrade.runtime_settings"),
        (["enqueue", "--case", "current-core"], "qualification.upgrade.runtime_history_settings"),
    ],
)
def test_observer_only_settings_cannot_cross_into_execution_commands(
    argv, module, monkeypatch, capsys
):
    import django

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", module)
    calls = []
    monkeypatch.setattr(django, "setup", lambda: calls.append("setup"))
    assert steps.main(argv) == 1
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {
        "step_failed": True,
        "complete_upgrade_gate": False,
    }
