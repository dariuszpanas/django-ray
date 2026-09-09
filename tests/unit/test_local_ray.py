"""Resource-free checks for required Linux Ray test allocation and evidence."""

from __future__ import annotations

import ast
import os
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

    monkeypatch.setattr(local_ray, "require_linux", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: state.initialized,
            init=init,
            shutdown=lambda: state.shutdowns.append(True),
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
    assert fake_ray.shutdowns == [True]


def test_non_linux_is_rejected_before_startup_or_cleanup(fake_ray, monkeypatch) -> None:
    def refuse() -> None:
        raise SystemExit("Linux required")

    monkeypatch.setattr(local_ray, "require_linux", refuse)
    with pytest.raises(SystemExit, match="Linux required"):
        local_ray.init_local_ray()
    assert fake_ray.calls == fake_ray.shutdowns == []


def test_test_sources_use_owned_allocation_or_explicit_live_cluster() -> None:
    direct_calls = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
