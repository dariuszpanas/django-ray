from __future__ import annotations

import builtins
import platform
import sys
import time
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from django_ray import ray_target_probe as probe
from django_ray.target_attestation import (
    RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetAttestationEncodeError,
    RayTargetExpectation,
    compare_ray_target_attestation,
)

NODE_A = "1" * 56
NODE_B = "2" * 56
NODE_C = "3" * 56
SESSION = "session_probe_test"
RUNTIME = RayRuntimeVersion(
    ray_major=2,
    ray_minor=56,
    ray_patch=0,
    python_implementation="cpython",
    python_major=3,
    python_minor=12,
    python_patch=12,
)


def _runtime_mapping(
    node_id: str = NODE_A,
    *,
    session_name: str = SESSION,
    ray_version: str = "2.56.0",
    python_implementation: str = "cpython",
    python_version: str = "3.12.12",
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "session_name": session_name,
        "ray_version": ray_version,
        "python_implementation": python_implementation,
        "python_version": python_version,
    }


def _snapshot_mapping(
    node_ids: tuple[str, ...],
    *,
    revision: int,
    node_revision: int,
    session_name: str = SESSION,
) -> dict[str, object]:
    return {
        "session_name": session_name,
        "cluster_resource_state_version": revision,
        "node_state_versions": [[node_id, node_revision] for node_id in node_ids],
    }


def _interval_envelope(
    *,
    before_ids: tuple[str, ...] = (NODE_A,),
    after_ids: tuple[str, ...] | None = None,
    observed_ids: tuple[str, ...] | None = None,
    before_revision: int = 10,
    after_revision: int = 11,
    before_node_revision: int = 20,
    after_node_revision: int = 21,
    session_name: str = SESSION,
) -> dict[str, object]:
    selected_after = before_ids if after_ids is None else after_ids
    selected_observed = before_ids if observed_ids is None else observed_ids
    return {
        "ok": True,
        "coordinator": _runtime_mapping(session_name=session_name),
        "before": _snapshot_mapping(
            before_ids,
            revision=before_revision,
            node_revision=before_node_revision,
            session_name=session_name,
        ),
        "after": _snapshot_mapping(
            selected_after,
            revision=after_revision,
            node_revision=after_node_revision,
            session_name=session_name,
        ),
        "nodes": [
            _runtime_mapping(node_id=node_id, session_name=session_name)
            for node_id in selected_observed
        ],
    }


def _expectation(
    *,
    runner_family: RayRunnerFamily = RayRunnerFamily.RAY_CORE,
    cluster_session: str = SESSION,
    runtime: RayRuntimeVersion = RUNTIME,
) -> RayTargetExpectation:
    return RayTargetExpectation(
        target_key="primary",
        runner_family=runner_family,
        cluster_session=cluster_session,
        policy_revision=7,
        runtime=runtime,
    )


def _raw_observation() -> probe._RawClusterObservation:
    caller = probe._RuntimeObservation(
        node_id=NODE_A,
        session_name=SESSION,
        ray_version="2.56.0",
        python_implementation="cpython",
        python_version=(3, 12, 12),
    )
    interval = probe._ClusterIntervalObservation(
        coordinator=caller,
        before=probe._ResourceStateSnapshot(
            session_name=SESSION,
            cluster_resource_state_version=10,
            node_state_versions=((NODE_A, 20), (NODE_B, 30)),
        ),
        after=probe._ResourceStateSnapshot(
            session_name=SESSION,
            cluster_resource_state_version=12,
            node_state_versions=((NODE_A, 21), (NODE_B, 32)),
        ),
        nodes=(
            caller,
            probe._RuntimeObservation(
                node_id=NODE_B,
                session_name=SESSION,
                ray_version="2.56.0",
                python_implementation="cpython",
                python_version=(3, 12, 12),
            ),
        ),
    )
    return probe._RawClusterObservation(caller=caller, interval=interval)


def _assert_probe_failure(
    error: pytest.ExceptionInfo[probe.RayTargetProbeError],
    classification: probe.RayTargetProbeFailure,
) -> None:
    assert error.value.classification is classification
    assert str(error.value) == f"Ray target probe failed: {classification.value}"


def test_decode_interval_accepts_stable_set_with_advancing_versions() -> None:
    result = probe._decode_remote_interval(
        _interval_envelope(before_ids=(NODE_A, NODE_B)),
        max_nodes=2,
    )

    assert result.before.node_ids == (NODE_A, NODE_B)
    assert result.after.cluster_resource_state_version == 11
    assert result.after.node_state_versions == ((NODE_A, 21), (NODE_B, 21))
    assert tuple(node.node_id for node in result.nodes) == (NODE_A, NODE_B)


@pytest.mark.parametrize(
    ("after_ids", "classification"),
    [
        ((NODE_A,), probe.RayTargetProbeFailure.MEMBERSHIP_CHANGED),
        ((NODE_A, NODE_B, NODE_C), probe.RayTargetProbeFailure.MEMBERSHIP_CHANGED),
    ],
)
def test_decode_interval_rejects_node_leave_and_join(
    after_ids: tuple[str, ...],
    classification: probe.RayTargetProbeFailure,
) -> None:
    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_remote_interval(
            _interval_envelope(before_ids=(NODE_A, NODE_B), after_ids=after_ids),
            max_nodes=3,
        )

    _assert_probe_failure(error, classification)


def test_decode_interval_rejects_wrong_hard_affinity_node() -> None:
    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_remote_interval(
            _interval_envelope(observed_ids=(NODE_B,)),
            max_nodes=2,
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_ID_MISMATCH)


def test_decode_interval_requires_coordinator_on_a_schedulable_node() -> None:
    envelope = _interval_envelope()
    envelope["coordinator"] = _runtime_mapping(node_id=NODE_C)

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_remote_interval(envelope, max_nodes=1)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_ID_MISMATCH)


@pytest.mark.parametrize(
    "changes",
    [
        {"before_revision": 11, "after_revision": 10},
        {"before_node_revision": 21, "after_node_revision": 20},
    ],
)
def test_decode_interval_rejects_revision_regression(changes: dict[str, int]) -> None:
    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_remote_interval(_interval_envelope(**changes), max_nodes=1)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.REVISION_REGRESSION)


def test_decode_interval_rejects_snapshot_truncation_before_iteration() -> None:
    envelope = _interval_envelope(before_ids=(NODE_A, NODE_B))
    envelope["nodes"] = [_runtime_mapping()]

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_remote_interval(envelope, max_nodes=1)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.RESOURCE_LIMIT)


def test_remote_envelopes_require_exact_plain_container_types() -> None:
    class DictSubclass(dict[str, object]):
        pass

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_remote_interval(DictSubclass(_interval_envelope()), max_nodes=1)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)


@pytest.mark.parametrize(
    "python_version",
    ["03.12.12", "3.012.12", "3.12.012", f"3.{('9' * 10000)}.1"],
)
def test_runtime_observation_rejects_noncanonical_or_unbounded_python_version(
    python_version: str,
) -> None:
    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_runtime_observation(_runtime_mapping(python_version=python_version))

    _assert_probe_failure(error, probe.RayTargetProbeFailure.INVALID_NODE_OBSERVATION)


@pytest.mark.parametrize(
    ("field", "value", "classification"),
    [
        ("python_implementation", None, probe.RayTargetProbeFailure.INVALID_NODE_OBSERVATION),
        (
            "python_implementation",
            "bad value",
            probe.RayTargetProbeFailure.INVALID_NODE_OBSERVATION,
        ),
        ("python_version", None, probe.RayTargetProbeFailure.INVALID_NODE_OBSERVATION),
        ("node_id", "A" * 56, probe.RayTargetProbeFailure.INVALID_NODE_OBSERVATION),
        ("session_name", "not-a-session", probe.RayTargetProbeFailure.INVALID_NODE_OBSERVATION),
        ("ray_version", "2.56.1", probe.RayTargetProbeFailure.UNSUPPORTED_RAY_VERSION),
    ],
)
def test_runtime_observation_rejects_every_malformed_identity_field(
    field: str,
    value: object,
    classification: probe.RayTargetProbeFailure,
) -> None:
    raw = _runtime_mapping()
    raw[field] = value

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_runtime_observation(raw)

    _assert_probe_failure(error, classification)


def test_runtime_observation_rejects_non_dict_and_extra_fields() -> None:
    for raw in ([], {**_runtime_mapping(), "extra": True}):
        with pytest.raises(probe.RayTargetProbeError) as error:
            probe._decode_runtime_observation(raw)
        _assert_probe_failure(error, probe.RayTargetProbeFailure.INVALID_NODE_OBSERVATION)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("session_name", "invalid"),
        ("cluster_resource_state_version", True),
        ("cluster_resource_state_version", 1 << 63),
        ("node_state_versions", []),
        ("node_state_versions", "not-a-list"),
        ("node_state_versions", [(NODE_A, 1)]),
        ("node_state_versions", [["bad-node", 1]]),
        ("node_state_versions", [[NODE_A, True]]),
        ("node_state_versions", [[NODE_B, 1], [NODE_A, 1]]),
        ("node_state_versions", [[NODE_A, 1], [NODE_A, 2]]),
    ],
)
def test_snapshot_decoder_rejects_malformed_boundaries(
    mutation: str,
    value: object,
) -> None:
    raw = _snapshot_mapping((NODE_A,), revision=1, node_revision=1)
    raw[mutation] = value

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_snapshot(raw, max_nodes=2)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.INVALID_SNAPSHOT)


def test_snapshot_decoder_rejects_non_dict_and_wrong_keys() -> None:
    for raw in (None, {**_snapshot_mapping((NODE_A,), revision=1, node_revision=1), "x": 1}):
        with pytest.raises(probe.RayTargetProbeError) as error:
            probe._decode_snapshot(raw, max_nodes=2)
        _assert_probe_failure(error, probe.RayTargetProbeFailure.INVALID_SNAPSHOT)


@pytest.mark.parametrize(
    ("envelope", "classification"),
    [
        (
            {"ok": False, "classification": "empty_cluster"},
            probe.RayTargetProbeFailure.EMPTY_CLUSTER,
        ),
        (
            {"ok": False, "classification": 1},
            probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE,
        ),
        (
            {"ok": False, "classification": "not-a-classification"},
            probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE,
        ),
        (
            {"ok": True},
            probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE,
        ),
        (
            {**_interval_envelope(), "nodes": []},
            probe.RayTargetProbeFailure.RESOURCE_LIMIT,
        ),
    ],
)
def test_remote_envelope_failure_shapes_map_to_fixed_classifications(
    envelope: dict[str, object],
    classification: probe.RayTargetProbeFailure,
) -> None:
    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_remote_interval(envelope, max_nodes=2)

    _assert_probe_failure(error, classification)


@pytest.mark.parametrize("mismatch", ["snapshot", "node-session", "node-runtime"])
def test_interval_rejects_session_and_runtime_mismatches(mismatch: str) -> None:
    envelope = _interval_envelope()
    if mismatch == "snapshot":
        before = envelope["before"]
        assert type(before) is dict
        before["session_name"] = "session_other"
        classification = probe.RayTargetProbeFailure.SESSION_MISMATCH
    else:
        nodes = envelope["nodes"]
        assert type(nodes) is list and type(nodes[0]) is dict
        if mismatch == "node-session":
            nodes[0]["session_name"] = "session_other"
            classification = probe.RayTargetProbeFailure.SESSION_MISMATCH
        else:
            nodes[0]["python_version"] = "3.12.13"
            classification = probe.RayTargetProbeFailure.RUNTIME_MISMATCH

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._decode_remote_interval(envelope, max_nodes=1)

    _assert_probe_failure(error, classification)


def test_expired_deadline_uses_fixed_timeout() -> None:
    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._remaining_seconds(time.monotonic() - 1)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_TIMEOUT)


@pytest.mark.parametrize(
    ("timeout_seconds", "max_nodes"),
    [
        (True, 1),
        pytest.param(10**10000, 1, id="huge-integer-timeout"),
        (float("nan"), 1),
        (1.0, True),
        (1.0, probe.RAY_TARGET_PROBE_MAX_NODES + 1),
    ],
)
def test_probe_limits_fail_closed_without_numeric_conversion_leaks(
    timeout_seconds: object,
    max_nodes: object,
) -> None:
    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._validate_probe_limits(
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
            max_nodes=max_nodes,  # type: ignore[arg-type]
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.INVALID_CONFIGURATION)


def _install_remote_ray_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    states: list[object],
    get_error: Exception | None = None,
    encoded: object = b"state",
    gcs_error: Exception | None = None,
    status_values: dict[str, int] | None = None,
) -> tuple[ModuleType, list[object], list[dict[str, object]]]:
    state_iterator = iter(states)
    expected_by_ref: dict[object, str] = {}
    cancellations: list[object] = []
    affinity_options: list[dict[str, object]] = []

    ray_module = ModuleType("ray")
    ray_module.__path__ = []  # type: ignore[attr-defined]
    ray_module.__version__ = "2.56.0"
    ray_module.get_runtime_context = lambda: SimpleNamespace(
        get_node_id=lambda: NODE_A,
        get_session_name=lambda: SESSION,
    )

    class RemoteProbe:
        selected_node_id: str | None = None

        def options(self, *, scheduling_strategy: object) -> RemoteProbe:
            self.selected_node_id = scheduling_strategy.node_id
            affinity_options.append(
                {
                    "node_id": scheduling_strategy.node_id,
                    "soft": scheduling_strategy.soft,
                }
            )
            return self

        def remote(self) -> object:
            assert self.selected_node_id is not None
            ref = object()
            expected_by_ref[ref] = self.selected_node_id
            return ref

    def remote(**options: object) -> Any:
        assert options == {"num_cpus": 0, "max_retries": 0}
        return lambda _function: RemoteProbe()

    def get(refs: list[object], *, timeout: float) -> list[dict[str, object]]:
        assert timeout > 0
        if get_error is not None:
            raise get_error
        return [_runtime_mapping(node_id=expected_by_ref[ref]) for ref in refs]

    ray_module.remote = remote
    ray_module.wait = lambda refs, **_kwargs: (list(refs), [])
    ray_module.get = get
    ray_module.cancel = lambda ref, **_kwargs: cancellations.append(ref)

    private_module = ModuleType("ray._private")
    private_module.__path__ = []  # type: ignore[attr-defined]
    worker_module = ModuleType("ray._private.worker")

    def get_cluster_resource_state(**_kwargs: object) -> object:
        if gcs_error is not None:
            raise gcs_error
        return encoded

    worker_module.global_worker = SimpleNamespace(
        gcs_client=SimpleNamespace(get_cluster_resource_state=get_cluster_resource_state)
    )

    core_module = ModuleType("ray.core")
    core_module.__path__ = []  # type: ignore[attr-defined]
    generated_module = ModuleType("ray.core.generated")
    generated_module.__path__ = []  # type: ignore[attr-defined]
    autoscaler_module = ModuleType("ray.core.generated.autoscaler_pb2")

    class Reply:
        cluster_resource_state: object

        def ParseFromString(self, _value: bytes) -> None:  # noqa: N802 - protobuf API
            selected = next(state_iterator)
            if isinstance(selected, Exception):
                raise selected
            self.cluster_resource_state = selected

    class NodeStatus:
        @staticmethod
        def Value(name: str) -> int:  # noqa: N802 - protobuf enum API
            values = status_values or {
                "UNSPECIFIED": 0,
                "RUNNING": 1,
                "DEAD": 2,
                "IDLE": 3,
                "DRAINING": 4,
            }
            return values[name]

    autoscaler_module.GetClusterResourceStateReply = Reply
    autoscaler_module.NodeStatus = NodeStatus
    generated_module.autoscaler_pb2 = autoscaler_module

    util_module = ModuleType("ray.util")
    util_module.__path__ = []  # type: ignore[attr-defined]
    scheduling_module = ModuleType("ray.util.scheduling_strategies")

    class NodeAffinitySchedulingStrategy:
        def __init__(self, node_id: str, soft: bool) -> None:
            self.node_id = node_id
            self.soft = soft

    scheduling_module.NodeAffinitySchedulingStrategy = NodeAffinitySchedulingStrategy

    for name, module in {
        "ray": ray_module,
        "ray._private": private_module,
        "ray._private.worker": worker_module,
        "ray.core": core_module,
        "ray.core.generated": generated_module,
        "ray.core.generated.autoscaler_pb2": autoscaler_module,
        "ray.util": util_module,
        "ray.util.scheduling_strategies": scheduling_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return ray_module, cancellations, affinity_options


def _remote_state(
    nodes: tuple[tuple[str, int, int], ...],
    *,
    revision: int,
) -> object:
    return SimpleNamespace(
        cluster_session_name=SESSION,
        cluster_resource_state_version=revision,
        node_states=[
            SimpleNamespace(
                node_id=bytes.fromhex(node_id),
                status=status,
                node_state_version=node_revision,
            )
            for node_id, status, node_revision in nodes
        ],
    )


def test_remote_coordinator_probes_every_running_and_idle_node_with_hard_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes_before = (
        (NODE_A, 1, 10),
        (NODE_B, 3, 20),
        (NODE_C, 2, 30),
        ("4" * 56, 4, 40),
    )
    nodes_after = (
        (NODE_A, 3, 11),
        (NODE_B, 1, 21),
        (NODE_C, 2, 30),
        ("4" * 56, 4, 40),
    )
    _ray, _cancellations, affinity = _install_remote_ray_modules(
        monkeypatch,
        states=[
            _remote_state(nodes_before, revision=10),
            _remote_state(nodes_after, revision=12),
        ],
    )

    result = probe._make_cluster_probe_coordinator()(
        1.0,
        4,
        probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES,
    )

    assert result["ok"] is True
    result_nodes = result["nodes"]
    assert type(result_nodes) is list
    observed_node_ids: list[object] = []
    for item in result_nodes:
        assert type(item) is dict
        observed_node_ids.append(item["node_id"])
    assert observed_node_ids == [NODE_A, NODE_B]
    assert affinity == [
        {"node_id": NODE_A, "soft": False},
        {"node_id": NODE_B, "soft": False},
    ]


@pytest.mark.parametrize("status", [0, 5])
def test_remote_coordinator_rejects_unspecified_and_unknown_node_status(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    _install_remote_ray_modules(
        monkeypatch,
        states=[_remote_state(((NODE_A, status, 10),), revision=10)],
    )

    result = probe._make_cluster_probe_coordinator()(
        1.0,
        1,
        probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES,
    )

    assert result == {"ok": False, "classification": "invalid_snapshot"}


def test_remote_coordinator_maps_failing_node_ref_without_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poison = "credential=value-must-not-escape"
    _ray, _cancellations, _affinity = _install_remote_ray_modules(
        monkeypatch,
        states=[_remote_state(((NODE_A, 1, 10),), revision=10)],
        get_error=RuntimeError(poison),
    )

    result = probe._make_cluster_probe_coordinator()(
        1.0,
        1,
        probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES,
    )

    assert result == {"ok": False, "classification": "node_probe_unavailable"}
    assert poison not in repr(result)


def test_remote_cleanup_thread_failure_never_masks_node_wait_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ray_module, _cancellations, _affinity = _install_remote_ray_modules(
        monkeypatch,
        states=[_remote_state(((NODE_A, 1, 10),), revision=10)],
    )
    ray_module.wait = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("remote wait poison")
    )

    def reject_thread(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("thread limit poison")

    monkeypatch.setattr(probe.threading, "Thread", reject_thread)

    result = probe._make_cluster_probe_coordinator()(
        1.0,
        1,
        probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES,
    )

    assert result == {"ok": False, "classification": "node_probe_unavailable"}


def test_remote_coordinator_fails_closed_when_private_adapter_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ray_module = ModuleType("ray")
    ray_module.__version__ = "2.56.0"
    ray_module.get_runtime_context = lambda: SimpleNamespace(
        get_node_id=lambda: NODE_A,
        get_session_name=lambda: SESSION,
    )
    monkeypatch.setitem(sys.modules, "ray", ray_module)
    monkeypatch.delitem(sys.modules, "ray._private.worker", raising=False)

    result = probe._make_cluster_probe_coordinator()(
        1.0,
        1,
        probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES,
    )

    assert result == {"ok": False, "classification": "private_api_unavailable"}


def test_remote_coordinator_rejects_changed_private_status_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_remote_ray_modules(
        monkeypatch,
        states=[],
        status_values={
            "UNSPECIFIED": 0,
            "RUNNING": 9,
            "DEAD": 2,
            "IDLE": 3,
            "DRAINING": 4,
        },
    )

    result = probe._make_cluster_probe_coordinator()(
        1.0, 1, probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES
    )

    assert result == {"ok": False, "classification": "private_api_unavailable"}


@pytest.mark.parametrize(
    ("kwargs", "classification"),
    [
        (
            {"gcs_error": RuntimeError("gcs poison")},
            "snapshot_unavailable",
        ),
        ({"encoded": None}, "invalid_snapshot"),
        (
            {"encoded": b"x" * (probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES + 1)},
            "resource_limit",
        ),
        ({"states": [RuntimeError("parse poison")]}, "invalid_snapshot"),
    ],
)
def test_remote_snapshot_transport_failures_are_fixed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    classification: str,
) -> None:
    selected = {"states": [_remote_state(((NODE_A, 1, 1),), revision=1)], **kwargs}
    _install_remote_ray_modules(monkeypatch, **selected)  # type: ignore[arg-type]

    result = probe._make_cluster_probe_coordinator()(
        1.0, 1, probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES
    )

    assert result == {"ok": False, "classification": classification}
    assert "poison" not in repr(result)


@pytest.mark.parametrize(
    ("state", "max_nodes", "classification"),
    [
        (
            _remote_state(((NODE_A, 1, 1), (NODE_B, 1, 1)), revision=1),
            1,
            "resource_limit",
        ),
        (
            SimpleNamespace(
                cluster_session_name="invalid",
                cluster_resource_state_version=1,
                node_states=[],
            ),
            1,
            "invalid_snapshot",
        ),
        (
            SimpleNamespace(
                cluster_session_name=SESSION,
                cluster_resource_state_version=True,
                node_states=[],
            ),
            1,
            "invalid_snapshot",
        ),
        (
            SimpleNamespace(
                cluster_session_name=SESSION,
                cluster_resource_state_version=1,
                node_states=[object()],
            ),
            1,
            "invalid_snapshot",
        ),
        (
            SimpleNamespace(
                cluster_session_name=SESSION,
                cluster_resource_state_version=1,
                node_states=[SimpleNamespace(node_id=NODE_A, status=1, node_state_version=1)],
            ),
            1,
            "invalid_snapshot",
        ),
        (
            _remote_state(((NODE_A, 1, -1),), revision=1),
            1,
            "invalid_snapshot",
        ),
        (
            _remote_state(((NODE_A, 2, 1),), revision=1),
            1,
            "empty_cluster",
        ),
    ],
)
def test_remote_snapshot_rejects_malformed_or_empty_resource_state(
    monkeypatch: pytest.MonkeyPatch,
    state: object,
    max_nodes: int,
    classification: str,
) -> None:
    _install_remote_ray_modules(monkeypatch, states=[state])

    result = probe._make_cluster_probe_coordinator()(
        1.0, max_nodes, probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES
    )

    assert result == {"ok": False, "classification": classification}


@pytest.mark.parametrize("mode", ["setup", "unfinished", "bad-list", "bad-item", "wrong-node"])
def test_remote_node_observation_failures_are_fixed(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    ray_module, cancellations, _affinity = _install_remote_ray_modules(
        monkeypatch,
        states=[_remote_state(((NODE_A, 1, 1),), revision=1)],
    )
    expected_classification = "node_probe_unavailable"
    if mode == "setup":
        ray_module.remote = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("setup poison"))
    elif mode == "unfinished":
        ray_module.wait = lambda refs, **_kwargs: ([], list(refs))
        expected_classification = "node_probe_timeout"
    elif mode == "bad-list":
        ray_module.get = lambda *_args, **_kwargs: {}
        expected_classification = "invalid_node_observation"
    elif mode == "bad-item":
        ray_module.get = lambda *_args, **_kwargs: [None]
        expected_classification = "invalid_node_observation"
    else:
        ray_module.get = lambda *_args, **_kwargs: [_runtime_mapping(node_id=NODE_B)]
        expected_classification = "node_id_mismatch"

    result = probe._make_cluster_probe_coordinator()(
        1.0, 1, probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES
    )

    assert result == {"ok": False, "classification": expected_classification}
    if mode == "unfinished":
        assert len(cancellations) == 1


def test_remote_synchronous_cancel_failure_preserves_wait_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ray_module, _cancellations, _affinity = _install_remote_ray_modules(
        monkeypatch,
        states=[_remote_state(((NODE_A, 1, 1),), revision=1)],
    )
    ray_module.wait = lambda refs, **_kwargs: ([], list(refs))
    ray_module.cancel = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("cancel poison")
    )

    result = probe._make_cluster_probe_coordinator()(
        1.0, 1, probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES
    )

    assert result == {"ok": False, "classification": "node_probe_timeout"}


@pytest.mark.parametrize(
    ("timeout_seconds", "max_nodes", "max_bytes"),
    [
        (True, 1, probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES),
        (10**10000, 1, probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES),
        (1.0, 0, probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES),
        (1.0, 1, 1),
    ],
    ids=["bool-timeout", "huge-timeout", "zero-nodes", "wrong-byte-bound"],
)
def test_remote_coordinator_rejects_invalid_operational_bounds(
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: object,
    max_nodes: int,
    max_bytes: int,
) -> None:
    ray_module = ModuleType("ray")
    ray_module.__version__ = "2.56.0"
    monkeypatch.setitem(sys.modules, "ray", ray_module)

    result = probe._make_cluster_probe_coordinator()(
        timeout_seconds,  # type: ignore[arg-type]
        max_nodes,
        max_bytes,
    )

    assert result == {"ok": False, "classification": "invalid_configuration"}


def test_remote_coordinator_checks_exact_version_before_private_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ray_module = ModuleType("ray")
    ray_module.__version__ = "2.56.1"
    monkeypatch.setitem(sys.modules, "ray", ray_module)

    result = probe._make_cluster_probe_coordinator()(
        1.0,
        1,
        probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES,
    )

    assert result == {"ok": False, "classification": "unsupported_ray_version"}


def test_remote_coordinator_is_by_value_and_invocable_without_django_ray_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ray.cloudpickle as ray_cloudpickle

    payload = ray_cloudpickle.dumps(probe._make_cluster_probe_coordinator())
    original_import = builtins.__import__
    fake_ray = ModuleType("ray")
    fake_ray.__version__ = "2.56.0"
    fake_ray.get_runtime_context = lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))
    monkeypatch.setitem(sys.modules, "ray", fake_ray)

    def guarded_import(name: str, *args: object, **kwargs: object) -> Any:
        if name == "django_ray" or name.startswith("django_ray."):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    try:
        builtins.__import__ = guarded_import
        coordinator = ray_cloudpickle.loads(payload)
        result = coordinator(
            0.01,
            1,
            probe.RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES,
        )
    finally:
        builtins.__import__ = original_import

    assert result == {"ok": False, "classification": "node_probe_unavailable"}
    assert "target_key" not in repr(coordinator.__closure__)


class _FakeCoordinatorRemote:
    def __init__(self, ref: object) -> None:
        self.ref = ref

    def remote(self, *_args: object) -> object:
        return self.ref


class _FakeOuterRay:
    def __init__(self, *, ready: bool, get_error: Exception | None = None) -> None:
        self.ref = object()
        self.ready = ready
        self.get_error = get_error
        self.cancelled: list[tuple[object, bool, bool]] = []
        self.remote_options: list[dict[str, object]] = []
        self.get_timeouts: list[float] = []

    def remote(self, **options: object) -> Any:
        self.remote_options.append(options)
        return lambda _function: _FakeCoordinatorRemote(self.ref)

    def wait(self, refs: list[object], **_kwargs: object) -> tuple[list[object], list[object]]:
        return (list(refs), []) if self.ready else ([], list(refs))

    def get(self, _ref: object, *, timeout: float) -> object:
        self.get_timeouts.append(timeout)
        if self.get_error is not None:
            raise self.get_error
        return _interval_envelope()

    def cancel(self, ref: object, *, force: bool, recursive: bool) -> None:
        self.cancelled.append((ref, force, recursive))


def test_outer_timeout_attempts_exact_recursive_cancellation_without_shutdown() -> None:
    fake_ray = _FakeOuterRay(ready=False)

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._run_cluster_coordinator(
            fake_ray,
            deadline=time.monotonic() + 0.1,
            max_nodes=1,
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_TIMEOUT)
    assert fake_ray.cancelled == [(fake_ray.ref, True, True)]
    assert fake_ray.remote_options == [{"num_cpus": 0, "max_retries": 0}]


def test_outer_cleanup_thread_failure_never_masks_primary_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ray = _FakeOuterRay(ready=False)

    def reject_thread(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("thread limit contains secret")

    monkeypatch.setattr(probe.threading, "Thread", reject_thread)

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._run_cluster_coordinator(
            fake_ray,
            deadline=time.monotonic() + 0.1,
            max_nodes=1,
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_TIMEOUT)

    assert probe._CANCEL_THREAD_SLOT.acquire(blocking=False)
    probe._CANCEL_THREAD_SLOT.release()


def test_occupied_cancel_slot_bounds_repeated_timeout_cleanup() -> None:
    fake_ray = _FakeOuterRay(ready=False)
    assert probe._CANCEL_THREAD_SLOT.acquire(blocking=False)
    try:
        with pytest.raises(probe.RayTargetProbeError) as error:
            probe._run_cluster_coordinator(
                fake_ray,
                deadline=time.monotonic() + 0.1,
                max_nodes=1,
            )
    finally:
        probe._CANCEL_THREAD_SLOT.release()

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_TIMEOUT)
    assert fake_ray.cancelled == []


def test_cancel_helper_handles_empty_refs_cancel_and_wait_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SynchronousThread:
        def __init__(self, *, target: Any, daemon: bool) -> None:
            assert daemon is True
            self.target = target

        def start(self) -> None:
            self.target()

        def join(self, *, timeout: float) -> None:
            assert timeout > 0

    monkeypatch.setattr(probe.threading, "Thread", SynchronousThread)
    fake_ray = SimpleNamespace(
        cancel=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cancel")),
        wait=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("wait")),
    )

    probe._cancel_refs_bounded(fake_ray, [], deadline=time.monotonic() + 1)
    probe._cancel_refs_bounded(fake_ray, [object()], deadline=time.monotonic() + 1)

    assert probe._CANCEL_THREAD_SLOT.acquire(blocking=False)
    probe._CANCEL_THREAD_SLOT.release()


def test_outer_failing_ref_is_fixed_and_get_is_deadline_bounded() -> None:
    poison = "private remote traceback"
    fake_ray = _FakeOuterRay(ready=True, get_error=RuntimeError(poison))

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._run_cluster_coordinator(
            fake_ray,
            deadline=time.monotonic() + 1.0,
            max_nodes=1,
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)
    assert poison not in str(error.value)
    assert len(fake_ray.get_timeouts) == 1
    assert 0 < fake_ray.get_timeouts[0] <= 1.0


def test_outer_happy_path_decodes_without_real_ray() -> None:
    fake_ray = _FakeOuterRay(ready=True)

    interval = probe._run_cluster_coordinator(
        fake_ray,
        deadline=time.monotonic() + 1.0,
        max_nodes=1,
    )

    assert interval.before.node_ids == (NODE_A,)
    assert interval.coordinator.node_id == NODE_A


def test_outer_submission_failure_is_fixed_and_redacted() -> None:
    class BrokenRay(_FakeOuterRay):
        def remote(self, **_options: object) -> Any:
            raise RuntimeError("submission credential")

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._run_cluster_coordinator(
            BrokenRay(ready=True),
            deadline=time.monotonic() + 1.0,
            max_nodes=1,
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)


def test_outer_propagates_its_own_deadline_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ray = _FakeOuterRay(ready=True)
    calls = 0

    def remaining(_deadline: float) -> float:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise probe.RayTargetProbeError(probe.RayTargetProbeFailure.NODE_PROBE_TIMEOUT)
        return 1.0

    monkeypatch.setattr(probe, "_remaining_seconds", remaining)

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._run_cluster_coordinator(
            fake_ray,
            deadline=time.monotonic() + 1.0,
            max_nodes=1,
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_PROBE_TIMEOUT)
    assert fake_ray.cancelled == [(fake_ray.ref, True, True)]


def test_ambient_ray_version_bypass_does_not_weaken_exact_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAY_IGNORE_VERSION_MISMATCH", "1")
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(__version__="2.56.1", is_initialized=lambda: True),
    )

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._collect_raw_cluster_observation(timeout_seconds=1.0, max_nodes=1)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.UNSUPPORTED_RAY_VERSION)


def test_current_caller_observation_success_and_fixed_failures() -> None:
    good_ray = SimpleNamespace(
        __version__="2.56.0",
        get_runtime_context=lambda: SimpleNamespace(
            get_node_id=lambda: NODE_A,
            get_session_name=lambda: SESSION,
        ),
    )
    assert probe._current_caller_observation(good_ray).session_name == SESSION

    broken_context = SimpleNamespace(
        __version__="2.56.0",
        get_runtime_context=lambda: (_ for _ in ()).throw(RuntimeError("context poison")),
    )
    with pytest.raises(probe.RayTargetProbeError) as unavailable:
        probe._current_caller_observation(broken_context)
    _assert_probe_failure(unavailable, probe.RayTargetProbeFailure.PUBLIC_RUNTIME_UNAVAILABLE)

    invalid_public_value = SimpleNamespace(
        __version__="2.56.0",
        get_runtime_context=lambda: SimpleNamespace(
            get_node_id=lambda: "bad",
            get_session_name=lambda: SESSION,
        ),
    )
    with pytest.raises(probe.RayTargetProbeError) as invalid:
        probe._current_caller_observation(invalid_public_value)
    _assert_probe_failure(invalid, probe.RayTargetProbeFailure.INVALID_NODE_OBSERVATION)


def test_raw_collection_maps_import_and_initialization_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def block_ray(name: str, *args: object, **kwargs: object) -> Any:
        if name == "ray":
            raise ImportError("ray import poison")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_ray)
    with pytest.raises(probe.RayTargetProbeError) as missing:
        probe._collect_raw_cluster_observation(timeout_seconds=1.0, max_nodes=1)
    _assert_probe_failure(missing, probe.RayTargetProbeFailure.PUBLIC_RUNTIME_UNAVAILABLE)
    monkeypatch.setattr(builtins, "__import__", original_import)

    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            __version__="2.56.0",
            is_initialized=lambda: (_ for _ in ()).throw(RuntimeError("init poison")),
        ),
    )
    with pytest.raises(probe.RayTargetProbeError) as unavailable:
        probe._collect_raw_cluster_observation(timeout_seconds=1.0, max_nodes=1)
    _assert_probe_failure(unavailable, probe.RayTargetProbeFailure.PUBLIC_RUNTIME_UNAVAILABLE)

    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(__version__="2.56.0", is_initialized=lambda: False),
    )
    with pytest.raises(probe.RayTargetProbeError) as stopped:
        probe._collect_raw_cluster_observation(timeout_seconds=1.0, max_nodes=1)
    _assert_probe_failure(stopped, probe.RayTargetProbeFailure.RAY_NOT_INITIALIZED)


def test_raw_collection_requires_caller_on_a_schedulable_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ray = SimpleNamespace(__version__="2.56.0", is_initialized=lambda: True)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    raw = _raw_observation()
    caller = probe._RuntimeObservation(
        node_id=NODE_C,
        session_name=SESSION,
        ray_version="2.56.0",
        python_implementation="cpython",
        python_version=(3, 12, 12),
    )
    monkeypatch.setattr(probe, "_current_caller_observation", lambda _ray: caller)
    monkeypatch.setattr(
        probe,
        "_run_cluster_coordinator",
        lambda *_args, **_kwargs: raw.interval,
    )

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._collect_raw_cluster_observation(timeout_seconds=1.0, max_nodes=2)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.NODE_ID_MISMATCH)


@pytest.mark.parametrize("mismatch", ["session", "runtime"])
def test_raw_collection_rejects_caller_coordinator_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    fake_ray = SimpleNamespace(__version__="2.56.0", is_initialized=lambda: True)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    raw = _raw_observation()
    caller = raw.caller
    if mismatch == "session":
        caller = probe._RuntimeObservation(
            node_id=NODE_A,
            session_name="session_other",
            ray_version="2.56.0",
            python_implementation="cpython",
            python_version=(3, 12, 12),
        )
        classification = probe.RayTargetProbeFailure.SESSION_MISMATCH
    else:
        caller = probe._RuntimeObservation(
            node_id=NODE_A,
            session_name=SESSION,
            ray_version="2.56.0",
            python_implementation="cpython",
            python_version=(3, 12, 13),
        )
        classification = probe.RayTargetProbeFailure.RUNTIME_MISMATCH
    monkeypatch.setattr(probe, "_current_caller_observation", lambda _ray: caller)
    monkeypatch.setattr(
        probe,
        "_run_cluster_coordinator",
        lambda *_args, **_kwargs: raw.interval,
    )

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe._collect_raw_cluster_observation(timeout_seconds=1.0, max_nodes=2)

    _assert_probe_failure(error, classification)


def test_public_probe_rejects_ray_job_before_collecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def collect(**_kwargs: object) -> probe._RawClusterObservation:
        nonlocal called
        called = True
        return _raw_observation()

    monkeypatch.setattr(probe, "_collect_raw_cluster_observation", collect)

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe.probe_ray_target(
            _expectation(runner_family=RayRunnerFamily.RAY_JOB),
            ttl_seconds=60,
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.UNSUPPORTED_RUNNER_FAMILY)
    assert called is False


def test_public_probe_rejects_wrong_and_noncanonical_expectations_before_collect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe,
        "_collect_raw_cluster_observation",
        lambda **_kwargs: pytest.fail("collection must not start"),
    )

    with pytest.raises(probe.RayTargetProbeError) as wrong_type:
        probe.probe_ray_target(object(), ttl_seconds=60)  # type: ignore[arg-type]
    _assert_probe_failure(wrong_type, probe.RayTargetProbeFailure.INVALID_EXPECTATION)

    invalid = RayTargetExpectation(
        target_key="UPPERCASE",
        runner_family=RayRunnerFamily.RAY_CORE,
        cluster_session=SESSION,
        policy_revision=1,
        runtime=RUNTIME,
    )
    with pytest.raises(probe.RayTargetProbeError) as noncanonical:
        probe.probe_ray_target(invalid, ttl_seconds=60)
    _assert_probe_failure(noncanonical, probe.RayTargetProbeFailure.INVALID_EXPECTATION)


@pytest.mark.parametrize("ttl_seconds", [True, 0, RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS + 1])
def test_public_probe_requires_exact_bounded_explicit_ttl(
    monkeypatch: pytest.MonkeyPatch,
    ttl_seconds: object,
) -> None:
    called = False

    def collect(**_kwargs: object) -> probe._RawClusterObservation:
        nonlocal called
        called = True
        return _raw_observation()

    monkeypatch.setattr(probe, "_collect_raw_cluster_observation", collect)

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe.probe_ray_target(
            _expectation(),
            ttl_seconds=ttl_seconds,  # type: ignore[arg-type]
        )

    _assert_probe_failure(error, probe.RayTargetProbeFailure.INVALID_CONFIGURATION)
    assert called is False


def test_public_probe_builds_only_canonical_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe,
        "_collect_raw_cluster_observation",
        lambda **_kwargs: _raw_observation(),
    )
    expectation = _expectation()

    attestation = probe.probe_ray_target(expectation, ttl_seconds=60)

    assert (
        compare_ray_target_attestation(
            expectation,
            attestation,
            now=attestation.observed_at,
        )
        is None
    )
    assert attestation.expectation == expectation
    assert tuple(node.node_id for node in attestation.nodes) == (NODE_A, NODE_B)
    assert attestation.boundary.resource_state_version_before == 10
    assert attestation.boundary.resource_state_version_after == 12
    assert (attestation.expires_at - attestation.observed_at).total_seconds() == 60


def test_public_probe_maps_canonical_builder_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray import target_attestation

    monkeypatch.setattr(
        probe,
        "_collect_raw_cluster_observation",
        lambda **_kwargs: _raw_observation(),
    )
    monkeypatch.setattr(
        target_attestation,
        "build_ray_node_observation",
        lambda **_kwargs: (_ for _ in ()).throw(RayTargetAttestationEncodeError()),
    )

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe.probe_ray_target(_expectation(), ttl_seconds=60)

    _assert_probe_failure(error, probe.RayTargetProbeFailure.ATTESTATION_BUILD_FAILED)


@pytest.mark.parametrize(
    ("expectation", "classification"),
    [
        (
            _expectation(cluster_session="session_other"),
            probe.RayTargetProbeFailure.SESSION_MISMATCH,
        ),
        (
            _expectation(
                runtime=RayRuntimeVersion(
                    ray_major=2,
                    ray_minor=56,
                    ray_patch=0,
                    python_implementation="cpython",
                    python_major=3,
                    python_minor=12,
                    python_patch=13,
                )
            ),
            probe.RayTargetProbeFailure.RUNTIME_MISMATCH,
        ),
    ],
)
def test_public_probe_rejects_expected_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    expectation: RayTargetExpectation,
    classification: probe.RayTargetProbeFailure,
) -> None:
    monkeypatch.setattr(
        probe,
        "_collect_raw_cluster_observation",
        lambda **_kwargs: _raw_observation(),
    )

    with pytest.raises(probe.RayTargetProbeError) as error:
        probe.probe_ray_target(expectation, ttl_seconds=60)

    _assert_probe_failure(error, classification)


@pytest.mark.real_ray
def test_real_local_ray_probe_allows_advancing_revisions_and_preserves_runtime(
    ray_cluster: Any,
) -> None:
    context = ray_cluster.get_runtime_context()
    expectation = RayTargetExpectation(
        target_key="local",
        runner_family=RayRunnerFamily.RAY_CORE,
        cluster_session=context.get_session_name(),
        policy_revision=1,
        runtime=RayRuntimeVersion(
            ray_major=2,
            ray_minor=56,
            ray_patch=0,
            python_implementation=platform.python_implementation().lower(),
            python_major=sys.version_info.major,
            python_minor=sys.version_info.minor,
            python_patch=sys.version_info.micro,
        ),
    )

    attestation = probe.probe_ray_target(
        expectation,
        ttl_seconds=60,
        timeout_seconds=20,
        max_nodes=4,
    )

    assert (
        compare_ray_target_attestation(
            expectation,
            attestation,
            now=attestation.observed_at,
        )
        is None
    )
    assert ray_cluster.is_initialized()
    assert (
        attestation.boundary.resource_state_version_after
        > attestation.boundary.resource_state_version_before
    )
    before_ids = tuple(item.node_id for item in attestation.boundary.node_state_versions_before)
    after_ids = tuple(item.node_id for item in attestation.boundary.node_state_versions_after)
    assert before_ids == after_ids == tuple(node.node_id for node in attestation.nodes)
    assert all(node.cluster_session == expectation.cluster_session for node in attestation.nodes)
