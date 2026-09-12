"""Small resource-free checks for the sender-only measurement contract."""

from __future__ import annotations

import sys

import pytest

from scripts import benchmark_distributed_preparation as benchmark


@pytest.mark.parametrize("helper", ["map", "starmap", "scatter"])
def test_measurement_uses_a_nonexecuting_sink_and_observes_preparation(helper: str) -> None:
    result = benchmark._measure(helper, 5, 2)
    assert result["execution_protocol_version"] == 3
    assert result["context_kind"] == "synthetic-sender-only"
    assert result["requests"] == 5
    assert result["full_encodings"] == 1
    assert result["callable_hashes"] == (5 if helper == "scatter" else 1)
    assert result["max_prepared_ahead"] == 2
    assert result["effective_window"] == 2
    assert len(result["wire_sha256"]) == 64
    assert result["python_peak_bytes"] > 0
    assert result["wall_seconds"] >= result["first_submit_seconds"] >= 0


def test_benchmark_rejects_cross_epoch_comparisons_without_claiming_byte_equivalence():
    with pytest.raises(ValueError, match="different active execution protocols"):
        benchmark._compare_request_bytes(
            {"execution_protocol_version": 1, "wire_sha256": "a" * 64},
            {"execution_protocol_version": 3, "wire_sha256": "a" * 64},
        )


@pytest.mark.parametrize("protocol", [1, 3])
def test_same_epoch_benchmark_still_requires_exact_wire_bytes(protocol):
    baseline = {"execution_protocol_version": protocol, "wire_sha256": "a" * 64}
    benchmark._compare_request_bytes(baseline, dict(baseline))
    with pytest.raises(AssertionError, match="request bytes differ"):
        benchmark._compare_request_bytes(baseline, {**baseline, "wire_sha256": "b" * 64})


def test_current_benchmark_parent_is_canonical_but_only_synthetic_sender_state():
    from django_ray.execution_codec import ExecutionIdentity
    from django_ray.runtime.context import (
        get_current_task_context,
        require_strict_task_execution_context,
    )
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.target.cohort_contract import decode_cohort_execution_contract
    from django_ray.workflow.plans import runtime_env_plan_identity

    identity = runtime_env_plan_identity(normalize_runtime_env({})).as_transport_dict()
    with benchmark._sender_context(identity):
        context = require_strict_task_execution_context(get_current_task_context())
        contract = decode_cohort_execution_contract(
            context.cohort_contract_json,
            expected_contract_digest=context.cohort_contract_digest,
            expected_identity=ExecutionIdentity(41, "benchmark-41", 1, 1),
        )
        assert contract.target_expectation.cluster_session == "session_benchmark"
        assert context.execution_protocol_version == 3


@pytest.mark.parametrize(
    "arguments",
    [
        ["--items", "0"],
        ["--items", "5001"],
        ["--items", "1", "1"],
        ["--window", "257"],
        ["--repetitions", "6"],
        ["--case-timeout", "181"],
    ],
)
def test_cli_refuses_unbounded_profiles(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["benchmark", *arguments])
    with pytest.raises(SystemExit) as caught:
        benchmark.main()
    assert caught.value.code == 2


def test_cli_refuses_nonlinux_benchmark_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["benchmark"])
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit) as caught:
        benchmark.main()
    assert caught.value.code == 2
