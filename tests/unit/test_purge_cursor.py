"""Opaque cleanup cursors preserve policy without exposing storage locations."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from django_ray.management import purge_cursor
from django_ray.management.purge_cursor import (
    CURSOR_MAX_CHARS,
    InvalidPurgeCursorError,
    PurgeCursor,
    decode_purge_cursor,
    encode_purge_cursor,
)


def _cursor() -> PurgeCursor:
    return PurgeCursor(
        "s3://private-bucket/private-key", datetime(2026, 10, 1, tzinfo=UTC), 30, False
    )


def _authenticated(plaintext: bytes) -> str:
    nonce = b"1" * 12
    data = nonce + purge_cursor._cipher().encrypt(nonce, plaintext, purge_cursor._CONTEXT)
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def test_cursor_hides_location_randomizes_ciphertext_and_preserves_policy():
    cursor = _cursor()
    first, second = encode_purge_cursor(cursor), encode_purge_cursor(cursor)
    assert first != second
    assert decode_purge_cursor(first) == cursor == decode_purge_cursor(second)
    assert "private" not in first
    ciphertext = base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))
    assert b"private-bucket" not in ciphertext
    assert len(first) <= CURSOR_MAX_CHARS


def test_cursor_key_rotation_requires_a_new_pass():
    token = encode_purge_cursor(_cursor())
    with override_settings(SECRET_KEY="replacement-secret", SECRET_KEY_FALLBACKS=[]):
        with pytest.raises(InvalidPurgeCursorError):
            decode_purge_cursor(token)


def test_cursor_tampering_and_wrong_aad_fail_without_reflecting_input():
    token = encode_purge_cursor(_cursor())
    changed = ("A" if token[0] != "A" else "B") + token[1:]
    for invalid in (changed, token + "=", token[:-1]):
        with pytest.raises(CommandError, match="Invalid purge cursor") as error:
            call_command("django_ray_purge_inputs", cursor=invalid)
        assert token not in str(error.value)
    nonce = b"2" * 12
    encrypted = nonce + purge_cursor._cipher().encrypt(nonce, b"{}", b"other-context")
    wrong_context = base64.urlsafe_b64encode(encrypted).decode().rstrip("=")
    with pytest.raises(InvalidPurgeCursorError):
        decode_purge_cursor(wrong_context)


@pytest.mark.parametrize("token", [None, 123, "", "!", "x" * (CURSOR_MAX_CHARS + 1), "\u2603", "A"])
def test_invalid_cursor_is_rejected_before_cipher_or_database(monkeypatch, token):
    monkeypatch.setattr(
        purge_cursor, "_cipher", lambda: pytest.fail("invalid token reached cipher")
    )
    with pytest.raises(InvalidPurgeCursorError):
        decode_purge_cursor(token)


@pytest.mark.parametrize(
    "change",
    [
        {"v": True},
        {"v": 2},
        {"after": ""},
        {"after": "x" * 501},
        {"after": 3},
        {"days": True},
        {"days": -1},
        {"days": 365001},
        {"delete": 1},
        {"cutoff": 1},
        {"cutoff": "nonsense"},
        {"cutoff": "2026-10-01"},
        {"cutoff": "x" * 41},
        {"unexpected": "value"},
    ],
)
def test_authenticated_cursor_still_requires_strict_schema(change):
    value = {
        "v": 1,
        "after": "ref",
        "cutoff": "2026-10-01T00:00:00+00:00",
        "days": 30,
        "delete": False,
    }
    value.update(change)
    token = _authenticated(json.dumps(value, separators=(",", ":")).encode())
    with pytest.raises(InvalidPurgeCursorError):
        decode_purge_cursor(token)


@pytest.mark.parametrize("plaintext", [b"[]", b"null", b"{", b"{} ", b'{"v":1,"v":1}', b"[" * 1100])
def test_authenticated_cursor_rejects_malformed_json(plaintext):
    with pytest.raises(InvalidPurgeCursorError):
        decode_purge_cursor(_authenticated(plaintext))


def test_encoder_rejects_invalid_persisted_primary_key():
    with pytest.raises(InvalidPurgeCursorError):
        encode_purge_cursor(PurgeCursor("x" * 501, _cursor().cutoff, 30, False))
    with pytest.raises(InvalidPurgeCursorError):
        encode_purge_cursor(PurgeCursor("\ud800", _cursor().cutoff, 30, False))
