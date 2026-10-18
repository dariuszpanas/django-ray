"""Resource-free semantic proof for the bounded deployed mismatch observer."""

from contextlib import nullcontext
from datetime import timedelta

import pytest
from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskWorkerLease, TaskWorkerLeasePolicy
from qualification.application import lease_policy as fixture

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    # Local unit proof uses SQLite and no process timer. Actual hosted acceptance
    # must use the source-owned PostgreSQL deadlines and Linux alarm.
    monkeypatch.setattr(fixture, "deadline", nullcontext)
    monkeypatch.setattr(fixture, "database_deadlines", nullcontext)
    TaskWorkerLease.objects.create(
        worker_id="private-existing-worker",
        hostname="manager-private",
        pid=4321,
        last_heartbeat_at=timezone.now(),
        is_active=True,
    )
    row = RayTaskExecution.objects.create(
        task_id="00000000-0000-4000-8000-000000000624",
        callable_path="fixture.task",
        state="SUCCEEDED",
    )
    return str(row.task_id)


def test_real_startup_refusal_preserves_peers_and_cleans_owned_control(prepared):
    before = fixture.snapshot()
    policy = list(TaskWorkerLeasePolicy.objects.values())
    evidence = fixture.empty_evidence()
    fixture.verify_lease_policy(task_id=prepared, manager_prefix="manager-", evidence=evidence)
    fixture.validate_evidence(evidence)
    assert set(evidence) == set(fixture.FACTS)
    assert all(value is True for value in evidence.values())
    assert fixture.preserved(before, fixture.snapshot())
    assert list(TaskWorkerLeasePolicy.objects.values()) == policy
    assert "private" not in str(evidence)


def test_failure_still_removes_control_and_restores_settings(prepared, monkeypatch, settings):
    from django_ray import protocol_status

    def failure(*args, **kwargs):
        raise ValueError("private-field-must-not-be-serialized")

    original = dict(settings.DJANGO_RAY)
    before = fixture.snapshot()
    monkeypatch.setattr(protocol_status, "annotate_execution_protocol_availability", failure)
    evidence = fixture.empty_evidence()
    with pytest.raises(ValueError):
        fixture.verify_lease_policy(task_id=prepared, manager_prefix="manager-", evidence=evidence)
    assert evidence["control_removed"] is True
    assert settings.DJANGO_RAY == original
    assert fixture.preserved(before, fixture.snapshot())
    assert "private-field" not in str(evidence)


@pytest.mark.parametrize("value", [None, {}, dict.fromkeys(fixture.FACTS, 1)])
def test_incomplete_or_nonboolean_evidence_cannot_pass(value):
    with pytest.raises(ValueError, match="incomplete"):
        fixture.validate_evidence(value)


def test_peer_heartbeat_may_advance_but_incarnation_or_state_cannot(prepared):
    before = fixture.snapshot()
    TaskWorkerLease.objects.update(last_heartbeat_at=timezone.now() + timedelta(seconds=1))
    assert fixture.preserved(before, fixture.snapshot())
    TaskWorkerLease.objects.update(is_active=False)
    assert not fixture.preserved(before, fixture.snapshot())


def test_snapshot_refuses_excess_rows_before_retaining_them(prepared):
    for index in range(fixture.MAX_LEASES):
        TaskWorkerLease.objects.create(worker_id=f"bounded-{index}", hostname="fixture", pid=1)
    with pytest.raises(ValueError, match="lease bound"):
        fixture.snapshot()


def test_command_output_is_bounded():
    output = fixture.BoundedOutput()
    output.write("x" * 4096)
    with pytest.raises(ValueError, match="bound"):
        output.write("x")
