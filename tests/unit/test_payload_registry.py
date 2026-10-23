"""Resource-free physical payload identity and registry refusal checks."""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from django.db import transaction
from django.utils import timezone

from django_ray import input_storage, payload_registry, ray_job_request_storage
from django_ray.execution_codec import ExecutionIdentity, ExecutionRequest, encode_execution_request
from django_ray.input_storage import InputPayloadValidationError, PreparedTaskInput
from django_ray.management.commands.django_ray_purge_inputs import Command
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
)
from django_ray.payload_registry import (
    _has_other_payload_registry_reference,
    _payload_registry_references,
)
from django_ray.ray_job_request_storage import (
    RAY_JOB_REQUEST_REFERENCE_PREFIX,
    RayJobRequestStorageError,
    RayJobRequestStorageRejection,
)
from django_ray.result_storage import ResultStorageError


def _references(scheme="s3", prefix="tenant alpha/inputs", *, size=128, digest="a" * 64):
    key = f"{prefix}/{digest[:2]}/{digest[2:4]}/{digest}.json"
    raw = f"{scheme}://bucket/{key}?bytes={size}"
    canonical = f"{scheme}://bucket/{quote(key, safe='/-._~')}?bytes={size}"
    backend = "S3" if scheme == "s3" else "GCS"
    config = {
        f"INPUT_STORAGE_{backend}_BUCKET": "bucket",
        f"INPUT_STORAGE_{backend}_PREFIX": prefix,
    }
    return raw, canonical, config


@pytest.mark.parametrize("scheme", ["s3", "gs"])
@pytest.mark.parametrize(
    ("prefix", "count"),
    [
        ("inputs", 2),
        ("tenant alpha/inputs", 3),
        ("tenant-é/inputs", 3),
        ("tenant%25/inputs", 4),
        ("tenant%2F/inputs", 3),
        ("tenant+100%/inputs", 3),
    ],
)
def test_physical_aliases_are_finite_and_configuration_bound(scheme, prefix, count):
    raw, canonical, config = _references(scheme, prefix)
    from_raw = _payload_registry_references(raw, config)
    from_canonical = _payload_registry_references(canonical, config)
    wrapped = _payload_registry_references(RAY_JOB_REQUEST_REFERENCE_PREFIX + canonical, config)

    assert from_raw == from_canonical
    assert from_raw.canonical == canonical
    assert wrapped.canonical == RAY_JOB_REQUEST_REFERENCE_PREFIX + canonical
    assert wrapped.aliases == from_raw.aliases
    assert len(from_raw.aliases) == count
    assert raw in from_raw.aliases and canonical in from_raw.aliases
    assert from_raw.aliases == tuple(sorted(set(from_raw.aliases)))


@pytest.mark.parametrize("prefix", ["inputs?opaque", "inputs#opaque"])
def test_unparsable_raw_key_is_not_an_alias(prefix):
    _raw, canonical, config = _references(prefix=prefix)
    references = _payload_registry_references(canonical, config)
    assert references.aliases == tuple(
        sorted((canonical, RAY_JOB_REQUEST_REFERENCE_PREFIX + canonical))
    )


def test_oversized_canonical_spelling_does_not_block_existing_legacy_identity():
    raw, canonical, config = _references(prefix="é" * 150)
    assert len(raw) <= 500 < len(canonical)
    references = _payload_registry_references(raw, config)
    assert references.canonical == canonical
    assert references.aliases == (raw,)


@pytest.mark.parametrize("changed", ["bucket", "prefix", "encoding", "query", "wrapper"])
def test_alias_enumeration_rejects_unauthorized_or_noncanonical_input(changed):
    _raw, canonical, config = _references()
    if changed == "bucket":
        canonical = canonical.replace("bucket/", "other/")
    elif changed == "prefix":
        config["INPUT_STORAGE_S3_PREFIX"] = "other"
    elif changed == "encoding":
        canonical = canonical.replace("%20", "%2520")
    elif changed == "query":
        canonical = canonical.replace("bytes=128", "bytes=0128")
    else:
        canonical = RAY_JOB_REQUEST_REFERENCE_PREFIX + canonical.replace("%20", " ")
    with pytest.raises(ResultStorageError):
        _payload_registry_references(canonical, config)


def test_filesystem_aliases_do_not_initialize_storage_or_require_a_root(monkeypatch):
    reference = "resultfs://sha256/" + "a" * 64 + "?rel=aa/aa/" + "a" * 64 + ".json&bytes=128"
    monkeypatch.setattr(payload_registry, "get_settings", lambda: pytest.fail("settings lookup"))
    references = _payload_registry_references(reference)
    assert references.canonical == reference
    assert references.aliases == tuple(
        sorted((reference, RAY_JOB_REQUEST_REFERENCE_PREFIX + reference))
    )


def _registry(monkeypatch):
    manager = Mock()
    for method in ("using", "filter", "select_for_update", "order_by", "values_list", "only"):
        getattr(manager, method).return_value = manager
    manager.first.return_value = None
    manager.exists.return_value = False
    monkeypatch.setattr(TaskInputPayload, "objects", manager)
    return manager


@pytest.mark.parametrize("lock", [False, True])
def test_alias_query_is_bounded_and_locks_only_one_row_before_refusal(monkeypatch, lock):
    raw, canonical, config = _references(prefix="tenant%25/inputs")
    references = _payload_registry_references(canonical, config)
    manager = _registry(monkeypatch)
    manager.first.return_value = raw
    manager.exists.return_value = True

    assert _has_other_payload_registry_reference(canonical, references, using="selected", lock=lock)
    manager.using.assert_called_once_with("selected")
    manager.filter.assert_called_once_with(
        reference__in=tuple(alias for alias in references.aliases if alias != canonical)
    )
    if lock:
        manager.select_for_update.assert_called_once_with()
        manager.order_by.assert_called_once_with("reference")
        manager.values_list.assert_called_once_with("reference", flat=True)
        manager.first.assert_called_once_with()
        manager.exists.assert_not_called()
    else:
        manager.select_for_update.assert_not_called()
        manager.exists.assert_called_once_with()


def _prepared_input(*, legacy=False, value="same payload"):
    serialized = input_storage._serialize_envelope([value], {})
    encoded = serialized.encode()
    digest = hashlib.sha256(encoded).hexdigest()
    raw, canonical, config = _references(size=len(encoded), digest=digest)
    prepared = PreparedTaskInput(
        args_json="null",
        kwargs_json="null",
        input_reference=raw if legacy else canonical,
        serialized_payload=serialized,
        digest=digest,
        size_bytes=len(encoded),
        backend="s3",
        envelope_version=1,
    )
    return prepared, raw, canonical, config


@pytest.mark.parametrize("legacy", [False, True])
def test_input_alias_refusal_precedes_own_registry_write_and_storage(monkeypatch, legacy):
    prepared, raw, canonical, config = _prepared_input(legacy=legacy)
    manager = _registry(monkeypatch)
    manager.first.return_value = canonical if legacy else raw
    monkeypatch.setattr(
        input_storage, "S3ResultStorage", lambda **_: pytest.fail("storage reached")
    )

    with pytest.raises(InputPayloadValidationError, match="another physical registry"):
        input_storage.register_task_input(prepared, config, using="selected")
    manager.get_or_create.assert_not_called()
    manager.select_for_update.assert_called_once_with()
    manager.using.assert_called_once_with("selected")


def test_new_legacy_input_identity_is_refused_without_insertion(monkeypatch):
    prepared, _raw, _canonical, config = _prepared_input(legacy=True)
    manager = _registry(monkeypatch)
    with pytest.raises(InputPayloadValidationError, match="require a canonical registry"):
        input_storage.register_task_input(prepared, config)
    manager.get_or_create.assert_not_called()
    manager.exists.assert_called_once_with()


@pytest.mark.parametrize("value", ["same payload", "é"])
def test_existing_legacy_input_identity_is_reused_without_rewriting(monkeypatch, value):
    prepared, raw, canonical, config = _prepared_input(legacy=True, value=value)
    manager = _registry(monkeypatch)
    manager.exists.return_value = True
    payload = SimpleNamespace(
        reference=raw,
        payload_kind=InputPayloadKind.TASK_INPUT,
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
        state=InputPayloadState.ACTIVE,
        last_used_at=timezone.now(),
        save=Mock(),
    )
    manager.get_or_create.return_value = payload, False
    storage = Mock()
    storage.load.return_value = prepared.serialized_payload
    storage.store_payload.return_value = canonical
    monkeypatch.setattr(input_storage, "_storage_backend_for_reference", lambda *_: storage)

    assert input_storage.register_task_input(prepared, config) is payload
    assert payload.reference == raw
    assert prepared.input_reference == raw
    payload.save.assert_called_once()


@pytest.mark.parametrize("direction", ["raw", "canonical", "wrapped"])
@pytest.mark.parametrize("delete", [False, True])
def test_purge_alias_refusal_precedes_execution_locks_and_delete(monkeypatch, direction, delete):
    raw, canonical, config = _references()
    reference = {
        "raw": raw,
        "canonical": canonical,
        "wrapped": RAY_JOB_REQUEST_REFERENCE_PREFIX + canonical,
    }[direction]
    payload = SimpleNamespace(
        reference=reference,
        state=InputPayloadState.ACTIVE,
        last_used_at=timezone.now() - timedelta(days=60),
        payload_kind=InputPayloadKind.RAY_JOB_REQUEST
        if direction == "wrapped"
        else InputPayloadKind.TASK_INPUT,
        save=Mock(),
    )
    manager = _registry(monkeypatch)
    manager.first.return_value = payload
    manager.exists.return_value = True
    executions = Mock()
    monkeypatch.setattr(RayTaskExecution, "objects", executions)
    monkeypatch.setattr(payload_registry, "get_settings", lambda: config)
    monkeypatch.setattr(transaction, "atomic", lambda: nullcontext())
    deleter = Mock()
    monkeypatch.setattr(input_storage, "delete_input_reference", deleter)

    assert (
        Command()._process_reference(
            reference, cutoff=timezone.now() - timedelta(days=30), delete=delete
        )
        == "incomplete"
    )
    executions.select_for_update.assert_not_called()
    deleter.assert_not_called()
    payload.save.assert_not_called()
    manager.select_for_update.assert_called_once_with()


@pytest.mark.parametrize("value", ["same payload", "é"])
def test_unaliased_new_input_registration_keeps_canonical_identity(monkeypatch, value):
    prepared, _raw, canonical, config = _prepared_input(value=value)
    manager = _registry(monkeypatch)
    payload = SimpleNamespace(reference=canonical)
    manager.get_or_create.return_value = payload, True
    assert input_storage.register_task_input(replace(prepared), config) is payload
    assert manager.get_or_create.call_args.kwargs["reference"] == canonical
    manager.exists.assert_not_called()


def _prepared_request(monkeypatch):
    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
        coordination_sha256,
    )

    request = ExecutionRequest(
        identity=ExecutionIdentity(1, "physical-alias-request", 1, 2),
        execution_protocol_version=1,
        callable_path="testproject.tasks.add_numbers",
        transport_version=1,
        serialized_args="[1]",
        serialized_kwargs="{}",
        input_reference=None,
        runtime_env_profile=None,
        runtime_env_hash="a" * 64,
        runtime_env_plan_identity={},
        compiled_graph_submission_transport="ray-job",
    )
    serialized = encode_execution_request(request)
    encoded = serialized.encode()
    raw, canonical, config = _references(
        prefix="tenant%25/inputs", size=len(encoded), digest=hashlib.sha256(encoded).hexdigest()
    )
    storage = Mock()
    storage.store_payload.return_value = canonical
    monkeypatch.setattr(ray_job_request_storage, "S3ResultStorage", Mock(return_value=storage))
    prepared = ray_job_request_storage.prepare_ray_job_request(
        serialized, {**config, "INPUT_STORAGE_BACKEND": "s3"}
    )
    job_id = STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX + coordination_sha256(
        request.identity
    )
    execution = RayTaskExecution(
        pk=1,
        task_id=request.identity.task_id,
        attempt_number=1,
        execution_generation=2,
        execution_protocol_version=1,
        claimed_by_worker="alias-worker",
    )
    execution._state.db = "default"
    handle = SimpleNamespace(ray_job_id=job_id, ray_address="http://unused-ray:8265")
    monkeypatch.setattr(transaction, "atomic", lambda **_: nullcontext())
    storage.store_payload.reset_mock()
    return prepared, raw, execution, handle, storage


def test_request_alias_refusal_precedes_registry_creation_and_execution_lock(monkeypatch):
    prepared, raw, execution, handle, storage = _prepared_request(monkeypatch)
    manager = _registry(monkeypatch)
    manager.first.return_value = raw
    executions = Mock()
    monkeypatch.setattr(RayTaskExecution, "objects", executions)

    with pytest.raises(RayJobRequestStorageError) as caught:
        ray_job_request_storage.register_and_attach_ray_job_request(
            prepared, task_execution=execution, submission_handle=handle
        )
    assert caught.value.classification is RayJobRequestStorageRejection.REGISTRY_MISMATCH
    manager.get_or_create.assert_not_called()
    executions.select_for_update.assert_not_called()
    storage.store_payload.assert_not_called()
    assert execution.ray_job_request_reference is None
    manager.using.assert_called_once_with("default")


def test_new_noncanonical_wrapped_request_identity_is_refused(monkeypatch):
    prepared, raw, execution, handle, storage = _prepared_request(monkeypatch)
    legacy = RAY_JOB_REQUEST_REFERENCE_PREFIX + raw
    locator = replace(prepared.locator, reference=legacy)
    prepared = replace(
        prepared,
        reference=legacy,
        locator=locator,
        locator_json=ray_job_request_storage.serialize_ray_job_request_locator(locator),
        encoded_locator=ray_job_request_storage.encode_ray_job_request_locator(locator),
    )
    manager = _registry(monkeypatch)

    with pytest.raises(RayJobRequestStorageError) as caught:
        ray_job_request_storage.register_and_attach_ray_job_request(
            prepared, task_execution=execution, submission_handle=handle
        )
    assert caught.value.classification is RayJobRequestStorageRejection.INVALID_REQUEST
    manager.get_or_create.assert_not_called()
    manager.exists.assert_called_once_with()
    storage.store_payload.assert_not_called()


def test_input_registration_rejects_request_bytes_before_empty_registry_query(monkeypatch):
    prepared, _raw, _execution, _handle, storage = _prepared_request(monkeypatch)
    disguised = PreparedTaskInput(
        args_json="null",
        kwargs_json="null",
        input_reference=prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX),
        serialized_payload=prepared.serialized_request,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        backend=prepared.backend,
        envelope_version=1,
    )
    config = {
        "INPUT_STORAGE_S3_BUCKET": prepared.locator.s3_bucket,
        "INPUT_STORAGE_S3_PREFIX": prepared.locator.s3_prefix,
    }
    manager = _registry(monkeypatch)
    manager.get_or_create.return_value = SimpleNamespace(), True

    with pytest.raises(InputPayloadValidationError, match="envelope"):
        input_storage.register_task_input(disguised, config)
    manager.using.assert_not_called()
    storage.store_payload.assert_not_called()


@pytest.mark.parametrize("changed", ["bytes", "digest", "size", "version", "canonical"])
def test_input_registration_validates_prepared_bytes_before_registry_query(monkeypatch, changed):
    prepared, _raw, _canonical, config = _prepared_input()
    assert prepared.input_reference is not None
    assert prepared.serialized_payload is not None
    if changed == "bytes":
        prepared = replace(
            prepared, serialized_payload=input_storage._serialize_envelope(["other"], {})
        )
    elif changed == "digest":
        prepared = replace(
            prepared,
            digest="b" * 64,
            input_reference=_references(digest="b" * 64, size=prepared.size_bytes)[1],
        )
    elif changed == "size":
        prepared = replace(
            prepared,
            size_bytes=1,
            input_reference=prepared.input_reference.rsplit("=", 1)[0] + "=1",
        )
    elif changed == "version":
        prepared = replace(prepared, envelope_version=2)
    else:
        prepared = replace(prepared, serialized_payload=" " + prepared.serialized_payload)
    manager = _registry(monkeypatch)
    manager.get_or_create.return_value = SimpleNamespace(), True

    with pytest.raises(InputPayloadValidationError):
        input_storage.register_task_input(prepared, config)
    manager.using.assert_not_called()


def test_no_alias_query_is_issued_for_an_unrepresentable_sibling(monkeypatch):
    raw, _canonical, config = _references(prefix="é" * 150)
    manager = _registry(monkeypatch)
    assert not _has_other_payload_registry_reference(raw, _payload_registry_references(raw, config))
    manager.using.assert_not_called()


@pytest.mark.parametrize(
    "reference",
    [
        "oversize://sha256/" + "a" * 64 + "?bytes=1",
        "s3://bucket/inputs/aa/aa/" + "a" * 64 + ".json?bytes=1",
    ],
)
def test_unretrievable_or_unconfigured_registry_namespace_is_refused(reference):
    with pytest.raises(ResultStorageError):
        _payload_registry_references(reference, {})
