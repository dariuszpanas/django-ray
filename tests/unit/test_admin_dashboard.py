"""Browser-facing dashboard links require explicit application configuration."""

import pytest
from django.contrib.admin import AdminSite
from django.test import override_settings

from django_ray.admin import RayTaskExecutionAdmin
from django_ray.models import RayTaskExecution


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        8265,
        "ray://head:10001",
        "//head:8265",
        "https://",
        "https://[bad",
        "https://head:bad",
        "https://head:99999",
        "https://user:secret@head",
        "https://head/?token=secret",
        "https://head/#/jobs",
        "https://head/ bad",
        "https://head\\other",
    ],
)
def test_invalid_or_empty_dashboard_configuration_has_no_link(url: object) -> None:
    task = RayTaskExecution(ray_job_id="02000000:task")
    model_admin = RayTaskExecutionAdmin(RayTaskExecution, AdminSite())
    with override_settings(RAY_DASHBOARD_URL=url):
        for rendered in (
            model_admin.ray_dashboard_link(task),
            model_admin.ray_job_id_display(task),
        ):
            assert "configure RAY_DASHBOARD_URL" in rendered
            assert "href=" not in rendered


def test_missing_dashboard_setting_does_not_invent_localhost(settings) -> None:
    if hasattr(settings, "RAY_DASHBOARD_URL"):
        del settings.RAY_DASHBOARD_URL
    task = RayTaskExecution(ray_job_id="raysubmit_example")
    model_admin = RayTaskExecutionAdmin(RayTaskExecution, AdminSite())
    assert "href=" not in model_admin.ray_dashboard_link(task)
    assert "raysubmit_example" in model_admin.ray_job_id_display(task)


@pytest.mark.parametrize("base", ["https://ray.example/proxy", "https://ray.example/proxy/"])
@pytest.mark.parametrize(
    ("identifier", "route"),
    [
        ("raysubmit_example", "jobs/raysubmit_example"),
        ("02000000:task/?#", "jobs/02000000/tasks/task%2F%3F%23"),
    ],
)
def test_dashboard_routes_preserve_base_path_and_encode_identifiers(
    base, identifier, route
) -> None:
    task = RayTaskExecution(ray_job_id=identifier)
    model_admin = RayTaskExecutionAdmin(RayTaskExecution, AdminSite())
    with override_settings(RAY_DASHBOARD_URL=base):
        for rendered in (
            model_admin.ray_dashboard_link(task),
            model_admin.ray_job_id_display(task),
        ):
            assert f'href="https://ray.example/proxy/#/{route}"' in rendered


@override_settings(RAY_DASHBOARD_URL="http://localhost:8265/")
def test_explicit_port_forward_and_legacy_handle() -> None:
    model_admin = RayTaskExecutionAdmin(RayTaskExecution, AdminSite())
    task = RayTaskExecution(ray_job_id="ray_core:42")
    assert 'href="http://localhost:8265/#/jobs"' in model_admin.ray_dashboard_link(task)
    assert model_admin.ray_job_id_display(task) == "N/A (legacy format)"
    task.ray_job_id = ""
    assert model_admin.ray_dashboard_link(task) == "-"
    assert model_admin.ray_job_id_display(task) == "Not yet submitted"
