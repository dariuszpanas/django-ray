"""Private PostgreSQL projection boundaries without a database or native runtime."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualification.application import protected_database as database
from qualification.application import protected_runtime as runtime

ENVIRONMENT = {
    "DATABASE_ENGINE": "django.db.backends.postgresql",
    "DATABASE_HOST": "postgres",
    "DATABASE_PORT": "5432",
    "DATABASE_NAME": "django_ray",
    "DATABASE_USER": "django_ray",
    "DATABASE_PASSWORD": "private-password",
}
IDS = [11, 22, 33]
TASKS = ["10000000-0000-4000-8000-000000000001", "10000000-0000-4000-8000-000000000002"]


def envelopes():
    return [
        (
            pk,
            json.dumps({"nonce": f"nonce-{pk}", "ciphertext": f"cipher-{pk}"}),
            True,
            index == 0,
            index != 0,
        )
        for index, pk in enumerate(IDS)
    ]


class Connection:
    def __init__(self, *, fail=None, rows=None, tasks=None):
        self.fail = fail
        self.rows = envelopes() if rows is None else rows
        self.tasks = [(pk, task, True) for pk, task in zip(IDS[1:], TASKS, strict=True)]
        if tasks is not None:
            self.tasks = tasks
        self.events = []
        self.queries = []
        self.fetch_limits = []
        self.closed = False
        self.in_transaction = False

    def __enter__(self):
        self.events.append("connection")
        return self

    def __exit__(self, *exc):
        self.closed = True
        self.events.append("closed")

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        self.in_transaction = True
        try:
            yield
            if self.fail == "commit":
                raise OSError("private-commit")
            self.events.append("commit")
        except Exception:
            self.events.append("rollback")
            raise
        finally:
            self.in_transaction = False

    @contextmanager
    def cursor(self):
        assert self.in_transaction
        if self.fail == "cursor":
            raise OSError("private-cursor")
        self.events.append("cursor")
        try:
            yield self
        finally:
            self.events.append("cursor_closed")

    def execute(self, query, parameters=None):
        assert self.in_transaction and not self.closed
        self.queries.append((query, parameters))
        if self.fail == len(self.queries):
            raise TimeoutError("private-query")

    def fetchmany(self, limit):
        self.fetch_limits.append(limit)
        if self.fail == "fetch":
            raise OSError("private-fetch")
        return self.rows if self.queries[-1][0] == database.ENVELOPES_SQL else self.tasks


def install(monkeypatch, connection=None, *, failure=None):
    connection = connection or Connection()
    calls = []
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    def connect(*args, **kwargs):
        assert not args
        calls.append(kwargs)
        if failure:
            raise failure
        return connection

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    return connection, calls


@pytest.mark.parametrize("status_only", [False, True])
def test_read_only_bounded_parameterized_projection_closes_before_return(monkeypatch, status_only):
    connection, calls = install(monkeypatch)
    values, tasks = database.read_owned_rows(IDS, status_only=status_only)
    assert values == runtime.protected_values_from_rows(IDS, envelopes())
    assert tasks == (TASKS if status_only else [])
    assert calls == [
        {
            "host": "postgres",
            "port": 5432,
            "dbname": "django_ray",
            "user": "django_ray",
            "password": "private-password",
            "connect_timeout": 2,
            "autocommit": True,
        }
    ]
    assert connection.queries[:3] == [
        ("SET TRANSACTION READ ONLY", None),
        ("SET LOCAL statement_timeout = '1000ms'", None),
        ("SET LOCAL lock_timeout = '1000ms'", None),
    ]
    assert connection.queries[3] == (
        database.ENVELOPES_SQL,
        (
            65536,
            "thin",
            "testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            "SUCCEEDED",
            "FAILED",
            *IDS,
        ),
    )
    assert connection.queries[4:] == (
        [(database.TASKS_SQL, (*IDS[1:], "FAILED"))] if status_only else []
    )
    assert connection.fetch_limits == ([4, 3] if status_only else [4])
    assert connection.events == [
        "connection",
        "begin",
        "cursor",
        "cursor_closed",
        "commit",
        "closed",
    ]
    assert connection.closed and not connection.in_transaction


def test_projection_columns_match_the_candidate_model_schema():
    # Bind the hand-written projection to the real source schema without querying it.
    from django_ray.models import RayTaskExecution

    assert RayTaskExecution._meta.db_table == "django_ray_raytaskexecution"
    assert RayTaskExecution._meta.pk.column == "id"
    for name in ("runtime_env_json", "runtime_env_profile", "callable_path", "state", "task_id"):
        assert RayTaskExecution._meta.get_field(name).column == name


@pytest.mark.parametrize(
    "fault", [None, "ascii", "multibyte", "nul", "callable", "profile", "state", "task-suffix"]
)
def test_actual_projection_guards_bytes_fixture_and_complete_tasks(fault):
    # Exercise projection semantics with stdlib SQLite, adapting only parameter
    # markers and PostgreSQL's byte-length function. PostgreSQL execution itself
    # remains a hosted qualification requirement.
    connection = sqlite3.connect(":memory:")
    try:
        connection.create_function("OCTET_LENGTH", 1, lambda value: len(value.encode()))
        connection.execute(
            "CREATE TABLE django_ray_raytaskexecution (id INTEGER PRIMARY KEY, "
            "runtime_env_json TEXT, runtime_env_profile TEXT, callable_path TEXT, "
            "state TEXT, task_id TEXT)"
        )
        rows: list[list[int | str]] = []
        for index, pk in enumerate([*IDS, 99]):
            rows.append(
                [
                    pk,
                    json.dumps({"nonce": f"n-{pk}", "ciphertext": f"c-{pk}"}),
                    "thin",
                    "testproject.apps.cluster_tasks.tasks.runtime_env_probe",
                    "SUCCEEDED" if index == 0 else "FAILED",
                    TASKS[index - 1] if index in {1, 2} else "unused",
                ]
            )
        if fault in {"ascii", "multibyte", "nul"}:
            rows[0][1] = {
                "ascii": "x" * 65537,
                "multibyte": "é" * 32769,
                "nul": "x\0" + "x" * 65537,
            }[fault]
        elif fault == "callable":
            rows[0][3] = "other.callable"
        elif fault == "profile":
            rows[0][2] = "other-profile"
        elif fault == "state":
            rows[0][4] = "FAILED"
        elif fault == "task-suffix":
            task_id = rows[1][5]
            assert isinstance(task_id, str)
            rows[1][5] = task_id + "\0private-suffix"
        connection.executemany(
            "INSERT INTO django_ray_raytaskexecution VALUES (?, ?, ?, ?, ?, ?)", rows
        )
        observed = connection.execute(
            database.ENVELOPES_SQL.replace("%s", "?"),
            (
                65536,
                "thin",
                "testproject.apps.cluster_tasks.tasks.runtime_env_probe",
                "SUCCEEDED",
                "FAILED",
                *IDS,
            ),
        ).fetchall()
        assert len(observed) == 3
        assert {row[0] for row in observed} == set(IDS)
        if fault in {"ascii", "multibyte", "nul"}:
            assert next(row for row in observed if row[0] == IDS[0])[1] is None
            assert all(rows[0][1] not in row for row in observed)
        if fault in {"ascii", "multibyte", "nul", "callable", "profile", "state"}:
            with pytest.raises(ValueError):
                runtime.protected_values_from_rows(IDS, observed)
        else:
            assert len(runtime.protected_values_from_rows(IDS, observed)) == 11
        observed_tasks = connection.execute(
            database.TASKS_SQL.replace("%s", "?"), (*IDS[1:], "FAILED")
        ).fetchall()
        assert len(observed_tasks) == 2
        assert all(len(row[1]) <= 37 for row in observed_tasks)
        if fault == "task-suffix":
            with pytest.raises(ValueError, match="incomplete"):
                runtime.refused_task_ids_from_rows(IDS, observed_tasks)
        else:
            assert runtime.refused_task_ids_from_rows(IDS, observed_tasks) == TASKS
    finally:
        connection.close()


@pytest.mark.parametrize("failure", ["cursor", 1, 2, 3, 4, 5, "fetch", "commit"])
def test_query_cursor_and_transaction_failures_close_connection(monkeypatch, failure):
    connection, _ = install(monkeypatch, Connection(fail=failure))
    with pytest.raises((OSError, TimeoutError)):
        database.read_owned_rows(IDS, status_only=True)
    assert connection.closed and not connection.in_transaction
    assert connection.events[-2:] == ["rollback", "closed"]


def test_connect_failure_does_not_attempt_queries(monkeypatch):
    connection, calls = install(monkeypatch, failure=OSError("private-connect"))
    with pytest.raises(OSError):
        database.read_owned_rows(IDS, status_only=True)
    assert len(calls) == 1
    assert not connection.events and not connection.queries


@pytest.mark.parametrize("name", ENVIRONMENT)
@pytest.mark.parametrize("value", [None, ""])
def test_missing_database_configuration_refuses_before_connect(monkeypatch, name, value):
    _, calls = install(monkeypatch)
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="owned"):
        database.read_owned_rows(IDS, status_only=True)
    assert not calls


@pytest.mark.parametrize("name", [name for name in ENVIRONMENT if name != "DATABASE_PASSWORD"])
def test_other_database_coordinates_are_not_followed(monkeypatch, name):
    _, calls = install(monkeypatch)
    monkeypatch.setenv(name, "other")
    with pytest.raises(ValueError, match="owned database"):
        database.read_owned_rows(IDS, status_only=True)
    assert not calls


@pytest.mark.parametrize(
    "ids", [[], [11, 22], [11, 22, 22], [True, 22, 33], [0, 22, 33], ["11", 22, 33], None]
)
def test_invalid_ids_refuse_before_connect(monkeypatch, ids):
    _, calls = install(monkeypatch)
    with pytest.raises(ValueError, match="three owned"):
        database.read_owned_rows(ids, status_only=True)
    assert not calls


def test_truthy_phase_is_not_an_explicit_phase(monkeypatch):
    _, calls = install(monkeypatch)
    with pytest.raises(ValueError, match="explicit phase"):
        database.read_owned_rows(IDS, status_only=1)
    assert not calls


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "duplicate",
        "wrong-id",
        "wrong-fixture",
        "wrong-state",
        "null",
        "oversized",
        "multibyte",
        "json-list",
        "nonce",
        "ciphertext",
    ],
)
def test_bad_envelope_projection_rolls_back_and_closes(monkeypatch, fault):
    rows = envelopes()
    row = list(rows[0])
    if fault == "missing":
        rows.pop()
    elif fault == "extra":
        rows.append(rows[0])
    elif fault == "duplicate":
        rows[1] = rows[0]
    elif fault == "wrong-id":
        row[0] = 44
    elif fault == "wrong-fixture":
        row[2] = False
    elif fault == "wrong-state":
        row[3] = False
    elif fault == "null":
        row[1] = None
    elif fault == "oversized":
        row[1] = "x" * 65537
    elif fault == "multibyte":
        row[1] = "é" * 32769
    elif fault == "json-list":
        row[1] = "[]"
    else:
        row[1] = json.dumps({"nonce": "n", "ciphertext": "c", fault: ""})
    rows[0] = tuple(row)
    connection, _ = install(monkeypatch, Connection(rows=rows))
    with pytest.raises(ValueError):
        database.read_owned_rows(IDS, status_only=True)
    assert connection.closed and connection.events[-2:] == ["rollback", "closed"]
    assert len(connection.queries) == 4


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "duplicate",
        "wrong-id",
        "incomplete",
        "v1",
        "uppercase",
        "oversized",
        "malformed",
    ],
)
def test_bad_refused_task_projection_rolls_back_and_closes(monkeypatch, fault):
    tasks = [(pk, task, True) for pk, task in zip(IDS[1:], TASKS, strict=True)]
    if fault == "missing":
        tasks.pop()
    elif fault == "extra":
        tasks.append(tasks[0])
    elif fault == "duplicate":
        tasks[1] = tasks[0]
    else:
        pk, task, complete = tasks[0]
        if fault == "wrong-id":
            pk = 44
        elif fault == "incomplete":
            complete = False
        elif fault == "v1":
            task = "10000000-0000-1000-8000-000000000001"
        elif fault == "uppercase":
            task = "ABCDEF00-0000-4000-8000-000000000001"
        elif fault == "oversized":
            task += "x"
        else:
            task = "x" * 36
        tasks[0] = (pk, task, complete)
    connection, _ = install(monkeypatch, Connection(tasks=tasks))
    with pytest.raises(ValueError):
        database.read_owned_rows(IDS, status_only=True)
    assert connection.closed and connection.events[-2:] == ["rollback", "closed"]


def test_reader_and_shared_validators_do_not_import_django_in_fresh_process():
    script = """
import importlib.abc, json, sys
from contextlib import contextmanager
from types import SimpleNamespace
class DenyApplicationImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'django', 'django_ray', 'ray', 'unfold', 'testproject'}:
            raise AssertionError('application bootstrap forbidden')
sys.meta_path.insert(0, DenyApplicationImports())
from qualification.application import protected_database as database
from qualification.application import protected_runtime as runtime
rows = [(pk, json.dumps({'nonce':'n','ciphertext':'c'}), True, pk == 11, pk != 11) for pk in [11,22,33]]
assert len(runtime.protected_values_from_rows([11,22,33], rows)) == 11
tasks = ['10000000-0000-4000-8000-000000000001','10000000-0000-4000-8000-000000000002']
assert runtime.refused_task_ids_from_rows([11,22,33], [(22,tasks[0],True),(33,tasks[1],True)]) == tasks
class Connection:
    closed = False
    def __enter__(self): return self
    def __exit__(self, *exc): self.closed = True
    @contextmanager
    def transaction(self): yield
    @contextmanager
    def cursor(self): yield self
    def execute(self, sql, params=None): self.sql = sql
    def fetchmany(self, limit):
        return rows if self.sql == database.ENVELOPES_SQL else [(22,tasks[0],True),(33,tasks[1],True)]
connection = Connection()
def connect(**kwargs):
    assert kwargs['connect_timeout'] == 2
    return connection
sys.modules['psycopg'] = SimpleNamespace(connect=connect)
values, found = database.read_owned_rows([11,22,33], status_only=True)
assert len(values) == 11 and found == tasks and connection.closed
assert not any(name.split('.')[0] in {'django','django_ray','ray','unfold','testproject'} for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, **ENVIRONMENT, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b""
