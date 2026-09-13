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
TASKS = {"default": {"BACKEND": "django_ray.backends.RayTaskBackend"}}
DJANGO_RAY = {
    "RUNNER": "ray_core",
    "RAY_ADDRESS": "auto",
    "DEFAULT_CONCURRENCY": 1,
    "MAX_TASK_ATTEMPTS": 1,
    "WORKER_HEARTBEAT_SECONDS": 2,
    "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
    "INPUT_STORAGE_BACKEND": "filesystem",
    "INPUT_STORAGE_FILESYSTEM_PATH": str(Path(CONFIG["artifacts"]) / "inputs"),
    "MAX_RESULT_SIZE_BYTES": 1024,
    "RESULT_STORAGE_BACKEND": "filesystem",
    "RESULT_STORAGE_FILESYSTEM_PATH": str(Path(CONFIG["artifacts"]) / "results"),
}
