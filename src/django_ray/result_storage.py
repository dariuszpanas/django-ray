"""Result storage backends for oversized task payloads."""

from __future__ import annotations

import hashlib
import importlib
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from django_ray.conf.settings import get_settings


class ResultStorageError(RuntimeError):
    """Raised when result storage operations fail."""


def is_valid_result_reference(reference: str) -> bool:
    """Validate the shape of a result reference without loading its backend."""
    if not isinstance(reference, str) or not reference or len(reference) > 500:
        return False

    try:
        parsed = urlparse(reference)
    except ValueError:
        return False
    if parsed.scheme in {"oversize", "resultfs"}:
        if parsed.netloc != "sha256" or not re.fullmatch(r"/[0-9a-f]{64}", parsed.path):
            return False
        if parsed.scheme == "resultfs":
            relative_values = parse_qs(parsed.query).get("rel")
            if not relative_values:
                return False
            relative_path = Path(relative_values[0])
            if (
                not relative_path.parts
                or relative_path.is_absolute()
                or ".." in relative_path.parts
            ):
                return False
    elif parsed.scheme in {"s3", "gs"}:
        object_key = parsed.path.lstrip("/")
        if not parsed.netloc or not object_key or ".." in Path(object_key).parts:
            return False
    else:
        return False

    byte_values = parse_qs(parsed.query).get("bytes")
    if not byte_values:
        return False
    try:
        return int(byte_values[0]) >= 0
    except ValueError:
        return False


class PayloadStorageBackend(Protocol):
    """Internal storage contract for durable serialized JSON payloads."""

    def store_payload(self, *, serialized_payload: str) -> str:
        """Persist a serialized payload and return a reference string."""

    def load(self, *, reference: str) -> str | None:
        """Load a serialized payload from a reference if supported."""

    def delete(self, *, reference: str) -> None:
        """Delete a serialized payload when the backend supports cleanup."""


class ResultStorageBackend(PayloadStorageBackend, Protocol):
    """Backward-compatible result storage contract."""

    def store(self, *, serialized_result: str) -> str:
        """Persist serialized result and return a reference string."""

    def load(self, *, reference: str) -> str | None:
        """Load serialized result from a reference if supported."""


class DigestResultStorage:
    """Digest-only reference backend (no external persistence)."""

    def store(self, *, serialized_result: str) -> str:
        return self.store_payload(serialized_payload=serialized_result)

    def store_payload(self, *, serialized_payload: str) -> str:
        payload = serialized_payload.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        return f"oversize://sha256/{digest}?bytes={len(payload)}"

    def load(self, *, reference: str) -> str | None:  # noqa: ARG002
        return None

    def delete(self, *, reference: str) -> None:  # noqa: ARG002
        return None


def _build_digest_and_bytes(serialized_result: str) -> tuple[str, int]:
    payload = serialized_result.encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _build_object_key(prefix: str, digest: str) -> str:
    clean_prefix = prefix.strip("/")
    suffix = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
    if not clean_prefix:
        return suffix
    return f"{clean_prefix}/{suffix}"


class FilesystemResultStorage:
    """Filesystem-backed result storage for oversized payloads."""

    def __init__(self, root_path: str | Path) -> None:
        self.root_path = Path(root_path)

    def store(self, *, serialized_result: str) -> str:
        return self.store_payload(serialized_payload=serialized_result)

    def store_payload(self, *, serialized_payload: str) -> str:
        digest, payload_size = _build_digest_and_bytes(serialized_payload)
        relative_path = Path(digest[:2]) / digest[2:4] / f"{digest}.json"
        full_path = self.root_path / relative_path
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            if not full_path.exists():
                full_path.write_text(serialized_payload, encoding="utf-8")
        except OSError as e:
            raise ResultStorageError(f"Failed to persist result to filesystem: {e}") from e

        return f"resultfs://sha256/{digest}?rel={relative_path.as_posix()}&bytes={payload_size}"

    def load(self, *, reference: str) -> str | None:
        relative_path = self._relative_path_from_reference(reference)
        full_path = self.root_path / relative_path
        if not full_path.exists():
            raise ResultStorageError(f"Result payload not found for reference: {reference}")
        try:
            return full_path.read_text(encoding="utf-8")
        except OSError as e:
            raise ResultStorageError(
                f"Failed to read result payload for reference: {reference}"
            ) from e

    def delete(self, *, reference: str) -> None:
        relative_path = self._relative_path_from_reference(reference)
        full_path = self.root_path / relative_path
        try:
            full_path.unlink(missing_ok=True)
        except OSError as e:
            raise ResultStorageError(
                f"Failed to delete filesystem payload for reference: {reference}"
            ) from e

    def _relative_path_from_reference(self, reference: str) -> Path:
        parsed = urlparse(reference)
        if parsed.scheme != "resultfs" or parsed.netloc != "sha256":
            raise ResultStorageError(f"Unsupported filesystem result reference: {reference}")

        relative_values = parse_qs(parsed.query).get("rel")
        if not relative_values:
            raise ResultStorageError(f"Missing rel query parameter in reference: {reference}")

        relative_path = Path(relative_values[0])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ResultStorageError(f"Unsafe relative path in reference: {reference}")

        return relative_path


class S3ResultStorage:
    """S3-backed result storage for oversized payloads."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "django-ray/results",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix
        if client is not None:
            self.client = client
            return
        try:
            boto3_module = importlib.import_module("boto3")
        except ImportError as e:
            raise ResultStorageError(
                "S3 result storage requires boto3. Install with: pip install boto3"
            ) from e
        self.client = boto3_module.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
        )

    def store(self, *, serialized_result: str) -> str:
        return self.store_payload(serialized_payload=serialized_result)

    def store_payload(self, *, serialized_payload: str) -> str:
        digest, payload_size = _build_digest_and_bytes(serialized_payload)
        key = _build_object_key(self.prefix, digest)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=serialized_payload.encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as e:
            raise ResultStorageError(f"Failed to persist result to S3: {e}") from e
        return f"s3://{self.bucket}/{key}?bytes={payload_size}"

    def load(self, *, reference: str) -> str | None:
        bucket, key = self._parse_reference(reference)
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            body = response["Body"].read()
        except Exception as e:
            raise ResultStorageError(f"Failed to load result from S3: {e}") from e
        if isinstance(body, bytes):
            return body.decode("utf-8")
        if isinstance(body, str):
            return body
        raise ResultStorageError("Unexpected S3 response body type")

    def delete(self, *, reference: str) -> None:
        bucket, key = self._parse_reference(reference)
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
        except Exception as e:
            raise ResultStorageError(f"Failed to delete payload from S3: {e}") from e

    def _parse_reference(self, reference: str) -> tuple[str, str]:
        parsed = urlparse(reference)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
            raise ResultStorageError(f"Unsupported S3 result reference: {reference}")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if ".." in Path(key).parts:
            raise ResultStorageError(f"Unsafe S3 key in reference: {reference}")
        return bucket, key


class GCSResultStorage:
    """GCS-backed result storage for oversized payloads."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "django-ray/results",
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix
        if client is not None:
            self.client = client
            return
        try:
            storage_module = importlib.import_module("google.cloud.storage")
        except ImportError as e:
            raise ResultStorageError(
                "GCS result storage requires google-cloud-storage. "
                "Install with: pip install google-cloud-storage"
            ) from e
        self.client = storage_module.Client()

    def store(self, *, serialized_result: str) -> str:
        return self.store_payload(serialized_payload=serialized_result)

    def store_payload(self, *, serialized_payload: str) -> str:
        digest, payload_size = _build_digest_and_bytes(serialized_payload)
        key = _build_object_key(self.prefix, digest)
        try:
            blob = self.client.bucket(self.bucket).blob(key)
            blob.upload_from_string(serialized_payload, content_type="application/json")
        except Exception as e:
            raise ResultStorageError(f"Failed to persist result to GCS: {e}") from e
        return f"gs://{self.bucket}/{key}?bytes={payload_size}"

    def load(self, *, reference: str) -> str | None:
        bucket, key = self._parse_reference(reference)
        try:
            blob = self.client.bucket(bucket).blob(key)
            data = blob.download_as_bytes()
        except Exception as e:
            raise ResultStorageError(f"Failed to load result from GCS: {e}") from e
        return data.decode("utf-8")

    def delete(self, *, reference: str) -> None:
        bucket, key = self._parse_reference(reference)
        try:
            self.client.bucket(bucket).blob(key).delete()
        except Exception as e:
            raise ResultStorageError(f"Failed to delete payload from GCS: {e}") from e

    def _parse_reference(self, reference: str) -> tuple[str, str]:
        parsed = urlparse(reference)
        if parsed.scheme != "gs" or not parsed.netloc or not parsed.path:
            raise ResultStorageError(f"Unsupported GCS result reference: {reference}")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if ".." in Path(key).parts:
            raise ResultStorageError(f"Unsafe GCS key in reference: {reference}")
        return bucket, key


def get_result_storage_backend_for_reference(
    reference: str,
    config: dict[str, Any] | None = None,
) -> ResultStorageBackend:
    """Resolve a result storage backend from a stored reference string."""
    if config is None:
        config = get_settings()

    parsed = urlparse(reference)
    if parsed.scheme == "oversize":
        return DigestResultStorage()
    if parsed.scheme == "resultfs":
        root_path = config.get("RESULT_STORAGE_FILESYSTEM_PATH")
        if not root_path:
            raise ResultStorageError(
                "RESULT_STORAGE_FILESYSTEM_PATH is required to load filesystem result references"
            )
        return FilesystemResultStorage(root_path)
    if parsed.scheme == "s3":
        if not parsed.netloc:
            raise ResultStorageError(f"Unsupported S3 result reference: {reference}")
        return S3ResultStorage(
            bucket=parsed.netloc,
            endpoint_url=(
                str(config["RESULT_STORAGE_S3_ENDPOINT_URL"])
                if config.get("RESULT_STORAGE_S3_ENDPOINT_URL")
                else None
            ),
            region_name=(
                str(config["RESULT_STORAGE_S3_REGION"])
                if config.get("RESULT_STORAGE_S3_REGION")
                else None
            ),
        )
    if parsed.scheme == "gs":
        if not parsed.netloc:
            raise ResultStorageError(f"Unsupported GCS result reference: {reference}")
        return GCSResultStorage(bucket=parsed.netloc)

    raise ResultStorageError(f"Unsupported result reference scheme: {parsed.scheme or reference}")


def load_result_reference(
    reference: str,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Load serialized result data from a stored reference string."""
    backend = get_result_storage_backend_for_reference(reference, config)
    return backend.load(reference=reference)


def get_result_storage_backend(config: dict[str, Any] | None = None) -> ResultStorageBackend:
    """Resolve the configured result storage backend."""
    if config is None:
        config = get_settings()

    backend = str(config.get("RESULT_STORAGE_BACKEND", "digest")).lower()
    if backend == "digest":
        return DigestResultStorage()
    if backend == "filesystem":
        root_path = config.get("RESULT_STORAGE_FILESYSTEM_PATH")
        if not root_path:
            raise ResultStorageError(
                "RESULT_STORAGE_FILESYSTEM_PATH is required for RESULT_STORAGE_BACKEND='filesystem'"
            )
        return FilesystemResultStorage(root_path)
    if backend == "s3":
        bucket = config.get("RESULT_STORAGE_S3_BUCKET")
        if not bucket:
            raise ResultStorageError(
                "RESULT_STORAGE_S3_BUCKET is required for RESULT_STORAGE_BACKEND='s3'"
            )
        return S3ResultStorage(
            bucket=str(bucket),
            prefix=str(config.get("RESULT_STORAGE_S3_PREFIX", "django-ray/results")),
            endpoint_url=(
                str(config["RESULT_STORAGE_S3_ENDPOINT_URL"])
                if config.get("RESULT_STORAGE_S3_ENDPOINT_URL")
                else None
            ),
            region_name=(
                str(config["RESULT_STORAGE_S3_REGION"])
                if config.get("RESULT_STORAGE_S3_REGION")
                else None
            ),
        )
    if backend == "gcs":
        bucket = config.get("RESULT_STORAGE_GCS_BUCKET")
        if not bucket:
            raise ResultStorageError(
                "RESULT_STORAGE_GCS_BUCKET is required for RESULT_STORAGE_BACKEND='gcs'"
            )
        return GCSResultStorage(
            bucket=str(bucket),
            prefix=str(config.get("RESULT_STORAGE_GCS_PREFIX", "django-ray/results")),
        )

    raise ResultStorageError(f"Unsupported RESULT_STORAGE_BACKEND: {backend}")
