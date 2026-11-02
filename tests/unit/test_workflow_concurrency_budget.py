"""Unit tests for cross-run workflow concurrency and shared service budgets (#95)."""

from __future__ import annotations

import pytest

from django_ray.workflow.concurrency import (
    ConcurrencyScopeSpec,
    ExternalServiceBudgetSpec,
    OverlapPolicy,
    RunOwnerIdentity,
    SharedServiceBudgetController,
    StaleConcurrencyLeaseError,
    WorkflowConcurrencyConfigurationError,
    WorkflowConcurrencyCoordinator,
    WorkflowConcurrencyRejectedError,
)


def _owner(pk: int, *, attempt: int = 1, gen: int = 1, priority: int = 0) -> RunOwnerIdentity:
    return RunOwnerIdentity(
        task_execution_pk=pk,
        attempt_number=attempt,
        execution_generation=gen,
        priority=priority,
    )


def test_concurrency_scope_queue_priority_fifo_and_fencing() -> None:
    coord = WorkflowConcurrencyCoordinator()
    coord.register_scope(
        ConcurrencyScopeSpec(
            scope_key="k8s-cluster-prod",
            overlap_policy=OverlapPolicy.QUEUE,
            max_concurrent_runs=1,
            max_queued_runs=4,
            lease_ttl_seconds=30.0,
        )
    )

    run1 = _owner(1, priority=0)
    run2 = _owner(2, priority=0)
    run3_high = _owner(3, priority=10)

    adm1 = coord.request_admission("k8s-cluster-prod", run1, now_monotonic=10.0)
    assert adm1.status == "admitted"
    assert adm1.lease is not None and adm1.lease.fencing_token == 1

    adm2 = coord.request_admission("k8s-cluster-prod", run2, now_monotonic=11.0)
    assert adm2.status == "queued" and adm2.queue_position == 1

    # Higher-priority run3 jumps ahead of run2 in queue.
    adm3 = coord.request_admission("k8s-cluster-prod", run3_high, now_monotonic=12.0)
    assert adm3.status == "queued" and adm3.queue_position == 1

    # Release run1 -> run3_high is promoted with fencing_token=2.
    promoted = coord.release_and_advance(adm1.lease, now_monotonic=15.0)
    assert len(promoted) == 1
    assert promoted[0].owner == run3_high
    assert promoted[0].fencing_token == 2

    # Stale lease from run1 is fenced and rejected.
    with pytest.raises(StaleConcurrencyLeaseError):
        coord.validate_fencing_token(adm1.lease, now_monotonic=15.0)


def test_coalesce_latest_and_reject_overlap_policies() -> None:
    coord = WorkflowConcurrencyCoordinator()
    coord.register_scope(
        ConcurrencyScopeSpec(
            scope_key="reconcile-cluster-a",
            overlap_policy=OverlapPolicy.COALESCE_LATEST,
            max_concurrent_runs=1,
        )
    )
    coord.register_scope(
        ConcurrencyScopeSpec(
            scope_key="strict-singleton",
            overlap_policy=OverlapPolicy.REJECT,
            max_concurrent_runs=1,
        )
    )

    r1, r2, r3 = _owner(10), _owner(11), _owner(12)
    adm1 = coord.request_admission("reconcile-cluster-a", r1, now_monotonic=0.0)
    assert adm1.status == "admitted" and adm1.lease is not None

    adm2 = coord.request_admission("reconcile-cluster-a", r2, now_monotonic=1.0)
    assert adm2.status == "queued" and adm2.superseded_owner is None

    # r3 coalesces and supersedes queued r2!
    adm3 = coord.request_admission("reconcile-cluster-a", r3, now_monotonic=2.0)
    assert adm3.status == "coalesced"
    assert adm3.superseded_owner == r2
    assert coord.superseded_owners == [("reconcile-cluster-a", r2)]

    # When r1 finishes, only r3 is promoted.
    promoted = coord.release_and_advance(adm1.lease, now_monotonic=3.0)
    assert [p.owner for p in promoted] == [r3]

    # REJECT policy refuses concurrent arrivals immediately.
    coord.request_admission("strict-singleton", r1, now_monotonic=0.0)
    with pytest.raises(WorkflowConcurrencyRejectedError, match="OverlapPolicy.REJECT"):
        coord.request_admission("strict-singleton", r2, now_monotonic=1.0)


def test_expired_lease_reclamation_fences_stale_owner() -> None:
    coord = WorkflowConcurrencyCoordinator()
    coord.register_scope(
        ConcurrencyScopeSpec(
            scope_key="ttl-scope",
            overlap_policy=OverlapPolicy.QUEUE,
            max_concurrent_runs=1,
            lease_ttl_seconds=10.0,
        )
    )
    r1, r2 = _owner(1), _owner(2)
    adm1 = coord.request_admission("ttl-scope", r1, now_monotonic=0.0)
    assert adm1.lease is not None and adm1.lease.fencing_token == 1

    # At t=15.0, r1's lease has expired; r2 reclaims the slot with fencing_token=2.
    adm2 = coord.request_admission("ttl-scope", r2, now_monotonic=15.0)
    assert adm2.status == "admitted"
    assert adm2.lease is not None and adm2.lease.fencing_token == 2
    assert len(coord.reclaimed_expired_leases) == 1

    # Stale r1 cannot renew its expired lease.
    with pytest.raises(StaleConcurrencyLeaseError):
        coord.renew_lease(adm1.lease, now_monotonic=16.0)


def test_shared_external_service_budget_fairness_and_throttle_feedback() -> None:
    budget = SharedServiceBudgetController()
    budget.register_service(
        ExternalServiceBudgetSpec(
            service_key="k8s-apiserver",
            max_in_flight=3,
            max_in_flight_per_run=2,
            rate_tokens_per_second=2.0,
            burst_capacity=4,
        ),
        now_monotonic=0.0,
    )
    run_a = _owner(100)
    run_b = _owner(200)

    # run_a acquires 2 grants, then hits per_run_fairness_cap even though service has 3 slots.
    g1, s1 = budget.try_acquire("k8s-apiserver", run_a, now_monotonic=0.0)
    g2, s2 = budget.try_acquire("k8s-apiserver", run_a, now_monotonic=0.0)
    g3_blocked, s3 = budget.try_acquire("k8s-apiserver", run_a, now_monotonic=0.0)
    assert g1 is not None and s1 == "granted"
    assert g2 is not None and s2 == "granted"
    assert g3_blocked is None and s3 == "per_run_fairness_cap"

    # run_b is not starved by run_a and acquires the 3rd slot!
    g_b1, s_b1 = budget.try_acquire("k8s-apiserver", run_b, now_monotonic=0.0)
    assert g_b1 is not None and s_b1 == "granted"

    # Service is now at max_in_flight=3.
    g_b2_blocked, s_b2 = budget.try_acquire("k8s-apiserver", run_b, now_monotonic=0.0)
    assert g_b2_blocked is None and s_b2 == "service_max_in_flight"

    budget.release(g1)
    budget.release(g2)
    budget.release(g_b1)

    # Record 429 Retry-After=5.0s feedback at t=1.0 -> blocks until t=6.0, then refills at 2 tok/s.
    cooldown_until = budget.record_throttle_feedback(
        "k8s-apiserver",
        retry_after_seconds=5.0,
        now_monotonic=1.0,
    )
    assert cooldown_until == 6.0
    g_cool, s_cool = budget.try_acquire("k8s-apiserver", run_a, now_monotonic=4.0)
    assert g_cool is None and s_cool == "throttled_cooldown"

    # At t=6.5 (0.5s after cooldown end), 1 token has refilled (0.5s * 2.0 tok/s = 1.0).
    g_after, s_after = budget.try_acquire("k8s-apiserver", run_a, now_monotonic=6.5)
    assert g_after is not None and s_after == "granted"


def test_invalid_scope_key_rejected() -> None:
    with pytest.raises(WorkflowConcurrencyConfigurationError):
        ConcurrencyScopeSpec(scope_key="INVALID UPPERCASE").validated()


def test_concurrency_and_service_budget_validation_and_renewal_edges() -> None:
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="RunOwnerIdentity"):
        RunOwnerIdentity(0, 1, 1).validate()
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="overlap_policy"):
        ConcurrencyScopeSpec(scope_key="valid-key", overlap_policy="bad").validated()  # type: ignore[arg-type]
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="max_concurrent_runs"):
        ConcurrencyScopeSpec(scope_key="valid-key", max_concurrent_runs=0).validated()
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="max_queued_runs"):
        ConcurrencyScopeSpec(scope_key="valid-key", max_queued_runs=-1).validated()
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="lease_ttl_seconds"):
        ConcurrencyScopeSpec(scope_key="valid-key", lease_ttl_seconds=0.0).validated()

    coord = WorkflowConcurrencyCoordinator()
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="Unregistered scope_key"):
        coord.request_admission("unregistered", _owner(1))

    coord.register_scope(
        ConcurrencyScopeSpec(
            scope_key="tenant:renew",
            overlap_policy=OverlapPolicy.QUEUE,
            max_concurrent_runs=1,
            max_queued_runs=1,
            lease_ttl_seconds=10.0,
        )
    )
    adm = coord.request_admission("tenant:renew", _owner(1), now_monotonic=1.0)
    assert adm.lease is not None
    # Idempotent re-admission returns the same lease
    adm_again = coord.request_admission("tenant:renew", _owner(1), now_monotonic=2.0)
    assert adm_again.lease == adm.lease
    # Renew lease extends expiry
    renewed = coord.renew_lease(adm.lease, now_monotonic=5.0)
    assert renewed.expires_at_monotonic == 15.0
    # Queue full rejection
    coord.request_admission("tenant:renew", _owner(2), now_monotonic=5.0)
    with pytest.raises(WorkflowConcurrencyRejectedError, match="wait queue is full"):
        coord.request_admission("tenant:renew", _owner(3), now_monotonic=5.0)

    # ExternalServiceBudgetSpec validation and rate exhaustion
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="max_in_flight "):
        ExternalServiceBudgetSpec(service_key="svc-1", max_in_flight=0).validated()
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="max_in_flight_per_run"):
        ExternalServiceBudgetSpec(
            service_key="svc-1", max_in_flight=2, max_in_flight_per_run=5
        ).validated()
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="rate_tokens_per_second"):
        ExternalServiceBudgetSpec(service_key="svc-1", rate_tokens_per_second=0.0).validated()
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="burst_capacity"):
        ExternalServiceBudgetSpec(service_key="svc-1", burst_capacity=0).validated()

    budget = SharedServiceBudgetController()
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="Unregistered service_key"):
        budget.try_acquire("missing-svc", _owner(1))
    with pytest.raises(WorkflowConcurrencyConfigurationError, match="Unregistered service_key"):
        budget.record_throttle_feedback("missing-svc", retry_after_seconds=1.0)

    budget.register_service(
        ExternalServiceBudgetSpec(
            service_key="svc-rate",
            max_in_flight=4,
            max_in_flight_per_run=4,
            rate_tokens_per_second=1.0,
            burst_capacity=1,
        )
    )
    g1, s1 = budget.try_acquire("svc-rate", _owner(1), now_monotonic=0.0)
    assert g1 is not None and s1 == "granted"
    g2, s2 = budget.try_acquire("svc-rate", _owner(1), now_monotonic=0.0)
    assert g2 is None and s2 == "rate_budget_exhausted"
