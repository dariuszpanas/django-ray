"""Cancellation requests remain one-shot across acknowledgment and restart."""

import pytest
from django.db import transaction

from django_ray.models import RayTaskCohortClaim, RayTaskExecution
from django_ray.runner import cohort_cancel_request as control
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import _started

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
@pytest.mark.parametrize("requested", [False, True])
def test_reserved_request_is_never_replayed_after_ack_or_unknown_outcome(case, family, requested):
    value = _started(case, family)
    result = control.reserve_cohort_cancellation(value, request=True, now=case.now)
    assert result.should_request
    assert result.dispatch.execution.state == "CANCELLING"
    assert result.dispatch.execution.cancellation_status == "INDETERMINATE"
    value = control.acknowledge_cohort_cancellation(
        result.dispatch, requested=requested, now=case.now
    )
    assert value.execution.cancellation_status == ("REQUESTED" if requested else "INDETERMINATE")
    assert not control.reserve_cohort_cancellation(value, request=True, now=case.now).should_request
    claim = RayTaskCohortClaim.objects.get()
    assert claim.disposition == "OPEN" and claim.revision == result.dispatch.claim.revision


@pytest.mark.parametrize("data", ["", "{}", "malformed-result"])
def test_any_pending_completion_prevents_a_new_remote_cancellation(case, data):
    value = _started(case, "ray_job")
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(completion_data=data)
    result = control.reserve_cohort_cancellation(value, request=True, now=case.now)
    assert not result.should_request
    assert result.dispatch.execution.state == "RUNNING"
    assert result.dispatch.execution.cancellation_status is None


def test_ordinary_poll_does_not_cancel_running_work(case):
    result = control.reserve_cohort_cancellation(_started(case, "ray_core"), now=case.now)
    assert not result.should_request and result.dispatch.execution.state == "RUNNING"


def test_outer_transaction_refused_before_reserving_remote_request(case):
    value = _started(case, "ray_core")
    with transaction.atomic(), pytest.raises(ValueError):
        control.reserve_cohort_cancellation(value, request=True, now=case.now)
    assert RayTaskExecution.objects.get().state == "RUNNING"


def test_sync_cannot_create_remote_cancellation_metadata(case):
    value = _started(case, "sync")
    with pytest.raises(ValueError):
        control.reserve_cohort_cancellation(value, request=True, now=case.now)
    assert RayTaskExecution.objects.get().cancellation_status is None


def test_explicit_unstarted_operation_releases_local_capacity_reservation(case):
    value = _started(case, "ray_job")
    reservation = control.reserve_cohort_cancellation(value, request=True, now=case.now)
    value = control.release_unstarted_cohort_cancellation(reservation, now=case.now)
    assert value.execution.state == "CANCELLING"
    assert value.execution.cancellation_status is None
    assert control.reserve_cohort_cancellation(value, now=case.now).should_request


def test_unstarted_release_cannot_erase_an_already_observed_acknowledgment(case):
    reservation = control.reserve_cohort_cancellation(
        _started(case, "ray_core"), request=True, now=case.now
    )
    control.acknowledge_cohort_cancellation(reservation.dispatch, requested=True, now=case.now)
    value = control.release_unstarted_cohort_cancellation(reservation, now=case.now)
    assert value.execution.cancellation_status == "REQUESTED"
