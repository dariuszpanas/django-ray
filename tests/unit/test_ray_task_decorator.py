"""Unit tests for the declarative ``@ray_task`` decorator and ``RayTaskContract``."""

from __future__ import annotations

import ast
import math
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from django.tasks import task
from django.tasks.base import Task

from django_ray._result_tasks import _project_result_task
from django_ray.decorators import (
    RayTaskContract,
    RayTaskContractError,
    get_ray_task_contract,
    ray_task,
)
from django_ray.runtime.import_utils import resolve_task_callable


@ray_task
def _bare_ray_task(left: int, right: int) -> int:
    return left + right


@ray_task(
    num_cpus=2,
    num_gpus=0.5,
    memory=256 * 1024 * 1024,
    max_attempts=5,
    retry_backoff_seconds=30,
    retry_jitter=False,
    timeout_seconds=120,
    runtime_env_profile="ml-gpu",
    queue_name="ml",
    priority=25,
    takes_context=False,
    backend="default",
)
def _configured_ray_task(value: int) -> int:
    return value * 3


@ray_task(num_cpus=0.25, queue_name="default", takes_context=True)
async def _async_context_ray_task(context: Any, value: str) -> str:
    return f"{context.task_result.id}:{value}"


@task(queue_name="low-priority", priority=-10)
def _stacked_base_task(value: int) -> int:
    return value + 1


@task(queue_name="default")
def _plain_django_task(value: int) -> int:
    return value


def _direct_call_target(value: int) -> int:
    return value * 2


def _plain_function(value: int) -> int:
    return value


class TestRayTaskDecorator:
    """Verify Django 6 Task parity, contract introspection, and security compliance."""

    def test_bare_and_configured_tasks_are_genuine_django_tasks(self) -> None:
        assert isinstance(_bare_ray_task, Task)
        assert isinstance(_configured_ray_task, Task)
        assert isinstance(_async_context_ray_task, Task)
        assert _bare_ray_task.call(20, 22) == 42
        assert _configured_ray_task.call(7) == 21
        assert _configured_ray_task.queue_name == "ml"
        assert _configured_ray_task.priority == 25
        assert _configured_ray_task.backend == "default"
        assert _async_context_ray_task.takes_context is True

    def test_passes_wave1_resolve_task_callable_security_boundary(self, settings: Any) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        resolved_bare = resolve_task_callable(f"{__name__}._bare_ray_task")
        resolved_cfg = resolve_task_callable(f"{__name__}._configured_ray_task")

        assert resolved_bare is _bare_ray_task
        assert resolved_cfg is _configured_ray_task
        assert get_ray_task_contract(resolved_cfg) == get_ray_task_contract(_configured_ray_task)

    def test_contract_introspection_and_immutability(self) -> None:
        contract = get_ray_task_contract(_configured_ray_task)
        assert contract is not None
        assert contract.num_cpus == 2.0
        assert type(contract.num_cpus) is float
        assert contract.num_gpus == 0.5
        assert contract.memory == 256 * 1024 * 1024
        assert contract.max_attempts == 5
        assert contract.retry_backoff_seconds == 30
        assert contract.retry_jitter is False
        assert contract.timeout_seconds == 120
        assert contract.runtime_env_profile == "ml-gpu"
        assert contract.queue_name == "ml"
        assert contract.priority == 25
        assert contract.takes_context is False
        assert contract.backend == "default"

        assert contract.to_ray_options() == {
            "num_cpus": 2.0,
            "num_gpus": 0.5,
            "memory": 256 * 1024 * 1024,
        }
        assert contract.to_dict()["runtime_env_profile"] == "ml-gpu"

        # Introspection works from both the Task object and its underlying function.
        assert get_ray_task_contract(_configured_ray_task.func) == contract

        # Frozen immutability.
        mutable_contract: Any = contract
        with pytest.raises(FrozenInstanceError):
            mutable_contract.num_cpus = 4.0

    def test_bare_contract_defaults_and_empty_ray_options(self) -> None:
        contract = get_ray_task_contract(_bare_ray_task)
        assert contract is not None
        assert contract.num_cpus is None
        assert contract.num_gpus is None
        assert contract.memory is None
        assert contract.max_attempts is None
        assert contract.retry_backoff_seconds is None
        assert contract.retry_jitter is True
        assert contract.timeout_seconds is None
        assert contract.runtime_env_profile is None
        assert contract.queue_name == "default"
        assert contract.priority == 0
        assert contract.takes_context is False
        assert contract.backend == "default"
        assert contract.to_ray_options() == {}

    def test_using_overrides_reflected_in_get_ray_task_contract(self) -> None:
        overridden = _configured_ray_task.using(queue_name="high-priority", priority=80)
        assert isinstance(overridden, Task)

        updated_contract = get_ray_task_contract(overridden)
        original_contract = get_ray_task_contract(_configured_ray_task)
        assert updated_contract is not None
        assert original_contract is not None

        assert updated_contract.queue_name == "high-priority"
        assert updated_contract.priority == 80
        assert updated_contract.num_cpus == 2.0
        assert updated_contract.num_gpus == 0.5
        assert updated_contract.timeout_seconds == 120
        # Original task contract stays immutable.
        assert original_contract.queue_name == "ml"
        assert original_contract.priority == 25

    def test_stacking_over_existing_django_task_preserves_defaults(self) -> None:
        stacked = ray_task(num_cpus=1.5, timeout_seconds=45)(_stacked_base_task)
        assert isinstance(stacked, Task)
        assert stacked.queue_name == "low-priority"
        assert stacked.priority == -10

        contract = get_ray_task_contract(stacked)
        assert contract is not None
        assert contract.num_cpus == 1.5
        assert contract.timeout_seconds == 45
        assert contract.queue_name == "low-priority"
        assert contract.priority == -10

    def test_direct_call_decorator_syntax_with_options(self) -> None:
        wrapped = ray_task(_direct_call_target, num_cpus=0.5, queue_name="ml")
        assert isinstance(wrapped, Task)
        assert wrapped.call(6) == 12
        contract = get_ray_task_contract(wrapped)
        assert contract is not None
        assert contract.num_cpus == 0.5
        assert contract.queue_name == "ml"

    def test_plain_task_function_and_non_callable_return_none(self) -> None:
        assert get_ray_task_contract(_plain_django_task) is None
        assert get_ray_task_contract(_plain_function) is None
        assert get_ray_task_contract(None) is None
        assert get_ray_task_contract(123) is None

    def test_rejects_non_callable_and_read_only_result_task(self) -> None:
        bad_target: Any = "not-a-callable"
        with pytest.raises(TypeError, match="requires a callable"):
            ray_task(bad_target)

        readonly = _project_result_task(
            alias="default",
            callable_path="myapp.tasks.archived",
            priority=0,
            queue_name="default",
            run_after=None,
        )
        with pytest.raises(TypeError, match="read-only"):
            ray_task(num_cpus=1)(readonly)

    @pytest.mark.django_db
    def test_enqueue_persists_durable_execution_via_ray_backend(self) -> None:
        result = _bare_ray_task.enqueue(10, 5)
        assert result.id
        assert result.args == [10, 5]

    def test_decoupled_from_dormant_workflow_concurrency_module(self) -> None:
        decorators_path = (
            Path(__file__).resolve().parents[2] / "src" / "django_ray" / "decorators.py"
        )
        tree = ast.parse(decorators_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "concurrency" not in alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert "concurrency" not in node.module


class TestRayTaskContractValidation:
    """Verify strict eager validation rejects bad resources, retries, and timeouts."""

    @pytest.mark.parametrize(
        ("field_name", "bad_value"),
        [
            ("num_cpus", -0.5),
            ("num_cpus", -1),
            ("num_cpus", math.nan),
            ("num_cpus", math.inf),
            ("num_cpus", -math.inf),
            ("num_cpus", True),
            ("num_cpus", "2"),
            ("num_gpus", -1),
            ("num_gpus", math.nan),
            ("num_gpus", math.inf),
            ("num_gpus", False),
            ("num_gpus", "1"),
            ("memory", 0),
            ("memory", -1024),
            ("memory", True),
            ("memory", 1024.5),
            ("memory", math.nan),
            ("max_attempts", 0),
            ("max_attempts", -1),
            ("max_attempts", 101),
            ("max_attempts", True),
            ("max_attempts", 2.5),
            ("retry_backoff_seconds", -1),
            ("retry_backoff_seconds", 3601),
            ("retry_backoff_seconds", False),
            ("retry_backoff_seconds", 1.5),
            ("retry_jitter", 1),
            ("retry_jitter", "true"),
            ("timeout_seconds", 0),
            ("timeout_seconds", -10),
            ("timeout_seconds", True),
            ("timeout_seconds", 5.5),
            ("runtime_env_profile", ""),
            ("runtime_env_profile", " bad space"),
            ("runtime_env_profile", "../escape"),
            ("runtime_env_profile", "a" * 101),
            ("runtime_env_profile", 123),
            ("queue_name", ""),
            ("queue_name", " default "),
            ("queue_name", "q" * 101),
            ("priority", -101),
            ("priority", 101),
            ("priority", True),
            ("priority", 1.0),
            ("takes_context", 1),
            ("backend", ""),
            ("backend", " default"),
        ],
    )
    def test_rejects_invalid_contract_parameters_eagerly(
        self,
        field_name: str,
        bad_value: Any,
    ) -> None:
        with pytest.raises(RayTaskContractError):
            RayTaskContract(**{field_name: bad_value})

        # Decorator factory must fail eagerly BEFORE receiving any target function.
        with pytest.raises(RayTaskContractError):
            ray_task(**{field_name: bad_value})

    def test_accepts_zero_cpus_and_zero_gpus_and_zero_backoff(self) -> None:
        contract = RayTaskContract(num_cpus=0, num_gpus=0, retry_backoff_seconds=0)
        assert contract.num_cpus == 0.0
        assert contract.num_gpus == 0.0
        assert contract.retry_backoff_seconds == 0
        assert contract.to_ray_options() == {"num_cpus": 0.0, "num_gpus": 0.0}
