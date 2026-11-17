"""One fixed landing-script HTTP identity observation in the named web container."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from qualification.application.run_api import ApplicationHttp

SOURCE_RELATIVE = Path("testproject/static/testproject/landing.js")
SOURCE_PATH = Path("/app") / SOURCE_RELATIVE
ASSET = "testproject/landing.js"
ASSET_PATH = "/static/testproject/landing.js"
CONTENT_TYPE = 'text/javascript; charset="utf-8"'
MAX_SOURCE_BYTES = 64 * 1024
MAX_RECORD_BYTES = 4096
RECORD_FIELDS = {"schema_version", "status", "asset", "path", "bytes", "sha256"}
BINDING_FIELDS = {"pod", "uid", "container"}
FAILURE = "Landing asset observation is incomplete"


def source_record(path: Path) -> dict:
    """Read raw source bytes; neither side of the comparison is normalized."""
    with path.open("rb") as stream:
        body = stream.read(MAX_SOURCE_BYTES + 1)
    if not 0 < len(body) <= MAX_SOURCE_BYTES:
        raise ValueError(FAILURE)
    return {
        "schema_version": 1,
        "status": "passed",
        "asset": ASSET,
        "path": ASSET_PATH,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def validate_record(value: object, *, expected: dict | None = None) -> dict:
    """Refuse incomplete, coercible or unexpected fixed-asset identities."""
    if (
        not isinstance(value, dict)
        or set(value) != RECORD_FIELDS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["status"] != "passed"
        or value["asset"] != ASSET
        or value["path"] != ASSET_PATH
        or type(value["bytes"]) is not int
        or not 0 < value["bytes"] <= MAX_SOURCE_BYTES
        or not isinstance(value["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
        or (expected is not None and value != expected)
    ):
        raise ValueError(FAILURE)
    return value


def validate_bound_record(value: object, web_runtime: dict) -> None:
    """The live summary must bind this identity to the validated web snapshot."""
    if not isinstance(value, dict) or set(value) != RECORD_FIELDS | BINDING_FIELDS:
        raise ValueError(FAILURE)
    validate_record({key: value[key] for key in RECORD_FIELDS})
    if any(
        not isinstance(value[key], str) or value[key] != web_runtime[key] for key in BINDING_FIELDS
    ):
        raise ValueError(FAILURE)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(FAILURE)
        result[key] = value
    return result


def _constant(_value):
    raise ValueError(FAILURE)


def parse_record(raw: bytes, *, expected: dict) -> dict:
    """Check the compact child output before strict JSON decoding."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RECORD_BYTES:
        raise ValueError(FAILURE)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError):
        raise ValueError(FAILURE) from None
    return validate_record(value, expected=expected)


def observe() -> dict:
    """Compare one unauthenticated loopback response with this image's raw file."""
    with SOURCE_PATH.open("rb") as stream:
        source = stream.read(MAX_SOURCE_BYTES + 1)
    if not 0 < len(source) <= MAX_SOURCE_BYTES:
        raise ValueError(FAILURE)
    request = ApplicationHttp("http://127.0.0.1:8000", request_timeout=5)
    status, body = request(
        ASSET_PATH,
        method="GET",
        headers={"Host": "django-web", "Accept-Encoding": "identity"},
        response_limit=len(source),
        required_response_headers={"Content-Encoding": None, "Content-Type": CONTENT_TYPE},
        required_media_types=frozenset({"text/javascript"}),
    )
    if type(status) is not int or status != 200 or body != source:
        raise ValueError(FAILURE)
    return {
        "schema_version": 1,
        "status": "passed",
        "asset": ASSET,
        "path": ASSET_PATH,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def main() -> int:
    try:
        value = validate_record(observe())
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError(FAILURE)
    except Exception:
        sys.stdout.buffer.write(b'{"status":"failed"}\n')
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
