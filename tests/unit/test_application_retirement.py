"""Worker cleanup and cold-incarnation evidence; no remote execution."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from django_ray import models
from django_ray.runner.leasing import WorkerLeaseIdentity
from qualification.application import retire_manager as retirement

IDS = ("25200000-0000-4000-8000-000000000002", "25200000-0000-4000-8000-000000000003")


@pytest.fixture
def retired(monkeypatch):
    now = timezone.now()
    identity = WorkerLeaseIdentity(
        "manager-original", "django-manager-original", 42, now - timedelta(seconds=30)
    )
    row = SimpleNamespace(
        revision=2,
        state="RETIRED",
        actor="django-ray-worker",
        reason="owned-cleanup-confirmed",
        cleanup_evidence_digest="sha256:" + "a" * 64,
        cleanup_confirmed_at=now - timedelta(seconds=2),
        created_at=now - timedelta(seconds=1),
    )
    lease = SimpleNamespace(
        **identity.database_filters(), is_active=False, stopped_at=row.created_at
    )
    monkeypatch.setattr(models.TaskWorkerLease.objects, "get", lambda **_k: lease)
    monkeypatch.setattr(
        models.RayWorkerRetirement.objects,
        "filter",
        lambda **_k: SimpleNamespace(order_by=lambda *_a: SimpleNamespace(first=lambda: row)),
    )
    flags = {"cleanup": False, "task": False, "claim": False, "capacity": False}
    from django_ray.target import cohort_job_cleanup

    monkeypatch.setattr(
        cohort_job_cleanup, "owned_job_cleanup_pending", lambda *_a: flags["cleanup"]
    )
    for model, key in (
        (models.RayTaskExecution, "task"),
        (models.RayTaskCohortClaim, "claim"),
        (models.RayWorkerTargetCapability, "capacity"),
    ):
        query = SimpleNamespace(exists=lambda key=key: flags[key])
        query.exclude = lambda query=query, **_k: query
        monkeypatch.setattr(model.objects, "filter", lambda query=query, **_k: query)
    return identity, row, lease, flags


def test_sql_zero_cannot_replace_worker_confirmation(retired):
    identity, row, _lease, _flags = retired
    assert retirement._retired(identity) is row
    row.state = "REQUESTED"
    assert retirement._retired(identity) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor", "application-qualification"),
        ("reason", "counts-only"),
        ("revision", 1),
        ("cleanup_confirmed_at", None),
        ("cleanup_evidence_digest", "invalid"),
    ],
)
def test_cleanup_requires_exact_worker_authored_confirmation(retired, field, value):
    identity, row, _lease, _flags = retired
    setattr(row, field, value)
    with pytest.raises(ValueError):
        retirement._retired(identity)


@pytest.mark.parametrize("key", ["cleanup", "task", "claim", "capacity"])
def test_retirement_retains_all_independent_durable_blockers(retired, key):
    identity, _row, _lease, flags = retired
    flags[key] = True
    with pytest.raises(ValueError, match="retains owned"):
        retirement._retired(identity)


def test_active_lease_and_future_confirmation_are_not_cleanup(retired):
    identity, row, lease, _flags = retired
    lease.is_active = True
    with pytest.raises(ValueError):
        retirement._retired(identity)
    lease.is_active = False
    row.created_at = lease.stopped_at = timezone.now() + timedelta(seconds=30)
    with pytest.raises(ValueError):
        retirement._retired(identity)


def test_retirement_receipt_preserves_exact_original_history(retired, monkeypatch):
    identity, row, _lease, _flags = retired
    receipt = {
        "manager": {**identity.database_filters(), "started_at": identity.started_at.isoformat()},
        "task_ids": IDS,
        "history_digest": "sha256:" + "b" * 64,
        "cluster_session": "original",
        "cleanup_evidence_digest": row.cleanup_evidence_digest,
        "cleanup_confirmed_at": row.cleanup_confirmed_at.isoformat(),
    }
    monkeypatch.setattr(
        retirement, "history_digest", lambda ids: "sha256:" + "b" * 64 if ids == IDS else "changed"
    )
    monkeypatch.setattr(retirement, "core_session", lambda _id: "original")
    retirement.verify_retired_receipt(receipt)
    receipt["history_digest"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="history changed"):
        retirement.verify_retired_receipt(receipt)


@pytest.mark.parametrize("failure", [None, "not-confirmed", "late-confirmed", "changed-history"])
def test_request_once_waits_for_owned_confirmation_without_finalizing(
    tmp_path, retired, monkeypatch, failure
):
    from django_ray import maintenance
    from qualification.application import run_core

    identity, row, lease, _flags = retired
    path = tmp_path / "before-core.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "layer": "application_core",
                "status": "passed",
                "complete_application_gate": False,
                "executions": [{"task_id": i} for i in IDS],
            }
        )
    )
    monkeypatch.setattr(
        run_core, "verify_durable_task", lambda *_a, **_k: {"worker_id": identity.worker_id}
    )
    monkeypatch.setattr(retirement, "history_digest", lambda *_a: "sha256:" + "b" * 64)
    monkeypatch.setattr(retirement, "core_session", lambda *_a: "original")
    calls = []

    def request(got, **kwargs):
        assert got == identity
        assert kwargs == {
            "expected_revision": 0,
            "actor": "application-qualification",
            "reason": "before-cold-session",
            "authorized": True,
        }
        calls.append(got)
        return SimpleNamespace(changed=True, revision=1, state="REQUESTED")

    monkeypatch.setattr(maintenance, "request_worker_retirement", request)
    monkeypatch.setattr(
        maintenance, "complete_worker_retirement", lambda *_a, **_k: pytest.fail("forged cleanup")
    )
    clocks = iter([0.0, 181.0] if failure in {"not-confirmed", "late-confirmed"} else [0.0, 1.0])
    monkeypatch.setattr(retirement.time, "monotonic", lambda: next(clocks))
    if failure == "not-confirmed":
        row.state = "REQUESTED"
    if failure == "changed-history":
        hashes = iter(["sha256:" + "b" * 64, "sha256:" + "c" * 64])
        monkeypatch.setattr(retirement, "history_digest", lambda *_a: next(hashes))
    if failure:
        with pytest.raises(ValueError):
            retirement.retire(path)
    else:
        result = retirement.retire(path)
        assert result["manager"]["started_at"] == lease.started_at.isoformat()
        assert result["complete_application_gate"] is False
    assert calls == [identity]


@pytest.mark.parametrize("failure", [None, "write-collision", "settings", "private-failure"])
def test_retirement_receipt_bytes_are_exact_and_errors_redacted(
    tmp_path, monkeypatch, capsys, failure
):
    import django

    result = {
        "schema_version": 1,
        "layer": "application_retirement",
        "status": "passed",
        "complete_application_gate": False,
    }
    monkeypatch.setenv(
        "DJANGO_SETTINGS_MODULE",
        "other" if failure == "settings" else "testproject.settings_qualification",
    )
    monkeypatch.setattr(django, "setup", lambda: None)

    def retire(*_a):
        if failure == "private-failure":
            raise ValueError("private database detail")
        return result

    monkeypatch.setattr(retirement, "retire", retire)
    path = tmp_path / "receipt.json"
    if failure == "write-collision":
        path.write_bytes(b"retained")
    status = retirement.main(
        ["--before-core", str(tmp_path / "before.json"), "--receipt", str(path)]
    )
    output = capsys.readouterr().out.strip().encode()
    assert status == (1 if failure else 0)
    assert b"private" not in output
    if not failure:
        assert path.read_bytes() == output
    elif failure == "write-collision":
        assert path.read_bytes() == b"retained"
    else:
        assert not path.exists()


@pytest.mark.parametrize(
    "failure",
    [None, "same-worker", "same-host", "old-start", "same-session", "same-task", "history"],
)
def test_replacement_requires_new_incarnation_session_and_preserved_history(
    tmp_path, monkeypatch, failure
):
    now = timezone.now()
    old = {
        "schema_version": 1,
        "layer": "application_retirement",
        "status": "passed",
        "complete_application_gate": False,
        "task_ids": list(IDS),
        "cluster_session": "old",
        "manager": {
            "worker_id": "old",
            "hostname": "django-manager-old",
            "pid": 10,
            "started_at": (now - timedelta(seconds=30)).isoformat(),
        },
        "cleanup_confirmed_at": (now - timedelta(seconds=10)).isoformat(),
    }
    path = tmp_path / "before.json"
    path.write_text(json.dumps(old))
    new = SimpleNamespace(
        worker_id="new",
        hostname="django-manager-new",
        pid=10,
        started_at=now - timedelta(seconds=2),
        is_active=True,
    )
    if failure == "same-worker":
        new.worker_id = "old"
    if failure == "same-host":
        new.hostname = "django-manager-old"
    if failure == "old-start":
        new.started_at = now - timedelta(seconds=11)

    def verify(_receipt):
        if failure == "history":
            raise ValueError("history changed")

    monkeypatch.setattr(retirement, "verify_retired_receipt", verify)
    monkeypatch.setattr(models.TaskWorkerLease.objects, "get", lambda **_k: new)
    monkeypatch.setattr(
        retirement, "core_session", lambda _id: "old" if failure == "same-session" else "new"
    )
    ids = (
        IDS
        if failure == "same-task"
        else ("35200000-0000-4000-8000-000000000002", "35200000-0000-4000-8000-000000000003")
    )
    executions = [{"task_id": value, "worker_id": new.worker_id} for value in ids]
    if failure:
        with pytest.raises(ValueError):
            retirement.verify_replacement(path, executions)
    else:
        result = retirement.verify_replacement(path, executions)
        assert result["previous_retirement_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert result["original_history_preserved"] is True
        assert result["manager"]["pid"] == old["manager"]["pid"]  # PID reuse alone is harmless.
