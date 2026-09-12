"""Resource-free evidence checks for the disposable application core layer."""

import base64
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from django.utils import timezone

from django_ray.conf import settings as ray_settings
from django_ray.models import RayTaskCohortClaim, RayTaskExecution, TaskWorkerLease
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
        execution_protocol_version=3,
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
        pid=100,
        capability_schema_version=1,
        legacy_admission_token_id=None,
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
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
    from django_ray.execution_codec import ExecutionIdentity
    from django_ray.target import cohort_intent_storage
    from django_ray.target.cohort_claim import (
        CohortCapabilitySnapshot,
        CohortRunnerFamily,
        cohort_claim_facts_digest,
        cohort_task_runtime_env_snapshot_digest,
        encode_cohort_claim_facts,
    )
    from django_ray.target.cohort_intent import decode_cohort_intent
    from tests.unit.test_cohort_claim import facts

    retained = replace(
        facts(family=CohortRunnerFamily.RAY_CORE),
        identity=ExecutionIdentity(row.pk, row.task_id, 1, 1),
        binding_id=row.pk,
        worker_lease_id=worker.worker_id,
        worker_lease_hostname=worker.hostname,
        worker_lease_pid=worker.pid,
        worker_lease_started_at=worker.started_at,
        runtime_env_profile=row.runtime_env_profile,
        runtime_env_hash=row.runtime_env_hash,
        runtime_env_snapshot_digest=cohort_task_runtime_env_snapshot_digest(
            profile=row.runtime_env_profile,
            serialized=row.runtime_env_json,
            digest=row.runtime_env_hash,
        ),
        claimed_at=row.started_at,
        capability=CohortCapabilitySnapshot(1, 1, 1, worker.started_at + timedelta(seconds=1)),
    )
    row.ray_target_binding = SimpleNamespace(
        runner_family="ray_core", package_version=package_version, target_policy_id=1
    )
    claim = SimpleNamespace(
        facts_json=encode_cohort_claim_facts(retained),
        facts_digest=cohort_claim_facts_digest(retained),
        owner_lease_id=worker.worker_id,
        owner_lease_hostname=worker.hostname,
        owner_lease_pid=worker.pid,
        owner_lease_started_at=worker.started_at,
        target_policy_id=1,
        claim_attestation_id=1,
        disposition="RESOLVED",
        resolution_kind="application_completed",
        prepared_request_digest="sha256:" + "a" * 64,
        resolution_digest="sha256:" + "b" * 64,
        dispatched_at=row.started_at + timedelta(milliseconds=100),
        resolved_at=row.finished_at - timedelta(milliseconds=100),
    )
    monkeypatch.setattr(RayTaskCohortClaim.objects, "get", lambda **kwargs: claim)
    monkeypatch.setattr(
        cohort_intent_storage,
        "read_cohort_intent",
        lambda _: decode_cohort_intent(retained.intent_json),
    )
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **kwargs: row)
    monkeypatch.setattr(TaskWorkerLease.objects, "get", lambda **kwargs: worker)
    return SimpleNamespace(
        row=row, worker=worker, attempt=attempt, claim=claim, runtime=runtime, seal=seal
    )


def test_durable_evidence_authenticates_snapshot_and_manager(durable):
    result = run_core.verify_durable_task(
        TASK_ID, profile="project", manager_prefix="django-manager-"
    )
    assert result["encrypted_snapshot_authenticated"] is True
    assert result["current_cohort_claim_correlated"] is True
    assert result["worker_id"] == "owned-worker"
    assert result["elapsed_seconds"] == 3
    assert MARKER not in json.dumps(result)
    assert "ciphertext" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("facts_digest", "sha256:" + "0" * 64),
        ("owner_lease_id", "replacement"),
        ("owner_lease_hostname", "replacement-host"),
        ("owner_lease_pid", 200),
        ("target_policy_id", 2),
        ("claim_attestation_id", 2),
        ("disposition", "HELD"),
        ("resolution_kind", "verified_cancelled"),
        ("prepared_request_digest", None),
        ("resolution_digest", None),
        ("dispatched_at", None),
        ("resolved_at", None),
    ],
)
def test_durable_evidence_rejects_uncorrelated_or_unresolved_claim(durable, field, value):
    setattr(durable.claim, field, value)
    with pytest.raises(ValueError):
        run_core.verify_durable_task(TASK_ID, profile="project", manager_prefix="django-manager-")


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


@pytest.mark.parametrize("replacement", [False, True])
def test_core_receipt_file_and_collected_bytes_bind_same_retirement_input(
    tmp_path, monkeypatch, capsys, replacement
):
    import django

    from qualification.application import retire_manager

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(django, "setup", lambda: None)
    monkeypatch.setattr(
        run_core, "verify_application_api", lambda *_a, **_k: run_core.ApiEvidence(task_id=TASK_ID)
    )
    monkeypatch.setattr(run_core, "verify_runtime_probe", lambda *_a, **_k: TASK_ID)
    monkeypatch.setattr(run_core, "read_token", lambda *_a: "unused")
    monkeypatch.setattr(run_core, "verify_durable_task", lambda *_a, **_k: {"task_id": TASK_ID})
    seen = []

    def verify(path, executions):
        seen.append((path, executions))
        return {"original_history_preserved": True}

    monkeypatch.setattr(retire_manager, "verify_replacement", verify)
    output = tmp_path / "core.json"
    args = ["--token-file", str(tmp_path / "token"), "--receipt", str(output)]
    if replacement:
        args += ["--previous-retirement", str(tmp_path / "retired.json")]
    assert run_core.main(args) == 0
    assert output.read_bytes() == capsys.readouterr().out.strip().encode()
    assert bool(seen) is replacement
    assert json.loads(output.read_bytes())["complete_application_gate"] is False
