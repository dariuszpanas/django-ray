"""Current-cohort SQL admission order and per-candidate claim transactions."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from django.db import connection, transaction

from django_ray import maintenance
from django_ray.models import RayTaskExecution, RayTaskTargetBinding, RayWorkerTargetCapability
from django_ray.runner import cohort_claims as selection
from django_ray.runner.cohort_qualification import (
    EligibleCohortAlias,
    PreparedCohortAlias,
    SharedCohortQualification,
)
from django_ray.runtime.cohort_job import CohortProbeJobLease
from django_ray.target.attestation import RayRunnerFamily, decode_ray_cluster_attestation
from django_ray.target.cohort_claim import CohortManagerRuntime, CohortRunnerFamily
from django_ray.target.cohort_intent_storage import persist_cohort_intent
from tests.integration.test_cohort_claim_storage import (
    _fresh_draining_ray_arguments,
    _ray_arguments,
    _resolve_ray_claim_and_queue_next_attempt,
)
from tests.integration.test_cohort_claim_storage import (
    case as case,
)
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import (
    ledger_database as ledger_database,
)
from tests.unit.test_cohort_claim import DIGEST, NOW, PYTHON

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _alias(case, *, queues=("default",)):
    return selection.CohortClaimAlias(
        case.intent.backend_alias,
        case.intent.configuration_digest,
        case.intent.selection_policy,
        queues,
    )


def _clone(case, *, name, priority=10, queue="default", intent=None):
    intent = intent or case.intent
    with transaction.atomic():
        task = RayTaskExecution.objects.create(
            task_id=name,
            callable_path="tests.tasks.add",
            priority=priority,
            queue_name=queue,
            execution_protocol_version=3,
            created_with_django_ray_version=intent.package_version,
            created_at=NOW,
            runtime_env_hash=hashlib.sha256(b"{}").hexdigest(),
            runtime_env_json="{}",
        )
        persist_cohort_intent(task.pk, intent, now=NOW)
    return task


def _claim(case, *, aliases=None, qualifications=(), family=CohortRunnerFamily.SYNC, limit=1):
    case.monkeypatch.setattr(selection, "_clock", lambda: case.now)
    return selection.claim_cohort_tasks(
        case.owner,
        aliases=aliases or (_alias(case),),
        qualifications=qualifications,
        runner_family=family,
        manager_runtime=CohortManagerRuntime(
            "0.5.0", PYTHON, None if family is CohortRunnerFamily.SYNC else (2, 58, 0)
        ),
        limit=limit,
        now=case.now,
    )


def _qualified(case, arguments, *, alias=None):
    """Test the selector's SQL against source-created proof, not cache recovery."""
    alias = alias or _alias(case)
    cap = RayWorkerTargetCapability.objects.select_related("attestation", "target_policy").get(
        pk=arguments["capability_id"]
    )
    return EligibleCohortAlias(
        PreparedCohortAlias(
            alias.alias, alias.declaration_digest, alias.selection_policy, alias.queues, DIGEST
        ),
        SharedCohortQualification(
            CohortProbeJobLease(
                case.owner.worker_id, case.owner.hostname, case.owner.pid, case.owner.started_at
            ),
            "0.5.0",
            cap.target_policy_id,
            cap.attestation_id,
            cap.pk,
            cap.revision,
            decode_ray_cluster_attestation(cap.attestation.attestation_json),
            cap.target_policy.desired_state,
        ),
        arguments.get("job_qualification"),
    )


def _pause(*scopes, global_claims=False):
    return maintenance.replace_maintenance_policy(
        scopes,
        pause_enqueues=False,
        pause_claims=global_claims,
        expected_revision=maintenance.read_maintenance_policy().revision,
        actor="selector-test",
        reason="bounded-pause",
        authorized=True,
    )


def test_quarantined_high_priority_task_is_filtered_before_limit(case):
    from django_ray.execution_codec import ExecutionIdentity

    high = _clone(case, name="quarantined")
    maintenance.set_task_quarantine(
        ExecutionIdentity(high.pk, high.task_id, high.attempt_number, high.execution_generation),
        quarantined=True,
        expected_revision=0,
        actor="selector-test",
        reason="manual-review",
        authorized=True,
    )
    try:
        claimed = _claim(case)
        assert [item.execution.pk for item in claimed] == [case.task.pk]
        high.refresh_from_db()
        assert (high.state, high.execution_generation) == ("QUEUED", 0)
    finally:
        maintenance.set_task_quarantine(
            ExecutionIdentity(
                high.pk, high.task_id, high.attempt_number, high.execution_generation
            ),
            quarantined=False,
            expected_revision=1,
            actor="selector-test",
            reason="review-complete",
            authorized=True,
        )
        # Match the isolated stopped ledger fixture's parent-first SQLite
        # teardown; production deletion still enforces all protected history.
        if connection.vendor == "sqlite":
            with connection.constraint_checks_disabled(), connection.cursor() as cursor:
                cursor.execute("DELETE FROM django_ray_raytaskexecution WHERE id=%s", [high.pk])


def test_exact_retiring_worker_does_not_take_new_ownership(case):
    maintenance.request_worker_retirement(
        case.owner,
        expected_revision=0,
        actor="selector-test",
        reason="retire-incarnation",
        authorized=True,
    )
    try:
        assert _claim(case) == ()
        case.task.refresh_from_db()
        case.lease.refresh_from_db()
        assert (case.task.state, case.task.execution_generation) == ("QUEUED", 0)
        assert case.lease.is_active
    finally:
        # Lease deletion retains REQUESTED history and is not retirement proof.
        case.lease.delete()


def test_pending_job_cleanup_filters_queued_retry_before_limit_and_rechecks_claim(case):
    from django_ray.target.cohort_claim_storage import CohortClaimStorageError
    from tests.integration.test_cohort_claim_storage import _claim as claim_locked
    from tests.integration.test_cohort_cleanup_recovery import _completed
    from tests.integration.test_cohort_recovery import _arguments

    original, obligation = _completed(case, missing=True, retry=True)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(priority=100, queue_name="default")
    case.task.refresh_from_db()
    ready = _clone(case, name="ready-after-pending-cleanup", priority=1)
    arguments = _arguments(original)
    result = _claim(
        case,
        family=CohortRunnerFamily.RAY_JOB,
        qualifications=(_qualified(case, arguments),),
        limit=1,
    )
    assert [item.execution.pk for item in result] == [ready.pk]
    with pytest.raises(CohortClaimStorageError):
        claim_locked(case, **arguments)
    case.task.refresh_from_db()
    assert (case.task.state, case.task.attempt_number, case.task.execution_generation) == (
        "QUEUED",
        2,
        1,
    )
    assert obligation.state == "OPEN" and obligation.expectation is None


@pytest.mark.parametrize("mismatch", ["configuration", "alias", "package", "selection"])
def test_ineligible_high_priority_intent_is_filtered_before_limit(case, mismatch):
    changes = {
        "configuration": {"configuration_digest": "sha256:" + "d" * 64},
        "alias": {"backend_alias": "other"},
        "package": {"package_version": "0.4.0"},
        "selection": {"selection_policy": selection.CohortSelectionPolicy.JOBS_ONLY},
    }
    high = _clone(case, name="ineligible", intent=replace(case.intent, **changes[mismatch]))
    claimed = _claim(case)
    assert [item.execution.pk for item in claimed] == [case.task.pk]
    high.refresh_from_db()
    assert (high.state, high.execution_generation) == ("QUEUED", 0)
    assert claimed[0].claim.facts.identity.execution_generation == 1


def test_queue_pause_is_filtered_before_limit_and_does_not_pause_other_queue(case):
    high = _clone(case, name="paused", queue="paused")
    _pause(maintenance.MaintenanceScope("queue", queue_name="paused"))
    claimed = _claim(case, aliases=(_alias(case, queues=("paused", "default")),))
    assert [item.execution.pk for item in claimed] == [case.task.pk]
    high.refresh_from_db()
    assert high.state == "QUEUED"


@pytest.mark.parametrize("scope", ["global", "protocol"])
def test_global_and_exact_protocol_pauses_admit_no_new_generation(case, scope):
    _pause(
        *(
            (maintenance.MaintenanceScope("protocol", protocol_version=3),)
            if scope == "protocol"
            else ()
        ),
        global_claims=scope == "global",
    )
    assert _claim(case) == ()
    case.task.refresh_from_db()
    assert (case.task.state, case.task.execution_generation) == ("QUEUED", 0)


@pytest.mark.parametrize("family", [CohortRunnerFamily.RAY_CORE, CohortRunnerFamily.RAY_JOB])
def test_ray_claim_requires_owned_qualification_even_with_database_capability(case, family):
    arguments = _ray_arguments(case, RayRunnerFamily(family.value))
    assert _claim(case, family=family) == ()
    result = _claim(case, family=family, qualifications=(_qualified(case, arguments),))
    assert len(result) == 1
    assert result[0].claim.facts.binding.runner_family is family


@pytest.mark.parametrize("family", [CohortRunnerFamily.RAY_CORE, CohortRunnerFamily.RAY_JOB])
@pytest.mark.parametrize("bound", [False, True])
def test_first_claim_on_draining_target_remains_excluded_before_limit(case, family, bound):
    arguments = _ray_arguments(case, RayRunnerFamily(family.value), active=False)
    if bound:
        RayTaskTargetBinding.objects.create(
            execution_id=case.task.pk,
            schema_version=2,
            runner_family=family.value,
            package_version="0.5.0",
            target_policy_id=arguments["binding_spec"].target_policy_id,
            created_at=NOW,
        )
    assert _claim(case, family=family, qualifications=(_qualified(case, arguments),)) == ()
    case.task.refresh_from_db()
    assert case.task.execution_generation == 0


@pytest.mark.parametrize("family", [CohortRunnerFamily.RAY_CORE, CohortRunnerFamily.RAY_JOB])
def test_draining_continuation_keeps_original_binding_and_target_pause_blocks_it(case, family):
    arguments = _ray_arguments(case, RayRunnerFamily(family.value))
    first = _claim(case, family=family, qualifications=(_qualified(case, arguments),))[0].claim
    _resolve_ray_claim_and_queue_next_attempt(case, first)
    refreshed, draining = _fresh_draining_ray_arguments(case, arguments)
    current = _qualified(case, refreshed)
    _pause(maintenance.MaintenanceScope("target", target_id=draining.target_id))
    assert _claim(case, family=family, qualifications=(current,)) == ()
    _pause()
    second = _claim(case, family=family, qualifications=(current,))[0].claim
    assert second.facts.binding == first.facts.binding
    assert second.facts.target_policy_id == draining.pk != first.facts.binding.target_policy_id


def test_claim_orchestration_refuses_caller_transaction_before_any_admission(case):
    with transaction.atomic(), pytest.raises(selection.CohortSelectionError):
        _claim(case)


@pytest.mark.parametrize("advance_on_entry", [1, 2])
def test_barrier_wait_refreshes_clock_before_selection_or_claim(case, advance_on_entry):
    high = _clone(case, name="expiring")
    RayTaskExecution.objects.filter(pk=high.pk).update(
        queue_timeout_seconds=2, queue_deadline_at=NOW + timedelta(seconds=2)
    )
    original = selection.maintenance_admission_barrier
    entries = 0

    @contextmanager
    def waiting(**kwargs):
        nonlocal entries
        with original(**kwargs) as token:
            entries += 1
            if entries == advance_on_entry:
                case.now = NOW + timedelta(seconds=3)
            yield token

    case.monkeypatch.setattr(selection, "maintenance_admission_barrier", waiting)
    result = _claim(case, limit=1 if advance_on_entry == 1 else 2)
    assert [item.execution.pk for item in result] == [case.task.pk]
    high.refresh_from_db()
    assert (high.state, high.execution_generation) == ("QUEUED", 0)


def test_advisory_queue_selection_is_rechecked_under_claim_locks(case):
    original = selection.claim_cohort_execution

    def changed(*args, **kwargs):
        RayTaskExecution.objects.filter(pk=case.task.pk).update(queue_name="unserved")
        return original(*args, **kwargs)

    case.monkeypatch.setattr(selection, "claim_cohort_execution", changed)
    assert _claim(case) == ()
    case.task.refresh_from_db()
    assert (case.task.state, case.task.execution_generation) == ("QUEUED", 0)
