"""Authenticated storage envelopes for durable RuntimeEnv snapshots."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.encoding import force_bytes
from django.utils.functional import LazyObject, Promise

RUNTIME_ENV_ENVELOPE_FORMAT = "django-ray.runtime-env.encrypted"
RUNTIME_ENV_ENVELOPE_VERSION = 1
RUNTIME_ENV_ENVELOPE_ALGORITHM = "AES-256-GCM"
DJANGO_SECRET_KEY_ID = "django-secret"

_STORAGE_MODES = frozenset({"plaintext", "encrypted"})
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_TASK_ID = re.compile(r"^[\x21-\x7e]{1,255}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_FIELDS = frozenset(
    {
        "format",
        "version",
        "algorithm",
        "key_id",
        "nonce",
        "ciphertext",
    }
)
_AAD_DOMAIN = b"django-ray.runtime-env-storage.aad.v1\0"
_DJANGO_HKDF_SALT = b"django-ray.runtime-env-storage.django-secret.hkdf-sha256.v1\0"
_DJANGO_HKDF_INFO = b"django-ray.runtime-env-storage.aes-256-gcm.key.v1\0"
_USE_DJANGO_SETTINGS = object()


class RuntimeEnvEncryptionError(ImproperlyConfigured):
    """A fixed diagnostic from RuntimeEnv encryption configuration or storage."""


@dataclass(frozen=True, repr=False)
class RuntimeEnvEncryptionConfig:
    """Validated RuntimeEnv storage policy with secret-bearing fields hidden."""

    mode: str
    keys: Mapping[str, bytes]
    active_key_id: str | None
    django_secret_fallback: bool
    django_secret_keys: tuple[bytes, ...]


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_canonical_base64url(
    value: object,
    *,
    exact_bytes: int | None = None,
    minimum_bytes: int | None = None,
) -> bytes | None:
    if not isinstance(value, str) or not value or _BASE64URL.fullmatch(value) is None:
        return None
    if exact_bytes is not None:
        expected_length = (exact_bytes * 8 + 5) // 6
        if len(value) != expected_length:
            return None
    padding = "=" * (-len(value) % 4)
    decoded: bytes | None
    try:
        decoded = base64.b64decode(
            f"{value}{padding}".encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error, UnicodeEncodeError):
        decoded = None
    if decoded is None:
        return None
    if exact_bytes is not None and len(decoded) != exact_bytes:
        return None
    if minimum_bytes is not None and len(decoded) < minimum_bytes:
        return None
    if _canonical_base64url(decoded) != value:
        return None
    return decoded


def _derive_django_secret_key(value: object) -> bytes:
    if not isinstance(value, str | bytes | Promise | LazyObject):
        raise RuntimeEnvEncryptionError(
            "django-ray: Django SECRET_KEY values used for RuntimeEnv encryption "
            "must be non-empty text or bytes"
        )
    material: bytes | None
    try:
        material = force_bytes(value)
    except Exception:
        material = None
    if not material:
        raise RuntimeEnvEncryptionError(
            "django-ray: Django SECRET_KEY values used for RuntimeEnv encryption "
            "must be non-empty text or bytes"
        )

    derived: bytes | None
    try:
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_DJANGO_HKDF_SALT,
            info=_DJANGO_HKDF_INFO,
        ).derive(material)
    except Exception:
        derived = None
    if derived is None:
        raise RuntimeEnvEncryptionError(
            "django-ray: Django SECRET_KEY derivation for RuntimeEnv encryption failed"
        )
    return derived


def _django_secret_candidates(
    current: object,
    fallbacks: object,
) -> tuple[bytes, ...]:
    if not isinstance(fallbacks, Sequence) or isinstance(
        fallbacks,
        str | bytes | bytearray,
    ):
        raise RuntimeEnvEncryptionError(
            "django-ray: SECRET_KEY_FALLBACKS must be a sequence when the "
            "RuntimeEnv Django-secret fallback is enabled"
        )

    candidates: list[bytes] = []
    for secret_value in (current, *fallbacks):
        derived = _derive_django_secret_key(secret_value)
        if derived not in candidates:
            candidates.append(derived)
    return tuple(candidates)


def validate_runtime_env_encryption_settings(
    config: Mapping[str, Any],
    *,
    django_secret_key: object = _USE_DJANGO_SETTINGS,
    django_secret_key_fallbacks: object = _USE_DJANGO_SETTINGS,
) -> RuntimeEnvEncryptionConfig:
    """Validate RuntimeEnv storage encryption settings without exposing values."""
    mode = config.get("RUNTIME_ENV_STORAGE_MODE", "plaintext")
    if not isinstance(mode, str) or mode not in _STORAGE_MODES:
        raise RuntimeEnvEncryptionError(
            "django-ray: RUNTIME_ENV_STORAGE_MODE must be 'plaintext' or 'encrypted'"
        )

    raw_keys = config.get("RUNTIME_ENV_ENCRYPTION_KEYS", {})
    if not isinstance(raw_keys, dict):
        raise RuntimeEnvEncryptionError(
            "django-ray: RUNTIME_ENV_ENCRYPTION_KEYS must be a dictionary"
        )

    decoded_keys: dict[str, bytes] = {}
    for key_id, encoded_key in raw_keys.items():
        if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
            raise RuntimeEnvEncryptionError(
                "django-ray: RuntimeEnv encryption key IDs must be 1-64 characters "
                "and use only letters, numbers, '.', '_', or '-'"
            )
        if key_id == DJANGO_SECRET_KEY_ID:
            raise RuntimeEnvEncryptionError(
                "django-ray: RUNTIME_ENV_ENCRYPTION_KEYS cannot use the reserved "
                "'django-secret' key ID"
            )
        decoded_key = _decode_canonical_base64url(encoded_key, exact_bytes=32)
        if decoded_key is None:
            raise RuntimeEnvEncryptionError(
                "django-ray: RUNTIME_ENV_ENCRYPTION_KEYS values must be canonical "
                "unpadded base64url encodings of exactly 32 bytes"
            )
        decoded_keys[key_id] = decoded_key

    django_secret_fallback = config.get(
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK",
        False,
    )
    if type(django_secret_fallback) is not bool:
        raise RuntimeEnvEncryptionError(
            "django-ray: RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK must be a boolean"
        )

    django_secret_keys: tuple[bytes, ...] = ()
    if django_secret_fallback:
        if django_secret_key is _USE_DJANGO_SETTINGS:
            django_secret_key = settings.SECRET_KEY
        if django_secret_key_fallbacks is _USE_DJANGO_SETTINGS:
            django_secret_key_fallbacks = getattr(settings, "SECRET_KEY_FALLBACKS", ())
        django_secret_keys = _django_secret_candidates(
            django_secret_key,
            django_secret_key_fallbacks,
        )

    active_key_id = config.get("RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY")
    if active_key_id is not None and (
        not isinstance(active_key_id, str) or _KEY_ID.fullmatch(active_key_id) is None
    ):
        raise RuntimeEnvEncryptionError(
            "django-ray: RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY must be None or a valid key ID"
        )
    if active_key_id == DJANGO_SECRET_KEY_ID:
        if not django_secret_fallback:
            raise RuntimeEnvEncryptionError(
                "django-ray: RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY selects 'django-secret' "
                "but the Django-secret fallback is disabled"
            )
    elif active_key_id is not None and active_key_id not in decoded_keys:
        raise RuntimeEnvEncryptionError(
            "django-ray: RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY does not resolve to a "
            "configured RuntimeEnv encryption key"
        )
    if mode == "encrypted" and active_key_id is None:
        raise RuntimeEnvEncryptionError(
            "django-ray: RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY is required when "
            "RUNTIME_ENV_STORAGE_MODE is 'encrypted'"
        )

    return RuntimeEnvEncryptionConfig(
        mode=mode,
        keys=MappingProxyType(decoded_keys),
        active_key_id=active_key_id,
        django_secret_fallback=django_secret_fallback,
        django_secret_keys=django_secret_keys,
    )


def is_runtime_env_encryption_envelope_candidate(value: object) -> bool:
    """Recognize the namespaced discriminator or one complete envelope shape."""
    if not isinstance(value, dict):
        return False
    format_value = value.get("format")
    if isinstance(format_value, str) and format_value.startswith("django-ray.runtime-env."):
        return True
    return set(value) == _ENVELOPE_FIELDS


def _validate_identity(
    *,
    task_id: object,
    profile: object,
    digest: object,
) -> bool:
    return (
        isinstance(task_id, str)
        and _TASK_ID.fullmatch(task_id) is not None
        and (
            profile is None
            or (isinstance(profile, str) and _PROFILE_NAME.fullmatch(profile) is not None)
        )
        and isinstance(digest, str)
        and _SHA256_DIGEST.fullmatch(digest) is not None
    )


def _plaintext_validation(
    plaintext: object,
    *,
    digest: str,
) -> tuple[str | None, bytes | None]:
    if not isinstance(plaintext, str):
        return "invalid", None
    plaintext_bytes: bytes | None
    try:
        plaintext_bytes = plaintext.encode("utf-8")
    except UnicodeEncodeError:
        plaintext_bytes = None
    if plaintext_bytes is None:
        return "invalid", None

    parsed: object | None
    try:
        parsed = json.loads(plaintext)
    except (TypeError, ValueError, RecursionError):
        parsed = None
    if (
        not isinstance(parsed, dict)
        or is_runtime_env_encryption_envelope_candidate(parsed)
        or _canonical_json(parsed) != plaintext
    ):
        return "invalid", None
    observed_digest = hashlib.sha256(plaintext_bytes).hexdigest()
    if not hmac.compare_digest(observed_digest, digest):
        return "hash", None
    return None, plaintext_bytes


def _associated_data(
    *,
    key_id: str,
    task_id: str,
    profile: str | None,
    digest: str,
) -> bytes:
    payload = {
        "algorithm": RUNTIME_ENV_ENVELOPE_ALGORITHM,
        "format": RUNTIME_ENV_ENVELOPE_FORMAT,
        "key_id": key_id,
        "runtime_env_hash": digest,
        "runtime_env_profile": profile,
        "task_id": task_id,
        "version": RUNTIME_ENV_ENVELOPE_VERSION,
    }
    return _AAD_DOMAIN + _canonical_json(payload).encode("utf-8")


def protect_runtime_env_snapshot(
    plaintext: str,
    *,
    task_id: str,
    profile: str | None,
    digest: str,
    encryption: RuntimeEnvEncryptionConfig,
) -> str:
    """Return plaintext or a fresh authenticated envelope for one new snapshot."""
    if not isinstance(encryption, RuntimeEnvEncryptionConfig) or not _validate_identity(
        task_id=task_id,
        profile=profile,
        digest=digest,
    ):
        raise RuntimeEnvEncryptionError("django-ray: RuntimeEnv encryption identity is invalid")
    plaintext_error, plaintext_bytes = _plaintext_validation(
        plaintext,
        digest=digest,
    )
    if plaintext_error == "hash":
        raise RuntimeEnvEncryptionError(
            "django-ray: RuntimeEnv encryption plaintext hash does not match"
        )
    if plaintext_error is not None or plaintext_bytes is None:
        raise RuntimeEnvEncryptionError("django-ray: RuntimeEnv encryption plaintext is invalid")
    if encryption.mode == "plaintext":
        return plaintext

    key_id = encryption.active_key_id
    if key_id == DJANGO_SECRET_KEY_ID:
        key = encryption.django_secret_keys[0] if encryption.django_secret_keys else None
    else:
        key = encryption.keys.get(key_id) if isinstance(key_id, str) else None
    if key is None or key_id is None:
        raise RuntimeEnvEncryptionError("django-ray: RuntimeEnv encryption key is unavailable")

    nonce: bytes | None
    try:
        nonce = secrets.token_bytes(12)
    except Exception:
        nonce = None
    if nonce is None or len(nonce) != 12:
        raise RuntimeEnvEncryptionError("django-ray: RuntimeEnv encryption nonce generation failed")

    ciphertext: bytes | None
    try:
        ciphertext = AESGCM(key).encrypt(
            nonce,
            plaintext_bytes,
            _associated_data(
                key_id=key_id,
                task_id=task_id,
                profile=profile,
                digest=digest,
            ),
        )
    except Exception:
        ciphertext = None
    if ciphertext is None:
        raise RuntimeEnvEncryptionError("django-ray: RuntimeEnv snapshot encryption failed")

    return _canonical_json(
        {
            "format": RUNTIME_ENV_ENVELOPE_FORMAT,
            "version": RUNTIME_ENV_ENVELOPE_VERSION,
            "algorithm": RUNTIME_ENV_ENVELOPE_ALGORITHM,
            "key_id": key_id,
            "nonce": _canonical_base64url(nonce),
            "ciphertext": _canonical_base64url(ciphertext),
        }
    )


def unprotect_runtime_env_snapshot(
    serialized_envelope: str,
    *,
    task_id: str,
    profile: str | None,
    digest: str,
    encryption: RuntimeEnvEncryptionConfig,
) -> str:
    """Authenticate and decrypt one strict RuntimeEnv storage envelope."""
    if not isinstance(encryption, RuntimeEnvEncryptionConfig) or not _validate_identity(
        task_id=task_id,
        profile=profile,
        digest=digest,
    ):
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv encryption identity is malformed.")
    if not isinstance(serialized_envelope, str):
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv encryption envelope is malformed.")

    parsed: object | None
    try:
        parsed = json.loads(serialized_envelope)
    except (TypeError, ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, dict) or set(parsed) != _ENVELOPE_FIELDS:
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv encryption envelope is malformed.")
    if (
        not isinstance(parsed.get("format"), str)
        or type(parsed.get("version")) is not int
        or not isinstance(parsed.get("algorithm"), str)
    ):
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv encryption envelope is malformed.")
    if (
        parsed["format"] != RUNTIME_ENV_ENVELOPE_FORMAT
        or parsed["version"] != RUNTIME_ENV_ENVELOPE_VERSION
        or parsed["algorithm"] != RUNTIME_ENV_ENVELOPE_ALGORITHM
    ):
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv encryption metadata is unsupported.")
    if serialized_envelope != _canonical_json(parsed):
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv encryption envelope is malformed.")

    key_id = parsed.get("key_id")
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv encryption envelope is malformed.")
    nonce = _decode_canonical_base64url(parsed.get("nonce"), exact_bytes=12)
    ciphertext = _decode_canonical_base64url(
        parsed.get("ciphertext"),
        minimum_bytes=16,
    )
    if nonce is None or ciphertext is None:
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv encryption envelope is malformed.")

    if key_id == DJANGO_SECRET_KEY_ID:
        candidate_keys = encryption.django_secret_keys if encryption.django_secret_fallback else ()
    else:
        dedicated_key = encryption.keys.get(key_id)
        candidate_keys = (dedicated_key,) if dedicated_key is not None else ()
    if not candidate_keys:
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv decryption key is unavailable.")

    aad = _associated_data(
        key_id=key_id,
        task_id=task_id,
        profile=profile,
        digest=digest,
    )
    plaintext_bytes: bytes | None = None
    unexpected_failure = False
    for key in candidate_keys:
        try:
            plaintext_bytes = AESGCM(key).decrypt(nonce, ciphertext, aad)
        except InvalidTag:
            continue
        except Exception:
            unexpected_failure = True
            break
        else:
            break
    if unexpected_failure or plaintext_bytes is None:
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv authentication failed.")

    plaintext: str | None
    try:
        plaintext = plaintext_bytes.decode("utf-8")
    except UnicodeDecodeError:
        plaintext = None
    if plaintext is None:
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv decrypted payload is invalid.")
    plaintext_error, _ = _plaintext_validation(plaintext, digest=digest)
    if plaintext_error == "hash":
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv plaintext hash does not match.")
    if plaintext_error is not None:
        raise RuntimeEnvEncryptionError("Stored RuntimeEnv decrypted payload is invalid.")
    return plaintext


__all__ = [
    "DJANGO_SECRET_KEY_ID",
    "RUNTIME_ENV_ENVELOPE_ALGORITHM",
    "RUNTIME_ENV_ENVELOPE_FORMAT",
    "RUNTIME_ENV_ENVELOPE_VERSION",
    "RuntimeEnvEncryptionConfig",
    "RuntimeEnvEncryptionError",
    "is_runtime_env_encryption_envelope_candidate",
    "protect_runtime_env_snapshot",
    "unprotect_runtime_env_snapshot",
    "validate_runtime_env_encryption_settings",
]
