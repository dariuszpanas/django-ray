"""Matched first-workflow evidence must preserve failures without overlapping work."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from qualification.application import run_first_workflows as runner
from qualification.application import run_workflows
from qualification.application.run_chainsaw import parse_receipts


def passing_pair(monkeypatch):
    def execute(_request, *, token, case, on_terminal):
        assert token == "private-token"
        assert dict(case.options) == {"item_count": 3, "work_seconds": 0.05}
        on_terminal()
        task_id = (
            "f717c512-17d7-4b5e-b778-d614fb14427c"
            if case.name == "showcase-first"
            else "f717c512-17d7-4b5e-b778-d614fb14427d"
        )
        return [
            {
                "task_id": task_id,
                "state": "SUCCEEDED",
                "reporting_policy": "full",
                "counts": {"nodes": 21, "edges": 28},
                "api_admin_graph_match": True,
                "fixture_graph_verified": True,
                "complete_workflow_gate": False,
                "admin_contract": {
                    "task_id": task_id,
                    "task_state": "SUCCEEDED",
                    "attempt_number": 1,
                    "admin_workflow": "verified",
                    "graph_status": "AVAILABLE",
                    "graph_nodes": 21,
                    "graph_edges": 28,
                    "graph_succeeded_nodes": 21,
                    "graph_preview_contract": "showcase-succeeded-verified",
                    "diagnostics_preserved": True,
                },
                "browser_contract": {
                    "status": "passed",
                    "policy": "full",
                    "attempts_rendered": 1,
                    "javascript_errors": 0,
                    "diagnostics_preserved": True,
                },
            }
        ]

    mock = Mock(side_effect=execute)
    monkeypatch.setattr(run_workflows, "execute_case", mock)
    return mock


def test_first_and_warm_cases_keep_the_original_three_item_workload():
    first, warm = run_workflows.first_workflow_cases()
    assert first.name == "showcase-first"
    assert warm.name == "showcase-warm"
    assert first.options == warm.options == (("item_count", 3), ("work_seconds", 0.05))
    assert first.endpoint == warm.endpoint == "/api/cluster/workflow-showcase"
    assert first.callable_path == warm.callable_path
    assert first.states == warm.states == ("SUCCEEDED",)
    assert first.policy == warm.policy == "full"
    assert not set(run_workflows.first_workflow_cases()) & set(run_workflows.workflow_cases())


def test_matched_pair_passes_once_in_order_and_collects_before_other_receipts(monkeypatch):
    execute = passing_pair(monkeypatch)
    receipt = runner.observe_pair(Mock(), token="private-token")
    runner.validate_receipt(receipt)
    assert receipt["status"] == "passed"
    assert [call.kwargs["case"].name for call in execute.call_args_list] == [
        "showcase-first",
        "showcase-warm",
    ]
    raw = json.dumps(receipt).encode()
    assert parse_receipts(raw, ("before-first",)) == {"before-first": raw}
    assert b"private-token" not in raw


@pytest.mark.parametrize("mode", ["help", "receipt"])
def test_host_runner_and_valid_receipt_need_only_the_standard_library(tmp_path, monkeypatch, mode):
    passing_pair(monkeypatch)
    path = tmp_path / "first.json"
    path.write_text(json.dumps(runner.observe_pair(Mock(), token="private-token")))
    script = """
import json
import runpy
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
if sys.argv[2] == "help":
    sys.argv = ["qualification.application.run_chainsaw", "--help"]
    try:
        runpy.run_module("qualification.application.run_chainsaw", run_name="__main__")
    except SystemExit as error:
        assert error.code == 0
else:
    from qualification.application import run_chainsaw
    raw = Path(sys.argv[3]).read_bytes()
    run_chainsaw.validate_first_workflow_receipt(json.loads(raw))
    assert set(run_chainsaw.parse_receipts(raw, ("before-first",))) == {"before-first"}
assert not any(name in sys.modules for name in ("django", "django_ray", "ray"))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(Path(__file__).resolve().parents[2]),
            mode,
            str(path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_diagnostic_receipt_identifies_requested_limits_and_cannot_replace_acceptance(monkeypatch):
    from qualification.application.resource_profiles import select_profile

    passing_pair(monkeypatch)
    receipt = runner.observe_pair(
        Mock(),
        token="private-token",
        resource_profile="constrained-ray",
        validation_intent="diagnostic",
    )
    expected = select_profile("constrained-ray", "diagnostic")
    assert receipt["qualification_profile"] == expected
    raw = json.dumps(receipt).encode()
    assert parse_receipts(raw, ("before-first",), expected_profile=expected)
    with pytest.raises(ValueError, match="another resource profile"):
        parse_receipts(
            raw, ("before-first",), expected_profile=select_profile("standard", "acceptance")
        )
    receipt["qualification_profile"]["validation_intent"] = "acceptance"
    with pytest.raises(ValueError, match="diagnostic intent"):
        runner.validate_receipt(receipt)


@pytest.mark.parametrize("settled", [False, True])
def test_first_failure_cannot_be_hidden_by_warm_success(monkeypatch, settled):
    def execute(_request, *, token, case, on_terminal):
        if settled or case.name == "showcase-warm":
            on_terminal()
        if case.name == "showcase-first":
            raise ValueError("private task value or token")
        return [{"verified": True}]

    execute = Mock(side_effect=execute)
    monkeypatch.setattr(run_workflows, "execute_case", execute)
    result = runner.observe_pair(Mock(), token="private-token")
    assert result["status"] == "failed"
    assert execute.call_count == (2 if settled else 1)
    assert result["trials"][0]["status"] == "failed"
    if settled:
        assert result["trials"][1]["status"] == "passed"
    assert "private" not in json.dumps(result)
    with pytest.raises(ValueError):
        parse_receipts(json.dumps(result).encode(), ("before-first",))


@pytest.mark.parametrize(
    "corrupt",
    ["order", "input", "timeout", "first", "settlement", "time", "empty", "browser", "identity"],
)
def test_collector_refuses_invalid_first_pair_even_when_status_says_passed(monkeypatch, corrupt):
    passing_pair(monkeypatch)
    result = runner.observe_pair(Mock(), token="private-token")
    if corrupt == "order":
        result["trials"].reverse()
    elif corrupt == "input":
        result["inputs"]["item_count"] = 1
    elif corrupt == "timeout":
        result["terminal_flush_timeout_seconds"] = 60
    elif corrupt == "first":
        result["trials"][0]["status"] = "failed"
    elif corrupt == "settlement":
        result["trials"][0]["terminal_observed"] = False
    elif corrupt == "time":
        result["trials"][0]["elapsed_seconds"] = float("inf")
    elif corrupt == "empty":
        result["trials"][0]["observations"] = [{}]
    elif corrupt == "browser":
        result["trials"][0]["observations"][0]["browser_contract"]["attempts_rendered"] = 0
    else:
        result["trials"][1]["observations"][0]["task_id"] = result["trials"][0]["observations"][0][
            "task_id"
        ]
    with pytest.raises(ValueError):
        parse_receipts(json.dumps(result).encode(), ("before-first",))


@pytest.mark.parametrize("timeout", [15, 60])
def test_entrypoint_checks_timeout_and_preserves_bounded_receipt(
    monkeypatch, tmp_path, capsys, timeout
):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr(
        "django_ray.conf.settings.get_settings",
        lambda: {
            "WORKFLOW_PROGRESS_REPORTING_POLICY": "full",
            "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": timeout,
        },
    )
    monkeypatch.setattr(runner, "read_token", lambda _: "private-token")
    execute = passing_pair(monkeypatch)
    path = tmp_path / "first.json"
    status = runner.main(["--token-file", str(tmp_path / "token"), "--receipt", str(path)])
    output = capsys.readouterr().out
    assert "private-token" not in output
    assert status == (0 if timeout == 15 else 1)
    assert execute.call_count == (2 if timeout == 15 else 0)
    if timeout == 15:
        assert json.loads(path.read_bytes()) == json.loads(output)
        assert path.stat().st_size < runner.RECEIPT_MAX_BYTES
    else:
        assert not path.exists()


def test_showcase_graph_verifier_requires_three_items_in_both_maps():
    from qualification.application.workflow_fixtures import (
        WORKFLOW_SHOWCASE_EDGES,
        WORKFLOW_SHOWCASE_NODE_LAYERS,
        verify_showcase_graph,
    )

    graph = {
        "nodes": [
            {"id": node, "state": "SUCCEEDED", "kind": "task"}
            for node in sorted(frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS))
        ],
        "edges": [{"source": a, "target": b} for a, b in sorted(WORKFLOW_SHOWCASE_EDGES)],
    }
    assert len(graph["nodes"]) == 21
    assert len(graph["edges"]) == 28
    for node in graph["nodes"]:
        if node["id"] in {"0.1.g0.1", "0.5"}:
            node.update(
                kind="map",
                fanout={
                    "submitted_items": 3,
                    "completed_items": 3,
                    "in_flight_items": 0,
                    "input_exhausted": True,
                },
            )
    verify_showcase_graph(graph)
    for node_id in ("0.1.g0.1", "0.5"):
        changed = copy.deepcopy(graph)
        node = next(node for node in changed["nodes"] if node["id"] == node_id)
        node["fanout"]["completed_items"] = 1
        with pytest.raises(ValueError, match="matched map"):
            verify_showcase_graph(changed)
