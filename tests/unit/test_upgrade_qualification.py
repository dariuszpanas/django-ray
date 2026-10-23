"""The database stage must reject incomplete evidence and preserve its runtime boundary."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
import yaml

from qualification.docker.scenario import QualificationError
from qualification.upgrade import contract, prepare, scenario


def receipt(backend="sqlite"):
    values = [
        {"tasks": 8, "fixture_kind": "synthetic-released-models"},
        {"blocked_tasks": 2, "active_leases": 1, "read_only": True},
        {"nonterminal_tasks": 0, "active_leases": 0, "settlement": "synthetic-only"},
        {
            "historical_sha256": "a" * 64,
            "tasks": 8,
            "inert_execution_refusals": 0,
            "input_and_result_artifacts_read": True,
        },
        {
            "historical_sha256": "a" * 64,
            "tasks": 8,
            "inert_execution_refusals": 8,
            "input_and_result_artifacts_read": True,
            "missing_and_corrupt_result_rejected": True,
        },
        {"current_enqueue": True, "candidate_only_rows": 1},
        {
            "historical_sha256": "a" * 64,
            "tasks": 8,
            "inert_execution_refusals": 0,
            "input_and_result_artifacts_read": True,
            "candidate_writes_preserved": True,
            "candidate_result_read": True,
            "migrations_retained": True,
            "read_only": True,
        },
        {
            "historical_sha256": "a" * 64,
            "tasks": 8,
            "inert_execution_refusals": 0,
            "input_and_result_artifacts_read": True,
            "candidate_writes_absent_from_old_backup": True,
        },
    ]
    return {
        "backend": backend,
        "phases": [
            {
                "phase": phase,
                "pid": index + 1,
                "module": "/installed/django_ray/__init__.py",
                "version": contract.CANDIDATE_VERSION
                if phase.startswith("candidate-")
                else contract.BASELINE_VERSION,
                "status": "passed",
                "observations": values[index],
            }
            for index, phase in enumerate(contract.PHASES)
        ],
        "backup_sha256": "b" * 64,
        "artifacts_sha256": "c" * 64,
        "fixture_cleanup": True,
        "server_stopped": True,
        "complete_upgrade_gate": False,
        "missing_acceptance": list(contract.MISSING),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-phase",
        "reordered",
        "same-process",
        "wrong-version",
        "failed-phase",
        "bad-backup",
        "cleanup",
        "server",
        "overclaim",
        "missing-gaps",
        "changed-history",
        "no-blockers",
        "executable-history",
        "lost-new-write",
        "ignored-missing-artifact",
        "boolean-row-count",
        "rollback-lost-write",
        "rollback-downgraded-schema",
        "rollback-not-read-only",
    ],
)
def test_incomplete_upgrade_cannot_pass(mutation):
    value = receipt()
    if mutation == "missing-phase":
        value["phases"].pop()
    elif mutation == "reordered":
        value["phases"].reverse()
    elif mutation == "same-process":
        value["phases"][1]["pid"] = value["phases"][0]["pid"]
    elif mutation == "wrong-version":
        value["phases"][4]["version"] = "0.4.0"
    elif mutation == "failed-phase":
        value["phases"][2]["status"] = "skipped"
    elif mutation == "bad-backup":
        value["backup_sha256"] = "x" * 64
    elif mutation == "cleanup":
        value["fixture_cleanup"] = False
    elif mutation == "server":
        value["server_stopped"] = False
    elif mutation == "overclaim":
        value["complete_upgrade_gate"] = True
    elif mutation == "missing-gaps":
        value["missing_acceptance"].clear()
    elif mutation == "changed-history":
        value["phases"][4]["observations"]["historical_sha256"] = "d" * 64
    elif mutation == "no-blockers":
        value["phases"][1]["observations"]["blocked_tasks"] = 0
    elif mutation == "executable-history":
        value["phases"][4]["observations"]["inert_execution_refusals"] = 0
    elif mutation == "lost-new-write":
        value["phases"][5]["observations"]["candidate_only_rows"] = 0
    elif mutation == "ignored-missing-artifact":
        value["phases"][4]["observations"]["missing_and_corrupt_result_rejected"] = False
    elif mutation.startswith("rollback-"):
        field = {
            "rollback-lost-write": "candidate_writes_preserved",
            "rollback-downgraded-schema": "migrations_retained",
            "rollback-not-read-only": "read_only",
        }[mutation]
        value["phases"][6]["observations"][field] = False
    else:
        value["phases"][5]["observations"]["candidate_only_rows"] = True
    with pytest.raises(QualificationError):
        contract.validate_backend(value, backend="sqlite")


def test_complete_stage_emits_sixteen_phases_but_no_complete_upgrade_claim():
    values = [receipt(backend) for backend in contract.BACKENDS]
    before = copy.deepcopy(values)
    suite = ElementTree.fromstring(contract.junit(values, failure=None))
    assert len(suite) == 16 and suite.attrib["failures"] == "0"
    assert values == before and all(value["complete_upgrade_gate"] is False for value in values)
    with pytest.raises(QualificationError, match="missing-upgrade-backend"):
        contract.junit(values[:1], failure=None)
    failed = ElementTree.fromstring(contract.junit([], failure="fixture-cleanup-failed"))
    assert failed.attrib["failures"] == "1"


@pytest.mark.parametrize("kind", ["wrong-commit", "escape", "symlink", "oversized"])
def test_baseline_archive_rejects_wrong_identity_and_unsafe_entries(tmp_path, kind):
    path = tmp_path / "source.tar"
    with tarfile.open(
        path,
        "w",
        format=tarfile.PAX_FORMAT,
        pax_headers={
            "comment": "0" * 40 if kind == "wrong-commit" else contract.BASELINE_COMMIT,
        },
    ) as archive:
        entry = tarfile.TarInfo("../outside" if kind == "escape" else "pyproject.toml")
        entry.size = 0
        if kind == "symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/outside"
        archive.addfile(entry, io.BytesIO())
    if kind == "oversized":
        with path.open("r+b") as stream:
            stream.truncate(128 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        prepare.verify_archive(path)


def test_restore_cannot_target_original_database(tmp_path, monkeypatch):
    command = Mock()
    monkeypatch.setattr(scenario, "_run", command)
    with pytest.raises(QualificationError, match="restore-destination"):
        scenario._restore(tmp_path, "postgresql", "baseline")
    command.assert_not_called()


def test_postgres_restore_uses_only_owned_socket_and_fixed_database(tmp_path, monkeypatch):
    command = Mock()
    monkeypatch.setattr(scenario, "_run", command)
    scenario._restore(tmp_path, "postgresql", "rollback")
    args = command.call_args.args[0]
    assert args[2] == tmp_path / "socket"
    assert args[6] == "rollback"
    assert "--exit-on-error" in args and args[-1] == tmp_path / "backup"


def test_compose_has_no_runtime_network_and_requires_explicit_source_and_evidence():
    root = Path(__file__).resolve().parents[2]
    document = yaml.safe_load((root / "qualification/upgrade/data.yaml").read_text())
    service = document["services"]["data"]
    assert service["network_mode"] == "none"
    assert service["cpus"] == 1.0 and service["mem_limit"] == service["memswap_limit"] == "1g"
    assert service["pids_limit"] == 128 and service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert "ports" not in service and "depends_on" not in service
    assert service["build"]["additional_contexts"]["tools"] == "service:tools"
    assert "UPGRADE_BASELINE_SOURCE:?" in service["build"]["additional_contexts"]["released-source"]
    assert any("UPGRADE_EVIDENCE_DIR:?" in volume["source"] for volume in service["volumes"])


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), ValueError("invalid")])
def test_archive_failure_retains_failed_manifest_without_rehashing_missing_file(monkeypatch, error):
    output = {}
    monkeypatch.setattr(scenario.wheel, "_ensure_evidence_root", lambda _path: None)
    monkeypatch.setattr(scenario.wheel, "_require_non_root", lambda: None)
    monkeypatch.setattr(scenario.wheel, "_require_linux_process_groups", lambda: None)
    monkeypatch.setattr(scenario, "verify_archive", Mock(side_effect=error))
    digest = Mock(side_effect=AssertionError("failed archive must not be hashed"))
    monkeypatch.setattr(scenario.wheel, "_sha256", digest)
    monkeypatch.setattr(
        scenario.wheel, "_write_manifest", lambda path, value: output.setdefault(path.name, value)
    )

    assert scenario.execute() == 1

    digest.assert_not_called()
    manifest = json.loads(output["execution-manifest.json"])
    assert manifest["outcome"] == "failed" and manifest["failure"] == type(error).__name__
    assert manifest["baseline_archive_sha256"] is None
    assert manifest["candidate_source_files_sha256"] is None
    assert manifest["complete_upgrade_gate"] is False
    assert ElementTree.fromstring(output["junit.xml"]).attrib["failures"] == "1"


@pytest.mark.parametrize("baseline", ["0.4.0", "0.5.0", "0.6.0", "0.7.0"])
def test_reviewed_baseline_selection_matches_candidate_source(baseline):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from qualification.upgrade import contract as c; "
            "print(json.dumps([c.BASELINE_VERSION, c.BASELINE_COMMIT, c.CANDIDATE_VERSION]))",
        ],
        cwd=root,
        env={**os.environ, "DJANGO_RAY_UPGRADE_BASELINE": baseline},
        check=True,
        capture_output=True,
        text=True,
    )
    expected = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert json.loads(result.stdout) == [baseline, contract.BASELINES[baseline], expected]


def test_baseline_defaults_and_image_allowlist_preserve_reviewed_history():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from qualification.upgrade.contract import BASELINE_VERSION; print(BASELINE_VERSION)",
        ],
        cwd=root,
        env={
            key: value for key, value in os.environ.items() if key != "DJANGO_RAY_UPGRADE_BASELINE"
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "0.4.0"
    assert contract.BASELINES["0.7.0"] == "d8204c9e368336077396679a3019f7f300211e06"
    dockerfile = (root / "qualification/upgrade/Dockerfile").read_text()
    assert set(re.findall(r"'([0-9a-f]{40})'", dockerfile)) == set(contract.BASELINES.values())
    compose = yaml.safe_load((root / "qualification/upgrade/data.yaml").read_text())
    data = compose["services"]["data"]
    assert (
        data["environment"]["DJANGO_RAY_UPGRADE_BASELINE"]
        == "${DJANGO_RAY_UPGRADE_BASELINE:-0.4.0}"
    )
    assert data["build"]["args"]["UPGRADE_BASELINE_COMMIT"] == (
        "${UPGRADE_BASELINE_COMMIT:-" + contract.BASELINES["0.4.0"] + "}"
    )


@pytest.mark.parametrize("swap_installed_bytes", [False, True])
def test_equal_version_baseline_and_candidate_keep_distinct_verified_sources(
    tmp_path, monkeypatch, swap_installed_bytes
):
    wheel = scenario.wheel
    sources, targets, wheels = {}, {}, {}
    for name in ("released", "candidate"):
        source = tmp_path / name / "source"
        package = source / "src" / "django_ray"
        package.mkdir(parents=True)
        (source / "pyproject.toml").write_text('[project]\nversion = "0.7.0"\n')
        content = f'__version__ = "0.7.0"\nSOURCE = "{name}"\n'
        (package / "__init__.py").write_text(content)
        target = tmp_path / name / "installed"
        (target / "django_ray").mkdir(parents=True)
        (target / "django_ray" / "__init__.py").write_text(content)
        selected = tmp_path / name / "django_ray-0.7.0-py3-none-any.whl"
        selected.write_bytes(name.encode())
        sources[name], targets[name], wheels[name] = source, target, selected

    def imported(target):
        module = ModuleType("django_ray")
        module.__file__ = str(target / "django_ray" / "__init__.py")
        return module

    monkeypatch.setattr(
        wheel, "_target_distribution", lambda _target: SimpleNamespace(version="0.7.0")
    )
    monkeypatch.setattr(wheel, "_reject_preinstalled_django_ray", lambda: None)
    monkeypatch.setattr(wheel, "_import_candidate", imported)
    identities = {
        name: wheel._inspect_candidate(wheels[name], targets[name], sources[name])
        for name in sources
    }
    assert identities["released"].version == identities["candidate"].version == "0.7.0"
    assert identities["released"].source_package_tree_sha256 != (
        identities["candidate"].source_package_tree_sha256
    )
    assert identities["released"].wheel_sha256 != identities["candidate"].wheel_sha256
    if swap_installed_bytes:
        (targets["candidate"] / "django_ray" / "__init__.py").write_bytes(
            (targets["released"] / "django_ray" / "__init__.py").read_bytes()
        )
        with pytest.raises(QualificationError, match="candidate-source-tree-mismatch"):
            wheel._inspect_candidate(
                wheels["candidate"], targets["candidate"], sources["candidate"]
            )


@pytest.mark.parametrize("phase", ["baseline-run", "candidate-run", "baseline-post-write"])
def test_native_equal_versions_select_phase_owned_environment(tmp_path, monkeypatch, phase):
    from qualification.upgrade import native_scenario

    monkeypatch.setattr(native_scenario, "BASELINE_VERSION", "0.7.0")
    monkeypatch.setattr(native_scenario, "CANDIDATE_VERSION", "0.7.0")
    target = tmp_path / ("released" if phase.startswith("baseline") else "candidate")
    module = str((target / "django_ray/__init__.py").resolve())

    def run(command, **kwargs):
        expected_python = (
            scenario.RELEASED_PYTHON if phase.startswith("baseline") else Path(sys.executable)
        )
        assert command[0] == expected_python
        assert command[-2] == module
        assert kwargs["environment"]["PYTHONPATH"].split(os.pathsep)[0] == str(target)
        command[-1].write_text(
            json.dumps({"phase": phase, "module": module, "version": "0.7.0", "observations": {}})
        )

    monkeypatch.setattr(scenario, "_run", run)
    observed = native_scenario.phase(
        tmp_path,
        "sqlite",
        phase,
        target,
        database="restored",
        artifacts=tmp_path,
        runner="ray_core",
    )
    assert observed["module"] == module and observed["phase"] == phase


def test_unreviewed_baseline_fails_before_fixture_execution():
    result = subprocess.run(
        [sys.executable, "-c", "import qualification.upgrade.contract"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DJANGO_RAY_UPGRADE_BASELINE": "main"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unsupported-upgrade-baseline" in result.stderr


@pytest.mark.parametrize("corrupt", [False, True])
def test_sqlite_restore_checks_django_constraints_and_closes_connections(tmp_path, corrupt):
    from django.db.backends.sqlite3._functions import register

    with contextlib.closing(sqlite3.connect(tmp_path / "backup")) as source:
        register(source)
        source.execute(
            "CREATE TABLE fixture (recorded TEXT CHECK "
            "(django_format_dtdelta('+', recorded, 0) IS NOT NULL))"
        )
        if corrupt:
            source.execute("PRAGMA ignore_check_constraints=ON")
        source.execute(
            "INSERT INTO fixture VALUES (?)", ("not-a-date" if corrupt else "2026-01-01",)
        )
        source.commit()
    if corrupt:
        with pytest.raises(AssertionError):
            scenario._restore(tmp_path, "sqlite", "restored")
    else:
        scenario._restore(tmp_path, "sqlite", "restored")
    # Windows refuses unlinking an open SQLite file. Both success and error
    # paths must release the connection before fixture cleanup.
    (tmp_path / "restored.sqlite3").unlink()
    (tmp_path / "backup").unlink()


@pytest.mark.parametrize("version", ["0.4.0", "0.5.0", "0.6.0", "0.7.0"])
@pytest.mark.parametrize("refusal", ["25006", "unexpected-error", "accepted"])
def test_native_graph_read_only_fence_is_independent_of_module_layout(
    monkeypatch, version, refusal
):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import django.db
    import django.db.transaction

    import django_ray
    from qualification.upgrade import native_workflow

    monkeypatch.setattr(django_ray, "__version__", version)
    cursor = MagicMock()
    cursor.fetchone.return_value = ("on",)
    connection = MagicMock(vendor="postgresql")
    connection.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr(django.db, "connection", connection)
    monkeypatch.setattr(django.db.transaction, "atomic", nullcontext)

    def topology(*args, **kwargs):
        if refusal == "accepted":
            return []
        cause = RuntimeError("database read-only refusal")
        cause.sqlstate = refusal
        raise django.db.InternalError("cannot lock read-only rows") from cause

    reads = SimpleNamespace(
        get_workflow_progress_summary=Mock(return_value={}),
        list_workflow_topology_nodes=Mock(side_effect=topology),
    )
    modules = []

    def import_module(name):
        modules.append(name)
        return reads if name.endswith("reads") else SimpleNamespace()

    monkeypatch.setattr(native_workflow, "importlib", SimpleNamespace(import_module=import_module))
    execution = SimpleNamespace(pk=1)
    if refusal == "25006":
        native_workflow.read_graph(execution)
    else:
        with pytest.raises(AssertionError):
            native_workflow.read_graph(execution)
    assert modules[0] == (
        "django_ray.workflow_progress_reads"
        if version == "0.4.0"
        else "django_ray.workflow.progress.reads"
    )
    reads.list_workflow_topology_nodes.assert_called_once()
    cursor.execute.assert_called_once_with("SHOW default_transaction_read_only")


def test_legacy_failure_manager_uses_cli_shutdown_semantics(monkeypatch):
    import django
    from django.core import management

    from django_ray.runner.ray_job import RayJobRunner
    from qualification.upgrade import legacy_failure

    calls = []
    monkeypatch.setattr(django, "setup", lambda: None)
    monkeypatch.setattr(management, "execute_from_command_line", calls.append)
    original_logs = RayJobRunner.get_logs
    try:
        legacy_failure.manager()
        assert RayJobRunner.get_logs is not original_logs
        assert calls == [["django-ray-qualification", "django_ray_worker", "--concurrency=1"]]
    finally:
        RayJobRunner.get_logs = original_logs


def test_retired_carriers_are_refused_in_a_fresh_process_before_setup():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            "import json; from qualification.upgrade.carriers import verify_retired_carriers; "
            "print(json.dumps(verify_retired_carriers()))",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)])},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "legacy_runtime_env_refused": 4,
        "job_carriers_refused": 4,
        "inline_job_carriers_refused": 2,
        "core_carriers_refused": 2,
        "workflow_carriers_refused": 3,
        "malformed_job_refused": True,
        "cli_refusal_exit": 78,
        "application_boundary_calls": 0,
        "django_setup": False,
    }
    assert not result.stderr


@pytest.fixture
def crash_policy_fixture(settings):
    from django.utils import timezone

    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKER_LEASE_SECONDS": 6,
        "WORKER_HEARTBEAT_SECONDS": 2,
    }
    task = RayTaskExecution.objects.create(
        task_id="retained-native-history",
        callable_path="fixture.task",
        state="SUCCEEDED",
        attempt_number=3,
        execution_generation=5,
        runtime_env_json="private-retained-envelope",
        runtime_env_hash="a" * 64,
        result_data='{"value":42}',
    )
    TaskAttempt.objects.create(execution=task, attempt_number=3, state="SUCCEEDED")
    lease = TaskWorkerLease.objects.create(
        worker_id="retained-old-worker",
        hostname="fixture",
        pid=123,
        queue_name="default",
        started_at=timezone.now(),
        last_heartbeat_at=timezone.now(),
        is_active=False,
        stopped_at=timezone.now(),
    )
    return task, lease


@pytest.mark.django_db(transaction=True)
def test_native_crash_policy_roundtrip_uses_cas_and_preserves_retained_records(
    crash_policy_fixture,
):
    from django_ray.lease_policy import LeasePolicy, read_lease_policy
    from qualification.upgrade import native_probe

    history = native_probe.snapshot()
    before = native_probe.transition_crash_lease_policy()
    assert read_lease_policy() == LeasePolicy(6, 2)
    after = native_probe.transition_crash_lease_policy(restore=True)
    assert read_lease_policy(matching=False) == LeasePolicy(60, 3)
    for report, expected in ((before, (60, 1, 6, 2)), (after, (6, 2, 60, 3))):
        assert (
            tuple(
                report[name]
                for name in ("expected_seconds", "expected_revision", "lease_seconds", "revision")
            )
            == expected
        )
        assert report["history_preserved"] and report["leases_preserved"]
        assert report["active_leases"] == report["nonterminal"] == 0
        assert "private-retained-envelope" not in json.dumps(report)
    assert native_probe.snapshot() == history


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("restore", [False, True])
def test_native_crash_policy_refuses_active_stale_lease_without_retiring_it(
    crash_policy_fixture, restore
):
    from datetime import timedelta

    from django.core.management import CommandError
    from django.utils import timezone

    from django_ray.lease_policy import read_lease_policy
    from django_ray.models import TaskWorkerLease
    from qualification.upgrade import native_probe

    _, lease = crash_policy_fixture
    if restore:
        native_probe.transition_crash_lease_policy()
    lease.is_active = True
    lease.stopped_at = None
    lease.last_heartbeat_at = timezone.now() - timedelta(days=1)
    lease.save()
    before_policy = read_lease_policy(matching=False)
    before_lease = TaskWorkerLease.objects.values().get(pk=lease.pk)
    before_history = native_probe.snapshot()
    with pytest.raises(CommandError, match="lease_policy_active_leases"):
        native_probe.transition_crash_lease_policy(restore=restore)
    assert TaskWorkerLease.objects.values().get(pk=lease.pk) == before_lease
    assert read_lease_policy(matching=False) == before_policy
    assert native_probe.snapshot() == before_history


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "CANCELLING"])
def test_native_crash_policy_requires_drained_tasks_before_transition(crash_policy_fixture, state):
    from django_ray.lease_policy import LeasePolicy, read_lease_policy
    from qualification.upgrade import native_probe

    task, _ = crash_policy_fixture
    task.state = state
    task.save()
    before = native_probe.snapshot()
    with pytest.raises(AssertionError):
        native_probe.transition_crash_lease_policy()
    assert read_lease_policy(matching=False) == LeasePolicy(60, 1)
    assert native_probe.snapshot() == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fails", [False, True])
def test_native_candidate_crash_brackets_work_with_policy_transitions(
    crash_policy_fixture, settings, monkeypatch, tmp_path, fails
):
    import django_ray
    from django_ray.lease_policy import LeasePolicy, read_lease_policy
    from qualification.upgrade import native_probe

    settings.ROOT, settings.RUNNER, settings.CRASH_MANAGER = tmp_path, "ray_core", True
    receipt_path = tmp_path / "receipt.json"
    (tmp_path / "history.json").write_text(json.dumps(native_probe.snapshot()))
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "candidate-run", str(Path(django_ray.__file__).resolve()), str(receipt_path)],
    )
    monkeypatch.setattr(native_probe, "read_history", lambda *_args: 4)

    def work(_root, *, check_completion_encoding):
        assert check_completion_encoding
        assert read_lease_policy() == LeasePolicy(6, 2)
        if fails:
            raise RuntimeError("manager exit was not confirmed")
        return {"active_leases": 0, "nonterminal": 0}

    monkeypatch.setattr(native_probe, "run_work", work)
    if fails:
        with pytest.raises(RuntimeError, match="manager exit was not confirmed"):
            native_probe.main()
        assert read_lease_policy() == LeasePolicy(6, 2)
        assert not receipt_path.exists()
    else:
        native_probe.main()
        assert read_lease_policy(matching=False) == LeasePolicy(60, 3)
        observed = json.loads(receipt_path.read_text())["observations"]["lease_policy"]
        assert observed["before_crash"]["revision"] == 2
        assert observed["after_drain"]["revision"] == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("baseline", ["0.5.0", "0.6.0"])
def test_native_baseline_never_requires_candidate_lease_policy_command(
    settings, monkeypatch, tmp_path, baseline
):
    import django_ray
    from qualification.upgrade import native_probe

    monkeypatch.setattr(django_ray, "__version__", baseline)
    monkeypatch.setattr(contract, "BASELINE_VERSION", baseline)
    settings.ROOT, settings.RUNNER, settings.CRASH_MANAGER = tmp_path, "ray_core", True
    receipt_path = tmp_path / "baseline.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "baseline-run", str(Path(django_ray.__file__).resolve()), str(receipt_path)],
    )
    monkeypatch.setattr(native_probe, "read_history", lambda *_args: 4)
    monkeypatch.setattr(native_probe, "run_work", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        native_probe,
        "transition_crash_lease_policy",
        Mock(side_effect=AssertionError("released version has no lease policy command")),
    )
    native_probe.main()
    assert json.loads(receipt_path.read_text())["version"] == baseline
    assert "lease_policy" not in json.loads(receipt_path.read_text())["observations"]
