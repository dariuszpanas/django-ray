"""Resource-free SQLite and PostgreSQL challenge lifecycle and race contracts."""

from __future__ import annotations

import hashlib
import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, local

import pytest
from django.apps import apps
from django.db import DatabaseError, close_old_connections, connection, transaction

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.target import cohort_probe_challenges as challenges
from django_ray.target.attestation import (
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)
from django_ray.target.cohort_probe_challenges import (
    IssuedProbeChallenge,
    ProbeChallengeError,
    ProbeChallengeRejection,
    _consume_locked_probe_challenge,
    _lock_probe_challenge_for_completion,
    consume_ray_target_probe_challenge,
    issue_ray_target_probe_challenge,
    replace_ray_target_probe_challenge,
)

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 9, 12, 1, 0, tzinfo=UTC)
CONFIGURATION = "sha256:" + "a" * 64


def _target(key="primary", *, family=RayRunnerFamily.RAY_CORE):
    runtime = RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14)
    expectation = RayTargetExpectation(key, family, "session_" + key, 1, runtime)
    target = RayTarget.objects.create(
        target_key=key,
        runner_family=family.value,
        cluster_session=expectation.cluster_session,
        created_at=NOW - timedelta(minutes=2),
        **asdict(runtime),
    )
    _policy(target, expectation, "draining")
    return expectation


def _policy(target, expectation, state):
    return RayTargetPolicyRevision.objects.create(
        target=target,
        revision=expectation.policy_revision,
        desired_state=state,
        expectation_schema_version=1,
        expectation_json=encode_ray_target_expectation(expectation),
        expectation_digest=ray_target_expectation_digest(expectation),
        created_at=NOW - timedelta(seconds=1),
    )


def _lease(key="manager"):
    lease = TaskWorkerLease.objects.create(
        worker_id=key,
        hostname="manager-host",
        pid=100,
        started_at=NOW - timedelta(minutes=1),
        last_heartbeat_at=NOW,
        capability_schema_version=1,
        django_ray_version="0.5.0",
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
    )
    return lease, WorkerLeaseIdentity(key, lease.hostname, lease.pid, lease.started_at)


def _configuration(name):
    return (
        CONFIGURATION
        if name == "primary"
        else "sha256:" + hashlib.sha256(name.encode()).hexdigest()
    )


def _issue(identity, target="primary", **changes):
    arguments = {"configuration_digest": _configuration(target), "now": NOW}
    arguments.update(changes)
    revision = arguments.pop("expected_revision", 0)
    if revision:
        row = RayTargetProbeChallenge.objects.get(lease_id=identity.worker_id)
        arguments.setdefault("expected_target_policy_id", row.expected_target_policy_id)
        arguments.setdefault("expected_configuration_digest", row.configuration_digest)
        return replace_ray_target_probe_challenge(
            identity,
            row.pk,
            expected_revision=revision,
            **arguments,
        )
    arguments.setdefault("runner_family", RayRunnerFamily.RAY_CORE)
    return issue_ray_target_probe_challenge(identity, **arguments)


def _consume(identity, issued, **changes):
    receipt = issued.receipt
    arguments = {
        "configuration_digest": receipt.configuration_digest,
        "expected_revision": receipt.revision,
        "nonce": issued.nonce,
        "now": NOW + timedelta(seconds=1),
    }
    arguments.update(changes)
    return consume_ray_target_probe_challenge(identity, receipt.challenge_id, **arguments)


@pytest.fixture
def pending():
    lease, identity = _lease()
    return lease, identity, _issue(identity)


def _refused(reason):
    return pytest.raises(ProbeChallengeError, match=f": {reason}$")


def test_issue_and_consume_do_not_advertise_or_renew_lease(pending):
    lease, identity, issued = pending
    row = RayTargetProbeChallenge.objects.get(pk=issued.receipt.challenge_id)
    assert row.nonce_digest == hashlib.sha256(issued.nonce.encode("ascii")).hexdigest()
    assert issued.nonce not in repr(issued)
    assert issued.nonce not in str(row)
    assert issued.receipt.expires_at == NOW + timedelta(seconds=300)
    consumed = _consume(identity, issued)
    assert consumed.revision == 2
    assert consumed.consumed_at == NOW + timedelta(seconds=1)
    assert not RayWorkerTargetCapability.objects.exists()
    assert not RayTargetAttestationRevision.objects.exists()
    assert not RayTarget.objects.exists()
    assert issued.receipt.expected_target_policy_id is None
    lease.refresh_from_db()
    assert lease.last_heartbeat_at == NOW
    with _refused("already_consumed"):
        _consume(identity, issued, expected_revision=2)
    with _refused("challenge_changed"):
        _consume(identity, issued)


def test_consumed_replacement_rotates_nonce_and_configuration(pending):
    _lease_row, identity, issued = pending
    consumed = _consume(identity, issued)
    replacement = _issue(
        identity,
        expected_revision=consumed.revision,
        expected_nonce=issued.nonce,
        configuration_digest="sha256:" + "b" * 64,
        now=NOW + timedelta(seconds=2),
    )
    assert replacement.nonce != issued.nonce
    assert replacement.receipt.challenge_id == issued.receipt.challenge_id
    assert replacement.receipt.revision == 3
    assert replacement.receipt.consumed_at is None
    with _refused("nonce_changed"):
        _consume(identity, replacement, nonce=issued.nonce, now=NOW + timedelta(seconds=3))
    with _refused("configuration_changed"):
        _consume(
            identity,
            replacement,
            configuration_digest=CONFIGURATION,
            now=NOW + timedelta(seconds=3),
        )
    assert _consume(identity, replacement, now=NOW + timedelta(seconds=3)).revision == 4


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"expected_revision": 4}, "challenge_changed"),
        ({"nonce": "b" * 64}, "nonce_changed"),
        ({"configuration_digest": "sha256:" + "b" * 64}, "configuration_changed"),
        ({"now": NOW - timedelta(microseconds=1)}, "clock_regression"),
        ({"nonce": "unbounded-or-malformed"}, "invalid_argument"),
        ({"configuration_digest": "not-a-digest"}, "invalid_argument"),
        ({"expected_revision": True}, "invalid_argument"),
        ({"now": NOW.replace(tzinfo=None)}, "invalid_argument"),
    ],
)
def test_consume_rejects_stale_or_malformed_identity(pending, changes, reason):
    _lease_row, identity, issued = pending
    with _refused(reason):
        _consume(identity, issued, **changes)
    assert RayTargetProbeChallenge.objects.get().revision == 1


def test_expiry_is_exclusive_but_expired_challenge_can_be_replaced(pending):
    lease, identity, issued = pending
    now = issued.receipt.expires_at
    TaskWorkerLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=now)
    with _refused("expired"):
        _consume(identity, issued, now=now)
    replacement = _issue(
        identity,
        expected_revision=1,
        expected_nonce=issued.nonce,
        now=now,
    )
    assert replacement.receipt.revision == 2
    assert replacement.nonce != issued.nonce


@pytest.mark.parametrize("field,value", [("hostname", "other"), ("pid", 101)])
def test_forged_lease_incarnation_cannot_consume(pending, field, value):
    _lease_row, identity, issued = pending
    with _refused("lease_unavailable"):
        _consume(replace(identity, **{field: value}), issued)


@pytest.mark.parametrize(
    "changes",
    [
        {"is_active": False},
        {"stopped_at": NOW},
        {"hostname": "replacement"},
        {"pid": 101},
        {"started_at": NOW + timedelta(seconds=1)},
    ],
)
def test_lease_shutdown_or_reincarnation_discards_pending_challenges(pending, changes):
    lease, identity, issued = pending
    TaskWorkerLease.objects.filter(pk=lease.pk).update(**changes)
    assert not RayTargetProbeChallenge.objects.exists()
    with _refused("lease_unavailable"):
        _consume(identity, issued)


def test_detached_and_stale_lease_are_not_probe_capacity(pending):
    lease, identity, issued = pending
    TaskWorkerLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=NOW - timedelta(days=1))
    with _refused("lease_unavailable"):
        _consume(identity, issued)
    lease.delete()
    assert not RayTargetProbeChallenge.objects.exists()
    with _refused("lease_unavailable"):
        _issue(identity)


def test_policy_change_invalidates_refresh_and_requires_explicit_replacement():
    expectation = _target()
    _lease_row, identity = _lease()
    selected = RayTargetPolicyRevision.objects.get(target_id="primary", revision=1)
    issued = _issue(identity, expected_target_policy_id=selected.pk)
    next_policy = _policy(selected.target, replace(expectation, policy_revision=2), "active")
    with _refused("policy_changed"):
        _consume(identity, issued)
    replacement = _issue(
        identity,
        expected_target_policy_id=next_policy.pk,
        expected_revision=1,
        expected_nonce=issued.nonce,
    )
    assert _consume(identity, replacement).revision == 3


def test_known_target_cannot_be_forgotten_under_unchanged_configuration():
    _target()
    _lease_row, identity = _lease()
    selected = RayTargetPolicyRevision.objects.get(target_id="primary", revision=1)
    issued = _issue(identity, expected_target_policy_id=selected.pk)
    with _refused("target_unavailable"):
        _issue(
            identity,
            expected_revision=1,
            expected_nonce=issued.nonce,
            expected_target_policy_id=None,
        )
    replacement = _issue(
        identity,
        expected_revision=1,
        expected_nonce=issued.nonce,
        expected_target_policy_id=None,
        configuration_digest=_configuration("changed-endpoint"),
    )
    assert replacement.receipt.expected_target_policy_id is None


@pytest.mark.parametrize("ttl", [0, -1, 601, True, 1.5])
def test_unbounded_ttl_is_rejected(ttl):
    _target()
    _lease_row, identity = _lease()
    with _refused("invalid_argument"):
        _issue(identity, ttl_seconds=ttl)


def test_minimum_and_maximum_ttl_and_clock_regression(pending):
    lease, identity, issued = pending
    replacement = _issue(
        identity, expected_revision=1, expected_nonce=issued.nonce, ttl_seconds=600
    )
    assert replacement.receipt.expires_at == NOW + timedelta(seconds=600)
    replacement = _issue(
        identity,
        expected_revision=2,
        expected_nonce=replacement.nonce,
        ttl_seconds=1,
        now=NOW + timedelta(seconds=5),
    )
    assert replacement.receipt.expires_at == NOW + timedelta(seconds=6)
    with _refused("clock_regression"):
        _issue(identity, expected_revision=3, expected_nonce=replacement.nonce, now=NOW)
    TaskWorkerLease.objects.filter(pk=lease.pk).update(
        last_heartbeat_at=NOW + timedelta(seconds=10)
    )
    with _refused("clock_regression"):
        _consume(identity, replacement, now=NOW + timedelta(seconds=5))


def test_creation_and_replacement_are_revision_fenced(pending):
    _lease_row, identity, issued = pending
    with _refused("challenge_changed"):
        _issue(identity)
    with _refused("nonce_changed"):
        _issue(identity, expected_revision=1, expected_nonce="b" * 64)
    with _refused("invalid_argument"):
        _issue(identity, runner_family="ray_job")
    with _refused("invalid_argument"):
        _issue(identity, expected_revision=1, expected_nonce=None)


def test_core_and_jobs_slots_keep_existing_limits():
    _lease_row, identity = _lease()
    _target()
    _target("second")
    _issue(identity)
    with _refused("limit_exceeded"):
        _issue(identity, "second")
    _lease_row, jobs_identity = _lease("jobs-manager")
    for number in range(64):
        key = f"jobs-{number}"
        _target(key, family=RayRunnerFamily.RAY_JOB)
        _issue(jobs_identity, key, runner_family=RayRunnerFamily.RAY_JOB)
    assert RayTargetProbeChallenge.objects.filter(lease_id="jobs-manager").count() == 64
    _target("overflow", family=RayRunnerFamily.RAY_JOB)
    with _refused("limit_exceeded"):
        _issue(jobs_identity, "overflow", runner_family=RayRunnerFamily.RAY_JOB)
    with _refused("limit_exceeded"):
        _issue(identity, "jobs-0", runner_family=RayRunnerFamily.RAY_JOB)


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 4},
        {"schema_version": 2},
        {"configuration_digest": "sha256:" + "B" * 64, "revision": 2},
        {"nonce_digest": "invalid", "revision": 2},
        {"lease_hostname": "other", "revision": 2},
        {"expires_at": NOW + timedelta(seconds=601), "revision": 2},
        {"consumed_at": NOW + timedelta(seconds=301), "revision": 2},
        {"consumed_at": NOW - timedelta(seconds=1), "revision": 2},
        {"configuration_digest": "sha256:" + "b" * 64, "revision": 2},
    ],
)
def test_database_fences_reject_direct_invalid_updates(pending, changes):
    _lease_row, _identity, issued = pending
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeChallenge.objects.filter(pk=issued.receipt.challenge_id).update(**changes)
    assert RayTargetProbeChallenge.objects.get().revision == 1


def test_database_consumption_cannot_be_rewritten(pending):
    _lease_row, identity, issued = pending
    _consume(identity, issued)
    for changes in (
        {"consumed_at": None, "revision": 3},
        {"consumed_at": NOW + timedelta(seconds=2), "revision": 3},
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            RayTargetProbeChallenge.objects.update(**changes)


def test_nested_transaction_cannot_weaken_durable_boundary(pending):
    _lease_row, identity, issued = pending
    with transaction.atomic(), _refused("persistence_refused"):
        _consume(identity, issued)


def test_locked_helpers_share_positive_publication_rollback(pending):
    _lease_row, identity, issued = pending
    arguments = {
        "configuration_digest": issued.receipt.configuration_digest,
        "expected_revision": issued.receipt.revision,
        "nonce": issued.nonce,
        "now": NOW + timedelta(seconds=1),
    }
    with _refused("persistence_refused"):
        _lock_probe_challenge_for_completion(identity, issued.receipt.challenge_id, **arguments)
    with pytest.raises(RuntimeError, match="publication failed"), transaction.atomic(durable=True):
        locked = _lock_probe_challenge_for_completion(
            identity,
            issued.receipt.challenge_id,
            **arguments,
        )
        _consume_locked_probe_challenge(locked, now=arguments["now"])
        raise RuntimeError("publication failed")
    assert RayTargetProbeChallenge.objects.get().revision == 1
    assert RayTargetProbeChallenge.objects.get().consumed_at is None
    assert _consume(identity, issued).revision == 2


def test_jobs_challenges_cannot_cross_endpoint_bindings():
    _lease_row, identity = _lease()
    first = _issue(identity, runner_family=RayRunnerFamily.RAY_JOB)
    second = _issue(identity, "second", runner_family=RayRunnerFamily.RAY_JOB)
    with _refused("configuration_changed"):
        _consume(identity, second, configuration_digest=first.receipt.configuration_digest)
    with _refused("nonce_changed"):
        _consume(identity, second, nonce=first.nonce)
    assert _consume(identity, first).revision == 2
    assert RayTargetProbeChallenge.objects.get(pk=second.receipt.challenge_id).consumed_at is None


@pytest.mark.parametrize("operation", ["consume", "replace"])
@pytest.mark.postgresql
def test_postgresql_concurrent_consumers_or_replacements_have_one_winner(pending, operation):
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    _lease_row, identity, issued = pending
    barrier = Barrier(2)

    def compete():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if operation == "consume":
                _consume(identity, issued)
            else:
                _issue(identity, expected_revision=1, expected_nonce=issued.nonce)
            return "success"
        except ProbeChallengeError as error:
            return error.classification
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compete) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == sorted(["success", ProbeChallengeRejection.CHALLENGE_CHANGED])
    assert RayTargetProbeChallenge.objects.get().revision == 2


def test_issued_result_type_is_explicit(pending):
    assert isinstance(pending[2], IssuedProbeChallenge)


def test_probe_rollback_guard_retains_pending_rows(pending):
    del pending
    migration = importlib.import_module("django_ray.migrations.0027_ray_target_probe_challenges")
    with pytest.raises(RuntimeError, match="probe challenges remain"), transaction.atomic():
        migration._guard_empty(apps, connection.schema_editor())


@pytest.mark.postgresql
def test_postgresql_challenge_binding_constraints_and_consumption(pending):
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    lease, identity, issued = pending
    for changes in (
        {"nonce_digest": "not-a-digest", "revision": 2},
        {"configuration_digest": "bad", "revision": 2},
        {"consumed_at": NOW + timedelta(seconds=301), "revision": 2},
        {"lease_hostname": "forged", "revision": 2},
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            RayTargetProbeChallenge.objects.filter(pk=issued.receipt.challenge_id).update(**changes)
    assert _consume(identity, issued).revision == 2
    TaskWorkerLease.objects.filter(pk=lease.pk).update(is_active=False)
    assert not RayTargetProbeChallenge.objects.exists()


def _crossed_refreshes():
    first = _target("target-a")
    second = _target("target-b")
    first_policy = RayTargetPolicyRevision.objects.get(target_id=first.target_key)
    second_policy = RayTargetPolicyRevision.objects.get(target_id=second.target_key)
    _first_lease, first_identity = _lease("manager-a")
    _second_lease, second_identity = _lease("manager-b")
    first_issued = _issue(first_identity, "first", expected_target_policy_id=first_policy.pk)
    second_issued = _issue(second_identity, "second", expected_target_policy_id=second_policy.pk)
    return (
        (first_identity, first_issued, second_policy, _configuration("second")),
        (second_identity, second_issued, first_policy, _configuration("first")),
    )


def _replace_crossed_refresh(identity, issued, policy, configuration_digest):
    return replace_ray_target_probe_challenge(
        identity,
        issued.receipt.challenge_id,
        expected_configuration_digest=issued.receipt.configuration_digest,
        configuration_digest=configuration_digest,
        expected_revision=issued.receipt.revision,
        expected_nonce=issued.nonce,
        expected_target_policy_id=policy.pk,
        now=NOW + timedelta(seconds=1),
    )


def test_changed_configuration_replacement_locks_only_its_requested_target(monkeypatch):
    arguments = _crossed_refreshes()[0]
    locked_targets = []
    original = challenges._locked_target

    def record_target(*, target_key, using, vendor):
        locked_targets.append(target_key)
        return original(target_key=target_key, using=using, vendor=vendor)

    monkeypatch.setattr(challenges, "_locked_target", record_target)
    replacement = _replace_crossed_refresh(*arguments)
    assert locked_targets == [arguments[2].target_id]
    assert replacement.receipt.expected_target_policy_id == arguments[2].pk
    assert replacement.nonce != arguments[1].nonce
    assert replacement.receipt.revision == 2


@pytest.mark.postgresql
def test_postgresql_crossed_target_replacements_do_not_lock_previous_targets(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("requires hosted PostgreSQL coordination database")
    arguments = _crossed_refreshes()
    barrier = Barrier(2)
    acquired = local()
    original = challenges._locked_target

    def synchronize_requested_target(*, target_key, using, vendor):
        target = original(target_key=target_key, using=using, vendor=vendor)
        if not getattr(acquired, "first", False):
            acquired.first = True
            # Both transactions now own different requested targets. Attempting
            # to lock the opposite previous target would deadlock PostgreSQL.
            barrier.wait(timeout=10)
        return target

    monkeypatch.setattr(challenges, "_locked_target", synchronize_requested_target)

    def replace_one(values):
        close_old_connections()
        try:
            return _replace_crossed_refresh(*values)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(replace_one, values) for values in arguments]
        results = [future.result(timeout=20) for future in futures]
    assert [result.receipt.expected_target_policy_id for result in results] == [
        values[2].pk for values in arguments
    ]
    assert all(result.receipt.revision == 2 for result in results)
