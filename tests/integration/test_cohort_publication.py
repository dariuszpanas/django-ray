"""Atomic publication boundaries; fake observations never claim native proof."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from ray.dashboard.modules.job.common import JobStatus
from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)
from django_ray.runtime.cohort_job import (
    CohortProbeJobLease,
    CohortProbeJobRequest,
    probe_job_metadata,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.runtime.cohort_job_entrypoint import (
    CohortProbeJobLaunch,
    probe_job_launch_entrypoint,
)
from django_ray.target import (
    capabilities,
    cohort_job_control,
    cohort_job_http,
    cohort_runtime,
    coordination,
)
from django_ray.target import cohort_job_receipt_storage as storage
from django_ray.target import cohort_publication as publication
from django_ray.target.attestation import (
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)
from django_ray.target.cohort_job_control import cohort_probe_submitted_runtime_env_digest
from django_ray.target.cohort_probe import derive_cohort_target_key
from django_ray.target.cohort_probe_challenges import replace_ray_target_probe_challenge
from tests.integration.test_cohort_job_receipts import _codec_receipt
from tests.integration.test_cohort_probe_challenges import NOW, _issue, _lease, _target

pytestmark = pytest.mark.django_db(transaction=True)
RUNTIME = RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14)
ENDPOINT = "http://ray-head:8265"


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=NOW, queries=[], observation_calls=0, on_query=lambda: None)
    for module in (publication, storage, cohort_job_control):
        monkeypatch.setattr(module, "_now", lambda: state.now)
    monkeypatch.setattr(publication, "_local_runtime", lambda _ray: ("0.5.0", RUNTIME))
    monkeypatch.setattr(cohort_runtime, "_local_runtime", lambda _ray: ("0.5.0", RUNTIME))
    return state


def _request(identity, issued, *, family=RayRunnerFamily.RAY_JOB):
    challenge = issued.receipt
    return CohortProbeJobRequest(
        challenge.challenge_id,
        challenge.revision,
        CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ),
        challenge.configuration_digest,
        None,
        family,
        "0.5.0",
        RUNTIME,
        None,
        None,
        1,
        challenge.issued_at,
        challenge.expires_at,
    )


@pytest.fixture
def core_case(clock, monkeypatch):
    lease, identity = _lease()
    issued = _issue(identity)
    plan = publication.CoreCohortProbePlan(issued.receipt, None, "0.5.0", RUNTIME)
    versions = (RayNodeStateVersion("1" * 56, 1),)
    proof = build_ray_cluster_attestation(
        expectation=RayTargetExpectation(
            derive_cohort_target_key(RayRunnerFamily.RAY_CORE, "session_core"),
            RayRunnerFamily.RAY_CORE,
            "session_core",
            1,
            RUNTIME,
        ),
        boundary=build_ray_observation_boundary(
            resource_state_version_before=1,
            resource_state_version_after=2,
            node_state_versions_before=versions,
            node_state_versions_after=versions,
        ),
        nodes=(
            build_ray_node_observation(
                node_id="1" * 56, cluster_session="session_core", runtime=RUNTIME
            ),
        ),
        observed_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=30),
    )

    case = SimpleNamespace(lease=lease, identity=identity, issued=issued, plan=plan, proof=proof)

    def observe(**_arguments):
        assert not connection.in_atomic_block
        clock.observation_calls += 1
        clock.now = NOW + timedelta(seconds=2)
        return case.proof

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)
    return case


@pytest.fixture
def job_case(clock, monkeypatch):
    lease, identity = _lease()
    issued = _issue(identity, runner_family=RayRunnerFamily.RAY_JOB)
    request = _request(identity, issued)
    runtime_env = {"env_vars": {"DJANGO_SETTINGS_MODULE": "tests.probe_settings"}}
    launch = CohortProbeJobLaunch(
        request,
        probe_job_request_digest(request),
        ENDPOINT,
        cohort_probe_submitted_runtime_env_digest(runtime_env),
        "tests.probe_settings",
    )
    entrypoint = probe_job_launch_entrypoint(launch)
    storage.reserve_cohort_job_probe(
        identity,
        request,
        nonce=issued.nonce,
        jobs_endpoint=ENDPOINT,
        entrypoint=entrypoint,
        submitted_runtime_env=runtime_env,
    )
    receipt = _codec_receipt(request)
    clock.now = NOW + timedelta(seconds=3)
    storage.write_cohort_job_receipt(receipt)
    clock.now = NOW + timedelta(seconds=4)
    assert JobDetails is not None
    details = JobDetails(
        type=JobType.SUBMISSION,
        submission_id=probe_job_submission_id(request),
        job_id=receipt.native_job_id,
        status=JobStatus.SUCCEEDED,
        entrypoint=entrypoint,
        metadata=probe_job_metadata(request),
        runtime_env=runtime_env,
    )

    def fetch(endpoint, handle):
        assert not connection.in_atomic_block
        clock.queries.append((endpoint, handle))
        clock.on_query()
        return details

    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", fetch)
    return SimpleNamespace(
        lease=lease, identity=identity, issued=issued, launch=launch, receipt=receipt
    )


def _publish(case, **changes):
    arguments = {"nonce": case.issued.nonce, **changes}
    if hasattr(case, "plan"):
        return publication.publish_core_cohort_probe(case.identity, case.plan, **arguments)
    return publication.publish_cohort_job_probe(case.identity, case.launch, **arguments)


def _no_publication():
    assert not RayTarget.objects.exists()
    assert not RayTargetPolicyRevision.objects.exists()
    assert not RayTargetAttestationRevision.objects.exists()
    assert not RayWorkerTargetCapability.objects.exists()
    assert not RayTargetProbeChallenge.objects.filter(consumed_at__isnull=False).exists()


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def publication_database(request):
    if connection.vendor != request.param:
        pytest.skip(f"Requires {request.param}")


def _shared_job_proof(case, clock, *, observed_seconds=2, node="1", expires_seconds=40):
    """Seed independently published shared proof; endpoint A still needs inspection."""
    original = case.receipt.attestation
    versions = (RayNodeStateVersion(node * 56, 1),)
    shared = build_ray_cluster_attestation(
        expectation=original.expectation,
        boundary=build_ray_observation_boundary(
            resource_state_version_before=3,
            resource_state_version_after=4,
            node_state_versions_before=versions,
            node_state_versions_after=versions,
        ),
        nodes=(
            build_ray_node_observation(
                node_id=node * 56,
                cluster_session=original.expectation.cluster_session,
                runtime=RUNTIME,
            ),
        ),
        observed_at=NOW + timedelta(seconds=observed_seconds),
        expires_at=NOW + timedelta(seconds=expires_seconds),
    )
    with transaction.atomic():
        coordination._register_ray_target_locked(shared.expectation, now=NOW, using="default")
        coordination._record_ray_target_attestation_locked(
            shared.expectation.target_key,
            shared,
            expected_policy_revision=1,
            expected_attestation_revision=0,
            now=clock.now,
            using="default",
        )
        capabilities._advertise_ray_worker_target_capability_locked(
            case.identity,
            shared.expectation.target_key,
            RUNTIME,
            manager_runner_family=RayRunnerFamily.RAY_JOB,
            expected_policy_revision=1,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=clock.now,
            using="default",
        )
    return shared


@pytest.mark.usefixtures("publication_database")
@pytest.mark.parametrize("observed_seconds", [1, 2])
def test_jobs_configuration_keeps_its_own_receipt_with_newer_shared_proof(
    job_case, clock, observed_seconds
):
    shared = _shared_job_proof(job_case, clock, observed_seconds=observed_seconds)
    shared_row = RayTargetAttestationRevision.objects.get()
    clock.now += timedelta(seconds=1)
    result = _publish(job_case, expected_attestation_revision=1, expected_capability_revision=1)
    own = result.job_qualification
    assert result.attestation_id == shared_row.pk
    assert result.capability_revision == 2
    assert RayTargetAttestationRevision.objects.count() == 1
    assert own.endpoint_attestation_digest == job_case.receipt.attestation.attestation_digest
    assert own.endpoint_attestation_digest != shared.attestation_digest
    assert own.endpoint_membership_digest == shared.membership_digest
    assert own.endpoint_expires_at == NOW + timedelta(seconds=30) < shared.expires_at
    assert own.endpoint_observed_at == job_case.receipt.attestation.observed_at
    assert own.configuration_digest == job_case.launch.request.configuration_digest
    assert own.jobs_endpoint == ENDPOINT
    assert own.request_digest == job_case.launch.request_digest
    assert own.request_revision == job_case.launch.request.challenge_revision
    assert own.consumed_challenge_revision == own.request_revision + 1
    assert own.consumed_at == result.consumed_at
    assert own.receipt_received_at == NOW + timedelta(seconds=3)
    assert own.submission_id == job_case.receipt.submission_id
    assert own.native_job_id == job_case.receipt.native_job_id
    assert own.submitted_control_runtime_env_digest == job_case.launch.submitted_runtime_env_digest
    assert clock.queries == [(ENDPOINT, own.submission_id)]


@pytest.mark.usefixtures("publication_database")
@pytest.mark.parametrize(
    "failure",
    ["membership", "shared_expiry", "endpoint_expiry", "attestation_cas", "capability_cas"],
)
def test_shared_proof_cannot_bypass_endpoint_publication_fences(
    job_case, clock, monkeypatch, failure
):
    _shared_job_proof(
        job_case,
        clock,
        node="2" if failure == "membership" else "1",
        expires_seconds=6 if failure == "shared_expiry" else 40,
    )
    clock.now += timedelta(seconds=1)
    if failure in {"shared_expiry", "endpoint_expiry"}:
        original = capabilities._advertise_ray_worker_target_capability_locked

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            clock.now = NOW + timedelta(seconds=6 if failure == "shared_expiry" else 30)
            return result

        monkeypatch.setattr(capabilities, "_advertise_ray_worker_target_capability_locked", delayed)
    with pytest.raises(publication.CohortPublicationError):
        _publish(
            job_case,
            expected_attestation_revision=0 if failure == "attestation_cas" else 1,
            expected_capability_revision=0 if failure == "capability_cas" else 1,
        )
    assert RayTargetAttestationRevision.objects.count() == 1
    assert RayWorkerTargetCapability.objects.get().revision == 1
    assert RayTargetProbeChallenge.objects.get().consumed_at is None


@pytest.mark.usefixtures("publication_database")
def test_newer_endpoint_observation_can_advance_changed_shared_membership(job_case, clock):
    _shared_job_proof(job_case, clock, observed_seconds=0.5, node="2")
    clock.now += timedelta(seconds=1)
    result = _publish(job_case, expected_attestation_revision=1, expected_capability_revision=1)
    latest = RayTargetAttestationRevision.objects.get(pk=result.attestation_id)
    assert latest.revision == 2
    assert latest.attestation_digest == result.job_qualification.endpoint_attestation_digest
    assert latest.membership_digest == job_case.receipt.attestation.membership_digest


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
def test_publication_commits_full_proof_capability_and_consumption_as_draining(
    request, fixture_name
):
    case = request.getfixturevalue(fixture_name)
    result = _publish(case)
    assert result.desired_state == "draining"
    assert result.capability_revision == 1
    assert RayTarget.objects.count() == 1
    assert RayTargetPolicyRevision.objects.get().revision == 1
    assert RayTargetAttestationRevision.objects.get().pk == result.attestation_id
    capability = RayWorkerTargetCapability.objects.get()
    assert capability.pk == result.capability_id
    assert capability.attestation_id == result.attestation_id
    assert capability.target_policy_id == result.target_policy_id
    assert RayTargetProbeChallenge.objects.get().consumed_at == result.consumed_at
    assert (result.job_qualification is None) == (fixture_name == "core_case")
    case.lease.refresh_from_db()
    assert case.lease.last_heartbeat_at == NOW
    with pytest.raises(publication.CohortPublicationError):
        _publish(case)
    assert RayTargetAttestationRevision.objects.count() == 1


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
def test_automatic_discovery_rejects_caller_selected_keys_before_remote_work(
    request, fixture_name, clock
):
    case = request.getfixturevalue(fixture_name)
    if hasattr(case, "plan"):
        case.plan = replace(case.plan, target_key="bypass-the-drain")
    else:
        case.launch = replace(
            case.launch, request=replace(case.launch.request, target_key="bypass-the-drain")
        )
    with pytest.raises(publication.CohortPublicationError):
        _publish(case, activate_new_target=True)
    assert not clock.queries and clock.observation_calls == 0
    _no_publication()


def test_publication_independently_rejects_relabelled_discovery_observation(core_case):
    core_case.proof = build_ray_cluster_attestation(
        expectation=replace(core_case.proof.expectation, target_key="bypass-the-drain"),
        boundary=core_case.proof.boundary,
        nodes=core_case.proof.nodes,
        observed_at=core_case.proof.observed_at,
        expires_at=core_case.proof.expires_at,
    )
    with pytest.raises(publication.CohortPublicationError):
        _publish(core_case, activate_new_target=True)
    _no_publication()


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
def test_operator_named_drained_session_cannot_gain_an_automatically_enabled_sibling(
    request, fixture_name
):
    case = request.getfixturevalue(fixture_name)
    proof = case.proof if hasattr(case, "proof") else case.receipt.attestation
    expectation = replace(proof.expectation, target_key="operator-drained")
    target = RayTarget.objects.create(
        target_key=expectation.target_key,
        runner_family=expectation.runner_family.value,
        cluster_session=expectation.cluster_session,
        created_at=NOW - timedelta(seconds=1),
        **asdict(expectation.runtime),
    )
    policy = RayTargetPolicyRevision.objects.create(
        target=target,
        revision=1,
        desired_state="draining",
        expectation_schema_version=1,
        expectation_json=encode_ray_target_expectation(expectation),
        expectation_digest=ray_target_expectation_digest(expectation),
        created_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(publication.CohortPublicationError):
        _publish(case, activate_new_target=True)
    # The existing family/session unique constraint fences even differently
    # named insertions; the verified discovery proof is never relabelled.
    assert RayTarget.objects.get().pk == target.pk
    assert RayTargetPolicyRevision.objects.get().pk == policy.pk
    assert not RayTargetAttestationRevision.objects.exists()
    assert not RayWorkerTargetCapability.objects.exists()
    assert not RayTargetProbeChallenge.objects.filter(consumed_at__isnull=False).exists()


@pytest.mark.parametrize("change", ["alias-configuration", "package", "runtime"])
def test_another_manager_cannot_escape_existing_drain_by_changing_configuration(
    core_case, clock, monkeypatch, change
):
    first = _publish(core_case)
    package = "0.5.1" if change == "package" else "0.5.0"
    runtime = replace(RUNTIME, python_patch=15) if change == "runtime" else RUNTIME
    lease = TaskWorkerLease.objects.create(
        worker_id="second-manager",
        hostname=core_case.identity.hostname,
        pid=core_case.identity.pid,
        started_at=core_case.identity.started_at,
        last_heartbeat_at=NOW,
        capability_schema_version=1,
        django_ray_version=package,
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
    )
    identity = replace(core_case.identity, worker_id=lease.pk)
    issued = _issue(
        identity, "another-alias-and-endpoint" if change == "alias-configuration" else "primary"
    )
    monkeypatch.setattr(publication, "_local_runtime", lambda _ray: (package, runtime))
    proof = build_ray_cluster_attestation(
        expectation=replace(core_case.proof.expectation, runtime=runtime),
        boundary=core_case.proof.boundary,
        nodes=tuple(
            build_ray_node_observation(
                node_id=node.node_id, cluster_session=node.cluster_session, runtime=runtime
            )
            for node in core_case.proof.nodes
        ),
        observed_at=NOW + timedelta(seconds=3),
        expires_at=NOW + timedelta(seconds=30),
    )

    def observe(**_arguments):
        clock.now = NOW + timedelta(seconds=4)
        return proof

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)
    second = SimpleNamespace(
        identity=identity,
        issued=issued,
        plan=publication.CoreCohortProbePlan(issued.receipt, None, package, runtime),
    )
    if change == "runtime":
        with pytest.raises(publication.CohortPublicationError):
            _publish(second, activate_new_target=True, expected_attestation_revision=1)
        assert RayTargetAttestationRevision.objects.count() == 1
    else:
        result = _publish(second, activate_new_target=True, expected_attestation_revision=1)
        assert result.target_key == first.target_key and result.activation_policy_id is None
        assert RayTargetAttestationRevision.objects.count() == 2
    assert RayTarget.objects.count() == RayTargetPolicyRevision.objects.count() == 1
    assert RayTargetPolicyRevision.objects.get().desired_state == "draining"


def test_discovery_at_same_endpoint_creates_only_a_new_verified_session(
    core_case, clock, monkeypatch
):
    first = _publish(core_case)
    _lease_row, identity = _lease("replacement-session-manager")
    issued = _issue(identity)
    session = "session_replacement"
    expected = replace(
        core_case.proof.expectation,
        target_key=derive_cohort_target_key(RayRunnerFamily.RAY_CORE, session),
        cluster_session=session,
    )
    proof = build_ray_cluster_attestation(
        expectation=expected,
        boundary=core_case.proof.boundary,
        nodes=tuple(
            build_ray_node_observation(
                node_id=node.node_id, cluster_session=session, runtime=RUNTIME
            )
            for node in core_case.proof.nodes
        ),
        observed_at=NOW + timedelta(seconds=3),
        expires_at=NOW + timedelta(seconds=30),
    )

    def observe(**_arguments):
        clock.now = NOW + timedelta(seconds=4)
        return proof

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)
    case = SimpleNamespace(
        identity=identity,
        issued=issued,
        plan=publication.CoreCohortProbePlan(issued.receipt, None, "0.5.0", RUNTIME),
    )
    second = _publish(case, activate_new_target=True)
    assert first.target_key != second.target_key
    assert RayTarget.objects.count() == 2
    assert (
        RayTargetPolicyRevision.objects.get(target_id=first.target_key).desired_state == "draining"
    )
    assert (
        RayTargetPolicyRevision.objects.get(pk=second.activation_policy_id).desired_state
        == "active"
    )


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
@pytest.mark.parametrize(
    "phase",
    [
        "_register_ray_target_locked",
        "_record_ray_target_attestation_locked",
        "_advertise_ray_worker_target_capability_locked",
        "_consume_locked_probe_challenge",
    ],
)
def test_failure_after_each_write_rolls_back_the_whole_publication(
    request, fixture_name, phase, monkeypatch
):
    case = request.getfixturevalue(fixture_name)
    owner = (
        capabilities
        if phase.startswith("_advertise")
        else publication
        if phase.startswith("_consume")
        else coordination
    )
    original = getattr(owner, phase)

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise DatabaseError("secret diagnostic must not escape")

    monkeypatch.setattr(owner, phase, fail)
    with pytest.raises(publication.CohortPublicationError) as error:
        _publish(case)
    assert "secret" not in str(error.value)
    _no_publication()


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
def test_wrong_nonce_refuses_before_remote_observation(request, fixture_name, clock):
    case = request.getfixturevalue(fixture_name)
    with pytest.raises(publication.CohortPublicationError):
        _publish(case, nonce="0" * 64)
    assert not clock.queries and clock.observation_calls == 0
    _no_publication()


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
def test_expiry_while_waiting_for_final_lock_cannot_publish(
    request, fixture_name, monkeypatch, clock
):
    case = request.getfixturevalue(fixture_name)
    original = capabilities._locked_current_capability

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now = NOW + timedelta(seconds=30)
        return result

    monkeypatch.setattr(capabilities, "_locked_current_capability", delayed)
    with pytest.raises(publication.CohortPublicationError):
        _publish(case)
    _no_publication()


@pytest.mark.parametrize("seconds", [-1, 30, 301])
def test_clock_after_capability_write_still_fences_commit(core_case, clock, monkeypatch, seconds):
    original = capabilities._advertise_ray_worker_target_capability_locked

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now = NOW + timedelta(seconds=seconds)
        return result

    monkeypatch.setattr(capabilities, "_advertise_ray_worker_target_capability_locked", delayed)
    with pytest.raises(publication.CohortPublicationError):
        _publish(core_case)
    _no_publication()


def test_jobs_challenge_replacement_during_http_cannot_publish_old_receipt(job_case, clock):
    def replace_challenge():
        slot = job_case.issued.receipt
        replace_ray_target_probe_challenge(
            job_case.identity,
            slot.challenge_id,
            expected_configuration_digest=slot.configuration_digest,
            configuration_digest=slot.configuration_digest,
            expected_revision=slot.revision,
            expected_nonce=job_case.issued.nonce,
            now=clock.now,
        )

    clock.on_query = replace_challenge
    with pytest.raises(publication.CohortPublicationError):
        _publish(job_case)
    assert len(clock.queries) == 1
    _no_publication()


def test_jobs_requires_fixed_launch_matching_reserved_command_before_http(job_case, clock):
    job_case.launch = replace(job_case.launch, django_settings_module="tests.another_settings")
    with pytest.raises(publication.CohortPublicationError):
        _publish(job_case)
    assert not clock.queries
    _no_publication()


@pytest.mark.parametrize("refresh", [False, True])
def test_retargeted_challenge_refuses_before_locking_any_target(
    core_case, clock, monkeypatch, refresh
):
    if refresh:
        expectation = _target("primary")
        old = core_case.issued.receipt
        core_case.issued = replace_ray_target_probe_challenge(
            core_case.identity,
            old.challenge_id,
            expected_configuration_digest=old.configuration_digest,
            configuration_digest=old.configuration_digest,
            expected_revision=old.revision,
            expected_nonce=core_case.issued.nonce,
            expected_target_policy_id=RayTargetPolicyRevision.objects.get().pk,
            now=NOW,
        )
        core_case.plan = replace(
            core_case.plan,
            challenge=core_case.issued.receipt,
            target_key=expectation.target_key,
            expected_cluster_session=expectation.cluster_session,
        )
        core_case.proof = build_ray_cluster_attestation(
            expectation=expectation,
            boundary=core_case.proof.boundary,
            nodes=(
                build_ray_node_observation(
                    node_id="1" * 56,
                    cluster_session=expectation.cluster_session,
                    runtime=RUNTIME,
                ),
            ),
            observed_at=core_case.proof.observed_at,
            expires_at=core_case.proof.expires_at,
        )
    _target("secondary")
    second_policy = RayTargetPolicyRevision.objects.get(target_id="secondary")
    calls = []
    original = publication.observe_current_cohort_target

    def observe(**kwargs):
        proof = original(**kwargs)
        old = core_case.issued.receipt
        replace_ray_target_probe_challenge(
            core_case.identity,
            old.challenge_id,
            expected_configuration_digest=old.configuration_digest,
            configuration_digest="sha256:" + "b" * 64,
            expected_revision=old.revision,
            expected_nonce=core_case.issued.nonce,
            expected_target_policy_id=second_policy.pk,
            now=clock.now,
        )

        def unexpected(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("a stale publication must not lock or create any target")

        monkeypatch.setattr(publication, "_locked_expected_policy", unexpected)
        monkeypatch.setattr(coordination, "_register_ray_target_locked", unexpected)
        return proof

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)
    with pytest.raises(publication.CohortPublicationError) as error:
        _publish(core_case)
    assert error.value.reason is publication.CohortPublicationReason.CHALLENGE_CHANGED
    assert not calls
    assert RayTarget.objects.count() == 1 + refresh
    assert not RayTargetAttestationRevision.objects.exists()
    assert not RayWorkerTargetCapability.objects.exists()
    assert RayTargetProbeChallenge.objects.get().consumed_at is None


@pytest.mark.parametrize("automatic", [False, True])
def test_active_policy_requires_a_fresh_matching_proof(core_case, clock, monkeypatch, automatic):
    first = _publish(core_case, activate_new_target=automatic)
    original_proof = core_case.proof
    active_expectation = replace(original_proof.expectation, policy_revision=2)
    if automatic:
        active_policy = RayTargetPolicyRevision.objects.get(pk=first.activation_policy_id)
        assert active_policy.desired_state == "active" and active_policy.revision == 2
    else:
        assert first.activation_policy_id is None
        active_policy = RayTargetPolicyRevision.objects.create(
            target_id=first.target_key,
            revision=2,
            desired_state="active",
            expectation_schema_version=1,
            expectation_json=encode_ray_target_expectation(active_expectation),
            expectation_digest=ray_target_expectation_digest(active_expectation),
            created_at=NOW + timedelta(seconds=3),
        )
    # An explicit policy append neither relabels old proof nor renews capability.
    assert RayTargetAttestationRevision.objects.get().policy_id == first.target_policy_id
    assert RayWorkerTargetCapability.objects.get().target_policy_id == first.target_policy_id
    row = RayTargetProbeChallenge.objects.get()
    clock.now = NOW + timedelta(seconds=4)
    core_case.issued = replace_ray_target_probe_challenge(
        core_case.identity,
        row.pk,
        expected_configuration_digest=row.configuration_digest,
        configuration_digest=row.configuration_digest,
        expected_revision=row.revision,
        expected_nonce=core_case.issued.nonce,
        expected_target_policy_id=active_policy.pk,
        now=clock.now,
    )
    core_case.plan = replace(
        core_case.plan,
        challenge=core_case.issued.receipt,
        target_key=active_expectation.target_key,
        expected_cluster_session=active_expectation.cluster_session,
        policy_revision=2,
    )

    def observe(**kwargs):
        assert kwargs["policy_revision"] == 2
        clock.now = NOW + timedelta(seconds=6)
        return core_case.proof

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)
    with pytest.raises(publication.CohortPublicationError):
        _publish(core_case, expected_capability_revision=1)
    assert not RayTargetAttestationRevision.objects.filter(policy=active_policy).exists()
    assert RayWorkerTargetCapability.objects.get().revision == 1
    assert RayTargetProbeChallenge.objects.get().consumed_at is None
    core_case.proof = build_ray_cluster_attestation(
        expectation=active_expectation,
        boundary=original_proof.boundary,
        nodes=original_proof.nodes,
        observed_at=NOW + timedelta(seconds=7),
        expires_at=NOW + timedelta(seconds=30),
    )

    def fresh_observation(**kwargs):
        clock.now = NOW + timedelta(seconds=8)
        return core_case.proof

    monkeypatch.setattr(publication, "observe_current_cohort_target", fresh_observation)
    result = _publish(core_case, expected_capability_revision=1)
    assert result.desired_state == "active"
    assert result.target_policy_id == active_policy.pk
    assert result.capability_revision == 2
    assert RayTargetAttestationRevision.objects.count() == 2
    capability = RayWorkerTargetCapability.objects.get()
    assert capability.target_policy_id == active_policy.pk
    assert capability.attestation_id == result.attestation_id


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
@pytest.mark.parametrize("existing", [False, True])
def test_automatic_activation_is_only_for_a_target_created_in_this_publication(
    request, fixture_name, existing
):
    case = request.getfixturevalue(fixture_name)
    expectation = (
        case.proof.expectation if hasattr(case, "proof") else case.receipt.attestation.expectation
    )
    if existing:
        target = RayTarget.objects.create(
            target_key=expectation.target_key,
            runner_family=expectation.runner_family.value,
            cluster_session=expectation.cluster_session,
            created_at=NOW - timedelta(seconds=1),
            **asdict(RUNTIME),
        )
        RayTargetPolicyRevision.objects.create(
            target=target,
            revision=1,
            desired_state="draining",
            expectation_schema_version=1,
            expectation_json=encode_ray_target_expectation(expectation),
            expectation_digest=ray_target_expectation_digest(expectation),
            created_at=NOW - timedelta(seconds=1),
        )
    result = _publish(case, activate_new_target=True)
    assert result.desired_state == "draining"
    assert RayTargetAttestationRevision.objects.get().policy_id == result.target_policy_id
    assert RayWorkerTargetCapability.objects.get().target_policy_id == result.target_policy_id
    if existing:
        assert result.activation_policy_id is None
        assert RayTargetPolicyRevision.objects.get().desired_state == "draining"
    else:
        assert RayTargetPolicyRevision.objects.count() == 2
        active = RayTargetPolicyRevision.objects.get(pk=result.activation_policy_id)
        assert active.revision == 2 and active.desired_state == "active"
        assert not RayTargetAttestationRevision.objects.filter(policy=active).exists()
        assert not RayWorkerTargetCapability.objects.filter(target_policy=active).exists()


def test_automatic_activation_preserves_an_explicitly_drained_later_revision(
    core_case, clock, monkeypatch
):
    first = _publish(core_case, activate_new_target=True)
    active_policy = RayTargetPolicyRevision.objects.get(pk=first.activation_policy_id)
    assert active_policy.revision == 2 and active_policy.desired_state == "active"
    drained_expectation = replace(core_case.proof.expectation, policy_revision=3)
    drained_policy = RayTargetPolicyRevision.objects.create(
        target_id=first.target_key,
        revision=3,
        desired_state="draining",
        expectation_schema_version=1,
        expectation_json=encode_ray_target_expectation(drained_expectation),
        expectation_digest=ray_target_expectation_digest(drained_expectation),
        created_at=NOW + timedelta(seconds=3),
    )
    row = RayTargetProbeChallenge.objects.get()
    clock.now = NOW + timedelta(seconds=4)
    core_case.issued = replace_ray_target_probe_challenge(
        core_case.identity,
        row.pk,
        expected_configuration_digest=row.configuration_digest,
        configuration_digest=row.configuration_digest,
        expected_revision=row.revision,
        expected_nonce=core_case.issued.nonce,
        expected_target_policy_id=drained_policy.pk,
        now=clock.now,
    )
    core_case.plan = replace(
        core_case.plan,
        challenge=core_case.issued.receipt,
        target_key=drained_expectation.target_key,
        expected_cluster_session=drained_expectation.cluster_session,
        policy_revision=3,
    )
    core_case.proof = build_ray_cluster_attestation(
        expectation=drained_expectation,
        boundary=core_case.proof.boundary,
        nodes=core_case.proof.nodes,
        observed_at=NOW + timedelta(seconds=5),
        expires_at=NOW + timedelta(seconds=30),
    )

    def observe(**arguments):
        assert arguments["policy_revision"] == 3
        clock.now = NOW + timedelta(seconds=6)
        return core_case.proof

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)
    result = _publish(core_case, expected_capability_revision=1, activate_new_target=True)
    assert result.activation_policy_id is None
    assert result.desired_state == "draining"
    assert result.target_policy_id == drained_policy.pk
    assert result.capability_revision == 2
    assert RayTargetPolicyRevision.objects.count() == 3
    latest = RayTargetPolicyRevision.objects.latest("revision")
    assert latest.pk == drained_policy.pk and latest.desired_state == "draining"
    capability = RayWorkerTargetCapability.objects.get()
    assert capability.target_policy_id == drained_policy.pk
    assert capability.attestation_id == result.attestation_id
    assert (
        RayTargetAttestationRevision.objects.get(pk=result.attestation_id).policy_id
        == drained_policy.pk
    )
    assert RayTargetProbeChallenge.objects.get().consumed_at == result.consumed_at


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
@pytest.mark.parametrize("failure", ["database", "expiry"])
def test_automatic_activation_failure_rolls_back_creation_and_all_proof(
    request, fixture_name, failure, clock, monkeypatch
):
    case = request.getfixturevalue(fixture_name)
    original = publication._activate_new_target_locked

    def fail(*args, **kwargs):
        result = original(*args, **kwargs)
        if failure == "database":
            raise DatabaseError("bootstrap failure")
        clock.now = NOW + timedelta(seconds=300)
        return result

    monkeypatch.setattr(publication, "_activate_new_target_locked", fail)
    with pytest.raises(publication.CohortPublicationError):
        _publish(case, activate_new_target=True)
    _no_publication()


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
@pytest.mark.parametrize("invalid", [None, 0, 1, "true"])
def test_invalid_bootstrap_option_refuses_before_remote_observation(
    request, fixture_name, invalid, clock
):
    case = request.getfixturevalue(fixture_name)
    with pytest.raises(publication.CohortPublicationError):
        _publish(case, activate_new_target=invalid)
    assert not clock.queries and clock.observation_calls == 0
    _no_publication()


@pytest.mark.parametrize("change", ["package", "heartbeat"])
def test_jobs_lease_change_during_http_cannot_publish(job_case, clock, change):
    def alter_lease():
        if change == "package":
            job_case.lease.django_ray_version = "0.5.1"
            job_case.lease.save(update_fields=["django_ray_version"])
        else:
            job_case.lease.last_heartbeat_at = NOW - timedelta(seconds=60)
            job_case.lease.save(update_fields=["last_heartbeat_at"])

    clock.on_query = alter_lease
    with pytest.raises(publication.CohortPublicationError):
        _publish(job_case)
    assert len(clock.queries) == 1
    _no_publication()


@pytest.mark.parametrize("fixture_name", ["core_case", "job_case"])
@pytest.mark.parametrize("revision", ["attestation", "capability"])
def test_wrong_expected_revision_rolls_back_publication(request, fixture_name, revision):
    case = request.getfixturevalue(fixture_name)
    with pytest.raises(publication.CohortPublicationError):
        _publish(case, **{f"expected_{revision}_revision": 1})
    _no_publication()


@pytest.mark.parametrize("stage", ["receipt", "job"])
def test_pending_jobs_never_publish_positive_records(job_case, clock, monkeypatch, stage):
    if stage == "receipt":
        monkeypatch.setattr(
            publication, "read_cohort_job_reservation", lambda *args, **kwargs: None
        )
    else:
        original = cohort_job_http.fetch_reserved_cohort_job_details

        def running(endpoint, handle):
            details = original(endpoint, handle)
            return details.model_copy(update={"status": JobStatus.RUNNING})

        monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", running)
    assert _publish(job_case) is None
    assert len(clock.queries) == (stage == "job")
    _no_publication()


@pytest.mark.parametrize("manual", [False, True])
def test_open_transaction_cannot_contact_ray(core_case, clock, manual):
    if manual:
        connection.set_autocommit(False)
        try:
            with pytest.raises(publication.CohortPublicationError):
                _publish(core_case)
        finally:
            connection.rollback()
            connection.set_autocommit(True)
    else:
        with transaction.atomic(), pytest.raises(publication.CohortPublicationError):
            _publish(core_case)
    assert clock.observation_calls == 0
    _no_publication()


@pytest.mark.postgresql
@pytest.mark.parametrize("automatic", [False, True])
def test_postgresql_concurrent_jobs_publications_have_one_atomic_winner(job_case, clock, automatic):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    barrier = Barrier(2, timeout=10)
    clock.on_query = barrier.wait

    def publish():
        close_old_connections()
        try:
            try:
                return _publish(job_case, activate_new_target=automatic)
            except publication.CohortPublicationError:
                return None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: publish(), range(2)))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert RayTarget.objects.count() == 1
    assert RayTargetPolicyRevision.objects.count() == 1 + automatic
    assert RayTargetAttestationRevision.objects.count() == 1
    assert RayWorkerTargetCapability.objects.count() == 1
    assert RayTargetAttestationRevision.objects.get().policy_id == winner.target_policy_id
    assert RayWorkerTargetCapability.objects.get().target_policy_id == winner.target_policy_id
    if automatic:
        active = RayTargetPolicyRevision.objects.get(pk=winner.activation_policy_id)
        assert active.revision == 2 and active.desired_state == "active"
        assert not RayTargetAttestationRevision.objects.filter(policy=active).exists()
        assert not RayWorkerTargetCapability.objects.filter(target_policy=active).exists()
    else:
        assert winner.activation_policy_id is None
        assert RayTargetPolicyRevision.objects.get().desired_state == "draining"
    assert RayTargetProbeChallenge.objects.get().consumed_at is not None


@pytest.mark.postgresql
def test_postgresql_different_managers_discover_one_session_and_activate_once(
    core_case, clock, monkeypatch
):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL unique insertion and row locks required")
    _lease_row, identity = _lease("second-manager")
    issued = _issue(identity, "other-alias")
    second = SimpleNamespace(
        identity=identity,
        issued=issued,
        plan=publication.CoreCohortProbePlan(issued.receipt, None, "0.5.0", RUNTIME),
    )
    barrier = Barrier(2, timeout=10)

    def observe(**_arguments):
        barrier.wait()
        clock.now = NOW + timedelta(seconds=2)
        return core_case.proof

    monkeypatch.setattr(publication, "observe_current_cohort_target", observe)

    def publish(case):
        close_old_connections()
        try:
            try:
                return _publish(case, activate_new_target=True)
            except publication.CohortPublicationError:
                return None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, [core_case, second]))
    assert sum(result is not None for result in results) == 1
    assert RayTarget.objects.count() == 1
    assert RayTargetPolicyRevision.objects.count() == 2
    assert RayTargetPolicyRevision.objects.get(revision=2).desired_state == "active"
    assert (
        RayTargetAttestationRevision.objects.count()
        == RayWorkerTargetCapability.objects.count()
        == 1
    )
    assert RayTargetProbeChallenge.objects.filter(consumed_at__isnull=False).count() == 1
