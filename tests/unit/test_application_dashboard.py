"""Identity and routing boundaries for the disposable dashboard browser proof."""

import http.client
import io
import json
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from django.test import override_settings

from qualification.application import dashboard_browser, dashboard_proxy
from qualification.application.dashboard_browser import validate_packet, validate_task_response
from qualification.application.dashboard_proxy import upstream_path


def packet():
    return {
        "origin": "http://django-web:8000",
        "execution_pk": 1,
        "job_id": "01000000",
        "task_id": "a" * 48,
        "cookie": "sessionid=fixture",
    }


@pytest.mark.parametrize(
    "target",
    [
        "http://other/ray/api/v0/tasks",
        "//other/ray/",
        "/api/v0/tasks",
        "/ray/../api/v0/tasks",
        "/ray/%2e%2e/api/v0/tasks",
        "/ray/%5cother",
        "/ray/%00",
        "/ray/#fragment",
        "/ray/\r\nheader:value",
        "/ray/" + "x" * 4096,
    ],
)
def test_proxy_rejects_other_origins_and_paths(target):
    with pytest.raises(ValueError):
        upstream_path(target)


def test_proxy_preserves_base_relative_assets_and_encoded_task_filter():
    assert upstream_path("/ray/static/js/main.js") == "/static/js/main.js"
    assert upstream_path("/ray/api/v0/tasks?filter_predicates=%3D&limit=1") == (
        "/api/v0/tasks?filter_predicates=%3D&limit=1"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_pk", True),
        ("execution_pk", 0),
        ("job_id", "other"),
        ("task_id", "../other"),
        ("cookie", "sessionid=fixture; admin=true"),
        ("origin", "http://user:password@django-web:8000"),
    ],
)
def test_browser_refuses_unbound_identity_or_credentials(field, value):
    with pytest.raises(ValueError):
        validate_packet({**packet(), field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"task_id": "b" * 48},
        {"job_id": "02000000"},
        {"state": "RUNNING"},
        {"state": "FAILED"},
        {"attempt_number": 1},
        {"attempt_number": False},
    ],
)
def test_dashboard_response_cannot_substitute_identity_or_outcome(change):
    expected = packet()
    row = {
        "job_id": expected["job_id"],
        "task_id": expected["task_id"],
        "state": "FINISHED",
        "attempt_number": 0,
    }
    validate_task_response({"result": True, "data": {"result": {"result": [row]}}}, expected)
    with pytest.raises(ValueError):
        validate_task_response(
            {"result": True, "data": {"result": {"result": [{**row, **change}]}}}, expected
        )


@pytest.mark.parametrize("rows", [[], [{}, {}], {}])
def test_missing_or_ambiguous_task_result_cannot_pass(rows):
    with pytest.raises(ValueError):
        validate_task_response({"result": True, "data": {"result": {"result": rows}}}, packet())


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("content_type", ["application/json", "text/plain\r\nX-Injected: yes"])
def test_proxy_enforces_origin_credentials_read_only_and_cleanup(
    monkeypatch, oversized, content_type
):
    original_connection = http.client.HTTPConnection
    calls = []
    closed = []
    response = io.BytesIO(b"x" * (33 if oversized else 8))
    response.status = 200
    response.getheader = lambda _name, default=None: content_type

    class Upstream:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("ray-head", 8265, 5)

        def request(self, method, path, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return response

        def close(self):
            closed.append(True)

    monkeypatch.setattr(dashboard_proxy, "LISTEN_PORT", 0)
    monkeypatch.setattr(dashboard_proxy, "MAX_RESPONSE_BYTES", 32)
    monkeypatch.setattr(dashboard_proxy.http.client, "HTTPConnection", Upstream)
    with dashboard_proxy.dashboard_proxy() as base:
        port = urlsplit(base).port
        connection = original_connection("127.0.0.1", port, timeout=2)
        try:
            connection.request("POST", "/ray/api/jobs", body=b"must not submit")
            assert connection.getresponse().status == 501
            connection.close()
            connection.request("GET", "/ray/../api/v0/tasks")
            assert connection.getresponse().status == 400
            assert not calls
            connection.close()
            connection.request(
                "GET",
                "/ray/api/v0/tasks?limit=1",
                headers={"Cookie": "sessionid=private", "Authorization": "Bearer private"},
            )
            if oversized:
                with pytest.raises(http.client.RemoteDisconnected):
                    connection.getresponse()
            else:
                result = connection.getresponse()
                assert result.status == 200
                assert result.getheader("X-Injected") is None
                assert result.getheader("Content-Type") == content_type.replace("\r", "").replace(
                    "\n", ""
                )
                assert result.read() == b"x" * 8
            assert calls == [("GET", "/api/v0/tasks?limit=1", {"Accept-Encoding": "identity"})]
        finally:
            connection.close()
    assert closed
    with pytest.raises(OSError):
        original_connection("127.0.0.1", port, timeout=2).connect()


@pytest.mark.parametrize(
    "failure",
    [None, "exit", "exit_line", "exit_bool", "exit_large", "missing", "oversized", "timeout"],
)
def test_browser_wrapper_requires_complete_evidence_and_always_cleans_up(monkeypatch, failure):
    import os
    import subprocess

    from django_ray.models import RayTaskExecution

    cleanup = []

    @contextmanager
    def session():
        try:
            yield "sessionid=fixture"
        finally:
            cleanup.append("session")

    @contextmanager
    def proxy():
        try:
            yield dashboard_proxy.DASHBOARD_BASE
        finally:
            cleanup.append("proxy")

    class Process:
        pid = 12345
        returncode = 1 if failure and failure.startswith("exit") else 0

        def communicate(self, encoded, timeout):
            assert timeout == 60
            dashboard_browser.validate_packet(json.loads(encoded))
            if failure == "timeout":
                raise subprocess.TimeoutExpired("browser", timeout)
            if failure == "oversized":
                return b"x" * 1025, None
            if failure in {"exit_line", "exit_bool", "exit_large"}:
                line = {"exit_line": 123, "exit_bool": True, "exit_large": 10001}[failure]
                return json.dumps({"status": "failed", "line": line}).encode(), None
            value = {} if failure == "missing" else dashboard_browser.EXPECTED
            return json.dumps(value).encode(), None

        def wait(self, timeout):
            cleanup.append("process")

    monkeypatch.setattr(
        RayTaskExecution.objects,
        "get",
        lambda **_: SimpleNamespace(
            pk=1,
            ray_job_id="01000000:" + "a" * 48,
        ),
    )
    monkeypatch.setattr(dashboard_browser, "qualification_admin_session", session)
    monkeypatch.setattr(dashboard_browser, "dashboard_proxy", proxy)
    monkeypatch.setattr(dashboard_browser.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(os, "killpg", lambda *args: cleanup.append("kill"), raising=False)
    monkeypatch.setattr(dashboard_browser.signal, "SIGKILL", 9, raising=False)
    request = SimpleNamespace(hostname="django-web", port=8000, secure=False)
    with override_settings(RAY_DASHBOARD_URL=dashboard_proxy.DASHBOARD_BASE):
        if failure:
            with pytest.raises((ValueError, subprocess.TimeoutExpired)) as caught:
                dashboard_browser.observe_dashboard(request, "disposable-task")
            if failure.startswith("exit"):
                assert caught.value.line == (123 if failure == "exit_line" else None)
        else:
            assert dashboard_browser.observe_dashboard(request, "disposable-task") == {
                **dashboard_browser.EXPECTED,
                "session_and_proxy_removed": True,
            }
    assert cleanup == ["kill", "process", "proxy", "session"]


def test_browser_failure_output_contains_only_status_and_source_line(monkeypatch, capsys):
    def fail(_packet):
        raise ValueError("private-cookie-and-page-content")

    monkeypatch.setattr(dashboard_browser, "render", fail)
    monkeypatch.setattr(
        dashboard_browser.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(packet()).encode())),
    )
    assert dashboard_browser.main() == 1
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert set(output) == {"status", "line"}
    assert output["status"] == "failed"
    assert type(output["line"]) is int and 0 < output["line"] <= 10000
    assert "private-cookie" not in captured.out
    assert captured.err == ""
