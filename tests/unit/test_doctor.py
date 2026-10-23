"""Resource-free contracts and isolated, owned SQLite observations for doctor."""

from __future__ import annotations

import base64
import copy
import json
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

from django_ray.doctor import contracts, probe, runner
from django_ray.models import RayTaskExecution, TaskInputPayload, TaskWorkerLeasePolicy


@pytest.mark.parametrize(
    "total,per_probe",
    [(float("inf"), 5), (15, float("nan")), (True, 5), (4, 3), (61, 5), (15, 21), (5, 5)],
)
def test_time_budgets_reject_nonfinite_or_missing_cleanup(total, per_probe):
    with pytest.raises(ValueError):
        contracts.Limits(total, per_probe)


def test_reports_remain_incomplete_with_explicit_remote_boundaries():
    sections = contracts.initial_sections()
    for name in contracts.DATABASE_SECTIONS + ("configuration",):
        sections[name] = contracts.observation("observed", "observed")
    report = contracts.result(sections, contracts.Limits(), 0.1, True)
    assert report["exit_code"] == 2 and not report["complete"]
    assert all(
        sections[name]["code"] == "not_implemented" for name in contracts.UNAVAILABLE_SECTIONS
    )
    for as_json in (True, False):
        assert len(contracts.render(report, as_json=as_json).encode()) < contracts.OUTPUT_BYTES
    sections["work"] = contracts.observation("blocker", "work_blockers", queued=1)
    assert contracts.result(sections, contracts.Limits(), 0, True)["exit_code"] == 1
    assert contracts.result(sections, contracts.Limits(), 0, False)["exit_code"] == 2


@pytest.mark.parametrize(
    "values",
    [
        {"secret": "secret"},
        {"vendor": "https://private.invalid"},
        {"total": -1},
        {"total": 2**63},
        {"runner": object()},
        {"package_version": "secret"},
    ],
)
def test_contract_rejects_unbounded_or_unsanitized_fields(values):
    with pytest.raises(ValueError):
        contracts.observation("observed", "observed", **values)


def test_frame_schema_rejects_extra_unknown_or_malformed_fields():
    good = contracts.section_frame(
        "database", contracts.observation("observed", "observed", vendor="sqlite")
    )
    assert contracts.parse_section(contracts.encode(good))[0] == "database"
    for value in (
        {**good, "extra": "secret"},
        {**good, "section": ["database"]},
        {**good, "kind": "private"},
    ):
        with pytest.raises(ValueError):
            contracts.parse_section(json.dumps(value).encode())
    with pytest.raises(ValueError):
        contracts.parse_section(b" " * (contracts.FRAME_BYTES + 1))


def test_nonlinux_and_unreproducible_settings_are_explicit(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "win32")
    assert (
        runner.diagnose(settings_module="example.settings")["sections"]["database"]["code"]
        == "linux_required"
    )
    monkeypatch.setattr(runner.sys, "platform", "linux")
    assert (
        runner.diagnose(settings_module=None)["sections"]["database"]["code"]
        == "settings_not_reproducible"
    )


@pytest.mark.parametrize("storage_mode", ["encrypted", "required"])
def test_configuration_shape_is_finite_and_secret_free(storage_mode):
    material = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
    config = {
        "RUNNER": "ray_core",
        "RUNTIME_ENV_STORAGE_MODE": storage_mode,
        "RUNTIME_ENV_ENCRYPTION_KEYS": {"private-key-name": material},
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "private-key-name",
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": "/private/location",
    }
    settings = SimpleNamespace(DJANGO_RAY=config)
    original = copy.deepcopy(config)
    result = probe.configuration(settings)
    assert result["outcome"] == result["code"] == "observed"
    assert result["values"]["storage_mode"] == storage_mode
    assert result["values"]["key_count"] == 1
    assert result["values"]["active_key_configured"] is True
    assert result["values"]["key_material_shape_valid"]
    assert config == original
    sections = contracts.initial_sections()
    sections["configuration"] = result
    report = contracts.result(sections, contracts.Limits(), 0, True)
    text = contracts.render(report, as_json=True)
    assert len(text.encode()) < contracts.OUTPUT_BYTES
    assert "private" not in text and material not in text


def test_required_storage_without_active_key_emits_only_fixed_configuration_diagnostic(monkeypatch):
    import django.conf

    material = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
    selected = SimpleNamespace(
        DJANGO_RAY={
            "RUNTIME_ENV_STORAGE_MODE": "required",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"private-key-name": material},
        }
    )
    with pytest.raises(probe.ProbeUnavailableError, match="^configuration_invalid$"):
        probe.configuration(selected)
    monkeypatch.setattr(django.conf, "Settings", lambda _name: selected)
    frames = []
    assert (
        probe.run(
            probe="configuration",
            settings_module="owned.settings",
            using="default",
            deadline=time.monotonic() + 5,
            emit=frames.append,
        )
        == 0
    )
    assert len(frames) == 1
    raw = contracts.encode(frames[0])
    name, value = contracts.parse_section(raw)
    assert name == "configuration"
    assert value["outcome"] == "blocker" and value["code"] == "configuration_invalid"
    assert value["values"] == {}
    assert b"private" not in raw and material.encode() not in raw


@pytest.mark.parametrize(
    "update",
    [
        {"RUNNER": []},
        {"INPUT_STORAGE_BACKEND": "callable"},
        {"INPUT_STORAGE_BACKEND": "filesystem"},
        {"RUNTIME_ENV_STORAGE_MODE": "unknown"},
        {"RUNTIME_ENV_ENCRYPTION_KEYS": {str(n): "a" * 43 for n in range(33)}},
        {"RUNTIME_ENV_ENCRYPTION_KEYS": {"bounded": "a" * 44}},
        {"RUNTIME_ENV_ENCRYPTION_KEYS": {"bad id": "a" * 43}},
        {"RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "missing"},
        {"RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "a" * 65},
        {"RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": "yes"},
    ],
)
def test_configuration_rejects_shapes_without_serializing_values(update):
    with pytest.raises(probe.ProbeUnavailableError, match="^configuration_invalid$"):
        probe.configuration(SimpleNamespace(DJANGO_RAY=update))


def test_secret_fallback_refuses_arbitrary_objects_without_calling_them():
    class Secret:
        def __str__(self):
            pytest.fail("Doctor must not coerce arbitrary secret objects")

    settings = SimpleNamespace(
        DJANGO_RAY={"RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": True}, SECRET_KEY=Secret()
    )
    with pytest.raises(probe.ProbeUnavailableError):
        probe.configuration(settings)


@pytest.mark.parametrize("raw", [[], {str(index): None for index in range(257)}])
def test_configuration_rejects_nonmapping_or_oversized_settings(raw):
    with pytest.raises(probe.ProbeUnavailableError, match="^configuration_invalid$"):
        probe.configuration(SimpleNamespace(DJANGO_RAY=raw))


def test_configuration_bounds_fallback_secret_count_without_serializing_them():
    selected = SimpleNamespace(
        DJANGO_RAY={"RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": True},
        SECRET_KEY="private",
        SECRET_KEY_FALLBACKS=["private"] * 9,
    )
    with pytest.raises(probe.ProbeUnavailableError, match="^configuration_invalid$"):
        probe.configuration(selected)


def test_postgresql_clone_preserves_original_and_enforces_query_bounds():
    original = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "owned",
            "OPTIONS": {"options": "-c search_path=public", "connect_timeout": 90},
        }
    }
    snapshot = copy.deepcopy(original)
    clone = probe.readonly_database(
        SimpleNamespace(DATABASES=original), "default", time.monotonic() + 2
    )
    assert original == snapshot
    assert clone["CONN_MAX_AGE"] == 0
    assert 1 <= clone["OPTIONS"]["connect_timeout"] <= 3
    assert clone["OPTIONS"]["options"].endswith(
        "-c default_transaction_read_only=on -c statement_timeout=1000 -c lock_timeout=250"
    )


@pytest.mark.parametrize(
    "database,code",
    [
        ({"ENGINE": "custom.backend"}, "database_unsupported"),
        ({"ENGINE": "django.db.backends.sqlite3", "NAME": object()}, "database_unsupported"),
        ({"ENGINE": "django.db.backends.sqlite3", "OPTIONS": []}, "database_unsupported"),
        (
            {"ENGINE": "django.db.backends.sqlite3", "NAME": "owned", "OPTIONS": {"uri": True}},
            "database_unsupported",
        ),
        (
            {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
            "memory_database_unsupported",
        ),
        (
            {"ENGINE": "django.db.backends.sqlite3", "NAME": "file:memory?mode=memory"},
            "memory_database_unsupported",
        ),
        (
            {"ENGINE": "django.db.backends.sqlite3", "NAME": "missing-doctor-database"},
            "database_unavailable",
        ),
        (
            {"ENGINE": "django.db.backends.postgresql", "OPTIONS": {"pool": True}},
            "database_unsupported",
        ),
        ({"ENGINE": "django.db.backends.postgresql", "HOST": object()}, "database_unsupported"),
        (
            {"ENGINE": "django.db.backends.postgresql", "OPTIONS": {"sslmode": object()}},
            "database_unsupported",
        ),
        (
            {"ENGINE": "django.db.backends.postgresql", "OPTIONS": {"options": 1}},
            "database_unsupported",
        ),
    ],
)
def test_database_clone_refuses_unsupported_connections(database, code):
    with pytest.raises(probe.ProbeUnavailableError, match=code):
        probe.readonly_database(
            SimpleNamespace(DATABASES={"default": database}), "default", time.monotonic() + 3
        )


def test_database_clone_refuses_missing_alias_directory_and_expired_connect_budget(tmp_path):
    for databases in ([], {"default": []}, {}):
        with pytest.raises(probe.ProbeUnavailableError, match="^database_unsupported$"):
            probe.readonly_database(
                SimpleNamespace(DATABASES=databases), "default", time.monotonic() + 3
            )
    with pytest.raises(probe.ProbeUnavailableError, match="^database_unavailable$"):
        probe.readonly_database(
            SimpleNamespace(
                DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": tmp_path}}
            ),
            "default",
            time.monotonic() + 3,
        )
    with pytest.raises(probe.ProbeUnavailableError, match="^probe_timeout$"):
        probe.readonly_database(
            SimpleNamespace(DATABASES={"default": {"ENGINE": "django.db.backends.postgresql"}}),
            "default",
            time.monotonic() - 1,
        )


@pytest.mark.parametrize(
    "settings_module,alias,remaining",
    [
        ("bad/settings", "default", 3),
        ("owned.settings", "bad/alias", 3),
        ("owned.settings", "default", -1),
        ("owned.settings", "default", 21),
        ("owned.settings", "default", float("nan")),
    ],
)
def test_private_probe_cli_refuses_invalid_identity_or_deadline_before_observing(
    monkeypatch, settings_module, alias, remaining
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--probe",
            "database",
            "--settings",
            settings_module,
            "--database=" + alias,
            "--deadline=" + str(time.monotonic() + remaining),
        ],
    )
    monkeypatch.setattr(probe, "run", lambda **_kwargs: pytest.fail("Invalid probe executed"))
    assert probe.main() == 2


def test_private_probe_cli_preserves_leading_dash_alias_and_emits_versioned_frame(monkeypatch):
    deadline = time.monotonic() + 3
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--probe",
            "configuration",
            "--settings",
            "owned.settings",
            "--database=-archive",
            "--deadline=" + str(deadline),
        ],
    )
    stream = BytesIO()
    monkeypatch.setattr(probe, "sys", SimpleNamespace(stdout=SimpleNamespace(buffer=stream)))

    def observe(**kwargs):
        assert kwargs["probe"] == "configuration" and kwargs["using"] == "-archive"
        assert kwargs["settings_module"] == "owned.settings" and kwargs["deadline"] == deadline
        kwargs["emit"](
            contracts.section_frame(
                "configuration", contracts.observation("observed", "observed", key_count=0)
            )
        )
        return 0

    monkeypatch.setattr(probe, "run", observe)
    assert probe.main() == 0
    name, value = contracts.parse_section(stream.getvalue())
    assert name == "configuration" and value["values"] == {"key_count": 0}


def test_query_and_row_budget_edges():
    budget = probe.QueryBudget(time.monotonic() + 5)
    for _ in range(contracts.SQL_STATEMENTS):
        budget.execute(lambda *_: None, "SELECT 1", (), False, {})
    with pytest.raises(probe.ProbeUnavailableError, match="query_budget_exhausted"):
        budget.execute(
            lambda *_: pytest.fail("Over-budget query executed"), "SELECT 1", (), False, {}
        )
    assert budget.returned(1, contracts.RETURNED_ROWS) == 1
    with pytest.raises(probe.ProbeUnavailableError, match="row_budget_exhausted"):
        budget.returned(1)
    with pytest.raises(probe.ProbeUnavailableError, match="probe_timeout"):
        probe.QueryBudget(time.monotonic() - 1).check()


@pytest.mark.parametrize(
    "field,code",
    [
        ("sqlstate", "57014"),
        ("sqlstate", "55P03"),
        ("sqlite_errorcode", 5),
        ("sqlite_errorcode", 9),
    ],
)
def test_database_budget_errors_use_driver_codes_without_raw_text(field, code):
    error = RuntimeError("private details must not appear")
    cause = RuntimeError("private driver details")
    setattr(cause, field, code)
    error.__cause__ = cause
    assert probe.database_error_code(error, time.monotonic() + 5) == "probe_timeout"


def test_missing_migration_recorder_preserves_connection_observation(owned_database):
    alias, path = owned_database
    with connections[alias].cursor() as cursor:
        cursor.execute("DROP TABLE django_migrations")
    source = SimpleNamespace(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}}
    )
    wrapper = ConnectionHandler(
        {"default": probe.readonly_database(source, "default", time.monotonic() + 5)}
    )["default"]
    wrapper.alias = alias
    original = connections[alias]
    connections[alias] = wrapper
    output = {}
    try:
        with pytest.raises(probe.ProbeUnavailableError, match="migrations_unavailable"):
            probe.collect_database(
                using=alias,
                budget=probe.QueryBudget(time.monotonic() + 5),
                config={"WORKER_LEASE_SECONDS": 60},
                emit=lambda name, value: output.update({name: value}),
            )
        assert output["database"]["outcome"] == "observed"
        assert wrapper.connection is None
    finally:
        connections[alias] = original


@pytest.fixture
def owned_database(tmp_path, django_db_blocker, transactional_db):
    alias = "doctor_owned_test"
    path = tmp_path / "owned.sqlite3"
    source = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}
    configured = ConnectionHandler({"default": source}).settings["default"]
    wrapper = ConnectionHandler({"default": configured})["default"]
    wrapper.alias = alias
    connections.databases[alias] = configured
    connections[alias] = wrapper
    with django_db_blocker.unblock():
        try:
            call_command("migrate", database=alias, verbosity=0, interactive=False)
            yield alias, path
        finally:
            connections[alias].close()
            del connections[alias]
            del connections.databases[alias]


def observe_owned(alias, path, *, config=None, output=None):
    source = SimpleNamespace(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}}
    )
    settings = probe.readonly_database(source, "default", time.monotonic() + 5)
    wrapper = ConnectionHandler({"default": settings})["default"]
    wrapper.alias = alias
    original = connections[alias]
    connections[alias] = wrapper
    output = {} if output is None else output
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


def permit_historical_fixture_rows(alias, table):
    """Seed pre-guard/malformed history only in this test's owned database."""
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=%s", [table]
        )
        for (name,) in cursor.fetchall():
            cursor.execute(f'DROP TRIGGER "{name}"')


def test_sqlite_observation_preserves_original_connection_callback_and_database(owned_database):
    alias, path = owned_database
    original = connections[alias]
    original.ensure_connection()
    callbacks = []
    original.connection.set_progress_handler(lambda: callbacks.append(True) and False, 1)
    before = path.read_bytes()
    report = observe_owned(alias, path)
    assert set(report) == set(contracts.DATABASE_SECTIONS)
    assert all(item["outcome"] == "observed" for item in report.values())
    assert report["database"]["values"]["read_only"] is True
    assert path.read_bytes() == before
    callbacks.clear()
    with original.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)
    assert callbacks
    original.connection.set_progress_handler(None, 0)


def test_doctor_reports_database_lease_duration_and_revision_without_repair(owned_database):
    alias, path = owned_database
    TaskWorkerLeasePolicy.objects.using(alias).update(lease_seconds=90, revision=7)
    before = path.read_bytes()
    report = observe_owned(alias, path, config={"WORKER_LEASE_SECONDS": 90})
    assert report["leases"]["outcome"] == report["leases"]["code"] == "observed"
    assert report["leases"]["values"]["lease_policy_seconds"] == 90
    assert report["leases"]["values"]["lease_policy_revision"] == 7
    assert set(report) == set(contracts.DATABASE_SECTIONS)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "fault,code",
    [
        ("mismatch", "lease_policy_mismatch"),
        ("missing", "lease_policy_invalid"),
        ("invalid-revision", "lease_policy_invalid"),
        ("unavailable", "lease_policy_unavailable"),
    ],
)
def test_doctor_refuses_unusable_lease_authority_before_worker_observations(
    owned_database, fault, code
):
    alias, path = owned_database
    original = connections[alias]
    if fault == "mismatch":
        TaskWorkerLeasePolicy.objects.using(alias).update(lease_seconds=90)
    elif fault == "missing":
        TaskWorkerLeasePolicy.objects.using(alias).all().delete()
    else:
        with original.cursor() as cursor:
            if fault == "invalid-revision":
                cursor.execute("PRAGMA ignore_check_constraints=ON")
                cursor.execute("UPDATE django_ray_taskworkerleasepolicy SET revision=0")
                cursor.execute("PRAGMA ignore_check_constraints=OFF")
            else:
                cursor.execute("DROP TABLE django_ray_taskworkerleasepolicy")
    before = path.read_bytes()
    completed = {}
    with pytest.raises(probe.ProbeUnavailableError, match=f"^{code}$") as captured:
        observe_owned(alias, path, output=completed)
    assert captured.value.code == code
    assert set(completed) == {"database", "migrations", "protocol"}
    assert all(value["outcome"] == "observed" for value in completed.values())
    sections = contracts.initial_sections()
    sections.update(completed)
    report = contracts.result(sections, contracts.Limits(), 0, True)
    assert report["exit_code"] == 2 and not report["complete"]
    assert report["sections"]["leases"]["values"] == {}
    assert report["sections"]["work"]["values"] == {}
    assert path.read_bytes() == before
    assert connections[alias] is original and original.connection is not None
    with original.cursor() as cursor:
        cursor.execute("PRAGMA query_only")
        assert cursor.fetchone() == (0,)


def test_sqlite_counts_never_materialize_payloads_and_detect_nul_states(owned_database):
    alias, path = owned_database
    now = datetime.now(UTC)
    execution = RayTaskExecution.objects.using(alias).create(
        task_id="private-task",
        callable_path="private.task",
        state="QUEUED",
        args_json=["secret" * 50000],
        kwargs_json={},
        runtime_env_json={},
    )
    TaskInputPayload.objects.using(alias).create(
        reference="private" * 10000,
        backend="filesystem",
        digest="a" * 64,
        size_bytes=0,
        envelope_version=1,
        state="ACTIVE\x00private",
        cleanup_error="private" * 30000,
    )
    RayTaskExecution.objects.using(alias).filter(pk=execution.pk).update(state="QUEUED\x00private")
    report = observe_owned(alias, path)
    assert report["work"]["values"]["invalid_state"] == 1
    assert report["registered_cleanup"]["values"]["invalid_state"] == 1
    assert report["registered_cleanup"]["values"]["cleanup_errors"] == 1
    assert "private" not in json.dumps(report) and len(json.dumps(report)) < 5000
    RayTaskExecution.objects.using(alias).filter(pk=execution.pk).update(
        state="QUEUED", queue_deadline_at=now - timedelta(seconds=1)
    )
    report = observe_owned(alias, path)
    assert report["work"]["values"]["queued_without_protocol_lease"] == 1
    assert report["work"]["values"]["queue_expired"] == 1


def test_sqlite_readonly_uri_refuses_mutation(owned_database):
    _alias, path = owned_database
    clone = probe.readonly_database(
        SimpleNamespace(
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": path}}
        ),
        "default",
        time.monotonic() + 2,
    )
    with sqlite3.connect(clone["NAME"], **clone["OPTIONS"]) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden(value int)")


def test_sqlite_progress_deadline_interrupts_real_query_and_closes_owned_wrapper(owned_database):
    alias, path = owned_database
    source = SimpleNamespace(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}}
    )
    wrapper = ConnectionHandler(
        {"default": probe.readonly_database(source, "default", time.monotonic() + 5)}
    )["default"]
    wrapper.alias = alias
    original = connections[alias]
    connections[alias] = wrapper
    output = {}

    def expensive_query(execute, sql, params, many, context):
        if sql.lstrip().startswith("SELECT") and "django_migrations" in sql:
            return execute(
                "WITH RECURSIVE items(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM items WHERE n<100000000) SELECT sum(n) FROM items",
                (),
                many,
                context,
            )
        return execute(sql, params, many, context)

    started = time.monotonic()
    try:
        with wrapper.execute_wrapper(expensive_query):
            with pytest.raises(probe.ProbeUnavailableError, match="probe_timeout"):
                probe.collect_database(
                    using=alias,
                    budget=probe.QueryBudget(started + 0.2),
                    config={"WORKER_LEASE_SECONDS": 60},
                    emit=lambda name, value: output.update({name: value}),
                )
        assert time.monotonic() - started < 2
        assert output["database"]["outcome"] == "observed"
        assert wrapper.connection is None
        with original.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
    finally:
        connections[alias] = original


def test_managed_command_does_not_run_checks_or_leak_exception_payload(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "linux")
    output = StringIO()
    with pytest.raises(CommandError) as error:
        call_command("django_ray_doctor", format="json", stdout=output)
    assert error.value.returncode == 2
    report = json.loads(output.getvalue())
    assert report["sections"]["database"]["code"] == "settings_not_reproducible"
    assert len(output.getvalue().encode()) < contracts.OUTPUT_BYTES


@pytest.mark.parametrize("state", ["RUNNING", "CANCELLING"])
@pytest.mark.parametrize("field", ["execution_protocol_version", "metadata_schema_version"])
def test_retired_nonterminal_protocol_or_metadata_is_a_blocker(owned_database, state, field):
    alias, path = owned_database
    permit_historical_fixture_rows(alias, "django_ray_raytaskexecution")
    with connections[alias].cursor() as cursor:
        cursor.execute("PRAGMA ignore_check_constraints=ON")
    RayTaskExecution.objects.using(alias).create(
        task_id="retired",
        callable_path="missing.task",
        state=state,
        last_heartbeat_at=datetime.now(UTC),
        **{field: 0},
    )
    with connections[alias].cursor() as cursor:
        cursor.execute("PRAGMA ignore_check_constraints=OFF")
    report = observe_owned(alias, path)
    assert report["work"]["outcome"] == "blocker"
    assert report["work"]["values"]["unsupported_protocol"] == 1
    assert report["work"]["values"]["active_without_heartbeat"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param(
            "min_supported_execution_protocol_version", "private" * 30000, id="malformed-minimum"
        ),
        pytest.param(
            "max_supported_execution_protocol_version", "private" * 30000, id="malformed-maximum"
        ),
        pytest.param("is_active", 2, id="malformed-active"),
    ],
)
def test_sqlite_noninteger_capability_fields_are_malformed_without_hydration(
    owned_database, field, value
):
    from django_ray.models import TaskWorkerLease

    alias, path = owned_database
    permit_historical_fixture_rows(alias, "django_ray_taskworkerlease")
    TaskWorkerLease.objects.using(alias).create(
        worker_id="private-worker",
        hostname="private-host",
        pid=1,
        capability_schema_version=1,
        django_ray_version="0.6.0",
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
    )
    with connections[alias].cursor() as cursor:
        cursor.execute("PRAGMA ignore_check_constraints=ON")
        cursor.execute(f'UPDATE django_ray_taskworkerlease SET "{field}" = %s', [value])
        cursor.execute("PRAGMA ignore_check_constraints=OFF")
    report = observe_owned(alias, path)
    assert report["leases"]["values"]["malformed"] == 1
    assert report["leases"]["values"]["live"] == 0
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "use_tz,zone", [(False, None), (True, "Europe/Warsaw"), (True, "America/Los_Angeles")]
)
def test_unreproducible_timestamp_semantics_are_unsupported(tmp_path, use_tz, zone):
    path = tmp_path / "owned.sqlite3"
    path.touch()
    source = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path), "TIME_ZONE": zone}
    with pytest.raises(probe.ProbeUnavailableError, match="database_unsupported"):
        probe.readonly_database(
            SimpleNamespace(DATABASES={"default": source}, USE_TZ=use_tz),
            "default",
            time.monotonic() + 3,
        )


@pytest.mark.parametrize("failure", ["migrations_unavailable", "probe_timeout", "late_close"])
def test_probe_retains_completed_sections_and_sanitizes_partial_or_late_failure(
    monkeypatch, failure
):
    import django
    import django.conf

    monkeypatch.setattr(django.conf, "Settings", lambda _name: SimpleNamespace())
    monkeypatch.setattr(probe, "readonly_database", lambda *_args: {})
    monkeypatch.setattr(django.conf, "settings", SimpleNamespace(configure=lambda **_kwargs: None))
    monkeypatch.setattr(django, "setup", lambda **_kwargs: None)

    def collect(*, emit, **_kwargs):
        for name in contracts.DATABASE_SECTIONS if failure == "late_close" else ("database",):
            emit(name, contracts.observation("observed", "observed"))
        if failure == "late_close":
            raise RuntimeError("private connection close details")
        raise probe.ProbeUnavailableError(failure)

    monkeypatch.setattr(probe, "collect_database", collect)
    frames = []
    status = probe.run(
        probe="database",
        settings_module="owned.settings",
        using="default",
        deadline=time.monotonic() + 5,
        emit=frames.append,
    )
    assert status == (2 if failure == "late_close" else 0)
    assert len(frames) == len(contracts.DATABASE_SECTIONS)
    assert frames[0]["observation"]["outcome"] == "observed"
    if failure != "late_close":
        assert frames[1]["observation"]["code"] == failure
        assert frames[1]["observation"]["outcome"] == (
            "budget_exhausted" if failure == "probe_timeout" else "unavailable"
        )
    assert "private" not in json.dumps(frames)


def test_probe_settings_import_failure_has_no_exception_text(monkeypatch):
    import django.conf

    def selected(_name):
        raise RuntimeError("private settings and credentials")

    monkeypatch.setattr(django.conf, "Settings", selected)
    frames = []
    assert (
        probe.run(
            probe="configuration",
            settings_module="owned.settings",
            using="default",
            deadline=time.monotonic() + 5,
            emit=frames.append,
        )
        == 0
    )
    assert frames[0]["observation"]["code"] == "settings_unavailable"
    assert "private" not in json.dumps(frames)


def test_standalone_entry_emits_finite_incomplete_report(monkeypatch, capsys):
    from django_ray.doctor import __main__ as entry

    monkeypatch.setattr(
        entry,
        "diagnose",
        lambda **_kwargs: contracts.result(
            contracts.initial_sections(), contracts.Limits(), 0, True
        ),
    )
    monkeypatch.setattr("sys.argv", ["doctor", "--settings", "owned.settings", "--format", "json"])
    assert entry.main() == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 2 and not output.err
