"""Database bounds and one-time probe receipt storage; no remote proof is claimed."""

from __future__ import annotations

import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
from django.apps import apps
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from django_ray.models import (
    RAY_TARGET_PROBE_JOB_RECEIPT_MAX_BYTES,
    RayTargetProbeChallenge,
    RayTargetProbeJobReceipt,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)
from django_ray.runtime.cohort_job import (
    COHORT_PROBE_JOB_MAX_BYTES,
    CohortProbeJobLease,
    CohortProbeJobRequest,
    encode_probe_job_request,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.target import cohort_job_receipt_storage as storage
from django_ray.target.attestation import (
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
)
from django_ray.target.cohort_job_receipt import (
    COHORT_JOB_RECEIPT_MAX_BYTES,
    CohortJobReceipt,
    cohort_job_receipt_digest,
    decode_cohort_job_receipt,
    encode_cohort_job_receipt,
)
from django_ray.target.cohort_job_receipt_storage import (
    CohortJobStorageError,
    read_cohort_job_reservation,
    reserve_cohort_job_probe,
    write_cohort_job_receipt,
)
from django_ray.target.cohort_probe import derive_cohort_target_key
from django_ray.target.cohort_probe_challenges import (
    ProbeChallengeError,
    consume_ray_target_probe_challenge,
    replace_ray_target_probe_challenge,
)
from tests.integration.test_cohort_probe_challenges import NOW, _issue, _lease, _target

pytestmark = pytest.mark.django_db(transaction=True)
LATEST = [("django_ray", "0030_cohort_claims")]
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def reservation():
    lease, identity = _lease()
    issued = _issue(identity, runner_family=RayRunnerFamily.RAY_JOB)
    slot = issued.receipt
    request = CohortProbeJobRequest(
        challenge_id=slot.challenge_id,
        challenge_revision=slot.revision,
        lease=CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ),
        configuration_digest=slot.configuration_digest,
        target_key=None,
        runner_family=RayRunnerFamily.RAY_JOB,
        expected_package_version="0.5.0",
        expected_runtime=RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14),
        expected_cluster_session=None,
        expected_target_policy_id=None,
        policy_revision=1,
        issued_at=slot.issued_at,
        expires_at=slot.expires_at,
    )
    values = {
        "challenge_id": slot.challenge_id,
        "challenge_revision": slot.revision,
        "request_json": encode_probe_job_request(request),
        "request_digest": probe_job_request_digest(request),
        "ray_address": "http://ray-head:8265",
        "submission_id": probe_job_submission_id(request),
        "entrypoint_digest": DIGEST,
        "submitted_runtime_env_digest": DIGEST,
        "reserved_at": NOW,
    }
    return lease, identity, issued, request, values


def _receive(row, **changes):
    # This deliberately tests storage only. The separate pure codec/publication
    # boundary must reject this unverified observation as target authority.
    values = {
        "receipt_json": '{"storage_test":true}',
        "receipt_digest": DIGEST,
        "received_at": NOW + timedelta(seconds=1),
    }
    values.update(changes)
    return RayTargetProbeJobReceipt.objects.filter(
        pk=row.pk,
        challenge_revision=row.challenge_revision,
        receipt_json__isnull=True,
        request_digest=row.request_digest,
    ).update(**values)


def _raw_update(row, field, value):
    table = connection.ops.quote_name(RayTargetProbeJobReceipt._meta.db_table)
    column = connection.ops.quote_name(field)
    with connection.cursor() as cursor:
        cursor.execute(f"UPDATE {table} SET {column} = %s WHERE challenge_id = %s", [value, row.pk])


def test_reservation_receipt_is_one_time_and_never_consumes_or_advertises(reservation):
    lease, _identity, issued, _request, values = reservation
    row = RayTargetProbeJobReceipt.objects.create(**values)
    assert issued.nonce not in row.request_json
    assert issued.nonce not in str(row)
    assert _receive(row) == 1
    assert _receive(row) == 0
    row.refresh_from_db()
    assert row.received_at == NOW + timedelta(seconds=1)
    assert RayTargetProbeChallenge.objects.get().consumed_at is None
    assert not RayWorkerTargetCapability.objects.exists()
    lease.refresh_from_db()
    assert lease.last_heartbeat_at == NOW
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeJobReceipt.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw_update(row, "receipt_json", row.receipt_json)


def _codec_receipt(request):
    versions = (RayNodeStateVersion("1" * 56, 1),)
    attestation = build_ray_cluster_attestation(
        expectation=RayTargetExpectation(
            request.target_key
            if request.target_key is not None
            else derive_cohort_target_key(request.runner_family, "session_jobs"),
            request.runner_family,
            "session_jobs",
            1,
            request.expected_runtime,
        ),
        boundary=build_ray_observation_boundary(
            resource_state_version_before=1,
            resource_state_version_after=2,
            node_state_versions_before=versions,
            node_state_versions_after=versions,
        ),
        nodes=(
            build_ray_node_observation(
                node_id=versions[0].node_id,
                cluster_session="session_jobs",
                runtime=request.expected_runtime,
            ),
        ),
        observed_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=30),
    )
    return CohortJobReceipt(
        request=request,
        request_digest=probe_job_request_digest(request),
        submission_id=probe_job_submission_id(request),
        native_job_id="01000000",
        observed_package_version=request.expected_package_version,
        attestation=attestation,
        collected_at=NOW + timedelta(seconds=2),
    )


def test_real_codec_receipt_roundtrip_preserves_independent_reservation(reservation):
    _lease_row, _identity, issued, request, values = reservation
    row = RayTargetProbeJobReceipt.objects.create(**values)
    receipt = _codec_receipt(request)
    assert (
        _receive(
            row,
            receipt_json=encode_cohort_job_receipt(receipt),
            receipt_digest=cohort_job_receipt_digest(receipt),
            received_at=NOW + timedelta(seconds=3),
        )
        == 1
    )
    row.refresh_from_db()
    assert (
        decode_cohort_job_receipt(
            row.receipt_json,
            expected_request=request,
            expected_request_digest=row.request_digest,
            expected_submission_id=row.submission_id,
            expected_receipt_digest=row.receipt_digest,
        )
        == receipt
    )
    assert issued.nonce not in row.receipt_json
    assert RAY_TARGET_PROBE_JOB_RECEIPT_MAX_BYTES == COHORT_JOB_RECEIPT_MAX_BYTES
    assert not RayWorkerTargetCapability.objects.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("challenge_revision", 0),
        ("challenge_revision", 2),
        ("request_json", "{}"),
        ("request_json", "not-json"),
        ("request_json", '{"payload":"' + "x" * COHORT_PROBE_JOB_MAX_BYTES + '"}'),
        ("request_digest", "sha256:" + "A" * 64),
        ("entrypoint_digest", "bad"),
        ("submitted_runtime_env_digest", "sha256:" + "g" * 64),
        ("ray_address", "http://user:secret@ray:8265"),
        ("ray_address", "http://ray:8265?token=secret"),
        ("ray_address", "http://ray:8265#secret"),
        ("ray_address", "http://"),
        ("ray_address", "ray://ray:10001"),
        ("ray_address", "http://ray\\secret"),
        ("ray_address", "http://ray/\nsecret"),
        ("ray_address", "http://" + "x" * 2048),
        ("submission_id", "arbitrary-job-id"),
        ("reserved_at", NOW - timedelta(microseconds=1)),
        ("reserved_at", NOW + timedelta(seconds=300)),
        ("receipt_json", "{}"),
        ("receipt_digest", DIGEST),
        ("received_at", NOW),
    ],
)
def test_reservation_invalid_fields_fail_closed(reservation, field, value):
    values = {**reservation[4], field: value}
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeJobReceipt.objects.create(**values)
    assert not RayTargetProbeJobReceipt.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 1},
        {"target_key": "caller-selected"},
        {"expected_cluster_session": "session_claimed"},
        {"policy_revision": 2},
    ],
)
def test_raw_discovery_reservation_rejects_old_schema_and_caller_selected_identity(
    reservation, changes
):
    values = dict(reservation[4])
    request = json.loads(values["request_json"]) | changes
    values["request_json"] = json.dumps(request, sort_keys=True, separators=(",", ":"))
    # Even self-consistent transport hashes cannot relax the database format.
    domain = (
        b"django-ray/cohort-probe-job/v1\x00"
        if changes.get("schema_version") == 1
        else b"django-ray/cohort-probe-job/v2\x00"
    )
    digest = hashlib.sha256(domain + values["request_json"].encode()).hexdigest()
    values["request_digest"] = "sha256:" + digest
    values["submission_id"] = "django-ray-cohort-probe-" + digest
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeJobReceipt.objects.create(**values)
    assert not RayTargetProbeJobReceipt.objects.exists()
    assert RayTargetProbeChallenge.objects.get().consumed_at is None


@pytest.mark.postgresql
def test_postgresql_discovery_request_schema_and_null_key_guard(reservation):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL JSON and receipt trigger required")
    for changes in (
        {"schema_version": 1},
        {"target_key": "caller-selected"},
        {"expected_cluster_session": "session_claimed"},
        {"policy_revision": 2},
    ):
        test_raw_discovery_reservation_rejects_old_schema_and_caller_selected_identity(
            reservation, changes
        )
    row = RayTargetProbeJobReceipt.objects.create(**reservation[4])
    assert row.receipt_json is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("challenge_id", 999),
        ("challenge_revision", 2),
        ("configuration_digest", DIGEST),
        ("issued_at", NOW + timedelta(microseconds=1)),
        ("expires_at", NOW + timedelta(seconds=299)),
        ("lease", CohortProbeJobLease("other", "manager-host", 100, NOW - timedelta(minutes=1))),
        ("lease", CohortProbeJobLease("manager", "other", 100, NOW - timedelta(minutes=1))),
        ("lease", CohortProbeJobLease("manager", "manager-host", 101, NOW - timedelta(minutes=1))),
        ("lease", CohortProbeJobLease("manager", "manager-host", 100, NOW - timedelta(seconds=59))),
    ],
)
def test_request_identity_must_match_pending_challenge(reservation, field, value):
    _lease_row, _identity, _issued, request, values = reservation
    changed = replace(request, **{field: value})
    values = {
        **values,
        "request_json": encode_probe_job_request(changed),
        "request_digest": probe_job_request_digest(changed),
        "submission_id": probe_job_submission_id(changed),
    }
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeJobReceipt.objects.create(**values)


@pytest.mark.parametrize("placement", ["root", "lease", "expected_runtime"])
def test_request_does_not_admit_a_nonce_field(reservation, placement):
    values = dict(reservation[4])
    request = json.loads(values["request_json"])
    (request if placement == "root" else request[placement])["nonce"] = reservation[2].nonce
    values["request_json"] = json.dumps(request, sort_keys=True, separators=(",", ":"))
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeJobReceipt.objects.create(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_json", "{}"),
        ("request_digest", DIGEST),
        ("challenge_revision", 2),
        ("ray_address", "http://replacement:8265"),
        ("submission_id", "replacement"),
        ("entrypoint_digest", "sha256:" + "c" * 64),
        ("submitted_runtime_env_digest", "sha256:" + "c" * 64),
        ("reserved_at", NOW + timedelta(seconds=1)),
    ],
)
def test_reservation_columns_are_immutable_even_with_receipt(reservation, field, value):
    row = RayTargetProbeJobReceipt.objects.create(**reservation[4])
    with pytest.raises(DatabaseError), transaction.atomic():
        _receive(row, **{field: value})
    row.refresh_from_db()
    assert row.received_at is None


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt_json": None},
        {"receipt_digest": None},
        {"received_at": None},
        {"receipt_json": "[]"},
        {"receipt_json": "not-json"},
        {"receipt_digest": "bad"},
        {"receipt_json": '{"data":"' + "x" * RAY_TARGET_PROBE_JOB_RECEIPT_MAX_BYTES + '"}'},
        {"received_at": NOW - timedelta(microseconds=1)},
        {"received_at": NOW + timedelta(seconds=300)},
    ],
)
def test_receipt_bounds_shape_and_window_are_enforced(reservation, changes):
    row = RayTargetProbeJobReceipt.objects.create(**reservation[4])
    with pytest.raises(DatabaseError), transaction.atomic():
        _receive(row, **changes)
    assert _receive(row) == 1


def test_consumed_challenge_cannot_receive_or_reserve(reservation):
    _lease_row, identity, issued, _request, values = reservation
    row = RayTargetProbeJobReceipt.objects.create(**values)
    consume_ray_target_probe_challenge(
        identity,
        issued.receipt.challenge_id,
        configuration_digest=issued.receipt.configuration_digest,
        expected_revision=1,
        nonce=issued.nonce,
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        _receive(row)
    row.delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeJobReceipt.objects.create(**values)


def test_receipt_rejects_regression_behind_lease_heartbeat(reservation):
    lease, _identity, _issued, _request, values = reservation
    row = RayTargetProbeJobReceipt.objects.create(**values)
    TaskWorkerLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=NOW + timedelta(seconds=2))
    with pytest.raises(DatabaseError), transaction.atomic():
        _receive(row)
    assert _receive(row, received_at=NOW + timedelta(seconds=2)) == 1


@pytest.mark.parametrize("kind", ["challenge", "lease", "lease_stop", "lease_reincarnate"])
def test_raw_sql_parent_cleanup_removes_receipt(reservation, kind):
    lease, _identity, issued, _request, values = reservation
    RayTargetProbeJobReceipt.objects.create(**values)
    challenge_table = connection.ops.quote_name(RayTargetProbeChallenge._meta.db_table)
    lease_table = connection.ops.quote_name(TaskWorkerLease._meta.db_table)
    with connection.cursor() as cursor:
        if kind == "challenge":
            cursor.execute(
                f"DELETE FROM {challenge_table} WHERE id = %s", [issued.receipt.challenge_id]
            )
        elif kind == "lease":
            cursor.execute(f"DELETE FROM {lease_table} WHERE worker_id = %s", [lease.pk])
        elif kind == "lease_stop":
            cursor.execute(
                f"UPDATE {lease_table} SET is_active = %s WHERE worker_id = %s", [False, lease.pk]
            )
        else:
            cursor.execute(
                f"UPDATE {lease_table} SET pid = pid + 1 WHERE worker_id = %s", [lease.pk]
            )
    assert not RayTargetProbeJobReceipt.objects.exists()
    assert not RayTargetProbeChallenge.objects.exists()


@pytest.mark.parametrize("raw", [False, True])
def test_replacement_deletes_old_reservation_and_late_receipt_cannot_attach(reservation, raw):
    _lease_row, identity, issued, _request, values = reservation
    row = RayTargetProbeJobReceipt.objects.create(**values)
    if raw:
        RayTargetProbeChallenge.objects.filter(pk=row.pk).update(
            nonce_digest="c" * 64,
            revision=2,
            issued_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=301),
        )
    else:
        replace_ray_target_probe_challenge(
            identity,
            row.pk,
            expected_configuration_digest=issued.receipt.configuration_digest,
            configuration_digest=issued.receipt.configuration_digest,
            expected_revision=1,
            expected_nonce=issued.nonce,
            now=NOW + timedelta(seconds=1),
        )
    assert not RayTargetProbeJobReceipt.objects.exists()
    assert _receive(row) == 0
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeJobReceipt.objects.create(**values)


def test_invalid_replacement_rolls_back_receipt_deletion(reservation):
    row = RayTargetProbeJobReceipt.objects.create(**reservation[4])
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeChallenge.objects.filter(pk=row.pk).update(nonce_digest="c" * 64, revision=7)
    assert RayTargetProbeJobReceipt.objects.filter(pk=row.pk).exists()
    assert _receive(row) == 1


@pytest.mark.parametrize("raw", [False, True])
def test_replacement_cannot_erase_a_receipt_from_a_later_time(reservation, raw):
    _lease_row, identity, issued, _request, values = reservation
    row = RayTargetProbeJobReceipt.objects.create(**values)
    _receive(row, received_at=NOW + timedelta(seconds=2))
    with (
        pytest.raises(DatabaseError if raw else ProbeChallengeError),
        transaction.atomic() if raw else nullcontext(),
    ):
        if raw:
            RayTargetProbeChallenge.objects.filter(pk=row.pk).update(
                nonce_digest="c" * 64,
                revision=2,
                issued_at=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(seconds=301),
            )
        else:
            replace_ray_target_probe_challenge(
                identity,
                row.pk,
                expected_configuration_digest=issued.receipt.configuration_digest,
                configuration_digest=issued.receipt.configuration_digest,
                expected_revision=1,
                expected_nonce=issued.nonce,
                now=NOW + timedelta(seconds=1),
            )
    assert RayTargetProbeJobReceipt.objects.get().received_at == NOW + timedelta(seconds=2)
    assert RayTargetProbeChallenge.objects.get().revision == 1


def test_reverse_guard_retains_reservations(reservation):
    row = RayTargetProbeJobReceipt.objects.create(**reservation[4])
    migration = importlib.import_module("django_ray.migrations.0029_cohort_job_receipts")
    with pytest.raises(RuntimeError, match="Jobs probe receipts remain"), transaction.atomic():
        migration._guard_empty(apps, connection.schema_editor())
    row.delete()
    with transaction.atomic():
        migration._guard_empty(apps, connection.schema_editor())


def test_migration_is_unseeded_and_reversible_when_empty():
    try:
        MigrationExecutor(connection).migrate([("django_ray", "0028_ray_task_cohort_intent")])
        MigrationExecutor(connection).migrate(LATEST)
        assert not RayTargetProbeJobReceipt.objects.exists()
        assert not RayTargetProbeChallenge.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.postgresql
def test_postgresql_concurrent_receipt_writers_have_one_winner(reservation, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    monkeypatch.setattr(storage, "_now", lambda: NOW)
    _reserve(reservation)
    receipt = _codec_receipt(reservation[3])
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))
    barrier = Barrier(2)

    def write():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return write_cohort_job_receipt(receipt)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == [0, 1]


@pytest.mark.postgresql
def test_postgresql_receipt_constraints_and_raw_cascade(reservation):
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    row = RayTargetProbeJobReceipt.objects.create(**reservation[4])
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw_update(row, "request_digest", DIGEST)
    assert _receive(row) == 1
    table = connection.ops.quote_name(TaskWorkerLease._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table} WHERE worker_id = %s", [reservation[0].pk])
    assert not RayTargetProbeJobReceipt.objects.exists()
    assert not RayTargetProbeChallenge.objects.exists()


@pytest.mark.postgresql
def test_postgresql_receipt_racing_replacement_cannot_survive_old_revision(
    reservation, monkeypatch
):
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    _lease_row, identity, issued, request, _values = reservation
    monkeypatch.setattr(storage, "_now", lambda: NOW)
    _reserve(reservation)
    row = RayTargetProbeJobReceipt.objects.get()
    receipt = _codec_receipt(request)
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))
    barrier = Barrier(2)

    def write():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return write_cohort_job_receipt(receipt)
        except CohortJobStorageError as error:
            assert error.classification == storage.CohortJobStorageRejection.CHALLENGE_UNAVAILABLE
            return False
        finally:
            close_old_connections()

    def rotate():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return replace_ray_target_probe_challenge(
                identity,
                row.pk,
                expected_configuration_digest=issued.receipt.configuration_digest,
                configuration_digest=issued.receipt.configuration_digest,
                expected_revision=1,
                expected_nonce=issued.nonce,
                now=NOW + timedelta(seconds=3),
            ).receipt.revision
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(write)
        replacement = executor.submit(rotate)
        assert writer.result(timeout=20) in (0, 1)
        assert replacement.result(timeout=20) == 2
    assert not RayTargetProbeJobReceipt.objects.exists()
    assert RayTargetProbeChallenge.objects.get(pk=row.pk).revision == 2


@pytest.mark.postgresql
def test_postgresql_service_reservation_race_is_exact_idempotent(reservation, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    monkeypatch.setattr(storage, "_now", lambda: NOW)
    barrier = Barrier(2)

    def reserve():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return _reserve(reservation).changed
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == [False, True]
    assert RayTargetProbeJobReceipt.objects.count() == 1


@pytest.fixture
def service_reservation(reservation, monkeypatch):
    monkeypatch.setattr(storage, "_now", lambda: NOW)
    return reservation


def _reserve(reservation, **changes):
    _lease_row, identity, issued, request, values = reservation
    arguments = {
        "nonce": issued.nonce,
        "jobs_endpoint": values["ray_address"],
        "entrypoint": "python -m django_ray.runtime.cohort_job",
        "submitted_runtime_env": {},
    }
    arguments.update(changes)
    return reserve_cohort_job_probe(identity, request, **arguments)


def _read(reservation, **changes):
    arguments = {"jobs_endpoint": reservation[4]["ray_address"]}
    arguments.update(changes)
    return read_cohort_job_reservation(reservation[1], reservation[3], **arguments)


def test_service_reserve_is_exact_idempotent_and_pending_read_has_no_snapshot(service_reservation):
    first = _reserve(service_reservation)
    assert first.changed
    second = _reserve(service_reservation)
    assert not second.changed
    assert replace(first, changed=False) == second
    assert _read(service_reservation) is None
    assert RayTargetProbeJobReceipt.objects.count() == 1
    assert RayTargetProbeChallenge.objects.get().consumed_at is None
    assert not RayWorkerTargetCapability.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": None},
        {"nonce": ""},
        {"nonce": "f" * 64},
        {"jobs_endpoint": "http://user:secret@ray:8265"},
        {"jobs_endpoint": "http://ray:99999"},
        {"jobs_endpoint": "auto"},
        {"entrypoint": "\nsecret"},
        {"submitted_runtime_env": {"value": object()}},
    ],
)
def test_service_reservation_refuses_invalid_arguments_without_persistence(
    service_reservation, changes
):
    with pytest.raises(CohortJobStorageError) as error:
        _reserve(service_reservation, **changes)
    assert "secret" not in str(error.value)
    assert service_reservation[2].nonce not in str(error.value)
    assert not RayTargetProbeJobReceipt.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"jobs_endpoint": "http://replacement:8265"},
        {"entrypoint": "python replacement.py"},
        {"submitted_runtime_env": {"env_vars": {"TOKEN": "secret"}}},
    ],
)
def test_service_crossed_reservation_never_overwrites_first(service_reservation, changes):
    first = _reserve(service_reservation)
    with pytest.raises(CohortJobStorageError, match="reservation_mismatch"):
        _reserve(service_reservation, **changes)
    assert _reserve(service_reservation) == replace(first, changed=False)


def test_service_receipt_is_nonce_free_one_time_and_reader_maps_endpoint(
    service_reservation, monkeypatch
):
    _reserve(service_reservation)
    receipt = _codec_receipt(service_reservation[3])
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))

    def no_nonce(*_args, **_kwargs):
        raise AssertionError("driver receipt must not consume the manager nonce")

    monkeypatch.setattr(storage, "_lock_probe_challenge_for_completion", no_nonce)
    assert write_cohort_job_receipt(receipt)
    assert not write_cohort_job_receipt(receipt)
    snapshot = _read(service_reservation)
    assert snapshot is not None
    assert snapshot.jobs_endpoint == service_reservation[4]["ray_address"]
    assert snapshot.request == service_reservation[3]
    assert snapshot.receipt_json == encode_cohort_job_receipt(receipt)
    assert service_reservation[2].nonce not in repr(snapshot)
    assert snapshot.jobs_endpoint not in repr(snapshot)
    assert RayTargetProbeChallenge.objects.get().consumed_at is None
    assert not RayWorkerTargetCapability.objects.exists()


def test_service_receipt_cannot_create_its_own_reservation(service_reservation, monkeypatch):
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))
    with pytest.raises(CohortJobStorageError, match="reservation_unavailable"):
        write_cohort_job_receipt(_codec_receipt(service_reservation[3]))
    assert not RayTargetProbeJobReceipt.objects.exists()


def test_service_conflicting_second_receipt_cannot_replace_observation(
    service_reservation, monkeypatch
):
    _reserve(service_reservation)
    receipt = _codec_receipt(service_reservation[3])
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))
    assert write_cohort_job_receipt(receipt)
    with pytest.raises(CohortJobStorageError, match="receipt_mismatch"):
        write_cohort_job_receipt(replace(receipt, native_job_id="02000000"))
    assert _read(service_reservation).receipt_json == encode_cohort_job_receipt(receipt)


def test_service_reader_rejects_crossed_endpoint_request_and_identity(
    service_reservation, monkeypatch
):
    _reserve(service_reservation)
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))
    write_cohort_job_receipt(_codec_receipt(service_reservation[3]))
    with pytest.raises(CohortJobStorageError, match="reservation_mismatch"):
        _read(service_reservation, jobs_endpoint="http://replacement:8265")
    with pytest.raises(CohortJobStorageError, match="reservation_mismatch"):
        read_cohort_job_reservation(
            replace(service_reservation[1], pid=111),
            service_reservation[3],
            jobs_endpoint=service_reservation[4]["ray_address"],
        )
    with pytest.raises(CohortJobStorageError, match="invalid"):
        read_cohort_job_reservation(
            service_reservation[1],
            replace(service_reservation[3], target_key="crossed"),
            jobs_endpoint=service_reservation[4]["ray_address"],
        )


@pytest.mark.parametrize("operation", ["reserve", "write", "read"])
def test_storage_requires_outer_durable_boundary(service_reservation, operation, monkeypatch):
    if operation != "reserve":
        _reserve(service_reservation)
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))
    with transaction.atomic(), pytest.raises(CohortJobStorageError, match="transaction_open"):
        if operation == "reserve":
            _reserve(service_reservation)
        elif operation == "write":
            write_cohort_job_receipt(_codec_receipt(service_reservation[3]))
        else:
            _read(service_reservation)


@pytest.mark.parametrize("operation", ["reserve", "write", "read"])
@pytest.mark.parametrize("fresh", [NOW - timedelta(microseconds=1), NOW + timedelta(seconds=61)])
def test_storage_rechecks_clock_and_live_lease_after_all_locks(
    service_reservation, monkeypatch, operation, fresh
):
    if operation != "reserve":
        _reserve(service_reservation)
    values = iter((NOW, fresh))
    monkeypatch.setattr(storage, "_now", lambda: next(values))
    reason = "clock_regression" if fresh < NOW else "challenge_unavailable"
    with pytest.raises(CohortJobStorageError, match=reason):
        if operation == "reserve":
            _reserve(service_reservation)
        elif operation == "write":
            write_cohort_job_receipt(_codec_receipt(service_reservation[3]))
        else:
            _read(service_reservation)
    assert not RayTargetProbeJobReceipt.objects.filter(received_at__isnull=False).exists()


def test_storage_replacement_fences_late_canonical_receipt(service_reservation, monkeypatch):
    _reserve(service_reservation)
    issued = service_reservation[2]
    replace_ray_target_probe_challenge(
        service_reservation[1],
        issued.receipt.challenge_id,
        expected_configuration_digest=issued.receipt.configuration_digest,
        configuration_digest=issued.receipt.configuration_digest,
        expected_revision=1,
        expected_nonce=issued.nonce,
        now=NOW + timedelta(seconds=1),
    )
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))
    with pytest.raises(CohortJobStorageError, match="challenge_unavailable"):
        write_cohort_job_receipt(_codec_receipt(service_reservation[3]))
    assert not RayTargetProbeJobReceipt.objects.exists()


def test_storage_reader_refuses_noncanonical_database_observation(service_reservation, monkeypatch):
    _reserve(service_reservation)
    row = RayTargetProbeJobReceipt.objects.get()
    _receive(row)
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=3))
    with pytest.raises(CohortJobStorageError, match="receipt_mismatch"):
        _read(service_reservation)


def test_storage_receipt_waits_for_collected_time_and_expires_with_observation(
    service_reservation, monkeypatch
):
    _reserve(service_reservation)
    receipt = _codec_receipt(service_reservation[3])
    with pytest.raises(CohortJobStorageError, match="clock_regression"):
        write_cohort_job_receipt(receipt)
    monkeypatch.setattr(storage, "_now", lambda: NOW + timedelta(seconds=30))
    with pytest.raises(CohortJobStorageError, match="receipt_mismatch"):
        write_cohort_job_receipt(receipt)
    assert RayTargetProbeJobReceipt.objects.get().receipt_json is None


@pytest.mark.parametrize(
    "changes",
    [
        {"target_key": "crossed"},
        {"expected_cluster_session": "session_crossed"},
        {"policy_revision": 2},
        {"expected_runtime": RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 13)},
    ],
)
def test_service_refresh_request_must_match_current_policy(service_reservation, changes):
    _target(family=RayRunnerFamily.RAY_JOB)
    policy = apps.get_model("django_ray", "RayTargetPolicyRevision").objects.get()
    lease, identity, issued, request, values = service_reservation
    refreshed = replace_ray_target_probe_challenge(
        identity,
        issued.receipt.challenge_id,
        expected_configuration_digest=issued.receipt.configuration_digest,
        configuration_digest=issued.receipt.configuration_digest,
        expected_revision=1,
        expected_nonce=issued.nonce,
        now=NOW,
        expected_target_policy_id=policy.pk,
    )
    request = replace(
        request,
        challenge_revision=2,
        target_key="primary",
        expected_target_policy_id=policy.pk,
        expected_cluster_session="session_primary",
    )
    current = (lease, identity, refreshed, request, values)
    with pytest.raises(CohortJobStorageError, match="reservation_mismatch"):
        _reserve((lease, identity, refreshed, replace(request, **changes), values))
    assert not RayTargetProbeJobReceipt.objects.exists()
    assert _reserve(current).changed
