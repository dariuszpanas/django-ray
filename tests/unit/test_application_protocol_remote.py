"""Real strict decoding and owned remote-lifetime boundaries without Ray startup."""

import hashlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import ray
from ray import exceptions

from qualification.application import protocol_remote as remote
from qualification.application.protocol_fixture import Fixture
from testproject import qualification_protocol_tasks


def wrapped(cause):
    return exceptions.RayTaskError(
        "owned", "not exported", cause, proctitle="owned", pid=123, ip="127.0.0.1"
    )


@pytest.mark.parametrize(
    "error,confirmed",
    [
        (None, True),
        (exceptions.TaskCancelledError(), True),
        (wrapped(exceptions.TaskCancelledError()), True),
        (wrapped(ValueError("task ended")), True),
        (exceptions.RayError("transport unknown"), False),
        (exceptions.GetTimeoutError("still executing"), False),
        (wrapped(exceptions.RayError("wrapped transport unknown")), False),
        (wrapped(exceptions.GetTimeoutError("wrapped timeout")), False),
        (exceptions.WorkerCrashedError(), False),
        (OSError("transport unknown"), False),
    ],
)
def test_cleanup_confirms_only_completed_result_or_explicit_terminal_task_error(error, confirmed):
    owned = remote.Remote()
    owned.refs = [object()]
    owned.ray = SimpleNamespace(
        cancel=Mock(), get=Mock(side_effect=error), shutdown=Mock(), exceptions=exceptions
    )
    closed = Mock()
    owned.stack.callback(closed)
    if confirmed:
        owned.cleanup(remote.time.monotonic() + 5)
    elif isinstance(error, TimeoutError):
        with pytest.raises(TimeoutError):
            owned.cleanup(remote.time.monotonic() + 5)
    else:
        with pytest.raises(ValueError, match="unconfirmed"):
            owned.cleanup(remote.time.monotonic() + 5)
    owned.ray.cancel.assert_called_once_with(owned.refs[0], force=True)
    if isinstance(error, TimeoutError):
        owned.ray.shutdown.assert_not_called()
        closed.assert_not_called()
    else:
        owned.ray.shutdown.assert_called_once_with()
        closed.assert_called_once_with()


def test_ambiguous_submission_cannot_be_cleared_by_later_shutdown():
    owned = remote.Remote()
    owned.ray = SimpleNamespace(shutdown=Mock(), exceptions=exceptions)
    function = SimpleNamespace(remote=Mock(side_effect=OSError("lost submission")))
    with pytest.raises(OSError):
        owned.get(function, (), {}, remote.time.monotonic() + 5)
    assert owned.uncertain and not owned.refs
    with pytest.raises(ValueError, match="unconfirmed"):
        owned.cleanup(remote.time.monotonic() + 5)
    owned.ray.shutdown.assert_called_once_with()


def test_timed_out_get_retains_exact_reference_for_cancellation():
    owned = remote.Remote()
    reference = object()
    owned.ray = SimpleNamespace(get=Mock(side_effect=exceptions.GetTimeoutError()))
    with pytest.raises(exceptions.GetTimeoutError):
        owned.get(
            SimpleNamespace(remote=Mock(return_value=reference)),
            (),
            {},
            remote.time.monotonic() + 5,
        )
    assert owned.refs == [reference] and not owned.uncertain


def test_shutdown_failure_still_releases_owned_runtime_snapshot():
    owned = remote.Remote()
    owned.ray = SimpleNamespace(shutdown=Mock(side_effect=OSError("lost connection")))
    closed = Mock()
    owned.stack.callback(closed)
    with pytest.raises(OSError):
        owned.cleanup(remote.time.monotonic() + 5)
    closed.assert_called_once_with()


def test_cleanup_does_not_swallow_owned_alarm_or_begin_more_calls_after_expiry(monkeypatch):
    owned = remote.Remote()
    owned.refs = [object(), object()]
    clock = [0.0]

    def interrupted(*args, **kwargs):
        clock[0] = 7.0
        raise TimeoutError("owned alarm")

    owned.ray = SimpleNamespace(cancel=Mock(side_effect=interrupted), get=Mock(), shutdown=Mock())
    monkeypatch.setattr(remote, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(TimeoutError):
        owned.cleanup(7)
    assert owned.ray.cancel.call_count == 1
    owned.ray.get.assert_not_called()
    owned.ray.shutdown.assert_not_called()
    with pytest.raises(TimeoutError):
        owned.cleanup(7)
    assert owned.ray.cancel.call_count == 1


def test_shared_mount_control_reads_candidate_module_and_writes_back(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ray, "get_runtime_context", lambda: SimpleNamespace(get_node_id=lambda: "node")
    )
    control = b"owned-control"
    (tmp_path / "control").write_bytes(control)
    digest = hashlib.sha256(
        remote.Path(qualification_protocol_tasks.__file__).read_bytes()
    ).hexdigest()
    probe = remote.mount_probe()
    assert probe(str(tmp_path), control, digest, first=True) == {
        "node": "node",
        "marker_absent": True,
    }
    assert (tmp_path / "observed").read_bytes() == control
    (tmp_path / "invoked").write_bytes(b"invoked")
    assert probe(str(tmp_path), control, digest, first=False)["marker_absent"] is False
    with pytest.raises(ValueError, match="differs"):
        probe(str(tmp_path), control, "0" * 64, first=False)
    with pytest.raises(ValueError, match="not shared"):
        probe(str(tmp_path), b"another-control", digest, first=False)


def test_actual_poison_invocation_leaves_exclusive_marker(tmp_path, monkeypatch):
    path = remote.Path
    monkeypatch.setattr(qualification_protocol_tasks, "Path", lambda _: tmp_path)
    task_id = str(uuid4())
    root = tmp_path / f"protocol-before-{task_id}"
    root.mkdir()
    assert (
        qualification_protocol_tasks.poison("before", task_id)
        == "protocol-qualification-poison-invoked"
    )
    assert (root / "invoked").read_bytes() == b"invoked"
    with pytest.raises(FileExistsError):
        qualification_protocol_tasks.poison("before", task_id)
    with pytest.raises(ValueError):
        qualification_protocol_tasks.poison("foreign", task_id)
    assert path(root).is_dir()


def test_reject_exercises_actual_strict_entrypoint_before_bootstrap_or_callable(
    tmp_path, monkeypatch
):
    from ray import cloudpickle

    import django_ray.runtime.remote as remote_module
    from django_ray.runner import ray_core
    from django_ray.runtime import entrypoint
    from django_ray.runtime.remote import execute_django_task_remote

    forbidden = Mock(side_effect=AssertionError("unsupported request crossed invocation boundary"))
    monkeypatch.setattr(entrypoint, "execute_task", forbidden)
    monkeypatch.setattr(entrypoint, "bootstrap_django", forbidden)
    monkeypatch.setattr(qualification_protocol_tasks, "poison", forbidden)
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        assert "_target_execution_transport" not in kwargs
        return execute_django_task_remote(*args, **kwargs)

    payloads = []

    def remote_definition():
        # Ray Client loads this definition on the generic head before the
        # task's RuntimeEnv supplies django-ray. Capture the real wire payload.
        payloads.append(cloudpickle.dumps(execute_django_task_remote))
        return SimpleNamespace(options=lambda **kwargs: SimpleNamespace(remote=execute))

    monkeypatch.setattr(ray_core, "_get_remote_execute_django_task", remote_definition)
    owned = remote.Remote()
    owned.ray = SimpleNamespace(
        cloudpickle=cloudpickle,
        get=lambda result, **kwargs: result,
        util=SimpleNamespace(
            client=SimpleNamespace(ray=SimpleNamespace(is_connected=lambda: True))
        ),
    )
    owned.options = {"scheduling_strategy": "fixed-worker"}
    owned.profile = SimpleNamespace(profile="thin", digest="a" * 64)
    owned.plan = SimpleNamespace(as_transport_dict=lambda: {"profile": "thin", "schema_version": 1})
    owned.root = tmp_path
    observations = []
    monkeypatch.setattr(owned, "observe", lambda deadline: observations.append(deadline))
    fixture = Fixture("before", pk=123)
    registered = remote_module.__name__ in cloudpickle.list_registry_pickle_by_value()
    if registered:
        cloudpickle.unregister_pickle_by_value(remote_module)
    try:
        owned.reject(fixture, remote.time.monotonic() + 5)
    finally:
        if remote_module.__name__ in cloudpickle.list_registry_pickle_by_value():
            cloudpickle.unregister_pickle_by_value(remote_module)
        if registered:
            cloudpickle.register_pickle_by_value(remote_module)
    assert len(calls) == len(observations) == 1
    assert not owned.refs and not owned.uncertain
    assert not (tmp_path / "invoked").exists()
    forbidden.assert_not_called()
    assert len(payloads) == 1
    generic_head = """
import importlib.abc
import sys
import ray.cloudpickle as cloudpickle

class BlockDjangoRay(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "django_ray" or fullname.startswith("django_ray."):
            raise ModuleNotFoundError(fullname, name=fullname)
        return None

sys.meta_path.insert(0, BlockDjangoRay())
cloudpickle.loads(sys.stdin.buffer.read())
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", generic_head],
        input=payloads[0],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_owned_get_refuses_expired_work_before_submission(monkeypatch):
    owned = remote.Remote()
    function = SimpleNamespace(remote=Mock())
    monkeypatch.setattr(remote, "time", SimpleNamespace(monotonic=lambda: 10))
    with pytest.raises(TimeoutError):
        owned.get(function, (), {}, 10)
    function.remote.assert_not_called()


def test_prepare_uses_production_profile_and_same_hard_pinned_worker_without_retries(
    tmp_path, monkeypatch, settings
):
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "ray://ray-head:10001",
        "RUNTIME_ENV_PROFILES": {"project": {}},
    }
    monkeypatch.setattr(ray, "is_initialized", lambda: False)
    initialize = Mock()
    shutdown = Mock()
    monkeypatch.setattr(ray, "init", initialize)
    monkeypatch.setattr(ray, "shutdown", shutdown)
    monkeypatch.setattr(
        ray,
        "nodes",
        lambda: [
            {"Alive": True, "NodeID": "a" * 56, "Resources": {"node:__internal_head__": 1}},
            {"Alive": True, "NodeID": "b" * 56, "Resources": {"CPU": 1}},
        ],
    )
    monkeypatch.setattr(
        ray, "get_runtime_context", lambda: SimpleNamespace(get_node_id=lambda: "b" * 56)
    )
    monkeypatch.setattr(ray, "get", lambda value, **kwargs: value)
    submissions = []

    def wrap(function):
        def options(**kwargs):
            submissions.append(kwargs)
            return SimpleNamespace(remote=function)

        return SimpleNamespace(options=options)

    monkeypatch.setattr(ray, "remote", wrap)
    control = b"unpredictable-owned-control"
    (tmp_path / "control").write_bytes(control)
    owned = remote.Remote()
    owned.prepare(tmp_path, control, remote.time.monotonic() + 5)
    owned.observe(remote.time.monotonic() + 5)
    assert len(submissions) == 2
    for options in submissions:
        assert options["scheduling_strategy"].node_id == "b" * 56
        assert options["scheduling_strategy"].soft is False
        assert options["max_retries"] == 0 and options["retry_exceptions"] is False
        assert options["runtime_env"] == {}
    assert owned.profile.profile == "project"
    assert (tmp_path / "observed").read_bytes() == control
    initialize.assert_called_once_with(address="ray://ray-head:10001")
    owned.cleanup(remote.time.monotonic() + 5)
    shutdown.assert_called_once_with()
