"""Regression suite verifying Wave 1 and Wave 2 access-control and storage invariants."""

from __future__ import annotations

import errno
import json
import os
import stat
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.http import Http404
from django.tasks.base import Task
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_ray import admin as admin_module
from django_ray._blob_storage import (
    configured_filesystem_file_mode,
    write_temporary_payload_stream_hashed,
)
from django_ray.admin import RayTaskExecutionAdmin, TaskAttemptAdmin
from django_ray.decorators import get_ray_task_contract, ray_task
from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    encode_execution_request,
)
from django_ray.input_storage import (
    InputPayloadError,
    load_task_input,
    prepare_task_input,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
from django_ray.ray_job_request_storage import (
    RayJobRequestStorageError,
    decode_ray_job_request_locator,
    load_ray_job_request,
    prepare_ray_job_request,
)
from django_ray.redaction import REDACTED
from django_ray.result_storage import (
    FilesystemResultStorage,
    ResultStorageError,
    ResultStorageIntegrityError,
)
from django_ray.runtime.import_utils import resolve_task_callable
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.workflow.plans import runtime_env_plan_identity


@ray_task
def _bare_ray_task(value: int) -> dict[str, int]:
    return {"value": value}


@ray_task(
    num_cpus=2,
    num_gpus=0.5,
    max_attempts=4,
    retry_jitter=True,
    queue_name="ml",
    timeout_seconds=120,
)
def _parameterized_ray_task(item: str, *, api_key: str = "placeholder") -> dict[str, str]:
    return {"item": item, "api_key": api_key}


def _plain_non_task_callable(value: int) -> int:
    return value + 1


def _make_staff_user(
    *,
    username: str,
    permissions: tuple[tuple[type[Any], str], ...],
) -> Any:
    user_model = get_user_model()
    user = user_model.objects.create_user(
        username=username,
        password="test-password-123!",
        is_staff=True,
    )
    for model, codename in permissions:
        content_type = ContentType.objects.get_for_model(model)
        perm = Permission.objects.get(content_type=content_type, codename=codename)
        user.user_permissions.add(perm)
    return user


def _make_execution_and_attempt(
    *,
    task_id: str,
    queue_name: str = "default",
    state: str = TaskState.FAILED,
) -> tuple[RayTaskExecution, TaskAttempt]:
    execution = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path=_parameterized_ray_task.module_path,
        queue_name=queue_name,
        args_json='["item-1"]',
        kwargs_json='{"api_key": "sensitive-token-value"}',
        state=state,
        attempt_number=1,
        execution_generation=1,
        error_message="failure with password=sensitive-password-value",
    )
    attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=state,
        error_message="attempt failed with token=sensitive-attempt-token",
    )
    return execution, attempt


def _path_from_resultfs_ref(root: Path, reference: str) -> Path:
    unwrapped = reference.removeprefix("rq2ref:v1:")
    parsed = urlsplit(unwrapped)
    assert parsed.scheme == "resultfs"
    rel = parse_qs(parsed.query)["rel"][0]
    return root / rel


class TestBlobStorageFilesystemInvariants:
    """Shared blob storage preserves restrictive file modes and rejects non-regular files."""

    def test_configured_file_mode_enforces_strict_allowlist(self, settings: Any) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto"}
        assert configured_filesystem_file_mode() == 0o600
        assert configured_filesystem_file_mode(0o640) == 0o640
        assert configured_filesystem_file_mode("0o640") == 0o640

        for rejected in (0o644, 0o660, 0o666, 0o755):
            with pytest.raises(ResultStorageError):
                configured_filesystem_file_mode(rejected)

    def test_write_and_store_request_restrictive_mode_across_all_three_storage_callers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        settings: Any,
    ) -> None:
        recorded_modes: list[int] = []
        real_open = os.open

        def tracking_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
            if flags & os.O_CREAT:
                recorded_modes.append(mode)
            return real_open(path, flags, mode, **kwargs)

        # 1. Direct _blob_storage helper check (default 0o600 and configured 0o640).
        temp_dir = tmp_path / "direct"
        temp_dir.mkdir()
        fake_os = SimpleNamespace(
            open=tracking_open,
            fdopen=os.fdopen,
            fsync=os.fsync,
            **({"fchmod": os.fchmod} if hasattr(os, "fchmod") else {}),
        )
        p1, _ = write_temporary_payload_stream_hashed(temp_dir, b'{"a":1}', os_module=fake_os)
        p2, _ = write_temporary_payload_stream_hashed(
            temp_dir, b'{"a":2}', file_mode=0o640, os_module=fake_os
        )
        assert recorded_modes == [0o600, 0o640]
        if os.name == "posix":
            assert stat.S_IMODE(p1.stat().st_mode) == 0o600
            assert stat.S_IMODE(p2.stat().st_mode) == 0o640

        # 2. result_storage, input_storage, and ray_job_request_storage all write with 0o600.
        results_dir = tmp_path / "results"
        inputs_dir = tmp_path / "inputs"
        results_dir.mkdir()
        inputs_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 16,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(inputs_dir),
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(results_dir),
        }

        recorded_modes.clear()
        monkeypatch.setattr(os, "open", tracking_open)

        res_backend = FilesystemResultStorage(root_path=str(results_dir))
        res_ref = res_backend.store(serialized_result='{"result":"ok"}')
        res_path = _path_from_resultfs_ref(results_dir, res_ref)

        prepared_input = prepare_task_input(["oversized-input-value-12345"], {"k": "v"})
        assert prepared_input.input_reference is not None
        inp_path = _path_from_resultfs_ref(inputs_dir, prepared_input.input_reference)

        runtime_env = normalize_runtime_env({})
        plan_id = runtime_env_plan_identity(runtime_env, trust_identity={})
        req = ExecutionRequest(
            identity=ExecutionIdentity(
                task_execution_pk=1,
                task_id="sec-storage-req-1",
                attempt_number=1,
                execution_generation=1,
            ),
            execution_protocol_version=1,
            callable_path=_bare_ray_task.module_path,
            transport_version=1,
            serialized_args="[1]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=runtime_env.profile,
            runtime_env_hash=runtime_env.digest,
            runtime_env_plan_identity=plan_id.as_transport_dict(),
            compiled_graph_submission_transport="ray-job",
        )
        prepared_req = prepare_ray_job_request(encode_execution_request(req), settings.DJANGO_RAY)
        req_path = _path_from_resultfs_ref(inputs_dir, prepared_req.reference)

        assert len(recorded_modes) >= 3
        assert all(mode == 0o600 for mode in recorded_modes)
        if os.name == "posix":
            for path in (res_path, inp_path, req_path):
                assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_symlink_and_non_regular_file_rejected_across_all_three_storage_callers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        settings: Any,
    ) -> None:
        results_dir = tmp_path / "res"
        inputs_dir = tmp_path / "inp"
        results_dir.mkdir()
        inputs_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 16,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(inputs_dir),
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(results_dir),
        }

        res_backend = FilesystemResultStorage(root_path=str(results_dir))
        res_ref = res_backend.store(serialized_result='{"result":"safe"}')
        prepared_input = prepare_task_input(["oversized-input-value-67890"], {})
        assert prepared_input.input_reference is not None

        runtime_env = normalize_runtime_env({})
        plan_id = runtime_env_plan_identity(runtime_env, trust_identity={})
        req = ExecutionRequest(
            identity=ExecutionIdentity(
                task_execution_pk=2,
                task_id="sec-storage-req-2",
                attempt_number=1,
                execution_generation=1,
            ),
            execution_protocol_version=1,
            callable_path=_bare_ray_task.module_path,
            transport_version=1,
            serialized_args="[2]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=runtime_env.profile,
            runtime_env_hash=runtime_env.digest,
            runtime_env_plan_identity=plan_id.as_transport_dict(),
            compiled_graph_submission_transport="ray-job",
        )
        prepared_req = prepare_ray_job_request(encode_execution_request(req), settings.DJANGO_RAY)
        req_locator = decode_ray_job_request_locator(prepared_req.encoded_locator)

        # Non-regular file descriptor (e.g., FIFO) rejected via os.fstat S_ISREG check.
        real_fstat = os.fstat

        def fake_fifo_fstat(fd: int) -> Any:
            real_res = real_fstat(fd)
            return SimpleNamespace(
                st_mode=(real_res.st_mode & ~stat.S_IFMT(real_res.st_mode)) | stat.S_IFIFO,
                st_size=real_res.st_size,
            )

        with monkeypatch.context() as mp:
            mp.setattr(os, "fstat", fake_fifo_fstat)
            with pytest.raises(ResultStorageIntegrityError):
                res_backend.load(reference=res_ref)
            with pytest.raises(InputPayloadError):
                load_task_input(
                    args_json="null",
                    kwargs_json="null",
                    input_reference=prepared_input.input_reference,
                )
            with pytest.raises(RayJobRequestStorageError):
                load_ray_job_request(req_locator)

        # Symlink open error (ELOOP / O_NOFOLLOW) rejected across all 3 storage callers.
        real_open = os.open

        def fake_eloop_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
            if not (flags & os.O_CREAT):
                raise OSError(errno.ELOOP, "Too many levels of symbolic links")
            return real_open(path, flags, mode, **kwargs)

        with monkeypatch.context() as mp:
            mp.setattr(os, "open", fake_eloop_open)
            with pytest.raises(ResultStorageIntegrityError):
                res_backend.load(reference=res_ref)
            with pytest.raises(InputPayloadError):
                load_task_input(
                    args_json="null",
                    kwargs_json="null",
                    input_reference=prepared_input.input_reference,
                )
            with pytest.raises(RayJobRequestStorageError):
                load_ray_job_request(req_locator)


@pytest.mark.django_db
class TestReplayCommandSecurityInvariants:
    """django_ray_replay is strictly read-only, redacts by default, and enforces Task resolution."""

    def test_replay_executes_zero_db_writes_and_redacts_by_default(self) -> None:
        execution, _attempt = _make_execution_and_attempt(task_id="sec-replay-readonly-1")

        out = StringIO()
        err = StringIO()
        with CaptureQueriesContext(connection) as captured:
            call_command(
                "django_ray_replay",
                str(execution.task_id),
                "--format=json",
                stdout=out,
                stderr=err,
            )

        write_sql = [
            q["sql"]
            for q in captured.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert write_sql == []

        report = json.loads(out.getvalue())
        assert report["sensitive_redacted"] is True
        assert report["replay"]["status"] == "succeeded"
        rendered = out.getvalue()
        assert "sensitive-token-value" not in rendered
        assert "sensitive-password-value" not in rendered
        assert REDACTED in rendered

        # Verify DB rows were untouched.
        execution.refresh_from_db()
        assert execution.state == TaskState.FAILED
        assert execution.attempt_number == 1
        assert TaskAttempt.objects.filter(execution=execution).count() == 1

    def test_replay_rejects_non_task_and_disallowed_module_callables(
        self,
        settings: Any,
    ) -> None:
        non_task_exec = RayTaskExecution.objects.create(
            task_id="sec-replay-nontask-1",
            callable_path=f"{__name__}._plain_non_task_callable",
            queue_name="default",
            args_json="[1]",
            kwargs_json="{}",
            state=TaskState.FAILED,
        )
        with pytest.raises(CommandError, match="Failed to resolve task callable"):
            call_command("django_ray_replay", str(non_task_exec.task_id), "--dry-run")

        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "ALLOWED_CALLABLE_MODULES": ["allowed_app.tasks"],
        }
        task_exec, _ = _make_execution_and_attempt(task_id="sec-replay-disallowed-mod-1")
        with pytest.raises(CommandError, match="Failed to resolve task callable"):
            call_command("django_ray_replay", str(task_exec.task_id), "--dry-run")


@pytest.mark.django_db
class TestPurgeExecutionsActiveProtectionInvariants:
    """django_ray_purge_executions never deletes active executions, active leases, or shared blobs."""

    def test_purge_never_deletes_active_rows_or_active_leases_even_with_zero_retention(
        self,
        tmp_path: Path,
        settings: Any,
    ) -> None:
        results_dir = tmp_path / "purge-results"
        results_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(results_dir),
        }

        storage = FilesystemResultStorage(root_path=str(results_dir))
        shared_ref = storage.store(serialized_result='{"shared":true}')
        unshared_ref = storage.store(serialized_result='{"unshared":true}')
        shared_path = _path_from_resultfs_ref(results_dir, shared_ref)
        unshared_path = _path_from_resultfs_ref(results_dir, unshared_ref)

        old_ts = timezone.now() - timedelta(days=60)
        active_ids: list[str] = []
        for active_state in (TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING):
            row = RayTaskExecution.objects.create(
                task_id=f"sec-purge-active-{active_state.lower()}",
                callable_path=_bare_ray_task.module_path,
                queue_name="default",
                state=active_state,
                created_at=old_ts,
                started_at=old_ts if active_state != TaskState.QUEUED else None,
                finished_at=old_ts,
                result_reference=shared_ref if active_state == TaskState.RUNNING else None,
            )
            active_ids.append(str(row.task_id))

        terminal_shared = RayTaskExecution.objects.create(
            task_id="sec-purge-term-shared",
            callable_path=_bare_ray_task.module_path,
            queue_name="default",
            state=TaskState.SUCCEEDED,
            created_at=old_ts,
            finished_at=old_ts,
            result_reference=shared_ref,
        )
        terminal_unshared = RayTaskExecution.objects.create(
            task_id="sec-purge-term-unshared",
            callable_path=_bare_ray_task.module_path,
            queue_name="default",
            state=TaskState.FAILED,
            created_at=old_ts,
            finished_at=old_ts,
            result_reference=unshared_ref,
        )

        active_lease = TaskWorkerLease.objects.create(
            worker_id="sec-purge-active-lease",
            hostname="host-a",
            pid=4001,
            queue_name="default",
            capability_schema_version=1,
            legacy_admission_token=None,
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            is_active=True,
            started_at=old_ts,
            last_heartbeat_at=old_ts,
            lease_expires_at=old_ts + timedelta(seconds=60),
            lease_policy_revision=1,
        )
        inactive_lease = TaskWorkerLease.objects.create(
            worker_id="sec-purge-inactive-lease",
            hostname="host-b",
            pid=4002,
            queue_name="default",
            capability_schema_version=1,
            legacy_admission_token=None,
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            is_active=False,
            started_at=old_ts,
            last_heartbeat_at=old_ts,
            lease_expires_at=old_ts + timedelta(seconds=60),
            lease_policy_revision=1,
            stopped_at=old_ts,
        )

        out = StringIO()
        call_command(
            "django_ray_purge_executions",
            "--older-than-days",
            "0",
            "--delete",
            "--until-empty",
            "--json",
            stdout=out,
        )
        summary = json.loads(out.getvalue())

        # All active executions and active leases MUST remain untouched.
        for active_id in active_ids:
            assert RayTaskExecution.objects.filter(task_id=active_id).exists()
        assert TaskWorkerLease.objects.filter(worker_id=active_lease.worker_id).exists()

        # Terminal rows and inactive lease are purged; shared blob stays while unshared is removed.
        assert not RayTaskExecution.objects.filter(pk=terminal_shared.pk).exists()
        assert not RayTaskExecution.objects.filter(pk=terminal_unshared.pk).exists()
        assert not TaskWorkerLease.objects.filter(worker_id=inactive_lease.worker_id).exists()
        assert shared_path.exists()
        assert not unshared_path.exists()
        assert summary["counts"]["results_skipped_shared"] >= 1


class TestRayTaskCallableResolutionInvariants:
    """@ray_task callables are genuine Django Task instances accepted by resolve_task_callable."""

    def test_ray_task_decorator_produces_django_task_accepted_by_resolver(self) -> None:
        for decorated in (_bare_ray_task, _parameterized_ray_task):
            assert isinstance(decorated, Task)
            resolved = resolve_task_callable(decorated.module_path)
            assert resolved is decorated
            resolved_allowed = resolve_task_callable(
                decorated.module_path,
                allowed_modules=(__name__.split(".", 1)[0],),
            )
            assert resolved_allowed is decorated
            with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
                resolve_task_callable(
                    decorated.module_path,
                    allowed_modules=("unmatched_prefix",),
                )

        contract = get_ray_task_contract(_parameterized_ray_task.using(priority=5))
        assert contract is not None
        assert contract.priority == 5
        assert contract.num_cpus == 2.0
        assert contract.num_gpus == 0.5
        assert contract.max_attempts == 4


@pytest.mark.django_db
class TestAdminWave1AuthorizationInvariantsPreserved:
    """Admin preserves Wave 1 parent scoping, authz gates, bounded cancels, and nosniff headers."""

    def test_task_attempt_admin_enforces_parent_permission_and_queryset_scoping(self) -> None:
        _allowed_exec, _allowed_attempt = _make_execution_and_attempt(
            task_id="sec-adm-tenant-a",
            queue_name="tenant-a",
        )
        _denied_exec, denied_attempt = _make_execution_and_attempt(
            task_id="sec-adm-tenant-b",
            queue_name="tenant-b",
        )

        # Missing parent view_raytaskexecution permission -> PermissionDenied.
        site_basic = AdminSite(name="sec-adm-basic")
        site_basic.register(RayTaskExecution, RayTaskExecutionAdmin)
        site_basic.register(TaskAttempt, TaskAttemptAdmin)
        attempt_admin = site_basic._registry[TaskAttempt]

        user_no_parent = _make_staff_user(
            username="sec-adm-no-parent",
            permissions=(
                (TaskAttempt, "view_taskattempt"),
                (RayTaskExecution, "view_sensitive_task_data"),
            ),
        )
        req = RequestFactory().get(f"/admin/django_ray/taskattempt/{denied_attempt.pk}/sensitive/")
        req.user = user_no_parent
        with pytest.raises(PermissionDenied):
            attempt_admin.sensitive_data_view(req, str(denied_attempt.pk))

        # Custom parent queryset scoping excludes out-of-scope parent -> Http404.
        class _ScopedExecAdmin(RayTaskExecutionAdmin):
            def get_queryset(self, request: Any) -> Any:
                return super().get_queryset(request).filter(queue_name="tenant-a")

        site_scoped = AdminSite(name="sec-adm-scoped")
        site_scoped.register(RayTaskExecution, _ScopedExecAdmin)
        site_scoped.register(TaskAttempt, TaskAttemptAdmin)
        scoped_attempt_admin = site_scoped._registry[TaskAttempt]
        user_with_parent = _make_staff_user(
            username="sec-adm-scoped-user",
            permissions=(
                (TaskAttempt, "view_taskattempt"),
                (RayTaskExecution, "view_raytaskexecution"),
                (RayTaskExecution, "view_sensitive_task_data"),
            ),
        )
        req_scoped = RequestFactory().get(
            f"/admin/django_ray/taskattempt/{denied_attempt.pk}/sensitive/"
        )
        req_scoped.user = user_with_parent
        with pytest.raises(Http404):
            scoped_attempt_admin.sensitive_data_view(req_scoped, str(denied_attempt.pk))

    def test_unauthorized_staff_denied_uniformly_for_missing_and_existing_ids(self) -> None:
        execution, attempt = _make_execution_and_attempt(task_id="sec-adm-uniform-1")
        site = AdminSite(name="sec-adm-uniform")
        site.register(RayTaskExecution, RayTaskExecutionAdmin)
        site.register(TaskAttempt, TaskAttemptAdmin)
        exec_admin = site._registry[RayTaskExecution]
        attempt_admin = site._registry[TaskAttempt]

        user = _make_staff_user(username="sec-adm-zero-perms", permissions=())
        get_req = RequestFactory().get("/admin/django_ray/")
        get_req.user = user
        post_req = RequestFactory().post("/admin/django_ray/")
        post_req.user = user

        for obj_id in (str(execution.pk), "999999"):
            with pytest.raises(PermissionDenied):
                exec_admin.sensitive_data_view(get_req, obj_id)
            with pytest.raises(PermissionDenied):
                exec_admin.observability_view(get_req, obj_id)
            with pytest.raises(PermissionDenied):
                exec_admin.workflow_diagnostics_view(get_req, obj_id)
            with pytest.raises(PermissionDenied):
                exec_admin.workflow_graph_view(get_req, obj_id)
            with pytest.raises(PermissionDenied):
                exec_admin.workflow_node_detail_view(get_req, obj_id)
            with pytest.raises(PermissionDenied):
                exec_admin.cancel_view(post_req, obj_id)

        for attempt_id in (str(attempt.pk), "999999"):
            with pytest.raises(PermissionDenied):
                attempt_admin.sensitive_data_view(get_req, attempt_id)

    def test_bulk_cancel_cap_detail_cancel_authz_and_nosniff_headers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        execution, _ = _make_execution_and_attempt(
            task_id="sec-adm-cancel-1",
            queue_name="restricted",
            state=TaskState.QUEUED,
        )

        class _ObjectScopedExecAdmin(RayTaskExecutionAdmin):
            def has_action_permission(self, request: Any, obj: Any = None) -> bool:
                if not super().has_action_permission(request, obj):
                    return False
                if obj is None:
                    return True
                return getattr(obj, "queue_name", None) == "allowed"

        site = AdminSite(name="admin")
        site.register(RayTaskExecution, _ObjectScopedExecAdmin)
        exec_admin = site._registry[RayTaskExecution]

        changer = _make_staff_user(
            username="sec-adm-changer",
            permissions=(
                (RayTaskExecution, "view_raytaskexecution"),
                (RayTaskExecution, "change_raytaskexecution"),
            ),
        )

        # Detail cancel_view rejects GET with 405 + nosniff and denies unauthorized object POST.
        get_cancel_req = RequestFactory().get(
            f"/admin/django_ray/raytaskexecution/{execution.pk}/cancel/"
        )
        get_cancel_req.user = changer
        get_resp = exec_admin.cancel_view(get_cancel_req, str(execution.pk))
        assert get_resp.status_code == 405
        assert get_resp["X-Content-Type-Options"] == "nosniff"
        assert get_resp["Cache-Control"] == "no-store"

        post_cancel_req = RequestFactory().post(
            f"/admin/django_ray/raytaskexecution/{execution.pk}/cancel/"
        )
        post_cancel_req.user = changer
        with pytest.raises(PermissionDenied):
            exec_admin.cancel_view(post_cancel_req, str(execution.pk))

        # Bulk cancel_tasks rejects oversized batches without cancelling any task.
        rows = [
            SimpleNamespace(pk=idx, attempt_number=1, execution_generation=1)
            for idx in range(1, admin_module.ADMIN_CANCEL_MAX_TASKS + 2)
        ]
        fake_qs = Mock()
        fake_qs.only.return_value = rows
        cancel_spy = Mock(side_effect=AssertionError("must not cancel oversized batch"))
        monkeypatch.setattr(admin_module, "request_task_cancellation", cancel_spy)
        recorded: list[tuple[str, int]] = []
        monkeypatch.setattr(
            exec_admin,
            "message_user",
            lambda _req, msg, level=messages.INFO: recorded.append((msg, level)),
        )
        exec_admin.cancel_tasks(post_cancel_req, fake_qs)
        cancel_spy.assert_not_called()
        assert recorded and recorded[0][1] == messages.ERROR

        # Admin observability & workflow endpoints emit X-Content-Type-Options: nosniff + no-store.
        plain_site = AdminSite(name="admin")
        plain_site.register(RayTaskExecution, RayTaskExecutionAdmin)
        plain_admin = plain_site._registry[RayTaskExecution]
        obs_req = RequestFactory().get(
            f"/admin/django_ray/raytaskexecution/{execution.pk}/observability/"
        )
        obs_req.user = changer
        obs_resp = plain_admin.observability_view(obs_req, str(execution.pk))
        assert obs_resp.status_code == 200
        assert obs_resp["X-Content-Type-Options"] == "nosniff"
        assert obs_resp["Cache-Control"] == "no-store"

        node_req = RequestFactory().get(
            f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/node/"
        )
        node_req.user = changer
        node_resp = plain_admin.workflow_node_detail_view(node_req, str(execution.pk))
        assert node_resp["X-Content-Type-Options"] == "nosniff"
        assert node_resp["Cache-Control"] == "no-store"

        graph_req = RequestFactory().get(
            f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/graph/"
        )
        graph_req.user = changer
        graph_resp = plain_admin.workflow_graph_view(graph_req, str(execution.pk))
        assert graph_resp["X-Content-Type-Options"] == "nosniff"
        assert graph_resp["Cache-Control"] == "no-store"
