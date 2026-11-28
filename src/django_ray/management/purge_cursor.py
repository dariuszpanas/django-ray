"""Private, bounded continuation tokens for retention and payload cleanup.

The registry primary key is a storage location, so signing or base64 alone
would disclose it in command output. Tokens encrypt scan boundaries and
authenticate the fixed retention cutoff and explicit deletion mode; they grant
no authority.
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
_MAX_EXECUTION_PK = (1 << 63) - 1
_CONTEXT = b"django-ray.registered-payload-purge.cursor.v1"
_INPUT_CURSOR_PREFIX = "ipc1."
_EXECUTION_CONTEXT = b"django-ray.execution-purge.cursor.v1"
_EXECUTION_CURSOR_PREFIX = "epc1."
_MAX_DATABASE_ALIAS_LEN = 128


class InvalidPurgeCursorError(ValueError):
    """The continuation token is invalid or belongs to another policy."""


@dataclass(frozen=True)
class PurgeCursor:
    """One immutable scan boundary, independent of the continued row's existence."""

    after: str
    cutoff: datetime
    retention_days: int
    delete: bool
    database: str = "default"


@dataclass(frozen=True)
class ExecutionPurgeCursor:
    """One immutable execution/lease retention scan boundary."""

    after_execution_pk: int
    after_lease_id: str
    cutoff: datetime
    older_than_days: int
    delete: bool
    skip_leases: bool
    skip_results: bool
    database: str = "default"
    after_finished_at: datetime | None = None


def _secret_key_bytes() -> bytes:
    secret_key = settings.SECRET_KEY
    if isinstance(secret_key, bytes):
        if not secret_key:
            raise InvalidPurgeCursorError
        return secret_key
    if isinstance(secret_key, str):
        if not secret_key:
            raise InvalidPurgeCursorError
        return secret_key.encode("utf-8")
    raise InvalidPurgeCursorError


def _cipher_for_context(context: bytes) -> AESGCM:
    key = hmac.new(_secret_key_bytes(), context, hashlib.sha256).digest()
    return AESGCM(key)


def _cipher() -> AESGCM:
    return _cipher_for_context(_CONTEXT)


def _execution_cipher() -> AESGCM:
    return _cipher_for_context(_EXECUTION_CONTEXT)


def _validate_database_alias(database: object) -> str:
    if (
        type(database) is not str
        or not 1 <= len(database) <= _MAX_DATABASE_ALIAS_LEN
        or not database.isascii()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in database)
    ):
        raise InvalidPurgeCursorError
    return database


def _validate(value: object) -> PurgeCursor:
    legacy_keys = {"v", "after", "cutoff", "days", "delete"}
    current_keys = legacy_keys | {"db"}
    if type(value) is not dict or set(value) not in (legacy_keys, current_keys):
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
    database = _validate_database_alias(value["db"]) if "db" in value else "default"
    if type(value["cutoff"]) is not str or len(value["cutoff"]) > 40:
        raise InvalidPurgeCursorError
    cutoff = datetime.fromisoformat(value["cutoff"])
    if cutoff.isoformat() != value["cutoff"]:
        raise InvalidPurgeCursorError
    return PurgeCursor(after, cutoff, days, value["delete"], database=database)


def _validate_execution(value: object) -> ExecutionPurgeCursor:
    legacy_keys = {
        "v",
        "after_pk",
        "after_lease",
        "cutoff",
        "days",
        "delete",
        "skip_leases",
        "skip_results",
        "db",
    }
    current_keys = legacy_keys | {"after_finished_at"}
    if type(value) is not dict or set(value) not in (legacy_keys, current_keys):
        raise InvalidPurgeCursorError
    if type(value["v"]) is not int or value["v"] != 1:
        raise InvalidPurgeCursorError
    after_pk = value["after_pk"]
    if type(after_pk) is not int or not 0 <= after_pk <= _MAX_EXECUTION_PK:
        raise InvalidPurgeCursorError
    after_lease = value["after_lease"]
    if type(after_lease) is not str or len(after_lease) > 255:
        raise InvalidPurgeCursorError
    if len(after_lease.encode("utf-8")) > 1020:
        raise InvalidPurgeCursorError
    days = value["days"]
    if type(days) is not int or not 0 <= days <= RETENTION_MAX_DAYS:
        raise InvalidPurgeCursorError
    if (
        type(value["delete"]) is not bool
        or type(value["skip_leases"]) is not bool
        or type(value["skip_results"]) is not bool
    ):
        raise InvalidPurgeCursorError
    database = _validate_database_alias(value["db"])
    if type(value["cutoff"]) is not str or len(value["cutoff"]) > 40:
        raise InvalidPurgeCursorError
    cutoff = datetime.fromisoformat(value["cutoff"])
    if cutoff.isoformat() != value["cutoff"]:
        raise InvalidPurgeCursorError
    after_finished_at: datetime | None = None
    if "after_finished_at" in value and value["after_finished_at"] is not None:
        raw_after_finished_at = value["after_finished_at"]
        if type(raw_after_finished_at) is not str or len(raw_after_finished_at) > 40:
            raise InvalidPurgeCursorError
        after_finished_at = datetime.fromisoformat(raw_after_finished_at)
        if after_finished_at.isoformat() != raw_after_finished_at:
            raise InvalidPurgeCursorError
        if after_pk <= 0:
            raise InvalidPurgeCursorError
    return ExecutionPurgeCursor(
        after_execution_pk=after_pk,
        after_lease_id=after_lease,
        cutoff=cutoff,
        older_than_days=days,
        delete=value["delete"],
        skip_leases=value["skip_leases"],
        skip_results=value["skip_results"],
        database=database,
        after_finished_at=after_finished_at,
    )


def encode_purge_cursor(cursor: PurgeCursor) -> str:
    """Encrypt a bounded PK; SECRET_KEY rotation invalidates."""
    value = {
        "v": 1,
        "after": cursor.after,
        "cutoff": cursor.cutoff.isoformat(),
        "days": cursor.retention_days,
        "delete": cursor.delete,
        "db": cursor.database,
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
    """Reject unbounded, noncanonical, malformed or unauthenticated tokens.

    Accepts both ``ipc1.``-prefixed tokens and legacy unprefixed tokens for
    backward compatibility across upgrades.
    """
    try:
        if type(token) is not str or not 1 <= len(token) <= CURSOR_MAX_CHARS:
            raise InvalidPurgeCursorError
        if not token.isascii():
            raise InvalidPurgeCursorError
        body = (
            token.removeprefix(_INPUT_CURSOR_PREFIX)
            if token.startswith(_INPUT_CURSOR_PREFIX)
            else token
        )
        if not body:
            raise InvalidPurgeCursorError
        encoded = body.encode("ascii")
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


def encode_execution_purge_cursor(cursor: ExecutionPurgeCursor) -> str:
    """Encrypt one bounded execution/lease retention cursor with a safe non-hyphen prefix."""
    value: dict[str, object] = {
        "v": 1,
        "after_pk": cursor.after_execution_pk,
        "after_lease": cursor.after_lease_id,
        "cutoff": cursor.cutoff.isoformat(),
        "days": cursor.older_than_days,
        "delete": cursor.delete,
        "skip_leases": cursor.skip_leases,
        "skip_results": cursor.skip_results,
        "db": cursor.database,
    }
    if cursor.after_finished_at is not None:
        value["after_finished_at"] = cursor.after_finished_at.isoformat()
    try:
        _validate_execution(value)
        plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(12)
        encrypted = nonce + _execution_cipher().encrypt(nonce, plaintext, _EXECUTION_CONTEXT)
        encoded = base64.urlsafe_b64encode(encrypted).decode("ascii").rstrip("=")
        token = f"{_EXECUTION_CURSOR_PREFIX}{encoded}"
        if len(token) > CURSOR_MAX_CHARS:
            raise InvalidPurgeCursorError
        return token
    except (ValueError, UnicodeError, TypeError, OverflowError):
        raise InvalidPurgeCursorError from None


def decode_execution_purge_cursor(token: object) -> ExecutionPurgeCursor:
    """Reject malformed, tampered, or cross-command execution purge cursors."""
    try:
        if type(token) is not str or not 1 <= len(token) <= CURSOR_MAX_CHARS:
            raise InvalidPurgeCursorError
        if not token.isascii() or not token.startswith(_EXECUTION_CURSOR_PREFIX):
            raise InvalidPurgeCursorError
        body = token.removeprefix(_EXECUTION_CURSOR_PREFIX)
        if not body:
            raise InvalidPurgeCursorError
        encoded = body.encode("ascii")
        encrypted = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(encrypted).rstrip(b"=") != encoded or len(encrypted) < 29:
            raise InvalidPurgeCursorError
        plaintext = _execution_cipher().decrypt(encrypted[:12], encrypted[12:], _EXECUTION_CONTEXT)
        value = json.loads(plaintext)
        cursor = _validate_execution(value)
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
