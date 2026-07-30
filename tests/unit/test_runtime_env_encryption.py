"""Tests for strict authenticated RuntimeEnv storage envelopes."""

from __future__ import annotations

import base64
import hashlib
import json
import traceback
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.utils.functional import SimpleLazyObject, lazy

from django_ray.runtime import runtime_env_encryption as encryption_module
from django_ray.runtime.runtime_env_encryption import (
    DJANGO_SECRET_KEY_ID,
    RUNTIME_ENV_ENVELOPE_ALGORITHM,
    RUNTIME_ENV_ENVELOPE_FORMAT,
    RUNTIME_ENV_ENVELOPE_VERSION,
    RuntimeEnvEncryptionConfig,
    RuntimeEnvEncryptionError,
    is_runtime_env_encryption_envelope_candidate,
    protect_runtime_env_snapshot,
    unprotect_runtime_env_snapshot,
    validate_runtime_env_encryption_settings,
)

_KEY_BYTES = bytes(range(32))
_OTHER_KEY_BYTES = bytes(reversed(range(32)))
_KEY_ID = "key-2026-07"
_TASK_ID = "48a7c482-15bf-446b-966a-3902ed5716a8"
_PROFILE = "thin"
_PLAINTEXT = '{"env_vars":{"TOKEN":"customer-secret-marker-7cf3"}}'
_DIGEST = hashlib.sha256(_PLAINTEXT.encode()).hexdigest()
_ENVELOPE_FIELDS = {
    "algorithm",
    "ciphertext",
    "format",
    "key_id",
    "nonce",
    "version",
}
_AAD_DOMAIN = b"django-ray.runtime-env-storage.aad.v1\0"
_BASE64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _noncanonical_base64url(value: bytes) -> str:
    canonical = _base64url(value)
    final_index = _BASE64URL_ALPHABET.index(canonical[-1])
    return canonical[:-1] + _BASE64URL_ALPHABET[final_index + 1]


def _settings(
    *,
    mode: str = "encrypted",
    keys: dict | None = None,
    active_key: str | None = _KEY_ID,
    django_secret_fallback: bool = False,
    django_secret_key: object = "current-django-secret",
    django_secret_key_fallbacks: object = (),
):
    config = {
        "RUNTIME_ENV_STORAGE_MODE": mode,
        "RUNTIME_ENV_ENCRYPTION_KEYS": (
            {_KEY_ID: _base64url(_KEY_BYTES)} if keys is None else keys
        ),
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": active_key,
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": django_secret_fallback,
    }
    return validate_runtime_env_encryption_settings(
        config,
        django_secret_key=django_secret_key,
        django_secret_key_fallbacks=django_secret_key_fallbacks,
    )


def _protect(*, encryption=None, plaintext: str = _PLAINTEXT, digest: str = _DIGEST) -> str:
    return protect_runtime_env_snapshot(
        plaintext,
        task_id=_TASK_ID,
        profile=_PROFILE,
        digest=digest,
        encryption=encryption or _settings(),
    )


def _unprotect(
    envelope: str,
    *,
    encryption=None,
    task_id: str = _TASK_ID,
    profile: str | None = _PROFILE,
    digest: str = _DIGEST,
) -> str:
    return unprotect_runtime_env_snapshot(
        envelope,
        task_id=task_id,
        profile=profile,
        digest=digest,
        encryption=encryption or _settings(),
    )


def _canonical_envelope(envelope: dict) -> str:
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _aad(
    *,
    key_id: str = _KEY_ID,
    task_id: str = _TASK_ID,
    profile: str | None = _PROFILE,
    digest: str = _DIGEST,
) -> bytes:
    return (
        _AAD_DOMAIN
        + json.dumps(
            {
                "algorithm": RUNTIME_ENV_ENVELOPE_ALGORITHM,
                "format": RUNTIME_ENV_ENVELOPE_FORMAT,
                "key_id": key_id,
                "runtime_env_hash": digest,
                "runtime_env_profile": profile,
                "task_id": task_id,
                "version": RUNTIME_ENV_ENVELOPE_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def _forged_envelope(
    plaintext: bytes,
    *,
    digest: str,
    nonce: bytes = bytes(range(12)),
) -> str:
    ciphertext = AESGCM(_KEY_BYTES).encrypt(
        nonce,
        plaintext,
        _aad(digest=digest),
    )
    return _canonical_envelope(
        {
            "algorithm": RUNTIME_ENV_ENVELOPE_ALGORITHM,
            "ciphertext": _base64url(ciphertext),
            "format": RUNTIME_ENV_ENVELOPE_FORMAT,
            "key_id": _KEY_ID,
            "nonce": _base64url(nonce),
            "version": RUNTIME_ENV_ENVELOPE_VERSION,
        }
    )


def _assert_error_is_sanitized(error: RuntimeEnvEncryptionError, marker: str) -> None:
    formatted = "".join(
        traceback.format_exception(
            type(error),
            error,
            error.__traceback__,
        )
    )
    assert marker not in str(error)
    assert marker not in repr(error)
    assert marker not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


def test_plaintext_defaults_preserve_the_canonical_snapshot() -> None:
    encryption = validate_runtime_env_encryption_settings({})

    protected = protect_runtime_env_snapshot(
        _PLAINTEXT,
        task_id=_TASK_ID,
        profile=_PROFILE,
        digest=_DIGEST,
        encryption=encryption,
    )

    assert encryption.mode == "plaintext"
    assert encryption.active_key_id is None
    assert protected == _PLAINTEXT
    assert _base64url(_KEY_BYTES) not in repr(encryption)


def test_encrypted_snapshot_uses_the_exact_canonical_envelope(
    monkeypatch,
) -> None:
    nonce = bytes(range(12))
    monkeypatch.setattr(encryption_module.secrets, "token_bytes", lambda size: nonce)

    serialized = _protect()
    envelope = json.loads(serialized)

    assert serialized == _canonical_envelope(envelope)
    assert set(envelope) == _ENVELOPE_FIELDS
    assert envelope["format"] == RUNTIME_ENV_ENVELOPE_FORMAT
    assert envelope["version"] == RUNTIME_ENV_ENVELOPE_VERSION
    assert envelope["algorithm"] == RUNTIME_ENV_ENVELOPE_ALGORITHM
    assert envelope["key_id"] == _KEY_ID
    assert envelope["nonce"] == _base64url(nonce)
    assert "customer-secret-marker-7cf3" not in serialized
    expected_ciphertext = AESGCM(_KEY_BYTES).encrypt(
        nonce,
        _PLAINTEXT.encode(),
        _aad(),
    )
    assert envelope["ciphertext"] == _base64url(expected_ciphertext)
    assert _unprotect(serialized) == _PLAINTEXT


@pytest.mark.parametrize("generic_field", sorted(_ENVELOPE_FIELDS))
def test_one_generic_field_does_not_make_a_mapping_an_envelope_candidate(
    generic_field,
) -> None:
    assert not is_runtime_env_encryption_envelope_candidate({generic_field: "value"})


def test_namespaced_format_makes_a_mapping_an_envelope_candidate() -> None:
    assert is_runtime_env_encryption_envelope_candidate({"format": "django-ray.runtime-env.future"})


def test_exact_envelope_shape_is_a_candidate_even_with_a_corrupt_format() -> None:
    assert is_runtime_env_encryption_envelope_candidate(dict.fromkeys(_ENVELOPE_FIELDS, "value"))


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"env_vars": {"MODE": "plaintext"}},
        [],
        "not-a-mapping",
        None,
    ],
)
def test_plain_runtime_env_values_are_not_envelope_candidates(value) -> None:
    assert not is_runtime_env_encryption_envelope_candidate(value)


def test_encryption_uses_a_fresh_nonce_for_each_write() -> None:
    first = json.loads(_protect())
    second = json.loads(_protect())

    assert first["nonce"] != second["nonce"]
    assert first["ciphertext"] != second["ciphertext"]


@pytest.mark.parametrize(
    ("identity_override", "value"),
    [
        ("task_id", "a-different-task"),
        ("profile", "a-different-profile"),
        ("digest", "f" * 64),
    ],
)
def test_aad_binds_every_persisted_identity_field(
    identity_override,
    value,
) -> None:
    serialized = _protect()
    kwargs = {identity_override: value}

    with pytest.raises(RuntimeEnvEncryptionError, match="authentication failed"):
        _unprotect(serialized, **kwargs)


def test_envelope_cannot_be_copied_to_a_plaintext_mode_reader_with_another_task() -> None:
    serialized = _protect()
    plaintext_reader = _settings(mode="plaintext")

    assert _unprotect(serialized, encryption=plaintext_reader) == _PLAINTEXT
    with pytest.raises(RuntimeEnvEncryptionError, match="authentication failed"):
        _unprotect(
            serialized,
            encryption=plaintext_reader,
            task_id="copied-to-another-task",
        )


def test_aad_binds_key_id_even_when_two_ids_have_identical_key_material() -> None:
    shared_keys = {
        "first-key": _base64url(_KEY_BYTES),
        "second-key": _base64url(_KEY_BYTES),
    }
    writer = _settings(keys=shared_keys, active_key="first-key")
    reader = _settings(
        mode="plaintext",
        keys=shared_keys,
        active_key=None,
    )
    envelope = json.loads(_protect(encryption=writer))
    envelope["key_id"] = "second-key"

    with pytest.raises(RuntimeEnvEncryptionError, match="authentication failed"):
        _unprotect(_canonical_envelope(envelope), encryption=reader)


def test_none_profile_is_bound_and_round_trips() -> None:
    serialized = protect_runtime_env_snapshot(
        _PLAINTEXT,
        task_id=_TASK_ID,
        profile=None,
        digest=_DIGEST,
        encryption=_settings(),
    )

    assert (
        unprotect_runtime_env_snapshot(
            serialized,
            task_id=_TASK_ID,
            profile=None,
            digest=_DIGEST,
            encryption=_settings(),
        )
        == _PLAINTEXT
    )


@pytest.mark.parametrize(
    "mode",
    ["ENCRYPTED", "", " encrypted", True, None],
)
def test_storage_mode_is_strict(mode) -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="RUNTIME_ENV_STORAGE_MODE"):
        validate_runtime_env_encryption_settings({"RUNTIME_ENV_STORAGE_MODE": mode})


@pytest.mark.parametrize("raw_keys", [[], (), "key", 1, None])
def test_key_ring_must_be_a_dictionary(raw_keys) -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="must be a dictionary"):
        validate_runtime_env_encryption_settings({"RUNTIME_ENV_ENCRYPTION_KEYS": raw_keys})


@pytest.mark.parametrize(
    "key_id",
    [
        "",
        "-leading-hyphen",
        "contains space",
        "a" * 65,
        "é",
        1,
    ],
)
def test_dedicated_key_ids_use_the_bounded_ascii_contract(key_id) -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="key IDs"):
        validate_runtime_env_encryption_settings(
            {
                "RUNTIME_ENV_ENCRYPTION_KEYS": {
                    key_id: _base64url(_KEY_BYTES),
                }
            }
        )


def test_django_secret_key_id_is_reserved_from_the_dedicated_ring() -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="reserved"):
        validate_runtime_env_encryption_settings(
            {
                "RUNTIME_ENV_ENCRYPTION_KEYS": {
                    DJANGO_SECRET_KEY_ID: _base64url(_KEY_BYTES),
                }
            }
        )


@pytest.mark.parametrize(
    "material",
    [
        _base64url(_KEY_BYTES) + "=",
        _base64url(_KEY_BYTES[:-1]),
        _base64url(_KEY_BYTES + b"x"),
        _noncanonical_base64url(_KEY_BYTES),
        "+" + _base64url(_KEY_BYTES)[1:],
        "",
        1,
        None,
    ],
)
def test_dedicated_keys_require_exact_canonical_base64url(material) -> None:
    marker = "dedicated-key-material-marker-39f2"
    with pytest.raises(RuntimeEnvEncryptionError, match="exactly 32 bytes") as exc_info:
        validate_runtime_env_encryption_settings(
            {
                "RUNTIME_ENV_ENCRYPTION_KEYS": {
                    _KEY_ID: material,
                    "marker-key": marker,
                }
            }
        )

    _assert_error_is_sanitized(exc_info.value, marker)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {
                "RUNTIME_ENV_STORAGE_MODE": "encrypted",
                "RUNTIME_ENV_ENCRYPTION_KEYS": {_KEY_ID: _base64url(_KEY_BYTES)},
            },
            "is required",
        ),
        (
            {"RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "missing"},
            "does not resolve",
        ),
        (
            {"RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "not a valid ID"},
            "valid key ID",
        ),
        (
            {"RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": 1},
            "must be a boolean",
        ),
        (
            {"RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": DJANGO_SECRET_KEY_ID},
            "fallback is disabled",
        ),
    ],
)
def test_active_key_and_fallback_settings_fail_closed(config, message) -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match=message):
        validate_runtime_env_encryption_settings(config)


@pytest.mark.parametrize(
    ("current", "fallbacks"),
    [
        ("", ()),
        (1234, ()),
        (bytearray(b"not-an-accepted-secret"), ()),
        ("valid", ""),
        ("valid", b"bytes"),
        ("valid", [1]),
        ("valid", [bytearray(b"not-an-accepted-secret")]),
        ("valid", [""]),
    ],
)
def test_django_secret_sources_require_nonempty_strings_or_bytes(
    current,
    fallbacks,
) -> None:
    config = {
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": True,
    }

    with pytest.raises(RuntimeEnvEncryptionError, match="SECRET_KEY"):
        validate_runtime_env_encryption_settings(
            config,
            django_secret_key=current,
            django_secret_key_fallbacks=fallbacks,
        )


def test_django_secret_sources_accept_nonempty_bytes() -> None:
    encryption = _settings(
        mode="plaintext",
        keys={},
        active_key=None,
        django_secret_fallback=True,
        django_secret_key=b"current-secret",
        django_secret_key_fallbacks=(b"fallback-secret",),
    )

    assert len(encryption.django_secret_keys) == 2


@pytest.mark.parametrize(
    "secret",
    [
        SimpleLazyObject(lambda: "lazy-object-secret"),
        lazy(lambda: "promise-secret", str)(),
    ],
)
def test_django_secret_sources_accept_django_lazy_text(secret) -> None:
    encryption = _settings(
        mode="plaintext",
        keys={},
        active_key=None,
        django_secret_fallback=True,
        django_secret_key=secret,
    )

    assert len(encryption.django_secret_keys) == 1


def test_disabled_django_fallback_does_not_consume_secret_sources() -> None:
    encryption = validate_runtime_env_encryption_settings(
        {},
        django_secret_key=object(),
        django_secret_key_fallbacks=object(),
    )

    assert not encryption.django_secret_fallback
    assert encryption.django_secret_keys == ()


def test_django_secret_sources_default_to_django_settings(settings) -> None:
    settings.SECRET_KEY = "settings-current-secret"
    settings.SECRET_KEY_FALLBACKS = ["settings-fallback-secret"]

    encryption = validate_runtime_env_encryption_settings(
        {"RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": True}
    )

    assert len(encryption.django_secret_keys) == 2


@pytest.mark.parametrize("failure_site", ["force_bytes", "hkdf"])
def test_django_secret_derivation_errors_are_sanitized(
    monkeypatch,
    failure_site,
) -> None:
    marker = "django-derivation-internal-marker-722b"
    if failure_site == "force_bytes":
        monkeypatch.setattr(
            encryption_module,
            "force_bytes",
            lambda _value: (_ for _ in ()).throw(RuntimeError(marker)),
        )
        message = "must be non-empty"
    else:
        monkeypatch.setattr(
            encryption_module,
            "HKDF",
            lambda **_kwargs: SimpleNamespace(
                derive=lambda _value: (_ for _ in ()).throw(RuntimeError(marker))
            ),
        )
        message = "derivation"

    with pytest.raises(RuntimeEnvEncryptionError, match=message) as exc_info:
        validate_runtime_env_encryption_settings(
            {"RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": True},
            django_secret_key="valid-secret",
            django_secret_key_fallbacks=(),
        )

    _assert_error_is_sanitized(exc_info.value, marker)


def test_django_secret_rotation_uses_one_stable_key_id() -> None:
    old_writer = _settings(
        keys={},
        active_key=DJANGO_SECRET_KEY_ID,
        django_secret_fallback=True,
        django_secret_key="old-django-secret",
    )
    new_reader = _settings(
        mode="plaintext",
        keys={},
        active_key=None,
        django_secret_fallback=True,
        django_secret_key="new-django-secret",
        django_secret_key_fallbacks=("old-django-secret",),
    )

    serialized = _protect(encryption=old_writer)
    envelope = json.loads(serialized)

    assert envelope["key_id"] == DJANGO_SECRET_KEY_ID
    assert _unprotect(serialized, encryption=new_reader) == _PLAINTEXT


def test_django_secret_encryption_uses_current_secret_not_a_fallback() -> None:
    rotated_writer = _settings(
        keys={},
        active_key=DJANGO_SECRET_KEY_ID,
        django_secret_fallback=True,
        django_secret_key="new-django-secret",
        django_secret_key_fallbacks=("old-django-secret",),
    )
    old_only_reader = _settings(
        mode="plaintext",
        keys={},
        active_key=None,
        django_secret_fallback=True,
        django_secret_key="old-django-secret",
    )

    serialized = _protect(encryption=rotated_writer)

    with pytest.raises(RuntimeEnvEncryptionError, match="authentication failed"):
        _unprotect(serialized, encryption=old_only_reader)


def test_duplicate_django_secret_fallbacks_are_deduplicated() -> None:
    encryption = _settings(
        mode="plaintext",
        keys={},
        active_key=None,
        django_secret_fallback=True,
        django_secret_key="same-secret",
        django_secret_key_fallbacks=("same-secret", "same-secret"),
    )

    assert len(encryption.django_secret_keys) == 1


def test_unknown_envelope_key_fails_without_disclosing_the_id() -> None:
    marker = "unknown-customer-key-marker"
    envelope = json.loads(_protect())
    envelope["key_id"] = marker
    serialized = _canonical_envelope(envelope)

    with pytest.raises(RuntimeEnvEncryptionError, match="unavailable") as exc_info:
        _unprotect(serialized)

    _assert_error_is_sanitized(exc_info.value, marker)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda envelope: envelope.pop("nonce"),
        lambda envelope: envelope.update({"extra": True}),
        lambda envelope: envelope.update({"version": True}),
        lambda envelope: envelope.update({"key_id": "invalid key"}),
        lambda envelope: envelope.update({"nonce": "AA"}),
        lambda envelope: envelope.update({"nonce": envelope["nonce"] + "="}),
        lambda envelope: envelope.update({"ciphertext": "AA"}),
        lambda envelope: envelope.update({"ciphertext": "***"}),
    ],
)
def test_malformed_envelopes_fail_closed(mutation) -> None:
    envelope = json.loads(_protect())
    mutation(envelope)

    with pytest.raises(RuntimeEnvEncryptionError, match="envelope is malformed"):
        _unprotect(_canonical_envelope(envelope))


def test_noncanonical_envelope_json_is_rejected() -> None:
    envelope = json.loads(_protect())
    noncanonical = json.dumps(envelope, indent=2)

    with pytest.raises(RuntimeEnvEncryptionError, match="envelope is malformed"):
        _unprotect(noncanonical)


@pytest.mark.parametrize(
    "serialized",
    [
        '{"format":',
        "[]",
        '"not-an-object"',
        None,
    ],
)
def test_invalid_serialized_envelopes_are_sanitized(serialized) -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="envelope is malformed"):
        _unprotect(serialized)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "django-ray.runtime-env.encrypted-future"),
        ("version", 2),
        ("algorithm", "AES-128-GCM"),
    ],
)
def test_unsupported_envelope_metadata_is_not_downgraded(
    field,
    value,
) -> None:
    envelope = json.loads(_protect())
    envelope[field] = value

    with pytest.raises(RuntimeEnvEncryptionError, match="metadata is unsupported"):
        _unprotect(_canonical_envelope(envelope))


def test_ciphertext_tampering_has_a_fixed_authentication_error() -> None:
    envelope = json.loads(_protect())
    ciphertext = bytearray(base64.urlsafe_b64decode(envelope["ciphertext"] + "=="))
    ciphertext[0] ^= 1
    envelope["ciphertext"] = _base64url(bytes(ciphertext))

    with pytest.raises(RuntimeEnvEncryptionError, match="authentication failed"):
        _unprotect(_canonical_envelope(envelope))


def test_unexpected_decryptor_errors_are_sanitized(
    monkeypatch,
) -> None:
    marker = "cryptography-internal-marker-84a1"
    serialized = _protect()

    monkeypatch.setattr(
        encryption_module,
        "AESGCM",
        lambda _key: SimpleNamespace(
            decrypt=lambda *_args: (_ for _ in ()).throw(RuntimeError(marker))
        ),
    )

    with pytest.raises(RuntimeEnvEncryptionError, match="authentication failed") as exc_info:
        _unprotect(serialized)

    _assert_error_is_sanitized(exc_info.value, marker)


def test_nonce_generation_errors_are_sanitized(monkeypatch) -> None:
    marker = "random-source-marker-51b9"
    monkeypatch.setattr(
        encryption_module.secrets,
        "token_bytes",
        lambda _size: (_ for _ in ()).throw(RuntimeError(marker)),
    )

    with pytest.raises(RuntimeEnvEncryptionError, match="nonce generation failed") as exc_info:
        _protect()

    _assert_error_is_sanitized(exc_info.value, marker)


def test_encryption_provider_errors_are_sanitized(monkeypatch) -> None:
    marker = "encryption-provider-marker-4d1e"
    monkeypatch.setattr(
        encryption_module,
        "AESGCM",
        lambda _key: SimpleNamespace(
            encrypt=lambda *_args: (_ for _ in ()).throw(RuntimeError(marker))
        ),
    )

    with pytest.raises(RuntimeEnvEncryptionError, match="encryption failed") as exc_info:
        _protect()

    _assert_error_is_sanitized(exc_info.value, marker)


def test_invalid_constructed_policy_cannot_select_an_unavailable_key() -> None:
    invalid = RuntimeEnvEncryptionConfig(
        mode="encrypted",
        keys={},
        active_key_id="missing",
        django_secret_fallback=False,
        django_secret_keys=(),
    )

    with pytest.raises(RuntimeEnvEncryptionError, match="key is unavailable"):
        _protect(encryption=invalid)


@pytest.mark.parametrize(
    ("task_id", "profile"),
    [
        ("", _PROFILE),
        ("x" * 256, _PROFILE),
        ("\ud800", _PROFILE),
        ("tâsk", _PROFILE),
        (_TASK_ID, ""),
        (_TASK_ID, "contains space"),
        (_TASK_ID, "\ud800"),
    ],
)
def test_encryption_identity_is_strict_and_sanitized(task_id, profile) -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="identity") as exc_info:
        protect_runtime_env_snapshot(
            _PLAINTEXT,
            task_id=task_id,
            profile=profile,
            digest=_DIGEST,
            encryption=_settings(),
        )

    _assert_error_is_sanitized(exc_info.value, "customer-secret-marker-7cf3")


def test_protection_rejects_a_digest_that_is_not_the_plaintext_hash() -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="plaintext"):
        _protect(digest="f" * 64)


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "0" * 63,
        "A" * 64,
        1,
        None,
    ],
)
def test_digest_identity_requires_lowercase_sha256(digest) -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="identity"):
        _protect(digest=digest)


def test_unprotect_rejects_invalid_identity_before_crypto() -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="identity is malformed"):
        unprotect_runtime_env_snapshot(
            _protect(),
            task_id="not valid because it has spaces",
            profile=_PROFILE,
            digest=_DIGEST,
            encryption=_settings(),
        )


def test_protection_rejects_non_string_plaintext() -> None:
    with pytest.raises(RuntimeEnvEncryptionError, match="plaintext is invalid"):
        _protect(plaintext=1)


@pytest.mark.parametrize(
    "plaintext",
    [
        "not-json",
        '{ "env_vars": {} }',
        '{"format":"django-ray.runtime-env.encrypted"}',
        '["not-a-mapping"]',
        "\ud800",
    ],
)
def test_protection_requires_canonical_plaintext_runtime_env(plaintext) -> None:
    try:
        digest = hashlib.sha256(plaintext.encode()).hexdigest()
    except UnicodeEncodeError:
        digest = "0" * 64

    with pytest.raises(RuntimeEnvEncryptionError, match="plaintext is invalid"):
        _protect(plaintext=plaintext, digest=digest)


@pytest.mark.parametrize(
    ("plaintext", "digest", "message"),
    [
        (b"\xff", hashlib.sha256(b"\xff").hexdigest(), "payload is invalid"),
        (b"not-json", hashlib.sha256(b"not-json").hexdigest(), "payload is invalid"),
        (_PLAINTEXT.encode(), "f" * 64, "hash does not match"),
    ],
)
def test_authenticated_but_invalid_plaintext_fails_closed(
    plaintext,
    digest,
    message,
) -> None:
    serialized = _forged_envelope(plaintext, digest=digest)

    with pytest.raises(RuntimeEnvEncryptionError, match=message):
        _unprotect(serialized, digest=digest)
