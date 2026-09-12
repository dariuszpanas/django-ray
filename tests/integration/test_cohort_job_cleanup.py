"""Post-result cleanup survives retries, owner loss and ordinary retention."""

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from django_ray import maintenance
from django_ray.models import RayCohortJobCleanup, RayTaskCohortClaim, RayTaskExecution
from django_ray.runner import cohort_completion as completion
from django_ray.runner.cohort_job_execution_control import CohortJobExecutionInspection
from django_ray.target import cohort_claim_storage as storage
from django_ray.target import cohort_job_cleanup as cleanup
from django_ray.target.cohort_claim import CohortResolutionKind
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import (
    _apply,
    _result,
    _started,
    _stored_request_reference,
)
from tests.migration_cleanup import (
    closed_preactivation_protocol_schema as closed_preactivation_protocol_schema,
)
from tests.migration_cleanup import preactivation_protocol_schema as preactivation_protocol_schema

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _completed(case, *, reference=True, callback=None, success=True):
    value = _started(case, "ray_job")
    if reference:
        _stored_request_reference(value)
    result = _apply(value, case, result=_result(value, success=success), callback=callback)
    return result.dispatch, cleanup.cleanup_record(RayCohortJobCleanup.objects.get())


def _close(case, record, **changes):
    values = {
        "inspection": CohortJobExecutionInspection(
            record.expectation, "SUCCEEDED", False, "01000000"
        ),
        "inspection_began_at": case.now,
        "observed_at": case.now,
    } | changes
    return cleanup.close_cohort_job_cleanup(case.owner, record, **values)


def test_authentic_result_and_cleanup_are_distinct_and_close_keeps_original_facts(case):
    value, record = _completed(case)
    assert record.state == "OPEN" and record.expectation is not None
    assert record.claim_id == value.claim.claim_id and record.owner == value.claim.owner
    original = RayTaskCohortClaim.objects.values().get()
    current = RayTaskExecution.objects.values().get()
    assert original["disposition"] == "RESOLVED" and current["state"] == "SUCCEEDED"
    assert cleanup.owned_job_cleanup_pending(case.owner)
    with transaction.atomic(), pytest.raises(cleanup.CohortJobCleanupError, match="pending"):
        cleanup.check_no_pending_job_cleanup(value.execution)
    closed = _close(case, record)
    assert closed.state == "CLOSED" and closed.revision == record.revision + 1
    assert not cleanup.owned_job_cleanup_pending(case.owner)
    assert RayTaskCohortClaim.objects.values().get() == original
    assert RayTaskExecution.objects.values().get() == current
    with pytest.raises(cleanup.CohortJobCleanupError, match="changed"):
        _close(case, record)


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "STOPPED"])
def test_exact_post_completion_terminal_status_closes_cleanup_only(case, status):
    value, record = _completed(case)
    result = _close(
        case,
        record,
        inspection=CohortJobExecutionInspection(
            record.expectation, status, status == "STOPPED", "01000000"
        ),
    )
    assert result.state == "CLOSED"
    value.execution.refresh_from_db()
    assert value.execution.state == "SUCCEEDED"


@pytest.mark.parametrize(
    "status,native",
    [
        ("RUNNING", "01000000"),
        ("PENDING", None),
        ("SUCCEEDED", None),
        ("STOPPED", "ffffffff"),
        ("FAILED", "0100000A"),
    ],
)
def test_nonterminal_or_uncorroborated_native_driver_never_closes(case, status, native):
    _value, record = _completed(case)
    with pytest.raises(cleanup.CohortJobCleanupError, match="inspection_unconfirmed"):
        _close(
            case,
            record,
            inspection=CohortJobExecutionInspection(
                record.expectation, status, status == "STOPPED", native
            ),
        )
    assert RayCohortJobCleanup.objects.get().state == "OPEN"


def test_missing_expectation_keeps_truthful_completion_and_blocks_retirement(case):
    value, record = _completed(case, reference=False)
    assert record.expectation is None and record.missing_expectation_reason == "missing_expectation"
    assert value.execution.state == "SUCCEEDED" and value.claim.disposition.value == "RESOLVED"
    with pytest.raises(cleanup.CohortJobCleanupError, match="inspection_unconfirmed"):
        _close(case, record)
    case.monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    maintenance.request_worker_retirement(
        case.owner, expected_revision=0, actor="test", reason="test", authorized=True
    )
    with pytest.raises(maintenance.MaintenanceAdmissionError, match="ownership_remains"):
        maintenance.complete_worker_retirement(
            case.owner,
            expected_revision=1,
            actor="test",
            reason="test",
            authorized=True,
            independently_confirmed_cleanup=True,
            cleanup_evidence_digest="sha256:" + "a" * 64,
            cleanup_confirmed_at=case.now,
        )
    # The test owns this inactive fixture only; scalar retirement audit persists.
    case.lease.delete()


def test_database_rejects_all_null_expectation_group_at_initial_insert(case, monkeypatch):
    original_save = RayCohortJobCleanup.save

    def all_null(row, *args, **kwargs):
        if row._state.adding:
            row.expectation_json = None
            row.expectation_digest = None
            row.missing_expectation_reason = None
        return original_save(row, *args, **kwargs)

    monkeypatch.setattr(RayCohortJobCleanup, "save", all_null)
    value = _started(case, "ray_job")
    with pytest.raises(cleanup.CohortJobCleanupError, match="persistence_refused"):
        _apply(value, case, result=_result(value))
    assert not RayCohortJobCleanup.objects.exists()
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"
    assert RayTaskExecution.objects.get().state == "RUNNING"


def test_retry_can_queue_but_pending_cleanup_fences_next_generation_before_limit(case):
    def retry(task, _decoded, *, retry_admitted):
        assert retry_admitted
        task.state = "QUEUED"
        task.attempt_number += 1
        task.save(update_fields=("state", "attempt_number"))
        return True

    value, record = _completed(case, callback=retry, success=False)
    assert value.execution.state == "QUEUED" and value.execution.attempt_number == 2
    assert (
        not RayTaskExecution.objects.alias(pending=cleanup.job_cleanup_blocked_expression())
        .filter(pending=False)
        .exists()
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=value.execution.pk).update(
            state="RUNNING", execution_generation=2
        )
    _close(case, record)
    assert (
        RayTaskExecution.objects.alias(pending=cleanup.job_cleanup_blocked_expression())
        .filter(pending=False)
        .exists()
    )


@pytest.mark.parametrize("model", [RayCohortJobCleanup, RayTaskCohortClaim, RayTaskExecution])
def test_open_cleanup_cannot_be_deleted_indirectly_or_directly(case, model):
    _completed(case)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)}")
    assert RayCohortJobCleanup.objects.get().state == "OPEN"


def test_closed_cleanup_can_follow_normal_claim_retention(case):
    _value, record = _completed(case)
    _close(case, record)
    RayTaskCohortClaim.objects.get().delete()
    assert not RayCohortJobCleanup.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"request_digest": "sha256:" + "a" * 64},
        {"expectation_json": "{}"},
        {"queue_name": "another"},
        {"completion_digest": "sha256:" + "b" * 64},
        {"created_at": None},
    ],
)
def test_cleanup_original_snapshots_are_immutable(case, changes):
    _completed(case)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayCohortJobCleanup.objects.update(**changes)


@pytest.mark.parametrize("offset", [-1, 1])
def test_cleanup_observation_must_be_after_obligation_and_not_future(case, offset):
    _value, record = _completed(case)
    changes = (
        {"inspection_began_at": case.now - timedelta(seconds=1)}
        if offset < 0
        else {"observed_at": case.now + timedelta(seconds=1)}
    )
    with pytest.raises(cleanup.CohortJobCleanupError, match="clock_regression"):
        _close(case, record, **changes)
    assert RayCohortJobCleanup.objects.get().state == "OPEN"


def test_stale_revision_or_crossed_expectation_cannot_close(case):
    _value, record = _completed(case)
    with pytest.raises(cleanup.CohortJobCleanupError, match="changed"):
        _close(case, replace(record, revision=record.revision + 1))
    crossed = replace(record.expectation, contract_digest="sha256:" + "a" * 64)
    with pytest.raises(cleanup.CohortJobCleanupError, match="inspection_unconfirmed"):
        _close(
            case,
            record,
            inspection=CohortJobExecutionInspection(crossed, "SUCCEEDED", False, "01000000"),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("cleanup_id", True),
        ("execution_id", True),
        ("revision", True),
        ("revision", 0),
        ("revision", 1.0),
        ("queue_name", "\x00"),
        ("queue_name", " " * 101),
        ("state", None),
        ("completion_digest", "sha256:" + "A" * 64),
        ("expectation_digest", None),
        ("missing_expectation_reason", "missing_expectation"),
    ],
)
def test_malformed_retained_snapshot_is_refused_before_database_locks(
    case, field, value, django_assert_num_queries
):
    _value, record = _completed(case)
    with (
        django_assert_num_queries(0),
        pytest.raises(cleanup.CohortJobCleanupError, match="invalid"),
    ):
        _close(case, replace(record, **{field: value}))
    assert RayCohortJobCleanup.objects.get().state == "OPEN"


def test_close_rechecks_clock_after_final_locks_and_rolls_back(case, monkeypatch):
    _value, record = _completed(case)
    times = iter((case.now, case.now - timedelta(microseconds=1)))
    monkeypatch.setattr(cleanup, "_clock", lambda: next(times))
    with pytest.raises(cleanup.CohortJobCleanupError, match="clock_regression"):
        _close(case, record)
    assert cleanup.cleanup_record(RayCohortJobCleanup.objects.get()) == record


@pytest.mark.parametrize(
    "field", ["jobs_endpoint", "request_digest", "contract_digest", "row-contract"]
)
def test_close_independently_revalidates_canonical_persisted_snapshot_against_claim(
    case, monkeypatch, field
):
    original_save = RayCohortJobCleanup.save

    def crossed_insert(row, *args, **kwargs):
        # Simulate canonical but incorrectly correlated initial persisted data.
        # Immutable UPDATE guards remain installed and are not bypassed.
        if row._state.adding:
            expected = cleanup.decode_cohort_job_cleanup_expectation(row.expectation_json)
            if field == "row-contract":
                row.contract_digest = "sha256:" + "a" * 64
            else:
                expected = replace(
                    expected,
                    **{
                        field: "http://other:8265"
                        if field == "jobs_endpoint"
                        else "sha256:" + "a" * 64
                    },
                )
                row.expectation_json = cleanup.encode_cohort_job_cleanup_expectation(expected)
                row.expectation_digest = cleanup.cohort_job_cleanup_expectation_digest(expected)
        return original_save(row, *args, **kwargs)

    monkeypatch.setattr(RayCohortJobCleanup, "save", crossed_insert)
    _value, retained = _completed(case)
    reason = "completion_unconfirmed" if field == "row-contract" else "expectation_changed"
    with pytest.raises(cleanup.CohortJobCleanupError, match=reason):
        _close(case, retained)
    assert RayCohortJobCleanup.objects.get().state == "OPEN"
    assert RayTaskExecution.objects.get().state == "SUCCEEDED"


def test_expectation_codec_is_canonical_bounded_and_duplicate_safe(case):
    _value, record = _completed(case)
    expected = record.expectation
    assert (
        cleanup.decode_cohort_job_cleanup_expectation(
            record.expectation_json, expected_digest=record.expectation_digest
        )
        == expected
    )
    data = json.loads(record.expectation_json)
    invalid = [
        record.expectation_json + " ",
        "{" + record.expectation_json[1:-1] + ',"request_digest":"duplicate"}',
        "[" * 20000,
    ]
    for field in ("request_digest", "identity", "request_size_bytes"):
        changed = dict(data)
        changed.pop(field)
        invalid.append(json.dumps(changed))
    for serialized in invalid:
        with pytest.raises(cleanup.CohortJobCleanupError, match="invalid_expectation"):
            cleanup.decode_cohort_job_cleanup_expectation(serialized)
    with pytest.raises(cleanup.CohortJobCleanupError, match="invalid_expectation"):
        cleanup.decode_cohort_job_cleanup_expectation(
            record.expectation_json, expected_digest="sha256:" + "a" * 64
        )


def test_expectation_rejects_subclass_and_oversize_without_provider_details(case, monkeypatch):
    _value, record = _completed(case)

    class String(str):
        pass

    with pytest.raises(cleanup.CohortJobCleanupError, match="invalid_expectation"):
        cleanup.encode_cohort_job_cleanup_expectation(
            replace(record.expectation, jobs_endpoint=String(record.expectation.jobs_endpoint))
        )
    monkeypatch.setattr(cleanup, "EXPECTATION_MAX_BYTES", 16)
    with pytest.raises(cleanup.CohortJobCleanupError, match="invalid_expectation"):
        cleanup.encode_cohort_job_cleanup_expectation(record.expectation)


@pytest.mark.parametrize("mode", ["stale-claim", "crossed-expectation", "persistence"])
def test_record_refusal_rolls_back_before_authentic_resolution(case, monkeypatch, mode):
    value = _started(case, "ray_job")
    _stored_request_reference(value)
    with transaction.atomic():
        storage._lease(case.owner, case.now, using="default")
        current = RayTaskExecution.objects.select_for_update().get(pk=value.execution.pk)
        row = RayTaskCohortClaim.objects.select_for_update().get(pk=value.claim.claim_id)
        retained = storage._record(row)
        expected = completion._cleanup_expectation(current, retained, value)
        assert expected is not None
        if mode == "stale-claim":
            retained = replace(retained, revision=retained.revision + 1)
        elif mode == "crossed-expectation":
            expected = replace(expected, request_digest="sha256:" + "a" * 64)
        else:

            def fail(*_args, **_kwargs):
                raise DatabaseError("secret provider detail")

            monkeypatch.setattr(RayCohortJobCleanup, "save", fail)
        reason = {
            "stale-claim": "claim_changed",
            "crossed-expectation": "expectation_changed",
            "persistence": "persistence_refused",
        }[mode]
        with pytest.raises(cleanup.CohortJobCleanupError, match=reason), transaction.atomic():
            cleanup.record_cohort_job_cleanup_locked(
                current,
                retained,
                expected,
                completion_evidence_digest="sha256:" + "b" * 64,
                now=case.now,
            )
    assert not RayCohortJobCleanup.objects.exists()
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


def test_obligation_cannot_be_closed_before_its_authentic_resolution_commits(case):
    value = _started(case, "ray_job")
    with transaction.atomic():
        storage._lease(case.owner, case.now, using="default")
        current = RayTaskExecution.objects.select_for_update().get(pk=value.execution.pk)
        row = RayTaskCohortClaim.objects.select_for_update().get(pk=value.claim.claim_id)
        pending = cleanup.record_cohort_job_cleanup_locked(
            current,
            storage._record(row),
            None,
            completion_evidence_digest="sha256:" + "a" * 64,
            now=case.now,
        )
        with pytest.raises(cleanup.CohortJobCleanupError, match="completion_unconfirmed"):
            cleanup._locked_row(pending, using="default")
        transaction.set_rollback(True)
    assert not RayCohortJobCleanup.objects.exists()


def test_expired_active_cleanup_owner_is_stopped_only_with_qualified_transfer(case):
    from tests.integration.test_cohort_cleanup_recovery import _recover
    from tests.integration.test_cohort_recovery import _qualify_adopter

    value, before = _completed(case)
    item, original_lease, _original_owner = _qualify_adopter(case, value, stop_source=False)
    original_lease.last_heartbeat_at = case.now - timedelta(days=1)
    original_lease.save(update_fields=["last_heartbeat_at"])
    (recovered,) = _recover(case, value, item)
    original_lease.refresh_from_db()
    assert not original_lease.is_active and original_lease.stopped_at == case.now
    assert recovered.cleanup.owner == case.owner != before.owner
    assert recovered.cleanup.revision == before.revision + 1


@pytest.mark.postgresql
@pytest.mark.parametrize("ledger_database", ["postgresql"], indirect=True)
def test_postgresql_only_one_exact_terminal_observation_wins_cleanup_revision(case):
    _value, record = _completed(case)
    started = Barrier(2)

    def close():
        connection.close()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '3s'")
            started.wait(timeout=5)
            try:
                return _close(case, record).state
            except cleanup.CohortJobCleanupError as exc:
                # A timeout/provider error is not successful CAS rejection.
                assert exc.reason == "changed"
                return "changed"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(close) for _ in range(2)]
        assert sorted(result.result(timeout=8) for result in outcomes) == ["CLOSED", "changed"]
    assert RayCohortJobCleanup.objects.get().revision == record.revision + 1


def test_jobs_claim_cannot_resolve_without_durable_cleanup_obligation(case):
    value = _started(case, "ray_job")
    with pytest.raises(storage.CohortClaimStorageError), transaction.atomic():
        storage.resolve_cohort_claim(
            case.owner,
            value.claim.claim_id,
            expected_identity=value.claim.facts.identity,
            expected_revision=value.claim.revision,
            kind=CohortResolutionKind.APPLICATION_COMPLETED,
            evidence_digest="sha256:" + "a" * 64,
            now=case.now,
        )
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


def test_cleanup_requires_transaction_boundaries_and_rolls_back_failed_close(case, monkeypatch):
    _value, record = _completed(case)
    with (
        transaction.atomic(),
        pytest.raises(cleanup.CohortJobCleanupError, match="transaction_open"),
    ):
        _close(case, record)
    with pytest.raises(cleanup.CohortJobCleanupError, match="transaction_required"):
        cleanup.check_no_pending_job_cleanup(RayTaskExecution.objects.get())
    with monkeypatch.context() as patch:

        def fail(*_args, **_kwargs):
            raise DatabaseError("secret provider detail")

        patch.setattr(RayCohortJobCleanup, "save", fail)
        with pytest.raises(cleanup.CohortJobCleanupError, match="persistence_refused"):
            _close(case, record)
    assert RayCohortJobCleanup.objects.get().state == "OPEN"


def test_reverse_refuses_retained_cleanup_history_inside_outer_transaction(case):
    _completed(case)
    migration = importlib.import_module("django_ray.migrations.0033_cohort_job_cleanup")
    with pytest.raises(RuntimeError, match="retained history"), transaction.atomic():
        migration._remove(apps, connection.schema_editor(atomic=False))


@pytest.mark.parametrize("family", ["ray_job", "ray_core", "sync"])
@pytest.mark.usefixtures("closed_preactivation_protocol_schema")
def test_historical0032_completed_jobs_refused_without_reinterpreting_other_history(case, family):
    value = _started(case, family)
    previous = [("django_ray", "0032_maintenance_controls")]
    current = [("django_ray", "0033_cohort_job_cleanup")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    historical = executor.loader.project_state(previous).apps
    try:
        with transaction.atomic():
            storage.resolve_cohort_claim(
                case.owner,
                value.claim.claim_id,
                expected_identity=value.claim.facts.identity,
                expected_revision=value.claim.revision,
                kind=CohortResolutionKind.APPLICATION_COMPLETED,
                evidence_digest="sha256:" + "a" * 64,
                now=case.now,
            )
            historical.get_model("django_ray", "RayTaskExecution").objects.filter(
                pk=case.task.pk
            ).update(state="SUCCEEDED")
        if family == "ray_job":
            with pytest.raises(RuntimeError, match="pre-release resolved Jobs history"):
                MigrationExecutor(connection).migrate(current)
            assert (
                historical.get_model("django_ray", "RayTaskCohortClaim")
                .objects.get(pk=value.claim.claim_id)
                .disposition
                == "RESOLVED"
            )
        else:
            MigrationExecutor(connection).migrate(current)
            assert RayTaskCohortClaim.objects.get(pk=value.claim.claim_id).disposition == "RESOLVED"
            assert not RayCohortJobCleanup.objects.exists()
    finally:
        # This stopped historical test owns the exact terminal fixture claim.
        # Delete through the historical model while0033 may still be absent.
        historical.get_model("django_ray", "RayTaskCohortClaim").objects.filter(
            pk=value.claim.claim_id
        ).delete()
        MigrationExecutor(connection).migrate([("django_ray", "0035_activate_current_cohort")])


def test_historical_protocol1_result_is_preserved_without_cleanup_backfill():
    previous = [("django_ray", "0032_maintenance_controls")]
    current = [("django_ray", "0033_cohort_job_cleanup")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    historical = executor.loader.project_state(previous).apps
    task = historical.get_model("django_ray", "RayTaskExecution")
    try:
        value = task.objects.create(
            task_id=str(uuid4()),
            callable_path="tests.tasks.add",
            execution_protocol_version=1,
            state="SUCCEEDED",
            completion_data='{"historical":"retained"}',
        )
        before = task.objects.values().get(pk=value.pk)
        MigrationExecutor(connection).migrate(current)
        assert RayTaskExecution.objects.values().get(pk=value.pk) == before
        assert not RayCohortJobCleanup.objects.exists()
    finally:
        MigrationExecutor(connection).migrate([("django_ray", "0035_activate_current_cohort")])
