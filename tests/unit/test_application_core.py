"""Resource-free evidence checks for the disposable application core layer."""

import base64
import hashlib
import json
from datetime import timedelta
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from django.utils import timezone

from django_ray.conf import settings as ray_settings
from django_ray.models import RayTaskExecution, TaskWorkerLease
from django_ray.runtime.runtime_env_encryption import (
    RuntimeEnvEncryptionError,
    protect_runtime_env_snapshot,
    validate_runtime_env_encryption_settings,
)
from qualification.application import run_core

TASK_ID = "25200000-0000-4000-8000-000000000002"
MARKER = "django-ray-runtime-env-encryption-canary-v1-7c4e2a91"


@pytest.fixture
def durable(monkeypatch):
    config = {
        "RAY_ADDRESS": "ray://ray-head:10001",
        "WORKER_LEASE_SECONDS": 60,
        "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "qualification",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {
            "qualification": base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
        },
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": False,
    }
    monkeypatch.setattr(ray_settings, "get_settings", lambda: config)
    monkeypatch.setenv("DJANGO_RAY_RECOVERY_WORKING_DIR", "/runtime/recovery.zip")
    now = timezone.now()
    package_version = version("django-ray")
    runtime = {
        "working_dir": "/runtime/recovery.zip",
        "env_vars": {"DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE": MARKER},
    }
    row = SimpleNamespace(
        pk=5,
        task_id=TASK_ID,
        state="SUCCEEDED",
        attempt_number=1,
        execution_generation=1,
        execution_protocol_version=1,
        runtime_env_profile="project",
        ray_target_address=config["RAY_ADDRESS"],
        ray_address=config["RAY_ADDRESS"],
        ray_job_id="ray_core:5",
        ray_job_request_reference=None,
        created_with_django_ray_version=package_version,
        managed_with_django_ray_version=package_version,
        executor_django_ray_version=package_version,
        created_at=now - timedelta(seconds=4),
        started_at=now - timedelta(seconds=3),
        finished_at=now - timedelta(seconds=1),
        claimed_by_worker="owned-worker",
        result_data="5",
        result_reference=None,
    )
    worker = SimpleNamespace(
        worker_id="owned-worker",
        is_active=True,
        hostname="django-manager-owned",
        django_ray_version=package_version,
        started_at=now - timedelta(seconds=5),
        last_heartbeat_at=now - timedelta(seconds=1),
    )
    attempt = SimpleNamespace(**vars(row))
    row.attempts = SimpleNamespace(all=lambda: [attempt])

    def seal():
        plaintext = json.dumps(runtime, sort_keys=True, separators=(",", ":"))
        row.runtime_env_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        row.runtime_env_json = protect_runtime_env_snapshot(
            plaintext,
            task_id=row.task_id,
            profile=row.runtime_env_profile,
            digest=row.runtime_env_hash,
            encryption=validate_runtime_env_encryption_settings(
                config, django_secret_key="unused", django_secret_key_fallbacks=[]
            ),
        )

    seal()
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **kwargs: row)
    monkeypatch.setattr(TaskWorkerLease.objects, "get", lambda **kwargs: worker)
    return SimpleNamespace(row=row, worker=worker, attempt=attempt, runtime=runtime, seal=seal)


def test_durable_evidence_authenticates_snapshot_and_manager(durable):
    result = run_core.verify_durable_task(
        TASK_ID, profile="project", manager_prefix="django-manager-"
    )
    assert result["encrypted_snapshot_authenticated"] is True
    assert result["worker_id"] == "owned-worker"
    assert result["elapsed_seconds"] == 3
    assert MARKER not in json.dumps(result)
    assert "ciphertext" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "FAILED"),
        ("attempt_number", 2),
        ("execution_generation", 0),
        ("execution_protocol_version", 2),
        ("runtime_env_profile", "different"),
        ("ray_target_address", "ray://other:10001"),
        ("ray_address", "auto"),
        ("ray_job_id", None),
        ("ray_job_request_reference", "rq2:another-transport"),
        ("executor_django_ray_version", "0.0.0"),
        ("finished_at", None),
    ],
)
def test_durable_evidence_rejects_wrong_execution(durable, field, value):
    setattr(durable.row, field, value)
    with pytest.raises((ValueError, RuntimeEnvEncryptionError)):
        run_core.verify_durable_task(TASK_ID, profile="project", manager_prefix="django-manager-")


@pytest.mark.parametrize(
    "failure",
    ["inactive", "foreign", "stale", "attempt", "tamper", "identity", "pip", "path", "marker"],
)
def test_durable_evidence_rejects_unproven_owner_or_environment(durable, failure):
    if failure == "inactive":
        durable.worker.is_active = False
    elif failure == "foreign":
        durable.worker.hostname = "other-manager"
    elif failure == "stale":
        durable.worker.last_heartbeat_at -= timedelta(seconds=61)
    elif failure == "attempt":
        durable.attempt.result_data = "incorrect"
    elif failure == "tamper":
        envelope = json.loads(durable.row.runtime_env_json)
        envelope["ciphertext"] = "A" * len(envelope["ciphertext"])
        durable.row.runtime_env_json = json.dumps(envelope)
    elif failure == "identity":
        durable.row.runtime_env_hash = "0" * 64
    else:
        if failure == "pip":
            durable.runtime["pip"] = ["unreviewed"]
        elif failure == "path":
            durable.runtime["working_dir"] = "/other/recovery.zip"
        else:
            durable.runtime["env_vars"]["DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE"] = "wrong"
        durable.seal()
    with pytest.raises((ValueError, RuntimeEnvEncryptionError)):
        run_core.verify_durable_task(TASK_ID, profile="project", manager_prefix="django-manager-")


def test_runtime_probe_uses_existing_bounded_api_route(durable, monkeypatch):
    durable.row.callable_path = "testproject.apps.cluster_tasks.tasks.runtime_env_probe"
    durable.row.result_data = json.dumps(
        {
            "profile_marker": "thin",
            "storage_encryption_verified": True,
            "package": "django-ray",
            "package_version": version("django-ray"),
        }
    )
    monkeypatch.setattr(
        run_core, "validate_task_status_payload", lambda payload, **kwargs: "SUCCEEDED"
    )
    requests = []

    def request(path, **kwargs):
        requests.append((path, kwargs))
        return 200, json.dumps({"task_id": TASK_ID}).encode()

    assert run_core.verify_runtime_probe(request, token="private", timeout=180) == TASK_ID
    assert requests[0][0] == "/api/cluster/runtime-env/probe?profile=thin&package=django-ray"
    assert requests[1][0] == f"/api/tasks/{TASK_ID}"
    assert requests[1][1]["response_limit"] == 64 * 1024
    assert requests[1][1]["required_response_headers"] == {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }


def test_core_failure_retains_safe_api_progress_without_private_response(
    tmp_path, monkeypatch, capsys
):
    import django

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(django, "setup", lambda: None)

    def private_failure(*args, **kwargs):
        args[0].requests = 11
        args[0].last_http_status = 200
        kwargs["evidence"].task_id = TASK_ID
        kwargs["evidence"].task_state = "FAILED"
        raise ValueError("private API response")

    monkeypatch.setattr(run_core, "verify_application_api", private_failure)
    receipt = tmp_path / "core.json"
    assert run_core.main(["--token-file", str(tmp_path / "secret"), "--receipt", str(receipt)]) == 1
    output = capsys.readouterr().out
    assert "private" not in output
    failed = json.loads(output)
    assert failed["failed_stage"] == "application_api"
    assert failed["api_diagnostics"]["requests"] == 11
    assert failed["api_diagnostics"]["last_http_status"] == 200
    assert failed["api_diagnostics"]["observations"]["task_id"] == TASK_ID
    assert failed["api_diagnostics"]["observations"]["task_state"] == "FAILED"
    assert not receipt.exists()
