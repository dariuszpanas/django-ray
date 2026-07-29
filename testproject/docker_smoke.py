"""Bounded end-to-end smoke for the tracked Docker Compose quickstart."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

_MAX_RESPONSE_BYTES = 1_048_576
_REQUEST_TIMEOUT_SECONDS = 5.0


class DockerSmokeError(RuntimeError):
    """Report a bounded quickstart contract failure without exposing credentials."""


def _response_json(response: Any) -> dict[str, Any]:
    body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise DockerSmokeError("HTTP response exceeded the smoke byte limit")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DockerSmokeError("HTTP response was not valid JSON") from error
    if not isinstance(payload, dict):
        raise DockerSmokeError("HTTP response must be a JSON object")
    return payload


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers=headers,
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        response = error
    except urllib.error.URLError as error:
        raise DockerSmokeError(f"request to {path} failed") from error

    with response:
        if response.status != expected_status:
            raise DockerSmokeError(
                f"request to {path} returned HTTP {response.status}; "
                f"expected HTTP {expected_status}"
            )
        return _response_json(response)


def _wait_until(
    description: str,
    deadline: float,
    probe: Callable[[], bool],
    *,
    interval_seconds: float = 0.5,
) -> None:
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(interval_seconds)
    raise DockerSmokeError(f"timed out waiting for {description}")


def _verify_database_contract() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    if connection.vendor != "postgresql":
        raise DockerSmokeError("quickstart smoke requires the shared PostgreSQL database")
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    if executor.migration_plan(targets):
        raise DockerSmokeError("the one-shot migration service left unapplied migrations")


def _run_smoke(*, base_url: str, token: str, timeout_seconds: float) -> dict[str, Any]:
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings")
    django.setup()

    from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease

    _verify_database_contract()
    deadline = time.monotonic() + timeout_seconds

    def web_is_ready() -> bool:
        try:
            payload = _request_json(base_url, "/api/readyz")
        except DockerSmokeError:
            return False
        return payload.get("status") == "healthy" and payload.get("database") == "ok"

    _wait_until("the web service database readiness check", deadline, web_is_ready)
    _wait_until(
        "an active default-queue worker lease in PostgreSQL",
        deadline,
        lambda: TaskWorkerLease.objects.filter(
            is_active=True,
            queue_name__contains="default",
        ).exists(),
    )

    _request_json(
        base_url,
        "/api/enqueue/add/20/22",
        method="POST",
        expected_status=401,
    )
    _request_json(
        base_url,
        "/api/enqueue/add/20/22",
        method="POST",
        token="invalid-django-ray-smoke-token",
        expected_status=401,
    )
    enqueued = _request_json(
        base_url,
        "/api/enqueue/add/20/22",
        method="POST",
        token=token,
    )
    task_id = enqueued.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise DockerSmokeError("authenticated enqueue returned no task ID")

    terminal_status: str | None = None

    def task_succeeded() -> bool:
        nonlocal terminal_status
        payload = _request_json(base_url, f"/api/tasks/{task_id}", token=token)
        status = payload.get("status")
        if not isinstance(status, str):
            raise DockerSmokeError("task result endpoint returned no status")
        terminal_status = status
        if status in {"FAILED", "CANCELLED"}:
            raise DockerSmokeError(f"worker execution reached terminal status {status}")
        return status == "SUCCESSFUL"

    _wait_until("worker execution and API result refresh", deadline, task_succeeded)

    execution_payload = _request_json(
        base_url,
        f"/api/executions?task_id={task_id}&limit=1",
        token=token,
    )
    tasks = execution_payload.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1:
        raise DockerSmokeError("execution API did not return exactly the enqueued task")
    api_execution = tasks[0]
    if not isinstance(api_execution, dict):
        raise DockerSmokeError("execution API returned an invalid task record")
    try:
        api_result = json.loads(api_execution["result_data"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise DockerSmokeError("execution API returned an invalid result payload") from error
    if api_execution.get("state") != TaskState.SUCCEEDED or api_result != 42:
        raise DockerSmokeError("execution API did not return the expected successful result")

    database_execution = RayTaskExecution.objects.get(task_id=task_id)
    try:
        database_result = json.loads(database_execution.result_data or "null")
    except json.JSONDecodeError as error:
        raise DockerSmokeError("PostgreSQL stored an invalid result payload") from error
    if database_execution.state != TaskState.SUCCEEDED or database_result != 42:
        raise DockerSmokeError("worker did not persist the expected result in shared PostgreSQL")

    return {
        "database": "postgresql",
        "migrations": "applied",
        "authentication": "fail-closed-and-valid-token-accepted",
        "worker_lease": "observed",
        "task_id": task_id,
        "task_status": terminal_status,
        "result": database_result,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://web:8000")
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.timeout <= 0:
        raise DockerSmokeError("--timeout must be positive")
    token = os.environ.get("DJANGO_API_TOKEN")
    if not token:
        raise DockerSmokeError("DJANGO_API_TOKEN must be set")
    result = _run_smoke(
        base_url=args.base_url,
        token=token,
        timeout_seconds=args.timeout,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
