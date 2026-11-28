"""Structural and delegation tests for the Strangler-Fig admin decomposition (#829)."""

from __future__ import annotations

import ast
import importlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.utils.safestring import SafeData

from django_ray import admin as admin_module
from django_ray import admin_views
from django_ray.admin import (
    ActiveWorkerFilter,
    ExecutionProtocolFilter,
    HasWorkflowFilter,
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
    TaskAttemptInline,
    TaskExecutionProtocolPolicyAdmin,
    TaskWorkerLeaseAdmin,
)
from django_ray.admin_views import actions, badges, diagnostics, sensitive
from django_ray.lifecycle import (
    TaskCancellationRequestResult,
    TaskCancellationRequestStatus,
)
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)
from django_ray.workflow.admin_graph import AdminWorkflowGraphError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_PATH = _REPO_ROOT / "src" / "django_ray" / "admin.py"
_ADMIN_VIEWS_DIR = _REPO_ROOT / "src" / "django_ray" / "admin_views"
_EXPECTED_SUBMODULES = ("__init__.py", "actions.py", "badges.py", "diagnostics.py", "sensitive.py")


def _module_imports(file_path: Path, package_name: str, known_modules: set[str]) -> set[str]:
    """Return every static or deferred module import target within a module via AST."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package_name.split(".")
                prefix = ".".join(parts[: max(0, len(parts) - node.level + 1)])
                full_base = f"{prefix}.{base}" if base else prefix
            else:
                full_base = base
            if full_base:
                imports.add(full_base)
            for alias in node.names:
                if full_base and alias.name != "*":
                    candidate = f"{full_base}.{alias.name}"
                    if candidate in known_modules:
                        imports.add(candidate)
    return imports


def _has_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """Return a cycle path if one exists in the directed import graph."""
    visiting: list[str] = []
    visited: set[str] = set()

    def dfs(node: str) -> list[str] | None:
        if node in visiting:
            return [*visiting[visiting.index(node) :], node]
        if node in visited:
            return None
        visiting.append(node)
        for neighbor in sorted(graph.get(node, ())):
            if neighbor in graph:
                cycle = dfs(neighbor)
                if cycle is not None:
                    return cycle
        visiting.pop()
        visited.add(node)
        return None

    for start in sorted(graph):
        cycle = dfs(start)
        if cycle is not None:
            return cycle
    return None


class TestAdminDecompositionStructure:
    """Verify clean Strangler-Fig separation and zero circular imports."""

    def test_expected_admin_views_modules_exist_and_admin_py_is_slim(self) -> None:
        for filename in _EXPECTED_SUBMODULES:
            module_path = _ADMIN_VIEWS_DIR / filename
            assert module_path.is_file(), f"Missing decomposed module: {module_path}"
            assert module_path.read_text(encoding="utf-8").strip()

        admin_loc = len(_ADMIN_PATH.read_text(encoding="utf-8").splitlines())
        # Decomposed from 4,415 LOC monolith into thin ModelAdmin declarations/delegators.
        assert admin_loc < 1300, f"Expected slim admin.py (<1300 LOC), got {admin_loc}"
        assert set(admin_views.__all__) == {"actions", "badges", "diagnostics", "sensitive"}
        assert admin_views.badges is badges
        assert admin_views.diagnostics is diagnostics
        assert admin_views.actions is actions
        assert admin_views.sensitive is sensitive

    def test_admin_views_never_imports_django_ray_admin_and_import_graph_is_acyclic(self) -> None:
        known_modules = {
            "django_ray.admin",
            "django_ray.admin_views",
            *(
                f"django_ray.admin_views.{Path(filename).stem}"
                for filename in _EXPECTED_SUBMODULES
                if filename != "__init__.py"
            ),
        }
        internal_graph: dict[str, set[str]] = {}
        admin_imports = _module_imports(_ADMIN_PATH, "django_ray", known_modules)
        internal_graph["django_ray.admin"] = admin_imports & known_modules

        for filename in _EXPECTED_SUBMODULES:
            module_path = _ADMIN_VIEWS_DIR / filename
            module_name = (
                "django_ray.admin_views"
                if filename == "__init__.py"
                else f"django_ray.admin_views.{module_path.stem}"
            )
            imports = _module_imports(module_path, "django_ray.admin_views", known_modules)

            # Strict one-way boundary: admin_views/* must never import django_ray.admin.
            assert "django_ray.admin" not in imports, (
                f"{module_name} must not import django_ray.admin"
            )
            assert not any(imp.startswith("django_ray.admin.") for imp in imports), (
                f"{module_name} must not import symbols from django_ray.admin"
            )
            assert module_name not in imports, f"{module_name} must not import itself"

            internal_graph[module_name] = imports & known_modules

        # Verify strict layering: __init__ and badges are pure leaves among admin_views.
        assert internal_graph["django_ray.admin_views"] == set()
        assert internal_graph["django_ray.admin_views.badges"] == set()
        assert internal_graph["django_ray.admin_views.diagnostics"] <= {
            "django_ray.admin_views.badges"
        }
        assert internal_graph["django_ray.admin_views.actions"] <= {
            "django_ray.admin_views.diagnostics"
        }
        assert internal_graph["django_ray.admin_views.sensitive"] <= {
            "django_ray.admin_views.diagnostics"
        }

        cycle = _has_cycle(internal_graph)
        assert cycle is None, f"Circular import detected across admin modules: {cycle}"

    @pytest.mark.parametrize(
        "module_name",
        [
            "django_ray.admin_views",
            "django_ray.admin_views.badges",
            "django_ray.admin_views.diagnostics",
            "django_ray.admin_views.actions",
            "django_ray.admin_views.sensitive",
            "django_ray.admin",
        ],
    )
    def test_all_decomposed_modules_import_cleanly(self, module_name: str) -> None:
        loaded = importlib.import_module(module_name)
        assert loaded is not None


class TestAdminDelegationParity:
    """Verify thin ModelAdmin wrappers delegate faithfully and preserve public contracts."""

    def test_registered_admins_and_url_routes_are_preserved(self) -> None:
        assert isinstance(admin.site._registry[RayTaskExecution], RayTaskExecutionAdmin)
        assert isinstance(admin.site._registry[TaskAttempt], TaskAttemptAdmin)
        assert isinstance(admin.site._registry[TaskWorkerLease], TaskWorkerLeaseAdmin)
        assert isinstance(
            admin.site._registry[TaskExecutionProtocolPolicy],
            TaskExecutionProtocolPolicyAdmin,
        )

        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, AdminSite(name="decomp-admin"))
        attempt_admin = TaskAttemptAdmin(TaskAttempt, AdminSite(name="decomp-admin"))
        exec_url_names = {url.name for url in exec_admin.get_urls() if getattr(url, "name", None)}
        attempt_url_names = {
            url.name for url in attempt_admin.get_urls() if getattr(url, "name", None)
        }

        expected_exec_routes = {
            "django_ray_raytaskexecution_sensitive_data",
            "django_ray_raytaskexecution_retry",
            "django_ray_raytaskexecution_cancel",
            "django_ray_raytaskexecution_observability",
            "django_ray_raytaskexecution_workflow_diagnostics",
            "django_ray_raytaskexecution_workflow_plan_download",
            "django_ray_raytaskexecution_workflow_plan_selection_download",
            "django_ray_raytaskexecution_workflow_graph",
            "django_ray_raytaskexecution_workflow_topology_nodes",
            "django_ray_raytaskexecution_workflow_topology_edges",
            "django_ray_raytaskexecution_workflow_node_details",
            "django_ray_raytaskexecution_workflow_node_detail",
        }
        assert expected_exec_routes <= exec_url_names
        assert "django_ray_raytaskexecution_delete" not in exec_url_names
        assert "django_ray_raytaskexecution_history" not in exec_url_names

        assert "django_ray_taskattempt_sensitive_data" in attempt_url_names
        assert "django_ray_taskattempt_delete" not in attempt_url_names
        assert "django_ray_taskattempt_history" not in attempt_url_names

    def test_badges_and_filters_delegate_and_preserve_safestring(self) -> None:
        assert ExecutionProtocolFilter is badges.ExecutionProtocolFilter
        assert HasWorkflowFilter is badges.HasWorkflowFilter
        assert ActiveWorkerFilter is badges.ActiveWorkerFilter

        site = AdminSite(name="decomp-badges")
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, site)
        attempt_admin = TaskAttemptAdmin(TaskAttempt, site)
        inline = TaskAttemptInline(TaskAttempt, site)
        lease_admin = TaskWorkerLeaseAdmin(TaskWorkerLease, site)

        execution = RayTaskExecution(
            task_id="decomp-badge-1",
            callable_path="my_app.tasks.long_callable_name",
            queue_name="default",
            state=TaskState.RUNNING,
            created_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
            result_reference="oversize://sha256:abc",
            result_data='{"ok": true}',
            error_traceback="Traceback:\n  ValueError: boom",
        )
        attempt = TaskAttempt(execution_id=1, attempt_number=2, state=TaskState.FAILED)
        lease = TaskWorkerLease(
            worker_id="worker-1234567890abcdef",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=2,
        )

        badge_html = exec_admin.state_display(execution)
        assert isinstance(badge_html, SafeData)
        assert badge_html == badges.state_badge_html(TaskState.RUNNING)
        assert attempt_admin.state_display(attempt) == badges.state_badge_html(TaskState.FAILED)
        assert inline.state_display(attempt) == badges.state_badge_html(TaskState.FAILED)

        assert isinstance(exec_admin.task_display(execution), SafeData)
        assert isinstance(exec_admin.queue_display(execution), SafeData)
        assert isinstance(exec_admin.created_display(execution), SafeData)
        assert isinstance(exec_admin.result_data_display(execution), SafeData)
        assert isinstance(exec_admin.error_traceback_display(execution), SafeData)
        assert isinstance(lease_admin.worker_id_short(lease), SafeData)
        assert lease_admin.supported_execution_protocols(lease) == "1-2"

    def test_diagnostics_and_sensitive_delegation_parity(self) -> None:
        issue = AdminWorkflowGraphError("UNAVAILABLE")
        direct = diagnostics.workflow_graph_issue_response(issue)
        delegated = RayTaskExecutionAdmin._workflow_graph_issue_response(issue)
        assert delegated.status_code == direct.status_code
        assert delegated.content == direct.content
        assert delegated["X-Content-Type-Options"] == "nosniff"
        assert delegated["Cache-Control"] == "no-store"

        site = AdminSite(name="decomp-sensitive")
        site.register(RayTaskExecution, RayTaskExecutionAdmin)
        site.register(TaskAttempt, TaskAttemptAdmin)
        attempt_admin = site._registry[TaskAttempt]
        assert attempt_admin._parent_execution_admin() is sensitive.parent_execution_admin(site)

    def test_actions_delegation_honors_admin_module_monkeypatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = AdminSite(name="decomp-actions")
        exec_admin = RayTaskExecutionAdmin(RayTaskExecution, site)
        recorded: list[tuple[int, int, int]] = []
        messages_seen: list[str] = []

        def fake_cancel(pk: int, **kwargs: int) -> TaskCancellationRequestResult:
            recorded.append(
                (pk, kwargs["expected_attempt_number"], kwargs["expected_execution_generation"])
            )
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.ACCEPTED,
                execution_id=pk,
                attempt_number=kwargs["expected_attempt_number"],
                execution_generation=kwargs["expected_execution_generation"],
                state=TaskState.CANCELLED,
            )

        monkeypatch.setattr(admin_module, "request_task_cancellation", fake_cancel)
        monkeypatch.setattr(exec_admin, "message_user", lambda _req, msg: messages_seen.append(msg))

        request = RequestFactory().post("/admin/django_ray/raytaskexecution/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_staff=True,
            has_perm=lambda _perm, _obj=None: True,
            has_module_perms=lambda _app: True,
        )
        queryset = Mock()
        queryset.only.return_value = [
            SimpleNamespace(pk=42, attempt_number=2, execution_generation=5)
        ]

        assert exec_admin.has_action_permission(request) is actions.has_execution_action_permission(
            exec_admin, request
        )
        exec_admin.cancel_tasks(request, queryset)

        assert recorded == [(42, 2, 5)]
        assert any("Accepted cancellation for 1 task(s)." in msg for msg in messages_seen)
