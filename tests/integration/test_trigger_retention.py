"""Verify database trigger retention across migrations on SQLite and PostgreSQL.

Guards against Django's SQLite table-rebuild behavior silently dropping triggers
installed by earlier migrations (`0019`, `0020`, `0025`) when later migrations
alter `RayTaskExecution`, `TaskAttempt`, or `TaskWorkerLease`, and verifies the
exact column-scoped update semantics of the `0025` lease trigger.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import DatabaseError, IntegrityError, connection
from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease

_EXPECTED_0019_TRIGGERS = frozenset(
    {
        "ray_attempt_immutable_0019",
        "ray_exec_immutable_0019",
        "ray_exec_legacy_insert_0019",
        "ray_exec_owner_update_0019",
        "ray_lease_admission_insert_0019",
        "ray_lease_admission_update_0019",
        "ray_lease_capability_immutable_0019",
    }
)
_EXPECTED_0020_TRIGGERS = frozenset(
    {
        "ray_exec_legacy_open_insert_0020",
        "ray_exec_legacy_open_retry_0020",
    }
)
_EXPECTED_0025_TRIGGERS_BY_VENDOR = {
    "sqlite": frozenset(
        {
            "ray_wtcap_insert_0025",
            "ray_wtcap_update_0025",
            "ray_wtcap_lease_update_0025",
        }
    ),
    "postgresql": frozenset(
        {
            "ray_wtcap_guard_0025",
            "ray_wtcap_lease_guard_0025",
        }
    ),
}
_LEASE_UPDATE_TRIGGER_BY_VENDOR = {
    "sqlite": "ray_wtcap_lease_update_0025",
    "postgresql": "ray_wtcap_lease_guard_0025",
}


def _installed_triggers() -> dict[str, str]:
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
            return {str(name): str(sql or "") for name, sql in cursor.fetchall()}
        if connection.vendor == "postgresql":
            cursor.execute(
                """
                SELECT tgname, pg_get_triggerdef(oid)
                FROM pg_trigger
                WHERE NOT tgisinternal
                """
            )
            return {str(name): str(defn or "") for name, defn in cursor.fetchall()}
    pytest.skip(f"Unsupported database vendor for trigger retention check: {connection.vendor}")


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
@pytest.mark.sqlite_compatible
def test_migrations_retain_0019_0020_and_0025_triggers() -> None:
    """All protocol and target-capability triggers survive through the latest migration."""
    triggers = _installed_triggers()
    vendor = connection.vendor
    expected = (
        _EXPECTED_0019_TRIGGERS
        | _EXPECTED_0020_TRIGGERS
        | _EXPECTED_0025_TRIGGERS_BY_VENDOR[vendor]
    )
    missing = sorted(expected - set(triggers))
    assert not missing, f"Missing {vendor} migration triggers after full migrate: {missing}"

    lease_trigger_name = _LEASE_UPDATE_TRIGGER_BY_VENDOR[vendor]
    normalized_ddl = " ".join(triggers[lease_trigger_name].lower().split())
    assert "before update of worker_id, hostname, pid, started_at" in normalized_ddl


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
@pytest.mark.sqlite_compatible
def test_retained_triggers_enforce_immutability_without_blocking_heartbeats() -> None:
    """`0019` immutability triggers remain active while lease heartbeats update cleanly."""
    execution = RayTaskExecution.objects.create(
        task_id="trigger-retention-immutability",
        callable_path="testproject.tasks.add_numbers",
        queue_name="default",
        state=TaskState.QUEUED,
        args_json="[1, 2]",
        kwargs_json="{}",
    )
    table_name = connection.ops.quote_name(RayTaskExecution._meta.db_table)
    with pytest.raises((DatabaseError, IntegrityError)), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table_name} SET execution_protocol_version = 2 WHERE id = %s",
            [execution.pk],
        )

    lease = TaskWorkerLease.objects.create(
        worker_id="trigger-retention-worker",
        hostname="trigger-retention-host",
        pid=12345,
        queue_name="default",
        is_active=True,
    )
    later = timezone.now() + timedelta(seconds=5)
    updated = TaskWorkerLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=later)
    assert updated == 1
