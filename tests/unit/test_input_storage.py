from __future__ import annotations

import hashlib
import json
import traceback
from typing import Any

import pytest

from django_ray import input_storage
from django_ray.input_storage import (
    EXTERNAL_INPUT_PLACEHOLDER,
    INPUT_ENVELOPE_SCHEMA,
    INPUT_ENVELOPE_VERSION,
    InputPayloadStorageError,
    InputPayloadValidationError,
    PreparedTaskInput,
    delete_input_reference,
    load_task_input,
    prepare_task_input,
    register_task_input,
)
from django_ray.result_storage import ResultStorageError


def _s3_config(**overrides: Any) -> dict[str, Any]:
    config = {
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "s3",
        "INPUT_STORAGE_S3_BUCKET": "task-inputs",
        "INPUT_STORAGE_S3_PREFIX": "django-ray/inputs",
        "INPUT_STORAGE_S3_REGION": None,
        "INPUT_STORAGE_S3_ENDPOINT_URL": None,
    }
    config.update(overrides)
    return config


class FakePayloadStorage:
    def __init__(self) -> None:
        self.payloads: dict[str, str] = {}
        self.deleted: list[str] = []
        self.store_error: Exception | None = None
        self.load_error: Exception | None = None
        self.delete_error: Exception | None = None

    def store_payload(self, *, serialized_payload: str) -> str:
        if self.store_error is not None:
            raise self.store_error
        payload = serialized_payload.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        reference = (
            f"s3://task-inputs/django-ray/inputs/{digest[:2]}/{digest[2:4]}/"
            f"{digest}.json?bytes={len(payload)}"
        )
        self.payloads[reference] = serialized_payload
        return reference

    def load(self, *, reference: str) -> str | None:
        if self.load_error is not None:
            raise self.load_error
        return self.payloads.get(reference)

    def delete(self, *, reference: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.payloads.pop(reference, None)
        self.deleted.append(reference)


def _use_fake_storage(monkeypatch: pytest.MonkeyPatch, storage: FakePayloadStorage) -> None:
    monkeypatch.setattr(input_storage, "_storage_backend", lambda config: ("s3", storage))
    monkeypatch.setattr(
        input_storage,
        "_storage_backend_for_reference",
        lambda metadata, config: storage,
    )


def test_prepare_inline_input_preserves_legacy_fields_when_disabled() -> None:
    prepared = prepare_task_input(
        (1, "two"),
        {"enabled": True},
        {"MAX_INLINE_INPUT_SIZE_BYTES": None},
    )

    assert prepared == PreparedTaskInput(
        args_json='[1, "two"]',
        kwargs_json='{"enabled": true}',
    )
    assert prepared.is_external is False


def test_prepare_input_uses_combined_utf8_size_at_threshold() -> None:
    args = ["é"]
    kwargs = {"label": "值"}
    serialized = input_storage._serialize_envelope(args, kwargs)
    threshold = len(serialized.encode("utf-8"))

    prepared = prepare_task_input(
        args,
        kwargs,
        {"MAX_INLINE_INPUT_SIZE_BYTES": threshold},
    )

    assert prepared.input_reference is None


def test_prepare_external_input_is_deterministic_and_uses_null_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)

    first = prepare_task_input((1,), {"z": 2, "a": 3}, _s3_config())
    second = prepare_task_input([1], {"a": 3, "z": 2}, _s3_config())

    assert first == second
    assert first.args_json == EXTERNAL_INPUT_PLACEHOLDER
    assert first.kwargs_json == EXTERNAL_INPUT_PLACEHOLDER
    assert first.input_reference is not None
    assert first.serialized_payload == (
        '{"args":[1],"kwargs":{"a":3,"z":2},'
        f'"schema":"{INPUT_ENVELOPE_SCHEMA}","version":{INPUT_ENVELOPE_VERSION}}}'
    )
    assert first.digest == hashlib.sha256(first.serialized_payload.encode()).hexdigest()
    assert first.size_bytes == len(first.serialized_payload.encode("utf-8"))


@pytest.mark.parametrize(
    ("args", "kwargs", "message"),
    [
        (object(), {}, "tuple or list"),
        ((), object(), "must be an object"),
        ((object(),), {}, "JSON-serializable"),
    ],
)
def test_prepare_rejects_invalid_inputs(args: Any, kwargs: Any, message: str) -> None:
    with pytest.raises(InputPayloadValidationError, match=message):
        prepare_task_input(args, kwargs, {"MAX_INLINE_INPUT_SIZE_BYTES": None})


def test_combined_envelope_rejects_keys_that_cannot_be_sorted() -> None:
    with pytest.raises(InputPayloadValidationError, match="JSON-serializable"):
        prepare_task_input((), {1: "integer", "1": "string"}, _s3_config())


def test_disabled_spillover_does_not_apply_new_envelope_constraints() -> None:
    prepared = prepare_task_input(
        (),
        {1: "integer", "1": "string"},
        {"MAX_INLINE_INPUT_SIZE_BYTES": None},
    )

    assert json.loads(prepared.kwargs_json) == {"1": "string"}


@pytest.mark.parametrize("threshold", [True, -1, 1.5, "100"])
def test_prepare_rejects_invalid_threshold(threshold: Any) -> None:
    with pytest.raises(InputPayloadValidationError, match="MAX_INLINE_INPUT_SIZE_BYTES"):
        prepare_task_input((), {}, {"MAX_INLINE_INPUT_SIZE_BYTES": threshold})


@pytest.mark.parametrize("backend", [None, "digest", "azure", 1])
def test_prepare_rejects_non_retrievable_storage_backend(backend: Any) -> None:
    config = _s3_config(INPUT_STORAGE_BACKEND=backend)

    with pytest.raises(InputPayloadValidationError, match="INPUT_STORAGE_BACKEND"):
        prepare_task_input((), {}, config)


def test_prepare_wraps_storage_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakePayloadStorage()
    storage.store_error = ResultStorageError("secret storage detail")
    _use_fake_storage(monkeypatch, storage)

    with pytest.raises(InputPayloadStorageError, match="Failed to persist") as caught:
        prepare_task_input((), {}, _s3_config())

    assert "secret storage detail" not in str(caught.value)


def test_load_inline_input_validates_container_types() -> None:
    assert load_task_input(
        args_json="[1]",
        kwargs_json='{"a": 2}',
        input_reference=None,
    ) == ([1], {"a": 2})

    with pytest.raises(InputPayloadValidationError, match="args must decode to a list"):
        load_task_input(args_json="null", kwargs_json="{}", input_reference=None)
    with pytest.raises(InputPayloadValidationError, match="kwargs must decode to an object"):
        load_task_input(args_json="[]", kwargs_json="null", input_reference=None)
    with pytest.raises(InputPayloadValidationError, match="invalid JSON"):
        load_task_input(args_json="[", kwargs_json="{}", input_reference=None)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{", "invalid JSON"),
        ("[]", "must be an object"),
        ('{"args":[],"kwargs":{},"schema":"django-ray.task-input"}', "invalid fields"),
        (
            '{"args":[],"kwargs":{},"schema":"unknown","version":1}',
            "unknown schema",
        ),
        (
            '{"args":[],"kwargs":{},"schema":"django-ray.task-input","version":2}',
            "unsupported",
        ),
        (
            '{"args":{},"kwargs":{},"schema":"django-ray.task-input","version":1}',
            "args must be a list",
        ),
        (
            '{"args":[],"kwargs":[],"schema":"django-ray.task-input","version":1}',
            "kwargs must be an object",
        ),
    ],
)
def test_stored_envelope_rejects_invalid_shape(payload: str, message: str) -> None:
    with pytest.raises(InputPayloadValidationError, match=message):
        input_storage._deserialize_envelope(payload)


def test_external_round_trip_verifies_reference_and_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input(("hello",), {"count": 2}, _s3_config())

    loaded = load_task_input(
        args_json=prepared.args_json,
        kwargs_json=prepared.kwargs_json,
        input_reference=prepared.input_reference,
        config=_s3_config(),
    )

    assert loaded == (["hello"], {"count": 2})


def test_external_load_rejects_non_placeholder_inline_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((), {}, _s3_config())

    with pytest.raises(InputPayloadValidationError, match="safety placeholders"):
        load_task_input(
            args_json="[]",
            kwargs_json=prepared.kwargs_json,
            input_reference=prepared.input_reference,
            config=_s3_config(),
        )


@pytest.mark.parametrize(
    "reference",
    [
        "s3://other/django-ray/inputs/aa/bb/" + "a" * 64 + ".json?bytes=1",
        "s3://task-inputs/other-prefix/aa/bb/" + "a" * 64 + ".json?bytes=1",
        "gs://task-inputs/django-ray/inputs/aa/bb/" + "a" * 64 + ".json?bytes=1",
        "oversize://sha256/" + "a" * 64 + "?bytes=1",
    ],
)
def test_load_rejects_unauthorized_or_digest_only_reference(reference: str) -> None:
    with pytest.raises(InputPayloadValidationError):
        load_task_input(
            args_json="null",
            kwargs_json="null",
            input_reference=reference,
            config=_s3_config(),
        )


def test_external_load_rejects_wrong_byte_count(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((), {}, _s3_config())
    assert prepared.input_reference is not None
    bad_reference = prepared.input_reference.rsplit("=", 1)[0] + "=1"
    storage.payloads[bad_reference] = prepared.serialized_payload or ""

    with pytest.raises(InputPayloadValidationError, match="byte count does not match"):
        load_task_input(
            args_json="null",
            kwargs_json="null",
            input_reference=bad_reference,
            config=_s3_config(),
        )


def test_external_load_rejects_digest_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((1,), {}, _s3_config())
    assert prepared.input_reference is not None
    corrupt_payload = (prepared.serialized_payload or "").replace("[1]", "[2]")
    storage.payloads[prepared.input_reference] = corrupt_payload

    with pytest.raises(InputPayloadValidationError, match="digest does not match"):
        load_task_input(
            args_json="null",
            kwargs_json="null",
            input_reference=prepared.input_reference,
            config=_s3_config(),
        )


def test_external_load_rejects_noncanonical_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    envelope = {
        "schema": INPUT_ENVELOPE_SCHEMA,
        "version": INPUT_ENVELOPE_VERSION,
        "args": [],
        "kwargs": {},
    }
    noncanonical = json.dumps(envelope, indent=2)
    digest = hashlib.sha256(noncanonical.encode()).hexdigest()
    reference = (
        f"s3://task-inputs/django-ray/inputs/{digest[:2]}/{digest[2:4]}/"
        f"{digest}.json?bytes={len(noncanonical.encode())}"
    )
    storage.payloads[reference] = noncanonical

    with pytest.raises(InputPayloadValidationError, match="not canonical"):
        load_task_input(
            args_json="null",
            kwargs_json="null",
            input_reference=reference,
            config=_s3_config(),
        )


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "x" * 501,
        "http://[::1",
        "s3://task-inputs/key?bytes=1#fragment",
        "s3://task-inputs/key",
        "s3://task-inputs/key?bytes=",
        "s3://task-inputs/key?bytes=one",
        "s3://task-inputs/key?bytes=-1",
        "s3://task-inputs/key?bytes=01",
    ],
)
def test_reference_parser_rejects_malformed_metadata(reference: str) -> None:
    with pytest.raises(InputPayloadValidationError) as caught:
        input_storage._validated_reference(reference, _s3_config())
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_noncanonical_object_reference_is_rejected_before_sdk_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "a" * 64
    base = f"s3://task-inputs/django-ray/inputs/aa/aa/{digest}.json"
    references = (f"{base}?bytes=%31", f"{base}?%62ytes=1")
    constructor_calls = 0

    class UnexpectedS3:
        def __init__(self, **kwargs: Any) -> None:
            nonlocal constructor_calls
            constructor_calls += 1

    monkeypatch.setattr(input_storage, "S3ResultStorage", UnexpectedS3)

    for reference in references:
        with pytest.raises(InputPayloadValidationError, match="reference is invalid"):
            load_task_input(
                args_json="null",
                kwargs_json="null",
                input_reference=reference,
                config=_s3_config(),
            )
        with pytest.raises(InputPayloadValidationError, match="reference is invalid"):
            delete_input_reference(reference, _s3_config())

    assert constructor_calls == 0


def test_malformed_reference_traceback_does_not_retain_query_tokens() -> None:
    sensitive = "VERY_PRIVATE_STORAGE_TOKEN"
    digest = "a" * 64
    reference = f"s3://task-inputs/django-ray/inputs/aa/aa/{digest}.json?bytes=1&{sensitive}"

    with pytest.raises(InputPayloadValidationError, match="reference is invalid") as caught:
        input_storage._validated_reference(reference, _s3_config())

    formatted = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert sensitive not in formatted
    assert reference not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_filesystem_reference_authorization_and_storage_resolution(tmp_path) -> None:
    config = {
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    payload = input_storage._serialize_envelope([], {})
    digest = hashlib.sha256(payload.encode()).hexdigest()
    reference = (
        f"resultfs://sha256/{digest}?rel={digest[:2]}/{digest[2:4]}/{digest}.json"
        f"&bytes={len(payload.encode())}"
    )

    metadata = input_storage._validated_reference(reference, config)
    backend_name, backend = input_storage._storage_backend(config)

    assert metadata == input_storage.InputReferenceMetadata(
        backend="filesystem",
        digest=digest,
        size_bytes=len(payload.encode()),
    )
    assert backend_name == "filesystem"
    assert backend.root_path == tmp_path


def test_noncanonical_filesystem_query_is_rejected_before_storage_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    digest = "a" * 64
    reference = f"resultfs://sha256/{digest}?bytes=1&rel={digest[:2]}/{digest[2:4]}/{digest}.json"
    constructor_calls = 0

    class UnexpectedFilesystem:
        def __init__(self, root_path: str) -> None:
            nonlocal constructor_calls
            constructor_calls += 1

    monkeypatch.setattr(input_storage, "FilesystemResultStorage", UnexpectedFilesystem)

    with pytest.raises(InputPayloadValidationError, match="reference is invalid"):
        load_task_input(
            args_json="null",
            kwargs_json="null",
            input_reference=reference,
            config={
                "INPUT_STORAGE_BACKEND": "filesystem",
                "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
            },
        )

    assert constructor_calls == 0


@pytest.mark.parametrize(
    "reference",
    [
        "resultfs://sha256/" + "a" * 64 + "?bytes=1&rel=a.json&extra=1",
        "s3://sha256/" + "a" * 64 + "?bytes=1&rel=aa/aa/" + "a" * 64 + ".json",
        "resultfs://sha256/not-a-digest?bytes=1&rel=a.json",
        "resultfs://sha256/" + "a" * 64 + "?bytes=1&rel=../" + "a" * 64 + ".json",
        "resultfs://sha256/" + "a" * 64 + "?bytes=1&rel=wrong.json",
    ],
)
def test_filesystem_reference_rejects_invalid_shape(reference: str, tmp_path) -> None:
    config = {
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    with pytest.raises(InputPayloadValidationError):
        input_storage._validated_reference(reference, config)


def test_filesystem_reference_requires_configured_root() -> None:
    digest = "a" * 64
    reference = f"resultfs://sha256/{digest}?rel={digest[:2]}/{digest[2:4]}/{digest}.json&bytes=1"
    with pytest.raises(InputPayloadValidationError, match="FILESYSTEM_PATH"):
        input_storage._validated_reference(
            reference,
            {"INPUT_STORAGE_BACKEND": "filesystem"},
        )


def test_object_reference_rejects_extra_query_and_invalid_digest() -> None:
    with pytest.raises(InputPayloadValidationError, match="reference is invalid"):
        input_storage._validated_reference(
            "s3://task-inputs/key?bytes=1&extra=1",
            _s3_config(),
        )
    with pytest.raises(InputPayloadValidationError, match="reference is invalid"):
        input_storage._validated_reference(
            "s3://task-inputs/django-ray/inputs/aa/bb/not-a-digest.json?bytes=1",
            _s3_config(),
        )


def test_object_reference_accepts_only_canonical_encoded_configured_prefix() -> None:
    digest = "a" * 64
    config = _s3_config(INPUT_STORAGE_S3_PREFIX="tenant alpha/résults")
    reference = f"s3://task-inputs/tenant%20alpha/r%C3%A9sults/aa/aa/{digest}.json?bytes=1"

    metadata = input_storage._validated_reference(reference, config)

    assert metadata.digest == digest
    with pytest.raises(InputPayloadValidationError, match="configured input storage"):
        input_storage._validated_reference(
            reference.replace("tenant%20alpha", "%74enant%20alpha"),
            config,
        )


def test_object_reference_accepts_encoding_only_legacy_prefix() -> None:
    digest = "a" * 64
    prefix = "tenant alpha/résults+100%"
    config = _s3_config(INPUT_STORAGE_S3_PREFIX=prefix)
    reference = f"s3://task-inputs/{prefix}/aa/aa/{digest}.json?bytes=1"

    metadata = input_storage._validated_reference(reference, config)

    assert metadata.digest == digest
    with pytest.raises(InputPayloadValidationError, match="configured input storage"):
        input_storage._validated_reference(
            reference,
            _s3_config(INPUT_STORAGE_S3_PREFIX="another-prefix"),
        )


def test_gcs_reference_authorization_and_storage_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeGCS:
        def __init__(self, *, bucket: str, prefix: str) -> None:
            captured.update(bucket=bucket, prefix=prefix)

    monkeypatch.setattr(input_storage, "GCSResultStorage", FakeGCS)
    config = {
        "INPUT_STORAGE_BACKEND": "gcs",
        "INPUT_STORAGE_GCS_BUCKET": "gcs-inputs",
        "INPUT_STORAGE_GCS_PREFIX": "custom/inputs",
    }
    digest = "a" * 64
    reference = f"gs://gcs-inputs/custom/inputs/aa/aa/{digest}.json?bytes=1"

    metadata = input_storage._validated_reference(reference, config)
    backend_name, backend = input_storage._storage_backend(config)

    assert metadata.backend == "gcs"
    assert backend_name == "gcs"
    assert isinstance(backend, FakeGCS)
    assert captured == {"bucket": "gcs-inputs", "prefix": "custom/inputs"}


def test_s3_storage_resolution_passes_connection_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeS3:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(input_storage, "S3ResultStorage", FakeS3)
    config = _s3_config(
        INPUT_STORAGE_S3_REGION="us-test-1",
        INPUT_STORAGE_S3_ENDPOINT_URL="https://storage.example",
    )

    backend_name, backend = input_storage._storage_backend(config)

    assert backend_name == "s3"
    assert isinstance(backend, FakeS3)
    assert captured == {
        "bucket": "task-inputs",
        "prefix": "django-ray/inputs",
        "endpoint_url": "https://storage.example",
        "region_name": "us-test-1",
    }


def test_s3_storage_resolution_preserves_an_explicit_empty_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeS3:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(input_storage, "S3ResultStorage", FakeS3)
    config = _s3_config(INPUT_STORAGE_S3_PREFIX="")

    input_storage._storage_backend(config)

    assert captured["prefix"] == ""


def test_s3_storage_resolution_rejects_a_non_string_prefix_before_client_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized = False

    class FakeS3:
        def __init__(self, **_kwargs: Any) -> None:
            nonlocal initialized
            initialized = True

    monkeypatch.setattr(input_storage, "S3ResultStorage", FakeS3)

    with pytest.raises(InputPayloadValidationError, match="INPUT_STORAGE_S3_PREFIX"):
        input_storage._storage_backend(_s3_config(INPUT_STORAGE_S3_PREFIX=123))

    assert initialized is False


def test_historical_input_read_and_delete_dispatch_to_retained_s3_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = input_storage._serialize_envelope(["historical"], {"version": 1})
    payload_bytes = payload.encode("utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    reference = (
        f"s3://old-inputs/retained/inputs/{digest[:2]}/{digest[2:4]}/"
        f"{digest}.json?bytes={len(payload_bytes)}"
    )
    load_calls: list[str] = []
    delete_calls: list[str] = []
    constructor_kwargs: list[dict[str, Any]] = []

    class RetainedS3:
        def __init__(self, **kwargs: Any) -> None:
            constructor_kwargs.append(kwargs)

        def load(self, *, reference: str) -> str:
            load_calls.append(reference)
            return payload

        def delete(self, *, reference: str) -> None:
            delete_calls.append(reference)

    class UnexpectedActiveGCS:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("active writer must not handle a retained S3 reference")

    monkeypatch.setattr(input_storage, "S3ResultStorage", RetainedS3)
    monkeypatch.setattr(input_storage, "GCSResultStorage", UnexpectedActiveGCS)
    config = {
        "INPUT_STORAGE_BACKEND": "gcs",
        "INPUT_STORAGE_GCS_BUCKET": "new-inputs",
        "INPUT_STORAGE_GCS_PREFIX": "current/inputs",
        "INPUT_STORAGE_S3_BUCKET": "old-inputs",
        "INPUT_STORAGE_S3_PREFIX": "retained/inputs",
        "INPUT_STORAGE_S3_ENDPOINT_URL": "https://retained.example",
        "INPUT_STORAGE_S3_REGION": "us-test-1",
    }

    assert load_task_input(
        args_json="null",
        kwargs_json="null",
        input_reference=reference,
        config=config,
    ) == (["historical"], {"version": 1})
    delete_input_reference(reference, config)

    assert load_calls == [reference]
    assert delete_calls == [reference]
    assert constructor_kwargs == [
        {
            "bucket": "old-inputs",
            "prefix": "retained/inputs",
            "endpoint_url": "https://retained.example",
            "region_name": "us-test-1",
        },
        {
            "bucket": "old-inputs",
            "prefix": "retained/inputs",
            "endpoint_url": "https://retained.example",
            "region_name": "us-test-1",
        },
    ]


def test_storage_resolution_requires_backend_specific_configuration() -> None:
    with pytest.raises(InputPayloadValidationError, match="S3_BUCKET"):
        input_storage._storage_backend(
            {"INPUT_STORAGE_BACKEND": "s3", "INPUT_STORAGE_S3_BUCKET": ""}
        )


def test_storage_resolution_wraps_backend_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingS3:
        def __init__(self, **kwargs: Any) -> None:
            raise ResultStorageError("private initialization error")

    monkeypatch.setattr(input_storage, "S3ResultStorage", FailingS3)
    with pytest.raises(InputPayloadStorageError, match="initialize") as caught:
        input_storage._storage_backend(_s3_config())
    assert "private initialization error" not in str(caught.value)


def test_external_load_wraps_missing_and_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((), {}, _s3_config())
    assert prepared.input_reference is not None
    storage.payloads.clear()

    with pytest.raises(InputPayloadStorageError, match="unavailable"):
        load_task_input(
            args_json="null",
            kwargs_json="null",
            input_reference=prepared.input_reference,
            config=_s3_config(),
        )

    storage.load_error = ResultStorageError("private backend error")
    with pytest.raises(InputPayloadStorageError, match="Failed to load") as caught:
        load_task_input(
            args_json="null",
            kwargs_json="null",
            input_reference=prepared.input_reference,
            config=_s3_config(),
        )
    assert "private backend error" not in str(caught.value)


def test_prepare_rejects_inconsistent_backend_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    original_store = storage.store_payload

    def wrong_size(*, serialized_payload: str) -> str:
        return original_store(serialized_payload=serialized_payload).rsplit("=", 1)[0] + "=1"

    monkeypatch.setattr(storage, "store_payload", wrong_size)
    with pytest.raises(InputPayloadValidationError, match="inconsistent"):
        prepare_task_input((), {}, _s3_config())


def test_delete_validates_reference_and_wraps_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((), {}, _s3_config())
    assert prepared.input_reference is not None

    delete_input_reference(prepared.input_reference, _s3_config())
    assert storage.deleted == [prepared.input_reference]

    storage.delete_error = ResultStorageError("private delete error")
    with pytest.raises(InputPayloadStorageError, match="Failed to delete") as caught:
        delete_input_reference(prepared.input_reference, _s3_config())
    assert "private delete error" not in str(caught.value)


@pytest.mark.django_db
def test_register_task_input_creates_and_reuses_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.models import InputPayloadKind, InputPayloadState, TaskInputPayload

    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((1,), {"a": 2}, _s3_config())

    first = register_task_input(prepared, _s3_config())
    assert first is not None
    assert first.reference == prepared.input_reference
    assert first.payload_kind == InputPayloadKind.TASK_INPUT
    assert first.backend == "s3"
    assert first.digest == prepared.digest
    assert first.size_bytes == prepared.size_bytes
    assert first.envelope_version == INPUT_ENVELOPE_VERSION
    assert first.state == InputPayloadState.ACTIVE

    initial_used_at = first.last_used_at
    second = register_task_input(prepared, _s3_config())
    assert second is not None
    assert second.pk == first.pk
    assert second.last_used_at >= initial_used_at
    assert TaskInputPayload.objects.count() == 1


@pytest.mark.django_db
def test_register_task_input_reactivates_purged_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.models import InputPayloadState

    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((1,), {}, _s3_config())
    payload = register_task_input(prepared, _s3_config())
    assert payload is not None
    payload.state = InputPayloadState.PURGED
    payload.purged_at = input_storage.timezone.now()
    payload.cleanup_error = "old failure"
    payload.save(update_fields=["state", "purged_at", "cleanup_error"])
    storage.payloads.clear()

    reactivated = register_task_input(prepared, _s3_config())

    assert reactivated is not None
    assert reactivated.state == InputPayloadState.ACTIVE
    assert reactivated.purged_at is None
    assert reactivated.cleanup_error == ""
    assert prepared.input_reference in storage.payloads


@pytest.mark.django_db
def test_register_reactivation_wraps_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.models import InputPayloadState

    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((), {}, _s3_config())
    payload = register_task_input(prepared, _s3_config())
    assert payload is not None
    payload.state = InputPayloadState.PURGED
    payload.save(update_fields=["state"])
    storage.store_error = ResultStorageError("private")

    with pytest.raises(InputPayloadStorageError, match="reactivate"):
        register_task_input(prepared, _s3_config())


@pytest.mark.django_db
def test_register_reactivation_rejects_changed_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.models import InputPayloadState

    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((), {}, _s3_config())
    payload = register_task_input(prepared, _s3_config())
    assert payload is not None
    payload.state = InputPayloadState.PURGED
    payload.save(update_fields=["state"])
    original_store = storage.store_payload

    def changed_reference(*, serialized_payload: str) -> str:
        return original_store(serialized_payload=serialized_payload).replace(
            "s3://task-inputs/", "s3://other/"
        )

    monkeypatch.setattr(storage, "store_payload", changed_reference)
    with pytest.raises(InputPayloadValidationError, match="changed an immutable"):
        register_task_input(prepared, _s3_config())


@pytest.mark.django_db
def test_register_task_input_rejects_registry_metadata_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((1,), {}, _s3_config())
    payload = register_task_input(prepared, _s3_config())
    assert payload is not None
    payload.digest = "0" * 64
    payload.save(update_fields=["digest"])

    with pytest.raises(InputPayloadValidationError, match="registry metadata"):
        register_task_input(prepared, _s3_config())


@pytest.mark.django_db
def test_register_task_input_rejects_request_kind_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.models import InputPayloadKind, InputPayloadState

    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    prepared = prepare_task_input((1,), {}, _s3_config())
    payload = register_task_input(prepared, _s3_config())
    assert payload is not None
    payload.payload_kind = InputPayloadKind.RAY_JOB_REQUEST
    payload.state = InputPayloadState.PURGED
    payload.purged_at = input_storage.timezone.now()
    payload.save(update_fields=["payload_kind", "state", "purged_at"])
    original_last_used_at = payload.last_used_at
    storage.payloads.clear()

    with pytest.raises(InputPayloadValidationError, match="registry metadata"):
        register_task_input(prepared, _s3_config())

    payload.refresh_from_db()
    assert payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST
    assert payload.state == InputPayloadState.PURGED
    assert payload.last_used_at == original_last_used_at
    assert prepared.input_reference not in storage.payloads


@pytest.mark.django_db
def test_register_rejects_incomplete_or_inconsistent_prepared_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakePayloadStorage()
    _use_fake_storage(monkeypatch, storage)
    incomplete = PreparedTaskInput(
        args_json="null",
        kwargs_json="null",
        input_reference="s3://task-inputs/reference",
    )
    with pytest.raises(InputPayloadValidationError, match="incomplete"):
        register_task_input(incomplete, _s3_config())

    prepared = prepare_task_input((), {}, _s3_config())
    inconsistent = PreparedTaskInput(
        **{
            **prepared.__dict__,
            "digest": "0" * 64,
        }
    )
    with pytest.raises(InputPayloadValidationError, match="metadata is inconsistent"):
        register_task_input(inconsistent, _s3_config())


def test_register_inline_input_is_noop() -> None:
    prepared = prepare_task_input((), {}, {"MAX_INLINE_INPUT_SIZE_BYTES": None})

    assert register_task_input(prepared) is None
