"""Additive policy authority preserves history and refuses active rollback."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.postgresql,
    pytest.mark.sqlite_compatible,
]

_OLD = [("django_ray", "0027_workflow_reporting_diagnostics")]
_NEW = [("django_ray", "0028_worker_lease_policy")]
_HEAD = [("django_ray", "0029_lease_engine_and_hot_indexes")]


def test_lease_policy_migration_preserves_old_rows_and_refuses_active_reverse():
    MigrationExecutor(connection).migrate(_OLD)
    try:
        apps = MigrationExecutor(connection).loader.project_state(_OLD).apps
        lease_model = apps.get_model("django_ray", "TaskWorkerLease")
        task_model = apps.get_model("django_ray", "RayTaskExecution")
        lease = lease_model.objects.create(
            worker_id="retained-old-owner",
            hostname="old-host",
            pid=1,
            last_heartbeat_at=timezone.now(),
            is_active=True,
        )
        task = task_model.objects.create(
            task_id="retained-queued",
            callable_path="fixture.task",
            state="QUEUED",
            args_json="[1]",
            kwargs_json="{}",
        )
        before_lease = lease_model.objects.filter(pk=lease.pk).values().get()
        before_task = task_model.objects.filter(pk=task.pk).values().get()
        MigrationExecutor(connection).migrate(_NEW)
        new_apps = MigrationExecutor(connection).loader.project_state(_NEW).apps
        policy_model = new_apps.get_model("django_ray", "TaskWorkerLeasePolicy")
        assert policy_model.objects.values_list("lease_seconds", "revision").get() == (60, 1)
        assert lease_model.objects.filter(pk=lease.pk).values().get() == before_lease
        assert task_model.objects.filter(pk=task.pk).values().get() == before_task
        with CaptureQueriesContext(connection) as captured:
            with pytest.raises(RuntimeError, match="lease_policy_active_cohort"):
                MigrationExecutor(connection).migrate(_OLD)
        sql_statements = [entry["sql"].upper() for entry in captured]
        if connection.vendor == "postgresql":
            assert any("FOR UPDATE NOWAIT" in sql for sql in sql_statements)
        else:
            assert any("SET REVISION = REVISION" in sql for sql in sql_statements)
        assert policy_model.objects.count() == 1
        lease_model.objects.filter(pk=lease.pk).update(is_active=False)
        MigrationExecutor(connection).migrate(_OLD)
        assert task_model.objects.filter(pk=task.pk).values().get() == before_task
    finally:
        MigrationExecutor(connection).migrate(_HEAD)


def test_lease_policy_migration_honors_custom_worker_lease_seconds_when_no_leases_exist(settings):
    MigrationExecutor(connection).migrate(_OLD)
    try:
        apps = MigrationExecutor(connection).loader.project_state(_OLD).apps
        lease_model = apps.get_model("django_ray", "TaskWorkerLease")
        lease_model.objects.all().delete()
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "WORKER_LEASE_SECONDS": 120,
        }
        MigrationExecutor(connection).migrate(_NEW)
        new_apps = MigrationExecutor(connection).loader.project_state(_NEW).apps
        policy_model = new_apps.get_model("django_ray", "TaskWorkerLeasePolicy")
        assert policy_model.objects.values_list("lease_seconds", "revision").get() == (120, 1)
    finally:
        MigrationExecutor(connection).migrate(_HEAD)
