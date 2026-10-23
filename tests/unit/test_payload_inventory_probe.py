"""Resource-free checks of the isolated observer's database and failure boundary."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import django
import django.conf
import django.db
import django.db.transaction
import pytest

from django_ray.payload_inventory import contracts, probe


class Connection:
    """Exercise the observer's SQL wrapper without opening a database."""

    def __init__(self, vendor):
        self.vendor = vendor
        self.connection = SimpleNamespace(set_progress_handler=Mock())
        self.wrapper = None
        self.statements = []
        self.in_transaction = False
        self.ensure_connection = Mock()
        self.close = Mock()

    @contextmanager
    def execute_wrapper(self, wrapper):
        self.wrapper = wrapper
        try:
            yield
        finally:
            self.wrapper = None

    @contextmanager
    def cursor(self):
        yield self

    @contextmanager
    def atomic(self):
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    def execute(self, sql, *, many=False):
        def observed(statement, _params, _many, _context):
            self.statements.append(statement)

        assert self.wrapper is not None
        return self.wrapper(observed, sql, None, many, {})


@pytest.fixture
def observer(monkeypatch, tmp_path):
    selected = SimpleNamespace(
        DATABASES={
            "archive": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "private-database",
                "PASSWORD": "private-password",
                "CONN_MAX_AGE": 600,
                "OPTIONS": {"options": "-c application_name=inventory-test"},
            }
        },
        DJANGO_RAY={
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "private-payloads"),
        },
        INSTALLED_APPS=["application_ready_must_not_import"],
        DATABASE_ROUTERS=["application_router_must_not_import"],
        USE_TZ=True,
    )
    connection = Connection("postgresql")
    configure = Mock()
    settings_loader = Mock(return_value=selected)
    setup = Mock()
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(django.conf, "Settings", settings_loader)
    monkeypatch.setattr(django.conf, "settings", SimpleNamespace(configure=configure))
    monkeypatch.setattr(django, "setup", setup)
    monkeypatch.setattr(django.db, "connections", {"default": connection})
    monkeypatch.setattr(django.db.transaction, "atomic", connection.atomic)
    frames = []

    def run(**options):
        return probe.run(
            settings_module="owned.settings",
            using="archive",
            deadline=110,
            limits=contracts.Limits(max_objects=2),
            emit=frames.append,
            **options,
        )

    return SimpleNamespace(
        selected=selected,
        connection=connection,
        configure=configure,
        settings_loader=settings_loader,
        setup=setup,
        clock=clock,
        frames=frames,
        run=run,
    )


@pytest.mark.parametrize("vendor", ["postgresql", "sqlite"])
def test_observer_isolates_application_setup_and_readonly_database_clone(
    observer, monkeypatch, tmp_path, vendor
):
    connection = observer.connection
    connection.vendor = vendor
    if vendor == "sqlite":
        database = tmp_path / "registry.sqlite3"
        database.touch()
        observer.selected.DATABASES["archive"] = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(database),
            "OPTIONS": {"timeout": 60},
        }
    original = copy.deepcopy(vars(observer.selected))

    def scan(self, path, lookup):
        assert path == observer.selected.DJANGO_RAY["INPUT_STORAGE_FILESYSTEM_PATH"]
        assert lookup is probe.registry_lookup
        assert connection.in_transaction
        assert connection.statements == [
            "PRAGMA query_only=ON" if vendor == "sqlite" else "SET TRANSACTION READ ONLY"
        ]
        if vendor == "sqlite":
            callback, frequency = connection.connection.set_progress_handler.call_args.args
            assert frequency == 1000 and callback() is False
            observer.clock.now = 110
            assert callback() is True
            observer.clock.now = 100
        self.entries = self.objects = 1
        self.record("private-object", "unregistered_aged")
        self.finish("observed")

    monkeypatch.setattr(probe.Scan, "run", scan)
    assert observer.run() == 0
    isolated = observer.configure.call_args.kwargs
    assert isolated["INSTALLED_APPS"] == ["django_ray.doctor.probe.DoctorAppConfig"]
    assert isolated["DATABASE_ROUTERS"] == [] and isolated["DJANGO_RAY"] == {}
    assert isolated["LOGGING_CONFIG"] is None
    assert set(isolated["DATABASES"]) == {"default"}
    readonly = isolated["DATABASES"]["default"]
    assert readonly["CONN_MAX_AGE"] == 0 and readonly["CONN_HEALTH_CHECKS"] is False
    if vendor == "sqlite":
        assert readonly["NAME"].endswith("?mode=ro")
        assert readonly["OPTIONS"] == {"uri": True, "timeout": 0.25}
        connection.connection.set_progress_handler.assert_called_with(None, 0)
    else:
        assert 1 <= readonly["OPTIONS"]["connect_timeout"] <= 3
        for protection in (
            "application_name=inventory-test",
            "default_transaction_read_only=on",
            "statement_timeout=",
            "lock_timeout=",
        ):
            assert protection in readonly["OPTIONS"]["options"]
    assert vars(observer.selected) == original
    observer.setup.assert_called_once_with(set_prefix=False)
    connection.ensure_connection.assert_called_once()
    connection.close.assert_called_once()
    assert not connection.in_transaction and connection.wrapper is None
    assert observer.frames[-1]["code"] == "observed"
    assert observer.frames[-1]["counts"]["unregistered_aged"] == 1
    assert "private" not in json.dumps(observer.frames)


@pytest.mark.parametrize("failure", ["statement_count", "many", "deadline"])
def test_query_guard_refuses_excess_work_before_the_driver(observer, monkeypatch, failure):
    def scan(self, _path, _lookup):
        self.entries = self.objects = 1
        self.record("private-object", "registered_active")
        if failure == "deadline":
            observer.clock.now = 110
        for _ in range(10):
            observer.connection.execute("SELECT 1", many=failure == "many")
        pytest.fail("Query budget did not stop observation")

    monkeypatch.setattr(probe.Scan, "run", scan)
    assert observer.run() == 0
    expected = "probe_timeout" if failure == "deadline" else "query_budget_exhausted"
    assert observer.frames[-1]["code"] == expected
    assert observer.frames[-1]["counts"]["registered_active"] == 1
    assert len(observer.connection.statements) == (10 if failure == "statement_count" else 1)
    observer.connection.close.assert_called_once()
    assert observer.connection.wrapper is None


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"INPUT_STORAGE_BACKEND": "s3"}, "filesystem_required"),
        ({"INPUT_STORAGE_BACKEND": "gcs"}, "filesystem_required"),
        ({"INPUT_STORAGE_BACKEND": None}, "filesystem_required"),
        ({"INPUT_STORAGE_FILESYSTEM_PATH": None}, "configuration_invalid"),
        ({"INPUT_STORAGE_FILESYSTEM_PATH": ""}, "configuration_invalid"),
        ({"INPUT_STORAGE_FILESYSTEM_PATH": "x" * 4097}, "configuration_invalid"),
        ({"RESULT_STORAGE_FILESYSTEM_PATH": "same"}, "configuration_invalid"),
    ],
)
def test_rejected_storage_never_initializes_django_or_database(observer, changes, expected):
    if changes.get("RESULT_STORAGE_FILESYSTEM_PATH") == "same":
        changes = {
            "RESULT_STORAGE_FILESYSTEM_PATH": observer.selected.DJANGO_RAY[
                "INPUT_STORAGE_FILESYSTEM_PATH"
            ]
        }
    observer.selected.DJANGO_RAY.update(changes)
    assert observer.run() == 0
    assert observer.frames[0]["code"] == expected and len(observer.frames) == 1
    observer.configure.assert_not_called()
    observer.connection.ensure_connection.assert_not_called()
    observer.connection.close.assert_not_called()


@pytest.mark.parametrize("failure", ["expired", "settings", "database"])
def test_early_failures_are_fixed_without_loading_an_application(observer, failure):
    if failure == "expired":
        observer.clock.now = 110
    elif failure == "settings":
        observer.settings_loader.side_effect = RuntimeError("private settings credentials")
    else:
        observer.selected.DATABASES["archive"]["ENGINE"] = "private.custom.backend"
    assert observer.run() == 0
    expected = {
        "expired": "probe_timeout",
        "settings": "settings_unavailable",
        "database": "database_unsupported",
    }[failure]
    assert observer.frames[0]["code"] == expected
    assert "private" not in json.dumps(observer.frames)
    observer.setup.assert_not_called()
    if failure == "expired":
        observer.settings_loader.assert_not_called()


@pytest.mark.parametrize("driver_code", [None, "57014", "55P03", 9])
def test_driver_failures_are_sanitized_and_connection_is_closed(observer, monkeypatch, driver_code):
    error = django.db.DatabaseError("private SQL, parameters and database location")
    cause = RuntimeError("private driver detail")
    if type(driver_code) is str:
        cause.sqlstate = driver_code
    elif driver_code is not None:
        cause.sqlite_errorcode = driver_code
    error.__cause__ = cause

    def scan(self, _path, _lookup):
        self.entries = self.objects = 1
        self.record("private-object", "ambiguous")
        raise error

    monkeypatch.setattr(probe.Scan, "run", scan)
    assert observer.run() == 0
    assert observer.frames[-1]["code"] == (
        "database_unavailable" if driver_code is None else "probe_timeout"
    )
    assert observer.frames[-1]["counts"]["ambiguous"] == 1
    assert "private" not in json.dumps(observer.frames)
    observer.connection.close.assert_called_once()


def test_late_error_preserves_one_summary_and_returns_failure(observer, monkeypatch):
    def scan(self, _path, _lookup):
        self.finish("observed")
        raise RuntimeError("private post-observation failure")

    monkeypatch.setattr(probe.Scan, "run", scan)
    assert observer.run() == 2
    assert len(observer.frames) == 1 and observer.frames[0]["code"] == "observed"
    observer.connection.close.assert_called_once()


def test_sqlite_connect_failure_cleans_up_without_a_driver_handle(observer, tmp_path):
    database = tmp_path / "registry.sqlite3"
    database.touch()
    observer.selected.DATABASES["archive"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(database),
    }
    observer.connection.vendor = "sqlite"
    observer.connection.connection = None
    observer.connection.ensure_connection.side_effect = django.db.DatabaseError("private connect")
    assert observer.run() == 0
    assert observer.frames[-1]["code"] == "database_unavailable"
    observer.connection.close.assert_called_once()
