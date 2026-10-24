"""Resource-free command target selection through real runner construction."""

from __future__ import annotations

import os
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.management import CommandError

from django_ray.management.commands import django_ray_worker as worker
from django_ray.runner.ray_core import RayCoreRunner

REQUESTED = "ray://requested.invalid:10001"
AMBIENT = "ray://ambient.invalid:10001"


class FakeRay:
    """Model a cleared connection between the command check and constructor."""

    def __init__(self, *, entry="startup", disconnect=True, fail_constructor=False):
        self.entry = entry
        self.disconnect = disconnect
        self.fail_constructor = fail_constructor
        self.connected = entry == "submit"
        self.calls = []
        self.resources = 0
        self.checks = 0

    def init(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_constructor and (self.entry == "submit" or len(self.calls) % 2 == 0):
            raise RuntimeError("selected connection unavailable")
        self.connected = True

    def is_initialized(self):
        self.checks += 1
        observed = self.connected
        if self.entry == "submit" and self.checks == 1 and self.disconnect:
            self.connected = False
        return observed

    def cluster_resources(self):
        self.resources += 1
        after = 2 if self.entry == "reconnect" else 1
        if self.disconnect and self.resources % after == 0:
            self.connected = False
        return {"CPU": 1}

    def shutdown(self):
        self.connected = False


@pytest.fixture
def command(monkeypatch):
    command = worker.Command(stdout=StringIO())
    monkeypatch.setattr(command, "setup_signal_handlers", Mock())
    monkeypatch.setattr(command, "_create_lease", Mock())
    monkeypatch.setattr(command, "shutdown", Mock())
    monkeypatch.setattr(command, "run_loop", Mock())
    monkeypatch.setattr(worker.time, "sleep", lambda _delay: None)
    return command


def _set_ambient(monkeypatch, address):
    if address is None:
        monkeypatch.delenv("RAY_ADDRESS", raising=False)
    else:
        monkeypatch.setenv("RAY_ADDRESS", address)


@pytest.mark.parametrize("ambient", [AMBIENT, None])
@pytest.mark.parametrize("disconnect", [False, True])
def test_cluster_cli_keeps_selected_target_through_runner_construction(
    monkeypatch, command, ambient, disconnect
):
    _set_ambient(monkeypatch, ambient)
    fake_ray = FakeRay(disconnect=disconnect)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    parser = command.create_parser("manage.py", "django_ray_worker")
    options = vars(parser.parse_args(["--cluster", REQUESTED, "--queue", "default"]))

    command.handle(**options)

    assert command.cluster_address == REQUESTED
    assert isinstance(command.ray_core_runner, RayCoreRunner)
    assert [call["address"] for call in fake_ray.calls] == [REQUESTED] * (2 if disconnect else 1)
    assert os.environ.get("RAY_ADDRESS") == ambient
    command.run_loop.assert_called_once()


@pytest.mark.parametrize("ambient", [AMBIENT, None])
@pytest.mark.parametrize("fail", [False, True])
def test_cluster_reconnect_retries_only_selected_target(monkeypatch, command, ambient, fail):
    _set_ambient(monkeypatch, ambient)
    command.execution_mode = "cluster"
    command.cluster_address = REQUESTED
    fake_ray = FakeRay(entry="reconnect", fail_constructor=fail)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)

    command._reconnect_ray()

    assert [call["address"] for call in fake_ray.calls] == [REQUESTED] * (10 if fail else 2)
    assert os.environ.get("RAY_ADDRESS") == ambient
    if fail:
        assert command.ray_core_runner is None
        assert "Failed to reconnect after 5 attempts" in command.stdout._out.getvalue()
    else:
        assert isinstance(command.ray_core_runner, RayCoreRunner)


@pytest.mark.parametrize("ambient", [AMBIENT, None])
@pytest.mark.parametrize("fail", [False, True])
def test_cluster_submission_runner_keeps_target_and_existing_failure_boundary(
    monkeypatch, command, ambient, fail
):
    _set_ambient(monkeypatch, ambient)
    command.execution_mode = "cluster"
    command.cluster_address = REQUESTED
    fake_ray = FakeRay(entry="submit", fail_constructor=fail)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    submit = Mock(side_effect=RuntimeError("stop before execution"))
    monkeypatch.setattr(RayCoreRunner, "submit_durable", submit)
    failure = Mock()
    monkeypatch.setattr(command, "_handle_task_failure", failure)
    task = SimpleNamespace(attempt_number=1, execution_generation=1, ray_job_id="")

    if fail:
        with pytest.raises(RuntimeError, match="selected connection unavailable"):
            command.submit_task_to_ray_core(task)
        assert command.ray_core_runner is None
        submit.assert_not_called()
        failure.assert_not_called()
    else:
        command.submit_task_to_ray_core(task)
        assert isinstance(command.ray_core_runner, RayCoreRunner)
        submit.assert_called_once_with(task_execution=task)
        failure.assert_called_once()
    assert fake_ray.calls == [{"address": REQUESTED, "ignore_reinit_error": True}]
    assert os.environ.get("RAY_ADDRESS") == ambient


def test_cluster_startup_constructor_failure_keeps_existing_loop_recovery(monkeypatch, command):
    monkeypatch.setenv("RAY_ADDRESS", AMBIENT)
    fake_ray = FakeRay(fail_constructor=True)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    parser = command.create_parser("manage.py", "django_ray_worker")

    command.handle(**vars(parser.parse_args(["--cluster", REQUESTED, "--queue", "default"])))

    assert [call["address"] for call in fake_ray.calls] == [REQUESTED, REQUESTED]
    assert command.ray_core_runner is None
    command.run_loop.assert_called_once()
    assert "Will retry connection during operation" in command.stdout._out.getvalue()


@pytest.mark.parametrize("ambient", [AMBIENT, None])
@pytest.mark.parametrize("initialized", [False, True])
def test_standalone_runner_keeps_default_initialization(monkeypatch, ambient, initialized):
    _set_ambient(monkeypatch, ambient)
    fake_ray = SimpleNamespace(is_initialized=lambda: initialized, init=Mock())
    monkeypatch.setitem(sys.modules, "ray", fake_ray)

    RayCoreRunner()

    if initialized:
        fake_ray.init.assert_not_called()
    else:
        fake_ray.init.assert_called_once_with(address=ambient or "auto", ignore_reinit_error=True)


@pytest.mark.parametrize("address", [REQUESTED, "selected.invalid:6379", "auto"])
@pytest.mark.parametrize("initialized", [False, True])
def test_explicit_runner_address_overrides_ambient_only_when_initializing(
    monkeypatch, address, initialized
):
    monkeypatch.setenv("RAY_ADDRESS", AMBIENT)
    fake_ray = SimpleNamespace(is_initialized=lambda: initialized, init=Mock())
    monkeypatch.setitem(sys.modules, "ray", fake_ray)

    RayCoreRunner(address=address)

    if initialized:
        fake_ray.init.assert_not_called()
    else:
        fake_ray.init.assert_called_once_with(address=address, ignore_reinit_error=True)
    assert os.environ["RAY_ADDRESS"] == AMBIENT


def test_cluster_runner_without_selected_address_refuses_before_construction(monkeypatch, command):
    command.execution_mode = "cluster"
    command.cluster_address = None
    monkeypatch.setenv("RAY_ADDRESS", AMBIENT)
    constructor = Mock(side_effect=AssertionError("runner must not be constructed"))
    monkeypatch.setattr(worker, "RayCoreRunner", constructor)

    with pytest.raises(CommandError, match="Cluster execution requires a selected Ray address"):
        command._create_ray_core_runner()

    constructor.assert_not_called()
    assert os.environ["RAY_ADDRESS"] == AMBIENT


def test_local_command_keeps_its_helper_configuration_and_default_constructor(monkeypatch, command):
    monkeypatch.setenv("RAY_ADDRESS", AMBIENT)
    fake_ray = FakeRay(disconnect=False)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    constructor = Mock(wraps=RayCoreRunner)
    monkeypatch.setattr(worker, "RayCoreRunner", constructor)
    parser = command.create_parser("manage.py", "django_ray_worker")

    command.handle(**vars(parser.parse_args(["--local", "--queue", "default"])))

    constructor.assert_called_once_with()
    assert len(fake_ray.calls) == 1
    options = fake_ray.calls[0]
    assert "address" not in options
    assert options["dashboard_host"] == "127.0.0.1"
    assert options["dashboard_port"] == 8265
    assert options["include_dashboard"] is True
    assert options["runtime_env"]["env_vars"]["PYTHONPATH"] == os.pathsep.join(sys.path)
    assert options["_system_config"] == {
        "enable_timeline": True,
        "task_events_report_interval_ms": 100,
    }
    assert "RAY_ADDRESS" not in os.environ
