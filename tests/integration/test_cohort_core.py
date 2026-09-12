"""Parent DB composition with fake observations and one marked native Core case."""

import os
import socket
import threading
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import connection
from django.db.backends.utils import CursorWrapper

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    TaskWorkerLease,
)
from django_ray.runner import cohort_core as adapter
from django_ray.runner.cohort_qualification import (
    CohortQualificationError,
    CohortQualificationLifecycle,
)
from django_ray.runtime.cohort_job import CohortProbeJobLease
from django_ray.target import cohort_publication as publication
from django_ray.target import probe
from django_ray.target.attestation import (
    RayRunnerFamily,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)
from django_ray.target.cohort_probe import derive_cohort_target_key
from tests.integration.test_cohort_probe_challenges import NOW, _lease, _target
from tests.unit.test_cohort_qualification import DIGEST, RUNTIME, alias, attestation

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def database_backend(request):
    if connection.vendor != request.param:
        pytest.skip(f"Requires {request.param}")


@pytest.fixture
def case(monkeypatch):
    lease, identity = _lease()
    state = SimpleNamespace(now=NOW, session="session_test", queries=[], observations=[])
    owner = threading.get_ident()
    original = CursorWrapper.execute

    def execute(cursor, *args, **kwargs):
        assert threading.get_ident() == owner, "Observation daemon must not access the database"
        state.queries.append(threading.get_ident())
        return original(cursor, *args, **kwargs)

    monkeypatch.setattr(CursorWrapper, "execute", execute)
    monkeypatch.setattr(adapter, "_supported_connection", lambda *_args: object())

    def caller_observation(_ray):
        assert threading.get_ident() != owner, "Session discovery must not run on parent"
        return SimpleNamespace(session_name=state.session)

    monkeypatch.setattr(probe, "_current_caller_observation", caller_observation)
    monkeypatch.setattr(publication, "_now", lambda: state.now)
    monkeypatch.setattr(publication, "_local_runtime", lambda _ray: ("0.5.0", RUNTIME))

    def observe(**kwargs):
        assert threading.get_ident() != owner
        assert kwargs["owned_cleanup"] is True
        assert probe._current_caller_observation(None).session_name == state.session
        state.observations.append(kwargs)
        state.now += timedelta(seconds=1)
        proof = attestation(observed=state.now)
        from django_ray.target.attestation import (
            build_ray_cluster_attestation,
            build_ray_node_observation,
        )

        expected = replace(
            proof.expectation,
            target_key=kwargs["target_key"]
            or derive_cohort_target_key(RayRunnerFamily.RAY_CORE, state.session),
            cluster_session=state.session,
            policy_revision=kwargs["policy_revision"],
        )
        return build_ray_cluster_attestation(
            expectation=expected,
            boundary=proof.boundary,
            nodes=(
                build_ray_node_observation(
                    node_id="1" * 56, cluster_session=state.session, runtime=RUNTIME
                ),
            ),
            observed_at=state.now,
            expires_at=state.now + timedelta(seconds=30),
        )

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)
    lifecycle = CohortQualificationLifecycle(
        CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ),
        "0.5.0",
        RUNTIME,
        RayRunnerFamily.RAY_CORE,
        monotonic=lambda: 100.0 + (state.now - NOW).total_seconds(),
        wall_clock=lambda: state.now,
    )
    lifecycle.configure_aliases([alias(), alias("second")])
    manager = adapter.CoreCohortManagerAdapter(lifecycle, wall_clock=lambda: state.now)
    return SimpleNamespace(manager=manager, lifecycle=lifecycle, state=state, lease=lease)


def finish(case):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = case.manager.poll()
        if value is not None:
            return value
        time.sleep(0.001)
    raise AssertionError("Resource-free observation did not finish")


def test_first_discovery_requires_a_fresh_active_policy_probe(case, database_backend):
    case.manager.begin(DIGEST, 1)
    first = finish(case)
    assert first.shared.activation_policy_id is not None
    assert first.shared.desired_state == "draining"
    assert case.manager.eligible_aliases() == ()
    case.manager.begin(DIGEST, 1)
    second = finish(case)
    assert second.shared.target_policy_id == first.shared.activation_policy_id
    assert second.shared.activation_policy_id is None
    assert second.shared.attestation.expectation.policy_revision == 2
    assert len(case.manager.eligible_aliases()) == 2
    assert len(case.state.observations) == 2
    assert all(q == threading.get_ident() for q in case.state.queries)
    assert RayTargetProbeChallenge.objects.get().revision == 4


def test_operator_named_drained_target_remains_drained(case, database_backend):
    expectation = _target("operator-target")
    case.state.session = expectation.cluster_session
    case.manager.begin(DIGEST, 1)
    result = finish(case)
    assert result.shared.attestation.expectation == expectation
    assert result.shared.activation_policy_id is None
    assert case.manager.eligible_aliases() == ()
    assert list(RayTarget.objects.values_list("target_key", flat=True)) == ["operator-target"]
    assert [p["target_key"] for p in case.state.observations] == [None, "operator-target"]
    assert RayTargetAttestationRevision.objects.count() == 1
    assert RayTargetProbeChallenge.objects.get().revision == 3


def test_first_discovery_of_existing_active_target_reprobes_its_current_policy(case):
    expected = attestation().expectation
    RayTarget.objects.create(
        target_key=expected.target_key,
        runner_family=expected.runner_family.value,
        cluster_session=expected.cluster_session,
        created_at=NOW - timedelta(minutes=2),
        **asdict(RUNTIME),
    )
    from tests.integration.test_cohort_probe_challenges import _policy

    active = replace(expected, policy_revision=2)
    _policy(RayTarget.objects.get(), expected, "draining")
    _policy(RayTarget.objects.get(), active, "active")
    case.manager.begin(DIGEST, 1)
    result = finish(case)
    assert result.shared.attestation.expectation == active
    assert result.shared.activation_policy_id is None
    assert len(case.manager.eligible_aliases()) == 2
    assert [p["policy_revision"] for p in case.state.observations] == [1, 2]
    assert RayTargetAttestationRevision.objects.count() == 1


def test_policy_churn_allows_only_one_automatic_followup_without_publishing(case, monkeypatch):
    expected = _target("operator-target")
    case.state.session = expected.cluster_session
    original = case.manager._retained_policy
    lookups = 0

    def lookup(session, now):
        nonlocal lookups
        lookups += 1
        if lookups == 3:
            from tests.integration.test_cohort_probe_challenges import _policy

            _policy(RayTarget.objects.get(), replace(expected, policy_revision=2), "draining")
        return original(session, now)

    monkeypatch.setattr(case.manager, "_retained_policy", lookup)
    case.manager.begin(DIGEST, 1)
    with pytest.raises(adapter.CoreCohortAdapterError, match="target_unavailable"):
        finish(case)
    assert case.lifecycle.outstanding is None
    assert len(case.state.observations) == 2
    assert RayTargetProbeChallenge.objects.get().consumed_at is None
    assert not RayTargetAttestationRevision.objects.exists()
    assert case.manager.eligible_aliases() == ()


def test_new_session_at_same_descriptor_requires_a_new_epoch(case):
    _target("operator-target")
    case.state.session = "session_operator-target"
    case.manager.begin(DIGEST, 1)
    finish(case)
    previous_digest = RayTargetProbeChallenge.objects.get().configuration_digest
    case.state.session = "session_reconnected"
    ticket = case.manager.begin(DIGEST, 1)
    with pytest.raises(CohortQualificationError):
        finish(case)
    assert case.manager.eligible_aliases() == ()
    assert RayTarget.objects.count() == 1
    assert case.lifecycle.outstanding is ticket
    case.manager.confirm_cleanup(ticket, lambda: True)
    case.manager.begin(DIGEST, 2)
    result = finish(case)
    assert result.shared.attestation.expectation.cluster_session == case.state.session
    assert result.shared.activation_policy_id is not None
    assert RayTarget.objects.count() == 2
    assert RayTargetProbeChallenge.objects.get().configuration_digest != previous_digest


def test_failed_thread_stays_quarantined_until_independent_cleanup(case, monkeypatch):
    def failed(**_kwargs):
        raise RuntimeError("private failure payload")

    monkeypatch.setattr(publication, "observe_current_cohort_target", failed)
    ticket = case.manager.begin(DIGEST, 1)
    with pytest.raises(CohortQualificationError):
        finish(case)
    with pytest.raises(adapter.CoreCohortAdapterError):
        case.manager.begin(DIGEST, 2)
    with pytest.raises(CohortQualificationError):
        case.manager.confirm_cleanup(ticket, lambda: False)
    assert case.lifecycle.outstanding is ticket
    assert RayTargetProbeChallenge.objects.get().revision == 1
    assert case.manager.eligible_aliases() == ()
    case.manager.confirm_cleanup(ticket, lambda: True)
    assert case.lifecycle.outstanding is None


def test_expired_observation_never_publishes_and_exit_does_not_release(case):
    ticket = case.manager.begin(DIGEST, 1, timeout_seconds=1)
    with pytest.raises(CohortQualificationError):
        finish(case)
    assert case.lifecycle.outstanding is ticket
    assert not RayTarget.objects.exists()
    assert not RayTargetAttestationRevision.objects.exists()


def test_lost_publication_response_cannot_reconstruct_positive_cache(case, monkeypatch):
    monkeypatch.setattr(
        case.manager, "_convert", lambda *_args: (_ for _ in ()).throw(ValueError())
    )
    ticket = case.manager.begin(DIGEST, 1)
    with pytest.raises(CohortQualificationError):
        finish(case)
    assert RayTargetProbeChallenge.objects.get().consumed_at is not None
    assert case.manager.eligible_aliases() == ()
    assert case.lifecycle.outstanding is ticket


def test_substituted_publication_state_cannot_seed_positive_cache(case, monkeypatch):
    original = publication.publish_prepared_core_cohort_probe

    def publish(*args, **kwargs):
        return replace(original(*args, **kwargs), desired_state=RayTargetDesiredState.ACTIVE)

    monkeypatch.setattr(publication, "publish_prepared_core_cohort_probe", publish)
    ticket = case.manager.begin(DIGEST, 1)
    with pytest.raises(CohortQualificationError, match="publication_failed"):
        finish(case)
    assert RayTargetProbeChallenge.objects.get().consumed_at is not None
    assert case.lifecycle.outstanding is ticket
    assert case.manager.eligible_aliases() == ()


def test_later_explicit_drain_is_never_automatically_reactivated(case):
    case.manager.begin(DIGEST, 1)
    first = finish(case)
    target = first.shared.attestation.expectation.target_key
    drained = replace(first.shared.attestation.expectation, policy_revision=3)
    RayTargetPolicyRevision.objects.create(
        target_id=target,
        revision=3,
        desired_state=RayTargetDesiredState.DRAINING,
        expectation_schema_version=1,
        expectation_json=encode_ray_target_expectation(drained),
        expectation_digest=ray_target_expectation_digest(drained),
        created_at=case.state.now,
    )
    case.manager.begin(DIGEST, 1)
    result = finish(case)
    assert result.shared.attestation.expectation.policy_revision == 3
    assert result.shared.activation_policy_id is None
    assert result.shared.desired_state == "draining"
    assert case.manager.eligible_aliases() == ()


def test_hung_observation_never_blocks_parent_or_accepts_thread_exit_as_cleanup(case, monkeypatch):
    started, release, exited = threading.Event(), threading.Event(), threading.Event()

    def observe(**_kwargs):
        started.set()
        try:
            assert release.wait(2)
            raise RuntimeError("controlled observation exit")
        finally:
            exited.set()

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)
    ticket = case.manager.begin(DIGEST, 1, timeout_seconds=5)
    try:
        assert started.wait(1)
        assert case.manager.poll() is None
        case.state.now += timedelta(seconds=5)
        with pytest.raises(CohortQualificationError):
            case.manager.poll()
        with pytest.raises(CohortQualificationError):
            case.manager.confirm_cleanup(ticket, lambda: True)
        assert case.lifecycle.outstanding is ticket
    finally:
        release.set()
        assert exited.wait(1)
    with pytest.raises(CohortQualificationError):
        case.manager.confirm_cleanup(ticket, lambda: False)
    assert case.lifecycle.outstanding is ticket
    case.manager.confirm_cleanup(ticket, lambda: True)


def test_inactive_lease_invalidates_cache_before_observation(case):
    case.lease.is_active = False
    case.lease.stopped_at = case.state.now
    case.lease.save(update_fields=("is_active", "stopped_at"))
    from django_ray.target.cohort_probe_challenges import ProbeChallengeError

    with pytest.raises(ProbeChallengeError):
        case.manager.begin(DIGEST, 1)
    assert case.lifecycle.outstanding is None
    assert not case.state.observations
    assert not RayTargetProbeChallenge.objects.exists()


def test_unsupported_context_refuses_before_issuing_or_starting(case, monkeypatch):
    def unsupported(*_args):
        raise adapter.CoreCohortAdapterError(adapter.CoreCohortAdapterReason.UNSUPPORTED_CONNECTION)

    monkeypatch.setattr(adapter, "_supported_connection", unsupported)
    with pytest.raises(adapter.CoreCohortAdapterError):
        case.manager.begin(DIGEST, 1)
    assert case.manager.eligible_aliases() == ()
    assert case.lifecycle.outstanding is None
    assert not case.state.observations
    assert not RayTargetProbeChallenge.objects.exists()


@pytest.mark.real_ray
def test_native_core_manager_requires_fresh_active_policy_proof(
    ray_cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real daemon and parent publisher; production claims stay off.

    The existing ray_cluster fixture owns the two-CPU local runtime and waits
    for its processes during teardown, including when this bounded test fails.
    No independent server, external cluster or speculative cleanup is created.
    """
    from django_ray.target.cohort_runtime import _local_runtime

    package, runtime = _local_runtime(ray_cluster)
    owner = threading.get_ident()
    query_threads: list[int] = []
    observation_threads: list[int] = []
    original_execute = CursorWrapper.execute
    original_executemany = CursorWrapper.executemany
    original_observe = publication.observe_prepared_core_cohort_probe

    def execute(cursor, *args, **kwargs):
        query_threads.append(threading.get_ident())
        assert threading.get_ident() == owner, "Native observation accessed parent database"
        return original_execute(cursor, *args, **kwargs)

    def executemany(cursor, *args, **kwargs):
        query_threads.append(threading.get_ident())
        assert threading.get_ident() == owner, "Native observation accessed parent database"
        return original_executemany(cursor, *args, **kwargs)

    def observe(prepared, *, owned_cleanup=False):
        observation_threads.append(threading.get_ident())
        assert threading.get_ident() != owner
        assert owned_cleanup is True
        return original_observe(prepared, owned_cleanup=owned_cleanup)

    monkeypatch.setattr(CursorWrapper, "execute", execute)
    monkeypatch.setattr(CursorWrapper, "executemany", executemany)
    monkeypatch.setattr(publication, "observe_prepared_core_cohort_probe", observe)
    now = datetime.now(UTC)
    lease = TaskWorkerLease.objects.create(
        worker_id="native-core-cohort-manager",
        hostname=socket.gethostname(),
        pid=os.getpid(),
        started_at=now - timedelta(seconds=1),
        last_heartbeat_at=now,
        capability_schema_version=1,
        django_ray_version=package,
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    lifecycle = CohortQualificationLifecycle(
        CohortProbeJobLease(lease.worker_id, lease.hostname, lease.pid, lease.started_at),
        package,
        runtime,
        RayRunnerFamily.RAY_CORE,
    )
    lifecycle.configure_aliases([alias()])
    manager = adapter.CoreCohortManagerAdapter(lifecycle)

    def publish_current():
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            # Database heartbeats remain parent-owned while native collection
            # runs in the one observation slot. This is not a worker activation.
            TaskWorkerLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=datetime.now(UTC))
            result = manager.poll()
            if result is not None:
                return result
            time.sleep(0.02)
        pytest.fail("Native Core adapter did not finish within its owned deadline")

    try:
        manager.begin(DIGEST, 1, timeout_seconds=30)
        first = publish_current()
        assert first.shared.desired_state == "draining"
        assert first.shared.activation_policy_id is not None
        assert first.shared.attestation.expectation.policy_revision == 1
        assert first.shared.attestation.expectation.runtime == runtime
        assert manager.eligible_aliases() == ()
        assert lifecycle.outstanding is None

        manager.begin(DIGEST, 1, timeout_seconds=30)
        second = publish_current()
        assert second.shared.target_policy_id == first.shared.activation_policy_id
        assert second.shared.activation_policy_id is None
        assert second.shared.attestation.expectation.policy_revision == 2
        assert second.shared.attestation.expectation.cluster_session == (
            first.shared.attestation.expectation.cluster_session
        )
        assert second.shared.attestation.expectation.target_key == derive_cohort_target_key(
            RayRunnerFamily.RAY_CORE, second.shared.attestation.expectation.cluster_session
        )
        assert second.shared.attestation_id != first.shared.attestation_id
        assert second.shared.attestation.observed_at > first.shared.attestation.observed_at
        eligible = manager.eligible_aliases()
        assert len(eligible) == 1
        assert eligible[0].shared == second.shared
        assert lifecycle.outstanding is None
        assert len(observation_threads) == 2
        assert all(thread != owner for thread in observation_threads)
        assert query_threads and all(thread == owner for thread in query_threads)
        assert RayTargetProbeChallenge.objects.get(lease=lease).revision == 4
    finally:
        # Never convert an exited/hung daemon into cleanup evidence. The owned
        # runtime fixture performs terminal process cleanup even on failure.
        lifecycle.invalidate()
