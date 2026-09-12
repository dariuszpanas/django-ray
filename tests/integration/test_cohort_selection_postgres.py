"""Independent-connection contention proof for the bounded cohort selector."""

from __future__ import annotations

import importlib
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connection, transaction

from django_ray.models import RayTaskCohortClaim, RayTaskExecution
from django_ray.runner import cohort_claims as selection
from django_ray.target.cohort_claim_storage import CohortClaimStorageError, CohortClaimStorageReason
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_selection import _claim, _clone

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL independent row locks")


def test_locked_highest_candidate_is_skipped_while_next_candidate_claims(
    case, monkeypatch, record_property
):
    high = _clone(case, name="locked-highest", priority=100)
    locked, release = Event(), Event()
    holder_identity = {}
    refusals = []
    original_claim = selection.claim_cohort_execution
    original_import = importlib.import_module

    def forbidden(*args, **kwargs):
        pytest.fail("Selection cannot execute a callable, load input, or contact a runtime")

    def guarded_import(name, package=None):
        if name == "tests.tasks" or name == "ray" or name.startswith("ray."):
            forbidden()
        return original_import(name, package)

    def observe_claim(*args, **kwargs):
        try:
            return original_claim(*args, **kwargs)
        except CohortClaimStorageError as error:
            refusals.append(error.reason)
            raise

    monkeypatch.setattr(importlib, "import_module", guarded_import)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("django.utils.module_loading.import_string", forbidden)
    monkeypatch.setattr("django_ray.input_storage.load_task_input", forbidden)
    monkeypatch.setattr(selection, "claim_cohort_execution", observe_claim)

    def hold_highest():
        connection.close()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '2s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    holder_identity["pid"] = cursor.fetchone()[0]
                RayTaskExecution.objects.select_for_update().get(pk=high.pk)
                locked.set()
                if not release.wait(timeout=8):
                    raise TimeoutError(
                        "Selector did not finish while the highest row stayed locked"
                    )
        finally:
            connection.close()

    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        original_timeout = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('statement_timeout', '2s', false), pg_backend_pid()")
        selector_pid = cursor.fetchone()[1]
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = executor.submit(hold_highest)
            try:
                assert locked.wait(timeout=5), (
                    "Independent connection did not acquire the task lock"
                )
                assert holder_identity["pid"] != selector_pid
                claimed = _claim(case, limit=2)
                assert not release.is_set() and not holder.done()
                assert [item.execution.pk for item in claimed] == [case.task.pk]
                # A blocking SELECT timing out and then continuing is not the
                # same result as immediately declining a busy task identity.
                assert refusals == [CohortClaimStorageReason.EXECUTION_CHANGED]
                high.refresh_from_db()
                assert (high.state, high.execution_generation) == ("QUEUED", 0)
                assert not RayTaskCohortClaim.objects.filter(binding_id=high.pk).exists()
                assert claimed[0].claim.facts.identity.execution_generation == 1
                record_property("holder_pid", holder_identity["pid"])
                record_property("selector_pid", selector_pid)
                record_property("claimed_before_lock_release", True)
            finally:
                release.set()
                holder.result(timeout=3)
    finally:
        release.set()
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, false)", [original_timeout])
