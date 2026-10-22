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

TRANSIENT_HTTP = frozenset({408, 429, 502, 503, 504})
MAX_METADATA_RETRIES = 3
# The supported suite can consume 20 minutes before package verification starts.
# Reserve another 15 minutes for package work and scheduling; this remains a
# finite allowance, not a bound on all CI jobs. Callers keep their separate
# workload budgets after this prerequisite; no workload is retried.
CHECKPOINT_SECONDS = 35 * 60
DEADLINE_ERROR = "No passing exact-source CI attempt within the checkpoint deadline"


class CheckpointError(RuntimeError):
    """A fixed, secret-free qualification prerequisite failure."""


class MetadataReadError(CheckpointError):
    """An allowlisted transport category, never captured command output."""

    def __init__(self, category: str, status: int | None = None):
        if category not in {
            "timeout",
            "http_transient",
            "http_permission",
            "http_rejected",
            "command",
            "malformed_response",
            "malformed_contract",
        } or (status is not None and status not in TRANSIENT_HTTP):
            raise ValueError("Invalid metadata failure category")
        self.category = category
        self.status = status
        self.retryable = category in {"timeout", "http_transient"}
        super().__init__(f"Unable to read CI checkpoint metadata ({category})")


def read_metadata(endpoint: str, *, timeout: float) -> dict[str, Any]:
    """Classify the real HTTP status without rendering headers, bodies or stderr."""
    if timeout <= 0:
        raise CheckpointError(DEADLINE_ERROR)
    try:
        result = subprocess.run(
            ["gh", "api", "--include", "--method", "GET", endpoint],
            capture_output=True,
            check=False,
            timeout=min(20, timeout),
        )
    except subprocess.TimeoutExpired:
        raise MetadataReadError("timeout") from None
    except (subprocess.SubprocessError, OSError):
        raise MetadataReadError("command") from None

    raw = result.stdout
    boundary = re.search(rb"\r?\n\r?\n", raw[:65540])
    if boundary is None:
        raise MetadataReadError("command" if result.returncode else "malformed_response")
    header, body = raw[: boundary.start()], raw[boundary.end() :]
    first = header.splitlines()[0] if header else b""
    match = re.fullmatch(rb"HTTP/[123](?:\.[01])? ([1-5][0-9]{2})(?: [^\r\n]*)?", first)
    if len(header) > 65536 or len(first) > 256 or match is None:
        raise MetadataReadError("command" if result.returncode else "malformed_response")
    status = int(match[1])
    if status in TRANSIENT_HTTP:
        raise MetadataReadError("http_transient", status)
    if status in {401, 403}:
        raise MetadataReadError("http_permission")
    if status != 200:
        raise MetadataReadError("http_rejected")
    if result.returncode:
        raise MetadataReadError("command")
    try:
        value = json.loads(body)
    except ValueError:
        raise MetadataReadError("malformed_response") from None
    if not isinstance(value, dict):
        raise MetadataReadError("malformed_contract")
    return value


def inventory(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
    count, rows = payload.get("total_count"), payload.get(field)
    if type(count) is not int or count < 0 or not isinstance(rows, list):
        raise MetadataReadError("malformed_contract")
    if count > 100:
        raise CheckpointError("CI inventory exceeds the bounded checkpoint window")
    if len(rows) != count or any(not isinstance(row, dict) for row in rows):
        raise MetadataReadError("malformed_contract")
    return rows


@dataclass(frozen=True)
class Checkpoint:
    run_id: int
    attempt: int


@dataclass
class Observation:
    """Fixed diagnostic facts, never arbitrary workflow metadata or command output."""

    phase: str = "discovery"
    workflow_status: str = "unavailable"
    workflow_conclusion: str = "unavailable"

    def workflow(self, run: dict[str, Any] | None) -> None:
        self.workflow_status = "unavailable"
        self.workflow_conclusion = "unavailable"
        if run is not None:
            status, conclusion = run.get("status"), run.get("conclusion")
            self.workflow_status = (
                status
                if status
                in {"queued", "requested", "waiting", "pending", "in_progress", "completed"}
                else "unknown"
            )
            self.workflow_conclusion = (
                "pending"
                if conclusion is None
                else conclusion
                if conclusion
                in {"success", "failure", "cancelled", "timed_out", "skipped", "neutral", "stale"}
                else "unknown"
            )


def latest_run(payload: dict[str, Any], sha: str) -> dict[str, Any] | None:
    rows = inventory(payload, "workflow_runs")
    for row in rows:
        if (
            any(
                type(row.get(key)) is not int or row[key] <= 0
                for key in ("id", "run_number", "run_attempt")
            )
            or any(
                not isinstance(row.get(key), str) or not row[key]
                for key in ("head_sha", "path", "event", "status")
            )
            or not (row.get("conclusion") is None or isinstance(row["conclusion"], str))
        ):
            raise MetadataReadError("malformed_contract")
    runs = [
        run
        for run in rows
        if run.get("head_sha") == sha
        and run.get("path") == ".github/workflows/ci.yml"
        and run.get("event") in {"pull_request", "push", "workflow_dispatch"}
    ]
    if not runs:
        return None
    return max(runs, key=lambda run: run["run_number"])


def inspect_checkpoint(
    api: Callable[[str], dict[str, Any]],
    repo: str,
    sha: str,
    *,
    observation: Observation | None = None,
) -> Checkpoint | None:
    if observation is None:
        observation = Observation()
    observation.phase = "discovery"
    observation.workflow(None)
    endpoint = f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100"
    run = latest_run(api(endpoint), sha)
    observation.workflow(run)
    if run is None or run.get("status") != "completed":
        return None
    if run.get("conclusion") == "cancelled":
        # A cancelled readiness/synchronization run may have a replacement that
        # is not visible yet. Never accept it; retain the finite wait deadline.
        return None
    if run.get("conclusion") != "success":
        raise CheckpointError("Current exact-source CI workflow did not pass")
    run_id, attempt = run["id"], run["run_attempt"]
    observation.phase = "attempt_jobs"
    jobs = api(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    rows = inventory(jobs, "jobs")
    if any(
        not isinstance(job.get("name"), str)
        or not (job.get("conclusion") is None or isinstance(job["conclusion"], str))
        for job in rows
    ):
        raise MetadataReadError("malformed_contract")
    gates = [job for job in rows if job.get("name") == "CI Gate"]
    if len(gates) != 1 or gates[0].get("conclusion") != "success":
        raise CheckpointError("Current CI attempt has no unique passing CI Gate")
    # Detect a new workflow or rerun appearing while the gate was inspected.
    observation.phase = "confirmation"
    confirmed = latest_run(api(endpoint), sha)
    observation.workflow(confirmed)
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
    timeout: float = CHECKPOINT_SECONDS,
    interval: float = 25,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    deadline: float | None = None,
) -> Checkpoint:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise CheckpointError("Invalid repository identity")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CheckpointError("Invalid source identity")
    started = clock()
    if deadline is None:
        deadline = started + timeout
    retries = 0
    observation = Observation()

    def expired() -> CheckpointError:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "checkpoint": "ci_metadata",
                    "reason": "deadline",
                    "elapsed_seconds": round(max(0, clock() - started), 3),
                    "deadline_seconds": round(max(0, deadline - started), 3),
                    "last_phase": observation.phase,
                    "workflow_status": observation.workflow_status,
                    "workflow_conclusion": observation.workflow_conclusion,
                    "metadata_retries": retries,
                },
                sort_keys=True,
            )
        )
        return CheckpointError(DEADLINE_ERROR)

    while clock() < deadline:
        try:
            run_id = inspect_checkpoint(api, repo, sha, observation=observation)
        except MetadataReadError as error:
            if not error.retryable:
                raise
            if clock() >= deadline:
                raise expired() from None
            if retries == MAX_METADATA_RETRIES:
                raise CheckpointError(
                    f"CI metadata retry limit exhausted ({error.category})"
                ) from None
            retries += 1
            print(
                json.dumps(
                    {
                        "status": "retrying",
                        "checkpoint": "ci_metadata",
                        "category": error.category,
                        "http_status": error.status,
                        "retry": retries,
                        "retry_limit": MAX_METADATA_RETRIES,
                    },
                    sort_keys=True,
                )
            )
            run_id = None
        except CheckpointError as error:
            if str(error) == DEADLINE_ERROR:
                raise expired() from None
            raise
        if run_id is not None and clock() < deadline:
            return run_id
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(interval, remaining))
    raise expired()


def main() -> int:
    deadline = time.monotonic() + CHECKPOINT_SECONDS

    def api(endpoint: str) -> dict[str, Any]:
        return read_metadata(endpoint, timeout=deadline - time.monotonic())

    try:
        run_id = wait_for_checkpoint(
            api,
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("SOURCE_SHA", ""),
            deadline=deadline,
            clock=time.monotonic,
            sleep=time.sleep,
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
