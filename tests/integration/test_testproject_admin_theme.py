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
from django.test import Client
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
assert settings.UNFOLD == {
    "SITE_TITLE": "django-ray admin",
    "SITE_HEADER": "django-ray",
    "SITE_SUBHEADER": "Distributed task testproject",
}
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

call_command("migrate", interactive=False, verbosity=0)
call_command("collectstatic", interactive=False, verbosity=0)

static_root = Path(settings.STATIC_ROOT)
assert (static_root / "unfold/css/styles.css").is_file()
assert (static_root / "unfold/js/app.js").is_file()
assert (static_root / "django_ray/admin/task_live.js").is_file()

user = get_user_model().objects.create_superuser(
    username="unfold-admin-probe",
    password="unfold-admin-probe-password",
)
execution = RayTaskExecution.objects.create(
    task_id="unfold-admin-execution",
    callable_path="testproject.tasks.add_numbers",
    state=TaskState.QUEUED,
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

assert "django-ray" in index_html
assert reverse("admin:django_ray_taskattempt_changelist") not in index_html
assert "retry_tasks" in changelist_html
assert "cancel_tasks" in changelist_html
assert "django-ray-live-observability" in change_html
assert "django_ray/admin/task_live" in change_html
assert "Attempt history" in change_html
assert "unfold-attempt-inline-marker" in change_html
assert attempt_detail_url in change_html

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

payload = observability.json()
assert payload["id"] == execution.pk
assert payload["state"] == TaskState.QUEUED

print(
    json.dumps(
        {
            "admin": type(admin.site).__name__,
            "attempt_detail": attempt_detail.status_code,
            "attempt_inline": "passed",
            "change_view": change.status_code,
            "changelist": changelist.status_code,
            "collectstatic": "passed",
            "index": index.status_code,
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
        "change_view": 200,
        "changelist": 200,
        "collectstatic": "passed",
        "index": 200,
        "login": 200,
        "observability": 200,
        "static": 200,
        "themed_actions": "passed",
    }


def test_package_admin_uses_standard_django_without_unfold_enabled() -> None:
    payload = _run_probe(STANDARD_ADMIN_PROBE)

    assert payload["attempt_inline"] == "passed"
    assert payload["unfold_imported"] is False
