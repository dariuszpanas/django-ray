"""Django settings for testproject."""

import os
from collections.abc import Callable
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.templatetags.static import static

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable without treating typos as truthy."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"true", "1", "yes", "on"}


def _looks_like_placeholder(value: str) -> bool:
    """Reject common sample values that are never safe deployment credentials."""
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in ("change-me", "replace-with", "placeholder", "example", "insecure")
    )


# The sample is safe by default for a local container, while the explicitly selected
# production mode fails closed when required deployment secrets are absent.
DEPLOYMENT_MODE = os.environ.get("DJANGO_DEPLOYMENT_MODE", "demo").strip().lower()
if DEPLOYMENT_MODE not in {"demo", "production"}:
    raise ImproperlyConfigured("DJANGO_DEPLOYMENT_MODE must be 'demo' or 'production'.")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool("DJANGO_DEBUG", default=False)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-local-demo-key-do-not-use-in-production",
)

_allowed_hosts_value = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
ALLOWED_HOSTS = [host.strip() for host in _allowed_hosts_value.split(",") if host.strip()]

if DEPLOYMENT_MODE == "production":
    if DEBUG:
        raise ImproperlyConfigured("DJANGO_DEBUG must be false in production mode.")
    if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or _looks_like_placeholder(SECRET_KEY):
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be a random value of at least 50 characters in production."
        )
    if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            "DJANGO_ALLOWED_HOSTS must contain explicit hostnames in production mode."
        )

DJANGO_API_TOKEN = os.environ.get("DJANGO_API_TOKEN")
if DEPLOYMENT_MODE == "production" and (
    not DJANGO_API_TOKEN
    or len(DJANGO_API_TOKEN) < 32
    or len(set(DJANGO_API_TOKEN)) < 5
    or _looks_like_placeholder(DJANGO_API_TOKEN)
):
    raise ImproperlyConfigured(
        "DJANGO_API_TOKEN must be a random value of at least 32 characters in production."
    )

# Application definition
INSTALLED_APPS = [
    "unfold",  # Must precede django.contrib.admin so its templates and static assets win.
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # django-ray
    "django_ray",
    # Testproject-owned management commands and integration helpers.
    "testproject",
    # Example apps demonstrating different execution modes
    # (These are in testproject/apps/ for demonstration purposes)
]


def _static_asset(path: str) -> Callable[[object], str]:
    """Resolve an Unfold asset at request time so manifests remain supported."""

    def resolve(_request: object) -> str:
        return static(path)

    return resolve


UNFOLD = {
    "SITE_TITLE": "django-ray admin",
    "SITE_HEADER": "django-ray",
    "SITE_SUBHEADER": "Distributed task testproject",
    "SITE_ICON": _static_asset("testproject/django-ray.svg"),
    "SITE_FAVICONS": [
        {
            "href": _static_asset("testproject/django-ray.svg"),
            "rel": "icon",
            "type": "image/svg+xml",
        }
    ],
    "STYLES": [_static_asset("testproject/admin.css")],
    "BORDER_RADIUS": "8px",
    "COLORS": {
        "base": {
            "50": "#f8fafc",
            "100": "#f1f5f9",
            "200": "#e2e8f0",
            "300": "#cbd5e1",
            "400": "#94a3b8",
            "500": "#64748b",
            "600": "#475569",
            "700": "#334155",
            "800": "#1e293b",
            "900": "#0f172a",
            "950": "#020617",
        },
        "primary": {
            "50": "#f0f9ff",
            "100": "#e0f2fe",
            "200": "#bae6fd",
            "300": "#7dd3fc",
            "400": "#38bdf8",
            "500": "#0ea5e9",
            "600": "#075985",
            "700": "#0c4a6e",
            "800": "#082f49",
            "900": "#062338",
            "950": "#03151f",
        },
    },
    "LOGIN": {
        "image": _static_asset("testproject/landing-graph-bg.png"),
    },
}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # Serve static files in production
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "testproject.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "testproject" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "testproject.wsgi.application"

# Database
# Use environment variables for Kubernetes/production deployment
# Falls back to SQLite for local development
DATABASE_ENGINE = os.environ.get("DATABASE_ENGINE", "django.db.backends.sqlite3")

if DATABASE_ENGINE == "django.db.backends.sqlite3":
    DATABASES = {
        "default": {
            "ENGINE": DATABASE_ENGINE,
            "NAME": os.environ.get("DATABASE_NAME", str(BASE_DIR / "db.sqlite3")),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": DATABASE_ENGINE,
            "NAME": os.environ.get("DATABASE_NAME", "django_ray"),
            "USER": os.environ.get("DATABASE_USER", "django_ray"),
            "PASSWORD": os.environ.get("DATABASE_PASSWORD", ""),
            "HOST": os.environ.get("DATABASE_HOST", "localhost"),
            "PORT": os.environ.get("DATABASE_PORT", "5432"),
            "CONN_MAX_AGE": 60,  # Keep connections open for 60 seconds
            "CONN_HEALTH_CHECKS": True,  # Check connection health before use
            "OPTIONS": {
                "connect_timeout": 10,
            },
        }
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles")))
STATICFILES_DIRS = [BASE_DIR / "testproject" / "static"]

# Whitenoise for serving static files in production
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Ray Dashboard URL used by Django admin deep links.
RAY_DASHBOARD_URL = os.environ.get("RAY_DASHBOARD_URL", "http://localhost:8265")

# Django 6 Tasks Configuration - Use Ray backend for distributed execution
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default", "high-priority", "low-priority", "sync", "ml"],
        "OPTIONS": {
            "RAY_ADDRESS": os.environ.get("RAY_ADDRESS", "auto"),
            "RUNTIME_ENV_PROFILE": "project",
        },
    },
    "thin": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {
            "RAY_ADDRESS": os.environ.get("RAY_ADDRESS", "auto"),
            "RUNTIME_ENV_PROFILE": "thin",
        },
    },
    "numpy-2-2": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {
            "RAY_ADDRESS": os.environ.get("RAY_ADDRESS", "auto"),
            "RUNTIME_ENV_PROFILE": "numpy-2-2",
        },
    },
    "numpy-2-3": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {
            "RAY_ADDRESS": os.environ.get("RAY_ADDRESS", "auto"),
            "RUNTIME_ENV_PROFILE": "numpy-2-3",
        },
    },
}

_runtime_env_encryption_active_key = os.environ.get("DJANGO_RAY_RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY")
if _runtime_env_encryption_active_key is not None:
    _runtime_env_encryption_active_key = _runtime_env_encryption_active_key.strip() or None

# django-ray configuration used by the library backend and worker command.
DJANGO_RAY = {
    # Use "auto" for local Ray, or "ray://host:port" for cluster
    "RAY_ADDRESS": os.environ.get("RAY_ADDRESS", "auto"),
    "RAY_STATE_API_ADDRESS": os.environ.get("RAY_STATE_API_ADDRESS"),
    "RUNTIME_ENV_PROFILES": {
        "project": {
            "working_dir": os.environ.get(
                "DJANGO_RAY_WORKING_DIR_URI",
                str(BASE_DIR),
            ),
            "excludes": [".git", ".venv", "__pycache__", "staticfiles"],
            "pip": [
                "cryptography>=42.0.8",
                "django>=6.0",
                "django-unfold==0.102.0",
                "psycopg[binary]>=3.1",
                "django-ninja>=1.5.1",
                "whitenoise>=6.6",
            ],
            "env_vars": {
                "DJANGO_RAY_RUNTIME_ENV": "project",
                "DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE": (
                    "django-ray-runtime-env-encryption-canary-v1-7c4e2a91"
                ),
                "PYTHONPATH": "src",
            },
        },
        "thin": {
            "extends": "project",
            "runtime_env": {
                "env_vars": {"DJANGO_RAY_RUNTIME_ENV": "thin"},
            },
        },
        "numpy-2-2": {
            "extends": "project",
            "runtime_env": {
                "pip": ["numpy==2.2.6"],
                "env_vars": {"DJANGO_RAY_RUNTIME_ENV": "numpy-2-2"},
            },
        },
        "numpy-2-3": {
            "extends": "project",
            "runtime_env": {
                "pip": ["numpy==2.3.5"],
                "env_vars": {"DJANGO_RAY_RUNTIME_ENV": "numpy-2-3"},
            },
        },
    },
    "DEFAULT_RUNTIME_ENV_PROFILE": "project",
    "RUNTIME_ENV_STORAGE_MODE": os.environ.get(
        "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE",
        "plaintext",
    ).strip(),
    "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": _runtime_env_encryption_active_key,
    "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": _env_bool(
        "DJANGO_RAY_RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK",
        default=False,
    ),
    "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT": _env_bool(
        "DJANGO_RAY_WORKFLOW_PROGRESS_SCHEMA_V3_PILOT",
        default=True,
    ),
    "MAX_TASK_ATTEMPTS": int(os.environ.get("RAY_MAX_RETRIES", "3")),
    "RETRY_BACKOFF_SECONDS": int(os.environ.get("RAY_RETRY_DELAY_SECONDS", "5")),
    # Exceptions that won't trigger auto-retry (use for manual retry testing)
    "RETRY_EXCEPTION_DENYLIST": [
        "testproject.tasks.NoRetryError",
        "testproject.apps.cluster_tasks.workflows.ComplexWorkflowFixtureError",
        "testproject.apps.cluster_tasks.workflows.WorkflowShowcaseFixtureError",
    ],
}
