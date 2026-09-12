"""Resource-free contracts and exact native DRIVER HTTP transport."""

import copy
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
import ray
from ray.dashboard.modules.job.pydantic_models import DriverInfo, JobDetails, JobStatus, JobType

from django_ray.runner import cohort_client_discovery as discovery
from django_ray.target.cohort_job_http import CohortJobHttpError
from django_ray.target.cohort_runtime import _local_runtime
from tests.unit.test_cohort_job_http import transport as transport  # noqa: F401

NOW = datetime(2026, 9, 12, tzinfo=UTC)
PACKAGE, RUNTIME = _local_runtime(ray)
ENDPOINT = "https://ray.example.test:8265/prefix"
NATIVE_ID = "01000000"


def request(**changes):
    return {
        "discovery_id": "a" * 32,
        "configuration_digest": "sha256:" + "b" * 64,
        "ray_address": "ray://ray.example.test:10001",
        "package_version": PACKAGE,
        "runtime": asdict(RUNTIME),
        "issued_at": discovery._timestamp(NOW - timedelta(seconds=5)),
        "expires_at": discovery._timestamp(NOW + timedelta(seconds=60)),
    } | changes


def driver(arguments=None, **changes):
    arguments = arguments or request()
    assert JobDetails is not None and DriverInfo is not None
    return JobDetails(
        **(
            {
                "type": JobType.DRIVER,
                "job_id": NATIVE_ID,
                "submission_id": None,
                "status": JobStatus.RUNNING,
                "entrypoint": "python /fixed/ray/client/server.py",
                "metadata": discovery._metadata(arguments),
                "runtime_env": copy.deepcopy(discovery._RUNTIME_ENV),
                "start_time": int(NOW.timestamp() * 1000),
                "driver_info": DriverInfo(id=NATIVE_ID, node_ip_address="10.0.0.4", pid="123"),
            }
            | changes
        )
    )


def observation(arguments=None, **changes):
    arguments = arguments or request()
    return {
        "schema_version": 1,
        "request": copy.deepcopy(arguments),
        "jobs_endpoint": ENDPOINT,
        "native_job_id": NATIVE_ID,
        "cluster_session": "session_native_test",
        "namespace": discovery._namespace(arguments),
        **discovery._corroborate(driver(arguments), arguments, native_id=NATIVE_ID),
        "observed_at": discovery._timestamp(NOW),
        "disconnect_returned": True,
    } | changes


@pytest.mark.parametrize(
    "change",
    [
        {"discovery_id": ""},
        {"discovery_id": "A" * 32},
        {"configuration_digest": "secret"},
        {"ray_address": "http://ray:8265"},
        {"ray_address": "ray://ray"},
        {"ray_address": "ray://ray:10001/path"},
        {"ray_address": "ray://user:password@ray:10001"},
        {"ray_address": "ray://ray:10001?token=secret"},
        {"ray_address": "ray://ray:10001#fragment"},
        {"ray_address": "ray://ray:10001/"},
        {"ray_address": "ray://ray:99999"},
        {"expires_at": "2026-09-12T00:01:00+00:00"},
        {"expires_at": discovery._timestamp(NOW + timedelta(seconds=301))},
        {"expires_at": discovery._timestamp(NOW - timedelta(seconds=6))},
        {"package_version": "999.0.0"},
        {"extra": "not allowed"},
    ],
)
def test_invalid_discovery_request_cannot_connect(monkeypatch, change):
    import ray.client_builder

    monkeypatch.setattr(
        ray.client_builder, "ClientBuilder", lambda *_args: pytest.fail("Invalid request connected")
    )
    with pytest.raises(discovery.ClientDiscoveryError) as error:
        discovery.discover_client_jobs_endpoint(request(**change))
    assert "secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"disconnect_returned": 1},
        {"namespace": "other"},
        {"native_job_id": "django-ray-cohort-probe-" + "a" * 64},
        {"native_job_id": "ffffffff"},
        {"native_job_id": "01000000/stop"},
        {"cluster_session": "invalid/session"},
        {"jobs_endpoint": "https://ray.example.test:8265/prefix/"},
        {"driver_pid": "00123"},
        {"driver_start_time": True},
        {"driver_node_ip_address": "hostname-not-ip"},
        {"driver_entrypoint_digest": "secret"},
        {"observed_at": discovery._timestamp(NOW - timedelta(seconds=6))},
        {"observed_at": discovery._timestamp(NOW + timedelta(seconds=60))},
        {"extra": 1},
    ],
)
def test_observation_correlates_exact_owned_request(change):
    with pytest.raises(discovery.ClientDiscoveryError):
        discovery.validate_client_discovery_observation(request(), observation(**change))


def test_observation_cannot_cross_operation_configuration_or_runtime():
    value = observation()
    for changed in (
        request(discovery_id="c" * 32),
        request(configuration_digest="sha256:" + "c" * 64),
        request(ray_address="ray://other:10001"),
    ):
        with pytest.raises(discovery.ClientDiscoveryError):
            discovery.validate_client_discovery_observation(changed, value)
    value["request"]["runtime"]["python_patch"] = float(RUNTIME.python_patch)
    with pytest.raises(discovery.ClientDiscoveryError):
        discovery.validate_client_discovery_observation(request(), value)


def test_native_http_uses_authenticated_exact_get_and_never_submission_transport(transport):
    transport.body = driver().model_dump_json().encode()
    actual = discovery._driver_details(ENDPOINT, NATIVE_ID)
    assert actual.type is JobType.DRIVER
    assert actual.submission_id is None
    assert len(transport.requests) == 1
    url, options = transport.requests[0]
    assert url == ENDPOINT + "/api/jobs/" + NATIVE_ID
    assert options["headers"]["Authorization"] == "Bearer fixed-test-value"
    assert options["verify"] == "fixed-ca-path"
    assert options["allow_redirects"] is False
    assert options["stream"] is True
    assert transport.closed_responses == transport.closed_sessions == 1


@pytest.mark.parametrize(
    "change",
    [
        {"type": "SUBMISSION"},
        {"job_id": "02000000"},
        {"submission_id": "01000000"},
        {"driver_info": None},
        {"driver_info": {"id": "02000000", "pid": "123", "node_ip_address": "10.0.0.4"}},
        {"start_time": "123"},
        {"start_time": True},
        {"new_unreviewed_field": 1},
    ],
)
def test_native_http_rejects_crossed_or_coerced_records(transport, change):
    transport.body = json.dumps(driver().model_dump(mode="json") | change).encode()
    with pytest.raises((discovery.ClientDiscoveryError, ValueError)):
        discovery._driver_details(ENDPOINT, NATIVE_ID)
    assert transport.closed_responses == transport.closed_sessions == 1


@pytest.mark.parametrize("status", [301, 401, 403, 404, 500])
def test_native_http_does_not_treat_missing_or_unreadable_driver_as_dead(transport, status):
    transport.response.status_code = status
    with pytest.raises(discovery.ClientDiscoveryError):
        discovery._driver_details(ENDPOINT, NATIVE_ID)
    assert transport.reads == 0


def test_native_http_duplicate_and_oversized_payload_fail_closed(transport):
    transport.body = b'{"job_id":"01000000","job_id":"02000000"}'
    with pytest.raises(ValueError):
        discovery._driver_details(ENDPOINT, NATIVE_ID)
    transport.position = 0
    transport.response.headers["Content-Length"] = str(128 * 1024 + 1)
    transport.reads = 0
    with pytest.raises(CohortJobHttpError):
        discovery._driver_details(ENDPOINT, NATIVE_ID)
    assert transport.reads == 0


@pytest.mark.parametrize("terminal", [None, 0, 1, "true"])
def test_inspection_requires_boolean_terminal_and_exact_correlated_observation(terminal):
    observed = observation()
    result = {
        "schema_version": 1,
        "request": request(),
        "observation": observed,
        "terminal": terminal,
        "inspected_at": discovery._timestamp(NOW),
    }
    with pytest.raises(discovery.ClientDiscoveryError):
        discovery.validate_client_driver_inspection(request(), observed, result)


@pytest.mark.parametrize("change", ["extra", "request", "observation", "clock", "schema"])
def test_inspection_response_cannot_substitute_another_owned_operation(change):
    observed = observation()
    result = {
        "schema_version": 1,
        "request": request(),
        "observation": copy.deepcopy(observed),
        "terminal": True,
        "inspected_at": discovery._timestamp(NOW),
    }
    if change == "extra":
        result["extra"] = True
    elif change == "request":
        result["request"]["discovery_id"] = "c" * 32
    elif change == "observation":
        result["observation"]["native_job_id"] = "02000000"
    elif change == "clock":
        result["inspected_at"] = discovery._timestamp(NOW - timedelta(seconds=1))
    else:
        result["schema_version"] = True
    with pytest.raises(discovery.ClientDiscoveryError):
        discovery.validate_client_driver_inspection(request(), observed, result)


def test_parent_request_validation_performs_no_connection_and_retains_fixed_errors(monkeypatch):
    import ray.client_builder

    monkeypatch.setattr(
        ray.client_builder, "ClientBuilder", lambda *_args: pytest.fail("Validation connected")
    )
    assert discovery.validate_client_discovery_request(request()) == request()
    with pytest.raises(discovery.ClientDiscoveryError, match="invalid"):
        discovery.validate_client_discovery_request({})


def test_native_http_progress_budget_rejects_late_body_and_closes_response(transport):
    transport.body = driver().model_dump_json().encode()
    transport.read_step = 1.0
    with pytest.raises(CohortJobHttpError):
        discovery._driver_details(ENDPOINT, NATIVE_ID)
    assert transport.reads <= 5
    assert transport.closed_responses == transport.closed_sessions == 1


def test_real_pinned_client_configuration_suppresses_ambient_uv_and_source_upload(monkeypatch):
    from ray._private.runtime_env import uv_runtime_env_hook
    from ray.client_builder import ClientBuilder
    from ray.job_config import JobConfig
    from ray.util.client import _apply_uv_hook_for_client

    monkeypatch.setenv("RAY_RUNTIME_ENV", '{"working_dir":"/unrelated/source"}')
    monkeypatch.setenv("RAY_NAMESPACE", "unrelated-namespace")
    monkeypatch.setattr(
        uv_runtime_env_hook, "hook", lambda *_args: pytest.fail("Inherited UV hook was invoked")
    )
    builder = ClientBuilder("ray.example.test:10001")
    builder._job_config = JobConfig(
        runtime_env=dict(discovery._RUNTIME_ENV),
        ray_namespace=discovery._namespace(request()),
        metadata=discovery._metadata(request()),
    )
    builder._fill_defaults_from_env()
    assert builder._job_config.ray_namespace == discovery._namespace(request())
    assert builder._job_config.runtime_env == discovery._RUNTIME_ENV
    assert _apply_uv_hook_for_client(builder._job_config.runtime_env) == discovery._RUNTIME_ENV
    proto = builder._job_config._get_proto_job_config()
    assert dict(proto.metadata) == discovery._metadata(request())
    assert json.loads(proto.runtime_env_info.serialized_runtime_env) == discovery._RUNTIME_ENV


def test_pinned_server_uv_hook_would_replace_client_job_config(monkeypatch, tmp_path):
    from ray._private import ray_constants, worker
    from ray._private.runtime_env import uv_runtime_env_hook

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ray_constants, "RAY_ENABLE_UV_RUN_RUNTIME_ENV", True)
    monkeypatch.setattr(
        uv_runtime_env_hook, "_get_uv_run_cmdline", lambda: ["uv", "run", "python", "fixed.py"]
    )
    # Specific-server ray.init receives job_config but runtime_env=None. A
    # nonempty return here replaces that JobConfig in worker.init, even though
    # the separate Client hook respected the supplied py_executable.
    injected = worker._maybe_modify_runtime_env(None, False)
    assert injected and injected["py_executable"] != "python"
    assert injected["working_dir"] == str(tmp_path)


def test_control_environment_is_installed_before_server_exec(monkeypatch):
    from types import SimpleNamespace

    from ray._private.runtime_env import context

    monkeypatch.setenv("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "1")
    calls = []

    def launched(*args, **kwargs):
        assert os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] == "0"
        calls.append(True)
        return SimpleNamespace(wait=lambda: None)

    monkeypatch.setattr(context.os, "execvp", launched)
    monkeypatch.setattr(context.subprocess, "Popen", launched)
    configured = context.RuntimeEnvContext(
        py_executable=discovery._RUNTIME_ENV["py_executable"],
        env_vars=copy.deepcopy(discovery._RUNTIME_ENV["env_vars"]),
    )
    configured.exec_worker(
        ["-m", "ray.util.client.server", "--mode=specific-server"],
        context.Language.PYTHON,
    )
    assert calls == [True]


def test_fresh_control_server_interpreter_does_not_inherit_uv_runtime_env():
    environment = dict(os.environ)
    environment.pop("RAY_RUNTIME_ENV_HOOK", None)
    env_vars = discovery._RUNTIME_ENV["env_vars"]
    assert isinstance(env_vars, dict)
    environment.update(env_vars)
    code = """
from unittest.mock import patch
from ray._private import ray_constants, worker
from ray._private.runtime_env import uv_runtime_env_hook
assert ray_constants.RAY_ENABLE_UV_RUN_RUNTIME_ENV is False
with patch.object(uv_runtime_env_hook, '_get_uv_run_cmdline',
                  side_effect=AssertionError('server inspected UV ancestors')):
    assert worker._maybe_modify_runtime_env(None, False) is None
import ray
assert ray.is_initialized() is False
print('verified')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert result.stdout.strip() == "verified"


@pytest.mark.parametrize(
    "runtime_env",
    [
        {"py_executable": "python"},
        {"py_executable": "python", "env_vars": {"RAY_ENABLE_UV_RUN_RUNTIME_ENV": "1"}},
        {
            "py_executable": "python",
            "env_vars": {"RAY_ENABLE_UV_RUN_RUNTIME_ENV": "0"},
            "working_dir": "/private/source",
        },
    ],
)
def test_driver_corroboration_requires_entire_fixed_control_profile(runtime_env):
    with pytest.raises(discovery.ClientDiscoveryError) as error:
        discovery._corroborate(driver(runtime_env=runtime_env), request(), native_id=NATIVE_ID)
    assert error.value.reason is discovery.ClientDiscoveryReason.RESPONSE_MISMATCH


def test_module_import_and_rejection_do_not_initialize_django_or_ray():
    code = """
import builtins
import sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'django' or name.startswith('django.'):
        raise AssertionError('Client discovery imported Django')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from django_ray.runner.cohort_client_discovery import discover_client_jobs_endpoint, ClientDiscoveryError
try:
    discover_client_jobs_endpoint({})
except ClientDiscoveryError:
    pass
else:
    raise AssertionError('invalid request accepted')
import ray
assert ray.is_initialized() is False
assert not any(name == 'django' or name.startswith('django.') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
