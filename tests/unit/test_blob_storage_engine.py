"""Contract tests for the unified _blob_storage engine, client pools, lock-free I/O, and distributed.py."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import transaction

import django_ray._blob_storage as blob_storage
import django_ray.input_storage as input_storage
import django_ray.payload_registry as payload_registry
import django_ray.ray_job_request_storage as request_storage
import django_ray.result_storage as result_storage
import django_ray.runner.ray_job as ray_job_module
from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    NestedExecutionBoundaryKind,
    encode_execution_request,
)
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
)
from django_ray.ray_job_protocol import (
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    coordination_sha256,
)
from django_ray.result_storage import (
    BlobDeleteOutcome,
    DigestResultStorage,
    FilesystemResultStorage,
    GCSResultStorage,
    ResultStorageError,
    ResultStorageIntegrityError,
    S3ResultStorage,
    clear_blob_client_caches,
    delete_blob_verified,
    delete_result_reference,
    validate_storage_configuration,
)
from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.ray_job import (
    RayJobRunner,
    UntrustedRayAddressError,
    _address_pinned_job_client,
    _bounded_submission_requests,
    clear_job_client_cache,
)
from django_ray.runtime import distributed
from django_ray.runtime.context import durable_task_execution
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.workflow.plans import runtime_env_plan_identity
from django_ray.workflow.progress.spill_capacity import (
    SpillCapacityConfig,
    SpillCapacityController,
    WorkflowProgressSpillCapacityUnavailableError,
)


class _ProviderError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"provider error {status} {code}")
        self.code = status
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class ParamValidationError(ValueError):
    """Simulates botocore ParamValidationError when IfMatch is unsupported."""


class _ContractS3Client:
    """Protocol-faithful in-memory S3 contract harness."""

    def __init__(
        self,
        *,
        reject_if_match: str | None = None,
    ) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.reject_if_match = reject_if_match
        self.head_calls = 0
        self.get_calls = 0
        self.put_calls = 0
        self.delete_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls += 1
        key = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _ProviderError(412, "PreconditionFailed")
        body = bytes(kwargs["Body"])
        etag = f'"{hashlib.sha256(body).hexdigest()}"'
        self.objects[key] = (body, etag)
        return {"ETag": etag}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_calls += 1
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise _ProviderError(404, "NoSuchKey")
        body, etag = self.objects[key]
        return {"ContentLength": len(body), "ETag": etag}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls += 1
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise _ProviderError(404, "NoSuchKey")
        body, etag = self.objects[key]
        return {
            "Body": SimpleNamespace(
                read=lambda n=-1: body[:n] if n >= 0 else body, close=lambda: None
            ),
            "ContentLength": len(body),
            "ETag": etag,
        }

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls.append(dict(kwargs))
        key = (kwargs["Bucket"], kwargs["Key"])
        if "IfMatch" in kwargs:
            if self.reject_if_match == "ParamValidationError":
                raise ParamValidationError("Unknown parameter in input: IfMatch")
            if self.reject_if_match == "NotImplemented":
                raise _ProviderError(501, "NotImplemented")
            if key not in self.objects:
                raise _ProviderError(404, "NoSuchKey")
            _body, etag = self.objects[key]
            if kwargs["IfMatch"] != etag:
                raise _ProviderError(412, "PreconditionFailed")
        self.objects.pop(key, None)
        return {}


class _ContractGCSClient:
    """Protocol-faithful in-memory GCS contract harness."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, int]] = {}
        self.next_generation = 1
        self.reload_calls = 0
        self.download_calls = 0
        self.delete_calls: list[tuple[str, str, int | None]] = []

    def bucket(self, name: str) -> Any:
        client = self

        class _Blob:
            def __init__(self, key: str) -> None:
                self.name = key
                self.size: int | None = None
                self.generation: int | None = None

            def upload_from_string(
                self,
                data: bytes,
                content_type: str = "application/json",  # noqa: ARG002
                *,
                if_generation_match: int,
            ) -> None:
                obj_key = (name, self.name)
                if if_generation_match == 0 and obj_key in client.objects:
                    raise _ProviderError(412, "PreconditionFailed")
                client.objects[obj_key] = (bytes(data), client.next_generation)
                client.next_generation += 1

            def reload(self) -> None:
                client.reload_calls += 1
                obj_key = (name, self.name)
                if obj_key not in client.objects:
                    raise _ProviderError(404, "NotFound")
                data, gen = client.objects[obj_key]
                self.size = len(data)
                self.generation = gen

            def download_as_bytes(
                self,
                *,
                start: int = 0,
                end: int | None = None,
                if_generation_match: int | None = None,
            ) -> bytes:
                client.download_calls += 1
                obj_key = (name, self.name)
                if obj_key not in client.objects:
                    raise _ProviderError(404, "NotFound")
                data, gen = client.objects[obj_key]
                if if_generation_match is not None and if_generation_match != gen:
                    raise _ProviderError(412, "PreconditionFailed")
                return data[start : (end + 1 if end is not None else None)]

            def delete(self, *, if_generation_match: int | None = None) -> None:
                client.delete_calls.append((name, self.name, if_generation_match))
                obj_key = (name, self.name)
                if obj_key not in client.objects:
                    raise _ProviderError(404, "NotFound")
                _data, gen = client.objects[obj_key]
                if if_generation_match is not None and if_generation_match != gen:
                    raise _ProviderError(412, "PreconditionFailed")
                client.objects.pop(obj_key)

        return SimpleNamespace(blob=lambda key: _Blob(key))


async def _async_double(value: int, factor: int = 2) -> int:
    return value * factor


async def _async_add(left: int, right: int) -> int:
    return left + right


def _sync_returning_coroutine(value: int) -> Any:
    return _async_double(value, factor=3)


def _mul(value: int, factor: int = 1) -> int:
    return value * factor


class TestBlobStorageS3AndGCSDeleteContracts:
    """F-017 & F-079: Metadata-only HEAD delete, 404 idempotency, and S3-compat fallback."""

    def test_s3_delete_uses_head_only_and_is_404_idempotent(self) -> None:
        client = _ContractS3Client()
        storage = S3ResultStorage(bucket="test-bucket", prefix="app/results", client=client)
        reference = storage.store(serialized_result='{"huge":"payload"}')

        outcome_first = storage.delete_verified(reference=reference)
        assert outcome_first == BlobDeleteOutcome(
            deleted=True,
            already_missing=False,
            reference=reference,
            backend="s3",
        )
        assert client.head_calls == 1
        assert client.get_calls == 0

        # Second delete must succeed idempotently on 404 without raising.
        outcome_second = storage.delete_verified(reference=reference)
        assert outcome_second == BlobDeleteOutcome(
            deleted=False,
            already_missing=True,
            reference=reference,
            backend="s3",
        )
        assert client.get_calls == 0

        # Explicit ignore_missing=False still raises on 404.
        with pytest.raises(ResultStorageError, match="unavailable from S3"):
            storage.delete_verified(reference=reference, ignore_missing=False)

    @pytest.mark.parametrize("rejection_kind", ["ParamValidationError", "NotImplemented"])
    def test_s3_compatible_store_falls_back_when_if_match_unsupported(
        self,
        rejection_kind: str,
    ) -> None:
        client = _ContractS3Client(reject_if_match=rejection_kind)
        storage = S3ResultStorage(
            bucket="minio-bucket",
            prefix="app/results",
            endpoint_url="http://minio.local:9000",
            client=client,
        )
        reference = storage.store(serialized_result='{"store":"minio"}')

        outcome = storage.delete_verified(reference=reference)
        assert outcome.deleted is True
        assert client.get_calls == 0
        assert len(client.delete_calls) == 2
        assert "IfMatch" in client.delete_calls[0]
        assert "IfMatch" not in client.delete_calls[1]
        assert client.objects == {}

    def test_s3_conditional_delete_false_skips_if_match(self) -> None:
        client = _ContractS3Client(reject_if_match="NotImplemented")
        storage = S3ResultStorage(
            bucket="r2-bucket",
            prefix="app/results",
            endpoint_url="https://r2.example.com",
            client=client,
            conditional_delete=False,
        )
        reference = storage.store(serialized_result='{"store":"r2"}')

        outcome = storage.delete_verified(reference=reference)
        assert outcome.deleted is True
        assert len(client.delete_calls) == 1
        assert "IfMatch" not in client.delete_calls[0]

    def test_s3_head_size_mismatch_fails_closed_before_delete(self) -> None:
        client = _ContractS3Client()
        storage = S3ResultStorage(bucket="test-bucket", prefix="app/results", client=client)
        reference = storage.store(serialized_result='{"size":"original"}')
        only_key = next(iter(client.objects))
        client.objects[only_key] = (b"tampered", '"tampered-etag"')

        with pytest.raises(ResultStorageIntegrityError, match="integrity verification"):
            storage.delete_verified(reference=reference)
        assert client.delete_calls == []

    def test_gcs_delete_uses_reload_only_and_is_404_idempotent(self) -> None:
        client = _ContractGCSClient()
        storage = GCSResultStorage(bucket="gcs-bucket", prefix="app/results", client=client)
        reference = storage.store(serialized_result='{"gcs":"payload"}')

        outcome_first = storage.delete_verified(reference=reference)
        assert outcome_first == BlobDeleteOutcome(
            deleted=True,
            already_missing=False,
            reference=reference,
            backend="gcs",
        )
        assert client.reload_calls == 1
        assert client.download_calls == 0

        outcome_second = storage.delete_verified(reference=reference)
        assert outcome_second == BlobDeleteOutcome(
            deleted=False,
            already_missing=True,
            reference=reference,
            backend="gcs",
        )
        assert client.download_calls == 0

        with pytest.raises(ResultStorageError, match="unavailable from GCS"):
            storage.delete_verified(reference=reference, ignore_missing=False)

    def test_delete_result_reference_and_delete_blob_verified_contracts(
        self,
        tmp_path: Path,
    ) -> None:
        config = {
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        }
        fs = FilesystemResultStorage(tmp_path)
        fs_ref = fs.store(serialized_result='{"ok":true}')

        assert delete_result_reference(fs_ref, config) is True
        assert delete_result_reference(fs_ref, config, ignore_missing=True) is False
        with pytest.raises(ResultStorageError, match="unavailable from filesystem"):
            delete_result_reference(fs_ref, config, ignore_missing=False)

        digest_ref = DigestResultStorage().store(serialized_result='{"stub":true}')
        assert delete_result_reference(digest_ref, config) is False
        outcome = delete_blob_verified(config, digest_ref)
        assert outcome == BlobDeleteOutcome(
            deleted=False,
            already_missing=False,
            reference=digest_ref,
            backend="digest",
        )


class TestAtomicFilesystemAndStaleLockRecovery:
    """F-015, F-019, F-055: Atomic os.link/filelock installs, stream-hash, and stale lock recovery."""

    def test_fresh_filesystem_store_stream_hashes_without_post_fsync_reread(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        read_calls = 0
        original_read = result_storage._read_filesystem_payload

        def spy_read(*args: Any, **kwargs: Any) -> str:
            nonlocal read_calls
            read_calls += 1
            return original_read(*args, **kwargs)

        monkeypatch.setattr(result_storage, "_read_filesystem_payload", spy_read)
        reference = storage.store(serialized_result='{"stream":"hashed"}')

        assert read_calls == 0
        assert list(tmp_path.rglob("*.install-lock")) == []
        assert list(tmp_path.rglob("*.install.lock")) == []
        assert storage.load(reference=reference) == '{"stream":"hashed"}'
        assert read_calls == 1

    def test_hardlink_fallback_reaps_stale_legacy_install_lock_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        payload = '{"fallback":"lock"}'
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        target_dir = tmp_path / digest[:2] / digest[2:4]
        target_dir.mkdir(parents=True)
        stale_dir = target_dir / f".{digest}.json.install-lock"
        stale_dir.mkdir()
        old_mtime = time.time() - 120.0
        os.utime(stale_dir, (old_mtime, old_mtime))

        def _no_hardlinks(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

        monkeypatch.setattr(result_storage.os, "link", _no_hardlinks)
        reference = storage.store(serialized_result=payload)

        assert storage.load(reference=reference) == payload
        assert not stale_dir.exists()
        assert list(tmp_path.rglob("*.install.lock")) == []
        # On POSIX, the persistent per-directory flock target stays on disk (never unlinked).
        if os.name != "nt":
            assert (target_dir / ".django-ray-install.lock").exists()

    def test_spill_capacity_reclaims_dead_pid_and_reused_pid_and_old_orphan_locks(
        self,
        tmp_path: Path,
    ) -> None:
        spill_root = tmp_path / "spill"
        now_ns = 200_000_000_000
        alive_pids = {99991}
        pid_tokens = {99991: "proc-starttime:new-owner", os.getpid(): "proc-starttime:self"}
        ctrl = SpillCapacityController(
            SpillCapacityConfig(
                root_directory=spill_root,
                stale_lease_grace_seconds=10.0,
                lock_timeout_seconds=0.05,
            ),
            pid_alive_fn=lambda pid: pid in alive_pids,
            process_start_token_fn=lambda pid: pid_tokens.get(pid, f"pid:{pid}"),
            clock_ns=lambda: now_ns,
        )
        ctrl.verify_ready()
        lock_dir = spill_root / ".django-ray-spill-capacity.lock"

        # Case 1: Dead PID in owner.json is reclaimed immediately.
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(
            json.dumps(
                {
                    "creator_pid": 77777,
                    "process_start_token": "proc-starttime:dead",
                    "created_at_ns": now_ns,
                }
            ),
            encoding="utf-8",
        )
        diag = ctrl.diagnostics()
        assert diag.active_count == 0
        assert not lock_dir.exists()

        # Case 2: Alive PID whose start token changed (PID reuse) is reclaimed immediately.
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(
            json.dumps(
                {
                    "creator_pid": 99991,
                    "process_start_token": "proc-starttime:old-owner",
                    "created_at_ns": now_ns,
                }
            ),
            encoding="utf-8",
        )
        diag = ctrl.diagnostics()
        assert diag.active_count == 0
        assert not lock_dir.exists()

        # Case 3: Alive PID with matching start token still blocks acquisition cleanly.
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(
            json.dumps(
                {
                    "creator_pid": 99991,
                    "process_start_token": "proc-starttime:new-owner",
                    "created_at_ns": now_ns,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(
            WorkflowProgressSpillCapacityUnavailableError,
            match="Timed out acquiring",
        ):
            ctrl.diagnostics()
        (lock_dir / "owner.json").unlink()
        lock_dir.rmdir()

        # Case 4: Bare orphan lock dir older than stale_grace_seconds is reclaimed.
        lock_dir.mkdir()
        old_mtime = time.time() - 60.0
        os.utime(lock_dir, (old_mtime, old_mtime))
        now_ns = time.time_ns()
        diag = ctrl.diagnostics()
        assert diag.active_count == 0
        assert not lock_dir.exists()

    def test_payload_registry_ignores_filelock_install_entries(self) -> None:
        assert payload_registry._is_temporary_filesystem_entry(
            ".abc.json.install.lock", is_dir=False
        )
        assert payload_registry._is_temporary_filesystem_entry(
            ".abc.json.install-lock", is_dir=True
        )


class TestClientPoolingAndRayJobContracts:
    """F-018 & F-019: Pooled S3/GCS/RayJob clients, bounded submit timeouts, single-RPC get_status."""

    def test_s3_and_gcs_client_pools_reuse_and_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clear_blob_client_caches()
        created_s3: list[dict[str, Any]] = []
        created_gcs: list[int] = []

        class FakeConfig:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        fake_boto3 = SimpleNamespace(
            client=lambda service, **kwargs: (
                created_s3.append({"service": service, **kwargs}) or object()
            )
        )
        fake_botocore_config = SimpleNamespace(Config=FakeConfig)
        fake_gcs = SimpleNamespace(Client=lambda: created_gcs.append(1) or object())

        def fake_import(name: str) -> Any:
            if name == "boto3":
                return fake_boto3
            if name == "botocore.config":
                return fake_botocore_config
            if name == "google.cloud.storage":
                return fake_gcs
            raise ImportError(name)

        monkeypatch.setattr(blob_storage, "_ORIGINAL_IMPORT_MODULE", fake_import)
        try:
            c1 = blob_storage.get_or_create_s3_client(
                endpoint_url="http://minio:9000",
                region_name="us-east-1",
                import_module_fn=fake_import,
            )
            c2 = blob_storage.get_or_create_s3_client(
                endpoint_url="http://minio:9000",
                region_name="us-east-1",
                import_module_fn=fake_import,
            )
            assert c1 is c2
            assert len(created_s3) == 1
            assert isinstance(created_s3[0]["config"], FakeConfig)
            assert created_s3[0]["config"].kwargs["connect_timeout"] == 5.0
            assert created_s3[0]["config"].kwargs["read_timeout"] == 30.0

            g1 = blob_storage.get_or_create_gcs_client(import_module_fn=fake_import)
            g2 = blob_storage.get_or_create_gcs_client(import_module_fn=fake_import)
            assert g1 is g2
            assert len(created_gcs) == 1

            clear_blob_client_caches()
            c3 = blob_storage.get_or_create_s3_client(
                endpoint_url="http://minio:9000",
                region_name="us-east-1",
                import_module_fn=fake_import,
            )
            assert c3 is not c1
            assert len(created_s3) == 2
        finally:
            clear_blob_client_caches()

    def test_ray_job_get_status_uses_single_get_job_info_rpc(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "http://ray-head:8265"}
        runner = RayJobRunner()
        info_calls = 0

        class SingleRpcClient:
            def get_job_status(self, _job_id: str) -> str:
                pytest.fail("get_job_status must not be called when get_job_info returns status")

            def get_job_info(self, _job_id: str) -> Any:
                nonlocal info_calls
                info_calls += 1
                return SimpleNamespace(
                    status=SimpleNamespace(value="SUCCEEDED"),
                    message="completed cleanly",
                    start_time=100,
                    end_time=200,
                    metadata=None,
                    driver_exit_code=0,
                )

        monkeypatch.setattr(runner, "_get_client", lambda _addr=None: SingleRpcClient())
        handle = SubmissionHandle(
            ray_job_id="raysubmit_single_rpc",
            ray_address="http://ray-head:8265",
            submitted_at=datetime.now(UTC),
        )
        info = runner.get_status(handle)
        assert info.status is JobStatus.SUCCEEDED
        assert info.message == "completed cleanly"
        assert info.driver_exit_code == 0
        assert info_calls == 1

    def test_ray_job_submission_bounds_upload_and_submit_timeouts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "http://ray-head:8265",
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "req"),
            "RAY_JOB_SUBMISSION_TIMEOUT_SECONDS": 17.5,
        }
        observed_timeouts: list[tuple[str, float | None]] = []

        class RecordingClient:
            _address = "http://ray-head:8265"

            def _upload_working_dir_if_needed(self, _spec: dict[str, Any]) -> None:
                observed_timeouts.append(
                    ("working_dir", getattr(ray_job_module._REQUEST_TIMEOUT_LOCAL, "timeout", None))
                )

            def _upload_py_modules_if_needed(self, _spec: dict[str, Any]) -> None:
                observed_timeouts.append(
                    ("py_modules", getattr(ray_job_module._REQUEST_TIMEOUT_LOCAL, "timeout", None))
                )

            def submit_job(self, **kwargs: Any) -> str:
                observed_timeouts.append(
                    ("submit_job", getattr(ray_job_module._REQUEST_TIMEOUT_LOCAL, "timeout", None))
                )
                return str(kwargs["submission_id"])

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _addr=None: RecordingClient())
        monkeypatch.setattr(
            ray_job_module,
            "register_and_attach_ray_job_request",
            lambda prepared, **_kwargs: prepared.reference,
        )
        runtime_env = normalize_runtime_env({})
        execution = SimpleNamespace(
            pk=99,
            task_id="bounded-submit-99",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
            runtime_env_profile=runtime_env.profile,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            ray_target_address=None,
            ray_address="http://ray-head:8265",
        )
        runner._submit_serialized_request(
            task_execution=execution,
            callable_path="testproject.tasks.echo_task",
            args_json="[]",
            kwargs_json="{}",
            input_reference=None,
        )
        assert observed_timeouts == [
            ("working_dir", 17.5),
            ("py_modules", 17.5),
            ("submit_job", 17.5),
        ]
        assert getattr(ray_job_module._REQUEST_TIMEOUT_LOCAL, "timeout", None) is None

    def test_ray_job_client_pool_enforces_allowlist_and_closes_sessions(
        self,
        settings: Any,
    ) -> None:
        clear_job_client_cache()
        settings.DJANGO_RAY = {"RAY_ADDRESS": "http://trusted-ray:8265"}
        closed = False

        class FakeSession:
            def close(self) -> None:
                nonlocal closed
                closed = True

        fake_client = SimpleNamespace(_session=FakeSession())
        ray_job_module._JOB_CLIENT_POOL[("http://trusted-ray:8265", ())] = fake_client

        # Untrusted address must still be rejected even when pool is populated.
        with pytest.raises(UntrustedRayAddressError):
            _address_pinned_job_client("http://evil.internal:8265", enforce_allowlist=True)

        # Trusted cached address returns pooled client without re-initializing.
        assert (
            _address_pinned_job_client("http://trusted-ray:8265", enforce_allowlist=True)
            is fake_client
        )

        with _bounded_submission_requests(fake_client, timeout_seconds=12.0):
            assert getattr(ray_job_module._REQUEST_TIMEOUT_LOCAL, "timeout", None) == 12.0
        assert getattr(ray_job_module._REQUEST_TIMEOUT_LOCAL, "timeout", None) is None

        clear_job_client_cache()
        assert closed is True
        assert ray_job_module._JOB_CLIENT_POOL == {}


class TestNamespaceContainmentSymlinksAndGCSPagination:
    """F-074 & F-093: Symlink/realpath containment, nested prefix rejection, symlink mounts, GCS paging."""

    def test_validate_storage_configuration_rejects_parent_child_and_symlink_overlaps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        parent = tmp_path / "shared"
        child = parent / "inputs"
        child.mkdir(parents=True)

        with pytest.raises(ResultStorageError, match="must not reuse the result storage namespace"):
            validate_storage_configuration(
                {
                    "RESULT_STORAGE_BACKEND": "filesystem",
                    "RESULT_STORAGE_FILESYSTEM_PATH": str(parent),
                    "INPUT_STORAGE_BACKEND": "filesystem",
                    "INPUT_STORAGE_FILESYSTEM_PATH": str(child),
                }
            )

        with pytest.raises(ResultStorageError, match="must not reuse the result storage namespace"):
            validate_storage_configuration(
                {
                    "RESULT_STORAGE_BACKEND": "filesystem",
                    "RESULT_STORAGE_FILESYSTEM_PATH": str(child),
                    "INPUT_STORAGE_BACKEND": "filesystem",
                    "INPUT_STORAGE_FILESYSTEM_PATH": str(parent),
                }
            )

        # Symlink resolution via realpath detects aliased mounts.
        real_dir = tmp_path / "real_storage"
        real_dir.mkdir()
        alias_dir = tmp_path / "alias_storage"
        original_realpath = os.path.realpath
        monkeypatch.setattr(
            os.path,
            "realpath",
            lambda p: str(real_dir) if str(p) == str(alias_dir) else original_realpath(p),
        )
        with pytest.raises(ResultStorageError, match="must not reuse the result storage namespace"):
            validate_storage_configuration(
                {
                    "RESULT_STORAGE_BACKEND": "filesystem",
                    "RESULT_STORAGE_FILESYSTEM_PATH": str(real_dir),
                    "INPUT_STORAGE_BACKEND": "filesystem",
                    "INPUT_STORAGE_FILESYSTEM_PATH": str(alias_dir),
                }
            )

    @pytest.mark.parametrize(
        ("result_prefix", "input_prefix", "should_overlap"),
        [
            ("django-ray/shared", "django-ray/shared", True),
            ("django-ray", "django-ray/inputs", True),
            ("django-ray/results/nested", "django-ray/results", True),
            ("", "django-ray/inputs", True),
            ("django-ray/results", "django-ray/results-v2", False),
            ("django-ray/results", "django-ray/inputs", False),
        ],
    )
    def test_validate_storage_configuration_checks_s3_and_gcs_prefix_containment(
        self,
        result_prefix: str,
        input_prefix: str,
        should_overlap: bool,
    ) -> None:
        s3_cfg = {
            "RESULT_STORAGE_BACKEND": "s3",
            "RESULT_STORAGE_S3_BUCKET": "shared-bucket",
            "RESULT_STORAGE_S3_PREFIX": result_prefix,
            "INPUT_STORAGE_BACKEND": "s3",
            "INPUT_STORAGE_S3_BUCKET": "shared-bucket",
            "INPUT_STORAGE_S3_PREFIX": input_prefix,
        }
        gcs_cfg = {
            "RESULT_STORAGE_BACKEND": "gcs",
            "RESULT_STORAGE_GCS_BUCKET": "shared-bucket",
            "RESULT_STORAGE_GCS_PREFIX": result_prefix,
            "INPUT_STORAGE_BACKEND": "gcs",
            "INPUT_STORAGE_GCS_BUCKET": "shared-bucket",
            "INPUT_STORAGE_GCS_PREFIX": input_prefix,
        }
        if should_overlap:
            with pytest.raises(ResultStorageError, match="must not reuse"):
                validate_storage_configuration(s3_cfg)
            with pytest.raises(ResultStorageError, match="must not reuse"):
                validate_storage_configuration(gcs_cfg)
        else:
            validate_storage_configuration(s3_cfg)
            validate_storage_configuration(gcs_cfg)

    def test_rq2_locator_allows_symlinked_shared_mount_while_rejecting_relative_or_traversal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        real_root = tmp_path / "real_mount"
        real_root.mkdir()
        symlink_mount = tmp_path / "symlink_mount"
        original_resolve = Path.resolve

        def fake_resolve(self: Path, strict: bool = False) -> Path:
            if str(self) == str(symlink_mount):
                return real_root
            return original_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", fake_resolve)
        digest = "a" * 64
        base_locator = {
            "backend": "filesystem",
            "digest": digest,
            "reference": f"file://{digest[:2]}/{digest[2:4]}/{digest}.json?bytes=12",
            "request_size_bytes": 12,
            "schema": request_storage.RAY_JOB_REQUEST_LOCATOR_SCHEMA,
            "version": request_storage.RAY_JOB_REQUEST_LOCATOR_VERSION,
        }
        decoded = request_storage._decoded_location(
            {**base_locator, "filesystem_path": str(symlink_mount)},
            backend="filesystem",
        )
        assert decoded == {"filesystem_path": str(symlink_mount)}

        for bad_path in (".", "relative/path", f"{tmp_path}/a/../b"):
            with pytest.raises(request_storage.RayJobRequestLoadError):
                request_storage._decoded_location(
                    {**base_locator, "filesystem_path": bad_path},
                    backend="filesystem",
                )

    def test_input_storage_rejects_nan_and_infinity(self) -> None:
        with pytest.raises(input_storage.InputPayloadValidationError):
            input_storage.prepare_task_input(
                [float("nan")], {}, {"MAX_INLINE_INPUT_SIZE_BYTES": None}
            )
        with pytest.raises(input_storage.InputPayloadValidationError):
            input_storage.prepare_task_input([float("inf")], {}, {"MAX_INLINE_INPUT_SIZE_BYTES": 0})

    def test_gcs_discovery_stops_at_max_pages_without_extra_fetch(self) -> None:
        digest_prefix = "0" * 60
        fetched_pages = 0

        class BoundedPageIterator:
            def __init__(self) -> None:
                self.next_page_token: str | None = "token-1"

            @property
            def pages(self) -> Any:
                nonlocal fetched_pages
                for page_idx in range(5):
                    fetched_pages += 1
                    self.next_page_token = f"token-{page_idx + 1}" if page_idx < 4 else None
                    digest = f"{digest_prefix}{page_idx:04x}"
                    yield [
                        SimpleNamespace(
                            name=f"django-ray/inputs/{digest[:2]}/{digest[2:4]}/{digest}.json",
                            size=16,
                            updated=datetime(2026, 1, 1, tzinfo=UTC),
                        )
                    ]

        fake_client = SimpleNamespace(list_blobs=lambda *_a, **_kw: BoundedPageIterator())
        storage = GCSResultStorage(bucket="bucket", prefix="django-ray/inputs", client=fake_client)
        items, has_more = payload_registry._discover_gcs_objects(
            storage,
            max_objects=100,
            max_pages=2,
        )

        assert fetched_pages == 2
        assert len(items) == 2
        assert has_more is True


@pytest.mark.django_db(transaction=True)
class TestLockFreeStorageTransactions:
    """F-009: Verify remote/external storage I/O runs strictly outside DB transaction.atomic()."""

    def test_register_task_input_standalone_runs_remote_restore_outside_atomic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        cfg = {
            "MAX_INLINE_INPUT_SIZE_BYTES": 0,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        }
        prepared = input_storage.prepare_task_input(["lock-free"], {}, cfg)
        assert prepared.input_reference is not None
        first = input_storage.register_task_input(prepared, cfg)
        assert first is not None
        # Mark purged and delete file so reactivation must hit storage.
        first.state = InputPayloadState.PURGED
        first.save(update_fields=["state"])
        FilesystemResultStorage(tmp_path).delete(reference=prepared.input_reference)

        atomic_states: list[bool] = []
        original_store = FilesystemResultStorage.store_payload

        def spy_store(self: FilesystemResultStorage, *, serialized_payload: str) -> str:
            atomic_states.append(bool(transaction.get_connection("default").in_atomic_block))
            return original_store(self, serialized_payload=serialized_payload)

        monkeypatch.setattr(FilesystemResultStorage, "store_payload", spy_store)
        reactivated = input_storage.register_task_input(prepared, cfg, using="default")
        assert reactivated is not None
        assert reactivated.state == InputPayloadState.ACTIVE
        assert atomic_states == [True]

    def test_register_and_attach_rq2_runs_restore_outside_atomic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        cfg = {
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        }
        runtime_env = normalize_runtime_env({})
        execution = RayTaskExecution.objects.create(
            task_id="rq2-lock-free-task",
            callable_path="testproject.tasks.echo_task",
            state="RUNNING",
            claimed_by_worker="worker-1",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
            args_json="[1]",
            kwargs_json="{}",
            runtime_env_profile=runtime_env.profile,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
        )
        identity = ExecutionIdentity(
            task_execution_pk=int(execution.pk),
            task_id=execution.task_id,
            attempt_number=1,
            execution_generation=1,
        )
        job_id = STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX + coordination_sha256(
            identity
        )
        execution.ray_job_id = job_id
        execution.ray_address = "http://ray-head:8265"
        execution.save(update_fields=["ray_job_id", "ray_address"])

        req = ExecutionRequest(
            identity=identity,
            execution_protocol_version=1,
            callable_path="testproject.tasks.echo_task",
            transport_version=1,
            serialized_args="[1]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=runtime_env.profile,
            runtime_env_hash=runtime_env.digest,
            runtime_env_plan_identity={},
            compiled_graph_submission_transport="ray-job",
        )
        prepared = request_storage.prepare_ray_job_request(encode_execution_request(req), cfg)
        TaskInputPayload.objects.create(
            reference=prepared.reference,
            payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
            backend=prepared.backend,
            digest=prepared.digest,
            size_bytes=prepared.size_bytes,
            envelope_version=prepared.envelope_version,
            state=InputPayloadState.PURGED,
            last_used_at=datetime.now(UTC) - timedelta(days=5),
        )
        FilesystemResultStorage(tmp_path).delete(
            reference=request_storage._native_request_reference(prepared.reference)
        )

        atomic_states: list[bool] = []
        original_restore = request_storage._restore_request_payload

        def spy_restore(item: Any) -> None:
            atomic_states.append(bool(transaction.get_connection("default").in_atomic_block))
            original_restore(item)

        monkeypatch.setattr(request_storage, "_restore_request_payload", spy_restore)
        handle = SubmissionHandle(
            ray_job_id=job_id,
            ray_address="http://ray-head:8265",
            submitted_at=datetime.now(UTC),
        )
        attached = request_storage.register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=handle,
        )
        assert attached == prepared.reference
        assert atomic_states == [True]
        assert (
            TaskInputPayload.objects.get(reference=prepared.reference).state
            == InputPayloadState.ACTIVE
        )

    def test_reap_unregistered_object_deletes_strictly_outside_atomic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        cfg = {
            "MAX_INLINE_INPUT_SIZE_BYTES": 0,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        }
        prepared = input_storage.prepare_task_input(["orphan-item"], {}, cfg)
        assert prepared.input_reference is not None
        payload_file = next(tmp_path.rglob("*.json"))
        old_time = time.time() - 7200
        os.utime(payload_file, (old_time, old_time))

        atomic_states: list[bool] = []
        original_delete = FilesystemResultStorage.delete

        def spy_delete(
            self: FilesystemResultStorage, *, reference: str, ignore_missing: bool = True
        ) -> None:
            atomic_states.append(bool(transaction.get_connection("default").in_atomic_block))
            original_delete(self, reference=reference, ignore_missing=ignore_missing)

        monkeypatch.setattr(FilesystemResultStorage, "delete", spy_delete)
        report = payload_registry.discover_and_reap_unregistered_payloads(
            config=cfg,
            grace_seconds=60,
            delete_unregistered=True,
        )
        assert report.counts["purged"] == 1
        assert atomic_states == [True]
        tombstone = TaskInputPayload.objects.get(reference=prepared.input_reference)
        assert tombstone.state == InputPayloadState.PURGED
        assert tombstone.cleanup_error == ""


class TestDistributedRuntimeEnhancements:
    """F-072, F-001, F-023, F-024: Fail-fast unbounded cancel, ray.put, async, auto-init, DB conn hygiene."""

    def test_bootstrap_does_not_latch_true_without_settings_and_is_thread_safe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import django

        setup_calls = 0
        ready = False

        def fake_setup() -> None:
            nonlocal setup_calls, ready
            time.sleep(0.01)
            setup_calls += 1
            ready = True

        monkeypatch.setattr(distributed, "_django_bootstrapped", False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.setattr("django.apps.apps.ready", False, raising=False)
        distributed._bootstrap_django_if_needed()
        assert distributed._django_bootstrapped is False

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
        monkeypatch.setattr(django, "setup", fake_setup)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: distributed._bootstrap_django_if_needed(), range(4)))
        assert setup_calls == 1
        assert distributed._django_bootstrapped is True

    def test_cached_remotes_invalidate_across_ray_session_reconnects(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        distributed.reset_cached_remotes()
        session = {"id": "session-1"}
        created: list[str] = []

        class SessionRay:
            def get_runtime_context(self) -> Any:
                return SimpleNamespace(get_job_id=lambda: session["id"])

            def remote(self, fn: Any) -> Any:
                marker = f"{fn.__name__}@{session['id']}"
                created.append(marker)
                return marker

        monkeypatch.setitem(sys.modules, "ray", SessionRay())
        first = distributed._get_cached_remote("map")
        second = distributed._get_cached_remote("map")
        assert first == second == "_parallel_map_remote@session-1"

        session["id"] = "session-2"
        third = distributed._get_cached_remote("map")
        assert third == "_parallel_map_remote@session-2"
        assert len(created) == 2
        distributed.reset_cached_remotes()

    def test_parallel_map_uses_ray_put_once_for_shared_func_and_kwargs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        distributed.reset_cached_remotes()
        put_values: list[Any] = []

        class _ObjRef:
            def __init__(self, value: Any) -> None:
                self.value = value

        class PutAwareRay:
            def is_initialized(self) -> bool:
                return True

            def put(self, value: Any) -> _ObjRef:
                put_values.append(value)
                return _ObjRef(value)

            def remote(self, fn: Any) -> Any:
                class _Wrapper:
                    def options(self, **_opts: Any) -> Any:
                        return self

                    def remote(self, *args: Any) -> _ObjRef:
                        unwrapped = tuple(a.value if isinstance(a, _ObjRef) else a for a in args)
                        return _ObjRef(fn(*unwrapped))

                return _Wrapper()

            def wait(
                self, refs: list[Any], num_returns: int = 1, **_kw: Any
            ) -> tuple[list[Any], list[Any]]:
                return refs[:num_returns], refs[num_returns:]

            def get(self, ref: Any) -> Any:
                return ref.value

        monkeypatch.setitem(sys.modules, "ray", PutAwareRay())
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)

        results = distributed.parallel_map(_mul, [1, 2, 3, 4], factor=5)
        assert results == [5, 10, 15, 20]
        # Exactly 2 ray.put calls total: pickled_func once + kwargs dict once!
        assert len(put_values) == 2
        assert isinstance(put_values[0], bytes)
        assert put_values[1] == {"factor": 5}
        distributed.reset_cached_remotes()

    def test_strict_leaf_hashes_pickled_callable_only_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import django_ray.execution_codec as codec

        pickled = pickle.dumps(_mul)
        env_identity = runtime_env_plan_identity(normalize_runtime_env({})).as_transport_dict()
        with durable_task_execution(
            77,
            task_id="dedup-hash-77",
            execution_protocol_version=1,
            attempt_number=1,
            execution_generation=1,
            runtime_env_plan_identity=env_identity,
            strict_execution_request=True,
        ):
            op = distributed._strict_nested_operation(NestedExecutionBoundaryKind.DISTRIBUTED_MAP)
            assert op is not None
            controls = distributed._nested_distributed_request(op, pickled, 0)

        digest_calls = 0
        original_digest = codec.nested_callable_digest

        def spy_digest(raw: bytes) -> str:
            nonlocal digest_calls
            digest_calls += 1
            return original_digest(raw)

        monkeypatch.setattr(codec, "nested_callable_digest", spy_digest)
        with distributed._nested_distributed_leaf_execution(
            pickled,
            *controls,
            boundary_kind=NestedExecutionBoundaryKind.DISTRIBUTED_MAP,
        ):
            pass
        assert digest_calls == 1

    def test_is_ray_available_auto_inits_in_ray_job_driver(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = {"initialized": False, "init_kwargs": None}

        class AutoInitRay:
            def is_initialized(self) -> bool:
                return bool(state["initialized"])

            def init(self, **kwargs: Any) -> None:
                state["init_kwargs"] = kwargs
                state["initialized"] = True

        monkeypatch.setitem(sys.modules, "ray", AutoInitRay())
        assert distributed.is_ray_available() is False
        assert state["init_kwargs"] is None

        with durable_task_execution(101, task_id="job-driver-101", ray_job_driver=True):
            assert distributed.is_ray_available() is True
        assert state["init_kwargs"] == {"address": "auto", "ignore_reinit_error": True}

    def test_async_callables_work_in_sequential_and_worker_paths_with_conn_hygiene(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(distributed, "is_ray_available", lambda: False)
        assert distributed.parallel_map(_async_double, [2, 3], factor=4) == [8, 12]
        assert distributed.parallel_starmap(_async_add, [(1, 2), (5, 7)]) == [3, 12]
        assert distributed.scatter_gather([(_sync_returning_coroutine, (4,), {})]) == [12]

        close_calls: list[str] = []
        monkeypatch.setattr(distributed, "_bootstrap_django_if_needed", lambda: None)
        monkeypatch.setattr(
            distributed,
            "_close_old_django_connections",
            lambda: close_calls.append("close"),
        )
        assert distributed._parallel_map_remote(pickle.dumps(_async_double), 5, {"factor": 3}) == 15
        assert close_calls == ["close", "close"]


class TestBlobStorageEdgeBranches:
    """Cover _blob_storage error, fallback, stale-lock, client-pool, and resolver branches."""

    def test_provider_not_found_file_mode_write_failures_and_install_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from filelock import Timeout as FileLockTimeout

        from django_ray import _blob_storage as bs

        assert bs.is_provider_not_found(FileNotFoundError("gone")) is True

        class _NoSuchKeyError(Exception):
            pass

        _NoSuchKeyError.__name__ = "NoSuchKey"
        assert bs.is_provider_not_found(_NoSuchKeyError()) is True

        # configured_filesystem_file_mode: valid octal str, bad octal str, disallowed mode
        assert bs.configured_filesystem_file_mode("0o640") == 0o640
        with pytest.raises(ResultStorageError, match="0o600 or 0o640"):
            bs.configured_filesystem_file_mode("not-octal")
        with pytest.raises(ResultStorageError, match="0o600 or 0o640"):
            bs.configured_filesystem_file_mode(0o777)

        # write_temporary_payload_stream_hashed: OSError during write + unlink OSError swallow
        class _FailFsyncOs:
            O_WRONLY = os.O_WRONLY
            O_CREAT = os.O_CREAT
            O_EXCL = os.O_EXCL

            def open(self, path: Path, flags: int, mode: int) -> int:
                return os.open(path, flags, mode)

            def fchmod(self, _fd: int, _mode: int) -> None:
                pass

            def fdopen(self, fd: int, mode: str) -> Any:
                return os.fdopen(fd, mode)

            def fsync(self, _fd: int) -> None:
                raise OSError("disk IO error")

        orig_unlink = Path.unlink
        monkeypatch.setattr(
            Path,
            "unlink",
            lambda self, missing_ok=False: (
                (_ for _ in ()).throw(OSError("unlink failed"))
                if ".tmp" in self.name
                else orig_unlink(self, missing_ok=missing_ok)
            ),
        )
        with pytest.raises(OSError, match="temporary result payload write failed"):
            bs.write_temporary_payload_stream_hashed(tmp_path, b"abc", os_module=_FailFsyncOs())
        monkeypatch.undo()

        # _reap_legacy_install_lock_dir: non-directory file ignored, rmdir OSError swallowed
        not_a_dir = tmp_path / "regular-lock"
        not_a_dir.write_text("x", encoding="utf-8")
        bs._reap_legacy_install_lock_dir(not_a_dir)
        assert not_a_dir.exists()

        non_empty_dir = tmp_path / "non-empty.install-lock"
        non_empty_dir.mkdir()
        (non_empty_dir / "keep").write_text("x", encoding="utf-8")
        old_ts = time.time() - 7200
        os.utime(non_empty_dir, (old_ts, old_ts))
        bs._reap_legacy_install_lock_dir(non_empty_dir)
        assert non_empty_dir.exists()

        # install_temporary_payload_atomic: existing file returns False; FileExistsError on link;
        # FileLock fallback existing file and FileLockTimeout
        src_tmp = tmp_path / "src.tmp"
        dst = tmp_path / "dst.json"
        src_tmp.write_bytes(b"123")
        dst.write_bytes(b"123")
        assert bs.install_temporary_payload_atomic(src_tmp, dst) is False
        dst.unlink()

        class _RaceLinkOs:
            def link(self, _a: Path, _b: Path) -> None:
                raise FileExistsError("raced")

        assert bs.install_temporary_payload_atomic(src_tmp, dst, os_module=_RaceLinkOs()) is False

        class _UnsupportedLinkOs:
            def link(self, _a: Path, _b: Path) -> None:
                err = OSError("no hardlink")
                err.errno = errno.EOPNOTSUPP
                raise err

            def replace(self, a: Path, b: Path) -> None:
                os.replace(a, b)

        class _RacyFileLock:
            def __init__(self, path: str, timeout: float = -1) -> None:
                self.path = path
                self.timeout = timeout

            def __enter__(self) -> _RacyFileLock:
                dst.write_bytes(b"created-by-peer")
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

        monkeypatch.setattr(bs, "FileLock", _RacyFileLock)
        assert (
            bs.install_temporary_payload_atomic(src_tmp, dst, os_module=_UnsupportedLinkOs())
            is False
        )
        dst.unlink()

        class _TimeoutFileLock:
            def __init__(self, path: str, timeout: float = -1) -> None:
                self.path = path

            def __enter__(self) -> _TimeoutFileLock:
                raise FileLockTimeout(self.path)

            def __exit__(self, *_a: Any) -> None:
                pass

        monkeypatch.setattr(bs, "FileLock", _TimeoutFileLock)
        with pytest.raises(OSError, match="installation lock is unavailable"):
            bs.install_temporary_payload_atomic(src_tmp, dst, os_module=_UnsupportedLinkOs())
        monkeypatch.undo()

    def test_stale_lock_dir_and_client_fallback_and_delete_blob_verified(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from django_ray import _blob_storage as bs

        # _read_lock_owner_metadata on non-regular (directory) returns None
        dir_owner = tmp_path / "owner_dir"
        dir_owner.mkdir()
        assert bs._read_lock_owner_metadata(dir_owner) is None

        # _try_reclaim_stale_lock_dir: missing path, non-dir file, rename OSError, rmtree OSError
        assert (
            bs._try_reclaim_stale_lock_dir(
                tmp_path / "missing_lock",
                now_ns=time.time_ns(),
                stale_grace_seconds=1.0,
                pid_alive_fn=lambda _p: False,
                process_start_token_fn=lambda _p: "t",
            )
            is False
        )
        file_not_dir = tmp_path / "file_not_dir"
        file_not_dir.write_text("x", encoding="utf-8")
        assert (
            bs._try_reclaim_stale_lock_dir(
                file_not_dir,
                now_ns=time.time_ns(),
                stale_grace_seconds=1.0,
                pid_alive_fn=lambda _p: False,
                process_start_token_fn=lambda _p: "t",
            )
            is False
        )

        lock_dir = tmp_path / "reclaim_err"
        lock_dir.mkdir()
        monkeypatch.setattr(os, "rename", lambda _a, _b: (_ for _ in ()).throw(OSError("busy")))
        assert (
            bs._try_reclaim_stale_lock_dir(
                lock_dir,
                now_ns=time.time_ns() + 10_000_000_000,
                stale_grace_seconds=0.0,
                pid_alive_fn=lambda _p: False,
                process_start_token_fn=lambda _p: "t",
            )
            is False
        )
        monkeypatch.undo()

        # acquire_stale_safe_lock_dir when owner file open raises OSError + release_stale_safe_lock_dir
        lock_dir2 = tmp_path / "acquire_owner_err"
        real_os_open = os.open

        def _fail_owner_open(path: Any, *args: Any, **kwargs: Any) -> int:
            if Path(path).name == bs._LOCK_OWNER_FILENAME:
                raise OSError("ro")
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", _fail_owner_open)
        acquired = bs.acquire_stale_safe_lock_dir(
            lock_dir2,
            timeout_seconds=1.0,
            stale_grace_seconds=10.0,
            pid_alive_fn=lambda _p: True,
            process_start_token_fn=lambda _p: "tok",
        )
        assert acquired == lock_dir2
        monkeypatch.undo()

        # Concurrent stale reclamation + replacement acquisition is serialized by the kernel FileLock
        # so a racing stale reclaimer can never evict a newly acquired live lock directory.
        import threading

        lock_dir_race = tmp_path / "race_lock"
        lock_dir_race.mkdir()
        (lock_dir_race / bs._LOCK_OWNER_FILENAME).write_text(
            json.dumps({"creator_pid": 999999, "process_start_token": "dead-token"}),
            encoding="utf-8",
        )
        reclaim_results: list[bool] = []
        live_pid = os.getpid()

        def _race_reclaim() -> None:
            reclaim_results.append(
                bs._try_reclaim_stale_lock_dir(
                    lock_dir_race,
                    now_ns=time.time_ns(),
                    stale_grace_seconds=60.0,
                    pid_alive_fn=lambda pid: pid == live_pid,
                    process_start_token_fn=lambda pid: f"live-{pid}",
                )
            )

        t1 = threading.Thread(target=_race_reclaim)
        t2 = threading.Thread(target=_race_reclaim)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        assert sorted(reclaim_results) == [False, True]

        # Now acquire a live replacement and verify a concurrent stale reclaim cannot evict it.
        live_lock = bs.acquire_stale_safe_lock_dir(
            lock_dir_race,
            timeout_seconds=1.0,
            stale_grace_seconds=60.0,
            pid_alive_fn=lambda pid: pid == live_pid,
            process_start_token_fn=lambda pid: f"live-{pid}",
        )
        assert (
            bs._try_reclaim_stale_lock_dir(
                lock_dir_race,
                now_ns=time.time_ns(),
                stale_grace_seconds=60.0,
                pid_alive_fn=lambda pid: pid == live_pid,
                process_start_token_fn=lambda pid: f"live-{pid}",
            )
            is False
        )
        assert live_lock.is_dir()
        assert (live_lock / bs._LOCK_OWNER_FILENAME).is_file()
        bs.release_stale_safe_lock_dir(live_lock)
        assert not live_lock.exists()

        # release_stale_safe_lock_dir swallows unlink and rmdir OSErrors
        monkeypatch.setattr(
            Path, "unlink", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("unlink"))
        )
        monkeypatch.setattr(os, "rmdir", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("rmdir")))
        bs.release_stale_safe_lock_dir(lock_dir2)
        monkeypatch.undo()
        bs.release_stale_safe_lock_dir(lock_dir2)

        # get_or_create_s3_client: botocore.config raises + boto3.client rejects config kwarg
        def fake_import_no_botocore(name: str) -> Any:
            if name == "botocore.config":
                raise RuntimeError("no config")
            return SimpleNamespace(client=lambda _svc, **_kw: ("client", _kw))

        client_no_cfg = bs.get_or_create_s3_client(import_module_fn=fake_import_no_botocore)
        assert client_no_cfg[0] == "client"

        def fake_import_typeerror_cfg(name: str) -> Any:
            if name == "botocore.config":
                return SimpleNamespace(Config=lambda **_kw: object())

            def _client(_svc: str, **kw: Any) -> Any:
                if "config" in kw:
                    raise TypeError("unexpected config")
                return ("fallback-client", kw)

            return SimpleNamespace(client=_client)

        client_fb = bs.get_or_create_s3_client(import_module_fn=fake_import_typeerror_cfg)
        assert client_fb[0] == "fallback-client"

        # delete_blob_verified: missing resolver error + DigestResultStorage + custom backend
        saved_resolver = bs._blob_backend_resolver
        try:
            bs._blob_backend_resolver = None
            with pytest.raises(ResultStorageError, match="resolver is not registered"):
                bs.delete_blob_verified(None, "resultfs://sha256/" + ("a" * 64))
        finally:
            bs._blob_backend_resolver = saved_resolver

        class DigestResultStorage:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete(self, *, reference: str) -> None:
                self.deleted.append(reference)

        digest_backend = DigestResultStorage()
        digest_out = bs.delete_blob_verified(digest_backend, "oversize://sha256/" + ("a" * 64))
        assert digest_out.deleted is False
        assert digest_out.backend == "digest"

        class CustomBackend:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete(self, *, reference: str) -> None:
                self.deleted.append(reference)

        custom_backend = CustomBackend()
        custom_out = bs.delete_blob_verified(custom_backend, "custom://ref")
        assert custom_out.deleted is True
        assert custom_out.backend == "custom"


@pytest.mark.django_db(transaction=True)
class TestPayloadRegistryAndResultStorageEdgeBranches:
    """Cover edge error, pagination, reap, and GCS/FS verification branches."""

    def test_payload_registry_discovery_and_reap_edge_branches(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from django.db import IntegrityError

        from django_ray.models import InputPayloadKind, InputPayloadState, TaskInputPayload

        # Validation guards in discover_and_reap_unregistered_payloads
        with pytest.raises(ValueError, match="grace_seconds"):
            payload_registry.discover_and_reap_unregistered_payloads(grace_seconds=1)
        with pytest.raises(ValueError, match="max_objects"):
            payload_registry.discover_and_reap_unregistered_payloads(max_objects=0)
        with pytest.raises(ValueError, match="max_pages"):
            payload_registry.discover_and_reap_unregistered_payloads(max_pages=0)
        with pytest.raises(ValueError, match="max_details"):
            payload_registry.discover_and_reap_unregistered_payloads(max_details=101)
        with pytest.raises(ResultStorageError, match="Unsupported payload inventory backend"):
            payload_registry.discover_and_reap_unregistered_payloads(
                storage_backend=("custom", object())
            )

        # _discover_filesystem_objects: root stat OSError, max_objects truncation
        root = tmp_path / "inv_root"
        root.mkdir()
        fs_storage = FilesystemResultStorage(root)
        d1 = "1" * 64
        d2 = "2" * 64
        for d in (d1, d2):
            p = root / d[:2] / d[2:4]
            p.mkdir(parents=True, exist_ok=True)
            (p / f"{d}.json").write_text('{"a":1}', encoding="utf-8")
        items, has_more = payload_registry._discover_filesystem_objects(fs_storage, max_objects=1)
        assert len(items) == 1 and has_more is True

        # _discover_s3_objects: Contents=None, max_objects cut, max_pages cut
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        s3_none = S3ResultStorage(
            bucket="bkt",
            prefix="pref",
            client=SimpleNamespace(list_objects_v2=lambda **_kw: {"Contents": None}),
        )
        assert payload_registry._discover_s3_objects(s3_none, max_objects=5, max_pages=2) == (
            [],
            False,
        )
        s3_many = S3ResultStorage(
            bucket="bkt",
            prefix="pref",
            client=SimpleNamespace(
                list_objects_v2=lambda **_kw: {
                    "Contents": [
                        {
                            "Key": f"pref/{d1[:2]}/{d1[2:4]}/{d1}.json",
                            "Size": 10,
                            "LastModified": ts,
                        },
                        {
                            "Key": f"pref/{d2[:2]}/{d2[2:4]}/{d2}.json",
                            "Size": 10,
                            "LastModified": ts,
                        },
                    ],
                    "IsTruncated": True,
                    "NextContinuationToken": "tok-2",
                }
            ),
        )
        s3_items, s3_more = payload_registry._discover_s3_objects(
            s3_many, max_objects=1, max_pages=2
        )
        assert len(s3_items) == 1 and s3_more is True
        s3_items2, s3_more2 = payload_registry._discover_s3_objects(
            s3_many, max_objects=10, max_pages=1
        )
        assert len(s3_items2) == 2 and s3_more2 is True

        # _discover_gcs_objects: TypeError fallback, pages without next_page_token peek, error paths
        gcs_blob1 = SimpleNamespace(
            name=f"pref/{d1[:2]}/{d1[2:4]}/{d1}.json", size=10, updated=None, time_created=ts
        )
        gcs_blob2 = SimpleNamespace(
            name=f"pref/{d2[:2]}/{d2[2:4]}/{d2}.json", size=10, updated=ts, time_created=None
        )

        class _NoTokenPages:
            def __init__(self, pages: list[list[Any]]) -> None:
                self.pages = iter(pages)

        def _legacy_list_blobs(bucket: str, prefix: str = "", **extra: Any) -> Any:
            if extra:
                raise TypeError("legacy signature")
            return _NoTokenPages([[gcs_blob1], [gcs_blob2]])

        gcs_legacy = GCSResultStorage(
            bucket="bkt", prefix="pref", client=SimpleNamespace(list_blobs=_legacy_list_blobs)
        )
        g_items, g_more = payload_registry._discover_gcs_objects(
            gcs_legacy, max_objects=10, max_pages=1
        )
        assert len(g_items) == 1 and g_more is True

        # Non-pages iterator max_objects cut & error
        gcs_flat = GCSResultStorage(
            bucket="bkt",
            prefix="pref",
            client=SimpleNamespace(list_blobs=lambda *_a, **_kw: iter([gcs_blob1, gcs_blob2])),
        )
        gf_items, gf_more = payload_registry._discover_gcs_objects(
            gcs_flat, max_objects=1, max_pages=2
        )
        assert len(gf_items) == 1 and gf_more is True

        def _broken_gen() -> Any:
            yield gcs_blob1
            raise RuntimeError("gcs stream error")

        gcs_broken = GCSResultStorage(
            bucket="bkt",
            prefix="pref",
            client=SimpleNamespace(list_blobs=lambda *_a, **_kw: _broken_gen()),
        )
        with pytest.raises(ResultStorageError, match="Failed to iterate GCS payload inventory"):
            payload_registry._discover_gcs_objects(gcs_broken, max_objects=10, max_pages=2)

        # _current_object_modified_at: FS non-regular/OSError, GCS time_created fallback
        raw_dir = payload_registry._RawDiscoveredObject(
            fingerprint_sha256="f" * 64,
            malformed=False,
            storage_key="subdir",
        )
        (root / "subdir").mkdir(exist_ok=True)
        assert payload_registry._current_object_modified_at(fs_storage, raw_dir) is None
        raw_gcs = payload_registry._RawDiscoveredObject(
            fingerprint_sha256="f" * 64,
            malformed=False,
            storage_key=gcs_blob1.name,
            modified_at=ts,
        )
        gcs_reload_client = SimpleNamespace(
            bucket=lambda _b: SimpleNamespace(
                blob=lambda _k: SimpleNamespace(reload=lambda: None, updated=None, time_created=ts)
            )
        )
        gcs_reload = GCSResultStorage(bucket="bkt", prefix="pref", client=gcs_reload_client)
        assert payload_registry._current_object_modified_at(gcs_reload, raw_gcs) == ts

        # _reap_unregistered_object: load error -> "failed"; IntegrityError -> "skipped";
        # post-create mtime drift -> "failed"
        raw_file = payload_registry._RawDiscoveredObject(
            fingerprint_sha256="e" * 64,
            malformed=False,
            digest=d1,
            size_bytes=7,
            native_reference=f"resultfs://sha256/{d1}?rel={d1[:2]}/{d1[2:4]}/{d1}.json&bytes=7",
            modified_at=ts,
            storage_key=f"{d1[:2]}/{d1[2:4]}/{d1}.json",
        )
        old_ts = ts.timestamp()
        os.utime(root / d1[:2] / d1[2:4] / f"{d1}.json", (old_ts, old_ts))
        refs = payload_registry._PayloadRegistryReferences(
            canonical=raw_file.native_reference,
            aliases=(raw_file.native_reference,),
        )
        monkeypatch.setattr(
            fs_storage, "load", lambda **_kw: (_ for _ in ()).throw(OSError("io fail"))
        )
        assert (
            payload_registry._reap_unregistered_object(
                fs_storage,
                backend_name="filesystem",
                item=raw_file,
                references=refs,
                cutoff=datetime(2026, 1, 1, tzinfo=UTC),
                config={},
                use_default_delete=False,
                using="default",
            )
            == "failed"
        )
        monkeypatch.undo()

        monkeypatch.setattr(
            payload_registry,
            "_detect_payload_envelope_kind",
            lambda *_a, **_kw: (InputPayloadKind.TASK_INPUT, 1, raw_file.native_reference),
        )
        monkeypatch.setattr(
            TaskInputPayload.objects,
            "using",
            lambda _u: SimpleNamespace(
                select_for_update=lambda: SimpleNamespace(
                    filter=lambda **_kw: SimpleNamespace(
                        order_by=lambda *_a: SimpleNamespace(values_list=lambda *_a, **_kw: [])
                    )
                ),
                filter=lambda **_kw: SimpleNamespace(
                    select_for_update=lambda: SimpleNamespace(
                        filter=lambda **_kw2: SimpleNamespace(
                            order_by=lambda *_a: SimpleNamespace(values_list=lambda *_a, **_kw2: [])
                        )
                    )
                ),
                create=lambda **_kw: (_ for _ in ()).throw(IntegrityError("dup")),
            ),
        )
        # Exercise reverse-scan invalid active reference -> ambiguous count
        monkeypatch.undo()
        TaskInputPayload.objects.create(
            reference="invalid://bad-ref",
            payload_kind=InputPayloadKind.TASK_INPUT,
            backend="filesystem",
            digest="9" * 64,
            size_bytes=5,
            envelope_version=1,
            state=InputPayloadState.ACTIVE,
            last_used_at=ts,
        )
        rep = payload_registry.discover_and_reap_unregistered_payloads(
            config={
                "INPUT_STORAGE_BACKEND": "filesystem",
                "INPUT_STORAGE_FILESYSTEM_PATH": str(root),
            },
            storage_backend=("filesystem", fs_storage),
            now=datetime(2026, 1, 1, tzinfo=UTC),
            using="default",
        )
        assert rep.counts["ambiguous"] >= 1

    def test_result_storage_gcs_metadata_and_fs_verification_edge_branches(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from django_ray import result_storage as rs

        # _read_filesystem_payload: Patched Path.stat error & size mismatch; ELOOP integrity error
        storage = FilesystemResultStorage(tmp_path / "rs_edge")
        ref = storage.store_payload(serialized_payload='{"ok":1}')
        meta = storage._filesystem_reference(ref)
        path = storage._path_for_reference(meta)

        monkeypatch.setattr(Path, "stat", lambda _self, **_kw: (_ for _ in ()).throw(OSError("no")))
        with pytest.raises(ResultStorageError, match="unavailable"):
            rs._read_filesystem_payload(path, meta, unavailable_message="unavailable")
        monkeypatch.setattr(Path, "stat", lambda _self, **_kw: SimpleNamespace(st_size=999999))
        with pytest.raises(rs.ResultStorageIntegrityError):
            rs._read_filesystem_payload(path, meta, unavailable_message="unavailable")
        monkeypatch.undo()

        # _verify_filesystem_delete: missing file -> False; tampered content -> integrity error
        missing_path = tmp_path / "does-not-exist.json"
        assert rs._verify_filesystem_delete(missing_path, meta) is False
        path.write_bytes(b"X" * meta.size_bytes)
        with pytest.raises(rs.ResultStorageIntegrityError):
            rs._verify_filesystem_delete(path, meta)

        # GCSResultStorage._reload_verified_blob_metadata & delete_verified edge branches
        d = "a" * 64
        gcs_ref = f"gs://bkt/pref/{d[:2]}/{d[2:4]}/{d}.json?bytes=10"

        def _make_gcs(blob_obj: Any) -> GCSResultStorage:
            return GCSResultStorage(
                bucket="bkt",
                prefix="pref",
                client=SimpleNamespace(bucket=lambda _b: SimpleNamespace(blob=lambda _k: blob_obj)),
            )

        # 404 with ignore_missing=False raises ResultStorageError
        blob_404 = SimpleNamespace(
            reload=lambda: (_ for _ in ()).throw(FileNotFoundError("NotFound"))
        )
        with pytest.raises(ResultStorageError, match="unavailable from GCS"):
            _make_gcs(blob_404).delete_verified(reference=gcs_ref, ignore_missing=False)

        # Unexpected reload error
        blob_err = SimpleNamespace(reload=lambda: (_ for _ in ()).throw(RuntimeError("net")))
        with pytest.raises(ResultStorageError, match="unavailable from GCS"):
            _make_gcs(blob_err).delete_verified(reference=gcs_ref)

        # Size mismatch & invalid generation
        blob_bad_size = SimpleNamespace(reload=lambda: None, size=99, generation=1)
        with pytest.raises(rs.ResultStorageIntegrityError):
            _make_gcs(blob_bad_size).delete_verified(reference=gcs_ref)

        blob_bad_gen = SimpleNamespace(reload=lambda: None, size=10, generation=0)
        with pytest.raises(rs.ResultStorageIntegrityError):
            _make_gcs(blob_bad_gen).delete_verified(reference=gcs_ref)

        # delete() raises 404 -> already_missing=True when ignore_missing=True
        blob_del_404 = SimpleNamespace(
            reload=lambda: None,
            size=10,
            generation=5,
            delete=lambda **_kw: (_ for _ in ()).throw(FileNotFoundError("NotFound")),
        )
        out_404 = _make_gcs(blob_del_404).delete_verified(reference=gcs_ref, ignore_missing=True)
        assert out_404.already_missing is True and out_404.deleted is False

        # _validate_filesystem_setting, _validated_config_prefix, _normalized_s3_endpoint_namespace
        with pytest.raises(ResultStorageError, match="printable path"):
            rs._validate_filesystem_setting({"P": "bad\npath"}, "P", required=True)
        with pytest.raises(ResultStorageError, match="canonical object-key prefix"):
            rs._validated_config_prefix("/leading-slash/", "PREF")
        assert rs._normalized_s3_endpoint_namespace("") is None
        assert rs._normalized_s3_endpoint_namespace("https://s3.local/") == "https://s3.local"
