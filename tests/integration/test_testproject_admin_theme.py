"""Fresh-process contracts for the bundled Unfold admin and package fallback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

THEMED_ADMIN_PROBE = r"""
import html
import json
import re
from pathlib import Path

import django

django.setup()

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.template import loader
from django.test import Client, RequestFactory
from django.urls import reverse
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.admin import TabularInline as UnfoldTabularInline
from unfold.sites import UnfoldAdminSite

from django_ray.admin import (
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
    TaskAttemptInline,
    TaskWorkerLeaseAdmin,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease

assert settings.INSTALLED_APPS.index("unfold") < settings.INSTALLED_APPS.index(
    "django.contrib.admin"
)
assert settings.UNFOLD["SITE_TITLE"] == "django-ray admin"
assert settings.UNFOLD["SITE_HEADER"] == "django-ray"
assert settings.UNFOLD["SITE_SUBHEADER"] == "Distributed task testproject"
assert callable(settings.UNFOLD["SITE_ICON"])
assert callable(settings.UNFOLD["SITE_FAVICONS"][0]["href"])
assert callable(settings.UNFOLD["STYLES"][0])
assert callable(settings.UNFOLD["LOGIN"]["image"])
assert settings.UNFOLD["BORDER_RADIUS"] == "8px"
assert settings.UNFOLD["COLORS"]["base"]["100"] == "#f1f5f9"
assert settings.UNFOLD["COLORS"]["base"]["400"] == "#94a3b8"
assert settings.UNFOLD["COLORS"]["base"]["700"] == "#334155"
assert settings.UNFOLD["COLORS"]["base"]["800"] == "#1e293b"
assert settings.UNFOLD["COLORS"]["base"]["900"] == "#0f172a"
assert settings.UNFOLD["COLORS"]["base"]["950"] == "#020617"
assert settings.UNFOLD["COLORS"]["primary"]["400"] == "#38bdf8"
assert settings.UNFOLD["COLORS"]["primary"]["600"] == "#075985"
assert isinstance(admin.site, UnfoldAdminSite)

expected_admins = {
    RayTaskExecution: RayTaskExecutionAdmin,
    TaskAttempt: TaskAttemptAdmin,
    TaskWorkerLease: TaskWorkerLeaseAdmin,
}
for model, expected_admin in expected_admins.items():
    registered_admin = admin.site._registry[model]
    assert isinstance(registered_admin, expected_admin)
    assert isinstance(registered_admin, UnfoldModelAdmin)
assert issubclass(TaskAttemptInline, UnfoldTabularInline)

template_origin = loader.get_template("admin/base.html").origin.name.replace("\\", "/")
assert "/unfold/templates/admin/base.html" in template_origin
assert finders.find("unfold/css/styles.css")
assert finders.find("unfold/js/app.js")
assert finders.find("django_ray/admin/task_live.css")
assert finders.find("django_ray/admin/workflow_diagnostics.js")
assert finders.find("testproject/admin.css")
assert finders.find("testproject/django-ray.svg")
assert finders.find("testproject/landing-graph-bg.png")

call_command("migrate", interactive=False, verbosity=0)
call_command("collectstatic", interactive=False, verbosity=0)

static_root = Path(settings.STATIC_ROOT)
assert (static_root / "unfold/css/styles.css").is_file()
assert (static_root / "unfold/js/app.js").is_file()
assert (static_root / "django_ray/admin/task_live.css").is_file()
assert (static_root / "django_ray/admin/task_live.js").is_file()
assert (static_root / "django_ray/admin/workflow_diagnostics.js").is_file()
assert (static_root / "testproject/admin.css").is_file()
assert (static_root / "testproject/django-ray.svg").is_file()
assert (static_root / "testproject/landing-graph-bg.png").is_file()

custom_stylesheet_path = settings.UNFOLD["STYLES"][0](None)
site_icon_path = settings.UNFOLD["SITE_ICON"](None)
login_image_path = settings.UNFOLD["LOGIN"]["image"](None)
assert re.fullmatch(r"/static/testproject/admin(?:\.[0-9a-f]+)?\.css", custom_stylesheet_path)
assert re.fullmatch(r"/static/testproject/django-ray(?:\.[0-9a-f]+)?\.svg", site_icon_path)
assert re.fullmatch(
    r"/static/testproject/landing-graph-bg(?:\.[0-9a-f]+)?\.png",
    login_image_path,
)

user = get_user_model().objects.create_superuser(
    username="unfold-admin-probe",
    password="unfold-admin-probe-password",
)
execution = RayTaskExecution.objects.create(
    task_id="unfold-admin-execution",
    callable_path="testproject.tasks.add_numbers",
    state=TaskState.QUEUED,
    runtime_env_profile="unfold-runtime-profile",
    runtime_env_hash="c" * 64,
    runtime_env_json=json.dumps(
        {
            "env_vars": {"DISPLAY_NAME": "unfold-runtime-secret-marker"},
            "working_dir": "https://user:pass@private.example/runtime.zip",
        }
    ),
)
attempt = TaskAttempt.objects.create(
    execution=execution,
    attempt_number=1,
    state=TaskState.FAILED,
    error_message="unfold-attempt-inline-marker",
)
failed_execution = RayTaskExecution.objects.create(
    task_id="unfold-admin-retry",
    callable_path="testproject.tasks.failing_task",
    state=TaskState.FAILED,
    args_json="[]",
    kwargs_json="{}",
)
queued_execution = RayTaskExecution.objects.create(
    task_id="unfold-admin-cancel",
    callable_path="testproject.tasks.add_numbers",
    state=TaskState.QUEUED,
    args_json="[]",
    kwargs_json="{}",
)
permission_request = RequestFactory().get("/admin/")
permission_request.user = user
execution_admin = admin.site._registry[RayTaskExecution]
assert execution_admin.has_change_permission(permission_request) is True
assert execution_admin.has_change_permission(permission_request, execution) is False
assert set(execution_admin.get_actions(permission_request)) == {
    "retry_tasks",
    "cancel_tasks",
}

anonymous = Client()
login = anonymous.get(reverse("admin:login"))
assert login.status_code == 200

authenticated = Client()
authenticated.force_login(user)
index = authenticated.get(reverse("admin:index"))
changelist = authenticated.get(reverse("admin:django_ray_raytaskexecution_changelist"))
change = authenticated.get(
    reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
)
observability = authenticated.get(
    reverse("admin:django_ray_raytaskexecution_observability", args=[execution.pk])
)
attempt_detail_url = reverse(
    "admin:django_ray_taskattempt_change",
    args=[attempt.pk],
)
attempt_detail = authenticated.get(attempt_detail_url)

assert index.status_code == 200
assert changelist.status_code == 200
assert change.status_code == 200
assert observability.status_code == 200
assert attempt_detail.status_code == 200

login_html = login.content.decode("utf-8")
index_html = index.content.decode("utf-8")
changelist_html = changelist.content.decode("utf-8")
change_html = change.content.decode("utf-8")

for rendered_html in (login_html, index_html, changelist_html, change_html):
    assert "unfold/css/styles" in rendered_html
    assert "unfold/js/app" in rendered_html
    assert "testproject/admin" in rendered_html
    assert "testproject/django-ray" in rendered_html

assert "django-ray" in index_html
assert "testproject/landing-graph-bg" in login_html
assert "--color-primary-400:" in index_html
assert "--border-radius: 8px" in index_html
assert reverse("admin:django_ray_taskattempt_changelist") not in index_html
assert "retry_tasks" in changelist_html
assert "cancel_tasks" in changelist_html
compact_columns = [
    "column-id",
    "column-state_display",
    "column-task_display",
    "column-queue_display",
    "column-priority",
    "column-attempt_display",
    "column-ray_dashboard_link",
    "column-created_display",
    "column-started_display",
    "column-finished_display",
]
assert [changelist_html.index(marker) for marker in compact_columns] == sorted(
    changelist_html.index(marker) for marker in compact_columns
)
for removed_column in (
    "column-execution_generation",
    "column-workflow_run_id",
    "column-workflow_plan_fingerprint",
    "column-workflow_plan_pinned_attempt",
):
    assert removed_column not in changelist_html
assert "django-ray-live-observability" in change_html
assert "django_ray/admin/task_live" in change_html
assert "django_ray/admin/workflow_diagnostics" in change_html
assert "django-ray-live__grid" in change_html
assert "django-ray-workflow-diagnostics" in change_html
assert "django-ray-workflow__summary" in change_html
assert 'aria-labelledby="django-ray-live-heading"' in change_html
assert change_html.count('role="status"') == 2
assert "Workflow execution" in change_html
workflow_paths = {
    "data-topology-nodes-url": reverse(
        "admin:django_ray_raytaskexecution_workflow_topology_nodes",
        args=[execution.pk],
    ),
    "data-topology-edges-url": reverse(
        "admin:django_ray_raytaskexecution_workflow_topology_edges",
        args=[execution.pk],
    ),
    "data-node-details-url": reverse(
        "admin:django_ray_raytaskexecution_workflow_node_details",
        args=[execution.pk],
    ),
}
for attribute, workflow_path in workflow_paths.items():
    assert f'{attribute}="{workflow_path}"' in change_html
    assert f'href="{workflow_path}"' not in change_html
assert "unfold-runtime-profile" in change_html
assert "c" * 64 in change_html
assert "Runtime env json" not in change_html
assert "unfold-runtime-secret-marker" not in change_html
assert "user:pass@private.example" not in change_html
assert "Execution metadata is read-only" in change_html
assert 'name="_save"' not in change_html
assert 'name="_continue"' not in change_html
assert (
    reverse(
        "admin:django_ray_raytaskexecution_delete",
        args=[execution.pk],
    )
    not in change_html
)
for immutable_field in (
    "priority",
    "queue_name",
    "state",
    "attempt_number",
    "execution_generation",
    "claimed_by_worker",
):
    assert f'name="{immutable_field}"' not in change_html
assert "Attempt history" in change_html
assert "unfold-attempt-inline-marker" in change_html
assert str(attempt) not in change_html
assert change_html.count(attempt_detail_url) == 1
assert "field-attempt_detail_link" in change_html

retry_response = authenticated.post(
    reverse("admin:django_ray_raytaskexecution_changelist"),
    {
        "action": "retry_tasks",
        "_selected_action": [str(failed_execution.pk)],
    },
)
cancel_response = authenticated.post(
    reverse("admin:django_ray_raytaskexecution_changelist"),
    {
        "action": "cancel_tasks",
        "_selected_action": [str(queued_execution.pk)],
    },
)
assert retry_response.status_code == 302
assert cancel_response.status_code == 302
failed_execution.refresh_from_db()
queued_execution.refresh_from_db()
assert failed_execution.state == TaskState.QUEUED
assert queued_execution.state == TaskState.CANCELLED

stylesheet_match = re.search(
    r'''href=["'](?P<path>/static/unfold/css/styles[^"']*\.css)["']''',
    index_html,
)
assert stylesheet_match is not None
stylesheet_response = authenticated.get(
    html.unescape(stylesheet_match.group("path")),
)
assert stylesheet_response.status_code == 200
assert stylesheet_response["Content-Type"].startswith("text/css")
assert "immutable" in stylesheet_response["Cache-Control"]
stylesheet_body = b"".join(stylesheet_response.streaming_content)
stylesheet_response.close()
assert stylesheet_body.strip()

custom_stylesheet_response = authenticated.get(custom_stylesheet_path)
assert custom_stylesheet_response.status_code == 200
assert custom_stylesheet_response["Content-Type"].startswith("text/css")
assert "immutable" in custom_stylesheet_response["Cache-Control"]
custom_stylesheet_body = b"".join(custom_stylesheet_response.streaming_content)
custom_stylesheet_response.close()
custom_stylesheet_text = custom_stylesheet_body.decode("utf-8")
assert b"--django-ray-admin-accent" in custom_stylesheet_body
assert b"--django-ray-admin-dark-canvas: #0b0c0f" in custom_stylesheet_body
assert b"--django-ray-admin-dark-surface: #16171a" in custom_stylesheet_body
assert b"--django-ray-admin-dark-surface-raised: #212226" in custom_stylesheet_body
assert b"--django-ray-admin-dark-border: #303238" in custom_stylesheet_body
assert b"--django-ray-admin-dark-text: #f4f4f5" in custom_stylesheet_body
assert b"--django-ray-admin-dark-muted: #a1a1aa" in custom_stylesheet_body
assert b"--color-base-100: #f4f4f5" in custom_stylesheet_body
assert b"--color-base-400: #a1a1aa" in custom_stylesheet_body
assert b"--color-base-700: var(--django-ray-admin-dark-border)" in custom_stylesheet_body
assert b"--color-base-800: var(--django-ray-admin-dark-surface-raised)" in (
    custom_stylesheet_body
)
assert b"--color-base-900: var(--django-ray-admin-dark-surface)" in (
    custom_stylesheet_body
)
assert b"--color-base-950: var(--django-ray-admin-dark-canvas)" in (
    custom_stylesheet_body
)
assert b"--color-primary-600: var(--django-ray-admin-accent-strong)" in (
    custom_stylesheet_body
)
assert b"html.dark #changelist-actions > div" in custom_stylesheet_body
assert b'html.dark #changelist-actions button[name="index"]' in custom_stylesheet_body
assert b"django-ray" in custom_stylesheet_body
for selector in (
    "html.dark body.login #page",
    "html.dark body:not(.login) #main",
):
    rule = re.search(
        rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}",
        custom_stylesheet_text,
    )
    assert rule is not None
    assert "background: var(--django-ray-admin-dark-canvas)" in rule.group("body")
    assert "gradient" not in rule.group("body")
light_login_rule = re.search(
    rf"{re.escape('html:not(.dark) body.login #page')}\s*\{{(?P<body>[^}}]*)\}}",
    custom_stylesheet_text,
)
assert light_login_rule is not None
assert "radial-gradient" in light_login_rule.group("body")
assert "linear-gradient" in light_login_rule.group("body")

live_stylesheet_match = re.search(
    r'''href=["'](?P<path>/static/django_ray/admin/task_live[^"']*\.css)["']''',
    change_html,
)
assert live_stylesheet_match is not None
live_stylesheet_response = authenticated.get(
    html.unescape(live_stylesheet_match.group("path")),
)
assert live_stylesheet_response.status_code == 200
assert live_stylesheet_response["Content-Type"].startswith("text/css")
assert "immutable" in live_stylesheet_response["Cache-Control"]
live_stylesheet_body = b"".join(live_stylesheet_response.streaming_content)
live_stylesheet_response.close()
assert b"#django-ray-live-observability" in live_stylesheet_body
assert b".django-ray-workflow__summary" in live_stylesheet_body
assert b"grid-template-columns: repeat(4, minmax(0, 1fr))" in live_stylesheet_body
assert b".django-ray-workflow__chip" in live_stylesheet_body
assert b":focus-visible" in live_stylesheet_body

workflow_script_match = re.search(
    r'''src=["'](?P<path>/static/django_ray/admin/workflow_diagnostics[^"']*\.js)["']''',
    change_html,
)
assert workflow_script_match is not None
workflow_script_response = authenticated.get(
    html.unescape(workflow_script_match.group("path")),
)
assert workflow_script_response.status_code == 200
assert "javascript" in workflow_script_response["Content-Type"]
assert "immutable" in workflow_script_response["Cache-Control"]
workflow_script_body = b"".join(workflow_script_response.streaming_content)
workflow_script_response.close()
assert b"django-ray-workflow-diagnostics" in workflow_script_body
assert b"credentials: \"same-origin\"" in workflow_script_body
assert b"innerHTML" not in workflow_script_body

site_icon_response = authenticated.get(site_icon_path)
assert site_icon_response.status_code == 200
assert site_icon_response["Content-Type"].startswith("image/svg+xml")
assert "immutable" in site_icon_response["Cache-Control"]
site_icon_body = b"".join(site_icon_response.streaming_content)
site_icon_response.close()
assert b'aria-label="django-ray"' in site_icon_body

payload = observability.json()
assert payload["id"] == execution.pk
assert payload["state"] == TaskState.QUEUED

print(
    json.dumps(
        {
            "admin": type(admin.site).__name__,
            "attempt_detail": attempt_detail.status_code,
            "attempt_inline": "passed",
            "branding": "passed",
            "change_view": change.status_code,
            "changelist": changelist.status_code,
            "collectstatic": "passed",
            "index": index.status_code,
            "layout": "passed",
            "login": login.status_code,
            "observability": observability.status_code,
            "static": stylesheet_response.status_code,
            "themed_actions": "passed",
        },
        sort_keys=True,
    )
)
"""

STANDARD_ADMIN_PROBE = r"""
import json
import sys
from types import SimpleNamespace

from django.conf import settings

settings.configure(
    SECRET_KEY="standard-admin-fallback",
    INSTALLED_APPS=[
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.messages",
        "django_ray",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    DJANGO_RAY={"RAY_ADDRESS": "local"},
)

import django

django.setup()

from django.contrib import admin
from django.test import RequestFactory

from django_ray.admin import (
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
    TaskAttemptInline,
    TaskWorkerLeaseAdmin,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

assert "unfold" not in sys.modules
expected_admins = {
    RayTaskExecution: RayTaskExecutionAdmin,
    TaskAttempt: TaskAttemptAdmin,
    TaskWorkerLease: TaskWorkerLeaseAdmin,
}
for model, expected_admin in expected_admins.items():
    registered_admin = admin.site._registry[model]
    assert isinstance(registered_admin, expected_admin)
    assert isinstance(registered_admin, admin.ModelAdmin)
execution_admin = admin.site._registry[RayTaskExecution]
execution_fields = {
    field
    for _, options in execution_admin.fieldsets
    for field in options.get("fields", ())
}
assert "runtime_env_json" not in execution_admin.readonly_fields
assert "runtime_env_json" not in execution_fields
assert {"runtime_env_profile", "runtime_env_hash"} <= execution_fields
assert execution_fields <= set(execution_admin.readonly_fields)
assert execution_admin.has_add_permission(None) is False
assert execution_admin.has_delete_permission(None) is False
permission_request = RequestFactory().get("/admin/")
permission_request.user = SimpleNamespace(has_perm=lambda permission: True)
probe_execution = RayTaskExecution(
    task_id="standard-permission-probe",
    callable_path="testproject.tasks.add_numbers",
)
assert execution_admin.has_change_permission(permission_request) is True
assert execution_admin.has_change_permission(permission_request, probe_execution) is False
assert set(execution_admin.get_actions(permission_request)) == {
    "retry_tasks",
    "cancel_tasks",
}
assert issubclass(TaskAttemptInline, admin.TabularInline)
assert TaskAttemptInline in admin.site._registry[RayTaskExecution].get_inlines(
    None,
    RayTaskExecution(
        task_id="standard-inline-probe",
        callable_path="testproject.tasks.add_numbers",
    ),
)

print(
    json.dumps(
        {
            "admin": type(admin.site).__name__,
            "attempt_inline": "passed",
            "unfold_imported": False,
        }
    )
)
"""


def _run_probe(code: str, *, environment: dict[str, str] | None = None) -> dict[str, object]:
    env = os.environ.copy()
    env.update(environment or {})
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_testproject_renders_unfold_admin_contract(tmp_path: Path) -> None:
    payload = _run_probe(
        THEMED_ADMIN_PROBE,
        environment={
            "DATABASE_ENGINE": "django.db.backends.sqlite3",
            "DATABASE_NAME": ":memory:",
            "DJANGO_ALLOWED_HOSTS": "testserver,localhost",
            "DJANGO_SETTINGS_MODULE": "testproject.settings",
            "DJANGO_STATIC_ROOT": str(tmp_path / "staticfiles"),
        },
    )

    assert payload == {
        "admin": "UnfoldAdminSite",
        "attempt_detail": 200,
        "attempt_inline": "passed",
        "branding": "passed",
        "change_view": 200,
        "changelist": 200,
        "collectstatic": "passed",
        "index": 200,
        "layout": "passed",
        "login": 200,
        "observability": 200,
        "static": 200,
        "themed_actions": "passed",
    }


def test_package_admin_uses_standard_django_without_unfold_enabled() -> None:
    payload = _run_probe(STANDARD_ADMIN_PROBE)

    assert payload["attempt_inline"] == "passed"
    assert payload["unfold_imported"] is False
