"""Private, bounded continuation tokens for registered payload cleanup.

The registry primary key is a storage location, so signing or base64 alone
would disclose it in command output. Tokens encrypt that key and authenticate
the fixed retention cutoff and explicit deletion mode; they grant no authority.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

CURSOR_MAX_CHARS = 3072
RETENTION_MAX_DAYS = 365000
_CONTEXT = b"django-ray.registered-payload-purge.cursor.v1"


class InvalidPurgeCursorError(ValueError):
    """The continuation token is invalid or belongs to another policy."""


@dataclass(frozen=True)
class PurgeCursor:
    """One immutable scan boundary, independent of the continued row's existence."""

    after: str
    cutoff: datetime
    retention_days: int
    delete: bool


def _cipher() -> AESGCM:
    key = hmac.new(settings.SECRET_KEY.encode("utf-8"), _CONTEXT, hashlib.sha256).digest()
    return AESGCM(key)


def _validate(value: object) -> PurgeCursor:
    if type(value) is not dict or set(value) != {"v", "after", "cutoff", "days", "delete"}:
        raise InvalidPurgeCursorError
    if type(value["v"]) is not int or value["v"] != 1:
        raise InvalidPurgeCursorError
    after = value["after"]
    if type(after) is not str or not 1 <= len(after) <= 500:
        raise InvalidPurgeCursorError
    if len(after.encode("utf-8")) > 2000:
        raise InvalidPurgeCursorError
    days = value["days"]
    if type(days) is not int or not 0 <= days <= RETENTION_MAX_DAYS:
        raise InvalidPurgeCursorError
    if type(value["delete"]) is not bool:
        raise InvalidPurgeCursorError
    if type(value["cutoff"]) is not str or len(value["cutoff"]) > 40:
        raise InvalidPurgeCursorError
    cutoff = datetime.fromisoformat(value["cutoff"])
    if cutoff.isoformat() != value["cutoff"]:
        raise InvalidPurgeCursorError
    return PurgeCursor(after, cutoff, days, value["delete"])


def encode_purge_cursor(cursor: PurgeCursor) -> str:
    """Encrypt a bounded PK; changing SECRET_KEY invalidates outstanding tokens."""
    value = {
        "v": 1,
        "after": cursor.after,
        "cutoff": cursor.cutoff.isoformat(),
        "days": cursor.retention_days,
        "delete": cursor.delete,
    }
    try:
        _validate(value)
        plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(12)
        encrypted = nonce + _cipher().encrypt(nonce, plaintext, _CONTEXT)
        token = base64.urlsafe_b64encode(encrypted).decode("ascii").rstrip("=")
        if len(token) > CURSOR_MAX_CHARS:
            raise InvalidPurgeCursorError
        return token
    except (ValueError, UnicodeError, TypeError, OverflowError):
        raise InvalidPurgeCursorError from None


def decode_purge_cursor(token: object) -> PurgeCursor:
    """Reject unbounded, noncanonical, malformed or unauthenticated tokens."""
    try:
        if type(token) is not str or not 1 <= len(token) <= CURSOR_MAX_CHARS:
            raise InvalidPurgeCursorError
        if not token.isascii():
            raise InvalidPurgeCursorError
        encoded = token.encode("ascii")
        encrypted = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(encrypted).rstrip(b"=") != encoded or len(encrypted) < 29:
            raise InvalidPurgeCursorError
        plaintext = _cipher().decrypt(encrypted[:12], encrypted[12:], _CONTEXT)
        value = json.loads(plaintext)
        cursor = _validate(value)
        # One canonical encoding excludes duplicate keys and alternative JSON forms.
        if (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            != plaintext
        ):
            raise InvalidPurgeCursorError
        return cursor
    except (
        ValueError,
        UnicodeError,
        TypeError,
        OverflowError,
        binascii.Error,
        InvalidTag,
        RecursionError,
    ):
        raise InvalidPurgeCursorError from None
