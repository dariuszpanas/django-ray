"""Run the shared API layer from a namespace Job using only the standard library."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import re
import time
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from qualification.application.api import ApiEvidence, verify_application_api

MAX_RESPONSE_BYTES = 256 * 1024
MAX_EXPLICIT_RESPONSE_BYTES = 1024 * 1024
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+={0,2}\Z")


class HttpFailureCode(StrEnum):
    """Fixed public categories; never include request or dependency text."""

    REQUEST = "request_failed"
    RESPONSE_HEADERS = "response_headers_failed"
    BODY_READ = "body_read_failed"
    REQUIRED_HEADERS = "required_headers_mismatch"
    CACHE = "cache_directives_mismatch"
    MEDIA_TYPE = "media_type_mismatch"
    BODY_LIMIT = "body_limit_exceeded"


class ApplicationHttpError(ValueError):
    """An HTTP assertion failure with bounded, secret-free receipt fields."""

    def __init__(self, message: str, *, code: HttpFailureCode, status: int | None) -> None:
        super().__init__(message)
        if not isinstance(code, HttpFailureCode):
            raise ValueError("Invalid HTTP failure category")
        self.code = code
        self.status = status if type(status) is int and 100 <= status <= 599 else None

    def diagnostic(self) -> dict[str, str | int | None]:
        return {"code": self.code.value, "status": self.status}


class ApplicationHttp:
    """One explicit origin, no proxies or redirects, bounded reads and socket waits.

    The enclosing Job must also set its hard execution deadline. Socket timeouts
    bound individual blocking operations, not a whole slow-streaming response.
    """

    def __init__(self, base_url: str, *, request_timeout: float = 10) -> None:
        parsed = urlsplit(base_url)
        if (
            not base_url.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in base_url)
            or "\\" in base_url
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in base_url
            or "#" in base_url
        ):
            raise ValueError("The application URL must be one HTTP(S) origin")
        if not math.isfinite(request_timeout) or not 0 < request_timeout <= 10:
            raise ValueError("The request timeout must be positive and at most 10 seconds")
        self.hostname = parsed.hostname
        self.port = parsed.port
        self.secure = parsed.scheme == "https"
        self.request_timeout = request_timeout
        self.requests = 0
        self.last_http_status: int | None = None

    def __call__(
        self,
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_RESPONSE_BYTES,
        required_response_headers: dict[str, str] | None = None,
        required_cache_directives: frozenset[str] = frozenset(),
        required_media_types: frozenset[str] = frozenset(),
    ) -> tuple[int, bytes]:
        parsed = urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or not path.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in path)
            or "\\" in path
            or "#" in path
            or parsed.scheme
            or parsed.netloc
        ):
            raise ValueError("The application request must use an origin-relative path")
        if method not in {"GET", "POST", "DELETE"}:
            raise ValueError("Unsupported application assertion method")
        if type(response_limit) is not int or not 0 < response_limit <= MAX_EXPLICIT_RESPONSE_BYTES:
            raise ValueError("The application response limit is invalid")
        connection_type = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        connection = connection_type(self.hostname, self.port, timeout=self.request_timeout)
        self.requests += 1
        self.last_http_status = None
        result: tuple[int, bytes] | None = None
        failure = "Application HTTP request failed"
        failure_code = HttpFailureCode.REQUEST
        try:
            connection.request(method, path, headers=headers or {})
            failure_code = HttpFailureCode.RESPONSE_HEADERS
            response = connection.getresponse()
            self.last_http_status = response.status
            if any(
                response.getheader(name) != value
                for name, value in (required_response_headers or {}).items()
            ):
                failure = "Application HTTP response headers did not match"
                failure_code = HttpFailureCode.REQUIRED_HEADERS
            elif not required_cache_directives.issubset(
                part.strip().lower()
                for part in (response.getheader("Cache-Control") or "").split(",")
            ):
                failure = "Application HTTP response cache directives did not match"
                failure_code = HttpFailureCode.CACHE
            elif required_media_types and (
                (response.getheader("Content-Type") or "").partition(";")[0].strip().lower()
                not in required_media_types
            ):
                failure = "Application HTTP response media type did not match"
                failure_code = HttpFailureCode.MEDIA_TYPE
            else:
                failure_code = HttpFailureCode.BODY_READ
                body = response.read(response_limit + 1)
                if len(body) > response_limit:
                    failure = "Application HTTP response exceeded its byte limit"
                    failure_code = HttpFailureCode.BODY_LIMIT
                else:
                    result = response.status, body
        except (OSError, http.client.HTTPException, ValueError):
            # Dependency errors may retain request headers or private responses.
            pass
        finally:
            connection.close()
        if result is None:
            raise ApplicationHttpError(failure, code=failure_code, status=self.last_http_status)
        return result


def read_token(path: Path) -> str:
    """Read the application credential from a mounted file, never from argv."""
    token: str | None = None
    try:
        with path.open("rb") as stream:
            raw = stream.read(513)
        if 32 <= len(raw) <= 512:
            token = raw.decode("ascii")
    except (OSError, UnicodeDecodeError):
        pass
    if token is None or TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("The application token file is missing or invalid")
    return token


def main(argv: list[str] | None = None) -> int:
    """Emit one bounded layer receipt and return nonzero on any failed assertion."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--task-timeout", type=float, default=180)
    args = parser.parse_args(argv)
    observations = ApiEvidence()
    started = time.monotonic()
    transport = None
    passed = False
    try:
        if not math.isfinite(args.task_timeout) or not 0 < args.task_timeout <= 600:
            raise ValueError("Task timeout must be positive and at most 600 seconds")
        transport = ApplicationHttp(args.base_url)
        verify_application_api(
            transport,
            get_token=lambda: read_token(args.token_file),
            task_timeout=args.task_timeout,
            evidence=observations,
        )
        passed = True
    except Exception:
        # Assertion exceptions can contain server-controlled result values.
        # Keep only the typed observations and fixed failure classification.
        pass
    print(
        json.dumps(
            {
                "schema_version": 1,
                "layer": "application_api",
                "status": "passed" if passed else "failed",
                "complete_application_gate": False,
                "failure": None if passed else "application_api_assertion_failed",
                "requests": transport.requests if transport is not None else 0,
                "last_http_status": transport.last_http_status if transport is not None else None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "observations": asdict(observations),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
