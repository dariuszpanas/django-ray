"""Fail closed before allocating a disposable hosted monitoring cluster."""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

GIB = 1024**3


def admit(*, hosted: bool, linux: bool, cpus: float, total: int, available: int, disk: int) -> dict:
    """Require explicit host capacity; limits do not imply capacity exists."""
    if (
        not hosted
        or not linux
        or cpus < 4
        or total < 15 * GIB
        or available < 13 * GIB
        or disk < 20 * GIB
    ):
        raise ValueError("Disposable hosted monitoring capacity was not admitted")
    return {
        "host_cpu_count": cpus,
        "host_memory_bytes": total,
        "available_memory_bytes": available,
        "available_disk_bytes": disk,
        "kind_cpu_limit_millicores": 3250,
        "kind_memory_limit_mib": 12288,
        "pod_cpu_limits_millicores": 2850,
        "pod_memory_limits_mib": 9344,
        "kind_system_cpu_headroom_millicores": 400,
        "kind_system_memory_headroom_mib": 2944,
        "registry_cpu_limit_millicores": 250,
        "registry_memory_limit_mib": 128,
        "browser_cpu_limit_millicores": 250,
        "browser_memory_limit_mib": 512,
        "browser_pids_limit": 256,
        "browser_render_seconds": 90,
        "host_cpu_headroom_millicores": 250,
        "observation_seconds": 600,
        "teardown_seconds": 300,
    }


def effective_capacity(
    cpus: float, total: int, available: int, cgroup: Path
) -> tuple[float, int, int]:
    """Account for inherited cgroup v2 limits as well as affinity/host memory."""
    quota, period = (cgroup / "cpu.max").read_text().split()
    effective = float(cpus) if quota == "max" else min(float(cpus), int(quota) / int(period))
    limit = (cgroup / "memory.max").read_text().strip()
    if limit != "max":
        ceiling = int(limit)
        total = min(total, ceiling)
        available = min(available, max(0, ceiling - int((cgroup / "memory.current").read_text())))
    return effective, total, available


def main() -> None:
    """This command is deliberately unavailable on a shared/local workstation."""
    if platform.system() != "Linux":
        raise SystemExit("Monitoring admission requires Linux")
    memory = {
        line.split(":")[0]: int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemTotal:", "MemAvailable:"))
    }
    root = Path("/sys/fs/cgroup").resolve()
    entries = Path("/proc/self/cgroup").read_text().splitlines()
    unified = next(line.removeprefix("0::") for line in entries if line.startswith("0::"))
    current = (root / unified.lstrip("/")).resolve()
    if not current.is_relative_to(root) or len(current.relative_to(root).parts) > 16:
        raise ValueError("Inherited cgroup boundary is unavailable")
    cpus, total, available = (
        float(len(os.sched_getaffinity(0))),
        memory["MemTotal"],
        memory["MemAvailable"],
    )
    # The cgroup-v2 root has no cpu.max/memory.max knobs. Affinity and
    # /proc/meminfo supply its host ceiling; every non-root ancestor is read.
    while current != root:
        cpus, total, available = effective_capacity(cpus, total, available, current)
        current = current.parent
    result = admit(
        hosted=os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
        linux=True,
        cpus=cpus,
        total=total,
        available=available,
        disk=shutil.disk_usage(os.environ["RUNNER_TEMP"]).free,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
