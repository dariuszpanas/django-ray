from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from django_ray.runtime.cohort_job import (
    CohortProbeJobLease,
    CohortProbeJobRequest,
    encode_probe_job_request,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.target.attestation import (
    RAY_TARGET_ATTESTATION_MAX_NODES,
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
    COHORT_JOB_RECEIPT_MAX_DEPTH,
    CohortJobReceipt,
    CohortJobReceiptError,
    CohortJobReceiptRejection,
    cohort_job_receipt_digest,
    decode_cohort_job_receipt,
    encode_cohort_job_receipt,
    is_canonical_native_ray_job_id,
)
from django_ray.target.cohort_probe import derive_cohort_target_key

NOW = datetime(2020, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def receipt(*, refresh=False, nodes=1):
    runtime = RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14)
    request = CohortProbeJobRequest(
        challenge_id=11,
        challenge_revision=2,
        lease=CohortProbeJobLease("worker-one", "manager-one", 456, NOW - timedelta(seconds=10)),
        configuration_digest="sha256:" + "a" * 64,
        target_key="jobs-first" if refresh else None,
        runner_family=RayRunnerFamily.RAY_JOB,
        expected_package_version="0.5.0",
        expected_runtime=runtime,
        expected_cluster_session="session_jobs" if refresh else None,
        expected_target_policy_id=9 if refresh else None,
        policy_revision=4 if refresh else 1,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )
    versions = tuple(RayNodeStateVersion(f"{number:056x}", 1) for number in range(1, nodes + 1))
    attestation = build_ray_cluster_attestation(
        expectation=RayTargetExpectation(
            request.target_key
            if request.target_key is not None
            else derive_cohort_target_key(request.runner_family, "session_jobs"),
            request.runner_family,
            "session_jobs",
            request.policy_revision,
            runtime,
        ),
        boundary=build_ray_observation_boundary(
            resource_state_version_before=1,
            resource_state_version_after=2,
            node_state_versions_before=versions,
            node_state_versions_after=versions,
        ),
        nodes=tuple(
            build_ray_node_observation(
                node_id=version.node_id, cluster_session="session_jobs", runtime=runtime
            )
            for version in versions
        ),
        observed_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=30),
    )
    return CohortJobReceipt(
        request=request,
        request_digest=probe_job_request_digest(request),
        submission_id=probe_job_submission_id(request),
        native_job_id="01000000",
        observed_package_version="0.5.0",
        attestation=attestation,
        collected_at=NOW + timedelta(seconds=2),
    )


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def bindings(value):
    return {
        "expected_request": value.request,
        "expected_request_digest": value.request_digest,
        "expected_submission_id": value.submission_id,
        "expected_receipt_digest": cohort_job_receipt_digest(value),
    }


def assert_refusal(error, reason):
    assert error.value.classification is reason
    assert str(error.value) == f"Cohort Job receipt rejected: {reason.value}"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("refresh", [False, True])
def test_discovery_and_refresh_roundtrip_with_independent_reservation_bindings(refresh):
    value = receipt(refresh=refresh)
    encoded = encode_cohort_job_receipt(value)
    assert decode_cohort_job_receipt(encoded, **bindings(value)) == value
    assert "nonce" not in encoded
    assert json.loads(encoded)["request"] == json.loads(encode_probe_job_request(value.request))
    assert (
        cohort_job_receipt_digest(value)
        == "sha256:"
        + hashlib.sha256(
            b"django-ray/cohort-probe-job-receipt/v1\x00" + encoded.encode("ascii")
        ).hexdigest()
    )
    # Historical evidence remains decodable; a publisher owns current freshness.
    assert value.collected_at.year == 2020


def test_maximum_supported_cluster_membership_roundtrip():
    value = receipt(nodes=RAY_TARGET_ATTESTATION_MAX_NODES)
    encoded = encode_cohort_job_receipt(value)
    assert decode_cohort_job_receipt(encoded, **bindings(value)) == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_request_digest", "sha256:" + "b" * 64),
        ("expected_receipt_digest", "sha256:" + "b" * 64),
        ("expected_request_digest", True),
        ("expected_submission_id", "another-job"),
        ("expected_submission_id", True),
        ("expected_request", replace(receipt().request, challenge_revision=3)),
        ("expected_request", replace(receipt().request, configuration_digest="sha256:" + "b" * 64)),
        (
            "expected_request",
            replace(receipt().request, lease=replace(receipt().request.lease, pid=7)),
        ),
    ],
)
def test_wrong_independent_reservation_bindings_refuse(field, value):
    original = receipt()
    with pytest.raises(CohortJobReceiptError):
        decode_cohort_job_receipt(
            encode_cohort_job_receipt(original), **(bindings(original) | {field: value})
        )


def test_self_consistent_new_challenge_cannot_replace_reserved_request():
    original = receipt()
    new_request = replace(original.request, challenge_revision=3)
    changed = replace(
        original,
        request=new_request,
        request_digest=probe_job_request_digest(new_request),
        submission_id=probe_job_submission_id(new_request),
    )
    with pytest.raises(CohortJobReceiptError) as error:
        decode_cohort_job_receipt(encode_cohort_job_receipt(changed), **bindings(original))
    assert_refusal(error, CohortJobReceiptRejection.BINDING_MISMATCH)


@pytest.mark.parametrize("field", ["native_job_id", "collected_at"])
def test_independent_receipt_digest_rejects_changed_driver_or_collection_time(field):
    original = receipt()
    change = (
        "02000000" if field == "native_job_id" else original.collected_at + timedelta(seconds=1)
    )
    encoded = encode_cohort_job_receipt(replace(original, **{field: change}))
    with pytest.raises(CohortJobReceiptError) as error:
        decode_cohort_job_receipt(encoded, **bindings(original))
    assert_refusal(error, CohortJobReceiptRejection.DIGEST_MISMATCH)


@pytest.mark.parametrize(
    "value", [None, True, 1, b"01000000", "", "1", "AB000000", "010000000", "ffffffff", " 1000000"]
)
def test_native_ray_job_id_requires_exact_non_nil_hex(value):
    assert not is_canonical_native_ray_job_id(value)
    with pytest.raises(CohortJobReceiptError) as error:
        encode_cohort_job_receipt(replace(receipt(), native_job_id=value))
    assert_refusal(error, CohortJobReceiptRejection.INVALID)


@pytest.mark.parametrize("value", ["00000000", "01000000", "ab00cdef"])
def test_native_id_format_validation_does_not_claim_remote_provenance(value):
    assert is_canonical_native_ray_job_id(value)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"request_digest": "sha256:" + "b" * 64}, CohortJobReceiptRejection.DIGEST_MISMATCH),
        ({"submission_id": "other-job"}, CohortJobReceiptRejection.BINDING_MISMATCH),
        ({"observed_package_version": "0.5.1"}, CohortJobReceiptRejection.RUNTIME_MISMATCH),
        ({"observed_package_version": "v0.5.0"}, CohortJobReceiptRejection.RUNTIME_MISMATCH),
        ({"observed_package_version": True}, CohortJobReceiptRejection.RUNTIME_MISMATCH),
        ({"collected_at": NOW}, CohortJobReceiptRejection.OBSERVATION_WINDOW_INVALID),
        (
            {"collected_at": NOW + timedelta(seconds=30)},
            CohortJobReceiptRejection.OBSERVATION_WINDOW_INVALID,
        ),
        ({"collected_at": NOW.replace(tzinfo=None)}, CohortJobReceiptRejection.INVALID),
    ],
)
def test_encoder_rejects_inconsistent_typed_evidence(change, reason):
    with pytest.raises(CohortJobReceiptError) as error:
        encode_cohort_job_receipt(replace(receipt(), **change))
    assert_refusal(error, reason)


@pytest.mark.parametrize("kind", ["request_expired", "observation_before_request"])
def test_request_deadline_and_observation_chronology_are_strict(kind):
    original = receipt()
    request = replace(
        original.request,
        **(
            {"expires_at": original.collected_at}
            if kind == "request_expired"
            else {"issued_at": original.collected_at}
        ),
    )
    changed = replace(
        original,
        request=request,
        request_digest=probe_job_request_digest(request),
        submission_id=probe_job_submission_id(request),
    )
    with pytest.raises(CohortJobReceiptError) as error:
        encode_cohort_job_receipt(changed)
    assert_refusal(error, CohortJobReceiptRejection.OBSERVATION_WINDOW_INVALID)


@pytest.mark.parametrize(
    "change",
    [
        {
            "target_key": "other-target",
            "expected_cluster_session": "session_jobs",
            "expected_target_policy_id": 9,
        },
        {"expected_runtime": replace(receipt().request.expected_runtime, ray_patch=1)},
        {
            "target_key": "jobs-first",
            "expected_cluster_session": "session_other",
            "expected_target_policy_id": 9,
        },
        {
            "target_key": "jobs-first",
            "expected_cluster_session": "session_jobs",
            "expected_target_policy_id": 9,
            "policy_revision": 2,
        },
    ],
)
def test_full_attestation_must_match_request_target_session_policy_and_runtime(change):
    original = receipt()
    request = replace(original.request, **change)
    changed = replace(
        original,
        request=request,
        request_digest=probe_job_request_digest(request),
        submission_id=probe_job_submission_id(request),
    )
    with pytest.raises(CohortJobReceiptError) as error:
        encode_cohort_job_receipt(changed)
    assert_refusal(error, CohortJobReceiptRejection.BINDING_MISMATCH)


def test_discovery_receipt_independently_rejects_a_canonical_relabelled_attestation():
    original = receipt()
    assert original.request.target_key is None
    proof = original.attestation
    relabelled = build_ray_cluster_attestation(
        expectation=replace(proof.expectation, target_key="caller-selected"),
        boundary=proof.boundary,
        nodes=proof.nodes,
        observed_at=proof.observed_at,
        expires_at=proof.expires_at,
    )
    changed = replace(original, attestation=relabelled)
    assert changed.request_digest == probe_job_request_digest(changed.request)
    assert changed.submission_id == probe_job_submission_id(changed.request)
    with pytest.raises(CohortJobReceiptError) as error:
        encode_cohort_job_receipt(changed)
    assert_refusal(error, CohortJobReceiptRejection.BINDING_MISMATCH)


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("schema_version",), 2),
        (("schema",), "other"),
        (("nonce",), "private-value"),
        (("nonce_digest",), "private-value"),
        (("request", "nonce"), "private-value"),
        (("request", "schema_version"), 1),
        (("request", "target_key"), "caller-selected"),
        (("request", "challenge_id"), True),
        (("request", "challenge_revision"), 0),
        (("request", "lease", "pid"), 1 << 31),
        (("attestation", "attestation_digest"), "sha256:" + "b" * 64),
        (("attestation", "membership_digest"), "sha256:" + "b" * 64),
        (("attestation", "nodes", 0, "observation_digest"), "sha256:" + "b" * 64),
        (("collected_at",), "2020-01-02T03:04:07.123456+00:00"),
    ],
)
def test_malformed_or_tampered_nested_fields_are_rejected_without_echo(path, value):
    wire = json.loads(encode_cohort_job_receipt(receipt()))
    target = wire
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(CohortJobReceiptError) as error:
        decode_cohort_job_receipt(canonical(wire))
    assert "private-value" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_missing_duplicate_and_noncanonical_fields_refuse():
    encoded = encode_cohort_job_receipt(receipt())
    missing = json.loads(encoded)
    missing.pop("native_job_id")
    for invalid in (
        canonical(missing),
        '{"schema_version":1,' + encoded[1:],
        encoded.replace('"challenge_id":11', '"challenge_id":11,"challenge_id":11'),
        " " + encoded,
        json.dumps(json.loads(encoded), indent=2),
        encoded.replace("01000000", "\\u00301000000"),
    ):
        with pytest.raises(CohortJobReceiptError):
            decode_cohort_job_receipt(invalid)


def test_receipt_limit_is_exact_and_can_only_be_lowered():
    value = receipt()
    encoded = encode_cohort_job_receipt(value)
    assert decode_cohort_job_receipt(encoded, max_bytes=len(encoded)) == value
    assert encode_cohort_job_receipt(value, max_bytes=len(encoded)) == encoded
    for limit in (len(encoded) - 1, 0, -1, True, COHORT_JOB_RECEIPT_MAX_BYTES + 1):
        for operation, payload in (
            (encode_cohort_job_receipt, value),
            (decode_cohort_job_receipt, encoded),
        ):
            with pytest.raises(CohortJobReceiptError) as error:
                operation(payload, max_bytes=limit)
            assert_refusal(error, CohortJobReceiptRejection.RESOURCE_LIMIT)


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"{}",
        "[]",
        "null",
        "{",
        "\ud800",
        " " * (COHORT_JOB_RECEIPT_MAX_BYTES + 1),
        '{"x":NaN}',
        '{"x":1.0}',
        '{"x":9223372036854775808}',
        '{"x":' + "1" * 100 + "}",
        "[" * (COHORT_JOB_RECEIPT_MAX_DEPTH + 1) + "0" + "]" * (COHORT_JOB_RECEIPT_MAX_DEPTH + 1),
    ],
    ids=[
        "none",
        "bytes",
        "list",
        "null",
        "malformed",
        "surrogate",
        "oversized",
        "nan",
        "float",
        "counter-overflow",
        "long-integer",
        "depth",
    ],
)
def test_untrusted_input_limits_and_number_types_fail_closed(value):
    with pytest.raises(CohortJobReceiptError) as error:
        decode_cohort_job_receipt(value)
    assert error.value.__suppress_context__ is True


def test_receipt_roundtrip_in_fresh_process_with_django_and_ray_imports_poisoned():
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import sys
class NoApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'ray', 'testproject'}:
            raise RuntimeError('forbidden application import')
sys.meta_path.insert(0, NoApplication())
from django_ray.target.cohort_job_receipt import decode_cohort_job_receipt, encode_cohort_job_receipt
assert encode_cohort_job_receipt(decode_cohort_job_receipt(sys.argv[1])) == sys.argv[1]
assert 'django_ray.models' not in sys.modules
assert 'django_ray.runtime.entrypoint' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script, encode_cohort_job_receipt(receipt())],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
