"""Qualification is parent-owned, finite, and independent of native call duration."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from django_ray.runner.cohort_qualification import (
    CohortQualificationError,
    CohortQualificationLifecycle,
    CohortQualificationReason,
    CoreObservationInput,
    OwnedCoreObservation,
    PreparedCohortAlias,
    PublishedCohortQualification,
    SharedCohortQualification,
)
from django_ray.runtime.cohort_job import (
    CohortProbeJobLease,
    CohortProbeJobRequest,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.runtime.cohort_job_entrypoint import (
    CohortProbeJobLaunch,
    probe_job_launch_entrypoint,
)
from django_ray.target.attestation import (
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
)
from django_ray.target.cohort_claim import CohortJobQualificationProvenance
from django_ray.target.cohort_intent import CohortSelectionPolicy
from django_ray.target.cohort_job_control import cohort_probe_entrypoint_digest
from django_ray.target.cohort_probe import derive_cohort_target_key

NOW = datetime(2026, 9, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
RUNTIME = RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14)
LEASE = CohortProbeJobLease("worker", "host", 12, NOW - timedelta(seconds=20))


class Clock:
    elapsed = 0.0

    def monotonic(self):
        return 100.0 + self.elapsed

    def wall(self):
        return NOW + timedelta(seconds=self.elapsed)


def alias(name="default", **changes):
    return replace(
        PreparedCohortAlias(
            name,
            DIGEST,
            CohortSelectionPolicy.WORKER_SELECTED,
            ("default",),
            DIGEST,
        ),
        **changes,
    )


def controller(family=RayRunnerFamily.RAY_CORE):
    clock = Clock()
    value = CohortQualificationLifecycle(
        LEASE,
        "0.5.0",
        RUNTIME,
        family,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    value.configure_aliases([alias()])
    if family is RayRunnerFamily.RAY_CORE:
        value.configure_core_connection(DIGEST, 1)
    return value, clock


def attestation(family=RayRunnerFamily.RAY_CORE, *, observed=NOW, ttl=30, node="1" * 56):
    expected = RayTargetExpectation(
        derive_cohort_target_key(family, "session_test"), family, "session_test", 1, RUNTIME
    )
    boundary = build_ray_observation_boundary(
        resource_state_version_before=1,
        resource_state_version_after=1,
        node_state_versions_before=(RayNodeStateVersion(node, 1),),
        node_state_versions_after=(RayNodeStateVersion(node, 1),),
    )
    return build_ray_cluster_attestation(
        expectation=expected,
        boundary=boundary,
        nodes=(
            build_ray_node_observation(
                node_id=node, cluster_session="session_test", runtime=RUNTIME
            ),
        ),
        observed_at=observed,
        expires_at=observed + timedelta(seconds=ttl),
    )


def core_input(**changes):
    return replace(
        CoreObservationInput(
            DIGEST,
            1,
            1,
            1,
            NOW - timedelta(seconds=2),
            NOW + timedelta(seconds=60),
        ),
        **changes,
    )


def shared(proof, **changes):
    return replace(
        SharedCohortQualification(LEASE, "0.5.0", 1, 1, 1, 1, proof, "active"), **changes
    )


def core_publication(proof, **changes):
    return replace(PublishedCohortQualification(shared(proof), 1, NOW), **changes)


def poll(value, clock):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = value.poll_core(now=clock.wall())
        if result is not None:
            return result
        time.sleep(0.001)
    raise AssertionError("Owned resource-free observation did not finish")


def core_success(value, clock, **changes):
    proof = attestation(**changes)
    value.begin_core(core_input(), observe=lambda: proof, timeout_seconds=5, now=clock.wall())
    observation = poll(value, clock)
    result = value.publish_core(
        observation, lambda actual: core_publication(actual), now=clock.wall()
    )
    return observation, result


def candidates(value, clock, **changes):
    args = {
        "now": clock.wall(),
        "live_lease": LEASE,
        "lease_expires_at": clock.wall() + timedelta(seconds=60),
    } | changes
    return value.eligible_aliases(**args)


def launch(configuration=None, *, challenge=1, expires=NOW + timedelta(seconds=60)):
    configuration = configuration or alias()
    request = CohortProbeJobRequest(
        challenge,
        1,
        LEASE,
        configuration.declaration_digest,
        None,
        RayRunnerFamily.RAY_JOB,
        "0.5.0",
        RUNTIME,
        None,
        None,
        1,
        NOW - timedelta(seconds=2),
        expires,
    )
    return CohortProbeJobLaunch(
        request, probe_job_request_digest(request), "http://ray:8265", DIGEST, "project.settings"
    )


def job_publication(packet, proof, *, consumed=NOW, original=None, capability_revision=1):
    original = original or proof
    request = packet.request
    q = CohortJobQualificationProvenance(
        request.configuration_digest,
        packet.jobs_endpoint,
        request.challenge_id,
        request.challenge_revision,
        request.challenge_revision + 1,
        request.issued_at,
        request.expires_at,
        consumed,
        packet.request_digest,
        DIGEST,
        probe_job_submission_id(request),
        "01000000",
        cohort_probe_entrypoint_digest(probe_job_launch_entrypoint(packet)),
        packet.submitted_runtime_env_digest,
        original.expectation_digest,
        original.attestation_digest,
        original.membership_digest,
        original.observed_at,
        original.expires_at,
        consumed,
    )
    return PublishedCohortQualification(
        shared(proof, capability_revision=capability_revision),
        request.challenge_id,
        consumed,
        q,
    )


def job_success(value, clock, *, configuration=None, packet=None, proof=None, **changes):
    configuration = configuration or alias()
    packet = packet or launch(configuration)
    proof = proof or attestation(RayRunnerFamily.RAY_JOB)
    ticket = value.begin_job(
        configuration.alias,
        packet,
        source_control_profile_digest=configuration.control_profile_digest,
        timeout_seconds=10,
        now=clock.wall(),
    )
    published = job_publication(packet, proof, consumed=clock.wall(), **changes)
    value.publish_job(ticket, lambda: published, now=clock.wall())
    return published


def test_core_observation_does_not_publish_until_owned_parent_callback():
    value, clock = controller()
    proof = attestation()
    thread_ids = []

    def observe():
        thread_ids.append(threading.get_ident())
        return proof

    ticket = value.begin_core(core_input(), observe=observe, timeout_seconds=5, now=NOW)
    result = poll(value, clock)
    assert candidates(value, clock) == ()
    assert value.outstanding is ticket and value.connection_busy
    assert thread_ids != [threading.get_ident()]
    assert value.poll_core(now=NOW) is None

    def publish(actual):
        assert threading.get_ident() == value._owner
        assert actual is proof
        return core_publication(actual)

    value.publish_core(result, publish, now=NOW)
    assert not value.connection_busy
    assert len(candidates(value, clock)) == 1
    with pytest.raises(CohortQualificationError, match="stale"):
        value.publish_core(result, publish, now=NOW)


def test_expired_observer_retains_slot_and_reconnect_fence_through_cleanup():
    value, clock = controller()
    entered, release = threading.Event(), threading.Event()

    def observe():
        entered.set()
        assert release.wait(2)
        return attestation()

    ticket = value.begin_core(core_input(), observe=observe, timeout_seconds=1, now=NOW)
    assert entered.wait(1)
    try:
        assert value.poll_core(now=NOW) is None
        clock.elapsed = 1
        assert candidates(value, clock) == ()
        assert value.blocked_reason is CohortQualificationReason.DEADLINE
        with pytest.raises(CohortQualificationError, match="deadline"):
            value.poll_core(now=clock.wall())
        with pytest.raises(CohortQualificationError, match="busy"):
            value.begin_core(
                core_input(), observe=lambda: attestation(), timeout_seconds=1, now=clock.wall()
            )
        with pytest.raises(CohortQualificationError, match="busy"):
            value.configure_core_connection(DIGEST, 2)
        with pytest.raises(CohortQualificationError, match="busy"):
            value.confirm_cleanup(
                ticket, lambda: pytest.fail("Cleanup callback ran before local call exited")
            )
    finally:
        release.set()
        value._operation.thread.join(timeout=2)
    assert value.connection_busy
    with pytest.raises(CohortQualificationError, match="cleanup_unconfirmed"):
        value.confirm_cleanup(ticket, lambda: False)
    value.confirm_cleanup(ticket, lambda: True)
    assert not value.connection_busy
    value.configure_core_connection(DIGEST, 2)
    assert candidates(value, clock) == ()


def test_discard_completed_owned_discovery_grants_nothing_and_releases_only_its_slot():
    value, clock = controller()
    core_success(value, clock)
    assert candidates(value, clock)
    ticket = value.begin_core(
        core_input(), observe=lambda: attestation(), timeout_seconds=5, now=NOW
    )
    observed = poll(value, clock)
    with pytest.raises(CohortQualificationError, match="stale"):
        value.discard_core_observation(replace(observed), now=NOW)
    assert value.outstanding is ticket
    value.discard_core_observation(observed, now=NOW)
    assert value.outstanding is None
    assert candidates(value, clock) == ()
    with pytest.raises(CohortQualificationError, match="stale"):
        value.discard_core_observation(observed, now=NOW)
    new = value.begin_core(
        core_input(challenge_revision=2), observe=lambda: attestation(), timeout_seconds=5, now=NOW
    )
    assert new is not ticket
    value.discard_core_observation(poll(value, clock), now=NOW)


@pytest.mark.parametrize("reason", ["deadline", "cleanup", "expired_proof"])
def test_discard_cannot_release_failed_or_expired_accepted_observation(reason):
    value, clock = controller()
    ticket = value.begin_core(
        core_input(), observe=lambda: attestation(ttl=2), timeout_seconds=5, now=NOW
    )
    observed = poll(value, clock)
    if reason == "cleanup":
        value.require_cleanup(ticket)
    else:
        clock.elapsed = 5 if reason == "deadline" else 2
    with pytest.raises(CohortQualificationError):
        value.discard_core_observation(observed, now=clock.wall())
    assert value.outstanding is ticket
    assert value.blocked_reason is not None
    assert candidates(value, clock) == ()


def test_discard_cannot_invent_success_from_failed_thread_exit():
    value, clock = controller()

    def fail():
        raise RuntimeError("controlled failure")

    ticket = value.begin_core(core_input(), observe=fail, timeout_seconds=5, now=NOW)
    with pytest.raises(CohortQualificationError, match="observation_failed"):
        poll(value, clock)
    with pytest.raises(CohortQualificationError, match="observation_failed"):
        value.discard_core_observation(OwnedCoreObservation(ticket, attestation()), now=NOW)
    assert value.outstanding is ticket


def test_failure_cannot_revive_prior_core_proof_after_cleanup():
    value, clock = controller()
    core_success(value, clock)
    assert candidates(value, clock)

    def fail():
        raise RuntimeError("secret details")

    ticket = value.begin_core(core_input(challenge_id=2), observe=fail, timeout_seconds=5, now=NOW)
    with pytest.raises(CohortQualificationError) as caught:
        poll(value, clock)
    assert "secret" not in str(caught.value)
    assert value.connection_busy
    value.confirm_cleanup(ticket, lambda: True)
    assert candidates(value, clock) == ()


@pytest.mark.parametrize("changed", ["profile", "remove_readd", "declaration"])
def test_observed_jobs_configuration_aba_cannot_reuse_old_result(changed):
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    packet = launch()
    ticket = value.begin_job(
        "default", packet, source_control_profile_digest=DIGEST, timeout_seconds=10, now=NOW
    )
    if changed == "remove_readd":
        value.configure_aliases([])
    else:
        key = "control_profile_digest" if changed == "profile" else "declaration_digest"
        value.configure_aliases([alias(**{key: OTHER})])
    value.configure_aliases([alias()])
    with pytest.raises(CohortQualificationError, match="stale"):
        value.publish_job(ticket, lambda: pytest.fail("Stale publisher invoked"), now=NOW)
    assert candidates(value, clock) == ()


def test_alias_only_edit_preserves_core_connection_proof_and_jobs_only_affinity():
    value, clock = controller()
    core_success(value, clock)
    value.configure_aliases(
        [
            alias(declaration_digest=OTHER),
            alias("jobs", selection_policy=CohortSelectionPolicy.JOBS_ONLY),
        ]
    )
    assert [item.configuration.alias for item in candidates(value, clock)] == ["default"]
    value.configure_core_connection(DIGEST, 2)
    assert candidates(value, clock) == ()


@pytest.mark.parametrize("state,activation", [("draining", None), ("retired", None), ("active", 2)])
def test_activation_requires_fresh_active_proof(state, activation):
    value, clock = controller()
    proof = attestation()
    value.begin_core(core_input(), observe=lambda: proof, timeout_seconds=5, now=NOW)
    observed = poll(value, clock)
    published = core_publication(
        proof, shared=shared(proof, desired_state=state, activation_policy_id=activation)
    )
    value.publish_core(observed, lambda _proof: published, now=NOW)
    assert candidates(value, clock) == ()


def test_newer_shared_b_capability_preserves_a_original_proof_and_expiry():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    a, b = alias("a"), alias("b", declaration_digest=OTHER)
    value.configure_aliases([a, b])
    original = attestation(RayRunnerFamily.RAY_JOB, ttl=10)
    pub_a = job_success(value, clock, configuration=a, packet=launch(a), proof=original)
    clock.elapsed = 2
    newer = attestation(RayRunnerFamily.RAY_JOB, observed=clock.wall(), ttl=60)
    job_success(
        value,
        clock,
        configuration=b,
        packet=launch(b, challenge=2),
        proof=newer,
        capability_revision=2,
    )
    selected = {item.configuration.alias: item for item in candidates(value, clock)}
    assert set(selected) == {"a", "b"}
    assert selected["a"].shared.capability_revision == 2
    assert selected["a"].job_qualification == pub_a.job_qualification
    assert selected["a"].job_qualification.endpoint_attestation_digest != newer.attestation_digest
    clock.elapsed = 10
    assert [item.configuration.alias for item in candidates(value, clock)] == ["b"]


def test_slow_a_can_return_original_proof_and_newer_shared_metadata():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    original = attestation(RayRunnerFamily.RAY_JOB)
    newer = attestation(RayRunnerFamily.RAY_JOB, observed=NOW + timedelta(seconds=1), ttl=40)
    clock.elapsed = 2
    published = job_success(value, clock, proof=newer, original=original)
    selected = candidates(value, clock)[0]
    assert selected.shared.attestation == newer
    assert selected.job_qualification == published.job_qualification


@pytest.mark.parametrize("expiry", ["endpoint", "challenge", "shared"])
def test_each_independent_ttl_stops_jobs_eligibility(expiry):
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    own = attestation(RayRunnerFamily.RAY_JOB, ttl=1 if expiry == "endpoint" else 30)
    current = attestation(RayRunnerFamily.RAY_JOB, ttl=1 if expiry == "shared" else 30)
    packet = launch(expires=NOW + timedelta(seconds=1 if expiry == "challenge" else 60))
    job_success(value, clock, packet=packet, proof=current, original=own)
    assert candidates(value, clock)
    clock.elapsed = 1
    assert candidates(value, clock) == ()


def test_membership_change_withdraws_only_compatible_shared_candidates():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    a, b = alias("a"), alias("b", declaration_digest=OTHER)
    value.configure_aliases([a, b])
    job_success(value, clock, configuration=a, packet=launch(a))
    newer = attestation(RayRunnerFamily.RAY_JOB, node="2" * 56)
    job_success(
        value,
        clock,
        configuration=b,
        packet=launch(b, challenge=2),
        proof=newer,
        capability_revision=2,
    )
    assert [item.configuration.alias for item in candidates(value, clock)] == ["b"]


def test_pending_and_ambiguous_jobs_keep_one_slot_until_exact_cleanup():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    ticket = value.begin_job(
        "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=10, now=NOW
    )
    assert value.publish_job(ticket, lambda: None, now=NOW) is None
    assert value.outstanding is ticket
    with pytest.raises(CohortQualificationError, match="busy"):
        value.begin_job(
            "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=10, now=NOW
        )
    value.require_cleanup(ticket)
    with pytest.raises(CohortQualificationError, match="cleanup_unconfirmed"):
        value.confirm_cleanup(ticket, lambda: "terminal")
    value.confirm_cleanup(ticket, lambda: True)
    assert value.outstanding is None and candidates(value, clock) == ()


def test_lost_publication_reply_does_not_seed_cache_or_replay_publisher():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    job_success(value, clock)
    ticket = value.begin_job(
        "default",
        launch(challenge=2),
        source_control_profile_digest=DIGEST,
        timeout_seconds=10,
        now=NOW,
    )
    calls = []

    def lost_reply():
        calls.append("committed")
        raise RuntimeError("opaque transport token")

    with pytest.raises(CohortQualificationError, match="publication_failed") as caught:
        value.publish_job(ticket, lost_reply, now=NOW)
    assert "token" not in str(caught.value)
    with pytest.raises(CohortQualificationError, match="publication_failed"):
        value.publish_job(ticket, lost_reply, now=NOW)
    assert calls == ["committed"] and candidates(value, clock) == ()


@pytest.mark.parametrize("kind", ["deadline", "wall_expiry", "epoch"])
def test_post_publication_recheck_refuses_late_or_changed_success(kind):
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    packet = launch(expires=NOW + timedelta(seconds=5))
    ticket = value.begin_job(
        "default", packet, source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )
    published = job_publication(packet, attestation(RayRunnerFamily.RAY_JOB))

    def callback():
        if kind == "epoch":
            value.configure_aliases([alias(control_profile_digest=OTHER)])
        elif kind == "wall_expiry":
            value._wall_clock = lambda: NOW + timedelta(seconds=5)
        else:
            clock.elapsed = 5
        return published

    with pytest.raises(CohortQualificationError, match="deadline|stale"):
        value.publish_job(ticket, callback, now=NOW)
    assert value.outstanding is ticket


def test_fresh_post_callback_clock_accepts_legitimate_later_consumption():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    packet = launch()
    ticket = value.begin_job(
        "default", packet, source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )

    def callback():
        clock.elapsed = 1
        return job_publication(packet, attestation(RayRunnerFamily.RAY_JOB), consumed=clock.wall())

    value.publish_job(ticket, callback, now=NOW)
    assert candidates(value, clock)


@pytest.mark.parametrize(
    "field,value",
    [
        ("challenge_id", 2),
        ("request_revision", 2),
        ("request_digest", OTHER),
        ("jobs_endpoint", "http://other:8265"),
        ("submitted_control_runtime_env_digest", OTHER),
        ("entrypoint_digest", OTHER),
        ("configuration_digest", OTHER),
        ("endpoint_expectation_digest", OTHER),
        ("endpoint_membership_digest", OTHER),
        ("native_job_id", "not-a-job"),
    ],
)
def test_mismatched_jobs_publisher_return_cannot_grant_eligibility(field, value):
    manager, clock = controller(RayRunnerFamily.RAY_JOB)
    packet = launch()
    ticket = manager.begin_job(
        "default", packet, source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )
    published = job_publication(packet, attestation(RayRunnerFamily.RAY_JOB))
    changed = replace(
        published, job_qualification=replace(published.job_qualification, **{field: value})
    )
    with pytest.raises(CohortQualificationError, match="publication_mismatch"):
        manager.publish_job(ticket, lambda: changed, now=NOW)
    assert candidates(manager, clock) == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"lease": replace(LEASE, pid=13)},
        {"package_version": "0.5.1"},
        {"capability_revision": True},
        {"target_policy_id": 0},
        {"desired_state": "unknown"},
        {"activation_policy_id": True},
    ],
)
def test_mismatched_shared_publication_is_rejected(changes):
    value, clock = controller()
    proof = attestation()
    value.begin_core(core_input(), observe=lambda: proof, timeout_seconds=5, now=NOW)
    observed = poll(value, clock)
    published = core_publication(proof, shared=shared(proof, **changes))
    with pytest.raises(CohortQualificationError, match="publication_mismatch"):
        value.publish_core(observed, lambda _actual: published, now=NOW)
    assert candidates(value, clock) == ()


def test_equal_but_unowned_ticket_and_observation_are_rejected():
    value, clock = controller()
    proof = attestation()
    value.begin_core(core_input(), observe=lambda: proof, timeout_seconds=5, now=NOW)
    observed = poll(value, clock)
    for forged in (replace(observed), replace(observed, operation=replace(observed.operation))):
        with pytest.raises(CohortQualificationError, match="stale"):
            value.publish_core(
                forged, lambda _proof: pytest.fail("Unowned callback invoked"), now=NOW
            )


@pytest.mark.parametrize("which", ["monotonic", "wall"])
def test_clock_regression_permanently_discards_eligibility(which):
    value, clock = controller()
    core_success(value, clock)
    clock.elapsed = 2
    assert candidates(value, clock)
    if which == "monotonic":
        clock.elapsed = 1
        args = {}
    else:
        args = {"now": NOW}
    with pytest.raises(CohortQualificationError, match="clock_regression"):
        candidates(value, clock, **args)
    clock.elapsed = 3
    assert candidates(value, clock) == ()


def test_current_lease_snapshot_and_shutdown_are_independent_from_stored_proof():
    value, clock = controller()
    core_success(value, clock)
    assert candidates(value, clock, live_lease=replace(LEASE, started_at=NOW)) == ()
    assert candidates(value, clock, lease_expires_at=NOW) == ()
    value.invalidate()
    assert candidates(value, clock) == ()
    with pytest.raises(CohortQualificationError, match="stale"):
        value.configure_aliases([alias()])


@pytest.mark.parametrize("timeout", [True, False, 0, -1, 601, float("inf"), float("nan"), "5"])
def test_invalid_operation_timeout_does_not_start_work(timeout):
    value, _clock = controller()
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.begin_core(
            core_input(),
            observe=lambda: pytest.fail("Invalid observer started"),
            timeout_seconds=timeout,
            now=NOW,
        )
    assert value.outstanding is None


@pytest.mark.parametrize(
    "configuration",
    [
        [alias(), alias()],
        [alias(queues=())],
        [alias(queues=("x", "x"))],
        [alias(control_profile_digest="bad")],
        [alias(selection_policy="jobs_only")],
        [alias(str(index)) for index in range(65)],
    ],
)
def test_malformed_finite_configuration_is_atomic(configuration):
    value, clock = controller()
    core_success(value, clock)
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.configure_aliases(configuration)
    assert len(candidates(value, clock)) == 1


def test_controller_mutation_from_observation_thread_is_refused():
    value, clock = controller()

    def observe():
        with pytest.raises(CohortQualificationError, match="wrong_owner"):
            value.configure_aliases([])
        return attestation()

    value.begin_core(core_input(), observe=observe, timeout_seconds=5, now=NOW)
    observed = poll(value, clock)
    value.publish_core(observed, lambda proof: core_publication(proof), now=NOW)
    assert candidates(value, clock)


def test_import_is_django_and_ray_free_in_fresh_interpreter():
    code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'django' or name.startswith('django.') or name == 'ray' or name.startswith('ray.'):
        raise AssertionError('Forbidden import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import django_ray.runner.cohort_qualification
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_cleanup_callback_cannot_recursively_release_and_reuse_slot():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    ticket = value.begin_job(
        "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )
    value.require_cleanup(ticket)

    def confirmation():
        with pytest.raises(CohortQualificationError, match="busy"):
            value.confirm_cleanup(ticket, lambda: True)
        with pytest.raises(CohortQualificationError, match="busy"):
            value.begin_job(
                "default",
                launch(challenge=2),
                source_control_profile_digest=DIGEST,
                timeout_seconds=5,
                now=NOW,
            )
        assert value.outstanding is ticket
        return True

    value.confirm_cleanup(ticket, confirmation)
    assert value.outstanding is None


def test_publisher_cannot_release_the_operation_while_callback_is_running():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    ticket = value.begin_job(
        "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )

    def publication():
        value.require_cleanup(ticket)
        with pytest.raises(CohortQualificationError, match="busy"):
            value.confirm_cleanup(ticket, lambda: True)
        return None

    with pytest.raises(CohortQualificationError, match="cleanup_unconfirmed"):
        value.publish_job(ticket, publication, now=NOW)
    assert value.outstanding is ticket


@pytest.mark.parametrize("change", ["digest", "family", "observed", "key", "runtime"])
def test_invalid_core_observation_never_reaches_publisher(change):
    value, clock = controller()
    proof = attestation()
    if change == "digest":
        proof = replace(proof, attestation_digest=OTHER)
    elif change == "family":
        proof = attestation(RayRunnerFamily.RAY_JOB)
    elif change == "observed":
        proof = attestation(observed=NOW - timedelta(seconds=10))
    else:
        expected = replace(
            proof.expectation,
            **(
                {"target_key": "arbitrary.target"}
                if change == "key"
                else {"runtime": replace(RUNTIME, python_patch=15)}
            ),
        )
        proof = replace(proof, expectation=expected)
    ticket = value.begin_core(core_input(), observe=lambda: proof, timeout_seconds=5, now=NOW)
    with pytest.raises(CohortQualificationError, match="observation_failed"):
        poll(value, clock)
    assert value.outstanding is ticket and candidates(value, clock) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"connection_epoch": True},
        {"challenge_id": True},
        {"challenge_revision": False},
        {"configuration_digest": "bad"},
        {"issued_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW},
        {"policy_revision": True},
        {"target_key": "unexpected"},
    ],
)
def test_malformed_prepared_core_fields_fail_before_thread_start(change):
    value, _clock = controller()
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.begin_core(
            core_input(**change),
            observe=lambda: pytest.fail("Malformed observer started"),
            timeout_seconds=5,
            now=NOW,
        )
    assert value.outstanding is None


@pytest.mark.parametrize(
    "args",
    [
        {"lease": replace(LEASE, pid=True)},
        {"lease": replace(LEASE, hostname="")},
        {"package_version": "not package"},
        {"runtime": replace(RUNTIME, ray_major=True)},
        {"runner_family": "ray_core"},
        {"wall_clock": False},
        {"monotonic": False},
    ],
)
def test_malformed_controller_inputs_are_fixed_refusals(args):
    keywords = {
        "lease": LEASE,
        "package_version": "0.5.0",
        "runtime": RUNTIME,
        "runner_family": RayRunnerFamily.RAY_CORE,
    } | args
    with pytest.raises(CohortQualificationError, match="invalid"):
        CohortQualificationLifecycle(**keywords)


def test_configuration_churn_does_not_retain_historical_aliases_or_targets():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    for index in range(200):
        value.configure_aliases([alias(f"alias-{index}")])
    assert len(value._aliases) == 1
    assert value._configuration_epoch == 201
    assert not value._jobs and not value._shared and value.outstanding is None


def test_check_operation_deadline_retains_failed_job_until_cleanup():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    ticket = value.begin_job(
        "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=1, now=NOW
    )
    value.check_operation(ticket, now=NOW)
    clock.elapsed = 1
    with pytest.raises(CohortQualificationError, match="deadline"):
        value.check_operation(ticket, now=clock.wall())
    assert value.outstanding is ticket


def test_invalidate_during_observation_retains_ownership_but_refuses_late_return():
    value, clock = controller()
    ticket = value.begin_core(
        core_input(), observe=lambda: attestation(), timeout_seconds=5, now=NOW
    )
    value.invalidate()
    with pytest.raises(CohortQualificationError, match="stale"):
        poll(value, clock)
    assert value.outstanding is ticket
    value._operation.thread.join(timeout=2)
    value.confirm_cleanup(ticket, lambda: True)
    assert candidates(value, clock) == ()


@pytest.mark.parametrize("bad_clock", [None, NOW.replace(tzinfo=None), "now"])
def test_malformed_post_publication_clock_prevents_replaying_possible_commit(bad_clock):
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    packet = launch()
    ticket = value.begin_job(
        "default", packet, source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )
    published = job_publication(packet, attestation(RayRunnerFamily.RAY_JOB))
    calls = []

    def callback():
        calls.append("committed")
        value._wall_clock = lambda: bad_clock
        return published

    with pytest.raises(CohortQualificationError, match="publication_failed"):
        value.publish_job(ticket, callback, now=NOW)
    value._wall_clock = clock.wall
    with pytest.raises(CohortQualificationError, match="publication_failed"):
        value.publish_job(ticket, callback, now=NOW)
    assert calls == ["committed"] and value.outstanding is ticket


def test_refresh_withdraws_only_its_old_receipt_before_publication():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    a, b = alias("a"), alias("b", declaration_digest=OTHER)
    value.configure_aliases([a, b])
    job_success(value, clock, configuration=a, packet=launch(a))
    job_success(value, clock, configuration=b, packet=launch(b, challenge=2))
    assert len(candidates(value, clock)) == 2
    packet = launch(a, challenge=3)
    ticket = value.begin_job(
        "a", packet, source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )
    assert [item.configuration.alias for item in candidates(value, clock)] == ["b"]
    value.publish_job(
        ticket, lambda: job_publication(packet, attestation(RayRunnerFamily.RAY_JOB)), now=NOW
    )
    assert len(candidates(value, clock)) == 2


@pytest.mark.parametrize("bad", [None, "publication", False])
def test_core_requires_a_complete_publication_return(bad):
    value, clock = controller()
    proof = attestation()
    value.begin_core(core_input(), observe=lambda: proof, timeout_seconds=5, now=NOW)
    observed = poll(value, clock)
    with pytest.raises(CohortQualificationError, match="publication_mismatch"):
        value.publish_core(observed, lambda _proof: bad, now=NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"shared": None},
        {"consumed_at": NOW - timedelta(seconds=1)},
        {"challenge_id": 2},
    ],
)
def test_core_rejects_wrong_publication_identity_and_chronology(change):
    value, clock = controller()
    proof = attestation()
    value.begin_core(core_input(), observe=lambda: proof, timeout_seconds=5, now=NOW)
    observed = poll(value, clock)
    with pytest.raises(CohortQualificationError, match="publication_mismatch"):
        value.publish_core(observed, lambda _proof: core_publication(proof, **change), now=NOW)


@pytest.mark.parametrize("proof", [None, attestation(RayRunnerFamily.RAY_JOB)])
def test_shared_publication_requires_correct_full_attestation(proof):
    value, clock = controller()
    original = attestation()
    value.begin_core(core_input(), observe=lambda: original, timeout_seconds=5, now=NOW)
    observed = poll(value, clock)
    published = core_publication(original, shared=shared(proof))
    with pytest.raises(CohortQualificationError, match="publication_mismatch"):
        value.publish_core(observed, lambda _proof: published, now=NOW)


def test_job_requires_endpoint_provenance_even_with_valid_shared_proof():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    packet = launch()
    ticket = value.begin_job(
        "default", packet, source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )
    published = replace(
        job_publication(packet, attestation(RayRunnerFamily.RAY_JOB)), job_qualification=None
    )
    with pytest.raises(CohortQualificationError, match="publication_mismatch"):
        value.publish_job(ticket, lambda: published, now=NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"lease": replace(LEASE, pid=2)},
        {"expected_runtime": replace(RUNTIME, python_patch=13)},
        {"configuration_digest": OTHER},
        {"expected_package_version": "0.5.1"},
    ],
)
def test_job_launch_must_match_current_manager_and_alias(change):
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    packet = launch()
    request = replace(packet.request, **change)
    packet = replace(packet, request=request, request_digest=probe_job_request_digest(request))
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.begin_job(
            "default", packet, source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
        )
    assert value.outstanding is None


def test_registered_jobs_refresh_checks_exact_policy_identity():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    proof = attestation(RayRunnerFamily.RAY_JOB)
    packet = launch()
    request = replace(
        packet.request,
        target_key=proof.expectation.target_key,
        expected_cluster_session="session_test",
        expected_target_policy_id=2,
    )
    packet = replace(packet, request=request, request_digest=probe_job_request_digest(request))
    ticket = value.begin_job(
        "default", packet, source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )
    with pytest.raises(CohortQualificationError, match="publication_mismatch"):
        value.publish_job(ticket, lambda: job_publication(packet, proof), now=NOW)


def test_registered_core_observation_uses_current_session_policy():
    value, clock = controller()
    proof = attestation()
    prepared = core_input(target_key=proof.expectation.target_key, cluster_session="session_test")
    value.begin_core(prepared, observe=lambda: proof, timeout_seconds=5, now=NOW)
    observed = poll(value, clock)
    value.publish_core(observed, lambda _proof: core_publication(proof), now=NOW)
    assert candidates(value, clock)


def test_failed_thread_start_retains_cleanup_state(monkeypatch):
    value, _clock = controller()

    def fail(_thread):
        raise RuntimeError("thread resource unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(CohortQualificationError, match="observation_failed"):
        value.begin_core(core_input(), observe=lambda: attestation(), timeout_seconds=5, now=NOW)
    ticket = value.outstanding
    assert ticket is not None and value.connection_busy
    value.confirm_cleanup(ticket, lambda: True)


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf")])
def test_invalid_monotonic_clock_withdraws_cache(bad):
    value, clock = controller()
    core_success(value, clock)
    value._monotonic = lambda: bad
    with pytest.raises(CohortQualificationError, match="invalid"):
        candidates(value, clock)
    value._monotonic = clock.monotonic
    assert candidates(value, clock) == ()


def test_clock_exception_and_cleanup_exception_fail_closed():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    ticket = value.begin_job(
        "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )

    def fail():
        raise RuntimeError("secret")

    value._monotonic = fail
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.check_operation(ticket, now=NOW)
    value._monotonic = clock.monotonic
    with pytest.raises(CohortQualificationError, match="cleanup_unconfirmed"):
        value.confirm_cleanup(ticket, fail)
    assert value.outstanding is ticket


def test_stopped_controller_cannot_begin_new_work_after_cleanup():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    value.invalidate()
    with pytest.raises(CohortQualificationError, match="stale"):
        value.begin_job(
            "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
        )


def test_context_configuration_validation_and_same_epoch_aba_refusal():
    value, _clock = controller()
    value.configure_core_connection(DIGEST, 1)
    with pytest.raises(CohortQualificationError, match="stale"):
        value.configure_core_connection(OTHER, 1)
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.configure_core_connection("bad", 2)
    value.configure_core_connection(OTHER, 2)
    with pytest.raises(CohortQualificationError, match="stale"):
        value.configure_core_connection(DIGEST, 1)


def test_cleanup_requires_owned_failed_ticket_and_callable_confirmation():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    ticket = value.begin_job(
        "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )
    with pytest.raises(CohortQualificationError, match="stale"):
        value.confirm_cleanup(ticket, lambda: True)
    with pytest.raises(CohortQualificationError, match="stale"):
        value.require_cleanup(replace(ticket))
    value.require_cleanup(ticket)
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.confirm_cleanup(ticket, True)


def test_publisher_reentrancy_does_not_invoke_another_callback():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    ticket = value.begin_job(
        "default", launch(), source_control_profile_digest=DIGEST, timeout_seconds=5, now=NOW
    )

    def publisher():
        with pytest.raises(CohortQualificationError, match="invalid"):
            value.publish_job(ticket, lambda: pytest.fail("Recursive publisher invoked"), now=NOW)
        return None

    assert value.publish_job(ticket, publisher, now=NOW) is None


def test_invalid_lease_expiry_is_not_an_eligible_candidate():
    value, clock = controller()
    core_success(value, clock)
    with pytest.raises(CohortQualificationError, match="invalid"):
        candidates(value, clock, lease_expires_at=NOW.replace(tzinfo=None))


def test_jobs_source_profile_is_bound_independently_of_submitted_runtime_env():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    configured = alias(control_profile_digest=OTHER)
    value.configure_aliases([configured])
    packet = launch(configured)
    assert packet.submitted_runtime_env_digest != configured.control_profile_digest
    ticket = value.begin_job(
        "default",
        packet,
        source_control_profile_digest=OTHER,
        timeout_seconds=5,
        now=NOW,
    )
    assert value._operation.source_control_profile_digest == OTHER
    value.publish_job(
        ticket, lambda: job_publication(packet, attestation(RayRunnerFamily.RAY_JOB)), now=NOW
    )
    selected = candidates(value, clock)[0]
    assert selected.configuration.control_profile_digest == OTHER
    assert selected.launch.submitted_runtime_env_digest == DIGEST
    assert selected.job_qualification.submitted_control_runtime_env_digest == DIGEST


class StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "source", [OTHER, None, True, b"sha256", "bad", "sha256:" + "A" * 64, StringSubclass(DIGEST)]
)
def test_bad_source_binding_preserves_previous_qualification_and_operation_state(source):
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    job_success(value, clock)
    before = candidates(value, clock)
    sequence = value._sequence
    shared_before, jobs_before = dict(value._shared), dict(value._jobs)
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.begin_job(
            "default",
            launch(challenge=2),
            source_control_profile_digest=source,
            timeout_seconds=5,
            now=NOW,
        )
    assert value.outstanding is None and value._sequence == sequence
    assert value._shared == shared_before and value._jobs == jobs_before
    assert candidates(value, clock) == before


def test_absent_source_binding_is_a_required_keyword_before_any_mutation():
    value, clock = controller(RayRunnerFamily.RAY_JOB)
    job_success(value, clock)
    before = candidates(value, clock)
    sequence = value._sequence
    with pytest.raises(TypeError, match="source_control_profile_digest"):
        value.begin_job("default", launch(challenge=2), timeout_seconds=5, now=NOW)
    assert value.outstanding is None and value._sequence == sequence
    assert candidates(value, clock) == before


def test_stale_source_profile_cannot_bind_a_new_configuration_epoch():
    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    configured = alias(control_profile_digest=OTHER)
    value.configure_aliases([configured])
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.begin_job(
            "default",
            launch(configured),
            source_control_profile_digest=DIGEST,
            timeout_seconds=5,
            now=NOW,
        )
    assert value.outstanding is None and value._sequence == 0


def test_source_profile_comparison_uses_constant_time_primitive(monkeypatch):
    import django_ray.runner.cohort_qualification as module

    value, _clock = controller(RayRunnerFamily.RAY_JOB)
    original = module.secrets.compare_digest
    comparisons = []

    def compare(actual, expected):
        comparisons.append((actual, expected))
        return original(actual, expected)

    monkeypatch.setattr(module.secrets, "compare_digest", compare)
    value.begin_job(
        "default",
        launch(),
        source_control_profile_digest=DIGEST,
        timeout_seconds=5,
        now=NOW,
    )
    assert (DIGEST, DIGEST) in comparisons


@pytest.mark.parametrize(
    "queue", ["任务", "work queue", " leading", "trailing ", "  café queue  ", "a" * 100]
)
def test_queue_spelling_preserves_existing_unicode_and_spaces(queue):
    value, clock = controller()
    core_success(value, clock)
    value.configure_aliases([alias(queues=(queue,))])
    assert candidates(value, clock)[0].configuration.queues == (queue,)


@pytest.mark.parametrize("queue", ["", " ", "\t\n", "\u2003", "a\x00b", "a" * 101, None, True])
def test_invalid_queue_names_fail_without_changing_previous_configuration(queue):
    value, clock = controller()
    core_success(value, clock)
    previous = candidates(value, clock)
    with pytest.raises(CohortQualificationError, match="invalid"):
        value.configure_aliases([alias(queues=(queue,))])
    assert candidates(value, clock) == previous
