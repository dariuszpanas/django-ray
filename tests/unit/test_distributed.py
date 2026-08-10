"""Unit tests for distributed computing utilities."""

from __future__ import annotations

import json
import pickle
from typing import Any

import pytest

from django_ray.execution_codec import (
    ExecutionCompletionSource,
    ExecutionIdentity,
    NestedExecutionBoundaryKind,
    NestedExecutionRequestRejected,
    NestedExecutionRequestRejection,
    decode_execution_completion,
)
from django_ray.runtime import distributed, entrypoint
from django_ray.runtime.context import durable_task_execution, get_current_task_context
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.workflow_plans import runtime_env_plan_identity


# Module-level functions for Ray tests (must be picklable)
def _square(x: int) -> int:
    """Square a number - used in Ray parallel_map test."""
    return x * x


def _strict_context_snapshot(value: int) -> dict[str, Any]:
    """Return the context installed after strict leaf validation."""
    context = get_current_task_context()
    assert context is not None
    return {
        "value": value,
        "task_pk": context.task_pk,
        "task_id": context.task_id,
        "attempt_number": context.attempt_number,
        "execution_generation": context.execution_generation,
        "execution_protocol_version": context.execution_protocol_version,
        "runtime_env_plan_identity": context.runtime_env_plan_identity,
        "compiled_graph_submission_transport": (context.compiled_graph_submission_transport),
        "strict_execution_request": context.strict_execution_request,
    }


class _InvocationCounter:
    def __init__(self) -> None:
        self.count = 0

    def increment(self) -> None:
        self.count += 1

    def value(self) -> int:
        return self.count


def _counted_value(item: tuple[Any, int]) -> int:
    """Application target whose invocation is observed by a Ray actor."""
    import ray

    counter, value = item
    ray.get(counter.increment.remote())
    return value


def _strict_entrypoint_parallel_map(counter_name: str) -> list[int]:
    """Fan out from the outer entrypoint through the strict distributed boundary."""
    import ray

    counter = ray.get_actor(counter_name)
    return distributed.parallel_map(_counted_value, [(counter, 17)])


def _strict_runtime_env_identity() -> dict[str, Any]:
    return runtime_env_plan_identity(normalize_runtime_env({})).as_transport_dict()


def _strict_execution_context():
    return durable_task_execution(
        73,
        task_id="task-73",
        execution_protocol_version=1,
        attempt_number=2,
        execution_generation=5,
        runtime_env_plan_identity=_strict_runtime_env_identity(),
        strict_execution_request=True,
    )


def _strict_map_controls(
    pickled_func: bytes,
) -> tuple[Any, ...]:
    with _strict_execution_context():
        operation = distributed._strict_nested_operation(
            NestedExecutionBoundaryKind.DISTRIBUTED_MAP
        )
        assert operation is not None
        return distributed._nested_distributed_request(operation, pickled_func, 0)


class TestDistributedUtilities:
    """Tests for distributed computing helpers."""

    def test_is_ray_available_without_ray(self) -> None:
        """Test is_ray_available returns False when Ray not initialized."""
        from django_ray.runtime.distributed import is_ray_available

        # Ray might be initialized from other tests, but if not, should return False
        result = is_ray_available()
        assert isinstance(result, bool)

    def test_get_ray_resources_without_ray(self) -> None:
        """Test get_ray_resources returns empty dict when Ray not available."""
        from django_ray.runtime.distributed import get_ray_resources, is_ray_available

        if not is_ray_available():
            resources = get_ray_resources()
            assert resources == {}

    def test_parallel_map_fallback_sequential(self) -> None:
        """Test parallel_map falls back to sequential without Ray."""
        from django_ray.runtime.distributed import parallel_map

        def double(x: int) -> int:
            return x * 2

        items = [1, 2, 3, 4, 5]
        results = parallel_map(double, items)

        assert results == [2, 4, 6, 8, 10]

    def test_parallel_map_with_kwargs(self) -> None:
        """Test parallel_map passes kwargs correctly."""
        from django_ray.runtime.distributed import parallel_map

        def multiply(x: int, factor: int = 1) -> int:
            return x * factor

        items = [1, 2, 3]
        results = parallel_map(multiply, items, factor=10)

        assert results == [10, 20, 30]

    def test_parallel_map_empty_list(self) -> None:
        """Test parallel_map handles empty list."""
        from django_ray.runtime.distributed import parallel_map

        def identity(x: int) -> int:
            return x

        results = parallel_map(identity, [])
        assert results == []

    def test_parallel_starmap_fallback_sequential(self) -> None:
        """Test parallel_starmap falls back to sequential without Ray."""
        from django_ray.runtime.distributed import parallel_starmap

        def add(a: int, b: int) -> int:
            return a + b

        items = [(1, 2), (3, 4), (5, 6)]
        results = parallel_starmap(add, items)

        assert results == [3, 7, 11]

    def test_parallel_starmap_empty_list(self) -> None:
        """Test parallel_starmap handles empty list."""
        from django_ray.runtime.distributed import parallel_starmap

        def add(a: int, b: int) -> int:
            return a + b

        results = parallel_starmap(add, [])
        assert results == []

    def test_scatter_gather_fallback_sequential(self) -> None:
        """Test scatter_gather falls back to sequential without Ray."""
        from django_ray.runtime.distributed import scatter_gather

        def task_a() -> str:
            return "a"

        def task_b(x: int) -> int:
            return x * 2

        def task_c(msg: str) -> str:
            return msg.upper()

        tasks = [
            (task_a, (), {}),
            (task_b, (5,), {}),
            (task_c, (), {"msg": "hello"}),
        ]

        results = scatter_gather(tasks)
        assert results == ["a", 10, "HELLO"]

    def test_scatter_gather_empty_list(self) -> None:
        """Test scatter_gather handles empty list."""
        from django_ray.runtime.distributed import scatter_gather

        results = scatter_gather([])
        assert results == []

    def test_get_num_workers_without_ray(self) -> None:
        """Test get_num_workers returns 1 without Ray."""
        from django_ray.runtime.distributed import get_num_workers, is_ray_available

        if not is_ray_available():
            assert get_num_workers() == 1

    def test_get_total_cpus_without_ray(self) -> None:
        """Test get_total_cpus returns local CPU count without Ray."""
        import os

        from django_ray.runtime.distributed import get_total_cpus, is_ray_available

        if not is_ray_available():
            expected = float(os.cpu_count() or 1)
            assert get_total_cpus() == expected


@pytest.mark.real_ray
class TestDistributedWithRay:
    """Tests that require Ray to be running."""

    @pytest.fixture(autouse=True)
    def ray_cluster(self):
        """Initialize Ray for these tests."""
        import ray

        if not ray.is_initialized():
            ray.init(ignore_reinit_error=True)
        yield
        if ray.is_initialized():
            ray.shutdown()

    def test_parallel_map_with_ray(self) -> None:
        """Test parallel_map uses Ray when available."""
        from django_ray.runtime.distributed import is_ray_available, parallel_map

        assert is_ray_available(), "Ray should be initialized by fixture"

        # Use module-level function (can be pickled for Ray)
        items = list(range(10))
        results = parallel_map(_square, items)

        assert results == [x * x for x in items]

    def test_parallel_map_repeated_calls_reuse_cached_remote(self) -> None:
        """Repeated fan-outs should succeed without registering nested remotes."""
        from django_ray.runtime.distributed import parallel_map

        for offset in range(20):
            assert parallel_map(_square, [offset, offset + 1], max_concurrency=1) == [
                offset * offset,
                (offset + 1) * (offset + 1),
            ]

    def test_strict_parallel_map_round_trip_installs_exact_context(self) -> None:
        expected_runtime_env = _strict_runtime_env_identity()

        with _strict_execution_context():
            results = distributed.parallel_map(_strict_context_snapshot, [11])

        assert results == [
            {
                "value": 11,
                "task_pk": 73,
                "task_id": "task-73",
                "attempt_number": 2,
                "execution_generation": 5,
                "execution_protocol_version": 1,
                "runtime_env_plan_identity": expected_runtime_env,
                "compiled_graph_submission_transport": "direct-ray-core",
                "strict_execution_request": True,
            }
        ]

    @pytest.mark.parametrize(
        ("tamper", "classification"),
        [
            ("protocol", NestedExecutionRequestRejection.PROTOCOL_MISMATCH),
            ("callable", NestedExecutionRequestRejection.CALLABLE_MISMATCH),
        ],
    )
    def test_strict_rejection_survives_ray_without_invoking_callable(
        self,
        tamper: str,
        classification: NestedExecutionRequestRejection,
    ) -> None:
        import ray
        from ray.exceptions import RayTaskError

        counter = ray.remote(_InvocationCounter).remote()
        pickled_func = pickle.dumps(_counted_value)
        controls = _strict_map_controls(pickled_func)
        serialized = controls[0]
        if tamper == "protocol":
            value = json.loads(serialized)
            value["execution_protocol_version"] += 1
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            submitted_callable = pickled_func
        else:
            submitted_callable = pickle.dumps(_square)

        remote = distributed._get_cached_remote("map").options(num_cpus=1.0, num_gpus=0.0)
        # Ray necessarily deserializes these ordinary handler arguments first. The
        # actor proves that the separately bound application callable is not
        # invoked after the strict request is rejected; the mocked tests separately
        # guard ``pickle.loads`` with a zero-call sentinel.
        ref = remote.remote(
            submitted_callable,
            (counter, 17),
            {},
            serialized,
            *controls[1:],
        )
        with pytest.raises(RayTaskError) as caught:
            ray.get(ref)

        cause = caught.value.cause
        assert isinstance(cause, NestedExecutionRequestRejected)
        assert cause.classification is classification
        assert str(cause) == f"nested execution request rejected: {classification.value}"
        assert cause.retryable is False
        assert ray.get(counter.value.remote()) == 0

    def test_get_ray_resources_with_ray(self) -> None:
        """Test get_ray_resources returns actual resources."""
        from django_ray.runtime.distributed import get_ray_resources, is_ray_available

        assert is_ray_available(), "Ray should be initialized by fixture"

        resources = get_ray_resources()

        assert "CPU" in resources
        assert resources["CPU"] > 0


@pytest.mark.real_ray
def test_nested_rejection_reaches_outer_enriched_completion(
    ray_cluster: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ExecutionIdentity(
        task_execution_pk=73,
        task_id="task-73",
        attempt_number=2,
        execution_generation=5,
    )
    runtime_env_identity = _strict_runtime_env_identity()
    counter_name = "strict-distributed-entrypoint-counter"
    counter = ray_cluster.remote(_InvocationCounter).options(name=counter_name).remote()
    original_nested_request = distributed._nested_distributed_request
    monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)

    def tamper_nested_protocol(
        operation: Any,
        pickled_func: bytes,
        item_index: int,
    ) -> tuple[Any, ...]:
        serialized, *expected_controls = original_nested_request(
            operation,
            pickled_func,
            item_index,
        )
        request = json.loads(serialized)
        request["execution_protocol_version"] += 1
        return (
            json.dumps(
                request,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            *expected_controls,
        )

    monkeypatch.setattr(distributed, "_nested_distributed_request", tamper_nested_protocol)
    try:
        encoded = entrypoint.execute_task(
            "tests.unit.test_distributed._strict_entrypoint_parallel_map",
            json.dumps([counter_name]),
            "{}",
            task_execution_pk=identity.task_execution_pk,
            task_id=identity.task_id,
            attempt_number=identity.attempt_number,
            execution_generation=identity.execution_generation,
            runtime_env_plan_identity=runtime_env_identity,
            ray_job_driver=False,
            _completion_identity=identity,
            _execution_protocol_version=1,
            _strict_execution_request=True,
        )
        decoded = decode_execution_completion(
            encoded,
            expected_identity=identity,
            expected_execution_protocol_version=1,
        )
        completion = decoded.completion

        assert decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
        assert completion.success is False
        assert completion.result is None
        assert completion.result_reference is None
        assert completion.error == "nested execution request rejected: protocol_mismatch"
        assert completion.traceback is None
        assert completion.exception_type == (
            "django_ray.execution_codec.NestedExecutionRequestRejected"
        )
        assert completion.retryable is False
        assert ray_cluster.get(counter.value.remote()) == 0
    finally:
        ray_cluster.kill(counter, no_restart=True)
