"""Regression tests for Wave 4 Admin UI, security, workflow read coalescing, and CLI UX fixes."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import connection
from django.http import HttpRequest
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import django_ray.admin as admin_module
from django_ray.admin import RayTaskExecutionAdmin, TaskAttemptAdmin
from django_ray.admin_views import actions as admin_actions
from django_ray.admin_views import badges as admin_badges
from django_ray.admin_views import diagnostics as admin_diagnostics
from django_ray.management.commands import django_ray_benchmark_polling as benchmark_cmd
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.plans import PlanEligibility, materialize_workflow_plan
from django_ray.workflow.progress.preparation import prepare_workflow_progress_topology
from django_ray.workflow.progress.reads import read_workflow_progress_collections
from django_ray.workflow.progress.storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    stage_workflow_progress_topology,
)
from django_ray.workflows import map_step
from tests.workflow_progress_storage_helpers import (
    workflow_detail,
    workflow_node,
    workflow_summary,
)

_ADMIN_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
_REPO_ROOT = Path(__file__).parents[2]
_TASK_LIVE_CSS = _REPO_ROOT / "src/django_ray/static/django_ray/admin/task_live.css"


def _w4_increment(value: int) -> int:
    return value + 1


def _attach_messages(request: HttpRequest) -> HttpRequest:
    middleware = SessionMiddleware(lambda _req: None)  # type: ignore[arg-type,return-value]
    middleware.process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    return request


def _staff_user(username: str, *codenames: str):
    user = get_user_model().objects.create_user(username=username, is_staff=True)
    if codenames:
        perms = Permission.objects.filter(
            content_type__app_label="django_ray",
            codename__in=codenames,
        )
        user.user_permissions.add(*perms)
    return user


def _create_terminal_schema_v3_workflow(
    *,
    task_id: str,
    run_id: str,
    with_plan: bool = False,
) -> RayTaskExecution:
    terminal_time = timezone.now()
    started_at = (terminal_time - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    detail_finished_at = (terminal_time - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    finished_at = terminal_time.isoformat().replace("+00:00", "Z")
    plan_kwargs: dict[str, Any] = {}
    plan = None
    if with_plan:
        plan = materialize_workflow_plan(
            map_step(_w4_increment),
            invocation_args=([1, 2],),
        ).plan
        selection = PlanEligibility(("dynamic_tasks",), (), 0).select(
            "dynamic_tasks", requested_policy="auto", reporting_policy="full"
        )
        plan_kwargs = {
            "workflow_plan_fingerprint": plan.fingerprint,
            "workflow_plan_pinned_attempt": 1,
            "workflow_plan_json": plan.canonical_json,
            "workflow_plan_selection": json.dumps(selection.as_dict()),
        }
    execution = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=run_id,
        **plan_kwargs,
    )
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=1,
        execution_generation=1,
        run_id=run_id,
    )
    node_ids = ("step-a", "step-b")
    nodes = [workflow_node(node_id) for node_id in node_ids]
    topology = prepare_workflow_progress_topology(
        identity,
        1,
        nodes,
        ({"source": node_ids[0], "target": node_ids[1]},),
    )
    details = []
    for node_id in node_ids:
        detail = workflow_detail(node_id)
        detail.update(
            state="SUCCEEDED",
            started_at=started_at,
            finished_at=detail_finished_at,
        )
        details.append(detail)
    prepared_detail = prepare_workflow_progress_detail(details, topology=topology)
    manifest_id = stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    summary = workflow_summary(
        identity,
        summary_revision=4,
        node_count=len(node_ids),
        running_count=0,
    )
    summary["state"] = "SUCCEEDED"
    summary["node_counts"].update(pending=0, succeeded=len(node_ids))
    summary["edge_counts"].update(declared=1, discovered=1)
    summary["progress_percent"] = 100.0
    summary["timestamps"].update(
        started_at=started_at,
        updated_at=finished_at,
        finished_at=finished_at,
    )
    summary["terminal"].update(outcome="SUCCEEDED", finished_at=finished_at)
    if plan is not None:
        summary["plan_fingerprint"] = plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
    publication = persist_workflow_progress_publication(
        identity,
        summary,
        manifest_id=manifest_id,
        prepared_topology=topology,
        prepared_detail=prepared_detail,
    )
    assert publication.accepted is True
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.SUCCEEDED)
    execution.refresh_from_db()
    return execution


@pytest.mark.django_db
class TestR2022CancelTasksSqlSlice:
    """R2-022: cancel_tasks must slice at SQL level and preload authz fields."""

    def test_cancel_tasks_slices_sql_at_max_tasks_plus_one(self) -> None:
        RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"r2-022-slice-{idx:03d}",
                    callable_path="testproject.tasks.add_numbers",
                    queue_name="default",
                    state=TaskState.QUEUED,
                )
                for idx in range(5)
            ]
        )
        superuser = get_user_model().objects.create_superuser(username="r2-022-slice-admin")
        request = _attach_messages(RequestFactory().post("/admin/django_ray/raytaskexecution/"))
        request.user = superuser
        model_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        cancel_fn = Mock()

        with CaptureQueriesContext(connection) as queries:
            admin_actions.cancel_tasks(
                model_admin,
                request,
                RayTaskExecution.objects.filter(task_id__startswith="r2-022-slice-"),
                cancel_fn=cancel_fn,
                max_tasks=3,
            )

        cancel_fn.assert_not_called()
        select_sqls = [
            q["sql"]
            for q in queries.captured_queries
            if q["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in q["sql"]
        ]
        assert len(select_sqls) == 1
        assert "LIMIT 4" in select_sqls[0].upper()

    def test_cancel_tasks_preloads_authz_fields_without_deferred_n_plus_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"r2-022-authz-{idx}",
                    callable_path="testproject.tasks.add_numbers",
                    queue_name="priority-q",
                    state=TaskState.QUEUED,
                )
                for idx in range(3)
            ]
        )
        superuser = get_user_model().objects.create_superuser(username="r2-022-authz-admin")
        request = _attach_messages(RequestFactory().post("/admin/django_ray/raytaskexecution/"))
        request.user = superuser
        model_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        inspected: list[tuple[str, str, str]] = []

        def custom_action_perm(_req: HttpRequest, obj: RayTaskExecution | None = None) -> bool:
            if obj is not None:
                inspected.append((obj.task_id, obj.queue_name, str(obj.state)))
            return True

        monkeypatch.setattr(model_admin, "has_action_permission", custom_action_perm)
        fake_result = Mock(accepted=True, status=None)
        cancel_fn = Mock(return_value=fake_result)

        with CaptureQueriesContext(connection) as queries:
            admin_actions.cancel_tasks(
                model_admin,
                request,
                RayTaskExecution.objects.filter(task_id__startswith="r2-022-authz-"),
                cancel_fn=cancel_fn,
                max_tasks=10,
            )

        assert len(inspected) == 3
        assert cancel_fn.call_count == 3
        task_selects = [
            q["sql"]
            for q in queries.captured_queries
            if q["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in q["sql"]
        ]
        # Exactly one bounded SELECT query — zero deferred N+1 queries inside has_action_permission.
        assert len(task_selects) == 1


@pytest.mark.django_db
class TestR2025CoalescedWorkflowProgressReads:
    """R2-025: Admin workflow graph and diagnostics views use one coalesced snapshot."""

    def test_workflow_graph_view_uses_single_coalesced_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        execution = _create_terminal_schema_v3_workflow(
            task_id="r2-025-graph-coalesced",
            run_id="00000000-0000-4000-8000-000000002501",
        )
        superuser = get_user_model().objects.create_superuser(username="r2-025-graph-admin")
        request = RequestFactory().get("/admin/workflow/graph/?attempt_number=1")
        request.user = superuser

        coalesced_calls: list[dict[str, Any]] = []

        def spy_read_collections(*args: Any, **kwargs: Any) -> dict[str, Any]:
            coalesced_calls.append(kwargs)
            return read_workflow_progress_collections(*args, **kwargs)

        monkeypatch.setattr(
            admin_module, "read_workflow_progress_collections", spy_read_collections
        )
        admin_obj = RayTaskExecutionAdmin(RayTaskExecution, admin.site)

        response = admin_obj.workflow_graph_view(request, str(execution.pk))

        assert response.status_code == 200
        payload = json.loads(response.content)
        assert payload["status"] == "AVAILABLE"
        assert [node["id"] for node in payload["nodes"]] == ["step-a", "step-b"]
        assert len(coalesced_calls) == 1
        assert coalesced_calls[0]["include_topology_nodes"] is True
        assert coalesced_calls[0]["include_topology_edges"] is True
        assert coalesced_calls[0]["include_node_details"] is True

    def test_workflow_diagnostics_view_uses_single_coalesced_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        execution = _create_terminal_schema_v3_workflow(
            task_id="r2-025-diag-coalesced",
            run_id="00000000-0000-4000-8000-000000002502",
            with_plan=True,
        )
        superuser = get_user_model().objects.create_superuser(username="r2-025-diag-admin")
        request = RequestFactory().get("/admin/workflow/diagnostics/?attempt_number=1")
        request.user = superuser

        coalesced_calls: list[dict[str, Any]] = []

        def spy_read_collections(*args: Any, **kwargs: Any) -> dict[str, Any]:
            coalesced_calls.append(kwargs)
            return read_workflow_progress_collections(*args, **kwargs)

        monkeypatch.setattr(
            admin_module, "read_workflow_progress_collections", spy_read_collections
        )
        admin_obj = RayTaskExecutionAdmin(RayTaskExecution, admin.site)

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))

        assert response.status_code == 200
        payload = json.loads(response.content)
        assert payload["plan"]["status"] == "AVAILABLE"
        assert payload["progress"]["state"] == "AVAILABLE"
        assert payload["progress"]["actions"] == {
            "topology_nodes": True,
            "topology_edges": True,
            "node_details": True,
        }
        assert len(coalesced_calls) == 1
        assert coalesced_calls[0]["limit"] == 1
        assert coalesced_calls[0]["include_legacy"] is True


@pytest.mark.django_db
class TestR2032AdminReplaySnippet:
    """R2-032: Clean copyable read-only replay CLI snippet on Admin execution detail."""

    def test_replay_cli_command_quotes_safely_and_rejects_control_chars(self) -> None:
        assert (
            admin_diagnostics.replay_cli_command("task-abc-123")
            == "python manage.py django_ray_replay task-abc-123"
        )
        assert (
            admin_diagnostics.replay_cli_command("task; rm -rf /")
            == "python manage.py django_ray_replay 'task; rm -rf /'"
        )
        assert admin_diagnostics.replay_cli_command("task\nrm -rf /") is None
        assert admin_diagnostics.replay_cli_command("task\x1b[31m") is None
        assert admin_diagnostics.replay_cli_command("") is None
        assert admin_diagnostics.replay_cli_command(None) is None

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_detail_renders_copyable_replay_snippet_for_viewers(self, client) -> None:
        viewer = _staff_user("r2-032-viewer", "view_raytaskexecution")
        execution = RayTaskExecution.objects.create(
            task_id="r2-032-replay-target",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
        )
        client.force_login(viewer)
        detail_url = reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])

        response = client.get(detail_url)
        html = response.content.decode("utf-8")

        assert response.status_code == 200
        assert "Local read-only replay" in html
        assert "python manage.py django_ray_replay r2-032-replay-target" in html
        assert 'data-action="copy-replay"' in html
        assert (
            'data-replay-command="python manage.py django_ray_replay r2-032-replay-target"' in html
        )


@pytest.mark.django_db
class TestR2033TerminalDotAndBenchmarkCli:
    """R2-033(a) semantic terminal dot and R2-033(d) benchmark_polling CLI help/--format."""

    def test_terminal_indicator_css_colors_only_succeeded_green(self) -> None:
        css = _TASK_LIVE_CSS.read_text(encoding="utf-8")
        assert (
            '.django-ray-live__indicator[data-polling-state="terminal"] {\n  animation: none;\n  background: #94a3b8;'
            in css
        )
        assert (
            '.django-ray-live__indicator[data-polling-state="terminal"][data-state="SUCCEEDED"] {\n  background: #4ade80;'
            in css
        )
        assert '[data-state="FAILED"],' in css
        assert '[data-state="LOST"]' in css
        assert '[data-state="CANCELLED"],' in css
        assert '[data-state="EXPIRED"]' in css

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_detail_indicator_emits_data_state_attribute(self, client) -> None:
        user = get_user_model().objects.create_superuser(username="r2-033-indicator-admin")
        failed = RayTaskExecution.objects.create(
            task_id="r2-033-failed-dot",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        client.force_login(user)

        response = client.get(reverse("admin:django_ray_raytaskexecution_change", args=[failed.pk]))
        html = response.content.decode("utf-8")

        assert response.status_code == 200
        assert 'data-field="indicator"' in html
        assert 'data-polling-state="terminal"' in html
        assert 'data-state="FAILED"' in html

    def test_benchmark_polling_all_arguments_have_help_and_support_format_option(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        command = benchmark_cmd.Command()
        parser = command.create_parser("manage.py", "django_ray_benchmark_polling")
        custom_actions = [
            action
            for action in parser._actions
            if action.dest
            not in {
                "help",
                "version",
                "verbosity",
                "settings",
                "pythonpath",
                "traceback",
                "no_color",
                "force_color",
                "skip_checks",
            }
        ]
        assert len(custom_actions) >= 13
        missing_help = [action.option_strings for action in custom_actions if not action.help]
        assert missing_help == []

        parsed_default = parser.parse_args([])
        assert parsed_default.format == "text"
        assert parsed_default.json_output is False
        parsed_format_json = parser.parse_args(["--format", "json"])
        assert parsed_format_json.format == "json"

        # Verify --format json emits valid JSON just like --json.
        fake_policy = benchmark_cmd.BenchmarkResult(
            policy="fixed",
            workers=1,
            tasks_per_phase=1,
            base_interval_seconds=0.1,
            max_interval_seconds=0.1,
            idle_claim_queries_per_worker_second=1.0,
            idle_total_sql_per_worker_second=1.0,
            idle_peak_overlapping_workers=1,
            idle_cross_worker_overlap_ratio=0.0,
            claim_latency_p50_ms=1.0,
            claim_latency_p95_ms=2.0,
            burst_claim_throughput_per_second=10.0,
        )
        fake_plan = benchmark_cmd.ProtocolPredicatePlanSummary(
            node_shape=("0:limit", "1:index_scan"),
            index_categories=("claimable",),
            estimated_rows=1,
            actual_rows=1,
            actual_loops=1,
            estimated_total_cost=1.0,
        )
        fake_variant = benchmark_cmd.ProtocolPredicateVariantResult(
            name="production",
            protocol_predicate=True,
            duration_samples_ms=(1.0,),
            duration_p50_ms=1.0,
            duration_p95_ms=1.0,
            plan=fake_plan,
        )
        fake_evidence = benchmark_cmd.ProtocolPredicateEvidence(
            schema_version=1,
            method="counterbalanced_paired_explain_analyze",
            seeded_rows=1,
            query_limit=1,
            timed_pairs=1,
            production_first_pairs=1,
            control_first_pairs=0,
            seeded_protocol_version=1,
            protocol_minimum=1,
            protocol_maximum=1,
            production_claim_sql_shape_verified=True,
            variant_selection_verified=True,
            variants=(fake_variant, fake_variant),
            paired_delta_samples_ms=(0.0,),
            paired_delta_p50_ms=0.0,
            paired_delta_p95_ms=0.0,
        )
        monkeypatch.setattr(benchmark_cmd, "connection", Mock(vendor="postgresql"))
        monkeypatch.setattr(benchmark_cmd.Command, "_run_policy", lambda self, **_kw: fake_policy)
        monkeypatch.setattr(
            benchmark_cmd.Command,
            "_run_protocol_predicate_evidence",
            lambda self, **_kw: fake_evidence,
        )
        monkeypatch.setattr(
            benchmark_cmd.Command,
            "_environment_metadata",
            lambda self, **_kw: {"vendor": "postgresql"},
        )

        out = io.StringIO()
        call_command("django_ray_benchmark_polling", "--format", "json", stdout=out)
        data = json.loads(out.getvalue())
        assert data["environment"] == {"vendor": "postgresql"}
        assert len(data["results"]) == 2


@pytest.mark.django_db
class TestR2036AdminAuthzAndSecure405:
    """R2-036: Pre-authz heavy-query suppression, existence-oracle closure, and secure 405s."""

    def test_observability_view_skips_heavy_annotations_for_unauthorized_staff(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="r2-036-obs-denied",
            callable_path="testproject.tasks.add_numbers",
            error_message="secret failure",
        )
        denied_staff = _staff_user("r2-036-denied-staff")
        request = RequestFactory().get(f"/admin/execution/{execution.pk}/observability/")
        request.user = denied_staff
        admin_obj = RayTaskExecutionAdmin(RayTaskExecution, admin.site)

        with CaptureQueriesContext(connection) as queries:
            with pytest.raises(PermissionDenied):
                admin_obj.observability_view(request, str(execution.pk))

        sql_blob = "\n".join(q["sql"].lower() for q in queries.captured_queries)
        assert "django_ray_taskworkerlease" not in sql_blob
        assert "django_ray_taskexecutionprotocolpolicy" not in sql_blob
        assert "error_message" not in sql_blob
        assert "octet_length" not in sql_blob

        anon_request = RequestFactory().get(f"/admin/execution/{execution.pk}/observability/")
        anon_request.user = AnonymousUser()
        with CaptureQueriesContext(connection) as anon_queries:
            with pytest.raises(PermissionDenied):
                admin_obj.observability_view(anon_request, str(execution.pk))
        assert len(anon_queries.captured_queries) == 0

        bad_id_request = RequestFactory().get("/admin/execution/not-an-int/observability/")
        bad_id_request.user = denied_staff
        with CaptureQueriesContext(connection) as bad_id_queries:
            with pytest.raises(PermissionDenied):
                admin_obj.observability_view(bad_id_request, "not-an-int")
        assert all(
            "django_ray_raytaskexecution" not in q["sql"].lower()
            for q in bad_id_queries.captured_queries
        )

    def test_sensitive_views_return_403_not_404_when_missing_sensitive_permission(self) -> None:
        ordinary = _staff_user("r2-036-ordinary", "view_raytaskexecution", "view_taskattempt")
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)

        exec_req = RequestFactory().get("/admin/execution/999999/sensitive-data/")
        exec_req.user = ordinary
        with pytest.raises(PermissionDenied):
            exec_admin.sensitive_data_view(exec_req, "999999")

        attempt_req = RequestFactory().get("/admin/attempt/999999/sensitive-data/")
        attempt_req.user = ordinary
        with pytest.raises(PermissionDenied):
            attempt_admin.sensitive_data_view(attempt_req, "999999")

    def test_all_admin_custom_views_wrap_405_with_secure_headers(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="r2-036-405-headers",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
        )
        superuser = get_user_model().objects.create_superuser(username="r2-036-405-admin")
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)
        rf = RequestFactory()

        get_req = rf.get("/admin/custom/")
        get_req.user = superuser
        post_req = rf.post("/admin/custom/")
        post_req.user = superuser

        responses = [
            exec_admin.retry_view(get_req, str(execution.pk)),
            exec_admin.observability_view(post_req, str(execution.pk)),
            exec_admin.workflow_topology_nodes_view(post_req, str(execution.pk)),
            exec_admin.workflow_node_detail_view(post_req, str(execution.pk)),
            exec_admin.sensitive_data_view(post_req, str(execution.pk)),
            attempt_admin.sensitive_data_view(post_req, str(attempt.pk)),
        ]
        for response in responses:
            assert response.status_code == 405
            assert response["Cache-Control"] == "no-store"
            assert response["X-Content-Type-Options"] == "nosniff"


@pytest.mark.django_db
class TestR2037StandaloneModeDashboardUrlAndPolish:
    """R2-037: standalone archived graph links, DJANGO_RAY dashboard URL, timestamps, and copy."""

    @override_settings(
        MIDDLEWARE=_ADMIN_MIDDLEWARE,
        DJANGO_RAY={"TASK_ATTEMPT_ADMIN_MODE": "standalone"},
    )
    def test_standalone_mode_still_renders_archived_workflow_graph_anchors(self, client) -> None:
        user = get_user_model().objects.create_superuser(username="r2-037-standalone-admin")
        execution = RayTaskExecution.objects.create(
            task_id="r2-037-standalone-task",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=2,
            workflow_run_id="00000000-0000-4000-8000-000000003701",
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="first attempt failed",
        )
        client.force_login(user)

        response = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
        )
        html = response.content.decode("utf-8")

        assert response.status_code == 200
        assert 'id="workflow-graph-attempt-1"' in html
        assert "attempt_number=1" in html

        # Parent-only viewer without view_taskattempt still must not see archived attempt graphs.
        parent_only = _staff_user("r2-037-standalone-parent-only", "view_raytaskexecution")
        client.force_login(parent_only)
        denied_html = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
        ).content.decode("utf-8")
        assert 'id="workflow-graph-attempt-1"' not in denied_html
        assert 'data-workflow-attempt-graph="' not in denied_html

    def test_ray_dashboard_url_supports_django_ray_setting_fallback(self) -> None:
        with override_settings(
            RAY_DASHBOARD_URL=None,
            DJANGO_RAY={"RAY_DASHBOARD_URL": "https://ray.internal.example:8265/"},
        ):
            assert admin_badges.ray_dashboard_url() == "https://ray.internal.example:8265"

        with override_settings(
            RAY_DASHBOARD_URL="https://top-level.example:8265/",
            DJANGO_RAY={"RAY_DASHBOARD_URL": "https://dict-level.example:8265/"},
        ):
            assert admin_badges.ray_dashboard_url() == "https://top-level.example:8265"

        with override_settings(
            RAY_DASHBOARD_URL=None,
            DJANGO_RAY={"RAY_DASHBOARD_URL": "javascript:alert(1)"},
        ):
            assert admin_badges.ray_dashboard_url() is None

    def test_task_attempt_admin_uses_compact_time_elements_and_active_copy_polish(self) -> None:
        attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)
        assert "started_display" in attempt_admin.list_display
        assert "finished_display" in attempt_admin.list_display
        assert "started_at" not in attempt_admin.list_display
        assert "finished_at" not in attempt_admin.list_display

        execution = RayTaskExecution.objects.create(
            task_id="r2-037-attempt-time",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
        )
        ts = datetime(2026, 7, 29, 12, 30, 0, tzinfo=UTC)
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.SUCCEEDED,
            started_at=ts,
            finished_at=ts + timedelta(seconds=5),
        )
        assert "<time " in str(attempt_admin.started_display(attempt))
        assert "<time " in str(attempt_admin.finished_display(attempt))

        running_guidance = admin_actions.retry_detail_guidance(TaskState.RUNNING)
        assert "best-effort cancellation" in running_guidance
        assert "This page opened while the execution was running." in running_guidance

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_non_terminal_and_terminal_non_workflow_copy(self, client) -> None:
        user = get_user_model().objects.create_superuser(username="r2-037-copy-admin")
        queued = RayTaskExecution.objects.create(
            task_id="r2-037-queued-non-wf",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.QUEUED,
        )
        succeeded = RayTaskExecution.objects.create(
            task_id="r2-037-done-non-wf",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
        )
        client.force_login(user)

        queued_html = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[queued.pk])
        ).content.decode("utf-8")
        done_html = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[succeeded.pk])
        ).content.decode("utf-8")

        assert "No workflow recorded yet." in queued_html
        assert "Not a workflow task." in done_html


class TestAdminWorkflowGraphValidationBranches:
    """Cover validation and degradation branches in workflow/admin_graph.py."""

    def test_admin_workflow_graph_validation_errors_and_truncation(self) -> None:
        from copy import deepcopy
        from uuid import uuid4

        from django_ray.workflow import admin_graph as ag

        # Message/status validation
        with pytest.raises(ValueError, match="Unsupported admin workflow graph reason"):
            ag._graph_message("UNAVAILABLE", "NO_SUCH_REASON")
        with pytest.raises(ValueError, match="Unsupported admin workflow graph status"):
            ag.AdminWorkflowGraphError("NOT_A_STATUS")
        with pytest.raises(ValueError, match="Unsupported degraded admin workflow graph status"):
            ag.degraded_admin_workflow_graph("NOT_A_STATUS")

        # Low-level validators: _counter, _identity, _publication, _bounded_redacted_text, _node_id, _safe_fanout
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._counter(True)
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._identity("not-a-dict")
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._identity(
                {
                    "schema_version": 1,
                    "run_id": "not-a-uuid",
                    "attempt_number": 1,
                    "execution_generation": 0,
                }
            )
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._identity(
                {
                    "schema_version": 2,
                    "run_id": str(uuid4()),
                    "attempt_number": 1,
                    "execution_generation": 0,
                }
            )
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._publication({"schema_version": 1})
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._bounded_redacted_text(123, max_bytes=32)
        trunc = ag._bounded_redacted_text("a" * 80, max_bytes=30)
        assert trunc is not None and trunc.endswith("... [truncated]")
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._node_id("")
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._safe_fanout({"bad": 1})
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._safe_fanout(
                {
                    "max_concurrency": 4,
                    "max_items": 10,
                    "submitted_items": 2,
                    "completed_items": 3,
                    "in_flight_items": 0,
                    "input_exhausted": True,
                }
            )

        # inspect_admin_workflow_graph_summary edge rejections
        run_id = str(uuid4())
        identity = {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_number": 1,
            "execution_generation": 0,
        }
        pub = {"summary_revision": 1, "topology_version": 1, "detail_revision": 1}
        valid_env: dict[str, Any] = {
            "schema": "django-ray.workflow-progress-summary",
            "schema_version": 1,
            "source_schema_version": 3,
            "availability": "AVAILABLE",
            "complete": True,
            "task_id": "t-1",
            "run_identity": identity,
            "publication": pub,
            "summary": {
                "schema_version": 3,
                "state": "SUCCEEDED",
                "run_identity": identity,
                "summary_revision": 1,
                "topology_version": 1,
                "detail_revision": 1,
                "timestamps": {"finished_at": "2026-01-01T00:00:00+00:00"},
                "terminal": {
                    "outcome": "SUCCEEDED",
                    "finished_at": "2026-01-01T00:00:00+00:00",
                },
                "detail": {
                    "availability": "AVAILABLE",
                    "complete": True,
                    "truncation_reasons": [],
                },
                "node_counts": {
                    "discovered": 1,
                    "retained_topology": 1,
                    "retained_detail": 1,
                    "pending": 0,
                    "running": 0,
                    "succeeded": 1,
                    "failed": 0,
                    "declared": 1,
                },
                "edge_counts": {
                    "discovered": 0,
                    "retained_topology": 0,
                    "declared": 0,
                },
            },
        }
        exp = ag.inspect_admin_workflow_graph_summary(valid_env)
        assert exp.node_count == 1

        # Various corrupt / degraded summary mutations
        for mutate, expected_status in [
            (lambda e: e.update({"schema": "wrong"}), "CORRUPT"),
            (lambda e: e.update({"summary": "not-dict"}), "CORRUPT"),
            (lambda e: e["summary"].update({"state": "BOGUS"}), "CORRUPT"),
            (lambda e: e.update({"availability": "NOT_REPORTED"}), "NOT_REPORTED"),
            (lambda e: e.update({"availability": "BOGUS_AVAIL"}), "CORRUPT"),
            (lambda e: e["summary"].update({"detail": "bad"}), "CORRUPT"),
            (lambda e: e.update({"complete": False}), "CORRUPT"),
            (
                lambda e: e["summary"]["detail"].update({"truncation_reasons": ["cap"]}),
                "TRUNCATED",
            ),
            (lambda e: e.update({"task_id": ""}), "CORRUPT"),
            (lambda e: e["summary"].update({"run_identity": {}}), "CORRUPT"),
            (lambda e: e["summary"].update({"summary_revision": 99}), "CORRUPT"),
            (lambda e: e["summary"].update({"timestamps": "bad"}), "CORRUPT"),
            (lambda e: e["summary"]["node_counts"].update({"succeeded": 0}), "CORRUPT"),
            (
                lambda e: e["summary"]["node_counts"].update({"retained_topology": 0}),
                "TRUNCATED",
            ),
            (lambda e: e["summary"]["node_counts"].update({"declared": 0}), "CORRUPT"),
            (lambda e: e["summary"]["node_counts"].update({"declared": 2}), "TRUNCATED"),
        ]:
            cloned = deepcopy(valid_env)
            mutate(cloned)
            with pytest.raises(ag.AdminWorkflowGraphError) as exc_info:
                ag.inspect_admin_workflow_graph_summary(cloned)
            assert exc_info.value.status == expected_status

        # _page_items availability branches
        base_page = {
            "schema": "django-ray.workflow-progress-page",
            "schema_version": 1,
            "collection": "topology_nodes",
            "task_id": "t-1",
            "run_identity": identity,
            "publication": pub,
            "availability": "EXPIRED",
            "complete": True,
            "next_cursor": None,
            "items": [],
            "returned_count": 0,
        }
        with pytest.raises(ag.AdminWorkflowGraphError) as page_unavail:
            ag._page_items(base_page, exp, collection="topology_nodes", expected_count=0)
        assert page_unavail.value.status == "UNAVAILABLE"

        corrupt_page = {**base_page, "availability": "AVAILABLE", "complete": False}
        with pytest.raises(ag.AdminWorkflowGraphError) as page_corrupt:
            ag._page_items(corrupt_page, exp, collection="topology_nodes", expected_count=0)
        assert page_corrupt.value.status == "CORRUPT"

        # _project_nodes / _project_edges / _failure_path diamond dedup
        topo = [
            {
                "node_id": "n1",
                "kind": "task",
                "label": "step1",
                "callable_path": "tests.fn",
                "runtime_env": None,
                "ray_options": {},
            }
        ]
        detail_base: dict[str, Any] = {
            "schema_version": 1,
            "node_id": "n1",
            "state": "SUCCEEDED",
            "truncated": False,
            "invocation_identity": None,
            "execution": None,
            "recent_events": [],
            "progress": None,
            "error": None,
            "fanout": None,
            "started_at": None,
            "finished_at": None,
        }
        for bad_detail_patch in [
            {"extra_key": 1},
            {"invocation_identity": "bad"},
            {"execution": "bad"},
            {"recent_events": "bad"},
            {"schema_version": 99},
            {"progress": "bad"},
            {
                "progress": {
                    "current": 1,
                    "total": 1,
                    "percent": 100,
                    "message": None,
                    "metrics": "bad",
                    "updated_at": None,
                }
            },
            {"error": "unexpected-error-on-succeeded"},
            {
                "fanout": {
                    "max_concurrency": 2,
                    "max_items": 4,
                    "submitted_items": 1,
                    "completed_items": 1,
                    "in_flight_items": 0,
                    "input_exhausted": True,
                }
            },
        ]:
            mutated = {**detail_base, **bad_detail_patch}
            with pytest.raises(ag.AdminWorkflowGraphError):
                ag._project_nodes(topo, [mutated], exp)

        # Mismatched topology/detail node sets and state counts
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._project_nodes(topo, [], exp)

        # Edge invalid field set
        with pytest.raises(ag.AdminWorkflowGraphError):
            ag._project_edges([{"source": "n1"}], known_nodes={"n1"})

        # _failure_path diamond DAG visits shared ancestor once (line 687)
        path = ag._failure_path(
            {"root": "SUCCEEDED", "left": "SUCCEEDED", "right": "SUCCEEDED", "sink": "FAILED"},
            {"root": set(), "left": {"root"}, "right": {"root"}, "sink": {"left", "right"}},
        )
        assert path == {"root", "left", "right", "sink"}


@pytest.mark.django_db
class TestAdminActionsAndDiagnosticsEdgeBranches:
    """Cover permission, token, cancel view, and workflow diagnostics edge branches."""

    def test_admin_actions_permissions_tokens_cancel_view_and_lease_policy_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser
        from django.core import signing
        from django.core.exceptions import PermissionDenied
        from django.http import Http404
        from django.test import RequestFactory

        from django_ray.admin import RayTaskExecutionAdmin, TaskWorkerLeaseAdmin
        from django_ray.lease_policy import LeasePolicyError
        from django_ray.lifecycle import TaskCancellationRequestStatus
        from django_ray.models import TaskWorkerLease

        rf = RequestFactory()
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        lease_admin = TaskWorkerLeaseAdmin(TaskWorkerLease, admin.site)
        execution = RayTaskExecution.objects.create(
            task_id="actions-edge-1",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
        )
        view_only = _staff_user("actions-edge-view", "view_raytaskexecution")
        super_user = get_user_model().objects.create_superuser(username="actions-edge-super")

        # has_execution_action_permission denied without change or object view perm
        req_view = rf.post("/")
        req_view.user = view_only
        assert (
            admin_actions.has_execution_action_permission(exec_admin, req_view, execution) is False
        )

        req_super = rf.post("/")
        req_super.user = super_user
        monkeypatch.setattr(exec_admin, "has_view_permission", lambda _r, _o=None: False)
        assert (
            admin_actions.has_execution_action_permission(exec_admin, req_super, execution) is False
        )
        monkeypatch.undo()

        # actor / session fingerprints & token matching edge rejections
        anon_req = rf.post("/")
        anon_req.user = AnonymousUser()
        with pytest.raises(PermissionDenied):
            admin_actions.admin_retry_actor_fingerprint(anon_req)
        assert admin_actions.admin_retry_session_fingerprint(anon_req, create=True) is None

        assert (
            admin_actions.admin_retry_confirmation_matches(
                "", [], actor_fingerprint="a", session_fingerprint="s"
            )
            is False
        )
        bad_keys_token = signing.dumps(
            {"version": 1}, salt=admin_actions.ADMIN_RETRY_CONFIRMATION_SALT, compress=False
        )
        assert (
            admin_actions.admin_retry_confirmation_matches(
                bad_keys_token, [], actor_fingerprint="a", session_fingerprint="s"
            )
            is False
        )
        bad_ver_token = signing.dumps(
            {"version": 99, "actor": "a", "session": "s", "count": 0, "digest": "d"},
            salt=admin_actions.ADMIN_RETRY_CONFIRMATION_SALT,
            compress=False,
        )
        assert (
            admin_actions.admin_retry_confirmation_matches(
                bad_ver_token, [], actor_fingerprint="a", session_fingerprint="s"
            )
            is False
        )

        # cancel_view: GET -> 405, unauthorized -> 403, missing -> 404, accepted / unsupported / rejected
        get_req = rf.get("/")
        get_req.user = super_user
        assert admin_actions.cancel_view(exec_admin, get_req, str(execution.pk)).status_code == 405

        with pytest.raises(PermissionDenied):
            admin_actions.cancel_view(exec_admin, req_view, str(execution.pk))

        with pytest.raises(Http404):
            admin_actions.cancel_view(exec_admin, req_super, "999999")

        recorded_msgs: list[str] = []
        monkeypatch.setattr(
            exec_admin,
            "message_user",
            lambda _r, msg, **_kw: recorded_msgs.append(str(msg)),
        )
        for status, accepted, expected_sub in [
            (None, True, "Accepted cancellation"),
            (
                TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL,
                False,
                "not support this execution protocol",
            ),
            (None, False, "did not accept cancellation"),
        ]:
            resp = admin_actions.cancel_view(
                exec_admin,
                req_super,
                str(execution.pk),
                cancel_fn=lambda *_a, _s=status, _acc=accepted, **_kw: SimpleNamespace(
                    status=_s,
                    accepted=_acc,
                ),
            )
            assert resp.status_code == 302
            assert expected_sub in recorded_msgs[-1]

        # mark_leases_inactive & delete_inactive_leases catch LeasePolicyError gracefully
        lease_msgs: list[str] = []
        monkeypatch.setattr(
            lease_admin,
            "message_user",
            lambda _r, msg, **_kw: lease_msgs.append(str(msg)),
        )
        monkeypatch.setattr(
            lease_admin,
            "_mark_inactive_under_policy",
            lambda _r, _qs: (_ for _ in ()).throw(LeasePolicyError("lease_policy_unavailable")),
        )
        monkeypatch.setattr(
            lease_admin,
            "_delete_inactive_under_policy",
            lambda _r, _qs: (_ for _ in ()).throw(LeasePolicyError("lease_policy_unavailable")),
        )
        admin_actions.mark_leases_inactive(lease_admin, req_super, TaskWorkerLease.objects.none())
        admin_actions.delete_inactive_leases(lease_admin, req_super, TaskWorkerLease.objects.none())
        assert "no leases changed" in lease_msgs[0]
        assert "no leases deleted" in lease_msgs[1]

    def test_admin_diagnostics_json_bounds_auth_preflight_and_progress_error_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser
        from django.core.exceptions import PermissionDenied
        from django.http import Http404
        from django.test import RequestFactory

        from django_ray.admin import RayTaskExecutionAdmin
        from django_ray.admin_views import diagnostics as diag
        from django_ray.lease_policy import LeasePolicyError
        from django_ray.models import TaskWorkerLease
        from django_ray.workflow.progress.reads import (
            WorkflowProgressReadError,
            WorkflowProgressReadErrorCode,
        )

        rf = RequestFactory()
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
        super_user = get_user_model().objects.create_superuser(username="diag-edge-super")
        no_perm_user = _staff_user("diag-edge-noperm")

        # ordinary_admin_result_display with digest-only oversize stub and null result_data -> badge only
        stub_obj = SimpleNamespace(
            result_reference=f"{diag.OVERSIZE_RESULT_REFERENCE_PREFIX}{'a' * 64}",
            result_data=None,
        )
        assert "Digest-only stub" in str(diag.ordinary_admin_result_display(stub_obj))

        # prepare_worker_lease_queryset degrades to NULL liveness on LeasePolicyError
        monkeypatch.setattr(
            "django_ray.lease_policy.read_lease_policy",
            lambda **_kw: (_ for _ in ()).throw(LeasePolicyError("lease_policy_unavailable")),
        )
        qs = diag.prepare_worker_lease_queryset(TaskWorkerLease.objects.all())
        assert list(qs) == []
        monkeypatch.undo()

        # admin_json_response and admin_json_attachment reject non-serializable / oversized payloads
        with pytest.raises(ValueError, match="not serializable"):
            diag.admin_json_response({"bad": object()}, max_bytes=1024)
        with pytest.raises(ValueError, match="not serializable"):
            diag.admin_json_attachment({"bad": object()}, filename="x.json", max_bytes=1024)
        with pytest.raises(ValueError, match="exceed their response limit"):
            diag.admin_json_attachment({"big": "x" * 200}, filename="x.json", max_bytes=32)

        # observability_execution & authorized_workflow_read_execution invalid / unauthenticated / missing
        anon_req = rf.get("/")
        anon_req.user = AnonymousUser()
        with pytest.raises(PermissionDenied):
            diag.authorized_workflow_read_execution(exec_admin, anon_req, "1")

        super_req = rf.get("/")
        super_req.user = super_user
        noperm_req = rf.get("/")
        noperm_req.user = no_perm_user

        with pytest.raises(Http404):
            diag.observability_execution(exec_admin, super_req, "not-an-int")
        with pytest.raises(PermissionDenied):
            diag.observability_execution(exec_admin, noperm_req, "999999")
        with pytest.raises(Http404):
            diag.authorized_workflow_read_execution(exec_admin, super_req, "not-an-int")
        with pytest.raises(PermissionDenied):
            diag.authorized_workflow_read_execution(exec_admin, noperm_req, "not-an-int")
        with pytest.raises(PermissionDenied):
            diag.authorized_workflow_read_execution(exec_admin, noperm_req, "999999")

        # preflight_workflow_collection routes all 3 collections
        execution = RayTaskExecution.objects.create(
            task_id="diag-edge-exec",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
        )
        seen_collections: list[str] = []
        hooks = diag.WorkflowReadHooks(
            list_nodes=lambda *_a, **_kw: (
                seen_collections.append("nodes") or {"availability": "AVAILABLE"}
            ),
            list_edges=lambda *_a, **_kw: (
                seen_collections.append("edges") or {"availability": "AVAILABLE"}
            ),
            list_details=lambda *_a, **_kw: (
                seen_collections.append("details") or {"availability": "AVAILABLE"}
            ),
        )
        for col in ("topology_nodes", "topology_edges", "node_details"):
            diag.preflight_workflow_collection(
                exec_admin, super_req, execution, collection=col, hooks=hooks
            )
        assert seen_collections == ["nodes", "edges", "details"]

        # lazy_workflow_progress_presentation error & validation branches
        plan = {"status": "AVAILABLE"}
        binding = {
            "fingerprint": "sha256:" + ("a" * 64),
            "selected_strategy": "ray_core",
            "reporting_policy": "full",
        }
        with pytest.raises(PermissionDenied):
            diag.lazy_workflow_progress_presentation(
                exec_admin,
                super_req,
                execution,
                plan,
                binding,
                hooks=diag.WorkflowReadHooks(
                    get_summary=lambda *_a, **_kw: (_ for _ in ()).throw(
                        WorkflowProgressReadError(WorkflowProgressReadErrorCode.ACCESS_DENIED)
                    )
                ),
            )
        with pytest.raises(Http404):
            diag.lazy_workflow_progress_presentation(
                exec_admin,
                super_req,
                execution,
                plan,
                binding,
                hooks=diag.WorkflowReadHooks(
                    get_summary=lambda *_a, **_kw: (_ for _ in ()).throw(
                        WorkflowProgressReadError(WorkflowProgressReadErrorCode.NOT_FOUND)
                    )
                ),
            )

        # terminal_only unexpected availability, unsupported source schema, bad detail/truncation, and
        # preflight returning EXPIRED -> degraded state
        res_term_bad = diag.lazy_workflow_progress_presentation(
            exec_admin,
            super_req,
            execution,
            plan,
            {**binding, "reporting_policy": "terminal_only"},
            hooks=diag.WorkflowReadHooks(
                get_summary=lambda *_a, **_kw: {
                    "source_schema_version": None,
                    "availability": "ODD",
                }
            ),
        )
        assert res_term_bad["state"] == "CORRUPT"

        res_bad_schema = diag.lazy_workflow_progress_presentation(
            exec_admin,
            super_req,
            execution,
            plan,
            binding,
            hooks=diag.WorkflowReadHooks(
                get_summary=lambda *_a, **_kw: {"source_schema_version": 99, "summary": {}}
            ),
        )
        assert res_bad_schema["state"] == "CORRUPT"

        base_v3_summary: dict[str, Any] = {
            "plan_fingerprint": binding["fingerprint"],
            "selected_strategy": "ray_core",
            "reporting_policy": "full",
            "state": "SUCCEEDED",
            "detail": {"truncation_reasons": []},
            "node_counts": {"retained_topology": 1, "retained_detail": 0},
            "edge_counts": {"retained_topology": 0},
        }
        res_bad_detail = diag.lazy_workflow_progress_presentation(
            exec_admin,
            super_req,
            execution,
            plan,
            binding,
            hooks=diag.WorkflowReadHooks(
                get_summary=lambda *_a, **_kw: {
                    "source_schema_version": 3,
                    "availability": "AVAILABLE",
                    "summary": {**base_v3_summary, "detail": "bad"},
                    "publication": {"topology_version": 1, "detail_revision": 0},
                }
            ),
        )
        assert res_bad_detail["state"] == "CORRUPT"

        res_bad_trunc = diag.lazy_workflow_progress_presentation(
            exec_admin,
            super_req,
            execution,
            plan,
            binding,
            hooks=diag.WorkflowReadHooks(
                get_summary=lambda *_a, **_kw: {
                    "source_schema_version": 3,
                    "availability": "AVAILABLE",
                    "summary": {**base_v3_summary, "detail": {"truncation_reasons": [123]}},
                    "publication": {"topology_version": 1, "detail_revision": 0},
                }
            ),
        )
        assert res_bad_trunc["state"] == "CORRUPT"

        monkeypatch.setattr(
            exec_admin,
            "_preflight_workflow_collection",
            lambda *_a, **_kw: {"availability": "EXPIRED", "returned_count": 0, "items": []},
        )
        res_expired = diag.lazy_workflow_progress_presentation(
            exec_admin,
            super_req,
            execution,
            plan,
            binding,
            hooks=diag.WorkflowReadHooks(
                get_summary=lambda *_a, **_kw: {
                    "source_schema_version": 3,
                    "availability": "AVAILABLE",
                    "summary": base_v3_summary,
                    "publication": {"topology_version": 1, "detail_revision": 0},
                }
            ),
        )
        assert res_expired["state"] == "EXPIRED"
