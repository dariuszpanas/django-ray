"""Explicit offline settings for the disposable application qualification image.

The ordinary sample settings and dependency-download profiles remain unchanged.
This module must travel inside the locked recovery archive to generic Ray nodes.
"""

import copy
import os
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.runtime_env_encryption import validate_runtime_env_encryption_settings

from . import settings as _base

# Retain production validation, middleware, installed apps, API and static rules.
globals().update({name: value for name, value in vars(_base).items() if name.isupper()})

if _base.DEPLOYMENT_MODE != "production":
    raise ImproperlyConfigured("Application qualification requires production deployment mode.")
if _base.DATABASE_ENGINE != "django.db.backends.postgresql":
    raise ImproperlyConfigured("Application qualification requires disposable PostgreSQL.")

_address = os.environ.get("RAY_ADDRESS", "")
_target = urlsplit(_address)
if (
    _target.scheme != "ray"
    or not _target.hostname
    or not _target.port
    or _target.username is not None
    or _target.password is not None
    or _target.path
    or "?" in _address
    or "#" in _address
    or "\\" in _address
    or not _address.isascii()
    or any(ord(char) <= 32 or ord(char) == 127 for char in _address)
):
    raise ImproperlyConfigured("Application qualification requires an explicit Ray Client target.")

_archive = os.environ.get("DJANGO_RAY_RECOVERY_WORKING_DIR", "")
_archive_path = PurePosixPath(_archive)
if (
    not _archive_path.is_absolute()
    or ".." in _archive_path.parts
    or _archive_path.suffix != ".zip"
    or str(_archive_path) != _archive
):
    raise ImproperlyConfigured("Application qualification requires an absolute Linux archive path.")

DJANGO_RAY = copy.deepcopy(_base.DJANGO_RAY)
DJANGO_RAY["RUNTIME_ENV_PROFILES"]["project"] = {
    "working_dir": _archive,
    "env_vars": {
        # Preserve the application's validated client target when the generic
        # Ray Pod supplies a different node-local RAY_ADDRESS to its workers.
        "RAY_ADDRESS": _address,
        "DJANGO_RAY_RUNTIME_ENV": "project",
        "DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE": (
            "django-ray-runtime-env-encryption-canary-v1-7c4e2a91"
        ),
    },
}
# The recovery bundle puts django_ray at its root. There is no src/ PYTHONPATH
# and no pip installer or implicit package-index egress in this profile.
DJANGO_RAY.update(
    RUNTIME_ENV_STORAGE_MODE="encrypted",
    RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY="qualification",
    RUNTIME_ENV_ENCRYPTION_KEYS={
        "qualification": os.environ.get("DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY", "")
    },
    RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK=False,
)
validate_runtime_env_encryption_settings(
    DJANGO_RAY, django_secret_key=_base.SECRET_KEY, django_secret_key_fallbacks=[]
)
