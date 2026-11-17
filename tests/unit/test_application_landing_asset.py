"""Fixed landing-script identity controls without HTTP, Django or Ray services."""

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualification.application import landing_asset as asset
from qualification.application import run_api

BODY = b"const landing = 'source';\n"


def record(body=BODY):
    return {
        "schema_version": 1,
        "status": "passed",
        "asset": "testproject/landing.js",
        "path": "/static/testproject/landing.js",
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "landing.js"
    path.write_bytes(BODY)
    monkeypatch.setattr(asset, "SOURCE_PATH", path)
    return path


def transport(monkeypatch, body=BODY, *, status=200, headers=None):
    calls = []
    reads = []
    closed = []
    reply = io.BytesIO(body)
    response_headers = {"Content-Type": asset.CONTENT_TYPE, **(headers or {})}

    class Response:
        def getheader(self, name):
            return response_headers.get(name)

        def read(self, count):
            reads.append(count)
            return reply.read(count)

    response = Response()
    response.status = status

    class Connection:
        def __init__(self, hostname, port, *, timeout):
            assert (hostname, port, timeout) == ("127.0.0.1", 8000, 5)

        def request(self, method, path, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return response

        def close(self):
            closed.append(True)

    monkeypatch.setattr(run_api.http.client, "HTTPConnection", Connection)
    return calls, reads, closed


def test_fixed_loopback_get_compares_raw_bytes_and_reports_actual_identity(source, monkeypatch):
    calls, reads, closed = transport(monkeypatch)
    assert asset.observe() == record()
    assert calls == [
        (
            "GET",
            "/static/testproject/landing.js",
            {"Host": "django-web", "Accept-Encoding": "identity"},
        )
    ]
    assert reads == [len(BODY) + 1]
    assert closed == [True]


@pytest.mark.parametrize("body", [BODY[:-1], BODY + b"x", b"x" * len(BODY)])
def test_short_extra_or_different_served_bytes_refuse(source, monkeypatch, body):
    calls, _reads, closed = transport(monkeypatch, body)
    with pytest.raises(ValueError):
        asset.observe()
    assert len(calls) == 1
    assert closed == [True]


@pytest.mark.parametrize("status", [302, 404])
def test_non200_does_not_redirect_or_retry(source, monkeypatch, status):
    calls, _reads, closed = transport(monkeypatch, status=status)
    with pytest.raises(ValueError):
        asset.observe()
    assert len(calls) == 1
    assert closed == [True]


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Encoding": "gzip"},
        {"Content-Encoding": "identity"},
        {"Content-Type": 'text/javascript; charset="utf-16"'},
    ],
)
def test_matching_raw_body_with_wrong_encoding_metadata_refuses_before_read(
    source, monkeypatch, headers
):
    calls, reads, closed = transport(monkeypatch, headers=headers)
    with pytest.raises(ValueError):
        asset.observe()
    assert len(calls) == 1
    assert reads == []
    assert closed == [True]


@pytest.mark.parametrize("size", [0, 64 * 1024 + 1])
def test_source_bounds_refuse_before_http(source, monkeypatch, size):
    source.write_bytes(b"x" * size)
    monkeypatch.setattr(
        asset, "ApplicationHttp", lambda *_a, **_k: pytest.fail("HTTP before source admission")
    )
    with pytest.raises(ValueError, match="Landing asset"):
        asset.observe()
    with pytest.raises(ValueError, match="Landing asset"):
        asset.source_record(source)


def test_exact_source_cap_and_raw_line_endings_are_preserved(source, monkeypatch):
    body = b"x" * (64 * 1024 - 2) + b"\r\n"
    source.write_bytes(body)
    _calls, reads, _closed = transport(monkeypatch, body)
    assert asset.observe() == record(body)
    assert asset.source_record(source) == record(body)
    assert reads == [64 * 1024 + 1]


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"bytes": True},
        {"bytes": float(len(BODY))},
        {"sha256": "A" * 64},
        {"path": "/static/other.js"},
        {"extra": "private-canary"},
    ],
)
def test_compact_identity_refuses_coercible_or_unexpected_fields(change):
    raw = json.dumps({**record(), **change}).encode()
    with pytest.raises(ValueError, match="Landing asset"):
        asset.parse_record(raw, expected=record())


@pytest.mark.parametrize(
    "raw",
    [
        b'{"status":"passed","status":"passed"}',
        b'{"bytes":NaN}',
        b"private-canary",
        b"x" * 4097,
        b"\xff",
    ],
)
def test_duplicate_nonfinite_invalid_or_oversized_json_is_fixed_refusal(raw):
    with pytest.raises(ValueError, match="^Landing asset observation is incomplete$"):
        asset.parse_record(raw, expected=record())


def test_parent_expected_source_identity_is_independent_and_not_normalized(source):
    assert asset.parse_record(json.dumps(record()).encode(), expected=record()) == record()
    source.write_bytes(BODY.replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match="Landing asset"):
        asset.parse_record(json.dumps(record()).encode(), expected=asset.source_record(source))


@pytest.mark.parametrize("field,value", [("pod", "other"), ("uid", "other"), ("container", True)])
def test_bound_identity_requires_the_same_typed_web_snapshot(field, value):
    web = {"pod": "django-web-fixture", "uid": "fixed-uid", "container": "web"}
    bound = {**record(), **web}
    asset.validate_bound_record(bound, web)
    with pytest.raises(ValueError, match="Landing asset"):
        asset.validate_bound_record({**bound, field: value}, web)


@pytest.mark.parametrize("failure", [False, True])
def test_cli_outputs_only_compact_identity_or_fixed_failure(monkeypatch, failure):
    output = io.BytesIO()
    monkeypatch.setattr(asset.sys, "stdout", SimpleNamespace(buffer=output))

    def observe():
        if failure:
            raise ValueError("private-canary response bytes")
        return record()

    monkeypatch.setattr(asset, "observe", observe)
    assert asset.main() == (1 if failure else 0)
    raw = output.getvalue()
    assert len(raw) <= 4096
    assert b"private-canary" not in raw
    if failure:
        assert raw == b'{"status":"failed"}\n'
    else:
        assert asset.parse_record(raw, expected=record()) == record()


def test_fresh_import_needs_no_application_dependencies_or_http():
    root = Path(__file__).resolve().parents[2]
    code = """
import http.client
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'django_ray', 'ray'}:
            raise AssertionError('application dependency import')
sys.meta_path.insert(0, Deny())
def refuse(*args, **kwargs):
    raise AssertionError('HTTP at import')
http.client.HTTPConnection = refuse
from qualification.application import landing_asset
assert not any(name.split('.')[0] in {'django', 'django_ray', 'ray'} for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(root)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
