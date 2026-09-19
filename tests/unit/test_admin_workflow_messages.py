"""Unavailable graphs explain observed conditions without exposing schema choices."""

import pytest

from django_ray.admin import RayTaskExecutionAdmin
from django_ray.workflow.admin_graph import (
    AdminWorkflowGraphError,
    degraded_admin_workflow_graph,
    inspect_admin_workflow_graph_summary,
)


@pytest.mark.parametrize(
    "state,availability,status,phrase",
    [
        ("RUNNING", "NOT_REPORTED", "NOT_REPORTED", "has not finished"),
        ("SUCCEEDED", "DISABLED", "UNAVAILABLE", "reporting was disabled"),
        ("SUCCEEDED", "OMITTED_BY_POLICY", "UNAVAILABLE", "terminal-only reporting"),
        ("FAILED", "EXPIRED", "UNAVAILABLE", "have expired"),
        ("FAILED", "MISSING", "UNAVAILABLE", "are missing"),
    ],
)
def test_observed_summary_reason_reaches_admin_response(state, availability, status, phrase):
    summary = {
        "schema": "django-ray.workflow-progress-summary",
        "schema_version": 1,
        "source_schema_version": 3,
        "summary": {"state": state},
        "availability": availability,
    }
    with pytest.raises(AdminWorkflowGraphError) as caught:
        inspect_admin_workflow_graph_summary(summary)
    error = caught.value
    assert error.status == status
    response = RayTaskExecutionAdmin._workflow_graph_issue_response(error)
    assert response.status_code == 200
    assert phrase.encode() in response.content
    assert b"schema-v3" not in response.content


@pytest.mark.parametrize("source", [None, 1, 2])
def test_missing_or_historical_data_never_promises_waiting_will_make_a_graph(source):
    summary = {
        "schema": "django-ray.workflow-progress-summary",
        "schema_version": 1,
        "source_schema_version": source,
    }
    with pytest.raises(AdminWorkflowGraphError) as caught:
        inspect_admin_workflow_graph_summary(summary)
    message = str(caught.value)
    assert "future" in message or "cannot reconstruct past runs" in message
    assert "not available yet" not in message
    assert "schema" not in message


@pytest.mark.parametrize(
    "status",
    ["NOT_REPORTED", "UNSUPPORTED", "TRUNCATED", "UNAVAILABLE", "LIMIT_EXCEEDED", "CORRUPT"],
)
def test_degraded_messages_preserve_empty_bounded_graph_contract(status):
    graph = degraded_admin_workflow_graph(status)
    assert len(graph["message"].encode()) <= 256
    assert graph["status"] == status
    assert graph["schema_version"] == 2
    assert graph["complete"] is False
    assert graph["nodes"] == graph["edges"] == []


@pytest.mark.parametrize(
    "status,reason", [("UNAVAILABLE", "raw user error"), ("CORRUPT", "RUNNING")]
)
def test_graph_messages_cannot_contain_arbitrary_diagnostics(status, reason):
    with pytest.raises(ValueError, match="Unsupported admin workflow graph reason"):
        AdminWorkflowGraphError(status, reason=reason)
    with pytest.raises(ValueError, match="Unsupported admin workflow graph reason"):
        degraded_admin_workflow_graph(status, reason=reason)
