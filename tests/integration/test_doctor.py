"""Scalar-only consistent SQLite diagnostics; no native runtime or storage I/O."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Event

import pytest
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, connections, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.test.utils import CaptureQueriesContext

from django_ray import doctor, maintenance
from django_ray.models import RayTaskExecution, TaskWorkerLease
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.coordination import record_ray_target_attestation
from tests.integration.test_cohort_claim_storage import (
    _claim,
    _hold,
    _ray_arguments,
)
from tests.integration.test_cohort_claim_storage import (
    case as case,
)
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_completion import (
    isolated_completion_controls as isolated_completion_controls,
)
from tests.integration.test_cohort_job_cleanup import _close, _completed
from tests.integration.test_cohort_probe_challenges import (
    NOW as PROBE_NOW,
)
from tests.integration.test_cohort_probe_challenges import (
    _issue,
    _lease,
    _policy,
    _target,
)
from tests.integration.test_maintenance_controls import _complete, _quarantine, _request
from tests.integration.test_ray_target_coordination import _attestation

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 9, 12, tzinfo=UTC)


def _cohort(report: doctor.DoctorReport):
    assert report.cohort is not None
    return report.cohort


def test_empty_database_state_is_never_healthy_or_drained():
    report = doctor.build_doctor(observed_at=NOW)
    value = doctor.doctor_to_dict(report)
    assert value["database"]["connectivity"] == "reachable"
    assert value["database"]["migrations"]["status"] == "current"
    assert value["protocol"]["schema"] == "django-ray.protocol-status"
    assert value["protocol"]["schema_version"] == 1
    assert value["protocol"]["queue_capacity_attested"] is False
    assert value["cohort"]["claims"]["total"] == 0
    assert (
        value["drain_status"] == value["upgrade_status"] == value["rollback_status"] == "unverified"
    )
    assert "remote_work_quiescence_and_cleanup" in value["unverified"]
    assert "drain_completion_and_remote_retirement" in value["unverified"]
    assert value["scope"] == "selected_database"
    for section in ("quarantine", "worker_retirement", "job_cleanup"):
        assert all(count == 0 for count in value["cohort"][section].values())


def test_observation_is_read_only_and_does_not_materialize_sensitive_blobs(monkeypatch):
    task = RayTaskExecution.objects.create(
        task_id="doctor-task",
        callable_path="must.never.import.this",
        runtime_env_json="secret-env",
        args_json="[]",
        kwargs_json="{}",
        queue_name="token=secret",
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    with CaptureQueriesContext(connection) as captured:
        report = doctor.build_doctor(observed_at=NOW)
    assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
    forbidden = (
        "args_json",
        "kwargs_json",
        "runtime_env_json",
        "facts_json",
        "receipt_json",
        "expectation_json",
        "attestation_json",
        "nonce_digest",
        "callable_path",
    )
    for query in captured.captured_queries:
        sql = query["sql"].upper().strip()
        assert sql.startswith(("SELECT", "WITH", "BEGIN", "COMMIT", "SET TRANSACTION")), sql
        assert "FOR UPDATE" not in sql
        for column in forbidden:
            assert f'"{column.upper()}"' not in sql
    assert "must.never.import.this" not in doctor.render_doctor_json(report)


def test_future_migration_stops_before_protocol_or_cohort_reads(monkeypatch):
    recorder = MigrationRecorder(connection)
    recorder.record_applied("django_ray", "9999_token=private-value")
    monkeypatch.setattr(
        doctor.protocol_status,
        "_build_protocol_status_observation",
        lambda **kwargs: pytest.fail("model read after future schema"),
    )
    try:
        report = doctor.build_doctor(observed_at=NOW)
        assert report.database["migrations"]["status"] == "unknown_applied"
        assert report.database["migrations"]["unknown_applied_records"] == 1
        assert report.protocol is report.cohort is None
        assert "private-value" not in doctor.render_doctor_json(report)
    finally:
        recorder.record_unapplied("django_ray", "9999_token=private-value")


def test_unmigrated_sqlite_database_produces_report_without_model_queries(django_db_blocker):
    alias = "doctor_empty"
    settings = dict(connection.settings_dict, NAME=":memory:")
    connections.databases[alias] = settings
    try:
        with django_db_blocker.unblock():
            empty = connections[alias]
            with CaptureQueriesContext(empty) as queries:
                report = doctor.build_doctor(using=alias, observed_at=NOW)
        assert report.database["connectivity"] == "reachable"
        assert report.database["migrations"]["recorder_present"] is False
        assert report.database["migrations"]["missing_count"] > 0
        assert report.protocol is report.cohort is None
        assert all("django_ray_" not in query["sql"] for query in queries.captured_queries)
    finally:
        connections[alias].close()
        del connections[alias]
        connections.databases.pop(alias)


def test_current_held_owner_counts_use_exact_incarnation(case):
    record = _hold(case, _claim(case))
    report = doctor.build_doctor(observed_at=case.now)
    assert _cohort(report)["claims"]["current_held"] == 1
    assert _cohort(report)["claims"]["current_unresolved_without_exact_live_owner"] == 0
    # Lease cleanup preserves immutable claim provenance. A new lease with the
    # same worker ID must not look like ownership of that held generation.
    case.lease.delete()
    TaskWorkerLease.objects.create(
        worker_id=case.owner.worker_id,
        hostname="replacement",
        pid=999,
        started_at=case.now,
        last_heartbeat_at=case.now,
        capability_schema_version=1,
        django_ray_version="0.5.0",
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    changed = doctor.build_doctor(observed_at=case.now)
    assert _cohort(changed)["claims"]["current_unresolved_without_exact_live_owner"] == 1
    assert any(blocker["code"] == "current_held_claims" for blocker in changed.blockers)
    assert str(record.facts.worker_lease_started_at) not in doctor.render_doctor_json(changed)


@pytest.mark.parametrize("field", ["last_heartbeat_at", "started_at"])
def test_future_owner_clock_is_unavailable_and_excluded_from_packages(case, field):
    _hold(case, _claim(case))
    TaskWorkerLease.objects.filter(pk=case.lease.pk).update(
        **{field: case.now + timedelta(seconds=1)}
    )
    report = doctor.build_doctor(observed_at=case.now)
    assert _cohort(report)["claims"]["current_unresolved_without_exact_live_owner"] == 1
    assert _cohort(report)["manager_packages"]["total_leases"] == 0
    # Existing protocol-status semantics are preserved rather than silently
    # relabelled as this doctor's stricter exact-incarnation observation.
    assert report.protocol is not None
    assert report.protocol.leases.heartbeat_live == 1


def test_future_capability_heartbeat_is_not_live(case):
    _ray_arguments(case, RayRunnerFamily.RAY_CORE)
    TaskWorkerLease.objects.filter(pk=case.lease.pk).update(
        last_heartbeat_at=case.now + timedelta(seconds=1)
    )
    counts = _cohort(doctor.build_doctor(observed_at=case.now))["capabilities"]
    assert counts["total"] == counts["stale_or_crossed_lease"] == 1
    assert counts["exact_heartbeat_live_lease"] == 0


def test_probe_expiry_and_draining_policy_do_not_imply_remote_cleanup():
    _target()
    lease, identity = _lease()
    _issue(identity)
    TaskWorkerLease.objects.filter(pk=lease.pk).update(
        last_heartbeat_at=PROBE_NOW + timedelta(seconds=1)
    )
    assert (
        _cohort(doctor.build_doctor(observed_at=PROBE_NOW))["probes"][
            "pending_without_exact_live_owner"
        ]
        == 1
    )
    TaskWorkerLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=PROBE_NOW)
    before = doctor.build_doctor(observed_at=PROBE_NOW)
    assert _cohort(before)["policies"]["draining"] == 1
    assert _cohort(before)["policies"]["proof_missing"] == 1
    assert _cohort(before)["probes"]["pending"] == 1
    later = doctor.build_doctor(observed_at=PROBE_NOW + timedelta(seconds=601))
    assert _cohort(later)["probes"]["pending_expired"] == 1
    assert _cohort(later)["probes"]["pending_without_exact_live_owner"] == 1
    assert doctor.doctor_to_dict(later)["drain_status"] == "unverified"
    assert "session_primary" not in doctor.render_doctor_json(later)


def test_only_latest_policy_proof_window_is_counted():
    from django_ray.models import RayTarget

    expectation = _target()
    proof = _attestation(
        expectation,
        observed_at=PROBE_NOW - timedelta(seconds=1),
        expires_at=PROBE_NOW + timedelta(seconds=10),
    )
    record_ray_target_attestation(
        expectation.target_key,
        proof,
        expected_policy_revision=1,
        expected_attestation_revision=0,
        now=PROBE_NOW,
    )
    report = doctor.build_doctor(observed_at=PROBE_NOW)
    assert _cohort(report)["policies"]["proof_within_recorded_window"] == 1
    assert (
        "canonical_proof_and_endpoint_qualification" in doctor.doctor_to_dict(report)["unverified"]
    )
    assert (
        _cohort(doctor.build_doctor(observed_at=PROBE_NOW - timedelta(seconds=2)))["policies"][
            "proof_future"
        ]
        == 1
    )
    assert (
        _cohort(doctor.build_doctor(observed_at=PROBE_NOW + timedelta(seconds=10)))["policies"][
            "proof_expired"
        ]
        == 1
    )
    _policy(RayTarget.objects.get(), replace(expectation, policy_revision=2), "active")
    current = _cohort(doctor.build_doctor(observed_at=PROBE_NOW))["policies"]
    assert current["total"] == current["active"] == current["proof_missing"] == 1
    assert current["draining"] == current["proof_within_recorded_window"] == 0


def test_failed_cohort_read_discards_partial_protocol_observation(monkeypatch):
    def unavailable(**kwargs):
        raise DatabaseError("private-host token=secret")

    monkeypatch.setattr(doctor, "_cohort_observation", unavailable)
    report = doctor.build_doctor(observed_at=NOW)
    assert report.database["connectivity"] == "reachable"
    assert report.protocol is report.cohort is None
    assert report.blockers[0]["code"] == "database_observation_unavailable"
    assert "private-host" not in doctor.render_doctor_json(report)


def test_displayed_package_names_are_bounded_in_sql_and_terminal_safe():
    for index, name in enumerate(("token=secret", "line\n\r\x1b[31m", "x" * 100_000)):
        TaskWorkerLease.objects.create(
            worker_id=f"package-{index}",
            hostname="host",
            pid=index + 1,
            started_at=NOW,
            last_heartbeat_at=NOW,
            capability_schema_version=1,
            django_ray_version=name,
            min_supported_execution_protocol_version=3,
            max_supported_execution_protocol_version=3,
            legacy_admission_token=None,
        )
    report = doctor.build_doctor(observed_at=NOW)
    names = {row["package"] for row in _cohort(report)["manager_packages"]["groups"]}
    assert names == {"[REDACTED]", "line\n\n", "[OVERSIZED]"}
    rendered = doctor.render_doctor_text(report)
    assert "token=secret" not in rendered
    assert "\x1b" not in rendered
    assert "x" * 129 not in rendered


def test_combined_groups_are_redacted_bounded_and_deterministic():
    for index in range(doctor.DOCTOR_GROUP_LIMIT + 1):
        TaskWorkerLease.objects.create(
            worker_id=f"bounded-{index}",
            hostname="host",
            pid=index + 1,
            started_at=NOW,
            last_heartbeat_at=NOW,
            capability_schema_version=1,
            django_ray_version=f"{index:03d}" + "🙂" * 125,
            min_supported_execution_protocol_version=3,
            max_supported_execution_protocol_version=3,
            legacy_admission_token=None,
        )
        RayTaskExecution.objects.create(
            task_id=f"bounded-{index}",
            callable_path="never.import",
            queue_name=f"{index:03d}" + "🙂" * 97,
            execution_protocol_version=3,
        )
    TaskWorkerLease.objects.create(
        worker_id="redacted",
        hostname="host",
        pid=999,
        started_at=NOW,
        last_heartbeat_at=NOW,
        capability_schema_version=1,
        django_ray_version="token=secret",
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    report = doctor.build_doctor(observed_at=NOW)
    repeated = doctor.build_doctor(observed_at=NOW)
    assert report == repeated
    packages = _cohort(report)["manager_packages"]
    assert len(packages["groups"]) <= doctor.DOCTOR_GROUP_LIMIT
    assert len(packages["groups"]) + packages["omitted_groups"] == packages["total_groups"]
    assert (
        sum(row["count"] for row in packages["groups"]) + packages["omitted_leases"]
        == packages["total_leases"]
    )
    for render in (doctor.render_doctor_json, doctor.render_doctor_text):
        value = render(report)
        assert len((value + "\n").encode()) <= doctor.DOCTOR_OUTPUT_MAX_BYTES
        assert "token=secret" not in value
    assert report.protocol is not None
    assert report.protocol.nonterminal_work.omitted_groups > 0
    assert (
        sum(group.count for group in report.protocol.nonterminal_work.groups)
        + report.protocol.nonterminal_work.omitted_tasks
        == 65
    )


@pytest.mark.parametrize("arguments", [(), ("--json",)])
def test_public_command_output(arguments):
    output = StringIO()
    call_command("django_ray_doctor", *arguments, stdout=output)
    value = output.getvalue()
    assert len(value.encode()) <= doctor.DOCTOR_OUTPUT_MAX_BYTES
    if arguments:
        assert json.loads(value)["schema"] == doctor.DOCTOR_SCHEMA
    else:
        assert "Drain, upgrade and rollback: unverified" in value


def test_rejects_existing_transaction():
    with transaction.atomic(), pytest.raises(doctor.DoctorError, match="outermost"):
        doctor.build_doctor(observed_at=NOW)


def test_quarantine_latest_decision_is_exact_and_history_is_retained(case):
    case.monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    case.task = RayTaskExecution.objects.create(
        task_id="doctor-quarantine", callable_path="never.import", created_at=case.now
    )
    _quarantine(case.task, actor="operator-private", reason="private-reason")
    _quarantine(case.task, revision=1, quarantined=False)
    _quarantine(case.task, revision=2)
    report = doctor.build_doctor(observed_at=case.now)
    counts = _cohort(report)["quarantine"]
    assert counts["total"] == counts["current_quarantined"] == 1
    assert counts["current_nonterminal_quarantined"] == 1
    assert counts["current_released"] == 0
    assert "operator-private" not in doctor.render_doctor_json(report)
    assert "private-reason" not in doctor.render_doctor_json(report)
    _quarantine(case.task, revision=3, quarantined=False)
    released = _cohort(doctor.build_doctor(observed_at=case.now))["quarantine"]
    assert released["current_quarantined"] == 0 and released["current_released"] == 1
    old_pk = case.task.pk
    case.task.delete()
    replacement = RayTaskExecution.objects.create(
        pk=old_pk, task_id="replacement", callable_path="never.import"
    )
    retained = _cohort(doctor.build_doctor(observed_at=case.now))["quarantine"]
    assert retained["total"] == retained["retained_without_current_identity"] == 1
    assert retained["current_quarantined"] == retained["current_released"] == 0
    replacement.delete()


@pytest.mark.parametrize("retired", [False, True])
def test_retirement_latest_decision_never_applies_to_reused_worker_id(case, retired):
    case.monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    _request(case.owner)
    if retired:
        _complete(case.owner)
    report = doctor.build_doctor(observed_at=case.now)
    counts = _cohort(report)["worker_retirement"]
    assert counts["total"] == 1
    assert counts["requested_with_exact_live_lease"] == int(not retired)
    assert counts["retired_with_exact_inactive_lease"] == int(retired)
    case.lease.delete()
    TaskWorkerLease.objects.create(
        worker_id=case.owner.worker_id,
        hostname=case.owner.hostname,
        pid=case.owner.pid,
        started_at=case.owner.started_at + timedelta(seconds=1),
        last_heartbeat_at=case.now,
        capability_schema_version=1,
        django_ray_version="0.5.0",
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    retained = _cohort(doctor.build_doctor(observed_at=case.now))["worker_retirement"]
    assert retained["retained_without_exact_lease"] == 1
    assert retained["requested_without_exact_live_lease"] == int(not retired)
    assert retained["retired_without_exact_inactive_lease"] == int(retired)
    assert doctor.doctor_to_dict(report)["drain_status"] == "unverified"


def test_future_control_decisions_are_not_reported_as_current(case):
    case.monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    _hold(case, _claim(case))
    case.task.refresh_from_db()
    _request(case.owner)
    _quarantine(case.task)
    counts = _cohort(doctor.build_doctor(observed_at=case.now - timedelta(microseconds=1)))
    assert counts["worker_retirement"]["future_decisions"] == 1
    assert counts["worker_retirement"]["requested"] == 0
    assert counts["quarantine"]["future_decisions"] == 1
    assert counts["quarantine"]["current_quarantined"] == 0


@pytest.mark.parametrize("reference", [False, True])
def test_open_cleanup_survives_terminal_result_without_materializing_expectation(case, reference):
    _completed(case, reference=reference)
    with CaptureQueriesContext(connection) as captured:
        report = doctor.build_doctor(observed_at=case.now)
    counts = _cohort(report)["job_cleanup"]
    assert counts["open"] == counts["open_for_terminal_task"] == 1
    assert counts["open_uninspectable"] == int(not reference)
    assert counts["open_without_exact_live_owner"] == 0
    assert _cohort(report)["claims"]["unresolved"] == 0
    assert any(item["code"] == "open_jobs_cleanup" for item in report.blockers)
    for query in captured.captured_queries:
        assert '"expectation_json"' not in query["sql"]
        assert '"facts_json"' not in query["sql"]
    assert doctor.doctor_to_dict(report)["drain_status"] == "unverified"


def test_cleanup_prior_attempt_and_unavailable_owner_remain_visible_until_closed(case):
    def retry(task, _decoded, *, retry_admitted):
        assert retry_admitted
        task.state = "QUEUED"
        task.attempt_number += 1
        task.save(update_fields=("state", "attempt_number"))
        return True

    _value, record = _completed(case, callback=retry, success=False)
    TaskWorkerLease.objects.filter(pk=case.lease.pk).update(
        last_heartbeat_at=case.now + timedelta(seconds=1)
    )
    counts = _cohort(doctor.build_doctor(observed_at=case.now))["job_cleanup"]
    assert counts["open"] == counts["open_for_prior_attempt_or_generation"] == 1
    assert counts["open_without_exact_live_owner"] == 1
    assert counts["open_for_terminal_task"] == 0
    TaskWorkerLease.objects.filter(pk=case.lease.pk).update(last_heartbeat_at=case.now)
    _close(case, record)
    report = doctor.build_doctor(observed_at=case.now)
    assert _cohort(report)["job_cleanup"]["closed_records"] == 1
    assert _cohort(report)["job_cleanup"]["open"] == 0
    assert not any(item["code"] == "open_jobs_cleanup" for item in report.blockers)
    assert doctor.doctor_to_dict(report)["drain_status"] == "unverified"


def test_new_summaries_remain_on_explicit_database_despite_read_router(case, monkeypatch):
    from django.db import router

    _completed(case, reference=False)
    case.monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    case.task.refresh_from_db()
    _quarantine(case.task)
    _request(case.owner)
    with monkeypatch.context() as routed:
        routed.setattr(
            router, "db_for_read", lambda *args, **kwargs: pytest.fail("unqualified routed read")
        )
        report = doctor.build_doctor(using="default", observed_at=case.now)
    counts = _cohort(report)
    assert counts["quarantine"]["current_quarantined"] == 1
    assert counts["worker_retirement"]["requested"] == 1
    assert counts["job_cleanup"]["open_uninspectable"] == 1


def test_selected_database_does_not_mix_default_control_or_cleanup_rows(
    case, tmp_path, django_db_blocker
):
    from django.db.migrations.executor import MigrationExecutor

    if connection.vendor != "sqlite":
        pytest.skip("uses an independently migrated SQLite observation database")
    _completed(case, reference=False)
    case.monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    case.task.refresh_from_db()
    _quarantine(case.task)
    _request(case.owner)
    alias = "doctor_selected"
    connections.databases[alias] = dict(
        connection.settings_dict, NAME=str(tmp_path / "doctor.sqlite3")
    )
    try:
        with django_db_blocker.unblock():
            selected = connections[alias]
            MigrationExecutor(selected).migrate([("django_ray", "0035_activate_current_cohort")])
            with CaptureQueriesContext(connection) as other_queries:
                report = doctor.build_doctor(using=alias, observed_at=case.now)
        assert not other_queries.captured_queries
        for section in ("claims", "quarantine", "worker_retirement", "job_cleanup"):
            assert _cohort(report)[section]["total"] == 0
        assert doctor.doctor_to_dict(report)["drain_status"] == "unverified"
    finally:
        connections[alias].close()
        del connections[alias]
        connections.databases.pop(alias)


@pytest.mark.postgresql
def test_postgresql_control_decisions_share_the_protocol_snapshot(case, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("requires the hosted PostgreSQL coordination database")
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    begin_write = Event()
    original = doctor._cohort_observation

    def writer():
        close_old_connections()
        try:
            assert begin_write.wait(timeout=5)
            _quarantine(case.task)
            _request(case.owner)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        completed = pool.submit(writer)

        def interleaved(**kwargs):
            begin_write.set()
            completed.result(timeout=10)
            return original(**kwargs)

        monkeypatch.setattr(doctor, "_cohort_observation", interleaved)
        observed = _cohort(doctor.build_doctor(observed_at=case.now))
        assert observed["quarantine"]["total"] == observed["worker_retirement"]["total"] == 0
    monkeypatch.setattr(doctor, "_cohort_observation", original)
    later = _cohort(doctor.build_doctor(observed_at=case.now))
    assert later["quarantine"]["current_quarantined"] == 1
    assert later["worker_retirement"]["requested_with_exact_live_lease"] == 1


@pytest.mark.postgresql
def test_postgresql_protocol_and_cohort_share_one_read_only_snapshot(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("requires the hosted PostgreSQL coordination database")
    begin_write = Event()
    original = doctor._cohort_observation

    def writer():
        close_old_connections()
        try:
            assert begin_write.wait(timeout=10)
            _target()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        completed = pool.submit(writer)

        def interleaved(**kwargs):
            begin_write.set()
            completed.result(timeout=10)
            return original(**kwargs)

        monkeypatch.setattr(doctor, "_cohort_observation", interleaved)
        report = doctor.build_doctor(observed_at=PROBE_NOW)
        assert _cohort(report)["policies"]["total"] == 0
    monkeypatch.setattr(doctor, "_cohort_observation", original)
    assert _cohort(doctor.build_doctor(observed_at=PROBE_NOW))["policies"]["total"] == 1
