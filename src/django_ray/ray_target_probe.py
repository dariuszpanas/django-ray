"""Django-free, fail-closed feasibility probe for one Ray Core target.

The probe observes one bounded interval.  It does not activate a target, grant
worker capability, or make a claim about cluster membership after the second
snapshot.  Ray 2.56.0 resource-state counters advance during ordinary
heartbeats, so they are retained as non-regressing before/after diagnostics;
they are not membership epochs.

Remote code is produced by a local factory and serialized by value.  Generic
Ray nodes therefore need Ray itself, but do not need ``django_ray`` installed.
All private Ray access is confined to that remote bootstrap and occurs only
after an exact Ray 2.56.0 version check.
"""

from __future__ import annotations

import math
import platform
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Never

if TYPE_CHECKING:
    from django_ray.target_attestation import RayClusterAttestation, RayTargetExpectation

RAY_TARGET_PROBE_RAY_VERSION = "2.56.0"
RAY_TARGET_PROBE_DEFAULT_TIMEOUT_SECONDS = 30.0
RAY_TARGET_PROBE_MAX_TIMEOUT_SECONDS = 120.0
RAY_TARGET_PROBE_DEFAULT_MAX_NODES = 64
RAY_TARGET_PROBE_MAX_NODES = 256
RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES = 4 * 1024 * 1024

_SESSION_NAME_MAX_CHARS = 256
_NODE_ID_CHARS = 56
_PYTHON_IMPLEMENTATION_MAX_CHARS = 64
_MAX_COUNTER = (1 << 63) - 1
_SESSION_NAME = re.compile(r"session_[A-Za-z0-9][A-Za-z0-9_.-]{0,247}")
_NODE_ID = re.compile(r"[0-9a-f]{56}")
_PYTHON_IMPLEMENTATION = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_PYTHON_VERSION = re.compile(r"([1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})")
_CANCEL_THREAD_SLOT = threading.BoundedSemaphore(1)


class RayTargetProbeFailure(StrEnum):
    """Fixed, redaction-safe probe failure classifications."""

    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_EXPECTATION = "invalid_expectation"
    UNSUPPORTED_RUNNER_FAMILY = "unsupported_runner_family"
    RAY_NOT_INITIALIZED = "ray_not_initialized"
    UNSUPPORTED_RAY_VERSION = "unsupported_ray_version"
    PUBLIC_RUNTIME_UNAVAILABLE = "public_runtime_unavailable"
    PRIVATE_API_UNAVAILABLE = "private_api_unavailable"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    INVALID_SNAPSHOT = "invalid_snapshot"
    RESOURCE_LIMIT = "resource_limit"
    EMPTY_CLUSTER = "empty_cluster"
    NODE_PROBE_TIMEOUT = "node_probe_timeout"
    NODE_PROBE_UNAVAILABLE = "node_probe_unavailable"
    INVALID_NODE_OBSERVATION = "invalid_node_observation"
    NODE_ID_MISMATCH = "node_id_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"
    MEMBERSHIP_CHANGED = "membership_changed"
    REVISION_REGRESSION = "revision_regression"
    ATTESTATION_BUILD_FAILED = "attestation_build_failed"


class RayTargetProbeError(RuntimeError):
    """Reject a probe without retaining dependency or target details."""

    def __init__(self, classification: RayTargetProbeFailure) -> None:
        self.classification = classification
        super().__init__(f"Ray target probe failed: {classification.value}")


@dataclass(frozen=True, slots=True)
class _RuntimeObservation:
    node_id: str
    session_name: str
    ray_version: str
    python_implementation: str
    python_version: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _ResourceStateSnapshot:
    session_name: str
    cluster_resource_state_version: int
    node_state_versions: tuple[tuple[str, int], ...]

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node_id for node_id, _version in self.node_state_versions)


@dataclass(frozen=True, slots=True)
class _ClusterIntervalObservation:
    coordinator: _RuntimeObservation
    before: _ResourceStateSnapshot
    after: _ResourceStateSnapshot
    nodes: tuple[_RuntimeObservation, ...]


@dataclass(frozen=True, slots=True)
class _RawClusterObservation:
    caller: _RuntimeObservation
    interval: _ClusterIntervalObservation


def _reject(classification: RayTargetProbeFailure) -> Never:
    raise RayTargetProbeError(classification)


def _valid_bounded_string(value: object, *, maximum: int, pattern: re.Pattern[str]) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and "\x00" not in value
        and pattern.fullmatch(value) is not None
    )


def _normalize_python_implementation(value: object) -> str:
    if type(value) is not str:
        _reject(RayTargetProbeFailure.INVALID_NODE_OBSERVATION)
    normalized = value.strip().lower()
    if not _valid_bounded_string(
        normalized,
        maximum=_PYTHON_IMPLEMENTATION_MAX_CHARS,
        pattern=_PYTHON_IMPLEMENTATION,
    ):
        _reject(RayTargetProbeFailure.INVALID_NODE_OBSERVATION)
    return normalized


def _decode_python_version(value: object) -> tuple[int, int, int]:
    if type(value) is not str:
        _reject(RayTargetProbeFailure.INVALID_NODE_OBSERVATION)
    match = _PYTHON_VERSION.fullmatch(value)
    if match is None:
        _reject(RayTargetProbeFailure.INVALID_NODE_OBSERVATION)
    major, minor, patch = (int(component) for component in match.groups())
    return major, minor, patch


def _decode_runtime_observation(value: object) -> _RuntimeObservation:
    if type(value) is not dict or set(value) != {
        "node_id",
        "session_name",
        "ray_version",
        "python_implementation",
        "python_version",
    }:
        _reject(RayTargetProbeFailure.INVALID_NODE_OBSERVATION)
    node_id = value["node_id"]
    session_name = value["session_name"]
    ray_version = value["ray_version"]
    if not _valid_bounded_string(node_id, maximum=_NODE_ID_CHARS, pattern=_NODE_ID):
        _reject(RayTargetProbeFailure.INVALID_NODE_OBSERVATION)
    if not _valid_bounded_string(
        session_name,
        maximum=_SESSION_NAME_MAX_CHARS,
        pattern=_SESSION_NAME,
    ):
        _reject(RayTargetProbeFailure.INVALID_NODE_OBSERVATION)
    if type(ray_version) is not str or ray_version != RAY_TARGET_PROBE_RAY_VERSION:
        _reject(RayTargetProbeFailure.UNSUPPORTED_RAY_VERSION)
    return _RuntimeObservation(
        node_id=node_id,
        session_name=session_name,
        ray_version=ray_version,
        python_implementation=_normalize_python_implementation(value["python_implementation"]),
        python_version=_decode_python_version(value["python_version"]),
    )


def _decode_snapshot(value: object, *, max_nodes: int) -> _ResourceStateSnapshot:
    if type(value) is not dict or set(value) != {
        "session_name",
        "cluster_resource_state_version",
        "node_state_versions",
    }:
        _reject(RayTargetProbeFailure.INVALID_SNAPSHOT)
    session_name = value["session_name"]
    revision = value["cluster_resource_state_version"]
    raw_versions = value["node_state_versions"]
    if (
        not _valid_bounded_string(
            session_name,
            maximum=_SESSION_NAME_MAX_CHARS,
            pattern=_SESSION_NAME,
        )
        or type(revision) is not int
        or not 0 <= revision <= _MAX_COUNTER
    ):
        _reject(RayTargetProbeFailure.INVALID_SNAPSHOT)
    if type(raw_versions) is not list or not raw_versions:
        _reject(RayTargetProbeFailure.INVALID_SNAPSHOT)
    if len(raw_versions) > max_nodes:
        _reject(RayTargetProbeFailure.RESOURCE_LIMIT)
    versions: list[tuple[str, int]] = []
    for item in raw_versions:
        if type(item) is not list or len(item) != 2:
            _reject(RayTargetProbeFailure.INVALID_SNAPSHOT)
        node_id, node_revision = item
        if (
            not _valid_bounded_string(node_id, maximum=_NODE_ID_CHARS, pattern=_NODE_ID)
            or type(node_revision) is not int
            or not 0 <= node_revision <= _MAX_COUNTER
        ):
            _reject(RayTargetProbeFailure.INVALID_SNAPSHOT)
        versions.append((node_id, node_revision))
    if versions != sorted(versions) or len({node_id for node_id, _ in versions}) != len(versions):
        _reject(RayTargetProbeFailure.INVALID_SNAPSHOT)
    return _ResourceStateSnapshot(
        session_name=session_name,
        cluster_resource_state_version=revision,
        node_state_versions=tuple(versions),
    )


def _decode_remote_interval(value: object, *, max_nodes: int) -> _ClusterIntervalObservation:
    if type(value) is not dict:
        _reject(RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)
    if value.get("ok") is False and set(value) == {"ok", "classification"}:
        classification = value["classification"]
        if type(classification) is not str:
            _reject(RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)
        try:
            fixed_failure = RayTargetProbeFailure(classification)
        except (TypeError, ValueError):
            _reject(RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)
        _reject(fixed_failure)
    if value.get("ok") is not True or set(value) != {
        "ok",
        "coordinator",
        "before",
        "after",
        "nodes",
    }:
        _reject(RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)
    raw_nodes = value["nodes"]
    if type(raw_nodes) is not list or not 0 < len(raw_nodes) <= max_nodes:
        _reject(RayTargetProbeFailure.RESOURCE_LIMIT)
    interval = _ClusterIntervalObservation(
        coordinator=_decode_runtime_observation(value["coordinator"]),
        before=_decode_snapshot(value["before"], max_nodes=max_nodes),
        after=_decode_snapshot(value["after"], max_nodes=max_nodes),
        nodes=tuple(_decode_runtime_observation(node) for node in raw_nodes),
    )
    _validate_interval(interval)
    return interval


def _validate_interval(interval: _ClusterIntervalObservation) -> None:
    expected_session = interval.coordinator.session_name
    if (
        interval.before.session_name != expected_session
        or interval.after.session_name != expected_session
    ):
        _reject(RayTargetProbeFailure.SESSION_MISMATCH)
    if interval.before.node_ids != interval.after.node_ids:
        _reject(RayTargetProbeFailure.MEMBERSHIP_CHANGED)
    if interval.coordinator.node_id not in interval.before.node_ids:
        _reject(RayTargetProbeFailure.NODE_ID_MISMATCH)
    observed_ids = tuple(node.node_id for node in interval.nodes)
    if observed_ids != interval.before.node_ids:
        _reject(RayTargetProbeFailure.NODE_ID_MISMATCH)
    expected_runtime = (
        interval.coordinator.ray_version,
        interval.coordinator.python_implementation,
        interval.coordinator.python_version,
    )
    for node in interval.nodes:
        if node.session_name != expected_session:
            _reject(RayTargetProbeFailure.SESSION_MISMATCH)
        if (node.ray_version, node.python_implementation, node.python_version) != expected_runtime:
            _reject(RayTargetProbeFailure.RUNTIME_MISMATCH)
    if (
        interval.after.cluster_resource_state_version
        < interval.before.cluster_resource_state_version
    ):
        _reject(RayTargetProbeFailure.REVISION_REGRESSION)
    before_versions = dict(interval.before.node_state_versions)
    if any(
        after_version < before_versions[node_id]
        for node_id, after_version in interval.after.node_state_versions
    ):
        _reject(RayTargetProbeFailure.REVISION_REGRESSION)


def _validate_probe_limits(*, timeout_seconds: float, max_nodes: int) -> tuple[float, int]:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        _reject(RayTargetProbeFailure.INVALID_CONFIGURATION)
    try:
        normalized_timeout = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        _reject(RayTargetProbeFailure.INVALID_CONFIGURATION)
    if (
        not math.isfinite(normalized_timeout)
        or not 0 < normalized_timeout <= RAY_TARGET_PROBE_MAX_TIMEOUT_SECONDS
        or type(max_nodes) is not int
        or not 0 < max_nodes <= RAY_TARGET_PROBE_MAX_NODES
    ):
        _reject(RayTargetProbeFailure.INVALID_CONFIGURATION)
    return normalized_timeout, max_nodes


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _reject(RayTargetProbeFailure.NODE_PROBE_TIMEOUT)
    return remaining


def _cancel_refs_bounded(
    ray_module: Any,
    refs: Sequence[object],
    *,
    deadline: float,
) -> None:
    """Make one bounded best-effort cancellation pass without masking failure."""
    owned_refs = tuple(refs)
    if not owned_refs:
        return
    if not _CANCEL_THREAD_SLOT.acquire(blocking=False):
        return

    def cancel_owned() -> None:
        try:
            for ref in owned_refs:
                try:
                    ray_module.cancel(ref, force=True, recursive=True)
                except Exception:
                    pass
        finally:
            _CANCEL_THREAD_SLOT.release()

    try:
        thread = threading.Thread(target=cancel_owned, daemon=True)
        thread.start()
    except Exception:
        try:
            _CANCEL_THREAD_SLOT.release()
        except ValueError:
            pass
        return
    try:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            thread.join(timeout=remaining)
    except Exception:
        return
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            ray_module.wait(
                list(owned_refs),
                num_returns=len(owned_refs),
                timeout=remaining,
                fetch_local=False,
            )
        except Exception:
            pass


def _current_caller_observation(ray_module: Any) -> _RuntimeObservation:
    try:
        context = ray_module.get_runtime_context()
        raw = {
            "node_id": context.get_node_id(),
            "session_name": context.get_session_name(),
            "ray_version": ray_module.__version__,
            "python_implementation": platform.python_implementation().strip().lower(),
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
        }
        return _decode_runtime_observation(raw)
    except RayTargetProbeError:
        raise
    except Exception:
        _reject(RayTargetProbeFailure.PUBLIC_RUNTIME_UNAVAILABLE)


def _make_cluster_probe_coordinator() -> Callable[[float, int, int], dict[str, object]]:
    """Return stdlib-plus-Ray remote code serialized by value, not module name."""
    supported_ray_version = "2.56.0"
    max_timeout_seconds = 120.0
    max_nodes_bound = 256
    expected_max_bytes = 4 * 1024 * 1024
    max_counter = (1 << 63) - 1
    max_session_chars = 256
    node_id_chars = 56

    class _RemoteProbeError(Exception):
        def __init__(self, classification: str) -> None:
            self.classification = classification

    def reject(classification: str) -> Never:
        raise _RemoteProbeError(classification)

    def remaining(deadline: float) -> float:
        import time

        value = deadline - time.monotonic()
        if value <= 0:
            reject("node_probe_timeout")
        return value

    def runtime_observation() -> dict[str, object]:
        import platform
        import sys

        import ray

        context = ray.get_runtime_context()
        return {
            "node_id": context.get_node_id(),
            "session_name": context.get_session_name(),
            "ray_version": ray.__version__,
            "python_implementation": platform.python_implementation().strip().lower(),
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
        }

    def snapshot(*, deadline: float, max_nodes: int, max_bytes: int) -> dict[str, object]:
        import math
        import re

        try:
            from ray._private.worker import global_worker
            from ray.core.generated import autoscaler_pb2

            reply_type = autoscaler_pb2.GetClusterResourceStateReply
            node_status = autoscaler_pb2.NodeStatus

            expected_statuses = {
                "UNSPECIFIED": 0,
                "RUNNING": 1,
                "DEAD": 2,
                "IDLE": 3,
                "DRAINING": 4,
            }
            if any(node_status.Value(name) != value for name, value in expected_statuses.items()):
                reject("private_api_unavailable")
            client = global_worker.gcs_client
        except _RemoteProbeError:
            raise
        except Exception:
            reject("private_api_unavailable")
        try:
            encoded = client.get_cluster_resource_state(
                timeout_s=max(1, math.ceil(remaining(deadline)))
            )
        except _RemoteProbeError:
            raise
        except Exception:
            reject("snapshot_unavailable")
        if type(encoded) is not bytes:
            reject("invalid_snapshot")
        if len(encoded) > max_bytes:
            reject("resource_limit")
        try:
            reply = reply_type()
            reply.ParseFromString(encoded)
            state = reply.cluster_resource_state
            raw_nodes = state.node_states
            if len(raw_nodes) > max_nodes:
                reject("resource_limit")
            session_name = state.cluster_session_name
            revision = state.cluster_resource_state_version
        except _RemoteProbeError:
            raise
        except Exception:
            reject("invalid_snapshot")
        session_pattern = re.compile(r"session_[A-Za-z0-9][A-Za-z0-9_.-]{0,247}")
        if (
            type(session_name) is not str
            or not 0 < len(session_name) <= max_session_chars
            or session_pattern.fullmatch(session_name) is None
            or type(revision) is not int
            or not 0 <= revision <= max_counter
        ):
            reject("invalid_snapshot")
        node_pattern = re.compile(r"[0-9a-f]{56}")
        seen: set[str] = set()
        live_versions: list[list[object]] = []
        for node in raw_nodes:
            try:
                raw_node_id = node.node_id
                status = node.status
                node_version = node.node_state_version
            except Exception:
                reject("invalid_snapshot")
            if type(raw_node_id) is not bytes:
                reject("invalid_snapshot")
            node_id = raw_node_id.hex()
            if (
                len(node_id) != node_id_chars
                or node_pattern.fullmatch(node_id) is None
                or node_id in seen
                or type(status) is not int
                or type(node_version) is not int
                or not 0 <= node_version <= max_counter
            ):
                reject("invalid_snapshot")
            seen.add(node_id)
            if status in (2, 4):
                continue
            if status not in (1, 3):
                reject("invalid_snapshot")
            live_versions.append([node_id, node_version])
        if not live_versions:
            reject("empty_cluster")
        live_versions.sort()
        return {
            "session_name": session_name,
            "cluster_resource_state_version": revision,
            "node_state_versions": live_versions,
        }

    def cancel_refs(ray_module: Any, refs: list[object]) -> None:
        for ref in refs:
            try:
                ray_module.cancel(ref, force=True, recursive=True)
            except Exception:
                pass

    def observe_nodes(
        *,
        node_ids: list[str],
        deadline: float,
    ) -> list[dict[str, object]]:
        import ray

        try:
            from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

            remote_probe = ray.remote(num_cpus=0, max_retries=0)(runtime_observation)
        except Exception:
            reject("node_probe_unavailable")
        refs: list[object] = []
        expected_by_ref: dict[object, str] = {}
        try:
            for node_id in node_ids:
                ref = remote_probe.options(
                    scheduling_strategy=NodeAffinitySchedulingStrategy(
                        node_id=node_id,
                        soft=False,
                    )
                ).remote()
                refs.append(ref)
                expected_by_ref[ref] = node_id
                remaining(deadline)
            ready, unfinished = ray.wait(
                refs,
                num_returns=len(refs),
                timeout=remaining(deadline),
            )
        except _RemoteProbeError:
            cancel_refs(ray, refs)
            raise
        except Exception:
            cancel_refs(ray, refs)
            reject("node_probe_unavailable")
        if unfinished:
            cancel_refs(ray, refs)
            reject("node_probe_timeout")
        try:
            raw_observations = ray.get(ready, timeout=remaining(deadline))
        except _RemoteProbeError:
            raise
        except Exception:
            reject("node_probe_unavailable")
        if type(raw_observations) is not list or len(raw_observations) != len(node_ids):
            reject("invalid_node_observation")
        observations_by_node: dict[str, dict[str, object]] = {}
        for ref, observation in zip(ready, raw_observations, strict=True):
            if type(observation) is not dict or set(observation) != {
                "node_id",
                "session_name",
                "ray_version",
                "python_implementation",
                "python_version",
            }:
                reject("invalid_node_observation")
            expected_node_id = expected_by_ref.get(ref)
            observed_node_id = observation.get("node_id")
            if observed_node_id != expected_node_id or expected_node_id in observations_by_node:
                reject("node_id_mismatch")
            observations_by_node[expected_node_id] = observation
        return [observations_by_node[node_id] for node_id in sorted(node_ids)]

    def coordinator(
        timeout_seconds: float,
        max_nodes: int,
        max_resource_state_bytes: int,
    ) -> dict[str, object]:
        import math
        import time
        from typing import cast

        try:
            import ray

            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
                reject("invalid_configuration")
            try:
                normalized_timeout = float(timeout_seconds)
            except (OverflowError, TypeError, ValueError):
                reject("invalid_configuration")
            if (
                not math.isfinite(normalized_timeout)
                or not 0 < normalized_timeout <= max_timeout_seconds
                or type(max_nodes) is not int
                or not 0 < max_nodes <= max_nodes_bound
                or type(max_resource_state_bytes) is not int
                or max_resource_state_bytes != expected_max_bytes
            ):
                reject("invalid_configuration")
            if ray.__version__ != supported_ray_version:
                reject("unsupported_ray_version")
            deadline = time.monotonic() + normalized_timeout
            coordinator_runtime = runtime_observation()
            before = snapshot(
                deadline=deadline,
                max_nodes=max_nodes,
                max_bytes=max_resource_state_bytes,
            )
            raw_before_versions = cast(list[list[object]], before["node_state_versions"])
            node_ids = [cast(str, item[0]) for item in raw_before_versions]
            nodes = observe_nodes(
                node_ids=node_ids,
                deadline=deadline,
            )
            after = snapshot(
                deadline=deadline,
                max_nodes=max_nodes,
                max_bytes=max_resource_state_bytes,
            )
            remaining(deadline)
            return {
                "ok": True,
                "coordinator": coordinator_runtime,
                "before": before,
                "after": after,
                "nodes": nodes,
            }
        except _RemoteProbeError as error:
            return {"ok": False, "classification": error.classification}
        except Exception:
            return {"ok": False, "classification": "node_probe_unavailable"}

    return coordinator


def _run_cluster_coordinator(
    ray_module: Any,
    *,
    deadline: float,
    max_nodes: int,
) -> _ClusterIntervalObservation:
    owned_refs: list[object] = []
    try:
        coordinator = ray_module.remote(num_cpus=0, max_retries=0)(
            _make_cluster_probe_coordinator()
        )
        ref = coordinator.remote(
            _remaining_seconds(deadline),
            max_nodes,
            RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES,
        )
        owned_refs.append(ref)
        ready, unfinished = ray_module.wait(
            owned_refs,
            num_returns=1,
            timeout=_remaining_seconds(deadline),
        )
    except RayTargetProbeError:
        _cancel_refs_bounded(ray_module, owned_refs, deadline=deadline)
        raise
    except Exception:
        _cancel_refs_bounded(ray_module, owned_refs, deadline=deadline)
        _reject(RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)
    if unfinished:
        _cancel_refs_bounded(ray_module, owned_refs, deadline=deadline)
        _reject(RayTargetProbeFailure.NODE_PROBE_TIMEOUT)
    try:
        raw_interval = ray_module.get(ready[0], timeout=_remaining_seconds(deadline))
    except RayTargetProbeError:
        _cancel_refs_bounded(ray_module, owned_refs, deadline=deadline)
        raise
    except Exception:
        _reject(RayTargetProbeFailure.NODE_PROBE_UNAVAILABLE)
    return _decode_remote_interval(raw_interval, max_nodes=max_nodes)


def _collect_raw_cluster_observation(
    *,
    timeout_seconds: float = RAY_TARGET_PROBE_DEFAULT_TIMEOUT_SECONDS,
    max_nodes: int = RAY_TARGET_PROBE_DEFAULT_MAX_NODES,
) -> _RawClusterObservation:
    """Collect one exact bounded interval without constructing target policy."""
    timeout_seconds, max_nodes = _validate_probe_limits(
        timeout_seconds=timeout_seconds,
        max_nodes=max_nodes,
    )
    deadline = time.monotonic() + timeout_seconds
    try:
        import ray
    except ImportError:
        _reject(RayTargetProbeFailure.PUBLIC_RUNTIME_UNAVAILABLE)
    if ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION:
        _reject(RayTargetProbeFailure.UNSUPPORTED_RAY_VERSION)
    try:
        initialized = ray.is_initialized()
    except Exception:
        _reject(RayTargetProbeFailure.PUBLIC_RUNTIME_UNAVAILABLE)
    if initialized is not True:
        _reject(RayTargetProbeFailure.RAY_NOT_INITIALIZED)
    caller = _current_caller_observation(ray)
    interval = _run_cluster_coordinator(ray, deadline=deadline, max_nodes=max_nodes)
    if caller.node_id not in interval.before.node_ids:
        _reject(RayTargetProbeFailure.NODE_ID_MISMATCH)
    if caller.session_name != interval.coordinator.session_name:
        _reject(RayTargetProbeFailure.SESSION_MISMATCH)
    if (
        caller.ray_version,
        caller.python_implementation,
        caller.python_version,
    ) != (
        interval.coordinator.ray_version,
        interval.coordinator.python_implementation,
        interval.coordinator.python_version,
    ):
        _reject(RayTargetProbeFailure.RUNTIME_MISMATCH)
    return _RawClusterObservation(caller=caller, interval=interval)


def probe_ray_target(
    expectation: RayTargetExpectation,
    *,
    ttl_seconds: int,
    timeout_seconds: float = RAY_TARGET_PROBE_DEFAULT_TIMEOUT_SECONDS,
    max_nodes: int = RAY_TARGET_PROBE_DEFAULT_MAX_NODES,
) -> RayClusterAttestation:
    """Return a canonical attestation for one exact Ray Core expectation."""
    try:
        from datetime import UTC, datetime, timedelta

        from django_ray.target_attestation import (
            RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS,
            RayNodeStateVersion,
            RayRunnerFamily,
            RayRuntimeVersion,
            RayTargetAttestationEncodeError,
            RayTargetAttestationError,
            RayTargetExpectation,
            build_ray_cluster_attestation,
            build_ray_node_observation,
            build_ray_observation_boundary,
            encode_ray_target_expectation,
        )
    except ImportError:
        _reject(RayTargetProbeFailure.ATTESTATION_BUILD_FAILED)
    if type(expectation) is not RayTargetExpectation:
        _reject(RayTargetProbeFailure.INVALID_EXPECTATION)
    try:
        encode_ray_target_expectation(expectation)
    except (RayTargetAttestationError, RayTargetAttestationEncodeError):
        _reject(RayTargetProbeFailure.INVALID_EXPECTATION)
    if expectation.runner_family is not RayRunnerFamily.RAY_CORE:
        _reject(RayTargetProbeFailure.UNSUPPORTED_RUNNER_FAMILY)
    if (
        type(ttl_seconds) is not int
        or not 0 < ttl_seconds <= RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS
    ):
        _reject(RayTargetProbeFailure.INVALID_CONFIGURATION)

    raw = _collect_raw_cluster_observation(
        timeout_seconds=timeout_seconds,
        max_nodes=max_nodes,
    )
    observed_runtime = RayRuntimeVersion(
        ray_major=2,
        ray_minor=56,
        ray_patch=0,
        python_implementation=raw.caller.python_implementation,
        python_major=raw.caller.python_version[0],
        python_minor=raw.caller.python_version[1],
        python_patch=raw.caller.python_version[2],
    )
    if expectation.cluster_session != raw.caller.session_name:
        _reject(RayTargetProbeFailure.SESSION_MISMATCH)
    if expectation.runtime != observed_runtime:
        _reject(RayTargetProbeFailure.RUNTIME_MISMATCH)
    try:
        nodes = tuple(
            build_ray_node_observation(
                node_id=node.node_id,
                cluster_session=node.session_name,
                runtime=observed_runtime,
            )
            for node in raw.interval.nodes
        )
        boundary = build_ray_observation_boundary(
            resource_state_version_before=(raw.interval.before.cluster_resource_state_version),
            resource_state_version_after=raw.interval.after.cluster_resource_state_version,
            node_state_versions_before=tuple(
                RayNodeStateVersion(node_id=node_id, node_state_version=node_version)
                for node_id, node_version in raw.interval.before.node_state_versions
            ),
            node_state_versions_after=tuple(
                RayNodeStateVersion(node_id=node_id, node_state_version=node_version)
                for node_id, node_version in raw.interval.after.node_state_versions
            ),
        )
        observed_at = datetime.now(UTC)
        return build_ray_cluster_attestation(
            expectation=expectation,
            boundary=boundary,
            nodes=nodes,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(seconds=ttl_seconds),
        )
    except (RayTargetAttestationError, RayTargetAttestationEncodeError, OverflowError):
        _reject(RayTargetProbeFailure.ATTESTATION_BUILD_FAILED)


__all__ = [
    "RAY_TARGET_PROBE_DEFAULT_MAX_NODES",
    "RAY_TARGET_PROBE_DEFAULT_TIMEOUT_SECONDS",
    "RAY_TARGET_PROBE_MAX_NODES",
    "RAY_TARGET_PROBE_MAX_RESOURCE_STATE_BYTES",
    "RAY_TARGET_PROBE_MAX_TIMEOUT_SECONDS",
    "RAY_TARGET_PROBE_RAY_VERSION",
    "RayTargetProbeError",
    "RayTargetProbeFailure",
    "probe_ray_target",
]
