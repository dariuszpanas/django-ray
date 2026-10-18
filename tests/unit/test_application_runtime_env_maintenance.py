"""Bounded command orchestration and real resource-free qualification seams."""

import base64
import copy
import json
from io import StringIO

import pytest

from qualification.application import runtime_env_maintenance as maintenance
from qualification.application import runtime_env_refusal as refusal


def complete():
    return {case: dict.fromkeys(facts, True) for case, facts in maintenance.FACTS.items()}


@pytest.mark.parametrize("change", ["missing", "false", "extra", "integer", "field"])
def test_evidence_requires_every_exact_true_boolean(change):
    value = complete()
    if change == "missing":
        del value["scan"]
    elif change == "extra":
        value["private"] = "must not be accepted"
    elif change == "field":
        value["scan"]["private"] = True
    else:
        value["scan"]["rewrite_range_complete"] = 1 if change == "integer" else False
    with pytest.raises(ValueError):
        maintenance.validate_evidence(value)
    maintenance.validate_evidence(complete())


def test_failed_diagnostics_project_only_fixed_boolean_facts():
    from qualification.application.safe_diagnostics import lines, passed_receipt

    evidence = complete()
    evidence["scan"]["private"] = "must-not-survive"
    evidence["scan"]["rewrite_range_complete"] = 1
    receipt = {
        "layer": "application_core",
        "status": "failed",
        "failed_stage": "runtime_env_maintenance",
        "runtime_env_maintenance": evidence,
    }
    projected = lines(json.dumps(receipt).encode())[0]
    assert projected["failed_stage"] == "runtime_env_maintenance"
    assert projected["runtime_env_maintenance"]["scan"]["rewrite_range_complete"] is False
    assert "must-not-survive" not in json.dumps(projected)
    passed_receipt({**receipt, "runtime_env_maintenance": complete()})


def page(details, *, more=False, high_water=3, after=None):
    counts = dict.fromkeys(
        ("current_key", "rewritten", "rewrapped", "invalid_snapshot", "unavailable_key"), 0
    )
    for item in details:
        counts[item["outcome"]] += 1
    return {
        "schema": "django-ray.runtime-env-maintenance",
        "schema_version": 1,
        "mode": "rewrite",
        "counts": counts,
        "details": details,
        "processed": len(details),
        "processed_bytes": 100,
        "details_omitted": 0,
        "exit_code": 1
        if counts["invalid_snapshot"] or counts["unavailable_key"]
        else 2
        if more
        else 0,
        "cleanup_confirmed": True,
        "stop_code": None,
        "budget_exhausted": None,
        "current_table_only": True,
        "fresh_pass_required": True,
        "backup_obligations": "not_verified",
        "key_retirement_authorized": False,
        "high_water": high_water,
        "after": after if after is not None else high_water,
        "has_more": more,
        "range_complete": not more,
        "next_cursor": "bounded-cursor" if more else None,
    }


@pytest.fixture
def fake_command(monkeypatch, settings):
    from django.core.management.base import CommandError

    reports = [
        page([{"pk": 1, "outcome": "invalid_snapshot"}], more=True, after=1),
        page([{"pk": 2, "outcome": "rewritten"}, {"pk": 3, "outcome": "rewrapped"}]),
    ]
    calls = []
    original = copy.deepcopy(settings.DJANGO_RAY)

    def command(name, **kwargs):
        calls.append(kwargs)
        assert name == "django_ray_runtime_env"
        assert kwargs["max_rows"] == kwargs["max_details"] == 100
        assert kwargs["max_bytes"] == 65536
        assert kwargs["max_total_bytes"] == 1048576
        assert 0 < kwargs["max_seconds"] <= 5
        assert kwargs["confirm"] is True
        assert kwargs["database"] == "default"
        assert kwargs["format"] == "json"
        assert settings.DJANGO_RAY == {"temporary": "old-key-ring"}
        report = reports.pop(0)
        kwargs["stdout"].write(json.dumps(report))
        if report["exit_code"]:
            raise CommandError("fixed command failure", returncode=report["exit_code"])

    monkeypatch.setattr("django.core.management.call_command", command)
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: 0)
    return reports, calls, original


def run_scan():
    maintenance.scan(
        mode="rewrite",
        config={"temporary": "old-key-ring"},
        expected={1: "invalid_snapshot", 2: "rewritten", 3: "rewrapped"},
        deadline=60,
        private=["sensitive-canary"],
    )


def test_actual_command_interface_keeps_earlier_blockers_and_restores_settings(
    fake_command, settings
):
    _, calls, original = fake_command
    run_scan()
    assert len(calls) == 2
    assert calls[0]["cursor"] is None
    assert calls[1]["cursor"] == "bounded-cursor"
    assert settings.DJANGO_RAY == original


@pytest.mark.parametrize(
    "change",
    [
        "unexpected",
        "lost-counter",
        "unknown-counter",
        "cleanup",
        "time",
        "stop",
        "missing-row",
        "changed-range",
        "duplicate",
        "no-cursor",
        "false-success",
        "private",
        "large-output",
    ],
)
def test_scan_fails_closed_and_always_restores_old_key_override(fake_command, settings, change):
    reports, _, original = fake_command
    if change == "unexpected":
        reports[0]["details"][0]["outcome"] = "current_key"
    elif change == "lost-counter":
        reports[0]["counts"]["invalid_snapshot"] = 0
    elif change == "unknown-counter":
        reports[0]["counts"]["database_unavailable"] = 1
    elif change == "cleanup":
        reports[0]["cleanup_confirmed"] = False
    elif change == "time":
        reports[0]["budget_exhausted"] = "elapsed"
    elif change == "stop":
        reports[0]["stop_code"] = "commit_uncertain"
    elif change == "missing-row":
        reports[1] = page([{"pk": 3, "outcome": "rewrapped"}])
    elif change == "changed-range":
        reports[1]["high_water"] = 4
    elif change == "duplicate":
        reports[1] = page([{"pk": 1, "outcome": "invalid_snapshot"}])
    elif change == "no-cursor":
        reports[0]["next_cursor"] = None
    elif change == "false-success":
        reports[0]["exit_code"] = 0
    elif change == "private":
        reports[0]["private"] = "sensitive-canary"
    else:
        reports[0]["private"] = "x" * 32769
    with pytest.raises(ValueError):
        run_scan()
    assert settings.DJANGO_RAY == original


def test_scan_never_starts_after_shared_deadline(fake_command):
    with pytest.raises(ValueError, match="deadline"):
        maintenance.scan(mode="rewrite", config={}, expected={}, deadline=0, private=["marker"])
    assert not fake_command[1]


def test_bounded_output_counts_utf8_across_writes():
    output = maintenance.BoundedOutput()
    output.write("é" * 8192)
    output.write("é" * 8192)
    with pytest.raises(ValueError, match="output exceeded"):
        output.write("x")


def test_unknown_legacy_row_stops_before_any_fixture_or_rewrite(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(maintenance, "fixture_configuration", dict)
    monkeypatch.setattr(
        maintenance,
        "inventory",
        lambda: {1: maintenance.Snapshot(1, "task", "thin", "digest", "{}", "hash")},
    )
    monkeypatch.setattr(maintenance, "expected_blockers", lambda *a, **k: {})
    monkeypatch.setattr(maintenance, "create_probes", lambda *a: pytest.fail("unexpected write"))
    with pytest.raises(ValueError, match="unexpected preexisting"):
        maintenance.verify_maintenance(
            None,
            token="unused",
            manager_prefix="django-manager-",
            refusal_ids=[2, 3],
            evidence=maintenance.empty_evidence(),
        )


def test_second_generation_uses_only_prior_validated_refusal_identities(tmp_path):
    from qualification.application.run_core import maintenance_refusal_ids

    value = {
        "layer": "application_core",
        "status": "passed",
        "schema_version": 1,
        "complete_application_gate": False,
        "failed_stage": None,
        "protected_runtime": {
            "api_clear": True,
            "row_ids": [1, 2, 3],
            "started_at": "2026-10-18T00:00:00Z",
        },
        "runtime_env_maintenance": complete(),
    }
    before, after = tmp_path / "before-core.json", tmp_path / "after-core.json"
    before.write_text(json.dumps(value))
    assert maintenance_refusal_ids([5, 6], after) == [2, 3, 5, 6]
    assert maintenance_refusal_ids([5, 6], before) == [5, 6]
    value["runtime_env_maintenance"]["scan"]["expected_blockers_visible"] = False
    before.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        maintenance_refusal_ids([5, 6], after)


@pytest.fixture
def fixture_settings(settings, monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    settings.DEBUG = False
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "ray://ray-head:10001",
        "RUNTIME_ENV_PROFILES": {"thin": {"env_vars": {"marker": "private-canary"}}},
        "RUNTIME_ENV_STORAGE_MODE": "required",
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "qualification",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {
            "qualification": base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
        },
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": False,
    }


@pytest.mark.django_db
def test_database_held_probes_and_full_row_comparison(fixture_settings):
    from django_ray.models import RayTaskExecution
    from django_ray.runtime.runtime_env import RuntimeEnvSnapshotError, runtime_env_for_execution
    from django_ray.runtime.runtime_env_encryption import (
        unprotect_runtime_env_snapshot,
        validate_runtime_env_encryption_settings,
    )

    config = maintenance.fixture_configuration()
    fixtures = maintenance.create_probes(config)
    first = maintenance.inventory()
    assert len(first) == 2
    assert len({RayTaskExecution.objects.get(pk=item.pk).queue_name for item in fixtures}) == 1
    for item in fixtures:
        row = RayTaskExecution.objects.get(pk=item.pk)
        assert row.queue_name.startswith("maintenance-")
        assert row.ray_target_address == config["RAY_ADDRESS"]
        assert row.state == "QUEUED" and row.execution_generation == 0 and row.attempt_number == 1
        assert not row.attempts.exists()
        if item.case == "old_key_rewrap":
            plaintext = unprotect_runtime_env_snapshot(
                item.serialized,
                task_id=item.task_id,
                profile="thin",
                digest=item.digest,
                encryption=validate_runtime_env_encryption_settings(config),
            )
            assert plaintext == fixtures[0].serialized
        with pytest.raises(RuntimeEnvSnapshotError):
            runtime_env_for_execution(row)
    assert maintenance.inventory() == first
    RayTaskExecution.objects.filter(pk=fixtures[0].pk).update(kwargs_json='{"changed":true}')
    assert (
        maintenance.inventory()[fixtures[0].pk].other_fields != first[fixtures[0].pk].other_fields
    )
    assert (
        maintenance.OLD_KEY_ID
        not in __import__("django.conf", fromlist=["settings"]).settings.DJANGO_RAY[
            "RUNTIME_ENV_ENCRYPTION_KEYS"
        ]
    )


@pytest.mark.django_db
@pytest.mark.parametrize("bound", ["rows", "row-bytes", "total-bytes"])
def test_database_inventory_rejects_bounds_before_snapshot_hydration(
    fixture_settings, monkeypatch, bound
):
    from django_ray.models import RayTaskExecution

    RayTaskExecution.objects.create(runtime_env_json="sensitive-body", task_id="owned")
    monkeypatch.setattr(
        maintenance,
        {"rows": "MAX_ROWS", "row-bytes": "MAX_ROW_BYTES", "total-bytes": "MAX_TOTAL_BYTES"}[bound],
        0,
    )
    with pytest.raises(ValueError, match="inventory exceeded"):
        maintenance.inventory()


@pytest.mark.django_db
def test_database_required_plaintext_manager_and_real_retry_preserve_history(
    fixture_settings, monkeypatch, client, settings
):
    from django_ray.management.commands.django_ray_worker import Command
    from django_ray.models import RayTaskExecution
    from django_ray.runtime.runtime_env import resolve_runtime_env_profile

    monkeypatch.setattr("socket.gethostname", lambda: "django-manager-fixture")
    command = Command()
    command.stdout = StringIO()
    command.execution_mode = "cluster"
    command.worker_id = "maintenance-fixture-manager"
    command.active_tasks = {}
    command._create_lease("default")
    for method in ("execute_task_sync", "submit_task_to_ray_core", "submit_task_to_ray"):
        monkeypatch.setattr(command, method, lambda *a, **k: pytest.fail("plaintext reached Ray"))
    runtime = resolve_runtime_env_profile("thin")
    row = RayTaskExecution.objects.create(
        task_id="5cbf5741-81c4-4eea-9cfe-d96de901cfbf",
        callable_path=refusal.CALLABLE,
        runtime_env_profile="thin",
        runtime_env_json=runtime.serialized,
        runtime_env_hash=runtime.digest,
    )
    fixture = refusal.Fixture(
        "required_plaintext", row.pk, row.task_id, runtime.serialized, runtime.digest
    )
    assert command.claim_and_process_tasks(queues=["default"], concurrency=1) == 1
    before = refusal.inspect_failure(fixture, manager_prefix="django-manager-")
    assert before
    response = client.post(
        f"/api/executions/{row.pk}/retry", HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}"
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Persisted RuntimeEnv snapshot failed validation"}
    assert refusal.inspect_failure(fixture, manager_prefix="django-manager-") == before


def test_configuration_refuses_debug_before_deriving_temporary_key(fixture_settings, settings):
    settings.DEBUG = True
    with pytest.raises(ValueError, match="configuration"):
        maintenance.fixture_configuration()


@pytest.mark.django_db
@pytest.mark.parametrize("mutation", [None, "state", "generation", "identity", "delete-failure"])
def test_database_cleanup_only_removes_three_proven_terminal_rows_atomically(monkeypatch, mutation):
    from django.db.models import QuerySet

    from django_ray.models import RayTaskExecution

    fixtures = []
    for index, state in enumerate(("SUCCEEDED", "SUCCEEDED", "FAILED")):
        row = RayTaskExecution.objects.create(
            task_id=f"maintenance-owned-{index}",
            state=state,
            attempt_number=1,
            execution_generation=1,
            callable_path=refusal.CALLABLE,
            runtime_env_profile="thin",
            runtime_env_hash="a" * 64,
        )
        fixtures.append(refusal.Fixture("owned", row.pk, row.task_id, "{}", "a" * 64))
    other = RayTaskExecution.objects.create(task_id="unrelated", state="SUCCEEDED")
    last = fixtures[-1]
    if mutation in {"state", "generation", "identity"}:
        changes = {
            "state": {"state": "RUNNING"},
            "generation": {"execution_generation": 2},
            "identity": {"task_id": "changed"},
        }[mutation]
        RayTaskExecution.objects.filter(pk=last.pk).update(**changes)
    elif mutation == "delete-failure":
        original = QuerySet.delete
        count = []

        def delete(query):
            count.append(None)
            if len(count) == 3:
                raise ValueError("fixed cleanup failure")
            return original(query)

        monkeypatch.setattr(QuerySet, "delete", delete)
    if mutation:
        with pytest.raises(ValueError):
            maintenance.remove_terminal_fixtures(fixtures[:2], last)
        assert RayTaskExecution.objects.count() == 4
    else:
        maintenance.remove_terminal_fixtures(fixtures[:2], last)
        assert list(RayTaskExecution.objects.values_list("pk", flat=True)) == [other.pk]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mutation", [None, "historical-row", "historical-version", "current-owner"]
)
def test_database_only_exact_prior_generation_blockers_allow_inactive_owner(
    fixture_settings, monkeypatch, mutation
):
    from django_ray.management.commands import django_ray_worker
    from django_ray.management.commands.django_ray_worker import Command
    from django_ray.models import RayTaskExecution, TaskWorkerLease

    monkeypatch.setattr("socket.gethostname", lambda: "django-manager-fixture")
    fixtures = []
    current_version = django_ray_worker.django_ray_version
    for generation in ("before", "after"):
        monkeypatch.setattr(
            django_ray_worker,
            "django_ray_version",
            "0.0.0"
            if mutation == "historical-version" and generation == "before"
            else current_version,
        )
        command = Command()
        command.stdout = StringIO()
        command.execution_mode = "cluster"
        command.worker_id = f"maintenance-{generation}-manager"
        command.active_tasks = {}
        command._create_lease("default")
        for method in ("execute_task_sync", "submit_task_to_ray_core", "submit_task_to_ray"):
            monkeypatch.setattr(
                command, method, lambda *a, **k: pytest.fail("malformed snapshot reached Ray")
            )
        fixtures.extend(refusal.create_fixtures())
        assert command.claim_and_process_tasks(queues=["default"], concurrency=2) == 2
        if generation == "before":
            TaskWorkerLease.objects.filter(worker_id=command.worker_id).update(is_active=False)
    if mutation == "historical-row":
        RayTaskExecution.objects.filter(pk=fixtures[0].pk).update(
            ray_job_id="unexpected-submission"
        )
    elif mutation == "current-owner":
        TaskWorkerLease.objects.filter(worker_id="maintenance-after-manager").update(
            is_active=False
        )
    rows = maintenance.inventory()
    if mutation:
        with pytest.raises(ValueError):
            maintenance.expected_blockers(
                [item.pk for item in fixtures], rows, manager_prefix="django-manager-"
            )
    else:
        expected = maintenance.expected_blockers(
            [item.pk for item in fixtures], rows, manager_prefix="django-manager-"
        )
        assert list(expected.values()) == ["invalid_snapshot", "unavailable_key"] * 2
        with pytest.raises(ValueError, match="live manager"):
            refusal.inspect_failure(fixtures[0], manager_prefix="django-manager-")
