"""Runner exceptions that callers use to classify backend outcomes."""

from __future__ import annotations


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


__all__ = ["RayJobSubmissionUncertainError"]
