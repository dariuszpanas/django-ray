"""Exact owned database fixture for current-cohort unsupported work evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

QUEUE = "default"
CALLABLE = "testproject.qualification_protocol_tasks.poison"
NONTERMINAL = ("QUEUED", "RUNNING", "CANCELLING")


def fingerprint(row, *, state=None):
    """Keep every concrete field except the independently checked primary key."""
    from django.core.serializers.json import DjangoJSONEncoder

    value = {
        f.attname: f.value_from_object(row) for f in row._meta.concrete_fields if not f.primary_key
    }
    if state is not None:
        value["state"] = state
    raw = json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"))
    if len(raw.encode()) > 64 * 1024:
        raise ValueError("Owned protocol snapshot exceeded its bound")
    return hashlib.sha256(raw.encode()).hexdigest()


def policy_state():
    from django_ray.models import LegacyWorkerAdmissionToken, TaskExecutionProtocolPolicy

    policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
    tokens = LegacyWorkerAdmissionToken.objects.count()
    if (
        policy.schema_version != 1
        or policy.active_write_protocol_version != 1
        or type(policy.revision) is not int
        or not 1 <= policy.revision <= (1 << 63) - 3
        or tokens != int(policy.legacy_worker_admission_enabled)
    ):
        raise ValueError("Owned protocol policy is inconsistent")
    return policy


@dataclass
class Fixture:
    """Keep private identity before the first write, including ambiguous inserts."""

    generation: str
    task_id: str = field(default_factory=lambda: str(uuid4()))
    pk: int | None = None
    revision: int | None = None
    staged_digest: str | None = field(default=None, repr=False)
    queued_digest: str | None = field(default=None, repr=False)
    manager_id: str | None = field(default=None, repr=False)
    queued_at: object = field(default=None, repr=False)

    def seed(self, checkpoint):
        from django.utils import timezone

        from django_ray import __version__
        from django_ray.models import RayTaskExecution, TaskWorkerLease
        from django_ray.protocol_coordination import close_legacy_worker_admission

        if self.generation not in {"before", "after"} or self.revision is not None:
            raise ValueError("Invalid owned protocol fixture")
        checkpoint()
        policy = policy_state()
        # This fixture deliberately requires the ordinary open baseline. It
        # must never reopen a baseline which was already closed by another owner.
        if not policy.legacy_worker_admission_enabled:
            raise ValueError("Owned protocol fixture requires open admission")
        if TaskWorkerLease.objects.filter(capability_schema_version=0).exists():
            # Closing admission also detaches inactive legacy token holders.
            # This current-only fixture must not mutate their baseline state.
            raise ValueError("A legacy manager lease remains")
        if (
            RayTaskExecution.objects.filter(state__in=NONTERMINAL)
            .exclude(execution_protocol_version=1)
            .exists()
        ):
            raise ValueError("Another unsupported nonterminal row exists")
        managers = list(
            TaskWorkerLease.objects.filter(
                queue_name=QUEUE,
                is_active=True,
                capability_schema_version=1,
                django_ray_version=__version__,
                min_supported_execution_protocol_version=1,
                max_supported_execution_protocol_version=1,
                last_heartbeat_at__gte=timezone.now() - timedelta(seconds=60),
                hostname__startswith="django-manager-",
            ).order_by("worker_id")[:2]
        )
        if len(managers) != 1:
            raise ValueError("Expected exactly one current compatible queue manager")
        self.manager_id = managers[0].worker_id
        self.revision = int(policy.revision)
        row = RayTaskExecution(
            task_id=self.task_id,
            callable_path=CALLABLE,
            queue_name=QUEUE,
            execution_protocol_version=2,
            state="FAILED",
            created_with_django_ray_version=__version__,
            args_json=json.dumps([self.generation, self.task_id], separators=(",", ":")),
            kwargs_json="{}",
        )
        self.staged_digest = fingerprint(row)
        self.queued_digest = fingerprint(row, state="QUEUED")
        checkpoint()
        row.save(force_insert=True)
        self.pk = row.pk
        checkpoint()
        transition = close_legacy_worker_admission(
            expected_revision=self.revision,
            legacy_producers_retired=True,
        )
        if transition.enabled or not transition.changed or transition.revision != self.revision + 1:
            raise ValueError("Owned admission did not close exactly once")
        checkpoint()
        if (
            RayTaskExecution.objects.filter(
                pk=self.pk,
                task_id=self.task_id,
                state="FAILED",
                execution_protocol_version=2,
                callable_path=CALLABLE,
                claimed_by_worker__isnull=True,
            ).update(state="QUEUED")
            != 1
        ):
            raise ValueError("Owned protocol row did not queue exactly once")
        self.queued_at = timezone.now()
        self.observe()

    def observe(self):
        from django.utils import timezone

        from django_ray import __version__
        from django_ray.models import RayTaskExecution, TaskWorkerLease

        if self.revision is None or self.queued_at is None:
            raise ValueError("Owned protocol fixture has not queued")
        row = RayTaskExecution.objects.get(pk=self.pk, task_id=self.task_id)
        if fingerprint(row) != self.queued_digest or row.attempts.exists():
            raise ValueError("Unsupported queued execution or history changed")
        policy = policy_state()
        if policy.legacy_worker_admission_enabled or policy.revision != self.revision + 1:
            raise ValueError("Owned admission changed during observation")
        manager = TaskWorkerLease.objects.get(worker_id=self.manager_id)
        now = timezone.now()
        if (
            not manager.is_active
            or manager.queue_name != QUEUE
            or manager.capability_schema_version != 1
            or manager.django_ray_version != __version__
            or manager.min_supported_execution_protocol_version != 1
            or manager.max_supported_execution_protocol_version != 1
            or not now - timedelta(seconds=60) <= manager.last_heartbeat_at <= now
        ):
            raise ValueError("Current queue manager is no longer live and compatible")
        return manager.last_heartbeat_at > self.queued_at

    def status(self):
        from django_ray.protocol_status import build_protocol_status, protocol_status_to_dict

        value = protocol_status_to_dict(build_protocol_status())
        expected = [
            {"count": 1, "execution_protocol_version": 2, "queue": QUEUE, "state": "QUEUED"}
        ]
        if (
            value["policy"]["active_write_protocol_version"] != 1
            or value["policy"]["legacy_worker_admission_enabled"] is not False
            or value["policy"]["legacy_admission_token_present"] is not False
            or value["non_v1_nonterminal_count"] != 1
            or value["unsupported_work"]["total_tasks"] != 1
            or value["unsupported_work"]["omitted_tasks"] != 0
            or value["unsupported_work"]["groups"] != expected
        ):
            raise ValueError("Protocol status did not expose the exact unsupported row")

    def cleanup(self, checkpoint):
        """Terminalize exact fixture before restoring admission; never reset revision."""
        from django.db import transaction

        from django_ray.models import RayTaskExecution
        from django_ray.protocol_coordination import reopen_legacy_worker_admission

        if self.revision is None:
            return
        checkpoint()
        policy = policy_state()
        if (policy.legacy_worker_admission_enabled, policy.revision) not in {
            (True, self.revision),
            (False, self.revision + 1),
            (True, self.revision + 2),
        }:
            raise ValueError("Foreign policy revision prevents fixture cleanup")
        with transaction.atomic():
            rows = list(
                RayTaskExecution.objects.select_for_update().filter(task_id=self.task_id)[:2]
            )
            if len(rows) > 1:
                raise ValueError("Ambiguous protocol fixture identity")
            if rows:
                row = rows[0]
                if (
                    (self.pk is not None and row.pk != self.pk)
                    or fingerprint(row) not in {self.staged_digest, self.queued_digest}
                    or row.attempts.exists()
                    or row.state not in {"FAILED", "QUEUED"}
                    or (row.state == "QUEUED" and policy.legacy_worker_admission_enabled)
                ):
                    raise ValueError("Foreign claim or changed fixture prevents cleanup")
                if row.state == "QUEUED":
                    checkpoint()
                    row.state = "FAILED"
                    row.save(update_fields=["state"])
            elif not policy.legacy_worker_admission_enabled or self.pk is not None:
                raise ValueError("Owned fixture disappeared before restoration")
        checkpoint()
        if not policy.legacy_worker_admission_enabled:
            transition = reopen_legacy_worker_admission(expected_revision=self.revision + 1)
            if not transition.enabled or transition.revision != self.revision + 2:
                raise ValueError("Owned admission restoration failed")
        checkpoint()
        restored = policy_state()
        if not restored.legacy_worker_admission_enabled or restored.revision not in {
            self.revision,
            self.revision + 2,
        }:
            raise ValueError("Protocol baseline was not restored")
        with transaction.atomic():
            for row in RayTaskExecution.objects.select_for_update().filter(task_id=self.task_id):
                if fingerprint(row) != self.staged_digest or row.attempts.exists():
                    raise ValueError("Terminal fixture identity changed before removal")
                checkpoint()
                row.delete()
        checkpoint()
        if RayTaskExecution.objects.filter(task_id=self.task_id).exists():
            raise ValueError("Owned protocol fixture remains")
