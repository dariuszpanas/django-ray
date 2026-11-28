"""Leaf workflow structural protocols and executor-provider registry.

Allows ``django_ray.workflow.strategy`` to type-check workflow signatures and
resolve workflow executors without importing ``django_ray.workflows``, breaking
the mutual dependency between ``workflows`` and ``workflow.strategy``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from django_ray.workflow.contracts import WorkflowDefinitionKind


@runtime_checkable
class WorkflowSignatureProtocol(Protocol):
    """Minimal structural contract satisfied by all workflow signatures."""

    @property
    def _workflow_definition_kind(self) -> WorkflowDefinitionKind: ...

    def _submit(
        self,
        executor: Any,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> Any: ...


@runtime_checkable
class StepSignatureProtocol(WorkflowSignatureProtocol, Protocol):
    """Structural contract for ``Step`` workflow leaf signatures."""

    callable_path: str
    bootstrap_django: bool
    bound_args: tuple[Any, ...]
    bound_kwargs: Mapping[str, Any]


@runtime_checkable
class ChainSignatureProtocol(WorkflowSignatureProtocol, Protocol):
    """Structural contract for sequential ``Chain`` workflow signatures."""

    signatures: tuple[WorkflowSignatureProtocol, ...]


@runtime_checkable
class GroupSignatureProtocol(WorkflowSignatureProtocol, Protocol):
    """Structural contract for parallel ``Group`` workflow signatures."""

    signatures: tuple[WorkflowSignatureProtocol, ...]


_get_executor_provider: Callable[[bool | None], Any] | None = None
_create_local_executor_provider: Callable[[], Any] | None = None
_is_local_executor_provider: Callable[[object], bool] | None = None


def register_workflow_executor_providers(
    *,
    get_executor: Callable[[bool | None], Any],
    create_local_executor: Callable[[], Any],
    is_local_executor: Callable[[object], bool],
) -> None:
    """Register runtime executor callbacks owned by ``django_ray.workflows``."""
    global _get_executor_provider
    global _create_local_executor_provider
    global _is_local_executor_provider
    _get_executor_provider = get_executor
    _create_local_executor_provider = create_local_executor
    _is_local_executor_provider = is_local_executor


def get_workflow_executor(use_ray: bool | None) -> Any:
    """Resolve an active workflow executor through the registered provider."""
    if _get_executor_provider is None:
        raise RuntimeError(
            "Workflow executor provider is not registered; import django_ray.workflows first"
        )
    return _get_executor_provider(use_ray)


def create_local_workflow_executor() -> Any:
    """Construct a local workflow executor through the registered provider."""
    if _create_local_executor_provider is None:
        raise RuntimeError(
            "Local workflow executor provider is not registered; import django_ray.workflows first"
        )
    return _create_local_executor_provider()


def is_local_workflow_executor(executor: object) -> bool:
    """Return whether ``executor`` is a local workflow executor instance."""
    if _is_local_executor_provider is None:
        raise RuntimeError(
            "Local workflow executor predicate is not registered; import django_ray.workflows first"
        )
    return _is_local_executor_provider(executor)


__all__ = [
    "ChainSignatureProtocol",
    "GroupSignatureProtocol",
    "StepSignatureProtocol",
    "WorkflowSignatureProtocol",
    "create_local_workflow_executor",
    "get_workflow_executor",
    "is_local_workflow_executor",
    "register_workflow_executor_providers",
]
