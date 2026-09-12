"""Work limits are enforced before running sample code or submitting children."""

import ast
from pathlib import Path

import pytest

from django_ray.runtime.distributed import _collect_remote_results
from testproject import tasks
from testproject.apps.cluster_tasks import tasks as cluster_tasks
from testproject.apps.cluster_tasks import workflows
from testproject.apps.local_ray import tasks as local_tasks
from testproject.apps.ml_pipeline import tasks as ml_tasks
from testproject.route_policy import POST_ROUTE_CAPABILITIES, is_demo_route
from testproject.workload_limits import validate_call, validate_json
from tests.unit.test_distributed_cleanup import _CollectorRay


@pytest.mark.parametrize(
    "task,args,kwargs",
    [
        (tasks.slow_task, (), {"seconds": float("nan")}),
        (tasks.slow_task, (), {"seconds": float("inf")}),
        (tasks.slow_task, (), {"seconds": True}),
        (tasks.slow_task, (), {"seconds": 301}),
        (local_tasks.stress_cpu, (), {"duration_seconds": 11}),
        (local_tasks.stress_memory, (), {"size_mb": 33}),
        (local_tasks.stress_memory, (), {"size_mb": True}),
        (local_tasks.stress_nested_compute, (), {"depth": 6, "width": 10}),
        (local_tasks.stress_json_payload, (), {"size_kb": 65, "depth": 4}),
        (local_tasks.stress_json_payload, (), {"size_kb": 1, "depth": 5}),
        (local_tasks.stress_prime_search, (), {"start": 1000001, "count": 1}),
        (local_tasks.stress_prime_search, (), {"start": 0, "count": 101}),
        (local_tasks.stress_concurrent_simulation, (), {"task_count": 101}),
        (local_tasks.simulate_workload, (), {"iterations": 2000001}),
        (local_tasks.simulate_workload, (), {"sleep_ms": 10001}),
        (local_tasks.fibonacci, (10001,), {}),
        (local_tasks.prime_check, (100000001,), {}),
        (cluster_tasks.process_chunk, (list(range(101)),), {}),
        (cluster_tasks.batch_http_requests, (["x"],), {"timeout_seconds": 31}),
        (cluster_tasks.distributed_cpu_benchmark, (), {"num_items": 101}),
        (cluster_tasks.complex_workflow_benchmark, (), {"fast_items": 51, "slow_items": 50}),
        (cluster_tasks.runtime_env_benchmark, ("profile",), {"repeats": 11}),
        (ml_tasks.train_model, ("data",), {"epochs": 101}),
        (
            ml_tasks.hyperparameter_search,
            ("data", {"a": list(range(11)), "b": list(range(10))}),
            {},
        ),
        (ml_tasks.feature_engineering, ([], [{"type": "polynomial", "params": {"degree": 5}}]), {}),
    ],
    ids=lambda value: value.func.__name__ if hasattr(value, "func") else None,
)
def test_direct_task_calls_reject_before_work(task, args, kwargs):
    with pytest.raises(ValueError):
        task.call(*args, **kwargs)


def test_valid_numeric_boundaries_and_small_computation():
    validate_call(local_tasks.stress_memory.func, (), {"size_mb": 32})
    validate_call(tasks.slow_task.func, (), {"seconds": 300})
    validate_call(local_tasks.stress_nested_compute.func, (), {"depth": 5, "width": 10})
    assert local_tasks.stress_nested_compute.call(depth=3, width=2)["width"] == 2
    assert local_tasks.matrix_multiply.call([[2.0]], [[3.0]]) == [[6.0]]
    assert local_tasks.stress_json_payload.call(size_kb=1, depth=4)["actual_size_bytes"] < 1200


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -(10**500), [0] * 101, "x" * 2049])
def test_structural_values_are_bounded_without_running_tasks(value):
    with pytest.raises(ValueError):
        validate_json(value)


def test_sample_fanout_uses_four_slots_and_preserves_skewed_result_order():
    class WindowRay(_CollectorRay):
        def remote(self, value):
            assert len(self.submitted) - len(self.consumed) < 4
            return super().remote(value)

    ray = WindowRay()
    result = _collect_remote_results(ray, ray, [(i,) for i in range(100)], 4)
    assert result == [i * 10 for i in range(100)]
    assert not ray.cancelled


@pytest.mark.parametrize("failure", [ValueError("failure"), KeyboardInterrupt()])
def test_sample_window_cleans_children_after_failure_or_cancellation(failure):
    ray = _CollectorRay()
    ray.failure = failure
    ray.fail_get = True
    with pytest.raises(type(failure)):
        _collect_remote_results(ray, ray, [(i,) for i in range(100)], 4)
    assert ray.submitted == [0, 1, 2, 3]
    assert ray.cancelled == [(i, False, True) for i in range(4)]


def test_all_sample_dynamic_fanout_sites_declare_limits():
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "testproject/apps/cluster_tasks/tasks.py",
        "testproject/apps/cluster_tasks/workflows.py",
    ):
        tree = ast.parse((root / relative).read_text())
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id in {"parallel_map", "parallel_starmap"}:
                assert any(
                    keyword.arg == "max_concurrency" and ast.literal_eval(keyword.value) == 4
                    for keyword in call.keywords
                )
            elif call.func.id == "map_step":
                attribute = parents[call]
                assert isinstance(attribute, ast.Attribute) and attribute.attr == "with_limits"
                limits = {
                    keyword.arg: ast.literal_eval(keyword.value)
                    for keyword in parents[attribute].keywords
                }
                assert limits == {"max_items": 100, "max_concurrency": 4}


def test_workflow_builder_rejects_before_materializing_work():
    with pytest.raises(ValueError):
        workflows.build_cpu_work_items(101, 0.01)
    with pytest.raises(ValueError):
        workflows.build_complex_config(51, 50, 0.01, 0.01)
    with pytest.raises(ValueError):
        workflows.run_runtime_env_cache_benchmark("profile", repeats=11, use_ray=False)


def test_every_mutating_route_is_classified_and_enqueued_through_admission():
    source = Path(__file__).resolve().parents[2] / "testproject/api.py"
    routes = {}
    for node in ast.parse(source.read_text()).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "api"
                and decorator.func.attr == "post"
            ):
                routes[decorator.args[0].value] = node
    assert routes.keys() == POST_ROUTE_CAPABILITIES.keys()
    for route, node in routes.items():
        assert is_demo_route(route) == (POST_ROUTE_CAPABILITIES[route] == "demo")
        calls = [value for value in ast.walk(node) if isinstance(value, ast.Call)]
        assert not any(
            isinstance(call.func, ast.Attribute) and call.func.attr == "enqueue" for call in calls
        )
        if POST_ROUTE_CAPABILITIES[route] != "lifecycle":
            assert any(
                isinstance(call.func, ast.Name) and call.func.id == "enqueue_sample"
                for call in calls
            )
