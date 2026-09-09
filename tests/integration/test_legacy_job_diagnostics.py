"""The same legacy diagnostic persistence/fence cases run on SQLite and PostgreSQL."""

from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests
from django.db import close_old_connections, connections

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState
from django_ray.redaction import REDACTED
from django_ray.runner.base import JobInfo, JobStatus
from django_ray.runner.job_diagnostics import (
    MAX_JOB_DIAGNOSTIC_BYTES,
    OVERSIZED_JOB_DIAGNOSTIC,
    UNAVAILABLE_JOB_DIAGNOSTIC,
)
from django_ray.runner.ray_job import RayJobRunner

pytestmark = pytest.mark.django_db(transaction=True)


class LogResponse:
    status_code = 200
    headers = {}

    def __init__(self, body, before_read=lambda: None):
        self.body = io.BytesIO(body)
        self.before_read = before_read
        self.raw = self
        self.closed = False

    def read(self, amount, *, decode_content):
        assert amount == MAX_JOB_DIAGNOSTIC_BYTES + 1
        assert decode_content is False
        self.before_read()
        return self.body.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True


@pytest.fixture
def legacy_job(monkeypatch):
    command = Command()
    command.stdout = io.StringIO()
    command.worker_id = "legacy-diagnostic-worker"
    command.sync_mode = False
    command._create_lease("default")
    task = RayTaskExecution.objects.create(
        task_id="legacy-diagnostic-task",
        callable_path="testproject.tasks.add_numbers",
        queue_name="default",
        state=TaskState.RUNNING,
        args_json="[1, 2]",
        kwargs_json="{}",
        attempt_number=1,
        ray_job_id="raysubmit_legacy_diagnostic",
        ray_address="ray://selected:10001",
        claimed_by_worker=command.worker_id,
    )
    command.active_tasks = {task.pk: task.ray_job_id}
    client = SimpleNamespace(
        _address="https://selected.example", _headers={}, _cookies=None, _verify=True
    )
    monkeypatch.setattr(RayJobRunner, "_get_client", lambda *_args: client)
    monkeypatch.setattr(
        RayJobRunner,
        "get_status",
        lambda _self, handle: JobInfo(
            job_id=handle.ray_job_id,
            status=JobStatus.FAILED,
            message="password=untrusted-status-detail",
        ),
    )
    return command, task


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("ordinary", "line one\nline two"),
        ("multibyte", "échec\n二行"),
        ("secret", REDACTED),
        ("oversized", OVERSIZED_JOB_DIAGNOSTIC),
        ("malformed", UNAVAILABLE_JOB_DIAGNOSTIC),
        ("unavailable", UNAVAILABLE_JOB_DIAGNOSTIC),
        ("timeout", UNAVAILABLE_JOB_DIAGNOSTIC),
    ],
)
def test_failure_fields_are_bounded_before_both_durable_writes(
    monkeypatch, legacy_job, caplog, case, expected
):
    command, task = legacy_job
    text = {
        "ordinary": "\x1b[31mline one\x1b[0m\r\nline two",
        "multibyte": "échec\n二行",
        "secret": "password=untrusted-log-detail",
    }.get(case, "")
    body = json.dumps({"logs": text}, ensure_ascii=False).encode()
    if case == "oversized":
        body = b"x" * (MAX_JOB_DIAGNOSTIC_BYTES + 100)
    elif case == "malformed":
        body = b'{"logs":["invalid"]}'
    response = LogResponse(body)
    if case == "unavailable":
        response.status_code = 404

    def get(*_args, **kwargs):
        assert kwargs["timeout"] == 5.0 and kwargs["stream"]
        if case == "timeout":
            raise requests.Timeout("password=transport-exception")
        return response

    monkeypatch.setattr(requests, "get", get)
    command.reconcile_tasks()

    task.refresh_from_db()
    attempt = task.attempts.get(attempt_number=1)
    for stored in (task, attempt):
        assert stored.state == TaskState.FAILED
        assert stored.error_message == "Legacy Ray Job failed without an exact completion envelope"
        assert stored.error_traceback == expected
        assert len(stored.error_traceback.encode()) <= MAX_JOB_DIAGNOSTIC_BYTES
    assert task.attempt_number == 1
    assert task.pk not in command.active_tasks
    assert "untrusted-status-detail" not in command.stdout.getvalue() + caplog.text
    assert "untrusted-log-detail" not in command.stdout.getvalue() + caplog.text
    assert "transport-exception" not in command.stdout.getvalue() + caplog.text
    assert response.closed is (case != "timeout")


@pytest.mark.parametrize("replacement", ["job", "owner", "attempt", "generation", "completion"])
def test_diagnostic_arriving_after_replacement_cannot_write_or_retry(
    monkeypatch, legacy_job, replacement
):
    command, task = legacy_job
    changes = {
        "job": {"ray_job_id": "raysubmit_replacement"},
        "owner": {"claimed_by_worker": "replacement-worker"},
        "attempt": {"attempt_number": task.attempt_number + 1},
        "generation": {"execution_generation": task.execution_generation + 1},
        "completion": {"completion_data": '{"success":true,"result":3}'},
    }[replacement]

    def replace_on_independent_connection():
        close_old_connections()
        try:
            assert RayTaskExecution.objects.filter(pk=task.pk).update(**changes) == 1
        finally:
            connections.close_all()

    def before_read():
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(replace_on_independent_connection).result(timeout=5)

    response = LogResponse(b'{"logs":"obsolete failure detail"}', before_read)
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: response)
    command.reconcile_tasks()

    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.error_message is None and task.error_traceback is None
    assert not task.attempts.exists()
    for field, expected in changes.items():
        assert getattr(task, field) == expected
    assert response.closed
