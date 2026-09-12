"""Private Jobs helper commands use bounded transport and no native Ray/DB setup."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import ray
import requests
from ray.dashboard.modules import dashboard_sdk
from ray.dashboard.modules.job.common import JobStatus
from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

from django_ray.runner import cohort_job_control as control
from django_ray.runtime.cohort_job import (
    CohortProbeJobLease,
    CohortProbeJobRequest,
    encode_probe_job_request,
    probe_job_metadata,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.runtime.cohort_job_entrypoint import (
    CohortProbeJobLaunch,
    encode_probe_job_launch,
    probe_job_launch_entrypoint,
)
from django_ray.target import cohort_job_control as inspector
from django_ray.target.attestation import RayRunnerFamily
from tests.unit.test_cohort_job_http import transport as transport  # noqa: F401

NOW = datetime(2026, 9, 12, tzinfo=UTC)
PACKAGE, RUNTIME = control._local_runtime(ray)
SETTINGS = "project.settings"
ENDPOINT = "https://ray.example.test:8265/prefix"
DIGEST = "sha256:" + "a" * 64


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def specification(**changes):
    return {"env_vars": {"DJANGO_SETTINGS_MODULE": SETTINGS}, **changes}


def preparation(spec=None, **changes):
    spec = specification() if spec is None else spec
    return {
        "ray_address": ENDPOINT,
        "control_runtime_env_json": canonical(spec),
        "source_control_profile_digest": control.cohort_probe_submitted_runtime_env_digest(spec),
        "django_settings_module": SETTINGS,
        "expected_package_version": PACKAGE,
        "expected_runtime": asdict(RUNTIME),
    } | changes


def launch(spec=None, **changes):
    spec = specification() if spec is None else spec
    request = CohortProbeJobRequest(
        1,
        1,
        CohortProbeJobLease("manager", "host", 12, NOW - timedelta(seconds=30)),
        DIGEST,
        None,
        RayRunnerFamily.RAY_JOB,
        PACKAGE,
        RUNTIME,
        None,
        None,
        1,
        NOW - timedelta(seconds=10),
        NOW + timedelta(seconds=300),
    )
    request = replace(request, **changes)
    return CohortProbeJobLaunch(
        request,
        probe_job_request_digest(request),
        ENDPOINT,
        control.cohort_probe_submitted_runtime_env_digest(spec),
        SETTINGS,
    )


def submit_arguments(packet=None, spec=None):
    spec = specification() if spec is None else spec
    packet = launch(spec) if packet is None else packet
    return {
        "launch_json": encode_probe_job_launch(packet),
        "submitted_runtime_env_json": canonical(spec),
    }


def snapshot_arguments(packet=None):
    packet = packet or launch()
    return {
        "request_json": encode_probe_job_request(packet.request),
        "request_digest": packet.request_digest,
        "jobs_endpoint": packet.jobs_endpoint,
        "entrypoint_digest": control.cohort_probe_entrypoint_digest(
            probe_job_launch_entrypoint(packet)
        ),
        "submitted_runtime_env_digest": packet.submitted_runtime_env_digest,
        "receipt_json": "{}",
        "receipt_digest": DIGEST,
    }


def details(packet=None, *, status=JobStatus.RUNNING, **changes):
    packet = packet or launch()
    assert JobDetails is not None
    values = {
        "type": JobType.SUBMISSION,
        "submission_id": probe_job_submission_id(packet.request),
        "job_id": "01000000",
        "status": status,
        "entrypoint": probe_job_launch_entrypoint(packet),
        "metadata": probe_job_metadata(packet.request),
        "runtime_env": specification(),
    } | changes
    return JobDetails(**values)


@pytest.fixture
def sdk(transport, monkeypatch):
    # Full native suites may run long after collection. Keep these mocked
    # request windows independent of that delay without bypassing expiry checks.
    monkeypatch.setattr(control, "datetime", SimpleNamespace(now=lambda _tz: NOW))
    state = transport
    state.sequence = []
    state.uploads = []
    state.normalizations = []
    state.normalized_change = None
    state.requests.clear()
    state.body = b"{}"
    original_get = requests.Session.get

    def request(self, method, url, **kwargs):
        state.methods = getattr(state, "methods", []) + [method]
        if state.sequence:
            state.response.status_code, body = state.sequence.pop(0)
            state.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        state.position = 0
        state.response.raw.closed = False
        return original_get(self, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", request, raising=False)

    def upload(client, spec, key):
        state.uploads.append(key)
        if key in spec:
            path = "/api/packages/gcs/_ray_pkg_123456.zip"
            found = client._do_request("GET", path)
            if found.status_code == 404:
                client._do_request("PUT", path, data=b"owned package")
            spec[key] = (
                ["gcs://_ray_pkg_123456.zip"]
                if key == "py_modules"
                else "gcs://_ray_pkg_123456.zip"
            )

    monkeypatch.setattr(
        dashboard_sdk.SubmissionClient,
        "_upload_working_dir_if_needed",
        lambda client, spec: upload(client, spec, "working_dir"),
        raising=False,
    )
    monkeypatch.setattr(
        dashboard_sdk.SubmissionClient,
        "_upload_py_modules_if_needed",
        lambda client, spec: upload(client, spec, "py_modules"),
        raising=False,
    )

    class RuntimeEnv(dict):
        def __init__(self, **spec):
            state.normalizations.append(dict(spec))
            if state.normalized_change:
                spec.update(state.normalized_change)
            super().__init__(spec)

        def to_dict(self):
            return dict(self)

    import ray.runtime_env
    import ray.runtime_env.runtime_env

    monkeypatch.setattr(ray.runtime_env, "RuntimeEnv", RuntimeEnv)
    monkeypatch.setattr(ray.runtime_env.runtime_env, "_validate_no_local_paths", lambda spec: None)
    monkeypatch.setattr(ray, "init", lambda *a, **k: pytest.fail("Ray initialized"))
    return state


def execute(command, payload):
    return control.execute_cohort_job_control(command, payload)


def assert_closed(sdk, count=1):
    assert sdk.closed_responses == count
    assert sdk.closed_sessions == count


def test_prepare_preserves_trusted_dependency_delivery_and_distinct_source_digest(sdk, monkeypatch):
    source = specification(
        working_dir="/owned/source", pip=["example==1.0"], uv={"packages": ["example==1.0"]}
    )
    arguments = preparation(source)
    monkeypatch.setenv("RAY_ADDRESS", "http://wrong.invalid")
    monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "http://wrong.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://wrong.invalid")
    sdk.sequence = [(404, b""), (200, b"")]
    result = execute("prepare", arguments)
    submitted = json.loads(result["submitted_runtime_env_json"])
    assert result["jobs_endpoint"] == ENDPOINT
    assert result["source_control_profile_digest"] == arguments["source_control_profile_digest"]
    assert result["submitted_runtime_env_digest"] != result["source_control_profile_digest"]
    assert submitted["working_dir"] == "gcs://_ray_pkg_123456.zip"
    assert submitted["pip"] == source["pip"] and submitted["uv"] == source["uv"]
    assert sdk.methods == ["GET", "PUT"]
    assert sdk.initializations == [ENDPOINT]
    for url, options in sdk.requests:
        assert url.startswith(ENDPOINT + "/api/packages/gcs/")
        assert options["allow_redirects"] is False and options["proxies"] == {}
        assert options["stream"] is True and options["verify"] == "fixed-ca-path"
        assert options["headers"]["Authorization"] == "Bearer fixed-test-value"
        assert options["cookies"] == {"session": "fixed-cookie"}
        assert options["headers"]["Accept-Encoding"] == "identity"
    assert_closed(sdk, 2)


def test_prepare_uploads_modules_and_pins_missing_settings(sdk):
    source = {"py_modules": ["/owned/module"]}
    result = execute("prepare", preparation(source))
    submitted = json.loads(result["submitted_runtime_env_json"])
    assert submitted["py_modules"] == ["gcs://_ray_pkg_123456.zip"]
    assert submitted["env_vars"]["DJANGO_SETTINGS_MODULE"] == SETTINGS
    assert sdk.uploads == ["working_dir", "py_modules"]


@pytest.mark.parametrize(
    "changes",
    [
        {"source_control_profile_digest": DIGEST},
        {"source_control_profile_digest": True},
        {"django_settings_module": "other.settings"},
        {"django_settings_module": "invalid"},
        {"control_runtime_env_json": '{"env_vars":{},"env_vars":{}}'},
        {"control_runtime_env_json": '{"x":NaN}'},
        {"control_runtime_env_json": "{} "},
        {"nonce": "must-not-cross"},
        {"control_runtime_env_json": "[1]"},
    ],
)
def test_invalid_prepare_carrier_refuses_before_sdk(sdk, changes):
    with pytest.raises(control.CohortJobProcessControlError):
        execute("prepare", preparation() | changes)
    assert sdk.initializations == [] and sdk.requests == []


@pytest.mark.parametrize("spec", [{"env_vars": []}, {"env_vars": {"NAME": False}}])
def test_prepare_rejects_non_string_settings_environment_before_sdk(sdk, spec):
    with pytest.raises(control.CohortJobProcessControlError):
        execute("prepare", preparation(spec))
    assert sdk.initializations == []


@pytest.mark.parametrize(
    "serialized", [True, "x" * (64 * 1024 + 1)], ids=["non-string", "oversized"]
)
def test_profile_byte_limit_is_checked_before_json_parsing_or_sdk(sdk, serialized):
    with pytest.raises(control.CohortJobProcessControlError, match="invalid"):
        execute("prepare", preparation(control_runtime_env_json=serialized))
    assert sdk.initializations == []


def test_normalized_profile_expansion_is_bounded(sdk):
    sdk.normalized_change = {"large": "x" * (64 * 1024)}
    with pytest.raises(control.CohortJobProcessControlError, match="resource_limit"):
        execute("prepare", preparation())
    assert sdk.requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_package_version": "0.0.1"},
        {"expected_runtime": asdict(replace(RUNTIME, python_patch=RUNTIME.python_patch + 1))},
        {"expected_runtime": asdict(replace(RUNTIME, ray_minor=True))},
        {"expected_runtime": {}},
    ],
)
def test_local_runtime_mismatch_precedes_any_endpoint_work(sdk, monkeypatch, changes):
    monkeypatch.setattr(
        control,
        "_resolve_endpoint",
        lambda *_a: pytest.fail("Endpoint resolved before local guard"),
    )
    with pytest.raises(control.CohortJobProcessControlError, match="local_runtime_mismatch"):
        execute("prepare", preparation() | changes)


def test_ray_client_address_is_explicitly_unavailable_without_init(sdk):
    with pytest.raises(control.CohortJobProcessControlError, match="unsupported_address"):
        execute("prepare", preparation(ray_address="ray://ray-head:10001"))
    assert sdk.initializations == []


@pytest.mark.parametrize(
    "declared,addresses,bootstrap",
    [
        ("gcs.example:6379", set(), None),
        ("auto", {"gcs.example:6379"}, None),
        ("auto", {"other.example:6379", "gcs.example:6379"}, "gcs.example:6379"),
        ("auto", set(), "gcs.example:6379"),
    ],
)
def test_gcs_resolution_uses_exact_concrete_address_without_global_kv_or_env_override(
    sdk, monkeypatch, declared, addresses, bootstrap
):
    import ray._raylet as raylet
    import ray.experimental.internal_kv
    from ray._private import ray_constants, services

    monkeypatch.setenv("RAY_ADDRESS", "wrong:6379")
    monkeypatch.setattr(services, "find_gcs_addresses", lambda: addresses)
    monkeypatch.setattr(services, "find_bootstrap_address", lambda _arg: bootstrap)
    calls = []
    monkeypatch.setattr(
        services, "canonicalize_bootstrap_address", lambda value: calls.append(value) or value
    )
    monkeypatch.setattr(
        ray.experimental.internal_kv,
        "_initialize_internal_kv",
        lambda *_a: pytest.fail("Global KV changed"),
    )

    class GcsClient:
        def __init__(self, *, address):
            assert address == "gcs.example:6379"

        def internal_kv_get(self, key, *, namespace, timeout):
            assert key == ray_constants.DASHBOARD_ADDRESS
            assert namespace == ray_constants.KV_NAMESPACE_DASHBOARD and timeout == 5
            return b"discovered.example:8265"

    monkeypatch.setattr(raylet, "GcsClient", GcsClient)
    result = execute("prepare", preparation(ray_address=declared))
    assert result["jobs_endpoint"] == "http://discovered.example:8265"
    assert calls == ["gcs.example:6379"]


@pytest.mark.parametrize("kind", ["absent", "ambiguous", "invalid_concrete", "missing_dashboard"])
def test_gcs_discovery_failure_never_guesses_an_http_endpoint(sdk, monkeypatch, kind):
    import ray._raylet as raylet
    from ray._private import services

    addresses = {"one:6379", "two:6379"} if kind == "ambiguous" else set()
    monkeypatch.setattr(services, "find_gcs_addresses", lambda: addresses)
    monkeypatch.setattr(services, "find_bootstrap_address", lambda _arg: None)
    monkeypatch.setattr(
        services,
        "canonicalize_bootstrap_address",
        lambda value: None if kind == "invalid_concrete" else value,
    )

    class GcsClient:
        def __init__(self, *, address):
            assert kind == "missing_dashboard"

        def internal_kv_get(self, *_a, **_k):
            return None

    monkeypatch.setattr(raylet, "GcsClient", GcsClient)
    address = "auto" if kind in {"absent", "ambiguous"} else "gcs.example:6379"
    with pytest.raises(control.CohortJobProcessControlError):
        execute("prepare", preparation(ray_address=address))
    assert sdk.initializations == [] and sdk.requests == []


def test_submit_is_one_fixed_exact_request_and_returns_owned_id(sdk):
    packet = launch()
    submission_id = probe_job_submission_id(packet.request)
    sdk.body = json.dumps({"job_id": submission_id, "submission_id": submission_id}).encode()
    assert execute("submit", submit_arguments(packet)) == {
        "submitted": True,
        "submission_id": submission_id,
    }
    assert sdk.methods == ["POST"] and sdk.uploads == []
    url, options = sdk.requests[0]
    assert url == ENDPOINT + "/api/jobs/"
    assert options["json"] == {
        "entrypoint": probe_job_launch_entrypoint(packet),
        "submission_id": submission_id,
        "runtime_env": specification(),
        "metadata": probe_job_metadata(packet.request),
    }
    assert_closed(sdk)


def test_submit_rechecks_expiry_after_client_preparation_before_http(sdk, monkeypatch):
    times = iter((NOW, NOW + timedelta(seconds=301)))
    monkeypatch.setattr(control, "datetime", SimpleNamespace(now=lambda _tz: next(times)))
    with pytest.raises(control.CohortJobProcessControlError, match="invalid"):
        execute("submit", submit_arguments())
    assert sdk.initializations == [ENDPOINT]
    assert sdk.requests == []


@pytest.mark.parametrize("kind", ["settings", "digest", "normalization", "expired"])
def test_submit_refuses_mismatched_immutable_mapping_before_http(sdk, kind):
    packet = launch()
    args = submit_arguments(packet)
    if kind == "settings":
        args["submitted_runtime_env_json"] = canonical(
            {"env_vars": {"DJANGO_SETTINGS_MODULE": "wrong.settings"}}
        )
    elif kind == "digest":
        args["submitted_runtime_env_json"] = canonical(specification(pip=["extra"]))
    elif kind == "normalization":
        sdk.normalized_change = {"pip": ["mutation"]}
    else:
        args = submit_arguments(
            launch(issued_at=NOW - timedelta(seconds=10), expires_at=NOW - timedelta(seconds=1))
        )
    with pytest.raises(control.CohortJobProcessControlError):
        execute("submit", args)
    assert sdk.requests == []


@pytest.mark.parametrize(
    "body",
    [
        b'{"job_id":"other","submission_id":"other"}',
        b'{"submission_id":"x"}',
        b'{"a":1,"a":2}',
        b'{"x":NaN}',
    ],
)
def test_ambiguous_submit_response_never_resubmits_or_exposes_body(sdk, body):
    sdk.body = body
    with pytest.raises(control.CohortJobProcessControlError) as caught:
        execute("submit", submit_arguments())
    assert len(sdk.requests) == 1
    assert "other" not in str(caught.value)
    assert_closed(sdk)


@pytest.mark.parametrize(
    "header,value",
    [
        ("Content-Encoding", "gzip"),
        ("Transfer-Encoding", "chunked"),
        ("Content-Length", str(128 * 1024 + 1)),
    ],
)
def test_mutation_response_limits_reuse_existing_bounded_reader(sdk, header, value):
    sdk.response.headers[header] = value
    with pytest.raises(control.CohortJobProcessControlError):
        execute("submit", submit_arguments())
    assert len(sdk.requests) == 1
    assert_closed(sdk)


def test_trickle_timeout_and_redirect_are_fixed_refusals(sdk):
    sdk.body = b"unbounded-secret-response"
    sdk.read_step = 1
    with pytest.raises(control.CohortJobProcessControlError) as caught:
        execute("submit", submit_arguments())
    assert "secret" not in str(caught.value) and sdk.reads <= 5
    assert_closed(sdk)


def test_redirect_does_not_follow_or_read_server_body(sdk):
    sdk.response.status_code = 302
    with pytest.raises(control.CohortJobProcessControlError, match="response_mismatch"):
        execute("submit", submit_arguments())
    assert sdk.reads == 0 and sdk.requests[0][1]["allow_redirects"] is False
    assert_closed(sdk)


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/api/jobs/x"),
        ("GET", "/api/version"),
        ("PUT", "/api/packages/gcs/../x.zip"),
        ("PUT", "/api/packages/http/x.zip"),
        ("POST", "/api/jobs/"),
    ],
)
def test_upload_client_cannot_access_arbitrary_methods_or_paths(sdk, method, path):
    client = control._client(ENDPOINT, operation="prepare")
    with pytest.raises(control.CohortJobProcessControlError, match="invalid"):
        client._do_request(method, path, data=b"package")
    assert sdk.requests == []


@pytest.mark.parametrize("terminal", [False, True])
def test_stop_ack_is_not_terminal_proof(sdk, monkeypatch, terminal):
    packet = launch()
    reads = [
        details(packet),
        details(packet, status=JobStatus.STOPPED if terminal else JobStatus.RUNNING),
    ]
    monkeypatch.setattr(
        control, "fetch_reserved_cohort_job_details", lambda endpoint, handle: reads.pop(0)
    )
    sdk.body = b'{"stopped":true}'
    assert execute("stop", {"launch_json": encode_probe_job_launch(packet)}) == {
        "terminal": terminal
    }
    assert sdk.methods == ["POST"]
    assert (
        sdk.requests[0][0]
        == ENDPOINT + "/api/jobs/" + probe_job_submission_id(packet.request) + "/stop"
    )


def test_already_terminal_failed_bootstrap_without_native_driver_is_cleanable(sdk, monkeypatch):
    packet = launch(expires_at=NOW - timedelta(seconds=1))
    monkeypatch.setattr(
        control,
        "fetch_reserved_cohort_job_details",
        lambda *_a: details(packet, status=JobStatus.FAILED, job_id=None),
    )
    assert execute("stop", {"launch_json": encode_probe_job_launch(packet)}) == {"terminal": True}
    assert sdk.requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"metadata": {}},
        {"entrypoint": "arbitrary"},
        {"runtime_env": {}},
        {"submission_id": "other"},
        {"type": JobType.DRIVER},
        {"job_id": "not-native"},
        {"driver_info": {"id": "02000000", "node_ip_address": "127.0.0.1", "pid": "1"}},
    ],
)
def test_stop_refuses_wrong_owned_job_before_mutation(sdk, monkeypatch, changes):
    monkeypatch.setattr(
        control, "fetch_reserved_cohort_job_details", lambda *_a: details(**changes)
    )
    with pytest.raises(control.CohortJobProcessControlError, match="response_mismatch"):
        execute("stop", {"launch_json": encode_probe_job_launch(launch())})
    assert sdk.requests == []


def test_stop_refuses_changed_native_incarnation_after_request(sdk, monkeypatch):
    rows = [details(), details(status=JobStatus.STOPPED, job_id="02000000")]
    monkeypatch.setattr(control, "fetch_reserved_cohort_job_details", lambda *_a: rows.pop(0))
    sdk.body = b'{"stopped":true}'
    with pytest.raises(control.CohortJobProcessControlError, match="response_mismatch"):
        execute("stop", {"launch_json": encode_probe_job_launch(launch())})
    assert len(sdk.requests) == 1


@pytest.mark.parametrize("body", [b'{"stopped":1}', b'{"stopped":true,"message":"secret"}'])
def test_stop_rejects_malformed_ack_without_claiming_cleanup(sdk, monkeypatch, body):
    sdk.body = body
    monkeypatch.setattr(control, "fetch_reserved_cohort_job_details", lambda *_a: details())
    with pytest.raises(control.CohortJobProcessControlError, match="response_mismatch") as caught:
        execute("stop", {"launch_json": encode_probe_job_launch(launch())})
    assert "secret" not in str(caught.value)
    assert len(sdk.requests) == 1


@pytest.mark.parametrize("pending", [False, True])
def test_inspect_uses_detached_verifier_and_only_returns_fixed_receipt_time(
    sdk, monkeypatch, pending
):
    seen = []

    def detached(snapshot):
        seen.append(snapshot)
        return None if pending else inspector.InspectedCohortJobReceipt(snapshot, None, NOW)

    monkeypatch.setattr(inspector, "inspect_detached_cohort_job", detached)
    result = execute("inspect", snapshot_arguments())
    assert result == (
        {"pending": True}
        if pending
        else {"pending": False, "inspected_at": control._timestamp(NOW)}
    )
    assert len(seen) == 1 and seen[0].request_digest == launch().request_digest
    assert sdk.initializations == []


@pytest.mark.parametrize(
    "command,args",
    [
        ("unknown", {}),
        (True, {}),
        ("prepare", []),
        ("stop", {"launch_json": "{}", "nonce": "private"}),
        ("inspect", snapshot_arguments() | {"nonce": "private"}),
        ("submit", submit_arguments() | {"entrypoint": "arbitrary"}),
    ],
)
def test_command_schema_rejects_extra_authority_fields(sdk, command, args):
    with pytest.raises(control.CohortJobProcessControlError):
        execute(command, args)
    assert sdk.requests == []


def test_transport_exceptions_never_echo_secret_diagnostics(sdk):
    sdk.get_error = RuntimeError("Authorization Bearer secret-value")
    with pytest.raises(control.CohortJobProcessControlError) as caught:
        execute("submit", submit_arguments())
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert sdk.closed_sessions == 1


def test_import_has_no_django_or_ray_import_in_fresh_process():
    code = """
import builtins
original = builtins.__import__
def guarded(name,*args,**kwargs):
    if name == 'django' or name.startswith('django.') or name == 'ray' or name.startswith('ray.'):
        raise AssertionError('Forbidden import: ' + name)
    return original(name,*args,**kwargs)
builtins.__import__ = guarded
import django_ray.runner.cohort_job_control
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
