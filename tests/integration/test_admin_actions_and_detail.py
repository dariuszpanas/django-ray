"""Integration tests for django-ray admin workflow summaries, detail views, and retries."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import Http404
from django.test import Client, RequestFactory, override_settings
from django.test.html import parse_html
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from django_ray.admin import (
    ADMIN_DIAGNOSTIC_MAX_CHARS,
)
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.plans import (
    EffectiveWorkflowPlan,
    materialize_workflow_plan,
)
from django_ray.workflow.progress.preparation import prepare_workflow_progress_topology
from django_ray.workflow.progress.reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
)
from django_ray.workflow.progress.storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    stage_workflow_progress_topology,
)
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from django_ray.workflows import map_step
from tests.integration._admin_helpers import (
    _ADMIN_MIDDLEWARE,
    _REPOSITORY_ROOT,
    _confirmed_retry_request,
    _plan_selection_json,
    _retry_confirmation,
    _retry_confirmation_context,
    _retry_request,
    _task_admin,
    _walk_html,
)
from tests.integration.test_admin import _admin_diagnostic_increment
from tests.workflow_progress_storage_helpers import (
    workflow_detail,
    workflow_node,
    workflow_summary,
)
from tests.workflow_progress_summary_helpers import (
    terminal_only_workflow_progress_summary,
    workflow_progress_summary,
)


@pytest.fixture(scope="module")
def admin_dynamic_workflow_plan() -> EffectiveWorkflowPlan:
    return materialize_workflow_plan(
        map_step(_admin_diagnostic_increment),
        invocation_args=([1, 2],),
    ).plan


@pytest.mark.django_db
class TestRayTaskExecutionAdminActionsAndDetail:
    """Tests for terminal-only summaries, detail views, and retry confirmation."""

    @pytest.mark.parametrize(
        ("mismatched_field", "mismatched_value"),
        [
            ("plan_fingerprint", "sha256:" + ("0" * 64)),
            ("selected_strategy", "local"),
            ("reporting_policy", "sampled"),
        ],
    )
    def test_workflow_diagnostics_rejects_plan_progress_binding_mismatch(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        mismatched_field: str,
        mismatched_value: str,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id=f"admin-workflow-progress-mismatch-{mismatched_field}",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960001",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        summary[mismatched_field] = mismatched_value
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username=f"workflow-progress-mismatch-{mismatched_field}-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def unexpected_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("mismatched progress must not preflight topology")

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            unexpected_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == "AVAILABLE"
        assert payload["progress"] == {
            "state": "CORRUPT",
            "message": "Workflow progress failed validation.",
            "availability": "CORRUPT",
            "complete": False,
            "truncation_reasons": [],
            "actions": {
                "topology_nodes": False,
                "topology_edges": False,
                "node_details": False,
            },
        }

    def test_workflow_progress_binding_uses_validated_values_before_redaction(
        self,
        monkeypatch,
        settings,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "REDACT_PATTERNS": [r"dynamic_tasks", r"full", r"sha256"],
        }
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-progress-redacted-binding",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960006",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="workflow-progress-redacted-binding-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def useful_preflight(
            _request: Any,
            _execution: RayTaskExecution,
            *,
            collection: str,
            attempt_number: int | None,
        ) -> dict[str, Any]:
            assert attempt_number is None
            assert collection in {"topology_nodes", "node_details"}
            return {
                "availability": "AVAILABLE",
                "returned_count": 1,
                "items": [{"collection": collection}],
            }

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            useful_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == "AVAILABLE"
        assert payload["plan"]["fingerprint"] == "[REDACTED]"
        assert payload["plan"]["selected_strategy"] == "[REDACTED]"
        assert payload["plan"]["reporting_policy"] == "[REDACTED]"
        assert payload["progress"]["state"] == "AVAILABLE"
        assert payload["progress"]["availability"] == "AVAILABLE"
        assert payload["progress"]["actions"] == {
            "topology_nodes": True,
            "topology_edges": False,
            "node_details": True,
        }

    @pytest.mark.parametrize(
        ("plan_case", "expected_plan_status"),
        [
            ("not_recorded", "NOT_RECORDED"),
            ("corrupt", "CORRUPT"),
        ],
    )
    def test_schema_v3_progress_requires_an_available_verified_plan(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        plan_case: str,
        expected_plan_status: str,
    ) -> None:
        plan_fields: dict[str, str | None] = {
            "workflow_plan_fingerprint": None,
            "workflow_plan_json": None,
            "workflow_plan_selection": None,
        }
        if plan_case == "corrupt":
            plan_fields = {
                "workflow_plan_fingerprint": "sha256:" + ("0" * 64),
                "workflow_plan_json": admin_dynamic_workflow_plan.canonical_json,
                "workflow_plan_selection": _plan_selection_json(
                    admin_dynamic_workflow_plan,
                ),
            }
        execution = RayTaskExecution.objects.create(
            task_id=f"admin-workflow-v3-{plan_case}-plan",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id=(
                "00000000-0000-0000-0000-000000960002"
                if plan_case == "not_recorded"
                else "00000000-0000-0000-0000-000000960003"
            ),
            **plan_fields,
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username=f"workflow-v3-{plan_case}-plan-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def unexpected_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("unverified plans must not preflight topology")

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            unexpected_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == expected_plan_status
        assert payload["progress"]["state"] == "CORRUPT"
        assert payload["progress"]["availability"] == "CORRUPT"
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    def test_corrupt_plan_preserves_terminal_only_graph_suppression_signal(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-terminal-only-corrupt-plan",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.SUCCEEDED,
            workflow_run_id="00000000-0000-0000-0000-000000960004",
            workflow_plan_fingerprint="sha256:" + ("0" * 64),
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(
                admin_dynamic_workflow_plan,
                reporting_policy="terminal_only",
            ),
        )
        manifest = json.loads(admin_dynamic_workflow_plan.canonical_json)
        summary = terminal_only_workflow_progress_summary(
            execution,
            declared_node_count=len(manifest["nodes"]),
            declared_edge_count=len(manifest["edges"]),
        )
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="terminal-only-corrupt-plan-admin",
        )
        request = RequestFactory().get(
            "/admin/workflow/diagnostics/",
            {"attempt_number": execution.attempt_number},
        )
        request.user = user
        admin_obj = _task_admin()

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            lambda *_args, **_kwargs: pytest.fail(
                "corrupt plans must not preflight terminal-only detail"
            ),
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == "CORRUPT"
        assert payload["progress"]["state"] == "CORRUPT"
        assert payload["progress"]["availability"] == "CORRUPT"
        assert payload["progress"]["reporting_policy"] == "terminal_only"
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    def test_corrupt_plan_preserves_terminal_only_policy_without_a_summary(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-terminal-only-corrupt-plan-missing-summary",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.SUCCEEDED,
            workflow_run_id="00000000-0000-0000-0000-000000960005",
            workflow_plan_fingerprint="sha256:" + ("0" * 64),
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(
                admin_dynamic_workflow_plan,
                reporting_policy="terminal_only",
            ),
        )
        user = get_user_model().objects.create_superuser(
            username="terminal-only-corrupt-plan-missing-summary-admin",
        )
        request = RequestFactory().get(
            "/admin/workflow/diagnostics/",
            {"attempt_number": execution.attempt_number},
        )
        request.user = user
        admin_obj = _task_admin()
        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            lambda *_args, **_kwargs: pytest.fail(
                "a terminal-only run without a summary must not preflight detail"
            ),
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == "CORRUPT"
        assert payload["plan"]["reporting_policy"] == "terminal_only"
        assert payload["progress"]["state"] == "TERMINAL_ONLY_MISSING"
        assert payload["progress"]["availability"] == "MISSING"
        assert payload["progress"]["reporting_policy"] == "terminal_only"
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    def test_workflow_diagnostics_read_is_pinned_to_the_requested_attempt(
        self,
        monkeypatch,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-pinned-diagnostics",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            attempt_number=2,
        )
        user = get_user_model().objects.create_superuser(
            username="workflow-pinned-diagnostics-admin",
        )
        request = RequestFactory().get(
            "/admin/workflow/diagnostics/",
            {"attempt_number": 1},
        )
        request.user = user
        observed: list[int | None] = []

        def summary_read(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            observed.append(kwargs.get("attempt_number"))
            return {
                "source_schema_version": None,
                "summary": None,
                "availability": "NOT_REPORTED",
                "complete": False,
            }

        monkeypatch.setattr(
            "django_ray.admin.get_workflow_progress_summary",
            summary_read,
        )

        response = _task_admin().workflow_diagnostics_view(
            request,
            str(execution.pk),
        )

        assert response.status_code == 200
        assert observed == [1]

    def test_workflow_diagnostics_does_not_advertise_summary_only_topology(
        self,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-summary-only-topology",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960004",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="workflow-summary-only-topology-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        diagnostics_response = admin_obj.workflow_diagnostics_view(
            request,
            str(execution.pk),
        )
        topology_response = admin_obj.workflow_topology_nodes_view(
            request,
            str(execution.pk),
        )
        diagnostics = json.loads(diagnostics_response.content)
        topology = json.loads(topology_response.content)

        assert diagnostics_response.status_code == 200
        assert diagnostics["progress"]["state"] == "MISSING"
        assert diagnostics["progress"]["availability"] == "MISSING"
        assert diagnostics["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }
        assert topology_response.status_code == 409
        assert topology["code"] == "MISSING"

    def test_workflow_diagnostics_hides_actions_when_preflight_is_corrupt(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-corrupt-topology-preflight",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960005",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="workflow-corrupt-topology-preflight-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def corrupt_preflight(
            _request: Any,
            _execution: RayTaskExecution,
            *,
            collection: str,
            attempt_number: int | None,
        ) -> dict[str, Any]:
            assert attempt_number is None
            assert collection == "topology_nodes"
            raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.CORRUPT)

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            corrupt_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["progress"]["state"] == "CORRUPT"
        assert payload["progress"]["availability"] == "CORRUPT"
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    @pytest.mark.parametrize(
        ("preflight_page", "expected_state"),
        [
            ({"availability": "AVAILABLE", "returned_count": 1}, "CORRUPT"),
            (
                {
                    "availability": "AVAILABLE",
                    "returned_count": 1,
                    "items": None,
                },
                "CORRUPT",
            ),
            (
                {
                    "availability": "AVAILABLE",
                    "returned_count": 2,
                    "items": [{}],
                },
                "CORRUPT",
            ),
            (
                {
                    "availability": "AVAILABLE",
                    "returned_count": 1,
                    "items": [{}, {}],
                },
                "CORRUPT",
            ),
            (
                {
                    "availability": "AVAILABLE",
                    "returned_count": 0,
                    "items": [],
                },
                "MISSING",
            ),
        ],
        ids=[
            "missing-items",
            "items-not-list",
            "items-shorter-than-count",
            "items-longer-than-count",
            "valid-empty-page",
        ],
    )
    def test_workflow_diagnostics_fails_closed_for_unusable_preflight_pages(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        preflight_page: dict[str, Any],
        expected_state: str,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-inconsistent-topology-preflight",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960007",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="workflow-inconsistent-topology-preflight-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def inconsistent_preflight(
            _request: Any,
            _execution: RayTaskExecution,
            *,
            collection: str,
            attempt_number: int | None,
        ) -> dict[str, Any]:
            assert attempt_number is None
            assert collection == "topology_nodes"
            return preflight_page

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            inconsistent_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["progress"]["state"] == expected_state
        assert payload["progress"]["availability"] == expected_state
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    def test_workflow_diagnostics_advertises_only_persisted_nonempty_collections(
        self,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        run_id = "00000000-0000-0000-0000-000000960007"
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-persisted-diagnostics",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
            workflow_run_id=run_id,
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        identity = WorkflowRunIdentity(
            task_execution_pk=execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
            run_id=run_id,
        )
        node_ids = ("admin-node-00001", "admin-node-00002")
        topology = prepare_workflow_progress_topology(
            identity,
            1,
            tuple(workflow_node(node_id) for node_id in node_ids),
            ({"source": node_ids[0], "target": node_ids[1]},),
        )
        prepared_detail = prepare_workflow_progress_detail(
            tuple(workflow_detail(node_id) for node_id in node_ids),
            topology=topology,
        )
        manifest_id = stage_workflow_progress_topology(topology)
        assert manifest_id is not None
        summary = workflow_summary(
            identity,
            summary_revision=1,
            node_count=len(node_ids),
            running_count=0,
        )
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        edge_counts = summary["edge_counts"]
        assert isinstance(edge_counts, dict)
        edge_counts.update(
            declared=1,
            discovered=1,
        )
        publication = persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=manifest_id,
            prepared_topology=topology,
            prepared_detail=prepared_detail,
        )
        assert publication.accepted is True

        user = get_user_model().objects.create_superuser(
            username="workflow-persisted-diagnostics-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        diagnostics_response = admin_obj.workflow_diagnostics_view(
            request,
            str(execution.pk),
        )
        diagnostics = json.loads(diagnostics_response.content)

        assert diagnostics_response.status_code == 200
        assert diagnostics["plan"]["status"] == "AVAILABLE"
        assert diagnostics["progress"]["state"] == "AVAILABLE"
        assert diagnostics["progress"]["actions"] == {
            "topology_nodes": True,
            "topology_edges": True,
            "node_details": True,
        }

        advertised_views = {
            "topology_nodes": admin_obj.workflow_topology_nodes_view,
            "topology_edges": admin_obj.workflow_topology_edges_view,
            "node_details": admin_obj.workflow_node_details_view,
        }
        for action, view in advertised_views.items():
            assert diagnostics["progress"]["actions"][action] is True
            response = view(request, str(execution.pk))
            payload = json.loads(response.content)
            assert response.status_code == 200
            assert payload["availability"] == "AVAILABLE"
            assert payload["returned_count"] > 0
            assert len(payload["items"]) == payload["returned_count"]

    def test_observability_endpoint_defers_payloads_and_reads_progress_once(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-bounded-query-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data=json.dumps({"schema_version": 1, "revision": 4}),
            args_json=json.dumps(["private-input"]),
        )
        user = get_user_model().objects.create_superuser(username="bounded-query-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        with CaptureQueriesContext(connection) as queries:
            response = _task_admin().observability_view(request, str(execution.pk))

        assert response.status_code == 200
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert task_selects
        assert all("progress_data" not in query for query in task_selects)
        assert all("args_json" not in query for query in task_selects)
        assert all("workflow_plan_json" not in query for query in task_selects)
        assert all("workflow_plan_selection" not in query for query in task_selects)
        assert any("workflow_progress_summary_json" in query for query in task_selects)
        detail_tables = (
            "django_ray_workflowprogressrunstorage",
            "django_ray_workflowprogresstopologymanifest",
            "django_ray_workflowprogresstopologypage",
            "django_ray_workflowprogressnodedetail",
        )
        assert all(
            table not in query for query in queries.captured_queries for table in detail_tables
        )

    def test_observability_endpoint_bounds_invalid_summary_without_legacy_fallback(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-invalid-summary-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data="password=legacy-secret" * 1_000,
            workflow_progress_summary_json="{" + ("x" * 20_000),
        )
        user = get_user_model().objects.create_superuser(username="invalid-summary-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        with CaptureQueriesContext(connection) as queries:
            response = _task_admin().observability_view(request, str(execution.pk))

        payload = json.loads(response.content)
        assert response.status_code == 200
        assert payload["workflow"] is None
        assert payload["workflow_error_code"] == "CORRUPT"
        assert len(payload["workflow_error"]) <= ADMIN_DIAGNOSTIC_MAX_CHARS
        assert "legacy-secret" not in response.content.decode("utf-8")
        assert all("progress_data" not in query["sql"] for query in queries.captured_queries)

    def test_authorized_admin_detail_views_call_bounded_services(
        self,
        monkeypatch,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-detail-links-001",
            callable_path="testproject.tasks.add_numbers",
        )
        user = get_user_model().objects.create_superuser(username="detail-links-admin")
        admin_obj = _task_admin()
        called: list[str] = []

        def page_payload(collection):
            return {
                "schema": "django-ray.workflow-progress-page",
                "schema_version": 1,
                "generated_at": "2026-07-20T12:00:00Z",
                "task_id": execution.task_id,
                "run_identity": None,
                "publication": {
                    "summary_revision": None,
                    "topology_version": None,
                    "detail_revision": None,
                },
                "availability": "NOT_REPORTED",
                "complete": False,
                "collection": collection,
                "returned_count": 0,
                "items": [],
                "next_cursor": None,
            }

        def fake_nodes(candidate, *, authorize, **kwargs):
            assert authorize(candidate) is True
            assert kwargs["attempt_number"] == 2
            called.append("topology_nodes")
            return page_payload("topology_nodes")

        def fake_edges(candidate, *, authorize, **kwargs):
            assert authorize(candidate) is True
            assert kwargs["attempt_number"] == 2
            called.append("topology_edges")
            return page_payload("topology_edges")

        def fake_details(candidate, *, authorize, **kwargs):
            assert authorize(candidate) is True
            assert kwargs["attempt_number"] == 2
            called.append("node_details")
            return page_payload("node_details")

        def fake_node(candidate, node_id, *, authorize, **kwargs):
            assert node_id == "node/one"
            assert authorize(candidate) is True
            assert kwargs["attempt_number"] == 2
            called.append("node")
            return {
                **page_payload("node_details"),
                "schema": "django-ray.workflow-progress-node",
                "found": False,
                "item": None,
            }

        monkeypatch.setattr("django_ray.admin.list_workflow_topology_nodes", fake_nodes)
        monkeypatch.setattr("django_ray.admin.list_workflow_topology_edges", fake_edges)
        monkeypatch.setattr("django_ray.admin.list_workflow_node_details", fake_details)
        monkeypatch.setattr("django_ray.admin.get_workflow_node_detail", fake_node)

        with CaptureQueriesContext(connection) as queries:
            for view in (
                admin_obj.workflow_topology_nodes_view,
                admin_obj.workflow_topology_edges_view,
                admin_obj.workflow_node_details_view,
            ):
                request = RequestFactory().get("/admin/workflow/?limit=10&attempt_number=2")
                request.user = user
                response = view(request, str(execution.pk))
                assert response.status_code == 200
                assert response["Cache-Control"] == "no-store"
            request = RequestFactory().get(
                "/admin/workflow/node/?attempt_number=2&node_id=node%2Fone"
            )
            request.user = user
            response = admin_obj.workflow_node_detail_view(request, str(execution.pk))
            assert response.status_code == 200
        assert called == ["topology_nodes", "topology_edges", "node_details", "node"]
        task_selects = [
            query["sql"].lower()
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 4
        payload_fields = (
            "runtime_env_json",
            "args_json",
            "kwargs_json",
            "result_data",
            "progress_data",
            "workflow_progress_summary_json",
            "workflow_plan_json",
            "workflow_plan_selection",
            "completion_data",
            "cancellation_error",
            "error_message",
            "error_traceback",
        )
        assert all(field not in query for query in task_selects for field in payload_fields)

        invalid_limit = RequestFactory().get("/admin/workflow/?limit=not-an-integer")
        invalid_limit.user = user
        response = admin_obj.workflow_topology_nodes_view(
            invalid_limit,
            str(execution.pk),
        )
        assert response.status_code == 400
        assert json.loads(response.content)["code"] == "INVALID_ARGUMENT"

        assert admin_obj._page_limit(RequestFactory().get("/admin/workflow/")) == 100

        denied_user = get_user_model().objects.create_user(
            username="bounded-detail-denied-staff",
            is_staff=True,
        )
        denied_page = RequestFactory().get(
            "/admin/workflow/?limit=invalid&attempt_number=invalid&cursor=invalid&state=INVALID"
        )
        denied_page.user = denied_user
        with pytest.raises(PermissionDenied):
            admin_obj.workflow_node_details_view(denied_page, str(execution.pk))
        denied_node = RequestFactory().get("/admin/workflow/node/?attempt_number=invalid")
        denied_node.user = denied_user
        with pytest.raises(PermissionDenied):
            admin_obj.workflow_node_detail_view(denied_node, str(execution.pk))
        assert called == ["topology_nodes", "topology_edges", "node_details", "node"]

        post = RequestFactory().post("/admin/workflow/")
        post.user = user
        assert admin_obj.workflow_topology_nodes_view(post, str(execution.pk)).status_code == 405
        assert admin_obj.workflow_node_detail_view(post, str(execution.pk)).status_code == 405

        def corrupt_page(*args, **kwargs):
            del args, kwargs
            raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.CORRUPT)

        monkeypatch.setattr("django_ray.admin.list_workflow_topology_nodes", corrupt_page)
        request = RequestFactory().get("/admin/workflow/")
        request.user = user
        response = admin_obj.workflow_topology_nodes_view(request, str(execution.pk))
        assert response.status_code == 503

        def missing_node_detail(*args, **kwargs):
            del args, kwargs
            raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.MISSING)

        monkeypatch.setattr("django_ray.admin.get_workflow_node_detail", missing_node_detail)
        request = RequestFactory().get("/admin/workflow/node/?node_id=node%2Fone")
        request.user = user
        response = admin_obj.workflow_node_detail_view(request, str(execution.pk))
        assert response.status_code == 409

        missing_node = RequestFactory().get("/admin/workflow/node/")
        missing_node.user = user
        response = admin_obj.workflow_node_detail_view(missing_node, str(execution.pk))
        assert response.status_code == 400
        assert json.loads(response.content)["code"] == "INVALID_ARGUMENT"

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            (WorkflowProgressReadErrorCode.INVALID_ARGUMENT, 400),
            (WorkflowProgressReadErrorCode.INVALID_CURSOR, 400),
            (WorkflowProgressReadErrorCode.CURSOR_MISMATCH, 409),
            (WorkflowProgressReadErrorCode.MISSING, 409),
            (WorkflowProgressReadErrorCode.CORRUPT, 503),
        ],
    )
    def test_admin_detail_errors_keep_bounded_codes(
        self,
        code: WorkflowProgressReadErrorCode,
        status: int,
    ) -> None:
        response = _task_admin()._workflow_read_error_response(WorkflowProgressReadError(code))

        assert response.status_code == status
        assert response["Cache-Control"] == "no-store"
        assert json.loads(response.content) == {
            "code": code.value,
            "message": str(WorkflowProgressReadError(code)),
        }

    def test_admin_detail_access_and_missing_errors_raise(self) -> None:
        admin_obj = _task_admin()

        with pytest.raises(PermissionDenied):
            admin_obj._workflow_read_error_response(
                WorkflowProgressReadError(WorkflowProgressReadErrorCode.ACCESS_DENIED)
            )
        with pytest.raises(Http404):
            admin_obj._workflow_read_error_response(
                WorkflowProgressReadError(WorkflowProgressReadErrorCode.NOT_FOUND)
            )

    def test_observability_endpoint_maps_bounded_v3_summary(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-v3-summary-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000000125",
        )
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
            workflow_progress_summary(execution, published_detail=True)
        )
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(username="v3-summary-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        payload = json.loads(_task_admin().observability_view(request, str(execution.pk)).content)

        assert payload["workflow_revision"] == 1
        assert payload["workflow"]["revision"] == 1
        assert payload["workflow"]["total_nodes"] == 1
        assert payload["workflow"]["pending_nodes"] == 1
        assert payload["workflow"]["detail"]["availability"] == "AVAILABLE"
        assert payload["workflow_availability"] == "AVAILABLE"
        assert "task_execution_pk" not in payload["workflow"]["run_identity"]

    def test_change_form_loads_live_status_panel_and_package_script(
        self,
    ) -> None:
        assert "unfold" not in django_settings.INSTALLED_APPS
        callable_path = (
            "testproject.tasks.order_fulfillment_recovery_showcase_with_an_unbroken_callable_name"
        )
        execution = RayTaskExecution.objects.create(
            task_id="admin-live-form-001",
            callable_path=callable_path,
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000990001",
            progress_data="legacy-graph" * 10_000,
            workflow_progress_summary_json="summary" * 10_000,
            workflow_plan_json="raw-workflow-plan-marker" * 1_000,
            workflow_plan_selection="raw-workflow-selection-marker" * 1_000,
        )
        user_model = get_user_model()
        superuser = user_model.objects.create_superuser(
            username="observability-form-superuser",
        )
        change_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        )
        request = RequestFactory().get(change_url)
        request.user = superuser
        with CaptureQueriesContext(connection) as queries:
            response = _task_admin().change_view(request, str(execution.pk))
            response.render()

        content = response.content.decode("utf-8")
        endpoint = reverse(
            "admin:django_ray_raytaskexecution_observability",
            args=[execution.pk],
        )
        diagnostics_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_diagnostics",
            args=[execution.pk],
        )
        plan_download_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_plan_download",
            args=[execution.pk],
        )
        selection_download_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_plan_selection_download",
            args=[execution.pk],
        )
        graph_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_graph",
            args=[execution.pk],
        )
        attempt_query = f"?attempt_number={execution.attempt_number}"
        assert response.status_code == 200
        body_match = re.search(r'<body class="(?P<classes>[^"]*)"', content)
        assert body_match is not None
        assert "django-ray-execution-change" in body_match.group("classes").split()
        assert f"<h2>{execution}</h2>" in content
        breadcrumbs_match = re.search(
            r'<(?P<tag>div|ol) class="breadcrumbs">(?P<body>.*?)</(?P=tag)>',
            content,
            flags=re.DOTALL,
        )
        assert breadcrumbs_match is not None
        assert str(execution) in breadcrumbs_match.group("body")
        assert 'class="module aligned django-ray-live"' in content
        assert 'id="django-ray-live-observability"' in content
        assert f'data-observability-url="{endpoint}"' in content
        assert 'href="/static/django_ray/admin/task_live.css"' in content
        assert 'src="/static/django_ray/admin/task_live.js"' in content
        assert 'src="/static/django_ray/admin/workflow_diagnostics.js"' in content
        assert 'aria-labelledby="django-ray-live-heading"' in content
        assert 'id="django-ray-live-heading"' in content
        assert content.count('role="status"') == 3
        assert 'id="django-ray-task-actions"' in content
        assert "This page opened while the execution was running." in content
        assert "reload this page to review recovery actions" in content
        assert "Retry task..." not in content
        assert 'aria-live="polite"' in content
        assert 'class="django-ray-live__grid"' in content
        assert 'class="django-ray-live__state"' in content
        assert 'data-state="RUNNING"' in content
        assert 'id="django-ray-workflow-diagnostics"' in content
        assert 'class="django-ray-workflow"' in content
        assert "data-workflow-diagnostics-status" in content
        assert "data-workflow-diagnostics-content" in content
        assert 'aria-atomic="true"' in content
        assert f'data-diagnostics-url="{diagnostics_url}{attempt_query}"' in content
        assert f'data-pinned-attempt-number="{execution.attempt_number}"' in content
        assert f'data-graph-url="{graph_url}{attempt_query}"' in content
        assert f'data-plan-download-url="{plan_download_url}"' in content
        assert f'data-selection-download-url="{selection_download_url}"' in content
        assert "Workflow execution" in content
        details_start = content.index("<details")
        details_opening_tag = content[details_start : content.index(">", details_start)]
        assert " open" not in details_opening_tag
        assert "raw-workflow-plan-marker" not in content
        assert "raw-workflow-selection-marker" not in content
        assert "Effective workflow plan" not in content
        assert "Workflow strategy selection" not in content
        topology_nodes_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_topology_nodes",
            args=[execution.pk],
        )
        topology_edges_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_topology_edges",
            args=[execution.pk],
        )
        node_details_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_node_details",
            args=[execution.pk],
        )
        node_detail_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_node_detail",
            args=[execution.pk],
        )
        assert f'data-topology-nodes-url="{topology_nodes_url}{attempt_query}"' in content
        assert f'data-topology-edges-url="{topology_edges_url}{attempt_query}"' in content
        assert f'data-node-details-url="{node_details_url}{attempt_query}"' in content
        assert f'data-node-detail-url="{node_detail_url}{attempt_query}"' in content
        assert f'href="{graph_url}"' not in content
        assert f'href="{topology_nodes_url}"' not in content
        assert f'href="{topology_edges_url}"' not in content
        assert f'href="{node_details_url}"' not in content
        assert f'href="{node_detail_url}"' not in content
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert task_selects
        assert all("progress_data" not in query for query in task_selects)
        assert all("runtime_env_json" not in query for query in task_selects)
        assert all("workflow_progress_summary_json" not in query for query in task_selects)
        assert all("workflow_plan_json" not in query for query in task_selects)
        assert all("workflow_plan_selection" not in query for query in task_selects)

    def test_workflow_diagnostics_javascript_contract(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI"):
                pytest.fail("Node.js is required for the workflow diagnostics contract in CI")
            pytest.skip("Node.js is unavailable for the workflow diagnostics contract")

        result = subprocess.run(
            [node, "--test", "tests/javascript/workflow_diagnostics.test.mjs"],
            cwd=_REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_retry_tasks_confirms_full_replay_before_requeue(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        failed = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=3,
            execution_generation=8,
            error_message="boom",
            args_json="[]",
            kwargs_json="{}",
            completion_data='{"success": false}',
        )
        lost = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-002",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.LOST,
            attempt_number=2,
            error_message="worker-lost-secret-marker",
            args_json="[]",
            kwargs_json="{}",
        )
        expired_deadline = datetime.now(UTC) - timedelta(minutes=1)
        expired = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-expired-003",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.EXPIRED,
            attempt_number=1,
            error_message="expired",
            args_json="[]",
            kwargs_json="{}",
            queue_timeout_seconds=60,
            queue_deadline_at=expired_deadline,
        )
        running = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-004",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json="{}",
        )

        qs = RayTaskExecution.objects.filter(pk__in=[failed.pk, lost.pk, expired.pk, running.pk])
        response = _retry_confirmation(admin_obj, qs)

        failed.refresh_from_db()
        lost.refresh_from_db()
        expired.refresh_from_db()
        running.refresh_from_db()
        assert failed.state == TaskState.FAILED
        assert lost.state == TaskState.LOST
        assert expired.state == TaskState.EXPIRED
        assert running.state == TaskState.RUNNING

        content = response.content.decode()
        context = _retry_confirmation_context(response)
        assert "Retry can repeat external effects" in content
        assert "restart at their entry node" in content
        assert "does not resume at the failed node" in content
        assert "Confirm full retry" in content
        assert 'class="django-ray-retry-card"' in content
        assert 'class="django-ray-retry-card__warning" role="alert"' in content
        assert 'class="django-ray-retry-card__confirm"' in content
        assert "Eligible failed, lost, or expired tasks" in content
        assert context["selected_count"] == 4
        assert context["retryable_count"] == 3
        assert context["non_retryable_count"] == 1
        assert context["known_workflow_count"] == 0
        assert context["selected_ids"] == [str(failed.pk), str(lost.pk), str(expired.pk)]
        assert len(context["confirmation_token"]) <= 512
        assert "boom" not in content
        assert "worker-lost-secret-marker" not in content

        confirmed_queryset = RayTaskExecution.objects.filter(pk__in=context["selected_ids"])
        assert (
            admin_obj.retry_tasks(
                _confirmed_retry_request(response),
                confirmed_queryset,
            )
            is None
        )

        failed.refresh_from_db()
        lost.refresh_from_db()
        expired.refresh_from_db()
        running.refresh_from_db()

        assert failed.state == TaskState.QUEUED
        assert failed.attempt_number == 4
        assert failed.execution_generation == 9
        assert failed.completion_data is None
        assert lost.state == TaskState.QUEUED
        assert lost.attempt_number == 3
        assert expired.state == TaskState.QUEUED
        assert expired.attempt_number == 2
        assert expired.queue_deadline_at is not None
        assert expired.queue_deadline_at > datetime.now(UTC)
        assert running.state == TaskState.RUNNING
        assert messages[-1] == "Queued 3 task(s) for retry."
        assert TaskAttempt.objects.get(execution=failed, attempt_number=3).error_message == "boom"
        assert (
            TaskAttempt.objects.get(execution=lost, attempt_number=2).error_message
            == "worker-lost-secret-marker"
        )
        assert (
            TaskAttempt.objects.get(execution=expired, attempt_number=1).state == TaskState.EXPIRED
        )

    @pytest.mark.parametrize(
        "opened_state",
        [TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING],
    )
    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_detail_active_retry_guidance_explains_reload(
        self,
        opened_state: str,
    ) -> None:
        user = get_user_model().objects.create_superuser(username="retry-active-guidance")
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-active-guidance",
            callable_path="testproject.tasks.failing_task",
            state=opened_state,
        )
        client = Client()
        client.force_login(user)
        detail_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[task.pk],
        )

        opened_response = client.get(detail_url)
        opened_html = opened_response.content.decode()

        assert opened_response.status_code == 200
        assert f"This page opened while the execution was {opened_state.lower()}." in opened_html
        assert (
            "After a terminal outcome, reload this page to review recovery actions." in opened_html
        )
        assert "Retry task..." not in opened_html

        # A view fixture transition models a terminal update while this page is open.
        # A fresh GET remains authoritative for permission and action availability.
        RayTaskExecution.objects.filter(pk=task.pk).update(state=TaskState.FAILED)
        reloaded_response = client.get(detail_url)
        reloaded_html = reloaded_response.content.decode()

        assert reloaded_response.status_code == 200
        assert "Retrying creates a new attempt" in reloaded_html
        assert "Retry task..." in reloaded_html
        assert "This page opened while the execution was" not in reloaded_html

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_detail_exposes_retry_or_fresh_enqueue_guidance(self) -> None:
        user = get_user_model().objects.create_superuser(username="retry-detail-guidance")
        failed = RayTaskExecution.objects.create(
            task_id="admin-retry-detail-failed",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        succeeded = RayTaskExecution.objects.create(
            task_id="admin-retry-detail-succeeded",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            result_data='{"value": 42}',
        )
        client = Client()
        client.force_login(user)
        retry_url = reverse(
            "admin:django_ray_raytaskexecution_retry",
            args=[failed.pk],
        )

        failed_response = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[failed.pk],
            )
        )
        failed_html = failed_response.content.decode()
        assert failed_response.status_code == 200
        assert 'id="django-ray-task-actions"' in failed_html
        assert "Retrying creates a new attempt" in failed_html
        assert "Retry task..." in failed_html
        assert f'formaction="{retry_url}"' in failed_html
        assert 'name="csrfmiddlewaretoken"' in failed_html
        document = parse_html(failed_html)
        detail_form = next(
            element
            for element in _walk_html(document)
            if dict(element.attributes).get("id") == "raytaskexecution_form"
        )
        retry_button = next(
            element
            for element in _walk_html(document)
            if "django-ray-task-actions__retry" in dict(element.attributes).get("class", "").split()
        )
        assert detail_form.name == "form"
        assert retry_button.name == "button"
        assert "disabled" not in dict(retry_button.attributes)
        assert dict(retry_button.attributes) == {
            "aria-describedby": "django-ray-task-actions-guidance",
            "class": (
                "button default django-ray-admin-action "
                "django-ray-admin-action--retry django-ray-task-actions__retry"
            ),
            "form": "raytaskexecution_form",
            "formaction": retry_url,
            "formmethod": "post",
            "formnovalidate": None,
            "type": "submit",
        }
        assert sum(element.name == "form" for element in _walk_html(detail_form)) == 1

        succeeded_response = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[succeeded.pk],
            )
        )
        succeeded_html = succeeded_response.content.decode()
        assert succeeded_response.status_code == 200
        assert "Retry is unavailable because this execution succeeded" in succeeded_html
        assert "enqueue a new task" in succeeded_html
        assert "Retry task..." not in succeeded_html
        assert retry_url not in succeeded_html
        admin_obj = _task_admin()
        assert "cancelled work again" in admin_obj._retry_detail_guidance(TaskState.CANCELLED)
        assert (
            admin_obj._retry_detail_guidance("UNKNOWN")
            == "Retry is unavailable for this execution state."
        )

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_detail_retry_reuses_confirmation_and_returns_to_detail(self) -> None:
        user = get_user_model().objects.create_superuser(username="retry-detail-flow")
        task = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="admin-retry-detail-flow",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=3,
            execution_generation=8,
            error_message="retry-detail-secret-marker",
        )
        client = Client()
        client.force_login(user)
        detail_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[task.pk],
        )
        retry_url = reverse(
            "admin:django_ray_raytaskexecution_retry",
            args=[task.pk],
        )

        assert client.get(retry_url).status_code == 405
        confirmation = client.post(retry_url)
        confirmation_html = confirmation.content.decode()
        assert confirmation.status_code == 200
        assert "Confirm full task retry" in confirmation_html
        assert "Retry can repeat external effects" in confirmation_html
        assert f'href="{detail_url}"' in confirmation_html
        assert "retry-detail-secret-marker" not in confirmation_html
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 3
        assert not TaskAttempt.objects.filter(execution=task).exists()

        token_match = re.search(
            r'name="retry_confirmation_token"[^>]*value="([^"]+)"',
            confirmation_html,
            re.DOTALL,
        )
        assert token_match is not None
        confirmed = client.post(
            retry_url,
            {
                "post": "yes",
                "retry_confirmation_token": token_match.group(1),
            },
        )
        assert confirmed.status_code == 302
        assert confirmed.headers["Location"] == detail_url
        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 4
        assert task.execution_generation == 9
        assert TaskAttempt.objects.filter(execution=task, attempt_number=3).exists()

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_detail_retry_rejects_succeeded_crafted_post(self) -> None:
        user = get_user_model().objects.create_superuser(username="retry-detail-succeeded-post")
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-detail-succeeded-post",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            result_data='{"value": 42}',
        )
        client = Client()
        client.force_login(user)
        detail_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[task.pk],
        )
        retry_url = reverse(
            "admin:django_ray_raytaskexecution_retry",
            args=[task.pk],
        )

        response = client.post(retry_url)

        assert response.status_code == 302
        assert response.headers["Location"] == detail_url
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.attempt_number == 1
        assert task.execution_generation == 0
        assert task.result_data == '{"value": 42}'
        assert not TaskAttempt.objects.filter(execution=task).exists()

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_detail_retry_requires_global_change_permission(self) -> None:
        user = get_user_model().objects.create_user(
            username="retry-detail-view-only",
            is_staff=True,
        )
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="django_ray",
                codename="view_raytaskexecution",
            )
        )
        task = RayTaskExecution.objects.create(
            task_id="admin-retry-detail-view-only",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        client = Client()
        client.force_login(user)
        detail_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[task.pk],
        )
        retry_url = reverse(
            "admin:django_ray_raytaskexecution_retry",
            args=[task.pk],
        )

        detail = client.get(detail_url)
        retry = client.post(retry_url)

        assert detail.status_code == 200
        assert 'id="django-ray-task-actions"' not in detail.content.decode()
        assert "Retry task..." not in detail.content.decode()
        assert retry.status_code == 403
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_execution_detail_retry_rejects_missing_invalid_and_denied_objects(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()

        with pytest.raises(Http404):
            admin_obj.retry_view(_retry_request(), "not-an-integer")
        with pytest.raises(Http404):
            admin_obj.retry_view(_retry_request(), "999999")

        task = RayTaskExecution.objects.create(
            task_id="admin-retry-detail-object-denied",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
        )
        monkeypatch.setattr(admin_obj, "has_view_permission", lambda request, obj=None: False)

        with pytest.raises(PermissionDenied):
            admin_obj.retry_view(_retry_request(), str(task.pk))

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert not TaskAttempt.objects.filter(execution=task).exists()
