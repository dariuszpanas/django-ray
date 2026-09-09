"""Only the disposable server's private Unix socket is addressable."""

import os

SECRET_KEY = "disposable-transaction-qualification"
USE_TZ = True
INSTALLED_APPS = ["django_ray"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "postgres",
        "USER": "qualification",
        "HOST": os.environ["DJANGO_RAY_TRANSACTION_SOCKET"],
        "PORT": "5432",
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 5, "options": "-c statement_timeout=10000"},
        "TEST": {"NAME": "test_receipts"},
    }
}
TASKS = {"default": {"BACKEND": "django_ray.backends.RayTaskBackend"}}
DJANGO_RAY = {"RAY_ADDRESS": "auto"}
