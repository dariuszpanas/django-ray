"""Focused coverage tests for runtime, lifecycle, models, and result storage."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from django_ray import lifecycle
from django_ray.models import TaskAttempt, TaskState
from django_ray.result_storage import is_valid_result_reference
from django_ray.runtime import distributed


def _identity(value: object) -> object:
    return value


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"num_cpus": True}, "num_cpus"),
        ({"max_concurrency": True}, "max_concurrency"),
    ],
)
def test_parallel_map_rejects_boolean_resource_values(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        distributed.parallel_map(_identity, [], **kwargs)  # type: ignore[arg-type]


def test_parallel_map_rejects_non_callable() -> None:
    with pytest.raises(TypeError, match="func must be callable"):
        distributed.parallel_map(object(), [])  # type: ignore[arg-type]


def test_cached_remote_rejects_unknown_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace())

    with pytest.raises(ValueError, match="unknown distributed helper"):
        distributed._get_cached_remote("unknown")


@pytest.mark.parametrize(
    "task",
    [
        (_identity, [], {}),
        (_identity, (), []),
    ],
)
def test_scatter_gather_rejects_invalid_task_arguments(task: tuple[object, object, object]) -> None:
    with pytest.raises(TypeError, match=r"tasks\[0\]\[[12]\]"):
        distributed.scatter_gather([task])  # type: ignore[list-item]


@pytest.mark.django_db
def test_retry_task_returns_none_for_missing_execution() -> None:
    assert lifecycle.retry_task(999_999) is None


def test_task_attempt_string_representation_includes_execution_and_state() -> None:
    attempt = TaskAttempt(execution_id=17, attempt_number=3, state=TaskState.FAILED)

    assert str(attempt) == "17 attempt 3 (FAILED)"


def test_result_reference_validation_rejects_invalid_url_parse() -> None:
    assert is_valid_result_reference("s3://[::1/payload?bytes=0") is False


def test_result_reference_validation_rejects_absolute_filesystem_path() -> None:
    reference = (
        "resultfs://sha256/"
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        "?rel=payload.json&bytes=0"
    )
    assert is_valid_result_reference(reference) is False


def test_result_reference_validation_rejects_non_numeric_byte_count() -> None:
    digest = "a" * 64
    reference = f"s3://bucket/aa/aa/{digest}.json?bytes=not-a-number"
    assert is_valid_result_reference(reference) is False
