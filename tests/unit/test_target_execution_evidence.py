"""Tests for canonical protocol-2 target execution claim evidence."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, tzinfo

import pytest

from django_ray.target_execution_evidence import (
    RayTaskTargetExecutionEvidenceClaim,
    RayTaskTargetExecutionEvidenceError,
    decode_ray_task_target_execution_evidence,
    encode_ray_task_target_execution_evidence,
    ray_task_target_execution_evidence_digest,
)

_DIGEST_ONE = f"sha256:{'1' * 64}"
_DIGEST_TWO = f"sha256:{'2' * 64}"
_EXPECTED_DIGEST = "sha256:8d7db5ace90cddbb0eea94ca6fd727ae72a8b8dd999342bd041853da80a53a65"


class _HostileTimezone(tzinfo):
    def utcoffset(self, _value: datetime | None) -> timedelta:
        raise RuntimeError("hostile timezone")

    def dst(self, _value: datetime | None) -> timedelta:
        raise RuntimeError("hostile timezone")


def _claim() -> RayTaskTargetExecutionEvidenceClaim:
    return RayTaskTargetExecutionEvidenceClaim(
        execution_id=17,
        task_id="task-386",
        attempt_number=2,
        execution_generation=5,
        route_selection_id=17,
        route_backend_alias="default",
        route_revision_id=23,
        route_revision=4,
        selected_target_policy_id=31,
        target_id="blue.ray-core",
        target_policy_id=37,
        claim_attestation_id=41,
        target_expectation_digest=_DIGEST_ONE,
        claim_attestation_digest=_DIGEST_TWO,
        worker_target_capability_id=43,
        worker_target_capability_schema_version=1,
        worker_target_capability_revision=7,
        worker_target_capability_advertised_at=datetime(2026, 8, 15, 20, tzinfo=UTC),
        worker_lease_id="worker-386",
        worker_lease_hostname="worker.example",
        worker_lease_pid=1234,
        worker_lease_started_at=datetime(2026, 8, 15, 19, 59, tzinfo=UTC),
        runner_family="ray_core",
        manager_ray_major=2,
        manager_ray_minor=56,
        manager_ray_patch=0,
        manager_python_implementation="cpython",
        manager_python_major=3,
        manager_python_minor=12,
        manager_python_patch=12,
        claimed_at=datetime(2026, 8, 15, 20, 0, 1, tzinfo=UTC),
    )


def test_claim_evidence_is_canonical_round_trippable_and_domain_separated() -> None:
    claim = _claim()
    encoded = encode_ray_task_target_execution_evidence(claim)

    assert decode_ray_task_target_execution_evidence(encoded) == claim
    assert ray_task_target_execution_evidence_digest(claim) == _EXPECTED_DIGEST
    assert "target_execution_evidence_id" not in encoded
    assert encoded == json.dumps(
        json.loads(encoded),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_every_mutable_claim_input_changes_the_digest() -> None:
    claim = _claim()
    mutations = {
        "execution_id": 18,
        "task_id": "task-386-other",
        "attempt_number": 3,
        "execution_generation": 6,
        "route_selection_id": 18,
        "route_backend_alias": "other",
        "route_revision_id": 24,
        "route_revision": 5,
        "selected_target_policy_id": 32,
        "target_id": "green.ray-core",
        "target_policy_id": 38,
        "claim_attestation_id": 42,
        "target_expectation_digest": f"sha256:{'3' * 64}",
        "claim_attestation_digest": f"sha256:{'4' * 64}",
        "worker_target_capability_id": 44,
        "worker_target_capability_revision": 8,
        "worker_target_capability_advertised_at": (
            claim.worker_target_capability_advertised_at + timedelta(microseconds=1)
        ),
        "worker_lease_id": "worker-387",
        "worker_lease_hostname": "worker-2.example",
        "worker_lease_pid": 1235,
        "worker_lease_started_at": claim.worker_lease_started_at + timedelta(microseconds=1),
        "manager_ray_major": 3,
        "manager_ray_minor": 57,
        "manager_ray_patch": 1,
        "manager_python_implementation": "pypy",
        "manager_python_major": 4,
        "manager_python_minor": 13,
        "manager_python_patch": 13,
        "claimed_at": claim.claimed_at + timedelta(microseconds=1),
    }
    covered = set(mutations) | {
        "worker_target_capability_schema_version",
        "runner_family",
        "schema_version",
        "execution_protocol_version",
    }
    assert covered == {field.name for field in fields(claim)}

    original = ray_task_target_execution_evidence_digest(claim)
    for field_name, value in mutations.items():
        changed = replace(claim, **{field_name: value})
        assert ray_task_target_execution_evidence_digest(changed) != original, field_name


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("worker_target_capability_schema_version", 2),
        ("schema_version", 2),
        ("execution_protocol_version", 1),
        ("attempt_number", True),
        ("attempt_number", 2_147_483_648),
        ("execution_generation", 0),
        ("target_id", "Blue"),
        ("runner_family", "ray_job"),
        ("worker_lease_pid", 2_147_483_648),
        ("manager_python_implementation", "CPython"),
        ("claimed_at", datetime(2026, 8, 15, 20, 0, 1)),
    ),
)
def test_claim_evidence_rejects_invalid_fields(field_name: str, value: object) -> None:
    with pytest.raises(RayTaskTargetExecutionEvidenceError):
        encode_ray_task_target_execution_evidence(replace(_claim(), **{field_name: value}))


@pytest.mark.parametrize("field_name", ("task_id", "worker_lease_id", "worker_lease_hostname"))
@pytest.mark.parametrize("value", ("é" * 255, "e\u0301\n"))
def test_identity_text_preserves_existing_character_contract(field_name: str, value: str) -> None:
    claim = replace(_claim(), **{field_name: value})

    assert (
        decode_ray_task_target_execution_evidence(encode_ray_task_target_execution_evidence(claim))
        == claim
    )


@pytest.mark.parametrize("field_name", ("task_id", "worker_lease_id", "worker_lease_hostname"))
@pytest.mark.parametrize("value", ("", "x" * 256, "nul\x00value", "\ud800"))
def test_identity_text_rejects_only_out_of_contract_values(field_name: str, value: str) -> None:
    with pytest.raises(RayTaskTargetExecutionEvidenceError):
        encode_ray_task_target_execution_evidence(replace(_claim(), **{field_name: value}))


def test_claim_evidence_rejects_capability_advertised_before_lease_start() -> None:
    claim = _claim()

    with pytest.raises(RayTaskTargetExecutionEvidenceError):
        encode_ray_task_target_execution_evidence(
            replace(
                claim,
                worker_target_capability_advertised_at=(
                    claim.worker_lease_started_at - timedelta(microseconds=1)
                ),
            )
        )


def test_decoder_rejects_noncanonical_unknown_and_duplicate_content() -> None:
    encoded = encode_ray_task_target_execution_evidence(_claim())
    decoded = json.loads(encoded)

    with pytest.raises(RayTaskTargetExecutionEvidenceError):
        decode_ray_task_target_execution_evidence(json.dumps(decoded))

    decoded["unknown"] = 1
    with pytest.raises(RayTaskTargetExecutionEvidenceError):
        decode_ray_task_target_execution_evidence(
            json.dumps(decoded, sort_keys=True, separators=(",", ":"))
        )

    duplicate = encoded[:-1] + ',"task_id":"duplicate"}'
    with pytest.raises(RayTaskTargetExecutionEvidenceError):
        decode_ray_task_target_execution_evidence(duplicate)


def test_codec_maps_hostile_timezone_and_recursive_json_to_fixed_error() -> None:
    hostile = datetime(2026, 8, 15, 20, tzinfo=_HostileTimezone())
    with pytest.raises(
        RayTaskTargetExecutionEvidenceError,
        match="Ray task target execution evidence is invalid",
    ):
        encode_ray_task_target_execution_evidence(replace(_claim(), claimed_at=hostile))

    nested = "[" * 2000 + "]" * 2000
    with pytest.raises(RayTaskTargetExecutionEvidenceError):
        decode_ray_task_target_execution_evidence(nested)
