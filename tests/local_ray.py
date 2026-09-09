"""Explicit allocation for serial Linux test runtimes.

These are Ray scheduler and object-store settings, not OS resource limits.
The admitting Linux runner must still bound CPU, RAM, PIDs, scratch and time.
"""

from __future__ import annotations

from scripts.require_linux import require_linux

OBJECT_STORE_BYTES = 128 * 1024 * 1024
MAX_LOGICAL_CPUS = 4


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
        ray.shutdown()
        raise
