"""Retire the exact successful smoke manager before replacing its Ray session.

Only the worker may attest owned cleanup. This helper requests retirement and
reads its result; the enclosing Chainsaw/runner separately proves Job reaping.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

MAX_RECEIPT_BYTES = 16 * 1024


def read_receipt(path: Path, *, layer: str) -> tuple[dict, str]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    value = json.loads(raw)
    if (
        not raw
        or len(raw) > MAX_RECEIPT_BYTES
        or type(value) is not dict
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("layer") != layer
        or value.get("status") != "passed"
        or value.get("complete_application_gate") is not False
    ):
        raise ValueError("Required application receipt is unavailable")
    return value, hashlib.sha256(raw).hexdigest()


def task_ids(receipt: dict) -> tuple[str, str]:
    executions = receipt.get("executions")
    if type(executions) is not list or len(executions) != 2:
        raise ValueError("Retirement requires the exact two successful smoke tasks")
    ids = tuple(item["task_id"] for item in executions)
    if len(set(ids)) != 2 or any(
        type(value) is not str or str(UUID(value)) != value for value in ids
    ):
        raise ValueError("Smoke task identities are malformed")
    return ids[0], ids[1]


def history_digest(ids) -> str:
    """Hash bounded original task/attempt/claim/intent bytes without emitting them."""
    from django.core.serializers.json import DjangoJSONEncoder

    from django_ray.models import RayTaskExecution

    histories = []
    for task_id in ids:
        row = RayTaskExecution.objects.get(task_id=task_id)
        if row.state != "SUCCEEDED" or row.execution_protocol_version != 3:
            raise ValueError("Original smoke execution changed")
        histories.append(
            {
                "execution": RayTaskExecution.objects.filter(pk=row.pk).values().get(),
                "attempts": list(row.attempts.order_by("attempt_number").values()),
                "binding": row.ray_target_binding.__class__.objects.filter(pk=row.pk)
                .values()
                .get(),
                "claims": list(row.ray_target_binding.cohort_claims.order_by("pk").values()),
                "intent": row.cohort_intent.__class__.objects.filter(pk=row.pk).values().get(),
            }
        )
    encoded = json.dumps(histories, cls=DjangoJSONEncoder, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > 512 * 1024:
        raise ValueError("Smoke history exceeds its qualification bound")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def core_session(task_id: str) -> str:
    from django_ray.models import RayTaskCohortClaim, RayTaskExecution
    from django_ray.target.attestation import decode_ray_cluster_attestation
    from django_ray.target.cohort_claim import decode_cohort_claim_facts

    row = RayTaskExecution.objects.get(task_id=task_id)
    claim = RayTaskCohortClaim.objects.select_related("claim_attestation").get(
        binding_id=row.pk,
        attempt_number=row.attempt_number,
        execution_generation=row.execution_generation,
    )
    facts = decode_cohort_claim_facts(claim.facts_json, expected_digest=claim.facts_digest)
    proof = decode_ray_cluster_attestation(claim.claim_attestation.attestation_json)
    if proof.attestation_digest != facts.claim_attestation_digest:
        raise ValueError("Original session proof differs from its claim")
    return proof.expectation.cluster_session


def _identity(values):
    from django_ray.runner.leasing import WorkerLeaseIdentity
    from django_ray.target.capabilities import _identity as validate

    if type(values) is not dict or set(values) != {"worker_id", "hostname", "pid", "started_at"}:
        raise ValueError("Manager identity is malformed")
    return validate(
        WorkerLeaseIdentity(
            **{**values, "started_at": datetime.fromisoformat(values["started_at"])}
        )
    )


def _retired(identity):
    from django_ray.models import (
        RayTaskCohortClaim,
        RayTaskExecution,
        RayWorkerRetirement,
        RayWorkerTargetCapability,
        TaskWorkerLease,
    )
    from django_ray.target.cohort_contract import _digest
    from django_ray.target.cohort_job_cleanup import owned_job_cleanup_pending

    lease = TaskWorkerLease.objects.get(**identity.database_filters())
    row = (
        RayWorkerRetirement.objects.filter(**identity.database_filters())
        .order_by("-revision")
        .first()
    )
    if row is None or row.state != "RETIRED":
        return None
    if (
        row.revision != 2
        or row.actor != "django-ray-worker"
        or row.reason != "owned-cleanup-confirmed"
        or lease.is_active
        or lease.stopped_at is None
        or row.cleanup_confirmed_at is None
        or not identity.started_at <= row.cleanup_confirmed_at <= row.created_at == lease.stopped_at
        or row.created_at > datetime.now(UTC)
    ):
        raise ValueError("Manager lacks an exact worker cleanup confirmation")
    _digest(row.cleanup_evidence_digest)
    # These are corroborating blockers. Only the worker's separate owned-cleanup
    # confirmation above supplies remote/callback cleanup evidence.
    if (
        owned_job_cleanup_pending(identity)
        or RayTaskExecution.objects.filter(
            claimed_by_worker=identity.worker_id, state__in=("RUNNING", "CANCELLING")
        ).exists()
        or RayTaskCohortClaim.objects.filter(
            owner_lease_id=identity.worker_id,
            owner_lease_hostname=identity.hostname,
            owner_lease_pid=identity.pid,
            owner_lease_started_at=identity.started_at,
        )
        .exclude(disposition="RESOLVED")
        .exists()
        or RayWorkerTargetCapability.objects.filter(lease_id=identity.worker_id).exists()
    ):
        raise ValueError("Retired manager retains owned work or capacity")
    return row


def verify_retired_receipt(receipt: dict) -> None:
    identity = _identity(receipt["manager"])
    row = _retired(identity)
    if (
        row is None
        or row.cleanup_evidence_digest != receipt["cleanup_evidence_digest"]
        or row.cleanup_confirmed_at.isoformat() != receipt["cleanup_confirmed_at"]
        or history_digest(receipt["task_ids"]) != receipt["history_digest"]
        or any(
            core_session(task_id) != receipt["cluster_session"] for task_id in receipt["task_ids"]
        )
    ):
        raise ValueError("Retired manager or original smoke history changed")


def verify_replacement(path: Path, executions: list[dict]) -> dict:
    """Require preserved originals and a later, distinct exact manager/session."""
    from django_ray.models import TaskWorkerLease

    previous, digest = read_receipt(path, layer="application_retirement")
    verify_retired_receipt(previous)
    ids = task_ids({"executions": executions})
    if set(ids) & set(previous["task_ids"]):
        raise ValueError("Cold smoke reused an original task")
    if len({item["worker_id"] for item in executions}) != 1:
        raise ValueError("Cold smoke did not use one manager")
    lease = TaskWorkerLease.objects.get(worker_id=executions[0]["worker_id"])
    original = _identity(previous["manager"])
    if (
        not lease.is_active
        or lease.worker_id == original.worker_id
        or lease.hostname == original.hostname
        or lease.started_at <= datetime.fromisoformat(previous["cleanup_confirmed_at"])
        or lease.started_at > datetime.now(UTC)
    ):
        raise ValueError("Cold smoke lacks a new manager incarnation")
    sessions = {core_session(task_id) for task_id in ids}
    if len(sessions) != 1 or previous["cluster_session"] in sessions:
        raise ValueError("Cold smoke did not qualify a new Ray session")
    return {
        "previous_retirement_sha256": digest,
        "original_history_preserved": True,
        "manager": {
            "worker_id": lease.worker_id,
            "hostname": lease.hostname,
            "pid": lease.pid,
            "started_at": lease.started_at.isoformat(),
        },
        "cluster_session": sessions.pop(),
    }


def retire(before: Path, *, timeout: float = 180) -> dict:
    from django_ray.maintenance import request_worker_retirement
    from django_ray.models import TaskWorkerLease
    from django_ray.runner.leasing import WorkerLeaseIdentity
    from qualification.application.run_core import verify_durable_task

    if type(timeout) not in {int, float} or not 0 < timeout <= 180:
        raise ValueError("Retirement deadline is invalid")
    receipt, source_digest = read_receipt(before, layer="application_core")
    ids = task_ids(receipt)
    owners = [
        verify_durable_task(task_id, profile=profile, manager_prefix="django-manager-")
        for task_id, profile in zip(ids, ("project", "thin"), strict=True)
    ]
    if owners[0]["worker_id"] != owners[1]["worker_id"]:
        raise ValueError("Smoke work did not use one manager incarnation")
    lease = TaskWorkerLease.objects.get(worker_id=owners[0]["worker_id"])
    identity = WorkerLeaseIdentity(lease.worker_id, lease.hostname, lease.pid, lease.started_at)
    history = history_digest(ids)
    sessions = {core_session(task_id) for task_id in ids}
    if len(sessions) != 1:
        raise ValueError("Smoke work did not use one verified session")
    changed = request_worker_retirement(
        identity,
        expected_revision=0,
        actor="application-qualification",
        reason="before-cold-session",
        authorized=True,
    )
    if not changed.changed or changed.revision != 1 or changed.state != "REQUESTED":
        raise ValueError("Manager retirement request was not newly accepted")
    deadline = time.monotonic() + timeout
    while True:
        row = _retired(identity)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Manager did not confirm owned cleanup before its deadline")
        if row is not None:
            break
        time.sleep(min(0.2, remaining))
    result = {
        "schema_version": 1,
        "layer": "application_retirement",
        "status": "passed",
        "complete_application_gate": False,
        "before_core_sha256": source_digest,
        "manager": {
            "worker_id": identity.worker_id,
            "hostname": identity.hostname,
            "pid": identity.pid,
            "started_at": identity.started_at.isoformat(),
        },
        "task_ids": ids,
        "history_digest": history,
        "cluster_session": sessions.pop(),
        "cleanup_evidence_digest": row.cleanup_evidence_digest,
        "cleanup_confirmed_at": row.cleanup_confirmed_at.isoformat(),
    }
    verify_retired_receipt(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-core", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    result = {
        "schema_version": 1,
        "layer": "application_retirement",
        "status": "failed",
        "complete_application_gate": False,
    }
    try:
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
            raise ValueError("Explicit qualification settings required")
        import django

        django.setup()
        result = retire(args.before_core)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_RECEIPT_BYTES:
            raise ValueError("Retirement receipt is too large")
        with args.receipt.open("xb") as stream:
            stream.write(encoded)
    except Exception:
        result = {
            "schema_version": 1,
            "layer": "application_retirement",
            "status": "failed",
            "complete_application_gate": False,
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
