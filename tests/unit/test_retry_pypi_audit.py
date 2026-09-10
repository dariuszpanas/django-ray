"""Exercise retry outcomes through the pinned CLI and real PyPI query method."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from packaging.version import Version
from pip_audit import _cli
from pip_audit._service import ResolvedDependency, pypi

from scripts import retry_pypi_audit as retry


@pytest.fixture
def scanner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    requirements = tmp_path / "requirements.txt"
    requirements.touch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "retry-pypi-audit",
            "--strict",
            "--require-hashes",
            "--disable-pip",
            "--vulnerability-service",
            "pypi",
            "--progress-spinner",
            "off",
            "--timeout",
            "30",
            "--requirement",
            str(requirements),
        ],
    )
    state = SimpleNamespace(outcomes=[], queries=[], collections=0, sleeps=[])

    def collect() -> Any:
        state.collections += 1
        return iter([ResolvedDependency(name="django", version=Version("6.0"))])

    monkeypatch.setattr(
        _cli, "RequirementSource", lambda *a, **kw: SimpleNamespace(collect=collect)
    )

    def get(*, url: str, timeout: int) -> requests.Response:
        state.queries.append((url, timeout))
        outcome = state.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"vulnerabilities": outcome}).encode()
        return response

    monkeypatch.setattr(pypi, "caching_session", lambda cache: SimpleNamespace(get=get))
    monkeypatch.setattr(retry.time, "sleep", state.sleeps.append)
    return state


def test_read_timeout_restarts_complete_scanner_once(
    scanner: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner.outcomes = [requests.ReadTimeout("advisory feed timed out"), []]

    retry.main()

    assert scanner.collections == 2
    assert scanner.queries == [("https://pypi.org/pypi/django/6.0/json", 30)] * 2
    assert scanner.sleeps == [2]
    stderr = capsys.readouterr().err
    assert "ReadTimeout: advisory feed timed out" in stderr
    assert "retrying the complete audit once" in stderr
    assert "No known vulnerabilities found" in stderr


def test_retry_exhaustion_propagates_final_timeout(scanner: SimpleNamespace) -> None:
    final = requests.ReadTimeout("still unavailable")
    scanner.outcomes = [requests.ReadTimeout("first timeout"), final]

    with pytest.raises(requests.ReadTimeout) as caught:
        retry.main()

    assert caught.value is final
    assert scanner.collections == 2
    assert scanner.sleeps == [2]


@pytest.mark.parametrize("after_timeout", [False, True])
def test_real_cli_vulnerability_exit_is_never_retried(
    scanner: SimpleNamespace, after_timeout: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    finding = [{"id": "PYSEC-TEST", "aliases": [], "fixed_in": ["6.0.1"]}]
    scanner.outcomes = ([requests.ReadTimeout("first timeout")] if after_timeout else []) + [
        finding
    ]

    with pytest.raises(SystemExit) as caught:
        retry.main()

    assert caught.value.code == 1
    assert scanner.collections == (2 if after_timeout else 1)
    assert scanner.sleeps == ([2] if after_timeout else [])
    output = capsys.readouterr()
    assert "PYSEC-TEST" in output.out
    assert "Found 1 known vulnerability" in output.err
    assert "No known vulnerabilities found" not in output.err


@pytest.mark.parametrize("error", [requests.ConnectionError("offline"), ValueError("bad feed")])
def test_other_service_failures_are_not_retried(scanner: SimpleNamespace, error: Exception) -> None:
    scanner.outcomes = [error]
    with pytest.raises(type(error)) as caught:
        retry.main()
    assert caught.value is error
    assert scanner.collections == 1
    assert scanner.sleeps == []


def test_read_timeout_outside_advisory_query_is_not_retried(
    scanner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        raise requests.ReadTimeout("dependency source")

    monkeypatch.setattr(_cli, "audit", fail)
    with pytest.raises(requests.ReadTimeout, match="dependency source"):
        retry.main()
    assert scanner.collections == 0
    assert scanner.sleeps == []


def test_scanner_entry_point_rechecks_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry.importlib.metadata, "version", lambda name: "0.0.0")
    monkeypatch.setattr(_cli, "audit", lambda: pytest.fail("must reject before scanning"))
    with pytest.raises(RuntimeError, match=r"expected pip-audit==2\.10\.1"):
        retry.main()
