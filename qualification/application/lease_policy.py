"""Bounded negative startup proof against the live common lease authority."""

from __future__ import annotations

import io
import json
import os
import signal
import time
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

FACTS = (
    "authority_matched",
    "healthy_control",
    "startup_refused",
    "no_runtime_or_claim",
    "no_new_lease",
    "readiness_unavailable",
    "status_mismatch",
    "capacity_unavailable",
    "peers_preserved",
    "policy_preserved",
    "control_removed",
)
MAX_LEASES = 32
OBSERVATION_SECONDS = 20
CONTROL_QUEUE = "qualification-lease-policy-control"


def empty_evidence() -> dict:
    return dict.fromkeys(FACTS, False)


def validate_evidence(value: object) -> None:
    if (
        type(value) is not dict
        or set(value) != set(FACTS)
        or any(value[name] is not True for name in FACTS)
    ):
        raise ValueError("Lease policy qualification evidence is incomplete")


class BoundedOutput(io.StringIO):
    """Retain only bounded command output privately; never include it in evidence."""

    def write(self, value):
        if type(value) is not str or len(value.encode()) + len(self.getvalue().encode()) > 4096:
            raise ValueError("Lease policy command output exceeded its bound")
        return super().write(value)


@contextmanager
def deadline():
    """Own one finite Linux alarm, restoring prior handlers/timers after cleanup."""
    if not hasattr(signal, "setitimer"):
        raise ValueError("Lease policy qualification requires Linux")
    started = time.monotonic()
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def timeout(*_args):
        raise TimeoutError("Lease policy qualification deadline expired")

    signal.signal(signal.SIGALRM, timeout)
    duration = min(OBSERVATION_SECONDS, old_timer[0]) if old_timer[0] else OBSERVATION_SECONDS
    signal.setitimer(signal.ITIMER_REAL, duration)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, old_timer[0] - (time.monotonic() - started)),
                old_timer[1],
            )


def snapshot() -> dict:
    """Bound before materialization; retain private identities only in this process."""
    from django.db.models import Q
    from django.db.models.functions import Length

    from django_ray.models import TaskWorkerLease

    rows = TaskWorkerLease.objects.alias(
        _worker_length=Length("worker_id"),
        _host_length=Length("hostname"),
        _queue_length=Length("queue_name"),
    )
    if rows.exclude(
        Q(_worker_length__lte=255) & Q(_host_length__lte=255) & Q(_queue_length__lte=100)
    ).exists():
        raise ValueError("Lease policy fixture encountered invalid bounded metadata")
    values = list(
        rows.order_by("worker_id").values(
            "worker_id",
            "hostname",
            "pid",
            "queue_name",
            "started_at",
            "last_heartbeat_at",
            "is_active",
            "stopped_at",
        )[: MAX_LEASES + 1]
    )
    if len(values) > MAX_LEASES:
        raise ValueError("Lease policy fixture exceeded its lease bound")
    return {row["worker_id"]: row for row in values}


@contextmanager
def database_deadlines():
    """Restore the owned observer session's finite PostgreSQL query limits."""
    from django.db import connection

    if connection.vendor != "postgresql" or not connection.get_autocommit():
        raise ValueError("Lease policy qualification requires an owned PostgreSQL observer")
    saved = {}
    try:
        with connection.cursor() as cursor:
            for name, limit in (("statement_timeout", "1000ms"), ("lock_timeout", "250ms")):
                cursor.execute(f"SHOW {name}")
                value = cursor.fetchone()[0]
                if type(value) is not str or len(value) > 32:
                    raise ValueError("Lease policy observer settings are unavailable")
                saved[name] = value
                cursor.execute("SELECT set_config(%s, %s, false)", [name, limit])
        yield
    finally:
        with connection.cursor() as cursor:
            for name, value in saved.items():
                cursor.execute("SELECT set_config(%s, %s, false)", [name, value])


def preserved(before: dict, after: dict) -> bool:
    """Healthy peers may heartbeat normally; their incarnation cannot be retired."""
    if set(before) != set(after):
        return False
    for identity, first in before.items():
        second = after[identity]
        if (
            any(first[name] != second[name] for name in first if name != "last_heartbeat_at")
            or second["last_heartbeat_at"] < first["last_heartbeat_at"]
        ):
            return False
    return True


def _remove_owned(command) -> None:
    from django_ray.lease_policy import lease_policy_guard
    from django_ray.models import TaskWorkerLease
    from django_ray.runner.leasing import release_lease

    if command.lease_identity is None:
        return
    identity = command.lease_identity
    release_lease(identity)
    with lease_policy_guard():
        owned = TaskWorkerLease.objects.filter(**identity.database_filters(), is_active=False)
        _total, deleted = owned.delete()
        if deleted.get(TaskWorkerLease._meta.label) != 1:
            raise ValueError("Lease policy control cleanup was not exact")


def verify_lease_policy(*, task_id: str, manager_prefix: str, evidence: dict) -> None:
    """Exercise real startup; later runtime entry points are fail-fast barriers."""
    if (
        os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
        or type(manager_prefix) is not str
        or not 1 <= len(manager_prefix) <= 63
        or type(task_id) is not str
        or not 1 <= len(task_id) <= 64
        or evidence != empty_evidence()
    ):
        raise ValueError("Lease policy fixture requires owned qualification settings")
    from django.conf import settings
    from django.core.management import CommandError, call_command
    from django.test import override_settings
    from django.utils import timezone

    from django_ray.lease_policy import LeasePolicy, read_lease_policy
    from django_ray.management.commands.django_ray_worker import Command
    from django_ray.models import RayTaskExecution, TaskWorkerLeasePolicy
    from django_ray.protocol_status import annotate_execution_protocol_availability
    from django_ray.worker_readiness import check_worker_lease_readiness

    original_handlers = {kind: signal.getsignal(kind) for kind in (signal.SIGINT, signal.SIGTERM)}
    control = Command(stdout=BoundedOutput(), stderr=BoundedOutput())
    candidate = Command(stdout=BoundedOutput(), stderr=BoundedOutput())
    with deadline(), database_deadlines():
        before = snapshot()
        observed = timezone.now()
        if not any(
            row["is_active"]
            and row["stopped_at"] is None
            and row["hostname"].startswith(manager_prefix)
            and observed - timedelta(seconds=60) <= row["last_heartbeat_at"] <= observed
            for row in before.values()
        ):
            raise ValueError("Lease policy fixture requires an actual healthy manager")
        authority = list(TaskWorkerLeasePolicy.objects.values())
        if read_lease_policy() != LeasePolicy(60, 1):
            raise ValueError("Lease policy fixture requires the unchanged default authority")
        evidence["authority_matched"] = True
        try:
            control._create_lease(CONTROL_QUEUE)
            identity = control.lease_identity
            if identity is None:
                raise ValueError("Lease policy control was not acquired")
            control_ready = check_worker_lease_readiness(
                queue=CONTROL_QUEUE, hostname=identity.hostname, worker_id=identity.worker_id
            )
            if control_ready.status != "ready":
                raise ValueError("Lease policy control was not healthy")
            evidence["healthy_control"] = True
            with override_settings(
                DJANGO_RAY={
                    **settings.DJANGO_RAY,
                    "WORKER_LEASE_SECONDS": 6,
                    "WORKER_HEARTBEAT_SECONDS": 2,
                }
            ):
                with (
                    patch.object(
                        Command,
                        "_initialize_ray_execution",
                        side_effect=AssertionError(
                            "Lease policy boundary reached runtime initialization"
                        ),
                    ) as initialize,
                    patch.object(
                        Command,
                        "run_loop",
                        side_effect=AssertionError("Lease policy boundary reached claiming"),
                    ) as claim,
                    patch.object(
                        Command,
                        "shutdown",
                        side_effect=AssertionError(
                            "Lease policy boundary reached runtime shutdown"
                        ),
                    ) as shutdown,
                ):
                    try:
                        call_command(
                            candidate,
                            "--queue",
                            "default",
                            "--cluster",
                            settings.DJANGO_RAY["RAY_ADDRESS"],
                            verbosity=0,
                        )
                    except CommandError as error:
                        if str(error) != "lease_policy_mismatch":
                            raise ValueError(
                                "Lease policy startup returned an unexpected refusal"
                            ) from None
                    else:
                        raise ValueError("Lease policy startup did not refuse")
                    evidence["startup_refused"] = True
                    evidence["no_runtime_or_claim"] = not (
                        initialize.called or claim.called or shutdown.called
                    )
                evidence["no_new_lease"] = candidate.lease_identity is None
                ready = check_worker_lease_readiness(
                    queue=CONTROL_QUEUE, hostname=identity.hostname, worker_id=identity.worker_id
                )
                evidence["readiness_unavailable"] = (
                    ready.status == "error" and ready.reason == "lease_policy_mismatch"
                )
                output = BoundedOutput()
                try:
                    call_command("django_ray_lease_policy", stdout=output, stderr=BoundedOutput())
                except CommandError as error:
                    if str(error) != "lease_policy_mismatch" or error.returncode != 2:
                        raise ValueError(
                            "Lease policy status returned an unexpected refusal"
                        ) from None
                else:
                    raise ValueError("Lease policy status did not refuse")
                evidence["status_mismatch"] = json.loads(output.getvalue()) == {
                    "schema_version": 1,
                    "scope": "worker_lease_policy",
                    "status": "mismatch",
                    "configured_seconds": 6,
                    "lease_seconds": 60,
                    "revision": 1,
                }
                capacity = list(
                    annotate_execution_protocol_availability(
                        RayTaskExecution.objects.filter(task_id=task_id)
                    ).values_list("protocol_compatible_worker_available", flat=True)[:2]
                )
                evidence["capacity_unavailable"] = capacity == [None]
        finally:
            for kind, handler in original_handlers.items():
                signal.signal(kind, handler)
            _remove_owned(candidate)
            _remove_owned(control)
            evidence["control_removed"] = True
        evidence["peers_preserved"] = preserved(before, snapshot())
        evidence["policy_preserved"] = list(TaskWorkerLeasePolicy.objects.values()) == authority
        validate_evidence(evidence)
