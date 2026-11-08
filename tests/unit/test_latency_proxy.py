"""Exercise the real proxy handler without sockets, Ray, or child processes."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import Mock

import pytest

from qualification.latency import processes


@pytest.fixture
def proxy_handler(monkeypatch):
    class Server:
        server_port = 9999

        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 0)
            self.handler = handler

        def serve_forever(self):
            pass

    class Response(io.BytesIO):
        status = 200

    opener = Mock()
    opener.open.return_value = Response(b'{"accepted":true}')
    build_opener = Mock(return_value=opener)
    monkeypatch.setattr(processes, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(processes.threading, "Thread", Mock())
    monkeypatch.setattr(processes.urllib.request, "build_opener", build_opener)
    proxy = processes.JobsProxy("http://127.0.0.1:8265")

    def forward(headers=None):
        handler = object.__new__(proxy.server.handler)
        handler.headers = Message()
        for name, value in (headers or {}).items():
            handler.headers[name] = value
        handler.path = "/api/version"
        handler.command = "GET"
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.forward()
        return statuses, handler.wfile.getvalue()

    return proxy, opener, build_opener, forward


@pytest.mark.parametrize("header", ["Authorization", "authorization", "X-Ray-Authorization"])
def test_proxy_forwards_only_allowed_authentication_without_retaining_it(proxy_handler, header):
    proxy, opener, build_opener, forward = proxy_handler
    credential = "Bearer synthetic-qualification-token"
    statuses, body = forward({header: credential, "Cookie": "unrelated-cookie"})

    assert statuses == [200] and body == b'{"accepted":true}'
    request = opener.open.call_args.args[0]
    forwarded = {name.lower(): value for name, value in request.header_items()}
    assert forwarded == {"content-type": "application/json", header.lower(): credential}
    assert request.full_url == "http://127.0.0.1:8265/api/version"
    assert opener.open.call_args.kwargs == {"timeout": 5}
    handlers = build_opener.call_args.args
    assert isinstance(handlers[0], urllib.request.ProxyHandler) and handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, 302, "", {}, "http://elsewhere") is None
    assert credential not in json.dumps(proxy.requests)
    assert proxy.failure_diagnostics() == {"observed_requests": 1, "http_status_counts": {"200": 1}}


@pytest.mark.parametrize("status", [401, 403, 302])
def test_proxy_preserves_refusal_status_without_raw_upstream_error(proxy_handler, status):
    proxy, opener, _, forward = proxy_handler
    opener.open.side_effect = urllib.error.HTTPError(
        "http://127.0.0.1:8265/api/version", status, "private response", {}, io.BytesIO(b"private")
    )

    statuses, body = forward()

    assert statuses == [status]
    assert body == b'{"error":"qualification upstream request failed"}'
    assert set(dict(opener.open.call_args.args[0].header_items())) == {"Content-type"}
    assert proxy.failure_diagnostics()["http_status_counts"] == {str(status): 1}
    assert "private" not in json.dumps(proxy.requests) + json.dumps(proxy.failure_diagnostics())


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("private failure"),
        TimeoutError("private failure"),
        ConnectionError("private failure"),
    ],
)
def test_proxy_records_safe_connection_failure_status(proxy_handler, error):
    proxy, opener, _, forward = proxy_handler
    opener.open.side_effect = error

    statuses, body = forward({"Authorization": "Bearer synthetic-qualification-token"})

    assert statuses == [502]
    assert body == b'{"error":"qualification upstream request failed"}'
    assert proxy.failure_diagnostics() == {"observed_requests": 1, "http_status_counts": {"502": 1}}


@pytest.mark.parametrize("status", [None, "200", False, 99, 600])
def test_proxy_refuses_invalid_upstream_status_as_safe_gateway_failure(proxy_handler, status):
    proxy, opener, _, forward = proxy_handler
    opener.open.return_value.status = status

    statuses, body = forward()

    assert statuses == [502]
    assert body == b'{"error":"qualification upstream request failed"}'
    assert proxy.failure_diagnostics() == {"observed_requests": 1, "http_status_counts": {"502": 1}}


def test_outage_remains_local_and_does_not_forward_credentials(proxy_handler):
    proxy, opener, _, forward = proxy_handler
    proxy.unavailable.set()

    statuses, body = forward({"Authorization": "Bearer synthetic-qualification-token"})

    assert statuses == [503] and body == b'{"error":"qualification outage"}'
    opener.open.assert_not_called()
    assert proxy.failure_diagnostics() == {"observed_requests": 1, "http_status_counts": {"503": 1}}


@pytest.mark.parametrize(
    "upstream",
    [
        "http://127.0.0.1:8265@elsewhere.invalid",
        "http://user@127.0.0.1:8265",
        "http://localhost:8265",
        "https://127.0.0.1:8265",
        "http://127.0.0.1:8265/path",
        "http://127.0.0.1:8265?token=private",
        "http://127.0.0.1:8265#fragment",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
    ],
)
def test_proxy_refuses_unowned_upstream_before_server_start(monkeypatch, upstream):
    server = Mock()
    monkeypatch.setattr(processes, "ThreadingHTTPServer", server)
    with pytest.raises(ValueError, match="owned loopback"):
        processes.JobsProxy(upstream)
    server.assert_not_called()
