from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import connections, transaction
from django.test.utils import CaptureQueriesContext
from ray.dashboard.modules.job.common import JobStatus
from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

from django_ray.runtime.cohort_job import probe_job_request_digest, probe_job_submission_id
from django_ray.target import cohort_job_control as control
from django_ray.target import cohort_job_http
from django_ray.target.cohort_job_receipt import (
    CohortJobReceipt,
    cohort_job_receipt_digest,
    decode_cohort_job_receipt,
    encode_cohort_job_receipt,
)
from tests.unit.test_cohort_job import NOW, attestation, request

ENTRYPOINT = "python -m django_ray.runtime.cohort_job --probe fixed-carrier"
ENVIRONMENT = {"working_dir": "gcs://probe-source.zip", "env_vars": {"PROBE_CONFIG": "one"}}
NATIVE_ID = "01000000"


def reservation():
    value = request()
    receipt = CohortJobReceipt(
        request=value,
        request_digest=probe_job_request_digest(value),
        submission_id=probe_job_submission_id(value),
        native_job_id=NATIVE_ID,
        observed_package_version=value.expected_package_version,
        attestation=attestation(value),
        collected_at=NOW + timedelta(seconds=1),
    )
    return control.CohortJobReservationSnapshot(
        request=value,
        request_digest=receipt.request_digest,
        jobs_endpoint="http://127.0.0.1:8265",
        entrypoint_digest=control.cohort_probe_entrypoint_digest(ENTRYPOINT),
        submitted_runtime_env_digest=control.cohort_probe_submitted_runtime_env_digest(ENVIRONMENT),
        receipt_json=encode_cohort_job_receipt(receipt),
        receipt_digest=cohort_job_receipt_digest(receipt),
    )


@pytest.fixture
def endpoint(monkeypatch, transactional_db):
    snapshot = reservation()
    assert JobDetails is not None
    state = SimpleNamespace(
        constructors=[],
        queries=[],
        details=JobDetails(
            type=JobType.SUBMISSION,
            submission_id=probe_job_submission_id(snapshot.request),
            job_id=NATIVE_ID,
            status=JobStatus.SUCCEEDED,
            entrypoint=ENTRYPOINT,
            metadata=control.probe_job_metadata(snapshot.request),
            runtime_env=ENVIRONMENT,
        ),
    )

    def fetch(address, submission_id):
        assert not any(
            connection.in_atomic_block for connection in connections.all(initialized_only=True)
        )
        state.constructors.append(address)
        state.queries.append(submission_id)
        return state.details

    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", fetch)
    monkeypatch.setattr(control, "_now", lambda: NOW + timedelta(seconds=2))
    return snapshot, state


def test_inspector_fetches_exact_reserved_endpoint_and_handle(endpoint):
    snapshot, state = endpoint
    with CaptureQueriesContext(connections["default"]) as queries:
        result = control.inspect_reserved_cohort_job(snapshot)
    assert not queries
    assert result.reservation == snapshot
    assert result.receipt.native_job_id == NATIVE_ID
    assert result.inspected_at == NOW + timedelta(seconds=2)
    assert state.constructors == [snapshot.jobs_endpoint]
    assert state.queries == [probe_job_submission_id(snapshot.request)]
    assert snapshot.jobs_endpoint not in repr(result)
    assert snapshot.receipt_json not in repr(snapshot)


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_unfinished_job_is_not_proof_even_with_valid_receipt(endpoint, status):
    snapshot, state = endpoint
    state.details = state.details.model_copy(update={"status": status, "job_id": None})
    assert control.inspect_reserved_cohort_job(snapshot) is None
    assert len(state.queries) == 1


@pytest.mark.parametrize("status", [JobStatus.FAILED, JobStatus.STOPPED])
def test_failed_job_does_not_publish_its_receipt(endpoint, status):
    snapshot, state = endpoint
    state.details = state.details.model_copy(update={"status": status})
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(snapshot)
    assert error.value.reason is control.CohortJobInspectionReason.JOB_FAILED


@pytest.mark.parametrize(
    "change",
    [
        {"type": JobType.DRIVER},
        {"submission_id": "another-physical-job"},
        {"job_id": "02000000"},
        {"job_id": None},
        {"metadata": {}},
        {"entrypoint": ENTRYPOINT + " changed"},
        {"runtime_env": {"env_vars": {"PROBE_CONFIG": "changed"}}},
        {"runtime_env": None},
        {"status": "SUCCEEDED"},
    ],
)
def test_success_status_and_metadata_cannot_override_actual_record_bindings(endpoint, change):
    snapshot, state = endpoint
    state.details = state.details.model_copy(update=change)
    with pytest.raises(control.CohortJobInspectionError):
        control.inspect_reserved_cohort_job(snapshot)
    assert len(state.queries) == 1


def test_caller_like_record_is_not_an_actual_typed_jobs_response(endpoint):
    snapshot, state = endpoint
    state.details = SimpleNamespace(**state.details.model_dump())
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(snapshot)
    assert error.value.reason is control.CohortJobInspectionReason.JOB_MISMATCH


@pytest.mark.parametrize(
    "change",
    [
        {"request_digest": "sha256:" + "0" * 64},
        {"entrypoint_digest": "invalid"},
        {"submitted_runtime_env_digest": "invalid"},
        {"jobs_endpoint": "auto"},
        {"jobs_endpoint": "ray://127.0.0.1:10001"},
        {"jobs_endpoint": "http://name:password@127.0.0.1:8265"},
        {"jobs_endpoint": "http://127.0.0.1:8265?"},
        {"jobs_endpoint": "http://127.0.0.1:8265#"},
        {"receipt_digest": "sha256:" + "0" * 64},
        {"receipt_json": "invalid-secret-receipt"},
    ],
)
def test_invalid_stored_bindings_refuse_before_network(endpoint, change):
    snapshot, state = endpoint
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(replace(snapshot, **change))
    assert "password" not in str(error.value)
    assert "secret" not in str(error.value)
    assert not state.constructors


@pytest.mark.django_db(transaction=True)
def test_open_database_transaction_refuses_before_network(endpoint):
    snapshot, state = endpoint
    with transaction.atomic():
        with pytest.raises(control.CohortJobInspectionError) as error:
            control.inspect_reserved_cohort_job(snapshot)
    assert error.value.reason is control.CohortJobInspectionReason.TRANSACTION_OPEN
    assert not state.constructors


@pytest.mark.django_db(transaction=True)
def test_manual_transaction_refuses_before_network(endpoint):
    snapshot, state = endpoint
    connection = connections["default"]
    connection.set_autocommit(False)
    try:
        with pytest.raises(control.CohortJobInspectionError) as error:
            control.inspect_reserved_cohort_job(snapshot)
        assert error.value.reason is control.CohortJobInspectionReason.TRANSACTION_OPEN
        assert not state.constructors
    finally:
        connection.rollback()
        connection.set_autocommit(True)


@pytest.mark.parametrize("seconds", [-1, 300])
def test_request_window_is_checked_before_network(endpoint, monkeypatch, seconds):
    snapshot, state = endpoint
    monkeypatch.setattr(control, "_now", lambda: NOW + timedelta(seconds=seconds))
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(snapshot)
    assert error.value.reason is control.CohortJobInspectionReason.REQUEST_EXPIRED
    assert not state.constructors


@pytest.mark.parametrize("seconds", [1, 300])
def test_clock_and_deadline_are_rechecked_after_network(endpoint, monkeypatch, seconds):
    snapshot, state = endpoint
    times = iter([NOW + timedelta(seconds=2), NOW + timedelta(seconds=seconds)])
    monkeypatch.setattr(control, "_now", lambda: next(times))
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(snapshot)
    expected = (
        control.CohortJobInspectionReason.CLOCK_REGRESSION
        if seconds == 1
        else control.CohortJobInspectionReason.REQUEST_EXPIRED
    )
    assert error.value.reason is expected
    assert len(state.queries) == 1


def test_transport_exception_is_redacted(endpoint, monkeypatch):
    snapshot, _state = endpoint

    def fail(_address, _submission_id):
        raise ConnectionError("password=do-not-print")

    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", fail)
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(snapshot)
    assert error.value.reason is control.CohortJobInspectionReason.JOB_UNAVAILABLE
    assert "password" not in str(error.value)
    assert error.value.__suppress_context__


def test_actual_manager_runtime_must_match_before_connecting(endpoint, monkeypatch):
    from django_ray.target import cohort_runtime

    snapshot, state = endpoint
    monkeypatch.setattr(
        cohort_runtime,
        "_local_runtime",
        lambda _ray: ("999.0.0", snapshot.request.expected_runtime),
    )
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(snapshot)
    assert error.value.reason is control.CohortJobInspectionReason.LOCAL_RUNTIME_MISMATCH
    assert not state.constructors


def test_proof_expiring_during_query_cannot_be_published(endpoint, monkeypatch):
    snapshot, state = endpoint
    times = iter([NOW + timedelta(seconds=2), NOW + timedelta(seconds=30)])
    monkeypatch.setattr(control, "_now", lambda: next(times))
    with pytest.raises(control.CohortJobInspectionError):
        control.inspect_reserved_cohort_job(snapshot)
    assert len(state.queries) == 1


def test_future_collection_timestamp_is_not_a_current_receipt(endpoint):
    snapshot, state = endpoint
    receipt = replace(
        decode_cohort_job_receipt(snapshot.receipt_json), collected_at=NOW + timedelta(seconds=3)
    )
    snapshot = replace(
        snapshot,
        receipt_json=encode_cohort_job_receipt(receipt),
        receipt_digest=cohort_job_receipt_digest(receipt),
    )
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(snapshot)
    assert error.value.reason is control.CohortJobInspectionReason.INVALID_RECEIPT
    assert not state.constructors


def test_driver_association_cannot_contradict_native_receipt(endpoint):
    from ray.dashboard.modules.job.pydantic_models import DriverInfo

    assert DriverInfo is not None
    snapshot, state = endpoint
    state.details = state.details.model_copy(
        update={"driver_info": DriverInfo(id="02000000", node_ip_address="127.0.0.1", pid="1")}
    )
    with pytest.raises(control.CohortJobInspectionError) as error:
        control.inspect_reserved_cohort_job(snapshot)
    assert error.value.reason is control.CohortJobInspectionReason.JOB_MISMATCH


@pytest.mark.parametrize("value", [None, "", "bad\ncommand", "x" * 16385, "\ud800"])
def test_entrypoint_binding_is_bounded_and_redacted(value):
    with pytest.raises(control.CohortJobInspectionError):
        control.cohort_probe_entrypoint_digest(value)


@pytest.mark.parametrize(
    "value", [None, [], {"key": "x" * 65537}, {"key": float("nan")}, {"key": "\0"}]
)
def test_runtime_env_transport_binding_is_bounded(value):
    with pytest.raises(control.CohortJobInspectionError):
        control.cohort_probe_submitted_runtime_env_digest(value)


def test_transport_digest_is_order_independent_but_binds_actual_values():
    digest = control.cohort_probe_submitted_runtime_env_digest
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"env_vars": {"KEY": "one"}}) != digest({"env_vars": {"KEY": "two"}})
