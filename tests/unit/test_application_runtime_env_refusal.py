"""Focused DB and resource-free checks for the negative application fixture."""

import base64
import json
from io import StringIO
from types import SimpleNamespace

import pytest

from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease
from qualification.application import runtime_env_refusal as refusal


@pytest.fixture
def fixture_settings(settings, monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "ray://ray-head:10001",
        "RUNTIME_ENV_PROFILES": {"thin": {}},
        "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "qualification",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {
            "qualification": base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
        },
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": False,
    }


@pytest.fixture
def refused(fixture_settings, db, monkeypatch):
    from django_ray.management.commands.django_ray_worker import Command

    monkeypatch.setattr("socket.gethostname", lambda: "django-manager-fixture")
    command = Command()
    command.stdout = StringIO()
    command.execution_mode = "cluster"
    command.worker_id = "negative-fixture-manager"
    command.active_tasks = {}
    command._create_lease("default")

    def forbidden(*args, **kwargs):
        pytest.fail("The malformed snapshot reached a dispatch path")

    for method in ("execute_task_sync", "submit_task_to_ray_core", "submit_task_to_ray"):
        monkeypatch.setattr(command, method, forbidden)
    fixtures = refusal.create_fixtures()
    assert command.claim_and_process_tasks(queues=["default"], concurrency=2) == 2
    return fixtures


@pytest.mark.django_db
def test_owned_manager_refuses_both_snapshots_and_real_api_retry_preserves_every_field(
    refused, monkeypatch, client, settings
):
    snapshots = {
        item.case: refusal.inspect_failure(item, manager_prefix="django-manager-")
        for item in refused
    }
    assert all(snapshots.values())
    monkeypatch.setattr(refusal, "create_fixtures", lambda: refused)
    calls = []

    def request(path, **kwargs):
        calls.append(path)
        assert kwargs["method"] == "POST"
        assert kwargs["response_limit"] == 64 * 1024
        response = client.post(path, HTTP_AUTHORIZATION=kwargs["headers"]["Authorization"])
        return response.status_code, response.content

    evidence = refusal.empty_evidence()
    refusal.verify_refusals(
        request,
        token=settings.DJANGO_API_TOKEN,
        manager_prefix="django-manager-",
        evidence=evidence,
    )
    refusal.validate_evidence(evidence)
    assert calls == [f"/api/executions/{item.pk}/retry" for item in refused]
    assert RayTaskExecution.objects.count() == TaskAttempt.objects.count() == 2
    for item in refused:
        assert (
            refusal.inspect_failure(item, manager_prefix="django-manager-") == snapshots[item.case]
        )
        assert item.task_id not in json.dumps(evidence)
        assert item.serialized not in json.dumps(evidence)
        assert item.digest not in json.dumps(evidence)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "SUCCEEDED"),
        ("attempt_number", 2),
        ("execution_generation", 2),
        ("execution_protocol_version", 2),
        ("runtime_env_profile", "project"),
        ("runtime_env_json", "untrusted-envelope"),
        ("runtime_env_hash", "0" * 64),
        ("error_message", "a different failure"),
        ("ray_job_id", "unexpected-submission"),
        ("ray_address", "auto"),
        ("ray_job_request_reference", "unexpected-reference"),
        ("executor_django_ray_version", "0.6.0"),
        ("completion_data", "unexpected-completion"),
        ("result_data", "5"),
        ("result_reference", "unexpected-result"),
        ("progress_data", "{}"),
        ("workflow_progress_summary_json", "{}"),
        ("error_traceback", "untrusted-traceback"),
        ("managed_with_django_ray_version", "0.0.0"),
    ],
)
def test_refusal_rejects_other_terminal_outcomes_or_submission_evidence(
    refused, field, value, monkeypatch
):
    row = RayTaskExecution.objects.get(pk=refused[0].pk)
    setattr(row, field, value)
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **kwargs: row)
    with pytest.raises(ValueError):
        refusal.inspect_failure(refused[0], manager_prefix="django-manager-")


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["missing", "extra", "different", "owner", "inactive"])
def test_refusal_requires_one_matching_archive_and_current_owner(refused, change):
    fixture = refused[0]
    attempts = TaskAttempt.objects.filter(execution_id=fixture.pk)
    if change == "missing":
        attempts.delete()
    elif change == "extra":
        TaskAttempt.objects.create(execution_id=fixture.pk, attempt_number=2, state="FAILED")
    elif change == "different":
        attempts.update(error_message="different")
    elif change == "owner":
        TaskWorkerLease.objects.update(hostname="different-manager")
    else:
        TaskWorkerLease.objects.update(is_active=False)
    with pytest.raises(ValueError):
        refusal.inspect_failure(fixture, manager_prefix="django-manager-")


@pytest.mark.django_db
def test_both_fixtures_are_rolled_back_if_second_creation_fails(fixture_settings, monkeypatch):
    original = RayTaskExecution.objects.create
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise ValueError("second creation failed")
        return original(**kwargs)

    monkeypatch.setattr(RayTaskExecution.objects, "create", create)
    with pytest.raises(ValueError):
        refusal.create_fixtures()
    assert not RayTaskExecution.objects.exists()


@pytest.fixture
def fake_observer(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    fixtures = tuple(
        refusal.Fixture(case, number, "task", "envelope", "digest")
        for number, case in enumerate(refusal.CASES, 1)
    )
    monkeypatch.setattr(refusal, "create_fixtures", lambda: fixtures)
    clock = [0]
    monkeypatch.setattr(
        refusal,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    monkeypatch.setattr(refusal, "inspect_failure", lambda *args, **kwargs: b"snapshot")
    return fixtures, clock


def conflict(*args, **kwargs):
    return 409, b'{"detail":"Persisted RuntimeEnv snapshot failed validation"}'


def test_second_timeout_retains_only_completed_first_case(fake_observer, monkeypatch):
    fixtures, clock = fake_observer
    monkeypatch.setattr(
        refusal,
        "inspect_failure",
        lambda item, **kwargs: b"snapshot" if item == fixtures[0] else None,
    )
    evidence = refusal.empty_evidence()
    with pytest.raises(ValueError, match="deadline"):
        refusal.verify_refusals(
            conflict, token="secret", manager_prefix="owned-", evidence=evidence
        )
    assert clock[0] == refusal.OBSERVATION_SECONDS
    assert all(evidence[refusal.CASES[0]].values())
    assert not any(evidence[refusal.CASES[1]].values())


@pytest.mark.parametrize("kind", ["accepted", "other-conflict", "changed", "late-change"])
def test_retry_failure_or_any_history_change_cannot_pass(fake_observer, monkeypatch, kind):
    calls = []

    def inspect(item, **kwargs):
        calls.append(item.case)
        changed = len(calls) >= (2 if kind == "changed" else 5)
        return b"changed" if kind in {"changed", "late-change"} and changed else b"snapshot"

    monkeypatch.setattr(refusal, "inspect_failure", inspect)

    def request(*args, **kwargs):
        if kind == "accepted":
            return 202, b"{}"
        if kind == "other-conflict":
            return 409, b'{"detail":"different"}'
        return conflict()

    with pytest.raises(ValueError):
        refusal.verify_refusals(
            request, token="secret", manager_prefix="owned-", evidence=refusal.empty_evidence()
        )


def test_fixture_settings_are_required_before_any_database_access(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
    monkeypatch.setattr(refusal, "create_fixtures", lambda: pytest.fail("unexpected creation"))
    with pytest.raises(ValueError, match="disposable"):
        refusal.verify_refusals(
            conflict, token="secret", manager_prefix="owned-", evidence=refusal.empty_evidence()
        )


def test_final_history_observation_must_finish_inside_budget(fake_observer, monkeypatch):
    _, clock = fake_observer
    calls = []

    def inspect(*args, **kwargs):
        calls.append(None)
        if len(calls) == 6:
            clock[0] = refusal.OBSERVATION_SECONDS + 1
        return b"snapshot"

    monkeypatch.setattr(refusal, "inspect_failure", inspect)
    with pytest.raises(ValueError, match="deadline"):
        refusal.verify_refusals(
            conflict, token="secret", manager_prefix="owned-", evidence=refusal.empty_evidence()
        )
