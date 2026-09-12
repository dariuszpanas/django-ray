"""Producer intent preserves declaration identity without selecting a target."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

from django_ray.target.cohort_intent import (
    COHORT_INTENT_MAX_BYTES,
    COHORT_INTENT_SCHEMA_VERSION,
    CohortExecutionDeclaration,
    CohortIntent,
    CohortIntentError,
    CohortIntentMismatch,
    CohortIntentRejection,
    CohortSelectionPolicy,
    build_cohort_intent,
    cohort_declaration_digest,
    cohort_intent_digest,
    decode_cohort_intent,
    encode_cohort_intent,
    match_cohort_intent,
)

OBSERVATION = "sha256:" + "a" * 64


def declaration(**changes) -> CohortExecutionDeclaration:
    return replace(
        CohortExecutionDeclaration("default", "auto", False),
        **changes,
    )


def intent() -> CohortIntent:
    return build_cohort_intent(
        declaration(), package_version="0.5.0", runtime_env_identity_digest=OBSERVATION
    )


def test_intent_round_trip_separates_admission_from_original_task_observation() -> None:
    value = intent()
    encoded = encode_cohort_intent(value)
    assert decode_cohort_intent(encoded, expected_digest=cohort_intent_digest(value)) == value
    assert match_cohort_intent(value, declaration(), package_version="0.5.0") is None
    assert value.selection_policy is CohortSelectionPolicy.WORKER_SELECTED
    assert {item.name for item in fields(value)} == {
        "package_version",
        "backend_alias",
        "configuration_digest",
        "runtime_env_identity_digest",
        "selection_policy",
    }
    assert json.loads(encoded)["execution_protocol_version"] == 3
    assert json.loads(encoded)["schema_version"] == COHORT_INTENT_SCHEMA_VERSION == 2
    assert "auto" not in encoded and "ray_address" not in encoded
    assert cohort_intent_digest(value) != cohort_declaration_digest(declaration())
    with pytest.raises(FrozenInstanceError):
        value.package_version = "0.6.0"


@pytest.mark.parametrize(
    "address",
    [
        "auto",
        "local",
        "localhost:6379",
        "[::1]:6379",
        "ray://host:10001",
        "http://host:8265",
        "HTTP://Host:8265/",
        "https://host:443/prefix",
    ],
)
def test_endpoint_spelling_is_validated_without_resolution_or_normalization(address: str) -> None:
    declared = declaration(ray_address=address)
    assert cohort_declaration_digest(declared).startswith("sha256:")
    assert declared.ray_address == address
    assert address not in repr(declared)


@pytest.mark.parametrize(
    "address",
    [
        "http://host:8265",
        "http://host:8265/",
        "HTTP://host:8265",
        "http://Host:8265",
        "http://host:8265/a/../b",
        "http://host:8265/b",
    ],
)
def test_semantically_distinct_endpoint_spellings_do_not_collapse(address: str) -> None:
    spellings = [
        "http://host:8265",
        "http://host:8265/",
        "HTTP://host:8265",
        "http://Host:8265",
        "http://host:8265/a/../b",
        "http://host:8265/b",
    ]
    digest = cohort_declaration_digest(declaration(ray_address=address))
    for other in spellings:
        if other != address:
            assert cohort_declaration_digest(declaration(ray_address=other)) != digest


@pytest.mark.parametrize(
    ("changes", "package", "reason"),
    [
        ({}, "0.6.0", CohortIntentMismatch.PACKAGE_VERSION),
        ({"backend_alias": "other"}, "0.5.0", CohortIntentMismatch.BACKEND_ALIAS),
        ({"ray_job_only": True}, "0.5.0", CohortIntentMismatch.SELECTION_POLICY),
        ({"ray_address": "http://another:8265"}, "0.5.0", CohortIntentMismatch.CONFIGURATION),
        (
            {"trust_identity": {"trust_domain": "another"}},
            "0.5.0",
            CohortIntentMismatch.CONFIGURATION,
        ),
    ],
)
def test_declaration_or_package_drift_returns_fixed_mismatch(changes, package, reason) -> None:
    assert match_cohort_intent(intent(), declaration(**changes), package_version=package) is reason


def test_jobs_only_is_derived_from_explicit_existing_backend_policy() -> None:
    value = build_cohort_intent(
        declaration(ray_job_only=True),
        package_version="0.5.0",
        runtime_env_identity_digest=OBSERVATION,
    )
    assert value.selection_policy is CohortSelectionPolicy.JOBS_ONLY
    assert decode_cohort_intent(encode_cohort_intent(value)) == value
    assert (
        match_cohort_intent(value, declaration(), package_version="0.5.0")
        is CohortIntentMismatch.SELECTION_POLICY
    )


def test_per_task_observation_does_not_partition_manager_admission() -> None:
    original = intent()
    other = build_cohort_intent(
        declaration(), package_version="0.5.0", runtime_env_identity_digest="sha256:" + "b" * 64
    )
    assert original.configuration_digest == other.configuration_digest
    assert original.runtime_env_identity_digest != other.runtime_env_identity_digest
    assert cohort_intent_digest(original) != cohort_intent_digest(other)
    for value in (original, other):
        assert match_cohort_intent(value, declaration(), package_version="0.5.0") is None
        assert (
            match_cohort_intent(
                value,
                declaration(),
                package_version="0.5.0",
                expected_runtime_env_identity_digest=value.runtime_env_identity_digest,
            )
            is None
        )
    assert (
        match_cohort_intent(
            original,
            declaration(),
            package_version="0.5.0",
            expected_runtime_env_identity_digest=other.runtime_env_identity_digest,
        )
        is CohortIntentMismatch.RUNTIME_ENV_OBSERVATION
    )


def test_trust_uses_existing_normalization_without_mandatory_revisions() -> None:
    first = declaration(trust_identity={"trust_domain": "café", "credential_profile": "app"})
    equivalent = declaration(
        trust_identity={"credential_profile": "app", "trust_domain": "cafe\u0301"}
    )
    assert cohort_declaration_digest(first) == cohort_declaration_digest(equivalent)
    assert cohort_declaration_digest(declaration()) == cohort_declaration_digest(
        declaration(trust_identity={"environment_revision": None})
    )
    value = build_cohort_intent(
        first, package_version="0.5.0", runtime_env_identity_digest=OBSERVATION
    )
    assert match_cohort_intent(value, equivalent, package_version="0.5.0") is None
    assert "café" not in repr(first)
    wire = json.loads(encode_cohort_intent(value))
    assert "trust_identity" not in wire and "trust_domain" not in wire
    assert (
        match_cohort_intent(
            value,
            declaration(trust_identity={"trust_domain": "changed", "credential_profile": "app"}),
            package_version="0.5.0",
        )
        is CohortIntentMismatch.CONFIGURATION
    )


@pytest.mark.parametrize(
    "spec,reusable",
    [
        ({"pip": ["unpinned-package"]}, False),
        ({"worker_process_setup_hook": "application.worker_only_hook"}, True),
        ({"application_plugin": {"runtime_selected": True}}, False),
    ],
)
def test_dynamic_runtime_env_observation_needs_no_reusable_identity(spec, reusable) -> None:
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.workflow.plans import runtime_env_plan_identity

    logical = runtime_env_plan_identity(normalize_runtime_env(spec))
    assert logical.reusable is reusable
    original_digest = logical.manifest["digest"]
    value = build_cohort_intent(
        declaration(), package_version="0.5.0", runtime_env_identity_digest=original_digest
    )
    assert value.runtime_env_identity_digest == original_digest
    assert value.configuration_digest == intent().configuration_digest
    assert match_cohort_intent(value, declaration(), package_version="0.5.0") is None


@pytest.mark.parametrize("digest", [None, "", "a" * 64, "sha256:" + "A" * 64, {"secret": "x"}])
def test_original_observation_must_be_a_canonical_digest(digest) -> None:
    with pytest.raises(CohortIntentError) as caught:
        build_cohort_intent(
            declaration(), package_version="0.5.0", runtime_env_identity_digest=digest
        )
    assert caught.value.classification is CohortIntentRejection.INVALID
    with pytest.raises(CohortIntentError):
        encode_cohort_intent(replace(intent(), runtime_env_identity_digest=digest))


def test_old_dormant_wire_cannot_be_reinterpreted_as_finite_admission() -> None:
    old_wire = json.loads(encode_cohort_intent(intent()))
    old_wire["schema_version"] = 1
    del old_wire["runtime_env_identity_digest"]
    with pytest.raises(CohortIntentError) as caught:
        decode_cohort_intent(json.dumps(old_wire, sort_keys=True, separators=(",", ":")))
    assert caught.value.classification is CohortIntentRejection.UNSUPPORTED_SCHEMA


@pytest.mark.parametrize(
    "changes",
    [
        {"backend_alias": ""},
        {"backend_alias": "a" * 129},
        {"backend_alias": "bad alias"},
        {"ray_job_only": 1},
        {"ray_job_only": "false"},
        {"trust_identity": "private-value"},
        {"trust_identity": {"encrypted": "private-value"}},
        {"trust_identity": {"trust_domain": ""}},
        {"trust_identity": {"trust_domain": "x" * 257}},
        {"trust_identity": {"trust_domain": {"nested": "private-value"}}},
        {"ray_address": ""},
        {"ray_address": "a" * 256},
        {"ray_address": "http://host\n"},
        {"ray_address": "http://host:invalid"},
        {"ray_address": "http://host:65536"},
        {"ray_address": "http://host:0"},
        {"ray_address": "http://user:private-value@host:8265"},
        {"ray_address": "http://host:8265?token=private-value"},
        {"ray_address": "http://host:8265#private-value"},
        {"ray_address": "http://"},
        {"ray_address": "http://host/\ud800"},
    ],
)
def test_invalid_or_credential_bearing_declarations_fail_without_echo(changes) -> None:
    with pytest.raises(CohortIntentError) as caught:
        build_cohort_intent(
            declaration(**changes), package_version="0.5.0", runtime_env_identity_digest=OBSERVATION
        )
    assert str(caught.value) == "Cohort intent rejected: invalid"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("version", [None, "", "v0.5.0", "0.5.0-1", "0.5.0+UPPER", "private-value"])
def test_package_version_must_be_exact_canonical_pep440(version) -> None:
    with pytest.raises(CohortIntentError):
        build_cohort_intent(
            declaration(), package_version=version, runtime_env_identity_digest=OBSERVATION
        )


@pytest.mark.parametrize(
    "transform",
    [
        lambda wire: wire.update(schema_version=True),
        lambda wire: wire.update(execution_protocol_version=1),
        lambda wire: wire.update(execution_protocol_version=3.0),
        lambda wire: wire.update(extra="private-value"),
        lambda wire: wire.pop("package_version"),
        lambda wire: wire.update(configuration_digest="sha256:" + "A" * 64),
        lambda wire: wire.update(runtime_env_identity_digest="sha256:" + "A" * 64),
        lambda wire: wire.pop("runtime_env_identity_digest"),
        lambda wire: wire.update(selection_policy="ray_core"),
        lambda wire: wire.update(backend_alias={"nested": "private-value"}),
    ],
)
def test_decoder_rejects_legacy_malformed_or_extended_intent(transform) -> None:
    wire = json.loads(encode_cohort_intent(intent()))
    transform(wire)
    with pytest.raises(CohortIntentError) as caught:
        decode_cohort_intent(json.dumps(wire, sort_keys=True, separators=(",", ":")))
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("serialized", [None, b"{}", "[]", "null", "NaN", "{", "\ud800"])
def test_decoder_rejects_non_object_input_with_fixed_errors(serialized) -> None:
    with pytest.raises(CohortIntentError):
        decode_cohort_intent(serialized)


def test_decoder_rejects_duplicates_noncanonical_json_and_wrong_independent_digest() -> None:
    encoded = encode_cohort_intent(intent())
    duplicate = '{"schema_version":1,' + encoded[1:]
    for invalid in (duplicate, " " + encoded, json.dumps(json.loads(encoded), indent=2)):
        with pytest.raises(CohortIntentError):
            decode_cohort_intent(invalid)
    with pytest.raises(CohortIntentError) as caught:
        decode_cohort_intent(encoded, expected_digest="sha256:" + "0" * 64)
    assert caught.value.classification is CohortIntentRejection.DIGEST_MISMATCH


@pytest.mark.parametrize("limit", [0, -1, True, 1, COHORT_INTENT_MAX_BYTES + 1])
def test_codec_byte_limit_can_only_be_lowered_and_is_enforced(limit) -> None:
    encoded = encode_cohort_intent(intent())
    with pytest.raises(CohortIntentError) as caught:
        encode_cohort_intent(intent(), max_bytes=limit)
    assert caught.value.classification is CohortIntentRejection.RESOURCE_LIMIT
    with pytest.raises(CohortIntentError) as caught:
        decode_cohort_intent(encoded, max_bytes=limit)
    assert caught.value.classification is CohortIntentRejection.RESOURCE_LIMIT


def test_byte_limit_exact_boundary_and_oversized_untrusted_input() -> None:
    value = intent()
    encoded = encode_cohort_intent(value)
    assert decode_cohort_intent(encoded, max_bytes=len(encoded)) == value
    with pytest.raises(CohortIntentError) as caught:
        decode_cohort_intent(" " * (COHORT_INTENT_MAX_BYTES + 1))
    assert caught.value.classification is CohortIntentRejection.RESOURCE_LIMIT


def test_intent_module_does_not_import_django_ray_runtime_or_application_code() -> None:
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'ray', 'testproject'}:
            raise RuntimeError('forbidden runtime import')
sys.meta_path.insert(0, NoRuntime())
from django_ray.target.cohort_intent import CohortExecutionDeclaration, build_cohort_intent
build_cohort_intent(CohortExecutionDeclaration('default', 'auto', False), package_version='0.5.0', runtime_env_identity_digest='sha256:' + 'a' * 64)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
