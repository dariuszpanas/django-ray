"""Authenticated, bounded continuation for RuntimeEnv maintenance.

The token carries only a scan boundary and a keyed policy fingerprint. Encryption
also hides database namespace equality and numeric row identifiers from logs.
Tokens grant no mutation permission and are invalid after SECRET_KEY rotation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

MAX_CURSOR_CHARS = 1024
MAX_PK = (1 << 63) - 1
_CONTEXT = b"django-ray.runtime-env-maintenance.cursor.v1"


class InvalidRuntimeEnvCursorError(ValueError):
    """A fixed refusal that never includes the supplied token."""


@dataclass(frozen=True)
class RuntimeEnvCursor:
    """An indexed high-water range, not a database snapshot or audit receipt."""

    after: int
    high_water: int
    policy: str
    mode: str


def keyed_fingerprint(value: bytes) -> str:
    """Bind an internal policy without returning its key or namespace material."""
    secret = settings.SECRET_KEY
    if type(secret) is not str or not 1 <= len(secret) <= 4096:
        raise InvalidRuntimeEnvCursorError
    return hmac.new(secret.encode(), _CONTEXT + b"\0" + value, hashlib.sha256).hexdigest()


def _cipher() -> AESGCM:
    return AESGCM(bytes.fromhex(keyed_fingerprint(b"encryption")))


def _validate(value: object) -> RuntimeEnvCursor:
    if type(value) is not dict or set(value) != {"v", "after", "high_water", "policy", "mode"}:
        raise InvalidRuntimeEnvCursorError
    if type(value["v"]) is not int or value["v"] != 1:
        raise InvalidRuntimeEnvCursorError
    after, high_water = value["after"], value["high_water"]
    if type(after) is not int or type(high_water) is not int:
        raise InvalidRuntimeEnvCursorError
    if not 0 <= after <= high_water <= MAX_PK:
        raise InvalidRuntimeEnvCursorError
    policy = value["policy"]
    if (
        type(policy) is not str
        or len(policy) != 64
        or any(c not in "0123456789abcdef" for c in policy)
    ):
        raise InvalidRuntimeEnvCursorError
    mode = value["mode"]
    if type(mode) is not str or mode not in {"dry-run", "check", "rewrite"}:
        raise InvalidRuntimeEnvCursorError
    return RuntimeEnvCursor(after, high_water, policy, mode)


def encode_runtime_env_cursor(cursor: RuntimeEnvCursor) -> str:
    """Return one canonical encrypted continuation, bounded before encryption."""
    value = {
        "v": 1,
        "after": cursor.after,
        "high_water": cursor.high_water,
        "policy": cursor.policy,
        "mode": cursor.mode,
    }
    try:
        _validate(value)
        plaintext = json.dumps(value, separators=(",", ":")).encode()
        nonce = secrets.token_bytes(12)
        encrypted = nonce + _cipher().encrypt(nonce, plaintext, _CONTEXT)
        token = base64.urlsafe_b64encode(encrypted).rstrip(b"=").decode()
        if len(token) > MAX_CURSOR_CHARS:
            raise InvalidRuntimeEnvCursorError
        return token
    except (ValueError, UnicodeError, TypeError, OverflowError):
        raise InvalidRuntimeEnvCursorError from None


def decode_runtime_env_cursor(token: object) -> RuntimeEnvCursor:
    """Refuse tampering, oversized tokens, duplicate fields and alternate forms."""
    try:
        if type(token) is not str or not 1 <= len(token) <= MAX_CURSOR_CHARS or not token.isascii():
            raise InvalidRuntimeEnvCursorError
        encoded = token.encode()
        encrypted = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        if len(encrypted) < 29 or base64.urlsafe_b64encode(encrypted).rstrip(b"=") != encoded:
            raise InvalidRuntimeEnvCursorError
        plaintext = _cipher().decrypt(encrypted[:12], encrypted[12:], _CONTEXT)
        value = json.loads(plaintext)
        cursor = _validate(value)
        if json.dumps(value, separators=(",", ":")).encode() != plaintext:
            raise InvalidRuntimeEnvCursorError
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
        raise InvalidRuntimeEnvCursorError from None
