"""One cleaned-up Jobs reservation may rediscover without rebinding task history."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayTargetProbeJobReceipt,
    RayTaskCohortClaim,
    RayTaskExecution,
    RayTaskTargetBinding,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)
from django_ray.runtime.cohort_job import (
    CohortProbeJobLease,
    CohortProbeJobRequest,
    probe_job_request_digest,
)
from django_ray.runtime.cohort_job_entrypoint import (
    CohortProbeJobLaunch,
    probe_job_launch_entrypoint,
)
from django_ray.target import cohort_job_receipt_storage as receipts
from django_ray.target import cohort_job_retirement as retirement
from django_ray.target.attestation import RayRunnerFamily, RayRuntimeVersion
from django_ray.target.cohort_claim import CohortResolutionKind
from django_ray.target.cohort_job_control import cohort_probe_submitted_runtime_env_digest
from django_ray.target.cohort_job_receipt import (
    cohort_job_receipt_digest,
    encode_cohort_job_receipt,
)
from django_ray.target.cohort_probe_challenges import (
    ProbeChallengeError,
    consume_ray_target_probe_challenge,
    issue_ray_target_probe_challenge,
    replace_ray_target_probe_challenge,
)
from tests.integration.test_cohort_claim_storage import (
    _claim,
    _hold,
    _mutate,
    _ray_arguments,
)
from tests.integration.test_cohort_claim_storage import (
    case as _claim_case,
)
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import (
    storage as claim_storage,
)
from tests.integration.test_cohort_job_receipts import _codec_receipt, _receive
from tests.integration.test_cohort_probe_challenges import NOW, _issue, _lease, _policy, _target

pytestmark = pytest.mark.django_db(transaction=True)
ledger_case = _claim_case
ENDPOINT = "http://ray-head:8265"
ENVIRONMENT = {"env_vars": {"DJANGO_SETTINGS_MODULE": "tests.probe_settings"}}


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgresql", id="postgresql", marks=pytest.mark.postgresql),
    ]
)
def selected_database(request):
    if connection.vendor != request.param:
        pytest.skip(f"Requires {request.param}")


def _make_case(monkeypatch, *, known=True, package="0.5.0"):
    lease, identity = _lease()
    expected = _target("jobs", family=RayRunnerFamily.RAY_JOB) if known else None
    policy = RayTargetPolicyRevision.objects.get(target_id="jobs") if known else None
    issued = _issue(
        identity,
        runner_family=RayRunnerFamily.RAY_JOB,
        expected_target_policy_id=policy.pk if policy else None,
    )
    slot = issued.receipt
    request = CohortProbeJobRequest(
        slot.challenge_id,
        slot.revision,
        CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ),
        slot.configuration_digest,
        expected.target_key if expected else None,
        RayRunnerFamily.RAY_JOB,
        package,
        RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14),
        expected.cluster_session if expected else None,
        policy.pk if policy else None,
        1,
        slot.issued_at,
        slot.expires_at,
    )
    launch = CohortProbeJobLaunch(
        request,
        probe_job_request_digest(request),
        ENDPOINT,
        cohort_probe_submitted_runtime_env_digest(ENVIRONMENT),
        "tests.probe_settings",
    )
    case = SimpleNamespace(
        lease=lease,
        identity=identity,
        policy=policy,
        expected=expected,
        issued=issued,
        request=request,
        launch=launch,
        now=NOW,
    )
    monkeypatch.setattr(receipts, "_now", lambda: case.now)
    monkeypatch.setattr(retirement, "_clock", lambda: case.now)
    receipts.reserve_cohort_job_probe(
        identity,
        request,
        nonce=issued.nonce,
        jobs_endpoint=ENDPOINT,
        entrypoint=probe_job_launch_entrypoint(launch),
        submitted_runtime_env=ENVIRONMENT,
    )
    case.now = NOW + timedelta(seconds=5)
    return case


@pytest.fixture
def case(selected_database, monkeypatch):
    return _make_case(monkeypatch)


def _arguments(case):
    row = RayTargetProbeJobReceipt.objects.get(pk=case.request.challenge_id)
    slot = RayTargetProbeChallenge.objects.get(pk=row.pk)
    return {
        "expected_challenge_revision": slot.revision,
        "nonce": case.issued.nonce,
        "expected_reserved_at": row.reserved_at,
        "expected_receipt_digest": row.receipt_digest,
        "expected_received_at": row.received_at,
        "cleanup_confirmed": True,
        "cleanup_confirmed_at": case.now,
        "now": case.now,
    }


def _retire(case, **changes):
    arguments = _arguments(case) | changes
    identity = arguments.pop("identity", case.identity)
    launch = arguments.pop("launch", case.launch)
    return retirement.retire_and_reissue_cohort_job_probe(identity, launch, **arguments)


def _snapshot():
    return (
        list(RayTargetProbeChallenge.objects.order_by("pk").values()),
        list(RayTargetProbeJobReceipt.objects.order_by("pk").values()),
        list(RayTargetPolicyRevision.objects.order_by("pk").values()),
    )


def _refused(reason):
    return pytest.raises(retirement.CohortJobRetirementError, match=f": {reason}$")


def _write_receipt(case):
    case.now = NOW + timedelta(seconds=3)
    receipt = _codec_receipt(case.request)
    assert receipts.write_cohort_job_receipt(receipt)
    case.now = NOW + timedelta(seconds=5)
    return receipt


def _consume(case):
    consume_ray_target_probe_challenge(
        case.identity,
        case.request.challenge_id,
        configuration_digest=case.request.configuration_digest,
        expected_revision=case.request.challenge_revision,
        nonce=case.issued.nonce,
        now=NOW + timedelta(seconds=4),
    )


@pytest.mark.parametrize("state", ["pending", "received", "consumed", "expired"])
def test_retirement_reissues_fresh_discovery_without_changing_policy_or_lease(case, state):
    if state in {"received", "consumed", "expired"}:
        _write_receipt(case)
    if state in {"consumed", "expired"}:
        _consume(case)
    if state == "expired":
        case.now = NOW + timedelta(seconds=301)
        TaskWorkerLease.objects.filter(pk=case.lease.pk).update(last_heartbeat_at=case.now)
    policies = list(RayTargetPolicyRevision.objects.values())
    lease_values = TaskWorkerLease.objects.values().get(pk=case.lease.pk)
    issued = _retire(case)
    row = RayTargetProbeChallenge.objects.get()
    assert row.pk == issued.receipt.challenge_id > case.request.challenge_id
    assert row.revision == 1 and row.consumed_at is None
    assert row.expected_target_policy_id is None
    assert row.configuration_digest == case.request.configuration_digest
    assert issued.nonce != case.issued.nonce
    assert row.nonce_digest == hashlib.sha256(issued.nonce.encode("ascii")).hexdigest()
    assert issued.nonce not in repr(issued)
    assert row.issued_at == case.now and row.expires_at == case.now + timedelta(seconds=300)
    assert not RayTargetProbeJobReceipt.objects.exists()
    assert list(RayTargetPolicyRevision.objects.values()) == policies
    assert TaskWorkerLease.objects.values().get(pk=case.lease.pk) == lease_values
    assert not RayTargetAttestationRevision.objects.exists()
    assert not RayWorkerTargetCapability.objects.exists()


def test_unknown_target_reservation_can_be_retired_without_target_creation(
    selected_database, monkeypatch
):
    case = _make_case(monkeypatch, known=False)
    issued = _retire(case)
    assert issued.receipt.expected_target_policy_id is None
    assert not RayTarget.objects.exists()


def test_receipt_and_old_nonce_cannot_replay_after_new_discovery(case):
    old_receipt = _write_receipt(case)
    arguments = _arguments(case)
    replacement = _retire(case)
    after = _snapshot()
    with _refused("challenge_changed"):
        retirement.retire_and_reissue_cohort_job_probe(case.identity, case.launch, **arguments)
    with pytest.raises(receipts.CohortJobStorageError):
        receipts.write_cohort_job_receipt(old_receipt)
    with pytest.raises(ProbeChallengeError):
        consume_ray_target_probe_challenge(
            case.identity,
            replacement.receipt.challenge_id,
            configuration_digest=case.request.configuration_digest,
            expected_revision=1,
            nonce=case.issued.nonce,
            now=case.now,
        )
    assert _snapshot() == after


def test_ordinary_replace_and_raw_update_still_cannot_forget_known_target(case):
    with pytest.raises(ProbeChallengeError):
        replace_ray_target_probe_challenge(
            case.identity,
            case.request.challenge_id,
            expected_configuration_digest=case.request.configuration_digest,
            configuration_digest=case.request.configuration_digest,
            expected_revision=1,
            expected_nonce=case.issued.nonce,
            now=case.now,
            expected_target_policy_id=None,
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTargetProbeChallenge.objects.filter(pk=case.request.challenge_id).update(
            revision=2, expected_target_policy_id=None, nonce_digest="f" * 64
        )
    assert RayTargetProbeChallenge.objects.get().expected_target_policy_id == case.policy.pk


def test_obsolete_policy_is_not_a_retirement_blocker_and_drain_is_preserved(case):
    current = _policy(
        case.policy.target,
        replace(case.expected, policy_revision=2),
        "active",
    )
    _policy(current.target, replace(case.expected, policy_revision=3), "draining")
    before = list(RayTargetPolicyRevision.objects.order_by("pk").values())
    _retire(case)
    assert list(RayTargetPolicyRevision.objects.order_by("pk").values()) == before


@pytest.mark.parametrize("confirmation", [False, None, 1, "true"])
def test_cleanup_requires_an_explicit_true_trusted_parent_acknowledgement(case, confirmation):
    before = _snapshot()
    with _refused("cleanup_unconfirmed"):
        _retire(case, cleanup_confirmed=confirmation)
    assert _snapshot() == before


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"expected_challenge_revision": True}, "invalid"),
        ({"expected_challenge_revision": 0}, "invalid"),
        ({"expected_challenge_revision": 1 << 63}, "invalid"),
        ({"expected_challenge_revision": 2}, "challenge_changed"),
        ({"nonce": "a" * 64}, "nonce_changed"),
        ({"nonce": "secret"}, "invalid"),
        ({"ttl_seconds": True}, "invalid"),
        ({"ttl_seconds": 0}, "invalid"),
        ({"ttl_seconds": 601}, "invalid"),
        ({"expected_reserved_at": NOW + timedelta(seconds=1)}, "reservation_changed"),
        ({"expected_receipt_digest": "sha256:" + "a" * 64}, "invalid"),
        ({"expected_received_at": NOW + timedelta(seconds=2)}, "invalid"),
        ({"cleanup_confirmed_at": NOW - timedelta(seconds=1)}, "clock_regression"),
        ({"cleanup_confirmed_at": NOW + timedelta(seconds=6)}, "clock_regression"),
        ({"now": NOW.replace(tzinfo=None)}, "invalid"),
        ({"using": "nonexistent"}, "invalid"),
    ],
)
def test_invalid_or_stale_cas_has_no_partial_writes(case, changes, reason):
    before = _snapshot()
    with _refused(reason):
        _retire(case, **changes)
    assert _snapshot() == before


@pytest.mark.parametrize(
    "field", ["jobs_endpoint", "submitted_runtime_env_digest", "django_settings_module"]
)
def test_changed_canonical_launch_cannot_retire_original_reservation(case, field):
    value = {
        "jobs_endpoint": "http://another-head:8265",
        "submitted_runtime_env_digest": "sha256:" + "c" * 64,
        "django_settings_module": "other.settings",
    }[field]
    before = _snapshot()
    with _refused("reservation_changed"):
        _retire(case, launch=replace(case.launch, **{field: value}))
    assert _snapshot() == before


def test_late_receipt_write_invalidates_retained_empty_snapshot(case):
    arguments = _arguments(case)
    _write_receipt(case)
    before = _snapshot()
    with _refused("reservation_changed"):
        retirement.retire_and_reissue_cohort_job_probe(case.identity, case.launch, **arguments)
    assert _snapshot() == before


def test_receipt_cleanup_chronology_and_canonical_bytes_are_required(case):
    row = RayTargetProbeJobReceipt.objects.get()
    _receive(row)
    before = _snapshot()
    with _refused("reservation_changed"):
        _retire(case)
    assert _snapshot() == before


def test_cleanup_cannot_predate_the_received_observation(case):
    _write_receipt(case)
    before = _snapshot()
    with _refused("clock_regression"):
        _retire(case, cleanup_confirmed_at=NOW + timedelta(seconds=2))
    assert _snapshot() == before


@pytest.mark.parametrize("change", ["expired", "inactive", "incarnation"])
def test_changed_lease_cannot_reissue_and_never_gets_a_heartbeat(case, change):
    if change == "expired":
        case.now = NOW + timedelta(seconds=61)
    elif change == "inactive":
        TaskWorkerLease.objects.filter(pk=case.lease.pk).update(is_active=False)
    else:
        TaskWorkerLease.objects.filter(pk=case.lease.pk).update(hostname="replacement-host")
    before = _snapshot()
    with _refused("lease_unavailable"):
        _retire(case) if change == "expired" else retirement.retire_and_reissue_cohort_job_probe(
            case.identity,
            case.launch,
            expected_challenge_revision=1,
            nonce=case.issued.nonce,
            expected_reserved_at=NOW,
            expected_receipt_digest=None,
            expected_received_at=None,
            cleanup_confirmed=True,
            cleanup_confirmed_at=case.now,
            now=case.now,
        )
    assert _snapshot() == before
    assert TaskWorkerLease.objects.get().last_heartbeat_at == NOW


def test_package_mismatch_cannot_reissue(selected_database, monkeypatch):
    case = _make_case(monkeypatch, package="0.5.1")
    before = _snapshot()
    with _refused("lease_unavailable"):
        _retire(case)
    assert _snapshot() == before


@pytest.mark.parametrize("after", ["lock", "issue"])
def test_clock_recheck_after_final_locks_and_writes_rolls_back(case, monkeypatch, after):
    before = _snapshot()
    name = "_locked_reservation" if after == "lock" else "_issue_row"
    original = getattr(retirement, name)

    def delay(*args, **kwargs):
        value = original(*args, **kwargs)
        case.now += timedelta(seconds=60)
        return value

    monkeypatch.setattr(retirement, name, delay)
    with _refused("lease_unavailable"):
        _retire(case)
    assert _snapshot() == before


@pytest.mark.parametrize("failure", ["insert", "reused_nonce", "reused_id"])
def test_failed_reissue_restores_exact_old_reservation(case, monkeypatch, failure):
    _write_receipt(case)
    _consume(case)
    before = _snapshot()
    original = retirement._issue_row

    def issue(*args, **kwargs):
        assert not RayTargetProbeChallenge.objects.filter(pk=case.request.challenge_id).exists()
        if failure == "insert":
            raise DatabaseError("private storage diagnostic")
        value = original(*args, **kwargs)
        if failure == "reused_id":
            return replace(
                value, receipt=replace(value.receipt, challenge_id=case.request.challenge_id)
            )
        return replace(value, nonce=case.issued.nonce)

    monkeypatch.setattr(retirement, "_issue_row", issue)
    with _refused("persistence_refused") as caught:
        _retire(case)
    assert "private" not in str(caught.value)
    assert _snapshot() == before


def test_nested_and_manual_transactions_cannot_weaken_outer_commit(case):
    before = _snapshot()
    with transaction.atomic(), _refused("transaction_open"):
        _retire(case)
    connection.set_autocommit(False)
    try:
        with _refused("transaction_open"):
            _retire(case)
    finally:
        connection.rollback()
        connection.set_autocommit(True)
    assert _snapshot() == before


@pytest.mark.parametrize("clock", [None, NOW.replace(tzinfo=None), NOW, "raises"])
def test_invalid_or_regressing_fresh_clock_never_retires(case, monkeypatch, clock):
    def read():
        if clock == "raises":
            raise RuntimeError("private clock diagnostic")
        return clock

    monkeypatch.setattr(retirement, "_clock", read)
    before = _snapshot()
    with _refused("clock_regression"):
        _retire(case)
    assert _snapshot() == before


def test_new_challenge_expiry_before_commit_restores_old_pair(case, monkeypatch):
    original = retirement._issue_row
    before = _snapshot()

    def delayed_issue(*args, **kwargs):
        issued = original(*args, **kwargs)
        case.now += timedelta(seconds=1)
        return issued

    monkeypatch.setattr(retirement, "_issue_row", delayed_issue)
    with _refused("expired"):
        _retire(case, ttl_seconds=1)
    assert _snapshot() == before


@pytest.mark.parametrize("change", ["missing-policy", "different-runtime", "different-lease"])
def test_canonical_request_substitution_cannot_retire_original_slot(case, change):
    request = case.request
    if change == "missing-policy":
        request = replace(request, expected_target_policy_id=case.policy.pk + 100)
    elif change == "different-runtime":
        request = replace(
            request, expected_runtime=replace(request.expected_runtime, python_patch=15)
        )
    else:
        request = replace(request, lease=replace(request.lease, hostname="different-host"))
    launch = replace(case.launch, request=request, request_digest=probe_job_request_digest(request))
    before = _snapshot()
    reason = "invalid" if change == "different-lease" else "target_changed"
    with _refused(reason):
        _retire(case, launch=launch)
    assert _snapshot() == before


def test_consumed_slot_cannot_be_retired_by_an_earlier_clock(case):
    _consume(case)
    case.now = NOW + timedelta(seconds=3)
    before = _snapshot()
    with _refused("clock_regression"):
        _retire(case)
    assert _snapshot() == before


def test_cleanup_may_precede_later_database_consumption(case):
    _write_receipt(case)
    _consume(case)
    issued = _retire(case, cleanup_confirmed_at=NOW + timedelta(seconds=3))
    assert issued.receipt.challenge_id > case.request.challenge_id


def test_canonical_receipt_must_observe_after_actual_reservation(case):
    row = RayTargetProbeJobReceipt.objects.get()
    values = RayTargetProbeJobReceipt.objects.values().get(pk=row.pk)
    row.delete()
    values["reserved_at"] = NOW + timedelta(seconds=2)
    row = RayTargetProbeJobReceipt.objects.create(**values)
    receipt = _codec_receipt(case.request)
    assert (
        _receive(
            row,
            receipt_json=encode_cohort_job_receipt(receipt),
            receipt_digest=cohort_job_receipt_digest(receipt),
            received_at=NOW + timedelta(seconds=3),
        )
        == 1
    )
    before = _snapshot()
    with _refused("clock_regression"):
        _retire(case)
    assert _snapshot() == before


def test_missing_reservation_cannot_retire_a_current_challenge(case):
    arguments = _arguments(case)
    RayTargetProbeJobReceipt.objects.get().delete()
    before = _snapshot()
    with _refused("reservation_changed"):
        retirement.retire_and_reissue_cohort_job_probe(case.identity, case.launch, **arguments)
    assert _snapshot() == before


def test_retirement_preserves_sibling_proof_and_held_original_execution(
    selected_database, ledger_case, monkeypatch
):
    case = ledger_case
    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    record = _claim(case, **arguments)
    record = _mutate(
        case,
        record,
        claim_storage.prepare_cohort_claim,
        request_digest="sha256:" + "c" * 64,
    )
    record = _mutate(case, record, claim_storage.mark_cohort_claim_dispatched)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(
        ray_job_id="original-application-handle"
    )
    record = _hold(case, record)
    sibling = issue_ray_target_probe_challenge(
        case.owner,
        "sha256:" + "d" * 64,
        runner_family=RayRunnerFamily.RAY_JOB,
        now=case.now,
        expected_target_policy_id=case.job_request.expected_target_policy_id,
    )
    launch = CohortProbeJobLaunch(
        case.job_request,
        probe_job_request_digest(case.job_request),
        ENDPOINT,
        cohort_probe_submitted_runtime_env_digest(ENVIRONMENT),
        "tests.probe_settings",
    )
    operation = SimpleNamespace(
        identity=case.owner,
        request=case.job_request,
        launch=launch,
        issued=case.job_issued,
        now=case.now,
    )
    monkeypatch.setattr(retirement, "_clock", lambda: case.now)
    models = (
        RayTaskExecution,
        RayTaskTargetBinding,
        RayTaskCohortClaim,
        RayTarget,
        RayTargetPolicyRevision,
        RayTargetAttestationRevision,
        RayWorkerTargetCapability,
    )
    before = [list(model.objects.order_by("pk").values()) for model in models]
    sibling_before = RayTargetProbeChallenge.objects.values().get(pk=sibling.receipt.challenge_id)
    issued = _retire(operation)
    assert issued.receipt.expected_target_policy_id is None
    assert [list(model.objects.order_by("pk").values()) for model in models] == before
    assert (
        RayTargetProbeChallenge.objects.values().get(pk=sibling.receipt.challenge_id)
        == sibling_before
    )
    assert not RayTargetProbeJobReceipt.objects.filter(pk=case.job_request.challenge_id).exists()
    # Authentic late completion relies on immutable generation facts, not the
    # retired qualification receipt or its TTL. Keep the original hold audit.
    case.now += timedelta(seconds=1)
    with transaction.atomic():
        resolved = claim_storage.resolve_cohort_claim(
            case.owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            now=case.now,
            kind=CohortResolutionKind.APPLICATION_COMPLETED,
            evidence_digest="sha256:" + "e" * 64,
        )
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="SUCCEEDED")
    assert resolved.facts == record.facts
    retained = RayTaskCohortClaim.objects.get(pk=record.claim_id)
    assert retained.disposition == "RESOLVED" and retained.hold_reason == "transport_uncertain"


@pytest.mark.postgresql
def test_postgresql_retirement_has_one_exact_cas_winner(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locking")
    case = _make_case(monkeypatch)
    arguments = _arguments(case)
    barrier = Barrier(2)

    def retire():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            retirement.retire_and_reissue_cohort_job_probe(case.identity, case.launch, **arguments)
            return "success"
        except retirement.CohortJobRetirementError as error:
            return error.reason.value
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(retire) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == [
            "challenge_changed",
            "success",
        ]
    assert RayTargetProbeChallenge.objects.count() == 1
    assert RayTargetProbeChallenge.objects.get().pk != case.request.challenge_id
    assert not RayTargetProbeJobReceipt.objects.exists()
