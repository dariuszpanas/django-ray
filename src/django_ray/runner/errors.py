"""Runner exceptions that callers use to classify backend outcomes."""

from __future__ import annotations

from enum import StrEnum


class RayJobRequestPreparationRejection(StrEnum):
    """Stable, secret-safe classifications for definite rq2 preparation failures."""

    INVALID_REQUEST = "invalid_request"
    RESOURCE_LIMIT = "resource_limit"
    CONFIGURATION = "configuration"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    INTEGRITY_MISMATCH = "integrity_mismatch"
    REGISTRY_MISMATCH = "registry_mismatch"
    BINDING_MISMATCH = "binding_mismatch"

    @property
    def requires_nonretryable_disposition(self) -> bool:
        """Return whether a deterministic rejection must suppress automatic retry."""
        return self is not RayJobRequestPreparationRejection.STORAGE_UNAVAILABLE


class RayJobRequestPreparationError(RuntimeError):
    """Reject rq2 before Ray submission without retaining request material."""

    def __init__(self, classification: RayJobRequestPreparationRejection) -> None:
        self.classification = classification
        super().__init__(f"Ray Job request preparation rejected: {classification.value}")

    @property
    def requires_nonretryable_disposition(self) -> bool:
        """Expose the fixed retry disposition without inspecting its cause."""
        return self.classification.requires_nonretryable_disposition


class RayJobSubmissionUncertainError(RuntimeError):
    """A Ray submission request may have been accepted before it failed."""

    def __init__(
        self,
        submission_id: str,
        detail: str,
        *,
        observed_submission_id: str | None = None,
    ) -> None:
        """Retain the exact identity that must be reconciled."""
        self.submission_id = submission_id
        self.observed_submission_id = observed_submission_id
        super().__init__(
            f"Ray Job submission {submission_id!r} has uncertain acceptance state: {detail}"
        )


__all__ = [
    "RayJobRequestPreparationError",
    "RayJobRequestPreparationRejection",
    "RayJobSubmissionUncertainError",
]
