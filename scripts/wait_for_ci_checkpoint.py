"""Wait for the latest exact-source CI workflow attempt, not an obsolete check."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class CheckpointError(RuntimeError):
    """A fixed, secret-free qualification prerequisite failure."""


@dataclass(frozen=True)
class Checkpoint:
    run_id: int
    attempt: int


def latest_run(payload: dict[str, Any], sha: str) -> dict[str, Any] | None:
    if payload.get("total_count", 0) > 100:
        raise CheckpointError("CI run inventory exceeds the bounded checkpoint window")
    runs = [
        run
        for run in payload.get("workflow_runs", [])
        if run.get("head_sha") == sha
        and run.get("path") == ".github/workflows/ci.yml"
        and run.get("event") in {"pull_request", "push", "workflow_dispatch"}
    ]
    if not runs:
        return None
    return max(runs, key=lambda run: run["run_number"])


def inspect_checkpoint(
    api: Callable[[str], dict[str, Any]], repo: str, sha: str
) -> Checkpoint | None:
    endpoint = f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100"
    run = latest_run(api(endpoint), sha)
    if run is None or run.get("status") != "completed":
        return None
    if run.get("conclusion") == "cancelled":
        # A cancelled readiness/synchronization run may have a replacement that
        # is not visible yet. Never accept it; retain the finite wait deadline.
        return None
    if run.get("conclusion") != "success":
        raise CheckpointError("Current exact-source CI workflow did not pass")
    run_id, attempt = run["id"], run["run_attempt"]
    jobs = api(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    if jobs.get("total_count", 0) > 100:
        raise CheckpointError("CI job inventory exceeds the bounded checkpoint window")
    gates = [job for job in jobs.get("jobs", []) if job.get("name") == "CI Gate"]
    if len(gates) != 1 or gates[0].get("conclusion") != "success":
        raise CheckpointError("Current CI attempt has no unique passing CI Gate")
    # Detect a new workflow or rerun appearing while the gate was inspected.
    confirmed = latest_run(api(endpoint), sha)
    if confirmed is None or any(
        confirmed.get(key) != run.get(key) for key in ("id", "run_attempt", "status", "conclusion")
    ):
        return None
    return Checkpoint(run_id, attempt)


def wait_for_checkpoint(
    api: Callable[[str], dict[str, Any]],
    repo: str,
    sha: str,
    *,
    timeout: float = 1080,
    interval: float = 25,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Checkpoint:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise CheckpointError("Invalid repository identity")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CheckpointError("Invalid source identity")
    deadline = clock() + timeout
    while clock() < deadline:
        run_id = inspect_checkpoint(api, repo, sha)
        if run_id is not None and clock() < deadline:
            return run_id
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(interval, remaining))
    raise CheckpointError("No passing exact-source CI attempt within the checkpoint deadline")


def main() -> int:
    def api(endpoint: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["gh", "api", endpoint],
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            )
            return json.loads(result.stdout)
        except (subprocess.SubprocessError, OSError, ValueError):
            raise CheckpointError("Unable to read CI checkpoint metadata") from None

    try:
        run_id = wait_for_checkpoint(
            api, os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("SOURCE_SHA", "")
        )
    except CheckpointError as error:
        print(f"::error::{error}")
        return 1
    print(
        json.dumps(
            {
                "status": "passed",
                "source_sha": os.environ["SOURCE_SHA"],
                "ci_run_id": run_id.run_id,
                "ci_attempt": run_id.attempt,
                "workflow": ".github/workflows/ci.yml",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
