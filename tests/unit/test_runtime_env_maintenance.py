"""Owned-file SQLite and resource-free contracts for RuntimeEnv maintenance."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections

from django_ray.management import runtime_env_cursor as cursor_protocol
from django_ray.management import runtime_env_maintenance as maintenance
from django_ray.management.runtime_env_cursor import (
    InvalidRuntimeEnvCursorError,
    RuntimeEnvCursor,
    decode_runtime_env_cursor,
    encode_runtime_env_cursor,
)
from django_ray.models import TaskState
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    normalize_runtime_env,
    runtime_env_for_execution,
    runtime_env_for_storage,
)

_SECRET = "maintenance-plaintext-sentinel-aebfd"
_OLD = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
_NEW = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode()


def configuration(*, active="new", mode="encrypted", fallback=False):
    return {
        "RUNTIME_ENV_STORAGE_MODE": mode,
        "RUNTIME_ENV_ENCRYPTION_KEYS": {"old": _OLD, "new": _NEW},
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": active,
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": fallback,
    }


@pytest.fixture
def owned_database(tmp_path, settings):
    path = tmp_path / "maintenance.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE django_ray_raytaskexecution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, runtime_env_profile TEXT, runtime_env_hash TEXT,
            runtime_env_json TEXT, state TEXT, attempt_number INTEGER,
            execution_generation INTEGER, claimed_by_worker TEXT, ray_job_id TEXT,
            cancellation_status TEXT, cancellation_error TEXT,
            result_data TEXT, arbitrary_preserved TEXT)""")
    settings.DATABASES = {
        **settings.DATABASES,
        "maintenance": {"ENGINE": "django.db.backends.sqlite3", "NAME": path},
    }

    def add(*, config=None, **overrides):
        resolved = normalize_runtime_env({"env_vars": {"TOKEN": _SECRET}})
        values = {
            "task_id": "maintenance-task",
            "runtime_env_profile": None,
            "runtime_env_hash": resolved.digest,
            "runtime_env_json": resolved.serialized,
            "state": "QUEUED",
            "attempt_number": 2,
            "execution_generation": 7,
            "claimed_by_worker": None,
            "ray_job_id": None,
            "cancellation_status": None,
            "cancellation_error": None,
            "result_data": "retained-result",
            "arbitrary_preserved": "retained-state",
        }
        if config is not None:
            values["runtime_env_json"] = runtime_env_for_storage(
                resolved,
                task_id=values["task_id"],
                config=config,
            ).serialized
        values.update(overrides)
        with sqlite3.connect(path) as connection:
            cursor = connection.execute(
                f"INSERT INTO django_ray_raytaskexecution ({','.join(values)}) VALUES "
                f"({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
            return cursor.lastrowid

    def read(pk):
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM django_ray_raytaskexecution WHERE id=?", (pk,)
            ).fetchone()
            return dict(row) if row else None

    def mutate(pk, **values):
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE django_ray_raytaskexecution SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE id=?",
                (*values.values(), pk),
            )

    return SimpleNamespace(path=path, add=add, read=read, mutate=mutate)


def run(*, mode="rewrite", **options):
    return maintenance.maintain_runtime_env(
        using="maintenance",
        config=options.pop("config", configuration()),
        mode=mode,
        confirm=mode == "rewrite",
        **options,
    )


def test_plaintext_rewrite_preserves_every_other_column_and_is_idempotent(owned_database):
    pk = owned_database.add()
    before = owned_database.read(pk)
    report = run()
    after = owned_database.read(pk)
    assert report["exit_code"] == 0 and report["counts"]["rewritten"] == 1
    assert {k: v for k, v in before.items() if k != "runtime_env_json"} == {
        k: v for k, v in after.items() if k != "runtime_env_json"
    }
    assert _SECRET not in after["runtime_env_json"]
    decoded = runtime_env_for_execution(
        SimpleNamespace(pk=pk, **after), config=configuration(mode="required")
    )
    assert decoded.spec["env_vars"]["TOKEN"] == _SECRET
    assert run()["counts"]["current_key"] == 1
    assert owned_database.read(pk) == after


@pytest.mark.parametrize("state", TaskState.values)
def test_every_supported_retained_state_can_be_rewrapped(owned_database, state):
    pk = owned_database.add(config=configuration(active="old"), state=state)
    before = owned_database.read(pk)
    result = run()
    after = owned_database.read(pk)
    assert result["counts"]["rewrapped"] == 1
    assert after["state"] == before["state"]
    assert after["runtime_env_hash"] == before["runtime_env_hash"]
    assert json.loads(after["runtime_env_json"])["key_id"] == "new"


@pytest.mark.parametrize("mode", ["dry-run", "check"])
def test_observation_is_read_only_and_pending_rewrite_is_incomplete(
    owned_database, monkeypatch, mode
):
    pk = owned_database.add()
    before = owned_database.read(pk)
    original = maintenance._Database.query
    statements = []

    def query(self, sql, parameters=(), **kwargs):
        statements.append(sql)
        return original(self, sql, parameters, **kwargs)

    monkeypatch.setattr(maintenance._Database, "query", query)
    report = run(mode=mode)
    assert report["counts"]["plaintext"] == 1 and report["exit_code"] == 2
    assert owned_database.read(pk) == before
    assert not any(sql.startswith(("UPDATE", "INSERT", "DELETE", "BEGIN")) for sql in statements)
    assert "PRAGMA query_only=ON" in statements


def test_required_mode_maintenance_can_convert_plaintext(owned_database):
    pk = owned_database.add()
    config = configuration(mode="required")
    with pytest.raises(RuntimeEnvSnapshotError, match="plaintext is forbidden"):
        runtime_env_for_execution(SimpleNamespace(pk=pk, **owned_database.read(pk)), config=config)
    assert run(config=config)["counts"]["rewritten"] == 1
    runtime_env_for_execution(SimpleNamespace(pk=pk, **owned_database.read(pk)), config=config)


def test_django_secret_fallback_with_same_key_id_is_rewrapped(owned_database, settings):
    settings.SECRET_KEY = "old-django-key"
    config = configuration(active="django-secret", fallback=True)
    pk = owned_database.add(config=config)
    before = owned_database.read(pk)
    settings.SECRET_KEY = "current-django-key"
    settings.SECRET_KEY_FALLBACKS = ["old-django-key"]
    assert run(config=config)["counts"]["rewrapped"] == 1
    after = owned_database.read(pk)
    assert before["runtime_env_hash"] == after["runtime_env_hash"]
    assert before["runtime_env_json"] != after["runtime_env_json"]
    settings.SECRET_KEY_FALLBACKS = []
    assert run(config=config)["counts"]["current_key"] == 1


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"runtime_env_hash": ""}, "missing_identity"),
        ({"runtime_env_profile": ""}, "missing_identity"),
        ({"task_id": "unsafe task"}, "missing_identity"),
        ({"runtime_env_json": "malformed-private-content"}, "invalid_snapshot"),
        ({"runtime_env_hash": "a" * 64}, "invalid_snapshot"),
        ({"runtime_env_hash": None}, "invalid_snapshot"),
        ({"runtime_env_json": sqlite3.Binary(b"private-blob")}, "row_too_large"),
        ({"state": "UNKNOWN"}, "invalid_snapshot"),
        ({"attempt_number": 0}, "invalid_snapshot"),
        ({"execution_generation": -1}, "invalid_snapshot"),
        ({"task_id": "x" * 256}, "row_too_large"),
        ({"runtime_env_profile": "é" * 51}, "row_too_large"),
        ({"cancellation_error": "x" * 4097}, "row_too_large"),
        ({"runtime_env_json": "x" * 65537}, "row_too_large"),
    ],
)
def test_invalid_or_oversized_rows_fail_closed_without_mutation(owned_database, changes, code):
    pk = owned_database.add(**changes)
    before = owned_database.read(pk)
    report = run()
    assert report["exit_code"] == 1 and report["counts"][code] == 1
    assert owned_database.read(pk) == before
    assert "malformed-private-content" not in json.dumps(report)


def test_unknown_keys_and_tampering_are_separate_fixed_outcomes(owned_database):
    missing = owned_database.add(config=configuration(active="old"))
    corrupted = owned_database.add(config=configuration(active="old"))
    missing_value = json.loads(owned_database.read(missing)["runtime_env_json"])
    missing_value["key_id"] = "unknown-private-key-identifier"
    owned_database.mutate(missing, runtime_env_json=json.dumps(missing_value))
    corrupted_value = json.loads(owned_database.read(corrupted)["runtime_env_json"])
    corrupted_value["ciphertext"] = corrupted_value["ciphertext"][:-3] + "AAA"
    owned_database.mutate(corrupted, runtime_env_json=json.dumps(corrupted_value))
    before = [owned_database.read(pk) for pk in (missing, corrupted)]
    report = run()
    assert report["counts"]["unavailable_key"] == report["counts"]["invalid_snapshot"] == 1
    assert [owned_database.read(pk) for pk in (missing, corrupted)] == before
    assert "unknown-private-key-identifier" not in json.dumps(report)


def test_high_water_cursor_pages_do_not_claim_new_inserts(owned_database):
    first, second = owned_database.add(), owned_database.add()
    limits = maintenance.MaintenanceLimits(rows=1)
    initial = run(limits=limits)
    assert initial["counts"]["rewritten"] == 1 and initial["has_more"]
    third = owned_database.add()
    result = run(limits=limits, cursor=initial["next_cursor"])
    assert result["exit_code"] == 0 and result["high_water"] == second
    assert _SECRET in owned_database.read(third)["runtime_env_json"]
    assert result["fresh_pass_required"] and not result["key_retirement_authorized"]
    assert result["backup_obligations"] == "not_verified"
    assert run()["counts"]["rewritten"] == 1
    assert first < second < third


@pytest.mark.parametrize("change", ["mode", "key", "database"])
def test_cursor_is_bound_to_policy_mode_and_database(owned_database, settings, change):
    owned_database.add()
    owned_database.add()
    report = run(limits=maintenance.MaintenanceLimits(rows=1))
    options = {"cursor": report["next_cursor"]}
    if change == "mode":
        options["mode"] = "check"
    elif change == "key":
        options["config"] = configuration(active="old")
    else:
        settings.DATABASES["maintenance"]["NAME"] = owned_database.path.parent / "other.sqlite3"
        with sqlite3.connect(settings.DATABASES["maintenance"]["NAME"]):
            pass
    with pytest.raises(maintenance.MaintenanceError, match="cursor"):
        run(**options)


@pytest.mark.parametrize("token", ["", "x" * 1025, "é", "AAAA", "not-a-token!"])
def test_cursor_rejects_unbounded_or_malformed_tokens(token):
    with pytest.raises(InvalidRuntimeEnvCursorError):
        decode_runtime_env_cursor(token)


def test_cursor_encryption_hides_policy_and_rejects_tampering():
    cursor = RuntimeEnvCursor(1, 2, "a" * 64, "check")
    token = encode_runtime_env_cursor(cursor)
    assert decode_runtime_env_cursor(token) == cursor
    assert "a" * 64 not in token
    with pytest.raises(InvalidRuntimeEnvCursorError):
        decode_runtime_env_cursor(token[:-4] + "AAAA")


@pytest.mark.parametrize(
    "change",
    [
        {"attempt_number": 3},
        {"execution_generation": 8},
        {"state": "CANCELLED"},
        {"cancellation_status": "REQUESTED"},
        {"cancellation_error": "changed"},
        {"claimed_by_worker": "other"},
        {"runtime_env_hash": "b" * 64},
    ],
)
def test_exact_cas_loses_to_concurrent_lifecycle_or_identity_change(
    owned_database, monkeypatch, change
):
    pk = owned_database.add()
    original = maintenance._Database.replace

    def racing(self, snapshot, envelope):
        owned_database.mutate(pk, **change)
        return original(self, snapshot, envelope)

    monkeypatch.setattr(maintenance._Database, "replace", racing)
    report = run()
    assert report["counts"]["changed"] == 1 and report["exit_code"] == 2
    after = owned_database.read(pk)
    assert _SECRET in after["runtime_env_json"]
    assert all(after[key] == value for key, value in change.items())


def test_two_rewriters_cannot_replace_the_winning_envelope(owned_database, monkeypatch):
    pk = owned_database.add()
    original = maintenance._Database.replace
    winner = []

    def racing(self, snapshot, envelope):
        monkeypatch.setattr(maintenance._Database, "replace", original)
        assert run()["counts"]["rewritten"] == 1
        winner.append(owned_database.read(pk)["runtime_env_json"])
        return original(self, snapshot, envelope)

    monkeypatch.setattr(maintenance._Database, "replace", racing)
    report = run()
    assert report["counts"]["changed"] == 1
    assert owned_database.read(pk)["runtime_env_json"] == winner[0]


def test_commit_unknown_retains_resume_boundary_and_survives_rollback_failure(
    owned_database, monkeypatch
):
    pk = owned_database.add()
    original = maintenance._Database.open

    class UncertainCommit:
        def __init__(self, connection):
            self.inner = connection

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def commit(self):
            self.inner.commit()
            raise OSError(_SECRET)

        def rollback(self):
            raise OSError(_SECRET)

    def opened(self):
        original(self)
        self.connection = UncertainCommit(self.connection)

    monkeypatch.setattr(maintenance._Database, "open", opened)
    report = run()
    assert report["counts"]["commit_uncertain"] == 1
    assert report["counts"]["rewritten"] == 0 and report["after"] == 0
    assert report["next_cursor"] and not report["cleanup_confirmed"]
    assert _SECRET not in json.dumps(report)
    monkeypatch.setattr(maintenance._Database, "open", original)
    resumed = run(cursor=report["next_cursor"])
    assert resumed["counts"]["current_key"] == 1 and resumed["exit_code"] == 0
    assert _SECRET not in owned_database.read(pk)["runtime_env_json"]


def test_total_byte_budget_prevents_snapshot_hydration(owned_database, monkeypatch):
    owned_database.add()
    monkeypatch.setattr(
        maintenance, "_inspect", lambda *args: pytest.fail("snapshot must not be hydrated")
    )
    report = run(limits=maintenance.MaintenanceLimits(total_bytes=10))
    assert report["processed"] == report["processed_bytes"] == 0
    assert report["budget_exhausted"] == "total_bytes" and report["next_cursor"]


def test_output_and_details_remain_bounded(owned_database):
    for _ in range(9):
        owned_database.add()
    report = run(limits=maintenance.MaintenanceLimits(details=2))
    assert len(report["details"]) == 2 and report["details_omitted"] == 7
    assert len(json.dumps(report).encode()) < maintenance.OUTPUT_BYTES - 256


def test_debug_logging_never_sees_snapshot_or_cas_parameters(
    owned_database, settings, caplog, monkeypatch
):
    settings.DEBUG = True
    pk = owned_database.add()
    before_queries = list(connections["default"].queries_log)
    caplog.set_level(logging.DEBUG, logger="django.db.backends")
    assert run()["exit_code"] == 0
    encrypted = owned_database.read(pk)["runtime_env_json"]
    owned_database.add()
    original = maintenance._Database.query

    def fail_update(self, sql, parameters=(), **kwargs):
        if sql.startswith("UPDATE"):
            raise RuntimeError(_SECRET + encrypted)
        return original(self, sql, parameters, **kwargs)

    monkeypatch.setattr(maintenance._Database, "query", fail_update)
    report = run()
    combined = caplog.text + json.dumps(report) + str(list(connections["default"].queries_log))
    assert _SECRET not in combined and encrypted not in combined
    assert list(connections["default"].queries_log) == before_queries
    assert report["counts"]["database_unavailable"] == 1


def test_command_json_contains_policy_boundary_and_sanitized_incomplete_status(
    owned_database, settings
):
    owned_database.add()
    settings.DJANGO_RAY = configuration()
    output = StringIO()
    with pytest.raises(CommandError) as error:
        call_command("django_ray_runtime_env", database="maintenance", format="json", stdout=output)
    assert error.value.returncode == 2
    report = json.loads(output.getvalue())
    assert report["mode"] == "dry-run" and report["counts"]["plaintext"] == 1
    assert report["limits"]["time_contract"] == "cooperative"
    assert not report["key_retirement_authorized"] and _SECRET not in output.getvalue()


@pytest.mark.parametrize(
    "changes",
    [
        {"rows": 0},
        {"rows": 1001},
        {"rows": True},
        {"snapshot_bytes": 1048577},
        {"total_bytes": 8388609},
        {"seconds": float("nan")},
        {"seconds": 61},
        {"details": 101},
    ],
)
def test_invalid_budgets_fail_before_database_work(changes):
    with pytest.raises(maintenance.MaintenanceError, match="budget"):
        maintenance.MaintenanceLimits(**changes)


def test_mutation_requires_confirmation_and_encrypted_write_policy(owned_database):
    owned_database.add()
    with pytest.raises(maintenance.MaintenanceError, match="confirm"):
        maintenance.maintain_runtime_env(
            using="maintenance", config=configuration(), mode="rewrite"
        )
    with pytest.raises(maintenance.MaintenanceError, match="storage mode"):
        run(config=configuration(mode="plaintext"))


def test_connection_failure_emits_no_false_empty_range_cursor(owned_database, monkeypatch):
    owned_database.add()
    monkeypatch.setattr(
        maintenance._Database, "open", lambda _: (_ for _ in ()).throw(OSError(_SECRET))
    )
    report = run()
    assert report["exit_code"] == 2 and report["next_cursor"] is None
    assert report["counts"]["database_unavailable"] == 1
    assert _SECRET not in json.dumps(report)


def test_interruption_after_update_rolls_back_and_resumes_without_losing_row(
    owned_database, monkeypatch
):
    pk = owned_database.add()
    before = owned_database.read(pk)
    original = maintenance._Database.query

    def interrupted(self, sql, parameters=(), **kwargs):
        result = original(self, sql, parameters, **kwargs)
        if sql.startswith("UPDATE"):
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(maintenance._Database, "query", interrupted)
    report = run()
    assert report["counts"]["interrupted"] == 1 and report["after"] == 0
    assert report["cleanup_confirmed"] and owned_database.read(pk) == before
    monkeypatch.setattr(maintenance._Database, "query", original)
    assert run(cursor=report["next_cursor"])["counts"]["rewritten"] == 1


def test_elapsed_budget_before_write_preserves_envelope(owned_database, monkeypatch):
    pk = owned_database.add()
    before = owned_database.read(pk)

    def expired(*_args):
        raise maintenance._BudgetExpiredError

    monkeypatch.setattr(maintenance._Database, "replace", expired)
    report = run()
    assert report["budget_exhausted"] == "elapsed" and report["after"] == 0
    assert report["counts"]["rewritten"] == 0 and owned_database.read(pk) == before


def test_delete_between_read_and_rewrite_does_not_resurrect_a_row(owned_database, monkeypatch):
    pk = owned_database.add()
    original = maintenance._Database.replace

    def deleted(self, snapshot, envelope):
        with sqlite3.connect(owned_database.path) as connection:
            connection.execute("DELETE FROM django_ray_raytaskexecution WHERE id=?", (pk,))
        return original(self, snapshot, envelope)

    monkeypatch.setattr(maintenance._Database, "replace", deleted)
    assert run()["counts"]["changed"] == 1
    assert owned_database.read(pk) is None


def test_readonly_driver_rejects_writes_even_when_called_directly(owned_database, monkeypatch):
    pk = owned_database.add()
    before = owned_database.read(pk)
    original = maintenance._Database.open

    def opened(self):
        original(self)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            self.query("UPDATE django_ray_raytaskexecution SET runtime_env_json='not allowed'")

    monkeypatch.setattr(maintenance._Database, "open", opened)
    assert run(mode="check")["counts"]["plaintext"] == 1
    assert owned_database.read(pk) == before


def test_database_options_are_part_of_cursor_policy(owned_database):
    config, encryption = maintenance._configuration(configuration(), "check")
    del config
    original = {"engine": "postgresql", "name": "db", "options": {"options": "-c search_path=one"}}
    changed = {**original, "options": {"options": "-c search_path=two"}}
    assert maintenance._policy(original, encryption, {}) != maintenance._policy(
        changed, encryption, {}
    )


def test_sqlite_numeric_identity_rejects_unbounded_text_without_hydration(
    owned_database, monkeypatch
):
    owned_database.add(attempt_number="non-numeric-private-value" * 10000)
    original = maintenance._inspect
    observed = []

    def inspect(snapshot, *args):
        observed.append(snapshot.values["attempt_number"])
        return original(snapshot, *args)

    monkeypatch.setattr(maintenance, "_inspect", inspect)
    report = run()
    assert observed == [None] and report["counts"]["invalid_snapshot"] == 1
    assert "non-numeric-private-value" not in json.dumps(report)


@pytest.mark.parametrize(
    "changes,fallbacks",
    [
        ({"RUNTIME_ENV_ENCRYPTION_KEYS": []}, []),
        ({"RUNTIME_ENV_ENCRYPTION_KEYS": {str(i): _NEW for i in range(33)}}, []),
        ({"RUNTIME_ENV_ENCRYPTION_KEYS": {"x" * 65: _NEW}}, []),
        ({"RUNTIME_ENV_ENCRYPTION_KEYS": {"new": _SECRET}}, []),
        ({"RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "missing-private-key"}, []),
        ({}, "private-fallback"),
        ({}, ["private-fallback"] * 9),
        ({}, ["x" * 4097]),
        ({}, [None]),
    ],
)
def test_unsupported_key_configuration_is_refused_before_connecting(
    owned_database, settings, monkeypatch, changes, fallbacks
):
    pk = owned_database.add()
    before = owned_database.read(pk)
    settings.SECRET_KEY_FALLBACKS = fallbacks
    monkeypatch.setattr(
        maintenance._Database,
        "open",
        lambda _: pytest.fail("configuration must precede connection"),
    )
    with pytest.raises(maintenance.MaintenanceError) as error:
        run(config={**configuration(), **changes})
    assert str(error.value) == "Invalid RuntimeEnv maintenance encryption configuration."
    assert owned_database.read(pk) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"ENGINE": "django.db.backends.mysql"},
        {"NAME": None},
        {"NAME": "x" * 4097},
        {"NAME": ":memory:"},
        {"NAME": "file:private-location?mode=rw"},
        {"OPTIONS": []},
        {"OPTIONS": {str(i): 1 for i in range(17)}},
        {"OPTIONS": {"uri": True}},
        {"ENGINE": "django.db.backends.postgresql", "OPTIONS": {"service": _SECRET}},
        {"ENGINE": "django.db.backends.postgresql", "NAME": None},
        {"ENGINE": "django.db.backends.postgresql", "NAME": "db", "PASSWORD": "x" * 4097},
        {"ENGINE": "django.db.backends.postgresql", "NAME": "db", "OPTIONS": {"sslmode": []}},
        {"ENGINE": "django.db.backends.postgresql", "NAME": "db", "OPTIONS": {"options": 1}},
    ],
)
def test_unsupported_database_configuration_never_opens_a_connection(
    owned_database, settings, monkeypatch, changes
):
    pk = owned_database.add()
    before = owned_database.read(pk)
    settings.DATABASES["maintenance"] = {**settings.DATABASES["maintenance"], **changes}
    monkeypatch.setattr(
        maintenance._Database, "open", lambda _: pytest.fail("unsupported database must not open")
    )
    with pytest.raises(maintenance.MaintenanceError) as error:
        run()
    assert str(error.value) == "Unsupported RuntimeEnv maintenance database."
    assert owned_database.read(pk) == before


@pytest.mark.parametrize("using", [None, "", "a" * 101, "unknown"])
def test_invalid_database_alias_is_a_fixed_refusal(using):
    with pytest.raises(maintenance.MaintenanceError) as error:
        maintenance.maintain_runtime_env(using=using, config=configuration())
    assert str(error.value) == "Unsupported RuntimeEnv maintenance database."


@pytest.mark.parametrize("directory", [False, True])
def test_unavailable_sqlite_path_does_not_create_a_database(owned_database, settings, directory):
    path = (
        owned_database.path.parent if directory else owned_database.path.parent / "absent.sqlite3"
    )
    settings.DATABASES["maintenance"]["NAME"] = path
    with pytest.raises(maintenance.MaintenanceError) as error:
        run()
    assert str(error.value) == "RuntimeEnv maintenance database is unavailable."
    assert path.is_dir() if directory else not path.exists()


@pytest.mark.parametrize("mode", ["dry-run", "check", "rewrite"])
def test_postgresql_connection_failure_keeps_private_details_out_of_report_and_bounds_driver(
    owned_database, settings, monkeypatch, mode
):
    pk = owned_database.add()
    before = owned_database.read(pk)
    settings.DATABASES["maintenance"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "maintenance-db",
        "PASSWORD": _SECRET,
        "OPTIONS": {"options": "-c search_path=maintenance", "connect_timeout": 600},
    }
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        raise OSError(_SECRET)

    def driver_module(name):
        assert name == "django.db.backends.postgresql.base"
        return SimpleNamespace(Database=SimpleNamespace(connect=connect))

    monkeypatch.setattr(maintenance, "importlib", SimpleNamespace(import_module=driver_module))
    report = run(mode=mode)
    assert len(calls) == 1
    assert calls[0]["connect_timeout"] == 3
    assert calls[0]["options"].startswith("-c search_path=maintenance ")
    assert "-c statement_timeout=1000 -c lock_timeout=250" in calls[0]["options"]
    assert ("default_transaction_read_only=on" in calls[0]["options"]) == (mode != "rewrite")
    assert report["counts"]["database_unavailable"] == 1 and report["exit_code"] == 2
    assert report["next_cursor"] is None and report["cleanup_confirmed"]
    assert _SECRET not in json.dumps(report) and owned_database.read(pk) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"v": 2},
        {"v": True},
        {"extra": _SECRET},
        {"after": True},
        {"after": -1},
        {"after": 3},
        {"high_water": 1 << 63},
        {"policy": "g" * 64},
        {"mode": "unknown"},
    ],
)
def test_authenticated_cursor_with_unsupported_payload_is_refused(changes):
    payload = {"v": 1, "after": 1, "high_water": 2, "policy": "a" * 64, "mode": "check"}
    payload.update(changes)
    nonce = b"cursor-test!"
    ciphertext = cursor_protocol._cipher().encrypt(
        nonce, json.dumps(payload, separators=(",", ":")).encode(), cursor_protocol._CONTEXT
    )
    token = base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode()
    with pytest.raises(InvalidRuntimeEnvCursorError) as error:
        decode_runtime_env_cursor(token)
    assert not str(error.value)


@pytest.mark.parametrize("duplicate", [False, True])
def test_authenticated_cursor_refuses_noncanonical_or_duplicate_fields(duplicate):
    payload = '{"v":1,"after":1,"high_water":2,"policy":"' + "a" * 64 + '","mode":"check"}'
    plaintext = payload.replace('"v":1,', '"v":1,"v":1,') if duplicate else " " + payload
    nonce = b"cursor-test!"
    ciphertext = cursor_protocol._cipher().encrypt(
        nonce, plaintext.encode(), cursor_protocol._CONTEXT
    )
    token = base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode()
    with pytest.raises(InvalidRuntimeEnvCursorError):
        decode_runtime_env_cursor(token)


def test_cursor_encoder_refuses_invalid_range_and_unbounded_secret(settings):
    with pytest.raises(InvalidRuntimeEnvCursorError):
        encode_runtime_env_cursor(RuntimeEnvCursor(3, 2, "a" * 64, "check"))
    settings.SECRET_KEY = _SECRET * 200
    with pytest.raises(InvalidRuntimeEnvCursorError) as error:
        encode_runtime_env_cursor(RuntimeEnvCursor(1, 2, "a" * 64, "check"))
    assert not str(error.value)


@pytest.mark.parametrize("options", [{"max_rows": 0}, {"mode": "check", "confirm": True}])
def test_command_refusal_has_no_partial_output_or_mutation(owned_database, settings, options):
    pk = owned_database.add()
    before = owned_database.read(pk)
    settings.DJANGO_RAY = configuration()
    output = StringIO()
    with pytest.raises(CommandError) as error:
        call_command("django_ray_runtime_env", database="maintenance", stdout=output, **options)
    assert error.value.returncode == 1 and _SECRET not in str(error.value)
    assert output.getvalue() == "" and owned_database.read(pk) == before


def test_human_command_continuation_reports_bounded_progress_and_resumes(owned_database, settings):
    first, second = owned_database.add(), owned_database.add()
    settings.DJANGO_RAY = configuration()
    output = StringIO()
    with pytest.raises(CommandError) as error:
        call_command(
            "django_ray_runtime_env",
            database="maintenance",
            mode="rewrite",
            confirm=True,
            max_rows=1,
            stdout=output,
        )
    rendered = output.getvalue()
    assert error.value.returncode == 2
    assert "processed 1 row(s)" in rendered and "Range complete: False; exit: 2." in rendered
    assert f"pk={first} outcome=rewritten" in rendered and "rewritten: 1" in rendered
    assert "backups not verified; key retirement not authorized" in rendered
    assert "not a hard wall-clock deadline" in rendered and _SECRET not in rendered
    token = next(
        line.removeprefix("Next cursor: ")
        for line in rendered.splitlines()
        if line.startswith("Next cursor: ")
    )
    assert decode_runtime_env_cursor(token).after == first
    resumed = StringIO()
    call_command(
        "django_ray_runtime_env",
        database="maintenance",
        mode="rewrite",
        confirm=True,
        cursor=token,
        stdout=resumed,
    )
    assert "Range complete: True; exit: 0." in resumed.getvalue()
    assert f"pk={second} outcome=rewritten" in resumed.getvalue()
    assert "Next cursor:" not in resumed.getvalue()


def test_invalid_high_water_identity_is_not_an_empty_success(owned_database):
    pk = owned_database.add(id=-1)
    before = owned_database.read(pk)
    with pytest.raises(maintenance.MaintenanceError, match="row identity"):
        run()
    assert owned_database.read(pk) == before


def test_delete_before_hydration_is_visible_and_never_resurrected(owned_database, monkeypatch):
    pk = owned_database.add()
    original = maintenance._Database.read

    def deleted(self, identity, remaining):
        with sqlite3.connect(owned_database.path) as connection:
            connection.execute("DELETE FROM django_ray_raytaskexecution WHERE id=?", (pk,))
        return original(self, identity, remaining)

    monkeypatch.setattr(maintenance._Database, "read", deleted)
    report = run()
    assert report["counts"]["missing"] == 1 and report["exit_code"] == 2
    assert report["processed_bytes"] == 0 and report["after"] == pk
    assert owned_database.read(pk) is None


def test_expired_budget_before_connecting_reports_no_known_range(owned_database, monkeypatch):
    pk = owned_database.add()
    before = owned_database.read(pk)
    ticks = iter([0, 61])
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    report = run()
    assert report["budget_exhausted"] == "elapsed" and report["exit_code"] == 2
    assert report["processed"] == report["sql_statements"] == 0
    assert report["next_cursor"] is None and owned_database.read(pk) == before


def test_total_bytes_reserves_encryption_work_before_mutation(owned_database):
    pk = owned_database.add()
    before = owned_database.read(pk)
    read_size = run(mode="check")["processed_bytes"]
    report = run(limits=maintenance.MaintenanceLimits(total_bytes=read_size + 1))
    assert report["budget_exhausted"] == "total_bytes" and report["exit_code"] == 2
    assert report["processed_bytes"] == read_size and report["processed"] == report["after"] == 0
    assert owned_database.read(pk) == before
    assert run(cursor=report["next_cursor"])["counts"]["rewritten"] == 1


def test_envelope_expansion_cannot_exceed_snapshot_cap(owned_database):
    pk = owned_database.add()
    before = owned_database.read(pk)
    limits = maintenance.MaintenanceLimits(snapshot_bytes=len(before["runtime_env_json"].encode()))
    report = run(limits=limits)
    assert report["counts"]["row_too_large"] == 1 and report["exit_code"] == 1
    assert owned_database.read(pk) == before


def test_encryption_failure_preserves_plaintext_and_has_fixed_public_outcome(
    owned_database, monkeypatch
):
    pk = owned_database.add()
    before = owned_database.read(pk)

    def failed(*args, **kwargs):
        raise maintenance.RuntimeEnvEncryptionError(_SECRET)

    with monkeypatch.context() as patch:
        patch.setattr(maintenance, "protect_runtime_env_snapshot", failed)
        report = run()
    assert report["counts"]["encryption_failed"] == 1 and report["exit_code"] == 1
    assert owned_database.read(pk) == before and _SECRET not in json.dumps(report)
    assert run()["counts"]["rewritten"] == 1


def test_close_failure_marks_committed_result_incomplete_without_disclosing_error(
    owned_database, monkeypatch
):
    pk = owned_database.add()
    original = maintenance._Database.open

    class CloseFailure:
        def __init__(self, connection):
            self.inner = connection

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            self.inner.close()
            raise OSError(_SECRET)

    def opened(self):
        original(self)
        self.connection = CloseFailure(self.connection)

    with monkeypatch.context() as patch:
        patch.setattr(maintenance._Database, "open", opened)
        report = run()
    assert report["counts"]["rewritten"] == 1 and report["exit_code"] == 2
    assert not report["cleanup_confirmed"] and _SECRET not in json.dumps(report)
    assert _SECRET not in owned_database.read(pk)["runtime_env_json"]
    assert run()["counts"]["current_key"] == 1


def test_cursor_provider_failure_requires_fresh_pass_without_losing_committed_rows(
    owned_database, monkeypatch
):
    first, second = owned_database.add(), owned_database.add()

    def failed(_cursor):
        raise InvalidRuntimeEnvCursorError(_SECRET)

    with monkeypatch.context() as patch:
        patch.setattr(maintenance, "encode_runtime_env_cursor", failed)
        report = run(limits=maintenance.MaintenanceLimits(rows=1))
    assert report["stop_code"] == "cursor_unavailable" and report["exit_code"] == 2
    assert report["next_cursor"] is None and report["after"] == first
    assert report["counts"]["rewritten"] == 1 and _SECRET not in json.dumps(report)
    assert _SECRET in owned_database.read(second)["runtime_env_json"]
    restarted = run()
    assert restarted["counts"]["current_key"] == restarted["counts"]["rewritten"] == 1
