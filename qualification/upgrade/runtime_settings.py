"""Explicit settings for fresh installed-wheel native upgrade processes.

The orchestrator supplies every connection, credential and existing artifact
directory. Importing this module validates configuration without connecting to
PostgreSQL or Ray, creating storage, or loading the sample application's settings.
The same module and encrypted profile travel in both source-owned runtime images.
"""

from __future__ import annotations

import os
import platform
import re
import stat
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from django_ray import __version__ as _package_version
from django_ray.runtime.runtime_env_encryption import validate_runtime_env_encryption_settings

SETTINGS_MODULE = "qualification.upgrade.runtime_settings"
CORE_QUEUE = "upgrade-core"
JOBS_QUEUE = "upgrade-jobs"
PROFILE_NAME = "upgrade"
_PREFIX = "DJANGO_RAY_UPGRADE_"
_RUNTIMES = {"baseline": ("0.4.0", "2.56.0"), "candidate": ("0.5.0", "2.58.0")}
_REQUIRED = (
    "BUILD",
    "PYTHON_VERSION",
    "DATABASE",
    "PRIMARY_DATABASE",
    "SCRATCH_DATABASE",
    "CORE_ADDRESS",
    "JOBS_ADDRESS",
    "ARTIFACT_ROOT",
    "ENCRYPTION_KEY_FILE",
)
_POSTGRES = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD_FILE")


def _reject():
    raise ImproperlyConfigured("invalid-native-upgrade-settings") from None


def _text(environment, key, maximum=4096):
    value = environment.get(key)
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        _reject()
    return value


def _endpoint(address, *, jobs):
    try:
        if not address.isascii() or len(address) > 512 or any(ord(c) <= 32 for c in address):
            _reject()
        parsed = urlsplit(address if jobs else "tcp://" + address)
        if (
            parsed.scheme not in ({"http", "https"} if jobs else {"tcp"})
            or not parsed.hostname
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or any(c in address for c in "?#%\\")
        ):
            _reject()
        if not jobs and re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", address) is None:
            _reject()
    except ValueError:
        _reject()
    return address


def _existing_root(raw):
    try:
        root = Path(raw)
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            _reject()
        for name in ("inputs", "results", "runtime-effects"):
            child = root / name
            if child.is_symlink() or not child.is_dir() or child.resolve(strict=True) != child:
                _reject()
    except (OSError, ValueError):
        _reject()
    return root


def _secret_file(raw):
    try:
        path = Path(raw)
        # Kubernetes projected Secret keys are symlinks to regular files. Follow
        # that source-owned mount shape, while refusing FIFOs/devices/directories.
        if not path.is_absolute() or not stat.S_ISREG(path.stat().st_mode):
            _reject()
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                _reject()
            value = stream.read(4097)
        if not 0 < len(value) <= 4096:
            _reject()
        return _text({"secret": value.decode("utf-8")}, "secret")
    except (OSError, UnicodeError, ValueError):
        _reject()


def build_runtime_settings(environment, *, package_version, ray_version, python_version):
    """Validate a captured environment; observed versions are supplied by the loader.

    Returning settings is configuration validation, not connection, migration,
    installed-source, stopped-writer, or native-execution qualification.
    """
    values = {name: _text(environment, _PREFIX + name) for name in _REQUIRED}
    postgres = {name: _text(environment, _PREFIX + name) for name in _POSTGRES}
    secret_path = _text(environment, "DJANGO_SECRET_KEY_FILE")
    secret = _secret_file(secret_path)
    password = _secret_file(postgres["POSTGRES_PASSWORD_FILE"])
    encryption_key = _secret_file(values["ENCRYPTION_KEY_FILE"])
    expected = _RUNTIMES.get(values["BUILD"])
    if (
        expected is None
        or (package_version, ray_version) != expected
        or re.fullmatch(r"3\.(?:12|13|14)\.(?:0|[1-9][0-9]{0,2})", values["PYTHON_VERSION"]) is None
        or python_version != values["PYTHON_VERSION"]
        or values["DATABASE"] not in {"primary", "scratch"}
        or len(secret) < 32
    ):
        _reject()
    names = [values["PRIMARY_DATABASE"], values["SCRATCH_DATABASE"], postgres["POSTGRES_USER"]]
    if any(re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) is None for name in names):
        _reject()
    if names[0] == names[1]:
        _reject()
    host = postgres["POSTGRES_HOST"]
    if (
        len(host) > 253
        or re.fullmatch(r"[A-Za-z0-9.\-]+", host) is None
        or re.fullmatch(r"[1-9][0-9]{0,4}", postgres["POSTGRES_PORT"]) is None
        or int(postgres["POSTGRES_PORT"]) > 65535
    ):
        _reject()
    core = _endpoint(values["CORE_ADDRESS"], jobs=False)
    jobs = _endpoint(values["JOBS_ADDRESS"], jobs=True)
    root = _existing_root(values["ARTIFACT_ROOT"])
    selected_database = values[values["DATABASE"].upper() + "_DATABASE"]
    # Only the known configuration is carried into remote task interpreters.
    # In particular, do not override the Ray supervisor's RAY_ADDRESS or JobConfig.
    profile_environment = {_PREFIX + name: value for name, value in values.items()}
    profile_environment.update({_PREFIX + name: value for name, value in postgres.items()})
    profile_environment.update(
        DJANGO_SETTINGS_MODULE=SETTINGS_MODULE, DJANGO_SECRET_KEY_FILE=secret_path
    )
    config = {
        "RAY_ADDRESS": core,
        "RAY_STATE_API_ADDRESS": jobs,
        "RAY_STATE_API_TIMEOUT_SECONDS": 5,
        "RUNNER": "ray_job",
        "DEFAULT_CONCURRENCY": 1,
        "WORKER_POLL_INTERVAL_SECONDS": 0.1,
        "WORKER_POLL_MAX_INTERVAL_SECONDS": 0.1,
        "WORKER_LEASE_SECONDS": 15,
        "WORKER_HEARTBEAT_SECONDS": 3,
        "TASK_MONITOR_HEARTBEAT_SECONDS": 3,
        "STUCK_TASK_TIMEOUT_SECONDS": 60,
        "MAX_TASK_ATTEMPTS": 2,
        "RETRY_BACKOFF_SECONDS": 1,
        "RETRY_EXCEPTION_DENYLIST": ["qualification.upgrade.runtime_tasks.FixtureTerminalError"],
        "QUEUE_TIMEOUT_SECONDS": 900,
        "RUNTIME_ENV_PROFILES": {PROFILE_NAME: {"env_vars": profile_environment}},
        "DEFAULT_RUNTIME_ENV_PROFILE": PROFILE_NAME,
        "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {"upgrade": encryption_key},
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "upgrade",
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": False,
        "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(root / "inputs"),
        "MAX_RESULT_SIZE_BYTES": 1024,
        "RESULT_STORAGE_BACKEND": "filesystem",
        "RESULT_STORAGE_FILESYSTEM_PATH": str(root / "results"),
    }
    try:
        validate_runtime_env_encryption_settings(
            config, django_secret_key=secret, django_secret_key_fallbacks=[]
        )
    except ImproperlyConfigured:
        _reject()
    return {
        "SECRET_KEY": secret,
        "DEBUG": False,
        "USE_TZ": True,
        "TIME_ZONE": "UTC",
        "DEFAULT_AUTO_FIELD": "django.db.models.BigAutoField",
        "INSTALLED_APPS": ["django_ray"],
        "DATABASE_ROUTERS": [],
        "DATABASES": {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": selected_database,
                "HOST": host,
                "PORT": postgres["POSTGRES_PORT"],
                "USER": postgres["POSTGRES_USER"],
                "PASSWORD": password,
                "CONN_MAX_AGE": 0,
                "OPTIONS": {
                    "connect_timeout": 5,
                    "options": "-c statement_timeout=30000 -c lock_timeout=5000",
                },
            }
        },
        "TASKS": {
            "default": {
                "BACKEND": "django_ray.backends.RayTaskBackend",
                "QUEUES": [CORE_QUEUE],
                "OPTIONS": {
                    "RAY_ADDRESS": core,
                    "RUNTIME_ENV_PROFILE": PROFILE_NAME,
                    "TIMEOUT_SECONDS": 240,
                },
            },
            "jobs": {
                "BACKEND": "django_ray.backends.RayTaskBackend",
                "QUEUES": [JOBS_QUEUE],
                "OPTIONS": {
                    "RAY_ADDRESS": jobs,
                    "RAY_JOB_ONLY": True,
                    "RUNTIME_ENV_PROFILE": PROFILE_NAME,
                    "TIMEOUT_SECONDS": 240,
                },
            },
        },
        "DJANGO_RAY": config,
    }


if version("django-ray") != _package_version or platform.python_implementation() != "CPython":
    _reject()
globals().update(
    build_runtime_settings(
        dict(os.environ),
        package_version=_package_version,
        ray_version=version("ray"),
        python_version=platform.python_version(),
    )
)
