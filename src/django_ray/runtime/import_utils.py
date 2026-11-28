"""Utilities for importing task and workflow callables by path."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django.tasks.base import Task

_DEFAULT_EXCLUDED_APP_PREFIXES = frozenset({"django", "django_ray"})
_active_allowed_callable_modules: ContextVar[tuple[str, ...] | None] = ContextVar(
    "django_ray_active_allowed_callable_modules",
    default=None,
)


def _is_valid_dotted_module(module_path: str) -> bool:
    if type(module_path) is not str or not module_path:
        return False
    parts = module_path.split(".")
    return all(part.isidentifier() for part in parts)


def _split_dotted_path(dotted_path: str) -> tuple[str, str]:
    if type(dotted_path) is not str or "." not in dotted_path:
        raise ImportError(f"Invalid callable path '{dotted_path}': must contain at least one dot")
    module_path, callable_name = dotted_path.rsplit(".", 1)
    if not _is_valid_dotted_module(module_path) or not callable_name.isidentifier():
        raise ImportError(
            f"Invalid callable path '{dotted_path}': must be a valid dotted Python identifier"
        )
    return module_path, callable_name


def _normalize_allowed_modules(allowed_modules: Sequence[str]) -> tuple[str, ...]:
    if isinstance(allowed_modules, (str, bytes)) or not isinstance(allowed_modules, Sequence):
        raise ValueError("ALLOWED_CALLABLE_MODULES must be a sequence of dotted module prefixes")
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in allowed_modules:
        if type(entry) is not str or not _is_valid_dotted_module(entry):
            raise ValueError(
                "ALLOWED_CALLABLE_MODULES entries must be valid dotted Python module prefixes"
            )
        if entry not in seen:
            seen.add(entry)
            normalized.append(entry)
    return tuple(normalized)


def _configured_allowed_callable_modules() -> tuple[str, ...] | None:
    try:
        from django.conf import settings as django_settings
    except ImportError:  # pragma: no cover - Django is always installed
        return None
    if not django_settings.configured:
        return None
    config = getattr(django_settings, "DJANGO_RAY", None)
    if not isinstance(config, Mapping):
        return None
    configured = config.get("ALLOWED_CALLABLE_MODULES")
    if configured is None:
        return None
    return _normalize_allowed_modules(configured)


def _default_workflow_allowed_modules() -> tuple[str, ...]:
    """Derive non-framework user app module prefixes from INSTALLED_APPS."""
    try:
        from django.apps import apps
        from django.conf import settings as django_settings
    except ImportError:  # pragma: no cover - Django is always installed
        return ()
    if not django_settings.configured:
        return ()

    candidates: list[str] = []
    if apps.ready:
        for app_config in apps.get_app_configs():
            name = getattr(app_config, "name", None)
            if isinstance(name, str) and name:
                candidates.append(name)
    else:
        for raw_entry in getattr(django_settings, "INSTALLED_APPS", ()) or ():
            if not isinstance(raw_entry, str) or not raw_entry:
                continue
            prefix = raw_entry.split(".apps.", 1)[0]
            candidates.append(prefix)

    allowed: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not _is_valid_dotted_module(candidate):
            continue
        top_level = candidate.split(".", 1)[0]
        if top_level in _DEFAULT_EXCLUDED_APP_PREFIXES:
            continue
        if candidate not in seen:
            seen.add(candidate)
            allowed.append(candidate)
    return tuple(allowed)


@contextmanager
def active_allowed_callable_modules(
    allowed_modules: Sequence[str] | None,
) -> Generator[tuple[str, ...] | None, None, None]:
    """Bind an inherited callable allowlist for nested non-Django workflow steps."""
    normalized = (
        _normalize_allowed_modules(allowed_modules) if allowed_modules is not None else None
    )
    token = _active_allowed_callable_modules.set(normalized)
    try:
        yield normalized
    finally:
        _active_allowed_callable_modules.reset(token)


def get_driver_allowed_callable_modules() -> tuple[str, ...]:
    """Return the effective callable module allowlist for the active driver.

    Prefers explicit ``DJANGO_RAY['ALLOWED_CALLABLE_MODULES']`` when configured;
    otherwise derives non-framework user application prefixes from
    ``INSTALLED_APPS``. When Django settings are not configured, falls back to
    an allowlist propagated via :func:`active_allowed_callable_modules`, and
    otherwise returns an empty tuple so callers fail closed unless an explicit
    allowlist is passed.
    """
    try:
        from django.conf import settings as django_settings

        settings_configured = bool(django_settings.configured)
    except ImportError:  # pragma: no cover - Django is always installed
        settings_configured = False
    if not settings_configured:
        inherited = _active_allowed_callable_modules.get()
        if inherited is not None:
            return inherited
        return ()
    configured = _configured_allowed_callable_modules()
    if configured is not None:
        return configured
    return _default_workflow_allowed_modules()


def _module_matches_allowlist(module_path: str, allowed_prefixes: Sequence[str]) -> bool:
    return any(
        module_path == prefix or module_path.startswith(f"{prefix}.") for prefix in allowed_prefixes
    )


def _import_dotted_attribute(module_path: str, callable_name: str) -> Any:
    try:
        module = import_module(module_path)
    except ImportError as e:
        raise ImportError(f"Could not import module '{module_path}': {e}") from e

    try:
        return getattr(module, callable_name)
    except AttributeError as e:
        raise AttributeError(f"Module '{module_path}' has no attribute '{callable_name}'") from e


def resolve_task_callable(
    dotted_path: str,
    *,
    allowed_modules: Sequence[str] | None = None,
) -> Task:
    """Resolve a Django ``Task`` object by dotted path with boundary checks.

    Enforces ``ALLOWED_CALLABLE_MODULES`` (defaulting to non-framework user
    ``INSTALLED_APPS`` modules) *before* importing ``module_path``, requires the
    resolved object to be a genuine ``django.tasks.base.Task`` instance, and
    never unwraps ``functools.partial``. When Django settings are unconfigured,
    callers must pass ``allowed_modules`` explicitly or resolution fails closed.
    """
    from django.tasks.base import Task

    module_path, callable_name = _split_dotted_path(dotted_path)
    effective_allowed = (
        _normalize_allowed_modules(allowed_modules)
        if allowed_modules is not None
        else get_driver_allowed_callable_modules()
    )
    if not _module_matches_allowlist(module_path, effective_allowed):
        raise ImportError(
            f"Callable module '{module_path}' is not permitted; "
            "configure its module prefix in DJANGO_RAY['ALLOWED_CALLABLE_MODULES'] "
            "or add the module's app to INSTALLED_APPS"
        )

    resolved = _import_dotted_attribute(module_path, callable_name)
    if not isinstance(resolved, Task):
        raise TypeError(
            f"'{dotted_path}' is not a Django Task; top-level task callables must be "
            "decorated with @task"
        )
    return resolved


def resolve_workflow_callable(
    dotted_path: str,
    *,
    allowed_modules: Sequence[str] | None = None,
) -> Callable[..., Any]:
    """Resolve a workflow step or fold callable after pre-import allowlist checks.

    Enforces ``ALLOWED_CALLABLE_MODULES`` (defaulting to non-framework user
    ``INSTALLED_APPS`` modules) *before* importing ``module_path``. Unwraps only
    genuine ``django.tasks.base.Task`` wrappers and never ``functools.partial``.
    """
    from django.tasks.base import Task

    module_path, callable_name = _split_dotted_path(dotted_path)
    effective_allowed = (
        _normalize_allowed_modules(allowed_modules)
        if allowed_modules is not None
        else get_driver_allowed_callable_modules()
    )

    if not _module_matches_allowlist(module_path, effective_allowed):
        raise ImportError(
            f"Workflow callable module '{module_path}' is not permitted; "
            "configure its module prefix in DJANGO_RAY['ALLOWED_CALLABLE_MODULES'] "
            "or add the module's app to INSTALLED_APPS"
        )

    resolved = import_callable(dotted_path)
    if isinstance(resolved, Task):
        resolved = resolved.func
    if not callable(resolved):
        raise TypeError(f"'{dotted_path}' is not callable")
    return resolved


def import_callable(dotted_path: str) -> Callable[..., Any]:
    """Import a callable from a dotted path.

    Args:
        dotted_path: Full dotted path to the callable (e.g., 'myapp.tasks.my_task').

    Returns:
        The imported callable.

    Raises:
        ImportError: If the module cannot be imported.
        AttributeError: If the callable is not found in the module.
        TypeError: If the imported object is not callable.

    Note:
        If the imported object is a Django 6 Task object (decorated with @task),
        this function returns the underlying function via the .func attribute.
    """
    if "." not in dotted_path:
        raise ImportError(f"Invalid callable path '{dotted_path}': must contain at least one dot")

    module_path, callable_name = dotted_path.rsplit(".", 1)

    try:
        module = import_module(module_path)
    except ImportError as e:
        raise ImportError(f"Could not import module '{module_path}': {e}") from e

    try:
        callable_obj = getattr(module, callable_name)
    except AttributeError as e:
        raise AttributeError(f"Module '{module_path}' has no attribute '{callable_name}'") from e

    # Handle Django 6 Task objects - extract the underlying function without
    # unwrapping arbitrary callables such as functools.partial.
    from django.tasks.base import Task

    if isinstance(callable_obj, Task):
        callable_obj = callable_obj.func

    if not callable(callable_obj):
        raise TypeError(f"'{dotted_path}' is not callable")

    return callable_obj
