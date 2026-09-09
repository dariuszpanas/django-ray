"""One disposable database shared by the driver, managers and real Ray Jobs."""

import json
import os
from pathlib import Path

ROOT = Path(os.environ["DJANGO_RAY_LATENCY_ROOT"])
CONFIG = json.loads((ROOT / "config.json").read_text())
SECRET_KEY = "disposable-latency-qualification"
USE_TZ = True
INSTALLED_APPS = ["django_ray", "qualification.latency.apps.ProbeConfig"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(ROOT / "db.sqlite3"),
        "OPTIONS": {"timeout": 10},
    }
}
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "OPTIONS": {"RAY_JOB_ONLY": True},
    }
}
DJANGO_RAY = {
    "RUNNER": "ray_job",
    "RAY_ADDRESS": CONFIG["address"],
    "DEFAULT_CONCURRENCY": 1,
    "MAX_TASK_ATTEMPTS": 1,
    "WORKER_HEARTBEAT_SECONDS": 2,
    "WORKER_POLL_INTERVAL_SECONDS": 0.1,
    "WORKER_POLL_MAX_INTERVAL_SECONDS": 5.0,
    "INPUT_STORAGE_BACKEND": "filesystem",
    "INPUT_STORAGE_FILESYSTEM_PATH": str(ROOT / "inputs"),
    "RAY_RUNTIME_ENV": {
        "env_vars": {
            "DJANGO_SETTINGS_MODULE": "qualification.latency.settings",
            "DJANGO_RAY_LATENCY_ROOT": str(ROOT),
            "PYTHONPATH": CONFIG["pythonpath"],
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    },
}
