"""Trusted CLI pause controls use the same real revisioned SQLite service."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from django_ray.maintenance import (
    MaintenanceScope,
    read_maintenance_policy,
    replace_maintenance_policy,
)
from django_ray.management.commands import django_ray_maintenance as command
from django_ray.models import (
    RayMaintenanceAudit,
    RayMaintenancePolicy,
    RayTarget,
    RayTargetPolicyRevision,
    RayTaskExecution,
    RayTaskQuarantine,
    RayWorkerRetirement,
)
from tests.integration.test_cohort_probe_challenges import _target
from tests.integration.test_maintenance_controls import case_data as case_data
from tests.integration.test_maintenance_controls import isolated_controls as isolated_controls

pytestmark = pytest.mark.django_db(transaction=True)


def _task_selector(task):
    return (
        "--task-pk",
        str(task.pk),
        "--task-id",
        task.task_id,
        "--attempt",
        str(task.attempt_number),
        "--generation",
        str(task.execution_generation),
    )


def _worker_selector(owner):
    return (
        "--worker-id",
        owner.worker_id,
        "--worker-hostname",
        owner.hostname,
        "--worker-pid",
        str(owner.pid),
        "--worker-started-at",
        owner.started_at.isoformat(),
    )


def test_exact_control_status_is_read_only_and_keeps_zero_revision(case_data):
    for selector in (_task_selector(case_data.task), _worker_selector(case_data.owner)):
        with CaptureQueriesContext(connection) as queries:
            result = json.loads(_call(*selector, "--json"))
        assert result["state"] == "UNCONTROLLED" and result["revision"] == 0
        assert result["current_identity_present"] and not result["drain_verified"]
        assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
        assert "Drain verification: not performed." in _call(*selector)


def test_quarantine_dry_run_apply_release_and_current_generation_revision(case_data):
    selector = _task_selector(case_data.task)
    preview = json.loads(
        _call(*_controls(*selector, "--quarantine-task", "--json", mode="--dry-run", revision=0))
    )
    assert preview["revision"] == 1 and preview["mode"] == "dry-run"
    assert not RayTaskQuarantine.objects.exists()
    applied = json.loads(_call(*_controls(*selector, "--quarantine-task", "--json", revision=0)))
    assert applied["state"] == "QUARANTINED" and applied["changed"]
    with pytest.raises(CommandError, match="revision_changed"):
        _call(*_controls(*selector, "--release-task", revision=0))
    _call(*_controls(*selector, "--release-task", revision=1))
    RayTaskExecution.objects.filter(pk=case_data.task.pk).update(execution_generation=1)
    case_data.task.refresh_from_db()
    selector = _task_selector(case_data.task)
    status = json.loads(_call(*selector, "--json"))
    assert status["revision"] == 2 and status["control_identity"]["execution_generation"] == 0
    _call(*_controls(*selector, "--quarantine-task", revision=2))
    assert RayTaskQuarantine.objects.latest("revision").execution_generation == 1


def test_worker_request_cli_cannot_manufacture_final_retirement_or_drain(case_data):
    selector = _worker_selector(case_data.owner)
    preview = json.loads(
        _call(*_controls(*selector, "--retire-worker", "--json", mode="--dry-run", revision=0))
    )
    assert preview["state"] == "REQUESTED" and not RayWorkerRetirement.objects.exists()
    actual = json.loads(_call(*_controls(*selector, "--retire-worker", "--json", revision=0)))
    assert actual["state"] == "REQUESTED" and not actual["drain_verified"]
    case_data.lease.refresh_from_db()
    assert case_data.lease.is_active
    case_data.lease.delete()
    status = json.loads(_call(*selector, "--json"))
    assert status["state"] == "REQUESTED" and not status["current_identity_present"]
    with pytest.raises(CommandError, match="identity_changed"):
        _call(*_controls(*selector, "--retire-worker", revision=1))


@pytest.mark.parametrize(
    "extra",
    [
        ("--queue", "work"),
        ("--all",),
        ("--pause-claims",),
        ("--retire-worker",),
    ],
)
def test_task_control_refuses_mixed_scope_or_wrong_action(case_data, extra):
    with pytest.raises(CommandError):
        _call(*_controls(*_task_selector(case_data.task), *extra, revision=0))
    assert not RayTaskQuarantine.objects.exists()


@pytest.mark.parametrize(
    "options",
    [
        {"worker_id": "partial"},
        {"task_pk": 1},
        {"quarantine_task": True},
        {"retire_worker": True},
        {"task_pk": True, "task_id": "bad", "attempt": 1, "generation": 0},
    ],
)
def test_entity_incomplete_or_invalid_identity_is_refused(options):
    with pytest.raises(CommandError):
        _call(**options)


@pytest.mark.parametrize(
    "change",
    [
        {"authorized": False},
        {"expected_revision": None},
        {"expected_revision": True},
        {"actor": None},
        {"reason": ""},
        {"quarantine_task": 1},
    ],
)
def test_entity_apply_requires_explicit_authority_and_valid_cas(case_data, change):
    with pytest.raises(CommandError):
        _call(
            *_task_selector(case_data.task),
            **(
                {
                    "apply": True,
                    "quarantine_task": True,
                    "authorized": True,
                    "expected_revision": 0,
                    "actor": "operator",
                    "reason": "maintenance",
                }
                | change
            ),
        )
    assert not RayTaskQuarantine.objects.exists()


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-09-12T00:00:00", "x" * 65])
def test_worker_status_requires_aware_bounded_start_time(case_data, timestamp):
    with pytest.raises(CommandError):
        _call(*_worker_selector(case_data.owner), worker_started_at=timestamp)


def test_entity_provider_error_redaction_and_bounded_output(case_data, monkeypatch):
    selector = _task_selector(case_data.task)
    with monkeypatch.context() as patch:
        patch.setattr(command, "_MAX_OUTPUT_BYTES", 1)
        with pytest.raises(CommandError, match="output bound"):
            _call(*selector, "--json")

    def failed(*_args, **_kwargs):
        raise RuntimeError("sensitive provider credentials")

    monkeypatch.setattr(command, "read_maintenance_policy", failed)
    with pytest.raises(CommandError, match="Maintenance operation failed") as error:
        _call(*selector)
    assert "sensitive" not in str(error.value)


def _call(*arguments, **options):
    output = StringIO()
    call_command("django_ray_maintenance", *arguments, stdout=output, **options)
    return output.getvalue()


def _controls(*arguments, mode="--apply", revision=None):
    current = read_maintenance_policy().revision if revision is None else revision
    return (
        mode,
        "--expected-revision",
        str(current),
        "--actor",
        "operator",
        "--reason",
        "planned-maintenance",
        "--authorized",
        *arguments,
    )


def _set(scopes=(), **flags):
    values = {"pause_enqueues": False, "pause_claims": False}
    values.update(flags)
    return replace_maintenance_policy(
        scopes,
        expected_revision=read_maintenance_policy().revision,
        actor="fixture",
        reason="fixture",
        authorized=True,
        **values,
    )


def test_default_status_is_read_only_bounded_and_explicit_about_drain():
    with CaptureQueriesContext(connection) as queries:
        status = json.loads(_call("--json"))
    assert status == {
        "schema_version": 1,
        "mode": "status",
        "changed": False,
        "previous_revision": 1,
        "revision": 1,
        "pause_enqueues": False,
        "pause_claims": False,
        "scopes": [],
        "drain_verified": False,
        "updated_at": read_maintenance_policy().updated_at.isoformat(),
    }
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    assert "Drain verification: not performed." in _call()
    assert RayMaintenanceAudit.objects.count() == 1


def test_dry_run_proposes_without_publishing_then_exact_apply_audits():
    before = read_maintenance_policy()
    preview = json.loads(_call(*_controls("--all", "--pause-enqueues", "--json", mode="--dry-run")))
    assert preview["mode"] == "dry-run" and preview["changed"] and preview["revision"] == 2
    assert read_maintenance_policy() == before
    assert RayMaintenanceAudit.objects.count() == 1
    actual = json.loads(
        _call(*_controls("--all", "--pause-enqueues", "--json", revision=before.revision))
    )
    assert actual["mode"] == "applied" and actual["pause_enqueues"] and not actual["drain_verified"]
    audit = RayMaintenanceAudit.objects.get(pk=2)
    assert (audit.actor, audit.reason) == ("operator", "planned-maintenance")


def test_apply_requires_fresh_cas_and_never_retries_automatically(monkeypatch):
    _set(pause_claims=True)
    with pytest.raises(CommandError, match="revision_changed"):
        _call(*_controls("--all", "--resume-claims", revision=1))
    original = command.replace_maintenance_policy
    calls = []

    def race(*args, **kwargs):
        calls.append(True)
        _set(pause_claims=True, pause_enqueues=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(command, "replace_maintenance_policy", race)
    with pytest.raises(CommandError, match="revision_changed"):
        _call(*_controls("--all", "--resume-claims"))
    assert calls == [True]
    assert read_maintenance_policy().pause_claims


def test_scope_changes_preserve_unmentioned_flags_and_scopes():
    _set((MaintenanceScope("queue", queue_name="other"),), pause_enqueues=True)
    _call(*_controls("--queue", " work 队列 ", "--protocol", "3", "--pause-claims"))
    policy = read_maintenance_policy()
    assert policy.pause_enqueues and not policy.pause_claims
    assert {scope.queue_name for scope in policy.scopes if scope.kind == "queue"} == {
        "other",
        " work 队列 ",
    }
    assert any(scope.kind == "protocol" and scope.protocol_version == 3 for scope in policy.scopes)
    _call(*_controls("--queue", " work 队列 ", "--pause-enqueues"))
    _call(*_controls("--queue", " work 队列 ", "--resume-claims"))
    scope = next(
        scope for scope in read_maintenance_policy().scopes if scope.queue_name == " work 队列 "
    )
    assert scope.pause_enqueues and not scope.pause_claims
    _call(*_controls("--queue", " work 队列 ", "--resume-enqueues"))
    assert all(scope.queue_name != " work 队列 " for scope in read_maintenance_policy().scopes)
    _call(*_controls("--all", "--resume-enqueues"))
    assert len(read_maintenance_policy().scopes) == 2


def test_idempotent_resume_does_not_create_audit_revision():
    response = json.loads(_call(*_controls("--queue", "already-open", "--resume-claims", "--json")))
    assert response["changed"] is False and response["revision"] == 1
    assert RayMaintenanceAudit.objects.count() == 1


def test_exact_target_pause_never_activates_drained_target():
    expectation = _target("drained")
    target_before = RayTarget.objects.values().get(pk=expectation.target_key)
    policies_before = list(RayTargetPolicyRevision.objects.values())
    assert policies_before[0]["desired_state"] == "draining"
    _call(*_controls("--target", expectation.target_key, "--pause-claims"))
    scope = read_maintenance_policy().scopes[0]
    assert scope.target_id == expectation.target_key and not scope.pause_enqueues
    _call(*_controls("--target", expectation.target_key, "--resume-claims"))
    assert read_maintenance_policy().scopes == ()
    assert RayTarget.objects.values().get(pk=expectation.target_key) == target_before
    assert list(RayTargetPolicyRevision.objects.values()) == policies_before


@pytest.mark.parametrize(
    "arguments",
    [
        ("--apply",),
        ("--dry-run",),
        ("--queue", "default"),
        ("--pause-claims",),
        ("--expected-revision", "1"),
        ("--actor", "operator"),
        ("--authorized",),
    ],
)
def test_partial_controls_never_mutate(arguments):
    with pytest.raises(CommandError):
        _call(*arguments)
    assert read_maintenance_policy().revision == 1


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("--pause-claims",),
        ("--all",),
        ("--all", "--queue", "default", "--pause-claims"),
        ("--target", "x", "--pause-enqueues"),
        ("--target", "x", "--resume-enqueues"),
        ("--queue", "x", "--queue", "x", "--pause-claims"),
        ("--queue", "", "--resume-claims"),
        ("--queue", "x\x00y", "--resume-claims"),
        ("--queue", "x" * 101, "--pause-claims"),
        ("--protocol", "0", "--pause-claims"),
        ("--protocol", "32768", "--pause-claims"),
        ("--target", "credential\nvalue", "--pause-claims"),
    ],
)
def test_invalid_change_selections_fail_closed(arguments):
    with pytest.raises(CommandError):
        _call(*_controls(*arguments))
    assert RayMaintenanceAudit.objects.count() == 1


@pytest.mark.parametrize("omit", ["--expected-revision", "--actor", "--reason", "--authorized"])
def test_each_mutation_acknowledgment_is_required(omit):
    arguments = list(_controls("--all", "--pause-claims"))
    index = arguments.index(omit)
    del arguments[index : index + (1 if omit == "--authorized" else 2)]
    with pytest.raises(CommandError):
        _call(*arguments)
    assert RayMaintenanceAudit.objects.count() == 1


@pytest.mark.parametrize(
    "options",
    [
        {"apply": 1},
        {"authorized": 1},
        {"as_json": "true"},
        {"enqueue_change": 1},
        {"queue": ("x",)},
        {"queue": [True]},
        {"protocol": [True]},
        {"apply": True, "dry_run": True},
    ],
)
def test_python_command_call_cannot_coerce_control_types(options):
    with pytest.raises(CommandError):
        _call(**options)
    assert RayMaintenanceAudit.objects.count() == 1


def test_scope_bound_is_checked_before_building_unbounded_status():
    with pytest.raises(CommandError):
        _call(*_controls("--pause-claims"), queue=[str(i) for i in range(65)])
    _set(tuple(MaintenanceScope("queue", queue_name=str(i)) for i in range(64)))
    with pytest.raises(CommandError, match="invalid"):
        _call(*_controls("--queue", "sixty-fifth", "--pause-claims"))
    assert len(read_maintenance_policy().scopes) == 64


def test_queue_control_characters_are_escaped_in_status_output():
    _call(*_controls("--queue", "control\x1b[31m\nqueue", "--pause-claims"))
    rendered = _call()
    assert "\x1b" not in rendered
    assert "\\u001b" in rendered and "\\nqueue" in rendered


def test_provider_exception_is_redacted_and_missing_policy_is_explicit(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("password=do-not-echo")

    with monkeypatch.context() as patch:
        patch.setattr(command, "read_maintenance_policy", fail)
        with pytest.raises(CommandError) as caught:
            _call()
        assert str(caught.value) == "Maintenance operation failed."
        assert caught.value.__suppress_context__
    RayMaintenancePolicy.objects.all().delete()
    with pytest.raises(CommandError, match="unavailable"):
        _call()


def test_cli_mutation_does_not_join_a_caller_transaction():
    arguments = _controls("--all", "--pause-claims")
    with transaction.atomic(), pytest.raises(CommandError, match="transaction_open"):
        _call(*arguments)
