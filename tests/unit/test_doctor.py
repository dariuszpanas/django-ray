"""Resource-free doctor failure, transaction and command boundaries."""

from contextlib import nullcontext
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management.base import CommandError
from django.db import OperationalError

from django_ray import doctor
from django_ray.management.commands import django_ray_doctor as command

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def _fake_database(monkeypatch, *, vendor="sqlite", atomic=False, failure=False):
    queries = []

    class Cursor:
        def execute(self, sql):
            queries.append(sql)
            if failure:
                raise OperationalError("postgres://secret:password@private.invalid token=secret")

        def fetchone(self):
            return (1,)

    connection = SimpleNamespace(
        vendor=vendor,
        in_atomic_block=atomic,
        get_autocommit=lambda: True,
        cursor=lambda: nullcontext(Cursor()),
    )
    monkeypatch.setattr(doctor, "connections", {"probe": connection})
    monkeypatch.setattr(doctor.transaction, "atomic", lambda **kwargs: nullcontext())
    return queries


@pytest.mark.parametrize("vendor", ["sqlite", "postgresql"])
def test_migration_failure_stops_before_model_observation(monkeypatch, vendor):
    queries = _fake_database(monkeypatch, vendor=vendor)
    monkeypatch.setattr(doctor, "_migration_status", lambda connection: {"status": "pending"})
    monkeypatch.setattr(doctor, "_cohort_observation", lambda **kwargs: pytest.fail("model read"))
    monkeypatch.setattr(
        doctor.protocol_status,
        "_build_protocol_status_observation",
        lambda **kwargs: pytest.fail("protocol read"),
    )
    report = doctor.build_doctor(using="probe", observed_at=NOW)
    assert report.protocol is report.cohort is None
    assert report.blockers == ({"code": "migrations_not_current", "count": None},)
    assert queries == (
        ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"]
        if vendor == "postgresql"
        else []
    ) + ["SELECT 1"]


def test_failed_connection_is_redacted_and_never_followed_by_migration_reads(monkeypatch):
    queries = _fake_database(monkeypatch, failure=True)
    monkeypatch.setattr(
        doctor, "_migration_status", lambda connection: pytest.fail("migration read")
    )
    report = doctor.build_doctor(using="probe", observed_at=NOW)
    assert report.database == {"connectivity": "unavailable", "migrations": None}
    assert queries == ["SELECT 1"]
    for rendered in (doctor.render_doctor_json(report), doctor.render_doctor_text(report)):
        assert "private.invalid" not in rendered
        assert "password" not in rendered
        assert "token=secret" not in rendered
        assert "database_unavailable" in rendered


def test_unknown_alias_and_unsupported_vendor_are_bounded(monkeypatch):
    report = doctor.build_doctor(using="doctor-no-such-database", observed_at=NOW)
    assert report.blockers[0]["code"] == "database_unavailable"
    queries = _fake_database(monkeypatch, vendor="mysql")
    assert (
        doctor.build_doctor(using="probe", observed_at=NOW).blockers[0]["code"]
        == "database_vendor_unsupported"
    )
    assert not queries


def test_nested_transaction_is_refused_before_any_observation(monkeypatch):
    queries = _fake_database(monkeypatch, atomic=True)
    with pytest.raises(doctor.DoctorError, match="outermost"):
        doctor.build_doctor(using="probe", observed_at=NOW)
    assert not queries


@pytest.mark.parametrize("value", [datetime(2026, 9, 12), "untrusted", 1, 0, False])
def test_invalid_observation_time_is_refused(value):
    with pytest.raises(doctor.DoctorError, match="aware observation"):
        doctor.build_doctor(observed_at=value)


@pytest.mark.parametrize("as_json", [True, False])
def test_command_skips_user_system_checks_and_preserves_bounded_report(monkeypatch, as_json):
    report = doctor.DoctorReport(
        NOW,
        {"connectivity": "unavailable", "migrations": None},
        None,
        None,
        ({"code": "database_unavailable", "count": None},),
    )
    selected = []
    monkeypatch.setattr(command, "build_doctor", lambda *, using: selected.append(using) or report)
    output = StringIO()
    tool = command.Command(stdout=output)
    assert tool.requires_system_checks == []
    assert tool.requires_migrations_checks is False
    tool.handle(database="chosen", as_json=as_json)
    assert selected == ["chosen"]
    assert len(output.getvalue().encode()) <= doctor.DOCTOR_OUTPUT_MAX_BYTES
    assert "unverified" in output.getvalue()


def test_command_safe_service_error(monkeypatch):
    def refused(**kwargs):
        raise doctor.DoctorError("doctor must own its outermost read-only database transaction")

    monkeypatch.setattr(command, "build_doctor", refused)
    with pytest.raises(CommandError, match="outermost"):
        command.Command().handle(database="default", as_json=True)


def test_renderers_refuse_oversized_caller_constructed_report():
    report = doctor.DoctorReport(
        NOW, {"untrusted": "x" * doctor.DOCTOR_OUTPUT_MAX_BYTES}, None, None, ()
    )
    for render in (doctor.render_doctor_json, doctor.render_doctor_text):
        with pytest.raises(doctor.DoctorError, match="output budget"):
            render(report)


def test_migration_inventory_does_not_import_migration_modules(monkeypatch):
    monkeypatch.setattr(
        doctor.pkgutil,
        "iter_modules",
        lambda paths: iter(
            [
                SimpleNamespace(name="0030_cohort_claims"),
                SimpleNamespace(name="__init__"),
                SimpleNamespace(name="unexpected.py; token=secret"),
            ]
        ),
    )
    assert doctor._known_migrations() == ("0030_cohort_claims",)
