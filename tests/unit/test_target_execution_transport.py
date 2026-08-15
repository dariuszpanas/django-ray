from __future__ import annotations

import builtins
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import django_ray.ray_target_probe as probe_module
from django_ray.ray_target_probe import (
    RayTargetExecutionCompatibilityError,
    RayTargetExecutionResultValidationError,
    RayTargetProbeError,
    RayTargetProbeFailure,
    validate_ray_target_execution_result_semantics,
    verify_ray_target_execution,
)
from django_ray.runner.base import JobStatus
from django_ray.runner.ray_core import (
    RayCoreHandle,
    RayCoreRunner,
    RayCoreTargetExecutionTransportState,
    RayCoreTargetExecutionUncertainty,
    _RayCoreSubmissionHandle,
)
from django_ray.runtime.remote import execute_django_task_remote
from django_ray.target_attestation import (
    build_ray_cluster_attestation,
    ray_cluster_attestation_digest,
)
from django_ray.target_execution_codec import (
    TargetApplicationCompletion,
    TargetExecutionCompatibilityReason,
    TargetExecutionCompatibilityRejection,
    TargetExecutionCompletion,
    TargetExecutionRequestDecodeError,
    build_target_execution_observed_evidence,
    decode_target_execution_result,
    encode_target_execution_request,
    encode_target_execution_result,
)
from django_ray.target_execution_evidence import (
    RayTaskTargetExecutionEvidenceClaim,
    ray_task_target_execution_evidence_digest,
)
from tests.unit.test_target_execution_codec import (
    _NODE_ID,
    _OBSERVED_AT,
    _expected_controls,
    _observed_target,
    _target_completion,
    _target_request,
)

_CLAIMED_AT = _OBSERVED_AT + timedelta(microseconds=1)


def _fresh_request():
    request = _target_request()
    now = datetime.now(UTC)
    attestation = build_ray_cluster_attestation(
        expectation=request.target_expectation,
        boundary=request.claim_attestation.boundary,
        nodes=request.claim_attestation.nodes,
        observed_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
    )
    return replace(
        request,
        target_execution_claimed_at=now,
        claim_attestation=attestation,
        claim_attestation_digest=ray_cluster_attestation_digest(attestation),
    )


def _install_current_target(
    monkeypatch,
    request,
    *,
    node_ids: tuple[str, ...] = (_NODE_ID,),
    python_patch: int | None = None,
) -> None:
    import ray

    runtime = request.target_expectation.runtime
    caller = probe_module._RuntimeObservation(
        node_id=_NODE_ID,
        session_name=request.target_expectation.cluster_session,
        ray_version="2.56.0",
        python_implementation=runtime.python_implementation,
        python_version=(
            runtime.python_major,
            runtime.python_minor,
            runtime.python_patch if python_patch is None else python_patch,
        ),
    )
    snapshot = probe_module._ResourceStateSnapshot(
        session_name=request.target_expectation.cluster_session,
        cluster_resource_state_version=50,
        node_state_versions=tuple(
            (node_id, 70 + index) for index, node_id in enumerate(sorted(node_ids))
        ),
    )
    monkeypatch.setattr(ray, "is_initialized", lambda: True)
    monkeypatch.setattr(ray, "__version__", "2.56.0")
    monkeypatch.setattr(probe_module, "_current_caller_observation", lambda _ray: caller)
    monkeypatch.setattr(
        probe_module,
        "_current_resource_state_snapshot",
        lambda _ray, **_kwargs: snapshot,
    )


def _execute_target_request(serialized: str, request) -> str:
    return execute_django_task_remote(
        serialized,
        expected_task_execution_pk=request.identity.task_execution_pk,
        expected_task_id=request.identity.task_id,
        expected_attempt_number=request.identity.attempt_number,
        expected_execution_generation=request.identity.execution_generation,
        expected_execution_protocol_version=2,
        expected_target_execution_evidence_id=request.target_execution_evidence_id,
        expected_target_execution_evidence_digest=(request.target_execution_evidence_digest),
        expected_target_execution_claimed_at=request.target_execution_claimed_at,
        expected_target_expectation_digest=request.target_expectation_digest,
        expected_claim_attestation_digest=request.claim_attestation_digest,
        _target_execution_transport=True,
    )


def _application_success() -> str:
    return json.dumps(
        {
            "success": True,
            "result": {"answer": 42},
            "result_reference": None,
            "error": None,
            "traceback": None,
            "exception_type": None,
            "retryable": None,
        }
    )


def _observed_for(request, **changes):
    values = {
        "observed_node_id": _NODE_ID,
        "observed_cluster_session": request.target_expectation.cluster_session,
        "observed_runtime": request.target_expectation.runtime,
        "observed_membership_digest": request.claim_attestation.membership_digest,
        "observed_at": _OBSERVED_AT + timedelta(seconds=1),
    }
    values.update(changes)
    return build_target_execution_observed_evidence(
        identity=request.identity,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        **values,
    )


def _compatibility_rejection(request, reason, observed):
    return TargetExecutionCompatibilityRejection(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=observed,
        compatibility_reason=reason,
    )


def test_per_invocation_verifier_accepts_fresh_exact_membership(monkeypatch) -> None:
    request = _fresh_request()
    _install_current_target(monkeypatch, request)

    observed = verify_ray_target_execution(request)

    assert observed.observed_node_id == _NODE_ID
    assert observed.observed_cluster_session == request.target_expectation.cluster_session
    assert observed.observed_runtime == request.target_expectation.runtime
    assert observed.observed_membership_digest == request.claim_attestation.membership_digest
    assert observed.observed_proof_digest.startswith("sha256:")


def test_per_invocation_membership_change_returns_proven_rejection(monkeypatch) -> None:
    request = _fresh_request()
    second_node = "02" * 28
    _install_current_target(monkeypatch, request, node_ids=(_NODE_ID, second_node))

    with pytest.raises(RayTargetExecutionCompatibilityError) as error:
        verify_ray_target_execution(request)

    assert error.value.reason is TargetExecutionCompatibilityReason.MEMBERSHIP_MISMATCH
    assert error.value.observed_target.observed_node_id == _NODE_ID
    assert (
        error.value.observed_target.observed_membership_digest
        != request.claim_attestation.membership_digest
    )
    assert error.value.observed_target.observed_proof_digest.startswith("sha256:")


def test_per_invocation_runtime_mismatch_has_complete_observed_proof(monkeypatch) -> None:
    request = _fresh_request()
    _install_current_target(
        monkeypatch,
        request,
        python_patch=request.target_expectation.runtime.python_patch + 1,
    )

    with pytest.raises(RayTargetExecutionCompatibilityError) as error:
        verify_ray_target_execution(request)

    assert error.value.reason is TargetExecutionCompatibilityReason.PYTHON_VERSION_MISMATCH
    assert error.value.observed_target.observed_runtime.python_patch == (
        request.target_expectation.runtime.python_patch + 1
    )
    assert error.value.observed_target.observed_membership_digest


def test_per_invocation_observation_failure_stays_uncertain(monkeypatch) -> None:
    request = _fresh_request()
    _install_current_target(monkeypatch, request)

    def unavailable(*_args, **_kwargs):
        raise RayTargetProbeError(RayTargetProbeFailure.SNAPSHOT_UNAVAILABLE)

    monkeypatch.setattr(probe_module, "_current_resource_state_snapshot", unavailable)

    with pytest.raises(RayTargetProbeError) as error:
        verify_ray_target_execution(request)

    assert error.value.classification is RayTargetProbeFailure.SNAPSHOT_UNAVAILABLE


def test_per_invocation_preclaim_clock_stays_uncertain_before_application(
    monkeypatch,
) -> None:
    request = _fresh_request()
    request = replace(
        request,
        target_execution_claimed_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    _install_current_target(monkeypatch, request)

    with pytest.raises(RayTargetProbeError) as error:
        verify_ray_target_execution(request)

    assert error.value.classification is RayTargetProbeFailure.ATTESTATION_BUILD_FAILED


@pytest.mark.parametrize(
    ("mismatch", "reason"),
    [
        ("session", TargetExecutionCompatibilityReason.CLUSTER_SESSION_MISMATCH),
        ("ray", TargetExecutionCompatibilityReason.RAY_VERSION_MISMATCH),
        (
            "implementation",
            TargetExecutionCompatibilityReason.PYTHON_IMPLEMENTATION_MISMATCH,
        ),
        ("python", TargetExecutionCompatibilityReason.PYTHON_VERSION_MISMATCH),
        ("node", TargetExecutionCompatibilityReason.CURRENT_NODE_NOT_ATTESTED),
        ("membership", TargetExecutionCompatibilityReason.MEMBERSHIP_MISMATCH),
    ],
)
def test_result_semantics_validate_each_live_observed_mismatch(
    mismatch: str,
    reason: TargetExecutionCompatibilityReason,
) -> None:
    request = _target_request()
    runtime = request.target_expectation.runtime
    changes: dict[str, object] = {}
    if mismatch == "session":
        changes["observed_cluster_session"] = "session_other"
    elif mismatch == "ray":
        changes["observed_runtime"] = replace(runtime, ray_patch=runtime.ray_patch + 1)
    elif mismatch == "implementation":
        changes["observed_runtime"] = replace(runtime, python_implementation="pypy")
    elif mismatch == "python":
        changes["observed_runtime"] = replace(
            runtime,
            python_patch=runtime.python_patch + 1,
        )
    elif mismatch == "node":
        changes["observed_node_id"] = "02" * 28
    else:
        changes["observed_membership_digest"] = "sha256:" + "f" * 64
    rejection = _compatibility_rejection(request, reason, _observed_for(request, **changes))

    validate_ray_target_execution_result_semantics(
        rejection,
        target_expectation=request.target_expectation,
        claim_attestation=request.claim_attestation,
        validation_now=_OBSERVED_AT + timedelta(seconds=2),
    )


@pytest.mark.parametrize(
    ("observed_at", "reason"),
    [
        (
            _OBSERVED_AT + timedelta(minutes=5),
            TargetExecutionCompatibilityReason.EXPIRED,
        ),
    ],
)
def test_result_semantics_validate_attestation_window_rejections(
    observed_at: datetime,
    reason: TargetExecutionCompatibilityReason,
) -> None:
    request = _target_request()
    rejection = _compatibility_rejection(
        request,
        reason,
        _observed_for(request, observed_at=observed_at),
    )

    validate_ray_target_execution_result_semantics(
        rejection,
        target_expectation=request.target_expectation,
        claim_attestation=request.claim_attestation,
        validation_now=observed_at + timedelta(seconds=1),
    )


def test_result_semantics_treat_backwards_remote_time_as_uncertain() -> None:
    request = _target_request()
    rejection = _compatibility_rejection(
        request,
        TargetExecutionCompatibilityReason.EXPIRED,
        _observed_for(request, observed_at=_OBSERVED_AT - timedelta(microseconds=1)),
    )

    with pytest.raises(RayTargetExecutionResultValidationError):
        validate_ray_target_execution_result_semantics(
            rejection,
            target_expectation=request.target_expectation,
            claim_attestation=request.claim_attestation,
            validation_now=_OBSERVED_AT + timedelta(seconds=1),
        )


def test_result_semantics_treat_corrupt_expectation_relation_as_uncertain() -> None:
    request = _target_request()
    attested_expectation = replace(request.target_expectation, target_key="other")
    attestation = build_ray_cluster_attestation(
        expectation=attested_expectation,
        boundary=request.claim_attestation.boundary,
        nodes=request.claim_attestation.nodes,
        observed_at=request.claim_attestation.observed_at,
        expires_at=request.claim_attestation.expires_at,
    )
    corrupt_request = replace(
        request,
        claim_attestation=attestation,
        claim_attestation_digest=ray_cluster_attestation_digest(attestation),
    )
    rejection = _compatibility_rejection(
        corrupt_request,
        TargetExecutionCompatibilityReason.CLUSTER_SESSION_MISMATCH,
        _observed_for(corrupt_request),
    )

    with pytest.raises(RayTargetExecutionResultValidationError):
        validate_ray_target_execution_result_semantics(
            rejection,
            target_expectation=corrupt_request.target_expectation,
            claim_attestation=corrupt_request.claim_attestation,
            validation_now=_OBSERVED_AT + timedelta(seconds=2),
        )


def test_remote_verified_completion_wraps_raw_application_body(monkeypatch) -> None:
    request = _target_request()
    observed = _observed_target(request)
    monkeypatch.setattr(probe_module, "verify_ray_target_execution", lambda _request: observed)
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.execute_task", lambda *_args, **_kw: _application_success()
    )

    serialized_result = _execute_target_request(encode_target_execution_request(request), request)
    result = decode_target_execution_result(serialized_result, **_expected_controls(request))

    assert isinstance(result, TargetExecutionCompletion)
    assert result.application_completion == TargetApplicationCompletion(
        success=True,
        result={"answer": 42},
        result_reference=None,
        error=None,
        traceback=None,
        exception_type=None,
        retryable=None,
    )


def test_remote_proven_rejection_precedes_entrypoint_import(monkeypatch) -> None:
    request = _target_request()
    observed = _observed_target(request)

    def reject(_request):
        raise RayTargetExecutionCompatibilityError(
            TargetExecutionCompatibilityReason.PYTHON_VERSION_MISMATCH,
            observed,
        )

    monkeypatch.setattr(probe_module, "verify_ray_target_execution", reject)
    imported_entrypoint: list[str] = []
    real_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        if name == "django_ray.runtime.entrypoint":
            imported_entrypoint.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)

    serialized_result = _execute_target_request(encode_target_execution_request(request), request)
    result = decode_target_execution_result(serialized_result, **_expected_controls(request))

    assert isinstance(result, TargetExecutionCompatibilityRejection)
    assert result.compatibility_reason is TargetExecutionCompatibilityReason.PYTHON_VERSION_MISMATCH
    assert imported_entrypoint == []


def test_remote_malformed_target_request_raises_instead_of_encoding_failure() -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_request(request))
    wire["task_id"] = "wrong-generation-identity"

    with pytest.raises(TargetExecutionRequestDecodeError):
        _execute_target_request(json.dumps(wire, sort_keys=True, separators=(",", ":")), request)


def _target_handle(request, object_ref: object) -> RayCoreHandle:
    return RayCoreHandle(
        task_pk=request.identity.task_execution_pk,
        object_ref=object_ref,
        submitted_at=datetime.now(UTC),
        task_name="target_task",
        attempt_number=request.identity.attempt_number,
        execution_generation=request.identity.execution_generation,
        strict_request=True,
        durable_task_id=request.identity.task_id,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        target_expectation=request.target_expectation,
        claim_attestation=request.claim_attestation,
    )


def _target_submission_arguments(request) -> dict[str, object]:
    claim = _target_evidence_claim(request)
    return {
        "target_execution_evidence_id": request.target_execution_evidence_id,
        "target_execution_evidence_claim": claim,
        "target_expectation": request.target_expectation,
        "claim_attestation": request.claim_attestation,
        "claim_attestation_recorded_at": request.claim_attestation.observed_at,
    }


def _target_evidence_claim(request, **changes: object) -> RayTaskTargetExecutionEvidenceClaim:
    runtime = request.target_expectation.runtime
    values: dict[str, object] = {
        "execution_id": request.identity.task_execution_pk,
        "task_id": request.identity.task_id,
        "attempt_number": request.identity.attempt_number,
        "execution_generation": request.identity.execution_generation,
        "route_selection_id": request.identity.task_execution_pk,
        "route_backend_alias": "default",
        "route_revision_id": 31,
        "route_revision": 4,
        "selected_target_policy_id": 37,
        "target_id": request.target_expectation.target_key,
        "target_policy_id": 41,
        "claim_attestation_id": 43,
        "target_expectation_digest": request.target_expectation_digest,
        "claim_attestation_digest": request.claim_attestation_digest,
        "worker_target_capability_id": 47,
        "worker_target_capability_schema_version": 1,
        "worker_target_capability_revision": 3,
        "worker_target_capability_advertised_at": (
            request.target_execution_claimed_at - timedelta(seconds=1)
        ),
        "worker_lease_id": "worker-lease",
        "worker_lease_hostname": "worker.example",
        "worker_lease_pid": 1234,
        "worker_lease_started_at": (request.target_execution_claimed_at - timedelta(seconds=2)),
        "runner_family": "ray_core",
        "manager_ray_major": runtime.ray_major,
        "manager_ray_minor": runtime.ray_minor,
        "manager_ray_patch": runtime.ray_patch,
        "manager_python_implementation": runtime.python_implementation,
        "manager_python_major": runtime.python_major,
        "manager_python_minor": runtime.python_minor,
        "manager_python_patch": runtime.python_patch,
        "claimed_at": request.target_execution_claimed_at,
    }
    values.update(changes)
    return RayTaskTargetExecutionEvidenceClaim(**values)  # type: ignore[arg-type]


def _task_execution_for(request, **changes: object):
    values: dict[str, object] = {
        "pk": request.identity.task_execution_pk,
        "task_id": request.identity.task_id,
        "attempt_number": request.identity.attempt_number,
        "execution_generation": request.identity.execution_generation,
        "execution_protocol_version": request.execution_protocol_version,
        "state": "RUNNING",
        "claimed_by_worker": "worker-lease",
        "started_at": request.target_execution_claimed_at - timedelta(seconds=3),
        "finished_at": None,
        "callable_path": request.callable_path,
        "args_json": request.serialized_args,
        "kwargs_json": request.serialized_kwargs,
        "input_reference": request.input_reference,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _target_submission_handle(handle: RayCoreHandle) -> _RayCoreSubmissionHandle:
    return _RayCoreSubmissionHandle(
        ray_job_id="ray-job:target-task",
        ray_address="auto",
        submitted_at=handle.submitted_at,
        pending_handle=handle,
    )


def test_target_submission_requires_canonical_claim() -> None:
    request = _target_request()
    runner = RayCoreRunner.__new__(RayCoreRunner)
    arguments = _target_submission_arguments(request)
    arguments.pop("target_execution_evidence_claim")

    with pytest.raises(TypeError):
        runner._submit_target_execution(  # type: ignore[call-arg]
            _task_execution_for(request),
            **arguments,
        )


def test_target_submission_derives_all_controls_from_canonical_claim(monkeypatch) -> None:
    request = _target_request()
    runner = RayCoreRunner.__new__(RayCoreRunner)
    captured: dict[str, object] = {}
    sentinel = object()

    def capture(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(runner, "_submit_serialized_request", capture)

    result = runner._submit_target_execution(
        _task_execution_for(request),
        **_target_submission_arguments(request),
    )

    evidence = captured["target_execution_evidence"]
    claim = _target_evidence_claim(request)
    assert result is sentinel
    assert evidence.evidence_claim == claim
    assert evidence.evidence_digest == ray_task_target_execution_evidence_digest(claim)
    assert evidence.evidence_claim.claimed_at == request.target_execution_claimed_at


@pytest.mark.parametrize(
    ("claim_changes", "task_changes", "recorded_at"),
    [
        ({"execution_id": 18}, {}, None),
        ({"route_selection_id": 18}, {}, None),
        ({"target_id": "other"}, {}, None),
        ({"target_expectation_digest": "sha256:" + "f" * 64}, {}, None),
        ({"claim_attestation_digest": "sha256:" + "f" * 64}, {}, None),
        ({"manager_python_patch": 99}, {}, None),
        ({}, {"state": "QUEUED"}, None),
        ({}, {"claimed_by_worker": "other-worker"}, None),
        ({}, {"started_at": None}, None),
        ({}, {"started_at": _CLAIMED_AT + timedelta(seconds=1)}, None),
        ({}, {"finished_at": _CLAIMED_AT}, None),
        ({}, {}, _CLAIMED_AT + timedelta(microseconds=1)),
    ],
)
def test_target_submission_rejects_unpersistable_lineage_before_ray(
    monkeypatch,
    claim_changes: dict[str, object],
    task_changes: dict[str, object],
    recorded_at: datetime | None,
) -> None:
    request = _target_request()
    runner = RayCoreRunner.__new__(RayCoreRunner)
    crossed = False

    def unexpected(**_kwargs):
        nonlocal crossed
        crossed = True
        raise AssertionError("invalid claim must not cross into Ray submission")

    monkeypatch.setattr(runner, "_submit_serialized_request", unexpected)
    arguments = _target_submission_arguments(request)
    arguments["target_execution_evidence_claim"] = _target_evidence_claim(
        request,
        **claim_changes,
    )
    if recorded_at is not None:
        arguments["claim_attestation_recorded_at"] = recorded_at

    with pytest.raises(ValueError) as error:
        runner._submit_target_execution(
            _task_execution_for(request, **task_changes),
            **arguments,
        )

    assert str(error.value) == "target execution evidence claim is invalid"
    assert crossed is False


def test_target_submission_rejects_claim_at_attestation_expiry_before_ray(
    monkeypatch,
) -> None:
    request = _target_request()
    runner = RayCoreRunner.__new__(RayCoreRunner)
    crossed = False

    def unexpected(**_kwargs):
        nonlocal crossed
        crossed = True
        raise AssertionError("expired claim must not cross into Ray submission")

    monkeypatch.setattr(runner, "_submit_serialized_request", unexpected)
    expired_claim = _target_evidence_claim(
        request,
        worker_target_capability_advertised_at=(
            request.claim_attestation.expires_at - timedelta(seconds=1)
        ),
        worker_lease_started_at=request.claim_attestation.expires_at - timedelta(seconds=2),
        claimed_at=request.claim_attestation.expires_at,
    )
    arguments = _target_submission_arguments(request)
    arguments["target_execution_evidence_claim"] = expired_claim

    with pytest.raises(ValueError) as error:
        runner._submit_target_execution(
            _task_execution_for(
                request,
                started_at=request.claim_attestation.expires_at - timedelta(seconds=3),
            ),
            **arguments,
        )

    assert str(error.value) == "target execution evidence claim is invalid"
    assert crossed is False


def test_target_poll_does_not_cross_ray_without_claim_time(monkeypatch) -> None:
    import ray

    request = _target_request()
    handle = replace(_target_handle(request, object()), target_execution_claimed_at=None)
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Ray wait must not receive an incomplete p2 capability")

    monkeypatch.setattr(ray, "wait", unexpected)

    assert runner._poll_target_execution_results((handle,)) == []
    assert runner.pending_count == 1


def test_generic_status_does_not_consume_ready_target_result(monkeypatch) -> None:
    import ray

    request = _target_request()
    handle = _target_handle(request, object())
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([handle.object_ref], []))

    def unexpected_get(*_args, **_kwargs):
        raise AssertionError("generic status must not consume a protocol-2 result")

    monkeypatch.setattr(ray, "get", unexpected_get)

    status = runner.get_status(_target_submission_handle(handle))

    assert status.status is JobStatus.UNKNOWN
    assert status.message == "Target-bound result requires authenticated protocol-2 polling"
    assert runner.pending_count == 1


def test_generic_status_reports_unready_target_as_running(monkeypatch) -> None:
    import ray

    request = _target_request()
    handle = _target_handle(request, object())
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([], [handle.object_ref]))

    def unexpected_get(*_args, **_kwargs):
        raise AssertionError("generic status must not consume a protocol-2 result")

    monkeypatch.setattr(ray, "get", unexpected_get)

    status = runner.get_status(_target_submission_handle(handle))

    assert status.status is JobStatus.RUNNING
    assert runner.pending_count == 1


def test_generic_status_retains_target_handle_on_wait_error(monkeypatch) -> None:
    import ray

    request = _target_request()
    handle = _target_handle(request, object())
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}

    def disconnected(*_args, **_kwargs):
        raise RuntimeError("poison connection detail")

    def unexpected_get(*_args, **_kwargs):
        raise AssertionError("generic status must not consume a protocol-2 result")

    monkeypatch.setattr(ray, "wait", disconnected)
    monkeypatch.setattr(ray, "get", unexpected_get)

    status = runner.get_status(_target_submission_handle(handle))

    assert status.status is JobStatus.UNKNOWN
    assert status.message == "Target-bound Ray Core execution status is uncertain"
    assert "poison" not in status.message
    assert runner.pending_count == 1


@pytest.mark.parametrize("ray_value", [RuntimeError("transport"), "{}"])
def test_runner_maps_transport_and_malformed_results_to_uncertain(
    monkeypatch, ray_value: object
) -> None:
    import ray

    request = _target_request()
    object_ref = object()
    handle = _target_handle(request, object_ref)
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([object_ref], []))

    def get(_ref):
        if isinstance(ray_value, Exception):
            raise ray_value
        return ray_value

    monkeypatch.setattr(ray, "get", get)

    [result] = runner._poll_target_execution_results(
        (handle,),
        validation_now=_OBSERVED_AT + timedelta(seconds=2),
    )

    assert result.transport_state is RayCoreTargetExecutionTransportState.UNCERTAIN
    assert result.result is None
    assert result.uncertainty in {
        RayCoreTargetExecutionUncertainty.RAY_TRANSPORT_ERROR,
        RayCoreTargetExecutionUncertainty.INVALID_RESULT,
    }
    assert runner.pending_count == 1


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("runtime", "ray_major", 0),
        ("runtime", "python_major", 0),
        ("runtime", "python_implementation", "CPython\n"),
        ("identity", "attempt_number", 1 << 31),
        ("identity", "execution_generation", 0),
        ("identity", "target_execution_claimed_at", "invalid"),
    ],
)
def test_runner_keeps_noncanonical_runtime_or_identity_result_uncertain(
    monkeypatch,
    section: str,
    field: str,
    value: object,
) -> None:
    import ray

    request = _target_request()
    object_ref = object()
    handle = _target_handle(request, object_ref)
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    wire = json.loads(encode_target_execution_result(_target_completion(request)))
    if section == "runtime":
        wire["observed_target"]["observed_runtime"][field] = value
    else:
        wire[field] = value
    serialized = json.dumps(wire, sort_keys=True, separators=(",", ":"))
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([object_ref], []))
    monkeypatch.setattr(ray, "get", lambda _ref: serialized)

    [result] = runner._poll_target_execution_results(
        (handle,),
        validation_now=_OBSERVED_AT + timedelta(seconds=2),
    )

    assert result.transport_state is RayCoreTargetExecutionTransportState.UNCERTAIN
    assert result.result is None
    assert result.uncertainty is RayCoreTargetExecutionUncertainty.INVALID_RESULT
    assert runner.pending_count == 1


def test_runner_keeps_different_canonical_claim_time_uncertain(monkeypatch) -> None:
    import ray

    request = _target_request()
    object_ref = object()
    handle = _target_handle(request, object_ref)
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    wire = json.loads(encode_target_execution_result(_target_completion(request)))
    changed_claimed_at = request.target_execution_claimed_at + timedelta(microseconds=1)
    wire["target_execution_claimed_at"] = changed_claimed_at.isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    serialized = json.dumps(wire, sort_keys=True, separators=(",", ":"))
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([object_ref], []))
    monkeypatch.setattr(ray, "get", lambda _ref: serialized)

    [result] = runner._poll_target_execution_results(
        (handle,),
        validation_now=_OBSERVED_AT + timedelta(seconds=2),
    )

    assert result.transport_state is RayCoreTargetExecutionTransportState.UNCERTAIN
    assert result.result is None
    assert result.uncertainty is RayCoreTargetExecutionUncertainty.EVIDENCE_MISMATCH
    assert runner.pending_count == 1


def test_runner_decodes_exact_verified_completion(monkeypatch) -> None:
    import ray

    request = _target_request()
    object_ref = object()
    handle = _target_handle(request, object_ref)
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    completion = TargetExecutionCompletion(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=_observed_target(request),
        application_completion=TargetApplicationCompletion(
            success=True,
            result=42,
            result_reference=None,
            error=None,
            traceback=None,
            exception_type=None,
            retryable=None,
        ),
    )
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([object_ref], []))
    monkeypatch.setattr(ray, "get", lambda _ref: encode_target_execution_result(completion))

    [result] = runner._poll_target_execution_results(
        (handle,),
        validation_now=_OBSERVED_AT + timedelta(seconds=2),
    )

    assert result.transport_state is RayCoreTargetExecutionTransportState.COMPLETION
    assert result.result == completion
    assert result.uncertainty is None
    assert runner.pending_count == 0


def test_runner_rejects_semantically_false_compatibility_reason(monkeypatch) -> None:
    import ray

    request = _target_request()
    object_ref = object()
    handle = _target_handle(request, object_ref)
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    rejection = TargetExecutionCompatibilityRejection(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=_observed_target(request),
        compatibility_reason=TargetExecutionCompatibilityReason.PYTHON_VERSION_MISMATCH,
    )
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([object_ref], []))
    monkeypatch.setattr(ray, "get", lambda _ref: encode_target_execution_result(rejection))

    [result] = runner._poll_target_execution_results(
        (handle,),
        validation_now=_OBSERVED_AT + timedelta(seconds=2),
    )

    assert result.transport_state is RayCoreTargetExecutionTransportState.UNCERTAIN
    assert result.result is None
    assert result.uncertainty is RayCoreTargetExecutionUncertainty.PROOF_MISMATCH
    assert runner.pending_count == 1


@pytest.mark.parametrize(
    ("claimed_at", "validation_now"),
    [
        (
            _CLAIMED_AT,
            _OBSERVED_AT + timedelta(microseconds=500_000),
        ),
        (
            _OBSERVED_AT + timedelta(seconds=2),
            _OBSERVED_AT + timedelta(seconds=3),
        ),
    ],
)
def test_runner_keeps_future_or_preclaim_remote_time_uncertain(
    monkeypatch,
    claimed_at: datetime,
    validation_now: datetime,
) -> None:
    import ray

    request = _target_request(target_execution_claimed_at=claimed_at)
    object_ref = object()
    handle = _target_handle(request, object_ref)
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    completion = TargetExecutionCompletion(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=_observed_target(request),
        application_completion=TargetApplicationCompletion(
            success=True,
            result=42,
            result_reference=None,
            error=None,
            traceback=None,
            exception_type=None,
            retryable=None,
        ),
    )
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([object_ref], []))
    monkeypatch.setattr(ray, "get", lambda _ref: encode_target_execution_result(completion))

    [result] = runner._poll_target_execution_results(
        (handle,),
        validation_now=validation_now,
    )

    assert result.transport_state is RayCoreTargetExecutionTransportState.UNCERTAIN
    assert result.result is None
    assert result.uncertainty is RayCoreTargetExecutionUncertainty.PROOF_MISMATCH
    assert runner.pending_count == 1


def test_runner_retires_semantically_authenticated_compatibility_rejection(
    monkeypatch,
) -> None:
    import ray

    request = _target_request()
    object_ref = object()
    handle = _target_handle(request, object_ref)
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    rejection = _compatibility_rejection(
        request,
        TargetExecutionCompatibilityReason.MEMBERSHIP_MISMATCH,
        _observed_for(
            request,
            observed_membership_digest="sha256:" + "f" * 64,
        ),
    )
    monkeypatch.setattr(ray, "wait", lambda *_args, **_kwargs: ([object_ref], []))
    monkeypatch.setattr(ray, "get", lambda _ref: encode_target_execution_result(rejection))

    [result] = runner._poll_target_execution_results(
        (handle,),
        validation_now=_OBSERVED_AT + timedelta(seconds=2),
    )

    assert result.transport_state is RayCoreTargetExecutionTransportState.COMPATIBILITY_REJECTION
    assert result.result == rejection
    assert result.uncertainty is None
    assert runner.pending_count == 0


def test_runner_maps_wait_connection_failure_to_uncertain(monkeypatch) -> None:
    import ray

    request = _target_request()
    handle = _target_handle(request, object())
    runner = RayCoreRunner.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}

    def disconnected(*_args, **_kwargs):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(ray, "wait", disconnected)

    [result] = runner._poll_target_execution_results((handle,))

    assert result.transport_state is RayCoreTargetExecutionTransportState.UNCERTAIN
    assert result.result is None
    assert result.uncertainty is RayCoreTargetExecutionUncertainty.RAY_TRANSPORT_ERROR
    assert runner.pending_count == 1
