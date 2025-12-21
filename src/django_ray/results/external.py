"""External result storage (S3/GCS/MinIO).

Note: This is a Phase 4 feature and is currently a stub.
"""

from __future__ import annotations

from typing import Any

from django_ray.results.base import BaseResultStore


class ExternalResultStore(BaseResultStore):
    """Store results in external object storage.

    Supports S3, GCS, and MinIO backends.

    Note: This is a Phase 4 feature and is not yet implemented.
    """

    def __init__(self, backend: str = "s3") -> None:
        """Initialize the external result store.

        Args:
            backend: Storage backend ('s3', 'gcs', 'minio').
        """
        self.backend = backend
        # TODO: Initialize storage client

    def store(self, task_id: str, result: Any) -> str:
        """Store result in external storage."""
        raise NotImplementedError("External result store is not yet implemented")

    def retrieve(self, reference: str) -> Any:
        """Retrieve result from external storage."""
        raise NotImplementedError("External result store is not yet implemented")

    def delete(self, reference: str) -> bool:
        """Delete result from external storage."""
        raise NotImplementedError("External result store is not yet implemented")
