"""The same bounded worker readiness contracts run on SQLite and PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_ray import worker_readiness
from django_ray.models import TaskWorkerLease

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(worker_readiness.timezone, "now", lambda: NOW)
    with override_settings(
        DJANGO_RAY={
            **settings.DJANGO_RAY,
            "WORKER_LEASE_SECONDS": 60,
            "WORKER_HEARTBEAT_SECONDS": 15,
        }
    ):
        yield


def _lease(worker_id: str = "worker-a", **changes: object) -> TaskWorkerLease:
    values = {
        "worker_id": worker_id,
        "hostname": "manager-pod",
        "queue_name": "default",
        "pid": 1,
        "started_at": NOW - timedelta(minutes=2),
        "last_heartbeat_at": NOW,
        **changes,
    }
    return TaskWorkerLease.objects.create(**values)


def _observe(**changes: str) -> worker_readiness.WorkerLeaseReadiness:
    return worker_readiness.check_worker_lease_readiness(
        **{"queue": "default", "hostname": "manager-pod", **changes}
    )


@pytest.mark.parametrize("age", [0, 59, 60])
def test_fresh_lease_includes_exact_policy_boundary(age: int) -> None:
    _lease(last_heartbeat_at=NOW - timedelta(seconds=age))
    assert _observe().as_dict() == {
        "schema_version": 1,
        "scope": "worker_lease",
        "status": "ready",
        "reason": "live_lease",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"last_heartbeat_at": NOW - timedelta(seconds=60, microseconds=1)},
        {"last_heartbeat_at": NOW + timedelta(microseconds=1)},
        {"started_at": NOW + timedelta(microseconds=1)},
        {"is_active": False},
        {"stopped_at": NOW},
        {"hostname": "another-pod"},
        {"queue_name": "another-queue"},
    ],
)
def test_unusable_or_conflicting_lease_is_not_ready(changes: dict[str, object]) -> None:
    _lease(**changes)
    assert _observe().reason == "no_live_lease"
    assert _observe().exit_code == 1


def test_absent_lease_is_not_ready() -> None:
    assert _observe().status == "not_ready"


def test_duplicate_live_leases_need_an_exact_worker_id() -> None:
    _lease()
    _lease("worker-b")
    assert _observe().reason == "ambiguous_lease"
    assert _observe(worker_id="worker-a").status == "ready"
    assert _observe(worker_id="missing-worker").status == "not_ready"
    assert _observe(worker_id="worker-a", hostname="another-pod").status == "not_ready"


def test_inactive_history_does_not_hide_the_current_worker() -> None:
    _lease("old-worker", is_active=False, stopped_at=NOW)
    _lease()
    assert _observe().status == "ready"


def test_observation_uses_one_time_and_one_bounded_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _lease()
    calls = []

    def now() -> datetime:
        calls.append(True)
        return NOW

    monkeypatch.setattr(worker_readiness.timezone, "now", now)
    with CaptureQueriesContext(connection) as queries:
        assert _observe().status == "ready"
    assert calls == [True]
    assert len(queries) == 1
    statement = queries[0]["sql"]
    assert statement.startswith("SELECT 1")
    assert "LIMIT 2" in statement
    assert TaskWorkerLease.objects.get().is_active


@pytest.mark.parametrize(
    "coordinates", [{"hostname": ""}, {"queue": "x" * 101}, {"worker_id": "\n"}]
)
def test_invalid_coordinates_are_fixed_errors(coordinates: dict[str, str]) -> None:
    assert _observe(**coordinates).reason == "invalid_coordinates"
    assert _observe(**coordinates).exit_code == 2


def test_unknown_database_alias_is_not_echoed() -> None:
    report = _observe(using="secret-database-name")
    assert report.reason == "invalid_database"
    assert "secret" not in json.dumps(report.as_dict())


def test_invalid_heartbeat_policy_fails_closed() -> None:
    with override_settings(DJANGO_RAY={**settings.DJANGO_RAY, "WORKER_LEASE_SECONDS": 5}):
        assert _observe().reason == "invalid_settings"


def test_database_error_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("password=private-value")

    monkeypatch.setattr(TaskWorkerLease.objects, "using", unavailable)
    report = _observe()
    assert report.reason == "database_unavailable"
    assert "private" not in json.dumps(report.as_dict())


def test_caller_transaction_cannot_supply_an_old_snapshot() -> None:
    _lease()
    with transaction.atomic():
        assert _observe().reason == "transaction_active"


def test_command_invalid_observation_uses_exit_two() -> None:
    output = StringIO()
    with pytest.raises(CommandError) as caught:
        call_command(
            "django_ray_worker_ready",
            queue="default",
            hostname="manager-pod",
            database="private-database",
            as_json=True,
            stdout=output,
        )
    assert caught.value.returncode == 2
    assert json.loads(output.getvalue())["reason"] == "invalid_database"
    assert "private" not in output.getvalue() + str(caught.value)


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_command_has_stable_exit_and_bounded_output(ready: bool, as_json: bool) -> None:
    if ready:
        _lease()
    output = StringIO()
    options = {"queue": "default", "hostname": "manager-pod", "as_json": as_json, "stdout": output}
    if ready:
        call_command("django_ray_worker_ready", **options)
    else:
        with pytest.raises(CommandError) as caught:
            call_command("django_ray_worker_ready", **options)
        assert caught.value.returncode == 1
    rendered = output.getvalue()
    assert len(rendered.encode()) < 200
    assert "manager-pod" not in rendered
    if as_json:
        assert json.loads(rendered) == _observe().as_dict()
    else:
        assert rendered == f"Worker lease: {_observe().status} ({_observe().reason}).\n"
