"""Verify the health and ownership boundaries of bundled Prometheus targets."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast
from urllib.request import OpenerDirector, Request, urlopen

EXPECTED_JOBS = ("django-ray", "ray-head", "ray-workers")
REMOVED_WORKER_JOB = "django-ray-worker"


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Prometheus response field {field} must be an object")
    return cast("Mapping[str, Any]", value)


def inspect_target_health(payload: object) -> tuple[dict[str, int], list[str]]:
    """Return expected target counts and actionable health problems."""
    response = _mapping(payload, field="response")
    if response.get("status") != "success":
        raise ValueError("Prometheus targets API did not return status=success")

    data = _mapping(response.get("data"), field="data")
    active_targets = data.get("activeTargets")
    if not isinstance(active_targets, list):
        raise ValueError("Prometheus response field data.activeTargets must be a list")

    targets_by_job: dict[str, list[Mapping[str, Any]]] = {}
    for index, value in enumerate(active_targets):
        target = _mapping(value, field=f"data.activeTargets[{index}]")
        labels = _mapping(target.get("labels"), field=f"data.activeTargets[{index}].labels")
        job = labels.get("job")
        if not isinstance(job, str) or not job:
            raise ValueError(
                f"Prometheus response field data.activeTargets[{index}].labels.job "
                "must be a non-empty string"
            )
        targets_by_job.setdefault(job, []).append(target)

    counts = {job: len(targets_by_job.get(job, ())) for job in EXPECTED_JOBS}
    problems: list[str] = []
    for job in EXPECTED_JOBS:
        targets = targets_by_job.get(job, ())
        if not targets:
            problems.append(f"expected scrape job {job!r} has no active targets")
            continue
        for target in targets:
            if target.get("health") == "up":
                continue
            labels = _mapping(target.get("labels"), field=f"target labels for job {job!r}")
            instance = labels.get("instance", "unknown instance")
            health = target.get("health", "unknown")
            last_error = target.get("lastError") or "no scrape error reported"
            problems.append(f"{job} target {instance} is {health}: {last_error}")

    removed_targets = targets_by_job.get(REMOVED_WORKER_JOB, ())
    if removed_targets:
        problems.append(
            f"removed scrape job {REMOVED_WORKER_JOB!r} still has "
            f"{len(removed_targets)} active target(s); reload or restart Prometheus"
        )
    return counts, problems


def fetch_active_targets(
    base_url: str,
    *,
    request_timeout: float = 10.0,
    opener: OpenerDirector | None = None,
) -> object:
    """Fetch active targets from the Prometheus HTTP API."""
    endpoint = f"{base_url.rstrip('/')}/api/v1/targets?state=active"
    request = Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "User-Agent": "django-ray-prometheus-target-check/1",
        },
    )
    open_request = urlopen if opener is None else opener.open
    with open_request(request, timeout=request_timeout) as response:
        return json.load(response)


def wait_for_healthy_targets(
    fetch: Callable[[], object],
    *,
    timeout: float,
    interval: float,
    expected_counts: Mapping[str, int] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Poll until every expected target is present and healthy."""
    deadline = clock() + timeout
    last_problems: list[str] = []
    while True:
        try:
            counts, last_problems = inspect_target_health(fetch())
            if not last_problems and expected_counts is not None:
                for job, expected in expected_counts.items():
                    observed = counts.get(job, 0)
                    if observed != expected:
                        last_problems.append(
                            f"scrape job {job!r} has {observed} active target(s), "
                            f"expected exactly {expected}"
                        )
        except (OSError, ValueError) as error:
            counts = {}
            last_problems = [str(error)]
        if not last_problems:
            return counts
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError("; ".join(last_problems))
        sleep(min(interval, remaining))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check bundled django-ray and Ray Prometheus target health"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:30090",
        help="Prometheus base URL (default: http://localhost:30090)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for healthy targets (default: 120)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Seconds between checks (default: 2)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Prometheus target-health acceptance check."""
    args = _parser().parse_args(argv)
    if args.timeout < 0 or args.interval <= 0:
        print("--timeout must be non-negative and --interval must be positive", file=sys.stderr)
        return 2
    try:
        counts = wait_for_healthy_targets(
            lambda: fetch_active_targets(args.url),
            timeout=args.timeout,
            interval=args.interval,
        )
    except RuntimeError as error:
        print(f"Prometheus target check failed: {error}", file=sys.stderr)
        return 1

    summary = ", ".join(f"{job}={counts[job]}" for job in EXPECTED_JOBS)
    print(f"Prometheus targets healthy: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
