"""Resource-free checks for required Linux Ray test allocation and evidence."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import local_ray

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_ray(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(initialized=False, calls=[], shutdowns=[], init_error=None)

    def init(**options: Any) -> None:
        state.calls.append(options)
        if state.init_error:
            raise state.init_error
        state.initialized = True

    def shutdown(**options: Any) -> None:
        state.shutdowns.append(options)
        state.initialized = False

    monkeypatch.setattr(local_ray, "require_linux", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: state.initialized,
            init=init,
            shutdown=shutdown,
        ),
    )
    return state


def test_default_allocation_ignores_host_discovery_and_gpu_capacity(fake_ray, monkeypatch) -> None:
    monkeypatch.setenv("RAY_ADDRESS", "ray://unrelated-cluster:10001")
    local_ray.init_local_ray()

    assert fake_ray.calls == [
        {
            "address": "local",
            "num_cpus": 2,
            "num_gpus": 0,
            "object_store_memory": 128 * 1024 * 1024,
            "include_dashboard": False,
            "resources": None,
        }
    ]
    assert fake_ray.shutdowns == []


def test_specialized_actor_and_jobs_requirements_keep_fixed_allocation(fake_ray) -> None:
    local_ray.init_local_ray(num_cpus=4, include_dashboard=True, resources={"result_buffer": 1})

    options = fake_ray.calls[0]
    assert options["num_cpus"] == 4
    assert options["num_gpus"] == 0
    assert options["object_store_memory"] == 128 * 1024 * 1024
    assert options["resources"] == {"result_buffer": 1}
    assert options["include_dashboard"] is True


@pytest.mark.parametrize("num_cpus", [0, -1, 5, True, 1.5, "2", None])
def test_invalid_cpu_allocation_fails_before_ray(fake_ray, num_cpus) -> None:
    with pytest.raises(ValueError, match="1..4 logical CPUs"):
        local_ray.init_local_ray(num_cpus=num_cpus)
    assert fake_ray.calls == fake_ray.shutdowns == []


def test_existing_runtime_is_neither_reused_nor_stopped(fake_ray) -> None:
    fake_ray.initialized = True
    with pytest.raises(RuntimeError, match="found an initialized runtime"):
        local_ray.init_local_ray()
    assert fake_ray.calls == fake_ray.shutdowns == []


def test_partial_startup_failure_cleans_owned_runtime_and_preserves_error(fake_ray) -> None:
    fake_ray.init_error = OSError("startup failed")
    with pytest.raises(OSError) as raised:
        local_ray.init_local_ray()
    assert raised.value is fake_ray.init_error
    assert fake_ray.shutdowns == [{"wait_for_processes": True}]


@pytest.mark.parametrize("test_failed", [False, True])
def test_shared_fixture_waits_for_owned_processes_after_test(fake_ray, test_failed) -> None:
    from tests.conftest import ray_cluster

    fixture = ray_cluster.__wrapped__()
    assert next(fixture) is sys.modules["ray"]
    assert fake_ray.shutdowns == []

    if test_failed:
        failure = RuntimeError("test failed")
        with pytest.raises(RuntimeError) as raised:
            fixture.throw(failure)
        assert raised.value is failure
    else:
        with pytest.raises(StopIteration):
            next(fixture)

    assert fake_ray.shutdowns == [{"wait_for_processes": True}]


def test_shared_local_ray_session_reuses_single_runtime_until_shutdown(
    fake_ray, monkeypatch
) -> None:
    monkeypatch.setenv(local_ray.SHARED_LOCAL_RAY_ENV, "1")
    monkeypatch.setattr(local_ray, "_shared_local_ray_active", False)

    try:
        with local_ray.local_ray_runtime(num_cpus=2) as first_ray:
            assert first_ray is sys.modules["ray"]
            assert len(fake_ray.calls) == 1
            assert fake_ray.calls[0]["num_cpus"] == local_ray.MAX_LOGICAL_CPUS
            assert fake_ray.calls[0]["include_dashboard"] is False
            assert fake_ray.calls[0]["resources"] == {"result_buffer": 1, "result_fold": 1}

        assert fake_ray.shutdowns == []

        with local_ray.local_ray_runtime(num_cpus=4) as second_ray:
            assert second_ray is sys.modules["ray"]
            assert len(fake_ray.calls) == 1

        assert fake_ray.shutdowns == []
    finally:
        local_ray.shutdown_shared_local_ray()

    assert fake_ray.shutdowns == [{"wait_for_processes": True}]
    assert local_ray._shared_local_ray_active is False


@pytest.mark.parametrize("explicit_isolated", [True, False])
def test_isolated_local_ray_runtime_tears_down_shared_runtime_and_cleans_up(
    fake_ray, monkeypatch, explicit_isolated: bool
) -> None:
    monkeypatch.setenv(local_ray.SHARED_LOCAL_RAY_ENV, "1")
    monkeypatch.setattr(local_ray, "_shared_local_ray_active", False)

    try:
        with local_ray.local_ray_runtime(num_cpus=2):
            assert len(fake_ray.calls) == 1
            assert fake_ray.calls[0]["include_dashboard"] is False

        with local_ray.local_ray_runtime(include_dashboard=True, isolated=explicit_isolated):
            assert len(fake_ray.shutdowns) == 1
            assert len(fake_ray.calls) == 2
            assert fake_ray.calls[1]["include_dashboard"] is True
            assert local_ray._shared_local_ray_active is False

        assert len(fake_ray.shutdowns) == 2
        assert fake_ray.initialized is False

        with local_ray.local_ray_runtime(num_cpus=2):
            assert len(fake_ray.calls) == 3
            assert fake_ray.calls[2]["include_dashboard"] is False
            assert local_ray._shared_local_ray_active is True
    finally:
        local_ray.shutdown_shared_local_ray()

    assert len(fake_ray.shutdowns) == 3
    assert local_ray._shared_local_ray_active is False


def test_non_linux_is_rejected_before_startup_or_cleanup(fake_ray, monkeypatch) -> None:
    def refuse() -> None:
        raise SystemExit("Linux required")

    monkeypatch.setattr(local_ray, "require_linux", refuse)
    with pytest.raises(SystemExit, match="Linux required"):
        local_ray.init_local_ray()
    assert fake_ray.calls == fake_ray.shutdowns == []


def test_test_sources_use_owned_allocation_or_explicit_live_cluster() -> None:
    direct_calls = []
    ray_init_re = re.compile(r"\bray\s*\.\s*init\b")
    for path in sorted((ROOT / "tests").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if ray_init_re.search(source) is None:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ray"
                and node.func.attr == "init"
            ):
                direct_calls.append(path.relative_to(ROOT).as_posix())

    assert sorted(direct_calls) == [
        "tests/integration/test_live_failure_injection.py",
        "tests/local_ray.py",
    ]


@pytest.mark.parametrize(
    ("markers", "body", "expected_code", "expected_summary"),
    [
        ("@pytest.mark.real_ray", "pytest.skip('runtime unavailable')", 1, "1 failed"),
        ("@pytest.mark.real_ray\n@pytest.mark.skip(reason='unavailable')", "pass", 1, "1 error"),
        ("@pytest.mark.real_ray", "pytest.xfail('runtime broken')", 1, "1 failed"),
        (
            "@pytest.mark.real_ray\n@pytest.mark.compiled_graph_opt_in",
            "pytest.skip('capability is opt in')",
            0,
            "1 skipped",
        ),
        ("", "pytest.skip('unrelated optional case')", 0, "1 skipped"),
        ("@pytest.mark.real_ray", "assert True", 0, "1 passed"),
    ],
)
def test_required_ray_skip_policy_in_real_pytest_without_starting_ray(
    tmp_path: Path, markers: str, body: str, expected_code: int, expected_summary: str
) -> None:
    # Import just the reporting hook in a disposable suite, without the ownership
    # hook or Ray fixtures. This verifies wrapper order, including pytest xfail.
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import pytest_runtest_makereport\n", encoding="utf-8"
    )
    (tmp_path / "test_probe.py").write_text(
        f"import pytest\n{markers}\ndef test_probe():\n    {body}\n", encoding="utf-8"
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "PYTEST_PLUGINS": "",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--confcutdir", str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected_code, result.stdout + result.stderr
    assert expected_summary in result.stdout, result.stdout + result.stderr
    if expected_code:
        assert "skip/xfail is forbidden" in result.stdout


def test_shared_local_ray_ordering_places_isolated_dashboard_modules_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tests import conftest

    plugin = conftest._SharedLocalRayOrderingPlugin()
    config = SimpleNamespace(rootpath=ROOT)

    def make_item(rel_path: str, *, real_ray: bool = True) -> Any:
        return SimpleNamespace(
            path=ROOT / rel_path,
            nodeid=f"{rel_path}::test_case",
            get_closest_marker=lambda name: object() if real_ray and name == "real_ray" else None,
        )

    original = [
        make_item("tests/unit/test_workflows_real_ray.py"),
        make_item("tests/integration/test_task_execution.py"),
        make_item("tests/unit/test_result_buffer.py"),
        make_item("tests/unit/test_distributed.py"),
    ]

    monkeypatch.delenv(local_ray.SHARED_LOCAL_RAY_ENV, raising=False)
    items_without_env = list(original)
    plugin.pytest_collection_modifyitems(config, items_without_env)  # type: ignore[arg-type]
    assert [item.nodeid for item in items_without_env] == [item.nodeid for item in original]

    monkeypatch.setenv(local_ray.SHARED_LOCAL_RAY_ENV, "1")
    items_with_env = list(original)
    plugin.pytest_collection_modifyitems(config, items_with_env)  # type: ignore[arg-type]
    assert [item.nodeid for item in items_with_env] == [
        "tests/integration/test_task_execution.py::test_case",
        "tests/unit/test_result_buffer.py::test_case",
        "tests/unit/test_workflows_real_ray.py::test_case",
        "tests/unit/test_distributed.py::test_case",
    ]
