"""Assert authenticated application execution and its durable manager ownership."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from qualification.application.api import (
    TASK_FAILURE_STATES,
    ApiEvidence,
    validate_task_status_payload,
    verify_application_api,
)
from qualification.application.run_api import ApplicationHttp, read_token


def verify_durable_task(task_id: str, *, profile: str, manager_prefix: str) -> dict:
    """Inspect only this disposable application DB, with no executor authority."""
    from datetime import timedelta
    from importlib.metadata import version

    from django.conf import settings
    from django.utils import timezone

    from django_ray.conf.settings import get_settings
    from django_ray.models import RayTaskExecution, TaskWorkerLease
    from django_ray.runtime.runtime_env_encryption import (
        unprotect_runtime_env_snapshot,
        validate_runtime_env_encryption_settings,
    )

    row = RayTaskExecution.objects.get(task_id=task_id)
    config = get_settings()
    now = timezone.now()
    current_version = version("django-ray")
    if (
        row.state != "SUCCEEDED"
        or row.attempt_number != 1
        or row.execution_generation < 1
        or row.execution_protocol_version != 1
        or row.runtime_env_profile != profile
        or row.ray_target_address != config["RAY_ADDRESS"]
        or not row.ray_job_id
        or row.ray_address != config["RAY_ADDRESS"]
        or row.ray_job_request_reference is not None
        or row.created_with_django_ray_version != current_version
        or row.managed_with_django_ray_version != current_version
        or row.executor_django_ray_version != current_version
        or not row.started_at
        or not row.finished_at
        or not row.created_at <= row.started_at <= row.finished_at <= now
    ):
        raise ValueError("Durable application execution does not match the core contract")
    worker = TaskWorkerLease.objects.get(worker_id=row.claimed_by_worker)
    if (
        not worker.is_active
        or not worker.hostname.startswith(manager_prefix)
        or worker.django_ray_version != current_version
        or worker.started_at > row.started_at
        or not now - timedelta(seconds=config["WORKER_LEASE_SECONDS"])
        <= worker.last_heartbeat_at
        <= now
    ):
        raise ValueError("Durable application execution lacks a current manager owner")
    attempts = list(row.attempts.all())
    if len(attempts) != 1:
        raise ValueError("Core execution has unexpected attempt history")
    attempt = attempts[0]
    for field in (
        "attempt_number",
        "state",
        "execution_protocol_version",
        "managed_with_django_ray_version",
        "executor_django_ray_version",
        "started_at",
        "finished_at",
        "result_data",
        "result_reference",
    ):
        if getattr(attempt, field) != getattr(row, field):
            raise ValueError("Durable attempt differs from its execution")

    marker = "django-ray-runtime-env-encryption-canary-v1-7c4e2a91"
    if (
        not isinstance(row.runtime_env_json, str)
        or len(row.runtime_env_json.encode()) > 64 * 1024
        or marker in row.runtime_env_json
    ):
        raise ValueError("RuntimeEnv storage is unbounded or exposes the plaintext canary")
    envelope = json.loads(row.runtime_env_json)
    if envelope.get("key_id") != "qualification":
        raise ValueError("RuntimeEnv storage does not use the qualification key")
    plaintext = unprotect_runtime_env_snapshot(
        row.runtime_env_json,
        task_id=row.task_id,
        profile=row.runtime_env_profile,
        digest=row.runtime_env_hash,
        encryption=validate_runtime_env_encryption_settings(
            config, django_secret_key=settings.SECRET_KEY, django_secret_key_fallbacks=[]
        ),
    )
    runtime = json.loads(plaintext)
    if (
        "pip" in runtime
        or "PYTHONPATH" in runtime.get("env_vars", {})
        or runtime.get("env_vars", {}).get("DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE") != marker
        or runtime.get("working_dir") != os.environ["DJANGO_RAY_RECOVERY_WORKING_DIR"]
    ):
        raise ValueError("Durable RuntimeEnv does not match the offline qualification profile")
    return {
        "task_id": row.task_id,
        "execution_id": row.pk,
        "attempt_number": row.attempt_number,
        "execution_generation": row.execution_generation,
        "worker_id": worker.worker_id,
        "worker_hostname": worker.hostname,
        "django_ray_version": current_version,
        "encrypted_snapshot_authenticated": True,
        "elapsed_seconds": round((row.finished_at - row.created_at).total_seconds(), 3),
    }


def verify_runtime_probe(request: ApplicationHttp, *, token: str, timeout: float) -> str:
    """The existing sample task must observe the decrypted canary on Ray."""
    from importlib.metadata import version

    from django_ray.models import RayTaskExecution

    headers = {"Authorization": f"Bearer {token}"}
    status, body = request(
        "/api/cluster/runtime-env/probe?profile=thin&package=django-ray",
        method="POST",
        headers=headers,
    )
    if status != 200:
        raise ValueError("RuntimeEnv probe enqueue failed")
    identity = json.loads(body).get("task_id")
    if (
        not isinstance(identity, str)
        or str(UUID(identity)) != identity
        or UUID(identity).version != 4
    ):
        raise ValueError("RuntimeEnv probe returned no canonical task identity")
    deadline = time.monotonic() + timeout
    while True:
        status, body = request(
            f"/api/tasks/{identity}",
            method="GET",
            headers=headers,
            response_limit=64 * 1024,
            required_response_headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
        if status != 200:
            raise ValueError("RuntimeEnv probe polling failed")
        state = validate_task_status_payload(json.loads(body), task_id=identity)
        if state == "SUCCEEDED":
            break
        if state in TASK_FAILURE_STATES or time.monotonic() >= deadline:
            raise ValueError("RuntimeEnv probe did not succeed within its deadline")
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    row = RayTaskExecution.objects.get(task_id=identity)
    if row.callable_path != "testproject.apps.cluster_tasks.tasks.runtime_env_probe":
        raise ValueError("RuntimeEnv probe identity belongs to a different callable")
    if not isinstance(row.result_data, str) or len(row.result_data.encode()) > 4096:
        raise ValueError("RuntimeEnv probe result is unbounded or absent")
    result = json.loads(row.result_data)
    if (
        result.get("profile_marker") != "thin"
        or result.get("storage_encryption_verified") is not True
        or result.get("package") != "django-ray"
        or result.get("package_version") != version("django-ray")
    ):
        raise ValueError("Remote RuntimeEnv did not prove the decrypted canary and package")
    return identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://django-web:8000")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--manager-prefix", default="django-manager-")
    args = parser.parse_args(argv)
    receipt = {
        "schema_version": 1,
        "layer": "application_core",
        "status": "failed",
        "complete_application_gate": False,
        "failed_stage": "configuration",
    }
    passed = False
    request = None
    api_evidence = ApiEvidence()
    try:
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
            raise ValueError("Core assertions require qualification settings")
        if not args.manager_prefix or len(args.manager_prefix) > 63:
            raise ValueError("A bounded manager hostname prefix is required")
        import django

        django.setup()
        request = ApplicationHttp(args.base_url)
        receipt["failed_stage"] = "application_api"
        api = verify_application_api(
            request,
            get_token=lambda: read_token(args.token_file),
            task_timeout=180,
            evidence=api_evidence,
        )
        receipt["failed_stage"] = "api_execution_ownership"
        api_owner = verify_durable_task(
            api.task_id, profile="project", manager_prefix=args.manager_prefix
        )
        receipt["failed_stage"] = "remote_runtime_env"
        probe_id = verify_runtime_probe(request, token=read_token(args.token_file), timeout=180)
        receipt["failed_stage"] = "probe_execution_ownership"
        probe_owner = verify_durable_task(
            probe_id, profile="thin", manager_prefix=args.manager_prefix
        )
        receipt.update(api=asdict(api), executions=[api_owner, probe_owner])
        receipt["failed_stage"] = "receipt"
        encoded = json.dumps(
            {**receipt, "status": "passed", "failed_stage": None}, sort_keys=True
        ).encode()
        if len(encoded) > 16 * 1024:
            raise ValueError("Application core receipt exceeds its byte limit")
        with args.receipt.open("xb") as stream:
            stream.write(encoded)
        receipt.update(status="passed", failed_stage=None)
        passed = True
    except Exception:
        # No raw HTTP/DB/Ray responses, RuntimeEnv plaintext or credentials.
        receipt.pop("api", None)
        receipt.pop("executions", None)
        if request is not None:
            receipt["api_diagnostics"] = {
                "requests": request.requests,
                "last_http_status": request.last_http_status,
                "observations": asdict(api_evidence),
            }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
