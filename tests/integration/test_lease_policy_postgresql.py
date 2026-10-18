"""Real PostgreSQL shared/exclusive lease-policy contention, with no Ray runtime."""

from __future__ import annotations

from threading import Event, Thread

import pytest
from django.db import connection, connections, transaction

from django_ray.lease_policy import (
    LeasePolicy,
    LeasePolicyError,
    lock_lease_policy,
    set_lease_policy,
)
from django_ray.models import TaskWorkerLease

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("requires the existing owned PostgreSQL lane")


def _with_holder(*, exclusive: bool, callback, register: bool = False):
    entered = Event()
    release = Event()
    errors = []

    def hold():
        try:
            with transaction.atomic():
                lock_lease_policy(exclusive=exclusive)
                if register:
                    TaskWorkerLease.objects.create(
                        worker_id="policy-race-owner", hostname="owned-test", pid=1
                    )
                entered.set()
                if not release.wait(5):
                    raise AssertionError("bounded policy holder timed out")
        except BaseException as error:
            errors.append(error)
            entered.set()
        finally:
            connections["default"].close()

    thread = Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(5)
        assert not errors
        callback()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors


def _transition():
    return set_lease_policy(
        seconds=90,
        expected_seconds=60,
        expected_revision=1,
        all_lease_users_stopped=True,
    )


def test_independent_worker_shared_guards_coexist():
    def observe():
        with transaction.atomic():
            assert lock_lease_policy() == LeasePolicy(60, 1)

    _with_holder(exclusive=False, callback=observe)


def test_transition_cannot_cross_uncommitted_worker_registration():
    def transition():
        with pytest.raises(LeasePolicyError, match="lease_policy_busy"):
            _transition()

    _with_holder(exclusive=False, callback=transition, register=True)
    with pytest.raises(LeasePolicyError, match="lease_policy_active_leases"):
        _transition()
    assert TaskWorkerLease.objects.get(worker_id="policy-race-owner").is_active


def test_exclusive_transition_blocks_new_worker_before_any_lease_write():
    def register():
        with pytest.raises(LeasePolicyError, match="lease_policy_busy"):
            with transaction.atomic():
                lock_lease_policy()
                TaskWorkerLease.objects.create(
                    worker_id="must-not-exist", hostname="owned-test", pid=1
                )

    _with_holder(exclusive=True, callback=register)
    assert not TaskWorkerLease.objects.exists()


def test_read_only_policy_does_not_require_mutation_privileges():
    from django_ray.lease_policy import read_lease_policy

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        assert read_lease_policy() == LeasePolicy(60, 1)
