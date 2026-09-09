"""Record the exact narrow pytest run without accepting a skip as runtime proof."""

import json
import os
from pathlib import Path

import pytest

from qualification.transactions.contract import CASES, TEST_PATH

_receipt = {}


def pytest_sessionstart(session):
    import django_ray

    module = str(Path(django_ray.__file__).resolve())
    if module != os.environ["DJANGO_RAY_TRANSACTION_MODULE"]:
        raise pytest.UsageError("transaction qualification imported another candidate")
    _receipt.clear()
    _receipt.update(module=module, server_version=None, socket_only=None, cases={})


@pytest.fixture(scope="session", autouse=True)
def transaction_server_identity(django_db_setup, django_db_blocker):
    from django.db import connection

    if connection.vendor != "postgresql":
        raise pytest.UsageError("transaction qualification requires PostgreSQL")
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        version = int(cursor.fetchone()[0])
        cursor.execute("SHOW listen_addresses")
        socket_only = cursor.fetchone()[0] == ""
    connection.close()
    if not 170000 <= version < 180000:
        raise pytest.UsageError("transaction qualification requires PostgreSQL 17")
    _receipt.update(server_version=version, socket_only=socket_only)


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    if sorted(item.nodeid for item in session.items) != [f"{TEST_PATH}::{name}" for name in CASES]:
        raise pytest.UsageError("transaction qualification requires the exact case set")


def pytest_runtest_logreport(report):
    case = _receipt["cases"].setdefault(
        report.nodeid.split("::", 1)[1], {"phases": [], "observations": {}}
    )
    case["phases"].append(f"{report.when}:{report.outcome}")
    case["observations"].update(dict(report.user_properties))


def pytest_sessionfinish(session, exitstatus):
    if _receipt:
        Path(os.environ["DJANGO_RAY_TRANSACTION_RECEIPT"]).write_text(
            json.dumps(_receipt, sort_keys=True), encoding="utf-8"
        )
