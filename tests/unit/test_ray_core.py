"""Unit tests for Ray Core handle format compatibility."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner


def _make_runner(monkeypatch) -> RayCoreRunner:
    monkeypatch.setattr(RayCoreRunner, "_ensure_ray_initialized", lambda self: None)
    return RayCoreRunner()


def _install_fake_ray(monkeypatch, results: dict[object, str | Exception] | None = None):
    results_map = {} if results is None else results
    cancelled: list[tuple[object, bool]] = []

    def wait(refs, timeout=0, num_returns=None):  # noqa: ANN001
        ready = [ref for ref in refs if ref in results_map]
        not_ready = [ref for ref in refs if ref not in ready]
        return ready, not_ready

    def get(ref):  # noqa: ANN001
        value = results_map[ref]
        if isinstance(value, Exception):
            raise value
        return value

    def cancel(ref, force=False):  # noqa: ANN001
        cancelled.append((ref, force))

    fake_ray = SimpleNamespace(
        wait=wait,
        get=get,
        cancel=cancel,
        exceptions=SimpleNamespace(RayTaskError=RuntimeError),
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    return cancelled


class TestRayCoreHandleFormats:
    """Tests for legacy and composite Ray Core handle IDs."""

    def test_get_status_accepts_legacy_handle_format(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch, results={})
        runner = _make_runner(monkeypatch)

        obj_ref = object()
        runner._pending_tasks[42] = RayCoreHandle(
            task_pk=42,
            object_ref=obj_ref,
            submitted_at=datetime.now(UTC),
            task_name="task",
        )

        info = runner.get_status(
            SubmissionHandle(
                ray_job_id="ray_core:42",
                ray_address="auto",
                submitted_at=datetime.now(UTC),
            )
        )

        assert info.status == JobStatus.RUNNING
        assert info.job_id == "ray_core:42"

    def test_get_status_accepts_composite_handle_format(self, monkeypatch) -> None:
        obj_ref = object()
        _install_fake_ray(monkeypatch, results={obj_ref: '{"success": true, "result": 123}'})
        runner = _make_runner(monkeypatch)

        runner._pending_tasks[7] = RayCoreHandle(
            task_pk=7,
            object_ref=obj_ref,
            submitted_at=datetime.now(UTC),
            task_name="task",
            ray_job_id="02000000",
            ray_task_id="abcdef123456",
        )

        handle_id = "02000000:abcdef123456"
        info = runner.get_status(
            SubmissionHandle(
                ray_job_id=handle_id,
                ray_address="auto",
                submitted_at=datetime.now(UTC),
            )
        )

        assert info.status == JobStatus.SUCCEEDED
        assert info.job_id == handle_id
        assert 7 not in runner._pending_tasks

    def test_cancel_accepts_composite_handle_format(self, monkeypatch) -> None:
        obj_ref = object()
        cancelled = _install_fake_ray(monkeypatch, results={})
        runner = _make_runner(monkeypatch)

        runner._pending_tasks[9] = RayCoreHandle(
            task_pk=9,
            object_ref=obj_ref,
            submitted_at=datetime.now(UTC),
            task_name="task",
            ray_job_id="02000000",
            ray_task_id="feedbeef1234",
        )

        ok = runner.cancel(
            SubmissionHandle(
                ray_job_id="02000000:feedbeef1234",
                ray_address="auto",
                submitted_at=datetime.now(UTC),
            )
        )

        assert ok is True
        assert cancelled == [(obj_ref, False)]
        assert 9 not in runner._pending_tasks

    def test_get_status_rejects_unrecognized_handle(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch, results={})
        runner = _make_runner(monkeypatch)

        info = runner.get_status(
            SubmissionHandle(
                ray_job_id="invalid-format",
                ray_address="auto",
                submitted_at=datetime.now(UTC),
            )
        )

        assert info.status == JobStatus.FAILED
        assert info.message == "Invalid handle format"
