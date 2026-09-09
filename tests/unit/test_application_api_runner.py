"""Exercise the Job transport and receipt boundary without starting Django or Ray."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from qualification.application import run_api


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
    origin, _, replies = server
    replies.extend([(status, {}, b"1234"), (status, {}, b"12345")])
    transport = run_api.ApplicationHttp(origin)
    assert transport("/api/metrics", method="GET", response_limit=4) == (status, b"1234")
    with pytest.raises(ValueError, match="byte limit"):
        transport("/api/metrics", method="GET", response_limit=4)


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
