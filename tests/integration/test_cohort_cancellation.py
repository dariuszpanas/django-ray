"""Exact owned cancellation resolves only its retained current generation."""

from dataclasses import replace

import pytest

from django_ray.models import RayTaskCohortClaim, RayTaskExecution
from django_ray.runner import cohort_cancellation as cancellation
from django_ray.runner import cohort_dispatch as dispatch
from django_ray.target.cohort_claim import CohortHoldReason
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import _started
from tests.integration.test_cohort_recovery import _qualify_adopter, _recover

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _cancelling(case, mode, *, held=False):
    value = _started(case, "ray_core" if mode == "core" else "ray_job")
    if held:
        value = dispatch.hold_cohort_dispatch(
            value,
            reason=CohortHoldReason.CANCELLATION_UNCERTAIN,
            now=case.now,
        )
    if mode == "recovered":
        item, _, _ = _qualify_adopter(case, value)
        (value,) = _recover(case, value, item)
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(state="CANCELLING")
    return value


def _apply(case, value, *, kind=None, callback=None):
    def cancelled(task):
        assert RayTaskCohortClaim.objects.get(pk=value.claim.claim_id).disposition == "RESOLVED"
        task.state = "CANCELLED"
        task.save(update_fields=["state"])
        return True

    return cancellation.apply_cohort_cancellation(
        value,
        evidence_kind=kind
        or (
            "owned_core_terminal"
            if value.claim.facts.binding.runner_family == "ray_core"
            else "exact_jobs_stopped"
        ),
        apply_cancel=cancelled if callback is None else callback,
        now=case.now,
    )


@pytest.mark.parametrize("mode", ["core", "jobs", "recovered"])
@pytest.mark.parametrize("held", [False, True])
def test_exact_owned_terminal_cancellation_preserves_claim_history(case, mode, held):
    value = _cancelling(case, mode, held=held)
    before = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    result = _apply(case, value)
    after = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    assert result.applied and not result.retry_admitted
    assert result.dispatch.execution.state == "CANCELLED"
    assert after.resolution_kind == "verified_cancelled"
    assert (after.facts_json, after.held_at, after.hold_evidence_digest) == (
        before.facts_json,
        before.held_at,
        before.hold_evidence_digest,
    )
    with pytest.raises(RuntimeError):
        _apply(case, value)


@pytest.mark.parametrize("mode", ["core", "jobs", "recovered"])
@pytest.mark.parametrize("data", ["{}", "", "malformed-completion"])
def test_any_durable_completion_precedes_terminal_status_cancellation(case, mode, data):
    value = _cancelling(case, mode)
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(completion_data=data)
    result = _apply(case, value, callback=lambda *a: pytest.fail("Completion must win"))
    assert not result.applied
    assert result.dispatch.execution.state == "CANCELLING"
    assert RayTaskCohortClaim.objects.get().revision == value.claim.revision


@pytest.mark.parametrize("mode", ["false", "truthy", "exception", "unchanged", "wrong-terminal"])
def test_false_or_invalid_cancel_callback_rolls_back_resolution(case, mode):
    value = _cancelling(case, "jobs")

    def apply(task):
        if mode != "unchanged":
            task.state = "FAILED" if mode == "wrong-terminal" else "CANCELLED"
            task.save(update_fields=["state"])
        if mode == "exception":
            raise RuntimeError("callback-failed")
        return {"false": False, "truthy": 1}.get(mode, True)

    with pytest.raises(RuntimeError):
        _apply(case, value, callback=apply)
    assert RayTaskExecution.objects.get().state == "CANCELLING"
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


@pytest.mark.parametrize("kind", ["stop_ack", "local_exit", "owned_core_terminal", True])
def test_jobs_cancellation_requires_its_exact_source_evidence_kind(case, kind):
    value = _cancelling(case, "jobs")
    with pytest.raises(cancellation.CohortCancellationError):
        _apply(case, value, kind=kind)
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


def test_running_work_cannot_be_cancelled_without_requested_state(case):
    value = _started(case, "ray_core")
    with pytest.raises(cancellation.CohortCancellationError):
        _apply(case, value)
    assert RayTaskExecution.objects.get().state == "RUNNING"


def test_recovered_cancellation_rejects_crossed_request_digest(case):
    value = _cancelling(case, "recovered")
    with pytest.raises(RuntimeError):
        _apply(case, replace(value, request_digest="sha256:" + "e" * 64))
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"
