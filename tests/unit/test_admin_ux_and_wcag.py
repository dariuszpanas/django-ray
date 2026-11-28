"""Unit and Django Admin UX, WCAG AA contrast, and query-efficiency tests.

Covers Wave 2 Admin improvements across #832 (WCAG AA CSP-safe badges,
in-Admin DAG graph routing, detail UX polish), #818 (oversize digest-only
stub badge), and #825 (F-076 O(1) DB-clock TaskWorkerLeaseAdmin queries).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from django_ray.admin import (
    ExecutionProtocolFilter,
    HasWorkflowFilter,
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
    TaskAttemptInline,
    TaskWorkerLeaseAdmin,
    _state_badge_html,
)
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    TaskWorkerLease,
    TaskWorkerLeasePolicy,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DIAGNOSTICS_CSS = (
    _REPO_ROOT / "src" / "django_ray" / "static" / "django_ray" / "admin" / "diagnostics.css"
)
_TASK_LIVE_CSS = (
    _REPO_ROOT / "src" / "django_ray" / "static" / "django_ray" / "admin" / "task_live.css"
)
_ADMIN_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]


def _relative_luminance(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    assert len(raw) == 6, f"Expected 6-digit hex color, got {hex_color!r}"
    channels = [int(raw[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = _relative_luminance(fg_hex)
    l2 = _relative_luminance(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _strip_at_media_blocks(css_text: str) -> str:
    """Remove @media blocks linearly without nested-quantifier backtracking."""
    out: list[str] = []
    index = 0
    length = len(css_text)
    while index < length:
        media_pos = css_text.find("@media", index)
        if media_pos == -1:
            out.append(css_text[index:])
            break
        out.append(css_text[index:media_pos])
        brace_pos = css_text.find("{", media_pos)
        if brace_pos == -1:
            break
        depth = 1
        cursor = brace_pos + 1
        while cursor < length and depth > 0:
            ch = css_text[cursor]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            cursor += 1
        index = cursor
    return "".join(out)


def _extract_badge_colors(css_text: str, *, dark: bool) -> dict[str, tuple[str, str]]:
    """Extract (color, background) hex pairs for every .djray-badge--<slug> rule."""
    # Strip @media blocks when parsing explicit light / :is(html.dark, ...) selectors.
    top_level = _strip_at_media_blocks(css_text)
    rule_pattern = re.compile(r"(?P<selectors>[^{}@]+)\{(?P<body>[^{}]*)\}", re.DOTALL)
    result: dict[str, tuple[str, str]] = {}
    for match in rule_pattern.finditer(top_level):
        body = match.group("body")
        fg = re.search(r"(?<![a-z-])color:\s*(#[0-9a-fA-F]{6})\s*;", body)
        bg = re.search(r"background:\s*(#[0-9a-fA-F]{6})\s*;", body)
        if not (fg and bg):
            continue
        for raw_selector in re.split(r",(?![^(]*\))", match.group("selectors")):
            selector = " ".join(raw_selector.split())
            is_dark = ':is(html.dark, html[data-theme="dark"])' in selector
            if is_dark != dark:
                continue
            slug_match = re.search(r"\.djray-badge--([a-z]+)\b", selector)
            if slug_match:
                result[slug_match.group(1)] = (fg.group(1), bg.group(1))
    return result


def _make_staff_user(*, username: str, permissions: tuple[tuple[type, str], ...]):
    user_model = get_user_model()
    user = user_model.objects.create_user(
        username=username,
        password="test-password-123!",
        is_staff=True,
    )
    for model, codename in permissions:
        content_type = ContentType.objects.get_for_model(model)
        perm = Permission.objects.get(content_type=content_type, codename=codename)
        user.user_permissions.add(perm)
    return user


class TestWcagContrastAndCssTokens:
    """Verify WCAG 2.1 AA contrast and CSP-safe CSS rules directly from stylesheets."""

    EXPECTED_BADGES = {
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "cancelling",
        "lost",
        "expired",
        "warning",
        "stub",
    }

    def test_all_state_badges_meet_wcag_aa_contrast_in_light_and_dark_modes(self) -> None:
        css = _DIAGNOSTICS_CSS.read_text(encoding="utf-8")
        light_pairs = _extract_badge_colors(css, dark=False)
        dark_pairs = _extract_badge_colors(css, dark=True)

        assert self.EXPECTED_BADGES <= set(light_pairs)
        assert self.EXPECTED_BADGES <= set(dark_pairs)

        for slug in sorted(self.EXPECTED_BADGES):
            light_ratio = _contrast_ratio(*light_pairs[slug])
            dark_ratio = _contrast_ratio(*dark_pairs[slug])
            assert light_ratio >= 4.5, (
                f"Light badge .djray-badge--{slug} contrast {light_ratio:.2f} < 4.5"
            )
            assert dark_ratio >= 4.5, (
                f"Dark badge .djray-badge--{slug} contrast {dark_ratio:.2f} < 4.5"
            )

    def test_downstream_failed_dag_nodes_use_danger_palette_and_inspector_modal_exists(
        self,
    ) -> None:
        css = _TASK_LIVE_CSS.read_text(encoding="utf-8")
        assert '.django-ray-workflow-graph__node[data-state="FAILED"]' in css
        assert ".django-ray-node-inspector__dialog" in css
        assert ".django-ray-live__indicator[data-polling-state=" in css
        assert "prefers-reduced-motion: reduce" in css

    @pytest.mark.parametrize("state", list(TaskState.values))
    def test_state_badge_html_is_csp_safe_with_zero_inline_styles(self, state: str) -> None:
        rendered = _state_badge_html(state)
        assert "style=" not in rendered
        assert f"djray-badge--{state.lower()}" in rendered
        assert f'data-state="{state}"' in rendered
        assert f">{state}<" in rendered

    def test_unknown_state_falls_back_to_neutral_badge_without_inline_style(self) -> None:
        rendered = _state_badge_html("CUSTOM_UNKNOWN")
        assert "style=" not in rendered
        assert "djray-badge--unknown" in rendered
        assert "CUSTOM_UNKNOWN" in rendered


@pytest.mark.django_db
class TestAdminBadgesStubsAndDagRouting:
    """Verify consistent badges, oversize stub badge, and in-Admin DAG routing."""

    def test_state_badges_are_consistent_across_execution_attempt_and_inline(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="ux-badge-consistency-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
        )
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)
        inline = TaskAttemptInline(TaskAttempt, admin.site)

        for rendered in (
            exec_admin.state_display(execution),
            attempt_admin.state_display(attempt),
            inline.state_display(attempt),
        ):
            assert "djray-badge djray-badge--failed" in rendered
            assert 'data-state="FAILED"' in rendered
            assert "style=" not in rendered

        assert "state_display" in attempt_admin.list_display
        assert "state_display" in attempt_admin.fields
        assert "state_display" in inline.fields

    def test_oversize_result_reference_renders_digest_only_stub_badge(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="ux-oversize-stub-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            result_reference="oversize://sha256:abcdef0123456789",
            result_data='{"digest_only": true, "bytes": 2097152}',
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.SUCCEEDED,
            result_reference="oversize://sha256:abcdef0123456789",
            result_data=None,
        )
        normal = RayTaskExecution.objects.create(
            task_id="ux-normal-ref-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            result_reference="file:///var/results/ok.json",
            result_data='{"ok": true}',
        )
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)

        exec_rendered = exec_admin.result_data_display(execution)
        assert "djray-badge--stub" in exec_rendered
        assert "Digest-only stub (&gt;1 MiB; configure RESULT_STORAGE_BACKEND)" in exec_rendered
        assert "django-ray-diagnostic--json" in exec_rendered
        assert "&quot;digest_only&quot;: true" in exec_rendered

        attempt_rendered = attempt_admin.result_data_display(attempt)
        assert "djray-badge--stub" in attempt_rendered
        assert "Digest-only stub (&gt;1 MiB; configure RESULT_STORAGE_BACKEND)" in attempt_rendered

        normal_rendered = exec_admin.result_data_display(normal)
        assert "djray-badge--stub" not in normal_rendered
        assert "&quot;ok&quot;: true" in normal_rendered

    def test_attempt_workflow_graph_link_routes_to_html_admin_view_with_secondary_json(
        self,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="ux-attempt-graph-link-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            workflow_run_id="00000000-0000-0000-0000-000000880001",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.FAILED,
        )
        attempt.admin_has_workflow_graph = True

        attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)
        rendered = attempt_admin.workflow_graph_link(attempt)

        expected_html_url = (
            reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
            + "#workflow-graph-attempt-2"
        )
        expected_json_url = (
            reverse("admin:django_ray_raytaskexecution_workflow_graph", args=[execution.pk])
            + "?attempt_number=2"
        )
        assert f'href="{expected_html_url}"' in rendered
        assert "Open graph for attempt #2" in rendered
        assert f'href="{expected_json_url}"' in rendered
        assert 'target="_blank"' in rendered
        assert 'rel="noopener noreferrer"' in rendered
        assert "Graph JSON" in rendered


@pytest.mark.django_db
class TestExecutionDetailAndRetryUx:
    """Verify elevated live panel, detail cancel action, accordion hiding, and retry card."""

    @pytest.fixture(autouse=True)
    def _admin_middleware(self, settings) -> None:
        settings.MIDDLEWARE = _ADMIN_MIDDLEWARE

    def test_live_panel_elevated_above_fieldsets_and_plain_task_hides_workflow_accordion(
        self, client
    ) -> None:
        superuser = get_user_model().objects.create_superuser(username="ux-elevated-admin")
        plain_running = RayTaskExecution.objects.create(
            task_id="ux-plain-running-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            error_traceback="Traceback (most recent call last):\n  ValueError: boom",
            kwargs_json='{"alpha": 1}',
        )
        plain_terminal = RayTaskExecution.objects.create(
            task_id="ux-plain-terminal-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
        )
        workflow_running = RayTaskExecution.objects.create(
            task_id="ux-workflow-running-001",
            callable_path="testproject.tasks.workflow_task",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000880002",
        )
        client.force_login(superuser)

        running_resp = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[plain_running.pk])
        )
        assert running_resp.status_code == 200
        running_html = running_resp.content.decode("utf-8")

        live_idx = running_html.index('id="django-ray-live-observability"')
        actions_idx = running_html.index('id="django-ray-task-actions"')
        task_info_idx = running_html.index("Task Info")
        assert live_idx < task_info_idx
        assert actions_idx < task_info_idx
        assert 'data-initial-state="RUNNING"' in running_html
        assert 'data-has-workflow="false"' in running_html
        assert 'data-action="reload"' in running_html
        assert 'id="django-ray-workflow-diagnostics"' in running_html
        assert "Not a workflow task." in running_html or "No workflow recorded yet." in running_html
        assert "Cancel task" in running_html
        cancel_url = reverse("admin:django_ray_raytaskexecution_cancel", args=[plain_running.pk])
        assert f'formaction="{cancel_url}"' in running_html
        assert "django-ray-diagnostic--traceback" in running_html
        assert "django-ray-diagnostic--json" in running_html

        terminal_resp = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[plain_terminal.pk])
        )
        assert terminal_resp.status_code == 200
        terminal_html = terminal_resp.content.decode("utf-8")
        assert 'data-initial-state="SUCCEEDED"' in terminal_html
        assert 'data-polling-state="terminal"' in terminal_html
        assert "Execution is in terminal state SUCCEEDED. Live polling is stopped." in terminal_html
        assert "Waiting for live task status..." not in terminal_html
        assert "Cancel task" not in terminal_html

        wf_resp = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[workflow_running.pk])
        )
        assert wf_resp.status_code == 200
        wf_html = wf_resp.content.decode("utf-8")
        assert 'data-has-workflow="true"' in wf_html
        assert 'id="django-ray-workflow-diagnostics"' in wf_html
        assert 'id="workflow-graph"' in wf_html

    def test_detail_cancel_view_enforces_method_authz_and_fences_cancellation(
        self, client, monkeypatch
    ) -> None:
        from django_ray import admin as admin_module
        from django_ray.lifecycle import (
            TaskCancellationRequestResult,
            TaskCancellationRequestStatus,
        )

        execution = RayTaskExecution.objects.create(
            task_id="ux-detail-cancel-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.QUEUED,
            attempt_number=2,
            execution_generation=3,
        )
        viewer = _make_staff_user(
            username="ux-cancel-viewer",
            permissions=((RayTaskExecution, "view_raytaskexecution"),),
        )
        changer = _make_staff_user(
            username="ux-cancel-changer",
            permissions=(
                (RayTaskExecution, "view_raytaskexecution"),
                (RayTaskExecution, "change_raytaskexecution"),
            ),
        )
        cancel_url = reverse("admin:django_ray_raytaskexecution_cancel", args=[execution.pk])

        # View-only user does not see Cancel button and gets 403 on POST (including missing ID).
        client.force_login(viewer)
        detail = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
        )
        assert "Cancel task" not in detail.content.decode("utf-8")
        assert client.post(cancel_url).status_code == 403
        missing_url = reverse("admin:django_ray_raytaskexecution_cancel", args=[999_999])
        assert client.post(missing_url).status_code == 403

        # Authorized changer: GET returns 405 with nosniff + no-store; POST forwards fencing args.
        client.force_login(changer)
        get_resp = client.get(cancel_url)
        assert get_resp.status_code == 405
        assert get_resp["X-Content-Type-Options"] == "nosniff"
        assert "no-store" in get_resp["Cache-Control"]

        calls: list[dict[str, object]] = []

        def fake_cancel(pk, **kwargs):
            calls.append({"pk": pk, **kwargs})
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.ACCEPTED,
                execution_id=execution.pk,
                attempt_number=2,
                execution_generation=3,
                state=TaskState.CANCELLED,
            )

        monkeypatch.setattr(admin_module, "request_task_cancellation", fake_cancel)
        post_resp = client.post(cancel_url)
        assert post_resp.status_code == 302
        assert post_resp["X-Content-Type-Options"] == "nosniff"
        assert "no-store" in post_resp["Cache-Control"]
        assert calls == [
            {
                "pk": execution.pk,
                "expected_attempt_number": 2,
                "expected_execution_generation": 3,
            }
        ]

    def test_detail_cancel_view_enforces_per_object_action_permission(self) -> None:
        denied_exec = RayTaskExecution.objects.create(
            task_id="ux-detail-cancel-obj-deny",
            callable_path="testproject.tasks.add_numbers",
            queue_name="restricted",
            state=TaskState.QUEUED,
        )

        class QueueScopedAdmin(RayTaskExecutionAdmin):
            def has_view_permission(self, request, obj=None) -> bool:
                if not super().has_view_permission(request, obj):
                    return False
                return obj is None or obj.queue_name == "public"

        site = AdminSite(name="admin")
        site.register(RayTaskExecution, QueueScopedAdmin)
        exec_admin = site._registry[RayTaskExecution]
        user = _make_staff_user(
            username="ux-cancel-scoped",
            permissions=((RayTaskExecution, "change_raytaskexecution"),),
        )
        request = RequestFactory().post(
            f"/admin/django_ray/raytaskexecution/{denied_exec.pk}/cancel/"
        )
        request.user = user
        with pytest.raises(PermissionDenied):
            exec_admin.cancel_view(request, str(denied_exec.pk))

    def test_retry_confirmation_has_no_inline_style_and_single_h1(self, client) -> None:
        superuser = get_user_model().objects.create_superuser(username="ux-retry-card-admin")
        failed = RayTaskExecution.objects.create(
            task_id="ux-retry-card-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        client.force_login(superuser)

        response = client.post(
            reverse("admin:django_ray_raytaskexecution_retry", args=[failed.pk]),
        )
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert "<style" not in html
        assert "django_ray/admin/diagnostics.css" in html
        assert len(re.findall(r"<h1\b", html)) == 1
        assert "Confirm full task retry" in html

    def test_filters_and_compact_dashboard_placeholder(self) -> None:
        wf_exec = RayTaskExecution.objects.create(
            task_id="ux-filter-wf-001",
            callable_path="testproject.tasks.workflow_task",
            state=TaskState.QUEUED,
            workflow_run_id="00000000-0000-0000-0000-000000880003",
            ray_job_id="02000000:abcdef0123456789",
        )
        plain_exec = RayTaskExecution.objects.create(
            task_id="ux-filter-plain-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.QUEUED,
        )
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        request = RequestFactory().get("/admin/django_ray/raytaskexecution/")

        protocol_filter = ExecutionProtocolFilter(request, {}, RayTaskExecution, exec_admin)
        assert ("1", "Protocol 1") in protocol_filter.lookup_choices
        assert ("other", "Other (non-v1)") in protocol_filter.lookup_choices

        wf_yes = HasWorkflowFilter(request, {"has_workflow": ["yes"]}, RayTaskExecution, exec_admin)
        wf_no = HasWorkflowFilter(request, {"has_workflow": ["no"]}, RayTaskExecution, exec_admin)
        qs = RayTaskExecution.objects.filter(pk__in=[wf_exec.pk, plain_exec.pk])
        assert list(wf_yes.queryset(request, qs)) == [wf_exec]
        assert list(wf_no.queryset(request, qs)) == [plain_exec]

        with override_settings(RAY_DASHBOARD_URL=None):
            rendered = exec_admin.ray_dashboard_link(wf_exec)
        assert "djray-muted-hint" in rendered
        assert "configure RAY_DASHBOARD_URL" in rendered
        assert ">—<" in rendered


@pytest.mark.django_db
class TestWorkerLeaseQueryEfficiencyAndPolish:
    """Verify F-076 O(1) lease changelist queries and F-095 Admin polish."""

    @pytest.fixture(autouse=True)
    def _admin_middleware(self, settings) -> None:
        settings.MIDDLEWARE = _ADMIN_MIDDLEWARE

    def test_worker_lease_changelist_uses_constant_queries_without_policy_n_plus_one(
        self, client
    ) -> None:
        superuser = get_user_model().objects.create_superuser(username="ux-lease-perf-admin")
        client.force_login(superuser)
        changelist_url = reverse("admin:django_ray_taskworkerlease_changelist") + "?is_active=all"

        now = datetime.now(UTC)
        TaskWorkerLease.objects.create(
            worker_id="lease-perf-00",
            hostname="host-0",
            pid=1000,
            queue_name="default",
            last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=60),
            is_active=True,
        )
        with CaptureQueriesContext(connection) as one_row_queries:
            resp_one = client.get(changelist_url)
        assert resp_one.status_code == 200

        for idx in range(1, 15):
            is_active = idx % 3 != 0
            expired = idx % 2 == 0
            TaskWorkerLease.objects.create(
                worker_id=f"lease-perf-{idx:02d}-long-identifier",
                hostname=f"host-{idx % 3}",
                pid=1000 + idx,
                queue_name="default",
                last_heartbeat_at=now - timedelta(minutes=10 if expired else 0),
                lease_expires_at=(
                    now - timedelta(seconds=30) if expired else now + timedelta(seconds=60)
                ),
                is_active=is_active,
            )

        with CaptureQueriesContext(connection) as many_row_queries:
            resp_many = client.get(changelist_url)
        assert resp_many.status_code == 200

        # Zero per-row policy queries: total query count is identical for 1 vs 15 leases.
        assert len(many_row_queries) == len(one_row_queries)
        policy_selects = [
            q["sql"] for q in many_row_queries if "django_ray_taskworkerleasepolicy" in q["sql"]
        ]
        # O(1) policy queries independent of row count (count + page fetch), zero N+1.
        assert len(policy_selects) <= 4

    def test_worker_lease_annotation_matches_db_clock_freshness_and_policy_failure(self) -> None:
        now = datetime.now(UTC)
        fresh = TaskWorkerLease.objects.create(
            worker_id="short-id",
            hostname="host-fresh",
            pid=2001,
            last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=60),
            is_active=True,
        )
        expired_by_clock = TaskWorkerLease.objects.create(
            worker_id="expired-worker-1234567890",
            hostname="host-expired",
            pid=2002,
            last_heartbeat_at=now,
            lease_expires_at=now - timedelta(seconds=5),
            is_active=True,
        )
        inactive = TaskWorkerLease.objects.create(
            worker_id="inactive-worker-1234567890",
            hostname="host-inactive",
            pid=2003,
            last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=60),
            is_active=False,
        )
        lease_admin = TaskWorkerLeaseAdmin(TaskWorkerLease, admin.site)
        request = RequestFactory().get("/admin/django_ray/taskworkerlease/")

        rows = {row.worker_id: row for row in lease_admin.get_queryset(request)}
        with CaptureQueriesContext(connection) as display_queries:
            assert lease_admin.is_active_display_list(rows[fresh.worker_id]) is True
            assert lease_admin.is_active_display_list(rows[expired_by_clock.worker_id]) is False
            assert lease_admin.is_active_display_list(rows[inactive.worker_id]) is False
        assert len(display_queries) == 0

        # Short ID <= 12 chars is not truncated with "..." and still gets a tooltip.
        assert lease_admin.worker_id_short(fresh) == '<span title="short-id">short-id</span>'
        assert (
            lease_admin.worker_id_short(expired_by_clock)
            == '<span title="expired-worker-1234567890">expired-work...</span>'
        )
        assert "<time " in lease_admin.started_display(fresh)
        assert "<time " in lease_admin.last_heartbeat_display(fresh)
        assert "lease_expires_at" in lease_admin.readonly_fields
        assert "lease_policy_revision" in lease_admin.readonly_fields
        config_desc = dict(lease_admin.fieldsets)["Configuration"]["description"]
        assert "Changing the queue here" not in config_desc
        assert "read-only" in config_desc

        # Missing lease policy reports None for active leases and False for inactive leases.
        TaskWorkerLeasePolicy.objects.all().delete()
        rows_no_policy = {row.worker_id: row for row in lease_admin.get_queryset(request)}
        assert lease_admin.is_active_display_list(rows_no_policy[fresh.worker_id]) is None
        assert lease_admin.is_active_display_list(rows_no_policy[inactive.worker_id]) is False

    def test_sensitive_task_data_page_renders_complete_breadcrumbs(self, client) -> None:
        superuser = get_user_model().objects.create_superuser(username="ux-sensitive-crumb-admin")
        execution = RayTaskExecution.objects.create(
            task_id="ux-sensitive-crumb-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json='["ok"]',
        )
        client.force_login(superuser)
        url = reverse("admin:django_ray_raytaskexecution_sensitive_data", args=[execution.pk])
        response = client.get(url)
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert reverse("admin:index") in html
        assert reverse("admin:django_ray_raytaskexecution_changelist") in html
        assert reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk]) in html
        assert "Redacted task detail" in html
        assert "Unredacted task data" in html
        # Confirm resolver_match sanity when calling directly too
        assert resolve(url).url_name == "django_ray_raytaskexecution_sensitive_data"
