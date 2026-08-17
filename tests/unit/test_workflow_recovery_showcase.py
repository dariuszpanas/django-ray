"""Tests for the deterministic three-attempt workflow recovery showcase."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ray.exceptions import RayTaskError

from django_ray.runner import retry as retry_module
from django_ray.runtime.context import DurableTaskContext, durable_task_execution
from django_ray.runtime.entrypoint import _serialize_error
from django_ray.runtime.runtime_env import resolve_runtime_env_profile
from django_ray.workflow.plans import materialize_workflow_plan, runtime_env_plan_identity
from testproject import settings as testproject_settings
from testproject.apps.cluster_tasks import tasks, workflows


def _raise(error: Exception) -> Callable[..., None]:
    def raise_error(*_args: Any, **_kwargs: Any) -> None:
        raise error

    return raise_error


@pytest.mark.parametrize(
    ("attempt_number", "stage"),
    [
        (1, workflows.WORKFLOW_RECOVERY_EARLY_STAGE),
        (2, workflows.WORKFLOW_RECOVERY_MID_STAGE),
        (3, workflows.WORKFLOW_RECOVERY_SUCCESS_STAGE),
        (4, workflows.WORKFLOW_RECOVERY_SUCCESS_STAGE),
    ],
)
def test_recovery_stage_is_derived_only_from_durable_attempt(
    attempt_number: int,
    stage: str,
) -> None:
    assert tasks.workflow_recovery_stage_for_attempt(attempt_number) == stage


@pytest.mark.parametrize("attempt_number", [True, 0, -1])
def test_recovery_stage_rejects_invalid_attempt_identity(attempt_number: int) -> None:
    with pytest.raises(ValueError, match="attempt_number"):
        tasks.workflow_recovery_stage_for_attempt(attempt_number)


def test_recovery_workflow_fails_at_entry_before_building_a_batch() -> None:
    with pytest.raises(
        workflows.WorkflowRecoveryEarlyFixtureError,
        match=workflows.WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE,
    ):
        workflows.build_recovery_order_batch(
            1,
            0,
            workflows.WORKFLOW_RECOVERY_EARLY_STAGE,
        )


def test_recovery_attempt_stages_keep_one_content_hashed_retry_safe_plan_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DJANGO_RAY_IMAGE_DIGEST", raising=False)
    trust_identity = testproject_settings.DJANGO_RAY.get(
        "WORKFLOW_PLAN_TRUST_IDENTITY",
        {},
    )
    recovery = resolve_runtime_env_profile(
        "recovery-showcase",
        config=testproject_settings.DJANGO_RAY,
    )
    project = resolve_runtime_env_profile(
        "project",
        config=testproject_settings.DJANGO_RAY,
    )
    recovery_identity = runtime_env_plan_identity(
        recovery,
        trust_identity=trust_identity,
    )
    project_identity = runtime_env_plan_identity(
        project,
        trust_identity=trust_identity,
    )
    task_context = DurableTaskContext(
        task_pk=42,
        attempt_number=1,
        execution_generation=1,
        runtime_env_profile=recovery.profile,
        runtime_env_hash=recovery.digest,
        runtime_env_plan_identity=recovery_identity.as_transport_dict(),
    )
    plans = [
        materialize_workflow_plan(
            workflows.order_fulfillment_recovery_showcase_workflow,
            invocation_args=(1, 0.0, stage),
            task_context=task_context,
        ).plan
        for stage in (
            workflows.WORKFLOW_RECOVERY_EARLY_STAGE,
            workflows.WORKFLOW_RECOVERY_MID_STAGE,
            workflows.WORKFLOW_RECOVERY_SUCCESS_STAGE,
        )
    ]

    assert len({plan.fingerprint for plan in plans}) == 1
    assert all(plan.retry_safe is True for plan in plans)
    assert all(plan.manifest["definition"]["container_image_digest"] is None for plan in plans)
    assert recovery_identity.retry_safe is True
    assert recovery_identity.retry_unsafe_paths == ()
    assert project_identity.retry_safe is False
    assert "spec.pip.0" in project_identity.retry_unsafe_paths
    assert any(path.startswith("spec.env_vars.") for path in project_identity.retry_unsafe_paths)


def test_recovery_workflow_fails_at_midpoint_after_upstream_work() -> None:
    batch = workflows.build_recovery_order_batch(
        1,
        0,
        workflows.WORKFLOW_RECOVERY_MID_STAGE,
    )
    validations = [
        workflows.validate_order_item(item) for item in workflows.select_validation_items(batch)
    ]
    customer = workflows.join_customer_context(
        [
            workflows.load_customer_profile(batch),
            workflows.load_customer_history(batch),
        ]
    )
    inventory = workflows.load_recovery_inventory_snapshot(batch)

    assert validations == [{"order_id": "showcase-order-0001", "item_id": 0, "valid": True}]
    assert customer["tier"] == "GOLD"
    assert inventory["available_units"] == {"0": 3}
    with pytest.raises(
        workflows.WorkflowRecoveryMidFixtureError,
        match=workflows.WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE,
    ):
        workflows.join_recovery_order_inputs([validations, customer, inventory])


def test_recovery_task_returns_explicit_success_for_attempt_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"status": "FULFILLED"}

    monkeypatch.setattr(tasks, "run_order_fulfillment_recovery_showcase_workflow", run)
    with durable_task_execution(42, attempt_number=3, execution_generation=3):
        result = tasks.order_fulfillment_recovery_showcase_task.func(1, 0)

    assert captured == {
        "args": (1, 0, workflows.WORKFLOW_RECOVERY_SUCCESS_STAGE),
        "kwargs": {"use_ray": True},
    }
    assert result == {
        "status": "FULFILLED",
        "recovery": {
            "scenario": "three-attempt-recovery",
            "attempt_number": 3,
            "outcome": "SUCCEEDED",
        },
    }


@pytest.mark.parametrize(
    ("attempt_number", "error_type", "message"),
    [
        (
            1,
            workflows.WorkflowRecoveryEarlyFixtureError,
            workflows.WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE,
        ),
        (
            2,
            workflows.WorkflowRecoveryMidFixtureError,
            workflows.WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE,
        ),
    ],
)
def test_recovery_task_normalizes_only_the_expected_ray_wrapped_failure(
    monkeypatch: pytest.MonkeyPatch,
    attempt_number: int,
    error_type: type[RuntimeError],
    message: str,
) -> None:
    wrapped = RayTaskError(
        "workflow_recovery",
        "fixture traceback",
        error_type(message),
        proctitle="ray::workflow_recovery",
        pid=123,
        ip="127.0.0.1",
    )
    monkeypatch.setattr(
        tasks,
        "run_order_fulfillment_recovery_showcase_workflow",
        _raise(wrapped),
    )

    with (
        durable_task_execution(
            42,
            attempt_number=attempt_number,
            execution_generation=attempt_number,
        ),
        pytest.raises(error_type) as caught,
    ):
        tasks.order_fulfillment_recovery_showcase_task.func(1, 0)

    assert type(caught.value) is error_type
    assert str(caught.value) == message
    serialized = json.loads(_serialize_error(caught.value))
    denylist = cast(list[str], testproject_settings.DJANGO_RAY["RETRY_EXCEPTION_DENYLIST"])
    assert serialized["exception_type"] not in denylist
    monkeypatch.setattr(retry_module, "get_settings", lambda: testproject_settings.DJANGO_RAY)
    decision = retry_module.should_retry(
        SimpleNamespace(attempt_number=attempt_number),
        exception_type=serialized["exception_type"],
    )
    assert decision.should_retry is True
    assert decision.next_attempt_at is not None


def test_recovery_task_preserves_unrelated_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = RuntimeError("unrelated workflow failure")
    monkeypatch.setattr(
        tasks,
        "run_order_fulfillment_recovery_showcase_workflow",
        _raise(unrelated),
    )

    with (
        durable_task_execution(42, attempt_number=2, execution_generation=2),
        pytest.raises(RuntimeError) as caught,
    ):
        tasks.order_fulfillment_recovery_showcase_task.func(1, 0)

    assert caught.value is unrelated


def test_recovery_task_requires_durable_attempt_context() -> None:
    with pytest.raises(RuntimeError, match="durable attempt identity"):
        tasks.order_fulfillment_recovery_showcase_task.func(1, 0)
