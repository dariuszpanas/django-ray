"""Pure claim facts retain exact provenance without fabricating Sync Ray proof."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from django_ray.execution_codec import ExecutionIdentity
from django_ray.target.cohort_claim import (
    CohortBindingSpec,
    CohortCapabilitySnapshot,
    CohortClaimError,
    CohortClaimFacts,
    CohortJobQualificationProvenance,
    CohortManagerRuntime,
    CohortPythonVersion,
    CohortRunnerFamily,
    cohort_claim_facts_digest,
    cohort_task_runtime_env_snapshot_digest,
    decode_cohort_claim_facts,
    encode_cohort_claim_facts,
    validate_cohort_job_qualification,
)
from django_ray.target.cohort_intent import (
    CohortIntent,
    CohortSelectionPolicy,
    cohort_intent_digest,
    encode_cohort_intent,
)

NOW = datetime(2026, 9, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
PYTHON = CohortPythonVersion("cpython", 3, 12, 14)


def qualification():
    return CohortJobQualificationProvenance(
        configuration_digest=DIGEST,
        jobs_endpoint="http://ray-head:8265",
        challenge_id=1,
        request_revision=1,
        consumed_challenge_revision=2,
        challenge_issued_at=NOW - timedelta(seconds=1),
        challenge_expires_at=NOW + timedelta(seconds=60),
        consumed_at=NOW,
        request_digest=DIGEST,
        receipt_digest=DIGEST,
        submission_id="django-ray-cohort-probe-" + "a" * 64,
        native_job_id="01000000",
        entrypoint_digest=DIGEST,
        submitted_control_runtime_env_digest=DIGEST,
        endpoint_expectation_digest=DIGEST,
        endpoint_attestation_digest=DIGEST,
        endpoint_membership_digest=DIGEST,
        endpoint_observed_at=NOW,
        endpoint_expires_at=NOW + timedelta(seconds=25),
        receipt_received_at=NOW,
    )


def facts(*, family=CohortRunnerFamily.SYNC):
    intent = CohortIntent(
        "0.5.0", "default", DIGEST, "sha256:" + "b" * 64, CohortSelectionPolicy.WORKER_SELECTED
    )
    ray = family is not CohortRunnerFamily.SYNC
    return CohortClaimFacts(
        ExecutionIdentity(1, "task", 1, 1),
        1,
        CohortBindingSpec(family, "0.5.0", 1 if ray else None, None if ray else PYTHON),
        CohortManagerRuntime("0.5.0", PYTHON, (2, 58, 0) if ray else None),
        "manager",
        "host",
        100,
        NOW - timedelta(seconds=10),
        encode_cohort_intent(intent),
        cohort_intent_digest(intent),
        None,
        "a" * 64,
        DIGEST,
        NOW,
        1 if ray else None,
        1 if ray else None,
        DIGEST if ray else None,
        DIGEST if ray else None,
        CohortCapabilitySnapshot(1, 1, 1, NOW - timedelta(seconds=1)) if ray else None,
        qualification() if family is CohortRunnerFamily.RAY_JOB else None,
    )


@pytest.mark.parametrize("family", list(CohortRunnerFamily))
def test_exact_roundtrip_and_digest(family):
    value = facts(family=family)
    digest = cohort_claim_facts_digest(value)
    assert (
        decode_cohort_claim_facts(encode_cohort_claim_facts(value), expected_digest=digest) == value
    )
    assert "cohort_evidence_id" not in encode_cohort_claim_facts(value)
    assert "claim_id" not in encode_cohort_claim_facts(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"binding_id": True},
        {"binding_id": 2},
        {"worker_lease_pid": True},
        {"worker_lease_pid": 1 << 31},
        {"worker_lease_id": ""},
        {"worker_lease_started_at": NOW + timedelta(seconds=1)},
        {"claimed_at": NOW.replace(tzinfo=None)},
        {"intent_digest": "sha256:" + "c" * 64},
        {"runtime_env_snapshot_digest": "bad"},
        {"manager": CohortManagerRuntime("0.5.1", PYTHON)},
        {"manager": CohortManagerRuntime("0.5.0", replace(PYTHON, patch=True))},
        {"claim_attestation_id": 1},
        {"target_policy_id": 1},
        {"manager": CohortManagerRuntime("0.5.0", PYTHON, (2, 58, 0))},
        {"identity": ExecutionIdentity(1, "task", 1, 0)},
        {"identity": ExecutionIdentity(1, "task", 1 << 31, 1)},
    ],
)
def test_invalid_sync_fact_rejected(changes):
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(replace(facts(), **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_attestation_id": None},
        {"capability": None},
        {"capability": CohortCapabilitySnapshot(1, True, 1, NOW)},
        {"capability": CohortCapabilitySnapshot(1, 1, 1, NOW + timedelta(seconds=1))},
        {"manager": CohortManagerRuntime("0.5.0", PYTHON, (2, True, 0))},
        {"binding": CohortBindingSpec(CohortRunnerFamily.RAY_CORE, "0.5.0", 1, PYTHON)},
    ],
)
def test_ray_fact_requires_exact_proof_shape(changes):
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(replace(facts(family=CohortRunnerFamily.RAY_CORE), **changes))


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text + " ",
        lambda text: text.replace('"schema_version":2', '"schema_version":true'),
        lambda text: text.replace('"schema_version":2', '"schema_version":2.0'),
        lambda text: text.replace('"schema_version":2', '"schema_version":NaN'),
        lambda text: text.replace('"schema_version":2', '"schema_version":2,"schema_version":2'),
        lambda text: text.replace('"schema_version":2', '"schema_version":2,"extra":1'),
        lambda text: text.replace('"worker_lease_pid":100', '"worker_lease_pid":' + "1" * 400),
        lambda text: "[" * 3000 + "0" + "]" * 3000,
    ],
)
def test_untrusted_json_rejected(change):
    with pytest.raises(CohortClaimError):
        decode_cohort_claim_facts(change(encode_cohort_claim_facts(facts())))


@pytest.mark.parametrize("limit", [True, 0, -1, 16385, 2])
def test_bounds_fail_closed(limit):
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(facts(), max_bytes=limit)


def test_snapshot_digest_binds_raw_storage_without_parsing_or_comparing_intent():
    values = {"profile": "local", "serialized": "encrypted:not-json", "digest": "a" * 64}
    first = cohort_task_runtime_env_snapshot_digest(**values)
    assert first != cohort_task_runtime_env_snapshot_digest(
        **(values | {"serialized": "encrypted:other"})
    )
    value = replace(facts(), runtime_env_snapshot_digest=first)
    assert decode_cohort_claim_facts(encode_cohort_claim_facts(value)) == value


def test_missing_or_substituted_schema_and_digest_fail():
    value = json.loads(encode_cohort_claim_facts(facts()))
    value.pop("claimed_at")
    with pytest.raises(CohortClaimError):
        decode_cohort_claim_facts(json.dumps(value))
    with pytest.raises(CohortClaimError):
        decode_cohort_claim_facts(encode_cohort_claim_facts(facts()), expected_digest=DIGEST)


def test_large_runtime_env_storage_retains_existing_acceptance():
    value = "\U0001f30d" * (300 * 1024)
    first = cohort_task_runtime_env_snapshot_digest(profile=None, serialized=value, digest="")
    second = cohort_task_runtime_env_snapshot_digest(
        profile=None, serialized=value[:-1] + "x", digest=""
    )
    assert first != second
    assert first != cohort_task_runtime_env_snapshot_digest(profile="", serialized=value, digest="")


def test_fresh_import_never_initializes_django_or_ray():
    script = """
import sys
class Poison:
    def find_spec(self, fullname, *args):
        if fullname == 'django' or fullname.startswith('django.') or fullname == 'ray' or fullname.startswith('ray.'):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Poison())
import django_ray.target.cohort_claim
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "changes",
    [
        {"worker_lease_hostname": "\ud800"},
        {"manager": None},
        {"manager": CohortManagerRuntime("0.5.0", replace(PYTHON, implementation="CPython"))},
        {"runtime_env_profile": "x" * 101},
        {"runtime_env_hash": "invalid"},
        {"binding": CohortBindingSpec("sync", "0.5.0", sync_python=PYTHON)},
        {"binding": CohortBindingSpec(CohortRunnerFamily.SYNC, "0.5.0")},
        {"binding": CohortBindingSpec(CohortRunnerFamily.SYNC, "v0.5.0", sync_python=PYTHON)},
        {"manager": CohortManagerRuntime("0.5.0", replace(PYTHON, patch=15))},
    ],
)
def test_additional_type_and_local_runtime_refusals(changes):
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(replace(facts(), **changes))


def test_named_profile_is_an_independent_stored_observation():
    assert (
        decode_cohort_claim_facts(
            encode_cohort_claim_facts(replace(facts(), runtime_env_profile="deployment"))
        ).runtime_env_profile
        == "deployment"
    )
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(None)
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(
            replace(
                facts(family=CohortRunnerFamily.RAY_CORE),
                manager=CohortManagerRuntime("0.5.0", PYTHON, [2, 58, 0]),
            )
        )


def test_empty_stored_runtime_env_identity_is_preserved_without_interpretation():
    value = replace(facts(), runtime_env_profile="", runtime_env_hash="")
    assert decode_cohort_claim_facts(encode_cohort_claim_facts(value)) == value


@pytest.mark.parametrize("family", [CohortRunnerFamily.SYNC, CohortRunnerFamily.RAY_CORE])
def test_jobs_only_intent_cannot_be_claimed_by_other_families(family):
    value = facts(family=family)
    intent = CohortIntent("0.5.0", "default", DIGEST, DIGEST, CohortSelectionPolicy.JOBS_ONLY)
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(
            replace(
                value,
                intent_json=encode_cohort_intent(intent),
                intent_digest=cohort_intent_digest(intent),
            )
        )


@pytest.mark.parametrize(
    "value,limit",
    [(None, 16384), ("x" * 16385, 16384), ("é" * 16384, 16384), ("{}", True)],
    ids=("type", "characters", "utf8-bytes", "boolean-limit"),
)
def test_decoder_checks_initial_byte_and_type_bounds(value, limit):
    with pytest.raises(CohortClaimError):
        decode_cohort_claim_facts(value, max_bytes=limit)


def test_decoder_rejects_wrong_schema_and_nonarray_ray_version():
    serialized = encode_cohort_claim_facts(facts(family=CohortRunnerFamily.RAY_CORE))
    for changed in (
        serialized.replace("django-ray.cohort-claim-facts", "unknown"),
        serialized.replace('"ray_version":[2,58,0]', '"ray_version":{}'),
    ):
        with pytest.raises(CohortClaimError):
            decode_cohort_claim_facts(changed)


@pytest.mark.parametrize("serialized", [False, "\ud800"])
def test_snapshot_refuses_invalid_strings_without_echoing_them(serialized):
    with pytest.raises(CohortClaimError, match="^Invalid current-cohort claim$"):
        cohort_task_runtime_env_snapshot_digest(profile=None, serialized=serialized, digest="")


@pytest.mark.parametrize(
    "change",
    [
        {"challenge_id": True},
        {"request_revision": 0},
        {"consumed_challenge_revision": 3},
        {"consumed_challenge_revision": 1 << 63},
        {"configuration_digest": "bad"},
        {"receipt_digest": "bad"},
        {"native_job_id": "ffffffff"},
        {"native_job_id": "ABCDEF00"},
        {"native_job_id": 1000000},
        {"submission_id": "other"},
        {"jobs_endpoint": "ray://head:10001"},
        {"jobs_endpoint": "http://user:password@head:8265"},
        {"jobs_endpoint": "http://head:8265/path%2Fother"},
        {"jobs_endpoint": "http://head:8265\\path"},
        {"jobs_endpoint": "http://head:8265?"},
        {"jobs_endpoint": "http://head:8265#"},
        {"jobs_endpoint": "http://head:8265\n"},
        {"jobs_endpoint": "http://héad:8265"},
        {"challenge_issued_at": NOW.replace(tzinfo=None)},
        {"challenge_expires_at": NOW},
        {"challenge_expires_at": NOW + timedelta(seconds=601)},
        {"endpoint_expires_at": NOW},
        {"endpoint_expires_at": NOW + timedelta(seconds=3601)},
        {"endpoint_observed_at": NOW - timedelta(seconds=2)},
        {"receipt_received_at": NOW - timedelta(microseconds=1)},
        {"consumed_at": NOW - timedelta(microseconds=1)},
    ],
)
def test_job_qualification_rejects_malformed_primitives_and_windows(change):
    with pytest.raises(CohortClaimError, match="^Invalid current-cohort claim$"):
        validate_cohort_job_qualification(replace(qualification(), **change))


def test_qualification_rejects_nonexact_string_and_object_types():
    class Text(str):
        pass

    with pytest.raises(CohortClaimError):
        validate_cohort_job_qualification(
            replace(qualification(), submission_id=Text(qualification().submission_id))
        )
    with pytest.raises(CohortClaimError):
        validate_cohort_job_qualification(None)


@pytest.mark.parametrize("family", [CohortRunnerFamily.SYNC, CohortRunnerFamily.RAY_CORE])
def test_other_families_cannot_carry_job_qualification(family):
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(replace(facts(family=family), job_qualification=qualification()))


@pytest.mark.parametrize(
    "change",
    [
        None,
        replace(qualification(), configuration_digest="sha256:" + "c" * 64),
        replace(qualification(), endpoint_expectation_digest="sha256:" + "c" * 64),
        replace(qualification(), challenge_issued_at=NOW - timedelta(seconds=11)),
        replace(qualification(), consumed_at=NOW + timedelta(microseconds=1)),
    ],
)
def test_job_facts_require_matching_original_context(change):
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(
            replace(facts(family=CohortRunnerFamily.RAY_JOB), job_qualification=change)
        )


@pytest.mark.parametrize("seconds", [25, 60])
def test_job_fact_claim_time_must_precede_both_original_deadlines(seconds):
    value = facts(family=CohortRunnerFamily.RAY_JOB)
    if seconds == 60:
        value = replace(
            value,
            job_qualification=replace(
                value.job_qualification, endpoint_expires_at=NOW + timedelta(seconds=120)
            ),
        )
    with pytest.raises(CohortClaimError):
        encode_cohort_claim_facts(replace(value, claimed_at=NOW + timedelta(seconds=seconds)))


def test_schema_two_preserves_historical_provenance_and_rejects_old_draft():
    original = facts(family=CohortRunnerFamily.RAY_JOB)
    serialized = encode_cohort_claim_facts(original)
    assert decode_cohort_claim_facts(serialized) == original
    # No wall-clock lookup occurs when this historical payload is decoded.
    assert original.job_qualification.endpoint_expires_at > original.claimed_at
    for change in (
        serialized.replace('"schema_version":2', '"schema_version":1'),
        serialized.replace(
            '"native_job_id":"01000000"', '"native_job_id":"01000000","native_job_id":"01000000"'
        ),
        serialized.replace('"native_job_id":"01000000"', '"native_job_id":"01000000","unknown":0'),
    ):
        with pytest.raises(CohortClaimError):
            decode_cohort_claim_facts(change)
    changed = replace(
        original, job_qualification=replace(original.job_qualification, native_job_id="02000000")
    )
    assert cohort_claim_facts_digest(changed) != cohort_claim_facts_digest(original)
