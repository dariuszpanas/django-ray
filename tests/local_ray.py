"""Explicit allocation for serial Linux test runtimes.

These are Ray scheduler and object-store settings, not OS resource limits.
The admitting Linux runner must still bound CPU, RAM, PIDs, scratch and time.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from scripts.require_linux import require_linux

OBJECT_STORE_BYTES = 128 * 1024 * 1024
MAX_LOGICAL_CPUS = 4
SHARED_LOCAL_RAY_ENV = "DJANGO_RAY_SHARED_LOCAL_RAY"
_SHARED_LOCAL_RAY_RESOURCES: dict[str, float] = {
    "result_buffer": 1,
    "result_fold": 1,
}
_shared_local_ray_active = False


def init_local_ray(
    *,
    num_cpus: int = 2,
    include_dashboard: bool = False,
    resources: dict[str, float] | None = None,
) -> None:
    """Start an owned runtime without host discovery or host-sized allocation."""
    require_linux()
    if type(num_cpus) is not int or not 1 <= num_cpus <= MAX_LOGICAL_CPUS:
        raise ValueError(f"Local Ray tests require 1..{MAX_LOGICAL_CPUS} logical CPUs")

    import ray

    if ray.is_initialized():
        raise RuntimeError("Required local Ray fixture found an initialized runtime")
    try:
        ray.init(
            address="local",
            num_cpus=num_cpus,
            num_gpus=0,
            object_store_memory=OBJECT_STORE_BYTES,
            include_dashboard=include_dashboard,
            resources=resources,
        )
    except Exception:
        # Do not let a failed startup overlap the next owned local runtime.
        # The admitting test runner supplies the enclosing execution deadline.
        ray.shutdown(wait_for_processes=True)
        raise


def use_shared_local_ray() -> bool:
    """Return True when the dedicated serial real_ray lane opts into one session runtime."""
    return os.environ.get(SHARED_LOCAL_RAY_ENV) == "1"


def ensure_shared_local_ray() -> Any:
    """Boot the bounded dashboard-free session Ray runtime once and return the ray module."""
    global _shared_local_ray_active
    import ray

    if not _shared_local_ray_active:
        init_local_ray(
            num_cpus=MAX_LOGICAL_CPUS,
            include_dashboard=False,
            resources=dict(_SHARED_LOCAL_RAY_RESOURCES),
        )
        _shared_local_ray_active = True
    return ray


def shutdown_shared_local_ray() -> None:
    """Tear down the session-shared local Ray runtime if this process started it."""
    global _shared_local_ray_active
    if not _shared_local_ray_active:
        return
    import ray

    try:
        ray.shutdown(wait_for_processes=True)
    finally:
        _shared_local_ray_active = False


@contextmanager
def local_ray_runtime(
    *,
    num_cpus: int = 2,
    include_dashboard: bool = False,
    resources: dict[str, float] | None = None,
    wrap_startup_error: bool = False,
    isolated: bool = False,
) -> Generator[Any, None, None]:
    """Yield the shared session runtime when compatible, else an isolated bounded runtime."""
    if use_shared_local_ray() and not isolated and not include_dashboard:
        yield ensure_shared_local_ray()
        return

    if _shared_local_ray_active:
        shutdown_shared_local_ray()

    import ray

    if ray.is_initialized():
        raise RuntimeError("Required local Ray fixture found an initialized runtime")
    try:
        init_local_ray(
            num_cpus=num_cpus,
            include_dashboard=include_dashboard,
            resources=resources,
        )
    except Exception as exc:
        if wrap_startup_error:
            raise RuntimeError(f"Required local Ray startup failed: {exc}") from exc
        raise
    try:
        yield ray
    finally:
        ray.shutdown(wait_for_processes=True)
