"""Independent PostgreSQL writer/observer races for read-only inventory."""

from __future__ import annotations

import copy
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from django.db import DatabaseError, connection, connections, transaction
from django.db.utils import ConnectionHandler

from django_ray.doctor.probe import readonly_database
from django_ray.input_storage import load_task_input, prepare_task_input, register_task_input
from django_ray.models import TaskInputPayload
from django_ray.payload_inventory.contracts import Limits
from django_ray.payload_inventory.filesystem import Scan, registry_lookup

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.postgresql,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inventory"),
]


@pytest.fixture
def observer(django_db_blocker):
    if connection.vendor != "postgresql":
        pytest.skip("requires owned PostgreSQL test database")
    alias = "inventory_readonly"
    original = copy.deepcopy(connection.settings_dict)
    selected = SimpleNamespace(DATABASES={"default": original}, USE_TZ=True)
    clone = readonly_database(selected, "default", time.monotonic() + 10)
    wrapper = ConnectionHandler({"default": clone})["default"]
    wrapper.alias = alias
    connections.databases[alias] = wrapper.settings_dict
    connections[alias] = wrapper
    with django_db_blocker.unblock():
        try:
            yield alias, wrapper
        finally:
            wrapper.close()
            del connections[alias]
            del connections.databases[alias]
            assert connection.settings_dict == original


def configuration(tmp_path):
    return {
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
    }


def observe(path, alias):
    frames = []
    Scan(limits=Limits(), deadline=time.monotonic() + 5, emit=frames.append).run(
        str(path),
        lambda reference, digest, size: registry_lookup(reference, digest, size, using=alias),
    )
    return frames[-1]


@pytest.mark.parametrize("commit", [True, False])
def test_old_unregistered_reuse_survives_inventory_before_uncertain_registration(
    tmp_path, observer, commit
):
    alias, wrapper = observer
    config = configuration(tmp_path)
    prepared = prepare_task_input(("reused old object",), {}, config)
    assert prepared.digest is not None
    path = tmp_path / prepared.digest[:2] / prepared.digest[2:4] / f"{prepared.digest}.json"
    os.utime(path, (1, 1))
    original = path.read_bytes()
    reused, register, registered, finish = Event(), Event(), Event(), Event()

    def writer():
        try:
            # This successful store reuses old bytes and does not refresh their age.
            again = prepare_task_input(("reused old object",), {}, config)
            reused.set()
            assert register.wait(5)
            with transaction.atomic():
                register_task_input(again, config)
                registered.set()
                assert finish.wait(5)
                if not commit:
                    transaction.set_rollback(True)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer)
        try:
            assert reused.wait(5)
            assert observe(tmp_path, alias)["counts"]["unregistered_aged"] == 1
            register.set()
            assert registered.wait(5)
            # A pending database commit is not observable ownership authority.
            assert observe(tmp_path, alias)["counts"]["unregistered_aged"] == 1
            assert path.read_bytes() == original
            with pytest.raises(DatabaseError):
                with wrapper.cursor() as cursor:
                    cursor.execute("UPDATE django_ray_taskinputpayload SET state = state")
        finally:
            register.set()
            finish.set()
        future.result(timeout=5)
    outcome = "registered_active" if commit else "unregistered_aged"
    assert observe(tmp_path, alias)["counts"][outcome] == 1
    assert path.read_bytes() == original
    assert load_task_input(
        args_json=prepared.args_json,
        kwargs_json=prepared.kwargs_json,
        input_reference=prepared.input_reference,
        config=config,
    ) == (["reused old object"], {})


def test_registered_size_mismatch_and_alias_conflicts_remain_ambiguous(tmp_path, observer):
    alias, _ = observer
    config = configuration(tmp_path)
    prepared = prepare_task_input(("registered bytes",), {}, config)
    assert prepared.digest is not None and prepared.input_reference is not None
    with transaction.atomic():
        register_task_input(prepared, config)
    path = tmp_path / prepared.digest[:2] / prepared.digest[2:4] / f"{prepared.digest}.json"
    path.write_bytes(b"short")
    assert observe(tmp_path, alias)["counts"]["ambiguous"] == 1
    assert observe(tmp_path, alias)["counts"]["unregistered_aged"] == 0
    path.write_bytes(prepared.serialized_payload.encode())
    TaskInputPayload.objects.create(
        reference="rq2ref:v1:" + prepared.input_reference,
        payload_kind="ray_job_request",
        backend="filesystem",
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=1,
    )
    assert observe(tmp_path, alias)["counts"]["ambiguous"] == 1


def test_inventory_does_not_take_writer_locks_or_modify_purged_tombstones(tmp_path, observer):
    alias, _ = observer
    config = configuration(tmp_path)
    prepared = prepare_task_input(("reactivation bytes",), {}, config)
    with transaction.atomic():
        row = register_task_input(prepared, config)
    TaskInputPayload.objects.filter(pk=row.pk).update(state="PURGED")
    locked, release = Event(), Event()

    def writer():
        try:
            with transaction.atomic():
                TaskInputPayload.objects.select_for_update().get(pk=row.pk)
                locked.set()
                assert release.wait(5)
                register_task_input(prepared, config)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer)
        try:
            assert locked.wait(5)
            result = observe(tmp_path, alias)
            assert result["code"] == "observed"
            assert result["counts"]["registered_tombstone"] == 1
            row.refresh_from_db()
            assert row.state == "PURGED"
        finally:
            release.set()
        future.result(timeout=5)
    row.refresh_from_db()
    assert row.state == "ACTIVE"
