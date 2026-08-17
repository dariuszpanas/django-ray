"""Dependency-leaf contracts shared by workflow definitions and planning."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class WorkflowDefinitionKind(StrEnum):
    """Built-in workflow node kinds understood by the plan compiler."""

    STEP = "step"
    CHAIN = "chain"
    GROUP = "group"
    MAP = "map"


class PlannableWorkflowSignature(Protocol):
    """Minimal definition contract consumed by the plan compiler."""

    @property
    def _workflow_definition_kind(self) -> WorkflowDefinitionKind | None:
        """Return the built-in node kind, or ``None`` for unsupported extensions."""
        ...


__all__ = ["PlannableWorkflowSignature", "WorkflowDefinitionKind"]
