"""Tests for the testproject's opt-in complex-workflow failure fixture."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from ray.exceptions import RayTaskError

from django_ray.runner import retry as retry_module
from django_ray.runtime.entrypoint import _serialize_error
from testproject import settings as testproject_settings
from testproject.apps.cluster_tasks import tasks
from testproject.apps.cluster_tasks.workflows import (
    COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE,
    ComplexWorkflowFixtureError,
)


def _raise(error: Exception) -> Callable[..., None]:
    def raise_error(*_args: Any, **_kwargs: Any) -> None:
        raise error

    return raise_error


@pytest.mark.parametrize(
    ("failure_branch", "failure_item", "message"),
    [
        (None, 0, "provided together"),
        ("fast", None, "provided together"),
        ("other", 0, "must be 'fast' or 'slow'"),
        ("fast", True, "must be an integer"),
        ("fast", -1, "select an item"),
        ("slow", 2, "select an item"),
    ],
)
def test_complex_workflow_failure_controls_reject_invalid_selection(
    failure_branch: str | None,
    failure_item: int | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        tasks.validate_complex_workflow_failure_controls(
            fast_items=3,
            slow_items=2,
            failure_branch=failure_branch,
            failure_item=failure_item,
        )


def test_complex_workflow_task_passes_unchanged_defaults(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(tasks, "run_complex_branch_workflow", run)

    assert tasks.complex_workflow_benchmark.func() == {"status": "ok"}
    assert captured == {
        "args": (8, 4, 0.02, 0.5),
        "kwargs": {
            "use_ray": True,
        },
    }


def test_complex_workflow_task_normalizes_exact_ray_wrapped_fixture(monkeypatch) -> None:
    wrapped = RayTaskError(
        "run_cpu_work_item",
        "fixture traceback",
        ComplexWorkflowFixtureError(COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE),
        proctitle="ray::run_cpu_work_item",
        pid=123,
        ip="127.0.0.1",
    )
    monkeypatch.setattr(tasks, "run_complex_branch_workflow", _raise(wrapped))

    with pytest.raises(ComplexWorkflowFixtureError) as caught:
        tasks.complex_workflow_benchmark.func(
            failure_branch="fast",
            failure_item=1,
        )

    assert type(caught.value) is ComplexWorkflowFixtureError
    assert str(caught.value) == COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE
    serialized = json.loads(_serialize_error(caught.value))
    exception_type = "testproject.apps.cluster_tasks.workflows.ComplexWorkflowFixtureError"
    assert serialized["exception_type"] == exception_type
    denylist = testproject_settings.DJANGO_RAY["RETRY_EXCEPTION_DENYLIST"]
    assert isinstance(denylist, list)
    assert exception_type in denylist
    monkeypatch.setattr(
        retry_module,
        "get_settings",
        lambda: testproject_settings.DJANGO_RAY,
    )
    decision = retry_module.should_retry(
        SimpleNamespace(attempt_number=1),
        exception_type=serialized["exception_type"],
    )
    assert decision.should_retry is False
    assert decision.next_attempt_at is None


def test_complex_workflow_task_does_not_normalize_unrelated_wrapped_failure(
    monkeypatch,
) -> None:
    wrapped = RayTaskError(
        "run_cpu_work_item",
        "unrelated traceback",
        RuntimeError("unrelated failure"),
        proctitle="ray::run_cpu_work_item",
        pid=123,
        ip="127.0.0.1",
    )
    monkeypatch.setattr(tasks, "run_complex_branch_workflow", _raise(wrapped))

    with pytest.raises(RayTaskError) as caught:
        tasks.complex_workflow_benchmark.func(
            failure_branch="fast",
            failure_item=1,
        )

    assert caught.value is wrapped
