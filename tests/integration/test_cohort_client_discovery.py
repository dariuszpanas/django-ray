"""Resource-free Client composition and a mandatory owned native Linux comparison."""

import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import ray
import ray.client_builder
import ray.util.client as client
from ray.dashboard.modules.job.pydantic_models import JobStatus

from django_ray.runner import cohort_client_discovery as discovery
from tests.unit.test_cohort_client_discovery import (
    ENDPOINT,
    NATIVE_ID,
    NOW,
    RUNTIME,
    driver,
    request,
)


@pytest.fixture
def owned_client(monkeypatch):
    state = SimpleNamespace(
        now=NOW,
        calls=[],
        connected=False,
        context_active=False,
        built=None,
        close_error=False,
        connect_error=False,
        details=driver(),
    )

    class Context:
        ray_version = "2.58.0"
        python_version = ".".join(
            map(str, (RUNTIME.python_major, RUNTIME.python_minor, RUNTIME.python_patch))
        )
        dashboard_url = ENDPOINT

        def __enter__(self):
            state.calls.append("enter")
            state.context_active = True
            return self

        def __exit__(self, *_args):
            state.calls.append("restore")
            state.context_active = False

        def disconnect(self):
            state.calls.append("disconnect")
            state.connected = False
            if state.close_error:
                raise RuntimeError("private disconnect detail")

    class Builder:
        def __init__(self, address):
            state.calls.append("builder")
            state.address = address
            state.built = self

        def _init_args(self, **kwargs):
            state.options = kwargs

        def connect(self):
            state.calls.append("connect")
            if state.connect_error:
                raise RuntimeError("private connect detail")
            state.connected = True
            return state.context

    def runtime_context():
        assert state.context_active and state.connected
        state.calls.append("runtime")
        return SimpleNamespace(
            get_job_id=lambda: NATIVE_ID,
            get_session_name=lambda: "session_native_test",
            namespace=discovery._namespace(request()),
        )

    def details(endpoint, native_id, **_kwargs):
        assert endpoint == ENDPOINT and native_id == NATIVE_ID
        state.calls.append("live-http" if state.connected else "cleanup-http")
        return state.details

    state.context = Context()
    monkeypatch.setattr(discovery, "_now", lambda: state.now)
    monkeypatch.setattr(ray, "is_initialized", lambda: False)
    monkeypatch.setattr(client.ray, "is_connected", lambda: False)
    monkeypatch.setattr(client.ray, "is_default", lambda: True)
    monkeypatch.setattr(client, "num_connected_contexts", lambda: 0)
    monkeypatch.setattr(ray.client_builder, "ClientBuilder", Builder)
    monkeypatch.setattr(ray, "get_runtime_context", runtime_context)
    monkeypatch.setattr(discovery, "_driver_details", details)
    monkeypatch.setattr(ray, "init", lambda **_kwargs: pytest.fail("Discovery used implicit init"))
    monkeypatch.setattr(ray, "shutdown", lambda: pytest.fail("Discovery used global shutdown"))
    return state


def test_owned_discovery_correlates_live_driver_before_disconnect_and_separate_death(
    owned_client, monkeypatch
):
    state = owned_client
    monkeypatch.setenv("RAY_RUNTIME_ENV", '{"working_dir":"/unrelated/source"}')
    monkeypatch.setenv("RAY_NAMESPACE", "unrelated")
    observed = discovery.discover_client_jobs_endpoint(request())
    assert state.calls == [
        "builder",
        "connect",
        "enter",
        "runtime",
        "live-http",
        "restore",
        "disconnect",
    ]
    assert state.built._job_config.runtime_env == {
        "py_executable": "python",
        "env_vars": {"RAY_ENABLE_UV_RUN_RUNTIME_ENV": "0"},
    }
    assert state.built._job_config.metadata == discovery._metadata(request())
    assert "job_submission_id" not in state.built._job_config.metadata
    assert state.built._job_config.ray_namespace == discovery._namespace(request())
    assert state.options == {
        "allow_multiple": True,
        "logging_level": "ERROR",
        "log_to_driver": False,
    }
    assert state.address == "ray.example.test:10001"
    assert observed["disconnect_returned"] is True
    assert "terminal" not in observed
    pending = discovery.inspect_client_driver({"request": request(), "observation": observed})
    assert pending["terminal"] is False
    state.details = driver(status=JobStatus.SUCCEEDED)
    # Expiry prevents another driver creation, but cannot prevent cleanup reads.
    state.now = NOW + timedelta(minutes=3)
    terminal = discovery.inspect_client_driver({"request": request(), "observation": observed})
    assert terminal["terminal"] is True
    assert state.calls.count("connect") == state.calls.count("disconnect") == 1


@pytest.mark.parametrize(
    "change", ["init", "close", "expired", "python", "namespace", "metadata", "runtime_env"]
)
def test_unknown_or_crossed_discovery_never_returns_cleanup_proof(
    owned_client, monkeypatch, change
):
    state = owned_client
    if change == "init":
        state.connect_error = True
    elif change == "close":
        state.close_error = True
    elif change == "expired":
        state.now = NOW + timedelta(minutes=2)
    elif change == "python":
        state.context.python_version = "3.1.0"
    elif change == "namespace":
        monkeypatch.setattr(
            ray,
            "get_runtime_context",
            lambda: SimpleNamespace(
                get_job_id=lambda: NATIVE_ID,
                get_session_name=lambda: "session_native_test",
                namespace="wrong",
            ),
        )
    elif change == "metadata":
        state.details = driver(metadata={"job_submission_id": NATIVE_ID})
    else:
        state.details = driver(runtime_env={"working_dir": "gcs://other.zip"})
    with pytest.raises(discovery.ClientDiscoveryError) as error:
        discovery.discover_client_jobs_endpoint(request())
    assert "private" not in str(error.value)
    assert state.calls.count("disconnect") <= 1
    assert "cleanup-http" not in state.calls


@pytest.mark.parametrize(
    "change", ["native_id", "pid", "ip", "entrypoint", "start_time", "metadata", "status"]
)
def test_driver_cleanup_must_match_original_live_identity(owned_client, change):
    state = owned_client
    observed = discovery.discover_client_jobs_endpoint(request())
    details = driver(status=JobStatus.SUCCEEDED)
    if change == "native_id":
        details = details.model_copy(update={"job_id": "02000000"})
    elif change == "pid":
        details.driver_info.pid = "124"
    elif change == "ip":
        details.driver_info.node_ip_address = "10.0.0.5"
    elif change == "entrypoint":
        details.entrypoint = "different native driver"
    elif change == "start_time":
        details.start_time += 1
    elif change == "metadata":
        details.metadata = {}
    else:
        details.status = JobStatus.FAILED
    state.details = details
    with pytest.raises(discovery.ClientDiscoveryError):
        discovery.inspect_client_driver({"request": request(), "observation": observed})
    assert state.calls.count("connect") == state.calls.count("disconnect") == 1


@pytest.mark.parametrize(
    "change", ["initialized", "falsey", "connected", "nondefault", "other_context", "boolean_count"]
)
def test_discovery_refuses_any_preexisting_or_unknown_connection_state(
    owned_client, monkeypatch, change
):
    if change in {"initialized", "falsey"}:
        monkeypatch.setattr(ray, "is_initialized", lambda: True if change == "initialized" else 0)
    elif change == "connected":
        monkeypatch.setattr(client.ray, "is_connected", lambda: True)
    elif change == "nondefault":
        monkeypatch.setattr(client.ray, "is_default", lambda: False)
    else:
        monkeypatch.setattr(
            client, "num_connected_contexts", lambda: True if change == "boolean_count" else 1
        )
    with pytest.raises(discovery.ClientDiscoveryError, match="unsupported_context"):
        discovery.discover_client_jobs_endpoint(request())
    assert owned_client.calls == []


def test_close_return_after_deadline_never_returns_discovery_observation(owned_client):
    state = owned_client
    original = state.context.disconnect

    def close():
        original()
        state.now = NOW + timedelta(minutes=2)

    state.context.disconnect = close
    with pytest.raises(discovery.ClientDiscoveryError, match="expired"):
        discovery.discover_client_jobs_endpoint(request())
    assert state.calls.count("disconnect") == 1


def test_cleanup_transport_failure_cannot_be_reinterpreted_as_driver_death(
    owned_client, monkeypatch
):
    observed = discovery.discover_client_jobs_endpoint(request())

    def fail(*_args, **_kwargs):
        raise RuntimeError("private server transport detail")

    monkeypatch.setattr(discovery, "_driver_details", fail)
    with pytest.raises(discovery.ClientDiscoveryError, match="cleanup_unconfirmed") as error:
        discovery.inspect_client_driver({"request": request(), "observation": observed})
    assert "private" not in str(error.value)
    assert owned_client.calls.count("connect") == owned_client.calls.count("disconnect") == 1


def test_cleanup_clock_regression_during_http_cannot_emit_terminal(owned_client, monkeypatch):
    state = owned_client
    observed = discovery.discover_client_jobs_endpoint(request())
    state.now = NOW + timedelta(seconds=10)

    def details(*_args, **_kwargs):
        state.now = NOW + timedelta(seconds=5)
        return driver(status=JobStatus.SUCCEEDED)

    monkeypatch.setattr(discovery, "_driver_details", details)
    with pytest.raises(discovery.ClientDiscoveryError, match="expired"):
        discovery.inspect_client_driver({"request": request(), "observation": observed})


_OWNED_CLIENT_DIAGNOSTIC_SOURCE = r'''
"""Temporary diagnostic for exactly one owned Ray Client server, not app code."""
import sys
from pathlib import Path


def role(arguments):
    if type(arguments) not in (list, tuple) or not 3 <= len(arguments) <= 32:
        return None
    if any(type(value) is not str for value in arguments):
        return None
    if len(arguments) == 4 and list(arguments[1:3]) == [
        "-m", "django_ray.runner.cohort_job_helper"
    ]:
        return "owned_helper"
    if arguments.count("-m") != 1:
        return None
    index = arguments.index("-m")
    if list(arguments[index:index + 2]) != ["-m", "ray.util.client.server"]:
        return None
    modes = [value for value in arguments if value.startswith("--mode=")]
    if modes not in (["--mode=proxy"], ["--mode=specific-server"]):
        return None
    if index == 1:
        stage = "module"
    elif index == 2 and arguments[1].replace("\\", "/").endswith(
        "/ray/_private/workers/setup_worker.py"
    ):
        stage = "shim"
    else:
        return None
    return modes[0][len("--mode="):] + "_" + stage


def exit_kind(code):
    if type(code) is not int:
        return "EXIT_UNKNOWN"
    known = {0: "EXIT_ZERO", 1: "EXIT_ONE", 2: "EXIT_TWO", 126: "EXEC_PERMISSION",
             127: "EXEC_NOT_FOUND", -6: "SIGNAL_ABORT", -9: "SIGNAL_KILL",
             -11: "SIGNAL_SEGV", -15: "SIGNAL_TERM", -1: "SIGNAL_HUP"}
    return known.get(code, "EXIT_OTHER" if code > 0 else "SIGNAL_OTHER")


def install_helper(directory):
    import ray
    import ray.client_builder
    import ray.runtime_context
    from django_ray.runner import cohort_client_discovery as discovery

    active = {}
    seen = set()

    def record(label):
        # Each fixed label is written at most once per owned helper process.
        if label in seen:
            return
        seen.add(label)
        try:
            with (directory / "owned_client_helper.log").open("a") as stream:
                stream.write("HELPER_" + label + "\n")
        except OSError:
            pass

    def report_exception(error):
        if isinstance(error, discovery.ClientDiscoveryError):
            reason = error.reason
            if type(reason) is discovery.ClientDiscoveryReason:
                record("REFUSAL_" + reason.name)
        name = type(error).__name__
        if name in {"ValueError", "TypeError", "AttributeError", "AssertionError",
                    "RuntimeError", "KeyError", "OSError", "TimeoutError"}:
            record("EXCEPTION_" + name)
        else:
            record("EXCEPTION_OTHER")

    def wrap(owner, name, label, before=None, after=None):
        original = getattr(owner, name)

        def wrapped(*args, **kwargs):
            record(label + "_ENTERED")
            try:
                if before is not None:
                    try:
                        before(*args, **kwargs)
                    except Exception:
                        record("DIAGNOSTIC_UNAVAILABLE")
                result = original(*args, **kwargs)
                if after is not None:
                    try:
                        after(result)
                    except Exception:
                        record("DIAGNOSTIC_UNAVAILABLE")
            except BaseException as error:
                report_exception(error)
                raise
            record(label + "_RETURNED")
            return result

        setattr(owner, name, wrapped)

    def remember(request):
        active["request"] = request

    def connected(context):
        expected = active["request"]["runtime"]
        record("RAY_VERSION_MATCH_" + str(context.ray_version == "2.58.0").upper())
        version = ".".join(str(expected[k]) for k in
                           ("python_major", "python_minor", "python_patch"))
        record("PYTHON_VERSION_MATCH_" + str(context.python_version == version).upper())

    def corroborating(details, request, *, native_id):
        from ray.dashboard.modules.job.pydantic_models import JobType
        flags = {
            "METADATA_MATCH": details.metadata == discovery._metadata(request),
            "RUNTIME_ENV_MATCH": details.runtime_env == discovery._RUNTIME_ENV,
            "DRIVER_TYPE_MATCH": details.type is JobType.DRIVER,
            "NATIVE_ID_MATCH": details.job_id == native_id,
            "SUBMISSION_ABSENT": details.submission_id is None,
            "DRIVER_INFO_PRESENT": details.driver_info is not None,
            "START_TIME_INTEGER": type(details.start_time) is int,
        }
        if details.driver_info is not None:
            flags["DRIVER_INFO_ID_MATCH"] = details.driver_info.id == native_id
            flags["DRIVER_PID_STRING"] = type(details.driver_info.pid) is str
        for key, value in flags.items():
            record(key + "_" + str(value).upper())

    wrap(discovery, "discover_client_jobs_endpoint", "DISCOVERY", before=remember)
    wrap(ray.client_builder.ClientBuilder, "connect", "CONNECT", after=connected)
    wrap(ray.client_builder.ClientContext, "disconnect", "DISCONNECT")
    wrap(ray, "get_runtime_context", "RUNTIME_CONTEXT")
    wrap(ray.runtime_context.RuntimeContext, "get_job_id", "NATIVE_ID")
    wrap(ray.runtime_context.RuntimeContext, "get_session_name", "SESSION")
    wrap(discovery, "_dashboard_endpoint", "ENDPOINT")
    wrap(discovery, "_driver_details", "DRIVER_HTTP")
    wrap(discovery, "_corroborate", "CORROBORATE", before=corroborating)
    record("INSTALLED")


def install():
    selected = role(getattr(sys, "orig_argv", ()))
    if selected not in {"proxy_module", "specific-server_shim", "specific-server_module",
                        "owned_helper"}:
        return
    directory = Path(__file__).parent / "diagnostics"
    if selected == "owned_helper":
        install_helper(directory)
        return
    if selected.startswith("specific-server_"):
        import faulthandler
        stage = selected.rsplit("_", 1)[1]
        stream = (directory / ("owned_client_" + stage + ".stack")).open("x")
        stream.write("SPECIFIC_" + stage.upper() + "_ENTERED\n")
        stream.flush()
        # Keep the fd alive. Each fixed stage gets one timer, never repeated;
        # CPython caps dumps at 100 threads/100 frames and 500-char strings.
        # Disable Python 3.14 C-stack dumps, which can have unbounded DWARF cost.
        globals()["_stack_stream"] = stream
        options = {"file": stream, "all_threads": False}
        if sys.version_info >= (3, 14):
            options["c_stack"] = False
        faulthandler.enable(**options)
        faulthandler.dump_traceback_later(15, repeat=False, file=stream, exit=False)
        return

    import subprocess
    import threading
    import ray._private.services as services

    def record(label):
        with (directory / "owned_client_process.log").open("a") as stream:
            stream.write(label + "\n")

    original = services.start_ray_process
    recorded = False

    def start(command, process_type, *args, **kwargs):
        nonlocal recorded
        selected_child = role(command) == "specific-server_shim" and not recorded
        if selected_child:
            recorded = True
            # Arm in the already-running proxy before Ray's preexec_fn fork.
            # The child inherits this fatal handler until exec; its later
            # sitecustomize handler cannot diagnose that earlier interval.
            # This adds no timer, retry, signal or altered launch arguments.
            try:
                import faulthandler
                stream = (directory / "owned_client_proxy.stack").open("x")
                options = {"file": stream, "all_threads": False}
                if sys.version_info >= (3, 14):
                    options["c_stack"] = False
                try:
                    faulthandler.enable(**options)
                except BaseException:
                    stream.close()
                    raise
                globals()["_proxy_stack_stream"] = stream
                stream.write("PROXY_FATAL_HANDLER_INSTALLED\n")
                stream.flush()
            except Exception:
                record("PROXY_FATAL_HANDLER_UNAVAILABLE")
        process_info = original(command, process_type, *args, **kwargs)
        if not selected_child:
            return process_info
        record("SPECIFIC_CHILD_STARTED")

        def observe_exit():
            try:
                code = process_info.process.wait(timeout=35)
                record(exit_kind(code))
            except subprocess.TimeoutExpired:
                record("CHILD_RUNNING_AT_35_SECONDS")
            except BaseException:
                record("EXIT_UNKNOWN")

        threading.Thread(target=observe_exit, daemon=True,
                         name="owned-client-exit-diagnostic").start()
        return process_info

    services.start_ray_process = start
    record("PROXY_DIAGNOSTIC_INSTALLED")


if __name__ == "sitecustomize":
    try:
        install()
    except BaseException:
        # Never print sitecustomize's default exception, paths or environment.
        pass
'''


def _owned_client_diagnostic_namespace(directory):
    namespace = {
        "__name__": "owned_client_diagnostic",
        "__file__": str(directory / "sitecustomize.py"),
    }
    exec(compile(_OWNED_CLIENT_DIAGNOSTIC_SOURCE, "owned-client-diagnostic", "exec"), namespace)
    return namespace


def _install_owned_client_diagnostics(monkeypatch, directory):
    from ray._private import services

    from django_ray.runner import cohort_process

    directory.mkdir()
    (directory / "diagnostics").mkdir()
    (directory / "sitecustomize.py").write_text(_OWNED_CLIENT_DIAGNOSTIC_SOURCE, encoding="utf-8")
    diagnostic = _owned_client_diagnostic_namespace(directory)
    original = services.start_ray_process

    def start(command, process_type, *args, **kwargs):
        if diagnostic["role"](command) == "proxy_shim":
            updates = dict(kwargs.get("env_updates") or {})
            previous = updates.get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
            updates["PYTHONPATH"] = str(directory) + (os.pathsep + previous if previous else "")
            kwargs["env_updates"] = updates
        return original(command, process_type, *args, **kwargs)

    monkeypatch.setattr(services, "start_ray_process", start)
    original_popen = cohort_process.subprocess.Popen

    def popen(command, *args, **kwargs):
        if diagnostic["role"](command) == "owned_helper":
            environment = dict(kwargs.get("env") or os.environ)
            previous = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = str(directory) + (os.pathsep + previous if previous else "")
            kwargs["env"] = environment
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(cohort_process.subprocess, "Popen", popen)
    return directory / "diagnostics"


def _owned_client_log_markers(directory, diagnostic_directory=None):
    """Return fixed classifications, never log content or runtime identifiers."""
    markers = {
        "proxy_connection": "New data connection from client",
        "runtime_env_requested": "Increasing runtime env reference",
        "runtime_env_creating": "Creating runtime env:",
        "runtime_env_created": "Successfully created runtime env:",
        "specific_server_started": "SpecificServer started on port:",
        "specific_server_failed": "SpecificServer startup failed",
        "native_init_failed": "Running Ray Init failed:",
        "driver_connected": "Connected to Ray cluster",
        "driver_disconnected": "Disconnecting from the Ray cluster",
        "connection_refused": "ConnectionRefusedError",
        "module_unavailable": "ModuleNotFoundError",
        "runtime_env_timeout": "GetOrCreateRuntimeEnv request failed",
        "grpc_unavailable": "StatusCode.UNAVAILABLE",
        "traceback_present": "Traceback (most recent call last)",
        "grpc_channel_timeout": "grpc.FutureTimeoutError",
        "proxy_diagnostic_installed": "PROXY_DIAGNOSTIC_INSTALLED",
        "proxy_fatal_handler_installed": "PROXY_FATAL_HANDLER_INSTALLED",
        "proxy_fatal_handler_unavailable": "PROXY_FATAL_HANDLER_UNAVAILABLE",
        "specific_child_started": "SPECIFIC_CHILD_STARTED",
        "specific_shim_entered": "SPECIFIC_SHIM_ENTERED",
        "specific_module_entered": "SPECIFIC_MODULE_ENTERED",
        "faulthandler_timeout": "Timeout (0:00:15)",
        "child_running_at_deadline": "CHILD_RUNNING_AT_35_SECONDS",
        "helper_installed": "HELPER_INSTALLED",
        "helper_diagnostic_unavailable": "HELPER_DIAGNOSTIC_UNAVAILABLE",
        **{
            "helper_" + stage.lower() + "_" + phase.lower(): "HELPER_" + stage + "_" + phase
            for stage in (
                "DISCOVERY",
                "CONNECT",
                "DISCONNECT",
                "RUNTIME_CONTEXT",
                "NATIVE_ID",
                "SESSION",
                "ENDPOINT",
                "DRIVER_HTTP",
                "CORROBORATE",
            )
            for phase in ("ENTERED", "RETURNED")
        },
        **{
            "helper_" + flag.lower() + "_" + value.lower(): "HELPER_" + flag + "_" + value
            for flag in (
                "RAY_VERSION_MATCH",
                "PYTHON_VERSION_MATCH",
                "METADATA_MATCH",
                "RUNTIME_ENV_MATCH",
                "DRIVER_TYPE_MATCH",
                "NATIVE_ID_MATCH",
                "SUBMISSION_ABSENT",
                "DRIVER_INFO_PRESENT",
                "START_TIME_INTEGER",
                "DRIVER_INFO_ID_MATCH",
                "DRIVER_PID_STRING",
            )
            for value in ("TRUE", "FALSE")
        },
        **{
            "helper_refusal_" + reason.name.lower(): "HELPER_REFUSAL_" + reason.name
            for reason in discovery.ClientDiscoveryReason
        },
        **{
            "helper_exception_" + name: "HELPER_EXCEPTION_" + name
            for name in (
                "ValueError",
                "TypeError",
                "AttributeError",
                "AssertionError",
                "RuntimeError",
                "KeyError",
                "OSError",
                "TimeoutError",
                "OTHER",
            )
        },
        **{
            f"child_{name.lower()}": name
            for name in (
                "EXIT_ZERO",
                "EXIT_ONE",
                "EXIT_TWO",
                "EXEC_PERMISSION",
                "EXEC_NOT_FOUND",
                "SIGNAL_ABORT",
                "SIGNAL_KILL",
                "SIGNAL_SEGV",
                "SIGNAL_TERM",
                "SIGNAL_HUP",
                "EXIT_OTHER",
                "SIGNAL_OTHER",
                "EXIT_UNKNOWN",
            )
        },
    }
    # Only recognize fixed classes and source locations. Exception messages,
    # traceback paths, function names and runtime identifiers never leave the
    # owned log directory, including when a class is outside this allowlist.
    exceptions = (
        "AssertionError",
        "AttributeError",
        "FileNotFoundError",
        "ImportError",
        "IndexError",
        "KeyError",
        "MemoryError",
        "ModuleNotFoundError",
        "NotImplementedError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "SystemError",
        "SystemExit",
        "TimeoutError",
        "TypeError",
        "ValueError",
    )
    frames = {
        "setup_worker": "_private/workers/setup_worker.py",
        "runtime_env_exec": "_private/runtime_env/context.py",
        "client_module": "util/client/server/__main__.py",
        "client_server": "util/client/server/server.py",
        "client_proxy": "util/client/server/proxier.py",
        "grpc_server": "_private/grpc_utils.py",
        "grpc_port": "_common/tls_utils.py",
        "node": "_private/node.py",
        "core_init": "_private/worker.py",
        "parameters": "_private/parameter.py",
        "job_config": "job_config.py",
        "auth_token": "_private/authentication/authentication_token_setup.py",
        "auth_server": "_private/authentication/grpc_authentication_server_interceptor.py",
        "ray_import": "__init__.py",
    }
    # Exact upstream gRPC signatures, with only bounded numeric logging fields.
    # Emit booleans, not C++ diagnostics, descriptors, timestamps, or process IDs.
    grpc_prefix = (
        r"^[ \t]*(?:[IWEF](?:[0-9]{4} [0-9]{2}:[0-9]{2}:"
        r"[0-9]{2,12}\.[0-9]{1,9}[ \t]+[0-9]{1,10})?[ \t]+)?"
    )
    classifications = {
        "grpc_epoll1_worker_kicked": (
            grpc_prefix + r"ev_epoll1_linux\.cc:[0-9]{1,6}\] "
            r"Check failed: next_worker->state == KICKED[ \t]*\r?$"
        ),
        "grpc_fork_handlers_skipped": (
            grpc_prefix + r"fork_posix\.cc:[0-9]{1,6}\] "
            r"Other threads are currently calling into gRPC, skipping fork\(\) handlers[ \t]*\r?$"
        ),
        "grpc_poll_inherited_fd": (
            grpc_prefix + r"ev_poll_posix\.cc:[0-9]{1,6}\] "
            r"FD from fork parent still in poll list: fd\([0-9]{1,10}, "
            r"generation: [0-9]{1,10}\)[ \t]*\r?$"
        ),
        **{f"exception_{name}": rf"^\s*(?:builtins\.)?{name}(?::|$)" for name in exceptions},
        **{
            f"frame_{label}": rf'^\s*File "[^"\r\n]*/ray/{re.escape(path)}", line [0-9]+(?:,| in )'
            for label, path in frames.items()
        },
    }
    # The proxy owns this stream and its pre-exec child inherits it. These
    # observations localize frames; they do not identify a crash's root cause.
    proxy_classifications = {
        "proxy_fatal_segv": r"^Fatal Python error: Segmentation fault\s*$",
        "proxy_fatal_abort": r"^Fatal Python error: Aborted\s*$",
        "proxy_frame_subprocess": (
            r'^\s*File "[^"\r\n]*/subprocess\.py", line [0-9]+ in _execute_child\s*$'
        ),
        "proxy_frame_preexec": (
            r'^\s*File "[^"\r\n]*/ray/_private/services\.py", line [0-9]+ in preexec_fn\s*$'
        ),
    }
    result = dict.fromkeys(markers, False)
    result.update(dict.fromkeys(classifications, False))
    result.update(dict.fromkeys(proxy_classifications, False))
    result.update(files_read=0, read_failed=False, truncated=False)
    paths = sorted(
        path
        for pattern in ("ray_client_server*", "runtime_env_agent*", "python-core-driver*")
        for path in Path(directory).glob(pattern)
        if path.is_file() and not path.is_symlink()
    )
    if diagnostic_directory is not None:
        paths = sorted(
            [
                *paths,
                *(
                    path
                    for path in Path(diagnostic_directory).glob("owned_client_*")
                    if path.is_file() and not path.is_symlink()
                ),
            ]
        )
    result["truncated"] = len(paths) > 16
    for path in paths[:16]:
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - 65536))
                body = stream.read(65536).decode("utf-8", errors="replace")
            result["files_read"] += 1
            result["truncated"] |= size > 65536
            for key, marker in markers.items():
                result[key] |= marker in body
            for key, pattern in classifications.items():
                result[key] |= re.search(pattern, body, flags=re.MULTILINE) is not None
            if (
                diagnostic_directory is not None
                and path == Path(diagnostic_directory) / "owned_client_proxy.stack"
            ):
                for key, pattern in proxy_classifications.items():
                    result[key] |= re.search(pattern, body, flags=re.MULTILINE) is not None
        except OSError:
            result["read_failed"] = True
    return result


_FATAL_TRACE_BYTES = 16384
_FATAL_TRACE_FRAMES = 16
_FATAL_TRACE_FILES = (
    "subprocess.py",
    "threading.py",
    "runpy.py",
    "site.py",
    "importlib/__init__.py",
    "importlib/_bootstrap.py",
    "importlib/_bootstrap_external.py",
    "ctypes/__init__.py",
    "ray/__init__.py",
    "ray/_private/services.py",
    "ray/_private/utils.py",
    "ray/_private/node.py",
    "ray/_private/worker.py",
    "ray/_private/workers/setup_worker.py",
    "ray/_private/runtime_env/context.py",
    "ray/util/client/server/__main__.py",
    "ray/util/client/server/server.py",
    "ray/util/client/server/proxier.py",
    "grpc/__init__.py",
    "grpc/_channel.py",
    "grpc/_server.py",
    "grpc/_common.py",
    "grpc/_cython/cygrpc.pyx",
)


def _parse_owned_client_fatal_trace(body):
    """Keep only a fatal header and reviewed framework frames, never raw lines."""
    result = {"header": None, "frames": [], "truncated": len(body) > _FATAL_TRACE_BYTES}
    for line in body[:_FATAL_TRACE_BYTES].decode("utf-8", errors="replace").splitlines():
        if result["header"] is None:
            if re.fullmatch(
                r"Fatal Python error: (?:Aborted|Segmentation fault|Bus error|"
                r"Illegal instruction|Floating point exception)",
                line,
            ):
                result["header"] = line
            continue
        frame = re.fullmatch(
            r'\s*File "([^"\r\n]{1,1024})", line ([1-9][0-9]{0,6})(?:,)? in '
            r"([A-Za-z_][A-Za-z_0-9]{0,63}|<(?:module|listcomp|dictcomp|setcomp|genexpr)>)",
            line,
        )
        if frame is None:
            continue
        path, number, function = frame.groups()
        # Never retain an absolute path, arbitrary package name, exception
        # message, thread address, C frame, environment, argv or request value.
        path = path.replace("\\", "/")
        if path in {"<frozen importlib._bootstrap>", "<frozen importlib._bootstrap_external>"}:
            source = path[8:-1].replace(".", "/") + ".py"
        else:
            source = next(
                (name for name in _FATAL_TRACE_FILES if path == name or path.endswith("/" + name)),
                None,
            )
        if source is None or ".." in path.split("/"):
            continue
        if len(result["frames"]) == _FATAL_TRACE_FRAMES:
            result["truncated"] = True
            break
        result["frames"].append({"file": source, "line": int(number), "function": function})
    return result


def _owned_client_fatal_traces(diagnostic_directory):
    """Read only the three streams created for this fixture's one owned child.

    The proxy stream can be inherited before exec; its label identifies the
    stream, not which process faulted. No PID or crash cause is inferred.
    """
    result = {"streams": {}, "read_failed": False}
    if diagnostic_directory is None:
        return result
    for role in ("proxy", "shim", "module"):
        path = Path(diagnostic_directory) / f"owned_client_{role}.stack"
        try:
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as stream:
                trace = _parse_owned_client_fatal_trace(stream.read(_FATAL_TRACE_BYTES + 1))
            if trace["header"] is not None or trace["truncated"]:
                result["streams"][role] = trace
        except OSError:
            result["read_failed"] = True
    return result


def _owned_client_failure_evidence(directory, diagnostic_directory):
    # Diagnostics must not replace the original failure or prevent node reaping.
    try:
        return {
            "markers": _owned_client_log_markers(directory, diagnostic_directory),
            "fatal_traces": _owned_client_fatal_traces(diagnostic_directory),
        }
    except Exception:
        return {"diagnostic_failed": True}


def _assert_owned_client_helper_succeeded(node, command, reason):
    if reason is not None:
        raise AssertionError(
            f"Owned {command} helper failed: {reason}; "
            f"readiness={json.dumps(node.readiness, sort_keys=True)}; "
            f"ray_evidence={json.dumps(node.diagnostics(), sort_keys=True)}"
        )


def _client_proxy_ready(port, timeout):
    import grpc
    from ray._private.authentication.http_token_authentication import (
        get_auth_headers_if_auth_enabled,
    )
    from ray.core.generated import ray_client_pb2, ray_client_pb2_grpc

    # PING is answered by the proxy without creating a native DRIVER.
    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        response = ray_client_pb2_grpc.RayletDriverStub(channel).ClusterInfo(
            ray_client_pb2.ClusterInfoRequest(type=ray_client_pb2.ClusterInfoType.PING),
            timeout=timeout,
            metadata=[
                (key.lower(), value) for key, value in get_auth_headers_if_auth_enabled({}).items()
            ],
        )
    return response.json == "{}"


def _runtime_agent_ready(port, timeout, *, host="127.0.0.1"):
    from urllib.request import ProxyHandler, Request, build_opener

    from ray._private.authentication.http_token_authentication import (
        get_auth_headers_if_auth_enabled,
    )
    from ray.core.generated import runtime_env_agent_pb2

    # Read-only protobuf route; limit=0 returns no RuntimeEnv definitions.
    headers = {"Content-Type": "application/octet-stream"}
    headers.update(get_auth_headers_if_auth_enabled(headers))
    request = Request(
        f"http://{host}:{port}/get_runtime_envs_info",
        data=runtime_env_agent_pb2.GetRuntimeEnvsInfoRequest(limit=0).SerializeToString(),
        headers=headers,
        method="POST",
    )
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        if response.status != 200:
            return False
        body = response.read(1025)
    if len(body) > 1024:
        return False
    reply = runtime_env_agent_pb2.GetRuntimeEnvsInfoReply()
    reply.ParseFromString(body)
    return not reply.runtime_env_states and reply.total >= 0


def _wait_for_owned_client_services(node, client_port, runtime_port, *, runtime_host="127.0.0.1"):
    # Node construction waits for GCS/raylet, but Client PING and the HTTP
    # runtime agent start independently. Retrying these reads creates no driver
    # and prevents Ray's unbounded initial runtime-env request racing startup.
    deadline = time.monotonic() + 10
    ready = {"proxy": False, "runtime_agent": False, "owned_processes_alive": False}
    checks = {
        "proxy": (_client_proxy_ready, client_port),
        "runtime_agent": (
            lambda port, timeout: _runtime_agent_ready(port, timeout, host=runtime_host),
            runtime_port,
        ),
    }
    while time.monotonic() < deadline:
        ready["owned_processes_alive"] = node.remaining_processes_alive()
        if not ready["owned_processes_alive"]:
            break
        for name, (check, port) in checks.items():
            remaining = deadline - time.monotonic()
            if ready[name] or remaining <= 0:
                continue
            try:
                ready[name] = check(port, min(0.5, remaining)) is True
            except Exception:
                ready[name] = False
        if all(ready.values()):
            return ready
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise AssertionError(
        "Owned Client service readiness failed: " + json.dumps(ready, sort_keys=True)
    )


def test_client_log_diagnostics_are_bounded_fixed_classifications(tmp_path):
    (tmp_path / "ray_client_server_123.err").write_text(
        "secret-token=never-print\nNew data connection from client private-id\n"
        "ModuleNotFoundError: private-module\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.log").write_text("Running Ray Init failed:", encoding="utf-8")
    result = _owned_client_log_markers(tmp_path)
    assert result["proxy_connection"] and result["module_unavailable"]
    assert not result["native_init_failed"]
    assert result["files_read"] == 1
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)
    (tmp_path / "runtime_env_agent.log").write_bytes(b"x" * 65536 + b"Creating runtime env:")
    assert _owned_client_log_markers(tmp_path)["truncated"] is True


def test_client_traceback_classifications_never_return_messages_or_paths(tmp_path):
    (tmp_path / "ray_client_server_private.err").write_text(
        "Traceback (most recent call last):\n"
        '  File "/private-root/ray/_private/workers/setup_worker.py", line 36, in <module>\n'
        '  File "/private-root/ray/_private/runtime_env/context.py", line 103, in exec_worker\n'
        "TypeError: secret-token and private-runtime-value\n"
        '  File "/private-root/ray/util/client/server/server.py", line 793, in serve\n'
        "RuntimeError: another-private-detail\n",
        encoding="utf-8",
    )
    result = _owned_client_log_markers(tmp_path)
    assert result["exception_TypeError"] and result["exception_RuntimeError"]
    assert result["frame_setup_worker"] and result["frame_runtime_env_exec"]
    assert result["frame_client_server"]
    assert not result["frame_core_init"] and not result["exception_MemoryError"]
    encoded = json.dumps(result)
    assert all(value not in encoded for value in ("private", "secret", "793", "exec_worker"))
    assert all(type(value) in {bool, int} for value in result.values())


def test_client_traceback_classifications_ignore_unknown_or_embedded_content(tmp_path):
    (tmp_path / "ray_client_server_123.err").write_text(
        "PrivateCustomerError: do-not-return\n"
        "logged message contains TypeError: private detail\n"
        'logged message contains File "/ray/_private/worker.py", line 100, in init\n'
        '  File "/private-root/unrelated.py", line 4, in private_function\n',
        encoding="utf-8",
    )
    result = _owned_client_log_markers(tmp_path)
    assert not any(
        value for key, value in result.items() if key.startswith(("exception_", "frame_"))
    )
    assert "PrivateCustomer" not in json.dumps(result)


def _diagnostic_command(mode, *, shim=False):
    return [
        "python",
        *(["/private/ray/_private/workers/setup_worker.py"] if shim else []),
        "-m",
        "ray.util.client.server",
        f"--mode={mode}",
    ]


@pytest.mark.parametrize("mode", ["proxy", "specific-server"])
@pytest.mark.parametrize("shim", [False, True])
def test_diagnostic_role_accepts_only_fixed_ray_server_stages(tmp_path, mode, shim):
    script = _owned_client_diagnostic_namespace(tmp_path)
    assert script["role"](_diagnostic_command(mode, shim=shim)) == mode + (
        "_shim" if shim else "_module"
    )
    assert script["role"](["python", "-m", "django_ray.runner.cohort_job_control"]) is None
    assert script["role"](["python", "-m", "application", "--mode=" + mode]) is None
    assert script["role"](_diagnostic_command(mode) + ["--mode=" + mode]) is None
    assert script["role"](["python", "/private/other.py", *_diagnostic_command(mode)[1:]]) is None


def test_diagnostic_injection_changes_only_owned_proxy_child_environment(tmp_path, monkeypatch):
    from ray._private import services

    calls = []
    monkeypatch.setattr(
        services, "start_ray_process", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setenv("PYTHONPATH", "existing-source-path")
    before = dict(os.environ)
    directory = tmp_path / "diagnostic"
    _install_owned_client_diagnostics(monkeypatch, directory)
    original_updates = {"UNCHANGED": "private"}
    services.start_ray_process(
        _diagnostic_command("proxy", shim=True),
        "ray_client_server",
        fate_share=False,
        env_updates=original_updates,
    )
    services.start_ray_process(
        ["raylet", "--owned"], "raylet", fate_share=False, env_updates=original_updates
    )
    assert calls[0][1]["env_updates"] == {
        "UNCHANGED": "private",
        "PYTHONPATH": str(directory) + os.pathsep + "existing-source-path",
    }
    assert calls[1][1]["env_updates"] is original_updates
    assert original_updates == {"UNCHANGED": "private"} and dict(os.environ) == before


def test_helper_diagnostic_injection_preserves_fixed_command_and_parent_environment(
    tmp_path, monkeypatch
):
    from django_ray.runner import cohort_process

    calls = []
    monkeypatch.setattr(
        cohort_process.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setenv("PYTHONPATH", "existing-source-path")
    before = dict(os.environ)
    directory = tmp_path / "diagnostic"
    _install_owned_client_diagnostics(monkeypatch, directory)
    fixed = [sys.executable, "-m", cohort_process._HELPER_MODULE, "/owned/request-directory"]
    environment = {"UNCHANGED": "private", "PYTHONPATH": "explicit-source-path"}
    cohort_process.subprocess.Popen(fixed, env=environment, stdout=-3, stderr=-3)
    other = [sys.executable, "-m", "application", "/owned/request-directory"]
    cohort_process.subprocess.Popen(other, env=environment)
    assert calls[0][0] == (fixed,)
    assert calls[0][1]["stdout"] == calls[0][1]["stderr"] == -3
    assert calls[0][1]["env"] == {
        "UNCHANGED": "private",
        "PYTHONPATH": str(directory) + os.pathsep + "explicit-source-path",
    }
    assert calls[1][0] == (other,) and calls[1][1]["env"] is environment
    assert environment == {"UNCHANGED": "private", "PYTHONPATH": "explicit-source-path"}
    assert dict(os.environ) == before
    role = _owned_client_diagnostic_namespace(directory)["role"]
    assert role(fixed) == "owned_helper"
    assert role(fixed + ["--untrusted"]) is None


@pytest.mark.parametrize("outcome", ["success", "metadata", "runtime_env", "exception"])
def test_helper_stage_diagnostics_preserve_result_and_hide_private_values(
    owned_client, tmp_path, monkeypatch, outcome
):
    import ray.runtime_context

    script = _owned_client_diagnostic_namespace(tmp_path)
    directory = tmp_path / "diagnostics"
    directory.mkdir()
    # Register restoration before the test-only installer wraps these functions.
    for owner, names in (
        (
            discovery,
            (
                "discover_client_jobs_endpoint",
                "_dashboard_endpoint",
                "_driver_details",
                "_corroborate",
            ),
        ),
        (ray, ("get_runtime_context",)),
        (ray.client_builder.ClientBuilder, ("connect",)),
        (ray.client_builder.ClientContext, ("disconnect",)),
        (ray.runtime_context.RuntimeContext, ("get_job_id", "get_session_name")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, getattr(owner, name))
    if outcome == "metadata":
        owned_client.details.metadata = {"private-token": "never-export-this"}
    elif outcome == "runtime_env":
        owned_client.details.runtime_env = {"working_dir": "/never-export-this"}
    elif outcome == "exception":
        owned_client.connect_error = True
    script["install_helper"](directory)
    if outcome == "success":
        observed = discovery.discover_client_jobs_endpoint(request())
        assert observed["native_job_id"] == NATIVE_ID
    else:
        with pytest.raises(discovery.ClientDiscoveryError) as error:
            discovery.discover_client_jobs_endpoint(request())
        expected = (
            discovery.ClientDiscoveryReason.DISCOVERY_UNCONFIRMED
            if outcome == "exception"
            else discovery.ClientDiscoveryReason.RESPONSE_MISMATCH
        )
        assert error.value.reason is expected
    markers = _owned_client_log_markers(tmp_path, directory)
    assert markers["helper_installed"] and markers["helper_discovery_entered"]
    assert markers["helper_connect_returned"] is (outcome != "exception")
    if outcome in {"metadata", "runtime_env"}:
        assert markers["helper_" + outcome + "_match_false"]
        assert markers["helper_refusal_response_mismatch"]
    if outcome == "success":
        assert markers["helper_corroborate_returned"] and markers["helper_discovery_returned"]
    body = (directory / "owned_client_helper.log").read_text()
    assert "private" not in body and "never-export-this" not in body
    assert all(re.fullmatch(r"HELPER_[A-Z_a-z]+", line) for line in body.splitlines())
    assert len(body) < 4096 and all(type(value) in {bool, int} for value in markers.values())


@pytest.mark.parametrize("shim", [False, True])
def test_specific_stage_arms_one_nonfatal_nonrepeating_stack_dump(tmp_path, monkeypatch, shim):
    script = _owned_client_diagnostic_namespace(tmp_path)
    (tmp_path / "diagnostics").mkdir()
    script["sys"] = SimpleNamespace(
        orig_argv=_diagnostic_command("specific-server", shim=shim), version_info=(3, 14)
    )
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "faulthandler",
        SimpleNamespace(
            enable=lambda **kwargs: calls.append(("enable", kwargs)),
            dump_traceback_later=lambda *args, **kwargs: calls.append((args, kwargs)),
        ),
    )
    script["install"]()
    try:
        assert calls[0][1]["all_threads"] is False and calls[0][1]["c_stack"] is False
        assert calls[1][0] == (15,)
        assert calls[1][1]["repeat"] is False and calls[1][1]["exit"] is False
        assert calls[0][1]["file"] is calls[1][1]["file"] is script["_stack_stream"]
    finally:
        script["_stack_stream"].close()


@pytest.mark.parametrize("outcome", [-9, -11, 127, "timeout"])
@pytest.mark.parametrize("python_version", [(3, 12), (3, 14)])
def test_proxy_diagnostic_observes_exact_child_without_stopping_or_retrying(
    tmp_path, monkeypatch, outcome, python_version
):
    import subprocess
    import threading

    from ray._private import services

    script = _owned_client_diagnostic_namespace(tmp_path)
    directory = tmp_path / "diagnostics"
    directory.mkdir()
    script["sys"] = SimpleNamespace(
        orig_argv=_diagnostic_command("proxy"), version_info=python_version
    )
    waits, threads, starts, handlers = [], [], [], []

    def enable(**kwargs):
        assert not starts and not threads
        handlers.append(kwargs)

    monkeypatch.setitem(sys.modules, "faulthandler", SimpleNamespace(enable=enable))

    def wait(*, timeout):
        waits.append(timeout)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("private-command", timeout)
        return outcome

    info = SimpleNamespace(process=SimpleNamespace(wait=wait))
    monkeypatch.setattr(
        services, "start_ray_process", lambda *args, **kwargs: starts.append(args) or info
    )
    monkeypatch.setattr(
        threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: threads.append(kwargs))
    )
    script["install"]()
    assert not handlers
    command = _diagnostic_command("specific-server", shim=True)
    try:
        assert services.start_ray_process(command, "ray_client_server", fate_share=False) is info
        assert len(handlers) == 1
        assert handlers[0]["file"] is script["_proxy_stack_stream"]
        assert handlers[0]["all_threads"] is False
        assert handlers[0].get("c_stack") is (False if python_version >= (3, 14) else None)
        assert starts == [(command, "ray_client_server")]
    finally:
        if "_proxy_stack_stream" in script:
            script["_proxy_stack_stream"].close()
    assert len(starts) == len(threads) == 1 and not waits
    assert threads[0]["daemon"] is True
    threads[0]["target"]()
    assert waits == [35] and len(starts) == 1
    (directory / "owned_client_module.stack").write_text(
        "SPECIFIC_MODULE_ENTERED\nTimeout (0:00:15)!\n"
        '  File "/private-directory/ray/__init__.py", line 123 in private_function\n',
        encoding="utf-8",
    )
    marker = {
        -9: "child_signal_kill",
        -11: "child_signal_segv",
        127: "child_exec_not_found",
        "timeout": "child_running_at_deadline",
    }[outcome]
    result = _owned_client_log_markers(tmp_path, directory)
    assert result[marker] and result["frame_ray_import"] and result["specific_module_entered"]
    assert result["proxy_diagnostic_installed"] and result["specific_child_started"]
    assert result["proxy_fatal_handler_installed"] and not result["proxy_fatal_handler_unavailable"]
    assert "private" not in json.dumps(result) and "123" not in json.dumps(result)
    assert all(type(value) in {bool, int} for value in result.values())


def test_proxy_fatal_handler_failure_does_not_change_or_retry_owned_launch(tmp_path, monkeypatch):
    import threading

    from ray._private import services

    script = _owned_client_diagnostic_namespace(tmp_path)
    directory = tmp_path / "diagnostics"
    directory.mkdir()
    script["sys"] = SimpleNamespace(orig_argv=_diagnostic_command("proxy"), version_info=(3, 14))
    streams, launches, watchers = [], [], []

    def unavailable(**kwargs):
        streams.append(kwargs["file"])
        raise OSError("private provider diagnostic must not escape")

    info = SimpleNamespace(process=object())
    monkeypatch.setitem(sys.modules, "faulthandler", SimpleNamespace(enable=unavailable))
    monkeypatch.setattr(
        services,
        "start_ray_process",
        lambda *args, **kwargs: launches.append((args, kwargs)) or info,
    )
    monkeypatch.setattr(
        threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: watchers.append(kwargs))
    )
    script["install"]()
    command = _diagnostic_command("specific-server", shim=True)
    assert services.start_ray_process(command, "ray_client_server", fate_share=True) is info
    assert launches == [((command, "ray_client_server"), {"fate_share": True})]
    assert len(watchers) == len(streams) == 1 and streams[0].closed
    result = _owned_client_log_markers(tmp_path, directory)
    assert result["proxy_fatal_handler_unavailable"] and not result["proxy_fatal_handler_installed"]
    assert result["specific_child_started"]
    assert "private" not in json.dumps(result)
    assert "private" not in (directory / "owned_client_process.log").read_text()


@pytest.mark.parametrize("origin", ["proxy", "other", "embedded"])
def test_proxy_fatal_classification_is_bounded_and_scoped_to_owned_stream(tmp_path, origin):
    directory = tmp_path / "diagnostics"
    directory.mkdir()
    name = "owned_client_module.stack" if origin == "other" else "owned_client_proxy.stack"
    lines = [
        "Fatal Python error: Segmentation fault",
        '  File "/private-root/subprocess.py", line 123 in _execute_child',
        '  File "/private-root/ray/_private/services.py", line 456 in preexec_fn',
    ]
    if origin == "embedded":
        lines = ["untrusted message contains " + line for line in lines]
    (directory / name).write_text("\n".join(lines) + "\nsecret-marker", encoding="utf-8")
    result = _owned_client_log_markers(tmp_path, directory)
    for key in ("proxy_fatal_segv", "proxy_frame_subprocess", "proxy_frame_preexec"):
        assert result[key] is (origin == "proxy")
    assert not result["proxy_fatal_abort"]
    assert all(type(value) in {bool, int} for value in result.values())
    assert all(
        value not in json.dumps(result) for value in ("private-root", "secret-marker", "123", "456")
    )
    (directory / name).write_bytes(b"x" * 65536 + b"\nFatal Python error: Aborted\n")
    assert _owned_client_log_markers(tmp_path, directory)["truncated"]


@pytest.mark.parametrize(
    "prefix", ["", "F  ", "E0908 12:34:56.123456 1234 ", "E0000 00:00:1750000000.123456 1234 "]
)
@pytest.mark.parametrize("alteration", ["exact", "embedded", "suffix", "wrong-source"])
def test_upstream_grpc_signatures_emit_only_exact_bounded_classifications(
    tmp_path, prefix, alteration
):
    lines = [
        "ev_epoll1_linux.cc:1125] Check failed: next_worker->state == KICKED",
        "fork_posix.cc:71] Other threads are currently calling into gRPC, skipping fork() handlers",
        "ev_poll_posix.cc:593] FD from fork parent still in poll list: fd(18, generation: 1)",
    ]
    lines = [prefix + line for line in lines]
    if alteration == "embedded":
        lines = ["private-payload " + line for line in lines]
    elif alteration == "suffix":
        lines = [line + " private-payload" for line in lines]
    elif alteration == "wrong-source":
        lines = [line.replace(".cc:", ".py:") for line in lines]
    log = tmp_path / "ray_client_server.err"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _owned_client_log_markers(tmp_path)
    for name in (
        "grpc_epoll1_worker_kicked",
        "grpc_fork_handlers_skipped",
        "grpc_poll_inherited_fd",
    ):
        assert result[name] is (alteration == "exact")
    assert all(type(value) in {bool, int} for value in result.values())
    assert "private-payload" not in json.dumps(result)
    log.write_bytes(log.read_bytes() + b"x" * 65536)
    result = _owned_client_log_markers(tmp_path)
    assert result["truncated"]
    assert not any(
        result[name]
        for name in (
            "grpc_epoll1_worker_kicked",
            "grpc_fork_handlers_skipped",
            "grpc_poll_inherited_fd",
        )
    )


def test_client_readiness_retries_only_read_only_services(monkeypatch):
    now = [0.0]
    calls = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def proxy(port, timeout):
        calls.append(("proxy", port, timeout))
        return True

    def agent(port, timeout, *, host):
        assert host == "192.0.2.1"
        calls.append(("agent", port, timeout))
        return now[0] > 0

    monkeypatch.setattr(sys.modules[__name__], "_client_proxy_ready", proxy)
    monkeypatch.setattr(sys.modules[__name__], "_runtime_agent_ready", agent)
    assert all(
        _wait_for_owned_client_services(
            SimpleNamespace(remaining_processes_alive=lambda: True), 1, 2, runtime_host="192.0.2.1"
        ).values()
    )
    assert [name for name, _, _ in calls] == ["proxy", "agent", "agent"]
    assert all(0 < timeout <= 0.5 for _, _, timeout in calls)


def test_client_readiness_stops_at_deadline_without_creating_a_driver(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(sys.modules[__name__], "_client_proxy_ready", lambda *_args: True)

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("private listener diagnostic")

    monkeypatch.setattr(sys.modules[__name__], "_runtime_agent_ready", unavailable)
    with pytest.raises(AssertionError, match='"runtime_agent": false') as error:
        _wait_for_owned_client_services(
            SimpleNamespace(remaining_processes_alive=lambda: True), 1, 2
        )
    assert now[0] == 10
    assert "private" not in str(error.value)


@pytest.mark.parametrize("header", ["Aborted", "Segmentation fault"])
def test_owned_fatal_trace_keeps_structural_frames_without_private_payload(header):
    body = (
        '  File "/before/ray/_private/services.py", line 1 in ignored_before_fatal\n'
        f"Fatal Python error: {header}\n\n"
        "Current thread 0x12345678 (most recent call first):\n"
        '  File "/private-owner/.venv/lib/python3.12/subprocess.py", line 1883 in _execute_child\n'
        '  File "/private-token/site-packages/ray/_private/services.py", line 1046, in preexec_fn\n'
        '  File "<frozen importlib._bootstrap>", line 488 in _call_with_frames_removed\n'
        '  File "/private-owner/application.py", line 123 in secret_request\n'
        '  File "/private-owner/ray/secret_token.py", line 12 in leak\n'
        '  File "/private-owner/../ray/_private/services.py", line 13 in traversal\n'
        '  File "/private-owner/subprocess.py", line 14 in bad_function=secret\n'
        'message: File "/private-owner/subprocess.py", line 15 in embedded\n'
        "Fatal Python error: unbounded-provider-payload\n"
        "environment TOKEN=secret, argv=request-bytes, exception=private-provider-value\n"
        "Extension modules: application-secret (total: 1)\n"
    ).encode()
    result = _parse_owned_client_fatal_trace(body)
    assert result == {
        "header": f"Fatal Python error: {header}",
        "frames": [
            {"file": "subprocess.py", "line": 1883, "function": "_execute_child"},
            {"file": "ray/_private/services.py", "line": 1046, "function": "preexec_fn"},
            {
                "file": "importlib/_bootstrap.py",
                "line": 488,
                "function": "_call_with_frames_removed",
            },
        ],
        "truncated": False,
    }
    assert all(
        value not in json.dumps(result)
        for value in (
            "private-owner",
            "private-token",
            "secret",
            "request",
            "argv",
            "0x12345678",
            "traversal",
        )
    )


def test_owned_fatal_trace_bounds_input_and_frames_and_requires_exact_fatal_header():
    frame = b'  File "/private/subprocess.py", line 123 in _execute_child\n'
    assert _parse_owned_client_fatal_trace(frame)["frames"] == []
    assert (
        _parse_owned_client_fatal_trace(
            b"untrusted message: Fatal Python error: Aborted\n" + frame
        )["header"]
        is None
    )
    result = _parse_owned_client_fatal_trace(b"Fatal Python error: Aborted\n" + frame * 100)
    assert len(result["frames"]) == _FATAL_TRACE_FRAMES and result["truncated"]
    result = _parse_owned_client_fatal_trace(
        b"x" * _FATAL_TRACE_BYTES + b"\nFatal Python error: Aborted\n" + frame
    )
    assert result == {"header": None, "frames": [], "truncated": True}


def test_owned_fatal_reader_uses_only_bounded_owned_streams(tmp_path, monkeypatch):
    body = b"Fatal Python error: Aborted\n" + b"x" * (_FATAL_TRACE_BYTES * 2)
    (tmp_path / "owned_client_proxy.stack").write_bytes(body)
    (tmp_path / "unrelated.stack").write_bytes(b"Fatal Python error: Segmentation fault\n")
    (tmp_path / "owned_client_helper.log").write_bytes(b"Fatal Python error: Segmentation fault\n")
    reads = []
    original = Path.open

    class BoundedReader:
        def __init__(self, path):
            self.stream = original(path, "rb")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def read(self, size):
            reads.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(Path, "open", lambda path, *_args: BoundedReader(path))
    result = _owned_client_fatal_traces(tmp_path)
    assert reads == [_FATAL_TRACE_BYTES + 1]
    assert result == {
        "streams": {
            "proxy": {"header": "Fatal Python error: Aborted", "frames": [], "truncated": True}
        },
        "read_failed": False,
    }
    assert _owned_client_fatal_traces(None) == {"streams": {}, "read_failed": False}


def test_owned_fatal_reader_refuses_symlink_and_redacts_read_failure(tmp_path, monkeypatch):
    for role in ("proxy", "shim"):
        (tmp_path / f"owned_client_{role}.stack").write_bytes(b"Fatal Python error: Aborted\n")
    original = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: path.name == "owned_client_proxy.stack" or original(path)
    )
    opened = []

    def unavailable(path, *_args):
        opened.append(path.name)
        raise OSError("private provider detail")

    monkeypatch.setattr(Path, "open", unavailable)
    assert _owned_client_fatal_traces(tmp_path) == {"streams": {}, "read_failed": True}
    assert opened == ["owned_client_shim.stack"]


@pytest.mark.parametrize("diagnostic_failure", [False, True])
def test_owned_helper_failure_retains_evidence_before_cleanup_and_preserves_reason(
    tmp_path, monkeypatch, diagnostic_failure
):
    reads = []
    original = _owned_client_fatal_traces

    def read(directory):
        assert directory.is_dir()
        reads.append(directory)
        if diagnostic_failure:
            raise RuntimeError("private diagnostic failure")
        return original(directory)

    monkeypatch.setattr(sys.modules[__name__], "_owned_client_fatal_traces", read)
    with pytest.raises(AssertionError, match="helper failed: deadline") as error:
        with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
            owned = Path(directory)
            (owned / "owned_client_proxy.stack").write_text(
                'Fatal Python error: Aborted\n  File "/private/subprocess.py", line 123 in _execute_child\n'
            )
            node = SimpleNamespace(
                readiness={"owned_processes_alive": True},
                diagnostics=lambda: _owned_client_failure_evidence(owned, owned),
            )
            _assert_owned_client_helper_succeeded(node, "discover-client", "deadline")
    assert reads == [owned] and not owned.exists()
    assert "private" not in str(error.value)
    if diagnostic_failure:
        assert '"diagnostic_failed": true' in str(error.value)
    else:
        assert '"header": "Fatal Python error: Aborted"' in str(error.value)
        assert '"function": "_execute_child"' in str(error.value)


def _configure_owned_client_network(monkeypatch, client_port):
    from ray._private import services

    # A fresh Ray driver canonicalizes loopback GCS addresses to this interface.
    # Advertising 127.0.0.1 via private Node instead binds GCS only to loopback,
    # so the Client server's later ray.init waits on a different address.
    node_ip = services.resolve_ip_for_localhost("127.0.0.1")
    address = ipaddress.ip_address(node_ip)
    assert isinstance(address, ipaddress.IPv4Address) and not (
        address.is_loopback or address.is_unspecified or address.is_multicast
    ), "Owned native Client fixture requires a concrete host IPv4 address"
    original = services.start_ray_client_server

    def start(gcs_address, advertised_ip, port, *args, **kwargs):
        assert advertised_ip == node_ip and port == client_port
        assert gcs_address.startswith(node_ip + ":")
        assert kwargs.get("server_type", "proxy") == "proxy"
        # Pin only this owned listener; retain the real advertised GCS address
        # and runtime-agent address passed by Node for its child processes.
        return original(gcs_address, "127.0.0.1", port, *args, **kwargs)

    monkeypatch.setattr(services, "start_ray_client_server", start)
    return node_ip


def test_owned_node_advertisement_survives_fresh_driver_canonicalization(monkeypatch):
    from ray._private import services

    calls = []
    monkeypatch.setattr(services, "get_node_ip_address", lambda: "192.0.2.1")
    monkeypatch.setattr(
        services,
        "start_ray_client_server",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    advertised = _configure_owned_client_network(monkeypatch, 12345)
    # This is the actual pinned Ray canonicalizer used before connect-only Node.
    assert services.canonicalize_bootstrap_address("127.0.0.1:23456") != "127.0.0.1:23456"
    gcs_address = advertised + ":23456"
    assert services.canonicalize_bootstrap_address(gcs_address) == gcs_address
    services.start_ray_client_server(
        gcs_address, advertised, 12345, runtime_env_agent_address="http://192.0.2.1:34567"
    )
    assert calls == [
        (
            (gcs_address, "127.0.0.1", 12345),
            {"runtime_env_agent_address": "http://192.0.2.1:34567"},
        )
    ]


@pytest.fixture(params=(False, True), ids=("stock", "instrumented"))
def owned_client_ray_node(request, monkeypatch):
    """One unconnected host head with loopback Dashboard/Client access.

    CPU/worker memory are logical Ray resources, not operating-system limits.
    The admitted Linux native runner supplies VM and enclosing time bounds.
    Explicit runtime-agent port is required because Ray 2.58 starts the Client
    proxy before the raylet can report a dynamically allocated agent port.
    GCS/Raylet use Ray's native host advertisement and all-interface listeners;
    the admitted disposable Linux VM is the isolation boundary. The runtime
    agent listens at the advertised host IP. No shared cluster is attached.

    Each mandatory variant owns and reaps a separate node in the serial native
    lane. Stock launches install no diagnostic sitecustomize or launch wrappers;
    only existing owned Ray logs are read. The other variant retains the bounded
    instrumentation. One outcome pair does not establish a deterministic cause.
    """
    from scripts.require_linux import require_linux

    assert request.node.get_closest_marker("real_ray") is not None
    require_linux()
    assert ray.is_initialized() is False
    from ray._private.node import Node
    from ray._private.parameter import RayParams

    # First allocate only local public access ports. The agent binds the host
    # interface selected below, so reserve its port on that same interface.
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(2)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        client_port, dashboard_port = [sock.getsockname()[1] for sock in sockets]
        node_ip = _configure_owned_client_network(monkeypatch, client_port)
        runtime_socket = stack.enter_context(socket.socket())
        runtime_socket.bind((node_ip, 0))
        runtime_port = runtime_socket.getsockname()[1]
    # A port race fails startup and cleans this owned node; never discover or
    # attach another server that happens to occupy an allocated port.
    with tempfile.TemporaryDirectory(prefix="dr-client-", dir="/tmp") as directory:
        diagnostic_directory = (
            _install_owned_client_diagnostics(monkeypatch, Path(directory) / "client-diagnostic")
            if request.param
            else None
        )
        parameters: dict[str, Any] = {
            "num_cpus": 2,
            "num_gpus": 0,
            "object_store_memory": 128 * 1024 * 1024,
            "memory": 512 * 1024 * 1024,
            "node_ip_address": node_ip,
            "dashboard_host": "127.0.0.1",
            "dashboard_port": dashboard_port,
            "ray_client_server_port": client_port,
            "runtime_env_agent_port": runtime_port,
            "dashboard_agent_listen_port": 0,
            "gcs_server_port": 0,
            "min_worker_port": 0,
            "max_worker_port": 0,
            "include_dashboard": True,
            "no_monitor": True,
            "temp_dir": directory,
        }
        node = Node.__new__(Node)
        try:
            # Retain the instance before construction so partial startup is
            # cleaned too. Fate sharing owns children if the test runner dies.
            Node.__init__(
                node, RayParams(**parameters), head=True, shutdown_at_exit=False, spawn_reaper=True
            )
            assert ray.is_initialized() is False
            assert node.remaining_processes_alive()
            readiness = _wait_for_owned_client_services(
                node, client_port, runtime_port, runtime_host=node_ip
            )
            yield SimpleNamespace(
                ray_address=f"ray://127.0.0.1:{client_port}",
                jobs_endpoint=discovery._dashboard_endpoint(node.webui_url),
                cluster_session=node.session_name,
                readiness=readiness,
                diagnostics=lambda: _owned_client_failure_evidence(
                    node.get_logs_dir_path(), diagnostic_directory
                ),
            )
        except BaseException:
            # Retain bounded, filtered evidence before deleting the owned tree.
            if hasattr(node, "_logs_dir"):
                print(
                    "OWNED_CLIENT_RAY_DIAGNOSTICS "
                    + json.dumps(
                        _owned_client_failure_evidence(
                            node.get_logs_dir_path(), diagnostic_directory
                        ),
                        sort_keys=True,
                    )
                )
            raise
        finally:
            if hasattr(node, "all_processes"):
                node.kill_all_processes(check_alive=False, allow_graceful=True, wait=True)
                assert not node.any_processes_alive(), (
                    "Owned Client fixture left live Ray processes"
                )
            assert ray.is_initialized() is False


@pytest.mark.real_ray
def test_native_client_discovery_and_driver_death_use_owned_exec_helpers(
    owned_client_ray_node, monkeypatch: pytest.MonkeyPatch
):
    """Require the same Client DRIVER lifecycle with and without instrumentation.

    Both independently collected cases must pass; neither retries, replaces or
    promotes the other's outcome. This is not deployed authentication evidence.
    """
    from django.db.backends.utils import CursorWrapper

    from django_ray.runner.cohort_process import CohortProcessPhase, CohortProcessSupervisor

    def forbidden(*_args, **_kwargs):
        pytest.fail("Client management parent initialized Ray or accessed Django DB")

    monkeypatch.setattr(ray, "init", forbidden)
    monkeypatch.setattr(CursorWrapper, "execute", forbidden)
    monkeypatch.setattr(CursorWrapper, "executemany", forbidden)
    supervisor = CohortProcessSupervisor()
    completed = []

    def execute(command, arguments, *, timeout):
        ticket = supervisor.start(
            {"command": command, "arguments": arguments}, timeout_seconds=timeout
        )
        deadline = time.monotonic() + timeout + 6
        try:
            while time.monotonic() < deadline:
                result = supervisor.poll(ticket)
                if result is not None:
                    completed.append(result)
                    assert supervisor.outstanding is None
                    assert supervisor.phase is CohortProcessPhase.IDLE
                    assert result.operation_id == ticket.operation_id
                    _assert_owned_client_helper_succeeded(
                        owned_client_ray_node, command, result.reason
                    )
                    assert result.response is not None
                    return result.response
                time.sleep(0.02)
            pytest.fail(f"Owned {command} helper did not complete within its deadline")
        finally:
            if supervisor.outstanding is ticket:
                supervisor.cancel(ticket)
                cleanup_deadline = time.monotonic() + 7
                while supervisor.outstanding is ticket and time.monotonic() < cleanup_deadline:
                    supervisor.poll(ticket)
                    time.sleep(0.02)
                assert supervisor.outstanding is None, (
                    "Owned helper/process group cleanup remains unconfirmed"
                )

    now = datetime.now(UTC)
    arguments = request(
        discovery_id=uuid.uuid4().hex,
        ray_address=owned_client_ray_node.ray_address,
        issued_at=discovery._timestamp(now),
        expires_at=discovery._timestamp(now + timedelta(seconds=60)),
    )
    observed = execute("discover-client", arguments, timeout=40)
    discovery.validate_client_discovery_observation(arguments, observed)
    assert observed["jobs_endpoint"] == owned_client_ray_node.jobs_endpoint
    assert observed["cluster_session"] == owned_client_ray_node.cluster_session
    assert observed["namespace"] == discovery._namespace(arguments)
    assert observed["request"] == arguments
    assert discovery._native_id(observed["native_job_id"]) == observed["native_job_id"]
    assert observed["disconnect_returned"] is True
    assert "terminal" not in observed
    assert ray.is_initialized() is False

    terminal = None
    cleanup_deadline = time.monotonic() + 30
    for _attempt in range(6):
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 1:
            break
        inspected = execute(
            "inspect-driver",
            {"request": arguments, "observation": observed},
            timeout=min(10, remaining),
        )
        discovery.validate_client_driver_inspection(arguments, observed, inspected)
        if inspected["terminal"]:
            terminal = inspected
            break
        time.sleep(0.1)
    assert terminal is not None, (
        "Exact native DRIVER death did not propagate within the owned cleanup window"
    )
    assert terminal["observation"]["native_job_id"] == observed["native_job_id"]
    assert terminal["observation"]["driver_pid"] == observed["driver_pid"]
    assert len(completed) >= 2
    assert ray.is_initialized() is False
    assert supervisor.outstanding is None
