"""Base result store interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ResultTooLargeError(Exception):
    """Raised when a result exceeds the maximum allowed size."""


class BaseResultStore(ABC):
    """Abstract base class for result storage."""

    @abstractmethod
    def store(self, task_id: str, result: Any) -> str:
        """Store a task result.

        Args:
            task_id: The task execution ID.
            result: The result to store.

        Returns:
            A reference/pointer to the stored result.

        Raises:
            ResultTooLargeError: If the result exceeds size limits.
        """

    @abstractmethod
    def retrieve(self, reference: str) -> Any:
        """Retrieve a stored result.

        Args:
            reference: The reference returned by store().

        Returns:
            The stored result.

        Raises:
            KeyError: If the result is not found.
        """

    @abstractmethod
    def delete(self, reference: str) -> bool:
        """Delete a stored result.

        Args:
            reference: The reference returned by store().

        Returns:
            True if deleted, False if not found.
        """
