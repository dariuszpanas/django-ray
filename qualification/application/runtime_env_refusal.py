"""Observe two fixed malformed RuntimeEnv rows through the owned application."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from uuid import uuid4

CASES = ("tampered_ciphertext", "unavailable_key")
FACTS = ("manager_refused", "no_submission_identity", "retry_conflict", "snapshot_preserved")
CALLABLE = "testproject.apps.cluster_tasks.tasks.runtime_env_probe"
ERRORS = (
    "Stored RuntimeEnv authentication failed.",
    "Stored RuntimeEnv decryption key is unavailable.",
)
OBSERVATION_SECONDS = 60
MAX_SNAPSHOT_BYTES = 128 * 1024


@dataclass(frozen=True)
class Fixture:
    """In-process fixture identity; never serialize this into a receipt."""

    case: str
    pk: int
    task_id: str
    serialized: str = field(repr=False)
    digest: str = field(repr=False)


def empty_evidence() -> dict:
    return {case: dict.fromkeys(FACTS, False) for case in CASES}


def validate_evidence(value: object) -> None:
    """Missing, partial, additional or non-boolean assertions cannot pass."""
    if not isinstance(value, dict) or set(value) != set(CASES):
        raise ValueError("RuntimeEnv refusal evidence has the wrong cases")
    for case in CASES:
        facts = value[case]
        if (
            not isinstance(facts, dict)
            or set(facts) != set(FACTS)
            or any(facts[name] is not True for name in FACTS)
        ):
            raise ValueError("RuntimeEnv refusal evidence is incomplete")


def create_fixtures() -> tuple[Fixture, ...]:
    """Commit both corruptions before the real manager can claim either row."""
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
        raise ValueError("RuntimeEnv fixture creation requires qualification settings")
    from importlib.metadata import version

    from django.db import transaction

    from django_ray.models import RayTaskExecution, TaskState
    from django_ray.runtime.runtime_env import resolve_runtime_env_profile, runtime_env_for_storage

    fixtures = []
    with transaction.atomic():
        resolved = resolve_runtime_env_profile("thin")
        for case in CASES:
            task_id = str(uuid4())
            stored = runtime_env_for_storage(resolved, task_id=task_id)
            envelope = json.loads(stored.serialized)
            if envelope.get("key_id") != "qualification":
                raise ValueError("RuntimeEnv fixture did not use the qualification key")
            if case == "tampered_ciphertext":
                ciphertext = envelope["ciphertext"]
                envelope["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
            else:
                envelope["key_id"] = "qualification-unavailable"
            serialized = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
            if len(serialized.encode()) > 64 * 1024:
                raise ValueError("RuntimeEnv fixture exceeded its envelope budget")
            row = RayTaskExecution.objects.create(
                task_id=task_id,
                callable_path=CALLABLE,
                queue_name="default",
                priority=100,
                state=TaskState.QUEUED,
                created_with_django_ray_version=version("django-ray"),
                runtime_env_profile=stored.profile,
                runtime_env_json=serialized,
                runtime_env_hash=stored.digest,
                timeout_seconds=30,
            )
            fixtures.append(Fixture(case, row.pk, task_id, serialized, stored.digest))
    return tuple(fixtures)


def inspect_failure(
    fixture: Fixture, *, manager_prefix: str, historical_owner: bool = False
) -> bytes | None:
    """Return an internal full-row snapshot only after exact refusal invariants."""
    from datetime import timedelta
    from importlib.metadata import version

    from django.core.serializers.json import DjangoJSONEncoder
    from django.utils import timezone

    from django_ray.conf.settings import get_settings
    from django_ray.models import RayTaskExecution, TaskWorkerLease

    row = RayTaskExecution.objects.get(pk=fixture.pk, task_id=fixture.task_id)
    if row.state in {"QUEUED", "RUNNING"}:
        return None
    if (
        row.state != "FAILED"
        or row.callable_path != CALLABLE
        or row.attempt_number != 1
        or row.execution_generation != 1
        or row.execution_protocol_version != 1
        or row.runtime_env_profile != "thin"
        or row.runtime_env_json != fixture.serialized
        or row.runtime_env_hash != fixture.digest
        or row.error_message != expected_error(fixture)
        or not row.started_at
        or not row.finished_at
        or not row.created_at <= row.started_at <= row.finished_at
        or any(
            getattr(row, name) is not None
            for name in (
                "ray_job_id",
                "ray_address",
                "ray_job_request_reference",
                "executor_django_ray_version",
                "completion_data",
                "result_data",
                "result_reference",
                "progress_data",
                "workflow_progress_summary_json",
                "error_traceback",
            )
        )
    ):
        raise ValueError("RuntimeEnv refusal crossed the pre-submission boundary")
    worker = TaskWorkerLease.objects.get(worker_id=row.claimed_by_worker)
    now = timezone.now()
    current_version = version("django-ray")
    if (
        not worker.hostname.startswith(manager_prefix)
        or worker.django_ray_version != current_version
        or row.managed_with_django_ray_version != current_version
        or not worker.started_at <= row.started_at <= row.finished_at <= now
        or (
            not historical_owner
            and (
                not worker.is_active
                or not now - timedelta(seconds=get_settings()["WORKER_LEASE_SECONDS"])
                <= worker.last_heartbeat_at
                <= now
            )
        )
    ):
        raise ValueError("RuntimeEnv refusal has no current live manager owner")
    attempts = list(row.attempts.order_by("attempt_number", "pk")[:2])
    if len(attempts) != 1 or any(
        getattr(attempts[0], name) != getattr(row, name)
        for name in (
            "attempt_number",
            "state",
            "execution_protocol_version",
            "managed_with_django_ray_version",
            "executor_django_ray_version",
            "started_at",
            "finished_at",
            "error_message",
            "error_traceback",
            "result_data",
            "result_reference",
            "workflow_progress_summary_json",
        )
    ):
        raise ValueError("RuntimeEnv refusal did not preserve its single failed attempt")

    def concrete(instance):
        return {item.attname: item.value_from_object(instance) for item in instance._meta.fields}

    snapshot = json.dumps(
        {"execution": concrete(row), "attempt": concrete(attempts[0])},
        cls=DjangoJSONEncoder,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if len(snapshot) > MAX_SNAPSHOT_BYTES:
        raise ValueError("RuntimeEnv refusal snapshot exceeded its internal budget")
    return snapshot


def expected_error(fixture: Fixture) -> str:
    if fixture.case == "required_plaintext":
        return "django-ray: Persisted RuntimeEnv plaintext is forbidden by required encryption"
    return (
        f"django-ray: Persisted RuntimeEnv snapshot for task {fixture.pk}: "
        + ERRORS[CASES.index(fixture.case)]
    )


def verify_refusals(request, *, token: str, manager_prefix: str, evidence: dict) -> list[int]:
    """Use one finite observation budget; the generation owns the hard deadline."""
    if (
        os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
        or not manager_prefix
        or len(manager_prefix) > 63
        or evidence != empty_evidence()
    ):
        raise ValueError("RuntimeEnv refusal requires the disposable qualification fixture")
    deadline = time.monotonic() + OBSERVATION_SECONDS
    fixtures = create_fixtures()
    snapshots = {}
    for fixture in fixtures:
        while True:
            remaining = deadline - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                raise ValueError("RuntimeEnv refusal observation deadline expired")
            snapshot = inspect_failure(fixture, manager_prefix=manager_prefix)
            if snapshot is not None:
                break
            time.sleep(min(1, remaining))
        facts = evidence[fixture.case]
        facts.update(manager_refused=True, no_submission_identity=True)
        status, body = request(
            f"/api/executions/{fixture.pk}/retry",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
            response_limit=64 * 1024,
        )
        if status != 409 or json.loads(body) != {
            "detail": "Persisted RuntimeEnv snapshot failed validation"
        }:
            raise ValueError("RuntimeEnv refusal retry did not return its fixed conflict")
        facts["retry_conflict"] = True
        if inspect_failure(fixture, manager_prefix=manager_prefix) != snapshot:
            raise ValueError("RuntimeEnv refusal retry changed execution or attempt history")
        facts["snapshot_preserved"] = True
        snapshots[fixture.case] = snapshot
    if time.monotonic() > deadline:
        raise ValueError("RuntimeEnv refusal observation deadline expired")
    for fixture in fixtures:
        if inspect_failure(fixture, manager_prefix=manager_prefix) != snapshots[fixture.case]:
            raise ValueError("RuntimeEnv refusal history changed during observation")
    if time.monotonic() > deadline:
        raise ValueError("RuntimeEnv refusal observation deadline expired")
    validate_evidence(evidence)
    return [fixture.pk for fixture in fixtures]
