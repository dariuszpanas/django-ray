"""Tests for the order-fulfillment showcase controls and failure fixture."""

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
from testproject.apps.cluster_tasks import tasks, workflows
from testproject.apps.cluster_tasks.workflows import (
    WorkflowShowcaseFixtureError,
    workflow_showcase_fixture_error_message,
)


def _raise(error: Exception) -> Callable[..., None]:
    def raise_error(*_args: Any, **_kwargs: Any) -> None:
        raise error

    return raise_error


@pytest.mark.parametrize(
    ("item_count", "work_seconds", "failure_stage", "failure_item", "message"),
    [
        (True, 0.05, None, None, "item_count must be an integer"),
        (0, 0.05, None, None, "item_count must be between"),
        (9, 0.05, None, None, "item_count must be between"),
        (3, True, None, None, "work_seconds must be a number"),
        (3, -0.01, None, None, "work_seconds must be finite"),
        (3, 1.01, None, None, "work_seconds must be finite"),
        (3, float("inf"), None, None, "work_seconds must be finite"),
        (3, 0.05, "reserve_inventory", None, "provided together"),
        (3, 0.05, None, 0, "provided together"),
        (3, 0.05, "other", 0, "failure_stage must be"),
        (3, 0.05, "reserve_inventory", True, "failure_item must be an integer"),
        (3, 0.05, "reserve_inventory", -1, "must select an item"),
        (3, 0.05, "reserve_inventory", 3, "must select an item"),
    ],
)
def test_workflow_showcase_rejects_unbounded_or_invalid_controls(
    item_count: int,
    work_seconds: float,
    failure_stage: str | None,
    failure_item: int | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        workflows.validate_order_fulfillment_showcase_inputs(
            item_count=item_count,
            work_seconds=work_seconds,
            failure_stage=failure_stage,
            failure_item=failure_item,
        )


def test_workflow_showcase_selects_exactly_one_reservation_failure() -> None:
    batch = workflows.build_order_batch(3, 0, "reserve_inventory", 1)
    validations = [
        workflows.validate_order_item(item) for item in workflows.select_validation_items(batch)
    ]
    customer = workflows.join_customer_context(
        [
            workflows.load_customer_profile(batch),
            workflows.load_customer_history(batch),
        ]
    )
    inventory = workflows.load_inventory_snapshot(batch)
    context = workflows.join_order_inputs([validations, customer, inventory])
    selected_items = workflows.select_reservation_items(context)
    risk_recommendation = workflows.join_risk_recommendation(
        [
            workflows.score_order_risk(context),
            workflows.build_order_recommendation(context),
        ]
    )
    commercial = workflows.join_commercial_context(
        [
            workflows.calculate_order_price(context),
            risk_recommendation,
        ]
    )
    reservation_items = workflows.attach_commercial_context_to_reservations(
        [selected_items, commercial]
    )

    assert [
        item["item_id"]
        for item in reservation_items
        if item["_fail_workflow_showcase_fixture"] is True
    ] == [1]
    assert all(
        item["_fail_workflow_showcase_fixture"] is False
        for item in (reservation_items[0], reservation_items[2])
    )
    assert all(item["commercial"] == commercial for item in reservation_items)
    with pytest.raises(
        WorkflowShowcaseFixtureError,
        match=workflow_showcase_fixture_error_message(1),
    ):
        workflows.reserve_inventory(reservation_items[1])


def test_workflow_showcase_task_forwards_full_reporting_defaults(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(tasks, "run_order_fulfillment_showcase_workflow", run)

    assert tasks.order_fulfillment_showcase_task.func() == {"status": "ok"}
    assert captured == {
        "args": (3, 0.05),
        "kwargs": {
            "failure_stage": None,
            "failure_item": None,
            "use_ray": True,
        },
    }


def test_workflow_showcase_task_normalizes_exact_ray_wrapped_fixture(
    monkeypatch,
) -> None:
    expected_message = workflow_showcase_fixture_error_message(1)
    wrapped = RayTaskError(
        "reserve_inventory",
        "fixture traceback",
        WorkflowShowcaseFixtureError(expected_message),
        proctitle="ray::reserve_inventory",
        pid=123,
        ip="127.0.0.1",
    )
    monkeypatch.setattr(
        tasks,
        "run_order_fulfillment_showcase_workflow",
        _raise(wrapped),
    )

    with pytest.raises(WorkflowShowcaseFixtureError) as caught:
        tasks.order_fulfillment_showcase_task.func(
            failure_stage="reserve_inventory",
            failure_item=1,
        )

    assert type(caught.value) is WorkflowShowcaseFixtureError
    assert str(caught.value) == expected_message
    serialized = json.loads(_serialize_error(caught.value))
    exception_type = "testproject.apps.cluster_tasks.workflows.WorkflowShowcaseFixtureError"
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


def test_workflow_showcase_task_preserves_wrong_item_from_ray_cause(
    monkeypatch,
) -> None:
    requested_message = workflow_showcase_fixture_error_message(1)
    actual_cause = WorkflowShowcaseFixtureError(workflow_showcase_fixture_error_message(2))
    wrapped = RayTaskError(
        "reserve_inventory",
        "fixture traceback",
        actual_cause,
        proctitle="ray::reserve_inventory",
        pid=123,
        ip="127.0.0.1",
    )
    monkeypatch.setattr(
        tasks,
        "run_order_fulfillment_showcase_workflow",
        _raise(wrapped),
    )

    with pytest.raises(WorkflowShowcaseFixtureError) as caught:
        tasks.order_fulfillment_showcase_task.func(
            failure_stage="reserve_inventory",
            failure_item=1,
        )

    assert caught.value is actual_cause
    assert str(caught.value) == workflow_showcase_fixture_error_message(2)
    assert str(caught.value) != requested_message


def test_workflow_showcase_task_does_not_normalize_unrelated_failure(
    monkeypatch,
) -> None:
    unrelated = RuntimeError("unrelated failure")
    monkeypatch.setattr(
        tasks,
        "run_order_fulfillment_showcase_workflow",
        _raise(unrelated),
    )

    with pytest.raises(RuntimeError) as caught:
        tasks.order_fulfillment_showcase_task.func(
            failure_stage="reserve_inventory",
            failure_item=1,
        )

    assert caught.value is unrelated
