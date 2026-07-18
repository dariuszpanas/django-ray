"""Mocked unit tests for distributed runtime utilities."""

from __future__ import annotations

import builtins
import sys
from typing import Any

import django

from django_ray.runtime import distributed


def _add(a: int, b: int) -> int:
    return a + b


def _mul(value: int, factor: int = 1) -> int:
    return value * factor


class _DistRef:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeRay:
    def __init__(self) -> None:
        self.remote_options: list[dict[str, Any]] = []
        self.resources: dict[str, float] = {"CPU": 8.0}

    def remote(self, *args: Any, **options: Any):
        self.remote_options.append(options)

        def _decorator(fn):
            class _RemoteCallable:
                def options(self, **options: Any):
                    fake_options = _RemoteCallable()
                    fake_options._options = options
                    return fake_options

                @staticmethod
                def remote(*args: Any, **kwargs: Any) -> _DistRef:
                    return _DistRef(fn(*args, **kwargs))

            return _RemoteCallable()

        return _decorator(args[0]) if args else _decorator

    def get(self, refs: Any) -> Any:
        if isinstance(refs, list):
            return [ref.value for ref in refs]
        return refs.value

    def wait(self, refs: list[_DistRef], num_returns: int = 1):
        return refs[:num_returns], refs[num_returns:]

    def cluster_resources(self) -> dict[str, float]:
        return self.resources


def _install_fake_ray(monkeypatch) -> _FakeRay:
    fake = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake)
    return fake


class TestDistributedMocked:
    """Coverage for distributed runtime branches with mocked Ray."""

    def test_bootstrap_calls_django_setup_when_needed(self, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(distributed, "_django_bootstrapped", False)
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
        monkeypatch.setattr("django.apps.apps.ready", False, raising=False)
        monkeypatch.setattr(django, "setup", lambda: calls.append("setup"))

        distributed._bootstrap_django_if_needed()

        assert calls == ["setup"]
        assert distributed._django_bootstrapped is True

    def test_bootstrap_marks_done_even_without_settings_module(self, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(distributed, "_django_bootstrapped", False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.setattr("django.apps.apps.ready", False, raising=False)
        monkeypatch.setattr(django, "setup", lambda: calls.append("setup"))

        distributed._bootstrap_django_if_needed()

        assert calls == []
        assert distributed._django_bootstrapped is True

    def test_bootstrap_returns_when_already_complete(self, monkeypatch) -> None:
        monkeypatch.setattr(distributed, "_django_bootstrapped", True)

        distributed._bootstrap_django_if_needed()

        assert distributed._django_bootstrapped is True

    def test_is_ray_available_handles_import_error(self, monkeypatch) -> None:
        original_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):  # noqa: ANN001
            if name == "ray":
                raise ImportError("ray missing")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        monkeypatch.delitem(sys.modules, "ray", raising=False)

        assert distributed.is_ray_available() is False

    def test_parallel_map_uses_ray_batch_mode(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        bootstrap_calls: list[str] = []
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(
            distributed, "_bootstrap_django_if_needed", lambda: bootstrap_calls.append("boot")
        )

        results = distributed.parallel_map(_mul, [1, 2, 3], factor=10, max_concurrency=2)

        assert results == [10, 20, 30]
        assert len(bootstrap_calls) == 3

    def test_parallel_map_uses_single_ray_batch_when_unbounded(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)

        assert distributed.parallel_map(_mul, [1, 2], factor=3) == [3, 6]
        assert distributed.parallel_map(_mul, [3, 4], factor=3) == [9, 12]
        assert len(fake.remote_options) == 1

    def test_parallel_starmap_uses_ray_batch_mode(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        bootstrap_calls: list[str] = []
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(
            distributed, "_bootstrap_django_if_needed", lambda: bootstrap_calls.append("boot")
        )

        results = distributed.parallel_starmap(_add, [(1, 2), (3, 4), (5, 6)], max_concurrency=2)

        assert results == [3, 7, 11]
        assert len(bootstrap_calls) == 3

    def test_parallel_starmap_submits_all_items_without_limit(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)

        assert distributed.parallel_starmap(_add, [(1, 2), (3, 4)]) == [3, 7]

    def test_scatter_gather_uses_ray_mode(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        bootstrap_calls: list[str] = []
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(
            distributed, "_bootstrap_django_if_needed", lambda: bootstrap_calls.append("boot")
        )

        tasks = [
            (_add, (1, 2), {}),
            (_mul, (3,), {"factor": 4}),
        ]
        results = distributed.scatter_gather(tasks)

        assert results == [3, 12]
        assert len(bootstrap_calls) == 2

    def test_get_num_workers_and_total_cpus_with_ray_resources(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.resources = {
            "CPU": 16.0,
            "node:10.0.0.1": 1.0,
            "node:10.0.0.2": 1.0,
            "node:__internal_head__": 1.0,
        }
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)

        assert distributed.get_num_workers() == 2
        assert distributed.get_total_cpus() == 16.0

    def test_get_total_cpus_defaults_when_cpu_not_reported(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.resources = {"node:10.0.0.1": 1.0}
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)

        assert distributed.get_total_cpus() == 1.0

    def test_helpers_reject_invalid_limits_and_resources(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="max_concurrency"):
            distributed.parallel_map(_mul, [1], max_concurrency=0)
        with pytest.raises(ValueError, match="num_cpus"):
            distributed.parallel_map(_mul, [1], num_cpus=-1)
        with pytest.raises(ValueError, match="num_gpus"):
            distributed.parallel_starmap(_add, [(1, 2)], num_gpus=-1)

    def test_helpers_reject_unsupported_shapes(self) -> None:
        import pytest

        with pytest.raises(TypeError, match="items must"):
            distributed.parallel_map(_mul, 1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"items\[0\]"):
            distributed.parallel_starmap(_add, [[1, 2]])  # type: ignore[list-item]
        with pytest.raises(TypeError, match=r"tasks\[0\]"):
            distributed.scatter_gather([(_add, (1, 2))])  # type: ignore[list-item]
