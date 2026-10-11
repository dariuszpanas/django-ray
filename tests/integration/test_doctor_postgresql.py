"""PostgreSQL read-only, statement/lock/connect deadline and isolation evidence."""

from __future__ import annotations

import copy
import socket
import time
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from django.db import DatabaseError, connection, connections, transaction
from django.db.utils import ConnectionHandler
from django.utils import timezone

from django_ray.doctor import probe
from django_ray.models import RayTaskExecution, TaskExecutionProtocolPolicy, TaskState
from django_ray.protocol_coordination import close_legacy_worker_admission

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and an owned PostgreSQL test database")


@pytest.fixture
def owned_observer(django_db_blocker):
    alias = "doctor_owned_postgresql"
    original = copy.deepcopy(connection.settings_dict)
    selected = SimpleNamespace(DATABASES={"default": original}, USE_TZ=True)
    clone = probe.readonly_database(selected, "default", time.monotonic() + 5)
    wrapper = ConnectionHandler({"default": clone})["default"]
    wrapper.alias = alias
    connections.databases[alias] = wrapper.settings_dict
    connections[alias] = wrapper
    # The independently registered alias is not in the TestCase's startup
    # alias set. Permit this owned fixture's connections, then remove the alias
    # before restoring the caller's test guard (as for the owned SQLite fixture).
    with django_db_blocker.unblock():
        try:
            yield alias, wrapper
        finally:
            wrapper.close()
            del connections[alias]
            del connections.databases[alias]
            assert connection.settings_dict == original


def session_settings(wrapper):
    with wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('default_transaction_read_only'), current_setting('statement_timeout'), current_setting('lock_timeout'), pg_backend_pid()"
        )
        return cursor.fetchone()


def test_postgresql_doctor_reads_on_dedicated_readonly_session_and_preserves_caller(owned_observer):
    alias, wrapper = owned_observer
    original = session_settings(connection)
    owned = session_settings(wrapper)
    assert owned[:3] == ("on", "1s", "250ms") and owned[3] != original[3]
    with pytest.raises(DatabaseError) as refusal:
        with wrapper.cursor() as cursor:
            cursor.execute("UPDATE django_ray_raytaskexecution SET state = state")
    assert refusal.value.__cause__.sqlstate == "25006"
    output = {}
    budget = probe.QueryBudget(time.monotonic() + 5)
    probe.collect_database(
        using=alias,
        budget=budget,
        config={"WORKER_LEASE_SECONDS": 60},
        emit=lambda name, value: output.update({name: value}),
    )
    assert output["database"]["values"] == {"vendor": "postgresql", "read_only": True}
    assert output["work"]["values"]["queued"] == 0
    assert budget.statements <= 32 and budget.rows <= 64
    assert wrapper.connection is None
    assert session_settings(connection) == original


def test_postgresql_doctor_statement_timeout_does_not_change_caller(owned_observer):
    _alias, wrapper = owned_observer
    original = session_settings(connection)
    started = time.monotonic()
    with pytest.raises(DatabaseError) as timeout:
        with wrapper.cursor() as cursor:
            cursor.execute("SELECT pg_sleep(10)")
    assert timeout.value.__cause__.sqlstate == "57014"
    assert time.monotonic() - started < 3
    assert session_settings(connection) == original


def test_postgresql_doctor_preserves_unsupported_work_without_hydrating_payloads(owned_observer):
    alias, wrapper = owned_observer
    policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
    close_legacy_worker_admission(expected_revision=policy.revision, legacy_producers_retired=True)
    execution = RayTaskExecution.objects.create(
        task_id="doctor-owned-unsupported",
        callable_path="must_not_be_imported_by_doctor.execute",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        last_heartbeat_at=timezone.now(),
        args_json="private-arguments" * 4096,
        kwargs_json="private-keywords",
        runtime_env_json="private-runtime",
        input_reference="private-input-reference",
    )
    before = RayTaskExecution.objects.filter(pk=execution.pk).values().get()
    original = session_settings(connection)
    queries = []

    def observe_sql(execute, sql, params, many, context):
        queries.append(sql)
        return execute(sql, params, many, context)

    output = {}
    with wrapper.execute_wrapper(observe_sql):
        probe.collect_database(
            using=alias,
            budget=probe.QueryBudget(time.monotonic() + 5),
            config={"WORKER_LEASE_SECONDS": 60},
            emit=lambda name, value: output.update({name: value}),
        )
    assert output["work"]["outcome"] == "blocker"
    assert output["work"]["values"]["unsupported_protocol"] == 1
    assert output["work"]["values"]["running"] == 1
    assert output["work"]["values"]["active_without_heartbeat"] == 0
    assert queries and any("django_ray_raytaskexecution" in sql for sql in queries)
    for sql in queries:
        assert sql.lstrip().upper().startswith(("SELECT", "SET TRANSACTION"))
        assert all(
            field not in sql
            for field in (
                "callable_path",
                "args_json",
                "kwargs_json",
                "runtime_env_json",
                "input_reference",
            )
        )
    assert RayTaskExecution.objects.filter(pk=execution.pk).values().get() == before
    assert wrapper.connection is None
    assert session_settings(connection) == original


def test_postgresql_doctor_lock_timeout_retains_partial_evidence_and_closes_session(owned_observer):
    alias, wrapper = owned_observer
    output = {}
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("LOCK TABLE django_ray_raytaskexecution IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(probe.ProbeUnavailableError, match="probe_timeout"):
            probe.collect_database(
                using=alias,
                budget=probe.QueryBudget(time.monotonic() + 5),
                config={"WORKER_LEASE_SECONDS": 60},
                emit=lambda name, value: output.update({name: value}),
            )
        assert time.monotonic() - started < 3
        assert "leases" in output and "work" not in output
        assert wrapper.connection is None
        assert RayTaskExecution.objects.count() == 0


def test_postgresql_doctor_connect_timeout_bounds_stalled_owned_peer():
    # An owned local listener accepts libpq startup but never replies. It has no
    # access to production infrastructure and exits with the fixture's Event.
    stop = Event()
    accepted = Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)

    def stall():
        try:
            peer, _address = listener.accept()
            with peer:
                accepted.set()
                stop.wait(6)
        except OSError:
            pass

    worker = Thread(target=stall)
    worker.start()
    source = {
        **connection.settings_dict,
        "HOST": "127.0.0.1",
        "PORT": str(listener.getsockname()[1]),
        "OPTIONS": {"sslmode": "disable"},
    }
    clone = probe.readonly_database(
        SimpleNamespace(DATABASES={"default": source}), "default", time.monotonic() + 2
    )
    wrapper = ConnectionHandler({"default": clone})["default"]
    started = time.monotonic()
    try:
        with pytest.raises(DatabaseError):
            wrapper.ensure_connection()
        assert accepted.is_set()
        assert time.monotonic() - started < 5
    finally:
        wrapper.close()
        stop.set()
        listener.close()
        worker.join(timeout=6)
        assert not worker.is_alive()
