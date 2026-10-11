"""Settings for one owned local PostgreSQL/Ray Jobs cancellation experiment."""

import json
import os
from pathlib import Path

CONFIG = json.loads(os.environ["DJANGO_RAY_CANCELLATION_CONFIG"])
ROOT = Path(CONFIG["root"])
SECRET_KEY = "owned-cancellation-fixture"
USE_TZ = True
INSTALLED_APPS = ["django_ray"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "restored",
        "USER": "qualification",
        "HOST": str(ROOT / "socket"),
        "PORT": "5432",
        "OPTIONS": {"connect_timeout": 2, "options": "-c statement_timeout=5000"},
    }
}
TASKS = {
    "cancellation": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["cancellation"],
        "OPTIONS": {"RAY_JOB_ONLY": True},
    }
}
DJANGO_RAY = {
    "RUNNER": "ray_job",
    "RAY_ADDRESS": "http://127.0.0.1:8265",
    "DEFAULT_CONCURRENCY": 1,
    "MAX_TASK_ATTEMPTS": 3,
    "WORKER_LEASE_SECONDS": 60,
    "WORKER_HEARTBEAT_SECONDS": 2,
    "INPUT_STORAGE_BACKEND": "filesystem",
    "INPUT_STORAGE_FILESYSTEM_PATH": str(ROOT / "requests"),
    "RAY_RUNTIME_ENV": {
        "env_vars": {
            "DJANGO_SETTINGS_MODULE": "qualification.cancellation.settings",
            "DJANGO_RAY_CANCELLATION_CONFIG": os.environ["DJANGO_RAY_CANCELLATION_CONFIG"],
            "PYTHONPATH": os.environ["PYTHONPATH"],
            "PATH": os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    },
}
