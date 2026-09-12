"""Record the exact narrow pytest run without accepting a skip as runtime proof."""

import json
import os
from pathlib import Path

import pytest

from qualification.transactions.contract import CASES, case_nodeids

_receipt = {}


def pytest_sessionstart(session):
    import django_ray
    from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION

    module = str(Path(django_ray.__file__).resolve())
    if module != os.environ["DJANGO_RAY_TRANSACTION_MODULE"]:
        raise pytest.UsageError("transaction qualification imported another candidate")
    _receipt.clear()
    _receipt.update(
        module=module,
        execution_protocol_version=EXECUTION_PROTOCOL_VERSION,
        server_version=None,
        socket_only=None,
        cases={},
    )
    case_nodeids(EXECUTION_PROTOCOL_VERSION)


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
    if sorted(item.nodeid for item in session.items) != sorted(
        case_nodeids(_receipt["execution_protocol_version"])
    ):
        raise pytest.UsageError("transaction qualification requires the exact case set")


def pytest_runtest_logreport(report):
    names = dict(zip(case_nodeids(_receipt["execution_protocol_version"]), CASES, strict=True))
    if report.nodeid not in names:
        raise pytest.UsageError("transaction qualification received an unexpected case")
    case = _receipt["cases"].setdefault(names[report.nodeid], {"phases": [], "observations": {}})
    case["phases"].append(f"{report.when}:{report.outcome}")
    case["observations"].update(dict(report.user_properties))


def pytest_sessionfinish(session, exitstatus):
    if _receipt:
        Path(os.environ["DJANGO_RAY_TRANSACTION_RECEIPT"]).write_text(
            json.dumps(_receipt, sort_keys=True), encoding="utf-8"
        )
