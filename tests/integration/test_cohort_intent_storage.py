"""Bounded SQLite/PostgreSQL persistence for inactive protocol-3 intent."""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.db.models.query import QuerySet

from django_ray.models import RayTaskCohortIntent, RayTaskExecution, TaskExecutionProtocolPolicy
from django_ray.protocol_coordination import close_legacy_worker_admission
from django_ray.target.cohort_intent import (
    COHORT_INTENT_SCHEMA_VERSION,
    CohortExecutionDeclaration,
    CohortIntent,
    CohortSelectionPolicy,
    build_cohort_intent,
)
from django_ray.target.cohort_intent_storage import (
    CohortIntentStorageError,
    persist_cohort_intent,
    read_cohort_intent,
)

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 9, 12, 2, 0, tzinfo=UTC)
LATEST = [("django_ray", "0030_cohort_claims")]
INTENT = CohortIntent(
    "0.5.0",
    "default",
    "sha256:" + "a" * 64,
    "sha256:" + "b" * 64,
    CohortSelectionPolicy.WORKER_SELECTED,
)


@pytest.fixture(autouse=True)
def closed_legacy_admission(_restore_execution_protocol_rollout_seed):
    """Exercise the existing stopped-writer fence before inserting reserved work."""
    policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
    if policy.legacy_worker_admission_enabled:
        close_legacy_worker_admission(
            expected_revision=policy.revision, legacy_producers_retired=True
        )


def _execution(*, protocol=3, task_id="cohort-task", **changes):
    return RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=protocol,
        created_at=NOW - timedelta(seconds=1),
        **changes,
    )


def _persist(execution, intent=INTENT):
    with transaction.atomic():
        return persist_cohort_intent(execution.pk, intent, now=NOW)


def _raw_values(execution):
    return {
        "execution": execution,
        "schema_version": 2,
        "package_version": "0.5.0",
        "backend_alias": "default",
        "configuration_digest": INTENT.configuration_digest,
        "runtime_env_identity_digest": INTENT.runtime_env_identity_digest,
        "selection_policy": "worker_selected",
        "created_at": NOW,
    }


def test_intent_roundtrip_is_immutable_and_indexed():
    execution = _execution()
    row = _persist(execution)
    assert read_cohort_intent(execution.pk) == INTENT
    assert row.pk == execution.pk
    assert row.created_at == NOW
    assert row.schema_version == COHORT_INTENT_SCHEMA_VERSION == 2
    assert row.runtime_env_identity_digest == INTENT.runtime_env_identity_digest
    index = RayTaskCohortIntent._meta.indexes[0]
    assert index.fields == [
        "configuration_digest",
        "package_version",
        "backend_alias",
        "selection_policy",
    ]
    for changes in (
        {"backend_alias": "other"},
        {"schema_version": 2},
        {"created_at": NOW},
        {"runtime_env_identity_digest": "sha256:" + "c" * 64},
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            RayTaskCohortIntent.objects.filter(pk=row.pk).update(**changes)


def test_caller_transaction_rolls_back_task_and_intent_together():
    with pytest.raises(RuntimeError, match="enqueue failed"), transaction.atomic():
        execution = _execution()
        persist_cohort_intent(execution.pk, INTENT, now=NOW)
        raise RuntimeError("enqueue failed")
    assert not RayTaskExecution.objects.filter(task_id="cohort-task").exists()
    assert not RayTaskCohortIntent.objects.exists()


def test_finite_admission_filter_precedes_limit_without_partitioning_task_runtime_env():
    """The stored admission fields support fair selection before any future claim."""
    declared = CohortExecutionDeclaration("default", "http://jobs:8265", False)

    def enqueue(
        task_id, *, declaration=declared, package_version="0.5.0", observation="a", priority=0
    ):
        execution = _execution(
            task_id=task_id, priority=priority, ray_target_address=declaration.ray_address
        )
        _persist(
            execution,
            build_cohort_intent(
                declaration,
                package_version=package_version,
                runtime_env_identity_digest="sha256:" + observation * 64,
            ),
        )
        return execution

    enqueue(
        "other-endpoint",
        declaration=replace(declared, ray_address="http://other:8265"),
        priority=10,
    )
    enqueue(
        "other-trust",
        declaration=replace(declared, trust_identity={"trust_domain": "other"}),
        priority=10,
    )
    enqueue("other-package", package_version="0.6.0", priority=10)
    enqueue("jobs-only", declaration=replace(declared, ray_job_only=True), priority=10)
    first = enqueue("dynamic-env-a", observation="a", priority=1)
    second = enqueue("dynamic-env-b", observation="b")
    expected = read_cohort_intent(first.pk)
    assert (
        read_cohort_intent(second.pk).runtime_env_identity_digest
        != expected.runtime_env_identity_digest
    )
    eligible = RayTaskExecution.objects.filter(
        execution_protocol_version=3,
        state="QUEUED",
        ray_target_address=declared.ray_address,
        cohort_intent__schema_version=2,
        cohort_intent__configuration_digest=expected.configuration_digest,
        cohort_intent__package_version=expected.package_version,
        cohort_intent__backend_alias=expected.backend_alias,
        cohort_intent__selection_policy=expected.selection_policy.value,
    ).order_by("-priority", "id")
    assert list(eligible.values_list("task_id", flat=True)[:2]) == [first.task_id, second.task_id]


def test_storage_requires_caller_transaction_and_one_insert():
    execution = _execution()
    with pytest.raises(CohortIntentStorageError, match="transaction_required"):
        persist_cohort_intent(execution.pk, INTENT, now=NOW)
    _persist(execution)
    with pytest.raises(CohortIntentStorageError, match="persistence_refused"):
        _persist(execution)
    assert RayTaskCohortIntent.objects.count() == 1


@pytest.mark.parametrize("protocol", [1, 2, 4])
def test_other_protocols_have_no_inferred_intent(protocol):
    execution = _execution(protocol=protocol)
    with pytest.raises(CohortIntentStorageError, match="execution_unavailable"):
        _persist(execution)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskCohortIntent.objects.create(**_raw_values(execution))
    with pytest.raises(CohortIntentStorageError, match="intent_unavailable"):
        read_cohort_intent(execution.pk)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "RUNNING"},
        {"state": "SUCCEEDED"},
        {"execution_generation": 1},
        {"attempt_number": 2},
        {"claimed_by_worker": "manager"},
        {"started_at": NOW},
        {"finished_at": NOW},
        {"last_heartbeat_at": NOW},
        {"ray_job_id": "retained-job"},
        {"ray_address": "retained-address"},
        {"ray_job_request_reference": "retained-request"},
        {"completion_data": "{}"},
        {"executor_django_ray_version": "0.5.0"},
        {"result_data": "1"},
        {"result_reference": "retained-result"},
        {"cancellation_status": "PENDING"},
    ],
)
def test_executed_or_previously_claimed_work_cannot_gain_invented_intent(changes):
    execution = _execution(**changes)
    with pytest.raises(CohortIntentStorageError, match="execution_unavailable"):
        _persist(execution)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskCohortIntent.objects.create(**_raw_values(execution))
    assert not RayTaskCohortIntent.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 1},
        {"schema_version": 3},
        {"package_version": ""},
        {"package_version": "0" * 129},
        {"package_version": "v0.5.0"},
        {"package_version": "0.5.0\x00ignored"},
        {"backend_alias": ""},
        {"backend_alias": "a" * 129},
        {"backend_alias": "default alias"},
        {"backend_alias": "default\n"},
        {"backend_alias": "d\x00ignored"},
        {"backend_alias": "café"},
        {"configuration_digest": "sha256:" + "B" * 64},
        {"configuration_digest": INTENT.configuration_digest + "\x00ignored"},
        {"runtime_env_identity_digest": ""},
        {"runtime_env_identity_digest": None},
        {"runtime_env_identity_digest": "b" * 64},
        {"runtime_env_identity_digest": "sha256:" + "B" * 64},
        {"runtime_env_identity_digest": INTENT.runtime_env_identity_digest + "\x00ignored"},
        {"selection_policy": "future_policy"},
        {"created_at": NOW - timedelta(seconds=2)},
    ],
)
def test_database_bounds_reject_invalid_intent_rows(changes):
    execution = _execution()
    values = _raw_values(execution)
    values.update(changes)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskCohortIntent.objects.create(**values)
    assert not RayTaskCohortIntent.objects.exists()


@pytest.mark.parametrize("version", ["0.5.0", "1!0.5.0rc1.post2.dev3+linux.x86.64"])
def test_canonical_version_and_exact_printable_alias_are_preserved(version):
    intent = replace(INTENT, package_version=version, backend_alias="Queue.A/Jobs")
    execution = _execution()
    _persist(execution, intent)
    assert read_cohort_intent(execution.pk) == intent


def test_private_codec_rejects_noncanonical_versions_before_persistence():
    execution = _execution()
    with pytest.raises(CohortIntentStorageError, match="invalid"):
        _persist(execution, replace(INTENT, package_version="v0.5.0"))
    assert not RayTaskCohortIntent.objects.exists()


def test_missing_execution_and_clock_regression_fail_closed():
    execution = _execution()
    with (
        pytest.raises(CohortIntentStorageError, match="execution_unavailable"),
        transaction.atomic(),
    ):
        persist_cohort_intent(execution.pk, INTENT, now=NOW - timedelta(seconds=2))
    with (
        pytest.raises(CohortIntentStorageError, match="execution_unavailable"),
        transaction.atomic(),
    ):
        persist_cohort_intent(execution.pk + 1, INTENT, now=NOW)
    with pytest.raises(CohortIntentStorageError, match="invalid"), transaction.atomic():
        persist_cohort_intent(True, INTENT, now=NOW)


def test_protected_history_requires_explicit_child_retention():
    execution = _execution()
    _persist(execution)
    with pytest.raises(ProtectedError):
        execution.delete()
    RayTaskCohortIntent.objects.filter(pk=execution.pk).delete()
    execution.delete()


@pytest.mark.parametrize("schema,protocol", [(1, 3), (3, 3), (2, 4)])
def test_reader_rejects_old_or_future_schema_or_protocol(monkeypatch, schema, protocol):
    row = SimpleNamespace(
        schema_version=schema,
        execution=SimpleNamespace(execution_protocol_version=protocol),
    )
    monkeypatch.setattr(QuerySet, "first", lambda self: row)
    with pytest.raises(CohortIntentStorageError, match="unsupported_schema"):
        read_cohort_intent(1)


def test_intent_rollback_guard_retains_live_rows():
    execution = _execution()
    _persist(execution)
    migration = importlib.import_module("django_ray.migrations.0028_ray_task_cohort_intent")
    with pytest.raises(RuntimeError, match="cohort intent remains"), transaction.atomic():
        migration._guard_empty(apps, connection.schema_editor())


def test_new_migrations_do_not_backfill_old_execution_history():
    try:
        MigrationExecutor(connection).migrate(
            [("django_ray", "0026_ray_task_target_execution_evidence")]
        )
        old_apps = (
            MigrationExecutor(connection)
            .loader.project_state([("django_ray", "0026_ray_task_target_execution_evidence")])
            .apps
        )
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution").objects.create(
            task_id="old-history",
            callable_path="removed.old.callable",
            execution_protocol_version=1,
        )
        MigrationExecutor(connection).migrate(LATEST)
        assert not RayTaskCohortIntent.objects.exists()
        preserved = RayTaskExecution.objects.get(pk=old_execution.pk)
        assert preserved.callable_path == "removed.old.callable"
        assert preserved.execution_protocol_version == 1
    finally:
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.postgresql
def test_postgresql_duplicate_intent_insert_has_one_winner():
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    execution = _execution()
    barrier = Barrier(2)

    def compete():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            _persist(execution)
            return "success"
        except CohortIntentStorageError as error:
            return error.classification.value
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compete) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == ["persistence_refused", "success"]


@pytest.mark.postgresql
def test_postgresql_intent_constraints_and_rollback():
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    test_caller_transaction_rolls_back_task_and_intent_together()
    execution = _execution()
    for invalid in (
        {"schema_version": 1},
        {"backend_alias": "has space"},
        {"runtime_env_identity_digest": "sha256:" + "B" * 64},
    ):
        values = _raw_values(execution)
        values.update(invalid)
        with pytest.raises(DatabaseError), transaction.atomic():
            RayTaskCohortIntent.objects.create(**values)
    _persist(execution)
    assert read_cohort_intent(execution.pk) == INTENT
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskCohortIntent.objects.filter(pk=execution.pk).update(package_version="0.6.0")
