"""Core Ray integration with Django."""

import ray


def initialize_ray() -> None:
    """Initialize Ray cluster if not already initialized."""
    if not ray.is_initialized():
        ray.init()


def shutdown_ray() -> None:
    """Shutdown Ray cluster if initialized."""
    if ray.is_initialized():
        ray.shutdown()
