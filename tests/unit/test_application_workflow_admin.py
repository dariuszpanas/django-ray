"""The public Admin checker is pinned to the disposable application service."""

import time
from unittest.mock import Mock

import pytest

from qualification.application import workflow_admin as admin
from testproject import docker_smoke

TASK_ID = "f717c512-17d7-4b5e-b778-d614fb14427c"


@pytest.mark.parametrize("deadline", [None, float("nan"), float("inf"), -float("inf")])
def test_admin_adapter_rejects_unbounded_deadlines_before_transport(monkeypatch, deadline):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    factory = Mock()
    monkeypatch.setattr(admin, "ApplicationHttp", factory)
    with pytest.raises(ValueError, match="deadline"):
        admin.read_admin_text(admin.FIXTURE_ORIGIN, "/admin/", deadline=deadline)
    factory.assert_not_called()


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize(
    "settings_module", ["testproject.settings", "testproject.settings_qualification"]
)
def test_service_origin_requires_qualification_settings_and_explicit_transport(
    monkeypatch, qualified, settings_module
):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", settings_module)
    arguments = {
        "base_url": admin.FIXTURE_ORIGIN,
        "task_id": TASK_ID,
        "qualified_transport": qualified,
    }
    if qualified and settings_module == "testproject.settings_qualification":
        assert docker_smoke._validate_existing_workflow_mode(**arguments) == TASK_ID
    else:
        with pytest.raises(docker_smoke.DockerSmokeError):
            docker_smoke._validate_existing_workflow_mode(**arguments)


@pytest.mark.parametrize(
    "origin",
    [
        "http://elsewhere:8000",
        "http://django-web:8001",
        "https://django-web:8000",
        "http://django-web:8000/path",
    ],
)
def test_qualification_cannot_enable_arbitrary_admin_destinations(monkeypatch, origin):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    with pytest.raises(docker_smoke.DockerSmokeError):
        docker_smoke._validate_existing_workflow_mode(
            base_url=origin, task_id=TASK_ID, qualified_transport=True
        )


@pytest.mark.parametrize("status", [200, 302, 403, 500])
def test_admin_adapter_preserves_status_and_forwards_one_bounded_get(monkeypatch, status):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    transport = Mock(return_value=(status, b"<html>fixture</html>"))
    factory = Mock(return_value=transport)
    monkeypatch.setattr(admin, "ApplicationHttp", factory)
    args = {"headers": {"Cookie": "sessionid=fixture"}, "deadline": time.monotonic() + 10}
    if status == 200:
        assert (
            admin.read_admin_text(admin.FIXTURE_ORIGIN, "/admin/", **args) == "<html>fixture</html>"
        )
    else:
        with pytest.raises(ValueError, match="status or deadline"):
            admin.read_admin_text(admin.FIXTURE_ORIGIN, "/admin/", **args)
    transport.assert_called_once()
    assert transport.call_args.kwargs["method"] == "GET"
    assert transport.call_args.kwargs["response_limit"] == 1024 * 1024
    assert factory.call_args.kwargs["request_timeout"] <= 5


@pytest.mark.django_db
@pytest.mark.parametrize("fail", [False, True])
def test_reused_admin_checker_removes_its_real_session_and_user(fail):
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.models import Session

    users = get_user_model().objects.count()
    sessions = Session.objects.count()
    try:
        with docker_smoke._disposable_admin_headers() as headers:
            assert headers["Cookie"]
            assert get_user_model().objects.count() == users + 1
            assert Session.objects.count() == sessions + 1
            if fail:
                raise RuntimeError("observation failed")
    except RuntimeError:
        assert fail
    assert get_user_model().objects.count() == users
    assert Session.objects.count() == sessions


@pytest.mark.django_db
@pytest.mark.parametrize("mutation", [None, "current", "archived"])
def test_admin_observer_detects_diagnostic_mutation_without_exporting_raw_values(
    monkeypatch, mutation
):
    from django_ray.models import RayTaskExecution, TaskAttempt

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    row = RayTaskExecution.objects.create(
        task_id=TASK_ID,
        callable_path="fixture.task",
        state="FAILED",
        attempt_number=1,
        error_message="private fixture error",
    )
    history = TaskAttempt.objects.create(
        execution=row, state="FAILED", attempt_number=1, error_message="private fixture error"
    )

    def observe(**_kwargs):
        if mutation == "current":
            RayTaskExecution.objects.filter(pk=row.pk).update(error_message="changed")
        elif mutation == "archived":
            TaskAttempt.objects.filter(pk=history.pk).update(error_message="changed")
        return {"admin_workflow": "verified"}

    monkeypatch.setattr(docker_smoke, "_run_existing_workflow_admin_smoke", observe)
    if mutation is not None:
        with pytest.raises(ValueError, match="changed protected diagnostics"):
            admin.observe_admin_contract(task_id=TASK_ID, policy="full", attempt=None)
    else:
        evidence = admin.observe_admin_contract(task_id=TASK_ID, policy="full", attempt=None)
        assert evidence == {"admin_workflow": "verified", "diagnostics_preserved": True}
        assert "private fixture error" not in str(evidence)
