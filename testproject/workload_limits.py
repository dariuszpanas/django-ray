"""Fixed, application-owned limits for the executable sample workloads."""

from __future__ import annotations

import inspect
import json
import math
from functools import wraps
from typing import Any

MAX_BODY_BYTES = 64 * 1024
MAX_STRING_CHARS = 2048
MAX_COLLECTION_ITEMS = 100
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 2048
MAX_FANOUT_ITEMS = 100
MAX_FANOUT_CONCURRENCY = 4
MAX_GRID_COMBINATIONS = 100


def integer(value: Any, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Sample integer exceeds its supported bounds")


def number(value: Any, minimum: float, maximum: float) -> None:
    if (
        type(value) not in (int, float)
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError("Sample number exceeds its supported bounds")


def validate_json(value: Any) -> None:
    """Bound traversal before serialization, including already constructed inputs."""
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if visited > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("Sample input structure exceeds its supported bounds")
        if item is None or type(item) is bool:
            continue
        if type(item) in (int, float):
            number(item, -1e12, 1e12)
        elif type(item) is str:
            if len(item) > MAX_STRING_CHARS:
                raise ValueError("Sample string exceeds its supported bounds")
        elif type(item) in (list, tuple, dict):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise ValueError("Sample collection exceeds its supported bounds")
            if isinstance(item, dict):
                for key, child in item.items():
                    if type(key) is not str or len(key) > 128:
                        raise ValueError("Sample object key exceeds its supported bounds")
                    pending.append((child, depth + 1))
            else:
                pending.extend((child, depth + 1) for child in item)
        else:
            raise ValueError("Sample inputs must contain JSON values")
    if len(json.dumps(value, allow_nan=False, ensure_ascii=True).encode()) > MAX_BODY_BYTES:
        raise ValueError("Sample input exceeds its supported byte limit")


def validate_arguments(name: str, values: dict[str, Any]) -> None:
    validate_json(values)
    integer_fields = {
        "a": (-(10**9), 10**9),
        "b": (-(10**9), 10**9),
        "iterations": (0, 2_000_000),
        "sleep_ms": (0, 10_000),
        "size_mb": (1, 32),
        "size_kb": (1, 64),
        "count": (1, 100),
        "start": (0, 1_000_000),
        "task_count": (1, 100),
        "task_duration_ms": (0, 100),
        "num_items": (1, MAX_FANOUT_ITEMS),
        "fast_items": (1, 100),
        "slow_items": (1, 100),
        "item_count": (1, 100),
        "epochs": (1, 100),
        "repeats": (2, 10),
        "chunk_id": (0, 10**9),
        "fail_until_attempt": (1, 10),
        "timeout_seconds": (1, 30),
        "checkpoint_interval": (1, 30),
        "failure_item": (0, 99),
    }
    for key, (minimum, maximum) in integer_fields.items():
        if (
            key in values
            and values[key] is not None
            and not (name == "matrix_multiply" and key in {"a", "b"})
        ):
            integer(values[key], minimum, maximum)
    for key in ("seconds", "duration_seconds"):
        if key in values:
            number(
                values[key],
                0,
                300 if name in {"slow_task", "async_slow_task", "long_running_job"} else 10,
            )
    for key in ("seconds_per_item", "fast_seconds", "slow_seconds"):
        if key in values:
            number(values[key], 0.01, 10)
    if "work_seconds" in values:
        number(values["work_seconds"], 0, 1)
    if "fast_items" in values and "slow_items" in values:
        if values["fast_items"] + values["slow_items"] > MAX_FANOUT_ITEMS:
            raise ValueError("Sample workflow exceeds its total item limit")
    if "n" in values:
        maximum = {"fibonacci": 10_000, "prime_check": 100_000_000}.get(name, 2_000_000)
        integer(values["n"], 0, maximum)
    if "case_sensitive" in values and type(values["case_sensitive"]) is not bool:
        raise ValueError("case_sensitive must be a boolean")
    if name == "stress_nested_compute":
        integer(values["depth"], 1, 6)
        integer(values["width"], 1, 10)
        if values["width"] ** values["depth"] > 100_000:
            raise ValueError("Sample recursive work exceeds its supported bounds")
    elif name == "stress_json_payload":
        integer(values["depth"], 0, 4)
    if name == "hyperparameter_search":
        grid = values["param_grid"]
        if type(grid) is not dict or not 1 <= len(grid) <= 8:
            raise ValueError("Sample parameter grid exceeds its supported bounds")
        combinations = 1
        for choices in grid.values():
            if type(choices) is not list or not choices:
                raise ValueError("Sample parameter grid requires nonempty lists")
            combinations *= len(choices)
            if combinations > MAX_GRID_COMBINATIONS:
                raise ValueError("Sample parameter grid exceeds its combination limit")
    if name == "matrix_multiply":
        for matrix in (values["a"], values["b"]):
            if type(matrix) is not list or len(matrix) > 32:
                raise ValueError("Sample matrix exceeds its supported bounds")
            for row in matrix:
                if type(row) is not list or len(row) > 32:
                    raise ValueError("Sample matrix exceeds its supported bounds")
                for value in row:
                    number(value, -1000, 1000)
    if name == "feature_engineering":
        for config in values.get("feature_configs") or []:
            if config.get("type") == "polynomial":
                integer(config.get("params", {}).get("degree", 2), 1, 4)


def validate_call(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    bound = inspect.signature(function).bind(*args, **kwargs)
    bound.apply_defaults()
    validate_arguments(function.__name__, bound.arguments)


def bounded_sample_task(function: Any) -> Any:
    """Repeat input validation before task work, including non-HTTP producers."""
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def async_checked(*args: Any, **kwargs: Any) -> Any:
            validate_call(function, args, kwargs)
            return await function(*args, **kwargs)

        return async_checked

    @wraps(function)
    def checked(*args: Any, **kwargs: Any) -> Any:
        validate_call(function, args, kwargs)
        return function(*args, **kwargs)

    return checked
