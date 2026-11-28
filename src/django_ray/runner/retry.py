"""Retry policy implementation for failed tasks."""

from __future__ import annotations

import builtins
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django_ray.conf.settings import get_settings

if TYPE_CHECKING:
    from django_ray.decorators import RayTaskContract
    from django_ray.models import RayTaskExecution

_MAX_BACKOFF_SECONDS = 3600


def resolve_task_contract(task_execution: object) -> RayTaskContract | None:
    """Resolve any ``@ray_task`` contract declared on a task execution's callable.

    Checks already-loaded modules in ``sys.modules`` first without importing
    unloaded modules, then falls back to the allowlisted task resolver for
    real task modules. Returns ``None`` cleanly when no contract is declared
    or the callable path cannot be resolved.
    """
    from django_ray.decorators import RayTaskContract, get_ray_task_contract

    direct = get_ray_task_contract(task_execution)
    if direct is not None:
        return direct
    cached = getattr(task_execution, "_ray_task_contract", None)
    if isinstance(cached, RayTaskContract):
        return cached

    callable_path = getattr(task_execution, "callable_path", None)
    if type(callable_path) is not str or "." not in callable_path:
        return None
    cleaned = callable_path.strip()
    if not cleaned or len(cleaned) > 512:
        return None

    parts = cleaned.split(".")
    if all(part.isidentifier() for part in parts):
        for split_idx in range(len(parts) - 1, 0, -1):
            module_name = ".".join(parts[:split_idx])
            module = sys.modules.get(module_name)
            if module is None:
                continue
            current: object = module
            for attr in parts[split_idx:]:
                namespace = getattr(current, "__dict__", None)
                if namespace is None or attr not in namespace:
                    current = None
                    break
                current = namespace[attr]
            if current is not None:
                return get_ray_task_contract(current)

    try:
        from django_ray.runtime.import_utils import resolve_task_callable

        resolved = resolve_task_callable(cleaned)
    except Exception:
        return None
    return get_ray_task_contract(resolved)


@dataclass
class RetryDecision:
    """Decision about whether and when to retry a task."""

    should_retry: bool
    next_attempt_at: datetime | None = None
    reason: str | None = None


def get_max_attempts() -> int:
    """Get the maximum number of task attempts."""
    settings = get_settings()
    return settings.get("MAX_TASK_ATTEMPTS", 3)


def get_base_backoff_seconds() -> int:
    """Get the base backoff duration in seconds."""
    settings = get_settings()
    return settings.get("RETRY_BACKOFF_SECONDS", 60)


def get_backoff_jitter_ratio() -> float:
    """Get the configured bounded backoff jitter ratio in [0.0, 1.0]."""
    settings = get_settings()
    raw = settings.get("RETRY_BACKOFF_JITTER_RATIO", 0.0)
    return min(1.0, max(0.0, float(raw)))


def calculate_backoff(
    attempt_number: int,
    *,
    base_backoff_seconds: int | None = None,
    jitter_ratio: float | None = None,
    random_value: Callable[[], float] | None = None,
) -> timedelta:
    """Calculate bounded exponential backoff with downward jitter for retry.

    Args:
        attempt_number: The current attempt number (1-based).
        base_backoff_seconds: Optional per-task override for base backoff seconds.
            Defaults to ``RETRY_BACKOFF_SECONDS`` from settings.
        jitter_ratio: Optional override for jitter ratio in [0.0, 1.0].
            Defaults to ``RETRY_BACKOFF_JITTER_RATIO`` from settings.
        random_value: Optional callable returning a float in [0.0, 1.0] for
            deterministic testing and seeded per-worker decorrelation.

    Returns:
        Backoff duration capped at 3600 seconds.
    """
    base = (
        get_base_backoff_seconds()
        if base_backoff_seconds is None
        else max(0, int(base_backoff_seconds))
    )
    effective_attempt = max(1, int(attempt_number))
    backoff_seconds = float(min(base * (2 ** (effective_attempt - 1)), _MAX_BACKOFF_SECONDS))
    effective_ratio = (
        get_backoff_jitter_ratio()
        if jitter_ratio is None
        else min(1.0, max(0.0, float(jitter_ratio)))
    )
    if backoff_seconds > 0.0 and effective_ratio > 0.0:
        sample = min(1.0, max(0.0, float((random_value or random.random)())))
        # Jitter downward so retries spread across [(1 - ratio) * backoff, backoff]
        # instead of clamping the upper half against the 3600-second ceiling.
        backoff_seconds = backoff_seconds * (1.0 - effective_ratio * sample)
    return timedelta(seconds=backoff_seconds)


def _normalize_exception_name(exception_name: str) -> set[str]:
    """Return exact comparable variants without flattening qualified module paths."""
    name = exception_name.strip()
    if not name:
        return set()

    variants = {name}
    if "." not in name:
        variants.add(f"builtins.{name}")
    return variants


def _resolve_loaded_exception_class(name: str) -> type[BaseException] | None:
    """Resolve an exception class from builtins or already-loaded modules only.

    Never imports unloaded modules or invokes module ``__getattr__`` hooks.
    """
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 256:
        return None

    if "." not in cleaned:
        if not cleaned.isidentifier():
            return None
        candidate = getattr(builtins, cleaned, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            return candidate
        return None

    parts = cleaned.split(".")
    if not all(part.isidentifier() for part in parts):
        return None

    # Match the longest already-loaded module prefix in sys.modules.
    for split_idx in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split_idx])
        module = sys.modules.get(module_name)
        if module is None:
            continue
        current: object = module
        for attr in parts[split_idx:]:
            namespace = getattr(current, "__dict__", None)
            if namespace is None or attr not in namespace:
                current = None
                break
            current = namespace[attr]
        if isinstance(current, type) and issubclass(current, BaseException):
            return current
    return None


def _exception_mro_names(exc_cls: type[BaseException]) -> tuple[set[str], set[str]]:
    """Collect qualified and short names across an exception's BaseException MRO."""
    qualified: set[str] = set()
    short: set[str] = set()
    for base in exc_cls.__mro__:
        if not issubclass(base, BaseException):
            continue
        short.add(base.__name__)
        qualified.add(f"{base.__module__}.{base.__name__}")
        qualified.add(f"{base.__module__}.{base.__qualname__}")
    return qualified, short


def _match_denylist_entry(
    exception_type: str | None,
    denylist: list[object],
    *,
    exception: BaseException | type[BaseException] | None = None,
) -> str | None:
    """Return the matched denylist entry, if any.

    Qualified dotted entries (e.g. ``myapp.errors.PermanentError``) match only
    exact module+class paths and subclasses of that class, never colliding on
    short class names from unrelated modules. Unqualified short entries (e.g.
    ``ValueError``) match builtins, matching short class names, and subclasses.
    """
    exc_cls: type[BaseException] | None = None
    if isinstance(exception, BaseException):
        exc_cls = type(exception)
    elif isinstance(exception, type) and issubclass(exception, BaseException):
        exc_cls = exception

    exc_name = exception_type.strip() if isinstance(exception_type, str) else ""
    if exc_cls is None and exc_name:
        exc_cls = _resolve_loaded_exception_class(exc_name)
    if not exc_name and exc_cls is not None:
        exc_name = f"{exc_cls.__module__}.{exc_cls.__qualname__}"
    if not exc_name and exc_cls is None:
        return None

    mro_qualified, mro_short = (
        _exception_mro_names(exc_cls) if exc_cls is not None else (set(), set())
    )
    exc_short = exc_name.rsplit(".", 1)[-1] if exc_name else ""
    exc_is_unqualified = "." not in exc_name

    for entry in denylist:
        if not isinstance(entry, str):
            continue
        clean_entry = entry.strip()
        if not clean_entry:
            continue

        if "." in clean_entry:
            # Qualified dotted denylist entry: exact path, builtin shorthand, or subclass.
            if exc_name == clean_entry:
                return entry
            if exc_is_unqualified and clean_entry == f"builtins.{exc_name}":
                return entry
            if exc_cls is not None:
                if clean_entry in mro_qualified:
                    return entry
                deny_cls = _resolve_loaded_exception_class(clean_entry)
                if deny_cls is not None and issubclass(exc_cls, deny_cls):
                    return entry
        else:
            # Unqualified short denylist entry: exact, builtin, short name, or subclass.
            if exc_name == clean_entry or exc_name == f"builtins.{clean_entry}":
                return entry
            if exc_short == clean_entry:
                return entry
            if exc_cls is not None:
                if clean_entry in mro_short:
                    return entry
                deny_cls = _resolve_loaded_exception_class(clean_entry)
                if deny_cls is not None and issubclass(exc_cls, deny_cls):
                    return entry

    return None


def should_retry(
    task_execution: RayTaskExecution,
    exception_type: str | None = None,
    *,
    exception: BaseException | type[BaseException] | None = None,
    random_value: Callable[[], float] | None = None,
    contract: RayTaskContract | None = None,
) -> RetryDecision:
    """Determine if a failed task should be retried.

    Per-task ``@ray_task(...)`` contracts override global ``MAX_TASK_ATTEMPTS``,
    ``RETRY_BACKOFF_SECONDS``, and ``retry_jitter=False`` when present.

    Args:
        task_execution: The failed task execution.
        exception_type: The type of exception that caused the failure.
        exception: Optional exception instance or class for exact subclass checks.
        random_value: Optional callable returning a float in [0.0, 1.0] for
            deterministic backoff jitter in tests.
        contract: Optional pre-resolved ``RayTaskContract`` override.

    Returns:
        RetryDecision with retry information.
    """
    resolved_contract = contract if contract is not None else resolve_task_contract(task_execution)
    max_attempts = (
        resolved_contract.max_attempts
        if resolved_contract is not None and resolved_contract.max_attempts is not None
        else get_max_attempts()
    )
    attempt_number: int = task_execution.attempt_number  # type: ignore[assignment]

    if attempt_number >= max_attempts:
        return RetryDecision(
            should_retry=False,
            reason=f"Max attempts ({max_attempts}) reached",
        )

    settings = get_settings()
    denylist = settings.get("RETRY_EXCEPTION_DENYLIST", [])

    matched_entry = (
        _match_denylist_entry(exception_type, denylist, exception=exception)
        if exception_type is not None or exception is not None
        else None
    )
    if matched_entry is not None:
        label = exception_type or (
            f"{type(exception).__module__}.{type(exception).__qualname__}"
            if isinstance(exception, BaseException)
            else f"{exception.__module__}.{exception.__qualname__}"
            if isinstance(exception, type)
            else "unknown"
        )
        return RetryDecision(
            should_retry=False,
            reason=f"Exception type '{label}' matched denylist entry '{matched_entry}'",
        )

    base_backoff_seconds = (
        resolved_contract.retry_backoff_seconds
        if resolved_contract is not None and resolved_contract.retry_backoff_seconds is not None
        else None
    )
    jitter_ratio = (
        0.0 if resolved_contract is not None and resolved_contract.retry_jitter is False else None
    )
    backoff = calculate_backoff(
        attempt_number,
        base_backoff_seconds=base_backoff_seconds,
        jitter_ratio=jitter_ratio,
        random_value=random_value,
    )
    next_attempt_at = datetime.now(UTC) + backoff

    return RetryDecision(
        should_retry=True,
        next_attempt_at=next_attempt_at,
    )
