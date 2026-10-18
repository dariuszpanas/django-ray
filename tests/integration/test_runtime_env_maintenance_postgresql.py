"""Independent PostgreSQL transactions for envelope maintenance and lifecycle races."""

from __future__ import annotations

import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from django.db import connection, transaction

from django_ray.lifecycle import (
    record_failure,
    request_task_cancellation,
    request_task_retry,
    succeed_task,
)
from django_ray.management import runtime_env_maintenance as maintenance
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.runtime.runtime_env import RuntimeEnvSnapshotError, normalize_runtime_env

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def owned_database(settings):
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and the owned PostgreSQL test database")
    selected = copy.deepcopy(connection.settings_dict)
    settings.DATABASES = {**settings.DATABASES, "maintenance": selected}
    key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    config = {
        "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {"current": key},
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "current",
    }
    settings.DJANGO_RAY = config
    return config


def execution(state=TaskState.RUNNING):
    resolved = normalize_runtime_env({"env_vars": {"TOKEN": "pg-maintenance-secret"}})
    return RayTaskExecution.objects.create(
        task_id="maintenance-" + uuid4().hex,
        callable_path="must_not_import.execute",
        state=state,
        attempt_number=2,
        execution_generation=5,
        runtime_env_json=resolved.serialized,
        runtime_env_hash=resolved.digest,
    )


def maintain(config, **options):
    return maintenance.maintain_runtime_env(
        using="maintenance",
        config=config,
        mode="rewrite",
        confirm=True,
        **options,
    )


@pytest.mark.parametrize("operation", ["retry", "cancellation", "completion", "deletion"])
def test_lifecycle_commit_between_read_and_cas_is_preserved(owned_database, monkeypatch, operation):
    task = execution(TaskState.FAILED if operation == "retry" else TaskState.RUNNING)
    original_envelope = task.runtime_env_json
    ready, proceed = Event(), Event()
    original = maintenance._Database.replace

    def blocked(self, snapshot, envelope):
        ready.set()
        if not proceed.wait(5):
            raise TimeoutError("maintenance race barrier expired")
        return original(self, snapshot, envelope)

    monkeypatch.setattr(maintenance._Database, "replace", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(maintain, owned_database)
        try:
            assert ready.wait(5)
            with transaction.atomic():
                if operation == "retry":
                    assert request_task_retry(task).accepted
                elif operation == "cancellation":
                    assert request_task_cancellation(task.pk).accepted
                elif operation == "completion":
                    assert succeed_task(task, result_data='"preserved"', result_reference=None)
                else:
                    RayTaskExecution.objects.filter(pk=task.pk).delete()
        finally:
            proceed.set()
        report = pending.result(timeout=8)
    assert report["exit_code"] == 2 and report["counts"]["changed"] == 1
    if operation == "deletion":
        assert not RayTaskExecution.objects.filter(pk=task.pk).exists()
    else:
        task.refresh_from_db()
        assert task.runtime_env_json == original_envelope
        if operation == "retry":
            assert task.attempt_number == 3 and task.execution_generation == 6
            assert TaskAttempt.objects.filter(execution=task, attempt_number=2).exists()
        elif operation == "cancellation":
            assert task.state == TaskState.CANCELLING
        else:
            assert task.state == TaskState.SUCCEEDED and task.result_data == '"preserved"'


def test_competing_rewrite_retains_one_committed_envelope(owned_database, monkeypatch):
    task = execution()
    ready, proceed = Event(), Event()
    original = maintenance._Database.replace
    blocked_once = [False]

    def blocked(self, snapshot, envelope):
        if not blocked_once[0]:
            blocked_once[0] = True
            ready.set()
            if not proceed.wait(5):
                raise TimeoutError("maintenance race barrier expired")
        return original(self, snapshot, envelope)

    monkeypatch.setattr(maintenance._Database, "replace", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(maintain, owned_database)
        try:
            assert ready.wait(5)
            winner = maintain(owned_database)
            assert winner["counts"]["rewritten"] == 1
            task.refresh_from_db()
            retained = task.runtime_env_json
        finally:
            proceed.set()
        loser = first.result(timeout=8)
    assert loser["counts"]["changed"] == 1
    task.refresh_from_db()
    assert task.runtime_env_json == retained


@pytest.mark.parametrize("automatic", [False, True])
def test_required_mode_retry_refusal_preserves_attempt_and_history(
    owned_database, settings, automatic
):
    task = execution(TaskState.RUNNING if automatic else TaskState.FAILED)
    settings.DJANGO_RAY = {**owned_database, "RUNTIME_ENV_STORAGE_MODE": "required"}
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    with pytest.raises(RuntimeEnvSnapshotError, match="plaintext is forbidden"):
        if automatic:
            record_failure(task, error_message="must not replace", retry=True)
        else:
            request_task_retry(task)
    assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
    assert not TaskAttempt.objects.filter(execution=task).exists()


def test_owned_readonly_connection_is_distinct_and_preserves_caller(owned_database, monkeypatch):
    execution()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_backend_pid(),current_setting('statement_timeout'),current_setting('lock_timeout')"
        )
        caller = cursor.fetchone()
    observed = []
    original = maintenance._Database.open

    def opened(self):
        original(self)
        observed.append(
            self.query(
                "SELECT pg_backend_pid(),current_setting('default_transaction_read_only'),"
                "current_setting('statement_timeout'),current_setting('lock_timeout')",
                fetch="one",
            )
        )

    monkeypatch.setattr(maintenance._Database, "open", opened)
    result = maintenance.maintain_runtime_env(
        using="maintenance", config=owned_database, mode="check"
    )
    assert result["counts"]["plaintext"] == 1 and result["cleanup_confirmed"]
    assert observed[0][0] != caller[0] and observed[0][1:] == ("on", "1s", "250ms")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_backend_pid(),current_setting('statement_timeout'),current_setting('lock_timeout')"
        )
        assert cursor.fetchone() == caller


def test_contended_row_lock_is_bounded_and_rewrite_does_not_overwrite(owned_database):
    task = execution()
    with transaction.atomic():
        RayTaskExecution.objects.select_for_update().get(pk=task.pk)
        with ThreadPoolExecutor(max_workers=1) as pool:
            report = pool.submit(maintain, owned_database).result(timeout=5)
    assert report["counts"]["database_unavailable"] == 1
    assert report["counts"]["rewritten"] == 0 and report["after"] == 0
    task.refresh_from_db()
    assert "pg-maintenance-secret" in task.runtime_env_json


def test_changed_search_path_rejects_cursor_before_other_schema_rows(owned_database, settings):
    execution()
    execution()
    report = maintain(owned_database, limits=maintenance.MaintenanceLimits(rows=1))
    schema = "maintenance_" + uuid4().hex
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TABLE "{schema}".django_ray_raytaskexecution (LIKE django_ray_raytaskexecution INCLUDING ALL)'
            )
        selected = copy.deepcopy(settings.DATABASES["maintenance"])
        selected["OPTIONS"] = {"options": f"-c search_path={schema},public"}
        settings.DATABASES["maintenance"] = selected
        with pytest.raises(maintenance.MaintenanceError, match="cursor"):
            maintain(owned_database, cursor=report["next_cursor"])
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_rewrite_preserves_all_non_envelope_fields_on_real_schema(owned_database):
    task = execution(TaskState.LOST)
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    report = maintain(owned_database)
    after = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    assert report["counts"]["rewritten"] == 1
    assert json.loads(after.pop("runtime_env_json"))["key_id"] == "current"
    before.pop("runtime_env_json")
    assert before == after
    assert not TaskAttempt.objects.filter(execution=task).exists()
