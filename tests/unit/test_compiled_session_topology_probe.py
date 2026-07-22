"""Tests for the opt-in raw-Ray compiled-session topology probe."""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

import pytest

from scripts import compiled_session_topology_probe as probe


def _valid_report(invocations: int = 3) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = probe.build_run_identity("93ca4356-7666-4106-b1d8-c9f5181bf372")
    results = []
    for index in range(invocations):
        results.append(
            {
                "run_identity": identity,
                "invocation_id": f"invocation-{index}",
                "invoked_by_pid": 100,
                "value": index + 2,
                "stage_trace": [
                    {"name": "left", "pid": 101, "invocation_count": index + 1},
                    {"name": "right", "pid": 102, "invocation_count": index + 1},
                ],
            }
        )
    return (
        {
            "schema_version": probe.RESULT_SCHEMA_VERSION,
            "topology": probe.TOPOLOGY,
            "reuse_boundary": "within_run",
            "run_identity": identity,
            "compiled_by_pid": 100,
            "compile_count": 1,
            "invocation_count": invocations,
            "max_in_flight": 1,
            "max_buffered_results": 1,
            "teardown_completed": True,
            "results": results,
        },
        identity,
    )


def test_config_reserves_ray_client_for_live_cluster_probe() -> None:
    with pytest.raises(ValueError, match="dedicated live-cluster topology probe"):
        probe.ProbeConfig(address="ray://cluster:10001").validate()


def test_config_rejects_unbounded_values() -> None:
    with pytest.raises(ValueError, match="invocations"):
        probe.ProbeConfig(invocations=1).validate()
    with pytest.raises(ValueError, match="timeout_seconds"):
        probe.ProbeConfig(timeout_seconds=0).validate()
    with pytest.raises(ValueError, match="namespace"):
        probe.ProbeConfig(namespace=" ").validate()


def test_run_identity_matches_fenced_workflow_shape() -> None:
    assert probe.build_run_identity("run-1") == {
        "schema_version": 1,
        "task_execution_pk": 0,
        "attempt_number": 1,
        "execution_generation": 1,
        "run_id": "run-1",
    }


def test_validate_report_accepts_one_owner_and_reused_actors() -> None:
    report, identity = _valid_report()

    probe.validate_report(
        report,
        expected_run_identity=identity,
        expected_invocations=3,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report.update(compile_count=2), "compile exactly once"),
        (
            lambda report: report["results"][1].update(invocation_id="invocation-0"),
            "not unique",
        ),
        (
            lambda report: report["results"][1]["stage_trace"][0].update(invocation_count=99),
            "not reused",
        ),
        (
            lambda report: report["results"][0].update(invoked_by_pid=999),
            "compiler process",
        ),
        (
            lambda report: report["results"][0]["stage_trace"][0].update(pid=100),
            "separate processes",
        ),
        (lambda report: report.update(teardown_completed=False), "explicit teardown"),
    ],
)
def test_validate_report_rejects_invalid_topology_evidence(mutation, message: str) -> None:
    report, identity = _valid_report()
    changed = deepcopy(report)
    mutation(changed)

    with pytest.raises(AssertionError, match=message):
        probe.validate_report(
            changed,
            expected_run_identity=identity,
            expected_invocations=3,
        )


def test_main_refuses_native_work_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(probe.OPT_IN_ENV, raising=False)
    monkeypatch.setattr(
        probe,
        "run_probe",
        lambda _config: pytest.fail("native probe must not run without opt-in"),
    )

    assert probe.main([]) == 2
    assert probe.OPT_IN_ENV in capsys.readouterr().err


def test_main_emits_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(probe.OPT_IN_ENV, "1")

    def fail(_config):
        raise RuntimeError("native failure")

    monkeypatch.setattr(probe, "run_probe", fail)

    assert probe.main(["--address", "local"]) == 1
    output = capsys.readouterr().out
    assert '"status": "failed"' in output
    assert '"error_type": "RuntimeError"' in output


@pytest.mark.compiled_graph_opt_in
@pytest.mark.real_ray
@pytest.mark.skipif(
    os.environ.get(probe.OPT_IN_ENV) != "1",
    reason=f"set {probe.OPT_IN_ENV}=1 after the #86 capability gate passes",
)
def test_opt_in_native_nested_owner_topology() -> None:
    config = probe.ProbeConfig(address="local", invocations=2, timeout_seconds=60)

    report = probe.run_probe(config)

    assert report["topology"] == probe.TOPOLOGY
    assert report["reuse_boundary"] == "within_run"
