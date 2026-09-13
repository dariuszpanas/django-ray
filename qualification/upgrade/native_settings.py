"""Settings for the disposable, sequential old/current native rehearsal."""

import json
import os
from pathlib import Path

ROOT = Path(os.environ["DJANGO_RAY_UPGRADE_ROOT"])
CONFIG = json.loads(os.environ["DJANGO_RAY_UPGRADE_CONFIG"])
SECRET_KEY = "disposable-native-upgrade"
USE_TZ = True
INSTALLED_APPS = ["django_ray"]
DATABASES = {"default": CONFIG["database"]}
RUNNER = CONFIG.get("runner", "ray_core")
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "OPTIONS": {"RAY_JOB_ONLY": RUNNER == "ray_job"},
    }
}
DJANGO_RAY = {
    "RUNNER": RUNNER,
    "RAY_ADDRESS": "http://127.0.0.1:8265" if RUNNER == "ray_job" else "auto",
    "DEFAULT_CONCURRENCY": 1,
    "MAX_TASK_ATTEMPTS": 1,
    "WORKER_HEARTBEAT_SECONDS": 2,
    "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT": True,
    "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
    "INPUT_STORAGE_BACKEND": "filesystem",
    "INPUT_STORAGE_FILESYSTEM_PATH": str(Path(CONFIG["artifacts"]) / "inputs"),
    "MAX_RESULT_SIZE_BYTES": 1024,
    "RESULT_STORAGE_BACKEND": "filesystem",
    "RESULT_STORAGE_FILESYSTEM_PATH": str(Path(CONFIG["artifacts"]) / "results"),
}
if RUNNER == "ray_job":
    DJANGO_RAY.update(
        RUNTIME_ENV_STORAGE_MODE="encrypted",
        RUNTIME_ENV_ENCRYPTION_KEYS={"fixture": (ROOT / "runtime-key").read_text()},
        RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY="fixture",
    )
    DJANGO_RAY["RAY_RUNTIME_ENV"] = {
        "working_dir": str(Path(CONFIG["artifacts"]) / "runtime.zip"),
        "env_vars": {
            "PATH": os.environ["PATH"],
            "DJANGO_SETTINGS_MODULE": "qualification.upgrade.native_settings",
            "DJANGO_RAY_UPGRADE_ROOT": str(ROOT),
            "DJANGO_RAY_UPGRADE_CONFIG": os.environ["DJANGO_RAY_UPGRADE_CONFIG"],
            "PYTHONPATH": os.environ["PYTHONPATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "QUALIFICATION_RUNTIME_MARKER": "delivered",
        },
    }
