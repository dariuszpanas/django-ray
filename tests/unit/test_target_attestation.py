from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from django_ray.target_attestation import (
    RAY_CLUSTER_ATTESTATION_MAX_BYTES,
    RAY_CLUSTER_ATTESTATION_SCHEMA,
    RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
    RAY_NODE_ID_HEX_CHARS,
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RAY_TARGET_ATTESTATION_MAX_NODES,
    RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS,
    RAY_TARGET_EXPECTATION_MAX_BYTES,
    RAY_TARGET_EXPECTATION_SCHEMA,
    RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
    RayClusterAttestation,
    RayNodeObservation,
    RayNodeStateVersion,
    RayObservationBoundary,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetAttestationEncodeError,
    RayTargetAttestationError,
    RayTargetAttestationRejection,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    compare_ray_target_attestation,
    decode_ray_cluster_attestation,
    decode_ray_target_expectation,
    encode_ray_cluster_attestation,
    encode_ray_target_expectation,
    ray_cluster_attestation_digest,
    ray_membership_digest,
    ray_node_observation_digest,
    ray_target_expectation_digest,
)

NODE_A = "1" * RAY_NODE_ID_HEX_CHARS
NODE_B = "a" * RAY_NODE_ID_HEX_CHARS
NODE_C = "f" * RAY_NODE_ID_HEX_CHARS
SESSION = "session_2026-08-15_12-00-00_123456_1"
OBSERVED_AT = datetime(2026, 8, 15, 19, 0, 0, 123456, tzinfo=UTC)


def _runtime(**changes: object) -> RayRuntimeVersion:
    values: dict[str, object] = {
        "ray_major": 2,
        "ray_minor": 56,
        "ray_patch": 0,
        "python_implementation": "cpython",
        "python_major": 3,
        "python_minor": 12,
        "python_patch": 12,
    }
    values.update(changes)
    return RayRuntimeVersion(**values)  # type: ignore[arg-type]


def _expectation(**changes: object) -> RayTargetExpectation:
    values: dict[str, object] = {
        "target_key": "primary.ray",
        "runner_family": RayRunnerFamily.RAY_CORE,
        "cluster_session": SESSION,
        "policy_revision": 7,
        "runtime": _runtime(),
    }
    values.update(changes)
    return RayTargetExpectation(**values)  # type: ignore[arg-type]


def _versions(
    *values: tuple[str, int],
) -> tuple[RayNodeStateVersion, ...]:
    return tuple(
        RayNodeStateVersion(node_id=node_id, node_state_version=version)
        for node_id, version in values
    )


def _boundary(
    *,
    before_resource: int = 100,
    after_resource: int = 104,
    before: tuple[RayNodeStateVersion, ...] | None = None,
    after: tuple[RayNodeStateVersion, ...] | None = None,
) -> RayObservationBoundary:
    if before is None:
        before = _versions((NODE_A, 10), (NODE_B, 20))
    if after is None:
        after = _versions((NODE_A, 12), (NODE_B, 21))
    return build_ray_observation_boundary(
        resource_state_version_before=before_resource,
        resource_state_version_after=after_resource,
        node_state_versions_before=before,
        node_state_versions_after=after,
    )


def _nodes(
    expectation: RayTargetExpectation | None = None,
) -> tuple[RayNodeObservation, ...]:
    if expectation is None:
        expectation = _expectation()
    return tuple(
        build_ray_node_observation(
            node_id=node_id,
            cluster_session=expectation.cluster_session,
            runtime=expectation.runtime,
        )
        for node_id in (NODE_A, NODE_B)
    )


def _attestation(
    *,
    expectation: RayTargetExpectation | None = None,
    boundary: RayObservationBoundary | None = None,
    nodes: tuple[RayNodeObservation, ...] | None = None,
    observed_at: datetime = OBSERVED_AT,
    expires_at: datetime | None = None,
) -> RayClusterAttestation:
    if expectation is None:
        expectation = _expectation()
    if boundary is None:
        boundary = _boundary()
    if nodes is None:
        nodes = _nodes(expectation)
    if expires_at is None:
        expires_at = observed_at + timedelta(seconds=60)
    return build_ray_cluster_attestation(
        expectation=expectation,
        boundary=boundary,
        nodes=nodes,
        observed_at=observed_at,
        expires_at=expires_at,
    )


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assert_rejection(
    error: pytest.ExceptionInfo[RayTargetAttestationError],
    classification: RayTargetAttestationRejection,
) -> None:
    assert error.value.classification is classification
    assert str(error.value) == f"Ray target attestation rejected: {classification.value}"


def _with_valid_full_digest(attestation: RayClusterAttestation) -> RayClusterAttestation:
    temporary = replace(attestation, attestation_digest="sha256:" + "0" * 64)
    return replace(temporary, attestation_digest=ray_cluster_attestation_digest(temporary))


def test_runner_family_is_an_exact_two_value_contract() -> None:
    assert [(item.name, item.value) for item in RayRunnerFamily] == [
        ("RAY_CORE", "ray_core"),
        ("RAY_JOB", "ray_job"),
    ]


def test_expectation_round_trips_as_exact_canonical_json() -> None:
    expectation = _expectation()

    serialized = encode_ray_target_expectation(expectation)
    value = json.loads(serialized)

    assert serialized == _canonical(value)
    assert value == {
        "schema": RAY_TARGET_EXPECTATION_SCHEMA,
        "schema_version": RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        "target_key": "primary.ray",
        "runner_family": "ray_core",
        "cluster_session": SESSION,
        "policy_revision": 7,
        "runtime": {
            "ray_major": 2,
            "ray_minor": 56,
            "ray_patch": 0,
            "python_implementation": "cpython",
            "python_major": 3,
            "python_minor": 12,
            "python_patch": 12,
        },
    }
    assert decode_ray_target_expectation(serialized) == expectation


def test_cluster_attestation_round_trips_with_advancing_snapshot_versions() -> None:
    attestation = _attestation()

    serialized = encode_ray_cluster_attestation(attestation)
    value = json.loads(serialized)

    assert serialized == _canonical(value)
    assert value["schema"] == RAY_CLUSTER_ATTESTATION_SCHEMA
    assert value["schema_version"] == RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION
    assert value["boundary"]["resource_state_version_before"] == 100
    assert value["boundary"]["resource_state_version_after"] == 104
    assert [item["node_id"] for item in value["nodes"]] == [NODE_A, NODE_B]
    assert value["observed_at"] == "2026-08-15T19:00:00.123456Z"
    assert value["expires_at"] == "2026-08-15T19:01:00.123456Z"
    assert decode_ray_cluster_attestation(serialized) == attestation
    compare_ray_target_attestation(
        _expectation(),
        attestation,
        now=OBSERVED_AT + timedelta(seconds=30),
    )


def test_digest_domains_are_deterministic_and_distinct() -> None:
    expectation = _expectation()
    boundary = _boundary()
    node = _nodes(expectation)[0]
    attestation = _attestation(expectation=expectation, boundary=boundary)

    digests = {
        ray_target_expectation_digest(expectation),
        ray_node_observation_digest(node),
        ray_membership_digest(boundary),
        ray_cluster_attestation_digest(attestation),
    }

    assert len(digests) == 4
    assert all(value.startswith("sha256:") and len(value) == 71 for value in digests)
    assert ray_target_expectation_digest(expectation) == attestation.expectation_digest
    assert ray_membership_digest(boundary) == attestation.membership_digest
    assert ray_cluster_attestation_digest(attestation) == attestation.attestation_digest


def test_membership_digest_ignores_heartbeat_versions_but_not_node_identity() -> None:
    first = _boundary()
    later = _boundary(
        before_resource=500,
        after_resource=900,
        before=_versions((NODE_A, 400), (NODE_B, 800)),
        after=_versions((NODE_A, 700), (NODE_B, 999)),
    )
    different = _boundary(
        before=_versions((NODE_A, 10), (NODE_C, 20)),
        after=_versions((NODE_A, 12), (NODE_C, 21)),
    )

    assert ray_membership_digest(first) == ray_membership_digest(later)
    assert ray_membership_digest(first) != ray_membership_digest(different)


@pytest.mark.parametrize(
    ("changes", "secret"),
    [
        ({"target_key": "Primary"}, "Primary"),
        ({"target_key": "primary ray"}, "primary ray"),
        ({"target_key": "primary\nray"}, "primary"),
        ({"target_key": "é"}, "é"),
        ({"target_key": "a" * 129}, "a" * 129),
        ({"cluster_session": "not-a-session"}, "not-a-session"),
        ({"cluster_session": "session_bad:colon"}, "bad:colon"),
        ({"cluster_session": "session_bad\u202e"}, "bad"),
        ({"cluster_session": "session_" + "a" * 249}, "a" * 249),
        ({"runner_family": "ray_core"}, "ray_core"),
        ({"policy_revision": True}, "True"),
        ({"policy_revision": -1}, "-1"),
        ({"policy_revision": RAY_TARGET_ATTESTATION_MAX_COUNTER + 1}, "922"),
    ],
)
def test_expectation_encoder_rejects_noncanonical_or_unbounded_identity_without_echo(
    changes: dict[str, object], secret: str
) -> None:
    with pytest.raises(RayTargetAttestationEncodeError) as error:
        encode_ray_target_expectation(_expectation(**changes))

    assert str(error.value) == "Ray target attestation encoding failed"
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"ray_major": 0},
        {"ray_major": True},
        {"ray_minor": -1},
        {"ray_patch": RAY_TARGET_ATTESTATION_MAX_COUNTER + 1},
        {"python_implementation": "CPython"},
        {"python_implementation": "cpython dev"},
        {"python_implementation": "cpython\u200b"},
        {"python_major": 0},
        {"python_minor": False},
        {"python_patch": -1},
    ],
)
def test_runtime_tuple_rejects_ambiguous_non_numeric_or_overflow_components(
    changes: dict[str, object],
) -> None:
    with pytest.raises(RayTargetAttestationEncodeError):
        encode_ray_target_expectation(_expectation(runtime=_runtime(**changes)))


@pytest.mark.parametrize("serialized", [None, b"{}", 7, [], {}])
def test_expectation_decoder_rejects_non_text_input(serialized: object) -> None:
    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_target_expectation(serialized)

    _assert_rejection(error, RayTargetAttestationRejection.INVALID)


def test_expectation_decoder_rejects_duplicate_keys() -> None:
    serialized = encode_ray_target_expectation(_expectation())
    duplicate = serialized.replace(
        '"target_key":"primary.ray"',
        '"target_key":"foreign-secret","target_key":"primary.ray"',
    )

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_target_expectation(duplicate)

    _assert_rejection(error, RayTargetAttestationRejection.INVALID)
    assert "foreign-secret" not in str(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema="foreign"),
        lambda value: value.update(schema_version=2),
    ],
)
def test_expectation_decoder_rejects_unsupported_schema(
    mutation: Callable[[dict[str, Any]], object],
) -> None:
    value = json.loads(encode_ray_target_expectation(_expectation()))
    mutation(value)

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_target_expectation(_canonical(value))

    _assert_rejection(error, RayTargetAttestationRejection.UNSUPPORTED_SCHEMA)


def test_expectation_decoder_rejects_noncanonical_json() -> None:
    value = json.loads(encode_ray_target_expectation(_expectation()))
    for serialized in (json.dumps(value, indent=2), json.dumps(value, sort_keys=False)):
        with pytest.raises(RayTargetAttestationError) as error:
            decode_ray_target_expectation(serialized)
        _assert_rejection(error, RayTargetAttestationRejection.NONCANONICAL)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("target_key"),
        lambda value: value.update(extra=True),
        lambda value: value["runtime"].update(extra=1),
        lambda value: value["runtime"].pop("ray_patch"),
        lambda value: value.update(policy_revision=True),
        lambda value: value["runtime"].update(ray_minor=2.0),
        lambda value: value["runtime"].update(python_implementation="CPython"),
        lambda value: value.update(target_key="bad\nkey"),
        lambda value: value.update(cluster_session="session_bad\ud800"),
    ],
)
def test_expectation_decoder_rejects_non_exact_shapes_and_values(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    value = json.loads(encode_ray_target_expectation(_expectation()))
    mutate(value)

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_target_expectation(_canonical(value))

    _assert_rejection(error, RayTargetAttestationRejection.INVALID)


def test_expectation_decoder_classifies_integer_and_frame_overflow() -> None:
    serialized = encode_ray_target_expectation(_expectation())
    huge_integer = serialized.replace(
        '"policy_revision":7', '"policy_revision":99999999999999999999'
    )

    for payload in (huge_integer, " " * (RAY_TARGET_EXPECTATION_MAX_BYTES + 1)):
        with pytest.raises(RayTargetAttestationError) as error:
            decode_ray_target_expectation(payload)
        _assert_rejection(error, RayTargetAttestationRejection.RESOURCE_LIMIT)


@pytest.mark.parametrize(
    "serialized",
    [
        "[]",
        "{",
        '{"policy_revision":NaN}',
    ],
)
def test_expectation_decoder_rejects_json_values_and_constants(serialized: str) -> None:
    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_target_expectation(serialized)

    _assert_rejection(error, RayTargetAttestationRejection.INVALID)


def test_expectation_decoder_rejects_bool_schema_and_unknown_runner() -> None:
    for field, value in (
        ("schema_version", True),
        ("runner_family", "ray_client"),
    ):
        payload = json.loads(encode_ray_target_expectation(_expectation()))
        payload[field] = value
        with pytest.raises(RayTargetAttestationError) as error:
            decode_ray_target_expectation(_canonical(payload))
        _assert_rejection(error, RayTargetAttestationRejection.INVALID)


def test_expectation_decoder_rejects_exact_nineteen_digit_counter_overflow() -> None:
    payload = encode_ray_target_expectation(_expectation()).replace(
        '"policy_revision":7',
        f'"policy_revision":{RAY_TARGET_ATTESTATION_MAX_COUNTER + 1}',
    )

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_target_expectation(payload)

    _assert_rejection(error, RayTargetAttestationRejection.RESOURCE_LIMIT)


def test_expectation_encoder_rejects_wrong_public_object_types() -> None:
    for value in (object(), _expectation(runtime=object())):
        with pytest.raises(RayTargetAttestationEncodeError):
            encode_ray_target_expectation(value)  # type: ignore[arg-type]


def test_boundary_accepts_equal_or_advancing_versions() -> None:
    equal = _boundary(
        before_resource=5,
        after_resource=5,
        before=_versions((NODE_A, 3), (NODE_B, 4)),
        after=_versions((NODE_A, 3), (NODE_B, 4)),
    )
    advancing = _boundary()

    assert equal.resource_state_version_before == equal.resource_state_version_after
    assert advancing.resource_state_version_after > advancing.resource_state_version_before
    assert advancing.node_state_versions_after[0].node_state_version == 12


@pytest.mark.parametrize(
    "kwargs",
    [
        {"before_resource": 2, "after_resource": 1},
        {
            "before": _versions((NODE_A, 3), (NODE_B, 4)),
            "after": _versions((NODE_A, 2), (NODE_B, 4)),
        },
        {
            "before": _versions((NODE_A, 3), (NODE_B, 4)),
            "after": _versions((NODE_A, 3), (NODE_C, 4)),
        },
        {
            "before": _versions((NODE_B, 4), (NODE_A, 3)),
            "after": _versions((NODE_B, 4), (NODE_A, 3)),
        },
        {
            "before": _versions((NODE_A, 3), (NODE_A, 4)),
            "after": _versions((NODE_A, 3), (NODE_A, 4)),
        },
        {"before": (), "after": ()},
        {"before_resource": True},
        {"after_resource": RAY_TARGET_ATTESTATION_MAX_COUNTER + 1},
        {
            "before": _versions((NODE_A, 3), (NODE_B.upper(), 4)),
            "after": _versions((NODE_A, 3), (NODE_B.upper(), 4)),
        },
    ],
)
def test_boundary_rejects_regression_changed_or_noncanonical_membership(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(RayTargetAttestationEncodeError):
        _boundary(**kwargs)  # type: ignore[arg-type]


def test_boundary_rejects_non_tuple_and_node_limit() -> None:
    with pytest.raises(RayTargetAttestationEncodeError):
        build_ray_observation_boundary(
            resource_state_version_before=1,
            resource_state_version_after=1,
            node_state_versions_before=[RayNodeStateVersion(NODE_A, 1)],  # type: ignore[arg-type]
            node_state_versions_after=[RayNodeStateVersion(NODE_A, 1)],  # type: ignore[arg-type]
        )

    too_many = tuple(
        RayNodeStateVersion(f"{index:056x}", 1)
        for index in range(RAY_TARGET_ATTESTATION_MAX_NODES + 1)
    )
    with pytest.raises(RayTargetAttestationEncodeError):
        build_ray_observation_boundary(
            resource_state_version_before=1,
            resource_state_version_after=1,
            node_state_versions_before=too_many,
            node_state_versions_after=too_many,
        )


def test_maximum_public_contract_is_encodable_and_round_trips_under_frame_cap() -> None:
    maximum = RAY_TARGET_ATTESTATION_MAX_COUNTER
    session = "session_" + "A" * 248
    runtime = RayRuntimeVersion(
        ray_major=maximum,
        ray_minor=maximum,
        ray_patch=maximum,
        python_implementation="p" * 64,
        python_major=maximum,
        python_minor=maximum,
        python_patch=maximum,
    )
    expectation = RayTargetExpectation(
        target_key="a" * 128,
        runner_family=RayRunnerFamily.RAY_JOB,
        cluster_session=session,
        policy_revision=maximum,
        runtime=runtime,
    )
    node_ids = tuple(f"{index:056x}" for index in range(RAY_TARGET_ATTESTATION_MAX_NODES))
    versions = tuple(RayNodeStateVersion(node_id, maximum) for node_id in node_ids)
    boundary = build_ray_observation_boundary(
        resource_state_version_before=maximum,
        resource_state_version_after=maximum,
        node_state_versions_before=versions,
        node_state_versions_after=versions,
    )
    nodes = tuple(
        build_ray_node_observation(
            node_id=node_id,
            cluster_session=session,
            runtime=runtime,
        )
        for node_id in node_ids
    )
    attestation = build_ray_cluster_attestation(
        expectation=expectation,
        boundary=boundary,
        nodes=nodes,
        observed_at=OBSERVED_AT,
        expires_at=OBSERVED_AT + timedelta(seconds=RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS),
    )

    serialized = encode_ray_cluster_attestation(attestation)

    assert len(serialized.encode()) <= RAY_CLUSTER_ATTESTATION_MAX_BYTES
    assert decode_ray_cluster_attestation(serialized) == attestation


def test_node_observation_digest_is_exact_and_builder_is_redaction_safe() -> None:
    node = build_ray_node_observation(
        node_id=NODE_A,
        cluster_session=SESSION,
        runtime=_runtime(),
    )

    assert node.observation_digest == ray_node_observation_digest(node)
    assert len(node.observation_digest) == 71

    with pytest.raises(RayTargetAttestationEncodeError) as error:
        build_ray_node_observation(
            node_id="FOREIGN-NODE-SECRET",
            cluster_session=SESSION,
            runtime=_runtime(),
        )
    assert "FOREIGN-NODE-SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"node_id": NODE_A[:-1]},
        {"node_id": NODE_B.upper()},
        {"cluster_session": "foreign"},
        {"cluster_session": "session_bad\nname"},
        {"runtime": _runtime(python_implementation="CPython")},
    ],
)
def test_node_observation_rejects_noncanonical_fields(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "node_id": NODE_A,
        "cluster_session": SESSION,
        "runtime": _runtime(),
    }
    values.update(changes)
    with pytest.raises(RayTargetAttestationEncodeError):
        build_ray_node_observation(**values)  # type: ignore[arg-type]


def test_attestation_builder_requires_exact_sorted_membership_and_node_runtime() -> None:
    expectation = _expectation()
    nodes = _nodes(expectation)

    invalid_nodes = [
        tuple(reversed(nodes)),
        (nodes[0], nodes[0]),
        (nodes[0],),
        (
            build_ray_node_observation(
                node_id=NODE_A,
                cluster_session="session_foreign",
                runtime=expectation.runtime,
            ),
            nodes[1],
        ),
        (
            build_ray_node_observation(
                node_id=NODE_A,
                cluster_session=SESSION,
                runtime=_runtime(ray_patch=1),
            ),
            nodes[1],
        ),
        (replace(nodes[0], observation_digest="sha256:" + "f" * 64), nodes[1]),
    ]

    for candidate in invalid_nodes:
        with pytest.raises(RayTargetAttestationEncodeError):
            _attestation(expectation=expectation, nodes=candidate)


@pytest.mark.parametrize(
    ("observed_at", "expires_at"),
    [
        (OBSERVED_AT.replace(tzinfo=None), OBSERVED_AT + timedelta(seconds=1)),
        (OBSERVED_AT, OBSERVED_AT),
        (OBSERVED_AT, OBSERVED_AT - timedelta(microseconds=1)),
        (
            OBSERVED_AT,
            OBSERVED_AT + timedelta(seconds=RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS + 1),
        ),
        (
            OBSERVED_AT.astimezone(timezone(timedelta(hours=1))),
            OBSERVED_AT + timedelta(seconds=1),
        ),
    ],
)
def test_attestation_builder_requires_bounded_utc_validity_window(
    observed_at: datetime, expires_at: datetime
) -> None:
    with pytest.raises(RayTargetAttestationEncodeError):
        _attestation(observed_at=observed_at, expires_at=expires_at)


def test_cluster_decoder_rejects_non_text_duplicate_and_oversized_frames() -> None:
    for serialized in (None, b"{}", [], 1):
        with pytest.raises(RayTargetAttestationError) as error:
            decode_ray_cluster_attestation(serialized)
        _assert_rejection(error, RayTargetAttestationRejection.INVALID)

    serialized = encode_ray_cluster_attestation(_attestation())
    duplicate = serialized.replace(
        '"membership_digest":',
        '"foreign_secret":"do-not-echo","membership_digest":',
    ).replace(
        '"foreign_secret":"do-not-echo",',
        '"foreign_secret":"do-not-echo","foreign_secret":"again",',
    )
    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation(duplicate)
    _assert_rejection(error, RayTargetAttestationRejection.INVALID)
    assert "do-not-echo" not in str(error.value)

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation(" " * (RAY_CLUSTER_ATTESTATION_MAX_BYTES + 1))
    _assert_rejection(error, RayTargetAttestationRejection.RESOURCE_LIMIT)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema="foreign"),
        lambda value: value.update(schema_version=2),
    ],
)
def test_cluster_decoder_rejects_unsupported_schema(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    value = json.loads(encode_ray_cluster_attestation(_attestation()))
    mutate(value)

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation(_canonical(value))

    _assert_rejection(error, RayTargetAttestationRejection.UNSUPPORTED_SCHEMA)


def test_cluster_decoder_rejects_noncanonical_framing_and_unsorted_nodes() -> None:
    serialized = encode_ray_cluster_attestation(_attestation())
    value = json.loads(serialized)

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation(json.dumps(value, indent=2))
    _assert_rejection(error, RayTargetAttestationRejection.NONCANONICAL)

    value["nodes"].reverse()
    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation(_canonical(value))
    _assert_rejection(error, RayTargetAttestationRejection.INVALID)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("boundary"),
        lambda value: value.update(extra=True),
        lambda value: value["boundary"].update(extra=True),
        lambda value: value.update(nodes={}),
        lambda value: value["nodes"][0].update(extra=True),
        lambda value: value["boundary"].update(resource_state_version_before=True),
        lambda value: value.update(observed_at="2026-08-15T19:00:00Z"),
        lambda value: value.update(observed_at="2026-08-15T19:00:00.123456+00:00"),
        lambda value: value.update(expires_at="not-a-time"),
        lambda value: value["nodes"][1].update(node_id=NODE_B.upper()),
    ],
)
def test_cluster_decoder_rejects_non_exact_nested_shapes(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    value = json.loads(encode_ray_cluster_attestation(_attestation()))
    mutate(value)

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation(_canonical(value))

    _assert_rejection(error, RayTargetAttestationRejection.INVALID)


def test_cluster_decoder_rejects_non_object_and_deep_malformed_values() -> None:
    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation("[]")
    _assert_rejection(error, RayTargetAttestationRejection.INVALID)

    mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
        lambda value: value["boundary"].update(node_state_versions_before={}),
        lambda value: value["boundary"]["node_state_versions_before"].__setitem__(0, {}),
        lambda value: value.update(observed_at="garbageZ"),
    )
    for mutate in mutations:
        payload = json.loads(encode_ray_cluster_attestation(_attestation()))
        mutate(payload)
        with pytest.raises(RayTargetAttestationError) as error:
            decode_ray_cluster_attestation(_canonical(payload))
        _assert_rejection(error, RayTargetAttestationRejection.INVALID)


@pytest.mark.parametrize(
    ("field", "classification"),
    [
        ("expectation_digest", RayTargetAttestationRejection.EXPECTATION_DIGEST_MISMATCH),
        ("membership_digest", RayTargetAttestationRejection.MEMBERSHIP_MISMATCH),
        ("attestation_digest", RayTargetAttestationRejection.ATTESTATION_DIGEST_MISMATCH),
    ],
)
def test_cluster_decoder_classifies_top_level_digest_tampering(
    field: str, classification: RayTargetAttestationRejection
) -> None:
    value = json.loads(encode_ray_cluster_attestation(_attestation()))
    value[field] = "sha256:" + "f" * 64

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation(_canonical(value))

    _assert_rejection(error, classification)


def test_cluster_decoder_classifies_node_digest_tampering_before_full_digest() -> None:
    value = json.loads(encode_ray_cluster_attestation(_attestation()))
    value["nodes"][0]["observation_digest"] = "sha256:" + "f" * 64

    with pytest.raises(RayTargetAttestationError) as error:
        decode_ray_cluster_attestation(_canonical(value))

    _assert_rejection(error, RayTargetAttestationRejection.OBSERVATION_DIGEST_MISMATCH)


@pytest.mark.parametrize(
    ("expected", "classification"),
    [
        (_expectation(target_key="foreign"), RayTargetAttestationRejection.TARGET_KEY_MISMATCH),
        (
            _expectation(runner_family=RayRunnerFamily.RAY_JOB),
            RayTargetAttestationRejection.RUNNER_FAMILY_MISMATCH,
        ),
        (
            _expectation(cluster_session="session_foreign"),
            RayTargetAttestationRejection.CLUSTER_SESSION_MISMATCH,
        ),
        (
            _expectation(policy_revision=8),
            RayTargetAttestationRejection.POLICY_REVISION_MISMATCH,
        ),
        (
            _expectation(runtime=_runtime(ray_patch=1)),
            RayTargetAttestationRejection.RAY_VERSION_MISMATCH,
        ),
        (
            _expectation(runtime=_runtime(python_implementation="pypy")),
            RayTargetAttestationRejection.PYTHON_IMPLEMENTATION_MISMATCH,
        ),
        (
            _expectation(runtime=_runtime(python_patch=13)),
            RayTargetAttestationRejection.PYTHON_VERSION_MISMATCH,
        ),
    ],
)
def test_comparison_rejects_every_expected_target_tuple_mismatch(
    expected: RayTargetExpectation,
    classification: RayTargetAttestationRejection,
) -> None:
    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(expected, _attestation(), now=OBSERVED_AT)

    _assert_rejection(error, classification)


def test_comparison_rejects_not_yet_valid_and_expired_attestations() -> None:
    attestation = _attestation()

    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(
            _expectation(), attestation, now=OBSERVED_AT - timedelta(microseconds=1)
        )
    _assert_rejection(error, RayTargetAttestationRejection.NOT_YET_VALID)

    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(_expectation(), attestation, now=attestation.expires_at)
    _assert_rejection(error, RayTargetAttestationRejection.EXPIRED)


def test_comparison_verifies_full_digest_before_invalid_window_semantics() -> None:
    attestation = _attestation()
    stale_digest = replace(attestation, expires_at=attestation.observed_at)

    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(_expectation(), stale_digest, now=OBSERVED_AT)
    _assert_rejection(error, RayTargetAttestationRejection.ATTESTATION_DIGEST_MISMATCH)

    invalid_windows = (
        (
            replace(attestation, expires_at=attestation.observed_at),
            RayTargetAttestationRejection.INVALID,
        ),
        (
            replace(
                attestation,
                expires_at=attestation.observed_at
                + timedelta(seconds=RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS + 1),
            ),
            RayTargetAttestationRejection.RESOURCE_LIMIT,
        ),
    )
    for invalid, classification in invalid_windows:
        with pytest.raises(RayTargetAttestationError) as error:
            compare_ray_target_attestation(
                _expectation(),
                _with_valid_full_digest(invalid),
                now=OBSERVED_AT,
            )
        _assert_rejection(error, classification)


def test_comparison_rejects_direct_object_digest_tampering_before_target_fields() -> None:
    attestation = _attestation()
    tampered = replace(
        attestation,
        expectation=replace(attestation.expectation, target_key="foreign"),
        expectation_digest="sha256:" + "f" * 64,
    )

    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(_expectation(), tampered, now=OBSERVED_AT)

    _assert_rejection(error, RayTargetAttestationRejection.EXPECTATION_DIGEST_MISMATCH)


@pytest.mark.parametrize(
    ("node", "classification"),
    [
        (
            build_ray_node_observation(
                node_id=NODE_A,
                cluster_session="session_foreign",
                runtime=_runtime(),
            ),
            RayTargetAttestationRejection.CLUSTER_SESSION_MISMATCH,
        ),
        (
            build_ray_node_observation(
                node_id=NODE_A,
                cluster_session=SESSION,
                runtime=_runtime(ray_patch=1),
            ),
            RayTargetAttestationRejection.RAY_VERSION_MISMATCH,
        ),
        (
            build_ray_node_observation(
                node_id=NODE_A,
                cluster_session=SESSION,
                runtime=_runtime(python_implementation="pypy"),
            ),
            RayTargetAttestationRejection.PYTHON_IMPLEMENTATION_MISMATCH,
        ),
        (
            build_ray_node_observation(
                node_id=NODE_A,
                cluster_session=SESSION,
                runtime=_runtime(python_patch=13),
            ),
            RayTargetAttestationRejection.PYTHON_VERSION_MISMATCH,
        ),
    ],
)
def test_comparison_rejects_internally_digest_valid_node_mismatches(
    node: RayNodeObservation,
    classification: RayTargetAttestationRejection,
) -> None:
    original = _attestation()
    tampered = _with_valid_full_digest(replace(original, nodes=(node, original.nodes[1])))

    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(_expectation(), tampered, now=OBSERVED_AT)

    _assert_rejection(error, classification)


def test_comparison_rejects_digest_valid_node_set_mismatch() -> None:
    original = _attestation()
    tampered = _with_valid_full_digest(replace(original, nodes=(original.nodes[0],)))

    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(_expectation(), tampered, now=OBSERVED_AT)

    _assert_rejection(error, RayTargetAttestationRejection.MEMBERSHIP_MISMATCH)


def test_comparison_rejects_invalid_direct_objects_and_clock() -> None:
    attestation = _attestation()
    invalid_clock = OBSERVED_AT.replace(tzinfo=None)

    for expectation, candidate, now in (
        (_expectation(policy_revision=True), attestation, OBSERVED_AT),
        (_expectation(), replace(attestation, nodes=[]), OBSERVED_AT),  # type: ignore[arg-type]
        (_expectation(), attestation, invalid_clock),
    ):
        with pytest.raises(RayTargetAttestationError) as error:
            compare_ray_target_attestation(expectation, candidate, now=now)
        _assert_rejection(error, RayTargetAttestationRejection.INVALID)


def test_comparison_classifies_direct_object_type_and_resource_failures() -> None:
    attestation = _attestation()

    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(
            _expectation(),
            object(),
            now=OBSERVED_AT,  # type: ignore[arg-type]
        )
    _assert_rejection(error, RayTargetAttestationRejection.INVALID)

    with pytest.raises(RayTargetAttestationError) as error:
        compare_ray_target_attestation(
            _expectation(policy_revision=RAY_TARGET_ATTESTATION_MAX_COUNTER + 1),
            attestation,
            now=OBSERVED_AT,
        )
    _assert_rejection(error, RayTargetAttestationRejection.RESOURCE_LIMIT)

    for nodes, classification in (
        ((), RayTargetAttestationRejection.INVALID),
        (
            (attestation.nodes[0],) * (RAY_TARGET_ATTESTATION_MAX_NODES + 1),
            RayTargetAttestationRejection.RESOURCE_LIMIT,
        ),
        ((object(),), RayTargetAttestationRejection.INVALID),
    ):
        with pytest.raises(RayTargetAttestationError) as error:
            compare_ray_target_attestation(
                _expectation(),
                replace(attestation, nodes=nodes),  # type: ignore[arg-type]
                now=OBSERVED_AT,
            )
        _assert_rejection(error, classification)


def test_attestation_encoder_rejects_invalid_direct_object_with_fixed_error() -> None:
    attestation = _attestation()
    invalid = replace(attestation, attestation_digest="foreign-secret")

    with pytest.raises(RayTargetAttestationEncodeError) as error:
        encode_ray_cluster_attestation(invalid)

    assert str(error.value) == "Ray target attestation encoding failed"
    assert "foreign-secret" not in str(error.value)

    digest_tampered = replace(attestation, attestation_digest="sha256:" + "f" * 64)
    with pytest.raises(RayTargetAttestationEncodeError):
        encode_ray_cluster_attestation(digest_tampered)

    with pytest.raises(RayTargetAttestationEncodeError):
        build_ray_cluster_attestation(
            expectation=_expectation(),
            boundary=object(),  # type: ignore[arg-type]
            nodes=_nodes(),
            observed_at=OBSERVED_AT,
            expires_at=OBSERVED_AT + timedelta(seconds=1),
        )
