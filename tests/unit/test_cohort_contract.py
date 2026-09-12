from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from django_ray.execution_codec import ExecutionIdentity
from django_ray.execution_protocol import (
    EXECUTION_PROTOCOL_VERSION,
    TARGET_EXECUTION_PROTOCOL_VERSION,
)
from django_ray.target.attestation import (
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    ray_target_expectation_digest,
)
from django_ray.target.cohort_contract import (
    COHORT_CONTRACT_MAX_BYTES,
    COHORT_EXECUTION_PROTOCOL_VERSION,
    COHORT_LEAF_MAX_BYTES,
    CohortContractError,
    CohortContractRejection,
    CohortExecutionContract,
    CohortRuntimeMismatch,
    cohort_execution_contract_digest,
    cohort_leaf_contract_digest,
    compare_cohort_runtime,
    decode_cohort_execution_contract,
    decode_cohort_leaf_contract,
    derive_cohort_leaf_contract,
    encode_cohort_execution_contract,
    encode_cohort_leaf_contract,
)

NOW = datetime(2020, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def contract(*, family=RayRunnerFamily.RAY_CORE, nodes=2):
    runtime = RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14)
    expectation = RayTargetExpectation("current", family, "session_current", 1, runtime)
    versions = tuple(RayNodeStateVersion(f"{number:056x}", 1) for number in range(1, nodes + 1))
    boundary = build_ray_observation_boundary(
        resource_state_version_before=1,
        resource_state_version_after=2,
        node_state_versions_before=versions,
        node_state_versions_after=versions,
    )
    attestation = build_ray_cluster_attestation(
        expectation=expectation,
        boundary=boundary,
        nodes=tuple(
            build_ray_node_observation(
                node_id=item.node_id, cluster_session=expectation.cluster_session, runtime=runtime
            )
            for item in versions
        ),
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )
    return CohortExecutionContract(
        identity=ExecutionIdentity(1, "task-identity", 1, 1),
        expected_django_ray_version="0.5.0",
        target_binding_id=2,
        cohort_evidence_id=3,
        cohort_evidence_digest=DIGEST,
        claimed_at=NOW + timedelta(seconds=1),
        target_expectation=expectation,
        target_expectation_digest=ray_target_expectation_digest(expectation),
        claim_attestation=attestation,
        claim_attestation_digest=attestation.attestation_digest,
    )


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def bindings(value):
    return {
        "expected_identity": value.identity,
        "expected_target_binding_id": value.target_binding_id,
        "expected_cohort_evidence_id": value.cohort_evidence_id,
        "expected_cohort_evidence_digest": value.cohort_evidence_digest,
        "expected_claimed_at": value.claimed_at,
        "expected_target_expectation_digest": value.target_expectation_digest,
        "expected_claim_attestation_digest": value.claim_attestation_digest,
    }


@pytest.mark.parametrize("family", list(RayRunnerFamily))
def test_canonical_outer_and_compact_leaf_roundtrip_both_runner_families(family):
    value = contract(family=family)
    wire = encode_cohort_execution_contract(value)
    digest = cohort_execution_contract_digest(value)
    assert (
        decode_cohort_execution_contract(wire, **bindings(value), expected_contract_digest=digest)
        == value
    )
    assert (
        digest
        == "sha256:"
        + hashlib.sha256(b"django-ray/cohort-execution-contract/v3\x00" + wire.encode()).hexdigest()
    )
    leaf = derive_cohort_leaf_contract(value)
    leaf_wire = encode_cohort_leaf_contract(leaf)
    assert "claim_attestation" not in json.loads(leaf_wire)
    assert leaf.outer_contract_digest == digest
    assert leaf.claim_membership_digest == value.claim_attestation.membership_digest
    assert (
        decode_cohort_leaf_contract(
            leaf_wire,
            **bindings(value),
            expected_claim_membership_digest=value.claim_attestation.membership_digest,
            expected_outer_contract_digest=digest,
            expected_contract_digest=cohort_leaf_contract_digest(leaf),
        )
        == leaf
    )
    assert (
        cohort_leaf_contract_digest(leaf)
        == "sha256:"
        + hashlib.sha256(b"django-ray/cohort-leaf-contract/v3\x00" + leaf_wire.encode()).hexdigest()
    )
    assert cohort_leaf_contract_digest(leaf) != digest


def test_full_membership_is_not_duplicated_into_every_leaf():
    value = contract(nodes=256)
    assert len(encode_cohort_execution_contract(value).encode()) > COHORT_LEAF_MAX_BYTES
    leaf = derive_cohort_leaf_contract(value)
    assert len(encode_cohort_leaf_contract(leaf).encode()) < 4096
    assert leaf.claim_attestation_digest == value.claim_attestation_digest
    assert leaf.claim_membership_digest == value.claim_attestation.membership_digest


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_binding_id", True),
        ("target_binding_id", 0),
        ("cohort_evidence_id", -1),
        ("cohort_evidence_id", RAY_TARGET_ATTESTATION_MAX_COUNTER + 1),
        ("cohort_evidence_digest", "sha256:" + "A" * 64),
        ("cohort_evidence_digest", "secret-canary"),
        ("expected_django_ray_version", "v0.5.0"),
        ("expected_django_ray_version", "not-a-version"),
        ("expected_django_ray_version", "0.5.0\n"),
        ("expected_django_ray_version", "1" * 129),
        ("claimed_at", NOW.replace(tzinfo=None)),
        ("claimed_at", NOW.astimezone(timezone(timedelta(hours=1)))),
        ("claimed_at", NOW - timedelta(microseconds=1)),
        ("claimed_at", NOW + timedelta(seconds=60)),
    ],
)
def test_malformed_local_fields_and_claim_window_fail_without_echo(field, value):
    with pytest.raises(CohortContractError) as error:
        encode_cohort_execution_contract(replace(contract(), **{field: value}))
    assert "secret-canary" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_execution_pk", 0),
        ("attempt_number", True),
        ("execution_generation", 0),
        ("execution_generation", RAY_TARGET_ATTESTATION_MAX_COUNTER + 1),
        ("task_id", "\x00"),
    ],
)
def test_existing_identity_contract_is_reused_with_positive_claim_generation(field, value):
    original = contract()
    with pytest.raises(CohortContractError):
        encode_cohort_execution_contract(
            replace(original, identity=replace(original.identity, **{field: value}))
        )


@pytest.mark.parametrize("field", ["target_expectation_digest", "claim_attestation_digest"])
def test_derived_digest_mismatch_is_rejected(field):
    with pytest.raises(CohortContractError) as error:
        encode_cohort_execution_contract(replace(contract(), **{field: OTHER_DIGEST}))
    assert error.value.classification is CohortContractRejection.DIGEST_MISMATCH


def test_attestation_from_another_expectation_is_not_a_valid_claim():
    original = contract()
    other = contract(family=RayRunnerFamily.RAY_JOB)
    with pytest.raises(CohortContractError) as error:
        encode_cohort_execution_contract(
            replace(
                original,
                claim_attestation=other.claim_attestation,
                claim_attestation_digest=other.claim_attestation_digest,
            )
        )
    assert error.value.classification is CohortContractRejection.EXPECTATION_MISMATCH


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "extra",
        "duplicate",
        "nested_duplicate",
        "pretty",
        "prefix",
        "schema_bool",
        "protocol1",
        "protocol2",
        "float",
        "timestamp",
        "unicode",
        "deep",
        "big_integer",
        "surrogate",
    ],
)
def test_wire_parser_rejects_noncanonical_or_ambiguous_data(damage):
    wire = encode_cohort_execution_contract(contract())
    value = json.loads(wire)
    if damage == "missing":
        value.pop("cohort_evidence_digest")
    elif damage == "extra":
        value["unexpected"] = True
    elif damage == "duplicate":
        wire = wire.replace('"target_binding_id":2', '"target_binding_id":2,"target_binding_id":2')
    elif damage == "nested_duplicate":
        wire = wire.replace('"ray_major":2', '"ray_major":2,"ray_major":2', 1)
    elif damage == "pretty":
        wire = json.dumps(value, indent=2)
    elif damage == "prefix":
        wire = " " + wire
    elif damage == "schema_bool":
        value["schema_version"] = True
    elif damage.startswith("protocol"):
        value["execution_protocol_version"] = int(damage[-1])
    elif damage == "float":
        value["target_binding_id"] = 2.0
    elif damage == "timestamp":
        value["claimed_at"] = "2020-01-02T03:04:06.123456+00:00"
    elif damage == "unicode":
        wire = wire.replace('"task-identity"', '"task-\\u0069dentity"')
    elif damage == "deep":
        wire = "[" * 17 + "0" + "]" * 17
    elif damage == "big_integer":
        wire = wire.replace('"target_binding_id":2', '"target_binding_id":' + "1" * 20)
    elif damage == "surrogate":
        wire += "\ud800"
    if damage in {
        "missing",
        "extra",
        "schema_bool",
        "protocol1",
        "protocol2",
        "float",
        "timestamp",
    }:
        wire = canonical(value)
    with pytest.raises(CohortContractError):
        decode_cohort_execution_contract(wire)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_identity", ExecutionIdentity(1, "task-identity", 1, 2)),
        ("expected_target_binding_id", 4),
        ("expected_cohort_evidence_id", 4),
        ("expected_cohort_evidence_digest", OTHER_DIGEST),
        ("expected_claimed_at", NOW + timedelta(seconds=2)),
        ("expected_target_expectation_digest", OTHER_DIGEST),
        ("expected_claim_attestation_digest", OTHER_DIGEST),
        ("expected_contract_digest", OTHER_DIGEST),
    ],
)
def test_independently_held_bindings_reject_stale_or_substituted_claims(field, value):
    with pytest.raises(CohortContractError) as error:
        decode_cohort_execution_contract(
            encode_cohort_execution_contract(contract()), **{field: value}
        )
    assert error.value.classification is CohortContractRejection.BINDING_MISMATCH


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_django_ray_version": "0.4.0"},
        {"claim_membership_digest": OTHER_DIGEST},
    ],
)
def test_leaf_cannot_change_runtime_expectation_while_echoing_outer_digest(changes):
    leaf = derive_cohort_leaf_contract(contract())
    changed = replace(leaf, **changes)
    with pytest.raises(CohortContractError) as error:
        decode_cohort_leaf_contract(
            encode_cohort_leaf_contract(changed),
            **bindings(leaf),
            expected_outer_contract_digest=leaf.outer_contract_digest,
            expected_contract_digest=cohort_leaf_contract_digest(leaf),
        )
    assert error.value.classification is CohortContractRejection.BINDING_MISMATCH


def test_leaf_membership_binding_is_required_and_independently_checkable():
    leaf = derive_cohort_leaf_contract(contract())
    wire = encode_cohort_leaf_contract(leaf)
    with pytest.raises(CohortContractError) as error:
        decode_cohort_leaf_contract(wire, expected_claim_membership_digest=OTHER_DIGEST)
    assert error.value.classification is CohortContractRejection.BINDING_MISMATCH
    value = json.loads(wire)
    value.pop("claim_membership_digest")
    with pytest.raises(CohortContractError):
        decode_cohort_leaf_contract(canonical(value))
    for malformed in [None, "secret-canary", "sha256:" + "A" * 64]:
        with pytest.raises(CohortContractError):
            encode_cohort_leaf_contract(replace(leaf, claim_membership_digest=malformed))


def test_claim_window_accepts_observation_time_and_last_microsecond_before_expiry():
    original = contract()
    for claimed_at in [NOW, original.claim_attestation.expires_at - timedelta(microseconds=1)]:
        value = replace(original, claimed_at=claimed_at)
        assert decode_cohort_execution_contract(encode_cohort_execution_contract(value)) == value


def test_leaf_and_outer_domains_are_not_interchangeable():
    value = contract()
    leaf = derive_cohort_leaf_contract(value)
    for decode, wire in [
        (decode_cohort_leaf_contract, encode_cohort_execution_contract(value)),
        (decode_cohort_execution_contract, encode_cohort_leaf_contract(leaf)),
    ]:
        with pytest.raises(CohortContractError):
            decode(wire)
    with pytest.raises(CohortContractError):
        decode_cohort_leaf_contract(
            encode_cohort_leaf_contract(leaf), expected_outer_contract_digest=OTHER_DIGEST
        )


@pytest.mark.parametrize("leaf", [False, True])
def test_byte_budget_can_be_lowered_but_not_disabled_or_increased(leaf):
    value = derive_cohort_leaf_contract(contract()) if leaf else contract()
    encode = encode_cohort_leaf_contract if leaf else encode_cohort_execution_contract
    decode = decode_cohort_leaf_contract if leaf else decode_cohort_execution_contract
    ceiling = COHORT_LEAF_MAX_BYTES if leaf else COHORT_CONTRACT_MAX_BYTES
    wire = encode(value)
    size = len(wire.encode())
    assert decode(wire, max_bytes=size) == value
    for budget in [False, 0, -1, size - 1, ceiling + 1, 1.0]:
        with pytest.raises(CohortContractError) as error:
            decode(wire, max_bytes=budget)
        assert error.value.classification is CohortContractRejection.RESOURCE_LIMIT


@pytest.mark.parametrize("leaf", [False, True])
@pytest.mark.parametrize(
    "change,reason",
    [
        ({}, None),
        ({"actual_django_ray_version": "0.4.0"}, CohortRuntimeMismatch.PACKAGE_VERSION_MISMATCH),
        (
            {"actual_cluster_session": "session_replacement"},
            CohortRuntimeMismatch.CLUSTER_SESSION_MISMATCH,
        ),
        ({"ray_patch": 1}, CohortRuntimeMismatch.RAY_VERSION_MISMATCH),
        ({"python_implementation": "pypy"}, CohortRuntimeMismatch.PYTHON_IMPLEMENTATION_MISMATCH),
        ({"python_patch": 15}, CohortRuntimeMismatch.PYTHON_VERSION_MISMATCH),
    ],
)
def test_runtime_compare_is_exact_and_does_not_expire_a_valid_historical_claim(
    leaf, change, reason
):
    value = derive_cohort_leaf_contract(contract()) if leaf else contract()
    observation = {
        "actual_django_ray_version": "0.5.0",
        "actual_runtime": value.target_expectation.runtime,
        "actual_cluster_session": "session_current",
    }
    for key, item in change.items():
        if key.startswith("actual_"):
            observation[key] = item
        else:
            observation["actual_runtime"] = replace(observation["actual_runtime"], **{key: item})
    assert compare_cohort_runtime(value, **observation) is reason
    assert "application_invoked" not in {field.name for field in fields(value)}


@pytest.mark.parametrize(
    "field,value",
    [
        ("actual_django_ray_version", None),
        ("actual_cluster_session", "http://ray-head:8265"),
        ("actual_runtime", None),
        ("actual_runtime", RayRuntimeVersion(True, 58, 0, "cpython", 3, 12, 14)),
    ],
)
def test_malformed_observation_is_uncertainty_not_a_runtime_mismatch(field, value):
    original = contract()
    observation = {
        "actual_django_ray_version": "0.5.0",
        "actual_runtime": original.target_expectation.runtime,
        "actual_cluster_session": "session_current",
    }
    observation[field] = value
    with pytest.raises(CohortContractError) as error:
        compare_cohort_runtime(original, **observation)
    assert error.value.classification is CohortContractRejection.INVALID_OBSERVATION


def test_module_import_needs_neither_django_nor_ray_and_does_not_activate_protocol():
    root = Path(__file__).resolve().parents[2]
    code = """
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name.split('.')[0] in {'django', 'ray'}:
        raise AssertionError('forbidden framework import')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
import django_ray.target.cohort_contract
assert 'django' not in sys.modules and 'ray' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root / "src")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert COHORT_EXECUTION_PROTOCOL_VERSION == 3
    assert EXECUTION_PROTOCOL_VERSION == 1
    assert TARGET_EXECUTION_PROTOCOL_VERSION == 2
