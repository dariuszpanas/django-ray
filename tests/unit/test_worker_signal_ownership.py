"""Resource-free coverage of command-owned signals across Ray initialization."""

from __future__ import annotations

import signal
import sys
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django_ray.management.commands import django_ray_worker as worker
from django_ray.runner.leasing import WorkerLeaseIdentity


def _ray_exit(signum, _frame):
    """Match Ray's main-thread init handler without importing or starting Ray."""
    raise SystemExit(signum)


@pytest.fixture
def owned_signals(monkeypatch):
    handlers = {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_DFL}
    writes = []

    def install(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        writes.append((signum, handler))
        return previous

    monkeypatch.setattr(worker.signal, "getsignal", handlers.__getitem__)
    monkeypatch.setattr(worker.signal, "signal", install)
    monkeypatch.delenv("RAY_ADDRESS", raising=False)
    monkeypatch.delenv("RAY_RUNTIME_ENV_HOOK", raising=False)
    command = worker.Command(stdout=StringIO())
    command.setup_signal_handlers()
    writes.clear()
    return command, handlers, writes


def _initialize(command, mode):
    if mode == "local":
        command._init_local_ray()
    else:
        command._init_cluster_ray(mode)


MODES = ("local", "auto", "ray://cluster:10001")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_initialization_restores_owned_handlers_on_every_exit(
    monkeypatch, owned_signals, mode, error
):
    command, handlers, _writes = owned_signals

    def initialize(**_kwargs):
        handlers.update(dict.fromkeys(handlers, _ray_exit))
        if error is not None:
            raise error("initialization interrupted")

    def resources():
        assert all(handler == command.handle_shutdown_signal for handler in handlers.values())
        return {"CPU": 1}

    fake_ray = SimpleNamespace(
        init=Mock(side_effect=initialize),
        is_initialized=lambda: False,
        cluster_resources=resources,
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)

    if error is None:
        _initialize(command, mode)
    else:
        with pytest.raises(error, match="initialization interrupted"):
            _initialize(command, mode)

    fake_ray.init.assert_called_once()
    assert all(handler == command.handle_shutdown_signal for handler in handlers.values())
    assert not command.shutdown_requested


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("first_signal", [signal.SIGINT, signal.SIGTERM])
def test_second_signal_during_handoff_keeps_lease_release_and_disconnect(
    monkeypatch, owned_signals, mode, first_signal
):
    command, handlers, _writes = owned_signals
    events = []
    initialized = False

    def initialize(**_kwargs):
        nonlocal initialized
        handlers.update(dict.fromkeys(handlers, _ray_exit))
        initialized = True

    fake_ray = SimpleNamespace(
        init=initialize,
        is_initialized=lambda: initialized,
        cluster_resources=lambda: {"CPU": 1},
        shutdown=lambda: events.append("disconnect"),
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    command.execution_mode = "local" if mode == "local" else "cluster"
    command.lease_identity = WorkerLeaseIdentity(
        worker_id="signal-test", hostname="unit-host", pid=1, started_at=datetime.now(UTC)
    )

    def handoff():
        events.append("handoff")
        second_signal = signal.SIGTERM if first_signal == signal.SIGINT else signal.SIGINT
        handlers[second_signal](second_signal, None)

    monkeypatch.setattr(command, "_prepare_shutdown_handoff", handoff)
    release = Mock(side_effect=lambda _identity: events.append("release") or True)
    monkeypatch.setattr("django_ray.runner.leasing.release_lease", release)
    _initialize(command, mode)
    handlers[first_signal](first_signal, None)

    command.shutdown()

    assert events == ["handoff", "release", "disconnect"]
    release.assert_called_once_with(command.lease_identity)
    assert command.shutdown_signal == first_signal
    assert command.shutdown_exit_code == 128 + first_signal
    assert command._shutdown_event.is_set()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("recover", [True, False])
def test_reconnect_restores_handlers_between_attempts_and_after_exhaustion(
    monkeypatch, owned_signals, mode, recover
):
    command, handlers, _writes = owned_signals
    attempts = 0
    initialized = False

    def initialize(**_kwargs):
        nonlocal attempts, initialized
        assert all(handler == command.handle_shutdown_signal for handler in handlers.values())
        attempts += 1
        handlers.update(dict.fromkeys(handlers, _ray_exit))
        if not recover or attempts == 1:
            raise RuntimeError("connection unavailable")
        initialized = True

    fake_ray = SimpleNamespace(
        init=initialize,
        is_initialized=lambda: initialized,
        cluster_resources=lambda: {"CPU": 1},
        shutdown=Mock(),
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr(worker.time, "sleep", lambda _delay: None)
    runner = Mock()
    monkeypatch.setattr(worker, "RayCoreRunner", runner)
    command.execution_mode = "local" if mode == "local" else "cluster"
    command.cluster_address = None if mode == "local" else mode

    command._reconnect_ray()

    assert attempts == (2 if recover else 5)
    assert runner.call_count == (1 if recover else 0)
    assert all(handler == command.handle_shutdown_signal for handler in handlers.values())


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("owned", [(), (signal.SIGINT,)])
def test_embedded_initialization_only_restores_this_commands_owned_slots(
    monkeypatch, owned_signals, mode, owned
):
    command, handlers, writes = owned_signals
    other_command = worker.Command(stdout=StringIO())
    for signum in handlers:
        if signum not in owned:
            handlers[signum] = other_command.handle_shutdown_signal
    fake_ray = SimpleNamespace(
        init=lambda **_kwargs: handlers.update(dict.fromkeys(handlers, _ray_exit)),
        is_initialized=lambda: False,
        cluster_resources=dict,
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)

    _initialize(command, mode)

    assert writes == [(signum, command.handle_shutdown_signal) for signum in owned]
    for signum in handlers:
        expected = command.handle_shutdown_signal if signum in owned else _ray_exit
        assert handlers[signum] == expected


@pytest.mark.parametrize("mode", MODES)
def test_off_main_thread_helper_does_not_register_handlers(monkeypatch, owned_signals, mode):
    command, handlers, writes = owned_signals
    monkeypatch.setattr(worker, "current_thread", object)
    fake_ray = SimpleNamespace(init=Mock(), is_initialized=lambda: False, cluster_resources=dict)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr(worker.signal, "getsignal", Mock(side_effect=AssertionError("not owned")))

    _initialize(command, mode)

    fake_ray.init.assert_called_once()
    assert not writes
    assert all(handler == command.handle_shutdown_signal for handler in handlers.values())


def test_existing_local_runtime_does_not_touch_signal_handlers(monkeypatch, owned_signals):
    command, _handlers, writes = owned_signals
    fake_ray = SimpleNamespace(init=Mock(), is_initialized=lambda: True)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr(worker.signal, "getsignal", Mock(side_effect=AssertionError("no init")))

    command._init_local_ray()

    fake_ray.init.assert_not_called()
    assert not writes


@pytest.mark.parametrize("entry", ["startup_local", "startup_cluster", "reconnect", "submit"])
@pytest.mark.parametrize("fail", [False, True])
def test_command_runner_initialization_restores_handlers_after_connection_loss(
    monkeypatch, owned_signals, entry, fail
):
    command, handlers, _writes = owned_signals
    command.execution_mode = "local" if entry == "startup_local" else "cluster"
    command.cluster_address = "ray://cluster:10001"
    # The explicit connection helper has already completed. A background Ray
    # Client disconnect can make the real runner constructor initialize again.
    monkeypatch.setattr(command, "_init_local_ray", lambda: None)
    monkeypatch.setattr(command, "_init_cluster_ray", lambda _address: None)
    monkeypatch.setattr(worker.time, "sleep", lambda _delay: None)

    def initialize(**_kwargs):
        handlers.update(dict.fromkeys(handlers, _ray_exit))
        if fail:
            raise RuntimeError("runner initialization unavailable")

    if entry == "reconnect":
        # Initial reconnect check, then healthy verification followed by loss
        # at constructor entry, repeated for each failed retry.
        states = [False] + [True, False] * (5 if fail else 1)
    elif entry == "submit":
        states = [True, False]
    else:
        states = [False]
    fake_ray = SimpleNamespace(
        init=Mock(side_effect=initialize),
        is_initialized=Mock(side_effect=states),
        cluster_resources=lambda: {"CPU": 1},
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    submit = Mock(side_effect=RuntimeError("stop before task execution"))
    monkeypatch.setattr(worker.RayCoreRunner, "submit_durable", submit)
    failure = Mock()
    monkeypatch.setattr(command, "_handle_task_failure", failure)

    if entry.startswith("startup"):
        command._initialize_ray_execution()
    elif entry == "reconnect":
        command._reconnect_ray()
    else:
        task = SimpleNamespace(attempt_number=1, execution_generation=1, ray_job_id="")
        if fail:
            with pytest.raises(RuntimeError, match="runner initialization unavailable"):
                command.submit_task_to_ray_core(task)
        else:
            command.submit_task_to_ray_core(task)
            submit.assert_called_once_with(task_execution=task)
            failure.assert_called_once()

    assert fake_ray.init.call_count == (5 if entry == "reconnect" and fail else 1)
    assert all(handler == command.handle_shutdown_signal for handler in handlers.values())
    if fail:
        assert command.ray_core_runner is None
    else:
        assert isinstance(command.ray_core_runner, worker.RayCoreRunner)
