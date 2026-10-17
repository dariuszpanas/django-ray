"""Exercise the fixture through real coordination, row triggers and APIs."""

from datetime import timedelta
from io import StringIO

import pytest
from django.db.models.query import QuerySet
from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease
from django_ray.protocol_coordination import close_legacy_worker_admission
from qualification.application import protocol_fixture as fixture
from qualification.application.api import (
    validate_execution_protocol_metrics,
    validate_execution_protocol_visibility,
    validate_task_status_payload,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def manager(monkeypatch):
    from django_ray.management.commands.django_ray_worker import Command

    monkeypatch.setattr("socket.gethostname", lambda: "django-manager-protocol-fixture")
    command = Command()
    command.stdout = StringIO()
    command.execution_mode = "cluster"
    command.worker_id = "protocol-fixture-manager"
    command.active_tasks = {}
    command._create_lease("default")

    def forbidden(*args, **kwargs):
        pytest.fail("The unsupported queued row reached manager dispatch")

    for method in ("execute_task_sync", "submit_task_to_ray_core", "submit_task_to_ray"):
        monkeypatch.setattr(command, method, forbidden)
    return command


def checkpoint():
    pass


def test_real_manager_skips_owned_row_while_api_status_and_metrics_expose_it(
    manager, client, settings
):
    owned = fixture.Fixture("before")
    baseline = fixture.policy_state().revision
    headers = {"HTTP_AUTHORIZATION": f"Bearer {settings.DJANGO_API_TOKEN}"}
    initial = client.get("/api/metrics", **headers)
    assert validate_execution_protocol_metrics(initial.content)[("other", "QUEUED")] == 0
    owned.seed(checkpoint)
    assert owned.observe() is False
    assert manager.claim_and_process_tasks(["default"], concurrency=1) == 0
    TaskWorkerLease.objects.filter(pk=manager.worker_id).update(last_heartbeat_at=timezone.now())
    assert owned.observe() is True
    owned.status()
    row = RayTaskExecution.objects.get(pk=owned.pk)
    assert fixture.fingerprint(row) == owned.queued_digest
    assert not row.attempts.exists()
    status = client.get(f"/api/tasks/{owned.task_id}", **headers)
    assert status.status_code == 200
    payload = status.json()
    assert validate_task_status_payload(payload, task_id=owned.task_id) == "QUEUED"
    validate_execution_protocol_visibility(
        payload, surface="task", expected_protocol=2, expected_compatible=False
    )
    response = client.get("/api/executions", {"task_id": owned.task_id, "limit": 1}, **headers)
    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["task_id"] == owned.task_id
    validate_execution_protocol_visibility(
        tasks[0], surface="list", expected_protocol=2, expected_compatible=False
    )
    assert (
        validate_execution_protocol_metrics(client.get("/api/metrics", **headers).content)[
            ("other", "QUEUED")
        ]
        == 1
    )
    owned.cleanup(checkpoint)
    assert not RayTaskExecution.objects.filter(task_id=owned.task_id).exists()
    restored = fixture.policy_state()
    assert restored.legacy_worker_admission_enabled is True
    assert restored.revision == baseline + 2
    assert TaskWorkerLease.objects.get(pk=manager.worker_id).is_active


def test_closed_baseline_is_not_reopened(manager):
    original = fixture.policy_state().revision
    close_legacy_worker_admission(expected_revision=original, legacy_producers_retired=True)
    owned = fixture.Fixture("before")
    with pytest.raises(ValueError, match="open admission"):
        owned.seed(checkpoint)
    owned.cleanup(checkpoint)
    policy = fixture.policy_state()
    assert policy.revision == original + 1 and not policy.legacy_worker_admission_enabled
    assert not RayTaskExecution.objects.exists()


def test_inactive_legacy_token_holder_is_not_detached_by_current_only_fixture(manager):
    old = TaskWorkerLease.objects.create(
        worker_id="inactive-legacy",
        hostname="old",
        pid=1,
        is_active=False,
    )
    baseline = fixture.policy_state().revision
    owned = fixture.Fixture("before")
    with pytest.raises(ValueError, match="legacy manager lease"):
        owned.seed(checkpoint)
    owned.cleanup(checkpoint)
    old.refresh_from_db()
    assert old.legacy_admission_token_id == 1
    assert fixture.policy_state().revision == baseline
    assert not RayTaskExecution.objects.exists()


@pytest.mark.parametrize("failure", ["insert", "close", "queue"])
def test_ambiguous_successful_write_is_recovered_by_saved_exact_identity(
    manager, monkeypatch, failure
):
    owned = fixture.Fixture("after")
    baseline = fixture.policy_state().revision

    if failure == "insert":
        original = RayTaskExecution.save

        def save(row, *args, **kwargs):
            result = original(row, *args, **kwargs)
            if kwargs.get("force_insert"):
                raise RuntimeError("lost insert acknowledgment")
            return result

        monkeypatch.setattr(RayTaskExecution, "save", save)
    elif failure == "close":

        def close(**kwargs):
            close_legacy_worker_admission(**kwargs)
            raise RuntimeError("lost policy acknowledgment")

        monkeypatch.setattr("django_ray.protocol_coordination.close_legacy_worker_admission", close)
    else:
        original = QuerySet.update

        def update(query, **kwargs):
            result = original(query, **kwargs)
            if query.model is RayTaskExecution and kwargs == {"state": "QUEUED"}:
                raise RuntimeError("lost queue acknowledgment")
            return result

        monkeypatch.setattr(QuerySet, "update", update)
    with pytest.raises(RuntimeError, match="acknowledgment"):
        owned.seed(checkpoint)
    assert RayTaskExecution.objects.filter(task_id=owned.task_id).count() == 1
    owned.cleanup(checkpoint)
    assert not RayTaskExecution.objects.exists()
    policy = fixture.policy_state()
    assert policy.legacy_worker_admission_enabled
    assert policy.revision == baseline + (0 if failure == "insert" else 2)


@pytest.mark.parametrize("change", ["claim", "input", "attempt", "revision"])
def test_foreign_change_refuses_cleanup_without_mutating_row_or_reopening(manager, change):
    from django_ray.models import TaskExecutionProtocolPolicy

    owned = fixture.Fixture("before")
    owned.seed(checkpoint)
    assert owned.revision is not None
    rows = RayTaskExecution.objects.filter(pk=owned.pk)
    if change == "claim":
        rows.update(claimed_by_worker=manager.worker_id)
    elif change == "input":
        rows.update(args_json='["foreign"]')
    elif change == "attempt":
        TaskAttempt.objects.create(execution_id=owned.pk, attempt_number=1, state="FAILED")
    else:
        TaskExecutionProtocolPolicy.objects.update(revision=owned.revision + 3)
    snapshot = fixture.fingerprint(rows.get())
    with pytest.raises(ValueError):
        owned.cleanup(checkpoint)
    assert fixture.fingerprint(rows.get()) == snapshot
    assert not fixture.policy_state().legacy_worker_admission_enabled


def test_failed_reopen_preserves_terminalized_row_for_owned_retry(manager, monkeypatch):
    from django_ray import protocol_coordination

    owned = fixture.Fixture("before")
    owned.seed(checkpoint)
    original = protocol_coordination.reopen_legacy_worker_admission

    def fail(**kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(protocol_coordination, "reopen_legacy_worker_admission", fail)
    with pytest.raises(RuntimeError):
        owned.cleanup(checkpoint)
    row = RayTaskExecution.objects.get(pk=owned.pk)
    assert row.state == "FAILED" and fixture.fingerprint(row) == owned.staged_digest
    assert not fixture.policy_state().legacy_worker_admission_enabled
    monkeypatch.setattr(protocol_coordination, "reopen_legacy_worker_admission", original)
    owned.cleanup(checkpoint)
    assert not RayTaskExecution.objects.exists()
    assert fixture.policy_state().legacy_worker_admission_enabled


@pytest.mark.parametrize("change", ["stale", "inactive", "future-heartbeat", "changed-row"])
def test_observation_requires_live_same_manager_and_all_original_fields(manager, change):
    owned = fixture.Fixture("before")
    owned.seed(checkpoint)
    if change == "stale":
        TaskWorkerLease.objects.update(last_heartbeat_at=timezone.now() - timedelta(seconds=61))
    elif change == "inactive":
        TaskWorkerLease.objects.update(is_active=False)
    elif change == "future-heartbeat":
        TaskWorkerLease.objects.update(last_heartbeat_at=timezone.now() + timedelta(seconds=60))
    else:
        RayTaskExecution.objects.filter(pk=owned.pk).update(completion_data="foreign")
    with pytest.raises(ValueError):
        owned.observe()
