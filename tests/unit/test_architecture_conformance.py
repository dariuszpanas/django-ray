"""Architecture conformance CI gates (F-010, F-073, F-095, #829, #836).

Enforces structural invariants across ``src/django_ray/`` via static AST checks:
1. Zero direct circular import pairs across the entire package (including lazy
   function-local and ``TYPE_CHECKING`` imports).
2. Leaf-module dependency directionality for extracted modules
   (``conf.validation``, ``_storage_types``, ``workflow._types``,
   ``_blob_storage``, ``_version``).
3. ADR-to-Schema / dormant-table governance: no unapproved Django models or new
   triggers on active hot tables (``RayTaskExecution``, ``TaskWorkerLease``,
   ``TaskAttempt``) and no experimental module leaks into active hot paths.
4. Lock-boundary static check: storage and HTTP purge/upload helpers are not
   invoked inside ``with transaction.atomic(...)`` blocks in key storage and
   purge modules.
5. Public top-level re-exports on ``django_ray`` (``ray_task``,
   ``RayTaskContract``, ``RayTaskContractError``, ``get_ray_task_contract``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
PACKAGE_ROOT = SRC_ROOT / "django_ray"
MIGRATIONS_ROOT = PACKAGE_ROOT / "migrations"

# Documented model baseline in src/django_ray/models.py (ADR-to-Schema gate).
# Adding any new model requires an explicit ADR review and baseline update.
_APPROVED_ACTIVE_MODELS = frozenset(
    {
        "LegacyWorkerAdmissionToken",
        "RayTaskExecution",
        "TaskAttempt",
        "TaskExecutionProtocolPolicy",
        "TaskInputPayload",
        "TaskWorkerLease",
        "TaskWorkerLeasePolicy",
        "WorkflowProgressNodeDetail",
        "WorkflowProgressRunStorage",
        "WorkflowProgressTopologyManifest",
        "WorkflowProgressTopologyManifestPage",
        "WorkflowProgressTopologyPage",
    }
)
_FROZEN_DORMANT_TARGET_MODELS = frozenset(
    {
        "RayTarget",
        "RayTargetAttestationRevision",
        "RayTargetPolicyRevision",
        "RayTargetRoute",
        "RayTargetRouteRevision",
        "RayTaskTargetBinding",
        "RayTaskTargetExecutionEvidence",
        "RayTaskTargetExecutionOutcome",
        "RayTaskTargetRouteSelection",
        "RayWorkerTargetCapability",
    }
)
_APPROVED_MODELS = _APPROVED_ACTIVE_MODELS | _FROZEN_DORMANT_TARGET_MODELS

# Documented triggers targeting active hot tables (RayTaskExecution,
# TaskWorkerLease, TaskAttempt). No new triggers may be added to hot tables
# without explicit ADR review and SQLite table-rebuild retention tests.
_HOT_TABLE_TARGETS = frozenset(
    {
        "attempt_table",
        "django_ray_raytaskexecution",
        "django_ray_taskattempt",
        "django_ray_taskworkerlease",
        "execution_table",
        "lease_table",
    }
)
_APPROVED_HOT_TABLE_TRIGGERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("0019_execution_protocol_schema.py", "ray_attempt_immutable_0019"),
        ("0019_execution_protocol_schema.py", "ray_exec_immutable_0019"),
        ("0019_execution_protocol_schema.py", "ray_exec_legacy_insert_0019"),
        ("0019_execution_protocol_schema.py", "ray_exec_owner_update_0019"),
        ("0019_execution_protocol_schema.py", "ray_lease_admission_insert_0019"),
        ("0019_execution_protocol_schema.py", "ray_lease_admission_update_0019"),
        ("0019_execution_protocol_schema.py", "ray_lease_capability_immutable_0019"),
        ("0020_legacy_open_rollback_fence.py", "ray_exec_legacy_open_insert_0020"),
        ("0020_legacy_open_rollback_fence.py", "ray_exec_legacy_open_retry_0020"),
        ("0025_ray_worker_target_capabilities.py", "ray_wtcap_lease_guard_0025"),
        ("0025_ray_worker_target_capabilities.py", "ray_wtcap_lease_update_0025"),
    }
)
_CREATE_TRIGGER_PATTERN = re.compile(
    r"CREATE\s+TRIGGER\s+(\w+)\s+(?:BEFORE|AFTER)\s+.*?\bON\s+\W*([a-zA-Z0-9_]+)",
    re.IGNORECASE | re.DOTALL,
)

# Experimental / quarantined modules that must never enter active hot paths.
_EXPERIMENTAL_MODULES = frozenset(
    {
        "django_ray.runtime.compiled_graph",
        "django_ray.runtime.compiled_graph_lifecycle",
        "django_ray.runtime.compiled_graph_probe",
        "django_ray.workflow.checkpoints",
        "django_ray.workflow.concurrency",
        "django_ray.workflow.map_transport",
        "django_ray.workflow.paged_input",
    }
)
_ZERO_IMPORTER_EXPERIMENTAL_MODULES = frozenset(
    {
        "django_ray.runtime.compiled_graph_probe",
        "django_ray.workflow.checkpoints",
        "django_ray.workflow.concurrency",
        "django_ray.workflow.map_transport",
        "django_ray.workflow.paged_input",
    }
)
_ACTIVE_HOT_PATH_MODULES = frozenset(
    {
        "django_ray._blob_storage",
        "django_ray._storage_types",
        "django_ray.admin",
        "django_ray.backends",
        "django_ray.conf.settings",
        "django_ray.conf.validation",
        "django_ray.decorators",
        "django_ray.input_storage",
        "django_ray.lease_policy",
        "django_ray.lifecycle",
        "django_ray.management.commands.django_ray_worker",
        "django_ray.payload_registry",
        "django_ray.ray_job_request_storage",
        "django_ray.result_storage",
        "django_ray.runner.cancellation",
        "django_ray.runner.leasing",
        "django_ray.runner.maintenance",
        "django_ray.runner.reconciliation",
    }
)
_ACTIVE_HOT_PATH_PREFIXES = (
    "django_ray.worker.",
    "django_ray.admin_views.",
)

# DB-first purge & worker modules checked for lock-held external blob / HTTP calls.
# (TaskInputPayload registry purge/reap/reactivation intentionally serialize under
# SELECT ... FOR UPDATE per the PostgreSQL coordination tests.)
_LOCK_BOUNDARY_MODULES = (
    PACKAGE_ROOT / "management" / "commands" / "django_ray_purge_executions.py",
    PACKAGE_ROOT / "worker" / "dispatcher.py",
    PACKAGE_ROOT / "worker" / "reconciler.py",
)
_FORBIDDEN_IN_ATOMIC_CALLS = frozenset(
    {
        "JobSubmissionClient",
        "_address_pinned_job_client",
        "_restore_request_payload",
        "_verify_or_restore_input_payload",
        "delete_blob_verified",
        "delete_input_reference",
        "delete_ray_job_request_reference",
        "delete_result_reference",
        "load_input_reference",
        "prepare_remote_cancellation",
        "request_remote_cancellation",
        "store_payload",
    }
)
_FORBIDDEN_IN_ATOMIC_IMPORTS = frozenset(
    {
        "django_ray._blob_storage",
        "ray.job_submission",
    }
)


def _discover_package_modules(package_root: Path = PACKAGE_ROOT) -> dict[str, Path]:
    """Map every dotted module name under ``package_root`` to its source path."""
    modules: dict[str, Path] = {}
    src_root = package_root.parent
    for path in sorted(package_root.rglob("*.py")):
        rel = path.relative_to(src_root).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _collect_module_imports(
    source: str,
    *,
    module_name: str,
    is_package_init: bool,
    known_modules: frozenset[str],
    include_type_checking: bool = True,
) -> set[str]:
    """Collect all internal module imports (top-level and lazy) from AST."""
    tree = ast.parse(source)
    edges: set[str] = set()

    def _resolve(node: ast.Import | ast.ImportFrom) -> set[str]:
        found: set[str] = set()
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        else:
            package_parts = module_name.split(".")
            if not is_package_init:
                package_parts = package_parts[:-1]
            if node.level > 0:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                if node.module:
                    base = [*base, *node.module.split(".")]
                target = ".".join(base)
            else:
                target = node.module or ""
            if target:
                found.add(target)
                for alias in node.names:
                    candidate = f"{target}.{alias.name}"
                    if candidate in known_modules:
                        found.add(candidate)
        return {mod for mod in found if mod in known_modules and mod != module_name}

    def _walk(node: ast.AST, *, in_type_checking: bool) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if include_type_checking or not in_type_checking:
                edges.update(_resolve(node))
            return
        if isinstance(node, ast.If):
            is_tc_guard = (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING") or (
                isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
            )
            for child in node.body:
                _walk(child, in_type_checking=in_type_checking or is_tc_guard)
            for child in node.orelse:
                _walk(child, in_type_checking=in_type_checking)
            return
        for child in ast.iter_child_nodes(node):
            _walk(child, in_type_checking=in_type_checking)

    _walk(tree, in_type_checking=False)
    return edges


def _find_direct_circular_import_pairs(
    graph: dict[str, set[str]],
) -> list[tuple[str, str]]:
    """Return sorted unique 2-node cycles ``(a, b)`` where ``a -> b`` and ``b -> a``."""
    pairs = {
        (min(left, right), max(left, right))
        for left, targets in graph.items()
        for right in targets
        if left in graph.get(right, set())
    }
    return sorted(pairs)


def _build_package_import_graph(
    *,
    include_type_checking: bool = True,
) -> dict[str, set[str]]:
    modules = _discover_package_modules()
    known = frozenset(modules)
    return {
        mod_name: _collect_module_imports(
            path.read_text(encoding="utf-8"),
            module_name=mod_name,
            is_package_init=path.name == "__init__.py",
            known_modules=known,
            include_type_checking=include_type_checking,
        )
        for mod_name, path in modules.items()
    }


def _is_atomic_context_call(expr: ast.expr) -> bool:
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    return (isinstance(func, ast.Attribute) and func.attr == "atomic") or (
        isinstance(func, ast.Name) and func.id == "atomic"
    )


def _call_target_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _find_lock_boundary_violations(source: str, *, label: str) -> list[str]:
    """Find forbidden external storage/HTTP calls or imports inside ``transaction.atomic()``."""
    tree = ast.parse(source)
    violations: list[str] = []

    def _scan(node: ast.AST, *, in_atomic: bool) -> None:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            entered = in_atomic or any(
                _is_atomic_context_call(item.context_expr) for item in node.items
            )
            for item in node.items:
                _scan(item.context_expr, in_atomic=in_atomic)
            for stmt in node.body:
                _scan(stmt, in_atomic=entered)
            return
        if in_atomic:
            if isinstance(node, ast.Call):
                name = _call_target_name(node)
                if name in _FORBIDDEN_IN_ATOMIC_CALLS:
                    violations.append(f"{label}:{node.lineno}: {name}() inside transaction.atomic")
            elif isinstance(node, ast.ImportFrom) and node.module in _FORBIDDEN_IN_ATOMIC_IMPORTS:
                violations.append(
                    f"{label}:{node.lineno}: import from {node.module} inside transaction.atomic"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_IN_ATOMIC_IMPORTS:
                        violations.append(
                            f"{label}:{node.lineno}: import {alias.name} inside transaction.atomic"
                        )
        for child in ast.iter_child_nodes(node):
            _scan(child, in_atomic=in_atomic)

    _scan(tree, in_atomic=False)
    return violations


class TestZeroDirectCircularImports:
    """Enforce zero direct 2-node import cycles across src/django_ray/ (F-073)."""

    def test_zero_direct_circular_import_pairs_runtime_and_lazy(self) -> None:
        graph = _build_package_import_graph(include_type_checking=False)
        cycles = _find_direct_circular_import_pairs(graph)
        assert cycles == [], f"Direct circular import pairs detected: {cycles}"

    def test_zero_direct_circular_import_pairs_including_type_checking(self) -> None:
        graph = _build_package_import_graph(include_type_checking=True)
        cycles = _find_direct_circular_import_pairs(graph)
        assert cycles == [], f"Direct circular import pairs (incl. TYPE_CHECKING): {cycles}"

    def test_extracted_leaf_modules_preserve_acyclic_directionality(self) -> None:
        graph = _build_package_import_graph(include_type_checking=True)

        assert graph["django_ray._version"] == set()
        assert "django_ray.conf.settings" not in graph["django_ray.conf.validation"]
        assert graph["django_ray.conf.settings"] <= {
            "django_ray.conf.defaults",
            "django_ray.conf.validation",
        }
        assert "django_ray.result_storage" not in graph["django_ray._blob_storage"]
        assert graph["django_ray._storage_types"].isdisjoint(
            {
                "django_ray.input_storage",
                "django_ray.payload_registry",
                "django_ray.ray_job_protocol",
                "django_ray.ray_job_request_storage",
            }
        )
        assert graph["django_ray.workflow._types"].isdisjoint(
            {
                "django_ray.workflows",
                "django_ray.workflow.strategy",
            }
        )
        assert "django_ray.workflows" not in graph["django_ray.workflow.strategy"]
        assert "django_ray.runtime" not in graph["django_ray.runtime.entrypoint"]

    def test_cycle_detector_catches_synthetic_direct_cycle(self) -> None:
        known = frozenset({"pkg.alpha", "pkg.beta"})
        alpha_imports = _collect_module_imports(
            "def f():\n    from pkg.beta import helper\n    return helper()\n",
            module_name="pkg.alpha",
            is_package_init=False,
            known_modules=known,
        )
        beta_imports = _collect_module_imports(
            "from pkg.alpha import f\n",
            module_name="pkg.beta",
            is_package_init=False,
            known_modules=known,
        )
        cycles = _find_direct_circular_import_pairs(
            {"pkg.alpha": alpha_imports, "pkg.beta": beta_imports}
        )
        assert cycles == [("pkg.alpha", "pkg.beta")]


class TestAdrToSchemaAndDormantGovernance:
    """Enforce ADR-to-Schema model/trigger governance and experimental isolation (F-010, F-095)."""

    def test_models_match_documented_adr_baseline(self) -> None:
        tree = ast.parse((PACKAGE_ROOT / "models.py").read_text(encoding="utf-8"))
        discovered_models: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                is_django_model = (isinstance(base, ast.Attribute) and base.attr == "Model") or (
                    isinstance(base, ast.Name) and base.id == "Model"
                )
                if is_django_model:
                    discovered_models.add(node.name)

        unapproved = sorted(discovered_models - _APPROVED_MODELS)
        assert unapproved == [], (
            "Unapproved Django model(s) added to src/django_ray/models.py without "
            f"ADR-to-Schema baseline update: {unapproved}"
        )
        assert discovered_models == _APPROVED_MODELS

    def test_no_unapproved_triggers_on_active_hot_tables(self) -> None:
        discovered_hot_triggers: set[tuple[str, str]] = set()
        for migration_path in sorted(MIGRATIONS_ROOT.glob("*.py")):
            text = migration_path.read_text(encoding="utf-8")
            for match in _CREATE_TRIGGER_PATTERN.finditer(text):
                trigger_name, table_target = match.group(1), match.group(2)
                if table_target in _HOT_TABLE_TARGETS:
                    discovered_hot_triggers.add((migration_path.name, trigger_name))

        unapproved = sorted(discovered_hot_triggers - _APPROVED_HOT_TABLE_TRIGGERS)
        assert unapproved == [], (
            "Unapproved trigger(s) targeting active hot tables "
            f"(RayTaskExecution/TaskWorkerLease/TaskAttempt): {unapproved}"
        )
        assert discovered_hot_triggers == _APPROVED_HOT_TABLE_TRIGGERS

    def test_experimental_modules_do_not_leak_into_active_hot_paths(self) -> None:
        graph = _build_package_import_graph(include_type_checking=True)
        hot_leaks: dict[str, list[str]] = {}
        for mod_name, imports in sorted(graph.items()):
            is_hot = mod_name in _ACTIVE_HOT_PATH_MODULES or any(
                mod_name.startswith(prefix) for prefix in _ACTIVE_HOT_PATH_PREFIXES
            )
            if not is_hot:
                continue
            leaked = sorted(imports & _EXPERIMENTAL_MODULES)
            if leaked:
                hot_leaks[mod_name] = leaked
        assert hot_leaks == {}, f"Experimental modules imported by active hot paths: {hot_leaks}"

    def test_quarantined_zero_importer_modules_remain_unwired(self) -> None:
        graph = _build_package_import_graph(include_type_checking=True)
        unexpected_importers: dict[str, list[str]] = {}
        for mod_name, imports in sorted(graph.items()):
            if mod_name in _EXPERIMENTAL_MODULES:
                continue
            hits = sorted(imports & _ZERO_IMPORTER_EXPERIMENTAL_MODULES)
            if hits:
                unexpected_importers[mod_name] = hits
        assert unexpected_importers == {}, (
            "Quarantined experimental modules imported into runtime code without "
            f"promotion review: {unexpected_importers}"
        )


class TestLockBoundaryStaticConformance:
    """Ensure storage and HTTP helpers are not newly introduced inside transaction.atomic()."""

    def test_storage_and_purge_modules_keep_blob_and_http_io_outside_atomic_blocks(self) -> None:
        all_violations: list[str] = []
        for path in _LOCK_BOUNDARY_MODULES:
            label = path.relative_to(PROJECT_ROOT).as_posix()
            all_violations.extend(
                _find_lock_boundary_violations(path.read_text(encoding="utf-8"), label=label)
            )
        assert all_violations == [], (
            "External storage/HTTP calls detected inside transaction.atomic(): "
            + "; ".join(all_violations)
        )

    def test_blob_and_result_storage_remain_free_of_django_db_transactions(self) -> None:
        for rel_path in ("_blob_storage.py", "result_storage.py"):
            tree = ast.parse((PACKAGE_ROOT / rel_path).read_text(encoding="utf-8"))
            db_imports: list[int] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("django.db"):
                    db_imports.append(node.lineno)
                elif isinstance(node, ast.Import):
                    if any(alias.name.startswith("django.db") for alias in node.names):
                        db_imports.append(node.lineno)
            assert db_imports == [], f"{rel_path} must not import django.db (lines {db_imports})"

    def test_lock_boundary_checker_catches_synthetic_violation(self) -> None:
        bad_source = (
            "from django.db import transaction\n"
            "def bad():\n"
            "    with transaction.atomic():\n"
            "        delete_blob_verified(None, 's3://bucket/key')\n"
        )
        violations = _find_lock_boundary_violations(bad_source, label="synthetic.py")
        assert violations == ["synthetic.py:4: delete_blob_verified() inside transaction.atomic"]


class TestTopLevelPublicExports:
    """Verify public top-level @ray_task and RayTaskResult exports on django_ray (#836)."""

    def test_django_ray_reexports_ray_task_contract_and_results(self) -> None:
        import django_ray
        from django_ray._version import __version__ as leaf_version
        from django_ray.backends import RayTaskResult, ResultUnavailableError
        from django_ray.decorators import (
            RayTaskContract,
            RayTaskContractError,
            get_ray_task_contract,
            ray_task,
        )

        assert django_ray.__version__ == leaf_version
        assert django_ray.ray_task is ray_task
        assert django_ray.RayTaskContract is RayTaskContract
        assert django_ray.RayTaskContractError is RayTaskContractError
        assert django_ray.get_ray_task_contract is get_ray_task_contract
        assert django_ray.RayTaskResult is RayTaskResult
        assert django_ray.ResultUnavailableError is ResultUnavailableError
        for symbol in (
            "RayTaskContract",
            "RayTaskContractError",
            "RayTaskResult",
            "ResultUnavailableError",
            "__version__",
            "get_ray_task_contract",
            "ray_task",
        ):
            assert symbol in django_ray.__all__
        with pytest.raises(AttributeError):
            django_ray.__getattr__("nonexistent_public_symbol_xyz")
