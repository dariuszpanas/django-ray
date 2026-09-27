"""Fixed requested Ray resources; selection never establishes observed placement."""

from __future__ import annotations

RESOURCE_PROFILES = {
    "standard": ((1, "750m"), (1, "750m")),
    "constrained-ray": ((0, "350m"), (1, "500m")),
}


def select_profile(name: str, intent: str) -> dict:
    """Keep constrained experiments separate from the standard acceptance gate."""
    if (
        not isinstance(intent, str)
        or not isinstance(name, str)
        or intent not in {"acceptance", "diagnostic"}
        or name not in RESOURCE_PROFILES
    ):
        raise ValueError("Unknown application qualification profile or intent")
    if name != "standard" and intent != "diagnostic":
        raise ValueError("The constrained Ray profile requires diagnostic intent")
    return {
        "resource_profile": name,
        "validation_intent": intent,
        "requested_ray_resources": {
            role: {"logical_cpus": logical, "cpu_request": cpu, "cpu_limit": cpu}
            for role, (logical, cpu) in zip(
                ("head", "worker"), RESOURCE_PROFILES[name], strict=True
            )
        },
    }


def chainsaw_values(profile: dict) -> dict:
    """Render only the selected, source-owned values into the existing fixture."""
    resources = profile["requested_ray_resources"]
    return {
        "resourceProfile": profile["resource_profile"],
        "validationIntent": profile["validation_intent"],
        "rayHeadLogicalCPU": str(resources["head"]["logical_cpus"]),
        "rayWorkerLogicalCPU": str(resources["worker"]["logical_cpus"]),
        "rayHeadCPU": resources["head"]["cpu_limit"],
        "rayWorkerCPU": resources["worker"]["cpu_limit"],
    }
