"""Unit tests for task reconciliation guard conditions."""

from __future__ import annotations

from types import SimpleNamespace

from django_ray.runner import reconciliation


def test_is_task_stuck_rejects_non_running_and_missing_activity() -> None:
    assert reconciliation.is_task_stuck(SimpleNamespace(state="SUCCEEDED")) is False
    assert (
        reconciliation.is_task_stuck(
            SimpleNamespace(state="RUNNING", last_heartbeat_at=None, started_at=None)
        )
        is False
    )


def test_is_task_timed_out_rejects_non_running_and_incomplete_tasks() -> None:
    assert reconciliation.is_task_timed_out(SimpleNamespace(state="FAILED")) is False
    assert (
        reconciliation.is_task_timed_out(
            SimpleNamespace(state="RUNNING", timeout_seconds=None, started_at=None)
        )
        is False
    )
    assert (
        reconciliation.is_task_timed_out(
            SimpleNamespace(state="RUNNING", timeout_seconds=10, started_at=None)
        )
        is False
    )
