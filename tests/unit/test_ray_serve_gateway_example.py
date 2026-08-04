"""Executable contract tests for the copyable Django-to-Serve gateway."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import time
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, override_settings
from django.urls import path
from django.views.decorators.csrf import ensure_csrf_cookie

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "docs" / "examples" / "ray_serve_gateway.py"


def _load_gateway() -> ModuleType:
    spec = importlib.util.spec_from_file_location("django_ray_docs_serve_gateway", EXAMPLE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gateway = _load_gateway()


@ensure_csrf_cookie
def _csrf_bootstrap(request: HttpRequest) -> HttpResponse:
    return HttpResponse(status=204)


urlpatterns = [
    path("api/classify", gateway.classify),
    path("csrf-bootstrap", _csrf_bootstrap),
]

_SESSION_CSRF_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]


@dataclass
class _UpstreamState:
    status: int = 200
    content_type: str | None = "application/json"
    body: bytes = b'{"model_revision":"model-v1","prediction":"positive"}'
    headers: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)


class _LoopbackServer(ThreadingHTTPServer):
    state: _UpstreamState


class _LoopbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        state = cast(_LoopbackServer, self.server).state
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        state.calls.append(
            {
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "request_id": self.headers.get("X-Request-ID"),
                "body": body,
                "path": self.path,
            }
        )
        if state.delay_seconds:
            time.sleep(state.delay_seconds)

        try:
            self.send_response(state.status)
            if state.content_type is not None:
                self.send_header("Content-Type", state.content_type)
            for name, value in state.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)
        except (OSError, ValueError):
            pass

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@contextmanager
def _loopback_upstream(**overrides: Any) -> Iterator[tuple[str, _UpstreamState]]:
    state = _UpstreamState(**overrides)
    server = _LoopbackServer(("127.0.0.1", 0), _LoopbackHandler)
    server.state = state
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}/v1/classify", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@dataclass(frozen=True)
class _User:
    is_authenticated: bool = True
    permitted: bool = True
    pk: object = 7

    def has_perm(self, permission: str) -> bool:
        assert permission == "myapp.use_model"
        return self.permitted


def _request(
    body: bytes | str = '{"text":"good"}',
    *,
    content_type: str = "application/json",
    user: _User | None = None,
    enforce_csrf: bool = False,
    valid_csrf: bool = False,
    method: str = "post",
) -> HttpRequest:
    factory = RequestFactory()
    if method == "post":
        request = factory.post("/api/classify", data=body, content_type=content_type)
    else:
        request = factory.generic(method.upper(), "/api/classify")
    request.user = user or _User()
    if valid_csrf:
        bootstrap = factory.get("/csrf-bootstrap")
        token = get_token(bootstrap)
        request.COOKIES[settings.CSRF_COOKIE_NAME] = bootstrap.META["CSRF_COOKIE"]
        request.META["HTTP_X_CSRFTOKEN"] = token
    elif not enforce_csrf:
        request._dont_enforce_csrf_checks = True
    return request


def _invoke(
    request: HttpRequest,
    url: str,
    *,
    token: str = "test-model-token",
    timeout: float = 1.0,
) -> HttpResponse:
    with override_settings(
        MODEL_SERVE_URL=url,
        MODEL_SERVE_TOKEN=token,
        MODEL_SERVE_TIMEOUT_SECONDS=timeout,
        CSRF_FAILURE_VIEW=f"{gateway.__name__}.csrf_failure",
    ):
        return gateway.classify(request)


def _payload(response: HttpResponse) -> dict[str, Any]:
    return json.loads(response.content)


def _assert_no_store(response: HttpResponse) -> None:
    assert "no-store" in response.headers["Cache-Control"]


def _assert_rejection_logged(
    caplog: pytest.LogCaptureFixture,
    response: HttpResponse,
    expected_error: str,
) -> None:
    records = [
        record
        for record in caplog.records
        if record.name == gateway.__name__
        and record.getMessage() == "model gateway request rejected"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.request_id == _payload(response)["request_id"]
    assert record.failure_code == expected_error
    assert record.status_code == response.status_code


def test_gateway_opts_out_of_request_wide_database_transactions() -> None:
    assert gateway.classify._non_atomic_requests == {"default"}


def test_gateway_success_is_no_store_bounded_and_does_not_log_canaries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_canary = "INPUT-CANARY-DO-NOT-LOG"
    output_canary = "OUTPUT-CANARY-DO-NOT-LOG"
    token_canary = "TOKEN-CANARY-DO-NOT-LOG"
    user_id = "user-" + "x" * 500
    upstream_body = json.dumps({"model_revision": "model-v1", "prediction": output_canary}).encode()

    with _loopback_upstream(body=upstream_body) as (url, state):
        with caplog.at_level(logging.INFO, logger=gateway.__name__):
            response = _invoke(
                _request(
                    json.dumps({"text": input_canary}),
                    user=_User(pk=user_id),
                    valid_csrf=True,
                ),
                url,
                token=token_canary,
            )

    assert response.status_code == 200
    _assert_no_store(response)
    public_payload = _payload(response)
    assert public_payload["prediction"] == output_canary
    assert len(state.calls) == 1
    call = state.calls[0]
    assert call["authorization"] == f"Bearer {token_canary}"
    assert call["request_id"] == public_payload["request_id"]
    assert json.loads(call["body"])["text"] == input_canary

    completed_record = next(
        record for record in caplog.records if record.getMessage() == "model inference completed"
    )
    assert completed_record.user_id == user_id[: gateway.MAX_LOG_IDENTIFIER_CHARACTERS]
    captured = "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)
    assert public_payload["request_id"] in captured
    assert input_canary not in captured
    assert output_canary not in captured
    assert token_canary not in captured
    assert user_id not in captured


def test_gateway_failure_log_omits_input_token_and_raw_upstream_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_canary = "FAILED-INPUT-CANARY-DO-NOT-LOG"
    upstream_canary = "RAW-UPSTREAM-CANARY-DO-NOT-LOG"
    token_canary = "FAILED-TOKEN-CANARY-DO-NOT-LOG"

    with _loopback_upstream(status=500, body=upstream_canary.encode()) as (url, _state):
        with caplog.at_level(logging.WARNING, logger=gateway.__name__):
            response = _invoke(
                _request(json.dumps({"text": input_canary})),
                url,
                token=token_canary,
            )

    assert response.status_code == 502
    captured = "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)
    assert "model inference failed" in captured
    assert _payload(response)["request_id"] in captured
    assert input_canary not in captured
    assert upstream_canary not in captured
    assert token_canary not in captured


@pytest.mark.parametrize(
    ("user", "expected_status", "expected_error"),
    [
        (_User(is_authenticated=False), 403, "authentication_required"),
        (_User(permitted=False), 403, "permission_denied"),
    ],
)
def test_gateway_auth_failures_are_fixed_no_store_and_do_not_call_serve(
    user: _User,
    expected_status: int,
    expected_error: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _loopback_upstream() as (url, state):
        with caplog.at_level(logging.INFO, logger=gateway.__name__):
            response = _invoke(_request(user=user), url)

    assert response.status_code == expected_status
    assert _payload(response)["error"] == expected_error
    _assert_no_store(response)
    _assert_rejection_logged(caplog, response, expected_error)
    assert state.calls == []


def test_gateway_rejection_audit_is_bounded_and_excludes_request_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_canary = "REJECTED-INPUT-CANARY-DO-NOT-LOG"
    user_id = "user-" + "x" * 500

    with _loopback_upstream() as (url, state):
        with caplog.at_level(logging.INFO, logger=gateway.__name__):
            response = _invoke(
                _request(input_canary, user=_User(pk=user_id)),
                url,
            )

    assert response.status_code == 400
    _assert_rejection_logged(caplog, response, "invalid_request")
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "model gateway request rejected"
    )
    assert record.user_id == user_id[: gateway.MAX_LOG_IDENTIFIER_CHARACTERS]
    captured = "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)
    assert input_canary not in captured
    assert user_id not in captured
    assert state.calls == []


def test_gateway_enforces_session_csrf_and_does_not_call_serve(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _loopback_upstream() as (url, state):
        with caplog.at_level(logging.INFO, logger=gateway.__name__):
            response = _invoke(_request(enforce_csrf=True), url)

    assert response.status_code == 403
    _assert_no_store(response)
    assert response.headers["Content-Type"].startswith("application/json")
    assert set(_payload(response)) == {"error", "request_id"}
    assert _payload(response)["error"] == "csrf_failed"
    _assert_rejection_logged(caplog, response, "csrf_failed")
    assert state.calls == []


@pytest.mark.django_db
def test_gateway_real_session_accepts_valid_csrf_and_rejects_missing_or_wrong_token() -> None:
    user_model = get_user_model()
    user = user_model.objects.create_user(username="gateway-user", password="unused")
    content_type = ContentType.objects.create(app_label="myapp", model="modelgateway")
    permission = Permission.objects.create(
        name="Can use model gateway",
        codename="use_model",
        content_type=content_type,
    )
    user.user_permissions.add(permission)

    with _loopback_upstream() as (url, state):
        with override_settings(
            ROOT_URLCONF=__name__,
            MIDDLEWARE=_SESSION_CSRF_MIDDLEWARE,
            ALLOWED_HOSTS=["testserver"],
            MODEL_SERVE_URL=url,
            MODEL_SERVE_TOKEN="session-test-token",
            MODEL_SERVE_TIMEOUT_SECONDS=1.0,
            CSRF_FAILURE_VIEW=f"{gateway.__name__}.csrf_failure",
        ):
            valid_client = Client(enforce_csrf_checks=True)
            valid_client.force_login(user)
            assert valid_client.get("/csrf-bootstrap").status_code == 204
            csrf_token = valid_client.cookies[settings.CSRF_COOKIE_NAME].value
            accepted = valid_client.post(
                "/api/classify",
                data=json.dumps({"text": "good"}),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf_token,
            )

            missing_client = Client(enforce_csrf_checks=True)
            missing_client.force_login(user)
            missing = missing_client.post(
                "/api/classify",
                data=json.dumps({"text": "good"}),
                content_type="application/json",
            )
            incorrect = valid_client.post(
                "/api/classify",
                data=json.dumps({"text": "good"}),
                content_type="application/json",
                HTTP_X_CSRFTOKEN="x" * len(csrf_token),
            )

    assert accepted.status_code == 200
    _assert_no_store(accepted)
    assert _payload(accepted)["prediction"] == "positive"
    assert len(state.calls) == 1

    for rejected in (missing, incorrect):
        assert rejected.status_code == 403
        _assert_no_store(rejected)
        assert rejected.headers["Content-Type"].startswith("application/json")
        assert set(_payload(rejected)) == {"error", "request_id"}
        assert _payload(rejected)["error"] == "csrf_failed"


@pytest.mark.parametrize(
    ("status", "headers", "expected_status", "expected_error"),
    [
        (302, {}, 502, "model_protocol_error"),
        (400, {}, 502, "model_contract_rejected"),
        (408, {}, 504, "model_timeout"),
        (413, {}, 502, "model_contract_rejected"),
        (415, {}, 502, "model_contract_rejected"),
        (422, {}, 502, "model_contract_rejected"),
        (429, {}, 503, "model_overloaded"),
        (500, {}, 502, "model_error"),
        (503, {}, 503, "model_unavailable"),
        (503, {"Retry-After": "1"}, 503, "model_unavailable"),
    ],
)
def test_gateway_maps_upstream_statuses_to_fixed_no_store_failures(
    status: int,
    headers: dict[str, str],
    expected_status: int,
    expected_error: str,
) -> None:
    with _loopback_upstream(status=status, headers=headers) as (url, _state):
        response = _invoke(_request(), url)

    assert response.status_code == expected_status
    assert set(_payload(response)) == {"error", "request_id"}
    assert _payload(response)["error"] == expected_error
    _assert_no_store(response)


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"content_type": "text/plain"}, "model_protocol_error"),
        ({"body": b"not-json"}, "model_response_invalid"),
        (
            {"body": b'{"model_revision":' + b"1" * 5_000 + b"}"},
            "model_response_invalid",
        ),
        (
            {"body": b"[" * 5_000 + b"0" + b"]" * 5_000},
            "model_response_invalid",
        ),
        ({"body": b"x" * (32 * 1024 + 1)}, "model_response_too_large"),
        ({"body": b'{"model_revision":"model-v1"}'}, "model_response_invalid"),
        (
            {"body": b'{"model_revision":"model-v1","prediction":"bad\\nvalue"}'},
            "model_response_invalid",
        ),
    ],
)
def test_gateway_rejects_invalid_or_oversized_model_responses(
    overrides: dict[str, Any], expected_error: str
) -> None:
    with _loopback_upstream(**overrides) as (url, _state):
        response = _invoke(_request(), url)

    assert response.status_code == 502
    assert _payload(response)["error"] == expected_error
    _assert_no_store(response)


@pytest.mark.parametrize("parser_error", [ValueError, RecursionError])
def test_gateway_maps_model_json_parser_limits_to_fixed_failure(
    parser_error: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    class _SecondParseFails:
        calls = 0
        dumps = staticmethod(json.dumps)

        @classmethod
        def loads(cls, body: bytes) -> dict[str, str]:
            cls.calls += 1
            if cls.calls == 1:
                return {"text": "good"}
            raise parser_error("bounded parser failure")

    monkeypatch.setattr(gateway, "json", _SecondParseFails)
    with _loopback_upstream() as (url, state):
        response = _invoke(_request(), url)

    assert _SecondParseFails.calls == 2
    assert len(state.calls) == 1
    assert response.status_code == 502
    assert _payload(response)["error"] == "model_response_invalid"
    _assert_no_store(response)


def test_gateway_timeout_is_fixed_and_no_store() -> None:
    with _loopback_upstream(delay_seconds=0.2) as (url, _state):
        response = _invoke(_request(), url, timeout=0.03)

    assert response.status_code == 504
    assert _payload(response)["error"] == "model_timeout"
    _assert_no_store(response)


def test_gateway_does_not_forward_credentials_across_redirects() -> None:
    token = "REDIRECT-TOKEN-CANARY"
    with _loopback_upstream() as (target_url, target_state):
        with _loopback_upstream(
            status=302,
            headers={"Location": target_url},
            body=b"",
        ) as (redirect_url, redirect_state):
            response = _invoke(_request(), redirect_url, token=token)

    assert response.status_code == 502
    assert _payload(response)["error"] == "model_protocol_error"
    _assert_no_store(response)
    assert redirect_state.calls[0]["authorization"] == f"Bearer {token}"
    assert target_state.calls == []


def test_gateway_maps_connection_refusal_without_exposing_raw_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RefusingOpener:
        def open(self, *args: object, **kwargs: object) -> None:
            raise urllib.error.URLError(ConnectionRefusedError("loopback refused"))

    monkeypatch.setattr(gateway, "model_service_opener", _RefusingOpener())
    response = _invoke(_request(), "http://127.0.0.1:1/v1/classify")

    assert response.status_code == 503
    assert set(_payload(response)) == {"error", "request_id"}
    assert _payload(response)["error"] == "model_unavailable"
    _assert_no_store(response)


@pytest.mark.parametrize(
    ("incoming_request", "expected_status", "expected_error"),
    [
        (_request("not-json"), 400, "invalid_request"),
        (
            _request(b'{"text":' + b"1" * 5_000 + b"}"),
            400,
            "invalid_request",
        ),
        (
            _request(b'{"text":' + b"[" * 5_000 + b"0" + b"]" * 5_000 + b"}"),
            400,
            "invalid_request",
        ),
        (_request("{}"), 400, "invalid_request"),
        (_request('{"text":""}'), 400, "invalid_text"),
        (_request('{"text":1}'), 400, "invalid_request"),
        (_request(b"x" * (16 * 1024 + 1)), 413, "request_too_large"),
        (_request(content_type="text/plain"), 415, "content_type_not_supported"),
    ],
)
def test_gateway_rejects_invalid_inbound_requests_before_serve(
    incoming_request: HttpRequest,
    expected_status: int,
    expected_error: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _loopback_upstream() as (url, state):
        with caplog.at_level(logging.INFO, logger=gateway.__name__):
            response = _invoke(incoming_request, url)

    assert response.status_code == expected_status
    assert _payload(response)["error"] == expected_error
    _assert_no_store(response)
    _assert_rejection_logged(caplog, response, expected_error)
    assert state.calls == []


@pytest.mark.parametrize("parser_error", [ValueError, RecursionError])
def test_gateway_maps_inbound_json_parser_limits_to_fixed_failure(
    parser_error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingParser:
        dumps = staticmethod(json.dumps)

        @staticmethod
        def loads(body: bytes) -> dict[str, str]:
            raise parser_error("bounded parser failure")

    monkeypatch.setattr(gateway, "json", _FailingParser)
    with _loopback_upstream() as (url, state):
        with caplog.at_level(logging.INFO, logger=gateway.__name__):
            response = _invoke(_request(), url)

    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_request"
    _assert_no_store(response)
    _assert_rejection_logged(caplog, response, "invalid_request")
    assert state.calls == []


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete", "options"])
def test_gateway_rejects_non_post_methods_with_explicit_json(
    method: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=gateway.__name__):
        response = _invoke(_request(method=method), "http://127.0.0.1:1/v1/classify")

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    assert response.headers["Content-Type"].startswith("application/json")
    assert set(_payload(response)) == {"error", "request_id"}
    assert _payload(response)["error"] == "method_not_allowed"
    _assert_no_store(response)
    _assert_rejection_logged(caplog, response, "method_not_allowed")


@pytest.mark.parametrize(("method", "has_body"), [("get", True), ("head", False)])
def test_gateway_real_django_request_preserves_head_semantics(method: str, has_body: bool) -> None:
    with override_settings(
        ROOT_URLCONF=__name__,
        MIDDLEWARE=_SESSION_CSRF_MIDDLEWARE,
        ALLOWED_HOSTS=["testserver"],
    ):
        response = getattr(Client(enforce_csrf_checks=True), method)("/api/classify")

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    assert response.headers["Content-Type"].startswith("application/json")
    if has_body:
        assert set(_payload(response)) == {"error", "request_id"}
        assert _payload(response)["error"] == "method_not_allowed"
    else:
        assert response.content == b""
    _assert_no_store(response)


def test_gateway_real_django_put_without_csrf_reaches_explicit_json_405() -> None:
    with override_settings(
        ROOT_URLCONF=__name__,
        MIDDLEWARE=_SESSION_CSRF_MIDDLEWARE,
        ALLOWED_HOSTS=["testserver"],
    ):
        response = Client(enforce_csrf_checks=True).put(
            "/api/classify",
            data=b'"ignored"',
            content_type="application/json",
        )

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    assert response.headers["Content-Type"].startswith("application/json")
    assert set(_payload(response)) == {"error", "request_id"}
    assert _payload(response)["error"] == "method_not_allowed"
    _assert_no_store(response)


def test_gateway_does_not_use_django_require_post_decorator() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert "require_POST" not in source
