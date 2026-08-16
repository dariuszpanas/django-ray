"""Contracts for tests that own Ray or another external runtime."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import cast

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts import local_resource_coordinator as coordinator
from tests import conftest
from tests.integration import test_live_failure_injection, test_task_execution


@dataclass
class _CollectedItem:
    nodeid: str = "tests/example.py::test_external_runtime"
    fixturenames: list[str] = field(default_factory=list)
    markers: set[str] = field(default_factory=set)

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self.markers else None


@dataclass
class _ConfigOption:
    collectonly: bool = False


@dataclass
class _Config:
    rootpath: Path
    option: _ConfigOption = field(default_factory=_ConfigOption)
    stash: dict[object, object] = field(default_factory=dict)


@dataclass
class _Session:
    config: _Config
    items: list[_CollectedItem]
    testsfailed: int = 0


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


def test_live_submission_tracks_declared_testproject_runtime_packages(monkeypatch) -> None:
    from testproject import settings as testproject_settings

    working_dir_uri = "file:///runtime-env/django-ray-source.zip"
    django_ray_config = cast(dict[str, object], testproject_settings.DJANGO_RAY)
    project_profiles = cast(
        dict[str, dict[str, object]],
        django_ray_config["RUNTIME_ENV_PROFILES"],
    )
    declared_packages = cast(list[str], project_profiles["project"]["pip"])
    monkeypatch.setattr(
        test_live_failure_injection,
        "LIVE_WORKING_DIR_URI",
        working_dir_uri,
    )

    runtime_env = test_live_failure_injection._live_project_runtime_env_spec()

    assert runtime_env == {
        "working_dir": working_dir_uri,
        "pip": declared_packages,
        "env_vars": {
            "DATABASE_ENGINE": "django.db.backends.sqlite3",
            "DJANGO_SETTINGS_MODULE": "testproject.settings",
            "PYTHONPATH": "src",
        },
    }
    assert runtime_env["pip"] is not declared_packages
    runtime_packages = runtime_env["pip"]
    assert isinstance(runtime_packages, list)
    assert all(isinstance(requirement, str) for requirement in runtime_packages)
    parsed_requirements = [Requirement(requirement) for requirement in runtime_packages]
    unfold_requirements = [
        requirement
        for requirement in parsed_requirements
        if canonicalize_name(requirement.name) == canonicalize_name("django-unfold")
    ]
    assert unfold_requirements == [
        Requirement(f"django-unfold=={distribution_version('django-unfold')}")
    ]


def test_external_resource_guard_is_tryfirst() -> None:
    hook_options = conftest.pytest_collection_modifyitems.pytest_impl

    assert hook_options["tryfirst"] is True


def test_real_ray_ownership_hook_runs_after_final_deselection() -> None:
    hook_options = conftest.pytest_collection_finish.pytest_impl

    assert hook_options["trylast"] is True


@pytest.mark.parametrize(
    ("collectonly", "testsfailed", "markers"),
    [
        (True, 0, {"real_ray"}),
        (False, 1, {"real_ray"}),
        (False, 0, set()),
    ],
)
def test_real_ray_ownership_ignores_non_executing_final_selections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    collectonly: bool,
    testsfailed: int,
    markers: set[str],
) -> None:
    def unexpected_acquisition(**_kwargs: object) -> None:
        raise AssertionError("ownership must remain inert")

    monkeypatch.setattr(conftest, "acquire_local_resources", unexpected_acquisition)
    session = _Session(
        config=_Config(
            rootpath=tmp_path,
            option=_ConfigOption(collectonly=collectonly),
        ),
        items=[_CollectedItem(markers=markers)],
        testsfailed=testsfailed,
    )

    conftest.pytest_collection_finish(session)  # type: ignore[arg-type]


@pytest.mark.parametrize("first_release_hook", ["sessionfinish", "unconfigure"])
def test_real_ray_ownership_uses_final_items_and_releases_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    first_release_hook: str,
) -> None:
    acquired: list[dict[str, object]] = []
    release_calls: list[tuple[str, str | None]] = []

    class _FakeOwnership:
        def release(
            self,
            *,
            outcome: str = "completed",
            postcondition: str | None = None,
        ) -> None:
            release_calls.append((outcome, postcondition))

    ownership = _FakeOwnership()

    def acquire(**kwargs: object) -> _FakeOwnership:
        acquired.append(dict(kwargs))
        on_acquired = cast(Callable[[object], None], kwargs["on_acquired"])
        on_acquired(ownership)
        return ownership

    monkeypatch.setattr(conftest, "acquire_local_resources", acquire)
    for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
        monkeypatch.setenv(key, f"private-{key}")
    monkeypatch.setenv(coordinator.LOCAL_RESOURCE_AGENT_ENV, "pytest-agent")
    config = _Config(rootpath=tmp_path)
    session = _Session(
        config=config,
        items=[
            _CollectedItem(markers={"real_ray"}),
            _CollectedItem(markers={"real_ray", "compiled_graph_opt_in"}),
            _CollectedItem(),
        ],
    )

    conftest.pytest_collection_finish(session)  # type: ignore[arg-type]

    assert len(acquired) == 1
    assert set(acquired[0]) == {
        "on_acquired",
        "phase",
        "profile",
        "rootpath",
        "selected_count",
    }
    assert acquired[0]["rootpath"] == tmp_path.resolve()
    assert acquired[0]["selected_count"] == 2
    assert acquired[0]["profile"] == "real-ray"
    assert acquired[0]["phase"] == "pytest"
    assert all(key not in os.environ for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS)
    assert os.environ[coordinator.LOCAL_RESOURCE_AGENT_ENV] == "pytest-agent"

    if first_release_hook == "sessionfinish":
        conftest.pytest_sessionfinish(session, 0)  # type: ignore[arg-type]
        conftest.pytest_unconfigure(config)  # type: ignore[arg-type]
    else:
        conftest.pytest_unconfigure(config)  # type: ignore[arg-type]
        conftest.pytest_sessionfinish(session, 0)  # type: ignore[arg-type]

    expected_outcome = "passed" if first_release_hook == "sessionfinish" else "interrupted"
    assert release_calls == [
        (
            expected_outcome,
            (
                "pytest session finished; Ray cleanup checks remain authoritative"
                if first_release_hook == "sessionfinish"
                else "pytest unconfigured; Ray cleanup checks remain authoritative"
            ),
        )
    ]


def test_real_ray_ownership_is_retained_before_acquisition_returns_and_scrub_errors_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retained_before_return: list[bool] = []
    release_calls: list[tuple[str, str | None]] = []

    class _FakeOwnership:
        def release(
            self,
            *,
            outcome: str = "completed",
            postcondition: str | None = None,
        ) -> None:
            release_calls.append((outcome, postcondition))

    ownership = _FakeOwnership()
    config = _Config(rootpath=tmp_path)

    def acquire(**kwargs: object) -> _FakeOwnership:
        on_acquired = cast(Callable[[object], None], kwargs["on_acquired"])
        on_acquired(ownership)
        retained_before_return.append(
            config.stash.get(conftest._REAL_RAY_OWNERSHIP_KEY) is ownership
        )
        return ownership

    class _InterruptDuringScrub:
        def __iter__(self) -> Iterator[str]:
            raise KeyboardInterrupt

    monkeypatch.setattr(conftest, "acquire_local_resources", acquire)
    monkeypatch.setattr(
        conftest,
        "LOCAL_RESOURCE_INHERITANCE_ENV_KEYS",
        _InterruptDuringScrub(),
    )
    session = _Session(
        config=config,
        items=[_CollectedItem(markers={"real_ray"})],
    )

    with pytest.raises(KeyboardInterrupt):
        conftest.pytest_collection_finish(session)  # type: ignore[arg-type]

    assert retained_before_return == [True]
    assert release_calls == [("failed", "pytest ownership initialization failed")]
    assert conftest._REAL_RAY_OWNERSHIP_KEY not in config.stash


def test_real_ray_ownership_callback_failure_is_bounded_and_released(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_calls: list[tuple[str, str | None]] = []

    class _FakeOwnership:
        released = False

        def release(
            self,
            *,
            outcome: str = "completed",
            postcondition: str | None = None,
        ) -> None:
            if self.released:
                return
            self.released = True
            release_calls.append((outcome, postcondition))

    class _CommitThenFailStash(dict[object, object]):
        def __setitem__(self, key: object, value: object) -> None:
            super().__setitem__(key, value)
            raise RuntimeError("synthetic private callback detail")

    ownership = _FakeOwnership()
    config = _Config(rootpath=tmp_path, stash=_CommitThenFailStash())

    def acquire(**kwargs: object) -> _FakeOwnership:
        on_acquired = cast(Callable[[object], None], kwargs["on_acquired"])
        try:
            on_acquired(ownership)
        except BaseException:
            ownership.release(
                outcome="failed",
                postcondition="public acquisition callback failed",
            )
            raise
        return ownership

    monkeypatch.setattr(conftest, "acquire_local_resources", acquire)
    session = _Session(
        config=config,
        items=[_CollectedItem(markers={"real_ray"})],
    )

    with pytest.raises(
        pytest.UsageError,
        match="pytest could not retain acquired local resource ownership",
    ):
        conftest.pytest_collection_finish(session)  # type: ignore[arg-type]

    assert release_calls == [("failed", "public acquisition callback failed")]
    assert conftest._REAL_RAY_OWNERSHIP_KEY not in config.stash


def test_real_ray_acquisition_cleanup_error_is_bounded_and_unconfigure_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_calls: list[tuple[str, str | None]] = []

    class _FailOnceOwnership:
        def release(
            self,
            *,
            outcome: str = "completed",
            postcondition: str | None = None,
        ) -> None:
            release_calls.append((outcome, postcondition))
            if len(release_calls) == 1:
                raise coordinator.LocalResourceStateError(
                    "synthetic bounded initialization cleanup failure"
                )

    ownership = _FailOnceOwnership()
    config = _Config(rootpath=tmp_path)

    def acquire(**kwargs: object) -> _FailOnceOwnership:
        on_acquired = cast(Callable[[object], None], kwargs["on_acquired"])
        on_acquired(ownership)
        raise coordinator.LocalResourceStateError("synthetic bounded post-acquisition failure")

    monkeypatch.setattr(conftest, "acquire_local_resources", acquire)
    session = _Session(
        config=config,
        items=[_CollectedItem(markers={"real_ray"})],
    )

    with pytest.raises(
        pytest.UsageError,
        match="synthetic bounded initialization cleanup failure",
    ):
        conftest.pytest_collection_finish(session)  # type: ignore[arg-type]

    assert config.stash[conftest._REAL_RAY_OWNERSHIP_KEY] is ownership

    conftest.pytest_unconfigure(config)  # type: ignore[arg-type]

    assert release_calls == [
        ("failed", "pytest ownership initialization failed"),
        (
            "interrupted",
            "pytest unconfigured; Ray cleanup checks remain authoritative",
        ),
    ]
    assert conftest._REAL_RAY_OWNERSHIP_KEY not in config.stash


def test_real_ray_ownership_release_failure_remains_stashed_for_unconfigure_retry(
    tmp_path: Path,
) -> None:
    release_calls: list[tuple[str, str | None]] = []

    class _FailOnceOwnership:
        def release(
            self,
            *,
            outcome: str = "completed",
            postcondition: str | None = None,
        ) -> None:
            release_calls.append((outcome, postcondition))
            if len(release_calls) == 1:
                raise OSError("synthetic first release failure")

    config = _Config(rootpath=tmp_path)
    session = _Session(config=config, items=[])
    ownership = _FailOnceOwnership()
    config.stash[conftest._REAL_RAY_OWNERSHIP_KEY] = ownership

    with pytest.raises(OSError, match="synthetic first release failure"):
        conftest.pytest_sessionfinish(session, 0)  # type: ignore[arg-type]

    assert config.stash[conftest._REAL_RAY_OWNERSHIP_KEY] is ownership

    conftest.pytest_unconfigure(config)  # type: ignore[arg-type]

    assert release_calls == [
        (
            "passed",
            "pytest session finished; Ray cleanup checks remain authoritative",
        ),
        (
            "interrupted",
            "pytest unconfigured; Ray cleanup checks remain authoritative",
        ),
    ]
    assert conftest._REAL_RAY_OWNERSHIP_KEY not in config.stash


def test_real_ray_ownership_contention_becomes_bounded_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def contend(**_kwargs: object) -> None:
        raise coordinator.LocalResourceBusyError(
            'local resources are active; owner metadata: {"pid": 4242}'
        )

    monkeypatch.setattr(conftest, "acquire_local_resources", contend)
    session = _Session(
        config=_Config(rootpath=tmp_path),
        items=[_CollectedItem(markers={"real_ray"})],
    )

    with pytest.raises(pytest.UsageError, match=r"owner metadata: .*4242"):
        conftest.pytest_collection_finish(session)  # type: ignore[arg-type]


def test_unsafe_real_ray_ownership_path_becomes_bounded_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def reject_path(**_kwargs: object) -> None:
        raise coordinator.RealRayOwnershipPathError(tmp_path / "owner.lock")

    monkeypatch.setattr(conftest, "acquire_local_resources", reject_path)
    session = _Session(
        config=_Config(rootpath=tmp_path),
        items=[_CollectedItem(markers={"real_ray"})],
    )

    with pytest.raises(pytest.UsageError, match="refusing unsafe lock path"):
        conftest.pytest_collection_finish(session)  # type: ignore[arg-type]


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
