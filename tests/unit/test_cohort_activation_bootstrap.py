"""Fresh-process default activation through a bounded SQLite Sync worker."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_fresh_default_producer_and_sync_worker_complete_current_cohort(tmp_path):
    """Use real defaults, migration seeds and guards without native Ray startup."""
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "activation_settings.py").write_text(
        "SECRET_KEY = 'activation-test-only'\n"
        "INSTALLED_APPS = ['django.contrib.contenttypes', 'django_ray']\n"
        "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', "
        "'NAME': ':memory:'}}\n"
        "TASKS = {'default': {'BACKEND': 'django_ray.backends.RayTaskBackend', "
        "'QUEUES': ['default'], 'OPTIONS': {}}}\n"
        "DJANGO_RAY = {'RAY_ADDRESS': 'auto'}\n"
        "USE_TZ = True\n",
        encoding="utf-8",
    )
    (tmp_path / "activation_tasks.py").write_text(
        "from django.tasks import task\n@task\ndef add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    script = """
import json
from unittest.mock import patch
import django
django.setup()
from django.core.management import call_command
from django_ray.execution_protocol import (
    EXECUTION_PROTOCOL_VERSION, MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
)
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import (
    LegacyWorkerAdmissionToken, RayTaskCohortClaim, RayTaskCohortIntent,
    RayTaskExecution, TaskAttempt, TaskExecutionProtocolPolicy, TaskWorkerLease,
)
assert (EXECUTION_PROTOCOL_VERSION, MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
        MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION) == (3, 3, 3)
call_command('migrate', verbosity=0, interactive=False)
policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
assert policy.active_write_protocol_version == 3
assert not policy.legacy_worker_admission_enabled
assert not LegacyWorkerAdmissionToken.objects.exists()
from activation_tasks import add
result = add.enqueue(2, 5)
task = RayTaskExecution.objects.get(task_id=result.id)
assert task.execution_protocol_version == 3 and task.metadata_schema_version == 1
assert RayTaskCohortIntent.objects.get(execution=task).package_version
command = Command()
def finish_after_first_cycle(timeout):
    task.refresh_from_db()
    assert task.state == 'SUCCEEDED', (task.state, task.error_message)
    command.shutdown_requested = True
with patch.object(command, '_wait_for_poll_deadline', finish_after_first_cycle), \
     patch.object(command, '_initialize_ray_execution', side_effect=AssertionError('legacy init')), \
     patch.object(command, 'claim_and_process_tasks', side_effect=AssertionError('legacy claim')):
    call_command(command, sync=True, queue='default', concurrency=1, verbosity=0)
task.refresh_from_db()
assert json.loads(task.result_data) == 7
claim = RayTaskCohortClaim.objects.get(binding_id=task.pk)
assert claim.disposition == 'RESOLVED'
assert claim.resolution_kind == 'application_completed'
attempt = TaskAttempt.objects.get(execution_id=task.pk)
assert attempt.state == 'SUCCEEDED' and attempt.execution_protocol_version == 3
lease = TaskWorkerLease.objects.get(worker_id=command.worker_id)
assert not lease.is_active and lease.legacy_admission_token_id is None
assert (lease.min_supported_execution_protocol_version,
        lease.max_supported_execution_protocol_version) == (3, 3)
assert command.tasks_processed_count == 1 and command.shutdown_exit_code is None
print('CURRENT_COHORT_SYNC_BOOTSTRAP_OK')
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(tmp_path)))
    environment["DJANGO_SETTINGS_MODULE"] = "activation_settings"
    environment.pop("DJANGO_RAY_SKIP_VALIDATION", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CURRENT_COHORT_SYNC_BOOTSTRAP_OK" in result.stdout
