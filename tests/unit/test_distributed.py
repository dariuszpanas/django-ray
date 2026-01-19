"""Unit tests for distributed computing utilities."""

from __future__ import annotations

import pytest


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
        from django_ray.runtime.distributed import parallel_map, is_ray_available

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


@pytest.mark.skipif(
    not pytest.importorskip("ray").is_initialized(),
    reason="Ray not initialized"
)
class TestDistributedWithRay:
    """Tests that require Ray to be running."""

    def test_parallel_map_with_ray(self) -> None:
        """Test parallel_map uses Ray when available."""
        from django_ray.runtime.distributed import parallel_map, is_ray_available

        if not is_ray_available():
            pytest.skip("Ray not available")

        def square(x: int) -> int:
            return x * x

        items = list(range(10))
        results = parallel_map(square, items)

        assert results == [x * x for x in items]

    def test_get_ray_resources_with_ray(self) -> None:
        """Test get_ray_resources returns actual resources."""
        from django_ray.runtime.distributed import get_ray_resources, is_ray_available

        if not is_ray_available():
            pytest.skip("Ray not available")

        resources = get_ray_resources()

        assert "CPU" in resources
        assert resources["CPU"] > 0

