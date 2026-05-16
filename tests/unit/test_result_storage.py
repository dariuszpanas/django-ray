"""Unit tests for result storage backends."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import django_ray.result_storage as result_storage_module
from django_ray.result_storage import (
    DigestResultStorage,
    FilesystemResultStorage,
    GCSResultStorage,
    ResultStorageError,
    S3ResultStorage,
    get_result_storage_backend,
    get_result_storage_backend_for_reference,
    load_result_reference,
)


class TestDigestResultStorage:
    """Tests for digest-only fallback storage."""

    def test_store_returns_deterministic_reference(self) -> None:
        storage = DigestResultStorage()
        payload = json.dumps({"value": "x" * 32})

        reference_one = storage.store(serialized_result=payload)
        reference_two = storage.store(serialized_result=payload)

        assert reference_one == reference_two
        assert reference_one.startswith("oversize://sha256/")
        assert "bytes=" in reference_one

    def test_load_returns_none(self) -> None:
        storage = DigestResultStorage()
        assert storage.load(reference="oversize://sha256/abc?bytes=1") is None


class TestFilesystemResultStorage:
    """Tests for filesystem-backed result storage."""

    def test_store_persists_and_load_round_trips(self, tmp_path) -> None:
        storage = FilesystemResultStorage(tmp_path)
        payload = json.dumps({"value": "x" * 64})

        reference = storage.store(serialized_result=payload)

        assert reference.startswith("resultfs://sha256/")
        assert storage.load(reference=reference) == payload

    def test_store_reuses_existing_payload_for_same_digest(self, tmp_path) -> None:
        storage = FilesystemResultStorage(tmp_path)
        payload = json.dumps({"value": "x" * 64})

        reference_one = storage.store(serialized_result=payload)
        reference_two = storage.store(serialized_result=payload)

        assert reference_one == reference_two
        assert len(list(tmp_path.rglob("*.json"))) == 1

    def test_load_rejects_unsafe_reference(self, tmp_path) -> None:
        storage = FilesystemResultStorage(tmp_path)

        with pytest.raises(ResultStorageError, match="Unsafe relative path"):
            storage.load(reference="resultfs://sha256/abc?rel=../secrets.json")


class TestS3ResultStorage:
    """Tests for S3-backed result storage."""

    @staticmethod
    def _make_client():
        objects: dict[tuple[str, str], bytes] = {}

        class Client:
            def put_object(self, **kwargs):
                objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

            def get_object(self, **kwargs):
                data = objects[(kwargs["Bucket"], kwargs["Key"])]
                return {"Body": SimpleNamespace(read=lambda: data)}

        return Client()

    def test_store_and_load_round_trip(self) -> None:
        storage = S3ResultStorage(
            bucket="test-bucket", prefix="app/results", client=self._make_client()
        )
        payload = json.dumps({"value": "x" * 64})

        reference = storage.store(serialized_result=payload)
        loaded = storage.load(reference=reference)

        assert reference.startswith("s3://test-bucket/app/results/")
        assert loaded == payload

    def test_load_rejects_unsafe_reference(self) -> None:
        storage = S3ResultStorage(bucket="test-bucket", client=self._make_client())

        with pytest.raises(ResultStorageError, match="Unsafe S3 key"):
            storage.load(reference="s3://test-bucket/../secrets.json")


class TestGCSResultStorage:
    """Tests for GCS-backed result storage."""

    @staticmethod
    def _make_client():
        objects: dict[tuple[str, str], bytes] = {}

        class Blob:
            def __init__(self, bucket_name: str, key: str) -> None:
                self.bucket_name = bucket_name
                self.key = key

            def upload_from_string(self, data: str, content_type: str = "application/json"):  # noqa: ARG002
                objects[(self.bucket_name, self.key)] = data.encode("utf-8")

            def download_as_bytes(self) -> bytes:
                return objects[(self.bucket_name, self.key)]

        class Bucket:
            def __init__(self, name: str) -> None:
                self.name = name

            def blob(self, key: str) -> Blob:
                return Blob(self.name, key)

        class Client:
            def bucket(self, name: str) -> Bucket:
                return Bucket(name)

        return Client()

    def test_store_and_load_round_trip(self) -> None:
        storage = GCSResultStorage(
            bucket="test-bucket", prefix="app/results", client=self._make_client()
        )
        payload = json.dumps({"value": "x" * 64})

        reference = storage.store(serialized_result=payload)
        loaded = storage.load(reference=reference)

        assert reference.startswith("gs://test-bucket/app/results/")
        assert loaded == payload

    def test_load_rejects_unsafe_reference(self) -> None:
        storage = GCSResultStorage(bucket="test-bucket", client=self._make_client())

        with pytest.raises(ResultStorageError, match="Unsafe GCS key"):
            storage.load(reference="gs://test-bucket/../secrets.json")


class TestResultStorageFactory:
    """Tests for backend factory resolution."""

    def test_default_backend_is_digest(self) -> None:
        backend = get_result_storage_backend({})
        assert isinstance(backend, DigestResultStorage)

    def test_filesystem_backend_requires_path(self) -> None:
        with pytest.raises(ResultStorageError, match="RESULT_STORAGE_FILESYSTEM_PATH"):
            get_result_storage_backend({"RESULT_STORAGE_BACKEND": "filesystem"})

    def test_filesystem_backend_can_be_resolved(self, tmp_path) -> None:
        backend = get_result_storage_backend(
            {
                "RESULT_STORAGE_BACKEND": "filesystem",
                "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
            }
        )
        assert isinstance(backend, FilesystemResultStorage)

    def test_s3_backend_requires_bucket(self) -> None:
        with pytest.raises(ResultStorageError, match="RESULT_STORAGE_S3_BUCKET"):
            get_result_storage_backend({"RESULT_STORAGE_BACKEND": "s3"})

    def test_s3_backend_can_be_resolved(self, monkeypatch) -> None:
        class FakeS3Backend:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

            def store(self, *, serialized_result: str) -> str:  # noqa: ARG002
                return "s3://fake"

            def load(self, *, reference: str) -> str | None:  # noqa: ARG002
                return None

        monkeypatch.setattr(result_storage_module, "S3ResultStorage", FakeS3Backend)

        backend = get_result_storage_backend(
            {
                "RESULT_STORAGE_BACKEND": "s3",
                "RESULT_STORAGE_S3_BUCKET": "bucket-a",
                "RESULT_STORAGE_S3_PREFIX": "prefix-a",
                "RESULT_STORAGE_S3_REGION": "us-east-1",
                "RESULT_STORAGE_S3_ENDPOINT_URL": "http://localhost:9000",
            }
        )

        assert isinstance(backend, FakeS3Backend)
        assert backend.kwargs["bucket"] == "bucket-a"
        assert backend.kwargs["prefix"] == "prefix-a"
        assert backend.kwargs["region_name"] == "us-east-1"
        assert backend.kwargs["endpoint_url"] == "http://localhost:9000"

    def test_gcs_backend_requires_bucket(self) -> None:
        with pytest.raises(ResultStorageError, match="RESULT_STORAGE_GCS_BUCKET"):
            get_result_storage_backend({"RESULT_STORAGE_BACKEND": "gcs"})

    def test_gcs_backend_can_be_resolved(self, monkeypatch) -> None:
        class FakeGCSBackend:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

            def store(self, *, serialized_result: str) -> str:  # noqa: ARG002
                return "gs://fake"

            def load(self, *, reference: str) -> str | None:  # noqa: ARG002
                return None

        monkeypatch.setattr(result_storage_module, "GCSResultStorage", FakeGCSBackend)

        backend = get_result_storage_backend(
            {
                "RESULT_STORAGE_BACKEND": "gcs",
                "RESULT_STORAGE_GCS_BUCKET": "bucket-b",
                "RESULT_STORAGE_GCS_PREFIX": "prefix-b",
            }
        )

        assert isinstance(backend, FakeGCSBackend)
        assert backend.kwargs["bucket"] == "bucket-b"
        assert backend.kwargs["prefix"] == "prefix-b"

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ResultStorageError, match="Unsupported RESULT_STORAGE_BACKEND"):
            get_result_storage_backend({"RESULT_STORAGE_BACKEND": "unknown"})

    def test_reference_factory_resolves_filesystem_backend(self, tmp_path) -> None:
        backend = get_result_storage_backend_for_reference(
            "resultfs://sha256/abc?rel=a/b.json&bytes=3",
            {"RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path)},
        )

        assert isinstance(backend, FilesystemResultStorage)

    def test_reference_factory_resolves_s3_backend_from_reference(self, monkeypatch) -> None:
        class FakeS3Backend:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

            def store(self, *, serialized_result: str) -> str:  # noqa: ARG002
                return "s3://fake"

            def load(self, *, reference: str) -> str | None:  # noqa: ARG002
                return '{"loaded": true}'

        monkeypatch.setattr(result_storage_module, "S3ResultStorage", FakeS3Backend)

        backend = get_result_storage_backend_for_reference(
            "s3://bucket-from-ref/a/b/result.json?bytes=7",
            {
                "RESULT_STORAGE_S3_REGION": "us-west-2",
                "RESULT_STORAGE_S3_ENDPOINT_URL": "http://localhost:9000",
            },
        )

        assert isinstance(backend, FakeS3Backend)
        assert backend.kwargs["bucket"] == "bucket-from-ref"
        assert backend.kwargs["region_name"] == "us-west-2"
        assert backend.kwargs["endpoint_url"] == "http://localhost:9000"

    def test_load_result_reference_uses_reference_scheme(self, monkeypatch) -> None:
        class FakeFilesystemBackend:
            def load(self, *, reference: str) -> str | None:
                return f"loaded:{reference}"

        monkeypatch.setattr(
            result_storage_module,
            "get_result_storage_backend_for_reference",
            lambda reference, config=None: FakeFilesystemBackend(),  # noqa: ARG005
        )

        loaded = load_result_reference("resultfs://sha256/abc?rel=x/y.json&bytes=4")

        assert loaded == "loaded:resultfs://sha256/abc?rel=x/y.json&bytes=4"
