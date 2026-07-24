"""Contracts for tests that own Ray or another external runtime."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

import pytest

from tests import conftest
from tests.integration import test_live_failure_injection, test_task_execution


@dataclass
class _CollectedItem:
    nodeid: str = "tests/example.py::test_external_runtime"
    fixturenames: list[str] = field(default_factory=list)
    markers: set[str] = field(default_factory=set)

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self.markers else None

    def add_marker(self, marker: object) -> None:
        pass


@dataclass
class _FakeRay:
    initialized: bool = False
    init_error: Exception | None = None
    node_inventory: list[dict[str, object]] = field(default_factory=list)
    init_calls: list[dict[str, object]] = field(default_factory=list)
    shutdown_calls: int = 0

    def is_initialized(self) -> bool:
        return self.initialized

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.initialized = False

    def init(self, **kwargs: object) -> None:
        self.init_calls.append(kwargs)
        if self.init_error is not None:
            raise self.init_error
        self.initialized = True

    def nodes(self) -> list[dict[str, object]]:
        return self.node_inventory


def test_required_local_ray_startup_error_fails_the_fixture(monkeypatch) -> None:
    startup_error = OSError("dashboard port is unavailable")
    shutdown_calls: list[bool] = []
    monkeypatch.setattr(test_task_execution.ray, "is_initialized", lambda: False)
    monkeypatch.setattr(
        test_task_execution.ray,
        "shutdown",
        lambda: shutdown_calls.append(True),
    )

    def fail_startup(**_kwargs: object) -> None:
        raise startup_error

    monkeypatch.setattr(test_task_execution.ray, "init", fail_startup)
    fixture = test_task_execution.ray_cluster.__wrapped__()

    with pytest.raises(RuntimeError, match="Required local Ray startup failed") as raised:
        next(fixture)

    assert raised.value.__cause__ is startup_error
    assert shutdown_calls == [True]


def test_required_local_ray_uses_explicit_local_runtime_and_tears_down(
    monkeypatch,
) -> None:
    init_calls: list[dict[str, object]] = []
    shutdown_calls: list[bool] = []
    monkeypatch.setattr(test_task_execution.ray, "is_initialized", lambda: False)
    monkeypatch.setattr(
        test_task_execution.ray,
        "init",
        lambda **kwargs: init_calls.append(kwargs),
    )
    monkeypatch.setattr(
        test_task_execution.ray,
        "shutdown",
        lambda: shutdown_calls.append(True),
    )
    fixture = test_task_execution.ray_cluster.__wrapped__()

    assert next(fixture) is None
    with pytest.raises(StopIteration):
        next(fixture)

    assert init_calls == [
        {
            "address": "local",
            "include_dashboard": True,
            "dashboard_port": 8265,
        }
    ]
    assert shutdown_calls == [True]


def test_required_local_ray_fixture_rejects_preexisting_runtime(monkeypatch) -> None:
    startup_calls: list[bool] = []
    shutdown_calls: list[bool] = []
    monkeypatch.setattr(test_task_execution.ray, "is_initialized", lambda: True)
    monkeypatch.setattr(
        test_task_execution.ray,
        "init",
        lambda **_kwargs: startup_calls.append(True),
    )
    monkeypatch.setattr(
        test_task_execution.ray,
        "shutdown",
        lambda: shutdown_calls.append(True),
    )
    fixture = test_task_execution.ray_cluster.__wrapped__()

    with pytest.raises(RuntimeError, match="found an initialized runtime"):
        next(fixture)

    assert startup_calls == []
    assert shutdown_calls == []


def test_django_settings_env_restores_existing_settings_and_exact_sys_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_paths = {
        str(test_task_execution.PROJECT_ROOT),
        str(test_task_execution.PROJECT_ROOT / "src"),
    }
    pre_fixture_path = [entry for entry in sys.path if entry not in project_paths]
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "existing_project.settings")
    monkeypatch.setattr(sys, "path", pre_fixture_path.copy())
    assert project_paths.isdisjoint(sys.path)

    with pytest.MonkeyPatch.context() as fixture_monkeypatch:
        test_task_execution.django_settings_env.__wrapped__(fixture_monkeypatch)

        assert os.environ["DJANGO_SETTINGS_MODULE"] == "testproject.settings"
        assert project_paths.issubset(sys.path)

    assert os.environ["DJANGO_SETTINGS_MODULE"] == "existing_project.settings"
    assert sys.path == pre_fixture_path


def test_enabled_live_cluster_connection_failure_is_not_skipped(monkeypatch) -> None:
    connection_error = OSError("connection refused")
    fake_ray = _FakeRay(init_error=connection_error)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr(test_live_failure_injection, "LIVE_RAY_ADDRESS", "ray://required:10001")
    fixture = test_live_failure_injection.live_ray_cluster.__wrapped__()

    with pytest.raises(RuntimeError, match="Required live Ray connection failed") as raised:
        next(fixture)

    assert raised.value.__cause__ is connection_error
    assert fake_ray.init_calls == [{"address": "ray://required:10001"}]
    assert fake_ray.shutdown_calls == 1


def test_enabled_live_cluster_rejects_preexisting_driver_without_cleanup(monkeypatch) -> None:
    fake_ray = _FakeRay(initialized=True)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    fixture = test_live_failure_injection.live_ray_cluster.__wrapped__()

    with pytest.raises(RuntimeError, match="found an initialized driver"):
        next(fixture)

    assert fake_ray.initialized is True
    assert fake_ray.init_calls == []
    assert fake_ray.shutdown_calls == 0


def test_enabled_live_cluster_insufficient_nodes_fails_and_cleans_up(monkeypatch) -> None:
    fake_ray = _FakeRay(node_inventory=[{"Alive": True}])
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr(test_live_failure_injection, "LIVE_MIN_NODES", 2)
    fixture = test_live_failure_injection.live_ray_cluster.__wrapped__()

    with pytest.raises(RuntimeError, match="1 alive node.*requires at least 2"):
        next(fixture)

    assert fake_ray.shutdown_calls == 1


def test_enabled_live_cluster_success_tears_down_owned_connection(monkeypatch) -> None:
    fake_ray = _FakeRay(node_inventory=[{"Alive": True}, {"Alive": True}])
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr(test_live_failure_injection, "LIVE_MIN_NODES", 2)
    fixture = test_live_failure_injection.live_ray_cluster.__wrapped__()

    assert next(fixture) is fake_ray
    with pytest.raises(StopIteration):
        next(fixture)

    assert fake_ray.shutdown_calls == 1


def test_external_resource_guard_is_tryfirst() -> None:
    hook_options = conftest.pytest_collection_modifyitems.pytest_impl

    assert hook_options["tryfirst"] is True


def test_compiled_graph_opt_in_requires_real_ray_marker() -> None:
    item = _CollectedItem(markers={"compiled_graph_opt_in"})

    with pytest.raises(pytest.UsageError, match="not marked 'real_ray'"):
        conftest.pytest_collection_modifyitems([item])  # type: ignore[list-item]


def test_compiled_graph_opt_in_accepts_real_ray_marker() -> None:
    item = _CollectedItem(markers={"compiled_graph_opt_in", "real_ray"})

    conftest.pytest_collection_modifyitems([item])  # type: ignore[list-item]


@pytest.mark.parametrize(
    ("fixture_name", "marker_name"),
    sorted(conftest.EXTERNAL_RESOURCE_FIXTURE_MARKERS.items()),
)
def test_external_resource_fixture_requires_matching_marker(
    fixture_name: str,
    marker_name: str,
) -> None:
    item = _CollectedItem(fixturenames=[fixture_name])

    with pytest.raises(pytest.UsageError, match=rf"not marked '{marker_name}'"):
        conftest.pytest_collection_modifyitems([item])  # type: ignore[list-item]


@pytest.mark.parametrize(
    ("fixture_name", "marker_name"),
    sorted(conftest.EXTERNAL_RESOURCE_FIXTURE_MARKERS.items()),
)
def test_external_resource_fixture_accepts_matching_marker(
    fixture_name: str,
    marker_name: str,
) -> None:
    item = _CollectedItem(fixturenames=[fixture_name], markers={marker_name})

    conftest.pytest_collection_modifyitems([item])  # type: ignore[list-item]
