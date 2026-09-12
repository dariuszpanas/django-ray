"""Explicit p3 invocation boundaries; no native Ray or database resources."""

import builtins
import json
import platform
import sys
from dataclasses import replace

import pytest

from django_ray import __version__
from django_ray.execution_codec import (
    ExecutionCompletion,
    _encode_execution_completion_for_protocols,
)
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.runtime import cohort_execution as execution
from django_ray.target.cohort_claim import CohortPythonVersion
from django_ray.target.cohort_contract import (
    CohortContractError,
    CohortRuntimeMismatch,
    cohort_execution_contract_digest,
    encode_cohort_execution_contract,
)
from django_ray.target.cohort_runtime import CohortRuntimeGuardError, CohortRuntimeGuardReason
from django_ray.target.cohort_sync import (
    CohortSyncContract,
    cohort_sync_contract_digest,
    decode_cohort_sync_contract,
    encode_cohort_sync_contract,
    verify_cohort_sync_runtime,
)
from django_ray.target.cohort_transport import (
    CohortExecutionResult,
    PreparedCohortExecution,
    cohort_execution_request_digest,
    decode_cohort_execution_result,
    encode_cohort_execution_request,
    encode_cohort_execution_result,
)
from tests.unit.test_cohort_runtime import _contract
from tests.unit.test_execution_codec import _inline_request


def sync_contract():
    original = _contract()
    return CohortSyncContract(
        original.identity,
        __version__,
        original.identity.task_execution_pk,
        3,
        "sha256:" + "a" * 64,
        original.claimed_at,
        CohortPythonVersion(
            platform.python_implementation().lower(),
            sys.version_info.major,
            sys.version_info.minor,
            sys.version_info.micro,
        ),
    )


def prepared(contract=None):
    contract = contract or _contract()
    sync = type(contract) is CohortSyncContract
    request = replace(
        _inline_request(contract.identity),
        execution_protocol_version=3,
        compiled_graph_submission_transport=None if sync else "direct-ray-core",
        cohort_contract_json=(
            encode_cohort_sync_contract(contract)
            if sync
            else encode_cohort_execution_contract(contract)
        ),
    )
    return PreparedCohortExecution(
        contract.identity,
        encode_cohort_execution_request(request),
        cohort_execution_request_digest(request),
        cohort_sync_contract_digest(contract)
        if sync
        else cohort_execution_contract_digest(contract),
    )


def bindings(value):
    return {
        "expected_identity": value.identity,
        "expected_request_digest": value.request_digest,
        "expected_cohort_contract_digest": value.contract_digest,
    }


def completed(value):
    return _encode_execution_completion_for_protocols(
        ExecutionCompletion(value.identity, 3, __version__, True, 42, None, None, None, None, None),
        ExecutionProtocolRange(3, 3),
    )


def test_sync_contract_checks_actual_python_without_ray_or_django(monkeypatch):
    contract = sync_contract()
    original = builtins.__import__

    def poison(name, *args, **kwargs):
        if name == "ray" or name.startswith(("ray.", "django.", "testproject.")):
            pytest.fail("Sync guard imported application or Ray")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", poison)
    encoded = encode_cohort_sync_contract(contract)
    assert (
        decode_cohort_sync_contract(
            encoded,
            expected_identity=contract.identity,
            expected_contract_digest=cohort_sync_contract_digest(contract),
        )
        == contract
    )
    assert verify_cohort_sync_runtime(contract) is None
    assert (
        verify_cohort_sync_runtime(replace(contract, expected_django_ray_version="999.0.0"))
        == "package_mismatch"
    )
    assert (
        verify_cohort_sync_runtime(replace(contract, python=replace(contract.python, patch=999)))
        == "runtime_mismatch"
    )


@pytest.mark.parametrize("change", ["duplicate", "extra", "version", "bool", "binding"])
def test_sync_contract_rejects_ambiguous_or_crossed_wire(change):
    contract = sync_contract()
    encoded = encode_cohort_sync_contract(contract)
    value = json.loads(encoded)
    if change == "duplicate":
        encoded = '{"schema_version":1,' + encoded[1:]
    else:
        if change == "extra":
            value["private_extra"] = True
        elif change == "version":
            value["execution_protocol_version"] = 1
        elif change == "bool":
            value["cohort_evidence_id"] = True
        else:
            value["target_binding_id"] += 1
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with pytest.raises(CohortContractError):
        decode_cohort_sync_contract(encoded)


@pytest.mark.parametrize(
    "reason,refusal,not_invoked",
    [
        (CohortRuntimeMismatch.PACKAGE_VERSION_MISMATCH, "package_mismatch", True),
        (CohortRuntimeMismatch.PYTHON_VERSION_MISMATCH, "runtime_mismatch", True),
        (CohortRuntimeMismatch.CLUSTER_SESSION_MISMATCH, "session_mismatch", True),
        (CohortRuntimeGuardReason.MEMBERSHIP_CHANGED, "membership_mismatch", True),
        (CohortRuntimeGuardReason.OBSERVATION_UNAVAILABLE, "observation_unavailable", False),
    ],
)
def test_outer_refusal_precedes_any_application_import(monkeypatch, reason, refusal, not_invoked):
    value = prepared()

    def guard(_contract):
        raise CohortRuntimeGuardError(reason)

    monkeypatch.setattr(execution, "verify_cohort_runtime", guard)
    original = builtins.__import__

    def poison(name, *args, **kwargs):
        if name == "django_ray.runtime.entrypoint" or name.startswith(("django.", "testproject.")):
            pytest.fail("Rejected outer crossed the application boundary")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", poison)
    result = decode_cohort_execution_result(
        execution.execute_cohort_request(value.request_json, **bindings(value)), **bindings(value)
    )
    assert result.refusal == refusal
    assert result.application_invoked is (False if not_invoked else None)
    assert result.boundary == "outer" and result.completion_json is None


@pytest.mark.parametrize(
    "digest_field", ["expected_request_digest", "expected_cohort_contract_digest"]
)
def test_crossed_request_never_reaches_runtime_guard(monkeypatch, digest_field):
    value = prepared()
    monkeypatch.setattr(
        execution,
        "verify_cohort_runtime",
        lambda _: pytest.fail("Crossed request observed runtime"),
    )
    with pytest.raises(ValueError):
        execution.execute_cohort_request(
            value.request_json, **(bindings(value) | {digest_field: "sha256:" + "f" * 64})
        )


def test_success_is_bound_and_persisted_only_as_full_jobs_result(monkeypatch):
    from django_ray.runtime import entrypoint

    value = prepared()
    calls = []
    monkeypatch.setattr(execution, "verify_cohort_runtime", lambda _: calls.append("guard"))

    def invoke(*_args, **kwargs):
        assert calls == ["guard"]
        assert kwargs["_persist_completion"] is False
        assert kwargs["_cohort_contract_digest"] == value.contract_digest
        calls.append("application")
        return completed(value)

    monkeypatch.setattr(entrypoint, "execute_task", invoke)
    monkeypatch.setattr(entrypoint, "_persist_task_completion", lambda *args: calls.append(args))
    encoded = execution.execute_cohort_request(
        value.request_json, **bindings(value), ray_job_driver=True
    )
    result = decode_cohort_execution_result(encoded, **bindings(value))
    assert result.completion_json == completed(value)
    assert result.application_invoked is None
    assert calls[2] == (
        value.identity.task_execution_pk,
        value.identity.attempt_number,
        value.identity.execution_generation,
        encoded,
    )


def test_nested_refusal_never_claims_outer_was_not_invoked(monkeypatch):
    from django_ray.runtime import entrypoint

    value = prepared()
    monkeypatch.setattr(execution, "verify_cohort_runtime", lambda _: None)

    def invoke(*_args, **_kwargs):
        try:
            raise CohortRuntimeGuardError(CohortRuntimeMismatch.CLUSTER_SESSION_MISMATCH)
        except CohortRuntimeGuardError as error:
            raise RuntimeError("Ray wrapper") from error

    monkeypatch.setattr(entrypoint, "execute_task", invoke)
    result = decode_cohort_execution_result(
        execution.execute_cohort_request(value.request_json, **bindings(value)), **bindings(value)
    )
    assert result.refusal == "nested_refusal" and result.boundary == "nested"
    assert result.application_invoked is None


def test_guarded_application_uses_explicit_p3_completion_codec_without_legacy_fallback(monkeypatch):
    from django_ray.execution_codec import decode_execution_completion
    from django_ray.runtime import entrypoint, import_utils

    value = prepared()
    events = []
    monkeypatch.setattr(execution, "verify_cohort_runtime", lambda _: events.append("guard"))
    monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: events.append("bootstrap"))

    def resolve(_path):
        assert events == ["guard", "bootstrap"]
        return lambda a, b, scale=1: (a + b) * scale

    monkeypatch.setattr(import_utils, "import_callable", resolve)
    monkeypatch.setattr(
        entrypoint,
        "_persist_task_completion",
        lambda *_args: pytest.fail("Core wrote a Jobs result"),
    )
    result = decode_cohort_execution_result(
        execution.execute_cohort_request(value.request_json, **bindings(value)), **bindings(value)
    )
    completion = decode_execution_completion(
        result.completion_json,
        expected_identity=value.identity,
        expected_execution_protocol_version=3,
        supported_protocols=ExecutionProtocolRange(3, 3),
    ).completion
    assert completion.success and completion.result == 42
    assert (
        completion.execution_protocol_version == 3
        and completion.executor_django_ray_version == __version__
    )


@pytest.mark.parametrize("change", ["both", "neither", "nested_false", "unknown_false", "crossed"])
def test_result_codec_rejects_false_authority_or_crossed_completion(change):
    value = prepared()
    result = CohortExecutionResult(
        value.identity,
        value.request_digest,
        value.contract_digest,
        completion_json=completed(value),
    )
    if change == "both":
        result = replace(result, refusal="package_mismatch")
    elif change == "neither":
        result = replace(result, completion_json=None)
    elif change == "nested_false":
        result = replace(
            result,
            completion_json=None,
            refusal="nested_refusal",
            boundary="nested",
            application_invoked=False,
        )
    elif change == "unknown_false":
        result = replace(
            result,
            completion_json=None,
            refusal="observation_unavailable",
            application_invoked=False,
        )
    else:
        result = replace(result, identity=replace(value.identity, execution_generation=99))
    with pytest.raises(ValueError):
        encode_cohort_execution_result(result)


def test_core_result_poll_requires_exact_owned_reference_and_does_not_retire(monkeypatch):
    from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner
    from tests.unit.test_ray_core_runner import _FakeObjectRef, _install_fake_ray

    fake = _install_fake_ray(monkeypatch)
    value = prepared()
    runner = RayCoreRunner()
    ref = _FakeObjectRef("a" * 56)
    handle = RayCoreHandle(
        1,
        ref,
        _contract().claimed_at,
        "task",
        cohort_prepared=value,
        attempt_number=value.identity.attempt_number,
        execution_generation=value.identity.execution_generation,
    )
    runner._pending_tasks[1] = handle
    fake.values[ref] = encode_cohort_execution_result(
        CohortExecutionResult(
            value.identity,
            value.request_digest,
            value.contract_digest,
            refusal="session_mismatch",
            application_invoked=False,
        )
    )
    fake.ready_refs.add(ref)
    assert runner.poll_cohort_completed([replace(handle)]) == []
    result = runner.poll_cohort_completed([handle])[0]
    assert result.handle is handle and result.result.refusal == "session_mismatch"
    assert runner._pending_tasks[1] is handle
    fake.values[ref] = '{"success":true}'
    assert runner.poll_cohort_completed([handle])[0].uncertainty == "transport_uncertain"
    assert runner._pending_tasks[1] is handle


def test_core_submit_retains_exact_prepared_request_and_never_reconnects(monkeypatch):
    from django_ray.runner import ray_core
    from django_ray.target.cohort_transport import prepare_cohort_execution
    from tests.unit.test_ray_core_runner import _FakeObjectRef, _install_fake_ray, _task_execution

    fake = _install_fake_ray(monkeypatch)
    claim = _contract()
    task = _task_execution(
        claim.identity.task_execution_pk,
        task_id=claim.identity.task_id,
        attempt_number=claim.identity.attempt_number,
        execution_generation=claim.identity.execution_generation,
        execution_protocol_version=3,
        callable_path="testproject.tasks.add_numbers",
    )
    value = prepare_cohort_execution(task, contract=claim, transport="direct-ray-core")
    calls = []

    class Remote:
        def options(self, **_kwargs):
            return self

        def remote(self, serialized, **kwargs):
            calls.append((serialized, kwargs))
            return _FakeObjectRef("a" * 56)

    monkeypatch.setattr(ray_core, "_get_remote_execute_cohort_task", lambda: Remote())
    runner = ray_core.RayCoreRunner()
    handle = runner.submit_cohort_task(task, prepared=value)
    assert isinstance(handle, ray_core.RayCoreHandle)
    assert handle.cohort_prepared is value
    assert runner._pending_tasks[task.pk] is handle and fake.init_calls == []
    assert calls[0][0] == value.request_json
    assert calls[0][1]["expected_request_digest"] == value.request_digest
    assert calls[0][1]["expected_cohort_contract_digest"] == value.contract_digest
    fake.initialized = False
    with pytest.raises(ValueError, match="connection unavailable"):
        runner.submit_cohort_task(task, prepared=value)
    assert len(calls) == 1 and fake.init_calls == []


def test_distributed_leaves_guard_and_retain_claim_for_deeper_nested_dispatch(monkeypatch):
    from django_ray.runtime import cohort_nested, distributed
    from django_ray.runtime.context import durable_task_execution
    from tests.unit.test_distributed_mocked import (
        _install_fake_ray,
        _nested_map,
        _runtime_env_identity,
    )

    fake = _install_fake_ray(monkeypatch)
    monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
    guards = []
    monkeypatch.setattr(cohort_nested, "verify_cohort_runtime", lambda claim: guards.append(claim))
    monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)
    claim = _contract()
    with durable_task_execution(
        claim.identity.task_execution_pk,
        task_id=claim.identity.task_id,
        attempt_number=claim.identity.attempt_number,
        execution_generation=claim.identity.execution_generation,
        execution_protocol_version=3,
        runtime_env_plan_identity=_runtime_env_identity(),
        strict_execution_request=True,
        cohort_contract_json=encode_cohort_execution_contract(claim),
        cohort_contract_digest=cohort_execution_contract_digest(claim),
    ):
        assert distributed.parallel_map(_nested_map, [5]) == [10]
    assert len(guards) == 2 and len(fake.remote_invocations) == 2
    assert guards[0] == guards[1]
    assert guards[0].outer_contract_digest == cohort_execution_contract_digest(claim)


def test_distributed_point_refusal_precedes_bootstrap_and_callable_unpickle(monkeypatch):
    import pickle

    from django_ray.runtime import cohort_nested, distributed
    from django_ray.runtime.context import durable_task_execution
    from tests.unit.test_distributed_mocked import _install_fake_ray, _mul, _runtime_env_identity

    _install_fake_ray(monkeypatch)
    monkeypatch.setattr(distributed, "is_ray_available", lambda: True)

    def refuse(_claim):
        raise CohortRuntimeGuardError(CohortRuntimeMismatch.CLUSTER_SESSION_MISMATCH)

    monkeypatch.setattr(cohort_nested, "verify_cohort_runtime", refuse)
    monkeypatch.setattr(
        distributed,
        "_bootstrap_django_if_needed",
        lambda: pytest.fail("Refused leaf bootstrapped Django"),
    )
    monkeypatch.setattr(pickle, "loads", lambda *_args: pytest.fail("Refused leaf loaded callable"))
    claim = _contract()
    with durable_task_execution(
        claim.identity.task_execution_pk,
        task_id=claim.identity.task_id,
        attempt_number=claim.identity.attempt_number,
        execution_generation=claim.identity.execution_generation,
        execution_protocol_version=3,
        runtime_env_plan_identity=_runtime_env_identity(),
        strict_execution_request=True,
        cohort_contract_json=encode_cohort_execution_contract(claim),
        cohort_contract_digest=cohort_execution_contract_digest(claim),
    ):
        with pytest.raises(CohortRuntimeGuardError):
            distributed.parallel_map(_mul, [5])


def workflow_controls(kind):
    from types import SimpleNamespace

    from django_ray.runtime.context import DurableTaskContext
    from django_ray.workflows import _RayExecutor
    from tests.unit.test_distributed_mocked import _runtime_env_identity

    claim = _contract()
    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=claim.identity.task_execution_pk,
        task_id=claim.identity.task_id,
        attempt_number=claim.identity.attempt_number,
        execution_generation=claim.identity.execution_generation,
        execution_protocol_version=3,
        strict_execution_request=True,
        runtime_env_plan_identity=_runtime_env_identity(),
        cohort_contract_json=encode_cohort_execution_contract(claim),
        cohort_contract_digest=cohort_execution_contract_digest(claim),
    )
    executor.workflow_run_identity = SimpleNamespace(run_id="00000000-0000-4000-8000-000000000611")
    return executor._strict_nested_request_kwargs(
        boundary_kind=kind,
        node_id="0.reducer",
        callable_path="tests.unit.test_result_fold.sum_items",
        binding=None,
    )


@pytest.mark.parametrize("kind", ["workflow_step", "result_fold"])
def test_workflow_and_fold_transports_carry_independent_leaf_claim(kind, monkeypatch):
    from django_ray.execution_codec import NestedExecutionBoundaryKind
    from django_ray.runtime import cohort_nested, remote, result_fold

    controls = workflow_controls(NestedExecutionBoundaryKind(kind))
    serialized = controls.pop("nested_execution_request")
    guards = []
    monkeypatch.setattr(cohort_nested, "verify_cohort_runtime", lambda claim: guards.append(claim))
    if kind == "workflow_step":
        request = remote._decode_workflow_step_request(
            serialized,
            callable_path="tests.unit.test_result_fold.sum_items",
            output_preview_path=None,
            **controls,
        )
    else:
        request = result_fold._decode_result_fold_request(
            serialized, callable_path="tests.unit.test_result_fold.sum_items", **controls
        )
    assert len(guards) == 1 and request.execution_protocol_version == 3
    assert guards[0].outer_contract_digest == controls["expected_outer_contract_digest"]
    controls["expected_cohort_leaf_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError):
        if kind == "workflow_step":
            remote._decode_workflow_step_request(
                serialized,
                callable_path="tests.unit.test_result_fold.sum_items",
                output_preview_path=None,
                **controls,
            )
        else:
            result_fold._decode_result_fold_request(
                serialized, callable_path="tests.unit.test_result_fold.sum_items", **controls
            )
    assert len(guards) == 1


def test_retained_result_fold_rechecks_current_cohort_before_reducer_unpickle(monkeypatch):
    import ray.cloudpickle

    from django_ray.execution_codec import NestedExecutionBoundaryKind
    from django_ray.runtime import cohort_nested, result_fold
    from django_ray.target import cohort_runtime

    controls = workflow_controls(NestedExecutionBoundaryKind.RESULT_FOLD)
    monkeypatch.setattr(cohort_nested, "verify_cohort_runtime", lambda _: None)
    monkeypatch.setattr(cohort_runtime, "verify_cohort_runtime", lambda _: None)
    actor = result_fold.WorkflowMapResultFold(
        2, 1, 4096, "tests.unit.test_result_fold.sum_items", False, (), {}, 0, **controls
    )

    def refuse(_claim):
        raise CohortRuntimeGuardError(CohortRuntimeGuardReason.MEMBERSHIP_CHANGED)

    monkeypatch.setattr(cohort_runtime, "verify_cohort_runtime", refuse)
    monkeypatch.setattr(
        ray.cloudpickle, "loads", lambda *_args: pytest.fail("Stale fold unpickled input")
    )
    with pytest.raises(CohortRuntimeGuardError):
        actor.append(0, 5)
    assert actor.folded_items == 0
