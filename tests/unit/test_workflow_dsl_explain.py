"""Unit tests for Workflow DSL 2.0 combinators and dry-run plan explanation."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from django.tasks import task as django_task

from django_ray.workflow.dsl import (
    EXPLAIN_SCHEMA,
    EXPLAIN_SCHEMA_VERSION,
    WorkflowBuilder,
    WorkflowExpr,
    branch,
    explain_workflow,
    parallel,
    pipe,
)
from django_ray.workflow.plans import WorkflowPlanBuildContext
from django_ray.workflows import (
    Chain,
    Group,
    Map,
    Step,
    WorkflowDefinitionError,
    WorkflowSignature,
    chain,
    group,
    map_step,
    step,
)

# ---------------------------------------------------------------------------
# Module-level fixture callables (never executed during explain_workflow!)
# ---------------------------------------------------------------------------

EXECUTED_CALLS: list[str] = []


def produce_items(count: int) -> list[int]:
    EXECUTED_CALLS.append("produce_items")
    return list(range(1, count + 1))


def add_one(value: int) -> int:
    EXECUTED_CALLS.append("add_one")
    return value + 1


def double(value: int) -> int:
    EXECUTED_CALLS.append("double")
    return value * 2


def triple(value: int) -> int:
    EXECUTED_CALLS.append("triple")
    return value * 3


def sum_items(values: list[int]) -> int:
    EXECUTED_CALLS.append("sum_items")
    return sum(values)


def fold_sum(acc: int, item: int) -> int:
    return acc + item


def preview_int(value: int) -> dict[str, int]:
    return {"value": value}


def is_even(value: int) -> bool:
    EXECUTED_CALLS.append("is_even")
    return value % 2 == 0


def always_fail(value: int) -> int:
    EXECUTED_CALLS.append("always_fail")
    raise ValueError(f"boom:{value}")


def fail_runtime(value: int) -> int:
    raise RuntimeError(f"fatal:{value}")


def recover_value(value: int) -> int:
    EXECUTED_CALLS.append("recover_value")
    return -value


def recover_with_error(value: int, error: Exception) -> dict[str, Any]:
    EXECUTED_CALLS.append("recover_with_error")
    return {"value": value, "error": str(error)}


def accept_secret(value: int, *, api_token: str = "") -> int:
    del api_token
    return value


async def async_predicate(value: int) -> bool:
    return value > 0


@django_task
def task_predicate(value: int) -> bool:
    return value > 0


class _CallableObject:
    def __call__(self, value: int) -> bool:
        return value > 0


class _MethodHolder:
    def method(self, value: int) -> int:
        return value


class _CustomWorkflowBuilder:
    def __init__(self, signature: WorkflowSignature) -> None:
        self._signature = signature

    def build(self) -> WorkflowSignature:
        return self._signature


class _BrokenWorkflowBuilder:
    def build(self) -> Any:
        return "not-a-signature"


class _CustomAppError(ValueError):
    """Non-builtin exception used to verify qualified exception formatting."""


@pytest.fixture(autouse=True)
def _allow_test_workflow_module(settings: Any) -> None:
    EXECUTED_CALLS.clear()
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "ALLOWED_CALLABLE_MODULES": [__name__],
        "RUNTIME_ENV_PROFILES": {
            "ml-gpu": {"env_vars": {"SERVICE_TOKEN": "super-secret-profile-value"}},
        },
    }


class TestWorkflowExprCombinators:
    """Verify immutable | / & / pipe / parallel / map composition and execution."""

    def test_pipe_and_operator_flatten_to_native_chain(self) -> None:
        expr = WorkflowExpr(add_one) | double | triple
        built = expr.build()

        assert isinstance(built, Chain)
        assert len(built.signatures) == 3
        assert [s.callable_path for s in built.signatures if isinstance(s, Step)] == [
            f"{__name__}.add_one",
            f"{__name__}.double",
            f"{__name__}.triple",
        ]
        assert expr.run(4, use_ray=False) == 30
        assert (pipe(add_one, double) | pipe(triple)).build() == built
        assert pipe(add_one).build() == step(add_one)

    def test_parallel_and_ampersand_flatten_to_native_group(self) -> None:
        expr = WorkflowExpr(double) & triple & add_one
        built = expr.build()

        assert isinstance(built, Group)
        assert len(built.signatures) == 3
        assert expr.run(5, use_ray=False) == [10, 15, 6]
        assert (parallel(double, triple) & add_one).build() == built
        assert parallel(double).run(5, use_ray=False) == [10]

    def test_right_hand_operators_with_signature_and_callable(self) -> None:
        piped = step(add_one) | WorkflowExpr(double)
        grouped = add_one & WorkflowExpr(double)
        diamond = piped | grouped | sum_items

        assert isinstance(diamond.build(), Chain)
        # (3 + 1) * 2 = 8 -> [9, 16] -> 25
        assert diamond.run(3, use_ray=False) == 25

    def test_map_method_with_limits_and_options(self) -> None:
        expr = (
            WorkflowExpr(produce_items).map(
                double,
                max_concurrency=4,
                max_items=10,
                cancel_timeout_seconds=2.5,
                ray_options={"num_cpus": 0.25},
                runtime_env="ml-gpu",
            )
            | sum_items
        )
        built = expr.build()

        assert isinstance(built, Chain)
        assert isinstance(built.signatures[1], Map)
        mapped = built.signatures[1]
        assert mapped.max_concurrency == 4
        assert mapped.max_items == 10
        assert mapped.cancel_timeout_seconds == 2.5
        assert expr.run(4, use_ray=False) == 20  # [2, 4, 6, 8] -> 20

    def test_map_accepts_workflow_signature_builder_and_expr(self) -> None:
        sig_map = WorkflowExpr(produce_items).map(step(double))
        builder_map = WorkflowExpr(produce_items).map(_CustomWorkflowBuilder(step(triple)))
        expr_map = WorkflowExpr(produce_items).map(WorkflowExpr(add_one) | double)

        assert sig_map.run(3, use_ray=False) == [2, 4, 6]
        assert builder_map.run(3, use_ray=False) == [3, 6, 9]
        assert expr_map.run(3, use_ray=False) == [4, 6, 8]

    def test_immutability_equality_and_secret_free_repr(self) -> None:
        secret_step = step(accept_secret, api_token="sk-live-secret-999")
        expr1 = WorkflowExpr(secret_step)
        expr2 = WorkflowExpr(secret_step)

        assert expr1 == expr2
        assert (expr1 == "other") is False
        assert "sk-live-secret-999" not in repr(expr1)
        assert repr(expr1) == "WorkflowExpr(kind='step')"
        assert repr(pipe(add_one, double)) == "WorkflowExpr(kind='chain')"
        assert repr(parallel(add_one, double)) == "WorkflowExpr(kind='group')"
        assert repr(WorkflowExpr(map_step(double))) == "WorkflowExpr(kind='map')"
        assert (
            repr(branch(is_even, on_true=double, on_false=triple)) == "WorkflowExpr(kind='branch')"
        )
        assert repr(WorkflowExpr(add_one).with_fallback(recover_value)) == (
            "WorkflowExpr(kind='fallback')"
        )

        with pytest.raises(AttributeError, match="immutable"):
            expr1.node = step(add_one)  # type: ignore[misc]
        with pytest.raises(AttributeError, match="immutable"):
            del expr1._node  # type: ignore[misc]

    def test_custom_workflow_builder_protocol_coercion(self) -> None:
        builder = _CustomWorkflowBuilder(chain(step(add_one), step(double)))
        assert isinstance(builder, WorkflowBuilder)
        expr = WorkflowExpr(builder) | triple
        assert expr.run(2, use_ray=False) == 18


class TestBranchAndFallback:
    """Verify declarative branch() and .with_fallback() composition and sync execution."""

    def test_branch_routes_both_paths_and_composes_with_pipe_and_group(self) -> None:
        routed = (
            WorkflowExpr(add_one)
            | branch(is_even, on_true=double, on_false=triple)
            | parallel(add_one, double)
        )

        # Input 3 -> add_one=4 (even) -> double=8 -> [9, 16]
        assert routed.run(3, use_ray=False) == [9, 16]
        # Input 4 -> add_one=5 (odd) -> triple=15 -> [16, 30]
        assert routed.run(4, use_ray=False) == [16, 30]

        with pytest.raises(
            WorkflowDefinitionError, match="Cannot build a static WorkflowSignature"
        ):
            routed.build()

    def test_branch_accepts_dotted_predicate_string_respecting_allowlist(self) -> None:
        routed = branch(
            f"{__name__}.is_even",
            on_true=f"{__name__}.double",
            on_false=f"{__name__}.triple",
        )
        assert routed.run(2, use_ray=False) == 4
        assert routed.run(3, use_ray=False) == 9

    def test_branch_dotted_predicate_blocked_when_not_in_allowlist(self, settings: Any) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "ALLOWED_CALLABLE_MODULES": ["testproject"]}
        routed = branch(
            f"{__name__}.is_even",
            on_true=double,
            on_false=triple,
        )
        with pytest.raises(ImportError, match="not permitted"):
            routed.run(2, use_ray=False)

    def test_with_fallback_recovers_matching_exception_and_propagates_others(self) -> None:
        recovered = WorkflowExpr(always_fail).with_fallback(
            recover_value,
            exceptions=(ValueError,),
        )
        assert recovered.run(7, use_ray=False) == -7

        with_err = WorkflowExpr(always_fail).with_fallback(
            recover_with_error,
            exceptions=ValueError,
            pass_error=True,
        )
        assert with_err.run(5, use_ray=False) == {"value": 5, "error": "boom:5"}

        narrow = WorkflowExpr(fail_runtime).with_fallback(
            recover_value,
            exceptions=(ValueError,),
        )
        with pytest.raises(RuntimeError, match="fatal:9"):
            narrow.run(9, use_ray=False)

    def test_conditional_map_execution_and_bounds(self) -> None:
        conditional_body = branch(is_even, on_true=double, on_false=triple)
        wf = WorkflowExpr(produce_items).map(conditional_body, max_items=5, max_concurrency=2)

        # [1, 2, 3, 4] -> [3, 4, 9, 8]
        assert wf.run(4, use_ray=False) == [3, 4, 9, 8]

        with pytest.raises(WorkflowDefinitionError, match="max_items=5"):
            wf.run(6, use_ray=False)

        # Direct map execution against a non-sized generator enforces max_items lazily.
        chain_node: Any = wf.node
        gen_wf = WorkflowExpr._from_node(chain_node.children[1])
        with pytest.raises(WorkflowDefinitionError, match="max_items=5"):
            gen_wf.run((i for i in range(10)), use_ray=False)
        with pytest.raises(WorkflowDefinitionError, match="exactly one iterable"):
            gen_wf.run(1, 2, use_ray=False)
        with pytest.raises(WorkflowDefinitionError, match="non-string iterable"):
            gen_wf.run("abc", use_ray=False)

    def test_parallel_and_chain_composition_with_conditional_nodes(self) -> None:
        fb = WorkflowExpr(always_fail).with_fallback(recover_value, exceptions=_CustomAppError)
        par = parallel(fb, add_one)
        chained = par | (WorkflowExpr(sum_items) & sum_items)
        assert isinstance(par, WorkflowExpr)
        # Verify group flattening with conditional node.
        par_flat = (
            WorkflowExpr(branch(is_even, on_true=double, on_false=triple)) & add_one
        ) & double
        assert par_flat.run(2, use_ray=False) == [4, 3, 4]
        assert chained.explain(format="dict")["topology_class"] == "conditional"


class TestExplainWorkflow:
    """Verify dry-run explain_workflow text/dict output, plan fingerprint, and secret safety."""

    def test_explain_does_not_execute_callables_and_includes_plan_fingerprint(self) -> None:
        wf = chain(
            step(produce_items),
            map_step(
                step(
                    double,
                    django=True,
                    ray_options={"num_cpus": 0.25, "num_gpus": 0},
                    runtime_env="ml-gpu",
                ).with_output_preview(preview_int)
            )
            .with_limits(max_concurrency=4, max_items=100)
            .with_result_buffer(
                max_serialized_bytes=1024 * 1024,
                actor_options={"num_cpus": 0.25, "memory": 2 * 1024 * 1024},
            ),
            step(sum_items),
        )
        build_ctx = WorkflowPlanBuildContext(build_revision="build:abc123")

        report = explain_workflow(wf, format="dict", build_context=build_ctx)
        text = explain_workflow(wf, format="text", build_context=build_ctx)

        assert EXECUTED_CALLS == []
        assert report["schema"] == EXPLAIN_SCHEMA
        assert report["schema_version"] == EXPLAIN_SCHEMA_VERSION
        assert report["root_kind"] == "chain"
        assert report["topology_class"] == "dynamic"
        assert report["stability"]["classification"] == "dynamic_bounded"
        assert report["summary"] == {
            "total_nodes": 5,
            "step_count": 3,
            "group_count": 0,
            "map_count": 1,
            "branch_count": 0,
            "fallback_count": 0,
            "has_unbounded_map": False,
            "has_django_steps": True,
            "runtime_env_profiles": ["ml-gpu"],
        }
        assert isinstance(report["plan"]["fingerprint"], str)
        assert report["plan"]["fingerprint"].startswith("sha256:")
        assert "local" in report["plan"]["eligible_strategies"]
        assert "dynamic_tasks" in report["plan"]["eligible_strategies"]

        map_node = next(n for n in report["nodes"] if n["kind"] == "map")
        assert map_node["limits"]["bounded"] is True
        assert map_node["result_transport"]["mode"] == "result_buffer"
        assert map_node["result_transport"]["max_serialized_bytes"] == 1024 * 1024

        step_node = next(n for n in report["nodes"] if n["callable_path"] == f"{__name__}.double")
        assert step_node["runtime_env"] == {"mode": "profile", "profile": "ml-gpu"}
        assert step_node["output_preview"] == f"{__name__}.preview_int"
        assert step_node["ray_options"]["num_cpus"] == 0.25

        assert "Workflow Explain" in text
        assert report["plan"]["fingerprint"] in text
        assert "ml-gpu" in text
        assert "result_buffer(1048576B)" in text
        assert f"preview={__name__}.preview_int" in text

    def test_explain_never_leaks_secrets_from_runtime_env_bound_args_or_fold_initial(self) -> None:
        secret_inline_token = "SECRET-INLINE-TOKEN-XYZ-98765"
        secret_bound_kwarg = "SECRET-BOUND-KWARG-ABC-54321"
        secret_fold_initial = "SECRET-FOLD-INIT-TOKEN-11111"

        wf = chain(
            step(
                accept_secret,
                "SECRET-POSITIONAL-ARG",
                api_token=secret_bound_kwarg,
                runtime_env={
                    "env_vars": {"API_TOKEN": secret_inline_token},
                    "pip": ["requests==2.32.0"],
                },
            ),
            map_step(add_one)
            .with_limits(max_concurrency=2, max_items=10)
            .reduce(
                step(fold_sum),
                initial={"secret": secret_fold_initial},
                max_serialized_bytes=4096,
                actor_options={"num_cpus": 0.25, "memory": 8192},
            ),
        )

        report = explain_workflow(wf, format="dict")
        text = explain_workflow(wf, format="text")
        serialized = json.dumps(report)

        for secret in (
            secret_inline_token,
            secret_bound_kwarg,
            secret_fold_initial,
            "SECRET-POSITIONAL-ARG",
            "super-secret-profile-value",
        ):
            assert secret not in serialized
            assert secret not in text

        step_node = report["nodes"][1]
        assert step_node["runtime_env"] == {
            "mode": "inline",
            "profile": None,
            "keys": ["env_vars", "pip"],
        }
        assert step_node["bound_arg_count"] == 1
        assert step_node["bound_kwarg_names"] == ["api_token"]
        assert report["plan"]["retry_safe"] is False
        assert "Retry-unsafe paths:" in text

        map_node = next(n for n in report["nodes"] if n["kind"] == "map")
        assert map_node["result_transport"]["mode"] == "result_fold"
        assert map_node["result_transport"]["reducer"] == f"{__name__}.fold_sum"
        assert "result_fold(" in text

    def test_explain_static_unbounded_map_and_conditional_classifications(self) -> None:
        static_report = explain_workflow(pipe(add_one, parallel(double, triple)), format="dict")
        assert static_report["topology_class"] == "static"
        assert static_report["stability"]["classification"] == "static_deterministic"
        assert static_report["summary"]["group_count"] == 1

        unbounded_wf = chain(step(produce_items), map_step(double))
        unbounded_report = explain_workflow(unbounded_wf, format="dict")
        unbounded_text = explain_workflow(unbounded_wf, format="text")
        assert unbounded_report["topology_class"] == "dynamic"
        assert unbounded_report["stability"]["classification"] == "dynamic_unbounded"
        assert unbounded_report["summary"]["has_unbounded_map"] is True
        assert "transport=default" in unbounded_text

        group_report = explain_workflow(group(step(double), step(triple)), format="dict")
        assert group_report["root_kind"] == "group"
        assert group_report["summary"]["group_count"] == 1

        cond_expr = WorkflowExpr(add_one) | branch(
            is_even, on_true=double, on_false=triple
        ).with_fallback(
            recover_value,
            exceptions=(ValueError, _CustomAppError),
            pass_error=True,
        )
        cond_dict = cond_expr.explain(format="dict")
        cond_text = cond_expr.explain(format="text")

        assert cond_dict["topology_class"] == "conditional"
        assert cond_dict["stability"]["classification"] == "conditional"
        assert cond_dict["summary"]["branch_count"] == 1
        assert cond_dict["summary"]["fallback_count"] == 1
        assert cond_dict["plan"]["fingerprint"] is None
        assert cond_dict["plan"]["unavailable_reason"] == "conditional_dsl_expression"
        assert "branch predicate=" in cond_text
        assert "fallback on=(ValueError," in cond_text
        assert "unavailable (conditional_dsl_expression)" in cond_text

    def test_explain_handles_unknown_runtime_env_profile_without_crashing(self) -> None:
        wf = step(add_one, runtime_env="nonexistent-profile")
        report = explain_workflow(wf, format="dict")
        text = explain_workflow(wf, format="text")

        assert report["plan"]["fingerprint"] is None
        assert report["plan"]["unavailable_reason"] is not None
        assert "ImproperlyConfigured" in report["plan"]["unavailable_reason"]
        assert "Plan fingerprint: unavailable" in text

    def test_explain_accepts_workflow_builder_and_handles_non_scalar_ray_options(self) -> None:
        sig = step(
            add_one, ray_options={"resources": {"gpu_pool": 1}, "scheduling_strategy": object()}
        )
        builder = _CustomWorkflowBuilder(sig)
        report = explain_workflow(builder, format="dict")
        step_node = report["nodes"][0]

        assert step_node["ray_options"]["resources"] == {"gpu_pool": 1}
        assert step_node["ray_options"]["scheduling_strategy"] == "object"


class TestValidationAndArchitecture:
    """Verify strict eager validation and zero circular imports."""

    def test_invalid_inputs_rejected_eagerly(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="at least one step"):
            pipe()
        with pytest.raises(WorkflowDefinitionError, match="at least one step"):
            parallel()
        with pytest.raises(TypeError, match="Workflow DSL target"):
            WorkflowExpr(123)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="WorkflowBuilder.build"):
            WorkflowExpr(_BrokenWorkflowBuilder())
        with pytest.raises(TypeError, match="WorkflowBuilder.build"):
            explain_workflow(_BrokenWorkflowBuilder())
        with pytest.raises(TypeError, match="WorkflowSignature"):
            explain_workflow(123)  # type: ignore[arg-type]
        invalid_format: Any = "yaml"
        with pytest.raises(ValueError, match="format must be"):
            explain_workflow(step(add_one), format=invalid_format)

        class _UnsupportedSignature(WorkflowSignature):
            def _submit(
                self,
                executor: Any,
                input_args: tuple[Any, ...],
                input_kwargs: dict[str, Any],
                node_id: str,
                dependencies: tuple[str, ...],
            ) -> Any:
                raise NotImplementedError

        with pytest.raises(TypeError, match="Unsupported workflow node type"):
            explain_workflow(_UnsupportedSignature())

        with pytest.raises(TypeError):
            _ = WorkflowExpr(add_one) | 123
        with pytest.raises(TypeError):
            _ = 123 | WorkflowExpr(add_one)
        with pytest.raises(TypeError):
            _ = WorkflowExpr(add_one) & 123
        with pytest.raises(TypeError):
            _ = 123 & WorkflowExpr(add_one)

    def test_branch_and_fallback_validation(self) -> None:
        def local_pred(x: int) -> bool:
            return x > 0

        task_branch = branch(task_predicate, on_true=double, on_false=triple)
        assert task_branch.run(5, use_ray=False) == 10
        assert task_branch.run(-1, use_ray=False) == -3

        with pytest.raises(WorkflowDefinitionError, match="module-level callable"):
            branch(_CallableObject(), on_true=double, on_false=triple)
        with pytest.raises(WorkflowDefinitionError, match="module-level callable"):
            branch(local_pred, on_true=double, on_false=triple)
        with pytest.raises(WorkflowDefinitionError, match="module-level callable"):
            branch(lambda x: x > 0, on_true=double, on_false=triple)
        with pytest.raises(WorkflowDefinitionError, match="not a method"):
            branch(_MethodHolder().method, on_true=double, on_false=triple)
        with pytest.raises(WorkflowDefinitionError, match="synchronous"):
            branch(async_predicate, on_true=double, on_false=triple)
        with pytest.raises(WorkflowDefinitionError, match="dotted import path"):
            branch("not_dotted", on_true=double, on_false=triple)
        with pytest.raises(WorkflowDefinitionError, match="valid Python identifiers"):
            branch("bad.123invalid", on_true=double, on_false=triple)
        with pytest.raises(TypeError, match="importable callable"):
            branch(42, on_true=double, on_false=triple)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="pass_error must be a boolean"):
            WorkflowExpr(add_one).with_fallback(recover_value, pass_error=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="at least one Exception subclass"):
            WorkflowExpr(add_one).with_fallback(recover_value, exceptions=())
        with pytest.raises(TypeError, match="Exception subclasses"):
            WorkflowExpr(add_one).with_fallback(recover_value, exceptions=(BaseException,))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="Exception subclasses"):
            WorkflowExpr(add_one).with_fallback(recover_value, exceptions=(KeyboardInterrupt,))  # type: ignore[arg-type]

    def test_map_validation_errors(self) -> None:
        base = WorkflowExpr(produce_items)
        with pytest.raises(TypeError, match="integer or None"):
            base.map(double, max_concurrency=True)
        with pytest.raises(ValueError, match="at least 1"):
            base.map(double, max_items=0)
        with pytest.raises(TypeError, match="must be a number"):
            base.map(double, cancel_timeout_seconds="fast")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="finite and non-negative"):
            base.map(double, cancel_timeout_seconds=-1.0)
        with pytest.raises(WorkflowDefinitionError, match="WorkflowExpr"):
            base.map(WorkflowExpr(double), django=True)
        with pytest.raises(WorkflowDefinitionError, match="WorkflowBuilder"):
            base.map(_CustomWorkflowBuilder(step(double)), runtime_env="ml-gpu")
        with pytest.raises(TypeError, match="map target"):
            base.map(123)  # type: ignore[arg-type]

    def test_dsl_module_has_zero_circular_imports(self) -> None:
        src_root = Path(__file__).resolve().parents[2] / "src" / "django_ray"
        dsl_path = src_root / "workflow" / "dsl.py"
        assert dsl_path.is_file()

        # workflow/dsl.py must never import django_ray.workflows (strict DAG).
        dsl_tree = ast.parse(dsl_path.read_text(encoding="utf-8"))
        dsl_imports_workflows = False
        for node in ast.walk(dsl_tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "django_ray.workflows" for alias in node.names
            ):
                dsl_imports_workflows = True
            elif isinstance(node, ast.ImportFrom) and node.module == "django_ray.workflows":
                dsl_imports_workflows = True
        assert dsl_imports_workflows is False

        importers: list[str] = []
        for py_file in src_root.rglob("*.py"):
            if py_file == dsl_path:
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == "django_ray.workflow.dsl" for alias in node.names):
                        importers.append(py_file.relative_to(src_root).as_posix())
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module == "django_ray.workflow.dsl" or (
                        node.module == "django_ray.workflow"
                        and any(alias.name == "dsl" for alias in node.names)
                    ):
                        importers.append(py_file.relative_to(src_root).as_posix())

        assert sorted(set(importers)) == ["workflows.py"]
