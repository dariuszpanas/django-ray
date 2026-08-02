"""Unit tests for result storage backends."""

from __future__ import annotations

import errno
import hashlib
import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import django_ray.result_storage as result_storage_module
from django_ray.result_storage import (
    DigestResultStorage,
    FilesystemResultStorage,
    GCSResultStorage,
    ResultStorageError,
    S3ResultStorage,
    canonicalize_result_reference,
    get_result_storage_backend,
    get_result_storage_backend_for_reference,
    is_valid_result_reference,
    load_result_reference,
)


def _digest_reference(payload: str) -> str:
    payload_bytes = payload.encode("utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    return f"oversize://sha256/{digest}?bytes={len(payload_bytes)}"


def _filesystem_reference(payload: str) -> str:
    payload_bytes = payload.encode("utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    relative_path = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
    return f"resultfs://sha256/{digest}?rel={relative_path}&bytes={len(payload_bytes)}"


def _object_reference(
    payload: str,
    *,
    scheme: str,
    bucket: str,
    prefix: str = "django-ray/results",
) -> str:
    payload_bytes = payload.encode("utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    clean_prefix = prefix.strip("/")
    suffix = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
    key = f"{clean_prefix}/{suffix}" if clean_prefix else suffix
    return f"{scheme}://{bucket}/{key}?bytes={len(payload_bytes)}"


class _ProviderError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__("sensitive provider failure")
        self.code = status
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


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
        reference = storage.store(serialized_result="payload")
        assert storage.load(reference=reference) is None
        assert storage.delete(reference=reference) is None


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "resultfs://sha256/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef?bytes=0",
        "oversize://sha256/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    ],
)
def test_result_reference_validation_rejects_missing_required_parts(reference: str) -> None:
    assert is_valid_result_reference(reference) is False


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

    def test_relative_root_is_stable_if_process_working_directory_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        storage = FilesystemResultStorage("storage")
        reference = storage.store(serialized_result="payload")

        other_directory = tmp_path / "other"
        other_directory.mkdir()
        monkeypatch.chdir(other_directory)

        assert storage.load(reference=reference) == "payload"
        storage.delete(reference=reference)
        assert list((tmp_path / "storage").rglob("*.json")) == []

    def test_delete_removes_payload_and_wraps_errors(self, monkeypatch, tmp_path) -> None:
        storage = FilesystemResultStorage(tmp_path)
        reference = storage.store(serialized_result="payload")

        storage.delete(reference=reference)
        assert list(tmp_path.rglob("*.json")) == []

        monkeypatch.setattr(
            Path,
            "unlink",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("locked")),
        )
        with pytest.raises(ResultStorageError, match="Failed to delete result payload"):
            storage.delete(reference=reference)

    def test_load_rejects_unsafe_reference(self, tmp_path) -> None:
        storage = FilesystemResultStorage(tmp_path)

        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference="resultfs://sha256/abc?rel=../secrets.json")

    def test_store_and_load_wrap_filesystem_errors(self, monkeypatch, tmp_path) -> None:
        storage = FilesystemResultStorage(tmp_path)

        def _mkdir_error(*args, **kwargs):
            raise OSError("read only")

        monkeypatch.setattr(Path, "mkdir", _mkdir_error)
        with pytest.raises(ResultStorageError, match="Failed to persist result payload") as caught:
            storage.store(serialized_result="payload")
        assert "read only" not in str(caught.value)
        assert caught.value.__cause__ is None

        monkeypatch.undo()
        reference = storage.store(serialized_result="payload")

        def _read_error(*args, **kwargs):
            raise OSError("unreadable")

        monkeypatch.setattr(Path, "open", _read_error)
        with pytest.raises(ResultStorageError, match="unavailable from filesystem"):
            storage.load(reference=reference)

    def test_load_rejects_missing_and_malformed_references(self, tmp_path) -> None:
        storage = FilesystemResultStorage(tmp_path)

        with pytest.raises(ResultStorageError, match="unavailable from filesystem"):
            storage.load(reference=_filesystem_reference("missing"))
        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference="s3://bucket/key")
        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference="resultfs://sha256/abc")


class TestS3ResultStorage:
    """Tests for S3-backed result storage."""

    @staticmethod
    def _make_client():
        objects: dict[tuple[str, str], tuple[bytes, str]] = {}

        class Client:
            def put_object(self, **kwargs):
                object_id = (kwargs["Bucket"], kwargs["Key"])
                assert kwargs["IfNoneMatch"] == "*"
                if object_id in objects:
                    raise _ProviderError(412, "PreconditionFailed")
                payload = kwargs["Body"]
                objects[object_id] = (payload, f'"{hashlib.sha256(payload).hexdigest()}"')

            def get_object(self, **kwargs):
                data, etag = objects[(kwargs["Bucket"], kwargs["Key"])]
                return {
                    "Body": SimpleNamespace(
                        read=lambda amount: data[:amount],
                        close=lambda: None,
                    ),
                    "ContentLength": len(data),
                    "ETag": etag,
                }

            def delete_object(self, **kwargs):
                object_id = (kwargs["Bucket"], kwargs["Key"])
                _payload, etag = objects[object_id]
                if kwargs["IfMatch"] != etag:
                    raise _ProviderError(412, "PreconditionFailed")
                objects.pop(object_id)

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

        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference="s3://test-bucket/../secrets.json")

    def test_imports_boto3_when_client_is_omitted(self, monkeypatch) -> None:
        marker = object()
        module = SimpleNamespace(client=lambda *args, **kwargs: marker)
        monkeypatch.setattr(result_storage_module.importlib, "import_module", lambda name: module)

        storage = S3ResultStorage(bucket="bucket")

        assert storage.client is marker

    def test_missing_boto3_has_install_guidance(self, monkeypatch) -> None:
        def _missing(name: str):
            raise ImportError(name)

        monkeypatch.setattr(result_storage_module.importlib, "import_module", _missing)

        with pytest.raises(ResultStorageError, match="pip install boto3"):
            S3ResultStorage(bucket="bucket")

    def test_wraps_client_errors_and_validates_body_type(self) -> None:
        client = SimpleNamespace(
            put_object=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("put failed")),
            get_object=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("get failed")),
        )
        storage = S3ResultStorage(bucket="bucket", client=client)

        with pytest.raises(ResultStorageError, match="Failed to persist result payload") as caught:
            storage.store(serialized_result="payload")
        assert "put failed" not in str(caught.value)
        assert caught.value.__cause__ is None
        reference = _object_reference("payload", scheme="s3", bucket="bucket")
        with pytest.raises(ResultStorageError, match="unavailable from S3") as caught:
            storage.load(reference=reference)
        assert "get failed" not in str(caught.value)
        assert caught.value.__cause__ is None

        storage.client = SimpleNamespace(
            get_object=lambda **kwargs: {
                "Body": SimpleNamespace(read=lambda amount: b"payload"[:amount]),
                "ContentLength": 7,
            }
        )
        assert storage.load(reference=reference) == "payload"

        storage.client = SimpleNamespace(
            get_object=lambda **kwargs: {
                "Body": SimpleNamespace(read=lambda amount: object()),
                "ContentLength": 7,
            }
        )
        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.load(reference=reference)

    def test_rejects_malformed_reference(self) -> None:
        storage = S3ResultStorage(bucket="bucket", client=self._make_client())

        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference="https://example.com/result")

    def test_empty_prefix_stores_at_digest_root(self) -> None:
        storage = S3ResultStorage(bucket="bucket", prefix="", client=self._make_client())

        reference = storage.store(serialized_result="payload")

        assert reference.startswith("s3://bucket/")
        assert "django-ray/results" not in reference

    def test_delete_removes_object_and_wraps_errors(self) -> None:
        storage = S3ResultStorage(bucket="bucket", client=self._make_client())
        reference = storage.store(serialized_result="payload")
        storage.delete(reference=reference)
        with pytest.raises(ResultStorageError, match="unavailable from S3"):
            storage.load(reference=reference)

        storage.client = SimpleNamespace(
            get_object=lambda **kwargs: {
                "Body": SimpleNamespace(read=lambda amount: b"payload"[:amount]),
                "ContentLength": 7,
                "ETag": '"payload-etag"',
            },
            delete_object=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("denied")),
        )
        with pytest.raises(ResultStorageError, match="Failed to delete result payload from S3"):
            storage.delete(reference=reference)


class TestGCSResultStorage:
    """Tests for GCS-backed result storage."""

    @staticmethod
    def _make_client():
        objects: dict[tuple[str, str], tuple[bytes, int]] = {}
        next_generation = 1

        class Blob:
            def __init__(self, bucket_name: str, key: str) -> None:
                self.bucket_name = bucket_name
                self.key = key

            def upload_from_string(
                self,
                data: bytes,
                content_type: str = "application/json",  # noqa: ARG002
                *,
                if_generation_match: int,
            ) -> None:
                nonlocal next_generation
                object_id = (self.bucket_name, self.key)
                assert if_generation_match == 0
                if object_id in objects:
                    raise _ProviderError(412, "PreconditionFailed")
                objects[object_id] = (data, next_generation)
                next_generation += 1

            @property
            def size(self) -> int:
                return len(objects[(self.bucket_name, self.key)][0])

            @property
            def generation(self) -> int:
                return objects[(self.bucket_name, self.key)][1]

            def reload(self) -> None:
                objects[(self.bucket_name, self.key)]

            def download_as_bytes(
                self,
                *,
                start: int,
                end: int,
                if_generation_match: int,
            ) -> bytes:
                payload, generation = objects[(self.bucket_name, self.key)]
                if if_generation_match != generation:
                    raise _ProviderError(412, "PreconditionFailed")
                return payload[start : end + 1]

            def delete(self, *, if_generation_match: int) -> None:
                object_id = (self.bucket_name, self.key)
                _payload, generation = objects[object_id]
                if if_generation_match != generation:
                    raise _ProviderError(412, "PreconditionFailed")
                objects.pop(object_id)

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

        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference="gs://test-bucket/../secrets.json")

    def test_imports_storage_client_when_client_is_omitted(self, monkeypatch) -> None:
        marker = object()
        module = SimpleNamespace(Client=lambda: marker)
        monkeypatch.setattr(result_storage_module.importlib, "import_module", lambda name: module)

        storage = GCSResultStorage(bucket="bucket")

        assert storage.client is marker

    def test_missing_storage_sdk_has_install_guidance(self, monkeypatch) -> None:
        def _missing(name: str):
            raise ImportError(name)

        monkeypatch.setattr(result_storage_module.importlib, "import_module", _missing)

        with pytest.raises(ResultStorageError, match="google-cloud-storage"):
            GCSResultStorage(bucket="bucket")

    def test_wraps_client_errors(self) -> None:
        bucket = SimpleNamespace(
            blob=lambda key: SimpleNamespace(
                upload_from_string=lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("upload failed")
                ),
                reload=lambda: None,
                size=7,
                generation=1,
                download_as_bytes=lambda **kwargs: (_ for _ in ()).throw(
                    RuntimeError("download failed")
                ),
            )
        )
        storage = GCSResultStorage(
            bucket="bucket",
            client=SimpleNamespace(bucket=lambda name: bucket),
        )

        with pytest.raises(ResultStorageError, match="Failed to persist result payload") as caught:
            storage.store(serialized_result="payload")
        assert "upload failed" not in str(caught.value)
        assert caught.value.__cause__ is None
        with pytest.raises(ResultStorageError, match="unavailable from GCS") as caught:
            storage.load(reference=_object_reference("payload", scheme="gs", bucket="bucket"))
        assert "download failed" not in str(caught.value)
        assert caught.value.__cause__ is None

    def test_rejects_malformed_reference(self) -> None:
        storage = GCSResultStorage(bucket="bucket", client=self._make_client())

        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference="https://example.com/result")

    def test_delete_removes_object_and_wraps_errors(self) -> None:
        storage = GCSResultStorage(bucket="bucket", client=self._make_client())
        reference = storage.store(serialized_result="payload")
        storage.delete(reference=reference)
        with pytest.raises(ResultStorageError, match="unavailable from GCS"):
            storage.load(reference=reference)

        blob = SimpleNamespace(
            reload=lambda: None,
            size=7,
            generation=1,
            download_as_bytes=lambda **kwargs: b"payload",
            delete=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("denied")),
        )
        storage.client = SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: blob))
        with pytest.raises(ResultStorageError, match="Failed to delete result payload from GCS"):
            storage.delete(reference=reference)


class TestResultStorageFactory:
    """Tests for backend factory resolution."""

    @pytest.mark.parametrize(
        ("reference", "expected"),
        [
            (_digest_reference("x"), True),
            (_filesystem_reference("x"), True),
            (_object_reference("x", scheme="s3", bucket="bucket"), True),
            (_object_reference("x", scheme="gs", bucket="bucket"), True),
            ("s3://bucket/?bytes=1", False),
            ("s3://[::1/?bytes=1", False),
            (
                "resultfs://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?rel=.&bytes=1",
                False,
            ),
            ("oversize://sha256/unknown?bytes=1", False),
            ("unknown://bucket/result?bytes=1", False),
            ("s3://bucket/result?bytes=nope", False),
        ],
    )
    def test_result_reference_validation(self, reference: str, expected: bool) -> None:
        assert is_valid_result_reference(reference) is expected

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

    def test_s3_backend_factory_preserves_an_explicit_empty_prefix(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        class FakeS3Backend:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(result_storage_module, "S3ResultStorage", FakeS3Backend)

        get_result_storage_backend(
            {
                "RESULT_STORAGE_BACKEND": "s3",
                "RESULT_STORAGE_S3_BUCKET": "bucket",
                "RESULT_STORAGE_S3_PREFIX": "",
            }
        )

        assert captured["prefix"] == ""

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
            _filesystem_reference("abc"),
            {"RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path)},
        )

        assert isinstance(backend, FilesystemResultStorage)

    def test_reference_factory_resolves_configured_s3_backend(self, monkeypatch) -> None:
        class FakeS3Backend:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

            def store(self, *, serialized_result: str) -> str:  # noqa: ARG002
                return "s3://fake"

            def load(self, *, reference: str) -> str | None:  # noqa: ARG002
                return '{"loaded": true}'

        monkeypatch.setattr(result_storage_module, "S3ResultStorage", FakeS3Backend)

        reference = _object_reference(
            "payload",
            scheme="s3",
            bucket="configured-bucket",
            prefix="configured-prefix",
        )
        backend = get_result_storage_backend_for_reference(
            reference,
            {
                "RESULT_STORAGE_S3_BUCKET": "configured-bucket",
                "RESULT_STORAGE_S3_PREFIX": "configured-prefix",
                "RESULT_STORAGE_S3_REGION": "us-west-2",
                "RESULT_STORAGE_S3_ENDPOINT_URL": "http://localhost:9000",
            },
        )

        assert isinstance(backend, FakeS3Backend)
        assert backend.kwargs["bucket"] == "configured-bucket"
        assert backend.kwargs["prefix"] == "configured-prefix"
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

        reference = _filesystem_reference("data")
        loaded = load_result_reference(reference)

        assert loaded == f"loaded:{reference}"

    def test_reference_factory_handles_digest_and_missing_configuration(self) -> None:
        assert isinstance(
            get_result_storage_backend_for_reference(_digest_reference("x"), {}),
            DigestResultStorage,
        )

        with pytest.raises(ResultStorageError, match="RESULT_STORAGE_FILESYSTEM_PATH"):
            get_result_storage_backend_for_reference(_filesystem_reference("x"), {})
        with pytest.raises(ResultStorageError, match="RESULT_STORAGE_S3_BUCKET"):
            get_result_storage_backend_for_reference(
                _object_reference("x", scheme="s3", bucket="bucket"), {}
            )
        with pytest.raises(ResultStorageError, match="RESULT_STORAGE_GCS_BUCKET"):
            get_result_storage_backend_for_reference(
                _object_reference("x", scheme="gs", bucket="bucket"), {}
            )
        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            get_result_storage_backend_for_reference("https://example.com/result", {})

    def test_reference_factory_resolves_gcs_backend(self, monkeypatch) -> None:
        class FakeGCSBackend:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

            def load(self, *, reference: str) -> str | None:  # noqa: ARG002
                return None

        monkeypatch.setattr(result_storage_module, "GCSResultStorage", FakeGCSBackend)

        backend = get_result_storage_backend_for_reference(
            _object_reference("payload", scheme="gs", bucket="bucket", prefix="configured/results"),
            {
                "RESULT_STORAGE_GCS_BUCKET": "bucket",
                "RESULT_STORAGE_GCS_PREFIX": "configured/results",
            },
        )

        assert isinstance(backend, FakeGCSBackend)
        assert backend.kwargs == {"bucket": "bucket", "prefix": "configured/results"}

    def test_factories_use_django_settings_when_config_is_omitted(
        self, monkeypatch, settings
    ) -> None:
        settings.DJANGO_RAY = {"RESULT_STORAGE_BACKEND": "digest"}
        assert isinstance(get_result_storage_backend(), DigestResultStorage)

        monkeypatch.setattr(
            result_storage_module,
            "get_result_storage_backend_for_reference",
            lambda reference, config=None: DigestResultStorage(),
        )
        assert load_result_reference(_digest_reference("x")) is None

    def test_reference_factory_uses_settings_when_config_is_omitted(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            result_storage_module,
            "get_settings",
            lambda: {
                "RESULT_STORAGE_BACKEND": "filesystem",
                "RESULT_STORAGE_FILESYSTEM_PATH": tmp_path,
            },
        )

        backend = get_result_storage_backend_for_reference(_filesystem_reference("x"))

        assert isinstance(backend, FilesystemResultStorage)
        assert backend.root_path == tmp_path


class TestResultReferenceIntegrity:
    """Adversarial coverage for canonical references and payload integrity."""

    def test_canonicalization_requires_authorized_filesystem_namespace(self) -> None:
        reference = _filesystem_reference("payload")

        with pytest.raises(ResultStorageError, match="FILESYSTEM_PATH is required"):
            canonicalize_result_reference(reference, {})
        assert (
            canonicalize_result_reference(
                reference,
                {"RESULT_STORAGE_FILESYSTEM_PATH": "/srv/django-ray/results"},
            )
            == reference
        )

    def test_canonicalization_uses_default_prefix_and_rejects_invalid_prefix_type(
        self,
    ) -> None:
        reference = _object_reference("payload", scheme="s3", bucket="bucket")

        assert (
            canonicalize_result_reference(
                reference,
                {"RESULT_STORAGE_S3_BUCKET": "bucket"},
            )
            == reference
        )
        with pytest.raises(ResultStorageError, match="configuration is invalid"):
            canonicalize_result_reference(
                reference,
                {
                    "RESULT_STORAGE_S3_BUCKET": "bucket",
                    "RESULT_STORAGE_S3_PREFIX": 42,
                },
            )

    @pytest.mark.parametrize("scheme", ["s3", "gs"])
    def test_canonicalization_requires_configured_object_authority(self, scheme: str) -> None:
        reference = _object_reference("payload", scheme=scheme, bucket="bucket")

        with pytest.raises(ResultStorageError, match="BUCKET is required"):
            canonicalize_result_reference(reference, {})

    def test_filesystem_constructor_bounds_resolution_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            Path,
            "resolve",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private path")),
        )

        with pytest.raises(ResultStorageError, match="path is unavailable") as caught:
            FilesystemResultStorage(tmp_path)
        assert "private path" not in str(caught.value)
        assert caught.value.__context__ is None

    def test_s3_conditional_create_reuses_only_verified_content(self) -> None:
        payload = b"payload"
        reference = _object_reference("payload", scheme="s3", bucket="bucket")
        existing = payload
        put_conditions: list[str] = []

        class Client:
            def put_object(self, **kwargs):
                put_conditions.append(kwargs["IfNoneMatch"])
                raise _ProviderError(412, "PreconditionFailed")

            def get_object(self, **kwargs):
                return {
                    "Body": SimpleNamespace(
                        read=lambda amount: existing[:amount],
                        close=lambda: None,
                    ),
                    "ContentLength": len(existing),
                    "ETag": '"existing"',
                }

        storage = S3ResultStorage(bucket="bucket", client=Client())
        assert storage.store(serialized_result="payload") == reference

        existing = b"PAYLOAD"
        with pytest.raises(ResultStorageError, match="integrity verification") as caught:
            storage.store(serialized_result="payload")
        assert put_conditions == ["*", "*"]
        assert existing == b"PAYLOAD"
        assert caught.value.__context__ is None

    def test_s3_conditional_conflict_retries_without_unconditional_write(self) -> None:
        calls: list[str] = []

        class Client:
            def put_object(self, **kwargs):
                calls.append(kwargs["IfNoneMatch"])
                if len(calls) < 3:
                    raise _ProviderError(409, "ConditionalRequestConflict")

        storage = S3ResultStorage(bucket="bucket", client=Client())

        assert storage.store(serialized_result="payload") == _object_reference(
            "payload", scheme="s3", bucket="bucket"
        )
        assert calls == ["*", "*", "*"]

    def test_s3_delete_refuses_a_replacement_after_verified_read(self) -> None:
        payload = b"payload"
        reference = _object_reference("payload", scheme="s3", bucket="bucket")
        delete_conditions: list[str] = []

        class Client:
            def get_object(self, **kwargs):
                return {
                    "Body": SimpleNamespace(
                        read=lambda amount: payload[:amount],
                        close=lambda: None,
                    ),
                    "ContentLength": len(payload),
                    "ETag": '"verified-version"',
                }

            def delete_object(self, **kwargs):
                delete_conditions.append(kwargs["IfMatch"])
                raise _ProviderError(412, "PreconditionFailed")

        storage = S3ResultStorage(bucket="bucket", client=Client())
        with pytest.raises(ResultStorageError, match="Failed to delete") as caught:
            storage.delete(reference=reference)

        assert delete_conditions == ['"verified-version"']
        assert caught.value.__context__ is None

    def test_s3_delete_requires_a_bounded_verified_etag(self) -> None:
        payload = b"payload"
        reference = _object_reference("payload", scheme="s3", bucket="bucket")
        storage = S3ResultStorage(
            bucket="bucket",
            client=SimpleNamespace(
                get_object=lambda **kwargs: {
                    "Body": SimpleNamespace(
                        read=lambda amount: payload[:amount],
                        close=lambda: None,
                    ),
                    "ContentLength": len(payload),
                },
                delete_object=lambda **kwargs: pytest.fail(
                    "delete must not run without a verified ETag"
                ),
            ),
        )

        with pytest.raises(ResultStorageError, match="Failed to delete") as caught:
            storage.delete(reference=reference)
        assert caught.value.__context__ is None

    def test_s3_hostile_provider_metadata_is_bounded(self) -> None:
        sensitive = "private-response-token"
        reference = _object_reference("payload", scheme="s3", bucket="bucket")

        class HostileResponse(dict):
            def get(self, key, default=None):  # noqa: ARG002
                raise RuntimeError(sensitive)

        storage = S3ResultStorage(
            bucket="bucket",
            client=SimpleNamespace(get_object=lambda **kwargs: HostileResponse()),
        )
        with pytest.raises(ResultStorageError, match="unavailable from S3") as caught:
            storage.load(reference=reference)

        assert sensitive not in str(caught.value)
        assert caught.value.__context__ is None

    def test_s3_hostile_provider_exception_attributes_are_bounded(self) -> None:
        sensitive = "private-provider-attribute"

        class HostileError(RuntimeError):
            @property
            def response(self):
                raise RuntimeError(sensitive)

            @property
            def code(self):
                raise RuntimeError(sensitive)

            @property
            def status_code(self):
                raise RuntimeError(sensitive)

        storage = S3ResultStorage(
            bucket="bucket",
            client=SimpleNamespace(
                put_object=lambda **kwargs: (_ for _ in ()).throw(HostileError(sensitive))
            ),
        )

        with pytest.raises(ResultStorageError, match="Failed to persist") as caught:
            storage.store(serialized_result="payload")
        assert sensitive not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_hostile_provider_error_mappings_and_markers_are_ignored(self) -> None:
        sensitive = "private-provider-marker"

        class HostileMapping(dict):
            def get(self, key, default=None):  # noqa: ARG002
                raise RuntimeError(sensitive)

        class HostileInt(int):
            def __int__(self):
                raise RuntimeError(sensitive)

        class HostileError(RuntimeError):
            pass

        top_level = HostileError(sensitive)
        top_level.response = HostileMapping()
        top_level.code = HostileInt(409)
        top_level.status_code = None
        assert result_storage_module._provider_error_markers(top_level) == set()

        nested = HostileError(sensitive)
        nested.response = {
            "Error": HostileMapping(),
            "ResponseMetadata": HostileMapping(),
        }
        assert result_storage_module._provider_error_markers(nested) == set()

    def test_gcs_conditional_create_reuses_only_verified_generation(self) -> None:
        payload = b"payload"
        existing = payload
        upload_conditions: list[int] = []
        download_conditions: list[int] = []

        class Blob:
            size = len(payload)
            generation = 7

            def upload_from_string(self, data, **kwargs):
                upload_conditions.append(kwargs["if_generation_match"])
                raise _ProviderError(412, "PreconditionFailed")

            def reload(self) -> None:
                return None

            def download_as_bytes(self, **kwargs):
                download_conditions.append(kwargs["if_generation_match"])
                return existing

        blob = Blob()
        storage = GCSResultStorage(
            bucket="bucket",
            client=SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: blob)),
        )
        assert storage.store(serialized_result="payload") == _object_reference(
            "payload", scheme="gs", bucket="bucket"
        )

        existing = b"PAYLOAD"
        with pytest.raises(ResultStorageError, match="integrity verification") as caught:
            storage.store(serialized_result="payload")
        assert upload_conditions == [0, 0]
        assert download_conditions == [7, 7]
        assert caught.value.__context__ is None

    def test_gcs_delete_refuses_a_replacement_after_generation_pinned_read(self) -> None:
        payload = b"payload"
        reference = _object_reference("payload", scheme="gs", bucket="bucket")
        delete_conditions: list[int] = []

        class Blob:
            size = len(payload)
            generation = 11

            def reload(self) -> None:
                return None

            def download_as_bytes(self, **kwargs):
                assert kwargs["if_generation_match"] == 11
                return payload

            def delete(self, **kwargs):
                delete_conditions.append(kwargs["if_generation_match"])
                raise _ProviderError(412, "PreconditionFailed")

        blob = Blob()
        storage = GCSResultStorage(
            bucket="bucket",
            client=SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: blob)),
        )
        with pytest.raises(ResultStorageError, match="Failed to delete") as caught:
            storage.delete(reference=reference)

        assert delete_conditions == [11]
        assert caught.value.__context__ is None

    @pytest.mark.parametrize("generation", [None, 0, "11", True])
    def test_gcs_rejects_unusable_generation_metadata(self, generation: object) -> None:
        payload = b"payload"
        reference = _object_reference("payload", scheme="gs", bucket="bucket")
        blob = SimpleNamespace(
            reload=lambda: None,
            size=len(payload),
            generation=generation,
            download_as_bytes=lambda **kwargs: pytest.fail(
                "download must be pinned to a valid generation"
            ),
        )
        storage = GCSResultStorage(
            bucket="bucket",
            client=SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: blob)),
        )

        with pytest.raises(ResultStorageError, match="integrity verification") as caught:
            storage.load(reference=reference)
        assert caught.value.__context__ is None

    def test_gcs_generation_lookup_failure_is_bounded(self) -> None:
        sensitive = "private-generation-metadata"
        reference = _object_reference("payload", scheme="gs", bucket="bucket")

        class Blob:
            size = 7

            def reload(self) -> None:
                return None

            @property
            def generation(self) -> int:
                raise RuntimeError(sensitive)

        storage = GCSResultStorage(
            bucket="bucket",
            client=SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: Blob())),
        )

        with pytest.raises(ResultStorageError, match="unavailable from GCS") as caught:
            storage.load(reference=reference)
        assert sensitive not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True

    @pytest.mark.parametrize(
        "reference",
        [
            "oversize://sha256/" + "a" * 64 + "?bytes=01",
            "oversize://sha256/" + "a" * 64 + "?bytes=+1",
            "oversize://sha256/" + "A" * 64 + "?bytes=1",
            "OVERSIZE://sha256/" + "a" * 64 + "?bytes=1",
            "oversize://sha256/" + "a" * 64 + "?bytes=1&extra=1",
            "oversize://sha256/" + "a" * 64 + "?bytes=9223372036854775808",
            "oversize://sha256/" + "a" * 64 + "?bytes=1#fragment",
            "resultfs://sha256/" + "a" * 64 + "?bytes=1&rel=aa/aa/" + "a" * 64 + ".json",
            "resultfs://sha256/" + "a" * 64 + "?rel=aa/aa/" + "b" * 64 + ".json&bytes=1",
            "RESULTFS://sha256/" + "a" * 64 + "?rel=aa/aa/" + "a" * 64 + ".json&bytes=1",
            "s3://user@bucket/aa/aa/" + "a" * 64 + ".json?bytes=1",
            "s3://bucket:443/aa/aa/" + "a" * 64 + ".json?bytes=1",
            "s3://bucket/%2E%2E/aa/aa/" + "a" * 64 + ".json?bytes=1",
            "s3://bucket/%72esults/aa/aa/" + "a" * 64 + ".json?bytes=1",
            "s3://bucket/results//aa/aa/" + "a" * 64 + ".json?bytes=1",
            "s3://bucket/results/ab/aa/" + "a" * 64 + ".json?bytes=1",
            "s3://bucket/results/aa/aa/" + "a" * 64 + ".json?bytes=1&bytes=1",
            "s3://bucket/%FF/aa/aa/" + "a" * 64 + ".json?bytes=1",
            "s3://bucket/%C2%80/aa/aa/" + "a" * 64 + ".json?bytes=1",
            "s3://bucket/results/aa/aa/not-json.txt?bytes=1",
            "s3://bucket/results/aa/aa/not-a-digest.json?bytes=1",
            "s3://bucket/not-a-digest.json?bytes=1",
            "S3://bucket/results/aa/aa/" + "a" * 64 + ".json?bytes=1",
            "x" * 501,
        ],
    )
    def test_noncanonical_or_unsafe_references_are_rejected(self, reference: str) -> None:
        assert is_valid_result_reference(reference) is False
        with pytest.raises(ResultStorageError, match="Result reference is invalid") as caught:
            get_result_storage_backend_for_reference(reference, {})
        assert reference not in str(caught.value)

    @pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
    def test_unpaired_surrogate_in_object_path_is_rejected_bounded(
        self,
        surrogate: str,
    ) -> None:
        digest = "a" * 64
        reference = f"s3://bucket/{surrogate}/aa/aa/{digest}.json?bytes=1"

        assert is_valid_result_reference(reference) is False
        with pytest.raises(ResultStorageError, match="Result reference is invalid") as caught:
            get_result_storage_backend_for_reference(reference, {})
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_historical_outer_slash_prefix_is_readable_after_setting_normalization(
        self,
    ) -> None:
        reference = _object_reference(
            "payload",
            scheme="s3",
            bucket="bucket",
            prefix="/prod/results/",
        )

        assert (
            canonicalize_result_reference(
                reference,
                {
                    "RESULT_STORAGE_S3_BUCKET": "bucket",
                    "RESULT_STORAGE_S3_PREFIX": "prod/results",
                },
            )
            == reference
        )

    @pytest.mark.parametrize(
        "legacy_prefix",
        ["prod//results", "prod/./results", "prod\\results", "prod/\x01results"],
        ids=("empty-segment", "dot-segment", "backslash", "control-character"),
    )
    def test_historical_unsafe_prefix_requires_pre_upgrade_migration(
        self,
        legacy_prefix: str,
    ) -> None:
        reference = _object_reference(
            "payload",
            scheme="s3",
            bucket="bucket",
            prefix=legacy_prefix,
        )

        assert is_valid_result_reference(reference) is False
        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            canonicalize_result_reference(
                reference,
                {
                    "RESULT_STORAGE_S3_BUCKET": "bucket",
                    "RESULT_STORAGE_S3_PREFIX": legacy_prefix,
                },
            )

    def test_payload_encoding_and_digest_backend_scheme_fail_closed(self, tmp_path: Path) -> None:
        storage = DigestResultStorage()
        with pytest.raises(ResultStorageError, match="could not be encoded") as caught:
            storage.store(serialized_result="\ud800")
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

        reference = _filesystem_reference("payload")
        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference=reference)
        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.delete(reference=reference)
        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            FilesystemResultStorage(tmp_path).load(reference=_digest_reference("payload"))

    def test_filesystem_namespace_normalization_failure_is_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sensitive = "private-filesystem-namespace"
        original_normpath = result_storage_module.os.path.normpath

        def _fail_for_result_namespace(value: str) -> str:
            if value == "/srv/results":
                raise OSError(sensitive)
            return original_normpath(value)

        monkeypatch.setattr(
            result_storage_module.os.path,
            "normpath",
            _fail_for_result_namespace,
        )

        with pytest.raises(ResultStorageError, match="usable filesystem namespace") as caught:
            result_storage_module.validate_storage_configuration(
                {
                    "RESULT_STORAGE_FILESYSTEM_PATH": "/srv/results",
                    "INPUT_STORAGE_FILESYSTEM_PATH": "/srv/inputs",
                }
            )
        assert sensitive not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True

    @pytest.mark.parametrize(
        "resolution_error",
        [
            OSError("private path"),
            RuntimeError("private path"),
            ValueError("private path"),
        ],
    )
    def test_filesystem_path_resolution_failure_is_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        resolution_error: Exception,
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        monkeypatch.setattr(
            Path,
            "resolve",
            lambda *args, **kwargs: (_ for _ in ()).throw(resolution_error),
        )

        with pytest.raises(ResultStorageError, match="path is unavailable") as caught:
            storage.load(reference=_filesystem_reference("payload"))
        assert "private path" not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_parse_failure_does_not_retain_parser_context(self) -> None:
        with pytest.raises(ResultStorageError, match="Result reference is invalid") as caught:
            DigestResultStorage().load(reference="s3://[::1/result?bytes=1")

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_filesystem_tamper_and_corrupt_reuse_fail_closed(self, tmp_path: Path) -> None:
        storage = FilesystemResultStorage(tmp_path)
        reference = storage.store(serialized_result="payload")
        payload_path = next(tmp_path.rglob("*.json"))
        payload_path.write_bytes(b"PAYLOAD")

        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.load(reference=reference)
        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.store(serialized_result="payload")

        assert payload_path.read_bytes() == b"PAYLOAD"

    def test_filesystem_concurrent_same_digest_install_is_atomic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        original_link = result_storage_module.os.link

        def _racing_link(source: Path, destination: Path) -> None:
            original_link(source, destination)

        monkeypatch.setattr(result_storage_module.os, "link", _racing_link)
        with ThreadPoolExecutor(max_workers=2) as executor:
            references = list(
                executor.map(
                    lambda _index: storage.store(serialized_result="payload"),
                    range(2),
                )
            )

        assert references[0] == references[1]
        assert storage.load(reference=references[0]) == "payload"
        assert len(list(tmp_path.rglob("*.json"))) == 1
        assert list(tmp_path.rglob(".django-ray-result-*.tmp")) == []

    def test_filesystem_falls_back_atomically_when_hard_links_are_unsupported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)

        def _unsupported_link(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

        monkeypatch.setattr(result_storage_module.os, "link", _unsupported_link)
        reference = storage.store(serialized_result="payload")

        assert storage.load(reference=reference) == "payload"
        assert len(list(tmp_path.rglob("*.json"))) == 1
        assert list(tmp_path.rglob(".django-ray-result-*.tmp")) == []
        assert list(tmp_path.rglob("*.install-lock")) == []

    def test_filesystem_temporary_name_collisions_fail_bounded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        monkeypatch.setattr(
            result_storage_module.os,
            "open",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileExistsError("private")),
        )

        with pytest.raises(ResultStorageError, match="Failed to persist") as caught:
            storage.store(serialized_result="payload")

        assert caught.value.__context__ is None
        assert "private" not in str(caught.value)

    def test_filesystem_temporary_write_failure_is_cleaned_and_bounded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        monkeypatch.setattr(
            result_storage_module.os,
            "fsync",
            lambda descriptor: (_ for _ in ()).throw(OSError("private write failure")),
        )

        with pytest.raises(ResultStorageError, match="Failed to persist") as caught:
            storage.store(serialized_result="payload")

        assert caught.value.__context__ is None
        assert "private write failure" not in str(caught.value)
        assert list(tmp_path.rglob(".django-ray-result-*.tmp")) == []

    def test_filesystem_stat_and_install_failures_are_bounded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        monkeypatch.setattr(
            Path,
            "stat",
            lambda self: (_ for _ in ()).throw(OSError("private stat failure")),
        )
        with pytest.raises(ResultStorageError, match="Failed to persist") as caught:
            storage.store(serialized_result="payload")
        assert caught.value.__context__ is None

        monkeypatch.undo()
        monkeypatch.setattr(
            result_storage_module.os,
            "link",
            lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "private link failure")),
        )
        with pytest.raises(ResultStorageError, match="Failed to persist") as caught:
            storage.store(serialized_result="payload")
        assert caught.value.__context__ is None
        assert list(tmp_path.rglob("*.install-lock")) == []

    def test_filesystem_loader_checks_stat_and_bounds_the_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        reference = _filesystem_reference("payload")
        read_limits: list[int] = []

        class Handle:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, amount: int) -> bytes:
                read_limits.append(amount)
                return b"payload"

        monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=8))
        monkeypatch.setattr(Path, "open", lambda self, mode: Handle())
        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.load(reference=reference)
        assert read_limits == []

        monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=7))
        assert storage.load(reference=reference) == "payload"
        assert read_limits == [8]

    def test_filesystem_reference_must_match_digest_layout_before_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        read_calls = 0

        def _unexpected_read(*args, **kwargs):
            nonlocal read_calls
            read_calls += 1
            return b"payload"

        monkeypatch.setattr(Path, "open", _unexpected_read)
        digest = hashlib.sha256(b"payload").hexdigest()
        reference = f"resultfs://sha256/{digest}?rel=wrong/{digest}.json&bytes=7"

        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            storage.load(reference=reference)
        assert read_calls == 0

    def test_s3_authority_and_prefix_mismatch_make_no_sdk_calls(self) -> None:
        class Client:
            get_calls = 0
            delete_calls = 0

            def get_object(self, **kwargs):
                self.get_calls += 1
                return {"Body": SimpleNamespace(read=lambda: b"payload")}

            def delete_object(self, **kwargs):
                self.delete_calls += 1

        client = Client()
        storage = S3ResultStorage(
            bucket="configured-bucket",
            prefix="configured/results",
            client=client,
        )
        wrong_bucket = _object_reference(
            "payload",
            scheme="s3",
            bucket="other-bucket",
            prefix="configured/results",
        )
        wrong_prefix = _object_reference(
            "payload",
            scheme="s3",
            bucket="configured-bucket",
            prefix="other/results",
        )

        for reference in (wrong_bucket, wrong_prefix):
            with pytest.raises(ResultStorageError, match="Result reference is invalid"):
                storage.load(reference=reference)
            with pytest.raises(ResultStorageError, match="Result reference is invalid"):
                storage.delete(reference=reference)

        assert client.get_calls == 0
        assert client.delete_calls == 0

    def test_gcs_authority_and_prefix_mismatch_make_no_sdk_calls(self) -> None:
        class Blob:
            download_calls = 0
            delete_calls = 0

            def download_as_bytes(self) -> bytes:
                self.download_calls += 1
                return b"payload"

            def delete(self) -> None:
                self.delete_calls += 1

        blob = Blob()
        bucket_calls = 0

        def _bucket(name: str) -> SimpleNamespace:
            nonlocal bucket_calls
            bucket_calls += 1
            return SimpleNamespace(blob=lambda key: blob)

        storage = GCSResultStorage(
            bucket="configured-bucket",
            prefix="configured/results",
            client=SimpleNamespace(bucket=_bucket),
        )
        wrong_bucket = _object_reference(
            "payload",
            scheme="gs",
            bucket="other-bucket",
            prefix="configured/results",
        )
        wrong_prefix = _object_reference(
            "payload",
            scheme="gs",
            bucket="configured-bucket",
            prefix="other/results",
        )

        for reference in (wrong_bucket, wrong_prefix):
            with pytest.raises(ResultStorageError, match="Result reference is invalid"):
                storage.load(reference=reference)
            with pytest.raises(ResultStorageError, match="Result reference is invalid"):
                storage.delete(reference=reference)

        assert bucket_calls == 0
        assert blob.download_calls == 0
        assert blob.delete_calls == 0

    def test_factory_rejects_mismatched_authority_before_sdk_initialization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import_calls = 0

        def _unexpected_import(name: str):
            nonlocal import_calls
            import_calls += 1
            raise AssertionError(name)

        monkeypatch.setattr(result_storage_module.importlib, "import_module", _unexpected_import)
        reference = _object_reference(
            "payload", scheme="s3", bucket="untrusted", prefix="configured/results"
        )

        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            get_result_storage_backend_for_reference(
                reference,
                {
                    "RESULT_STORAGE_S3_BUCKET": "configured",
                    "RESULT_STORAGE_S3_PREFIX": "configured/results",
                },
            )
        assert import_calls == 0

    def test_gcs_factory_rejects_mismatched_prefix_before_sdk_initialization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import_calls = 0

        def _unexpected_import(name: str):
            nonlocal import_calls
            import_calls += 1
            raise AssertionError(name)

        monkeypatch.setattr(result_storage_module.importlib, "import_module", _unexpected_import)
        reference = _object_reference(
            "payload", scheme="gs", bucket="configured", prefix="untrusted/results"
        )

        with pytest.raises(ResultStorageError, match="Result reference is invalid"):
            get_result_storage_backend_for_reference(
                reference,
                {
                    "RESULT_STORAGE_GCS_BUCKET": "configured",
                    "RESULT_STORAGE_GCS_PREFIX": "configured/results",
                },
            )
        assert import_calls == 0

    @pytest.mark.parametrize(("backend", "message"), [("s3", "S3"), ("gcs", "GCS")])
    def test_sdk_initialization_failure_is_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        backend: str,
        message: str,
    ) -> None:
        sensitive = "credential-bearing initialization failure"
        if backend == "s3":
            module = SimpleNamespace(
                client=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(sensitive))
            )
        else:
            module = SimpleNamespace(Client=lambda: (_ for _ in ()).throw(RuntimeError(sensitive)))
        monkeypatch.setattr(
            result_storage_module.importlib,
            "import_module",
            lambda name: module,
        )

        with pytest.raises(ResultStorageError, match=f"initialize {message}") as caught:
            if backend == "s3":
                S3ResultStorage(bucket="bucket")
            else:
                GCSResultStorage(bucket="bucket")
        assert sensitive not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    @pytest.mark.parametrize(("backend", "message"), [("s3", "S3"), ("gcs", "GCS")])
    def test_sdk_import_failure_is_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        backend: str,
        message: str,
    ) -> None:
        sensitive = "credential-bearing import failure"
        monkeypatch.setattr(
            result_storage_module.importlib,
            "import_module",
            lambda name: (_ for _ in ()).throw(RuntimeError(sensitive)),
        )

        with pytest.raises(ResultStorageError, match=f"initialize {message}") as caught:
            if backend == "s3":
                S3ResultStorage(bucket="bucket")
            else:
                GCSResultStorage(bucket="bucket")
        assert sensitive not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_s3_verifies_size_digest_utf8_and_body_type(self) -> None:
        reference = _object_reference("payload", scheme="s3", bucket="bucket")
        bodies: list[object] = [
            b"PAYLOAD",
            b"payload!",
            "payload",
            b"\xff\xfe\xfd\xfc\xfb\xfa\xf9",
        ]

        def _client_for_body(payload: object) -> SimpleNamespace:
            return SimpleNamespace(
                get_object=lambda **kwargs: {
                    "Body": SimpleNamespace(read=lambda amount: payload),
                    "ContentLength": 7,
                }
            )

        for body in bodies:
            storage = S3ResultStorage(
                bucket="bucket",
                client=_client_for_body(body),
            )
            with pytest.raises(ResultStorageError, match="integrity verification"):
                storage.load(reference=reference)

        invalid_utf8 = b"\xff"
        digest = hashlib.sha256(invalid_utf8).hexdigest()
        invalid_utf8_reference = f"s3://bucket/{digest[:2]}/{digest[2:4]}/{digest}.json?bytes=1"
        storage = S3ResultStorage(
            bucket="bucket",
            prefix="",
            client=SimpleNamespace(
                get_object=lambda **kwargs: {
                    "Body": SimpleNamespace(read=lambda amount: invalid_utf8),
                    "ContentLength": 1,
                }
            ),
        )
        with pytest.raises(ResultStorageError, match="integrity verification") as caught:
            storage.load(reference=invalid_utf8_reference)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_s3_checks_provider_size_and_bounds_body_read(self) -> None:
        reference = _object_reference("payload", scheme="s3", bucket="bucket")
        read_limits: list[int] = []
        closed = 0

        class Body:
            def read(self, amount: int) -> bytes:
                read_limits.append(amount)
                return b"payload!"[:amount]

            def close(self) -> None:
                nonlocal closed
                closed += 1

        body = Body()
        client = SimpleNamespace(get_object=lambda **kwargs: {"Body": body, "ContentLength": 8})
        storage = S3ResultStorage(bucket="bucket", client=client)

        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.load(reference=reference)
        assert read_limits == []
        assert closed == 1

        client.get_object = lambda **kwargs: {"Body": body, "ContentLength": 7}
        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.load(reference=reference)
        assert read_limits == [8]
        assert closed == 2

    def test_s3_body_and_close_failures_are_bounded(self) -> None:
        reference = _object_reference("payload", scheme="s3", bucket="bucket")

        class FailingBody:
            def read(self, amount: int) -> bytes:
                raise RuntimeError("private read failure")

            def close(self) -> None:
                raise RuntimeError("private close failure")

        storage = S3ResultStorage(
            bucket="bucket",
            client=SimpleNamespace(
                get_object=lambda **kwargs: {
                    "Body": FailingBody(),
                    "ContentLength": 7,
                }
            ),
        )
        with pytest.raises(ResultStorageError, match="unavailable from S3") as caught:
            storage.load(reference=reference)
        assert caught.value.__context__ is None
        assert "private" not in str(caught.value)

        body = SimpleNamespace(
            read=lambda amount: b"payload"[:amount],
            close=lambda: (_ for _ in ()).throw(RuntimeError("private close failure")),
        )
        storage.client = SimpleNamespace(
            get_object=lambda **kwargs: {"Body": body, "ContentLength": 7}
        )
        assert storage.load(reference=reference) == "payload"

    def test_gcs_verifies_loaded_bytes(self) -> None:
        reference = _object_reference("payload", scheme="gs", bucket="bucket")
        blob = SimpleNamespace(
            reload=lambda: None,
            size=7,
            generation=1,
            download_as_bytes=lambda **kwargs: b"PAYLOAD",
        )
        storage = GCSResultStorage(
            bucket="bucket",
            client=SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: blob)),
        )

        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.load(reference=reference)

    def test_gcs_checks_provider_size_and_bounds_download(self) -> None:
        reference = _object_reference("payload", scheme="gs", bucket="bucket")
        download_ranges: list[tuple[int, int]] = []
        blob = SimpleNamespace(
            reload=lambda: None,
            size=8,
            generation=1,
            download_as_bytes=lambda **kwargs: download_ranges.append(
                (kwargs["start"], kwargs["end"])
            ),
        )
        storage = GCSResultStorage(
            bucket="bucket",
            client=SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: blob)),
        )

        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.load(reference=reference)
        assert download_ranges == []

        blob.size = 7

        def _oversized_download(
            *,
            start: int,
            end: int,
            if_generation_match: int,
        ) -> bytes:
            assert if_generation_match == 1
            download_ranges.append((start, end))
            return b"payload!"

        blob.download_as_bytes = _oversized_download
        with pytest.raises(ResultStorageError, match="integrity verification"):
            storage.load(reference=reference)
        assert download_ranges == [(0, 7)]

    def test_gcs_size_lookup_failure_is_bounded(self) -> None:
        reference = _object_reference("payload", scheme="gs", bucket="bucket")

        class Blob:
            def reload(self) -> None:
                return None

            @property
            def size(self) -> int:
                raise RuntimeError("private metadata failure")

        storage = GCSResultStorage(
            bucket="bucket",
            client=SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: Blob())),
        )
        with pytest.raises(ResultStorageError, match="unavailable from GCS") as caught:
            storage.load(reference=reference)
        assert caught.value.__context__ is None
        assert "private metadata failure" not in str(caught.value)

    @pytest.mark.parametrize("scheme", ["s3", "gs"])
    def test_encoding_legacy_reference_is_authorized_and_can_be_canonicalized(
        self,
        scheme: str,
    ) -> None:
        payload = b"payload"
        digest = hashlib.sha256(payload).hexdigest()
        prefix = "tenant alpha/résults+100%"
        key = f"{prefix}/{digest[:2]}/{digest[2:4]}/{digest}.json"
        reference = f"{scheme}://bucket/{key}?bytes={len(payload)}"
        assert is_valid_result_reference(reference) is False

        if scheme == "s3":
            storage = S3ResultStorage(
                bucket="bucket",
                prefix=prefix,
                client=SimpleNamespace(
                    get_object=lambda **kwargs: {
                        "Body": SimpleNamespace(read=lambda amount: payload[:amount]),
                        "ContentLength": len(payload),
                    }
                ),
            )
            config = {
                "RESULT_STORAGE_S3_BUCKET": "bucket",
                "RESULT_STORAGE_S3_PREFIX": prefix,
            }
        else:
            blob = SimpleNamespace(
                reload=lambda: None,
                size=len(payload),
                generation=1,
                download_as_bytes=lambda **kwargs: payload,
            )
            storage = GCSResultStorage(
                bucket="bucket",
                prefix=prefix,
                client=SimpleNamespace(
                    bucket=lambda name: SimpleNamespace(blob=lambda object_key: blob)
                ),
            )
            config = {
                "RESULT_STORAGE_GCS_BUCKET": "bucket",
                "RESULT_STORAGE_GCS_PREFIX": prefix,
            }

        assert storage.load(reference=reference) == "payload"
        canonical = canonicalize_result_reference(reference, config)
        assert "%20" in canonical
        assert "%C3%A9" in canonical
        assert "%2B" in canonical
        assert "%25" in canonical
        assert is_valid_result_reference(canonical) is True
        assert storage.load(reference=canonical) == "payload"

    def test_escape_like_legacy_prefix_is_resolved_by_retained_configuration(self) -> None:
        payload = b"payload"
        digest = hashlib.sha256(payload).hexdigest()
        prefix = "tenant%25/results"
        key = f"{prefix}/{digest[:2]}/{digest[2:4]}/{digest}.json"
        reference = f"s3://bucket/{key}?bytes={len(payload)}"
        requested_keys: list[str] = []

        def get_object(**kwargs):
            requested_keys.append(kwargs["Key"])
            return {
                "Body": SimpleNamespace(
                    read=lambda amount: payload[:amount],
                    close=lambda: None,
                ),
                "ContentLength": len(payload),
            }

        storage = S3ResultStorage(
            bucket="bucket",
            prefix=prefix,
            client=SimpleNamespace(get_object=get_object),
        )
        config = {
            "RESULT_STORAGE_S3_BUCKET": "bucket",
            "RESULT_STORAGE_S3_PREFIX": prefix,
        }

        assert is_valid_result_reference(reference) is True
        assert storage.load(reference=reference) == "payload"
        canonical = canonicalize_result_reference(reference, config)
        assert canonical != reference
        assert "tenant%2525/results" in canonical
        assert storage.load(reference=canonical) == "payload"
        assert requested_keys == [key, key]

    def test_object_prefix_is_canonically_encoded_and_round_trips(self) -> None:
        objects: dict[tuple[str, str], bytes] = {}

        class Client:
            def put_object(self, **kwargs):
                objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

            def get_object(self, **kwargs):
                payload = objects[(kwargs["Bucket"], kwargs["Key"])]
                return {
                    "Body": SimpleNamespace(read=lambda amount: payload[:amount]),
                    "ContentLength": len(payload),
                }

        storage = S3ResultStorage(
            bucket="bucket",
            prefix="tenant alpha/résults",
            client=Client(),
        )
        reference = storage.store(serialized_result="payload")

        assert "tenant%20alpha/r%C3%A9sults" in reference
        assert is_valid_result_reference(reference) is True
        assert storage.load(reference=reference) == "payload"

    def test_backend_failure_diagnostics_suppress_sensitive_context(self) -> None:
        sensitive = "secret-access-key-and-private-object-reference"
        reference = _object_reference("payload", scheme="s3", bucket="bucket")
        storage = S3ResultStorage(
            bucket="bucket",
            client=SimpleNamespace(
                get_object=lambda **kwargs: (_ for _ in ()).throw(RuntimeError(sensitive))
            ),
        )

        with pytest.raises(ResultStorageError) as caught:
            storage.load(reference=reference)

        formatted = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
        assert sensitive not in str(caught.value)
        assert sensitive not in formatted
        assert reference not in formatted
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert caught.value.__suppress_context__ is True

    @pytest.mark.parametrize(
        ("bucket", "prefix"),
        [
            ("user@bucket", "results"),
            ("bucket:443", "results"),
            ("bucket", "../results"),
            ("bucket", "results//private"),
            ("bucket", "results\\private"),
            ("bucket", "results/\u0080private"),
            ("bucket", "results/\ud800private"),
        ],
        ids=(
            "userinfo-authority",
            "port-authority",
            "parent-segment",
            "empty-segment",
            "backslash",
            "control-character",
            "surrogate",
        ),
    )
    def test_unsafe_storage_configuration_fails_before_sdk_use(
        self, bucket: str, prefix: str
    ) -> None:
        with pytest.raises(ResultStorageError, match="configuration is invalid"):
            S3ResultStorage(bucket=bucket, prefix=prefix, client=object())
