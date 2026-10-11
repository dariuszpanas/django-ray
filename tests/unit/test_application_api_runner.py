"""Exercise the Job transport and receipt boundary without starting Django or Ray."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualification.application import run_api
from qualification.application.http_context import (
    OUTER_DEADLINE_ENV,
    HttpObserver,
    local_outer_deadline,
)


def failure_taxonomy(error):
    return {
        key: value
        for key, value in error.diagnostic().items()
        if key in {"code", "status", "transport_failure"}
    }


@pytest.mark.parametrize(
    "media_type,accepted",
    [("application/json; charset=utf-8", True), ("text/html", False), ("", False)],
)
def test_http_transport_checks_media_type_before_returning_body(server, media_type, accepted):
    url, requests, replies = server
    replies.append((200, {"Content-Type": media_type}, b"{}"))
    transport = run_api.ApplicationHttp(url)
    if accepted:
        assert transport(
            "/admin/graph", method="GET", required_media_types=frozenset({"application/json"})
        ) == (200, b"{}")
    else:
        with pytest.raises(ValueError, match="media type"):
            transport(
                "/admin/graph", method="GET", required_media_types=frozenset({"application/json"})
            )
    assert len(requests) == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1024 * 1024 + 1])
def test_http_transport_refuses_invalid_explicit_body_limit_before_request(server, limit):
    url, requests, _replies = server
    with pytest.raises(ValueError, match="response limit"):
        run_api.ApplicationHttp(url)("/admin/", method="GET", response_limit=limit)
    assert requests == []


@pytest.fixture
def server():
    requests = []
    replies = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            status, headers, body = replies.pop(0)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.do_GET()

        def do_DELETE(self):
            self.do_GET()

        def log_message(self, *args, **kwargs):
            pass

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=service.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{service.server_port}", requests, replies
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    ("cache_control", "accepted"),
    [
        ("max-age=0, no-cache, no-store, must-revalidate, private", True),
        ("private, NO-STORE", True),
        ("private, no-cache", False),
        ("not-no-store", False),
        ("no-store=false", False),
        ("", False),
    ],
)
def test_http_transport_requires_exact_cache_directives(server, cache_control, accepted):
    origin, _, replies = server
    replies.append((200, {"Cache-Control": cache_control}, b"{}"))
    transport = run_api.ApplicationHttp(origin)
    if accepted:
        assert transport(
            "/admin/example/", method="GET", required_cache_directives=frozenset({"no-store"})
        ) == (200, b"{}")
    else:
        with pytest.raises(ValueError, match="cache directives"):
            transport(
                "/admin/example/", method="GET", required_cache_directives=frozenset({"no-store"})
            )


def test_http_transport_ignores_proxies_and_does_not_follow_redirects(server, monkeypatch):
    origin, requests, replies = server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    replies.append((302, {"Location": "http://127.0.0.1:1/credential-sink"}, b"redirect"))
    transport = run_api.ApplicationHttp(origin)
    assert transport(
        "/api/executions", method="GET", headers={"Authorization": "Bearer private"}
    ) == (302, b"redirect")
    assert requests == [("/api/executions", "Bearer private")]
    assert transport.requests == 1


@pytest.mark.parametrize("status", [200, 403, 500])
def test_http_transport_enforces_limits_for_success_and_error_bodies(server, status):
    origin, requests, replies = server
    replies.extend([(status, {}, b"1234"), (status, {}, b"12345")])
    transport = run_api.ApplicationHttp(origin, retry_read_disconnects=True)
    assert transport("/api/metrics", method="GET", response_limit=4) == (status, b"1234")
    with pytest.raises(ValueError, match="byte limit"):
        transport("/api/metrics", method="GET", response_limit=4)
    assert len(requests) == 2


def test_http_transport_requires_response_headers(server):
    origin, _, replies = server
    replies.extend([(200, {"Cache-Control": "no-store"}, b"{}"), (200, {}, b"{}")])
    transport = run_api.ApplicationHttp(origin)
    required = {"Cache-Control": "no-store"}
    assert transport("/api/tasks/example", method="GET", required_response_headers=required) == (
        200,
        b"{}",
    )
    with pytest.raises(ValueError, match="headers did not match"):
        transport("/api/tasks/example", method="GET", required_response_headers=required)


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://app",
        "http://user:secret@app",
        "http://app/path",
        "http://app?x=1",
        "http://app#fragment",
        "http://app?",
        "http://app#",
        "http://app\\foreign",
        " http://app",
        "http://app\n",
        "http://",
        "http://app:99999",
    ],
)
def test_http_transport_rejects_non_origin_urls(origin):
    with pytest.raises(ValueError):
        run_api.ApplicationHttp(origin)


@pytest.mark.parametrize(
    "path",
    [
        "https://foreign/api",
        "//foreign/api",
        "api/tasks",
        "/api\\foreign",
        "/api#fragment",
        "/api\r\nHost: foreign",
    ],
)
def test_http_transport_rejects_escaping_paths_before_connecting(path):
    transport = run_api.ApplicationHttp("http://127.0.0.1:1")
    with pytest.raises(ValueError, match="origin-relative"):
        transport(path, method="GET")
    assert transport.requests == 0


def test_http_failure_closes_connection_and_discards_private_exception(monkeypatch):
    closed = []

    class BrokenConnection:
        def __init__(self, *args, **kwargs):
            assert kwargs["timeout"] == 0.1

        def request(self, *args, **kwargs):
            raise OSError("private-request-header")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(http.client, "HTTPConnection", BrokenConnection)
    with pytest.raises(ValueError, match="Application HTTP request failed") as caught:
        run_api.ApplicationHttp("http://app", request_timeout=0.1)("/api", method="GET")
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert closed == [True]
    assert failure_taxonomy(caught.value) == {
        "code": "request_failed",
        "status": None,
        "transport_failure": "os_error",
    }


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_http_read_failures_keep_only_fixed_stage_and_status(monkeypatch, stage):
    class Response:
        status = 200

        def getheader(self, _name):
            return None

        def read(self, _limit):
            raise OSError("secret response body")

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            if stage == "headers":
                raise http.client.RemoteDisconnected("secret header")
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        run_api.ApplicationHttp("http://app")("/private", method="GET")
    assert failure_taxonomy(caught.value) == {
        "code": "response_headers_failed" if stage == "headers" else "body_read_failed",
        "status": None if stage == "headers" else 200,
        "transport_failure": "remote_disconnected" if stage == "headers" else "os_error",
    }
    assert "secret" not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "options,code",
    [
        ({"required_response_headers": {"X-Test": "required"}}, "required_headers_mismatch"),
        ({"required_cache_directives": frozenset({"no-store"})}, "cache_directives_mismatch"),
        ({"required_media_types": frozenset({"application/json"})}, "media_type_mismatch"),
        ({"response_limit": 1}, "body_limit_exceeded"),
    ],
)
def test_http_contract_failure_categories_exclude_private_response(server, options, code):
    origin, requests, replies = server
    replies.append((200, {"X-Test": "private-header"}, b"private-body"))
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        run_api.ApplicationHttp(origin, retry_read_disconnects=True)(
            "/private", method="GET", **options
        )
    assert failure_taxonomy(caught.value) == {"code": code, "status": 200}
    assert "private" not in str(caught.value)
    assert len(requests) == 1


@pytest.fixture
def scripted_http(monkeypatch):
    state = SimpleNamespace(now=0.0, replies=[], timeouts=[], requests=[], closed=[])

    class Response:
        status = 200

        def __init__(self, reply):
            self.reply = reply

        def getheader(self, _name):
            return None

        def read(self, _limit):
            if self.reply.get("stage") == "body":
                raise self.reply["error"]
            return b"{}"

    class Connection:
        def __init__(self, _hostname, _port, *, timeout):
            state.timeouts.append(timeout)
            self.reply = state.replies.pop(0)

        def request(self, method, path, *, headers):
            state.requests.append((method, path, headers))
            if self.reply.get("stage") == "request":
                raise self.reply["error"]

        def getresponse(self):
            state.now += self.reply.get("elapsed", 0)
            if self.reply.get("stage") == "headers":
                raise self.reply["error"]
            return Response(self.reply)

        def close(self):
            state.closed.append(True)

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(run_api.time, "monotonic", lambda: state.now)
    return state


@pytest.mark.parametrize(
    "error_type,category",
    [
        (http.client.RemoteDisconnected, "remote_disconnected"),
        (ConnectionResetError, "connection_reset"),
    ],
)
def test_read_observer_recovers_one_pre_status_disconnect_with_remaining_budget(
    scripted_http, error_type, category
):
    scripted_http.replies = [
        {"stage": "headers", "error": error_type("private header"), "elapsed": 2.5},
        {},
    ]
    transport = run_api.ApplicationHttp("http://app", retry_read_disconnects=True)
    assert transport("/private", method="GET", headers={"Authorization": "private"}) == (200, b"{}")
    assert scripted_http.timeouts == [10, 7.5]
    assert scripted_http.requests == [("GET", "/private", {"Authorization": "private"})] * 2
    assert scripted_http.closed == [True, True]
    assert transport.requests == 2
    assert transport.transport_recoveries[category] == 1
    assert sum(transport.transport_recoveries.values()) == 1


def test_read_observer_stops_after_two_disconnects(scripted_http):
    scripted_http.replies = [
        {"stage": "headers", "error": http.client.RemoteDisconnected("private")} for _ in range(2)
    ]
    transport = run_api.ApplicationHttp("http://app", retry_read_disconnects=True)
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        transport("/private", method="GET")
    assert failure_taxonomy(caught.value) == {
        "code": "response_headers_failed",
        "status": None,
        "transport_failure": "remote_disconnected",
    }
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)
    assert transport.requests == 2
    assert scripted_http.closed == [True, True]
    assert sum(transport.transport_recoveries.values()) == 0


@pytest.mark.parametrize(
    "method,enabled,stage,error_type,category",
    [
        ("GET", False, "headers", http.client.RemoteDisconnected, "remote_disconnected"),
        ("POST", True, "headers", http.client.RemoteDisconnected, "remote_disconnected"),
        ("DELETE", True, "headers", ConnectionResetError, "connection_reset"),
        ("GET", True, "request", ConnectionResetError, "connection_reset"),
        ("GET", True, "headers", TimeoutError, "timeout"),
        ("GET", True, "headers", http.client.BadStatusLine, "http_protocol_error"),
        ("GET", True, "headers", OSError, "os_error"),
        ("GET", True, "headers", ValueError, "invalid_value"),
        ("GET", True, "body", http.client.RemoteDisconnected, "remote_disconnected"),
        ("GET", True, "body", ConnectionResetError, "connection_reset"),
    ],
)
def test_read_retry_policy_does_not_retry_other_failures(
    scripted_http, method, enabled, stage, error_type, category
):
    scripted_http.replies = [{"stage": stage, "error": error_type("private dependency text")}]
    transport = run_api.ApplicationHttp("http://app", retry_read_disconnects=enabled)
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        transport("/private", method=method)
    assert caught.value.diagnostic()["transport_failure"] == category
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)
    assert transport.requests == 1
    assert scripted_http.closed == [True]
    assert sum(transport.transport_recoveries.values()) == 0


def test_read_retry_window_cannot_restart_after_budget_expires(scripted_http):
    scripted_http.replies = [
        {"stage": "headers", "error": http.client.RemoteDisconnected("private"), "elapsed": 10}
    ]
    transport = run_api.ApplicationHttp("http://app", retry_read_disconnects=True)
    with pytest.raises(run_api.ApplicationHttpError):
        transport("/private", method="GET")
    assert transport.requests == 1
    assert sum(transport.transport_recoveries.values()) == 0


@pytest.mark.parametrize("retry", [False, True])
def test_read_observer_rejects_late_success(scripted_http, retry):
    scripted_http.replies = (
        [{"stage": "headers", "error": ConnectionResetError("private"), "elapsed": 1}]
        if retry
        else []
    ) + [{"elapsed": 10}]
    transport = run_api.ApplicationHttp("http://app", retry_read_disconnects=True)
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        transport("/private", method="GET")
    assert failure_taxonomy(caught.value) == {
        "code": "body_read_failed",
        "status": 200,
        "transport_failure": "timeout",
    }
    assert transport.requests == 1 + retry
    assert sum(transport.transport_recoveries.values()) == 0


@pytest.mark.parametrize("policy", [1, None, "true"])
def test_read_retry_policy_requires_explicit_boolean(policy):
    with pytest.raises(ValueError, match="policy must be boolean"):
        run_api.ApplicationHttp("http://app", retry_read_disconnects=policy)


@pytest.mark.parametrize("status", [True, 99, 600, "private", None])
def test_http_failure_discards_invalid_status(status):
    error = run_api.ApplicationHttpError(
        "failure", code=run_api.HttpFailureCode.REQUEST, status=status
    )
    assert error.diagnostic() == {"code": "request_failed", "status": None}


@pytest.mark.parametrize("raw", [None, "private", "nan", "inf", "601", "1" * 33])
def test_unknown_outer_context_does_not_change_http_policy(monkeypatch, scripted_http, raw):
    if raw is None:
        monkeypatch.delenv(OUTER_DEADLINE_ENV, raising=False)
    else:
        monkeypatch.setenv(OUTER_DEADLINE_ENV, raw)
    assert local_outer_deadline() is None
    scripted_http.replies = [{"stage": "headers", "error": TimeoutError("private")}]
    transport = run_api.ApplicationHttp("http://app", observer=HttpObserver.WORKFLOW)
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        transport("/api/tasks/private", method="GET")
    assert caught.value.diagnostic()["outer_remaining_seconds"] is None
    assert scripted_http.timeouts == [10]


@pytest.mark.parametrize("observer", ["workflow", "private-token", None, True])
def test_observer_requires_finite_vocabulary(observer):
    with pytest.raises(ValueError, match="observer category"):
        run_api.ApplicationHttp("http://app", observer=observer)


@pytest.mark.parametrize("error_type", [http.client.RemoteDisconnected, ConnectionResetError])
def test_final_attempt_keeps_actual_budget_policy_and_redaction(
    monkeypatch, scripted_http, error_type
):
    monkeypatch.setenv(OUTER_DEADLINE_ENV, "100")
    scripted_http.replies = [
        {"stage": "headers", "error": error_type("private-token"), "elapsed": 2.5},
        {"stage": "headers", "error": TimeoutError("private-body"), "elapsed": 7.5},
    ]
    transport = run_api.ApplicationHttp(
        "http://app", retry_read_disconnects=True, observer=HttpObserver.WORKFLOW
    )
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        transport(
            "/api/tasks/private-id?secret=private-query",
            method="GET",
            headers={"Authorization": "private-token", "Cookie": "private-cookie"},
        )
    assert caught.value.diagnostic() == {
        "code": "response_headers_failed",
        "status": None,
        "transport_failure": "timeout",
        "observer": "workflow",
        "request_substage": "task_status",
        "method": "GET",
        "attempt": 2,
        "configured_timeout_seconds": 10,
        "attempt_timeout_seconds": 7.5,
        "retry_policy": "one_get_pre_status_disconnect_or_reset",
        "retry_remaining_seconds": 0,
        "outer_remaining_seconds": 90,
        "observer_remaining_seconds": None,
    }
    assert scripted_http.timeouts == [10, 7.5]
    assert transport.requests == 2
    assert sum(transport.transport_recoveries.values()) == 0
    assert caught.value.__context__ is None
    assert "private" not in json.dumps(caught.value.diagnostic()) + str(caught.value)


@pytest.mark.parametrize(
    "path,substage",
    [
        ("/api/tasks/private", "task_status"),
        ("/api/cluster/workflows/private?attempt_number=1", "workflow_summary"),
        ("/api/cluster/workflows/private/topology/nodes?cursor=private", "workflow_collection"),
        ("/api/cluster/workflows/private/topology/edges", "workflow_collection"),
        ("/api/cluster/workflows/private/nodes", "workflow_collection"),
        ("/api/cluster/workflows/private/node-detail?node_id=private", "workflow_collection"),
        ("/admin/django_ray/raytaskexecution/private/workflow/graph/", "admin_graph"),
        ("/admin/django_ray/raytaskexecution/private/change/", "admin_page"),
        ("/api/private?secret=private", "other"),
    ],
)
def test_route_labels_never_retain_dynamic_components(scripted_http, path, substage):
    scripted_http.replies = [{"stage": "headers", "error": TimeoutError("private")}]
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        run_api.ApplicationHttp("http://app")(path, method="GET")
    diagnostic = caught.value.diagnostic()
    assert diagnostic["request_substage"] == substage
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "error_type", [TimeoutError, http.client.RemoteDisconnected, ConnectionResetError]
)
@pytest.mark.parametrize("remaining", [2.0, 180.0])
def test_nested_admin_uses_own_deadline_without_adopting_main_retry_policy(
    monkeypatch, scripted_http, error_type, remaining
):
    from qualification.application import workflow_admin

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setenv(OUTER_DEADLINE_ENV, "100")
    scripted_http.replies = [{"stage": "headers", "error": error_type("private"), "elapsed": 1}]
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        workflow_admin.read_admin_text(
            workflow_admin.FIXTURE_ORIGIN,
            "/admin/django_ray/raytaskexecution/private/workflow/graph/?secret=private",
            headers={"Cookie": "private"},
            deadline=remaining,
        )
    diagnostic = caught.value.diagnostic()
    assert diagnostic["observer"] == "nested_admin"
    assert diagnostic["request_substage"] == "admin_graph"
    assert diagnostic["retry_policy"] == "none"
    assert diagnostic["retry_remaining_seconds"] is None
    assert diagnostic["outer_remaining_seconds"] == 99
    assert diagnostic["observer_remaining_seconds"] == remaining - 1
    assert diagnostic["configured_timeout_seconds"] == min(5, remaining)
    assert diagnostic["attempt_timeout_seconds"] == min(5, remaining)
    assert diagnostic["attempt"] == 1
    assert len(scripted_http.requests) == 1
    assert scripted_http.timeouts == [min(5, remaining)]
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    "error_type,category",
    [
        (TimeoutError, "timeout"),
        (http.client.RemoteDisconnected, "remote_disconnected"),
        (ConnectionResetError, "connection_reset"),
    ],
)
def test_workflow_main_propagates_real_transport_failure_with_caller(
    monkeypatch, scripted_http, tmp_path, capsys, nested, error_type, category
):
    from qualification.application import run_workflows, workflow_admin

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setenv(OUTER_DEADLINE_ENV, "120")
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr(run_workflows, "read_token", lambda _path: "private-token")
    case = run_workflows.workflow_cases()[0]
    monkeypatch.setattr(
        "testproject.admission.enqueue_sample",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="f717c512-17d7-4b5e-b778-d614fb14427c", args=[], kwargs=dict(case.options)
        ),
    )
    if nested:

        def observe(*_args, **_kwargs):
            return workflow_admin.read_admin_text(
                workflow_admin.FIXTURE_ORIGIN, "/admin/private/?secret=private", deadline=180
            )

        monkeypatch.setattr(run_workflows, "execute_case", observe)
    attempts = 1 if nested or error_type is TimeoutError else 2
    scripted_http.replies = [
        {"stage": "headers", "error": error_type("private"), "elapsed": 3} for _ in range(attempts)
    ]
    receipt = tmp_path / "receipt.json"
    assert run_workflows.main(["--token-file", "unused", "--receipt", str(receipt)]) == 1
    output = capsys.readouterr().out
    value = json.loads(output)
    diagnostic = value["http_failure"]
    assert value["failed_stage"] == case.name
    assert diagnostic["observer"] == ("nested_admin" if nested else "workflow")
    assert diagnostic["request_substage"] == ("admin_page" if nested else "task_status")
    assert diagnostic["configured_timeout_seconds"] == (5 if nested else 10)
    assert diagnostic["outer_remaining_seconds"] == 120 - 3 * attempts
    assert diagnostic["transport_failure"] == category
    assert diagnostic["attempt"] == attempts
    assert len(scripted_http.requests) == attempts
    assert value["http_transport_recoveries"] == {"remote_disconnected": 0, "connection_reset": 0}
    assert len(output.encode()) < 32768
    assert "private" not in output
    assert not receipt.exists()


def test_first_workflow_pair_preserves_typed_post_failure_without_replay(scripted_http):
    from qualification.application import run_first_workflows

    scripted_http.replies = [{"stage": "headers", "error": ConnectionResetError("private")}]
    request = run_api.ApplicationHttp("http://app", observer=HttpObserver.FIRST_WORKFLOW)
    receipt = run_first_workflows.observe_pair(request, token="private")
    assert receipt["status"] == "failed"
    assert len(receipt["trials"]) == 1
    failure = receipt["trials"][0]["http_failure"]
    assert failure["observer"] == "first_workflow"
    assert failure["method"] == "POST"
    assert failure["retry_policy"] == "none"
    assert failure["transport_failure"] == "connection_reset"
    assert len(scripted_http.requests) == 1
    assert len(json.dumps(receipt).encode()) < run_first_workflows.RECEIPT_MAX_BYTES
    assert "private" not in json.dumps(receipt)


@pytest.mark.parametrize("entrypoint", ["api", "core"])
def test_api_and_core_cli_preserve_real_typed_failure(
    monkeypatch, scripted_http, capsys, tmp_path, entrypoint
):
    from qualification.application import run_core

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr("django.setup", lambda: None)
    scripted_http.replies = [{"stage": "headers", "error": TimeoutError("private")}]
    args = ["--base-url", "http://app", "--token-file", "unused"]
    receipt = tmp_path / "receipt.json"
    if entrypoint == "core":
        args += ["--receipt", str(receipt)]
    assert (run_core if entrypoint == "core" else run_api).main(args) == 1
    output = capsys.readouterr().out
    failure = json.loads(output)["http_failure"]
    assert failure["observer"] == f"application_{entrypoint}"
    assert failure["method"] == "POST"
    assert failure["transport_failure"] == "timeout"
    assert len(scripted_http.requests) == 1
    assert "private" not in output
    assert not receipt.exists()


@pytest.mark.parametrize("deadline", [True, "private", float("nan"), float("inf"), 601])
def test_invalid_observer_deadline_is_unknown_without_changing_transport(scripted_http, deadline):
    scripted_http.replies = [{"stage": "headers", "error": TimeoutError("private")}]
    request = run_api.ApplicationHttp("http://app", observer_deadline=deadline)
    with pytest.raises(run_api.ApplicationHttpError) as caught:
        request("/admin/private/", method="GET")
    assert caught.value.diagnostic()["observer_remaining_seconds"] is None
    assert scripted_http.timeouts == [10]


@pytest.mark.parametrize("raw", [b"short", b"x" * 513, b"x" * 31 + b"\n", b"x" * 31 + b"\xff"])
def test_token_file_is_bounded_and_rejects_unsafe_headers(tmp_path, raw):
    path = tmp_path / "token"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="token file is missing or invalid") as caught:
        run_api.read_token(path)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_cli_real_failed_assertion_exits_nonzero_without_reading_missing_token(server, capsys):
    origin, requests, replies = server
    replies.append((200, {}, b"private-response"))
    assert run_api.main(["--base-url", origin, "--token-file", "missing-credential"]) == 1
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert receipt["status"] == "failed"
    assert receipt["requests"] == 1
    assert receipt["last_http_status"] == 200
    assert receipt["observations"]["task_id"] == ""
    assert receipt["complete_application_gate"] is False
    assert requests == [("/api/enqueue/add/2/3", None)]
    assert "private-response" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1", "601"])
def test_cli_rejects_invalid_task_timeout_without_connecting(timeout, capsys):
    assert (
        run_api.main(
            [
                "--base-url",
                "http://127.0.0.1:1",
                "--token-file",
                "missing",
                "--task-timeout",
                timeout,
            ]
        )
        == 1
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["requests"] == 0
    assert receipt["status"] == "failed"


def test_cli_receipt_requires_successful_shared_layer_return(tmp_path, monkeypatch, capsys):
    token = "never-print-this-application-token-123456"
    path = tmp_path / "token"
    path.write_text(token, encoding="ascii")
    should_fail = False

    def assertion(transport, *, get_token, task_timeout, evidence):
        assert get_token() == token
        assert task_timeout == 12
        assert isinstance(transport, run_api.ApplicationHttp)
        evidence.api_bulk_reset_absent = True
        if should_fail:
            raise ValueError("private-server-result-" + token)

    monkeypatch.setattr(run_api, "verify_application_api", assertion)
    args = ["--base-url", "https://app", "--token-file", str(path), "--task-timeout", "12"]
    assert run_api.main(args) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["status"] == "passed"
    assert success["complete_application_gate"] is False
    should_fail = True
    assert run_api.main(args) == 1
    captured = capsys.readouterr()
    failure = json.loads(captured.out)
    assert failure["status"] == "failed"
    assert failure["observations"]["api_bulk_reset_absent"] is True
    assert token not in captured.out
    assert "private-server-result" not in captured.out
    assert captured.err == ""


def test_cli_import_and_configuration_failure_need_only_standard_library():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from qualification.application.run_api import main; "
            "assert not any(name.split('.')[0] in "
            "{'django', 'django_ray', 'ray', 'kubernetes', 'scripts'} for name in sys.modules); "
            "raise SystemExit(main(['--base-url', 'invalid', '--token-file', 'missing']))",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "failed"
    assert result.stderr == ""
