"""Tests for exit-0 doctor health, probe accuracy, unified CLI flags, and maintenance fixes."""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.db.utils import ConnectionHandler

from django_ray.doctor import __main__ as doctor_entry
from django_ray.doctor import contracts, probe, runner, supervisor
from django_ray.management import runtime_env_cursor as cursor_protocol
from django_ray.management import runtime_env_maintenance as maintenance
from django_ray.management.commands import (
    django_ray_audit_workflow_progress as audit_cmd,
)
from django_ray.management.commands import (
    django_ray_benchmark_workflow_progress as benchmark_cmd,
)
from django_ray.management.commands import django_ray_doctor as doctor_cmd
from django_ray.management.commands import django_ray_lease_policy as lease_policy_cmd
from django_ray.management.commands import django_ray_protocol_status as protocol_status_cmd
from django_ray.management.commands import django_ray_runtime_env as runtime_env_cmd
from django_ray.management.commands import django_ray_worker_ready as worker_ready_cmd
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease, TaskWorkerLeasePolicy
from django_ray.runtime.runtime_env import normalize_runtime_env

_OLD_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
_NEW_KEY = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def _doctor_template_db(tmp_path_factory, django_db_blocker):
    alias = "doctor_w2_template"
    path = tmp_path_factory.mktemp("doctor_w2_template") / "owned.sqlite3"
    source = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}
    configured = ConnectionHandler({"default": source}).settings["default"]
    wrapper = ConnectionHandler({"default": configured})["default"]
    wrapper.alias = alias
    connections.databases[alias] = configured
    connections[alias] = wrapper
    with django_db_blocker.unblock():
        try:
            call_command("migrate", database=alias, verbosity=0, interactive=False)
        finally:
            connections[alias].close()
            del connections[alias]
            del connections.databases[alias]
    return path


@pytest.fixture
def owned_doctor_db(tmp_path, django_db_blocker, db, _doctor_template_db):
    del db
    alias = "doctor_w2_test"
    path = tmp_path / "owned.sqlite3"
    shutil.copyfile(_doctor_template_db, path)
    source = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}
    configured = ConnectionHandler({"default": source}).settings["default"]
    wrapper = ConnectionHandler({"default": configured})["default"]
    wrapper.alias = alias
    connections.databases[alias] = configured
    connections[alias] = wrapper
    with django_db_blocker.unblock():
        try:
            yield alias, path
        finally:
            connections[alias].close()
            del connections[alias]
            del connections.databases[alias]


def _observe_owned(alias: str, path, *, config: dict | None = None) -> dict:
    source = SimpleNamespace(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}}
    )
    readonly = probe.readonly_database(source, "default", time.monotonic() + 5)
    wrapper = ConnectionHandler({"default": readonly})["default"]
    wrapper.alias = alias
    original = connections[alias]
    connections[alias] = wrapper
    output: dict = {}
    try:
        probe.collect_database(
            using=alias,
            budget=probe.QueryBudget(time.monotonic() + 5),
            config={"WORKER_LEASE_SECONDS": 60} if config is None else config,
            emit=lambda name, value: output.update({name: value}),
        )
    finally:
        connections[alias] = original
        assert wrapper.connection is None
    return output


def _healthy_report() -> dict:
    sections = contracts.initial_sections()
    for name in contracts.REQUIRED_SECTIONS:
        sections[name] = contracts.observation("observed", "observed")
    return contracts.result(sections, contracts.Limits(), 0.25, True)


class TestDoctorExitZeroAndRemediation:
    """Healthy hosts exit 0 with explicit deferred sections and readable output."""

    def test_healthy_report_exits_zero_and_distinguishes_deferred_sections(self) -> None:
        report = _healthy_report()
        assert report["status"] == "ok"
        assert report["exit_code"] == 0
        assert report["complete"] is True
        assert report["deferred_sections"] == list(contracts.UNAVAILABLE_SECTIONS)
        assert report["remediations"] == {}

        rendered = contracts.render(report, as_json=False)
        assert "status=OK" in rendered
        assert "Required checks:" in rendered
        assert "Optional checks (deferred / out-of-scope):" in rendered
        assert "[OK]" in rendered
        assert "[SKIPPED]" in rendered
        assert "Remediation:" not in rendered
        assert len(rendered.encode("utf-8")) < contracts.OUTPUT_BYTES

    def test_management_command_and_standalone_exit_zero_on_healthy_host(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        healthy = _healthy_report()
        monkeypatch.setattr(doctor_cmd, "diagnose", lambda **_kwargs: healthy)

        text_out = StringIO()
        call_command("django_ray_doctor", stdout=text_out)
        assert "status=OK" in text_out.getvalue()

        json_out = StringIO()
        call_command("django_ray_doctor", "--json", stdout=json_out)
        parsed = json.loads(json_out.getvalue())
        assert parsed["status"] == "ok" and parsed["exit_code"] == 0

        monkeypatch.setattr(doctor_entry, "diagnose", lambda **_kwargs: healthy)
        monkeypatch.setattr(sys, "argv", ["doctor", "--settings", "owned.settings", "--json"])
        assert doctor_entry.main() == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["status"] == "ok"
        assert captured.err == ""

    def test_blocker_and_incomplete_reports_emit_specific_exit_codes_and_remediations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sections = contracts.initial_sections()
        for name in contracts.REQUIRED_SECTIONS:
            sections[name] = contracts.observation("observed", "observed")
        sections["configuration"] = contracts.observation(
            "blocker", "configuration_invalid", detail="invalid_runner"
        )
        blocker_report = contracts.result(sections, contracts.Limits(), 0.1, True)
        assert blocker_report["status"] == "blocker"
        assert blocker_report["exit_code"] == 1
        assert "DJANGO_RAY['RUNNER']" in blocker_report["remediations"]["configuration"]
        rendered_blocker = contracts.render(blocker_report, as_json=False)
        assert "[BLOCKER]" in rendered_blocker
        assert "Remediation:" in rendered_blocker
        assert "DJANGO_RAY['RUNNER']" in rendered_blocker

        monkeypatch.setattr(doctor_cmd, "diagnose", lambda **_kwargs: blocker_report)
        with pytest.raises(CommandError, match="blockers") as exc_info:
            call_command("django_ray_doctor", stdout=StringIO())
        assert exc_info.value.returncode == 1

        sections["configuration"] = contracts.observation("observed", "observed")
        sections["leases"] = contracts.observation("unavailable", "lease_policy_mismatch")
        incomplete_report = contracts.result(sections, contracts.Limits(), 0.1, True)
        assert incomplete_report["status"] == "incomplete"
        assert incomplete_report["exit_code"] == 2
        assert "WORKER_LEASE_SECONDS" in incomplete_report["remediations"]["leases"]

    def test_limits_errors_name_offending_flag_and_bounds(self) -> None:
        with pytest.raises(ValueError, match=r"--total-seconds.*between 5 and 60"):
            contracts.Limits(total_seconds=2, probe_seconds=3)
        with pytest.raises(ValueError, match=r"--probe-seconds.*between 3 and 20"):
            contracts.Limits(total_seconds=15, probe_seconds=25)
        with pytest.raises(ValueError, match=r"--probe-seconds.*--total-seconds - 2"):
            contracts.Limits(total_seconds=6, probe_seconds=5)

        with pytest.raises(CommandError, match="--total-seconds") as exc_info:
            call_command("django_ray_doctor", "--total-seconds", "1")
        assert exc_info.value.returncode == 2

    def test_configuration_checks_surface_distinct_secret_free_detail_codes(self) -> None:
        cases = [
            ({"RUNNER": "unknown"}, "invalid_runner"),
            ({"RUNTIME_ENV_STORAGE_MODE": "bad"}, "invalid_runtime_env_storage_mode"),
            ({"INPUT_STORAGE_BACKEND": "bad"}, "invalid_input_storage_backend"),
            ({"RESULT_STORAGE_BACKEND": "bad"}, "invalid_result_storage_backend"),
            ({"INPUT_STORAGE_BACKEND": "s3"}, "missing_input_storage_location"),
            ({"RESULT_STORAGE_BACKEND": "gcs"}, "missing_result_storage_location"),
            ({"RUNTIME_ENV_ENCRYPTION_KEYS": []}, "invalid_encryption_keys"),
            ({"RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "x" * 65}, "invalid_encryption_active_key"),
            ({"RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": "yes"}, "invalid_secret_fallback"),
        ]
        for config, expected_detail in cases:
            with pytest.raises(probe.ProbeUnavailableError) as exc_info:
                probe.configuration(SimpleNamespace(DJANGO_RAY=config))
            assert exc_info.value.code == "configuration_invalid"
            assert exc_info.value.detail == expected_detail
            assert contracts.remediation_for(exc_info.value.code, exc_info.value.detail)


class TestDoctorProbeLogicFixes:
    """Verify F-033 and F-021 database probe logic fixes."""

    def test_legacy_worker_capacity_and_legacy_metadata_schema_zero_are_accepted(
        self, owned_doctor_db
    ) -> None:
        alias, path = owned_doctor_db
        now = datetime.now(UTC)
        TaskWorkerLease.objects.using(alias).create(
            worker_id="legacy-v0-worker",
            hostname="legacy-host",
            pid=42,
            capability_schema_version=0,
            django_ray_version=None,
            min_supported_execution_protocol_version=None,
            max_supported_execution_protocol_version=None,
            last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=60),
            is_active=True,
        )
        RayTaskExecution.objects.using(alias).create(
            task_id="legacy-meta-v0-task",
            callable_path="myapp.tasks.work",
            state=TaskState.QUEUED,
            execution_protocol_version=1,
            metadata_schema_version=0,
        )

        report = _observe_owned(alias, path)
        assert report["leases"]["outcome"] == "observed"
        assert report["leases"]["values"]["live"] == 1
        assert report["leases"]["values"]["legacy_active"] == 1
        assert report["work"]["outcome"] == "observed"
        assert report["work"]["values"]["queued"] == 1
        assert report["work"]["values"]["queued_without_protocol_lease"] == 0
        assert report["work"]["values"]["unsupported_protocol"] == 0

    def test_active_without_heartbeat_uses_stuck_task_timeout_not_worker_lease_seconds(
        self, owned_doctor_db
    ) -> None:
        alias, path = owned_doctor_db
        TaskWorkerLeasePolicy.objects.using(alias).update(lease_seconds=15)
        now = datetime.now(UTC)
        # Task heartbeat is 60s old: older than WORKER_LEASE_SECONDS (15s),
        # but well within STUCK_TASK_TIMEOUT_SECONDS (300s).
        healthy_task = RayTaskExecution.objects.using(alias).create(
            task_id="healthy-long-heartbeat",
            callable_path="myapp.tasks.work",
            state=TaskState.RUNNING,
            last_heartbeat_at=now - timedelta(seconds=60),
        )

        report = _observe_owned(
            alias,
            path,
            config={"WORKER_LEASE_SECONDS": 15, "STUCK_TASK_TIMEOUT_SECONDS": 300},
        )
        assert report["work"]["values"]["running"] == 1
        assert report["work"]["values"]["active_without_heartbeat"] == 0
        assert report["work"]["outcome"] == "observed"

        # Now make the heartbeat older than STUCK_TASK_TIMEOUT_SECONDS (300s).
        RayTaskExecution.objects.using(alias).filter(pk=healthy_task.pk).update(
            last_heartbeat_at=now - timedelta(seconds=310)
        )
        stale_report = _observe_owned(
            alias,
            path,
            config={"WORKER_LEASE_SECONDS": 15, "STUCK_TASK_TIMEOUT_SECONDS": 300},
        )
        assert stale_report["work"]["values"]["active_without_heartbeat"] == 1
        assert stale_report["work"]["outcome"] == "blocker"

    def test_db_clock_lease_expires_at_eliminates_zero_skew_race(self, owned_doctor_db) -> None:
        alias, path = owned_doctor_db
        now = datetime.now(UTC)
        # Lease has DB-clock lease_expires_at in the future and a slight 1s forward
        # heartbeat skew that previously triggered stale_active.
        TaskWorkerLease.objects.using(alias).create(
            worker_id="live-db-clock-worker",
            hostname="host-a",
            pid=101,
            capability_schema_version=1,
            django_ray_version="0.8.0",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
            started_at=now - timedelta(seconds=10),
            last_heartbeat_at=now + timedelta(seconds=1),
            lease_expires_at=now + timedelta(seconds=45),
            is_active=True,
        )
        # Expired DB-clock lease must be classified stale_active.
        TaskWorkerLease.objects.using(alias).create(
            worker_id="expired-db-clock-worker",
            hostname="host-b",
            pid=102,
            capability_schema_version=1,
            django_ray_version="0.8.0",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
            started_at=now - timedelta(seconds=120),
            last_heartbeat_at=now - timedelta(seconds=70),
            lease_expires_at=now - timedelta(seconds=5),
            is_active=True,
        )

        report = _observe_owned(alias, path)
        assert report["leases"]["values"]["live"] == 1
        assert report["leases"]["values"]["stale_active"] == 1

    def test_tight_budget_startup_margin_and_blocking_io_handling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Runner reserves STARTUP_MARGIN_SECONDS so tight remaining budgets report
        # total_budget_exhausted cleanly instead of spawning a doomed child.
        clock = SimpleNamespace(now=0.0)
        monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: clock.now))
        monkeypatch.setattr(runner.sys, "platform", "linux")
        monkeypatch.setattr(runner, "_caller_sigchld_default", lambda: True)
        monkeypatch.setattr(runner.signal, "signal", lambda *_args: runner.signal.SIG_DFL)

        launched: list[str] = []

        def fake_run(name, _settings, _alias, _deadline, sections, _interrupted):
            launched.append(name)
            sections[name] = contracts.observation("observed", "observed")
            # Leave 4.1s total -> available = 2.1s <= CLEANUP_SECONDS + 0.5s margin.
            clock.now = 10.9
            return True

        monkeypatch.setattr(runner, "_run_probe", fake_run)
        report = runner.diagnose(settings_module="owned.settings", limits=contracts.Limits(15, 5))
        assert launched == ["configuration"]
        assert report["sections"]["database"]["code"] == "total_budget_exhausted"
        assert report["exit_code"] == 2

        # Supervisor main emits a clean probe_timeout done frame if startup ate the budget.
        stream = BytesIO()
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "supervisor",
                "--probe",
                "database",
                "--settings",
                "owned.settings",
                "--database=default",
                "--deadline",
                str(time.monotonic() + 1.0),
            ],
        )
        monkeypatch.setattr(supervisor.sys, "stdout", SimpleNamespace(buffer=stream))
        assert supervisor.main() == 2
        frame = json.loads(stream.getvalue().decode("utf-8"))
        assert frame == {"kind": "done", "code": "probe_timeout", "cleanup_confirmed": True}

    def test_supervisor_read_frames_retries_after_blocking_io_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        process = SimpleNamespace(
            pid=999,
            stdout=SimpleNamespace(fileno=lambda: 10),
            stderr=SimpleNamespace(fileno=lambda: 11),
            returncode=None,
        )
        frame = contracts.encode(
            contracts.section_frame("configuration", contracts.observation("observed", "observed"))
        )
        reads = iter([BlockingIOError(), frame, b"", b""])

        class FakeSelector:
            def __init__(self) -> None:
                self._registered: dict[int, SimpleNamespace] = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def register(self, fileobj, _events, data) -> None:
                fd = fileobj.fileno()
                self._registered[fd] = SimpleNamespace(fileobj=fileobj, fd=fd, data=data)

            def unregister(self, fileobj) -> None:
                self._registered.pop(fileobj.fileno(), None)

            def get_map(self) -> dict[int, SimpleNamespace]:
                return self._registered

            def select(self, _timeout: float) -> list[tuple[SimpleNamespace, int]]:
                first = next(iter(self._registered.values()))
                return [(first, 1)]

        def fake_read(_fd: int, _size: int) -> bytes:
            item = next(reads)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(supervisor.selectors, "DefaultSelector", FakeSelector)
        monkeypatch.setattr(supervisor.os, "set_blocking", lambda *_args: None)
        monkeypatch.setattr(supervisor.os, "read", fake_read)
        monkeypatch.setattr(supervisor, "peek_status", lambda _proc: 0)

        received: list[bytes] = []
        code = supervisor.read_frames(process, time.monotonic() + 5, received.append)
        assert code == "observed"
        assert len(received) == 1


class TestUnifiedCliFlagsHelpAndActionableErrors:
    """Every owned CLI command exposes consistent --format/--json flags and complete help text."""

    @pytest.mark.parametrize(
        ("command_name", "command_cls"),
        [
            ("django_ray_doctor", doctor_cmd.Command),
            ("django_ray_lease_policy", lease_policy_cmd.Command),
            ("django_ray_protocol_status", protocol_status_cmd.Command),
            ("django_ray_runtime_env", runtime_env_cmd.Command),
            ("django_ray_worker_ready", worker_ready_cmd.Command),
            ("django_ray_audit_workflow_progress", audit_cmd.Command),
            ("django_ray_benchmark_workflow_progress", benchmark_cmd.Command),
        ],
    )
    def test_all_owned_commands_have_help_text_and_format_json_flags(
        self, command_name: str, command_cls: type
    ) -> None:
        parser = command_cls().create_parser("manage.py", command_name)
        custom_actions = [
            action
            for action in parser._actions
            if action.dest
            not in {
                "help",
                "version",
                "verbosity",
                "settings",
                "pythonpath",
                "traceback",
                "no_color",
                "force_color",
                "skip_checks",
            }
        ]
        assert custom_actions
        missing_help = [
            action.option_strings for action in custom_actions if not (action.help or "").strip()
        ]
        assert missing_help == [], f"{command_name} has flags missing help=: {missing_help}"

        option_flags = {opt for action in custom_actions for opt in action.option_strings}
        assert "--format" in option_flags, f"{command_name} missing --format"
        assert "--json" in option_flags, f"{command_name} missing --json"

    @pytest.mark.django_db(transaction=True)
    def test_lease_policy_text_and_json_formats_emit_actionable_remediation(self) -> None:
        text_out = StringIO()
        call_command("django_ray_lease_policy", "--format", "text", stdout=text_out)
        assert "Worker lease policy: matched" in text_out.getvalue()

        json_out = StringIO()
        call_command("django_ray_lease_policy", "--json", stdout=json_out)
        assert json.loads(json_out.getvalue())["status"] == "matched"

        # Actionable CommandError in text mode names exact transition flags.
        with pytest.raises(CommandError) as exc_info:
            call_command(
                "django_ray_lease_policy",
                "--format",
                "text",
                "--expected-seconds",
                "60",
            )
        assert exc_info.value.returncode == 2
        assert "lease_policy_transition_invalid" in str(exc_info.value)
        assert "--set-seconds" in str(exc_info.value)
        assert "--all-lease-users-stopped" in str(exc_info.value)

        # JSON mode preserves exact machine code in CommandError while writing
        # a secret-free Hint to stderr.
        stderr_out = StringIO()
        with pytest.raises(CommandError, match="^lease_policy_transition_invalid$"):
            call_command(
                "django_ray_lease_policy",
                "--format",
                "json",
                "--expected-seconds",
                "60",
                stderr=stderr_out,
            )
        assert "--set-seconds" in stderr_out.getvalue()

    @pytest.mark.django_db(transaction=True)
    def test_protocol_status_and_worker_ready_support_format_json_flag(self) -> None:
        proto_out = StringIO()
        call_command("django_ray_protocol_status", "--format", "json", stdout=proto_out)
        assert json.loads(proto_out.getvalue())["schema"] == "django-ray.protocol-status"

        ready_out = StringIO()
        with pytest.raises(CommandError) as exc_info:
            call_command(
                "django_ray_worker_ready",
                "--queue",
                "default",
                "--hostname",
                "host-a",
                "--format",
                "json",
                stdout=ready_out,
            )
        assert exc_info.value.returncode == 1
        assert json.loads(ready_out.getvalue())["status"] == "not_ready"

    def test_audit_and_benchmark_commands_support_json_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_audit = SimpleNamespace(
            topology_version=1,
            detail_revision=2,
            node_count=3,
            encoded_bytes=120,
            decoded_bytes=240,
            event_count=4,
            truncated_count=0,
            state_counts=(("SUCCEEDED", 3),),
        )
        monkeypatch.setattr(
            audit_cmd, "audit_workflow_progress_detail_storage", lambda *_a, **_k: fake_audit
        )
        audit_out = StringIO()
        call_command(
            "django_ray_audit_workflow_progress",
            "--task-execution-pk",
            "7",
            "--attempt-number",
            "1",
            "--execution-generation",
            "1",
            "--run-id",
            "00000000-0000-0000-0000-000000000007",
            "--format",
            "json",
            stdout=audit_out,
        )
        audit_payload = json.loads(audit_out.getvalue())
        assert audit_payload["status"] == "passed"
        assert audit_payload["nodes"] == 3
        assert audit_payload["states"] == {"SUCCEEDED": 3}

        bench_out = StringIO()
        call_command(
            "django_ray_benchmark_workflow_progress",
            "--nodes",
            "1",
            "--change-rates",
            "1.0",
            "--repetitions",
            "1",
            "--warmups",
            "0",
            "--json",
            stdout=bench_out,
        )
        bench_payload = json.loads(bench_out.getvalue())
        assert bench_payload["benchmark"] == "django-ray-workflow-progress-storage"


class TestRuntimeEnvMaintenanceFixes:
    """Verify F-066 fixes: plaintext dry-run, PG params/pool/SQL tx, and cursor progress."""

    def test_dry_run_and_check_work_on_default_plaintext_deployment(
        self, tmp_path, settings
    ) -> None:
        path = tmp_path / "plaintext.sqlite3"
        resolved = normalize_runtime_env({"env_vars": {"PLAIN": "ok"}})
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE django_ray_raytaskexecution (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT, runtime_env_profile TEXT, runtime_env_hash TEXT,
                    runtime_env_json TEXT, state TEXT, attempt_number INTEGER,
                    execution_generation INTEGER, claimed_by_worker TEXT, ray_job_id TEXT,
                    cancellation_status TEXT, cancellation_error TEXT)"""
            )
            conn.execute(
                """INSERT INTO django_ray_raytaskexecution
                   (task_id, runtime_env_profile, runtime_env_hash, runtime_env_json,
                    state, attempt_number, execution_generation)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("t-plain", None, resolved.digest, resolved.serialized, "QUEUED", 1, 0),
            )
        settings.DATABASES = {
            **settings.DATABASES,
            "maint_plain": {"ENGINE": "django.db.backends.sqlite3", "NAME": path},
        }
        settings.DJANGO_RAY = {
            "RUNTIME_ENV_STORAGE_MODE": "plaintext",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": None,
            "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": False,
        }

        out = StringIO()
        with pytest.raises(CommandError) as exc_info:
            call_command(
                "django_ray_runtime_env",
                "--database",
                "maint_plain",
                "--mode",
                "dry-run",
                "--format",
                "json",
                stdout=out,
            )
        assert exc_info.value.returncode == 2
        report = json.loads(out.getvalue())
        assert report["mode"] == "dry-run"
        assert report["counts"]["plaintext"] == 1
        assert report["processed"] == 1

        # Backward-compatible --format human alias remains supported.
        human_out = StringIO()
        with pytest.raises(CommandError) as exc_info:
            call_command(
                "django_ray_runtime_env",
                "--database",
                "maint_plain",
                "--mode",
                "dry-run",
                "--format",
                "human",
                stdout=human_out,
            )
        assert exc_info.value.returncode == 2
        assert "plaintext: 1" in human_out.getvalue()

    def test_postgresql_accepts_empty_or_none_port_host_and_pool_option(
        self, settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.DATABASES = {
            **settings.DATABASES,
            "pg_pool": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "appdb",
                "USER": None,
                "PASSWORD": "",
                "HOST": None,
                "PORT": "",
                "OPTIONS": {"pool": {"min_size": 2, "max_size": 8}, "sslmode": "prefer"},
            },
        }
        parsed = maintenance._database_settings("pg_pool")
        assert parsed["port"] == ""
        assert parsed["host"] == ""
        assert "pool" not in parsed["options"]
        assert parsed["options"]["sslmode"] == "prefer"

        captured: list[dict] = []

        def fake_connect(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(autocommit=False, close=lambda: None, rollback=lambda: None)

        monkeypatch.setattr(
            maintenance,
            "importlib",
            SimpleNamespace(
                import_module=lambda _name: SimpleNamespace(
                    Database=SimpleNamespace(connect=fake_connect)
                )
            ),
        )
        db = maintenance._Database(
            parsed,
            rewrite=False,
            deadline=time.monotonic() + 5,
            limits=maintenance.MaintenanceLimits(),
        )
        db.open()
        assert len(captured) == 1
        assert captured[0]["dbname"] == "appdb"
        assert "port" not in captured[0]
        assert "host" not in captured[0]
        assert "user" not in captured[0]
        assert "password" not in captured[0]
        assert "pool" not in captured[0]

    def test_postgresql_replace_uses_explicit_sql_commit_and_rollback(self) -> None:
        executed: list[str] = []

        class FakeCursor:
            rowcount = 1

            def execute(self, sql: str, params=()) -> None:
                del params
                executed.append(sql)

            def close(self) -> None:
                return None

        fake_conn = SimpleNamespace(
            autocommit=True,
            cursor=lambda: FakeCursor(),
            commit=lambda: pytest.fail(
                "PG autocommit mode must use SQL COMMIT, not connection.commit()"
            ),
            rollback=lambda: pytest.fail(
                "PG autocommit mode must use SQL ROLLBACK, not connection.rollback()"
            ),
        )
        db = maintenance._Database(
            {"engine": "postgresql", "name": "appdb", "options": {}},
            rewrite=True,
            deadline=time.monotonic() + 5,
            limits=maintenance.MaintenanceLimits(),
        )
        db.connection = fake_conn
        snap = maintenance._Snapshot(
            pk=1,
            values={"attempt_number": 1, "execution_generation": 0, "state": "QUEUED"},
            size=10,
            oversized=False,
        )
        assert db.replace(snap, '{"encrypted":true}') == "updated"
        assert executed[0] == "BEGIN"
        assert executed[1].startswith("UPDATE ")
        assert executed[2] == "COMMIT"

    def test_cursor_supports_bytes_secret_key(self, settings) -> None:
        settings.SECRET_KEY = b"binary-secret-key-for-cursor-tests"
        cursor = cursor_protocol.RuntimeEnvCursor(1, 10, "a" * 64, "dry-run")
        token = cursor_protocol.encode_runtime_env_cursor(cursor)
        assert cursor_protocol.decode_runtime_env_cursor(token) == cursor

    def test_maintenance_limits_errors_name_offending_flag(self) -> None:
        with pytest.raises(maintenance.MaintenanceError, match="--max-rows"):
            maintenance.MaintenanceLimits(rows=0)
        with pytest.raises(maintenance.MaintenanceError, match="--max-seconds"):
            maintenance.MaintenanceLimits(seconds=120)
