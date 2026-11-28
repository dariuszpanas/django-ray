"""Forward and reverse migration tests for 0029_lease_engine_and_hot_indexes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.postgresql,
    pytest.mark.sqlite_compatible,
]

_BEFORE = [("django_ray", "0028_worker_lease_policy")]
_AFTER = [("django_ray", "0029_lease_engine_and_hot_indexes")]
_EXPECTED_LEASE_SQLITE_TRIGGERS = {
    "ray_lease_admission_insert_0019",
    "ray_lease_admission_update_0019",
    "ray_lease_capability_immutable_0019",
    "ray_wtcap_lease_update_0025",
}


def _table_index_names(table_name: str) -> set[str]:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, table_name)
    return {name for name, info in constraints.items() if info.get("index")}


def _sqlite_trigger_names(table_name: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = %s",
            [table_name],
        )
        return {str(row[0]) for row in cursor.fetchall()}


def test_migration_0029_backfills_leases_indexes_and_preserves_triggers() -> None:
    MigrationExecutor(connection).migrate(_BEFORE)
    try:
        before_apps = MigrationExecutor(connection).loader.project_state(_BEFORE).apps
        policy_model = before_apps.get_model("django_ray", "TaskWorkerLeasePolicy")
        lease_model = before_apps.get_model("django_ray", "TaskWorkerLease")
        task_model = before_apps.get_model("django_ray", "RayTaskExecution")
        attempt_model = before_apps.get_model("django_ray", "TaskAttempt")

        lease_model.objects.all().delete()
        task_model.objects.all().delete()
        policy_model.objects.filter(singleton_key=1).update(lease_seconds=90, revision=3)
        heartbeat = timezone.now().replace(microsecond=0) - timedelta(seconds=10)
        lease = lease_model.objects.create(
            worker_id="pre-0029-worker",
            hostname="migration-host",
            pid=42,
            queue_name="default",
            started_at=heartbeat - timedelta(seconds=30),
            last_heartbeat_at=heartbeat,
            is_active=True,
        )

        if connection.vendor == "sqlite":
            assert _EXPECTED_LEASE_SQLITE_TRIGGERS.issubset(
                _sqlite_trigger_names(lease_model._meta.db_table)
            )

        MigrationExecutor(connection).migrate(_AFTER)

        after_apps = MigrationExecutor(connection).loader.project_state(_AFTER).apps
        after_lease_model = after_apps.get_model("django_ray", "TaskWorkerLease")
        migrated = after_lease_model.objects.get(pk=lease.pk)
        assert migrated.lease_expires_at == heartbeat + timedelta(seconds=90)
        assert migrated.lease_policy_revision == 3

        # Multi-queue CSV wider than historical 100-char limit must fit cleanly (F-063).
        wide_queues = ",".join(f"queue-{idx:03d}" for idx in range(30))
        assert len(wide_queues) > 100
        wide_lease = after_lease_model.objects.create(
            worker_id="wide-queue-worker",
            hostname="migration-host",
            pid=43,
            queue_name=wide_queues,
            last_heartbeat_at=heartbeat,
            lease_expires_at=heartbeat + timedelta(seconds=90),
            lease_policy_revision=3,
            is_active=False,
        )
        assert after_lease_model.objects.get(pk=wide_lease.pk).queue_name == wide_queues

        # Verify hot-path partial indexes, attempt state index, and removed indexes.
        lease_indexes = _table_index_names(after_lease_model._meta.db_table)
        assert "ray_lease_active_worker_idx" in lease_indexes
        with connection.cursor() as cursor:
            lease_constraints = connection.introspection.get_constraints(
                cursor, after_lease_model._meta.db_table
            )
        indexed_columns = {
            tuple(info["columns"])
            for info in lease_constraints.values()
            if info.get("index") and info.get("columns")
        }
        assert ("lease_expires_at",) not in indexed_columns

        task_indexes = _table_index_names(task_model._meta.db_table)
        assert "ray_task_heartbeat_idx" not in task_indexes
        assert "ray_task_claimable_idx" not in task_indexes
        assert "ray_task_expiry_idx" not in task_indexes
        assert "ray_task_claim_hot_idx" in task_indexes
        assert "ray_task_active_maint_idx" in task_indexes
        assert "ray_task_queued_exp_idx" in task_indexes
        assert "ray_task_term_purge_idx" in task_indexes
        assert "ray_task_result_ref_idx" in task_indexes
        with connection.cursor() as cursor:
            task_constraints = connection.introspection.get_constraints(
                cursor, task_model._meta.db_table
            )
        assert tuple(task_constraints["ray_task_active_maint_idx"]["columns"]) == (
            "state",
            "id",
            "queue_name",
        )

        attempt_indexes = _table_index_names(attempt_model._meta.db_table)
        assert "ray_attempt_state_idx" in attempt_indexes
        assert "ray_attempt_result_ref_idx" in attempt_indexes
        assert "django_ray__executi_31dfd4_idx" not in attempt_indexes

        for model_name, dropped_index in (
            ("RayTargetPolicyRevision", "ray_tpolicy_latest_idx"),
            ("RayTargetAttestationRevision", "ray_tattest_latest_idx"),
            ("RayTargetRouteRevision", "ray_troute_latest_idx"),
        ):
            dormant_model = after_apps.get_model("django_ray", model_name)
            assert dropped_index not in _table_index_names(dormant_model._meta.db_table)

        if connection.vendor == "sqlite":
            assert _EXPECTED_LEASE_SQLITE_TRIGGERS.issubset(
                _sqlite_trigger_names(after_lease_model._meta.db_table)
            )

        # Clean up wide row before reverse migration (queue_name narrows back to 100).
        after_lease_model.objects.filter(pk=wide_lease.pk).delete()
        MigrationExecutor(connection).migrate(_BEFORE)

        if connection.vendor == "sqlite":
            assert _EXPECTED_LEASE_SQLITE_TRIGGERS.issubset(
                _sqlite_trigger_names(lease_model._meta.db_table)
            )
        assert lease_model.objects.filter(pk=lease.pk).exists()
    finally:
        MigrationExecutor(connection).migrate(_AFTER)
