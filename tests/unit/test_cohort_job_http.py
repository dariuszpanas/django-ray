from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import ray
import requests
from ray.dashboard.modules import dashboard_sdk
from ray.dashboard.modules.job.pydantic_models import JobDetails, JobStatus, JobType
from requests.structures import CaseInsensitiveDict

from django_ray.target import cohort_job_http as http

ENDPOINT = "https://ray.example.test:8265/prefix"
SUBMISSION_ID = "django-ray-cohort-probe-" + "a" * 64


def details_body(**changes):
    return json.dumps(
        {
            "type": "SUBMISSION",
            "submission_id": SUBMISSION_ID,
            "job_id": "01000000",
            "status": "SUCCEEDED",
            "entrypoint": "python -m django_ray.runtime.cohort_job",
            "metadata": {"django_ray_cohort_probe": "1"},
            "runtime_env": {"env_vars": {"PROBE_CONFIG": "one"}},
        }
        | changes,
        separators=(",", ":"),
    ).encode()


@pytest.fixture
def transport(monkeypatch):
    state = SimpleNamespace(
        now=100.0,
        initializations=[],
        requests=[],
        closed_responses=0,
        closed_sessions=0,
        reads=0,
        read_step=0.0,
        get_step=0.0,
        timeout_updates=0,
        last_timeout=None,
        body=details_body(),
        position=0,
        read_error=None,
        auth_error=None,
        get_error=None,
        expected_read_amount=1,
    )

    class Socket:
        def settimeout(self, seconds):
            assert 0 < seconds <= 5.0
            state.timeout_updates += 1
            state.last_timeout = seconds

    class Raw:
        chunked = False
        closed = False
        connection = SimpleNamespace(sock=Socket())

        def read(self, amount, *, decode_content):
            assert amount == state.expected_read_amount
            assert decode_content is False
            state.reads += 1
            state.now += state.read_step
            if state.read_error:
                raise state.read_error
            chunk = state.body[state.position : state.position + amount]
            state.position += len(chunk)
            if not chunk:
                self.closed = True
            return chunk

    class Response:
        status_code = 200
        headers = CaseInsensitiveDict()
        raw = Raw()

        @property
        def text(self):
            pytest.fail("unbounded response.text access")

        @property
        def content(self):
            pytest.fail("unbounded response.content access")

        def json(self):
            pytest.fail("unbounded response.json access")

        def close(self):
            state.closed_responses += 1

    class Session:
        def __init__(self):
            self.trust_env = True
            self.proxies = {"https": "https://unrelated-proxy.invalid"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            state.closed_sessions += 1

        def get(self, url, **kwargs):
            assert self.trust_env is False
            assert self.proxies == {}
            state.requests.append((url, kwargs))
            state.now += state.get_step
            if state.get_error:
                raise state.get_error
            return state.response

    class Client:
        def __init__(self, *, address):
            state.initializations.append(address)
            if state.auth_error:
                raise state.auth_error
            self._address = address
            self._headers = {"Authorization": "Bearer fixed-test-value"}
            self._cookies = {"session": "fixed-cookie"}
            self._verify = "fixed-ca-path"

        def _do_request(self, *args, **kwargs):
            pytest.fail("SDK eager request used")

        def _check_connection_and_version(self, *args, **kwargs):
            pytest.fail("SDK version probe used")

    state.response = Response()
    state.real_client = dashboard_sdk.SubmissionClient
    monkeypatch.setattr(http.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(dashboard_sdk, "SubmissionClient", Client)
    return state


def fetch(**changes):
    arguments = {"jobs_endpoint": ENDPOINT, "submission_id": SUBMISSION_ID}
    return http.fetch_reserved_cohort_job_details(**(arguments | changes))


def assert_refusal(error, reason):
    assert error.value.reason is reason
    assert str(error.value) == f"Cohort Job HTTP refused: {reason.value}"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def assert_closed(state):
    assert state.closed_responses == 1
    assert state.closed_sessions == 1


def test_fetch_uses_one_exact_endpoint_and_sdk_auth_with_no_ambient_proxy(transport, monkeypatch):
    state = transport
    monkeypatch.setenv("RAY_ADDRESS", "http://other-ray.invalid")
    monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "http://other-dashboard.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://other-proxy.invalid")
    monkeypatch.setenv("HTTP_PROXY", "http://other-proxy.invalid")
    monkeypatch.setenv("ALL_PROXY", "http://other-proxy.invalid")
    result = fetch()
    assert type(result) is JobDetails
    assert result.type is JobType.SUBMISSION
    assert result.status is JobStatus.SUCCEEDED
    assert result.submission_id == SUBMISSION_ID
    assert state.initializations == [ENDPOINT]
    assert state.requests == [
        (
            ENDPOINT + "/api/jobs/" + SUBMISSION_ID,
            {
                "headers": {
                    "Authorization": "Bearer fixed-test-value",
                    "Accept-Encoding": "identity",
                },
                "cookies": {"session": "fixed-cookie"},
                "verify": "fixed-ca-path",
                "proxies": {},
                "stream": True,
                "allow_redirects": False,
                "timeout": (5.0, 5.0),
            },
        )
    ]
    assert state.reads == len(state.body) + 1
    assert state.timeout_updates == state.reads
    assert_closed(state)


def test_real_sdk_configuration_constructor_never_probes_or_overrides_endpoint(
    transport, monkeypatch
):
    state = transport

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected SDK request or native Ray initialization")

    monkeypatch.setattr(dashboard_sdk, "SubmissionClient", state.real_client)
    monkeypatch.setattr(state.real_client, "_do_request", forbidden)
    monkeypatch.setattr(ray, "init", forbidden)
    monkeypatch.setattr(
        dashboard_sdk,
        "get_auth_headers_if_auth_enabled",
        lambda _headers: {"Authorization": "fixed-auth"},
    )
    monkeypatch.setenv("RAY_ADDRESS", "http://wrong.invalid")
    monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "http://wrong.invalid")
    fetch()
    url, kwargs = state.requests[0]
    assert url == ENDPOINT + "/api/jobs/" + SUBMISSION_ID
    assert kwargs["headers"]["Authorization"] == "fixed-auth"
    assert kwargs["verify"] is True
    assert_closed(state)


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "",
        "auto",
        "ray://head:10001",
        "ftp://head",
        "HTTP://head",
        "http://a:b@head",
        "http://head?",
        "http://head#",
        "http://head:0",
        "http://head/%2fapi",
        "http://head\\api",
        "http://head/\u0080",
    ],
)
def test_invalid_endpoint_refuses_before_client_construction(transport, endpoint):
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch(jobs_endpoint=endpoint)
    assert_refusal(error, http.CohortJobHttpReason.INVALID_ARGUMENT)
    assert transport.initializations == []
    assert transport.requests == []


@pytest.mark.parametrize(
    "submission", [None, "", "normal-job", SUBMISSION_ID + "/logs", SUBMISSION_ID.upper(), True]
)
def test_requires_exact_probe_submission_handle_before_network(transport, submission):
    with pytest.raises(http.CohortJobHttpError):
        fetch(submission_id=submission)
    assert transport.initializations == []


@pytest.mark.parametrize("timeout", [0, -1, True, None, float("inf"), float("nan"), 5.1, "5"])
def test_timeout_must_be_finite_positive_and_bounded(transport, timeout):
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch(timeout_seconds=timeout)
    assert_refusal(error, http.CohortJobHttpReason.INVALID_ARGUMENT)
    assert transport.initializations == []


def test_shorter_timeout_and_trailing_slash_use_exact_normalized_address(transport):
    fetch(jobs_endpoint=ENDPOINT + "/", timeout_seconds=0.5)
    assert transport.initializations == [ENDPOINT]
    assert transport.requests[0][1]["timeout"] == (0.5, 0.5)


def test_wrong_ray_version_refuses_before_auth_or_request(transport, monkeypatch):
    monkeypatch.setattr(ray, "__version__", "2.59.0")
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.UNSUPPORTED_RUNTIME)
    assert transport.initializations == []


@pytest.mark.parametrize("status", [True, 301, 302, 307, 308, 401, 403, 404, 500])
def test_redirect_and_error_responses_are_closed_without_reading_body(transport, status):
    transport.response.status_code = status
    transport.response.headers["Location"] = "https://different-host.invalid"
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.TRANSPORT_UNAVAILABLE)
    assert transport.reads == 0
    assert len(transport.requests) == 1
    assert_closed(transport)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "identity,gzip"])
def test_compressed_response_is_refused_without_decompression(transport, encoding):
    transport.response.headers["Content-Encoding"] = encoding
    transport.body = b"compressed bytes that could expand beyond the limit"
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.INVALID_RESPONSE)
    assert transport.reads == 0
    assert_closed(transport)


@pytest.mark.parametrize("header", [None, "chunked", "identity"])
def test_chunk_framing_is_not_allowed_to_hide_slow_unbounded_chunk_headers(transport, header):
    if header is None:
        transport.response.raw.chunked = True
    else:
        transport.response.headers["Transfer-Encoding"] = header
    with pytest.raises(http.CohortJobHttpError):
        fetch()
    assert transport.reads == 0
    assert_closed(transport)


@pytest.mark.parametrize("value", ["-1", "+1", "1,1", "one", "１２", "1" * 21])
def test_invalid_content_length_is_refused_before_body_read(transport, value):
    transport.response.headers["Content-Length"] = value
    with pytest.raises(http.CohortJobHttpError):
        fetch()
    assert transport.reads == 0
    assert_closed(transport)


def test_oversized_declared_length_is_refused_before_body_read(transport):
    transport.response.headers["Content-Length"] = str(http.COHORT_JOB_HTTP_MAX_BYTES + 1)
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.RESOURCE_LIMIT)
    assert transport.reads == 0
    assert_closed(transport)


@pytest.mark.parametrize("declared", [None, "2"])
def test_actual_body_cap_applies_even_without_or_with_false_small_length(transport, declared):
    transport.body = b" " * (http.COHORT_JOB_HTTP_MAX_BYTES + 1)
    if declared is not None:
        transport.response.headers["Content-Length"] = declared
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.RESOURCE_LIMIT)
    assert transport.reads == http.COHORT_JOB_HTTP_MAX_BYTES + 1
    assert_closed(transport)


def test_exact_byte_limit_is_accepted_for_a_valid_record(transport):
    transport.body += b" " * (http.COHORT_JOB_HTTP_MAX_BYTES - len(transport.body))
    transport.response.headers["Content-Length"] = str(http.COHORT_JOB_HTTP_MAX_BYTES)
    assert fetch().submission_id == SUBMISSION_ID
    assert_closed(transport)


def test_truncated_or_false_declared_length_is_not_accepted(transport):
    transport.response.headers["Content-Length"] = str(len(transport.body) + 1)
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.INVALID_RESPONSE)
    assert_closed(transport)


def test_total_progress_budget_stops_trickle_and_shrinks_socket_timeout(transport):
    transport.read_step = 1.0
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.TIMEOUT)
    assert transport.reads == 5
    assert transport.last_timeout == 1.0
    assert_closed(transport)


@pytest.mark.parametrize(
    "seconds,reason",
    [(5.0, http.CohortJobHttpReason.TIMEOUT), (-1.0, http.CohortJobHttpReason.CLOCK_REGRESSION)],
)
def test_header_wait_budget_is_checked_before_reading_body(transport, seconds, reason):
    transport.get_step = seconds
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, reason)
    assert transport.reads == 0
    assert_closed(transport)


def test_connection_close_socket_remains_owned_by_response_buffer(transport):
    raw = transport.response.raw
    sock = raw.connection.sock
    raw.connection = None
    raw._fp = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=sock)))
    assert fetch().submission_id == SUBMISSION_ID
    assert transport.timeout_updates > 0
    assert_closed(transport)


def test_unsupported_live_socket_transport_does_not_drop_read_deadline(transport):
    transport.response.raw.connection = None
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.TRANSPORT_UNAVAILABLE)
    assert transport.reads == 0
    assert_closed(transport)


@pytest.mark.parametrize("phase", ["auth", "get", "read"])
def test_transport_exceptions_never_echo_sensitive_details(transport, phase):
    setattr(transport, phase + "_error", RuntimeError("password=private-secret"))
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.TRANSPORT_UNAVAILABLE)
    assert "private-secret" not in str(error.value)
    assert transport.closed_sessions == (0 if phase == "auth" else 1)
    assert transport.closed_responses == (1 if phase == "read" else 0)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json private-secret",
        b"\xff",
        b"[]",
        b"null",
        b'{"metadata":{"a":1,"a":2}}',
        b'{"status":"RUNNING","status":"SUCCEEDED"}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":9223372036854775808}',
        b'{"x":' + b"1" * 100 + b"}",
        b"[" * 17 + b"0" + b"]" * 17,
        b'{"x":"\\u0000"}',
        b'{"x":[' + b"0," * 8200 + b"0]}",
    ],
    ids=[
        "invalid",
        "utf8",
        "list",
        "null",
        "nested-duplicate",
        "duplicate",
        "nan",
        "infinity",
        "overflow-float",
        "overflow-integer",
        "large-integer",
        "depth",
        "nul",
        "nodes",
    ],
)
def test_untrusted_json_is_bounded_and_validated_before_typed_response(transport, payload):
    transport.body = payload
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert "private-secret" not in str(error.value)
    assert_closed(transport)


@pytest.mark.parametrize(
    "change",
    [
        {"submission_id": "other-handle"},
        {"submission_id": True},
        {"start_time": "100"},
        {"driver_exit_code": True},
    ],
)
def test_typed_response_cannot_coerce_or_substitute_bound_job_fields(transport, change):
    transport.body = details_body(**change)
    with pytest.raises(http.CohortJobHttpError) as error:
        fetch()
    assert_refusal(error, http.CohortJobHttpReason.INVALID_RESPONSE)
    assert_closed(transport)


def test_http_module_import_is_pure_and_does_not_initialize_ray_or_django():
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'ray', 'requests'}:
            raise RuntimeError('forbidden runtime import')
sys.meta_path.insert(0, NoRuntime())
from django_ray.target.cohort_job_http import fetch_reserved_cohort_job_details, CohortJobHttpError
try:
    fetch_reserved_cohort_job_details('auto', 'invalid')
except CohortJobHttpError:
    pass
else:
    raise AssertionError('invalid endpoint accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
