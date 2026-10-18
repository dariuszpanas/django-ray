"""Additive policy authority preserves history and refuses active rollback."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_lease_policy_migration_preserves_old_rows_and_refuses_active_reverse():
    old = [("django_ray", "0027_workflow_reporting_diagnostics")]
    new = [("django_ray", "0028_worker_lease_policy")]
    MigrationExecutor(connection).migrate(old)
    try:
        apps = MigrationExecutor(connection).loader.project_state(old).apps
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
        MigrationExecutor(connection).migrate(new)
        new_apps = MigrationExecutor(connection).loader.project_state(new).apps
        policy_model = new_apps.get_model("django_ray", "TaskWorkerLeasePolicy")
        assert policy_model.objects.values_list("lease_seconds", "revision").get() == (60, 1)
        assert lease_model.objects.filter(pk=lease.pk).values().get() == before_lease
        assert task_model.objects.filter(pk=task.pk).values().get() == before_task
        with pytest.raises(RuntimeError, match="lease_policy_active_cohort"):
            MigrationExecutor(connection).migrate(old)
        assert policy_model.objects.count() == 1
        lease_model.objects.filter(pk=lease.pk).update(is_active=False)
        MigrationExecutor(connection).migrate(old)
        assert task_model.objects.filter(pk=task.pk).values().get() == before_task
    finally:
        MigrationExecutor(connection).migrate(new)
