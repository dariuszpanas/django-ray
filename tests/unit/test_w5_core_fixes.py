"""Wave 5B regression tests for core, storage, worker, tracing, replay, admin, and config fixes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pdb
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.tasks import task
from django.test import RequestFactory
from django.utils import timezone

from django_ray.admin import RayTaskExecutionAdmin
from django_ray.admin_views.diagnostics import workflow_graph_view
from django_ray.backends import RayTaskBackend
from django_ray.conf.defaults import DEFAULTS
from django_ray.conf.validation import validate_settings
from django_ray.decorators import ray_task
from django_ray.input_storage import load_task_input
from django_ray.management import purge_cursor
from django_ray.management.commands.django_ray_worker import Command as WorkerCommand
from django_ray.management.purge_cursor import (
    ExecutionPurgeCursor,
    PurgeCursor,
    decode_execution_purge_cursor,
    decode_purge_cursor,
    encode_execution_purge_cursor,
    encode_purge_cursor,
)
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
    TaskState,
    TaskWorkerLease,
)
from django_ray.result_storage import FilesystemResultStorage
from django_ray.runner.ray_job import (
    RayJobRequestPreparationError,
    RayJobRunner,
)
from django_ray.runtime.runtime_env import (
    resolve_runtime_env_profile,
    runtime_env_for_execution,
    runtime_env_for_storage,
)
from django_ray.tracing import (
    DEFAULT_MAX_WORKFLOW_NODE_SPANS,
    SPAN_TASK_REMOTE_EXECUTE,
    SPAN_TASK_TERMINAL_PERSIST,
    InMemoryTracer,
    TaskTracingCoordinator,
    TracingConfig,
    _resolve_lifecycle_coordinator,
    clear_tracing_caches,
    configure_task_tracing,
    emit_task_lifecycle_span,
)
from django_ray.worker import claimer, dispatcher
from django_ray.workflow.progress import reads as progress_reads


@task(queue_name="default")
def _w5_plain_task(payload: str) -> str:
    return payload


@ray_task(queue_name="default", timeout_seconds=45, runtime_env_profile="gpu-profile")
def _w5_contract_task(value: int) -> int:
    return value * 2


@task(queue_name="default")
async def _w5_async_simple_task(value: int) -> int:
    step_one = value + 10
    return step_one * 2


@task(queue_name="default")
async def _w5_async_await_task(value: int) -> int:
    initial = value + 1
    await asyncio.sleep(0)
    return initial * 3


def _resultfs_disk_path(root: Path, reference: str) -> Path:
    parsed = urlparse(reference)
    rel = parse_qs(parsed.query)["rel"][0]
    return root.joinpath(*rel.split("/"))


def _make_backend() -> RayTaskBackend:
    return RayTaskBackend(
        "default",
        {
            "QUEUES": ["default"],
            "OPTIONS": {"RAY_ADDRESS": "auto"},
        },
    )


@pytest.mark.django_db(transaction=True)
class TestR3007OuterTxPurgedRestoreAndGraceFloor:
    """R3-007: outer-tx PURGED reactivation restores bytes; --retention-days 0 keeps 60s grace."""

    def test_outer_tx_enqueue_restores_deleted_blob_when_registry_row_was_purged(
        self,
        tmp_path: Path,
        settings: Any,
    ) -> None:
        input_dir = tmp_path / "inputs"
        input_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 32,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(input_dir),
        }
        backend = _make_backend()
        payload = "r3-007-" + ("z" * 128)

        first = backend.enqueue(_w5_plain_task, args=(payload,), kwargs={})
        first_row = RayTaskExecution.objects.get(task_id=first.id)
        assert first_row.input_reference is not None
        ref = str(first_row.input_reference)
        disk_path = _resultfs_disk_path(input_dir, ref)
        assert disk_path.exists()

        # Simulate a concurrent django_ray_purge_inputs --delete finishing between
        # prepare_task_input() and register_task_input() inside an outer atomic block.
        disk_path.unlink()
        assert not disk_path.exists()
        TaskInputPayload.objects.filter(pk=ref).update(
            state=InputPayloadState.PURGED,
            purged_at=timezone.now(),
            cleanup_error="",
        )

        # Even if prepare_task_input ran before deletion (simulated by deleting right
        # before register_task_input via outer atomic + monkeypatch on prepare_task_input),
        # register_task_input(verify_storage=False) MUST restore PURGED tombstones.
        import django_ray.backends as backends_module

        original_prepare = backends_module.prepare_task_input

        def prepare_then_concurrent_purge(*args: Any, **kwargs: Any) -> Any:
            prepared = original_prepare(*args, **kwargs)
            assert prepared.input_reference == ref
            disk_path.unlink()
            TaskInputPayload.objects.filter(pk=ref).update(
                state=InputPayloadState.PURGED,
                purged_at=timezone.now(),
                cleanup_error="",
            )
            return prepared

        old_prepare = backends_module.prepare_task_input
        backends_module.prepare_task_input = prepare_then_concurrent_purge
        try:
            with transaction.atomic():
                second = backend.enqueue(_w5_plain_task, args=(payload,), kwargs={})
        finally:
            backends_module.prepare_task_input = old_prepare

        second_row = RayTaskExecution.objects.get(task_id=second.id)
        assert second_row.input_reference == ref
        assert TaskInputPayload.objects.get(pk=ref).state == InputPayloadState.ACTIVE
        assert disk_path.exists()
        loaded_args, loaded_kwargs = load_task_input(
            args_json=second_row.args_json,
            kwargs_json=second_row.kwargs_json,
            input_reference=second_row.input_reference,
        )
        assert loaded_args == [payload]
        assert loaded_kwargs == {}

        # Codex P2: even when a concurrent purge deletes the blob after prepare_task_input()
        # and rolls back / loses its tombstone (leaving the registry row ACTIVE), enqueueing
        # inside transaction.atomic() must still verify and restore the missing blob.
        def prepare_then_failed_purge_leaves_active(*args: Any, **kwargs: Any) -> Any:
            prepared = original_prepare(*args, **kwargs)
            assert prepared.input_reference == ref
            disk_path.unlink()
            assert TaskInputPayload.objects.get(pk=ref).state == InputPayloadState.ACTIVE
            return prepared

        backends_module.prepare_task_input = prepare_then_failed_purge_leaves_active
        try:
            with transaction.atomic():
                third = backend.enqueue(_w5_plain_task, args=(payload,), kwargs={})
        finally:
            backends_module.prepare_task_input = old_prepare

        third_row = RayTaskExecution.objects.get(task_id=third.id)
        assert third_row.input_reference == ref
        assert disk_path.exists()
        loaded_args_3, loaded_kwargs_3 = load_task_input(
            args_json=third_row.args_json,
            kwargs_json=third_row.kwargs_json,
            input_reference=third_row.input_reference,
        )
        assert loaded_args_3 == [payload]
        assert loaded_kwargs_3 == {}

    def test_purge_inputs_retention_days_zero_enforces_60s_unattached_grace_floor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys
        from types import ModuleType

        now = timezone.now()
        fresh_digest = "1" * 64
        stale_digest = "2" * 64
        fresh_ref = (
            f"resultfs://sha256/{fresh_digest}"
            f"?rel={fresh_digest[:2]}/{fresh_digest[2:4]}/{fresh_digest}.json&bytes=64"
        )
        stale_ref = (
            f"resultfs://sha256/{stale_digest}"
            f"?rel={stale_digest[:2]}/{stale_digest[2:4]}/{stale_digest}.json&bytes=64"
        )
        TaskInputPayload.objects.create(
            reference=fresh_ref,
            backend="filesystem",
            digest=fresh_digest,
            size_bytes=64,
            envelope_version=1,
            payload_kind=InputPayloadKind.TASK_INPUT,
            state=InputPayloadState.ACTIVE,
            created_at=now - timedelta(seconds=10),
            last_used_at=now - timedelta(seconds=10),
        )
        TaskInputPayload.objects.create(
            reference=stale_ref,
            backend="filesystem",
            digest=stale_digest,
            size_bytes=64,
            envelope_version=1,
            payload_kind=InputPayloadKind.TASK_INPUT,
            state=InputPayloadState.ACTIVE,
            created_at=now - timedelta(seconds=120),
            last_used_at=now - timedelta(seconds=120),
        )

        deleted: list[str] = []
        stub_storage = ModuleType("django_ray.input_storage")
        stub_storage.delete_input_reference = deleted.append  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "django_ray.input_storage", stub_storage)

        stdout = StringIO()
        call_command(
            "django_ray_purge_inputs",
            retention_days=0,
            delete=True,
            format="json",
            stdout=stdout,
        )
        report = json.loads(stdout.getvalue())
        assert deleted == [stale_ref]
        assert report["counts"]["purged"] == 1
        assert TaskInputPayload.objects.get(pk=fresh_ref).state == InputPayloadState.ACTIVE
        assert TaskInputPayload.objects.get(pk=stale_ref).state == InputPayloadState.PURGED


@pytest.mark.django_db(transaction=True)
class TestR3008ClaimRenewalAndProcessTaskHandoff:
    """R3-008: claim renews due heartbeats inside lock; process_task hands off on transient fail."""

    def test_claim_renews_lease_when_heartbeat_due_and_avoids_post_claim_heartbeat_gap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cmd = WorkerCommand()
        cmd.stdout = StringIO()
        cmd.execution_mode = "sync"
        cmd.sync_mode = True
        cmd._create_lease("default")

        env = resolve_runtime_env_profile(None)
        stored = runtime_env_for_storage(env, task_id="r3-008-claim-hb")
        task_row = RayTaskExecution.objects.create(
            task_id="r3-008-claim-hb",
            callable_path=f"{__name__}._w5_plain_task",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json=json.dumps(["hi"]),
            kwargs_json=json.dumps({}),
            runtime_env_profile=stored.profile,
            runtime_env_json=stored.serialized,
            runtime_env_hash=stored.digest,
        )

        # Force heartbeat due prior to claim.
        cmd._last_successful_heartbeat_monotonic = 0.0
        renew_flags: list[bool] = []
        orig_lock = cmd._lock_authoritative_leases

        def spy_lock(**kwargs: Any) -> Any:
            renew_flags.append(bool(kwargs.get("renew_heartbeat", True)))
            return orig_lock(**kwargs)

        post_claim_hb_calls: list[bool] = []
        monkeypatch.setattr(cmd, "_lock_authoritative_leases", spy_lock)
        monkeypatch.setattr(
            cmd,
            "_update_lease_heartbeat",
            lambda: post_claim_hb_calls.append(True) or True,
        )
        dispatched: list[int] = []
        monkeypatch.setattr(cmd, "execute_task_sync", lambda t: dispatched.append(int(t.pk)))

        claimed = claimer.claim_and_process_tasks(cmd, ["default"], concurrency=1)
        assert claimed == 1
        assert renew_flags[0] is True
        assert cmd._last_successful_heartbeat_monotonic > 0.0
        assert dispatched == [task_row.pk]
        assert post_claim_hb_calls == []

    def test_process_task_hands_off_running_task_when_transient_heartbeat_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cmd = WorkerCommand()
        cmd.stdout = StringIO()
        cmd.execution_mode = "sync"
        cmd.sync_mode = True
        cmd._create_lease("default")

        now = timezone.now()
        task_row = RayTaskExecution.objects.create(
            task_id="r3-008-handoff",
            callable_path=f"{__name__}._w5_plain_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            started_at=now,
            last_heartbeat_at=now,
            attempt_number=1,
            execution_generation=1,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd._last_successful_heartbeat_monotonic = 0.0
        monkeypatch.setattr(cmd, "_update_lease_heartbeat", lambda: False)

        dispatcher.process_task(cmd, task_row)

        task_row.refresh_from_db()
        assert cmd.lease_ownership_lost is False
        assert task_row.state == TaskState.QUEUED
        assert task_row.claimed_by_worker is None
        assert task_row.started_at is None


@pytest.mark.django_db
class TestR3009NoPreMutationOfRuntimeEnvProfile:
    """R3-009: _apply_task_contract_for_dispatch never mutates runtime_env_profile in memory."""

    def test_apply_task_contract_does_not_mutate_runtime_env_profile_on_unsnapshotted_rows(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "ALLOWED_CALLABLE_MODULES": [__name__],
            "RUNTIME_ENV_PROFILES": {"gpu-profile": {"env_vars": {"GPU": "1"}}},
        }
        cmd = SimpleNamespace(worker_id="worker-r3-009")

        # 1. Direct-ORM unsnapshotted row (runtime_env_hash == "")
        direct_row = RayTaskExecution.objects.create(
            task_id="r3-009-direct",
            callable_path=f"{__name__}._w5_contract_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-r3-009",
            attempt_number=1,
            execution_generation=1,
            runtime_env_profile=None,
            runtime_env_hash="",
            runtime_env_json="",
        )
        dispatcher._apply_task_contract_for_dispatch(cmd, direct_row)
        assert direct_row.runtime_env_profile is None
        assert direct_row.timeout_seconds == 45

        # 2. Empty canonical snapshot row (must still pass runtime_env_for_execution cleanly)
        empty_env = resolve_runtime_env_profile(None)
        stored_empty = runtime_env_for_storage(empty_env, task_id="r3-009-empty-snap")
        snap_row = RayTaskExecution.objects.create(
            task_id="r3-009-empty-snap",
            callable_path=f"{__name__}._w5_contract_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-r3-009",
            attempt_number=1,
            execution_generation=1,
            runtime_env_profile=None,
            runtime_env_hash=stored_empty.digest,
            runtime_env_json=stored_empty.serialized,
        )
        dispatcher._apply_task_contract_for_dispatch(cmd, snap_row)
        assert snap_row.runtime_env_profile is None
        resolved = runtime_env_for_execution(snap_row)
        assert resolved.profile is None
        assert resolved.spec == {}


@pytest.mark.django_db
class TestR3010PublicRayJobReserveLeaseFence:
    """R3-010: RayJobRunner._reserve_public_submission enforces live worker lease before write."""

    def test_reserve_public_submission_rejects_expired_worker_without_stamping_row(
        self,
        tmp_path: Path,
        settings: Any,
    ) -> None:
        storage_dir = tmp_path / "req-storage"
        storage_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "http://127.0.0.1:8265",
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(storage_dir),
            "ALLOWED_CALLABLE_MODULES": [__name__],
        }
        old_ts = timezone.now() - timedelta(minutes=10)
        TaskWorkerLease.objects.create(
            worker_id="dead-worker-r3-010",
            hostname="host-a",
            pid=999,
            queue_name="default",
            capability_schema_version=1,
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
            started_at=old_ts,
            last_heartbeat_at=old_ts,
            lease_expires_at=old_ts + timedelta(seconds=60),
            lease_policy_revision=1,
            is_active=True,
            stopped_at=None,
        )
        env = resolve_runtime_env_profile(None)
        stored = runtime_env_for_storage(env, task_id="r3-010-task")
        row = RayTaskExecution.objects.create(
            task_id="r3-010-task",
            callable_path=f"{__name__}._w5_plain_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="dead-worker-r3-010",
            attempt_number=1,
            execution_generation=1,
            args_json=json.dumps(["payload"]),
            kwargs_json=json.dumps({}),
            runtime_env_profile=stored.profile,
            runtime_env_hash=stored.digest,
            runtime_env_json=stored.serialized,
        )

        runner = RayJobRunner(cluster_address="http://127.0.0.1:8265")
        with pytest.raises(RayJobRequestPreparationError):
            runner.submit(row, row.callable_path, ("payload",), {})

        row.refresh_from_db()
        assert row.ray_job_id is None
        assert row.ray_address is None
        assert row.ray_job_request_reference is None


class TestR3011AndR3016TracingDefaultsAndWorkerCorrelation:
    """R3-011 & R3-016: default 100 workflow node spans + worker started->finished correlation."""

    def test_default_max_workflow_node_spans_aligned_with_conf_defaults(
        self, settings: Any
    ) -> None:
        clear_tracing_caches()
        configure_task_tracing(None)
        assert DEFAULT_MAX_WORKFLOW_NODE_SPANS == 100
        assert DEFAULTS["TRACING_MAX_WORKFLOW_NODE_SPANS"] == 100
        assert TracingConfig().max_workflow_node_spans_per_run == 100

        settings.DJANGO_RAY = {"TRACING_ENABLED": True}
        try:
            coord = _resolve_lifecycle_coordinator()
            assert coord is not None
            assert coord.config.max_workflow_node_spans_per_run == 100
        finally:
            clear_tracing_caches()

    def test_worker_started_and_finished_spans_correlate_without_in_process_enqueue(self) -> None:
        clear_tracing_caches()
        tracer = InMemoryTracer()
        coordinator = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=tracer)
        configure_task_tracing(coordinator)
        try:
            execution = SimpleNamespace(
                pk=9091,
                task_id="worker-only-trace-1",
                attempt_number=1,
                execution_generation=1,
                queue_name="default",
                state="RUNNING",
                runner_mode="ray_core",
            )
            emit_task_lifecycle_span("started", execution)
            execution.state = "SUCCEEDED"
            emit_task_lifecycle_span("finished", execution)
        finally:
            configure_task_tracing(None)
            clear_tracing_caches()

        assert len(tracer.spans) == 2
        started_span, finished_span = tracer.spans
        assert started_span.name == SPAN_TASK_REMOTE_EXECUTE
        assert finished_span.name == SPAN_TASK_TERMINAL_PERSIST
        assert finished_span.trace_id == started_span.trace_id
        assert finished_span.parent_context is not None
        assert finished_span.parent_context.span_id == started_span.span_id


@pytest.mark.django_db
class TestR3012AndR3019ReplayAsyncPdbAndFormatPrecedence:
    """R3-012 & R3-019: async --pdb steps into coroutine body; --json wins over --format=text."""

    def test_async_pdb_steps_into_coroutine_body_for_simple_and_awaiting_tasks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        observed_frames: list[tuple[str, str]] = []

        class _SpyPdb(pdb.Pdb):
            def user_call(self, frame: Any, argument_list: Any) -> None:
                del argument_list
                observed_frames.append(("call", frame.f_code.co_name))

            def user_line(self, frame: Any) -> None:
                observed_frames.append(("line", frame.f_code.co_name))
                self.set_continue()

        monkeypatch.setattr(
            pdb, "runcall", lambda func, *a, **kw: _SpyPdb().runcall(func, *a, **kw)
        )

        for func_name, task_id, expected_result in (
            ("_w5_async_simple_task", "r3-012-async-simple", 30),
            ("_w5_async_await_task", "r3-012-async-await", 18),
        ):
            observed_frames.clear()
            execution = RayTaskExecution.objects.create(
                task_id=task_id,
                callable_path=f"{__name__}.{func_name}",
                queue_name="default",
                state=TaskState.FAILED,
                args_json=json.dumps([5]),
                kwargs_json=json.dumps({}),
            )
            stdout = StringIO()
            call_command("django_ray_replay", execution.task_id, "--pdb", "--json", stdout=stdout)
            payload = json.loads(stdout.getvalue())
            assert payload["replay"]["status"] == "succeeded"
            assert payload["replay"]["result"] == expected_result
            assert ("line", func_name) in observed_frames

    def test_replay_json_flag_wins_over_format_text(self, settings: Any) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        execution = RayTaskExecution.objects.create(
            task_id="r3-019-replay-fmt",
            callable_path=f"{__name__}._w5_plain_task",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            args_json=json.dumps(["ok"]),
            kwargs_json=json.dumps({}),
        )
        stdout = StringIO()
        call_command(
            "django_ray_replay",
            execution.task_id,
            "--format=text",
            "--json",
            "--dry-run",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())
        assert payload["task"]["id"] == "r3-019-replay-fmt"
        assert payload["mode"] == "dry_run"


@pytest.mark.django_db
class TestR3015WorkflowGraphShortCircuitAndLimits:
    """R3-015: read_workflow_progress_collections short-circuits oversized graphs before reads."""

    def test_workflow_graph_view_short_circuits_collections_when_summary_exceeds_node_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from django.contrib import admin as django_admin
        from django.contrib.auth import get_user_model

        execution = RayTaskExecution.objects.create(
            task_id="r3-015-oversized-graph",
            callable_path=f"{__name__}._w5_plain_task",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            args_json="[]",
            kwargs_json="{}",
        )
        run_identity = {
            "schema_version": 1,
            "run_id": "11111111-1111-4111-8111-111111111111",
            "attempt_number": 1,
            "execution_generation": 1,
        }
        publication = {
            "summary_revision": 1,
            "topology_version": 1,
            "detail_revision": 1,
        }
        finished_iso = datetime.now(UTC).isoformat()
        oversized_summary = {
            "schema": "django-ray.workflow-progress-summary",
            "schema_version": 1,
            "task_id": execution.task_id,
            "availability": "AVAILABLE",
            "complete": True,
            "run_identity": run_identity,
            "publication": publication,
            "summary": {
                "schema_version": 3,
                "state": "SUCCEEDED",
                "reporting_policy": "full",
                "run_identity": run_identity,
                "summary_revision": 1,
                "topology_version": 1,
                "detail_revision": 1,
                "timestamps": {
                    "started_at": finished_iso,
                    "updated_at": finished_iso,
                    "finished_at": finished_iso,
                },
                "terminal": {
                    "outcome": "SUCCEEDED",
                    "finished_at": finished_iso,
                },
                "node_counts": {
                    "declared": 150,
                    "discovered": 150,
                    "retained_topology": 150,
                    "retained_detail": 150,
                    "pending": 0,
                    "running": 0,
                    "succeeded": 150,
                    "failed": 0,
                },
                "edge_counts": {
                    "declared": 149,
                    "discovered": 149,
                    "retained_topology": 149,
                },
                "detail": {
                    "availability": "AVAILABLE",
                    "complete": True,
                    "truncation_reasons": [],
                    "topology_version": 1,
                    "detail_revision": 1,
                },
            },
        }

        manifest_called = False
        topology_called = False
        details_called = False

        def fail_manifest(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal manifest_called
            manifest_called = True
            raise AssertionError(
                "manifest must not load when should_load_collections returns False"
            )

        def fail_topology(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal topology_called
            topology_called = True
            raise AssertionError(
                "topology must not load when should_load_collections returns False"
            )

        def fail_details(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal details_called
            details_called = True
            raise AssertionError("details must not load when should_load_collections returns False")

        monkeypatch.setattr(progress_reads, "_load_verified_manifest_in_snapshot", fail_manifest)
        monkeypatch.setattr(progress_reads, "_list_topology_in_snapshot", fail_topology)
        monkeypatch.setattr(progress_reads, "_list_node_details_in_snapshot", fail_details)

        # Verify read_workflow_progress_collections short-circuits directly via predicate.
        monkeypatch.setattr(
            progress_reads,
            "_snapshot_context",
            lambda *_a, **_kw: SimpleNamespace(
                execution=execution,
                task_id=execution.task_id,
                source_schema_version=3,
                public_summary=oversized_summary["summary"],
                availability="AVAILABLE",
                attempt_number=1,
                execution_generation=1,
                run_id="11111111-1111-4111-8111-111111111111",
                summary=None,
                identity=None,
            ),
        )
        monkeypatch.setattr(
            progress_reads,
            "_common_envelope",
            lambda _ctx, *, schema, generated_at: {
                "schema": f"django-ray.{schema}",
                "schema_version": 1,
                "task_id": execution.task_id,
                "availability": "AVAILABLE",
                "complete": True,
                "run_identity": run_identity,
                "publication": publication,
                "generated_at": generated_at.isoformat(),
            },
        )

        # Exercise the real workflow_graph_view wiring end-to-end.
        user_cls = get_user_model()
        staff_user = user_cls.objects.create_superuser(
            username="r3-015-admin", email="admin@example.com", password="pw"
        )
        request = RequestFactory().get(f"/admin/django_ray/raytaskexecution/{execution.pk}/graph/")
        request.user = staff_user
        model_admin = RayTaskExecutionAdmin(RayTaskExecution, django_admin.site)

        response = workflow_graph_view(model_admin, request, str(execution.pk))
        assert response.status_code == 200
        body = json.loads(response.content.decode("utf-8"))
        assert body["status"] == "LIMIT_EXCEEDED"
        assert manifest_called is False
        assert topology_called is False
        assert details_called is False


@pytest.mark.django_db(transaction=True)
class TestR3017PurgeExecutionsMtimeGuardAndKeysetOrder:
    """R3-017: Phase 3 blob mtime <= cutoff guard + (finished_at, pk) keyset pagination."""

    def test_phase3_mtime_guard_retains_recently_modified_blob_and_deletes_old_blob(
        self,
        tmp_path: Path,
        settings: Any,
    ) -> None:
        root = tmp_path / "results_mtime"
        root.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(root),
        }
        storage = FilesystemResultStorage(root)
        old_ref = storage.store_payload(serialized_payload='{"blob":"genuinely-old"}')
        fresh_ref = storage.store_payload(serialized_payload='{"blob":"concurrently-rewritten"}')

        old_ts = timezone.now() - timedelta(days=45)
        old_epoch = old_ts.timestamp()
        old_path = _resultfs_disk_path(root, old_ref)
        os.utime(old_path, (old_epoch, old_epoch))
        # fresh_ref keeps current mtime (> cutoff of 30 days ago).
        fresh_path = _resultfs_disk_path(root, fresh_ref)

        RayTaskExecution.objects.create(
            task_id="r3-017-old-blob",
            callable_path=f"{__name__}._w5_plain_task",
            state=TaskState.SUCCEEDED,
            created_at=old_ts,
            finished_at=old_ts,
            result_reference=old_ref,
        )
        RayTaskExecution.objects.create(
            task_id="r3-017-fresh-blob",
            callable_path=f"{__name__}._w5_plain_task",
            state=TaskState.SUCCEEDED,
            created_at=old_ts,
            finished_at=old_ts,
            result_reference=fresh_ref,
        )

        stdout = StringIO()
        call_command(
            "django_ray_purge_executions",
            older_than_days=30,
            delete=True,
            format="json",
            stdout=stdout,
        )
        report = json.loads(stdout.getvalue())
        assert report["counts"]["executions_deleted"] == 2
        assert report["counts"]["results_deleted"] == 1
        assert report["counts"]["results_skipped_shared"] == 1
        assert not old_path.exists()
        assert fresh_path.exists()
        assert storage.load(reference=fresh_ref) == '{"blob":"concurrently-rewritten"}'

    def test_keyset_pagination_orders_by_finished_at_then_pk_without_skips(self) -> None:
        base = timezone.now() - timedelta(days=60)
        # Deliberately insert later finished_at FIRST (lower pk) and earlier finished_at LATER
        # (higher pk), plus a duplicate finished_at tie-break.
        later_low_pk = RayTaskExecution.objects.create(
            task_id="r3-017-later-low-pk",
            callable_path=f"{__name__}._w5_plain_task",
            state=TaskState.SUCCEEDED,
            created_at=base + timedelta(days=2),
            finished_at=base + timedelta(days=2),
        )
        tie_a = RayTaskExecution.objects.create(
            task_id="r3-017-tie-a",
            callable_path=f"{__name__}._w5_plain_task",
            state=TaskState.SUCCEEDED,
            created_at=base + timedelta(days=1),
            finished_at=base + timedelta(days=1),
        )
        tie_b = RayTaskExecution.objects.create(
            task_id="r3-017-tie-b",
            callable_path=f"{__name__}._w5_plain_task",
            state=TaskState.SUCCEEDED,
            created_at=base + timedelta(days=1),
            finished_at=base + timedelta(days=1),
        )
        earliest_high_pk = RayTaskExecution.objects.create(
            task_id="r3-017-earliest-high-pk",
            callable_path=f"{__name__}._w5_plain_task",
            state=TaskState.SUCCEEDED,
            created_at=base,
            finished_at=base,
        )

        # Page 1 (batch_size=2) must pick earliest_high_pk and tie_a first.
        out1 = StringIO()
        with pytest.raises(CommandError) as err1:
            call_command(
                "django_ray_purge_executions",
                older_than_days=30,
                batch_size=2,
                delete=True,
                skip_leases=True,
                format="json",
                stdout=out1,
            )
        assert err1.value.returncode == 2
        rep1 = json.loads(out1.getvalue())
        assert rep1["counts"]["executions_deleted"] == 2
        assert not RayTaskExecution.objects.filter(pk=earliest_high_pk.pk).exists()
        assert not RayTaskExecution.objects.filter(pk=tie_a.pk).exists()
        assert RayTaskExecution.objects.filter(pk=tie_b.pk).exists()
        assert RayTaskExecution.objects.filter(pk=later_low_pk.pk).exists()

        decoded = decode_execution_purge_cursor(rep1["next_cursor"])
        assert decoded.after_execution_pk == tie_a.pk
        assert decoded.after_finished_at is not None

        # Page 2 resumes from (tie_a.finished_at, tie_a.pk) and deletes tie_b & later_low_pk.
        out2 = StringIO()
        call_command(
            "django_ray_purge_executions",
            older_than_days=30,
            batch_size=2,
            delete=True,
            skip_leases=True,
            cursor=rep1["next_cursor"],
            format="json",
            stdout=out2,
        )
        rep2 = json.loads(out2.getvalue())
        assert rep2["has_more"] is False
        assert rep2["counts"]["executions_deleted"] == 2
        assert RayTaskExecution.objects.count() == 0


class TestR3018AndR3019ConfigCursorProbeAndDrainHelp:
    """R3-018 & R3-019: settings default/validation, PurgeCursor db binding, k8s probe, --drain."""

    def test_require_strict_nested_request_default_and_validation(self) -> None:
        assert DEFAULTS["REQUIRE_STRICT_NESTED_REQUEST"] is False
        validate_settings({"REQUIRE_STRICT_NESTED_REQUEST": False})
        validate_settings({"REQUIRE_STRICT_NESTED_REQUEST": True})
        with pytest.raises(ImproperlyConfigured, match="REQUIRE_STRICT_NESTED_REQUEST"):
            validate_settings({"REQUIRE_STRICT_NESTED_REQUEST": "true"})

    def test_purge_cursor_enforces_database_binding_and_accepts_legacy_tokens(self) -> None:
        cutoff = datetime(2026, 10, 1, tzinfo=UTC)
        cursor = PurgeCursor("s3://bucket/key", cutoff, 30, False, database="analytics")
        token = encode_purge_cursor(cursor)
        decoded = decode_purge_cursor(token)
        assert decoded == cursor
        assert decoded.database == "analytics"

        with pytest.raises(CommandError, match="conflicts"):
            call_command("django_ray_purge_inputs", cursor=token, database="default")

        # Legacy 5-key token without "db" still decodes cleanly as database="default".
        legacy_payload = json.dumps(
            {
                "v": 1,
                "after": "s3://bucket/key",
                "cutoff": cutoff.isoformat(),
                "days": 30,
                "delete": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = b"5" * 12
        encrypted = nonce + purge_cursor._cipher().encrypt(
            nonce, legacy_payload, purge_cursor._CONTEXT
        )
        legacy_token = base64.urlsafe_b64encode(encrypted).decode("ascii").rstrip("=")
        legacy_decoded = decode_purge_cursor(legacy_token)
        assert legacy_decoded.database == "default"

        # ExecutionPurgeCursor round-trips after_finished_at cleanly.
        exec_cursor = ExecutionPurgeCursor(
            after_execution_pk=99,
            after_lease_id="",
            cutoff=cutoff,
            older_than_days=30,
            delete=True,
            skip_leases=True,
            skip_results=False,
            database="default",
            after_finished_at=cutoff - timedelta(days=5),
        )
        assert (
            decode_execution_purge_cursor(encode_execution_purge_cursor(exec_cursor)) == exec_cursor
        )

    def test_worker_sync_k8s_liveness_probe_aligned_with_base_worker_probe(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        sync_manifest = (repo_root / "k8s/overlays/kuberay-kind/worker-sync.yaml").read_text(
            encoding="utf-8"
        )
        assert "(voluntary|nonvoluntary)_ctxt_switches:" in sync_manifest
        assert '$1=="Z"||$1=="X"||$1=="T"||$1=="D"' in sync_manifest

    def test_worker_drain_help_text_documents_passive_queue_quiescence(self) -> None:
        parser = WorkerCommand().create_parser("manage.py", "django_ray_worker")
        by_dest = {action.dest: action.help or "" for action in parser._actions}
        assert "passively" in by_dest["drain"].lower()
        assert "queued/active" in by_dest["drain"].lower()
        assert "queued/active" in by_dest["drain_timeout_seconds"].lower()
