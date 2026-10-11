"""Jobs-only overlay for the owned application assertion subprocesses."""

import copy
import os
from pathlib import Path

if os.environ.get("DJANGO_RAY_QUALIFICATION_RQ2") != "owned-application-job":
    raise ValueError("Jobs qualification requires the explicit owned fixture")
RQ2_GENERATION = os.environ.get("DJANGO_RAY_QUALIFICATION_RQ2_GENERATION")
if RQ2_GENERATION not in {"before", "after"}:
    raise ValueError("Jobs qualification requires a fixed generation")
RQ2_ROOT = Path("/rq2") / RQ2_GENERATION
# Record an attempted settings bootstrap before Django or the base settings can
# fail. Only the negative driver's RuntimeEnv contains this fixed marker.
if os.environ.get("DJANGO_RAY_QUALIFICATION_RQ2_SETUP_MARKER") == "missing":
    (RQ2_ROOT / "missing-setup").touch(exist_ok=False)

from . import settings_qualification as _base  # noqa: E402

globals().update({name: value for name, value in vars(_base).items() if name.isupper()})
TASKS = copy.deepcopy(_base.TASKS)
TASKS["rq2-qualification"] = {
    "BACKEND": "django_ray.backends.RayTaskBackend",
    "QUEUES": ["rq2-qualification"],
    "OPTIONS": {
        "RAY_ADDRESS": "http://ray-head:8265",
        "RAY_JOB_ONLY": True,
        "RUNTIME_ENV_PROFILE": "rq2-qualification",
    },
}
DJANGO_RAY = copy.deepcopy(_base.DJANGO_RAY)
# Lease cleanup spans every queue in the shared database. Inherit the Core
# manager's expiry horizon even though this owned manager heartbeats faster.
DJANGO_RAY.update(
    RUNNER="ray_job",
    RAY_ADDRESS="http://ray-head:8265",
    DEFAULT_CONCURRENCY=1,
    WORKER_HEARTBEAT_SECONDS=2,
    STUCK_TASK_TIMEOUT_SECONDS=30,
    # Keep retries enabled: the missing request must refuse automatic replay.
    MAX_TASK_ATTEMPTS=3,
    INPUT_STORAGE_BACKEND="filesystem",
    INPUT_STORAGE_FILESYSTEM_PATH="/rq2/requests",
)
DJANGO_RAY["RUNTIME_ENV_PROFILES"]["rq2-qualification"] = {
    "extends": "project",
    "runtime_env": {
        "env_vars": {
            "DJANGO_SETTINGS_MODULE": "testproject.settings_qualification_jobs",
            "DJANGO_RAY_QUALIFICATION_RQ2": "owned-application-job",
            "DJANGO_RAY_QUALIFICATION_RQ2_GENERATION": RQ2_GENERATION,
        }
    },
}
DJANGO_RAY["RUNTIME_ENV_PROFILES"]["rq2-missing"] = {
    "extends": "rq2-qualification",
    "runtime_env": {"env_vars": {"DJANGO_RAY_QUALIFICATION_RQ2_SETUP_MARKER": "missing"}},
}
TASKS["rq2-missing"] = copy.deepcopy(TASKS["rq2-qualification"])
TASKS["rq2-missing"]["OPTIONS"]["RUNTIME_ENV_PROFILE"] = "rq2-missing"
