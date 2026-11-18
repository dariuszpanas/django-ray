"""Assert authenticated application execution and its durable manager ownership."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from qualification.application.api import (
    TASK_FAILURE_STATES,
    ApiEvidence,
    validate_state_api_selection,
    validate_task_status_payload,
    verify_application_api,
)
from qualification.application.http_context import HttpObserver
from qualification.application.run_api import ApplicationHttp, ApplicationHttpError, read_token


class CoreEvidenceFailure(StrEnum):
    """Closed diagnostic vocabulary; never include observed values."""

    EXECUTION_CONTRACT = "execution_contract"
    EXECUTION_TIMING = "execution_timing"
    OWNER_IDENTITY = "owner_identity"
    OWNER_HEARTBEAT = "owner_heartbeat"
    ATTEMPT_COUNT = "attempt_count"
    ATTEMPT_MISMATCH = "attempt_mismatch"
    RUNTIME_ENVELOPE = "runtime_envelope"
    RUNTIME_KEY = "runtime_key"
    RUNTIME_PROFILE = "runtime_profile"
    STATE_API_SELECTION = "state_api_selection"


class CoreEvidenceError(ValueError):
    """A failed fixed assertion whose code is safe in a diagnostic receipt."""

    def __init__(self, code: CoreEvidenceFailure) -> None:
        self.code = CoreEvidenceFailure(code)
        super().__init__(self.code.value)


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
        not row.started_at
        or not row.finished_at
        or not row.created_at <= row.started_at <= row.finished_at <= now
    ):
        raise CoreEvidenceError(CoreEvidenceFailure.EXECUTION_TIMING)
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
    ):
        raise CoreEvidenceError(CoreEvidenceFailure.EXECUTION_CONTRACT)
    worker = TaskWorkerLease.objects.get(worker_id=row.claimed_by_worker)
    # A heartbeat may commit during the lease read. Compare the returned row
    # against a clock captured after that observation, not before the query.
    owner_observed_at = timezone.now()
    if (
        not worker.is_active
        or not worker.hostname.startswith(manager_prefix)
        or worker.django_ray_version != current_version
        or worker.started_at > row.started_at
    ):
        raise CoreEvidenceError(CoreEvidenceFailure.OWNER_IDENTITY)
    if not (
        owner_observed_at - timedelta(seconds=config["WORKER_LEASE_SECONDS"])
        <= worker.last_heartbeat_at
        <= owner_observed_at
    ):
        raise CoreEvidenceError(CoreEvidenceFailure.OWNER_HEARTBEAT)
    attempts = list(row.attempts.all())
    if len(attempts) != 1:
        raise CoreEvidenceError(CoreEvidenceFailure.ATTEMPT_COUNT)
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
            raise CoreEvidenceError(CoreEvidenceFailure.ATTEMPT_MISMATCH)

    marker = "django-ray-runtime-env-encryption-canary-v1-7c4e2a91"
    if (
        not isinstance(row.runtime_env_json, str)
        or len(row.runtime_env_json.encode()) > 64 * 1024
        or marker in row.runtime_env_json
    ):
        raise CoreEvidenceError(CoreEvidenceFailure.RUNTIME_ENVELOPE)
    envelope = json.loads(row.runtime_env_json)
    if envelope.get("key_id") != "qualification":
        raise CoreEvidenceError(CoreEvidenceFailure.RUNTIME_KEY)
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
        raise CoreEvidenceError(CoreEvidenceFailure.RUNTIME_PROFILE)
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


def verify_state_api_selection(task_id: str, *, execution_id: int) -> dict:
    """Exercise selected State endpoints over the existing owned native HTTP route."""
    from django_ray.conf.settings import get_settings
    from django_ray.models import RayTaskExecution
    from django_ray.observability import get_ray_task_state
    from qualification.application.http_context import local_outer_deadline

    try:
        config = get_settings()
        endpoint = config.get("RAY_STATE_API_ADDRESS")
        timeout = config.get("RAY_STATE_API_TIMEOUT_SECONDS", 5)
        deadline = local_outer_deadline()
        row = RayTaskExecution.objects.get(task_id=task_id)
        if (
            endpoint != "http://ray-head:8265"
            or type(timeout) is not int
            or timeout != 5
            or deadline is None
            or type(execution_id) is not int
            or execution_id <= 0
            or row.pk != execution_id
            or row.task_id != task_id
            or row.state != "SUCCEEDED"
            or row.attempt_number != 1
            or row.execution_protocol_version != 1
            or type(row.ray_job_id) is not str
        ):
            raise ValueError("State API selection requires its owned completed API task")
        match = re.fullmatch(r"([0-9a-f]{8}):([0-9a-f]{48})", row.ray_job_id)
        if match is None:
            raise ValueError("State API selection requires the owned composite Ray identity")
        job_id, ray_task_id = match.groups()
        keys = ("RAY_API_SERVER_ADDRESS", "RAY_ADDRESS")
        original = {key: os.environ[key] for key in keys if key in os.environ}
        facts = {}
        try:
            for fact, explicit, key in (
                ("explicit_api_server_address", True, keys[0]),
                ("explicit_ray_address", True, keys[1]),
                ("configured_api_server_address", False, keys[0]),
                ("configured_ray_address", False, keys[1]),
            ):
                for name in keys:
                    os.environ.pop(name, None)
                os.environ[key] = endpoint + "/__django_ray_qualification_unselected_state_api__"
                if deadline - time.monotonic() < timeout:
                    raise ValueError("State API selection lacks original generation time")
                values = (
                    get_ray_task_state(ray_task_id, address=endpoint)
                    if explicit
                    else get_ray_task_state(ray_task_id)
                )
                if time.monotonic() >= deadline:
                    raise ValueError("State API selection exhausted original generation time")
                if type(values) is not list or len(values) != 1 or type(values[0]) is not dict:
                    raise ValueError("State API selection requires exactly one owned Ray attempt")
                value = values[0]
                if (
                    value.get("task_id") != ray_task_id
                    or value.get("job_id") != job_id
                    or value.get("state") != "FINISHED"
                    or type(value.get("attempt_number")) is not int
                    or value["attempt_number"] != 0
                ):
                    raise ValueError("State API selection returned another Ray identity or outcome")
                facts[fact] = True
        finally:
            for key in keys:
                os.environ.pop(key, None)
            for key, value in original.items():
                os.environ[key] = value
        evidence = {"schema_version": 1, "execution_id": execution_id, "query_count": 4, **facts}
        validate_state_api_selection(evidence, execution_id=execution_id)
        return evidence
    except Exception:
        raise CoreEvidenceError(CoreEvidenceFailure.STATE_API_SELECTION) from None


def verify_runtime_probe(request: ApplicationHttp, *, token: str, timeout: float) -> str:
    """The existing sample task must observe the decrypted canary on Ray."""
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
    observe_runtime_probe(
        request, task_id=identity, token=token, deadline=time.monotonic() + timeout
    )
    return identity


def observe_runtime_probe(request, *, task_id: str, token: str, deadline: float) -> None:
    """Observe an already captured probe without enqueueing or retrying work."""
    from importlib.metadata import version

    from django_ray.models import RayTaskExecution

    identity = task_id
    headers = {"Authorization": f"Bearer {token}"}
    while True:
        if time.monotonic() >= deadline:
            raise ValueError("RuntimeEnv probe did not succeed within its deadline")
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
        if time.monotonic() >= deadline:
            raise ValueError("RuntimeEnv probe did not succeed within its deadline")
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


def maintenance_refusal_ids(current: list[int], receipt_path: Path) -> list[int]:
    """The second generation accepts only the first receipt's exact negative IDs."""
    if receipt_path.name != "after-core.json":
        return current
    from qualification.application.protected_runtime import validate
    from qualification.application.runtime_env_maintenance import validate_evidence

    with receipt_path.with_name("before-core.json").open("rb") as stream:
        raw = stream.read(16 * 1024 + 1)
    if len(raw) > 16 * 1024:
        raise ValueError("Prior RuntimeEnv fixture receipt exceeds its bound")
    previous = json.loads(raw)
    if (
        previous.get("layer") != "application_core"
        or previous.get("status") != "passed"
        or type(previous.get("schema_version")) is not int
        or previous["schema_version"] != 1
        or previous.get("complete_application_gate") is not False
        or previous.get("failed_stage") is not None
    ):
        raise ValueError("Prior RuntimeEnv fixture receipt did not pass")
    validate(previous.get("protected_runtime"))
    validate_evidence(previous.get("runtime_env_maintenance"))
    return [*previous["protected_runtime"]["row_ids"][1:], *current]


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
        request = ApplicationHttp(args.base_url, observer=HttpObserver.CORE)
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
        from qualification.application.protected_runtime import Capture

        protected = Capture(request)
        probe_id = verify_runtime_probe(protected, token=read_token(args.token_file), timeout=180)
        receipt["failed_stage"] = "probe_execution_ownership"
        probe_owner = verify_durable_task(
            probe_id, profile="thin", manager_prefix=args.manager_prefix
        )
        receipt.update(api=asdict(api), executions=[api_owner, probe_owner])
        receipt["failed_stage"] = "dashboard_proxy"
        from qualification.application.dashboard_browser import observe_dashboard

        receipt["dashboard"] = observe_dashboard(request, api.task_id)
        receipt["failed_stage"] = "state_api_selection"
        receipt["state_api_selection"] = verify_state_api_selection(
            api.task_id, execution_id=api_owner["execution_id"]
        )
        receipt["failed_stage"] = "lease_policy"
        from qualification.application.lease_policy import empty_evidence as empty_lease_evidence
        from qualification.application.lease_policy import verify_lease_policy

        receipt["lease_policy"] = empty_lease_evidence()
        verify_lease_policy(
            task_id=api.task_id,
            manager_prefix=args.manager_prefix,
            evidence=receipt["lease_policy"],
        )
        receipt["failed_stage"] = "runtime_env_refusal"
        from qualification.application.runtime_env_refusal import empty_evidence, verify_refusals

        receipt["runtime_env_refusals"] = empty_evidence()
        refusal_ids = verify_refusals(
            protected,
            token=read_token(args.token_file),
            manager_prefix=args.manager_prefix,
            evidence=receipt["runtime_env_refusals"],
        )
        receipt["failed_stage"] = "runtime_env_maintenance"
        from qualification.application.runtime_env_maintenance import (
            empty_evidence as empty_maintenance_evidence,
        )
        from qualification.application.runtime_env_maintenance import verify_maintenance

        receipt["runtime_env_maintenance"] = empty_maintenance_evidence()
        verify_maintenance(
            protected,
            token=read_token(args.token_file),
            manager_prefix=args.manager_prefix,
            refusal_ids=maintenance_refusal_ids(refusal_ids, args.receipt),
            evidence=receipt["runtime_env_maintenance"],
        )
        receipt["failed_stage"] = "protected_runtime_surfaces"
        receipt["protected_runtime"] = protected.finish([probe_owner["execution_id"], *refusal_ids])
        receipt["failed_stage"] = "ordinary_result_presentation"
        from qualification.application.result_presentation import observe_result_presentation

        receipt["ordinary_results"] = observe_result_presentation(token=read_token(args.token_file))
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
    except Exception as exc:
        # No raw HTTP/DB/Ray responses, RuntimeEnv plaintext or credentials.
        if isinstance(exc, ApplicationHttpError):
            receipt["http_failure"] = exc.diagnostic()
        if receipt.get("failed_stage") == "dashboard_proxy":
            from qualification.application.workflow_browser import BrowserObservationError

            if isinstance(exc, BrowserObservationError) and exc.line is not None:
                receipt["browser_failure_line"] = exc.line
            trace = exc.__traceback__
            while trace is not None:
                module = trace.tb_frame.f_globals.get("__name__", "")
                if module in {
                    "qualification.application.dashboard_browser",
                    "qualification.application.dashboard_proxy",
                    "qualification.application.workflow_session",
                }:
                    receipt["dashboard_failure_location"] = {
                        "module": module,
                        "line": trace.tb_lineno,
                    }
                trace = trace.tb_next
        receipt["failure_code"] = (
            exc.code.value
            if isinstance(exc, CoreEvidenceError) and isinstance(exc.code, CoreEvidenceFailure)
            else "unclassified"
        )
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
