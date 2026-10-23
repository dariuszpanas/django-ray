"""Bounded storage and reservation tests for rq2 Ray Job requests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import traceback
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import QuerySet
from django.utils import timezone

import django_ray.ray_job_request_storage as request_storage
from django_ray.execution_codec import (
    EXECUTION_REQUEST_MAX_BYTES,
    ExecutionIdentity,
    ExecutionRequest,
    encode_execution_request,
)
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
    TaskState,
)
from django_ray.ray_job_request_storage import (
    RAY_JOB_REQUEST_LOCATOR_MAX_CHARS,
    RAY_JOB_REQUEST_REFERENCE_MAX_BYTES,
    RAY_JOB_REQUEST_REFERENCE_PREFIX,
    RayJobRequestLoadError,
    RayJobRequestLocator,
    RayJobRequestStorageError,
    RayJobRequestStorageRejection,
    decode_ray_job_request_locator,
    delete_ray_job_request_reference,
    encode_ray_job_request_locator,
    load_ray_job_request,
    prepare_ray_job_request,
    ray_job_request_reference_content_identity,
    register_and_attach_ray_job_request,
    release_ray_job_request_reservation,
    validate_ray_job_request_storage_config,
)
from django_ray.result_storage import (
    GCSResultStorage,
    ResultStorageError,
    ResultStorageIntegrityError,
    S3ResultStorage,
)
from django_ray.runner.base import SubmissionHandle


def _filesystem_config(root: Path) -> dict[str, Any]:
    return {
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(root),
    }


def _s3_config() -> dict[str, Any]:
    return {
        "INPUT_STORAGE_BACKEND": "s3",
        "INPUT_STORAGE_S3_BUCKET": "request-bucket",
        "INPUT_STORAGE_S3_PREFIX": "requests/rq2",
        "INPUT_STORAGE_S3_REGION": "us-test-1",
        "INPUT_STORAGE_S3_ENDPOINT_URL": "https://objects.example.invalid:9443",
    }


def _gcs_config() -> dict[str, Any]:
    return {
        "INPUT_STORAGE_BACKEND": "gcs",
        "INPUT_STORAGE_GCS_BUCKET": "request-bucket",
        "INPUT_STORAGE_GCS_PREFIX": "requests/rq2",
    }


def _request(
    identity: ExecutionIdentity | None = None,
    *,
    callable_path: str = "testproject.tasks.add_numbers",
    serialized_args: str = "[1]",
    serialized_kwargs: str = '{"flag":true}',
    input_reference: str | None = None,
    runtime_env_profile: str | None = None,
    runtime_env_hash: str = "a" * 64,
) -> ExecutionRequest:
    return ExecutionRequest(
        identity=identity
        or ExecutionIdentity(
            task_execution_pk=1,
            task_id="opaque-public-task",
            attempt_number=1,
            execution_generation=2,
        ),
        execution_protocol_version=1,
        callable_path=callable_path,
        transport_version=2 if input_reference else 1,
        serialized_args="null" if input_reference else serialized_args,
        serialized_kwargs="null" if input_reference else serialized_kwargs,
        input_reference=input_reference,
        runtime_env_profile=runtime_env_profile,
        runtime_env_hash=runtime_env_hash,
        runtime_env_plan_identity={},
        compiled_graph_submission_transport="ray-job",
    )


def _raw_locator_token(serialized: str) -> str:
    return base64.urlsafe_b64encode(serialized.encode("utf-8")).rstrip(b"=").decode("ascii")


def _locator_token(value: object) -> str:
    return _raw_locator_token(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


def _request_path(locator: RayJobRequestLocator) -> Path:
    assert locator.filesystem_path is not None
    return (
        Path(locator.filesystem_path)
        / locator.digest[:2]
        / locator.digest[2:4]
        / f"{locator.digest}.json"
    )


class _MemoryObjectStorage:
    payloads: ClassVar[dict[str, str]] = {}
    constructions: ClassVar[list[dict[str, str | None]]] = []
    scheme: ClassVar[str]

    def _store(self, serialized_payload: str) -> str:
        payload = serialized_payload.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        key = request_storage._build_object_key(self.prefix, digest)
        reference = request_storage._object_reference(
            scheme=self.scheme,
            bucket=self.bucket,
            key=key,
            size_bytes=len(payload),
        )
        self.payloads[reference] = serialized_payload
        return reference

    def store_payload(self, *, serialized_payload: str) -> str:
        return self._store(serialized_payload)

    def load(self, *, reference: str) -> str | None:
        return self.payloads.get(reference)

    def delete(self, *, reference: str) -> None:
        self.payloads.pop(reference, None)


class _MemoryS3Storage(_MemoryObjectStorage):
    scheme = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.constructions.append(
            {
                "bucket": bucket,
                "prefix": prefix,
                "endpoint_url": endpoint_url,
                "region_name": region_name,
            }
        )


class _MemoryGCSStorage(_MemoryObjectStorage):
    scheme = "gs"

    def __init__(self, *, bucket: str, prefix: str) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.constructions.append({"bucket": bucket, "prefix": prefix})


def test_prepare_and_pre_django_load_round_trip(tmp_path: Path) -> None:
    serialized = encode_execution_request(_request())

    prepared = prepare_ray_job_request(serialized, _filesystem_config(tmp_path))
    decoded = decode_ray_job_request_locator(prepared.encoded_locator)
    loaded = load_ray_job_request(decoded)

    assert prepared.reference.startswith("rq2ref:v1:resultfs://sha256/")
    assert prepared.digest == decoded.digest == loaded.digest
    assert prepared.size_bytes == decoded.size_bytes == loaded.size_bytes
    assert prepared.locator_json.encode("utf-8")
    assert len(prepared.locator_json.encode("utf-8")) <= 4096
    assert len(prepared.encoded_locator) <= RAY_JOB_REQUEST_LOCATOR_MAX_CHARS
    assert "opaque-public-task" not in prepared.locator_json
    assert "add_numbers" not in prepared.locator_json
    assert loaded.serialized_request == serialized
    assert loaded.request == _request()


@pytest.mark.parametrize(
    ("backend", "config", "storage_type", "reference_prefix"),
    [
        ("s3", _s3_config(), _MemoryS3Storage, "s3://request-bucket/requests/rq2/"),
        ("gcs", _gcs_config(), _MemoryGCSStorage, "gs://request-bucket/requests/rq2/"),
    ],
)
def test_object_storage_locator_round_trip_uses_only_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    config: dict[str, Any],
    storage_type: type[_MemoryObjectStorage],
    reference_prefix: str,
) -> None:
    storage_type.payloads.clear()
    storage_type.constructions.clear()
    monkeypatch.setattr(
        request_storage,
        "S3ResultStorage" if backend == "s3" else "GCSResultStorage",
        storage_type,
    )
    serialized = encode_execution_request(_request())

    prepared = prepare_ray_job_request(serialized, config)
    decoded = decode_ray_job_request_locator(prepared.encoded_locator)
    loaded = load_ray_job_request(prepared.encoded_locator)

    assert prepared.reference.startswith("rq2ref:v1:" + reference_prefix)
    assert prepared.backend == decoded.backend == backend
    assert loaded.serialized_request == serialized
    assert loaded.request == _request()
    assert len(storage_type.constructions) == 2
    if backend == "s3":
        assert decoded.s3_bucket == "request-bucket"
        assert decoded.s3_prefix == "requests/rq2"
        assert decoded.s3_region == "us-test-1"
        assert decoded.s3_endpoint_url == "https://objects.example.invalid:9443"
        assert storage_type.constructions[-1]["region_name"] == "us-test-1"
    else:
        assert decoded.gcs_bucket == "request-bucket"
        assert decoded.gcs_prefix == "requests/rq2"


def test_config_validation_covers_each_retrievable_backend(tmp_path: Path) -> None:
    validate_ray_job_request_storage_config(_filesystem_config(tmp_path))
    validate_ray_job_request_storage_config(_s3_config())
    validate_ray_job_request_storage_config(_gcs_config())
    validate_ray_job_request_storage_config(
        {
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "request-bucket",
        }
    )
    validate_ray_job_request_storage_config(
        {
            "INPUT_STORAGE_BACKEND": "gcs",
            "INPUT_STORAGE_GCS_BUCKET": "request-bucket",
        }
    )


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"INPUT_STORAGE_BACKEND": "filesystem"},
        {
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": "\ud800",
        },
        {
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": "\u00e9" * 1025,
        },
        {
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "invalid/bucket",
        },
        {
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "request-bucket",
            "INPUT_STORAGE_S3_PREFIX": "/noncanonical",
        },
        {
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "request-bucket",
            "INPUT_STORAGE_S3_REGION": "invalid region",
        },
        {
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "request-bucket",
            "INPUT_STORAGE_S3_ENDPOINT_URL": "https://example.invalid:bad-port",
        },
        {
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "request-bucket",
            "INPUT_STORAGE_S3_ENDPOINT_URL": "http://[",
        },
        {
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "request-bucket",
            "INPUT_STORAGE_S3_ENDPOINT_URL": "https://example.invalid/\n",
        },
        {
            "INPUT_STORAGE_BACKEND": "gcs",
            "INPUT_STORAGE_GCS_BUCKET": "invalid/bucket",
        },
    ],
)
def test_config_validation_rejects_invalid_locations_without_sdk_initialization(
    config: Any,
) -> None:
    with pytest.raises(RayJobRequestStorageError) as caught:
        validate_ray_job_request_storage_config(config)

    assert caught.value.classification is RayJobRequestStorageRejection.CONFIGURATION


def test_reference_bound_is_shared_with_protocol_and_model() -> None:
    from django_ray.ray_job_protocol import (
        RAY_JOB_REQUEST_REFERENCE_MAX_BYTES as PROTOCOL_REFERENCE_MAX_BYTES,
    )

    field = RayTaskExecution._meta.get_field("ray_job_request_reference")
    assert RAY_JOB_REQUEST_REFERENCE_MAX_BYTES == PROTOCOL_REFERENCE_MAX_BYTES == field.max_length


def test_reference_content_identity_is_canonical_bounded_and_retrievable(
    tmp_path: Path,
) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )

    assert ray_job_request_reference_content_identity(prepared.reference) == (
        prepared.digest,
        prepared.size_bytes,
    )
    for reference in (
        "oversize://sha256/" + "a" * 64 + "?bytes=1",
        prepared.reference + "&token=private",
        "x" * (RAY_JOB_REQUEST_REFERENCE_MAX_BYTES + 1),
        prepared.reference.rsplit("=", 1)[0] + "=0",
        prepared.reference.rsplit("=", 1)[0] + f"={EXECUTION_REQUEST_MAX_BYTES + 1}",
    ):
        with pytest.raises(RayJobRequestStorageError) as caught:
            ray_job_request_reference_content_identity(reference)
        assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_locator_is_strict_unpadded_canonical_base64url(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )

    assert "=" not in prepared.encoded_locator
    assert encode_ray_job_request_locator(prepared.locator) == prepared.encoded_locator

    with pytest.raises(RayJobRequestLoadError) as caught:
        decode_ray_job_request_locator(prepared.encoded_locator + "=")
    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_locator_rejects_resource_limit_before_decode() -> None:
    with pytest.raises(RayJobRequestLoadError) as caught:
        decode_ray_job_request_locator("a" * (RAY_JOB_REQUEST_LOCATOR_MAX_CHARS + 1))

    assert caught.value.classification is RayJobRequestStorageRejection.RESOURCE_LIMIT


def test_locator_rejects_noncanonical_base64_as_invalid() -> None:
    # e30 is canonical unpadded base64url for b"{}". e31 decodes to the
    # same bytes but has non-zero unused pad bits and must not be normalized.
    with pytest.raises(RayJobRequestLoadError) as caught:
        decode_ray_job_request_locator("e31")

    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_locator_rejects_duplicate_and_noncanonical_json(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    duplicate = prepared.locator_json.replace(
        '{"backend":',
        '{"backend":"filesystem","backend":',
        1,
    )
    noncanonical = json.dumps(json.loads(prepared.locator_json), indent=2)

    for serialized in (duplicate, noncanonical):
        with pytest.raises(RayJobRequestLoadError) as caught:
            decode_ray_job_request_locator(_raw_locator_token(serialized))
        assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


@pytest.mark.parametrize("field", ["digest", "request_size_bytes"])
def test_locator_cross_checks_reference_digest_and_size(
    tmp_path: Path,
    field: str,
) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    value = json.loads(prepared.locator_json)
    value[field] = "f" * 64 if field == "digest" else value[field] + 1
    encoded = _raw_locator_token(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )

    with pytest.raises(RayJobRequestLoadError) as caught:
        decode_ray_job_request_locator(encoded)

    assert caught.value.classification is RayJobRequestStorageRejection.INTEGRITY_MISMATCH


def test_caller_constructed_locator_is_revalidated_before_backend_io(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    forged = replace(prepared.locator, digest="f" * 64)

    with pytest.raises(RayJobRequestLoadError) as caught:
        load_ray_job_request(forged)

    assert caught.value.classification is RayJobRequestStorageRejection.INTEGRITY_MISMATCH

    with pytest.raises(RayJobRequestLoadError) as extra_field:
        load_ray_job_request(replace(prepared.locator, s3_region="us-test-1"))
    assert extra_field.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_locator_rejects_non_string_token_and_filesystem_reference_from_another_backend(
    tmp_path: Path,
) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    other_backend_reference = request_storage._object_reference(
        scheme="s3",
        bucket="request-bucket",
        key=request_storage._build_object_key("requests/rq2", prepared.digest),
        size_bytes=prepared.size_bytes,
    )
    value = json.loads(prepared.locator_json)
    value["reference"] = other_backend_reference

    with pytest.raises(RayJobRequestLoadError) as wrong_type:
        decode_ray_job_request_locator(None)
    assert wrong_type.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR

    with pytest.raises(RayJobRequestLoadError) as wrong_backend:
        decode_ray_job_request_locator(_locator_token(value))
    assert wrong_backend.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_locator_subclass_cannot_bypass_canonical_dataclass_validation(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )

    class LocatorSubclass(RayJobRequestLocator):
        pass

    forged = LocatorSubclass(
        backend=prepared.locator.backend,
        reference=prepared.locator.reference,
        digest=prepared.locator.digest,
        size_bytes=prepared.locator.size_bytes,
        filesystem_path=prepared.locator.filesystem_path,
    )

    with pytest.raises(RayJobRequestLoadError) as caught:
        load_ray_job_request(forged)

    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_caller_constructed_locator_bounds_path_before_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    resolve_calls = 0

    def unexpected_resolve(self: Path, *, strict: bool = False) -> Path:
        nonlocal resolve_calls
        resolve_calls += 1
        return self

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)

    with pytest.raises(RayJobRequestLoadError) as caught:
        load_ray_job_request(replace(prepared.locator, filesystem_path="x" * 2049))

    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR
    assert resolve_calls == 0


def test_decoded_filesystem_root_must_be_exact_canonical_path(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    value = json.loads(prepared.locator_json)
    value["filesystem_path"] = "."
    encoded = _raw_locator_token(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )

    with pytest.raises(RayJobRequestLoadError) as caught:
        decode_ray_job_request_locator(encoded)

    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_locator_rejects_boolean_schema_version(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    value = json.loads(prepared.locator_json)
    value["version"] = True
    encoded = _raw_locator_token(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )

    with pytest.raises(RayJobRequestLoadError) as caught:
        decode_ray_job_request_locator(encoded)

    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_locator_rejects_malformed_json_and_backend_specific_shapes(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    filesystem = json.loads(prepared.locator_json)
    common = {
        key: filesystem[key]
        for key in (
            "backend",
            "digest",
            "reference",
            "request_size_bytes",
            "schema",
            "version",
        )
    }
    missing_filesystem_path = dict(filesystem)
    missing_filesystem_path.pop("filesystem_path")
    invalid_filesystem_path = {**filesystem, "filesystem_path": "bad\u0000path"}
    s3 = {
        **common,
        "backend": "s3",
        "s3_bucket": "request-bucket",
        "s3_endpoint_url": None,
        "s3_prefix": "requests/rq2",
        "s3_region": None,
    }
    missing_s3_field = dict(s3)
    missing_s3_field.pop("s3_region")
    gcs = {
        **common,
        "backend": "gcs",
        "gcs_bucket": "request-bucket",
        "gcs_prefix": "requests/rq2",
    }
    missing_gcs_field = dict(gcs)
    missing_gcs_field.pop("gcs_prefix")
    invalid_tokens = [
        _raw_locator_token("[]"),
        _raw_locator_token("NaN"),
        _raw_locator_token("1" * 20),
        _locator_token({**filesystem, "backend": 1}),
        _locator_token({**filesystem, "backend": "unknown"}),
        _locator_token(missing_filesystem_path),
        _locator_token(invalid_filesystem_path),
        _locator_token(missing_s3_field),
        _locator_token({**s3, "s3_endpoint_url": "https://user:secret@example.invalid"}),
        _locator_token({**s3, "s3_bucket": ""}),
        _locator_token(s3),
        _locator_token(missing_gcs_field),
        _locator_token({**gcs, "gcs_prefix": "bad\u0000prefix"}),
        _locator_token({**filesystem, "digest": "not-a-digest"}),
        _locator_token({**filesystem, "reference": "x" * 501}),
        base64.urlsafe_b64encode(b"\xff").rstrip(b"=").decode("ascii"),
        "a",
    ]

    for encoded in invalid_tokens:
        with pytest.raises(RayJobRequestLoadError) as caught:
            decode_ray_job_request_locator(encoded)
        assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_filesystem_locator_rejects_resolution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )

    def unavailable_resolve(self: Path, *, strict: bool = False) -> Path:
        raise OSError

    monkeypatch.setattr(Path, "resolve", unavailable_resolve)

    with pytest.raises(RayJobRequestLoadError) as caught:
        decode_ray_job_request_locator(prepared.encoded_locator)

    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


def test_caller_constructed_locator_rejects_invalid_and_aggregate_oversize_fields(
    tmp_path: Path,
) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    invalid_locators: list[object] = [
        object(),
        replace(prepared.locator, digest="bad"),
        replace(
            prepared.locator,
            backend="s3",
            s3_bucket="request-bucket",
            s3_prefix="requests/rq2",
        ),
        replace(
            prepared.locator,
            backend="gcs",
            gcs_bucket="request-bucket",
            gcs_prefix="requests/rq2",
        ),
    ]
    for locator in invalid_locators:
        with pytest.raises(RayJobRequestStorageError) as caught:
            encode_ray_job_request_locator(locator)  # type: ignore[arg-type]
        assert caught.value.classification is RayJobRequestStorageRejection.CONFIGURATION

    aggregate_oversize = replace(
        prepared.locator,
        backend="s3",
        filesystem_path=None,
        s3_bucket="b" * 2048,
        s3_prefix="p" * 2048,
    )
    with pytest.raises(RayJobRequestStorageError) as caught:
        encode_ray_job_request_locator(aggregate_oversize)
    assert caught.value.classification is RayJobRequestStorageRejection.RESOURCE_LIMIT


def test_locator_configuration_carries_no_credential_fields(tmp_path: Path) -> None:
    config = _filesystem_config(tmp_path)
    config.update(
        {
            "AWS_ACCESS_KEY_ID": "ACCESS_KEY_SHOULD_NOT_APPEAR",
            "AWS_SECRET_ACCESS_KEY": "SECRET_SHOULD_NOT_APPEAR",
            "GOOGLE_APPLICATION_CREDENTIALS_JSON": "GCP_SECRET_SHOULD_NOT_APPEAR",
        }
    )

    prepared = prepare_ray_job_request(encode_execution_request(_request()), config)

    assert "ACCESS_KEY_SHOULD_NOT_APPEAR" not in prepared.locator_json
    assert "SECRET_SHOULD_NOT_APPEAR" not in prepared.locator_json
    assert "GCP_SECRET_SHOULD_NOT_APPEAR" not in prepared.locator_json


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:private@example.invalid",
        "https://example.invalid?token=private",
        "https://example.invalid/#private",
    ],
)
def test_storage_configuration_rejects_credential_bearing_endpoint(endpoint: str) -> None:
    config = {
        "INPUT_STORAGE_BACKEND": "s3",
        "INPUT_STORAGE_S3_BUCKET": "requests",
        "INPUT_STORAGE_S3_PREFIX": "django-ray/inputs",
        "INPUT_STORAGE_S3_ENDPOINT_URL": endpoint,
    }

    with pytest.raises(RayJobRequestStorageError) as caught:
        validate_ray_job_request_storage_config(config)

    assert caught.value.classification is RayJobRequestStorageRejection.CONFIGURATION
    formatted = "".join(traceback.format_exception(caught.value))
    assert "private" not in formatted
    assert endpoint not in formatted


def test_storage_configuration_does_not_initialize_object_store_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedS3:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("SDK constructor must not run")

    monkeypatch.setattr(
        "django_ray.ray_job_request_storage.S3ResultStorage",
        UnexpectedS3,
    )

    validate_ray_job_request_storage_config(
        {
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "requests",
            "INPUT_STORAGE_S3_PREFIX": "django-ray/inputs",
        }
    )


def test_storage_configuration_bounds_path_before_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls = 0

    def unexpected_resolve(self: Path, *, strict: bool = False) -> Path:
        nonlocal resolve_calls
        resolve_calls += 1
        return self

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)

    with pytest.raises(RayJobRequestStorageError) as caught:
        validate_ray_job_request_storage_config(
            {
                "INPUT_STORAGE_BACKEND": "filesystem",
                "INPUT_STORAGE_FILESYSTEM_PATH": "x" * 2049,
            }
        )

    assert caught.value.classification is RayJobRequestStorageRejection.CONFIGURATION
    assert resolve_calls == 0


@pytest.mark.parametrize("prefix_size", [2048, 2049])
def test_storage_configuration_bounds_prefix_before_locator_encoding(prefix_size: int) -> None:
    with pytest.raises(RayJobRequestStorageError) as caught:
        validate_ray_job_request_storage_config(
            {
                "INPUT_STORAGE_BACKEND": "s3",
                "INPUT_STORAGE_S3_BUCKET": "requests",
                "INPUT_STORAGE_S3_PREFIX": "x" * prefix_size,
            }
        )

    assert caught.value.classification is RayJobRequestStorageRejection.CONFIGURATION


@pytest.mark.parametrize(
    ("case", "classification"),
    [
        ("invalid", RayJobRequestStorageRejection.INVALID_REQUEST),
        ("oversize", RayJobRequestStorageRejection.RESOURCE_LIMIT),
    ],
)
def test_prepare_rejects_invalid_or_oversize_request_before_storage(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    classification: RayJobRequestStorageRejection,
) -> None:
    store_calls = 0

    def unexpected_store(
        self: _MemoryS3Storage,
        *,
        serialized_payload: str,
    ) -> str:
        nonlocal store_calls
        store_calls += 1
        return self._store(serialized_payload)

    monkeypatch.setattr(_MemoryS3Storage, "store_payload", unexpected_store)
    monkeypatch.setattr(request_storage, "S3ResultStorage", _MemoryS3Storage)
    serialized = None if case == "invalid" else "x" * (EXECUTION_REQUEST_MAX_BYTES + 1)

    with pytest.raises(RayJobRequestStorageError) as caught:
        prepare_ray_job_request(serialized, _s3_config())

    assert caught.value.classification is classification
    assert store_calls == 0


@pytest.mark.parametrize(
    ("error", "classification"),
    [
        (
            ResultStorageIntegrityError("credential-bearing provider response"),
            RayJobRequestStorageRejection.INTEGRITY_MISMATCH,
        ),
        (
            ResultStorageError("credential-bearing provider response"),
            RayJobRequestStorageRejection.STORAGE_UNAVAILABLE,
        ),
        (
            OSError("credential-bearing provider response"),
            RayJobRequestStorageRejection.STORAGE_UNAVAILABLE,
        ),
    ],
)
def test_prepare_maps_storage_write_failures_to_fixed_classifications(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    classification: RayJobRequestStorageRejection,
) -> None:
    def fail_store(self: _MemoryS3Storage, *, serialized_payload: str) -> str:
        raise error

    monkeypatch.setattr(_MemoryS3Storage, "store_payload", fail_store)
    monkeypatch.setattr(request_storage, "S3ResultStorage", _MemoryS3Storage)

    with pytest.raises(RayJobRequestStorageError) as caught:
        prepare_ray_job_request(encode_execution_request(_request()), _s3_config())

    assert caught.value.classification is classification
    assert "credential-bearing" not in str(caught.value)


@pytest.mark.parametrize("failure", ["invalid-reference", "wrong-content"])
def test_prepare_rejects_backend_reference_that_does_not_bind_request_content(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def mismatched_store(self: _MemoryS3Storage, *, serialized_payload: str) -> str:
        if failure == "invalid-reference":
            return "not-a-reference"
        return self._store("{}")

    monkeypatch.setattr(_MemoryS3Storage, "store_payload", mismatched_store)
    monkeypatch.setattr(request_storage, "S3ResultStorage", _MemoryS3Storage)

    with pytest.raises(RayJobRequestStorageError) as caught:
        prepare_ray_job_request(encode_execution_request(_request()), _s3_config())

    assert caught.value.classification is RayJobRequestStorageRejection.INTEGRITY_MISMATCH


@pytest.mark.parametrize(
    ("outcome", "classification"),
    [
        (
            ResultStorageIntegrityError("credential-bearing provider response"),
            RayJobRequestStorageRejection.INTEGRITY_MISMATCH,
        ),
        (
            ResultStorageError("credential-bearing provider response"),
            RayJobRequestStorageRejection.STORAGE_UNAVAILABLE,
        ),
        (None, RayJobRequestStorageRejection.STORAGE_UNAVAILABLE),
        (object(), RayJobRequestStorageRejection.INTEGRITY_MISMATCH),
        ("tampered", RayJobRequestStorageRejection.INTEGRITY_MISMATCH),
    ],
)
def test_load_maps_backend_failures_and_untrusted_payloads(
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
    classification: RayJobRequestStorageRejection,
) -> None:
    _MemoryS3Storage.payloads.clear()
    monkeypatch.setattr(request_storage, "S3ResultStorage", _MemoryS3Storage)
    prepared = prepare_ray_job_request(encode_execution_request(_request()), _s3_config())

    def untrusted_load(self: _MemoryS3Storage, *, reference: str) -> Any:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(_MemoryS3Storage, "load", untrusted_load)

    with pytest.raises(RayJobRequestLoadError) as caught:
        load_ray_job_request(prepared.encoded_locator)

    assert caught.value.classification is classification


def test_backend_initialization_failure_is_fixed_for_prepare_and_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _MemoryS3Storage.payloads.clear()
    monkeypatch.setattr(request_storage, "S3ResultStorage", _MemoryS3Storage)
    prepared = prepare_ray_job_request(encode_execution_request(_request()), _s3_config())

    class UnavailableS3:
        def __init__(self, **kwargs: Any) -> None:
            raise ResultStorageError("credential-bearing provider response")

    monkeypatch.setattr(request_storage, "S3ResultStorage", UnavailableS3)

    with pytest.raises(RayJobRequestStorageError) as prepare_error:
        prepare_ray_job_request(encode_execution_request(_request()), _s3_config())
    assert prepare_error.value.classification is RayJobRequestStorageRejection.CONFIGURATION

    with pytest.raises(RayJobRequestLoadError) as load_error:
        load_ray_job_request(prepared.encoded_locator)
    assert load_error.value.classification is RayJobRequestStorageRejection.CONFIGURATION


def test_missing_and_replaced_payloads_fail_closed(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    path = _request_path(prepared.locator)
    path.unlink()

    with pytest.raises(RayJobRequestLoadError) as missing:
        load_ray_job_request(prepared.encoded_locator)
    assert missing.value.classification is RayJobRequestStorageRejection.STORAGE_UNAVAILABLE

    prepare_ray_job_request(prepared.serialized_request, _filesystem_config(tmp_path))
    replacement = b"x" * prepared.size_bytes
    path.write_bytes(replacement)
    with pytest.raises(RayJobRequestLoadError) as replaced:
        load_ray_job_request(prepared.encoded_locator)
    assert replaced.value.classification is RayJobRequestStorageRejection.INTEGRITY_MISMATCH


def test_canonical_non_request_bytes_fail_before_bootstrap(tmp_path: Path) -> None:
    from django_ray.result_storage import FilesystemResultStorage, _parse_result_reference

    storage = FilesystemResultStorage(tmp_path)
    reference = storage.store_payload(serialized_payload="{}")
    metadata = _parse_result_reference(reference)
    locator = RayJobRequestLocator(
        backend="filesystem",
        reference=reference,
        digest=metadata.digest,
        size_bytes=metadata.size_bytes,
        filesystem_path=str(storage.root_path),
    )

    with pytest.raises(RayJobRequestLoadError) as caught:
        load_ray_job_request(encode_ray_job_request_locator(locator))

    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_REQUEST


def test_module_and_loader_do_not_import_django(tmp_path: Path) -> None:
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()),
        _filesystem_config(tmp_path),
    )
    script = """
import sys
assert "django" not in sys.modules
from django_ray.ray_job_request_storage import load_ray_job_request
assert "django" not in sys.modules
loaded = load_ray_job_request(sys.argv[1])
assert loaded.request.identity.task_id == "opaque-public-task"
assert "django" not in sys.modules
assert "django_ray.models" not in sys.modules
"""
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)

    result = subprocess.run(
        [sys.executable, "-c", script, prepared.encoded_locator],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr


def _reserved_execution() -> tuple[RayTaskExecution, SubmissionHandle]:
    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
        coordination_sha256,
    )

    address = "http://ray-head:8265"
    execution = RayTaskExecution.objects.create(
        task_id="reserved-public-task",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=2,
        execution_protocol_version=1,
        claimed_by_worker="rq2-worker",
        ray_address=address,
        args_json="[1]",
        kwargs_json='{"flag":true}',
        runtime_env_profile=None,
        runtime_env_hash="a" * 64,
    )
    identity = ExecutionIdentity(
        task_execution_pk=int(execution.pk),
        task_id=execution.task_id,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    job_id = (
        f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{coordination_sha256(identity)}"
    )
    execution.ray_job_id = job_id
    execution.save(update_fields=["ray_job_id"])
    return execution, SubmissionHandle(
        ray_job_id=job_id,
        ray_address=address,
        submitted_at=datetime.now(UTC),
    )


def _prepared_for_execution(
    execution: RayTaskExecution,
    root: Path,
) -> Any:
    identity = ExecutionIdentity(
        task_execution_pk=int(execution.pk),
        task_id=execution.task_id,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    return prepare_ray_job_request(
        encode_execution_request(
            _request(
                identity,
                callable_path=execution.callable_path,
                serialized_args=execution.args_json,
                serialized_kwargs=execution.kwargs_json,
                input_reference=execution.input_reference,
                runtime_env_profile=execution.runtime_env_profile,
                runtime_env_hash=execution.runtime_env_hash,
            )
        ),
        _filesystem_config(root),
    )


def test_register_rejects_non_prepared_value_before_orm_access() -> None:
    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            object(),  # type: ignore[arg-type]
            task_execution=object(),  # type: ignore[arg-type]
            submission_handle=object(),  # type: ignore[arg-type]
        )

    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_REQUEST


@pytest.mark.django_db(transaction=True)
def test_register_locks_registry_before_execution_and_attaches_exact_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    lock_order: list[type[Any]] = []
    original = QuerySet.select_for_update

    def tracked_select_for_update(self, *args: Any, **kwargs: Any):
        if self.model in {TaskInputPayload, RayTaskExecution}:
            lock_order.append(self.model)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", tracked_select_for_update)

    reference = register_and_attach_ray_job_request(
        prepared,
        task_execution=execution,
        submission_handle=handle,
    )

    execution.refresh_from_db()
    payload = TaskInputPayload.objects.get(pk=reference)
    assert lock_order[:3] == [TaskInputPayload, TaskInputPayload, RayTaskExecution]
    assert payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST
    assert payload.state == InputPayloadState.ACTIVE
    assert execution.ray_job_request_reference == reference


@pytest.mark.django_db(transaction=True)
def test_register_is_idempotent_and_reactivates_under_registry_lock(tmp_path: Path) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    TaskInputPayload.objects.create(
        reference=prepared.reference,
        payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
        state=InputPayloadState.PURGED,
    )
    _request_path(prepared.locator).unlink()

    first = register_and_attach_ray_job_request(
        prepared,
        task_execution=execution,
        submission_handle=handle,
    )
    second = register_and_attach_ray_job_request(
        prepared,
        task_execution=execution,
        submission_handle=handle,
    )

    payload = TaskInputPayload.objects.get(pk=prepared.reference)
    assert first == second == prepared.reference
    assert payload.state == InputPayloadState.ACTIVE
    assert _request_path(prepared.locator).is_file()


@pytest.mark.parametrize(
    ("outcome", "classification"),
    [
        (
            ResultStorageIntegrityError("credential-bearing provider response"),
            RayJobRequestStorageRejection.INTEGRITY_MISMATCH,
        ),
        (
            ResultStorageError("credential-bearing provider response"),
            RayJobRequestStorageRejection.STORAGE_UNAVAILABLE,
        ),
        ("different-reference", RayJobRequestStorageRejection.INTEGRITY_MISMATCH),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_register_fails_closed_when_purged_request_cannot_be_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
    classification: RayJobRequestStorageRejection,
) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    TaskInputPayload.objects.create(
        reference=prepared.reference,
        payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
        state=InputPayloadState.PURGED,
    )
    _request_path(prepared.locator).unlink()

    class RestoreStorage:
        def __init__(self, root_path: str) -> None:
            self.root_path = root_path

        def load(self, *, reference: str) -> None:
            return None

        def store_payload(self, *, serialized_payload: str) -> str:
            if isinstance(outcome, Exception):
                raise outcome
            return str(outcome)

    monkeypatch.setattr(request_storage, "FilesystemResultStorage", RestoreStorage)

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=handle,
        )

    assert caught.value.classification is classification
    payload = TaskInputPayload.objects.get(pk=prepared.reference)
    assert payload.state == InputPayloadState.PURGED
    execution.refresh_from_db()
    assert execution.ray_job_request_reference is None


@pytest.mark.django_db(transaction=True)
def test_register_rejects_cross_kind_registry_collision(tmp_path: Path) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    TaskInputPayload.objects.create(
        reference=prepared.reference,
        payload_kind=InputPayloadKind.TASK_INPUT,
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
    )

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=handle,
        )

    assert caught.value.classification is RayJobRequestStorageRejection.REGISTRY_MISMATCH
    execution.refresh_from_db()
    assert execution.ray_job_request_reference is None


@pytest.mark.django_db(transaction=True)
def test_register_revalidates_caller_constructed_prepared_request(tmp_path: Path) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    forged = replace(prepared, digest="f" * 64)

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            forged,
            task_execution=execution,
            submission_handle=handle,
        )

    assert caught.value.classification is RayJobRequestStorageRejection.INTEGRITY_MISMATCH
    assert not TaskInputPayload.objects.filter(pk=prepared.reference).exists()


@pytest.mark.parametrize("tamper", ["locator-json", "serialized-request", "locator"])
@pytest.mark.django_db(transaction=True)
def test_register_rejects_bounded_prepared_object_internal_inconsistency(
    tmp_path: Path,
    tamper: str,
) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    if tamper == "locator-json":
        forged = replace(prepared, locator_json="x" * 4097)
        expected = RayJobRequestStorageRejection.INTEGRITY_MISMATCH
    elif tamper == "serialized-request":
        forged = replace(prepared, serialized_request="{}")
        expected = RayJobRequestStorageRejection.INVALID_REQUEST
    else:
        forged = replace(prepared, locator=replace(prepared.locator, digest="bad"))
        expected = RayJobRequestStorageRejection.INTEGRITY_MISMATCH

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            forged,
            task_execution=execution,
            submission_handle=handle,
        )

    assert caught.value.classification is expected
    assert not TaskInputPayload.objects.filter(pk=prepared.reference).exists()


@pytest.mark.django_db(transaction=True)
def test_register_rejects_invalid_in_memory_reservation_before_registry_write(
    tmp_path: Path,
) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    execution.claimed_by_worker = ""

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=handle,
        )

    assert caught.value.classification is RayJobRequestStorageRejection.BINDING_MISMATCH
    assert not TaskInputPayload.objects.filter(pk=prepared.reference).exists()


@pytest.mark.django_db(transaction=True)
def test_register_rejects_a_canonical_job_id_bound_to_another_identity(
    tmp_path: Path,
) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    replacement_last_character = "0" if handle.ray_job_id[-1] != "0" else "1"
    wrong_job_id = handle.ray_job_id[:-1] + replacement_last_character
    wrong_handle = replace(handle, ray_job_id=wrong_job_id)
    execution.ray_job_id = wrong_job_id
    execution.save(update_fields=["ray_job_id"])

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=wrong_handle,
        )

    assert caught.value.classification is RayJobRequestStorageRejection.BINDING_MISMATCH
    assert not TaskInputPayload.objects.filter(pk=prepared.reference).exists()


@pytest.mark.django_db(transaction=True)
def test_register_rejects_database_alias_mismatch_before_registry_write(tmp_path: Path) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=handle,
            using="other",
        )

    assert caught.value.classification is RayJobRequestStorageRejection.BINDING_MISMATCH
    assert not TaskInputPayload.objects.filter(pk=prepared.reference).exists()


@pytest.mark.django_db(transaction=True)
def test_register_rolls_back_registry_if_reserved_execution_disappears(tmp_path: Path) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    RayTaskExecution.objects.filter(pk=execution.pk).delete()

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=handle,
        )

    assert caught.value.classification is RayJobRequestStorageRejection.BINDING_MISMATCH
    assert not TaskInputPayload.objects.filter(pk=prepared.reference).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", TaskState.CANCELLED),
        ("claimed_by_worker", "replacement-worker"),
        ("ray_job_id", "raysubmit_django_ray_rq2_" + "2" * 64),
        ("ray_address", "http://replacement-ray:8265"),
        ("callable_path", "testproject.tasks.different"),
        ("args_json", "[2]"),
        ("runtime_env_hash", "b" * 64),
        ("ray_job_request_reference", "resultfs://sha256/replaced?bytes=1"),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_register_rejects_cancelled_transferred_or_replaced_reservation(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    RayTaskExecution.objects.filter(pk=execution.pk).update(**{field: value})

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=handle,
        )

    assert caught.value.classification is RayJobRequestStorageRejection.BINDING_MISMATCH
    assert not TaskInputPayload.objects.filter(pk=prepared.reference).exists()


@pytest.mark.django_db(transaction=True)
def test_release_locks_registry_before_execution_and_retains_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    register_and_attach_ray_job_request(
        prepared,
        task_execution=execution,
        submission_handle=handle,
    )
    lock_order: list[type[Any]] = []
    original = QuerySet.select_for_update

    def tracked_select_for_update(self, *args: Any, **kwargs: Any):
        if self.model in {TaskInputPayload, RayTaskExecution}:
            lock_order.append(self.model)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", tracked_select_for_update)

    released = release_ray_job_request_reservation(
        execution,
        handle,
        expected_reference=prepared.reference,
    )

    execution.refresh_from_db()
    assert released is True
    assert lock_order[:2] == [TaskInputPayload, RayTaskExecution]
    assert execution.ray_job_id is None
    assert execution.ray_address is None
    assert execution.ray_job_request_reference is None
    assert TaskInputPayload.objects.filter(pk=prepared.reference).exists()
    assert _request_path(prepared.locator).is_file()


@pytest.mark.django_db(transaction=True)
def test_release_without_attached_reference_clears_exact_reserved_tuple() -> None:
    execution, handle = _reserved_execution()

    released = release_ray_job_request_reservation(
        execution,
        handle,
        expected_reference=None,
    )

    execution.refresh_from_db()
    assert released is True
    assert execution.ray_job_id is None
    assert execution.ray_address is None
    assert execution.ray_job_request_reference is None


@pytest.mark.django_db(transaction=True)
def test_release_refuses_stale_reservation_and_preserves_tuple(tmp_path: Path) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    register_and_attach_ray_job_request(
        prepared,
        task_execution=execution,
        submission_handle=handle,
    )
    RayTaskExecution.objects.filter(pk=execution.pk).update(claimed_by_worker="replacement-worker")

    released = release_ray_job_request_reservation(
        execution,
        handle,
        expected_reference=prepared.reference,
    )

    execution.refresh_from_db()
    assert released is False
    assert execution.ray_job_id == handle.ray_job_id
    assert execution.ray_address == handle.ray_address
    assert execution.ray_job_request_reference == prepared.reference


@pytest.mark.django_db(transaction=True)
def test_release_rejects_invalid_handle_or_persisted_reference_without_writes() -> None:
    execution, handle = _reserved_execution()
    invalid_handle = replace(handle, ray_job_id="not-an-rq2-job-id")

    assert (
        release_ray_job_request_reservation(
            execution,
            invalid_handle,
            expected_reference=None,
        )
        is False
    )

    for reference in (
        "not-a-reference",
        "oversize://sha256/" + "a" * 64 + "?bytes=1",
    ):
        execution.ray_job_request_reference = reference
        assert (
            release_ray_job_request_reservation(
                execution,
                handle,
                expected_reference=reference,
            )
            is False
        )

    execution.refresh_from_db()
    assert execution.ray_job_id == handle.ray_job_id
    assert execution.ray_address == handle.ray_address
    assert execution.ray_job_request_reference is None


@pytest.mark.django_db(transaction=True)
def test_release_requires_typed_registry_row_for_attached_reference(tmp_path: Path) -> None:
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        ray_job_request_reference=prepared.reference
    )
    execution.ray_job_request_reference = prepared.reference

    released = release_ray_job_request_reservation(
        execution,
        handle,
        expected_reference=prepared.reference,
    )

    assert released is False
    execution.refresh_from_db()
    assert execution.ray_job_id == handle.ray_job_id
    assert execution.ray_address == handle.ray_address
    assert execution.ray_job_request_reference == prepared.reference


@pytest.mark.parametrize("backend", ["filesystem", "s3", "gcs"])
def test_native_historical_request_locator_remains_readable(tmp_path, monkeypatch, backend):
    config = _filesystem_config(tmp_path)
    if backend != "filesystem":
        storage_type = _MemoryS3Storage if backend == "s3" else _MemoryGCSStorage
        storage_type.payloads.clear()
        monkeypatch.setattr(
            request_storage,
            "S3ResultStorage" if backend == "s3" else "GCSResultStorage",
            storage_type,
        )
        config = _s3_config() if backend == "s3" else _gcs_config()
    prepared = prepare_ray_job_request(encode_execution_request(_request()), config)
    assert prepared.reference.startswith(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    native = prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    historical = replace(prepared.locator, reference=native)
    loaded = load_ray_job_request(encode_ray_job_request_locator(historical))
    assert loaded.serialized_request == prepared.serialized_request
    assert loaded.reference == native
    assert loaded.request == prepared.request


@pytest.mark.parametrize(
    "prefix",
    [
        "rq2ref:v2:",
        "RQ2REF:v1:",
        "rq2ref:V1:",
        "rq2ref:v1:rq2ref:v1:",
        "rq2ref:v1: ",
        "rq2ref:v1:\n",
    ],
)
def test_discriminator_rejects_noncanonical_and_nested_forms_before_storage(
    tmp_path, monkeypatch, prefix
):
    prepared = prepare_ray_job_request(
        encode_execution_request(_request()), _filesystem_config(tmp_path)
    )
    value = json.loads(prepared.locator_json)
    value["reference"] = prefix + prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    monkeypatch.setattr(
        request_storage, "_storage_for_locator", lambda *_: pytest.fail("storage I/O")
    )
    with pytest.raises(RayJobRequestLoadError) as caught:
        load_ray_job_request(_locator_token(value))
    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_LOCATOR


@pytest.mark.parametrize("scheme", ["s3", "gs"])
def test_discriminator_reference_limit_includes_prefix_and_encoded_location(scheme):
    digest = "a" * 64
    base = request_storage._object_reference(
        scheme=scheme,
        bucket="bucket",
        key=request_storage._build_object_key("", digest),
        size_bytes=1,
    )
    room = (
        RAY_JOB_REQUEST_REFERENCE_MAX_BYTES - len(RAY_JOB_REQUEST_REFERENCE_PREFIX) - len(base) - 1
    )
    native = request_storage._object_reference(
        scheme=scheme,
        bucket="bucket",
        key=request_storage._build_object_key("p" * room, digest),
        size_bytes=1,
    )
    wrapped = request_storage._wrap_request_reference(native)
    assert len(wrapped.encode()) == RAY_JOB_REQUEST_REFERENCE_MAX_BYTES
    assert ray_job_request_reference_content_identity(wrapped) == (digest, 1)
    with pytest.raises(RayJobRequestStorageError):
        request_storage._wrap_request_reference(native.replace("/p", "/pp", 1))
    # A native historical value can still use the entire old ceiling.
    historical = native.replace("/p", "/" + "p" * (len(RAY_JOB_REQUEST_REFERENCE_PREFIX) + 1), 1)
    assert len(historical) == RAY_JOB_REQUEST_REFERENCE_MAX_BYTES
    assert ray_job_request_reference_content_identity(historical) == (digest, 1)
    encoded = request_storage._object_reference(
        scheme=scheme,
        bucket="bucket",
        key=request_storage._build_object_key("\u00e9", digest),
        size_bytes=1,
    )
    assert "%C3%A9" in request_storage._wrap_request_reference(encoded)
    with pytest.raises(RayJobRequestStorageError):
        ray_job_request_reference_content_identity(
            RAY_JOB_REQUEST_REFERENCE_PREFIX + encoded.replace("%C3%A9", "\u00e9")
        )


def test_discriminator_configuration_budget_is_checked_before_backend_construction(monkeypatch):
    monkeypatch.setattr(
        request_storage, "S3ResultStorage", lambda **_: pytest.fail("backend construction")
    )
    config = {**_s3_config(), "INPUT_STORAGE_S3_PREFIX": "p" * 500}
    with pytest.raises(RayJobRequestStorageError) as caught:
        prepare_ray_job_request(encode_execution_request(_request()), config)
    assert caught.value.classification is RayJobRequestStorageRejection.CONFIGURATION


def test_generic_input_deletion_refuses_wrapped_request_before_selecting_backend(
    tmp_path, monkeypatch
):
    import django_ray.input_storage as input_storage

    config = _filesystem_config(tmp_path)
    prepared = prepare_ray_job_request(encode_execution_request(_request()), config)
    monkeypatch.setattr(
        input_storage, "_storage_backend_for_reference", lambda *_: pytest.fail("backend selection")
    )
    with pytest.raises(input_storage.InputPayloadValidationError):
        input_storage.delete_input_reference(prepared.reference, config)
    assert load_ray_job_request(prepared.encoded_locator).request == prepared.request


@pytest.mark.parametrize("historical", [False, True])
def test_typed_request_deletion_accepts_only_authorized_current_or_historical_location(
    tmp_path, historical
):
    config = _filesystem_config(tmp_path)
    prepared = prepare_ray_job_request(encode_execution_request(_request()), config)
    reference = (
        prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
        if historical
        else prepared.reference
    )
    delete_ray_job_request_reference(reference, config)
    with pytest.raises(RayJobRequestLoadError) as caught:
        load_ray_job_request(prepared.encoded_locator)
    assert caught.value.classification is RayJobRequestStorageRejection.STORAGE_UNAVAILABLE


@pytest.mark.parametrize("backend", ["s3", "gcs"])
@pytest.mark.parametrize("historical", [False, True])
def test_typed_object_request_delete_authorizes_location_before_client(
    tmp_path, monkeypatch, backend, historical
):
    import django_ray.input_storage as input_storage

    storage_type = _MemoryS3Storage if backend == "s3" else _MemoryGCSStorage
    storage_name = "S3ResultStorage" if backend == "s3" else "GCSResultStorage"
    storage_type.payloads.clear()
    monkeypatch.setattr(request_storage, storage_name, storage_type)
    config = _s3_config() if backend == "s3" else _gcs_config()
    prepared = prepare_ray_job_request(encode_execution_request(_request()), config)
    reference = (
        prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
        if historical
        else prepared.reference
    )
    monkeypatch.setattr(input_storage, storage_name, lambda **_: pytest.fail("unauthorized client"))
    field = "INPUT_STORAGE_S3_BUCKET" if backend == "s3" else "INPUT_STORAGE_GCS_BUCKET"
    with pytest.raises(input_storage.InputPayloadValidationError):
        delete_ray_job_request_reference(reference, {**config, field: "other-bucket"})
    assert load_ray_job_request(prepared.encoded_locator).request == prepared.request
    monkeypatch.setattr(input_storage, storage_name, storage_type)
    delete_ray_job_request_reference(reference, config)
    assert not storage_type.payloads


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", [InputPayloadState.ACTIVE, InputPayloadState.PURGED])
@pytest.mark.parametrize("kind", [InputPayloadKind.TASK_INPUT, InputPayloadKind.RAY_JOB_REQUEST])
def test_current_attachment_refuses_native_registry_alias_without_mutation(tmp_path, state, kind):
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    native = prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    alias = TaskInputPayload.objects.create(
        reference=native,
        payload_kind=kind,
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
        state=state,
    )
    before = TaskInputPayload.objects.values().get(pk=alias.pk)
    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared, task_execution=execution, submission_handle=handle
        )
    assert caught.value.classification is RayJobRequestStorageRejection.REGISTRY_MISMATCH
    assert TaskInputPayload.objects.values().get(pk=alias.pk) == before
    assert not TaskInputPayload.objects.filter(pk=prepared.reference).exists()
    execution.refresh_from_db()
    assert execution.ray_job_request_reference is None
    assert load_ray_job_request(prepared.encoded_locator).request == prepared.request


@pytest.mark.django_db(transaction=True)
def test_new_registration_refuses_native_prepared_request_but_keeps_history_readable(tmp_path):
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    native = prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    locator = replace(prepared.locator, reference=native)
    legacy = replace(
        prepared,
        reference=native,
        locator=locator,
        locator_json=request_storage.serialize_ray_job_request_locator(locator),
        encoded_locator=encode_ray_job_request_locator(locator),
    )
    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            legacy, task_execution=execution, submission_handle=handle
        )
    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_REQUEST
    assert not TaskInputPayload.objects.exists()
    assert load_ray_job_request(legacy.encoded_locator).request == prepared.request


@pytest.mark.django_db(transaction=True)
def test_wrapped_request_purge_deletes_only_old_terminal_payload_and_keeps_reference(
    tmp_path, monkeypatch
):
    import django_ray.input_storage as input_storage

    config = _filesystem_config(tmp_path)
    monkeypatch.setattr(input_storage, "get_settings", lambda: config)
    execution, handle = _reserved_execution()
    prepared = _prepared_for_execution(execution, tmp_path)
    register_and_attach_ray_job_request(
        prepared, task_execution=execution, submission_handle=handle
    )
    old = timezone.now() - timedelta(days=60)
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        state=TaskState.SUCCEEDED, finished_at=old
    )
    TaskInputPayload.objects.filter(pk=prepared.reference).update(last_used_at=old)
    output = StringIO()
    call_command("django_ray_purge_inputs", delete=True, format="json", stdout=output)
    assert json.loads(output.getvalue())["counts"]["purged"] == 1
    assert prepared.reference not in output.getvalue()
    assert TaskInputPayload.objects.get(pk=prepared.reference).state == InputPayloadState.PURGED
    execution.refresh_from_db()
    assert execution.ray_job_request_reference == prepared.reference
    with pytest.raises(RayJobRequestLoadError):
        load_ray_job_request(prepared.encoded_locator)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize(
    "native_kind", [InputPayloadKind.TASK_INPUT, InputPayloadKind.RAY_JOB_REQUEST]
)
@pytest.mark.parametrize("wrapped_state", [InputPayloadState.ACTIVE, InputPayloadState.PURGED])
def test_purge_retains_duplicate_native_and_wrapped_registry_identities(
    tmp_path, monkeypatch, delete, native_kind, wrapped_state
):
    import django_ray.input_storage as input_storage

    prepared = prepare_ray_job_request(
        encode_execution_request(_request()), _filesystem_config(tmp_path)
    )
    native = prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    for reference in (native, prepared.reference):
        TaskInputPayload.objects.create(
            reference=reference,
            payload_kind=native_kind if reference == native else InputPayloadKind.RAY_JOB_REQUEST,
            state=InputPayloadState.ACTIVE if reference == native else wrapped_state,
            backend=prepared.backend,
            digest=prepared.digest,
            size_bytes=prepared.size_bytes,
            envelope_version=prepared.envelope_version,
            last_used_at=timezone.now() - timedelta(days=60),
        )
    monkeypatch.setattr(
        input_storage, "delete_input_reference", lambda *_: pytest.fail("ambiguous deletion")
    )
    before = list(TaskInputPayload.objects.order_by("reference").values())
    output = StringIO()
    with pytest.raises(CommandError) as caught:
        call_command("django_ray_purge_inputs", delete=delete, format="json", stdout=output)
    assert caught.value.returncode == 2
    report = json.loads(output.getvalue())
    expected = 2 if wrapped_state == InputPayloadState.ACTIVE else 1
    assert report["counts"]["incomplete"] == report["counts"]["retained"] == expected
    assert report["counts"]["purged"] == 0
    assert native not in output.getvalue()
    assert list(TaskInputPayload.objects.order_by("reference").values()) == before
    assert load_ray_job_request(prepared.encoded_locator).request == prepared.request


def _reused_request_registration(monkeypatch, *, state):
    """Use a real prepared request with only provider and ORM seams replaced."""
    data = {}

    def store_payload(*, serialized_payload):
        encoded = serialized_payload.encode()
        digest = hashlib.sha256(encoded).hexdigest()
        reference = (
            f"resultfs://sha256/{digest}?rel={digest[:2]}/{digest[2:4]}/{digest}.json"
            f"&bytes={len(encoded)}"
        )
        data[reference] = serialized_payload
        return reference

    storage = Mock()
    storage.root_path = Path("unused-request-root").resolve()
    storage.load.return_value = None
    storage.store_payload.side_effect = store_payload
    monkeypatch.setattr(request_storage, "FilesystemResultStorage", Mock(return_value=storage))
    request = _request()
    prepared = prepare_ray_job_request(
        encode_execution_request(request), _filesystem_config(Path("unused-request-root"))
    )
    storage.store_payload.reset_mock()
    data.clear()
    current, handle, payload, registry, executions = _reused_request_rows(
        monkeypatch, prepared, state=state
    )
    return prepared, current, handle, payload, storage, data, registry, executions


def _reused_request_rows(monkeypatch, prepared, *, state):
    """Replace ORM boundaries while retaining request and execution binding checks."""
    from django.db import transaction

    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
        coordination_sha256,
    )

    request = prepared.request
    identity = request.identity
    job_id = (
        f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{coordination_sha256(identity)}"
    )
    current = RayTaskExecution(
        pk=identity.task_execution_pk,
        task_id=identity.task_id,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        execution_protocol_version=request.execution_protocol_version,
        state=TaskState.RUNNING,
        claimed_by_worker="rq2-worker",
        ray_job_id=job_id,
        ray_address="http://unused-ray-head:8265",
        callable_path=request.callable_path,
        args_json=request.serialized_args,
        kwargs_json=request.serialized_kwargs,
        input_reference=request.input_reference,
        runtime_env_profile=request.runtime_env_profile,
        runtime_env_hash=request.runtime_env_hash,
    )
    current._state.db = "default"
    current.save = Mock()
    handle = SubmissionHandle(
        ray_job_id=job_id,
        ray_address=current.ray_address,
        submitted_at=datetime.now(UTC),
    )
    payload = TaskInputPayload(
        reference=prepared.reference,
        payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
        state=state,
    )
    payload.save = Mock()
    registry = Mock()
    for method in ("using", "select_for_update", "filter", "values_list"):
        getattr(registry, method).return_value = registry
    registry.first.return_value = None
    registry.get_or_create.return_value = (payload, False)
    executions = Mock()
    for method in ("using", "select_for_update", "filter", "only"):
        getattr(executions, method).return_value = executions
    executions.first.return_value = current
    monkeypatch.setattr(TaskInputPayload, "objects", registry)
    monkeypatch.setattr(RayTaskExecution, "objects", executions)
    monkeypatch.setattr(transaction, "atomic", lambda **_kwargs: nullcontext())
    return current, handle, payload, registry, executions


@pytest.mark.parametrize("state", [InputPayloadState.ACTIVE, InputPayloadState.PURGED])
def test_reused_request_revalidates_missing_payload_before_execution_lock(monkeypatch, state):
    prepared, current, handle, payload, storage, data, registry, executions = (
        _reused_request_registration(monkeypatch, state=state)
    )
    previous_use = payload.last_used_at
    original_store = storage.store_payload.side_effect

    def restore(*, serialized_payload):
        assert registry.select_for_update.call_count == 2
        registry.get_or_create.assert_called_once()
        executions.select_for_update.assert_not_called()
        payload.save.assert_not_called()
        assert payload.last_used_at == previous_use
        return original_store(serialized_payload=serialized_payload)

    storage.store_payload.side_effect = restore
    assert (
        register_and_attach_ray_job_request(
            prepared, task_execution=current, submission_handle=handle
        )
        == prepared.reference
    )
    native = prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    assert data[native] == prepared.serialized_request
    assert current.ray_job_request_reference == prepared.reference
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.last_used_at >= previous_use
    current.save.assert_called_once()


@pytest.mark.parametrize("state", [InputPayloadState.ACTIVE, InputPayloadState.PURGED])
@pytest.mark.parametrize(
    ("outcome", "classification"),
    [
        (ResultStorageError("private"), RayJobRequestStorageRejection.STORAGE_UNAVAILABLE),
        (ResultStorageIntegrityError("private"), RayJobRequestStorageRejection.INTEGRITY_MISMATCH),
        ("different-reference", RayJobRequestStorageRejection.INTEGRITY_MISMATCH),
    ],
)
def test_reused_request_failure_preserves_registry_and_reservation(
    monkeypatch, state, outcome, classification
):
    prepared, current, handle, payload, storage, _data, _registry, executions = (
        _reused_request_registration(monkeypatch, state=state)
    )
    previous_use = payload.last_used_at
    if isinstance(outcome, Exception):
        storage.store_payload.side_effect = outcome
    else:
        storage.store_payload.side_effect = lambda **_kwargs: outcome

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared, task_execution=current, submission_handle=handle
        )

    assert caught.value.classification is classification
    storage.store_payload.assert_called_once_with(serialized_payload=prepared.serialized_request)
    assert payload.state == state
    assert payload.last_used_at == previous_use
    assert current.ray_job_request_reference is None
    payload.save.assert_not_called()
    current.save.assert_not_called()
    executions.select_for_update.assert_not_called()


def _reused_cloud_request_registration(monkeypatch, *, backend, state, outcome):
    """Retain real cloud validation and conditional writes with bounded fake clients."""
    prefix = "tenant%25/inputs"
    objects = {}
    phase = {"guard": False, "reads": 0}
    client = Mock()

    class PreconditionFailedError(Exception):
        code = 412

    def require_registry_lock():
        if not phase["guard"]:
            return
        assert registry.select_for_update.call_count == 2
        registry.get_or_create.assert_called_once()
        executions.select_for_update.assert_not_called()
        payload.save.assert_not_called()
        assert payload.last_used_at == previous_use

    def read_object(key):
        require_registry_lock()
        phase["reads"] += 1
        if outcome == "unavailable_then_corrupt" and phase["reads"] == 1:
            raise OSError("private provider response")
        if key not in objects:
            raise OSError("private missing object")
        return objects[key]

    def write_object(key, encoded):
        require_registry_lock()
        if key in objects:
            raise PreconditionFailedError("private conditional conflict")
        objects[key] = encoded

    if backend == "s3":
        config = {**_s3_config(), "INPUT_STORAGE_S3_PREFIX": prefix}

        def get_object(**kwargs):
            assert kwargs["Bucket"] == "request-bucket"
            encoded = read_object(kwargs["Key"])
            return {"ContentLength": len(encoded), "Body": BytesIO(encoded)}

        def put_object(**kwargs):
            assert kwargs["Bucket"] == "request-bucket"
            assert kwargs["IfNoneMatch"] == "*"
            write_object(kwargs["Key"], kwargs["Body"])

        client.get_object.side_effect = get_object
        client.put_object.side_effect = put_object
        storage = S3ResultStorage(bucket="request-bucket", prefix=prefix, client=client)
        constructor = "S3ResultStorage"
    else:
        config = {**_gcs_config(), "INPUT_STORAGE_GCS_PREFIX": prefix}
        blob = client.bucket.return_value.blob.return_value
        selected = {}

        def select_blob(key):
            selected["key"] = key
            return blob

        def reload_blob():
            encoded = read_object(selected["key"])
            blob.size = len(encoded)
            blob.generation = 1

        def download_as_bytes(**kwargs):
            assert kwargs["if_generation_match"] == 1
            return objects[selected["key"]]

        def upload_from_string(encoded, **kwargs):
            assert kwargs["if_generation_match"] == 0
            write_object(selected["key"], encoded)

        client.bucket.return_value.blob.side_effect = select_blob
        blob.reload.side_effect = reload_blob
        blob.download_as_bytes.side_effect = download_as_bytes
        blob.upload_from_string.side_effect = upload_from_string
        storage = GCSResultStorage(bucket="request-bucket", prefix=prefix, client=client)
        constructor = "GCSResultStorage"
    monkeypatch.setattr(request_storage, constructor, Mock(return_value=storage))
    prepared = prepare_ray_job_request(encode_execution_request(_request()), config)
    canonical = prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    assert "/tenant%2525/inputs/" in canonical
    retained = prepared.reference.replace("/tenant%2525/", "/tenant%25/")
    locator = replace(prepared.locator, reference=retained)
    prepared = replace(
        prepared,
        reference=retained,
        locator=locator,
        locator_json=request_storage.serialize_ray_job_request_locator(locator),
        encoded_locator=encode_ray_job_request_locator(locator),
    )
    assert request_storage._validate_prepared_request(prepared) is prepared
    current, handle, payload, registry, executions = _reused_request_rows(
        monkeypatch, prepared, state=state
    )
    previous_use = payload.last_used_at
    key = request_storage._build_object_key(prefix, prepared.digest)
    if outcome == "missing":
        objects.clear()
    elif outcome in {"corrupt", "unavailable_then_corrupt"}:
        objects[key] = b"x" * prepared.size_bytes
    client.reset_mock()
    phase["guard"] = True
    monkeypatch.setattr(storage, "load", Mock(wraps=storage.load))
    monkeypatch.setattr(storage, "store_payload", Mock(wraps=storage.store_payload))
    return SimpleNamespace(
        prepared=prepared,
        current=current,
        handle=handle,
        payload=payload,
        registry=registry,
        executions=executions,
        storage=storage,
        client=client,
        objects=objects,
        key=key,
        canonical=canonical,
        phase=phase,
    )


@pytest.mark.parametrize("backend", ["s3", "gcs"])
@pytest.mark.parametrize("state", [InputPayloadState.ACTIVE, InputPayloadState.PURGED])
def test_reused_request_reads_retained_percent_reference_without_writing(
    monkeypatch, backend, state
):
    retained = _reused_cloud_request_registration(
        monkeypatch, backend=backend, state=state, outcome="present"
    )
    prepared = retained.prepared

    assert (
        register_and_attach_ray_job_request(
            prepared, task_execution=retained.current, submission_handle=retained.handle
        )
        == prepared.reference
    )

    retained.storage.load.assert_called_once_with(
        reference=prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    )
    retained.storage.store_payload.assert_not_called()
    assert (
        retained.payload.reference
        == retained.current.ray_job_request_reference
        == prepared.reference
    )
    assert retained.payload.state == InputPayloadState.ACTIVE
    assert retained.objects[retained.key] == prepared.serialized_request.encode()
    retained.payload.save.assert_called_once()
    retained.current.save.assert_called_once()


@pytest.mark.parametrize("backend", ["s3", "gcs"])
@pytest.mark.parametrize("state", [InputPayloadState.ACTIVE, InputPayloadState.PURGED])
def test_reused_request_restores_retained_percent_reference_from_canonical_writer(
    monkeypatch, backend, state
):
    retained = _reused_cloud_request_registration(
        monkeypatch, backend=backend, state=state, outcome="missing"
    )
    prepared = retained.prepared

    assert (
        register_and_attach_ray_job_request(
            prepared, task_execution=retained.current, submission_handle=retained.handle
        )
        == prepared.reference
    )

    retained.storage.store_payload.assert_called_once_with(
        serialized_payload=prepared.serialized_request
    )
    assert retained.canonical != prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    assert (
        retained.payload.reference
        == retained.current.ray_job_request_reference
        == prepared.reference
    )
    assert retained.payload.state == InputPayloadState.ACTIVE
    assert retained.objects[retained.key] == prepared.serialized_request.encode()
    retained.phase["guard"] = False
    assert load_ray_job_request(prepared.encoded_locator).request == prepared.request


@pytest.mark.parametrize("backend", ["s3", "gcs"])
@pytest.mark.parametrize("changed", ["authority", "prefix", "digest", "size", "backend", "wrapper"])
def test_reused_request_rejects_restored_reference_outside_locator_binding(
    monkeypatch, backend, changed
):
    retained = _reused_cloud_request_registration(
        monkeypatch, backend=backend, state=InputPayloadState.ACTIVE, outcome="missing"
    )
    prepared = retained.prepared
    digest = "b" * 64 if changed == "digest" else prepared.digest
    scheme = "s3" if backend == "s3" else "gs"
    if changed == "backend":
        scheme = "gs" if scheme == "s3" else "s3"
    reference = request_storage._object_reference(
        scheme=scheme,
        bucket="other-bucket" if changed == "authority" else "request-bucket",
        key=request_storage._build_object_key(
            "other/inputs" if changed == "prefix" else "tenant%25/inputs", digest
        ),
        size_bytes=prepared.size_bytes + (1 if changed == "size" else 0),
    )
    if changed == "wrapper":
        reference = RAY_JOB_REQUEST_REFERENCE_PREFIX + reference
    retained.storage.store_payload.side_effect = None
    retained.storage.store_payload.return_value = reference
    before_payload = vars(retained.payload).copy()
    before_execution = vars(retained.current).copy()

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared, task_execution=retained.current, submission_handle=retained.handle
        )

    assert caught.value.classification is RayJobRequestStorageRejection.INTEGRITY_MISMATCH
    assert vars(retained.payload) == before_payload
    assert vars(retained.current) == before_execution
    retained.payload.save.assert_not_called()
    retained.current.save.assert_not_called()
    retained.executions.select_for_update.assert_not_called()


@pytest.mark.parametrize("backend", ["s3", "gcs"])
@pytest.mark.parametrize("state", [InputPayloadState.ACTIVE, InputPayloadState.PURGED])
@pytest.mark.parametrize("outcome", ["corrupt", "unavailable_then_corrupt"])
def test_reused_request_never_overwrites_corrupt_retained_object(
    monkeypatch, backend, state, outcome
):
    retained = _reused_cloud_request_registration(
        monkeypatch, backend=backend, state=state, outcome=outcome
    )
    before_payload = vars(retained.payload).copy()
    before_execution = vars(retained.current).copy()
    before_objects = retained.objects.copy()

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            retained.prepared,
            task_execution=retained.current,
            submission_handle=retained.handle,
        )

    assert caught.value.classification is RayJobRequestStorageRejection.INTEGRITY_MISMATCH
    assert "private" not in str(caught.value)
    assert vars(retained.payload) == before_payload
    assert vars(retained.current) == before_execution
    assert retained.objects == before_objects
    retained.payload.save.assert_not_called()
    retained.current.save.assert_not_called()
    retained.executions.select_for_update.assert_not_called()
    if outcome == "corrupt":
        retained.storage.store_payload.assert_not_called()
    else:
        retained.storage.store_payload.assert_called_once_with(
            serialized_payload=retained.prepared.serialized_request
        )
        assert retained.storage.load.call_count == 2
        if backend == "s3":
            retained.client.put_object.assert_called_once()
            assert retained.client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
        else:
            upload = retained.client.bucket.return_value.blob.return_value.upload_from_string
            upload.assert_called_once()
            assert upload.call_args.kwargs["if_generation_match"] == 0


@pytest.mark.parametrize("state", [InputPayloadState.ACTIVE, InputPayloadState.PURGED])
def test_reused_request_rejects_different_loaded_bytes_without_writing(monkeypatch, state):
    prepared, current, handle, payload, storage, _data, _registry, executions = (
        _reused_request_registration(monkeypatch, state=state)
    )
    storage.load.return_value = "different request bytes"
    before_payload = vars(payload).copy()
    before_execution = vars(current).copy()

    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared, task_execution=current, submission_handle=handle
        )

    assert caught.value.classification is RayJobRequestStorageRejection.INTEGRITY_MISMATCH
    assert vars(payload) == before_payload
    assert vars(current) == before_execution
    storage.store_payload.assert_not_called()
    payload.save.assert_not_called()
    current.save.assert_not_called()
    executions.select_for_update.assert_not_called()
