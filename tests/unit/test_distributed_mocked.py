"""Mocked unit tests for distributed runtime utilities."""

from __future__ import annotations

import builtins
import json
import pickle
import sys
from typing import Any

import django
import pytest

from django_ray.execution_codec import (
    ExecutionIdentity,
    NestedCallableBindingKind,
    NestedDistributedBoundaryIdentity,
    NestedExecutionBoundaryKind,
    NestedExecutionRequestRejected,
    NestedExecutionRequestRejection,
    decode_nested_execution_request,
    nested_callable_digest,
)
from django_ray.runtime import distributed
from django_ray.runtime.context import durable_task_execution, get_current_task_context
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.workflow_plans import runtime_env_plan_identity


def _add(a: int, b: int) -> int:
    return a + b


def _mul(value: int, factor: int = 1) -> int:
    return value * factor


def _context_snapshot(value: int) -> dict[str, Any]:
    context = get_current_task_context()
    assert context is not None
    return {
        "value": value,
        "task_pk": context.task_pk,
        "task_id": context.task_id,
        "attempt_number": context.attempt_number,
        "execution_generation": context.execution_generation,
        "execution_protocol_version": context.execution_protocol_version,
        "runtime_env_profile": context.runtime_env_profile,
        "runtime_env_hash": context.runtime_env_hash,
        "runtime_env_plan_identity": context.runtime_env_plan_identity,
        "compiled_graph_submission_transport": (context.compiled_graph_submission_transport),
        "strict_execution_request": context.strict_execution_request,
    }


def _nested_map(value: int) -> int:
    return distributed.parallel_map(_mul, [value], factor=2)[0]


def _runtime_env_identity() -> dict[str, Any]:
    return runtime_env_plan_identity(normalize_runtime_env({})).as_transport_dict()


def _strict_execution():
    return durable_task_execution(
        41,
        task_id="task-41",
        execution_protocol_version=1,
        attempt_number=3,
        execution_generation=7,
        runtime_env_plan_identity=_runtime_env_identity(),
        strict_execution_request=True,
    )


def _strict_map_controls(
    pickled_func: bytes,
    *,
    item_index: int = 4,
) -> tuple[Any, ...]:
    with _strict_execution():
        operation = distributed._strict_nested_operation(
            NestedExecutionBoundaryKind.DISTRIBUTED_MAP
        )
        assert operation is not None
        return distributed._nested_distributed_request(operation, pickled_func, item_index)


class _DistRef:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeRay:
    def __init__(self) -> None:
        self.remote_options: list[dict[str, Any]] = []
        self.remote_invocations: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.resources: dict[str, float] = {"CPU": 8.0}

    def remote(self, *args: Any, **options: Any):
        self.remote_options.append(options)

        def _decorator(fn):
            class _RemoteCallable:
                def options(self, **options: Any):
                    fake_options = _RemoteCallable()
                    fake_options._options = options
                    return fake_options

                @staticmethod
                def remote(*args: Any, **kwargs: Any) -> _DistRef:
                    self.remote_invocations.append((fn.__name__, args, kwargs))
                    return _DistRef(fn(*args, **kwargs))

            return _RemoteCallable()

        return _decorator(args[0]) if args else _decorator

    def get(self, refs: Any) -> Any:
        if isinstance(refs, list):
            return [ref.value for ref in refs]
        return refs.value

    def wait(self, refs: list[_DistRef], num_returns: int = 1):
        return refs[:num_returns], refs[num_returns:]

    def cluster_resources(self) -> dict[str, float]:
        return self.resources


def _install_fake_ray(monkeypatch) -> _FakeRay:
    fake = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake)
    return fake


class TestDistributedMocked:
    """Coverage for distributed runtime branches with mocked Ray."""

    def test_bootstrap_calls_django_setup_when_needed(self, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(distributed, "_django_bootstrapped", False)
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
        monkeypatch.setattr("django.apps.apps.ready", False, raising=False)
        monkeypatch.setattr(django, "setup", lambda: calls.append("setup"))

        distributed._bootstrap_django_if_needed()

        assert calls == ["setup"]
        assert distributed._django_bootstrapped is True

    def test_bootstrap_marks_done_even_without_settings_module(self, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(distributed, "_django_bootstrapped", False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.setattr("django.apps.apps.ready", False, raising=False)
        monkeypatch.setattr(django, "setup", lambda: calls.append("setup"))

        distributed._bootstrap_django_if_needed()

        assert calls == []
        assert distributed._django_bootstrapped is True

    def test_bootstrap_returns_when_already_complete(self, monkeypatch) -> None:
        monkeypatch.setattr(distributed, "_django_bootstrapped", True)

        distributed._bootstrap_django_if_needed()

        assert distributed._django_bootstrapped is True

    def test_is_ray_available_handles_import_error(self, monkeypatch) -> None:
        original_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):  # noqa: ANN001
            if name == "ray":
                raise ImportError("ray missing")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        monkeypatch.delitem(sys.modules, "ray", raising=False)

        assert distributed.is_ray_available() is False

    def test_parallel_map_uses_ray_batch_mode(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        bootstrap_calls: list[str] = []
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(
            distributed, "_bootstrap_django_if_needed", lambda: bootstrap_calls.append("boot")
        )

        results = distributed.parallel_map(_mul, [1, 2, 3], factor=10, max_concurrency=2)

        assert results == [10, 20, 30]
        assert len(bootstrap_calls) == 3

    def test_parallel_map_uses_single_ray_batch_when_unbounded(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)

        assert distributed.parallel_map(_mul, [1, 2], factor=3) == [3, 6]
        assert distributed.parallel_map(_mul, [3, 4], factor=3) == [9, 12]
        assert len(fake.remote_options) == 1

    def test_strict_helpers_bind_exact_nested_requests(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)

        with _strict_execution():
            assert distributed.parallel_map(_mul, [1, 2], factor=3) == [3, 6]
            assert distributed.parallel_starmap(_add, [(1, 2), (3, 4)]) == [3, 7]
            assert distributed.scatter_gather(
                [(_add, (1, 2), {}), (_mul, (3,), {"factor": 4})]
            ) == [3, 12]

        expected_kinds = [
            NestedExecutionBoundaryKind.DISTRIBUTED_MAP,
            NestedExecutionBoundaryKind.DISTRIBUTED_MAP,
            NestedExecutionBoundaryKind.DISTRIBUTED_STARMAP,
            NestedExecutionBoundaryKind.DISTRIBUTED_STARMAP,
            NestedExecutionBoundaryKind.DISTRIBUTED_SCATTER,
            NestedExecutionBoundaryKind.DISTRIBUTED_SCATTER,
        ]
        requests = []
        for expected_kind, (_, args, kwargs) in zip(
            expected_kinds, fake.remote_invocations, strict=True
        ):
            assert kwargs == {}
            (
                serialized,
                task_execution_pk,
                task_id,
                attempt_number,
                execution_generation,
                execution_protocol_version,
                operation_id,
                item_index,
                runtime_env_plan_digest,
                runtime_env_transport_digest,
            ) = args[-10:]
            assert isinstance(serialized, str)
            request = decode_nested_execution_request(
                serialized,
                expected_outer_identity=ExecutionIdentity(
                    task_execution_pk=task_execution_pk,
                    task_id=task_id,
                    attempt_number=attempt_number,
                    execution_generation=execution_generation,
                ),
                expected_execution_protocol_version=execution_protocol_version,
                expected_boundary_kind=expected_kind,
                expected_boundary_identity=NestedDistributedBoundaryIdentity(
                    operation_id=operation_id,
                    item_index=item_index,
                ),
                expected_callable_binding_kind=NestedCallableBindingKind.DIGEST,
                expected_callable_binding=nested_callable_digest(args[0]),
                expected_runtime_env_plan_digest=runtime_env_plan_digest,
                expected_runtime_env_transport_digest=runtime_env_transport_digest,
            )
            requests.append(request)
            wire = json.loads(serialized)
            assert "ray_version" not in wire
            assert "python_version" not in wire
            assert "cluster_id" not in wire
            assert wire["task_execution_pk"] == 41
            assert wire["task_id"] == "task-41"
            assert wire["attempt_number"] == 3
            assert wire["execution_generation"] == 7
            assert wire["execution_protocol_version"] == 1
            assert wire["callable_binding_kind"] == "digest"
            assert request.runtime_env_plan_identity == _runtime_env_identity()

        assert [request.boundary_identity.item_index for request in requests] == [0, 1] * 3
        operation_ids = [request.boundary_identity.operation_id for request in requests]
        assert operation_ids[0] == operation_ids[1]
        assert operation_ids[2] == operation_ids[3]
        assert operation_ids[4] == operation_ids[5]
        assert len({operation_ids[0], operation_ids[2], operation_ids[4]}) == 3

    def test_strict_leaf_installs_context_for_deeper_nesting(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)
        expected_runtime_env = _runtime_env_identity()

        with _strict_execution():
            snapshots = distributed.parallel_map(_context_snapshot, [9])
            nested_results = distributed.parallel_map(_nested_map, [5])

        assert snapshots == [
            {
                "value": 9,
                "task_pk": 41,
                "task_id": "task-41",
                "attempt_number": 3,
                "execution_generation": 7,
                "execution_protocol_version": 1,
                "runtime_env_profile": None,
                "runtime_env_hash": "",
                "runtime_env_plan_identity": expected_runtime_env,
                "compiled_graph_submission_transport": "direct-ray-core",
                "strict_execution_request": True,
            }
        ]
        assert nested_results == [10]
        assert len(fake.remote_invocations) == 3
        outer_nested_request = decode_nested_execution_request(fake.remote_invocations[1][1][-10])
        deeper_request = decode_nested_execution_request(fake.remote_invocations[2][1][-10])
        assert deeper_request.outer_identity == outer_nested_request.outer_identity
        assert (
            deeper_request.execution_protocol_version
            == outer_nested_request.execution_protocol_version
        )
        assert deeper_request.runtime_env_plan_identity == expected_runtime_env

    def test_released_context_keeps_legacy_distributed_call_shape(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)

        with durable_task_execution(
            41,
            task_id="task-41",
            execution_protocol_version=1,
            attempt_number=3,
            execution_generation=7,
            runtime_env_plan_identity=_runtime_env_identity(),
        ):
            assert distributed.parallel_map(_mul, [2], factor=4) == [8]

        assert len(fake.remote_invocations) == 1
        assert len(fake.remote_invocations[0][1]) == 3

    @pytest.mark.parametrize(
        ("tamper", "classification"),
        [
            ("identity", NestedExecutionRequestRejection.IDENTITY_MISMATCH),
            ("protocol", NestedExecutionRequestRejection.PROTOCOL_MISMATCH),
            ("kind", NestedExecutionRequestRejection.BOUNDARY_MISMATCH),
            ("operation", NestedExecutionRequestRejection.BOUNDARY_MISMATCH),
            ("index", NestedExecutionRequestRejection.BOUNDARY_MISMATCH),
            ("callable", NestedExecutionRequestRejection.CALLABLE_MISMATCH),
            ("runtime_env", NestedExecutionRequestRejection.RUNTIME_ENV_MISMATCH),
        ],
    )
    def test_strict_leaf_rejects_tampering_before_application_work(
        self,
        monkeypatch,
        tamper: str,
        classification: NestedExecutionRequestRejection,
    ) -> None:
        pickled_func = pickle.dumps(_mul)
        controls = _strict_map_controls(pickled_func)
        serialized = controls[0]
        value = json.loads(serialized)
        if tamper == "identity":
            value["task_execution_pk"] += 1
        elif tamper == "protocol":
            value["execution_protocol_version"] += 1
        elif tamper == "kind":
            value["boundary_kind"] = "distributed_starmap"
        elif tamper == "operation":
            value["operation_id"] = "must-not-leak"
        elif tamper == "index":
            value["item_index"] += 1
        elif tamper == "callable":
            value["callable_binding"] = "sha256:" + "0" * 64
        elif tamper == "runtime_env":
            value["runtime_env_plan_identity"]["profile"] = "tampered"
        else:  # pragma: no cover - parameter list is fixed above
            raise AssertionError(tamper)
        tampered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

        application_events: list[str] = []
        monkeypatch.setattr(
            distributed,
            "_bootstrap_django_if_needed",
            lambda: application_events.append("bootstrap"),
        )
        monkeypatch.setattr(
            pickle,
            "loads",
            lambda _value: application_events.append("pickle.loads"),
        )

        with pytest.raises(NestedExecutionRequestRejected) as caught:
            distributed._parallel_map_remote(
                pickled_func,
                2,
                {"factor": 3},
                tampered,
                *controls[1:],
            )

        assert caught.value.classification is classification
        assert str(caught.value) == f"nested execution request rejected: {classification.value}"
        assert "must-not-leak" not in str(caught.value)
        assert caught.value.retryable is False
        assert application_events == []

    def test_strict_leaf_rejects_wrong_callable_bytes_before_unpickling(self, monkeypatch) -> None:
        controls = _strict_map_controls(pickle.dumps(_mul))
        serialized = controls[0]
        wrong_pickled_func = pickle.dumps(_add)
        application_events: list[str] = []
        monkeypatch.setattr(
            distributed,
            "_bootstrap_django_if_needed",
            lambda: application_events.append("bootstrap"),
        )
        monkeypatch.setattr(
            pickle,
            "loads",
            lambda _value: application_events.append("pickle.loads"),
        )

        with pytest.raises(NestedExecutionRequestRejected) as caught:
            distributed._parallel_map_remote(
                wrong_pickled_func,
                2,
                {"factor": 3},
                serialized,
                *controls[1:],
            )

        assert caught.value.classification is NestedExecutionRequestRejection.CALLABLE_MISMATCH
        assert application_events == []

    def test_strict_leaf_rejects_boolean_expected_index_before_unpickling(
        self, monkeypatch
    ) -> None:
        pickled_func = pickle.dumps(_mul)
        controls = list(_strict_map_controls(pickled_func, item_index=1))
        controls[7] = True
        application_events: list[str] = []
        monkeypatch.setattr(
            distributed,
            "_bootstrap_django_if_needed",
            lambda: application_events.append("bootstrap"),
        )
        monkeypatch.setattr(
            pickle,
            "loads",
            lambda _value: application_events.append("pickle.loads"),
        )

        with pytest.raises(NestedExecutionRequestRejected) as caught:
            distributed._parallel_map_remote(
                pickled_func,
                2,
                {"factor": 3},
                *controls,
            )

        assert caught.value.classification is NestedExecutionRequestRejection.BOUNDARY_MISMATCH
        assert application_events == []

    @pytest.mark.parametrize(
        ("case", "classification"),
        [
            ("request_only", NestedExecutionRequestRejection.INVALID_VERSIONED),
            ("expectations_only", NestedExecutionRequestRejection.INVALID_VERSIONED),
            ("one_missing", NestedExecutionRequestRejection.INVALID_VERSIONED),
            ("marker_free", NestedExecutionRequestRejection.LEGACY_REQUEST),
        ],
    )
    def test_partial_strict_controls_never_fall_back_to_legacy(
        self,
        monkeypatch,
        case: str,
        classification: NestedExecutionRequestRejection,
    ) -> None:
        pickled_func = pickle.dumps(_mul)
        controls = _strict_map_controls(pickled_func)
        if case == "request_only":
            submitted_controls = controls[:1]
        elif case == "expectations_only":
            submitted_controls = (None, *controls[1:])
        elif case == "one_missing":
            submitted_controls = controls[:-1]
        else:
            submitted_controls = ("released-direct-call", *controls[1:])
        application_events: list[str] = []
        monkeypatch.setattr(
            distributed,
            "_bootstrap_django_if_needed",
            lambda: application_events.append("bootstrap"),
        )
        monkeypatch.setattr(
            pickle,
            "loads",
            lambda _value: application_events.append("pickle.loads"),
        )

        with pytest.raises(NestedExecutionRequestRejected) as caught:
            distributed._parallel_map_remote(
                pickled_func,
                2,
                {"factor": 3},
                *submitted_controls,
            )

        assert caught.value.classification is classification
        assert application_events == []

    @pytest.mark.parametrize("strict_marker", [True, 1, 0, None, "false"])
    def test_incomplete_strict_outer_context_never_submits_legacy_work(
        self, monkeypatch, strict_marker: Any
    ) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        with durable_task_execution(
            41,
            runtime_env_plan_identity={},
            strict_execution_request=strict_marker,
        ):
            with pytest.raises(NestedExecutionRequestRejected) as caught:
                distributed.parallel_map(_mul, [2], factor=3)

        assert caught.value.classification is NestedExecutionRequestRejection.MISSING_CONTEXT
        assert fake.remote_invocations == []

    def test_released_direct_leaf_calls_still_bootstrap_unpickle_and_invoke(
        self, monkeypatch
    ) -> None:
        bootstrap_calls: list[str] = []
        monkeypatch.setattr(
            distributed,
            "_bootstrap_django_if_needed",
            lambda: bootstrap_calls.append("bootstrap"),
        )

        assert distributed._parallel_map_remote(pickle.dumps(_mul), 2, {"factor": 3}) == 6
        assert distributed._parallel_starmap_remote(pickle.dumps(_add), (2, 3)) == 5
        assert distributed._scatter_gather_remote(pickle.dumps(_mul), (2,), {"factor": 4}) == 8
        assert bootstrap_calls == ["bootstrap"] * 3

    def test_parallel_starmap_uses_ray_batch_mode(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        bootstrap_calls: list[str] = []
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(
            distributed, "_bootstrap_django_if_needed", lambda: bootstrap_calls.append("boot")
        )

        results = distributed.parallel_starmap(_add, [(1, 2), (3, 4), (5, 6)], max_concurrency=2)

        assert results == [3, 7, 11]
        assert len(bootstrap_calls) == 3

    def test_parallel_starmap_submits_all_items_without_limit(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)

        assert distributed.parallel_starmap(_add, [(1, 2), (3, 4)]) == [3, 7]

    def test_scatter_gather_uses_ray_mode(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        bootstrap_calls: list[str] = []
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
        monkeypatch.setattr(
            distributed, "_bootstrap_django_if_needed", lambda: bootstrap_calls.append("boot")
        )

        tasks = [
            (_add, (1, 2), {}),
            (_mul, (3,), {"factor": 4}),
        ]
        results = distributed.scatter_gather(tasks)

        assert results == [3, 12]
        assert len(bootstrap_calls) == 2

    def test_get_num_workers_and_total_cpus_with_ray_resources(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.resources = {
            "CPU": 16.0,
            "node:10.0.0.1": 1.0,
            "node:10.0.0.2": 1.0,
            "node:__internal_head__": 1.0,
        }
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)

        assert distributed.get_num_workers() == 2
        assert distributed.get_total_cpus() == 16.0

    def test_get_total_cpus_defaults_when_cpu_not_reported(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.resources = {"node:10.0.0.1": 1.0}
        monkeypatch.setattr(distributed, "is_ray_available", lambda: True)

        assert distributed.get_total_cpus() == 1.0

    def test_helpers_reject_invalid_limits_and_resources(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="max_concurrency"):
            distributed.parallel_map(_mul, [1], max_concurrency=0)
        with pytest.raises(ValueError, match="num_cpus"):
            distributed.parallel_map(_mul, [1], num_cpus=-1)
        with pytest.raises(ValueError, match="num_gpus"):
            distributed.parallel_starmap(_add, [(1, 2)], num_gpus=-1)

    def test_helpers_reject_unsupported_shapes(self) -> None:
        import pytest

        with pytest.raises(TypeError, match="items must"):
            distributed.parallel_map(_mul, 1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"items\[0\]"):
            distributed.parallel_starmap(_add, [[1, 2]])  # type: ignore[list-item]
        with pytest.raises(TypeError, match=r"tasks\[0\]"):
            distributed.scatter_gather([(_add, (1, 2))])  # type: ignore[list-item]
