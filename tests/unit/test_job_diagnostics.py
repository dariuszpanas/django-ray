"""Transport and redaction boundaries for legacy Ray Job supporting logs."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
import requests

from django_ray.redaction import REDACTED
from django_ray.runner.job_diagnostics import (
    MAX_JOB_DIAGNOSTIC_BYTES,
    OVERSIZED_JOB_DIAGNOSTIC,
    read_job_diagnostic,
    sanitize_job_diagnostic,
)


class Response:
    def __init__(self, body: bytes, *, status=200, encoding="identity"):
        self.body = io.BytesIO(body)
        self.status_code = status
        self.headers = {"Content-Encoding": encoding}
        self.raw = self
        self.reads = []
        self.closed = False

    def read(self, amount, *, decode_content):
        self.reads.append((amount, decode_content))
        return self.body.read(amount)

    @property
    def text(self):
        raise AssertionError("Unbounded text access is forbidden")

    def json(self):
        raise AssertionError("Unbounded JSON access is forbidden")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True


@pytest.fixture
def client():
    return SimpleNamespace(
        _address="https://selected.example/base",
        _cookies={"session": "test-session"},
        _headers={"Authorization": "Bearer test-only", "Accept-Encoding": "gzip"},
        _verify="/pinned/test-ca.pem",
    )


def install_response(monkeypatch, response):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(requests, "get", get)
    return calls


def test_reader_preserves_selected_transport_and_closes_response(monkeypatch, client):
    response = Response(json.dumps({"logs": "line one\nline two"}).encode())
    calls = install_response(monkeypatch, response)

    assert read_job_diagnostic(client, "legacy/id?query", timeout=5.0) == "line one\nline two"
    assert calls == [
        (
            "https://selected.example/base/api/jobs/legacy%2Fid%3Fquery/logs",
            {
                "cookies": {"session": "test-session"},
                "headers": {
                    "Authorization": "Bearer test-only",
                    "Accept-Encoding": "identity",
                },
                "verify": "/pinned/test-ca.pem",
                "timeout": 5.0,
                "stream": True,
                "allow_redirects": False,
            },
        )
    ]
    assert client._headers["Accept-Encoding"] == "gzip"
    assert response.reads == [(MAX_JOB_DIAGNOSTIC_BYTES + 1, False)]
    assert response.closed


@pytest.mark.parametrize("overrun", [1, 1_000_000])
def test_wire_overrun_discards_the_entire_diagnostic(monkeypatch, client, overrun):
    response = Response(b"x" * (MAX_JOB_DIAGNOSTIC_BYTES + overrun))
    install_response(monkeypatch, response)

    assert read_job_diagnostic(client, "legacy", timeout=5.0) == OVERSIZED_JOB_DIAGNOSTIC
    assert response.body.tell() == MAX_JOB_DIAGNOSTIC_BYTES + 1
    assert response.closed


def test_exact_wire_ceiling_is_accepted(monkeypatch, client):
    overhead = len(b'{"logs":""}')
    text = "x" * (MAX_JOB_DIAGNOSTIC_BYTES - overhead)
    response = Response(b'{"logs":"' + text.encode() + b'"}')
    install_response(monkeypatch, response)

    assert read_job_diagnostic(client, "legacy", timeout=5.0) == text
    assert response.closed


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 500])
def test_error_and_redirect_bodies_are_never_read(monkeypatch, client, status):
    response = Response(b"password=must-not-be-read", status=status)
    install_response(monkeypatch, response)

    assert read_job_diagnostic(client, "legacy", timeout=5.0) is None
    assert response.reads == []
    assert response.closed


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip"])
def test_encoded_responses_are_rejected_without_decompression(monkeypatch, client, encoding):
    response = Response(b"not-read", encoding=encoding)
    install_response(monkeypatch, response)

    assert read_job_diagnostic(client, "legacy", timeout=5.0) is None
    assert response.reads == []
    assert response.closed


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\xff",
        b"not JSON",
        b'{"logs":"first","logs":"second"}',
        b'{"logs":123}',
        b'{"logs":null}',
        b'{"logs":["nested"]}',
        b'{"logs":"text","unexpected":true}',
        b'{"logs":"\xed\xa0\x80"}',
        b'{"logs":"\\ud800"}',
        b'["text"]',
    ],
)
def test_malformed_responses_return_no_diagnostic(monkeypatch, client, body):
    response = Response(body)
    install_response(monkeypatch, response)

    assert read_job_diagnostic(client, "legacy", timeout=5.0) is None
    assert response.closed


@pytest.mark.parametrize("where", ["request", "read"])
def test_timeout_never_materializes_or_logs_exception_text(monkeypatch, client, caplog, where):
    class UnsafeTimeout(requests.Timeout):
        def __str__(self):
            raise AssertionError("Do not materialize a transport exception")

    response = Response(b"")

    def timeout(*_args, **_kwargs):
        raise UnsafeTimeout("password=not-an-operator-message")

    if where == "request":
        monkeypatch.setattr(requests, "get", timeout)
    else:
        install_response(monkeypatch, response)
        response.read = timeout

    assert read_job_diagnostic(client, "legacy", timeout=5.0) is None
    assert not caplog.records
    assert response.closed is (where == "read")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ordinary traceback\nsecond line", "ordinary traceback\nsecond line"),
        ("\x1b[31mordinary\x1b[0m\r\nsecond", "ordinary\nsecond"),
        ("password=example-value", REDACTED),
        ("Authorization: Bearer example-value", REDACTED),
        ("é" * (MAX_JOB_DIAGNOSTIC_BYTES // 2), "é" * (MAX_JOB_DIAGNOSTIC_BYTES // 2)),
        ("é" * (MAX_JOB_DIAGNOSTIC_BYTES // 2 + 1), OVERSIZED_JOB_DIAGNOSTIC),
        ("x" * (MAX_JOB_DIAGNOSTIC_BYTES + 1), OVERSIZED_JOB_DIAGNOSTIC),
        ("\ud800", None),
        (None, None),
        ({"logs": "not text"}, None),
    ],
    ids=[
        "ordinary",
        "terminal",
        "password",
        "authorization",
        "utf8-limit",
        "utf8-overrun",
        "ascii-overrun",
        "surrogate",
        "absent",
        "object",
    ],
)
def test_sanitization_has_a_utf8_byte_ceiling(text, expected):
    assert sanitize_job_diagnostic(text) == expected


def test_arbitrary_objects_are_not_rendered():
    class Unsafe:
        def __str__(self):
            raise AssertionError("No object rendering")

    assert sanitize_job_diagnostic(Unsafe()) is None


def test_reader_redacts_before_returning(monkeypatch, client):
    response = Response(b'{"logs":"password=example-value"}')
    install_response(monkeypatch, response)

    assert read_job_diagnostic(client, "legacy", timeout=5.0) == REDACTED
    assert response.closed
