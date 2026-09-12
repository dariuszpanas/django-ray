"""Dormant claim ledger and lifecycle fences; no native Ray resource is used."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, transaction

from django_ray.execution_codec import ExecutionIdentity
from django_ray.maintenance import maintenance_admission_barrier
from django_ray.models import (
    RayTaskCohortClaim,
    RayTaskExecution,
    RayTaskTargetBinding,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.target import cohort_claim_storage as storage
from django_ray.target.cohort_claim import (
    CohortBindingSpec,
    CohortHoldBoundary,
    CohortHoldReason,
    CohortManagerRuntime,
    CohortResolutionKind,
    CohortRunnerFamily,
    cohort_task_runtime_env_snapshot_digest,
    encode_cohort_claim_facts,
)
from django_ray.target.cohort_intent import (
    CohortIntent,
    CohortSelectionPolicy,
    cohort_intent_digest,
)
from django_ray.target.cohort_intent_storage import persist_cohort_intent
from tests.integration.test_cohort_intent_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.unit.test_cohort_claim import DIGEST, NOW, PYTHON

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgresql", id="postgresql", marks=pytest.mark.postgresql),
    ]
)
def ledger_database(request):
    """Select the same SQL fences explicitly in each supported database lane."""
    if connection.vendor != request.param:
        pytest.skip(f"This case requires {request.param}")


@pytest.fixture(autouse=True)
def isolated_sqlite_ledger_maintenance():
    """Delete fixture execution rows first during isolated stopped test teardown.

    Production DELETE fences remain strict. Django's normal flush can then
    remove orphaned fixture ledger rows while FK checks are disabled.
    """
    yield
    if connection.vendor == "sqlite":
        with connection.constraint_checks_disabled(), connection.cursor() as cursor:
            # This isolated database is stopped: discard fixture obligations
            # without inventing a successful physical cleanup observation.
            # Restore the exact product fence before any following test runs.
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='ray_jobcleanup_delete_0033'"
            )
            cleanup_trigger = cursor.fetchone()
            if cleanup_trigger is not None:
                cursor.execute("DROP TRIGGER ray_jobcleanup_delete_0033")
                try:
                    cursor.execute("DELETE FROM django_ray_raycohortjobcleanup")
                finally:
                    cursor.execute(cleanup_trigger[0])
            cursor.execute(
                "DELETE FROM django_ray_raytaskexecution WHERE id IN (SELECT binding_id FROM django_ray_raytaskcohortclaim)"
            )


def _lease(name="manager", *, started=NOW - timedelta(seconds=10)):
    row = TaskWorkerLease.objects.create(
        worker_id=name,
        hostname="host",
        pid=100,
        started_at=started,
        last_heartbeat_at=NOW,
        capability_schema_version=1,
        django_ray_version="0.5.0",
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    return row, WorkerLeaseIdentity(name, row.hostname, row.pid, row.started_at)


@pytest.fixture
def case(monkeypatch):
    from types import SimpleNamespace

    lease, identity = _lease()
    intent = CohortIntent(
        "0.5.0", "default", DIGEST, "sha256:" + "b" * 64, CohortSelectionPolicy.WORKER_SELECTED
    )
    with transaction.atomic():
        task = RayTaskExecution.objects.create(
            task_id="claim-task",
            callable_path="tests.tasks.add",
            execution_protocol_version=3,
            created_with_django_ray_version="0.5.0",
            created_at=NOW,
            runtime_env_hash=hashlib.sha256(b"{}").hexdigest(),
            runtime_env_json="{}",
        )
        persist_cohort_intent(task.pk, intent, now=NOW)
    value = SimpleNamespace(
        task=task,
        lease=lease,
        owner=identity,
        intent=intent,
        now=NOW + timedelta(seconds=1),
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(storage, "_clock", lambda: value.now)
    return value


def _claim(case, **changes):
    arguments = {
        "expected_identity": ExecutionIdentity(
            case.task.pk,
            case.task.task_id,
            case.task.attempt_number,
            case.task.execution_generation,
        ),
        "binding_spec": CohortBindingSpec(CohortRunnerFamily.SYNC, "0.5.0", sync_python=PYTHON),
        "manager_runtime": CohortManagerRuntime("0.5.0", PYTHON),
        "expected_intent_digest": cohort_intent_digest(case.intent),
        "expected_runtime_env_snapshot_digest": cohort_task_runtime_env_snapshot_digest(
            profile=case.task.runtime_env_profile,
            serialized=case.task.runtime_env_json,
            digest=case.task.runtime_env_hash,
        ),
        "now": case.now,
    }
    arguments.update(changes)
    with transaction.atomic(), maintenance_admission_barrier() as barrier:
        return storage.claim_cohort_execution(case.owner, admission_barrier=barrier, **arguments)


def _mutate(case, record, function, **arguments):
    with transaction.atomic():
        return function(
            case.owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            now=case.now,
            **arguments,
        )


def _hold(case, record):
    return _mutate(
        case,
        record,
        storage.hold_cohort_claim,
        reason=CohortHoldReason.TRANSPORT_UNCERTAIN,
        boundary=CohortHoldBoundary.CONTROL,
        application_invoked=None,
        evidence_digest=DIGEST,
    )


def test_sync_claim_is_atomic_and_contains_no_ray_proof(case):
    record = _claim(case)
    case.task.refresh_from_db()
    assert case.task.state == "RUNNING"
    assert case.task.execution_generation == 1
    assert record.facts.claim_attestation_id is None
    assert record.facts.manager.ray_version is None
    assert record.facts.binding.sync_python == PYTHON
    assert record.owner == case.owner
    assert RayTaskTargetBinding.objects.get(execution=case.task).target_policy_id is None


def test_repeated_claim_rolls_back(case):
    record = _claim(case)
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case)
    assert RayTaskCohortClaim.objects.count() == 1
    assert RayTaskCohortClaim.objects.get().facts_digest == record.facts_digest


@pytest.mark.usefixtures("ledger_database")
def test_held_generation_blocks_terminal_retry_and_handle_replacement(case):
    record = _hold(case, _claim(case))
    for changes in (
        {"state": "LOST"},
        {"state": "FAILED"},
        {"state": "QUEUED", "execution_generation": 2},
        {"ray_job_id": "replacement"},
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            RayTaskExecution.objects.filter(pk=case.task.pk).update(**changes)
    assert RayTaskCohortClaim.objects.get(pk=record.claim_id).disposition == "HELD"
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskCohortClaim.objects.filter(pk=record.claim_id).delete()
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM django_ray_raytaskcohortclaim WHERE id=%s", [record.claim_id])


def test_late_completion_preserves_initial_hold(case):
    record = _claim(case)
    record = _mutate(case, record, storage.prepare_cohort_claim, request_digest=DIGEST)
    record = _mutate(case, record, storage.mark_cohort_claim_dispatched)
    record = _hold(case, record)
    first = RayTaskCohortClaim.objects.get(pk=record.claim_id)
    case.now += timedelta(seconds=5)
    with transaction.atomic():
        record = storage.resolve_cohort_claim(
            case.owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            now=case.now,
            kind=CohortResolutionKind.APPLICATION_COMPLETED,
            evidence_digest="sha256:" + "c" * 64,
        )
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="SUCCEEDED")
    final = RayTaskCohortClaim.objects.get(pk=record.claim_id)
    assert final.facts_json == first.facts_json
    assert final.hold_reason == first.hold_reason
    assert final.hold_evidence_digest == first.hold_evidence_digest
    assert final.held_at == first.held_at
    assert final.resolution_digest != final.hold_evidence_digest


@pytest.mark.parametrize(
    "changes",
    [
        {"facts_digest": "sha256:" + "d" * 64},
        {"attempt_number": 2},
        {"facts_json": "{}"},
        {"owner_lease_pid": 101},
        {"revision": 100},
        {"disposition": "RESOLVED"},
    ],
    ids=("facts-digest", "attempt", "facts-json", "owner", "revision", "resolution"),
)
@pytest.mark.usefixtures("ledger_database")
def test_raw_claim_mutation_is_fenced(case, changes):
    record = _hold(case, _claim(case))
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskCohortClaim.objects.filter(pk=record.claim_id).update(
            **{"revision": record.revision + 1, **changes}
        )
    retained = RayTaskCohortClaim.objects.get(pk=record.claim_id)
    assert retained.revision == record.revision
    assert retained.disposition == "HELD"
    assert retained.facts_digest == record.facts_digest


@pytest.mark.usefixtures("ledger_database")
def test_binding_mode_and_package_cannot_change(case):
    _claim(case)
    for changes in (
        {"package_version": "0.5.1"},
        {"runner_family": "ray_job"},
        {"sync_python_patch": 15},
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            RayTaskTargetBinding.objects.filter(execution=case.task).update(**changes)


def test_nested_refusal_cannot_establish_outer_noninvocation(case):
    record = _claim(case)
    with pytest.raises(storage.CohortClaimStorageError):
        _mutate(
            case,
            record,
            storage.hold_cohort_claim,
            reason=CohortHoldReason.RUNTIME_MISMATCH,
            boundary=CohortHoldBoundary.NESTED,
            application_invoked=False,
            evidence_digest=DIGEST,
        )


def test_stale_revision_and_prepare_replay_fail(case):
    record = _claim(case)
    prepared = _mutate(case, record, storage.prepare_cohort_claim, request_digest=DIGEST)
    with pytest.raises(storage.CohortClaimStorageError):
        _hold(case, record)
    with pytest.raises(storage.CohortClaimStorageError):
        _mutate(case, prepared, storage.prepare_cohort_claim, request_digest=DIGEST)


def test_same_id_recreated_lease_is_not_owner(case):
    record = _claim(case)
    case.lease.delete()
    _, identity = _lease(started=NOW)
    case.owner = identity
    with pytest.raises(storage.CohortClaimStorageError):
        _hold(case, record)
    assert RayTaskCohortClaim.objects.get().facts_digest == record.facts_digest


def test_adoption_changes_current_owner_only(case):
    record = _hold(case, _claim(case))
    original = record.facts
    case.lease.is_active = False
    case.lease.stopped_at = case.now
    case.lease.save(update_fields=("is_active", "stopped_at"))
    _, adopter = _lease("adopter")
    with transaction.atomic():
        adopted = storage.adopt_cohort_claim(
            adopter,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            expected_owner=case.owner,
            now=case.now,
        )
    assert adopted.owner == adopter
    assert adopted.facts == original
    assert adopted.disposition == "HELD"
    case.owner = adopter
    resolved = _mutate(
        case,
        adopted,
        storage.resolve_cohort_claim,
        kind=CohortResolutionKind.VERIFIED_CANCELLED,
        evidence_digest=DIGEST,
    )
    assert resolved.disposition == "RESOLVED"


def test_fresh_clock_after_lock_rejects_expired_lease(case, monkeypatch):
    original = storage._lock_task

    def block(pk, *, using):
        row = original(pk, using=using)
        case.now += timedelta(hours=1)
        return row

    monkeypatch.setattr(storage, "_lock_task", block)
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case)
    case.task.refresh_from_db()
    assert case.task.state == "QUEUED"
    assert not RayTaskTargetBinding.objects.exists()


def test_intent_observation_does_not_require_task_content_equality(case):
    case.task.runtime_env_json = "encrypted:task-specific-current-storage"
    case.task.save(update_fields=("runtime_env_json",))
    record = _claim(case)
    assert record.facts.intent_digest == cohort_intent_digest(case.intent)


def test_stale_intent_or_snapshot_refuses_before_binding(case):
    for field in ("expected_intent_digest", "expected_runtime_env_snapshot_digest"):
        with pytest.raises(storage.CohortClaimStorageError):
            _claim(case, **{field: "sha256:" + "f" * 64})
    assert not RayTaskTargetBinding.objects.exists()


def test_manual_database_alias_and_missing_transaction_fail(case):
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, using="other")
    with pytest.raises(storage.CohortClaimStorageError):
        storage.prepare_cohort_claim(
            case.owner,
            1,
            expected_identity=ExecutionIdentity(1, "t", 1, 1),
            expected_revision=1,
            request_digest=DIGEST,
            now=case.now,
        )


def test_p3_cannot_be_claimed_without_ledger(case):
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=case.task.pk).update(
            state="RUNNING", execution_generation=1, claimed_by_worker=case.owner.worker_id
        )


def _ray_arguments(
    case,
    family,
    *,
    active=True,
    endpoint_ttl=25,
    challenge_ttl=60,
    native_job_id="01000000",
    target_key="claim-target",
    cluster_session="session_claim",
    observed_at=NOW,
):
    from django_ray.models import RayTarget, RayTargetPolicyRevision, RayWorkerTargetCapability
    from django_ray.target import capabilities, coordination
    from django_ray.target.attestation import RayRuntimeVersion, RayTargetExpectation
    from django_ray.target.cohort_publication import _activate_new_target_locked
    from tests.integration.test_ray_worker_target_capabilities import _attestation

    runtime = RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14)
    expectation = RayTargetExpectation(target_key, family, cluster_session, 1, runtime)
    with transaction.atomic():
        capabilities._locked_exact_lease(case.owner, using="default", vendor=connection.vendor)
        coordination._register_ray_target_locked(expectation, now=observed_at)
        if active:
            _activate_new_target_locked(
                RayTarget.objects.get(pk=target_key), expectation, now=observed_at, using="default"
            )
            expectation = replace(expectation, policy_revision=2)
        coordination._record_ray_target_attestation_locked(
            expectation.target_key,
            _attestation(
                expectation,
                observed_at=observed_at,
                expires_at=observed_at + timedelta(seconds=endpoint_ttl),
            ),
            expected_policy_revision=expectation.policy_revision,
            expected_attestation_revision=0,
            now=observed_at,
        )
        capabilities._advertise_ray_worker_target_capability_locked(
            case.owner,
            expectation.target_key,
            runtime,
            manager_runner_family=family,
            expected_policy_revision=expectation.policy_revision,
            expected_attestation_revision=1,
            expected_capability_revision=0,
            now=observed_at,
        )
    policy = RayTargetPolicyRevision.objects.get(
        target_id=expectation.target_key, revision=expectation.policy_revision
    )
    cap = RayWorkerTargetCapability.objects.get(lease_id=case.owner.worker_id)
    arguments = {
        "binding_spec": CohortBindingSpec(CohortRunnerFamily(family.value), "0.5.0", policy.pk),
        "manager_runtime": CohortManagerRuntime("0.5.0", PYTHON, (2, 58, 0)),
        "capability_id": cap.pk,
        "capability_revision": cap.revision,
    }
    if family.value == "ray_job":
        arguments.update(
            _published_job_qualification(
                case,
                expectation,
                policy,
                cap,
                challenge_ttl=challenge_ttl,
                native_job_id=native_job_id,
                probe_started=observed_at,
            )
        )
    return arguments


def _published_job_qualification(
    case,
    expectation,
    policy,
    cap,
    *,
    challenge_ttl,
    native_job_id,
    probe_started=NOW,
    issued=None,
):
    """Use the actual publisher/inspector seam with a fake typed HTTP reply."""
    from ray.dashboard.modules.job.common import JobStatus
    from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

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
        cohort_job_control,
        cohort_job_http,
        cohort_job_receipt_storage,
        cohort_publication,
        cohort_runtime,
    )
    from django_ray.target.attestation import RayRunnerFamily, decode_ray_cluster_attestation
    from django_ray.target.cohort_job_receipt import CohortJobReceipt
    from django_ray.target.cohort_probe_challenges import issue_ray_target_probe_challenge

    runtime = expectation.runtime
    for module in (cohort_job_control, cohort_job_receipt_storage, cohort_publication):
        case.monkeypatch.setattr(module, "_now", lambda: case.now)
    for module in (cohort_publication, cohort_runtime):
        case.monkeypatch.setattr(module, "_local_runtime", lambda _ray: ("0.5.0", runtime))
    case.now = probe_started
    if issued is None:
        issued = issue_ray_target_probe_challenge(
            case.owner,
            case.intent.configuration_digest,
            runner_family=RayRunnerFamily.RAY_JOB,
            now=case.now,
            expected_target_policy_id=policy.pk,
            ttl_seconds=challenge_ttl,
        )
    slot = issued.receipt
    request = CohortProbeJobRequest(
        slot.challenge_id,
        slot.revision,
        CohortProbeJobLease(
            case.owner.worker_id, case.owner.hostname, case.owner.pid, case.owner.started_at
        ),
        slot.configuration_digest,
        expectation.target_key,
        RayRunnerFamily.RAY_JOB,
        "0.5.0",
        runtime,
        expectation.cluster_session,
        policy.pk,
        policy.revision,
        slot.issued_at,
        slot.expires_at,
    )
    environment = {"env_vars": {"DJANGO_SETTINGS_MODULE": "tests.probe_settings"}}
    endpoint = "http://ray-head:8265"
    launch = CohortProbeJobLaunch(
        request,
        probe_job_request_digest(request),
        endpoint,
        cohort_job_control.cohort_probe_submitted_runtime_env_digest(environment),
        "tests.probe_settings",
    )
    entrypoint = probe_job_launch_entrypoint(launch)
    cohort_job_receipt_storage.reserve_cohort_job_probe(
        case.owner,
        request,
        nonce=issued.nonce,
        jobs_endpoint=endpoint,
        entrypoint=entrypoint,
        submitted_runtime_env=environment,
    )
    original = decode_ray_cluster_attestation(cap.attestation.attestation_json)
    receipt = CohortJobReceipt(
        request,
        probe_job_request_digest(request),
        probe_job_submission_id(request),
        native_job_id,
        "0.5.0",
        original,
        probe_started + timedelta(microseconds=100000),
    )
    case.now = probe_started + timedelta(microseconds=200000)
    cohort_job_receipt_storage.write_cohort_job_receipt(receipt)
    assert JobDetails is not None
    details = JobDetails(
        type=JobType.SUBMISSION,
        submission_id=receipt.submission_id,
        job_id=receipt.native_job_id,
        status=JobStatus.SUCCEEDED,
        entrypoint=entrypoint,
        metadata=probe_job_metadata(request),
        runtime_env=environment,
    )
    case.job_reads = []

    def fetch(address, submission_id):
        assert not connection.in_atomic_block
        case.job_reads.append((address, submission_id))
        return details

    case.monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", fetch)
    case.now = probe_started + timedelta(microseconds=300000)
    published = cohort_publication.publish_cohort_job_probe(
        case.owner,
        launch,
        nonce=issued.nonce,
        expected_attestation_revision=cap.attestation.revision,
        expected_capability_revision=cap.revision,
    )
    case.now = probe_started + timedelta(seconds=1)
    case.job_issued, case.job_request, case.job_receipt, case.job_publication = (
        issued,
        request,
        receipt,
        published,
    )
    assert published.job_qualification is not None
    return {
        "capability_id": published.capability_id,
        "capability_revision": published.capability_revision,
        "job_qualification": published.job_qualification,
    }


@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
def test_ray_claim_reuses_actual_policy_attestation_and_capability(case, family):
    from django_ray.models import RayWorkerTargetCapability
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily(family))
    record = _claim(case, **arguments)
    assert record.facts.capability.capability_id == arguments["capability_id"]
    assert record.facts.target_policy_id == arguments["binding_spec"].target_policy_id
    assert record.facts.manager.ray_version == (2, 58, 0)
    case.lease.delete()
    assert not RayWorkerTargetCapability.objects.exists()
    retained = RayTaskCohortClaim.objects.get(pk=record.claim_id)
    assert retained.facts_digest == record.facts_digest
    assert retained.claim_attestation_id is not None


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
@pytest.mark.parametrize("preexisting_binding", [False, True])
def test_first_claim_never_uses_a_drained_target(case, preexisting_binding, family):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily(family), active=False)
    if preexisting_binding:
        RayTaskTargetBinding.objects.create(
            execution=case.task,
            schema_version=2,
            runner_family=family,
            package_version="0.5.0",
            target_policy_id=arguments["binding_spec"].target_policy_id,
            created_at=NOW,
        )
    with pytest.raises(storage.CohortClaimStorageError) as caught:
        _claim(case, **arguments)
    assert caught.value.reason is storage.CohortClaimStorageReason.PROOF_UNAVAILABLE
    assert not RayTaskCohortClaim.objects.exists()
    case.task.refresh_from_db()
    assert (case.task.state, case.task.execution_generation) == ("QUEUED", 0)
    assert RayTaskTargetBinding.objects.count() == int(preexisting_binding)


def _fresh_draining_ray_arguments(case, arguments):
    """Append a real policy/proof and renew this exact lease's qualification."""
    from django_ray.models import RayTargetPolicyRevision, RayWorkerTargetCapability
    from django_ray.target import capabilities, coordination
    from django_ray.target.attestation import (
        decode_ray_target_expectation,
        encode_ray_target_expectation,
        ray_target_expectation_digest,
    )
    from django_ray.target.cohort_probe_challenges import replace_ray_target_probe_challenge
    from tests.integration.test_ray_worker_target_capabilities import _attestation

    cap = RayWorkerTargetCapability.objects.get(pk=arguments["capability_id"])
    expectation = replace(
        decode_ray_target_expectation(cap.target_policy.expectation_json),
        policy_revision=cap.target_policy.revision + 1,
    )
    case.now += timedelta(seconds=2)
    with transaction.atomic():
        capabilities._locked_exact_lease(case.owner, using="default", vendor=connection.vendor)
        capabilities._locked_capability_target(
            target_key=expectation.target_key, using="default", vendor=connection.vendor
        )
        policy = RayTargetPolicyRevision.objects.create(
            target_id=expectation.target_key,
            revision=expectation.policy_revision,
            desired_state="draining",
            expectation_schema_version=1,
            expectation_json=encode_ray_target_expectation(expectation),
            expectation_digest=ray_target_expectation_digest(expectation),
            created_at=case.now,
        )
        coordination._record_ray_target_attestation_locked(
            expectation.target_key,
            _attestation(
                expectation,
                observed_at=case.now,
                expires_at=case.now + timedelta(seconds=25),
            ),
            expected_policy_revision=expectation.policy_revision,
            expected_attestation_revision=0,
            now=case.now,
        )
        renewed = capabilities._advertise_ray_worker_target_capability_locked(
            case.owner,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=expectation.runner_family,
            expected_policy_revision=expectation.policy_revision,
            expected_attestation_revision=1,
            expected_capability_revision=cap.revision,
            now=case.now,
        )
    refreshed = dict(arguments, capability_revision=renewed.revision)
    if expectation.runner_family.value == "ray_job":
        old = arguments["job_qualification"]
        issued = replace_ray_target_probe_challenge(
            case.owner,
            old.challenge_id,
            expected_configuration_digest=old.configuration_digest,
            configuration_digest=old.configuration_digest,
            expected_revision=old.consumed_challenge_revision,
            expected_nonce=case.job_issued.nonce,
            expected_target_policy_id=policy.pk,
            now=case.now,
        )
        cap.refresh_from_db()
        refreshed.update(
            _published_job_qualification(
                case,
                expectation,
                policy,
                cap,
                challenge_ttl=60,
                native_job_id="02000000",
                probe_started=case.now,
                issued=issued,
            )
        )
    return refreshed, policy


def _resolve_ray_claim_and_queue_next_attempt(case, record):
    """Model an independently verified cancellation followed by manual retry.

    Application completion has its own Jobs cleanup-obligation tests; this
    binding fixture needs an already terminal original execution.
    """
    prepared = _mutate(case, record, storage.prepare_cohort_claim, request_digest=DIGEST)
    dispatched = _mutate(case, prepared, storage.mark_cohort_claim_dispatched)
    with transaction.atomic():
        resolved = storage.resolve_cohort_claim(
            case.owner,
            dispatched.claim_id,
            expected_identity=dispatched.facts.identity,
            expected_revision=dispatched.revision,
            now=case.now,
            kind=CohortResolutionKind.VERIFIED_CANCELLED,
            evidence_digest="sha256:" + "e" * 64,
        )
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLED")
        RayTaskExecution.objects.filter(pk=case.task.pk).update(
            state="QUEUED", attempt_number=record.facts.identity.attempt_number + 1
        )
    case.task.refresh_from_db()
    return resolved


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
def test_resolved_ray_generation_can_continue_on_fresh_draining_same_target(case, family):
    from django_ray.models import RayWorkerTargetCapability
    from django_ray.target.attestation import RayRunnerFamily

    original_arguments = _ray_arguments(case, RayRunnerFamily(family))
    first = _claim(case, **original_arguments)
    original_binding = RayTaskTargetBinding.objects.values().get(execution=case.task)
    first_facts = RayTaskCohortClaim.objects.get(pk=first.claim_id).facts_json
    resolved = _resolve_ray_claim_and_queue_next_attempt(case, first)
    assert resolved.disposition == "RESOLVED"
    assert (case.task.attempt_number, case.task.execution_generation) == (2, 1)
    arguments, draining = _fresh_draining_ray_arguments(case, original_arguments)

    # The old ACTIVE proof is no longer claim authority after policy advancement.
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **original_arguments)
    assert RayTaskCohortClaim.objects.count() == 1
    second = _claim(case, **arguments)
    cap = RayWorkerTargetCapability.objects.get(pk=arguments["capability_id"])
    assert draining.desired_state == "draining" and draining.revision == 3
    assert second.facts.binding == first.facts.binding
    assert second.facts.binding.target_policy_id != draining.pk
    assert second.facts.target_policy_id == cap.target_policy_id == draining.pk
    assert (
        second.facts.claim_attestation_id == cap.attestation_id != first.facts.claim_attestation_id
    )
    assert (second.facts.identity.attempt_number, second.facts.identity.execution_generation) == (
        2,
        2,
    )
    assert RayTaskTargetBinding.objects.values().get(execution=case.task) == original_binding
    assert RayTaskCohortClaim.objects.get(pk=first.claim_id).facts_json == first_facts
    assert RayTaskCohortClaim.objects.get(pk=first.claim_id).disposition == "RESOLVED"
    assert RayTaskCohortClaim.objects.count() == 2
    if family == "ray_job":
        assert second.facts.job_qualification == arguments["job_qualification"]
        assert second.facts.job_qualification != first.facts.job_qualification


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
@pytest.mark.parametrize("disposition", ["OPEN", "HELD"])
def test_unresolved_ray_generation_cannot_advance_despite_fresh_draining_proof(
    case, family, disposition
):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily(family))
    first = _claim(case, **arguments)
    if disposition == "HELD":
        first = _hold(case, first)
    case.task.refresh_from_db()
    before = RayTaskExecution.objects.values().get(pk=case.task.pk)
    arguments, _policy = _fresh_draining_ray_arguments(case, arguments)
    with pytest.raises(DatabaseError), transaction.atomic():
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="QUEUED", attempt_number=2)
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **arguments)
    assert RayTaskExecution.objects.values().get(pk=case.task.pk) == before
    assert RayTaskCohortClaim.objects.count() == 1
    retained = RayTaskCohortClaim.objects.get(pk=first.claim_id)
    assert retained.disposition == disposition and retained.facts_digest == first.facts_digest


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
def test_new_verified_ray_session_cannot_replace_original_execution_binding(case, family):
    from django_ray.models import RayTargetProbeChallenge, RayWorkerTargetCapability
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily(family))
    first = _claim(case, **arguments)
    _resolve_ray_claim_and_queue_next_attempt(case, first)
    binding = RayTaskTargetBinding.objects.values().get(execution=case.task)
    # Ephemeral qualification may disappear; the durable execution binding must not.
    RayWorkerTargetCapability.objects.filter(lease=case.lease).delete()
    RayTargetProbeChallenge.objects.filter(lease=case.lease).delete()
    case.now += timedelta(seconds=2)
    replacement = _ray_arguments(
        case,
        RayRunnerFamily(family),
        target_key="replacement-target",
        cluster_session="session_replacement",
        observed_at=case.now,
        native_job_id="02000000",
    )
    with pytest.raises(storage.CohortClaimStorageError) as crossed:
        _claim(case, **replacement)
    assert crossed.value.reason is storage.CohortClaimStorageReason.BINDING_CHANGED
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **dict(replacement, binding_spec=first.facts.binding))
    assert RayTaskTargetBinding.objects.values().get(execution=case.task) == binding
    assert RayTaskCohortClaim.objects.count() == 1
    case.task.refresh_from_db()
    assert (case.task.state, case.task.attempt_number, case.task.execution_generation) == (
        "QUEUED",
        2,
        1,
    )


def test_capability_expiry_after_final_lock_refuses_claim(case, monkeypatch):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    original = storage._lock_task

    def blocked(pk, *, using):
        row = original(pk, using=using)
        case.now += timedelta(seconds=26)
        return row

    monkeypatch.setattr(storage, "_lock_task", blocked)
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **arguments)
    assert not RayTaskCohortClaim.objects.exists()


def test_late_resolution_does_not_recheck_original_attestation_ttl(case):
    from django_ray.target.attestation import RayRunnerFamily

    record = _claim(case, **_ray_arguments(case, RayRunnerFamily.RAY_CORE))
    record = _mutate(case, record, storage.prepare_cohort_claim, request_digest=DIGEST)
    record = _mutate(case, record, storage.mark_cohort_claim_dispatched)
    record = _hold(case, record)
    case.now = NOW + timedelta(seconds=30)
    resolved = _mutate(
        case,
        record,
        storage.resolve_cohort_claim,
        kind=CohortResolutionKind.APPLICATION_COMPLETED,
        evidence_digest=DIGEST,
    )
    assert resolved.disposition == "RESOLVED"


def test_wrong_package_adopter_and_clock_regression_make_no_writes(case):
    record = _claim(case)
    case.now += timedelta(seconds=5)
    record = _hold(case, record)
    case.lease.is_active = False
    case.lease.stopped_at = case.now
    case.lease.save(update_fields=("is_active", "stopped_at"))
    _, adopter = _lease("adopter")
    # A wrong exact owner snapshot refuses without altering immutable facts.
    wrong = replace(adopter, pid=101)
    for identity, now in ((wrong, case.now), (adopter, NOW + timedelta(seconds=1))):
        case.now = now
        with pytest.raises(storage.CohortClaimStorageError), transaction.atomic():
            storage.adopt_cohort_claim(
                identity,
                record.claim_id,
                expected_identity=record.facts.identity,
                expected_revision=record.revision,
                expected_owner=case.owner,
                now=now,
            )
    case.task.refresh_from_db()
    assert case.task.claimed_by_worker == case.owner.worker_id
    assert RayTaskCohortClaim.objects.get().revision == record.revision


def test_mismatched_package_adopter_refuses(case):
    record = _hold(case, _claim(case))
    case.lease.is_active = False
    case.lease.stopped_at = case.now
    case.lease.save(update_fields=("is_active", "stopped_at"))
    row = TaskWorkerLease.objects.create(
        worker_id="new-package",
        hostname="host",
        pid=200,
        started_at=NOW,
        last_heartbeat_at=NOW,
        capability_schema_version=1,
        django_ray_version="0.5.1",
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    owner = WorkerLeaseIdentity(row.pk, row.hostname, row.pid, row.started_at)
    with pytest.raises(storage.CohortClaimStorageError), transaction.atomic():
        storage.adopt_cohort_claim(
            owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            expected_owner=case.owner,
            now=case.now,
        )
    assert RayTaskCohortClaim.objects.get().revision == record.revision


@pytest.mark.postgresql
@pytest.mark.parametrize("operation", ["claim", "hold", "resolve"])
def test_postgresql_competing_claim_or_disposition_cas_has_one_winner(case, operation):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking is required")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    record = _claim(case) if operation != "claim" else None
    barrier = Barrier(2)

    def race():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if operation == "claim":
                _claim(case)
            elif operation == "hold":
                _hold(case, record)
            else:
                _mutate(
                    case,
                    record,
                    storage.resolve_cohort_claim,
                    kind=CohortResolutionKind.VERIFIED_NOT_INVOKED,
                    evidence_digest=DIGEST,
                )
            return "won"
        except storage.CohortClaimStorageError:
            return "refused"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == ["refused", "won"]
    assert RayTaskCohortClaim.objects.count() == 1


@pytest.mark.parametrize(
    "field,value",
    [("attempt_number", 1.5), ("execution_generation", 0.5), ("execution_generation", -1)],
)
def test_raw_task_counters_cannot_poison_claim_lineage(case, field, value):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite dynamic numeric storage requires exact type fencing")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE django_ray_raytaskexecution SET {field}=%s WHERE id=%s", [value, case.task.pk]
        )


def test_claim_write_failure_rolls_back_binding_and_task(case, monkeypatch):
    from django.db.models.query import QuerySet

    original = QuerySet.create

    def failure(query, **values):
        if query.model is RayTaskCohortClaim:
            raise DatabaseError("secret-free simulated storage refusal")
        return original(query, **values)

    monkeypatch.setattr(QuerySet, "create", failure)
    with pytest.raises(storage.CohortClaimStorageError) as failure:
        _claim(case)
    assert failure.value.reason is storage.CohortClaimStorageReason.PERSISTENCE_REFUSED
    case.task.refresh_from_db()
    assert case.task.state == "QUEUED"
    assert not RayTaskTargetBinding.objects.exists()


@pytest.mark.parametrize("change", ["future", "expired", "runtime", "identity", "sync_capability"])
def test_claim_selection_refusals_make_no_partial_binding(case, change):
    arguments = {}
    if change == "future":
        case.task.run_after = case.now + timedelta(seconds=1)
        case.task.save(update_fields=("run_after",))
    elif change == "expired":
        case.task.queue_deadline_at = case.now
        case.task.save(update_fields=("queue_deadline_at",))
    elif change == "runtime":
        arguments["manager_runtime"] = CohortManagerRuntime("0.5.1", PYTHON)
    elif change == "identity":
        arguments["expected_identity"] = None
    else:
        arguments["capability_id"] = 1
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **arguments)
    assert not RayTaskTargetBinding.objects.exists()


def test_existing_sync_binding_is_preserved_and_mismatch_refused(case):
    RayTaskTargetBinding.objects.create(
        execution=case.task,
        schema_version=2,
        package_version="0.5.0",
        runner_family="sync",
        sync_python_implementation="cpython",
        sync_python_major=3,
        sync_python_minor=12,
        sync_python_patch=14,
        created_at=NOW,
    )
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(
            case,
            binding_spec=CohortBindingSpec(
                CohortRunnerFamily.SYNC, "0.5.0", sync_python=replace(PYTHON, patch=15)
            ),
        )
    assert _claim(case).facts.binding.sync_python == PYTHON


@pytest.mark.parametrize("change", ["missing", "revision", "runtime"])
def test_ray_capability_selection_is_exact(case, change):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_CORE)
    if change == "missing":
        arguments["capability_id"] += 999
    elif change == "revision":
        arguments["capability_revision"] += 1
    else:
        arguments["manager_runtime"] = CohortManagerRuntime("0.5.0", PYTHON, (2, 57, 0))
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **arguments)
    assert not RayTaskCohortClaim.objects.exists()


@pytest.mark.parametrize(
    "change",
    [
        "dispatch_without_prepare",
        "complete_without_dispatch",
        "invalid_resolution",
        "invalid_hold",
        "held_again",
    ],
)
def test_disposition_refusals_preserve_row(case, change):
    record = _claim(case)
    if change == "held_again":
        record = _hold(case, record)
    with pytest.raises(storage.CohortClaimStorageError):
        if change == "dispatch_without_prepare":
            _mutate(case, record, storage.mark_cohort_claim_dispatched)
        elif change in {"complete_without_dispatch", "invalid_resolution"}:
            _mutate(
                case,
                record,
                storage.resolve_cohort_claim,
                kind=CohortResolutionKind.APPLICATION_COMPLETED
                if change == "complete_without_dispatch"
                else "application_completed",
                evidence_digest=DIGEST,
            )
        elif change == "invalid_hold":
            _mutate(
                case,
                record,
                storage.hold_cohort_claim,
                reason="runtime_mismatch",
                boundary=CohortHoldBoundary.OUTER,
                application_invoked=None,
                evidence_digest=DIGEST,
            )
        else:
            _hold(case, record)
    assert RayTaskCohortClaim.objects.get().revision == record.revision


def test_old_generation_cannot_resolve_current_and_binding_survives_retry(case):
    first = _claim(case)
    resolved = _mutate(
        case,
        first,
        storage.resolve_cohort_claim,
        kind=CohortResolutionKind.VERIFIED_NOT_INVOKED,
        evidence_digest=DIGEST,
    )
    RayTaskExecution.objects.filter(pk=case.task.pk).update(state="QUEUED", attempt_number=2)
    case.task.refresh_from_db()
    second = _claim(case)
    assert second.facts.binding == first.facts.binding
    assert second.facts.identity.execution_generation == 2
    with pytest.raises(storage.CohortClaimStorageError):
        _mutate(
            case,
            resolved,
            storage.resolve_cohort_claim,
            kind=CohortResolutionKind.VERIFIED_CANCELLED,
            evidence_digest=DIGEST,
        )


@pytest.mark.parametrize("change", ["same", "invalid", "healthy", "stale_revision"])
def test_adoption_refuses_without_altering_current_or_original_owner(case, change):
    record = _claim(case)
    _, adopter = _lease("adopter")
    arguments = {
        "expected_identity": record.facts.identity,
        "expected_revision": record.revision,
        "expected_owner": case.owner,
        "now": case.now,
    }
    if change == "same":
        adopter = case.owner
    elif change == "invalid":
        arguments["expected_identity"] = None
    elif change == "stale_revision":
        case.lease.is_active = False
        case.lease.stopped_at = case.now
        case.lease.save(update_fields=("is_active", "stopped_at"))
        arguments["expected_revision"] += 1
    with pytest.raises(storage.CohortClaimStorageError), transaction.atomic():
        storage.adopt_cohort_claim(adopter, record.claim_id, **arguments)
    assert RayTaskCohortClaim.objects.get().facts_digest == record.facts_digest


def test_clock_regression_before_claim_rolls_back(case):
    with pytest.raises(storage.CohortClaimStorageError) as error:
        _claim(case, now=case.now + timedelta(seconds=1))
    assert error.value.reason is storage.CohortClaimStorageReason.CLOCK_REGRESSION


@pytest.mark.usefixtures("ledger_database")
def test_reverse_guard_refuses_retained_p3_claim(case):
    import importlib
    from types import SimpleNamespace

    from django.apps import apps

    _claim(case)
    module = importlib.import_module("django_ray.migrations.0030_cohort_claims")
    with transaction.atomic(), connection.cursor() as cursor:
        editor = SimpleNamespace(
            connection=connection, quote_name=connection.ops.quote_name, execute=cursor.execute
        )
        with pytest.raises(RuntimeError, match="Cannot reverse"):
            module._remove(apps, editor)


def test_empty_stored_runtime_env_metadata_is_bound_exactly(case):
    case.task.runtime_env_profile = ""
    case.task.runtime_env_hash = ""
    case.task.save(update_fields=("runtime_env_profile", "runtime_env_hash"))
    record = _claim(case)
    assert record.facts.runtime_env_profile == ""
    assert record.facts.runtime_env_hash == ""
    assert record.facts.runtime_env_snapshot_digest == cohort_task_runtime_env_snapshot_digest(
        profile="", serialized="{}", digest=""
    )


def _advance_shared_job_proof(case, arguments, *, expires_seconds=120, changed_membership=False):
    from django_ray.models import RayWorkerTargetCapability
    from django_ray.target import capabilities, coordination
    from django_ray.target.attestation import (
        RayNodeStateVersion,
        RayRunnerFamily,
        build_ray_cluster_attestation,
        build_ray_node_observation,
        build_ray_observation_boundary,
    )

    original = case.job_receipt.attestation
    boundary, nodes = original.boundary, original.nodes
    if changed_membership:
        node_id = "c" * 56
        versions = (RayNodeStateVersion(node_id, 1),)
        boundary = build_ray_observation_boundary(
            resource_state_version_before=30,
            resource_state_version_after=31,
            node_state_versions_before=versions,
            node_state_versions_after=versions,
        )
        nodes = (
            build_ray_node_observation(
                node_id=node_id,
                cluster_session=original.expectation.cluster_session,
                runtime=original.expectation.runtime,
            ),
        )
    case.now = NOW + timedelta(seconds=2)
    shared = build_ray_cluster_attestation(
        expectation=original.expectation,
        boundary=boundary,
        nodes=nodes,
        observed_at=case.now,
        expires_at=NOW + timedelta(seconds=expires_seconds),
    )
    cap = RayWorkerTargetCapability.objects.get(pk=arguments["capability_id"])
    with transaction.atomic():
        capabilities._locked_exact_lease(case.owner, using="default", vendor=connection.vendor)
        coordination._record_ray_target_attestation_locked(
            original.expectation.target_key,
            shared,
            expected_policy_revision=original.expectation.policy_revision,
            expected_attestation_revision=cap.attestation.revision,
            now=case.now,
            using="default",
        )
        changed = capabilities._advertise_ray_worker_target_capability_locked(
            case.owner,
            original.expectation.target_key,
            original.expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_JOB,
            expected_policy_revision=original.expectation.policy_revision,
            expected_attestation_revision=cap.attestation.revision + 1,
            expected_capability_revision=cap.revision,
            now=case.now,
            using="default",
        )
    arguments["capability_revision"] = changed.revision
    return shared


@pytest.mark.usefixtures("ledger_database")
def test_published_endpoint_provenance_survives_newer_shared_proof(case):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    own = arguments["job_qualification"]
    shared = _advance_shared_job_proof(case, arguments)
    record = _claim(case, **arguments)
    assert case.job_reads == [(own.jobs_endpoint, own.submission_id)]
    assert record.facts.job_qualification == own == case.job_publication.job_qualification
    assert record.facts.claim_attestation_digest == shared.attestation_digest
    assert own.endpoint_attestation_digest != shared.attestation_digest
    assert own.endpoint_expires_at < shared.expires_at


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize("deadline", ["endpoint", "challenge", "shared"])
def test_each_jobs_proof_deadline_is_rechecked_after_task_lock(case, monkeypatch, deadline):
    from django_ray.target.attestation import RayRunnerFamily

    endpoint_ttl, challenge_ttl, shared_ttl, expires = {
        "endpoint": (25, 60, 120, 25),
        "challenge": (120, 10, 120, 10),
        "shared": (120, 60, 5, 5),
    }[deadline]
    arguments = _ray_arguments(
        case, RayRunnerFamily.RAY_JOB, endpoint_ttl=endpoint_ttl, challenge_ttl=challenge_ttl
    )
    _advance_shared_job_proof(case, arguments, expires_seconds=shared_ttl)
    original = storage._lock_task

    def blocked(pk, *, using):
        row = original(pk, using=using)
        case.now = NOW + timedelta(seconds=expires)
        return row

    monkeypatch.setattr(storage, "_lock_task", blocked)
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **arguments)
    case.task.refresh_from_db()
    assert (case.task.state, case.task.execution_generation) == ("QUEUED", 0)
    assert not RayTaskTargetBinding.objects.exists()
    assert not RayTaskCohortClaim.objects.exists()


def test_current_membership_cannot_be_borrowed_for_another_endpoint_observation(case):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    _advance_shared_job_proof(case, arguments, changed_membership=True)
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **arguments)
    assert not RayTaskTargetBinding.objects.exists()


def _rotate_job_challenge(case, arguments):
    from django_ray.target.cohort_probe_challenges import replace_ray_target_probe_challenge

    own = arguments["job_qualification"]
    return replace_ray_target_probe_challenge(
        case.owner,
        own.challenge_id,
        expected_configuration_digest=own.configuration_digest,
        configuration_digest=own.configuration_digest,
        expected_revision=own.consumed_challenge_revision,
        expected_nonce=case.job_issued.nonce,
        now=case.now,
        expected_target_policy_id=arguments["binding_spec"].target_policy_id,
    )


@pytest.mark.usefixtures("ledger_database")
def test_rotated_endpoint_slot_cannot_admit_with_old_qualification(case):
    from django_ray.models import RayTargetProbeJobReceipt
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    _rotate_job_challenge(case, arguments)
    assert not RayTargetProbeJobReceipt.objects.exists()
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **arguments)
    assert not RayTaskCohortClaim.objects.exists()


@pytest.mark.usefixtures("ledger_database")
def test_late_adopted_completion_retains_deleted_endpoint_receipt_audit(case):
    from django_ray.models import RayTargetProbeJobReceipt
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    record = _hold(case, _claim(case, **arguments))
    retained = record.facts
    case.lease.delete()
    assert not RayTargetProbeJobReceipt.objects.exists()
    case.now = NOW + timedelta(seconds=90)
    lease, adopter = _lease("late-jobs-owner")
    lease.last_heartbeat_at = case.now
    lease.save(update_fields=("last_heartbeat_at",))
    with transaction.atomic():
        adopted = storage.adopt_cohort_claim(
            adopter,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            expected_owner=case.owner,
            now=case.now,
        )
    case.owner = adopter
    resolved = _mutate(
        case,
        adopted,
        storage.resolve_cohort_claim,
        kind=CohortResolutionKind.VERIFIED_CANCELLED,
        evidence_digest=DIGEST,
    )
    assert resolved.facts == retained
    assert resolved.facts.job_qualification == arguments["job_qualification"]


def test_jobs_qualification_locks_precede_capability_and_task(case, monkeypatch):
    from django.db.models.query import QuerySet

    from django_ray.models import RayTargetProbeJobReceipt
    from django_ray.target import capabilities
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    events = []

    def observe(module, name, event):
        original = getattr(module, name)

        def wrapped(*args, **kwargs):
            events.append(event)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapped)

    observe(capabilities, "_locked_capability_target", "target")
    observe(storage, "_locked_slot", "challenge")
    observe(capabilities, "_locked_current_capability", "capability")
    observe(storage, "_lock_task", "task")
    original = QuerySet.select_for_update

    def selected(query, *args, **kwargs):
        if query.model is RayTargetProbeJobReceipt:
            events.append("receipt")
        return original(query, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", selected)
    _claim(case, **arguments)
    assert events[: events.index("task") + 1] == [
        "target",
        "challenge",
        "receipt",
        "capability",
        "task",
    ]


def _raw_next_claim_values(case, record):
    _mutate(
        case,
        record,
        storage.resolve_cohort_claim,
        kind=CohortResolutionKind.VERIFIED_NOT_INVOKED,
        evidence_digest=DIGEST,
    )
    RayTaskExecution.objects.filter(pk=case.task.pk).update(state="QUEUED")
    next_facts = replace(
        record.facts,
        identity=replace(
            record.facts.identity,
            execution_generation=record.facts.identity.execution_generation + 1,
        ),
        claimed_at=case.now,
    )
    values = {
        "binding_id": record.facts.binding_id,
        "attempt_number": next_facts.identity.attempt_number,
        "execution_generation": next_facts.identity.execution_generation,
        "target_policy_id": next_facts.target_policy_id,
        "claim_attestation_id": next_facts.claim_attestation_id,
        "claimed_at": next_facts.claimed_at,
        "owner_lease_id": case.owner.worker_id,
        "owner_lease_hostname": case.owner.hostname,
        "owner_lease_pid": case.owner.pid,
        "owner_lease_started_at": case.owner.started_at,
    }
    return values, json.loads(encode_cohort_claim_facts(next_facts))


def _raw_fact_insert(values, payload):
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = (
        "sha256:"
        + hashlib.sha256(
            b"django-ray/cohort-claim-facts/v3/schema2\x00" + serialized.encode("ascii")
        ).hexdigest()
    )
    return RayTaskCohortClaim.objects.create(**values, facts_json=serialized, facts_digest=digest)


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize(
    "field",
    [
        "configuration_digest",
        "jobs_endpoint",
        "challenge_id",
        "request_revision",
        "request_digest",
        "receipt_digest",
        "native_job_id",
        "entrypoint_digest",
        "submitted_control_runtime_env_digest",
        "endpoint_attestation_digest",
        "endpoint_membership_digest",
        "endpoint_observed_at",
        "endpoint_expires_at",
        "receipt_received_at",
        "consumed_at",
        "challenge_expires_at",
    ],
)
def test_raw_jobs_claim_cannot_substitute_endpoint_provenance(case, field):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    values, payload = _raw_next_claim_values(case, _claim(case, **arguments))
    own = payload["job_qualification"]
    if field.endswith("_digest"):
        own[field] = "sha256:" + "f" * 64
        if field == "request_digest":
            own["submission_id"] = "django-ray-cohort-probe-" + "f" * 64
    elif field == "jobs_endpoint":
        own[field] = "http://other-head:8265"
    elif field == "native_job_id":
        own[field] = "02000000"
    elif field in {"challenge_id", "request_revision"}:
        own[field] += 1
        if field == "request_revision":
            own["consumed_challenge_revision"] += 1
    else:
        own[field] = (
            (getattr(arguments["job_qualification"], field) + timedelta(microseconds=1))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw_fact_insert(values, payload)


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize("kind", ["null", "missing", "object", "string"])
def test_raw_sync_claim_requires_explicit_null_jobs_provenance(case, kind):
    values, payload = _raw_next_claim_values(case, _claim(case))
    if kind == "missing":
        del payload["job_qualification"]
    elif kind == "object":
        payload["job_qualification"] = {}
    elif kind == "string":
        payload["job_qualification"] = "null"
    if kind == "null":
        with transaction.atomic():
            _raw_fact_insert(values, payload)
            transaction.set_rollback(True)
    else:
        with pytest.raises(DatabaseError), transaction.atomic():
            _raw_fact_insert(values, payload)


@pytest.mark.usefixtures("ledger_database")
def test_raw_jobs_claim_rejects_expired_endpoint_with_fresh_shared_proof(case):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    _advance_shared_job_proof(case, arguments)
    values, payload = _raw_next_claim_values(case, _claim(case, **arguments))
    expired = arguments["job_qualification"].endpoint_expires_at
    values["claimed_at"] = expired
    payload["claimed_at"] = expired.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw_fact_insert(values, payload)


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize(
    "field", ["challenge_id", "request_revision", "consumed_challenge_revision"]
)
@pytest.mark.parametrize("kind", ["string", "real", "boolean"])
def test_raw_job_qualification_counters_have_exact_integer_types(case, field, kind):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    values, payload = _raw_next_claim_values(case, _claim(case, **arguments))
    value = payload["job_qualification"][field]
    payload["job_qualification"][field] = (
        str(value) if kind == "string" else float(value) if kind == "real" else True
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw_fact_insert(values, payload)


@pytest.mark.parametrize(
    "change", ["missing", "endpoint", "receipt", "control", "native", "deleted"]
)
def test_jobs_service_rejects_missing_or_substituted_publisher_qualification(case, change):
    from django_ray.models import RayTargetProbeJobReceipt
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB)
    own = arguments["job_qualification"]
    if change == "missing":
        arguments["job_qualification"] = None
    elif change == "deleted":
        RayTargetProbeJobReceipt.objects.filter(pk=own.challenge_id).delete()
    elif change == "endpoint":
        arguments["job_qualification"] = replace(own, jobs_endpoint="http://another-head:8265")
    elif change == "receipt":
        arguments["job_qualification"] = replace(own, receipt_digest="sha256:" + "f" * 64)
    elif change == "control":
        arguments["job_qualification"] = replace(
            own, submitted_control_runtime_env_digest="sha256:" + "f" * 64
        )
    else:
        arguments["job_qualification"] = replace(own, native_job_id="02000000")
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, **arguments)
    assert not RayTaskTargetBinding.objects.exists()
    assert not RayTaskCohortClaim.objects.exists()


@pytest.mark.parametrize("family", ["sync", "ray_core"])
def test_nonjobs_service_forbids_jobs_provenance(case, family):
    from django_ray.target.attestation import RayRunnerFamily
    from tests.unit.test_cohort_claim import qualification

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_CORE) if family == "ray_core" else {}
    with pytest.raises(storage.CohortClaimStorageError):
        _claim(case, job_qualification=qualification(), **arguments)
    assert not RayTaskTargetBinding.objects.exists()


@pytest.mark.usefixtures("ledger_database")
@pytest.mark.parametrize("shape", ["numeric-native-id", "missing", "extra", "old-schema"])
def test_raw_jobs_provenance_requires_exact_field_kinds_and_schema(case, shape):
    from django_ray.target.attestation import RayRunnerFamily

    arguments = _ray_arguments(case, RayRunnerFamily.RAY_JOB, native_job_id="10000000")
    values, payload = _raw_next_claim_values(case, _claim(case, **arguments))
    if shape == "numeric-native-id":
        payload["job_qualification"]["native_job_id"] = 10000000
    elif shape == "missing":
        del payload["job_qualification"]["native_job_id"]
    elif shape == "extra":
        payload["job_qualification"]["extra"] = "unrecognized"
    else:
        payload["schema_version"] = 1
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw_fact_insert(values, payload)
