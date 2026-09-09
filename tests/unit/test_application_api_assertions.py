"""The shared API layer runs without the host gate or application dependencies."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from qualification.application.api import ApiEvidence, verify_application_api


def test_api_assertions_import_with_only_the_standard_library() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from qualification.application.api import ApiEvidence, verify_application_api; "
            "assert ApiEvidence().task_id == ''; "
            "assert not any(name.split('.')[0] in "
            "{'django', 'django_ray', 'ray', 'kubernetes', 'scripts'} for name in sys.modules)",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("invalid_body", [b'{"private":', b"\xff-private", b"[" * 2000])
def test_api_parse_failure_retains_no_private_parser_exception(invalid_body: bytes) -> None:
    observations = ApiEvidence()

    def request(path: str, **kwargs) -> tuple[int, bytes]:
        return (200, invalid_body) if path == "/api/openapi.json" else (401, b"{}")

    def forbidden_token() -> str:
        raise AssertionError("Credentials must not be retrieved before schema validation")

    with pytest.raises(ValueError, match="OpenAPI schema did not return valid JSON") as caught:
        verify_application_api(
            request, get_token=forbidden_token, task_timeout=1, evidence=observations
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert observations.task_id == ""
    assert observations.api_task_status_bounded is False


def test_failed_authentication_assertion_does_not_retrieve_credentials() -> None:
    calls = []

    def request(path: str, **kwargs) -> tuple[int, bytes]:
        calls.append((path, kwargs))
        return 200, b"{}"

    def forbidden_token() -> str:
        raise AssertionError("The credential supplier must not run after failed protection")

    with pytest.raises(ValueError, match="unauthenticated.*expected 401"):
        verify_application_api(request, get_token=forbidden_token, task_timeout=1)
    assert calls == [("/api/enqueue/add/2/3", {"method": "POST"})]
